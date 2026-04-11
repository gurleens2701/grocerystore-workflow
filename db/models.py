"""
SQLAlchemy 2.0 ORM models — one set of tables per store database.
Every table has store_id so rows are always traceable to a specific store.
Tables that can be edited by both bot and owner have last_updated_by + last_updated_at.
"""

from datetime import datetime, date as date_type
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Integer, Numeric,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# pending_state — replaces the in-memory _pending dict in bot.py
# ---------------------------------------------------------------------------

class PendingState(Base):
    __tablename__ = "pending_state"
    __table_args__ = (UniqueConstraint("store_id", "state_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    state_key: Mapped[str] = mapped_column(String(64), nullable=False)
    state_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# daily_sales — one row per day per store
# ---------------------------------------------------------------------------

class DailySales(Base):
    __tablename__ = "daily_sales"
    __table_args__ = (UniqueConstraint("store_id", "sale_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sale_date: Mapped[date_type] = mapped_column(Date, nullable=False)

    # Left side — from NRS API
    product_sales: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    lotto_in: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    lotto_online: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    sales_tax: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    gpi: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    grand_total: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    refunds: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)

    # Right side manual — filled by owner via Telegram
    lotto_po: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    lotto_cr: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    food_stamp: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)

    # Payments — from NRS API
    cash_drop: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    card: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    check_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    atm: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    pull_tab: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    coupon: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    loyalty: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    vendor_payout: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)

    # Calculated after owner provides manual numbers
    total_payments: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    over_short: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)

    # CC settlement reconciliation — set when the day's card total is matched
    # to a bank deposit (auto) or manually resolved by the user via Telegram.
    cc_settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cc_bank_txn_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Department breakdown JSON [{"name": "BEER", "sales": 123.45, "items": 10}, ...]
    departments: Mapped[dict[str, Any]] = mapped_column(JSONB, default=list)

    total_transactions: Mapped[int] = mapped_column(Integer, default=0)

    last_updated_by: Mapped[str] = mapped_column(String(16), default="bot")
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# invoices — vendor / COGS invoices
# ---------------------------------------------------------------------------

class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    invoice_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    invoice_num: Mapped[str | None] = mapped_column(String(64), nullable=True)
    line_items: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    matched_bank_transaction_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_updated_by: Mapped[str] = mapped_column(String(16), default="bot")
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# expenses
# ---------------------------------------------------------------------------

class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expense_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_updated_by: Mapped[str] = mapped_column(String(16), default="bot")
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# rebates
# ---------------------------------------------------------------------------

class Rebate(Base):
    __tablename__ = "rebates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rebate_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    vendor: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_updated_by: Mapped[str] = mapped_column(String(16), default="bot")
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# revenues
# ---------------------------------------------------------------------------

class Revenue(Base):
    __tablename__ = "revenues"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revenue_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_updated_by: Mapped[str] = mapped_column(String(16), default="bot")
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# bank_transactions — Plaid (optional)
# ---------------------------------------------------------------------------

class BankTransaction(Base):
    __tablename__ = "bank_transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    transaction_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    description: Mapped[str] = mapped_column(String(256), nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transaction_type: Mapped[str | None] = mapped_column(String(32), nullable=True)  # ach_debit, deposit, fee
    plaid_transaction_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    matched_invoice_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_matched: Mapped[bool] = mapped_column(Boolean, default=False)

    # Reconciliation fields
    review_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending, confirmed, skipped, auto
    reconcile_type: Mapped[str | None] = mapped_column(String(32), nullable=True)  # invoice, expense, cc_settlement, rebate, payroll, skip
    reconcile_subcategory: Mapped[str | None] = mapped_column(String(128), nullable=True)  # vendor name or expense category

    last_updated_by: Mapped[str] = mapped_column(String(16), default="bot")
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# transaction_rules — learned patterns for auto-categorization
# ---------------------------------------------------------------------------

class TransactionRule(Base):
    __tablename__ = "transaction_rules"
    __table_args__ = (UniqueConstraint("store_id", "pattern"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pattern: Mapped[str] = mapped_column(String(256), nullable=False)  # lowercase substring to match in description
    reconcile_type: Mapped[str] = mapped_column(String(32), nullable=False)  # invoice, expense, cc_settlement, rebate, payroll, skip
    reconcile_subcategory: Mapped[str | None] = mapped_column(String(128), nullable=True)  # vendor or category
    confirmed_count: Mapped[int] = mapped_column(Integer, default=1)  # times user confirmed — higher = more confidence

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------------------
# vendor_prices — invoice history per vendor, builds price comparison database
# ---------------------------------------------------------------------------

class VendorPrice(Base):
    __tablename__ = "vendor_prices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)  # GROCERY, TOBACCO, BEER, etc.
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    invoice_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    invoice_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # FK to invoices.id
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# store_health_scores — weekly health score per store
# ---------------------------------------------------------------------------

class StoreHealthScore(Base):
    __tablename__ = "store_health_scores"
    __table_args__ = (UniqueConstraint("store_id", "week_start"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    week_start: Mapped[date_type] = mapped_column(Date, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)          # 0–100
    over_short_avg: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    expense_ratio: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=0)  # expenses/sales
    invoice_match_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=0)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# invoice_items — individual line items extracted from vendor invoices
# ---------------------------------------------------------------------------

class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    invoice_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # FK to invoices.id
    vendor: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    item_name: Mapped[str] = mapped_column(String(256), nullable=False)       # normalized name
    item_name_raw: Mapped[str] = mapped_column(String(256), nullable=False)   # as extracted from invoice
    upc: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)  # price per single unit after discount
    case_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)  # price per case if available
    case_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)      # units per case
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)   # TOBACCO, BEVERAGE, GROCERY, etc.
    invoice_date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    canonical_name: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)  # e.g. MARLBORO-RED-SHORT
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)  # 0–100
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# message_log — unified log of all Telegram + web chat messages
# ---------------------------------------------------------------------------

class MessageLog(Base):
    __tablename__ = "message_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)   # "telegram" | "web"
    role: Mapped[str] = mapped_column(String(16), nullable=False)     # "user" | "bot"
    sender_name: Mapped[str] = mapped_column(String(64), nullable=False)  # "Owner", employee name, "Bot"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


# ---------------------------------------------------------------------------
# conversation_history — for Telegram AI assistant
# ---------------------------------------------------------------------------

class ConversationHistory(Base):
    __tablename__ = "conversation_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "user" or "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
