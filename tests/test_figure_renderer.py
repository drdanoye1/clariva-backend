"""
utils/figure_renderer.py — deterministic figure image rendering/compositing
(docs/2_Upgrades_Clariva_AI_Figure_Specification_V01.docx). Phase 10 built
render_functional_diagram_png (Figure 1); Phase 11 added
compose_two_panel_figure (Figure 2's deterministic panel layout).

Pure/deterministic and needs no DB/app/OpenAI — matplotlib and PIL are
this module's only dependencies, so this file runs in any environment
those are installed in (unlike most of this feature's coverage, which
needs pydantic/sqlalchemy/openai and is therefore only exercised through
test_figures_api.py's HTTP-level "billing gate reached" pattern). Verified
for real during development directly in the sandbox:
  - Phase 10 (render_functional_diagram_png): all four layouts plus an
    unknown-layout fallback, a single-node figure, and the empty-nodes
    defensive fallback all produced real, valid PNGs (%PNG header, several
    KB each) — two renders (horizontal, circular_feedback) were
    additionally visually inspected and confirmed to show the correct
    node order, arrow direction/curvature, the feedback-loop dashed
    return arrow, and classification-based border styling (solid/dashed/
    dotted).
  - Phase 11 (compose_two_panel_figure): a two-panel composite from
    mismatched-aspect-ratio inputs produced the correct output size
    (1512, 960); a single-panel call produced (900, 960); two separate
    calls with identical inputs produced byte-identical output
    (determinism); a real render was visually inspected via the Read tool
    and confirmed clean, correctly-rendered panel labels — this caught a
    real bug (the original default labels used an em-dash "—", which
    PIL's ImageFont.load_default() bitmap font could not render, producing
    a garbled glyph; fixed by switching to a plain hyphen).
This test file re-creates that coverage in the project's standard pytest
form for CI.
"""
from __future__ import annotations

import io

import pytest

from utils import figure_renderer

_SAMPLE_NODES = [
    {"id": "sensor_array_installation", "label": "Sensor Array Installation", "order": 1, "classification": "confirmed"},
    {"id": "ai_algorithm_training", "label": "AI Algorithm Training", "order": 2, "classification": "inferred"},
    {"id": "system_calibration", "label": "System Calibration", "order": 3, "classification": "conceptual"},
    {"id": "real_time_monitoring", "label": "Real-Time Monitoring", "order": 4, "classification": "confirmed"},
    {"id": "threat_response", "label": "Threat Response", "order": 5, "classification": "confirmed"},
]


def _assert_valid_png(png_bytes: bytes) -> None:
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png_bytes) > 1500  # a real rendered diagram, not a blank/tiny stub


@pytest.mark.parametrize("layout", ["horizontal", "vertical", "hierarchical", "circular_feedback"])
def test_each_known_layout_produces_a_valid_png(layout):
    png = figure_renderer.render_functional_diagram_png(_SAMPLE_NODES, layout=layout, title="Figure 1. Test.")
    _assert_valid_png(png)


def test_unknown_layout_falls_back_to_horizontal():
    """Degrade gracefully, same convention as pdf_convert.py's
    SOFFICE_BINARY handling — an unrecognized layout string must not
    raise, and should render identically to the horizontal default."""
    known = figure_renderer.render_functional_diagram_png(_SAMPLE_NODES, layout="horizontal")
    unknown = figure_renderer.render_functional_diagram_png(_SAMPLE_NODES, layout="not_a_real_layout")
    _assert_valid_png(unknown)
    # Same layout branch -> same deterministic output for the same input.
    assert len(known) == len(unknown)


def test_single_node_renders_without_error():
    png = figure_renderer.render_functional_diagram_png(
        [{"id": "only", "label": "Only Step", "order": 1, "classification": "confirmed"}], layout="horizontal",
    )
    _assert_valid_png(png)


def test_empty_node_list_falls_back_to_a_placeholder_step():
    """A caller should never pass an empty node list in practice (the
    engine 500s first if the AI returns none), but the renderer itself
    must not crash if it ever does — defensive fallback, not a caller
    contract."""
    png = figure_renderer.render_functional_diagram_png([], layout="vertical")
    _assert_valid_png(png)


