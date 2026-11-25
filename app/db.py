"""Async SQLAlchemy engine/session + schema bootstrap (PostgreSQL + pgvector)."""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_size=20,
    max_overflow=40,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


class Base(DeclarativeBase):
    # Fetch server-generated values (server_default / onupdate=func.now()) with
    # RETURNING on INSERT *and* UPDATE, so attributes are never expired and
    # never lazily refreshed afterwards (lazy IO is illegal in async code).
    __mapper_args__ = {"eager_defaults": True}


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    # Lightweight additive migrations for databases created by earlier versions.
    # Only ALTER when the column is really missing (ALTER needs an exclusive lock
    # even with IF NOT EXISTS) and never wait more than a few seconds for a lock.
    migrations = (
        ("users", "session_token_hash", "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_token_hash VARCHAR(64)"),
        ("users", "session_issued_at", "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_issued_at TIMESTAMPTZ"),
    )
    for table, column, ddl in migrations:
        async with engine.begin() as conn:
            exists = (await conn.execute(text(
                "SELECT 1 FROM information_schema.columns WHERE table_name=:t AND column_name=:c"),
                {"t": table, "c": column})).first()
            if not exists:
                await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                await conn.execute(text(ddl))
