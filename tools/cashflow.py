"""
Month-end cash flow summary.

Queries PostgreSQL for the full month's DailySales, Expense, Invoice,
Rebate, and Revenue rows, computes a summary dict, formats it via Claude
Sonnet, sends it to Telegram, and saves the raw data as a JSON file.
"""

import json
import logging
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import anthropic
from sqlalchemy import func, select

from config.settings import settings
from db.database import get_async_session
from db.models import DailySales, Expense, Invoice, Rebate, Revenue
from tools.alerts import run_anomaly_checks

log = logging.getLogger(__name__)

_MONTH_NAMES = [
    "", "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
    "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
]


def _dec(value) -> Decimal:
    """Coerce None / float to Decimal safely."""
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


async def generate_cash_flow(store_id: str, year: int, month: int) -> dict:
    """
    Query PostgreSQL for the full month and return a summary dict.
    """
    month_name = _MONTH_NAMES[month]
    period = f"{month_name} {year}"

    # ------------------------------------------------------------------ sales
    best_day = None
    worst_day = None
    total_sales = Decimal("0")
    over_short_total = Decimal("0")
    over_short_count = 0
    total_days = 0

    async with get_async_session() as session:
        sales_rows = (await session.execute(
            select(DailySales.sale_date, DailySales.product_sales, DailySales.over_short).where(
                DailySales.store_id == store_id,
                func.extract("year", DailySales.sale_date) == year,
                func.extract("month", DailySales.sale_date) == month,
            ).order_by(DailySales.sale_date)
        )).all()

    for row in sales_rows:
        ps = _dec(row.product_sales)
        total_sales += ps
        total_days += 1
        if best_day is None or ps > best_day["amount"]:
            best_day = {"date": row.sale_date, "amount": ps}
        if worst_day is None or ps < worst_day["amount"]:
            worst_day = {"date": row.sale_date, "amount": ps}
        if row.over_short is not None:
            over_short_total += _dec(row.over_short)
            over_short_count += 1

    avg_daily_sales = (total_sales / total_days) if total_days else Decimal("0")
    over_short_avg = (over_short_total / over_short_count) if over_short_count else Decimal("0")

    # --------------------------------------------------------------- expenses
    async with get_async_session() as session:
        expense_rows = (await session.execute(
            select(Expense.category, Expense.amount).where(
                Expense.store_id == store_id,
                func.extract("year", Expense.expense_date) == year,
                func.extract("month", Expense.expense_date) == month,
            )
        )).all()

    expenses_by_category: dict[str, Decimal] = {}
    total_expenses = Decimal("0")
    for row in expense_rows:
        amt = _dec(row.amount)
        expenses_by_category[row.category] = expenses_by_category.get(row.category, Decimal("0")) + amt
        total_expenses += amt

    # --------------------------------------------------------------- invoices
    async with get_async_session() as session:
        invoice_rows = (await session.execute(
            select(Invoice.amount).where(
                Invoice.store_id == store_id,
                func.extract("year", Invoice.invoice_date) == year,
                func.extract("month", Invoice.invoice_date) == month,
            )
        )).all()

    total_invoices = sum((_dec(r.amount) for r in invoice_rows), Decimal("0"))

    # ---------------------------------------------------------------- rebates
    async with get_async_session() as session:
        rebate_rows = (await session.execute(
            select(Rebate.vendor, Rebate.amount).where(
                Rebate.store_id == store_id,
                func.extract("year", Rebate.rebate_date) == year,
                func.extract("month", Rebate.rebate_date) == month,
            )
        )).all()

    rebates_by_vendor: dict[str, Decimal] = {}
    total_rebates = Decimal("0")
    for row in rebate_rows:
        amt = _dec(row.amount)
        rebates_by_vendor[row.vendor] = rebates_by_vendor.get(row.vendor, Decimal("0")) + amt
        total_rebates += amt

    # --------------------------------------------------------------- revenues
    async with get_async_session() as session:
        revenue_rows = (await session.execute(
            select(Revenue.amount).where(
                Revenue.store_id == store_id,
                func.extract("year", Revenue.revenue_date) == year,
                func.extract("month", Revenue.revenue_date) == month,
            )
        )).all()

    total_revenue_taken = sum((_dec(r.amount) for r in revenue_rows), Decimal("0"))

    # ------------------------------------------------------------ net cash flow
    net_cash_flow = total_sales - total_expenses - total_invoices + total_rebates

    # --------------------------------------------------------- anomaly alerts
    check_date = date(year, month, 1)
    alerts = await run_anomaly_checks(store_id, today=check_date)

    return {
        "period": period,
        "total_sales": total_sales,
        "total_days": total_days,
        "avg_daily_sales": avg_daily_sales,
        "best_day": best_day,
        "worst_day": worst_day,
        "total_expenses": total_expenses,
        "expenses_by_category": expenses_by_category,
        "total_invoices": total_invoices,
        "total_rebates": total_rebates,
        "rebates_by_vendor": rebates_by_vendor,
        "total_revenue_taken": total_revenue_taken,
        "net_cash_flow": net_cash_flow,
        "over_short_total": over_short_total,
        "over_short_avg": over_short_avg,
        "alerts": alerts,
    }


