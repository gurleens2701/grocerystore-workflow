"""
tools/plaid_tools.py

Plaid integration — per-store bank connection.

Tokens are stored in PostgreSQL via db/state.py under key "plaid_credentials".
This means every store gets its own access token — no shared state.

Supported flows:
  1. create_link_token(store_id)  → link_token for Plaid Link UI
  2. exchange_public_token(store_id, public_token)  → stores access_token + item_id
  3. fetch_accounts(store_id)  → list of accounts with balances
  4. sync_transactions(store_id)  → upsert new transactions into bank_transactions table
  5. match_transactions(store_id)  → match bank rows to invoices/expenses by amount+date
  6. is_connected(store_id)  → bool
  7. get_recent_transactions(store_id, days)  → list from DB
"""

import asyncio
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import plaid
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.accounts_get_request import AccountsGetRequest

from config.settings import settings
from db.state import get_state, save_state

log = logging.getLogger(__name__)

_ENV_MAP = {
    "sandbox":     plaid.Environment.Sandbox,
    "development": plaid.Environment.Sandbox,   # plaid-python v10+ removed Development
    "production":  plaid.Environment.Production,
}

_STATE_KEY = "plaid_credentials"


def _client() -> plaid_api.PlaidApi:
    cfg = plaid.Configuration(
        host=_ENV_MAP.get(settings.plaid_env, plaid.Environment.Sandbox),
        api_key={
            "clientId": settings.plaid_client_id,
            "secret":   settings.plaid_secret,
        },
    )
    return plaid_api.PlaidApi(plaid.ApiClient(cfg))


# ── Credentials (per-store, in DB) ───────────────────────────────────────────

async def _get_creds(store_id: str) -> dict | None:
    return await get_state(store_id, _STATE_KEY)


async def _save_creds(store_id: str, access_token: str, item_id: str, cursor: str | None = None) -> None:
    # Preserve connected_at across re-saves so we don't lose the onboarding cutoff
    existing = await get_state(store_id, _STATE_KEY) or {}
    connected_at = existing.get("connected_at") or date.today().isoformat()
    await save_state(store_id, _STATE_KEY, {
        "access_token": access_token,
        "item_id":      item_id,
        "cursor":       cursor,
        "connected_at": connected_at,
    })


async def is_connected(store_id: str) -> bool:
    creds = await _get_creds(store_id)
    return bool(creds and creds.get("access_token"))


async def disconnect(store_id: str) -> None:
    """Remove stored credentials (does not revoke the Plaid item)."""
    from db.state import clear_state
    await clear_state(store_id, _STATE_KEY)


# ── Link token (step 1 of connect flow) ──────────────────────────────────────

def _create_link_token_sync(store_id: str) -> str:
    client = _client()
    req = dict(
        user=LinkTokenCreateRequestUser(client_user_id=store_id),
        client_name="ClerkAI",
        products=[Products("transactions")],
        country_codes=[CountryCode("US")],
        language="en",
        redirect_uri="https://clerkai.live/bank",
        link_customization_name="finances",
    )
    resp = client.link_token_create(LinkTokenCreateRequest(**req))
    return resp["link_token"]


async def create_link_token(store_id: str) -> str:
    return await asyncio.get_event_loop().run_in_executor(None, _create_link_token_sync, store_id)


# ── Exchange public token (step 2 of connect flow) ───────────────────────────

async def exchange_public_token(store_id: str, public_token: str) -> dict:
    def _exchange():
        client = _client()
        resp = client.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=public_token)
        )
        return resp["access_token"], resp["item_id"]

    access_token, item_id = await asyncio.get_event_loop().run_in_executor(None, _exchange)

    # Prime the cursor — advance past all historical transactions so new users
    # only get transactions from enrollment day forward (not 90 days of history).
    def _prime_cursor():
        client = _client()
        cursor = None
        has_more = True
        while has_more:
            kwargs: dict[str, Any] = {"access_token": access_token}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.transactions_sync(TransactionsSyncRequest(**kwargs))
            cursor = resp["next_cursor"]
            has_more = resp["has_more"]
        return cursor

    try:
        initial_cursor = await asyncio.get_event_loop().run_in_executor(None, _prime_cursor)
        log.info("Plaid cursor primed for store=%s — historical transactions skipped", store_id)
    except Exception as e:
        log.warning("Cursor priming failed for store=%s: %s — will sync 90 days on first sync", store_id, e)
        initial_cursor = None

    await _save_creds(store_id, access_token, item_id, cursor=initial_cursor)
    log.info("Plaid connected for store=%s item_id=%s", store_id, item_id)
    return {"access_token": access_token, "item_id": item_id}


