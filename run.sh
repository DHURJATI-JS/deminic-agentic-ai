#!/usr/bin/env bash
# Deminic one-click runner for macOS and Linux.
set -euo pipefail
cd "$(dirname "$0")"
[[ -f .env ]] || ./install.sh
MODE=$(cat data/.mode 2>/dev/null || echo native)

if ! docker info >/dev/null 2>&1; then
  [[ "$(uname)" == "Darwin" ]] && open -a Docker || true
  for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 3; done
fi
docker info >/dev/null 2>&1 || { echo "Docker is not running"; exit 1; }

echo "Starting PostgreSQL, Redis, ClamAV and Ollama..."
docker compose up -d db redis clamav ollama >/dev/null
for _ in $(seq 1 60); do
  db=$(docker inspect -f '{{.State.Health.Status}}' deminic-db-1 2>/dev/null || true)
  rd=$(docker inspect -f '{{.State.Health.Status}}' deminic-redis-1 2>/dev/null || true)
  [[ "$db" == healthy && "$rd" == healthy ]] && break; sleep 2
done

open_browser() {
  for _ in $(seq 1 60); do
    if curl -fs http://127.0.0.1:8700/healthz >/dev/null 2>&1; then
      if command -v open >/dev/null 2>&1; then open http://127.0.0.1:8700; elif command -v xdg-open >/dev/null 2>&1; then xdg-open http://127.0.0.1:8700 >/dev/null 2>&1; fi
      return
    fi
    sleep 1
  done
}
[[ "${1:-}" == "--no-browser" ]] || open_browser &

if [[ "$MODE" == docker ]]; then
  docker compose --profile full up -d app
  echo "Deminic is running in Docker. Stop with: docker compose --profile full down"
  exec docker compose logs -f app
else
  [[ -x .venv/bin/python ]] || { echo "Python environment missing - run ./install.sh"; exit 1; }
  exec .venv/bin/python run.py
fi
