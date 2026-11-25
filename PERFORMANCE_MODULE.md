# Staff Performance module

Status: **Phase 2c (part 1: capture).** Staff profiles, units/regions, org settings, rating scales, job roles with JDs/KPIs/versions/AI
drafting, the organisation structure, levels and targets, and now monthly figures with verification and the digital
appraisal. Filled-form upload with AI extraction, the Excel import and dashboards arrive in later phases. See
`PHASE1B_NOTES.md` .. `PHASE2C_NOTES.md` for what is and isn't verified.

## Turning it on and off (rollback)

| `PERFORMANCE_MODULE_ENABLED` | Effect |
|---|---|
| `false` (default) | The app is the original app: no `/api/admin/performance/*` routes, no performance tables created, no seeds, no extra menus, no `staff` role. The module's code is not even imported. |
| `true` | Tables are created (`create_all`), defaults are seeded, menus/role are registered, routes are served. |

**To roll back:** set `PERFORMANCE_MODULE_ENABLED=false` and restart. The data stays in the database (nothing
is dropped), so turning it back on restores everything. Proven by `performance_tests/test_parity_flag_off.py`,
which compares the disabled app to a snapshot of the original (routes, mounts, tables, menus, roles, defaults).

## Two kinds of role (kept separate)

* **Access role** (`admin_users.role`): decides which dashboard menus someone sees. Unchanged, plus one new generic
  role `staff` (own-performance login) that exists only while the module is on.
* **Performance job role** (`performance_roles`): decides how someone is measured. Unlimited, admin-managed.

## Who can see whom (`app/services/performance_scope.py`)

Every performance query goes through `get_performance_scope()`, computed from the database on every request.

| Viewer | Scores & people | Restricted actions (PIP, warnings) |
|---|---|---|
| Staff | self | own |
| Leader / branch manager / regional manager | reporting line + managed units + managed regions | **direct reports only** |
| HR | everyone | everyone |
| Admin | everyone | **everyone (an administrator can do everything HR can)**; every restricted access is audited |
| Executive (CEO flag) | everyone, **read-only** | none |

Out-of-scope ids return the same 404 as ids that don't exist. A profile on a PIP shows as `active` to everyone
outside the PIP audience, and status filters can't be used to find one.

## Scoring rules (`app/services/performance_scoring.py`, pure + unit-tested)

* Achievement % = actual / target x 100 (inverted for lower-is-better KPIs such as PAR).
* Zero / negative / missing target = **not scored** (excluded and reported; never a fake 0 or 100).
* Performance Index = achievement capped at the admin cap (default 150), floored at 0.
* 1-5 ratings convert to PI through an editable map (default 5 to 125, 4 to 110, 3 to 90, 2 to 70, 1 to 50).
* Composite = sum(weight x PI), re-normalised over what was actually scored. Competencies share defaults to 20%.
* Team / branch / region / org = **sum(actual) / sum(target)**, never an average of percentages.
* Categories: Above >= 120, Meeting 90 to <120, Approaching 60 to <90, Below < 60 (seed, admin-editable,
  **effective-dated**: a change is a new scale with a later start date; closed periods keep the scale they had).
  Boundaries are inclusive at the lower bound and classification uses the displayed 2-decimal score.
* Legacy staff-count bands (>100, 80-99, 60-79, <60) are kept alongside; labels are literal, so exactly 100 is "80%-99%".

## Job roles, JDs and KPIs (Phase 2a)

* A **performance role** has versions. Each version holds the JD (title, reports-to, purpose, key
  responsibilities with "% of time" and measurement criteria, mandates, planning, relationships,
  qualifications, competencies, approvals, next-quarter objective rows), the KPI set and the appraisal
  sign-off chain. **Responsibility weights must total 100, and so must KPI weights** (a role with no KPIs
  yet is simply "not configured" and cannot be scored).
* **Versioning:** saving updates the current version in place while it is unused. Once a version is used
  in a scored period it is *locked* (`lock_version`, called by the sales/appraisal code in later phases);
  from then on a save creates the next version and the old one is never changed.
* **Sign-off chain** is per role: staff, manager, then any of CSM / RM-GM / HR; the CEO step is added by a
  switch for senior roles. (The Branch Admin form, for example, has no HR step.)
* **Seeded templates** (version 1, editable): Loan Officer, Credit Risk Analyst and Branch Admin use the
  wording of your own documents; Branch Manager, Check-off Agent and Debt Recovery are *proposals* with no
  source document; HR Officer, Marketing Manager and Head Office Staff are empty skeletons for HR to fill.
