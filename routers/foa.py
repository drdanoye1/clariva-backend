"""FOA router — upload, parse, retrieve funding opportunity announcements."""

from __future__ import annotations

import re
import uuid
from html.parser import HTMLParser
from typing import List

import httpx
import openai
from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models.db_models import FOARecord, User
from models.schemas import FOATemplate, FOAUploadResponse
from routers.auth import get_current_user
from engines.foa_parser import FOAParserEngine
from engines.template_builder import FOATemplateBuilderEngine

router = APIRouter()
parser  = FOAParserEngine()
builder = FOATemplateBuilderEngine()


def _clean_parse_error(exc: Exception) -> HTTPException:
    """
    Convert any FOA parse exception into a safe, user-facing HTTPException.
    Full technical details are written to server logs only — never exposed to users.
    """
    import logging, traceback
    log = logging.getLogger(__name__)

    if isinstance(exc, openai.RateLimitError):
        log.error("AI service rate limit exceeded: %s", exc)
        return HTTPException(
            status_code=503,
            detail=(
                "The AI parsing service is temporarily unavailable due to high demand. "
                "Please try again in a few minutes. If the problem persists, contact support."
            ),
        )
    if isinstance(exc, openai.AuthenticationError):
        log.critical("AI service authentication failure: %s", exc)
        return HTTPException(
            status_code=503,
            detail="The AI parsing service is not configured correctly. Please contact support.",
        )
    if isinstance(exc, openai.APIConnectionError):
        log.error("AI service connection error: %s", exc)
        return HTTPException(
            status_code=503,
            detail="The AI parsing service is unreachable. Please try again in a moment.",
        )
    if isinstance(exc, openai.APIStatusError):
        log.error("AI service API error %s: %s", exc.status_code, exc.message)
        return HTTPException(
            status_code=503,
            detail="The AI parsing service returned an unexpected error. Please try again.",
        )
    # Unknown error — log full traceback for admin, return generic message to user
    log.error("Unexpected FOA parse error:\n%s", traceback.format_exc())
    return HTTPException(
        status_code=500,
        detail="Could not process the document. Please try again or contact support.",
    )


