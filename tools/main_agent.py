"""
Main agent — handles ALL user messages with a single Claude call + tool loop.

Replaces the intent router for most flows. Both read (query DB) and write
(log expenses, invoices, rebates, revenue to DB + Sheets) tools are available.
Claude decides which tools to call based on what the user says.

Entry point: run_agent(question, store_id) — blocking, safe to call via run_in_executor.
"""

import asyncio
import json
import logging
from datetime import date, timedelta
from decimal import Decimal

log = logging.getLogger(__name__)

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from sqlalchemy import and_, select, func

from config.settings import settings
from config.store_context import get_active_store, set_active_store
from db.database import get_sync_session
from db.models import (
    DailySales, Expense, Invoice, InvoiceItem, Rebate, Revenue, Store,
)
import tools.sheets_tools as sheets_tools
from tools.sheets_tools import resolve_vendor, VENDOR_ALIAS_MAP


# ---------------------------------------------------------------------------
# Date parsing helper
# ---------------------------------------------------------------------------

_MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def _parse_date(date_str: str) -> date:
    """
    Parse a flexible date string into a date object.

    Supported formats:
      ""                       → today
      "march"                  → first of that month (current year)
      "march 22"               → March 22 of current year
      "march 22 2026"          → March 22, 2026
      "3/22"                   → March 22 of current year
      "3/22/26" or "3/22/2026" → March 22, 2026
      "2026-03-22"             → ISO format
    """
    today = date.today()

    if not date_str or not date_str.strip():
        return today

    s = date_str.strip().lower()

    # Handle concatenated month+day like "april2", "march15", "jan3"
    import re as _re
    _concat = _re.match(r'^([a-z]+)(\d{1,2})$', s)
    if _concat and _concat.group(1) in _MONTH_NAMES:
        s = f"{_concat.group(1)} {_concat.group(2)}"

    # ISO format YYYY-MM-DD
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            return date.fromisoformat(s)
        except ValueError:
            pass

    # Slash-separated: M/D, M/D/YY, M/D/YYYY
    if "/" in s:
        parts = s.split("/")
        try:
            month = int(parts[0])
            day = int(parts[1])
            if len(parts) == 3:
                year = int(parts[2])
                if year < 100:
                    year += 2000
            else:
                year = today.year
            return date(year, month, day)
        except (ValueError, IndexError):
            pass

    # Month name forms: "march", "march 22", "march 22 2026"
    tokens = s.split()
    if tokens and tokens[0] in _MONTH_NAMES:
        month = _MONTH_NAMES[tokens[0]]
        day = 1
        year = today.year
        if len(tokens) >= 2:
            try:
                day = int(tokens[1])
            except ValueError:
                pass
        if len(tokens) >= 3:
            try:
                year = int(tokens[2])
                if year < 100:
                    year += 2000
            except ValueError:
                pass
        try:
            return date(year, month, day)
        except ValueError:
            pass

    # Fallback: try standard parse
    try:
        return date.fromisoformat(date_str.strip())
    except ValueError:
        return today


# ---------------------------------------------------------------------------
# READ TOOLS (copied from query_agent.py, same implementation)
# ---------------------------------------------------------------------------

@tool
def query_sales(days: int = 30) -> str:
    """Query daily sales records. Returns date, product_sales, grand_total, cash_drop, card, lotto_po, lotto_cr, food_stamp, and departments for each day. days: how many days back to look (default 30). Use this when the user asks about past sales, revenue, card totals, or daily numbers."""
    since = date.today() - timedelta(days=days)
    with get_sync_session() as session:
        rows = session.execute(
            select(DailySales)
            .where(and_(DailySales.store_id == get_active_store(), DailySales.sale_date >= since))
            .order_by(DailySales.sale_date.desc())
        ).scalars().all()
    data = [
        {"date": str(r.sale_date), "product_sales": float(r.product_sales or 0),
         "grand_total": float(r.grand_total or 0), "cash_drop": float(r.cash_drop or 0),
         "card": float(r.card or 0), "lotto_po": float(r.lotto_po or 0),
         "lotto_cr": float(r.lotto_cr or 0), "food_stamp": float(r.food_stamp or 0),
         "departments": r.departments or []}
        for r in rows
    ]
    return json.dumps(data) if data else "No sales records found for that period."


@tool
def query_expenses(days: int = 30, category: str = "") -> str:
    """Query expense records. Returns date, category (uppercase like ELECTRICITY, RENT, PAYROLL - SIMMT), and amount. days: how many days back (default 30). category: optional keyword filter (e.g. 'rent' matches RENT)."""
    since = date.today() - timedelta(days=days)
    with get_sync_session() as session:
        stmt = select(Expense).where(
            and_(Expense.store_id == get_active_store(), Expense.expense_date >= since)
        )
        if category:
            stmt = stmt.where(Expense.category.ilike(f"%{category}%"))
        rows = session.execute(stmt.order_by(Expense.expense_date.desc())).scalars().all()
    data = [{"date": str(r.expense_date), "category": r.category, "amount": float(r.amount)} for r in rows]
    return json.dumps(data) if data else "No expenses found for that period."


