import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_group_approval_migration_requires_reapproval_and_preserves_settings(tmp_path):
    database = tmp_path / "migration.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}"}
    root = Path(__file__).resolve().parents[2]

    def migrate(*args):
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    migrate("upgrade", "20260824_0022")
    # Synthetic legacy row: migration concerns approval/status, not tenant fixture setup.
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO group_integrations (id,tenant_id,title,telegram_chat_id,status,minimum_criticality) VALUES ('legacy','test','TEST',-10055,'active',93)"
        )
    migrate("upgrade", "head")
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT status,minimum_criticality,approved_at,approved_by_telegram_user_id FROM group_integrations WHERE id='legacy'"
        ).fetchone()
        assert row == ("pending", 93, None, None)
    migrate("downgrade", "20260824_0022")
    migrate("upgrade", "head")
    with sqlite3.connect(database) as db:
        assert (
            db.execute("SELECT status FROM group_integrations WHERE id='legacy'").fetchone()[0]
            == "pending"
        )
