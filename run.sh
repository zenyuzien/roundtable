#!/usr/bin/env bash
# One-command start: creates the venv, installs deps, builds the UI if needed, runs on localhost.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if [ ! -f frontend/dist/index.html ]; then
  (cd frontend && npm install --no-audit --no-fund && npm run build)
fi
exec .venv/bin/python -m roundtable "$@"
