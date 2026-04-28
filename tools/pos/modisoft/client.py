"""
tools/pos/modisoft/client.py

Modisoft POS connector — authenticates against the Modisoft mobile API
(back6.modisoft.com) and fetches raw daily closing data.

Why the mobile API and not the web portal (insights.modisoft.com)?
  The web portal hides cash drop, credit card breakdown, and check totals.
  The mobile API (used by the iPhone app) returns everything in one call.
  Reverse-engineered via Proxyman capture of the iPhone app.

Auth flow:
  1. POST /login/login with UName + UPwd → returns SToken (UUID)
  2. SToken cached in pending_state (like NRS token)
  3. On 401/IsSuccess:false, token is cleared and re-fetched

Credentials come from stores/{store_id}.env (written by onboard_store.py)
with keys MODISOFT_USERNAME, MODISOFT_PASSWORD, MODISOFT_STORE_ID.

If login breaks (portal changes auth flow):
  → Look here, specifically authenticate()
If data looks wrong (fields mapping wrong):
  → Look at transformer.py, not this file
"""

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

_BASE_URL = "https://back6.modisoft.com"
_STATE_TOKEN_KEY = "modisoft_token"

# Match the iPhone app's User-Agent so the API treats us as the official client
_MOBILE_USER_AGENT = "Modisoft/1.0 CFNetwork/3860.400.51 Darwin/25.3.0"


class ModisoftTokenExpiredError(RuntimeError):
    """Raised when the cached Modisoft SToken has expired."""


# ---------------------------------------------------------------------------
# Credential loading — from stores/{store_id}.env or fallback to .env
# ---------------------------------------------------------------------------

def _load_credentials(store_id: str) -> dict[str, str | None]:
    """
    Load Modisoft credentials for a store.
    Checks stores/{store_id}.env first (written by onboard_store.py),
    falls back to root .env for single-store setups.
    """
    username = password = modisoft_store_id = None

    env_path = Path("stores") / f"{store_id}.env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "MODISOFT_USERNAME":
                username = v
            elif k == "MODISOFT_PASSWORD":
                password = v
            elif k == "MODISOFT_STORE_ID":
                modisoft_store_id = v

    # Fallback to process env (root .env loaded via settings)
    if not username:
        username = os.getenv("MODISOFT_USERNAME")
    if not password:
        password = os.getenv("MODISOFT_PASSWORD")
    if not modisoft_store_id:
        modisoft_store_id = os.getenv("MODISOFT_STORE_ID")

    if not username or not password:
        raise RuntimeError(
            f"Modisoft credentials missing for store_id={store_id!r}. "
            f"Expected in {env_path} or root .env as "
            f"MODISOFT_USERNAME / MODISOFT_PASSWORD."
        )

    return {
        "username": username,
        "password": password,
        "modisoft_store_id": modisoft_store_id,
    }


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def get_cached_token(store_id: str) -> str | None:
    """Return cached SToken synchronously."""
    from db.database import get_sync_session
    from db.models import PendingState
    from sqlalchemy import select
    with get_sync_session() as session:
        row = session.execute(
            select(PendingState).where(
                PendingState.store_id == store_id,
                PendingState.state_key == _STATE_TOKEN_KEY,
            )
        ).scalar_one_or_none()
        return row.state_data.get("token") if row else None


async def save_cached_token(store_id: str, token: str) -> None:
    from db.state import save_state
    await save_state(store_id, _STATE_TOKEN_KEY, {"token": token})


def clear_cached_token(store_id: str) -> None:
    from db.database import get_sync_session
    from db.models import PendingState
    from sqlalchemy import delete
    with get_sync_session() as session:
        session.execute(
            delete(PendingState).where(
                PendingState.store_id == store_id,
                PendingState.state_key == _STATE_TOKEN_KEY,
            )
        )


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

