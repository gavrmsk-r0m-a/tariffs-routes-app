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
