"""Normalize legacy timezone values to validated IANA keys.

Revision ID: 20260808_0008
Revises: 20260808_0007
"""

import logging
from collections.abc import Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0008"
down_revision: str | None = "20260808_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.timezones")
ALIASES = {
    "moscow": "Europe/Moscow",
    "msk": "Europe/Moscow",
    "москва": "Europe/Moscow",
}


def _normalize(value: str) -> str:
    candidate = ALIASES.get((value or "").strip().casefold(), (value or "").strip())
    try:
        return ZoneInfo(candidate).key
    except (ZoneInfoNotFoundError, ModuleNotFoundError):
        logger.warning("Replacing invalid legacy timezone %r with UTC", value)
        return "UTC"


def upgrade() -> None:
    connection = op.get_bind()
    for table in ("tenant_settings", "tenant_analysis_schedules"):
        rows = connection.execute(sa.text(f"SELECT id, timezone FROM {table}"))
        for row_id, value in rows:
            normalized = _normalize(value)
            if normalized != value:
                connection.execute(
                    sa.text(f"UPDATE {table} SET timezone = :timezone WHERE id = :id"),
                    {"timezone": normalized, "id": row_id},
                )


def downgrade() -> None:
    # Data normalization is intentionally irreversible.
    pass
