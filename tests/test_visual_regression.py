"""
Visual regression corpus (Word/PDF Report Generation Development
Specification, CLARIVA-DOCGEN-SPEC-001, Phase 14 — "§14 P2: Visual
regression corpus and profile-specific golden files").

Unlike every other engine/API test pair in this codebase, this file needs
NO database session at all — engines/document_output.py::_export_docx()
is a synchronous, DB-free method (it only reads plain attributes off its
`proposal`/`sections` arguments), so `tests/golden/fixtures.py`'s plain
`SimpleNamespace` stand-ins are enough. It still needs `python-docx`
(genuinely installed in this sandbox) AND `pydantic` (NOT installed here
— utils/block_renderer.py::render_structured_content() validates every
section's structured_content against StructuredSectionContent before
rendering it), so this file is static-verified (ast.parse) in this
sandbox rather than pytest-run, same dependency-gap split as every other
phase's AI-touching test file. It runs for real once pydantic/sqlalchemy
are installed in CI/deployment — and unlike prior phases, once running
there it needs no OpenAI mocking either, since nothing here makes an AI
call.

Two kinds of coverage, split into two test functions per fixture:

1. test_fixture_renders_without_error — needs NO golden file, so it
   provides real regression value (catches a renderer crash/exception)
   from the very first CI run, before anyone has bootstrapped the corpus.
2. test_fixture_matches_golden_snapshot — the actual regression gate.
   Skips (not fails, not silently passes) with an actionable message when
   tests/golden/<agency>.snapshot.json doesn't exist yet — see
   scripts/generate_golden_files.py's docstring for the one-time bootstrap
   step. Once bootstrapped, a real, reviewable diff (paragraph text/style/
   alignment/bold, table shape/cells, footer text/page-number-field
   presence, image count) fails the build with the exact difference,
   rather than a directoryful of PNGs nobody actually looks at in review.
"""
from __future__ import annotations

import io
import json
import os
from types import SimpleNamespace

import pytest

from engines.document_output import DocumentOutputEngine
from tests.golden.fixtures import AGENCY_FIXTURES
from utils.docx_snapshot import diff_snapshots, extract_snapshot, pdf_page_count_via_soffice

_GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden")
# Real environment drift is possible even with the *same* fonts/LibreOffice
# version (e.g. a hyphenation-driven reflow shifting one line to the next
# page) without any actual formatting regression — a small tolerance band
# avoids that flaking the build, while still catching the kind of gross
# regression (a doubled page count, a collapsed-to-zero-pages export) this
# secondary signal exists for. See utils/docx_snapshot.py's module
# docstring for why page count, not a pixel diff, is used at all.
_PAGE_COUNT_TOLERANCE = 1


@pytest.fixture()
def engine():
    return DocumentOutputEngine()


def _render_fixture(engine, fixture) -> bytes:
    proposal = SimpleNamespace(
        title=fixture["title"], agency=fixture["agency"],
        phase=fixture["phase"], version=1,
    )
    sections = [
        SimpleNamespace(title=sec["title"], content=None, structured_content=sec["structured_content"])
        for sec in fixture["sections"]
    ]
    buffer = io.BytesIO()
    engine._export_docx(buffer, proposal, sections)
    return buffer.getvalue()


@pytest.mark.parametrize("fixture", AGENCY_FIXTURES, ids=lambda f: f["agency"])
def test_fixture_renders_without_error(engine, fixture):
    docx_bytes = _render_fixture(engine, fixture)
    assert docx_bytes.startswith(b"PK"), (
        f"{fixture['agency']}: _export_docx() fell back to plain TXT — "
        f"python-docx isn't importable in this environment."
    )
    # Extraction itself must not raise on a real render, regardless of
    # whether a golden file exists yet to compare against.
    snapshot = extract_snapshot(docx_bytes)
    assert snapshot["paragraph_count"] > 0
    # Every fixture's shared block palette (tests/golden/fixtures.py::
    # _shared_blocks()) includes one TableBlock AND one ScheduleBlock —
    # utils/block_renderer.py::_render_schedule_block() hands its phases to
    # render_schedule_table(), which builds a real docx Table (the same
    # Gantt-style table add_gantt_table() produces), so two real tables are
    # expected in the rendered document, not one.
    assert snapshot["table_count"] == 2


@pytest.mark.parametrize("fixture", AGENCY_FIXTURES, ids=lambda f: f["agency"])
def test_fixture_matches_golden_snapshot(engine, fixture):
    golden_path = os.path.join(_GOLDEN_DIR, f"{fixture['agency']}.snapshot.json")
    if not os.path.exists(golden_path):
        pytest.skip(
            f"No golden file for {fixture['agency']} yet — run "
            f"`python scripts/generate_golden_files.py` once and commit "
            f"tests/golden/{fixture['agency']}.snapshot.json to enable this check "
            f"(see that script's docstring)."
        )

    with open(golden_path) as f:
        golden = json.load(f)

    docx_bytes = _render_fixture(engine, fixture)
    candidate = extract_snapshot(docx_bytes)

    diffs = diff_snapshots(golden, candidate)
    assert not diffs, (
        f"{fixture['agency']} export no longer matches its golden snapshot "
        f"({golden_path}). If this change is intentional, review the diff "
        f"below, then re-run scripts/generate_golden_files.py and commit "
        f"the updated golden file:\n" + "\n".join(diffs)
    )

    golden_pages = golden.get("pdf_page_count")
    # Computed separately from `candidate` — extract_snapshot() itself
    # never adds pdf_page_count (see its docstring); only
    # scripts/generate_golden_files.py stamps it onto the golden JSON.
    candidate_pages = pdf_page_count_via_soffice(docx_bytes)
    if golden_pages is not None and candidate_pages is not None:
        assert abs(golden_pages - candidate_pages) <= _PAGE_COUNT_TOLERANCE, (
            f"{fixture['agency']} PDF page count shifted from {golden_pages} to "
            f"{candidate_pages} (tolerance ±{_PAGE_COUNT_TOLERANCE}) — a layout "
            f"regression is more likely than an intentional change of this size."
        )
