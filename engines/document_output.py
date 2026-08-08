"""
Engine 9 — Document Output Engine
Produces TXT, DOCX, and PDF exports of completed proposals.

Version 3.0 upgrade, "Real File Storage" scope (Phase B; see docs/
Clariva_File_Storage_Scoping_Document.docx) — export() used to write the
generated file to the API dyno's local disk (tempfile.gettempdir()/
sbir_exports/) and hand back a relative download_url read back from that
same path by routers/documents.py::download_file(). That local-disk path
never survived a Heroku dyno restart/deploy and would 404 the moment
traffic landed on a different dyno than the one that generated the file —
a live reliability bug, not a missing feature. Every exporter below now
writes into an in-memory io.BytesIO buffer instead (the exact pattern
already proven in routers/budget_export.py's six export endpoints), and
export() uploads that buffer to Cloudflare R2 via storage.py, logs a
StoredFile row, and returns a real presigned download URL. Nothing is ever
written to local disk anymore.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

import storage
from models.db_models import StoredFile, new_uuid


AI_DISCLAIMER = (
    "AI-GENERATED CONTENT DISCLAIMER: This document was produced using artificial "
    "intelligence and is provided as a DRAFT ONLY. It may contain errors, omissions, "
    "or inaccuracies. This content MUST be thoroughly reviewed, verified, and approved "
    "by a qualified professional — including a subject-matter expert, grant writer, "
    "compliance officer, or legal advisor — before submission to any funding agency. "
    "Clariva is a productivity and drafting tool, not a substitute for professional "
    "judgment. The submitting organization bears sole responsibility for the accuracy, "
    "compliance, and appropriateness of any content submitted under its name."
)


class DocumentOutputEngine:
    """
    Generates submission-ready documents in TXT, DOCX, and PDF formats.
    """

    # Content-Type per format, for the R2 object metadata and the
    # presigned URL's Content-Disposition header.
    _CONTENT_TYPES = {
        "txt": "text/plain",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pdf": "application/pdf",
    }

    async def export(
        self,
        proposal: Any,
        sections: List[Any],
        fmt: str,
        db: AsyncSession,
        created_by: str,
        org_id: Optional[str] = None,
        include_scoring: bool = True,
        include_reviewer: bool = False,
        include_compliance: bool = True,
        generate_figures: bool = False,
        format_options: Any = None,   # ExportFormatOptions schema object or None
    ) -> Dict[str, Any]:
        """Route to format-specific exporter, then persist the result to R2
        (storage.py) and log a StoredFile row. Flushes but does not commit —
        same "engines flush, routers commit" convention as every other
        engine in this codebase (e.g. award_engine.py)."""
        from utils.doc_utils import FormatOptions

        # Convert Pydantic schema → dataclass (None preserves all defaults)
        opts: FormatOptions | None = None
        if format_options is not None:
            opts = FormatOptions(
                font=format_options.font,
                font_pt=format_options.font_pt,
                alignment=format_options.alignment,
                margins_in=format_options.margins_in,
                page_num_position=format_options.page_num_position,
                cover_page_number=format_options.cover_page_number,
                page_break_h1=format_options.page_break_h1,
                section_numbering=format_options.section_numbering,
                space_after_pt=format_options.space_after_pt,
                space_before_h1_pt=format_options.space_before_h1_pt,
            )

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        safe_title = "".join(c if c.isalnum() or c in "-_" else "_" for c in proposal.title[:40])
        filename = f"{safe_title}_{timestamp}.{fmt}"

        # Pre-generate figures for DOCX/PDF when requested
        figures: Dict[str, bytes] = {}
        if generate_figures and fmt in ("docx", "pdf"):
            figures = await self._generate_figures(proposal, sections)

        buffer = io.BytesIO()
        if fmt == "txt":
            self._export_txt(buffer, proposal, sections)
        elif fmt == "docx":
            self._export_docx(buffer, proposal, sections, figures=figures, opts=opts)
        elif fmt == "pdf":
            self._export_pdf(buffer, proposal, sections)
        else:
            raise ValueError(f"Unsupported format: {fmt}")

        content = buffer.getvalue()
        content_type = self._CONTENT_TYPES.get(fmt, "application/octet-stream")

        storage_key = await storage.upload_file(org_id, "proposal_export", content, filename, content_type)
        stored_file = StoredFile(
            id=new_uuid(), org_id=org_id, object_type="proposal_export", object_id=proposal.id,
            storage_key=storage_key, original_filename=filename, content_type=content_type,
            size_bytes=len(content), checksum=storage.sha256_hex(content), created_by=created_by,
        )
        db.add(stored_file)
        await db.flush()
        download_url = await storage.get_download_url(storage_key, filename=filename)

        return {
            "download_url": download_url,
            "file_size": len(content),
            "exported_at": datetime.utcnow(),
        }

    # ── Figure pre-generation ─────────────────────────────────────────────────

    async def _generate_figures(self, proposal: Any, sections: List[Any]) -> Dict[str, bytes]:
        """
        Scan all section content for [FIGURE N: ...] and [IMAGE N: ...] markers,
        generate an image for each unique caption, and return a caption→bytes dict.
        """
        import re
        from engines import image_gen
        from config import settings

        MARKER_RE = re.compile(
            r'\[(?:FIGURE|IMAGE)\s*\d*\s*:\s*(.*?)\]',
            re.IGNORECASE,
        )
        captions: List[str] = []
        seen: set = set()
        for sec in sections:
            if not sec.content:
                continue
            for m in MARKER_RE.finditer(sec.content):
                cap = m.group(1).strip()
                if cap and cap not in seen:
                    seen.add(cap)
                    captions.append(cap)

        figures: Dict[str, bytes] = {}
        for cap in captions:
            img = await image_gen.generate_figure(cap, proposal.title, settings.OPENAI_API_KEY)
            if img:
                figures[cap] = img

        return figures

    # ── TXT ───────────────────────────────────────────────────────────────────

    def _export_txt(self, buffer: io.BytesIO, proposal: Any, sections: List[Any]) -> None:
        import textwrap
        disclaimer_wrapped = "\n".join(textwrap.wrap(AI_DISCLAIMER, width=68))
        lines = [
            "=" * 70,
            proposal.title.upper(),
            f"Agency: {proposal.agency}  |  Phase: {proposal.phase.replace('_', ' ').title()}",
            f"Status: {proposal.status}  |  Version: {proposal.version}",
            "=" * 70,
            "",
            "⚠  " + "─" * 65,
            disclaimer_wrapped,
            "─" * 68,
            "",
        ]
        for section in sections:
            if section.content:
                lines += [
                    f"\n{'─' * 70}",
                    f"  {section.title.upper()}",
                    f"{'─' * 70}",
                    "",
                    section.content,
                    "",
                ]
        lines += [
            "=" * 70,
            f"Generated by Clariva Intelligent Grant Writing Platform™",
            f"Exported: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
            "=" * 70,
        ]

        buffer.write("\n".join(lines).encode("utf-8"))

    # ── DOCX ──────────────────────────────────────────────────────────────────

    def _export_docx(self, buffer: io.BytesIO, proposal: Any, sections: List[Any],
                     figures: Dict[str, bytes] = {}, opts=None) -> None:
        try:
            from docx import Document
            from docx.shared import Pt, RGBColor
            from docx.oxml.ns import qn
            from docx.oxml import OxmlElement
            from docx.enum.text import WD_ALIGN_PARAGRAPH
            from utils.doc_utils import (
                apply_federal_margins, add_federal_heading, add_body_para,
                add_page_numbers, render_content, _make_run, _para_spacing,
                FEDERAL_FONT, H1_PT,
            )

            font = opts.font if opts else FEDERAL_FONT

            doc = Document()
            apply_federal_margins(doc, opts=opts)

            # ── Title ─────────────────────────────────────────────────────────
            title_para = doc.add_paragraph()
            title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _para_spacing(title_para, after=4, before=0)
            _make_run(title_para, proposal.title, bold=True, pt=H1_PT, font=font)

            # Metadata line
            meta = doc.add_paragraph()
            meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _para_spacing(meta, after=6)
            _make_run(meta,
                      f"Agency: {proposal.agency}  |  "
                      f"Phase: {proposal.phase.replace('_', ' ').title()}  |  "
                      f"Version: {proposal.version}",
                      pt=10, font=font)

            # ── AI Disclaimer box ─────────────────────────────────────────────
            disclaimer_para = doc.add_paragraph()
            _para_spacing(disclaimer_para, after=8)
            dr = disclaimer_para.add_run("AI-GENERATED DRAFT — REQUIRES PROFESSIONAL REVIEW\n")
            dr.bold = True; dr.font.size = Pt(9)
            dr.font.color.rgb = RGBColor(0x92, 0x40, 0x0E)
            br = disclaimer_para.add_run(AI_DISCLAIMER)
            br.font.size = Pt(9)
            br.font.color.rgb = RGBColor(0x44, 0x33, 0x00)
            pPr = disclaimer_para._p.get_or_add_pPr()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto")
            shd.set(qn("w:fill"), "FFF3CD")
            pPr.append(shd)
            pBdr = OxmlElement("w:pBdr")
            for side in ("top", "left", "bottom", "right"):
                bdr = OxmlElement(f"w:{side}")
                bdr.set(qn("w:val"), "single"); bdr.set(qn("w:sz"), "6")
                bdr.set(qn("w:space"), "4"); bdr.set(qn("w:color"), "D97706")
                pBdr.append(bdr)
            pPr.append(pBdr)
            # ─────────────────────────────────────────────────────────────────

            # ── Sections ──────────────────────────────────────────────────────
            for sec in sections:
                if not sec.content:
                    continue
                add_federal_heading(doc, sec.title, level=1, opts=opts)
                render_content(doc, sec.content, figures=figures, opts=opts)

            # ── Page numbers ──────────────────────────────────────────────────
            add_page_numbers(doc, opts=opts)

            doc.save(buffer)

        except ImportError:
            self._export_txt(buffer, proposal, sections)

    # ── PDF ───────────────────────────────────────────────────────────────────

    def _export_pdf(self, buffer: io.BytesIO, proposal: Any, sections: List[Any]) -> None:
        try:
            from reportlab.lib.pagesizes import LETTER
            from reportlab.lib.styles import ParagraphStyle
            from reportlab.lib.units import inch
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
            from reportlab.lib import colors
            from utils.doc_utils import get_pdf_styles, pdf_page_number, strip_for_pdf

            S = get_pdf_styles()
            NAVY = colors.HexColor("#1F4E79")

            disclaimer_style = ParagraphStyle(
                "DisclaimerStyle", fontName="Times-Roman", fontSize=9,
                leading=13, spaceAfter=6,
                backColor=colors.HexColor("#FFF3CD"),
                borderColor=colors.HexColor("#D97706"),
                borderWidth=1, borderPadding=8,
                textColor=colors.HexColor("#443300"),
            )
            disclaimer_label = ParagraphStyle(
                "DisclaimerLabel", fontName="Times-Bold", fontSize=9,
                textColor=colors.HexColor("#92400E"),
                backColor=colors.HexColor("#FFF3CD"),
                borderPadding=8,
            )

            doc_obj = SimpleDocTemplate(
                buffer, pagesize=LETTER,
                leftMargin=inch, rightMargin=inch,
                topMargin=inch, bottomMargin=inch,
            )

            story = [
                Paragraph(strip_for_pdf(proposal.title), S["title"]),
                Paragraph(
                    f"Agency: {proposal.agency}  |  "
                    f"Phase: {proposal.phase.replace('_', ' ').title()}",
                    S["small"],
                ),
                Spacer(1, 0.15 * inch),
                HRFlowable(width="100%", thickness=1, color=NAVY),
                Spacer(1, 0.08 * inch),
                Paragraph("AI-GENERATED DRAFT — REQUIRES PROFESSIONAL REVIEW", disclaimer_label),
                Paragraph(
                    AI_DISCLAIMER.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
                    disclaimer_style,
                ),
                Spacer(1, 0.15 * inch),
            ]

            for sec in sections:
                if not sec.content:
                    continue
                story.append(Paragraph(strip_for_pdf(sec.title), S["h1"]))
                for para_text in sec.content.split("\n\n"):
                    para_text = para_text.strip()
                    if not para_text:
                        continue
                    import re
                    if re.match(r'^#{1,3}\s', para_text) or re.match(r'^[A-Z]\.\s', para_text):
                        story.append(Paragraph(strip_for_pdf(para_text), S["h2"]))
                    else:
                        story.append(Paragraph(strip_for_pdf(para_text), S["body"]))
                story.append(Spacer(1, 0.12 * inch))

            story += [
                HRFlowable(width="100%", thickness=0.5, color=colors.grey),
                Spacer(1, 0.05 * inch),
                Paragraph(
                    f"<i>Generated by Clariva SBIR Intelligence Platform  |  "
                    f"{datetime.utcnow().strftime('%Y-%m-%d')}</i>",
                    S["small"],
                ),
            ]

            doc_obj.build(story,
                          onFirstPage=pdf_page_number,
                          onLaterPages=pdf_page_number)

        except ImportError:
            self._export_txt(buffer, proposal, sections)
