"""
Word/PDF Report Generation Development Specification (CLARIVA-DOCGEN-SPEC-001),
Phase 2 — the semantic DOCX renderer.

Consumes a validated StructuredSectionContent (models/schemas.py's "Universal
Document Block Schema") and emits real python-docx objects directly, block by
block. This replaces doc_utils.py::render_content()'s regex line-parser for
any section that has structured_content populated — that legacy parser
remains in place and is still used for every section that doesn't (every
section until content generation migrates in Phase 4, see
ProposalSection.structured_content's docstring in models/db_models.py).

Callers (document_output.py) MUST treat a pydantic.ValidationError from
render_structured_content() as "fall back to the legacy render_content()
path against this section's plain `content`", never as a hard export
failure — a malformed structured_content blob should degrade gracefully,
not break someone's proposal export.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from utils.doc_utils import (
    FormatOptions, FEDERAL_FONT, BODY_PT, SPACE_AFTER_PT,
    _para_spacing, _make_run, _set_run_font,
    add_figure_image, add_figure_placeholder,
    render_schedule_table,
)


# ── Entry point ──────────────────────────────────────────────────────────────

def render_structured_content(doc, structured_content: Dict[str, Any],
                               figures: Optional[Dict[str, bytes]] = None,
                               opts: Optional[FormatOptions] = None) -> None:
    """Validate *structured_content* against StructuredSectionContent, then
    render every block in order. Raises pydantic.ValidationError on
    malformed input — see module docstring for the required caller
    behavior on that error."""
    from models.schemas import StructuredSectionContent

    model = StructuredSectionContent.model_validate(structured_content)
    figures = figures or {}
    fig_counter = [0]
    for block in model.blocks:
        _render_block(doc, block, figures, fig_counter, opts)


def _render_block(doc, block, figures: Dict[str, bytes], fig_counter: List[int],
                   opts: Optional[FormatOptions]) -> None:
    from models.schemas import (
        ParagraphBlock, RunInBlock, BulletListBlock, NumberedListBlock,
        TableBlock, ScheduleBlock, FigureBlock, CalloutBlock, PageBreakBlock,
        ReferencesBlock,
    )

    if isinstance(block, ParagraphBlock):
        _render_paragraph_block(doc, block, opts)
    elif isinstance(block, RunInBlock):
        _render_runin_block(doc, block, opts)
    elif isinstance(block, BulletListBlock):
        _render_list_block(doc, block.items, style="List Bullet", opts=opts)
    elif isinstance(block, NumberedListBlock):
        _render_list_block(doc, block.items, style="List Number", opts=opts)
    elif isinstance(block, TableBlock):
        _render_table_block(doc, block, opts)
    elif isinstance(block, ScheduleBlock):
        _render_schedule_block(doc, block)
    elif isinstance(block, FigureBlock):
        _render_figure_block(doc, block, figures, fig_counter)
    elif isinstance(block, CalloutBlock):
        _render_callout_block(doc, block, opts)
    elif isinstance(block, PageBreakBlock):
        doc.add_page_break()
    elif isinstance(block, ReferencesBlock):
        _render_references_block(doc, block, opts)
    else:
        # Unreachable in practice — the discriminated union already rejects
        # unknown `type` values at StructuredSectionContent.model_validate()
        # time (CLARIVA-DOCGEN-SPEC-001 §3: "Unknown block types SHALL fail
        # validation"). Kept as a defensive guard against a future block
        # type being added to the union without a renderer branch here.
        raise ValueError(f"No renderer registered for block type: {type(block).__name__}")


# ── Missing-data tag (§3.3 Missing Data Contract) ────────────────────────────

_MISSING_LABEL_COLOR = "92400E"   # matches document_output.py's disclaimer-box label color


def _append_missing_tag(para, missing, pt: int) -> None:
    """Append a conspicuous, colored '[MISSING: label]' run to *para* —
    Professional Report Formatting Standard §1: unresolved placeholders
    must never blend into prose. Uses the same amber palette as the
    existing AI-disclaimer box (document_output.py) for visual consistency
    until the full brand palette (Aptos/navy) lands in a later phase."""
    from docx.shared import Pt, RGBColor

    tag = " " if para.runs else ""
    tag += f"[REVIEW REQUIRED: {missing.label}]"
    run = para.add_run(tag)
    run.bold = True
    run.font.size = Pt(max(pt - 1, 8))
    run.font.color.rgb = RGBColor.from_string(_MISSING_LABEL_COLOR)
    _set_run_font(run)


# ── paragraph / runIn ────────────────────────────────────────────────────────

def _render_paragraph_block(doc, block, opts: Optional[FormatOptions]) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    if not block.text and not block.runs and not block.missing:
        return   # nothing to render — an empty block is a no-op, not an error

    pt    = opts.font_pt        if opts else BODY_PT
    font  = opts.font           if opts else FEDERAL_FONT
    after = opts.space_after_pt if opts else SPACE_AFTER_PT

    para = doc.add_paragraph()
    _para_spacing(para, after=after)
    if opts and opts.alignment == "justify":
        para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    if block.runs:
        for run in block.runs:
            _make_run(para, run.text, bold=run.bold, italic=run.italic, pt=pt, font=font)
    elif block.text:
        # A ParagraphBlock's `text` field is documented as prose with no
        # inline formatting — the whole point of `runs` existing separately
        # is that formatting is explicit data, not inferred from markdown.
        # So this is a single plain run, not a **bold**-scanning pass.
        _make_run(para, block.text, pt=pt, font=font)

    if block.missing:
        _append_missing_tag(para, block.missing, pt=pt)


def _render_runin_block(doc, block, opts: Optional[FormatOptions]) -> None:
    """Bold label + body in one paragraph — Personnel, Equipment, Travel,
    Indirect Costs, etc. (§4/§5's "Clariva Run-In" style)."""
    pt    = opts.font_pt        if opts else BODY_PT
    font  = opts.font           if opts else FEDERAL_FONT
    after = opts.space_after_pt if opts else SPACE_AFTER_PT

    para = doc.add_paragraph()
    _para_spacing(para, after=after)
    label = block.label if block.label.endswith((".", ":")) else f"{block.label}."
    _make_run(para, f"{label} ", bold=True, pt=pt, font=font)
    _make_run(para, block.text, pt=pt, font=font)


# ── lists ────────────────────────────────────────────────────────────────────

def _render_list_block(doc, items: List[str], style: str, opts: Optional[FormatOptions]) -> None:
    """Real Word list paragraphs (List Bullet / List Number styles) — never
    simulated hyphens or manually-typed numbers (§5.1/§9)."""
    font = opts.font    if opts else FEDERAL_FONT
    pt   = opts.font_pt if opts else BODY_PT
    for item in items:
        if not item:
            continue
        try:
            para = doc.add_paragraph(style=style)
        except Exception:
            # Template doesn't define the style (rare, e.g. a stripped-down
            # base template) — fall back to a plain paragraph rather than
            # raising, so one missing style doesn't fail the whole export.
            para = doc.add_paragraph()
        _para_spacing(para, after=2)
        _make_run(para, item, pt=pt, font=font)


# ── table ────────────────────────────────────────────────────────────────────

_NUMERIC_RE = re.compile(r'^[\$\-\+]?[\d,]+(\.\d+)?%?$')


def _infer_column_alignment(columns: List[str], rows: List[List[str]]) -> List[str]:
    """§5.1: numeric/currency columns right-aligned, short status/date
    columns centered, narrative columns left-aligned. Used only when the
    caller doesn't supply an explicit `columnAlign` list."""
    n = len(columns)
    align = ["left"] * n
    for ci in range(n):
        cell_values = [row[ci] for row in rows if ci < len(row) and row[ci]]
        if not cell_values:
            continue
        if all(_NUMERIC_RE.match(v.strip()) for v in cell_values):
            align[ci] = "right"
        elif all(len(v.strip()) <= 12 for v in cell_values) and ci > 0:
            align[ci] = "center"
    if align:
        align[0] = "left"   # first column (typically a label) is always left
    return align


def _render_table_block(doc, block, opts: Optional[FormatOptions]) -> None:
    """Real Word table with a navy header row and white text, per the
    Professional Report Formatting Standard §2 ("Navy header row with
    white text, light alternating row fills") — never a Markdown pipe
    table or an empty one-cell fragment (§5.1's explicit prohibition)."""
    from docx.shared import Pt, RGBColor
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    if not block.columns or not block.rows:
        return

    font = opts.font    if opts else FEDERAL_FONT
    pt   = (opts.font_pt if opts else BODY_PT) - 1   # tables run slightly smaller than body text
    align = block.columnAlign or _infer_column_alignment(block.columns, block.rows)

    if block.caption:
        cap = doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _para_spacing(cap, after=3, before=8)
        cr = cap.add_run(block.caption)
        cr.italic = True
        cr.font.size = Pt(pt)
        _set_run_font(cr)

    t = doc.add_table(rows=len(block.rows) + 1, cols=len(block.columns))
    t.style = "Table Grid"

    ALIGN_MAP = {
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
    }

    # Header row — navy fill, white bold text, repeated on page breaks.
    header_cells = t.rows[0].cells
    for ci, col_name in enumerate(block.columns):
        cell = header_cells[ci]
        cell.text = ""
        cp = cell.paragraphs[0]
        _para_spacing(cp, after=1, before=1)
        cp.alignment = ALIGN_MAP.get(align[ci] if ci < len(align) else "left", WD_ALIGN_PARAGRAPH.LEFT)
        r = _make_run(cp, col_name, bold=True, pt=pt, font=font)
        r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        tc = cell._tc
        tcPr = tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), "1F4E79")
        tcPr.append(shd)
    _repeat_header_row(t.rows[0])

    # Data rows — light alternating fills.
    ALT_FILL = "F0F9FF"
    for ri, row_cells in enumerate(block.rows):
        row = t.rows[ri + 1]
        row_fill = ALT_FILL if ri % 2 else None
        for ci in range(len(block.columns)):
            text = row_cells[ci] if ci < len(row_cells) else ""
            cell = row.cells[ci]
            cell.text = ""
            cp = cell.paragraphs[0]
            _para_spacing(cp, after=1, before=1)
            cp.alignment = ALIGN_MAP.get(align[ci] if ci < len(align) else "left", WD_ALIGN_PARAGRAPH.LEFT)
            _make_run(cp, text, pt=pt, font=font)
            if row_fill:
                tc = cell._tc
                tcPr = tc.get_or_add_tcPr()
                shd = OxmlElement("w:shd")
                shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto")
                shd.set(qn("w:fill"), row_fill)
                tcPr.append(shd)

    doc.add_paragraph()   # spacer after table


