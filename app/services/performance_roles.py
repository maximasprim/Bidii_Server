"""
Job roles: JD + KPI validation, versioning, seeding.

Rules
* Responsibility weights ("% of time") must total 100 when any are given; so must KPI weights.
* A KPI may name the responsibility it measures (jd_section); if so it must match a title exactly.
* The sign-off chain starts staff -> manager; "ceo" is never listed, it is appended when
  requires_ceo_signoff is true. Steps are staff, manager, csm, rm_gm, hr.
* A role's current version is its highest number. A version that has been used in a scored
  period is locked (locked_at); saving a role whose current version is locked creates the next
  version, so history never changes. Until then, saves update the current version in place.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.performance import PerformanceKpi, PerformanceRole, PerformanceRoleVersion
from app.services.performance_role_templates import DEFAULT_COMPETENCIES, TEMPLATES

KPI_UNITS = ["kes", "count", "percent", "rating", "days", "score"]
KPI_DIRECTIONS = ["higher", "lower"]
KPI_SOURCES = ["kpi_entry", "manual", "appraisal_rating", "computed", "imported"]
KPI_FREQUENCIES = ["monthly", "quarterly", "annual"]
SIGNOFF_STEPS = ["staff", "manager", "csm", "rm_gm", "hr"]
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,59}$")
_TOLERANCE = Decimal("0.01")


class JDValidationError(ValueError):
    """Carries every problem found, so the admin can fix them in one go."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:6]) + (f" (+{len(errors) - 6} more)" if len(errors) > 6 else ""))
        self.errors = errors


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:60]


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    return d if d.is_finite() else None


