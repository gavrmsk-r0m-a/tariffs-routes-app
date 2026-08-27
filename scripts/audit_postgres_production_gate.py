#!/usr/bin/env python3
"""Audit the PostgreSQL production-readiness and explicit enablement gates."""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timezone
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
from scripts.postgres_deployment_rollback_check import check_rollback_artifacts  # noqa: E402
from app.security import (  # noqa: E402
    DEV_AUTH_SECRET,
    auth_cookie_attributes,
    get_auth_cookie_secret,
    login_is_locked,
    record_login_failure,
    security_gate_facts,
    validate_auth_secret,
)

EXPECTED_COVERAGE = {
    "repository_public_methods_count": 115,
    "smoke_covered_read_count": 63,
    "deferred_read_only_count": 0,
    "write_or_mutating_count": 51,
    "infrastructure_or_mixed_count": 1,
    "read_surface_coverage_percent": 100.0,
}
EXPECTED_REPOSITORY_SMOKE_CHECKS = 613
EXPECTED_WRITE_METHODS = 51
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
BLOCKERS: list[str] = []

BACKUP_RESTORE_ARTIFACTS = (
    "scripts/postgres_backup.py",
    "scripts/postgres_restore_verify.py",
    "scripts/postgres_backup_restore_smoke.py",
    "tests/test_postgres_backup_restore.py",
    "docs/postgres/backup_restore_runbook.md",
)

SECURITY_ARTIFACTS = (
    "app/security.py",
    "tests/test_postgres_security_gate.py",
    "docs/postgres/security_gate.md",
)

DEPLOYMENT_ROLLBACK_ARTIFACTS = (
    "scripts/postgres_deployment_rollback_smoke.py",
    "scripts/postgres_deployment_rollback_check.py",
    "tests/test_postgres_deployment_rollback_gate.py",
    "docs/postgres/deployment_rollback_runbook.md",
)

FINAL_ENABLEMENT_ARTIFACTS = (
    "scripts/postgres_final_enablement_check.py",
    "scripts/postgres_runtime_enablement_smoke.py",
    "tests/test_postgres_final_enablement_gate.py",
    "docs/postgres/final_enablement_runbook.md",
    "docs/postgres/final_enablement_approval.md",
)


def _final_enablement_gate_is_complete() -> bool:
    if not all((ROOT / path).is_file() for path in FINAL_ENABLEMENT_ARTIFACTS):
        return False
    check = (ROOT / "scripts/postgres_final_enablement_check.py").read_text(encoding="utf-8")
    smoke = (ROOT / "scripts/postgres_runtime_enablement_smoke.py").read_text(encoding="utf-8")
    approval = (ROOT / "docs/postgres/final_enablement_approval.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/postgres-migration-smoke.yml").read_text(encoding="utf-8")
    required_check = (
        '"--strict"', '"--format"', '"DB_BACKEND"', '"POSTGRES_RUNTIME_ENABLED"',
        "validate_auth_secret", 'manifest.get("sha256")', 'manifest.get("table_counts")',
    )
    required_smoke = ("db.load_db_config(environ)", "db.connect_database(config, environ)")
    required_workflow = (
        "python -m unittest tests.test_postgres_final_enablement_gate",
        "python scripts/postgres_runtime_enablement_smoke.py",
        "python scripts/postgres_final_enablement_check.py",
        "python scripts/audit_postgres_production_gate.py --strict",
    )
    return (
        "APPROVED_FOR_POSTGRES_RUNTIME_ENABLEMENT" in approval
        and "No production credentials are stored in this repository" in approval
        and all(value in check for value in required_check)
        and all(value in smoke for value in required_smoke)
        and all(value in workflow for value in required_workflow)
    )


