"""Speed: the report setup costs a fixed number of queries however many roles there are, and gives exactly the same answers as before."""

from datetime import date

from sqlalchemy import event

from app.models.performance import PerformanceRole, StaffProfile
from app.services import performance_reports as pr
from app.services.performance_batch import kpis_by_version, latest_versions
from app.services.performance_config import effective_config, get_org_config
from app.services.performance_roles import kpis_for, latest_version
from performance_tests.org import build_world

START, END = date(2026, 1, 1), date(2026, 12, 31)


def statements(db, fn):
    seen: list[str] = []
    engine = db.get_bind()

    def hook(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", hook)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", hook)
    return len(seen)


def staff_in_roles(db, n):
    roles = db.query(PerformanceRole).order_by(PerformanceRole.key).limit(n).all()
    out = []
    for i, role in enumerate(roles):
        p = StaffProfile(full_name=f"Speed Test {i}", performance_role_id=role.id)
        db.add(p)
        out.append(p)
    db.commit()
    return [p.id for p in out], roles


def test_setup_queries_do_not_grow_with_the_number_of_roles(client, db):
    build_world(client, db)
    one_ids, _ = staff_in_roles(db, 1)
    many_ids, roles = staff_in_roles(db, 8)
    assert len(roles) >= 6, "the seeded roles are needed for this test"
    pr.load_ctx(db, one_ids, START, END)  # warm anything one-off (defaults, seeds)
    few = statements(db, lambda: pr.load_ctx(db, one_ids, START, END))
    many = statements(db, lambda: pr.load_ctx(db, many_ids, START, END))
    assert many <= few + 3, f"{many} queries for 8 roles vs {few} for 1: setup should not cost more per role"


def test_batched_lookups_equal_the_one_by_one_ones(client, db):
    build_world(client, db)
    ids, roles = staff_in_roles(db, 8)
    ctx = pr.load_ctx(db, ids, START, END)
    org, _ = get_org_config(db)
    versions = latest_versions(db, [r.id for r in roles])
    kpis = kpis_by_version(db, [v.id for v in versions.values() if v is not None])
    for role in roles:
        single = latest_version(db, role.id)
        assert (versions[role.id].id if versions[role.id] else None) == (single.id if single else None)
        expect_kpis = [k.id for k in kpis_for(db, single.id)] if single else []
        assert [k.id for k in ctx.role_kpis[role.id]] == expect_kpis
        assert [k.id for k in kpis.get(single.id, [])] == expect_kpis if single else True
        assert ctx.cfg[role.id] == effective_config(db, role)


def test_effective_config_with_a_given_org_matches_and_is_a_private_copy(client, db):
    build_world(client, db)
    role = db.query(PerformanceRole).filter(PerformanceRole.key == "loan_officer").one()
    org, _ = get_org_config(db)
    a, b = effective_config(db, role, org=org), effective_config(db, role, org=org)
    assert a == effective_config(db, role) and a is not b and a is not org
    a["__scratch__"] = 1
    assert "__scratch__" not in b and "__scratch__" not in org  # one role's copy never leaks into another's
    role.config_overrides = {next(k for k, v in org.items() if isinstance(v, int) and not isinstance(v, bool)): 1}
    assert effective_config(db, role, org=org) == effective_config(db, role)  # with an override, or an ignored invalid one: same either way
    role.config_overrides = {"not_a_real_setting": 5}
    assert effective_config(db, role, org=org) == effective_config(db, role)
    role.config_overrides = None
    db.commit()

