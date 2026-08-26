#!/usr/bin/env python3
"""Smoke-test the real TeleRoute WSGI application against PostgreSQL.

The caller must migrate the demo SQLite database first.  This script does not
create or mutate schema: PostgreSQL schema ownership remains with migrations.
It adds one randomly-passworded CI-only user, imports ``app.server.app`` with
the guarded PostgreSQL environment enabled, and exercises authenticated pages.
"""
from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import secrets
import sys
import traceback
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect_postgres  # noqa: E402
from app.repository import Repository, hash_password  # noqa: E402
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
def _isolated_smoke_change_reason(database_url: str):
    suffix = secrets.token_hex(6).upper()
    original_name = f"CI_SMOKE_REASON_{suffix}"
    conn = connect_postgres(database_url)
    try:
        reason_id = Repository(conn, backend="postgres").create_change_reason(
            original_name,
            comment="CI full-app smoke reason",
            scopes=["none"],
        )
    finally:
        conn.close()
    try:
        yield {"id": reason_id, "name": original_name}
    finally:
        cleanup = connect_postgres(database_url)
        try:
            cleanup.execute(
                "DELETE FROM change_log WHERE entity_type = 'change_reason' AND entity_id = %s",
                (reason_id,),
            )
            cleanup.execute("DELETE FROM routing_events WHERE reason = %s", (original_name,))
            cleanup.execute("DELETE FROM change_reasons WHERE id = %s", (reason_id,))
            cleanup.commit()
        finally:
            cleanup.close()


def _change_reason_state(database_url: str, reason_id: int) -> tuple[dict[str, object] | None, int]:
    conn = connect_postgres(database_url)
    try:
        row = conn.execute(
            "SELECT name, description, is_active FROM change_reasons WHERE id = %s", (reason_id,)
        ).fetchone()
        log = conn.execute(
            "SELECT COUNT(*) AS value FROM change_log WHERE entity_type = 'change_reason' "
            "AND entity_id = %s AND change_type = 'change_reason.updated'",
            (reason_id,),
        ).fetchone()
        return (dict(row) if row else None, int(log["value"]))
    finally:
        conn.close()


def _smoke_prefix_values() -> tuple[str, str]:
    """Create distinct numeric-only prefix values accepted by production validation."""
    numeric_suffix = f"{secrets.randbelow(1_000_000):06d}"
    return f"69{numeric_suffix}", f"70{numeric_suffix}"


@contextmanager
def _isolated_smoke_dictionaries(database_url: str):
    suffix = secrets.token_hex(6).upper()
    prefix, prefix_renamed = _smoke_prefix_values()
    names = {
        "server_a": f"CI_SMOKE_SERVER_A_{suffix}",
        "server_b": f"CI_SMOKE_SERVER_B_{suffix}",
        "server_renamed": f"CI_SMOKE_SERVER_B_RENAMED_{suffix}",
        "project": f"CI_SMOKE_PROJECT_{suffix}",
        "provider_a": f"CI_SMOKE_PROVIDER_A_{suffix}",
        "provider_b": f"CI_SMOKE_PROVIDER_B_{suffix}",
        "prefix": prefix,
        "prefix_renamed": prefix_renamed,
    }
    conn = connect_postgres(database_url)
    try:
        server_ids = []
        for name in (names["server_a"], names["server_b"]):
            row = conn.execute(
                "INSERT INTO servers(name, is_active) VALUES (%s, true) RETURNING id", (name,)
            ).fetchone()
            server_ids.append(int(row["id"]))
        currency = conn.execute("SELECT id FROM currencies ORDER BY id LIMIT 1").fetchone()
        if not currency:
            raise SmokeFailure("/admin/dictionaries?section=prefixes", "currency is required for prefix smoke")
        provider_ids = []
        for name in (names["provider_a"], names["provider_b"]):
            row = conn.execute(
                "INSERT INTO providers(name, normalized_name, default_currency_id, is_active) VALUES (%s, %s, %s, true) RETURNING id",
                (name, name.lower(), currency["id"]),
            ).fetchone()
            provider_ids.append(int(row["id"]))
        prefix = conn.execute(
            "INSERT INTO provider_prefixes(provider_id, prefix, name, is_active) VALUES (%s, %s, %s, true) RETURNING id",
            (provider_ids[0], names["prefix"], "CI prefix"),
        ).fetchone()
        prefix_id = int(prefix["id"])
        conn.commit()
    finally:
        conn.close()
    try:
        yield {**names, "server_a_id": server_ids[0], "server_b_id": server_ids[1],
               "provider_a_id": provider_ids[0], "provider_b_id": provider_ids[1], "prefix_id": prefix_id}
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
            cleanup.execute("DELETE FROM change_log WHERE entity_type = 'prefixes' AND entity_id = %s", (prefix_id,))
            cleanup.execute("DELETE FROM provider_prefixes WHERE id = %s", (prefix_id,))
            cleanup.execute("DELETE FROM providers WHERE id = ANY(%s)", (provider_ids,))
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


