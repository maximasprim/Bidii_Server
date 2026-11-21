"""
Version control for workbook imports.

Every posted import is a numbered version of its year's workbook (v1, v2, ...). You can

  * unpost a version (reverse everything it wrote). A version can only be unposted while no NEWER version is still posted, unless you
    choose to unpost the newer ones with it (newest first), so the data is always the result of a clean stack of versions;
  * restore any version: newer posted versions are unposted and, if that version itself was unposted, it is posted again as a NEW
    version ("Restore of v2"). The whole step is one transaction: it completes or nothing changes;
  * compare two versions of the workbook (which amounts changed, appeared or vanished).

Reversing never touches a figure someone changed after the import (those are counted as "kept"), and a month that has been locked
blocks both unposting and restoring until HR reopens it, so closed periods can't shift silently. History is never rewritten: unposted
versions stay in the list with who unposted them and why.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.performance import ImportRow, ImportVersion, KpiEntry, PerformanceImport
from app.services import performance_governance as gov
from app.services import performance_import as imp_svc
from app.services.performance_config import log_audit
from app.services.performance_roles import latest_version, lock_version


def series_of(imp: PerformanceImport) -> str:
    y = (imp.parsed or {}).get("year")
    return str(y) if y else "unknown"


def assign(db: Session, imp: PerformanceImport, *, label: str | None = None, restored_from: int | None = None) -> ImportVersion:
    """Gives a posted import its version number (no commit)."""
    existing = db.query(ImportVersion).filter_by(import_id=imp.id).first()
    if existing is not None:
        return existing
    series = series_of(imp)
    top = db.query(func.max(ImportVersion.version_no)).filter(ImportVersion.series == series).scalar() or 0
    row = ImportVersion(import_id=imp.id, series=series, version_no=top + 1, label=label, restored_from=restored_from)
    db.add(row)
    db.flush()
    return row


def ensure_versions(db: Session) -> None:
    """Imports posted before versioning existed get numbers, oldest first. Idempotent."""
    have = {i for (i,) in db.query(ImportVersion.import_id)}
    missing = [i for i in db.query(PerformanceImport).filter(PerformanceImport.status.in_(("committed", "undone")), PerformanceImport.committed_at.isnot(None)).order_by(PerformanceImport.committed_at) if i.id not in have]
    for imp in missing:
        assign(db, imp)
    if missing:
        db.commit()


def _meta(db: Session, imp: PerformanceImport) -> ImportVersion:
    ensure_versions(db)
    row = db.query(ImportVersion).filter_by(import_id=imp.id).first()
    if row is None:
        raise imp_svc.ImportProblem("That import was never posted, so it has no version.")
    return row


def _newer_posted(db: Session, meta: ImportVersion) -> list[tuple[ImportVersion, PerformanceImport]]:
    rows = (
        db.query(ImportVersion, PerformanceImport)
        .join(PerformanceImport, PerformanceImport.id == ImportVersion.import_id)
        .filter(ImportVersion.series == meta.series, ImportVersion.version_no > meta.version_no, PerformanceImport.status == "committed")
        .order_by(ImportVersion.version_no.desc())
        .all()
    )
    return [(v, i) for v, i in rows]


def _locked_months(db: Session, imps: list[PerformanceImport]) -> list[str]:
    ids = [i.id for i in imps]
    if not ids:
        return []
    entry_ids = [e for (e,) in db.query(ImportRow.entity_id).filter(ImportRow.import_id.in_(ids), ImportRow.entity_type == "entry")]
    months = {m for (m,) in db.query(KpiEntry.month).filter(KpiEntry.id.in_(entry_ids)).distinct()} if entry_ids else set()
    return sorted(m.strftime("%Y-%m") for m in months if gov.is_locked(db, m))


def guard_unpost(db: Session, imp: PerformanceImport, *, cascade: bool) -> list[tuple[ImportVersion, PerformanceImport]]:
    """Raises if the version can't be unposted now; returns the newer versions that go with it when cascade is on."""
    meta = _meta(db, imp)
    newer = _newer_posted(db, meta)
    if newer and not cascade:
        names = ", ".join(f"v{v.version_no}" for v, _ in newer)
        raise imp_svc.ImportProblem(f"{names} {'was' if len(newer) == 1 else 'were'} posted after this one. Unpost the newer version(s) first, or choose to unpost them together.")
    locked = _locked_months(db, [imp, *[i for _, i in newer]])
    if locked:
        raise imp_svc.ImportProblem("These months are locked: " + ", ".join(locked) + ". HR must reopen them before the import can be reversed.")
    return newer


