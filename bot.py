"""
Interactive Telegram bot for Moraine Foodmart daily sheet.

Daily flow:
  1. Scheduler fires at 7 AM → fetches NRS data → sends LEFT SIDE to Telegram
  2. Bot asks owner for right-side numbers (lotto payout, ATM, coupon, etc.)
  3. Owner replies → bot builds complete sheet, calculates over/short
  4. Bot sends the final complete daily sheet
  5. Bot logs everything to Google Sheets
"""

import asyncio
import base64
import io
import logging
import re
from datetime import date, timedelta
from typing import Any

import anthropic
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config.settings import settings
from db.ops import log_message, save_daily_sales, save_expense, save_invoice, save_invoice_items, save_rebate, save_revenue, save_vendor_price
from db.state import clear_state, get_state, save_state
from tools.intent_router import classify_message
from tools.invoice_extractor import extract_invoice_from_photo
from tools.nrs_tools import fetch_daily_sales, fetch_inventory
from tools.price_lookup import compile_order, lookup_item_price, parse_order_list
from tools.health_score import _build_health_score_async, send_weekly_health_score
from tools.query_agent import answer_query
from tools.reports import save_daily_report
from tools.vendor_agent import get_vendor_comparison
from tools.sheets_tools import (
    log_cogs_entry, log_daily_sales, log_expense, log_inventory,
    log_rebate, log_revenue, log_transactions, resolve_vendor,
)

log = logging.getLogger(__name__)

# Conversation state
AWAITING_RIGHT_SIDE = 1

# PostgreSQL state keys
_STATE_SALES = "sales"
_STATE_INVOICE_ITEMS = "invoice_items"  # pending extracted line items awaiting user confirmation


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_left(sales: dict[str, Any]) -> str:
    """Build the left-side of the daily sheet (product sales + other)."""
    depts = sales.get("departments", [])
    dept_lines = "\n".join(
        f"  {d['name']:<20} ${d['sales']:>8.2f}"
        for d in depts
    )
    product_sales = sales.get("product_sales", 0)
    lotto_in = sales.get("lotto_in", 0)
    lotto_online = sales.get("lotto_online", 0)
    sales_tax = sales.get("sales_tax", 0)
    gpi = sales.get("gpi", 0)
    refunds = sales.get("refunds", 0)
    # Grand total does NOT subtract refunds (matches manual sheet)
    grand_total = round(product_sales + lotto_in + lotto_online + sales_tax + gpi, 2)
    sales["grand_total"] = grand_total  # update in-place

    lines = [
        f"📊 *Moraine Foodmart — {sales['day_of_week']} {sales['date']}*",
        "─" * 34,
        "",
        "*PRODUCT SALES*",
        f"```\n{dept_lines}",
        f"{'─'*34}",
        f"  {'TOTAL':<20} ${product_sales:>8.2f}\n```",
        "",
        "*OTHER*",
        "```",
        f"  {'IN. LOTTO':<20} ${lotto_in:>8.2f}",
        f"  {'ON. LINE':<20} ${lotto_online:>8.2f}",
        f"  {'SALES TAX':<20} ${sales_tax:>8.2f}",
        f"  {'GPI':<20} ${gpi:>8.2f}",
        "─" * 34,
        f"  {'GRAND TOTAL':<20} ${grand_total:>8.2f}",
        "```",
    ]
    if refunds:
        lines.append(f"_ℹ️ Refunds on record: ${refunds:.2f}_")

    # NRS-sourced payment fields (auto-filled)
    atm = sales.get("atm", 0)
    pull_tab = sales.get("pull_tab", 0)
    coupon = sales.get("coupon", 0)
    loyalty = sales.get("loyalty", 0)
    vendor = sales.get("vendor", 0)

    auto_lines = []
    if atm:
        auto_lines.append(f"  {'ATM':<20} ${atm:>8.2f}")
    if pull_tab:
        auto_lines.append(f"  {'PULL TAB':<20} ${pull_tab:>8.2f}")
    if coupon:
        auto_lines.append(f"  {'COUPON':<20} ${coupon:>8.2f}")
    if loyalty:
        auto_lines.append(f"  {'LOYALTY/ALTRIA':<20} ${loyalty:>8.2f}")
    if vendor:
        auto_lines.append(f"  {'VENDOR PAYOUT':<20} ${vendor:>8.2f}")

    if auto_lines:
        lines += ["", "*FROM NRS (auto)*", "```"] + auto_lines + ["```"]

    lines += [
        "",
        f"💵 Cash Drop: *${sales.get('cash_drops', 0):.2f}*   💳 Card: *${sales.get('card', 0):.2f}*",
        f"Baskets: *{sales.get('total_transactions', 0)}*",
    ]
    return "\n".join(lines)


def _prompt_for_right_side() -> str:
    return (
        "\n\n📋 *Please reply with the right-side numbers:*\n"
        "_(Enter 0 if none)_\n\n"
        "```\n"
        "LOTTO PO:    \n"
        "LOTTO CR:    \n"
        "FOOD STAMP:  \n"
        "```"
    )


