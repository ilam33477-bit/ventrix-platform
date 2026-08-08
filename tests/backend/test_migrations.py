from __future__ import annotations

import sqlite3

from alembic.config import Config

from alembic import command


def test_initial_migration_upgrade_downgrade_and_reupgrade(tmp_path, monkeypatch) -> None:
    database = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    expected = {
        "platform_owner",
        "tenants",
        "tenant_settings",
        "tenant_ai_profiles",
        "encrypted_secrets",
        "bot_instances",
        "audit_logs",
        "fsm_states",
        "background_jobs",
        "product_events",
        "telegram_connections",
        "telegram_folders",
        "telegram_dialogs",
        "initial_analysis_runs",
        "telegram_sync_cursors",
        "telegram_messages",
        "operational_problems",
    }
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert expected <= tables
        assert connection.execute("PRAGMA foreign_key_list(bot_instances)").fetchall()
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(background_jobs)")}
        assert "ix_background_jobs_available" in indexes
        bot_columns = {row[1] for row in connection.execute("PRAGMA table_info(bot_instances)")}
        assert {"runtime_status", "runtime_generation", "processed_updates"} <= bot_columns
    command.downgrade(config, "base")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not expected & tables
    command.upgrade(config, "head")
