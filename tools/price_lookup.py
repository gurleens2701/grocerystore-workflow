"""
tools/price_lookup.py

Price lookup and order compilation for gas station convenience store.

Public sync API:
    lookup_item_price(item_query: str) -> str
    compile_order(item_list: list[dict]) -> str
    parse_order_list(text: str) -> list[dict]  # returns [{item, qty}]
"""

import asyncio
import re
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, func, select

from config.settings import settings
from db.database import get_async_session, get_session_for_store
from db.models import InvoiceItem
from config.store_context import get_active_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_date(d: date) -> str:
    return d.strftime("%b %-d")


def _fmt_price(price) -> str:
    return f"${float(price):.2f}"


def _ilike_conditions(words: list[str]):
    """Search both canonical_name and item_name — all words must match."""
    return [
        (InvoiceItem.canonical_name.ilike(f"%{w}%") | InvoiceItem.item_name.ilike(f"%{w}%"))
        for w in words
    ]


def _latest_price_per_vendor(words: list[str], store_id: str):
    """Subquery + join to get min unit_price on the latest invoice date per vendor."""
    ilike = _ilike_conditions(words)

    latest_sq = (
        select(
            InvoiceItem.vendor,
            func.max(InvoiceItem.invoice_date).label("max_date"),
        )
        .where(and_(InvoiceItem.store_id == store_id, *ilike))
        .group_by(InvoiceItem.vendor)
        .subquery()
    )

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
        .where(and_(InvoiceItem.store_id == store_id, *ilike))
        .group_by(InvoiceItem.vendor, InvoiceItem.item_name, latest_sq.c.max_date)
        .order_by(func.min(InvoiceItem.unit_price))
    )
    return rows_q


# ---------------------------------------------------------------------------
# parse_order_list — returns [{item: str, qty: int}]
# ---------------------------------------------------------------------------

def parse_order_list(text: str) -> list[dict]:
    """
    Parse a plain text order list into items with quantities.

    Handles formats:
      marlboro red short x5
      5 coke 20oz
      doritos nacho - 10
      black mild ft sweet (3)

    Returns list of {"item": str, "qty": int}
    """
    if not text or not text.strip():
        return []

    raw_parts = re.split(r"[\n,]+", text)
    result = []

    for part in raw_parts:
        part = part.strip()
        if not part:
            continue

        qty = 1

        # Trailing quantity: "item x5", "item x 5", "item (5)", "item - 5"
        m = re.search(r"\s*[x×]\s*(\d+)\s*$", part, flags=re.IGNORECASE)
        if m:
            qty = int(m.group(1))
            part = part[:m.start()].strip()
        else:
            m = re.search(r"\s*\((\d+)\)\s*$", part)
            if m:
                qty = int(m.group(1))
                part = part[:m.start()].strip()
            else:
                m = re.search(r"\s*-\s*(\d+)\s*$", part)
                if m:
                    qty = int(m.group(1))
                    part = part[:m.start()].strip()

        # Leading quantity: "5 item", "5x item"
        m = re.match(r"^(\d+)\s*[x×]?\s+", part, flags=re.IGNORECASE)
        if m:
            qty = int(m.group(1))
            part = part[m.end():].strip()

        if part:
            result.append({"item": part, "qty": qty})

    return result


# ---------------------------------------------------------------------------
# lookup_item_price
# ---------------------------------------------------------------------------

async def _lookup_item_price_async(item_query: str, store_id: str | None = None) -> str:
    sid = store_id or get_active_store()
    words = item_query.strip().split()
    if not words:
        return "Please provide an item name to search for."

    async with get_session_for_store(sid) as session:
        rows_q = _latest_price_per_vendor(words, sid)

        # UPC fallback for short single-word queries
        upc_rows = []
        if len(words) == 1 and len(words[0]) <= 13:
            upc_q = (
                select(
                    InvoiceItem.vendor,
                    InvoiceItem.item_name,
                    InvoiceItem.unit_price,
                    InvoiceItem.invoice_date,
                )
                .where(
                    and_(
                        InvoiceItem.store_id == sid,
                        InvoiceItem.upc == words[0],
                    )
                )
                .order_by(InvoiceItem.invoice_date.desc())
            )
            upc_result = await session.execute(upc_q)
            upc_rows = upc_result.fetchall()

        result = await session.execute(rows_q)
        rows = result.fetchall()

        if not rows and upc_rows:
            rows = upc_rows

        if not rows:
            return (
                f'No results found for "{item_query}".\n\n'
                "Upload a vendor invoice to add this item."
            )

        display_name = rows[0].item_name.title()
        last_updated = max(r.invoice_date for r in rows)
        cheapest_price = rows[0].unit_price

        lines = []
        for r in rows:
            tag = "  🏆 cheapest" if r.unit_price == cheapest_price else ""
            vendor_col = r.vendor.upper().ljust(14)
            price_col = f"{_fmt_price(r.unit_price)}/unit".ljust(14)
            date_col = f"({_fmt_date(r.invoice_date)})"
            lines.append(f"{vendor_col} {price_col} {date_col}{tag}")

        return (
            f"🔍 {display_name}\n\n"
            + "\n".join(lines)
            + f"\n\nLast updated: {_fmt_date(last_updated)}, {last_updated.year}"
        )


