#!/usr/bin/env bash
set -euo pipefail

# Advisory hook: format/lint only when uv is available.
if command -v uv >/dev/null 2>&1; then
  uv run ruff check src services tests migrations scripts >/dev/null 2>&1 || true
fi
exit 0