async def authenticate(store_id: str) -> str:
    """
    POST credentials to /login/login and return the SToken.
    Raises if IsSuccess is false.
    """
    creds = _load_credentials(store_id)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{_BASE_URL}/login/login",
            data={
                "UName": creds["username"],
                "UPwd": creds["password"],
                "BRem": "1",
                "AppType": "0",
            },
            headers={"User-Agent": _MOBILE_USER_AGENT},
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("IsSuccess"):
            raise RuntimeError(f"Modisoft login failed: {data.get('Message')}")
        token = data.get("SToken")
        if not token:
            raise RuntimeError(f"Modisoft login succeeded but no SToken: {data}")
        log.info("Modisoft login succeeded for store_id=%s token=%s…", store_id, token[:8])
        return token


async def _get_token(store_id: str) -> str:
    """Return a valid SToken — cached or fresh."""
    token = get_cached_token(store_id)
    if token:
        log.debug("Using cached Modisoft token for store_id=%s", store_id)
        return token
    log.info("No cached Modisoft token for store_id=%s — logging in", store_id)
    token = await authenticate(store_id)
    await save_cached_token(store_id, token)
    return token


async def list_modisoft_stores(store_id: str) -> list[dict[str, Any]]:
    """Return the list of Modisoft stores this account has access to."""
    token = await _get_token(store_id)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{_BASE_URL}/login/GetStoresV2",
            data={"SToken": token},
            headers={"User-Agent": _MOBILE_USER_AGENT},
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("IsSuccess") is False:
            clear_cached_token(store_id)
            raise ModisoftTokenExpiredError(f"Modisoft token expired: {data.get('Message')}")
        return data if isinstance(data, list) else []


async def _resolve_modisoft_store_id(store_id: str) -> int:
    """
    Return the Modisoft internal StoreId (e.g. 138) for this platform store.
    Uses MODISOFT_STORE_ID from env if set, otherwise auto-discovers.
    """
    creds = _load_credentials(store_id)
    if creds.get("modisoft_store_id"):
        return int(creds["modisoft_store_id"])

    stores = await list_modisoft_stores(store_id)
    if not stores:
        raise RuntimeError(f"No Modisoft stores returned for store_id={store_id!r}")
    mid = int(stores[0]["StoreId"])
    log.info(
        "Auto-discovered Modisoft StoreId=%d (name=%s) for platform store_id=%s",
        mid, stores[0].get("StoreName"), store_id,
    )
    return mid


async def fetch_raw_closing(store_id: str, target_date: date) -> dict[str, Any]:
    """
    Fetch raw POS closing details from Modisoft mobile API.

    Returns the full response dict from /store/GetPOSClosingDetails:
      { "Grocery": [...], "Fuel": [...], "FinancialData": {...} }

    If the token is expired, clears it and raises ModisoftTokenExpiredError.
    Retries once on timeout — Modisoft servers occasionally hang under load.
    """
    token = await _get_token(store_id)
    modisoft_store_id = await _resolve_modisoft_store_id(store_id)
    date_str = target_date.strftime("%m-%d-%Y")

    async def _do_fetch() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{_BASE_URL}/store/GetPOSClosingDetails",
                data={
                    "StoreId": str(modisoft_store_id),
                    "SToken": token,
                    "FromDate": date_str,
                    "ToDate": date_str,
                },
                headers={"User-Agent": _MOBILE_USER_AGENT},
            )
            r.raise_for_status()
            return r.json()

    try:
        try:
            data = await _do_fetch()
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            log.warning("Modisoft fetch timed out — retrying once: %s", e)
            data = await _do_fetch()

        # Some endpoints signal token expiry via IsSuccess=false
        if isinstance(data, dict) and data.get("IsSuccess") is False and "Grocery" not in data:
            clear_cached_token(store_id)
            raise ModisoftTokenExpiredError(f"Modisoft session expired: {data.get('Message')}")

        return data
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            clear_cached_token(store_id)
            raise ModisoftTokenExpiredError("Modisoft session expired") from e
        raise


async def fetch_raw_summary(store_id: str, target_date: date) -> list[dict[str, Any]]:
    """Fetch the store-summary closing row (totals + void counts)."""
    token = await _get_token(store_id)
    date_str = target_date.strftime("%m-%d-%Y")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{_BASE_URL}/store/POSClosing",
            data={
                "SToken": token,
                "FromDate": date_str,
                "ToDate": date_str,
            },
            headers={"User-Agent": _MOBILE_USER_AGENT},
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
