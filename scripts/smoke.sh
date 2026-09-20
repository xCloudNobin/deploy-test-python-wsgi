#!/usr/bin/env bash
# Reproducible production smoke test for the plain-WSGI taskboard.
#
# 1. scripts/build.sh -> VERSION marker + runtime preflight.
# 2. Starts the REAL production process: waitress serving `wsgi:application`.
# 3. Bootstraps a session + CSRF token with a cookie jar, then exercises
#    liveness/readiness, project/task CRUD, search/filter and negative
#    validation cases over real HTTP against the JSON API.
# 4. Stops the process (SIGTERM -> graceful waitress shutdown), restarts it on
#    the SAME SQLite path and proves a survivor record persisted.
# 5. Starts a process against a database path that cannot be opened (its parent
#    is a regular file) and proves readiness goes 503 while liveness stays 200
#    and the static UI still serves; then repairs the path and proves readiness
#    recovers once the database is available again.
#
# Every started server is terminated on success AND on failure (trap EXIT).
# Exit codes: 0 = all checks passed, nonzero = a check failed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  PY="${PYTHON:-$(command -v python3 || true)}"
fi
[ -n "$PY" ] && [ -x "$PY" ] || { echo "python3 executable not found" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "curl not found" >&2; exit 1; }

"$ROOT/scripts/build.sh" >/dev/null
[ -f "$ROOT/wsgi.py" ] || { echo "wsgi.py missing" >&2; exit 1; }

WORK="$(mktemp -d /tmp/python-wsgi-smoke.XXXXXX)"
PIDFILE="$WORK/server.pid"
DB="$WORK/data/taskboard.db"
LOG1="$WORK/server1.log"
LOG2="$WORK/server2.log"
LOG3="$WORK/server-fail.log"
LOG4="$WORK/server-recovery.log"
BASE="http://127.0.0.1"
JAR="$WORK/cookies.txt"
SURVIVOR_NAME="PERSIST-$(date +%s)-survivor"

PASS=0
FAIL=0

