# Deminic one-click installer for Windows (PowerShell 5.1+).
#   install.bat            -> native app (uv + Python 3.12) + Docker infrastructure
#   install.bat -Docker    -> everything (app included) in Docker containers
param([switch]$Docker, [switch]$SkipModels, [switch]$NoFirewall, [switch]$NoShortcut)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "    $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host ""; Write-Host "ERROR: $msg" -ForegroundColor Red; Read-Host "Press Enter to close"; exit 1 }

Write-Host "==================================================================" -ForegroundColor Magenta
Write-Host "  Deminic Agentic AI - installer ($(if ($Docker) {'Docker mode'} else {'native mode'}))" -ForegroundColor Magenta
Write-Host "==================================================================" -ForegroundColor Magenta

# ---------------------------------------------------------------- Docker
Step "Checking Docker"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Start-Process "https://www.docker.com/products/docker-desktop/"
    Fail "Docker Desktop is required. Install it (the download page was opened), start it, then run install.bat again."
}
$dockerUp = $false
for ($i = 0; $i -lt 60; $i++) {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) { $dockerUp = $true; break }
    if ($i -eq 0) {
        $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
        if (Test-Path $dd) { Warn "Starting Docker Desktop..."; Start-Process $dd }
    }
    Start-Sleep -Seconds 3
}
if (-not $dockerUp) { Fail "Docker is not running. Start Docker Desktop and run install.bat again." }
Ok "Docker is running"

# ---------------------------------------------------------------- .env with fresh secrets
function New-RandomBytes([int]$n) { $b = New-Object byte[] $n; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); return ,$b }
function New-Hex([int]$n) { return ((New-RandomBytes $n) | ForEach-Object { $_.ToString('x2') }) -join '' }
function New-UrlSafe([int]$n) { return [Convert]::ToBase64String((New-RandomBytes $n)).Replace('+', '-').Replace('/', '_') }
function New-Alnum([int]$n) { $chars = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'; $b = New-RandomBytes $n; return (($b | ForEach-Object { $chars[$_ % $chars.Length] }) -join '') }

function Get-LanIps {
    try {
        return @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object {
            $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.InterfaceAlias -notmatch 'vEthernet|Loopback|WSL|Docker'
        } | Select-Object -ExpandProperty IPAddress)
    } catch { return @() }
}

Step "Creating configuration (.env)"
$existing = @{}
if (Test-Path .env) {
    foreach ($line in Get-Content .env) {
        if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $existing[$Matches[1]] = $Matches[2] }
    }
}
$values = [ordered]@{}
$out = New-Object System.Collections.Generic.List[string]
foreach ($line in Get-Content .env.example) {
    if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') {
        $k = $Matches[1]; $v = $Matches[2].Trim()
        if ($existing.ContainsKey($k) -and $existing[$k] -and $existing[$k] -ne '__GENERATE__') { $v = $existing[$k] }
        elseif ($v -eq '__GENERATE__') {
            switch ($k) {
                'SECRET_KEY' { $v = New-Hex 32 }
                'ENCRYPTION_KEY' { $v = New-UrlSafe 32 }
                default { $v = New-Alnum 32 }
            }
        }
        $values[$k] = $v
        $out.Add("$k=$v")
    } else { $out.Add($line) }
}
$final = foreach ($l in $out) {
    $r = $l
    foreach ($k in $values.Keys) { $r = $r.Replace('${' + $k + '}', $values[$k]) }
    $r
}
$lan = (Get-LanIps) -join ','
$final = @($final | Where-Object { $_ -notmatch '^EXTRA_CERT_HOSTS=' }) + "EXTRA_CERT_HOSTS=$lan"
if ([Environment]::Is64BitOperatingSystem -and $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { $final += "CLAMAV_IMAGE=clamav/clamav-debian:1.4" }
[IO.File]::WriteAllLines((Join-Path $PSScriptRoot '.env'), [string[]]$final, (New-Object Text.UTF8Encoding($false)))
Ok ".env ready (existing secrets kept, new ones generated). LAN IPs: $lan"
New-Item -ItemType Directory -Force -Path data | Out-Null
Set-Content -Path data\.mode -Value $(if ($Docker) { 'docker' } else { 'native' }) -Encoding ascii

# ---------------------------------------------------------------- Python via uv (native mode)
if (-not $Docker) {
    Step "Installing uv + Python 3.12 + dependencies"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        $uvDir = Join-Path $env:LOCALAPPDATA "Programs\uv"
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install --id=astral-sh.uv -e --accept-source-agreements --accept-package-agreements --silent | Out-Null
        }
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            New-Item -ItemType Directory -Force -Path $uvDir | Out-Null
            $zip = Join-Path $env:TEMP "uv.zip"
            Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
            Expand-Archive -Force -Path $zip -DestinationPath $uvDir
            $env:Path = "$uvDir;$env:Path"
        }
        $env:Path = "$env:USERPROFILE\.local\bin;$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:Path"
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail "Could not install uv. Install it from https://docs.astral.sh/uv/ and re-run." }
    uv sync --python 3.12
    if ($LASTEXITCODE -ne 0) { Fail "Dependency installation failed (uv sync)." }
    Ok "Python environment ready (.venv)"
}

