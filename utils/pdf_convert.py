"""
DOCX -> PDF conversion via headless LibreOffice (Word/PDF Report Generation
Development Specification, CLARIVA-DOCGEN-SPEC-001, Phase 8).

Before this module, PDF export was a second, independently-maintained
renderer (engines/document_output.py::_export_pdf(), reportlab-based) that
only ever read a section's flat `content` string — no figures, no tables,
no schedules/callouts/references, none of the block-JSON structured
rendering Phases 1-7 built for DOCX. The two formats had visibly drifted
apart. This module lets PDF export become "print the DOCX that's already
correct" instead: the DOCX exporter builds the real document (full
structured-block render, same as DOCX export produces), and this module
shells out to headless LibreOffice to convert those DOCX bytes to PDF
bytes. Single source of truth; the reportlab renderer stays only as a
last-resort fallback for an environment with no LibreOffice binary
available (see engines/document_output.py::_export_pdf_via_docx()).

Why a subprocess and not a Python PDF library that reads DOCX directly:
no pure-Python library renders OOXML (tables, structured blocks, embedded
images, page numbers) faithfully — LibreOffice's own layout engine is what
actually reproduces what Word/DOCX viewers show. This is the same
resolution this codebase reaches for problems like this: use the real
tool for the job, degrade gracefully if it's unavailable, don't
reimplement a rendering engine.

Concurrency note: LibreOffice's headless mode uses a user profile
directory for locking/state, and two simultaneous `soffice` invocations
sharing the same profile can collide ("Fatal Error: The application
cannot be started"). Since this is called from concurrent Heroku dyno
requests, every invocation gets its own throwaway profile directory via
`-env:UserInstallation=file://<unique tmp path>` — never the default
shared profile.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import uuid
from typing import Optional

from config import settings

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60.0
_CANDIDATE_BINARIES = ("soffice", "libreoffice")


class LibreOfficeUnavailableError(Exception):
    """No soffice/libreoffice binary found on PATH (or at the configured
    override). Caller should fall back to the legacy PDF renderer."""


class LibreOfficeConversionError(Exception):
    """The binary was found and invoked, but the conversion itself failed
    (nonzero exit, timeout, or no output file produced). Caller should
    fall back to the legacy PDF renderer."""


def _resolve_binary() -> Optional[str]:
    """Config override first (SOFFICE_BINARY), then PATH lookup for either
    common binary name — LibreOffice ships as `soffice` on most Linux
    distros/Homebrew and `libreoffice` on some (e.g. Debian's alias)."""
    if settings.SOFFICE_BINARY:
        return settings.SOFFICE_BINARY if shutil.which(settings.SOFFICE_BINARY) else None
    for name in _CANDIDATE_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


def is_available() -> bool:
    """Cheap availability check — lets a caller (e.g. an admin diagnostics
    endpoint) report whether LibreOffice PDF conversion is configured in
    this environment without attempting a real conversion."""
    return _resolve_binary() is not None


async def docx_bytes_to_pdf_bytes(docx_bytes: bytes, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> bytes:
    """Convert a DOCX file's raw bytes to PDF bytes via headless LibreOffice.

    Raises LibreOfficeUnavailableError if no binary is found, or
    LibreOfficeConversionError if the binary runs but fails/times out/
    produces no output. Never returns partial/corrupt bytes — either a
    complete PDF or an exception, so the caller's fallback logic can be a
    simple try/except around this call."""
    binary = _resolve_binary()
    if not binary:
        raise LibreOfficeUnavailableError(
            "No `soffice`/`libreoffice` binary found on PATH (set SOFFICE_BINARY to override)."
        )

    with tempfile.TemporaryDirectory(prefix="clariva_pdf_") as tmpdir:
        docx_path = os.path.join(tmpdir, "input.docx")
        with open(docx_path, "wb") as f:
            f.write(docx_bytes)

        # Unique per-invocation profile dir — see module docstring's
        # concurrency note. Doesn't need to exist beforehand; soffice
        # creates it.
        profile_dir = os.path.join(tempfile.gettempdir(), f"clariva_lo_profile_{uuid.uuid4().hex}")

        proc = await asyncio.create_subprocess_exec(
            binary,
            f"-env:UserInstallation=file://{profile_dir}",
            "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nofirststartwizard",
            "--convert-to", "pdf:writer_pdf_Export",
            "--outdir", tmpdir,
            docx_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise LibreOfficeConversionError(
                f"soffice conversion timed out after {timeout_seconds}s"
            )
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)

        if proc.returncode != 0:
            raise LibreOfficeConversionError(
                f"soffice exited {proc.returncode}: {stderr.decode(errors='replace')[:500]}"
            )

        pdf_path = os.path.join(tmpdir, "input.pdf")
        if not os.path.exists(pdf_path):
            raise LibreOfficeConversionError(
                f"soffice reported success (exit 0) but produced no PDF. "
                f"stdout: {stdout.decode(errors='replace')[:300]}"
            )

        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        if not pdf_bytes:
            raise LibreOfficeConversionError("soffice produced an empty PDF file.")

        return pdf_bytes
