#!/usr/bin/env bash
# Dev tasks. Uses local uv if installed, otherwise runs uv in a Python 3.12 container.
# Usage: scripts/dev.sh test|lint|format|lock|hash-password|serve
set -euo pipefail
cd "$(dirname "$0")/.."

UV_IMAGE=ghcr.io/astral-sh/uv:0.9.30-python3.12-bookworm-slim
DOCKER_ARGS=""

run_uv() {
  if command -v uv >/dev/null; then
    command uv "$@"
  else
    local tty=(); [[ -t 0 ]] && tty=(-t)
    docker run --rm -i ${tty[@]+"${tty[@]}"} -v "$PWD:/src" -w /src $DOCKER_ARGS \
      -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_LINK_MODE=copy \
      -v homebase-uv-cache:/root/.cache/uv "$UV_IMAGE" uv "$@"
  fi
}

case "${1:-}" in
  test) shift; run_uv run pytest "$@" ;;
  lint) run_uv run ruff check . && run_uv run ruff format --check . ;;
  format) run_uv run ruff check --fix . && run_uv run ruff format . ;;
  lock) run_uv lock ;;
  hash-password) run_uv run python -m app.core.auth ;;
  serve)
    export HOMEBASE_DATA_DIR=./data
    DOCKER_ARGS="-p 8000:8000 -e HOMEBASE_DATA_DIR"
    run_uv run uvicorn --factory app.main:create_app --reload --host 0.0.0.0 ;;
  *) echo "usage: $0 test|lint|format|lock|hash-password|serve" >&2; exit 2 ;;
esac
