"""A small realistic org used by the authorization tests.

    ceo (executive, read-only)
    gm  (manages region North)
     +- bm1 (manages unit Alpha)  -> lo1, lo2
     +- bm2 (manages unit Beta)   -> lo3
    hr user, admin user (no profiles needed)

Alpha is in region North; Beta is in region South (managed by nobody).
"""

from datetime import date, timedelta
from types import SimpleNamespace

from app.models.performance import PerformanceRole, PerformanceUnit, Region, StaffProfile, UnitRegionLink
from app.services.performance_config import ensure_defaults
from app.models.admin_user import AdminUser
from performance_tests.conftest import add_user, token_headers


def build_org(client, db):
    ensure_defaults(db)
    role = db.query(PerformanceRole).filter(PerformanceRole.key == "loan_officer").first()
    alpha = PerformanceUnit(name="Alpha", kind="branch")
    beta = PerformanceUnit(name="Beta", kind="branch")
    north, south = Region(name="North"), Region(name="South")
    db.add_all([alpha, beta, north, south])
    db.flush()
    past = date.today() - timedelta(days=400)
    db.add_all([UnitRegionLink(unit_id=alpha.id, region_id=north.id, effective_from=past),
                UnitRegionLink(unit_id=beta.id, region_id=south.id, effective_from=past)])

    def person(username, name, access_role, unit=None, manager=None, **kw):
        user = add_user(db, username, access_role)
        profile = StaffProfile(full_name=name, performance_role_id=role.id, admin_user_id=user.id,
                               unit_id=unit.id if unit else None, manager_id=manager.id if manager else None, **kw)
        db.add(profile)
        db.flush()
        return profile

    ceo = person("ceo", "Chief Exec", "staff", is_executive=True)
    gm = person("gm", "General Manager", "staff")
    bm1 = person("bm1", "Branch Manager One", "loan_officer", alpha, gm)
    bm2 = person("bm2", "Branch Manager Two", "loan_officer", beta, gm)
    lo1 = person("lo1", "Officer One", "loan_officer", alpha, bm1)
    lo2 = person("lo2", "Officer Two", "loan_officer", alpha, bm1)
    lo3 = person("lo3", "Officer Three", "loan_officer", beta, bm2)
    alpha.manager_profile_id = bm1.id
    beta.manager_profile_id = bm2.id
    north.manager_profile_id = gm.id
    add_user(db, "hr1", "hr")
    add_user(db, "admin1", "admin")
    db.commit()

    names = ["ceo", "gm", "bm1", "bm2", "lo1", "lo2", "lo3", "hr1", "admin1"]
    headers = {n: token_headers(db.query(AdminUser).filter_by(username=n).one()) for n in names}
    return SimpleNamespace(
        h=headers, role=role, alpha=alpha, beta=beta, north=north, south=south,
        p=SimpleNamespace(ceo=ceo, gm=gm, bm1=bm1, bm2=bm2, lo1=lo1, lo2=lo2, lo3=lo3),
    )


def build_world(client, db):
    """build_org + role templates + levels, and levels assigned: lo1=DSE1, lo2=DSE2, lo3=SNR DSE."""
    from app.models.performance import PerformanceLevel
    from app.services.performance_levels import seed_levels
    from app.services.performance_roles import seed_role_templates

    o = build_org(client, db)
    seed_role_templates(db)
    seed_levels(db)
    lv = {l.key: l.id for l in db.query(PerformanceLevel).filter_by(role_id=o.role.id)}
    o.levels = lv
    o.p.lo1.level_id, o.p.lo2.level_id, o.p.lo3.level_id = lv["dse1"], lv["dse2"], lv["snr_dse"]
    db.commit()
    return o
