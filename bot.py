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
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
from tools.nrs_tools import fetch_daily_sales, fetch_inventory, save_cached_token
from tools.price_lookup import compile_order, lookup_item_price, parse_order_list
from tools.health_score import _build_health_score_async, send_weekly_health_score
from tools.onboarding import (
    ONBOARDING_STEP_NAME, ONBOARDING_STEP_LANG,
    ONBOARDING_STEP_BACKOFFICE, ONBOARDING_STEP_BANK,
    get_user_profile, is_onboarding_complete,
    onboarding_bank, onboarding_backoffice,
    onboarding_language, onboarding_name, onboarding_start,
)
from tools.query_agent import answer_query
from tools.reports import save_daily_report
from tools.vendor_agent import get_vendor_comparison
from tools.sheets_tools import (
    log_cogs_entry, log_daily_sales, log_expense, log_inventory,
    log_rebate, log_revenue, log_transactions, mark_invoice_paid, resolve_vendor,
)

log = logging.getLogger(__name__)

# Conversation state
AWAITING_RIGHT_SIDE = 1

# PostgreSQL state keys
_STATE_SALES = "sales"
_STATE_INVOICE_ITEMS = "invoice_items"   # pending extracted line items awaiting user confirmation
_STATE_AWAITING_REPORT = "awaiting_daily_report"  # manual mode: waiting for the report photo
_STATE_REPORT_DRAFT = "daily_report_draft"        # manual mode: OCR done, some fields still missing
_STATE_CHAT_HISTORY = "chat_history"              # rolling conversation history (last 20 messages)

_HISTORY_MAX = 20  # max messages to keep (10 back-and-forth exchanges)


async def _load_history(store_id: str) -> list[dict]:
    """Load conversation history from DB state."""
    return await get_state(store_id, _STATE_CHAT_HISTORY) or []


async def _save_history(store_id: str, history: list[dict],
                        user_text: str, bot_reply: str) -> None:
    """Append a user/assistant exchange to history and persist (capped at _HISTORY_MAX)."""
    history = list(history)
    history.append({"role": "user",      "content": user_text})
    history.append({"role": "assistant", "content": bot_reply})
    await save_state(store_id, _STATE_CHAT_HISTORY, history[-_HISTORY_MAX:])


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
        "\n\n📋 *Please reply with these numbers:*\n"
        "_(Enter 0 if none)_\n\n"
        "```\n"
        "IN. LOTTO:   \n"
        "ON. LINE:    \n"
        "LOTTO PO:    \n"
        "LOTTO CR:    \n"
        "FOOD STAMP:  \n"
        "```"
    )


def _build_complete_sheet(sales: dict[str, Any], right: dict[str, float]) -> str:
    """Build the full daily sheet with over/short."""
    product_sales = sales.get("product_sales", 0)
    # Prefer user-supplied values over NRS auto values
    lotto_in     = right.get("lotto_in",    sales.get("lotto_in",    0))
    lotto_online = right.get("lotto_online", sales.get("lotto_online", 0))
    sales_tax = sales.get("sales_tax", 0)
    gpi = sales.get("gpi", 0)
    # Recalculate grand total using the (possibly overridden) lotto values
    grand_total = round(product_sales + lotto_in + lotto_online + sales_tax + gpi, 2)

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


def _parse_sales_edit(text: str, sales: dict) -> dict | None:
    """
    Use Claude Haiku to extract field updates from natural language.
    Returns {field: value, ...} or None if nothing recognised.
    Blocking — call via run_in_executor.
    """
    import json as _json
    import anthropic as _anthropic

    all_fields = [
        "lotto_in", "lotto_online", "sales_tax", "gpi", "product_sales",
        "lotto_po", "lotto_cr", "food_stamp", "cash_drops", "card",
        "check", "atm", "pull_tab", "coupon", "loyalty", "vendor",
    ]
    current = {k: round(float(sales.get(k) or 0), 2) for k in all_fields}

    prompt = f"""Daily sales report editor for a gas station.

Current values (field: current_value):
{_json.dumps(current, indent=2)}

Field aliases:
  lotto_in       = instant lottery sales (IN. LOTTO)
  lotto_online   = online lottery sales (ON. LINE)
  sales_tax      = sales tax collected
  gpi            = GPI / fee buster
  product_sales  = product / dept total (TOTAL)
  lotto_po       = lottery payout to customers (LOTTO P.O)
  lotto_cr       = lottery credit / net lotto (LOTTO CR.)
  food_stamp     = food stamp / EBT / SNAP
  cash_drops     = cash drop to safe (CASH DROP)
  card           = credit/debit card total (C.CARD)
  check          = check payments
  atm            = ATM cashback
  pull_tab       = pull tab payouts
  coupon         = coupons
  loyalty        = loyalty / altria
  vendor         = vendor payout

User says: "{text}"

If the user is changing one or more values, return ONLY a JSON object with the fields to update and their new values.
If the user is asking a question, greeting, or not changing any value, return {{}}.
No explanation. Only JSON.

Examples:
"change instant lotto to 150" → {{"lotto_in": 150}}
"lotto payout 500 food stamp 200" → {{"lotto_po": 500, "food_stamp": 200}}
"card was 1300 not 1299" → {{"card": 1300}}
"lotto credit 0" → {{"lotto_cr": 0}}
"what's my total?" → {{}}"""

    try:
        client = _anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        updates = _json.loads(raw)
        if not isinstance(updates, dict) or not updates:
            return None
        return {k: float(v) for k, v in updates.items() if k in all_fields}
    except Exception as e:
        log.warning("Sales edit parse failed: %s", e)
        return None


def _build_preview(sales: dict) -> str:
    """Build a live preview of the complete daily sheet from the current sales state."""
    right = {
        "lotto_in":    sales.get("lotto_in",    0),
        "lotto_online": sales.get("lotto_online", 0),
        "lotto_po":    sales.get("lotto_po",    0),
        "lotto_cr":    sales.get("lotto_cr",    0),
        "food_stamp":  sales.get("food_stamp",  0),
    }
    return _build_complete_sheet(sales, right)


