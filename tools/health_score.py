"""
tools/health_score.py

Weekly store health score — sent every Monday at 8 AM.

Score breakdown (100 pts total):
  - Days logged out of 7          (40 pts)
  - Over/short average tightness  (40 pts)
  - Expense ratio flag            (20 pts)
"""

import asyncio
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, select

from db.database import get_async_session
from db.models import DailySales, Expense, Invoice


def _week_range() -> tuple[date, date]:
    """Return Monday–Sunday of the previous week."""
    today = date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


def _fmt(amount: Decimal | float) -> str:
    return f"${float(amount):,.2f}"


def _score_days_logged(days_logged: int) -> int:
    """40 pts for 7 days, proportional."""
    return round((days_logged / 7) * 40)


def _score_over_short(avg_abs: float) -> int:
    """
    40 pts for perfect balance, drops as average over/short grows.
    $0–$2   → 40 pts
    $2–$5   → 30 pts
    $5–$10  → 20 pts
    $10–$20 → 10 pts
    >$20    → 0 pts
    """
    if avg_abs <= 2:
        return 40
    elif avg_abs <= 5:
        return 30
    elif avg_abs <= 10:
        return 20
    elif avg_abs <= 20:
        return 10
    return 0


def _score_expense_ratio(ratio: float) -> int:
    """
    20 pts if expense ratio is reasonable.
    Expenses / sales:
    <20%  → 20 pts
    20–30% → 15 pts
    30–40% → 10 pts
    >40%  → 0 pts
    """
    if ratio < 0.20:
        return 20
    elif ratio < 0.30:
        return 15
    elif ratio < 0.40:
        return 10
    return 0


def _score_label(score: int) -> str:
    if score >= 85:
        return "🟢 Excellent"
    elif score >= 70:
        return "🟡 Good"
    elif score >= 50:
        return "🟠 Needs Attention"
    return "🔴 Poor"


def _over_short_label(avg: float) -> str:
    if avg <= 2:
        return "great ✅"
    elif avg <= 5:
        return "acceptable"
    elif avg <= 10:
        return "high ⚠️"
    return "very high 🔴"


async def _build_health_score_async(store_id: str) -> str:
    week_start, week_end = _week_range()

    async with get_async_session() as session:

        # ── Daily sales for the week ────────────────────────────────────────
        sales_rows = await session.execute(
            select(DailySales).where(
                and_(
                    DailySales.store_id == store_id,
                    DailySales.sale_date >= week_start,
                    DailySales.sale_date <= week_end,
                )
            )
        )
        sales_rows = sales_rows.scalars().all()

        days_logged = len(sales_rows)
        total_sales = sum(float(r.grand_total or 0) for r in sales_rows)

        # Over/short: total_payments - grand_total per day
        over_shorts = []
        for r in sales_rows:
            if r.lotto_po is not None:  # right side was filled in
                total_payments = sum(float(getattr(r, col) or 0) for col in [
                    "cash_drop", "card", "check_amount", "lotto_po", "lotto_cr",
                    "atm", "pull_tab", "coupon", "food_stamp", "loyalty", "vendor_payout"
                ])
                diff = total_payments - float(r.grand_total or 0)
                over_shorts.append(diff)

        avg_over_short = (sum(abs(x) for x in over_shorts) / len(over_shorts)) if over_shorts else 0.0

        # ── Invoices (inventory bought) ─────────────────────────────────────
        inv_result = await session.execute(
            select(func.sum(Invoice.amount)).where(
                and_(
                    Invoice.store_id == store_id,
                    Invoice.invoice_date >= week_start,
                    Invoice.invoice_date <= week_end,
                )
            )
        )
        inventory_bought = float(inv_result.scalar() or 0)

        # ── Expenses for the week ───────────────────────────────────────────
        exp_result = await session.execute(
            select(Expense.category, func.sum(Expense.amount)).where(
                and_(
                    Expense.store_id == store_id,
                    Expense.expense_date >= week_start,
                    Expense.expense_date <= week_end,
                )
            ).group_by(Expense.category)
        )
        expense_rows = exp_result.fetchall()

        payroll = 0.0
        other_expenses = 0.0
        for cat, amt in expense_rows:
            amt = float(amt or 0)
            if "payroll" in (cat or "").lower() or "salary" in (cat or "").lower():
                payroll += amt
            else:
                other_expenses += amt

        total_expenses = payroll + other_expenses
        expense_ratio = (total_expenses / total_sales) if total_sales > 0 else 0.0

    # ── Score calculation ───────────────────────────────────────────────────
    s_days = _score_days_logged(days_logged)
    s_os = _score_over_short(avg_over_short)
    s_exp = _score_expense_ratio(expense_ratio)
    total_score = s_days + s_os + s_exp
    label = _score_label(total_score)

    # ── Format report ───────────────────────────────────────────────────────
    week_str = f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')}"

    over_short_str = (
        f"${avg_over_short:.2f} avg  ({_over_short_label(avg_over_short)})"
        if over_shorts else "N/A — right-side not filled"
    )

    lines = [
        f"📊 *Weekly Health Score*",
        f"_{week_str}_",
        "",
        f"*{total_score}/100 — {label}*",
        "",
        "```",
        f"  Days logged        {days_logged}/7          ({s_days}/40 pts)",
        f"  Over/Short avg     {over_short_str:<22} ({s_os}/40 pts)",
        f"  Expense ratio      {expense_ratio*100:.1f}%{'':<20} ({s_exp}/20 pts)",
        "```",
        "",
        "💰 *This Week*",
        "```",
        f"  Sales              {_fmt(total_sales)}",
        f"  Inventory bought   {_fmt(inventory_bought)}",
    ]

    if payroll:
        lines.append(f"  Payroll            {_fmt(payroll)}")
    if other_expenses:
        lines.append(f"  Other expenses     {_fmt(other_expenses)}")

    lines += [
        "```",
    ]

    if days_logged < 7:
        missing = 7 - days_logged
        lines.append(f"\n_⚠️ {missing} day(s) not logged this week._")

    return "\n".join(lines)


