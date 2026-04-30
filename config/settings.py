import os

from pydantic_settings import BaseSettings, SettingsConfigDict

# Read store ID from environment before Settings class loads
# so we can point it at the right store-specific env file
_store_id = os.environ.get("STORE_ID", "moraine")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[".env", f"stores/{_store_id}.env"],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Store identity
    store_id: str = "moraine"
    store_name: str = "Store"
    timezone: str = "America/New_York"

    # Anthropic
    anthropic_api_key: str = ""

    # OpenAI — Whisper voice transcription (optional, enables voice messages)
    openai_api_key: str = ""

    # NRS POS
    nrs_username: str = ""
    nrs_password: str = ""
    nrs_url: str = "https://mystore.nrsplus.com/merchant/nocache2026012917492101/index.html#/home"

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Google Sheets
    google_sheet_id: str = ""
    google_credentials_file: str = "config/google_credentials.json"

    # PostgreSQL
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "gasbot"
    postgres_password: str = ""

    # Dashboard auth
    dashboard_username: str = "admin"
    dashboard_password: str = "changeme"
    jwt_secret: str = "change-this-secret-in-production"
    # Comma-separated list of store IDs this dashboard user can access.
    # e.g. DASHBOARD_STORES=moraine,liberty
    dashboard_stores: str = ""

    @property
    def allowed_stores(self) -> list[str]:
        raw = self.dashboard_stores.strip()
        if raw:
            return [s.strip() for s in raw.split(",") if s.strip()]
        return [self.store_id]

    # Plaid (optional per store)
    plaid_enabled: bool = False
    plaid_client_id: str = ""
    plaid_secret: str = ""
    plaid_env: str = "sandbox"
    plaid_access_token: str = ""
    plaid_account_id: str = ""

    @property
    def db_url(self) -> str:
        """Async SQLAlchemy URL (asyncpg) — used by the bot at runtime."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/gasbot_{self.store_id}"
        )

    @property
    def db_url_sync(self) -> str:
        """Sync SQLAlchemy URL (psycopg2) — used by Alembic migrations only."""
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/gasbot_{self.store_id}"
        )


settings = Settings()
