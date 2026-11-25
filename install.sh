#!/usr/bin/env bash
# Deminic one-click installer for macOS and Linux.
#   ./install.sh           -> native app (uv + Python 3.12) + Docker infrastructure
#   ./install.sh --docker  -> everything (app included) in Docker containers
#   ./install.sh --skip-models  -> skip the fallback-model download
set -euo pipefail
cd "$(dirname "$0")"

MODE=native; SKIP_MODELS=0
for a in "$@"; do
  case "$a" in
    --docker) MODE=docker ;;
    --skip-models) SKIP_MODELS=1 ;;
  esac
done

c_cyan=$'\033[36m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'; c_red=$'\033[31m'; c_off=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$c_cyan" "$1" "$c_off"; }
ok()   { printf '    %s%s%s\n' "$c_green" "$1" "$c_off"; }
warn() { printf '    %s%s%s\n' "$c_yellow" "$1" "$c_off"; }
fail() { printf '\n%sERROR: %s%s\n' "$c_red" "$1" "$c_off"; exit 1; }

echo "=================================================================="
echo "  Deminic Agentic AI - installer ($MODE mode)"
echo "=================================================================="

step "Checking Docker"
command -v docker >/dev/null 2>&1 || fail "Docker is required: https://docs.docker.com/get-docker/ (Docker Desktop on macOS)"
if ! docker info >/dev/null 2>&1; then
  if [[ "$(uname)" == "Darwin" ]]; then warn "Starting Docker Desktop..."; open -a Docker || true; fi
  for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 3; done
fi
docker info >/dev/null 2>&1 || fail "Docker is not running - start it and run ./install.sh again"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required (docker compose ...)"
ok "Docker is running"

lan_ips() {
  if [[ "$(uname)" == "Darwin" ]]; then
    ifconfig | awk '/inet / && $2 !~ /^127\./ && $2 !~ /^169\.254\./ {print $2}'
  else
    (hostname -I 2>/dev/null || ip -4 -o addr show | awk '{print $4}' | cut -d/ -f1) | tr ' ' '\n' | grep -Ev '^(127\.|169\.254\.|172\.1[7-9]\.|$)' || true
  fi
}

rand_hex()   { openssl rand -hex "$1" 2>/dev/null || head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
rand_b64u()  { (openssl rand -base64 "$1" 2>/dev/null || head -c "$1" /dev/urandom | base64) | tr '+/' '-_' | tr -d '\n'; }
rand_alnum() { LC_ALL=C tr -dc 'A-HJ-NP-Za-km-z2-9' </dev/urandom | head -c "$1"; }

step "Creating configuration (.env)"
# bash 3.2 compatible (macOS): no associative arrays.
existing_value() { [[ -f .env ]] && grep -E "^$1=" .env | head -n 1 | cut -d= -f2- || true; }
tmp=$(mktemp)
DBPW=""; RPW=""
while IFS= read -r line || [[ -n "$line" ]]; do
  if [[ "$line" =~ ^([A-Z0-9_]+)=(.*)$ ]]; then
    k="${BASH_REMATCH[1]}"; v="${BASH_REMATCH[2]}"
    ex=$(existing_value "$k")
    if [[ -n "$ex" && "$ex" != "__GENERATE__" ]]; then v="$ex"
    elif [[ "$v" == "__GENERATE__" ]]; then
      case "$k" in SECRET_KEY) v=$(rand_hex 32) ;; ENCRYPTION_KEY) v=$(rand_b64u 32) ;; *) v=$(rand_alnum 32) ;; esac
    fi
    [[ "$k" == DB_PASSWORD ]] && DBPW="$v"
    [[ "$k" == REDIS_PASSWORD ]] && RPW="$v"
    echo "$k=$v" >> "$tmp"
  else
    echo "$line" >> "$tmp"
  fi
done < .env.example
out=$(cat "$tmp"); rm -f "$tmp"
out="${out//\$\{DB_PASSWORD\}/$DBPW}"
out="${out//\$\{REDIS_PASSWORD\}/$RPW}"
LAN=$(lan_ips | paste -sd, -)
{ echo "$out" | grep -v '^EXTRA_CERT_HOSTS=' | grep -v '^CLAMAV_IMAGE='; echo "EXTRA_CERT_HOSTS=$LAN"
  case "$(uname -m)" in arm64|aarch64) echo "CLAMAV_IMAGE=clamav/clamav-debian:1.4" ;; esac; } > .env
chmod 600 .env
mkdir -p data && echo "$MODE" > data/.mode
ok ".env ready (existing secrets kept). LAN IPs: ${LAN:-none}"

if [[ "$MODE" == native ]]; then
  step "Installing uv + Python 3.12 + dependencies"
  if ! command -v uv >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then brew install uv || true; fi
    if ! command -v uv >/dev/null 2>&1; then curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; fi
  fi
  command -v uv >/dev/null 2>&1 || fail "Could not install uv - see https://docs.astral.sh/uv/"
  uv sync --python 3.12
  ok "Python environment ready (.venv)"
fi

step "Starting PostgreSQL (pgvector), Redis, ClamAV and Ollama"
docker compose up -d db redis clamav ollama
for _ in $(seq 1 60); do
  db=$(docker inspect -f '{{.State.Health.Status}}' deminic-db-1 2>/dev/null || true)
  rd=$(docker inspect -f '{{.State.Health.Status}}' deminic-redis-1 2>/dev/null || true)
  [[ "$db" == healthy && "$rd" == healthy ]] && break; sleep 2
done
ok "database: ${db:-?}, redis: ${rd:-?}"

if [[ "$SKIP_MODELS" == 0 ]]; then
  step "Downloading the embedding model + local fallback LLMs (gemma / qwen-coder)"
  if docker compose up ollama-pull; then ok "Fallback models ready"; else warn "Model download failed - re-run ./install.sh later"; fi
  [[ "$(uname)" == "Darwin" ]] && warn "Tip: on macOS, Ollama inside Docker runs on CPU. For speed you can run the native Ollama app and set OLLAMA_URL=http://127.0.0.1:11434/v1 in .env"
fi

if [[ "$MODE" == docker ]]; then
  step "Building the app container"
  docker compose --profile full build app
  ok "App image built"
fi

if command -v ufw >/dev/null 2>&1 && sudo -n true 2>/dev/null && sudo ufw status | grep -q active; then
  step "Opening firewall ports 8700/8743 for LAN devices"
  sudo ufw allow 8700/tcp >/dev/null && sudo ufw allow 8743/tcp >/dev/null && ok "ufw rules added"
fi

chmod +x run.sh 2>/dev/null || true
echo
echo "${c_green}==================================================================${c_off}"
echo "${c_green}  Installed! Start it with: ./run.sh${c_off}"
echo "${c_green}  This device : http://127.0.0.1:8700${c_off}"
for ip in $(lan_ips); do echo "${c_green}  LAN         : https://$ip:8743  (install /ca.crt once per device)${c_off}"; done
echo "${c_green}==================================================================${c_off}"
