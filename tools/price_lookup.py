"""
tools/price_lookup.py

Price lookup and order compilation for gas station convenience store.

Public sync API:
    lookup_item_price(item_query: str) -> str
    compile_order(item_list: list[str]) -> str
    parse_order_list(text: str) -> list[str]
"""

import asyncio
import re
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, func, select

from config.settings import settings
from db.database import get_async_session
from db.models import InvoiceItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_date(d: date) -> str:
    """Format a date as 'Mon DD' (e.g. 'Mar 14')."""
    return d.strftime("%b %-d")


def _fmt_price(price: Decimal) -> str:
    return f"${float(price):.2f}"


# ---------------------------------------------------------------------------
# Core async implementations
# ---------------------------------------------------------------------------

async def _lookup_item_price_async(item_query: str) -> str:
    """
    Fuzzy search invoice_items for item_query across all vendors.
    Returns a formatted comparison string.
    """
    words = item_query.strip().split()
    if not words:
        return "Please provide an item name to search for."

    async with get_async_session() as session:
        # Search both canonical_name and item_name — all words must appear in either
        ilike_conditions = [
            (InvoiceItem.canonical_name.ilike(f"%{w}%") | InvoiceItem.item_name.ilike(f"%{w}%"))
            for w in words
        ]

        # Subquery: latest invoice_date per vendor for this item
        latest_sq = (
            select(
                InvoiceItem.vendor,
                func.max(InvoiceItem.invoice_date).label("max_date"),
            )
            .where(
                and_(
                    InvoiceItem.store_id == settings.store_id,
                    *ilike_conditions,
                )
            )
            .group_by(InvoiceItem.vendor)
            .subquery()
        )

        # Join back to get the actual row on that date (pick lowest unit_price
        # if multiple rows share the same max date for a vendor)
        rows_q = (
            select(
                InvoiceItem.vendor,
                InvoiceItem.item_name,
                func.min(InvoiceItem.unit_price).label("unit_price"),
                latest_sq.c.max_date.label("invoice_date"),
            )
            .join(
                latest_sq,
                and_(
                    InvoiceItem.vendor == latest_sq.c.vendor,
                    InvoiceItem.invoice_date == latest_sq.c.max_date,
                ),
            )
            .where(
                and_(
                    InvoiceItem.store_id == settings.store_id,
                    *ilike_conditions,
                )
            )
            .group_by(
                InvoiceItem.vendor,
                InvoiceItem.item_name,
                latest_sq.c.max_date,
            )
            .order_by(func.min(InvoiceItem.unit_price))
        )

        # If single short query word, also try UPC exact match as fallback
        upc_rows = []
        if len(words) == 1 and len(words[0]) <= 4:
            upc_q = (
                select(
                    InvoiceItem.vendor,
                    InvoiceItem.item_name,
                    InvoiceItem.unit_price,
                    InvoiceItem.invoice_date,
                )
                .where(
                    and_(
                        InvoiceItem.store_id == settings.store_id,
                        InvoiceItem.upc == words[0],
                    )
                )
                .order_by(InvoiceItem.invoice_date.desc())
            )
            upc_result = await session.execute(upc_q)
            upc_rows = upc_result.fetchall()

        result = await session.execute(rows_q)
        rows = result.fetchall()

        # Merge UPC rows if name search returned nothing
        if not rows and upc_rows:
            rows = upc_rows

        if not rows:
            return (
                f"No results found for \"{item_query}\".\n\n"
                "Log an invoice from your vendor to add this item."
            )

        # Determine canonical display name from the most-recent row
        # (rows sorted by price; grab name from cheapest which is first)
        display_name = rows[0].item_name.title()

        # Find the overall latest date across vendors
        last_updated = max(r.invoice_date for r in rows)

        # Build lines
        cheapest_price = rows[0].unit_price
        lines = []
        for r in rows:
            tag = "  \U0001f3c6 cheapest" if r.unit_price == cheapest_price else ""
            vendor_col = r.vendor.upper().ljust(14)
            price_col = f"{_fmt_price(r.unit_price)}/unit".ljust(14)
            date_col = f"({_fmt_date(r.invoice_date)})"
            lines.append(f"{vendor_col} {price_col} {date_col}{tag}")

        result_str = (
            f"\U0001f50d {display_name}\n\n"
            + "\n".join(lines)
            + f"\n\nLast updated: {_fmt_date(last_updated)}, {last_updated.year}"
        )
        return result_str


