"""
tools/bank_reconciler.py

Bank transaction reconciliation engine.

After every Plaid sync:
  1. Rule lookup  — check TransactionRule table for known patterns (instant, no AI call)
  2. AI categorize — Claude Haiku classifies unknown transactions with a confidence score
  3. Low-confidence (<95%) → mark review_status="pending", queue Telegram confirmation
  4. User confirms → save TransactionRule so future identical txns are auto-categorized
  5. CC settlement matching — compare bank CC deposits to DailySales.card for each day
  6. Report mismatches via Telegram bot (called externally)

Public API:
  reconcile_new_transactions(store_id) → {"needs_review": [...], "cc_mismatches": [...]}
  learn_rule(store_id, pattern, reconcile_type, subcategory) → saved rule
  get_pending_reviews(store_id) → list of BankTransaction dicts
  confirm_transaction(store_id, txn_id, reconcile_type, subcategory, sender) → True
  skip_transaction(store_id, txn_id) → True
  check_cc_settlements(store_id) → list of mismatch dicts
"""

import json
import logging
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import anthropic

from config.settings import settings

log = logging.getLogger(__name__)

# ── Patterns that are always auto-classified without AI ──────────────────────

_INSTANT_RULES: list[tuple[str, str, str | None]] = [
    # (substring, reconcile_type, subcategory)
    # CC settlements
    ("square",           "cc_settlement", None),
    ("stripe",           "cc_settlement", None),
    ("paymentech",       "cc_settlement", None),
    ("heartland",        "cc_settlement", None),
    ("tsys",             "cc_settlement", None),
    ("firstdata",        "cc_settlement", None),
    ("elavon",           "cc_settlement", None),
    ("worldpay",         "cc_settlement", None),
    ("visa batch",       "cc_settlement", None),
    ("mc batch",         "cc_settlement", None),
    ("mastercard batch", "cc_settlement", None),
    ("card deposit",     "cc_settlement", None),
    ("bankcard",         "cc_settlement", None),
    ("ach credit",       "cc_settlement", None),
    ("merchant bankcd",  "cc_settlement", None),
    # Lottery
    ("lottery inv",      "expense",  "LOTTERY"),
    ("lottery",          "expense",  "LOTTERY"),
    ("lotto",            "expense",  "LOTTERY"),
    # ATM settlement
    ("cash depot",       "rebate",   "ATM"),
    ("atm settle",       "rebate",   "ATM"),
    # Tobacco/vendor rebates
    ("pm usa",           "rebate",   "PM USA"),
    ("altria",           "rebate",   "ALTRIA"),
    ("rai services",     "rebate",   "RAI"),
    ("reynolds",         "rebate",   "REYNOLDS"),
    ("liggett",          "rebate",   "LIGGETT"),
    # NRS
    ("national retail",  "expense",  "NRS"),
]

def _check_instant_rules(description: str) -> tuple[str, str | None] | None:
    """Check if a transaction description matches any hardcoded instant rule."""
    desc_lower = description.lower()
    for substring, rtype, subcat in _INSTANT_RULES:
        if substring in desc_lower:
            return (rtype, subcat)
    return None


_CC_KEYWORDS = {
    "square", "stripe", "paymentech", "heartland", "tsys", "firstdata",
    "elavon", "worldpay", "visa", "mastercard", "bankcard", "card batch",
    "card deposit", "ach credit", "settlement",
}

_ANTHROPIC_CLIENT: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _ANTHROPIC_CLIENT
    if _ANTHROPIC_CLIENT is None:
        _ANTHROPIC_CLIENT = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _ANTHROPIC_CLIENT


# ── Rule lookup ───────────────────────────────────────────────────────────────

async def _lookup_rule(store_id: str, description: str) -> dict | None:
    """
    Check instant rules first, then DB learned rules.
    Returns {"reconcile_type", "reconcile_subcategory", "confidence"} or None.
    """
    desc_lower = description.lower()

    # 1. Instant hardcoded rules
    for pattern, rtype, subcat in _INSTANT_RULES:
        if pattern in desc_lower:
            return {"reconcile_type": rtype, "reconcile_subcategory": subcat, "confidence": 1.0}

    # 2. DB learned rules — reads from platform.store_bank_rules
    from sqlalchemy import select
    from db.database import get_async_session
    from db.models import StoreBankRule

    async with get_async_session() as session:
        rules = (await session.execute(
            select(StoreBankRule).where(StoreBankRule.store_id == store_id)
            .order_by(StoreBankRule.confirmed_count.desc())
        )).scalars().all()

    for rule in rules:
        if rule.pattern.lower() in desc_lower:
            # Confidence scales with how many times user confirmed: 1=0.90, 3=0.97, 5+=0.99
            confidence = min(0.99, 0.90 + (rule.confirmed_count - 1) * 0.03)
            return {
                "reconcile_type": rule.reconcile_type,
                "reconcile_subcategory": rule.reconcile_subcategory,
                "confidence": confidence,
            }

    return None


