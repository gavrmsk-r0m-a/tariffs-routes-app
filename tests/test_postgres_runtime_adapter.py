import builtins
import sqlite3
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from app.db import (
    DbConfig,
    connect_database,
    load_db_config,
    postgres_runtime_enabled,
)


class PostgresRuntimeAdapterTests(unittest.TestCase):
    def test_default_sqlite_is_unchanged_and_needs_no_guard(self):
        self.assertEqual(load_db_config({}).backend, "sqlite")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            connection = connect_database(
                DbConfig(backend="sqlite", sqlite_path=path), environ={}
            )
            self.assertIsInstance(connection, sqlite3.Connection)
            connection.close()

    def test_postgres_aliases_are_blocked_without_guard(self):
        for backend in ("postgres", "postgresql"):
            with self.subTest(backend=backend):
                config = DbConfig(
                    backend=backend,
                    sqlite_path=Path(":memory:"),
                    database_url="postgresql://example",
                )
                with self.assertRaisesRegex(NotImplementedError, "POSTGRES_RUNTIME_ENABLED=1"):
                    connect_database(config, environ={})

    def test_guard_accepts_only_one(self):
        config = DbConfig(
            backend="postgres",
            sqlite_path=Path(":memory:"),
            database_url="postgresql://example",
        )
        for value in ("true", "yes", "on"):
            with self.subTest(value=value):
                self.assertFalse(postgres_runtime_enabled({"POSTGRES_RUNTIME_ENABLED": value}))
                with self.assertRaises(NotImplementedError):
                    connect_database(config, environ={"POSTGRES_RUNTIME_ENABLED": value})
        sentinel = object()
        with patch("app.db.connect_postgres", return_value=sentinel) as adapter:
            self.assertIs(
                connect_database(config, environ={"POSTGRES_RUNTIME_ENABLED": "1"}),
                sentinel,
            )
        adapter.assert_called_once_with("postgresql://example")

    def test_database_url_is_required_after_guard(self):
        config = DbConfig(backend="postgres", sqlite_path=Path(":memory:"))
        with self.assertRaisesRegex(ValueError, "DATABASE_URL"):
            connect_database(config, environ={"POSTGRES_RUNTIME_ENABLED": "1"})

    def test_lazy_psycopg_connection_uses_dict_rows(self):
        connection = object()
        dict_row = object()
        psycopg = types.ModuleType("psycopg")
        psycopg.connect = Mock(return_value=connection)
        rows = types.ModuleType("psycopg.rows")
        rows.dict_row = dict_row
        config = DbConfig(
            backend="postgresql",
            sqlite_path=Path(":memory:"),
            database_url="postgresql://example/runtime",
        )
        with patch.dict(sys.modules, {"psycopg": psycopg, "psycopg.rows": rows}):
            with patch("app.db.connect") as sqlite_connect:
                result = connect_database(config, environ={"POSTGRES_RUNTIME_ENABLED": "1"})
        self.assertIs(result, connection)
        psycopg.connect.assert_called_once_with(
            "postgresql://example/runtime", row_factory=dict_row
        )
        sqlite_connect.assert_not_called()

    def test_missing_psycopg_has_installation_guidance(self):
        config = DbConfig(
            backend="postgres",
            sqlite_path=Path(":memory:"),
            database_url="postgresql://example/runtime",
        )
        real_import = builtins.__import__

        def import_without_psycopg(name, *args, **kwargs):
            if name == "psycopg" or name.startswith("psycopg."):
                raise ImportError("forced missing dependency")
            return real_import(name, *args, **kwargs)

        psycopg_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "psycopg" or name.startswith("psycopg.")
        }
        try:
            for name in psycopg_modules:
                sys.modules.pop(name, None)
            with patch("builtins.__import__", side_effect=import_without_psycopg):
                with self.assertRaisesRegex(RuntimeError, r"psycopg\[binary\]"):
                    connect_database(config, environ={"POSTGRES_RUNTIME_ENABLED": "1"})
        finally:
            for name in tuple(sys.modules):
                if name == "psycopg" or name.startswith("psycopg."):
                    sys.modules.pop(name, None)
            sys.modules.update(psycopg_modules)


if __name__ == "__main__":
    unittest.main()
