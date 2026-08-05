"""
Clariva — Canonical Schema Migration Definitions.

Single source of truth for every additive schema change made to the
production models after their table was first created. This module is
imported by two entry points that apply the exact same logic:

  1. database.py::create_tables()  — runs automatically on every app boot
     (local dev, Railway, Heroku). This is what keeps a running deployment's
     schema in sync without a separate deploy step.
  2. migrate_db.py                 — a manual/offline CLI wrapper around the
     same function, for one-off use (e.g. `heroku run python migrate_db.py`)
     when you want to apply schema changes without restarting the dyno.

Rules for adding a new column (Clariva Enterprise™ PRD, Phase 0 — Foundation
Hardening; see docs/ARCHITECTURE.md):

  - Add ONE new tuple to COLUMN_MIGRATIONS. Never edit or remove an existing
    tuple once it has shipped to production — this list is append-only.
  - Always make new columns nullable or DEFAULT-valued so existing rows
    remain valid without a backfill.
  - Add the column to the SQLAlchemy model in models/db_models.py in the
    same change, so new rows created via the ORM match the raw-SQL migration.
  - Prefer a single column addition per phase/module over multiple
    unrelated changes in one entry, so history stays legible.

Both the SQLite and PostgreSQL column-definition strings are required even
when identical, so a future column with genuinely different types per
dialect (e.g. JSON vs JSONB) doesn't need special-casing later.
"""
from __future__ import annotations

from typing import List, Tuple

# (table, column, sqlite_column_def, postgres_column_def)
ColumnMigration = Tuple[str, str, str, str]

