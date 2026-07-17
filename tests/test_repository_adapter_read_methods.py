import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import init_db
from app.repository import Repository
from scripts.create_migration_demo_sqlite import create_demo_sqlite


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

    def test_importer_phone_identity_read_method_uses_backend_placeholder(self):
        phone_id = self.repo.create_phone_number(
            country_id=self.country_id,
            provider_id=self.provider_id,
            number="43123456789",
            assignment_type="gl",
            status="used",
            created_by=1,
            currency_id=self.currency_id,
            imported_created_by="Excel User",
        )

        row = self.repo.get_phone_number_import_identity_by_normalized_number("43123456789")

        self.assertEqual(
            row,
            {
                "id": phone_id,
                "imported_created_by": "Excel User",
                "review_required": 0,
                "deactivated_at": None,
            },
        )
        self.assertIsNone(self.repo.get_phone_number_import_identity_by_normalized_number("43123456780"))

    def test_dynamic_in_clause_read_method_handles_values_and_empty_list(self):
        rows = self.repo.list_countries_by_ids([self.country_id])
        empty_rows = self.repo.list_countries_by_ids([])

        self.assertEqual([row["id"] for row in rows], [self.country_id])
        self.assertEqual(empty_rows, [])

    def test_existing_write_methods_still_work_on_sqlite(self):
        created_id = self.repo.create_country("Бельгия", "BE")

        self.assertEqual(self.repo.get_country(created_id)["name"], "Бельгия")

    def test_stage_34_reads_preserve_sqlite_rows_and_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = create_demo_sqlite(Path(directory) / "demo.db")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                repo = Repository(conn)
                self.assertEqual("enabled", repo.get_app_setting_value("demo_setting"))
                self.assertIsNone(repo.get_app_setting_value("missing_setting"))
                self.assertEqual(1, repo.get_hlr_daily_usage("2026-07-12")["checked_today"])
                self.assertEqual("2500", repo.get_hlr_limit_override())

                companies = repo.list_calling_companies()
                company = next(row for row in companies if row["company_id_external"] == "demo-company-1")
                self.assertIsInstance(company, sqlite3.Row)
                self.assertEqual("Demo Company", repo.get_calling_company(company["id"])["company_name"])
                self.assertIsNone(repo.get_calling_company(-1))

                currency_id = next(row["id"] for row in repo.list_currencies() if row["code"] == "EUR")
                latest = repo.latest_currency_rate(currency_id)
                self.assertIsInstance(latest, sqlite3.Row)
                self.assertEqual("2026-07-12", latest["rate_date"])
                self.assertEqual("EUR", repo.get_currency_rate(latest["id"])["currency_code"])
                self.assertIsNone(repo.latest_currency_rate(-1))
                self.assertIsNone(repo.get_currency_rate(-1))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
