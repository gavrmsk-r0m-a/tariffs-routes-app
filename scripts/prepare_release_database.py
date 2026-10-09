#!/usr/bin/env python3
"""Safely remove test operational data before the first production release."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable

CONFIRMATION = "CLEAN_TEST_OPERATIONAL_DATA"

# Child-first order is intentional and mirrors the canonical PostgreSQL FKs.
CLEANUP_TABLES = (
    "spam_check_result_routes", "spam_check_results", "phone_spam_state",
    "spam_check_batches", "routing_event_routes", "routing_event_servers",
    "routing_events", "company_routing_settings", "server_route_priorities",
    "provider_change_log_servers", "provider_change_logs",
    "route_phone_number_history", "route_phone_numbers", "route_history",
    "phone_number_history", "tariff_change_history", "phone_numbers",
    "tariffs", "calling_companies", "routes",
)
OPERATIONAL_CHANGE_LOG_TYPES = (
    "route", "tariff", "phone_number", "route_phone_number",
    "calling_company", "company_routing_setting", "server_route_priority",
    "routing_event", "provider_change_log", "spam_check_batch",
    "spam_check_result", "phone_spam_state",
)
PRESERVED_TABLES = (
    "users", "user_permissions", "change_reasons", "change_reason_scopes",
    "countries", "currencies", "currency_rates", "providers",
    "provider_prefixes", "servers", "projects", "phone_number_types",
    "phone_assignment_types", "route_naming_rules", "api_tokens",
    "app_settings", "telegram_settings",
)


def _count(conn, table: str) -> int:
    # All identifiers originate in constants above, never operator input.
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def collect_counts(conn, tables=CLEANUP_TABLES) -> dict[str, int]:
    return {table: _count(conn, table) for table in tables}


def preserved_fingerprints(conn) -> dict[str, tuple[int, str]]:
    fingerprints = {}
    for table in PRESERVED_TABLES:
        row = conn.execute(f'''SELECT COUNT(*), md5(COALESCE(string_agg(row_hash, '' ORDER BY row_hash), ''))
            FROM (SELECT md5(row_to_json(t)::text) AS row_hash FROM "{table}" AS t) AS rows''').fetchone()
        fingerprints[table] = (int(row[0]), str(row[1]))
    return fingerprints


def _reset_identities(conn) -> None:
    for table in CLEANUP_TABLES:
        identity_columns = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
              AND (
                  is_identity = 'YES'
                  OR column_default LIKE 'nextval(%%'
              )
            ORDER BY ordinal_position
            """,
            (table,),
        ).fetchall()

        for (column_name,) in identity_columns:
            sequence = conn.execute(
                "SELECT pg_get_serial_sequence(%s, %s)",
                (f"public.{table}", column_name),
            ).fetchone()[0]
            if sequence:
                conn.execute("SELECT setval(%s, 1, false)", (sequence,))

    sequence = conn.execute(
        "SELECT pg_get_serial_sequence('public.change_log', 'id')"
    ).fetchone()[0]
    if sequence:
        maximum = int(
            conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM change_log"
            ).fetchone()[0]
        )
        conn.execute(
            "SELECT setval(%s, %s, %s)",
            (sequence, max(maximum, 1), maximum > 0),
        )


def prepare_release_database(conn, *, apply: bool = False,
                             inject_failure: Callable[[], None] | None = None) -> dict:
    """Preview or transactionally apply cleanup using an existing connection."""
    if apply:
        cleanup_names = ", ".join(f'"{table}"' for table in CLEANUP_TABLES)
        preserved_names = ", ".join(f'"{table}"' for table in PRESERVED_TABLES)
        conn.execute(f"LOCK TABLE {cleanup_names}, change_log IN ACCESS EXCLUSIVE MODE")
        conn.execute(f"LOCK TABLE {preserved_names} IN SHARE MODE")
    before = collect_counts(conn)
    change_log_before = int(conn.execute(
        "SELECT COUNT(*) FROM change_log WHERE entity_type = ANY(%s)",
        (list(OPERATIONAL_CHANGE_LOG_TYPES),),
    ).fetchone()[0])
    result = {"mode": "apply" if apply else "dry-run", "before": before,
              "operational_change_log_entries": change_log_before}
    if not apply:
        conn.rollback()  # Guarantee the preview leaves no open or mutated transaction.
        return result

    preserved_before = preserved_fingerprints(conn)
    try:
        for table in CLEANUP_TABLES:
            conn.execute(f'DELETE FROM "{table}"')
        conn.execute("DELETE FROM change_log WHERE entity_type = ANY(%s)",
                     (list(OPERATIONAL_CHANGE_LOG_TYPES),))
        _reset_identities(conn)
        if inject_failure:
            inject_failure()
        after = collect_counts(conn)
        preserved_after = preserved_fingerprints(conn)
        if preserved_after != preserved_before:
            raise RuntimeError("preserved table verification failed")
        if any(after.values()):
            raise RuntimeError("operational table verification failed")
        change_log_after = int(conn.execute(
            "SELECT COUNT(*) FROM change_log WHERE entity_type = ANY(%s)",
            (list(OPERATIONAL_CHANGE_LOG_TYPES),),
        ).fetchone()[0])
        if change_log_after:
            raise RuntimeError("operational change-log verification failed")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    result["after"] = after
    result["operational_change_log_entries_after"] = change_log_after
    result["preserved"] = {table: {"rows": value[0], "verified": True}
                           for table, value in preserved_after.items()}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform destructive cleanup")
    parser.add_argument("--confirm", help=f"required with --apply: {CONFIRMATION}")
    args = parser.parse_args(argv)
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply requires --confirm {CONFIRMATION}")
    if not args.apply and args.confirm:
        parser.error("--confirm is only valid with --apply")
    if (os.environ.get("DB_BACKEND") or "").strip().lower() != "postgres":
        print("DB_BACKEND must equal postgres", file=sys.stderr)
        return 2
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        import psycopg
        with psycopg.connect(database_url) as conn:
            result = prepare_release_database(conn, apply=args.apply)
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        # Do not include driver exception details: they can echo connection data.
        print(f"Release database cleanup failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
