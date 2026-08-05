"""
Engine 17 — Connector Framework & Webhook Platform.

Real HTTP calls are excluded from this suite, same policy as OpenAI/
Grants.gov/SAM.gov calls elsewhere (see conftest.py's module docstring):
`ConnectorEngine._post` is monkeypatched to return a canned response so the
deterministic dispatch/logging/not-configured-degradation logic underneath
it gets full, real coverage without any network access — the same pattern
test_funding_intelligence_engine.py established for
`fetch_grants_gov`/`fetch_sam_gov`.

Like the other engine test files, methods are async and need a real DB
session, so each test wraps its body in asyncio.run(). Every literal ID is
generated fresh per test via `_id()`, per the established test-isolation
discipline (see test_collaboration_engine.py's and
test_funding_intelligence_engine.py's notes on the shared, run-persistent
test DB).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from database import AsyncSessionLocal
from engines.connector_engine import ConnectorEngine


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine():
    return ConnectorEngine()


def _id(prefix: str) -> str:
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def test_list_connector_types_includes_functional_and_placeholder_types(client, engine):
    types = {t["connector_type"]: t for t in engine.list_connector_types()}
    assert types["webhook"]["functional"] is True
    assert types["slack"]["functional"] is True
    assert types["ms365"]["functional"] is False
    assert types["docusign"]["functional"] is False


def test_create_connector_rejects_unknown_type(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.create_connector(db, org_id, {"connector_type": "not_a_real_type", "name": "Bogus"}, created_by=_id("user"))
            return exc_info.value.status_code

    assert _run(_body()) == 400


def test_create_update_delete_connector_lifecycle(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            connector = await engine.create_connector(
                db, org_id, {"connector_type": "webhook", "name": "My Webhook", "config": {"target_url": "https://example.com/hook"}},
                created_by=_id("user"),
            )
            await db.commit()
            connector_id = connector.id
        async with AsyncSessionLocal() as db:
            updated = await engine.update_connector(db, connector_id, {"name": "Renamed Webhook", "active": False})
            await db.commit()
        async with AsyncSessionLocal() as db:
            listed = await engine.list_connectors(db, org_id)
            await engine.delete_connector(db, connector_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            remaining = await engine.list_connectors(db, org_id)
            return updated, listed, remaining

    updated, listed, remaining = _run(_body())
    assert updated.name == "Renamed Webhook"
    assert updated.active is False
    assert len(listed) == 1
    assert remaining == []


def test_dispatch_webhook_success_is_logged(client, engine, monkeypatch):
    org_id = _id("org")

    async def fake_post(url, **kwargs):
        return _FakeResponse(200)
    monkeypatch.setattr(engine, "_post", fake_post)

    async def _body():
        async with AsyncSessionLocal() as db:
            connector = await engine.create_connector(
                db, org_id, {"connector_type": "webhook", "name": "Hook", "config": {"target_url": "https://example.com/hook", "secret": "shh"}},
                created_by=_id("user"),
            )
            await db.commit()
            connector_id = connector.id
        async with AsyncSessionLocal() as db:
            logs = await engine.dispatch_event(db, org_id, "proposal.approved", {"proposal_id": "p1"})
            await db.commit()
            return logs, connector_id

    logs, connector_id = _run(_body())
    assert len(logs) == 1
    assert logs[0].success is True
    assert logs[0].status_code == 200
    assert logs[0].event_type == "proposal.approved"


def test_dispatch_webhook_failure_is_logged_with_error(client, engine, monkeypatch):
    org_id = _id("org")

    async def fake_post(url, **kwargs):
        return _FakeResponse(500)
    monkeypatch.setattr(engine, "_post", fake_post)

    async def _body():
        async with AsyncSessionLocal() as db:
            connector = await engine.create_connector(
                db, org_id, {"connector_type": "webhook", "name": "Hook", "config": {"target_url": "https://example.com/hook"}},
                created_by=_id("user"),
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            logs = await engine.dispatch_event(db, org_id, "member.invited", {})
            await db.commit()
            return logs

    logs = _run(_body())
    assert len(logs) == 1
    assert logs[0].success is False
    assert logs[0].status_code == 500
    assert "500" in logs[0].error


def test_dispatch_respects_event_type_subscription_filter(client, engine, monkeypatch):
    org_id = _id("org")
    calls = []

    async def fake_post(url, **kwargs):
        calls.append(url)
        return _FakeResponse(200)
    monkeypatch.setattr(engine, "_post", fake_post)

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_connector(
                db, org_id, {"connector_type": "webhook", "name": "Only Approvals", "config": {"target_url": "https://example.com/a"}, "event_types": ["approval.decided"]},
                created_by=_id("user"),
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            not_subscribed = await engine.dispatch_event(db, org_id, "member.invited", {})
            subscribed = await engine.dispatch_event(db, org_id, "approval.decided", {})
            await db.commit()
            return not_subscribed, subscribed

    not_subscribed, subscribed = _run(_body())
    assert not_subscribed == []
    assert len(subscribed) == 1


def test_dispatch_skips_inactive_connectors(client, engine, monkeypatch):
    org_id = _id("org")

    async def fake_post(url, **kwargs):
        return _FakeResponse(200)
    monkeypatch.setattr(engine, "_post", fake_post)

    async def _body():
        async with AsyncSessionLocal() as db:
            connector = await engine.create_connector(
                db, org_id, {"connector_type": "webhook", "name": "Hook", "config": {"target_url": "https://example.com/hook"}},
                created_by=_id("user"),
            )
            await db.commit()
            connector_id = connector.id
        async with AsyncSessionLocal() as db:
            await engine.update_connector(db, connector_id, {"active": False})
            await db.commit()
        async with AsyncSessionLocal() as db:
            logs = await engine.dispatch_event(db, org_id, "member.invited", {})
            return logs

    assert _run(_body()) == []


def test_dispatch_to_oauth_placeholder_type_does_not_call_http_and_is_not_logged(client, engine, monkeypatch):
    org_id = _id("org")
    called = {"count": 0}

    async def fake_post(url, **kwargs):
        called["count"] += 1
        return _FakeResponse(200)
    monkeypatch.setattr(engine, "_post", fake_post)

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_connector(db, org_id, {"connector_type": "salesforce", "name": "SFDC"}, created_by=_id("user"))
            await db.commit()
        async with AsyncSessionLocal() as db:
            logs = await engine.dispatch_event(db, org_id, "member.invited", {})
            await db.commit()
            return logs

    logs = _run(_body())
    assert logs == []
    assert called["count"] == 0


def test_test_connector_on_functional_type_hits_http_and_updates_last_tested(client, engine, monkeypatch):
    org_id = _id("org")

    async def fake_post(url, **kwargs):
        return _FakeResponse(200)
    monkeypatch.setattr(engine, "_post", fake_post)

    async def _body():
        async with AsyncSessionLocal() as db:
            connector = await engine.create_connector(
                db, org_id, {"connector_type": "slack", "name": "Slack", "config": {"webhook_url": "https://hooks.slack.com/x"}},
                created_by=_id("user"),
            )
            await db.commit()
            connector_id = connector.id
        async with AsyncSessionLocal() as db:
            result = await engine.test_connector(db, connector_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            refreshed = await engine.get_connector_or_404(db, connector_id)
            return result, refreshed

    result, refreshed = _run(_body())
    assert result["success"] is True
    assert result["configured"] is True
    assert refreshed.last_tested_at is not None


def test_test_connector_on_placeholder_type_reports_not_configured(client, engine):
    org_id = _id("org")

    async def _body():
        async with AsyncSessionLocal() as db:
            connector = await engine.create_connector(db, org_id, {"connector_type": "docusign", "name": "DocuSign"}, created_by=_id("user"))
            await db.commit()
            connector_id = connector.id
        async with AsyncSessionLocal() as db:
            return await engine.test_connector(db, connector_id)

    result = _run(_body())
    assert result["success"] is False
    assert result["configured"] is False


def test_audit_log_action_dispatches_to_webhook(client, monkeypatch):
    """Integration with audit.py::log_action — the best-effort dispatch
    hook described in audit.py's module docstring."""
    from audit import log_action
    from engines.connector_engine import ConnectorEngine as _CE

    org_id = _id("org")
    calls = []

    async def fake_post(self, url, **kwargs):
        calls.append(url)
        return _FakeResponse(200)
    monkeypatch.setattr(_CE, "_post", fake_post)

    async def _body():
        async with AsyncSessionLocal() as db:
            eng = _CE()
            await eng.create_connector(
                db, org_id, {"connector_type": "webhook", "name": "Audit Hook", "config": {"target_url": "https://example.com/hook"}},
                created_by=_id("user"),
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            await log_action(db, actor_id=_id("user"), action="member.invited", org_id=org_id)
            await db.commit()

    _run(_body())
    assert len(calls) == 1
