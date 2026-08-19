"""
Figure 1 functional/process diagram renderer, and (Phase 11) the Figure 2
two-panel image compositor.

AI Figure Generation & Technical Illustration Development Specification
(docs/2_Upgrades_Clariva_AI_Figure_Specification_V01.docx), Phase 10 —
"Figure 1 functional/process diagram generation" — and Phase 11 — "Figure 2
technical/physical illustration + traceability".

Both figures share this module because both are ultimately "take some
already-generated visual material and lay it out predictably," even though
Figure 1's material is AI-extracted structured data (rendered from scratch
here) and Figure 2's material is two AI-generated images (composited here —
see compose_two_panel_figure() at the bottom of this file). Figure 2's own
docstring on that function explains why compositing, specifically, is the
deterministic step and image generation is not.

Figure 1's content (models.db_models.ProposalFigure.nodes — an ordered list
of {"id","label","order","classification"}) is fundamentally a sequence of
labeled boxes with directional arrows (§6/§7's own example: "1 Sensor Array
Installation -> 2 AI Algorithm Training -> ... -> 5 Threat Response"). That
is a deterministic rendering problem, not a generative one — this module
draws the diagram programmatically with matplotlib rather than asking an
image model to "draw a flowchart," which is both cheaper (no image-gen API
call) and produces the crisp, consistent, legible boxes §7's Diagram Design
Standard requires ("concise node labels ... consistent node geometry ...
readable typography ... limited text inside nodes") — an image model
reliably fails at exactly those requirements (illegible/garbled text,
inconsistent box shapes) for this kind of technical diagram.

This reuses the same matplotlib box/arrow/badge visual language
engines/image_gen.py::generate_flowchart already established for the legacy
caption-marker figure pipeline (navy/blue palette, rounded boxes, drop
shadow, numbered circular badges) so a Figure 1 diagram looks like it
belongs on the same page as any figure the legacy pipeline still produces
elsewhere, but is purpose-built for the new engines.figure_engine.py's
richer node model: every node carries an explicit `order` (independent of
list position, per FigureNode's own docstring) and a
TechnicalAccuracyClassification (confirmed/inferred/conceptual, spec §17)
that this renderer visually encodes as border style — solid navy
(confirmed), dashed navy (inferred), dotted grey with a lighter fill
(conceptual) — so a reviewer can see technical-accuracy status directly on
the figure, not just in an unread caption disclosure sentence (§18).

Layout support (§8's Diagram Type/Layout user controls):
  horizontal        - left-to-right row (the spec's own example orientation)
  vertical           - top-to-bottom column
  hierarchical       - top-to-bottom column with alternating left/right
                       indent, a lightweight way to visually distinguish
                       "hierarchical" from a plain vertical sequence without
                       inventing parent/child edges the data model doesn't
                       have (FigureNode is a flat ordered list, §5 does not
                       define branching/tree structure) — a real tree
                       layout is future scope if/when the node schema grows
                       parent-child links.
  circular_feedback  - nodes arranged evenly around a ring, connected
                       sequentially, with a final dashed arrow from the
                       last node back to the first — a legitimate reading
                       of "Circular/Feedback" per §8's own control option
                       name, and directly reflects §5's "does information
                       or control return to an earlier stage?" question.
Unknown/missing layout values fall back to horizontal (degrade gracefully,
same convention as pdf_convert.py's SOFFICE_BINARY handling).
"""
from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

_KNOWN_LAYOUTS = ("horizontal", "vertical", "hierarchical", "circular_feedback")

_NAVY = "#1F4E79"
_BLUE = "#2E75B6"
_WHITE = "#FFFFFF"
_SHADOW = "#CBD5E1"
_CONCEPTUAL_GREY = "#8C8C8C"
_CONCEPTUAL_FILL = "#5B7C99"  # muted/lightened navy for conceptual nodes

_PALETTE = [_NAVY, _BLUE]


