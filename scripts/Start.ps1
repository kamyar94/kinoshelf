# Docker Desktop on Windows: checks the setup, locks the secret file, builds and starts.
# Refuses to open a tunnel without Cloudflare Access settings.
#   powershell -ExecutionPolicy Bypass -File .\scripts\Start.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path .env)) { throw 'Copy .env.example to .env first.' }
if (-not (Test-Path compose.media.yml)) { throw 'Copy compose.media.example.yml to compose.media.yml and list your folders.' }
$envVars = @{}
Get-Content .env | Where-Object { $_ -match '^\s*([A-Z_]+)=(.*)$' } | ForEach-Object { $envVars[$Matches[1]] = $Matches[2].Trim() }
New-Item -ItemType Directory -Force data | Out-Null
if (($envVars['COMPOSE_PROFILES'] + '') -match 'tunnel') {
    if (-not (Test-Path cloudflared.env) -or -not (Select-String -Path cloudflared.env -Pattern '^TUNNEL_TOKEN=..' -Quiet)) {
        throw 'Put the tunnel token in cloudflared.env first.'
    }
    if (-not $envVars['KS_CF_ACCESS_TEAM'] -or -not $envVars['KS_CF_ACCESS_AUD']) {
        throw 'The tunnel needs Cloudflare Access: set KS_CF_ACCESS_TEAM and KS_CF_ACCESS_AUD in .env.'
    }
    # Only the current user (and SYSTEM) may read the token file.
    icacls cloudflared.env /inheritance:r /grant:r "$($env:USERNAME):(R,W)" /grant:r 'SYSTEM:(F)' | Out-Null
}
docker compose build
docker compose up -d
Start-Sleep -Seconds 3
$log = docker compose logs app 2>$null | Select-String 'setup code' | Select-Object -Last 1
if ($log) { Write-Host "If you have no account yet, the setup code is in this line:`n$log" }
& "$PSScriptRoot\Status.ps1"
