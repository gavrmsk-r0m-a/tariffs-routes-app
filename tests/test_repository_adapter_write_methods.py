import sqlite3
import unittest
from unittest.mock import patch

from app.db import init_db
from app.repository import Repository


class RepositoryAdapterWriteMethodsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        self.repo = Repository(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_selected_create_method_returns_id_on_sqlite(self):
        country_id = self.repo.create_country("Бельгия", "BE")
        provider_id = self.repo.create_provider("AdapterWriteTel")
        server_id = self.repo.create_server("adapter-write-server")

        self.assertIsInstance(country_id, int)
        self.assertIsInstance(provider_id, int)
        self.assertIsInstance(server_id, int)
        self.assertGreater(country_id, 0)
        self.assertGreater(provider_id, 0)
        self.assertGreater(server_id, 0)

    def test_selected_create_method_persists_row(self):
        provider_id = self.repo.create_provider("PersistTel", "voip", comment="created by adapter test")

        row = self.conn.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "PersistTel")
        self.assertEqual(row["normalized_name"], "persisttel")
        self.assertEqual(row["provider_type"], "voip")
        self.assertEqual(row["is_active"], 1)
        self.assertEqual(row["comment"], "created by adapter test")

    def test_insert_returning_helper_used_without_changing_sqlite_behavior(self):
        with patch("app.repository.prepare_insert_returning_id", wraps=__import__("app.db_adapter", fromlist=["prepare_insert_returning_id"]).prepare_insert_returning_id) as prepare, \
             patch("app.repository.extract_inserted_id", wraps=__import__("app.db_adapter", fromlist=["extract_inserted_id"]).extract_inserted_id) as extract:
            country_id = self.repo.create_country("Австрия", "AT")

        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(prepare.call_args.args[1], "sqlite")
        self.assertEqual(extract.call_args.args[1], "sqlite")
        self.assertEqual(self.repo.get_country(country_id)["code"], "AT")

    def test_boolean_write_uses_sqlite_integer(self):
        active_id = self.repo.create_change_reason("Active reason", is_active=True)
        inactive_id = self.repo.create_change_reason("Inactive reason", is_active=False)

        active = self.conn.execute("SELECT is_active FROM change_reasons WHERE id = ?", (active_id,)).fetchone()
        inactive = self.conn.execute("SELECT is_active FROM change_reasons WHERE id = ?", (inactive_id,)).fetchone()

        self.assertEqual(active["is_active"], 1)
        self.assertEqual(inactive["is_active"], 0)

    def test_existing_dictionary_write_still_works(self):
        country_id = self.repo.create_country("Мексика", "MX")
        provider_id = self.repo.create_provider("DictionaryTel")
        server_id = self.repo.create_server("dictionary-server", "plain write path")
        reason_id = self.repo.create_change_reason("Dictionary reason", comment="plain write path")

        self.assertEqual(self.repo.get_country(country_id)["name"], "Мексика")
        self.assertEqual(self.conn.execute("SELECT name FROM providers WHERE id = ?", (provider_id,)).fetchone()["name"], "DictionaryTel")
        self.assertEqual(self.conn.execute("SELECT comment FROM servers WHERE id = ?", (server_id,)).fetchone()["comment"], "plain write path")
        self.assertEqual(self.conn.execute("SELECT description FROM change_reasons WHERE id = ?", (reason_id,)).fetchone()["description"], "plain write path")

    def test_hlr_limit_override_keeps_sqlite_commit_default_and_allows_caller_transaction(self):
        self.repo.set_hlr_limit_override("2500")
        self.assertEqual(self.repo.get_hlr_limit_override(), "2500")
        self.repo.set_hlr_limit_override("5151", commit=False)
        self.assertEqual(self.repo.get_hlr_limit_override(), "5151")
        self.conn.rollback()
        self.assertEqual(self.repo.get_hlr_limit_override(), "2500")

    def test_hlr_limit_override_records_postgres_placeholder_and_upsert(self):
        class RecordingConnection:
            def __init__(self): self.calls = []; self.commits = 0
            def execute(self, sql, params=()): self.calls.append((sql, params))
            def commit(self): self.commits += 1
            def rollback(self): raise AssertionError("unexpected rollback")
        connection = RecordingConnection()
        Repository(connection, backend="postgres").set_hlr_limit_override("5151", commit=False)
        sql, params = connection.calls[0]
        self.assertIn("VALUES (%s, %s, CURRENT_TIMESTAMP, %s)", sql)
        self.assertIn("ON CONFLICT(key) DO UPDATE", sql)
        self.assertEqual(params, ("hlr_daily_limit_override", "5151", None))
        self.assertEqual(connection.commits, 0)

    def test_stage52_app_settings_use_postgres_placeholders_and_commit_contract(self):
        class RecordingConnection:
            def __init__(self): self.calls=[]; self.commits=0; self.rollbacks=0
            def execute(self, sql, params=()): self.calls.append((sql, params))
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        connection = RecordingConnection(); repo = Repository(connection, backend="postgres")
        repo.set_app_setting_value("key", "value", 7)
        self.assertIn("VALUES (%s, %s, CURRENT_TIMESTAMP, %s)", connection.calls[0][0])
        self.assertIn("ON CONFLICT(key) DO UPDATE", connection.calls[0][0])
        self.assertEqual(connection.calls[0][1], ("key", "value", 7)); self.assertEqual(connection.commits, 1)
        repo.delete_app_setting_value("key", commit=False)
        self.assertEqual(connection.calls[-1], ("DELETE FROM app_settings WHERE key = %s", ("key",)))
        self.assertEqual(connection.commits, 1)

    def test_stage52_hlr_usage_uses_postgres_placeholders_and_returns_usage(self):
        class Cursor:
            def fetchone(self): return None
        class RecordingConnection:
            def __init__(self): self.calls=[]; self.commits=0
            def execute(self, sql, params=()): self.calls.append((sql, params)); return Cursor()
            def commit(self): self.commits += 1
            def rollback(self): raise AssertionError("unexpected rollback")
        connection = RecordingConnection(); repo = Repository(connection, backend="postgres")
        with patch.object(repo, "get_hlr_daily_usage", return_value={"usage_date": "2099-12-31"}) as getter:
            result = repo.upsert_hlr_daily_usage("2099-12-31", 3, "0.75", "2099-12-31 10:00", commit=False)
        self.assertIn("WHERE usage_date = %s", connection.calls[0][0])
        self.assertIn("VALUES (%s, %s, %s, %s, %s, %s)", connection.calls[1][0])
        self.assertIn("ON CONFLICT(usage_date) DO UPDATE", connection.calls[1][0])
        self.assertEqual(connection.calls[1][1], ("2099-12-31", 3, 0.75, 3, "0.75", "2099-12-31 10:00"))
        self.assertEqual(connection.commits, 0); getter.assert_called_once_with("2099-12-31")
        self.assertEqual(result, {"usage_date": "2099-12-31"})

    def test_stage52_sqlite_app_settings_and_hlr_usage_keep_caller_owned_commit(self):
        self.repo.set_app_setting_value("stage52", "value", commit=False)
        self.assertEqual(self.repo.get_app_setting_value("stage52"), "value")
        self.conn.rollback()
        self.assertIsNone(self.repo.get_app_setting_value("stage52"))
        usage = self.repo.upsert_hlr_daily_usage("2099-12-31", 3, "0.75", "2099-12-31 10:00", commit=False)
        self.assertEqual(usage["checked_today"], 3)
        self.conn.rollback()
        self.assertEqual(self.repo.get_hlr_daily_usage("2099-12-31")["checked_today"], 0)

    def test_update_calling_company_import_fields_updates_row_and_booleans(self):
        user_id = self.repo.create_user("company-import-admin", "Company Import Admin")
        country_id = self.repo.create_country("Италия", "IT")
        server_id = self.repo.create_server("company-import-server")
        company_id = self.repo.create_calling_company(
            server_id=server_id,
            country_id=country_id,
            company_name="Before",
            company_id_external="import-1",
            has_autorotation=False,
            created_by=user_id,
            is_active=True,
        )

        rowcount = self.repo.update_calling_company_import_fields(
            server_id=server_id,
            country_id=country_id,
            company_id_external="import-1",
            company_name="After",
            has_autorotation=True,
            comment="Imported update",
            is_active=False,
            updated_by=user_id,
        )

        row = self.conn.execute("SELECT * FROM calling_companies WHERE id = ?", (company_id,)).fetchone()
        self.assertEqual(rowcount, 1)
        self.assertEqual(row["company_name"], "After")
        self.assertEqual(row["has_autorotation"], 1)
        self.assertEqual(row["comment"], "Imported update")
        self.assertEqual(row["is_active"], 0)
        self.assertEqual(row["updated_by"], user_id)

    def test_update_calling_company_import_fields_returns_zero_for_missing_row(self):
        rowcount = self.repo.update_calling_company_import_fields(
            server_id=999,
            country_id=999,
            company_id_external="missing",
            company_name="Missing",
            has_autorotation=False,
            comment=None,
            is_active=True,
            updated_by=999,
        )

        self.assertEqual(rowcount, 0)


if __name__ == "__main__":
    unittest.main()
