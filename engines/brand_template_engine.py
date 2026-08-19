"""
Engine 30 — Customer Co-Brand / White-Label Template Registry (Word/PDF
Report Generation Development Specification, CLARIVA-DOCGEN-SPEC-001, Phase
13 — "§14 P2: Customer co-brand/white-label template registry — Supports
Clariva Custom Solutions and enterprise deployments").

Gives the document JSON schema's `brandingProfile` field (§3, example value
"clariva_standard") a real backing implementation. Before this engine, that
field had zero references anywhere in the codebase — every exported
document rendered identically regardless of what `brandingProfile` an
export call specified. models/db_models.py::DocumentBrandTemplate makes
per-org branding admin-editable at runtime; this engine resolves the
effective branding for a (template_key, org_id) pair the same way
AgencyProfileEngine resolves per-agency guidance: prefer an active DB row,
fall back to sane defaults on a PER-FIELD basis.

Design notes (same discipline as every other engine in this codebase):
- Every method takes an already-open AsyncSession and only flushes, never
  commits — the caller's request-scoped router session commits once.
- No permission checks live here — routers/organizations.py gates the
  write endpoints via the existing `manage_branding` RBAC permission
  (owner-only). resolve() itself has no access restriction beyond its own
  org-scoping query design (see below) since it's the read path every
  export call needs, not a sensitive admin surface.
- Same append-only, "only one is_active=True row per (template_key, org_id)
  key at a time" versioning convention as AgencyProfile: create_version()
  never mutates an existing row; activate_version() flips the previous
  active row's is_active off and the target row's on, in the same
  transaction.

SECURITY INVARIANT — org isolation:
resolve() runs two SEPARATE queries rather than one query with an OR
filter: an org-scoped query (`org_id == org_id`) first, and only if that
finds nothing, a system-scoped query (`org_id IS NULL`) second. This
two-step structure makes it structurally impossible for org A's private
white-label template to ever be returned for org B's request — there is no
single query whose filter could be mis-written to leak across orgs, unlike
a one-query `WHERE org_id = :org_id OR org_id IS NULL` where a caller
passing the wrong org_id could still silently succeed. activate_version()
enforces the same boundary: it 404s (not 403 — this codebase's standing
convention, e.g. test_get_figure_set_404s_for_another_proposal, to avoid
leaking whether a resource exists to a caller who shouldn't see it) when
the target row's org_id doesn't match the caller's org_id.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import DocumentBrandTemplate, Organization, new_uuid
from models.schemas import DocumentBrandTemplateOut, DocumentBrandTemplateResolvedOut

# Hardcoded system baseline — used when no DocumentBrandTemplate row (system
# OR org) exists for a key at all, mirroring AgencyProfileEngine's
# AGENCY_GUIDANCE/AGENCY_SECTION_LIMITS fallback dicts. Kept as a small
# constant here (rather than in a separate module) since there is exactly
# one hardcoded default, not a per-agency-style lookup table.
SYSTEM_DEFAULT_TEMPLATE_KEY = "clariva_standard"
_SYSTEM_DEFAULT_NAME = "Clariva Standard"


class BrandTemplateEngine:

    # ── Resolution (read path — used by document_output.py at export time) ──

    async def resolve(
        self, db: AsyncSession, template_key: Optional[str] = None, org_id: Optional[str] = None,
    ) -> DocumentBrandTemplateResolvedOut:
        """Merge the active DB row (org-scoped, else system-scoped) over the
        owning org's own branding columns, over the hardcoded system
        default — per field. Never raises: an org_id with no matching row
        at all, or a template_key nobody has ever created, both resolve to
        the hardcoded clariva_standard baseline (with org-column branding
        applied if org_id was given), matching this codebase's "resolvers
        never crash on unknown keys" convention (see AgencyProfileEngine)."""
        key = (template_key or SYSTEM_DEFAULT_TEMPLATE_KEY).strip() or SYSTEM_DEFAULT_TEMPLATE_KEY

        org: Optional[Organization] = None
        if org_id:
            org_result = await db.execute(select(Organization).where(Organization.id == org_id))
            org = org_result.scalar_one_or_none()

        row: Optional[DocumentBrandTemplate] = None
        # Step 1 — org-scoped query. Only ever matches a row this exact org
        # owns (see module docstring's security invariant).
        if org_id:
            org_result = await db.execute(
                select(DocumentBrandTemplate).where(
                    DocumentBrandTemplate.template_key == key,
                    DocumentBrandTemplate.org_id == org_id,
                    DocumentBrandTemplate.is_active == True,  # noqa: E712
                )
            )
            row = org_result.scalar_one_or_none()

        # Step 2 — system-scoped fallback. A completely separate query, not
        # an OR clause on the query above — see module docstring.
        if row is None:
            sys_result = await db.execute(
                select(DocumentBrandTemplate).where(
                    DocumentBrandTemplate.template_key == key,
                    DocumentBrandTemplate.org_id.is_(None),
                    DocumentBrandTemplate.is_active == True,  # noqa: E712
                )
            )
            row = sys_result.scalar_one_or_none()

        # Org-column fallback values (Phase 6 branding columns) — only used
        # for logo_url/primary_color, which are the two fields Organization
        # actually has. `is not None`/truthy-string checks throughout: an
        # explicit empty override isn't meaningfully distinguishable for
        # these text fields, so falling through on falsy is fine here
        # (unlike AgencyProfileResolvedOut's section_limits={} case, which
        # has a real "explicitly no limits" meaning).
        org_logo = org.logo_url if org else None
        org_color = org.primary_color if org else None
        org_name = org.brand_name if org else None

        if row is None:
            return DocumentBrandTemplateResolvedOut(
                template_key=key,
                name=org_name or _SYSTEM_DEFAULT_NAME,
                logo_url=org_logo,
                primary_color=org_color,
                header_text=None,
                footer_text=None,
                hide_clariva_branding=False,
                version=None,
            )

        return DocumentBrandTemplateResolvedOut(
            template_key=key,
            name=row.name or org_name or _SYSTEM_DEFAULT_NAME,
            logo_url=row.logo_url or org_logo,
            primary_color=row.primary_color or org_color,
            header_text=row.header_text,
            footer_text=row.footer_text,
            hide_clariva_branding=bool(row.hide_clariva_branding),
            version=row.version,
        )

    # ── Admin CRUD (write path — used by routers/organizations.py) ─────────

    async def list_active_templates_for_org(self, db: AsyncSession, org_id: str) -> List[DocumentBrandTemplateOut]:
        """Every active template visible to this org: its own private
        white-label variants plus every Clariva system template — the
        org's template picker."""
        result = await db.execute(
            select(DocumentBrandTemplate).where(
                DocumentBrandTemplate.is_active == True,  # noqa: E712
                (DocumentBrandTemplate.org_id == org_id) | (DocumentBrandTemplate.org_id.is_(None)),
            ).order_by(DocumentBrandTemplate.org_id.is_(None).desc(), DocumentBrandTemplate.template_key)
        )
        return [DocumentBrandTemplateOut.model_validate(row) for row in result.scalars().all()]

    async def list_versions(
        self, db: AsyncSession, template_key: str, org_id: Optional[str],
    ) -> List[DocumentBrandTemplateOut]:
        """Versions for exactly this (template_key, org_id) pair — org_id
        None lists a SYSTEM template's history, never another org's."""
        result = await db.execute(
            select(DocumentBrandTemplate)
            .where(
                DocumentBrandTemplate.template_key == template_key,
                DocumentBrandTemplate.org_id == org_id if org_id else DocumentBrandTemplate.org_id.is_(None),
            )
            .order_by(DocumentBrandTemplate.version.desc())
        )
        return [DocumentBrandTemplateOut.model_validate(row) for row in result.scalars().all()]

    async def create_version(
        self,
        db: AsyncSession,
        *,
        template_key: str,
        org_id: str,
        name: str,
        logo_url: Optional[str] = None,
        primary_color: Optional[str] = None,
        header_text: Optional[str] = None,
        footer_text: Optional[str] = None,
        hide_clariva_branding: bool = False,
        notes: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        activate: bool = False,
    ) -> DocumentBrandTemplateOut:
        """Append-only — never mutates an existing row. Org-scoped only:
        this path never creates a system template (org_id is required, not
        Optional) — Clariva's own "clariva_standard" system template is
        seeded via seed_defaults() instead, not created through this
        customer-facing path. New version number is one past the highest
        existing version for this exact (template_key, org_id) pair (or 1
        if none exist yet)."""
        if not org_id:
            raise HTTPException(status_code=400, detail="org_id is required to create a brand template version")

        result = await db.execute(
            select(DocumentBrandTemplate.version)
            .where(
                DocumentBrandTemplate.template_key == template_key,
                DocumentBrandTemplate.org_id == org_id,
            )
            .order_by(DocumentBrandTemplate.version.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
        next_version = (latest or 0) + 1

        row = DocumentBrandTemplate(
            id=new_uuid(),
            template_key=template_key,
            org_id=org_id,
            version=next_version,
            is_active=False,
            name=name,
            logo_url=logo_url,
            primary_color=primary_color,
            header_text=header_text,
            footer_text=footer_text,
            hide_clariva_branding=hide_clariva_branding,
            notes=notes,
            created_by_user_id=created_by_user_id,
        )
        db.add(row)
        await db.flush()

        if activate:
            await self.activate_version(db, row.id, org_id)
            await db.refresh(row)

        return DocumentBrandTemplateOut.model_validate(row)

    async def activate_version(self, db: AsyncSession, template_id: str, org_id: str) -> DocumentBrandTemplateOut:
        """Flip the previously-active row's is_active off and the target
        row's on, in the same transaction — mirrors AgencyProfile's
        activation pattern. 404s (not 403) if the target doesn't exist OR
        belongs to a different org — see module docstring's security
        invariant; callers can never distinguish "doesn't exist" from
        "exists but isn't yours" from this response."""
        result = await db.execute(
            select(DocumentBrandTemplate).where(
                DocumentBrandTemplate.id == template_id,
                DocumentBrandTemplate.org_id == org_id,
            )
        )
        target = result.scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail="Brand template version not found")

        await db.execute(
            update(DocumentBrandTemplate)
            .where(
                DocumentBrandTemplate.template_key == target.template_key,
                DocumentBrandTemplate.org_id == org_id,
                DocumentBrandTemplate.is_active == True,  # noqa: E712
            )
            .values(is_active=False)
        )
        target.is_active = True
        await db.flush()
        await db.refresh(target)
        return DocumentBrandTemplateOut.model_validate(target)

    async def set_default_for_org(self, db: AsyncSession, org_id: str, template_key: Optional[str]) -> None:
        """Sets Organization.default_brand_template_key. Validates the key
        actually resolves to something (a real system template or a real
        org-owned template) before accepting — a typo'd key would otherwise
        silently degrade every future export for this org down to the
        hardcoded baseline with no error surfaced anywhere. None always
        validates (it means "clear the override, use the system
        default")."""
        if template_key:
            resolved = await self.resolve(db, template_key, org_id)
            valid = resolved.version is not None or template_key == SYSTEM_DEFAULT_TEMPLATE_KEY
            if not valid:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{template_key}' does not match any brand template visible to this organization",
                )

        result = await db.execute(select(Organization).where(Organization.id == org_id))
        org = result.scalar_one_or_none()
        if org is None:
            raise HTTPException(status_code=404, detail="Organization not found")
        org.default_brand_template_key = template_key
        await db.flush()

    async def seed_defaults(self, db: AsyncSession, created_by_user_id: Optional[str]) -> Optional[DocumentBrandTemplateOut]:
        """Idempotent — creates and activates the "clariva_standard" SYSTEM
        template (org_id null) if it doesn't already exist. Returns None if
        it already exists (no-op). NOT auto-run at boot: resolve() already
        degrades gracefully to the hardcoded baseline with zero DB rows, so
        this is purely an optional convenience for an admin who wants the
        system default to be an editable DB row instead of hardcoded
        Python — same optional-seeding posture as AgencyProfileEngine.
        seed_defaults()."""
        result = await db.execute(
            select(DocumentBrandTemplate).where(
                DocumentBrandTemplate.template_key == SYSTEM_DEFAULT_TEMPLATE_KEY,
                DocumentBrandTemplate.org_id.is_(None),
            )
        )
        if result.scalar_one_or_none() is not None:
            return None

        row = DocumentBrandTemplate(
            id=new_uuid(),
            template_key=SYSTEM_DEFAULT_TEMPLATE_KEY,
            org_id=None,
            version=1,
            is_active=True,
            name=_SYSTEM_DEFAULT_NAME,
            hide_clariva_branding=False,
            notes="Seeded platform default.",
            created_by_user_id=created_by_user_id,
        )
        db.add(row)
        await db.flush()
        return DocumentBrandTemplateOut.model_validate(row)
