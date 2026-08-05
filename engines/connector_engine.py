"""
Engine 17 — Connector Framework & Webhook Platform
Delivered as an Enterprise Integration Platform layer (Clariva Enterprise™
PRD §19): a connector framework plus outbound webhook dispatch, so
integrations are added without touching core engines.

Design notes (same discipline as engines/funding_intelligence_engine.py):
- One uniform `ConnectorConnection` row represents any external
  integration. `CONNECTOR_TYPES` below is the registry of what's actually
  functional in this environment (webhook/Slack/Teams — no OAuth app
  needed) versus registered-but-not-configured placeholders (Microsoft
  365, Google Workspace, Salesforce, HubSpot, DocuSign, Adobe Sign,
  financial ERPs — all of which need a real OAuth app and live
  credentials this environment doesn't have). Dispatching to a
  non-functional type always returns "not configured" rather than
  erroring — the same graceful-degradation pattern SAM.gov established in
  Phase 4 (see funding_intelligence_engine.py).
- `_post()` is the one seam that makes an actual HTTP call — tests
  monkeypatch it exactly like funding_intelligence_engine.py's
  `fetch_grants_gov`/`fetch_sam_gov`, so no real network access happens in
  the automated suite.
- Every method takes an already-open AsyncSession and only flushes, never
  commits — the caller's request-scoped session commits once, after the
  router handler returns.
- `dispatch_event()` is called from audit.py::log_action() on a
  best-effort basis (wrapped in try/except there) so a webhook failure can
  never break the primary action being audited. This reuses the audit
  log's existing "every permission-gated action" call sites as the event
  source, rather than sprinkling new dispatch calls through dozens of
  routers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import ConnectorConnection, ConnectorEventLog, new_uuid

_log = logging.getLogger(__name__)

CONNECTOR_TYPES: Dict[str, Dict[str, Any]] = {
    "webhook":          {"label": "Generic Webhook",     "functional": True,  "config_fields": ["target_url", "secret"]},
    "slack":            {"label": "Slack",                "functional": True,  "config_fields": ["webhook_url"]},
    "teams":            {"label": "Microsoft Teams",      "functional": True,  "config_fields": ["webhook_url"]},
    "ms365":            {"label": "Microsoft 365",        "functional": False, "config_fields": []},
    "google_workspace": {"label": "Google Workspace",     "functional": False, "config_fields": []},
    "salesforce":       {"label": "Salesforce",           "functional": False, "config_fields": []},
    "hubspot":          {"label": "HubSpot",               "functional": False, "config_fields": []},
    "docusign":         {"label": "DocuSign",              "functional": False, "config_fields": []},
    "adobe_sign":       {"label": "Adobe Sign",            "functional": False, "config_fields": []},
    "financial_erp":    {"label": "Financial ERP",         "functional": False, "config_fields": []},
}


class ConnectorEngine:
    async def _post(self, url: str, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.post(url, **kwargs)

    # ── Connector types registry ────────────────────────────────────────────

    def list_connector_types(self) -> List[Dict[str, Any]]:
        return [{"connector_type": k, **v} for k, v in CONNECTOR_TYPES.items()]

    # ── Connections ──────────────────────────────────────────────────────────

    async def get_connector_or_404(self, db: AsyncSession, connector_id: str) -> ConnectorConnection:
        result = await db.execute(select(ConnectorConnection).where(ConnectorConnection.id == connector_id))
        connector = result.scalar_one_or_none()
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        return connector

    async def list_connectors(self, db: AsyncSession, org_id: str) -> List[ConnectorConnection]:
        result = await db.execute(
            select(ConnectorConnection).where(ConnectorConnection.org_id == org_id).order_by(ConnectorConnection.created_at.desc())
        )
        return list(result.scalars().all())

    async def create_connector(self, db: AsyncSession, org_id: str, data: Dict[str, Any], created_by: str) -> ConnectorConnection:
        connector_type = data["connector_type"]
        if connector_type not in CONNECTOR_TYPES:
            raise HTTPException(status_code=400, detail=f"Unknown connector type: {connector_type}")
        connector = ConnectorConnection(
            id=new_uuid(), org_id=org_id, connector_type=connector_type, name=data["name"],
            config=data.get("config") or {}, event_types=data.get("event_types"), created_by=created_by,
        )
        db.add(connector)
        await db.flush()
        await db.refresh(connector)
        return connector

    async def update_connector(self, db: AsyncSession, connector_id: str, data: Dict[str, Any]) -> ConnectorConnection:
        connector = await self.get_connector_or_404(db, connector_id)
        for field in ("name", "config", "event_types", "active"):
            if field in data and data[field] is not None:
                setattr(connector, field, data[field])
        await db.flush()
        await db.refresh(connector)
        return connector

    async def delete_connector(self, db: AsyncSession, connector_id: str) -> None:
        connector = await self.get_connector_or_404(db, connector_id)
        await db.delete(connector)
        await db.flush()

    # ── Dispatch ─────────────────────────────────────────────────────────────

    async def _dispatch_one(self, connector: ConnectorConnection, event_type: str, payload: Dict[str, Any]) -> Tuple[bool, Optional[int], Optional[str]]:
        meta = CONNECTOR_TYPES.get(connector.connector_type)
        if not meta or not meta["functional"]:
            return False, None, "not_configured"
        config = connector.config or {}
        try:
            if connector.connector_type == "webhook":
                target_url = config.get("target_url")
                if not target_url:
                    return False, None, "target_url is not configured."
                body = json.dumps({"event": event_type, "data": payload}, default=str).encode()
                headers = {"Content-Type": "application/json"}
                secret = config.get("secret")
                if secret:
                    headers["X-Clariva-Signature"] = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                resp = await self._post(target_url, content=body, headers=headers)
            elif connector.connector_type in ("slack", "teams"):
                webhook_url = config.get("webhook_url")
                if not webhook_url:
                    return False, None, "webhook_url is not configured."
                text = f"[Clariva] {event_type}: {json.dumps(payload, default=str)[:500]}"
                resp = await self._post(webhook_url, json={"text": text})
            else:
                return False, None, "not_configured"
        except Exception as exc:
            _log.warning("Connector %s dispatch failed: %s", connector.id, exc)
            return False, None, str(exc)[:500]

        success = resp.status_code < 400
        return success, resp.status_code, None if success else f"HTTP {resp.status_code}"

    async def dispatch_event(self, db: AsyncSession, org_id: str, event_type: str, payload: Dict[str, Any]) -> List[ConnectorEventLog]:
        connectors = await self.list_connectors(db, org_id)
        active = [c for c in connectors if c.active]
        logs: List[ConnectorEventLog] = []
        for connector in active:
            subscribed = not connector.event_types or event_type in connector.event_types
            if not subscribed:
                continue
            success, status_code, error = await self._dispatch_one(connector, event_type, payload)
            if error == "not_configured":
                continue  # don't clutter the log with placeholder connector types
            log = ConnectorEventLog(
                id=new_uuid(), connector_id=connector.id, event_type=event_type,
                payload=payload, success=success, status_code=status_code, error=error,
            )
            db.add(log)
            if error:
                connector.last_error = error
            logs.append(log)
        if logs:
            await db.flush()
        return logs

    async def test_connector(self, db: AsyncSession, connector_id: str) -> Dict[str, Any]:
        connector = await self.get_connector_or_404(db, connector_id)
        meta = CONNECTOR_TYPES.get(connector.connector_type)
        if not meta or not meta["functional"]:
            return {"success": False, "configured": False, "status_code": None, "error": "This connector type needs a real OAuth setup this environment doesn't have yet."}

        success, status_code, error = await self._dispatch_one(connector, "test", {"message": "This is a test event from Clariva."})
        log = ConnectorEventLog(
            id=new_uuid(), connector_id=connector.id, event_type="test",
            payload={"message": "This is a test event from Clariva."}, success=success, status_code=status_code, error=error,
        )
        db.add(connector)
        db.add(log)
        connector.last_tested_at = datetime.utcnow()
        if error:
            connector.last_error = error
        await db.flush()
        return {"success": success, "configured": True, "status_code": status_code, "error": error}

    async def list_event_log(self, db: AsyncSession, connector_id: str, limit: int = 50) -> List[ConnectorEventLog]:
        result = await db.execute(
            select(ConnectorEventLog).where(ConnectorEventLog.connector_id == connector_id)
            .order_by(ConnectorEventLog.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())
