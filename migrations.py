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

    # --- org_contexts: Firm identity/address + PI/BO/ACN contacts -----------
    # Added after reviewing NASA's SBIR/STTR ProSAMS Firm Information and
    # Phase I Proposal Forms — see docs/ARCHITECTURE.md's Company Profile
    # addendum. Firm-level identity (EIN, DUNS, address, phone) and the two
    # contact roles NASA requires alongside the PI (Business Official,
    # Authorized Contract Negotiator) had no columns at all before this.
    ("org_contexts", "ein_tax_id",   "VARCHAR(15)",  "VARCHAR(15)"),
    ("org_contexts", "duns_number",  "VARCHAR(13)",  "VARCHAR(13)"),
    ("org_contexts", "firm_street",  "VARCHAR(255)", "VARCHAR(255)"),
    ("org_contexts", "firm_apt_suite", "VARCHAR(100)", "VARCHAR(100)"),
    ("org_contexts", "firm_city",    "VARCHAR(100)", "VARCHAR(100)"),
    ("org_contexts", "firm_state",   "VARCHAR(50)",  "VARCHAR(50)"),
    ("org_contexts", "firm_zip",     "VARCHAR(12)",  "VARCHAR(12)"),
    ("org_contexts", "firm_phone",   "VARCHAR(30)",  "VARCHAR(30)"),
    ("org_contexts", "pi_email",     "VARCHAR(255)", "VARCHAR(255)"),
    ("org_contexts", "pi_phone",     "VARCHAR(30)",  "VARCHAR(30)"),
    ("org_contexts", "bo_name",      "VARCHAR(255)", "VARCHAR(255)"),
    ("org_contexts", "bo_title",     "VARCHAR(150)", "VARCHAR(150)"),
    ("org_contexts", "bo_phone",     "VARCHAR(30)",  "VARCHAR(30)"),
    ("org_contexts", "bo_email",     "VARCHAR(255)", "VARCHAR(255)"),
    ("org_contexts", "acn_name",     "VARCHAR(255)", "VARCHAR(255)"),
    ("org_contexts", "acn_title",    "VARCHAR(150)", "VARCHAR(150)"),
    ("org_contexts", "acn_phone",    "VARCHAR(30)",  "VARCHAR(30)"),
    ("org_contexts", "acn_email",    "VARCHAR(255)", "VARCHAR(255)"),

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

    # --- foa_records (Version 3.0 architecture upgrade, Phase 14 — Renewal Loop Closure) --
    # Persists the notes captured on the renewal-creation form; previously
    # accepted by RenewalCreate but never written anywhere.
    ("foa_records", "renewal_notes",           "TEXT",                             "TEXT"),

    # --- foa_records (Version 3.0 architecture upgrade, Phase 16 cont'd) --------
    # Plain-language AI summary shown on the FOA Library card, generated
    # alongside eligibility_summary (above) so a user can decide whether an
    # opportunity is worth pursuing before clicking "Use this FOA." Nullable
    # — existing records are backfilled on-demand via POST /foa/{id}/summarize
    # rather than in a batch migration.
    ("foa_records", "ai_summary",              "TEXT",                             "TEXT"),

    # --- foa_records (Funding Opportunity Intelligence, Phase 1 — Grant
    # Finding Workspace Upgrade) --------------------------------------------
    # Upgrades the paid "Generate AI Summary" service into a structured,
    # 14-section pursuit-decision report. `intelligence_report` holds the
    # full JSON; the four columns after it are cached headline
    # classifications for fast card rendering. All nullable — existing
    # records simply show no report until analyzed on demand, exactly like
    # ai_summary's own backfill-on-demand precedent above.
    ("foa_records", "intelligence_report",     "JSON",                             "JSON"),
    ("foa_records", "eligibility_status",      "VARCHAR(30)",                      "VARCHAR(30)"),
    ("foa_records", "complexity",              "VARCHAR(20)",                      "VARCHAR(20)"),
    ("foa_records", "attractiveness",          "VARCHAR(20)",                      "VARCHAR(20)"),
    ("foa_records", "attractiveness_reason",   "TEXT",                             "TEXT"),

    # --- organizations (Phase 6 — Integrations & Marketplace, PRD §20 white-label) --
    # All nullable/defaulted — an org with none of these set renders exactly
    # like today (Clariva-branded, no change in behavior).
    ("organizations", "white_label_enabled", "BOOLEAN DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("organizations", "brand_name",          "VARCHAR(255)",      "VARCHAR(255)"),
    ("organizations", "logo_url",            "VARCHAR(1000)",     "VARCHAR(1000)"),
    ("organizations", "primary_color",       "VARCHAR(20)",       "VARCHAR(20)"),

    # --- ai_credit_ledgers (low-balance warning) --------------------------------
    # Defaulted to the same starter allotment as `balance` so every existing
    # ledger row reads as "100% remaining" immediately after this migration
    # runs, rather than divide-by-zero or a false low-balance alarm.
    ("ai_credit_ledgers", "reference_balance", "FLOAT DEFAULT 100.0", "FLOAT NOT NULL DEFAULT 100.0"),

    # --- credit_allocations (team/department spending caps) --------------------
    # Additive dimensions alongside the existing per-user/org-wide-default cap
    # — see CreditAllocation's docstring in models/db_models.py for the
    # precedence rule CreditEngine.check_allocation() enforces.
    ("credit_allocations", "team_id",       "VARCHAR(36)", "VARCHAR(36)"),
    ("credit_allocations", "department_id", "VARCHAR(36)", "VARCHAR(36)"),

    # --- awards (Phase 7 — Award Received data model foundation, Version 3.0) --
    # IMPORTANT: this SQL-level default ("active") is intentionally DIFFERENT
    # from the Python/ORM-level default ("received") on Award.award_status in
    # models/db_models.py — see that column's docstring for the full
    # rationale. Short version: every Award row that already exists when this
    # migration runs was created before Award Received existed as a distinct
    # stage, so it is backfilled as already-active/past-negotiation; only
    # Award rows the ORM inserts AFTER this migration (i.e. new awards, via
    # AwardEngine.create_award()) get the "received" starting state and must
    # go through an explicit "Activate Project" action.
    ("awards", "award_status", "VARCHAR(20) DEFAULT 'active'", "VARCHAR(20) NOT NULL DEFAULT 'active'"),

    # --- proposals (Version 3.0 upgrade, Phase 15 — Quick Award Intake) --------
    # Nullable, no backfill needed: every existing proposal implicitly reads
    # as native (origin IS NULL) and behaves exactly as it always has. Only
    # new shell proposals created by AwardEngine.create_award_from_intake()
    # ever get "imported" written into this column. See Proposal.origin's
    # docstring in models/db_models.py for the full rationale.
    ("proposals", "origin", "VARCHAR(20)", "VARCHAR(20)"),

    # --- organizations (Phase 2 — On-Demand AI Services Marketplace & Org
    # Funding Controls; Enterprise Pricing spec §9.2) --------------------------
    # Drives ComplimentaryAllowance lookups. Defaults every existing org to
    # "free" (no paid-plan allowances) — matches Organization.plan's default
    # in models/db_models.py so ORM-created and migration-backfilled rows
    # agree.
    ("organizations", "plan", "VARCHAR(30) DEFAULT 'free'", "VARCHAR(30) NOT NULL DEFAULT 'free'"),

    # --- org_contexts (Funding Opportunity Intelligence, Phase 2 —
    # Organization-Specific Matching, Ranking & Decision Intelligence) ------
    # Migrates the Company Profile from strictly per-user to optionally
    # org-owned (org_id nullable, same pattern as FOARecord.org_id/
    # Watchlist.org_id) — see OrgContextDB's docstring in
    # models/db_models.py for the full rationale, including why user_id is
    # no longer treated as unique. The uniqueness constraint itself is
    # dropped separately below in POSTGRES_ONLY_STATEMENTS (SQLite has no
    # equivalent DROP CONSTRAINT; a fresh SQLite DB created via
    # create_all() from the current model never has the constraint in the
    # first place, so this only matters for an existing production
    # Postgres table).
    ("org_contexts", "org_id",              "VARCHAR(36)", "VARCHAR(36)"),
    # Net-new Funding Intelligence Profile fields — all nullable, no
    # backfill: an existing profile simply shows these as empty until a
    # user fills them in, exactly like every other additive column in
    # this table's history.
    ("org_contexts", "mission_statement",   "TEXT",           "TEXT"),
    ("org_contexts", "industries",          "JSON",           "JSON"),
    ("org_contexts", "certifications",      "JSON",           "JSON"),
    ("org_contexts", "naics_codes",         "JSON",           "JSON"),
    ("org_contexts", "service_geography",   "JSON",           "JSON"),
    ("org_contexts", "funding_preferences", "JSON",           "JSON"),
    ("org_contexts", "entity_type",         "VARCHAR(30)",    "VARCHAR(30)"),
    # Funding Opportunity Intelligence, Phase 3 §4.3 (Portfolio-Level Recommendations)
    ("org_contexts", "pursuit_capacity",    "INTEGER",        "INTEGER"),

    # --- Phase 3 §4.7 billing wire-up — real charging for previously-seeded-
    # but-unwired service catalog entries (award_setup_*, proposal_development_*,
    # post_award_management_*). See docs/ARCHITECTURE.md for the full design.
    ("stored_files", "page_count", "INTEGER", "INTEGER"),
    # Proposal: tracks whether the one-time proposal_development_{tier} fee
    # has already been charged for this proposal's first full-draft
    # generation — see routers/proposals.py::generate_all_sections. Default
    # FALSE for every existing proposal (none of them were ever charged this
    # fee, since it didn't exist before this migration) — never retroactively
    # billed.
    ("proposals", "development_fee_charged", "BOOLEAN DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE"),
    # Award: post_award_management_{tier} recurring billing state — tier is
    # derived once at activation from total_award_value (see
    # routers/awards.py::activate_award) and never recomputed; last_billed_at
    # guards scripts/run_monthly_billing.py against double-billing the same
    # award twice in one calendar month. Both NULL for every award that
    # predates this column — an award activated before this migration simply
    # isn't billed until the next activation-equivalent event backfills its
    # tier (out of scope here; see the billing script's docstring for the
    # "awards with no tier are skipped, not defaulted" guard).
    ("awards", "post_award_tier",          "VARCHAR(20)", "VARCHAR(20)"),
    ("awards", "post_award_last_billed_at", "DATETIME",    "TIMESTAMPTZ"),

    # --- organizations (post-launch addendum — real-money payment wiring) -----
    # Nullable, no backfill: every existing org reads as "no active paid
    # subscription" (NULL), which is correct for all of them today — nothing
    # before this column existed ever actually activated a plan via Square,
    # since the webhook that sets this column didn't exist yet either. See
    # Organization.plan_expires_at's docstring in models/db_models.py and
    # scripts/downgrade_expired_plans.py.
    ("organizations", "plan_expires_at", "DATETIME", "TIMESTAMPTZ"),
]

