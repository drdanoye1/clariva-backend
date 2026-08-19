"""
Engine — Agency-Profile Resolver (Word/PDF Report Generation Development
Specification, CLARIVA-DOCGEN-SPEC-001, Phase 7).

Fully deterministic — no OpenAI calls, so unlike test_tense_validator.py
these tests need no client mocking. Like test_award_engine.py, methods are
async and need a real DB session, so each test wraps its body in
asyncio.run() and uses AsyncSessionLocal directly (the `client` fixture is
still requested to trigger create_tables() via the app's lifespan before any
test runs). Every agency_code used is a fresh, uuid-suffixed value (never a
real code like "NSF") so tests never collide with each other or with a real
seeded profile in the shared, run-persistent test DB — same discipline as
test_award_engine.py's `_id()` helper — EXCEPT the handful of tests that
specifically need a real hardcoded-default agency ("NSF", "DOD") to assert
the fallback-to-hardcoded-dict behavior. Those are ordered deliberately
top-of-file, before test_seed_defaults_* (which activates real DB rows for
every known agency, including NSF) — pytest runs a single file's tests in
top-to-bottom definition order by default, so this ordering is stable as
long as no test above test_seed_defaults_* creates an active row for NSF/DOD.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from database import AsyncSessionLocal
from engines.agency_profile_engine import AgencyProfileEngine
from models.db_models import AgencyProfile


def _run(coro):
    return asyncio.run(coro)


def _agency(prefix: str = "TESTAGENCY") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}".upper()


@pytest.fixture()
def engine():
    return AgencyProfileEngine()


# ── resolve() — falls back to hardcoded defaults when no DB row exists ─────────

def test_resolve_falls_back_to_hardcoded_defaults_for_known_agency(client, engine):
    """NSF has hardcoded AGENCY_GUIDANCE/AGENCY_SECTION_LIMITS/AGENCY_TOTAL_LIMITS
    entries and (in a fresh test DB) no AgencyProfile row — resolve() should
    return the hardcoded values with version=None."""
    async def _body():
        async with AsyncSessionLocal() as db:
            return await engine.resolve(db, "NSF")

    resolved = _run(_body())
    assert resolved.agency_code == "NSF"
    assert resolved.version is None
    assert "INTELLECTUAL MERIT" in resolved.guidance_text  # from the real AGENCY_GUIDANCE["NSF"] text
    assert resolved.section_limits.get("project_summary") == 1
    assert resolved.total_page_limit == 25


def test_resolve_unknown_agency_returns_empty_generic_defaults(client, engine):
    """An agency code with no hardcoded entry and no DB row should resolve
    to empty/generic defaults, never raise."""
    async def _body():
        async with AsyncSessionLocal() as db:
            return await engine.resolve(db, _agency())

    resolved = _run(_body())
    assert resolved.version is None
    assert resolved.guidance_text == ""
    assert resolved.section_limits == {}
    assert resolved.total_page_limit == 25  # global fallback


# ── create_version() / activate_version() — append-only, one active row ────────

def test_create_version_defaults_to_inactive(client, engine):
    agency = _agency()

    async def _body():
        async with AsyncSessionLocal() as db:
            row = await engine.create_version(
                db, agency_code=agency, guidance_text="Custom guidance.",
                section_limits={"technical_merit": 9}, total_page_limit=30,
                notes="test", created_by_user_id=None, activate=False,
            )
            await db.commit()
            return row

    row = _run(_body())
    assert row.version == 1
    assert row.is_active is False


def test_resolve_ignores_inactive_version_still_uses_hardcoded_default(client, engine):
    """A DB row exists but isn't active — resolve() must not pick it up."""
    agency = _agency()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_version(
                db, agency_code=agency, guidance_text="Should NOT be used.",
                section_limits=None, total_page_limit=None,
                notes=None, created_by_user_id=None, activate=False,
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.resolve(db, agency)

    resolved = _run(_body())
    assert resolved.version is None
    assert resolved.guidance_text == ""  # unknown agency, no hardcoded default either


def test_create_version_with_activate_true_makes_it_resolvable(client, engine):
    agency = _agency()

    async def _body():
        async with AsyncSessionLocal() as db:
            row = await engine.create_version(
                db, agency_code=agency, guidance_text="Live guidance text.",
                section_limits={"team": 3}, total_page_limit=18,
                notes=None, created_by_user_id=None, activate=True,
            )
            await db.commit()
            return row

    row = _run(_body())
    assert row.is_active is True

    async def _resolve():
        async with AsyncSessionLocal() as db:
            return await engine.resolve(db, agency)

    resolved = _run(_resolve())
    assert resolved.version == 1
    assert resolved.guidance_text == "Live guidance text."
    assert resolved.section_limits == {"team": 3}
    assert resolved.total_page_limit == 18


def test_resolve_per_field_fallback_when_active_row_has_null_fields(client, engine):
    """A version can override just guidance_text and leave section_limits/
    total_page_limit null — resolve() should still return sane values for
    those (falling back per-field), not crash or return None."""
    agency = "DOD"  # has real hardcoded section_limits/total_limit to fall back to

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_version(
                db, agency_code=agency, guidance_text="Only override the guidance text.",
                section_limits=None, total_page_limit=None,
                notes=None, created_by_user_id=None, activate=True,
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.resolve(db, agency)

    resolved = _run(_body())
    assert resolved.version == 1
    assert resolved.guidance_text == "Only override the guidance text."
    # Falls back to the real hardcoded DOD limits since the row's were null:
    assert resolved.section_limits.get("technical_merit") == 12
    assert resolved.total_page_limit == 20


def test_create_second_version_does_not_mutate_first(client, engine):
    agency = _agency()

    async def _body():
        async with AsyncSessionLocal() as db:
            v1 = await engine.create_version(
                db, agency_code=agency, guidance_text="v1", section_limits=None,
                total_page_limit=None, notes=None, created_by_user_id=None, activate=True,
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            v2 = await engine.create_version(
                db, agency_code=agency, guidance_text="v2", section_limits=None,
                total_page_limit=None, notes=None, created_by_user_id=None, activate=False,
            )
            await db.commit()
        return v1, v2

    v1, v2 = _run(_body())
    assert v1.version == 1
    assert v2.version == 2
    assert v2.is_active is False  # v1 stays active — v2 wasn't activated


def test_activate_version_flips_previous_active_off(client, engine):
    agency = _agency()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_version(
                db, agency_code=agency, guidance_text="v1", section_limits=None,
                total_page_limit=None, notes=None, created_by_user_id=None, activate=True,
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.create_version(
                db, agency_code=agency, guidance_text="v2", section_limits=None,
                total_page_limit=None, notes=None, created_by_user_id=None, activate=False,
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            activated = await engine.activate_version(db, agency, 2)
            await db.commit()
            return activated

    activated = _run(_body())
    assert activated.version == 2
    assert activated.is_active is True

    async def _check_v1():
        async with AsyncSessionLocal() as db:
            versions = await engine.list_versions(db, agency)
            return versions

    versions = _run(_check_v1())
    active_versions = [v for v in versions if v.is_active]
    assert len(active_versions) == 1
    assert active_versions[0].version == 2


def test_activate_nonexistent_version_raises_404(client, engine):
    agency = _agency()

    async def _body():
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await engine.activate_version(db, agency, 99)
            return exc_info.value.status_code

    status_code = _run(_body())
    assert status_code == 404


def test_list_versions_orders_newest_first(client, engine):
    agency = _agency()

    async def _body():
        async with AsyncSessionLocal() as db:
            await engine.create_version(
                db, agency_code=agency, guidance_text="v1", section_limits=None,
                total_page_limit=None, notes=None, created_by_user_id=None, activate=False,
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            await engine.create_version(
                db, agency_code=agency, guidance_text="v2", section_limits=None,
                total_page_limit=None, notes=None, created_by_user_id=None, activate=False,
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            return await engine.list_versions(db, agency)

    versions = _run(_body())
    assert [v.version for v in versions] == [2, 1]


# ── seed_defaults() — idempotent, never overwrites an existing row ─────────────

def test_seed_defaults_creates_and_activates_a_row_for_known_agencies(client, engine):
    async def _body():
        async with AsyncSessionLocal() as db:
            created = await engine.seed_defaults(db, created_by_user_id=None)
            await db.commit()
            return created

    created = _run(_body())
    codes = {row.agency_code for row in created}
    assert "NSF" in codes
    assert all(row.version == 1 and row.is_active for row in created)


def test_seed_defaults_is_idempotent_and_never_overwrites(client, engine):
    """First call seeds NSF (among others). A manual edit is then made
    (a new active v2). A second seed_defaults() call must NOT create
    anything new for NSF — it already has a row."""
    async def _first_seed():
        async with AsyncSessionLocal() as db:
            await engine.seed_defaults(db, created_by_user_id=None)
            await db.commit()

    _run(_first_seed())

    async def _manual_edit():
        async with AsyncSessionLocal() as db:
            row = await engine.create_version(
                db, agency_code="NSF", guidance_text="Manually customized NSF guidance.",
                section_limits=None, total_page_limit=None, notes="manual edit",
                created_by_user_id=None, activate=True,
            )
            await db.commit()
            return row

    manual_row = _run(_manual_edit())
    assert manual_row.version == 2

    async def _second_seed():
        async with AsyncSessionLocal() as db:
            created = await engine.seed_defaults(db, created_by_user_id=None)
            await db.commit()
            return created

    second_created = _run(_second_seed())
    assert "NSF" not in {row.agency_code for row in second_created}  # untouched

    async def _resolve_nsf():
        async with AsyncSessionLocal() as db:
            return await engine.resolve(db, "NSF")

    resolved = _run(_resolve_nsf())
    assert resolved.version == 2  # the manual edit is still active, not reverted
    assert resolved.guidance_text == "Manually customized NSF guidance."
