"""
Anomaly alert engine.

Runs alongside the nightly sync and at month-end.
Sends Telegram alerts for:
  1. New expense category this month not seen last month
  2. Recurring expense from last month missing so far this month
  3. Expected rebate vendor missing this month (was present last month)
  4. Over/short average worse than threshold (>$20 average absolute value)

All alerts are sent via Telegram to the store's chat ID.
"""

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from config.settings import settings
from db.database import get_async_session
from db.models import DailySales, Expense, Rebate

log = logging.getLogger(__name__)

# Over/short alert threshold — alert if monthly average absolute over/short > this
_OVER_SHORT_THRESHOLD = Decimal("20.00")


def _prev_month(d: date) -> tuple[int, int]:
    """Returns (year, month) of the previous month."""
    if d.month == 1:
        return d.year - 1, 12
    return d.year, d.month - 1


async def check_new_expenses(store_id: str, today: date) -> list[str]:
    """
    Returns alert messages for expense categories seen this month
    but not present in any of the previous 3 months.
    """
    alerts = []
    prev_year, prev_month = _prev_month(today)

    async with get_async_session() as session:
        # Categories used this month
        this_month_result = await session.execute(
            select(Expense.category).distinct().where(
                Expense.store_id == store_id,
                func.extract("year", Expense.expense_date) == today.year,
                func.extract("month", Expense.expense_date) == today.month,
            )
        )
        this_month_cats = {r[0] for r in this_month_result.all()}

        # Categories used last month
        last_month_result = await session.execute(
            select(Expense.category).distinct().where(
                Expense.store_id == store_id,
                func.extract("year", Expense.expense_date) == prev_year,
                func.extract("month", Expense.expense_date) == prev_month,
            )
        )
        last_month_cats = {r[0] for r in last_month_result.all()}

    # Only alert if last month actually had data — avoid false positives on new stores
    if not last_month_cats:
        return []

    new_cats = this_month_cats - last_month_cats
    for cat in sorted(new_cats):
        alerts.append(f"⚠️ *New expense category* not seen last month: *{cat}*")

    return alerts


async def check_missing_expenses(store_id: str, today: date) -> list[str]:
    """
    Returns alert messages for expense categories present last month
    but missing so far this month (after the 5th of the month).
    """
    if today.day < 5:
        return []  # Too early in month to flag missing expenses

    alerts = []
    prev_year, prev_month = _prev_month(today)

    async with get_async_session() as session:
        last_month_result = await session.execute(
            select(Expense.category).distinct().where(
                Expense.store_id == store_id,
                func.extract("year", Expense.expense_date) == prev_year,
                func.extract("month", Expense.expense_date) == prev_month,
            )
        )
        last_month_cats = {r[0] for r in last_month_result.all()}

        this_month_result = await session.execute(
            select(Expense.category).distinct().where(
                Expense.store_id == store_id,
                func.extract("year", Expense.expense_date) == today.year,
                func.extract("month", Expense.expense_date) == today.month,
            )
        )
        this_month_cats = {r[0] for r in this_month_result.all()}

    missing = last_month_cats - this_month_cats
    # Flag all categories that were present last month but missing this month
    for cat in sorted(missing):
        alerts.append(f"⚠️ *Missing expense* expected this month: *{cat}* (was logged last month)")

    return alerts


async def check_missing_rebates(store_id: str, today: date) -> list[str]:
    """
    Returns alert messages for rebate vendors present last month
    but missing so far this month (after the 10th).
    """
    if today.day < 10:
        return []

    alerts = []
    prev_year, prev_month = _prev_month(today)

    async with get_async_session() as session:
        last_month_result = await session.execute(
            select(Rebate.vendor).distinct().where(
                Rebate.store_id == store_id,
                func.extract("year", Rebate.rebate_date) == prev_year,
                func.extract("month", Rebate.rebate_date) == prev_month,
            )
        )
        last_month_vendors = {r[0] for r in last_month_result.all()}

        this_month_result = await session.execute(
            select(Rebate.vendor).distinct().where(
                Rebate.store_id == store_id,
                func.extract("year", Rebate.rebate_date) == today.year,
                func.extract("month", Rebate.rebate_date) == today.month,
            )
        )
        this_month_vendors = {r[0] for r in this_month_result.all()}

    missing = last_month_vendors - this_month_vendors
    for vendor in sorted(missing):
        alerts.append(f"⚠️ *Missing rebate* expected this month: *{vendor}* (received last month)")

    return alerts


async def check_over_short(store_id: str, today: date) -> list[str]:
    """
    Returns alert if the monthly average absolute over/short > threshold.
    Only checks if at least 7 days of data exist.
    """
    alerts = []

    async with get_async_session() as session:
        result = await session.execute(
            select(DailySales.over_short).where(
                DailySales.store_id == store_id,
                func.extract("year", DailySales.sale_date) == today.year,
                func.extract("month", DailySales.sale_date) == today.month,
                DailySales.over_short.isnot(None),
            )
        )
        values = [r[0] for r in result.all() if r[0] is not None]

    if len(values) < 7:
        return []

    avg_abs = sum(abs(v) for v in values) / len(values)
    if avg_abs > _OVER_SHORT_THRESHOLD:
        direction = "short" if sum(values) < 0 else "over"
        alerts.append(
            f"⚠️ *Over/Short alert*: average is ${avg_abs:.2f} ({direction}) "
            f"over {len(values)} days this month. Threshold: ${_OVER_SHORT_THRESHOLD:.2f}"
        )

    return alerts


async def _has_baseline(store_id: str, today: date, min_days: int = 30) -> bool:
    """
    Returns True only if the store has at least `min_days` of history.
    Anomaly detection needs a baseline to compare against — without one,
    every new category/vendor looks "new" and spams the owner with false alerts.
    """
    async with get_async_session() as session:
        result = await session.execute(
            select(func.min(DailySales.sale_date)).where(
                DailySales.store_id == store_id,
            )
        )
        earliest = result.scalar_one_or_none()
    if earliest is None:
        return False
    return (today - earliest).days >= min_days


async def run_anomaly_checks(store_id: str, today: date | None = None) -> list[str]:
    """
    Run all anomaly checks. Returns list of alert message strings.
    Call this from nightly sync or month-end summary.

    Skips entirely during the first 30 days after the store's first daily
    sales entry — anomaly detection needs a baseline to compare against.
    """
    if today is None:
        today = date.today()

    if not await _has_baseline(store_id, today):
        log.info("[%s] Skipping anomaly checks — less than 30 days of data.", store_id)
        return []

    all_alerts: list[str] = []

    checks = [
        check_new_expenses(store_id, today),
        check_missing_expenses(store_id, today),
        check_missing_rebates(store_id, today),
        check_over_short(store_id, today),
    ]

    import asyncio
    results = await asyncio.gather(*checks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            log.error("Anomaly check failed: %s", result, exc_info=True)
        else:
            all_alerts.extend(result)

    return all_alerts


async def send_alerts(store_id: str, bot, chat_id: str, today: date | None = None) -> None:
    """
    Run all anomaly checks and send any alerts via Telegram.
    Call this from nightly sync job.
    """
    alerts = await run_anomaly_checks(store_id, today)
    if not alerts:
        log.info("[%s] No anomalies detected.", store_id)
        return

    msg = "\n".join(alerts)
    await bot.send_message(
        chat_id=chat_id,
        text=f"🚨 *Daily Anomaly Report — {date.today()}*\n\n{msg}",
        parse_mode="Markdown",
    )
    log.info("[%s] Sent %d anomaly alerts.", store_id, len(alerts))