async def _build_health_score_structured(store_id: str) -> dict:
    """Return health score as structured data for the web dashboard."""
    week_start, week_end = _week_range()

    async with get_async_session() as session:
        sales_rows = (await session.execute(
            select(DailySales).where(
                and_(DailySales.store_id == store_id,
                     DailySales.sale_date >= week_start,
                     DailySales.sale_date <= week_end)
            )
        )).scalars().all()

        days_logged = len(sales_rows)
        total_sales = sum(float(r.grand_total or 0) for r in sales_rows)

        over_shorts = []
        for r in sales_rows:
            if r.lotto_po is not None:
                total_payments = sum(float(getattr(r, col) or 0) for col in [
                    "cash_drop", "card", "check_amount", "lotto_po", "lotto_cr",
                    "atm", "pull_tab", "coupon", "food_stamp", "loyalty", "vendor_payout"
                ])
                over_shorts.append(total_payments - float(r.grand_total or 0))

        avg_over_short = (sum(abs(x) for x in over_shorts) / len(over_shorts)) if over_shorts else 0.0

        inventory_bought = float((await session.execute(
            select(func.sum(Invoice.amount)).where(
                and_(Invoice.store_id == store_id,
                     Invoice.invoice_date >= week_start,
                     Invoice.invoice_date <= week_end)
            )
        )).scalar() or 0)

        expense_rows = (await session.execute(
            select(Expense.category, func.sum(Expense.amount)).where(
                and_(Expense.store_id == store_id,
                     Expense.expense_date >= week_start,
                     Expense.expense_date <= week_end)
            ).group_by(Expense.category)
        )).fetchall()

    payroll = 0.0
    other_expenses = 0.0
    for cat, amt in expense_rows:
        amt = float(amt or 0)
        if "payroll" in (cat or "").lower() or "salary" in (cat or "").lower():
            payroll += amt
        else:
            other_expenses += amt

    total_expenses = payroll + other_expenses
    expense_ratio = (total_expenses / total_sales) if total_sales > 0 else 0.0

    s_days = _score_days_logged(days_logged)
    s_os = _score_over_short(avg_over_short)
    s_exp = _score_expense_ratio(expense_ratio)
    total_score = s_days + s_os + s_exp

    return {
        "week": f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')}",
        "score": total_score,
        "label": _score_label(total_score).split(" ", 1)[1],  # strip emoji
        "label_color": (
            "green" if total_score >= 85 else
            "yellow" if total_score >= 70 else
            "orange" if total_score >= 50 else "red"
        ),
        "metrics": [
            {
                "name": "Days Logged",
                "value": f"{days_logged}/7",
                "detail": f"{s_days}/40 pts",
                "score": s_days,
                "max": 40,
                "status": "good" if days_logged == 7 else "warn" if days_logged >= 5 else "bad",
            },
            {
                "name": "Over/Short Avg",
                "value": f"${avg_over_short:.2f}" if over_shorts else "N/A",
                "detail": f"{s_os}/40 pts — {_over_short_label(avg_over_short)}" if over_shorts else "right-side not filled",
                "score": s_os,
                "max": 40,
                "status": "good" if s_os >= 30 else "warn" if s_os >= 20 else "bad",
            },
            {
                "name": "Expense Ratio",
                "value": f"{expense_ratio*100:.1f}%",
                "detail": f"{s_exp}/20 pts",
                "score": s_exp,
                "max": 20,
                "status": "good" if s_exp >= 15 else "warn" if s_exp >= 10 else "bad",
            },
        ],
        "financials": {
            "sales": total_sales,
            "inventory": inventory_bought,
            "payroll": payroll,
            "other_expenses": other_expenses,
        },
        "days_missing": max(0, 7 - days_logged),
    }


async def send_weekly_health_score(store_id: str, bot, chat_id: str) -> None:
    """Called by the scheduler every Monday at 8 AM."""
    try:
        report = await _build_health_score_async(store_id)
        await bot.send_message(chat_id=chat_id, text=report, parse_mode="Markdown")
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Health score failed: %s", e, exc_info=True)
        await bot.send_message(chat_id=chat_id, text=f"⚠️ Weekly health score failed: {e}")
