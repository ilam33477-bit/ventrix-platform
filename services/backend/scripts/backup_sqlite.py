from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from ..config import get_settings


def database_path_from_url(database_url: str) -> Path:
    url = make_url(database_url)
    if url.drivername != "sqlite+aiosqlite" or not url.database:
        raise ValueError("backup supports only sqlite+aiosqlite file databases")
    path = Path(url.database).expanduser()
    return (Path.cwd() / path).resolve() if not path.is_absolute() else path.resolve()


def backup_database(source_path: Path, backup_directory: Path) -> Path:
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {source_path}")
    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_directory / f"app-{timestamp}.db"
    with sqlite3.connect(source_path) as source, sqlite3.connect(destination) as target:
        source.execute("PRAGMA busy_timeout=5000")
        source.backup(target, pages=256, sleep=0.05)
        integrity = target.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError("SQLite backup failed integrity_check")
    destination.chmod(0o600)
    return destination


def restore_database(backup_path: Path, destination_path: Path) -> Path:
    backup_path = backup_path.resolve()
    destination_path = destination_path.resolve()
    if not backup_path.is_file():
        raise FileNotFoundError(f"Backup does not exist: {backup_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(backup_path) as source, sqlite3.connect(destination_path) as target:
        integrity = source.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError("Refusing to restore a corrupt SQLite backup")
        source.backup(target, pages=256, sleep=0.05)
    return destination_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a consistent SQLite backup")
    parser.add_argument("--database", type=Path, help="Override source database path")
    parser.add_argument("--output", type=Path, default=Path("./backups"))
    args = parser.parse_args()
    source = args.database or database_path_from_url(get_settings().database_url)
    print(backup_database(source, args.output))


if __name__ == "__main__":
    main()
