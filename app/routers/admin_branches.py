from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.branch import Branch
from app.schemas.branch import (
    BranchCreate,
    BranchCreateResponse,
    BranchListResponse,
    BranchRead,
    BranchUpdate,
    BranchUpdateResponse,
)
from app.services.auth import get_current_admin

router = APIRouter(prefix="/api/admin/branches", tags=["admin-branches"], dependencies=[Depends(get_current_admin)])


@router.get("", response_model=BranchListResponse)
def list_all_branches(db: Session = Depends(get_db)) -> BranchListResponse:
    """Includes inactive branches, for the admin Branches configuration page."""
    branches = db.query(Branch).order_by(Branch.display_order).all()
    return BranchListResponse(items=[BranchRead.model_validate(b) for b in branches])


@router.post("", response_model=BranchCreateResponse, status_code=status.HTTP_201_CREATED)
def create_branch(payload: BranchCreate, db: Session = Depends(get_db)) -> BranchCreateResponse:
    branch = Branch(**payload.model_dump())
    db.add(branch)
    db.commit()
    db.refresh(branch)
    return BranchCreateResponse(data=BranchRead.model_validate(branch))


@router.patch("/{branch_id}", response_model=BranchUpdateResponse)
def update_branch(branch_id: str, payload: BranchUpdate, db: Session = Depends(get_db)) -> BranchUpdateResponse:
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if branch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found.")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(branch, field, value)

    db.commit()
    db.refresh(branch)
    return BranchUpdateResponse(data=BranchRead.model_validate(branch))


@router.delete("/{branch_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_branch(branch_id: str, db: Session = Depends(get_db)) -> None:
    """
    Hard delete - branches aren't referenced by any other record (unlike
    loan tiers, which applications denormalize a copy of), so there's
    nothing else to keep consistent. Prefer setting is_active=False
    instead if the intent is to temporarily stop listing a branch while
    keeping it around for reference.
    """
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if branch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found.")
    db.delete(branch)
    db.commit()
