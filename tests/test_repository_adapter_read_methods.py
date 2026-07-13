import sqlite3
import unittest

from app.db import init_db
from app.repository import Repository


class RepositoryAdapterReadMethodsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        self.repo = Repository(self.conn)
        self.country_id = self.repo.create_country("Австрия", "AT")
        self.currency_id = self.repo.create_currency("EUR", "Euro", "€")
        self.provider_id = self.repo.create_provider("AdapterTel", "voip", self.currency_id)
        self.prefix_id = self.repo.create_prefix(self.provider_id, "43", "Austria")
        self.server_id = self.repo.create_server("adapter-server")
        self.conn.execute("INSERT INTO phone_number_types(name, is_active) VALUES (?, 1)", ("Mobile",))
        self.conn.commit()
        self.change_reason_id = self.repo.create_change_reason("Adapter reason")

    def tearDown(self):
        self.conn.close()

    def test_repository_default_backend_is_sqlite(self):
        repo = Repository(self.conn)

        self.assertEqual(repo.backend, "sqlite")
        self.assertEqual(repo.list_countries()[0]["name"], "Австрия")

    def test_repository_rejects_invalid_backend(self):
        with self.assertRaisesRegex(ValueError, "Unsupported database backend"):
            Repository(self.conn, backend="bad")

    def test_selected_read_methods_return_same_shape(self):
        method_names = [
            "list_countries",
            "list_currencies",
            "list_providers",
            "list_providers_with_currency",
            "list_projects",
            "list_servers",
            "list_phone_number_types",
            "list_phone_assignment_types",
            "list_provider_prefixes",
            "list_provider_prefixes_with_provider",
            "list_change_reasons",
            "list_active_change_reasons",
        ]

        for method_name in method_names:
            with self.subTest(method=method_name):
                rows = getattr(self.repo, method_name)()
                self.assertIsInstance(rows, list)
                self.assertTrue(rows)
                self.assertIsInstance(rows[0], dict)
                self.assertIn("id", rows[0])

    def test_selected_dictionary_reads_work_on_sqlite(self):
        self.assertEqual(self.repo.list_countries()[0]["name"], "Австрия")
        self.assertEqual(self.repo.list_currencies()[0]["code"], "EUR")
        self.assertEqual(self.repo.list_providers()[0]["name"], "AdapterTel")
        provider_with_currency = self.repo.list_providers_with_currency()[0]
        self.assertEqual(provider_with_currency["name"], "AdapterTel")
        self.assertEqual(provider_with_currency["currency_code"], "EUR")
        self.assertTrue(any(row["name"] == "adapter-server" for row in self.repo.list_servers()))
        self.assertTrue(any(row["name"] == "Adapter reason" for row in self.repo.list_change_reasons()))
        self.assertTrue(any(row["name"] == "Adapter reason" for row in self.repo.list_active_change_reasons()))
        self.assertTrue(any(row["code"] == "gl" for row in self.repo.list_phone_assignment_types()))
        self.assertTrue(any(row["name"] == "Меж.деп." for row in self.repo.list_projects()))
        self.assertEqual(self.repo.list_provider_prefixes(self.provider_id)[0]["prefix"], "43")
        prefix_with_provider = self.repo.list_provider_prefixes_with_provider()[0]
        self.assertEqual(prefix_with_provider["prefix"], "43")
        self.assertEqual(prefix_with_provider["provider_name"], "AdapterTel")
        counts = self.repo.dictionary_counts()
        self.assertGreaterEqual(counts["currencies"], 1)
        self.assertGreaterEqual(counts["prefixes"], 1)
        self.assertGreaterEqual(counts["projects"], 1)
        self.assertGreaterEqual(counts["phone-assignments"], 1)

    def test_read_method_with_parameter_uses_backend_placeholder(self):
        row = self.repo.get_country(self.country_id)

        self.assertEqual(row["id"], self.country_id)
        self.assertEqual(row["code"], "AT")

    def test_dynamic_in_clause_read_method_handles_values_and_empty_list(self):
        rows = self.repo.list_countries_by_ids([self.country_id])
        empty_rows = self.repo.list_countries_by_ids([])

        self.assertEqual([row["id"] for row in rows], [self.country_id])
        self.assertEqual(empty_rows, [])

    def test_existing_write_methods_still_work_on_sqlite(self):
        created_id = self.repo.create_country("Бельгия", "BE")

        self.assertEqual(self.repo.get_country(created_id)["name"], "Бельгия")


if __name__ == "__main__":
    unittest.main()
