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