ok()  { PASS=$((PASS + 1)); echo "ok   $*"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL $*"; }

cleanup() {
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null || true
    sleep 0.3
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

pick_port() {
  "$PY" - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

start_server() { # db_path port logfile -> 0 on success
  local db_path="$1" port="$2" logfile="$3"
  DATA_DIR="$(dirname "$db_path")" \
  DATABASE_PATH="$db_path" \
  PORT="$port" BIND_HOST="127.0.0.1" BUILD_MARKER="smoke-$port" \
    "$PY" -m waitress --listen="127.0.0.1:$port" --threads=4 wsgi:application \
      >"$logfile" 2>&1 &
  echo $! > "$PIDFILE"
  sleep 0.5
}

stop_server() { # SIGTERM first (graceful), wait, SIGKILL fallback
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 50); do
      if ! kill -0 "$pid" 2>/dev/null; then break; fi
      sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
      bad "process did not exit on SIGTERM, forcing"
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$PIDFILE"
}

wait_live() { # port label [logfile]
  local port="$1" label="$2" logfile="${3:-}"
  local code=000
  for _ in $(seq 1 80); do
    code="$(curl -s --max-time 2 -o /dev/null -w '%{http_code}' "$BASE:$port/api/health/live" || true)"
    [ "$code" = "200" ] && { ok "liveness HTTP 200 ($label)"; return 0; }
    sleep 0.3
  done
  bad "server never became live (last code $code, $label)"
  [ -n "$logfile" ] && [ -f "$logfile" ] && tail -20 "$logfile" || true
  return 1
}

expect_status() { # label url expected [method] [curl args...]
  local label="$1" url="$2" expected="$3"
  shift 3
  local method="GET"
  if [ "$#" -gt 0 ]; then method="$1"; shift; fi
  local code
  if [ "$#" -gt 0 ]; then
    code="$(curl -s --max-time 3 -o /dev/null -w '%{http_code}' -X "$method" -b "$JAR" -c "$JAR" "$@" "$url" || true)"
  else
    code="$(curl -s --max-time 3 -o /dev/null -w '%{http_code}' -X "$method" -b "$JAR" -c "$JAR" "$url" || true)"
  fi
  if [ "$code" = "$expected" ]; then ok "$label ($code)"; else bad "$label: expected $expected got $code"; fi
}

body() { # url -> body
  curl -s --max-time 3 -b "$JAR" -c "$JAR" "$1"
}

json_field() { # file dot.path -> value
  "$PY" - "$1" "$2" <<'PY'
import sys, json
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
for key in sys.argv[2].split("."):
    data = data[key]
print(data)
PY
}

post_json() { # url payload_file out_file -> http code  (CSRF header + cookie jar)
  local csrf
  csrf="$(cat "$WORK/csrf.txt" 2>/dev/null || printf '')"
  curl -s --max-time 3 -o "$3" -w '%{http_code}' -X POST \
    -b "$JAR" -c "$JAR" \
    -H 'content-type: application/json' -H "X-CSRF-Token: $csrf" \
    --data-binary @"$2" "$1"
}

patch_json() { # url payload_file out_file -> http code
  local csrf
  csrf="$(cat "$WORK/csrf.txt" 2>/dev/null || printf '')"
  curl -s --max-time 3 -o "$3" -w '%{http_code}' -X PATCH \
    -b "$JAR" -c "$JAR" \
    -H 'content-type: application/json' -H "X-CSRF-Token: $csrf" \
    --data-binary @"$2" "$1"
}

del_json() { # url -> http code
  local csrf
  csrf="$(cat "$WORK/csrf.txt" 2>/dev/null || printf '')"
  curl -s --max-time 3 -o /dev/null -w '%{http_code}' -X DELETE \
    -b "$JAR" -c "$JAR" -H "X-CSRF-Token: $csrf" "$1"
}

printf '=== python-wsgi smoke start: %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

PORT1="$(pick_port)"
echo "phase 1: production server on 127.0.0.1:$PORT1 (waitress -> wsgi:application)"
[ -f "$ROOT/wsgi.py" ] && ok "production entrypoint exists (wsgi.py)" || bad "entrypoint missing"
start_server "$DB" "$PORT1" "$LOG1"
wait_live "$PORT1" "phase-1" "$LOG1"

MARKER="smoke-$PORT1"
meta="$(body "$BASE:$PORT1/api/meta")"
case "$meta" in
  *"$MARKER"*) ok "release marker in /api/meta ($MARKER)";;
  *) bad "release marker missing from /api/meta: $meta";;
esac
case "$meta" in
  *'"name":"python"'*) ok "runtime reports python";;
  *) bad "runtime not reported as python";;
esac

printf '%s\n' "$meta" | "$PY" -c 'import sys,json; print(json.load(sys.stdin)["csrf"])' > "$WORK/csrf.txt"
[ -s "$WORK/csrf.txt" ] && ok "session bootstrapped and CSRF token captured" || bad "CSRF token not captured"
CSRF="$(cat "$WORK/csrf.txt")"

ready="$(body "$BASE:$PORT1/api/health/ready")"
case "$ready" in
  *'"status":"ready"'*) ok "readiness reports ready";;
  *) bad "readiness payload: $ready";;
esac

index="$(body "$BASE:$PORT1/")"
case "$index" in
  *"WSGI Taskboard"*) ok "UI served at /";;
  *) bad "UI index not served";;
esac
expect_status "client JS served" "$BASE:$PORT1/app.js" 200
expect_status "client CSS served" "$BASE:$PORT1/style.css" 200
expect_status "unknown static -> 404" "$BASE:$PORT1/missing.css" 404
expect_status "unknown API -> 404" "$BASE:$PORT1/api/unknown" 404
expect_status "nested UI route serves the shell" "$BASE:$PORT1/project/1" 200

