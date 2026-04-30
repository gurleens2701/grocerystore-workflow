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
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

# Module-level bot reference — set by build_app(), used by notify_bank_sync_results()
_bot_instance: Bot | None = None


def get_bot() -> Bot | None:
    """Return the bot instance (available after build_app)."""
    return _bot_instance


async def _guard_known_store(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs before every handler (group=-1). Rejects updates from unknown chats.
    Any chat_id not in platform.stores is silently dropped.
    If this is triggering unexpectedly, check platform.stores.chat_id values.
    """
    if not update.effective_chat:
        return
    from config.store_registry import load_store
    from config.store_context import set_active_store, get_active_store
    store = await load_store(chat_id=str(update.effective_chat.id))
    if store is None:
        log.warning("Rejected update from unknown chat_id=%s", update.effective_chat.id)
        raise ApplicationHandlerStop
    set_active_store(store.store_id)

from config.settings import settings
from config.store_context import get_active_store, set_active_store
from config.store_registry import load_store, load_all_active_stores
from db.ops import log_message, save_daily_sales, save_expense, save_invoice, save_invoice_items, save_rebate, save_revenue, save_vendor_price
from db.state import clear_state, get_state, save_state
from tools.intent_router import classify_message
from tools.invoice_extractor import extract_invoice_from_photo
from tools.nrs_tools import fetch_daily_sales, fetch_inventory, save_cached_token
from tools.price_lookup import compile_order, lookup_item_price, parse_order_list
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

# PostgreSQL state keys
_STATE_SALES = "sales"
_STATE_INVOICE_ITEMS = "invoice_items"   # pending extracted line items awaiting user confirmation
_STATE_AWAITING_REPORT = "awaiting_daily_report"  # manual mode: waiting for the report photo
_STATE_REPORT_DRAFT = "daily_report_draft"        # manual mode: OCR done, some fields still missing
_STATE_CHAT_HISTORY = "chat_history"              # rolling conversation history (last 20 messages)

_HISTORY_MAX = 40  # max messages to keep (20 back-and-forth exchanges)


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
# Formatting helpers — rules-driven from platform.store_daily_report_rules
# ---------------------------------------------------------------------------

# Field name aliases — different POS types use different names for the same data
# (e.g. Modisoft: lotto_payout; NRS: lotto_po). The DB column is one canonical name.
FIELD_NAME_ALIASES = {
    "lotto_payout": "lotto_po",   # Modisoft uses lotto_payout
    "vendor":       "vendor_payout",
    "check":        "check_amount",
}

# Field name → canonical.daily_sales column name. If a field's not here, it goes
# into extra_fields JSONB (no schema migration needed for store-specific fields).
DAILY_SALES_COLUMNS = {
    "product_sales", "lotto_in", "lotto_online", "sales_tax", "gpi",
    "grand_total", "refunds", "lotto_po", "lotto_cr", "food_stamp",
    "cash_drop", "card", "check_amount", "atm", "pull_tab", "coupon",
    "loyalty", "vendor_payout",
}


def _resolve_value(field_name: str, sales: dict[str, Any], right: dict[str, float]) -> float:
    """Look up a field's value across both API (sales) and manual (right) dicts,
    handling POS-specific aliases. Returns 0 if not found."""
    candidates = [field_name, FIELD_NAME_ALIASES.get(field_name, field_name)]
    for c in candidates:
        if c in right:
            return float(right[c] or 0)
        if c in sales:
            return float(sales[c] or 0)
    # Some Modisoft → DB column reverse aliases (e.g. cash → cash_drop)
    if field_name == "cash" and "cash_drop" in sales:
        return float(sales.get("cash_drop", 0) or 0)
    return 0.0


def _fmt_left(sales: dict[str, Any], rules: list = None, store_name: str = "Store") -> str:
    """
    Build the left-side of the daily sheet by iterating rules from
    platform.store_daily_report_rules. Departments come from the API's
    Grocery breakdown and are rendered before the rule-based totals.
    """
    if not rules:
        # No rules = no display. Strict: every store must have rules configured.
        return (
            f"⚠️ No daily report rules configured for this store.\n"
            f"Run scripts/manage_store.py to set up the daily sheet fields."
        )

    left_rules = sorted([r for r in rules if r.section == "left"], key=lambda r: r.display_order)
    right_rules = sorted([r for r in rules if r.section == "right"], key=lambda r: r.display_order)

    # Department breakdown from API (auto, every Modisoft/NRS day has its own list)
    depts = sales.get("departments", [])
    dept_lines = "\n".join(
        f"  {d['name']:<20} ${d['sales']:>8.2f}"
        for d in depts
    )

    # Render the rule-driven left-side rows
    left_body_lines = []
    grand_total_components = 0.0
    for r in left_rules:
        val = _resolve_value(r.field_name, sales, {})
        if r.source == "manual":
            # Manual fields show as awaiting until owner replies
            left_body_lines.append(f"  {r.label:<20} {'[awaiting]':>9}")
        else:
            left_body_lines.append(f"  {r.label:<20} ${val:>8.2f}")
            grand_total_components += val

    # Update sales dict so callers see the running grand_total estimate (manual=0)
    sales["grand_total"] = round(grand_total_components, 2)

    refunds = sales.get("refunds", 0)
    lines = [
        f"📊 *{store_name} — {sales['day_of_week']} {sales['date']}*",
        "─" * 34,
        "",
        "*PRODUCT SALES*",
        f"```\n{dept_lines or '  (no departments)'}",
        f"{'─'*34}",
        "\n".join(left_body_lines),
        "─" * 34,
        f"  {'GRAND TOTAL':<20} ${grand_total_components:>8.2f}",
        "```",
    ]
    if refunds:
        lines.append(f"_ℹ️ Refunds on record: ${refunds:.2f}_")

    # Auto-filled right-side fields (from POS API, owner doesn't type these)
    auto_right_lines = []
    for r in right_rules:
        if r.source != "api":
            continue
        val = _resolve_value(r.field_name, sales, {})
        if val:
            auto_right_lines.append(f"  {r.label:<20} ${val:>8.2f}")

    if auto_right_lines:
        lines += ["", "*PAYMENTS (auto)*", "```"] + auto_right_lines + ["```"]

    return "\n".join(lines)


def _prompt_for_right_side(manual_rules=None) -> str:
    """
    Build the 'please enter these numbers' prompt.
    If manual_rules (list of DailyReportRule) are provided, uses labels from DB.
    Falls back to Moraine hardcoded values when rules not yet loaded.
    """
    if manual_rules:
        lines = "\n".join(f"{r.label}:   " for r in manual_rules)
    else:
        lines = "IN. LOTTO:   \nON. LINE:    \nLOTTO PO:    \nLOTTO CR:    \nFOOD STAMP:  "
    return (
        "\n\n📋 *Please reply with these numbers:*\n"
        "_(Enter 0 if none)_\n\n"
        f"```\n{lines}\n```"
    )


def _manual_fields_text(manual_rules=None) -> str:
    """Human-readable list of manual fields for the active store."""
    if manual_rules:
        return ", ".join(r.label for r in manual_rules)
    return "the requested manual fields"


def _build_pending_report_context(sales: dict, store=None) -> str:
    """Compact store-aware context for the AI agent while a daily report is pending."""
    manual_rules = store.get_manual_rules() if store else []
    rules = store.daily_report_rules if store else []
    pos_label = store.pos_type.upper() if store else "POS"
    manual_labels = _manual_fields_text(manual_rules)

    auto_parts = []
    for r in sorted(rules, key=lambda x: (x.section, x.display_order)):
        if r.source == "manual":
            continue
        val = _resolve_value(r.field_name, sales, {})
        auto_parts.append(f"{r.label}=${val:.2f}")
    if not auto_parts:
        auto_parts = [
            f"product_sales=${sales.get('product_sales', 0):.2f}",
            f"grand_total=${sales.get('grand_total', 0):.2f}",
        ]

    return (
        f"[Context: There is a pending daily sales report for {sales.get('date', 'today')} "
        f"at {store.store_name if store else 'this store'}. "
        f"{pos_label} auto-filled: {', '.join(auto_parts)}. "
        f"To finalize the report, the owner must reply with: {manual_labels}.]"
    )


def _build_complete_sheet(sales: dict[str, Any], right: dict[str, float],
                          rules: list = None, store_name: str = "Store") -> str:
    """Build the full daily sheet with over/short — rules-driven."""
    if not rules:
        return "⚠️ No daily report rules configured for this store."

    left_rules = sorted([r for r in rules if r.section == "left"], key=lambda x: x.display_order)
    right_rules = sorted([r for r in rules if r.section == "right"], key=lambda x: x.display_order)

    # ---- LEFT SIDE ----
    left_lines = []
    grand_total = 0.0
    for r in left_rules:
        val = _resolve_value(r.field_name, sales, right)
        left_lines.append(f"  {r.label:<20} ${val:>8.2f}")
        grand_total += val
    grand_total = round(grand_total, 2)

    # Department breakdown (auto from API)
    depts = sales.get("departments", [])
    dept_lines = "\n".join(
        f"  {d['name']:<20} ${d['sales']:>8.2f}"
        for d in depts
    ) or "  (no departments)"

    # ---- RIGHT SIDE ----
    right_lines = []
    total_right = 0.0
    for r in right_rules:
        val = _resolve_value(r.field_name, sales, right)
        if val == 0:
            right_lines.append(f"  {r.label:<20} {'—':>9}")
        else:
            right_lines.append(f"  {r.label:<20} ${val:>8.2f}")
        total_right += val
    total_right = round(total_right, 2)

    # ---- OVER / SHORT ----
    diff = round(total_right - grand_total, 2)
    if diff > 0:
        over_short = f"OVER  +${diff:.2f} 🟢"
    elif diff < 0:
        over_short = f"SHORT -${abs(diff):.2f} 🔴"
    else:
        over_short = "EVEN  $0.00 ✅"

    msg = (
        f"✅ *{store_name} — {sales['day_of_week']} {sales['date']}*\n"
        f"```\n"
        f"{'─'*34}\n"
        f"  PRODUCT SALES\n"
        f"{dept_lines}\n"
        f"{'─'*34}\n"
        + "\n".join(left_lines) + "\n"
        + f"{'─'*34}\n"
        + f"  {'GRAND TOTAL':<20} ${grand_total:>8.2f}\n"
        + f"\n"
        + f"  PAYMENTS\n"
        + "\n".join(right_lines) + "\n"
        + f"{'─'*34}\n"
        + f"  {'TOTAL PAYMENTS':<20} ${total_right:>8.2f}\n"
        + f"{'─'*34}\n"
        + f"  {over_short}\n"
        + f"```"
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


def _build_preview(sales: dict, rules: list = None, store_name: str = "Store") -> str:
    """Build a live preview of the complete daily sheet from current sales state.

    Manual values are already merged into `sales` by the parser, so we pass
    sales as both `sales` and `right` — _resolve_value finds them either way.
    """
    return _build_complete_sheet(sales, sales, rules=rules, store_name=store_name)


def _parse_right_side(text: str, manual_rules=None) -> dict[str, float] | None:
    """
    Parse user's right-side input.
    If manual_rules (list of DailyReportRule) are provided, builds keys_map from DB labels.
    Falls back to Moraine hardcoded aliases when rules not loaded.
    Returns None if parsing fails.
    """
    def to_float(s: str) -> float:
        try:
            return round(float(s.replace(",", "").replace("$", "")), 2)
        except ValueError:
            return 0.0

    if manual_rules:
        # Build keys_map from DB: field_name → [label_lower, field_name with spaces]
        keys_map: dict[str, list[str]] = {}
        for r in manual_rules:
            aliases = [r.label.lower(), r.field_name.replace("_", " ")]
            keys_map[r.field_name] = aliases
    else:
        # Hardcoded fallback (Moraine defaults)
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

    # If no key matched, try plain number list matching the rule count.
    # This must only apply to replies that are actually just numbers. Otherwise
    # normal questions with dates ("department sales on April 27 2026") get
    # mistaken for manual sheet inputs while a daily report is pending.
    if not result:
        if not re.fullmatch(r"[\s,$\d.]+", text):
            return None
        nums = re.findall(r"\d+\.?\d*", text)
        n_rules = len(manual_rules) if manual_rules else 0
        field_order = [r.field_name for r in manual_rules] if manual_rules else []
        if n_rules and len(nums) == n_rules:
            result = {k: to_float(v) for k, v in zip(field_order, nums)}
        elif len(nums) == 5:
            order = ["lotto_in", "lotto_online", "lotto_po", "lotto_cr", "food_stamp"]
            result = {k: to_float(v) for k, v in zip(order, nums)}
        elif len(nums) == 3:
            order = ["lotto_po", "lotto_cr", "food_stamp"]
            result = {k: to_float(v) for k, v in zip(order, nums)}
        else:
            return None

    return result


def _looks_like_business_question(text: str) -> bool:
    """True when a pending daily sheet should not hijack a normal data question."""
    clean = text.strip().lower()
    if "?" in clean:
        return True
    starters = (
        "what ", "whats ", "what's ", "how ", "show ", "tell ", "give ",
        "list ", "which ", "when ", "why ", "where ",
    )
    if clean.startswith(starters):
        return True
    query_terms = (
        "department sales", "dept sales", "sales on", "sales for",
        "my sales", "total sales", "daily sales", "grand total",
    )
    return any(term in clean for term in query_terms)


# ---------------------------------------------------------------------------
# Bot handlers
# ---------------------------------------------------------------------------

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _parse_daily_date(text: str) -> str:
    """
    Extract a date from a 'daily' command/message. Returns ISO 'YYYY-MM-DD' or "".
    Accepts: "4-3", "4/3", "2026-04-03", "april 3", "apr 3 2026", "yesterday", "today".
    """
    from datetime import date as _date, timedelta as _td
    s = text.strip().lower()
    if not s:
        return ""
    if "yesterday" in s:
        return (_date.today() - _td(days=1)).isoformat()
    if "today" in s:
        return _date.today().isoformat()
    today = _date.today()
    # ISO: 2026-04-03
    m = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", s)
    if m:
        try:
            return _date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return ""
    # M-D or M/D (optionally with year)
    m = re.search(r"\b(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?\b", s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            return _date(year, month, day).isoformat()
        except ValueError:
            return ""
    # "april 3" / "apr 3rd" / "april 2nd 2026"
    m = re.search(r"\b(" + "|".join(_MONTHS.keys()) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", s)
    if m:
        month = _MONTHS[m.group(1)]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            return _date(year, month, day).isoformat()
        except ValueError:
            return ""
    return ""


async def _do_daily_fetch(bot: Bot, chat_id: str, target_date: str = "") -> bool:
    """Fetch POS data via dispatcher, send left side + prompt. Returns True on success."""
    try:
        log.info("_do_daily_fetch called with target_date=%r", target_date)
        label = target_date if target_date else "today's"

        # Load store profile — needed for dispatcher (pos_type) and manual rules
        store = None
        manual_rules = None
        try:
            active_store_id = get_active_store(required=False)
            if active_store_id:
                store = await load_store(store_id=active_store_id)
            if not store:
                store = await load_store(chat_id=str(chat_id))
            if store:
                manual_rules = store.get_manual_rules()
        except Exception as _e:
            log.warning("Could not load store profile: %s", _e)

        pos_label = (store.pos_type.upper() if store else "NRS")
        await bot.send_message(chat_id=chat_id, text=f"Fetching {label} data from {pos_label}...", parse_mode=None)

        if store:
            from tools.pos.dispatcher import fetch_daily_sales as dispatch_fetch
            from datetime import date as date_cls, timedelta
            parsed_date = None
            if target_date:
                try:
                    from datetime import datetime
                    parsed_date = datetime.strptime(target_date, "%Y-%m-%d").date()
                except ValueError:
                    pass
            sales = await dispatch_fetch(store, parsed_date)
        else:
            # Fallback: use legacy nrs_tools directly
            sales = await asyncio.to_thread(fetch_daily_sales, target_date)

        await save_state(get_active_store(), _STATE_SALES, sales)

        rules = store.daily_report_rules if store else None
        store_name = store.store_name if store else "Store"
        msg = _fmt_left(sales, rules=rules, store_name=store_name) + _prompt_for_right_side(manual_rules)
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
        asyncio.create_task(log_message(get_active_store(), "telegram", "bot", "Bot", msg))

        # Save daily report summary to history so follow-up questions have context
        summary = (
            f"I sent the daily sales report for {sales.get('date', 'today')}. "
            f"{pos_label} data is loaded for {store_name}. "
            f"Still waiting for owner to enter: {_manual_fields_text(manual_rules)}."
        )
        hist = await _load_history(get_active_store())
        hist.append({"role": "assistant", "content": summary})
        await save_state(get_active_store(), _STATE_CHAT_HISTORY, hist[-_HISTORY_MAX:])
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
        asyncio.create_task(log_message(get_active_store(), "telegram", "bot", "Bot", msg))
        return False


async def _do_manual_daily_prompt(bot: Bot, chat_id: str) -> None:
    """Manual-mode: ask owner to send their daily report photo."""
    await save_state(get_active_store(), _STATE_AWAITING_REPORT, {"pending": True})
    msg = (
        "📋 Ready to log today's sales!\n\n"
        "Take a photo of your daily sales report and send it here.\n"
        "Or send it as a file for better accuracy.\n\n"
        "_I'll read all the numbers and fill in the sheet for you._"
    )
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /daily — fetch POS data and send the daily sheet. State (_STATE_SALES)
    is managed in DB by _do_daily_fetch; the general message handler picks up
    subsequent replies. Plain command, no ConversationHandler."""
    sid = get_active_store()
    chat_id = str(update.effective_chat.id)
    profile = await get_user_profile(sid)
    if profile.get("backoffice") == "manual":
        await _do_manual_daily_prompt(context.bot, chat_id)
        return
    target_date = _parse_daily_date(" ".join(context.args)) if context.args else ""
    await _do_daily_fetch(context.bot, chat_id, target_date)
    # Send current bank balance after the daily sheet — silent if no Plaid.
    await _send_bank_balance(context.bot, sid, chat_id)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel pending daily report. Falls back to a no-op if nothing pending."""
    await clear_state(get_active_store(), _STATE_SALES)
    await update.message.reply_text("Cancelled.", parse_mode=None)


# ---------------------------------------------------------------------------
# Scheduler-triggered daily fetch (called from main.py)
# ---------------------------------------------------------------------------

async def scheduled_daily(app: Application) -> None:
    """Called by the scheduler at 7 AM. Runs for every active store in platform.stores."""
    bot = app.bot
    from tools.plaid_tools import is_connected, sync_transactions, fetch_accounts

    stores = await load_all_active_stores()
    if not stores:
        # Fallback to settings if platform.stores not yet seeded (pre-migration)
        stores_chat_ids = [(get_active_store(), settings.telegram_chat_id)]
    else:
        stores_chat_ids = [(s.store_id, s.chat_id) for s in stores]

    for store_id, chat_id in stores_chat_ids:
        set_active_store(store_id)
        try:
            profile = await get_user_profile(store_id)
            if profile.get("backoffice") == "manual":
                await _do_manual_daily_prompt(bot, chat_id)
                log.info("store=%s daily prompt sent (manual mode)", store_id)
            else:
                ok = await _do_daily_fetch(bot, chat_id)
                if ok:
                    log.info("store=%s daily fetch complete — waiting for right-side input", store_id)
        except Exception as e:
            log.warning("store=%s daily fetch failed: %s", store_id, e)

        # Bank sync + reconcile (if connected)
        try:
            if await is_connected(store_id):
                result = await sync_transactions(store_id)
                await notify_bank_sync_results(result, bot)

                # ── Negative balance alert ────────────────────────────────
                try:
                    accounts = await fetch_accounts(store_id)
                    for acct in accounts:
                        balance = acct.get("available") if acct.get("available") is not None else acct.get("current", 0)
                        if balance < 0:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"🚨 *Negative Bank Balance*\n"
                                    f"{acct['name']}: *${balance:,.2f}*\n\n"
                                    "Check your account — you may have overdraft fees coming."
                                ),
                                parse_mode=ParseMode.MARKDOWN,
                            )
                except Exception as e:
                    log.warning("store=%s balance check failed: %s", store_id, e)
        except Exception as e:
            log.warning("store=%s bank sync failed: %s", store_id, e)

    # ── Stale review reminder (currently store-agnostic, uses get_active_store()) ──
    try:
        await _send_stale_review_reminder(bot)
    except Exception as e:
        log.warning("Stale review reminder failed: %s", e)


# ---------------------------------------------------------------------------
# Per-store scheduler entry points — called by DB-driven scheduler in main.py
# Each job is registered once per store; these handle exactly one store.
# ---------------------------------------------------------------------------

async def _send_bank_balance(bot: Bot, store_id: str, chat_id: str) -> None:
    """
    Send current bank balance(s) to the chat. No-op if Plaid isn't connected.
    Called after the 7am daily fetch so owners see the morning balance.
    """
    try:
        from tools.plaid_tools import is_connected, fetch_accounts
        if not await is_connected(store_id):
            return
        accounts = await fetch_accounts(store_id)
        if not accounts:
            return

        lines = ["💰 *Bank Balance*", "```"]
        for a in accounts:
            name = a.get("official_name") or a.get("name", "Account")
            current = a.get("current", 0)
            lines.append(f"  {name[:24]:<24} ${current:>10,.2f}")
        lines.append("```")
        await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.warning("store=%s bank balance message failed: %s", store_id, e)


async def scheduled_daily_for_store(store_id: str, app: Application) -> None:
    """
    Daily fetch for a single store. Called by the DB-driven scheduler.

    Replaces the loop inside scheduled_daily() for multi-store setups.
    If the store profile is missing or daily_report is disabled, logs and returns.
    """
    from config.store_registry import load_store as _load
    from config.store_context import set_active_store
    set_active_store(store_id)
    store = await _load(store_id=store_id)
    if not store:
        log.warning("scheduled_daily_for_store: store_id=%s not found in platform.stores", store_id)
        return
    if not store.workflows.daily_report_enabled:
        log.info("store=%s daily_report disabled — skipping", store_id)
        return

    try:
        if store.workflows.daily_report_mode == "manual_entry":
            await _do_manual_daily_prompt(app.bot, store.chat_id)
            log.info("store=%s manual daily prompt sent", store_id)
        else:
            ok = await _do_daily_fetch(app.bot, store.chat_id)
            if ok:
                log.info("store=%s daily fetch done — waiting for right-side input", store_id)

        # After the daily sheet, send current bank balance — silent if no Plaid.
        await _send_bank_balance(app.bot, store_id, store.chat_id)
    except Exception as e:
        log.warning("store=%s scheduled_daily_for_store failed: %s", store_id, e, exc_info=True)


async def bank_sync_for_store(store_id: str, app: Application) -> None:
    """
    Bank sync for a single store. Called by the DB-driven scheduler.

    Skips silently if Plaid is not connected for this store.
    """
    from config.store_registry import load_store as _load
    from config.store_context import set_active_store
    from tools.plaid_tools import is_connected, sync_transactions, fetch_accounts

    set_active_store(store_id)
    store = await _load(store_id=store_id)
    if not store:
        log.warning("bank_sync_for_store: store_id=%s not found", store_id)
        return
    if not store.workflows.bank_recon_enabled:
        log.debug("store=%s bank_recon disabled — skipping", store_id)
        return

    try:
        if not await is_connected(store_id):
            return
        result = await sync_transactions(store_id)
        added = result.get("added", 0)
        needs = len(result.get("needs_review", []))
        autos = len(result.get("auto_list", []))
        if added or needs or autos:
            await notify_bank_sync_results(result, app.bot)
            log.info("store=%s bank sync: added=%d review=%d auto=%d", store_id, added, needs, autos)
        else:
            log.info("store=%s bank sync: no new transactions", store_id)

        # Negative balance alert
        try:
            for acct in await fetch_accounts(store_id):
                balance = acct.get("available") if acct.get("available") is not None else acct.get("current", 0)
                if balance < 0:
                    await app.bot.send_message(
                        chat_id=store.chat_id,
                        text=(
                            f"🚨 *Negative Bank Balance*\n"
                            f"{acct['name']}: *${balance:,.2f}*\n\n"
                            "Check your account — you may have overdraft fees coming."
                        ),
                        parse_mode=ParseMode.MARKDOWN,
                    )
        except Exception as e:
            log.warning("store=%s balance check failed: %s", store_id, e)
    except Exception as e:
        log.warning("store=%s bank_sync_for_store failed: %s", store_id, e, exc_info=True)


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
        "What I can do:\n"
        "\n"
        "SALES\n"
        '  "How much did I make this week?"\n'
        '  "What was my best day in March?"\n'
        '  "How much cash did I drop this month?"\n'
        "\n"
        "INVOICES\n"
        '  Send a photo of any invoice — I will read and log it.\n'
        '  Or type: "Pepsi 300 march 22"\n'
        '  "How much was Pepsi delivery last week?"\n'
        '  "Total inventory ordered this month?"\n'
        "\n"
        "EXPENSES\n"
        '  "electricity 340 march 10"\n'
        '  "rent 2500"\n'
        '  "payroll simmt 1200"\n'
        "\n"
        "REBATES\n"
        '  "altria rebate 500 march"\n'
        '  "How much rebates this month?"\n'
        "\n"
        "PRICES\n"
        '  "Price of marlboro red?"\n'
        '  "What does mountain dew 20oz cost?"\n'
        "\n"
        "BANK\n"
        "  Weekly summary sent every Sunday at 6PM.\n"
        "\n"
        "VENDORS\n"
        '  "Who are my vendors?"\n'
        '  "How much did I spend on McLane?"\n'
        "\n"
        "VOICE\n"
        "  Send a voice message in any language.\n"
        "\n"
        "COMMANDS\n"
        "  /daily — fetch yesterday's sales from NRS\n"
        "  /sync — pull latest from Google Sheet\n"
        "\n"
        "Just talk to me naturally. I speak Hindi, Gujarati, Punjabi, and more."
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

    parsed = await asyncio.to_thread(_extract_invoice_fields, text)
    if not parsed:
        await update.message.reply_text(
            "⚠️ Could not parse. Try: /invoice Heidelburg 500 3/9", parse_mode=None
        )
        return

    try:
        vendor = parsed["vendor"]
        amount = parsed["amount"]
        entry_date = parsed["entry_date"]
        invoice_id = await save_invoice(get_active_store(), vendor, amount, entry_date)
        await save_vendor_price(get_active_store(), vendor, amount, entry_date, invoice_id)
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
        report = await asyncio.to_thread(get_vendor_comparison, category
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
        result = await asyncio.to_thread(lookup_item_price, query
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
        await save_state(get_active_store(), "awaiting_order", {"pending": True})
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
        summary = await asyncio.to_thread(compile_order, items
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
        await save_expense(get_active_store(), parsed["label"], parsed["amount"], parsed["entry_date"])
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
        await save_rebate(get_active_store(), parsed["label"], parsed["amount"], parsed["entry_date"])
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
        await save_revenue(get_active_store(), parsed["label"], parsed["amount"], parsed["entry_date"])
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
    parsed = await asyncio.to_thread(_extract_invoice_fields, text)
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
        invoice_id = await save_invoice(get_active_store(), vendor_match, parsed["amount"], parsed["entry_date"])
        await save_vendor_price(get_active_store(), vendor_match, parsed["amount"], parsed["entry_date"], invoice_id)
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
    asyncio.create_task(log_message(get_active_store(), "telegram", "user", sender, text))

    # ── Onboarding gate — redirect new users before anything else ────────────
    if not await is_onboarding_complete(get_active_store()):
        await onboarding_start(update, context)
        return

    # ── Load owner profile (name, language) ─────────────────────────────────
    profile = await get_user_profile(get_active_store())
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
        k for k in (await _list_bank_confirm_keys(get_active_store()))
    ]
    if pending_confirm_keys:
        key = pending_confirm_keys[0]  # take first pending
        state = await get_state(get_active_store(), key)
        if state:
            from tools.bank_reconciler import confirm_transaction
            txn_id = state["txn_id"]
            reconcile_type = state["reconcile_type"]
            subcategory = text.strip()
            await clear_state(get_active_store(), key)
            result = await confirm_transaction(get_active_store(), txn_id, reconcile_type, subcategory, sender="user")
            await clear_state(get_active_store(), f"bk_msg_{txn_id}")
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
    sales = await get_state(get_active_store(), _STATE_SALES)
    if sales:
        # Auto-expire stale sales state after 12h — don't let it hijack chat forever.
        from db.state import get_state_age_hours
        age_h = await get_state_age_hours(get_active_store(), _STATE_SALES)
        if age_h is not None and age_h > 12:
            log.info("Auto-clearing stale sales state (age=%.1fh)", age_h)
            await clear_state(get_active_store(), _STATE_SALES)
            sales = None
    if sales:
        clean_reply = text.strip().lower()

        # Load this store's rules + name once so every preview/save call below uses them.
        # Without this, _build_preview falls back to Moraine's hardcoded fields and shows
        # "No daily report rules configured" for any non-NRS store.
        store_rules = None
        store_name = "Store"
        manual_rules = None
        _store = None
        try:
            _store = await load_store(chat_id=str(update.effective_chat.id))
            if _store:
                store_rules = _store.daily_report_rules
                store_name = _store.store_name
                manual_rules = _store.get_manual_rules()
        except Exception as _e:
            log.warning("Could not load store rules in pending-sales path: %s", _e)

        # ── Cancel — escape from daily report state to normal chat ─────────────
        if clean_reply in ("cancel", "nevermind", "never mind", "stop", "exit", "quit", "abort"):
            await clear_state(get_active_store(), _STATE_SALES)
            await update.message.reply_text(
                "Daily report cancelled. What do you need?",
                parse_mode=None,
            )
            return

        # ── Store/data questions should still work while a report is pending ──
        # The owner may ask "what were department sales on April 27 2026?"
        # before finishing today's sheet. Do not treat dates in those questions
        # as manual sheet numbers.
        if _looks_like_business_question(text):
            from tools.main_agent import run_agent
            history = await _load_history(get_active_store())
            question = f"{_build_pending_report_context(sales, _store)}\n\nOwner says: {text}"
            try:
                reply = await asyncio.to_thread(run_agent, question, get_active_store(), owner_name, history)
                await update.message.reply_text(reply, parse_mode=None)
                await _save_history(get_active_store(), history, text, reply)
            except Exception as e:
                log.error("Agent failed inside sales state: %s", e, exc_info=True)
                await update.message.reply_text(
                    "Sorry, I hit an error. Try again?",
                    parse_mode=None,
                )
            return

        # ── Save / confirm ───────────────────────────────────────────────────
        if clean_reply in ("ok", "confirm", "yes", "save", "good", "correct", "done", "log it", "log"):
            # Pull all manual fields out of sales (they were merged in by _parse_right_side)
            right = {}
            manual_values = sales.get("_manual_values") or {}
            if manual_rules:
                for r in manual_rules:
                    if r.field_name in manual_values:
                        right[r.field_name] = manual_values[r.field_name]
                    elif r.field_name in sales:
                        right[r.field_name] = sales[r.field_name]
            else:
                right = {
                    "lotto_po":   sales.get("lotto_po",   sales.get("_ocr_lotto_po",   0)),
                    "lotto_cr":   sales.get("lotto_cr",   sales.get("_ocr_lotto_cr",   0)),
                    "food_stamp": sales.get("food_stamp", sales.get("_ocr_food_stamp", 0)),
                }
            preview = _build_complete_sheet(sales, right, rules=store_rules, store_name=store_name)
            await update.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN)
            await update.message.reply_text("📊 Logging to Google Sheets...", parse_mode=None)
            try:
                sales_for_sheet = dict(sales)
                sales_for_sheet.update(right)
                log_daily_sales(sales_for_sheet)
                await save_daily_sales(get_active_store(), sales, right)
                save_daily_report(get_active_store(), sales, right)
                await clear_state(get_active_store(), _STATE_SALES)
                await update.message.reply_text("✅ Logged to Google Sheets.", parse_mode=None)
            except Exception as e:
                log.error("Sheets logging failed: %s", e, exc_info=True)
                await update.message.reply_text(f"⚠️ Sheets logging failed: {e}", parse_mode=None)
            return

        # ── Structured right-side format (LOTTO PO: X / LOTTO CR: Y / FOOD STAMP: Z) ──
        right = _parse_right_side(text, manual_rules)
        if right is not None:
            sales.update(right)
            manual_values = dict(sales.get("_manual_values") or {})
            manual_values.update(right)
            sales["_manual_values"] = manual_values
            await save_state(get_active_store(), _STATE_SALES, sales)
            preview = _build_preview(sales, rules=store_rules, store_name=store_name)
            await update.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN)
            await update.message.reply_text("Reply *ok* to save, or change any number.", parse_mode=ParseMode.MARKDOWN)
            return

        # ── Ambiguous-number detection ───────────────────────────────────────
        # If the message is just digits (no field labels) but they typed numbers,
        # they likely meant manual values — ask for clarification rather than
        # letting the agent guess (or _parse_sales_edit hallucinate).
        digits_only = re.findall(r"\d+\.?\d*", text)
        is_plain_number_reply = bool(re.fullmatch(r"[\s,$\d.]+", text))
        has_label = any(
            re.search(re.escape(r.label.lower()), text.lower()) or
            re.search(re.escape(r.field_name.replace("_", " ")), text.lower())
            for r in (manual_rules or [])
        ) if manual_rules else False
        if digits_only and is_plain_number_reply and not has_label and manual_rules:
            manual_values = dict(sales.get("_manual_values") or {})
            missing_rules = [r for r in manual_rules if r.field_name not in manual_values]
            if 0 < len(digits_only) <= len(missing_rules):
                partial = {
                    r.field_name: round(float(v.replace(",", "").replace("$", "")), 2)
                    for r, v in zip(missing_rules, digits_only)
                }
                sales.update(partial)
                manual_values.update(partial)
                sales["_manual_values"] = manual_values
                await save_state(get_active_store(), _STATE_SALES, sales)

                still_missing = [r for r in manual_rules if r.field_name not in manual_values]
                preview = _build_preview(sales, rules=store_rules, store_name=store_name)
                await update.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN)
                if still_missing:
                    await update.message.reply_text(
                        "Still need:\n```\n"
                        + "\n".join(f"{r.label}: <number>" for r in still_missing)
                        + "\n```",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                else:
                    await update.message.reply_text("Reply *ok* to save, or change any number.", parse_mode=ParseMode.MARKDOWN)
                return

            field_list = "\n".join(f"  {r.label}" for r in manual_rules)
            await update.message.reply_text(
                f"I see {len(digits_only)} numbers but I can't tell which field each is for. "
                f"Please reply like this:\n```\n"
                f"{chr(10).join(f'{r.label}: <number>' for r in manual_rules)}\n```",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # ── Natural language edit ("change card to 1300", "lotto payout 500") ──
        # Only run for Moraine (legacy hardcoded fields). Hamilton/other stores
        # skip this — _parse_sales_edit's prompt assumes Moraine's field names
        # and would hallucinate wrong fields for other layouts.
        if not store_rules or store_name == "Moraine Foodmart":
            edits = await asyncio.to_thread(_parse_sales_edit, text, sales)
            if edits:
                sales.update(edits)
                await save_state(get_active_store(), _STATE_SALES, sales)
                changed = ", ".join(f"{k}=${v:.2f}" for k, v in edits.items())
                preview = _build_preview(sales, rules=store_rules, store_name=store_name)
                await update.message.reply_text(
                    f"Updated: {changed}\n\n" + preview,
                    parse_mode=ParseMode.MARKDOWN,
                )
                await update.message.reply_text("Reply *ok* to save, or change any number.", parse_mode=ParseMode.MARKDOWN)
                return

        # ── Nothing matched — fall through to the agent. Do NOT nag about the
        # pending report on every message; user already saw it, and spamming
        # makes the bot feel robotic.
        from tools.main_agent import run_agent
        history = await _load_history(get_active_store())
        try:
            question = f"{_build_pending_report_context(sales, _store)}\n\nOwner says: {text}"
            reply = await asyncio.to_thread(run_agent, question, get_active_store(), owner_name, history)
            await update.message.reply_text(reply, parse_mode=None)
            await _save_history(get_active_store(), history, text, reply)
        except Exception as e:
            log.error("Agent failed inside sales state: %s", e, exc_info=True)
            await update.message.reply_text(
                "Sorry, I hit an error. Try again?", parse_mode=None,
            )
        return

    # ── Priority 2: pending invoice items confirmation ───────────────────────
    pending_items = await get_state(get_active_store(), _STATE_INVOICE_ITEMS)
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
                    get_active_store(), vendor, 0, inv_date, inv_num
                )
                # Save line items
                count = await save_invoice_items(
                    get_active_store(),
                    vendor,
                    pending_items.get("items", []),
                    inv_date,
                    invoice_id,
                )
                await clear_state(get_active_store(), _STATE_INVOICE_ITEMS)
                await update.message.reply_text(
                    f"✅ Saved {count} items from {vendor} invoice ({inv_date}).\n"
                    "Use /price <item> to look up prices anytime.",
                    parse_mode=None,
                )
            except Exception as e:
                log.error("Saving invoice items failed: %s", e, exc_info=True)
                await update.message.reply_text(f"⚠️ Save failed: {e}", parse_mode=None)
        elif answer in ("NO", "N", "DISCARD", "CANCEL"):
            await clear_state(get_active_store(), _STATE_INVOICE_ITEMS)
            await update.message.reply_text("🗑️ Invoice discarded.", parse_mode=None)
        else:
            await update.message.reply_text(
                "Reply *YES* to save the extracted items or *NO* to discard.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # ── Priority 2.5: daily report draft — collecting missing fields ────────
    report_draft = await get_state(get_active_store(), _STATE_REPORT_DRAFT)
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
            await save_state(get_active_store(), _STATE_REPORT_DRAFT, report_draft)
            return

        await clear_state(get_active_store(), _STATE_REPORT_DRAFT)

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
            await save_daily_sales(get_active_store(), sales, right)
            save_daily_report(get_active_store(), sales, right)
            await update.message.reply_text("✅ Logged to Google Sheets.", parse_mode=None)
        except Exception as e:
            log.error("Sheets logging failed: %s", e, exc_info=True)
            await update.message.reply_text(f"⚠️ Sheets logging failed: {e}", parse_mode=None)
        return

    # ── Priority 3: pending order list ──────────────────────────────────────
    awaiting_order = await get_state(get_active_store(), "awaiting_order")
    if awaiting_order:
        await clear_state(get_active_store(), "awaiting_order")
        await _process_order(update, text)
        return

    # ── Priority 4: daily fetch starts the state machine; everything else → unified agent ──
    intent = classify_message(text)
    log.info("Intent: %s | %s", intent, text[:60])

    if intent == "daily_fetch":
        chat_id = str(update.effective_chat.id)
        if profile.get("backoffice") == "manual":
            await _do_manual_daily_prompt(context.bot, chat_id)
        else:
            target_date = _parse_daily_date(text)
            await _do_daily_fetch(context.bot, chat_id, target_date)
    else:
        from tools.main_agent import run_agent

        # Load conversation history for context
        history = await _load_history(get_active_store())

        # If there's a pending daily report, include its data as context
        # so follow-up questions ("change lotto payout", "what was my card?") work
        pending_sales = await get_state(get_active_store(), _STATE_SALES)
        question = text
        if pending_sales:
            store = await load_store(store_id=get_active_store())
            question = f"{_build_pending_report_context(pending_sales, store)}\n\nOwner says: {text}"

        try:
            reply = await asyncio.to_thread(run_agent, question, get_active_store(), owner_name, history
            )
            await update.message.reply_text(reply, parse_mode=None)
            asyncio.create_task(log_message(get_active_store(), "telegram", "bot", "Bot", reply))
            # Save exchange to history (use original text, not injected context)
            await _save_history(get_active_store(), history, text, reply)
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
        current = (await get_state(get_active_store(), "language_pref")) or {}
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
    await save_state(get_active_store(), "language_pref", {"name": name.title(), "code": code})
    if name == "auto":
        await update.message.reply_text("Language set to auto-detect.", parse_mode=None)
    else:
        await update.message.reply_text(f"Language set to {name.title()}. Voice messages will be transcribed in {name.title()}.", parse_mode=None)


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Telegram voice messages — transcribe with Whisper, then run through agent."""
    from tools.voice import transcribe_voice
    from tools.main_agent import run_agent

    sender = update.effective_user.first_name or "Owner"
    asyncio.create_task(log_message(get_active_store(), "telegram", "user", sender, "🎤 Voice message"))

    # Onboarding gate
    if not await is_onboarding_complete(get_active_store()):
        await onboarding_start(update, context)
        return

    # Get language from user profile (set during onboarding), fall back to legacy pref
    profile = await get_user_profile(get_active_store())
    lang_code = profile.get("language") or (
        (await get_state(get_active_store(), "language_pref") or {}).get("code")
    )
    if lang_code == "auto":
        lang_code = None

    await update.message.reply_text("🎤 Listening...", parse_mode=None)

    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id, read_timeout=60, write_timeout=60, connect_timeout=30)
        audio_bytes = bytes(await file.download_as_bytearray(read_timeout=60, write_timeout=60, connect_timeout=30))

        # Transcribe
        text = await asyncio.to_thread(transcribe_voice, audio_bytes, lang_code
        )

        if not text:
            await update.message.reply_text("Could not understand the audio. Please try again.", parse_mode=None)
            return

        log.info("Voice transcribed: %s", text[:100])
        asyncio.create_task(log_message(get_active_store(), "telegram", "user", sender, f"🎤 {text}"))

        # Check if this is a pending daily sheet response
        sales = await get_state(get_active_store(), _STATE_SALES)
        if sales:
            store = await load_store(store_id=get_active_store())
            manual_rules = store.get_manual_rules() if store else None
            store_rules = store.daily_report_rules if store else None
            store_name = store.store_name if store else "Store"
            right = _parse_right_side(text, manual_rules)
            if right is not None:
                sales.update(right)
                manual_values = dict(sales.get("_manual_values") or {})
                manual_values.update(right)
                sales["_manual_values"] = manual_values
                sheet_msg = _build_complete_sheet(sales, right, rules=store_rules, store_name=store_name)
                await update.message.reply_text(sheet_msg, parse_mode=ParseMode.MARKDOWN)
                try:
                    sales_for_sheet = dict(sales)
                    sales_for_sheet.update(right)
                    log_daily_sales(sales_for_sheet)
                    await save_daily_sales(get_active_store(), sales, right)
                    save_daily_report(get_active_store(), sales, right)
                    await update.message.reply_text("Logged to Google Sheets.", parse_mode=None)
                except Exception as e:
                    await update.message.reply_text(f"Sheets logging failed: {e}", parse_mode=None)
                await clear_state(get_active_store(), _STATE_SALES)
                return

        # Otherwise run through unified agent
        intent = classify_message(text)
        if intent == "daily_fetch":
            await _do_daily_fetch(context.bot, str(update.effective_chat.id))
        else:
            reply = await asyncio.to_thread(run_agent, text, get_active_store(), profile.get("name", ""))
            asyncio.create_task(log_message(get_active_store(), "telegram", "bot", "Bot", reply))

            # Send voice reply + text reply
            try:
                from tools.voice import text_to_speech
                audio = await asyncio.to_thread(text_to_speech, reply
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
        result = await asyncio.to_thread(extract_daily_report_from_photo, photo_bytes
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

    await clear_state(get_active_store(), _STATE_AWAITING_REPORT)

    summary = _fmt_ocr_summary(extracted, departments, must_ask, report_date)

    if must_ask:
        # Save draft and ask for specific missing fields
        draft = {
            "extracted":   extracted,
            "departments": departments,
            "missing":     must_ask,
            "report_date": str(report_date) if report_date else None,
        }
        await save_state(get_active_store(), _STATE_REPORT_DRAFT, draft)
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
    await save_state(get_active_store(), _STATE_SALES, sales)
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
    asyncio.create_task(log_message(get_active_store(), "telegram", "user", sender, "📸 Sent a photo"))

    try:
        # Download photo bytes first — shared by both flows
        if update.message.document:
            file = await context.bot.get_file(update.message.document.file_id)
        else:
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
        photo_bytes = bytes(await file.download_as_bytearray())

        # ── Manual mode: daily report photo ──────────────────────────────────
        awaiting_report = await get_state(get_active_store(), _STATE_AWAITING_REPORT)
        if awaiting_report:
            await _handle_daily_report_photo(update, context, photo_bytes)
            return

        await update.message.reply_text("📸 Reading invoice, this may take a few seconds...", parse_mode=None)

        result = await asyncio.to_thread(extract_invoice_from_photo, photo_bytes
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
        await save_state(get_active_store(), _STATE_INVOICE_ITEMS, result)
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
    ("✏️ Other (type)", "other"),
]


def _get_subcat_options(rtype: str) -> list[str]:
    """Return canonical subcategory options for a given reconcile type.

    Sourced from tools/sheets_tools.py so the buttons always match the
    columns in the Google Sheet. Returns [] for types that don't have
    a fixed subcategory list.
    """
    try:
        from tools.sheets_tools import (
            EXPENSES_HEADERS,
            REBATES_HEADERS,
            PAYROLL_HEADERS,
            COGS_VENDOR_COLS,
        )
    except ImportError:
        return []
    skip = {"DATE", "TOTAL"}
    if rtype == "expense":
        return [h for h in EXPENSES_HEADERS if h not in skip]
    if rtype == "rebate":
        return [h for h in REBATES_HEADERS if h not in skip]
    if rtype == "payroll":
        return [h for h in PAYROLL_HEADERS if h not in skip]
    if rtype == "invoice":
        return [h for h in COGS_VENDOR_COLS if h not in skip]
    return []


def _build_subcat_keyboard(rtype: str, txn_id: int) -> InlineKeyboardMarkup:
    """Build a 2-column keyboard of subcategories for a reconcile type,
    with a final 'Other (type)' row for free-text entry."""
    options = _get_subcat_options(rtype)
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(options), 2):
        pair = options[i : i + 2]
        rows.append([
            InlineKeyboardButton(opt, callback_data=f"bks:{rtype}:{idx}:{txn_id}")
            for idx, opt in enumerate(pair, start=i)
        ])
    rows.append([
        InlineKeyboardButton("✏️ Other (type)", callback_data=f"bko:{rtype}:{txn_id}")
    ])
    return InlineKeyboardMarkup(rows)


async def _get_active_chat_id(default: str | None = None) -> str:
    """Resolve the Telegram chat for the active store."""
    try:
        sid = get_active_store(required=False)
        if sid:
            store = await load_store(store_id=sid)
            if store:
                return store.chat_id
    except Exception as e:
        log.warning("Could not resolve active store chat_id: %s", e)
    return default or settings.telegram_chat_id


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
        chat_id=await _get_active_chat_id(),
        text=text,
        parse_mode=ParseMode.MARKDOWN,
    )


