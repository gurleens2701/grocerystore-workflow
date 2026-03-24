"""
FastAPI backend for the gas station dashboard.
Runs on port 8000. Nginx proxies /api/* here.
"""

from datetime import date, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import and_, func, select

from api.auth import authenticate_user, create_access_token, decode_token
from config.settings import settings
from db.database import get_async_session
from db.models import DailySales, Expense, Invoice, InvoiceItem
from tools.health_score import _build_health_score_async
from tools.price_lookup import _compile_order_async

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
# Auth dependency
# ---------------------------------------------------------------------------

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    payload = decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return payload


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


@app.post("/api/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest):
    if not authenticate_user(body.username, body.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = create_access_token({"sub": body.username, "store_id": settings.store_id})
    return LoginResponse(access_token=token, store_name=settings.store_id.title())


@app.get("/api/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return {"username": user.get("sub"), "store_id": user.get("store_id")}


# ---------------------------------------------------------------------------
# Sales routes
# ---------------------------------------------------------------------------

@app.get("/api/sales")
async def get_sales(days: int = 7, user: dict = Depends(get_current_user)):
    """Return last N days of daily sales."""
    store_id = settings.store_id
    since = date.today() - timedelta(days=days)

    async with get_async_session() as session:
        result = await session.execute(
            select(DailySales)
            .where(
                and_(
                    DailySales.store_id == store_id,
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
async def get_health(user: dict = Depends(get_current_user)):
    report = await _build_health_score_async(settings.store_id)
    return {"report": report}


# ---------------------------------------------------------------------------
# Price database routes
# ---------------------------------------------------------------------------

@app.get("/api/prices")
async def search_prices(q: str = "", user: dict = Depends(get_current_user)):
    """Search invoice items by name."""
    store_id = settings.store_id

    async with get_async_session() as session:
        words = q.strip().split() if q.strip() else []

        base = select(
            InvoiceItem.vendor,
            InvoiceItem.item_name,
            InvoiceItem.canonical_name,
            InvoiceItem.unit_price,
            InvoiceItem.category,
            InvoiceItem.invoice_date,
        ).where(InvoiceItem.store_id == store_id)

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


@app.post("/api/order")
async def compile_order_api(body: OrderRequest, user: dict = Depends(get_current_user)):
    item_list = [{"item": i.item, "qty": i.qty} for i in body.items]
    result = await _compile_order_async(item_list)
    return {"summary": result}


# ---------------------------------------------------------------------------
# Settings route
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def get_settings(user: dict = Depends(get_current_user)):
    return {
        "store_name": settings.store_id.title(),
        "google_sheet_url": f"https://docs.google.com/spreadsheets/d/{settings.google_sheet_id}",
    }
