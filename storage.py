"""
File Storage Service — Cloudflare R2 (S3-compatible object storage).

Version 3.0 upgrade, "Real File Storage" scope (see docs/
Clariva_File_Storage_Scoping_Document.docx, Phase A). This is the ONLY
module in the codebase that should import boto3 or know R2's endpoint
shape — every engine/router calls the three functions below rather than
talking to R2 directly, the same "one shared helper, not reimplemented per
caller" convention already used elsewhere (routers/payments.py::
_square_config(), routers/extract.py's _extract_text_from_pdf()).

Why boto3 (synchronous) wrapped in asyncio.to_thread(), not an async S3
client (e.g. aioboto3): boto3 is the mature, battle-tested SDK; the async
wrapper libraries are comparatively immature, and this module's I/O is
infrequent and small (PDFs/DOCX/XLSX, not a hot path or bulk media), so the
stability of boto3 outweighs the marginal benefit of a "natively" async
client. This mirrors the pragmatism already reflected in requirements.txt,
which pairs the async asyncpg driver with the sync psycopg2-binary.

Degradation behavior deliberately differs from this codebase's other
optional integrations (SAM_GOV_API_KEY, RESEND_API_KEY), which log a
warning and silently no-op when unconfigured. File storage has no
meaningful no-op fallback — an unconfigured R2 means the upload or download
literally cannot happen — so instead this raises HTTPException(503) at the
point of use, exactly like routers/payments.py::_square_config() does for
missing Square credentials.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Optional

import boto3
from botocore.client import Config as BotoConfig
from fastapi import HTTPException

from config import settings

_client = None


def _get_client():
    """Lazily creates and caches the boto3 S3 client pointed at R2's
    S3-compatible endpoint. Raises HTTPException(503) if R2 credentials
    haven't been configured, rather than crashing app boot — the same
    "fail loudly at the point of use" behavior payments.py's
    _square_config() already uses for a missing Square token."""
    global _client
    if _client is not None:
        return _client
    if not (settings.R2_ACCOUNT_ID and settings.R2_ACCESS_KEY_ID
            and settings.R2_SECRET_ACCESS_KEY and settings.R2_BUCKET_NAME):
        raise HTTPException(
            status_code=503,
            detail="File storage is not configured. Contact support.",
        )
    _client = boto3.client(
        "s3",
        endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        config=BotoConfig(signature_version="s3v4"),
        region_name="auto",
    )
    return _client


def _new_key(org_id: Optional[str], category: str, filename: str) -> str:
    """Namespaces every object by org (or "personal" for orgless files) and
    category, so a future retention policy or bulk export can operate
    per-org/per-category without a database join — see the scoping doc's
    Section 3.1."""
    from models.db_models import new_uuid
    safe_name = re.sub(r"[^\w\-.]", "_", filename)[:100]
    prefix = org_id or "personal"
    return f"{prefix}/{category}/{new_uuid()}_{safe_name}"


async def upload_file(
    org_id: Optional[str], category: str, content: bytes, filename: str, content_type: str,
) -> str:
    """Uploads bytes to R2 and returns the storage key (not a URL — callers
    persist the key in a StoredFile row and resolve a download URL on
    demand via get_download_url(), since presigned URLs expire and
    shouldn't be treated as permanent)."""
    client = _get_client()
    key = _new_key(org_id, category, filename)
    await asyncio.to_thread(
        client.put_object,
        Bucket=settings.R2_BUCKET_NAME, Key=key, Body=content, ContentType=content_type,
    )
    return key


async def get_download_url(storage_key: str, filename: Optional[str] = None, expires_in: int = 3600) -> str:
    """Returns a time-limited (default 1 hour) presigned GET URL. Downloads
    are never proxied through this app — the browser fetches directly from
    R2, which is both faster and removes Heroku's dyno bandwidth/timeout
    limits from the picture entirely (the exact problem this phase fixes;
    see the scoping doc's Section 1.2)."""
    client = _get_client()
    params = {"Bucket": settings.R2_BUCKET_NAME, "Key": storage_key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    return await asyncio.to_thread(
        client.generate_presigned_url, "get_object", Params=params, ExpiresIn=expires_in,
    )


async def delete_file(storage_key: str) -> None:
    """For future retention/cleanup jobs (scoping doc Section 3.4/Phase F)
    and user-initiated deletion flows."""
    client = _get_client()
    await asyncio.to_thread(client.delete_object, Bucket=settings.R2_BUCKET_NAME, Key=storage_key)


def sha256_hex(content: bytes) -> str:
    """For StoredFile.checksum — integrity verification and future de-dup."""
    return hashlib.sha256(content).hexdigest()
