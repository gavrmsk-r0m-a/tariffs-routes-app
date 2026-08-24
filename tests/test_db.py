import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import (
    DEFAULT_DB_PATH,
    POSTGRES_RUNTIME_DISABLED_MESSAGE,
    SQLITE_BUSY_TIMEOUT_MS,
    connect,
    connect_database,
    init_db,
    load_db_config,
    run_lightweight_migrations,
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

    def test_postgres_backend_requires_runtime_enablement(self):
        config = load_db_config({
            "DB_BACKEND": "postgres",
            "DATABASE_URL": "postgresql://user:password@host:5432/teleroute",
        })

        self.assertEqual(config.backend, "postgres")
        self.assertEqual(config.database_url, "postgresql://user:password@host:5432/teleroute")
        with self.assertRaisesRegex(NotImplementedError, POSTGRES_RUNTIME_DISABLED_MESSAGE):
            connect_database(config)

    def test_fresh_sqlite_calling_company_country_is_nullable(self):
        conn = connect(":memory:")
        try:
            init_db(conn)
            columns = {row[1]: row for row in conn.execute("PRAGMA table_info(calling_companies)")}
            self.assertEqual(columns["country_id"][3], 0)
            routing_columns = {row[1]: row for row in conn.execute("PRAGMA table_info(company_routing_settings)")}
            self.assertEqual(routing_columns["country_id"][3], 0)
        finally:
            conn.close()

    def test_existing_sqlite_calling_company_nullable_migration_is_idempotent(self):
        conn = connect(":memory:")
        try:
            schema = (Path(__file__).parents[1] / "app/schema.sql").read_text(encoding="utf-8")
            nullable = "    country_id INTEGER REFERENCES countries(id) ON DELETE RESTRICT,"
            legacy = "    country_id INTEGER NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,"
            calling_start = schema.index("CREATE TABLE IF NOT EXISTS calling_companies")
            country_offset = schema.index(nullable, calling_start)
            schema = schema[:country_offset] + schema[country_offset:].replace(nullable, legacy, 1)
            conn.executescript(schema)
            conn.executescript("""
                INSERT INTO users(id, username, display_name, role_key, is_active) VALUES (1, 'legacy', 'Legacy', 'admin', 1);
                INSERT INTO servers(id, name) VALUES (2, 'Legacy server');
                INSERT INTO countries(id, name) VALUES (3, 'Legacy GEO');
                INSERT INTO calling_companies(id, server_id, country_id, company_name, company_id_external, created_by)
                VALUES (4, 2, 3, 'Legacy', 'legacy-4', 1);
            """)
            run_lightweight_migrations(conn)
            run_lightweight_migrations(conn)
            row = conn.execute("SELECT * FROM calling_companies WHERE id = 4").fetchone()
            self.assertEqual((row["country_id"], row["company_name"], row["company_id_external"]), (3, "Legacy", "legacy-4"))
            columns = {item[1]: item for item in conn.execute("PRAGMA table_info(calling_companies)")}
            self.assertEqual(columns["country_id"][3], 0)
            self.assertEqual(len(columns), 15)
            index_names = {item[1] for item in conn.execute("PRAGMA index_list(calling_companies)")}
            self.assertIn("ux_calling_companies_multi_geo_identity", index_names)
            foreign_tables = {item[2] for item in conn.execute("PRAGMA foreign_key_list(calling_companies)")}
            self.assertEqual(foreign_tables, {"users", "countries", "servers"})
            conn.execute("UPDATE calling_companies SET country_id = NULL WHERE id = 4")
            conn.commit()
        finally:
            conn.close()

    def test_existing_sqlite_routing_setting_nullable_migration_preserves_state_and_indexes(self):
        conn = connect(":memory:")
        try:
            schema = (Path(__file__).parents[1] / "app/schema.sql").read_text(encoding="utf-8")
            marker = "CREATE TABLE IF NOT EXISTS company_routing_settings"
            start = schema.index(marker)
            nullable = "    country_id INTEGER REFERENCES countries(id) ON DELETE RESTRICT,"
            offset = schema.index(nullable, start)
            schema = schema[:offset] + schema[offset:].replace(nullable, nullable.replace(" INTEGER ", " INTEGER NOT NULL "), 1)
            conn.executescript(schema)
            conn.executescript("""
                INSERT INTO users(id, username, display_name, role_key, is_active) VALUES (1, 'legacy', 'Legacy', 'admin', 1);
                INSERT INTO servers(id, name) VALUES (2, 'Legacy server');
                INSERT INTO countries(id, name) VALUES (3, 'Legacy GEO');
                INSERT INTO currencies(id, code, name) VALUES (4, 'EUR', 'Euro');
                INSERT INTO providers(id, name, normalized_name, default_currency_id) VALUES (5, 'Legacy provider', 'legacy provider', 4);
                INSERT INTO routes(id, country_id, provider_id, name, cli_source_type, cli_source_label, created_by)
                    VALUES (6, 3, 5, 'Legacy route', 'pool', 'Legacy pool', 1);
                INSERT INTO calling_companies(id, server_id, country_id, company_name, company_id_external, created_by)
                    VALUES (7, 2, 3, 'Legacy campaign', 'legacy-7', 1);
                INSERT INTO company_routing_settings(id, calling_company_id, country_id, server_id, route_id, routing_mode, has_autorotation, comment, valid_from, created_by)
                    VALUES (8, 7, 3, 2, 6, 'mixed', 1, 'preserve', '2026-01-02', 1);
            """)
            run_lightweight_migrations(conn)
            run_lightweight_migrations(conn)
            row = conn.execute("SELECT * FROM company_routing_settings WHERE id = 8").fetchone()
            self.assertEqual((row["route_id"], row["routing_mode"], row["has_autorotation"], row["valid_from"]), (6, "mixed", 1, "2026-01-02"))
            self.assertEqual({r[1] for r in conn.execute("PRAGMA index_list(company_routing_settings)")}, {
                "idx_company_routing_settings_company_id", "idx_company_routing_settings_country_id",
                "idx_company_routing_settings_server_id", "idx_company_routing_settings_route_id",
                "ux_company_routing_settings_one_active",
            })
            self.assertEqual({r[2] for r in conn.execute("PRAGMA foreign_key_list(company_routing_settings)")}, {"calling_companies", "countries", "servers", "routes", "users"})
            self.assertEqual({r[1]: r for r in conn.execute("PRAGMA table_info(company_routing_settings)")}["country_id"][3], 0)
            conn.execute("UPDATE company_routing_settings SET country_id = NULL WHERE id = 8")
        finally:
            conn.close()

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
