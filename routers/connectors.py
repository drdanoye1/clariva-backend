"""
Connector Framework & Webhook Platform router (Clariva Enterprise™ PRD §19,
Phase 6). Viewing connectors/event logs requires org membership; creating,
editing, deleting, or testing a connector requires the owner-only
`manage_connectors` permission (see rbac.py) — connector credentials
(webhook URLs, secrets) are org-identity/security-sensitive.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import User
from models.schemas import (
    ConnectorCreate, ConnectorEventLogOut, ConnectorOut, ConnectorTestResultOut,
    ConnectorTypeOut, ConnectorUpdate,
)
from routers.auth import get_current_user
from routers.organizations import _assert_member, _assert_permission
from engines.connector_engine import ConnectorEngine

router = APIRouter()
engine = ConnectorEngine()


@router.get("/types", response_model=List[ConnectorTypeOut])
async def list_connector_types(current_user: User = Depends(get_current_user)):
    return engine.list_connector_types()


@router.get("", response_model=List[ConnectorOut])
async def list_connectors(org_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _assert_member(org_id, current_user.id, db)
    return await engine.list_connectors(db, org_id)


@router.post("", response_model=ConnectorOut)
async def create_connector(
    org_id: str, payload: ConnectorCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "manage_connectors", db)
    connector = await engine.create_connector(db, org_id, payload.model_dump(), created_by=current_user.id)
    await db.commit()
    return connector


@router.patch("/{connector_id}", response_model=ConnectorOut)
async def update_connector(
    connector_id: str, payload: ConnectorUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    connector = await engine.get_connector_or_404(db, connector_id)
    await _assert_permission(connector.org_id, current_user.id, "manage_connectors", db)
    updated = await engine.update_connector(db, connector_id, payload.model_dump(exclude_unset=True))
    await db.commit()
    return updated


@router.delete("/{connector_id}")
async def delete_connector(connector_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    connector = await engine.get_connector_or_404(db, connector_id)
    await _assert_permission(connector.org_id, current_user.id, "manage_connectors", db)
    await engine.delete_connector(db, connector_id)
    await db.commit()
    return {"deleted": True}


@router.post("/{connector_id}/test", response_model=ConnectorTestResultOut)
async def test_connector(connector_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    connector = await engine.get_connector_or_404(db, connector_id)
    await _assert_permission(connector.org_id, current_user.id, "manage_connectors", db)
    result = await engine.test_connector(db, connector_id)
    await db.commit()
    return result


@router.get("/{connector_id}/events", response_model=List[ConnectorEventLogOut])
async def list_connector_events(connector_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    connector = await engine.get_connector_or_404(db, connector_id)
    await _assert_member(connector.org_id, current_user.id, db)
    return await engine.list_event_log(db, connector_id)