def _parse_right_side(text: str) -> dict[str, float] | None:
    """
    Parse user's right-side input for the 5 manual fields.
    Accepts formats like:
      IN. LOTTO: 208
      lotto po: 132
      food stamp: 0
    Or a plain list of 5 numbers (in order: lotto_in, lotto_online, lotto_po, lotto_cr, food_stamp).
    Returns None if parsing fails.
    """
    def to_float(s: str) -> float:
        try:
            return round(float(s.replace(",", "").replace("$", "")), 2)
        except ValueError:
            return 0.0

    keys_map = {
        "lotto_in":     ["in. lotto", "in lotto", "instant lotto", "instant lottery", "in.lotto"],
        "lotto_online": ["on. line", "on line", "online lotto", "online lottery", "online"],
        "lotto_po":     ["lotto po", "lotto p.o", "lottopo", "lotto payout", "payout"],
        "lotto_cr":     ["lotto cr", "lottocr", "lotto credit", "lotto cr."],
        "food_stamp":   ["food stamp", "foodstamp", "ebt", "food stamps", "snap"],
    }

    result: dict[str, float] = {}
    text_lower = text.lower()

    for key, aliases in keys_map.items():
        for alias in aliases:
            pattern = re.escape(alias) + r"[\s:]*(\d+\.?\d*)"
            m = re.search(pattern, text_lower)
            if m:
                result[key] = to_float(m.group(1))
                break

    # If no key matched at all, try plain number list (5 or 3 numbers)
    if not result:
        nums = re.findall(r"\d+\.?\d*", text)
        if len(nums) == 5:
            order = ["lotto_in", "lotto_online", "lotto_po", "lotto_cr", "food_stamp"]
            result = {k: to_float(v) for k, v in zip(order, nums)}
        elif len(nums) == 3:
            # Legacy: just the 3 original fields
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

        # Save daily report summary to history so follow-up questions have context
        summary = (
            f"I sent the daily sales report for {sales.get('date', 'today')}. "
            f"NRS data: product_sales=${sales.get('product_sales', 0):.2f}, "
            f"instant_lotto=${sales.get('lotto_in', 0):.2f}, "
            f"online_lotto=${sales.get('lotto_online', 0):.2f}, "
            f"sales_tax=${sales.get('sales_tax', 0):.2f}, "
            f"gpi=${sales.get('gpi', 0):.2f}, "
            f"grand_total=${sales.get('grand_total', 0):.2f}, "
            f"cash_drop=${sales.get('cash_drop', 0):.2f}, "
            f"card=${sales.get('card', 0):.2f}, "
            f"atm=${sales.get('atm', 0):.2f}. "
            f"Still waiting for owner to enter: IN. LOTTO, ON. LINE, LOTTO PO, LOTTO CR, FOOD STAMP."
        )
        hist = await _load_history(settings.store_id)
        hist.append({"role": "assistant", "content": summary})
        await save_state(settings.store_id, _STATE_CHAT_HISTORY, hist[-_HISTORY_MAX:])
        return True
    except Exception as e:
        log.error("Daily fetch failed: %s", e, exc_info=True)
        err_text = str(e)
        if "token not captured" in err_text or "NRS authentication" in err_text or "401" in err_text:
            msg = (
                "❌ NRS login failed (reCAPTCHA blocked the auto-login).\n\n"
                "To fix — grab your token from the browser:\n"
                "1. Open https://mystore.nrsplus.com in Chrome and log in\n"
                "2. Press F12 → Network tab\n"
                "3. Look for any request to *pos-papi.nrsplus.com*\n"
                "4. Copy the long segment after .com/ that looks like `u56967-abc123...`\n\n"
                "Then send:\n`/token u56967-abc123...`\n\n"
                "After that, /daily will work again."
            )
        else:
            msg = f"❌ Error fetching data: {err_text}"
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(log_message(settings.store_id, "telegram", "bot", "Bot", msg))
        return False


