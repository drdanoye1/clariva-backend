"""
Public API key management (Clariva Enterprise™ PRD §19), nested under the
same /api/v1/organizations prefix as credits.py — issuing and revoking keys
is an org-owner action, same "manage_api_keys" owner-only gate as
connectors/branding (see rbac.py). See api_keys.py (the module, not this
router) for key generation/hashing and routers/public_api.py for the
endpoints these keys actually authenticate against.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import ApiKey, User, new_uuid
from models.schemas import ApiKeyCreate, ApiKeyCreatedOut, ApiKeyOut
from routers.auth import get_current_user
from routers.organizations import _assert_permission
from rbac import ROLES, is_valid_role
from audit import log_action
from api_keys import generate_api_key

router = APIRouter()


@router.post("/{org_id}/api-keys", response_model=ApiKeyCreatedOut)
async def create_api_key(
    org_id: str, payload: ApiKeyCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "manage_api_keys", db)
    if not is_valid_role(payload.role):
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(ROLES)}")

    plaintext, key_prefix, key_hash = generate_api_key()
    key = ApiKey(id=new_uuid(), org_id=org_id, name=payload.name, key_prefix=key_prefix, key_hash=key_hash,
                 role=payload.role, created_by=current_user.id)
    db.add(key)
    await db.flush()
    await db.refresh(key)
    await log_action(db, actor_id=current_user.id, action="api_key.created", org_id=org_id,
                      object_type="api_key", object_id=key.id, detail={"name": key.name, "role": key.role})
    await db.commit()
    return ApiKeyCreatedOut(id=key.id, name=key.name, role=key.role, key=plaintext, key_prefix=key_prefix)


@router.get("/{org_id}/api-keys", response_model=List[ApiKeyOut])
async def list_api_keys(org_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    await _assert_permission(org_id, current_user.id, "manage_api_keys", db)
    result = await db.execute(select(ApiKey).where(ApiKey.org_id == org_id).order_by(ApiKey.created_at.desc()))
    return list(result.scalars().all())


@router.delete("/{org_id}/api-keys/{key_id}")
async def revoke_api_key(
    org_id: str, key_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    await _assert_permission(org_id, current_user.id, "manage_api_keys", db)
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.org_id == org_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    if not key.revoked_at:
        key.revoked_at = datetime.utcnow()
        await db.flush()
        await log_action(db, actor_id=current_user.id, action="api_key.revoked", org_id=org_id,
                          object_type="api_key", object_id=key.id)
    await db.commit()
    return {"revoked": True}
