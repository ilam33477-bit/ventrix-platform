"""Add indexes used by runtime metrics and owner monitoring."""

from alembic import op

revision = "20260906_0024"
down_revision = "20260904_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("background_jobs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_background_jobs_created_at",
            ["created_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_background_jobs_status_updated_at",
            ["status", "updated_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("background_jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_background_jobs_status_updated_at")
        batch_op.drop_index("ix_background_jobs_created_at")
