#!/usr/bin/env bash
set -euo pipefail
: "${DEPLOY_PATH:?}" "${DEPLOY_SERVICE:?}" "${IMAGE_REGISTRY:?}" "${IMAGE_NAMESPACE:?}" "${IMAGE_NAME:?}" "${IMAGE_TAG:?}" "${MUSE2API_ORIGIN:?}"
[[ "$IMAGE_TAG" =~ ^[0-9a-f]{40}$ ]]

repo_compose="$(pwd)/docker-compose.yml"
test -d "$DEPLOY_PATH/data"
test -f "$DEPLOY_PATH/.env"
python3 - "$DEPLOY_PATH/.env" <<'PY'
from pathlib import Path
import stat
import sys
path = Path(sys.argv[1])
if stat.S_IMODE(path.stat().st_mode) & 0o077:
    raise SystemExit('Private application .env must have mode 0600')
values = {}
for line in path.read_text(encoding='utf-8').splitlines():
    if line.strip() and not line.lstrip().startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('\"\'')
key = values.get('MUSE2API_KEY', '')
if not key or 'your_secret' in key or key.startswith('replace-'):
    raise SystemExit('Set a private MUSE2API_KEY before deployment')
password = values.get('MUSE2API_ADMIN_PASSWORD', '')
if not password or password.startswith('replace-'):
    raise SystemExit('Set a private MUSE2API_ADMIN_PASSWORD before deployment')
PY

release="$DEPLOY_PATH/.releases/$(date -u +%Y%m%dT%H%M%SZ)-${IMAGE_TAG:0:12}"
mkdir -p "$release"
chmod 700 "$DEPLOY_PATH/.releases" "$release"
if [ -f "$DEPLOY_PATH/docker-compose.yml" ]; then
  cp "$DEPLOY_PATH/docker-compose.yml" "$release/docker-compose.previous.yml"
fi
if docker inspect "$DEPLOY_SERVICE" >/dev/null 2>&1; then
  working_dir="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$DEPLOY_SERVICE")"
  if [ "$working_dir" != "$DEPLOY_PATH" ]; then
    echo 'Existing container belongs to a different deployment directory' >&2
    exit 1
  fi
  docker inspect -f '{{.Config.Image}}' "$DEPLOY_SERVICE" > "$release/previous-image"
fi
cp "$repo_compose" "$DEPLOY_PATH/docker-compose.yml"
cd "$DEPLOY_PATH"
docker compose config --quiet
docker network inspect my-shared-net >/dev/null 2>&1 || docker network create my-shared-net

# Image selection stays in this command environment, outside the application .env.
IMAGE_REGISTRY="$IMAGE_REGISTRY" IMAGE_NAMESPACE="$IMAGE_NAMESPACE" IMAGE_NAME="$IMAGE_NAME" IMAGE_TAG="$IMAGE_TAG" docker compose pull "$DEPLOY_SERVICE"

# JSON task records are atomically written by Store. Count every nonterminal state,
# and fail closed for invalid data rather than discarding accepted generations.
active_tasks() {
  python3 - "$DEPLOY_PATH/data/tasks.json" <<'PY'
import json
from pathlib import Path
import subprocess
import sys
path = Path(sys.argv[1])
try:
    payload = path.read_text(encoding='utf-8')
except FileNotFoundError:
    payload = '{}'
except PermissionError:
    # Store's atomic task files may be root-owned 0600. Read without changing them.
    try:
        payload = subprocess.run(['sudo', '-n', 'cat', str(path)],
                                 capture_output=True, text=True, encoding='utf-8',
                                 timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        raise SystemExit('Cannot safely read persisted task records') from None
try:
    tasks = json.loads(payload)
except (ValueError, UnicodeError):
    raise SystemExit('Cannot safely parse persisted task records') from None
if not isinstance(tasks, dict) or any(not isinstance(t, dict) for t in tasks.values()):
    raise SystemExit('Cannot safely inspect persisted task records')
terminal = {'completed', 'succeeded', 'success', 'done', 'failed', 'error',
            'cancelled', 'canceled', 'expired', 'rejected'}
print(sum(str(t.get('status', '')).lower() not in terminal for t in tasks.values()))
PY
}
deadline=$((SECONDS + 1200))
while true; do
  active="$(active_tasks)"
  echo "MUSE2API persisted tasks in progress: $active"
  if [ "$active" -eq 0 ]; then
    sleep 3
    if [ "$(active_tasks)" -eq 0 ]; then break; fi
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo 'Refusing to interrupt persisted tasks' >&2
    exit 1
  fi
  sleep 10
done

IMAGE_REGISTRY="$IMAGE_REGISTRY" IMAGE_NAMESPACE="$IMAGE_NAMESPACE" IMAGE_NAME="$IMAGE_NAME" IMAGE_TAG="$IMAGE_TAG" docker compose up -d --no-deps --no-build --pull never "$DEPLOY_SERVICE"
revision="$(docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$DEPLOY_SERVICE")"
test "$revision" = "$IMAGE_TAG"
for attempt in $(seq 1 30); do
  if curl -fsS --max-time 5 "$MUSE2API_ORIGIN/healthz" >/dev/null; then
    status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$MUSE2API_ORIGIN/v1/models")"
    if [ "$status" = 401 ]; then
      printf 'Deployed MUSE2API revision %s\n' "$revision"
      exit 0
    fi
  fi
  sleep 5
done
echo "MUSE2API health/authentication check failed; previous image metadata retained in $release" >&2
exit 1
