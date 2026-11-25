# Deminic one-click runner for Windows. Starts the infrastructure containers,
# then the app (native or Docker - whatever install.bat chose), opens the browser.
param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$Host.UI.RawUI.WindowTitle = "Deminic Agentic AI"

if (-not (Test-Path .env)) { Write-Host "Not installed yet - running install.bat first..." -ForegroundColor Yellow; & (Join-Path $PSScriptRoot 'install.ps1'); }
$mode = if (Test-Path data\.mode) { (Get-Content data\.mode -Raw).Trim() } else { 'native' }

$up = $false
for ($i = 0; $i -lt 60; $i++) {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) { $up = $true; break }
    if ($i -eq 0) { $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"; if (Test-Path $dd) { Write-Host "Starting Docker Desktop..."; Start-Process $dd } }
    Start-Sleep -Seconds 3
}
if (-not $up) { Write-Host "Docker is not running - start Docker Desktop and try again." -ForegroundColor Red; Read-Host "Press Enter"; exit 1 }

Write-Host "Starting PostgreSQL, Redis, ClamAV and Ollama..." -ForegroundColor Cyan
docker compose up -d db redis clamav ollama | Out-Null
for ($i = 0; $i -lt 60; $i++) {
    $db = docker inspect -f '{{.State.Health.Status}}' deminic-db-1 2>$null
    $rd = docker inspect -f '{{.State.Health.Status}}' deminic-redis-1 2>$null
    if ($db -eq 'healthy' -and $rd -eq 'healthy') { break }
    Start-Sleep -Seconds 2
}

if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        for ($i = 0; $i -lt 60; $i++) {
            try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:8700/healthz"; if ($r.StatusCode -eq 200) { Start-Process "http://127.0.0.1:8700"; break } } catch { }
            Start-Sleep -Seconds 1
        }
    } | Out-Null
}

if ($mode -eq 'docker') {
    docker compose --profile full up -d app
    Write-Host "Deminic is running in Docker. Logs: docker compose logs -f app   Stop: docker compose --profile full down" -ForegroundColor Green
    docker compose logs -f app
} else {
    $py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $py)) { Write-Host "Python environment missing - run install.bat" -ForegroundColor Red; Read-Host "Press Enter"; exit 1 }
    & $py run.py
}
