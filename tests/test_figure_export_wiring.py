"""
Export-side wiring for the AI Figure Generation Engine (engines/
figure_engine.py, Phases 9-12) — the fix for a real gap discovered after
Phase 14 shipped: nothing in engines/document_output.py ever consumed
this engine's output. The export UI's "Generate AI Figures" checkbox
only ever drove a much older, unrelated marker-scan mechanism
(_generate_figures() — regex-matches literal [FIGURE N: ...] text and
calls DALL-E directly); the elaborate Figure 1/Figure 2/QA/approval
system was fully built and tested but had no consumer anywhere.

This file covers the new pieces:
  1. FigureEngine.get_approved_figures_by_section() — the query that
     finds what's actually eligible to appear in an export (§22: only
     human-APPROVED figures, never pending/rejected/needs_regeneration).
  2. figures.py's download_url resolution on every figure response.

Same "test the model/engine directly via AsyncSessionLocal" approach as
test_figure_models.py for the engine-level pieces (no router involved),
and the real-HTTP client pattern (see test_figures_api.py) for the
API-level download_url check.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from database import AsyncSessionLocal
from engines.figure_engine import FigureEngine
from models.db_models import ProposalFigure, ProposalFigureSet, new_uuid


def _run(coro):
    return asyncio.run(coro)


def _id(prefix: str) -> str:
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


def test_get_approved_figures_by_section_filters_and_groups_and_orders(client):
    """Two sections, each with a figure set carrying a mix of approval
    statuses — only "approved" rows should come back, grouped by
    source_section, ordered by figure_number within each group."""
    proposal_id = _id("proposal")

    async def _seed():
        async with AsyncSessionLocal() as db:
            # Section A: figure 2 approved, figure 1 still pending — only
            # figure 2 should surface, proving the filter is per-figure,
            # not per-figure-set.
            fset_a = ProposalFigureSet(id=new_uuid(), proposal_id=proposal_id, source_section="technical_approach")
            db.add(fset_a)
            await db.flush()
            db.add(ProposalFigure(
                id=new_uuid(), figure_set_id=fset_a.id, figure_number=1,
                figure_type="functional_workflow", approval_status="pending",
                caption="Figure 1. Pending.",
            ))
            db.add(ProposalFigure(
                id=new_uuid(), figure_set_id=fset_a.id, figure_number=2,
                figure_type="technical_illustration", approval_status="approved",
                caption="Figure 2. Approved.",
            ))

            # Section B: both figures approved, inserted out of order —
            # the grouped result must still come back 1, then 2.
            fset_b = ProposalFigureSet(id=new_uuid(), proposal_id=proposal_id, source_section="commercialization")
            db.add(fset_b)
            await db.flush()
            db.add(ProposalFigure(
                id=new_uuid(), figure_set_id=fset_b.id, figure_number=2,
                figure_type="technical_illustration", approval_status="approved",
                caption="Figure 2. Approved.",
            ))
            db.add(ProposalFigure(
                id=new_uuid(), figure_set_id=fset_b.id, figure_number=1,
                figure_type="functional_workflow", approval_status="approved",
                caption="Figure 1. Approved.",
            ))

            # Section C: figure rejected — the whole section should be
            # absent from the result (no approved figures at all).
            fset_c = ProposalFigureSet(id=new_uuid(), proposal_id=proposal_id, source_section="budget_narrative")
            db.add(fset_c)
            await db.flush()
            db.add(ProposalFigure(
                id=new_uuid(), figure_set_id=fset_c.id, figure_number=1,
                figure_type="functional_workflow", approval_status="rejected",
                caption="Figure 1. Rejected.",
            ))
            await db.commit()

    _run(_seed())

    async def _query():
        async with AsyncSessionLocal() as db:
            return await FigureEngine().get_approved_figures_by_section(db, proposal_id)

    by_section = _run(_query())

    assert set(by_section.keys()) == {"technical_approach", "commercialization"}
    assert "budget_narrative" not in by_section

    assert [f.figure_number for f in by_section["technical_approach"]] == [2]
    assert by_section["technical_approach"][0].caption == "Figure 2. Approved."

    assert [f.figure_number for f in by_section["commercialization"]] == [1, 2]


def test_get_approved_figures_by_section_returns_empty_for_proposal_with_no_figures(client):
    proposal_id = _id("proposal")

    async def _query():
        async with AsyncSessionLocal() as db:
            return await FigureEngine().get_approved_figures_by_section(db, proposal_id)

    assert _run(_query()) == {}


def _register_and_login(client, label: str) -> dict:
    email = f"{label}-{uuid.uuid4().hex[:10]}@example.com"
    password = "TestPassword123!"
    resp = client.post("/api/v1/auth/register", json={
        "email": email, "password": password,
        "full_name": f"{label.title()} User", "organization": "Test Org",
    })
    assert resp.status_code == 201, resp.text
    login = client.post("/api/v1/auth/login", data={"username": email, "password": password})
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


def test_figure_response_always_carries_a_download_url_key(client, registered_user):
    """Same carve-out as test_figures_api.py's module docstring: actually
    calling /figure-1 would make a real OpenAI call, out of scope for
    this suite — a ProposalFigure row is inserted directly via
    AsyncSessionLocal instead (the same approach test_figures_api.py's
    own annotation/approval tests use), then read back through the real
    GET /figures list endpoint. Covers two cases: stored_file_id null
    (download_url must be present and None, not missing/erroring) and
    stored_file_id set but pointing at no real StoredFile row (defensive
    — _resolve_download_url()'s scalar_one_or_none() must return None
    rather than raise)."""
    resp = client.post("/api/v1/proposals/", json={
        "title": "Figure Export Wiring Test", "agency": "NSF", "phase": "full_proposal",
    }, headers=registered_user["headers"])
    assert resp.status_code == 201, resp.text
    proposal_id = resp.json()["id"]

    async def _seed():
        async with AsyncSessionLocal() as db:
            fset = ProposalFigureSet(id=new_uuid(), proposal_id=proposal_id, source_section="technical_approach")
            db.add(fset)
            await db.flush()
            db.add(ProposalFigure(
                id=new_uuid(), figure_set_id=fset.id, figure_number=1,
                figure_type="functional_workflow", caption="Figure 1. No stored file.",
                stored_file_id=None,
            ))
            db.add(ProposalFigure(
                id=new_uuid(), figure_set_id=fset.id, figure_number=2,
                figure_type="technical_illustration", caption="Figure 2. Dangling stored file id.",
                stored_file_id="nonexistent-stored-file-id",
            ))
            await db.commit()
            return fset.id

    figure_set_id = _run(_seed())

    resp = client.get(f"/api/v1/proposals/{proposal_id}/figures/{figure_set_id}", headers=registered_user["headers"])
    assert resp.status_code == 200, resp.text
    figures = resp.json()["figures"]
    assert len(figures) == 2
    for fig in figures:
        assert "download_url" in fig
        assert fig["download_url"] is None
