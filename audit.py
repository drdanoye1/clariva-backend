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
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import AuditLog, new_uuid

_log = logging.getLogger(__name__)


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
        # Stamped here in Python (microsecond precision) rather than left to
        # the column's server_default=func.now(): SQLite's func.now() only
        # has second resolution, so two audit rows written by fast
        # successive requests in the same wall-clock second (e.g. approve
        # then suspend in one test) get identical created_at values, making
        # `ORDER BY created_at DESC` (routers/partners.py's activity feed,
        # routers/organizations.py's audit-log endpoint) non-deterministic
        # for "newest first" — it can silently return insertion order
        # instead. `AuditLog.id` is a random UUID, not a usable tiebreaker.
        created_at=datetime.now(timezone.utc),
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