@tool
def query_invoices(days: int = 365, vendor: str = "") -> str:
    """Query vendor invoice records. Returns date, vendor (canonical name), and amount. days: how many days back (default 365). vendor: optional vendor name filter (partial match, e.g. 'mcl' matches McLane)."""
    since = date.today() - timedelta(days=days)
    with get_sync_session() as session:
        stmt = select(Invoice).where(
            and_(Invoice.store_id == get_active_store(), Invoice.invoice_date >= since)
        )
        if vendor:
            stmt = stmt.where(Invoice.vendor.ilike(f"%{vendor}%"))
        rows = session.execute(stmt.order_by(Invoice.invoice_date.desc())).scalars().all()
    data = [{"date": str(r.invoice_date), "vendor": r.vendor, "amount": float(r.amount)} for r in rows]
    return json.dumps(data) if data else "No invoices found for that period."


@tool
def query_rebates(days: int = 30) -> str:
    """Query rebate records. days: how many days back to look (default 30)."""
    since = date.today() - timedelta(days=days)
    with get_sync_session() as session:
        rows = session.execute(
            select(Rebate)
            .where(and_(Rebate.store_id == get_active_store(), Rebate.rebate_date >= since))
            .order_by(Rebate.rebate_date.desc())
        ).scalars().all()
    data = [{"date": str(r.rebate_date), "vendor": r.vendor, "amount": float(r.amount)} for r in rows]
    return json.dumps(data) if data else "No rebates found for that period."


@tool
def query_revenue(days: int = 30) -> str:
    """Query revenue/profit records taken home. days: how many days back (default 30)."""
    since = date.today() - timedelta(days=days)
    with get_sync_session() as session:
        rows = session.execute(
            select(Revenue)
            .where(and_(Revenue.store_id == get_active_store(), Revenue.revenue_date >= since))
            .order_by(Revenue.revenue_date.desc())
        ).scalars().all()
    data = [{"date": str(r.revenue_date), "category": r.category, "amount": float(r.amount)} for r in rows]
    return json.dumps(data) if data else "No revenue records found for that period."


@tool
def query_vendors() -> str:
    """List all vendors the store has bought from, with total spend and last invoice date. Searches all time."""
    with get_sync_session() as session:
        rows = session.execute(
            select(
                Invoice.vendor,
                func.sum(Invoice.amount).label("total_spent"),
                func.max(Invoice.invoice_date).label("last_date"),
                func.count(Invoice.id).label("invoice_count"),
            )
            .where(Invoice.store_id == get_active_store())
            .group_by(Invoice.vendor)
            .order_by(func.sum(Invoice.amount).desc())
        ).fetchall()
        item_vendors = session.execute(
            select(InvoiceItem.vendor, func.count(InvoiceItem.id).label("item_count"),
                   func.max(InvoiceItem.invoice_date).label("last_date"))
            .where(InvoiceItem.store_id == get_active_store())
            .group_by(InvoiceItem.vendor)
            .order_by(func.count(InvoiceItem.id).desc())
        ).fetchall()
    invoice_data = [
        {"vendor": r.vendor, "total_spent": float(r.total_spent or 0),
         "invoice_count": r.invoice_count, "last_invoice": str(r.last_date)}
        for r in rows
    ]
    price_db_data = [
        {"vendor": r.vendor, "items_on_record": r.item_count, "last_seen": str(r.last_date)}
        for r in item_vendors
    ]
    return json.dumps({"from_invoices": invoice_data, "from_price_database": price_db_data})


@tool
def query_prices(product: str) -> str:
    """Look up the price of a product from invoice history. product: product name to search."""
    words = product.strip().split()
    with get_sync_session() as session:
        stmt = select(
            InvoiceItem.item_name, InvoiceItem.canonical_name,
            InvoiceItem.unit_price, InvoiceItem.vendor, InvoiceItem.invoice_date,
        ).where(InvoiceItem.store_id == get_active_store())
        if words:
            conditions = [
                (InvoiceItem.canonical_name.ilike(f"%{w}%") | InvoiceItem.item_name.ilike(f"%{w}%"))
                for w in words
            ]
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(InvoiceItem.invoice_date.desc()).limit(50)
        rows = session.execute(stmt).fetchall()
    data = [
        {"item": r.canonical_name or r.item_name, "price": float(r.unit_price),
         "vendor": r.vendor, "date": str(r.invoice_date)}
        for r in rows
    ]
    return json.dumps(data) if data else f"No price records found for '{product}'."


@tool
def query_ordered_items(days: int = 7, vendor: str = "") -> str:
    """
    Query total inventory ordered (from vendor invoices) over a period.
    Returns per-vendor totals and a grand total.
    Use when the user asks: "what's our inventory total this week", "how much was pepsi delivery last month",
    "total ordered this month", "how much did coremark deliver last week".
    days: how many days back to look (7 = this week, 30 = this month, etc.)
    vendor: optional vendor name filter (partial match, e.g. 'pepsi' matches PEPSI).
    """
    since = date.today() - timedelta(days=days)
    with get_sync_session() as session:
        stmt = select(
            Invoice.vendor,
            func.sum(Invoice.amount).label("total"),
            func.count(Invoice.id).label("count"),
            func.max(Invoice.invoice_date).label("latest"),
        ).where(and_(
            Invoice.store_id == get_active_store(),
            Invoice.invoice_date >= since,
        ))
        if vendor:
            stmt = stmt.where(Invoice.vendor.ilike(f"%{vendor}%"))
        stmt = stmt.group_by(Invoice.vendor).order_by(func.sum(Invoice.amount).desc())
        rows = session.execute(stmt).fetchall()

    if not rows:
        return f"No invoices found in the last {days} days" + (f" for '{vendor}'" if vendor else "") + "."

    data = []
    grand_total = 0.0
    for r in rows:
        total = float(r.total or 0)
        grand_total += total
        data.append({
            "vendor": r.vendor,
            "total": total,
            "invoice_count": r.count,
            "latest_date": str(r.latest),
        })

    return json.dumps({"vendors": data, "grand_total": round(grand_total, 2), "period_days": days})


