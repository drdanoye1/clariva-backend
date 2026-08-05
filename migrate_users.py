"""
DEPRECATED — superseded by migrations.py + database.py::create_tables().

This one-time script added is_superadmin/role/subscription_plan to the users
table. Those columns are now part of the canonical, always-idempotent
migration list in migrations.py, which runs automatically on every app boot
and can also be applied manually via `python migrate_db.py`. Running this
file is harmless (the ALTER ... IF NOT EXISTS statements are idempotent) but
unnecessary — use `python migrate_db.py` instead.

Left in place for historical/runbook continuity rather than deleted.
"""
from database import sync_engine
from sqlalchemy import text

cols = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_superadmin BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(30) NOT NULL DEFAULT 'user'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_plan VARCHAR(30) NOT NULL DEFAULT 'free'",
]

if __name__ == "__main__":
    print("NOTE: this script is deprecated — run `python migrate_db.py` instead.")
    with sync_engine.connect() as conn:
        for sql in cols:
            conn.execute(text(sql))
            print(f"OK: {sql[:60]}...")
        conn.commit()
        print("Migration complete.")
