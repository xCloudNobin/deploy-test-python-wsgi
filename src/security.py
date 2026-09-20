"""Stateless cookie session + CSRF protection for the plain-WSGI taskboard.

A session is a single random token stored in an HttpOnly, SameSite=Lax cookie.
Every mutating request must echo that token in the `X-CSRF-Token` header or the
`_csrf` body field, so a cross-site form cannot forge a mutation even though the
browser legitimately holds the cookie.
"""

from __future__ import annotations

import http.cookies
import secrets

SESSION_COOKIE = "wsgi_tb_session"


def new_token() -> str:
    return secrets.token_hex(32)


def parse_cookies(header: str | None) -> dict:
    if not header:
        return {}
    jar = http.cookies.SimpleCookie()
    jar.load(header)
    return {name: morsel.value for name, morsel in jar.items()}


def set_cookie_header(value: str) -> str:
    jar = http.cookies.SimpleCookie()
    jar[SESSION_COOKIE] = value
    jar[SESSION_COOKIE]["path"] = "/"
    jar[SESSION_COOKIE]["httponly"] = True
    jar[SESSION_COOKIE]["samesite"] = "Lax"
    return jar.output(header="").strip()


def csrf_matches(candidate: str, session_token: str) -> bool:
    if not candidate or not session_token:
        return False
    return secrets.compare_digest(candidate, session_token)
