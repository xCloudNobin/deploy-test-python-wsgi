#!/usr/bin/env python3
"""Dependency-free test harness for the plain-WSGI taskboard.

Exercises the real SQLite stack and the exact `api_dispatch` router directly
(no HTTP needed): CRUD, search/filter, LIKE-wildcard escaping, validation
negatives, CSRF enforcement, schema/seed idempotency, restart-style
persistence, cascade delete, and readiness that degrades to 503 when the
database path is unavailable. Exit code 0 only when every check passes.
"""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import api, db  # noqa: E402


PASS = 0
FAIL = 0


def check(cond, name):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("ok   %s" % name)
    else:
        FAIL += 1
        print("FAIL %s" % name)


def fail(message):
    print("FAIL %s" % message)
    sys.exit(1)


WORK = tempfile.mkdtemp(prefix="python-wsgi-tests-")
BLOCKED = os.path.join(WORK, "blocked.txt")
with open(BLOCKED, "w", encoding="utf-8"):
    pass
DB_PATH = os.path.join(WORK, "taskboard.db")


class FakeCfg:
    def __init__(self, database_path):
        self.database_path = database_path
        self.build_marker = "test-marker"
        self.base_path = ""


CFG = FakeCfg(DB_PATH)
BAD_CFG = FakeCfg(os.path.join(BLOCKED, "taskboard.db"))
SESSION = "test-session-token"


def dispatch(
    method,
    segments,
    query=None,
    body=None,
    malformed=False,
    csrf="",
    session=SESSION,
    database_path=None,
    cfg=None,
):
    database_path = database_path or DB_PATH
    cfg = cfg or CFG
    return api.api_dispatch(
        method,
        segments,
        query or {},
        body,
        malformed,
        csrf,
        session,
        database_path,
        cfg,
    )


# ---------------------------------------------------------------- schema/seed
conn = db.open_db(DB_PATH)
db.apply_schema(conn)
db.seed(conn)
initial_projects = (
    sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM project").fetchone()[0]
)
initial_tasks = (
    sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM task").fetchone()[0]
)
check(
    initial_projects == len(db.SEED_PROJECTS),
    "first open creates the expected projects",
)
check(initial_tasks == len(db.SEED_TASKS), "first open creates the expected tasks")

seed2 = db.seed(conn)
check(seed2["seeded"] is False, "second seed run is a no-op (idempotent)")
check(
    seed2["projects"] == len(db.SEED_PROJECTS), "seed idempotent: project count stable"
)
check(seed2["tasks"] == len(db.SEED_TASKS), "seed idempotent: task count stable")

conn2 = db.open_db(DB_PATH)
db.apply_schema(conn2)
counts = db.seed(conn2)
check(
    counts["projects"] == len(db.SEED_PROJECTS),
    "schema/seed idempotent across fresh connections",
)
conn2.close()
conn.close()

# ---------------------------------------------------------------- health/meta
live = dispatch("GET", ["api", "health", "live"])
check(live["status"] == 200, "liveness returns 200")
check(live["body"]["status"] == "alive", "liveness body reports alive")

ready = dispatch("GET", ["api", "health", "ready"])
check(ready["status"] == 200, "readiness returns 200 when the database is available")
check(ready["body"]["status"] == "ready", "readiness body reports ready")

probe = db.probe(DB_PATH)
check(probe["ok"] is True, "probe succeeds on a usable database path")
bad_probe = db.probe(os.path.join(BLOCKED, "db.sqlite"))
check(bad_probe["ok"] is False, "probe fails when the database cannot be opened")

meta = dispatch("GET", ["api", "meta"])
check(meta["status"] == 200, "meta returns 200")
check(meta["body"]["release"] == "test-marker", "meta exposes the release marker")
check(meta["body"]["runtime"]["name"] == "python", "meta reports python runtime")
check(meta["body"]["csrf"] == SESSION, "meta exposes the session CSRF token")

# ---------------------------------------------------------------- CSRF guard
no_csrf = dispatch("POST", ["api", "tasks"], body={"project_id": 1, "title": "x"})
check(no_csrf["status"] == 403, "mutation without a CSRF token is rejected (403)")
bad_csrf = dispatch(
    "POST", ["api", "tasks"], body={"project_id": 1, "title": "x"}, csrf="wrong-token"
)
check(
    bad_csrf["status"] == 403, "mutation with a mismatched CSRF token is rejected (403)"
)

ok_read = dispatch("GET", ["api", "projects"])
check(ok_read["status"] == 200, "read routes pass without a CSRF token")

# ---------------------------------------------------------------- projects CRUD
create_project = dispatch(
    "POST",
    ["api", "projects"],
    body={"name": "Test Project", "description": "desc", "status": "active"},
    csrf=SESSION,
)
check(create_project["status"] == 201, "create project returns 201")
project = create_project["body"]["project"]
check(project and project.get("id"), "created project has an id")
pid = int(project["id"])

project_detail = dispatch("GET", ["api", "projects", str(pid)])
check(
    project_detail["status"] == 200
    and project_detail["body"]["project"]["name"] == "Test Project",
    "read project returns the created project",
)