def _repeat_header_row(row) -> None:
    """Mark a table row as a repeating header (tblHeader) so it re-prints
    on every page the table spans — §5.1: "Repeat header rows across
    pages.\""""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    trPr = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    trPr.append(header)


# ── schedule (Clariva extension — see SchedulePhase's docstring) ────────────

def _render_schedule_block(doc, block) -> None:
    """Hands validated (name, start_month, end_month) tuples straight to
    render_schedule_table() — the same table-building code add_gantt_table()
    uses, minus its regex-over-prose extraction step. This is the concrete
    fix for the malformed-schedule-table defect the Professional Report
    Formatting Standard's review calls out."""
    phases = [(p.name, p.start_month, p.end_month) for p in block.phases]
    if not phases:
        add_figure_placeholder(doc, f"{block.title} — no phase data provided", "G")
        return
    render_schedule_table(doc, phases, block.title)


# ── figure ───────────────────────────────────────────────────────────────────

def _render_figure_block(doc, block, figures: Dict[str, bytes], fig_counter: List[int]) -> None:
    """§3.1/§5.2: caption numbering is always assigned by the renderer, never
    trusted from the model — `block.figureNumber`, when present, is
    informational metadata for Figure 1<->Figure 2 traceability (Phases
    9-12), not the printed caption number."""
    fig_counter[0] += 1
    img_bytes = figures.get(block.caption) if figures else None
    if img_bytes:
        add_figure_image(doc, block.caption, fig_counter[0], img_bytes)
    else:
        # §5.2: "never output a caption pretending the figure exists" — the
        # placeholder box makes the gap visible instead of silently
        # rendering just a caption with no image above it.
        add_figure_placeholder(doc, block.caption, fig_counter[0])


