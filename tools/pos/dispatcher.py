"""
tools/pos/dispatcher.py

Routes daily fetch calls to the correct POS connector based on store.pos_type.

Responsibilities:
  - Call the right POS client (NRS, Modisoft, etc.)
  - Save raw payload to raw_nrs/raw_modisoft schema before transforming
  - Call the right transformer
  - Return canonical daily sales dict

Callers should use fetch_daily_sales(store, target_date) — they don't need
to know or care which POS system the store uses.
"""

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config.store_registry import StoreProfile

log = logging.getLogger(__name__)


async def fetch_daily_sales(store: "StoreProfile", target_date: date | None = None) -> dict:
    """
    Fetch and transform daily sales for any store, regardless of POS type.

    Steps:
      1. Determine target_date (default: yesterday)
      2. Call the correct POS client to get raw API response
      3. Save raw payload to raw_nrs (or raw_modisoft) table
      4. Call the correct transformer
      5. Return canonical dict

    Raises:
        NotImplementedError: for unsupported pos_type
        NRSTokenExpiredError: when NRS session has expired
    """
    if target_date is None:
        target_date = date.today() - timedelta(days=1)

    pos_type = store.pos_type

    if pos_type == "nrs":
        return await _fetch_nrs(store, target_date)
    elif pos_type == "modisoft":
        return await _fetch_modisoft(store, target_date)
    elif pos_type == "manual":
        # Manual stores have no POS fetch — all data entered by owner
        return {"date": str(target_date), "day_of_week": target_date.strftime("%A").upper()}
    else:
        raise NotImplementedError(f"Unsupported pos_type: {pos_type!r}")


async def _fetch_nrs(store: "StoreProfile", target_date: date) -> dict:
    """Fetch from NRS, save raw payload, return canonical dict."""
    from tools.pos.nrs.client import fetch_raw_stats
    from tools.pos.nrs.transformer import transform_daily_sales

    log.info("Fetching NRS daily sales for %s on %s", store.store_id, target_date)
    raw = await fetch_raw_stats(target_date)

    # Save raw payload before transforming (safety net for re-processing)
    await _save_raw_nrs_payload(store.store_id, target_date, raw)

    canonical = transform_daily_sales(raw, target_date)
    log.info(
        "NRS fetch complete for %s: product_sales=%.2f grand_total=%.2f",
        store.store_id, canonical.get("product_sales", 0), canonical.get("grand_total", 0),
    )
    return canonical


async def _fetch_modisoft(store: "StoreProfile", target_date: date) -> dict:
    """Fetch from Modisoft mobile API, save raw payload, return canonical dict."""
    from tools.pos.modisoft.client import fetch_raw_closing, ModisoftTokenExpiredError
    from tools.pos.modisoft.transformer import transform_daily_sales

    log.info("Fetching Modisoft daily sales for %s on %s", store.store_id, target_date)
    try:
        raw = await fetch_raw_closing(store.store_id, target_date)
    except ModisoftTokenExpiredError:
        # Token was cleared inside the client — retry once with fresh auth
        log.info("Modisoft token expired — retrying with fresh login")
        raw = await fetch_raw_closing(store.store_id, target_date)

    await _save_raw_modisoft_payload(store.store_id, target_date, raw)

    canonical = transform_daily_sales(raw, target_date)
    log.info(
        "Modisoft fetch complete for %s: product_sales=%.2f fuel=%.2f grand_total=%.2f",
        store.store_id, canonical.get("product_sales", 0),
        canonical.get("gas_dollars", 0), canonical.get("grand_total", 0),
    )
    return canonical


async def _save_raw_modisoft_payload(store_id: str, fetch_date: date, payload: dict) -> None:
    """
    Persist raw Modisoft response to pending_state (temporary safety net).
    When raw_modisoft.raw_sales_payloads table exists, move this there.
    """
    try:
        from db.state import save_state
        await save_state(
            store_id,
            f"modisoft_raw_{fetch_date.isoformat()}",
            {"payload": payload, "fetched_at": str(fetch_date)},
        )
        log.debug("Saved raw Modisoft payload for %s on %s", store_id, fetch_date)
    except Exception:
        log.exception("Failed to save raw Modisoft payload — continuing")


async def _save_raw_nrs_payload(store_id: str, fetch_date: date, payload: dict) -> None:
    """Persist raw NRS API response to raw_nrs.raw_sales_payloads."""
    try:
        from db.database import get_async_session
        from db.models import RawNRSPayload
        async with get_async_session() as session:
            row = RawNRSPayload(
                store_id=store_id,
                fetch_date=fetch_date,
                payload=payload,
            )
            session.add(row)
            await session.commit()
        log.debug("Saved raw NRS payload for %s on %s", store_id, fetch_date)
    except Exception:
        # Never let payload logging block the main flow
        log.exception("Failed to save raw NRS payload for %s on %s — continuing", store_id, fetch_date)
