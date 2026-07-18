#!/usr/bin/env bash
# Create the project's isolated CPU and GPU Python environments with uv.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:---cpu}"

if ! command -v uv >/dev/null 2>&1; then
  echo "missing uv 0.11.29; install it with: pipx install 'uv==0.11.29'" >&2
  exit 1
fi

sync_env() {
  local env_dir="$1"
  local version_file="$2"
  shift 2
  local version
  version="$(tr -d '[:space:]' < "$ROOT/$version_file")"
  uv python install "$version"
  UV_PROJECT_ENVIRONMENT="$ROOT/$env_dir" \
    uv sync --project "$ROOT" --managed-python --python "$version" --locked "$@"
}

case "$MODE" in
  --cpu)
    sync_env .venv .python-version
    ;;
  --gpu)
    sync_env .venv-gpu .python-version-gpu --extra gpu
    ;;
  --all)
    sync_env .venv .python-version
    sync_env .venv-gpu .python-version-gpu --extra gpu
    ;;
  *)
    echo "usage: tools/bootstrap_python.sh [--cpu|--gpu|--all]" >&2
    exit 2
    ;;
esac
