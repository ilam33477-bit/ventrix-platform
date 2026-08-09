from __future__ import annotations

import json
import logging
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
}


class StructuredJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        context = getattr(record, "safe_context", {})
        payload.update({key: value for key, value in context.items() if key in SAFE_CONTEXT_FIELDS})
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
