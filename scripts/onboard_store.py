"""
scripts/onboard_store.py

Interactive CLI to onboard a new store.

Usage (on VPS inside the container):
    docker compose exec app python scripts/onboard_store.py

What it does:
  1. Prompts for store identity + credentials
  2. Writes secrets to stores/{store_id}.env
  3. Inserts rows into platform.stores, store_workflows, store_scheduler_policies
  4. Copies Moraine's daily_report_rules + sheet_mappings + tool_policies as defaults
  5. Runs connectivity tests: Telegram, Google Sheet, POS login (optional)

After running:
  - Edit stores/{store_id}.env to adjust any credentials
  - Tweak platform.store_daily_report_rules for this store's actual fields
  - Restart the bot: docker compose restart app
"""

import asyncio
import os
import re
import sys
from pathlib import Path

# Make sure the project root is on the path when run from any directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEMPLATE_STORE_ID = "moraine"   # used as the default template for rules/mappings

SUPPORTED_POS = ["nrs", "modisoft", "manual"]

DEFAULT_JOBS = [
    ("daily_fetch",    "0 7 * * *"),
    ("bank_sync",      "every_4h"),
    ("nightly_sync",   "every_15m"),
    ("weekly_summary", "0 18 * * 0"),
    ("cashflow",       "0 8 L * *"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ask(prompt: str, default: str = "") -> str:
    """Prompt the user, show default, return stripped answer or default."""
    if default:
        display = f"{prompt} [{default}]: "
    else:
        display = f"{prompt}: "
    while True:
        val = input(display).strip()
        if val:
            return val
        if default:
            return default
        print("  (required — please enter a value)")


def ask_secret(prompt: str) -> str:
    """Prompt for a secret (no echo via getpass)."""
    import getpass
    while True:
        val = getpass.getpass(f"{prompt}: ").strip()
        if val:
            return val
        print("  (required)")


def slugify(name: str) -> str:
    """'Liberty Gas Station' → 'liberty'"""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    # Truncate to first meaningful word for brevity
    return slug.split("_")[0] if "_" in slug else slug


def yn(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

async def store_exists(store_id: str) -> bool:
    from db.database import get_async_session
    from db.models import Store
    from sqlalchemy import select
    async with get_async_session() as s:
        row = (await s.execute(select(Store).where(Store.store_id == store_id))).scalars().first()
    return row is not None


async def insert_store(store_id: str, store_name: str, pos_type: str, chat_id: str, timezone: str) -> None:
    from db.database import get_async_session
    from db.models import Store
    async with get_async_session() as s:
        s.add(Store(
            store_id=store_id, store_name=store_name, pos_type=pos_type,
            chat_id=chat_id, timezone=timezone, is_active=True,
        ))
        await s.commit()
    print(f"  platform.stores ✓")


async def insert_workflows(store_id: str, pos_type: str) -> None:
    from db.database import get_async_session
    from db.models import StoreWorkflow
    async with get_async_session() as s:
        s.add(StoreWorkflow(
            store_id=store_id,
            daily_report_enabled=True,
            daily_report_mode=f"{pos_type}_pull" if pos_type != "manual" else "manual_entry",
            manual_entry_enabled=True,
            nightly_sheet_sync=True,
            bank_recon_enabled=False,   # off by default — enable after Plaid is connected
            month_end_summary=True,
            weekly_bank_summary=True,
            invoice_ocr_enabled=False,
            unified_agent_enabled=True,
        ))
        await s.commit()
    print(f"  platform.store_workflows ✓")


async def insert_scheduler_policies(store_id: str) -> None:
    from db.database import get_async_session
    from db.models import StoreSchedulerPolicy
    async with get_async_session() as s:
        for job_name, schedule in DEFAULT_JOBS:
            s.add(StoreSchedulerPolicy(
                store_id=store_id, job_name=job_name, schedule=schedule, enabled=True,
            ))
        await s.commit()
    print(f"  platform.store_scheduler_policies ({len(DEFAULT_JOBS)} jobs) ✓")


async def copy_report_rules(store_id: str) -> int:
    """Copy daily_report_rules from TEMPLATE_STORE_ID to store_id."""
    from db.database import get_async_session
    from db.models import StoreDailyReportRule
    from sqlalchemy import select
    async with get_async_session() as s:
        rows = (await s.execute(
            select(StoreDailyReportRule).where(StoreDailyReportRule.store_id == TEMPLATE_STORE_ID)
        )).scalars().all()
        for r in rows:
            s.add(StoreDailyReportRule(
                store_id=store_id, section=r.section, field_name=r.field_name,
                label=r.label, source=r.source, display_order=r.display_order,
            ))
        await s.commit()
    print(f"  platform.store_daily_report_rules (copied {len(rows)} rows from {TEMPLATE_STORE_ID}) ✓")
    return len(rows)


async def copy_sheet_mappings(store_id: str) -> int:
    """Copy sheet_mappings from TEMPLATE_STORE_ID to store_id."""
    from db.database import get_async_session
    from db.models import StoreSheetMapping
    from sqlalchemy import select
    async with get_async_session() as s:
        rows = (await s.execute(
            select(StoreSheetMapping).where(StoreSheetMapping.store_id == TEMPLATE_STORE_ID)
        )).scalars().all()
        for r in rows:
            s.add(StoreSheetMapping(
                store_id=store_id, section=r.section, field_name=r.field_name,
                column_index=r.column_index, column_header=r.column_header,
            ))
        await s.commit()
    print(f"  platform.store_sheet_mappings (copied {len(rows)} rows from {TEMPLATE_STORE_ID}) ✓")
    return len(rows)


async def copy_tool_policies(store_id: str) -> int:
    """Copy tool_policies from TEMPLATE_STORE_ID, switching to pos_type-appropriate tools."""
    from db.database import get_async_session
    from db.models import StoreToolPolicy
    from sqlalchemy import select
    async with get_async_session() as s:
        rows = (await s.execute(
            select(StoreToolPolicy).where(StoreToolPolicy.store_id == TEMPLATE_STORE_ID)
        )).scalars().all()
        for r in rows:
            s.add(StoreToolPolicy(
                store_id=store_id, tool_name=r.tool_name, enabled=r.enabled,
            ))
        await s.commit()
    print(f"  platform.store_tool_policies (copied {len(rows)} tools from {TEMPLATE_STORE_ID}) ✓")
    return len(rows)


# ---------------------------------------------------------------------------
# Connectivity tests
# ---------------------------------------------------------------------------

async def test_telegram(bot_token: str, chat_id: str) -> bool:
    """Verify bot token is valid and can reach the chat."""
    try:
        from telegram import Bot
        bot = Bot(token=bot_token)
        me = await bot.get_me()
        print(f"  Telegram bot: ✓  @{me.username}")
        # Try sending a test message
        msg = await bot.send_message(
            chat_id=int(chat_id),
            text="✅ Gas Station Agent connected. Onboarding in progress...",
        )
        print(f"  Test message sent to chat {chat_id} ✓")
        return True
    except Exception as e:
        print(f"  Telegram: ✗  {e}")
        return False


async def test_google_sheet(sheet_id: str) -> bool:
    """Try to open the Google Sheet."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file("config/google_credentials.json", scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)
        print(f"  Google Sheet: ✓  \"{sh.title}\"")
        return True
    except Exception as e:
        print(f"  Google Sheet: ✗  {e}")
        return False


async def test_nrs_login() -> bool:
    """Run Playwright NRS login. Slow (~30s) — user can skip."""
    try:
        from tools.pos.nrs.client import _authenticate
        token = await _authenticate()
        print(f"  NRS login: ✓  token={token[:12]}…")
        return True
    except Exception as e:
        print(f"  NRS login: ✗  {e}")
        return False


# ---------------------------------------------------------------------------
# Secrets file
# ---------------------------------------------------------------------------

def write_env_file(store_id: str, fields: dict[str, str]) -> Path:
    """Write a stores/{store_id}.env file with the given key=value pairs."""
    stores_dir = Path("stores")
    stores_dir.mkdir(exist_ok=True)
    env_path = stores_dir / f"{store_id}.env"
    lines = [f"{k}={v}\n" for k, v in fields.items()]
    env_path.write_text("".join(lines))
    os.chmod(env_path, 0o600)   # owner-read only — contains credentials
    print(f"  Wrote {env_path}  (chmod 600)")
    return env_path


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

async def main() -> None:
    print("\n" + "=" * 60)
    print("  Gas Station Agent — Store Onboarding")
    print("=" * 60)
    print()

    # --- Identity ---
    store_name = ask("Store name (e.g. Liberty Gas)")
    suggested_id = slugify(store_name)
    store_id = ask("Store ID (short slug, no spaces)", default=suggested_id)
    if not re.fullmatch(r"[a-z0-9_]{1,32}", store_id):
        print("Store ID must be lowercase letters, digits, underscores, max 32 chars.")
        sys.exit(1)

    if await store_exists(store_id):
        print(f"\n⚠  store_id={store_id!r} already exists in platform.stores.")
        if not yn("Overwrite existing config?", default=False):
            print("Aborted.")
            sys.exit(0)
        # Delete existing rows so we can re-insert
        await _delete_store(store_id)

    pos_type = ask(f"POS type {SUPPORTED_POS}", default="nrs")
    if pos_type not in SUPPORTED_POS:
        print(f"Unknown POS type. Choose from: {SUPPORTED_POS}")
        sys.exit(1)

    chat_id = ask("Telegram chat ID (from /getid or bot logs)")
    timezone = ask("Timezone", default="America/New_York")

    # --- Credentials ---
    print()
    secrets: dict[str, str] = {
        "TELEGRAM_CHAT_ID": chat_id,
        "STORE_NAME": store_name,
    }

    if pos_type == "nrs":
        print("--- NRS Credentials ---")
        secrets["NRS_USERNAME"] = ask("NRS username (email)")
        secrets["NRS_PASSWORD"] = ask_secret("NRS password")

    elif pos_type == "modisoft":
        print("--- Modisoft Credentials ---")
        secrets["MODISOFT_USERNAME"] = ask("Modisoft username")
        secrets["MODISOFT_PASSWORD"] = ask_secret("Modisoft password")

    print()
    print("--- Google Sheets ---")
    google_sheet_id = ask("Google Sheet ID (the long ID from the URL)")
    secrets["GOOGLE_SHEET_ID"] = google_sheet_id

    # --- Template copy options ---
    print()
    copy_rules = yn(f"Copy daily sheet fields from {TEMPLATE_STORE_ID} as starting point?", default=True)
    copy_mappings = yn(f"Copy Google Sheet column mappings from {TEMPLATE_STORE_ID}?", default=True)
    copy_tools = yn(f"Copy agent tool policies from {TEMPLATE_STORE_ID}?", default=True)

    # --- Confirm ---
    print()
    print("-" * 40)
    print(f"  store_id  : {store_id}")
    print(f"  name      : {store_name}")
    print(f"  pos_type  : {pos_type}")
    print(f"  chat_id   : {chat_id}")
    print(f"  timezone  : {timezone}")
    print(f"  sheet_id  : {google_sheet_id}")
    print(f"  rules     : {'copy from ' + TEMPLATE_STORE_ID if copy_rules else 'none (add manually)'}")
    print("-" * 40)
    if not yn("Proceed?", default=True):
        print("Aborted.")
        sys.exit(0)

    # --- Write secrets file ---
    print()
    print("Writing secrets file...")
    write_env_file(store_id, secrets)

    # --- DB inserts ---
    print()
    print("Inserting into database...")
    await insert_store(store_id, store_name, pos_type, chat_id, timezone)
    await insert_workflows(store_id, pos_type)
    await insert_scheduler_policies(store_id)
    if copy_rules:
        await copy_report_rules(store_id)
    if copy_mappings:
        await copy_sheet_mappings(store_id)
    if copy_tools:
        await copy_tool_policies(store_id)

    # --- Connectivity tests ---
    print()
    print("Running connectivity tests...")
    from config.settings import settings as _s
    bot_token = _s.telegram_bot_token

    await test_telegram(bot_token, chat_id)
    await test_google_sheet(google_sheet_id)

    if pos_type == "nrs":
        run_nrs = ask("Test NRS login? (slow ~30s) [y/N]", default="n").lower()
        if run_nrs == "y":
            await test_nrs_login()
        else:
            print("  NRS login: skipped")

    # --- Done ---
    print()
    print(f"✅  Store \"{store_name}\" ({store_id}) onboarded successfully!")
    print()
    print("Next steps:")
    print(f"  1. Review/edit daily fields:")
    print(f"       SELECT * FROM platform.store_daily_report_rules WHERE store_id = '{store_id}';")
    print(f"  2. Review/edit sheet column mapping:")
    print(f"       SELECT * FROM platform.store_sheet_mappings WHERE store_id = '{store_id}';")
    print(f"  3. Restart the bot:")
    print(f"       docker compose restart app")
    print(f"  4. Send /daily from the store's Telegram chat to verify.")
    print()


async def _delete_store(store_id: str) -> None:
    """Remove all platform config rows for a store (for re-onboarding)."""
    from db.database import get_async_session
    from db.models import (
        Store, StoreWorkflow, StoreSchedulerPolicy,
        StoreDailyReportRule, StoreSheetMapping, StoreToolPolicy,
    )
    from sqlalchemy import delete
    async with get_async_session() as s:
        for model in [StoreToolPolicy, StoreSheetMapping, StoreDailyReportRule,
                      StoreSchedulerPolicy, StoreWorkflow, Store]:
            await s.execute(delete(model).where(model.store_id == store_id))
        await s.commit()
    print(f"  Deleted existing config for store_id={store_id!r}")


if __name__ == "__main__":
    asyncio.run(main())