# ── HTML → plain text helper ──────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """Lightweight HTML-to-text extractor using stdlib only."""
    SKIP_TAGS = {"script", "style", "head", "nav", "footer", "header", "noscript"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag.lower() in self.SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag.lower() in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self.chunks.append(stripped + " ")

    def get_text(self) -> str:
        raw = "".join(self.chunks)
        # Collapse runs of blank lines
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return extractor.get_text()


@router.post("/upload", response_model=FOAUploadResponse)
async def upload_foa(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a FOA PDF, DOCX, or TXT file. Parses it and builds a dynamic template."""
    ACCEPTED = {
        "application/pdf",
        "text/plain",
        "application/octet-stream",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "application/x-pdf",
    }
    ct = (file.content_type or "").split(";")[0].strip()
    fname = (file.filename or "").lower()
    if ct not in ACCEPTED and not any(fname.endswith(e) for e in (".pdf", ".txt", ".docx", ".doc")):
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ct}. Use PDF, DOCX, or TXT.")

    try:
        raw_bytes = await file.read()
        raw_text  = parser.extract_text(raw_bytes, file.filename or "foa.pdf")
        parsed    = await parser.parse(raw_text)
        template  = builder.build(parsed)

        record = FOARecord(
            id=str(uuid.uuid4()),
            agency=template.agency.value,
            program_title=template.program_title,
            solicitation_number=template.solicitation_number,
            phase=template.phase.value,
            raw_text=raw_text[:50000],
            parsed_template=template.model_dump(mode="json"),
            total_page_limit=template.total_page_limit,
            deadline=template.deadline,
            uploaded_by=current_user.id,
        )
        db.add(record)
        await db.flush()
    except Exception as exc:
        raise _clean_parse_error(exc)

    return FOAUploadResponse(
        foa_id=record.id,
        parsed=True,
        template=template,
        raw_text_preview=raw_text[:500],
    )


@router.post("/parse-text", response_model=FOAUploadResponse)
async def parse_foa_text(
    text: str = Body(..., embed=True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Parse pasted FOA text directly (no file upload needed)."""
    if not text or len(text.strip()) < 50:
        raise HTTPException(status_code=400, detail="Please paste at least 50 characters of FOA text.")
    try:
        parsed   = await parser.parse(text.strip())
        template = builder.build(parsed)

        record = FOARecord(
            id=str(uuid.uuid4()),
            agency=template.agency.value,
            program_title=template.program_title,
            solicitation_number=template.solicitation_number,
            phase=template.phase.value,
            raw_text=text[:50000],
            parsed_template=template.model_dump(mode="json"),
            total_page_limit=template.total_page_limit,
            deadline=template.deadline,
            uploaded_by=current_user.id,
        )
        db.add(record)
        await db.flush()
    except Exception as exc:
        raise _clean_parse_error(exc)

    return FOAUploadResponse(
        foa_id=record.id,
        parsed=True,
        template=template,
        raw_text_preview=text[:500],
    )


@router.post("/parse-url", response_model=FOAUploadResponse)
async def parse_foa_url(
    url: str = Body(..., embed=True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch a URL (grants.gov, agency site, PDF link, etc.) and parse it as a FOA."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    # ── Fetch the URL ─────────────────────────────────────────────────────────
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; Clariva-GrantBot/1.0; "
            "+https://ai-grant-generator.vercel.app)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain,*/*",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=422,
                detail=f"Could not fetch URL (HTTP {resp.status_code}). "
                       "Try downloading the PDF and uploading it directly.",
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=422, detail="Request timed out. The URL may be slow or unavailable.")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=422, detail=f"Network error fetching URL: {exc}")

    # ── Extract text based on content type ────────────────────────────────────
    ct = resp.headers.get("content-type", "").lower()
    if "pdf" in ct or url.lower().endswith(".pdf"):
        # Treat as PDF bytes
        raw_text = parser.extract_text(resp.content, "foa.pdf")
    elif "text/plain" in ct or url.lower().endswith(".txt"):
        raw_text = resp.text
    else:
        # HTML page — strip tags
        raw_text = _html_to_text(resp.text)

    raw_text = raw_text.strip()
    if len(raw_text) < 100:
        raise HTTPException(
            status_code=422,
            detail="Could not extract meaningful text from that URL. "
                   "Try downloading the PDF and uploading it directly.",
        )

    # Limit to 60 000 chars for the GPT-4o context window
    raw_text = raw_text[:60_000]

    # ── Parse & store ─────────────────────────────────────────────────────────
    try:
        parsed   = await parser.parse(raw_text)
        template = builder.build(parsed)

        record = FOARecord(
            id=str(uuid.uuid4()),
            agency=template.agency.value,
            program_title=template.program_title,
            solicitation_number=template.solicitation_number,
            phase=template.phase.value,
            raw_text=raw_text[:50_000],
            parsed_template=template.model_dump(mode="json"),
            total_page_limit=template.total_page_limit,
            deadline=template.deadline,
            uploaded_by=current_user.id,
        )
        db.add(record)
        await db.flush()
    except Exception as exc:
        raise _clean_parse_error(exc)

    return FOAUploadResponse(
        foa_id=record.id,
        parsed=True,
        template=template,
        raw_text_preview=raw_text[:500],
    )


@router.get("/{foa_id}", response_model=FOATemplate)
async def get_foa(
    foa_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(FOARecord).where(FOARecord.id == foa_id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="FOA not found")
    return FOATemplate(**record.parsed_template)


@router.get("/", response_model=List[dict])
async def list_foas(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(FOARecord.id, FOARecord.program_title, FOARecord.agency,
               FOARecord.phase, FOARecord.deadline, FOARecord.created_at)
        .where(FOARecord.uploaded_by == current_user.id)
        .order_by(FOARecord.created_at.desc())
    )
    rows = result.all()
    return [
        {
            "foa_id": r.id,
            "program_title": r.program_title,
            "agency": r.agency,
            "phase": r.phase,
            "deadline": r.deadline,
            "created_at": r.created_at,
        }
        for r in rows
    ]
