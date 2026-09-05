from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

SAFE_CONTEXT_FIELDS = {
    "correlation_id",
    "job_id",
    "tenant_id",
    "bot_instance_id",
    "telegram_account_id",
    "account_id",
    "dialog_id",
    "stage",
    "category",
    "worker_id",
    "duration_ms",
    "retry_count",
    "status",
    "error_type",
    "error_code",
    "component",
}

SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(/bot)\d{6,12}:[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b(authorization|api[_-]?key|token|initdata|password|2fa|otp|code)"
        r"\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){10,15}(?!\d)"),
)


def redact_log_text(value: object) -> str:
    text = str(value)
    for index, pattern in enumerate(SENSITIVE_PATTERNS):
        replacement = r"\1[REDACTED]" if index == 0 else "[REDACTED]"
        text = pattern.sub(replacement, text)
    return text[:1000]


def _safe_context_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_log_text(value)


class StructuredJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact_log_text(record.getMessage()),
        }
        context = getattr(record, "safe_context", {})
        payload.update(
            {
                key: _safe_context_value(value)
                for key, value in context.items()
                if key in SAFE_CONTEXT_FIELDS
            }
        )
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_structured_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJSONFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


def log_event(logger: logging.Logger, level: int, event: str, **context: Any) -> None:
    logger.log(
        level,
        event,
        extra={
            "safe_context": {
                key: value for key, value in context.items() if key in SAFE_CONTEXT_FIELDS
            }
        },
    )
