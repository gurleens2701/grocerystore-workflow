"""
tools/health_score.py

Store health score — used by the dashboard /api/health endpoint.

Returns structured data only. No Telegram delivery — health is dashboard-only
on purpose. If you ever want a weekly Telegram summary again, build on top of
build_health_score(), don't add the delivery here.

Period keys: this_week | last_week | this_month | last_month
"""

from datetime import date, timedelta

from sqlalchemy import and_, func, select

from db.database import get_async_session
from db.models import DailySales, Expense, Invoice, Rebate


def _period_range(period: str) -> tuple[date, date, str]:
    """Return (start, end, label) for the given period key."""
    today = date.today()
    wd = today.weekday()  # 0=Monday

    if period == "last_week":
        start = today - timedelta(days=wd + 7)
        end = start + timedelta(days=6)
    elif period == "this_month":
        start = today.replace(day=1)
        end = today
    elif period == "last_month":
        first_this = today.replace(day=1)
        end = first_this - timedelta(days=1)
        start = end.replace(day=1)
    else:  # this_week (default)
        start = today - timedelta(days=wd)
        end = today

    label = f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
    return start, end, label


def _days_in_period(period: str, start: date, end: date) -> int:
    if period in ("this_week", "last_week"):
        return 7
    return (end - start).days + 1


def _score_days_logged(days_logged: int, days_in_period: int) -> int:
    if days_in_period <= 0:
        return 0
    return round((days_logged / days_in_period) * 40)


def _score_over_short(avg_abs: float) -> int:
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
    if ratio < 0.20:
        return 20
    elif ratio < 0.30:
        return 15
    elif ratio < 0.40:
        return 10
    return 0


def _score_label(score: int) -> str:
    if score >= 85:
        return "Excellent"
    elif score >= 70:
        return "Good"
    elif score >= 50:
        return "Needs Attention"
    return "Poor"


def _label_color(score: int) -> str:
    if score >= 85:
        return "green"
    elif score >= 70:
        return "yellow"
    elif score >= 50:
        return "orange"
    return "red"


async def build_health_score(store_id: str, period: str = "this_week") -> dict:
    """Return store health score as structured data for the dashboard."""
    start, end, period_label = _period_range(period)
    days_in_period = _days_in_period(period, start, end)

    async with get_async_session() as session:
        sales_rows = (await session.execute(
            select(DailySales).where(
                and_(DailySales.store_id == store_id,
                     DailySales.sale_date >= start,
                     DailySales.sale_date <= end)
            )
        )).scalars().all()

        days_logged = len(sales_rows)
        total_sales = sum(float(r.grand_total or 0) for r in sales_rows)

        # Over/short — only count days the owner filled in the right side
        over_shorts = []
        for r in sales_rows:
            if r.lotto_po is not None:
                total_payments = sum(float(getattr(r, col) or 0) for col in [
                    "cash_drop", "card", "check_amount", "lotto_po", "lotto_cr",
                    "atm", "pull_tab", "coupon", "food_stamp", "loyalty", "vendor_payout"
                ])
                over_shorts.append(total_payments - float(r.grand_total or 0))

        over_short_avg = (
            sum(abs(x) for x in over_shorts) / len(over_shorts)
        ) if over_shorts else None

        # Department breakdown across the period
        dept_totals: dict[str, float] = {}
        for r in sales_rows:
            for d in (r.departments or []):
                name = d.get("name", "")
                val = float(d.get("sales", 0) or 0)
                if name and val > 0:
                    dept_totals[name] = dept_totals.get(name, 0.0) + val
        top_departments = sorted(
            [{"name": k, "amount": v} for k, v in dept_totals.items()],
            key=lambda x: x["amount"], reverse=True
        )[:5]

        inv_by_vendor = (await session.execute(
            select(Invoice.vendor, func.sum(Invoice.amount)).where(
                and_(Invoice.store_id == store_id,
                     Invoice.invoice_date >= start,
                     Invoice.invoice_date <= end)
            ).group_by(Invoice.vendor)
        )).fetchall()

        inventory_ordered = sum(float(amt or 0) for _, amt in inv_by_vendor)
        top_vendors = sorted(
            [{"vendor": v, "amount": float(a or 0)} for v, a in inv_by_vendor],
            key=lambda x: x["amount"], reverse=True
        )[:3]

        reb_by_vendor = (await session.execute(
            select(Rebate.vendor, func.sum(Rebate.amount)).where(
                and_(Rebate.store_id == store_id,
                     Rebate.rebate_date >= start,
                     Rebate.rebate_date <= end)
            ).group_by(Rebate.vendor)
        )).fetchall()

        rebates_total = sum(float(amt or 0) for _, amt in reb_by_vendor)
        top_rebates = sorted(
            [{"vendor": v, "amount": float(a or 0)} for v, a in reb_by_vendor],
            key=lambda x: x["amount"], reverse=True
        )[:3]

        exp_rows = (await session.execute(
            select(Expense.category, Expense.notes, func.sum(Expense.amount)).where(
                and_(Expense.store_id == store_id,
                     Expense.expense_date >= start,
                     Expense.expense_date <= end)
            ).group_by(Expense.category, Expense.notes)
        )).fetchall()

    # Split payroll vs other expenses (payroll uses notes for the employee name)
    payroll_by_name: dict[str, float] = {}
    other_by_cat: dict[str, float] = {}
    for cat, notes, amt in exp_rows:
        amt = float(amt or 0)
        if any(k in (cat or "").lower() for k in ("payroll", "salary", "wage")):
            name = (notes or cat or "Payroll").strip()
            payroll_by_name[name] = payroll_by_name.get(name, 0.0) + amt
        else:
            key = (cat or "Other").strip()
            other_by_cat[key] = other_by_cat.get(key, 0.0) + amt

    payroll_total = sum(payroll_by_name.values())
    other_expenses_total = sum(other_by_cat.values())
    total_expenses = payroll_total + other_expenses_total

    top_expenses = sorted(
        [{"category": k, "amount": v} for k, v in other_by_cat.items()],
        key=lambda x: x["amount"], reverse=True
    )[:3]
    top_payroll = sorted(
        [{"name": k, "amount": v} for k, v in payroll_by_name.items()],
        key=lambda x: x["amount"], reverse=True
    )[:3]

    expense_ratio = (total_expenses / total_sales) if total_sales > 0 else 0.0
    score = (
        _score_days_logged(days_logged, days_in_period)
        + _score_over_short(over_short_avg if over_short_avg is not None else 0.0)
        + _score_expense_ratio(expense_ratio)
    )
    inventory_pct = round((inventory_ordered / total_sales * 100), 1) if total_sales > 0 else None

    return {
        "period": period,
        "period_label": period_label,
        "days_logged": days_logged,
        "days_in_period": days_in_period,
        "score": score,
        "label": _score_label(score),
        "label_color": _label_color(score),
        "total_sales": total_sales,
        "over_short_avg": round(over_short_avg, 2) if over_short_avg is not None else None,
        "inventory_ordered": inventory_ordered,
        "inventory_pct_of_sales": inventory_pct,
        "payroll_total": payroll_total,
        "other_expenses_total": other_expenses_total,
        "rebates_total": rebates_total,
        "top_rebates": top_rebates,
        "top_departments": top_departments,
        "top_vendors": top_vendors,
        "top_expenses": top_expenses,
        "top_payroll": top_payroll,
        "days_missing": max(0, days_in_period - days_logged),
    }