# New tables introduced by Phase 2 (service_catalog_items,
# complimentary_allowances, org_service_entitlements,
# ai_service_transactions) need no entry here — same
# create_all()-handles-new-tables rule noted below.

# New tables introduced by Phase 7 (project_baselines, award_conditions) need
# no entry here either — same create_all()-handles-new-tables rule as Phase 5
# and Phase 6 above.

# New tables introduced by Phase 5 (awards, award_expenditures,
# award_compliance_items, award_amendments, project_issues,
# award_performance_records, award_closeouts) and Phase 6
# (connector_connections, connector_event_logs, api_keys,
# marketplace_listings) need no entry here — see the note above
# COLUMN_MIGRATIONS: Base.metadata.create_all() creates any table that
# doesn't exist yet, for both SQLite and PostgreSQL, automatically.

# square_webhook_events (post-launch addendum — real-money payment wiring)
# is a new table too, same rule: no entry needed here.

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
    # Funding Opportunity Intelligence, Phase 2 — drops the UNIQUE(user_id)
    # constraint org_contexts was originally created with (Postgres's
    # default name for a bare `unique=True` column is
    # "<table>_<column>_key"), now that a user can legitimately own more
    # than one profile row (their personal one, org_id NULL, plus any
    # organization's shared one they created/edited). `_pg_exec()` already
    # swallows and logs a warning on any error, so this is safe to run on
    # every boot even after the constraint no longer exists.
    "ALTER TABLE org_contexts DROP CONSTRAINT IF EXISTS org_contexts_user_id_key",
]
