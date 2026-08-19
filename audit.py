"""
Clariva — Audit logging helper (Clariva Enterprise™ PRD §18: "every
permission-gated action is logged... to support enterprise compliance
requirements").

Usage from any router:

    from audit import log_action
    await log_action(db, actor_id=current_user.id, action="member.invited",
                      org_id=org_id, object_type="user", object_id=target.id,
                      detail={"role": body.role})

`log_action` only adds + flushes the row; it does not commit — callers
already commit at the end of their request (via the `get_db` dependency's
session-per-request commit), consistent with how every other write in this
codebase works.

Phase 6 (Clariva Enterprise™ PRD §19) layers outbound webhook/connector
dispatch onto this same call site: every audited, org-scoped action is
already "every permission-gated action" the PRD wants observable — rather
than sprinkling new dispatch calls through dozens of routers, `log_action`
fires `ConnectorEngine.dispatch_event(db, org_id, action, detail)` as a
best-effort side effect. It is wrapped in try/except so a connector/
webhook failure (a bad URL, a timeout, an unreachable third party) can
never break the primary action being audited — the audit row itself always
gets written regardless.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import AuditLog, new_uuid

_log = logging.getLogger(__name__)

# Process-wide "last timestamp handed out" — see _next_created_at()'s
# docstring for why this exists.
_last_stamped_at: Optional[datetime] = None


def _next_created_at() -> datetime:
    """Returns a created_at value guaranteed to be strictly greater than
    every value this function has previously returned in this process.

    The naive fix (stamping datetime.now(timezone.utc) in Python instead of
    relying on the DB's func.now()) assumes the OS clock has microsecond
    resolution. It doesn't always: Windows' datetime.now() can return the
    same tick for two calls only a few hundred microseconds apart, which is
    routine for two sequential audit_log calls within one request-handling
    burst (e.g. approve-then-suspend in a test, or any handler that logs
    twice). When that tie happens, `ORDER BY created_at DESC` (routers/
    partners.py, routers/collaboration.py, routers/organizations.py) falls
    back to insertion/row order for the tied rows — i.e. oldest first,
    exactly backwards from the "newest first" activity feeds this powers.

    This function is a synchronous, non-awaiting critical section (no
    `await` between the read and write of the module-level variable), which
    is all the safety a single-threaded asyncio event loop needs — no lock
    required. It only guards against ties within one worker process; two
    separate `WEB_CONCURRENCY` workers writing to the same object_id at the
    literal same instant could still tie, but that's a vanishingly rare
    cross-process race, not the routine same-process case this fixes.
    """
    global _last_stamped_at
    now = datetime.now(timezone.utc)
    if _last_stamped_at is not None and now <= _last_stamped_at:
        now = _last_stamped_at + timedelta(microseconds=1)
    _last_stamped_at = now
    return now


async def log_action(
    db: AsyncSession,
    actor_id: str,
    action: str,
    org_id: Optional[str] = None,
    object_type: Optional[str] = None,
    object_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> AuditLog:
    entry = AuditLog(
        id=new_uuid(),
        org_id=org_id,
        actor_id=actor_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        detail=detail,
        # Stamped via _next_created_at() (strictly monotonic within this
        # process) rather than left to the column's server_default=func.now()
        # or a bare datetime.now(): the DB default's resolution (seconds on
        # SQLite) and even Python's own clock resolution (Windows can repeat
        # a tick across two calls microseconds apart) both allow two audit
        # rows written by fast successive requests (e.g. approve then
        # suspend in one test) to collide on created_at, making `ORDER BY
        # created_at DESC` (routers/partners.py's activity feed, routers/
        # collaboration.py's, routers/organizations.py's audit-log endpoint)
        # non-deterministic for "newest first" — it can silently return
        # insertion order instead. `AuditLog.id` is a random UUID, not a
        # usable tiebreaker, so this is the actual fix rather than a
        # cosmetic one.
        created_at=_next_created_at(),
    )
    db.add(entry)
    await db.flush()

    if org_id:
        try:
            from engines.connector_engine import ConnectorEngine
            await ConnectorEngine().dispatch_event(db, org_id, action, {
                "actor_id": actor_id, "object_type": object_type, "object_id": object_id, "detail": detail,
            })
        except Exception as exc:
            _log.warning("Connector dispatch failed for action %s (org %s): %s", action, org_id, exc)

    return entry
