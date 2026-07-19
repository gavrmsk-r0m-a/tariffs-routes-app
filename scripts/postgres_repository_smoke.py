#!/usr/bin/env python3
"""Read-only smoke checks for adapter-ready Repository methods on PostgreSQL."""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.repository import Repository


SMOKE_METHODS = (
    "list_countries", "get_country", "list_countries_by_ids", "list_currencies",
    "list_providers", "list_providers_with_currency", "list_projects", "list_servers",
    "list_phone_number_types", "list_phone_assignment_types", "list_provider_prefixes",
    "list_provider_prefixes_with_provider", "list_active_change_reasons",
    "list_change_reasons", "dictionary_counts", "get_country_by_name",
    "get_provider_by_normalized_name", "get_currency_by_code", "get_project_by_name",
    "get_phone_number_type_by_name", "get_phone_assignment_type_by_code_or_name",
    "get_server_by_name", "route_exists_by_country_name_and_name",
    "phone_number_exists_by_normalized_number",
    "calling_company_exists_by_server_country_external_id",
    "current_tariff_exists_by_country_provider_prefix",
    "get_phone_number_import_identity_by_normalized_number",
    "get_app_setting_value", "get_hlr_daily_usage", "get_hlr_limit_override",
    "list_calling_companies", "get_calling_company", "latest_currency_rate",
    "get_currency_rate",
    "dictionary_rename_preview", "get_user_section_permission", "get_user_permissions",
    "get_phone_number", "get_route", "route_numbers", "find_tariff_by_identity",
    "get_tariff",
    "list_users", "get_user", "get_user_by_username", "authenticate_user",
    "list_routes", "list_tariffs", "list_phone_numbers",
    "list_company_routing_settings", "get_company_routing_setting",
    "list_provider_changes", "list_routing_events", "get_routing_event",
    "list_phone_history", "list_route_history", "list_tariff_history",
    "list_company_routing_setting_history", "list_calling_company_history",
    "list_calling_company_events", "count_calling_company_events",
)

STAGE_34_METHODS = (
    "get_app_setting_value", "get_hlr_daily_usage", "get_hlr_limit_override",
    "list_calling_companies", "get_calling_company", "latest_currency_rate",
    "get_currency_rate",
)

STAGE_35_METHODS = (
    "dictionary_rename_preview", "get_user_section_permission", "get_user_permissions",
    "get_phone_number", "get_route", "route_numbers", "find_tariff_by_identity",
    "get_tariff",
)

STAGE_36_METHODS = (
    "list_users", "get_user", "get_user_by_username", "authenticate_user",
)

STAGE_37_METHODS = (
    "list_routes",
)

STAGE_38_METHODS = (
    "list_tariffs",
)

STAGE_39_METHODS = (
    "list_calling_companies",
)

STAGE_40_METHODS = (
    "list_phone_numbers",
)

STAGE_41_METHODS = (
    "list_company_routing_settings",
    "get_company_routing_setting",
)

STAGE_42_METHODS = (
    "list_provider_changes",
)

STAGE_43_METHODS = (
    "list_routing_events",
    "get_routing_event",
)

STAGE_46_METHODS = (
    "list_phone_history",
    "list_route_history",
    "list_tariff_history",
)

STAGE_47_METHODS = (
    "list_company_routing_setting_history",
)

STAGE_48_METHODS = (
    "list_calling_company_history",
)

STAGE_49_METHODS = (
    "list_calling_company_events",
    "count_calling_company_events",
)

EXISTS_CHECKS = (
    ("route_exists_by_country_name_and_name", ("Demo Country", "Demo Route"), True),
    ("phone_number_exists_by_normalized_number", ("525550000001",), True),
    (
        "calling_company_exists_by_server_country_external_id",
        ("demo-server-1", "Demo Country", "demo-company-1"),
        True,
    ),
    (
        "current_tariff_exists_by_country_provider_prefix",
        ("Demo Country", "Demo Provider", "123"),
        True,
    ),
    ("route_exists_by_country_name_and_name", ("Demo Country", "Missing Route"), False),
    ("phone_number_exists_by_normalized_number", ("525559999999",), False),
    (
        "calling_company_exists_by_server_country_external_id",
        ("demo-server-1", "Demo Country", "missing-company"),
        False,
    ),
    (
        "current_tariff_exists_by_country_provider_prefix",
        ("Demo Country", "Demo Provider", "999999"),
        False,
    ),
)


