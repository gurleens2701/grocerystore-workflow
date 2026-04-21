"""
api/auth.py

JWT authentication utilities — per-user DB accounts.

Login flow:
  1. POST /api/auth/login with username + password
  2. authenticate_user() looks up platform.users, verifies bcrypt hash
  3. Fetches store_ids from platform.user_store_memberships
  4. Returns signed JWT containing {sub: username, store_ids: [...]}
  5. Every API request validates the JWT and calls resolve_store() to gate access

Fallback (until create_user.py has been run):
  If platform.users is empty, falls back to DASHBOARD_USERNAME / DASHBOARD_PASSWORD
  from .env so Moraine keeps working before any users are created.

Fix point: if "Access denied for this store" — check user_store_memberships for that username.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from config.settings import settings

log = logging.getLogger(__name__)

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


async def authenticate_user(username: str, password: str) -> tuple[bool, list[str]]:
    """
    Verify credentials and return (ok, store_ids).

    store_ids is the list of stores this user is allowed to access.
    Empty list means auth failed.
    """
    from sqlalchemy import select, func as sa_func
    from db.database import get_async_session
    from db.models import DashboardUser, UserStoreMembership

    async with get_async_session() as session:
        # Count existing users so we know if the DB is bootstrapped
        user_count = (
            await session.execute(select(sa_func.count()).select_from(DashboardUser))
        ).scalar_one()

        if user_count == 0:
            # No users in DB yet — fall back to .env credentials so existing
            # setup doesn't break before create_user.py is run
            log.warning(
                "platform.users is empty — using settings fallback. "
                "Run scripts/create_user.py to create proper accounts."
            )
            if username == settings.dashboard_username and password == settings.dashboard_password:
                store_ids = settings.allowed_stores or [settings.store_id]
                return True, store_ids
            return False, []

        # Normal path: look up user in DB
        user = (
            await session.execute(
                select(DashboardUser).where(
                    DashboardUser.username == username,
                    DashboardUser.is_active == True,
                )
            )
        ).scalars().first()

        if not user or not verify_password(password, user.password_hash):
            return False, []

        # Get their store memberships
        memberships = (
            await session.execute(
                select(UserStoreMembership).where(UserStoreMembership.user_id == user.id)
            )
        ).scalars().all()

        store_ids = [m.store_id for m in memberships]
        return True, store_ids


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except JWTError:
        return None
