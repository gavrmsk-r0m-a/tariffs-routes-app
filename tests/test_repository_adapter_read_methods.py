import sqlite3
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

from app.db import init_db
from app.repository import Repository, query_filters
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

    def test_query_filters_sqlite_contract_and_mapping_order(self):
        filters = {"name_like": "  АДАПТЕР  ", "ignored": "x", "country_id": 7, "empty": ""}
        original = dict(filters)
        where, params = query_filters(filters, {"country_id": "r.country_id", "name_like": "r.name", "empty": "r.empty"})
        self.assertEqual(" WHERE r.country_id = ? AND search_text_matches(r.name, ?) = 1", where)
        self.assertEqual([7, "адаптер"], params)
        self.assertEqual(original, filters)
        self.assertEqual(("", []), query_filters({"x": "all"}, {"x": "r.x"}))

    def test_query_filters_postgres_literal_search_contract(self):
        for value in ("Demo_Route", "Demo%Route"):
            where, params = query_filters(
                {"country_id": 3, "name_like": value},
                {"country_id": "r.country_id", "name_like": "r.name"},
                backend="postgres",
            )
            normalized = where.upper()
            self.assertIn("R.COUNTRY_ID = %S", normalized)
            self.assertIn("POSITION", normalized)
            self.assertIn("LOWER", normalized)
            self.assertIn("COALESCE", normalized)
            self.assertNotIn("SEARCH_TEXT_MATCHES", normalized)
            self.assertNotIn(" LIKE ", normalized)
            self.assertNotIn(" ILIKE ", normalized)
            self.assertNotIn(value.upper(), normalized)
            self.assertEqual([3, value], params)

    def test_sqlite_search_udf_keeps_unicode_and_literal_semantics(self):
        self.assertEqual(1, self.conn.execute("SELECT search_text_matches(?, ?)", ("Привет Adapter", "пРИВЕТ")).fetchone()[0])
        self.assertEqual(0, self.conn.execute("SELECT search_text_matches(?, ?)", ("Demo Route", "Demo_Route")).fetchone()[0])
        self.assertEqual(0, self.conn.execute("SELECT search_text_matches(?, ?)", ("Demo Route", "Demo%Route")).fetchone()[0])
        self.assertEqual(1, self.conn.execute("SELECT search_text_matches(NULL, '')").fetchone()[0])

    def test_postgres_list_routes_sql_and_parameter_order(self):
        class CaptureConnection:
            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return []

        conn = CaptureConnection()
        filters = {"country_id": 4, "provider_id": 5, "is_actual": "1", "search_like": "Demo_Route", "prefix_id": 6}
        original = dict(filters)
        self.assertEqual([], Repository(conn, backend="postgres").list_routes(filters))
        sql = " ".join(conn.sql.split())
        self.assertIn("rpn.is_active = %s", sql)
        self.assertIn("r.provider_prefix_id = %s", sql)
        self.assertIn("r.is_actual = %s", sql)
        self.assertNotIn("rpn.is_active = 1", sql)
        self.assertNotIn("r.is_actual = 1", sql)
        self.assertNotIn("?", sql)
        self.assertNotIn("search_text_matches", sql)
        self.assertEqual([True, 4, 5, True, "Demo_Route", 6], conn.params)
        self.assertEqual(original, filters)

    def test_sqlite_list_routes_regression_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = create_demo_sqlite(Path(directory) / "demo.db")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                repo = Repository(conn)
                rows = repo.list_routes()
                demo = next(row for row in rows if row["name"] == "Demo Route")
                self.assertIsInstance(rows, list)
                self.assertIsInstance(demo, sqlite3.Row)
                self.assertEqual(
                    ["id", "country_id", "provider_id", "provider_prefix_id", "name", "project_label", "cli_source_type", "cli_source_label", "aon_pool", "rnd_type", "rnd_pool_owner", "comment", "is_actual", "priority_status", "inbound_line_available", "created_by", "created_at", "updated_by", "updated_at", "country_name", "provider_name", "prefix", "phone_count"],
                    demo.keys(),
                )
                self.assertIsInstance(demo["phone_count"], int)
                self.assertEqual([(row["country_name"], row["name"]) for row in rows], sorted((row["country_name"], row["name"]) for row in rows))
                country_id, provider_id = demo["country_id"], demo["provider_id"]
                prefix_id = demo["provider_prefix_id"]
                self.assertEqual("Demo Route", repo.list_routes({"country_id": country_id, "provider_id": provider_id})[0]["name"])
                self.assertEqual("Demo Route", repo.list_routes({"prefix_id": prefix_id, "is_actual": True, "search_like": "dEMO rOUTE"})[0]["name"])
                self.assertEqual([], repo.list_routes({"search_like": "Demo_Route"}))
                self.assertEqual([], repo.list_routes({"search_like": "Demo%Route"}))
            finally:
                conn.close()



    def test_postgres_list_calling_companies_sql_and_parameter_order(self):
        class CaptureConnection:
            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return []

        conn = CaptureConnection()
        filters = {
            "is_active": "1",
            "external_id_like": "  Manual-Company  ",
            "has_autorotation": "0",
            "company_like": "  Manual Company  ",
            "country_id": 5,
            "server_id": 4,
        }
        original = dict(filters)
        self.assertEqual([], Repository(conn, backend="postgres").list_calling_companies(filters))
        sql = " ".join(conn.sql.split())
        self.assertIn("COALESCE(active_crs.has_autorotation, %s) AS current_has_autorotation", sql)
        self.assertIn("active_crs.is_active = %s", sql)
        self.assertIn("cc.server_id = %s", sql)
        self.assertIn("cc.country_id = %s", sql)
        self.assertEqual(2, sql.count("POSITION(LOWER(CAST(%s AS TEXT)) IN LOWER(COALESCE(CAST("))
        self.assertIn("COALESCE(active_crs.has_autorotation, FALSE) = %s", sql)
        self.assertIn("cc.is_active = %s", sql)
        self.assertNotIn("COALESCE(active_crs.has_autorotation, 0)", sql)
        self.assertNotIn("CAST(COALESCE(active_crs.has_autorotation, 0) AS TEXT)", sql)
        self.assertNotIn("search_text_matches", sql)
        self.assertNotIn("?", sql)
        self.assertNotIn("Manual Company", sql)
        self.assertNotIn("Manual-Company", sql)
        self.assertNotIn(" = 4", sql)
        self.assertNotIn(" = 5", sql)
        self.assertEqual([False, True, 4, 5, "Manual Company", "Manual-Company", False, True], conn.params)
        self.assertEqual(original, filters)

    def test_sqlite_list_calling_companies_recording_and_regression_contract(self):
        class CaptureConnection:
            def create_function(self, *args):
                pass

            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return []

        capture = CaptureConnection()
        filters = {"is_active": "1", "external_id_like": "  MANUAL-COMPANY  ", "has_autorotation": "0", "company_like": "  MANUAL COMPANY  ", "country_id": 5, "server_id": 4}
        original = dict(filters)
        self.assertEqual([], Repository(capture).list_calling_companies(filters))
        sql = " ".join(capture.sql.split())
        self.assertIn("COALESCE(active_crs.has_autorotation, ?) AS current_has_autorotation", sql)
        self.assertIn("active_crs.is_active = ?", sql)
        self.assertIn("COALESCE(active_crs.has_autorotation, 0) = ?", sql)
        self.assertIn("cc.is_active = ?", sql)
        self.assertIn("search_text_matches(cc.company_name, ?) = 1", sql)
        self.assertIn("search_text_matches(cc.company_id_external, ?) = 1", sql)
        self.assertIn("cc.server_id = ?", sql)
        self.assertIn("cc.country_id = ?", sql)
        self.assertEqual([0, 1, 4, 5, "manual company", "manual-company", 0, 1], capture.params)
        self.assertEqual(original, filters)
        self.assertEqual([], Repository(capture).list_calling_companies({"has_autorotation": "invalid"}))

        with tempfile.TemporaryDirectory() as directory:
            path = create_demo_sqlite(Path(directory) / "demo.db")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                repo = Repository(conn)
                rows = repo.list_calling_companies()
                demo = next(row for row in rows if row["company_id_external"] == "demo-company-1")
                manual = next(row for row in rows if row["company_id_external"] == "ci-manual-company")
                inactive = next(row for row in rows if row["company_id_external"] == "ci-inactive-company")
                self.assertIsInstance(rows, list)
                self.assertIsInstance(demo, sqlite3.Row)
                self.assertEqual(["id", "server_id", "country_id", "company_name", "company_id_external", "has_autorotation", "line_count", "dial_set_count", "retry_interval_seconds", "comment", "is_active", "created_by", "created_at", "updated_by", "updated_at", "server_name", "country_name", "current_has_autorotation", "current_routing_mode", "current_route_id"], demo.keys())
                self.assertEqual([(row["country_name"], row["server_name"], row["company_name"]) for row in rows], sorted((row["country_name"], row["server_name"], row["company_name"]) for row in rows))
                self.assertEqual(1, manual["has_autorotation"])
                self.assertEqual(0, manual["current_has_autorotation"])
                self.assertEqual(0, inactive["current_has_autorotation"])
                false_ids = {row["company_id_external"] for row in repo.list_calling_companies({"has_autorotation": "0"})}
                self.assertIn("ci-manual-company", false_ids)
                self.assertIn("ci-inactive-company", false_ids)
                self.assertNotIn("demo-company-1", false_ids)
            finally:
                conn.close()

    def test_postgres_list_tariffs_sql_and_status_parameter_order(self):
        class CaptureConnection:
            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return []

        conn = CaptureConnection()
        filters = {"provider_id": 5, "country_id": 4, "status": "inactive"}
        original = dict(filters)
        self.assertEqual([], Repository(conn, backend="postgres").list_tariffs(filters))
        sql = " ".join(conn.sql.split())
        self.assertIn("t.country_id = %s", sql)
        self.assertIn("t.provider_id = %s", sql)
        self.assertIn("t.is_current = %s", sql)
        self.assertNotIn("t.is_current = 1", sql)
        self.assertNotIn("t.is_current = 0", sql)
        self.assertNotIn("?", sql)
        self.assertNotIn(" = 4", sql)
        self.assertNotIn(" = 5", sql)
        self.assertEqual([4, 5, False], conn.params)
        self.assertEqual(original, filters)

    def test_postgres_list_tariffs_status_predicates(self):
        class CaptureConnection:
            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return []

        cases = ((None, [True], True), ({"status": "active"}, [True], True), ({"status": "inactive"}, [False], True), ({"status": "all"}, [], False), ({"status": ""}, [], False), ({"status": None}, [], False))
        for filters, expected_params, has_predicate in cases:
            with self.subTest(filters=filters):
                conn = CaptureConnection()
                Repository(conn, backend="postgres").list_tariffs(filters)
                sql = " ".join(conn.sql.split())
                self.assertEqual(expected_params, conn.params)
                self.assertEqual(has_predicate, "t.is_current = %s" in sql)

    def test_sqlite_list_tariffs_sql_and_status_parameters(self):
        class CaptureConnection:
            def create_function(self, *args):
                pass

            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return []

        cases = (({"status": "active"}, [1], True), ({"status": "inactive"}, [0], True), ({"status": "all"}, [], False), ({"status": ""}, [], False), ({"status": None}, [], False))
        for filters, expected_params, has_predicate in cases:
            with self.subTest(filters=filters):
                conn = CaptureConnection()
                Repository(conn).list_tariffs(filters)
                sql = " ".join(conn.sql.split())
                self.assertEqual(expected_params, conn.params)
                self.assertEqual(has_predicate, "t.is_current = ?" in sql)
                self.assertIn("ORDER BY c.name, p.name, COALESCE(pp.prefix, '')", sql)

        conn = CaptureConnection()
        Repository(conn).list_tariffs({"provider_id": 5, "country_id": 4, "status": "inactive"})
        sql = " ".join(conn.sql.split())
        self.assertIn("t.country_id = ?", sql)
        self.assertIn("t.provider_id = ?", sql)
        self.assertEqual([4, 5, 0], conn.params)

    def test_sqlite_list_tariffs_regression_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = create_demo_sqlite(Path(directory) / "demo.db")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                repo = Repository(conn)
                rows = repo.list_tariffs({"status": "all"})
                demo = next(row for row in rows if row["provider_name"] == "Demo Provider")
                inactive = next(row for row in rows if row["provider_name"] == "Inactive Tariff Provider")
                self.assertIsInstance(rows, list)
                self.assertIsInstance(demo, sqlite3.Row)
                self.assertEqual(
                    ["id", "country_id", "provider_id", "provider_prefix_id", "provider_currency_id", "price_in_provider_currency", "conversion_rate_to_eur", "conversion_rate_date", "currency_rate_id", "eur_price", "priority_status", "is_estimated", "comment", "valid_from", "valid_to", "is_current", "created_by", "created_at", "updated_by", "updated_at", "country_name", "provider_name", "prefix", "currency_code"],
                    demo.keys(),
                )
                self.assertEqual([(row["country_name"], row["provider_name"], row["prefix"] or "") for row in rows], sorted((row["country_name"], row["provider_name"], row["prefix"] or "") for row in rows))
                self.assertEqual(1, demo["is_current"])
                self.assertEqual(0, demo["is_estimated"])
                self.assertEqual(0, inactive["is_current"])
                self.assertEqual(1, inactive["is_estimated"])
                self.assertEqual(Decimal("0.1"), Decimal(str(demo["price_in_provider_currency"])))
                self.assertEqual(Decimal("1"), Decimal(str(demo["conversion_rate_to_eur"])))
                self.assertEqual(Decimal("0.1"), Decimal(str(demo["eur_price"])))
                self.assertEqual([demo["id"]], [row["id"] for row in repo.list_tariffs({"status": "active"}) if row["provider_name"] == "Demo Provider"])
                self.assertEqual([inactive["id"]], [row["id"] for row in repo.list_tariffs({"status": "inactive"}) if row["provider_name"] == "Inactive Tariff Provider"])
                self.assertTrue(repo.list_tariffs({"country_id": demo["country_id"]}))
                self.assertTrue(repo.list_tariffs({"provider_id": demo["provider_id"]}))
                self.assertTrue(repo.list_tariffs({"status": "all"}))
                self.assertTrue(repo.list_tariffs({"status": ""}))
                self.assertTrue(repo.list_tariffs({"status": None}))
            finally:
                conn.close()

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
                self.assertEqual(1, company["current_has_autorotation"])
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

    def test_stage_35_reads_preserve_sqlite_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = create_demo_sqlite(Path(directory) / "demo.db")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                repo = Repository(conn)
                country = repo.get_country_by_name("Demo Country")
                provider = repo.get_provider_by_normalized_name("demo provider")
                currency = repo.get_currency_by_code("EUR")
                phone_type = next(row for row in repo.list_phone_number_types() if row["name"] == "Mobile")
                project = next(row for row in repo.list_projects() if row["code"] == "rep")
                assignment = next(row for row in repo.list_phone_assignment_types() if row["code"] == "gl")

                expected = {
                    "countries": {"Купленные номера": 1, "Маршруты": 1, "Тарифы": 1},
                    "providers": {"Купленные номера": 1, "Маршруты": 1, "Тарифы": 1},
                    "currencies": {"Купленные номера": 1, "Тарифы": 1},
                    "phone-types": {"Купленные номера": 1},
                    "projects": {"Купленные номера": 1, "Маршруты": 1},
                    "phone-assignments": {"Купленные номера": 1},
                }
                entities = {"countries": country, "providers": provider, "currencies": currency, "phone-types": phone_type, "projects": project, "phone-assignments": assignment}
                for kind, counts in expected.items():
                    with self.subTest(kind=kind):
                        preview = repo.dictionary_rename_preview(kind, entities[kind]["id"])
                        self.assertEqual(counts, preview)
                        self.assertTrue(all(type(value) is int for value in preview.values()))
                self.assertEqual({}, repo.dictionary_rename_preview("unknown", -1))

                company = next(row for row in repo.list_calling_companies() if row["company_id_external"] == "demo-company-1")
                user_id = company["created_by"]
                permission = repo.get_user_section_permission(user_id, "routes")
                self.assertIsInstance(permission, sqlite3.Row)
                self.assertEqual(1, permission["can_read"])
                self.assertIsNone(repo.get_user_section_permission(user_id, "missing"))
                permissions = repo.get_user_permissions(user_id)
                self.assertIsInstance(permissions["routes"], sqlite3.Row)
                self.assertEqual({}, repo.get_user_permissions(-1))

                phone_identity = repo.get_phone_number_import_identity_by_normalized_number("525550000001")
                phone = repo.get_phone_number(phone_identity["id"])
                self.assertIsInstance(phone, sqlite3.Row)
                self.assertIsNone(repo.get_phone_number(-1))
                route = repo.get_route(company["current_route_id"])
                self.assertIsInstance(route, sqlite3.Row)
                self.assertIsNone(repo.get_route(-1))
                numbers = repo.route_numbers(route["id"])
                self.assertIsInstance(numbers, list)
                self.assertIsInstance(numbers[0], sqlite3.Row)
                self.assertEqual(
                    [
                        "link_id",
                        "phone_id",
                        "number",
                        "status",
                        "assignment_type",
                        "connection_cost",
                        "monthly_fee",
                        "outgoing_rate",
                        "incoming_rate",
                        "phone_comment",
                        "link_comment",
                    ],
                    numbers[0].keys()[:11],
                )
                self.assertEqual(["usage_type", "is_active"], numbers[0].keys()[11:])
                self.assertEqual("cli", numbers[0]["usage_type"])
                self.assertEqual(1, numbers[0]["is_active"])
                self.assertEqual([], repo.route_numbers(-1))

                prefix = next(row for row in repo.list_provider_prefixes(provider["id"]) if row["prefix"] == "123")
                tariff = repo.find_tariff_by_identity(country["id"], provider["id"], prefix["id"])
                self.assertIsInstance(tariff, sqlite3.Row)
                self.assertIsNone(repo.find_tariff_by_identity(country["id"], -1, None))
                self.assertIsInstance(repo.get_tariff(tariff["id"]), sqlite3.Row)
                self.assertIsNone(repo.get_tariff(-1))
            finally:
                conn.close()

    def test_stage_35_postgres_placeholders_and_boolean_parameter_order(self):
        class Result:
            def fetchone(self):
                return None

            def __iter__(self):
                return iter(())

        class CaptureConnection:
            def execute(self, sql, params=()):
                self.calls.append((" ".join(sql.split()), params))
                return Result()

            def __init__(self):
                self.calls = []

        conn = CaptureConnection()
        repo = Repository(conn, backend="postgres")
        self.assertEqual([], repo.route_numbers(42))
        sql, params = conn.calls[-1]
        self.assertIn("rpn.route_id = %s", sql)
        self.assertIn("rpn.is_active = %s", sql)
        self.assertEqual((42, True, True), params)

        self.assertIsNone(repo.get_tariff(9))
        self.assertIn("WHERE t.id = %s", conn.calls[-1][0])

    def test_postgres_calling_company_boolean_fallback_parameter_order(self):
        class CaptureConnection:
            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return []

        conn = CaptureConnection()
        rows = Repository(conn, backend="postgres").list_calling_companies()

        self.assertEqual([], rows)
        normalized_sql = " ".join(conn.sql.split())
        self.assertNotIn("COALESCE(active_crs.has_autorotation, 0)", normalized_sql)
        self.assertIn("COALESCE(active_crs.has_autorotation, %s)", normalized_sql)
        self.assertIn("active_crs.is_active = %s", normalized_sql)
        self.assertEqual([False, True], conn.params)

    def test_stage_36_sqlite_user_read_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = create_demo_sqlite(Path(directory) / "demo.db")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            statements = []
            conn.set_trace_callback(statements.append)
            try:
                repo = Repository(conn)
                self.assertIn("role_key", repo._user_columns())
                users = repo.list_users()
                self.assertIsInstance(users, list)
                self.assertIsInstance(users[0], sqlite3.Row)
                keys = ["id", "username", "display_name", "role_key", "email", "must_change_password", "is_active", "created_at", "updated_at"]
                self.assertEqual(keys, users[0].keys())
                admin = next(row for row in users if row["username"] == "admin")
                inactive = next(row for row in users if row["username"] == "ci-inactive")
                self.assertEqual(0, inactive["is_active"])
                self.assertNotIn("ci-inactive", [row["username"] for row in repo.list_users(active_only=True)])
                detail = repo.get_user(admin["id"])
                self.assertIsInstance(detail, sqlite3.Row)
                self.assertEqual(keys, detail.keys())
                login = repo.get_user_by_username(" admin ")
                self.assertIsInstance(login, sqlite3.Row)
                self.assertEqual(keys + ["password_hash", "password_salt"], login.keys())
                self.assertEqual("admin", repo.authenticate_user("admin", "admin")["username"])
                self.assertIsNone(repo.authenticate_user("admin", "wrong"))
                self.assertTrue(any("PRAGMA table_info(users)" in sql for sql in statements))
                self.assertTrue(any("display_name COLLATE NOCASE" in sql and "username COLLATE NOCASE" in sql for sql in statements))
            finally:
                conn.close()

    def test_stage_36_legacy_sqlite_user_columns_and_role_fallback(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, display_name TEXT,
                     role TEXT, email TEXT, must_change_password INTEGER, is_active INTEGER,
                     created_at TEXT, updated_at TEXT, password_hash TEXT, password_salt TEXT)""")
        conn.execute("INSERT INTO users VALUES (1, 'legacy', '', 'ADMIN', NULL, 0, 1, 'now', 'now', NULL, NULL)")
        try:
            repo = Repository(conn)
            self.assertIn("role", repo._user_columns())
            self.assertNotIn("role_key", repo._user_columns())
            for row in (repo.list_users()[0], repo.get_user(1), repo.get_user_by_username("legacy")):
                self.assertEqual("admin", row["role_key"])
                self.assertEqual("legacy", row["display_name"])
        finally:
            conn.close()

    def test_stage_36_postgres_user_sql_recording(self):
        columns = ["id", "username", "display_name", "role_key", "email", "must_change_password", "is_active", "created_at", "updated_at", "password_hash", "password_salt"]

        class Result:
            def __init__(self, rows=()): self.rows = rows
            def __iter__(self): return iter(self.rows)
            def fetchone(self): return self.rows[0] if self.rows else None

        class Connection:
            def __init__(self): self.calls = []
            def execute(self, sql, params=()):
                normalized = " ".join(sql.split())
                self.calls.append((normalized, params))
                if "information_schema.columns" in normalized:
                    return Result([{"column_name": name} for name in columns])
                return Result()

        conn = Connection()
        repo = Repository(conn, backend="postgres")
        self.assertEqual(set(columns), repo._user_columns())
        repo.list_users(active_only=True)
        repo.get_user(7)
        repo.get_user_by_username(" admin ")
        introspection = next(call for call in conn.calls if "information_schema.columns" in call[0])
        self.assertIn("table_schema = current_schema()", introspection[0])
        self.assertIn("table_name = %s", introspection[0])
        self.assertEqual(("users",), introspection[1])
        self.assertFalse(any("PRAGMA" in sql for sql, _ in conn.calls))
        list_call = next(call for call in conn.calls if "FROM users" in call[0] and "ORDER BY" in call[0])
        self.assertEqual((True,), list_call[1])
        self.assertNotIn("COLLATE NOCASE", list_call[0])
        self.assertIn("LOWER(COALESCE(NULLIF(display_name, ''), username))", list_call[0])
        self.assertTrue(any("WHERE id = %s" in sql and params == (7,) for sql, params in conn.calls))
        self.assertTrue(any("WHERE username = %s" in sql and params == ("admin",) for sql, params in conn.calls))


    def test_postgres_list_phone_numbers_sql_and_parameter_order(self):
        class CaptureConnection:
            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return []

        conn = CaptureConnection()
        filters = {
            "review_required": "0",
            "number_like": "  525550000020  ",
            "status": "free",
            "assignment_type": "ivr",
            "project_like": "  CI PHONE PROJECT  ",
            "project": "CI Phone Project",
            "provider_id": 5,
            "country_id": 4,
        }
        original = dict(filters)
        self.assertEqual([], Repository(conn, backend="postgres").list_phone_numbers(filters))
        sql = " ".join(conn.sql.split())
        self.assertIn("STRING_AGG(r.name, ', ' ORDER BY r.name)", sql)
        self.assertIn("rpn.is_active = %s", sql)
        self.assertIn("pn.country_id = %s", sql)
        self.assertIn("COALESCE(pn.provider_id, 0) = %s", sql)
        self.assertIn("pn.project_label = %s", sql)
        self.assertEqual(2, sql.count("POSITION(LOWER(CAST(%s AS TEXT)) IN LOWER(COALESCE(CAST("))
        self.assertIn("pn.assignment_type = %s", sql)
        self.assertIn("pn.status = %s", sql)
        self.assertIn("pn.review_required = %s", sql)
        self.assertIn("ORDER BY pn.number", sql)
        for forbidden in ("GROUP_CONCAT", "rpn.is_active = 1", "search_text_matches", "?", "LIKE", "ILIKE", "CI PHONE PROJECT", "525550000020", " = 4", " = 5"):
            self.assertNotIn(forbidden, sql)
        self.assertEqual([True, 4, 5, "CI Phone Project", "CI PHONE PROJECT", "ivr", "free", "525550000020", False], conn.params)
        self.assertEqual(original, filters)

        no_filter_conn = CaptureConnection()
        Repository(no_filter_conn, backend="postgres").list_phone_numbers()
        self.assertEqual([True], no_filter_conn.params)
        invalid_conn = CaptureConnection()
        self.assertEqual([], Repository(invalid_conn, backend="postgres").list_phone_numbers({"review_required": "invalid"}))
        self.assertFalse(hasattr(invalid_conn, "sql"))

    def test_sqlite_list_phone_numbers_recording_and_regression_contract(self):
        class CaptureConnection:
            def create_function(self, *args):
                pass
            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return []

        capture = CaptureConnection()
        filters = {"review_required": "0", "number_like": "  525550000020  ", "status": "free", "assignment_type": "ivr", "project_like": "  CI PHONE PROJECT  ", "project": "CI Phone Project", "provider_id": 5, "country_id": 4}
        original = dict(filters)
        self.assertEqual([], Repository(capture).list_phone_numbers(filters))
        sql = " ".join(capture.sql.split())
        self.assertIn("GROUP_CONCAT", sql)
        self.assertIn("rpn.is_active = ?", sql)
        self.assertIn("pn.country_id = ?", sql)
        self.assertIn("COALESCE(pn.provider_id, 0) = ?", sql)
        self.assertIn("search_text_matches(pn.project_label, ?) = 1", sql)
        self.assertIn("search_text_matches(pn.number, ?) = 1", sql)
        self.assertEqual([1, 4, 5, "CI Phone Project", "ci phone project", "ivr", "free", "525550000020", 0], capture.params)
        self.assertEqual(original, filters)
        no_filter_capture = CaptureConnection()
        Repository(no_filter_capture).list_phone_numbers()
        self.assertEqual([1], no_filter_capture.params)
        invalid_capture = CaptureConnection()
        self.assertEqual([], Repository(invalid_capture).list_phone_numbers({"review_required": "invalid"}))
        self.assertFalse(hasattr(invalid_capture, "sql"))

        with tempfile.TemporaryDirectory() as directory:
            path = create_demo_sqlite(Path(directory) / "demo.db")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                repo = Repository(conn)
                rows = repo.list_phone_numbers()
                by_number = {row["number"]: row for row in rows}
                self.assertIsInstance(rows, list)
                self.assertIsInstance(rows[0], sqlite3.Row)
                expected_keys = ["id", "country_id", "provider_id", "country_label", "provider_label", "number", "normalized_number", "project_label", "assignment_type", "assignment_label", "phone_type", "tariff_label", "status", "connection_cost", "monthly_fee", "outgoing_rate", "incoming_rate", "currency_id", "currency_label", "comment", "is_active", "review_required", "imported_created_by", "created_by", "created_at", "updated_by", "updated_at", "deactivated_at", "country_name", "provider_name", "currency_code", "assignment_type_label", "route_names"]
                self.assertEqual(expected_keys, rows[0].keys())
                self.assertEqual([row["number"] for row in rows], sorted(row["number"] for row in rows))
                self.assertEqual("", by_number["525550000010"]["route_names"])
                self.assertEqual("CI Phone Route A, CI Phone Route B", by_number["525550000020"]["route_names"])
                self.assertNotIn("Hidden", by_number["525550000020"]["route_names"])
                self.assertEqual(["525550000010"], [row["number"] for row in repo.list_phone_numbers({"provider_id": 0})])
                self.assertEqual(["525550000020"], [row["number"] for row in repo.list_phone_numbers({"number_like": "0000020"})])
                self.assertEqual([], repo.list_phone_numbers({"number_like": "5255500000%20"}))
                self.assertEqual(["525550000010"], [row["number"] for row in repo.list_phone_numbers({"review_required": True})])
                self.assertEqual({"525550000001", "525550000020"}, {row["number"] for row in repo.list_phone_numbers({"review_required": "0"})})
            finally:
                conn.close()

class CompanyRoutingSettingsAdapterReadMethodsTest(unittest.TestCase):
    def test_postgres_company_routing_settings_sql_and_parameter_order(self):
        class CaptureConnection:
            def __init__(self): self.calls = []
            def execute(self, sql, params=()):
                self.calls.append((" ".join(sql.split()), params))
                return []
        conn = CaptureConnection()
        self.assertEqual([], Repository(conn, backend="postgres").list_company_routing_settings())
        sql, params = conn.calls[-1]
        self.assertIn("crs.is_active = %s", sql)
        self.assertIn("crs.valid_to IS NULL", sql)
        self.assertIn("ORDER BY c.name, s.name, cc.company_name, crs.valid_from DESC, crs.id DESC", sql)
        self.assertNotIn("crs.is_active = 1", sql)
        self.assertNotIn("search_text_matches", sql)
        self.assertNotIn("?", sql)
        self.assertEqual([True], params)

        filters = {"is_active": "0", "company_id_external": "  MANUAL-COMPANY  ", "calling_company_id": 9, "routing_mode": "autorotation", "server_id": 5, "country_id": 4, "show_history": "1"}
        original = dict(filters)
        Repository(conn, backend="postgres").list_company_routing_settings(filters)
        sql, params = conn.calls[-1]
        self.assertNotIn("crs.valid_to IS NULL", sql)
        self.assertIn("crs.country_id = %s", sql)
        self.assertIn("crs.server_id = %s", sql)
        self.assertIn("crs.routing_mode = %s", sql)
        self.assertIn("crs.calling_company_id = %s", sql)
        self.assertIn("POSITION(LOWER(CAST(%s AS TEXT)) IN LOWER(COALESCE(CAST(cc.company_id_external AS TEXT), ''))) > 0", sql)
        self.assertIn("crs.is_active = %s", sql)
        self.assertNotIn("search_text_matches", sql)
        self.assertNotIn(" LIKE ", sql.upper())
        self.assertNotIn(" ILIKE ", sql.upper())
        self.assertNotIn("?", sql)
        self.assertNotIn("MANUAL-COMPANY", sql)
        self.assertEqual([4, 5, "autorotation", 9, "MANUAL-COMPANY", False], params)
        self.assertEqual(original, filters)
        self.assertEqual([], Repository(conn, backend="postgres").list_company_routing_settings({"include_history": "false"}))
        self.assertEqual([], Repository(conn, backend="postgres").list_company_routing_settings({"include_history": "1", "is_active": "yes"}))
        self.assertEqual(len(conn.calls), 2)
        Repository(conn, backend="postgres").list_company_routing_settings({"include_history": "0", "is_active": "0"})
        self.assertEqual([True], conn.calls[-1][1])

    def test_postgres_company_routing_detail_uses_placeholder(self):
        class Result:
            def fetchone(self): return None
        class CaptureConnection:
            def execute(self, sql, params=()):
                self.sql = " ".join(sql.split()); self.params = params; return Result()
        conn = CaptureConnection()
        self.assertIsNone(Repository(conn, backend="postgres").get_company_routing_setting(7))
        self.assertIn("WHERE crs.id = %s", conn.sql)
        self.assertEqual((7,), conn.params)

    def test_sqlite_company_routing_settings_recording_and_fixture_contract(self):
        class CaptureConnection:
            def create_function(self, *args): pass
            def execute(self, sql, params=()):
                self.sql = " ".join(sql.split()); self.params = params; return []
        capture = CaptureConnection()
        filters = {"company_id_external": "  MANUAL-COMPANY  ", "country_id": 4, "server_id": 5, "routing_mode": "autorotation", "calling_company_id": 9, "include_history": "1", "is_active": "0"}
        original = dict(filters)
        self.assertEqual([], Repository(capture).list_company_routing_settings(filters))
        self.assertIn("search_text_matches(cc.company_id_external, ?) = 1", capture.sql)
        self.assertEqual([4, 5, "autorotation", 9, "manual-company", 0], capture.params)
        self.assertEqual(original, filters)
        self.assertEqual([], Repository(capture).list_company_routing_settings({"show_history": "true"}))

        with tempfile.TemporaryDirectory() as directory:
            path = create_demo_sqlite(Path(directory) / "demo.db")
            conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
            try:
                repo = Repository(conn)
                rows = repo.list_company_routing_settings()
                history = repo.list_company_routing_settings({"include_history": "1"})
                self.assertIsInstance(rows, list)
                self.assertIsInstance(rows[0], sqlite3.Row)
                list_keys = ["id", "calling_company_id", "country_id", "server_id", "route_id", "routing_mode", "has_autorotation", "is_active", "comment", "valid_from", "valid_to", "created_at", "created_by", "updated_at", "updated_by", "country_name", "server_name", "company_id_external", "company_name", "route_name", "provider_name", "updated_by_username"]
                detail_keys = list_keys[:-1]
                self.assertEqual(list_keys, rows[0].keys())
                manual_modes = [row["routing_mode"] for row in history if row["company_id_external"] == "ci-manual-company"]
                self.assertEqual(["server_priority", "autorotation"], manual_modes)
                self.assertEqual({"ci-manual-company"}, {row["company_id_external"] for row in repo.list_company_routing_settings({"company_id_external": "manual-company"})})
                self.assertEqual({"ci-manual-company"}, {row["company_id_external"] for row in repo.list_company_routing_settings({"include_history": "1", "is_active": "0"})})
                detail = repo.get_company_routing_setting(rows[0]["id"])
                self.assertIsInstance(detail, sqlite3.Row)
                self.assertEqual(detail_keys, detail.keys())
                self.assertIsNone(repo.get_company_routing_setting(-1))
            finally:
                conn.close()

class ProviderChangesAdapterReadMethodsTest(unittest.TestCase):
    def test_postgres_provider_changes_sql_and_parameter_order(self):
        class CaptureConnection:
            def execute(self, sql, params=()):
                self.sql = " ".join(sql.split())
                self.params = params
                return []
        conn = CaptureConnection()
        filters = {"date_from": "2026-07-10 00:00:00", "date_to": "2026-07-12 23:59:59", "country_id": 4, "provider_id": 5, "route_like": "Alpha_%", "reason_like": "Planned%", "user_id": 6}
        original = dict(filters)
        self.assertEqual([], Repository(conn, backend="postgres").list_provider_changes(filters))
        sql = conn.sql
        self.assertIn("STRING_AGG(s.name, ', ' ORDER BY s.name)", sql)
        self.assertIn("POSITION(LOWER(CAST(%s AS TEXT)) IN LOWER(COALESCE(CAST(rb.name AS TEXT), ''))) > 0", sql)
        self.assertIn("POSITION(LOWER(CAST(%s AS TEXT)) IN LOWER(COALESCE(CAST(ra.name AS TEXT), ''))) > 0", sql)
        self.assertIn("POSITION(LOWER(CAST(%s AS TEXT)) IN LOWER(COALESCE(CAST(pcl.reason_text AS TEXT), ''))) > 0", sql)
        self.assertIn("(pcl.provider_before_id = %s OR pcl.provider_after_id = %s)", sql)
        self.assertNotIn("GROUP_CONCAT", sql)
        self.assertNotIn("search_text_matches", sql)
        self.assertNotIn("?", sql)
        self.assertNotIn(" LIKE ", sql.upper())
        self.assertNotIn(" ILIKE ", sql.upper())
        self.assertNotIn("GROUP BY pcl.id", sql)
        self.assertNotIn("LEFT JOIN provider_change_log_servers", sql)
        self.assertEqual(["2026-07-10 00:00:00", "2026-07-12 23:59:59", 4, 5, 5, "Alpha_%", "Alpha_%", "Planned%", 6], conn.params)
        self.assertEqual(original, filters)

    def test_sqlite_provider_changes_recording_and_fixture_contract(self):
        class CaptureConnection:
            def create_function(self, *args): pass
            def execute(self, sql, params=()):
                self.sql = " ".join(sql.split())
                self.params = params
                return []
        capture = CaptureConnection()
        Repository(capture).list_provider_changes({"route_like": "Alpha_%", "reason_like": "Planned%", "provider_id": 5})
        self.assertIn("GROUP_CONCAT", capture.sql)
        self.assertIn("search_text_matches(rb.name, ?) = 1", capture.sql)
        self.assertIn("search_text_matches(ra.name, ?) = 1", capture.sql)
        self.assertIn("search_text_matches(pcl.reason_text, ?) = 1", capture.sql)
        self.assertIn("?", capture.sql)
        self.assertNotIn("STRING_AGG", capture.sql)
        self.assertNotIn("GROUP BY pcl.id", capture.sql)
        self.assertEqual([5, 5, "alpha_%", "alpha_%", "planned%"], capture.params)

        with tempfile.TemporaryDirectory() as directory:
            path = create_demo_sqlite(Path(directory) / "demo.db")
            conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
            try:
                repo = Repository(conn)
                rows = [row for row in repo.list_provider_changes() if row["reason_text"] in {"Planned provider switch", "AON refresh without provider switch"}]
                self.assertEqual(["Planned provider switch", "AON refresh without provider switch"], [row["reason_text"] for row in rows])
                self.assertEqual("Stage 42 Server A, Stage 42 Server B", rows[0]["server_names"])
                self.assertIsNone(rows[1]["server_names"])
                self.assertEqual([], repo.list_provider_changes({"route_like": "Stage 42 Alpha_"}))
                self.assertEqual([], repo.list_provider_changes({"reason_like": "Planned%provider"}))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