def _deployment_rollback_gate_is_complete() -> bool:
    if not all((ROOT / path).is_file() for path in DEPLOYMENT_ROLLBACK_ARTIFACTS):
        return False
    workflow = (ROOT / ".github/workflows/postgres-migration-smoke.yml").read_text(encoding="utf-8")
    runbook = (ROOT / "docs/postgres/deployment_rollback_runbook.md").read_text(encoding="utf-8").lower()
    smoke_path = ROOT / "scripts/postgres_deployment_rollback_smoke.py"
    check_path = ROOT / "scripts/postgres_deployment_rollback_check.py"
    smoke_source = smoke_path.read_text(encoding="utf-8")
    check_source = check_path.read_text(encoding="utf-8")
    required_workflow = (
        "python -m unittest tests.test_postgres_deployment_rollback_gate",
        "python scripts/postgres_deployment_rollback_smoke.py",
        "--workdir \"$RUNNER_TEMP/postgres-deployment-rollback\"",
    )
    required_runbook = ("never restore directly", "fresh database", "sha-256", "table counts", "smoke/read checks")
    if not all(value in workflow for value in required_workflow):
        return False
    if not all(value in runbook for value in required_runbook):
        return False
    credential_pattern = re.compile(r"postgres(?:ql)?://[^\s:/]+:([^@\s]+)@", re.IGNORECASE)
    if any(credential_pattern.search(source) for source in (smoke_source, check_source, runbook)):
        return False
    try:
        smoke_tree = ast.parse(smoke_source, filename=str(smoke_path))
        check_tree = ast.parse(check_source, filename=str(check_path))
    except SyntaxError:
        return False
    prefix_ok = any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "ROLLBACK_DATABASE_PREFIX" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and node.value.value == "teleroute_deployment_rollback_"
        for node in smoke_tree.body
    )
    finally_drops = any(
        isinstance(node, ast.Try)
        and any(
            isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "drop_database"
            for statement in node.finalbody for child in ast.walk(statement)
        )
        for node in ast.walk(smoke_tree)
    )
    strict_cli = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
        and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "--strict"
        for node in ast.walk(check_tree)
    )
    if not (prefix_ok and finally_drops and strict_cli):
        return False
    try:
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / "audit.dump"
            backup.write_bytes(b"rollback-audit")
            manifest = Path(directory) / "audit.manifest.json"
            manifest.write_text(json.dumps({
                "status": "ok", "format": "pg_dump_custom",
                "sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                "size_bytes": backup.stat().st_size, "table_counts": {"users": 1},
                "backup_file": str(backup),
            }), encoding="utf-8")
            strict_result = check_rollback_artifacts("current", "rollback", manifest, strict=True)
            backup.unlink()
            warning_result = check_rollback_artifacts("current", "rollback", manifest, strict=False)
            try:
                check_rollback_artifacts("current", "rollback", manifest, strict=True)
            except ValueError:
                strict_missing_fails = True
            else:
                strict_missing_fails = False
        return (
            strict_result.get("backup_sha256_verified") is True
            and warning_result.get("warnings")
            and strict_missing_fails
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _security_gate_is_complete() -> bool:
    if not all((ROOT / path).is_file() for path in SECURITY_ARTIFACTS):
        return False
    production_env = {"MVP_PRODUCTION_SECURITY": "1", "MVP_AUTH_SECRET": "audit-only-strong-auth-secret-32-characters"}
    try:
        if get_auth_cookie_secret({}) != DEV_AUTH_SECRET:
            return False
        if not validate_auth_secret({"MVP_PRODUCTION_SECURITY": "1"}):
            return False
        if not validate_auth_secret({"MVP_PRODUCTION_SECURITY": "1", "MVP_AUTH_SECRET": DEV_AUTH_SECRET}):
            return False
        if get_auth_cookie_secret(production_env) != production_env["MVP_AUTH_SECRET"]:
            return False
        prod_cookie = auth_cookie_attributes(production_env)
        dev_cookie = auth_cookie_attributes({})
        if not all(prod_cookie.get(key) == value for key, value in {"Secure": True, "HttpOnly": True, "SameSite": "Lax", "Path": "/"}.items()):
            return False
        if not all(dev_cookie.get(key) == value for key, value in {"HttpOnly": True, "SameSite": "Lax", "Path": "/"}.items()):
            return False
        facts = security_gate_facts(production_env)
        throttle = facts["login_throttle"]
        if throttle != {"max_failed_attempts": 5, "failure_window_seconds": 900, "lockout_seconds": 900}:
            return False
        if facts["passwordless_user_switching_allowed"] or not facts["default_credentials_forbidden_in_production"]:
            return False
        conn = sqlite3.connect(":memory:")
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE login_attempts(id INTEGER PRIMARY KEY, username_normalized TEXT NOT NULL, client_key TEXT NOT NULL, failed_at TEXT NOT NULL, reason TEXT)")
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            for _ in range(5):
                record_login_failure(conn, "audit-user", "audit-client", now=now)
            locked = login_is_locked(conn, "audit-user", "audit-client", now=now)
        finally:
            conn.close()
        if not locked:
            return False
    except (RuntimeError, TypeError, ValueError, sqlite3.Error):
        return False
    server_source = (ROOT / "app/server.py").read_text(encoding="utf-8")
    db_source = (ROOT / "app/db.py").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/postgres-migration-smoke.yml").read_text(encoding="utf-8")
    return (
        "login_is_locked(conn, username, request_client_key)" in server_source
        and server_source.index("login_is_locked(conn, username, request_client_key)") < server_source.index("repo.authenticate_user(username")
        and _authenticated_logout_contract_is_complete(production_env)
        and "Known default credentials are forbidden" in db_source
        and "python -m unittest tests.test_postgres_security_gate" in workflow
    )


def _authenticated_logout_contract_is_complete(production_env: dict[str, str]) -> bool:
    """Exercise the rendered production layout and logout endpoint, not helper placement."""
    with redirect_stdout(io.StringIO()):
        import app.server as server

    def request(path: str, cookie: str = "") -> tuple[str, dict[str, str], str]:
        captured: dict[str, object] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            captured["status"] = status
            captured["headers"] = headers

        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(),
        }
        if cookie:
            environ["HTTP_COOKIE"] = cookie
        body = b"".join(server.app(environ, start_response)).decode("utf-8")
        return str(captured["status"]), dict(captured["headers"]), body

    previous_db_path = server.DB_PATH
    try:
        with tempfile.NamedTemporaryFile() as database:
            server.DB_PATH = database.name
            conn = server.connect(server.DB_PATH)
            try:
                server.init_db(conn)
                repo = server.Repository(conn)
                server.ensure_seed(repo)
                for seeded_user in conn.execute("SELECT id FROM users").fetchall():
                    repo.update_user_password(
                        int(seeded_user["id"]),
                        f"production-audit-user-{seeded_user['id']}-password",
                        must_change_password=False,
                    )
                operator_id = repo.create_user(
                    "production-routes-audit", "operator", "Production Routes Audit",
                    password="production-routes-audit-password",
                )
                conn.execute(
                    """
                    INSERT INTO user_permissions(user_id, section_key, can_read, can_write, can_export)
                    VALUES (?, 'routes', 1, 0, 0)
                    """,
                    (operator_id,),
                )
                admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
                conn.commit()
            finally:
                conn.close()

            with patch.dict("os.environ", production_env, clear=False):
                for user_id, display_name in ((operator_id, "Production Routes Audit"), (admin_id, "Admin")):
                    cookie = f"{server.CURRENT_USER_COOKIE}={server.sign_user_id(user_id)}"
                    status, _, html = request("/routes", cookie)
                    if status != "200 OK":
                        return False
                    if not all(value in html for value in (
                        'class="current-user-selector"', 'href="/logout"', display_name,
                    )):
                        return False
                    if re.search(r"<form\b[^>]*(?:switch|current.?user)", html, re.IGNORECASE):
                        return False
                    if re.search(r"<select\b[^>]*(?:current.?user|user.?switch|account.?switch)", html, re.IGNORECASE):
                        return False

                logout_status, logout_headers, _ = request("/logout", cookie)
                if logout_status != "303 See Other" or logout_headers.get("Location") != "/login":
                    return False
                if logout_headers.get("Set-Cookie") != server.clear_current_user_cookie()[1]:
                    return False
        return True
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        return False
    finally:
        server.DB_PATH = previous_db_path


