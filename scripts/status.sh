#!/bin/sh
# Container state, the app's answer, published ports and the last tunnel log lines.
cd "$(dirname "$0")/.."
docker compose ps
echo
docker compose exec -T app python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8484/api/health',timeout=5).read().decode())" \
  || echo "The app did not answer."
echo
echo "Published ports:"; docker compose port app 8484 2>/dev/null || echo "  none (tunnel or SSH only)"
if docker compose ps --services --status running | grep -q '^tunnel$'; then
  echo; echo "Tunnel log:"; docker compose logs --tail 5 tunnel
fi
