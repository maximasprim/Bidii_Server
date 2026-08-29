from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.branch import Branch
from app.schemas.branch import BranchPublicListResponse, BranchPublicRead

router = APIRouter(prefix="/api/branches", tags=["branches"])


@router.get("", response_model=BranchPublicListResponse)
def list_active_branches(db: Session = Depends(get_db)) -> BranchPublicListResponse:
    """
    Powers the public Branch Locator page and the homepage branches
    preview. This is the live, admin-editable replacement for the
    hardcoded `branches` array that used to live in
    src/data/content.ts - admin add/edit/remove here takes effect
    immediately, no frontend deploy needed.
    """
    branches = db.query(Branch).filter(Branch.is_active.is_(True)).order_by(Branch.display_order).all()
    return BranchPublicListResponse(items=[BranchPublicRead.model_validate(b) for b in branches])
