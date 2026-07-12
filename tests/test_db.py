import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import (
    DEFAULT_DB_PATH,
    POSTGRES_NOT_IMPLEMENTED_MESSAGE,
    SQLITE_BUSY_TIMEOUT_MS,
    connect,
    connect_database,
    load_db_config,
)


class DbConfigTest(unittest.TestCase):
    def test_db_config_defaults_to_sqlite(self):
        config = load_db_config({})

        self.assertEqual(config.backend, "sqlite")
        self.assertEqual(config.sqlite_path, DEFAULT_DB_PATH)
        self.assertIsNone(config.database_url)

    def test_db_config_uses_mvp_db_path_for_backward_compat(self):
        config = load_db_config({"MVP_DB_PATH": "/tmp/back-compat.sqlite3"})

        self.assertEqual(config.backend, "sqlite")
        self.assertEqual(config.sqlite_path, Path("/tmp/back-compat.sqlite3"))

    def test_db_config_prefers_sqlite_db_path_over_mvp_db_path(self):
        config = load_db_config({
            "SQLITE_DB_PATH": "/tmp/new.sqlite3",
            "MVP_DB_PATH": "/tmp/old.sqlite3",
        })

        self.assertEqual(config.sqlite_path, Path("/tmp/new.sqlite3"))

    def test_db_config_rejects_unknown_backend(self):
        with self.assertRaisesRegex(ValueError, "Unsupported DB_BACKEND: oracle"):
            load_db_config({"DB_BACKEND": "oracle"})

    def test_postgres_backend_not_implemented_yet(self):
        config = load_db_config({
            "DB_BACKEND": "postgres",
            "DATABASE_URL": "postgresql://user:password@host:5432/teleroute",
        })

        self.assertEqual(config.backend, "postgres")
        self.assertEqual(config.database_url, "postgresql://user:password@host:5432/teleroute")
        with self.assertRaisesRegex(NotImplementedError, POSTGRES_NOT_IMPLEMENTED_MESSAGE):
            connect_database(config)

    def test_sqlite_connect_still_works(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        try:
            config = load_db_config({"DB_BACKEND": "sqlite", "SQLITE_DB_PATH": tmp.name})
            conn = connect_database(config)
            try:
                self.assertIs(conn.row_factory, sqlite3.Row)
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            finally:
                conn.close()
        finally:
            os.unlink(tmp.name)
            for suffix in ("-wal", "-shm"):
                path = tmp.name + suffix
                if os.path.exists(path):
                    os.unlink(path)


class SQLiteConnectionSettingsTest(unittest.TestCase):
    def test_connect_applies_wal_busy_timeout_and_foreign_keys(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        try:
            conn = connect(tmp.name)
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], SQLITE_BUSY_TIMEOUT_MS)
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            finally:
                conn.close()
        finally:
            os.unlink(tmp.name)
            for suffix in ("-wal", "-shm"):
                path = tmp.name + suffix
                if os.path.exists(path):
                    os.unlink(path)
