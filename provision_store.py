"""
provision_store.py — New store setup wizard.

Run this once per store to:
  1. Collect store credentials interactively
  2. Write stores/{store_id}.env
  3. Create the store's PostgreSQL database
  4. Run Alembic migrations on it
  5. Test NRS login, Telegram bot, and Google Sheets connection

Usage:
    python provision_store.py
"""

import asyncio
import os
import sys
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ask(prompt: str, default: str = "") -> str:
    if default:
        val = input(f"{prompt} [{default}]: ").strip()
        return val if val else default
    val = input(f"{prompt}: ").strip()
    return val


def ask_bool(prompt: str, default: bool = False) -> bool:
    default_str = "Y/n" if default else "y/N"
    val = input(f"{prompt} [{default_str}]: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


def section(title: str) -> None:
    print(f"\n{'─' * 50}")
    print(f"  {title}")
    print(f"{'─' * 50}")


# ---------------------------------------------------------------------------
# Step 1 — Collect store info
# ---------------------------------------------------------------------------

def collect_store_info() -> dict:
    section("STORE INFORMATION")
    store_id   = ask("Store ID (slug, no spaces)", "moraine").lower().replace(" ", "_")
    store_name = ask("Store name", "Moraine Foodmart")
    timezone   = ask("Timezone", "America/New_York")

    section("POS SYSTEM")
    print("Supported: nrs (more coming in Phase 3)")
    pos_type = ask("POS type", "nrs")

    section("NRS CREDENTIALS")
    nrs_username = ask("NRS login email")
    nrs_password = ask("NRS login password")

    section("TELEGRAM")
    print("Create a bot at https://t.me/BotFather to get a token.")
    telegram_token   = ask("Telegram bot token")
    telegram_chat_id = ask("Your Telegram chat ID")

    section("GOOGLE SHEETS")
    print("Share your Google Sheet with the service account email in config/google_credentials.json")
    google_sheet_id = ask("Google Sheet ID (from URL)")
    google_creds    = ask("Path to Google credentials JSON", "config/google_credentials.json")

    section("POSTGRESQL")
    postgres_host     = ask("PostgreSQL host", "localhost")
    postgres_port     = ask("PostgreSQL port", "5432")
    postgres_user     = ask("PostgreSQL user", "gasbot")
    postgres_password = ask("PostgreSQL password")

    section("PLAID BANK INTEGRATION (optional)")
    plaid_enabled = ask_bool("Enable Plaid bank integration?", False)
    plaid_client_id   = ""
    plaid_secret      = ""
    plaid_access_token  = ""
    plaid_account_id  = ""
    plaid_env         = "sandbox"
    if plaid_enabled:
        plaid_client_id    = ask("Plaid client ID")
        plaid_secret       = ask("Plaid secret")
        plaid_access_token = ask("Plaid access token")
        plaid_account_id   = ask("Plaid account ID")
        plaid_env          = ask("Plaid environment", "production")

    return {
        "store_id":         store_id,
        "store_name":       store_name,
        "timezone":         timezone,
        "pos_type":         pos_type,
        "nrs_username":     nrs_username,
        "nrs_password":     nrs_password,
        "telegram_token":   telegram_token,
        "telegram_chat_id": telegram_chat_id,
        "google_sheet_id":  google_sheet_id,
        "google_creds":     google_creds,
        "postgres_host":    postgres_host,
        "postgres_port":    postgres_port,
        "postgres_user":    postgres_user,
        "postgres_password": postgres_password,
        "plaid_enabled":    plaid_enabled,
        "plaid_client_id":  plaid_client_id,
        "plaid_secret":     plaid_secret,
        "plaid_access_token": plaid_access_token,
        "plaid_account_id": plaid_account_id,
        "plaid_env":        plaid_env,
    }


# ---------------------------------------------------------------------------
# Step 2 — Write stores/{store_id}.env
# ---------------------------------------------------------------------------

def write_env_file(info: dict) -> Path:
    stores_dir = Path("stores")
    stores_dir.mkdir(exist_ok=True)

    env_path = stores_dir / f"{info['store_id']}.env"
    content = f"""STORE_ID={info['store_id']}
STORE_NAME={info['store_name']}
TIMEZONE={info['timezone']}

NRS_USERNAME={info['nrs_username']}
NRS_PASSWORD={info['nrs_password']}

TELEGRAM_BOT_TOKEN={info['telegram_token']}
TELEGRAM_CHAT_ID={info['telegram_chat_id']}

GOOGLE_SHEET_ID={info['google_sheet_id']}
GOOGLE_CREDENTIALS_FILE={info['google_creds']}

POSTGRES_HOST={info['postgres_host']}
POSTGRES_PORT={info['postgres_port']}
POSTGRES_USER={info['postgres_user']}
POSTGRES_PASSWORD={info['postgres_password']}

PLAID_ENABLED={str(info['plaid_enabled']).lower()}
PLAID_CLIENT_ID={info['plaid_client_id']}
PLAID_SECRET={info['plaid_secret']}
PLAID_ACCESS_TOKEN={info['plaid_access_token']}
PLAID_ACCOUNT_ID={info['plaid_account_id']}
PLAID_ENV={info['plaid_env']}
"""
    env_path.write_text(content)
    print(f"\n✅ Written: {env_path}")
    return env_path


# ---------------------------------------------------------------------------
# Step 3 — Create PostgreSQL database
# ---------------------------------------------------------------------------

def create_database(info: dict) -> bool:
    db_name = f"gasbot_{info['store_id']}"
    print(f"\n⏳ Creating PostgreSQL database: {db_name}")

    try:
        import psycopg2
        # Connect to postgres (default DB) to create new DB
        conn = psycopg2.connect(
            host=info["postgres_host"],
            port=int(info["postgres_port"]),
            user=info["postgres_user"],
            password=info["postgres_password"],
            dbname="postgres",
        )
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute(f"SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
        if cur.fetchone():
            print(f"  ℹ️  Database '{db_name}' already exists — skipping creation.")
        else:
            cur.execute(f'CREATE DATABASE "{db_name}"')
            print(f"  ✅ Database '{db_name}' created.")

        cur.close()
        conn.close()
        return True

    except Exception as e:
        print(f"  ❌ Failed to create database: {e}")
        print("     Make sure PostgreSQL is running and credentials are correct.")
        return False


# ---------------------------------------------------------------------------
# Step 4 — Run Alembic migrations
# ---------------------------------------------------------------------------

def run_migrations(info: dict) -> bool:
    print(f"\n⏳ Running Alembic migrations on gasbot_{info['store_id']}...")

    env = os.environ.copy()
    env["STORE_ID"] = info["store_id"]
    # Make sure settings can load from the new env file
    env["POSTGRES_PASSWORD"] = info["postgres_password"]

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        print("  ✅ Migrations applied successfully.")
        return True
    else:
        print(f"  ❌ Migrations failed:\n{result.stderr}")
        return False


# ---------------------------------------------------------------------------
# Step 5 — Connection tests
# ---------------------------------------------------------------------------

async def test_telegram(info: dict) -> bool:
    print("\n⏳ Testing Telegram bot...")
    try:
        from telegram import Bot
        bot = Bot(token=info["telegram_token"])
        me = await bot.get_me()
        print(f"  ✅ Telegram OK — bot name: @{me.username}")
        await bot.send_message(
            chat_id=info["telegram_chat_id"],
            text=f"✅ {info['store_name']} bot is configured and ready.",
        )
        return True
    except Exception as e:
        print(f"  ❌ Telegram failed: {e}")
        return False


def test_google_sheets(info: dict) -> bool:
    print("\n⏳ Testing Google Sheets...")
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(
            info["google_creds"],
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        client = gspread.authorize(creds)
        sheet = client.open_by_key(info["google_sheet_id"])
        print(f"  ✅ Google Sheets OK — spreadsheet: {sheet.title}")
        return True
    except Exception as e:
        print(f"  ❌ Google Sheets failed: {e}")
        return False


def test_nrs(info: dict) -> bool:
    print("\n⏳ Testing NRS login (this takes ~15 seconds)...")
    try:
        os.environ["STORE_ID"] = info["store_id"]
        # Reload settings with new store
        import importlib
        import config.settings as settings_module
        importlib.reload(settings_module)

        from tools.nrs_tools import fetch_daily_sales
        sales = fetch_daily_sales()
        print(f"  ✅ NRS OK — yesterday's sales: ${sales.get('grand_total', 0):.2f}")
        return True
    except Exception as e:
        print(f"  ❌ NRS login failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("\n" + "=" * 50)
    print("  GAS STATION AGENT — Store Provisioning Wizard")
    print("=" * 50)
    print("\nThis wizard sets up a new store in ~5 minutes.")
    print("Have ready: NRS login, Telegram bot token, Google Sheet ID, PostgreSQL password.\n")

    info = collect_store_info()

    section("REVIEW")
    print(f"  Store:     {info['store_name']} ({info['store_id']})")
    print(f"  POS:       {info['pos_type'].upper()}")
    print(f"  Telegram:  chat {info['telegram_chat_id']}")
    print(f"  Sheet:     {info['google_sheet_id']}")
    print(f"  Database:  gasbot_{info['store_id']} @ {info['postgres_host']}")
    print(f"  Plaid:     {'enabled' if info['plaid_enabled'] else 'disabled'}")

    if not ask_bool("\nProceed with setup?", True):
        print("Aborted.")
        return

    # Run all steps
    write_env_file(info)

    db_ok   = create_database(info)
    mig_ok  = run_migrations(info) if db_ok else False
    tg_ok   = await test_telegram(info)
    sh_ok   = test_google_sheets(info)
    nrs_ok  = test_nrs(info)

    # Summary
    section("SETUP COMPLETE")
    print(f"  {'✅' if db_ok  else '❌'} PostgreSQL database created")
    print(f"  {'✅' if mig_ok else '❌'} Alembic migrations applied")
    print(f"  {'✅' if tg_ok  else '❌'} Telegram bot connected")
    print(f"  {'✅' if sh_ok  else '❌'} Google Sheets connected")
    print(f"  {'✅' if nrs_ok else '❌'} NRS login verified")

    if all([db_ok, mig_ok, tg_ok, sh_ok, nrs_ok]):
        print(f"\n🎉 {info['store_name']} is ready to go!")
        print(f"\nTo start the bot for this store:")
        print(f"  STORE_ID={info['store_id']} python main.py")
        print(f"\nOr with Docker:")
        print(f"  STORE_ID={info['store_id']} docker compose up -d")
    else:
        print("\n⚠️  Some steps failed. Fix the errors above and re-run this wizard.")


if __name__ == "__main__":
    asyncio.run(main())
