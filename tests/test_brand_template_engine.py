"""
Engine 30 — Customer Co-Brand / White-Label Template Registry (Word/PDF
Report Generation Development Specification, CLARIVA-DOCGEN-SPEC-001,
Phase 13).

Same testing discipline as test_agency_profile_engine.py: fully
deterministic (no OpenAI calls), methods are async and need a real DB
session, so each test wraps its body in asyncio.run() and uses
AsyncSessionLocal directly (the `client` fixture is still requested to
trigger create_tables() via the app's lifespan before any test runs).
Every template_key/org_id used is a fresh, uuid-suffixed value so tests
never collide with each other or with the "clariva_standard" system
default.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from database import AsyncSessionLocal
from engines.brand_template_engine import BrandTemplateEngine, SYSTEM_DEFAULT_TEMPLATE_KEY
from models.db_models import Organization


def _run(coro):
    return asyncio.run(coro)


def _key(prefix: str = "testtemplate") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def engine():
    return BrandTemplateEngine()


async def _make_org(db, *, logo_url=None, primary_color=None, brand_name=None) -> str:
    org = Organization(
        id=str(uuid.uuid4()), name=f"Test Org {uuid.uuid4().hex[:6]}",
        created_by=str(uuid.uuid4()), logo_url=logo_url, primary_color=primary_color,
        brand_name=brand_name,
    )
    db.add(org)
    await db.flush()
    return org.id


# ── resolve() — falls back to hardcoded/org defaults when no DB row exists ──

def test_resolve_with_no_key_no_org_returns_hardcoded_system_default(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            return await engine.resolve(db, None, None)

    resolved = _run(_body())
    assert resolved.template_key == SYSTEM_DEFAULT_TEMPLATE_KEY
    assert resolved.version is None
    assert resolved.name == "Clariva Standard"
    assert resolved.logo_url is None
    assert resolved.hide_clariva_branding is False


def test_resolve_unknown_key_with_org_falls_back_to_org_branding_columns(client, engine):
    """No DocumentBrandTemplate row exists for this key at all — resolve()
    should still surface the org's own Phase-6 logo_url/primary_color
    columns rather than returning nothing, per the class docstring's
    fallback chain."""
    key = _key()

    async def _body():
        async with AsyncSessionLocal() as db:
            org_id = await _make_org(db, logo_url="https://org.example/logo.png",
                                      primary_color="#112233", brand_name="Acme Org")
            await db.commit()
            async with AsyncSessionLocal() as db2:
                return await engine.resolve(db2, key, org_id)

    resolved = _run(_body())
    assert resolved.version is None
    assert resolved.logo_url == "https://org.example/logo.png"
    assert resolved.primary_color == "#112233"
    assert resolved.name == "Acme Org"


# ── create_version() / activate_version() — append-only, org-scoped only ───

def test_create_version_requires_org_id(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.create_version(
                    db, template_key=_key(), org_id=None, name="No org",
                )
            return exc_info.value.status_code

    assert _run(_body()) == 400


def test_create_version_defaults_to_inactive(client, engine):
    key = _key()

    async def _body():
        async with AsyncSessionLocal() as db:
            org_id = await _make_org(db)
            row = await engine.create_version(
                db, template_key=key, org_id=org_id, name="Acme Co-Brand",
                logo_url="https://row.example/logo.png", activate=False,
            )
            await db.commit()
            return row

    row = _run(_body())
    assert row.version == 1
    assert row.is_active is False


def test_create_version_with_activate_true_makes_it_resolvable(client, engine):
    key = _key()

    async def _body():
        async with AsyncSessionLocal() as db:
            org_id = await _make_org(db)
            await engine.create_version(
                db, template_key=key, org_id=org_id, name="Acme Co-Brand",
                logo_url="https://row.example/logo.png", primary_color="#445566",
                header_text="Powered by Acme", footer_text="Confidential",
                activate=True,
            )
            await db.commit()
            return org_id

    org_id = _run(_body())

    async def _resolve():
        async with AsyncSessionLocal() as db:
            return await engine.resolve(db, key, org_id)

    resolved = _run(_resolve())
    assert resolved.version == 1
    assert resolved.name == "Acme Co-Brand"
    assert resolved.logo_url == "https://row.example/logo.png"
    assert resolved.primary_color == "#445566"
    assert resolved.header_text == "Powered by Acme"
    assert resolved.footer_text == "Confidential"


def test_row_field_overrides_org_column_when_both_set(client, engine):
    """The template row's own logo/color, when set, wins over the org's
    Phase-6 branding columns — a specific template variant can differ from
    the org's default identity (e.g. a partner co-brand variant)."""
    key = _key()

    async def _body():
        async with AsyncSessionLocal() as db:
            org_id = await _make_org(db, logo_url="https://org.example/org-logo.png",
                                      primary_color="#000000")
            await engine.create_version(
                db, template_key=key, org_id=org_id, name="Partner Variant",
                logo_url="https://row.example/partner-logo.png", primary_color="#ffffff",
                activate=True,
            )
            await db.commit()
            return org_id

    org_id = _run(_body())

    async def _resolve():
        async with AsyncSessionLocal() as db:
            return await engine.resolve(db, key, org_id)

    resolved = _run(_resolve())
    assert resolved.logo_url == "https://row.example/partner-logo.png"
    assert resolved.primary_color == "#ffffff"


