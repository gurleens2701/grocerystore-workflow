"""
Telegram tools for sending messages and alerts.
"""

import asyncio
from typing import Any

from telegram import Bot
from telegram.constants import ParseMode

from config.settings import settings
from config.store_context import get_active_store


def _bot() -> Bot:
    return Bot(token=settings.telegram_bot_token)


def _active_store_profile() -> tuple[str, str]:
    """Return (store_name, chat_id) for the active store, failing closed on chat."""
    sid = get_active_store(required=False)
    if not sid:
        return "Store", ""
    try:
        from sqlalchemy import select
        from db.database import get_sync_session
        from db.models import Store
        with get_sync_session() as session:
            row = session.execute(
                select(Store.store_name, Store.chat_id).where(
                    Store.store_id == sid,
                    Store.is_active == True,
                )
            ).first()
            if row:
                return row.store_name or "Store", row.chat_id
    except Exception:
        pass
    return "Store", ""


async def _send(text: str, parse_mode: str = ParseMode.MARKDOWN) -> None:
    _, chat_id = _active_store_profile()
    if not chat_id:
        raise RuntimeError("No active store chat configured")
    async with _bot() as bot:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
        )


def send_message(text: str) -> str:
    """Send a plain text message via Telegram."""
    asyncio.run(_send(text, parse_mode=None))
    return "Message sent"


def send_daily_report(sales_data: dict[str, Any]) -> str:
    """Send a formatted daily sales report via Telegram matching the manual daily sheet."""
    d = sales_data.get("date", "N/A")
    dow = sales_data.get("day_of_week", "")

    # --- Product departments (LEFT TOP) ---
    depts = sales_data.get("departments", [])
    dept_lines = "\n".join(
        f"  {row['name']:<18} `${row['sales']:>8.2f}`"
        for row in depts
    )

    product_sales = sales_data.get("product_sales", 0)

    # --- Other items (LEFT BOTTOM) ---
    lotto_in = sales_data.get("lotto_in", 0)
    lotto_online = sales_data.get("lotto_online", 0)
    sales_tax = sales_data.get("sales_tax", 0)
    gpi = sales_data.get("gpi", 0)
    refunds = sales_data.get("refunds", 0)
    grand_total = sales_data.get("grand_total", 0)

    # --- Payments (RIGHT) ---
    cash = sales_data.get("cash", sales_data.get("cash_sales", 0))
    card = sales_data.get("card", sales_data.get("card_sales", 0))
    check = sales_data.get("check", 0)
    lotto_payout = sales_data.get("lotto_payout", 0)
    atm = sales_data.get("atm", 0)
    ebt = sales_data.get("ebt", 0)
    txns = sales_data.get("total_transactions", 0)
    store_name, _ = _active_store_profile()

    msg = (
        f"*📊 {store_name} Daily — {dow} {d}*\n"
        f"{'─' * 32}\n"
        f"\n*PRODUCT SALES*\n"
        f"{dept_lines}\n"
        f"{'─' * 32}\n"
        f"  {'TOTAL':<18} `${product_sales:>8.2f}`\n"
        f"\n*OTHER*\n"
        f"  {'IN. LOTTO':<18} `${lotto_in:>8.2f}`\n"
        f"  {'ON. LINE':<18} `${lotto_online:>8.2f}`\n"
        f"  {'SALES TAX':<18} `${sales_tax:>8.2f}`\n"
        f"  {'GPI':<18} `${gpi:>8.2f}`\n"
    )
    if refunds:
        msg += f"  {'REFUNDS':<18} `-${refunds:>7.2f}`\n"
    msg += (
        f"{'─' * 32}\n"
        f"  {'GRAND TOTAL':<18} `${grand_total:>8.2f}`\n"
        f"\n*PAYMENTS*\n"
        f"  {'CASH':<18} `${cash:>8.2f}`\n"
        f"  {'C.CARD':<18} `${card:>8.2f}`\n"
    )
    if check:
        msg += f"  {'CHECK':<18} `${check:>8.2f}`\n"
    if ebt:
        msg += f"  {'FOOD STAMP':<18} `${ebt:>8.2f}`\n"
    if lotto_payout:
        msg += f"  {'LOTTO PAYOUT':<18} `${lotto_payout:>8.2f}`\n"
    if atm:
        msg += f"  {'ATM':<18} `${atm:>8.2f}`\n"
    msg += f"\n  Baskets: `{txns}`"

    asyncio.run(_send(msg))
    return f"Daily report sent for {d}"


def send_low_stock_alert(inventory_data: dict[str, Any]) -> str:
    """Send a low stock alert via Telegram."""
    items = [i for i in inventory_data.get("items", []) if i.get("low_stock")]
    if not items:
        return "No low stock items — alert not sent"

    lines = "\n".join(
        f"  • {i.get('name', 'Unknown')} (SKU: {i.get('sku', '?')}) — Qty: {i.get('quantity', '?')}"
        for i in items
    )
    msg = f"*Low Stock Alert*\n\n{lines}"
    asyncio.run(_send(msg))
    return f"Low stock alert sent for {len(items)} items"


def send_bank_alert(balance_data: dict[str, Any], threshold: float = 5000.0) -> str:
    """Send an alert if any bank account balance is below the threshold."""
    low_accounts = [
        a for a in balance_data.get("accounts", [])
        if float(a.get("available", 999999)) < threshold
    ]

    if not low_accounts:
        return "Bank balances OK — no alert needed"

    lines = "\n".join(
        f"  • {a.get('name', 'Unknown')}: `${a.get('available', '?')}` available"
        for a in low_accounts
    )
    msg = f"*Low Bank Balance Alert*\n\n{lines}\n\n_Threshold: ${threshold:,.2f}_"
    asyncio.run(_send(msg))
    return f"Bank alert sent for {len(low_accounts)} account(s)"


def send_error_alert(error: str, context: str = "") -> str:
    """Send an error notification via Telegram."""
    msg = f"*Agent Error*\n\n`{error}`"
    if context:
        msg += f"\n\nContext: {context}"
    asyncio.run(_send(msg))
    return "Error alert sent"
