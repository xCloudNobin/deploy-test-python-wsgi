"""Environment-driven configuration for the plain-WSGI taskboard.

No web framework and no secrets: every knob is read from the process
environment, which the supervisor exports. Defaults are safe for local
development and for the verify/smoke harness.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass(frozen=True)
class Config:
    port: int
    bind_host: str
    data_dir: str
    database_path: str
    build_marker: str
    base_path: str

    @property
    def static_dir(self) -> str:
        return os.path.join(ROOT, "static")


def load_config(environ=None):
    if environ is None:
        environ = os.environ
    data_dir = environ.get("DATA_DIR") or os.path.join(ROOT, "data")
    return Config(
        port=int(environ.get("PORT") or "8080"),
        bind_host=environ.get("BIND_HOST") or "0.0.0.0",
        data_dir=data_dir,
        database_path=environ.get("DATABASE_PATH")
        or os.path.join(data_dir, "taskboard.db"),
        build_marker=environ.get("BUILD_MARKER") or _default_marker(),
        base_path=(environ.get("BASE_PATH") or "").rstrip("/"),
    )


def _default_marker() -> str:
    try:
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as fh:
            marker = fh.read().strip()
            if marker:
                return marker
    except OSError:
        pass
    return "dev"
