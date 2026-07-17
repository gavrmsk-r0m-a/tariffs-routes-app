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

    def test_workflow_paths_include_repository_and_db_adapter(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/postgres-migration-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("- app/repository.py", workflow)
        self.assertIn("- app/db_adapter.py", workflow)

    def test_expected_positive_exists_check_fails_when_repository_returns_false(self):
        repo = mock.Mock()
        repo.route_exists_by_country_name_and_name.return_value = False
        repo.phone_number_exists_by_normalized_number.return_value = False
        repo.calling_company_exists_by_server_country_external_id.return_value = False
        repo.current_tariff_exists_by_country_provider_prefix.return_value = False
        failures = []

        def check(name, operation):
            try:
                operation()
            except AssertionError:
                failures.append(name)

        smoke.run_exists_checks(repo, check)

        self.assertEqual(
            {
                "route_exists_by_country_name_and_name_existing",
                "phone_number_exists_by_normalized_number_existing",
                "calling_company_exists_by_server_country_external_id_existing",
                "current_tariff_exists_by_country_provider_prefix_existing",
            },
            set(failures),
        )

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