# ── AI categorizer ────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a gas station bookkeeping assistant. Classify bank transactions for a convenience store / gas station.\n\n"
    "Transaction types:\n"
    "  cc_settlement  — credit card processor batch deposit (Square, Stripe, Heartland, TSYS, etc.)\n"
    "  invoice        — vendor payment / COGS (McLane, Core-Mark, US Foods, beer/tobacco/grocery distributors)\n"
    "  expense        — operating expense (rent, insurance, utilities, maintenance, payroll, supplies)\n"
    "  rebate         — vendor rebate / incentive credit (Altria, RJ Reynolds, core-mark rebate)\n"
    "  payroll        — employee payroll / payroll service\n"
    "  skip           — bank fee, transfer between own accounts, NSF, interest — not meaningful to log\n\n"
    "Reply ONLY with a JSON object (no markdown):\n"
    '{"reconcile_type":"<type>","subcategory":"<vendor or expense category or null>","confidence":<0.0-1.0>,"reason":"<1 sentence>"}'
)


async def _ai_categorize(description: str, amount: float) -> dict:
    """Call Claude Haiku to classify a single transaction. Returns categorization dict."""
    client = _get_client()
    prompt = f"Transaction: {description!r}  Amount: ${amount:.2f} ({'debit' if amount > 0 else 'deposit'})"
    try:
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        return json.loads(raw)
    except Exception as e:
        log.warning("AI categorize failed for %r: %s", description, e)
        return {"reconcile_type": "skip", "subcategory": None, "confidence": 0.0, "reason": "AI error"}


# ── Google Sheet smart matching ───────────────────────────────────────────────

async def _match_to_sheet(description: str, amount: float) -> dict | None:
    """
    Try to match a bank transaction to an existing Google Sheet entry.

    Direction:  amount < 0 → money IN  (rebate, CC settlement)
                amount > 0 → money OUT (invoice/COGS, expense)

    Returns dict with keys: match_type, vendor, entry_date, proposed (bool)
    or None if no sheet match found.
    """
    import asyncio
    from tools.sheets_tools import (
        match_description_to_cogs_vendor,
        match_description_to_expense,
        match_description_to_rebate,
        find_cogs_by_vendor,
        find_cogs_by_amount,
        find_expense_by_category,
        find_expense_by_amount,
        find_rebate_by_vendor,
    )

    loop       = asyncio.get_event_loop()
    abs_amount = abs(amount)
    is_deposit = amount < 0
    is_check   = bool(re.match(r"^check\s+\d+", description.lower()))

    if is_deposit:
        # Money IN — look in rebate section by vendor name
        rebate_col = match_description_to_rebate(description)
        if rebate_col:
            match = await loop.run_in_executor(None, find_rebate_by_vendor, rebate_col, abs_amount)
            if match:
                return {"match_type": "rebate", "vendor": rebate_col,
                        "entry_date": match[0], "proposed": False}

    elif is_check:
        # CHECK with no vendor name — search COGS then expenses by amount
        cogs_hits = await loop.run_in_executor(None, find_cogs_by_amount, abs_amount)
        if cogs_hits:
            best = cogs_hits[0]
            return {"match_type": "invoice", "vendor": best[1],
                    "entry_date": best[0], "sheet_amount": best[2], "proposed": True}
        exp_hits = await loop.run_in_executor(None, find_expense_by_amount, abs_amount)
        if exp_hits:
            best = exp_hits[0]
            return {"match_type": "expense", "vendor": best[1],
                    "entry_date": best[0], "sheet_amount": best[2], "proposed": True}

    else:
        # ACH with vendor name — check COGS first, then expenses
        cogs_vendor = match_description_to_cogs_vendor(description)
        if cogs_vendor:
            match = await loop.run_in_executor(None, find_cogs_by_vendor, cogs_vendor, abs_amount)
            if match:
                return {"match_type": "invoice", "vendor": cogs_vendor,
                        "entry_date": match[0], "proposed": False}

        exp_col = match_description_to_expense(description)
        if exp_col:
            match = await loop.run_in_executor(None, find_expense_by_category, exp_col, abs_amount)
            if match:
                return {"match_type": "expense", "vendor": exp_col,
                        "entry_date": match[0], "proposed": False}

    return None


async def _highlight_sheet_match(sheet_match: dict) -> None:
    """Highlight the matched Google Sheet cell green after bank confirms it."""
    import asyncio
    from tools.sheets_tools import mark_invoice_paid, mark_expense_paid, mark_rebate_paid

    loop       = asyncio.get_event_loop()
    match_type = sheet_match["match_type"]
    vendor     = sheet_match["vendor"]
    entry_date = sheet_match["entry_date"]
    try:
        if match_type == "invoice":
            await loop.run_in_executor(None, mark_invoice_paid, vendor, entry_date)
        elif match_type == "expense":
            await loop.run_in_executor(None, mark_expense_paid, vendor, entry_date)
        elif match_type == "rebate":
            await loop.run_in_executor(None, mark_rebate_paid, vendor, entry_date)
    except Exception as e:
        log.warning("Sheet highlight failed for %s/%s: %s", match_type, vendor, e)


