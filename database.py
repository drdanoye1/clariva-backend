"""
Clariva Intelligent Grant Writing Platform — Database Setup
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
from migrations import COLUMN_MIGRATIONS, POSTGRES_ONLY_STATEMENTS


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
    """
    Create all tables on startup, then bring existing tables up to date.

    This is the single, standing schema-migration path for Clariva (Clariva
    Enterprise™ PRD, Phase 0 — Foundation Hardening): it runs automatically
    on every boot in every environment (local SQLite, Railway/Heroku
    PostgreSQL), and migrate_db.py is a manual CLI wrapper around this exact
    function for offline/one-off use. The column list itself lives in
    migrations.py — add new columns there, not here.
    """
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        if _is_sqlite:
            for table, column, sqlite_def, _pg_def in COLUMN_MIGRATIONS:
                await _sqlite_add_column_if_missing(conn, table, column, sqlite_def)
        else:
            # PostgreSQL: idempotent column migrations
            for table, column, _sqlite_def, pg_def in COLUMN_MIGRATIONS:
                await _pg_exec(
                    conn,
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {pg_def}",
                )
            for stmt in POSTGRES_ONLY_STATEMENTS:
                await _pg_exec(conn, stmt)


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