def _mark_unposted(db: Session, imp: PerformanceImport, username: str, reason: str | None) -> None:
    imp.status, imp.undone_at = "undone", imp_svc._now()  # noqa: SLF001
    meta = db.query(ImportVersion).filter_by(import_id=imp.id).first()
    if meta is not None:
        meta.unposted_by, meta.unposted_reason = username, (reason or None)


def unpost(db: Session, imp: PerformanceImport, username: str, reason: str, *, cascade: bool) -> dict[str, Any]:
    if imp.status != "committed":
        raise imp_svc.ImportProblem("Only a posted version can be unposted.")
    newer = guard_unpost(db, imp, cascade=cascade)
    done: list[dict[str, Any]] = []
    try:
        for meta, i in [*newer, (_meta(db, imp), imp)]:
            reverted, kept = imp_svc.undo_rows(db, i)
            _mark_unposted(db, i, username, reason)
            done.append({"version": meta.version_no, "reverted": reverted, "kept": kept})
        db.commit()
    except Exception:
        db.rollback()
        raise
    log_audit(db, username, "import_unposted", "performance_import", imp.id, {"versions": [d["version"] for d in done], "reason": reason[:200]})
    return {"unposted": done}


def restore_plan(db: Session, target: PerformanceImport) -> dict[str, Any]:
    meta = _meta(db, target)
    newer = _newer_posted(db, meta)
    locked = _locked_months(db, [i for _, i in newer])
    return {
        "target_version": meta.version_no, "series": meta.series, "target_posted": target.status == "committed",
        "unpost": [{"version": v.version_no, "label": v.label, "id": i.id} for v, i in newer],
        "repost_as_new_version": target.status != "committed", "blocked_by_locked_months": locked,
        "note": "Figures someone changed after an import are kept, not overwritten.",
    }


def restore(db: Session, target: PerformanceImport, username: str, is_hr: bool, reason: str) -> dict[str, Any]:
    meta = _meta(db, target)
    plan = restore_plan(db, target)
    if plan["blocked_by_locked_months"]:
        raise imp_svc.ImportProblem("These months are locked: " + ", ".join(plan["blocked_by_locked_months"]) + ". HR must reopen them before restoring.")
    if not plan["unpost"] and plan["target_posted"]:
        raise imp_svc.ImportProblem(f"v{meta.version_no} is already the current version.")
    done: list[dict[str, Any]] = []
    new_meta = None
    result: dict[str, Any] = {}
    try:
        for v, i in _newer_posted(db, meta):
            reverted, kept = imp_svc.undo_rows(db, i)
            _mark_unposted(db, i, username, reason)
            done.append({"version": v.version_no, "reverted": reverted, "kept": kept})
        if not plan["target_posted"]:
            clone = PerformanceImport(
                kind=target.kind, original_name=target.original_name, file_sha256=target.file_sha256, status="previewed",
                parsed=target.parsed, options=dict(target.options or {}), decisions=dict(target.decisions or {}), created_by_username=username,
            )
            db.add(clone)
            db.flush()
            result = imp_svc.execute(db, clone, username, is_hr)
            clone.status, clone.committed_by_username, clone.committed_at = "committed", username, imp_svc._now()  # noqa: SLF001
            clone.summary = {"counts": result["counts"], "issues": result["issues"][:100], "touched_profiles": result["touched_profiles"]}
            new_meta = assign(db, clone, label=f"Restore of v{meta.version_no}", restored_from=meta.version_no)
        db.commit()
    except Exception:
        db.rollback()
        raise
    for role_id in result.get("locked_roles", []):
        v = latest_version(db, role_id)
        if v is not None:
            lock_version(db, v.id)
    log_audit(db, username, "import_restored", "performance_import", target.id, {"to_version": meta.version_no, "unposted": [d["version"] for d in done], "reason": reason[:200]})
    return {"restored_to": meta.version_no, "unposted": done, "new_version": new_meta.version_no if new_meta else None, "counts": result.get("counts")}