# ── Main reconcile entry ──────────────────────────────────────────────────────

async def reconcile_new_transactions(store_id: str) -> dict:
    """
    Process all bank_transactions with review_status='pending' for this store.
    Returns {"needs_review": [txn_dicts], "cc_mismatches": [mismatch_dicts], "auto_classified": int}.
    """
    import asyncio
    from sqlalchemy import select, and_
    from db.database import get_async_session
    from db.models import BankTransaction

    async with get_async_session() as session:
        pending = (await session.execute(
            select(BankTransaction).where(and_(
                BankTransaction.store_id == store_id,
                BankTransaction.review_status == "pending",
            )).order_by(BankTransaction.transaction_date.desc()).limit(100)
        )).scalars().all()

        needs_review = []
        auto_list    = []
        auto_count   = 0

        for txn in pending:
          try:
            desc   = txn.description
            amount = float(txn.amount)

            # 0. Check instant (hardcoded) rules first
            instant = _check_instant_rules(desc)
            if instant:
                txn.review_status        = "auto"
                txn.reconcile_type       = instant[0]
                txn.reconcile_subcategory = instant[1]
                auto_count += 1
                auto_list.append(_txn_to_dict(txn, confidence=1.0, ai_guess=instant[0]))
                continue

            # 1. Learned rules (previously confirmed patterns)
            rule = await _lookup_rule(store_id, desc)
            if rule and rule["confidence"] >= 0.95:
                txn.review_status        = "auto"
                txn.reconcile_type       = rule["reconcile_type"]
                txn.reconcile_subcategory = rule["reconcile_subcategory"]
                auto_count += 1
                auto_list.append(_txn_to_dict(txn, confidence=rule["confidence"],
                                              ai_guess=rule["reconcile_type"]))
                continue

            # 2. Google Sheet smart matching (with timeout — sheets API is slow)
            try:
                sheet_match = await asyncio.wait_for(_match_to_sheet(desc, amount), timeout=10.0)
            except asyncio.TimeoutError:
                log.warning("Sheet match timed out for %s", desc[:40])
                sheet_match = None

            if sheet_match and not sheet_match.get("proposed"):
                # Confident match (ACH vendor found in sheet) — auto-confirm + highlight
                txn.review_status        = "auto"
                txn.reconcile_type       = sheet_match["match_type"]
                txn.reconcile_subcategory = sheet_match["vendor"]
                auto_count += 1
                await _highlight_sheet_match(sheet_match)
                await _upsert_rule(store_id, desc, sheet_match["match_type"],
                                   sheet_match["vendor"], confirmed=True, session=session)
                auto_list.append(_txn_to_dict(txn, confidence=0.95,
                                              ai_guess=sheet_match["match_type"]))
                continue

            if sheet_match and sheet_match.get("proposed"):
                # Proposed match (check matched by amount) — ask user to confirm
                txn.review_status        = "needs_review"
                txn.reconcile_type       = sheet_match["match_type"]
                txn.reconcile_subcategory = sheet_match["vendor"]
                d = _txn_to_dict(txn, confidence=0.8, ai_guess=sheet_match["match_type"])
                d["sheet_match"] = sheet_match
                needs_review.append(d)
                continue

            # 3. AI for truly unknown transactions (with timeout)
            if rule is None:
                try:
                    ai = await asyncio.wait_for(_ai_categorize(desc, amount), timeout=15.0)
                except asyncio.TimeoutError:
                    log.warning("AI categorize timed out for %s", desc[:40])
                    ai = {"reconcile_type": "skip", "confidence": 0.0}
                confidence = ai.get("confidence", 0.0)
                rtype      = ai.get("reconcile_type", "skip")
                subcat     = ai.get("subcategory")
                if confidence >= 0.95:
                    txn.review_status        = "auto"
                    txn.reconcile_type       = rtype
                    txn.reconcile_subcategory = subcat
                    auto_count += 1
                    await _upsert_rule(store_id, desc, rtype, subcat, confirmed=False, session=session)
                    auto_list.append(_txn_to_dict(txn, confidence=confidence, ai_guess=rtype))
                else:
                    txn.review_status        = "needs_review"
                    txn.reconcile_type       = rtype
                    txn.reconcile_subcategory = subcat
                    needs_review.append(_txn_to_dict(txn, confidence=confidence, ai_guess=rtype))
            else:
                # Rule matched but < 95% confidence — ask user
                txn.review_status        = "needs_review"
                txn.reconcile_type       = rule["reconcile_type"]
                txn.reconcile_subcategory = rule["reconcile_subcategory"]
                needs_review.append(_txn_to_dict(txn, confidence=rule["confidence"],
                                                  ai_guess=rule["reconcile_type"]))
          except Exception as e:
            log.error("Reconcile failed for txn %s (%s): %s", txn.id, txn.description[:40], e, exc_info=True)
            txn.review_status = "needs_review"
            needs_review.append(_txn_to_dict(txn))

        await session.commit()

    try:
        cc_mismatches = await asyncio.wait_for(check_cc_settlements(store_id), timeout=30.0)
    except (asyncio.TimeoutError, Exception) as e:
        log.warning("CC settlement check failed/timed out: %s", e)
        cc_mismatches = []

    log.info(
        "Reconcile store=%s auto=%d needs_review=%d cc_mismatches=%d",
        store_id, auto_count, len(needs_review), len(cc_mismatches),
    )
    return {
        "needs_review":      needs_review,
        "auto_classified":   auto_count,
        "auto_list":         auto_list,
        "cc_mismatches":     cc_mismatches,
    }


