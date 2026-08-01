"""
Budget export endpoints — Excel, Word, PDF for both budget and justification.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models.db_models import BudgetRecord, OrgContextDB, Proposal, User
from routers.auth import get_current_user
from routers.budget import _get_proposal, _calc_totals, _resolve_cap

router = APIRouter()


def _budget_title(proposal: Any) -> str:
    """Build a budget document title that respects the actual grant type."""
    gt = getattr(proposal, "grant_type", "") or ""
    pl = getattr(proposal, "program_label", None) or ""
    agency = getattr(proposal, "agency", "OTHER") or "OTHER"
    phase_str = ""
    if hasattr(proposal, "phase") and proposal.phase:
        phase_str = proposal.phase.replace("_", " ").title()
    if gt in ("sbir", "sttr"):
        return f"{agency} {phase_str} {gt.upper()} Budget"
    if pl:
        short = pl if len(pl) <= 50 else pl[:47] + "..."
        return f"{short} — Budget"
    label = gt.replace("_", " ").title() if gt else "Grant"
    return f"{agency} — {label} Budget"


# ── Shared helpers ────────────────────────────────────────────────────────────

def _money(n: float) -> str:
    return f"${n:,.0f}" if n else "$0"

def _load_budget_body(rec: BudgetRecord) -> Dict[str, Any]:
    return {
        "budget_months": rec.budget_months, "indirect_rate": rec.indirect_rate,
        "indirect_base": rec.indirect_base, "fee_rate": rec.fee_rate or 7.0,
        "personnel": rec.personnel or [], "consultants": rec.consultants or [],
        "equipment": rec.equipment or [], "travel": rec.travel or [],
        "other_direct": rec.other_direct or [], "subcontracts": rec.subcontracts or [],
    }


async def _get_rec_and_proposal(proposal_id: str, owner_id: str, db: AsyncSession):
    proposal = await _get_proposal(proposal_id, owner_id, db)
    r = await db.execute(select(BudgetRecord).where(BudgetRecord.proposal_id == proposal_id))
    rec = r.scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=404, detail="No saved budget found. Save budget first.")
    return proposal, rec


async def _get_org(user_id: str, db: AsyncSession) -> str:
    r = await db.execute(select(OrgContextDB).where(OrgContextDB.user_id == user_id))
    ctx = r.scalar_one_or_none()
    return (ctx and ctx.organization_name) or "Applicant Organization"


# ══════════════════════════════════════════════════════════════════════════════
# BUDGET EXPORTS
# ══════════════════════════════════════════════════════════════════════════════

# ── Excel ─────────────────────────────────────────────────────────────────────

@router.get("/{proposal_id}/export/xlsx")
async def export_budget_xlsx(
    proposal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    proposal, rec = await _get_rec_and_proposal(proposal_id, current_user.id, db)
    org = await _get_org(current_user.id, db)
    body = _load_budget_body(rec)
    totals = _calc_totals(body)
    months = rec.budget_months or 12

    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, numbers
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Budget Summary"

    # Styles
    H1 = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
    H2 = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    LABEL = Font(name="Calibri", size=10, bold=True)
    BODY = Font(name="Calibri", size=10)
    TOTAL = Font(name="Calibri", size=10, bold=True, color="1F4E79")
    GRAND = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    FILL_DARK = PatternFill("solid", fgColor="1F4E79")
    FILL_MED  = PatternFill("solid", fgColor="2E75B6")
    FILL_LIGHT= PatternFill("solid", fgColor="D6E4F0")
    FILL_GRAND= PatternFill("solid", fgColor="C00000")
    BORDER = Border(
        bottom=Side(style="thin", color="BFBFBF"),
    )
    CENTER = Alignment(horizontal="center", vertical="center")
    RIGHT = Alignment(horizontal="right")

    def set_cell(row, col, value, font=None, fill=None, align=None, number_format=None, border=None):
        c = ws.cell(row=row, column=col, value=value)
        if font: c.font = font
        if fill: c.fill = fill
        if align: c.alignment = align
        if number_format: c.number_format = number_format
        if border: c.border = border
        return c

    # Title block
    ws.merge_cells("A1:G1")
    set_cell(1, 1, _budget_title(proposal), H1, FILL_DARK, CENTER)
    ws.row_dimensions[1].height = 28

    ws.merge_cells("A2:G2")
    set_cell(2, 1, f"{proposal.title or 'Proposal'} | {org}", BODY, FILL_MED, CENTER)

    ws.merge_cells("A3:G3")
    set_cell(3, 1, f"Performance Period: {months} months   |   Generated: {datetime.now().strftime('%B %d, %Y')}", BODY, FILL_LIGHT, CENTER)

    row = 5

    def section_header(title):
        nonlocal row
        ws.merge_cells(f"A{row}:G{row}")
        set_cell(row, 1, title, H2, FILL_MED, CENTER)
        ws.row_dimensions[row].height = 20
        row += 1

    def col_headers(*labels):
        nonlocal row
        for i, lbl in enumerate(labels, 1):
            set_cell(row, i, lbl, LABEL, FILL_LIGHT, CENTER)
        row += 1

    def data_row(*values, bold=False):
        nonlocal row
        font = Font(name="Calibri", size=10, bold=bold)
        for i, val in enumerate(values, 1):
            c = ws.cell(row=row, column=i, value=val)
            c.font = font
            c.border = BORDER
            if isinstance(val, (int, float)) and i > 1:
                c.number_format = '"$"#,##0'
                c.alignment = RIGHT
        row += 1

    def subtotal_row(label, amount):
        nonlocal row
        ws.merge_cells(f"A{row}:E{row}")
        set_cell(row, 1, label, TOTAL)
        c = ws.cell(row=row, column=6, value=amount)
        c.font = TOTAL; c.number_format = '"$"#,##0'; c.alignment = RIGHT
        row += 1; row += 1  # blank after

    # ── A. Personnel ─────────────────────────────────────────────────────────
    section_header("A. Senior/Key Personnel")
    col_headers("Name", "Role", "Annual Salary", "Effort %", "Hours", "Salary Cost", "Fringe Cost")
    hrs_per_yr = 2080.0
    for p in (rec.personnel or []):
        sal = float(p.get("annual_salary") or 0)
        effort = float(p.get("effort_pct") or 0)
        fringe = float(p.get("fringe_rate") or 0)
        sal_cost = sal * (effort / 100) * (months / 12)
        frg_cost = sal_cost * (fringe / 100)
        hours = round((effort / 100) * (months / 12) * hrs_per_yr)
        data_row(p.get("name",""), p.get("role",""), sal, effort, hours, sal_cost, frg_cost)
    subtotal_row("A. Personnel + Fringe Total", totals["personnel_salary"] + totals["personnel_fringe"])

    # ── B. Consultants ────────────────────────────────────────────────────────
    if rec.consultants:
        section_header("B. Consultants")
        col_headers("Name", "Role", "Rate/Day", "Days", "", "Total", "")
        for c in rec.consultants:
            cost = float(c.get("rate_per_day") or 0) * float(c.get("days") or 0)
            data_row(c.get("name",""), c.get("role",""), c.get("rate_per_day",0), c.get("days",0), "", cost, "")
        subtotal_row("B. Consultants Total", totals["consultant_total"])

    # ── C. Equipment ──────────────────────────────────────────────────────────
    if rec.equipment:
        section_header("C. Equipment (≥ $5,000)")
        col_headers("Item", "Description", "", "", "", "Cost", "")
        for e in rec.equipment:
            data_row(e.get("name",""), e.get("description",""), "", "", "", e.get("cost",0), "")
        subtotal_row("C. Equipment Total", totals["equipment_total"])

    # ── D. Travel ─────────────────────────────────────────────────────────────
    if rec.travel:
        section_header("D. Travel")
        col_headers("Purpose", "Destination", "Trips", "People", "Cost/Trip", "Total", "")
        for t in rec.travel:
            cost = float(t.get("trips") or 0) * float(t.get("people") or 1) * float(t.get("cost_per_trip") or 0)
            data_row(t.get("purpose",""), t.get("destination",""), t.get("trips",0), t.get("people",1), t.get("cost_per_trip",0), cost, "")
        subtotal_row("D. Travel Total", totals["travel_total"])

    # ── E. Other Direct Costs ─────────────────────────────────────────────────
    if rec.other_direct:
        section_header("E. Other Direct Costs")
        col_headers("Category", "Description", "", "", "", "Cost", "")
        for o in rec.other_direct:
            data_row(o.get("category",""), o.get("description",""), "", "", "", o.get("cost",0), "")
        subtotal_row("E. Other Direct Total", totals["other_total"])

    # ── F. Subcontracts ───────────────────────────────────────────────────────
    if rec.subcontracts:
        section_header("F. Subcontracts / Subawards")
        col_headers("Organization", "PI", "", "", "", "Total Cost", "MTDC Portion")
        for s in rec.subcontracts:
            mtdc_portion = min(25000, float(s.get("total_cost") or 0))
            data_row(s.get("organization",""), s.get("pi_name",""), "", "", "", s.get("total_cost",0), mtdc_portion)
        subtotal_row("F. Subcontracts Total", totals["subcontract_total"])

    # ── Budget Totals ─────────────────────────────────────────────────────────
    section_header("Budget Totals")
    totals_rows = [
        ("H. Total Direct Costs", totals["total_direct"]),
        (f"I. Indirect / F&A ({rec.indirect_rate}% on {rec.indirect_base.upper()})", totals["total_indirect"]),
        ("J. Total Direct + Indirect", totals["total_cost"]),
        (f"K. Small Business Fee ({rec.fee_rate or 7}%)", totals.get("fee_amount", 0)),
    ]
    for label, val in totals_rows:
        ws.merge_cells(f"A{row}:E{row}")
        set_cell(row, 1, label, LABEL)
        c = ws.cell(row=row, column=6, value=val)
        c.font = LABEL; c.number_format = '"$"#,##0'; c.alignment = RIGHT
        row += 1

    # Grand total
    ws.merge_cells(f"A{row}:E{row}")
    set_cell(row, 1, "L. TOTAL AMOUNT REQUESTED", GRAND, FILL_GRAND, CENTER)
    c = ws.cell(row=row, column=6, value=totals.get("total_requested", totals["total_cost"]))
    c.font = GRAND; c.fill = FILL_GRAND; c.number_format = '"$"#,##0'; c.alignment = RIGHT
    cap = _resolve_cap(proposal.agency, proposal.phase)
    ws.cell(row=row, column=7, value=f"Cap: {_money(cap)}").font = Font(name="Calibri", size=9, italic=True, color="888888")
    ws.row_dimensions[row].height = 22

    # Column widths
    col_widths = [28, 22, 16, 12, 12, 16, 16]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    safe = re.sub(r"[^\w\-]", "_", proposal.title or "Budget")[:40]
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="Budget_{safe}.xlsx"'},
    )


# ── Word ──────────────────────────────────────────────────────────────────────

@router.get("/{proposal_id}/export/docx")
async def export_budget_docx(
    proposal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    proposal, rec = await _get_rec_and_proposal(proposal_id, current_user.id, db)
    org = await _get_org(current_user.id, db)
    body = _load_budget_body(rec)
    totals = _calc_totals(body)
    months = rec.budget_months or 12

    from docx import Document
    from docx.shared import Pt, Cm, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    doc = Document()

    # Page margins
    for sec in doc.sections:
        sec.top_margin = Cm(2); sec.bottom_margin = Cm(2)
        sec.left_margin = Cm(2.5); sec.right_margin = Cm(2.5)

    def heading(text, level=1):
        p = doc.add_heading(text, level=level)
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        return p

    def para(text, bold=False, italic=False):
        p = doc.add_paragraph()
        run = p.add_run(text)
        run.bold = bold; run.italic = italic
        return p

    def add_table(headers, rows_data):
        t = doc.add_table(rows=1 + len(rows_data), cols=len(headers))
        t.style = "Table Grid"
        # Header row
        hrow = t.rows[0]
        for i, h in enumerate(headers):
            c = hrow.cells[i]
            c.text = h
            c.paragraphs[0].runs[0].bold = True
            c.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        for ri, rd in enumerate(rows_data, 1):
            for ci, val in enumerate(rd):
                cell = t.rows[ri].cells[ci]
                cell.text = str(val)
                if ci > 1:
                    cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
        doc.add_paragraph()
        return t

    # Title
    title_p = doc.add_heading(_budget_title(proposal), 0)
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    para(f"{proposal.title or 'Proposal'}", bold=True).alignment = WD_ALIGN_PARAGRAPH.CENTER
    para(f"{org}  |  Performance Period: {months} months  |  {datetime.now().strftime('%B %d, %Y')}").alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph()

    # Personnel
    heading("A. Senior/Key Personnel")
    hrs_per_yr = 2080.0
    pers_data = []
    for p in (rec.personnel or []):
        sal = float(p.get("annual_salary") or 0)
        effort = float(p.get("effort_pct") or 0)
        fringe = float(p.get("fringe_rate") or 0)
        sal_cost = sal * (effort / 100) * (months / 12)
        frg_cost = sal_cost * (fringe / 100)
        hours = round((effort / 100) * (months / 12) * hrs_per_yr)
        pers_data.append([p.get("name",""), p.get("role",""), _money(sal), f"{effort}% / {hours}h", _money(sal_cost), _money(frg_cost), _money(sal_cost + frg_cost)])
    add_table(["Name", "Role", "Annual Salary", "Effort / Hours", "Salary Cost", "Fringe", "Total"], pers_data)
    para(f"Personnel + Fringe Subtotal: {_money(totals['personnel_salary'] + totals['personnel_fringe'])}", bold=True)
    doc.add_paragraph()

    # Consultants
    if rec.consultants:
        heading("B. Consultants")
        con_data = [[c.get("name",""), c.get("role",""), _money(c.get("rate_per_day",0)), str(c.get("days",0)), _money(float(c.get("rate_per_day",0)) * float(c.get("days",0)))] for c in rec.consultants]
        add_table(["Name", "Role", "Rate/Day", "Days", "Total"], con_data)
        para(f"Consultants Subtotal: {_money(totals['consultant_total'])}", bold=True)
        doc.add_paragraph()

    # Equipment
    if rec.equipment:
        heading("C. Equipment")
        eq_data = [[e.get("name",""), e.get("description",""), _money(e.get("cost",0))] for e in rec.equipment]
        add_table(["Item", "Description / Justification", "Cost"], eq_data)
        para(f"Equipment Subtotal: {_money(totals['equipment_total'])}", bold=True)
        doc.add_paragraph()

    # Travel
    if rec.travel:
        heading("D. Travel")
        tr_data = [[t.get("purpose",""), t.get("destination",""), str(t.get("trips",0)), str(t.get("people",1)), _money(t.get("cost_per_trip",0)), _money(float(t.get("trips",0))*float(t.get("people",1))*float(t.get("cost_per_trip",0)))] for t in rec.travel]
        add_table(["Purpose", "Destination", "Trips", "People", "Cost/Trip", "Total"], tr_data)
        para(f"Travel Subtotal: {_money(totals['travel_total'])}", bold=True)
        doc.add_paragraph()

    # Other Direct
    if rec.other_direct:
        heading("E. Other Direct Costs")
        od_data = [[o.get("category",""), o.get("description",""), _money(o.get("cost",0))] for o in rec.other_direct]
        add_table(["Category", "Description", "Cost"], od_data)
        para(f"Other Direct Subtotal: {_money(totals['other_total'])}", bold=True)
        doc.add_paragraph()

    # Subcontracts
    if rec.subcontracts:
        heading("F. Subcontracts")
        sub_data = [[s.get("organization",""), s.get("pi_name",""), _money(s.get("total_cost",0)), _money(min(25000, float(s.get("total_cost",0))))] for s in rec.subcontracts]
        add_table(["Organization", "PI", "Total Cost", "MTDC Portion"], sub_data)
        para(f"Subcontracts Subtotal: {_money(totals['subcontract_total'])}", bold=True)
        doc.add_paragraph()

    # Summary table
    heading("Budget Summary")
    summary_rows = [
        ["H. Total Direct Costs", _money(totals["total_direct"])],
        [f"I. Indirect / F&A ({rec.indirect_rate}% on {rec.indirect_base.upper()})", _money(totals["total_indirect"])],
        ["J. Total Direct + Indirect", _money(totals["total_cost"])],
        [f"K. Small Business Fee ({rec.fee_rate or 7}%)", _money(totals.get("fee_amount", 0))],
        ["L. TOTAL AMOUNT REQUESTED", _money(totals.get("total_requested", totals["total_cost"]))],
    ]
    add_table(["Category", "Amount"], summary_rows)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    safe = re.sub(r"[^\w\-]", "_", proposal.title or "Budget")[:40]
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="Budget_{safe}.docx"'},
    )


# ── PDF ───────────────────────────────────────────────────────────────────────

@router.get("/{proposal_id}/export/pdf")
async def export_budget_pdf(
    proposal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    proposal, rec = await _get_rec_and_proposal(proposal_id, current_user.id, db)
    org = await _get_org(current_user.id, db)
    body = _load_budget_body(rec)
    totals = _calc_totals(body)
    months = rec.budget_months or 12

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    buf = io.BytesIO()
    doc_pdf = SimpleDocTemplate(buf, pagesize=letter,
                                leftMargin=0.9*inch, rightMargin=0.9*inch,
                                topMargin=0.9*inch, bottomMargin=0.9*inch)
    styles = getSampleStyleSheet()
    NAVY = colors.HexColor("#1F4E79")
    RED  = colors.HexColor("#C00000")
    LGRAY= colors.HexColor("#D6E4F0")
    MGRAY= colors.HexColor("#2E75B6")

    title_style  = ParagraphStyle("title",  fontSize=15, textColor=colors.white, alignment=TA_CENTER, fontName="Helvetica-Bold", spaceAfter=2)
    sub_style    = ParagraphStyle("sub",    fontSize=10, textColor=colors.white, alignment=TA_CENTER, fontName="Helvetica")
    h2_style     = ParagraphStyle("h2",     fontSize=11, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=10, spaceAfter=4)
    body_style   = ParagraphStyle("body",   fontSize=9,  fontName="Helvetica", spaceAfter=2)
    bold_style   = ParagraphStyle("bold",   fontSize=9,  fontName="Helvetica-Bold", spaceAfter=4)

    elements = []

    # Title block
    title_tbl = Table([[Paragraph(_budget_title(proposal), title_style)],
                       [Paragraph(f"{proposal.title or 'Proposal'}  |  {org}", sub_style)],
                       [Paragraph(f"Performance Period: {months} months   |   {datetime.now().strftime('%B %d, %Y')}", sub_style)]],
                      colWidths=[6.6*inch])
    title_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), NAVY),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [MGRAY, MGRAY]),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
    ]))
    elements += [title_tbl, Spacer(1, 0.2*inch)]

    def section(title, headers, rows, subtotal_label=None, subtotal_val=None):
        elements.append(Paragraph(title, h2_style))
        if rows:
            col_count = len(headers)
            col_w = 6.6 * inch / col_count
            data = [headers] + rows
            tbl = Table(data, colWidths=[col_w] * col_count, repeatRows=1)
            ts = TableStyle([
                ("BACKGROUND", (0,0), (-1,0), MGRAY),
                ("TEXTCOLOR", (0,0), (-1,0), colors.white),
                ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
                ("FONTSIZE", (0,0), (-1,-1), 8),
                ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, LGRAY]),
                ("GRID", (0,0), (-1,-1), 0.4, colors.HexColor("#CCCCCC")),
                ("ALIGN", (0,0), (0,-1), "LEFT"),
                ("ALIGN", (1,0), (-1,-1), "RIGHT"),
                ("TOPPADDING", (0,0), (-1,-1), 4),
                ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ])
            tbl.setStyle(ts)
            elements.append(tbl)
        if subtotal_label:
            elements.append(Paragraph(f"<b>{subtotal_label}:</b> {_money(subtotal_val)}", bold_style))
        elements.append(Spacer(1, 0.1*inch))

    # Personnel
    hrs_per_yr = 2080.0
    pers_rows = []
    for p in (rec.personnel or []):
        sal = float(p.get("annual_salary") or 0)
        effort = float(p.get("effort_pct") or 0)
        fringe = float(p.get("fringe_rate") or 0)
        sc = sal * (effort / 100) * (months / 12)
        fc = sc * (fringe / 100)
        hrs = round((effort / 100) * (months / 12) * hrs_per_yr)
        pers_rows.append([p.get("name",""), p.get("role",""), _money(sal), f"{effort}%/{hrs}h", _money(sc), _money(fc), _money(sc+fc)])
    section("A. Senior/Key Personnel", ["Name","Role","Salary/yr","Effort/Hrs","Salary","Fringe","Total"], pers_rows,
            "Personnel + Fringe", totals["personnel_salary"] + totals["personnel_fringe"])

    if rec.consultants:
        con_rows = [[c.get("name",""), c.get("role",""), _money(c.get("rate_per_day",0)), str(c.get("days",0)), _money(float(c.get("rate_per_day",0))*float(c.get("days",0)))] for c in rec.consultants]
        section("B. Consultants", ["Name","Role","Rate/Day","Days","Total"], con_rows, "Consultants", totals["consultant_total"])

    if rec.equipment:
        eq_rows = [[e.get("name",""), e.get("description","")[:50], _money(e.get("cost",0))] for e in rec.equipment]
        section("C. Equipment", ["Item","Description","Cost"], eq_rows, "Equipment", totals["equipment_total"])

    if rec.travel:
        tr_rows = [[t.get("purpose",""), t.get("destination",""), str(t.get("trips",0)), str(t.get("people",1)), _money(t.get("cost_per_trip",0)), _money(float(t.get("trips",0))*float(t.get("people",1))*float(t.get("cost_per_trip",0)))] for t in rec.travel]
        section("D. Travel", ["Purpose","Destination","Trips","Ppl","$/Trip","Total"], tr_rows, "Travel", totals["travel_total"])

    if rec.other_direct:
        od_rows = [[o.get("category",""), o.get("description","")[:50], _money(o.get("cost",0))] for o in rec.other_direct]
        section("E. Other Direct Costs", ["Category","Description","Cost"], od_rows, "Other Direct", totals["other_total"])

    if rec.subcontracts:
        sub_rows = [[s.get("organization",""), s.get("pi_name",""), _money(s.get("total_cost",0)), _money(min(25000, float(s.get("total_cost",0))))] for s in rec.subcontracts]
        section("F. Subcontracts", ["Organization","PI","Total Cost","MTDC Portion"], sub_rows, "Subcontracts", totals["subcontract_total"])

    # Summary
    elements.append(HRFlowable(width="100%", thickness=1, color=NAVY))
    elements.append(Paragraph("Budget Summary", h2_style))
    sum_data = [
        ["H. Total Direct Costs", _money(totals["total_direct"])],
        [f"I. Indirect / F&A ({rec.indirect_rate}% {rec.indirect_base.upper()})", _money(totals["total_indirect"])],
        ["J. Total Direct + Indirect", _money(totals["total_cost"])],
        [f"K. Small Business Fee ({rec.fee_rate or 7}%)", _money(totals.get("fee_amount",0))],
        ["L. TOTAL AMOUNT REQUESTED", _money(totals.get("total_requested", totals["total_cost"]))],
    ]
    sum_tbl = Table(sum_data, colWidths=[5*inch, 1.6*inch])
    sum_tbl.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-2), "Helvetica"),
        ("FONTNAME", (0,-1), (-1,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("BACKGROUND", (0,-1), (-1,-1), RED),
        ("TEXTCOLOR", (0,-1), (-1,-1), colors.white),
        ("ALIGN", (1,0), (1,-1), "RIGHT"),
        ("ROWBACKGROUNDS", (0,0), (-1,-2), [colors.white, LGRAY]),
        ("GRID", (0,0), (-1,-1), 0.4, colors.HexColor("#CCCCCC")),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
    ]))
    elements.append(sum_tbl)

    doc_pdf.build(elements)
    buf.seek(0)
    safe = re.sub(r"[^\w\-]", "_", proposal.title or "Budget")[:40]
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Budget_{safe}.pdf"'},
    )


# ══════════════════════════════════════════════════════════════════════════════
# JUSTIFICATION EXPORTS (receive text from frontend, return formatted file)
# ══════════════════════════════════════════════════════════════════════════════

from pydantic import BaseModel

class JustificationBody(BaseModel):
    text: str
    title: str = "SBIR Proposal"


@router.post("/{proposal_id}/justification/export/docx")
async def export_justification_docx(
    proposal_id: str,
    body: JustificationBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_proposal(proposal_id, current_user.id, db)

    from docx import Document
    from docx.shared import Pt, Cm, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    for sec in doc.sections:
        sec.top_margin = Cm(2.5); sec.bottom_margin = Cm(2.5)
        sec.left_margin = Cm(3); sec.right_margin = Cm(3)

    NAVY = RGBColor(0x1F, 0x4E, 0x79)
    lines = body.text.split("\n")

    for line in lines:
        stripped = line.strip()
        if not stripped:
            doc.add_paragraph()
            continue

        # Section headers (A. through L. or "Executive Summary" etc.)
        if re.match(r'^[A-L]\.\s', stripped) or stripped in ("Executive Summary", "Budget Summary") or re.match(r'^#{1,3}\s', stripped):
            clean = re.sub(r'^#+\s', '', stripped)
            h = doc.add_heading(clean, level=2)
            for run in h.runs:
                run.font.color.rgb = NAVY
        # Numbered sub-items
        elif re.match(r'^\d+\.\s', stripped):
            p = doc.add_paragraph(style="List Number")
            run = p.add_run(stripped[stripped.index(".")+2:])
            run.bold = True
        # Table rows (markdown)
        elif stripped.startswith("|"):
            cells = [c.strip() for c in stripped.split("|") if c.strip()]
            if all(c.startswith("-") for c in cells):
                continue  # separator row
            t = doc.add_table(rows=1, cols=len(cells))
            t.style = "Table Grid"
            row_cells = t.rows[0].cells
            for i, cell_text in enumerate(cells):
                row_cells[i].text = cell_text
                if i > 0:
                    row_cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
        else:
            doc.add_paragraph(stripped)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    safe = re.sub(r"[^\w\-]", "_", body.title)[:40]
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="Budget_Justification_{safe}.docx"'},
    )


@router.post("/{proposal_id}/justification/export/pdf")
async def export_justification_pdf(
    proposal_id: str,
    body: JustificationBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_proposal(proposal_id, current_user.id, db)

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT, TA_JUSTIFY

    buf = io.BytesIO()
    pdf_doc = SimpleDocTemplate(buf, pagesize=letter,
                                leftMargin=inch, rightMargin=inch,
                                topMargin=inch, bottomMargin=inch)
    NAVY = colors.HexColor("#1F4E79")
    LGRAY= colors.HexColor("#F0F4F8")

    h1  = ParagraphStyle("h1",  fontSize=13, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=14, spaceAfter=4)
    h2  = ParagraphStyle("h2",  fontSize=11, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=10, spaceAfter=3)
    bod = ParagraphStyle("bod", fontSize=9.5, fontName="Helvetica", spaceAfter=5, leading=14, alignment=TA_JUSTIFY)
    num = ParagraphStyle("num", fontSize=9.5, fontName="Helvetica-Bold", spaceAfter=4, leftIndent=12)

    elements = []

    lines = body.text.split("\n")
    i = 0
    table_buffer = []

    while i < len(lines):
        line = lines[i].strip()

        if not line:
            i += 1
            continue

        # Flush table buffer when we hit non-table line
        if table_buffer and not line.startswith("|"):
            _flush_table(elements, table_buffer, LGRAY, NAVY)
            table_buffer = []

        if line.startswith("|"):
            table_buffer.append(line)
            i += 1
            continue

        is_section_h = re.match(r'^[A-L]\.\s', line) or line in ("Executive Summary", "Budget Summary")
        is_md_h = re.match(r'^#+\s', line)
        is_numbered = re.match(r'^\d+\.\s', line)

        if is_md_h:
            clean = re.sub(r'^#+\s+', '', line)
            level = len(re.match(r'^(#+)', line).group(1))
            elements.append(Paragraph(clean, h1 if level <= 2 else h2))
        elif is_section_h:
            elements.append(HRFlowable(width="100%", thickness=0.5, color=NAVY, spaceAfter=2))
            elements.append(Paragraph(line, h1))
        elif is_numbered:
            elements.append(Paragraph(line, num))
        else:
            elements.append(Paragraph(line, bod))
        i += 1

    if table_buffer:
        _flush_table(elements, table_buffer, LGRAY, NAVY)

    pdf_doc.build(elements)
    buf.seek(0)
    safe = re.sub(r"[^\w\-]", "_", body.title)[:40]
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Budget_Justification_{safe}.pdf"'},
    )


def _flush_table(elements, table_buffer, LGRAY, NAVY):
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import Table, TableStyle, Spacer

    rows = []
    for row_line in table_buffer:
        cells = [c.strip() for c in row_line.split("|") if c.strip()]
        if all(set(c) <= set("-: ") for c in cells):
            continue
        rows.append(cells)
    if not rows:
        return
    max_cols = max(len(r) for r in rows)
    padded = [r + [""] * (max_cols - len(r)) for r in rows]
    col_w = 6.5 * inch / max_cols
    tbl = Table(padded, colWidths=[col_w] * max_cols)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2E75B6")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME",   (0, 1), (-1,-1), "Helvetica"),
        ("FONTSIZE",   (0, 0), (-1,-1), 8.5),
        ("ALIGN",      (0, 0), (0, -1), "LEFT"),
        ("ALIGN",      (1, 0), (-1,-1), "RIGHT"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, LGRAY]),
        ("GRID", (0,0), (-1,-1), 0.4, colors.HexColor("#CCCCCC")),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    elements += [tbl, Spacer(1, 0.1 * inch)]
