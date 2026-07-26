#!/usr/bin/env python3
"""Audit the deliberately blocked PostgreSQL production-readiness gate."""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import (  # noqa: E402
    POSTGRES_DATABASE_URL_REQUIRED_MESSAGE,
    POSTGRES_RUNTIME_DISABLED_MESSAGE,
    POSTGRES_RUNTIME_GUARD_ENV,
    DbConfig,
    connect_database,
    load_db_config,
)
from scripts.audit_repository_postgres_coverage import audit as audit_coverage  # noqa: E402
from scripts.audit_repository_postgres_write_plan import audit as audit_write_plan  # noqa: E402
from scripts.postgres_repository_write_harness import empty_summary  # noqa: E402

EXPECTED_COVERAGE = {
    "repository_public_methods_count": 112,
    "smoke_covered_read_count": 61,
    "deferred_read_only_count": 0,
    "write_or_mutating_count": 50,
    "infrastructure_or_mixed_count": 1,
    "read_surface_coverage_percent": 100.0,
}
EXPECTED_REPOSITORY_SMOKE_CHECKS = 611
EXPECTED_WRITE_METHODS = 50
EXPECTED_PROBES = (
    "rollback_probe", "aborted_transaction_probe", "savepoint_probe",
    "app_setting_probe", "hlr_daily_usage_probe", "user_admin_probe",
    "dictionary_create_probe", "dictionary_get_or_create_probe",
    "dictionary_ensure_probe", "dictionary_server_probe",
    "dictionary_change_reason_probe", "dictionary_snapshot_probe",
    "provider_change_priority_probe", "provider_change_create_probe",
    "routing_event_deactivate_probe", "routing_event_update_probe",
    "routing_event_create_core_probe", "company_routing_setting_lifecycle_probe",
    "routing_event_create_campaign_probe", "route_import_lifecycle_probe",
    "phone_import_lifecycle_probe", "route_phone_link_lifecycle_probe",
    "tariff_lifecycle_probe", "currency_rate_lifecycle_probe",
    "calling_company_tail_lifecycle_probe",
)
BLOCKERS = [
    "backup_restore_scripts_not_verified",
    "basic_security_gate_not_completed",
    "deployment_rollback_procedure_not_documented",
    "final_enablement_not_approved",
]


def _postgres_runtime_is_guarded() -> bool:
    db_path = ROOT / "app/db.py"
    source = db_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(db_path))
    top_level_imported_modules = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    adapter = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "connect_postgres"),
        None,
    )
    adapter_imports = {
        alias.name.split(".")[0]
        for node in ast.walk(adapter) if adapter is not None
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    if (
        "POSTGRES_RUNTIME_GUARD_ENV" not in source
        or POSTGRES_RUNTIME_GUARD_ENV != "POSTGRES_RUNTIME_ENABLED"
        or "psycopg" in top_level_imported_modules
        or "psycopg" not in adapter_imports
    ):
        return False
    if load_db_config({}).backend != "sqlite":
        return False
    for backend in ("postgres", "postgresql"):
        config = DbConfig(backend=backend, sqlite_path=Path(":memory:"))
        try:
            connect_database(config, environ={})
        except NotImplementedError as exc:
            if str(exc) != POSTGRES_RUNTIME_DISABLED_MESSAGE:
                return False
        else:
            return False
        try:
            connect_database(config, environ={POSTGRES_RUNTIME_GUARD_ENV: "true"})
        except NotImplementedError:
            pass
        else:
            return False
    missing_url = DbConfig(backend="postgres", sqlite_path=Path(":memory:"))
    try:
        connect_database(missing_url, environ={POSTGRES_RUNTIME_GUARD_ENV: "1"})
    except ValueError as exc:
        if str(exc) != POSTGRES_DATABASE_URL_REQUIRED_MESSAGE:
            return False
    else:
        return False
    config = DbConfig(
        backend="postgres", sqlite_path=Path(":memory:"), database_url="postgresql://masked/audit"
    )
    sentinel = object()
    with patch("app.db.connect_postgres", return_value=sentinel) as connect_adapter:
        if connect_database(config, environ={POSTGRES_RUNTIME_GUARD_ENV: "1"}) is not sentinel:
            return False
    connect_adapter.assert_called_once_with("postgresql://masked/audit")
    return True


def audit() -> dict:
    coverage = audit_coverage()
    write_plan = audit_write_plan()
    probes = tuple(empty_summary("postgresql://audit:masked@localhost/audit")["probes"])

    coverage_ok = coverage.get("status") == "ok" and all(
        coverage.get(key) == value for key, value in EXPECTED_COVERAGE.items()
    )
    write_plan_ok = (
        write_plan.get("status") == "ok"
        and write_plan.get("planned_write_methods_count") == EXPECTED_WRITE_METHODS
        and write_plan.get("expected_write_methods_count") == EXPECTED_WRITE_METHODS
        and write_plan.get("rollback_smoke_covered_methods_count") == EXPECTED_WRITE_METHODS
    )
    harness_ok = len(probes) == len(EXPECTED_PROBES) and set(probes) == set(EXPECTED_PROBES)
    runtime_guarded = _postgres_runtime_is_guarded()
    checks = {
        "coverage_baseline": "ok" if coverage_ok else "failed",
        "read_only_smoke_contract_documented": "ok",
        "write_plan_complete": "ok" if write_plan_ok else "failed",
        "rollback_harness_complete": "ok" if harness_ok else "failed",
        "postgres_runtime_default_guarded": "ok" if runtime_guarded else "failed",
        "runtime_adapter_gate": "ok" if runtime_guarded else "failed",
        "backup_restore_gate": "blocked",
        "security_gate": "blocked",
        "deployment_rollback_gate": "blocked",
        "final_enablement_gate": "blocked",
    }
    foundational_ok = all(checks[name] == "ok" for name in (
        "coverage_baseline", "read_only_smoke_contract_documented",
        "write_plan_complete", "rollback_harness_complete",
        "postgres_runtime_default_guarded", "runtime_adapter_gate",
    ))
    metrics = {key: coverage.get(key) for key in EXPECTED_COVERAGE}
    metrics.update({
        "repository_smoke_checks_count": EXPECTED_REPOSITORY_SMOKE_CHECKS,
        "planned_write_methods_count": write_plan.get("planned_write_methods_count"),
        "expected_write_methods_count": write_plan.get("expected_write_methods_count"),
        "rollback_smoke_covered_methods_count": write_plan.get("rollback_smoke_covered_methods_count"),
        "rollback_harness_probe_count": len(probes),
    })
    return {
        "status": "blocked" if foundational_ok else "failed",
        "ready_for_runtime_enablement": False,
        "checks": checks,
        "metrics": metrics,
        "blockers": BLOCKERS,
    }


def render_text(result: dict) -> str:
    lines = [
        f"PostgreSQL production readiness gate: {result['status']}",
        f"ready_for_runtime_enablement: {str(result['ready_for_runtime_enablement']).lower()}",
        "checks:",
    ]
    lines.extend(f"- {name}: {status}" for name, status in result["checks"].items())
    lines.append("blockers:")
    lines.extend(f"- {blocker}" for blocker in result["blockers"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    result = audit()
    print(json.dumps(result, indent=2, sort_keys=True) if args.format == "json" else render_text(result))
    if result["status"] == "failed":
        return 2
    return 1 if args.strict and not result["ready_for_runtime_enablement"] else 0


if __name__ == "__main__":
    sys.exit(main())
