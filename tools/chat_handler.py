"""
tools/chat_handler.py

Processes a plain-text message exactly like the Telegram bot does,
but returns a string instead of calling reply_text.

Used by the web chat API so the web UI works identically to Telegram.
"""

import asyncio
import logging
import re
from datetime import date
from typing import Any

from db.ops import (
    save_daily_sales, save_expense, save_invoice,
    save_invoice_items, save_rebate, save_revenue, save_vendor_price,
)
from db.state import clear_state, get_state, save_state
from tools.intent_router import classify_message
from tools.nrs_tools import fetch_daily_sales
from tools.price_lookup import _compile_order_async, _lookup_item_price_async, parse_order_list
from tools.query_agent import answer_query
from tools.reports import save_daily_report
from tools.sheets_tools import (
    log_cogs_entry, log_daily_sales, log_expense,
    log_rebate, log_revenue, resolve_vendor,
)
from tools.vendor_agent import get_vendor_comparison
from tools.health_score import _build_health_score_async

log = logging.getLogger(__name__)

_STATE_SALES = "sales"
_STATE_INVOICE_ITEMS = "invoice_items"


# ---------------------------------------------------------------------------
# Formatting helpers (mirrored from bot.py)
# ---------------------------------------------------------------------------

def _fmt_left(sales: dict[str, Any]) -> str:
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
    grand_total = round(product_sales + lotto_in + lotto_online + sales_tax + gpi, 2)
    sales["grand_total"] = grand_total

    lines = [
        f"📊 *{sales.get('day_of_week', '')} {sales.get('date', '')}*",
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


# ---------------------------------------------------------------------------
# Pure helpers (mirrored from bot.py — no Telegram dependency)
# ---------------------------------------------------------------------------

def _parse_right_side(text: str) -> dict[str, float] | None:
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
            pattern = re.escape(alias) + r"[\s:]*(\d+\.?\d*)"
            m = re.search(pattern, text_lower)
            if m:
                result[key] = to_float(m.group(1))
                break
        if key not in result:
            result[key] = 0.0

    if not any(result.values()):
        nums = re.findall(r"\d+\.?\d*", text)
        if len(nums) == 3:
            result = {k: to_float(v) for k, v in zip(["lotto_po", "lotto_cr", "food_stamp"], nums)}
        else:
            return None

    return result


def _build_complete_sheet(sales: dict, right: dict) -> str:
    product_sales = sales.get("product_sales", 0)
    lotto_in = sales.get("lotto_in", 0)
    lotto_online = sales.get("lotto_online", 0)
    sales_tax = sales.get("sales_tax", 0)
    gpi = sales.get("gpi", 0)
    grand_total = sales.get("grand_total", 0)

    cash = sales.get("cash_drops", 0)
    card = sales.get("card", 0)
    check = sales.get("check", 0)
    atm = sales.get("atm", 0)
    pull_tab = sales.get("pull_tab", 0)
    coupon = sales.get("coupon", 0)
    loyalty = sales.get("loyalty", 0)
    vendor = sales.get("vendor", 0)

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

    depts = sales.get("departments", [])
    dept_lines = "\n".join(
        f"  {d['name']:<20} ${d['sales']:>8.2f}" for d in depts
    )

    def r(label, val):
        return f"  {label:<20} {'—':>9}" if val == 0 else f"  {label:<20} ${val:>8.2f}"

    return (
        f"✅ COMPLETE — {sales['day_of_week']} {sales['date']}\n"
        f"{'─'*34}\n"
        f"  PRODUCT SALES\n{dept_lines}\n{'─'*34}\n"
        f"  {'TOTAL':<20} ${product_sales:>8.2f}\n\n"
        f"  IN. LOTTO            ${lotto_in:>8.2f}\n"
        f"  ON. LINE             ${lotto_online:>8.2f}\n"
        f"  SALES TAX            ${sales_tax:>8.2f}\n"
        f"  GPI                  ${gpi:>8.2f}\n"
        f"{'─'*34}\n"
        f"  GRAND TOTAL          ${grand_total:>8.2f}\n\n"
        f"  PAYMENTS\n"
        f"{r('LOTTO P.O', lotto_po)}\n{r('LOTTO CR.', lotto_cr)}\n"
        f"{r('ATM', atm)}\n"
        f"  {'CASH DROP':<20} ${cash:>8.2f}\n"
        f"  {'C.CARD':<20} ${card:>8.2f}\n"
        f"{r('COUPON', coupon)}\n{r('PULL TAB', pull_tab)}\n"
        f"{r('FOOD STAMP', food_stamp)}\n{r('LOYALTY', loyalty)}\n"
        f"{r('VENDOR PAYOUT', vendor)}\n"
        f"{'─'*34}\n"
        f"  TOTAL PAYMENTS       ${total_right:>8.2f}\n"
        f"{'─'*34}\n"
        f"  {over_short}"
    )


def _parse_entry(text: str) -> dict | None:
    amount_match = re.search(r"\$?(\d+(?:\.\d{1,2})?)", text)
    if not amount_match:
        return None
    amount = float(amount_match.group(1))

    date_match = re.search(r"(\d{4}-\d{2}-\d{2})|(\d{1,2}/\d{1,2}(?:/\d{2,4})?)", text)
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

    label_text = re.sub(r"\$?\d+(?:\.\d{1,2})?", "", text)
    label_text = re.sub(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?", "", label_text)
    label_text = re.sub(r"\b(rebate|expense|revenue|profit|took|home|paid|payment)\b", "", label_text, flags=re.IGNORECASE)
    label = " ".join(label_text.split()).strip(" :-")

    if not label:
        return None
    return {"label": label, "amount": amount, "entry_date": entry_date}


def _extract_invoice_fields(text: str) -> dict | None:
    """
    Use Claude Haiku to extract vendor, amount, and date from natural language.
    Falls back to regex if the API call fails.
    """
    import anthropic as _anthropic
    from config.settings import settings as _settings

    try:
        client = _anthropic.Anthropic(api_key=_settings.anthropic_api_key)
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
            # Try regex fallback for amount
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


def _parse_invoice_text_regex(text: str) -> dict | None:
    """Regex fallback for structured inputs like: mclane $2100 3/14"""
    amount_match = re.search(r"\$?(\d+(?:\.\d{1,2})?)", text)
    if not amount_match:
        return None
    amount = float(amount_match.group(1))

    date_match = re.search(r"(\d{4}-\d{2}-\d{2})|(\d{1,2}/\d{1,2}(?:/\d{2,4})?)", text)
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


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def process_message(text: str, store_id: str) -> str:
    """
    Process a web chat message exactly like the Telegram bot.
    Returns a reply string.
    """
    text = text.strip()

    # ── /command shortcuts ────────────────────────────────────────────────────
    lower = text.lower()

    if lower.startswith("/daily"):
        try:
            sales = await asyncio.get_event_loop().run_in_executor(None, fetch_daily_sales)
            await save_state(store_id, _STATE_SALES, sales)
            return _fmt_left(sales) + _prompt_for_right_side()
        except Exception as e:
            log.error("Daily fetch failed: %s", e, exc_info=True)
            return f"❌ Error fetching data: {e}"

    if lower.startswith("/price "):
        query = text[7:].strip()
        return await _lookup_item_price_async(query, store_id=store_id)

    if lower.startswith("/price"):
        return "Usage: /price <item name>\nExample: /price marlboro red short"

    if lower.startswith("/order"):
        items_text = text[6:].strip()
        if not items_text:
            await save_state(store_id, "awaiting_order", {"pending": True})
            return (
                "Send your order list — one item per line:\n\n"
                "marlboro red short x5\ncoke 20oz x10\ndoritos nacho x3\n\n"
                "I'll show totals per vendor and the cheapest option."
            )
        items = parse_order_list(items_text)
        if not items:
            return "⚠️ Could not parse item list. One item per line."
        return await _compile_order_async(items, store_id=store_id)

    if lower.startswith("/vendors"):
        category = text[8:].strip().upper() or None
        try:
            return await asyncio.get_event_loop().run_in_executor(None, get_vendor_comparison, category)
        except Exception as e:
            return f"⚠️ Error: {e}"

    if lower.startswith("/health"):
        return await _build_health_score_async(store_id)

    if lower.startswith("/invoice "):
        inv_text = text[9:].strip()
        return await _handle_invoice_text_web(inv_text, store_id)

    if lower.startswith("/cancel"):
        await clear_state(store_id, _STATE_SALES)
        await clear_state(store_id, _STATE_INVOICE_ITEMS)
        await clear_state(store_id, "awaiting_order")
        return "Cancelled."

    # ── Priority 1: pending daily sheet ──────────────────────────────────────
    sales = await get_state(store_id, _STATE_SALES)
    if sales:
        right = _parse_right_side(text)
        if right is not None:
            sheet_msg = _build_complete_sheet(sales, right)
            try:
                sales_for_sheet = dict(sales)
                sales_for_sheet.update(right)
                log_daily_sales(sales_for_sheet)
                await save_daily_sales(store_id, sales, right)
                save_daily_report(store_id, sales, right)
                await clear_state(store_id, _STATE_SALES)
                return sheet_msg + "\n\n✅ Logged to Google Sheets."
            except Exception as e:
                await clear_state(store_id, _STATE_SALES)
                return sheet_msg + f"\n\n⚠️ Sheets logging failed: {e}"
        else:
            return "⚠️ Could not parse. Reply with:\nLOTTO PO: 16\nLOTTO CR: 0\nFOOD STAMP: 27.97"

    # ── Priority 2: pending invoice confirmation ──────────────────────────────
    pending_items = await get_state(store_id, _STATE_INVOICE_ITEMS)
    if pending_items:
        answer = text.strip().upper()
        if answer in ("YES", "Y", "SAVE", "OK", "CONFIRM"):
            try:
                vendor = pending_items.get("vendor", "UNKNOWN")
                raw_date = pending_items.get("invoice_date", "")
                try:
                    inv_date = date.fromisoformat(raw_date) if raw_date else date.today()
                except ValueError:
                    inv_date = date.today()
                inv_num = pending_items.get("invoice_number", "")
                items = pending_items.get("items", [])
                total_amount = sum(
                    float(i.get("case_price") or 0) or float(i.get("unit_price") or 0)
                    for i in items
                )
                invoice_id = await save_invoice(store_id, vendor, total_amount, inv_date, inv_num)
                count = await save_invoice_items(
                    store_id, vendor, items, inv_date, invoice_id
                )
                # Write to Google Sheets COGS section
                try:
                    log_cogs_entry(vendor=vendor, amount=total_amount, entry_date=inv_date)
                except Exception as sheet_err:
                    log.warning("Sheets COGS write failed: %s", sheet_err)
                await clear_state(store_id, _STATE_INVOICE_ITEMS)
                return f"✅ Saved {count} items from {vendor} ({inv_date}).\nUse /price <item> to look up prices."
            except Exception as e:
                return f"⚠️ Save failed: {e}"
        elif answer in ("NO", "N", "DISCARD", "CANCEL"):
            await clear_state(store_id, _STATE_INVOICE_ITEMS)
            return "🗑️ Invoice discarded."
        else:
            return "Reply YES to save the extracted items or NO to discard."

    # ── Priority 3: pending order list ────────────────────────────────────────
    awaiting_order = await get_state(store_id, "awaiting_order")
    if awaiting_order:
        await clear_state(store_id, "awaiting_order")
        items = parse_order_list(text)
        if not items:
            return "⚠️ Could not parse item list. One item per line."
        return await _compile_order_async(items, store_id=store_id)

    # ── Priority 4: daily fetch starts the state machine; everything else → unified agent ──
    intent = await asyncio.get_event_loop().run_in_executor(None, classify_message, text)
    log.info("Web chat intent: %s | %s", intent, text[:60])

    if intent == "daily_fetch":
        try:
            sales = await asyncio.get_event_loop().run_in_executor(None, fetch_daily_sales)
            await save_state(store_id, _STATE_SALES, sales)
            return _fmt_left(sales) + _prompt_for_right_side()
        except Exception as e:
            log.error("Daily fetch failed: %s", e, exc_info=True)
            return f"❌ Error fetching data: {e}"
    else:
        from tools.main_agent import run_agent
        try:
            return await asyncio.get_event_loop().run_in_executor(None, run_agent, text, store_id)
        except Exception as e:
            log.error("Agent failed: %s", e, exc_info=True)
            return f"⚠️ Something went wrong: {e}"


# ---------------------------------------------------------------------------
# Action handlers (return strings)
# ---------------------------------------------------------------------------

async def _handle_expense_web(text: str, store_id: str) -> str:
    parsed = _parse_entry(text)
    if not parsed:
        return "⚠️ Could not parse. Try: electricity $340 march 10"
    try:
        await save_expense(store_id, parsed["label"], parsed["amount"], parsed["entry_date"])
        log_expense(parsed["label"], parsed["amount"], parsed["entry_date"])
        return f"✅ Expense logged\n{parsed['label'].title()} — ${parsed['amount']:.2f} on {parsed['entry_date']}"
    except Exception as e:
        return f"⚠️ Failed: {e}"


async def _handle_rebate_web(text: str, store_id: str) -> str:
    parsed = _parse_entry(text)
    if not parsed:
        return "⚠️ Could not parse. Try: pmhelix rebate $820"
    try:
        await save_rebate(store_id, parsed["label"], parsed["amount"], parsed["entry_date"])
        log_rebate(parsed["label"], parsed["amount"], parsed["entry_date"])
        return f"✅ Rebate logged\n{parsed['label'].title()} — ${parsed['amount']:.2f} on {parsed['entry_date']}"
    except Exception as e:
        return f"⚠️ Failed: {e}"


async def _handle_revenue_web(text: str, store_id: str) -> str:
    parsed = _parse_entry(text)
    if not parsed:
        return "⚠️ Could not parse. Try: car payment $300"
    try:
        await save_revenue(store_id, parsed["label"], parsed["amount"], parsed["entry_date"])
        log_revenue(parsed["label"], parsed["amount"], parsed["entry_date"])
        return f"✅ Revenue logged\n{parsed['label'].title()} — ${parsed['amount']:.2f} on {parsed['entry_date']}"
    except Exception as e:
        return f"⚠️ Failed: {e}"


async def _handle_invoice_text_web(text: str, store_id: str) -> str:
    parsed = await asyncio.get_event_loop().run_in_executor(None, _extract_invoice_fields, text)
    if not parsed:
        return "⚠️ Could not parse. Try: mclane $2100 3/14"

    vendor_match = resolve_vendor(parsed["vendor"])
    if not vendor_match:
        words = parsed["vendor"].split()
        for i in range(len(words), 0, -1):
            vendor_match = resolve_vendor(" ".join(words[:i]))
            if vendor_match:
                break

    if not vendor_match:
        return (
            f"⚠️ Vendor '{parsed['vendor']}' not recognised.\n"
            "Use /invoice to log or check vendor name."
        )

    try:
        invoice_id = await save_invoice(store_id, vendor_match, parsed["amount"], parsed["entry_date"])
        await save_vendor_price(store_id, vendor_match, parsed["amount"], parsed["entry_date"], invoice_id)
        log_cogs_entry(vendor=vendor_match, amount=parsed["amount"], entry_date=parsed["entry_date"])
        return f"✅ Invoice logged\nVendor: {vendor_match}\nAmount: ${parsed['amount']:.2f}\nDate: {parsed['entry_date']}"
    except Exception as e:
        return f"⚠️ Failed: {e}"