async def _save_txn_message_id(txn_id: int, message_id: int) -> None:
    """Save the Telegram message_id for a bank transaction so we can edit it later."""
    await save_state(get_active_store(), f"bk_msg_{txn_id}", {"message_id": message_id})


async def _get_txn_message_id(txn_id: int) -> int | None:
    """Retrieve the Telegram message_id for a bank transaction."""
    state = await get_state(get_active_store(), f"bk_msg_{txn_id}")
    return state.get("message_id") if state else None


async def mark_txn_confirmed_on_telegram(txn_id: int, reconcile_type: str, subcategory: str | None) -> None:
    """
    Edit the Telegram message for a bank transaction to show it's been confirmed.
    Called from dashboard API when user confirms there — keeps Telegram in sync.
    """
    bot = _bot_instance
    if not bot:
        return
    msg_id = await _get_txn_message_id(txn_id)
    if not msg_id:
        return
    label = reconcile_type
    if subcategory:
        label += f" ({subcategory})"
    try:
        await bot.edit_message_text(
            chat_id=await _get_active_chat_id(),
            message_id=msg_id,
            text=f"✅ Confirmed via dashboard: {label}",
            parse_mode=None,
        )
    except Exception as e:
        log.warning("Failed to update Telegram message for txn %s: %s", txn_id, e)
    finally:
        await clear_state(get_active_store(), f"bk_msg_{txn_id}")


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
        await save_state(get_active_store(), f"bank_match_{txn['id']}", {
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
        msg = await bot.send_message(
            chat_id=await _get_active_chat_id(),
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        await _save_txn_message_id(txn["id"], msg.message_id)
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
    msg = await bot.send_message(
        chat_id=await _get_active_chat_id(),
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    await _save_txn_message_id(txn["id"], msg.message_id)


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
    msg = await bot.send_message(
        chat_id=await _get_active_chat_id(),
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    await _save_txn_message_id(txn["id"], msg.message_id)


async def send_cc_settlement_alert(bot: Bot, settlement: dict) -> None:
    """Notify owner of a CC settlement match, mismatch, or ambiguous case.

    Matches are info-only (plus Resolve buttons for any skipped older days).
    Ambiguous = multiple contiguous ranges tight-matched; user picks which.
    Mismatch = no range matched; user can Resolve the flagged day.
    """
    diff = settlement["diff"]
    matched = settlement.get("matched", False)
    skipped_days = settlement.get("skipped_days") or []
    settled_days = settlement.get("settled_days") or []
    ambiguous = settlement.get("ambiguous", False)
    reply_markup = None

    if matched:
        # Single day or multi-day range auto-settled
        header_days = (
            settled_days[0] if len(settled_days) == 1
            else f"{settled_days[0]} → {settled_days[-1]} ({len(settled_days)} days)"
        )
        text = (
            f"✅ *CC Settlement Matched*\n"
            f"{'─'*32}\n"
            f"  Bank deposit: *${settlement['bank_amount']:,.2f}* on {settlement['bank_date']}\n"
            f"  Matched to: {header_days}\n"
            f"  Card total: *${settlement['sale_card']:,.2f}*\n"
        )
        if abs(diff) > 0.01:
            text += f"  Diff: ${abs(diff):,.2f}\n"
        text += f"  Sheet CREDIT cells highlighted green."

        if skipped_days:
            days_str = ", ".join(skipped_days)
            text += (
                f"\n\n⚠️ Older unsettled day(s) skipped: *{days_str}*\n"
                f"Likely fee hold or held batch. Tap below to mark resolved "
                f"once you've confirmed with the processor."
            )
            buttons = [
                [InlineKeyboardButton(f"✓ Resolve {d}", callback_data=f"cc_resolve:{d}")]
                for d in skipped_days
            ]
            reply_markup = InlineKeyboardMarkup(buttons)

    elif ambiguous:
        options = settlement.get("ambiguous_options") or []
        text = (
            f"❓ *CC Settlement Ambiguous*\n"
            f"{'─'*32}\n"
            f"  Bank deposit: ${settlement['bank_amount']:,.2f} on {settlement['bank_date']}\n\n"
            f"Multiple day combinations match this deposit. Pick the right one:\n\n"
        )
        buttons = []
        for idx, opt in enumerate(options[:6], 1):  # cap at 6 options
            text += f"  {idx}. {opt['label']} — ${opt['total']:,.2f}\n"
            # Callback encodes all days comma-separated; handler will settle all
            days_payload = ",".join(opt["days"])
            buttons.append([InlineKeyboardButton(
                f"{idx}. Settle {opt['label']}",
                callback_data=f"cc_pick:{settlement['bank_txn_id']}:{days_payload}",
            )])
        text += "\nOr tap Skip to leave them for manual review."
        buttons.append([InlineKeyboardButton(
            "Skip",
            callback_data=f"cc_skip:{settlement['bank_txn_id']}",
        )])
        reply_markup = InlineKeyboardMarkup(buttons)

    else:
        diff_str = f"+${diff:,.2f} (bank over)" if diff > 0 else f"-${abs(diff):,.2f} (short)"
        text = (
            f"⚠️ *CC Settlement Mismatch*\n"
            f"{'─'*32}\n"
            f"  Bank deposit: ${settlement['bank_amount']:,.2f} on {settlement['bank_date']}\n"
            f"  Oldest unsettled day: {settlement['sale_date']} card ${settlement['sale_card']:,.2f}\n"
            f"  Difference: {diff_str}\n\n"
        )
        if diff < -1.00:
            text += "This is a real shortage — call the credit card processor.\n\n"
        else:
            text += (
                "Could be a fee hold, multi-day batch the matcher couldn't align, "
                f"or timing issue. If you've confirmed the {settlement['sale_date']} "
                f"day is handled, tap Resolve to stop alerts.\n\n"
            )
        text += "Dashboard: clerkai.live/bank"

        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✓ Resolve {settlement['sale_date']}",
                callback_data=f"cc_resolve:{settlement['sale_date']}",
            )
        ]])

    await bot.send_message(
        chat_id=await _get_active_chat_id(),
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
    )


async def notify_bank_sync_results(result: dict, bot: Bot | None = None) -> None:
    """
    Send Telegram notifications for bank sync results.
    Called from anywhere — daily scheduler, /bank command, or API sync endpoint.
    """
    if bot is None:
        bot = _bot_instance
    if bot is None:
        log.warning("notify_bank_sync_results: no bot instance available")
        return

    needs_review  = result.get("needs_review", [])
    auto_list     = result.get("auto_list", [])
    cc_mismatches = result.get("cc_mismatches", [])
    paid_invoices = result.get("paid_invoices", [])

    for inv in paid_invoices:
        try:
            await _send_invoice_paid_alert(bot, inv)
        except Exception as e:
            log.warning("Invoice paid alert failed: %s", e)

    for txn in needs_review:
        try:
            await send_bank_review_request(bot, txn)
        except Exception as e:
            log.warning("Review request failed for txn %s: %s", txn.get("id"), e)

    for txn in auto_list:
        try:
            await send_bank_auto_review(bot, txn)
        except Exception as e:
            log.warning("Auto review send failed for txn %s: %s", txn.get("id"), e)

    for mm in cc_mismatches:
        try:
            await send_cc_settlement_alert(bot, mm)
        except Exception as e:
            log.warning("CC settlement alert failed: %s", e)


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
                BankTransaction.store_id == get_active_store(),
                BankTransaction.review_status == "needs_review",
                BankTransaction.transaction_date <= cutoff,
            )
        )
        stale_count = result.scalar() or 0

    if stale_count > 0:
        await bot.send_message(
            chat_id=await _get_active_chat_id(),
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
    store_id = get_active_store()

    # ── CC settlement manual resolve ──────────────────────────────────────────
    if data.startswith("cc_resolve:"):
        sale_date_iso = data.split(":", 1)[1]
        from tools.bank_reconciler import resolve_sale_day_cc
        ok = await resolve_sale_day_cc(store_id, sale_date_iso)
        if ok:
            await query.edit_message_text(
                f"✓ Marked {sale_date_iso} as CC-resolved. No more alerts for this day.",
                parse_mode=None,
            )
        else:
            await query.edit_message_text(
                f"Couldn't find sale day {sale_date_iso}.",
                parse_mode=None,
            )
        return

    # ── CC ambiguous: user picked which range to settle ──────────────────────
    if data.startswith("cc_pick:"):
        _, bank_txn_id_str, days_csv = data.split(":", 2)
        bank_txn_id = int(bank_txn_id_str)
        day_isos = days_csv.split(",")
        from tools.bank_reconciler import settle_cc_days_with_deposit
        ok = await settle_cc_days_with_deposit(store_id, bank_txn_id, day_isos)
        if ok:
            label = day_isos[0] if len(day_isos) == 1 else f"{day_isos[0]} → {day_isos[-1]}"
            await query.edit_message_text(
                f"✓ Settled {label} ({len(day_isos)} day{'s' if len(day_isos) > 1 else ''}). Sheet highlighted.",
                parse_mode=None,
            )
        else:
            await query.edit_message_text("Couldn't settle — deposit or days not found.", parse_mode=None)
        return

    # ── CC ambiguous: user chose to skip ──────────────────────────────────────
    if data.startswith("cc_skip:"):
        bank_txn_id = int(data.split(":", 1)[1])
        from tools.bank_reconciler import skip_cc_deposit
        await skip_cc_deposit(store_id, bank_txn_id)
        await query.edit_message_text(
            "Skipped. Days stay unsettled — resolve manually from the dashboard.",
            parse_mode=None,
        )
        return

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
                await clear_state(store_id, f"bk_msg_{txn_id}")
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
            await clear_state(store_id, f"bk_msg_{txn_id}")
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

    # ── Subcategory button selected (bks:rtype:idx:txn_id) ────────────────────
    if data.startswith("bks:"):
        _, rtype, idx_str, txn_id_str = data.split(":", 3)
        txn_id = int(txn_id_str)
        idx = int(idx_str)
        options = _get_subcat_options(rtype)
        if not (0 <= idx < len(options)):
            await query.edit_message_text("Invalid option. Please retry.", parse_mode=None)
            return
        subcat = options[idx]
        from tools.bank_reconciler import confirm_transaction
        await confirm_transaction(store_id, txn_id, rtype, subcat, sender="user")
        await clear_state(store_id, f"bk_msg_{txn_id}")
        await query.edit_message_text(
            f"✅ Confirmed as {rtype}: {subcat}\n"
            f"I'll remember this for similar transactions.",
            parse_mode=None,
        )
        return

    # ── User wants to type a custom subcategory (bko:rtype:txn_id) ────────────
    if data.startswith("bko:"):
        _, rtype, txn_id_str = data.split(":", 2)
        txn_id = int(txn_id_str)
        await save_state(store_id, f"bank_confirm_{txn_id}", {
            "txn_id": txn_id,
            "reconcile_type": rtype,
        })
        prompt_map = {
            "invoice": "vendor name (e.g. McLane, Core-Mark)",
            "expense":  "expense category (e.g. Rent, Insurance, Utilities)",
            "rebate":   "rebate source",
            "payroll":  "employee name",
        }
        await query.edit_message_text(
            f"✏️ Type the {prompt_map.get(rtype, 'name')}:",
            parse_mode=None,
        )
        return

    # ── Standard category selection ───────────────────────────────────────────
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "bk":
        return

    _, reconcile_type, txn_id_str = parts
    txn_id = int(txn_id_str)
    store_id = get_active_store()

    # For known types that have fixed subcategory lists, show a button keyboard
    if reconcile_type in ("invoice", "expense", "rebate", "payroll"):
        rtype_label = {
            "invoice": "vendor",
            "expense": "expense category",
            "rebate":  "rebate source",
            "payroll": "employee",
        }[reconcile_type]
        await query.edit_message_text(
            f"Which {rtype_label}?",
            reply_markup=_build_subcat_keyboard(reconcile_type, txn_id),
            parse_mode=None,
        )
        return

    # "Other" at top level — let user type whatever this transaction is
    if reconcile_type == "other":
        await save_state(store_id, f"bank_confirm_{txn_id}", {
            "txn_id": txn_id,
            "reconcile_type": "other",
        })
        await query.edit_message_text(
            "✏️ What kind of transaction is this? Type a short description "
            "(e.g. 'loan repayment', 'tax refund', 'owner draw').",
            parse_mode=None,
        )
        return

    # For types that don't need extra info, confirm immediately
    from tools.bank_reconciler import confirm_transaction, skip_transaction
    if reconcile_type == "skip":
        await skip_transaction(store_id, txn_id)
        await clear_state(store_id, f"bk_msg_{txn_id}")
        await query.edit_message_text("✅ Marked as skipped (fee/transfer). Won't ask again for similar transactions.", parse_mode=None)
    elif reconcile_type == "cc_settlement":
        result = await confirm_transaction(store_id, txn_id, "cc_settlement", None, sender="user")
        await clear_state(store_id, f"bk_msg_{txn_id}")
        await query.edit_message_text("✅ Marked as CC settlement. Learning pattern for future.", parse_mode=None)
    else:
        result = await confirm_transaction(store_id, txn_id, reconcile_type, None, sender="user")
        await clear_state(store_id, f"bk_msg_{txn_id}")
        await query.edit_message_text(f"✅ Confirmed as {reconcile_type}.", parse_mode=None)


async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bank — show bank balance and recent transactions, or prompt to connect."""
    from tools.plaid_tools import is_connected, fetch_accounts, get_recent_transactions, sync_transactions

    connected = await is_connected(get_active_store())

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
        result = await sync_transactions(get_active_store())
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
    txns = await get_recent_transactions(get_active_store(), days=7)
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

    # Send review cards, auto-classified alerts, CC settlements via shared helper
    await notify_bank_sync_results(result, context.bot)


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sync — manually trigger the nightly Sheets → DB sync right now."""
    from tools.sync import run_nightly_sync
    await update.message.reply_text("🔄 Syncing Google Sheets → database...", parse_mode=None)
    try:
        await run_nightly_sync(get_active_store())
        await update.message.reply_text("✅ Sync complete. You can now query sales, expenses, and more.", parse_mode=None)
    except Exception as e:
        log.error("Manual sync failed: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Sync failed: {e}", parse_mode=None)




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
    await save_cached_token(get_active_store(), token)
    await update.message.reply_text(
        f"✅ NRS token saved. Send /daily to test it.",
        parse_mode=None,
    )


def build_app() -> Application:
    global _bot_instance
    app = Application.builder().token(settings.telegram_bot_token).build()
    _bot_instance = app.bot

    # Guard: reject updates from any chat not in platform.stores (runs first, group=-1)
    app.add_handler(TypeHandler(Update, _guard_known_store), group=-1)

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

    # Daily report — plain command handler. State (_STATE_SALES in DB) is the
    # source of truth; the message handler in handle_text reads it and routes
    # appropriately. No ConversationHandler — it caused two competing handlers
    # for the same messages, which broke owner Q&A while a report was pending.
    app.add_handler(onboarding_conv)
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("invoice", cmd_invoice))
    app.add_handler(CommandHandler("vendors", cmd_vendors))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("order", cmd_order))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("bank", cmd_bank))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CommandHandler("token", cmd_token))
    app.add_handler(CallbackQueryHandler(handle_bank_callback, pattern=r"^(bk|bks:|bko:|cc_resolve:|cc_pick:|cc_skip:)"))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_invoice_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_invoice_photo))
    # Plain-text invoice entries (outside conversation) e.g. "heidelburg 500 3/9"
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text_invoice))
    return app