def _txn_to_dict(txn: Any, confidence: float = 0.0, ai_guess: str = "") -> dict:
    return {
        "id":              txn.id,
        "date":            str(txn.transaction_date),
        "amount":          float(txn.amount),
        "description":     txn.description,
        "category":        txn.category,
        "type":            txn.transaction_type,
        "reconcile_type":  txn.reconcile_type,
        "reconcile_subcategory": txn.reconcile_subcategory,
        "review_status":   txn.review_status,
        "is_matched":      txn.is_matched,
        "confidence":      round(confidence, 2),
        "ai_guess":        ai_guess,
    }


# ── Rule learning ─────────────────────────────────────────────────────────────

async def _upsert_rule(store_id: str, description: str, reconcile_type: str, subcategory: str | None,
                       confirmed: bool = True, session=None) -> None:
    """
    Extract a canonical pattern from the description (first 5 significant words, lowercased),
    then upsert into platform.store_bank_rules.
    Fix point: if a transaction keeps asking for review, check platform.store_bank_rules
    for a matching pattern — either it's missing or the reconcile_type is wrong.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from db.database import get_async_session
    from db.models import StoreBankRule

    # Normalize: strip numbers/dates, take first 5 significant words
    words = re.sub(r"[0-9#\-/*]+", " ", description.lower()).split()
    words = [w for w in words if len(w) > 2][:5]
    if not words:
        return
    pattern = " ".join(words)

    async def _do(s):
        stmt = pg_insert(StoreBankRule).values(
            store_id=store_id,
            pattern=pattern,
            reconcile_type=reconcile_type,
            reconcile_subcategory=subcategory,
            confirmed_count=1 if confirmed else 0,
        ).on_conflict_do_update(
            index_elements=["store_id", "pattern"],
            set_={
                "reconcile_type": reconcile_type,
                "reconcile_subcategory": subcategory,
                "confirmed_count": StoreBankRule.confirmed_count + (1 if confirmed else 0),
                "last_seen_at": date.today(),
            },
        )
        await s.execute(stmt)
        await s.commit()

    if session:
        await _do(session)
    else:
        async with get_async_session() as s:
            await _do(s)


async def learn_rule(store_id: str, txn_description: str, reconcile_type: str, subcategory: str | None) -> None:
    """Public API: called when user confirms a transaction categorization."""
    await _upsert_rule(store_id, txn_description, reconcile_type, subcategory, confirmed=True)
    log.info("Learned rule store=%s type=%s subcat=%s pattern from: %r", store_id, reconcile_type, subcategory, txn_description)


# ── Confirm / skip individual transactions ────────────────────────────────────

async def confirm_transaction(
    store_id: str,
    txn_id: int,
    reconcile_type: str,
    subcategory: str | None,
    sender: str = "user",
) -> dict | None:
    """
    Mark a transaction as confirmed, log it to the appropriate table, and learn the rule.
    Returns the updated transaction dict or None if not found.
    """
    from sqlalchemy import select
    from db.database import get_async_session
    from db.models import BankTransaction

    async with get_async_session() as session:
        txn = (await session.execute(
            select(BankTransaction).where(
                BankTransaction.id == txn_id,
                BankTransaction.store_id == store_id,
            )
        )).scalar_one_or_none()

        if not txn:
            return None

        txn.review_status = "confirmed"
        txn.reconcile_type = reconcile_type
        txn.reconcile_subcategory = subcategory
        txn.last_updated_by = sender
        await session.commit()

        description = txn.description
        amount = float(txn.amount)
        txn_date = txn.transaction_date

    # Learn the rule
    await learn_rule(store_id, description, reconcile_type, subcategory)

    # Auto-log to appropriate table
    await _auto_log(store_id, txn_id, txn_date, amount, description, reconcile_type, subcategory)

    return {
        "id": txn_id,
        "reconcile_type": reconcile_type,
        "reconcile_subcategory": subcategory,
        "review_status": "confirmed",
    }


async def skip_transaction(store_id: str, txn_id: int) -> bool:
    """Mark a transaction as skipped (bank fee, own transfer, etc.)."""
    from sqlalchemy import select
    from db.database import get_async_session
    from db.models import BankTransaction

    async with get_async_session() as session:
        txn = (await session.execute(
            select(BankTransaction).where(
                BankTransaction.id == txn_id,
                BankTransaction.store_id == store_id,
            )
        )).scalar_one_or_none()

        if not txn:
            return False

        txn.review_status = "skipped"
        txn.reconcile_type = "skip"
        await session.commit()
        description = txn.description

    await learn_rule(store_id, description, "skip", None)
    return True


async def confirm_auto_transaction(store_id: str, txn_id: int) -> bool:
    """
    Confirm an auto-classified transaction as-is (user tapped ✅ Correct).
    Promotes review_status from 'auto' → 'confirmed' and reinforces the rule.
    """
    from sqlalchemy import select
    from db.database import get_async_session
    from db.models import BankTransaction

    async with get_async_session() as session:
        txn = (await session.execute(
            select(BankTransaction).where(
                BankTransaction.id == txn_id,
                BankTransaction.store_id == store_id,
            )
        )).scalar_one_or_none()

        if not txn:
            return False

        reconcile_type = txn.reconcile_type
        subcategory    = txn.reconcile_subcategory
        description    = txn.description
        amount         = float(txn.amount)
        txn_date       = txn.transaction_date

        txn.review_status     = "confirmed"
        txn.last_updated_by   = "user"
        await session.commit()

    await learn_rule(store_id, description, reconcile_type, subcategory)
    await _auto_log(store_id, txn_id, txn_date, amount, description, reconcile_type, subcategory)
    return True


async def _auto_log(
    store_id: str,
    txn_id: int,
    txn_date: date,
    amount: float,
    description: str,
    reconcile_type: str,
    subcategory: str | None,
) -> None:
    """
    Auto-log confirmed transactions to DB tables AND Google Sheet.
    - expense  → DB Expense + sheet log_expense + highlight green
    - invoice  → DB Invoice + sheet mark_invoice_paid (highlight green)
    - rebate   → DB Rebate  + sheet log_rebate + highlight green
    - payroll  → sheet log_payroll
    """
    import asyncio
    from decimal import Decimal
    from db.database import get_async_session
    from db.models import Expense, Invoice, Rebate

    dec_amount = Decimal(str(abs(amount)))
    float_amount = float(dec_amount)
    label = subcategory or description[:64]
    loop = asyncio.get_event_loop()

    if reconcile_type == "expense":
        async with get_async_session() as session:
            from sqlalchemy import select, and_
            existing = (await session.execute(
                select(Expense).where(and_(
                    Expense.store_id == store_id,
                    Expense.expense_date == txn_date,
                    Expense.category == label,
                    Expense.amount == dec_amount,
                ))
            )).scalar_one_or_none()
            if not existing:
                session.add(Expense(
                    store_id=store_id,
                    expense_date=txn_date,
                    category=label,
                    amount=dec_amount,
                    notes=f"Auto-logged from bank txn #{txn_id}",
                    last_updated_by="bank_reconciler",
                ))
                await session.commit()
                log.info("Auto-logged expense store=%s %s $%.2f", store_id, label, float_amount)

        # Write to Google Sheet (log if empty) + highlight green
        try:
            from tools.sheets_tools import log_expense_and_highlight
            result = await loop.run_in_executor(
                None, log_expense_and_highlight, label, float_amount, txn_date
            )
            log.info("Sheet: %s", result)
        except Exception as e:
            log.warning("Sheet expense write failed for %s: %s", label, e)

    elif reconcile_type == "invoice":
        async with get_async_session() as session:
            from sqlalchemy import select, and_
            vendor_label = subcategory or description[:128]
            existing = (await session.execute(
                select(Invoice).where(and_(
                    Invoice.store_id == store_id,
                    Invoice.invoice_date == txn_date,
                    Invoice.vendor == vendor_label,
                    Invoice.amount == dec_amount,
                ))
            )).scalar_one_or_none()
            if not existing:
                session.add(Invoice(
                    store_id=store_id,
                    vendor=vendor_label,
                    amount=dec_amount,
                    invoice_date=txn_date,
                    matched_bank_transaction_id=txn_id,
                    last_updated_by="bank_reconciler",
                ))
                await session.commit()
                log.info("Auto-logged invoice store=%s vendor=%s $%.2f", store_id, vendor_label, float_amount)

        # Write to Google Sheet (log COGS if empty) + highlight green
        try:
            from tools.sheets_tools import log_invoice_and_highlight
            result = await loop.run_in_executor(
                None, log_invoice_and_highlight, vendor_label, float_amount, txn_date
            )
            log.info("Sheet: %s", result)
        except Exception as e:
            log.warning("Sheet invoice write failed for %s: %s", subcategory, e)

    elif reconcile_type == "rebate":
        async with get_async_session() as session:
            from sqlalchemy import select, and_
            vendor_label = subcategory or description[:128]
            existing = (await session.execute(
                select(Rebate).where(and_(
                    Rebate.store_id == store_id,
                    Rebate.rebate_date == txn_date,
                    Rebate.vendor == vendor_label,
                    Rebate.amount == dec_amount,
                ))
            )).scalar_one_or_none()
            if not existing:
                session.add(Rebate(
                    store_id=store_id,
                    rebate_date=txn_date,
                    vendor=vendor_label,
                    amount=dec_amount,
                    notes=f"Auto-logged from bank txn #{txn_id}",
                    last_updated_by="bank_reconciler",
                ))
                await session.commit()
                log.info("Auto-logged rebate store=%s vendor=%s $%.2f", store_id, vendor_label, float_amount)

        # Write to Google Sheet (log if empty) + highlight green
        try:
            from tools.sheets_tools import log_rebate_and_highlight
            result = await loop.run_in_executor(
                None, log_rebate_and_highlight, vendor_label, float_amount, txn_date
            )
            log.info("Sheet: %s", result)
        except Exception as e:
            log.warning("Sheet rebate write failed for %s: %s", subcategory, e)

    elif reconcile_type == "payroll":
        # Write to Google Sheet (log if empty) + highlight green
        try:
            from tools.sheets_tools import log_payroll_and_highlight
            employee_label = subcategory or description[:64]
            result = await loop.run_in_executor(
                None, log_payroll_and_highlight, employee_label, float_amount, txn_date
            )
            log.info("Sheet: %s", result)
        except Exception as e:
            log.warning("Sheet payroll write failed for %s: %s", subcategory, e)


# ── Pending review queue ──────────────────────────────────────────────────────

async def get_auto_reviews(store_id: str) -> list[dict]:
    """Return auto-classified transactions that haven't been confirmed yet."""
    from sqlalchemy import select
    from db.database import get_async_session
    from db.models import BankTransaction

    async with get_async_session() as session:
        rows = (await session.execute(
            select(BankTransaction).where(
                BankTransaction.store_id == store_id,
                BankTransaction.review_status == "auto",
            ).order_by(BankTransaction.transaction_date.desc()).limit(50)
        )).scalars().all()

    return [_txn_to_dict(r) for r in rows]


