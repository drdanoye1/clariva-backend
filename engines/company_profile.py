"""
Company Profile / Funding Intelligence Profile — shared lookup helper.

Funding Opportunity Intelligence, Phase 2 (Organization-Specific Matching,
Ranking & Decision Intelligence) migrated OrgContextDB from strictly
per-user to optionally org-owned (see its docstring in models/db_models.py)
so an organization's shared pipeline can be scored against the
organization's own profile, not against whichever individual member
happened to fill one out.

That migration has one sharp edge every caller must respect:
`OrgContextDB.user_id` is no longer unique — a user can now own both a
personal profile (org_id IS NULL) and one or more organizations' shared
profiles (org_id set), each a separate row. Any plain
`select(OrgContextDB).where(OrgContextDB.user_id == X)` followed by
`.scalar_one_or_none()` — the pattern this codebase used in five different
places before this module existed (routers/profile.py, routers/proposals.py,
routers/reviewer.py, routers/budget.py, routers/budget_export.py) — can now
raise `MultipleResultsFound` the moment that user has more than one row.

`get_org_context()` below is the one place this lookup should happen from
now on; every one of those five call sites was updated to use it instead
of repeating the query inline.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import OrgContextDB


async def get_org_context(
    db: AsyncSession, user_id: Optional[str] = None, org_id: Optional[str] = None,
) -> Optional[OrgContextDB]:
    """
    Resolve exactly one Company/Funding Intelligence Profile row:
      - `org_id` given -> the org-shared profile for that Organization.
      - `org_id` absent -> the personal profile for `user_id` (org_id IS NULL).

    Uses `.scalars().first()` rather than `.scalar_one_or_none()`
    deliberately: there is no DB-level uniqueness constraint enforcing
    "at most one row per org_id" / "at most one personal row per user_id"
    (SQLite's partial-unique-index support is limited — same precedent as
    FOARecord.external_id's dedupe, enforced at the application layer via
    routers/profile.py's get-or-create instead). A defensive `.first()`
    here means a rare duplicate row surfaces as "pick one" rather than a
    500 for every request that touches it.
    """
    if org_id:
        result = await db.execute(select(OrgContextDB).where(OrgContextDB.org_id == org_id))
    elif user_id:
        result = await db.execute(
            select(OrgContextDB).where(OrgContextDB.user_id == user_id, OrgContextDB.org_id.is_(None))
        )
    else:
        return None
    return result.scalars().first()
