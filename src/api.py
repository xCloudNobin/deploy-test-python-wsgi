"""JSON API router and validated CRUD for the plain-WSGI taskboard.

`api_dispatch` is a pure function over method + path segments so the test
suite can exercise the exact production logic without HTTP. Responses are
returned as {"status", "body", "headers"} and serialized by the WSGI layer.
"""

from __future__ import annotations

import sqlite3

from . import db
from .security import csrf_matches

MALFORMED = object()

METHODS_BY_ROUTE = {
    ("health/live", None): {"GET"},
    ("health/ready", None): {"GET"},
    ("meta", None): {"GET"},
    ("csrf", None): {"GET"},
    ("projects", None): {"GET", "POST"},
    ("projects", "id"): {"GET", "PATCH", "DELETE"},
    ("tasks", None): {"GET", "POST"},
    ("tasks", "id"): {"GET", "PATCH", "DELETE"},
}
MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}


def _match_route(segments):
    if not segments or segments[0] != "api":
        return None
    rest = segments[1:]
    if len(rest) == 1 and rest[0] in ("meta", "csrf", "projects", "tasks"):
        return (rest[0], None)
    if len(rest) == 2 and rest[0] == "health" and rest[1] in ("live", "ready"):
        return ("health/" + rest[1], None)
    if len(rest) == 2 and rest[0] in ("projects", "tasks"):
        return (rest[0], rest[1])
    return None


def api_dispatch(
    method,
    segments,
    query,
    body,
    json_broken,
    header_csrf,
    session_token,
    database_path,
    cfg,
):
    route = _match_route(segments)
    if route is None:
        return _json(404, {"error": "not found"})
    key, ident = route
    if key == "health":
        return _json(404, {"error": "not found"})
    allowed = METHODS_BY_ROUTE.get((key, "id" if ident is not None else None), set())
    if method not in allowed:
        return _json(405, {"error": "method not allowed", "allowed": sorted(allowed)})
    if json_broken:
        return _json(400, {"error": "malformed JSON body"})
    if method in MUTATING_METHODS:
        csrf = header_csrf
        if isinstance(body, dict) and body.get("_csrf"):
            csrf = body["_csrf"]
        if not csrf_matches(csrf, session_token):
            return _json(403, {"error": "invalid or missing CSRF token"})
    body = dict(body) if isinstance(body, dict) else {}
    query = dict(query or {})

    if key == "health/live":
        return _live(cfg)
    if key == "health/ready":
        return _ready(database_path)
    if key == "meta":
        return _meta(session_token, cfg)
    if key == "csrf":
        return _json(200, {"csrf": session_token})
    return _handle_resource(key, ident, method, body, query, database_path)


def _json(status, payload, headers=None):
    return {"status": status, "body": payload, "headers": headers or {}}


def _live(cfg):
    import sys

    return _json(
        200,
        {
            "status": "alive",
            "runtime": "python",
            "version": sys.version.split()[0],
            "release": cfg.build_marker,
        },
    )


def _ready(database_path):
    result = db.probe(database_path)
    if result["ok"]:
        return _json(200, {"status": "ready", "database": database_path})
    return _json(503, {"status": "unavailable", "error": result.get("error", "")})


def _meta(session_token, cfg):
    import sys

    return _json(
        200,
        {
            "release": cfg.build_marker,
            "runtime": {
                "name": "python",
                "version": sys.version.split()[0],
                "server": "waitress",
            },
            "csrf": session_token,
        },
    )


def _handle_resource(key, ident, method, body, query, database_path):
    if key == "projects":
        if ident is None:
            if method == "GET":
                return _with_db(database_path, lambda conn: _list_projects(conn, query))
            if method == "POST":
                return _with_db(database_path, lambda conn: _create_project(conn, body))
        pid = _parse_id(ident)
        if pid is None:
            return _json(400, {"error": "invalid id"})
        if method == "GET":
            return _with_db(database_path, lambda conn: _get_project(conn, pid))
        if method == "PATCH":
            return _with_db(
                database_path, lambda conn: _update_project(conn, pid, body)
            )
        if method == "DELETE":
            return _with_db(database_path, lambda conn: _delete_project(conn, pid))

    if key == "tasks":
        if ident is None:
            if method == "GET":
                return _with_db(database_path, lambda conn: _list_tasks(conn, query))
            if method == "POST":
                return _with_db(database_path, lambda conn: _create_task(conn, body))
        pid = _parse_id(ident)
        if pid is None:
            return _json(400, {"error": "invalid id"})
        if method == "GET":
            return _with_db(database_path, lambda conn: _get_task(conn, pid))
        if method == "PATCH":
            return _with_db(database_path, lambda conn: _update_task(conn, pid, body))
        if method == "DELETE":
            return _with_db(database_path, lambda conn: _delete_task(conn, pid))

    return _json(404, {"error": "not found"})


