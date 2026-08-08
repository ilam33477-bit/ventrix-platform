#!/bin/sh
set -eu

if [ "${1:-}" = "api" ]; then
  alembic upgrade head
  exec uvicorn services.backend.api.app:app --host 0.0.0.0 --port 8000
fi

if [ "${1:-}" = "bot" ]; then
  exec python -m services.backend.bot.main
fi

if [ "${1:-}" = "worker" ]; then
  exec python -m services.backend.jobs.worker
fi

if [ "${1:-}" = "scheduler" ]; then
  exec python -m services.backend.scheduler.main
fi

if [ "${1:-}" = "client-bots" ]; then
  exec python -m services.backend.client_bots.main
fi

exec "$@"
