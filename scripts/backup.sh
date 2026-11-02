#!/bin/sh
# Online backup of /data (database, settings with keys, journals) into data/backups as .tar.gz.
# Copy the archive somewhere safe; it holds your API keys.
cd "$(dirname "$0")/.."
docker compose exec -T app kinoshelf backup
