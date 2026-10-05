"""Tests for configuration (Pydantic BaseSettings) and logging (sensitive-field redaction)."""

from __future__ import annotations

import json
import logging

import pytest

from relay.config import Settings
from relay.logging_config import JSONFormatter, _redact, setup_logging

# ===========================================================================
# Config — Settings validation
# ===========================================================================


_SETTINGS_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "INKGLASS_JWT_SECRET",
    "ADMIN_SECRET",
    "DATABASE_URL",
    "ADMIN_MODE",
    "ENVIRONMENT",
    "LOG_LEVEL",
)


def _settings(**kwargs) -> Settings:
    """Build Settings from explicit values only: no .env file, no shell env (see fixture)."""
    return Settings(_env_file=None, **kwargs)


class TestSettings:
    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch):
        """Keep the developer's shell and CI variables out of these tests."""
        for name in _SETTINGS_ENV_VARS:
            monkeypatch.delenv(name, raising=False)

    def test_defaults(self):
        s = _settings(ANTHROPIC_API_KEY="key", INKGLASS_JWT_SECRET="secret")
        assert s.anthropic_api_key == "key"
        assert s.jwt_secret == "secret"
        assert s.database_url == "sqlite+aiosqlite:///./inkglass.db"
        assert s.admin_mode is False
        assert s.environment == "development"
        assert s.log_level == "INFO"

    def test_admin_mode_bool_coercion(self):
        s = _settings(ANTHROPIC_API_KEY="k", INKGLASS_JWT_SECRET="s", ADMIN_MODE="true")
        assert s.admin_mode is True

    def test_invalid_log_level_rejected(self):
        with pytest.raises(ValueError):
            _settings(ANTHROPIC_API_KEY="k", INKGLASS_JWT_SECRET="s", LOG_LEVEL="YOLO")

    def test_invalid_environment_rejected(self):
        with pytest.raises(ValueError):
            _settings(ANTHROPIC_API_KEY="k", INKGLASS_JWT_SECRET="s", ENVIRONMENT="mars")

    def test_production_requires_jwt_secret(self):
        with pytest.raises(ValueError, match="INKGLASS_JWT_SECRET must be set"):
            _settings(ANTHROPIC_API_KEY="k", ADMIN_SECRET="a", INKGLASS_JWT_SECRET="", ENVIRONMENT="production")

    def test_production_requires_api_key_and_admin_secret(self):
        # JWT secret set, so only the production-only secrets can fail.
        with pytest.raises(ValueError, match="Required environment variables not set: ANTHROPIC_API_KEY, ADMIN_SECRET"):
            _settings(ANTHROPIC_API_KEY="", INKGLASS_JWT_SECRET="s", ENVIRONMENT="production")

    def test_development_allows_empty_secrets(self):
        s = _settings(ANTHROPIC_API_KEY="", INKGLASS_JWT_SECRET="", ENVIRONMENT="development")
        assert s.anthropic_api_key == ""
        # Empty JWT secret is replaced with a fixed dev-only key, never "".
        assert s.jwt_secret

    def test_custom_database_url(self):
        s = _settings(
            ANTHROPIC_API_KEY="k",
            INKGLASS_JWT_SECRET="s",
            DATABASE_URL="postgresql+asyncpg://localhost/test",
        )
        assert s.database_url == "postgresql+asyncpg://localhost/test"

    def test_dotenv_file_is_ignored(self, tmp_path, monkeypatch):
        """A developer's .env must not change what these tests see."""
        (tmp_path / ".env").write_text("ENVIRONMENT=production\nDATABASE_URL=sqlite:///elsewhere.db\n")
        monkeypatch.chdir(tmp_path)
        s = _settings(ANTHROPIC_API_KEY="k", INKGLASS_JWT_SECRET="s")
        assert s.environment == "development"
        assert s.database_url == "sqlite+aiosqlite:///./inkglass.db"


# ===========================================================================
# Logging — redaction
# ===========================================================================


class TestRedact:
    def test_redacts_top_level_key(self):
        result = _redact({"api_key": "sk-123", "name": "test"})
        assert result["api_key"] == "[REDACTED]"
        assert result["name"] == "test"

    def test_redacts_nested_key(self):
        result = _redact({"outer": {"password": "hunter2", "user": "bob"}})
        assert result["outer"]["password"] == "[REDACTED]"
        assert result["outer"]["user"] == "bob"

    def test_redacts_in_list_of_dicts(self):
        result = _redact([{"token": "abc"}, {"safe": "yes"}])
        assert result[0]["token"] == "[REDACTED]"
        assert result[1]["safe"] == "yes"

    def test_case_insensitive_key_match(self):
        result = _redact({"API_KEY": "val", "Password": "val"})
        assert result["API_KEY"] == "[REDACTED]"
        assert result["Password"] == "[REDACTED]"

    def test_non_dict_passthrough(self):
        assert _redact("plain string") == "plain string"
        assert _redact(42) == 42

    def test_jwt_secret_redacted(self):
        result = _redact({"jwt_secret": "s3cret", "config": "ok"})
        assert result["jwt_secret"] == "[REDACTED]"

    def test_player_prose_redacted(self):
        result = _redact({"player_prose": "long narrative text", "turn": 5})
        assert result["player_prose"] == "[REDACTED]"
        assert result["turn"] == 5


class TestJSONFormatter:
    def _make_record(self, msg: str, **extra: object) -> logging.LogRecord:
        record = logging.LogRecord(
            name="relay.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_basic_format(self):
        fmt = JSONFormatter()
        record = self._make_record("hello")
        output = json.loads(fmt.format(record))
        assert output["msg"] == "hello"
        assert output["level"] == "INFO"
        assert output["logger"] == "relay.test"
        assert "ts" in output

    def test_extra_fields_included(self):
        fmt = JSONFormatter()
        record = self._make_record("event", character_id="char_1", delta=5)
        output = json.loads(fmt.format(record))
        assert output["character_id"] == "char_1"
        assert output["delta"] == 5

    def test_sensitive_top_level_extra_redacted(self):
        fmt = JSONFormatter()
        record = self._make_record("auth", api_key="sk-live-abc123")
        output = json.loads(fmt.format(record))
        assert output["api_key"] == "[REDACTED]"

    def test_sensitive_nested_in_extra_redacted(self):
        fmt = JSONFormatter()
        record = self._make_record("request", headers={"authorization": "Bearer tok"})
        output = json.loads(fmt.format(record))
        assert output["headers"]["authorization"] == "[REDACTED]"

    def test_non_serializable_extra_repr(self):
        fmt = JSONFormatter()
        record = self._make_record("obj", custom_obj=object())
        output = json.loads(fmt.format(record))
        assert "object at" in output["custom_obj"]


class TestSetupLogging:
    def test_setup_sets_root_level(self):
        setup_logging(level="WARNING")
        root = logging.getLogger()
        assert root.level == logging.WARNING
        setup_logging(level="INFO")
