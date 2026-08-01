"""
Document Extraction Router
Accepts PDF, DOCX, image uploads or pasted text.
Uses GPT-4o to extract structured company/org profile data.
"""

from __future__ import annotations

import base64
import io
import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
import openai

from config import settings
from database import get_db
from models.db_models import User
from routers.auth import get_current_user

router = APIRouter()

_client = None

def _get_client():
    global _client
    if _client is None:
        _client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


EXTRACTION_SYSTEM = """You are an expert at extracting structured company and researcher profile information from documents.

Extract as much relevant information as possible and return it as valid JSON matching exactly the schema below.
Only include fields where you found actual information — use null for missing fields and [] for empty arrays.

Schema:
{
  "organization_name": string | null,
  "industry": string | null,
  "core_technologies": [string],
  "company_capabilities": string | null,
  "uei_number": string | null,
  "cage_code": string | null,
  "pi_name": string | null,
  "pi_degree": string | null,
  "pi_affiliation": string | null,
  "pi_orcid": string | null,
  "pi_publications": integer | null,
  "pi_credentials": string | null,
  "team_members": [
    {
      "name": string,
      "title": string,
      "role": string,
      "credentials": string,
      "effort_pct": number,
      "years_exp": number,
      "orcid": string
    }
  ],
  "facilities": [
    {
      "name": string,
      "type": string,
      "description": string,
      "certifications": [string],
      "sq_footage": number | null,
      "location": string
    }
  ],
  "partners": [
    {
      "name": string,
      "type": "subcontractor" | "research_institution" | "consultant" | "industry",
      "role": string,
      "pi_name": string,
      "location": string,
      "effort_pct": number
    }
  ],
  "past_performance": [
    {
      "title": string,
      "agency": string,
      "award_number": string,
      "amount": string,
      "period": string,
      "outcome": "completed" | "ongoing" | "funded" | "not_funded",
      "relevance": string
    }
  ],
  "extraction_notes": string
}

extraction_notes should describe what you found and any important caveats.
Return ONLY the JSON object — no markdown fences, no explanation outside the JSON."""


def _extract_text_from_pdf(content: bytes) -> str:
    """Extract text from PDF using pdfplumber (better layout) with pypdf fallback."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = []
            for page in pdf.pages[:30]:  # cap at 30 pages
                text = page.extract_text() or ""
                pages.append(text)
        result = "\n\n".join(pages)
        if len(result.strip()) > 100:
            return result
    except Exception:
        pass

    # Fallback to pypdf
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        return "\n\n".join(
            page.extract_text() or "" for page in reader.pages[:30]
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not extract text from PDF: {e}")


def _extract_text_from_docx(content: bytes) -> str:
    """Extract text from DOCX."""
    try:
        from docx import Document
        doc = Document(io.BytesIO(content))
        parts = []
        for para in doc.paragraphs:
            if para.text.strip():
                parts.append(para.text)
        # Also grab table cells
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text.strip():
                        parts.append(cell.text)
        return "\n".join(parts)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not read DOCX: {e}")


def _is_image(content_type: str, filename: str) -> bool:
    img_types = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/tiff"}
    img_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".tif"}
    ct = (content_type or "").lower()
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ct in img_types or ext in img_exts


def _ai_error(exc: Exception) -> HTTPException:
    """Convert any AI service exception to a safe, user-facing HTTPException."""
    import logging, traceback
    log = logging.getLogger(__name__)
    if isinstance(exc, openai.RateLimitError):
        log.error("AI service rate limit exceeded: %s", exc)
        return HTTPException(status_code=503, detail=(
            "The AI service is temporarily unavailable due to high demand. "
            "Please try again in a few minutes."
        ))
    if isinstance(exc, openai.AuthenticationError):
        log.critical("AI service authentication failure: %s", exc)
        return HTTPException(status_code=503, detail="The AI service is not configured correctly. Please contact support.")
    if isinstance(exc, openai.APIConnectionError):
        log.error("AI service connection error: %s", exc)
        return HTTPException(status_code=503, detail="The AI service is unreachable. Please try again in a moment.")
    if isinstance(exc, openai.APIStatusError):
        log.error("AI service API error %s: %s", exc.status_code, exc.message)
        return HTTPException(status_code=503, detail="The AI service returned an unexpected error. Please try again.")
    log.error("Unexpected AI error:\n%s", traceback.format_exc())
    return HTTPException(status_code=500, detail="An unexpected error occurred. Please try again or contact support.")


async def _call_gpt_text(text: str, source_hint: str) -> dict:
    """Send extracted text to GPT-4o for structured extraction."""
    client = _get_client()
    # Truncate to ~15k chars to stay within token limits
    text_truncated = text[:15000] + ("...[truncated]" if len(text) > 15000 else "")

    try:
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM},
                {"role": "user", "content": f"Source: {source_hint}\n\n---\n\n{text_truncated}"},
            ],
            temperature=0.1,
            max_tokens=3000,
        )
    except Exception as exc:
        raise _ai_error(exc)
    raw = response.choices[0].message.content.strip()
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="The AI returned an unexpected response. Please try again.")


async def _call_gpt_vision(image_bytes: bytes, content_type: str, source_hint: str) -> dict:
    """Send image to GPT-4o vision for structured extraction."""
    client = _get_client()
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    mime = content_type if content_type.startswith("image/") else "image/jpeg"

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Extract all company/researcher profile information from this document image. Source: {source_hint}"},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "high"}},
                    ],
                },
            ],
            temperature=0.1,
            max_tokens=3000,
        )
    except Exception as exc:
        raise _ai_error(exc)
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="The AI could not process this image. Please try again.")


@router.post("/extract-profile")
async def extract_profile(
    file: Optional[UploadFile] = File(None),
    pasted_text: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Extract profile fields from a document or pasted text.
    Accepts: PDF, DOCX, DOC, PNG, JPG, GIF, WEBP, or plain text.
    Returns structured profile JSON for preview before saving.
    """
    if not file and not pasted_text:
        raise HTTPException(status_code=400, detail="Provide either a file or pasted text.")

    if file:
        content = await file.read()
        filename = file.filename or ""
        content_type = file.content_type or ""
        fname_lower = filename.lower()

        if _is_image(content_type, filename):
            extracted = await _call_gpt_vision(content, content_type, filename)

        elif fname_lower.endswith(".pdf") or content_type == "application/pdf":
            text = _extract_text_from_pdf(content)
            if not text.strip():
                raise HTTPException(status_code=422, detail="PDF appears to be scanned/image-only. Try uploading as image instead.")
            extracted = await _call_gpt_text(text, filename)

        elif fname_lower.endswith((".docx", ".doc")) or "word" in content_type:
            text = _extract_text_from_docx(content)
            extracted = await _call_gpt_text(text, filename)

        elif fname_lower.endswith(".txt") or content_type.startswith("text/"):
            text = content.decode("utf-8", errors="replace")
            extracted = await _call_gpt_text(text, filename)

        else:
            # Try as text, fall back gracefully
            try:
                text = content.decode("utf-8", errors="replace")
                extracted = await _call_gpt_text(text, filename)
            except Exception:
                raise HTTPException(
                    status_code=415,
                    detail=f"Unsupported file type: {content_type or fname_lower}. Use PDF, DOCX, image, or TXT."
                )
    else:
        extracted = await _call_gpt_text(pasted_text, "pasted text")

    # Clean up: remove null values but keep empty arrays
    cleaned = {k: v for k, v in extracted.items() if v is not None or isinstance(v, list)}
    return {"extracted": cleaned, "field_count": len([v for v in cleaned.values() if v])}
