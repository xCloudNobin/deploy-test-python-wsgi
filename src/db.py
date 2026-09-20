"""SQLite persistence layer built only on the standard library.

Provides the idempotent schema, a one-time repeatable seed, normalized
parameterized queries, the readiness probe and LIKE-wildcard escaping. The
database is opened lazily per request, so a missing/unusable database path is
surfaced by data routes and readiness as 503 while liveness keeps serving.
"""

from __future__ import annotations

import os
import sqlite3
import time

PROJECT_STATUSES = ("active", "archived")
TASK_STATUSES = ("todo", "in_progress", "done")
TASK_PRIORITIES = ("low", "medium", "high")

SEED_PROJECTS = [
    {
        "name": "Launch checklist",
        "description": "Everything that must be true before the release ships.",
        "status": "active",
    },
    {
        "name": "Housekeeping",
        "description": "Small maintenance tasks that keep the workspace tidy.",
        "status": "active",
    },
]

SEED_TASKS = [
    {
        "project": "Launch checklist",
        "title": "Write the release notes",
        "description": "Summarize what changed and how.",
        "status": "todo",
        "priority": "medium",
    },
    {
        "project": "Launch checklist",
        "title": "Run the smoke tests",
        "description": "scripts/verify.sh must pass end to end.",
        "status": "in_progress",
        "priority": "high",
    },
    {
        "project": "Launch checklist",
        "title": "Announce the launch",
        "description": "Tell the world it is live.",
        "status": "todo",
        "priority": "low",
    },
    {
        "project": "Housekeeping",
        "title": "Rotate the access keys",
        "description": "Replace the expiring credentials.",
        "status": "done",
        "priority": "high",
    },
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'todo',
    priority    TEXT NOT NULL DEFAULT 'medium',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seed_flag (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_project ON task(project_id);
CREATE INDEX IF NOT EXISTS idx_task_status ON task(status);
CREATE INDEX IF NOT EXISTS idx_task_priority ON task(priority);
"""


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def open_db(database_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(database_path))
    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(database_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def seed(conn: sqlite3.Connection) -> dict:
    applied = conn.execute("SELECT COUNT(*) AS n FROM seed_flag").fetchone()
    if applied["n"] > 0:
        projects = conn.execute("SELECT COUNT(*) AS n FROM project").fetchone()["n"]
        tasks = conn.execute("SELECT COUNT(*) AS n FROM task").fetchone()["n"]
        return {"seeded": False, "projects": projects, "tasks": tasks}
    ts = now_utc()
    project_ids = {}
    for project in SEED_PROJECTS:
        cur = conn.execute(
            "INSERT INTO project (name, description, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (project["name"], project["description"], project["status"], ts, ts),
        )
        project_ids[project["name"]] = cur.lastrowid
    for task in SEED_TASKS:
        conn.execute(
            "INSERT INTO task (project_id, title, description, status, priority,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                project_ids[task["project"]],
                task["title"],
                task["description"],
                task["status"],
                task["priority"],
                ts,
                ts,
            ),
        )
    conn.execute("INSERT INTO seed_flag (id, applied_at) VALUES (1, ?)", (ts,))
    conn.commit()
    return {"seeded": True, "projects": len(SEED_PROJECTS), "tasks": len(SEED_TASKS)}


def probe(database_path: str) -> dict:
    """Readiness probe: fresh connection, schema, then a real write + read cycle."""
    try:
        conn = open_db(database_path)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        apply_schema(conn)
        ts = int(time.time())
        cur = conn.execute("INSERT INTO heartbeat (ts) VALUES (?)", (ts,))
        row = conn.execute(
            "SELECT ts FROM heartbeat WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        if row is None or int(row["ts"]) != ts:
            return {"ok": False, "error": "heartbeat write/read-back failed"}
        conn.execute("DELETE FROM heartbeat WHERE id = ?", (cur.lastrowid,))
        conn.commit()
        return {"ok": True}
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


_LIKE_ESCAPES = {"\\": "\\\\", "%": "\\%", "_": "\\_"}


def escape_like(term: str) -> str:
    return "".join(_LIKE_ESCAPES.get(ch, ch) for ch in term)
