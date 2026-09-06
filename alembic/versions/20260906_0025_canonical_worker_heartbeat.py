"""Remove superseded per-container worker heartbeat rows."""

from alembic import op

revision = "20260906_0025"
down_revision = "20260906_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM runtime_health WHERE component LIKE 'worker:%'")


def downgrade() -> None:
    # Runtime heartbeats are ephemeral and are recreated by running workers.
    pass