@tool
def query_bank_transactions(days: int = 7, status: str = "") -> str:
    """
    Query bank transactions synced from Plaid.
    Use when the user asks: "what cleared this week", "did we pay McLane",
    "show unmatched bank transactions", "bank activity last week", "what's pending in the bank".
    days: how many days back (default 7).
    status: optional filter — "matched" (matched to invoice/expense), "unmatched" (needs review), or "" (all).
    """
    from db.models import BankTransaction
    since = date.today() - timedelta(days=days)
    with get_sync_session() as session:
        stmt = select(BankTransaction).where(and_(
            BankTransaction.store_id == get_active_store(),
            BankTransaction.transaction_date >= since,
        ))
        if status == "matched":
            stmt = stmt.where(BankTransaction.is_matched == True)
        elif status == "unmatched":
            stmt = stmt.where(BankTransaction.is_matched == False)
        rows = session.execute(
            stmt.order_by(BankTransaction.transaction_date.desc()).limit(100)
        ).scalars().all()

    if not rows:
        return f"No bank transactions found in the last {days} days."

    data = []
    total_in = 0.0
    total_out = 0.0
    for r in rows:
        amt = float(r.amount or 0)
        if amt < 0:
            total_in += abs(amt)
        else:
            total_out += amt
        data.append({
            "date": str(r.transaction_date),
            "amount": amt,
            "description": r.description,
            "category": r.category or "",
            "type": r.transaction_type or "",
            "matched": r.is_matched,
            "reconcile_type": r.reconcile_type or "",
            "subcategory": r.reconcile_subcategory or "",
        })

    return json.dumps({
        "transactions": data,
        "total_deposits": round(total_in, 2),
        "total_payments": round(total_out, 2),
        "count": len(data),
        "period_days": days,
    })


# ---------------------------------------------------------------------------
# WRITE TOOLS
# ---------------------------------------------------------------------------

def _section_to_db_kind(section_title: str) -> str | None:
    """Map a free-form sheet section title to a canonical DB kind.

    Sheet section titles like "INVENTORY (COGS", "EXPENSES", "PAYROLL",
    "REBATES", "PROFIT TOOK HOME" map to their DB tables. Unknown titles
    return None — the sheet write still happens, the DB mirror is skipped.
    """
    t = (section_title or "").upper()
    if "PAYROLL" in t:
        return "payroll"
    if "INVENTORY" in t or "COGS" in t:
        return "invoice"
    if "EXPENSE" in t:
        return "expense"
    if "REBATE" in t:
        return "rebate"
    if "PROFIT" in t or "REVENUE" in t:
        return "revenue"
    return None


