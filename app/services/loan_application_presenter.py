"""
LoanApplicationRead has a couple of fields (assigned_branch_name,
assigned_loan_officer_name) that aren't real columns on LoanApplication -
they're joined in for display convenience. Pydantic's `from_attributes`
can't resolve those automatically, so every place that turns a
LoanApplication row into a LoanApplicationRead goes through this function
instead of a bare `LoanApplicationRead.model_validate(record)`, to avoid
that logic (and its N+1-avoidance) being duplicated/drifting between
app/routers/loan_applications.py and app/routers/admin.py.
"""

from sqlalchemy.orm import Session

from app.models.admin_user import AdminUser
from app.models.branch import Branch
from app.models.loan_application import LoanApplication
from app.schemas.loan_application import LoanApplicationRead


def to_loan_application_read(db: Session, record: LoanApplication) -> LoanApplicationRead:
    branch_name = None
    if record.assigned_branch_id:
        branch = db.query(Branch).filter(Branch.id == record.assigned_branch_id).first()
        branch_name = branch.name if branch else None

    officer_name = None
    if record.assigned_loan_officer_id:
        officer = db.query(AdminUser).filter(AdminUser.id == record.assigned_loan_officer_id).first()
        officer_name = officer.username if officer else None

    data = LoanApplicationRead.model_validate(record).model_dump()
    data["assigned_branch_name"] = branch_name
    data["assigned_loan_officer_name"] = officer_name
    return LoanApplicationRead(**data)


def to_loan_application_read_list(db: Session, records: list[LoanApplication]) -> list[LoanApplicationRead]:
    """
    Batches the branch/officer lookups instead of querying once per row -
    matters once a branch/officer list page has more than a handful of
    applications on it.
    """
    branch_ids = {r.assigned_branch_id for r in records if r.assigned_branch_id}
    officer_ids = {r.assigned_loan_officer_id for r in records if r.assigned_loan_officer_id}
    branches = {b.id: b.name for b in db.query(Branch).filter(Branch.id.in_(branch_ids)).all()} if branch_ids else {}
    officers = (
        {o.id: o.username for o in db.query(AdminUser).filter(AdminUser.id.in_(officer_ids)).all()}
        if officer_ids
        else {}
    )

    results = []
    for record in records:
        data = LoanApplicationRead.model_validate(record).model_dump()
        data["assigned_branch_name"] = branches.get(record.assigned_branch_id)
        data["assigned_loan_officer_name"] = officers.get(record.assigned_loan_officer_id)
        results.append(LoanApplicationRead(**data))
    return results
