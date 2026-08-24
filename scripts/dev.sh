#!/usr/bin/env bash
# Developer entry point: one command per quality gate, in the order CI runs them.
set -euo pipefail
cd "$(dirname "$0")/.."
VENV=${VENV:-.venv}

case "${1:-all}" in
  setup)   uv venv --python 3.13 "$VENV" && uv pip install --python "$VENV/bin/python" -e ".[dev]" ;;
  lint)    "$VENV/bin/ruff" check src tests && "$VENV/bin/ruff" format --check src tests ;;
  format)  "$VENV/bin/ruff" check src tests --fix && "$VENV/bin/ruff" format src tests ;;
  types)   "$VENV/bin/mypy" ;;
  test)    "$VENV/bin/python" -m pytest -q ;;
  cov)     "$VENV/bin/python" -m pytest -q --cov=gdap --cov-report=term-missing ;;
  demo)    "$VENV/bin/gdap" demo run ;;
  serve)   "$VENV/bin/gdap" system serve --reload ;;
  all)     "$0" lint && "$0" types && "$0" test ;;
  *)       echo "usage: $0 {setup|lint|format|types|test|cov|demo|serve|all}" && exit 1 ;;
esac