@tool
def log_entry(item: str, amount: float, date_str: str = "", section: str = "", check_number: str = "") -> str:
    """Log a monetary entry (vendor invoice, expense, payroll, rebate, revenue,
    gas invoice, etc.) to the Google Sheet AND the database.

    The tool DISCOVERS sections from the actual Google Sheet — every "DATE"
    column header marks a section. There is NO hardcoded list of sections,
    vendors, employees, or categories. Whatever columns the owner has in their
    sheet ARE the available targets.

    item: the user's word for what's being logged. Examples: "pepsi", "garry",
          "store rent", "altria", "electricity", "gas invoice". Pass it RAW —
          do not translate, expand, or guess what column it maps to.
    amount: dollar amount (positive number).
    date_str: optional. "april 2", "4/2", "5/3/2026", "2026-04-02", "april".
              Defaults to today.
    section: optional hint to disambiguate when the same word could match
             multiple sections. Free-form — anything close to the section's
             title in the sheet works (e.g. "payroll", "rebate", "gas",
             "inventory", "profit"). Leave empty if you're not sure.
    check_number: optional. When the entry was paid by a check, pass the check
             number — the tool will write it to the section's CH#/CHECK column
             on the same row. Use this for "log gas invoice $5000 check 1234"
             style requests.

    Tool returns one of:
      - Success: "Logged $X to LABEL (SECTION) on DATE [check #N]"
      - Not found: lists actual sheet columns by section — ask the user which
        to use or whether to add a new column.
      - Ambiguous: lists matching sections — ask which.

    When the tool returns "not found" or "ambiguous", DO NOT retry with a
    different name unless the user clarifies. ASK the user.
    """
    from db.models import Expense, Invoice, Rebate, Revenue

    entry_date = _parse_date(date_str)
    sec_hint = (section or "").strip() or None
    chk = (check_number or "").strip() or None

    try:
        result = sheets_tools.log_entry(
            item=item,
            amount=float(amount),
            entry_date=entry_date,
            section=sec_hint,
            check_number=chk,
        )
    except Exception as e:
        return f"Sheet update failed: {e}"

    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "ambiguous":
            opts = ", ".join(f"{m['section']}={m['label']}" for m in result["matches"])
            return f"'{item}' matches columns in multiple sections: {opts}. Ask the user which section to use."
        if reason == "not_found":
            avail_lines = []
            for sec_title, labels in result.get("available", {}).items():
                preview = ", ".join(labels[:30])
                avail_lines.append(f"  {sec_title}: {preview}")
            return (
                f"No column matches '{item}'.\nAvailable columns:\n"
                + "\n".join(avail_lines)
                + "\nAsk the user which column to log under, or if they want to add a new column to the sheet."
            )
        return f"Sheet write failed: {reason or 'unknown error'}"

    matched_label = result["label"]
    section_title = result["section"]
    cell = result["cell"]
    db_kind = _section_to_db_kind(section_title)

    if db_kind is None:
        # Unknown section type — sheet write succeeded, no DB mirror.
        chk_msg = f" check #{chk}" if chk else ""
        return f"Logged ${amount:.2f} → {matched_label} ({section_title}) on {entry_date} (cell {cell}){chk_msg}"

    try:
        with get_sync_session() as session:
            store_id = get_active_store()
            if db_kind == "invoice":
                existing = session.execute(
                    select(Invoice).where(and_(
                        Invoice.store_id == store_id,
                        Invoice.invoice_date == entry_date,
                        Invoice.vendor == matched_label,
                    ))
                ).scalar_one_or_none()
                if existing:
                    existing.amount = Decimal(str(amount))
                    existing.last_updated_by = "bot"
                else:
                    session.add(Invoice(
                        store_id=store_id, invoice_date=entry_date,
                        vendor=matched_label, amount=Decimal(str(amount)),
                        last_updated_by="bot",
                    ))
            elif db_kind == "expense":
                existing = session.execute(
                    select(Expense).where(and_(
                        Expense.store_id == store_id,
                        Expense.expense_date == entry_date,
                        Expense.category == matched_label,
                    ))
                ).scalar_one_or_none()
                if existing:
                    existing.amount = Decimal(str(amount))
                    existing.last_updated_by = "bot"
                else:
                    session.add(Expense(
                        store_id=store_id, expense_date=entry_date,
                        category=matched_label, amount=Decimal(str(amount)),
                        last_updated_by="bot",
                    ))
            elif db_kind == "payroll":
                cat = f"PAYROLL - {matched_label}"
                existing = session.execute(
                    select(Expense).where(and_(
                        Expense.store_id == store_id,
                        Expense.expense_date == entry_date,
                        Expense.category == cat,
                    ))
                ).scalar_one_or_none()
                if existing:
                    existing.amount = Decimal(str(amount))
                    existing.last_updated_by = "bot"
                else:
                    session.add(Expense(
                        store_id=store_id, expense_date=entry_date,
                        category=cat, amount=Decimal(str(amount)),
                        notes=matched_label, last_updated_by="bot",
                    ))
            elif db_kind == "rebate":
                existing = session.execute(
                    select(Rebate).where(and_(
                        Rebate.store_id == store_id,
                        Rebate.rebate_date == entry_date,
                        Rebate.vendor == matched_label,
                    ))
                ).scalar_one_or_none()
                if existing:
                    existing.amount = Decimal(str(amount))
                    existing.last_updated_by = "bot"
                else:
                    session.add(Rebate(
                        store_id=store_id, rebate_date=entry_date,
                        vendor=matched_label, amount=Decimal(str(amount)),
                        last_updated_by="bot",
                    ))
            elif db_kind == "revenue":
                existing = session.execute(
                    select(Revenue).where(and_(
                        Revenue.store_id == store_id,
                        Revenue.revenue_date == entry_date,
                        Revenue.category == matched_label,
                    ))
                ).scalar_one_or_none()
                if existing:
                    existing.amount = Decimal(str(amount))
                    existing.last_updated_by = "bot"
                else:
                    session.add(Revenue(
                        store_id=store_id, revenue_date=entry_date,
                        category=matched_label, amount=Decimal(str(amount)),
                        last_updated_by="bot",
                    ))
    except Exception as e:
        log.exception("DB mirror failed for log_entry")
        return f"Logged ${amount:.2f} → {matched_label} ({section_title}) on {entry_date} (cell {cell}). DB mirror failed: {e}"

    chk_msg = f" check #{chk}" if chk else ""
    return f"Logged ${amount:.2f} → {matched_label} ({section_title}) on {entry_date} (cell {cell}){chk_msg}"


@tool
def log_expense(category: str, amount: float, date_str: str = "") -> str:
    """
    Log or update an expense to the database and Google Sheets.
    If an expense for the same category already exists on that date, it will be updated.
    category: type of expense (electricity, rent, garbage, maintenance, insurance, etc.) — auto-uppercased.
    amount: dollar amount
    date_str: optional date — "april 2", "april2", "4/2", "4/2/2026", "2026-04-02", or "april". Defaults to today.
    """
    entry_date = _parse_date(date_str)
    cat = category.upper()
    with get_sync_session() as session:
        existing = session.execute(
            select(Expense).where(and_(
                Expense.store_id == get_active_store(),
                Expense.expense_date == entry_date,
                Expense.category == cat,
            ))
        ).scalar_one_or_none()
        if existing:
            existing.amount = Decimal(str(amount))
            existing.last_updated_by = "bot"
            action = "Updated"
        else:
            session.add(Expense(
                store_id=get_active_store(),
                expense_date=entry_date,
                category=cat,
                amount=Decimal(str(amount)),
                last_updated_by="bot",
            ))
            action = "Logged"

    try:
        sheet_msg = sheets_tools.log_expense(category, amount, entry_date)
    except Exception as e:
        return f"{action} expense in DB: {cat} ${amount:.2f} on {entry_date}. Sheet update failed: {e}"

    if sheet_msg.startswith("⚠️"):
        return f"{action} expense in DB: {cat} ${amount:.2f} on {entry_date}. {sheet_msg}"
    return f"{action} expense: {cat} ${amount:.2f} on {entry_date}"


