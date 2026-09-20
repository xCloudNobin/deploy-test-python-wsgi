"""SPA shell renderer.

Serves the single-page UI plus a non-sensitive boot payload (release marker,
runtime and the per-session CSRF token). Static assets live in `static/` and
are served directly by the WSGI router.
"""

from __future__ import annotations

import html
import json
import sys


def render_shell(cfg, session_token) -> str:
    boot = {
        "csrf": session_token,
        "release": cfg.build_marker,
        "runtime": "python %s / waitress" % sys.version.split()[0],
        "base": cfg.base_path,
    }
    boot_json = html.escape(json.dumps(boot), quote=True)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WSGI Taskboard</title>
<link rel="stylesheet" href="{cfg.base_path}/style.css">
</head>
<body>
<script>window.TB_BOOT = {boot_json};</script>
<header class="topbar">
  <h1>WSGI Taskboard</h1>
  <div class="meta"><span class="release">release {html.escape(cfg.build_marker)}</span></div>
</header>
<main id="app"></main>
<footer class="statusbar"><span id="status-text">loading…</span></footer>
<script src="{cfg.base_path}/app.js"></script>
</body>
</html>
"""