def _with_db(database_path, fn):
    try:
        conn = db.open_db(database_path)
    except (OSError, sqlite3.Error) as exc:
        return _json(503, {"error": "database unavailable", "detail": str(exc)})
    try:
        db.apply_schema(conn)
        db.seed(conn)
        return fn(conn)
    except sqlite3.Error as exc:
        return _json(503, {"error": "database unavailable", "detail": str(exc)})
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _parse_id(value):
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    parsed = int(text)
    return parsed if parsed > 0 else None


def _project_exists(conn, pid):
    return (
        conn.execute("SELECT 1 FROM project WHERE id = ?", (pid,)).fetchone()
        is not None
    )


def _nonblank_string(value, maxlen, name, errors):
    if value is None:
        return None
    if not isinstance(value, str):
        errors[name] = "must be a string"
        return None
    if len(value) > maxlen:
        errors[name] = "must be at most %d characters" % maxlen
        return None
    stripped = value.strip()
    if not stripped:
        errors[name] = "%s is required" % name
        return None
    return stripped


def _opt_string(value, maxlen, name, errors):
    if value is None:
        return None
    if not isinstance(value, str):
        errors[name] = "must be a string"
        return None
    if len(value) > maxlen:
        errors[name] = "must be at most %d characters" % maxlen
        return None
    return value


def _enum(value, allowed, default, name, errors):
    if value is None:
        return default
    if value not in allowed:
        errors[name] = "invalid value"
        return None
    return value


def _list_projects(conn, query):
    rows = conn.execute("SELECT * FROM project ORDER BY id").fetchall()
    projects = [_project_json(row, conn) for row in rows]
    return _json(200, {"projects": projects, "count": len(projects)})


def _project_json(row, conn):
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM task WHERE project_id = ?", (row["id"],)
    ).fetchone()["n"]
    open_count = conn.execute(
        "SELECT COUNT(*) AS n FROM task WHERE project_id = ? AND status != 'done'",
        (row["id"],),
    ).fetchone()["n"]
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "task_count": total,
        "open_count": open_count,
    }


def _get_project(conn, pid):
    row = conn.execute("SELECT * FROM project WHERE id = ?", (pid,)).fetchone()
    if row is None:
        return _json(404, {"error": "Project not found"})
    tasks = conn.execute(
        "SELECT t.*, p.name AS project_name FROM task t"
        " JOIN project p ON p.id = t.project_id WHERE t.project_id = ? ORDER BY t.id",
        (pid,),
    ).fetchall()
    return _json(
        200,
        {"project": _project_json(row, conn), "tasks": [_task_json(t) for t in tasks]},
    )


