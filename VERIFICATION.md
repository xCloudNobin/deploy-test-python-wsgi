# Verification record

Fixture: **deploy-test-python-wsgi** (plain Python WSGI + standard-library
SQLite + cookie session/CSRF, served by the Waitress production WSGI server).

Verified at the PR head (commits up to `7a38b26`; implementation snapshot
`8d56712`). Status: **local-verified** — no live xCloud deployment
qualification was performed.

## Command

```bash
scripts/verify.sh          # creates .venv, pins waitress==3.0.2, then:
scripts/build.sh           # VERSION marker + runtime preflight + compile + JS check
.venv/bin/python tests/run_tests.py
scripts/smoke.sh           # real production process (waitress -> wsgi:application)
```

Transition to a real production process — no development server is exercised.

## Environment

| Component | Version |
|-----------|---------|
| Python    | 3.12.3 |
| SQLite    | 3.45.1 (standard-library `sqlite3`) |
| WSGI server | Waitress 3.0.2 (pinned in `requirements.txt`) |
| Dependencies | waitress only (the app itself is standard library) |

## Steps and outcome (exit 0 = all passed)

1. `scripts/build.sh` — writes the `VERSION` release marker (git short SHA),
   preflights the runtime (Python >= 3.10 + stdlib `sqlite3` + pinned Waitress),
   compiles every Python source and syntax-checks the client JS: **passed**
   (`release marker: 7a38b26` resolved to the git short SHA at build time).
2. Unit/integration suite — `.venv/bin/python tests/run_tests.py` against real
   SQLite through the exact production `api_dispatch` router: **69 checks,
   0 failed** (CRUD, search/filter, LIKE-wildcard escaping, validation
   negatives, CSRF enforcement 403, schema/seed idempotency, restart-style
   persistence, cascade delete, database-unavailable readiness 503).
3. Production smoke — `scripts/smoke.sh` against the real process
   `waitress --listen=127.0.0.1:<port> wsgi:application`: **57 checks,
   0 failed**, including:
   - session + CSRF bootstrap with a cookie jar; mutations without the token
     are rejected with 403;
   - liveness/readiness, release marker, static UI + assets, nested UI route,
     unknown static/API 404, unhandled method 405;
   - project/task CRUD over HTTP with CSRF, read-back, search and
     status/priority/project filters;
   - negative/validation cases (400) and not-found (404);
   - sqlite file written to disk at the configured persistent path;
   - persistence survivor survives a signal-based stop → restart on the
     **same** SQLite path with stable counts;
   - database-unavailable readiness: `503` with `status:"unavailable"` while
     liveness stays `200`, the API degrades to 503 and the static UI keeps
     serving; readiness recovers once the database path is valid again.
   - every server started is terminated on success and on failure (trap EXIT).

## Notes

- The marker runs above resolved to the git short SHA of the current checkout;
  the committed `VERSION` is the implementation snapshot `8d56712`. A
  deployment re-running `scripts/build.sh` regenerates it from the deployed
  commit.
- Readiness is a real probe: it opens a fresh connection, ensures the schema and
  performs a write + read cycle; the outage tests prove it fails (503) — a
  static success marker would not.
- Sensitive configuration is never committed; only `.env.example` placeholders
  are published.
- No live platform/deployment qualification was performed; this is local
  production verification only.