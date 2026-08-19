"""
AI Figure Generation & Technical Illustration Development Specification
(docs/2_Upgrades_Clariva_AI_Figure_Specification_V01.docx), Phase 9 —
"Figure JSON model (figureSet schema)".

No engine or router exists yet for this feature (that's Phase 10-12) — this
phase is schema-only, so these tests exercise the SQLAlchemy models
(models.db_models.ProposalFigureSet/ProposalFigure) directly via
AsyncSessionLocal, and the Pydantic schemas (models.schemas.
ProposalFigureSetOut/ProposalFigureOut and their nested value objects)
against real ORM rows, same "test the model, not a nonexistent engine"
approach test_award_engine.py's own model-creation tests use. Fake
proposal_id/user_id strings are used throughout without creating real
Proposal/User rows — SQLite's FK enforcement is off by default in this
app's setup (see test_scope_of_work_engine.py's and test_award_engine.py's
notes on this).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database import AsyncSessionLocal
from models.db_models import ProposalFigure, ProposalFigureSet, StoredFile, new_uuid
from models.schemas import (
    FigureApprovalStatus,
    FigureCallout,
    FigureNode,
    FigurePanel,
    FigureRelationship,
    FigureSetStatus,
    ProposalFigureOut,
    ProposalFigureSetOut,
    TechnicalAccuracyClassification,
    VisualCommunicationPlan,
)


def _run(coro):
    return asyncio.run(coro)


def _id(prefix: str) -> str:
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


# ── SQLAlchemy models — creation, relationship, cascade ─────────────────────

def test_create_figure_set_with_two_ordered_figures(client):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            fset = ProposalFigureSet(
                id=new_uuid(), proposal_id=proposal_id, source_section="technical_approach",
                status="planning",
                visual_communication_plan={
                    "materially_improves_understanding": True,
                    "concepts_requiring_visuals": ["sensor deployment", "AI training pipeline"],
                    "recommended_figure_count": 2,
                },
            )
            db.add(fset)
            await db.flush()

            # Insert Figure 2 first, then Figure 1 — the relationship's
            # order_by="ProposalFigure.figure_number" must still return them
            # in the correct 1, 2 order regardless of insertion order.
            fig2 = ProposalFigure(
                id=new_uuid(), figure_set_id=fset.id, figure_number=2,
                figure_type="technical_illustration", role="physical_implementation",
                relationship_data={
                    "depends_on_figure_number": 1,
                    "relationship_type": "physical_implementation_of_functional_model",
                    "preserve_functional_sequence": True,
                    "preserve_numbering": True,
                },
                callouts=[
                    {"number": 1, "label": "Camera array", "relates_to_node": "sensor_array_installation",
                     "classification": "confirmed"},
                ],
                panels=[
                    {"panel": "A", "role": "perspective", "description": "Full system in operating environment"},
                    {"panel": "B", "role": "sectional", "description": "Internal subsystem exposure"},
                ],
            )
            fig1 = ProposalFigure(
                id=new_uuid(), figure_set_id=fset.id, figure_number=1,
                figure_type="functional_workflow", role="functional_reference_model",
                diagram_family="workflow", layout="horizontal", detail_level="standard",
                nodes=[
                    {"id": "sensor_array_installation", "label": "Sensor Array Installation", "order": 1, "classification": "confirmed"},
                    {"id": "ai_algorithm_training", "label": "AI Algorithm Training", "order": 2, "classification": "confirmed"},
                ],
                caption="Figure 1. Functional workflow of the proposed system.",
            )
            db.add_all([fig2, fig1])
            await db.commit()
            return fset.id

    fset_id = _run(_body())

    async def _reload():
        async with AsyncSessionLocal() as db:
            # selectinload(figures) — a bare `fset.figures` attribute access
            # below happens AFTER `await db.execute(...)` has already
            # returned, i.e. outside any active async greenlet context, so
            # relying on the relationship's default lazy load there raises
            # sqlalchemy.exc.MissingGreenlet (confirmed by a real pytest
            # run — the "lazy-load is fine here" assumption this comment
            # used to make was wrong: being inside the same `async with`
            # session block does not mean being inside an awaited call).
            result = await db.execute(
                select(ProposalFigureSet)
                .options(selectinload(ProposalFigureSet.figures))
                .where(ProposalFigureSet.id == fset_id)
            )
            fset = result.scalar_one()
            figures = fset.figures
            return fset, list(figures)

    fset, figures = _run(_reload())
    assert fset.source_section == "technical_approach"
    assert fset.visual_communication_plan["recommended_figure_count"] == 2
    assert [f.figure_number for f in figures] == [1, 2]
    assert figures[0].nodes[0]["id"] == "sensor_array_installation"
    assert figures[1].relationship_data["depends_on_figure_number"] == 1
    assert figures[1].panels[1]["role"] == "sectional"


def test_deleting_figure_set_cascades_to_its_figures(client):
    proposal_id = _id("proposal")

    async def _create():
        async with AsyncSessionLocal() as db:
            fset = ProposalFigureSet(id=new_uuid(), proposal_id=proposal_id, source_section="team")
            db.add(fset)
            await db.flush()
            fig = ProposalFigure(id=new_uuid(), figure_set_id=fset.id, figure_number=1, figure_type="functional_workflow")
            db.add(fig)
            await db.commit()
            return fset.id, fig.id

    fset_id, fig_id = _run(_create())

    async def _delete_and_check():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(ProposalFigureSet).where(ProposalFigureSet.id == fset_id))
            fset = result.scalar_one()
            await db.delete(fset)
            await db.commit()
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(ProposalFigure).where(ProposalFigure.id == fig_id))
            return result.scalar_one_or_none()

    orphan = _run(_delete_and_check())
    assert orphan is None, "cascade='all, delete-orphan' should have removed the child figure too"


def test_figure_links_to_a_stored_file_asset(client):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            stored = StoredFile(
                id=new_uuid(), org_id=None, object_type="proposal_figure", object_id="placeholder",
                storage_key="figures/fake-key.png", original_filename="figure_1.png",
                content_type="image/png", size_bytes=1024, checksum="deadbeef",
                created_by=_id("user"),
            )
            db.add(stored)
            await db.flush()

            fset = ProposalFigureSet(id=new_uuid(), proposal_id=proposal_id, source_section="technical_approach")
            db.add(fset)
            await db.flush()
            fig = ProposalFigure(
                id=new_uuid(), figure_set_id=fset.id, figure_number=1,
                figure_type="functional_workflow", stored_file_id=stored.id,
            )
            db.add(fig)
            await db.commit()
            return fig.id, stored.id

    fig_id, stored_id = _run(_body())

    async def _reload():
        async with AsyncSessionLocal() as db:
            # Same MissingGreenlet reasoning as
            # test_create_figure_set_with_two_ordered_figures's _reload()
            # above — eager-load stored_file so the `.storage_key` access
            # below doesn't attempt a lazy load outside the greenlet.
            result = await db.execute(
                select(ProposalFigure)
                .options(selectinload(ProposalFigure.stored_file))
                .where(ProposalFigure.id == fig_id)
            )
            fig = result.scalar_one()
            return fig.stored_file_id, fig.stored_file.storage_key

    linked_id, storage_key = _run(_reload())
    assert linked_id == stored_id
    assert storage_key == "figures/fake-key.png"


def test_defaults_are_planning_and_pending(client):
    proposal_id = _id("proposal")

    async def _body():
        async with AsyncSessionLocal() as db:
            fset = ProposalFigureSet(id=new_uuid(), proposal_id=proposal_id, source_section="commercialization")
            db.add(fset)
            await db.flush()
            fig = ProposalFigure(id=new_uuid(), figure_set_id=fset.id, figure_number=1, figure_type="functional_workflow")
            db.add(fig)
            await db.commit()
            return fset.status, fig.approval_status, fig.requires_user_approval

    status, approval_status, requires_approval = _run(_body())
    assert status == "planning"
    assert approval_status == "pending"
    assert requires_approval is True


# ── Pydantic schemas — real ORM row -> schema, and pure schema construction ──

def test_proposal_figure_out_validates_from_a_real_orm_row(client):
    proposal_id = _id("proposal")

    async def _create():
        async with AsyncSessionLocal() as db:
            fset = ProposalFigureSet(id=new_uuid(), proposal_id=proposal_id, source_section="technical_approach")
            db.add(fset)
            await db.flush()
            fig = ProposalFigure(
                id=new_uuid(), figure_set_id=fset.id, figure_number=1,
                figure_type="functional_workflow", role="functional_reference_model",
                nodes=[{"id": "n1", "label": "Node One", "order": 1, "classification": "confirmed"}],
                caption="Figure 1. Example.", alt_text="A workflow diagram.",
            )
            db.add(fig)
            await db.commit()
            return fig.id

    fig_id = _run(_create())

    async def _reload():
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(ProposalFigure).where(ProposalFigure.id == fig_id))
            return result.scalar_one()

    fig_row = _run(_reload())
    out = ProposalFigureOut.model_validate(fig_row)
    assert out.figure_number == 1
    assert out.approval_status == FigureApprovalStatus.PENDING
    assert len(out.nodes) == 1
    assert out.nodes[0].classification == TechnicalAccuracyClassification.CONFIRMED
    assert out.caption == "Figure 1. Example."


def test_figure_relationship_defaults_match_spec_section_10():
    rel = FigureRelationship(depends_on_figure_number=1)
    assert rel.relationship_type == "physical_implementation_of_functional_model"
    assert rel.preserve_functional_sequence is True
    assert rel.preserve_numbering is True


def test_figure_set_status_and_visual_communication_plan_construct_cleanly():
    plan = VisualCommunicationPlan(
        materially_improves_understanding=True,
        concepts_requiring_visuals=["sensor deployment"],
        recommended_figure_count=2,
        figure_1_purpose="Explain the operational workflow.",
        figure_2_purpose="Show the physical implementation.",
    )
    out = ProposalFigureSetOut(
        id=new_uuid(), proposal_id=_id("proposal"), source_section="technical_approach",
        status=FigureSetStatus.READY, visual_communication_plan=plan,
        figures=[], created_at=datetime.now(timezone.utc),
    )
    assert out.status == FigureSetStatus.READY
    assert out.visual_communication_plan.recommended_figure_count == 2


def test_figure_callout_and_panel_construct_cleanly():
    callout = FigureCallout(number=1, label="Camera array", relates_to_node="sensor_array_installation")
    assert callout.classification == TechnicalAccuracyClassification.CONFIRMED

    panel_a = FigurePanel(panel="A", role="perspective", description="Full system view")
    panel_b = FigurePanel(panel="B", role="sectional")
    assert panel_a.panel == "A"
    assert panel_b.description is None


def test_figure_node_requires_explicit_order_and_defaults_to_confirmed():
    node = FigureNode(id="n1", label="Node One", order=1)
    assert node.classification == TechnicalAccuracyClassification.CONFIRMED
