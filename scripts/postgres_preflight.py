#!/usr/bin/env python3
"""Read-only SQLite preflight checks for a future PostgreSQL migration."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

MAX_SAMPLES = 5
SENSITIVE_RE = re.compile(r"(password|passwd|token|secret|api[_-]?key|api[_-]?secret|salt|hash)", re.I)
PHONE_RE = re.compile(r"^\+?\d{7,21}$")


@dataclass
class CheckResult:
    level: str
    check: str
    table: str | None
    column: str | None
    message: str
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    count: int | None = None


@dataclass
class PreflightReport:
    db_path: str
    schema_path: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def errors_count(self) -> int:
        return sum(1 for r in self.results if r.level == "error")

    @property
    def warnings_count(self) -> int:
        return sum(1 for r in self.results if r.level == "warning")

    @property
    def info_count(self) -> int:
        return sum(1 for r in self.results if r.level == "info")

    def add(self, level: str, check: str, message: str, table: str | None = None,
            column: str | None = None, sample_rows: list[dict[str, Any]] | None = None,
            count: int | None = None) -> None:
        self.results.append(CheckResult(level, check, table, column, message, sample_rows or [], count))

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "schema_path": self.schema_path,
            "errors_count": self.errors_count,
            "warnings_count": self.warnings_count,
            "info_count": self.info_count,
            "results": [asdict(r) for r in self.results],
        }


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def open_read_only(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_pg_tables(schema_path: Path) -> set[str]:
    sql = schema_path.read_text(encoding="utf-8")
    return {m.group(1).split(".")[-1].strip('"') for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w\".]+)", sql, re.I)}


def sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return {r["name"] for r in rows if not r["name"].startswith("sqlite_")}


def columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    try:
        return {r["name"]: r for r in conn.execute(f"PRAGMA table_info({quote_ident(table)})")}
    except sqlite3.DatabaseError:
        return {}


def table_exists(tables: set[str], table: str) -> bool:
    return table in tables


def safe_value(column: str, value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    if SENSITIVE_RE.search(column):
        return "***MASKED***"
    if PHONE_RE.match(text):
        return text[:5] + "****" + text[-4:] if len(text) > 9 else "***MASKED_PHONE***"
    if len(text) > 120:
        text = text[:117] + "..."
    for key in ("password", "token", "secret", "api_key", "api_secret"):
        text = re.sub(rf'("{key}"\s*:\s*")[^"]+', rf'\1***MASKED***', text, flags=re.I)
    return text


def fetch_samples(conn: sqlite3.Connection, table: str, where: str, params: tuple = (), cols: Iterable[str] = ("id",)) -> list[dict[str, Any]]:
    table_cols = columns(conn, table)
    select_cols = [c for c in cols if c in table_cols]
    if not select_cols:
        select_cols = ["rowid"]
    sql = f"SELECT {', '.join(quote_ident(c) for c in select_cols)} FROM {quote_ident(table)} WHERE {where} LIMIT {MAX_SAMPLES}"
    samples = []
    for row in conn.execute(sql, params):
        samples.append({k: safe_value(k, row[k]) for k in row.keys()})
    return samples


def check_inventory(conn, report, pg_tables):
    st = sqlite_tables(conn)
    missing_pg = sorted(st - pg_tables)
    missing_sqlite = sorted(pg_tables - st)
    if missing_pg:
        report.add("warning", "table_inventory", "SQLite tables are not present in PostgreSQL draft", sample_rows=[{"table": t} for t in missing_pg], count=len(missing_pg))
    if missing_sqlite:
        report.add("warning", "table_inventory", "PostgreSQL draft tables are not present in SQLite runtime schema", sample_rows=[{"table": t} for t in missing_sqlite], count=len(missing_sqlite))
    ignored = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sqlite_%'")]
    report.add("info", "table_inventory", f"SQLite tables: {len(st)}; PostgreSQL draft tables: {len(pg_tables)}; ignored internal: {len(ignored)}", count=len(st))
    return st


def check_foreign_keys(conn, report, tables):
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    if rows:
        report.add("error", "foreign_key_check", "PRAGMA foreign_key_check found broken references", sample_rows=[dict(r) for r in rows[:MAX_SAMPLES]], count=len(rows))
    else:
        report.add("info", "foreign_key_check", "PRAGMA foreign_key_check returned no rows")
    custom = [("routes","country_id","countries"),("routes","provider_id","providers"),("tariffs","country_id","countries"),("tariffs","provider_id","providers"),("tariffs","provider_currency_id","currencies"),("phone_numbers","country_id","countries"),("phone_numbers","provider_id","providers"),("calling_companies","server_id","servers"),("calling_companies","country_id","countries"),("user_permissions","user_id","users"),("route_history","route_id","routes"),("tariff_change_history","tariff_id","tariffs"),("change_log","changed_by","users")]
    for table, col, parent in custom:
        if table not in tables or parent not in tables or col not in columns(conn, table):
            continue
        sql = f"SELECT COUNT(*) c FROM {quote_ident(table)} t LEFT JOIN {quote_ident(parent)} p ON t.{quote_ident(col)}=p.id WHERE t.{quote_ident(col)} IS NOT NULL AND p.id IS NULL"
        count = conn.execute(sql).fetchone()["c"]
        if count:
            samples = fetch_samples(conn, table, f"{quote_ident(col)} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {quote_ident(parent)} p WHERE p.id={quote_ident(table)}.{quote_ident(col)})", cols=("id", col))
            report.add("error", "orphan_fk", f"{table}.{col} references missing {parent}.id", table, col, samples, count)


def check_duplicates(conn, report, tables):
    checks = [("users", ["username"], None), ("countries", ["name"], None), ("providers", ["normalized_name"], None), ("providers", ["name"], None), ("currencies", ["code"], None), ("routes", ["country_id", "name"], None), ("phone_numbers", ["normalized_number"], "normalized_number IS NOT NULL AND trim(normalized_number) <> ''"), ("user_permissions", ["user_id", "section_key"], None), ("app_settings", ["key"], None), ("hlr_daily_usage", ["usage_date"], None), ("calling_companies", ["server_id", "country_id", "company_id_external"], None)]
    for table, cols, where in checks:
        tc = columns(conn, table) if table in tables else {}
        if not tc or any(c not in tc for c in cols):
            continue
        exprs = [quote_ident(c) for c in cols]
        sql_where = f"WHERE {where}" if where else ""
        sql = f"SELECT {', '.join(exprs)}, COUNT(*) c, GROUP_CONCAT(id) ids FROM {quote_ident(table)} {sql_where} GROUP BY {', '.join(exprs)} HAVING COUNT(*) > 1 LIMIT {MAX_SAMPLES}"
        rows = conn.execute(sql).fetchall()
        if rows:
            report.add("error", "duplicate_business_key", f"Duplicate future unique key on {table}({', '.join(cols)})", table, ",".join(cols), [{k: safe_value(k, r[k]) for k in r.keys()} for r in rows], len(rows))
    if "tariffs" in tables and {"country_id","provider_id","provider_prefix_id","is_current"}.issubset(columns(conn,"tariffs")):
        rows = conn.execute("SELECT country_id, provider_id, COALESCE(provider_prefix_id,0) provider_prefix_key, COUNT(*) c, GROUP_CONCAT(id) ids FROM tariffs WHERE is_current=1 GROUP BY country_id, provider_id, COALESCE(provider_prefix_id,0) HAVING COUNT(*)>1 LIMIT ?", (MAX_SAMPLES,)).fetchall()
        if rows:
            report.add("error", "duplicate_business_key", "Duplicate current tariff business key", "tariffs", "country_id,provider_id,provider_prefix_id", [dict(r) for r in rows], len(rows))


def check_boolean_timestamp_date_numeric_json_empty(conn, report, tables):
    timestamp_names = {"created_at","updated_at","changed_at","event_at","valid_from","valid_to","last_check_at","deactivated_at","started_at","finished_at","telegram_sent_at","added_at","removed_at"}
    date_names = {"rate_date","conversion_rate_date","usage_date","price_before_conversion_rate_date","price_after_conversion_rate_date","old_conversion_rate_date","new_conversion_rate_date"}
    json_targets = {("change_log","old_values"):"error",("change_log","new_values"):"error",("routing_events","snapshot_json"):"error",("import_jobs","preview_data"):"error",("import_jobs","summary"):"error",("import_jobs","error_report"):"error",("route_phone_number_history","old_values"):"warning",("route_phone_number_history","new_values"):"warning",("route_history","old_value"):"warning",("route_history","new_value"):"warning",("phone_number_history","old_value"):"warning",("phone_number_history","new_value"):"warning"}
    required = {("users","username"), ("countries","name"), ("providers","name"), ("currencies","code"), ("routes","name"), ("phone_numbers","number"), ("phone_numbers","normalized_number"), ("calling_companies","company_id_external"), ("calling_companies","company_name"), ("servers","name"), ("projects","name"), ("projects","code")}
    for table in sorted(tables):
        tc = columns(conn, table)
        for col, meta in tc.items():
            qcol = quote_ident(col)
            notnull = bool(meta["notnull"] or meta["pk"])
            # boolean by name
            if col.startswith(("is_","has_","can_")) or col in {"must_change_password","review_required","include_in_route_name","provider_changed","old_company_has_autorotation","new_company_has_autorotation","has_autorotation_snapshot","inbound_line_available"}:
                count = conn.execute(f"SELECT COUNT(*) c FROM {quote_ident(table)} WHERE {qcol} IS NOT NULL AND {qcol} NOT IN (0,1)").fetchone()["c"]
                if count:
                    report.add("error" if notnull else "warning", "boolean_values", f"Invalid boolean-target values in {table}.{col}", table, col, fetch_samples(conn, table, f"{qcol} IS NOT NULL AND {qcol} NOT IN (0,1)", cols=("id", col)), count)
            if col in timestamp_names:
                check_temporal(conn, report, table, col, notnull, False)
            if col in date_names or (col.endswith("_date") and col not in timestamp_names):
                check_temporal(conn, report, table, col, notnull, True)
            if any(x in col for x in ("price","cost","rate","fee","credits")):
                check_numeric(conn, report, table, col)
            if (table, col) in json_targets:
                check_json(conn, report, table, col, json_targets[(table,col)])
            if (table, col) in required or (notnull and meta["type"].upper().startswith("TEXT")):
                count = conn.execute(f"SELECT COUNT(*) c FROM {quote_ident(table)} WHERE {qcol} IS NOT NULL AND trim({qcol}) = ''").fetchone()["c"]
                if count:
                    report.add("error" if (table,col) in required else "warning", "required_empty", f"Empty string in required/business field {table}.{col}", table, col, fetch_samples(conn, table, f"{qcol} IS NOT NULL AND trim({qcol}) = ''", cols=("id", col)), count)


def check_temporal(conn, report, table, col, notnull, date_only):
    fmt = ["%Y-%m-%d"] if date_only else ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"]
    bad = []
    empty = []
    for r in conn.execute(f"SELECT id, {quote_ident(col)} v FROM {quote_ident(table)} WHERE {quote_ident(col)} IS NOT NULL LIMIT 100000"):
        v = str(r["v"])
        if v == "":
            empty.append({"id": r["id"], col: ""}); continue
        try:
            if date_only and re.match(r"^\d{4}-\d{2}-\d{2}[ T]", v):
                raise ValueError
            if not any(_try_dt(v, f) for f in fmt):
                raise ValueError
        except ValueError:
            bad.append({"id": r["id"], col: safe_value(col, v)})
    rows = (empty + bad)[:MAX_SAMPLES]
    if rows:
        report.add("error" if notnull else "warning", "date_values" if date_only else "timestamp_values", f"Invalid {'date' if date_only else 'timestamp'} values in {table}.{col}", table, col, rows, len(empty)+len(bad))


def _try_dt(v, fmt):
    try: datetime.strptime(v, fmt); return True
    except ValueError: return False


def check_numeric(conn, report, table, col):
    bad=[]
    for r in conn.execute(f"SELECT id, {quote_ident(col)} v FROM {quote_ident(table)} WHERE {quote_ident(col)} IS NOT NULL LIMIT 100000"):
        v=str(r["v"])
        try:
            if "," in v: raise InvalidOperation
            d=Decimal(v)
            if not d.is_finite() or len(d.as_tuple().digits) > 38: raise InvalidOperation
        except (InvalidOperation, ValueError):
            bad.append({"id": r["id"], col: safe_value(col, v)})
    if bad:
        report.add("error", "numeric_values", f"Invalid numeric values in {table}.{col}", table, col, bad[:MAX_SAMPLES], len(bad))


def check_json(conn, report, table, col, level):
    bad=[]
    for r in conn.execute(f"SELECT id, {quote_ident(col)} v FROM {quote_ident(table)} WHERE {quote_ident(col)} IS NOT NULL AND trim({quote_ident(col)}) <> '' LIMIT 100000"):
        try:
            json.loads(r["v"])
        except (TypeError, json.JSONDecodeError):
            bad.append({"id": r["id"], col: safe_value(col, r["v"])})
    if bad:
        report.add(level, "json_values", f"Invalid JSON in {table}.{col}", table, col, bad[:MAX_SAMPLES], len(bad))


def check_ids(conn, report, tables):
    for table in sorted(tables):
        tc = columns(conn, table)
        if "id" not in tc:
            continue
        r = conn.execute(f"SELECT COUNT(*) row_count, MIN(id) min_id, MAX(id) max_id, SUM(CASE WHEN id IS NULL THEN 1 ELSE 0 END) null_ids, SUM(CASE WHEN id <= 0 THEN 1 ELSE 0 END) nonpositive_ids FROM {quote_ident(table)}").fetchone()
        report.add("info", "id_sequence_readiness", f"{table}: rows={r['row_count']}, min_id={r['min_id']}, max_id={r['max_id']}", table, "id", [dict(r)], r["row_count"])
        if r["nonpositive_ids"]:
            report.add("error", "id_sequence_readiness", f"{table}.id has non-positive values", table, "id", count=r["nonpositive_ids"])


def run_preflight(db_path: str | Path, schema_path: str | Path) -> PreflightReport:
    db_path, schema_path = Path(db_path), Path(schema_path)
    report = PreflightReport(str(db_path), str(schema_path))
    pg_tables = parse_pg_tables(schema_path)
    with open_read_only(db_path) as conn:
        tables = check_inventory(conn, report, pg_tables)
        check_foreign_keys(conn, report, tables)
        check_duplicates(conn, report, tables)
        check_boolean_timestamp_date_numeric_json_empty(conn, report, tables)
        check_ids(conn, report, tables)
    return report


def format_text(report: PreflightReport) -> str:
    lines = ["PostgreSQL migration preflight", f"DB: {report.db_path}", f"Schema draft: {report.schema_path}", "", f"ERRORS: {report.errors_count}", f"WARNINGS: {report.warnings_count}", f"INFO: {report.info_count}", ""]
    prefix = {"error":"[ERROR]", "warning":"[WARN]", "info":"[OK]"}
    for r in report.results:
        loc = f" {r.table}.{r.column}" if r.table and r.column else (f" {r.table}" if r.table else "")
        lines.append(f"{prefix.get(r.level, '[INFO]')} {r.check}{loc}: {r.message}" + (f" (count={r.count})" if r.count is not None else ""))
        for sample in r.sample_rows[:MAX_SAMPLES]:
            lines.append("  sample: " + json.dumps(sample, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)


def default_db_path() -> str | None:
    return os.environ.get("SQLITE_DB_PATH") or os.environ.get("MVP_DB_PATH") or os.environ.get("DEFAULT_DB_PATH")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Read-only SQLite preflight checker for future PostgreSQL migration")
    p.add_argument("--db", default=default_db_path())
    p.add_argument("--schema", default="docs/postgres/schema.postgres.sql")
    p.add_argument("--json", action="store_true")
    p.add_argument("--output")
    p.add_argument("--fail-on-warning", action="store_true")
    args = p.parse_args(argv)
    if not args.db:
        print("ERROR: --db is required when SQLITE_DB_PATH/MVP_DB_PATH/DEFAULT_DB_PATH is not set", file=sys.stderr)
        return 2
    try:
        report = run_preflight(args.db, args.schema)
    except Exception as exc:
        print(f"ERROR: could not run preflight read-only: {exc}", file=sys.stderr)
        return 2
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2) if args.json else format_text(report)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    if report.errors_count or (args.fail_on_warning and report.warnings_count):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
