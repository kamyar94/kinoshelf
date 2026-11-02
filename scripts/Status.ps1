# Container state, the app's answer, published ports and the last tunnel log lines.
Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose ps
docker compose exec -T app python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8484/api/health',timeout=5).read().decode())"
Write-Host 'Published ports:'
docker compose port app 8484
if ((docker compose ps --services --status running) -contains 'tunnel') {
    Write-Host 'Tunnel log:'
    docker compose logs --tail 5 tunnel
}
