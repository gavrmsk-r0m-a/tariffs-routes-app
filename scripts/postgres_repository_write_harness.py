#!/usr/bin/env python3
"""Rollback-only PostgreSQL write probes for the Repository transaction model.

This CI-only utility never calls ``commit``.  Every write is invoked with
``commit=False`` and undone by the surrounding connection rollback.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.repository import Repository, normalize_provider_name
from scripts.postgres_repository_smoke import mask_postgres_url, sanitize_error

DEFAULT_PROBE_KEY = "__stage51_rollback_probe__"
DEFAULT_PROBE_VALUE = "5151"
APP_SETTING_PROBE_KEY = "__stage52_app_setting_probe__"
HLR_DAILY_USAGE_PROBE_DATE = "2099-12-31"
USER_ADMIN_PROBE_USERNAME = "__stage53_user_admin_probe__"
COUNTRY_PROBE_NAME, COUNTRY_PROBE_CODE = "__stage54_country_probe__", "S54"
CURRENCY_PROBE_CODE, CURRENCY_PROBE_NAME, CURRENCY_PROBE_SYMBOL = "S54", "Stage 54 Currency", "S54"
# ``providers.provider_type`` is constrained by the migrated PostgreSQL schema.
PROVIDER_PROBE_NAME, PROVIDER_PROBE_TYPE, PROVIDER_PROBE_COMMENT = "__stage54_provider_probe__", "voip", "Stage 54 rollback probe"
PREFIX_PROBE_VALUE, PREFIX_PROBE_NAME = "9954", "Stage 54 Prefix"
GOC_COUNTRY_PROBE_NAME = "__stage55_goc_country_probe__"
GOC_CURRENCY_PROBE_CODE = "S55"
GOC_PROVIDER_PROBE_NAME = "__stage55_goc_provider_probe__"
GOC_PREFIX_PROBE_VALUE = "9955"
PROJECT_PROBE_NAME = "__stage56_project_probe__"
PHONE_NUMBER_TYPE_PROBE_NAME = "__stage56_phone_number_type_probe__"
PHONE_ASSIGNMENT_CODE = "__stage56_assignment_probe__"
PHONE_ASSIGNMENT_NAME = "Stage 56 Assignment Probe"
SERVER_PROBE_NAME = "__stage57_server_probe__"
CHANGE_REASON_PROBE_NAME = "__stage58_change_reason_probe__"
CHANGE_REASON_PROBE_COMMENT = "Stage 58 change reason rollback probe"
PRIORITY_CHANGED_COMMENT = "__stage60_priority_changed_comment__"
PRIORITY_SAME_ROUTE_COMMENT = "__stage60_priority_same_route_comment__"


def empty_summary(postgres_url: str) -> dict:
    return {
        "status": "failed", "postgres_url": mask_postgres_url(postgres_url),
        "checks_count": 0, "failures": [],
        "probes": {name: "skipped" for name in (
            "rollback_probe", "aborted_transaction_probe", "savepoint_probe",
            "app_setting_probe", "hlr_daily_usage_probe", "user_admin_probe",
            "dictionary_create_probe", "dictionary_get_or_create_probe",
            "dictionary_ensure_probe", "dictionary_server_probe",
            "dictionary_change_reason_probe", "dictionary_snapshot_probe",
            "provider_change_priority_probe",
        )},
    }


def run_rollback_probe(repo: Repository, conn, key: str, value: str) -> None:
    """Confirm a caller-owned HLR override write is visible then fully rolled back.

    ``key`` identifies this synthetic probe in diagnostics.  The public HLR
    Repository API intentionally owns its fixed setting key, so no arbitrary
    application setting is written by this harness.
    """
    before = repo.get_hlr_limit_override()
    conn.rollback()  # End the read transaction before the explicit write transaction.
    conn.execute("BEGIN")
    try:
        repo.set_hlr_limit_override(value, commit=False)
        if repo.get_hlr_limit_override() != value:
            raise AssertionError(f"{key}: override is not visible inside the transaction")
    finally:
        conn.rollback()
    after = repo.get_hlr_limit_override()
    try:
        if after != before:
            raise AssertionError(f"{key}: rollback did not restore the prior override value")
    finally:
        conn.rollback()


def run_aborted_transaction_probe(conn) -> None:
    """Verify PostgreSQL rejects queries after an error until transaction rollback."""
    conn.execute("BEGIN")
    try:
        try:
            conn.execute("SELECT * FROM definitely_missing_stage51_table")
        except Exception:
            pass
        else:
            raise AssertionError("expected missing-table SELECT to fail")
        try:
            conn.execute("SELECT 1")
        except Exception:
            pass
        else:
            raise AssertionError("expected SELECT 1 to fail in an aborted transaction")
    finally:
        conn.rollback()
    conn.execute("SELECT 1")
    conn.rollback()


def run_savepoint_probe(conn) -> None:
    """Verify an expected error can be contained by a SAVEPOINT."""
    conn.execute("BEGIN")
    try:
        conn.execute("SAVEPOINT stage51_probe")
        try:
            conn.execute("SELECT * FROM definitely_missing_stage51_table")
        except Exception:
            pass
        else:
            raise AssertionError("expected missing-table SELECT to fail")
        conn.execute("ROLLBACK TO SAVEPOINT stage51_probe")
        conn.execute("SELECT 1")
    finally:
        conn.rollback()


def run_app_setting_probe(repo: Repository, conn, key: str = APP_SETTING_PROBE_KEY) -> None:
    """Exercise app-settings visibility and restoration in one rollback-only transaction."""
    before = repo.get_app_setting_value(key)
    conn.rollback()
    conn.execute("BEGIN")
    try:
        repo.set_app_setting_value(key, "stage52-value", updated_by=None, commit=False)
        if repo.get_app_setting_value(key) != "stage52-value":
            raise AssertionError(f"{key}: setting is not visible inside the transaction")
        repo.delete_app_setting_value(key, commit=False)
        if repo.get_app_setting_value(key) is not None:
            raise AssertionError(f"{key}: deleted setting remains visible inside the transaction")
    finally:
        conn.rollback()
    try:
        if repo.get_app_setting_value(key) != before:
            raise AssertionError(f"{key}: rollback did not restore the prior setting value")
    finally:
        conn.rollback()


def _assert_usage(actual: dict[str, object], expected: dict[str, object]) -> None:
    for field, value in expected.items():
        if field in {"credits_spent_today", "last_check_credits"} and value is not None:
            if Decimal(str(actual[field])) != Decimal(str(value)):
                raise AssertionError(f"HLR usage {field} was {actual[field]!r}, expected {value!r}")
        elif field == "updated_at" and value is not None:
            timestamp = actual[field]
            if timestamp == value:
                continue
            if hasattr(timestamp, "strftime"):
                if timestamp.strftime("%Y-%m-%d %H:%M") == value:
                    continue
            elif str(timestamp).startswith(value):
                continue
            raise AssertionError(f"HLR usage {field} was {timestamp!r}, expected {value!r}")
        elif actual[field] != value:
            raise AssertionError(f"HLR usage {field} was {actual[field]!r}, expected {value!r}")


def run_hlr_daily_usage_probe(repo: Repository, conn, usage_date: str = HLR_DAILY_USAGE_PROBE_DATE) -> None:
    """Exercise HLR usage increments and prove the row is restored by rollback."""
    before = repo.get_hlr_daily_usage(usage_date)
    conn.rollback()
    conn.execute("BEGIN")
    try:
        repo.upsert_hlr_daily_usage(usage_date, 3, "0.75", "2099-12-31 10:00", commit=False)
        _assert_usage(repo.get_hlr_daily_usage(usage_date), {
            "checked_today": 3, "credits_spent_today": "0.75", "last_check_count": 3,
            "last_check_credits": "0.75", "updated_at": "2099-12-31 10:00",
        })
        repo.upsert_hlr_daily_usage(usage_date, 2, "0.25", "2099-12-31 10:05", commit=False)
        _assert_usage(repo.get_hlr_daily_usage(usage_date), {
            "checked_today": 5, "credits_spent_today": "1.0", "last_check_count": 2,
            "last_check_credits": "0.25", "updated_at": "2099-12-31 10:05",
        })
    finally:
        conn.rollback()
    try:
        _assert_usage(repo.get_hlr_daily_usage(usage_date), before)
    finally:
        conn.rollback()



def run_user_admin_probe(repo: Repository, conn, username: str = USER_ADMIN_PROBE_USERNAME) -> None:
    """Exercise all Stage 53 user/admin writes inside one rolled-back transaction."""
    before = repo.get_user_by_username(username)
    if before is not None:
        raise AssertionError(f"{username}: probe username collision")
    user_id = None
    conn.rollback()
    conn.execute("BEGIN")
    try:
        user_id = repo.create_user(username, role="admin", display_name="Stage 53 Probe", password="stage53-old-password", email="stage53-before@example.test", must_change_password=True, commit=False)
        user = repo.get_user_by_username(username)
        if not user or user["id"] != user_id or user["display_name"] != "Stage 53 Probe" or user["email"] != "stage53-before@example.test" or not bool(user["is_active"]) or not repo.authenticate_user(username, "stage53-old-password"):
            raise AssertionError(f"{username}: created user is not visible inside the transaction")
        repo.update_user(user_id, display_name="Stage 53 Probe Updated", role_key="admin", is_active=True, username=username, email="stage53-after@example.test", commit=False)
        user = repo.get_user_by_username(username)
        if not user or user["display_name"] != "Stage 53 Probe Updated" or user["email"] != "stage53-after@example.test" or user["role_key"] != "admin" or not bool(user["is_active"]):
            raise AssertionError(f"{username}: updated user is not visible inside the transaction")
        repo.set_user_permissions(user_id, {"routes": {"can_read": True, "can_write": True, "can_export": False}, "settings": {"can_read": True, "can_write": False, "can_export": False}}, commit=False)
        route = repo.get_user_section_permission(user_id, "routes")
        permissions = repo.get_user_permissions(user_id)
        if not route or not (bool(route["can_read"]) and bool(route["can_write"]) and not bool(route["can_export"])) or "settings" not in permissions:
            raise AssertionError(f"{username}: permissions are not visible inside the transaction")
        repo.update_user_password(user_id, "stage53-new-password", must_change_password=False, commit=False)
        user = repo.get_user_by_username(username)
        if not repo.authenticate_user(username, "stage53-new-password") or repo.authenticate_user(username, "stage53-old-password") or bool(user["must_change_password"]):
            raise AssertionError(f"{username}: password update is not visible inside the transaction")
    finally:
        conn.rollback()
    try:
        if repo.get_user_by_username(username) is not None:
            raise AssertionError(f"{username}: rollback did not remove probe user")
        if user_id is not None and repo.get_user_permissions(user_id):
            raise AssertionError(f"{username}: rollback did not remove probe permissions")
    finally:
        conn.rollback()


def _dictionary_probe_rows(conn) -> tuple[object, object, object, object]:
    """Read only the deterministic Stage 54 rows with PostgreSQL placeholders."""
    country = conn.execute("SELECT id, is_active FROM countries WHERE name = %s AND code = %s", (COUNTRY_PROBE_NAME, COUNTRY_PROBE_CODE)).fetchone()
    currency = conn.execute("SELECT id, is_active FROM currencies WHERE code = %s", (CURRENCY_PROBE_CODE,)).fetchone()
    provider = conn.execute("SELECT id, default_currency_id, is_active FROM providers WHERE name = %s", (PROVIDER_PROBE_NAME,)).fetchone()
    prefix = conn.execute("SELECT id, provider_id, prefix, is_active FROM provider_prefixes WHERE prefix = %s AND name = %s", (PREFIX_PROBE_VALUE, PREFIX_PROBE_NAME)).fetchone()
    return country, currency, provider, prefix


def run_dictionary_create_probe(repo: Repository, conn) -> None:
    """Create core dictionary rows in one transaction and prove rollback removes them."""
    if any(_dictionary_probe_rows(conn)):
        raise AssertionError("Stage 54 dictionary probe values already exist")
    conn.rollback()
    try:
        conn.execute("BEGIN")
        country_id = repo.create_country(COUNTRY_PROBE_NAME, COUNTRY_PROBE_CODE, commit=False)
        currency_id = repo.create_currency(CURRENCY_PROBE_CODE, CURRENCY_PROBE_NAME, CURRENCY_PROBE_SYMBOL, commit=False)
        provider_id = repo.create_provider(PROVIDER_PROBE_NAME, provider_type=PROVIDER_PROBE_TYPE, default_currency_id=currency_id, comment=PROVIDER_PROBE_COMMENT, commit=False)
        prefix_id = repo.create_prefix(provider_id, PREFIX_PROBE_VALUE, PREFIX_PROBE_NAME, commit=False)
        country, currency, provider, prefix = _dictionary_probe_rows(conn)
        if not country or country["id"] != country_id or not bool(country["is_active"]):
            raise AssertionError("country is not visible and active inside the transaction")
        if not currency or currency["id"] != currency_id or not bool(currency["is_active"]):
            raise AssertionError("currency is not visible and active inside the transaction")
        if not provider or provider["id"] != provider_id or provider["default_currency_id"] != currency_id or not bool(provider["is_active"]):
            raise AssertionError("provider is not visible and active inside the transaction")
        if not prefix or prefix["id"] != prefix_id or prefix["provider_id"] != provider_id or prefix["prefix"] != PREFIX_PROBE_VALUE or not bool(prefix["is_active"]):
            raise AssertionError("prefix is not visible and active inside the transaction")
    finally:
        conn.rollback()
    try:
        if any(_dictionary_probe_rows(conn)):
            raise AssertionError("rollback did not remove Stage 54 dictionary probe rows")
    finally:
        conn.rollback()
def _dictionary_get_or_create_probe_rows(conn) -> tuple[object, object, object, object]:
    """Read only the deterministic Stage 55 rows with PostgreSQL placeholders."""
    country = conn.execute("SELECT id, is_active FROM countries WHERE name = %s", (GOC_COUNTRY_PROBE_NAME,)).fetchone()
    currency = conn.execute("SELECT id, is_active FROM currencies WHERE code = %s", (GOC_CURRENCY_PROBE_CODE,)).fetchone()
    provider = conn.execute("SELECT id, normalized_name, default_currency_id, is_active FROM providers WHERE name = %s", (GOC_PROVIDER_PROBE_NAME,)).fetchone()
    prefix = conn.execute("SELECT id, provider_id, prefix, is_active FROM provider_prefixes WHERE prefix = %s", (GOC_PREFIX_PROBE_VALUE,)).fetchone()
    return country, currency, provider, prefix


def run_dictionary_get_or_create_probe(repo: Repository, conn) -> None:
    """Exercise create and existing dictionary paths, then prove rollback cleanup."""
    if any(_dictionary_get_or_create_probe_rows(conn)):
        raise AssertionError("Stage 55 dictionary get-or-create probe values already exist")
    conn.rollback()
    try:
        conn.execute("BEGIN")
        country_id = repo.get_or_create_country(GOC_COUNTRY_PROBE_NAME, commit=False)
        country_id_again = repo.get_or_create_country(GOC_COUNTRY_PROBE_NAME, commit=False)
        country, _, _, _ = _dictionary_get_or_create_probe_rows(conn)
        if country_id_again != country_id or not country or country["id"] != country_id or not bool(country["is_active"]):
            raise AssertionError("country create/existing path is not visible and active inside the transaction")
        currency_id = repo.get_or_create_currency(GOC_CURRENCY_PROBE_CODE, commit=False)
        currency_id_again = repo.get_or_create_currency(GOC_CURRENCY_PROBE_CODE, commit=False)
        _, currency, _, _ = _dictionary_get_or_create_probe_rows(conn)
        if currency_id_again != currency_id or not currency or currency["id"] != currency_id or not bool(currency["is_active"]):
            raise AssertionError("currency create/existing path is not visible and active inside the transaction")
        provider_id = repo.get_or_create_provider(GOC_PROVIDER_PROBE_NAME, currency_id=currency_id, commit=False)
        provider_id_again = repo.get_or_create_provider(GOC_PROVIDER_PROBE_NAME, currency_id=currency_id, commit=False)
        _, _, provider, _ = _dictionary_get_or_create_probe_rows(conn)
        if provider_id_again != provider_id or not provider or provider["id"] != provider_id or not bool(provider["is_active"]) or provider["normalized_name"] != normalize_provider_name(GOC_PROVIDER_PROBE_NAME) or provider.get("default_currency_id") != currency_id:
            raise AssertionError("provider create/existing path is not visible and active inside the transaction")
        prefix_id = repo.get_or_create_prefix(provider_id, GOC_PREFIX_PROBE_VALUE, commit=False)
        prefix_id_again = repo.get_or_create_prefix(provider_id, GOC_PREFIX_PROBE_VALUE, commit=False)
        no_prefix_id = repo.get_or_create_prefix(provider_id, "без префикса", commit=False)
        _, _, _, prefix = _dictionary_get_or_create_probe_rows(conn)
        if prefix_id_again != prefix_id or no_prefix_id is not None or not prefix or prefix["id"] != prefix_id or prefix["provider_id"] != provider_id or prefix["prefix"] != GOC_PREFIX_PROBE_VALUE or not bool(prefix["is_active"]):
            raise AssertionError("prefix create/existing path is not visible and active inside the transaction")
    finally:
        conn.rollback()
    try:
        if any(_dictionary_get_or_create_probe_rows(conn)):
            raise AssertionError("rollback did not remove Stage 55 dictionary get-or-create probe rows")
    finally:
        conn.rollback()


def _dictionary_ensure_probe_rows(conn) -> tuple[object, object, object]:
    """Read only the deterministic Stage 56 rows with PostgreSQL placeholders."""
    project = conn.execute("SELECT name, is_active FROM projects WHERE name = %s", (PROJECT_PROBE_NAME,)).fetchone()
    phone_type = conn.execute("SELECT name, is_active FROM phone_number_types WHERE name = %s", (PHONE_NUMBER_TYPE_PROBE_NAME,)).fetchone()
    assignment = conn.execute("SELECT code, name, is_active FROM phone_assignment_types WHERE code = %s", (PHONE_ASSIGNMENT_CODE,)).fetchone()
    return project, phone_type, assignment


def run_dictionary_ensure_probe(repo: Repository, conn) -> None:
    """Exercise insert-ignore dictionary ensures and prove rollback cleanup."""
    if any(_dictionary_ensure_probe_rows(conn)):
        raise AssertionError("Stage 56 dictionary ensure probe values already exist")
    conn.rollback()
    try:
        conn.execute("BEGIN")
        project_inserted = repo.ensure_project_exists(PROJECT_PROBE_NAME, commit=False)
        project_existing = repo.ensure_project_exists(PROJECT_PROBE_NAME, commit=False)
        project, _, _ = _dictionary_ensure_probe_rows(conn)
        if project_inserted != 1 or project_existing != 0 or not project or not bool(project["is_active"]):
            raise AssertionError("project insert/ignore path is not visible and active inside the transaction")

        phone_type_inserted = repo.ensure_phone_number_type_exists(PHONE_NUMBER_TYPE_PROBE_NAME, commit=False)
        phone_type_existing = repo.ensure_phone_number_type_exists(PHONE_NUMBER_TYPE_PROBE_NAME, commit=False)
        _, phone_type, _ = _dictionary_ensure_probe_rows(conn)
        if phone_type_inserted != 1 or phone_type_existing != 0 or not phone_type or not bool(phone_type["is_active"]):
            raise AssertionError("phone number type insert/ignore path is not visible and active inside the transaction")

        assignment_inserted = repo.ensure_phone_assignment_type_exists(PHONE_ASSIGNMENT_CODE, PHONE_ASSIGNMENT_NAME, commit=False)
        assignment_existing = repo.ensure_phone_assignment_type_exists(PHONE_ASSIGNMENT_CODE, PHONE_ASSIGNMENT_NAME, commit=False)
        _, _, assignment = _dictionary_ensure_probe_rows(conn)
        if (assignment_inserted != 1 or assignment_existing != 0 or not assignment
                or assignment["code"] != PHONE_ASSIGNMENT_CODE
                or assignment["name"] != PHONE_ASSIGNMENT_NAME
                or not bool(assignment["is_active"])):
            raise AssertionError("phone assignment type insert/ignore path is not visible and active inside the transaction")
    finally:
        conn.rollback()
    try:
        if any(_dictionary_ensure_probe_rows(conn)):
            raise AssertionError("rollback did not remove Stage 56 dictionary ensure probe rows")
    finally:
        conn.rollback()


def _dictionary_server_probe_row(conn):
    """Read only the deterministic Stage 57 server row with PostgreSQL placeholders."""
    return conn.execute(
        "SELECT id, name, is_active FROM servers WHERE name = %s",
        (SERVER_PROBE_NAME,),
    ).fetchone()


def run_dictionary_server_probe(repo: Repository, conn) -> None:
    """Create one server in a caller-owned transaction and prove rollback cleanup."""
    if _dictionary_server_probe_row(conn) is not None:
        raise AssertionError("Stage 57 dictionary server probe value already exists")
    conn.rollback()
    try:
        conn.execute("BEGIN")
        server_id = repo.create_server(SERVER_PROBE_NAME, commit=False)
        server = _dictionary_server_probe_row(conn)
        if (not server or server["id"] != server_id or server["name"] != SERVER_PROBE_NAME
                or not bool(server["is_active"])):
            raise AssertionError("server is not visible and active inside the transaction")
    finally:
        conn.rollback()
    try:
        if _dictionary_server_probe_row(conn) is not None:
            raise AssertionError("rollback did not remove Stage 57 dictionary server probe row")
    finally:
        conn.rollback()


def _dictionary_change_reason_probe_rows(conn, reason_id=None):
    """Read the deterministic Stage 58 change reason and its audit row."""
    reason = conn.execute(
        "SELECT id, name, description, is_active FROM change_reasons WHERE name = %s",
        (CHANGE_REASON_PROBE_NAME,),
    ).fetchone()
    log = None
    if reason_id is not None:
        log = conn.execute(
            "SELECT entity_type, entity_id, change_type, changed_by, new_values, source "
            "FROM change_log WHERE entity_type = %s AND entity_id = %s AND change_type = %s",
            ("change_reason", reason_id, "change_reason.created"),
        ).fetchone()
    return reason, log


def run_dictionary_change_reason_probe(repo: Repository, conn) -> None:
    """Create a change reason and audit row, then prove rollback removes both."""
    if _dictionary_change_reason_probe_rows(conn)[0] is not None:
        raise AssertionError("Stage 58 change reason probe value already exists")
    reason_id = None
    conn.rollback()
    try:
        conn.execute("BEGIN")
        reason_id = repo.create_change_reason(
            CHANGE_REASON_PROBE_NAME, created_by=None,
            comment=CHANGE_REASON_PROBE_COMMENT, is_active=True, commit=False,
        )
        reason, log = _dictionary_change_reason_probe_rows(conn, reason_id)
        if (not reason or reason["id"] != reason_id or reason["name"] != CHANGE_REASON_PROBE_NAME
                or reason["description"] != CHANGE_REASON_PROBE_COMMENT or not bool(reason["is_active"])):
            raise AssertionError("change reason is not visible and active inside the transaction")
        if (not log or log["entity_type"] != "change_reason" or log["entity_id"] != reason_id
                or log["change_type"] != "change_reason.created" or log["changed_by"] is not None
                or ("source" in log and log["source"] != "ui")):
            raise AssertionError("change reason audit row is not visible inside the transaction")
        values = log.get("new_values") if hasattr(log, "get") else log["new_values"]
        if isinstance(values, str):
            if CHANGE_REASON_PROBE_NAME not in values:
                raise AssertionError("change reason audit values do not include the name")
        elif not isinstance(values, dict) or values.get("name") != CHANGE_REASON_PROBE_NAME:
            raise AssertionError("change reason audit values do not include the name")
    finally:
        conn.rollback()
    try:
        reason, log = _dictionary_change_reason_probe_rows(conn, reason_id)
        if reason is not None or log is not None:
            raise AssertionError("rollback did not remove Stage 58 change reason probe rows")
    finally:
        conn.rollback()


def run_dictionary_snapshot_probe(repo: Repository, conn) -> None:
    """Exercise every snapshot branch using transaction-local fixture updates only."""
    phone = conn.execute("SELECT * FROM phone_numbers ORDER BY id LIMIT 1").fetchone()
    route = conn.execute("SELECT * FROM routes ORDER BY id LIMIT 1").fetchone()
    rows = {
        "countries": conn.execute("SELECT id, name FROM countries ORDER BY id LIMIT 1").fetchone(),
        "providers": conn.execute("SELECT id, name FROM providers ORDER BY id LIMIT 1").fetchone(),
        "currencies": conn.execute("SELECT id, code FROM currencies ORDER BY id LIMIT 1").fetchone(),
        "phone-types": conn.execute("SELECT id, name FROM phone_number_types ORDER BY id LIMIT 1").fetchone(),
        "projects": conn.execute("SELECT id, name FROM projects ORDER BY id LIMIT 1").fetchone(),
        "phone-assignments": conn.execute("SELECT id, code, name FROM phone_assignment_types ORDER BY id LIMIT 1").fetchone(),
    }
    if not phone or not route or any(row is None for row in rows.values()):
        missing = [name for name, row in rows.items() if row is None]
        raise AssertionError(f"Stage 59 demo fixture is missing required rows: {', '.join(missing) or 'phone_numbers/routes'}")
    phone_id, route_id = phone["id"], route["id"]
    before_phone, before_route = dict(phone), dict(route)
    conn.rollback()
    labels = {"country": "__stage59_new_country_label__", "provider": "__stage59_new_provider_label__", "currency": "__stage59_unused_currency_label__", "phone_type": "__stage59_new_phone_type__", "project": "__stage59_new_project_label__", "assignment": "__stage59_new_assignment_label__"}
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE phone_numbers SET country_id=%s, provider_id=%s, currency_id=%s, phone_type=%s, project_label=%s, assignment_type=%s, country_label=%s, provider_label=%s, currency_label=%s, assignment_label=%s WHERE id=%s", (rows["countries"]["id"], rows["providers"]["id"], rows["currencies"]["id"], rows["phone-types"]["name"], rows["projects"]["name"], rows["phone-assignments"]["code"], "__stage59_old_country_label__", "__stage59_old_provider_label__", "__stage59_old_currency_label__", "__stage59_old_assignment_label__", phone_id))
        conn.execute("UPDATE routes SET project_label=%s WHERE id=%s", (rows["projects"]["name"], route_id))
        checks = (("countries", "name", "country_label", labels["country"]), ("providers", "name", "provider_label", labels["provider"]), ("currencies", "code", "currency_label", rows["currencies"]["code"]), ("phone-types", "name", "phone_type", labels["phone_type"]), ("projects", "name", "project_label", labels["project"]), ("phone-assignments", "name", "assignment_label", labels["assignment"]))
        for kind, old_field, field, expected in checks:
            result = repo.update_dictionary_snapshots(kind, rows[kind]["id"], rows[kind][old_field], labels.get(field.replace('_label', ''), labels.get(field, labels["currency"])))
            current = conn.execute(f"SELECT {field} FROM phone_numbers WHERE id=%s", (phone_id,)).fetchone()
            if (not isinstance(result, dict) or current[field] != expected
                    or (kind == "countries" and result.get("Купленные номера", 0) < 1)):
                raise AssertionError(f"{kind}: snapshot update is not visible inside the transaction")
        route_current = conn.execute("SELECT project_label FROM routes WHERE id=%s", (route_id,)).fetchone()
        if route_current["project_label"] != labels["project"]:
            raise AssertionError("projects: route snapshot update is not visible inside the transaction")
        repo.update_dictionary_snapshots("__stage59_unknown_kind__", 999999999, "old", "new")
    finally:
        conn.rollback()
    try:
        after_phone = conn.execute("SELECT * FROM phone_numbers WHERE id=%s", (phone_id,)).fetchone()
        after_route = conn.execute("SELECT * FROM routes WHERE id=%s", (route_id,)).fetchone()
        if dict(after_phone) != before_phone or dict(after_route) != before_route:
            raise AssertionError("Stage 59 rollback did not restore selected fixture rows")
        residual = conn.execute("SELECT 1 FROM phone_numbers WHERE country_label LIKE %s OR provider_label LIKE %s OR currency_label LIKE %s OR assignment_label LIKE %s OR phone_type LIKE %s OR project_label LIKE %s UNION ALL SELECT 1 FROM routes WHERE project_label LIKE %s", ("__stage59_%",) * 7).fetchone()
        if residual:
            raise AssertionError("Stage 59 labels remain after rollback")
    finally:
        conn.rollback()


def run_provider_change_priority_probe(repo: Repository, conn) -> None:
    """Exercise both server-priority update branches and prove rollback cleanup.

    The fixture preparation deliberately uses direct SQL only inside the
    transaction.  This probe does not exercise provider-change creation.
    """
    user = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    country = conn.execute(
        "SELECT country_id FROM routes GROUP BY country_id HAVING COUNT(*) >= 2 ORDER BY country_id LIMIT 1"
    ).fetchone()
    server = conn.execute("SELECT id FROM servers WHERE is_active = true ORDER BY id LIMIT 1").fetchone()
    if not server:
        server = conn.execute("SELECT id FROM servers WHERE is_active = 1 ORDER BY id LIMIT 1").fetchone()
    if not user or not country or not server:
        raise AssertionError("Stage 60 demo fixture is missing a user, country, or active server")
    routes = conn.execute("SELECT id FROM routes WHERE country_id = %s ORDER BY id LIMIT 2", (country["country_id"],)).fetchall()
    if len(routes) != 2:
        raise AssertionError("Stage 60 demo fixture requires two routes in one country")
    route_before_id, route_after_id = routes[0]["id"], routes[1]["id"]
    previous = conn.execute(
        "SELECT * FROM server_route_priorities WHERE country_id = %s AND server_id = %s",
        (country["country_id"], server["id"]),
    ).fetchone()
    priority_id = None
    conn.rollback()
    try:
        conn.execute("BEGIN")
        if previous:
            priority_id = previous["id"]
            conn.execute(
                "UPDATE server_route_priorities SET current_route_id=%s, previous_route_id=NULL, comment=%s, changed_by=%s, updated_by=%s WHERE id=%s",
                (route_before_id, "__stage60_priority_before__", user["id"], user["id"], priority_id),
            )
        else:
            priority_id = conn.execute(
                "INSERT INTO server_route_priorities (country_id, server_id, current_route_id, previous_route_id, reason, comment, created_by, updated_by) VALUES (%s, %s, %s, NULL, %s, %s, %s, %s) RETURNING id",
                (country["country_id"], server["id"], route_before_id, "__stage60_priority_reason__", "__stage60_priority_before__", user["id"], user["id"]),
            ).fetchone()["id"]
        repo.update_server_route_priority(priority_id=priority_id, current_route_id=route_after_id, comment=PRIORITY_CHANGED_COMMENT, changed_by=user["id"], commit=False)
        changed = conn.execute("SELECT current_route_id, previous_route_id, comment, changed_by, updated_by FROM server_route_priorities WHERE id=%s", (priority_id,)).fetchone()
        if not changed or (changed["current_route_id"], changed["previous_route_id"], changed["comment"], changed["changed_by"], changed["updated_by"]) != (route_after_id, route_before_id, PRIORITY_CHANGED_COMMENT, user["id"], user["id"]):
            raise AssertionError("Stage 60 route-changed priority state is not visible inside the transaction")
        log = conn.execute("SELECT * FROM change_log WHERE entity_type=%s AND entity_id=%s AND change_type=%s ORDER BY id DESC LIMIT 1", ("server_route_priority", priority_id, "server_route_priority.current_route_updated")).fetchone()
        if not log or log["changed_by"] != user["id"] or str(route_after_id) not in str(log["new_values"]) or PRIORITY_CHANGED_COMMENT not in str(log["new_values"]):
            raise AssertionError("Stage 60 route-changed change_log row is not visible inside the transaction")
        repo.update_server_route_priority(priority_id=priority_id, current_route_id=route_after_id, comment=PRIORITY_SAME_ROUTE_COMMENT, changed_by=user["id"], commit=False)
        same = conn.execute("SELECT current_route_id, previous_route_id, comment FROM server_route_priorities WHERE id=%s", (priority_id,)).fetchone()
        if not same or (same["current_route_id"], same["previous_route_id"], same["comment"]) != (route_after_id, route_before_id, PRIORITY_SAME_ROUTE_COMMENT):
            raise AssertionError("Stage 60 same-route priority state is not visible inside the transaction")
        log = conn.execute("SELECT new_values, summary FROM change_log WHERE entity_type=%s AND entity_id=%s AND change_type=%s ORDER BY id DESC LIMIT 1", ("server_route_priority", priority_id, "server_route_priority.current_route_updated")).fetchone()
        if not log or PRIORITY_SAME_ROUTE_COMMENT not in f"{log['new_values']} {log['summary']}":
            raise AssertionError("Stage 60 same-route change_log row is not visible inside the transaction")
        conn.execute("SAVEPOINT stage60_bad_route")
        try:
            repo.update_server_route_priority(priority_id=priority_id, current_route_id=-999999999, comment="bad", changed_by=user["id"], commit=False)
        except Exception as exc:
            if exc.__class__.__name__ != "BusinessRuleError":
                raise
        else:
            raise AssertionError("Stage 60 missing-route validation did not fail")
        finally:
            conn.execute("ROLLBACK TO SAVEPOINT stage60_bad_route")
            conn.execute("RELEASE SAVEPOINT stage60_bad_route")
    finally:
        conn.rollback()
    try:
        if conn.execute("SELECT 1 FROM server_route_priorities WHERE comment IN (%s, %s)", (PRIORITY_CHANGED_COMMENT, PRIORITY_SAME_ROUTE_COMMENT)).fetchone():
            raise AssertionError("Stage 60 priority comments remain after rollback")
        if conn.execute("SELECT 1 FROM change_log WHERE entity_type=%s AND (new_values::text LIKE %s OR new_values::text LIKE %s)", ("server_route_priority", "%stage60_priority_changed_comment%", "%stage60_priority_same_route_comment%")).fetchone():
            raise AssertionError("Stage 60 change_log rows remain after rollback")
    finally:
        conn.rollback()

def run_harness(postgres_url: str, probe_key: str = DEFAULT_PROBE_KEY, probe_value: str = DEFAULT_PROBE_VALUE) -> dict:
    """Run all probes; psycopg imports remain local so unit tests need no driver."""
    summary = empty_summary(postgres_url)
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        summary["failures"].append({"check": "psycopg_import", "error": sanitize_error(exc, postgres_url)})
        return summary

    conn = None
    def check(name, probe):
        try:
            probe()
            summary["checks_count"] += 1
            summary["probes"][name] = "ok"
        except Exception as exc:
            summary["failures"].append({"check": name, "error": sanitize_error(exc, postgres_url)})
            summary["probes"][name] = "failed"

    try:
        conn = psycopg.connect(postgres_url, row_factory=dict_row)
        repo = Repository(conn, backend="postgres")
        check("rollback_probe", lambda: run_rollback_probe(repo, conn, probe_key, probe_value))
        check("aborted_transaction_probe", lambda: run_aborted_transaction_probe(conn))
        check("savepoint_probe", lambda: run_savepoint_probe(conn))
        check("app_setting_probe", lambda: run_app_setting_probe(repo, conn))
        check("hlr_daily_usage_probe", lambda: run_hlr_daily_usage_probe(repo, conn))
        check("user_admin_probe", lambda: run_user_admin_probe(repo, conn))
        check("dictionary_create_probe", lambda: run_dictionary_create_probe(repo, conn))
        check("dictionary_get_or_create_probe", lambda: run_dictionary_get_or_create_probe(repo, conn))
        check("dictionary_ensure_probe", lambda: run_dictionary_ensure_probe(repo, conn))
        check("dictionary_server_probe", lambda: run_dictionary_server_probe(repo, conn))
        check("dictionary_change_reason_probe", lambda: run_dictionary_change_reason_probe(repo, conn))
        check("dictionary_snapshot_probe", lambda: run_dictionary_snapshot_probe(repo, conn))
        check("provider_change_priority_probe", lambda: run_provider_change_priority_probe(repo, conn))
    except Exception as exc:
        summary["failures"].append({"check": "connect", "error": sanitize_error(exc, postgres_url)})
    finally:
        if conn is not None:
            try:
                conn.rollback()
            finally:
                conn.close()
    summary["status"] = "ok" if not summary["failures"] else "failed"
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--probe-key", default=DEFAULT_PROBE_KEY)
    parser.add_argument("--probe-value", default=DEFAULT_PROBE_VALUE)
    args = parser.parse_args(argv)
    if not args.postgres_url:
        parser.error("--postgres-url or DATABASE_URL is required")
    summary = run_harness(args.postgres_url, args.probe_key, args.probe_value)
    rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    if args.json or not args.output:
        print(rendered, end="")
    return 0 if summary["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