async def format_cash_flow_message(data: dict) -> str:
    """
    Use Claude Sonnet to generate a clean Telegram-ready message from the
    cash flow summary dict.  Kept under 4000 characters.
    """
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Serialize Decimal / date values for the prompt
    def _serialise(obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, date):
            return obj.strftime("%b %d")
        return str(obj)

    data_str = json.dumps(data, default=_serialise, indent=2)

    system_prompt = (
        "You are a financial assistant for a gas station convenience store. "
        "Format the provided monthly cash flow data into a concise, clean Telegram message. "
        "Use plain text (no HTML). You may use emoji sparingly. "
        "Keep the total message under 4000 characters. "
        "Structure: header with period, sales summary, expenses breakdown, invoices/rebates, "
        "net cash flow, over/short, and any alerts at the bottom."
    )

    user_prompt = (
        f"Here is the month-end cash flow data for the store:\n\n{data_str}\n\n"
        "Please write a clear Telegram message summarizing this data for the store owner."
    )

    message = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return message.content[0].text


async def run_cash_flow_summary(
    store_id: str,
    bot,
    chat_id: str,
    year: int = None,
    month: int = None,
) -> None:
    """
    Main entry point called by APScheduler or /cashflow Telegram command.
    Generates the summary, formats it, sends to Telegram, and saves JSON.
    """
    today = date.today()
    if year is None:
        year = today.year
    if month is None:
        month = today.month

    month_name = _MONTH_NAMES[month].capitalize()
    log.info("[%s] Generating cash flow summary for %s %d …", store_id, month_name, year)

    try:
        data = await generate_cash_flow(store_id, year, month)
    except Exception:
        log.exception("[%s] Failed to generate cash flow data.", store_id)
        await bot.send_message(
            chat_id=chat_id,
            text="❌ Cash flow summary failed — check logs.",
        )
        return

    # Save raw JSON --------------------------------------------------------
    report_dir = Path("reports") / store_id / str(year) / month_name
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"cashflow-{month:02d}-{year}.json"

    def _json_serial(obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, date):
            return obj.isoformat()
        raise TypeError(f"Type {type(obj)} not serialisable")

    with open(report_path, "w") as fh:
        json.dump(data, fh, default=_json_serial, indent=2)
    log.info("[%s] Saved cash flow JSON → %s", store_id, report_path)

    # Format and send ------------------------------------------------------
    try:
        message_text = await format_cash_flow_message(data)
    except Exception:
        log.exception("[%s] Claude formatting failed; sending raw summary.", store_id)
        message_text = (
            f"📊 *{data['period']} CASH FLOW*\n\n"
            f"Total Sales: ${data['total_sales']:,.2f}\n"
            f"Days: {data['total_days']}\n"
            f"Expenses: ${data['total_expenses']:,.2f}\n"
            f"Invoices: ${data['total_invoices']:,.2f}\n"
            f"Rebates: +${data['total_rebates']:,.2f}\n"
            f"Net Cash Flow: ${data['net_cash_flow']:,.2f}\n"
            f"Over/Short Avg: ${data['over_short_avg']:,.2f}"
        )

    await bot.send_message(
        chat_id=chat_id,
        text=message_text,
    )
    log.info("[%s] Sent cash flow summary to Telegram.", store_id)
