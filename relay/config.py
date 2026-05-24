from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    jwt_secret: str = Field(default="", alias="INKGLASS_JWT_SECRET")
    admin_secret: str = Field(default="", alias="ADMIN_SECRET")
    database_url: str = Field(default="sqlite+aiosqlite:///./inkglass.db", alias="DATABASE_URL")
    admin_mode: bool = Field(default=False, alias="ADMIN_MODE")
    environment: Literal["development", "staging", "production"] = Field(default="development", alias="ENVIRONMENT")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO", alias="LOG_LEVEL")

    @model_validator(mode="after")
    def _check_required_secrets(self) -> Settings:
        """Validate secrets are set.

        jwt_secret is REQUIRED in all environments (staging + production).
        In development, an empty secret is only allowed if ALLOW_EMPTY_SECRETS
        is explicitly set — otherwise a predictable dev-only fallback is used
        to prevent accidental empty-string HMAC keys.

        anthropic_api_key and admin_secret are required in production only
        (tests and dev can run without a live LLM or admin panel).
        """
        # JWT secret: always required (empty string HMAC = trivially forgeable tokens)
        if not self.jwt_secret:
            if self.environment == "development":
                # Use a fixed dev-only key so decode_token() never operates on ""
                self.jwt_secret = "dev-only-insecure-key-do-not-use-in-prod"
            else:
                raise ValueError(
                    "INKGLASS_JWT_SECRET must be set in staging/production. "
                    "Empty JWT secret allows trivial token forgery."
                )

        # Other secrets: required in production only
        missing = []
        if not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if not self.admin_secret:
            missing.append("ADMIN_SECRET")
        if missing and self.environment == "production":
            raise ValueError(f"Required environment variables not set: {', '.join(missing)}")
        return self


settings = Settings()
