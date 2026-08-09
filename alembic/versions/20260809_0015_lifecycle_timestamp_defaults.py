"""Add DB defaults required by lifecycle ORM timestamps.

Revision ID: 20260809_0015
Revises: 20260809_0014
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0015"
down_revision: str | None = "20260809_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_defaults(default: sa.TextClause | None) -> None:
    for table_name in ("problem_transitions", "problem_verifications"):
        with op.batch_alter_table(table_name) as batch:
            batch.alter_column(
                "created_at",
                existing_type=sa.DateTime(timezone=True),
                existing_nullable=False,
                server_default=default,
            )
            batch.alter_column(
                "updated_at",
                existing_type=sa.DateTime(timezone=True),
                existing_nullable=False,
                server_default=default,
            )


def upgrade() -> None:
    _set_defaults(sa.func.now())


def downgrade() -> None:
    _set_defaults(None)
