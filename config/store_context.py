"""
config/store_context.py

Per-store config lookup keyed off a context variable. The current store_id
is set at request boundaries (Telegram handler, FastAPI endpoint, scheduler
dispatch, agent invocation). All sheet/store-scoped code reads from here so
we don't have to thread store_id through every function signature.

STRICT MODE — if no store is set in the contextvar AND no explicit arg is
passed, helpers raise RuntimeError. We do not silently fall back to the
default store. That used to leak Hamilton data into Moraine's sheet.

Usage at entry points:
    from config.store_context import set_active_store
    set_active_store("hamiltonomoil")

Usage in code that needs the active store:
    from config.store_context import get_active_store, get_store_sheet_id
    sid = get_active_store()                 # raises if not set
    sheet_id = get_store_sheet_id()          # raises if not set or unconfigured
"""

import os
from contextvars import ContextVar
from pathlib import Path

# Active store for the current async task. None means "no store context",
# which is a programmer error in strict mode and raises on lookup.
_current_store_id: ContextVar[str | None] = ContextVar("current_store_id", default=None)


def set_active_store(store_id: str | None) -> None:
    """Set (or clear) the active store_id for the current async task."""
    _current_store_id.set(store_id)


def get_active_store(*, required: bool = True) -> str:
    """
    Return the active store_id from the contextvar.

    With required=True (default): raises RuntimeError if not set. Use this
    in any code path that must be store-scoped.

    With required=False: returns "" if not set. Use this only at boundaries
    where you want to detect missing context without raising.
    """
    sid = _current_store_id.get()
    if not sid:
        if required:
            raise RuntimeError(
                "No active store in context. set_active_store(store_id) must "
                "be called at the request boundary (Telegram handler, FastAPI "
                "endpoint, scheduler job, or agent run) before any store-scoped "
                "code executes."
            )
        return ""
    return sid


def _read_store_env(store_id: str, key: str) -> str | None:
    """Read a single key from stores/{store_id}.env. Returns None if missing."""
    env_path = Path("stores") / f"{store_id}.env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == key:
            return v.strip()
    return None


def get_store_sheet_id(store_id: str | None = None) -> str:
    """
    Return the Google Sheet ID for a store.

    Lookup order:
      1. Explicit store_id argument (if passed)
      2. Active store from contextvar

    Raises RuntimeError if no store can be resolved, or if the resolved
    store has no GOOGLE_SHEET_ID configured. Strict mode — never silently
    falls back to a different store's sheet.
    """
    sid = store_id or get_active_store()
    val = _read_store_env(sid, "GOOGLE_SHEET_ID")
    if not val:
        raise RuntimeError(
            f"GOOGLE_SHEET_ID not configured for store_id={sid!r}. "
            f"Expected in stores/{sid}.env. Use scripts/manage_store.py to "
            f"set it, or check that the file exists and is readable."
        )
    return val
