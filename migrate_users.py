"""One-time migration: add is_superadmin, role, subscription_plan to users table."""
from database import sync_engine
from sqlalchemy import text

cols = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_superadmin BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(30) NOT NULL DEFAULT 'user'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_plan VARCHAR(30) NOT NULL DEFAULT 'free'",
]

with sync_engine.connect() as conn:
    for sql in cols:
        conn.execute(text(sql))
        print(f"OK: {sql[:60]}...")
    conn.commit()
    print("Migration complete.")
