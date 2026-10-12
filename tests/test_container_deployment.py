import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ContainerDeploymentContractTests(unittest.TestCase):
    def test_dockerfile_runs_production_launcher_as_non_root(self):
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM python:3.12-slim", text)
        self.assertIn("USER teleroute", text)
        self.assertIn('CMD ["python", "scripts/run_production.py"]', text)

    def test_production_compose_contains_app_postgres_and_migration_job(self):
        text = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
        self.assertIn("  postgres:", text)
        self.assertIn("  db_migrate:", text)
        self.assertIn("  app:", text)
        self.assertIn('HOST: "0.0.0.0"', text)
        self.assertIn('"127.0.0.1:${APP_PORT:-8000}:8000"', text)
        self.assertIn("service_completed_successfully", text)
        self.assertNotIn("127.0.0.1:5432:5432", text)

    def test_local_postgres_compose_remains_database_only(self):
        text = (ROOT / "docker-compose.postgres.yml").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1:5432:5432", text)
        self.assertNotIn("\n  app:", text)

    def test_migration_script_does_not_create_bootstrap_users(self):
        text = (ROOT / "scripts/apply_postgres_schema.py").read_text(encoding="utf-8")
        self.assertIn("schema.postgres.sql", text)
        self.assertIn("docs/postgres/migrations", text)
        self.assertNotIn("INSERT INTO users", text)


@unittest.skipUnless(os.environ.get("POSTGRES_TEST_ADMIN_URL"),
                     "POSTGRES_TEST_ADMIN_URL is required")
class ContainerMigrationPostgresTests(unittest.TestCase):
    def test_schema_apply_is_repeatable_on_fresh_database(self):
        from scripts.apply_postgres_schema import apply_schema_and_migrations
        from tests.postgres_test_support import TemporaryPostgresDatabase

        database = TemporaryPostgresDatabase().create_empty()
        try:
            first = apply_schema_and_migrations(database.database_url)
            second = apply_schema_and_migrations(database.database_url)
            self.assertTrue(first)
            self.assertEqual(first, second)
            with database.connect() as conn:
                self.assertIsNotNone(
                    conn.execute("SELECT to_regclass('public.routing_event_routes')").fetchone()[0]
                )
                self.assertIsNotNone(
                    conn.execute("SELECT to_regclass('public.phone_spam_state')").fetchone()[0]
                )
        finally:
            database.drop()


if __name__ == "__main__":
    unittest.main()
