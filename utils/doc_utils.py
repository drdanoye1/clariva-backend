"""
Shared document formatting utilities — federal submission standard.

Federal spec: Times New Roman 12pt, 1" margins, single spacing,
6pt after paragraphs, bottom-centre page numbers (Page X of Y),
three-level heading hierarchy (16 / 14 / 12 pt bold).
"""

from __future__ import annotations

import re
from typing import List, Tuple


# ─── Markdown stripping ───────────────────────────────────────────────────────

def strip_markdown(text: str) -> str:
    """Remove all markdown markers; return plain text."""
    if not text:
        return text
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*\*(.+?)\*\*',     r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*',         r'\1', text, flags=re.DOTALL)
    text = re.sub(r'___(.+?)___',       r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__',         r'\1', text, flags=re.DOTALL)
    text = re.sub(r'_(.+?)_',           r'\1', text, flags=re.DOTALL)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'`(.+?)`', r'\1', text)
    return text


def split_bold(text: str) -> List[Tuple[str, bool]]:
    """
    Split text on **bold** markers.
    Returns [(segment, is_bold), ...].
    """
    parts = re.split(r'\*\*(.+?)\*\*', text, flags=re.DOTALL)
    return [(p, i % 2 == 1) for i, p in enumerate(parts) if p]


# ─── python-docx helpers ──────────────────────────────────────────────────────

FEDERAL_FONT    = "Times New Roman"
BODY_PT         = 12
H1_PT, H2_PT, H3_PT = 16, 14, 12
MARGIN_IN       = 1.0
SPACE_AFTER_PT  = 6
SPACE_BEFORE_PT = 0


def apply_federal_margins(doc) -> None:
    """Set all section margins to 1 inch."""
    from docx.shared import Inches
    for sec in doc.sections:
        sec.top_margin    = Inches(MARGIN_IN)
        sec.bottom_margin = Inches(MARGIN_IN)
        sec.left_margin   = Inches(MARGIN_IN)
        sec.right_margin  = Inches(MARGIN_IN)


def _para_spacing(para, after: int = SPACE_AFTER_PT, before: int = SPACE_BEFORE_PT) -> None:
    """Single line spacing, 6pt after, 0pt before, widow/orphan on."""
    from docx.shared import Pt
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    pf = para.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after  = Pt(after)
    pPr = para._p.get_or_add_pPr()
    sp = pPr.find(qn('w:spacing'))
    if sp is None:
        sp = OxmlElement('w:spacing')
        pPr.append(sp)
    sp.set(qn('w:line'),     '240')
    sp.set(qn('w:lineRule'), 'auto')
    wc = pPr.find(qn('w:widowControl'))
    if wc is None:
        wc = OxmlElement('w:widowControl')
        pPr.append(wc)
    wc.set(qn('w:val'), '1')


def _make_run(para, text: str, bold: bool = False, italic: bool = False,
              pt: int = BODY_PT):
    from docx.shared import Pt
    from docx.oxml.ns import qn
    run = para.add_run(text)
    run.bold   = bold
    run.italic = italic
    run.font.size = Pt(pt)
    # Force Times New Roman through rPr/rFonts so it overrides the style default
    rPr = run._r.get_or_add_rPr()
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        from docx.oxml import OxmlElement
        rFonts = OxmlElement('w:rFonts')
        rPr.insert(0, rFonts)
    for attr in ('w:ascii', 'w:hAnsi', 'w:cs'):
        rFonts.set(qn(attr), FEDERAL_FONT)
    return run


def add_body_para(doc, text: str) -> None:
    """Add a body paragraph, converting **bold** to bold runs. Skips blank text."""
    text = text.strip()
    if not text:
        return
    text = re.sub(r'^#{1,6}\s+', '', text)   # strip stray heading markers
    para = doc.add_paragraph()
    _para_spacing(para)
    for segment, is_bold in split_bold(text):
        _make_run(para, segment, bold=is_bold)


