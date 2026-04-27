"""
Vendor price intelligence — Step 12.

Answers questions like:
  "where should I order chips from?"
  "who is cheapest for grocery?"
  "compare mclane vs coremark"

Called from bot.py when intent = "query" and topic is vendor/order related,
or directly via /vendors command.
"""

import asyncio
import json
from datetime import date, timedelta

from sqlalchemy import and_, func, select

from config.settings import settings
from db.database import get_async_session
from db.models import VendorPrice
from config.store_context import get_active_store


async def _get_vendor_summary(store_id: str, category: str | None, days: int) -> list[dict]:
    """Return average invoice amount per vendor, sorted cheapest first."""
    since = date.today() - timedelta(days=days)
    async with get_async_session() as session:
        stmt = (
            select(
                VendorPrice.vendor,
                VendorPrice.category,
                func.count(VendorPrice.id).label("invoice_count"),
                func.avg(VendorPrice.amount).label("avg_amount"),
                func.min(VendorPrice.amount).label("min_amount"),
                func.max(VendorPrice.amount).label("max_amount"),
                func.max(VendorPrice.invoice_date).label("last_order"),
            )
            .where(
                and_(
                    VendorPrice.store_id == store_id,
                    VendorPrice.invoice_date >= since,
                )
            )
        )
        if category:
            stmt = stmt.where(VendorPrice.category == category.upper())

        stmt = stmt.group_by(VendorPrice.vendor, VendorPrice.category)
        stmt = stmt.order_by(func.avg(VendorPrice.amount))

        result = await session.execute(stmt)
        rows = result.all()

    return [
        {
            "vendor": r.vendor,
            "category": r.category or "UNCATEGORIZED",
            "invoice_count": r.invoice_count,
            "avg_amount": round(float(r.avg_amount), 2),
            "min_amount": round(float(r.min_amount), 2),
            "max_amount": round(float(r.max_amount), 2),
            "last_order": str(r.last_order),
        }
        for r in rows
    ]


async def _get_recent_invoices(store_id: str, vendor: str, days: int) -> list[dict]:
    """Return recent invoices for a specific vendor."""
    since = date.today() - timedelta(days=days)
    async with get_async_session() as session:
        result = await session.execute(
            select(VendorPrice)
            .where(
                and_(
                    VendorPrice.store_id == store_id,
                    VendorPrice.vendor.ilike(f"%{vendor}%"),
                    VendorPrice.invoice_date >= since,
                )
            )
            .order_by(VendorPrice.invoice_date.desc())
        )
        rows = result.scalars().all()

    return [
        {"date": str(r.invoice_date), "amount": float(r.amount), "category": r.category}
        for r in rows
    ]


def get_vendor_comparison(category: str | None = None, days: int = 90) -> str:
    """
    Return a formatted vendor price comparison.
    Blocking — call via run_in_executor from async context.
    """
    rows = asyncio.run(_get_vendor_summary(get_active_store(), category, days))

    if not rows:
        period = f"last {days} days"
        if category:
            return f"No {category} invoices found in the {period}. Log some invoices first."
        return f"No invoice history found in the {period}. Log invoices to build price data."

    # Group by category for display
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)

    lines = [f"📦 *Vendor Price History* (last {days} days)\n"]
    for cat, vendors in sorted(by_cat.items()):
        lines.append(f"*{cat}*")
        for i, v in enumerate(vendors):
            tag = " 🏆 cheapest" if i == 0 and len(vendors) > 1 else ""
            lines.append(
                f"  {v['vendor']}: avg ${v['avg_amount']:,.2f} "
                f"({v['invoice_count']} orders){tag}"
            )
            if v["invoice_count"] > 1:
                lines.append(f"    Range: ${v['min_amount']:,.2f} – ${v['max_amount']:,.2f}")
        lines.append("")

    return "\n".join(lines).strip()