expect_status "mutation without CSRF token -> 403" "$BASE:$PORT1/api/projects" 403 POST \
  -H 'content-type: application/json' --data-binary '{"name":"nope"}'

PROJ_CODE="$(post_json "$BASE:$PORT1/api/projects" <(printf '%s' '{"name":"Smoke Project","description":"created over HTTP","status":"active"}') "$WORK/project.json")"
[ "$PROJ_CODE" = "201" ] && ok "create project (201)" || bad "create project: $PROJ_CODE"
PROJ_ID="$(json_field "$WORK/project.json" project.id)"

projs="$(body "$BASE:$PORT1/api/projects")"
case "$projs" in
  *"Launch checklist"*) ok "idempotent seed data present";;
  *) bad "seed data missing";;
esac

T1_OUT="$WORK/task1.json"; T2_OUT="$WORK/task2.json"; T3_OUT="$WORK/task3.json"
C1="$(post_json "$BASE:$PORT1/api/tasks" <(printf '{"project_id":%s,"title":"Smoke task done","description":"first","status":"done","priority":"high"}' "$PROJ_ID") "$T1_OUT")"
[ "$C1" = "201" ] && ok "create task done (201)" || bad "create task done: $C1"
C2="$(post_json "$BASE:$PORT1/api/tasks" <(printf '{"project_id":%s,"title":"Smoke task in progress","description":"second","status":"in_progress","priority":"medium"}' "$PROJ_ID") "$T2_OUT")"
[ "$C2" = "201" ] && ok "create task in_progress (201)" || bad "create task in_progress: $C2"
C3="$(post_json "$BASE:$PORT1/api/tasks" <(printf '{"project_id":%s,"title":"Smoke task todo","description":"third","status":"todo","priority":"low"}' "$PROJ_ID") "$T3_OUT")"
[ "$C3" = "201" ] && ok "create task todo (201)" || bad "create task todo: $C3"

T1_ID="$(json_field "$T1_OUT" task.id)"

proj="$(body "$BASE:$PORT1/api/projects/$PROJ_ID")"
hits=0
case "$proj" in *"Smoke task done"*) hits=$((hits + 1));; esac
case "$proj" in *"Smoke task in progress"*) hits=$((hits + 1));; esac
case "$proj" in *"Smoke task todo"*) hits=$((hits + 1));; esac
[ "$hits" = "3" ] && ok "read: project exposes all three tasks" || bad "project task listing incomplete (hits=$hits)"

search="$(body "$BASE:$PORT1/api/tasks?q=Smoke+task+done")"
case "$search" in *"Smoke task done"*) ok "search finds matching task";; *) bad "search missed task";; esac
case "$search" in *"Smoke task todo"*) bad "search leaked non-matching task";; *) ok "search excludes non-matching task";; esac

filtered="$(body "$BASE:$PORT1/api/tasks?status=done&project_id=$PROJ_ID")"
case "$filtered" in
  *"Smoke task done"*) ok "status filter includes done task";;
  *) bad "status filter missed done task";;
esac
case "$filtered" in
  *"Smoke task todo"*) bad "status filter leaked todo task";;
  *) ok "status filter excludes todo task";;
esac

prio="$(body "$BASE:$PORT1/api/tasks?priority=low&project_id=$PROJ_ID")"
case "$prio" in
  *"Smoke task todo"*) ok "priority filter includes low task";;
  *) bad "priority filter missed low task";;
esac
case "$prio" in
  *"Smoke task done"*) bad "priority filter leaked high task";;
  *) ok "priority filter excludes high task";;
esac

UP="$WORK/update.json"
U="$(patch_json "$BASE:$PORT1/api/tasks/$T1_ID" <(printf '%s' '{"title":"Smoke task done (updated)","status":"done","priority":"high"}') "$UP")"
[ "$U" = "200" ] && ok "update task (200)" || bad "update task: $U"
updated="$(body "$BASE:$PORT1/api/tasks/$T1_ID")"
case "$updated" in *"Smoke task done (updated)"*) ok "read-back confirms task update";; *) bad "task update not reflected";; esac

