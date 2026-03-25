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
from db.models import DailySales, Expense, Invoice, InvoiceItem, MessageLog
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
    user: dict = Depends(get_current_user),
):
    sid = resolve_store(store_id, user)
    data = await _build_health_score_structured(sid)
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