# ---------------------------------------------------------------------------
# compile_order — all vendors, totals, missing items, cheapest suggestion
# ---------------------------------------------------------------------------

async def _compile_order_async(item_list: list[dict], store_id: str | None = None) -> str:
    """
    For each item (with qty), find ALL vendors that carry it and their price.
    Build a per-vendor total. Show cheapest vendor, flag missing items per vendor.
    Suggest split order if cheapest vendor is missing items.
    """
    sid = store_id or get_active_store()
    if not item_list:
        return "No items provided."

    # all_vendors: vendor -> {total, items: [{name, qty, unit_price}], missing: [item]}
    all_vendors: dict[str, dict] = {}
    not_in_system: list[str] = []  # items not found in DB at all

    async with get_session_for_store(sid) as session:
        for entry in item_list:
            raw_item = entry["item"]
            qty = entry["qty"]
            words = raw_item.strip().split()
            if not words:
                continue

            rows_q = _latest_price_per_vendor(words, sid)
            result = await session.execute(rows_q)
            rows = result.fetchall()

            if not rows:
                not_in_system.append(f"{raw_item} ×{qty}")
                # Mark this item as missing for ALL known vendors
                for v in all_vendors:
                    all_vendors[v]["missing"].append(f"{raw_item} ×{qty}")
                continue

            vendors_with_item = {r.vendor.upper() for r in rows}

            # Add item to each vendor that carries it
            for r in rows:
                vendor = r.vendor.upper()
                if vendor not in all_vendors:
                    all_vendors[vendor] = {"total": Decimal("0"), "items": [], "missing": []}
                line_total = Decimal(str(r.unit_price)) * qty
                all_vendors[vendor]["total"] += line_total
                all_vendors[vendor]["items"].append({
                    "name": r.item_name.title(),
                    "qty": qty,
                    "unit_price": r.unit_price,
                    "line_total": line_total,
                })

            # Mark item as missing for vendors already tracked that don't carry it
            for vendor in all_vendors:
                if vendor not in vendors_with_item:
                    all_vendors[vendor]["missing"].append(f"{raw_item} ×{qty}")

    if not all_vendors and not_in_system:
        missing_lines = "\n".join(f"  • {i}" for i in not_in_system)
        return (
            "⚠️ None of these items are in the system yet.\n\n"
            "Upload vendor invoices to add them:\n" + missing_lines
        )

    # Sort vendors by total cost ascending
    sorted_vendors = sorted(all_vendors.items(), key=lambda x: x[1]["total"])
    cheapest_vendor = sorted_vendors[0][0] if sorted_vendors else None

    sections = [f"📦 Order Summary — {len(item_list)} items\n"]

    for vendor, data in sorted_vendors:
        is_cheapest = vendor == cheapest_vendor
        crown = " 🏆 cheapest" if is_cheapest else ""
        missing_count = len(data["missing"])
        coverage = f"  ⚠️ missing {missing_count} item(s)" if missing_count else "  ✅ full coverage"

        sections.append(f"{vendor}   {_fmt_price(data['total'])}{crown}{coverage}")
        for item in data["items"]:
            sections.append(
                f"  • {item['name']} ×{item['qty']}   "
                f"{_fmt_price(item['unit_price'])}/unit = {_fmt_price(item['line_total'])}"
            )
        if data["missing"]:
            for m in data["missing"]:
                sections.append(f"  ✗ {m}")
        sections.append("")

    # Items not in system at all
    if not_in_system:
        sections.append("⚠️ Not in system (upload invoices to add):")
        for m in not_in_system:
            sections.append(f"  • {m}")
        sections.append("")

    # Smart suggestion
    cheapest_data = all_vendors.get(cheapest_vendor, {})
    cheapest_missing = len(cheapest_data.get("missing", []))

    if cheapest_missing == 0:
        sections.append(f"💡 Order everything from {cheapest_vendor} — lowest cost, full coverage.")
    else:
        # Find cheapest vendor with full coverage
        full_coverage = [(v, d) for v, d in sorted_vendors if len(d["missing"]) == 0]
        if full_coverage:
            best_full = full_coverage[0]
            savings = best_full[1]["total"] - cheapest_data["total"]
            sections.append(
                f"💡 {cheapest_vendor} is cheapest but missing {cheapest_missing} item(s).\n"
                f"   {best_full[0]} has full coverage for {_fmt_price(best_full[1]['total'])} "
                f"(+{_fmt_price(savings)} more).\n"
                f"   Consider splitting or ordering from {best_full[0]} for simplicity."
            )
        else:
            sections.append(
                f"💡 No single vendor carries everything. "
                f"{cheapest_vendor} is cheapest — split remaining items from another vendor."
            )

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Public sync wrappers
# ---------------------------------------------------------------------------

def lookup_item_price(item_query: str) -> str:
    return asyncio.run(_lookup_item_price_async(item_query))


def compile_order(item_list: list[dict]) -> str:
    return asyncio.run(_compile_order_async(item_list))