def test_nodes_are_rendered_in_order_regardless_of_input_order():
    """render_functional_diagram_png re-sorts by `order` defensively (see
    module docstring) — passing nodes already shuffled must produce the
    same output as passing them pre-sorted."""
    shuffled = list(reversed(_SAMPLE_NODES))
    sorted_render = figure_renderer.render_functional_diagram_png(_SAMPLE_NODES, layout="horizontal")
    shuffled_render = figure_renderer.render_functional_diagram_png(shuffled, layout="horizontal")
    assert sorted_render == shuffled_render


def test_classification_affects_visual_output():
    """A node's classification changes its border style/fill (§17/§18) —
    two otherwise-identical single-node figures with different
    classifications must not render to byte-identical PNGs."""
    confirmed = figure_renderer.render_functional_diagram_png(
        [{"id": "n", "label": "Node", "order": 1, "classification": "confirmed"}], layout="horizontal",
    )
    conceptual = figure_renderer.render_functional_diagram_png(
        [{"id": "n", "label": "Node", "order": 1, "classification": "conceptual"}], layout="horizontal",
    )
    assert confirmed != conceptual


# ── compose_two_panel_figure (Phase 11) ──────────────────────────────────────

def _make_solid_png(width: int, height: int, color=(200, 50, 50)) -> bytes:
    """A minimal real PNG (not a stub) to feed compose_two_panel_figure as
    a stand-in for a DALL-E-generated panel image — the compositor only
    cares about pixel dimensions/content, not provenance."""
    from PIL import Image
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def test_two_panel_composite_has_correct_dimensions_for_mismatched_aspect_ratios():
    """Panels A and B are independently scaled to a common target height
    before being placed side by side — mismatched input aspect ratios must
    not distort or misalign the composite."""
    from PIL import Image
    panel_a = _make_solid_png(1024, 1024)   # square
    panel_b = _make_solid_png(1792, 1024)   # wide
    composite = figure_renderer.compose_two_panel_figure(panel_a, panel_b)
    _assert_valid_png(composite)
    img = Image.open(io.BytesIO(composite))
    # target_h=900, gap=12: panel A (1024x1024 -> scaled width 900) +
    # panel B (1792x1024 -> scaled width 1575) + 12px gap = 2487; height
    # is target_h(900) + label strip(60) = 960.
    assert img.size == (2487, 960)


def test_single_panel_composite_omits_panel_b():
    """If Panel B was never generated (e.g. a single-view figure type),
    the composite is just Panel A re-encoded through the same label-strip
    treatment, not a blank/placeholder second half."""
    from PIL import Image
    panel_a = _make_solid_png(1024, 1024)
    composite = figure_renderer.compose_two_panel_figure(panel_a, None)
    _assert_valid_png(composite)
    img = Image.open(io.BytesIO(composite))
    assert img.size == (900, 960)


def test_two_panel_composite_is_deterministic():
    panel_a = _make_solid_png(1024, 1024)
    panel_b = _make_solid_png(1024, 1792)
    first = figure_renderer.compose_two_panel_figure(panel_a, panel_b)
    second = figure_renderer.compose_two_panel_figure(panel_a, panel_b)
    assert first == second


def test_two_panel_composite_uses_default_hyphenated_labels():
    """Regression test for the em-dash rendering bug found during Phase 11
    development: PIL's ImageFont.load_default() bitmap font cannot render
    an em-dash glyph, so the default panel labels must use a plain ASCII
    hyphen instead. This doesn't OCR the rendered text (no OCR dependency
    in this sandbox) — it pins the actual default label strings so a
    future regression back to an em-dash (or any other non-ASCII
    punctuation) fails loudly here rather than silently producing garbled
    labels in production."""
    import inspect
    sig = inspect.signature(figure_renderer.compose_two_panel_figure)
    assert sig.parameters["panel_a_label"].default == "Panel A - Perspective View"
    assert sig.parameters["panel_b_label"].default == "Panel B - Sectional / Cutaway View"
    for label in (sig.parameters["panel_a_label"].default, sig.parameters["panel_b_label"].default):
        assert all(ord(ch) < 128 for ch in label), f"non-ASCII character in default label: {label!r}"
