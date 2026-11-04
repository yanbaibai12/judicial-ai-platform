#!/usr/bin/env bash
set -euo pipefail

payload="$(cat || true)"
case "$payload" in
  *"rm -rf /"*|*"DROP DATABASE"*|*"git push --force"*)
    echo "Blocked destructive command. Require explicit human approval." >&2
    exit 2
    ;;
esac
exit 0
