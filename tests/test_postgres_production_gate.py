import json
import subprocess
import sys
import unittest
from pathlib import Path

from app.db import DbConfig, connect_database, load_db_config
from scripts.audit_postgres_production_gate import audit


ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = ROOT / "scripts/audit_postgres_production_gate.py"


class PostgresProductionGateTests(unittest.TestCase):
    def test_json_audit_shape_and_baseline(self):
        result = audit()
        self.assertEqual(result["status"], "ready")
        self.assertIs(result["ready_for_runtime_enablement"], True)
        expected = {
            "repository_public_methods_count": 115,
            "smoke_covered_read_count": 63,
            "deferred_read_only_count": 0,
            "write_or_mutating_count": 51,
            "infrastructure_or_mixed_count": 1,
            "read_surface_coverage_percent": 100.0,
            "repository_smoke_checks_count": 613,
            "rollback_smoke_covered_methods_count": 51,
            "rollback_harness_probe_count": 25,
        }
        for name, value in expected.items():
            self.assertEqual(result["metrics"][name], value)

    def test_write_plan_is_complete(self):
        result = audit()
        self.assertEqual(result["checks"]["write_plan_complete"], "ok")
        self.assertEqual(result["metrics"]["planned_write_methods_count"], 51)
        self.assertEqual(result["metrics"]["expected_write_methods_count"], 51)
        self.assertEqual(result["metrics"]["rollback_smoke_covered_methods_count"], 51)

    def test_all_production_readiness_gates_are_ok(self):
        result = audit()
        self.assertEqual(result["checks"]["postgres_runtime_default_guarded"], "ok")
        self.assertEqual(result["checks"]["runtime_adapter_gate"], "ok")
        self.assertEqual(result["checks"]["backup_restore_gate"], "ok")
        self.assertEqual(result["checks"]["security_gate"], "ok")
        self.assertEqual(result["checks"]["deployment_rollback_gate"], "ok")
        self.assertEqual(result["checks"]["final_enablement_gate"], "ok")
        self.assertNotIn("production_postgres_runtime_not_implemented", result["blockers"])
        self.assertNotIn("backup_restore_scripts_not_verified", result["blockers"])
        self.assertNotIn("basic_security_gate_not_completed", result["blockers"])
        self.assertNotIn("deployment_rollback_procedure_not_documented", result["blockers"])
        self.assertEqual(result["blockers"], [])

    def test_runtime_remains_disabled_for_postgres_aliases(self):
        for backend in ("postgres", "postgresql"):
            with self.subTest(backend=backend):
                with self.assertRaises(NotImplementedError):
                    connect_database(
                        DbConfig(backend=backend, sqlite_path=Path(":memory:")), environ={}
                    )
        self.assertEqual(load_db_config({}).backend, "sqlite")

    def test_command_exit_codes(self):
        non_strict = subprocess.run(
            [sys.executable, str(AUDIT_SCRIPT), "--format", "json"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(non_strict.returncode, 0, non_strict.stderr)
        self.assertEqual(json.loads(non_strict.stdout)["status"], "ready")
        strict = subprocess.run(
            [sys.executable, str(AUDIT_SCRIPT), "--strict"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(strict.returncode, 0, strict.stderr)

    def test_documentation_records_gate_contract(self):
        document = ROOT / "docs/postgres/production_readiness_gate.md"
        self.assertTrue(document.exists())
        text = document.read_text(encoding="utf-8")
        for phrase in ("613", "51/51", "DB_BACKEND=postgres", "must not be enabled",
                       "backup", "security", "rollback"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_runtime_implementation_is_lazy_and_guarded(self):
        source = (ROOT / "app/db.py").read_text(encoding="utf-8")
        self.assertIn('POSTGRES_RUNTIME_GUARD_ENV = "POSTGRES_RUNTIME_ENABLED"', source)
        self.assertIn("raise NotImplementedError(POSTGRES_RUNTIME_DISABLED_MESSAGE)", source)
        self.assertIn("def connect_postgres", source)
        self.assertIn("        import psycopg", source)
        self.assertIn("return psycopg.connect(database_url, row_factory=dict_row)", source)


if __name__ == "__main__":
    unittest.main()
