"""
Shared Notification helper.

Before this module existed, three call sites each carried their own
identical inline copy of "build a Notification, add it, flush it":
`CollaborationEngine.notify`, `AwardEngine._notify`, and an inline insert
inside `FundingIntelligenceEngine.match_watchlists`. That's the same
"parallel pattern" tech debt this codebase avoids everywhere else (see
`audit.py::log_action` for the equivalent single-source-of-truth helper
for AuditLog rows) — this module is that single source of truth for
Notification rows instead, and Phase 3 (§4.4 Intelligent Alerts) needed a
fourth call site, which was the forcing function to unify rather than
write a fourth copy.

`CollaborationEngine.notify` and `AwardEngine._notify` now both delegate
here (`dedupe=False`, matching their original never-deduped behavior
exactly — this refactor changes nothing about their external behavior).
`FundingIntelligenceEngine.match_watchlists` and the new
`engines/alerts_engine.py` both opt into `dedupe=True`, reusing the exact
same "does an identical (user_id, type_, object_type, object_id)
notification already exist" rule match_watchlists originated.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import Notification, new_uuid


async def notify(
    db: AsyncSession, user_id: str, type_: str, message: str,
    object_type: Optional[str] = None, object_id: Optional[str] = None,
    dedupe: bool = False,
) -> Optional[Notification]:
    """Writes one Notification row and flushes it. When `dedupe=True` and
    an identical (user_id, type_, object_type, object_id) notification
    already exists, this is a no-op that returns None instead of writing a
    duplicate — callers that want at-most-once alerts (watchlist matches,
    Intelligent Alerts) should pass `dedupe=True`; callers that model
    genuinely repeatable events (a mention, a task assignment) should
    leave it False, same as before this helper existed."""
    if dedupe and object_type and object_id:
        existing = await db.execute(
            select(Notification).where(
                Notification.user_id == user_id, Notification.type == type_,
                Notification.object_type == object_type, Notification.object_id == object_id,
            ).limit(1)
        )
        if existing.scalars().first():
            return None

    n = Notification(
        id=new_uuid(), user_id=user_id, type=type_, message=message,
        object_type=object_type, object_id=object_id,
    )
    db.add(n)
    await db.flush()
    return n
