"""
Staff Performance - the Excel import wizard (Phase 2d): matching, planning, committing, undoing, reconciling.

How a run works
  1. PREVIEW   the uploaded workbook is parsed (performance_import_parse) and stored on a `performance_imports`
               row. The preview proposes: which sheet is which unit, which sheet name is which person (exact /
               fuzzy match to existing profiles, new person, team row), and lists every data-quality problem.
  2. DRY RUN   the admin's decisions are applied to the database inside a transaction that is then ROLLED BACK.
               It is the same code as the commit, so the counts shown are exactly what a commit will do.
  3. COMMIT    the same code, committed in ONE transaction (all or nothing). Every record it creates or changes
               gets an `import_rows` entry with the previous state.
  4. UNDO      reverses a committed batch (records changed by someone since the import are left alone and reported).

Rules
  * Idempotent: re-importing updates earlier imported figures; it never duplicates anything.
  * Figures that someone entered in the system (not by import) are never overwritten unless asked.
  * Imported figures are recorded as VERIFIED (the admin confirmed them in the wizard), entered by "import",
    so the four-eyes rule still holds and they count towards scores.
  * Notes become performance actions. Restricted ones (PIP, warnings) are read, shown and imported ONLY for HR:
    an admin importing sees how many there are, never what they say.
  * No dry run or commit touches role-version locks or the audit log until the end, and only on a real commit.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from calendar import monthrange
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import (
    ImportRow,
    KpiEntry,
    PerformanceAction,
    PerformanceImport,
    PerformanceRole,
    PerformanceTarget,
    PerformanceUnit,
    StaffAssignmentHistory,
    StaffProfile,
)
from app.services import performance_entries as pe
from app.services import performance_governance as gov
from app.services.performance_config import RESTRICTED_ACTION_TYPES, log_audit
from app.services.performance_import_parse import BAND_KEYS, parse_workbook
from app.services.performance_roles import latest_version, lock_version
from app.services.performance_scoring import achievement_pct, legacy_band
from app.services.performance_targets import clean_value as clean_target, in_force_map
from app.services.performance_time import today_nairobi

IMPORT_SOURCE = "import"
IMPORT_ENTERED_BY = "import"
DEFAULT_OPTIONS: dict[str, Any] = {
    "include_par": True,
    "import_targets": True,
    "import_actions": True,
    "create_missing_staff": True,
    "overwrite_manual": False,
    "overwrite_targets": False,
    "default_role_key": "loan_officer",
    "exclude_months": [],
}
FUZZY_MIN = 0.82
FUZZY_AUTO = 0.97  # near-identical spellings are accepted without asking
_SHEET_ROLE_HINTS = (("checkoff", "check_off_agent"),)
_SKIP_SHEET_NAME = re.compile(r"^sheet\d+$", re.IGNORECASE)


class ImportProblem(ValueError):
    """A problem the admin can fix; the message is safe to show."""


# ---- names ---------------------------------------------------------------------------------------------------------


def _tokens(name: str) -> list[str]:
    return re.sub(r"[^a-z0-9\s]", "", name.lower()).split()


def key_of(name: str) -> str:
    return " ".join(sorted(_tokens(name)))


def display_name(name: str) -> str:
    """ALL-CAPS names in the sheets become Title Case; anything already mixed-case is left as typed."""
    return name.title() if name.isupper() else name


def similarity(a: str, b: str) -> float:
    ta, tb = set(_tokens(a)), set(_tokens(b))
    ratio = difflib.SequenceMatcher(None, key_of(a), key_of(b)).ratio()
    if len(ta) >= 2 and len(tb) >= 2 and (ta <= tb or tb <= ta):
        ratio = max(ratio, 0.9)  # "Josephine Muriithi" vs "Josephine W Muriithi"
    return ratio


def _unit_norm(text: str) -> str:
    t = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    t = " ".join({"rd": "road"}.get(tok, tok) for tok in t.split())
    return t.replace(" ", "")


# ---- notes -> actions ----------------------------------------------------------------------------------------------

_DURATION = re.compile(r"(\d+)\s*(?:months?|mths?|m)\b", re.IGNORECASE)


def classify_note(text: str) -> dict[str, Any] | None:
    """A sheet note -> {action_type, outcome, months?}; None if it isn't a recognisable action (contract notes etc.)."""
    t = text.strip().lower()
    if not t:
        return None
    months = None
    m = _DURATION.search(t)
    if m:
        months = int(m.group(1))
    if "pip" in t:
        if "lift" in t:
            return {"action_type": "pip_lifted", "outcome": None, "months": None}
        if re.search(r"\bext", t):
            return {"action_type": "pip_extended", "outcome": None, "months": months}
        if re.search(r"\brev", t):
            return {"action_type": "coaching", "outcome": "PIP review", "months": None}
        return {"action_type": "pip", "outcome": "Quality" if "quality" in t else None, "months": months}
    if "extend" in t and "probation" in t:
        return {"action_type": "probation_extend", "outcome": None, "months": months}
    if "confirm" in t:
        return {"action_type": "probation_confirm", "outcome": None, "months": None}
    if "promot" in t:
        return {"action_type": "promotion", "outcome": None, "months": None}
    if "transfer" in t or "transition" in t:
        return {"action_type": "transfer", "outcome": text.strip()[:200], "months": None}
    if "exit" in t:
        return {"action_type": "exit", "outcome": text.strip()[:200] if t != "exit" else None, "months": None}
    if "dismiss" in t:
        return {"action_type": "dismissal", "outcome": None, "months": None}
    if "demot" in t:
        return {"action_type": "demotion", "outcome": None, "months": None}
    if "final" in t and "warn" in t:
        return {"action_type": "final_warning", "outcome": None, "months": None}
    if "warn" in t:
        return {"action_type": "verbal_warning", "outcome": None, "months": None}
    if "quality" in t:
        return {"action_type": "coaching", "outcome": "Quality issue", "months": None}
    if "talk" in t or "sitdown" in t or "sit down" in t:
        esc = "Escalated to the GM" if re.search(r"\bgm\b", t) else "Escalated to the RM" if re.search(r"\brm\b", t) else None
        return {"action_type": "talk", "outcome": esc, "months": None}
    return None


