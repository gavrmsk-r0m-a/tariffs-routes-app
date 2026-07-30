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
import traceback
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
PAGES = (
    "/", "/dashboard", "/routes", "/tariffs", "/phones", "/companies",
    "/provider-changes", "/admin/company-routing-settings", "/admin/server-priorities",
)


class SmokeFailure(RuntimeError):
    def __init__(self, path: str, message: str, status: str | None = None):
        super().__init__(message)
        self.path = path
        self.status = status


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


def _existing_currency(database_url: str) -> tuple[int, int]:
    conn = connect_postgres(database_url)
    try:
        row = conn.execute("SELECT id FROM currencies ORDER BY id LIMIT 1").fetchone()
        if row is None:
            raise SmokeFailure("/admin/currency-rates/upsert", "no existing currency is available for rate smoke")
        currency_id = int(row["id"])
        count = conn.execute(
            "SELECT COUNT(*) AS value FROM currency_rates WHERE currency_id = %s",
            (currency_id,),
        ).fetchone()
        return currency_id, int(count["value"])
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

    try:
        response_body = b"".join(application(environ, start_response))
    except Exception as exc:
        raise SmokeFailure(path, f"{type(exc).__name__}: {exc}") from exc
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
        "HLR_MODE": "demo",
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
        raise SmokeFailure("/login", "login page did not return the expected content", status)

    status, headers, _ = wsgi_request(app, "/routes")
    if status != "303 See Other" or _header(headers, "Location") != "/login":
        raise SmokeFailure("/routes", "unauthenticated request did not redirect to /login", status)

    status, headers, _ = wsgi_request(
        app, "/login", method="POST", data={"username": USERNAME, "password": password}
    )
    if status != "303 See Other" or _header(headers, "Location") != "/routes":
        raise SmokeFailure("/login", "test-user login did not redirect to /routes", status)
    set_cookie = _header(headers, "Set-Cookie")
    if not set_cookie:
        raise SmokeFailure("/login", "test-user login did not return an auth cookie", status)
    cookie = set_cookie.split(";", 1)[0]

    checked = []
    for path in PAGES:
        status, _, body = wsgi_request(app, path, cookie=cookie)
        if not status.startswith("200 ") or not body:
            raise SmokeFailure(path, "authenticated page did not return a non-empty 200 response", status)
        checked.append(path)
    currency_id, rate_count_before = _existing_currency(database_url)
    status, headers, _ = wsgi_request(
        app,
        "/admin/currency-rates/upsert",
        method="POST",
        data={"currency_id": str(currency_id), "rate_to_eur": "1.01"},
        cookie=cookie,
    )
    if status != "303 See Other" or _header(headers, "Location") != "/admin/currency-rates":
        raise SmokeFailure(
            "/admin/currency-rates/upsert",
            "currency-rate update did not redirect to /admin/currency-rates",
            status,
        )
    _, rate_count_after = _existing_currency(database_url)
    if rate_count_after != rate_count_before + 1:
        raise SmokeFailure(
            "/admin/currency-rates/upsert",
            "currency-rate update did not append exactly one currency_rates row",
            status,
        )
    checked.append("/admin/currency-rates/upsert (authenticated POST)")
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
        short_traceback = sanitize_text(traceback.format_exc(limit=4), args.database_url).replace(args.auth_secret, "***")
        root = exc.__cause__ or exc
        failure = {
            "status": "failed",
            "path": getattr(exc, "path", None),
            "http_status": getattr(exc, "status", None),
            "error_type": type(root).__name__,
            "error": message,
            "traceback": short_traceback,
        }
        print(json.dumps(failure, indent=2, sort_keys=True) if args.format == "json" else f"{failure['path'] or 'startup'}: {failure['error_type']}: {message}\n{short_traceback}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True) if args.format == "json" else "PostgreSQL full-app WSGI smoke: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