PU="$(patch_json "$BASE:$PORT1/api/projects/$PROJ_ID" <(printf '%s' '{"name":"Smoke Project (renamed)"}') "$WORK/project-updated.json")"
[ "$PU" = "200" ] && ok "update project (200)" || bad "update project: $PU"
proj2="$(body "$BASE:$PORT1/api/projects/$PROJ_ID")"
case "$proj2" in *"Smoke Project (renamed)"*) ok "read-back confirms project update";; *) bad "project update not reflected";; esac

TD_KEEP="$(post_json "$BASE:$PORT1/api/tasks" <(printf '{"project_id":%s,"title":"throwaway","status":"todo","priority":"low"}' "$PROJ_ID") "$WORK/throw.json")"
[ "$TD_KEEP" = "201" ] && ok "create throwaway task (201)" || bad "create throwaway: $TD_KEEP"
THROW_ID="$(json_field "$WORK/throw.json" task.id)"
D="$(del_json "$BASE:$PORT1/api/tasks/$THROW_ID")"
[ "$D" = "204" ] && ok "delete task (204)" || bad "delete task: $D"
gone="$(body "$BASE:$PORT1/api/tasks/$THROW_ID")"
case "$gone" in *'Task not found'*) ok "deleted task no longer served";; *) bad "unexpected body for deleted task: $gone";; esac

CSRF="$(cat "$WORK/csrf.txt")"
expect_status "blank task title -> 400" "$BASE:$PORT1/api/tasks" 400 POST \
  -H 'content-type: application/json' -H "X-CSRF-Token: $CSRF" --data-binary '{"project_id":1,"title":"   "}'
expect_status "missing project_id -> 400" "$BASE:$PORT1/api/tasks" 400 POST \
  -H 'content-type: application/json' -H "X-CSRF-Token: $CSRF" --data-binary '{"title":"x"}'
expect_status "invalid task status -> 400" "$BASE:$PORT1/api/tasks" 400 POST \
  -H 'content-type: application/json' -H "X-CSRF-Token: $CSRF" --data-binary '{"project_id":1,"title":"x","status":"warp"}'
expect_status "invalid priority -> 400" "$BASE:$PORT1/api/tasks" 400 POST \
  -H 'content-type: application/json' -H "X-CSRF-Token: $CSRF" --data-binary '{"project_id":1,"title":"x","priority":"urgent"}'
expect_status "malformed json -> 400" "$BASE:$PORT1/api/tasks" 400 POST \
  -H 'content-type: application/json' -H "X-CSRF-Token: $CSRF" --data-binary '{nope'
expect_status "blank project name -> 400" "$BASE:$PORT1/api/projects" 400 POST \
  -H 'content-type: application/json' -H "X-CSRF-Token: $CSRF" --data-binary '{"name":"   "}'
expect_status "invalid project status -> 400" "$BASE:$PORT1/api/projects" 400 POST \
  -H 'content-type: application/json' -H "X-CSRF-Token: $CSRF" --data-binary '{"name":"x","status":"warp"}'
expect_status "unknown task -> 404" "$BASE:$PORT1/api/tasks/999999" 404
expect_status "unknown project -> 404" "$BASE:$PORT1/api/projects/999999" 404
expect_status "empty patch -> 400" "$BASE:$PORT1/api/tasks/$T1_ID" 400 PATCH \
  -H 'content-type: application/json' -H "X-CSRF-Token: $CSRF" --data-binary '{}'
expect_status "unhandled method -> 405" "$BASE:$PORT1/api/tasks" 405 PUT \
  -H 'content-type: application/json' -H "X-CSRF-Token: $CSRF" --data-binary '{}'

