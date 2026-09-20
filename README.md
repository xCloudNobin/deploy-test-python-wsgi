# Python / WSGI Taskboard

A meaningful **plain Python** application for the xCloud app-compatibility suite: a
project/task board built entirely with the Python standard library and the
**Waitress** production WSGI server — no Flask, no Django, no ORM. Persistence
is **SQLite** through the standard-library `sqlite3` module, the JSON API is a
hand-written router over the WSGI `environ`, and the SPA client is a small
vanilla-JavaScript page rendered through `textContent` (output is escaped).

It is a production-process fixture, not a success-page shell: every workflow
reads and writes through parameterized SQL, all input is validated server-side
with meaningful error payloads, and `scripts/verify.sh` exercises the real
production process end to end.

## Feature summary

- A pure-Python WSGI callable at `wsgi.py:application` served by the Waitress
  production server (`python -m waitress --listen=... wsgi:application`).
- Projects and tasks with status/priority, search (`q`), and status/priority
  filters over a JSON API consumed by a small DOM-rendered client.
- Stateless cookie sessions + CSRF: cookie-authenticated mutations must present
  the per-session token (`X-CSRF-Token` header or `_csrf` body field); reads are
  unauthenticated and CSRF failures return **403**.
- Validated CRUD: blank/over-long/non-string fields, invalid status/priority,
  malformed JSON, missing references, invalid ids and not-found resources all
  return meaningful JSON errors (400/404/405/503).
- Parameterized SQLite statements everywhere (LIKE wildcards escaped) and all
  output rendered via `textContent` only — no `innerHTML` with user data.
- Idempotent schema setup (`CREATE TABLE IF NOT EXISTS`) and repeatable seed
  data guarded by a one-time seed flag.
- Persistence: explicit SQLite file (`DATA_DIR`/`DATABASE_PATH`); the app never
  stores permanent state in an ephemeral release directory.
- `/api/health/live` (process alive) and `/api/health/ready` (opens the
  configured SQLite path, ensures the schema and performs a real write + read;
  **503** while the database is unavailable). Readiness is a genuine dependency
  probe, not a static marker.
- Non-sensitive release marker: `scripts/build.sh` writes `VERSION` (git SHA by
  default); the marker is served by `/api/meta`, `/api/health/live` and the UI
  footer.