async def get_pending_reviews(store_id: str) -> list[dict]:
    """Return all transactions awaiting user categorization (pending + needs_review)."""
    from sqlalchemy import select
    from db.database import get_async_session
    from db.models import BankTransaction

    async with get_async_session() as session:
        rows = (await session.execute(
            select(BankTransaction).where(
                BankTransaction.store_id == store_id,
                BankTransaction.review_status.in_(["needs_review", "pending"]),
            ).order_by(BankTransaction.transaction_date.desc()).limit(50)
        )).scalars().all()

    return [_txn_to_dict(r) for r in rows]


# ── CC settlement matching ────────────────────────────────────────────────────

# CC settlement matching tolerances
# deposit can be up to $1 short (red flag: real shortage, call processor)
# deposit can be up to $30 over (fine: rounding, tips, small processor credits)
_CC_TOLERANCE_SHORT = 1.00   # deposit >= card - 1.00
_CC_TOLERANCE_OVER  = 30.00  # deposit <= card + 30.00


def _is_tight_match(deposit: float, card: float) -> bool:
    """Chronological tight match: deposit within [card - $1, card + $30]."""
    diff = deposit - card
    return -_CC_TOLERANCE_SHORT <= diff <= _CC_TOLERANCE_OVER


def _find_tight_ranges(
    daily_rows: list,
    deposit: float,
) -> list[tuple[int, int, float]]:
    """
    Find all contiguous ranges of daily_rows whose card totals sum to a
    tight match with the deposit amount. Handles processors that batch
    multiple days into one deposit (e.g. Thu+Fri+Sat → Monday deposit).

    daily_rows must be sorted oldest-first.
    Returns list of (start_idx, end_idx_inclusive, sum) tuples.
    """
    matches: list[tuple[int, int, float]] = []
    n = len(daily_rows)
    for i in range(n):
        running = 0.0
        for j in range(i, n):
            running += float(daily_rows[j].card)
            if running > deposit + _CC_TOLERANCE_OVER:
                break  # any further extension will only overshoot more
            if _is_tight_match(deposit, running):
                matches.append((i, j, round(running, 2)))
    return matches


