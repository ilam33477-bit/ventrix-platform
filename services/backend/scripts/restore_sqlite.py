from __future__ import annotations

import argparse
from pathlib import Path

from ..config import get_settings
from .backup_sqlite import database_path_from_url, restore_database


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore SQLite while backend, owner bot and worker are stopped"
    )
    parser.add_argument("backup", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--confirm-stopped", action="store_true", required=True)
    args = parser.parse_args()
    destination = args.destination or database_path_from_url(get_settings().database_url)
    print(restore_database(args.backup, destination))


if __name__ == "__main__":
    main()
