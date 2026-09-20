"""Production WSGI entry point for the plain-WSGI taskboard.

Serve with the Waitress production server (see README):

    .venv/bin/python -m waitress --listen="$BIND_HOST:$PORT" wsgi:application

The application keeps no in-memory database: every request opens a fresh SQLite
connection to the configured DATABASE_PATH, so liveness keeps answering while
an unavailable or broken database makes data routes and readiness report 503.
"""

from __future__ import annotations

import json
import os
import sys

from src import api
from src import config as cfg_mod
from src import security
from src import ui

CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}

STATUS_TEXT = {
    200: "OK",
    201: "Created",
    204: "No Content",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    503: "Service Unavailable",
}


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    raw_path = environ.get("PATH_INFO", "/")
    query = _parse_query(environ.get("QUERY_STRING", ""))
    cfg = cfg_mod.load_config(os.environ)

    if cfg.base_path and raw_path.startswith(cfg.base_path):
        raw_path = raw_path[len(cfg.base_path) :] or "/"

    cookies = security.parse_cookies(environ.get("HTTP_COOKIE"))
    session_token = cookies.get(security.SESSION_COOKIE)
    set_cookie = None
    if not session_token:
        session_token = security.new_token()
        set_cookie = security.set_cookie_header(session_token)

    if raw_path.startswith("/api/") or raw_path == "/api":
        status, payload, headers = _serve_api(
            method, raw_path, query, session_token, cfg, environ
        )
    elif _static_route(raw_path):
        status, payload, headers = _serve_static(cfg, raw_path)
    else:
        status, payload, headers = (
            200,
            ui.render_shell(cfg, session_token).encode("utf-8"),
            {
                "Content-Type": "text/html; charset=utf-8",
            },
        )

    headers = dict(headers)
    if set_cookie:
        headers["Set-Cookie"] = set_cookie
    status_line = "%d %s" % (status, STATUS_TEXT.get(status, "Unknown"))
    start_response(status_line, [(k, v) for k, v in headers.items()])
    _log(environ, method, raw_path, status)
    return [payload]


def _serve_api(method, raw_path, query, session_token, cfg, environ):
    segments = [part for part in raw_path.split("/") if part]
    header_csrf = environ.get("HTTP_X_CSRF_TOKEN", "")
    body, json_broken = _read_json_body(environ)
    result = api.api_dispatch(
        method,
        segments,
        query,
        body,
        json_broken,
        header_csrf,
        session_token,
        cfg.database_path,
        cfg,
    )
    payload = result["body"]
    if payload is None:
        return result["status"], b"", result["headers"]
    serialized = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    headers.update(result["headers"])
    return result["status"], serialized, headers


def _read_json_body(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return None, False
    raw = environ["wsgi.input"].read(length)
    if not raw.strip():
        return None, False
    try:
        return json.loads(raw.decode("utf-8")), False
    except (ValueError, UnicodeDecodeError):
        return api.MALFORMED, True


def _serve_static(cfg, raw_path):
    basename = os.path.basename(raw_path)
    allowed = {"app.js", "style.css"}
    if basename not in allowed:
        return 404, b"", {"Content-Type": "text/plain; charset=utf-8"}
    file_path = os.path.join(cfg.static_dir, basename)
    try:
        with open(file_path, "rb") as fh:
            payload = fh.read()
    except OSError:
        return 404, b"", {"Content-Type": "text/plain; charset=utf-8"}
    ext = os.path.splitext(basename)[1]
    return (
        200,
        payload,
        {"Content-Type": CONTENT_TYPES.get(ext, "application/octet-stream")},
    )


def _static_route(raw_path):
    if raw_path in ("/app.js", "/style.css"):
        return True
    return "." in os.path.basename(raw_path)


def _parse_query(qs):
    parsed = {}
    for part in qs.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        key = _url_decode(key)
        value = _url_decode(value)
        if key and key not in parsed:
            parsed[key] = value
    return parsed


def _url_decode(text):
    from urllib.parse import unquote_plus

    return unquote_plus(text)


def _log(environ, method, path, status):
    print(
        "%s %s %s %d" % (environ.get("REMOTE_ADDR", "-"), method, path, status),
        flush=True,
    )
