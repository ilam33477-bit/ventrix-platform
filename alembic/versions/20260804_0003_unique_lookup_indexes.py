"""Align explicitly named lookup indexes with unique model columns.

Revision ID: 20260804_0003
Revises: 20260804_0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260804_0003"
down_revision: str | None = "20260804_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNIQUE_INDEXES = (
    ("ix_platform_owner_telegram_user_id", "platform_owner", ["telegram_user_id"]),
    ("ix_tenant_settings_tenant_id", "tenant_settings", ["tenant_id"]),
    ("ix_tenant_ai_profiles_tenant_id", "tenant_ai_profiles", ["tenant_id"]),
    ("ix_bot_instances_telegram_bot_id", "bot_instances", ["telegram_bot_id"]),
    ("ix_bot_instances_username", "bot_instances", ["username"]),
)


def upgrade() -> None:
    for name, table, columns in UNIQUE_INDEXES:
        op.drop_index(name, table_name=table)
        op.create_index(name, table, columns, unique=True)


def downgrade() -> None:
    for name, table, columns in reversed(UNIQUE_INDEXES):
        op.drop_index(name, table_name=table)
        op.create_index(name, table, columns, unique=False)