update_project = dispatch(
    "PATCH",
    ["api", "projects", str(pid)],
    body={"name": "Test Project (renamed)"},
    csrf=SESSION,
)
check(
    update_project["status"] == 200
    and update_project["body"]["project"]["name"] == "Test Project (renamed)",
    "project update is reflected",
)

projects_list = dispatch("GET", ["api", "projects"])
names = [p["name"] for p in projects_list["body"]["projects"]]
check("Test Project (renamed)" in names, "project list contains the updated project")
check("Launch checklist" in names, "seeded project still present in the list")

# ---------------------------------------------------------------- tasks CRUD
create_task = dispatch(
    "POST",
    ["api", "tasks"],
    body={
        "project_id": pid,
        "title": "First task",
        "description": "about it",
        "status": "in_progress",
        "priority": "high",
    },
    csrf=SESSION,
)
check(create_task["status"] == 201, "create task returns 201")
task = create_task["body"]["task"]
check(task and task.get("id"), "created task has an id")
tid = int(task["id"])

dispatch(
    "POST",
    ["api", "tasks"],
    body={
        "project_id": pid,
        "title": "Second task",
        "status": "done",
        "priority": "low",
    },
    csrf=SESSION,
)

task_detail = dispatch("GET", ["api", "tasks", str(tid)])
check(
    task_detail["body"]["task"]["title"] == "First task",
    "read task returns the created task",
)
check(
    task_detail["body"]["task"]["project_name"] == "Test Project (renamed)",
    "task join exposes the project name",
)

update_task = dispatch(
    "PATCH",
    ["api", "tasks", str(tid)],
    body={"title": "First task (updated)", "status": "done"},
    csrf=SESSION,
)
check(
    update_task["status"] == 200
    and update_task["body"]["task"]["title"] == "First task (updated)",
    "task update is reflected",
)

move = dispatch(
    "PATCH", ["api", "tasks", str(tid)], body={"project_id": 2}, csrf=SESSION
)
check(
    move["status"] == 200 and move["body"]["task"]["project_id"] == 2,
    "task can be moved to another project",
)

delete_task = dispatch("DELETE", ["api", "tasks", str(tid)], csrf=SESSION)
check(delete_task["status"] == 204, "delete task returns 204")
gone = dispatch("GET", ["api", "tasks", str(tid)])
check(gone["status"] == 404, "deleted task is gone (404)")

# ---------------------------------------------------------------- search/filter
dispatch(
    "POST",
    ["api", "tasks"],
    body={
        "project_id": pid,
        "title": "Alpha widget error",
        "status": "todo",
        "priority": "low",
    },
    csrf=SESSION,
)
dispatch(
    "POST",
    ["api", "tasks"],
    body={
        "project_id": pid,
        "title": "Beta widget",
        "status": "in_progress",
        "priority": "high",
    },
    csrf=SESSION,
)

search = dispatch("GET", ["api", "tasks"], query={"q": "widget error"})
titles = [t["title"] for t in search["body"]["tasks"]]
check("Alpha widget error" in titles, "search finds the matching task")
check("Beta widget" not in titles, "search excludes non-matching tasks")
check(search["body"]["count"] == 1, "search count reflects only matches")

status_filter = dispatch(
    "GET", ["api", "tasks"], query={"status": "done", "project_id": "2"}
)
statuses = list({t["status"] for t in status_filter["body"]["tasks"]})
check(
    statuses == ["done"] or statuses == [],
    "status filter narrows results to the requested status",
)

project_filter = dispatch("GET", ["api", "tasks"], query={"project_id": str(pid)})
pids = [t["project_id"] for t in project_filter["body"]["tasks"]]
check(
    all(x == pid for x in pids),
    "project filter narrows results to the requested project",
)

invalid_filter = dispatch("GET", ["api", "tasks"], query={"status": "warp"})
check(invalid_filter["status"] == 400, "invalid status filter is rejected (400)")

wild = dispatch("GET", ["api", "tasks"], query={"q": "%"})
check(
    wild["body"]["count"] == 0,
    'search for "%" matches nothing while no record contains "%" (LIKE wildcards escaped)',
)

dispatch(
    "POST",
    ["api", "tasks"],
    body={"project_id": pid, "title": "100% complete review"},
    csrf=SESSION,
)
literal = dispatch("GET", ["api", "tasks"], query={"q": "100% complete"})
check(
    literal["body"]["count"] == 1, 'literal "%" in the query still matches its own text'
)
wild2 = dispatch("GET", ["api", "tasks"], query={"q": "%"})
check(
    wild2["body"]["count"] == 1,
    'search for "%" matches only the literal "%" record, not every task',
)

