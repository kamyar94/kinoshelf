# Online backup of /data (database, settings with keys, journals) into data\backups as .tar.gz.
# Copy the archive somewhere safe; it holds your API keys.
Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose exec -T app kinoshelf backup
