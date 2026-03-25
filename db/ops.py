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
    """Upsert a completed daily sales record (left + right side combined)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    sale_date = date.fromisoformat(sales["date"])

    values = dict(
        store_id=store_id,
        sale_date=sale_date,
        product_sales=Decimal(str(sales.get("product_sales", 0))),
        lotto_in=Decimal(str(sales.get("lotto_in", 0))),
        lotto_online=Decimal(str(sales.get("lotto_online", 0))),
        sales_tax=Decimal(str(sales.get("sales_tax", 0))),
        gpi=Decimal(str(sales.get("gpi", 0))),
        grand_total=Decimal(str(sales.get("grand_total", 0))),
        refunds=Decimal(str(sales.get("refunds", 0))),
        lotto_po=Decimal(str(right.get("lotto_po", 0))),
        lotto_cr=Decimal(str(right.get("lotto_cr", 0))),
        food_stamp=Decimal(str(right.get("food_stamp", 0))),
        cash_drop=Decimal(str(sales.get("cash_drops", 0))),
        card=Decimal(str(sales.get("card", 0))),
        check_amount=Decimal(str(sales.get("check", 0))),
        atm=Decimal(str(sales.get("atm", 0))),
        pull_tab=Decimal(str(sales.get("pull_tab", 0))),
        coupon=Decimal(str(sales.get("coupon", 0))),
        loyalty=Decimal(str(sales.get("loyalty", 0))),
        vendor_payout=Decimal(str(sales.get("vendor", 0))),
        departments=sales.get("departments", []),
        total_transactions=int(sales.get("total_transactions", 0)),
        last_updated_by="bot",
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