def add_federal_heading(doc, text: str, level: int = 1) -> None:
    """Add a heading at the given level with Times New Roman and federal sizes."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    text = re.sub(r'^#{1,6}\s+', '', text.strip())
    text = strip_markdown(text)
    pt_map = {1: H1_PT, 2: H2_PT, 3: H3_PT}
    pt     = pt_map.get(level, H2_PT)
    try:
        para = doc.add_paragraph(style=f"Heading {min(level, 3)}")
    except Exception:
        para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.LEFT
    para.clear()
    _para_spacing(para, after=6, before=(12 if level == 1 else 6))
    _make_run(para, text, bold=True, pt=pt)


def add_page_numbers(doc) -> None:
    """Insert bottom-centre 'Page X of Y' into every section footer."""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    def _field_run(para, instr: str):
        run = para.add_run()
        run.font.size = Pt(10)
        rPr = run._r.get_or_add_rPr()
        rFonts = OxmlElement('w:rFonts')
        for attr in ('w:ascii', 'w:hAnsi', 'w:cs'):
            rFonts.set(qn(attr), FEDERAL_FONT)
        rPr.insert(0, rFonts)
        begin = OxmlElement('w:fldChar'); begin.set(qn('w:fldCharType'), 'begin')
        itext = OxmlElement('w:instrText'); itext.set(qn('xml:space'), 'preserve')
        itext.text = f' {instr} '
        end = OxmlElement('w:fldChar'); end.set(qn('w:fldCharType'), 'end')
        run._r.extend([begin, itext, end])

    for section in doc.sections:
        footer = section.footer
        footer.is_linked_to_previous = False
        p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        p.clear()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _make_run(p, "Page ", pt=10)
        _field_run(p, "PAGE")
        _make_run(p, " of ", pt=10)
        _field_run(p, "NUMPAGES")


def render_content(doc, content: str) -> None:
    """
    Parse AI-generated text (may contain markdown) into *doc* using
    federal formatting.  Handles ## headings, A. section labels,
    |table| rows, numbered items, **bold** inline.
    """
    lines = content.split("\n")
    tbl_buf: list = []   # list of list[str]

    def flush_table():
        if not tbl_buf:
            return
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        t = doc.add_table(rows=len(tbl_buf), cols=len(tbl_buf[0]))
        t.style = "Table Grid"
        for ri, row_cells in enumerate(tbl_buf):
            for ci, cell_text in enumerate(row_cells):
                cell = t.rows[ri].cells[ci]
                cell.text = ""
                cp = cell.paragraphs[0]
                _make_run(cp, cell_text, bold=(ri == 0))
                if ci > 0:
                    cp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        doc.add_paragraph()
        tbl_buf.clear()

    for line in lines:
        s = line.strip()

        if s.startswith("|"):
            cells = [c.strip() for c in s.split("|") if c.strip()]
            if all(set(c) <= set("-: ") for c in cells):
                continue   # separator
            if not tbl_buf:
                tbl_buf.append(cells)
            else:
                padded = (cells + [""] * len(tbl_buf[0]))[:len(tbl_buf[0])]
                tbl_buf.append(padded)
            continue
        else:
            flush_table()

        if not s:
            continue

        md_h = re.match(r'^(#{1,3})\s+(.*)', s)
        if md_h:
            add_federal_heading(doc, md_h.group(2), level=len(md_h.group(1)))
            continue

        if re.match(r'^[A-Z]\.\s', s):
            add_federal_heading(doc, s, level=2)
            continue

        num_m = re.match(r'^(\d+)\.\s+(.*)', s)
        if num_m:
            para = doc.add_paragraph()
            _para_spacing(para)
            _make_run(para, f"{num_m.group(1)}. ")
            for seg, bold in split_bold(num_m.group(2)):
                _make_run(para, seg, bold=bold)
            continue

        add_body_para(doc, s)

    flush_table()


# ─── ReportLab helpers ────────────────────────────────────────────────────────

def strip_for_pdf(text: str) -> str:
    """Strip markdown and escape ReportLab XML special characters."""
    text = strip_markdown(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_pdf_styles():
    """
    ParagraphStyle dict using Times-Roman (federal standard).
    Keys: title, h1, h2, body, bold, small.
    """
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY

    NAVY = colors.HexColor("#1F4E79")
    return {
        "title": ParagraphStyle("fed_title", fontName="Times-Bold", fontSize=16,
                                textColor=NAVY, alignment=TA_CENTER, spaceAfter=6),
        "h1":    ParagraphStyle("fed_h1",    fontName="Times-Bold", fontSize=14,
                                textColor=NAVY, spaceAfter=4, spaceBefore=12),
        "h2":    ParagraphStyle("fed_h2",    fontName="Times-Bold", fontSize=12,
                                textColor=NAVY, spaceAfter=3, spaceBefore=8),
        "body":  ParagraphStyle("fed_body",  fontName="Times-Roman", fontSize=12,
                                alignment=TA_JUSTIFY, spaceAfter=6, leading=16),
        "bold":  ParagraphStyle("fed_bold",  fontName="Times-Bold",  fontSize=12,
                                spaceAfter=4),
        "small": ParagraphStyle("fed_small", fontName="Times-Roman", fontSize=10,
                                alignment=TA_CENTER, spaceAfter=2),
    }


def pdf_page_number(canvas, doc):
    """ReportLab canvas callback — draws 'Page X' at bottom centre."""
    from reportlab.lib.units import inch
    canvas.saveState()
    canvas.setFont("Times-Roman", 10)
    canvas.drawCentredString(doc.pagesize[0] / 2.0, 0.5 * inch,
                             f"Page {canvas.getPageNumber()}")
    canvas.restoreState()
