import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import run_local_postgres_app, setup_local_postgres


LOCAL_URL = "postgresql://postgres:database-secret@localhost:5432/teleroute_local"


class LocalPostgresRuntimeTests(unittest.TestCase):
    def test_setup_refuses_non_local_database_url(self):
        for url in (
            "postgresql://user:pass@db.production.example/teleroute",
            "postgresql://user:pass@10.0.0.5/teleroute",
            "sqlite:///tmp/teleroute.sqlite3",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                setup_local_postgres.validate_local_database_url(url)

    def test_setup_failure_does_not_print_raw_database_password(self):
        output = io.StringIO()
        with (
            patch.object(setup_local_postgres, "setup_database", side_effect=RuntimeError(LOCAL_URL)),
            contextlib.redirect_stderr(output),
        ):
            result = setup_local_postgres.main(["--database-url", LOCAL_URL])
        self.assertEqual(result, 1)
        self.assertNotIn("database-secret", output.getvalue())
        self.assertNotIn(LOCAL_URL, output.getvalue())

    def test_local_runner_sets_environment_before_importing_server(self):
        fake_module = Mock(app=object())

        def observe_import(name):
            self.assertEqual(name, "app.server")
            self.assertEqual(os.environ["DB_BACKEND"], "postgres")
            self.assertEqual(os.environ["POSTGRES_RUNTIME_ENABLED"], "1")
            self.assertEqual(os.environ["DATABASE_URL"], LOCAL_URL)
            self.assertEqual(os.environ["MVP_AUTH_SECRET"], "x" * 32)
            return fake_module

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(run_local_postgres_app.importlib, "import_module", side_effect=observe_import),
        ):
            application = run_local_postgres_app.load_application(LOCAL_URL, "x" * 32)
        self.assertIs(application, fake_module.app)

    def test_local_env_example_contains_current_local_settings(self):
        text = Path(".env.postgres.local.example").read_text(encoding="utf-8")
        self.assertIn("DB_BACKEND=postgres", text)
        self.assertIn("DATABASE_URL=postgresql://postgres:postgres@localhost:5432/teleroute_local", text)
        self.assertNotIn("POSTGRES_RUNTIME_ENABLED", text)
        self.assertIn("local-only-auth-secret", text)
        self.assertNotIn("production.example", text.lower())
        self.assertNotIn("prod_", text.lower())


if __name__ == "__main__":
    unittest.main()