def listing(db: Session, series: str | None = None) -> dict[str, Any]:
    ensure_versions(db)
    q = db.query(ImportVersion, PerformanceImport).join(PerformanceImport, PerformanceImport.id == ImportVersion.import_id)
    if series:
        q = q.filter(ImportVersion.series == series)
    rows = q.order_by(ImportVersion.series.desc(), ImportVersion.version_no.desc()).all()
    current: dict[str, int] = {}
    for v, i in rows:
        if i.status == "committed" and v.series not in current:
            current[v.series] = v.version_no
    sha_posted: dict[tuple[str, str], int] = {}
    for v, i in sorted(rows, key=lambda r: r[0].version_no):
        if i.status == "committed":
            sha_posted.setdefault((v.series, i.file_sha256), v.version_no)
    items = []
    for v, i in rows:
        dup = sha_posted.get((v.series, i.file_sha256))
        items.append({
            "import_id": i.id, "series": v.series, "version": v.version_no, "label": v.label, "restored_from": v.restored_from,
            "status": "posted" if i.status == "committed" else "unposted", "is_current": current.get(v.series) == v.version_no,
            "file_name": i.original_name, "posted_by": i.committed_by_username, "posted_at": i.committed_at,
            "unposted_by": v.unposted_by, "unposted_at": i.undone_at, "unposted_reason": v.unposted_reason,
            "counts": (i.summary or {}).get("counts"), "same_file_as_version": dup if dup is not None and dup != v.version_no else None,
        })
    return {"items": items}


def set_label(db: Session, imp: PerformanceImport, label: str | None, username: str) -> None:
    meta = _meta(db, imp)
    meta.label = (label or "").strip() or None
    db.commit()
    log_audit(db, username, "import_labelled", "performance_import", imp.id, {"version": meta.version_no})


def _cells(parsed: dict[str, Any]) -> dict[tuple[str, str, str], Any]:
    out: dict[tuple[str, str, str], Any] = {}
    for sheet in (parsed or {}).get("sheets", []):
        if sheet.get("kind") != "staff":
            continue
        for row in sheet.get("rows", []):
            for month, cell in (row.get("months") or {}).items():
                out[(sheet["name"], str(row.get("name")), month)] = (cell or {}).get("amount")
    return out


def compare(db: Session, a: PerformanceImport, b: PerformanceImport) -> dict[str, Any]:
    ma, mb = _meta(db, a), _meta(db, b)
    ca, cb = _cells(a.parsed), _cells(b.parsed)
    added = [k for k in cb if k not in ca]
    removed = [k for k in ca if k not in cb]
    changed = [k for k in cb if k in ca and ca[k] != cb[k]]

    def show(keys: list[tuple[str, str, str]], with_old: bool) -> list[dict[str, Any]]:
        return [{"sheet": k[0], "name": k[1], "month": k[2], "from": ca.get(k) if with_old else None, "to": cb.get(k)} for k in sorted(keys)[:100]]

    return {
        "from_version": ma.version_no, "to_version": mb.version_no, "same_file": a.file_sha256 == b.file_sha256,
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)},
        "changed": show(changed, True), "added": show(added, False),
        "removed": [{"sheet": k[0], "name": k[1], "month": k[2], "from": ca.get(k), "to": None} for k in sorted(removed)[:100]],
    }