def _build_complete_sheet(sales: dict[str, Any], right: dict[str, float]) -> str:
    """Build the full daily sheet with over/short."""
    product_sales = sales.get("product_sales", 0)
    lotto_in = sales.get("lotto_in", 0)
    lotto_online = sales.get("lotto_online", 0)
    sales_tax = sales.get("sales_tax", 0)
    gpi = sales.get("gpi", 0)
    grand_total = sales.get("grand_total", 0)  # already calculated

    # From NRS API
    cash = sales.get("cash_drops", 0)  # safe drop, not total cash received
    card = sales.get("card", 0)
    check = sales.get("check", 0)
    atm = sales.get("atm", 0)
    pull_tab = sales.get("pull_tab", 0)
    coupon = sales.get("coupon", 0)
    loyalty = sales.get("loyalty", 0)
    vendor = sales.get("vendor", 0)

    # From user input
    lotto_po = right.get("lotto_po", 0)
    lotto_cr = right.get("lotto_cr", 0)
    food_stamp = right.get("food_stamp", 0)

    total_right = round(
        cash + card + check + lotto_po + lotto_cr + atm
        + coupon + pull_tab + food_stamp + loyalty + vendor, 2
    )
    diff = round(total_right - grand_total, 2)
    if diff > 0:
        over_short = f"OVER  +${diff:.2f} 🟢"
    elif diff < 0:
        over_short = f"SHORT -${abs(diff):.2f} 🔴"
    else:
        over_short = "EVEN  $0.00 ✅"

    # Left side
    depts = sales.get("departments", [])
    dept_lines = "\n".join(
        f"  {d['name']:<20} ${d['sales']:>8.2f}"
        for d in depts
    )

    def r(label: str, val: float) -> str:
        if val == 0:
            return f"  {label:<20} {'—':>9}"
        return f"  {label:<20} ${val:>8.2f}"

    msg = (
        f"✅ *COMPLETE — {sales['day_of_week']} {sales['date']}*\n"
        f"```\n"
        f"{'─'*34}\n"
        f"  PRODUCT SALES\n"
        f"{dept_lines}\n"
        f"{'─'*34}\n"
        f"  {'TOTAL':<20} ${product_sales:>8.2f}\n"
        f"\n"
        f"  IN. LOTTO            ${lotto_in:>8.2f}\n"
        f"  ON. LINE             ${lotto_online:>8.2f}\n"
        f"  SALES TAX            ${sales_tax:>8.2f}\n"
        f"  GPI                  ${gpi:>8.2f}\n"
        f"{'─'*34}\n"
        f"  GRAND TOTAL          ${grand_total:>8.2f}\n"
        f"\n"
        f"  PAYMENTS\n"
        f"{r('LOTTO P.O', lotto_po)}\n"
        f"{r('LOTTO CR.', lotto_cr)}\n"
        f"{r('ATM', atm)}\n"
        f"  {'CASH DROP':<20} ${cash:>8.2f}\n"
        f"  {'CHECK':<20} ${check:>8.2f}\n"
        f"  {'C.CARD':<20} ${card:>8.2f}\n"
        f"{r('COUPON', coupon)}\n"
        f"{r('PULL TAB', pull_tab)}\n"
        f"{r('FOOD STAMP', food_stamp)}\n"
        f"{r('LOYALTY', loyalty)}\n"
        f"{r('VENDOR PAYOUT', vendor)}\n"
        f"{'─'*34}\n"
        f"  TOTAL PAYMENTS       ${total_right:>8.2f}\n"
        f"{'─'*34}\n"
        f"  {over_short}\n"
        f"```"
    )
    return msg


def _parse_right_side(text: str) -> dict[str, float] | None:
    """
    Parse user's right-side input for the 3 manual fields.
    Accepts formats like:
      LOTTO PO: 132
      lotto cr 31
      food stamp: 0
    Or a plain list of 3 numbers (in order: lotto_po, lotto_cr, food_stamp).
    Returns None if parsing fails.
    """
    def to_float(s: str) -> float:
        try:
            return round(float(s.replace(",", "").replace("$", "")), 2)
        except ValueError:
            return 0.0

    keys_map = {
        "lotto_po": ["lotto po", "lotto p.o", "lottopo", "lotto payout", "payout"],
        "lotto_cr": ["lotto cr", "lottocr", "lotto credit"],
        "food_stamp": ["food stamp", "foodstamp", "ebt", "food stamps"],
    }

    result: dict[str, float] = {}
    text_lower = text.lower()

    for key, aliases in keys_map.items():
        for alias in aliases:
            # Match "alias: 123" or "alias 123"
            pattern = re.escape(alias) + r"[\s:]*(\d+\.?\d*)"
            m = re.search(pattern, text_lower)
            if m:
                result[key] = to_float(m.group(1))
                break
        if key not in result:
            result[key] = 0.0

    # If no key matched at all, try plain number list (3 numbers)
    if not any(result.values()):
        nums = re.findall(r"\d+\.?\d*", text)
        if len(nums) == 3:
            order = ["lotto_po", "lotto_cr", "food_stamp"]
            result = {k: to_float(v) for k, v in zip(order, nums)}
        else:
            return None

    return result


# ---------------------------------------------------------------------------
# Bot handlers
# ---------------------------------------------------------------------------

