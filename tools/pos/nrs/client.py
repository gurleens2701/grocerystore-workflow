"""
tools/pos/nrs/client.py

NRS Plus API client — authentication and raw data fetching.

Responsibilities:
  - Playwright login to NRS Plus portal → capture session token
  - Token caching (DB via pending_state) and manual override (/token command)
  - Raw httpx API calls that return the unmodified API response

Returns raw dicts straight from the NRS API — no transformation here.
Transformation (cents→dollars, field mapping) lives in transformer.py.

If login breaks (new portal URL, reCAPTCHA change):
  → This file is where to look, specifically _authenticate()
If API data looks wrong:
  → Check transformer.py, not this file
"""

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

import httpx
from playwright.async_api import async_playwright

from config.settings import settings
from config.store_context import get_active_store

log = logging.getLogger(__name__)

_STATE_NRS_TOKEN = "nrs_token"

# NRS Plus portal constants
_BASE_URL       = "https://mystore.nrsplus.com"
_PAPI_BASE      = "https://pos-papi.nrsplus.com"
_NRS_STORE_ID   = 69653
_LOGIN_STORE_LABEL = "69201 - MORAINE FOODMART"


class NRSTokenExpiredError(RuntimeError):
    """Raised when the cached NRS token has expired (HTTP 401 from API)."""


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def get_cached_token(store_id: str) -> str | None:
    """Return the manually-saved NRS token synchronously."""
    from db.database import get_sync_session
    from db.models import PendingState
    from sqlalchemy import select
    with get_sync_session() as session:
        row = session.execute(
            select(PendingState).where(
                PendingState.store_id == store_id,
                PendingState.state_key == _STATE_NRS_TOKEN,
            )
        ).scalar_one_or_none()
        return row.state_data.get("token") if row else None


async def save_cached_token(store_id: str, token: str) -> None:
    """Persist a manually-provided NRS token."""
    from db.state import save_state
    await save_state(store_id, _STATE_NRS_TOKEN, {"token": token})


def clear_cached_token_sync(store_id: str) -> None:
    """Remove the cached token synchronously."""
    from db.database import get_sync_session
    from db.models import PendingState
    from sqlalchemy import delete
    with get_sync_session() as session:
        session.execute(
            delete(PendingState).where(
                PendingState.store_id == store_id,
                PendingState.state_key == _STATE_NRS_TOKEN,
            )
        )


async def _get_token() -> str:
    """Return a valid NRS session token (cached or via Playwright login)."""
    token = get_cached_token(get_active_store())
    if token:
        log.info("Using cached NRS token.")
        return token
    log.info("No cached token — attempting Playwright login.")
    return await _authenticate()


# ---------------------------------------------------------------------------
# Playwright login
# ---------------------------------------------------------------------------

async def _authenticate() -> str:
    """Launch headless browser, log in to NRS Plus, return session token."""
    token: str | None = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        import re as _re

        async def _capture(response: Any) -> None:
            nonlocal token
            if token:
                return
            url = response.url
            if "authenticate" in url and response.status == 200:
                try:
                    data = await response.json()
                    t = data.get("data", {}).get("token")
                    if t:
                        token = t
                        return
                except Exception:
                    pass
            if "pos-papi.nrsplus.com" in url and response.status == 200:
                m = _re.search(r"pos-papi\.nrsplus\.com/([^/]+)/", url)
                if m:
                    candidate = m.group(1)
                    if len(candidate) >= 10 and "-" in candidate:
                        token = candidate

        page.on("response", _capture)

        await page.goto(_BASE_URL, wait_until="domcontentloaded")
        await page.wait_for_selector("[name=creduser]", timeout=30000)

        await page.fill("[name=creduser]", settings.nrs_username)
        await page.fill("[name=credpass]", settings.nrs_password)
        await page.click('button:has-text("Sign In")')

        try:
            await page.wait_for_selector("select", timeout=20000)
            await page.select_option("select", label=_LOGIN_STORE_LABEL)
            await asyncio.sleep(1)
            await page.click('button:has-text("OK")')
        except Exception:
            pass

        for _ in range(90):
            if token:
                break
            await asyncio.sleep(0.5)

        await context.close()
        await browser.close()

    if not token:
        raise RuntimeError("NRS authentication failed — token not captured")
    log.info("NRS token captured: %s…", token[:12])
    return token


# ---------------------------------------------------------------------------
# Raw API calls — return unmodified API responses
# ---------------------------------------------------------------------------

async def fetch_raw_stats(target_date: date) -> dict[str, Any]:
    """
    Fetch raw daily stats from NRS pcrhist API.
    Returns the 'data' key from the API response without any transformation.
    If this data looks wrong, check the NRS API itself — the transformer is not involved.
    """
    token = await _get_token()
    date_str = target_date.strftime("%Y-%m-%d")
    url = (
        f"{_PAPI_BASE}/{token}/pcrhist/{_NRS_STORE_ID}/stats"
        f"/yesterday/{date_str}/{date_str}?elmer_id=0"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json().get("data", {})
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            clear_cached_token_sync(get_active_store())
            raise NRSTokenExpiredError(
                "NRS session expired. Send /token <value> in Telegram to set a new one."
            ) from e
        raise


async def fetch_raw_inventory() -> dict[str, Any]:
    """Fetch raw inventory data from NRS inventory API."""
    token = await _get_token()
    url = f"{_PAPI_BASE}/{token}/inventory/{_NRS_STORE_ID}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json().get("merchant", {})
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            clear_cached_token_sync(get_active_store())
            raise NRSTokenExpiredError(
                "NRS session expired. Send /token <value> in Telegram to set a new one."
            ) from e
        raise