def _wrap_label(label: str, max_chars: int = 16, max_lines: int = 3) -> str:
    words = (label or "").split()
    lines: List[str] = []
    cur: List[str] = []
    for w in words:
        test = " ".join(cur + [w])
        if len(test) > max_chars and cur:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines[:max_lines])


def _box_style(classification: str) -> Dict[str, Any]:
    """§17 Technical Accuracy Classification -> border/fill treatment.
    Confirmed is the default look; inferred/conceptual are visually
    distinguished so the figure itself discloses accuracy status, not just
    its caption (§18)."""
    cls = (classification or "confirmed").lower()
    if cls == "conceptual":
        return {"linestyle": "dotted", "edgecolor": _CONCEPTUAL_GREY, "linewidth": 1.6}
    if cls == "inferred":
        return {"linestyle": "dashed", "edgecolor": _NAVY, "linewidth": 1.6}
    return {"linestyle": "solid", "edgecolor": _NAVY, "linewidth": 1.5}


def _sorted_nodes(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Defensive sort by `order` — the caller (figure_engine.py) already
    sorts, but this module has no other caller-trust dependency, so it
    re-sorts rather than assuming."""
    return sorted(nodes or [], key=lambda n: n.get("order", 0))


def render_functional_diagram_png(
    nodes: List[Dict[str, Any]],
    layout: str = "horizontal",
    title: Optional[str] = None,
) -> bytes:
    """
    Render an ordered list of functional nodes
    ({"id","label","order","classification"}) as a Figure 1 diagram.
    Returns PNG bytes (150 dpi, white background). Pure/deterministic — no
    network calls, no DB, safe to unit-test directly.
    """
    layout = layout if layout in _KNOWN_LAYOUTS else "horizontal"
    ordered = _sorted_nodes(nodes)
    if not ordered:
        ordered = [{"id": "step_1", "label": "Step 1", "order": 1, "classification": "confirmed"}]

    if layout == "vertical":
        return _render_vertical(ordered, title, indent=False)
    if layout == "hierarchical":
        return _render_vertical(ordered, title, indent=True)
    if layout == "circular_feedback":
        return _render_circular(ordered, title)
    return _render_horizontal(ordered, title)


def _fill_color(i: int, classification: str) -> str:
    if (classification or "").lower() == "conceptual":
        return _CONCEPTUAL_FILL
    return _PALETTE[i % len(_PALETTE)]


def _draw_box(ax, x: float, y: float, w: float, h: float, i: int, node: Dict[str, Any]) -> None:
    from matplotlib.patches import Circle, FancyBboxPatch

    classification = node.get("classification", "confirmed")
    style = _box_style(classification)
    color = _fill_color(i, classification)

    ax.add_patch(FancyBboxPatch(
        (x + 0.05, y - 0.05), w, h, boxstyle="round,pad=0.07",
        linewidth=0, facecolor=_SHADOW, zorder=2,
    ))
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.07",
        facecolor=color, zorder=3, **style,
    ))
    badge_x, badge_y = x + 0.27, y + h - 0.27
    badge = Circle((badge_x, badge_y), 0.20, color=_WHITE, zorder=4)
    ax.add_patch(badge)
    node_num = node.get("order", i + 1)
    ax.text(badge_x, badge_y, str(node_num), ha="center", va="center",
            color=color, fontsize=8.5, fontweight="bold", zorder=5)

    label = _wrap_label(str(node.get("label", "")))
    ax.text(x + w / 2, y + h / 2 - 0.10, label, ha="center", va="center",
            color=_WHITE, fontsize=7.5, fontweight="bold", multialignment="center", zorder=4)


def _render_horizontal(nodes: List[Dict[str, Any]], title: Optional[str], **_ignored) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = len(nodes)
    box_w, box_h = 2.0, 1.3
    fig_w = max(9.0, n * 2.5)
    fig_h = 3.6
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    y_mid = 1.9
    x0 = 0.3
    step_span = fig_w - 0.6
    x_gap = (step_span - box_w) / max(n - 1, 1)

    for i, node in enumerate(nodes):
        x = x0 + i * x_gap
        _draw_box(ax, x, y_mid - box_h / 2, box_w, box_h, i, node)
        if i < n - 1:
            ax.annotate("", xy=(x + x_gap + 0.03, y_mid), xytext=(x + box_w + 0.04, y_mid),
                        arrowprops=dict(arrowstyle="->", color=_NAVY, lw=2.0, mutation_scale=20), zorder=6)

    _add_title(fig, title)
    return _to_png_bytes(fig)


def _render_vertical(nodes: List[Dict[str, Any]], title: Optional[str], indent: bool) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = len(nodes)
    box_w, box_h = 3.2, 1.1
    fig_w = 6.5 if not indent else 7.5
    fig_h = max(4.0, n * 1.8)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    x_center = (fig_w - box_w) / 2
    y_gap = 0.7
    y_span = n * (box_h + y_gap) - y_gap
    y0 = (fig_h - y_span) / 2

    for i, node in enumerate(nodes):
        # Draw top -> bottom in reading order (first node at top).
        y = y0 + (n - 1 - i) * (box_h + y_gap)
        x = x_center
        if indent:
            x += (0.6 if i % 2 == 0 else -0.6)
        _draw_box(ax, x, y, box_w, box_h, i, node)
        if i < n - 1:
            next_y = y0 + (n - 2 - i) * (box_h + y_gap)
            ax.annotate("", xy=(x_center + box_w / 2, next_y + box_h + y_gap - 0.05),
                        xytext=(x_center + box_w / 2, y - 0.05),
                        arrowprops=dict(arrowstyle="->", color=_NAVY, lw=2.0, mutation_scale=20), zorder=6)

    _add_title(fig, title)
    return _to_png_bytes(fig)


def _render_circular(nodes: List[Dict[str, Any]], title: Optional[str]) -> bytes:
    import math
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = len(nodes)
    box_w, box_h = 2.1, 1.1
    size = max(8.0, 2.2 * n)
    fig, ax = plt.subplots(figsize=(size, size))
    ax.set_xlim(0, size)
    ax.set_ylim(0, size)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    cx, cy = size / 2, size / 2
    radius = (size / 2) - max(box_w, box_h)
    centers = []
    for i in range(n):
        angle = (2 * math.pi * i / n) - (math.pi / 2)  # start at top, clockwise
        px = cx + radius * math.cos(angle)
        py = cy + radius * math.sin(angle)
        centers.append((px, py))

    for i, node in enumerate(nodes):
        px, py = centers[i]
        _draw_box(ax, px - box_w / 2, py - box_h / 2, box_w, box_h, i, node)

    for i in range(n):
        x1, y1 = centers[i]
        x2, y2 = centers[(i + 1) % n]
        is_feedback = (i == n - 1)  # last -> first is the feedback edge
        dx, dy = x2 - x1, y2 - y1
        dist = max((dx ** 2 + dy ** 2) ** 0.5, 0.001)
        pad = max(box_w, box_h) / 2 + 0.05
        sx, sy = x1 + dx / dist * pad, y1 + dy / dist * pad
        ex, ey = x2 - dx / dist * pad, y2 - dy / dist * pad
        style = dict(arrowstyle="->", color=_NAVY, lw=2.0, mutation_scale=18)
        if is_feedback:
            style = dict(arrowstyle="->", color=_BLUE, lw=1.6, linestyle="dashed", mutation_scale=16)
        ax.annotate("", xy=(ex, ey), xytext=(sx, sy), arrowprops=style, zorder=6)

    _add_title(fig, title)
    return _to_png_bytes(fig)


def _add_title(fig, title: Optional[str]) -> None:
    if title:
        fig.text(0.5, 0.02, title[:90], ha="center", va="bottom",
                  fontsize=8.5, color=_NAVY, style="italic")


def _to_png_bytes(fig) -> bytes:
    """Saves via the Figure object's own savefig (not the pyplot-global
    plt.savefig) so this is safe even if another figure were somehow open
    concurrently — matplotlib's pyplot state is a single global current-
    figure pointer, and this module is called from async engine code where
    "the current figure" isn't a safe assumption to rely on."""
    import matplotlib.pyplot as plt

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ── Figure 2 — two-panel compositor (Phase 11) ──────────────────────────────
#
# §14's "Recommended Two-Panel Figure 2" wants Panel A (perspective view)
# and Panel B (sectional/cutaway view) presented together as one coordinated
# figure, each with a numbered legend traceable back to Figure 1 (§10/§11).
# The two panel IMAGES themselves are genuinely generative — an AI image
# model earns its cost here, unlike Figure 1 — so engines/figure_engine.py
# generates each panel separately via DALL-E. But laying two finished
# images out side by side with panel labels is, like Figure 1's diagram,
# a solved deterministic problem: compose_two_panel_figure() below does
# that with PIL, so the AI is never asked to also get "put A next to B
# with a label under each" pixel-perfect, which image models are
# unreliable at.
#
# §19's Two-Stage Technical Illustration Generation (Stage 1 visual
# generation, Stage 2 programmatic annotation) maps onto this cleanly:
# Stage 1 is the DALL-E call (engines/figure_engine.py), Stage 2 is this
# compositor PLUS the figure's `callouts` field — the numbered legend
# itself is deliberately NOT burned into the image pixels (that would
# require pixel-accurate coordinates on an AI-generated image, which no
# available model reliably provides without additional vision-model
# tooling this phase doesn't build). Instead, callouts are structured data
# a renderer presents as a legend adjacent to the figure — exactly what
# ProposalFigure.callouts's own docstring already describes ("Figure 2's
# numbered component legend"), so this isn't a new design decision so
# much as taking that Phase 9 schema literally.