def mask_postgres_url(url: str) -> str:
    """Return a display-safe URL which never contains the password."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    user = parts.username or ""
    return urlunsplit((parts.scheme, f"{user}:***@{host}", parts.path, parts.query, parts.fragment))


def sanitize_error(message: object, postgres_url: str) -> str:
    """Remove both the full connection URL and its password from diagnostics."""
    text = str(message).replace(postgres_url, mask_postgres_url(postgres_url))
    password = urlsplit(postgres_url).password
    return text.replace(password, "***") if password else text


def empty_summary(postgres_url: str) -> dict:
    return {"status": "failed", "postgres_url": mask_postgres_url(postgres_url), "checks_count": 0, "failures": []}


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _decimal_equals(value: object, expected: str) -> bool:
    """Compare SQLite/PostgreSQL numeric values without depending on scale."""
    try:
        return Decimal(str(value)) == Decimal(expected)
    except (InvalidOperation, TypeError, ValueError):
        return False


def _is_database_true(value: object) -> bool:
    """Accept only SQLite's integer true or PostgreSQL's native boolean true."""
    return value is True or (type(value) is int and value == 1)


def _is_database_false(value: object) -> bool:
    """Accept only SQLite's integer false or PostgreSQL's native boolean false."""
    return value is False or (type(value) is int and value == 0)


def _snapshot_object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _row_keys(row: object) -> set[str]:
    return set(row.keys())


def run_exists_checks(repo: Repository, check) -> None:
    """Run positive and negative semantic checks for read-only exists methods."""
    for method_name, args, expected in EXISTS_CHECKS:
        suffix = "existing" if expected else "missing"
        check(
            f"{method_name}_{suffix}",
            lambda name=method_name, values=args, wanted=expected, kind=suffix: _check(
                getattr(repo, name)(*values) is wanted,
                f"{name} must return {wanted} for {kind} demo data",
            ),
        )


def run_stage_34_checks(repo: Repository, check, currencies: list[dict]):
    """Check the Stage 34 batch against deterministic migration-demo values."""
    check("get_app_setting_value", lambda: _check(repo.get_app_setting_value("demo_setting") == "enabled", "demo_setting must equal enabled"))
    check("get_app_setting_value_missing", lambda: _check(repo.get_app_setting_value("missing_setting") is None, "missing setting must return None"))

    usage = check("get_hlr_daily_usage", lambda: repo.get_hlr_daily_usage("2026-07-12"))
    check("get_hlr_daily_usage_values", lambda: _check(usage is not None and usage["checked_today"] == 1 and _decimal_equals(usage["credits_spent_today"], "0.5"), "demo HLR usage values are incorrect"))
    check("get_hlr_daily_usage_missing", lambda: _check(repo.get_hlr_daily_usage("1999-01-01")["checked_today"] == 0, "missing HLR usage must have zero checks"))
    check("get_hlr_limit_override", lambda: _check(repo.get_hlr_limit_override() == "2500", "demo HLR limit override must equal 2500"))

    companies = check("list_calling_companies", repo.list_calling_companies)
    company = next((row for row in (companies or []) if row["company_id_external"] == "demo-company-1"), None)
    check("list_calling_companies_values", lambda: _check(company is not None and company["company_name"] == "Demo Company" and company["server_name"] == "demo-server-1", "demo calling company is incorrect"))
    company_detail = check("get_calling_company", lambda: repo.get_calling_company(company["id"]) if company else None)
    check("get_calling_company_values", lambda: _check(company_detail is not None and company_detail["country_name"] == "Demo Country" and company_detail["company_id_external"] == "demo-company-1", "calling company detail is incorrect"))
    check("get_calling_company_missing", lambda: _check(repo.get_calling_company(-1) is None, "missing calling company must return None"))

    eur = next((row for row in (currencies or []) if row["code"] == "EUR"), None)
    latest_rate = check("latest_currency_rate", lambda: repo.latest_currency_rate(eur["id"]) if eur else None)
    check("latest_currency_rate_values", lambda: _check(latest_rate is not None and _decimal_equals(latest_rate["rate_to_eur"], "1") and str(latest_rate["rate_date"]) == "2026-07-12", "latest EUR rate is incorrect"))
    check("latest_currency_rate_missing", lambda: _check(repo.latest_currency_rate(-1) is None, "missing latest rate must return None"))
    rate = check("get_currency_rate", lambda: repo.get_currency_rate(latest_rate["id"]) if latest_rate else None)
    check("get_currency_rate_values", lambda: _check(rate is not None and rate["currency_code"] == "EUR" and rate["source"] == "manual", "currency rate detail is incorrect"))
    check("get_currency_rate_missing", lambda: _check(repo.get_currency_rate(-1) is None, "missing currency rate must return None"))
    return company, company_detail


def run_stage_35_checks(repo: Repository, check, collections: dict, lookups: dict, company, company_detail) -> None:
    """Check detail, relation, permission, and count reads added in Stage 35."""
    entities = {
        "countries": lookups["get_country_by_name"],
        "providers": lookups["get_provider_by_normalized_name"],
        "currencies": lookups["get_currency_by_code"],
        "phone-types": next((row for row in collections["list_phone_number_types"] if row["name"] == "Mobile"), None),
        "projects": next((row for row in collections["list_projects"] if row.get("code") == "rep"), None),
        "phone-assignments": next((row for row in collections["list_phone_assignment_types"] if row["code"] == "gl"), None),
    }
    expected_previews = {
        "countries": {"Купленные номера": 1, "Маршруты": 1, "Тарифы": 1},
        "providers": {"Купленные номера": 1, "Маршруты": 1, "Тарифы": 1},
        "currencies": {"Купленные номера": 1, "Тарифы": 1},
        "phone-types": {"Купленные номера": 1},
        "projects": {"Купленные номера": 1, "Маршруты": 1},
        "phone-assignments": {"Купленные номера": 1},
    }
    for kind, expected in expected_previews.items():
        preview = check(f"dictionary_rename_preview_{kind}", lambda kind=kind: repo.dictionary_rename_preview(kind, entities[kind]["id"]))
        check(f"dictionary_rename_preview_{kind}_values", lambda actual=preview, wanted=expected, kind=kind: _check(actual == wanted, f"{kind} rename preview must equal {wanted}"))
    check("dictionary_rename_preview_unknown", lambda: _check(repo.dictionary_rename_preview("unknown", -1) == {}, "unknown rename preview must be empty"))

    user_id = company_detail["created_by"] if company_detail else None
    permission = check("get_user_section_permission", lambda: repo.get_user_section_permission(user_id, "routes"))
    check("get_user_section_permission_values", lambda: _check(permission is not None and all(_is_database_true(permission[key]) for key in ("can_read", "can_write", "can_export")), "routes permission flags must all be database true"))
    check("get_user_section_permission_missing", lambda: _check(repo.get_user_section_permission(user_id, "missing-section") is None, "missing section permission must return None"))
    permissions = check("get_user_permissions", lambda: repo.get_user_permissions(user_id))
    check("get_user_permissions_values", lambda: _check("routes" in (permissions or {}) and all(_is_database_true(permissions["routes"][key]) for key in ("can_read", "can_write", "can_export")), "permission map must contain the demo routes flags"))
    check("get_user_permissions_missing", lambda: _check(repo.get_user_permissions(-1) == {}, "missing user permissions must be empty"))

    phone_identity = lookups["get_phone_number_import_identity_by_normalized_number"]
    phone = check("get_phone_number", lambda: repo.get_phone_number(phone_identity["id"]))
    check("get_phone_number_values", lambda: _check(phone is not None and (phone["normalized_number"] or phone["number"]) == "525550000001" and phone["country_name"] == "Demo Country" and phone["provider_name"] == "Demo Provider", "demo phone detail is incorrect"))
    check("get_phone_number_missing", lambda: _check(repo.get_phone_number(-1) is None, "missing phone must return None"))

    route_id = company["current_route_id"] if company else None
    route = check("get_route", lambda: repo.get_route(route_id))
    check("get_route_values", lambda: _check(route is not None and route["name"] == "Demo Route" and route["country_name"] == "Demo Country" and route["provider_name"] == "Demo Provider", "demo route detail is incorrect"))
    check("get_route_missing", lambda: _check(repo.get_route(-1) is None, "missing route must return None"))
    numbers = check("route_numbers", lambda: repo.route_numbers(route_id))
    demo_number = next((row for row in (numbers or []) if row["number"] == "525550000001"), None)
    check("route_numbers_values", lambda: _check(demo_number is not None and demo_number["usage_type"] == "cli" and _is_database_true(demo_number["is_active"]), "demo route number relation is incorrect"))
    check("route_numbers_missing", lambda: _check(repo.route_numbers(-1) == [], "missing route numbers must be empty"))

    tariff = check("find_tariff_by_identity", lambda: repo.find_tariff_by_identity(entities["countries"]["id"], entities["providers"]["id"], next(row["id"] for row in collections["list_provider_prefixes"] if row["prefix"] == "123")))
    check("find_tariff_by_identity_values", lambda: _check(tariff is not None and tariff["country_name"] == "Demo Country" and tariff["provider_name"] == "Demo Provider" and tariff["prefix"] == "123", "demo tariff identity is incorrect"))
    check("find_tariff_by_identity_missing", lambda: _check(repo.find_tariff_by_identity(entities["countries"]["id"], -1, None) is None, "missing tariff identity must return None"))
    tariff_detail = check("get_tariff", lambda: repo.get_tariff(tariff["id"]) if tariff else None)
    check("get_tariff_values", lambda: _check(tariff_detail is not None and tariff_detail["id"] == tariff["id"] and tariff_detail["currency_code"] == "EUR" and _decimal_equals(tariff_detail["price_in_provider_currency"], "0.1") and _decimal_equals(tariff_detail["conversion_rate_to_eur"], "1") and _decimal_equals(tariff_detail["eur_price"], "0.1") and _is_database_true(tariff_detail["is_current"]), "demo tariff detail is incorrect"))
    check("get_tariff_missing", lambda: _check(repo.get_tariff(-1) is None, "missing tariff must return None"))


def run_stage_36_checks(repo: Repository, check) -> None:
    """Check user reads and local password verification without exposing secrets."""
    users = check("list_users", lambda: repo.list_users(active_only=False))
    admin = next((row for row in (users or []) if row["username"] == "admin"), None)
    inactive = next((row for row in (users or []) if row["username"] == "ci-inactive"), None)
    check("list_users_admin_present", lambda: _check(admin is not None, "admin must be listed"))
    check("list_users_inactive_present", lambda: _check(inactive is not None, "inactive fixture must be listed"))
    check("list_users_admin_display_name", lambda: _check(admin is not None and admin["display_name"] == "Admin", "admin display name is incorrect"))
    check("list_users_admin_role", lambda: _check(admin is not None and admin["role_key"] == "admin", "admin role is incorrect"))
    check("list_users_admin_active", lambda: _check(admin is not None and _is_database_true(admin["is_active"]), "admin active flag must be database true"))
    check("list_users_admin_password_change", lambda: _check(admin is not None and _is_database_false(admin["must_change_password"]), "admin password-change flag must be database false"))
    check("list_users_inactive_role", lambda: _check(inactive is not None and inactive["role_key"] == "guest", "inactive fixture role is incorrect"))
    check("list_users_inactive_flag", lambda: _check(inactive is not None and _is_database_false(inactive["is_active"]), "inactive fixture flag must be database false"))
    check("list_users_active_first", lambda: _check(not users or [bool(row["is_active"]) for row in users] == sorted((bool(row["is_active"]) for row in users), reverse=True), "active users must precede inactive users"))
    check("list_users_excludes_credentials", lambda: _check(all({"password_hash", "password_salt"}.isdisjoint(_row_keys(row)) for row in (users or [])), "user list must exclude credential columns"))

    active_users = check("list_users_active_only", lambda: repo.list_users(active_only=True))
    check("list_users_active_admin", lambda: _check(any(row["username"] == "admin" for row in (active_users or [])), "active list must contain admin"))
    check("list_users_active_excludes_inactive", lambda: _check(all(row["username"] != "ci-inactive" for row in (active_users or [])), "active list must exclude inactive fixture"))
    check("list_users_active_flags", lambda: _check(bool(active_users) and all(_is_database_true(row["is_active"]) for row in active_users), "active list flags must be database true"))

    admin_detail = check("get_user", lambda: repo.get_user(admin["id"]) if admin is not None else None)
    check("get_user_values", lambda: _check(admin_detail is not None and admin_detail["username"] == "admin" and admin_detail["display_name"] == "Admin" and admin_detail["role_key"] == "admin", "admin detail is incorrect"))
    check("get_user_excludes_credentials", lambda: _check(admin_detail is not None and {"password_hash", "password_salt"}.isdisjoint(_row_keys(admin_detail)), "user detail must exclude credential columns"))
    check("get_user_missing", lambda: _check(repo.get_user(-1) is None, "missing user ID must return None"))

    login_user = check("get_user_by_username", lambda: repo.get_user_by_username(" admin "))
    check("get_user_by_username_values", lambda: _check(login_user is not None and login_user["username"] == "admin" and login_user["display_name"] == "Admin" and login_user["role_key"] == "admin", "trimmed admin lookup is incorrect"))
    check("get_user_by_username_credentials_present", lambda: _check(login_user is not None and bool(login_user.get("password_hash") if isinstance(login_user, dict) else login_user["password_hash"]) and (login_user.get("password_salt") if isinstance(login_user, dict) else login_user["password_salt"]) is not None, "authentication credentials must be available to Repository verification"))
    check("get_user_by_username_missing", lambda: _check(repo.get_user_by_username("missing-user") is None, "missing username must return None"))

    authenticated = check("authenticate_user", lambda: repo.authenticate_user("admin", "admin"))
    check("authenticate_user_valid", lambda: _check(authenticated is not None and authenticated["username"] == "admin", "valid admin credentials must authenticate"))
    check("authenticate_user_wrong_password", lambda: _check(repo.authenticate_user("admin", "incorrect") is None, "wrong password must not authenticate"))
    check("authenticate_user_missing", lambda: _check(repo.authenticate_user("missing-user", "anything") is None, "missing username must not authenticate"))
    check("authenticate_user_inactive", lambda: _check(repo.authenticate_user("ci-inactive", "anything") is None, "inactive user must not authenticate"))


def run_stage_37_checks(repo: Repository, check, collections: dict, lookups: dict) -> None:
    """Check the Stage 37 route list and each supported filter independently."""
    country = lookups["get_country_by_name"]
    provider = lookups["get_provider_by_normalized_name"]
    prefix = next((row for row in collections["list_provider_prefixes"] if row["prefix"] == "123"), None)

    def demo_route(rows):
        return next((row for row in (rows or []) if row["name"] == "Demo Route"), None)

    def includes(filters):
        return demo_route(repo.list_routes(filters)) is not None

    routes = check("list_routes", repo.list_routes)
    route = demo_route(routes)
    check("list_routes_demo_present", lambda: _check(route is not None, "unfiltered routes must contain Demo Route"))
    check("list_routes_country_name", lambda: _check(route is not None and route["country_name"] == "Demo Country", "Demo Route country is incorrect"))
    check("list_routes_provider_name", lambda: _check(route is not None and route["provider_name"] == "Demo Provider", "Demo Route provider is incorrect"))
    check("list_routes_prefix", lambda: _check(route is not None and route["prefix"] == "123", "Demo Route prefix is incorrect"))
    check("list_routes_phone_count", lambda: _check(route is not None and route["phone_count"] == 1, "Demo Route phone count must be one"))
    check("list_routes_country_id_filter", lambda: _check(includes({"country_id": country["id"]}), "country_id must return Demo Route"))
    check("list_routes_provider_id_filter", lambda: _check(includes({"provider_id": provider["id"]}), "provider_id must return Demo Route"))
    check("list_routes_country_provider_filter", lambda: _check(includes({"country_id": country["id"], "provider_id": provider["id"]}), "combined equality filters must return Demo Route"))
    for value, suffix in (("1", "string_true"), (1, "integer_true"), (True, "boolean_true")):
        check(f"list_routes_is_actual_{suffix}", lambda value=value: _check(includes({"is_actual": value}), f"is_actual={value!r} must return Demo Route"))
    for value, suffix in (("0", "string_false"), (False, "boolean_false")):
        check(f"list_routes_is_actual_{suffix}", lambda value=value: _check(not includes({"is_actual": value}), f"is_actual={value!r} must exclude Demo Route"))
    check("list_routes_prefix_id_filter", lambda: _check(prefix is not None and includes({"prefix_id": prefix["id"]}), "prefix_id must return Demo Route"))
    check("list_routes_prefix_none_filter", lambda: _check(not includes({"prefix_id": "__none__"}), "null prefix filter must exclude Demo Route"))
    check("list_routes_prefix_missing_filter", lambda: _check(repo.list_routes({"prefix_id": -1}) == [], "missing prefix must return an empty list"))
    for value, suffix in (("demo route", "lowercase"), ("DeMo RoUtE", "mixed_case"), ("  demo route  ", "trimmed"), ("emo Rou", "partial")):
        check(f"list_routes_search_{suffix}", lambda value=value: _check(includes({"search_like": value}), f"search {value!r} must return Demo Route"))
    check("list_routes_search_missing", lambda: _check(repo.list_routes({"search_like": "missing route text"}) == [], "missing search must return an empty list"))
    check("list_routes_search_literal_underscore", lambda: _check(not includes({"search_like": "Demo_Route"}), "underscore must be literal"))
    check("list_routes_search_literal_percent", lambda: _check(not includes({"search_like": "Demo%Route"}), "percent must be literal"))
    combined = {"country_id": country["id"], "provider_id": provider["id"], "prefix_id": prefix["id"], "is_actual": "1", "search_like": "demo route"}
    check("list_routes_combined_filter", lambda: _check([row["name"] for row in repo.list_routes(combined)] == ["Demo Route"], "combined filters must return exactly Demo Route"))



def run_stage_38_checks(repo: Repository, check, collections: dict, lookups: dict) -> None:
    """Check list_tariffs status contracts and equality filters."""
    country = lookups["get_country_by_name"]
    provider = lookups["get_provider_by_normalized_name"]
    expected_keys = [
        "id", "country_id", "provider_id", "provider_prefix_id", "provider_currency_id",
        "price_in_provider_currency", "conversion_rate_to_eur", "conversion_rate_date",
        "currency_rate_id", "eur_price", "priority_status", "is_estimated", "comment",
        "valid_from", "valid_to", "is_current", "created_by", "created_at", "updated_by",
        "updated_at", "country_name", "provider_name", "prefix", "currency_code",
    ]

    def demo_row(rows):
        return next((row for row in (rows or []) if row["country_name"] == "Demo Country" and row["provider_name"] == "Demo Provider"), None)

    def inactive_row(rows):
        return next((row for row in (rows or []) if row["country_name"] == "Inactive Tariff Country" and row["provider_name"] == "Inactive Tariff Provider"), None)

    default_rows = check("list_tariffs", repo.list_tariffs)
    default_demo = demo_row(default_rows)
    check("list_tariffs_default_contains_demo", lambda: _check(default_demo is not None, "default tariffs must contain Demo active tariff"))
    check("list_tariffs_default_excludes_inactive", lambda: _check(inactive_row(default_rows) is None, "default tariffs must exclude inactive fixture"))
    check("list_tariffs_default_demo_values", lambda: _check(default_demo is not None and default_demo["country_name"] == "Demo Country" and default_demo["provider_name"] == "Demo Provider" and default_demo["prefix"] == "123" and default_demo["currency_code"] == "EUR" and _is_database_true(default_demo["is_current"]) and _is_database_false(default_demo["is_estimated"]) and _decimal_equals(default_demo["price_in_provider_currency"], "0.1") and _decimal_equals(default_demo["conversion_rate_to_eur"], "1") and _decimal_equals(default_demo["eur_price"], "0.1"), "default Demo tariff values are incorrect"))
    empty_rows = check("list_tariffs_empty_filters", lambda: repo.list_tariffs({}))
    check("list_tariffs_empty_filters_active_only", lambda: _check(demo_row(empty_rows) is not None and inactive_row(empty_rows) is None, "empty filters must use active-only contract"))

    active_rows = check("list_tariffs_active", lambda: repo.list_tariffs({"status": "active"}))
    check("list_tariffs_active_contains_demo", lambda: _check(demo_row(active_rows) is not None, "active tariffs must contain Demo tariff"))
    check("list_tariffs_active_excludes_inactive", lambda: _check(inactive_row(active_rows) is None, "active tariffs must exclude inactive fixture"))
    check("list_tariffs_active_all_current", lambda: _check(all(_is_database_true(row["is_current"]) for row in (active_rows or [])), "active tariffs must all be database true"))

    inactive_rows = check("list_tariffs_inactive", lambda: repo.list_tariffs({"status": "inactive"}))
    inactive = inactive_row(inactive_rows)
    check("list_tariffs_inactive_contains_fixture", lambda: _check(inactive is not None, "inactive tariffs must contain inactive fixture"))
    check("list_tariffs_inactive_excludes_demo", lambda: _check(demo_row(inactive_rows) is None, "inactive tariffs must exclude Demo active tariff"))
    check("list_tariffs_inactive_values", lambda: _check(inactive is not None and inactive["country_name"] == "Inactive Tariff Country" and inactive["provider_name"] == "Inactive Tariff Provider" and inactive["prefix"] is None and inactive["currency_code"] == "XTS" and inactive["priority_status"] == "alternative" and _is_database_false(inactive["is_current"]) and _is_database_true(inactive["is_estimated"]) and _decimal_equals(inactive["price_in_provider_currency"], "2.5") and _decimal_equals(inactive["conversion_rate_to_eur"], "0.4") and _decimal_equals(inactive["eur_price"], "1"), "inactive tariff values are incorrect"))

    all_rows = check("list_tariffs_all", lambda: repo.list_tariffs({"status": "all"}))
    check("list_tariffs_all_contains_demo", lambda: _check(demo_row(all_rows) is not None, "all status must contain Demo tariff"))
    check("list_tariffs_all_contains_inactive", lambda: _check(inactive_row(all_rows) is not None, "all status must contain inactive fixture"))
    empty_status_rows = check("list_tariffs_empty_status", lambda: repo.list_tariffs({"status": ""}))
    check("list_tariffs_empty_status_contains_demo", lambda: _check(demo_row(empty_status_rows) is not None, "empty status must contain Demo tariff"))
    check("list_tariffs_empty_status_contains_inactive", lambda: _check(inactive_row(empty_status_rows) is not None, "empty status must contain inactive fixture"))
    none_status_rows = check("list_tariffs_none_status", lambda: repo.list_tariffs({"status": None}))
    check("list_tariffs_none_status_contains_demo", lambda: _check(demo_row(none_status_rows) is not None, "None status must contain Demo tariff"))
    check("list_tariffs_none_status_contains_inactive", lambda: _check(inactive_row(none_status_rows) is not None, "None status must contain inactive fixture"))

    inactive_all = inactive_row(all_rows)
    check("list_tariffs_demo_country_filter", lambda: _check(demo_row(repo.list_tariffs({"country_id": country["id"]})) is not None, "Demo country filter must return Demo tariff"))
    check("list_tariffs_demo_provider_filter", lambda: _check(demo_row(repo.list_tariffs({"provider_id": provider["id"]})) is not None, "Demo provider filter must return Demo tariff"))
    check("list_tariffs_demo_country_provider_filter", lambda: _check(demo_row(repo.list_tariffs({"country_id": country["id"], "provider_id": provider["id"]})) is not None, "Demo combined filters must return Demo tariff"))
    check("list_tariffs_inactive_country_default_empty", lambda: _check(inactive_all is not None and repo.list_tariffs({"country_id": inactive_all["country_id"]}) == [], "inactive country default active filter must be empty"))
    check("list_tariffs_inactive_provider_default_empty", lambda: _check(inactive_all is not None and repo.list_tariffs({"provider_id": inactive_all["provider_id"]}) == [], "inactive provider default active filter must be empty"))
    check("list_tariffs_inactive_country_filter", lambda: _check(inactive_all is not None and inactive_row(repo.list_tariffs({"country_id": inactive_all["country_id"], "status": "inactive"})) is not None, "inactive country filter must return inactive tariff"))
    check("list_tariffs_inactive_provider_filter", lambda: _check(inactive_all is not None and inactive_row(repo.list_tariffs({"provider_id": inactive_all["provider_id"], "status": "inactive"})) is not None, "inactive provider filter must return inactive tariff"))
    check("list_tariffs_inactive_country_provider_filter", lambda: _check(inactive_all is not None and [row["id"] for row in repo.list_tariffs({"country_id": inactive_all["country_id"], "provider_id": inactive_all["provider_id"], "status": "inactive"})] == [inactive_all["id"]], "inactive combined filters must return exactly inactive tariff"))
    check("list_tariffs_missing_country_filter", lambda: _check(repo.list_tariffs({"country_id": -1}) == [], "missing country filter must return empty list"))
    check("list_tariffs_missing_provider_filter", lambda: _check(repo.list_tariffs({"provider_id": -1}) == [], "missing provider filter must return empty list"))

    check("list_tariffs_all_returns_list", lambda: _check(isinstance(all_rows, list), "list_tariffs must return list"))
    check("list_tariffs_all_order", lambda: _check([(row["country_name"], row["provider_name"], row["prefix"] or "") for row in (all_rows or [])] == sorted((row["country_name"], row["provider_name"], row["prefix"] or "") for row in (all_rows or [])), "tariffs must be ordered by country/provider/prefix"))
    check("list_tariffs_all_shape", lambda: _check(bool(all_rows) and all(list(row.keys()) == expected_keys for row in all_rows), "tariff row keys must match existing contract"))
    check("list_tariffs_no_phone_history_write_side_effect_fields", lambda: _check(bool(all_rows) and all({"phone_count", "history_count", "change_log_count"}.isdisjoint(_row_keys(row)) for row in all_rows), "tariff list must not expose phone/history/write side-effect fields"))


def run_stage_39_checks(repo: Repository, check, collections: dict, lookups: dict) -> None:
    """Check list_calling_companies filtered read path semantics."""
    expected_keys = [
        "id", "server_id", "country_id", "company_name", "company_id_external",
        "has_autorotation", "line_count", "dial_set_count", "retry_interval_seconds",
        "comment", "is_active", "created_by", "created_at", "updated_by", "updated_at",
        "server_name", "country_name", "current_has_autorotation", "current_routing_mode",
        "current_route_id",
    ]

    def by_external(rows, external_id):
        return next((row for row in (rows or []) if row["company_id_external"] == external_id), None)

    def externals(rows):
        return {row["company_id_external"] for row in (rows or [])}

    rows = check("stage_39_list_calling_companies_unfiltered", repo.list_calling_companies)
    demo = by_external(rows, "demo-company-1")
    manual = by_external(rows, "ci-manual-company")
    inactive = by_external(rows, "ci-inactive-company")

    for external_id in ("demo-company-1", "ci-manual-company", "ci-inactive-company"):
        check(f"stage_39_unfiltered_contains_{external_id}", lambda external_id=external_id: _check(by_external(rows, external_id) is not None, f"unfiltered companies must contain {external_id}"))

    check("stage_39_demo_values", lambda: _check(demo is not None and demo["company_name"] == "Demo Company" and demo["server_name"] == "demo-server-1" and demo["country_name"] == "Demo Country" and _is_database_true(demo["current_has_autorotation"]) and _is_database_true(demo["is_active"]) and demo["current_routing_mode"] == "autorotation" and demo["current_route_id"] is not None, "Demo Company current values are incorrect"))
    check("stage_39_manual_values", lambda: _check(manual is not None and manual["company_name"] == "CI Manual Company" and manual["server_name"] == "ci-manual-server-1" and manual["country_name"] == "CI Manual Company Country" and _is_database_true(manual["has_autorotation"]) and _is_database_false(manual["current_has_autorotation"]) and _is_database_true(manual["is_active"]) and manual["current_routing_mode"] == "server_priority" and manual["current_route_id"] is None, "CI Manual Company values are incorrect"))
    check("stage_39_inactive_values", lambda: _check(inactive is not None and inactive["company_name"] == "CI Inactive Company" and inactive["server_name"] == "ci-inactive-server-1" and inactive["country_name"] == "CI Inactive Company Country" and _is_database_false(inactive["has_autorotation"]) and _is_database_false(inactive["current_has_autorotation"]) and _is_database_false(inactive["is_active"]) and inactive["current_routing_mode"] is None and inactive["current_route_id"] is None, "CI Inactive Company values are incorrect"))

    for row_name, row in (("demo", demo), ("manual", manual), ("inactive", inactive)):
        check(f"stage_39_{row_name}_shape", lambda row=row: _check(row is not None and list(row.keys()) == expected_keys, "calling company row keys must match existing contract"))
    check("stage_39_returns_list", lambda: _check(isinstance(rows, list), "list_calling_companies must return list"))
    check("stage_39_order", lambda: _check([(row["country_name"], row["server_name"], row["company_name"]) for row in (rows or [])] == sorted((row["country_name"], row["server_name"], row["company_name"]) for row in (rows or [])), "calling companies must be ordered by country/server/company"))

    filters = [
        ("demo_server", {"server_id": demo["server_id"]}, {"demo-company-1"}),
        ("demo_country", {"country_id": demo["country_id"]}, {"demo-company-1"}),
        ("demo_server_country", {"server_id": demo["server_id"], "country_id": demo["country_id"]}, {"demo-company-1"}),
        ("manual_server", {"server_id": manual["server_id"]}, {"ci-manual-company"}),
        ("manual_country", {"country_id": manual["country_id"]}, {"ci-manual-company"}),
        ("inactive_server", {"server_id": inactive["server_id"]}, {"ci-inactive-company"}),
        ("inactive_country", {"country_id": inactive["country_id"]}, {"ci-inactive-company"}),
        ("missing_server", {"server_id": -1}, set()),
        ("missing_country", {"country_id": -1}, set()),
    ]
    for name, filter_values, expected in filters:
        check(f"stage_39_filter_{name}", lambda filter_values=filter_values, expected=expected: _check(externals(repo.list_calling_companies(filter_values)) == expected, f"{name} filter returned wrong companies"))

    for name, value, expected in (
        ("company_lower", "ci manual company", {"ci-manual-company"}),
        ("company_mixed", "Ci MaNuAl CoMpAnY", {"ci-manual-company"}),
        ("company_trim", "  CI Manual Company  ", {"ci-manual-company"}),
        ("company_partial", "Manual Company", {"ci-manual-company"}),
        ("company_missing", "missing manual", set()),
        ("company_underscore_literal", "CI_Manual_Company", set()),
        ("company_percent_literal", "CI%Manual%Company", set()),
    ):
        check(f"stage_39_search_{name}", lambda value=value, expected=expected: _check(externals(repo.list_calling_companies({"company_like": value})) == expected, f"{name} search returned wrong companies"))

    for name, value, expected in (
        ("external_lower", "ci-manual-company", {"ci-manual-company"}),
        ("external_mixed", "CI-MANUAL-COMPANY", {"ci-manual-company"}),
        ("external_trim", "  ci-manual-company  ", {"ci-manual-company"}),
        ("external_partial", "manual-company", {"ci-manual-company"}),
        ("external_missing", "missing-company", set()),
        ("external_underscore_literal", "ci_manual_company", set()),
        ("external_percent_literal", "ci%manual%company", set()),
    ):
        check(f"stage_39_search_{name}", lambda value=value, expected=expected: _check(externals(repo.list_calling_companies({"external_id_like": value})) == expected, f"{name} search returned wrong companies"))

    for value in ("1", 1, True):
        check(f"stage_39_has_autorotation_true_{value!r}", lambda value=value: _check("demo-company-1" in externals(repo.list_calling_companies({"has_autorotation": value})) and "ci-manual-company" not in externals(repo.list_calling_companies({"has_autorotation": value})) and "ci-inactive-company" not in externals(repo.list_calling_companies({"has_autorotation": value})), "true autorotation filter must use current setting"))
    for value in ("0", 0, False):
        check(f"stage_39_has_autorotation_false_{value!r}", lambda value=value: _check({"ci-manual-company", "ci-inactive-company"} <= externals(repo.list_calling_companies({"has_autorotation": value})) and "demo-company-1" not in externals(repo.list_calling_companies({"has_autorotation": value})), "false autorotation filter must include current false and missing setting"))
    for value in ("1", 1, True):
        check(f"stage_39_is_active_true_{value!r}", lambda value=value: _check({"demo-company-1", "ci-manual-company"} <= externals(repo.list_calling_companies({"is_active": value})) and "ci-inactive-company" not in externals(repo.list_calling_companies({"is_active": value})), "active filter must use cc.is_active"))
    for value in ("0", 0, False):
        check(f"stage_39_is_active_false_{value!r}", lambda value=value: _check(externals(repo.list_calling_companies({"is_active": value})) == {"ci-inactive-company"}, "inactive filter must return only CI Inactive Company"))

    for name, filter_values, expected in (
        ("false_active", {"has_autorotation": "0", "is_active": "1"}, {"ci-manual-company"}),
        ("false_inactive", {"has_autorotation": "0", "is_active": "0"}, {"ci-inactive-company"}),
        ("true_active", {"has_autorotation": "1", "is_active": "1"}, {"demo-company-1"}),
        ("true_inactive", {"has_autorotation": "1", "is_active": "0"}, set()),
    ):
        check(f"stage_39_combined_bool_{name}", lambda filter_values=filter_values, expected=expected: _check(externals(repo.list_calling_companies(filter_values)) == expected, f"combined boolean {name} returned wrong companies"))

    baseline = externals(rows)
    for key in ("has_autorotation", "is_active"):
        for value_name, value in (("all", "all"), ("empty", ""), ("none", None)):
            check(f"stage_39_{key}_{value_name}_ignored", lambda key=key, value=value: _check(externals(repo.list_calling_companies({key: value})) == baseline, f"{key}={value_name} must not restrict results"))
        for value in ("true", "false", "yes", "invalid"):
            check(f"stage_39_{key}_invalid_{value}", lambda key=key, value=value: _check(repo.list_calling_companies({key: value}) == [], f"{key} invalid value must return []"))

    check("stage_39_full_combined_filter", lambda: _check(externals(repo.list_calling_companies({"server_id": manual["server_id"], "country_id": manual["country_id"], "company_like": "manual company", "external_id_like": "manual-company", "has_autorotation": "0", "is_active": "1"})) == {"ci-manual-company"}, "full combined filter must return only CI Manual Company"))


def run_stage_40_checks(repo: Repository, check) -> None:
    """Check list_phone_numbers read-only filters and active route aggregation."""
    expected_keys = ["id", "country_id", "provider_id", "country_label", "provider_label", "number", "normalized_number", "project_label", "assignment_type", "assignment_label", "phone_type", "tariff_label", "status", "connection_cost", "monthly_fee", "outgoing_rate", "incoming_rate", "currency_id", "currency_label", "comment", "is_active", "review_required", "imported_created_by", "created_by", "created_at", "updated_by", "updated_at", "deactivated_at", "country_name", "provider_name", "currency_code", "assignment_type_label", "route_names"]

    def by_number(rows, number):
        return next((row for row in (rows or []) if row["number"] == number), None)

    def numbers(rows):
        return {row["number"] for row in (rows or [])}

    rows = check("stage_40_list_phone_numbers_unfiltered", repo.list_phone_numbers)
    demo = by_number(rows, "525550000001")
    review = by_number(rows, "525550000010")
    routed = by_number(rows, "525550000020")
    for number in ("525550000001", "525550000010", "525550000020"):
        check(f"stage_40_unfiltered_contains_{number}", lambda number=number: _check(by_number(rows, number) is not None, f"unfiltered phones must contain {number}"))

    check("stage_40_demo_values", lambda: _check(demo is not None and demo["country_name"] == "Demo Country" and demo["provider_name"] == "Demo Provider" and demo["currency_code"] == "EUR" and demo["project_label"] == "REP" and demo["assignment_type"] == "gl" and demo["assignment_type_label"] == "ГЛ" and demo["status"] == "used" and _is_database_true(demo["is_active"]) and _is_database_false(demo["review_required"]) and demo["route_names"] == "Demo Route" and _decimal_equals(demo["monthly_fee"], "1.25") and _decimal_equals(demo["outgoing_rate"], "0.05") and _decimal_equals(demo["incoming_rate"], "0.02"), "Demo phone values are incorrect"))
    check("stage_40_review_values", lambda: _check(review is not None and review["country_name"] == "CI Review Phone Country" and review["provider_id"] is None and review["provider_name"] is None and review["currency_code"] == "XPN" and review["project_label"] == "ИТМ" and review["assignment_type"] == "aon" and review["assignment_type_label"] == "АОН" and review["status"] == "problem" and _is_database_false(review["is_active"]) and _is_database_true(review["review_required"]) and review["route_names"] == "" and review["deactivated_at"] is not None and _decimal_equals(review["connection_cost"], "3.5") and _decimal_equals(review["monthly_fee"], "4.5") and _decimal_equals(review["outgoing_rate"], "0.15") and _decimal_equals(review["incoming_rate"], "0.05"), "CI Review Phone values are incorrect"))
    check("stage_40_routed_values", lambda: _check(routed is not None and routed["country_name"] == "CI Routed Phone Country" and routed["provider_name"] == "CI Phone Provider" and routed["currency_code"] == "XPN" and routed["project_label"] == "CI Phone Project" and routed["assignment_type"] == "ivr" and routed["assignment_type_label"] == "IVR" and routed["status"] == "free" and _is_database_true(routed["is_active"]) and _is_database_false(routed["review_required"]) and routed["route_names"] == "CI Phone Route A, CI Phone Route B" and "CI Phone Route Hidden" not in routed["route_names"] and _decimal_equals(routed["connection_cost"], "0.75") and _decimal_equals(routed["monthly_fee"], "1.5") and _decimal_equals(routed["outgoing_rate"], "0.03") and _decimal_equals(routed["incoming_rate"], "0.01"), "CI Routed Phone values are incorrect"))

    filters = [
        ("country_demo", {"country_id": demo["country_id"]}, {"525550000001"}), ("country_review", {"country_id": review["country_id"]}, {"525550000010"}), ("country_routed", {"country_id": routed["country_id"]}, {"525550000020"}), ("country_missing", {"country_id": -1}, set()),
        ("provider_demo", {"provider_id": demo["provider_id"]}, {"525550000001"}), ("provider_routed", {"provider_id": routed["provider_id"]}, {"525550000020"}), ("provider_none", {"provider_id": 0}, {"525550000010"}), ("provider_missing", {"provider_id": -1}, set()),
        ("project_demo", {"project": "REP"}, {"525550000001"}), ("project_review", {"project": "ИТМ"}, {"525550000010"}), ("project_routed", {"project": "CI Phone Project"}, {"525550000020"}), ("project_missing", {"project": "missing"}, set()),
        ("assignment_demo", {"assignment_type": "gl"}, {"525550000001"}), ("assignment_review", {"assignment_type": "aon"}, {"525550000010"}), ("assignment_routed", {"assignment_type": "ivr"}, {"525550000020"}), ("assignment_missing", {"assignment_type": "missing"}, set()),
        ("status_demo", {"status": "used"}, {"525550000001"}), ("status_review", {"status": "problem"}, {"525550000010"}), ("status_routed", {"status": "free"}, {"525550000020"}), ("status_missing", {"status": "missing"}, set()),
    ]
    for name, filter_values, expected in filters:
        check(f"stage_40_filter_{name}", lambda filter_values=filter_values, expected=expected: _check(numbers(repo.list_phone_numbers(filter_values)) == expected, f"{name} filter returned wrong phones"))

    for name, value, expected in (("lower", "ci phone project", {"525550000020"}), ("mixed", "Ci PhOnE PrOjEcT", {"525550000020"}), ("trim", "  CI Phone Project  ", {"525550000020"}), ("partial", "Phone Project", {"525550000020"}), ("missing", "missing project", set()), ("underscore", "CI_Phone_Project", set()), ("percent", "CI%Phone%Project", set())):
        check(f"stage_40_project_like_{name}", lambda value=value, expected=expected: _check(numbers(repo.list_phone_numbers({"project_like": value})) == expected, f"project_like {name} returned wrong phones"))
    for name, value, expected in (("full", "525550000020", {"525550000020"}), ("trim", "  525550000020  ", {"525550000020"}), ("partial", "0000020", {"525550000020"}), ("missing", "999999", set()), ("underscore", "5255500000_0", set()), ("percent", "5255500000%0", set())):
        check(f"stage_40_number_like_{name}", lambda value=value, expected=expected: _check(numbers(repo.list_phone_numbers({"number_like": value})) == expected, f"number_like {name} returned wrong phones"))

    for value in ("1", 1, True):
        check(f"stage_40_review_required_true_{value!r}", lambda value=value: _check(numbers(repo.list_phone_numbers({"review_required": value})) == {"525550000010"}, "true review filter returned wrong phones"))
    for value in ("0", 0, False):
        check(f"stage_40_review_required_false_{value!r}", lambda value=value: _check(numbers(repo.list_phone_numbers({"review_required": value})) == {"525550000001", "525550000020"}, "false review filter returned wrong phones"))
    baseline = numbers(rows)
    for name, value in (("all", "all"), ("empty", ""), ("none", None)):
        check(f"stage_40_review_required_{name}_ignored", lambda value=value: _check(numbers(repo.list_phone_numbers({"review_required": value})) == baseline, "ignored review filter must not restrict"))
    for value in ("true", "false", "yes", "invalid"):
        check(f"stage_40_review_required_invalid_{value}", lambda value=value: _check(repo.list_phone_numbers({"review_required": value}) == [], "invalid review filter must return []"))

    check("stage_40_combined_routed", lambda: _check(numbers(repo.list_phone_numbers({"country_id": routed["country_id"], "provider_id": routed["provider_id"], "project": "CI Phone Project", "project_like": "phone project", "assignment_type": "ivr", "status": "free", "number_like": "0000020", "review_required": "0"})) == {"525550000020"}, "combined routed filters must match exactly"))
    check("stage_40_combined_review", lambda: _check(numbers(repo.list_phone_numbers({"country_id": review["country_id"], "provider_id": 0, "project": "ИТМ", "assignment_type": "aon", "status": "problem", "number_like": "0000010", "review_required": "1"})) == {"525550000010"}, "combined review filters must match exactly"))
    check("stage_40_returns_list", lambda: _check(isinstance(rows, list), "list_phone_numbers must return list"))
    check("stage_40_order", lambda: _check([row["number"] for row in (rows or [])] == sorted(row["number"] for row in (rows or [])), "phone rows must be ordered by number"))
    check("stage_40_shape", lambda: _check(bool(rows) and all(list(row.keys()) == expected_keys for row in rows), "phone row keys must match existing contract"))
    check("stage_40_route_names_string", lambda: _check(all(isinstance(row["route_names"], str) for row in (rows or [])), "route_names must be a string"))

def run_stage_41_checks(repo: Repository, check) -> None:
    """Check company routing settings list/detail read-only semantics."""
    expected_list_keys = ["id", "calling_company_id", "country_id", "server_id", "route_id", "routing_mode", "has_autorotation", "is_active", "comment", "valid_from", "valid_to", "created_at", "created_by", "updated_at", "updated_by", "country_name", "server_name", "company_id_external", "company_name", "route_name", "provider_name", "updated_by_username"]
    expected_detail_keys = expected_list_keys[:-1]

    def externals(rows):
        return {row["company_id_external"] for row in (rows or [])}

    def by_external_mode(rows, external, mode):
        return next((row for row in (rows or []) if row["company_id_external"] == external and row["routing_mode"] == mode), None)

    companies = check("stage_41_list_calling_companies_seed", repo.list_calling_companies)
    countries = check("stage_41_list_countries_seed", repo.list_countries)
    servers = check("stage_41_list_servers_seed", repo.list_servers)
    rows = check("stage_41_list_company_routing_settings_default", repo.list_company_routing_settings)
    history = check("stage_41_list_company_routing_settings_history", lambda: repo.list_company_routing_settings({"include_history": True}))
    demo = by_external_mode(rows, "demo-company-1", "autorotation")
    manual = by_external_mode(rows, "ci-manual-company", "server_priority")
    historical = by_external_mode(history, "ci-manual-company", "autorotation")

    check("stage_41_default_contains_demo_and_manual", lambda: _check({"demo-company-1", "ci-manual-company"} <= externals(rows), "current settings missing"))
    check("stage_41_default_excludes_history_and_inactive", lambda: _check(by_external_mode(rows, "ci-manual-company", "autorotation") is None and "ci-inactive-company" not in externals(rows), "default must exclude history and inactive without setting"))
    check("stage_41_default_current_contract", lambda: _check(all(_is_database_true(row["is_active"]) and row["valid_to"] is None for row in (rows or [])), "default rows must be active current only"))
    check("stage_41_demo_values", lambda: _check(demo is not None and demo["company_name"] == "Demo Company" and demo["country_name"] == "Demo Country" and demo["server_name"] == "demo-server-1" and _is_database_true(demo["has_autorotation"]) and demo["route_name"] == "Demo Route" and demo["provider_name"] == "Demo Provider" and demo["updated_by_username"] == "admin", "demo setting values are incorrect"))
    check("stage_41_manual_current_values", lambda: _check(manual is not None and manual["company_name"] == "CI Manual Company" and manual["country_name"] == "CI Manual Company Country" and manual["server_name"] == "ci-manual-server-1" and _is_database_false(manual["has_autorotation"]) and manual["route_id"] is None and manual["route_name"] is None and manual["provider_name"] is None and manual["updated_by_username"] == "admin", "manual current values are incorrect"))
    check("stage_41_historical_values", lambda: _check(historical is not None and _is_database_true(historical["has_autorotation"]) and _is_database_false(historical["is_active"]) and historical["valid_to"] is not None and historical["route_id"] is None and historical["route_name"] is None and historical["provider_name"] is None and historical["comment"] == "Synthetic historical autorotation setting" and historical["updated_by_username"] == "admin", "historical values are incorrect"))

    for name, filters in (("include_true", {"include_history": True}), ("include_int", {"include_history": 1}), ("include_str", {"include_history": "1"}), ("show_true", {"show_history": True}), ("show_str", {"show_history": "1"})):
        check(f"stage_41_history_{name}", lambda filters=filters: _check(by_external_mode(repo.list_company_routing_settings(filters), "ci-manual-company", "autorotation") is not None, "history alias must include historical"))
    for name, filters in (("include_false", {"include_history": False}), ("include_zero", {"include_history": 0}), ("include_zero_str", {"include_history": "0"}), ("show_false", {"show_history": False}), ("show_zero_str", {"show_history": "0"})):
        check(f"stage_41_history_false_{name}", lambda filters=filters: _check(all(row["valid_to"] is None and _is_database_true(row["is_active"]) for row in repo.list_company_routing_settings(filters)), "false history must keep current-only"))
    for value in ("true", "false", "yes", "invalid"):
        check(f"stage_41_invalid_history_{value}", lambda value=value: _check(repo.list_company_routing_settings({"include_history": value}) == [] and repo.list_company_routing_settings({"show_history": value}) == [], "invalid history flag must return []"))

    for name, filter_values, expected in (("demo_country", {"country_id": demo["country_id"]}, {"demo-company-1"}), ("demo_server", {"server_id": demo["server_id"]}, {"demo-company-1"}), ("demo_company", {"calling_company_id": demo["calling_company_id"]}, {"demo-company-1"}), ("manual_country", {"country_id": manual["country_id"]}, {"ci-manual-company"}), ("manual_server", {"server_id": manual["server_id"]}, {"ci-manual-company"}), ("manual_company", {"calling_company_id": manual["calling_company_id"]}, {"ci-manual-company"}), ("missing_country", {"country_id": -1}, set()), ("missing_server", {"server_id": -1}, set()), ("missing_company", {"calling_company_id": -1}, set())):
        check(f"stage_41_filter_{name}", lambda filter_values=filter_values, expected=expected: _check(externals(repo.list_company_routing_settings(filter_values)) == expected, f"{name} returned wrong settings"))

    check("stage_41_routing_mode_current_autorotation", lambda: _check("demo-company-1" in externals(repo.list_company_routing_settings({"routing_mode": "autorotation"})) and by_external_mode(repo.list_company_routing_settings({"routing_mode": "autorotation"}), "ci-manual-company", "autorotation") is None, "current autorotation filter wrong"))
    check("stage_41_routing_mode_current_server_priority", lambda: _check(externals(repo.list_company_routing_settings({"routing_mode": "server_priority"})) == {"ci-manual-company"}, "server priority filter wrong"))
    check("stage_41_routing_mode_history_autorotation", lambda: _check({"demo-company-1", "ci-manual-company"} <= externals(repo.list_company_routing_settings({"include_history": "1", "routing_mode": "autorotation"})), "history autorotation filter wrong"))
    check("stage_41_routing_mode_missing", lambda: _check(repo.list_company_routing_settings({"routing_mode": "missing"}) == [], "missing mode must return []"))

    for name, value, expected in (("lower", "ci-manual-company", {"ci-manual-company"}), ("mixed", "CI-MANUAL-COMPANY", {"ci-manual-company"}), ("trim", "  ci-manual-company  ", {"ci-manual-company"}), ("partial", "manual-company", {"ci-manual-company"}), ("missing", "missing", set()), ("underscore", "ci_manual_company", set()), ("percent", "ci%manual%company", set())):
        check(f"stage_41_external_{name}", lambda value=value, expected=expected: _check(externals(repo.list_company_routing_settings({"company_id_external": value})) == expected, "external search mismatch"))

    for value in ("1", 1, True):
        check(f"stage_41_history_active_true_{value!r}", lambda value=value: _check({"demo-company-1", "ci-manual-company"} <= externals(repo.list_company_routing_settings({"include_history": "1", "is_active": value})) and by_external_mode(repo.list_company_routing_settings({"include_history": "1", "is_active": value}), "ci-manual-company", "autorotation") is None, "active true history filter wrong"))
    for value in ("0", 0, False):
        check(f"stage_41_history_active_false_{value!r}", lambda value=value: _check(externals(repo.list_company_routing_settings({"include_history": "1", "is_active": value})) == {"ci-manual-company"} and by_external_mode(repo.list_company_routing_settings({"include_history": "1", "is_active": value}), "ci-manual-company", "autorotation") is not None, "active false history filter wrong"))
    for value in ("all", "", None):
        check(f"stage_41_history_active_ignored_{value!r}", lambda value=value: _check(len(repo.list_company_routing_settings({"include_history": "1", "is_active": value})) == len(history), "ignored active filter restricted history"))
    for value in ("true", "false", "yes", "invalid"):
        check(f"stage_41_history_active_invalid_{value}", lambda value=value: _check(repo.list_company_routing_settings({"include_history": "1", "is_active": value}) == [], "invalid active must return []"))
    check("stage_41_inactive_ignored_without_history", lambda: _check(all(row["valid_to"] is None and _is_database_true(row["is_active"]) for row in repo.list_company_routing_settings({"is_active": "0"})), "is_active must be ignored without history"))
    check("stage_41_combined_history_filter", lambda: _check([row["id"] for row in repo.list_company_routing_settings({"include_history": "1", "country_id": manual["country_id"], "server_id": manual["server_id"], "routing_mode": "autorotation", "calling_company_id": manual["calling_company_id"], "company_id_external": "manual-company", "is_active": "0"})] == [historical["id"]], "combined filter must return exactly historical manual"))

    current_detail = check("stage_41_get_company_routing_setting_current", lambda: repo.get_company_routing_setting(manual["id"]))
    history_detail = check("stage_41_get_company_routing_setting_history", lambda: repo.get_company_routing_setting(historical["id"]))
    check("stage_41_detail_current_values", lambda: _check(current_detail is not None and current_detail["company_id_external"] == "ci-manual-company" and current_detail["routing_mode"] == "server_priority" and _is_database_false(current_detail["has_autorotation"]) and _is_database_true(current_detail["is_active"]) and current_detail["valid_to"] is None, "current detail wrong"))
    check("stage_41_detail_history_values", lambda: _check(history_detail is not None and history_detail["routing_mode"] == "autorotation" and _is_database_true(history_detail["has_autorotation"]) and _is_database_false(history_detail["is_active"]) and history_detail["valid_to"] is not None, "history detail wrong"))
    check("stage_41_detail_missing", lambda: _check(repo.get_company_routing_setting(-1) is None, "missing detail must return None"))
    check("stage_41_list_shape", lambda: _check(isinstance(rows, list) and bool(rows) and all(list(row.keys()) == expected_list_keys for row in rows), "list shape/order changed"))
    check("stage_41_detail_shape", lambda: _check(current_detail is not None and list(current_detail.keys()) == expected_detail_keys and "updated_by_username" not in current_detail.keys(), "detail shape/order changed"))
    check("stage_41_order", lambda: _check([(row["country_name"], row["server_name"], row["company_name"]) for row in (history or [])] == sorted((row["country_name"], row["server_name"], row["company_name"]) for row in (history or [])), "country/server/company order must be stable"))
    check("stage_41_manual_current_before_history", lambda: _check([row["routing_mode"] for row in history if row["company_id_external"] == "ci-manual-company"] == ["server_priority", "autorotation"], "manual current must precede historical"))


def run_stage_42_checks(repo: Repository, check) -> None:
    """Check provider-change list read-only semantics and filters."""
    expected_keys = ["id", "changed_at", "country_id", "company_id", "company_name_snapshot", "has_autorotation_snapshot", "route_before_id", "provider_before_id", "provider_prefix_before_id", "tariff_before_id", "price_before_provider_currency_id", "price_before_in_provider_currency", "price_before_conversion_rate_to_eur", "price_before_conversion_rate_date", "price_before_eur", "route_after_id", "provider_after_id", "provider_prefix_after_id", "tariff_after_id", "price_after_provider_currency_id", "price_after_in_provider_currency", "price_after_conversion_rate_to_eur", "price_after_conversion_rate_date", "price_after_eur", "price_delta_eur", "provider_changed", "reason_id", "reason_text", "comment", "telegram_status", "telegram_sent_at", "created_by", "created_at", "updated_by", "updated_at", "country_name", "provider_before_name", "provider_after_name", "route_before_name", "route_after_name", "created_by_username", "server_names"]

    def stage42(rows):
        return [row for row in (rows or []) if str(row["reason_text"]).startswith(("Planned provider switch", "AON refresh without provider switch"))]

    def reasons(rows):
        return {row["reason_text"] for row in stage42(rows)}

    rows = check("stage_42_list_provider_changes", repo.list_provider_changes)
    new = next((row for row in stage42(rows) if row["reason_text"] == "Planned provider switch"), None)
    old = next((row for row in stage42(rows) if row["reason_text"] == "AON refresh without provider switch"), None)
    check("stage_42_contains_fixture_rows", lambda: _check(new is not None and old is not None, "provider-change fixture rows missing"))
    check("stage_42_new_values", lambda: _check(new is not None and _is_database_true(new["provider_changed"]) and new["route_before_name"] == "Stage 42 Alpha" and new["route_after_name"] == "Stage 42 Beta" and new["server_names"] == "Stage 42 Server A, Stage 42 Server B", "new provider-change values wrong"))
    check("stage_42_old_values", lambda: _check(old is not None and _is_database_false(old["provider_changed"]) and old["route_before_name"] == old["route_after_name"] == "Stage 42 Alpha" and old["server_names"] is None, "old provider-change values wrong"))

    check("stage_42_provider_before_filter", lambda: _check("Planned provider switch" in reasons(repo.list_provider_changes({"provider_id": new["provider_before_id"]})), "before provider filter missing new row"))
    check("stage_42_provider_after_filter", lambda: _check(reasons(repo.list_provider_changes({"provider_id": new["provider_after_id"]})) == {"Planned provider switch"}, "after provider filter wrong"))
    check("stage_42_route_before_search", lambda: _check({"Planned provider switch", "AON refresh without provider switch"} <= reasons(repo.list_provider_changes({"route_like": "alpha"})), "before route search wrong"))
    check("stage_42_route_after_search", lambda: _check(reasons(repo.list_provider_changes({"route_like": "beta"})) == {"Planned provider switch"}, "after route search wrong"))
    check("stage_42_reason_search", lambda: _check(reasons(repo.list_provider_changes({"reason_like": "planned provider"})) == {"Planned provider switch"}, "reason search wrong"))
    check("stage_42_user_filter", lambda: _check({"Planned provider switch", "AON refresh without provider switch"} <= reasons(repo.list_provider_changes({"user_id": new["created_by"]})), "user filter wrong"))
    check("stage_42_date_from_inclusive", lambda: _check(reasons(repo.list_provider_changes({"date_from": "2026-07-12 11:00:00"})) == {"Planned provider switch"}, "date_from must be inclusive"))
    check("stage_42_date_to_inclusive", lambda: _check({"Planned provider switch", "AON refresh without provider switch"} <= reasons(repo.list_provider_changes({"date_to": "2026-07-12 11:00:00"})), "date_to must be inclusive"))
    check("stage_42_literal_underscore", lambda: _check(reasons(repo.list_provider_changes({"route_like": "Stage 42 Alpha_"})) == set(), "underscore must be literal"))
    check("stage_42_literal_percent", lambda: _check(reasons(repo.list_provider_changes({"reason_like": "Planned%provider"})) == set(), "percent must be literal"))
    check("stage_42_order_desc", lambda: _check([row["reason_text"] for row in stage42(rows)] == ["Planned provider switch", "AON refresh without provider switch"], "provider changes must sort DESC"))
    check("stage_42_shape", lambda: _check(new is not None and list(new.keys()) == expected_keys, "provider-change list shape/order changed"))



def run_stage_43_checks(repo: Repository, check) -> None:
    """Check routing-event list/detail read-only semantics and filters."""
    expected_keys = ["id", "event_at", "apply_scope", "reason", "country_id", "server_id", "provider_id", "affected_route_id", "old_route_id", "new_route_id", "calling_company_id", "company_change_type", "old_company_routing_mode", "new_company_routing_mode", "old_company_route_id", "new_company_route_id", "old_company_has_autorotation", "new_company_has_autorotation", "has_overflow", "overflow_route_id", "comment", "snapshot_json", "is_active", "deactivation_reason", "deactivated_at", "deactivated_by", "created_at", "created_by", "updated_at", "updated_by", "country_name", "server_name", "provider_name", "affected_route_name", "new_route_name", "old_route_name", "overflow_route_name", "old_company_route_name", "new_company_route_name", "old_company_route_provider_name", "new_company_route_provider_name", "old_price_eur", "new_price_eur", "price_delta_eur", "company_id_external", "company_name", "company_server_name", "author_name"]

    def stage43(rows):
        return [row for row in (rows or []) if str(row["reason"]).startswith("Stage 43")]

    def reasons(rows):
        return {row["reason"] for row in stage43(rows)}

    def one(rows, reason):
        return next((row for row in stage43(rows) if row["reason"] == reason), None)

    rows = check("stage_43_list_routing_events_default", repo.list_routing_events)
    all_rows = check("stage_43_list_routing_events_include_inactive", lambda: repo.list_routing_events({"include_inactive": True}))
    none_active = one(rows, "Stage 43 none active")
    server = one(rows, "Stage 43 server priority")
    campaign = one(rows, "Stage 43 campaign setting")
    inactive = one(all_rows, "Stage 43 none inactive")

    check("stage_43_default_result_type", lambda: _check(isinstance(rows, list), "routing events must return list"))
    check("stage_43_default_active_only", lambda: _check(reasons(rows) == {"Stage 43 none active", "Stage 43 server priority", "Stage 43 campaign setting"} and "Stage 43 none inactive" not in reasons(rows), "default routing events must be active-only"))
    check("stage_43_default_active_flags", lambda: _check(all(_is_database_true(row["is_active"]) for row in stage43(rows)), "default Stage 43 rows must be active"))
    check("stage_43_none_active_values", lambda: _check(none_active is not None and none_active["apply_scope"] == "none" and none_active["country_name"] == "CI Routed Phone Country" and none_active["provider_name"] == "CI Phone Provider" and none_active["affected_route_name"] == "Stage 42 Alpha" and none_active["server_id"] is None and none_active["server_name"] is None and none_active["calling_company_id"] is None and _is_database_false(none_active["has_overflow"]) and none_active["old_price_eur"] is None and none_active["new_price_eur"] is None and none_active["price_delta_eur"] is None and none_active["author_name"] == "Admin" and _snapshot_object(none_active["snapshot_json"]).get("state") == "active", "none active values wrong"))
    check("stage_43_inactive_values", lambda: _check(inactive is not None and _is_database_false(inactive["is_active"]) and inactive["deactivation_reason"] == "Synthetic Stage 43 archive" and inactive["deactivated_at"] is not None and inactive["deactivated_by"] is not None and _snapshot_object(inactive["snapshot_json"]).get("state") == "inactive", "inactive values wrong"))
    check("stage_43_server_values", lambda: _check(server is not None and server["apply_scope"] == "server_priority" and server["country_name"] == "CI Routed Phone Country" and server["server_name"] == "Stage 42 Server A" and server["provider_name"] == "CI Provider Change After" and server["affected_route_name"] == "Stage 42 Beta" and server["old_route_name"] == "Stage 42 Alpha" and server["new_route_name"] == "Stage 42 Beta" and _is_database_true(server["has_overflow"]) and server["overflow_route_name"] == "CI Phone Route B" and server["calling_company_id"] is None and _decimal_equals(server["old_price_eur"], "1") and _decimal_equals(server["new_price_eur"], "1.5") and _decimal_equals(server["price_delta_eur"], "0.5") and server["author_name"] == "Admin" and _snapshot_object(server["snapshot_json"]).get("scope") == "server_priority", "server priority values wrong"))
    check("stage_43_campaign_values", lambda: _check(campaign is not None and campaign["apply_scope"] == "campaign_setting" and campaign["country_name"] == "Demo Country" and campaign["server_id"] is None and campaign["server_name"] is None and campaign["company_id_external"] == "demo-company-1" and campaign["company_name"] == "Demo Company" and campaign["company_server_name"] == "demo-server-1" and campaign["company_change_type"] == "set_campaign_route" and campaign["old_company_routing_mode"] == "autorotation" and campaign["new_company_routing_mode"] == "mixed" and campaign["old_company_route_name"] == "Demo Route" and campaign["new_company_route_name"] == "Demo Route" and campaign["old_company_route_provider_name"] == "Demo Provider" and campaign["new_company_route_provider_name"] == "Demo Provider" and _is_database_true(campaign["old_company_has_autorotation"]) and _is_database_true(campaign["new_company_has_autorotation"]) and _decimal_equals(campaign["old_price_eur"], "0.1") and _decimal_equals(campaign["new_price_eur"], "0.1") and _decimal_equals(campaign["price_delta_eur"], "0") and _snapshot_object(campaign["snapshot_json"]).get("scope") == "campaign_setting", "campaign values wrong"))

    for value in (True, 1, "1"):
        check(f"stage_43_include_true_{value!r}", lambda value=value: _check("Stage 43 none inactive" in reasons(repo.list_routing_events({"include_inactive": value})), "true include_inactive must include inactive"))
    for value in (False, 0, "0", None, "", "all"):
        check(f"stage_43_include_false_{value!r}", lambda value=value: _check("Stage 43 none inactive" not in reasons(repo.list_routing_events({"include_inactive": value})), "false include_inactive must stay active-only"))
    for value in ("true", "false", "yes", "invalid"):
        check(f"stage_43_include_invalid_{value}", lambda value=value: _check(repo.list_routing_events({"include_inactive": value}) == [], "invalid include_inactive must return []"))

    check("stage_43_date_from_inclusive", lambda: _check({"Stage 43 server priority", "Stage 43 campaign setting"} <= reasons(repo.list_routing_events({"date_from": "2026-07-15 11:00:00"})), "date_from inclusive wrong"))
    check("stage_43_date_to_inclusive", lambda: _check("Stage 43 none active" in reasons(repo.list_routing_events({"date_to": "2026-07-14 10:00:00"})) and "Stage 43 server priority" not in reasons(repo.list_routing_events({"date_to": "2026-07-14 10:00:00"})), "date_to inclusive wrong"))
    check("stage_43_inactive_exact_range", lambda: _check(reasons(repo.list_routing_events({"include_inactive": True, "date_from": "2026-07-13 09:00:00", "date_to": "2026-07-13 09:00:00"})) == {"Stage 43 none inactive"}, "inactive exact date range wrong"))

    check("stage_43_filter_routed_country", lambda: _check({"Stage 43 none active", "Stage 43 server priority"} <= reasons(repo.list_routing_events({"country_id": none_active["country_id"]})), "routed country filter wrong"))
    check("stage_43_filter_demo_country", lambda: _check("Stage 43 campaign setting" in reasons(repo.list_routing_events({"country_id": campaign["country_id"]})), "demo country filter wrong"))
    for scope, expected in (("none", {"Stage 43 none active"}), ("server_priority", {"Stage 43 server priority"}), ("campaign_setting", {"Stage 43 campaign setting"})):
        check(f"stage_43_filter_scope_{scope}", lambda scope=scope, expected=expected: _check(reasons(repo.list_routing_events({"apply_scope": scope})) == expected, "scope filter wrong"))
    check("stage_43_filter_calling_company", lambda: _check(reasons(repo.list_routing_events({"calling_company_id": campaign["calling_company_id"]})) == {"Stage 43 campaign setting"}, "calling company filter wrong"))
    check("stage_43_filter_provider_old", lambda: _check("Stage 43 none active" in reasons(repo.list_routing_events({"provider_id": none_active["provider_id"]})), "old provider filter wrong"))
    check("stage_43_filter_provider_new", lambda: _check(reasons(repo.list_routing_events({"provider_id": server["provider_id"]})) == {"Stage 43 server priority"}, "new provider filter wrong"))

    servers = check("stage_43_list_servers_seed", repo.list_servers)
    server_b = next(row for row in servers if row["name"] == "Stage 42 Server B")
    demo_server = next(row for row in servers if row["name"] == "demo-server-1")
    check("stage_43_filter_server_a", lambda: _check(reasons(repo.list_routing_events({"server_id": server["server_id"]})) == {"Stage 43 server priority"}, "server A filter wrong"))
    check("stage_43_filter_server_b_exists", lambda: _check(reasons(repo.list_routing_events({"server_id": server_b["id"]})) == {"Stage 43 server priority"}, "server B EXISTS filter wrong"))
    check("stage_43_filter_demo_server_campaign", lambda: _check("Stage 43 campaign setting" in reasons(repo.list_routing_events({"server_id": demo_server["id"]})), "demo server campaign filter wrong"))
    for name, value, expected in (("lower", "demo-company-1", {"Stage 43 campaign setting"}), ("mixed", "DeMo-CoMpAnY-1", {"Stage 43 campaign setting"}), ("trim", "  demo-company-1  ", {"Stage 43 campaign setting"}), ("partial", "demo-company", {"Stage 43 campaign setting"}), ("missing", "missing", set()), ("underscore", "demo_company_1", set()), ("percent", "demo%company%1", set())):
        check(f"stage_43_campaign_search_{name}", lambda value=value, expected=expected: _check(reasons(repo.list_routing_events({"campaign_id": value})) == expected, "campaign search wrong"))

    check("stage_43_combined_server", lambda: _check(reasons(repo.list_routing_events({"date_from": "2026-07-15 11:00:00", "date_to": "2026-07-15 11:00:00", "country_id": server["country_id"], "apply_scope": "server_priority", "provider_id": server["provider_id"], "server_id": server_b["id"], "include_inactive": "0"})) == {"Stage 43 server priority"}, "combined server filter wrong"))
    check("stage_43_combined_campaign", lambda: _check(reasons(repo.list_routing_events({"date_from": "2026-07-16 12:00:00", "date_to": "2026-07-16 12:00:00", "country_id": campaign["country_id"], "apply_scope": "campaign_setting", "calling_company_id": campaign["calling_company_id"], "server_id": demo_server["id"], "campaign_id": "demo-company", "include_inactive": "1"})) == {"Stage 43 campaign setting"}, "combined campaign filter wrong"))
    check("stage_43_sort_order_default", lambda: _check([row["reason"] for row in stage43(rows)] == ["Stage 43 campaign setting", "Stage 43 server priority", "Stage 43 none active"], "default sort order wrong"))
    check("stage_43_sort_order_include_inactive", lambda: _check([row["reason"] for row in stage43(all_rows)] == ["Stage 43 campaign setting", "Stage 43 server priority", "Stage 43 none active", "Stage 43 none inactive"], "include inactive sort order wrong"))
    check("stage_43_list_shape", lambda: _check(none_active is not None and list(none_active.keys()) == expected_keys, "routing-event list shape/order changed"))

    server_detail = check("stage_43_get_routing_event_server", lambda: repo.get_routing_event(server["id"]))
    campaign_detail = check("stage_43_get_routing_event_campaign", lambda: repo.get_routing_event(campaign["id"]))
    inactive_detail = check("stage_43_get_routing_event_inactive", lambda: repo.get_routing_event(inactive["id"]))
    check("stage_43_detail_server_values", lambda: _check(isinstance(server_detail, dict) and server_detail.get("affected_server_names") == "Stage 42 Server A, Stage 42 Server B" and _decimal_equals(server_detail.get("price_delta_eur"), "0.5"), "server detail wrong"))
    check("stage_43_detail_campaign_values", lambda: _check(isinstance(campaign_detail, dict) and "affected_server_names" not in campaign_detail and campaign_detail.get("company_id_external") == "demo-company-1", "campaign detail wrong"))
    check("stage_43_detail_inactive_values", lambda: _check(isinstance(inactive_detail, dict) and _is_database_false(inactive_detail.get("is_active")) and inactive_detail.get("deactivation_reason") == "Synthetic Stage 43 archive", "inactive detail wrong"))
    check("stage_43_detail_missing", lambda: _check(repo.get_routing_event(-1) is None, "missing detail must return None"))

def run_stage_46_checks(repo: Repository, check) -> None:
    """Check history reads with semantic fixture lookups and no write operations."""
    phones = check("stage_46_list_phone_numbers_seed", repo.list_phone_numbers)
    routes = check("stage_46_list_routes_seed", repo.list_routes)
    tariffs = check("stage_46_list_tariffs_seed", repo.list_tariffs)
    demo_phone = check("stage_46_demo_phone_seed", lambda: next(row for row in (phones or []) if row["normalized_number"] == "525550000001"))
    routed_phone = check("stage_46_routed_phone_seed", lambda: next(row for row in (phones or []) if row["normalized_number"] == "525550000020"))
    route = check("stage_46_demo_route_seed", lambda: next(row for row in (routes or []) if row["name"] == "Demo Route"))
    tariff = check("stage_46_demo_tariff_seed", lambda: next(row for row in (tariffs or []) if row["country_name"] == "Demo Country" and row["provider_name"] == "Demo Provider" and row["prefix"] == "123"))
    phone_rows = check("stage_46_list_phone_history_demo", lambda: repo.list_phone_history(demo_phone["id"]))
    routed_phone_rows = check("stage_46_list_phone_history_routed", lambda: repo.list_phone_history(routed_phone["id"]))
    route_rows = check("stage_46_list_route_history", lambda: repo.list_route_history(route["id"]))
    tariff_rows = check("stage_46_list_tariff_history", lambda: repo.list_tariff_history(tariff["id"]))
    phone_keys = ["source", "action", "changed_at", "user_name", "field_name", "old_value", "new_value", "reason", "comment", "route_name", "phone_number"]
    tariff_keys = ["id", "tariff_id", "changed_at", "changed_by", "country_id", "country_name_snapshot", "provider_id", "provider_name_snapshot", "provider_prefix_id", "prefix_snapshot", "old_provider_currency_id", "new_provider_currency_id", "old_price_in_provider_currency", "new_price_in_provider_currency", "old_conversion_rate_to_eur", "new_conversion_rate_to_eur", "old_conversion_rate_date", "new_conversion_rate_date", "old_eur_price", "new_eur_price", "eur_price_delta", "reason", "comment", "created_at", "user_name"]
    for name, rows, keys in (("phone", phone_rows, phone_keys), ("routed_phone", routed_phone_rows, phone_keys), ("route", route_rows, phone_keys), ("tariff", tariff_rows, tariff_keys)):
        check(f"stage_46_{name}_history_type", lambda rows=rows: _check(isinstance(rows, list), "history must return a list"))
        check(f"stage_46_{name}_history_shape", lambda rows=rows, keys=keys: _check(rows and list(rows[0].keys()) == keys, "history shape changed"))
    check("stage_46_phone_history_order", lambda: _check([row["changed_at"] for row in phone_rows] == sorted((row["changed_at"] for row in phone_rows), reverse=True), "phone history must be newest first"))
    check("stage_46_routed_phone_history_order", lambda: _check([row["changed_at"] for row in routed_phone_rows] == sorted((row["changed_at"] for row in routed_phone_rows), reverse=True), "routed phone history must be newest first"))
    check("stage_46_route_history_order", lambda: _check([row["changed_at"] for row in route_rows] == sorted((row["changed_at"] for row in route_rows), reverse=True), "route history must be newest first"))
    check("stage_46_tariff_history_order", lambda: _check([(row["changed_at"], row["id"]) for row in tariff_rows] == sorted(((row["changed_at"], row["id"]) for row in tariff_rows), reverse=True), "tariff history must be newest first"))
    check("stage_46_phone_history_missing", lambda: _check(repo.list_phone_history(-1) == [], "missing phone history must be empty"))
    check("stage_46_route_history_missing", lambda: _check(repo.list_route_history(-1) == [], "missing route history must be empty"))
    check("stage_46_tariff_history_missing", lambda: _check(repo.list_tariff_history(-1) == [], "missing tariff history must be empty"))
    demo_replacement = next((row for row in (phone_rows or []) if row["reason"] == "Stage 46 phone replaced"), None)
    routed_replacement = next((row for row in (routed_phone_rows or []) if row["reason"] == "Stage 46 phone replaced"), None)
    added = next((row for row in (phone_rows or []) if row["reason"] == "Stage 46 phone linked"), None)
    check("stage_46_phone_history_phone_event", lambda: _check(any(row["action"] == "updated" and row["field_name"] == "status" and row["old_value"] == "problem" and row["new_value"] == "used" and row["reason"] == "Stage 46 phone status" for row in (phone_rows or [])), "phone change history missing"))
    check("stage_46_phone_history_added", lambda: _check(added is not None and added["phone_number"] == "525550000001" and "usage_type=cli" in added["new_value"], "added history missing"))
    check("stage_46_replacement_old_phone_lookup", lambda: _check(demo_replacement is not None, "replacement missing through old phone id"))
    check("stage_46_replacement_new_phone_lookup", lambda: _check(routed_replacement is not None, "replacement missing through new phone id"))
    check("stage_46_replacement_shared_values", lambda: _check(demo_replacement is not None and routed_replacement is not None and demo_replacement["phone_number"] is None and demo_replacement["route_name"] == "Demo Route" and "525550000001" in demo_replacement["old_value"] and "525550000020" in demo_replacement["new_value"] and demo_replacement["changed_at"] == routed_replacement["changed_at"], "replacement old/new contract changed"))
    check("stage_46_route_history_route_event", lambda: _check(any(row["action"] == "updated" and row["field_name"] == "comment" and row["old_value"] == "Temporary Stage 46 route comment" and row["new_value"] == "Synthetic route" and row["reason"] == "Stage 46 route comment" for row in (route_rows or [])), "route change history missing"))
    check("stage_46_route_history_route_phone_events", lambda: _check({"Stage 46 phone linked", "Stage 46 phone replaced"} <= {row["reason"] for row in (route_rows or [])}, "route-phone history missing"))
    created = next((row for row in (tariff_rows or []) if row["reason"] == "tariff.created"), None)
    changed = next((row for row in (tariff_rows or []) if row["reason"] == "tariff.changed"), None)
    check("stage_46_tariff_history_created", lambda: _check(created is not None and created["old_price_in_provider_currency"] is None and created["old_conversion_rate_to_eur"] is None and created["old_eur_price"] is None and created["eur_price_delta"] is None and _decimal_equals(created["new_price_in_provider_currency"], "0.2") and _decimal_equals(created["new_conversion_rate_to_eur"], "1") and _decimal_equals(created["new_eur_price"], "0.2"), "tariff created values wrong"))
    check("stage_46_tariff_history_changed", lambda: _check(changed is not None and _decimal_equals(changed["old_price_in_provider_currency"], "0.2") and _decimal_equals(changed["new_price_in_provider_currency"], "0.1") and _decimal_equals(changed["old_conversion_rate_to_eur"], "1") and _decimal_equals(changed["new_conversion_rate_to_eur"], "1") and _decimal_equals(changed["old_eur_price"], "0.2") and _decimal_equals(changed["new_eur_price"], "0.1") and _decimal_equals(changed["eur_price_delta"], "-0.1"), "tariff changed values wrong"))
    check("stage_46_phone_current_state", lambda: _check(repo.get_phone_number(demo_phone["id"])["status"] == "used", "history must not change phone state"))
    check("stage_46_route_current_state", lambda: _check(repo.get_route(route["id"])["comment"] == "Synthetic route", "history must not change route state"))
    check("stage_46_tariff_current_state", lambda: _check(_decimal_equals(repo.get_tariff(tariff["id"])["price_in_provider_currency"], "0.1") and _is_database_true(repo.get_tariff(tariff["id"])["is_current"]), "history must not change tariff state"))


def run_stage_47_checks(repo: Repository, check) -> None:
    """Check active campaign-setting event history is company-scoped and adapter-safe."""
    expected_keys = ["id", "event_at", "apply_scope", "reason", "country_id", "server_id", "provider_id", "affected_route_id", "old_route_id", "new_route_id", "calling_company_id", "company_change_type", "old_company_routing_mode", "new_company_routing_mode", "old_company_route_id", "new_company_route_id", "old_company_has_autorotation", "new_company_has_autorotation", "has_overflow", "overflow_route_id", "comment", "snapshot_json", "is_active", "deactivation_reason", "deactivated_at", "deactivated_by", "created_at", "created_by", "updated_at", "updated_by", "user_name", "country_name", "company_server_name", "company_id_external", "company_name", "old_route_name", "old_provider_name", "new_route_name", "new_provider_name"]
    settings = check("stage_47_settings_seed", lambda: repo.list_company_routing_settings({"include_history": True}))
    companies = check("stage_47_companies_seed", repo.list_calling_companies)
    demo_company = check("stage_47_demo_company_seed", lambda: next(row for row in (companies or []) if row["company_id_external"] == "demo-company-1"))
    manual_company = check("stage_47_manual_company_seed", lambda: next(row for row in (companies or []) if row["company_id_external"] == "ci-manual-company"))
    demo_setting = check("stage_47_demo_setting_seed", lambda: next(row for row in (settings or []) if row["calling_company_id"] == demo_company["id"]))
    manual_settings = check("stage_47_manual_settings_seed", lambda: [row for row in (settings or []) if row["calling_company_id"] == manual_company["id"]])
    demo_rows = check("stage_47_demo_history", lambda: repo.list_company_routing_setting_history(demo_setting["id"]))
    manual_rows = check("stage_47_manual_current_history", lambda: repo.list_company_routing_setting_history(next(row["id"] for row in manual_settings if _is_database_true(row["is_active"]))))
    manual_old_rows = check("stage_47_manual_historical_history", lambda: repo.list_company_routing_setting_history(next(row["id"] for row in manual_settings if _is_database_false(row["is_active"]))))
    check("stage_47_demo_history_type", lambda: _check(isinstance(demo_rows, list), "history must return a list"))
    check("stage_47_demo_history_shape", lambda: _check(demo_rows and list(demo_rows[0].keys()) == expected_keys, "history shape/order changed"))
    check("stage_47_demo_active_only", lambda: _check("Stage 47 demo campaign inactive" not in {row["reason"] for row in demo_rows}, "inactive event leaked"))
    check("stage_47_demo_company_scope", lambda: _check(all(row["company_id_external"] == "demo-company-1" and row["apply_scope"] == "campaign_setting" and _is_database_true(row["is_active"]) for row in demo_rows), "demo rows are not active company campaign events"))
    check("stage_47_demo_order", lambda: _check([row["reason"] for row in demo_rows[:2]] == ["Stage 47 demo campaign active", "Stage 43 campaign setting"], "event order changed"))
    active = next((row for row in demo_rows if row["reason"] == "Stage 47 demo campaign active"), None)
    stage43 = next((row for row in demo_rows if row["reason"] == "Stage 43 campaign setting"), None)
    check("stage_47_demo_active_present", lambda: _check(active is not None, "active demo event missing"))
    check("stage_47_demo_active_aliases", lambda: _check(active and active["country_name"] == "Demo Country" and active["company_server_name"] == "demo-server-1" and active["company_name"] == "Demo Company" and active["old_route_name"] == active["new_route_name"] == "Demo Route" and active["old_provider_name"] == active["new_provider_name"] == "Demo Provider", "demo aliases wrong"))
    check("stage_47_demo_active_values", lambda: _check(active and active["company_change_type"] == "disable_autorotation" and active["old_company_routing_mode"] == "mixed" and active["new_company_routing_mode"] == "campaign_route" and _is_database_true(active["old_company_has_autorotation"]) and _is_database_false(active["new_company_has_autorotation"]) and active["user_name"] == "Admin" and active["comment"] == "Synthetic Stage 47 active demo campaign event", "demo event values wrong"))
    check("stage_47_demo_snapshot", lambda: _check(active and _snapshot_object(active["snapshot_json"]).get("stage") == 47 and _snapshot_object(active["snapshot_json"]).get("company") == "demo-company-1" and _snapshot_object(active["snapshot_json"]).get("state") == "active", "demo snapshot wrong"))
    check("stage_47_stage43_visible", lambda: _check(stage43 and stage43["company_id_external"] == "demo-company-1" and stage43["old_route_name"] == stage43["new_route_name"] == "Demo Route" and stage43["user_name"] == "Admin", "Stage 43 event missing"))
    check("stage_47_manual_version_equivalence", lambda: _check([(row["id"], row["reason"]) for row in manual_rows] == [(row["id"], row["reason"]) for row in manual_old_rows], "current/historical settings differ"))
    manual = next((row for row in manual_rows if row["reason"] == "Stage 47 manual campaign active"), None)
    check("stage_47_manual_scope", lambda: _check(manual and all("Demo Company" not in row["company_name"] for row in manual_rows), "manual company scope wrong"))
    check("stage_47_manual_values", lambda: _check(manual and manual["country_name"] == "CI Manual Company Country" and manual["company_server_name"] == "ci-manual-server-1" and manual["company_id_external"] == "ci-manual-company" and manual["company_name"] == "CI Manual Company" and manual["company_change_type"] == "disable_autorotation" and manual["old_company_routing_mode"] == "autorotation" and manual["new_company_routing_mode"] == "server_priority" and manual["old_route_name"] is manual["new_route_name"] is manual["old_provider_name"] is manual["new_provider_name"] is None and _is_database_true(manual["old_company_has_autorotation"]) and _is_database_false(manual["new_company_has_autorotation"]) and manual["user_name"] == "Admin", "manual event values wrong"))
    check("stage_47_manual_snapshot", lambda: _check(manual and _snapshot_object(manual["snapshot_json"]).get("state") == "active", "manual snapshot wrong"))
    check("stage_47_missing_setting", lambda: _check(repo.list_company_routing_setting_history(-1) == [], "missing setting must return []"))
    check("stage_47_event_sort", lambda: _check([(row["event_at"], row["id"]) for row in demo_rows] == sorted(((row["event_at"], row["id"]) for row in demo_rows), reverse=True), "history must be newest first"))


def run_stage_48_checks(repo: Repository, check, demo_company) -> None:
    """Check direct and JSON-routed calling-company history without writes."""
    expected_keys = ["changed_at", "user_name", "action", "old_value", "new_value", "comment", "current_company_name", "company_id_external"]
    demo_rows = check("stage_48_demo_history", lambda: repo.list_calling_company_history(demo_company["id"]))
    companies = check("stage_48_companies", repo.list_calling_companies)
    manual_company = check("stage_48_manual_company", lambda: next(row for row in (companies or []) if row["company_id_external"] == "ci-manual-company"))
    manual_rows = check("stage_48_manual_history", lambda: repo.list_calling_company_history(manual_company["id"]))
    missing_rows = check("stage_48_missing_history", lambda: repo.list_calling_company_history(-1))
    check("stage_48_demo_history_type", lambda: _check(isinstance(demo_rows, list), "history must return a list"))
    check("stage_48_demo_history_shape", lambda: _check(demo_rows and list(demo_rows[0].keys()) == expected_keys, "history shape/order changed"))
    stage_rows = [row for row in (demo_rows or []) if str(row["comment"]).startswith("Stage 48")]
    check("stage_48_demo_rows", lambda: _check([row["comment"] for row in stage_rows] == ["Stage 48 routing-event history", "Stage 48 company changed"], "demo Stage 48 history order changed"))
    check("stage_48_demo_order", lambda: _check([(row["changed_at"], row["comment"]) for row in (demo_rows or [])] == sorted(((row["changed_at"], row["comment"]) for row in (demo_rows or [])), reverse=True), "history must be newest first"))
    routing = next((row for row in stage_rows if row["comment"] == "Stage 48 routing-event history"), None)
    direct = next((row for row in stage_rows if row["comment"] == "Stage 48 company changed"), None)
    check("stage_48_routing_aliases", lambda: _check(routing and routing["user_name"] == "Admin" and routing["current_company_name"] == "Demo Company" and routing["company_id_external"] == "demo-company-1" and routing["action"] == "routing_event.created", "routing aliases/action wrong"))
    check("stage_48_routing_values", lambda: _check(routing and _snapshot_object(routing["old_value"]) == {"calling_company_id": demo_company["id"], "routing_mode": "autorotation"} and _snapshot_object(routing["new_value"]) == {"calling_company_id": demo_company["id"], "routing_mode": "mixed", "stage": 48}, "routing JSON values wrong"))
    check("stage_48_routing_summary_comment", lambda: _check(routing and routing["comment"] != "Synthetic Stage 48 routing-event change log", "output comment must use summary"))
    check("stage_48_direct_aliases", lambda: _check(direct and direct["user_name"] == "Admin" and direct["current_company_name"] == "Demo Company" and direct["company_id_external"] == "demo-company-1" and direct["action"] == "calling_company.updated", "direct aliases/action wrong"))
    check("stage_48_direct_values", lambda: _check(direct and _snapshot_object(direct["old_value"]) == {"company_name": "Demo Company", "line_count": 1, "comment": "Temporary Stage 48 company comment"} and _snapshot_object(direct["new_value"]) == {"company_name": "Demo Company", "line_count": 2, "comment": "Synthetic company"}, "direct JSON values wrong"))
    manual = next((row for row in (manual_rows or []) if row["comment"] == "Stage 48 manual company changed"), None)
    check("stage_48_manual_type", lambda: _check(isinstance(manual_rows, list), "manual history must return a list"))
    check("stage_48_manual_isolation", lambda: _check(manual and all(not str(row["comment"]).startswith("Stage 48 routing-event") and row["current_company_name"] == "CI Manual Company" and row["company_id_external"] == "ci-manual-company" for row in manual_rows) and all("Demo Company" not in str(row["current_company_name"]) for row in manual_rows), "manual company history leaked demo rows"))
    check("stage_48_manual_values", lambda: _check(manual and manual["user_name"] == "Admin" and manual["action"] == "calling_company.updated" and _snapshot_object(manual["old_value"]) == {"comment": "Temporary Stage 48 manual company comment"} and _snapshot_object(manual["new_value"]) == {"comment": "Synthetic active manual company"}, "manual JSON values wrong"))
    check("stage_48_missing_company", lambda: _check(missing_rows == [], "missing company must return []"))


def run_stage_49_checks(repo: Repository, check, demo_company) -> None:
    """Check calling-company event list/count parity, JSON routing and literals."""
    keys = ["id", "company_id", "changed_at", "user_name", "action", "old_value", "new_value", "comment", "current_company_name", "company_id_external"]
    rows = check("stage_49_all_rows", lambda: repo.list_calling_company_events(limit=1000, offset=0))
    total = check("stage_49_all_count", repo.count_calling_company_events)
    check("stage_49_list_type", lambda: _check(isinstance(rows, list), "events must return a list"))
    check("stage_49_count_type", lambda: _check(isinstance(total, int), "count must return int"))
    check("stage_49_count_parity", lambda: _check(total == len(rows or []), "list/count differ"))
    check("stage_49_shape", lambda: _check(rows and list(rows[0].keys()) == keys, "event shape/order changed"))
    stage = [row for row in (rows or []) if str(row["comment"]).startswith("Stage 49")]
    expected = ["Stage 49 manual delta", "Stage 49 manual gamma", "Stage 49 literal-%_\\-needle", "Stage 49 routing beta", "Stage 49 alpha-summary-needle-49"]
    check("stage_49_qualifying_order", lambda: _check([row["comment"] for row in stage] == expected, "qualifying events/order changed"))
    check("stage_49_excluded_entities", lambda: _check(not any("excluded-route" in str(row["comment"]) or "orphan-routing" in str(row["comment"]) for row in (rows or [])), "excluded events leaked"))
    routing = next((row for row in stage if row["comment"] == "Stage 49 routing beta"), None)
    check("stage_49_routing_contract", lambda: _check(routing and routing["company_id"] != demo_company["id"] and routing["action"] == "routing_event.updated" and routing["current_company_name"] == "Demo Company" and routing["company_id_external"] == "demo-company-1" and routing["user_name"] == "Admin", "routing output contract changed"))
    check("stage_49_routing_json", lambda: _check(routing and _snapshot_object(routing["old_value"])["calling_company_id"] == demo_company["id"] and int(_snapshot_object(routing["new_value"])["calling_company_id"]) == demo_company["id"], "routing JSON company ID wrong"))
    for index, query in enumerate((None, "", "   ", "  ALPHA-SUMMARY-NEEDLE-49  ", "OLD-JSON-NEEDLE-49", "NEW-JSON-NEEDLE-49", "ROUTING-NEW-NEEDLE-49", "DEMO-COMPANY-1", "demo company", "ci-manual-company", "%_\\", "excluded-route-needle-49", "orphan-routing-needle-49", str(demo_company["id"]))):
        found = check(f"stage_49_search_{index}_list", lambda q=query: repo.list_calling_company_events(search=q, limit=1000, offset=0))
        count = check(f"stage_49_search_{index}_count", lambda q=query: repo.count_calling_company_events(search=q))
        check(f"stage_49_search_{index}_parity", lambda found=found, count=count: _check(count == len(found or []), "search list/count differ"))
    check("stage_49_literal_found", lambda: _check(any(row["comment"] == "Stage 49 literal-%_\\-needle" for row in (repo.list_calling_company_events(search="%_\\", limit=1000, offset=0))), "literal search failed"))
    check("stage_49_excluded_searches", lambda: _check(repo.list_calling_company_events(search="excluded-route-needle-49", limit=1000, offset=0) == [] and repo.count_calling_company_events(search="orphan-routing-needle-49") == 0, "excluded search leaked"))
    check("stage_49_page_1", lambda: _check([row["comment"] for row in repo.list_calling_company_events(limit=2, offset=0)] == expected[:2], "first page wrong"))
    check("stage_49_page_2", lambda: _check([row["comment"] for row in repo.list_calling_company_events(limit=2, offset=2)] == expected[2:4], "second page wrong"))
    check("stage_49_page_3", lambda: _check([row["comment"] for row in repo.list_calling_company_events(limit=1, offset=4)] == expected[4:5], "third page wrong"))
    check("stage_49_zero_limit", lambda: _check(repo.list_calling_company_events(limit=0, offset=0) == [], "zero limit must be empty"))


def run_repository_checks(repo: Repository, postgres_url: str) -> dict:
    summary = empty_summary(postgres_url)
    checks: list[tuple[str, object]] = []
    failures: list[dict[str, str]] = []

    def check(name: str, operation) -> object:
        try:
            value = operation()
            checks.append((name, value))
            return value
        except Exception as exc:
            failures.append({"check": name, "error": sanitize_error(exc, postgres_url)})
            return None

    countries = check("list_countries", repo.list_countries)
    check("countries_nonempty", lambda: _check(bool(countries), "countries must not be empty"))
    country = countries[0] if countries else {}
    check("get_country", lambda: _check(isinstance(repo.get_country(country["id"]), dict), "country lookup must return dict"))
    check("list_countries_by_ids", lambda: _check(bool(repo.list_countries_by_ids([country["id"]])), "country ids lookup must return a row"))

    collection_methods = (
        "list_currencies", "list_providers", "list_providers_with_currency", "list_projects",
        "list_servers", "list_phone_number_types", "list_phone_assignment_types",
        "list_provider_prefixes", "list_provider_prefixes_with_provider",
        "list_active_change_reasons", "list_change_reasons",
    )
    collections = {}
    for method_name in collection_methods:
        collections[method_name] = check(method_name, getattr(repo, method_name))
        check(f"{method_name}_nonempty", lambda name=method_name: _check(bool(collections[name]), f"{name} must not be empty"))

    counts = check("dictionary_counts", repo.dictionary_counts)
    expected_keys = {"countries", "providers", "currencies", "prefixes", "servers", "phone-types", "projects", "phone-assignments"}
    check("dictionary_counts_keys", lambda: _check(isinstance(counts, dict) and expected_keys <= counts.keys(), "dictionary_counts keys are incomplete"))

    lookups = (
        ("get_country_by_name", ("Demo Country",)),
        ("get_provider_by_normalized_name", ("demo provider",)),
        ("get_currency_by_code", ("EUR",)),
        ("get_project_by_name", (collections["list_projects"][0]["name"],)),
        ("get_phone_number_type_by_name", ("Mobile",)),
        ("get_phone_assignment_type_by_code_or_name", ("gl",)),
        ("get_server_by_name", ("demo-server-1",)),
        ("get_phone_number_import_identity_by_normalized_number", ("525550000001",)),
    )
    lookup_results = {}
    for method_name, args in lookups:
        lookup_results[method_name] = check(method_name, lambda name=method_name, values=args: getattr(repo, name)(*values))
        check(f"{method_name}_value", lambda name=method_name: _check(isinstance(lookup_results[name], dict), f"{name} must return dict for demo data"))

    run_exists_checks(repo, check)
    company, company_detail = run_stage_34_checks(repo, check, collections["list_currencies"] or [])
    run_stage_35_checks(repo, check, collections, lookup_results, company, company_detail)
    run_stage_36_checks(repo, check)
    run_stage_37_checks(repo, check, collections, lookup_results)
    run_stage_38_checks(repo, check, collections, lookup_results)
    run_stage_39_checks(repo, check, collections, lookup_results)
    run_stage_40_checks(repo, check)
    run_stage_41_checks(repo, check)
    run_stage_42_checks(repo, check)
    run_stage_43_checks(repo, check)
    run_stage_46_checks(repo, check)
    run_stage_47_checks(repo, check)
    run_stage_48_checks(repo, check, company)
    run_stage_49_checks(repo, check, company)

    summary.update(status="ok" if not failures else "failed", checks_count=len(checks) + len(failures), failures=failures)
    return summary


def run_smoke(postgres_url: str) -> dict:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError('Install psycopg to run PostgreSQL repository smoke: pip install "psycopg[binary]"') from exc

    with psycopg.connect(postgres_url, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        return run_repository_checks(Repository(conn, backend="postgres"), postgres_url)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run read-only Repository smoke checks against PostgreSQL")
    parser.add_argument("--postgres-url", default=os.environ.get("DATABASE_URL"), help="PostgreSQL URL (or set DATABASE_URL)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print JSON summary")
    parser.add_argument("--output", type=Path, help="Write the JSON summary to this generated report path")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.postgres_url:
        parser.error("--postgres-url is required when DATABASE_URL is not set")
    safe_url = mask_postgres_url(args.postgres_url)
    try:
        summary = run_smoke(args.postgres_url)
    except Exception as exc:
        summary = empty_summary(args.postgres_url)
        summary["failures"] = [{"check": "connection_or_smoke", "error": sanitize_error(exc, args.postgres_url)}]
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered if args.as_json else f"PostgreSQL Repository smoke: {summary['status']} ({summary['checks_count']} checks) [{safe_url}]")
    if summary["failures"] and not args.as_json:
        for failure in summary["failures"]:
            print(f"- {failure['check']}: {failure['error']}", file=sys.stderr)
    return 0 if summary["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