def _month_end(month: str) -> date:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, monthrange(y, m)[1])


def _add_months(d: date, n: int) -> date:
    idx = d.year * 12 + (d.month - 1) + n
    y, m = divmod(idx, 12)
    return date(y, m + 1, min(d.day, monthrange(y, m + 1)[1]))


# ---- preview -------------------------------------------------------------------------------------------------------


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def create_preview(db: Session, data: bytes, filename: str, username: str, year: int | None = None) -> PerformanceImport:
    """Parses the workbook and stores it (as JSON) for the wizard. Writes no performance data."""
    parsed = parse_workbook(data, year)
    imp = PerformanceImport(
        original_name=(filename or "workbook.xlsx").rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:150],
        file_sha256=sha256(data), status="previewed", parsed=parsed, options=dict(DEFAULT_OPTIONS), decisions={},
        created_by_username=username,
    )
    db.add(imp)
    db.commit()
    db.refresh(imp)
    return imp


def suggest_sheet_units(db: Session, parsed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    units = db.query(PerformanceUnit).filter(PerformanceUnit.is_active.is_(True)).all()
    by_norm = {_unit_norm(u.name): u for u in units}
    out: dict[str, dict[str, Any]] = {}
    for s in parsed["sheets"]:
        if s["kind"] != "staff":
            continue
        n = _unit_norm(s["name"])
        unit = by_norm.get(n) or next((u for k, u in by_norm.items() if len(n) >= 3 and (n in k or k in n)), None)
        role_key = next((rk for hint, rk in _SHEET_ROLE_HINTS if hint in n), None)
        if _SKIP_SHEET_NAME.match(s["name"].strip()):
            out[s["name"]] = {"action": "skip", "unit_id": None, "reason": "A working copy ('Sheet2'): its rows repeat other sheets.", "role_key": role_key}
        elif unit is not None:
            out[s["name"]] = {"action": "use", "unit_id": unit.id, "unit_name": unit.name, "role_key": role_key}
        else:
            kind = "team" if "check" in n else "branch"
            out[s["name"]] = {"action": "create", "unit_id": None, "new_unit": {"name": s["name"].strip().title() if s["name"].isupper() else s["name"].strip(), "kind": kind}, "role_key": role_key}
    return out


def _effective_sheet_units(db: Session, parsed: dict[str, Any], decisions: dict[str, Any]) -> dict[str, dict[str, Any]]:
    base = suggest_sheet_units(db, parsed)
    for sheet, dec in (decisions.get("sheets") or {}).items():
        if sheet in base and isinstance(dec, dict):
            base[sheet] = {**base[sheet], **{k: v for k, v in dec.items() if k in ("action", "unit_id", "new_unit", "role_key")}}
    return base


class _Person:
    def __init__(self, key: str, name: str) -> None:
        self.key, self.name = key, name
        self.rows: list[tuple[str, dict[str, Any]]] = []  # (sheet name, parsed row)
        self.is_team_row = True


def collect_people(parsed: dict[str, Any], sheet_units: dict[str, dict[str, Any]]) -> dict[str, _Person]:
    people: dict[str, _Person] = {}
    for s in parsed["sheets"]:
        if s["kind"] != "staff" or sheet_units.get(s["name"], {}).get("action") == "skip":
            continue
        for row in s["rows"]:
            if not row["months"] and row["target"] is None:
                continue
            p = people.setdefault(row["key"], _Person(row["key"], row["name"]))
            p.rows.append((s["name"], row))
            p.is_team_row = p.is_team_row and row["is_team_row"]
    return people


def resolve_people(db: Session, people: dict[str, _Person], decisions: dict[str, Any], options: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{key: {state, profile_id?, candidates[], action}} - state: matched | new | team_row | ambiguous | decided."""
    profiles = db.query(StaffProfile).all()
    by_key: dict[str, list[StaffProfile]] = {}
    for pr in profiles:
        by_key.setdefault(key_of(pr.full_name), []).append(pr)
    staff_dec = decisions.get("staff") or {}
    out: dict[str, dict[str, Any]] = {}
    for key, person in people.items():
        dec = staff_dec.get(key)
        exact = by_key.get(key, [])
        fuzzy = sorted(
            ((similarity(person.name, pr.full_name), pr) for pr in profiles if key_of(pr.full_name) != key),
            key=lambda x: -x[0],
        )
        cands = [{"profile_id": pr.id, "full_name": pr.full_name, "score": round(sc, 2)} for sc, pr in fuzzy if sc >= FUZZY_MIN][:4]
        info: dict[str, Any] = {"name": person.name, "candidates": cands, "sheets": sorted({s for s, _ in person.rows})}
        if isinstance(dec, dict) and dec.get("action") in ("match", "create", "skip"):
            if dec["action"] == "match" and any(p.id == dec.get("profile_id") for p in profiles):
                info.update(state="decided", action="match", profile_id=dec["profile_id"])
            elif dec["action"] == "create":
                info.update(state="decided", action="create", role_key=dec.get("role_key"))
            elif dec["action"] == "skip":
                info.update(state="decided", action="skip")
            else:
                info.update(state="ambiguous", action=None)
        elif len(exact) == 1:
            info.update(state="matched", action="match", profile_id=exact[0].id, matched_name=exact[0].full_name)
        elif len(exact) > 1:
            info.update(state="ambiguous", action=None, candidates=[{"profile_id": pr.id, "full_name": pr.full_name, "score": 1.0} for pr in exact])
        elif person.is_team_row:
            info.update(state="team_row", action="skip")
        elif cands and cands[0]["score"] >= FUZZY_AUTO and (len(cands) == 1 or cands[1]["score"] < cands[0]["score"] - 0.05):
            info.update(state="matched", action="match", profile_id=cands[0]["profile_id"], matched_name=cands[0]["full_name"], fuzzy=True)
        elif cands:
            info.update(state="ambiguous", action=None)
        else:
            info.update(state="new", action="create" if options.get("create_missing_staff", True) else "skip")
        out[key] = info
    return out


def preview_payload(db: Session, imp: PerformanceImport, is_hr: bool) -> dict[str, Any]:
    """Everything the wizard shows. Restricted note text is withheld from non-HR callers."""
    parsed = imp.parsed
    sheet_units = _effective_sheet_units(db, parsed, imp.decisions or {})
    people = collect_people(parsed, sheet_units)
    resolved = resolve_people(db, people, imp.decisions or {}, imp.options or DEFAULT_OPTIONS)
    sheets = []
    for s in parsed["sheets"]:
        entry: dict[str, Any] = {"name": s["name"], "kind": s["kind"], "hidden": s.get("hidden", False)}
        if s["kind"] == "staff":
            entry.update(rows=len(s["rows"]), months=[m[-2:] for m in s["months"]], mapping=sheet_units.get(s["name"]))
        sheets.append(entry)
    names = [
        {"key": k, "name": v["name"], "state": v["state"], "action": v.get("action"), "profile_id": v.get("profile_id"),
         "matched_name": v.get("matched_name"), "candidates": v["candidates"], "sheets": v["sheets"]}
        for k, v in sorted(resolved.items(), key=lambda kv: (kv[1]["state"] not in ("ambiguous",), kv[1]["name"].lower()))
    ]
    return {
        "id": imp.id, "status": imp.status, "original_name": imp.original_name, "year": parsed["year"], "sheets": sheets,
        "names": names, "options": imp.options or DEFAULT_OPTIONS, "decisions": imp.decisions or {},
        "unresolved": sum(1 for v in resolved.values() if v["state"] == "ambiguous"),
        "issues": parsed["issues"], "issue_counts": parsed["issue_counts"], "issues_dropped": parsed.get("issues_dropped", 0),
        "reconciliation": reconcile(parsed, sheet_units, db),
        "summary": imp.summary, "is_hr": is_hr,
    }


# ---- the run (dry run and commit share this) ----------------------------------------------------------------------


class _Run:
    def __init__(self, db: Session, imp: PerformanceImport, username: str, is_hr: bool) -> None:
        self.db, self.imp, self.username, self.is_hr = db, imp, username, is_hr
        self.options = {**DEFAULT_OPTIONS, **(imp.options or {})}
        self.counts: dict[str, int] = {}
        self.issues: list[dict[str, Any]] = []
        self.rows: list[ImportRow] = []
        self.locked_roles: set[str] = set()
        self.touched_profiles: dict[str, StaffProfile] = {}
        self.restricted_skipped = 0
        self.unclassified: dict[str, int] = {}
        self.today = today_nairobi()
        self._kpi_cache: dict[str, dict[str, Any]] = {}

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def issue(self, code: str, severity: str, message: str, sheet: str | None = None, month: str | None = None) -> None:
        if len(self.issues) < 400:
            self.issues.append({"code": code, "severity": severity, "message": message, "sheet": sheet, "month": month})
        self.bump(f"issue:{code}")

    def record(self, entity_type: str, entity_id: str, action: str, before: dict | None = None, after: dict | None = None) -> None:
        self.rows.append(ImportRow(import_id=self.imp.id, entity_type=entity_type, entity_id=entity_id, action=action, before=before, after=after))

    def kpis(self, role_id: str) -> dict[str, Any]:
        if role_id not in self._kpi_cache:
            self._kpi_cache[role_id] = pe.entry_kpis(self.db, role_id)
        return self._kpi_cache[role_id]

    def amount_key(self, role_id: str) -> str | None:
        return next((k for k, v in self.kpis(role_id).items() if v.unit == "kes" and v.direction == "higher"), None)

    def par_key(self, role_id: str) -> str | None:
        return next((k for k, v in self.kpis(role_id).items() if v.unit == "percent" and v.direction == "lower"), None)


def _pick_month_cells(person: _Person, preferred_sheet: str | None, run: _Run) -> dict[str, tuple[str, dict[str, Any]]]:
    """For each month, the one (sheet, cell) to use when a person appears on several sheets (transfer / duplicate)."""
    by_month: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for sheet, row in person.rows:
        for month, cell in row["months"].items():
            by_month.setdefault(month, []).append((sheet, cell))
    chosen: dict[str, tuple[str, dict[str, Any]]] = {}
    excluded = set(run.options.get("exclude_months") or [])
    for month, cells in by_month.items():
        if month in excluded:
            run.bump("months_excluded")
            continue
        if len(cells) == 1:
            chosen[month] = cells[0]
            continue
        with_amount = [c for c in cells if c[1].get("amount") is not None]
        distinct = {c[1]["amount"] for c in with_amount}
        if len(distinct) > 1:
            pick = next((c for c in with_amount if c[0] == preferred_sheet), with_amount[0])
            run.issue("conflicting_sheets", "warning", f'"{person.name}" has different {month} amounts on {len(with_amount)} sheets; the one from "{pick[0]}" was used.', pick[0], month)
            chosen[month] = pick
        else:
            pick = next((c for c in cells if c[0] == preferred_sheet), cells[0])
            if len(with_amount) > 1:
                run.issue("duplicate_across_sheets", "info", f'"{person.name}" appears with the same {month} amount on more than one sheet; counted once.', pick[0], month)
            chosen[month] = pick
    return chosen


def _entry_status_ok(entry: KpiEntry) -> bool:
    return entry.source == IMPORT_SOURCE


def _apply_entry(run: _Run, profile: StaffProfile, key: str, month_iso: str, value: Any, sheet: str, is_new_profile: bool) -> None:
    kpis = run.kpis(profile.performance_role_id)
    kpi = kpis.get(key)
    if kpi is None:
        run.bump("entries_skipped")
        return
    month = date(int(month_iso[:4]), int(month_iso[5:7]), 1)
    if month > date(run.today.year, run.today.month, 1):
        run.issue("future_month", "warning", f"{month_iso} hasn't started yet, so it was skipped.", sheet, month_iso)
        run.bump("entries_skipped")
        return
    if gov.is_locked(run.db, month):
        run.issue("locked_month", "warning", f"{month_iso} is locked, so it was skipped.", sheet, month_iso)
        run.bump("entries_skipped")
        return
    try:
        v = pe.clean_value(kpi, value)
    except pe.EntryError as exc:
        run.issue("bad_value", "warning", f'"{profile.full_name}" {month_iso}: {exc}', sheet, month_iso)
        run.bump("entries_skipped")
        return
    existing = None if is_new_profile else run.db.query(KpiEntry).filter_by(profile_id=profile.id, kpi_key=key, month=month).first()
    if existing is None:
        e = KpiEntry(
            profile_id=profile.id, kpi_key=key, month=month, value=v, status="verified", source=IMPORT_SOURCE,
            entered_by_username=IMPORT_ENTERED_BY, verified_by_username=run.username, verified_at=_now(),
        )
        run.db.add(e)
        run.db.flush()
        run.record("entry", e.id, "created", None, {"value": str(v)})
        run.bump("entries_created")
        run.locked_roles.add(profile.performance_role_id)
        return
    if Decimal(existing.value) == v:
        run.bump("entries_unchanged")
        return
    if existing.source != IMPORT_SOURCE and not run.options.get("overwrite_manual"):
        run.issue("kept_manual_figure", "warning", f'"{profile.full_name}" {month_iso}: a figure entered in the system differs from the workbook; the system figure was kept.', sheet, month_iso)
        run.bump("entries_kept")
        return
    before = {"value": str(existing.value), "status": existing.status, "source": existing.source, "entered_by_username": existing.entered_by_username,
              "verified_by_username": existing.verified_by_username}
    existing.value, existing.status, existing.source = v, "verified", IMPORT_SOURCE
    existing.entered_by_username, existing.verified_by_username, existing.verified_at = IMPORT_ENTERED_BY, run.username, _now()
    existing.rejection_reason = None
    run.record("entry", existing.id, "updated", before, {"value": str(v)})
    run.bump("entries_updated")


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _apply_target(run: _Run, profile: StaffProfile, role_id: str, target: float | None, first_month: str, sheet: str, is_new_profile: bool) -> None:
    if not run.options.get("import_targets") or target is None:
        return
    key = run.amount_key(role_id)
    if key is None:
        return
    month = date(int(first_month[:4]), int(first_month[5:7]), 1)
    try:
        v = clean_target(target)
    except Exception:
        return
    current = None if is_new_profile else in_force_map(run.db, [profile.id], month, [key]).get((profile.id, key))
    if current is not None and Decimal(current.value) == v:
        run.bump("targets_unchanged")
        return
    if current is not None and not run.options.get("overwrite_targets"):
        run.issue("target_differs", "info", f'"{profile.full_name}": the workbook target ({v:,.0f}) differs from the one already set; the existing target was kept.', sheet, first_month)
        run.bump("targets_kept")
        return
    row = PerformanceTarget(
        profile_id=profile.id, kpi_key=key, effective_from=month, value=v, source=IMPORT_SOURCE,
        reason=f"Imported from {run.imp.original_name} (one target column per person)"[:300], set_by_username=run.username,
    )
    run.db.add(row)
    run.db.flush()
    run.record("target", row.id, "created", None, {"value": str(v)})
    run.bump("targets_created")


def _apply_actions(run: _Run, profile: StaffProfile, person: _Person, chosen: dict[str, tuple[str, dict[str, Any]]], is_new: bool) -> None:
    if not run.options.get("import_actions"):
        return
    made: list[tuple[date, str, PerformanceAction | None, int | None]] = []
    for month in sorted(chosen):
        sheet, cell = chosen[month]
        note = cell.get("note")
        if not note:
            continue
        for part in [p for p in note.split(" / ") if p.strip()]:
            c = classify_note(part)
            if c is None:
                run.unclassified[part.strip().lower()[:60]] = run.unclassified.get(part.strip().lower()[:60], 0) + 1
                run.bump("notes_unclassified")
                continue
            restricted = c["action_type"] in RESTRICTED_ACTION_TYPES
            if restricted and not run.is_hr:
                run.restricted_skipped += 1
                run.bump("actions_restricted_skipped")
                continue
            started = _month_end(month)
            ends = _add_months(started, c["months"]) if c.get("months") else None
            exists = run.db.query(PerformanceAction).filter_by(
                profile_id=profile.id, action_type=c["action_type"], started_on=started, source=IMPORT_SOURCE, note=part.strip()[:300]
            ).first() if not is_new else None
            if exists is not None:
                run.bump("actions_unchanged")
                made.append((started, c["action_type"], exists, c.get("months")))
                continue
            a = PerformanceAction(
                profile_id=profile.id, action_type=c["action_type"], restricted=restricted, started_on=started, ends_on=ends,
                outcome=c.get("outcome"), note=part.strip()[:300], source=IMPORT_SOURCE, raised_by_username=run.username,
            )
            run.db.add(a)
            run.db.flush()
            run.record("action", a.id, "created", None, {"type": a.action_type})
            run.bump("actions_created")
            made.append((started, a.action_type, a, c.get("months")))
    # A PIP is "open" only if its chain wasn't lifted and it hasn't clearly run its course.
    pips = [m for m in made if m[1] in ("pip", "pip_extended", "pip_lifted")]
    if pips and run.is_hr:
        started, kind, action, _ = sorted(pips, key=lambda m: m[0])[-1]
        if kind in ("pip", "pip_extended") and profile.status == "active":
            end = action.ends_on if action is not None and action.ends_on else _add_months(started, 1)
            latest = max((date(int(m[:4]), int(m[5:7]), 1) for m in chosen), default=None)
            if latest is not None and end >= latest:
                before = {"status": profile.status}
                profile.status = "pip"
                run.record("profile", profile.id, "updated", before, {"status": "pip"})
                run.bump("profiles_set_pip")
            else:
                run.issue("pip_not_lifted", "info", f'"{profile.full_name}" has a PIP in the notes that was never marked lifted, but it ended before the latest month; status left unchanged.', None, None)


def execute(db: Session, imp: PerformanceImport, username: str, is_hr: bool) -> dict[str, Any]:
    """Applies the wizard's decisions to the session WITHOUT committing. The caller commits (real run) or rolls back (dry run)."""
    run = _Run(db, imp, username, is_hr)
    parsed, decisions = imp.parsed, imp.decisions or {}
    sheet_units = _effective_sheet_units(db, parsed, decisions)
    people = collect_people(parsed, sheet_units)
    resolved = resolve_people(db, people, decisions, run.options)
    pending = [v["name"] for v in resolved.values() if v["state"] == "ambiguous"]
    if pending:
        raise ImportProblem(f"{len(pending)} name(s) still need a decision (match to an existing person, create, or skip): " + ", ".join(sorted(pending)[:8]) + ("..." if len(pending) > 8 else ""))

    # units
    unit_ids: dict[str, str | None] = {}
    for sheet, m in sheet_units.items():
        if m.get("action") == "skip":
            unit_ids[sheet] = None
        elif m.get("action") == "create":
            nu = m.get("new_unit") or {}
            name = str(nu.get("name") or "").strip()
            if not name or nu.get("kind") not in ("branch", "team", "head_office"):
                raise ImportProblem(f'Sheet "{sheet}": give the new unit a name and a type (branch, team or head office).')
            existing = db.query(PerformanceUnit).filter(PerformanceUnit.name == name).first()
            if existing is None:
                existing = PerformanceUnit(name=name, kind=nu["kind"])
                db.add(existing)
                db.flush()
                run.record("unit", existing.id, "created", None, {"name": name})
                run.bump("units_created")
            unit_ids[sheet] = existing.id
        else:
            uid = m.get("unit_id")
            if not uid or db.get(PerformanceUnit, uid) is None:
                raise ImportProblem(f'Sheet "{sheet}": choose which unit it is, create a new one, or skip it.')
            unit_ids[sheet] = uid

    roles = {r.key: r for r in db.query(PerformanceRole).all()}

    for key, person in people.items():
        res = resolved[key]
        if res["action"] == "skip":
            run.bump("people_skipped")
            if res["state"] == "team_row":
                run.issue("team_row_skipped", "info", f'"{person.name}" looks like a team row, not a person, so it was skipped. Create it as a person in the wizard if you track it that way.', person.rows[0][0])
            continue
        is_new = res["action"] == "create"
        profile = db.get(StaffProfile, res["profile_id"]) if not is_new else None
        sheets_used = [s for s, _ in person.rows]
        default_sheet = None
        if profile is not None and profile.unit_id:
            default_sheet = next((s for s, uid in unit_ids.items() if uid == profile.unit_id and s in sheets_used), None)
        chosen = _pick_month_cells(person, default_sheet, run)
        if is_new:
            last_sheet = chosen[max(chosen)][0] if chosen else sheets_used[-1]
            role_key = res.get("role_key") or sheet_units.get(last_sheet, {}).get("role_key") or run.options["default_role_key"]
            role = roles.get(role_key)
            if role is None or latest_version(db, role.id) is None:
                run.issue("missing_role", "error", f'"{person.name}": the job role "{role_key}" doesn\'t exist or has no KPI set, so they were skipped.', last_sheet)
                run.bump("people_skipped")
                continue
            profile = StaffProfile(full_name=display_name(person.name), performance_role_id=role.id, unit_id=unit_ids.get(last_sheet), status="active")
            db.add(profile)
            db.flush()
            run.record("profile", profile.id, "created", None, {"name_set": True})
            run.bump("profiles_created")
            first_month = min(chosen) if chosen else None
            stretches: list[tuple[str, str]] = []  # (first month, sheet) for each change of sheet
            for mth in sorted(chosen):
                if not stretches or stretches[-1][1] != chosen[mth][0]:
                    stretches.append((mth, chosen[mth][0]))
            if len(stretches) > 1:
                run.issue("transfer_detected", "info", f'"{person.name}" appears on {len(stretches)} sheets in different months; treated as a transfer and assignment history was recorded.', last_sheet)
            for i, (mth, sheet) in enumerate(stretches or [(first_month or f"{parsed['year']}-01", last_sheet)]):
                start = date(int(mth[:4]), int(mth[5:7]), 1)
                end = None
                if i + 1 < len(stretches):
                    nxt = stretches[i + 1][0]
                    end = date(int(nxt[:4]), int(nxt[5:7]), 1).replace(day=1)
                    end = date.fromordinal(end.toordinal() - 1)
                ah = StaffAssignmentHistory(profile_id=profile.id, unit_id=unit_ids.get(sheet), performance_role_id=role.id, effective_from=start, effective_to=end, reason=f"Imported from {imp.original_name}", created_by=username)
                db.add(ah)
                db.flush()
                run.record("assignment", ah.id, "created", None, None)
        else:
            if len(set(s for s, _ in person.rows)) > 1:
                run.issue("multi_sheet_person", "info", f'"{person.name}" appears on more than one sheet; unit and history on their existing profile were left as they are.', sheets_used[0])
        if profile is None:
            continue
        run.touched_profiles[profile.id] = profile
        role_id = profile.performance_role_id
        a_key, p_key = run.amount_key(role_id), run.par_key(role_id)
        latest_sheet = chosen[max(chosen)][0] if chosen else None
        row_target = next((row["target"] for sh, row in person.rows if sh == latest_sheet and row["target"] is not None), None)
        if row_target is None:
            row_target = next((row["target"] for _, row in person.rows if row["target"] is not None), None)
        has_target = row_target is not None
        for month in sorted(chosen):
            sheet, cell = chosen[month]
            amount = cell.get("amount")
            if amount is not None and a_key is not None:
                if amount == 0 and not has_target:
                    run.bump("zero_without_target")
                else:
                    _apply_entry(run, profile, a_key, month, amount, sheet, is_new)
            elif amount is not None and a_key is None:
                run.issue("no_amount_kpi", "warning", f'"{profile.full_name}": their job role has no monthly amount KPI, so amounts were not imported.', sheet)
            if cell.get("par") is not None and run.options.get("include_par"):
                if p_key is None:
                    run.bump("par_no_kpi")
                else:
                    _apply_entry(run, profile, p_key, month, cell["par"], sheet, is_new)
        if has_target:
            months_with_amount = sorted(m for m, (_, c) in chosen.items() if c.get("amount") is not None)
            _apply_target(run, profile, role_id, row_target, (months_with_amount or [f"{parsed['year']}-01"])[0], sheets_used[0], is_new)
        elif any((c.get("amount") or 0) > 0 for _, c in chosen.values()):
            run.issue("no_target", "warning", f'"{person.name}" has amounts but no target in the workbook, so they will show as "not scored" until a target is set.', sheets_used[0])
            run.bump("people_without_target")
        _apply_actions(run, profile, person, chosen, is_new)

    if run.restricted_skipped:
        run.issue("restricted_notes_skipped", "info", f"{run.restricted_skipped} restricted note(s) were not imported because only HR may see them. HR can run this import again to add them (nothing is duplicated).")
    if run.unclassified:
        total = sum(run.unclassified.values())
        listing = ": " + ", ".join(f'"{k}" x{v}' for k, v in sorted(run.unclassified.items(), key=lambda kv: -kv[1])[:8]) if run.is_hr else ""
        run.issue("unclassified_notes", "info", f"{total} note(s) aren't actions (for example contract notes) and were not imported{listing}")
    for r in run.rows:
        db.add(r)
    db.flush()
    return {"counts": run.counts, "issues": run.issues, "locked_roles": sorted(run.locked_roles), "touched_profiles": len(run.touched_profiles)}


# ---- dry run / commit / undo -------------------------------------------------------------------------------------


def dry_run(db: Session, imp: PerformanceImport, username: str, is_hr: bool) -> dict[str, Any]:
    imp_id = imp.id
    try:
        result = execute(db, imp, username, is_hr)
    finally:
        db.rollback()
    result["dry_run"] = True
    result["import_id"] = imp_id
    return result


def commit(db: Session, imp: PerformanceImport, username: str, is_hr: bool) -> dict[str, Any]:
    if imp.status != "previewed":
        raise ImportProblem("This import was already committed, undone or discarded. Upload the workbook again to run it once more.")
    imp_id = imp.id
    try:
        result = execute(db, imp, username, is_hr)
        imp.status, imp.committed_by_username, imp.committed_at = "committed", username, _now()
        imp.summary = {"counts": result["counts"], "issues": result["issues"][:100], "touched_profiles": result["touched_profiles"]}
        from app.services import performance_import_versions as ver

        ver.assign(db, imp)
        db.commit()
    except Exception:
        db.rollback()
        raise
    for role_id in result["locked_roles"]:
        version = latest_version(db, role_id)
        if version is not None:
            lock_version(db, version.id)
    log_audit(db, username, "import_committed", "performance_import", imp_id, {"entries": result["counts"].get("entries_created", 0) + result["counts"].get("entries_updated", 0)})
    result["dry_run"] = False
    result["import_id"] = imp_id
    return result


def undo(db: Session, imp: PerformanceImport, username: str) -> dict[str, Any]:
    if imp.status != "committed":
        raise ImportProblem("Only a committed import can be undone.")
    from app.services import performance_import_versions as ver

    ver.guard_unpost(db, imp, cascade=False)  # a newer posted version, or a locked month, blocks it
    reverted, kept = undo_rows(db, imp)
    imp.status, imp.undone_at = "undone", _now()
    db.commit()
    log_audit(db, username, "import_undone", "performance_import", imp.id, {"reverted": reverted, "kept": kept})
    return {"reverted": reverted, "kept": kept}


def undo_rows(db: Session, imp: PerformanceImport) -> tuple[int, int]:
    """Reverses what the import wrote, in the current transaction (no commit, no status change)."""
    rows = db.query(ImportRow).filter(ImportRow.import_id == imp.id).order_by(ImportRow.created_at.desc()).all()
    reverted, kept = 0, 0
    models = {"entry": KpiEntry, "target": PerformanceTarget, "action": PerformanceAction, "assignment": StaffAssignmentHistory}
    # Children first, then the people and units they hang off.
    order = {"entry": 0, "target": 1, "action": 2, "assignment": 3, "profile": 4, "unit": 5}
    for r in sorted(rows, key=lambda x: order.get(x.entity_type, 9)):
        if r.entity_type in models:
            obj = db.get(models[r.entity_type], r.entity_id)
            if obj is None:
                continue
            if r.entity_type == "entry":
                if r.action == "created":
                    if obj.source == IMPORT_SOURCE and Decimal(obj.value) == Decimal((r.after or {})["value"]):
                        db.delete(obj)
                        reverted += 1
                    else:
                        kept += 1
                else:
                    if Decimal(obj.value) == Decimal((r.after or {})["value"]):
                        b = r.before or {}
                        obj.value, obj.status, obj.source = Decimal(b["value"]), b["status"], b["source"]
                        obj.entered_by_username, obj.verified_by_username = b.get("entered_by_username"), b.get("verified_by_username")
                        reverted += 1
                    else:
                        kept += 1
            else:
                db.delete(obj)
                reverted += 1
        elif r.entity_type == "profile":
            db.flush()
            obj = db.get(StaffProfile, r.entity_id)
            if obj is None:
                continue
            if r.action == "created":
                has_other = (
                    db.query(KpiEntry).filter(KpiEntry.profile_id == obj.id).count()
                    or db.query(PerformanceTarget).filter(PerformanceTarget.profile_id == obj.id).count()
                    or db.query(PerformanceAction).filter(PerformanceAction.profile_id == obj.id).count()
                )
                if has_other or obj.admin_user_id or obj.manager_id:
                    kept += 1
                else:
                    db.query(StaffAssignmentHistory).filter(StaffAssignmentHistory.profile_id == obj.id).delete()
                    db.delete(obj)
                    reverted += 1
            else:
                obj.status = (r.before or {}).get("status", obj.status)
                reverted += 1
        elif r.entity_type == "unit":
            db.flush()
            obj = db.get(PerformanceUnit, r.entity_id)
            if obj is not None and db.query(StaffProfile).filter(StaffProfile.unit_id == obj.id).count() == 0:
                db.delete(obj)
                reverted += 1
            else:
                kept += 1
    return reverted, kept


# ---- reconciliation ----------------------------------------------------------------------------------------------


def _sheet_total(sheet: dict[str, Any], month: str) -> float:
    return sum((r["months"].get(month, {}).get("amount") or 0) for r in sheet["rows"])


def reconcile(parsed: dict[str, Any], sheet_units: dict[str, dict[str, Any]], db: Session) -> dict[str, Any]:
    """The workbook's own COMPANY totals vs what its branch sheets add up to, and its hand-counted bands vs recomputed ones."""
    company = parsed.get("company") or {}
    sheet_by_norm = {_unit_norm(s["name"]): s for s in parsed["sheets"] if s["kind"] == "staff" and not _SKIP_SHEET_NAME.match(s["name"].strip())}
    totals, unmatched = [], []
    for branch, months in company.items():
        s = sheet_by_norm.get(_unit_norm(branch))
        if s is None:
            if any(v for v in months.values()):
                unmatched.append(branch)
            continue
        for month, ref in sorted(months.items()):
            if month not in s["months"] and ref == 0:
                continue
            computed = _sheet_total(s, month)
            diff = round(computed - ref, 2)
            status = "ok" if diff == 0 else "small" if abs(diff) <= 1000 else "mismatch"
            if status != "ok":
                totals.append({"branch": branch, "sheet": s["name"], "month": month, "company_total": ref, "branch_sheet_total": computed, "difference": diff, "status": status})
    bands: list[dict[str, Any]] = []
    for month, hand in sorted((parsed.get("bands") or {}).items()):
        for branch, counts in hand.items():
            s = sheet_by_norm.get(_unit_norm(branch))
            if s is None:
                continue
            calc = {k: 0 for k in BAND_KEYS}
            for r in s["rows"]:
                a = r["months"].get(month, {}).get("amount")
                if r["is_team_row"] or r["target"] is None or a is None:
                    continue
                band = legacy_band(achievement_pct(Decimal(str(a)), Decimal(str(r["target"])), "higher"))
                if band is not None:
                    calc[{">100%": "gt100", "80%-99%": "b80_99", "60%-79%": "b60_79", "below 60%": "lt60"}.get(band, "lt60")] += 1
            if calc != counts:
                bands.append({"branch": branch, "month": month, "hand_counted": counts, "recomputed": calc})
    return {
        "company_total_differences": totals[:200], "company_total_difference_count": len(totals),
        "company_branches_without_sheet": unmatched, "band_differences": bands[:200], "band_difference_count": len(bands),
        "note": "Totals compare the workbook's own COMPANY sheet with its branch sheets. Band differences compare the hand-counted JAN-OCT sheets with counts recomputed from amount and target (a team's 'Debt Recovery' rows and anyone with no target are not counted).",
    }


# ---- a clean template ----------------------------------------------------------------------------------------------


def build_template(sheet_names: list[str], year: int | None = None) -> bytes:
    """A workbook in exactly the layout the importer reads: one sheet per branch/team, a month block per month."""
    import io as _io

    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    readme = wb.active
    readme.title = "Read me"
    for i, line in enumerate(
        [
            "Staff performance import template",
            "One sheet per branch or team. One row per person: name, monthly TARGET (KES), then a block per month.",
            "In each month block: the first cell is the AMOUNT disbursed (KES). 'PAR %' is the officer's 30-day PAR for that month (0-100).",
            "'Notes' is free text, e.g. talk, PIP; 1 month, Lift PIP, promote, confirm, extend probation, transfer, exit.",
            "Leave a target blank (not 0) if the person has none. Never type percentages for achievement: they are calculated for you.",
            "Don't add total rows, merge cells, or change the header row. Names must be the person's full name.",
        ],
        start=1,
    ):
        readme.cell(i, 1, line).font = Font(bold=(i == 1))
    readme.cell(8, 1, "Year").font = Font(bold=True)
    readme.cell(8, 2, year or today_nairobi().year)
    readme.column_dimensions["A"].width = 120
    months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    for name in (sheet_names or ["Branch name"]):
        title = re.sub(r"[\\/*?:\[\]]", " ", name)[:31] or "Branch"
        ws = wb.create_sheet(title)
        header = ["Loan Officer", "TARGET"]
        for m in months:
            header += [m, "PAR %", "Notes"]
        for c, h in enumerate(header, start=1):
            ws.cell(1, c, h).font = Font(bold=True)
        ws.column_dimensions["A"].width = 32
        ws.freeze_panes = "B2"
    buf = _io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
