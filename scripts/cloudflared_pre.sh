#!/usr/bin/env bash
set -euo pipefail
: "${CLOUDFLARED_CONFIG:?}" "${MUSE2API_HOSTNAME:?}" "${MUSE2API_ORIGIN:?}"
sudo test -f "$CLOUDFLARED_CONFIG"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
chmod 600 "$tmp"
sudo cat "$CLOUDFLARED_CONFIG" > "$tmp"
CLOUDFLARED_TMP="$tmp" python3 - <<'PY'
import os
from pathlib import Path
path = Path(os.environ['CLOUDFLARED_TMP'])
lines = path.read_text(encoding='utf-8').splitlines()
hostname, origin = os.environ['MUSE2API_HOSTNAME'], os.environ['MUSE2API_ORIGIN']
if not any(line.strip() == 'ingress:' for line in lines):
    raise SystemExit('cloudflared ingress is missing')
fallback = next((i for i, line in enumerate(lines)
                 if line.strip() == '- service: http_status:404'), None)
if fallback is None:
    raise SystemExit('cloudflared final 404 route is missing')
indent = lines[fallback][:len(lines[fallback]) - len(lines[fallback].lstrip())]
route = '- hostname: ' + hostname
start = next((i for i, line in enumerate(lines) if line.strip() == route), None)
if start is None:
    lines[fallback:fallback] = [indent + route, indent + '  service: ' + origin]
else:
    stop = next((i for i in range(start + 1, len(lines))
                 if lines[i].strip().startswith('- ')), len(lines))
    service = next((i for i in range(start + 1, stop)
                    if lines[i].strip().startswith('service:')), None)
    if service is None:
        lines.insert(start + 1, indent + '  service: ' + origin)
    else:
        lines[service] = indent + '  service: ' + origin
path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
PY

# Preserve the existing tunnel, protocol/edge relay settings, and other routes.
sudo cloudflared tunnel --config "$tmp" ingress validate
config_changed=0
backup="${CLOUDFLARED_CONFIG}.bak.$(date +%Y%m%d%H%M%S)"
if ! cmp -s "$tmp" <(sudo cat "$CLOUDFLARED_CONFIG"); then
  sudo cp "$CLOUDFLARED_CONFIG" "$backup"
  sudo install -m 600 -o root -g root "$tmp" "$CLOUDFLARED_CONFIG"
  config_changed=1
fi
tunnel_id="$(sudo awk '/^tunnel:/ {print $2; exit}' "$CLOUDFLARED_CONFIG")"
test -n "$tunnel_id"
origin_cert="${HOME}/.cloudflared/cert.pem"
route_dns() {
  if [ -f "$origin_cert" ]; then
    cloudflared --origincert "$origin_cert" tunnel route dns "$tunnel_id" "$MUSE2API_HOSTNAME"
  else
    sudo cloudflared tunnel route dns "$tunnel_id" "$MUSE2API_HOSTNAME"
  fi
}
if ! route_dns; then
  if [ "$config_changed" -eq 1 ]; then
    sudo cp "$backup" "$CLOUDFLARED_CONFIG"
  fi
  echo 'Cloudflared DNS routing failed; connector was not restarted' >&2
  exit 1
fi
if [ "$config_changed" -eq 1 ]; then
  sudo systemctl restart cloudflared
  sudo systemctl is-active --quiet cloudflared
else
  echo 'Cloudflared ingress unchanged; existing connector was not restarted'
fi
