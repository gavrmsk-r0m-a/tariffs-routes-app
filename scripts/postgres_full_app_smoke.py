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
from contextlib import contextmanager
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


@contextmanager
def _isolated_smoke_currency(database_url: str):
    code = f"CI_SMOKE_{secrets.token_hex(6).upper()}"
    conn = connect_postgres(database_url)
    try:
        row = conn.execute(
            """
            INSERT INTO currencies(code, name, symbol, is_active)
            VALUES (%s, %s, %s, true)
            RETURNING id
            """,
            (code, "CI full-app smoke currency", "CI"),
        ).fetchone()
        currency_id = int(row["id"])
        conn.commit()
    finally:
        conn.close()
    try:
        yield currency_id
    finally:
        cleanup = connect_postgres(database_url)
        try:
            cleanup.execute(
                """
                DELETE FROM change_log
                WHERE entity_type = 'currency_rate'
                  AND entity_id IN (SELECT id FROM currency_rates WHERE currency_id = %s)
                """,
                (currency_id,),
            )
            cleanup.execute("DELETE FROM currency_rates WHERE currency_id = %s", (currency_id,))
            cleanup.execute("DELETE FROM currencies WHERE id = %s", (currency_id,))
            cleanup.commit()
        finally:
            cleanup.close()


def _currency_rate_count(database_url: str, currency_id: int) -> int:
    conn = connect_postgres(database_url)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS value FROM currency_rates WHERE currency_id = %s",
            (currency_id,),
        ).fetchone()
        return int(row["value"])
    finally:
        conn.close()


@contextmanager
def _isolated_smoke_dictionaries(database_url: str):
    suffix = secrets.token_hex(6).upper()
    names = {
        "server_a": f"CI_SMOKE_SERVER_A_{suffix}",
        "server_b": f"CI_SMOKE_SERVER_B_{suffix}",
        "server_renamed": f"CI_SMOKE_SERVER_B_RENAMED_{suffix}",
        "project": f"CI_SMOKE_PROJECT_{suffix}",
    }
    conn = connect_postgres(database_url)
    try:
        server_ids = []
        for name in (names["server_a"], names["server_b"]):
            row = conn.execute(
                "INSERT INTO servers(name, is_active) VALUES (%s, true) RETURNING id", (name,)
            ).fetchone()
            server_ids.append(int(row["id"]))
        conn.commit()
    finally:
        conn.close()
    try:
        yield {**names, "server_a_id": server_ids[0], "server_b_id": server_ids[1]}
    finally:
        cleanup = connect_postgres(database_url)
        try:
            project = cleanup.execute("SELECT id FROM projects WHERE name = %s", (names["project"],)).fetchone()
            entity_ids = [*server_ids, *([int(project["id"])] if project else [])]
            if entity_ids:
                cleanup.execute(
                    "DELETE FROM change_log WHERE entity_id = ANY(%s) AND entity_type IN ('servers', 'projects')",
                    (entity_ids,),
                )
            if project:
                cleanup.execute("DELETE FROM projects WHERE id = %s", (project["id"],))
            cleanup.execute("DELETE FROM servers WHERE id = ANY(%s)", (server_ids,))
            cleanup.commit()
        finally:
            cleanup.close()


def _dictionary_value(database_url: str, table: str, entity_id: int) -> str | None:
    if table not in {"servers", "projects"}:
        raise ValueError("unsupported smoke dictionary")
    conn = connect_postgres(database_url)
    try:
        row = conn.execute(f"SELECT name FROM {table} WHERE id = %s", (entity_id,)).fetchone()
        return str(row["name"]) if row else None
    finally:
        conn.close()