async def check_cc_settlements(store_id: str) -> list[dict]:
    """
    Match NEW bank CC deposit transactions to DailySales.card totals.
    Settlement usually arrives 1-5 business days after the sale day, and
    processors frequently batch multiple days into a single deposit (e.g.
    Thu+Fri+Sat combined into a Monday deposit).

    Algorithm (chronological, conservative, supports multi-day batching):
      1. Process unmatched CC deposits oldest-first.
      2. Window per deposit: sale_date in [dep_date - 7, dep_date],
         excluding already-settled days (cc_settled_at IS NOT NULL),
         sorted oldest-first.
      3. Search for contiguous day ranges whose card-total sum tight-matches
         the deposit. Tight match = sum within [deposit - $30, deposit + $1],
         i.e. deposit up to $1 short or $30 over the sum.
      4. Decision:
         - Exactly 1 tight range → auto-settle every day in that range with
           this deposit. If older days precede the range, warn about them
           (likely fee hold / held batch).
         - Multiple tight ranges → ambiguous. Do not auto-settle. Alert user
           with range options so they can resolve manually.
         - Zero tight ranges → real mismatch. Alert against the oldest
           unsettled day. Day stays unsettled until resolved.
      5. On auto-match: write cc_settled_at = now() and cc_bank_txn_id on
         every day in the matched range.
      6. Deposits are marked is_matched=True after reporting (match or mismatch)
         so they don't re-alert.

    Only processes CC deposits where BankTransaction.is_matched=False.

    Returns list of match/mismatch dicts with keys:
      bank_txn_id, bank_date, bank_amount, bank_desc, sale_date, sale_card,
      diff, matched, ambiguous (bool), skipped_days (list[str]),
      settled_days (list[str] — days included in the match)
    """
    import asyncio
    from datetime import datetime
    from sqlalchemy import select, and_
    from db.database import get_async_session
    from db.models import BankTransaction, DailySales

    results: list[dict] = []

    async with get_async_session() as session:
        since = date.today() - timedelta(days=14)
        cc_deposits = (await session.execute(
            select(BankTransaction).where(and_(
                BankTransaction.store_id == store_id,
                BankTransaction.transaction_date >= since,
                BankTransaction.amount < 0,  # deposit (money in)
                BankTransaction.reconcile_type == "cc_settlement",
                BankTransaction.is_matched == False,  # not yet notified
            )).order_by(BankTransaction.transaction_date.asc())  # oldest first
        )).scalars().all()

        for dep in cc_deposits:
            bank_amount = abs(float(dep.amount))
            dep_date = dep.transaction_date

            # Window: look backward up to 7 days, never forward.
            date_lo = dep_date - timedelta(days=7)
            date_hi = dep_date

            daily_rows = (await session.execute(
                select(DailySales).where(and_(
                    DailySales.store_id == store_id,
                    DailySales.sale_date.between(date_lo, date_hi),
                    DailySales.card > 0,
                    DailySales.cc_settled_at.is_(None),  # not already settled
                )).order_by(DailySales.sale_date.asc())  # oldest first
            )).scalars().all()

            # No unsettled sales in the window — either sales not yet entered
            # or everything is already settled. Skip this deposit (retry next run).
            if not daily_rows:
                continue

            # Search for any contiguous range of days summing to a tight match
            ranges = _find_tight_ranges(daily_rows, bank_amount)

            if len(ranges) == 1:
                i, j, total = ranges[0]
                matched_rows = daily_rows[i : j + 1]
                diff = round(bank_amount - total, 2)
                settled_days = [str(r.sale_date) for r in matched_rows]
                # Days older than the match that were skipped (possible fee hold)
                skipped = [str(r.sale_date) for r in daily_rows[:i]]

                range_label = (
                    settled_days[0] if len(settled_days) == 1
                    else f"{settled_days[0]} → {settled_days[-1]} ({len(settled_days)} days)"
                )

                entry = {
                    "bank_txn_id":  dep.id,
                    "bank_date":    str(dep_date),
                    "bank_amount":  bank_amount,
                    "bank_desc":    dep.description,
                    "sale_date":    range_label,
                    "sale_card":    total,
                    "diff":         diff,
                    "matched":      True,
                    "ambiguous":    False,
                    "skipped_days": skipped,
                    "settled_days": settled_days,
                }

                # Persist settlement on every matched row
                now = datetime.utcnow()
                for r in matched_rows:
                    r.cc_settled_at = now
                    r.cc_bank_txn_id = dep.id

                try:
                    from tools.sheets_tools import mark_cc_settled
                    loop = asyncio.get_event_loop()
                    for r in matched_rows:
                        await loop.run_in_executor(
                            None, mark_cc_settled, r.sale_date, float(r.card), dep_date,
                        )
                    log.info("CC settled: bank $%.2f on %s → %s total $%.2f (diff $%.2f)%s",
                             bank_amount, dep_date, range_label, total, diff,
                             f" [skipped={skipped}]" if skipped else "")
                except Exception as e:
                    log.warning("CC sheet highlight failed: %s", e)

                dep.is_matched = True
                results.append(entry)

            elif len(ranges) > 1:
                # Ambiguous: multiple ranges tight-match. Don't auto-settle —
                # let the user resolve manually so we don't mark the wrong days.
                # Report each range as an option.
                options = []
                for (i, j, total) in ranges:
                    opt_days = [str(daily_rows[k].sale_date) for k in range(i, j + 1)]
                    opt_label = opt_days[0] if len(opt_days) == 1 else f"{opt_days[0]} → {opt_days[-1]}"
                    options.append({"label": opt_label, "days": opt_days, "total": total})

                entry = {
                    "bank_txn_id":  dep.id,
                    "bank_date":    str(dep_date),
                    "bank_amount":  bank_amount,
                    "bank_desc":    dep.description,
                    "sale_date":    options[0]["label"],
                    "sale_card":    options[0]["total"],
                    "diff":         round(bank_amount - options[0]["total"], 2),
                    "matched":      False,
                    "ambiguous":    True,
                    "ambiguous_options": options,
                    "skipped_days": [],
                    "settled_days": [],
                }

                # Mark deposit notified — we told the user. They'll resolve manually.
                dep.is_matched = True
                results.append(entry)

            else:
                # Zero tight ranges → real mismatch. Report against oldest day.
                oldest = daily_rows[0]
                sale_card = float(oldest.card)
                diff = round(bank_amount - sale_card, 2)

                entry = {
                    "bank_txn_id":  dep.id,
                    "bank_date":    str(dep_date),
                    "bank_amount":  bank_amount,
                    "bank_desc":    dep.description,
                    "sale_date":    str(oldest.sale_date),
                    "sale_card":    sale_card,
                    "diff":         diff,
                    "matched":      False,
                    "ambiguous":    False,
                    "skipped_days": [],
                    "settled_days": [],
                }

                # Mark deposit notified — we told the user, don't re-alert.
                # The day stays unsettled; user will tap [Resolve] or it'll be
                # matched by the correct deposit later.
                dep.is_matched = True
                results.append(entry)

        await session.commit()

    return results


