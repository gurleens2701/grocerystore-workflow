"""
Persistent state helpers — replaces the in-memory _pending dict in bot.py.

State survives bot restarts because it's stored in PostgreSQL.
Each store has its own state rows (isolated by store_id).

Usage:
    await save_state("moraine", "sales", {"product_sales": 1234.56, ...})
    data = await get_state("moraine", "sales")   # → dict or None
    await clear_state("moraine", "sales")
    await clear_all_state("moraine")
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from db.database import get_async_session
from db.models import PendingState


async def save_state(store_id: str, key: str, data: dict[str, Any]) -> None:
    """Upsert state data for (store_id, key). Creates or overwrites."""
    async with get_async_session() as session:
        stmt = insert(PendingState).values(
            store_id=store_id,
            state_key=key,
            state_data=data,
        ).on_conflict_do_update(
            index_elements=["store_id", "state_key"],
            set_={"state_data": data, "created_at": func.now()},
        )
        await session.execute(stmt)


async def get_state(store_id: str, key: str) -> dict[str, Any] | None:
    """Return state data for (store_id, key), or None if not found."""
    async with get_async_session() as session:
        result = await session.execute(
            select(PendingState).where(
                PendingState.store_id == store_id,
                PendingState.state_key == key,
            )
        )
        row = result.scalar_one_or_none()
        return row.state_data if row else None


async def clear_state(store_id: str, key: str) -> None:
    """Delete state for (store_id, key)."""
    async with get_async_session() as session:
        await session.execute(
            delete(PendingState).where(
                PendingState.store_id == store_id,
                PendingState.state_key == key,
            )
        )


async def get_state_age_hours(store_id: str, key: str) -> float | None:
    """Return age in hours of the state row, or None if not found."""
    async with get_async_session() as session:
        result = await session.execute(
            select(PendingState).where(
                PendingState.store_id == store_id,
                PendingState.state_key == key,
            )
        )
        row = result.scalar_one_or_none()
        if not row or not row.created_at:
            return None
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - created
        return delta.total_seconds() / 3600.0


async def clear_all_state(store_id: str) -> None:
    """Delete all pending state rows for a store."""
    async with get_async_session() as session:
        await session.execute(
            delete(PendingState).where(PendingState.store_id == store_id)
        )
