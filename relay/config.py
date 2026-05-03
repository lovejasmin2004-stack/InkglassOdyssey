from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    anthropic_api_key: str = os.environ["ANTHROPIC_API_KEY"]
    jwt_secret: str = os.environ["INKGLASS_JWT_SECRET"]
    database_url: str = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./inkglass.db")
    admin_mode: bool = os.environ.get("ADMIN_MODE", "false").lower() == "true"
    environment: str = os.environ.get("ENVIRONMENT", "development")
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")


settings = Settings()
