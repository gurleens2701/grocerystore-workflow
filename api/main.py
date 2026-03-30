"""
FastAPI backend for the gas station dashboard.
Runs on port 8000. Nginx proxies /api/* here.
"""

import asyncio
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import and_, func, select

from api.auth import authenticate_user, create_access_token, decode_token
from config.settings import settings
from db.database import get_session_for_store
from db.models import BankTransaction, DailySales, Expense, Invoice, InvoiceItem, MessageLog, Rebate, TransactionRule
from db.ops import log_message
from tools.chat_handler import process_message as chat_process_message
from tools.health_score import _build_health_score_async, _build_health_score_structured
from tools.price_lookup import _compile_order_async, _lookup_item_price_async, parse_order_list

app = FastAPI(title="Gas Station Dashboard API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()


# ---------------------------------------------------------------------------
# Auth dependency + store resolver
# ---------------------------------------------------------------------------

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    payload = decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    # Normalize: old tokens had store_id (str), new have store_ids (list)
    if "store_ids" not in payload:
        payload["store_ids"] = [payload.get("store_id", settings.store_id)]
    return payload


def resolve_store(store_id: Optional[str], user: dict) -> str:
    """Validate requested store_id is in the user's allowed list. Default to first."""
    allowed: list[str] = user.get("store_ids", [settings.store_id])
    sid = store_id or (allowed[0] if allowed else settings.store_id)
    if sid not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied for this store")
    return sid


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    store_name: str
    store_ids: list[str]


@app.post("/api/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest):
    if not authenticate_user(body.username, body.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    store_ids = settings.allowed_stores
    token = create_access_token({"sub": body.username, "store_ids": store_ids})
    return LoginResponse(
        access_token=token,
        store_name=store_ids[0].replace("_", " ").title(),
        store_ids=store_ids,
    )


@app.get("/api/auth/me")
async def me(user: dict = Depends(get_current_user)):
    store_ids: list[str] = user.get("store_ids", [settings.store_id])
    return {
        "username": user.get("sub"),
        "store_id": store_ids[0] if store_ids else settings.store_id,
        "store_ids": store_ids,
    }


# ---------------------------------------------------------------------------
# Stores list
# ---------------------------------------------------------------------------

@app.get("/api/stores")
async def list_stores(user: dict = Depends(get_current_user)):
    store_ids: list[str] = user.get("store_ids", [settings.store_id])
    return [
        {"id": sid, "name": sid.replace("_", " ").title()}
        for sid in store_ids
    ]


# ---------------------------------------------------------------------------
# Sales routes
# ---------------------------------------------------------------------------

@app.get("/api/sales")
async def get_sales(
    days: int = 7,
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    since = date.today() - timedelta(days=days)

    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(DailySales)
            .where(
                and_(
                    DailySales.store_id == sid,
                    DailySales.sale_date >= since,
                )
            )
            .order_by(DailySales.sale_date.desc())
        )
        rows = result.scalars().all()

    return [
        {
            "date": r.sale_date.isoformat(),
            "day_of_week": r.sale_date.strftime("%A"),
            "product_sales": float(r.product_sales or 0),
            "lotto_in": float(r.lotto_in or 0),
            "lotto_online": float(r.lotto_online or 0),
            "sales_tax": float(r.sales_tax or 0),
            "gpi": float(r.gpi or 0),
            "grand_total": float(r.grand_total or 0),
            "cash_drop": float(r.cash_drop or 0),
            "card": float(r.card or 0),
            "lotto_po": float(r.lotto_po or 0),
            "lotto_cr": float(r.lotto_cr or 0),
            "food_stamp": float(r.food_stamp or 0),
            "total_transactions": r.total_transactions or 0,
            "over_short": _calc_over_short(r),
            "departments": r.departments or [],
        }
        for r in rows
    ]


def _calc_over_short(r: DailySales) -> Optional[float]:
    if r.lotto_po is None:
        return None
    total_payments = sum(float(getattr(r, col) or 0) for col in [
        "cash_drop", "card", "check_amount", "lotto_po", "lotto_cr",
        "atm", "pull_tab", "coupon", "food_stamp", "loyalty", "vendor_payout"
    ])
    return round(total_payments - float(r.grand_total or 0), 2)


# ---------------------------------------------------------------------------
# Health score route
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def get_health(
    store_id: Optional[str] = Query(None),
    period: str = Query("this_week"),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    data = await _build_health_score_structured(sid, period=period)
    return data


# ---------------------------------------------------------------------------
# Price database routes
# ---------------------------------------------------------------------------

@app.get("/api/prices")
async def search_prices(
    q: str = "",
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)

    async with get_session_for_store(sid) as session:
        words = q.strip().split() if q.strip() else []

        base = select(
            InvoiceItem.vendor,
            InvoiceItem.item_name,
            InvoiceItem.canonical_name,
            InvoiceItem.unit_price,
            InvoiceItem.category,
            InvoiceItem.invoice_date,
        ).where(InvoiceItem.store_id == sid)

        if words:
            conditions = [
                (InvoiceItem.canonical_name.ilike(f"%{w}%") | InvoiceItem.item_name.ilike(f"%{w}%"))
                for w in words
            ]
            base = base.where(and_(*conditions))

        base = base.order_by(InvoiceItem.invoice_date.desc()).limit(200)
        result = await session.execute(base)
        rows = result.fetchall()

    return [
        {
            "vendor": r.vendor,
            "item_name": r.item_name,
            "canonical_name": r.canonical_name,
            "unit_price": float(r.unit_price),
            "category": r.category,
            "invoice_date": r.invoice_date.isoformat(),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Order builder route
# ---------------------------------------------------------------------------

class OrderItem(BaseModel):
    item: str
    qty: int = 1


class OrderRequest(BaseModel):
    items: list[OrderItem]
    store_id: Optional[str] = None


@app.post("/api/order")
async def compile_order_api(body: OrderRequest, user: dict = Depends(get_current_user)):
    sid = resolve_store(body.store_id, user)
    item_list = [{"item": i.item, "qty": i.qty} for i in body.items]
    result = await _compile_order_async(item_list, store_id=sid)
    return {"summary": result}


# ---------------------------------------------------------------------------
# Settings route
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def get_settings(
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    return {
        "store_name": sid.replace("_", " ").title(),
        "google_sheet_url": f"https://docs.google.com/spreadsheets/d/{settings.google_sheet_id}",
    }


# ---------------------------------------------------------------------------
# Chat routes — unified Telegram + web message history
# ---------------------------------------------------------------------------

@app.get("/api/messages")
async def get_messages(
    limit: int = 50,
    since: Optional[str] = Query(None),  # ISO timestamp
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    """Return message history. Poll with ?since=<last_timestamp> for updates."""
    sid = resolve_store(store_id, user)

    async with get_session_for_store(sid) as session:
        q = select(MessageLog).where(MessageLog.store_id == sid)
        if since:
            try:
                since_dt = datetime.fromisoformat(since)
                q = q.where(MessageLog.created_at > since_dt)
            except ValueError:
                pass
        q = q.order_by(MessageLog.created_at.asc()).limit(limit)
        result = await session.execute(q)
        rows = result.scalars().all()

    return [
        {
            "id": r.id,
            "source": r.source,
            "role": r.role,
            "sender_name": r.sender_name,
            "content": r.content,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


class ChatRequest(BaseModel):
    message: str
    sender_name: str = "Employee"
    store_id: Optional[str] = None


@app.post("/api/chat")
async def web_chat(body: ChatRequest, user: dict = Depends(get_current_user)):
    """Process a message from the web chat UI. Mirrors to Telegram."""
    sid = resolve_store(body.store_id, user)
    msg = body.message.strip()
    sender = body.sender_name or user.get("sub", "Employee")

    # Log incoming web message
    await log_message(sid, "web", "user", sender, msg)

    # Route the command
    reply = await chat_process_message(msg, sid)

    # Log bot reply
    await log_message(sid, "web", "bot", "Bot", reply)

    # Mirror to Telegram so owner sees it
    try:
        from telegram import Bot
        bot = Bot(token=settings.telegram_bot_token)
        tg_text = f"💬 *{sender}*: {msg}\n\n{reply}"
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=tg_text,
            parse_mode="Markdown",
        )
    except Exception:
        pass  # Telegram mirror failure must not block web response

    return {"reply": reply}



# ---------------------------------------------------------------------------
# Ledger routes
# ---------------------------------------------------------------------------

DAILY_SALES_FIELDS = [
    "product_sales", "lotto_in", "lotto_online", "sales_tax", "gpi", "grand_total",
    "cash_drop", "card", "check_amount", "lotto_po", "lotto_cr",
    "atm", "pull_tab", "coupon", "food_stamp", "loyalty", "vendor_payout", "refunds",
]


class SalesUpsert(BaseModel):
    date: str
    field: str
    value: float
    store_id: Optional[str] = None


class InvoiceUpsert(BaseModel):
    date: str
    vendor: str
    amount: float
    store_id: Optional[str] = None


class ExpenseUpsert(BaseModel):
    date: str
    category: str
    amount: float
    store_id: Optional[str] = None


class PayrollUpsert(BaseModel):
    date: str
    employee: str
    amount: float
    store_id: Optional[str] = None


class RebateUpsert(BaseModel):
    date: str
    vendor: str
    amount: float
    store_id: Optional[str] = None


def _sales_row_to_dict(r: DailySales) -> dict:
    return {
        "date": r.sale_date.isoformat(),
        "day_of_week": r.sale_date.strftime("%A"),
        "product_sales": float(r.product_sales or 0),
        "lotto_in": float(r.lotto_in or 0),
        "lotto_online": float(r.lotto_online or 0),
        "sales_tax": float(r.sales_tax or 0),
        "gpi": float(r.gpi or 0),
        "grand_total": float(r.grand_total or 0),
        "cash_drop": float(r.cash_drop or 0),
        "card": float(r.card or 0),
        "check_amount": float(r.check_amount or 0),
        "lotto_po": float(r.lotto_po or 0) if r.lotto_po is not None else None,
        "lotto_cr": float(r.lotto_cr or 0) if r.lotto_cr is not None else None,
        "atm": float(r.atm or 0),
        "pull_tab": float(r.pull_tab or 0),
        "coupon": float(r.coupon or 0),
        "food_stamp": float(r.food_stamp or 0) if r.food_stamp is not None else None,
        "loyalty": float(r.loyalty or 0),
        "vendor_payout": float(r.vendor_payout or 0),
        "over_short": _calc_over_short(r),
        "total_transactions": r.total_transactions or 0,
        "departments": r.departments or [],
    }


@app.get("/api/ledger/sales")
async def ledger_get_sales(
    month: str = Query(..., description="YYYY-MM"),
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    try:
        year, mo = int(month[:4]), int(month[5:7])
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")

    import calendar as _cal
    first_day = date(year, mo, 1)
    last_day = date(year, mo, _cal.monthrange(year, mo)[1])

    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(DailySales)
            .where(
                and_(
                    DailySales.store_id == sid,
                    DailySales.sale_date >= first_day,
                    DailySales.sale_date <= last_day,
                )
            )
            .order_by(DailySales.sale_date.asc())
        )
        rows = result.scalars().all()

    return [_sales_row_to_dict(r) for r in rows]


@app.put("/api/ledger/sales")
async def ledger_put_sales(body: SalesUpsert, user: dict = Depends(get_current_user)):
    sid = resolve_store(body.store_id, user)
    if body.field not in DAILY_SALES_FIELDS:
        raise HTTPException(status_code=400, detail=f"Unknown field: {body.field}")

    try:
        sale_date = date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(DailySales).where(
                and_(DailySales.store_id == sid, DailySales.sale_date == sale_date)
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = DailySales(store_id=sid, sale_date=sale_date)
            session.add(row)
        setattr(row, body.field, body.value)
        row.last_updated_by = "web"
        await session.commit()
        await session.refresh(row)

    return _sales_row_to_dict(row)


@app.get("/api/ledger/invoices")
async def ledger_get_invoices(
    month: str = Query(..., description="YYYY-MM"),
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    try:
        year, mo = int(month[:4]), int(month[5:7])
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")

    import calendar as _cal
    first_day = date(year, mo, 1)
    last_day = date(year, mo, _cal.monthrange(year, mo)[1])

    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Invoice)
            .where(
                and_(
                    Invoice.store_id == sid,
                    Invoice.invoice_date >= first_day,
                    Invoice.invoice_date <= last_day,
                )
            )
            .order_by(Invoice.invoice_date.asc(), Invoice.vendor.asc())
        )
        rows = result.scalars().all()

    return [
        {
            "id": r.id,
            "date": r.invoice_date.isoformat(),
            "vendor": r.vendor,
            "amount": float(r.amount),
        }
        for r in rows
    ]


@app.put("/api/ledger/invoices")
async def ledger_put_invoice(body: InvoiceUpsert, user: dict = Depends(get_current_user)):
    sid = resolve_store(body.store_id, user)
    try:
        inv_date = date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Invoice).where(
                and_(
                    Invoice.store_id == sid,
                    Invoice.invoice_date == inv_date,
                    Invoice.vendor == body.vendor,
                )
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = Invoice(store_id=sid, invoice_date=inv_date, vendor=body.vendor, amount=body.amount)
            session.add(row)
        else:
            row.amount = body.amount
        row.last_updated_by = "web"
        await session.commit()
        await session.refresh(row)

    # Sheets write (fire-and-forget, don't block on failure)
    try:
        from tools.sheets_tools import log_cogs_entry
        await asyncio.get_event_loop().run_in_executor(
            None, log_cogs_entry, body.vendor, body.amount, inv_date
        )
    except Exception:
        pass

    return {"id": row.id, "date": row.invoice_date.isoformat(), "vendor": row.vendor, "amount": float(row.amount)}


@app.delete("/api/ledger/invoices/{inv_id}")
async def ledger_delete_invoice(
    inv_id: int,
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Invoice).where(and_(Invoice.id == inv_id, Invoice.store_id == sid))
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Invoice not found")
        await session.delete(row)
        await session.commit()
    return {"deleted": inv_id}


@app.get("/api/ledger/expenses")
async def ledger_get_expenses(
    month: str = Query(..., description="YYYY-MM"),
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    try:
        year, mo = int(month[:4]), int(month[5:7])
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")

    import calendar as _cal
    first_day = date(year, mo, 1)
    last_day = date(year, mo, _cal.monthrange(year, mo)[1])

    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Expense)
            .where(
                and_(
                    Expense.store_id == sid,
                    Expense.expense_date >= first_day,
                    Expense.expense_date <= last_day,
                    ~Expense.category.ilike("PAYROLL%"),
                )
            )
            .order_by(Expense.expense_date.asc(), Expense.category.asc())
        )
        rows = result.scalars().all()

    return [
        {
            "id": r.id,
            "date": r.expense_date.isoformat(),
            "category": r.category,
            "amount": float(r.amount),
        }
        for r in rows
    ]


@app.put("/api/ledger/expenses")
async def ledger_put_expense(body: ExpenseUpsert, user: dict = Depends(get_current_user)):
    sid = resolve_store(body.store_id, user)
    try:
        exp_date = date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Expense).where(
                and_(
                    Expense.store_id == sid,
                    Expense.expense_date == exp_date,
                    Expense.category == body.category,
                )
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = Expense(store_id=sid, expense_date=exp_date, category=body.category, amount=body.amount)
            session.add(row)
        else:
            row.amount = body.amount
        row.last_updated_by = "web"
        await session.commit()
        await session.refresh(row)

    try:
        from tools.sheets_tools import log_expense
        await asyncio.get_event_loop().run_in_executor(
            None, log_expense, body.category, body.amount, exp_date
        )
    except Exception:
        pass

    return {"id": row.id, "date": row.expense_date.isoformat(), "category": row.category, "amount": float(row.amount)}


@app.delete("/api/ledger/expenses/{exp_id}")
async def ledger_delete_expense(
    exp_id: int,
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Expense).where(and_(Expense.id == exp_id, Expense.store_id == sid))
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Expense not found")
        await session.delete(row)
        await session.commit()
    return {"deleted": exp_id}


@app.get("/api/ledger/payroll")
async def ledger_get_payroll(
    month: str = Query(..., description="YYYY-MM"),
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    try:
        year, mo = int(month[:4]), int(month[5:7])
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")

    import calendar as _cal
    first_day = date(year, mo, 1)
    last_day = date(year, mo, _cal.monthrange(year, mo)[1])

    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Expense)
            .where(
                and_(
                    Expense.store_id == sid,
                    Expense.expense_date >= first_day,
                    Expense.expense_date <= last_day,
                    Expense.category.ilike("PAYROLL%"),
                )
            )
            .order_by(Expense.expense_date.asc(), Expense.category.asc())
        )
        rows = result.scalars().all()

    return [
        {
            "id": r.id,
            "date": r.expense_date.isoformat(),
            "employee": r.category.replace("PAYROLL_", "").replace("PAYROLL-", "").replace("PAYROLL:", "").strip(),
            "category": r.category,
            "amount": float(r.amount),
        }
        for r in rows
    ]


@app.put("/api/ledger/payroll")
async def ledger_put_payroll(body: PayrollUpsert, user: dict = Depends(get_current_user)):
    sid = resolve_store(body.store_id, user)
    try:
        pay_date = date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    category = f"PAYROLL - {body.employee.upper()}"

    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Expense).where(
                and_(
                    Expense.store_id == sid,
                    Expense.expense_date == pay_date,
                    Expense.category.ilike(f"PAYROLL%{body.employee.upper()}"),
                )
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = Expense(store_id=sid, expense_date=pay_date, category=category, amount=body.amount)
            session.add(row)
        else:
            row.amount = body.amount
        row.last_updated_by = "web"
        await session.commit()
        await session.refresh(row)

    try:
        from tools.sheets_tools import log_payroll
        await asyncio.get_event_loop().run_in_executor(
            None, log_payroll, body.employee, body.amount, pay_date
        )
    except Exception:
        pass

    return {
        "id": row.id,
        "date": row.expense_date.isoformat(),
        "employee": body.employee,
        "amount": float(row.amount),
    }


@app.delete("/api/ledger/payroll/{pay_id}")
async def ledger_delete_payroll(
    pay_id: int,
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Expense).where(
                and_(
                    Expense.id == pay_id,
                    Expense.store_id == sid,
                    Expense.category.ilike("PAYROLL%"),
                )
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Payroll entry not found")
        await session.delete(row)
        await session.commit()
    return {"deleted": pay_id}


@app.get("/api/ledger/rebates")
async def ledger_get_rebates(
    month: str = Query(..., description="YYYY-MM"),
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    try:
        year, mo = int(month[:4]), int(month[5:7])
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")

    import calendar as _cal
    first_day = date(year, mo, 1)
    last_day = date(year, mo, _cal.monthrange(year, mo)[1])

    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Rebate)
            .where(
                and_(
                    Rebate.store_id == sid,
                    Rebate.rebate_date >= first_day,
                    Rebate.rebate_date <= last_day,
                )
            )
            .order_by(Rebate.rebate_date.asc(), Rebate.vendor.asc())
        )
        rows = result.scalars().all()

    return [
        {
            "id": r.id,
            "date": r.rebate_date.isoformat(),
            "vendor": r.vendor,
            "amount": float(r.amount),
        }
        for r in rows
    ]


@app.put("/api/ledger/rebates")
async def ledger_put_rebate(body: RebateUpsert, user: dict = Depends(get_current_user)):
    sid = resolve_store(body.store_id, user)
    try:
        reb_date = date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Rebate).where(
                and_(
                    Rebate.store_id == sid,
                    Rebate.rebate_date == reb_date,
                    Rebate.vendor == body.vendor,
                )
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = Rebate(store_id=sid, rebate_date=reb_date, vendor=body.vendor, amount=body.amount)
            session.add(row)
        else:
            row.amount = body.amount
        row.last_updated_by = "web"
        await session.commit()
        await session.refresh(row)

    try:
        from tools.sheets_tools import log_rebate
        await asyncio.get_event_loop().run_in_executor(
            None, log_rebate, body.vendor, body.amount, reb_date
        )
    except Exception:
        pass

    return {"id": row.id, "date": row.rebate_date.isoformat(), "vendor": row.vendor, "amount": float(row.amount)}


@app.delete("/api/ledger/rebates/{reb_id}")
async def ledger_delete_rebate(
    reb_id: int,
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Rebate).where(and_(Rebate.id == reb_id, Rebate.store_id == sid))
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Rebate not found")
        await session.delete(row)
        await session.commit()
    return {"deleted": reb_id}


# ---------------------------------------------------------------------------
# Ledger suggestions (dynamic distinct values from DB)
# ---------------------------------------------------------------------------

@app.get("/api/ledger/suggestions/vendors")
async def ledger_suggest_vendors(
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Invoice.vendor).distinct().where(Invoice.store_id == sid).order_by(Invoice.vendor)
        )
        return sorted({r[0] for r in result.all() if r[0]})


@app.get("/api/ledger/suggestions/expenses")
async def ledger_suggest_expenses(
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Expense.category).distinct().where(
                and_(Expense.store_id == sid, ~Expense.category.ilike("PAYROLL%"))
            ).order_by(Expense.category)
        )
        return sorted({r[0] for r in result.all() if r[0]})


@app.get("/api/ledger/suggestions/employees")
async def ledger_suggest_employees(
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Expense.category).distinct().where(
                and_(Expense.store_id == sid, Expense.category.ilike("PAYROLL%"))
            )
        )
        names = set()
        for (cat,) in result.all():
            if cat:
                name = cat.replace("PAYROLL_", "").replace("PAYROLL-", "").replace("PAYROLL:", "").strip()
                if name:
                    names.add(name)
        return sorted(names)


@app.get("/api/ledger/suggestions/rebates")
async def ledger_suggest_rebates(
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(Rebate.vendor).distinct().where(Rebate.store_id == sid).order_by(Rebate.vendor)
        )
        return sorted({r[0] for r in result.all() if r[0]})


class DailyReportSubmit(BaseModel):
    date: str
    product_sales: float = 0
    lotto_in: float = 0
    lotto_online: float = 0
    sales_tax: float = 0
    gpi: float = 0
    cash_drop: float = 0
    card: float = 0
    check_amount: float = 0
    lotto_po: float = 0
    lotto_cr: float = 0
    food_stamp: float = 0
    atm: float = 0
    pull_tab: float = 0
    coupon: float = 0
    loyalty: float = 0
    departments: list = []
    store_id: Optional[str] = None


@app.post("/api/daily/report")
async def upload_daily_report(
    file: UploadFile = File(...),
    store_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user),
):
    """
    OCR a POS-printed daily report photo.
    Returns extracted fields + list of fields that still need to be provided.
    """
    sid = resolve_store(store_id, user)
    image_bytes = await file.read()

    from tools.report_ocr import extract_daily_report_from_photo
    result = await asyncio.get_event_loop().run_in_executor(
        None, extract_daily_report_from_photo, image_bytes
    )

    # Convert date to string for JSON serialisation
    if result.get("report_date"):
        result["report_date"] = str(result["report_date"])

    return result


@app.post("/api/daily/submit")
async def submit_daily_report(
    body: DailyReportSubmit,
    user: dict = Depends(get_current_user),
):
    """
    Save a complete daily report to the database and Google Sheets.
    Returns the completed sheet including over/short.
    """
    sid = resolve_store(body.store_id, user)

    try:
        sale_date = date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    grand_total = round(
        body.product_sales + body.lotto_in + body.lotto_online + body.sales_tax + body.gpi, 2
    )
    total_right = round(
        body.cash_drop + body.card + body.check_amount + body.lotto_po + body.lotto_cr
        + body.food_stamp + body.atm + body.pull_tab + body.coupon + body.loyalty, 2
    )
    over_short = round(total_right - grand_total, 2)

    async with get_session_for_store(sid) as session:
        result = await session.execute(
            select(DailySales).where(
                and_(DailySales.store_id == sid, DailySales.sale_date == sale_date)
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = DailySales(store_id=sid, sale_date=sale_date)
            session.add(row)

        row.product_sales = body.product_sales
        row.lotto_in      = body.lotto_in
        row.lotto_online  = body.lotto_online
        row.sales_tax     = body.sales_tax
        row.gpi           = body.gpi
        row.grand_total   = grand_total
        row.cash_drop     = body.cash_drop
        row.card          = body.card
        row.check_amount  = body.check_amount
        row.lotto_po      = body.lotto_po
        row.lotto_cr      = body.lotto_cr
        row.food_stamp    = body.food_stamp
        row.atm           = body.atm
        row.pull_tab      = body.pull_tab
        row.coupon        = body.coupon
        row.loyalty       = body.loyalty
        if body.departments:
            row.departments = body.departments
        row.last_updated_by = "web"
        await session.commit()
        await session.refresh(row)

    # Write to Google Sheets async (non-blocking)
    def _sheet_write():
        from tools.sheets_tools import log_daily_sales as _log_ds
        sales_dict = {
            "product_sales": body.product_sales,
            "lotto_in": body.lotto_in,
            "lotto_online": body.lotto_online,
            "sales_tax": body.sales_tax,
            "gpi": body.gpi,
            "grand_total": grand_total,
            "cash_drops": body.cash_drop,
            "card": body.card,
            "check": body.check_amount,
            "lotto_po": body.lotto_po,
            "lotto_cr": body.lotto_cr,
            "food_stamp": body.food_stamp,
            "atm": body.atm,
            "pull_tab": body.pull_tab,
            "coupon": body.coupon,
            "loyalty": body.loyalty,
            "departments": body.departments,
            "date": body.date,
            "day_of_week": sale_date.strftime("%A"),
        }
        _log_ds(sales_dict)

    try:
        await asyncio.get_event_loop().run_in_executor(None, _sheet_write)
    except Exception as e:
        log.warning("Sheets write failed for daily submit: %s", e)

    return {
        **_sales_row_to_dict(row),
        "over_short": over_short,
        "departments": body.departments,
    }


@app.post("/api/chat/invoice")
async def web_chat_invoice(
    file: UploadFile = File(...),
    sender_name: str = Form("Employee"),
    store_id: Optional[str] = Form(None),
    user: dict = Depends(get_current_user),
):
    """Extract invoice from uploaded photo. Returns items for confirmation."""
    sid = resolve_store(store_id, user)
    sender = sender_name or user.get("sub", "Employee")

    photo_bytes = await file.read()
    await log_message(sid, "web", "user", sender, f"📸 Uploaded invoice photo ({file.filename})")

    from tools.invoice_extractor import extract_invoice_from_photo
    from tools.normalizer import _normalize_async

    result = await asyncio.get_event_loop().run_in_executor(
        None, extract_invoice_from_photo, photo_bytes
    )

    if result.get("error"):
        reply = f"⚠️ Could not read invoice: {result['error']}"
        await log_message(sid, "web", "bot", "Bot", reply)
        return {"error": reply}

    items = result.get("items", [])
    if items:
        try:
            items = await _normalize_async(items, sid)
        except Exception:
            pass  # normalization failure must not block the response
    vendor = result.get("vendor", "Unknown")
    total = result.get("total")

    summary = f"📋 Extracted {len(items)} items from {vendor}"
    if total:
        summary += f" — Total: ${total:.2f}"
    await log_message(sid, "web", "bot", "Bot", summary)

    # Notify Telegram
    try:
        from telegram import Bot
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=f"💬 *{sender}* uploaded an invoice\n\n{summary}",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    return {"vendor": vendor, "total": total, "items": items, "summary": summary}


# ---------------------------------------------------------------------------
# Plaid / Bank routes
# ---------------------------------------------------------------------------

@app.get("/api/bank/status")
async def bank_status(
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    """Check whether this store has a bank connected."""
    from tools.plaid_tools import is_connected, fetch_accounts
    sid = resolve_store(store_id, user)
    connected = await is_connected(sid)
    accounts: list = []
    if connected:
        try:
            accounts = await fetch_accounts(sid)
        except Exception as e:
            log.warning("Plaid accounts fetch failed: %s", e)
    return {"connected": connected, "accounts": accounts}


@app.post("/api/bank/link-token")
async def bank_link_token(
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    """Create a Plaid link_token — pass this to the Plaid Link UI in the browser."""
    from tools.plaid_tools import create_link_token
    sid = resolve_store(store_id, user)
    try:
        token = await create_link_token(sid)
        return {"link_token": token}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bank/exchange")
async def bank_exchange(
    body: dict,
    user: dict = Depends(get_current_user),
):
    """Exchange Plaid public_token for a permanent access_token and store it."""
    from tools.plaid_tools import exchange_public_token
    public_token = body.get("public_token")
    store_id     = body.get("store_id")
    if not public_token:
        raise HTTPException(status_code=400, detail="public_token required")
    sid = resolve_store(store_id, user)
    try:
        result = await exchange_public_token(sid, public_token)
        # Kick off initial sync
        from tools.plaid_tools import sync_transactions
        asyncio.create_task(sync_transactions(sid))
        return {"status": "connected", "item_id": result["item_id"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bank/sync")
async def bank_sync(
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    """Pull latest transactions from Plaid and match them to invoices/expenses."""
    from tools.plaid_tools import sync_transactions
    sid = resolve_store(store_id, user)
    try:
        result = await sync_transactions(sid)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bank/transactions")
async def bank_transactions(
    days: int = Query(30),
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    """Return recent bank transactions from the local DB (post-sync)."""
    from tools.plaid_tools import get_recent_transactions
    sid = resolve_store(store_id, user)
    txns = await get_recent_transactions(sid, days=days)
    return txns


@app.delete("/api/bank/disconnect")
async def bank_disconnect(
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    """Remove stored Plaid credentials for this store."""
    from tools.plaid_tools import disconnect
    sid = resolve_store(store_id, user)
    await disconnect(sid)
    return {"status": "disconnected"}


@app.get("/api/bank/pending-reviews")
async def bank_pending_reviews(
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    """Return all bank transactions that need user categorization."""
    from tools.bank_reconciler import get_pending_reviews
    sid = resolve_store(store_id, user)
    return await get_pending_reviews(sid)


@app.get("/api/bank/cc-mismatches")
async def bank_cc_mismatches(
    store_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    """Return CC settlement vs daily card total mismatches."""
    from tools.bank_reconciler import check_cc_settlements
    sid = resolve_store(store_id, user)
    return await check_cc_settlements(sid)


class BankConfirmBody(BaseModel):
    txn_id: int
    reconcile_type: str
    subcategory: Optional[str] = None
    store_id: Optional[str] = None


@app.post("/api/bank/confirm")
async def bank_confirm(
    body: BankConfirmBody,
    user: dict = Depends(get_current_user),
):
    """Confirm categorization of a bank transaction from the web dashboard."""
    from tools.bank_reconciler import confirm_transaction
    sid = resolve_store(body.store_id, user)
    result = await confirm_transaction(sid, body.txn_id, body.reconcile_type, body.subcategory, sender="dashboard")
    if not result:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return result


@app.post("/api/bank/skip")
async def bank_skip(
    body: dict,
    user: dict = Depends(get_current_user),
):
    """Skip (mark as fee/transfer) a bank transaction."""
    from tools.bank_reconciler import skip_transaction
    txn_id   = body.get("txn_id")
    store_id = body.get("store_id")
    if not txn_id:
        raise HTTPException(status_code=400, detail="txn_id required")
    sid = resolve_store(store_id, user)
    ok = await skip_transaction(sid, int(txn_id))
    if not ok:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return {"status": "skipped"}

