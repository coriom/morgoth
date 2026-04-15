#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROCESS_NAME="${PM2_PROCESS_NAME:-morgoth}"
RESTART_DELAY_MS="${PM2_RESTART_DELAY_MS:-5000}"
MAX_MEMORY="${PM2_MAX_MEMORY:-1G}"
LOG_DIR="${APP_DIR}/data/logs"

if ! command -v pm2 >/dev/null 2>&1; then
  echo "pm2 is required but was not found in PATH" >&2
  exit 1
fi

if [[ -x "${APP_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${APP_DIR}/.venv/bin/python"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

mkdir -p "${LOG_DIR}"

if pm2 describe "${PROCESS_NAME}" >/dev/null 2>&1; then
  pm2 restart "${PROCESS_NAME}" --update-env
else
  pm2 start "${APP_DIR}/main.py" \
    --name "${PROCESS_NAME}" \
    --cwd "${APP_DIR}" \
    --interpreter "${PYTHON_BIN}" \
    --time \
    --restart-delay "${RESTART_DELAY_MS}" \
    --max-memory-restart "${MAX_MEMORY}" \
    --merge-logs \
    --output "${LOG_DIR}/pm2-out.log" \
    --error "${LOG_DIR}/pm2-err.log"
fi

pm2 save
pm2 status "${PROCESS_NAME}"
