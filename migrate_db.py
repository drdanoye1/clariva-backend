"""
Database migration script — safe to run multiple times (idempotent).
Adds new columns to org_contexts table introduced in the profile expansion.

Usage:
    cd backend
    python3 migrate_db.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "sbir_platform.db")

MIGRATIONS = [
    ("company_capabilities",  "ALTER TABLE org_contexts ADD COLUMN company_capabilities TEXT"),
    ("pi_orcid",              "ALTER TABLE org_contexts ADD COLUMN pi_orcid VARCHAR(25)"),
    ("pi_degree",             "ALTER TABLE org_contexts ADD COLUMN pi_degree VARCHAR(100)"),
    ("pi_affiliation",        "ALTER TABLE org_contexts ADD COLUMN pi_affiliation VARCHAR(255)"),
    ("pi_publications",       "ALTER TABLE org_contexts ADD COLUMN pi_publications INTEGER"),
    ("pi_prior_sbir_awards",  "ALTER TABLE org_contexts ADD COLUMN pi_prior_sbir_awards INTEGER"),
    ("team_members",          "ALTER TABLE org_contexts ADD COLUMN team_members JSON DEFAULT '[]'"),
    ("facilities",            "ALTER TABLE org_contexts ADD COLUMN facilities JSON DEFAULT '[]'"),
    ("partners",              "ALTER TABLE org_contexts ADD COLUMN partners JSON DEFAULT '[]'"),
    ("past_performance",      "ALTER TABLE org_contexts ADD COLUMN past_performance JSON DEFAULT '[]'"),
    # Organizations tables (safe to skip if already exist — handled by SQLAlchemy CREATE IF NOT EXISTS)
]

def migrate():
    print(f"Connecting to: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("PRAGMA table_info(org_contexts)")
    existing_cols = {row[1] for row in c.fetchall()}

    for col_name, sql in MIGRATIONS:
        if col_name not in existing_cols:
            c.execute(sql)
            print(f"  ✓ Added column: {col_name}")
        else:
            print(f"  - Already exists: {col_name}")

    conn.commit()

    c.execute("PRAGMA table_info(org_contexts)")
    final_cols = [row[1] for row in c.fetchall()]
    print(f"\n✓ Migration complete. org_contexts now has {len(final_cols)} columns.")
    conn.close()

if __name__ == "__main__":
    migrate()
