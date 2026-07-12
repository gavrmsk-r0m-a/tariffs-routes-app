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


if __name__ == "__main__":
    unittest.main()
