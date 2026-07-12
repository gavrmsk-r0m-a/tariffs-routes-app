#!/usr/bin/env python3
"""Draft SQLite -> PostgreSQL migration CLI for TeleRoute.

This is an offline migration tool only. It is intentionally not imported by the
runtime application and uses a lazy psycopg import only when --apply connects to
PostgreSQL.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_CONFIG = 2

MIGRATION_ORDER = [
    "users", "countries", "currencies", "providers", "projects", "servers",
    "change_reasons", "phone_number_types", "phone_assignment_types",
    "provider_prefixes", "routes", "currency_rates", "phone_numbers", "tariffs",
    "calling_companies", "company_routing_settings", "route_phone_numbers",
    "route_naming_rules", "routing_events", "server_route_priorities",
    "routing_event_servers", "route_history", "route_phone_number_history",
    "phone_number_history", "tariff_change_history", "change_log",
    "provider_change_logs", "provider_change_log_servers", "telegram_settings",
    "api_tokens", "import_jobs", "app_settings", "hlr_daily_usage",
    "user_permissions", "demo_data_state",
]

SCHEMA_ONLY_TABLES: set[str] = set()
IGNORED_SQLITE_TABLES = {"sqlite_sequence"}
NO_ID_TABLES = {"app_settings", "hlr_daily_usage", "demo_data_state"}

BOOLEAN_COLUMNS = {
    "must_change_password", "is_active", "include_in_route_name", "is_actual",
    "inbound_line_available", "is_estimated", "is_current", "review_required",
    "has_autorotation", "has_overflow", "provider_changed", "can_read",
    "can_write", "can_export", "is_enabled", "old_company_has_autorotation",
    "new_company_has_autorotation",
}
DATE_COLUMNS = {
    "rate_date", "conversion_rate_date", "usage_date",
    "price_before_conversion_rate_date", "price_after_conversion_rate_date",
    "old_conversion_rate_date", "new_conversion_rate_date",
}
TIMESTAMP_COLUMNS = {
    "created_at", "updated_at", "changed_at", "event_at", "valid_from", "valid_to",
    "checked_at", "added_at", "removed_at", "deactivated_at", "telegram_sent_at",
    "started_at", "finished_at", "last_used_at", "last_test_at",
}
NUMERIC_RE = re.compile(r"(price|rate|cost|fee|credit|delta|spent|eur)", re.I)
JSONB_COLUMNS = {
    ("change_log", "old_values"), ("change_log", "new_values"),
    ("routing_events", "snapshot_json"), ("import_jobs", "preview_data"),
    ("import_jobs", "summary"), ("import_jobs", "error_report"),
}


class MigrationError(Exception):
    """Data validation or migration error."""


class ConfigError(Exception):
    """Tool configuration or connection error."""


@dataclass
class TablePlan:
    name: str
    sqlite_columns: list[str]
    pg_columns: list[str]
    insert_columns: list[str]
    has_id: bool
    row_count: int = 0


@dataclass
class MigrationReport:
    sqlite_db: str
    postgres_target: str | None
    schema_path: str
    mode: str
    tables_count: int = 0
    total_rows: int = 0
    inserted_rows: dict[str, int] = field(default_factory=dict)
    skipped_schema_only_tables: list[str] = field(default_factory=list)
    sequence_reset_tables: list[str] = field(default_factory=list)
    validation_result: str = "not_run"
    duration_seconds: float = 0.0
    status: str = "pending"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def open_sqlite_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise ConfigError(f"SQLite database not found: {path}")
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def connect_postgres(postgres_url: str):
    try:
        import psycopg  # type: ignore
    except ImportError as exc:
        raise ConfigError("Install psycopg to run migration: pip install psycopg[binary]") from exc
    try:
        return psycopg.connect(postgres_url)
    except Exception as exc:  # pragma: no cover - requires PostgreSQL server
        raise ConfigError(f"Could not connect to PostgreSQL: {exc}") from exc


def mask_postgres_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, hostinfo = netloc.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]
        netloc = f"{user}:***@{hostinfo}" if user else f"***@{hostinfo}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def parse_create_table_columns(schema_sql: str) -> dict[str, list[str]]:
    tables: dict[str, list[str]] = {}
    pattern = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\w+\.)?\"?(\w+)\"?\s*\((.*?)\);", re.I | re.S)
    for table, body in pattern.findall(schema_sql):
        cols = []
        for raw in body.splitlines():
            line = raw.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            first = line.split(None, 1)[0].strip('"')
            if first.upper() in {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK"}:
                continue
            cols.append(first)
        tables[table] = cols
    return tables


def sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows if not r["name"].startswith("sqlite_")}


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({quote_ident(table)})")]


def validate_table_coverage(sqlite_names: set[str], pg_names: set[str]) -> None:
    order = set(MIGRATION_ORDER)
    missing_sqlite = sorted(sqlite_names - order - IGNORED_SQLITE_TABLES)
    missing_pg = sorted(pg_names - order - SCHEMA_ONLY_TABLES)
    if missing_sqlite or missing_pg:
        raise MigrationError(f"Table coverage failed; sqlite missing from order={missing_sqlite}; pg missing from order={missing_pg}")


def build_plan(conn: sqlite3.Connection, schema_path: str | Path, selected_tables: list[str] | None = None) -> list[TablePlan]:
    schema_sql = Path(schema_path).read_text(encoding="utf-8")
    pg_columns = parse_create_table_columns(schema_sql)
    st = sqlite_tables(conn)
    validate_table_coverage(st, set(pg_columns))
    unknown_selected = sorted(set(selected_tables or []) - set(MIGRATION_ORDER))
    if unknown_selected:
        raise MigrationError(f"Unknown --tables entries: {', '.join(unknown_selected)}")
    wanted = selected_tables or MIGRATION_ORDER
    plan = []
    for table in wanted:
        if table not in st or table in SCHEMA_ONLY_TABLES:
            continue
        scols = sqlite_columns(conn, table)
        pcols = pg_columns.get(table, [])
        insert_cols = [c for c in pcols if c in scols]
        has_id = "id" in insert_cols
        count = conn.execute(f"SELECT COUNT(*) AS c FROM {quote_ident(table)}").fetchone()["c"]
        plan.append(TablePlan(table, scols, pcols, insert_cols, has_id, count))
    return plan


def convert_boolean(value: Any) -> bool | None:
    if value is None:
        return None
    if value in (0, "0", False):
        return False
    if value in (1, "1", True):
        return True
    raise MigrationError(f"Invalid boolean value: {value!r}")


def convert_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise MigrationError(f"Invalid date value: {value!r}") from exc


def convert_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
    raise MigrationError(f"Invalid timestamp value: {value!r}")


def convert_numeric(value: Any) -> Decimal | None:
    if value is None:
        return None
    if value == "":
        raise MigrationError("Invalid numeric value: empty string")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MigrationError(f"Invalid numeric value: {value!r}") from exc


def convert_jsonb(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise MigrationError(f"Invalid JSONB value: {value!r}") from exc


def is_date_column(column: str) -> bool:
    return column in DATE_COLUMNS or column.endswith("_date")


def is_timestamp_column(column: str) -> bool:
    return column in TIMESTAMP_COLUMNS or column.endswith("_at")


def is_numeric_column(column: str) -> bool:
    return bool(NUMERIC_RE.search(column)) and not is_date_column(column)


def convert_value(table: str, column: str, value: Any) -> Any:
    if (table, column) in JSONB_COLUMNS:
        return convert_jsonb(value)
    if column in BOOLEAN_COLUMNS:
        return convert_boolean(value)
    if is_date_column(column):
        return convert_date(value)
    if is_timestamp_column(column):
        return convert_timestamp(value)
    if is_numeric_column(column):
        return convert_numeric(value)
    return value


def sequence_reset_sql(table: str) -> str:
    q = quote_ident(table)
    return (
        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
        f"COALESCE((SELECT MAX(id) FROM {q}), 1), "
        f"(SELECT COUNT(*) FROM {q}) > 0);"
    )


def sequence_reset_tables(plan: list[TablePlan]) -> list[str]:
    return [p.name for p in plan if p.has_id]


def ensure_pg_tables_empty(pg_conn, plan: list[TablePlan]) -> None:
    with pg_conn.cursor() as cur:
        for p in plan:
            cur.execute(f"SELECT COUNT(*) FROM {quote_ident(p.name)}")
            count = cur.fetchone()[0]
            if count:
                raise MigrationError(f"PostgreSQL table {p.name} is not empty ({count} rows)")


def pg_jsonb(value: Any) -> Any:
    try:
        from psycopg.types.json import Jsonb  # type: ignore
    except ImportError as exc:  # pragma: no cover - apply already checks psycopg
        raise ConfigError("Install psycopg to run migration: pip install psycopg[binary]") from exc
    return Jsonb(value)


def convert_for_postgres(table: str, column: str, value: Any) -> Any:
    converted = convert_value(table, column, value)
    if converted is not None and (table, column) in JSONB_COLUMNS:
        return pg_jsonb(converted)
    return converted


def import_table(sqlite_conn: sqlite3.Connection, pg_conn, table_plan: TablePlan) -> int:
    cols = table_plan.insert_columns
    if not cols:
        return 0
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO {quote_ident(table_plan.name)} ({', '.join(quote_ident(c) for c in cols)}) VALUES ({placeholders})"
    inserted = 0
    with pg_conn.cursor() as cur:
        for row in sqlite_conn.execute(f"SELECT {', '.join(quote_ident(c) for c in cols)} FROM {quote_ident(table_plan.name)}"):
            try:
                values = [convert_for_postgres(table_plan.name, c, row[c]) for c in cols]
                cur.execute(sql, values)
                inserted += 1
            except Exception as exc:
                sample = row["id"] if "id" in row.keys() else dict(row)
                raise MigrationError(f"Failed importing {table_plan.name} sample={sample!r}: {exc}") from exc
    return inserted


def validate_counts(sqlite_conn: sqlite3.Connection, pg_conn, plan: list[TablePlan]) -> None:
    with pg_conn.cursor() as cur:
        for p in plan:
            cur.execute(f"SELECT COUNT(*) FROM {quote_ident(p.name)}")
            pg_count = cur.fetchone()[0]
            if pg_count != p.row_count:
                raise MigrationError(f"Row count mismatch for {p.name}: SQLite={p.row_count}, PostgreSQL={pg_count}")
            if p.has_id and p.row_count:
                s = sqlite_conn.execute(f"SELECT MIN(id) min_id, MAX(id) max_id FROM {quote_ident(p.name)}").fetchone()
                cur.execute(f"SELECT MIN(id), MAX(id) FROM {quote_ident(p.name)}")
                pg_min, pg_max = cur.fetchone()
                if (s["min_id"], s["max_id"]) != (pg_min, pg_max):
                    raise MigrationError(f"ID range mismatch for {p.name}")


def run_apply(sqlite_conn: sqlite3.Connection, pg_url: str, schema_path: Path, plan: list[TablePlan], create_schema: bool, drop_existing: bool) -> tuple[dict[str, int], list[str]]:
    pg_conn = connect_postgres(pg_url)
    inserted: dict[str, int] = {}
    reset_tables = sequence_reset_tables(plan)
    try:
        with pg_conn:
            with pg_conn.cursor() as cur:
                if drop_existing:
                    # Draft tool safety: schema SQL uses unqualified public tables.
                    cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
                if create_schema:
                    cur.execute(schema_path.read_text(encoding="utf-8"))
            ensure_pg_tables_empty(pg_conn, plan)
            for p in plan:
                inserted[p.name] = import_table(sqlite_conn, pg_conn, p)
            with pg_conn.cursor() as cur:
                for table in reset_tables:
                    cur.execute(sequence_reset_sql(table))
            validate_counts(sqlite_conn, pg_conn, plan)
        return inserted, reset_tables
    except Exception:
        pg_conn.rollback()
        raise
    finally:
        pg_conn.close()


def print_text_report(report: MigrationReport, plan: list[TablePlan]) -> None:
    print("Migration plan" if report.mode == "dry-run" else "Migration report")
    print(f"SQLite DB: {report.sqlite_db}")
    print(f"PostgreSQL: {report.postgres_target or '(not required for dry-run)'}")
    print(f"Schema: {report.schema_path}\n")
    print(f"Tables: {report.tables_count}")
    print(f"Rows total: {report.total_rows}\n")
    for p in plan:
        print(f"{p.name}: {p.row_count}")
    if report.status == "success" and report.mode == "dry-run":
        print("\nReady for --apply.")
    elif report.error:
        print(f"\nERROR: {report.error}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Draft SQLite to PostgreSQL migration tool")
    parser.add_argument("--sqlite-db", default=os.environ.get("SQLITE_DB"))
    parser.add_argument("--postgres-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--schema", default="docs/postgres/schema.postgres.sql")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--create-schema", action="store_true")
    parser.add_argument("--drop-existing", action="store_true")
    parser.add_argument("--tables", nargs="+")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    if args.apply:
        args.dry_run = False
    return args


def run(argv: list[str] | None = None) -> int:
    start = time.monotonic()
    args = parse_args(argv)
    report = MigrationReport(args.sqlite_db or "", mask_postgres_url(args.postgres_url), args.schema, "apply" if args.apply else "dry-run")
    plan: list[TablePlan] = []
    try:
        if not args.sqlite_db:
            raise ConfigError("--sqlite-db is required (or SQLITE_DB env fallback)")
        schema_path = Path(args.schema)
        if not schema_path.exists():
            raise ConfigError(f"Schema file not found: {schema_path}")
        if args.apply and not args.yes:
            raise ConfigError("--apply requires --yes")
        if args.apply and not args.postgres_url:
            raise ConfigError("--apply requires --postgres-url or DATABASE_URL")
        if args.drop_existing and not (args.apply and args.yes):
            raise ConfigError("--drop-existing requires --apply --yes")
        with open_sqlite_readonly(args.sqlite_db) as sqlite_conn:
            plan = build_plan(sqlite_conn, schema_path, args.tables)
            report.tables_count = len(plan)
            report.total_rows = sum(p.row_count for p in plan)
            if args.apply:
                inserted, reset = run_apply(sqlite_conn, args.postgres_url, schema_path, plan, args.create_schema, args.drop_existing)
                report.inserted_rows = inserted
                report.sequence_reset_tables = reset
                report.validation_result = "passed"
            else:
                report.inserted_rows = {p.name: 0 for p in plan}
                report.sequence_reset_tables = sequence_reset_tables(plan)
                report.validation_result = "planned"
        report.status = "success"
        code = EXIT_OK
    except MigrationError as exc:
        report.status = "fail"
        report.error = str(exc)
        code = EXIT_VALIDATION
    except ConfigError as exc:
        report.status = "fail"
        report.error = str(exc)
        code = EXIT_CONFIG
    report.duration_seconds = round(time.monotonic() - start, 3)
    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print_text_report(report, plan)
    return code


if __name__ == "__main__":
    sys.exit(run())