* To configure a new role: **Performance settings > Add role**, then **JD & KPIs**. Fill the JD (or
  upload a PDF/DOCX to draft it), add KPIs (or **Suggest KPIs**), set the sign-off chain, **Save**.

### AI drafting ("AI drafts, a human saves")

* `POST /ai/extract-jd` reads an uploaded JD; `POST /ai/suggest-kpis` proposes KPIs. Both return
  **drafts only** - nothing is saved until an admin presses Save.
* **AI never sets a number.** KPI weights are calculated from the responsibility weights the admin already
  approved. With no AI key, or on any AI failure, KPI suggestion falls back to a plain rules proposal; JD
  extraction reports the failure so the JD can be typed in.
* **Only JD text goes out.** The text is cut before the "Acceptance / Approvals" block (which names people),
  and emails and 8+ digit numbers (phones, IDs) are masked. Provider, model and prompt version are stored
  in `ai_outputs` with every result, and identical inputs are served from that cache (there is a
  regenerate option).
* Uploads: the file is sent as the raw request body and read in chunks, aborting at 5 MB; the type is
  decided from the bytes (PDF `%PDF-`, DOCX zip with `word/document.xml`), the extension must agree, legacy
  `.doc` is refused with "save as DOCX or PDF", DOCX zip-bombs and encrypted/scanned PDFs get clear
  errors, and **nothing is stored**. 15 AI/upload requests per user per hour.

## Organisation structure (Phase 2b)

Modelled on the HR induction deck: Directors > CEO > {General Manager - Operations, IT, HR, Finance, Business
Development, Legal}; the GM Operations has the Customer Service Manager (contact centre / office admins), the
Regional Managers (> Branch Managers > Sales Team Leaders > sales executives) and the Credit, Risk and Compliance
Manager. Each person has **one line manager** (`manager_id`); a loop is refused on write and shown (not hidden) by
the tree and flagged by **Structure health** if bad data ever creates one. Functional sign-off steps (e.g. the
Customer Service Manager on an office admin's appraisal) are resolved **by job role** when appraisals are built, not
by a second manager link.

* **Org chart** (`/admin/performance/org`): the reporting tree, limited to the people you may see. A PIP never shows
  (it appears as "active" to anyone not entitled to it).
* **Structure health** (admin/HR): reporting loops and departed managers (errors); people with no manager or branch,
  branches with no manager or region, regions with no manager (warnings); people missing a level, roles with no KPI
  set (notes). The CEO needs no manager.
* **Set up branches and regions** (admin): creates a unit for every branch on the website's list (that list is only
  read, never changed), adds Head Office and Check-off teams (debt collection is not a unit: each branch has its own debt collection staff account, placed in that branch), and proposes three regions from the
  workbook (N. RIFT: Kawangware, CBD, Ngong Road, Nakuru; N. EAST: Kitengela, Embakasi, Machakos, Kajiado; CENTRAL:
  Ruiru, Thika, Karatina = the 11 current branches). Idempotent; never moves a branch already in a region.

## Levels and targets (Phase 2b)

* **Levels** are tiers within a job role. Seeded from the deck: Loan Officer **DSE1 / DSE2 / SNR DSE** (600,000 /
  700,000 / 800,000; PAR 5%), **Auto Champion** tiers (1,500,000 / 2,000,000 / 3,000,000; PAR 7.5%), Check-off
  Officer (1,000,000, from the Check-off sheet, to confirm). Level defaults are only ever **applied explicitly**.
* **Targets are effective-dated and append-only.** A row means "from this month on". The target in force for a month
  is the latest row not after it, so a mid-year change never rewrites earlier months. Every change needs a reason.
  A target of 0 (or none) means the KPI is **not scored**. Only numeric KPIs have targets (rated ones do not).
* **Setting** is admin/HR. A manager who can see someone (and isn't them) can **request** a change; admin/HR decide.
  **Four eyes:** nobody decides a request they made or one about their own target.
* **Bulk tools** (admin/HR): apply level defaults, set one value, or copy forward with an optional % uplift. Always a
  dry-run plan first; applying writes everything in one transaction.
* The disciplinary ladder from the deck (verbal warning, first written warning, final warning, demotion, dismissal)
  is recorded as restricted actions alongside PIPs.

## Monthly figures (Phase 2c)

* A person enters their month's figures (sales disbursed, new customers, PAR, retention: whatever their job role's
  `kpi_entry` KPIs are) in **My Performance**, or HR / their manager / their branch's manager enters them on their
  behalf (staff without a login are still tracked). **Monthly figures** (`/admin/performance/entries`) is the
  verification queue plus on-behalf entry.
* **Window:** open until `sales_entry_window_days` (default 10) after month end; later only HR/admin may enter or amend.
  Future months are refused.
