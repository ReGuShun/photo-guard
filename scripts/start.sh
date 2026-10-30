#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
export WEBAPP_PORT="${WEBAPP_PORT:-6006}"
mkdir -p web_frontend_runs
echo "Starting on port $WEBAPP_PORT; logs: web_frontend_runs/frontend.log"
"${FRONTEND_PYTHON:-python3}" -u app.py 2>&1 | tee -a web_frontend_runs/frontend.log