# ── callout ──────────────────────────────────────────────────────────────────

_CALLOUT_STYLES = {
    "warning": {"fill": "FFF3CD", "border": "D97706", "label_color": "92400E"},
    "missing": {"fill": "FEE2E2", "border": "DC2626", "label_color": "991B1B"},
    "note":    {"fill": "EFF6FF", "border": "2563EB", "label_color": "1E40AF"},
    "info":    {"fill": "F0F9FF", "border": "0EA5E9", "label_color": "0C4A6E"},
}


def _render_callout_block(doc, block, opts: Optional[FormatOptions]) -> None:
    """Styled shaded/bordered box — same visual technique as the existing
    AI-disclaimer box in document_output.py, generalized to the four
    callout kinds the schema defines."""
    from docx.shared import Pt, RGBColor
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    style = _CALLOUT_STYLES.get(block.kind, _CALLOUT_STYLES["note"])
    font = opts.font if opts else FEDERAL_FONT

    para = doc.add_paragraph()
    _para_spacing(para, after=8, before=4)

    if block.title:
        title_run = para.add_run(f"{block.title}\n")
        title_run.bold = True
        title_run.font.size = Pt(9)
        title_run.font.color.rgb = RGBColor.from_string(style["label_color"])
        _set_run_font(title_run)

    body_run = para.add_run(block.text)
    body_run.font.size = Pt(9)
    body_run.font.color.rgb = RGBColor.from_string(style["label_color"])
    _set_run_font(body_run)

    pPr = para._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), style["fill"])
    pPr.append(shd)
    pBdr = OxmlElement("w:pBdr")
    for side in ("top", "left", "bottom", "right"):
        bdr = OxmlElement(f"w:{side}")
        bdr.set(qn("w:val"), "single"); bdr.set(qn("w:sz"), "6")
        bdr.set(qn("w:space"), "4"); bdr.set(qn("w:color"), style["border"])
        pBdr.append(bdr)
    pPr.append(pBdr)


