"""JSON logging with redaction for common secret-bearing fields and values."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|token|api[_-]?key|secret|authorization|cookie|credential)",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_SENSITIVE_HEADER = re.compile(r"(?i)(\b(?:authorization|cookie|set-cookie)\b\s*[:=]\s*)[^\r\n]+")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:password|passwd|token|access[_-]?token|refresh[_-]?token|"
    r"api[_-]?key|secret|authorization|cookie|set-cookie|credential)\b\s*[:=]\s*)"
    r'(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|[^\s,;]+)'
)
_STANDARD_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def _redact_text(value: str) -> str:
    value = _SENSITIVE_HEADER.sub(r"\1[REDACTED]", value)
    value = _BEARER_VALUE.sub("Bearer [REDACTED]", value)
    return _SENSITIVE_ASSIGNMENT.sub(r"\1[REDACTED]", value)


def _redact_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(child_key): _redact_value(str(child_key), child) for child_key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(key, item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # Avoid calling arbitrary __str__/__repr__ methods that could disclose data.
    return f"<{type(value).__name__}>"


class JsonRedactingFormatter(logging.Formatter):
    """Serialize records as JSON while redacting known sensitive fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_KEYS and not key.startswith("_"):
                payload[key] = _redact_value(key, value)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str) -> None:
    """Configure structured root logging without exception or request payloads."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonRedactingFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    # Uvicorn's access log includes request targets, which may contain secrets.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.disabled = True
    access_logger.propagate = False

    # Uvicorn's default error handler formats exc_info with the exception message
    # and traceback. Route error records through the root redacting formatter,
    # which intentionally serializes neither exc_info nor raw exception objects.
    error_logger = logging.getLogger("uvicorn.error")
    error_logger.handlers.clear()
    error_logger.disabled = False
    error_logger.propagate = True