async def _do_daily_fetch(bot: Bot, chat_id: str) -> bool:
    """Fetch NRS data, send left side + prompt. Returns True on success."""
    try:
        await bot.send_message(chat_id=chat_id, text="⏳ Fetching today's data from NRS...", parse_mode=None)
        sales = await asyncio.get_event_loop().run_in_executor(None, fetch_daily_sales)
        await save_state(settings.store_id, _STATE_SALES, sales)

        msg = _fmt_left(sales) + _prompt_for_right_side()
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(log_message(settings.store_id, "telegram", "bot", "Bot", msg))
        return True
    except Exception as e:
        log.error("Daily fetch failed: %s", e, exc_info=True)
        err = f"❌ Error fetching data: {e}"
        await bot.send_message(chat_id=chat_id, text=err, parse_mode=None)
        asyncio.create_task(log_message(settings.store_id, "telegram", "bot", "Bot", err))
        return False


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /daily command — triggers the fetch and starts the conversation."""
    ok = await _do_daily_fetch(context.bot, settings.telegram_chat_id)
    return AWAITING_RIGHT_SIDE if ok else ConversationHandler.END


async def receive_right_side(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the user's reply with right-side numbers."""
    text = update.message.text.strip()
    sales = await get_state(settings.store_id, _STATE_SALES)

    if not sales:
        await update.message.reply_text(
            "No pending daily sheet. Send /daily to start.", parse_mode=None
        )
        return ConversationHandler.END

    right = _parse_right_side(text)
    if right is None:
        await update.message.reply_text(
            "⚠️ Could not parse numbers. Please use format:\n"
            "LOTTO PO: 39\nLOTTO CR: 0\nFOOD STAMP: 0",
            parse_mode=None,
        )
        return AWAITING_RIGHT_SIDE  # stay in state, ask again

    # Send complete daily sheet
    sheet_msg = _build_complete_sheet(sales, right)
    await update.message.reply_text(sheet_msg, parse_mode=ParseMode.MARKDOWN)

    # Log to Google Sheets
    await update.message.reply_text("📊 Logging to Google Sheets...", parse_mode=None)
    try:
        sales_for_sheet = dict(sales)
        sales_for_sheet.update(right)

        log_daily_sales(sales_for_sheet)
        await save_daily_sales(settings.store_id, sales, right)
        save_daily_report(settings.store_id, sales, right)

        txns = [
            {"type": "sale", "department": d["name"], "items": d["items"],
             "amount": d["sales"], "payment_method": "mixed", "date": sales["date"]}
            for d in sales.get("departments", [])
        ]
        log_transactions(txns, sales["date"])

        inv = await asyncio.get_event_loop().run_in_executor(None, fetch_inventory)
        log_inventory(inv)

        await update.message.reply_text("✅ Logged to Google Sheets.", parse_mode=None)
    except Exception as e:
        log.error("Sheets logging failed: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Sheets logging failed: {e}", parse_mode=None)

    await clear_state(settings.store_id, _STATE_SALES)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the current conversation."""
    await clear_state(settings.store_id, _STATE_SALES)
    await update.message.reply_text("Cancelled.", parse_mode=None)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Scheduler-triggered daily fetch (called from main.py)
# ---------------------------------------------------------------------------

async def scheduled_daily(app: Application) -> None:
    """Called by the scheduler at 7 AM. Fetches data and sends left side."""
    bot = app.bot
    ok = await _do_daily_fetch(bot, settings.telegram_chat_id)
    if ok:
        # Set conversation state so the next message is treated as right-side input
        # We store a dummy update to prime the ConversationHandler
        log.info("Scheduled daily fetch complete — waiting for right-side input via Telegram.")


# ---------------------------------------------------------------------------
# Invoice / COGS handlers
# ---------------------------------------------------------------------------

def _extract_invoice_fields(text: str) -> dict[str, Any] | None:
    """
    Use Claude Haiku to extract vendor, amount, and date from natural language.
    Falls back to regex for structured inputs like 'mclane $2100 3/14'.
    """
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{
                "role": "user",
                "content": (
                    "Extract the vendor name, dollar amount, and date from this message. "
                    "The vendor is a company or store name (like Pepsi, McLane, Heidelburg, Roma). "
                    "Ignore filler like 'log it', 'Google Sheet', 'in March', etc. "
                    f"Message: \"{text}\"\n\n"
                    "Reply in exactly this format (no extra text):\n"
                    "VENDOR: <name>\nAMOUNT: <number>\nDATE: <YYYY-MM-DD>\n"
                    "If a field is missing, write UNKNOWN."
                ),
            }],
        )
        result: dict = {}
        for line in msg.content[0].text.strip().splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                result[key.strip().upper()] = val.strip()

        vendor = result.get("VENDOR", "UNKNOWN")
        if not vendor or vendor == "UNKNOWN":
            return _parse_invoice_text_regex(text)

        try:
            amount = float(result.get("AMOUNT", "0").replace("$", "").replace(",", ""))
        except ValueError:
            m = re.search(r"\$?(\d+(?:\.\d{1,2})?)", text)
            amount = float(m.group(1)) if m else 0.0

        raw_date = result.get("DATE", "UNKNOWN")
        entry_date = date.today()
        if raw_date and raw_date != "UNKNOWN":
            try:
                entry_date = date.fromisoformat(raw_date)
            except ValueError:
                pass

        if amount == 0.0:
            return None
        return {"vendor": vendor, "amount": amount, "entry_date": entry_date}

    except Exception:
        return _parse_invoice_text_regex(text)


def _parse_invoice_text_regex(text: str) -> dict[str, Any] | None:
    """Regex fallback for structured inputs like: mclane $2100 3/14"""
    amount_match = re.search(r"\$?(\d+(?:\.\d{1,2})?)", text)
    if not amount_match:
        return None
    amount = float(amount_match.group(1))

    date_match = re.search(
        r"(\d{4}-\d{2}-\d{2})|(\d{1,2}/\d{1,2}(?:/\d{2,4})?)", text
    )
    entry_date = date.today()
    if date_match:
        raw = date_match.group(0)
        try:
            if "-" in raw:
                entry_date = date.fromisoformat(raw)
            else:
                parts = raw.split("/")
                m, d = int(parts[0]), int(parts[1])
                y = int(parts[2]) if len(parts) == 3 else date.today().year
                if y < 100:
                    y += 2000
                entry_date = date(y, m, d)
        except (ValueError, IndexError):
            pass

    vendor_text = re.sub(r"\$?\d+(?:\.\d{1,2})?", "", text)
    vendor_text = re.sub(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?", "", vendor_text)
    vendor_text = re.sub(r"[:\-]", " ", vendor_text)
    vendor = " ".join(vendor_text.split()).title()

    if not vendor:
        return None
    return {"vendor": vendor, "amount": amount, "entry_date": entry_date}


async def _extract_invoice_via_claude(photo_bytes: bytes) -> dict[str, Any] | None:
    """Send invoice photo to Claude vision and extract vendor/amount/date."""
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    b64 = base64.standard_b64encode(photo_bytes).decode()

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                },
                {
                    "type": "text",
                    "text": (
                        "This is a vendor invoice or receipt for a convenience store. "
                        "Extract: vendor/company name, total amount due, and invoice date. "
                        "Reply in exactly this format (no extra text):\n"
                        "VENDOR: <name>\nAMOUNT: <number>\nDATE: <YYYY-MM-DD>\n"
                        "If you cannot find a field, write UNKNOWN for it."
                    ),
                },
            ],
        }],
    )

    text = msg.content[0].text.strip()
    result: dict[str, Any] = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip().upper()] = val.strip()

    vendor = result.get("VENDOR", "")
    if not vendor or vendor == "UNKNOWN":
        return None

    try:
        amount = float(result.get("AMOUNT", "0").replace("$", "").replace(",", ""))
    except ValueError:
        amount = 0.0

    try:
        entry_date = date.fromisoformat(result.get("DATE", ""))
    except ValueError:
        entry_date = date.today()

    return {"vendor": vendor, "amount": amount, "entry_date": entry_date}


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — show all available features."""
    msg = (
        "Here is everything I can do for you:\n"
        "\n"
        "DAILY SALES\n"
        "I automatically send your sales numbers every morning at 7AM.\n"
        "You reply with 3 numbers (Lotto PO, Lotto Credit, Food Stamp) to complete the sheet.\n"
        "Or type: daily report\n"
        "\n"
        "INVOICES\n"
        "Take a photo of any vendor invoice and send it — I will read it and log it.\n"
        "Or send it as a file for better accuracy.\n"
        "Or just type: Pepsi $300 March 22\n"
        "Command: /invoice Pepsi 300 3/22\n"
        "\n"
        "EXPENSES\n"
        "Type things like:\n"
        "  electricity $340 March 10\n"
        "  rent $2500\n"
        "  payroll $1200 this week\n"
        "\n"
        "QUESTIONS — ask me anything\n"
        "  How much did I make this week?\n"
        "  What did I spend on McLane last month?\n"
        "  Who is my cheapest vendor for cigarettes?\n"
        "  What was my best day in March?\n"
        "  How much cash did I drop this month?\n"
        "  Do I owe Roma anything?\n"
        "\n"
        "VENDOR PRICES\n"
        "  /price marlboro red\n"
        "  /vendors — compare all vendors by category\n"
        "\n"
        "ORDERS\n"
        "  /order chips water energy drinks\n"
        "  Or type: I need to order Marlboro and Pepsi\n"
        "  I will tell you the cheapest vendor for each item.\n"
        "\n"
        "HEALTH SCORE\n"
        "  Type: health score\n"
        "  Or: how is my store doing\n"
        "  I send you a weekly score every Monday morning.\n"
        "\n"
        "CASH FLOW\n"
        "  Type: cash flow March\n"
        "  I will pull all your sales and expenses and give you a full monthly summary.\n"
        "\n"
        "VOICE MESSAGES\n"
        "  Send me a voice message in any language — I will understand and reply.\n"
        "  Set your language: /language hindi\n"
        "  Available: Hindi, Gujarati, Punjabi, Spanish, Arabic, Urdu, Bengali,\n"
        "             Chinese, Korean, Vietnamese, Portuguese, French, English\n"
        "\n"
        "GOOGLE SHEET\n"
        "  Everything is automatically saved to your Google Sheet.\n"
        "  You can also edit the sheet directly — I sync changes every night.\n"
        "\n"
        "WEB DASHBOARD\n"
        "  View reports, invoices, vendor prices, and health score at:\n"
        f"  http://178.104.61.18\n"
        "\n"
        "SYNC\n"
        "  /sync — manually pull latest changes from your Google Sheet right now.\n"
        "\n"
        "Just talk to me naturally — you do not need to use commands."
    )
    await update.message.reply_text(msg, parse_mode=None)