errbody="$(curl -s --max-time 3 -X POST -b "$JAR" -c "$JAR" -H 'content-type: application/json' -H "X-CSRF-Token: $CSRF" \
  --data-binary '{"project_id":1,"title":"   "}' "$BASE:$PORT1/api/tasks" || true)"
case "$errbody" in
  *"title"*"required"*) ok "meaningful validation error body";;
  *) bad "validation error body unexpected: $errbody";;
esac

if [ -f "$DB" ]; then
  bytes=$(wc -c < "$DB")
  [ "$bytes" -gt 0 ] && ok "sqlite data stored on disk ($bytes bytes)" || bad "database file empty"
else
  bad "database file missing at configured path"
fi

SRV="$WORK/survivor.json"
SC="$(post_json "$BASE:$PORT1/api/tasks" <(printf '{"project_id":%s,"title":"%s","description":"must survive restart","status":"in_progress","priority":"high"}' "$PROJ_ID" "$SURVIVOR_NAME") "$SRV")"
[ "$SC" = "201" ] && ok "persistence survivor created" || bad "survivor create: $SC"
SURV_ID="$(json_field "$SRV" task.id)"
COUNT_BEFORE="$(body "$BASE:$PORT1/api/tasks?project_id=$PROJ_ID" | "$PY" -c 'import sys,json;print(len(json.load(sys.stdin)["tasks"]))')"

echo "--- phase 2: signal-based stop (graceful waitress shutdown)"
stop_server

echo "--- phase 3: restart on same sqlite path (persistence)"
PORT2="$(pick_port)"
start_server "$DB" "$PORT2" "$LOG2"
wait_live "$PORT2" "phase-3" "$LOG2"
printf '%s\n' "$(body "$BASE:$PORT2/api/meta")" | "$PY" -c 'import sys,json; print(json.load(sys.stdin)["csrf"])' > "$WORK/csrf.txt"
surv="$(body "$BASE:$PORT2/api/tasks/$SURV_ID")"
case "$surv" in
  *"$SURVIVOR_NAME"*) ok "persistence: survivor task survived restart";;
  *) bad "persistence FAILED: survivor missing after restart";;
esac
COUNT_AFTER="$(body "$BASE:$PORT2/api/tasks?project_id=$PROJ_ID" | "$PY" -c 'import sys,json;print(len(json.load(sys.stdin)["tasks"]))')"
[ "$COUNT_BEFORE" = "$COUNT_AFTER" ] && ok "task counts stable across restart ($COUNT_AFTER)" || bad "count changed: $COUNT_BEFORE -> $COUNT_AFTER"
stop_server

echo "--- phase 4: database unavailable (readiness fails while process stays alive)"
BLOCKED="$WORK/blocked.txt"
touch "$BLOCKED"
PORT3="$(pick_port)"
start_server "$BLOCKED/unreachable.db" "$PORT3" "$LOG3"
wait_live "$PORT3" "phase-4" "$LOG3"
expect_status "readiness /api/health/ready -> 503" "$BASE:$PORT3/api/health/ready" 503
expect_status "liveness /api/health/live -> 200" "$BASE:$PORT3/api/health/live" 200
ready_down="$(body "$BASE:$PORT3/api/health/ready" || true)"
case "$ready_down" in
  *'"status":"unavailable"'*) ok "readiness body reports unavailable";;
  *) bad "readiness down body: $ready_down";;
esac
expect_status "API list route degrades to 503" "$BASE:$PORT3/api/projects" 503
expect_status "static UI still served" "$BASE:$PORT3/" 200
stop_server

echo "--- phase 5: database available again (readiness recovers)"
rm -f "$BLOCKED"
mkdir -p "$BLOCKED"
PORT4="$(pick_port)"
start_server "$BLOCKED/restored.db" "$PORT4" "$LOG4"
wait_live "$PORT4" "phase-5" "$LOG4"
expect_status "readiness recovers to 200" "$BASE:$PORT4/api/health/ready" 200
stop_server

echo
printf '=== python-wsgi smoke summary: %s passed, %s failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]