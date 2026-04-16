"""
Gas Station Agent — Entry Point

Runs the interactive Telegram bot continuously.
At 7:00 AM every day the bot fetches NRS data and sends the left-side
daily sheet to Telegram, then waits for you to provide the right-side
numbers (lotto payout, ATM, etc.) to complete the sheet.

Telegram updates are received via webhook (POST /telegram-webhook)
rather than polling — eliminates Telegram Conflict errors.

Run: python main.py
"""

import asyncio
import logging
import signal
import sys
from datetime import date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot import build_app, scheduled_daily, notify_bank_sync_results
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


async def run_bot() -> None:
    app = build_app()

    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    tz = settings.timezone

    # Daily NRS fetch — 7:00 AM store timezone
    scheduler.add_job(
        scheduled_daily,
        args=[app],
        trigger=CronTrigger(hour=7, minute=0, timezone=tz),
        id="daily_fetch",
        name="Daily NRS Fetch",
        replace_existing=True,
    )

    # Sheets → PostgreSQL sync every 15 minutes
    scheduler.add_job(
        run_nightly_sync,
        args=[settings.store_id],
        trigger=IntervalTrigger(minutes=15),
        id="nightly_sync",
        name="Nightly Sheets → PostgreSQL Sync",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Bank sync every 4 hours — pull new transactions and notify via Telegram
    async def _scheduled_bank_sync():
        from tools.plaid_tools import is_connected, sync_transactions
        try:
            if await is_connected(settings.store_id):
                result = await sync_transactions(settings.store_id)
                added = result.get("added", 0)
                needs = len(result.get("needs_review", []))
                autos = len(result.get("auto_list", []))
                if added or needs or autos:
                    await notify_bank_sync_results(result, app.bot)
                    log.info("Scheduled bank sync: added=%d needs_review=%d auto=%d", added, needs, autos)
                else:
                    log.info("Scheduled bank sync: no new transactions")
        except Exception as e:
            log.warning("Scheduled bank sync failed: %s", e)

    scheduler.add_job(
        _scheduled_bank_sync,
        trigger=IntervalTrigger(hours=4),
        id="bank_sync",
        name="Bank Transaction Sync (every 4h)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Weekly bank summary — every Sunday at 6 PM
    from tools.weekly_bank_summary import send_weekly_bank_summary
    scheduler.add_job(
        send_weekly_bank_summary,
        args=[settings.store_id, app.bot, settings.telegram_chat_id],
        trigger=CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=tz),
        id="weekly_bank_summary",
        name="Weekly Bank Summary (Sunday 6 PM)",
        replace_existing=True,
    )

    # Month-end cash flow summary — last day of month at 8:00 AM
    from tools.cashflow import run_cash_flow_summary
    scheduler.add_job(
        run_cash_flow_summary,
        args=[settings.store_id, app.bot, settings.telegram_chat_id],
        trigger=CronTrigger(day="last", hour=8, minute=0, timezone=tz),
        id="month_end_cashflow",
        name="Month-End Cash Flow Summary",
        replace_existing=True,
    )

    # Raw NRS payload retention — delete rows older than 90 days (runs weekly Sunday 3 AM)
    async def _purge_old_raw_nrs_payloads() -> None:
        try:
            from datetime import timedelta
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
            log.error("Raw NRS payload retention job failed: %s", e)

    scheduler.add_job(
        _purge_old_raw_nrs_payloads,
        trigger=CronTrigger(day_of_week="sun", hour=3, minute=0, timezone=tz),
        id="purge_raw_nrs_payloads",
        name="Raw NRS Payload Retention (90-day purge)",
        replace_existing=True,
    )

    async with app:
        await app.start()


        scheduler.start()

        # Start webhook server — Telegram pushes updates to WEBHOOK_URL
        await app.updater.start_webhook(
            listen="0.0.0.0",
            port=WEBHOOK_PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=WEBHOOK_URL,
            drop_pending_updates=True,
        )
        log.info("Webhook active: %s → listening on port %d", WEBHOOK_URL, WEBHOOK_PORT)

        stop_event = asyncio.Event()

        def _handle_signal():
            log.info("Shutdown signal received — stopping bot cleanly.")
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _handle_signal)

        try:
            await stop_event.wait()
        finally:
            scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    asyncio.run(run_bot())
