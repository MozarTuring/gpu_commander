#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}/coordinator"

export GPU_COMMANDER_CONFIG="${SCRIPT_DIR}/config.yaml"
exec python3 -m uvicorn coordinator_server:app --host 0.0.0.0 --port 9800 --reload