def _create_project(conn, body):
    errors = {}
    name = _nonblank_string(body.get("name"), 120, "name", errors)
    description = _opt_string(body.get("description"), 1000, "description", errors)
    status = _enum(body.get("status"), db.PROJECT_STATUSES, "active", "status", errors)
    if errors:
        return _json(400, {"error": "validation failed", "fields": errors})
    ts = db.now_utc()
    cur = conn.execute(
        "INSERT INTO project (name, description, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (name, description or "", status, ts, ts),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM project WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return _json(201, {"project": _project_json(row, conn)})


def _update_project(conn, pid, body):
    row = conn.execute("SELECT * FROM project WHERE id = ?", (pid,)).fetchone()
    if row is None:
        return _json(404, {"error": "Project not found"})
    errors = {}
    updates = {}
    present = set(body.keys()) & {"name", "description", "status"}
    if not present:
        return _json(400, {"error": "no updatable fields provided"})
    if "name" in present:
        name = _nonblank_string(body.get("name"), 120, "name", errors)
        if name is not None:
            updates["name"] = name
    if "description" in present:
        description = _opt_string(body.get("description"), 1000, "description", errors)
        if description is not None:
            updates["description"] = description
    if "status" in present:
        status = _enum(body.get("status"), db.PROJECT_STATUSES, None, "status", errors)
        if status is not None:
            updates["status"] = status
    if errors:
        return _json(400, {"error": "validation failed", "fields": errors})
    _apply_updates(conn, "project", updates, pid)
    conn.commit()
    row = conn.execute("SELECT * FROM project WHERE id = ?", (pid,)).fetchone()
    return _json(200, {"project": _project_json(row, conn)})


def _delete_project(conn, pid):
    row = conn.execute("SELECT * FROM project WHERE id = ?", (pid,)).fetchone()
    if row is None:
        return _json(404, {"error": "Project not found"})
    conn.execute("DELETE FROM project WHERE id = ?", (pid,))
    conn.commit()
    return _json(204, None)


def _list_tasks(conn, query):
    where = []
    params = []
    q = query.get("q")
    if q is not None:
        if not isinstance(q, str):
            return _json(400, {"error": "invalid q parameter"})
        like = "%%%s%%" % db.escape_like(q)
        where.append("(t.title LIKE ? ESCAPE '\\' OR t.description LIKE ? ESCAPE '\\')")
        params += [like, like]
    status = query.get("status")
    if status is not None:
        if status not in db.TASK_STATUSES:
            return _json(
                400, {"error": "invalid status filter", "fields": {"status": status}}
            )
        where.append("t.status = ?")
        params.append(status)
    priority = query.get("priority")
    if priority is not None:
        if priority not in db.TASK_PRIORITIES:
            return _json(
                400,
                {"error": "invalid priority filter", "fields": {"priority": priority}},
            )
        where.append("t.priority = ?")
        params.append(priority)
    project_id = _filter_project_id(query)
    if project_id is None:
        return _json(
            400,
            {
                "error": "invalid project_id filter",
                "fields": {"project_id": query.get("project_id")},
            },
        )
    if project_id is not False:
        where.append("t.project_id = ?")
        params.append(project_id)
    sql = (
        "SELECT t.*, p.name AS project_name FROM task t"
        " JOIN project p ON p.id = t.project_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.id"
    rows = conn.execute(sql, params).fetchall()
    tasks = [_task_json(row) for row in rows]
    return _json(200, {"tasks": tasks, "count": len(tasks)})


def _filter_project_id(query):
    value = query.get("project_id")
    if value is None:
        return False
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    parsed = int(text)
    return parsed if parsed > 0 else None


def _task_json(row):
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "project_name": row["project_name"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "priority": row["priority"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _create_task(conn, body):
    errors = {}
    pid = _parse_id(
        body.get("project_id") if body.get("project_id") is not None else None
    )
    if pid is None:
        errors["project_id"] = "must be a positive integer"
    elif not _project_exists(conn, pid):
        errors["project_id"] = "unknown project"
    title = _nonblank_string(body.get("title"), 200, "title", errors)
    description = _opt_string(body.get("description"), 1000, "description", errors)
    status = _enum(body.get("status"), db.TASK_STATUSES, "todo", "status", errors)
    priority = _enum(
        body.get("priority"), db.TASK_PRIORITIES, "medium", "priority", errors
    )
    if errors:
        return _json(400, {"error": "validation failed", "fields": errors})
    ts = db.now_utc()
    cur = conn.execute(
        "INSERT INTO task (project_id, title, description, status, priority,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pid, title, description or "", status, priority, ts, ts),
    )
    conn.commit()
    row = conn.execute(
        "SELECT t.*, p.name AS project_name FROM task t"
        " JOIN project p ON p.id = t.project_id WHERE t.id = ?",
        (cur.lastrowid,),
    ).fetchone()
    return _json(201, {"task": _task_json(row)})


def _update_task(conn, pid, body):
    row = conn.execute(
        "SELECT t.*, p.name AS project_name FROM task t"
        " JOIN project p ON p.id = t.project_id WHERE t.id = ?",
        (pid,),
    ).fetchone()
    if row is None:
        return _json(404, {"error": "Task not found"})
    errors = {}
    updates = {}
    present = set(body.keys()) & {
        "project_id",
        "title",
        "description",
        "status",
        "priority",
    }
    if not present:
        return _json(400, {"error": "no updatable fields provided"})
    if "project_id" in present:
        new_pid = _parse_id(body.get("project_id"))
        if new_pid is None or not _project_exists(conn, new_pid):
            errors["project_id"] = "unknown or invalid project"
        else:
            updates["project_id"] = new_pid
    if "title" in present:
        title = _nonblank_string(body.get("title"), 200, "title", errors)
        if title is not None:
            updates["title"] = title
    if "description" in present:
        description = _opt_string(body.get("description"), 1000, "description", errors)
        if description is not None:
            updates["description"] = description
    if "status" in present:
        status = _enum(body.get("status"), db.TASK_STATUSES, None, "status", errors)
        if status is not None:
            updates["status"] = status
    if "priority" in present:
        priority = _enum(
            body.get("priority"), db.TASK_PRIORITIES, None, "priority", errors
        )
        if priority is not None:
            updates["priority"] = priority
    if errors:
        return _json(400, {"error": "validation failed", "fields": errors})
    _apply_updates(conn, "task", updates, pid)
    conn.commit()
    row = conn.execute(
        "SELECT t.*, p.name AS project_name FROM task t"
        " JOIN project p ON p.id = t.project_id WHERE t.id = ?",
        (pid,),
    ).fetchone()
    return _json(200, {"task": _task_json(row)})


def _get_task(conn, pid):
    row = conn.execute(
        "SELECT t.*, p.name AS project_name FROM task t"
        " JOIN project p ON p.id = t.project_id WHERE t.id = ?",
        (pid,),
    ).fetchone()
    if row is None:
        return _json(404, {"error": "Task not found"})
    return _json(200, {"task": _task_json(row)})


def _delete_task(conn, pid):
    row = conn.execute("SELECT 1 FROM task WHERE id = ?", (pid,)).fetchone()
    if row is None:
        return _json(404, {"error": "Task not found"})
    conn.execute("DELETE FROM task WHERE id = ?", (pid,))
    conn.commit()
    return _json(204, None)


def _apply_updates(conn, table, updates, pid):
    assignments = []
    params = []
    for column, value in updates.items():
        assignments.append("%s = ?" % column)
        params.append(value)
    assignments.append("updated_at = ?")
    params.append(db.now_utc())
    params.append(pid)
    conn.execute(
        "UPDATE %s SET %s WHERE id = ?" % (table, ", ".join(assignments)), params
    )
