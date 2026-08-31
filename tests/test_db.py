from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from app.db import DbConfig, connect_database, load_db_config

class PostgreSQLOnlyConfigurationTest(unittest.TestCase):
    def test_postgres_configuration(self):
        config=load_db_config({"DB_BACKEND":"postgres","DATABASE_URL":"postgresql://db/app"})
        self.assertEqual(config, DbConfig("postgres", "postgresql://db/app"))
    def test_postgresql_alias_is_normalized(self):
        self.assertEqual(load_db_config({"DB_BACKEND":"postgresql","DATABASE_URL":"postgresql://db/app"}).backend,"postgres")
    def test_missing_and_blank_backend_fail(self):
        for env in ({},{"DB_BACKEND":"  ","DATABASE_URL":"postgresql://db/app"}):
            with self.subTest(env=env), self.assertRaisesRegex(ValueError,"DB_BACKEND is required"):
                load_db_config(env)
    def test_unsupported_backends_fail(self):
        for backend in ("sqlite","unknown"):
            with self.subTest(backend=backend), self.assertRaisesRegex(ValueError,"Unsupported DB_BACKEND"):
                load_db_config({"DB_BACKEND":backend,"DATABASE_URL":"postgresql://db/app"})
    def test_missing_and_blank_url_fail(self):
        for env in ({"DB_BACKEND":"postgres"},{"DB_BACKEND":"postgres","DATABASE_URL":" "}):
            with self.subTest(env=env), self.assertRaisesRegex(ValueError,"DATABASE_URL is required"):
                load_db_config(env)
    def test_connection_failure_propagates_without_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/"mvp.sqlite3"
            config=DbConfig("postgres","postgresql://unavailable/app")
            with patch("app.db.connect_postgres",side_effect=OSError("connection refused")):
                with self.assertRaisesRegex(OSError,"connection refused"):
                    connect_database(config)
            self.assertFalse(marker.exists())
    def test_postgres_connection_selected(self):
        config=DbConfig("postgres","postgresql://db/app")
        sentinel=object()
        with patch("app.db.connect_postgres",return_value=sentinel) as connect:
            self.assertIs(connect_database(config),sentinel)
        connect.assert_called_once_with("postgresql://db/app")
    def test_runtime_package_has_no_sqlite_dependencies(self):
        root=Path(__file__).parents[1]
        sources="\n".join(path.read_text(encoding="utf-8") for path in (root/"app").glob("*.py"))
        for token in ("sqlite3","mvp.sqlite3","schema.sql"):
            self.assertNotIn(token,sources)
        self.assertFalse((root/"app/schema.sql").exists())
