"""
AtiFixia SBIR Intelligence Platform — Database Setup
Async SQLAlchemy engine + session factory.
Primary: Neon PostgreSQL (asyncpg)
Fallback: SQLite (local dev only)
"""

from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import create_engine

from config import settings
from models.db_models import Base


_db_url = settings.DATABASE_URL
_is_sqlite = _db_url.startswith("sqlite")

# asyncpg rejects sslmode= and channel_binding= in the URL — strip them
if not _is_sqlite:
    import re
    _db_url = re.sub(r"[?&]sslmode=[^&]*", "", _db_url)
    _db_url = re.sub(r"[?&]channel_binding=[^&]*", "", _db_url)
    _db_url = re.sub(r"\?&", "?", _db_url).rstrip("?").rstrip("&")

# ─── Async engine ────────────────────────────────────────────
_engine_kwargs: dict = {"echo": settings.APP_ENV == "development"}

if _is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # PostgreSQL / Neon
    # asyncpg doesn't accept sslmode= in the URL — pass ssl via connect_args instead
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 10
    _engine_kwargs["pool_pre_ping"] = True
    _engine_kwargs["connect_args"] = {"ssl": "require"}

async_engine = create_async_engine(_db_url, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# ─── Sync engine (for Alembic / table creation) ──────────────
_sync_url = (
    settings.SYNC_DATABASE_URL
    or _db_url.replace("+aiosqlite", "").replace("+asyncpg", "")
)
sync_engine = create_engine(
    _sync_url,
    echo=False,
    **({"pool_pre_ping": True} if not _is_sqlite else {}),
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def create_tables() -> None:
    """Create all tables on startup."""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        if _is_sqlite:
            # SQLite: add columns introduced after initial schema
            await _sqlite_add_column_if_missing(
                conn, "proposals", "grant_type", "VARCHAR(30) DEFAULT 'sbir'"
            )
        else:
            # PostgreSQL: idempotent column migrations
            for col_sql in [
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_superadmin BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(30) NOT NULL DEFAULT 'user'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_plan VARCHAR(30) NOT NULL DEFAULT 'free'",
                # Taxonomy ground truth — grant wizard Steps 1/2/3
                "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS beneficiary_type VARCHAR(100)",
                "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS funder_class VARCHAR(100)",
                "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS program_label VARCHAR(300)",
                "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS program_size VARCHAR(50)",
                "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS grantor_name VARCHAR(200)",
                # Widen agency column to accommodate non-SBIR agency codes
                "ALTER TABLE proposals ALTER COLUMN agency TYPE VARCHAR(30)",
                # FOA records — grant_type added after initial schema
                "ALTER TABLE foa_records ADD COLUMN IF NOT EXISTS grant_type VARCHAR(30) NOT NULL DEFAULT 'sbir'",
                # Budget records — fee_rate and cached totals may be missing from older schema
                "ALTER TABLE budget_records ADD COLUMN IF NOT EXISTS fee_rate FLOAT DEFAULT 7.0",
                "ALTER TABLE budget_records ADD COLUMN IF NOT EXISTS total_direct FLOAT DEFAULT 0.0",
                "ALTER TABLE budget_records ADD COLUMN IF NOT EXISTS total_indirect FLOAT DEFAULT 0.0",
                "ALTER TABLE budget_records ADD COLUMN IF NOT EXISTS total_cost FLOAT DEFAULT 0.0",
            ]:
                await _pg_exec(conn, col_sql)


async def _pg_exec(conn, sql: str) -> None:
    """Run a raw SQL statement via the async connection (PostgreSQL helper)."""
    import logging
    from sqlalchemy import text
    try:
        await conn.execute(text(sql))
        logging.getLogger(__name__).info("Migration: %s", sql[:80])
    except Exception as exc:
        logging.getLogger(__name__).warning("Migration skipped (%s): %s", type(exc).__name__, sql[:80])


async def _sqlite_add_column_if_missing(conn, table: str, column: str, col_def: str) -> None:
    """Add a missing column to a SQLite table (SQLite-only migration helper)."""
    import logging
    from sqlalchemy import text

    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    existing = {row[1] for row in result.fetchall()}
    if column not in existing:
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}"))
        logging.getLogger(__name__).info("Migration: added %s.%s", table, column)
