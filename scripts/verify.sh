#!/usr/bin/env bash
# Full verification for the plain-WSGI taskboard fixture:
#
#   1. Creates the pinned venv (waitress==3.0.2 from requirements.txt) if the
#      project .venv is missing.
#   2. scripts/build.sh -> release marker (VERSION) + runtime preflight +
#      Python compilation + client JS syntax check.
#   3. dependency-free unit/integration suite -> python tests/run_tests.py
#   4. scripts/smoke.sh -> real production server (waitress -> wsgi:application):
#      CRUD, invalid input, search/filter, restart persistence,
#      database-unavailable readiness + recovery.
#
# Usage:
#   scripts/verify.sh
#
# Exit codes: 0 = all checks passed, nonzero = a check failed. The first
# failing step aborts with its own nonzero code.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$(command -v python3)}"
[ -x "$PY" ] || { echo "python3 executable not found" >&2; exit 1; }

step() { printf '\n=== %s ===\n' "$*"; }

step "provision pinned venv (waitress==3.0.2)"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  "$PY" -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install --quiet -r "$ROOT/requirements.txt"
else
  printf 'venv already present (reuse)\n'
fi

step "build release marker + preflight runtime + compile + JS check"
"$ROOT/scripts/build.sh"

step "unit/integration suite (python tests/run_tests.py)"
(cd "$ROOT" && "$ROOT/.venv/bin/python" tests/run_tests.py)

step "production smoke: real process + CRUD + negatives + persistence + DB outage/recovery"
"$ROOT/scripts/smoke.sh"

step "verification complete (all steps passed)"
printf '%s\n' "python: $("$ROOT/.venv/bin/python" -c 'import sys; print(sys.version.split()[0])')"
printf '%s\n' "waitress: $("$ROOT/.venv/bin/python" -c 'import importlib.metadata; print(importlib.metadata.version("waitress"))')"
printf '%s\n' "release marker: $(cat "$ROOT/VERSION")"