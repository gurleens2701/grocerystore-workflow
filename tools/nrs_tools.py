"""
NRS Plus (mystore.nrsplus.com) data fetching tools.

Strategy:
  1. Use Playwright (headless) to log in and capture the session token.
  2. Use httpx to call the pos-papi.nrsplus.com API directly.

Daily sheet mapping:
  LEFT TOP  — product_sales (bydept sum)  → "TOTAL" on the sheet
  LEFT BOT  — lotto_in, lotto_online, sales_tax, gpi
  RIGHT     — cash, card, check, lotto_payout, atm, altri
"""

import asyncio
from datetime import date, timedelta
from typing import Any

import httpx
from playwright.async_api import async_playwright

from config.settings import settings

# NRS Plus portal constants
_BASE_URL = "https://mystore.nrsplus.com"
_PAPI_BASE = "https://pos-papi.nrsplus.com"
_STORE_ID = 69653
_LOGIN_STORE_LABEL = "69201 - MORAINE FOODMART"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def _authenticate() -> str:
    """Launch headless browser, log in to NRS Plus, return session token."""
    token: str | None = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        async def _capture(response: Any) -> None:
            nonlocal token
            if "authenticate" in response.url and response.status == 200:
                try:
                    data = await response.json()
                    token = data.get("data", {}).get("token")
                except Exception:
                    pass

        page.on("response", _capture)

        # Navigate to base URL — it will redirect to the current portal path
        # (NRS periodically rotates the nocache parameter in the URL)
        await page.goto(_BASE_URL, wait_until="domcontentloaded")
        await page.wait_for_selector("[name=creduser]", timeout=30000)

        await page.fill("[name=creduser]", settings.nrs_username)
        await page.fill("[name=credpass]", settings.nrs_password)
        await page.click('button:has-text("Sign In")')

        # Store selector dialog — wait up to 20s
        try:
            await page.wait_for_selector("select", timeout=20000)
            await page.select_option("select", label=_LOGIN_STORE_LABEL)
            await asyncio.sleep(1)
            await page.click('button:has-text("OK")')
        except Exception:
            pass

        # Wait up to 30s for the auth token to be captured
        for _ in range(60):
            if token:
                break
            await asyncio.sleep(0.5)

        await browser.close()

    if not token:
        raise RuntimeError("NRS authentication failed — token not captured")
    return token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cents(value: Any) -> float:
    """Convert NRS API cent integer to dollars."""
    try:
        return round(int(value) / 100, 2)
    except (TypeError, ValueError):
        return 0.0


