"""
Common async database write operations.

These are called from bot.py handlers after intent routing.
All operations are store-scoped via store_id.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from db.database import get_async_session
from db.models import BankTransaction, DailySales, Expense, Invoice, InvoiceItem, MessageLog, Rebate, Revenue, VendorPrice


async def log_message(
    store_id: str,
    source: str,   # "telegram" | "web"
    role: str,     # "user" | "bot"
    sender_name: str,
    content: str,
) -> None:
    """Append one message to the unified message log. Fire-and-forget safe."""
    try:
        async with get_async_session() as session:
            session.add(MessageLog(
                store_id=store_id,
                source=source,
                role=role,
                sender_name=sender_name,
                content=content,
            ))
    except Exception:
        pass  # logging must never crash the caller


# ---------------------------------------------------------------------------
# Daily sales
# ---------------------------------------------------------------------------

async def save_daily_sales(store_id: str, sales: dict, right: dict) -> None:
    """Upsert a completed daily sales record (left + right side combined).

    Known column fields go in their typed columns; per-store manual fields
    that don't have a column (money_order, bill_pay, solds, etc.) flow into
    extra_fields JSONB. Keys come from rule.field_name.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    sale_date = date.fromisoformat(sales["date"])

    # Modisoft → DB column aliases (some POS use different names)
    aliases = {"lotto_payout": "lotto_po", "vendor": "vendor_payout", "check": "check_amount"}
    column_keys = {
        "product_sales", "lotto_in", "lotto_online", "sales_tax", "gpi",
        "grand_total", "refunds", "lotto_po", "lotto_cr", "food_stamp",
        "cash_drop", "card", "check_amount", "atm", "pull_tab", "coupon",
        "loyalty", "vendor_payout",
    }

    # Merge sales (api) and right (manual) — manual wins on conflict
    merged = dict(sales)
    merged.update(right)

    extra_fields: dict = {}
    column_values: dict = {}
    for k, v in merged.items():
        if k in ("date", "day_of_week", "departments", "total_transactions"):
            continue
        canonical = aliases.get(k, k)
        if canonical in column_keys:
            try:
                column_values[canonical] = Decimal(str(v or 0))
            except Exception:
                pass
        elif isinstance(v, (int, float)):
            extra_fields[k] = float(v)

    # cash_drop has a legacy alias "cash_drops" — pick it up if present
    if "cash_drop" not in column_values and "cash_drops" in sales:
        column_values["cash_drop"] = Decimal(str(sales.get("cash_drops", 0)))

    values = dict(
        store_id=store_id,
        sale_date=sale_date,
        departments=sales.get("departments", []),
        extra_fields=extra_fields,
        total_transactions=int(sales.get("total_transactions", 0)),
        last_updated_by="bot",
        **{k: column_values.get(k, Decimal("0")) for k in column_keys},
    )

    async with get_async_session() as session:
        stmt = pg_insert(DailySales).values(**values).on_conflict_do_update(
            index_elements=["store_id", "sale_date"],
            set_={k: v for k, v in values.items() if k not in ("store_id", "sale_date")},
        )
        await session.execute(stmt)


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------

async def save_expense(
    store_id: str,
    category: str,
    amount: float,
    expense_date: date,
    notes: str = "",
    updated_by: str = "owner",
) -> int:
    """Insert an expense row. Returns the new row id."""
    async with get_async_session() as session:
        row = Expense(
            store_id=store_id,
            expense_date=expense_date,
            category=category.upper(),
            amount=Decimal(str(amount)),
            notes=notes or None,
            last_updated_by=updated_by,
        )
        session.add(row)
        await session.flush()
        return row.id


# ---------------------------------------------------------------------------
# Rebates
# ---------------------------------------------------------------------------

async def save_rebate(
    store_id: str,
    vendor: str,
    amount: float,
    rebate_date: date,
    notes: str = "",
    updated_by: str = "owner",
) -> int:
    """Insert a rebate row. Returns the new row id."""
    async with get_async_session() as session:
        row = Rebate(
            store_id=store_id,
            rebate_date=rebate_date,
            vendor=vendor.upper(),
            amount=Decimal(str(amount)),
            notes=notes or None,
            last_updated_by=updated_by,
        )
        session.add(row)
        await session.flush()
        return row.id


# ---------------------------------------------------------------------------
# Revenues
# ---------------------------------------------------------------------------