COLUMN_MIGRATIONS: List[ColumnMigration] = [
    # --- users --------------------------------------------------------------
    ("users", "is_superadmin",     "BOOLEAN DEFAULT 0",          "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("users", "role",              "VARCHAR(30) DEFAULT 'user'", "VARCHAR(30) NOT NULL DEFAULT 'user'"),
    ("users", "subscription_plan", "VARCHAR(30) DEFAULT 'free'", "VARCHAR(30) NOT NULL DEFAULT 'free'"),

    # --- proposals ------------------------------------------------------------
    ("proposals", "grant_type",       "VARCHAR(30) DEFAULT 'sbir'", "VARCHAR(30) DEFAULT 'sbir'"),
    ("proposals", "beneficiary_type", "VARCHAR(100)",               "VARCHAR(100)"),
    ("proposals", "funder_class",     "VARCHAR(100)",               "VARCHAR(100)"),
    ("proposals", "program_label",    "VARCHAR(300)",               "VARCHAR(300)"),
    ("proposals", "program_size",     "VARCHAR(50)",                "VARCHAR(50)"),
    ("proposals", "grantor_name",     "VARCHAR(200)",               "VARCHAR(200)"),

    # --- foa_records ------------------------------------------------------------
    ("foa_records", "grant_type", "VARCHAR(30) DEFAULT 'sbir'", "VARCHAR(30) NOT NULL DEFAULT 'sbir'"),

    # --- budget_records ------------------------------------------------------------
    ("budget_records", "fee_rate",       "FLOAT DEFAULT 7.0", "FLOAT DEFAULT 7.0"),
    ("budget_records", "total_direct",   "FLOAT DEFAULT 0.0", "FLOAT DEFAULT 0.0"),
    ("budget_records", "total_indirect", "FLOAT DEFAULT 0.0", "FLOAT DEFAULT 0.0"),
    ("budget_records", "total_cost",     "FLOAT DEFAULT 0.0", "FLOAT DEFAULT 0.0"),

    # --- org_contexts ------------------------------------------------------------
    # Previously handled ONLY by the standalone, SQLite-only migrate_db.py script —
    # unified here so PostgreSQL (staging/production) receives the same columns
    # SQLite (local dev) has always gotten. See docs/ARCHITECTURE.md for the gap
    # this closes.
    ("org_contexts", "company_capabilities", "TEXT",                "TEXT"),
    ("org_contexts", "pi_orcid",              "VARCHAR(25)",         "VARCHAR(25)"),
    ("org_contexts", "pi_degree",             "VARCHAR(100)",        "VARCHAR(100)"),
    ("org_contexts", "pi_affiliation",        "VARCHAR(255)",        "VARCHAR(255)"),
    ("org_contexts", "pi_publications",       "INTEGER",             "INTEGER"),
    ("org_contexts", "pi_prior_sbir_awards",  "INTEGER",             "INTEGER"),
    ("org_contexts", "team_members",          "JSON DEFAULT '[]'",   "JSON DEFAULT '[]'::json"),
    ("org_contexts", "facilities",            "JSON DEFAULT '[]'",   "JSON DEFAULT '[]'::json"),
    ("org_contexts", "partners",              "JSON DEFAULT '[]'",   "JSON DEFAULT '[]'::json"),
    ("org_contexts", "past_performance",      "JSON DEFAULT '[]'",   "JSON DEFAULT '[]'::json"),

    # --- organizations ------------------------------------------------------------
    # Forward-looking extension point called for by the Clariva Enterprise™ PRD
    # (Platform Architecture Overview, §6.2): "All new modules are feature-flagged
    # at the organization level so enterprise rollout can be phased per-customer
    # without branching the codebase." Added empty/off by default; no module reads
    # it yet, so this is a pure no-op until Phase 1 (Enterprise Foundations) wires
    # it up as part of RBAC.
    ("organizations", "feature_flags", "TEXT DEFAULT '{}'", "JSON DEFAULT '{}'::json"),

    # --- users (Phase 1 — Enterprise Foundations: MFA + SSO groundwork) ---------
    ("users", "mfa_enabled",    "BOOLEAN DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("users", "mfa_secret",     "VARCHAR(64)",        "VARCHAR(64)"),
    ("users", "sso_provider",   "VARCHAR(50)",         "VARCHAR(50)"),
    ("users", "sso_subject_id", "VARCHAR(255)",        "VARCHAR(255)"),

    # --- org_proposals (Phase 3 — Collaboration: workspace hierarchy, PRD §11) --
    # Optional link from a shared proposal to the Team (within the same org)
    # working on it. Nullable — existing shared proposals have no team until
    # one is explicitly assigned.
    ("org_proposals", "team_id", "VARCHAR(36)", "VARCHAR(36)"),

    # --- foa_records (Phase 4 — Funding Intelligence & Grant Tracking, PRD §15) --
    # Extends the existing opportunity record with pipeline-stage fields and
    # Grants.gov/SAM.gov sync metadata. All nullable/defaulted — existing
    # personal FOA uploads keep working unchanged with pipeline_stage
    # defaulting to "identified" and source defaulting to "manual".
    ("foa_records", "org_id",                  "VARCHAR(36)",                     "VARCHAR(36)"),
    ("foa_records", "pipeline_stage",          "VARCHAR(20) DEFAULT 'identified'", "VARCHAR(20) NOT NULL DEFAULT 'identified'"),
    ("foa_records", "source",                  "VARCHAR(20) DEFAULT 'manual'",     "VARCHAR(20) NOT NULL DEFAULT 'manual'"),
    ("foa_records", "external_id",             "VARCHAR(100)",                     "VARCHAR(100)"),
    ("foa_records", "external_url",            "VARCHAR(1000)",                    "VARCHAR(1000)"),
    ("foa_records", "estimated_award_floor",   "FLOAT",                            "FLOAT"),
    ("foa_records", "estimated_award_ceiling", "FLOAT",                            "FLOAT"),
    ("foa_records", "eligibility_summary",     "TEXT",                             "TEXT"),
    ("foa_records", "bid_no_go_decision",      "VARCHAR(20)",                      "VARCHAR(20)"),
    ("foa_records", "bid_no_go_rationale",     "TEXT",                             "TEXT"),
    ("foa_records", "assigned_to",             "VARCHAR(36)",                      "VARCHAR(36)"),
    ("foa_records", "last_synced_at",          "DATETIME",                         "TIMESTAMPTZ"),

    # --- foa_records (Phase 5 — Award & Project Management, PRD §17 renewals) --
    # Links a renewal/continuation pipeline entry back to the Award it
    # renews. Nullable — every pre-Phase-5 and non-renewal FOARecord has none.
    ("foa_records", "originating_award_id",    "VARCHAR(36)",                      "VARCHAR(36)"),

    # --- organizations (Phase 6 — Integrations & Marketplace, PRD §20 white-label) --
    # All nullable/defaulted — an org with none of these set renders exactly
    # like today (Clariva-branded, no change in behavior).
    ("organizations", "white_label_enabled", "BOOLEAN DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("organizations", "brand_name",          "VARCHAR(255)",      "VARCHAR(255)"),
    ("organizations", "logo_url",            "VARCHAR(1000)",     "VARCHAR(1000)"),
    ("organizations", "primary_color",       "VARCHAR(20)",       "VARCHAR(20)"),
]

# New tables introduced by Phase 5 (awards, award_expenditures,
# award_compliance_items, award_amendments, project_issues,
# award_performance_records, award_closeouts) and Phase 6
# (connector_connections, connector_event_logs, api_keys,
# marketplace_listings) need no entry here — see the note above
# COLUMN_MIGRATIONS: Base.metadata.create_all() creates any table that
# doesn't exist yet, for both SQLite and PostgreSQL, automatically.

# New tables introduced after the initial schema (AuditLog, AICreditLedger,
# CreditTransaction, CreditAllocation, ...) do NOT need an entry here —
# Base.metadata.create_all() in create_tables() creates any table that
# doesn't exist yet, for both SQLite and PostgreSQL, automatically. This
# list is only for adding a COLUMN to a table that already exists.

# Statements that aren't a simple "add column if missing" (e.g. altering an
# existing column's type). PostgreSQL-only: SQLite has dynamic column typing
# and never needed this statement in the first place.
POSTGRES_ONLY_STATEMENTS: List[str] = [
    "ALTER TABLE proposals ALTER COLUMN agency TYPE VARCHAR(30)",
]