@contextmanager
def _isolated_smoke_campaign(database_url: str):
    external_id = f"CI_SMOKE_{secrets.token_hex(6).upper()}"
    conn = connect_postgres(database_url)
    try:
        server = conn.execute("SELECT id FROM servers WHERE is_active IS TRUE ORDER BY id LIMIT 1").fetchone()
        country = conn.execute("SELECT id FROM countries WHERE is_active IS TRUE ORDER BY id LIMIT 1").fetchone()
        if not server or not country:
            raise SmokeFailure("/companies/create", "active server and country are required for campaign smoke")
        yield {"external_id": external_id, "server_id": int(server["id"]), "country_id": int(country["id"])}
    finally:
        try:
            row = conn.execute(
                "SELECT id FROM calling_companies WHERE company_id_external = %s", (external_id,)
            ).fetchone()
            if row:
                company_id = int(row["id"])
                conn.execute(
                    """DELETE FROM change_log
                       WHERE (entity_type = 'calling_company' AND entity_id = %s)
                          OR (entity_type = 'company_routing_setting' AND entity_id IN
                              (SELECT id FROM company_routing_settings WHERE calling_company_id = %s))""",
                    (company_id, company_id),
                )
                conn.execute("DELETE FROM company_routing_settings WHERE calling_company_id = %s", (company_id,))
                conn.execute("DELETE FROM calling_companies WHERE id = %s", (company_id,))
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
    with _isolated_smoke_currency(database_url) as currency_id:
        rate_count_before = _currency_rate_count(database_url, currency_id)
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
        rate_count_after = _currency_rate_count(database_url, currency_id)
        if rate_count_after != rate_count_before + 1:
            raise SmokeFailure(
                "/admin/currency-rates/upsert",
                "currency-rate update did not append exactly one currency_rates row",
                status,
            )
    checked.append("/admin/currency-rates/upsert (authenticated POST)")
    with _isolated_smoke_dictionaries(database_url) as dictionaries:
        update_path = f"/admin/dictionaries/servers/{dictionaries['server_b_id']}/update"
        status, _, body = wsgi_request(
            app, update_path, method="POST",
            data={"name": dictionaries["server_a"], "comment": "duplicate probe", "is_active": "1"},
            cookie=cookie,
        )
        if status != "400 Bad Request" or "Кажется, такой сервер у нас уже есть".encode() not in body:
            raise SmokeFailure(update_path, "duplicate server update did not return friendly validation", status)
        if _dictionary_value(database_url, "servers", dictionaries["server_b_id"]) != dictionaries["server_b"]:
            raise SmokeFailure(update_path, "duplicate server update changed the database row", status)

        status, headers, _ = wsgi_request(
            app, update_path, method="POST",
            data={"name": dictionaries["server_renamed"], "comment": "successful probe", "is_active": "1"},
            cookie=cookie,
        )
        expected_location = "/admin/dictionaries?section=servers"
        if status != "303 See Other" or _header(headers, "Location") != expected_location:
            raise SmokeFailure(update_path, "unique server update did not redirect successfully", status)
        if _dictionary_value(database_url, "servers", dictionaries["server_b_id"]) != dictionaries["server_renamed"]:
            raise SmokeFailure(update_path, "unique server update was not persisted", status)

        create_path = "/admin/dictionaries/projects/create"
        status, headers, _ = wsgi_request(
            app, create_path, method="POST",
            data={"name": dictionaries["project"], "comment": "direct SQL create probe"}, cookie=cookie,
        )
        if status != "303 See Other" or _header(headers, "Location") != "/admin/dictionaries?section=projects":
            raise SmokeFailure(create_path, "project dictionary create did not redirect successfully", status)
        verify = connect_postgres(database_url)
        try:
            project = verify.execute("SELECT id FROM projects WHERE name = %s", (dictionaries["project"],)).fetchone()
            if not project:
                raise SmokeFailure(create_path, "project dictionary row was not created", status)
        finally:
            verify.close()
    checked.extend([
        "/admin/dictionaries/servers/{id}/update (duplicate authenticated POST)",
        "/admin/dictionaries/servers/{id}/update (successful authenticated POST)",
        "/admin/dictionaries/projects/create (authenticated POST)",
    ])
    with _isolated_smoke_campaign(database_url) as campaign:
        status, headers, _ = wsgi_request(
            app,
            "/companies/create",
            method="POST",
            data={
                "server_id": str(campaign["server_id"]), "country_id": str(campaign["country_id"]),
                "company_id_external": campaign["external_id"], "company_name": "CI_SMOKE campaign",
                "line_count": "1", "dial_set_count": "1", "retry_interval_seconds": "30",
                "has_autorotation": "1", "is_active": "1", "comment": "CI_SMOKE full-app campaign",
            },
            cookie=cookie,
        )
        if status != "303 See Other" or _header(headers, "Location") != "/companies":
            raise SmokeFailure("/companies/create", "campaign creation did not redirect to /companies", status)
        verify = connect_postgres(database_url)
        try:
            company = verify.execute(
                "SELECT id FROM calling_companies WHERE company_id_external = %s", (campaign["external_id"],)
            ).fetchone()
            if not company:
                raise SmokeFailure("/companies/create", "campaign row was not created", status)
            setting = verify.execute(
                "SELECT id FROM company_routing_settings WHERE calling_company_id = %s AND is_active IS TRUE",
                (company["id"],),
            ).fetchone()
            if not setting:
                raise SmokeFailure("/companies/create", "autorotation routing setting was not created", status)
        finally:
            verify.close()
    checked.append("/companies/create (authenticated POST)")
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
