import importlib.util
import os
import sys
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from scripts import postgres_repository_smoke as smoke
from scripts.create_migration_demo_sqlite import create_demo_sqlite
from app.repository import Repository


class RecordingRepository:
    def __init__(self, repository):
        self.repository = repository
        self.called = []

    def __getattr__(self, name):
        value = getattr(self.repository, name)
        if not callable(value):
            return value

        def recorded(*args, **kwargs):
            self.called.append(name)
            return value(*args, **kwargs)

        return recorded


class PostgreSQLRepositorySmokeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        path = create_demo_sqlite(Path(self.temp_dir.name) / "demo.db")
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def run_demo(self, repository=None):
        return smoke.run_repository_checks(repository or Repository(self.conn), "postgresql://user:secret@localhost/demo")

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
        forbidden = ("create_", "update_", "ensure_", "set_", "delete_", "clear_", "deactivate_", "record_", "log_", "upsert_")
        self.assertFalse([name for name in smoke.SMOKE_METHODS if name.startswith(forbidden)])

    def test_stage_34_methods_are_in_smoke_plan(self):
        self.assertEqual(7, len(smoke.STAGE_34_METHODS))
        self.assertTrue(set(smoke.STAGE_34_METHODS) <= set(smoke.SMOKE_METHODS))

    def test_stage_35_methods_are_in_smoke_plan(self):
        self.assertEqual(8, len(smoke.STAGE_35_METHODS))
        self.assertTrue(set(smoke.STAGE_35_METHODS) <= set(smoke.SMOKE_METHODS))

    def test_stage_36_methods_are_in_smoke_plan(self):
        self.assertEqual(("list_users", "get_user", "get_user_by_username", "authenticate_user"), smoke.STAGE_36_METHODS)
        self.assertTrue(set(smoke.STAGE_36_METHODS) <= set(smoke.SMOKE_METHODS))
        self.assertNotIn("_user_columns", smoke.SMOKE_METHODS)

    def test_every_declared_method_is_actually_called_and_no_write_is_called(self):
        repository = RecordingRepository(Repository(self.conn))
        summary = self.run_demo(repository)

        self.assertEqual("ok", summary["status"])
        self.assertFalse(set(smoke.SMOKE_METHODS) - set(repository.called))
        write_prefixes = ("create_", "update_", "ensure_", "delete_", "clear_", "set_", "upsert_", "add_", "remove_", "recalculate_", "log_")
        self.assertFalse([name for name in repository.called if name.startswith(write_prefixes)])

    def test_stage_34_semantics_and_check_count(self):
        summary = self.run_demo()

        self.assertEqual("ok", summary["status"])
        self.assertEqual(131, summary["checks_count"])
        self.assertGreater(summary["checks_count"], 61)
        self.assertNotIn("secret", str(summary))

    def test_wrong_existing_demo_value_causes_failure(self):
        repository = RecordingRepository(Repository(self.conn))
        original = repository.repository.get_app_setting_value
        repository.repository.get_app_setting_value = lambda key: "wrong" if key == "demo_setting" else original(key)

        summary = self.run_demo(repository)

        self.assertEqual("failed", summary["status"])
        self.assertIn("get_app_setting_value", {failure["check"] for failure in summary["failures"]})

    def test_wrong_negative_result_causes_failure(self):
        repository = RecordingRepository(Repository(self.conn))
        original = repository.repository.get_calling_company
        repository.repository.get_calling_company = lambda company_id: {"id": -1} if company_id == -1 else original(company_id)

        summary = self.run_demo(repository)

        self.assertEqual("failed", summary["status"])
        self.assertIn("get_calling_company_missing", {failure["check"] for failure in summary["failures"]})
        self.assertEqual(131, summary["checks_count"])

    def test_stage_35_assertion_failure_does_not_stop_later_checks(self):
        repository = RecordingRepository(Repository(self.conn))
        original = repository.repository.get_user_section_permission

        def wrong_permission(user_id, section_key):
            row = original(user_id, section_key)
            return {**dict(row), "can_write": 0} if row is not None and section_key == "routes" else row

        repository.repository.get_user_section_permission = wrong_permission
        summary = self.run_demo(repository)

        self.assertEqual("failed", summary["status"])
        self.assertIn("get_user_section_permission_values", {failure["check"] for failure in summary["failures"]})
        self.assertIn("get_tariff", repository.called)
        self.assertEqual(131, summary["checks_count"])

    def test_stage_36_assertion_failure_does_not_stop_later_checks(self):
        repository = RecordingRepository(Repository(self.conn))
        original = repository.repository.list_users

        def wrong_users(active_only=False):
            rows = original(active_only)
            return [{**dict(row), "display_name": "Wrong"} if row["username"] == "admin" else row for row in rows]

        repository.repository.list_users = wrong_users
        summary = self.run_demo(repository)
        self.assertEqual("failed", summary["status"])
        self.assertIn("list_users_admin_display_name", {failure["check"] for failure in summary["failures"]})
        self.assertIn("authenticate_user", repository.called)
        self.assertEqual(131, summary["checks_count"])

    def test_database_false_is_strict(self):
        self.assertTrue(smoke._is_database_false(False))
        self.assertTrue(smoke._is_database_false(0))
        for value in (None, "", "false", [], {}):
            self.assertFalse(smoke._is_database_false(value))

    def test_postgres_numeric_scales_pass_semantic_checks(self):
        repository = RecordingRepository(Repository(self.conn))
        original_usage = repository.repository.get_hlr_daily_usage
        original_latest = repository.repository.latest_currency_rate

        def usage_with_postgres_scale(usage_date):
            result = original_usage(usage_date)
            if usage_date == "2026-07-12":
                result["credits_spent_today"] = Decimal("0.50000000")
            return result

        def rate_with_postgres_scale(currency_id):
            result = original_latest(currency_id)
            if result is not None:
                result = dict(result)
                result["rate_to_eur"] = Decimal("1.00000000")
            return result

        repository.repository.get_hlr_daily_usage = usage_with_postgres_scale
        repository.repository.latest_currency_rate = rate_with_postgres_scale

        self.assertEqual("ok", self.run_demo(repository)["status"])

    def test_wrong_eur_rate_causes_failure(self):
        repository = RecordingRepository(Repository(self.conn))
        original = repository.repository.latest_currency_rate

        def wrong_rate(currency_id):
            result = original(currency_id)
            if result is not None:
                result = dict(result)
                result["rate_to_eur"] = Decimal("1.25")
            return result

        repository.repository.latest_currency_rate = wrong_rate
        summary = self.run_demo(repository)

        self.assertEqual("failed", summary["status"])
        self.assertIn("latest_currency_rate_values", {failure["check"] for failure in summary["failures"]})

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
