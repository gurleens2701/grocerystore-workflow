#!/usr/bin/env python3
"""
manage.py — Admin CLI for provisioning new stores

Usage:
  python manage.py add-store       Provision a new store
  python manage.py list-stores     List all configured stores
  python manage.py remove-store    Remove a store (keeps DB data)
"""

import os
import re
import secrets
import string
import subprocess
import sys

import psycopg2
import yaml

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STORES_DIR = os.path.join(BASE_DIR, "stores")
DC_FILE    = os.path.join(BASE_DIR, "docker-compose.yml")
ENV_FILE   = os.path.join(BASE_DIR, ".env")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_env(path: str) -> dict:
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

def _rand_password(n=16) -> str:
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(n))

def _rand_hex(n=32) -> str:
    return secrets.token_hex(n)

def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")

def _prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{msg}{suffix}: ").strip()
    return val or default

def _prompt_required(msg: str) -> str:
    while True:
        val = input(f"{msg}: ").strip()
        if val:
            return val
        print("  ✗ Required — please enter a value.")

def _get_pg_password() -> str:
    env = _read_env(ENV_FILE)
    pw = env.get("POSTGRES_PASSWORD", "")
    if not pw:
        pw = os.environ.get("POSTGRES_PASSWORD", "")
    return pw

def _create_db(store_id: str) -> None:
    pg_pass = _get_pg_password()
    conn = psycopg2.connect(
        host="localhost", port=5432,
        user="gasbot", password=pg_pass,
        dbname="gasbot_template",
    )
    conn.autocommit = True
    cur = conn.cursor()
    db_name = f"gasbot_{store_id}"
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
    if cur.fetchone():
        print(f"  ✓ Database {db_name} already exists")
    else:
        cur.execute(f'CREATE DATABASE "{db_name}" OWNER gasbot')
        print(f"  ✓ Created database {db_name}")
    cur.close()
    conn.close()

