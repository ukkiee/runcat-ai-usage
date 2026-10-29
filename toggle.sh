#!/usr/bin/env bash
# Hide a per-model limit (e.g. Fable) on every card, or show it again.
# Usage: ./toggle.sh Fable    (run it again to show the limit)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || true)"

if [ -z "$PY" ]; then
  echo "error: python3 not found on PATH" >&2
  exit 1
fi
if [ "$#" -ne 1 ]; then
  echo "usage: ./toggle.sh <model>    e.g. ./toggle.sh Fable" >&2
  exit 2
fi

exec "$PY" "$DIR/runcat-poll.py" toggle "$1"
