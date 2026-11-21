"""
Phase 6e - the monthly one-page board pack. Company-level and branch-level numbers only: no individual names, scores, PIPs or
appraisal content ever appear in it. All figures come from verified results through the same scoring as the reports.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone
from typing import Any
from xml.sax.saxutils import escape

from sqlalchemy.orm import Session

from app.models.performance import Appraisal, BoardPack, KpiEntry, PerformanceUnit, Region, StaffProfile
from app.services import performance_cascade as pc
from app.services import performance_insights as ins
from app.services import performance_reports as pr
from app.services import performance_storage as storage
from app.services.performance_config import get_org_config
from app.services.performance_periods import parse_period

LIVE = ("active", "probation", "pip")


def last_complete_month(today: date) -> date:
    return (today.replace(day=1) - timedelta(days=1)).replace(day=1)


def _p(g: dict[str, Any]) -> dict[str, Any]:
    return {"achievement_pct": g["achievement_pct"], "actual": g["actual_total"], "target": g["target_total"], "average_score": g["average_score"], "scored_people": g["scored_people"], "people": g["people"]}


def data(db: Session, month: date, today: date) -> dict[str, Any]:
    month = month.replace(day=1)
    cfg = get_org_config(db)[0]
    fy = int(cfg.get("fiscal_year_start_month", 1))
    ids = [i for (i,) in db.query(StaffProfile.id).filter(StaffProfile.status.in_(LIVE), StaffProfile.is_active.is_(True), StaffProfile.is_executive.is_(False))]
    m_period = parse_period(month.strftime("%Y-%m"))
    prev_period = parse_period(((month - timedelta(days=1)).replace(day=1)).strftime("%Y-%m"))
    ytd = pr.resolve_period("ytd", month_end_of(month), fy)
    start = min(ytd.start, prev_period.start)
    ctx = pr.load_ctx(db, ids, start, m_period.end, today=today)
    pm = pr.score_many(ctx, ids, m_period)
    gm = pr.group_summary(ctx, pm)
    gp = pr.group_summary(ctx, pr.score_many(ctx, ids, prev_period))
    py = pr.score_many(ctx, ids, ytd)
    gy = pr.group_summary(ctx, py)
    cats = ctx.scale.categories
    total_scored = sum(gm["category_counts"].values()) or 0
    categories = [{"key": c["key"], "label": c["label"], "count": gm["category_counts"].get(c["key"], 0), "share_pct": round(100 * gm["category_counts"].get(c["key"], 0) / total_scored, 1) if total_scored else None} for c in cats]
    # year-end projection for the company (deterministic, range-free headline)
    ctx_ytd = pr.load_ctx(db, ids, ytd.start, m_period.end, today=month_end_of(month) + timedelta(days=1))
    projs = [ins.project_person(ctx_ytd, p, fy) for p in pr.score_many(ctx_ytd, ids, ytd)]
    year_end = ins.project_group(projs, "year_end")
    # regions and branches
    by_region, _ur = pc._structure(db, month)  # noqa: SLF001
    by_unit: dict[str, list[dict[str, Any]]] = {}
    for p in pm:
        if p["unit_id"]:
            by_unit.setdefault(p["unit_id"], []).append(p)
    unit_names = {i: n for i, n in db.query(PerformanceUnit.id, PerformanceUnit.name)}
    region_names = {i: n for i, n in db.query(Region.id, Region.name)}
    regions, branches = [], []
    for rid, uids in by_region.items():
        members = [p for u in uids for p in by_unit.get(u, [])]
        if not members:
            continue
        g = pr.group_summary(ctx, members)
        regions.append({"name": region_names.get(rid, "Not in a region"), "achievement_pct": g["achievement_pct"], "people": len(members)})
        for u in uids:
            ug = by_unit.get(u, [])
            if ug:
                x = pr.group_summary(ctx, ug)
                branches.append({"name": unit_names.get(u), "achievement_pct": x["achievement_pct"], "people": len(ug), "small_unit": len(ug) < pr.SMALL_UNIT})
    ranked = sorted([b for b in branches if b["achievement_pct"] is not None], key=lambda b: -b["achievement_pct"])
    # completeness: people with at least one entry for the month
    reported = {pid for (pid,) in db.query(KpiEntry.profile_id).filter(KpiEntry.month == month, KpiEntry.status.in_(("verified", "locked")))}
    expected = [i for i in ids if ctx.profiles[i].hire_date is None or ctx.profiles[i].hire_date <= month_end_of(month)]
    comp = len([i for i in expected if i in reported])
    # appraisals (counts only)
    q = f"{month.year}-Q{(month.month - 1) // 3 + 1}"
    ap = {s: n for s, n in [(s, db.query(Appraisal).filter(Appraisal.period_key == q, Appraisal.status == s).count()) for s in ("draft", "in_review", "finalised")]}
    # cascade headline (only if a company target is set for the first additive KPI)
    cascade = None
    try:
        for k in pc.kpi_options(db):
            t = pc._value(db, "company", pc.COMPANY, k["key"], month)  # noqa: SLF001
            if t is not None:
                regs = pc._values(db, "region", k["key"], month)  # noqa: SLF001
                cascade = {"kpi": k["label"], "company_target": float(t), "allocated_to_regions": float(sum(regs.values(), 0)), "gap": float(t - sum(regs.values(), 0))}
                break
    except Exception:  # the pack must still build without it
        cascade = None
    return {
        "month": month.isoformat(), "label": f"{month:%B %Y}", "generated_on": today.isoformat(),
        "headline": "Sales (disbursed)" if gm["target_total"] else "Achievement",
        "month_result": _p(gm), "previous_month_pct": gp["achievement_pct"], "ytd": _p(gy), "year_end": {"projected_pct": year_end.get("projected_pct"), "available": year_end.get("available", False)},
        "categories": categories, "regions": sorted(regions, key=lambda r: -(r["achievement_pct"] or 0)), "top_branches": ranked[:3], "bottom_branches": ranked[-3:][::-1] if len(ranked) > 3 else [],
        "completeness": {"expected": len(expected), "reported": comp, "pct": round(100 * comp / len(expected), 1) if expected else None},
        "appraisals": {"quarter": q, **ap}, "cascade": cascade, "headcount": len(ids),
        "notes": ["Figures are verified results only.", "Branches with fewer than %d people are marked small; read their ranking with care." % pr.SMALL_UNIT],
    }


def month_end_of(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


# ---- PDF -------------------------------------------------------------------------------------------------------------------


def _n(v: Any, d: int = 0) -> str:
    return "–" if v is None else f"{v:,.{d}f}"


def render_pdf(d: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ink, soft, rule = colors.HexColor("#14213d"), colors.HexColor("#5b6779"), colors.HexColor("#d7dde6")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=17, leading=20, alignment=0, textColor=ink, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontSize=8.5, textColor=soft, leading=11)
    h2 = ParagraphStyle("h2", parent=ss["Heading3"], fontSize=10, leading=12, textColor=ink, spaceBefore=8, spaceAfter=3)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=7.5, leading=9.5, textColor=soft)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=34, rightMargin=34, topMargin=30, bottomMargin=28, title=f"Bidii Credit staff performance, {d['label']}")

    def tbl(rows, widths, header=True):
        t = Table(rows, colWidths=widths)
        st = [("FONTSIZE", (0, 0), (-1, -1), 8.5), ("TEXTCOLOR", (0, 0), (-1, -1), ink), ("LINEBELOW", (0, 0), (-1, -1), 0.25, rule), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if header:
            st += [("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("TEXTCOLOR", (0, 0), (-1, 0), soft)]
        t.setStyle(TableStyle(st))
        return t

    m, y = d["month_result"], d["ytd"]
    delta = None if d["previous_month_pct"] is None or m["achievement_pct"] is None else round(m["achievement_pct"] - d["previous_month_pct"], 1)
    tiles = [[Paragraph(f"<font size=8 color='#5b6779'>{escape(t)}</font><br/><font size=17><b>{escape(v)}</b></font><br/><font size=7.5 color='#5b6779'>{escape(s)}</font>", ss["Normal"]) for t, v, s in (
        (f"{d['label']} achievement", f"{_n(m['achievement_pct'], 1)}%", ("no change" if delta == 0 else f"{delta:+.1f} pts vs last month") if delta is not None else "no previous month"),
        ("Year to date", f"{_n(y['achievement_pct'], 1)}%", f"{_n(y['actual'])} of {_n(y['target'])}"),
        ("Year-end projection", f"{_n(d['year_end']['projected_pct'], 1)}%" if d["year_end"]["available"] else "–", "of annual target (indicative)" if d["year_end"]["available"] else "needs 3+ scored months"),
        ("Results reported", f"{_n(d['completeness']['pct'], 0)}%" if d["completeness"]["pct"] is not None else "–", f"{d['completeness']['reported']} of {d['completeness']['expected']} staff"),
    )]]
    tt = Table(tiles, colWidths=[127] * 4)
    tt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, rule), ("INNERGRID", (0, 0), (-1, -1), 0.5, rule), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story: list[Any] = [Paragraph("Staff performance: monthly board pack", h1), Paragraph(escape(f"{d['label']} · {d['headline']} · {m['scored_people']} people scored of {d['headcount']} · generated {d['generated_on']}"), sub), Spacer(1, 8), tt]
    story += [Paragraph("Where people landed this month", h2), tbl([["Category", "People", "Share"]] + [[c["label"], str(c["count"]), "–" if c["share_pct"] is None else f"{c['share_pct']}%"] for c in d["categories"]], [230, 80, 80])]
    if d["regions"]:
        story += [Paragraph("Regions", h2), tbl([["Region", "Achievement", "People"]] + [[r["name"], f"{_n(r['achievement_pct'], 1)}%", str(r["people"])] for r in d["regions"]], [230, 100, 80])]

    def branch_rows(rows):
        return [[b["name"] or "", f"{_n(b['achievement_pct'], 1)}%", f"{b['people']}" + (" (small)" if b["small_unit"] else "")] for b in rows]

    if d["top_branches"]:
        story += [Paragraph("Branches: strongest", h2), tbl([["Branch", "Achievement", "People"]] + branch_rows(d["top_branches"]), [230, 100, 80])]
    if d["bottom_branches"]:
        story += [Paragraph("Branches: needing support", h2), tbl([["Branch", "Achievement", "People"]] + branch_rows(d["bottom_branches"]), [230, 100, 80])]
    a = d["appraisals"]
    story += [Paragraph("Appraisals and targets", h2), Paragraph(escape(f"Appraisals for {a['quarter']}: {a.get('finalised', 0)} finalised, {a.get('in_review', 0)} in review, {a.get('draft', 0)} in draft."), ss["Normal"])]
    if d["cascade"]:
        c = d["cascade"]
        story.append(Paragraph(escape(f"{c['kpi']} target: company {_n(c['company_target'])}; allocated to regions {_n(c['allocated_to_regions'])}; " + ("fully allocated." if abs(c["gap"]) < 1 else f"{_n(abs(c['gap']))} still to allocate." if c["gap"] > 0 else f"over-allocated by {_n(abs(c['gap']))}.")), ss["Normal"]))
    story += [Spacer(1, 8), Paragraph(escape(" ".join(d["notes"]) + " Confidential: company and branch totals only; no individual is named."), small)]
    doc.build(story)
    return buf.getvalue()


# ---- storing -----------------------------------------------------------------------------------------------------------------


def generate(db: Session, month: date, username: str, today: date) -> BoardPack:
    d = data(db, month, today)
    pdf = render_pdf(d)
    m = month.replace(day=1)
    row = db.query(BoardPack).filter_by(month=m).first()
    path = storage.new_path("packs", m.strftime("%Y-%m"), "pdf")
    storage.put(path, pdf, "application/pdf")
    old = row.file_path if row else None
    summary = {"achievement_pct": d["month_result"]["achievement_pct"], "ytd_pct": d["ytd"]["achievement_pct"], "completeness_pct": d["completeness"]["pct"]}
    if row is None:
        row = BoardPack(month=m, file_path=path, generated_by=username, summary=summary)
        db.add(row)
    else:
        row.file_path, row.generated_by, row.generated_at, row.summary = path, username, datetime.now(timezone.utc), summary
    db.commit()
    if old:
        storage.remove(old)
    return row
