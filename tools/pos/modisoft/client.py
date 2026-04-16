"""
tools/pos/modisoft/client.py

Modisoft POS connector — stub for Phase 3.

TODO (Phase 3 Modisoft onboarding):
  - Playwright login to Modisoft portal
  - Capture session token / API key
  - httpx calls to Modisoft API

Returns raw dicts from Modisoft API — no transformation here.
Transformation lives in transformer.py.
"""

from datetime import date
from typing import Any


async def fetch_raw_stats(target_date: date, credentials: dict) -> dict[str, Any]:
    """Fetch daily sales stats from Modisoft. Not yet implemented."""
    raise NotImplementedError(
        "Modisoft connector not yet implemented. "
        "See tools/pos/modisoft/client.py to build it."
    )


async def fetch_raw_inventory(credentials: dict) -> dict[str, Any]:
    """Fetch inventory from Modisoft. Not yet implemented."""
    raise NotImplementedError(
        "Modisoft connector not yet implemented. "
        "See tools/pos/modisoft/client.py to build it."
    )