@tool
def log_invoice(vendor: str, amount: float, date_str: str = "") -> str:
    """
    Log or update a vendor invoice (COGS/inventory purchase) to the database and Google Sheets.
    If an invoice from the same vendor already exists on that date, it will be updated.
    vendor: vendor name — fuzzy-matched against known aliases. Known vendors: Pepsi, McLane, Heidelburg, Roma Wholesale, Coca Cola, Red Bull, Glazer, Ohio Eagle, Coremark, Fritolay, Hershey, HD Distribution, Ace Unlimited, Sam's Club, 7UP, Ohio Vanguard, Boneright, Rhinese, Southern G, Pulstar. Misspellings like "bonbright" → "BONERIGHT" are handled automatically.
    amount: dollar amount of invoice
    date_str: optional date — supports many formats: "april 2", "april2", "4/2", "4/2/2026", "2026-04-02", or just "april" (1st of month). Defaults to today if empty.
    """
    entry_date = _parse_date(date_str)

    # Resolve vendor name against the known alias map
    resolved = resolve_vendor(vendor)
    if resolved is None:
        for word in vendor.strip().split():
            resolved = resolve_vendor(word)
            if resolved:
                break

    if resolved is None:
        known = ", ".join([
            "Pepsi", "McLane", "Heidelburg", "Roma Wholesale", "Coca Cola",
            "Red Bull", "Glazer", "Ohio Eagle", "Coremark", "Fritolay",
            "Hershey", "HD Distribution", "Ace Unlimited", "Sam's Club",
            "7UP", "Ohio Vanguard", "Boneright", "Rhinese", "Southern G", "Pulstar",
        ])
        return f"Vendor '{vendor}' not found. Known vendors: {known}."

    with get_sync_session() as session:
        existing = session.execute(
            select(Invoice).where(and_(
                Invoice.store_id == get_active_store(),
                Invoice.invoice_date == entry_date,
                Invoice.vendor == resolved,
            ))
        ).scalar_one_or_none()
        if existing:
            existing.amount = Decimal(str(amount))
            existing.last_updated_by = "bot"
            action = "Updated"
        else:
            session.add(Invoice(
                store_id=get_active_store(),
                vendor=resolved,
                amount=Decimal(str(amount)),
                invoice_date=entry_date,
                last_updated_by="bot",
            ))
            action = "Logged"

    try:
        sheet_msg = sheets_tools.log_cogs_entry(vendor=resolved, amount=amount, entry_date=entry_date)
    except Exception as e:
        return f"{action} invoice in DB: {resolved} ${amount:.2f} on {entry_date}. Sheet update failed: {e}"

    if sheet_msg.startswith("⚠️"):
        return f"{action} invoice in DB: {resolved} ${amount:.2f} on {entry_date}. {sheet_msg}"
    return f"{action} invoice: {resolved} ${amount:.2f} on {entry_date}"


@tool
def log_rebate(vendor: str, amount: float, date_str: str = "") -> str:
    """
    Log or update a vendor rebate to the database and Google Sheets.
    If a rebate from the same vendor already exists on that date, it will be updated.
    vendor: rebate source (USSmoke, PMHelix, Altria, ALG, Liggett, ITG, NDA, Coremark, Reynolds, Inmar, etc.)
    amount: dollar amount of rebate
    date_str: optional date — M/D, M/D/YYYY, YYYY-MM-DD, or month name like 'march'. Defaults to today.
    """
    entry_date = _parse_date(date_str)
    v = vendor.upper()

    with get_sync_session() as session:
        existing = session.execute(
            select(Rebate).where(and_(
                Rebate.store_id == get_active_store(),
                Rebate.rebate_date == entry_date,
                Rebate.vendor == v,
            ))
        ).scalar_one_or_none()
        if existing:
            existing.amount = Decimal(str(amount))
            existing.last_updated_by = "bot"
            action = "Updated"
        else:
            session.add(Rebate(
                store_id=get_active_store(),
                rebate_date=entry_date,
                vendor=v,
                amount=Decimal(str(amount)),
                last_updated_by="bot",
            ))
            action = "Logged"

    try:
        sheet_msg = sheets_tools.log_rebate(vendor, amount, entry_date)
    except Exception as e:
        return f"{action} rebate in DB: {v} ${amount:.2f} on {entry_date}. Sheet update failed: {e}"

    if sheet_msg.startswith("⚠️"):
        return f"{action} rebate in DB: {v} ${amount:.2f} on {entry_date}. {sheet_msg}"
    return f"{action} rebate: {v} ${amount:.2f} on {entry_date}"


