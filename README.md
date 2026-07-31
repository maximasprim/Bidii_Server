# Bidii Credit — Backend API + Admin Dashboard

FastAPI + SQLite backend for the Bidii Credit website, plus a self-contained
admin dashboard. Built as a separate project from the frontend — deploy and
scale independently.

## What's built

**Public API endpoints** (called by the frontend):
- `GET /api/health` — health check
- `POST /api/contact` — Contact page form (matches `Contact.tsx`)
- `POST /api/loan-applications` — Apply page's final submission, validated
  server-side against real product/tier bounds (`app/data/loan_products.py`)
  — rejects unknown product/tier combos, out-of-range amounts/terms, wrong
  term units, each with a clear message
- `POST /api/careers/applications` — Careers page form, including CV (PDF)
  upload via multipart/form-data

**Admin dashboard** at `/admin/` — a single self-contained HTML/CSS/JS page
(no build step, no framework) covering:
- Login (JWT-based sessions)
- **Overview**: total/recent counts, loan applications by product and by
  status, career applications by status, total amount requested
- **Contact Messages**: paginated, filterable by subject
- **Loan Applications**: paginated, filterable by status/product, update
  status inline (pending → contacted → approved/declined)
- **Career Applications**: paginated, filterable by status, update status
  inline, download each applicant's CV
- **Admin Users**: create new admin accounts, view existing ones,
  deactivate accounts (with guardrails — see below)

**Admin auth** — DB-backed, multi-user:
- The first admin is seeded automatically on startup from
  `ADMIN_USERNAME`/`ADMIN_PASSWORD` in `.env` (only if the `admin_users`
  table is empty — this only ever happens once)
- Every admin after that is created **from the dashboard itself**
  (Admin Users tab), not by editing `.env`
- Passwords are bcrypt-hashed, never stored or returned in plaintext
- Login uses constant-time comparison and always runs the password check
  (even for a nonexistent username) to avoid leaking which usernames exist
  via response timing
- `get_current_admin` re-checks the database on **every** request, not just
  at login — deactivating an admin revokes their access immediately, it
  doesn't wait for their existing token to expire
- Guardrails: an admin can't deactivate their own account, and the last
  remaining active admin can't be deactivated by anyone — both exist to
  prevent an accidental total lockout

## Not yet built

- Content endpoints (products, branches, FAQs, etc.) — the frontend still
  reads all of this from its own static `content.ts`; there's no backend
  equivalent yet
- Email/SMS notifications on new submissions (currently just logs)
- Password reset flow for admins (currently: another active admin has to
  deactivate + you re-create the account, or a direct DB edit)

## Getting started

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: set a real ADMIN_PASSWORD and JWT_SECRET before running this
# anywhere but your own machine — the defaults are dev-only placeholders.

uvicorn app.main:app --reload   # http://localhost:8000
```

Then open **http://localhost:8000/admin/** and log in with the
`ADMIN_USERNAME`/`ADMIN_PASSWORD` from your `.env` (defaults: `admin` /
`changeme123`). From there, use the Admin Users tab to create real named
accounts for whoever needs dashboard access, and consider deactivating the
generic `admin` account once you have.

Interactive API docs (Swagger UI) are auto-generated at `/docs`.

## Running tests

```bash
pytest tests/ -v
```

44 tests: 6 contact, 10 loan applications (including every business-rule
rejection path), 7 careers (including file-upload validation), 11 admin
API (auth, stats, listing, status updates), 10 admin user management
(creation, duplicate rejection, deactivation, self/last-admin guardrails).

## Project structure

```
app/
  main.py           FastAPI app, CORS, global exception handler, admin
                     bootstrap, mounts the /admin/ static dashboard
  config.py         Settings (env-driven)
  database.py       SQLAlchemy engine/session
  data/
    loan_products.py   Python mirror of the frontend's tier bounds
    job_openings.py    Python mirror of the frontend's job titles (loose
                        validation — see comments in careers.py)
  models/           SQLAlchemy ORM models (one file per resource)
  schemas/          Pydantic request/response schemas
  routers/          API route handlers (one file per resource)
  services/
    auth.py         Password hashing, JWT issuance/verification,
                     get_current_admin dependency
  static/admin/
    index.html      The entire admin dashboard — HTML + CSS + vanilla JS,
                     talks to /api/admin/* using a bearer token
tests/
  conftest.py       Isolated in-memory SQLite per test run, seeds a test
                     admin (mirrors main.py's real bootstrap, since that
                     only touches the production DB)
  test_*.py
```

## Keeping this in sync with the frontend

`app/data/loan_products.py` and `app/data/job_openings.py` are hand-maintained
mirrors of `src/data/content.ts` on the frontend. If loan tiers or job
listings change on the frontend, update these files too, or:
- new/changed loan tiers → applications for them get rejected as "unknown
  product/plan combination"
- job listings → these are validated loosely (logged, not rejected) since
  they change more often; see the comment in `app/data/job_openings.py`

## Database

SQLite by default (`bidii.db`, created automatically on first run) — zero
setup, fine for getting started or a low-traffic deployment. For production
at scale, point `DATABASE_URL` at Postgres/MySQL and add Alembic for
migrations (tables are currently created via `Base.metadata.create_all`,
which doesn't handle schema changes to existing tables).

## File uploads

Career application CVs are stored under `uploads/careers/`, with the
original filename sanitized and prefixed with a UUID to prevent collisions
and path traversal. Files are only ever served back through the
authenticated `/api/admin/career-applications/{id}/cv` endpoint — never
publicly, since applicant CVs are personal data.

## CORS

Configured via `CORS_ORIGINS` in `.env` — defaults cover the Vite dev
server (`:5173`) and preview server (`:4173`). Add your deployed frontend's
real domain there before going to production.

## Security checklist before deploying anywhere real

- [ ] Change `ADMIN_PASSWORD` in `.env` from the default
- [ ] Change `JWT_SECRET` in `.env` to a real random value (e.g. `openssl rand -hex 32`)
- [ ] Serve over HTTPS (bearer tokens and form submissions should never
      travel over plain HTTP outside local development)
- [ ] Update `CORS_ORIGINS` to your real frontend domain(s) only
- [ ] Consider shortening `JWT_EXPIRY_MINUTES` (currently 8 hours) if that's
      too long a session for your risk tolerance