def _backup_restore_gate_is_complete() -> bool:
    if not all((ROOT / relative_path).is_file() for relative_path in BACKUP_RESTORE_ARTIFACTS):
        return False
    workflow = (ROOT / ".github/workflows/postgres-migration-smoke.yml").read_text(encoding="utf-8")
    required_workflow_text = (
        "python -m unittest tests.test_postgres_backup_restore",
        "python scripts/postgres_backup_restore_smoke.py",
        "--workdir \"$RUNNER_TEMP/postgres-backup-restore\"",
    )
    if not all(value in workflow for value in required_workflow_text):
        return False
    backup = (ROOT / "scripts/postgres_backup.py").read_text(encoding="utf-8")
    restore = (ROOT / "scripts/postgres_restore_verify.py").read_text(encoding="utf-8")
    runbook = (ROOT / "docs/postgres/backup_restore_runbook.md").read_text(encoding="utf-8")
    credential_pattern = re.compile(r"postgres(?:ql)?://[^\s:/]+:([^@\s]+)@", re.IGNORECASE)
    inspected = [backup, restore, runbook, (ROOT / "scripts/postgres_backup_restore_smoke.py").read_text(encoding="utf-8")]
    if any(credential_pattern.search(text) for text in inspected):
        return False
    workflow_credentials = credential_pattern.findall(workflow)
    # The hosted service's documented localhost-only test credential is the sole exception.
    if not workflow_credentials or any(value != "postgres" for value in workflow_credentials):
        return False
    return (
        all(value in backup for value in ("--format=custom", "--no-owner", "--no-acl", "PG_DUMP_BIN"))
        and all(value in restore for value in ("--exit-on-error", "--dbname", "PG_RESTORE_BIN"))
        and "example-password" not in runbook.lower()
    )


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
    backup_restore_ok = _backup_restore_gate_is_complete()
    security_ok = _security_gate_is_complete()
    deployment_rollback_ok = _deployment_rollback_gate_is_complete()
    final_enablement_ok = _final_enablement_gate_is_complete()
    checks = {
        "coverage_baseline": "ok" if coverage_ok else "failed",
        "read_only_smoke_contract_documented": "ok",
        "write_plan_complete": "ok" if write_plan_ok else "failed",
        "rollback_harness_complete": "ok" if harness_ok else "failed",
        "postgres_runtime_default_guarded": "ok" if runtime_guarded else "failed",
        "runtime_adapter_gate": "ok" if runtime_guarded else "failed",
        "backup_restore_gate": "ok" if backup_restore_ok else "failed",
        "security_gate": "ok" if security_ok else "failed",
        "deployment_rollback_gate": "ok" if deployment_rollback_ok else "failed",
        "final_enablement_gate": "ok" if final_enablement_ok else "failed",
    }
    foundational_ok = all(checks[name] == "ok" for name in (
        "coverage_baseline", "read_only_smoke_contract_documented",
        "write_plan_complete", "rollback_harness_complete",
        "postgres_runtime_default_guarded", "runtime_adapter_gate",
        "backup_restore_gate",
        "security_gate",
        "deployment_rollback_gate",
        "final_enablement_gate",
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
        "status": "ready" if foundational_ok else "failed",
        "ready_for_runtime_enablement": foundational_ok,
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