def _text(value: Any, label: str, max_len: int, errors: list[str], *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors.append(f"{label} must be text.")
        return ""
    value = value.strip()
    if required and not value:
        errors.append(f"{label} is required.")
    if len(value) > max_len:
        errors.append(f"{label} is too long (max {max_len} characters).")
        return value[:max_len]
    return value


def _text_list(value: Any, label: str, max_items: int, max_len: int, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        errors.append(f"{label} must be a list of at most {max_items} items.")
        return []
    return [t for t in (_text(v, label, max_len, errors) for v in value) if t]


def _weight(value: Any, label: str, errors: list[str]) -> Decimal:
    d = _number(value)
    if d is None or not Decimal(0) <= d <= Decimal(100):
        errors.append(f"{label} must be a number from 0 to 100.")
        return Decimal(0)
    return d


def _check_total(total: Decimal, label: str, errors: list[str]) -> None:
    if abs(total - Decimal(100)) > _TOLERANCE:
        errors.append(f"{label} add up to {total.normalize():f}; they must total 100.")


def validate_jd(jd: Any, errors: list[str]) -> dict[str, Any]:
    if not isinstance(jd, dict):
        errors.append("The job description must be an object.")
        return {}
    out: dict[str, Any] = {
        "title": _text(jd.get("title"), "Position title", 150, errors, required=True),
        "reports_to": _text(jd.get("reports_to"), "Reports to", 150, errors),
        "branch": _text(jd.get("branch"), "Branch", 150, errors),
        "department": _text(jd.get("department"), "Department", 150, errors),
        "grade": _text(jd.get("grade"), "Grade", 60, errors),
        "date": _text(jd.get("date"), "Date", 60, errors),
        "purpose": _text(jd.get("purpose"), "Role purpose", 3000, errors),
        "mandates": _text(jd.get("mandates"), "Decision-making mandates", 3000, errors),
        "planning": _text(jd.get("planning"), "Planning responsibility", 3000, errors),
        "relationships": _text(jd.get("relationships"), "Relationship management", 3000, errors),
        "qualifications": _text_list(jd.get("qualifications"), "Qualifications", 40, 600, errors),
        "approvals": _text_list(jd.get("approvals"), "Approvals", 6, 150, errors),
        "objective_rows": _text_list(jd.get("objective_rows"), "Next-quarter objective rows", 8, 100, errors),
    }

    raw_resps = jd.get("responsibilities") or []
    resps: list[dict[str, Any]] = []
    if not isinstance(raw_resps, list) or len(raw_resps) > 15:
        errors.append("Key responsibilities must be a list of at most 15.")
        raw_resps = []
    total = Decimal(0)
    seen_titles: set[str] = set()
    for i, r in enumerate(raw_resps, 1):
        if not isinstance(r, dict):
            errors.append(f"Responsibility {i} is invalid.")
            continue
        title = _text(r.get("title"), f"Responsibility {i} title", 200, errors, required=True)
        if title.lower() in seen_titles:
            errors.append(f'Two responsibilities are both called "{title}".')
        seen_titles.add(title.lower())
        weight = _weight(r.get("weight_pct"), f"Responsibility {i} weight", errors)
        total += weight
        resps.append({
            "title": title,
            "weight_pct": float(weight),
            "description": _text(r.get("description"), f"Responsibility {i} description", 2000, errors),
            "criteria": _text_list(r.get("criteria"), f"Responsibility {i} criteria", 10, 500, errors),
        })
    if resps:
        _check_total(total, "Responsibility weights", errors)
    out["responsibilities"] = resps

    comps: list[dict[str, str]] = []
    raw_comps = jd.get("competencies") or []
    if not isinstance(raw_comps, list) or len(raw_comps) > 12:
        errors.append("Competencies must be a list of at most 12.")
        raw_comps = []
    keys: set[str] = set()
    for i, c in enumerate(raw_comps, 1):
        if not isinstance(c, dict):
            errors.append(f"Competency {i} is invalid.")
            continue
        title = _text(c.get("title"), f"Competency {i} title", 150, errors, required=True)
        key = _text(c.get("key"), f"Competency {i} key", 60, errors) or slugify(title)
        if not KEY_RE.match(key) or key in keys:
            errors.append(f'Competency key "{key}" is invalid or repeated.')
        keys.add(key)
        comps.append({"key": key, "title": title, "description": _text(c.get("description"), f"Competency {i} description", 1000, errors)})
    out["competencies"] = comps
    return out


def validate_kpis(kpis: Any, responsibility_titles: list[str], errors: list[str]) -> list[dict[str, Any]]:
    if kpis is None:
        kpis = []
    if not isinstance(kpis, list) or len(kpis) > 25:
        errors.append("KPIs must be a list of at most 25.")
        return []
    out: list[dict[str, Any]] = []
    total = Decimal(0)
    keys: set[str] = set()
    for i, k in enumerate(kpis, 1):
        if not isinstance(k, dict):
            errors.append(f"KPI {i} is invalid.")
            continue
        label = _text(k.get("label"), f"KPI {i} name", 200, errors, required=True)
        key = _text(k.get("key"), f"KPI {i} key", 60, errors) or slugify(label)
        if not KEY_RE.match(key):
            errors.append(f'KPI key "{key}" must be lowercase letters, numbers and underscores, starting with a letter.')
        if key in keys:
            errors.append(f'KPI key "{key}" is used twice.')
        keys.add(key)
        for field, allowed in (("unit", KPI_UNITS), ("direction", KPI_DIRECTIONS), ("source", KPI_SOURCES), ("frequency", KPI_FREQUENCIES)):
            if k.get(field) not in allowed:
                errors.append(f'KPI "{label}": {field} must be one of {", ".join(allowed)}.')
        weight = _weight(k.get("weight"), f'KPI "{label}" weight', errors)
        total += weight
        cap = k.get("cap_pct")
        cap_d = None
        if cap is not None:
            cap_d = _number(cap)
            if cap_d is None or not Decimal(100) <= cap_d <= Decimal(300):
                errors.append(f'KPI "{label}": the cap must be between 100 and 300, or left empty to use the organisation cap.')
                cap_d = None
        section = _text(k.get("jd_section"), f'KPI "{label}" responsibility link', 200, errors) or None
        if section is not None and section not in responsibility_titles:
            errors.append(f'KPI "{label}" is linked to "{section}", which is not one of the key responsibilities.')
        out.append({
            "key": key, "label": label, "description": _text(k.get("description"), f'KPI "{label}" description', 1000, errors) or None,
            "unit": k.get("unit"), "direction": k.get("direction"), "weight": weight, "source": k.get("source"),
            "frequency": k.get("frequency"), "cap_pct": cap_d, "jd_section": section, "sort_order": i,
        })
    if out:
        _check_total(total, "KPI weights", errors)
    return out


def validate_chain(chain: Any, errors: list[str]) -> list[str]:
    if not isinstance(chain, list) or not all(isinstance(s, str) for s in chain):
        errors.append("The sign-off chain must be a list of steps.")
        return []
    if len(set(chain)) != len(chain):
        errors.append("A sign-off step can't appear twice.")
    bad = [s for s in chain if s not in SIGNOFF_STEPS]
    if bad:
        errors.append(f"Unknown sign-off step(s): {', '.join(bad)}. Allowed: {', '.join(SIGNOFF_STEPS)} (the CEO step is added with the CEO switch).")
    if chain[:2] != ["staff", "manager"]:
        errors.append("The sign-off chain must start with the staff member, then their manager.")
    return list(chain)


def validate_version_payload(jd: Any, kpis: Any, signoff_chain: Any, requires_ceo_signoff: Any) -> dict[str, Any]:
    errors: list[str] = []
    cleaned_jd = validate_jd(jd, errors)
    titles = [r["title"] for r in cleaned_jd.get("responsibilities", [])]
    cleaned_kpis = validate_kpis(kpis, titles, errors)
    chain = validate_chain(signoff_chain, errors)
    if not isinstance(requires_ceo_signoff, bool):
        errors.append("The CEO sign-off setting must be true or false.")
    if errors:
        raise JDValidationError(errors)
    return {"jd": cleaned_jd, "kpis": cleaned_kpis, "signoff_chain": chain, "requires_ceo_signoff": requires_ceo_signoff}


def effective_signoff_chain(version: PerformanceRoleVersion) -> list[str]:
    chain = list(version.signoff_chain or [])
    return [*chain, "ceo"] if version.requires_ceo_signoff else chain


def weights_summary(version: PerformanceRoleVersion, kpis: list[PerformanceKpi]) -> dict[str, float]:
    resp = sum((Decimal(str(r.get("weight_pct", 0))) for r in (version.jd or {}).get("responsibilities", [])), Decimal(0))
    kpi = sum((Decimal(k.weight) for k in kpis), Decimal(0))
    return {"responsibilities_total": float(resp), "kpis_total": float(kpi)}


def is_configured(version: PerformanceRoleVersion | None, kpis: list[PerformanceKpi]) -> bool:
    """A role can be used for scoring only once it has KPIs whose weights total 100."""
    if version is None or not kpis:
        return False
    return abs(sum((Decimal(k.weight) for k in kpis), Decimal(0)) - Decimal(100)) <= _TOLERANCE


# ---- database ---------------------------------------------------------------


def latest_version(db: Session, role_id: str) -> PerformanceRoleVersion | None:
    return (
        db.query(PerformanceRoleVersion)
        .filter(PerformanceRoleVersion.role_id == role_id)
        .order_by(PerformanceRoleVersion.version.desc())
        .first()
    )


def get_version(db: Session, role_id: str, version: int) -> PerformanceRoleVersion | None:
    return db.query(PerformanceRoleVersion).filter_by(role_id=role_id, version=version).first()


def kpis_for(db: Session, version_id: str) -> list[PerformanceKpi]:
    return db.query(PerformanceKpi).filter(PerformanceKpi.role_version_id == version_id).order_by(PerformanceKpi.sort_order.asc()).all()


def _replace_kpis(db: Session, version: PerformanceRoleVersion, kpis: list[dict[str, Any]]) -> None:
    db.query(PerformanceKpi).filter(PerformanceKpi.role_version_id == version.id).delete(synchronize_session=False)
    db.flush()
    for k in kpis:
        db.add(PerformanceKpi(role_version_id=version.id, **k))


def save_version(
    db: Session, role: PerformanceRole, cleaned: dict[str, Any], username: str, note: str | None = None
) -> tuple[PerformanceRoleVersion, bool]:
    """
    Saves a validated payload. Updates the current version in place while it is unused;
    once it is locked, creates the next version instead. Returns (version, created_new).
    """
    current = latest_version(db, role.id)
    if current is not None and current.locked_at is None:
        version, created_new = current, False
        version.jd, version.signoff_chain = cleaned["jd"], cleaned["signoff_chain"]
        version.requires_ceo_signoff = cleaned["requires_ceo_signoff"]
        if note is not None:
            version.note = note
    else:
        version, created_new = (
            PerformanceRoleVersion(
                role_id=role.id, version=(current.version + 1) if current else 1, jd=cleaned["jd"],
                signoff_chain=cleaned["signoff_chain"], requires_ceo_signoff=cleaned["requires_ceo_signoff"],
                note=note, created_by=username,
            ),
            True,
        )
        db.add(version)
        db.flush()
    _replace_kpis(db, version, cleaned["kpis"])
    db.commit()
    db.refresh(version)
    return version, created_new


def targetable_kpis(db: Session, role_id: str) -> dict[str, PerformanceKpi]:
    """
    KPIs of the role's CURRENT version that can carry a numeric target, by key. Rated KPIs (source
    appraisal_rating: a manager gives them a 1-5) have no target, so they are excluded.
    """
    version = latest_version(db, role_id)
    if version is None:
        return {}
    return {k.key: k for k in kpis_for(db, version.id) if k.source != "appraisal_rating"}


def lock_version(db: Session, version_id: str) -> None:
    """Called when a version is first used in a scored period (sales record, appraisal, ...). Idempotent."""
    version = db.get(PerformanceRoleVersion, version_id)
    if version is not None and version.locked_at is None:
        version.locked_at = datetime.now(timezone.utc)
        db.commit()


def seed_role_templates(db: Session) -> int:
    """Gives each built-in role its starting template as version 1 - only if it has no version yet. Idempotent."""
    created = 0
    for role in db.query(PerformanceRole).filter(PerformanceRole.is_system.is_(True)).all():
        template = TEMPLATES.get(role.key)
        if template is None or latest_version(db, role.id) is not None:
            continue
        errors: list[str] = []
        jd = validate_jd(template["jd"], errors)
        kpis = validate_kpis(template["kpis"], [r["title"] for r in jd["responsibilities"]], errors)
        chain = validate_chain(template["chain"], errors)
        if errors:  # a broken template is a programming error; never seed half-valid data
            raise JDValidationError(errors)
        save_version(db, role, {"jd": jd, "kpis": kpis, "signoff_chain": chain, "requires_ceo_signoff": template.get("requires_ceo", False)},
                     "system", "Starting template - review before use")
        created += 1
    return created


__all__ = ["DEFAULT_COMPETENCIES", "JDValidationError", "validate_version_payload", "save_version", "lock_version", "seed_role_templates"]