# ── references ───────────────────────────────────────────────────────────────

def _add_hyperlink(paragraph, url: str, text: str, pt: int, font: str) -> None:
    """Insert a real clickable hyperlink run — python-docx has no built-in
    helper for this, so it's built from the underlying OOXML relationship +
    <w:hyperlink> element directly. §5's Clariva Reference style: 'Preserve
    URLs as hyperlinks, not escaped text' (§3.1's ReferencesBlock rule:
    'no escaped URL artifacts')."""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.shared import Pt, RGBColor

    part = paragraph.part
    r_id = part.relate_to(
        url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "2563EB")
    rPr.append(color)
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rPr.append(u)
    rFonts = OxmlElement("w:rFonts")
    for attr in ("w:ascii", "w:hAnsi", "w:cs"):
        rFonts.set(qn(attr), font)
    rPr.append(rFonts)
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(pt * 2))
    rPr.append(sz)
    new_run.append(rPr)
    t = OxmlElement("w:t")
    t.text = text
    new_run.append(t)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)


def _render_references_block(doc, block, opts: Optional[FormatOptions]) -> None:
    """Hanging-indent reference list, one entry per paragraph — §4's
    "Clariva Reference" style."""
    from docx.shared import Pt, Inches

    font = opts.font    if opts else FEDERAL_FONT
    pt   = opts.font_pt if opts else BODY_PT
    pt   = max(pt - 2, 9)

    for entry in block.entries:
        para = doc.add_paragraph()
        _para_spacing(para, after=4)
        para.paragraph_format.left_indent = Inches(0.25)
        para.paragraph_format.first_line_indent = Inches(-0.25)
        if entry.url:
            _make_run(para, f"{entry.text} — ", pt=pt, font=font)
            _add_hyperlink(para, entry.url, entry.url, pt=pt, font=font)
        else:
            _make_run(para, entry.text, pt=pt, font=font)


