"""
SQLAlchemy engine and session management.

- async_engine / AsyncSessionLocal  → used by the bot at runtime
- get_async_session()               → async context manager for DB operations
- get_sync_engine()                 → sync engine for Alembic migrations only
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine, async_sessionmaker, create_async_engine

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
# Per-store session factory — used by the dashboard API for multi-store access
# ---------------------------------------------------------------------------

_store_engines: dict[str, tuple[AsyncEngine, async_sessionmaker]] = {}


def _get_store_engine(store_id: str) -> tuple[AsyncEngine, async_sessionmaker]:
    if store_id not in _store_engines:
        url = (
            f"postgresql+asyncpg://{settings.postgres_user}:{settings.postgres_password}"
            f"@{settings.postgres_host}:{settings.postgres_port}/gasbot_{store_id}"
        )
        engine = create_async_engine(url, echo=False, pool_pre_ping=True, pool_size=5, max_overflow=10)
        session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        _store_engines[store_id] = (engine, session_maker)
    return _store_engines[store_id]


@asynccontextmanager
async def get_session_for_store(store_id: str) -> AsyncGenerator[AsyncSession, None]:
    """
    Session for a given store. All store data now lives in one shared database
    (canonical schema), so this is just an alias for get_async_session().
    The store_id argument is accepted for call-site compatibility but ignored —
    data isolation is enforced by WHERE store_id = sid in every query.
    """
    async with get_async_session() as session:
        yield session


# ---------------------------------------------------------------------------
# Sync engine — Alembic migrations only, never used by the bot
# ---------------------------------------------------------------------------

def get_sync_engine():
    return create_engine(settings.db_url_sync, echo=False)


from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

_sync_engine = None
_SyncSession = None

def _get_sync_session_factory():
    global _sync_engine, _SyncSession
    if _sync_engine is None:
        _sync_engine = create_engine(settings.db_url_sync, echo=False, pool_pre_ping=True)
        _SyncSession = sessionmaker(_sync_engine, expire_on_commit=False)
    return _SyncSession

@contextmanager
def get_sync_session() -> Session:
    factory = _get_sync_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