_PANEL_LABEL_H = 60  # px reserved at the bottom of each panel for its "Panel A"/"Panel B" label
_PANEL_GAP = 12       # px gap between the two panels
_PANEL_BG = (255, 255, 255)
_PANEL_LABEL_COLOR = (31, 78, 121)  # matches _NAVY


def compose_two_panel_figure(
    panel_a_png: bytes,
    panel_b_png: Optional[bytes] = None,
    panel_a_label: str = "Panel A - Perspective View",
    panel_b_label: str = "Panel B - Sectional / Cutaway View",
) -> bytes:
    """
    Lays out one or two already-generated panel images side by side with a
    caption strip under each (§14). If `panel_b_png` is None (a
    single-view Figure 2 — view_type "perspective" or "architecture"
    rather than a two-panel combination), returns `panel_a_png` re-encoded
    through the same label-strip treatment for visual consistency with the
    two-panel case, rather than returning the raw bytes unchanged.
    Returns PNG bytes.
    """
    from PIL import Image, ImageDraw, ImageFont

    def _labeled(png_bytes: bytes, label: str, target_h: int) -> "Image.Image":
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        # Scale to a common height so mismatched panel image dimensions
        # (e.g. two DALL-E calls that returned slightly different aspect
        # ratios) still line up cleanly side by side.
        scale = target_h / img.height
        img = img.resize((max(1, int(img.width * scale)), target_h))
        canvas = Image.new("RGB", (img.width, target_h + _PANEL_LABEL_H), _PANEL_BG)
        canvas.paste(img, (0, 0))
        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None
        text_w = draw.textlength(label, font=font) if font else len(label) * 6
        draw.text(((canvas.width - text_w) / 2, target_h + 18), label, fill=_PANEL_LABEL_COLOR, font=font)
        return canvas

    target_h = 900
    left = _labeled(panel_a_png, panel_a_label, target_h)

    if panel_b_png is None:
        return _pil_to_png_bytes(left)

    right = _labeled(panel_b_png, panel_b_label, target_h)

    combined = Image.new("RGB", (left.width + _PANEL_GAP + right.width, left.height), _PANEL_BG)
    combined.paste(left, (0, 0))
    combined.paste(right, (left.width + _PANEL_GAP, 0))
    return _pil_to_png_bytes(combined)


def _pil_to_png_bytes(image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()