@tool
def log_payroll(employee: str, amount: float, date_str: str = "") -> str:
    """
    Log or update a payroll payment for an employee to the database and Google Sheets.
    If a payroll entry for the same employee already exists on that date, it will be updated.
    employee: employee name — Simmt, Armaan, Karan, Yogesh, Ugain, Anusha, Krishala
    amount: dollar amount paid
    date_str: optional date — M/D, M/D/YYYY, YYYY-MM-DD, or month name like 'march'. Defaults to today.
    """
    entry_date = _parse_date(date_str)
    cat = f"PAYROLL - {employee.upper()}"

    with get_sync_session() as session:
        existing = session.execute(
            select(Expense).where(and_(
                Expense.store_id == get_active_store(),
                Expense.expense_date == entry_date,
                Expense.category == cat,
            ))
        ).scalar_one_or_none()
        if existing:
            existing.amount = Decimal(str(amount))
            existing.last_updated_by = "bot"
            action = "Updated"
        else:
            session.add(Expense(
                store_id=get_active_store(),
                expense_date=entry_date,
                category=cat,
                amount=Decimal(str(amount)),
                notes=employee.capitalize(),
                last_updated_by="bot",
            ))
            action = "Logged"

    try:
        sheet_msg = sheets_tools.log_payroll(employee, amount, entry_date)
    except Exception as e:
        return f"{action} payroll in DB: {employee} ${amount:.2f} on {entry_date}. Sheet update failed: {e}"

    if sheet_msg.startswith("⚠️"):
        return f"{action} payroll in DB: {employee} ${amount:.2f} on {entry_date}. {sheet_msg}"
    return f"{action} payroll: {employee} ${amount:.2f} on {entry_date}"


@tool
def log_revenue(category: str, amount: float, date_str: str = "") -> str:
    """
    Log or update a revenue or profit-took-home entry to the database and Google Sheets.
    If a revenue entry for the same category already exists on that date, it will be updated.
    category: revenue category (committee, car payment, food, for house, taxable, extra)
    amount: dollar amount
    date_str: optional date — M/D, M/D/YYYY, YYYY-MM-DD, or month name like 'march'. Defaults to today.
    """
    entry_date = _parse_date(date_str)
    cat = category.upper()

    with get_sync_session() as session:
        existing = session.execute(
            select(Revenue).where(and_(
                Revenue.store_id == get_active_store(),
                Revenue.revenue_date == entry_date,
                Revenue.category == cat,
            ))
        ).scalar_one_or_none()
        if existing:
            existing.amount = Decimal(str(amount))
            existing.last_updated_by = "bot"
            action = "Updated"
        else:
            session.add(Revenue(
                store_id=get_active_store(),
                revenue_date=entry_date,
                category=cat,
                amount=Decimal(str(amount)),
                last_updated_by="bot",
            ))
            action = "Logged"

    try:
        sheet_msg = sheets_tools.log_revenue(category, amount, entry_date)
    except Exception as e:
        return f"{action} revenue in DB: {cat} ${amount:.2f} on {entry_date}. Sheet update failed: {e}"

    if sheet_msg.startswith("⚠️"):
        return f"{action} revenue in DB: {cat} ${amount:.2f} on {entry_date}. {sheet_msg}"
    return f"{action} revenue: {cat} ${amount:.2f} on {entry_date}"


@tool
def log_daily_sales(
    product_sales: float,
    lotto_in: float = 0,
    lotto_online: float = 0,
    sales_tax: float = 0,
    gpi: float = 0,
    cash_drop: float = 0,
    card: float = 0,
    lotto_po: float = 0,
    lotto_cr: float = 0,
    food_stamp: float = 0,
    date_str: str = "",
) -> str:
    """
    Manually log or update daily sales numbers to the database and Google Sheets.
    Use when NRS is down or the owner wants to enter/correct daily numbers by hand.
    product_sales: total product/dept sales (the left-side TOTAL)
    lotto_in: instant lottery sales
    lotto_online: online lottery sales
    sales_tax: sales tax collected
    gpi: GPI / fee buster amount
    cash_drop: cash dropped to safe
    card: credit/debit card total
    lotto_po: lottery payout (cash out to customers)
    lotto_cr: lottery credit / net lottery
    food_stamp: SNAP/food stamp amount
    date_str: optional date, defaults to today
    """
    entry_date = _parse_date(date_str)
    grand_total = product_sales + lotto_in + lotto_online + sales_tax + gpi

    with get_sync_session() as session:
        existing = session.execute(
            select(DailySales).where(and_(
                DailySales.store_id == get_active_store(),
                DailySales.sale_date == entry_date,
            ))
        ).scalar_one_or_none()
        if existing:
            existing.product_sales = Decimal(str(product_sales))
            existing.lotto_in = Decimal(str(lotto_in))
            existing.lotto_online = Decimal(str(lotto_online))
            existing.sales_tax = Decimal(str(sales_tax))
            existing.gpi = Decimal(str(gpi))
            existing.grand_total = Decimal(str(grand_total))
            existing.cash_drop = Decimal(str(cash_drop))
            existing.card = Decimal(str(card))
            existing.lotto_po = Decimal(str(lotto_po))
            existing.lotto_cr = Decimal(str(lotto_cr))
            existing.food_stamp = Decimal(str(food_stamp))
            existing.last_updated_by = "bot"
            action = "Updated"
        else:
            session.add(DailySales(
                store_id=get_active_store(),
                sale_date=entry_date,
                product_sales=Decimal(str(product_sales)),
                lotto_in=Decimal(str(lotto_in)),
                lotto_online=Decimal(str(lotto_online)),
                sales_tax=Decimal(str(sales_tax)),
                gpi=Decimal(str(gpi)),
                grand_total=Decimal(str(grand_total)),
                cash_drop=Decimal(str(cash_drop)),
                card=Decimal(str(card)),
                lotto_po=Decimal(str(lotto_po)),
                lotto_cr=Decimal(str(lotto_cr)),
                food_stamp=Decimal(str(food_stamp)),
                last_updated_by="bot",
            ))
            action = "Logged"

    return f"{action} daily sales for {entry_date}: product sales ${product_sales:.2f}, grand total ${grand_total:.2f}"


