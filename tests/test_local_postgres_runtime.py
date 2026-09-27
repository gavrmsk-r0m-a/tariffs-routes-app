import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import run_local_postgres_app, setup_local_postgres
from tests.postgres_test_support import TemporaryPostgresDatabase


LOCAL_URL = "postgresql://postgres:database-secret@localhost:5432/teleroute_local"


class LocalPostgresRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("POSTGRES_TEST_ADMIN_URL"), "requires PostgreSQL test admin URL")
    def test_setup_is_repeatable_with_idempotent_route_association_backfill(self):
        database = TemporaryPostgresDatabase().create_empty()
        self.addCleanup(database.drop)

        setup_local_postgres.setup_database(database.database_url, "local-dev", "first-password")
        with database.connect() as conn:
            table_name = conn.execute("SELECT to_regclass('public.routing_event_routes')").fetchone()[0]
            self.assertEqual(table_name, "routing_event_routes")
            user_id = conn.execute("SELECT id FROM users WHERE username = 'local-dev'").fetchone()[0]
            country_id = conn.execute(
                "INSERT INTO countries(name, code) VALUES ('Bootstrap test', 'BT') RETURNING id"
            ).fetchone()[0]
            provider_id = conn.execute(
                "INSERT INTO providers(name, normalized_name) VALUES ('Bootstrap provider', 'bootstrap provider') RETURNING id"
            ).fetchone()[0]
            route_id = conn.execute(
                """
                INSERT INTO routes(country_id, provider_id, name, cli_source_type, cli_source_label, created_by)
                VALUES (%s, %s, 'Bootstrap route', 'other', 'Other', %s)
                RETURNING id
                """,
                (country_id, provider_id, user_id),
            ).fetchone()[0]
            event_ids = []
            for comment in ("existing association", "needs backfill"):
                event_ids.append(
                    conn.execute(
                        """
                        INSERT INTO routing_events(
                            event_at, apply_scope, reason, country_id, provider_id,
                            affected_route_id, comment, created_by, updated_by
                        )
                        VALUES (CURRENT_TIMESTAMP, 'none', 'Bootstrap', %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (country_id, provider_id, route_id, comment, user_id, user_id),
                    ).fetchone()[0]
                )
            conn.execute(
                """
                INSERT INTO routing_event_routes(
                    routing_event_id, route_id, route_name, provider_name, position
                ) VALUES (%s, %s, 'Preserved route snapshot', 'Preserved provider snapshot', 0)
                """,
                (event_ids[0], route_id),
            )
            conn.commit()

        setup_local_postgres.setup_database(database.database_url, "local-dev", "second-password")
        with database.connect() as conn:
            rows = conn.execute(
                """
                SELECT routing_event_id, route_name, provider_name, position
                FROM routing_event_routes
                WHERE routing_event_id = ANY(%s)
                ORDER BY routing_event_id
                """,
                (event_ids,),
            ).fetchall()

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            (rows[0]["route_name"], rows[0]["provider_name"], rows[0]["position"]),
            ("Preserved route snapshot", "Preserved provider snapshot", 0),
        )
        self.assertEqual(
            (rows[1]["route_name"], rows[1]["provider_name"], rows[1]["position"]),
            ("Bootstrap route", "Bootstrap provider", 0),
        )

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