# ---------------------------------------------------------------- infrastructure
Step "Starting PostgreSQL (pgvector), Redis, ClamAV and Ollama"
docker compose up -d db redis clamav ollama
if ($LASTEXITCODE -ne 0) { Fail "docker compose failed" }
for ($i = 0; $i -lt 60; $i++) {
    $db = docker inspect -f '{{.State.Health.Status}}' deminic-db-1 2>$null
    $rd = docker inspect -f '{{.State.Health.Status}}' deminic-redis-1 2>$null
    if ($db -eq 'healthy' -and $rd -eq 'healthy') { break }
    Start-Sleep -Seconds 2
}
Ok "database: $db, redis: $rd (ClamAV loads its signatures in the background)"

if (-not $SkipModels) {
    Step "Downloading the embedding model + local fallback LLMs (gemma / qwen-coder) - this can take a while"
    docker compose up ollama-pull
    if ($LASTEXITCODE -ne 0) { Warn "Model download failed - re-run install.bat later (the app still works with your gateway)." } else { Ok "Fallback models ready" }
}

if ($Docker) {
    Step "Building the app container"
    docker compose --profile full build app
    if ($LASTEXITCODE -ne 0) { Fail "Image build failed" }
    Ok "App image built"
}

# ---------------------------------------------------------------- firewall (LAN access)
Step "Allowing LAN access through Windows Firewall (ports 8700 HTTP, 8743 HTTPS)"
$rule = Get-NetFirewallRule -DisplayName "Deminic Agentic AI" -ErrorAction SilentlyContinue
if ($NoFirewall) { Warn "Skipped (-NoFirewall). Other devices need ports 8700/8743 allowed to connect." }
elseif (-not $rule) {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    # Any network profile (home Wi-Fi is often classified "Public") but ONLY devices on the local subnet.
    $cmd = "New-NetFirewallRule -DisplayName 'Deminic Agentic AI' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8700,8743 -Profile Any -RemoteAddress LocalSubnet | Out-Null"
    try {
        if ($isAdmin) { Invoke-Expression $cmd } else { Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile -Command $cmd" }
        Ok "Firewall rule added (local-subnet devices only)"
    } catch { Warn "Could not add the firewall rule (declined?). Other devices may not reach the app until you allow ports 8700/8743." }
} else { Ok "Firewall rule already present" }

# ---------------------------------------------------------------- shortcut
Step "Creating a desktop shortcut"
if ($NoShortcut) { Warn "Skipped (-NoShortcut)" } else {
try {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Deminic.lnk'))
    $lnk.TargetPath = Join-Path $PSScriptRoot 'run.bat'
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.Description = 'Start Deminic Agentic AI'
    $lnk.Save()
    Ok "Desktop shortcut 'Deminic' created"
} catch { Warn "Shortcut not created: $($_.Exception.Message)" }
}

Write-Host ""
Write-Host "==================================================================" -ForegroundColor Green
Write-Host "  Installed! Double-click run.bat (or the Deminic desktop icon)." -ForegroundColor Green
Write-Host "  This device : http://127.0.0.1:8700" -ForegroundColor Green
foreach ($ip in (Get-LanIps)) { Write-Host "  LAN         : https://${ip}:8743  (install /ca.crt once per device)" -ForegroundColor Green }
Write-Host "==================================================================" -ForegroundColor Green
