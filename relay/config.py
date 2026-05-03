from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(override=True)


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(
            f"Required environment variable {key} is not set. "
            f"Check your .env file or environment."
        )
    return value


class Settings:
    anthropic_api_key: str = _require_env("ANTHROPIC_API_KEY")
    jwt_secret: str = _require_env("INKGLASS_JWT_SECRET")
    database_url: str = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./inkglass.db")
    admin_mode: bool = os.environ.get("ADMIN_MODE", "false").lower() == "true"
    environment: str = os.environ.get("ENVIRONMENT", "development")
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")


settings = Settings()