async def settle_cc_days_with_deposit(
    store_id: str,
    bank_txn_id: int,
    sale_date_isos: list[str],
) -> bool:
    """
    User picked which days this ambiguous deposit covers (via cc_pick button).
    Mark every specified day as cc_settled_at now, link to the bank txn, and
    highlight the sheet.
    """
    from datetime import datetime, date as _date
    from sqlalchemy import select, and_
    from db.database import get_async_session
    from db.models import BankTransaction, DailySales

    try:
        sds = [_date.fromisoformat(d) for d in sale_date_isos]
    except ValueError:
        return False

    async with get_async_session() as session:
        dep = (await session.execute(
            select(BankTransaction).where(and_(
                BankTransaction.id == bank_txn_id,
                BankTransaction.store_id == store_id,
            ))
        )).scalar_one_or_none()
        if not dep:
            return False

        rows = (await session.execute(
            select(DailySales).where(and_(
                DailySales.store_id == store_id,
                DailySales.sale_date.in_(sds),
            ))
        )).scalars().all()

        if not rows:
            return False

        now = datetime.utcnow()
        for r in rows:
            r.cc_settled_at = now
            r.cc_bank_txn_id = bank_txn_id
        dep.is_matched = True
        dep_date = dep.transaction_date
        await session.commit()

    # Highlight sheet for each day
    try:
        import asyncio
        from tools.sheets_tools import mark_cc_settled
        loop = asyncio.get_event_loop()
        for r in rows:
            await loop.run_in_executor(
                None, mark_cc_settled, r.sale_date, float(r.card), dep_date,
            )
    except Exception as e:
        log.warning("Sheet highlight after cc_pick failed: %s", e)

    log.info("CC ambiguous resolved store=%s bank_txn=%s days=%s",
             store_id, bank_txn_id, sale_date_isos)
    return True