* **Flow:** draft > submitted > verified | sent back. A submitted or verified figure can't be edited (HR can reopen it,
  with a reason). **Four eyes:** the verifier can't be the person, nor the one who entered it. Verifiers are the direct
  manager, the unit's manager and the region's manager (plus HR/admin); a branch office admin sees the branch but does
  not verify.
* **Only verified figures count** towards achievement; the rest show "pending verification". Achievement uses the
  target in force that month (pro-rated for new hires by the ramp-up setting).
* "Use as a starting value" pre-fills from approved website applications assigned to the officer. It is a rough guide
  (approval is not disbursement; only the application's created date is known) and never writes anything by itself.

## Appraisals (Phase 2c)

* The form mirrors the paper one: **A** job objectives, **B** % weight, **C** measurement criteria, **D** self
  assessment, **E** rating, **F** weighted (B x E), **G** manager assessment; plus competencies (if the job role has
  them), objectives for the next 3 months, two training needs, and a comment/sign-off block per step. Rows, weights and
  the sign-off chain are **snapshotted** from the job role when the appraisal starts, and starting one locks that
  version of the role, so later edits never change an appraisal in flight.
* **Chain** (per job role): staff > manager > [CSM] > [RM/GM] > [HR] > [CEO]. Who fills each step is resolved **by role**:
  manager = line manager; CSM = holder of the Customer Service Manager role; RM/GM = the nearest Regional/General Manager
  above the manager (else the unit's region manager, else a General Manager); HR = the HR access role; CEO = the
  executive. **If a step has nobody, HR may act**, so an appraisal can never get stuck. Nobody acts on their own
  appraisal beyond their own staff step; an administrator can act on the HR step like HR.
* Anyone the chain names (e.g. the CSM, the RM/GM, the CEO) can open **that appraisal** even if the person isn't in
  their reporting line; that opens the appraisal only, not the person's profile, targets, figures or actions.
* Staff fill the self-assessment and rate every row, then submit; the manager rates every row and signs; reviewers may
  **return** it one step with a reason. The final rating follows the manager (or the average, per the setting); the
  weighted rating is sum(weight x rating)/sum(weight). A finalised appraisal is read-only; HR/admin can **reopen** it
  with a reason (the history is kept).
* **Disputes:** the appraisee can dispute a rating once the manager has rated; HR resolves (optionally changing the
  manager's rating). Open disputes block the HR step and the final step. The appraisee can **acknowledge** a finalised appraisal.
* **Printable PDF** of any appraisal, and a **blank form** per job role. Every view, download and change is audited by id
  only; appraisal text is never written to the audit log.

## Excel import wizard (Phase 2d)

Admin or HR only: Staff Performance > **Import from Excel** (`/admin/performance/import`).

1. **Upload** the `.xlsx` (checked by its bytes, size-capped while streaming, macros and zip bombs refused). The year is read
   from the file (the COMPANY sheet's title, or the Year cell on the clean template) or typed in. The parsed workbook is held in
   the database (never as a file) until you commit or discard it.
2. **Review**: each sheet is mapped to a unit (existing, new, or skipped); each name is matched to a person (exact; near-identical
   spellings; otherwise *you* decide), new, or recognised as a team row ("Debt Recovery ..."). Names that could be two people
   block the import until decided.
3. **Dry run**: the same code as the commit, rolled back; the counts are exactly what a commit does.
4. **Commit**: one transaction, all or nothing. **Undo** reverses a run (records changed since are left alone and counted).

What it reads: columns are found by header (month blocks). The amount is the month cell; an unlabelled 0-100 number in the block
is that month's PAR %; text in the block is a note that belongs to that month. **Cached percentages are never used** (they are
recomputed from amount and target), and formulas pointing at the wrong row are reported. A blank or 0 target means *not scored*.
Subtotal rows, hidden sheets, copied month columns, amounts that are formulas, and future months are reported or skipped.

What it writes: figures as **verified** `kpi_entries` (source `import`, entered by "import", verified by the person who ran it), targets (effective from
the person's first month; existing targets are kept unless you choose to replace them), people and units you approved, transfers
as assignment history, and notes as **performance actions** (`talk`, `pip`, `pip_extended`, `pip_lifted`, `coaching`, `promotion`,
`probation_confirm`, `probation_extend`, `transfer`, `exit`, warnings...). Notes that aren't actions (contract notes) are listed, not imported.
Re-importing updates earlier imported figures and never duplicates. A figure someone entered in the system is **kept** unless you tick "overwrite".

PIP and warning notes are restricted: **HR and administrators** read and import them (an administrator can do everything HR can); everyone else never sees them. A PIP that was never marked lifted sets the person's status to `pip` only while it is
still current.

Reconciliation (information only): the COMPANY sheet's branch totals against what the branch sheets add up to, and the hand-counted
JAN-OCT staff bands against counts recomputed from amount and target. Use the first to decide which months to **exclude**.

**Version control** (Import from Excel > Workbook versions). Every posted import is a numbered version of its year's workbook (v1, v2, ...; never renumbered). *Unpost* reverses a version and needs a reason; it is refused while a newer version is still posted unless you unpost the newer ones with it (newest first). *Restore to this version* unposts the newer versions and, if that version was itself unposted, posts it again as a new version ("Restore of v2"), all in one transaction (it completes or nothing changes). *Compare* lists the amounts that changed, appeared or vanished between two versions. A locked month blocks unposting and restoring until HR reopens it; a figure someone changed after an import is kept, not overwritten. Unposted versions stay listed with who unposted them and why. Endpoints: `GET /import/versions`, `GET /import/versions/compare`, `GET /import/{id}/restore-plan`, `POST /import/{id}/unpost`, `POST /import/{id}/restore`, `PUT /import/{id}/label`. The old `/undo` route obeys the same rules.

Rollback: nothing here runs with `PERFORMANCE_MODULE_ENABLED=false`. Undo a run from the list of past imports.

## Menus for roles that already have saved permissions

Saved per-role overrides replace defaults, so adding a path to the defaults alone would not reach them. On startup
(only when enabled) `append_menus_to_role_overrides` appends `/admin/my-performance` and `/admin/performance` to every
saved override. It is additive only, never duplicates, and runs once per path (recorded in `performance_config`), so
an admin who later unticks a menu is not overruled by the next restart.

## Phase 6: reminders, weekly view, 360 feedback, cascading goals, board pack

- **6a Email/SMS reminders**: each person chooses email and/or SMS (`/notifications/prefs/me`); messages carry counts and generic wording only. One send per person, channel, kind and day (`ReminderDelivery`, no message text stored). SMS goes through Africa's Talking (`PERFORMANCE_SMS_*`). HR/admin see delivery counts in Governance.
- **6b Weekly "who needs me"**: `GET /ops/weekly` (a manager's own team, ranked by attention reasons) and cron `POST /api/internal/performance/weekly-digest` (counts-only message).
- **6c 360 feedback**: cycles (draft, open, closed, released) run by HR; raters are anonymous except the manager; a rater group under `min_group` (default 3) is never shown; comments are shuffled and unattributed; results are visible only after release (subject, direct manager, HR/admin).
- **6d Goal cascading**: company, region and branch targets per additive KPI with a live gap (balanced, still to allocate, over-allocated). HR/admin can preview a largest-remainder split and apply it (writes ordinary targets with source `cascade`, reason recorded). Append-only.
- **6e Monthly board pack**: one A4 page of company, region and branch figures (achievement, year to date, projection, categories, strongest and weakest branches, reporting completeness, appraisal counts, cascade gap). No individual names, PIPs or appraisal content. Admin, HR and executives only. `POST /pack/generate`, `GET /pack/list`, `GET /pack/download/{id}`, `GET /pack/preview`; cron `POST /api/internal/performance/monthly-pack` (same secret header) builds last month's pack once, notifies admin/HR/executives and sends a counts-only email/SMS (no attachment; they sign in to download). Stored privately under `performance/packs/`. UI: *Board pack* page.
- Internal cron routes are now `daily-digest`, `weekly-digest`, `monthly-pack`.

## Branches, regions and placing everyone
Staff Performance > Organisation chart > **Branches and regions** (admin edits; HR sees it and can place people). Branches come from the website's branch list (read only). The first-time wizard uses a fixed proposal from the 2025 workbook; after that this editor is the source of truth: add, rename or remove regions, move any branch or team to a region (history is kept), set unit and regional managers, add teams, and create units for any branch that has none. **Place people in the structure** proposes, for people with no unit or no line manager, a unit (from the branch on their login, or their role: head-office roles to Head Office, check-off agents to Check-off; debt collection accounts go to the branch on their login, as every branch has its own) and a line manager (the unit's manager, else the single branch manager there; a branch manager's manager is their region's manager). Nothing is written until you accept rows (untick to leave someone); the step is one transaction, refuses loops, and records each change in the person's assignment history. Endpoints: `GET /org-setup/layout`, `GET /org-setup/placement`, `POST /org-setup/placement/apply`.

**Duplicates and linking (admin).** *Possible duplicates* finds an empty unit that looks like a second copy of a unit with people (for example "Nairobi CBD" made by setup next to the imported "Cbd") and, when accepted, removes only the empty copy, links the kept unit to the website branch and can take over the branch's name. A unit with people, history, score snapshots or cascade targets is never removed. Any branch unit can also be linked to a website branch from its row; an unlinked website branch suggests the imported unit that looks like it. Endpoints: `GET /org-setup/duplicates`, `POST /org-setup/duplicates/clean-up`, `POST /org-setup/units/{id}/link-branch`, `DELETE /org-setup/units/{id}` (empty units only).

**Closed branches and regions.** A branch or team that stops operating (for example Kasarani) is *closed*, not deleted: its people move to another unit you choose (recorded in their history), its region link ends, it leaves the active lists, and everything already reported for it stays on record. `POST /org-setup/units/{id}/close`. Only a unit nothing refers to (no people, history, snapshots or cascade targets) can be deleted. Regions can be renamed, given a manager, and removed once they hold no branches (move or close those first).

## Logins and staff stay in step
Every login is also a staff member (`performance_sync.py`). A user created in Admin Users gets a staff profile (role "General staff" until HR assigns the real one; name taken from an optional `full_name`, else from the username). A staff profile created by an administrator can create the login in the same step (`login` in the request). Logins that already exist are backfilled at startup, linking to an existing unlinked profile only on an exact, unique, case-insensitive name match. Switching a login off or on is mirrored on the profile; nothing is ever deleted.

## Running the tests (isolated - never touches real data)

```bash
cd bidii-credit-backend
bash performance_tests/run.sh          # SQLite in memory: module enabled, then flag-off parity
```

`performance_tests/conftest.py` forces an in-memory SQLite database, dummy Supabase config and blank AI/SMTP settings
*before* importing the app, and aborts if anything real got through (for example a developer `.env`). To also run
against Postgres, start a **throwaway local** database named `perf_test...` and:

```bash
PERF_TEST_DB_URL=postgresql+psycopg://postgres@localhost:5433/perf_test_db python -m pytest performance_tests -q
```

The URL is rejected unless it points at localhost and a `perf_test*` database.

## Data protection notes

* Only `staff_profiles.admin_user_id` references `admin_users` (ON DELETE SET NULL), so the existing hard-delete user
  flow cannot hit a foreign-key conflict.
* The audit log stores ids, field names and action types only - never note text, names or ID numbers.
* Units are separate from the public `branches` table, so nothing here can publish a branch to the website.

## Scores and reports (Phase 3)

Everything is computed live from **verified** figures by `services/performance_reports.py` (batch-loaded: a fixed number
of queries whether there are 50 or 500 staff). Routes under `/api/admin/performance/reports`, all behind the
My Performance menu guard and `get_performance_scope`; an id outside your scope answers 404, exactly like one that does not exist.

| Route | Who | What |
|---|---|---|
| `GET /me`, `/person/{id}` | yourself / anyone in your scope | score gauge data, KPI breakdown, monthly trend, quarter-to-date, year-to-date, "how this was calculated" |
| `GET /team` | leaders (default: you; `?leader_id=`) | team score, per-person table, who needs attention, leader vs team, month heatmap |
| `GET /unit/{id}` | the branch's managers, HR, admin, executive | heatmap, vs organisation, "small branch" caution under 4 people |
| `GET /region/{id}` | the region's manager, HR, admin, executive | branch comparison, category mix, target vs actual |
| `GET /organisation` | admin, HR, executive only | category distribution by month, branch x month heatmap, target vs actual by branch, top and bottom movers, KES vs target |
| `GET /compare` | anyone, for what they can see | two periods side by side (month, quarter, year on year) |
| `GET /export?kind=&id=&period=&format=xlsx\|pdf` | as above | download; text cells are neutralised against formula injection; rate-limited and audited |
| `POST /snapshots/recompute`, `GET /snapshots` | admin/HR; own history | stored results per person and period, with the rating-scale date they were computed under |

Periods: `2026-03`, `2026-Q1`, `2026-H1`, `2026`, `ytd`, and a custom range `2026-01..2026-06` (up to 36 months).

How a score is built: per KPI, the verified figure against the target in force that month (pro-rated for new hires);
money and counts are pooled (sum actual / sum target), percentages are averaged, "lower is better" is inverted; months with
nothing verified are reported as pending or unreported, never as 0. Rated KPIs and competencies come from the latest
finalised appraisal overlapping the period. The score re-normalises over the KPIs that could be scored, then blends in
competencies at the configured share. The category and 1-5 rating use the rating scale in force on the period's last day.
Groups: Σ actual / Σ target, target-weighted average score, and the people counted in each category and legacy band.
A person's rank among peers is shown to others always, and to the person only if `show_peer_rankings` is on.
A PIP status or attention note is shown only to HR, the direct manager and the person concerned.
Viewing someone else's report is written to the audit log (ids only).

## Filled forms, evidence and the Word template (Phase 2c, second half)

Routes (all behind the performance menu guard and the person's scope; out-of-scope ids are 404; uploads and AI calls are rate-limited):

| Route | What |
|---|---|
| `POST /appraisals/{id}/upload?filename=` | stream a PDF, DOCX, PNG or JPG (5 MB cap enforced while streaming, type from the bytes, `.doc` refused, DOCX checked for zip bombs); stored privately as `performance/appraisals/<id>/<random>.<ext>` |
| `POST /appraisals/{id}/extract` | text is extracted, **pseudonymised** (the person and their managers become "Staff A" / "Manager B", e-mails and long numbers masked), and the AI maps it onto THIS appraisal's rows as strict JSON; unknown keys and ratings outside 1-5 are dropped; every row has a confidence, low ones are flagged; provider, model and prompt version are recorded; the result is only a draft |
| `GET /appraisals/{id}/extraction` | the draft, the original text and the current form, for the side-by-side review |
| `POST /appraisals/{id}/extraction/confirm` | a person confirms the **reviewed** values (tick-box required); only now are they written. The appraisee can confirm only their own columns; HR/admin or the direct manager can also record the manager column |
| `DELETE /appraisals/{id}/extraction` | discard the draft; the original stays attached |
| `GET /appraisals/{id}/file` | the original as an attachment (`nosniff`, audited) |
| `GET /roles/{id}/appraisal-template.docx` | the blank form in the paper form's columns, editable in Word (the PDF version already existed) |
| `POST/GET /entries/{id}/evidence` | attach / download a PDF, PNG or JPG for a monthly figure (not once it is verified) |

Needs an AI key for reading forms; without one the endpoint answers 503 "enter it by hand" and the file stays attached.
Comments found on a form are shown for reference but not saved: sign-off comments belong to the sign-off steps.

### Photos and scans

A PNG/JPG, or a scanned PDF (no text layer), is read **on your own server** with Tesseract (`pytesseract`; the Dockerfile installs `tesseract-ocr`).
Nothing is sent out as an image: the text then goes through the same route as any form (pseudonymise, AI mapping, side-by-side review, confirm).
Images are opened defensively (25 MP / 9000 px caps, structure verified, EXIF-rotated, greyscaled, contrast-stretched, 40 s timeout); scanned PDFs use the
images embedded in their first 10 pages. The reading quality (0-100) is shown on the review screen and **scales every row's confidence**, so a poor scan is flagged rather than trusted.
Handwriting is read poorly: every value must still be checked. If Tesseract isn't installed the file is attached and the screen says to enter the appraisal by hand.

## Phase 4a: projections, suggestions and AI advice

`/api/admin/performance/insights` (menu guard + scope on every route; out-of-scope ids answer 404).

| Route | What it does |
|---|---|
| `GET /projection/me`, `/person/{id}`, `/unit/{id}`, `/region/{id}`, `/organisation` | Quarter-end and year-end projection from verified months (50/50 recency-weighted run-rate and linear trend), the monthly run-rate needed to reach "Meeting Expectation", and bootstrap category chances as **ranges**. Needs 3+ scored months. Groups sum projections over sum of targets. Organisation view: admin/HR/executive only. |
| `GET /recommendations` | Only what the caller may see. PIP-related suggestions: HR/admin, the direct manager and the person. A person sees only suggestions meant for them (strong run, overdue appraisal). |
| `POST /recommendations/generate` | Runs the rules for the caller's reporting line (HR/admin: everyone). Idempotent per month; a dismissed suggestion is not recreated that month. Staff and the read-only executive get 403. |
| `PATCH /recommendations/{id}` | acknowledge / action / dismiss (dismiss needs a note); due date and assignee for managers. A person may only acknowledge. Audited. |
| `POST /recommendations/{id}/advice` | Optional AI advice from **pseudonymised** figures only ("Staff A", no names, ids or unit names). Rate limited; cached by input hash; any number not in the input rejects the answer; falls back to the rule text. Not available to the person concerned. |

Rules (thresholds from the early-warning settings): coaching after sustained months below threshold, PIP suggestion, PIP lift / expiring, sustained strong run, quality warning, target review, probation decision due, appraisal overdue, branch intervention.
Suggestions never change a score, rating, status or PIP. Actioned ones get a follow-up outcome (score then vs now) after 30 days.
UI: *Suggestions* page (from Staff Performance), "Where I'm heading" on My Performance.

## Phase 4b-1: governance (`/api/admin/performance/governance`)

- **Period locks** (HR/admin): lock finished months (`2026-03`, `2026-Q1`...). Saving, submitting, verifying or reopening a figure, and importing, are refused for a locked month; reopening needs a reason (audited, shown in the lock list). Open months are unaffected.
- **Staff data-use notice**: versioned text, shown as a banner on My Performance until acknowledged (never blocks). HR/admin see who hasn't acknowledged.
- **Retention**: leavers past `retention_years` are listed; nothing is removed automatically. Admin only, typing `ANONYMISE`: name, employee number, login link, action/appraisal/dispute/sign-off text, evidence and uploaded files are removed; the figures remain as statistics. Irreversible; audited.
- **Access log**: who viewed a person's report or appraisal. Visible to HR/admin and the person themselves (not to their manager).

## Phase 4b-2: operations (`/api/admin/performance/ops`)

- **Action tracker**: open coaching / PIP / warning items with review dates, overdue flag; update dates or note, or close with an outcome (closed items are immutable). Restricted items: HR or the direct manager only to change; HR, direct manager and the person to see.
- **Reminders and daily digest**: `GET /reminders/me`; `POST /api/internal/performance/daily-digest` for cron with header `X-Cron-Secret` (= `PERFORMANCE_CRON_TOKEN`; 404 if unset, 401 if wrong, constant-time compare). Creates in-app notifications only, one digest per person per day, counts and generic wording only (no PIP, warning or name).
  Covers: results due, figures awaiting verification (direct manager), overdue appraisals, check-ins due.
- **Anomalies** (a figure 5x above or below the person's own median) and **data-quality** checks (no unit/manager/targets, duplicate employee numbers, no results last month, probation date passed, exit date passed): HR/admin.
- **Calibration**: average score by line manager against the company (HR/admin/executive; teams under 3 hidden). **Branch leaderboard** (small units flagged). **Lifecycle timeline**. **Training themes** (units with fewer than 4 appraised people hidden).
- UI: *Operations* page. Email/SMS reminders are in Phase 6a.

## Phase 5: hardening and checks

- `performance_tests/test_authz.py` enumerates every `/api/admin/performance/*` route and fails if one has no menu/role guard or answers an anonymous caller with anything but 401/403; the only route outside that system is `POST /api/internal/performance/daily-digest` (secret header, 404 when unset). `test_hardening.py` also checks that the new request bodies forbid unknown fields and that spreadsheet formula injection is neutralised (`=`, `+`, `-`, `@`, tab, CR, leading spaces).
- Upload abuse (type from bytes, size cap while streaming, `.doc` refused, zip bombs, generated storage names, traversal names) is covered in `test_files.py`, `test_ai_roles.py` and `test_import.py`.
- Speed: 500 staff x 24 months, the organisation report, calibration, data quality, anomalies, leaderboard, training themes, projections and a full suggestion run all complete in seconds with a bounded query count.
- The original project's 87 tests give the same result before and after (21 fail, 66 pass, the same 21; they fail on a test-fixture error that pre-dates this work).

## Running the tests
From the backend folder: `PERF_TEST_FLAG=on python -m pytest performance_tests -q` and `PERF_TEST_FLAG=off python -m pytest performance_tests/test_parity_flag_off.py -q`.


## Debt collection

Debt collection is a unit **under each branch** ("Karatina Debt Collection"), not one team for the whole company.

- **Creating them:** Staff Performance > Organisation chart > "Add Debt Collection to every branch" (or `POST /org-setup/debt-collection-units`). One unit per open branch, kind `team`, `parent_unit_id` = the branch, in the branch's region. Safe to run again; an existing unit with the same name is adopted.
- **Region:** a unit under a branch follows the branch when the branch moves region. Under-branch units never show as units of their own in the region lists; they appear under their branch.
- **Reporting:** debt collectors (role `debt_recovery`) report to the **Debt Collection Manager** (a role named "Debt Collection Manager"; matched by key or name), who reports to the **General Manager** (role `general_manager`). "Place people in the structure" proposes this when there is exactly one of each; a debt collector's unit is their branch's Debt Collection unit, found from the branch on their login.
- **More than one branch:** a collector has one main unit on their profile; the extra branches they cover are in `staff_unit_coverage` (`GET/PUT /staff/{id}/coverage`, admin or HR; "Also covers" on the staff form). Coverage records who covers what, it does not move anyone. Closing a unit ends coverage of it.
- **Not done yet:** collections are still one monthly figure per person per KPI. Recording and targeting collections **per branch** is a separate step.


## Importing a Job Description (JD)

`POST /api/admin/performance/ai/extract-jd` turns an uploaded JD into a draft role (nothing is saved until an admin saves it).

* **.docx in the house layout** (info box, "Key Responsibilities | % of Time | Performance Measurement Criteria" table, "Other Responsibilities", qualifications box, competencies box) is read **directly** by `app/services/jd_docx_reader.py` - no AI, no key needed, exact and repeatable. It follows the *layout*, not the wording, so any number of responsibilities, duties or competencies, and loosely-worded labels, are fine. The response has `ai.provider = "local"`.
* **PDFs, scans and free-form documents** (and any .docx not in that layout) go to the AI extractor exactly as before.
* Add `?force_ai=true` to skip the direct reader for a .docx.
* Both paths run the same `performance_roles.validate_jd` rules and return problems as `warnings` (weights not totalling 100, a responsibility with no criteria, missing purpose/qualifications/competencies). Nothing is silently "fixed".
* Not imported: approvers and reporting relationships (set in the app). The competency list is whatever the document contains.
* Tests: `performance_tests/test_jd_docx_import.py` (real JDs in `performance_tests/fixtures/jds/` plus layout-mutation cases).


## Outlook chart and saved plans

On a person's report (and My Performance) the **Outlook** card plots, month by month, as a percentage of target:

* **Actual** - the scored months (the last 12 months to today, so it carries on across the year end).
* **Projection** - the next 3 months from the same method as the projections above, with a shaded 90% range from the person's own month-to-month swing. Needs at least 3 scored months.
* **Path needed** - a straight line from the latest month to a goal (default Meeting Expectation) over 3-6 months. It is arithmetic: what would have to happen. It is not a prediction, and the AI never writes these numbers.
* **Saved plan** - once a manager or HR saves a plan, its goal and path are frozen in `performance_plans` and drawn as a fourth line; later months are compared with it ("on track", "behind", "goal reached", or "ended without reaching the goal").

Endpoints (prefix `/api/admin/performance/insights`): `GET /outlook/me`, `GET /outlook/person/{id}`, `POST /plans`, `GET /plans/person/{id}`, `POST /plans/{id}/close`.

* **Who can save or end a plan:** managers and HR/admin for people they can see. Not the executive view, and not someone setting their own plan (HR/admin excepted). The person can see their own plan's goal and path, but not the manager's note or who saved it.
* **The server computes the path.** `POST /plans` takes only the person, optional goal and length, an optional suggestion id and a note; any other field is refused.
* **One active plan per person**, enforced by the database (unique `profile_id` + `active_slot`). Saving the same plan again changes nothing; saving a different one closes the old one as "superseded" and keeps how it stood. Ending a plan keeps its history and how it stood that day.
* **A plan never changes a score, rating, status or PIP**, and never writes to the suggestion's own `outcome` (the follow-up check owns that).
* **If saved plans can't be read** the chart still loads (`plans_unavailable`).
* **Anonymising a person** removes the notes on their plans; the goal and path numbers stay as anonymous statistics.
* Tests: `performance_tests/test_outlook.py`, `performance_tests/test_plans.py`.

## People who have left

Branch, region, organisation and team totals (the KES actual and target, and the achievement %) include the months a person worked before they left, so a result does not change when someone exits.

* **What counts:** every verified month they were in that unit, up to and including the month of their `exit_date` (a month typed after their exit never counts). "Left" means a status other than active / probation / PIP, or a deactivated profile.
* **What does not change:** they are not listed as a person, not counted in "People", and do not feed the average score or the category mix. Those describe the current team.
* **On screen:** the Achievement card says "includes N who have left" when this applies, and a **Staff who have left** card lists each of them (when they left, the months counted, actual and target, achievement) with a total of what they add. In the Team and Branch / unit tabs a row opens that person's details. No new improvement plan can be saved for someone who has left.
* **Compare, month-by-month and region/organisation branch tables** follow the same rule. Individual reports, snapshots and recommendations are unchanged.
* Tests: `performance_tests/test_leavers.py`.

## Speed notes

* **Report setup is a fixed number of queries.** `load_ctx` reads the organisation settings once and every role's current JD and KPIs in two queries (`performance_batch.py`), instead of several queries per role. The org health check does the same. Answers are identical; `test_speed.py` checks both that and that the query count does not grow with the number of roles.
* **Organisation report:** it no longer builds a second data set just to read the rating scale, and each branch's leavers are scored only against the branches they worked in.
* **Staff page:** the search waits 300 ms after you stop typing, and the role and manager lists are fetched on first load and after a save, not on every keystroke.
* **Report pages** show the last copy of a report you opened (same signed-in person, same period and choices, under 5 minutes old) while the fresh one loads, then replace it. Kept in memory only.
