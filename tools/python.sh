#!/usr/bin/env bash
# Run project Python tools from the uv-managed, system-independent environment.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_DIR="$ROOT/.venv"

if [ "${1:-}" = "--gpu" ]; then
  ENV_DIR="$ROOT/.venv-gpu"
  shift
fi

PYTHON="$ENV_DIR/bin/python"
if [ ! -x "$PYTHON" ]; then
  if [ "$ENV_DIR" = "$ROOT/.venv-gpu" ]; then
    echo "missing $ENV_DIR; run: UV_PROJECT_ENVIRONMENT=.venv-gpu uv sync --managed-python --locked --extra gpu" >&2
  else
    echo "missing $ENV_DIR; run: uv sync --managed-python --locked" >&2
  fi
  exit 1
fi

exec "$PYTHON" "$@"
