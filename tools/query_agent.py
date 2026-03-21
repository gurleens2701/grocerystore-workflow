"""
Telegram AI query handler — answers owner questions using Claude Sonnet + DB tools.

Called when the intent router classifies a message as "query".
Runs synchronously (blocking) so it can be used with run_in_executor.
"""

import asyncio
import json
from datetime import date, timedelta

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from sqlalchemy import and_, select

from config.settings import settings
from db.database import get_async_session
from db.models import DailySales, Expense, Invoice, Rebate, Revenue


# ---------------------------------------------------------------------------
# Async DB helpers
# ---------------------------------------------------------------------------

async def _sales_rows(days: int) -> list[dict]:
    since = date.today() - timedelta(days=days)
    async with get_async_session() as session:
        result = await session.execute(
            select(DailySales)
            .where(and_(DailySales.store_id == settings.store_id, DailySales.sale_date >= since))
            .order_by(DailySales.sale_date.desc())
        )
        rows = result.scalars().all()
    return [
        {
            "date": str(r.sale_date),
            "product_sales": float(r.product_sales or 0),
            "grand_total": float(r.grand_total or 0),
            "cash_drop": float(r.cash_drop or 0),
            "card": float(r.card or 0),
            "lotto_po": float(r.lotto_po or 0),
            "lotto_cr": float(r.lotto_cr or 0),
            "food_stamp": float(r.food_stamp or 0),
        }
        for r in rows
    ]


async def _expense_rows(days: int, category: str) -> list[dict]:
    since = date.today() - timedelta(days=days)
    async with get_async_session() as session:
        stmt = select(Expense).where(
            and_(Expense.store_id == settings.store_id, Expense.expense_date >= since)
        )
        if category:
            stmt = stmt.where(Expense.category.ilike(f"%{category}%"))
        result = await session.execute(stmt.order_by(Expense.expense_date.desc()))
        rows = result.scalars().all()
    return [{"date": str(r.expense_date), "category": r.category, "amount": float(r.amount)} for r in rows]


async def _invoice_rows(days: int, vendor: str) -> list[dict]:
    since = date.today() - timedelta(days=days)
    async with get_async_session() as session:
        stmt = select(Invoice).where(
            and_(Invoice.store_id == settings.store_id, Invoice.invoice_date >= since)
        )
        if vendor:
            stmt = stmt.where(Invoice.vendor.ilike(f"%{vendor}%"))
        result = await session.execute(stmt.order_by(Invoice.invoice_date.desc()))
        rows = result.scalars().all()
    return [{"date": str(r.invoice_date), "vendor": r.vendor, "amount": float(r.amount)} for r in rows]


async def _rebate_rows(days: int) -> list[dict]:
    since = date.today() - timedelta(days=days)
    async with get_async_session() as session:
        result = await session.execute(
            select(Rebate)
            .where(and_(Rebate.store_id == settings.store_id, Rebate.rebate_date >= since))
            .order_by(Rebate.rebate_date.desc())
        )
        rows = result.scalars().all()
    return [{"date": str(r.rebate_date), "vendor": r.vendor, "amount": float(r.amount)} for r in rows]


async def _revenue_rows(days: int) -> list[dict]:
    since = date.today() - timedelta(days=days)
    async with get_async_session() as session:
        result = await session.execute(
            select(Revenue)
            .where(and_(Revenue.store_id == settings.store_id, Revenue.revenue_date >= since))
            .order_by(Revenue.revenue_date.desc())
        )
        rows = result.scalars().all()
    return [{"date": str(r.revenue_date), "category": r.category, "amount": float(r.amount)} for r in rows]


# ---------------------------------------------------------------------------
# LangChain tools (sync wrappers — safe to call from run_in_executor thread)
# ---------------------------------------------------------------------------

@tool
def query_sales(days: int = 30) -> str:
    """Query daily sales records. days: how many days back to look (default 30)."""
    rows = asyncio.run(_sales_rows(days))
    return json.dumps(rows) if rows else "No sales records found for that period."


@tool
def query_expenses(days: int = 30, category: str = "") -> str:
    """Query expense records. days: how many days back. category: optional keyword filter."""
    rows = asyncio.run(_expense_rows(days, category))
    return json.dumps(rows) if rows else "No expenses found for that period."


@tool
def query_invoices(days: int = 30, vendor: str = "") -> str:
    """Query vendor invoice records. days: how many days back. vendor: optional vendor name filter."""
    rows = asyncio.run(_invoice_rows(days, vendor))
    return json.dumps(rows) if rows else "No invoices found for that period."


@tool
def query_rebates(days: int = 30) -> str:
    """Query rebate records. days: how many days back to look (default 30)."""
    rows = asyncio.run(_rebate_rows(days))
    return json.dumps(rows) if rows else "No rebates found for that period."


@tool
def query_revenue(days: int = 30) -> str:
    """Query revenue/profit records taken home. days: how many days back (default 30)."""
    rows = asyncio.run(_revenue_rows(days))
    return json.dumps(rows) if rows else "No revenue records found for that period."


_QUERY_TOOLS = [query_sales, query_expenses, query_invoices, query_rebates, query_revenue]
_TOOLS_BY_NAME = {t.name: t for t in _QUERY_TOOLS}

_SYSTEM = (
    "You are a helpful assistant for a gas station / convenience store owner. "
    "You have tools to query the store database for sales, expenses, invoices, rebates, and revenue. "
    "Answer the owner's question concisely using the data you retrieve. "
    "Format dollar amounts like $1,234.56. Keep replies short — this is a Telegram chat. "
    "Today is {today}. Store: {store_name}."
)


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
        SystemMessage(content=_SYSTEM.format(today=date.today(), store_name=settings.store_name)),
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
