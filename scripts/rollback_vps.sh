#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

compose_file="infra/docker-compose.vps.yml"
release_dir=".release"
current_file="$release_dir/current.env"
previous_file="$release_dir/previous.env"

if [ ! -f "$previous_file" ]; then
  echo "No previous Ventrix release metadata is available" >&2
  exit 2
fi

failed_file="$release_dir/failed-$(date -u +%Y%m%dT%H%M%SZ).env"
if [ -f "$current_file" ]; then
  cp "$current_file" "$failed_file"
  chmod 600 "$failed_file"
fi
cp "$previous_file" "$current_file"
chmod 600 "$current_file"

docker compose --env-file "$current_file" -f "$compose_file" config --quiet
docker compose --env-file "$current_file" -f "$compose_file" up -d --remove-orphans

attempt=0
until curl --fail --silent --show-error http://127.0.0.1:8010/health/ready >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    docker compose --env-file "$current_file" -f "$compose_file" ps
    echo "Ventrix rollback readiness check failed" >&2
    exit 1
  fi
  sleep 2
done

echo "Ventrix application image rollback completed. Database restore was not performed."
