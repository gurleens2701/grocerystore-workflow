"""
scripts/create_user.py

Create or update a dashboard user account with store access.

Usage (on VPS):
    docker compose exec app python scripts/create_user.py

Run this once per owner before they log in. Each user gets:
  - A username + bcrypt-hashed password stored in platform.users
  - A row in platform.user_store_memberships for each store they can access

After running this for at least one user, the settings-based fallback
(DASHBOARD_USERNAME / DASHBOARD_PASSWORD) is disabled automatically.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def ask(prompt: str, default: str = "") -> str:
    display = f"{prompt} [{default}]: " if default else f"{prompt}: "
    while True:
        val = input(display).strip()
        if val:
            return val
        if default:
            return default
        print("  (required)")


def ask_secret(prompt: str) -> str:
    import getpass
    while True:
        val = getpass.getpass(f"{prompt}: ").strip()
        if val:
            return val
        print("  (required)")


async def list_active_stores() -> list[tuple[str, str]]:
    """Returns [(store_id, store_name), ...]"""
    from db.database import get_async_session
    from db.models import Store
    from sqlalchemy import select
    async with get_async_session() as s:
        rows = (await s.execute(select(Store).where(Store.is_active == True))).scalars().all()
    return [(r.store_id, r.store_name) for r in rows]


async def get_user(username: str):
    from db.database import get_async_session
    from db.models import DashboardUser
    from sqlalchemy import select
    async with get_async_session() as s:
        return (await s.execute(
            select(DashboardUser).where(DashboardUser.username == username)
        )).scalars().first()


async def create_or_update_user(username: str, password_hash: str) -> int:
    """Insert or update user, return user.id."""
    from db.database import get_async_session
    from db.models import DashboardUser
    from sqlalchemy import select
    async with get_async_session() as s:
        user = (await s.execute(
            select(DashboardUser).where(DashboardUser.username == username)
        )).scalars().first()
        if user:
            user.password_hash = password_hash
            user.is_active = True
        else:
            user = DashboardUser(username=username, password_hash=password_hash, is_active=True)
            s.add(user)
        await s.flush()
        user_id = user.id
        await s.commit()
    return user_id


async def set_memberships(user_id: int, store_ids: list[str], role: str) -> None:
    """Replace the user's store memberships."""
    from db.database import get_async_session
    from db.models import UserStoreMembership
    from sqlalchemy import delete
    async with get_async_session() as s:
        # Remove existing memberships
        await s.execute(delete(UserStoreMembership).where(UserStoreMembership.user_id == user_id))
        # Insert new ones
        for sid in store_ids:
            s.add(UserStoreMembership(user_id=user_id, store_id=sid, role=role))
        await s.commit()


async def main() -> None:
    print("\n" + "=" * 55)
    print("  Gas Station Agent — Dashboard User Setup")
    print("=" * 55)
    print()

    stores = await list_active_stores()
    if not stores:
        print("No active stores in platform.stores. Run scripts/onboard_store.py first.")
        sys.exit(1)

    print("Active stores:")
    for i, (sid, name) in enumerate(stores, 1):
        print(f"  {i}. {name}  ({sid})")
    print()

    # --- Username ---
    username = ask("Username")
    existing = await get_user(username)
    if existing:
        print(f"  User '{username}' already exists — updating password and store access.")

    # --- Password ---
    from api.auth import get_password_hash
    password = ask_secret("Password")
    confirm = ask_secret("Confirm password")
    if password != confirm:
        print("Passwords do not match.")
        sys.exit(1)
    password_hash = get_password_hash(password)

    # --- Store access ---
    if len(stores) == 1:
        # Only one store — grant it automatically
        chosen_ids = [stores[0][0]]
        print(f"  Granting access to: {stores[0][1]}")
    else:
        print()
        print("Which stores should this user access?")
        print("  Enter comma-separated numbers (e.g. 1,2) or 'all'")
        raw = ask("Store access", default="all")
        if raw.strip().lower() == "all":
            chosen_ids = [s[0] for s in stores]
        else:
            chosen = []
            for part in raw.split(","):
                part = part.strip()
                if part.isdigit():
                    idx = int(part) - 1
                    if 0 <= idx < len(stores):
                        chosen.append(stores[idx][0])
            chosen_ids = chosen
            if not chosen_ids:
                print("No valid stores selected.")
                sys.exit(1)

    # --- Role ---
    role = ask("Role [owner/manager/viewer]", default="owner")
    if role not in ("owner", "manager", "viewer"):
        role = "owner"

    # --- Confirm ---
    print()
    store_names = [name for sid, name in stores if sid in chosen_ids]
    print("-" * 40)
    print(f"  username : {username}")
    print(f"  stores   : {', '.join(store_names)}")
    print(f"  role     : {role}")
    print("-" * 40)
    yn = input("Create user? [Y/n]: ").strip().lower()
    if yn and not yn.startswith("y"):
        print("Aborted.")
        sys.exit(0)

    # --- Write to DB ---
    user_id = await create_or_update_user(username, password_hash)
    await set_memberships(user_id, chosen_ids, role)

    action = "Updated" if existing else "Created"
    print(f"\n✅  {action} user '{username}' with access to: {', '.join(store_names)}")
    print()
    print("The user can now log in at https://clerkai.live")
    print()


if __name__ == "__main__":
    asyncio.run(main())
