"""
Clariva — Manual database migration CLI.

This is the manual/offline entry point into the SAME idempotent migration
logic that runs automatically every time the app boots (see
database.py::create_tables()). It is safe to run at any time, any number of
times, against SQLite (local dev) or PostgreSQL (staging/production) — it
only creates missing tables/columns and never drops or overwrites data.

Usage:
    cd backend
    python migrate_db.py                                    # local — uses DATABASE_URL from .env
    heroku run python migrate_db.py --app atifixia-api       # apply against production without a redeploy

All schema changes are defined in migrations.py (COLUMN_MIGRATIONS /
POSTGRES_ONLY_STATEMENTS) — add a new entry there, not in this file.
"""
from __future__ import annotations

import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")


async def main() -> None:
    # Imported inside main() so `python migrate_db.py --help`-style tooling
    # doesn't need the full app dependency stack importable at parse time.
    from database import create_tables, settings

    target = settings.DATABASE_URL
    if "@" in target:  # don't print credentials embedded in a Postgres URL
        target = target.split("@", 1)[1]

    print(f"Clariva schema migration — target database: {target}")
    await create_tables()
    print("Migration complete — schema is up to date.")


if __name__ == "__main__":
    asyncio.run(main())