# ---------------------------------------------------------------- validation negatives
validation_cases = [
    (
        "blank task title",
        dispatch(
            "POST",
            ["api", "tasks"],
            body={"project_id": 1, "title": "   "},
            csrf=SESSION,
        ),
    ),
    (
        "missing project_id",
        dispatch("POST", ["api", "tasks"], body={"title": "x"}, csrf=SESSION),
    ),
    (
        "invalid task status",
        dispatch(
            "POST",
            ["api", "tasks"],
            body={"project_id": 1, "title": "x", "status": "warp"},
            csrf=SESSION,
        ),
    ),
    (
        "invalid priority",
        dispatch(
            "POST",
            ["api", "tasks"],
            body={"project_id": 1, "title": "x", "priority": "urgent"},
            csrf=SESSION,
        ),
    ),
    (
        "blank project name",
        dispatch("POST", ["api", "projects"], body={"name": "  "}, csrf=SESSION),
    ),
    (
        "invalid project status",
        dispatch(
            "POST",
            ["api", "projects"],
            body={"name": "x", "status": "warp"},
            csrf=SESSION,
        ),
    ),
    (
        "nonexistent project on task create",
        dispatch(
            "POST",
            ["api", "tasks"],
            body={"project_id": 999999, "title": "x"},
            csrf=SESSION,
        ),
    ),
]
for name, result in validation_cases:
    check(result["status"] == 400, "validation: %s -> 400" % name)

long_title = "a" * 201
check(
    dispatch(
        "POST",
        ["api", "tasks"],
        body={"project_id": 1, "title": long_title},
        csrf=SESSION,
    )["status"]
    == 400,
    "validation: overlong task title -> 400",
)
check(
    dispatch(
        "POST", ["api", "tasks"], body={"project_id": 1, "title": 42}, csrf=SESSION
    )["status"]
    == 400,
    "validation: non-string task title -> 400",
)
check(
    dispatch(
        "POST", ["api", "tasks"], body={"project_id": "abc", "title": "x"}, csrf=SESSION
    )["status"]
    == 400,
    "validation: non-integer project_id -> 400",
)
check(
    dispatch("PATCH", ["api", "tasks", "1"], body={}, csrf=SESSION)["status"] == 400,
    "validation: empty PATCH -> 400",
)
check(
    dispatch("PATCH", ["api", "projects", "1"], body={}, csrf=SESSION)["status"] == 400,
    "validation: empty project PATCH -> 400",
)
check(
    dispatch("POST", ["api", "tasks"], malformed=True, csrf=SESSION)["status"] == 400,
    "validation: malformed JSON -> 400",
)

error_fields = dispatch(
    "POST", ["api", "tasks"], body={"title": "no project"}, csrf=SESSION
)
check(
    isinstance(error_fields["body"].get("fields"), dict),
    "validation error includes a fields map",
)
check(
    "project_id" in error_fields["body"]["fields"],
    "validation fields mention project_id",
)

# ---------------------------------------------------------------- not found / method
check(
    dispatch("GET", ["api", "tasks", "999999"])["status"] == 404, "unknown task -> 404"
)
check(
    dispatch("GET", ["api", "projects", "999999"])["status"] == 404,
    "unknown project -> 404",
)
check(
    dispatch("PUT", ["api", "tasks"], body={}, csrf=SESSION)["status"] == 405,
    "unhandled method -> 405",
)
check(dispatch("GET", ["api", "unknown"])["status"] == 404, "unknown API route -> 404")
check(
    dispatch("PATCH", ["api", "tasks", "a"], body={"title": "x"}, csrf=SESSION)[
        "status"
    ]
    == 400,
    "invalid id -> 400",
)

# ---------------------------------------------------------------- persistence across an open/close cycle
conn3 = db.open_db(DB_PATH)
row = conn3.execute(
    "SELECT title FROM task WHERE title = ?", ("100% complete review",)
).fetchone()
check(
    row is not None and row[0] == "100% complete review",
    "restart-style persistence: a record survives a fresh connection on the same path",
)
conn3.close()

delete_project = dispatch("DELETE", ["api", "projects", str(pid)], csrf=SESSION)
check(delete_project["status"] == 204, "delete project returns 204")
orphan_count = (
    sqlite3.connect(DB_PATH)
    .execute("SELECT COUNT(*) FROM task WHERE project_id = ?", (pid,))
    .fetchone()[0]
)
check(orphan_count == 0, "project delete cascades to its tasks")

# ---------------------------------------------------------------- readiness against an unusable path
down = dispatch(
    "GET", ["api", "health", "ready"], database_path=BAD_CFG.database_path, cfg=BAD_CFG
)
check(down["status"] == 503, "readiness -> 503 when the database is unavailable")
check(down["body"]["status"] == "unavailable", "readiness body reports unavailable")
down_alive = dispatch(
    "GET", ["api", "health", "live"], database_path=BAD_CFG.database_path, cfg=BAD_CFG
)
check(down_alive["status"] == 200, "liveness -> 200 while the database is unavailable")
down_api = dispatch(
    "GET", ["api", "projects"], database_path=BAD_CFG.database_path, cfg=BAD_CFG
)
check(
    down_api["status"] == 503, "API list route -> 503 while the database is unavailable"
)


def rmtree(path):
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.isdir(full):
            rmtree(full)
        else:
            os.unlink(full)
    os.rmdir(path)


rmtree(WORK)

print("")
print("=== test summary: %d passed, %d failed ===" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
