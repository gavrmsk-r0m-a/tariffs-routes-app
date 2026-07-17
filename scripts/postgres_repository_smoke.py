#!/usr/bin/env python3
"""Read-only smoke checks for adapter-ready Repository methods on PostgreSQL."""
from __future__ import annotations

import argparse
import json
import os
import sys
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
    for method_name, args in lookups:
        check(method_name, lambda name=method_name, values=args: _check(isinstance(getattr(repo, name)(*values), dict), f"{name} must return dict for demo data"))

    bool_checks = (
        ("route_exists_by_country_name_and_name", ("Demo Country", "Demo Route")),
        ("phone_number_exists_by_normalized_number", ("525550000001",)),
        ("calling_company_exists_by_server_country_external_id", ("demo-server-1", "Demo Country", "demo-company-1")),
        ("current_tariff_exists_by_country_provider_prefix", ("Demo Country", "Demo Provider", "123")),
    )
    for method_name, args in bool_checks:
        check(method_name, lambda name=method_name, values=args: _check(type(getattr(repo, name)(*values)) is bool, f"{name} must return bool"))

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
