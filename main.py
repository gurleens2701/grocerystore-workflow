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

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot import build_app, scheduled_daily
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

    async with app:
        await app.start()

        # Send any anomaly alerts queued by last night's sync
        from db.state import get_state, clear_state
        pending = await get_state(settings.store_id, "pending_alerts")
        if pending and pending.get("alerts"):
            alerts_text = "\n".join(pending["alerts"])
            await app.bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=f"🚨 *Anomaly Alerts ({pending.get('date', 'yesterday')})*\n\n{alerts_text}",
                parse_mode="Markdown",
            )
            await clear_state(settings.store_id, "pending_alerts")

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