async def cmd_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/invoice vendor amount date — log a COGS purchase directly."""
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text(
            "Usage: /invoice <vendor> <amount> <date>\n"
            "Example: /invoice Heidelburg 500 3/9\n\n"
            "Or just send a photo of the invoice.",
            parse_mode=None,
        )
        return

    parsed = await asyncio.get_event_loop().run_in_executor(None, _extract_invoice_fields, text)
    if not parsed:
        await update.message.reply_text(
            "⚠️ Could not parse. Try: /invoice Heidelburg 500 3/9", parse_mode=None
        )
        return

    try:
        vendor = parsed["vendor"]
        amount = parsed["amount"]
        entry_date = parsed["entry_date"]
        invoice_id = await save_invoice(settings.store_id, vendor, amount, entry_date)
        await save_vendor_price(settings.store_id, vendor, amount, entry_date, invoice_id)
        result = log_cogs_entry(vendor=vendor, amount=amount, entry_date=entry_date)
        await update.message.reply_text(f"✅ {result}", parse_mode=None)
    except Exception as e:
        log.error("COGS log failed: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Failed to log: {e}", parse_mode=None)


async def cmd_vendors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/vendors [category] — show vendor price comparison from invoice history."""
    category = " ".join(context.args).upper() if context.args else None
    await update.message.reply_text("📊 Pulling vendor price data...", parse_mode=None)
    try:
        report = await asyncio.get_event_loop().run_in_executor(
            None, get_vendor_comparison, category
        )
        await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.error("Vendor comparison failed: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Error: {e}", parse_mode=None)


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/price <item name> — look up item price across all vendors."""
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text(
            "Usage: /price <item name>\n"
            "Example: /price marlboro red short\n"
            "         /price coke 20oz\n"
            "         /price black mild ft",
            parse_mode=None,
        )
        return

    await update.message.reply_text(f"🔍 Looking up {query}...", parse_mode=None)
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, lookup_item_price, query
        )
        await update.message.reply_text(result, parse_mode=None)
    except Exception as e:
        log.error("Price lookup failed: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Error: {e}", parse_mode=None)


async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/order — compile an order. Send item list as command args or follow-up message."""
    # Check if items were included inline: /order item1, item2, item3
    text = " ".join(context.args) if context.args else ""

    if not text:
        await update.message.reply_text(
            "Send your order list with quantities — one item per line:\n\n"
            "Example:\n"
            "marlboro red short x5\n"
            "coke 20oz x10\n"
            "doritos nacho x3\n"
            "black mild ft sweet x2\n\n"
            "_I'll show totals per vendor, flag missing items, and suggest the cheapest option._",
            parse_mode=ParseMode.MARKDOWN,
        )
        # Save a flag so next message is treated as the order list
        await save_state(settings.store_id, "awaiting_order", {"pending": True})
        return

    await _process_order(update, text)


