"""
Telegram AI query handler — answers owner questions using Claude Sonnet + DB tools.

Called when the intent router classifies a message as "query".
Runs synchronously (blocking) so it can be used with run_in_executor.
"""

import json
from datetime import date, timedelta

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from sqlalchemy import and_, select

from config.settings import settings
from db.database import get_sync_session
from db.models import DailySales, Expense, Invoice, InvoiceItem, Rebate, Revenue
from config.store_context import get_active_store


# ---------------------------------------------------------------------------
# Sync DB tools — safe to call directly from run_in_executor threads
# ---------------------------------------------------------------------------

@tool
def query_sales(days: int = 30) -> str:
    """Query daily sales records. days: how many days back to look (default 30)."""
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
    """Query expense records. days: how many days back. category: optional keyword filter."""
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
    """Query vendor invoice records. days: how many days back (default 365). vendor: optional vendor name filter."""
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
        from sqlalchemy import func
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
        # Also get vendors from invoice_items (price database)
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


_QUERY_TOOLS = [query_sales, query_expenses, query_invoices, query_rebates, query_revenue, query_prices, query_vendors]
_TOOLS_BY_NAME = {t.name: t for t in _QUERY_TOOLS}


def _get_store_name() -> str:
    try:
        from db.models import Store
        with get_sync_session() as session:
            row = session.execute(
                select(Store.store_name).where(Store.store_id == get_active_store())
            ).scalar_one_or_none()
            return row or "your store"
    except Exception:
        return "your store"

_SYSTEM = """\
You are a helpful assistant for a gas station convenience store. \
The owner and staff may not speak perfect English and are not tech-savvy. \
Understand what they mean even if it is spelled wrong or phrased oddly.

RULES:
- Privacy is mandatory. You are speaking only for {store_name}. Never mention, compare, reveal, or guess any other store name, store ID, owner, chat, sheet, credentials, or data.
- Reply in plain conversational English only. No markdown, no asterisks, no bold, no bullet points, no emojis.
- Be direct. Answer like a knowledgeable friend, not a database report.
- Keep answers SHORT — 1 to 3 sentences is ideal. Only give more if truly needed.
- Never say "based on your database" or "according to records" — just say the answer.
- If someone greets you, greet back naturally in one sentence.
- If you have no data, say so simply: "I don't have that info yet."
- For prices: say "Marlboro Red Short costs $9.20 per pack from McLane."
- For sales: say "You made $2,243 total on Tuesday."
- For comparisons: say "This week you made $8,400. Last week was $7,900, so you are up about $500."
- Never suggest the owner set things up or change how they work. Just answer the question.
- Use tools when you need real data. For greetings or small talk, respond without tools.

WHAT YOU KNOW:
- Sales, expenses, invoices, vendor prices, rebates, and revenue are all in the database.
- The Google Sheet IS connected — data syncs both ways every night. If asked, say yes it is connected.

Today is {today}. Store: {store_name}.\
"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def answer_query(question: str) -> str:
    """
    Answer an owner's natural language question using Claude Sonnet + DB tools.
    Blocking — call via run_in_executor from async context.
    """
    llm = ChatAnthropic(
        model="claude-sonnet-4-6",
        api_key=settings.anthropic_api_key,
    ).bind_tools(_QUERY_TOOLS)

    messages = [
        SystemMessage(content=_SYSTEM.format(today=date.today(), store_name=_get_store_name())),
        HumanMessage(content=question),
    ]

    for _ in range(6):  # max rounds of tool calls
        response = llm.invoke(messages)
        messages.append(response)

        if not getattr(response, "tool_calls", None):
            return response.content

        for tc in response.tool_calls:
            try:
                result = _TOOLS_BY_NAME[tc["name"]].invoke(tc["args"])
            except Exception as e:
                result = f"ERROR: {e}"
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

    return "Sorry, I couldn't find the information. Try rephrasing your question."