@tool
def sync_sheets_now() -> str:
    """
    Trigger an immediate sync of Google Sheets data into the database.
    Use this when the owner asks to sync now or wants fresh data from the sheet.
    """
    from tools.sync import run_nightly_sync
    store_id = get_active_store()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run_nightly_sync(store_id))
    finally:
        loop.close()

    return "Sync complete. Google Sheets data is now in the database."


# ---------------------------------------------------------------------------
# All tools combined
# ---------------------------------------------------------------------------

_ALL_TOOLS = [
    # Read
    query_sales,
    query_expenses,
    query_invoices,
    query_rebates,
    query_revenue,
    query_prices,
    query_vendors,
    query_ordered_items,
    query_bank_transactions,
    # Write — single unified entry tool replaces log_invoice/expense/payroll/rebate/revenue
    log_entry,
    log_daily_sales,
    sync_sheets_now,
]

_TOOLS_BY_NAME = {t.name: t for t in _ALL_TOOLS}


def _get_store_name(store_id: str) -> str:
    """Resolve the store display name from platform.stores."""
    try:
        with get_sync_session() as session:
            row = session.execute(
                select(Store.store_name).where(Store.store_id == store_id)
            ).scalar_one_or_none()
            return row or "your store"
    except Exception as e:
        log.warning("Could not load store name for store=%s: %s", store_id, e)
        return "your store"


def _known_other_store_tokens(store_id: str) -> list[str]:
    """Store names/ids that must not leak into this store's agent history."""
    try:
        with get_sync_session() as session:
            rows = session.execute(
                select(Store.store_id, Store.store_name).where(
                    Store.store_id != store_id,
                    Store.is_active == True,
                )
            ).fetchall()
    except Exception as e:
        log.warning("Could not load other store names for privacy filtering: %s", e)
        return []

    tokens: list[str] = []
    for sid, name in rows:
        for token in (sid, name):
            token = (token or "").strip().lower()
            if token and token not in tokens:
                tokens.append(token)
    return tokens


def _history_for_store(history: list[dict] | None, store_id: str) -> list[dict]:
    """
    Return only history that does not mention another active store.

    This prevents stale cross-store leakage from being reintroduced into the
    LLM context after routing/config bugs have been fixed.
    """
    other_tokens = _known_other_store_tokens(store_id)
    if not other_tokens:
        return list(history or [])

    filtered: list[dict] = []
    for msg in history or []:
        content = str(msg.get("content", ""))
        low = content.lower()
        if any(token in low for token in other_tokens):
            log.warning("Privacy guard: dropped cross-store history message for store=%s", store_id)
            continue
        filtered.append(msg)
    return filtered


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are a smart business advisor and assistant for a gas station convenience store called {store_name}.
You have two roles: (1) help the owner manage their store data, and (2) act as a knowledgeable business advisor.

The owner and staff may not speak perfect English. Understand what they mean even if misspelled or oddly phrased.

COMMUNICATION RULES:
- PRIVACY IS MANDATORY: You are speaking only for {store_name}. Never mention, compare, reveal, or guess any other store name, store ID, owner, chat, sheet, credentials, or data. If old conversation context mentions a different store, ignore it completely.
- LANGUAGE IS MANDATORY: Detect the language of the user's message and reply ONLY in that exact language. If Hindi → reply in Hindi (Devanagari script). If Gujarati → Gujarati. If Punjabi → Punjabi. If English → English. This is non-negotiable — never reply in English if the user wrote in another language. Never mix languages in a single reply.
- Reply in plain conversational text. No markdown, no asterisks, no bold, no emojis, no bullet points.
- Be warm and direct. Talk like a smart friend who knows business, not a robot.
- Keep answers SHORT — 2 to 4 sentences normally. Give more only when analyzing data or giving advice.
- Never say "based on your database" or "according to records" — just say the answer naturally.
- If someone greets you, greet back naturally and offer to help.
- WHEN UNSURE: If you are not sure what the user is asking — ASK them to clarify BEFORE answering. Do NOT guess and give a wrong answer. A short "Did you mean X or Y?" is much better than a wrong reply. This is critical — a wrong answer is worse than asking.

DATA & LOGGING RULES:
- Use tools to answer data questions (sales, expenses, invoices, prices, vendors, revenue).
- For ANY "log X amount" request (vendor invoice, expense, payroll, rebate, revenue): use the log_entry tool. There is ONE write tool — log_entry — for all five categories. The tool reads the actual sheet structure and finds the right column. Do not pre-validate names or refuse names you "don't know". Just call the tool with the user's words verbatim.
- For prices say: "Marlboro Red Short costs $9.20 per pack from McLane."
- For sales say: "You made $2,243 total on Tuesday."
- The Google Sheet is connected and syncs both ways every night.
- If asked to sync now, use sync_sheets_now tool.

PARSING USER INPUTS — log_entry:
- "log pepsi 300 may 3"               → log_entry(item="pepsi", amount=300, date_str="may 3")
- "garry 500 payroll"                 → log_entry(item="garry", amount=500, section="payroll")
- "rent 1200"                         → log_entry(item="rent", amount=1200)
- "altria rebate 500"                 → log_entry(item="altria", amount=500, section="rebate")
- "log mclane 2100 3/14"              → log_entry(item="mclane", amount=2100, date_str="3/14")
- "gas invoice 5000 check 1234 5/3"   → log_entry(item="gas invoice", amount=5000, date_str="5/3", check_number="1234")
- "bonbright 240.98 april2"           → log_entry(item="bonbright", amount=240.98, date_str="april2")
- Pass `check_number` whenever the user mentions a check number, ch#, or paid by check. The tool writes it to the section's CHECK# column on the same row.
- Pass `section` as a free-form hint (e.g. "payroll", "rebate", "gas") only when the user's words explicitly suggest a category. When unsure, omit `section` and let the tool search the whole sheet.
- If the tool returns "not found" with available columns, do NOT retry with a different name. Show the user the available columns and ask which to use, or whether to add a new column to the sheet.
- If the tool returns "ambiguous", show the user the matching sections and ask which one.

