"""
Engine — Agency-Profile Resolver (Word/PDF Report Generation Development
Specification, CLARIVA-DOCGEN-SPEC-001, Phase 7).

Before this engine, per-agency prompt guidance (proposal_generator.py's
AGENCY_GUIDANCE dict) and page-limit rules (compliance_engine.py's
AGENCY_SECTION_LIMITS / AGENCY_TOTAL_LIMITS dicts) were hardcoded Python —
any update (an agency changing evaluation criteria, a new fiscal year's page
limits) required a code deploy. AgencyProfile (models/db_models.py) makes
these admin-editable at runtime; this engine resolves the effective profile
for a given agency by preferring an active DB row and falling back to the
hardcoded dicts on a PER-FIELD basis (a version can override just
guidance_text and leave section_limits null, and still get sane defaults
for the fields it didn't touch).

Design notes (same discipline as every other engine in this codebase):
- Every method takes an already-open AsyncSession and only flushes, never
  commits — the caller's request-scoped session commits once.
- No permission checks live here — routers/admin.py gates the write
  endpoints via its existing require_superadmin dependency. resolve() itself
  has no access restriction since it's read-only reference data consumed by
  generation/compliance code, not sensitive.
- Same append-only, "only one is_active=True row per key" versioning
  convention as CommissionRule/ProjectBaseline: create_version() never
  mutates an existing row; activate_version() flips the previous active
  row's is_active off and the target row's on, in the same transaction.
- AGENCY_GUIDANCE / AGENCY_SECTION_LIMITS / AGENCY_TOTAL_LIMITS are imported
  LAZILY, inside methods, not at module level — proposal_generator.py and
  compliance_engine.py do not import this module, so there's no real import
  cycle today, but keeping these as local imports avoids ever creating one
  as those modules evolve, and keeps this module cheap to import from
  routers/admin.py.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import AgencyProfile, new_uuid
from models.schemas import AgencyProfileResolvedOut, AgencyProfileVersionOut


class AgencyProfileEngine:

    # ── Resolution (read path — used by generation/compliance) ─────────────

    async def resolve(self, db: AsyncSession, agency_code: str) -> AgencyProfileResolvedOut:
        """Merge the active DB row (if any) for this agency over the
        hardcoded defaults, per field. Never raises — an unknown agency code
        with no hardcoded entry and no DB row simply resolves to empty/generic
        defaults, matching AGENCY_GUIDANCE.get()/AGENCY_SECTION_LIMITS.get()'s
        existing "no crash on unknown agency" behavior."""
        from engines.proposal_generator import AGENCY_GUIDANCE
        from engines.compliance_engine import ComplianceEngine

        key = (agency_code or "OTHER").upper()
        default_guidance = AGENCY_GUIDANCE.get(agency_code) or AGENCY_GUIDANCE.get(key) or ""
        default_section_limits = (
            ComplianceEngine.AGENCY_SECTION_LIMITS.get(agency_code)
            or ComplianceEngine.AGENCY_SECTION_LIMITS.get(key)
            or {}
        )
        default_total_limit = (
            ComplianceEngine.AGENCY_TOTAL_LIMITS.get(agency_code)
            or ComplianceEngine.AGENCY_TOTAL_LIMITS.get(key)
            or 25
        )

        result = await db.execute(
            select(AgencyProfile).where(
                AgencyProfile.agency_code == key,
                AgencyProfile.is_active == True,  # noqa: E712 — SQLAlchemy comparison, not a Python bool check
            )
        )
        row = result.scalar_one_or_none()

        if row is None:
            return AgencyProfileResolvedOut(
                agency_code=key,
                guidance_text=default_guidance,
                section_limits=default_section_limits,
                total_page_limit=default_total_limit,
                version=None,
            )

        # `is not None`, not `or` — an admin explicitly setting section_limits
        # to {} (meaning "no page limits for this agency") or total_page_limit
        # to 0 must be respected, not silently treated as "unset" the way a
        # truthy check would. Only guidance_text can't hit this edge case in
        # practice (an empty string is never a meaningful override), but the
        # same explicit check is used for consistency and to document intent.
        return AgencyProfileResolvedOut(
            agency_code=key,
            guidance_text=row.guidance_text if row.guidance_text is not None else default_guidance,
            section_limits=row.section_limits if row.section_limits is not None else default_section_limits,
            total_page_limit=row.total_page_limit if row.total_page_limit is not None else default_total_limit,
            version=row.version,
        )

    # ── Admin CRUD (write path — used by routers/admin.py) ─────────────────

    async def list_agencies_summary(self, db: AsyncSession) -> List[AgencyProfileResolvedOut]:
        """One resolved row per agency that has EITHER a hardcoded default OR
        at least one DB-stored version — the admin console's agency picker."""
        from engines.proposal_generator import AGENCY_GUIDANCE
        from engines.compliance_engine import ComplianceEngine

        result = await db.execute(select(AgencyProfile.agency_code).distinct())
        db_codes = {row[0] for row in result.all()}
        known_codes = set(AGENCY_GUIDANCE.keys()) | set(ComplianceEngine.AGENCY_SECTION_LIMITS.keys()) | db_codes
        return [await self.resolve(db, code) for code in sorted(known_codes)]

    async def list_versions(self, db: AsyncSession, agency_code: str) -> List[AgencyProfileVersionOut]:
        key = (agency_code or "OTHER").upper()
        result = await db.execute(
            select(AgencyProfile)
            .where(AgencyProfile.agency_code == key)
            .order_by(AgencyProfile.version.desc())
        )
        return [AgencyProfileVersionOut.model_validate(row) for row in result.scalars().all()]

    async def create_version(
        self,
        db: AsyncSession,
        agency_code: str,
        guidance_text: Optional[str],
        section_limits: Optional[Dict[str, int]],
        total_page_limit: Optional[int],
        notes: Optional[str],
        created_by_user_id: Optional[str],
        activate: bool = False,
    ) -> AgencyProfileVersionOut:
        """Append-only — never mutates an existing row. New version number is
        one past the highest existing version for this agency (or 1 if
        none exist yet)."""
        key = (agency_code or "OTHER").upper()
        result = await db.execute(
            select(AgencyProfile.version)
            .where(AgencyProfile.agency_code == key)
            .order_by(AgencyProfile.version.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
        next_version = (latest or 0) + 1

        row = AgencyProfile(
            id=new_uuid(),
            agency_code=key,
            version=next_version,
            is_active=False,
            guidance_text=guidance_text,
            section_limits=section_limits,
            total_page_limit=total_page_limit,
            notes=notes,
            created_by_user_id=created_by_user_id,
        )
        db.add(row)
        await db.flush()

        if activate:
            await self.activate_version(db, key, next_version)
            await db.refresh(row)

        return AgencyProfileVersionOut.model_validate(row)

    async def activate_version(self, db: AsyncSession, agency_code: str, version: int) -> AgencyProfileVersionOut:
        """Flip the previously-active row's is_active off and the target
        row's on, in the same transaction — mirrors CommissionRule's
        activation pattern. Raises 404 if the target version doesn't exist."""
        key = (agency_code or "OTHER").upper()
        result = await db.execute(
            select(AgencyProfile).where(
                AgencyProfile.agency_code == key,
                AgencyProfile.version == version,
            )
        )
        target = result.scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail=f"No version {version} found for agency {key}")

        await db.execute(
            update(AgencyProfile)
            .where(AgencyProfile.agency_code == key, AgencyProfile.is_active == True)  # noqa: E712
            .values(is_active=False)
        )
        target.is_active = True
        await db.flush()
        await db.refresh(target)
        return AgencyProfileVersionOut.model_validate(target)

    async def seed_defaults(self, db: AsyncSession, created_by_user_id: Optional[str]) -> List[AgencyProfileVersionOut]:
        """Idempotent — for every agency with a hardcoded default that does
        NOT yet have any AgencyProfile row at all, create version 1 (seeded
        verbatim from the hardcoded guidance/limits) and activate it. Agencies
        that already have at least one row (from a prior seed or a manual
        admin edit) are left untouched — this never overwrites existing data.
        Lets an admin start from "DB row per agency, editable" instead of
        "silent fallback to code" without hand-transcribing every dict."""
        from engines.proposal_generator import AGENCY_GUIDANCE
        from engines.compliance_engine import ComplianceEngine

        result = await db.execute(select(AgencyProfile.agency_code).distinct())
        existing_codes = {row[0] for row in result.all()}

        known_codes = sorted(set(AGENCY_GUIDANCE.keys()) | set(ComplianceEngine.AGENCY_SECTION_LIMITS.keys()))
        created: List[AgencyProfileVersionOut] = []
        for code in known_codes:
            key = code.upper()
            if key in existing_codes:
                continue
            created.append(await self.create_version(
                db, agency_code=key,
                guidance_text=AGENCY_GUIDANCE.get(code) or AGENCY_GUIDANCE.get(key),
                section_limits=ComplianceEngine.AGENCY_SECTION_LIMITS.get(code) or ComplianceEngine.AGENCY_SECTION_LIMITS.get(key),
                total_page_limit=ComplianceEngine.AGENCY_TOTAL_LIMITS.get(code) or ComplianceEngine.AGENCY_TOTAL_LIMITS.get(key),
                notes="Seeded from platform defaults.",
                created_by_user_id=created_by_user_id,
                activate=True,
            ))
        return created
