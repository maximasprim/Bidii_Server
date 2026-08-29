"""
Resolves a loan applicant's free-text location (e.g. "Kericho town",
"near the Nakuru bus stage") to one of the company's active branches.

Every application always ends up assigned to some real, active branch -
never left blank - per how this was scoped: an unmatched location still
needs to go to *the closest* branch, not sit unassigned. Three-tier
strategy, cheapest and most certain first:

1. Direct match - the location text contains (or is contained in) a
   branch's name or address. Instant, free, and correct whenever an
   applicant types a town/area name that's an actual branch location.
2. AI nearest-branch match - only when tier 1 finds nothing and an AI
   provider is configured. Gives the model the applicant's text and the
   real list of active branches (id + name + address) and asks it to pick
   the geographically closest one, using its general knowledge of Kenyan
   geography. The result is validated against the real branch id list
   before being trusted (see parse_branch_match_response) - the model
   cannot cause an assignment to a nonexistent branch.
3. Fallback - if there's no AI provider configured, or the AI call fails
   for any reason, assigns to the lowest-display_order active branch
   (effectively "head office" / the branch admins have ranked first).
   This tier never fails and never raises - see the try/except at the
   bottom of assign_branch().

`branch_assignment_method` on the resulting LoanApplication records which
tier actually decided it ("exact" | "ai" | "fallback"), so admins can see
at a glance which assignments are worth a manual double-check rather than
trusting a possibly-wrong branch silently forever - the manual reassign
endpoint (see app/routers/admin.py) always remains available regardless
of which tier assigned it.
"""

import logging
import re

from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.services.ai_providers.base import AIProviderError
from app.services.ai_providers.factory import default_model_for, first_configured_provider, get_provider

logger = logging.getLogger("bidii.branch_assignment")

# Generic words that appear in lots of addresses/location text and would
# cause false matches if treated as meaningful location signal on their
# own (e.g. "near the Total petrol station" shouldn't match every branch
# whose address happens to also say "near").
_STOPWORDS = {
    "the", "near", "town", "area", "branch", "street", "avenue", "road", "estate", "opposite",
    "next", "along", "off", "plaza", "building", "floor", "and", "at", "in", "on", "by", "of",
}


def _tokenize(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]{3,}", text.lower()) if t not in _STOPWORDS}


def _active_branches(db: Session) -> list[Branch]:
    return db.query(Branch).filter(Branch.is_active.is_(True)).order_by(Branch.display_order.asc()).all()


def _direct_match(location_text: str, branches: list[Branch]) -> Branch | None:
    """
    Token-level match, not whole-string containment - "I live in Kisumu
    near the lake" needs to match a branch named "Kisumu Branch" even
    though neither string contains the other in full. Matches on any
    shared meaningful word (3+ letters, not a generic address word) that
    appears in both the applicant's text and the branch's name/address.
    """
    location_tokens = _tokenize(location_text)
    if not location_tokens:
        return None
    for branch in branches:
        branch_tokens = _tokenize(branch.name) | _tokenize(branch.address)
        if location_tokens & branch_tokens:
            return branch
    return None


def assign_branch(db: Session, location_text: str) -> tuple[str | None, str | None]:
    """
    Returns (branch_id, method). branch_id is None only if there are
    literally no active branches configured at all - everything else
    always resolves to a real branch id.
    """
    branches = _active_branches(db)
    if not branches:
        logger.warning("No active branches configured - can't assign a branch to a new loan application.")
        return None, None

    direct = _direct_match(location_text, branches)
    if direct is not None:
        return direct.id, "exact"

    provider_name = first_configured_provider()
    if provider_name and location_text.strip():
        try:
            provider = get_provider(provider_name)
            model = default_model_for(provider_name)
            branch_dicts = [{"id": b.id, "name": b.name, "address": b.address} for b in branches]
            match = provider.suggest_nearest_branch(
                location_text=location_text, branches=branch_dicts, model=model, timeout_seconds=20
            )
            return match.branch_id, "ai"
        except AIProviderError as exc:
            logger.warning("AI branch matching failed for location %r, using fallback branch: %s", location_text, exc)
        except Exception:  # noqa: BLE001 - this must never break a loan application submission
            logger.exception("Unexpected error during AI branch matching for location %r", location_text)

    return branches[0].id, "fallback"
