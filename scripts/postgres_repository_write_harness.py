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

from app.repository import Repository
from scripts.postgres_repository_smoke import mask_postgres_url, sanitize_error

DEFAULT_PROBE_KEY = "__stage51_rollback_probe__"
DEFAULT_PROBE_VALUE = "5151"
APP_SETTING_PROBE_KEY = "__stage52_app_setting_probe__"
HLR_DAILY_USAGE_PROBE_DATE = "2099-12-31"


def empty_summary(postgres_url: str) -> dict:
    return {
        "status": "failed", "postgres_url": mask_postgres_url(postgres_url),
        "checks_count": 0, "failures": [],
        "probes": {name: "skipped" for name in (
            "rollback_probe", "aborted_transaction_probe", "savepoint_probe",
            "app_setting_probe", "hlr_daily_usage_probe",
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
