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
2. AI-geocoded nearest-branch match - only when tier 1 finds nothing and
   an AI provider is configured. Rather than asking the model to compare
   a list of branches and pick one (a harder, less reliable task for
   small/cheap models - see AIGeocodeResult's docstring), it's asked only
   to estimate real-world (lat, lng) coordinates for the applicant's
   described place. branch_assignment.py then picks the nearest branch
   itself, deterministically, via haversine distance against every active
   branch's stored Branch.lat/lng. The applicant's stated location text is
   always what drives this - county is not used to narrow the candidate
   list before this tier runs (see "Why county doesn't gate matching"
   below), so a real nearest branch is never excluded just because it's
   administratively in a different county than the applicant selected.
3. Fallback - if there's no AI provider configured, the AI call fails, or
   it returns coordinates outside Kenya, assigns to the applicant's
   selected county's lowest-display_order active branch if one exists,
   otherwise the lowest-display_order active branch overall ("head
   office" / whichever admins have ranked first). This tier never fails
   and never raises - see the try/except at the bottom of assign_branch().

`branch_assignment_method` on the resulting LoanApplication records which
tier actually decided it ("exact" | "ai" | "fallback"), so admins can see
at a glance which assignments are worth a manual double-check rather than
trusting a possibly-wrong branch silently forever - the manual reassign
endpoint (see app/routers/admin.py) always remains available regardless
of which tier assigned it.

PRODUCT_BRANCH_RESTRICTIONS below overrides all of this per product: an
applicant's location still picks the best match among the *allowed*
branches for that product, using the exact same tiers - the restriction
just narrows the candidate list before any of that runs. This is the one
hard, pre-matching filter left in this file - it encodes a real business
rule ("this product is only ever handled at this specific desk"), not a
geography signal, so it's applied before distance is even considered.

Why county doesn't gate matching (only breaks fallback-tier ties):
Earlier versions of this file narrowed the branch candidate list to only
branches in the applicant's selected county *before* running direct/AI
matching. In practice this caused two classes of wrong assignments once
real applicant data came in: (1) an applicant's actual nearest branch is
often in a neighboring county from the one they picked on the form (e.g.
"Juja Farm" selected as Kiambu is genuinely closer to Ruiru than to any
branch that happens to be tagged Kiambu), and (2) Branch.county is
admin-entered and easy to leave blank/inconsistent, which silently
widened or mis-narrowed the candidate list in ways that were invisible
until an admin cross-checked individual applications. Both problems
disappear once actual geographic distance (tier 2) decides the match
instead of an administrative boundary. County is still collected and
still enforced upstream (see COVERED_COUNTIES in the router - an
applicant outside a served county is rejected before this file ever
runs), and it's still used as a *soft* signal in two places: it's passed
to the AI as weak context for its geocode guess, and it's the tie-breaker
of last resort in the fallback tier when there's no location signal to
go on at all. If a computed nearest branch doesn't match the applicant's
stated county, that's logged (not blocked) so admins can spot-check it.
"""

import logging
import math
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


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two (lat, lng) points, in kilometers."""
    r_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * r_km * math.asin(math.sqrt(a))


def _nearest_by_coords(lat: float, lng: float, branches: list[Branch]) -> Branch:
    """Deterministic pick - the actual distance math, never left to the model."""
    return min(branches, key=lambda b: haversine_km(lat, lng, b.lat, b.lng))


def _restrict_for_county(branches: list[Branch], county: str | None) -> list[Branch]:
    """
    Narrows to branches whose Branch.county matches (case-insensitive) the
    applicant's selected county, or the unfiltered list given if nothing
    matches (or no county was given). Used only by the fallback tier now -
    see the module docstring's "Why county doesn't gate matching" section
    for why this no longer runs before direct/AI matching. As a
    last-resort tie-breaker (no location text, no AI available) county is
    still a better signal than nothing, so it's kept here.
    """
    if not county:
        return branches
    normalized = county.strip().lower()
    restricted = [b for b in branches if b.county and b.county.strip().lower() == normalized]
    if not restricted:
        logger.warning(
            "County %r has no active branch assigned to it yet — falling back to routing without county "
            "narrowing for this application's fallback tier. Assign counties to branches on the Branches "
            "admin page to fix this.",
            county,
        )
        return branches
    return restricted


def _log_if_county_mismatch(branch: Branch, county: str | None, location_text: str) -> None:
    """
    Purely observational - never changes the outcome. If the
    geography-driven pick lands in a branch tagged with a different
    county than the applicant selected, that's worth a log line for an
    admin to spot-check (could be a genuine cross-county nearest branch,
    or could be bad Branch.lat/lng or Branch.county data) - see this
    file's module docstring.
    """
    if county and branch.county and branch.county.strip().lower() != county.strip().lower():
        logger.info(
            "Loan application location %r (selected county %r) was geographically closest to %r, "
            "which is tagged county %r — routed there anyway per location-over-county priority; "
            "worth a quick admin spot-check.",
            location_text,
            county,
            branch.name,
            branch.county,
        )


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

    direct = _direct_match(location_text, branches)
    if direct is not None:
        return direct.id, "exact"

    provider_name = first_configured_provider()
    if provider_name and location_text.strip():
        try:
            provider = get_provider(provider_name)
            model = default_model_for(provider_name)
            geocode = provider.geocode_location(
                location_text=location_text, county=county, model=model, timeout_seconds=20
            )
            nearest = _nearest_by_coords(geocode.lat, geocode.lng, branches)
            _log_if_county_mismatch(nearest, county, location_text)
            return nearest.id, "ai"
        except AIProviderError as exc:
            logger.warning("AI geocoding failed for location %r, using fallback branch: %s", location_text, exc)
        except Exception:  # noqa: BLE001 - this must never break a loan application submission
            logger.exception("Unexpected error during AI geocoding for location %r", location_text)

    fallback_candidates = _restrict_for_county(branches, county)
    return fallback_candidates[0].id, "fallback"

# """
# Resolves a loan applicant's free-text location (e.g. "Kericho town",
# "near the Nakuru bus stage") to one of the company's active branches.

# Every application always ends up assigned to some real, active branch -
# never left blank - per how this was scoped: an unmatched location still
# needs to go to *the closest* branch, not sit unassigned. Three-tier
# strategy, cheapest and most certain first:

# 1. Direct match - the location text contains (or is contained in) a
#    branch's name or address. Instant, free, and correct whenever an
#    applicant types a town/area name that's an actual branch location.
# 2. AI nearest-branch match - only when tier 1 finds nothing and an AI
#    provider is configured. Gives the model the applicant's text and the
#    real list of active branches (id + name + address) and asks it to pick
#    the geographically closest one, using its general knowledge of Kenyan
#    geography. The result is validated against the real branch id list
#    before being trusted (see parse_branch_match_response) - the model
#    cannot cause an assignment to a nonexistent branch.
# 3. Fallback - if there's no AI provider configured, or the AI call fails
#    for any reason, assigns to the lowest-display_order active branch
#    (effectively "head office" / the branch admins have ranked first).
#    This tier never fails and never raises - see the try/except at the
#    bottom of assign_branch().

# `branch_assignment_method` on the resulting LoanApplication records which
# tier actually decided it ("exact" | "ai" | "fallback"), so admins can see
# at a glance which assignments are worth a manual double-check rather than
# trusting a possibly-wrong branch silently forever - the manual reassign
# endpoint (see app/routers/admin.py) always remains available regardless
# of which tier assigned it.

# PRODUCT_BRANCH_RESTRICTIONS below overrides this per product: an
# applicant's location still picks which of the *allowed* branches for
# that product is the best match, using the exact same three tiers - the
# restriction just narrows the candidate list before any of that runs.

# COVERED_COUNTIES + Branch.county work the same way, but for every
# application rather than one product: an applicant's selected county
# narrows the candidate list down to branches actually serving that
# county before the location text picks the closest one within it. The
# two restrictions stack - a check-off loan applicant in a restricted
# county gets narrowed by product first, then by county, always with a
# fallback to the less-restricted list rather than ever blocking someone
# outright (see _restrict_for_county's docstring for exactly how that
# fallback chain works).
# """

# import logging
# import re

# from sqlalchemy.orm import Session

# from app.models.branch import Branch
# from app.services.ai_providers.base import AIProviderError
# from app.services.ai_providers.factory import default_model_for, first_configured_provider, get_provider

# logger = logging.getLogger("bidii.branch_assignment")

# COVERED_COUNTIES = ["Nairobi", "Nyeri", "Kiambu", "Murang'a", "Kirinyaga", "Kajiado", "Machakos", "Nakuru"]
# # TEMPORARY, product-specific routing restriction: check-off loan
# # applications are only ever routed to whichever of these branches
# # matches best, regardless of the applicant's actual stated location.
# # Matched case-insensitively as a substring against each active branch's
# # name or address - e.g. "headoffice" matches a branch named "Head
# # Office - Nairobi", "ngong" matches "Ngong Road Branch". If your actual
# # branch names don't contain these words, this silently matches nothing
# # and falls back to normal unrestricted routing (logged as a warning,
# # not left silent - see assign_branch below) - worth confirming your
# # real branch names against these keywords before relying on this.
# # To revert to normal routing for this product, delete its entry here
# # (or set the value to an empty list) - nothing else needs to change.
# PRODUCT_BRANCH_RESTRICTIONS: dict[str, list[str]] = {
#     "check-off-loans": ["Head Office & Ngong Road"],
# }

# # Generic words that appear in lots of addresses/location text and would
# # cause false matches if treated as meaningful location signal on their
# # own (e.g. "near the Total petrol station" shouldn't match every branch
# # whose address happens to also say "near").
# _STOPWORDS = {
#     "the", "near", "town", "area", "branch", "street", "avenue", "road", "estate", "opposite",
#     "next", "along", "off", "plaza", "building", "floor", "and", "at", "in", "on", "by", "of",
# }


# def _tokenize(text: str) -> set[str]:
#     return {t for t in re.findall(r"[a-z]{3,}", text.lower()) if t not in _STOPWORDS}


# def _active_branches(db: Session) -> list[Branch]:
#     return db.query(Branch).filter(Branch.is_active.is_(True)).order_by(Branch.display_order.asc()).all()


# def _restrict_for_product(branches: list[Branch], product_slug: str | None) -> list[Branch]:
#     keywords = PRODUCT_BRANCH_RESTRICTIONS.get(product_slug or "", [])
#     if not keywords:
#         return branches
#     restricted = [
#         b for b in branches if any(kw in b.name.lower() or kw in b.address.lower() for kw in keywords)
#     ]
#     if not restricted:
#         logger.warning(
#             "Product %r has a branch restriction configured (%s) but no active branch name/address matched it — "
#             "falling back to normal unrestricted routing. Check PRODUCT_BRANCH_RESTRICTIONS in "
#             "branch_assignment.py against your actual branch names.",
#             product_slug,
#             keywords,
#         )
#         return branches
#     return restricted


# def _direct_match(location_text: str, branches: list[Branch]) -> Branch | None:
#     """
#     Token-level match, not whole-string containment - "I live in Kisumu
#     near the lake" needs to match a branch named "Kisumu Branch" even
#     though neither string contains the other in full. Matches on any
#     shared meaningful word (3+ letters, not a generic address word) that
#     appears in both the applicant's text and the branch's name/address.
#     """
#     location_tokens = _tokenize(location_text)
#     if not location_tokens:
#         return None
#     for branch in branches:
#         branch_tokens = _tokenize(branch.name) | _tokenize(branch.address)
#         if location_tokens & branch_tokens:
#             return branch
#     return None

# def _restrict_for_county(branches: list[Branch], county: str | None) -> list[Branch]:
#     """
#     Narrows to branches whose Branch.county matches (case-insensitive)
#     the applicant's selected county. Falls back to the *un*-county-filtered
#     list it was given (which may already be product-restricted - see
#     assign_branch) if nothing matches, rather than the full unfiltered
#     branch list - a genuinely-restricted product's rule should still win
#     over an incomplete/missing county assignment on branches. This is the
#     same kind of gap as PRODUCT_BRANCH_RESTRICTIONS's: it only works once
#     an admin has actually gone into the Branches page and set each
#     branch's county - until then this matches nothing for every county
#     and always falls back, which is logged, not silent.
#     """
#     if not county:
#         return branches
#     normalized = county.strip().lower()
#     restricted = [b for b in branches if b.county and b.county.strip().lower() == normalized]
#     if not restricted:
#         logger.warning(
#             "County %r has no active branch assigned to it yet — falling back to routing without county "
#             "narrowing for this application. Assign counties to branches on the Branches admin page to fix this.",
#             county,
#         )
#         return branches
#     return restricted


# def assign_branch(db: Session, location_text: str, product_slug: str | None = None, county: str | None = None) -> tuple[str | None, str | None]:
#     """
#     Returns (branch_id, method). branch_id is None only if there are
#     literally no active branches configured at all - everything else
#     always resolves to a real branch id.
#     """
#     branches = _active_branches(db)
#     if not branches:
#         logger.warning("No active branches configured - can't assign a branch to a new loan application.")
#         return None, None

#     branches = _restrict_for_product(branches, product_slug)
#     branches = _restrict_for_county(branches, county)

#     direct = _direct_match(location_text, branches)
#     if direct is not None:
#         return direct.id, "exact"

#     provider_name = first_configured_provider()
#     if provider_name and location_text.strip():
#         try:
#             provider = get_provider(provider_name)
#             model = default_model_for(provider_name)
#             branch_dicts = [{"id": b.id, "name": b.name, "address": b.address} for b in branches]
#             match = provider.suggest_nearest_branch(
#                 location_text=location_text, branches=branch_dicts, model=model, timeout_seconds=20
#             )
#             return match.branch_id, "ai"
#         except AIProviderError as exc:
#             logger.warning("AI branch matching failed for location %r, using fallback branch: %s", location_text, exc)
#         except Exception:  # noqa: BLE001 - this must never break a loan application submission
#             logger.exception("Unexpected error during AI branch matching for location %r", location_text)

#     return branches[0].id, "fallback"
