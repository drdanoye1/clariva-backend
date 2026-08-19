"""
Engine — Future-Tense Validator (Word/PDF Report Generation Development
Specification, CLARIVA-DOCGEN-SPEC-001, Phase 6).

Grant proposals describe PROPOSED (not-yet-performed) work and must use
future tense for it ("we will conduct", "the team will develop"). Past tense
is only correct when describing the applicant's already-completed track
record, prior work, or established facts ("our prior award demonstrated...",
"the facility was built in 2019"). Present tense is fine for ongoing
conditions/general truths.

This is deliberately a second LLM pass over already-generated section text,
not a regex/rules-based checker — an explicit product decision (see the
docstring on TenseCheckOutput in models/schemas.py) because only a model with
surrounding context can tell "we developed this approach" describing PAST
performance (correct) from the same sentence describing PROPOSED work
(wrong — should be future tense). A rules-based checker can't make that call
reliably.

Mirrors engines/supporting_documents_engine.py::generate_logic_model()'s
established pattern: response_format=json_object, parse, pydantic-validate,
retry once on ValidationError, record usage. Uses its own local _ai_error()
copy rather than importing one — this codebase's documented convention (see
the same docstring in supporting_documents_engine.py, scope_of_work_engine.py,
funding_strategy_engine.py, award_engine.py) is one local copy per module,
not a shared helper.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

import openai
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from engines import usage_tracking
from engines.usage_tracking import usage_from_response
from models.schemas import TenseCheckOutput, TenseValidationOut

_log = logging.getLogger(__name__)


def _ai_error(exc: Exception) -> HTTPException:
    """Local copy per this codebase's convention — no provider names exposed."""
    if isinstance(exc, openai.RateLimitError):
        _log.error("AI service rate limit exceeded: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is temporarily unavailable due to high demand. Please try again in a few minutes.")
    if isinstance(exc, openai.AuthenticationError):
        _log.critical("AI service authentication failure: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is not configured correctly. Please contact support.")
    if isinstance(exc, openai.APIConnectionError):
        _log.error("AI service connection error: %s", exc)
        return HTTPException(status_code=503, detail="The AI writing service is unreachable. Please try again in a moment.")
    if isinstance(exc, openai.APIStatusError):
        _log.error("AI service API error %s: %s", getattr(exc, "status_code", "?"), getattr(exc, "message", str(exc)))
        return HTTPException(status_code=503, detail="The AI writing service returned an unexpected error. Please try again.")
    _log.error("Unexpected tense-validation error:\n%s", traceback.format_exc())
    return HTTPException(status_code=500, detail="Tense validation failed. Please try again or contact support.")


def _parse_json_response(raw: str) -> Dict[str, Any]:
    """Strip markdown fences and parse JSON, with a brace-scan fallback —
    local copy of the helper duplicated across this codebase's other
    JSON-generating engines (supporting_documents_engine.py, etc.)."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}") + 1
        try:
            return json.loads(cleaned[start:end])
        except Exception:
            raise HTTPException(status_code=500, detail="Could not parse the AI tense-check response. Please try again.")


_SYSTEM_PROMPT = """You are a meticulous grant-compliance editor specializing in verb tense.

Grant proposals describe PROPOSED work — activities the applicant has NOT yet performed and
is asking to be funded to perform. That work MUST be described in future tense ("we will
conduct", "the team will develop", "this phase will produce").

Past tense is CORRECT only when describing:
- The applicant's already-completed track record or prior awards
- Existing facilities, capabilities, or established facts
- Cited literature, prior research, or historical context

Present tense is CORRECT for ongoing conditions, general truths, or descriptions of the
current state of the art / current problem.

Your job: read the section text and flag every sentence or clause where the WRONG tense is
used for its context — most commonly, past or present tense used to describe work that is
being proposed (not yet done). Do NOT flag correct uses of past tense for genuine past
performance or historical fact. Do NOT flag stylistic issues unrelated to tense. Be
conservative — only flag a clear, unambiguous tense mismatch, not defensible or good English.

Respond with ONLY a JSON object (no markdown fences, no commentary):
{"clean": true|false, "issues": [{"quote": "<the exact offending sentence or clause, verbatim from the text>", "problem": "<one sentence explaining the tense mismatch>", "suggested_fix": "<the same sentence rewritten in the correct tense>"}, ...]}

If there are no issues, return {"clean": true, "issues": []}."""


class TenseValidatorEngine:
    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    async def validate_section(
        self,
        section_id: str,
        section_title: str,
        content: str,
        grant_label: str = "",
        db: Optional[AsyncSession] = None,
        org_id: Optional[str] = None,
        user_id: Optional[str] = None,
        price_cents_charged: int = 0,
    ) -> TenseValidationOut:
        """Run the tense-consistency LLM pass over one section's plain-text
        content. Returns a TenseValidationOut (never raises for a "dirty"
        result — a nonempty `issues` list is a normal, successful outcome,
        not an error). Raises HTTPException only on a genuine AI-service or
        double-validation failure, same as generate_logic_model()."""

        if not content or not content.strip():
            # Nothing to check — an empty section can't have a tense problem.
            # No AI call, no charge; the caller decides whether to still
            # record_usage (it won't, since there's no _usage to report).
            return TenseValidationOut(
                section_id=section_id, title=section_title, clean=True, issues=[],
                checked_at=datetime.utcnow(),
            )

        prompt = f"""Section: "{section_title}"{f' (for a {grant_label} proposal)' if grant_label else ''}

Section text:
{content}

Check this text for tense-consistency issues as instructed."""

        async def _call():
            try:
                response = await self.client.chat.completions.create(
                    model=settings.OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,   # low — this is a consistency check, not creative writing
                    max_tokens=1500,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                raise _ai_error(exc)
            data = _parse_json_response(response.choices[0].message.content)
            return data, response

        data, response = await _call()
        try:
            validated = TenseCheckOutput.model_validate(data)
        except ValidationError:
            # Same "attempt one structured regeneration if validation fails"
            # rule as generate_logic_model() — never surface malformed JSON.
            data, response = await _call()
            try:
                validated = TenseCheckOutput.model_validate(data)
            except ValidationError as exc:
                _log.error("Tense validation for section %s failed schema validation twice: %s", section_id, exc)
                raise HTTPException(status_code=500, detail="Could not complete tense validation for this section. Please try again.")

        if db is not None:
            prompt_tokens, completion_tokens = usage_from_response(response)
            await usage_tracking.record_usage(
                db, org_id=org_id, user_id=user_id, operation="proposal:validate_tense",
                model=settings.OPENAI_MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                price_cents_charged=price_cents_charged, reference={"section_id": section_id},
            )

        return TenseValidationOut(
            section_id=section_id,
            title=section_title,
            clean=validated.clean and not validated.issues,
            issues=validated.issues,
            checked_at=datetime.utcnow(),
        )
