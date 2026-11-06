#!/usr/bin/env bash
set -euo pipefail

echo "[gate] ruff"
uv run ruff check src services tests migrations scripts

echo "[gate] mypy"
uv run mypy src

echo "[gate] pytest"
uv run python -m pytest --cov=judicial_ai --cov-report=term-missing --cov-fail-under=75

echo "[gate] alembic heads"
uv run python -m alembic heads >/dev/null

echo "[gate] all checks passed"
