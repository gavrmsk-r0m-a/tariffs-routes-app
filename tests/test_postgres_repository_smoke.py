import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from scripts import postgres_repository_smoke as smoke


class PostgreSQLRepositorySmokeTest(unittest.TestCase):
    def test_smoke_script_imports_without_psycopg(self):
        script = Path(smoke.__file__)
        spec = importlib.util.spec_from_file_location("lazy_smoke_test_module", script)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"psycopg": None, "psycopg.rows": None}):
            spec.loader.exec_module(module)
        self.assertTrue(callable(module.run_smoke))

    def test_postgres_url_masking(self):
        url = "postgresql://postgres:top-secret@localhost:5432/demo"
        masked = smoke.mask_postgres_url(url)
        self.assertNotIn("top-secret", masked)
        self.assertIn("postgres:***@", masked)
        sanitized = smoke.sanitize_error(f"failed for {url}; password=top-secret", url)
        self.assertNotIn("top-secret", sanitized)

    def test_smoke_plan_contains_read_only_methods(self):
        self.assertTrue(smoke.SMOKE_METHODS)
        forbidden = ("create_", "update_", "ensure_", "delete_", "clear_")
        self.assertFalse([name for name in smoke.SMOKE_METHODS if name.startswith(forbidden)])

    def test_smoke_result_summary_shape(self):
        summary = smoke.empty_summary("postgresql://user:secret@localhost/db")
        self.assertEqual({"status", "postgres_url", "checks_count", "failures"}, summary.keys())
        self.assertNotIn("secret", str(summary))

    def test_smoke_does_not_reference_sqlite_db(self):
        options = {action.dest for action in smoke.build_parser()._actions}
        self.assertNotIn("sqlite_db", options)
        self.assertNotIn("db", options)

    def test_smoke_rejects_missing_postgres_url(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(SystemExit, "2"):
            smoke.main([])


if __name__ == "__main__":
    unittest.main()
