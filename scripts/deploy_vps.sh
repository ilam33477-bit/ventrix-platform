#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

revision=${1:-$(git rev-parse --short=12 HEAD)}
case "$revision" in
  *[!A-Za-z0-9._-]*|'')
    echo "Invalid release revision" >&2
    exit 2
    ;;
esac

compose_file="infra/docker-compose.vps.yml"
release_dir=".release"
current_file="$release_dir/current.env"
previous_file="$release_dir/previous.env"
candidate_file="$release_dir/candidate.env"
image="ventrix-app:$revision"
mkdir -p "$release_dir"
chmod 700 "$release_dir"

if [ -f "$current_file" ]; then
  cp "$current_file" "$previous_file"
else
  backend_container=$(docker compose -f "$compose_file" ps -q backend 2>/dev/null || true)
  if [ -n "$backend_container" ]; then
    previous_image=$(docker inspect --format '{{.Config.Image}}' "$backend_container")
    printf 'VENTRIX_IMAGE=%s\nRELEASE_REVISION=previous\n' "$previous_image" >"$previous_file"
  fi
fi

[ ! -f "$previous_file" ] || chmod 600 "$previous_file"
printf 'VENTRIX_IMAGE=%s\nRELEASE_REVISION=%s\n' "$image" "$revision" >"$candidate_file"
chmod 600 "$candidate_file"

docker compose --env-file "$candidate_file" -f "$compose_file" config --quiet
docker build --label "org.opencontainers.image.revision=$revision" -t "$image" .

if [ -f "data/app.db" ]; then
  docker run --rm --entrypoint python \
    -v "$project_root/data:/app/data" \
    -v "$project_root/backups:/app/backups" \
    "$image" -m services.backend.scripts.backup_sqlite \
    --database /app/data/app.db --output /app/backups
fi

cp "$candidate_file" "$current_file"
chmod 600 "$current_file"
docker compose --env-file "$current_file" -f "$compose_file" up -d --remove-orphans

attempt=0
until curl --fail --silent --show-error http://127.0.0.1:8010/health/ready >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    docker compose --env-file "$current_file" -f "$compose_file" ps
    echo "Ventrix readiness check failed" >&2
    exit 1
  fi
  sleep 2
done

expected_services=6
running_services=$(docker compose --env-file "$current_file" -f "$compose_file" ps \
  --status running --services | wc -l | tr -d ' ')
if [ "$running_services" -ne "$expected_services" ]; then
  docker compose --env-file "$current_file" -f "$compose_file" ps
  echo "Only $running_services of $expected_services Ventrix services are running" >&2
  exit 1
fi

printf 'Ventrix release %s is ready; image %s; services %s/%s.\n' \
  "$revision" "$image" "$running_services" "$expected_services"
rm -f "$candidate_file"