# ── Accounts / balances ───────────────────────────────────────────────────────

def _get_accounts_sync(access_token: str) -> list[dict]:
    client = _client()
    resp = client.accounts_get(AccountsGetRequest(access_token=access_token))
    out = []
    for a in resp["accounts"]:
        bal = a["balances"]
        out.append({
            "account_id":    a["account_id"],
            "name":          a["name"],
            "official_name": a.get("official_name") or a["name"],
            "type":          str(a["type"]),
            "subtype":       str(a.get("subtype", "")),
            "current":       float(bal.get("current") or 0),
            "available":     float(bal["available"]) if bal.get("available") is not None else None,
            "currency":      bal.get("iso_currency_code", "USD"),
        })
    return out


async def fetch_accounts(store_id: str) -> list[dict]:
    creds = await _get_creds(store_id)
    if not creds:
        return []
    return await asyncio.get_event_loop().run_in_executor(
        None, _get_accounts_sync, creds["access_token"]
    )


# ── Transaction sync ──────────────────────────────────────────────────────────

def _sync_plaid_blocking(access_token: str, cursor: str | None) -> tuple[list[dict], str]:
    """Incrementally pull new/updated transactions using /transactions/sync."""
    client = _client()
    added: list[dict] = []
    has_more = True
    next_cursor = cursor

    while has_more:
        kwargs: dict[str, Any] = {"access_token": access_token}
        if next_cursor:
            kwargs["cursor"] = next_cursor
        resp = client.transactions_sync(TransactionsSyncRequest(**kwargs))

        for txn in resp["added"]:
            pfc = txn.get("personal_finance_category") or {}
            cat = pfc.get("primary") or (txn.get("category") or ["OTHER"])[0]
            added.append({
                "transaction_id": txn["transaction_id"],
                "date":           str(txn["date"]),
                "amount":         float(txn["amount"]),   # positive = debit (money out)
                "name":           txn["name"],
                "merchant_name":  txn.get("merchant_name") or "",
                "category":       cat,
                "pending":        bool(txn.get("pending")),
            })

        next_cursor = resp["next_cursor"]
        has_more    = resp["has_more"]

    return added, next_cursor


