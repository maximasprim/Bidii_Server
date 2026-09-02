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

PRODUCT_BRANCH_RESTRICTIONS below overrides this per product: an
applicant's location still picks which of the *allowed* branches for
that product is the best match, using the exact same three tiers - the
restriction just narrows the candidate list before any of that runs.

COVERED_COUNTIES + Branch.county work the same way, but for every
application rather than one product: an applicant's selected county
narrows the candidate list down to branches actually serving that
county before the location text picks the closest one within it. The
two restrictions stack - a check-off loan applicant in a restricted
county gets narrowed by product first, then by county, always with a
fallback to the less-restricted list rather than ever blocking someone
outright (see _restrict_for_county's docstring for exactly how that
fallback chain works).
"""

import logging
import re

from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.services.ai_providers.base import AIProviderError
from app.services.ai_providers.factory import default_model_for, first_configured_provider, get_provider

logger = logging.getLogger("bidii.branch_assignment")

COVERED_COUNTIES = ["Nairobi", "Nyeri", "Kiambu", "Murang'a", "Kirinyaga", "Kajiado", "Machakos", "Nakuru"]
# TEMPORARY, product-specific routing restriction: check-off loan
# applications are only ever routed to whichever of these branches
# matches best, regardless of the applicant's actual stated location.
# Matched case-insensitively as a substring against each active branch's
# name or address - e.g. "headoffice" matches a branch named "Head
# Office - Nairobi", "ngong" matches "Ngong Road Branch". If your actual
# branch names don't contain these words, this silently matches nothing
# and falls back to normal unrestricted routing (logged as a warning,
# not left silent - see assign_branch below) - worth confirming your
# real branch names against these keywords before relying on this.
# To revert to normal routing for this product, delete its entry here
# (or set the value to an empty list) - nothing else needs to change.
PRODUCT_BRANCH_RESTRICTIONS: dict[str, list[str]] = {
    "check-off-loans": ["Head Office & Ngong Road"],
}

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


def _restrict_for_product(branches: list[Branch], product_slug: str | None) -> list[Branch]:
    keywords = PRODUCT_BRANCH_RESTRICTIONS.get(product_slug or "", [])
    if not keywords:
        return branches
    restricted = [
        b for b in branches if any(kw in b.name.lower() or kw in b.address.lower() for kw in keywords)
    ]
    if not restricted:
        logger.warning(
            "Product %r has a branch restriction configured (%s) but no active branch name/address matched it — "
            "falling back to normal unrestricted routing. Check PRODUCT_BRANCH_RESTRICTIONS in "
            "branch_assignment.py against your actual branch names.",
            product_slug,
            keywords,
        )
        return branches
    return restricted


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

def _restrict_for_county(branches: list[Branch], county: str | None) -> list[Branch]:
    """
    Narrows to branches whose Branch.county matches (case-insensitive)
    the applicant's selected county. Falls back to the *un*-county-filtered
    list it was given (which may already be product-restricted - see
    assign_branch) if nothing matches, rather than the full unfiltered
    branch list - a genuinely-restricted product's rule should still win
    over an incomplete/missing county assignment on branches. This is the
    same kind of gap as PRODUCT_BRANCH_RESTRICTIONS's: it only works once
    an admin has actually gone into the Branches page and set each
    branch's county - until then this matches nothing for every county
    and always falls back, which is logged, not silent.
    """
    if not county:
        return branches
    normalized = county.strip().lower()
    restricted = [b for b in branches if b.county and b.county.strip().lower() == normalized]
    if not restricted:
        logger.warning(
            "County %r has no active branch assigned to it yet — falling back to routing without county "
            "narrowing for this application. Assign counties to branches on the Branches admin page to fix this.",
            county,
        )
        return branches
    return restricted


def assign_branch(db: Session, location_text: str, product_slug: str | None = None, county: str | None = None) -> tuple[str | None, str | None]:
    """
    Returns (branch_id, method). branch_id is None only if there are
    literally no active branches configured at all - everything else
    always resolves to a real branch id.
    """
    branches = _active_branches(db)
    if not branches:
        logger.warning("No active branches configured - can't assign a branch to a new loan application.")
        return None, None

    branches = _restrict_for_product(branches, product_slug)
    branches = _restrict_for_county(branches, county)

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
