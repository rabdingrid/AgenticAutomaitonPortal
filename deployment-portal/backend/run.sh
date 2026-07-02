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
# Use any installed Ollama model; override with OLLAMA_MODEL if you prefer another.
export OLLAMA_MODEL="${OLLAMA_MODEL:-llama3:latest}"

exec .venv/bin/uvicorn main:app --reload --host 0.0.0.0 --port "$PORT"