async def save_revenue(
    store_id: str,
    category: str,
    amount: float,
    revenue_date: date,
    notes: str = "",
    updated_by: str = "owner",
) -> int:
    """Insert a revenue row. Returns the new row id."""
    async with get_async_session() as session:
        row = Revenue(
            store_id=store_id,
            revenue_date=revenue_date,
            category=category.upper(),
            amount=Decimal(str(amount)),
            notes=notes or None,
            last_updated_by=updated_by,
        )
        session.add(row)
        await session.flush()
        return row.id


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

async def save_invoice(
    store_id: str,
    vendor: str,
    amount: float,
    invoice_date: date,
    invoice_num: str = "",
    updated_by: str = "owner",
) -> int:
    """Insert an invoice row. Returns the new row id."""
    async with get_async_session() as session:
        row = Invoice(
            store_id=store_id,
            vendor=vendor.upper(),
            amount=Decimal(str(amount)),
            invoice_date=invoice_date,
            invoice_num=invoice_num or None,
            last_updated_by=updated_by,
        )
        session.add(row)
        await session.flush()
        return row.id


# ---------------------------------------------------------------------------
# Vendor prices
# ---------------------------------------------------------------------------

_VENDOR_CATEGORIES: dict[str, str] = {
    "MCLANE": "GROCERY",
    "HEIDELBURG": "GROCERY",
    "COREMARK": "GROCERY",
    "CORE-MARK": "GROCERY",
    "EBY-BROWN": "GROCERY",
    "PEPSI": "BEVERAGE",
    "COKE": "BEVERAGE",
    "COCA-COLA": "BEVERAGE",
    "RED BULL": "BEVERAGE",
    "MONSTER": "BEVERAGE",
    "ALTRIA": "TOBACCO",
    "PMHELIX": "TOBACCO",
    "USSMOKE": "TOBACCO",
    "US SMOKE": "TOBACCO",
    "GLAZER": "ALCOHOL",
    "REPUBLIC": "ALCOHOL",
    "GREAT LAKES": "ALCOHOL",
}


def _infer_category(vendor: str) -> str | None:
    v = vendor.upper()
    for key, cat in _VENDOR_CATEGORIES.items():
        if key in v:
            return cat
    return None


async def save_vendor_price(
    store_id: str,
    vendor: str,
    amount: float,
    invoice_date: date,
    invoice_id: int | None = None,
) -> None:
    """Record a vendor invoice in the price history table."""
    async with get_async_session() as session:
        row = VendorPrice(
            store_id=store_id,
            vendor=vendor.upper(),
            category=_infer_category(vendor),
            amount=Decimal(str(amount)),
            invoice_date=invoice_date,
            invoice_id=invoice_id,
        )
        session.add(row)


# ---------------------------------------------------------------------------
# Invoice items (line-item price database)
# ---------------------------------------------------------------------------

async def save_invoice_items(
    store_id: str,
    vendor: str,
    items: list[dict],
    invoice_date: date,
    invoice_id: int | None = None,
) -> int:
    """
    Bulk-insert invoice line items extracted from an invoice.

    Each item dict should have:
      item_name: str          — normalized product name
      item_name_raw: str      — as extracted
      unit_price: float       — price per single unit after discount
      upc: str | None
      case_price: float | None
      case_qty: int | None
      category: str | None

    Returns count of items saved.
    """
    if not items:
        return 0

    async with get_async_session() as session:
        rows = [
            InvoiceItem(
                store_id=store_id,
                invoice_id=invoice_id,
                vendor=vendor.upper(),
                item_name=item.get("canonical_name", item["item_name"]).upper().strip(),
                item_name_raw=item.get("item_name_raw", item["item_name"]),
                upc=item.get("upc"),
                unit_price=Decimal(str(item["unit_price"])),
                case_price=Decimal(str(item["case_price"])) if item.get("case_price") else None,
                case_qty=item.get("case_qty"),
                category=item.get("category"),
                invoice_date=invoice_date,
                canonical_name=item.get("canonical_name", "").upper().strip() or None,
                confidence=Decimal(str(round(item["confidence"], 3))) if item.get("confidence") is not None else None,
            )
            for item in items
            if item.get("unit_price") and float(item["unit_price"]) > 0
        ]
        session.add_all(rows)
        return len(rows)
