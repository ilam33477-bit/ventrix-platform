from __future__ import annotations

import argparse
import asyncio

from ..database import get_session_factory
from .queue import SQLiteJobQueue


async def enqueue_test(job_type: str) -> None:
    queue = SQLiteJobQueue(get_session_factory())
    job_id = await queue.enqueue(job_type, {"source": "cli"})
    print(job_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local SQLite background jobs")
    parser.add_argument("command", choices=["enqueue-test"])
    parser.add_argument(
        "--type", default="system.echo", choices=["system.echo", "system.fail_once"]
    )
    args = parser.parse_args()
    if args.command == "enqueue-test":
        asyncio.run(enqueue_test(args.type))


if __name__ == "__main__":
    main()
