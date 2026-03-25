"""
Main agent — handles ALL user messages with a single Claude call + tool loop.

Replaces the intent router for most flows. Both read (query DB) and write
(log expenses, invoices, rebates, revenue to DB + Sheets) tools are available.
Claude decides which tools to call based on what the user says.

Entry point: run_agent(question, store_id) — blocking, safe to call via run_in_executor.
"""

import asyncio
import json
from datetime import date, timedelta
from decimal import Decimal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from sqlalchemy import and_, select, func

from config.settings import settings
from db.database import get_sync_session
from db.models import (
    DailySales, Expense, Invoice, InvoiceItem, Rebate, Revenue,
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
    """Query daily sales records. days: how many days back to look (default 30)."""
    since = date.today() - timedelta(days=days)
    with get_sync_session() as session:
        rows = session.execute(
            select(DailySales)
            .where(and_(DailySales.store_id == settings.store_id, DailySales.sale_date >= since))
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
    """Query expense records. days: how many days back. category: optional keyword filter."""
    since = date.today() - timedelta(days=days)
    with get_sync_session() as session:
        stmt = select(Expense).where(
            and_(Expense.store_id == settings.store_id, Expense.expense_date >= since)
        )
        if category:
            stmt = stmt.where(Expense.category.ilike(f"%{category}%"))
        rows = session.execute(stmt.order_by(Expense.expense_date.desc())).scalars().all()
    data = [{"date": str(r.expense_date), "category": r.category, "amount": float(r.amount)} for r in rows]
    return json.dumps(data) if data else "No expenses found for that period."


@tool
def query_invoices(days: int = 365, vendor: str = "") -> str:
    """Query vendor invoice records. days: how many days back (default 365). vendor: optional vendor name filter."""
    since = date.today() - timedelta(days=days)
    with get_sync_session() as session:
        stmt = select(Invoice).where(
            and_(Invoice.store_id == settings.store_id, Invoice.invoice_date >= since)
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
            .where(and_(Rebate.store_id == settings.store_id, Rebate.rebate_date >= since))
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
            .where(and_(Revenue.store_id == settings.store_id, Revenue.revenue_date >= since))
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
            .where(Invoice.store_id == settings.store_id)
            .group_by(Invoice.vendor)
            .order_by(func.sum(Invoice.amount).desc())
        ).fetchall()
        item_vendors = session.execute(
            select(InvoiceItem.vendor, func.count(InvoiceItem.id).label("item_count"),
                   func.max(InvoiceItem.invoice_date).label("last_date"))
            .where(InvoiceItem.store_id == settings.store_id)
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
        ).where(InvoiceItem.store_id == settings.store_id)
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


# ---------------------------------------------------------------------------
# WRITE TOOLS
# ---------------------------------------------------------------------------

@tool
def log_expense(category: str, amount: float, date_str: str = "") -> str:
    """
    Log an expense to the database and Google Sheets.
    category: type of expense (electricity, rent, garbage, maintenance, etc.)
    amount: dollar amount
    date_str: optional date — M/D, M/D/YYYY, YYYY-MM-DD, or month name like 'march'. Defaults to today.
    """
    entry_date = _parse_date(date_str)
    with get_sync_session() as session:
        row = Expense(
            store_id=settings.store_id,
            expense_date=entry_date,
            category=category.upper(),
            amount=Decimal(str(amount)),
            last_updated_by="bot",
        )
        session.add(row)

    try:
        sheets_tools.log_expense(category, amount, entry_date)
    except Exception as e:
        return f"Logged expense to DB: {category} ${amount:.2f} on {entry_date}. Sheet update failed: {e}"

    return f"Logged expense: {category} ${amount:.2f} on {entry_date}"


@tool
def log_invoice(vendor: str, amount: float, date_str: str = "") -> str:
    """
    Log a vendor invoice (COGS/inventory purchase) to the database and Google Sheets.
    vendor: vendor name (Pepsi, McLane, Heidelburg, Roma Wholesale, Coca Cola, Red Bull, Glazer, Ohio Eagle, etc.)
    amount: dollar amount of invoice
    date_str: optional date — M/D, M/D/YYYY, YYYY-MM-DD, or month name like 'march'. Defaults to today.
    """
    entry_date = _parse_date(date_str)

    # Resolve vendor name against the known alias map
    resolved = resolve_vendor(vendor)
    if resolved is None:
        # Try resolving each word individually (handles "Ohio Eagle" → "OHIO EAGLE" etc.)
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
        row = Invoice(
            store_id=settings.store_id,
            vendor=resolved,
            amount=Decimal(str(amount)),
            invoice_date=entry_date,
            last_updated_by="bot",
        )
        session.add(row)

    try:
        sheets_tools.log_cogs_entry(vendor=resolved, amount=amount, entry_date=entry_date)
    except Exception as e:
        return f"Logged invoice to DB: {resolved} ${amount:.2f} on {entry_date}. Sheet update failed: {e}"

    return f"Logged invoice: {resolved} ${amount:.2f} on {entry_date}"


@tool
def log_rebate(vendor: str, amount: float, date_str: str = "") -> str:
    """
    Log a vendor rebate to the database and Google Sheets.
    vendor: rebate source (USSmoke, PMHelix, Altria, ALG, Liggett, ITG, NDA, Coremark, Reynolds, Inmar, etc.)
    amount: dollar amount of rebate
    date_str: optional date — M/D, M/D/YYYY, YYYY-MM-DD, or month name like 'march'. Defaults to today.
    """
    entry_date = _parse_date(date_str)

    with get_sync_session() as session:
        row = Rebate(
            store_id=settings.store_id,
            rebate_date=entry_date,
            vendor=vendor.upper(),
            amount=Decimal(str(amount)),
            last_updated_by="bot",
        )
        session.add(row)

    try:
        sheets_tools.log_rebate(vendor, amount, entry_date)
    except Exception as e:
        return f"Logged rebate to DB: {vendor} ${amount:.2f} on {entry_date}. Sheet update failed: {e}"

    return f"Logged rebate: {vendor} ${amount:.2f} on {entry_date}"


@tool
def log_revenue(category: str, amount: float, date_str: str = "") -> str:
    """
    Log a revenue or profit-took-home entry to the database and Google Sheets.
    category: revenue category (committee, car payment, food, for house, taxable, extra)
    amount: dollar amount
    date_str: optional date — M/D, M/D/YYYY, YYYY-MM-DD, or month name like 'march'. Defaults to today.
    """
    entry_date = _parse_date(date_str)

    with get_sync_session() as session:
        row = Revenue(
            store_id=settings.store_id,
            revenue_date=entry_date,
            category=category.upper(),
            amount=Decimal(str(amount)),
            last_updated_by="bot",
        )
        session.add(row)

    try:
        sheets_tools.log_revenue(category, amount, entry_date)
    except Exception as e:
        return f"Logged revenue to DB: {category} ${amount:.2f} on {entry_date}. Sheet update failed: {e}"

    return f"Logged revenue: {category} ${amount:.2f} on {entry_date}"


@tool
def sync_sheets_now() -> str:
    """
    Trigger an immediate sync of Google Sheets data into the database.
    Use this when the owner asks to sync now or wants fresh data from the sheet.
    """
    from tools.sync import run_nightly_sync
    store_id = settings.store_id

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
    # Write
    log_expense,
    log_invoice,
    log_rebate,
    log_revenue,
    sync_sheets_now,
]

_TOOLS_BY_NAME = {t.name: t for t in _ALL_TOOLS}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are a smart business advisor and assistant for a gas station convenience store called {store_name}.
You have two roles: (1) help the owner manage their store data, and (2) act as a knowledgeable business advisor.

The owner and staff may not speak perfect English. Understand what they mean even if misspelled or oddly phrased.

COMMUNICATION RULES:
- Reply in plain conversational English. No markdown, no asterisks, no bold, no emojis, no bullet points.
- Be warm and direct. Talk like a smart friend who knows business, not a robot.
- Keep answers SHORT — 2 to 4 sentences normally. Give more only when analyzing data or giving advice.
- Never say "based on your database" or "according to records" — just say the answer naturally.
- If someone greets you, greet back naturally and offer to help.

DATA & LOGGING RULES:
- Use tools to answer data questions (sales, expenses, invoices, prices, vendors, revenue).
- For write operations (log expense, invoice, rebate, revenue): call the log_* tool directly. Do not ask for confirmation unless something is clearly ambiguous.
- For prices say: "Marlboro Red Short costs $9.20 per pack from McLane."
- For sales say: "You made $2,243 total on Tuesday."
- The Google Sheet is connected and syncs both ways every night.
- If asked to sync now, use sync_sheets_now tool.

BUSINESS ADVISOR ROLE:
- When you show the owner data, also give a short insight if something stands out. For example: "Your cigarette sales dropped 12% this week — that sometimes happens when a competitor runs a promotion nearby."
- When asked general business questions (pricing, margins, competitors, staffing, inventory, marketing), answer using your general knowledge. You do not need tools for this.
- Proactively suggest improvements when you see an opportunity. For example: if beer sales are high on weekends, mention stocking up Thursday. If expenses jumped, flag it. If a vendor is being paid a lot, suggest comparing prices.
- You know the convenience store and gas station industry well — shrink rates, typical margins (cigarettes ~10-15%, beer ~25-30%, soda ~40-50%), lotto commissions, common vendors, seasonal trends, theft prevention, upselling strategies, etc.
- If the owner asks something like "how can I make more money" or "what should I focus on", pull their recent data and give specific, actionable advice based on their actual numbers.
- Keep business advice practical and specific to a small gas station convenience store owner — not generic corporate advice.

Today is {today}. Store: {store_name}.\
"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_agent(question: str, store_id: str) -> str:
    """
    Handle any user message — reads from DB, writes to DB + Sheets, or just chats.
    Blocking — call via run_in_executor from async context.

    store_id is accepted for future multi-store support; currently settings.store_id
    is used inside individual tools (they read from settings directly).
    """
    llm = ChatAnthropic(
        model="claude-sonnet-4-6",
        api_key=settings.anthropic_api_key,
    ).bind_tools(_ALL_TOOLS)

    messages = [
        SystemMessage(content=_SYSTEM.format(
            today=date.today(),
            store_name=settings.store_name,
        )),
        HumanMessage(content=question),
    ]

    for _ in range(8):  # max rounds of tool calls
        response = llm.invoke(messages)
        messages.append(response)

        if not getattr(response, "tool_calls", None):
            return response.content

        for tc in response.tool_calls:
            try:
                result = _TOOLS_BY_NAME[tc["name"]].invoke(tc["args"])
            except Exception as e:
                result = f"ERROR calling {tc['name']}: {e}"
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

    return "Sorry, I couldn't complete that. Please try rephrasing."
