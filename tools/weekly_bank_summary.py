"""
Weekly bank summary — sent every Sunday evening via Telegram.

Covers the past 7 days:
  - Invoices cleared (bank payments matched to invoices)
  - Invoices still pending (logged but no matching bank payment)
  - Rebates deposited
  - Expenses paid
  - Payroll paid
  - Bank balance
"""

import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, select

from config.settings import settings
from db.database import get_async_session
from db.models import BankTransaction, Invoice, Expense, Rebate

log = logging.getLogger(__name__)


async def build_weekly_bank_summary(store_id: str) -> str:
    """Build a plain-text weekly bank summary for the last 7 days."""
    today = date.today()
    week_ago = today - timedelta(days=7)

    async with get_async_session() as session:
        # ── Invoices cleared (bank txns matched to invoices this week) ──
        cleared_rows = (await session.execute(
            select(BankTransaction).where(and_(
                BankTransaction.store_id == store_id,
                BankTransaction.transaction_date >= week_ago,
                BankTransaction.is_matched == True,
                BankTransaction.reconcile_type == "invoice",
            )).order_by(BankTransaction.transaction_date)
        )).scalars().all()

        # ── Invoices pending (logged this month, no matching bank txn) ──
        month_start = today.replace(day=1)
        all_invoices = (await session.execute(
            select(Invoice).where(and_(
                Invoice.store_id == store_id,
                Invoice.invoice_date >= month_start,
            )).order_by(Invoice.invoice_date)
        )).scalars().all()
        # An invoice is pending if it has no matched bank transaction
        matched_invoice_ids = {bt.matched_invoice_id for bt in cleared_rows if bt.matched_invoice_id}
        # Also check all-time matched invoices for this month
        all_matched = (await session.execute(
            select(BankTransaction.matched_invoice_id).where(and_(
                BankTransaction.store_id == store_id,
                BankTransaction.matched_invoice_id.isnot(None),
            ))
        )).scalars().all()
        all_matched_ids = set(all_matched)
        pending_invoices = [inv for inv in all_invoices if inv.id not in all_matched_ids]

        # ── Rebates deposited (bank txns categorized as rebate this week) ──
        rebate_rows = (await session.execute(
            select(BankTransaction).where(and_(
                BankTransaction.store_id == store_id,
                BankTransaction.transaction_date >= week_ago,
                BankTransaction.reconcile_type == "rebate",
            )).order_by(BankTransaction.transaction_date)
        )).scalars().all()

        # ── Expenses paid via bank this week ──
        expense_rows = (await session.execute(
            select(BankTransaction).where(and_(
                BankTransaction.store_id == store_id,
                BankTransaction.transaction_date >= week_ago,
                BankTransaction.reconcile_type == "expense",
            )).order_by(BankTransaction.transaction_date)
        )).scalars().all()

        # ── Payroll via bank this week ──
        payroll_rows = (await session.execute(
            select(BankTransaction).where(and_(
                BankTransaction.store_id == store_id,
                BankTransaction.transaction_date >= week_ago,
                BankTransaction.reconcile_type == "payroll",
            )).order_by(BankTransaction.transaction_date)
        )).scalars().all()

        # ── Unmatched transactions ──
        unmatched_rows = (await session.execute(
            select(BankTransaction).where(and_(
                BankTransaction.store_id == store_id,
                BankTransaction.transaction_date >= week_ago,
                BankTransaction.is_matched == False,
                BankTransaction.review_status == "pending",
            ))
        )).scalars().all()

    # ── Get bank balance ──
    balance_text = ""
    try:
        from tools.plaid_tools import is_connected, fetch_accounts
        if await is_connected(store_id):
            accounts = await fetch_accounts(store_id)
            if accounts:
                lines = []
                for a in accounts:
                    lines.append(f"  {a['name']}: ${a['current']:,.2f}")
                balance_text = "\n".join(lines)
    except Exception as e:
        log.warning("Failed to fetch bank balance for weekly summary: %s", e)

    # ── Build message ──
    parts = [f"Weekly Bank Summary ({week_ago.strftime('%b %d')} - {today.strftime('%b %d')})"]
    parts.append("")

    # Invoices cleared
    if cleared_rows:
        total = sum(float(r.amount) for r in cleared_rows)
        parts.append(f"Invoices Cleared: {len(cleared_rows)} totaling ${total:,.2f}")
        for r in cleared_rows:
            sub = r.reconcile_subcategory or r.description[:30]
            parts.append(f"  {r.transaction_date.strftime('%m/%d')} - {sub}: ${float(r.amount):,.2f}")
    else:
        parts.append("Invoices Cleared: None this week")

    parts.append("")

    # Invoices pending
    if pending_invoices:
        total = sum(float(inv.amount) for inv in pending_invoices)
        parts.append(f"Invoices Pending Payment: {len(pending_invoices)} totaling ${total:,.2f}")
        for inv in pending_invoices[:10]:
            parts.append(f"  {inv.invoice_date.strftime('%m/%d')} - {inv.vendor}: ${float(inv.amount):,.2f}")
        if len(pending_invoices) > 10:
            parts.append(f"  ...and {len(pending_invoices) - 10} more")
    else:
        parts.append("Invoices Pending Payment: All caught up!")

    parts.append("")

    # Rebates
    if rebate_rows:
        total = sum(abs(float(r.amount)) for r in rebate_rows)
        parts.append(f"Rebates Deposited: {len(rebate_rows)} totaling ${total:,.2f}")
        for r in rebate_rows:
            sub = r.reconcile_subcategory or r.description[:30]
            parts.append(f"  {r.transaction_date.strftime('%m/%d')} - {sub}: ${abs(float(r.amount)):,.2f}")
    else:
        parts.append("Rebates Deposited: None this week")

    parts.append("")

    # Expenses
    if expense_rows:
        total = sum(float(r.amount) for r in expense_rows)
        parts.append(f"Expenses Paid: {len(expense_rows)} totaling ${total:,.2f}")
        for r in expense_rows:
            sub = r.reconcile_subcategory or r.description[:30]
            parts.append(f"  {r.transaction_date.strftime('%m/%d')} - {sub}: ${float(r.amount):,.2f}")
    else:
        parts.append("Expenses Paid: None this week")

    parts.append("")

    # Payroll
    if payroll_rows:
        total = sum(float(r.amount) for r in payroll_rows)
        parts.append(f"Payroll: {len(payroll_rows)} totaling ${total:,.2f}")
        for r in payroll_rows:
            sub = r.reconcile_subcategory or r.description[:30]
            parts.append(f"  {r.transaction_date.strftime('%m/%d')} - {sub}: ${float(r.amount):,.2f}")
    else:
        parts.append("Payroll: None this week")

    parts.append("")

    # Unmatched
    if unmatched_rows:
        parts.append(f"Needs Review: {len(unmatched_rows)} unmatched transactions")
    else:
        parts.append("Needs Review: Everything is matched!")

    # Balance
    if balance_text:
        parts.append("")
        parts.append("Bank Balance:")
        parts.append(balance_text)

    return "\n".join(parts)


async def send_weekly_bank_summary(store_id: str, bot, chat_id: str) -> None:
    """Called by the scheduler every Sunday at 6 PM."""
    try:
        from tools.plaid_tools import is_connected
        if not await is_connected(store_id):
            log.info("Skipping weekly bank summary — bank not connected for %s", store_id)
            return

        summary = await build_weekly_bank_summary(store_id)
        await bot.send_message(chat_id=chat_id, text=summary, parse_mode=None)
        log.info("Sent weekly bank summary for %s", store_id)
    except Exception as e:
        log.error("Weekly bank summary failed for %s: %s", store_id, e, exc_info=True)