async def _compile_order_async(item_list: list[str]) -> str:
    """
    For each item in item_list, find the cheapest vendor (latest price,
    then min unit_price). Group by cheapest vendor, flag missing items.
    """
    if not item_list:
        return "No items provided."

    async with get_async_session() as session:
        # For each item we need: cheapest vendor + price
        # We'll do one query per item (list is typically short, ~5–30 items)

        vendor_items: dict[str, list[dict]] = {}  # vendor -> [{name, price, query}]
        not_found: list[str] = []

        for raw_item in item_list:
            words = raw_item.strip().split()
            if not words:
                continue

            ilike_conditions = [
                InvoiceItem.item_name.ilike(f"%{w}%") for w in words
            ]

            # Latest date per vendor
            latest_sq = (
                select(
                    InvoiceItem.vendor,
                    func.max(InvoiceItem.invoice_date).label("max_date"),
                )
                .where(
                    and_(
                        InvoiceItem.store_id == settings.store_id,
                        *ilike_conditions,
                    )
                )
                .group_by(InvoiceItem.vendor)
                .subquery()
            )

            # Min unit_price on that latest date, per vendor
            rows_q = (
                select(
                    InvoiceItem.vendor,
                    InvoiceItem.item_name,
                    func.min(InvoiceItem.unit_price).label("unit_price"),
                )
                .join(
                    latest_sq,
                    and_(
                        InvoiceItem.vendor == latest_sq.c.vendor,
                        InvoiceItem.invoice_date == latest_sq.c.max_date,
                    ),
                )
                .where(
                    and_(
                        InvoiceItem.store_id == settings.store_id,
                        *ilike_conditions,
                    )
                )
                .group_by(InvoiceItem.vendor, InvoiceItem.item_name)
                .order_by(func.min(InvoiceItem.unit_price))
            )

            result = await session.execute(rows_q)
            rows = result.fetchall()

            if not rows:
                not_found.append(raw_item)
                continue

            # Cheapest vendor = first row (ordered by unit_price asc)
            cheapest = rows[0]
            vendor = cheapest.vendor.upper()
            display_name = cheapest.item_name.title()
            price = cheapest.unit_price

            if vendor not in vendor_items:
                vendor_items[vendor] = []
            vendor_items[vendor].append({
                "name": display_name,
                "price": price,
                "query": raw_item,
            })

    if not vendor_items and not_found:
        missing_lines = "\n".join(f"  \u2022 {i}" for i in not_found)
        return (
            "\u26a0\ufe0f No items found in system.\n\n"
            "Log invoices from your vendors to add these items:\n"
            + missing_lines
        )

    sections = ["\U0001f4e6 Order Summary\n"]

    for vendor, items in sorted(vendor_items.items()):
        est_total = sum(i["price"] for i in items)
        sections.append(f"{vendor}  (est. {_fmt_price(est_total)})")
        for item in items:
            sections.append(f"  \u2022 {item['name']}  {_fmt_price(item['price'])} \u00d7 ?")
        sections.append("")

    if not_found:
        sections.append(
            "\u26a0\ufe0f Not in system yet (log invoices to add):"
        )
        for missing in not_found:
            sections.append(f"  \u2022 {missing}")
        sections.append("")

    sections.append(
        "Quantities not set \u2014 reply with quantities or place orders manually."
    )

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# parse_order_list — pure Python, no DB needed
# ---------------------------------------------------------------------------

def parse_order_list(text: str) -> list[str]:
    """
    Parse a plain text message listing items to order.
    Handles newline-separated or comma-separated lists.
    Strips leading quantities (e.g. '5 marlboro red' -> 'marlboro red')
    and trailing quantities (e.g. 'marlboro red x5' -> 'marlboro red').
    Returns a clean list of item name strings (lower-cased, stripped).
    """
    if not text or not text.strip():
        return []

    # Split on newlines and commas
    raw_parts = re.split(r"[\n,]+", text)

    items = []
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue

        # Strip trailing quantity: "item x5", "item x 5", "item (5)", "item - 5"
        part = re.sub(r"\s*[x\u00d7]\s*\d+\s*$", "", part, flags=re.IGNORECASE)
        part = re.sub(r"\s*\(\d+\)\s*$", "", part)
        part = re.sub(r"\s*-\s*\d+\s*$", "", part)
        # Strip leading quantity: "5 item", "5x item"
        part = re.sub(r"^\d+\s*[x\u00d7]?\s+", "", part, flags=re.IGNORECASE)

        part = part.strip()
        if part:
            items.append(part)

    return items


# ---------------------------------------------------------------------------
# Public sync wrappers
# ---------------------------------------------------------------------------

def lookup_item_price(item_query: str) -> str:
    """
    Fuzzy search invoice_items for the queried product across all vendors.
    Returns a formatted string showing vendors, latest prices, and cheapest marker.
    """
    return asyncio.run(_lookup_item_price_async(item_query))


def compile_order(item_list: list[str]) -> str:
    """
    Takes a list of item name strings, finds cheapest vendor per item,
    groups by vendor, and returns a formatted Telegram-ready order summary.
    """
    return asyncio.run(_compile_order_async(item_list))
