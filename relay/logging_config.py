from __future__ import annotations

import json
import logging
import logging.config
import time

# Fields redacted from all log output (CLAUDE.md: never log API keys or prose at INFO+)
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "anthropic_api_key",
        "jwt_secret",
        "password",
        "password_hash",
        "token",
        "secret",
        "authorization",
        "player_prose",
        "npc_prose",
    }
)

_REDACTED = "[REDACTED]"


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "relay.logging_config.JSONFormatter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stdout",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "relay": {"level": "DEBUG", "propagate": True},
        "uvicorn": {"level": "INFO", "propagate": True},
        "uvicorn.error": {"level": "INFO", "propagate": True},
        "uvicorn.access": {"level": "INFO", "propagate": True},
    },
}

_BUILTIN_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
        "color_message",
    }
)


def _redact(value: object) -> object:
    """Recursively redact sensitive keys from dicts/lists."""
    if isinstance(value, dict):
        return {k: _REDACTED if k.lower() in _SENSITIVE_KEYS else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        for key in set(record.__dict__) - _BUILTIN_RECORD_ATTRS:
            val = record.__dict__[key]
            if key.lower() in _SENSITIVE_KEYS:
                payload[key] = _REDACTED
            else:
                val = _redact(val)
                try:
                    json.dumps(val)
                    payload[key] = val
                except (TypeError, ValueError):
                    payload[key] = repr(val)

        return json.dumps(payload)


def setup_logging(level: str = "INFO") -> None:
    config = LOGGING_CONFIG.copy()
    config["root"]["level"] = level
    logging.config.dictConfig(config)
