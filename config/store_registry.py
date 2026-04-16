"""
config/store_registry.py

Loads store configuration from platform.* tables.

Usage:
    store = await load_store(chat_id="8525501774")
    store = await load_store(store_id="moraine")
    stores = await load_all_active_stores()

StoreProfile is the single object passed around when the backend needs to know
anything about a store — workflows, pos_type, scheduler policies, etc.

If something breaks and the wrong store is picked for a Telegram message,
look here first: get_store_by_chat_id() is the routing entry point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

log = logging.getLogger(__name__)


@dataclass
class WorkflowFlags:
    daily_report_enabled: bool = True
    daily_report_mode: str = "nrs_pull"        # nrs_pull | modisoft_pull | manual_entry
    manual_entry_enabled: bool = True
    nightly_sheet_sync: bool = True
    bank_recon_enabled: bool = False
    month_end_summary: bool = True
    weekly_bank_summary: bool = True
    invoice_ocr_enabled: bool = False
    unified_agent_enabled: bool = True


@dataclass
class SchedulerPolicy:
    job_name: str
    schedule: str                              # cron expression or label
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class StoreProfile:
    store_id: str
    store_name: str
    pos_type: str                              # nrs | modisoft | manual
    chat_id: str
    timezone: str
    workflows: WorkflowFlags = field(default_factory=WorkflowFlags)
    scheduler_policies: list[SchedulerPolicy] = field(default_factory=list)

    def get_scheduler_policy(self, job_name: str) -> SchedulerPolicy | None:
        for p in self.scheduler_policies:
            if p.job_name == job_name:
                return p
        return None


# ---------------------------------------------------------------------------
# Internal loader
# ---------------------------------------------------------------------------

async def _load_profile_from_db(store_row) -> StoreProfile:
    """Build a StoreProfile from a platform.stores row + related rows."""
    from db.database import get_async_session
    from db.models import StoreWorkflow, StoreSchedulerPolicy

    store_id = store_row.store_id

    async with get_async_session() as session:
        wf_row = (await session.execute(
            select(StoreWorkflow).where(StoreWorkflow.store_id == store_id)
        )).scalars().first()

        policy_rows = (await session.execute(
            select(StoreSchedulerPolicy).where(StoreSchedulerPolicy.store_id == store_id)
        )).scalars().all()

    workflows = WorkflowFlags()
    if wf_row:
        workflows = WorkflowFlags(
            daily_report_enabled=wf_row.daily_report_enabled,
            daily_report_mode=wf_row.daily_report_mode,
            manual_entry_enabled=wf_row.manual_entry_enabled,
            nightly_sheet_sync=wf_row.nightly_sheet_sync,
            bank_recon_enabled=wf_row.bank_recon_enabled,
            month_end_summary=wf_row.month_end_summary,
            weekly_bank_summary=wf_row.weekly_bank_summary,
            invoice_ocr_enabled=wf_row.invoice_ocr_enabled,
            unified_agent_enabled=wf_row.unified_agent_enabled,
        )

    policies = [
        SchedulerPolicy(
            job_name=p.job_name,
            schedule=p.schedule,
            enabled=p.enabled,
            config=p.config or {},
        )
        for p in policy_rows
    ]

    return StoreProfile(
        store_id=store_id,
        store_name=store_row.store_name,
        pos_type=store_row.pos_type,
        chat_id=store_row.chat_id,
        timezone=store_row.timezone,
        workflows=workflows,
        scheduler_policies=policies,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def load_store(*, chat_id: str | None = None, store_id: str | None = None) -> StoreProfile | None:
    """
    Load a StoreProfile by chat_id or store_id.

    Returns None if no active store matches — caller should silently ignore
    the message (it came from an unknown/unauthorized chat).
    """
    from db.database import get_async_session
    from db.models import Store

    if not chat_id and not store_id:
        raise ValueError("provide chat_id or store_id")

    async with get_async_session() as session:
        if chat_id:
            row = (await session.execute(
                select(Store).where(Store.chat_id == str(chat_id), Store.is_active == True)
            )).scalars().first()
        else:
            row = (await session.execute(
                select(Store).where(Store.store_id == store_id, Store.is_active == True)
            )).scalars().first()

    if not row:
        return None

    return await _load_profile_from_db(row)


async def load_all_active_stores() -> list[StoreProfile]:
    """Load all active stores. Called at scheduler startup to register per-store jobs."""
    from db.database import get_async_session
    from db.models import Store

    async with get_async_session() as session:
        rows = (await session.execute(
            select(Store).where(Store.is_active == True)
        )).scalars().all()

    profiles = []
    for row in rows:
        try:
            profiles.append(await _load_profile_from_db(row))
        except Exception as e:
            log.error("Failed to load profile for store_id=%s: %s", row.store_id, e)

    return profiles
