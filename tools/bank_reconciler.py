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
    ("square",        "cc_settlement", None),
    ("stripe",        "cc_settlement", None),
    ("paymentech",    "cc_settlement", None),
    ("heartland",     "cc_settlement", None),
    ("tsys",          "cc_settlement", None),
    ("firstdata",     "cc_settlement", None),
    ("elavon",        "cc_settlement", None),
    ("worldpay",      "cc_settlement", None),
    ("visa batch",    "cc_settlement", None),
    ("mc batch",      "cc_settlement", None),
    ("mastercard batch", "cc_settlement", None),
    ("card deposit",  "cc_settlement", None),
    ("bankcard",      "cc_settlement", None),
    ("ach credit",    "cc_settlement", None),
]

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

    # 2. DB learned rules
    from sqlalchemy import select, and_
    from db.database import get_async_session
    from db.models import TransactionRule

    async with get_async_session() as session:
        rules = (await session.execute(
            select(TransactionRule).where(TransactionRule.store_id == store_id)
            .order_by(TransactionRule.confirmed_count.desc())
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


# ── Main reconcile entry ──────────────────────────────────────────────────────

async def reconcile_new_transactions(store_id: str) -> dict:
    """
    Process all bank_transactions with review_status='pending' for this store.
    Returns {"needs_review": [txn_dicts], "cc_mismatches": [mismatch_dicts], "auto_classified": int}.
    """
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
        auto_count = 0

        for txn in pending:
            result = await _lookup_rule(store_id, txn.description)

            if result and result["confidence"] >= 0.95:
                # Auto-classify
                txn.review_status = "auto"
                txn.reconcile_type = result["reconcile_type"]
                txn.reconcile_subcategory = result["reconcile_subcategory"]
                auto_count += 1

                # Auto-log if it's a cc_settlement (no user action needed)
                # Other types (expense, invoice, rebate) still need explicit logging
                # but we mark them auto so they don't spam user

            else:
                # Try AI for unknown
                if result is None:
                    ai = await _ai_categorize(txn.description, float(txn.amount))
                    confidence = ai.get("confidence", 0.0)
                    rtype = ai.get("reconcile_type", "skip")
                    subcat = ai.get("subcategory")

                    if confidence >= 0.95:
                        txn.review_status = "auto"
                        txn.reconcile_type = rtype
                        txn.reconcile_subcategory = subcat
                        auto_count += 1
                        # Save as a learned rule so next time it's instant
                        await _upsert_rule(store_id, txn.description, rtype, subcat, confirmed=False, session=session)
                    else:
                        txn.review_status = "needs_review"
                        txn.reconcile_type = rtype          # AI's best guess
                        txn.reconcile_subcategory = subcat
                        needs_review.append(_txn_to_dict(txn, confidence=confidence, ai_guess=rtype))
                else:
                    # Rule matched but < 95% confidence — ask user
                    txn.review_status = "needs_review"
                    txn.reconcile_type = result["reconcile_type"]
                    txn.reconcile_subcategory = result["reconcile_subcategory"]
                    needs_review.append(_txn_to_dict(txn, confidence=result["confidence"], ai_guess=result["reconcile_type"]))

        await session.commit()

    cc_mismatches = await check_cc_settlements(store_id)

    log.info(
        "Reconcile store=%s auto=%d needs_review=%d cc_mismatches=%d",
        store_id, auto_count, len(needs_review), len(cc_mismatches),
    )
    return {"needs_review": needs_review, "cc_mismatches": cc_mismatches, "auto_classified": auto_count}


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
    Extract a canonical pattern from the description (first 6 words, lowercased),
    then upsert into transaction_rules.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from db.database import get_async_session
    from db.models import TransactionRule

    # Normalize: strip numbers/dates, take first 4-5 significant words
    words = re.sub(r"[0-9#\-/*]+", " ", description.lower()).split()
    words = [w for w in words if len(w) > 2][:5]
    if not words:
        return
    pattern = " ".join(words)

    async def _do(s):
        stmt = pg_insert(TransactionRule).values(
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
                "confirmed_count": TransactionRule.confirmed_count + (1 if confirmed else 0),
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


async def _auto_log(
    store_id: str,
    txn_id: int,
    txn_date: date,
    amount: float,
    description: str,
    reconcile_type: str,
    subcategory: str | None,
) -> None:
    """Auto-log confirmed transactions to expenses/invoices/rebates."""
    from decimal import Decimal
    from db.database import get_async_session
    from db.models import Expense, Invoice, Rebate

    dec_amount = Decimal(str(abs(amount)))

    if reconcile_type == "expense":
        from db.ops import log_message
        async with get_async_session() as session:
            # Check if already exists
            from sqlalchemy import select, and_
            existing = (await session.execute(
                select(Expense).where(and_(
                    Expense.store_id == store_id,
                    Expense.expense_date == txn_date,
                    Expense.category == (subcategory or description[:64]),
                    Expense.amount == dec_amount,
                ))
            )).scalar_one_or_none()
            if not existing:
                session.add(Expense(
                    store_id=store_id,
                    expense_date=txn_date,
                    category=subcategory or description[:64],
                    amount=dec_amount,
                    notes=f"Auto-logged from bank txn #{txn_id}",
                    last_updated_by="bank_reconciler",
                ))
                await session.commit()
                log.info("Auto-logged expense store=%s %s $%.2f", store_id, subcategory, float(dec_amount))

    elif reconcile_type == "invoice":
        async with get_async_session() as session:
            from sqlalchemy import select, and_
            existing = (await session.execute(
                select(Invoice).where(and_(
                    Invoice.store_id == store_id,
                    Invoice.invoice_date == txn_date,
                    Invoice.vendor == (subcategory or description[:128]),
                    Invoice.amount == dec_amount,
                ))
            )).scalar_one_or_none()
            if not existing:
                session.add(Invoice(
                    store_id=store_id,
                    vendor=subcategory or description[:128],
                    amount=dec_amount,
                    invoice_date=txn_date,
                    matched_bank_transaction_id=txn_id,
                    last_updated_by="bank_reconciler",
                ))
                await session.commit()
                log.info("Auto-logged invoice store=%s vendor=%s $%.2f", store_id, subcategory, float(dec_amount))

    elif reconcile_type == "rebate":
        async with get_async_session() as session:
            from sqlalchemy import select, and_
            existing = (await session.execute(
                select(Rebate).where(and_(
                    Rebate.store_id == store_id,
                    Rebate.rebate_date == txn_date,
                    Rebate.vendor == (subcategory or description[:128]),
                    Rebate.amount == dec_amount,
                ))
            )).scalar_one_or_none()
            if not existing:
                session.add(Rebate(
                    store_id=store_id,
                    rebate_date=txn_date,
                    vendor=subcategory or description[:128],
                    amount=dec_amount,
                    notes=f"Auto-logged from bank txn #{txn_id}",
                    last_updated_by="bank_reconciler",
                ))
                await session.commit()
                log.info("Auto-logged rebate store=%s vendor=%s $%.2f", store_id, subcategory, float(dec_amount))


# ── Pending review queue ──────────────────────────────────────────────────────

async def get_pending_reviews(store_id: str) -> list[dict]:
    """Return all transactions currently awaiting user confirmation."""
    from sqlalchemy import select
    from db.database import get_async_session
    from db.models import BankTransaction

    async with get_async_session() as session:
        rows = (await session.execute(
            select(BankTransaction).where(
                BankTransaction.store_id == store_id,
                BankTransaction.review_status == "needs_review",
            ).order_by(BankTransaction.transaction_date.desc()).limit(50)
        )).scalars().all()

    return [_txn_to_dict(r) for r in rows]


# ── CC settlement matching ────────────────────────────────────────────────────

async def check_cc_settlements(store_id: str) -> list[dict]:
    """
    Match bank CC deposit transactions to DailySales.card totals.
    Settlement usually arrives 1-3 business days after the sale day.
    Returns list of mismatch dicts: {"bank_date", "bank_amount", "sale_date", "sale_card", "diff"}.
    """
    from sqlalchemy import select, and_
    from db.database import get_async_session
    from db.models import BankTransaction, DailySales

    mismatches = []

    async with get_async_session() as session:
        # Get CC settlement deposits from past 14 days (amount < 0 = money IN to account)
        since = date.today() - timedelta(days=14)
        cc_deposits = (await session.execute(
            select(BankTransaction).where(and_(
                BankTransaction.store_id == store_id,
                BankTransaction.transaction_date >= since,
                BankTransaction.amount < 0,  # deposit (money in)
                BankTransaction.reconcile_type == "cc_settlement",
            ))
        )).scalars().all()

        for dep in cc_deposits:
            bank_amount = abs(float(dep.amount))
            dep_date = dep.transaction_date

            # Look for daily_sales within -1 to +4 days (settlement timing)
            date_lo = dep_date - timedelta(days=4)
            date_hi = dep_date + timedelta(days=1)

            daily_rows = (await session.execute(
                select(DailySales).where(and_(
                    DailySales.store_id == store_id,
                    DailySales.sale_date.between(date_lo, date_hi),
                    DailySales.card > 0,
                ))
            )).scalars().all()

            if not daily_rows:
                continue

            # Find the best matching day (closest amount)
            best = min(daily_rows, key=lambda r: abs(float(r.card) - bank_amount))
            sale_card = float(best.card)
            diff = round(bank_amount - sale_card, 2)

            if abs(diff) > 1.00:  # > $1 mismatch
                mismatches.append({
                    "bank_txn_id":  dep.id,
                    "bank_date":    str(dep_date),
                    "bank_amount":  bank_amount,
                    "bank_desc":    dep.description,
                    "sale_date":    str(best.sale_date),
                    "sale_card":    sale_card,
                    "diff":         diff,
                })

    return mismatches