async def _process_order(update: Update, text: str) -> None:
    """Parse item list and return order grouped by cheapest vendor."""
    items = parse_order_list(text)
    if not items:
        await update.message.reply_text(
            "⚠️ Could not parse item list. Send one item per line.",
            parse_mode=None,
        )
        return

    await update.message.reply_text(
        f"🔍 Finding cheapest vendors for {len(items)} items...", parse_mode=None
    )
    try:
        summary = await asyncio.get_event_loop().run_in_executor(
            None, compile_order, items
        )
        await update.message.reply_text(summary, parse_mode=None)
    except Exception as e:
        log.error("Order compilation failed: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Error: {e}", parse_mode=None)


def _parse_entry(text: str) -> dict | None:
    """
    Parse amount, date, and label from a free-form entry.
    Works for expenses, rebates, and revenues.
    Returns {"label": str, "amount": float, "entry_date": date} or None.
    """
    import re as _re
    amount_match = _re.search(r"\$?(\d+(?:\.\d{1,2})?)", text)
    if not amount_match:
        return None
    amount = float(amount_match.group(1))

    date_match = _re.search(
        r"(\d{4}-\d{2}-\d{2})|(\d{1,2}/\d{1,2}(?:/\d{2,4})?)", text
    )
    entry_date = date.today()
    if date_match:
        raw = date_match.group(0)
        try:
            if "-" in raw:
                entry_date = date.fromisoformat(raw)
            else:
                parts = raw.split("/")
                m, d = int(parts[0]), int(parts[1])
                y = int(parts[2]) if len(parts) == 3 else date.today().year
                if y < 100:
                    y += 2000
                entry_date = date(y, m, d)
        except (ValueError, IndexError):
            pass

    # Label = everything except the amount and date
    label_text = _re.sub(r"\$?\d+(?:\.\d{1,2})?", "", text)
    label_text = _re.sub(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?", "", label_text)
    label_text = _re.sub(r"\b(rebate|expense|revenue|profit|took|home|paid|payment)\b", "", label_text, flags=_re.IGNORECASE)
    label = " ".join(label_text.split()).strip(" :-")

    if not label:
        return None
    return {"label": label, "amount": amount, "entry_date": entry_date}


async def _handle_expense(update: Update, text: str) -> None:
    parsed = _parse_entry(text)
    if not parsed:
        await update.message.reply_text("⚠️ Could not parse. Try: electricity $340 march 10", parse_mode=None)
        return
    try:
        await save_expense(settings.store_id, parsed["label"], parsed["amount"], parsed["entry_date"])
        result = log_expense(parsed["label"], parsed["amount"], parsed["entry_date"])
        await update.message.reply_text(
            f"✅ *Expense logged*\n{parsed['label'].title()} — ${parsed['amount']:.2f} on {parsed['entry_date']}",
            parse_mode=ParseMode.MARKDOWN,
        )
        log.info(result)
    except Exception as e:
        log.error("Expense log failed: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Failed: {e}", parse_mode=None)


async def _handle_rebate(update: Update, text: str) -> None:
    parsed = _parse_entry(text)
    if not parsed:
        await update.message.reply_text("⚠️ Could not parse. Try: pmhelix rebate $820", parse_mode=None)
        return
    try:
        await save_rebate(settings.store_id, parsed["label"], parsed["amount"], parsed["entry_date"])
        result = log_rebate(parsed["label"], parsed["amount"], parsed["entry_date"])
        await update.message.reply_text(
            f"✅ *Rebate logged*\n{parsed['label'].title()} — ${parsed['amount']:.2f} on {parsed['entry_date']}",
            parse_mode=ParseMode.MARKDOWN,
        )
        log.info(result)
    except Exception as e:
        log.error("Rebate log failed: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Failed: {e}", parse_mode=None)


async def _handle_revenue(update: Update, text: str) -> None:
    parsed = _parse_entry(text)
    if not parsed:
        await update.message.reply_text("⚠️ Could not parse. Try: car payment $300", parse_mode=None)
        return
    try:
        await save_revenue(settings.store_id, parsed["label"], parsed["amount"], parsed["entry_date"])
        result = log_revenue(parsed["label"], parsed["amount"], parsed["entry_date"])
        await update.message.reply_text(
            f"✅ *Revenue logged*\n{parsed['label'].title()} — ${parsed['amount']:.2f} on {parsed['entry_date']}",
            parse_mode=ParseMode.MARKDOWN,
        )
        log.info(result)
    except Exception as e:
        log.error("Revenue log failed: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Failed: {e}", parse_mode=None)


async def _handle_invoice_text(update: Update, text: str) -> None:
    parsed = await asyncio.get_event_loop().run_in_executor(None, _extract_invoice_fields, text)
    if not parsed:
        await update.message.reply_text("⚠️ Could not parse invoice. Try: mclane $2100 3/14", parse_mode=None)
        return

    vendor_match = resolve_vendor(parsed["vendor"])
    if not vendor_match:
        words = parsed["vendor"].split()
        for i in range(len(words), 0, -1):
            vendor_match = resolve_vendor(" ".join(words[:i]))
            if vendor_match:
                break

    if not vendor_match:
        await update.message.reply_text(
            f"⚠️ Vendor *{parsed['vendor']}* not recognised.\n"
            f"Use /invoice to log or check vendor name.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        invoice_id = await save_invoice(settings.store_id, vendor_match, parsed["amount"], parsed["entry_date"])
        await save_vendor_price(settings.store_id, vendor_match, parsed["amount"], parsed["entry_date"], invoice_id)
        result = log_cogs_entry(vendor=vendor_match, amount=parsed["amount"], entry_date=parsed["entry_date"])
        await update.message.reply_text(
            f"✅ *Invoice logged*\nVendor: {vendor_match}\nAmount: ${parsed['amount']:.2f}\nDate: {parsed['entry_date']}",
            parse_mode=ParseMode.MARKDOWN,
        )
        log.info(result)
    except Exception as e:
        log.error("Invoice log failed: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Failed: {e}", parse_mode=None)


async def handle_plain_text_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Main free-text handler. Routes all incoming messages via Claude Haiku intent classifier.
    Priority 1: pending daily sheet → treat as right-side numbers.
    Priority 2: intent classification → expense / rebate / revenue / invoice / query.
    """
    text = update.message.text.strip()
    sender = update.effective_user.first_name or "Owner"
    asyncio.create_task(log_message(settings.store_id, "telegram", "user", sender, text))

    # ── Help shortcut — catch /help, \\help, //help, "help" etc ─────────────
    clean = text.lstrip("/\\").lower().strip()
    if clean in ("help", "help me", "what can you do", "features"):
        await cmd_help(update, context)
        return

    # ── Priority 1: pending daily sheet ─────────────────────────────────────
    sales = await get_state(settings.store_id, _STATE_SALES)
    if sales:
        right = _parse_right_side(text)
        if right is not None:
            sheet_msg = _build_complete_sheet(sales, right)
            await update.message.reply_text(sheet_msg, parse_mode=ParseMode.MARKDOWN)
            await update.message.reply_text("📊 Logging to Google Sheets...", parse_mode=None)
            try:
                sales_for_sheet = dict(sales)
                sales_for_sheet.update(right)
                log_daily_sales(sales_for_sheet)
                await save_daily_sales(settings.store_id, sales, right)
                save_daily_report(settings.store_id, sales, right)
                await update.message.reply_text("✅ Logged to Google Sheets.", parse_mode=None)
            except Exception as e:
                log.error("Sheets logging failed: %s", e, exc_info=True)
                await update.message.reply_text(f"⚠️ Sheets logging failed: {e}", parse_mode=None)
            await clear_state(settings.store_id, _STATE_SALES)
            return
        else:
            await update.message.reply_text(
                "⚠️ Could not parse. Please reply with:\nLOTTO PO: 16\nLOTTO CR: 0\nFOOD STAMP: 27.97",
                parse_mode=None,
            )
            return

    # ── Priority 2: pending invoice items confirmation ───────────────────────
    pending_items = await get_state(settings.store_id, _STATE_INVOICE_ITEMS)
    if pending_items:
        answer = text.strip().upper()
        if answer in ("YES", "Y", "SAVE", "OK", "CONFIRM"):
            await update.message.reply_text("💾 Saving items to price database...", parse_mode=None)
            try:
                from datetime import date as _date
                vendor = pending_items.get("vendor", "UNKNOWN")
                raw_date = pending_items.get("invoice_date", "")
                try:
                    inv_date = _date.fromisoformat(raw_date) if raw_date else _date.today()
                except ValueError:
                    inv_date = _date.today()
                inv_num = pending_items.get("invoice_number", "")

                # Save invoice header
                invoice_id = await save_invoice(
                    settings.store_id, vendor, 0, inv_date, inv_num
                )
                # Save line items
                count = await save_invoice_items(
                    settings.store_id,
                    vendor,
                    pending_items.get("items", []),
                    inv_date,
                    invoice_id,
                )
                await clear_state(settings.store_id, _STATE_INVOICE_ITEMS)
                await update.message.reply_text(
                    f"✅ Saved {count} items from {vendor} invoice ({inv_date}).\n"
                    "Use /price <item> to look up prices anytime.",
                    parse_mode=None,
                )
            except Exception as e:
                log.error("Saving invoice items failed: %s", e, exc_info=True)
                await update.message.reply_text(f"⚠️ Save failed: {e}", parse_mode=None)
        elif answer in ("NO", "N", "DISCARD", "CANCEL"):
            await clear_state(settings.store_id, _STATE_INVOICE_ITEMS)
            await update.message.reply_text("🗑️ Invoice discarded.", parse_mode=None)
        else:
            await update.message.reply_text(
                "Reply *YES* to save the extracted items or *NO* to discard.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # ── Priority 3: pending order list ──────────────────────────────────────
    awaiting_order = await get_state(settings.store_id, "awaiting_order")
    if awaiting_order:
        await clear_state(settings.store_id, "awaiting_order")
        await _process_order(update, text)
        return

    # ── Priority 4: daily fetch starts the state machine; everything else → unified agent ──
    intent = await asyncio.get_event_loop().run_in_executor(None, classify_message, text)
    log.info("Intent: %s | %s", intent, text[:60])

    if intent == "daily_fetch":
        await _do_daily_fetch(context.bot, settings.telegram_chat_id)
    else:
        from tools.main_agent import run_agent
        try:
            reply = await asyncio.get_event_loop().run_in_executor(None, run_agent, text, settings.store_id)
            await update.message.reply_text(reply, parse_mode=None)
            asyncio.create_task(log_message(settings.store_id, "telegram", "bot", "Bot", reply))
        except Exception as e:
            log.error("Agent failed: %s", e, exc_info=True)
            await update.message.reply_text(f"⚠️ Something went wrong: {e}", parse_mode=None)


def _fmt_extracted_items(result: dict) -> str:
    """Format extracted invoice items for user review."""
    vendor = result.get("vendor", "Unknown")
    inv_date = result.get("invoice_date", "")
    inv_num = result.get("invoice_number", "")
    items = result.get("items", [])

    header = f"📋 *{vendor}*"
    if inv_date:
        header += f"  |  {inv_date}"
    if inv_num:
        header += f"  |  #{inv_num}"

    low_confidence = [i for i in items if float(i.get("confidence", 1)) < 0.85]

    lines = [header, ""]
    for i, item in enumerate(items, 1):
        canonical = item.get("canonical_name") or item.get("item_name") or item.get("item_name_raw", "?")
        raw = item.get("item_name_raw", "")
        price = item.get("unit_price", 0)
        confidence = float(item.get("confidence", 1))
        flag = " ⚠️" if confidence < 0.85 else ""
        # Show raw name if it differs from canonical
        name_str = canonical
        if raw and raw.upper() != canonical.upper():
            name_str = f"{canonical} _{raw}_"
        lines.append(f"{i}. {name_str}  —  ${float(price):.2f}/unit{flag}")

    lines += ["", f"*{len(items)} items found.*"]

    if low_confidence:
        lines.append(f"⚠️ *{len(low_confidence)} item(s) flagged* — AI was unsure about the name. Review above.")

    lines.append("Reply *YES* to save to price database, or *NO* to discard.")
    return "\n".join(lines)


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/language [name] — set preferred language for voice transcription."""
    from tools.voice import SUPPORTED_LANGUAGES

    if not context.args:
        current = (await get_state(settings.store_id, "language_pref")) or {}
        lang = current.get("name", "auto (English)")
        lang_list = ", ".join(k.title() for k in SUPPORTED_LANGUAGES if k != "auto")
        await update.message.reply_text(
            f"Current language: {lang}\n\nAvailable: {lang_list}, Auto\n\nUsage: /language hindi",
            parse_mode=None,
        )
        return

    name = " ".join(context.args).lower().strip()
    if name not in SUPPORTED_LANGUAGES:
        await update.message.reply_text(
            f"Language '{name}' not recognised. Try: hindi, gujarati, punjabi, spanish, arabic, urdu, auto",
            parse_mode=None,
        )
        return

    code = SUPPORTED_LANGUAGES[name]
    await save_state(settings.store_id, "language_pref", {"name": name.title(), "code": code})
    if name == "auto":
        await update.message.reply_text("Language set to auto-detect.", parse_mode=None)
    else:
        await update.message.reply_text(f"Language set to {name.title()}. Voice messages will be transcribed in {name.title()}.", parse_mode=None)


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Telegram voice messages — transcribe with Whisper, then run through agent."""
    from tools.voice import transcribe_voice
    from tools.main_agent import run_agent

    sender = update.effective_user.first_name or "Owner"
    asyncio.create_task(log_message(settings.store_id, "telegram", "user", sender, "🎤 Voice message"))

    # Get language preference
    lang_pref = await get_state(settings.store_id, "language_pref") or {}
    lang_code = lang_pref.get("code")  # None = auto-detect

    await update.message.reply_text("🎤 Listening...", parse_mode=None)

    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        audio_bytes = bytes(await file.download_as_bytearray())

        # Transcribe
        text = await asyncio.get_event_loop().run_in_executor(
            None, transcribe_voice, audio_bytes, lang_code
        )

        if not text:
            await update.message.reply_text("Could not understand the audio. Please try again.", parse_mode=None)
            return

        log.info("Voice transcribed: %s", text[:100])
        asyncio.create_task(log_message(settings.store_id, "telegram", "user", sender, f"🎤 {text}"))

        # Check if this is a pending daily sheet response
        sales = await get_state(settings.store_id, _STATE_SALES)
        if sales:
            right = _parse_right_side(text)
            if right is not None:
                sheet_msg = _build_complete_sheet(sales, right)
                await update.message.reply_text(sheet_msg, parse_mode=ParseMode.MARKDOWN)
                try:
                    sales_for_sheet = dict(sales)
                    sales_for_sheet.update(right)
                    log_daily_sales(sales_for_sheet)
                    await save_daily_sales(settings.store_id, sales, right)
                    save_daily_report(settings.store_id, sales, right)
                    await update.message.reply_text("Logged to Google Sheets.", parse_mode=None)
                except Exception as e:
                    await update.message.reply_text(f"Sheets logging failed: {e}", parse_mode=None)
                await clear_state(settings.store_id, _STATE_SALES)
                return

        # Otherwise run through unified agent
        intent = await asyncio.get_event_loop().run_in_executor(None, classify_message, text)
        if intent == "daily_fetch":
            await _do_daily_fetch(context.bot, settings.telegram_chat_id)
        else:
            reply = await asyncio.get_event_loop().run_in_executor(None, run_agent, text, settings.store_id)
            asyncio.create_task(log_message(settings.store_id, "telegram", "bot", "Bot", reply))

            # Send voice reply + text reply
            try:
                from tools.voice import text_to_speech
                audio = await asyncio.get_event_loop().run_in_executor(
                    None, text_to_speech, reply
                )
                await update.message.reply_voice(
                    voice=io.BytesIO(audio),
                    caption=reply,
                )
            except Exception as tts_err:
                log.warning("TTS failed, falling back to text: %s", tts_err)
                await update.message.reply_text(reply, parse_mode=None)

    except ValueError as e:
        # Missing API key
        await update.message.reply_text(str(e), parse_mode=None)
    except Exception as e:
        log.error("Voice handler failed: %s", e, exc_info=True)
        await update.message.reply_text(f"Could not process voice message: {e}", parse_mode=None)


async def handle_invoice_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle a photo or document message — extract line items via Claude Opus vision.
    Accepts photos (Telegram-compressed) and documents (full quality, better OCR).
    """
    sender = update.effective_user.first_name or "Owner"
    asyncio.create_task(log_message(settings.store_id, "telegram", "user", sender, "📸 Sent an invoice photo"))
    await update.message.reply_text("📸 Reading invoice, this may take a few seconds...", parse_mode=None)

    try:
        # Prefer document (uncompressed) over photo (Telegram-compressed)
        if update.message.document:
            file = await context.bot.get_file(update.message.document.file_id)
        else:
            photo = update.message.photo[-1]  # largest compressed size
            file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()

        result = await asyncio.get_event_loop().run_in_executor(
            None, extract_invoice_from_photo, bytes(photo_bytes)
        )

        if result.get("error"):
            await update.message.reply_text(
                f"⚠️ Could not read invoice: {result['error']}\n"
                "Try: /invoice Vendor 500 3/9",
                parse_mode=None,
            )
            return

        items = result.get("items", [])
        if not items:
            # Fall back: at least log the total if we got vendor info
            vendor = result.get("vendor", "")
            if vendor:
                await update.message.reply_text(
                    f"⚠️ Found vendor *{vendor}* but no line items extracted.\n"
                    "Try: /invoice Vendor 500 3/9 to log the total manually.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await update.message.reply_text(
                    "⚠️ Could not extract items from this invoice.\n"
                    "Try: /invoice Vendor 500 3/9",
                    parse_mode=None,
                )
            return

        # Save to pending state and ask for confirmation
        await save_state(settings.store_id, _STATE_INVOICE_ITEMS, result)
        msg = _fmt_extracted_items(result)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        log.error("Invoice photo failed: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Error: {e}", parse_mode=None)


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sync — manually trigger the nightly Sheets → DB sync right now."""
    from tools.sync import run_nightly_sync
    await update.message.reply_text("🔄 Syncing Google Sheets → database...", parse_mode=None)
    try:
        await run_nightly_sync(settings.store_id)
        await update.message.reply_text("✅ Sync complete. You can now query sales, expenses, and more.", parse_mode=None)
    except Exception as e:
        log.error("Manual sync failed: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Sync failed: {e}", parse_mode=None)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/health — show last week's health score report."""
    await update.message.reply_text("📊 Calculating weekly health score...", parse_mode=None)
    try:
        await send_weekly_health_score(settings.store_id, context.bot, settings.telegram_chat_id)
    except Exception as e:
        log.error("Health score command failed: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Error: {e}", parse_mode=None)


# ---------------------------------------------------------------------------
# Build and return the Application
# ---------------------------------------------------------------------------

def build_app() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("daily", cmd_daily)],
        states={
            AWAITING_RIGHT_SIDE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_right_side)
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        # Allow re-entry so the scheduler can prime it
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("invoice", cmd_invoice))
    app.add_handler(CommandHandler("vendors", cmd_vendors))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("order", cmd_order))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_invoice_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_invoice_photo))
    # Plain-text invoice entries (outside conversation) e.g. "heidelburg 500 3/9"
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text_invoice))
    return app
