"""AI-powered field suggestion endpoint.

Accepts the current value of a proposal form field + grant context
and returns 3 alternative framings tailored to the selected grant program.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

import openai
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from routers.auth import get_current_user
from config import settings
from database import get_db
from models.db_models import User
from engines import usage_tracking
from engines.usage_tracking import usage_from_response

router = APIRouter()
_log = logging.getLogger(__name__)

FIELD_LABELS: dict[str, str] = {
    "title":                  "proposal title",
    "research_focus":         "research focus / problem statement",
    "innovation_description": "innovation description",
    "commercialization_plan": "commercialization / impact plan",
    "team_description":       "team qualifications description",
    "industry":               "industry or research area",
    "core_technologies":      "core technologies / keywords",
}


class SuggestRequest(BaseModel):
    field: str
    current_value: str
    grant_type: Optional[str] = "federal_other"
    program_label: Optional[str] = None
    beneficiary_type: Optional[str] = None
    funder_class: Optional[str] = None
    agency: Optional[str] = None
    grantor_name: Optional[str] = None


class SuggestResponse(BaseModel):
    suggestions: List[str]


@router.post("", response_model=SuggestResponse)
async def suggest_field_alternatives(
    body: SuggestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return 3 AI-generated alternative framings for a proposal form field."""
    field_label = FIELD_LABELS.get(body.field, body.field.replace("_", " "))

    # Build grant context block from Steps 1/2/3 ground truth
    ctx_lines: list[str] = []
    if body.program_label:
        ctx_lines.append(f"Grant Program: {body.program_label}")
    if body.agency:
        ctx_lines.append(f"Agency: {body.agency}")
    if body.funder_class:
        ctx_lines.append(f"Funder Type: {body.funder_class}")
    if body.beneficiary_type:
        ctx_lines.append(f"Target Beneficiary: {body.beneficiary_type}")
    if body.grantor_name:
        ctx_lines.append(f"Grantor / RFP: {body.grantor_name}")
    grant_context = "\n".join(ctx_lines) if ctx_lines else "Federal grant program"

    system_prompt = f"""You are an expert grant writer helping applicants craft compelling, funder-aligned grant proposal language.

GRANT CONTEXT (from the applicant's grant wizard selections):
{grant_context}

Your task: suggest 3 alternative framings for the field described below.
Rules:
- Each suggestion must be specifically tailored to the grant context above
- Vary the approach: e.g. problem-first, outcome-first, innovation-first
- Be concise, professional, compelling — never generic boilerplate
- Match the length and tone appropriate for the field type
- Return ONLY a valid JSON array of exactly 3 strings. No markdown, no explanation.

Example output: ["option 1", "option 2", "option 3"]"""

    user_prompt = (
        f'Field: {field_label}\n'
        f'User\'s current draft: "{body.current_value}"\n\n'
        f'Provide 3 stronger, grant-aligned alternatives for this {field_label}.'
    )

    try:
        client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.85,
            max_tokens=600,
        )
        # Phase 3 §4.7 — Administrator-Only Engineering Economics. This
        # endpoint is unmetered (free) — no credit debit here — so
        # price_cents_charged stays 0; we still record COGS for margin math.
        prompt_tokens, completion_tokens = usage_from_response(response)
        await usage_tracking.record_usage(
            db, org_id=None, user_id=current_user.id, operation="suggest:field_alternatives",
            model="gpt-4o-mini", prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            price_cents_charged=0, reference={"field": body.field},
        )

        raw = response.choices[0].message.content.strip()

        # Strip markdown code fences if the model wrapped its output
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        suggestions = json.loads(raw)
        if not isinstance(suggestions, list) or len(suggestions) < 1:
            raise ValueError("Invalid response structure")

        return SuggestResponse(suggestions=[str(s) for s in suggestions[:3]])

    except openai.RateLimitError as exc:
        _log.error("Suggest rate limit: %s", exc)
        raise HTTPException(status_code=503, detail="The AI service is temporarily unavailable. Please try again in a moment.")
    except openai.AuthenticationError as exc:
        _log.critical("Suggest auth error: %s", exc)
        raise HTTPException(status_code=503, detail="The AI service is not configured correctly. Please contact support.")
    except openai.APIConnectionError as exc:
        _log.error("Suggest connection error: %s", exc)
        raise HTTPException(status_code=503, detail="The AI service is unreachable. Please try again.")
    except Exception as exc:
        _log.error("Suggest unexpected error: %s", exc)
        raise HTTPException(status_code=500, detail="Could not generate suggestions. Please try again.")
