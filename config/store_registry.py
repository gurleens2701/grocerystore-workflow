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
class DailyReportRule:
    """One field on the daily sheet. source='api' → from NRS; 'manual' → owner types it."""
    field_name: str
    label: str
    source: str                               # api | manual
    section: str                              # left | right
    display_order: int


@dataclass
class SheetMapping:
    """Maps a field_name to a 1-based column_index in a Google Sheet section."""
    section: str
    field_name: str
    column_index: int
    column_header: str


@dataclass
class StoreProfile:
    store_id: str
    store_name: str
    pos_type: str                              # nrs | modisoft | manual
    chat_id: str
    timezone: str
    workflows: WorkflowFlags = field(default_factory=WorkflowFlags)
    scheduler_policies: list[SchedulerPolicy] = field(default_factory=list)
    daily_report_rules: list[DailyReportRule] = field(default_factory=list)
    sheet_mappings: list[SheetMapping] = field(default_factory=list)
    enabled_tools: set[str] = field(default_factory=set)

    def get_scheduler_policy(self, job_name: str) -> SchedulerPolicy | None:
        for p in self.scheduler_policies:
            if p.job_name == job_name:
                return p
        return None

    def get_manual_rules(self) -> list[DailyReportRule]:
        """Returns right-side fields the owner must enter, sorted by display_order."""
        return sorted(
            [r for r in self.daily_report_rules if r.source == "manual"],
            key=lambda r: r.display_order,
        )

    def get_sheet_column(self, section: str, field_name: str) -> int | None:
        """Returns 1-based column index for a field in the given sheet section."""
        for m in self.sheet_mappings:
            if m.section == section and m.field_name == field_name:
                return m.column_index
        return None


# ---------------------------------------------------------------------------
# Internal loader
# ---------------------------------------------------------------------------

async def _load_profile_from_db(store_row) -> StoreProfile:
    """Build a StoreProfile from a platform.stores row + all related config rows."""
    from db.database import get_async_session
    from db.models import (
        StoreWorkflow, StoreSchedulerPolicy,
        StoreDailyReportRule, StoreSheetMapping, StoreToolPolicy,
    )

    store_id = store_row.store_id

    async with get_async_session() as session:
        wf_row = (await session.execute(
            select(StoreWorkflow).where(StoreWorkflow.store_id == store_id)
        )).scalars().first()

        policy_rows = (await session.execute(
            select(StoreSchedulerPolicy).where(StoreSchedulerPolicy.store_id == store_id)
        )).scalars().all()

        rule_rows = (await session.execute(
            select(StoreDailyReportRule)
            .where(StoreDailyReportRule.store_id == store_id)
            .order_by(StoreDailyReportRule.display_order)
        )).scalars().all()

        mapping_rows = (await session.execute(
            select(StoreSheetMapping).where(StoreSheetMapping.store_id == store_id)
        )).scalars().all()

        tool_rows = (await session.execute(
            select(StoreToolPolicy).where(StoreToolPolicy.store_id == store_id)
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
        SchedulerPolicy(job_name=p.job_name, schedule=p.schedule,
                        enabled=p.enabled, config=p.config or {})
        for p in policy_rows
    ]

    report_rules = [
        DailyReportRule(field_name=r.field_name, label=r.label, source=r.source,
                        section=r.section, display_order=r.display_order)
        for r in rule_rows
    ]

    mappings = [
        SheetMapping(section=m.section, field_name=m.field_name,
                     column_index=m.column_index, column_header=m.column_header)
        for m in mapping_rows
    ]

    enabled_tools = {t.tool_name for t in tool_rows if t.enabled}

    return StoreProfile(
        store_id=store_id,
        store_name=store_row.store_name,
        pos_type=store_row.pos_type,
        chat_id=store_row.chat_id,
        timezone=store_row.timezone,
        workflows=workflows,
        scheduler_policies=policies,
        daily_report_rules=report_rules,
        sheet_mappings=mappings,
        enabled_tools=enabled_tools,
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
