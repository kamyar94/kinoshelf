#!/bin/sh
# Checks the setup, locks the secret file, builds and starts. Refuses to open a tunnel without
# Cloudflare Access settings. Run from the repository folder: sh scripts/start.sh
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "Copy .env.example to .env first."; exit 1; }
[ -f compose.media.yml ] || { echo "Copy compose.media.example.yml to compose.media.yml and list your folders."; exit 1; }
set -a; . ./.env; set +a
mkdir -p data
if echo "${COMPOSE_PROFILES:-}" | grep -q tunnel; then
  [ -f cloudflared.env ] && grep -q '^TUNNEL_TOKEN=..' cloudflared.env || { echo "Put the tunnel token in cloudflared.env first."; exit 1; }
  [ -n "${KS_CF_ACCESS_TEAM:-}" ] && [ -n "${KS_CF_ACCESS_AUD:-}" ] || {
    echo "The tunnel needs Cloudflare Access: set KS_CF_ACCESS_TEAM and KS_CF_ACCESS_AUD in .env."; exit 1; }
  chmod 600 cloudflared.env
fi
if echo "${COMPOSE_PROFILES:-}" | grep -q route; then
  [ -f route/config.json ] || { echo "The route sidecar needs route/config.json (copy route.example.json; see docs/NETWORK.md)."; exit 1; }
  chmod 700 route; chmod 600 route/config.json
fi
chmod 600 .env 2>/dev/null || true
# Build only when there is no image yet, or when asked (sh scripts/start.sh --build). With updates on, the updater
# keeps kinoshelf:local current; a build from an older checkout would go back in time until the next update.
if [ "${1:-}" = "--build" ] || ! docker image inspect kinoshelf:local >/dev/null 2>&1; then
  docker compose build
fi
docker compose up -d
sleep 3
if ! docker compose exec -T app python -c "import sqlite3;c=sqlite3.connect('/data/kinoshelf.sqlite');exit(0 if c.execute('select 1 from users limit 1').fetchone() else 1)" 2>/dev/null; then
  echo "No account yet. The setup code is in this log line:"
  docker compose logs app 2>/dev/null | grep -i "setup code" | tail -1
fi
sh scripts/status.sh
