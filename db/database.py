"""
SQLAlchemy engine and session management.

- async_engine / AsyncSessionLocal  → used by the bot at runtime
- get_async_session()               → async context manager for DB operations
- get_sync_engine()                 → sync engine for Alembic migrations only
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings

# ---------------------------------------------------------------------------
# Async engine — used by the running bot
# ---------------------------------------------------------------------------

async_engine = create_async_engine(
    settings.db_url,
    echo=False,
    pool_pre_ping=True,   # detect stale connections
    pool_size=5,
    max_overflow=10,
)

AsyncSessionLocal = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager — auto-commits on success, rolls back on error."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Sync engine — Alembic migrations only, never used by the bot
# ---------------------------------------------------------------------------

def get_sync_engine():
    return create_engine(settings.db_url_sync, echo=False)