def test_second_org_cannot_resolve_first_orgs_private_template(client, engine):
    """SECURITY — the resolve() org-isolation invariant: an active template
    row owned by org A must never be returned when org B (or no org at all)
    asks for the same template_key."""
    key = _key()

    async def _body():
        async with AsyncSessionLocal() as db:
            org_a = await _make_org(db)
            org_b = await _make_org(db)
            await engine.create_version(
                db, template_key=key, org_id=org_a, name="Org A Private",
                logo_url="https://a.example/logo.png", activate=True,
            )
            await db.commit()
            return org_a, org_b

    org_a, org_b = _run(_body())

    async def _resolve_as_b():
        async with AsyncSessionLocal() as db:
            return await engine.resolve(db, key, org_b)

    resolved_for_b = _run(_resolve_as_b())
    assert resolved_for_b.version is None  # org A's row never leaked to org B
    assert resolved_for_b.logo_url is None

    async def _resolve_no_org():
        async with AsyncSessionLocal() as db:
            return await engine.resolve(db, key, None)

    resolved_no_org = _run(_resolve_no_org())
    assert resolved_no_org.version is None


def test_activate_version_flips_previous_active_off(client, engine):
    key = _key()

    async def _body():
        async with AsyncSessionLocal() as db:
            org_id = await _make_org(db)
            v1 = await engine.create_version(db, template_key=key, org_id=org_id, name="v1", activate=True)
            await db.commit()
            return org_id, v1.id

    org_id, v1_id = _run(_body())

    async def _second():
        async with AsyncSessionLocal() as db:
            v2 = await engine.create_version(db, template_key=key, org_id=org_id, name="v2", activate=False)
            await db.commit()
            return v2.id

    v2_id = _run(_second())

    async def _activate():
        async with AsyncSessionLocal() as db:
            activated = await engine.activate_version(db, v2_id, org_id)
            await db.commit()
            return activated

    activated = _run(_activate())
    assert activated.version == 2
    assert activated.is_active is True

    async def _list():
        async with AsyncSessionLocal() as db:
            return await engine.list_versions(db, key, org_id)

    versions = _run(_list())
    active_versions = [v for v in versions if v.is_active]
    assert len(active_versions) == 1
    assert active_versions[0].version == 2


def test_activate_version_404s_for_wrong_org(client, engine):
    """SECURITY — activate_version() must 404 (not 403) when the target row
    belongs to a different org, so a caller can never distinguish
    "doesn't exist" from "exists but isn't yours"."""
    key = _key()

    async def _body():
        async with AsyncSessionLocal() as db:
            org_a = await _make_org(db)
            org_b = await _make_org(db)
            row = await engine.create_version(db, template_key=key, org_id=org_a, name="Org A", activate=False)
            await db.commit()
            return row.id, org_b

    row_id, org_b = _run(_body())

    async def _activate_as_b():
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.activate_version(db, row_id, org_b)
            return exc_info.value.status_code

    assert _run(_activate_as_b()) == 404


def test_activate_nonexistent_version_raises_404(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            org_id = await _make_org(db)
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.activate_version(db, str(uuid.uuid4()), org_id)
            return exc_info.value.status_code

    assert _run(_body()) == 404


# ── set_default_for_org() — validates before accepting ──────────────────────

def test_set_default_for_org_accepts_system_default_key(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            org_id = await _make_org(db)
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.set_default_for_org(db, org_id, SYSTEM_DEFAULT_TEMPLATE_KEY)
            await db.commit()
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Organization).where(Organization.id == org_id))
            return result.scalar_one().default_brand_template_key

    assert _run(_body()) == SYSTEM_DEFAULT_TEMPLATE_KEY


def test_set_default_for_org_rejects_unknown_key(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            org_id = await _make_org(db)
            await db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.set_default_for_org(db, org_id, _key("nonexistent"))
            return exc_info.value.status_code

    assert _run(_body()) == 400


def test_set_default_for_org_accepts_orgs_own_template(client, engine):
    key = _key()

    async def _body():
        async with AsyncSessionLocal() as db:
            org_id = await _make_org(db)
            await engine.create_version(db, template_key=key, org_id=org_id, name="Own", activate=True)
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.set_default_for_org(db, org_id, key)
            await db.commit()
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Organization).where(Organization.id == org_id))
            return result.scalar_one().default_brand_template_key

    assert _run(_body()) == key


def test_set_default_for_org_none_clears_override(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            org_id = await _make_org(db)
            org_result = await db.execute(
                __import__("sqlalchemy").select(Organization).where(Organization.id == org_id)
            )
            org_result.scalar_one().default_brand_template_key = SYSTEM_DEFAULT_TEMPLATE_KEY
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.set_default_for_org(db, org_id, None)
            await db.commit()
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Organization).where(Organization.id == org_id))
            return result.scalar_one().default_brand_template_key

    assert _run(_body()) is None


# ── seed_defaults() — idempotent, never overwrites an existing row ─────────

def test_seed_defaults_creates_and_activates_system_template(client, engine):
    async def _first():
        async with AsyncSessionLocal() as db:
            result = await engine.seed_defaults(db, created_by_user_id=None)
            await db.commit()
            return result

    result = _run(_first())
    # Either this test created it, or an earlier test-session run already
    # did (seed_defaults is a shared-DB-safe no-op the second time) — both
    # are valid outcomes for an idempotent seed.
    if result is not None:
        assert result.template_key == SYSTEM_DEFAULT_TEMPLATE_KEY
        assert result.version == 1
        assert result.is_active is True

    async def _second():
        async with AsyncSessionLocal() as db:
            return await engine.seed_defaults(db, created_by_user_id=None)

    assert _run(_second()) is None  # second call is always a no-op
