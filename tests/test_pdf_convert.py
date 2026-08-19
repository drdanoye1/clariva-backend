"""
utils/pdf_convert.py — DOCX -> PDF conversion via headless LibreOffice
(Word/PDF Report Generation Development Specification, CLARIVA-DOCGEN-
SPEC-001, Phase 8).

Fully deterministic and needs no DB/app/OpenAI — this module is pure
subprocess orchestration around a real `soffice` binary. Tests that need a
real conversion are skipped (not failed) when no LibreOffice binary is
present in the environment (`pdf_convert.is_available()` — set via this
repo's Aptfile on Heroku, or backend/Dockerfile locally), so this file
still passes cleanly in an environment without LibreOffice installed,
matching this module's own "no binary -> LibreOfficeUnavailableError,
caller falls back gracefully" design rather than treating that as a test
failure. This mirrors the "degrade gracefully" policy already applied to
SAM_GOV_API_KEY/RESEND_API_KEY-less environments elsewhere in this suite.

Verified for real (not just mocked) during development of this phase,
directly against the actual sandbox's installed soffice binary: a real
DOCX built by DocumentOutputEngine._export_docx() converted to a real,
valid PDF; an unavailable-binary override correctly raised
LibreOfficeUnavailableError; and four concurrent conversions of the same
DOCX all succeeded without profile-lock collisions (see this module's
concurrency note). This test file re-creates that coverage in the
project's standard pytest form for CI.
"""
from __future__ import annotations

import asyncio

import pytest

from utils import pdf_convert


def _run(coro):
    return asyncio.run(coro)


def _minimal_docx_bytes() -> bytes:
    """A real, minimal .docx built with python-docx — not a hand-rolled zip,
    so this exercises the exact same file shape DocumentOutputEngine
    produces."""
    import io
    from docx import Document

    doc = Document()
    doc.add_heading("PDF Conversion Test Document", level=1)
    doc.add_paragraph("This paragraph exists to give LibreOffice something real to convert.")
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "Phase"
    table.rows[0].cells[1].text = "Deliverable"
    table.rows[1].cells[0].text = "Phase I"
    table.rows[1].cells[1].text = "Prototype"

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_is_available_returns_a_bool():
    # Environment-dependent (True where LibreOffice is installed, False
    # otherwise) — just confirms the check itself never raises.
    assert isinstance(pdf_convert.is_available(), bool)


def test_unavailable_binary_raises_libreoffice_unavailable_error(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "SOFFICE_BINARY", "/nonexistent/soffice-binary-xyz")

    with pytest.raises(pdf_convert.LibreOfficeUnavailableError):
        _run(pdf_convert.docx_bytes_to_pdf_bytes(_minimal_docx_bytes()))


@pytest.mark.skipif(not pdf_convert.is_available(), reason="No LibreOffice binary in this environment")
def test_docx_bytes_to_pdf_bytes_produces_a_real_pdf():
    pdf_bytes = _run(pdf_convert.docx_bytes_to_pdf_bytes(_minimal_docx_bytes()))
    assert pdf_bytes[:4] == b"%PDF"
    assert len(pdf_bytes) > 500


@pytest.mark.skipif(not pdf_convert.is_available(), reason="No LibreOffice binary in this environment")
def test_concurrent_conversions_do_not_collide_on_the_default_profile():
    """Each invocation gets its own -env:UserInstallation profile dir (see
    this module's concurrency note) — without that, simultaneous soffice
    invocations sharing the default profile can fail outright. Runs several
    conversions of the same DOCX at once and asserts every one succeeds."""
    docx_bytes = _minimal_docx_bytes()

    async def _convert():
        return await pdf_convert.docx_bytes_to_pdf_bytes(docx_bytes)

    async def _run_concurrent():
        return await asyncio.gather(*[_convert() for _ in range(4)])

    results = _run(_run_concurrent())
    assert len(results) == 4
    for pdf_bytes in results:
        assert pdf_bytes[:4] == b"%PDF"
        assert len(pdf_bytes) > 500
