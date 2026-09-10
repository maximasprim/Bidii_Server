"""
Blocks a new loan application submission when the same person already has
one sitting at "pending" or "assigned" - stacking multiple applications
while staff haven't actually engaged with the first one isn't useful to
anyone. Being auto-routed to a specific agent (see
app/services/product_routing.py) or a branch queue moves a fresh
application straight to "assigned" without anyone having looked at it
yet, so "assigned" needs to block resubmission exactly like "pending"
always did - it's not the "staff have made contact" signal, "contacted"
is. Once staff move it to contacted, approved, or declined, this stops
blocking - that's the real signal that a human has engaged with it.

Matches on two independent signals, either one being enough to count as
the same person:
- id_number (exact, normalized) - the reliable identifier; two different
  people essentially never share one.
- full_name (normalized: case/whitespace-insensitive) - weaker on its own
  (common names collide, the same person might type their name slightly
  differently), but asked for explicitly and still useful as a second
  check alongside id_number, not instead of it.
"""

import re

from sqlalchemy.orm import Session

from app.models.loan_application import LoanApplication, LoanApplicationStatus

_UNENGAGED_STATUSES = (LoanApplicationStatus.pending, LoanApplicationStatus.assigned)


def _normalize_id_number(id_number: str) -> str:
    return re.sub(r"\s+", "", id_number).strip().lower()


def _normalize_name(full_name: str) -> str:
    return re.sub(r"\s+", " ", full_name).strip().lower()


def find_pending_duplicate(db: Session, *, id_number: str, full_name: str) -> LoanApplication | None:
    """
    Returns the existing pending LoanApplication if this applicant
    (matched by id_number or full_name) already has one, else None.
    Doesn't distinguish which signal matched - callers don't need to know,
    they just need to know whether to block the new submission.
    """
    normalized_id = _normalize_id_number(id_number)
    normalized_name = _normalize_name(full_name)

    candidates = db.query(LoanApplication).filter(LoanApplication.status.in_(_UNENGAGED_STATUSES)).all()
    for candidate in candidates:
        if _normalize_id_number(candidate.id_number) == normalized_id:
            return candidate
        if _normalize_name(candidate.full_name) == normalized_name:
            return candidate
    return None
