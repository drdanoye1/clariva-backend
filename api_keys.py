"""
Clariva — Public API key generation, hashing, and auth dependency
(Clariva Enterprise™ PRD §19: "a versioned public API... enabling a
partner/marketplace ecosystem").

A key is a machine credential, not a user — it authenticates as an
organization + a role (owner/editor/viewer), exactly the same role
vocabulary `rbac.py` already uses for human org members. There is
deliberately no "acting user" behind a key: `routers/public_api.py`
endpoints receive an `ApiKeyPrincipal(org_id, role, key_id)` instead of a
`User`, and gate each action with `rbac.role_has_permission(principal.role,
...)` / a plain read-vs-write check, the same registry human RBAC checks
already use.

Only `key_hash` (SHA-256 — deterministic, so a key can be looked up by
equality on every request) is ever persisted. The plaintext key is
generated once, returned once, and never stored or logged anywhere.
`hashlib.sha256` is used instead of `bcrypt` (Phase 1's password hasher)
deliberately: bcrypt's per-call random salt makes an equality lookup by
hash impossible, and an API key — a long, high-entropy random token, never
user-chosen or guessable — doesn't need bcrypt's slow, salted design the
way a human password does.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.db_models import ApiKey

KEY_PREFIX = "sk_live_"


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def generate_api_key() -> Tuple[str, str, str]:
    """Returns (plaintext_key, key_prefix_for_display, key_hash_to_store)."""
    plaintext = f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"
    key_prefix = plaintext[: len(KEY_PREFIX) + 8]
    return plaintext, key_prefix, hash_api_key(plaintext)


@dataclass
class ApiKeyPrincipal:
    org_id: str
    role: str
    key_id: str


async def get_api_key_principal(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyPrincipal:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header.")
    result = await db.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(x_api_key)))
    key = result.scalar_one_or_none()
    if not key or key.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key.")
    key.last_used_at = datetime.utcnow()
    await db.flush()
    return ApiKeyPrincipal(org_id=key.org_id, role=key.role, key_id=key.id)


def require_write(principal: ApiKeyPrincipal) -> None:
    """Only owner/editor-scoped keys may write through the public API;
    viewer-scoped keys are read-only, mirroring rbac.py's own role shape."""
    if principal.role not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="This API key is read-only.")