def _run_migrations(store_id: str) -> None:
    env = os.environ.copy()
    env["STORE_ID"] = store_id
    result = subprocess.run(
        ["docker", "exec", "-e", f"STORE_ID={store_id}", "app-api-1",
         "alembic", "upgrade", "head"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  ⚠ Migration warning: {result.stderr[-300:]}")
    else:
        print(f"  ✓ Migrations applied for {store_id}")

def _write_store_env(store_id: str, data: dict) -> str:
    os.makedirs(STORES_DIR, exist_ok=True)
    path = os.path.join(STORES_DIR, f"{store_id}.env")
    lines = [f"{k}={v}" for k, v in data.items()]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path

def _add_to_docker_compose(store_id: str) -> None:
    with open(DC_FILE) as f:
        content = f.read()

    # Don't add if already present
    if f"STORE_ID: {store_id}" in content:
        print(f"  ✓ docker-compose already has service for {store_id}")
        return

    service_block = f"""
  # ── Store: {store_id} ──────────────────────────────────────────────────────
  app_{store_id}:
    build:
      context: .
      dockerfile: Dockerfile
    command: python main.py
    restart: always
    stop_grace_period: 15s
    env_file: .env
    environment:
      STORE_ID: {store_id}
    depends_on:
      postgres:
        condition: service_healthy
    volumes:
      - ./stores:/app/stores:ro
      - ./config:/app/config:ro
      - ./reports:/app/reports
"""

    # Insert before the volumes: section at the end
    content = content.replace("\nvolumes:", service_block + "\nvolumes:")
    with open(DC_FILE, "w") as f:
        f.write(content)
    print(f"  ✓ Added app_{store_id} service to docker-compose.yml")

def _remove_from_docker_compose(store_id: str) -> None:
    with open(DC_FILE) as f:
        content = f.read()

    # Remove the block between the store comment and the next service/volume
    pattern = rf"\n  # ── Store: {store_id} ──.*?(?=\n  [a-z#]|\nvolumes:)"
    new_content = re.sub(pattern, "", content, flags=re.DOTALL)
    if new_content == content:
        print(f"  ℹ No docker-compose entry found for {store_id}")
        return
    with open(DC_FILE, "w") as f:
        f.write(new_content)
    print(f"  ✓ Removed app_{store_id} from docker-compose.yml")

# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_list_stores():
    stores = []
    for f in sorted(os.listdir(STORES_DIR)):
        if f.endswith(".env"):
            store_id = f[:-4]
            env = _read_env(os.path.join(STORES_DIR, f))
            name = env.get("STORE_NAME", store_id)
            stores.append((store_id, name))
    if not stores:
        print("No stores configured.")
        return
    print(f"\n{'ID':<20} {'Name'}")
    print("-" * 45)
    for sid, name in stores:
        print(f"{sid:<20} {name}")
    print()

def cmd_add_store():
    print("\n── Add New Store ─────────────────────────────────────")

    store_name = _prompt_required("Store name (e.g. Liberty Foodmart)")
    store_id   = _prompt("Store ID (slug)", _slugify(store_name))
    timezone   = _prompt("Timezone", "America/New_York")

    print("\n── POS / Back Office ──────────────────────────────────")
    has_nrs = _prompt("Does this store use NRS Plus? (y/n)", "y").lower() == "y"
    nrs_username = ""
    nrs_password = ""
    if has_nrs:
        nrs_username = _prompt_required("NRS username (email)")
        nrs_password = _prompt_required("NRS password")

    print("\n── Telegram ───────────────────────────────────────────")
    print("  Create a bot at t.me/BotFather → /newbot, then paste the token below.")
    bot_token = _prompt_required("Telegram bot token")
    chat_id   = _prompt_required("Telegram chat ID (send /start to @userinfobot to get it)")

    print("\n── Google Sheets ──────────────────────────────────────")
    sheet_id = _prompt("Google Sheet ID (leave blank to skip)", "")

    print("\n── Dashboard Login ────────────────────────────────────")
    dash_user = _prompt("Dashboard username", f"admin_{store_id}")
    dash_pass = _prompt("Dashboard password (leave blank to auto-generate)", "")
    if not dash_pass:
        dash_pass = _rand_password()
        print(f"  Generated password: {dash_pass}")

    jwt_secret = _rand_hex(32)

    # Read shared keys from root .env
    root_env = _read_env(ENV_FILE)

    store_data = {
        "STORE_ID":           store_id,
        "STORE_NAME":         store_name,
        "TIMEZONE":           timezone,
        "TELEGRAM_BOT_TOKEN": bot_token,
        "TELEGRAM_CHAT_ID":   chat_id,
        "NRS_USERNAME":       nrs_username,
        "NRS_PASSWORD":       nrs_password,
        "GOOGLE_SHEET_ID":    sheet_id,
        "DASHBOARD_USERNAME": dash_user,
        "DASHBOARD_PASSWORD": dash_pass,
        "DASHBOARD_STORES":   store_id,
        "JWT_SECRET":         jwt_secret,
        "PLAID_ENABLED":      "false",
        "PLAID_ACCESS_TOKEN": "",
        "PLAID_ACCOUNT_ID":   "",
        "PLAID_CLIENT_ID":    "",
        "PLAID_SECRET":       "",
        "POSTGRES_PASSWORD":  "",  # inherited from root .env
    }

    print("\n── Provisioning ───────────────────────────────────────")

    # 1. Write stores/{store_id}.env
    env_path = _write_store_env(store_id, store_data)
    print(f"  ✓ Created {env_path}")

    # 2. Create PostgreSQL database
    try:
        _create_db(store_id)
    except Exception as e:
        print(f"  ⚠ DB creation failed (may need to run on server): {e}")

    # 3. Run Alembic migrations
    try:
        _run_migrations(store_id)
    except Exception as e:
        print(f"  ⚠ Migration failed (run manually on server): {e}")

    # 4. Add to docker-compose
    _add_to_docker_compose(store_id)

    print(f"""
── Done! ─────────────────────────────────────────────────────
Store ID:   {store_id}
Store Name: {store_name}

Dashboard:
  URL:      http://178.104.61.18/login
  Username: {dash_user}
  Password: {dash_pass}

Next steps on the server:
  git pull
  docker compose up --build -d app_{store_id}
──────────────────────────────────────────────────────────────
""")

def cmd_remove_store():
    cmd_list_stores()
    store_id = _prompt_required("Store ID to remove")
    env_path = os.path.join(STORES_DIR, f"{store_id}.env")
    if not os.path.exists(env_path):
        print(f"✗ No store found with ID '{store_id}'")
        return
    confirm = _prompt(f"Remove {store_id}? DB data is kept. (yes/no)", "no")
    if confirm.lower() != "yes":
        print("Cancelled.")
        return
    os.remove(env_path)
    print(f"  ✓ Removed {env_path}")
    _remove_from_docker_compose(store_id)
    print(f"\nDone. Run: docker compose stop app_{store_id} && docker compose rm -f app_{store_id}")

# ── Entry point ───────────────────────────────────────────────────────────────

COMMANDS = {
    "add-store":    cmd_add_store,
    "list-stores":  cmd_list_stores,
    "remove-store": cmd_remove_store,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in COMMANDS:
        print(__doc__)
        print("Commands:", ", ".join(COMMANDS))
        sys.exit(1)
    COMMANDS[cmd]()
