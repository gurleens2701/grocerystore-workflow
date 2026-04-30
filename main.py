"""
Gas Station Agent — Entry Point

Telegram bot + scheduler. All jobs are DB-driven:
  platform.store_scheduler_policies controls which jobs run, when, and for which store.

Adding a new store: run scripts/onboard_store.py, then restart. No code changes.

Run: python main.py
"""

import asyncio
import logging
import signal
import sys
from datetime import date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot import (
    build_app,
    scheduled_daily_for_store,
    bank_sync_for_store,
    notify_bank_sync_results,
)
from config.settings import settings
from tools.sync import run_nightly_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram-webhook"
WEBHOOK_PORT = 8080
WEBHOOK_URL  = f"https://clerkai.live{WEBHOOK_PATH}"


# ---------------------------------------------------------------------------
# Schedule parser
# ---------------------------------------------------------------------------

def _parse_trigger(schedule: str, tz: str):
    """
    Convert a schedule string from platform.store_scheduler_policies to an APScheduler trigger.

    Supported formats:
      "every_4h"       → IntervalTrigger(hours=4)
      "every_15m"      → IntervalTrigger(minutes=15)
      "0 8 L * *"      → CronTrigger(day="last", hour=8, minute=0)
      "0 7 * * *"      → CronTrigger.from_crontab (standard 5-field cron)
    """
    if schedule == "every_4h":
        return IntervalTrigger(hours=4)
    if schedule == "every_15m":
        return IntervalTrigger(minutes=15)

    parts = schedule.split()
    if len(parts) == 5 and parts[2].upper() == "L":
        # Last day of month: "0 8 L * *" — APScheduler uses day="last"
        return CronTrigger(minute=parts[0], hour=parts[1], day="last", timezone=tz)

    # Standard 5-field cron — from_crontab handles 0=Sunday correctly
    return CronTrigger.from_crontab(schedule, timezone=tz)


# ---------------------------------------------------------------------------
# Per-store workflow dispatcher
# ---------------------------------------------------------------------------

def _make_workflow_runner(store_id: str, job_name: str, app):
    """Return a zero-arg async callable for scheduler.add_job."""
    async def _run():
        try:
            await _dispatch_workflow(store_id, job_name, app)
        except Exception as e:
            log.error("Unhandled error in workflow %s/%s: %s", store_id, job_name, e, exc_info=True)
    return _run


async def _dispatch_workflow(store_id: str, job_name: str, app) -> None:
    """Route a job_name to the correct workflow function for the given store."""
    from config.store_context import set_active_store
    set_active_store(store_id)

    if job_name == "daily_fetch":
        await scheduled_daily_for_store(store_id, app)

    elif job_name == "bank_sync":
        await bank_sync_for_store(store_id, app)

    elif job_name == "nightly_sync":
        await run_nightly_sync(store_id)

    elif job_name == "weekly_summary":
        from config.store_registry import load_store
        from tools.weekly_bank_summary import send_weekly_bank_summary
        store = await load_store(store_id=store_id)
        if store:
            await send_weekly_bank_summary(store_id, app.bot, store.chat_id)

    elif job_name == "cashflow":
        from config.store_registry import load_store
        from tools.cashflow import run_cash_flow_summary
        store = await load_store(store_id=store_id)
        if store:
            await run_cash_flow_summary(store_id, app.bot, store.chat_id)

    else:
        log.warning("Unknown job_name=%r for store_id=%s — skipping", job_name, store_id)


# ---------------------------------------------------------------------------
# Main bot runner
# ---------------------------------------------------------------------------

async def run_bot() -> None:
    app = build_app()
    tz = settings.timezone
    scheduler = AsyncIOScheduler(timezone=tz)

    # --- DB-driven per-store jobs ---
    from config.store_registry import load_all_active_stores
    stores = await load_all_active_stores()

    if not stores:
        log.error("Privacy guard: no active stores in platform.stores; no store jobs registered")
    else:
        registered = 0
        for store in stores:
            for policy in store.scheduler_policies:
                if not policy.enabled:
                    continue
                try:
                    trigger = _parse_trigger(policy.schedule, tz)
                except Exception as e:
                    log.error(
                        "store=%s job=%s bad schedule %r: %s — skipping",
                        store.store_id, policy.job_name, policy.schedule, e,
                    )
                    continue

                job_id = f"{policy.job_name}_{store.store_id}"
                scheduler.add_job(
                    _make_workflow_runner(store.store_id, policy.job_name, app),
                    trigger=trigger,
                    id=job_id,
                    name=f"{policy.job_name} [{store.store_name}]",
                    replace_existing=True,
                    misfire_grace_time=600,
                )
                log.info("Registered job: %s  schedule=%s", job_id, policy.schedule)
                registered += 1

        log.info("Registered %d per-store jobs across %d stores", registered, len(stores))

    # --- Platform-level jobs (not per-store) ---

    # Raw NRS payload retention — 90-day purge, every Sunday at 3 AM
    async def _purge_raw_nrs():
        try:
            from sqlalchemy import text as sa_text
            from db.database import get_async_session
            cutoff = date.today() - timedelta(days=90)
            async with get_async_session() as session:
                result = await session.execute(
                    sa_text("DELETE FROM raw_nrs.raw_sales_payloads WHERE fetched_at < :cutoff"),
                    {"cutoff": cutoff},
                )
                await session.commit()
            log.info("Purged %d raw NRS payload rows older than %s", result.rowcount, cutoff)
        except Exception as e:
            log.error("Raw NRS payload retention failed: %s", e)

    scheduler.add_job(
        _purge_raw_nrs,
        trigger=CronTrigger(day_of_week="sun", hour=3, minute=0, timezone=tz),
        id="purge_raw_nrs_payloads",
        name="Raw NRS Payload Retention (90-day purge)",
        replace_existing=True,
    )

    async with app:
        await app.start()
        scheduler.start()

        await app.updater.start_webhook(
            listen="0.0.0.0",
            port=WEBHOOK_PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=WEBHOOK_URL,
            drop_pending_updates=True,
        )
        log.info("Webhook active: %s → listening on port %d", WEBHOOK_URL, WEBHOOK_PORT)

        stop_event = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: stop_event.set())

        try:
            await stop_event.wait()
        finally:
            scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    asyncio.run(run_bot())
