"""
Mirrors the job titles in the frontend's src/data/content.ts jobOpenings.
Unlike loan tiers, this is intentionally loose validation — job openings
change more often than loan products, and rejecting a real applicant
because this list is a week stale would be worse than accepting an
application against a role that's since been filled or renamed.
"""

KNOWN_ROLES = [
    "Loan Officer",
    "Credit Risk Analyst",
    "Branch Relationship Officer",
    "Field Collections Associate",
    "Digital Product Associate",
    "General application",
]
