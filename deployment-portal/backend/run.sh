#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3.13}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON=python3
fi

if [[ ! -d .venv ]]; then
  "$PYTHON" -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

PORT="${PORT:-9002}"
# Load .env if present (Graph mail, GitSpace token, etc.)
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
# Default model only when not set in .env (avoid overriding qwen2.5-coder:7b).
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b}"

exec .venv/bin/uvicorn main:app --reload --host 0.0.0.0 --port "$PORT"