async def _do_manual_daily_prompt(bot: Bot, chat_id: str) -> None:
    """Manual-mode: ask owner to send their daily report photo."""
    await save_state(settings.store_id, _STATE_AWAITING_REPORT, {"pending": True})
    msg = (
        "📋 Ready to log today's sales!\n\n"
        "Take a photo of your daily sales report and send it here.\n"
        "Or send it as a file for better accuracy.\n\n"
        "_I'll read all the numbers and fill in the sheet for you._"
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /daily command — triggers fetch (NRS) or photo prompt (manual mode)."""
    profile = await get_user_profile(settings.store_id)
    if profile.get("backoffice") == "manual":
        await _do_manual_daily_prompt(context.bot, settings.telegram_chat_id)
        return ConversationHandler.END
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
            "IN. LOTTO: 208\nON. LINE: 4\nLOTTO PO: 39\nLOTTO CR: 0\nFOOD STAMP: 0",
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
    """Called by the scheduler at 7 AM. Fetches data (NRS) or prompts for photo (manual)."""
    bot = app.bot
    profile = await get_user_profile(settings.store_id)
    if profile.get("backoffice") == "manual":
        await _do_manual_daily_prompt(bot, settings.telegram_chat_id)
        log.info("Scheduled daily prompt sent (manual mode) — waiting for report photo.")
    else:
        ok = await _do_daily_fetch(bot, settings.telegram_chat_id)
        if ok:
            log.info("Scheduled daily fetch complete — waiting for right-side input via Telegram.")

    # Bank sync + reconcile (if connected)
    from tools.plaid_tools import is_connected, sync_transactions, fetch_accounts
    try:
        if await is_connected(settings.store_id):
            result = await sync_transactions(settings.store_id)
            needs_review  = result.get("needs_review", [])
            auto_list     = result.get("auto_list", [])
            cc_mismatches = result.get("cc_mismatches", [])
            paid_invoices = result.get("paid_invoices", [])

            for inv in paid_invoices:
                try:
                    await _send_invoice_paid_alert(bot, inv)
                except Exception as e:
                    log.warning("Invoice paid alert failed: %s", e)

            for txn in needs_review[:5]:
                try:
                    await send_bank_review_request(bot, txn)
                except Exception as e:
                    log.warning("Review request failed for txn %s: %s", txn.get("id"), e)

            for txn in auto_list[:8]:
                try:
                    await send_bank_auto_review(bot, txn)
                except Exception as e:
                    log.warning("Auto review send failed for txn %s: %s", txn.get("id"), e)

            for mm in cc_mismatches:
                try:
                    await send_cc_mismatch_alert(bot, mm)
                except Exception as e:
                    log.warning("CC mismatch alert failed: %s", e)

            # ── Negative balance alert ────────────────────────────────────
            try:
                accounts = await fetch_accounts(settings.store_id)
                for acct in accounts:
                    balance = acct.get("available") if acct.get("available") is not None else acct.get("current", 0)
                    if balance < 0:
                        await bot.send_message(
                            chat_id=settings.telegram_chat_id,
                            text=(
                                f"🚨 *Negative Bank Balance*\n"
                                f"{acct['name']}: *${balance:,.2f}*\n\n"
                                "Check your account — you may have overdraft fees coming."
                            ),
                            parse_mode=ParseMode.MARKDOWN,
                        )
            except Exception as e:
                log.warning("Balance check failed: %s", e)

    except Exception as e:
        log.warning("Scheduled bank sync failed: %s", e)

    # ── Stale review reminder (transactions needing review > 2 days old) ──
    try:
        await _send_stale_review_reminder(bot)
    except Exception as e:
        log.warning("Stale review reminder failed: %s", e)


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

    # ── Onboarding gate — redirect new users before anything else ────────────
    if not await is_onboarding_complete(settings.store_id):
        await onboarding_start(update, context)
        return

    # ── Load owner profile (name, language) ─────────────────────────────────
    profile = await get_user_profile(settings.store_id)
    owner_name = profile.get("name", "")

    # ── Help shortcut — catch /help, \\help, //help, "help" etc ─────────────
    clean = text.lstrip("/\\").lower().strip()
    if clean in ("help", "help me", "what can you do", "features"):
        await cmd_help(update, context)
        return

    # ── Priority 0.5: pending bank transaction subcategory reply ────────────
    # Check if user just clicked an inline button that needs a follow-up text
    from db.state import get_state as _gs
    pending_confirm_keys = [
        k for k in (await _list_bank_confirm_keys(settings.store_id))
    ]
    if pending_confirm_keys:
        key = pending_confirm_keys[0]  # take first pending
        state = await get_state(settings.store_id, key)
        if state:
            from tools.bank_reconciler import confirm_transaction
            txn_id = state["txn_id"]
            reconcile_type = state["reconcile_type"]
            subcategory = text.strip()
            await clear_state(settings.store_id, key)
            result = await confirm_transaction(settings.store_id, txn_id, reconcile_type, subcategory, sender="user")
            if result:
                await update.message.reply_text(
                    f"✅ Logged as {reconcile_type}: {subcategory}\n"
                    f"I'll remember this for similar transactions in the future.",
                    parse_mode=None,
                )
            else:
                await update.message.reply_text("⚠️ Could not find that transaction.", parse_mode=None)
            return

    # ── Priority 1: pending daily sheet ─────────────────────────────────────
    sales = await get_state(settings.store_id, _STATE_SALES)
    if sales:
        clean_reply = text.strip().lower()

        # ── Save / confirm ───────────────────────────────────────────────────
        if clean_reply in ("ok", "confirm", "yes", "save", "good", "correct", "done", "log it", "log"):
            right = {
                "lotto_po":   sales.get("lotto_po",   sales.get("_ocr_lotto_po",   0)),
                "lotto_cr":   sales.get("lotto_cr",   sales.get("_ocr_lotto_cr",   0)),
                "food_stamp": sales.get("food_stamp", sales.get("_ocr_food_stamp", 0)),
            }
            preview = _build_complete_sheet(sales, right)
            await update.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN)
            await update.message.reply_text("📊 Logging to Google Sheets...", parse_mode=None)
            try:
                sales_for_sheet = dict(sales)
                sales_for_sheet.update(right)
                log_daily_sales(sales_for_sheet)
                await save_daily_sales(settings.store_id, sales, right)
                save_daily_report(settings.store_id, sales, right)
                await clear_state(settings.store_id, _STATE_SALES)
                await update.message.reply_text("✅ Logged to Google Sheets.", parse_mode=None)
            except Exception as e:
                log.error("Sheets logging failed: %s", e, exc_info=True)
                await update.message.reply_text(f"⚠️ Sheets logging failed: {e}", parse_mode=None)
            return

        # ── Structured right-side format (LOTTO PO: X / LOTTO CR: Y / FOOD STAMP: Z) ──
        right = _parse_right_side(text)
        if right is not None:
            sales.update(right)
            await save_state(settings.store_id, _STATE_SALES, sales)
            preview = _build_preview(sales)
            await update.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN)
            await update.message.reply_text("Reply *ok* to save, or change any number.", parse_mode=ParseMode.MARKDOWN)
            return

        # ── Natural language edit ("change card to 1300", "lotto payout 500") ──
        edits = await asyncio.get_event_loop().run_in_executor(
            None, _parse_sales_edit, text, sales
        )
        if edits:
            sales.update(edits)
            await save_state(settings.store_id, _STATE_SALES, sales)
            changed = ", ".join(f"{k}=${v:.2f}" for k, v in edits.items())
            preview = _build_preview(sales)
            await update.message.reply_text(
                f"Updated: {changed}\n\n" + preview,
                parse_mode=ParseMode.MARKDOWN,
            )
            await update.message.reply_text("Reply *ok* to save, or change any number.", parse_mode=ParseMode.MARKDOWN)
            return

        # ── Nothing matched — show current state as a reminder ───────────────
        preview = _build_preview(sales)
        await update.message.reply_text(
            preview + "\n\nChange any number (e.g. *lotto payout 500*) or reply *ok* to save.",
            parse_mode=ParseMode.MARKDOWN,
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

    # ── Priority 2.5: daily report draft — collecting missing fields ────────
    report_draft = await get_state(settings.store_id, _STATE_REPORT_DRAFT)
    if report_draft:
        missing     = report_draft.get("missing", [])
        extracted   = dict(report_draft.get("extracted", {}))
        departments = report_draft.get("departments", [])
        raw_date    = report_draft.get("report_date")
        try:
            report_date = date.fromisoformat(raw_date) if raw_date else date.today()
        except (ValueError, TypeError):
            report_date = date.today()

        # Try to parse labeled values first (e.g. "lotto po 45 lotto cr 0 food stamp 27")
        parsed_labeled: dict[str, float] = {}
        text_lower = text.lower()
        for field in missing:
            aliases = {
                "lotto_po":      ["lotto po", "lotto payout", "lotto p.o", "lottery payout"],
                "lotto_cr":      ["lotto cr", "lotto credit", "lottery credit"],
                "lotto_in":      ["instant lotto", "scratch", "lotto in"],
                "lotto_online":  ["online lotto", "lottery terminal", "keno"],
                "food_stamp":    ["food stamp", "ebt", "snap"],
                "product_sales": ["total", "product sales", "net sales"],
                "sales_tax":     ["sales tax", "tax"],
                "gpi":           ["gpi", "fee buster", "feebuster"],
                "cash_drop":     ["cash drop", "drop"],
                "card":          ["card", "credit", "debit"],
            }.get(field, [field.replace("_", " ")])
            for alias in aliases:
                m = re.search(re.escape(alias) + r"[\s:]*(\d+\.?\d*)", text_lower)
                if m:
                    parsed_labeled[field] = round(float(m.group(1)), 2)
                    break

        # Fall back to positional numbers if labeled parsing didn't cover everything
        still_missing_after_label = [f for f in missing if f not in parsed_labeled]
        if still_missing_after_label:
            nums = re.findall(r"\d+\.?\d*", text)
            for i, field in enumerate(still_missing_after_label):
                if i < len(nums):
                    parsed_labeled[field] = round(float(nums[i]), 2)

        # Merge parsed values into extracted
        for field, val in parsed_labeled.items():
            extracted[field] = val

        remaining_missing = [f for f in missing if extracted.get(f) is None]

        if remaining_missing:
            await update.message.reply_text(
                f"Still need: {', '.join(_FIELD_HUMAN.get(f, f) for f in remaining_missing)}\n"
                f"Reply with those values (in that order, labeled or plain numbers).",
                parse_mode=None,
            )
            # Update draft with what we've got so far
            report_draft["extracted"] = extracted
            report_draft["missing"] = remaining_missing
            await save_state(settings.store_id, _STATE_REPORT_DRAFT, report_draft)
            return

        await clear_state(settings.store_id, _STATE_REPORT_DRAFT)

        # All fields now filled — check if we still need lotto_po/lotto_cr
        lotto_po   = extracted.get("lotto_po") or 0
        lotto_cr   = extracted.get("lotto_cr") or 0
        food_stamp = extracted.get("food_stamp") or 0

        sales = _build_ocr_sales_dict(extracted, departments, report_date)
        right = {"lotto_po": lotto_po, "lotto_cr": lotto_cr, "food_stamp": food_stamp}

        sheet_msg = _build_complete_sheet(sales, right)
        await update.message.reply_text(sheet_msg, parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text("📊 Logging...", parse_mode=None)
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
        if profile.get("backoffice") == "manual":
            await _do_manual_daily_prompt(context.bot, settings.telegram_chat_id)
        else:
            await _do_daily_fetch(context.bot, settings.telegram_chat_id)
    else:
        from tools.main_agent import run_agent

        # Load conversation history for context
        history = await _load_history(settings.store_id)

        # If there's a pending daily report, include its data as context
        # so follow-up questions ("change lotto payout", "what was my card?") work
        pending_sales = await get_state(settings.store_id, _STATE_SALES)
        question = text
        if pending_sales:
            question = (
                f"[Context: There is a pending daily sales report for {pending_sales.get('date', 'today')} "
                f"waiting for LOTTO PO, LOTTO CR, and FOOD STAMP manual inputs. "
                f"NRS auto-filled: product_sales=${pending_sales.get('product_sales', 0):.2f}, "
                f"instant_lotto=${pending_sales.get('lotto_in', 0):.2f}, "
                f"online_lotto=${pending_sales.get('lotto_online', 0):.2f}, "
                f"sales_tax=${pending_sales.get('sales_tax', 0):.2f}, "
                f"gpi=${pending_sales.get('gpi', 0):.2f}, "
                f"grand_total=${pending_sales.get('grand_total', 0):.2f}, "
                f"cash_drop=${pending_sales.get('cash_drop', 0):.2f}, "
                f"card=${pending_sales.get('card', 0):.2f}. "
                f"To finalize the report, the owner must reply with: "
                f"IN. LOTTO: X / ON. LINE: X / LOTTO PO: X / LOTTO CR: Y / FOOD STAMP: Z]\n\n"
                f"Owner says: {text}"
            )

        try:
            reply = await asyncio.get_event_loop().run_in_executor(
                None, run_agent, question, settings.store_id, owner_name, history
            )
            await update.message.reply_text(reply, parse_mode=None)
            asyncio.create_task(log_message(settings.store_id, "telegram", "bot", "Bot", reply))
            # Save exchange to history (use original text, not injected context)
            asyncio.create_task(_save_history(settings.store_id, history, text, reply))
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

    # Onboarding gate
    if not await is_onboarding_complete(settings.store_id):
        await onboarding_start(update, context)
        return

    # Get language from user profile (set during onboarding), fall back to legacy pref
    profile = await get_user_profile(settings.store_id)
    lang_code = profile.get("language") or (
        (await get_state(settings.store_id, "language_pref") or {}).get("code")
    )
    if lang_code == "auto":
        lang_code = None

    await update.message.reply_text("🎤 Listening...", parse_mode=None)

    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id, read_timeout=60, write_timeout=60, connect_timeout=30)
        audio_bytes = bytes(await file.download_as_bytearray(read_timeout=60, write_timeout=60, connect_timeout=30))

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
                await update.message.reply_voice(voice=io.BytesIO(audio))
            except Exception as tts_err:
                log.warning("TTS failed, falling back to text: %s", tts_err)
                await update.message.reply_text(reply, parse_mode=None)

    except ValueError as e:
        # Missing API key
        await update.message.reply_text(str(e), parse_mode=None)
    except Exception as e:
        log.error("Voice handler failed: %s", e, exc_info=True)
        await update.message.reply_text(f"Could not process voice message: {e}", parse_mode=None)


def _build_ocr_sales_dict(extracted: dict, departments: list, report_date: date | None) -> dict:
    """Build a sales dict (compatible with _fmt_left/_build_complete_sheet) from OCR output."""
    report_date = report_date or date.today()
    return {
        "product_sales": extracted.get("product_sales") or 0,
        "lotto_in":      extracted.get("lotto_in") or 0,
        "lotto_online":  extracted.get("lotto_online") or 0,
        "sales_tax":     extracted.get("sales_tax") or 0,
        "gpi":           extracted.get("gpi") or 0,
        "refunds":       0,
        "departments":   departments,
        "day_of_week":   report_date.strftime("%A"),
        "date":          str(report_date),
        "cash_drops":    extracted.get("cash_drop") or 0,
        "card":          extracted.get("card") or 0,
        "check":         extracted.get("check") or 0,
        "atm":           extracted.get("atm") or 0,
        "pull_tab":      extracted.get("pull_tab") or 0,
        "coupon":        extracted.get("coupon") or 0,
        "loyalty":       extracted.get("loyalty") or 0,
        "vendor":        0,
        "total_transactions": 0,
        "source":        "manual_ocr",
        "_ocr_lotto_po":    extracted.get("lotto_po") or 0,
        "_ocr_lotto_cr":    extracted.get("lotto_cr") or 0,
        "_ocr_food_stamp":  extracted.get("food_stamp") or 0,
    }


_FIELD_HUMAN = {
    "product_sales": "product sales (TOTAL)",
    "lotto_in":      "instant lotto sales",
    "lotto_online":  "online lotto sales",
    "sales_tax":     "sales tax",
    "gpi":           "GPI",
    "cash_drop":     "cash drop",
    "card":          "card total",
    "food_stamp":    "food stamp / EBT",
    "lotto_po":      "Lotto Payout (cash paid to winners)",
    "lotto_cr":      "Lotto Credit (net lottery)",
}


def _fmt_ocr_summary(extracted: dict, departments: list, must_ask: list, report_date: date | None) -> str:
    """Format a summary of what OCR found, split by got vs need."""
    rd = report_date.strftime("%A %b %-d") if report_date else "today"
    lines = [f"📋 *POS Report — {rd}*", ""]

    if departments:
        lines.append("*Departments:*")
        lines.append("```")
        for d in departments:
            lines.append(f"  {d['name']:<22} ${d['sales']:>8.2f}")
        lines.append("```")
        lines.append("")

    show_fields = ["product_sales", "lotto_in", "lotto_online", "sales_tax", "gpi",
                   "cash_drop", "card", "check", "atm", "food_stamp"]
    lines.append("*From your report:*")
    lines.append("```")
    for f in show_fields:
        val = extracted.get(f)
        if val is not None and val != 0:
            label = _FIELD_HUMAN.get(f, f).upper()
            lines.append(f"  {label:<22} ${val:>8.2f}")
    lines.append("```")

    if must_ask:
        still_human = [_FIELD_HUMAN.get(f, f) for f in must_ask]
        lines.append("")
        lines.append(f"❓ *Still needed:*  {', '.join(still_human)}")

    return "\n".join(lines)


def _prompt_for_missing(must_ask: list) -> str:
    """Build a prompt asking only for the specific missing fields."""
    human = [_FIELD_HUMAN.get(f, f) for f in must_ask]
    lines = ["\n\n📝 *Please reply with the following:*", "_(Enter 0 if none)_", "", "```"]
    for h in human:
        lines.append(f"{h.upper():<28}")
    lines.append("```")
    return "\n".join(lines)


async def _handle_daily_report_photo(update, context, photo_bytes: bytes) -> None:
    """
    OCR a POS daily report photo.
    Shows extracted numbers, then asks ONLY for what's missing
    (lotto payout, lotto credit, and anything the OCR couldn't read).
    """
    from tools.report_ocr import extract_daily_report_from_photo

    await update.message.reply_text("📋 Reading your POS report...", parse_mode=None)

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, extract_daily_report_from_photo, photo_bytes
        )
    except Exception as e:
        log.error("Daily report OCR failed: %s", e, exc_info=True)
        await update.message.reply_text(
            f"⚠️ Could not read the report: {e}\n\n"
            "You can enter numbers manually — type /daily.",
            parse_mode=None,
        )
        return

    extracted   = result["extracted"]
    departments = result["departments"]
    must_ask    = result["must_ask"]
    report_date = result["report_date"]

    await clear_state(settings.store_id, _STATE_AWAITING_REPORT)

    summary = _fmt_ocr_summary(extracted, departments, must_ask, report_date)

    if must_ask:
        # Save draft and ask for specific missing fields
        draft = {
            "extracted":   extracted,
            "departments": departments,
            "missing":     must_ask,
            "report_date": str(report_date) if report_date else None,
        }
        await save_state(settings.store_id, _STATE_REPORT_DRAFT, draft)
        await update.message.reply_text(
            summary + _prompt_for_missing(must_ask),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # All fields found — build complete sheet immediately
    sales = _build_ocr_sales_dict(extracted, departments, report_date)
    right = {
        "lotto_po":   extracted.get("lotto_po") or 0,
        "lotto_cr":   extracted.get("lotto_cr") or 0,
        "food_stamp": extracted.get("food_stamp") or 0,
    }
    await save_state(settings.store_id, _STATE_SALES, sales)
    sheet_msg = _build_complete_sheet(sales, right)
    await update.message.reply_text(
        summary + "\n\n" + sheet_msg
        + "\n\n_Reply *ok* to log, or correct any numbers first._",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_invoice_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle a photo or document message — extract line items via Claude Opus vision.
    Accepts photos (Telegram-compressed) and documents (full quality, better OCR).
    """
    sender = update.effective_user.first_name or "Owner"
    asyncio.create_task(log_message(settings.store_id, "telegram", "user", sender, "📸 Sent a photo"))

    try:
        # Download photo bytes first — shared by both flows
        if update.message.document:
            file = await context.bot.get_file(update.message.document.file_id)
        else:
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
        photo_bytes = bytes(await file.download_as_bytearray())

        # ── Manual mode: daily report photo ──────────────────────────────────
        awaiting_report = await get_state(settings.store_id, _STATE_AWAITING_REPORT)
        if awaiting_report:
            await _handle_daily_report_photo(update, context, photo_bytes)
            return

        await update.message.reply_text("📸 Reading invoice, this may take a few seconds...", parse_mode=None)

        result = await asyncio.get_event_loop().run_in_executor(
            None, extract_invoice_from_photo, photo_bytes
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


async def _list_bank_confirm_keys(store_id: str) -> list[str]:
    """Return all state keys for pending bank transaction subcategory replies."""
    from sqlalchemy import select
    from db.database import get_async_session
    from db.models import PendingState
    async with get_async_session() as session:
        rows = (await session.execute(
            select(PendingState.state_key).where(
                PendingState.store_id == store_id,
                PendingState.state_key.like("bank_confirm_%"),
            )
        )).scalars().all()
    return list(rows)


# ── Bank review helpers ──────────────────────────────────────────────────────

_REVIEW_TYPES = [
    ("Vendor Invoice", "invoice"),
    ("Expense",        "expense"),
    ("CC Settlement",  "cc_settlement"),
    ("Rebate",         "rebate"),
    ("Payroll",        "payroll"),
    ("Skip / Fee",     "skip"),
]


async def _send_invoice_paid_alert(bot: Bot, inv: dict) -> None:
    """Notify owner that a vendor invoice has been confirmed paid by the bank."""
    text = (
        f"✅ *Invoice Paid — {inv['vendor']}*\n"
        f"  Amount: *${inv['amount']:,.2f}*\n"
        f"  Invoice date: {inv['invoice_date']}\n"
        f"  Bank cleared: {inv['bank_date']}\n"
        f"  Google Sheet cell marked green ✔"
    )
    await bot.send_message(
        chat_id=settings.telegram_chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
    )


async def send_bank_review_request(bot: Bot, txn: dict) -> None:
    """Send a single transaction review message with inline keyboard to the owner."""
    direction  = "OUT" if txn["amount"] > 0 else "IN"
    emoji      = "💸" if txn["amount"] > 0 else "💰"
    desc       = txn["description"][:40]
    amount     = abs(txn["amount"])
    sheet_match = txn.get("sheet_match")

    # --- Proposed check match: show Yes / No ---
    if sheet_match and sheet_match.get("proposed"):
        vendor     = sheet_match["vendor"]
        entry_date = sheet_match["entry_date"]
        match_type = sheet_match["match_type"]
        # Save match details in DB state so callback can retrieve them
        await save_state(settings.store_id, f"bank_match_{txn['id']}", {
            "match_type": match_type,
            "vendor":     vendor,
            "entry_date": str(entry_date),
        })
        text = (
            f"🔍 *Check match found*\n"
            f"{'─'*32}\n"
            f"  {txn['date']}  [{direction}]\n"
            f"  {desc}\n"
            f"  *${amount:,.2f}*\n\n"
            f"  Matches *{vendor}* {match_type} logged on {entry_date}\n"
            f"  Is this correct?"
        )
        keyboard = [[
            InlineKeyboardButton("✅ Yes", callback_data=f"bk_yes:{txn['id']}"),
            InlineKeyboardButton("❌ No, something else", callback_data=f"bk_no:{txn['id']}"),
        ]]
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # --- Unknown transaction: standard category keyboard ---
    ai_guess   = txn.get("ai_guess", "")
    confidence = txn.get("confidence", 0.0)
    hint = f"  AI guess: {ai_guess} ({confidence*100:.0f}%)" if ai_guess and ai_guess != "skip" else ""

    text = (
        f"{emoji} *Unknown transaction — please classify*\n"
        f"{'─'*32}\n"
        f"  {txn['date']}  [{direction}]\n"
        f"  {desc}\n"
        f"  *${amount:,.2f}*\n"
        f"{hint}"
    )
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"bk:{rtype}:{txn['id']}")]
        for label, rtype in _REVIEW_TYPES
    ]
    await bot.send_message(
        chat_id=settings.telegram_chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def send_bank_auto_review(bot: Bot, txn: dict) -> None:
    """Send an auto-classified transaction with ✅ Correct / ✏️ Change buttons."""
    direction  = "OUT" if txn["amount"] > 0 else "IN"
    emoji      = "💸" if txn["amount"] > 0 else "💰"
    desc       = txn["description"][:40]
    amount     = abs(txn["amount"])
    ai_guess   = txn.get("ai_guess", "unknown")
    confidence = txn.get("confidence", 0.0)

    text = (
        f"{emoji} *Auto-classified transaction*\n"
        f"{'─'*32}\n"
        f"  {txn['date']}  [{direction}]\n"
        f"  {desc}\n"
        f"  *${amount:,.2f}*\n\n"
        f"  Category: *{ai_guess}* ({confidence*100:.0f}% confidence)\n"
        f"  Is this correct?"
    )
    keyboard = [[
        InlineKeyboardButton("✅ Correct", callback_data=f"bk_ok:{txn['id']}"),
        InlineKeyboardButton("✏️ Change", callback_data=f"bk_change:{txn['id']}"),
    ]]
    await bot.send_message(
        chat_id=settings.telegram_chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def send_cc_mismatch_alert(bot: Bot, mismatch: dict) -> None:
    """Notify owner of a CC settlement vs daily card total mismatch."""
    diff = mismatch["diff"]
    diff_str = f"+${diff:,.2f} (bank over)" if diff > 0 else f"-${abs(diff):,.2f} (short)"
    text = (
        f"⚠️ *CC Settlement Mismatch*\n"
        f"{'─'*32}\n"
        f"  Bank deposit: ${mismatch['bank_amount']:,.2f} on {mismatch['bank_date']}\n"
        f"  Daily card: ${mismatch['sale_card']:,.2f} for {mismatch['sale_date']}\n"
        f"  Difference: {diff_str}\n\n"
        f"Check your dashboard: http://178.104.61.18/bank"
    )
    await bot.send_message(
        chat_id=settings.telegram_chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
    )


async def _send_stale_review_reminder(bot: Bot) -> None:
    """
    If there are bank transactions stuck in needs_review for > 2 days, remind the owner.
    Runs daily after the bank sync.
    """
    from db.database import get_async_session
    from db.models import BankTransaction
    from sqlalchemy import select, func
    from datetime import datetime, timezone

    cutoff = date.today() - timedelta(days=2)
    async with get_async_session() as session:
        result = await session.execute(
            select(func.count())
            .select_from(BankTransaction)
            .where(
                BankTransaction.store_id == settings.store_id,
                BankTransaction.review_status == "needs_review",
                BankTransaction.date <= str(cutoff),
            )
        )
        stale_count = result.scalar() or 0

    if stale_count > 0:
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=(
                f"🔔 *{stale_count} bank transaction{'s' if stale_count > 1 else ''} "
                f"waiting for review* (2+ days old)\n\n"
                f"Reply to each one or visit your dashboard:\n"
                f"http://178.104.61.18/bank"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )


async def handle_bank_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard responses for bank transaction review."""
    query = update.callback_query
    await query.answer()
    data     = query.data
    store_id = settings.store_id

    # ── Check-match Yes / No ──────────────────────────────────────────────────
    if data.startswith("bk_yes:") or data.startswith("bk_no:"):
        prefix, txn_id_str = data.split(":", 1)
        txn_id = int(txn_id_str)

        if prefix == "bk_yes":
            # Retrieve the proposed match from DB state
            match_state = await get_state(store_id, f"bank_match_{txn_id}")
            await clear_state(store_id, f"bank_match_{txn_id}")
            if match_state:
                from datetime import date as _date
                from tools.bank_reconciler import confirm_transaction, _highlight_sheet_match
                entry_date = _date.fromisoformat(match_state["entry_date"])
                await confirm_transaction(store_id, txn_id,
                                          match_state["match_type"], match_state["vendor"],
                                          sender="user")
                await _highlight_sheet_match({
                    "match_type": match_state["match_type"],
                    "vendor":     match_state["vendor"],
                    "entry_date": entry_date,
                })
                await query.edit_message_text(
                    f"✅ Confirmed — {match_state['vendor']} {match_state['match_type']} "
                    f"on {entry_date}. Sheet highlighted green.",
                    parse_mode=None,
                )
            else:
                await query.edit_message_text("⚠️ Match state expired. Please re-sync to try again.", parse_mode=None)

        else:  # bk_no — user says it's something else, show standard keyboard
            await clear_state(store_id, f"bank_match_{txn_id}")
            keyboard = [
                [InlineKeyboardButton(label, callback_data=f"bk:{rtype}:{txn_id}")]
                for label, rtype in _REVIEW_TYPES
            ]
            await query.edit_message_text(
                "Ok — what category is this transaction?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=None,
            )
        return

    # ── Auto-classified: Correct / Change ────────────────────────────────────
    if data.startswith("bk_ok:") or data.startswith("bk_change:"):
        prefix, txn_id_str = data.split(":", 1)
        txn_id = int(txn_id_str)

        if prefix == "bk_ok":
            # User confirms the auto-classification — mark as confirmed
            from tools.bank_reconciler import confirm_auto_transaction
            await confirm_auto_transaction(store_id, txn_id)
            await query.edit_message_text("✅ Confirmed. I'll remember this for future transactions.", parse_mode=None)
        else:
            # User wants to change — show full category keyboard
            keyboard = [
                [InlineKeyboardButton(label, callback_data=f"bk:{rtype}:{txn_id}")]
                for label, rtype in _REVIEW_TYPES
            ]
            await query.edit_message_text(
                "What category should this transaction be?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=None,
            )
        return

    # ── Standard category selection ───────────────────────────────────────────
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "bk":
        return

    _, reconcile_type, txn_id_str = parts
    txn_id = int(txn_id_str)
    store_id = settings.store_id

    # For types that need a subcategory, prompt the user
    if reconcile_type in ("invoice", "expense", "rebate", "payroll"):
        # Store pending confirmation in DB state so next plain-text triggers it
        await save_state(store_id, f"bank_confirm_{txn_id}", {
            "txn_id": txn_id,
            "reconcile_type": reconcile_type,
        })

        label_map = {
            "invoice": "vendor name (e.g. McLane, Core-Mark)",
            "expense":  "expense category (e.g. Rent, Insurance, Utilities)",
            "rebate":   "vendor name (e.g. Altria, RJ Reynolds)",
            "payroll":  "employee name",
        }
        await query.edit_message_text(
            f"✏️ What is the {label_map.get(reconcile_type, 'category')} for this transaction?\n"
            f"Reply with a short name.",
            parse_mode=None,
        )
        return

    # For types that don't need extra info, confirm immediately
    from tools.bank_reconciler import confirm_transaction, skip_transaction
    if reconcile_type == "skip":
        await skip_transaction(store_id, txn_id)
        await query.edit_message_text("✅ Marked as skipped (fee/transfer). Won't ask again for similar transactions.", parse_mode=None)
    elif reconcile_type == "cc_settlement":
        result = await confirm_transaction(store_id, txn_id, "cc_settlement", None, sender="user")
        await query.edit_message_text("✅ Marked as CC settlement. Learning pattern for future.", parse_mode=None)
    else:
        result = await confirm_transaction(store_id, txn_id, reconcile_type, None, sender="user")
        await query.edit_message_text(f"✅ Confirmed as {reconcile_type}.", parse_mode=None)


async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bank — show bank balance and recent transactions, or prompt to connect."""
    from tools.plaid_tools import is_connected, fetch_accounts, get_recent_transactions, sync_transactions

    connected = await is_connected(settings.store_id)

    if not connected:
        await update.message.reply_text(
            "🏦 No bank account connected yet.\n\n"
            "To connect your business checking account, go to your dashboard:\n"
            f"http://178.104.61.18/bank\n\n"
            "Once connected, I can show your balance here and automatically match "
            "bank transactions to your invoices and expenses.",
            parse_mode=None,
        )
        return

    # Sync first, then show
    await update.message.reply_text("🔄 Syncing bank transactions...", parse_mode=None)
    try:
        result = await sync_transactions(settings.store_id)
        accounts      = result.get("accounts", [])
        added         = result.get("added", 0)
        matched       = result.get("matched", 0)
        paid_invoices = result.get("paid_invoices", [])
        needs_review  = result.get("needs_review", [])
        cc_mismatches = result.get("cc_mismatches", [])
    except Exception as e:
        await update.message.reply_text(f"⚠️ Sync failed: {e}", parse_mode=None)
        return

    # Build balances message
    if accounts:
        balance_lines = "\n".join(
            f"  {a['official_name']}: ${a['current']:,.2f}"
            for a in accounts
        )
    else:
        balance_lines = "  (no accounts)"

    # Recent transactions (last 7 days)
    txns = await get_recent_transactions(settings.store_id, days=7)
    if txns:
        txn_lines = "\n".join(
            f"  {'✓' if t['is_matched'] else '·'} {t['date']}  {t['description'][:28]:<28}  ${t['amount']:>8.2f}"
            for t in txns[:10]
        )
    else:
        txn_lines = "  No transactions in last 7 days."

    review_note = f"\n\n⚠️ {len(needs_review)} transaction(s) need your review — see below." if needs_review else ""
    cc_note     = f"\n⚠️ {len(cc_mismatches)} CC settlement mismatch(es) detected!" if cc_mismatches else ""

    msg = (
        f"🏦 Bank Update\n"
        f"{'─'*34}\n"
        f"BALANCES\n{balance_lines}\n\n"
        f"LAST 7 DAYS ({added} new, {matched} matched)\n"
        f"```\n{txn_lines}\n```"
        f"{review_note}{cc_note}\n\n"
        f"Full view: http://178.104.61.18/bank"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    # Send paid invoice alerts
    for inv in paid_invoices:
        try:
            await _send_invoice_paid_alert(context.bot, inv)
        except Exception as e:
            log.warning("Invoice paid alert failed: %s", e)

    # Send individual review cards for unknown transactions
    for txn in needs_review[:5]:  # cap at 5 per sync to avoid spam
        try:
            await send_bank_review_request(context.bot, txn)
        except Exception as e:
            log.warning("Failed to send review for txn %s: %s", txn.get("id"), e)

    # Send CC mismatch alerts
    for mm in cc_mismatches:
        try:
            await send_cc_mismatch_alert(context.bot, mm)
        except Exception as e:
            log.warning("Failed to send CC mismatch alert: %s", e)


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

async def cmd_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /token <value> — saves a manually-provided NRS session token."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /token <value>\n\n"
            "How to get your token:\n"
            "1. Open https://mystore.nrsplus.com in Chrome and log in\n"
            "2. Press F12 → click the Network tab\n"
            "3. Click any request listed — look for one whose URL contains pos-papi.nrsplus.com\n"
            "4. The URL looks like:\n"
            "   pos-papi.nrsplus.com/u56967-abc123.../pcrhist/...\n"
            "   Copy the segment between .com/ and the next /\n"
            "   (starts with u56967- followed by letters and numbers)\n\n"
            "Then send: /token u56967-abc123...",
            parse_mode=None,
        )
        return
    token = context.args[0].strip()
    await save_cached_token(settings.store_id, token)
    await update.message.reply_text(
        f"✅ NRS token saved. Send /daily to test it.",
        parse_mode=None,
    )


def build_app() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()

    # Onboarding conversation — runs on /start or first message
    onboarding_conv = ConversationHandler(
        entry_points=[CommandHandler("start", onboarding_start)],
        states={
            ONBOARDING_STEP_NAME:       [MessageHandler(filters.TEXT & ~filters.COMMAND, onboarding_name)],
            ONBOARDING_STEP_LANG:       [MessageHandler(filters.TEXT & ~filters.COMMAND, onboarding_language)],
            ONBOARDING_STEP_BACKOFFICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboarding_backoffice)],
            ONBOARDING_STEP_BANK:       [MessageHandler(filters.TEXT & ~filters.COMMAND, onboarding_bank)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    # Daily sales conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("daily", cmd_daily)],
        states={
            AWAITING_RIGHT_SIDE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_right_side)
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(onboarding_conv)
    app.add_handler(conv)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("invoice", cmd_invoice))
    app.add_handler(CommandHandler("vendors", cmd_vendors))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("order", cmd_order))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("bank", cmd_bank))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CommandHandler("token", cmd_token))
    app.add_handler(CallbackQueryHandler(handle_bank_callback, pattern=r"^bk"))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_invoice_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_invoice_photo))
    # Plain-text invoice entries (outside conversation) e.g. "heidelburg 500 3/9"
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text_invoice))
    return app