async def _get_stats(token: str, target_date: date) -> dict[str, Any]:
    """Fetch daily stats from NRS pcrhist API."""
    date_str = target_date.strftime("%Y-%m-%d")
    url = (
        f"{_PAPI_BASE}/{token}/pcrhist/{_STORE_ID}/stats"
        f"/yesterday/{date_str}/{date_str}?elmer_id=0"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json().get("data", {})


async def _get_inventory_raw(token: str) -> dict[str, Any]:
    """Fetch raw inventory data from NRS inventory API."""
    url = f"{_PAPI_BASE}/{token}/inventory/{_STORE_ID}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json().get("merchant", {})


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

async def get_daily_sales(target_date: date | None = None) -> dict[str, Any]:
    """
    Fetch daily sales matching the manual daily sheet structure.

    Returns:
      date, day_of_week
      --- LEFT TOP (product sales) ---
      departments: list of {name, items, sales}
      product_sales: sum of department sales (the "TOTAL" on the sheet)
      --- LEFT BOTTOM (other items) ---
      lotto_in: instant lotto sales
      lotto_online: online lotto sales
      sales_tax: tax collected
      gpi: feebuster / GPI amount
      refunds: refund amount
      other_subtotal: lotto_in + lotto_online + sales_tax + gpi
      grand_total: product_sales + other_subtotal (minus refunds)
      --- RIGHT (payments received) ---
      cash: cash payments
      card: credit/debit card payments
      check: check payments
      lotto_payout: lottery payout to customers
      atm: ATM cashback amount
      ebt: food stamps / EBT
      altri: other payment types
      total_payments: sum of all payment types
      --- Misc ---
      total_transactions: number of baskets
      cash_drops: cash dropped to safe
    """
    if target_date is None:
        target_date = date.today() - timedelta(days=1)

    token = await _authenticate()
    data = await _get_stats(token, target_date)

    payamts = data.get("payamts", {}) or {}

    # --- Product departments (LEFT TOP) ---
    bydept = data.get("bydept", []) or []
    departments = [
        {"name": d["dept"], "items": d.get("items", 0), "sales": _cents(d.get("sales", 0))}
        for d in bydept if d.get("dept")
    ]
    product_sales = round(sum(d["sales"] for d in departments), 2)

    # --- Other sales (LEFT BOTTOM) ---
    byother = data.get("byotherdept", []) or []
    lotto_in = 0.0
    lotto_online = 0.0
    for d in byother:
        name = (d.get("dept") or "").lower()
        if "instant" in name:
            lotto_in = _cents(d.get("sales", 0))
        elif "online" in name:
            lotto_online = _cents(d.get("sales", 0))

    # Sales tax from collections
    collections = data.get("collections", {}) or {}
    sales_tax = 0.0
    for v in collections.values():
        if isinstance(v, dict) and v.get("type") == "Tax":
            sales_tax = round(sales_tax + _cents(v.get("explicit", 0)), 2)

    # GPI = feebuster
    gpi = _cents(data.get("feebuster", 0))

    # Refunds
    refunds_raw = data.get("refunds", {})
    refunds = _cents(refunds_raw.get("amt", 0)) if isinstance(refunds_raw, dict) else 0.0

    other_subtotal = round(lotto_in + lotto_online + sales_tax + gpi, 2)
    # Grand total matches manual sheet: product + lotto + tax + GPI (refunds NOT deducted)
    grand_total = round(product_sales + other_subtotal, 2)

    # --- Payments (RIGHT) ---
    cash = _cents(payamts.get("cash", 0))
    card = _cents(payamts.get("credit_debit", 0))
    check = _cents(payamts.get("check", 0))
    ebt = _cents((payamts.get("ebt_snap", 0) or 0) + (payamts.get("ebt_cash", 0) or 0))
    altri = _cents(payamts.get("altri", 0))
    loyal = _cents(payamts.get("loyal", 0))

    # Lottery payout, ATM, pull tab from cashback list
    cashback_list = data.get("cashback", []) or []
    lotto_payout = 0.0
    atm = 0.0
    pull_tab = 0.0
    for cb in cashback_list:
        ptype = (cb.get("paytype") or "").lower()
        if "lottery" in ptype or "lotto" in ptype:
            lotto_payout = round(lotto_payout + _cents(cb.get("amt", 0)), 2)
        elif "atm" in ptype:
            atm = round(atm + _cents(cb.get("amt", 0)), 2)
        elif "pull tab" in ptype or "pulltab" in ptype:
            pull_tab = round(pull_tab + _cents(cb.get("amt", 0)), 2)

    # Vendor payout from payouts (cash paid out at register)
    payouts_data = data.get("payouts", {}) or {}
    vendor = _cents(payouts_data.get("amt", 0))

    # Coupon and loyalty/altria from payamts
    coupon = _cents(payamts.get("coupon", 0))
    # altri = altria tobacco payments, loyal = loyalty points redemptions
    altria = _cents(payamts.get("altri", 0))
    loyalty_combined = round(altria + loyal, 2)

    total_payments = round(cash + card + check + ebt + altri + loyal, 2)

    # Cash drops to safe
    drops = data.get("drops", {}) or {}
    cash_drops = _cents(drops.get("amt", 0))

    return {
        "date": str(target_date),
        "day_of_week": target_date.strftime("%A").upper(),
        # Product sales
        "departments": departments,
        "product_sales": product_sales,
        # Other items
        "lotto_in": lotto_in,
        "lotto_online": lotto_online,
        "sales_tax": sales_tax,
        "gpi": gpi,
        "refunds": refunds,
        "other_subtotal": other_subtotal,
        "grand_total": grand_total,
        # Payments
        "cash": cash,
        "card": card,
        "check": check,
        "lotto_payout": lotto_payout,
        "atm": atm,
        "pull_tab": pull_tab,
        "coupon": coupon,
        "loyalty": loyalty_combined,  # altria + loyalty points
        "vendor": vendor,
        "ebt": ebt,
        "altri": altri,
        "total_payments": total_payments,
        # Misc
        "total_transactions": payamts.get("num_sales", 0),
        "cash_drops": cash_drops,
        # Legacy aliases
        "total_sales": product_sales,
        "net_sales": grand_total,
        "cash_sales": cash,
        "card_sales": card,
    }


async def get_transaction_list(target_date: date | None = None) -> list[dict[str, Any]]:
    """
    Return department-level sales breakdown for the given date.
    (NRS API does not expose individual basket-level transactions.)
    """
    if target_date is None:
        target_date = date.today() - timedelta(days=1)

    token = await _authenticate()
    data = await _get_stats(token, target_date)

    rows = []
    for d in (data.get("bydept", []) or []):
        if d.get("dept"):
            rows.append({
                "type": "sale",
                "department": d["dept"],
                "items": d.get("items", 0),
                "amount": _cents(d.get("sales", 0)),
                "payment_method": "mixed",
                "date": str(target_date),
            })
    for d in (data.get("byotherdept", []) or []):
        if d.get("dept"):
            rows.append({
                "type": "other",
                "department": d["dept"],
                "items": d.get("items", 0),
                "amount": _cents(d.get("sales", 0)),
                "payment_method": "mixed",
                "date": str(target_date),
            })
    return rows


async def get_inventory_levels() -> dict[str, Any]:
    """Fetch current tracked inventory levels from NRS."""
    token = await _authenticate()
    merchant = await _get_inventory_raw(token)

    items = []
    low_stock_count = 0

    for group in merchant.get("groups", []):
        threshold = group.get("thresholds", {}).get("cnt", 10)
        for bucket in group.get("overview", []):
            for upc_item in bucket.get("upcs", []):
                on_hand = upc_item.get("onhand", 0)
                is_low = on_hand <= threshold or bucket.get("status") in ("alarm", "warn")
                if is_low:
                    low_stock_count += 1
                items.append({
                    "name": upc_item.get("name", ""),
                    "sku": upc_item.get("upc", ""),
                    "quantity": on_hand,
                    "days_on_hand": upc_item.get("onhand_days", 0),
                    "sales_per_day": upc_item.get("sales_per_day", 0),
                    "low_stock": is_low,
                    "status": bucket.get("status", "unknown"),
                })

    return {"items": items, "low_stock_count": low_stock_count}


# ---------------------------------------------------------------------------
# Sync wrappers for LangChain tools
# ---------------------------------------------------------------------------

def fetch_daily_sales(date_str: str = "") -> dict[str, Any]:
    """Fetch daily sales from NRS. date_str: YYYY-MM-DD or empty for yesterday."""
    target = date.fromisoformat(date_str) if date_str else None
    return asyncio.run(get_daily_sales(target))


def fetch_transactions(date_str: str = "") -> list[dict[str, Any]]:
    """Fetch department-level sales breakdown. date_str: YYYY-MM-DD or empty for yesterday."""
    target = date.fromisoformat(date_str) if date_str else None
    return asyncio.run(get_transaction_list(target))


def fetch_inventory() -> dict[str, Any]:
    """Fetch current inventory levels from NRS."""
    return asyncio.run(get_inventory_levels())