async def sync_transactions(store_id: str) -> dict:
    """
    Pull new transactions from Plaid and upsert into bank_transactions table.
    Returns {"added", "matched", "accounts"}.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from db.database import get_async_session
    from db.models import BankTransaction

    creds = await _get_creds(store_id)
    if not creds:
        return {"error": "Bank not connected"}

    access_token = creds["access_token"]
    cursor       = creds.get("cursor")
    connected_at_str = creds.get("connected_at")
    cutoff = date.fromisoformat(connected_at_str) if connected_at_str else None

    raw_txns, next_cursor = await asyncio.get_event_loop().run_in_executor(
        None, _sync_plaid_blocking, access_token, cursor
    )

    added_count = 0
    async with get_async_session() as session:
        for t in raw_txns:
            if t["pending"]:
                continue
            # Skip transactions older than onboarding date — we only care
            # about activity from the day the user connected onwards.
            if cutoff and date.fromisoformat(t["date"]) < cutoff:
                continue
            stmt = pg_insert(BankTransaction).values(
                store_id=store_id,
                transaction_date=date.fromisoformat(t["date"]),
                amount=Decimal(str(t["amount"])),
                description=t["name"][:255],
                category=(t["category"] or "")[:64],
                transaction_type=_classify_type(t["amount"], t["category"]),
                plaid_transaction_id=t["transaction_id"],
                is_matched=False,
                last_updated_by="plaid",
            ).on_conflict_do_nothing(index_elements=["plaid_transaction_id"])
            result = await session.execute(stmt)
            if result.rowcount:
                added_count += 1

    # Persist updated cursor
    creds["cursor"] = next_cursor
    await save_state(store_id, _STATE_KEY, creds)

    match_result = await match_transactions(store_id)
    matched       = match_result["count"]
    paid_invoices = match_result["paid_invoices"]
    accounts = await fetch_accounts(store_id)

    # Run reconciliation engine: auto-categorize, flag low-confidence for review
    from tools.bank_reconciler import reconcile_new_transactions
    reconcile_result = await reconcile_new_transactions(store_id)

    log.info(
        "Plaid sync store=%s added=%d matched=%d auto=%d needs_review=%d cc_mismatches=%d",
        store_id, added_count, matched,
        reconcile_result.get("auto_classified", 0),
        len(reconcile_result.get("needs_review", [])),
        len(reconcile_result.get("cc_mismatches", [])),
    )
    return {
        "added":          added_count,
        "matched":        matched,
        "paid_invoices":  paid_invoices,
        "accounts":       accounts,
        "needs_review":   reconcile_result.get("needs_review", []),
        "cc_mismatches":  reconcile_result.get("cc_mismatches", []),
        "auto_classified": reconcile_result.get("auto_classified", 0),
        "auto_list":      reconcile_result.get("auto_list", []),
    }


def _classify_type(amount: float, category: str) -> str:
    cat = (category or "").lower()
    if amount < 0:
        return "deposit"
    if "transfer" in cat:
        return "transfer"
    if "fee" in cat or "service" in cat:
        return "fee"
    return "payment"


# ── Match bank transactions to invoices/expenses ─────────────────────────────

async def match_transactions(store_id: str) -> dict:
    """
    Match unmatched bank debits to logged invoices/expenses by amount (±$1) and date (±3 days).
    Returns {"count": int, "paid_invoices": [{"vendor", "amount", "invoice_date", "bank_date"}]}.
    """
    from sqlalchemy import select, and_
    from db.database import get_async_session
    from db.models import BankTransaction, Invoice, Expense

    matched = 0
    paid_invoices: list[dict] = []

    async with get_async_session() as session:
        unmatched = (await session.execute(
            select(BankTransaction).where(and_(
                BankTransaction.store_id == store_id,
                BankTransaction.is_matched == False,
                BankTransaction.amount > 0,
            ))
        )).scalars().all()

        for bt in unmatched:
            date_lo  = bt.transaction_date - timedelta(days=3)
            date_hi  = bt.transaction_date + timedelta(days=3)
            amt_lo   = bt.amount - Decimal("1.00")
            amt_hi   = bt.amount + Decimal("1.00")

            invoice = (await session.execute(
                select(Invoice).where(and_(
                    Invoice.store_id == store_id,
                    Invoice.invoice_date.between(date_lo, date_hi),
                    Invoice.amount.between(amt_lo, amt_hi),
                ))
            )).scalars().first()

            if invoice:
                bt.matched_invoice_id = invoice.id
                bt.is_matched = True
                bt.review_status = "confirmed"
                bt.reconcile_type = "invoice"
                bt.reconcile_subcategory = invoice.vendor
                invoice.matched_bank_transaction_id = bt.id
                matched += 1
                paid_invoices.append({
                    "vendor":       invoice.vendor,
                    "amount":       float(invoice.amount),
                    "invoice_date": str(invoice.invoice_date),
                    "bank_date":    str(bt.transaction_date),
                })
                # Mark the Google Sheet COGS cell green to show bank confirmed payment
                try:
                    from tools.sheets_tools import mark_invoice_paid
                    import asyncio as _aio
                    await _aio.get_event_loop().run_in_executor(
                        None, mark_invoice_paid, invoice.vendor, invoice.invoice_date
                    )
                except Exception as _e:
                    log.warning("mark_invoice_paid failed for %s: %s", invoice.vendor, _e)
                continue

            expense = (await session.execute(
                select(Expense).where(and_(
                    Expense.store_id == store_id,
                    Expense.expense_date.between(date_lo, date_hi),
                    Expense.amount.between(amt_lo, amt_hi),
                ))
            )).scalars().first()

            if expense:
                bt.is_matched = True
                matched += 1

        await session.commit()

    return {"count": matched, "paid_invoices": paid_invoices}


# ── Recent transactions from DB (already synced) ─────────────────────────────

async def get_recent_transactions(store_id: str, days: int = 30) -> list[dict]:
    from sqlalchemy import select, and_
    from db.database import get_async_session
    from db.models import BankTransaction

    since = date.today() - timedelta(days=days)
    async with get_async_session() as session:
        rows = (await session.execute(
            select(BankTransaction).where(and_(
                BankTransaction.store_id == store_id,
                BankTransaction.transaction_date >= since,
            )).order_by(BankTransaction.transaction_date.desc()).limit(200)
        )).scalars().all()

    return [
        {
            "id":           r.id,
            "date":         str(r.transaction_date),
            "amount":       float(r.amount),
            "description":  r.description,
            "category":     r.category,
            "type":         r.transaction_type,
            "is_matched":   r.is_matched,
            "matched_invoice_id": r.matched_invoice_id,
        }
        for r in rows
    ]
