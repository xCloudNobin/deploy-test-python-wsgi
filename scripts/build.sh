#!/usr/bin/env bash
# Generate the non-sensitive release marker (VERSION) and preflight the Python
# runtime and the pinned Waitress production server (seen by verify.sh/smoke.sh).
#
# Resolution order for the marker:
#   1. $BUILD_MARKER (explicit), e.g. BUILD_MARKER=v1.2.3
#   2. latest git short SHA at the checkout
#   3. current UTC date as a fallback
#
# The marker is intentionally not secret; database paths and environment values
# stay out of the repository. The pins for the single dependency (waitress) live
# in requirements.txt; the venv is created by scripts/verify.sh when missing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/VERSION"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  PY="${PYTHON:-$(command -v python3 || true)}"
fi
[ -n "$PY" ] && [ -x "$PY" ] || { echo "python3 executable not found" >&2; exit 1; }

if [ -n "${BUILD_MARKER:-}" ]; then
  MARKER="$BUILD_MARKER"
elif sha="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)"; then
  MARKER="$sha"
else
  MARKER="build-$(date -u +%Y%m%d-%H%M%S)"
fi

printf '%s\n' "$MARKER" > "$OUT"
printf 'VERSION = %s\n' "$MARKER"

printf 'preflight runtime: '
"$PY" - <<'PY'
import importlib.metadata, sqlite3, sys
if sys.version_info < (3, 10):
    sys.stderr.write("python >= 3.10 required, got %s\n" % sys.version.split()[0])
    sys.exit(1)
try:
    waitress = importlib.metadata.version("waitress")
except importlib.metadata.PackageNotFoundError:
    sys.stderr.write("waitress is not installed; run scripts/verify.sh to create the venv\n")
    sys.exit(1)
print("python %s (stdlib sqlite %s, waitress %s)"
      % (sys.version.split()[0], sqlite3.sqlite_version, waitress))
PY

printf 'compiling Python sources\n'
"$PY" -m compileall -q "$ROOT/src" "$ROOT/tests" "$ROOT/wsgi.py"

if NODE="$(command -v node 2>/dev/null)"; then
  printf 'client JavaScript syntax check\n'
  "$NODE" --check "$ROOT/static/app.js"
else
  printf 'node not found; skipping client JavaScript syntax check\n'
fi

printf 'build complete\n'