def _prefix_value(database_url: str, prefix_id: int) -> dict[str, object] | None:
    conn = connect_postgres(database_url)
    try:
        row = conn.execute(
            "SELECT provider_id, prefix, name FROM provider_prefixes WHERE id = %s", (prefix_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _prefix_change_log_count(database_url: str, prefix_id: int) -> int:
    conn = connect_postgres(database_url)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS value FROM change_log WHERE entity_type = 'prefixes' AND entity_id = %s",
            (prefix_id,),
        ).fetchone()
        return int(row["value"])
    finally:
        conn.close()


def _normalized_body_text(body: bytes) -> str:
    """Return the complete normalized user-visible text from an HTML response."""
    decoded = body.decode("utf-8", errors="replace")
    without_nonvisible = re.sub(r"<(style|script)\b[^>]*>.*?</\1\s*>", " ", decoded, flags=re.I | re.S)
    without_markup = re.sub(r"<[^>]+>", " ", without_nonvisible)
    return " ".join(html.unescape(without_markup).split())


def _body_excerpt(text: str, *, limit: int = 500) -> str:
    """Bound already-normalized text for safe smoke failure diagnostics only."""
    return text[:limit]


def _tariff_state_diagnostic(tariff) -> str:
    """Return bounded, non-secret tariff fields for actionable smoke failures."""
    fields = ("price_in_provider_currency", "comment", "is_current", "updated_at")
    state = ", ".join(f"{field}={_body_excerpt(repr(tariff.get(field)), limit=120)}" for field in fields)
    return _body_excerpt(state, limit=400)


def _tariff_edit_state_is_expected(tariff, original_token) -> bool:
    return (
        Decimal(str(tariff["price_in_provider_currency"])) == Decimal("2.25")
        and tariff["comment"] == "tariff updated"
        and tariff["is_current"] is False
        and tariff["updated_at"] != original_token
    )


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


@contextmanager
def _isolated_edit_paths(database_url: str):
    """Create mutually isolated rows for the three PostgreSQL edit lifecycles."""
    suffix = secrets.token_hex(6).upper()
    conn = connect_postgres(database_url)
    ids = {}
    try:
        user = conn.execute("SELECT id FROM users WHERE username = %s", (USERNAME,)).fetchone()
        currency = conn.execute(
            "SELECT c.id, cr.id AS rate_id, cr.rate_to_eur, cr.rate_date FROM currencies c "
            "JOIN LATERAL (SELECT * FROM currency_rates WHERE currency_id=c.id ORDER BY rate_date DESC, id DESC LIMIT 1) cr ON true "
            "WHERE c.is_active IS TRUE ORDER BY c.id LIMIT 1"
        ).fetchone()
        if not user or not currency:
            raise SmokeFailure("/tariffs/{id}/edit", "CI user and an active currency rate are required")
        country = conn.execute("INSERT INTO countries(name, is_active) VALUES (%s, true) RETURNING id", (f"CI_EDIT_{suffix}",)).fetchone()
        provider = conn.execute(
            "INSERT INTO providers(name, normalized_name, default_currency_id, is_active) VALUES (%s, %s, %s, true) RETURNING id",
            (f"CI_EDIT_PROVIDER_{suffix}", f"ci_edit_provider_{suffix.lower()}", currency["id"]),
        ).fetchone()
        route = conn.execute(
            "INSERT INTO routes(country_id,provider_id,name,cli_source_type,cli_source_label,aon_pool,comment,created_by) "
            "VALUES (%s,%s,%s,'pool',%s,'Пул купленных номеров',%s,%s) RETURNING id, updated_at",
            (country["id"], provider["id"], f"CI_EDIT_ROUTE_{suffix}", f"CI_EDIT_{suffix}", "route initial", user["id"]),
        ).fetchone()
        tariff = conn.execute(
            "INSERT INTO tariffs(country_id,provider_id,provider_currency_id,price_in_provider_currency,conversion_rate_to_eur,conversion_rate_date,currency_rate_id,eur_price,comment,created_by) "
            "VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,%s) RETURNING id, updated_at",
            (country["id"], provider["id"], currency["id"], currency["rate_to_eur"], currency["rate_date"], currency["rate_id"], currency["rate_to_eur"], "tariff initial", user["id"]),
        ).fetchone()
        event = conn.execute(
            "INSERT INTO routing_events(event_at,apply_scope,reason,country_id,provider_id,affected_route_id,comment,created_by) "
            "VALUES (CURRENT_TIMESTAMP,'none','Провайдер сменил маршрут',%s,%s,%s,%s,%s) RETURNING id, updated_at",
            (country["id"], provider["id"], route["id"], "event initial", user["id"]),
        ).fetchone()
        ids = {"user": int(user["id"]), "country": int(country["id"]), "provider": int(provider["id"]),
               "currency": int(currency["id"]), "rate": int(currency["rate_id"]),
               "route": int(route["id"]), "tariff": int(tariff["id"]), "event": int(event["id"]),
               "route_token": route["updated_at"], "tariff_token": tariff["updated_at"], "event_token": event["updated_at"],
               "assignment_name": f"CI_ASSIGNMENT_{suffix}"}
        conn.commit()
        yield ids
    finally:
        try:
            if ids:
                if ids.get("phone"):
                    conn.execute("DELETE FROM phone_number_history WHERE phone_number_id=%s", (ids["phone"],))
                    conn.execute("DELETE FROM phone_numbers WHERE id=%s", (ids["phone"],))
                if ids.get("assignment"):
                    conn.execute("DELETE FROM phone_assignment_types WHERE id=%s", (ids["assignment"],))
                conn.execute("DELETE FROM change_log WHERE (entity_type='route' AND entity_id=%s) OR (entity_type='tariff' AND entity_id=%s) OR (entity_type='routing_event' AND entity_id=%s)", (ids["route"], ids["tariff"], ids["event"]))
                conn.execute("DELETE FROM tariff_change_history WHERE tariff_id=%s", (ids["tariff"],))
                conn.execute("DELETE FROM route_history WHERE route_id=%s", (ids["route"],))
                conn.execute("DELETE FROM routing_events WHERE id=%s", (ids["event"],))
                conn.execute("DELETE FROM tariffs WHERE id=%s", (ids["tariff"],))
                conn.execute("DELETE FROM routes WHERE id=%s", (ids["route"],))
                conn.execute("DELETE FROM providers WHERE id=%s", (ids["provider"],))
                conn.execute("DELETE FROM countries WHERE id=%s", (ids["country"],))
                conn.commit()
        finally:
            conn.close()


def wsgi_request(application, path: str, *, method: str = "GET", data=None, cookie: str | None = None, headers=None):
    body = urlencode(data or {}).encode("utf-8")
    path_info, _, query_string = path.partition("?")
    environ: dict[str, object] = {}
    setup_testing_defaults(environ)
    environ.update({
        "REQUEST_METHOD": method,
        "PATH_INFO": path_info,
        "QUERY_STRING": query_string,
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": "application/x-www-form-urlencoded",
        "wsgi.input": io.BytesIO(body),
        "REMOTE_ADDR": "127.0.0.1",
        "HTTP_USER_AGENT": "TeleRoute-Stage68A-Smoke",
    })
    if cookie:
        environ["HTTP_COOKIE"] = cookie
    environ.update(headers or {})
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


def _run_import_export_smoke(app, database_url: str, cookie: str) -> list[str]:
    """Exercise the existing import forms and section CSV exports on PostgreSQL."""
    suffix = secrets.token_hex(5).upper()
    country = f"CI_IMPORT_{suffix}"
    route = f"Маршрут_{suffix}"
    company = f"Кампания_{suffix}"
    external_id = f"CI-{suffix}"
    number = f"7999{int(suffix, 16) % 10**7:07d}"
    comment = f"Полный комментарий кириллицей; {suffix}; без UI-сокращения"
    conn = connect_postgres(database_url)
    try:
        provider = conn.execute("SELECT name FROM providers WHERE is_active IS TRUE ORDER BY id LIMIT 1").fetchone()
        currency = conn.execute("SELECT code FROM currencies WHERE is_active IS TRUE ORDER BY id LIMIT 1").fetchone()
        server = conn.execute("SELECT name FROM servers WHERE is_active IS TRUE ORDER BY id LIMIT 1").fetchone()
        if not provider or not currency or not server:
            raise SmokeFailure("/admin/import", "active provider, currency and server reference rows are required")
        fixtures = (
            ("dictionaries", f"type,name\ncountry,{country}\n", "countries", "name", country),
            ("routes", f"country,name,provider,comment\n{country},{route},{provider['name']},{comment}\n", "routes", "name", route),
            ("tariffs", f"country,provider,currency,price,rate,rate_date,comment\n{country},{provider['name']},{currency['code']},1.25,1,2026-01-01,{comment}\n", "tariffs", None, None),
            ("calling_companies", f"server,country,company_id_external,company_name,comment\n{server['name']},{country},{external_id},{company},{comment}\n", "calling_companies", "company_id_external", external_id),
            ("phone_numbers", f"country,provider,currency,number,final_status,comment\n{country},{provider['name']},{currency['code']},{number},Используется,{comment}\n", "phone_numbers", "normalized_number", number),
        )
        for entity, csv_data, table, key_column, key_value in fixtures:
            if key_column:
                before = int(conn.execute(f"SELECT COUNT(*) AS value FROM {table} WHERE {key_column} = %s", (key_value,)).fetchone()["value"])
            else:
                before = int(conn.execute("SELECT COUNT(*) AS value FROM tariffs t JOIN countries c ON c.id=t.country_id WHERE c.name=%s", (country,)).fetchone()["value"])
            status, _, body = wsgi_request(app, "/admin/import/preview", method="POST", cookie=cookie, data={"entity_type": entity, "mode": "append_update", "csv_data": csv_data})
            if status != "200 OK" or "ошибок: 0" not in _normalized_body_text(body):
                raise SmokeFailure("/admin/import/preview", f"{entity} preview failed", status)
            after_preview = int(conn.execute(f"SELECT COUNT(*) AS value FROM {table} WHERE {key_column} = %s", (key_value,)).fetchone()["value"]) if key_column else int(conn.execute("SELECT COUNT(*) AS value FROM tariffs t JOIN countries c ON c.id=t.country_id WHERE c.name=%s", (country,)).fetchone()["value"])
            if after_preview != before:
                raise SmokeFailure("/admin/import/preview", f"{entity} preview mutated PostgreSQL")
            status, _, body = wsgi_request(app, "/admin/import/apply", method="POST", cookie=cookie, data={"entity_type": entity, "mode": "append_update", "csv_data": csv_data})
            if status != "200 OK" or "создано 1" not in _normalized_body_text(body):
                raise SmokeFailure("/admin/import/apply", f"{entity} apply did not create exactly one row", status)
        conn.commit()
        for path, needles in (("/routes?export=csv", (route, comment)), ("/tariffs?export=csv", (country, comment)), ("/phones?export=csv", (number, comment)), ("/companies?export=csv", (external_id, comment))):
            status, headers, body = wsgi_request(app, path, cookie=cookie)
            decoded = body.decode("utf-8-sig")
            if status != "200 OK" or not _header(headers, "Content-Disposition") or any(value not in decoded for value in needles):
                raise SmokeFailure(path, "PostgreSQL CSV export omitted full imported UTF-8/reference data", status)
        return ["/admin/import/preview + apply (all five supported types)", "/routes, /tariffs, /phones, /companies CSV exports"]
    finally:
        try:
            conn.rollback()
            country_row = conn.execute("SELECT id FROM countries WHERE name=%s", (country,)).fetchone()
            if country_row:
                country_id = int(country_row["id"])
                conn.execute("DELETE FROM phone_number_history WHERE phone_number_id IN (SELECT id FROM phone_numbers WHERE country_id=%s)", (country_id,))
                conn.execute("DELETE FROM phone_numbers WHERE country_id=%s", (country_id,))
                conn.execute("DELETE FROM change_log WHERE (entity_type='route' AND entity_id IN (SELECT id FROM routes WHERE country_id=%s)) OR (entity_type='tariff' AND entity_id IN (SELECT id FROM tariffs WHERE country_id=%s)) OR (entity_type='calling_company' AND entity_id IN (SELECT id FROM calling_companies WHERE country_id=%s))", (country_id, country_id, country_id))
                conn.execute("DELETE FROM tariff_change_history WHERE tariff_id IN (SELECT id FROM tariffs WHERE country_id=%s)", (country_id,))
                conn.execute("DELETE FROM tariffs WHERE country_id=%s", (country_id,))
                conn.execute("DELETE FROM route_history WHERE route_id IN (SELECT id FROM routes WHERE country_id=%s)", (country_id,))
                conn.execute("DELETE FROM routes WHERE country_id=%s", (country_id,))
                conn.execute("DELETE FROM calling_companies WHERE country_id=%s", (country_id,))
                conn.execute("DELETE FROM countries WHERE id=%s", (country_id,))
                conn.commit()
        finally:
            conn.close()


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
    checked.extend(_run_import_export_smoke(app, database_url, cookie))
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
    with _isolated_smoke_change_reason(database_url) as reason:
        probe = connect_postgres(database_url)
        try:
            provider_id = int(probe.execute("SELECT id FROM providers WHERE is_active IS TRUE ORDER BY id LIMIT 1").fetchone()["id"])
        finally:
            probe.close()
        event_path = "/provider-changes/create"
        status, headers, _ = wsgi_request(
            app,
            event_path,
            method="POST",
            data={
                "apply_scope": "none", "event_at": "2026-08-26T12:00",
                "provider_id": str(provider_id), "reason": reason["name"],
            },
            cookie=cookie,
        )
        if status != "303 See Other" or _header(headers, "Location") != "/provider-changes":
            raise SmokeFailure(event_path, "DB-backed change reason was not accepted", status)
        verify = connect_postgres(database_url)
        try:
            event = verify.execute(
                "SELECT id, reason FROM routing_events WHERE reason = %s ORDER BY id DESC LIMIT 1", (reason["name"],)
            ).fetchone()
        finally:
            verify.close()
        if not event or event["reason"] != reason["name"]:
            raise SmokeFailure(event_path, "routing event did not preserve the reason snapshot", status)

        update_path = f"/admin/change-reasons/{reason['id']}/update"
        status, headers, _ = wsgi_request(
            app,
            update_path,
            method="POST",
            data={"name": reason["name"], "comment": "вызовы уходят в занято", "is_active": "0", "scopes": "none", "_scopes_present": "1"},
            cookie=cookie,
        )
        if status != "303 See Other" or _header(headers, "Location") != "/admin/change-reasons":
            raise SmokeFailure(update_path, "change-reason update did not redirect successfully", status)
        state, update_logs = _change_reason_state(database_url, int(reason["id"]))
        if state != {"name": reason["name"], "description": "вызовы уходят в занято", "is_active": False}:
            raise SmokeFailure(update_path, "change-reason update was not persisted", status)
        if update_logs != 1:
            raise SmokeFailure(update_path, "change-reason update did not create exactly one audit row", status)
        status, _, body = wsgi_request(app, "/provider-changes", cookie=cookie)
        rendered = body.decode("utf-8", errors="replace")
        selectable_payload = rendered.split("const reasonsByScope = ", 1)[-1].split(";", 1)[0]
        if status != "200 OK" or reason["name"] in selectable_payload:
            raise SmokeFailure("/provider-changes", "inactive reason remained selectable", status)
        verify = connect_postgres(database_url)
        try:
            snapshot = verify.execute("SELECT reason FROM routing_events WHERE id = %s", (event["id"],)).fetchone()
        finally:
            verify.close()
        if not snapshot or snapshot["reason"] != reason["name"]:
            raise SmokeFailure(event_path, "deactivation altered the historical snapshot", status)
    checked.append("/provider-changes DB reason lifecycle (authenticated POST)")
    with _isolated_smoke_dictionaries(database_url) as dictionaries:
        prefix_path = f"/admin/dictionaries/prefixes/{dictionaries['prefix_id']}/update"
        prefix_before_attack = _prefix_value(database_url, dictionaries["prefix_id"])
        log_count_before_attack = _prefix_change_log_count(database_url, dictionaries["prefix_id"])
        status, _, body = wsgi_request(
            app, prefix_path, method="POST",
            data={"provider_id": str(dictionaries["provider_b_id"]), "prefix": dictionaries["prefix_renamed"], "name": "attack", "is_active": "1"},
            cookie=cookie,
        )
        prefix_after_attack = _prefix_value(database_url, dictionaries["prefix_id"])
        log_count_after_attack = _prefix_change_log_count(database_url, dictionaries["prefix_id"])
        if prefix_after_attack != prefix_before_attack or log_count_after_attack != log_count_before_attack:
            raise SmokeFailure(prefix_path, "prefix ownership substitution changed the database row", status)
        full_body_text = _normalized_body_text(body)
        expected_message = "провайдера у существующего префикса менять нельзя"
        if status != "400 Bad Request" or expected_message not in full_body_text:
            raise SmokeFailure(
                prefix_path,
                f"prefix ownership substitution did not return friendly validation; "
                f"expected={expected_message!r}; actual body excerpt={_body_excerpt(full_body_text)!r}",
                status,
            )
        status, headers, _ = wsgi_request(
            app, prefix_path, method="POST",
            data={"prefix": dictionaries["prefix_renamed"], "name": "normal edit", "is_active": "1"}, cookie=cookie,
        )
        prefix_after_edit = _prefix_value(database_url, dictionaries["prefix_id"])
        if status != "303 See Other" or not prefix_after_edit or prefix_after_edit["provider_id"] != dictionaries["provider_a_id"] or prefix_after_edit["prefix"] != dictionaries["prefix_renamed"] or prefix_after_edit["name"] != "normal edit":
            raise SmokeFailure(prefix_path, "normal prefix edit did not preserve ownership", status)
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

        verify = connect_postgres(database_url)
        try:
            country = verify.execute("SELECT id FROM countries ORDER BY id LIMIT 1").fetchone()
        finally:
            verify.close()
        route_name = f"CI_PREFIX_ROUTE_{secrets.token_hex(6).upper()}"
        route_data = {
            "country_id": str(country["id"]), "provider_id": str(dictionaries["provider_a_id"]),
            "provider_prefix_id": str(dictionaries["prefix_id"]), "name": route_name,
            "project_label": "", "cli_source_type": "pool", "cli_source_label": "CI purchased pool",
            "aon_pool": "Пул купленных номеров", "rnd_type": "", "rnd_pool_owner": "",
            "comment": "CI prefix route", "is_actual": "1",
        }
        wrong_data = {**route_data, "provider_id": str(dictionaries["provider_b_id"])}
        status, _, body = wsgi_request(app, "/routes/create", method="POST", data=wrong_data, cookie=cookie)
        if status != "400 Bad Request" or "Префикс не принадлежит выбранному провайдеру" not in _normalized_body_text(body):
            raise SmokeFailure("/routes/create", "wrong-provider prefix was not rejected cleanly", status)
        status, headers, _ = wsgi_request(app, "/routes/create", method="POST", data=route_data, cookie=cookie)
        if status != "303 See Other" or _header(headers, "Location") != "/routes":
            raise SmokeFailure("/routes/create", "purchased-pool prefix route creation failed", status)
        verify = connect_postgres(database_url)
        try:
            route = verify.execute("SELECT id, provider_prefix_id, aon_pool FROM routes WHERE name = %s", (route_name,)).fetchone()
        finally:
            verify.close()
        if not route or int(route["provider_prefix_id"]) != dictionaries["prefix_id"] or route["aon_pool"] != "Пул купленных номеров":
            raise SmokeFailure("/routes/create", "purchased-pool prefix route was not persisted", status)
        route_id = int(route["id"])
        status, _, body = wsgi_request(app, "/routes", cookie=cookie)
        if status != "200 OK" or f'href="/routes/{route_id}/numbers">Показать номера</a>'.encode() not in body:
            raise SmokeFailure("/routes", "purchased-pool route number-management link was not rendered", status)
        for route_numbers_path in (f"/routes/{route_id}/numbers", f"/routes/{route_id}/numbers/manage"):
            status, _, _ = wsgi_request(app, route_numbers_path, cookie=cookie)
            if status != "200 OK":
                raise SmokeFailure(route_numbers_path, "purchased-pool route numbers page failed", status)
        cleanup = connect_postgres(database_url)
        try:
            cleanup.execute("DELETE FROM change_log WHERE entity_type = 'route' AND entity_id = %s", (route_id,))
            cleanup.execute("DELETE FROM route_history WHERE route_id = %s", (route_id,))
            cleanup.execute("DELETE FROM routes WHERE id = %s", (route_id,))
            cleanup.commit()
        finally:
            cleanup.close()

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
        "/routes/create + list + numbers/manage (purchased-pool prefix lifecycle)",
        "/admin/dictionaries/prefixes/{id}/update (ownership rejection and normal edit)",
        "/admin/dictionaries/servers/{id}/update (duplicate authenticated POST)",
        "/admin/dictionaries/servers/{id}/update (successful authenticated POST)",
        "/admin/dictionaries/projects/create (authenticated POST)",
    ])
    with _isolated_smoke_campaign(database_url) as campaign:
        blank_status, _, blank_body = wsgi_request(
            app,
            "/companies/create",
            method="POST",
            data={
                "server_id": str(campaign["server_id"]), "country_id": str(campaign["country_id"]),
                "company_id_external": campaign["external_id"], "company_name": "   ",
                "line_count": "0", "dial_set_count": "0", "retry_interval_seconds": "0",
                "has_autorotation": "1", "is_active": "1", "comment": "CI_SMOKE blank campaign",
            },
            cookie=cookie,
        )
        if blank_status != "400 Bad Request" or "Укажи название кампании." not in _normalized_body_text(blank_body):
            raise SmokeFailure("/companies/create", "whitespace-only campaign name was not returned as validation", blank_status)
        verify = connect_postgres(database_url)
        try:
            if verify.execute("SELECT id FROM calling_companies WHERE company_id_external = %s", (campaign["external_id"],)).fetchone():
                raise SmokeFailure("/companies/create", "whitespace-only campaign was persisted", blank_status)
        finally:
            verify.close()
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
    with _isolated_smoke_campaign(database_url) as campaign:
        multi_data = {
            "server_id": str(campaign["server_id"]), "country_id": "",
            "company_id_external": campaign["external_id"], "company_name": "CI_SMOKE multi-GEO",
            "line_count": "1", "dial_set_count": "1", "retry_interval_seconds": "30",
            "has_autorotation": "1", "is_active": "1", "comment": "CI_SMOKE multi-GEO campaign",
        }
        status, headers, _ = wsgi_request(app, "/companies/create", method="POST", data=multi_data, cookie=cookie)
        if status != "303 See Other" or _header(headers, "Location") != "/companies":
            raise SmokeFailure("/companies/create", "multi-GEO autorotation creation failed", status)
        verify = connect_postgres(database_url)
        try:
            company = verify.execute("SELECT id, country_id, updated_at FROM calling_companies WHERE company_id_external = %s", (campaign["external_id"],)).fetchone()
            setting = verify.execute(
                "SELECT country_id, route_id, routing_mode, has_autorotation, is_active, valid_to "
                "FROM company_routing_settings WHERE calling_company_id = %s AND is_active IS TRUE",
                (company["id"],),
            ).fetchone() if company else None
            if not company or company["country_id"] is not None or not setting or setting["country_id"] is not None:
                raise SmokeFailure("/companies/create", "multi-GEO autorotation nullable state was not persisted", status)
            if setting["route_id"] is not None or setting["routing_mode"] != "autorotation" or not setting["has_autorotation"] or not setting["is_active"] or setting["valid_to"] is not None:
                raise SmokeFailure("/companies/create", "multi-GEO autorotation setting has unexpected state", status)
            company_id = int(company["id"])
            token = str(company["updated_at"])
        finally:
            verify.close()
        status, _, body = wsgi_request(app, "/companies", cookie=cookie)
        if status != "200 OK" or "Несколько GEO" not in _normalized_body_text(body):
            raise SmokeFailure("/companies", "multi-GEO label was not rendered", status)
        edit_path = f"/companies/{company_id}/edit"
        status, _, _ = wsgi_request(app, edit_path, cookie=cookie)
        if status != "200 OK":
            raise SmokeFailure(edit_path, "multi-GEO edit form failed", status)
        update = {**multi_data, "country_id": str(campaign["country_id"]), "expected_updated_at": token}
        status, _, _ = wsgi_request(app, f"/companies/{company_id}/update", method="POST", data=update, cookie=cookie)
        if status != "303 See Other":
            raise SmokeFailure(edit_path, "multi-GEO to single-GEO update failed", status)
        verify = connect_postgres(database_url)
        try:
            changed = verify.execute("SELECT country_id, updated_at FROM calling_companies WHERE id = %s", (company_id,)).fetchone()
            if int(changed["country_id"]) != campaign["country_id"]:
                raise SmokeFailure(edit_path, "single-GEO value was not persisted", status)
            update["country_id"] = ""
            update["expected_updated_at"] = str(changed["updated_at"])
        finally:
            verify.close()
        status, _, _ = wsgi_request(app, f"/companies/{company_id}/update", method="POST", data=update, cookie=cookie)
        verify = connect_postgres(database_url)
        try:
            changed = verify.execute("SELECT country_id FROM calling_companies WHERE id = %s", (company_id,)).fetchone()
            if status != "303 See Other" or changed["country_id"] is not None:
                raise SmokeFailure(edit_path, "single-GEO to multi-GEO update failed", status)
        finally:
            verify.close()
    checked.append("/companies multi-GEO create/edit/validation (authenticated WSGI)")
    with _isolated_edit_paths(database_url) as edit:
        numbers_path = f"/routes/{edit['route']}/numbers"
        status, _, body = wsgi_request(app, numbers_path, cookie=cookie)
        if status != "200 OK" or b"CI_EDIT_ROUTE" not in body:
            raise SmokeFailure(numbers_path, "purchased-pool route numbers did not render on PostgreSQL", status)

        assignment_path = "/admin/dictionaries/phone-assignments/create"
        status, headers, _ = wsgi_request(app, assignment_path, method="POST", cookie=cookie, data={
            "name": edit["assignment_name"], "comment": "CI generated-code assignment",
        })
        if status != "303 See Other":
            raise SmokeFailure(assignment_path, "assignment create without code failed", status)
        verify = connect_postgres(database_url)
        try:
            assignment = verify.execute("SELECT id, code FROM phone_assignment_types WHERE name=%s", (edit["assignment_name"],)).fetchone()
            if not assignment or not assignment["code"]:
                raise SmokeFailure(assignment_path, "assignment backend did not generate a nonblank code", status)
            edit["assignment"] = int(assignment["id"])
            assignment_code = assignment["code"]
        finally:
            verify.close()

        phone_number = "999" + str(edit["route"]).zfill(9)[-9:]
        status, headers, _ = wsgi_request(app, "/phones/create", method="POST", cookie=cookie, data={
            "number": phone_number, "country_id": str(edit["country"]), "provider_id": str(edit["provider"]),
            "assignment_type": assignment_code, "status": "used", "is_active": "1", "comment": "phone initial",
        })
        verify = connect_postgres(database_url)
        try:
            phone = verify.execute("SELECT id FROM phone_numbers WHERE number=%s", (phone_number,)).fetchone()
            if status != "303 See Other" or not phone:
                raise SmokeFailure("/phones/create", "CI-only phone was not created", status)
            edit["phone"] = int(phone["id"])
        finally:
            verify.close()
        phone_edit_path = f"/phones/{edit['phone']}/edit"
        status, _, body = wsgi_request(app, phone_edit_path, cookie=cookie)
        if status != "200 OK" or b"phone initial" not in body:
            raise SmokeFailure(phone_edit_path, "phone edit GET failed on PostgreSQL", status)
        status, headers, _ = wsgi_request(app, f"/phones/{edit['phone']}/update", method="POST", cookie=cookie, data={
            "number": phone_number, "country_id": str(edit["country"]), "provider_id": str(edit["provider"]),
            "assignment_type": assignment_code, "status": "used", "is_active": "1", "comment": "phone updated",
        })
        verify = connect_postgres(database_url)
        try:
            phone = verify.execute("SELECT comment, assignment_type FROM phone_numbers WHERE id=%s", (edit["phone"],)).fetchone()
            if status != "303 See Other" or phone["comment"] != "phone updated" or phone["assignment_type"] != assignment_code:
                raise SmokeFailure(f"/phones/{edit['phone']}/update", "phone update was not persisted", status)
        finally:
            verify.close()
        scenarios = (
            (f"/provider-changes/{edit['event']}/edit", b"event initial", False),
            (f"/routes/{edit['route']}/edit", b"route initial", True),
            (f"/tariffs/{edit['tariff']}/edit", b"tariff initial", True),
        )
        for path, current_value, modal in scenarios:
            status, _, body = wsgi_request(
                app, path, cookie=cookie,
                headers={"HTTP_X_REQUESTED_WITH": "fetch"} if modal else None,
            )
            if status != "200 OK" or current_value not in body or (modal and b"data-modal-ready='1'" not in body):
                raise SmokeFailure(path, "edit GET did not render the current row and concurrency form", status)

        event_path = f"/provider-changes/{edit['event']}/update"
        status, headers, _ = wsgi_request(app, event_path, method="POST", cookie=cookie, data={
            "comment": "event updated", "updated_at_original": str(edit["event_token"]),
        })
        if status != "303 See Other" or _header(headers, "Location") != "/provider-changes":
            raise SmokeFailure(event_path, "provider-change comment update did not redirect", status)

        route_path = f"/routes/{edit['route']}/update"
        status, headers, _ = wsgi_request(app, route_path, method="POST", cookie=cookie, data={
            "name": f"CI_EDIT_ROUTE_UPDATED_{edit['route']}", "provider_id": str(edit["provider"]),
            "provider_prefix_id": "", "cli_source_type": "pool", "cli_source_label": "CI edit",
            "aon_pool": "pool", "rnd_type": "", "rnd_pool_owner": "", "is_actual": "1",
            "priority_status": "unknown", "comment": "route updated", "expected_updated_at": str(edit["route_token"]),
        })
        if status != "303 See Other" or _header(headers, "Location") != "/routes":
            raise SmokeFailure(route_path, "route update did not redirect", status)

        tariff_path = f"/tariffs/{edit['tariff']}/update"
        status, headers, _ = wsgi_request(app, tariff_path, method="POST", cookie=cookie, data={
            "currency_id": str(edit["currency"]), "price": "2.25", "comment": "tariff updated",
            "is_current": "0", "expected_updated_at": str(edit["tariff_token"]),
        })
        if status != "303 See Other" or _header(headers, "Location") != "/tariffs":
            raise SmokeFailure(tariff_path, "normal tariff update reported a false concurrency conflict", status)

        verify = connect_postgres(database_url)
        try:
            event = verify.execute("SELECT comment, updated_at FROM routing_events WHERE id=%s", (edit["event"],)).fetchone()
            route = verify.execute("SELECT comment, updated_at FROM routes WHERE id=%s", (edit["route"],)).fetchone()
            tariff = verify.execute("SELECT price_in_provider_currency, comment, is_current, updated_at FROM tariffs WHERE id=%s", (edit["tariff"],)).fetchone()
            if event["comment"] != "event updated" or event["updated_at"] == edit["event_token"]:
                raise SmokeFailure(event_path, "provider-change update was not persisted with a new token", status)
            if route["comment"] != "route updated" or route["updated_at"] == edit["route_token"]:
                raise SmokeFailure(route_path, "route update was not persisted with a new token", status)
            if not _tariff_edit_state_is_expected(tariff, edit["tariff_token"]):
                raise SmokeFailure(
                    tariff_path,
                    f"normal tariff fields were not persisted: {_tariff_state_diagnostic(tariff)}",
                    status,
                )
        finally:
            verify.close()

        status, _, body = wsgi_request(app, tariff_path, method="POST", cookie=cookie, data={
            "currency_id": str(edit["currency"]), "price": "9.99", "comment": "stale overwrite",
            "is_current": "1", "expected_updated_at": str(edit["tariff_token"]),
        })
        if status != "400 Bad Request" or "Запись была изменена другим пользователем".encode() not in body:
            raise SmokeFailure(tariff_path, "stale tariff token was not rejected as friendly concurrency", status)
        verify = connect_postgres(database_url)
        try:
            tariff = verify.execute("SELECT price_in_provider_currency, comment, is_current, updated_at FROM tariffs WHERE id=%s", (edit["tariff"],)).fetchone()
            if not _tariff_edit_state_is_expected(tariff, edit["tariff_token"]):
                raise SmokeFailure(
                    tariff_path,
                    f"stale tariff POST overwrote the latest row: {_tariff_state_diagnostic(tariff)}",
                    status,
                )
        finally:
            verify.close()
    checked.extend([
        "/routes/{id}/numbers (purchased-pool PostgreSQL GET)",
        "/admin/dictionaries/phone-assignments/create + /phones/{id}/edit + update (PostgreSQL lifecycle)",
        "/provider-changes/{id}/edit + update (PostgreSQL lifecycle)",
        "/routes/{id}/edit modal + update (PostgreSQL lifecycle)",
        "/tariffs/{id}/edit modal + normal/stale updates (PostgreSQL lifecycle)",
    ])
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