async def skip_cc_deposit(store_id: str, bank_txn_id: int) -> bool:
    """User tapped Skip on an ambiguous CC deposit alert. Just mark notified."""
    from sqlalchemy import select, and_
    from db.database import get_async_session
    from db.models import BankTransaction

    async with get_async_session() as session:
        dep = (await session.execute(
            select(BankTransaction).where(and_(
                BankTransaction.id == bank_txn_id,
                BankTransaction.store_id == store_id,
            ))
        )).scalar_one_or_none()
        if not dep:
            return False
        dep.is_matched = True
        await session.commit()
    return True


async def resolve_sale_day_cc(store_id: str, sale_date_iso: str) -> bool:
    """
    Manually mark a daily_sales row as CC-settled (user tapped [Resolve] in Telegram).
    Used for fee holds, late deposits, or other cases where the automatic matcher
    can't reconcile the day. Silent — just stops future alerts for that day.
    """
    from datetime import datetime, date as _date
    from sqlalchemy import select, and_
    from db.database import get_async_session
    from db.models import DailySales

    try:
        sd = _date.fromisoformat(sale_date_iso)
    except ValueError:
        return False

    async with get_async_session() as session:
        row = (await session.execute(
            select(DailySales).where(and_(
                DailySales.store_id == store_id,
                DailySales.sale_date == sd,
            ))
        )).scalar_one_or_none()

        if not row:
            return False

        row.cc_settled_at = datetime.utcnow()
        await session.commit()

    log.info("CC manually resolved store=%s sale_date=%s", store_id, sale_date_iso)
    return True
