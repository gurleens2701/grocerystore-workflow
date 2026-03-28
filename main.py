"""
Gas Station Agent — Entry Point

Runs the interactive Telegram bot continuously.
At 7:00 AM every day the bot fetches NRS data and sends the left-side
daily sheet to Telegram, then waits for you to provide the right-side
numbers (lotto payout, ATM, etc.) to complete the sheet.

At midnight every day the nightly sync runs: reads Google Sheets and
reconciles any manual owner edits back to PostgreSQL.

Telegram commands:
  /daily  — manually trigger the daily fetch

Run now (for testing):  python main.py --now
Run on schedule:        python main.py
"""

import argparse
import asyncio
import logging
import signal
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot import build_app, scheduled_daily
from config.settings import settings
from tools.sync import run_nightly_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


async def run_bot(hour: int = 7, minute: int = 0, run_now: bool = False) -> None:
    app = build_app()

    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    tz = settings.timezone

    # Daily NRS fetch — 7:00 AM store timezone
    scheduler.add_job(
        scheduled_daily,
        args=[app],
        trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
        id="daily_fetch",
        name="Daily NRS Fetch",
        replace_existing=True,
    )

    # Nightly sync — midnight, reconciles Sheets edits → PostgreSQL
    scheduler.add_job(
        run_nightly_sync,
        args=[settings.store_id],
        trigger=CronTrigger(hour=0, minute=0, timezone=tz),
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

    # Weekly health score — every Monday at 8:00 AM
    from tools.health_score import send_weekly_health_score
    scheduler.add_job(
        send_weekly_health_score,
        args=[settings.store_id, app.bot, settings.telegram_chat_id],
        trigger=CronTrigger(day_of_week="mon", hour=8, minute=0, timezone=tz),
        id="weekly_health_score",
        name="Weekly Health Score",
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

        if run_now:
            log.info("--now flag: running daily fetch immediately.")
            await scheduled_daily(app)
        else:
            log.info(
                "Bot started. Daily fetch at %02d:%02d %s. Nightly sync at 00:00. "
                "Send /daily in Telegram to trigger manually.",
                hour, minute, settings.timezone,
            )

        scheduler.start()
        await app.updater.start_polling(drop_pending_updates=False)

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Gas Station Telegram Bot")
    parser.add_argument("--now", action="store_true", help="Trigger daily fetch immediately on startup")
    parser.add_argument("--hour", type=int, default=7, help="Hour to run daily (24h, default: 7)")
    parser.add_argument("--minute", type=int, default=0, help="Minute to run daily (default: 0)")
    args = parser.parse_args()

    asyncio.run(run_bot(hour=args.hour, minute=args.minute, run_now=args.now))


if __name__ == "__main__":
    main()