# ── flatten-to-text (for search/embedding/TXT export/legacy PDF fallback) ───

def flatten_section_blocks_to_text(structured_content: Dict[str, Any]) -> str:
    """Derive a plain-text rendering of *structured_content* — the block-
    JSON equivalent of supporting_documents_engine.py's
    flatten_logic_model_to_text(). Used to keep ProposalSection.content (the
    field search/embedding/TXT-export/the legacy PDF path all read) in sync
    whenever structured_content is the field actually being edited, same
    "structured data is authoritative, content is derived" split. Does not
    require the DOCX renderer's python-docx dependency, so it's safe to call
    from any code path (including a future PDF-only export or a save
    endpoint) without pulling in python-docx.

    Raises pydantic.ValidationError on malformed input, same as
    render_structured_content() — callers should treat that identically
    (fall back to whatever `content` already holds rather than raising to
    the user).
    """
    from models.schemas import (
        StructuredSectionContent, ParagraphBlock, RunInBlock, BulletListBlock,
        NumberedListBlock, TableBlock, ScheduleBlock, FigureBlock, CalloutBlock,
        PageBreakBlock, ReferencesBlock,
    )

    model = StructuredSectionContent.model_validate(structured_content)
    lines: List[str] = []

    for block in model.blocks:
        if isinstance(block, ParagraphBlock):
            text = block.text or "".join(r.text for r in (block.runs or []))
            if block.missing:
                text = f"{text} [REVIEW REQUIRED: {block.missing.label}]".strip()
            if text:
                lines.append(text)
        elif isinstance(block, RunInBlock):
            label = block.label if block.label.endswith((".", ":")) else f"{block.label}."
            lines.append(f"{label} {block.text}")
        elif isinstance(block, BulletListBlock):
            lines.extend(f"- {item}" for item in block.items)
        elif isinstance(block, NumberedListBlock):
            lines.extend(f"{i}. {item}" for i, item in enumerate(block.items, start=1))
        elif isinstance(block, TableBlock):
            if block.caption:
                lines.append(block.caption)
            lines.append(" | ".join(block.columns))
            lines.extend(" | ".join(row) for row in block.rows)
        elif isinstance(block, ScheduleBlock):
            lines.append(block.title)
            # Deliberately kept in the exact "Name (Months X-Y)" shape the
            # legacy add_gantt_table() regex already understands, so text
            # flattened from a ScheduleBlock stays compatible with any code
            # path still scanning `content` for that pattern.
            lines.extend(f"{p.name} (Months {p.start_month}-{p.end_month})" for p in block.phases)
        elif isinstance(block, FigureBlock):
            lines.append(f"[Figure: {block.caption}]")
        elif isinstance(block, CalloutBlock):
            prefix = f"{block.title}: " if block.title else ""
            lines.append(f"[{block.kind.upper()}] {prefix}{block.text}")
        elif isinstance(block, PageBreakBlock):
            continue   # no plain-text equivalent
        elif isinstance(block, ReferencesBlock):
            lines.append("References:")
            lines.extend(
                f"- {e.text} ({e.url})" if e.url else f"- {e.text}"
                for e in block.entries
            )
        lines.append("")   # blank line between blocks

    return "\n".join(lines).strip()
