"""
Clariva — Visual regression corpus bootstrap/"bless" script (Word/PDF
Report Generation Development Specification, CLARIVA-DOCGEN-SPEC-001,
Phase 14).

Manual/offline entry point, same "no FastAPI request context" shape as
migrate_db.py and scripts/downgrade_expired_plans.py at the top of
backend/ — except this one is a developer-run tool, not a Scheduler job:
there's no server-side trigger for "bless a new golden file," only a
human deciding a rendering change is intentional.

What this does: renders tests/golden/fixtures.py::AGENCY_FIXTURES through
the REAL DOCX exporter (engines/document_output.py::
DocumentOutputEngine._export_docx() — the exact function every proposal
export in production calls), extracts a structural snapshot via
utils/docx_snapshot.py::extract_snapshot(), and writes each one to
tests/golden/<agency>.snapshot.json. tests/test_visual_regression.py then
diffs future renders of the same fixtures against these files on every
test run — that's the actual regression gate; this script only exists to
create/update the baseline it compares against.

When to run this:
  1. First-time bootstrap — these golden files are NOT pre-generated in
     this repo (see docs/ARCHITECTURE.md's Phase 14 entry for why: they
     must be produced by the real renderer, which needs pydantic/
     sqlalchemy installed — dependencies this project's own sandbox
     environment does not have, so they cannot be honestly fabricated
     there). Run this once, in any environment with `pip install -r
     requirements.txt` completed, and commit the resulting
     tests/golden/*.snapshot.json files.
  2. Re-run (and re-commit) ONLY after a deliberate, reviewed formatting
     change to the DOCX renderer (utils/doc_utils.py, utils/
     block_renderer.py, engines/document_output.py::_export_docx()) — an
     unreviewed diff in a golden file in a pull request is exactly the
     signal this corpus exists to surface, so re-blessing should be a
     visible, explained line in that PR, never a reflexive "just make CI
     green" step.

Usage:
    cd backend
    python scripts/generate_golden_files.py

Idempotent-by-design: re-running with no renderer changes overwrites each
golden file with byte-identical JSON content (the snapshot format is
deterministic — see utils/docx_snapshot.py's module docstring), so `git
diff` after a routine re-run with no real formatting change shows nothing.
"""
from __future__ import annotations

import io
import json
import os


def main() -> None:
    # Imported inside main() so this script's module-level docstring/CLI
    # help doesn't require the full app dependency stack importable at
    # parse time — same convention as migrate_db.py / downgrade_expired_
    # plans.py. `types.SimpleNamespace` duck-types the `proposal`/`section`
    # objects _export_docx() expects (it only reads a handful of
    # attributes — .title/.agency/.phase/.version and .title/.content/
    # .structured_content — never the real ORM classes), so this script
    # needs no database connection at all, unlike every other script in
    # this directory.
    from types import SimpleNamespace

    from engines.document_output import DocumentOutputEngine
    from tests.golden.fixtures import AGENCY_FIXTURES
    from utils.docx_snapshot import extract_snapshot, pdf_page_count_via_soffice

    engine = DocumentOutputEngine()
    golden_dir = os.path.join(os.path.dirname(__file__), "..", "tests", "golden")

    for fixture in AGENCY_FIXTURES:
        proposal = SimpleNamespace(
            title=fixture["title"], agency=fixture["agency"],
            phase=fixture["phase"], version=1,
        )
        sections = [
            SimpleNamespace(
                title=sec["title"], content=None,
                structured_content=sec["structured_content"],
            )
            for sec in fixture["sections"]
        ]

        buffer = io.BytesIO()
        engine._export_docx(buffer, proposal, sections)
        docx_bytes = buffer.getvalue()
        if not docx_bytes.startswith(b"PK"):
            raise RuntimeError(
                f"_export_docx() silently fell back to TXT for {fixture['agency']} "
                f"(python-docx not importable in this environment) — golden files "
                f"must be generated where the full dependency stack is installed."
            )

        snapshot = extract_snapshot(docx_bytes)
        # Best-effort secondary signal (see utils/docx_snapshot.py's module
        # docstring) — None if LibreOffice isn't available here; the
        # regression test treats that as "skip this check," not a failure.
        snapshot["pdf_page_count"] = pdf_page_count_via_soffice(docx_bytes)

        out_path = os.path.join(golden_dir, f"{fixture['agency']}.snapshot.json")
        with open(out_path, "w") as f:
            json.dump(snapshot, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"Wrote {out_path} "
              f"({snapshot['paragraph_count']} paragraphs, {snapshot['table_count']} tables, "
              f"pdf_page_count={snapshot['pdf_page_count']})")

    print(f"Done. {len(AGENCY_FIXTURES)} golden file(s) written to {os.path.abspath(golden_dir)}.")


if __name__ == "__main__":
    main()