- Logs go to stdout/stderr (one line per request from the WSGI layer plus
  Waitress's own logs); no credentials are ever logged.

## Runtime and dependencies

- Python **3.10+** required; validated on **3.12.3**.
- One pinned third-party dependency: **Waitress 3.0.2** (production WSGI
  server) in `requirements.txt`, installed into a local `.venv`.
- SQLite is the Python standard-library `sqlite3` module (`sqlite 3.45.1` in
  this verification).

Runtime versions (this verification):

| Component | Version |
|-----------|---------|
| Python    | 3.12.3 |
| SQLite    | 3.45.1 (stdlib `sqlite3`) |
| WSGI server | Waitress 3.0.2 |

## Quick start (development)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt    # waitress==3.0.2
cp .env.example .env                         # review and adjust
.venv/bin/python -m waitress --listen=127.0.0.1:8080 wsgi:application
```

Open http://localhost:8080 — the seeder has already created two demo projects
and a few tasks on first boot. The `.env` file is not auto-loaded by the
application; the process supervisor (or shell) exports the variables. That is
how `scripts/smoke.sh` drives arbitrary database paths and ports.

## Production start

```bash
scripts/build.sh            # writes VERSION + runtime preflight + compile
export PORT=8080 BIND_HOST=0.0.0.0 DATA_DIR=./data \
       DATABASE_PATH=./data/taskboard.db BUILD_MARKER=release-1
.venv/bin/python -m waitress --listen="$BIND_HOST:$PORT" --threads=4 \
    wsgi:application
```

- Binds to `BIND_HOST:PORT` (defaults **0.0.0.0:8080**). Waitress serves the
  `wsgi` module's `application` callable in-process.
- Run from the repository root so `src/`, `static/` and `VERSION` resolve.
- Logs go to stdout/stderr; Waitress shuts down cleanly on SIGTERM/SIGINT.
- Use a process supervisor (systemd, an xCloud process manager, etc.) to keep it
  running and to mount a **persistent volume** at `DATA_DIR`/`DATABASE_PATH`.

### Routing

`wsgi.py` reads `PATH_INFO` and routes to:

1. `/api/...` → the JSON API router;
2. `/app.js` and `/style.css` → static assets from `static/` (any other
   dotted filename returns 404);
3. anything else → the SPA shell, so nested client routes (e.g. `/project/3`)
   work and deep-link into the UI.

## Sessions and CSRF

Sessions are stateless: the first request receives an HttpOnly, SameSite=Lax
cookie (`wsgi_tb_session`) containing a random token. Every mutating API request
(POST/PATCH/PUT/DELETE) must echo that token via the `X-CSRF-Token` header or a
`_csrf` field in the JSON body. The SPA shell embeds the token on every page
load; `/api/meta` also returns it so automated clients can bootstrap a session
from a cookie jar. Cross-site forms cannot forge changes because they cannot
read the token from the HttpOnly cookie. Mutations without a valid token are
rejected with **403**.

## Health and readiness

| Endpoint | Meaning |
|----------|---------|
| `GET /api/health/live`  | Process is alive (always 200 while serving; DB not touched). |
| `GET /api/health/ready` | Opens the configured SQLite path, ensures the schema, performs a real write + read cycle; **503** with `status:"unavailable"` when the database is unavailable. |

`scripts/smoke.sh` proves readiness is a real probe: it starts the production
process against a database path that cannot be created (parent is a regular
file), observes `ready` return 503 while `live` stays 200 and the static UI
still serves, then repairs the path and verifies readiness recovers.

## Environment variables

See `.env.example` for the full commented list.

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `PORT` | no | `8080` | bind port |
| `BIND_HOST` | no | `0.0.0.0` | bind address |
| `DATA_DIR` | no | `<repo>/data` | base data directory |
| `DATABASE_PATH` | no | `<DATA_DIR>/taskboard.db` | **persistent SQLite path** |
| `BUILD_MARKER` | no | git SHA | release marker in `/api/meta`, `/api/health/live` and the UI footer |
| `BASE_PATH` | no | (empty) | URL prefix when served under a sub-path |

No credentials or secrets are committed or required.

## Persistence

Data lives in the SQLite file at `DATABASE_PATH`, which defaults under
`DATA_DIR` (gitignored). For redeploys that reuse or replace the release
directory, mount a persistent volume at `DATA_DIR`/`DATABASE_PATH` so the file
survives. `scripts/smoke.sh` proves it: it creates a "PERSIST" survivor task
over HTTP, stops the production process, restarts it on the **same database
path**, and verifies the record is still served with stable task counts.

Because the app opens a fresh SQLite connection per request, a database that
becomes unavailable is immediately reported by readiness (**503**) and the data
routes (**503**), and readiness recovers as soon as the path is valid again.

## Schema

Created idempotently (`CREATE TABLE IF NOT EXISTS`) by `src/db.py`:

- `project` — id, name (TEXT), description, status (`active|archived`),
  timestamps.
- `task` — id, `project_id` FK (`ON DELETE CASCADE`), title, description,
  status (`todo|in_progress|done`), priority (`low|medium|high`), timestamps.
- `seed_flag` — marks the one-time seed as applied.
- `heartbeat` — backing table for the readiness write/read probe.

Seeding is repeatable: the second and subsequent opens never add rows (the test
suite asserts seed idempotency).

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health/live` | liveness |
| GET | `/api/health/ready` | readiness (DB probe) |
| GET | `/api/meta` | release marker + runtime versions + CSRF token |
| GET | `/api/csrf` | per-session CSRF token |
| GET/POST | `/api/projects` | list / create projects |
| GET/PATCH/DELETE | `/api/projects/:id` | read / update / delete a project |
| GET/POST | `/api/tasks` | list (filters `q`, `status`, `priority`, `project_id`) / create tasks |
| GET/PATCH/DELETE | `/api/tasks/:id` | read / update / delete a task |

Mutating methods require the session CSRF token. The UI at `/` (and nested
paths like `/project/<id>`) consumes the same JSON API.

## Automated verification

```bash
scripts/verify.sh
```

Provisioning a pinned venv if needed, it runs, in order:

1. `scripts/build.sh` — writes the `VERSION` marker, preflights the runtime
   (Python version + pinned Waitress) and compiles every Python source.
2. `tests/run_tests.py` — dependency-free suite against **real SQLite**: CRUD,
   search/filter, LIKE-wildcard escaping, validation negatives (blank/over-long/
   mistyped fields, invalid status/priority, malformed JSON, missing project,
   empty PATCH, 404s), CSRF enforcement (403), schema/seed idempotency,
   restart-style persistence, cascade delete, and readiness that drops to 503
   when the database path is unusable.
3. `scripts/smoke.sh` — the real production process (`waitress` ->
   `wsgi:application`): session/CSRF bootstrap, liveness/readiness, CRUD over
   HTTP, search/status/priority filters, release marker, negative cases,
   stop → restart persistence, database-unavailable readiness (503) while
   liveness stays 200, and recovery once the database is available again.
   Every server it starts is terminated on success **and** failure.

Last full run: **69 unit/integration checks** and **57 smoke checks**, all
passing, against the exact commit listed in `VERSION`.

## Repository layout

```
wsgi.py         production WSGI callable + request router/static serving
src/            config.py (env), db.py (schema/seed/probe/LIKE), security.py
                (cookie session + CSRF), api.py (routing + validated CRUD),
                ui.py (SPA shell)
static/         app.js, style.css (vanilla client; escaped rendering)
tests/          run_tests.py (dependency-free unit/integration suite)
scripts/        build.sh (marker + preflight + compile), smoke.sh (production
                check), verify.sh (full verification)
.env.example    documented, safe configuration template
VERSION         non-sensitive release marker written by scripts/build.sh
```

## License

MIT — see [LICENSE](LICENSE). This fixture is part of the MIT-licensed
[xCloud app-compatibility suite](https://github.com/xCloudNobin/app-compatibility).