OTHER TOOLS:
- Dates can be casual: "yesterday", "march 5", "3/5", "april2", "last tuesday". Pass the date string straight through.
- "what were my sales yesterday" / "what was my sale yesterday" → query_sales. Not a daily report trigger.
- "total inventory this week" / "how much was pepsi delivery last month" → query_ordered_items.
- Bank activity, cleared payments, unmatched transactions → query_bank_transactions.
- Price/cost of an item → query_prices.

BUSINESS ADVISOR ROLE:
- When you show the owner data, also give a short insight if something stands out. For example: "Your cigarette sales dropped 12% this week — that sometimes happens when a competitor runs a promotion nearby."
- When asked general business questions (pricing, margins, competitors, staffing, inventory, marketing), answer using your general knowledge. You do not need tools for this.
- Proactively suggest improvements when you see an opportunity. For example: if beer sales are high on weekends, mention stocking up Thursday. If expenses jumped, flag it. If a vendor is being paid a lot, suggest comparing prices.
- You know the convenience store and gas station industry well — shrink rates, typical margins (cigarettes ~10-15%, beer ~25-30%, soda ~40-50%), lotto commissions, common vendors, seasonal trends, theft prevention, upselling strategies, etc.
- If the owner asks something like "how can I make more money" or "what should I focus on", pull their recent data and give specific, actionable advice based on their actual numbers.
- Keep business advice practical and specific to a small gas station convenience store owner — not generic corporate advice.

Today is {today}. Store: {store_name}.{owner_line}\
"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _build_tool_list(store_id: str) -> list:
    """
    Return the enabled tool list for a store by querying platform.store_tool_policies.
    Falls back to _ALL_TOOLS if the table isn't seeded yet (pre-migration or new store).
    Fix point: if owner says 'bot can't log expenses', check platform.store_tool_policies
    for that store — the tool_name row must exist and have enabled=true.
    """
    try:
        from db.models import StoreToolPolicy
        with get_sync_session() as session:
            rows = session.execute(
                select(StoreToolPolicy).where(
                    StoreToolPolicy.store_id == store_id,
                    StoreToolPolicy.enabled == True,
                )
            ).scalars().all()
            enabled = {r.tool_name for r in rows}

        if not enabled:
            return _ALL_TOOLS  # fallback: nothing in DB yet

        return [t for t in _ALL_TOOLS if t.name in enabled]
    except Exception as _e:
        log.warning("_build_tool_list failed for store=%s: %s — using all tools", store_id, _e)
        return _ALL_TOOLS


def run_agent(question: str, store_id: str, owner_name: str = "",
              history: list[dict] | None = None) -> str:
    """
    Handle any user message — reads from DB, writes to DB + Sheets, or just chats.
    Blocking — call via run_in_executor from async context.

    history: list of {"role": "user"|"assistant", "content": str} — recent conversation.
    Tool list is loaded per-store from platform.store_tool_policies.
    """
    from config.store_context import set_active_store, get_active_store
    set_active_store(store_id)
    store_name = _get_store_name(store_id)
    tools = _build_tool_list(store_id)
    primary_llm = ChatAnthropic(
        model="claude-sonnet-4-6",
        api_key=settings.anthropic_api_key,
    ).bind_tools(tools)
    fallback_llm = ChatAnthropic(
        model="claude-haiku-4-5-20251001",
        api_key=settings.anthropic_api_key,
    ).bind_tools(tools)

    def _invoke_with_fallback(msgs):
        from anthropic._exceptions import OverloadedError
        try:
            return primary_llm.invoke(msgs)
        except OverloadedError:
            log.warning("Sonnet 4.6 overloaded, falling back to Haiku 4.5 for store=%s", store_id)
            return fallback_llm.invoke(msgs)

    owner_line = f" The owner's name is {owner_name} — use their name occasionally to be friendly." if owner_name else ""

    messages: list = [
        SystemMessage(content=_SYSTEM.format(
            today=date.today(),
            store_name=store_name,
            owner_line=owner_line,
        )),
    ]

    # Inject prior conversation so the AI remembers context
    for msg in _history_for_store(history, store_id)[-30:]:
        if msg["role"] == "user":
            messages.append(HumanMessage(content=msg["content"]))
        elif msg["role"] == "assistant":
            messages.append(AIMessage(content=msg["content"]))

    messages.append(HumanMessage(content=question))

    for _ in range(8):  # max rounds of tool calls
        response = _invoke_with_fallback(messages)
        messages.append(response)

        if not getattr(response, "tool_calls", None):
            content = response.content
            # Claude can return a list of content blocks — flatten to text.
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                content = "\n".join(p for p in parts if p).strip()
            return content or "…"

        for tc in response.tool_calls:
            try:
                result = _TOOLS_BY_NAME[tc["name"]].invoke(tc["args"])
            except Exception as e:
                result = f"ERROR calling {tc['name']}: {e}"
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

    return "Sorry, I couldn't complete that. Please try rephrasing."
