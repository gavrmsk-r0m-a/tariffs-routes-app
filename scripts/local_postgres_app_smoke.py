#!/usr/bin/env python3
"""Exercise local-dev login and core pages through the real WSGI callable."""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.postgres_full_app_smoke import PAGES, SmokeFailure, _header, wsgi_request
from scripts.setup_local_postgres import DEFAULT_USERNAME, validate_local_database_url


def run_smoke(database_url: str, username: str, password: str) -> None:
    validate_local_database_url(database_url)
    os.environ.update({
        "DB_BACKEND": "postgres", "POSTGRES_RUNTIME_ENABLED": "1",
        "DATABASE_URL": database_url, "MVP_PRODUCTION_SECURITY": "1",
        "MVP_AUTH_SECRET": "local-smoke-" + secrets.token_urlsafe(32),
    })
    from app.server import app

    status, _, _ = wsgi_request(app, "/login")
    if not status.startswith("200 "):
        raise SmokeFailure("/login", "login page failed", status)
    status, headers, _ = wsgi_request(
        app, "/login", method="POST", data={"username": username, "password": password}
    )
    cookie_header = _header(headers, "Set-Cookie")
    if status != "303 See Other" or not cookie_header:
        raise SmokeFailure("/login", "local-dev login failed", status)
    cookie = cookie_header.split(";", 1)[0]
    for path in PAGES:
        status, _, body = wsgi_request(app, path, cookie=cookie)
        if not status.startswith("200 ") or not body:
            raise SmokeFailure(path, "authenticated page failed", status)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", required=True)
    args = parser.parse_args(argv)
    try:
        run_smoke(args.database_url, args.username, args.password)
    except Exception as exc:
        print(f"Local PostgreSQL app smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("Local PostgreSQL app smoke: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
