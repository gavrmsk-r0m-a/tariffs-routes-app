import os
import unittest
from unittest.mock import patch

from scripts import prepare_release_database, run_production


class ProductionLauncherTests(unittest.TestCase):
    GOOD = {
        "DB_BACKEND": "postgres",
        "DATABASE_URL": "postgresql://example.invalid/database",
        "MVP_PRODUCTION_SECURITY": "1",
        "MVP_AUTH_SECRET": "x" * 32,
    }

    def test_defaults_to_loopback(self):
        self.assertEqual(run_production.validate_production_environment(self.GOOD),
                         ("127.0.0.1", 8000))

    def test_rejects_missing_production_configuration(self):
        for missing in ("DB_BACKEND", "DATABASE_URL", "MVP_PRODUCTION_SECURITY",
                        "MVP_AUTH_SECRET"):
            environment = dict(self.GOOD)
            environment.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(RuntimeError):
                run_production.validate_production_environment(environment)

    def test_rejects_weak_secret(self):
        environment = dict(self.GOOD, MVP_AUTH_SECRET="too-short")
        with self.assertRaisesRegex(RuntimeError, "at least 32"):
            run_production.validate_production_environment(environment)

    def test_confirmation_is_required(self):
        with patch.dict(os.environ, self.GOOD, clear=True), \
                self.assertRaises(SystemExit) as raised:
            prepare_release_database.main(["--apply"])
        self.assertEqual(raised.exception.code, 2)


@unittest.skipUnless(os.environ.get("POSTGRES_TEST_ADMIN_URL"),
                     "POSTGRES_TEST_ADMIN_URL is required")
class ReleaseCleanupPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.postgres_test_support import TemporaryPostgresDatabase
        cls.database = TemporaryPostgresDatabase().create()

    @classmethod
    def tearDownClass(cls):
        cls.database.drop()

    def setUp(self):
        self.database.reset()

    def _snapshot(self, conn):
        tables = prepare_release_database.CLEANUP_TABLES + prepare_release_database.PRESERVED_TABLES
        return prepare_release_database.collect_counts(conn, tables)

    def test_dry_run_does_not_change_data(self):
        with self.database.connect() as conn:
            before = self._snapshot(conn)
            report = prepare_release_database.prepare_release_database(conn)
            after = self._snapshot(conn)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(before, after)

    def test_cleanup_resolves_fks_and_preserves_required_data(self):
        with self.database.connect() as conn:
            preserved_before = prepare_release_database.preserved_fingerprints(conn)
            report = prepare_release_database.prepare_release_database(conn, apply=True)
            preserved_after = prepare_release_database.preserved_fingerprints(conn)
        self.assertTrue(any(report["before"].values()))
        self.assertFalse(any(report["after"].values()))
        self.assertEqual(preserved_before, preserved_after)
        for table in ("users", "user_permissions", "change_reasons", "countries",
                      "providers", "api_tokens", "app_settings"):
            self.assertIn(table, report["preserved"])

    def test_injected_failure_rolls_back_everything(self):
        with self.database.connect() as conn:
            before = self._snapshot(conn)
            with self.assertRaisesRegex(RuntimeError, "injected"):
                prepare_release_database.prepare_release_database(
                    conn, apply=True,
                    inject_failure=lambda: (_ for _ in ()).throw(RuntimeError("injected")),
                )
            after = self._snapshot(conn)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
