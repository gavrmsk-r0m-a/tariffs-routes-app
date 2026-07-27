#!/usr/bin/env python3
"""Smoke-test the real TeleRoute WSGI application against PostgreSQL.

The caller must migrate the demo SQLite database first.  This script does not
create or mutate schema: PostgreSQL schema ownership remains with migrations.
It adds one randomly-passworded CI-only user, imports ``app.server.app`` with
the guarded PostgreSQL environment enabled, and exercises authenticated pages.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect_postgres  # noqa: E402
from app.repository import hash_password  # noqa: E402
from app.security import validate_auth_secret  # noqa: E402
from scripts.postgres_backup import sanitize_database_url, sanitize_text  # noqa: E402

USERNAME = "stage68a-full-app-ci"
PAGES = ("/routes", "/tariffs", "/phones", "/companies", "/provider-changes")


def _install_test_user(database_url: str, password: str) -> None:
    password_hash, password_salt = hash_password(password)
    conn = connect_postgres(database_url)
    try:
        conn.execute(
            """
            INSERT INTO users(username, display_name, role_key, role, must_change_password,
                              is_active, password_hash, password_salt)
            VALUES (%s, %s, 'admin', 'admin', false, true, %s, %s)
            ON CONFLICT(username) DO UPDATE SET
                display_name = excluded.display_name, role_key = excluded.role_key,
                role = excluded.role, must_change_password = false, is_active = true,
                password_hash = excluded.password_hash, password_salt = excluded.password_salt,
                updated_at = CURRENT_TIMESTAMP
            """,
            (USERNAME, "Stage 68A CI user", password_hash, password_salt),
        )
        conn.commit()
    finally:
        conn.close()


def wsgi_request(application, path: str, *, method: str = "GET", data=None, cookie: str | None = None):
    body = urlencode(data or {}).encode("utf-8")
    environ: dict[str, object] = {}
    setup_testing_defaults(environ)
    environ.update({
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": "application/x-www-form-urlencoded",
        "wsgi.input": io.BytesIO(body),
        "REMOTE_ADDR": "127.0.0.1",
        "HTTP_USER_AGENT": "TeleRoute-Stage68A-Smoke",
    })
    if cookie:
        environ["HTTP_COOKIE"] = cookie
    captured: dict[str, object] = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = headers

    response_body = b"".join(application(environ, start_response))
    return str(captured["status"]), list(captured["headers"]), response_body


def _header(headers, name: str) -> str | None:
    return next((value for key, value in headers if key.lower() == name.lower()), None)


def run_smoke(database_url: str, auth_secret: str) -> dict[str, object]:
    runtime_env = {
        "DB_BACKEND": "postgres",
        "POSTGRES_RUNTIME_ENABLED": "1",
        "DATABASE_URL": database_url,
        "MVP_PRODUCTION_SECURITY": "1",
        "MVP_AUTH_SECRET": auth_secret,
    }
    errors = validate_auth_secret(runtime_env)
    if errors:
        raise ValueError("invalid CI auth secret: " + "; ".join(errors))
    os.environ.update(runtime_env)

    password = secrets.token_urlsafe(24)
    _install_test_user(database_url, password)

    # Import only after setting the environment: app.server intentionally loads
    # its immutable runtime DB_CONFIG and cookie secret at module import time.
    from app.server import app

    status, _, body = wsgi_request(app, "/login")
    if not status.startswith("200 ") or b"TeleRoute" not in body:
        raise RuntimeError(f"GET /login failed with {status}")

    status, headers, _ = wsgi_request(app, "/routes")
    if not status.startswith("302 ") or _header(headers, "Location") != "/login":
        raise RuntimeError("unauthenticated GET /routes did not redirect to /login")

    status, headers, _ = wsgi_request(
        app, "/login", method="POST", data={"username": USERNAME, "password": password}
    )
    if not status.startswith("302 ") or _header(headers, "Location") != "/routes":
        raise RuntimeError(f"test-user login failed with {status}")
    set_cookie = _header(headers, "Set-Cookie")
    if not set_cookie:
        raise RuntimeError("test-user login did not return an auth cookie")
    cookie = set_cookie.split(";", 1)[0]

    checked = []
    for path in PAGES:
        status, _, body = wsgi_request(app, path, cookie=cookie)
        if not status.startswith("200 ") or not body:
            raise RuntimeError(f"authenticated GET {path} failed with {status}")
        checked.append(path)
    return {
        "status": "ok",
        "backend": "postgres",
        "runtime_guard_enabled": True,
        "wsgi_callable": "app.server.app",
        "checked_paths": ["/login", "/routes (unauthenticated)", *checked],
        "database_url_sanitized": sanitize_database_url(database_url),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--auth-secret", required=True)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        result = run_smoke(args.database_url, args.auth_secret)
    except Exception as exc:
        message = sanitize_text(str(exc), args.database_url).replace(args.auth_secret, "***")
        print(json.dumps({"status": "failed", "error": message}) if args.format == "json" else message, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True) if args.format == "json" else "PostgreSQL full-app WSGI smoke: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
