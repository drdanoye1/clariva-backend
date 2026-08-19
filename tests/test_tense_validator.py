"""
Engine — Future-Tense Validator (Phase 6, CLARIVA-DOCGEN-SPEC-001).

Per this codebase's standing policy (see test_funding_strategy_api.py's
module docstring), the real GPT-4o call is out of scope for network-free
tests EXCEPT via directly monkeypatching `engine.client.chat.completions.create`
— the same "mock the AI-calling method, exercise everything around it for
real" approach used there and in test_bulk_qualification_api.py. Tested at
the engine level directly (asyncio.run, no HTTP client/DB), same convention
as test_compliance_engine.py's "fully deterministic, no OpenAI calls except
where explicitly mocked" split.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from engines.tense_validator import TenseValidatorEngine


def _run(coro):
    return asyncio.run(coro)


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = None  # usage_from_response() tolerates a missing/None usage object


def _engine_with_fake_response(*contents, monkeypatch):
    """Wires engine.client.chat.completions.create to return _Resp(contents[i])
    on the i-th call, in order — lets a test simulate a retry-then-succeed
    sequence by passing two payloads."""
    engine = TenseValidatorEngine()
    call_log = []

    async def fake_create(**kwargs):
        call_log.append(kwargs)
        idx = len(call_log) - 1
        content = contents[min(idx, len(contents) - 1)]
        return _Resp(content)

    monkeypatch.setattr(engine.client.chat.completions, "create", fake_create)
    return engine, call_log


def test_clean_section_reports_no_issues(monkeypatch):
    payload = json.dumps({"clean": True, "issues": []})
    engine, calls = _engine_with_fake_response(payload, monkeypatch=monkeypatch)

    result = _run(engine.validate_section(
        section_id="technical_approach", section_title="Technical Approach",
        content="We will develop a novel sensor array and will validate it in Phase I.",
    ))

    assert result.clean is True
    assert result.issues == []
    assert result.section_id == "technical_approach"
    assert len(calls) == 1
    # response_format=json_object and a low temperature are load-bearing for
    # this being a consistency check, not creative writing — assert they're
    # actually set, not just documented in a comment.
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["temperature"] <= 0.3


def test_flagged_section_returns_issues_with_quote_and_fix(monkeypatch):
    payload = json.dumps({
        "clean": False,
        "issues": [{
            "quote": "We developed a novel sensor array for this project.",
            "problem": "Past tense used for proposed future work — this project has not started.",
            "suggested_fix": "We will develop a novel sensor array for this project.",
        }],
    })
    engine, _ = _engine_with_fake_response(payload, monkeypatch=monkeypatch)

    result = _run(engine.validate_section(
        section_id="technical_approach", section_title="Technical Approach",
        content="We developed a novel sensor array for this project.",
    ))

    assert result.clean is False
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.quote == "We developed a novel sensor array for this project."
    assert "will develop" in issue.suggested_fix


def test_empty_content_skips_the_ai_call_entirely(monkeypatch):
    engine = TenseValidatorEngine()
    called = {"n": 0}

    async def fake_create(**kwargs):
        called["n"] += 1
        return _Resp(json.dumps({"clean": True, "issues": []}))

    monkeypatch.setattr(engine.client.chat.completions, "create", fake_create)

    result = _run(engine.validate_section(section_id="references", section_title="References", content=""))

    assert result.clean is True
    assert called["n"] == 0  # nothing to check -> no AI call, no charge


def test_retries_once_on_schema_mismatch_then_succeeds(monkeypatch):
    """First response is valid JSON but the wrong shape (missing the
    required "clean" field) — fails TenseCheckOutput.model_validate(), not
    JSON parsing. Second response is well-formed and succeeds. Mirrors
    generate_logic_model()'s "attempt one structured regeneration if
    validation fails" rule — this is the retry path that rule actually
    covers (a parse failure on totally unparseable text is a separate,
    non-retried failure mode; see test_raises_500_immediately_on_unparseable_response)."""
    wrong_shape = json.dumps({"issues": []})  # missing required "clean" field
    good = json.dumps({"clean": True, "issues": []})
    engine, calls = _engine_with_fake_response(wrong_shape, good, monkeypatch=monkeypatch)

    result = _run(engine.validate_section(
        section_id="team", section_title="Team Qualifications",
        content="Our PI has led three prior federal awards.",
    ))

    assert result.clean is True
    assert len(calls) == 2  # confirms the retry actually happened


def test_raises_500_after_two_consecutive_schema_mismatches(monkeypatch):
    wrong_shape = json.dumps({"issues": []})  # missing required "clean" field, both times
    engine, calls = _engine_with_fake_response(wrong_shape, wrong_shape, monkeypatch=monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        _run(engine.validate_section(
            section_id="facilities", section_title="Facilities", content="Some real content here.",
        ))

    assert exc_info.value.status_code == 500
    assert len(calls) == 2  # exactly one retry, not an infinite loop


def test_raises_500_immediately_on_unparseable_response(monkeypatch):
    """A response that isn't JSON at all (no braces to brace-scan-recover)
    fails inside _parse_json_response() itself, before pydantic validation
    even runs — this is NOT retried, same as the equivalent codepath in
    proposal_generator.py/generate_logic_model(). One call, one failure."""
    engine, calls = _engine_with_fake_response(
        "not valid json at all with no braces", monkeypatch=monkeypatch,
    )

    with pytest.raises(HTTPException) as exc_info:
        _run(engine.validate_section(
            section_id="facilities", section_title="Facilities", content="Some real content here.",
        ))

    assert exc_info.value.status_code == 500
    assert len(calls) == 1  # no retry — this failure mode is distinct from schema-mismatch
