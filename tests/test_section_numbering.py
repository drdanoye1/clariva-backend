"""
Auto section numbering ("Auto section numbering (1. / 1.1 / 1.1.1)" in the
export Formatting Options panel) — a real, user-reported bug: the
FormatOptions.section_numbering flag was read off the export request and
threaded all the way into utils/doc_utils.py::render_content(), but that
function's _num_prefix() closure only ever fires for legacy ## markdown
sub-headings inside a section's raw text. It is never called for the
per-section H1 headings every export actually shows (those go through
add_federal_heading() directly in engines/document_output.py, which has no
numbering logic of its own at all) — so checking the box had no visible
effect on any real proposal export.

Same "NO database session at all" pattern as test_visual_regression.py:
DocumentOutputEngine._export_docx() is synchronous and DB-free, so plain
SimpleNamespace stand-ins for `proposal`/`sections` are enough. Needs
python-docx (installed in this sandbox); does NOT need pydantic here since
these fixtures use structured_content=None and go through the legacy
render_content() path — the AI Figure/block-JSON machinery isn't in play
either way for this particular bug, and keeping it out lets this file
actually run in every environment, not just ones with pydantic installed.
"""
from __future__ import annotations

import io
from types import SimpleNamespace

from engines.document_output import DocumentOutputEngine
from utils.doc_utils import FormatOptions
from utils.docx_snapshot import extract_snapshot


def _make_sections():
    return [
        SimpleNamespace(title="Executive Summary", content="Summary text.", structured_content=None),
        SimpleNamespace(title="Technical Approach", content="Approach text.", structured_content=None),
        SimpleNamespace(title="Budget Justification", content="Budget text.", structured_content=None),
    ]


def _make_proposal():
    return SimpleNamespace(title="Test Proposal", agency="NSF", phase="phase_i", version=1)


def test_section_numbering_off_by_default_leaves_titles_unprefixed():
    engine = DocumentOutputEngine()
    buffer = io.BytesIO()
    engine._export_docx(buffer, _make_proposal(), _make_sections())
    snapshot = extract_snapshot(buffer.getvalue())

    headings = [p["text"] for p in snapshot["paragraphs"] if p["text"] in
                ("Executive Summary", "Technical Approach", "Budget Justification")]
    assert headings == ["Executive Summary", "Technical Approach", "Budget Justification"]


def test_section_numbering_on_prefixes_each_section_heading_sequentially():
    engine = DocumentOutputEngine()
    buffer = io.BytesIO()
    opts = FormatOptions(section_numbering=True)
    engine._export_docx(buffer, _make_proposal(), _make_sections(), opts=opts)
    snapshot = extract_snapshot(buffer.getvalue())

    heading_texts = [p["text"] for p in snapshot["paragraphs"] if p["style"] and p["style"].startswith("Heading")]
    assert heading_texts == [
        "1. Executive Summary",
        "2. Technical Approach",
        "3. Budget Justification",
    ]


def test_section_numbering_skips_sections_with_no_content():
    """A section with neither `content` nor `structured_content` is
    skipped entirely by the export loop (see document_output.py's
    `if not sec.content and not structured: continue`) — the numbering
    counter must only increment for sections that actually render,
    so a gap in the middle of a proposal's sections doesn't produce a
    gap in the printed numbers (e.g. "1., 3." instead of "1., 2.")."""
    engine = DocumentOutputEngine()
    sections = _make_sections()
    sections[1] = SimpleNamespace(title="Technical Approach", content=None, structured_content=None)
    buffer = io.BytesIO()
    opts = FormatOptions(section_numbering=True)
    engine._export_docx(buffer, _make_proposal(), sections, opts=opts)
    snapshot = extract_snapshot(buffer.getvalue())

    heading_texts = [p["text"] for p in snapshot["paragraphs"] if p["style"] and p["style"].startswith("Heading")]
    assert heading_texts == ["1. Executive Summary", "2. Budget Justification"]
