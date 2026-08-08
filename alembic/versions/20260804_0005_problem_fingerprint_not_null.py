"""Require an idempotency fingerprint on every operational problem.

Revision ID: 20260804_0005
Revises: 20260804_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260804_0005"
down_revision: str | None = "20260804_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("operational_problems") as batch:
        batch.alter_column(
            "fingerprint",
            existing_type=sa.String(length=200),
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("operational_problems") as batch:
        batch.alter_column(
            "fingerprint",
            existing_type=sa.String(length=200),
            nullable=True,
        )
