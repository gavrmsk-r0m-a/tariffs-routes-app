import sqlite3
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

from scripts import migrate_sqlite_to_postgres as migration
from scripts import postgres_preflight
from scripts.create_migration_demo_sqlite import create_demo_sqlite, HISTORY_FROM, NOW

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = REPO_ROOT / "docs" / "postgres" / "schema.postgres.sql"
CORE_TABLES = (
    "users", "countries", "currencies", "providers", "projects", "servers",
    "routes", "tariffs", "phone_numbers", "calling_companies",
)


class MigrationDemoSqliteTests(unittest.TestCase):
    def make_demo_db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "migration_demo.sqlite3"
        create_demo_sqlite(db_path)
        return db_path

    def test_create_demo_sqlite_creates_file(self):
        db_path = self.make_demo_db()
        self.assertTrue(db_path.exists())
        self.assertGreater(db_path.stat().st_size, 0)

    def test_demo_sqlite_has_expected_tables(self):
        db_path = self.make_demo_db()
        with sqlite3.connect(db_path) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(set(migration.MIGRATION_ORDER), tables)
        self.assertEqual(35, len(tables))

    def test_demo_sqlite_preflight_passes(self):
        db_path = self.make_demo_db()
        report = postgres_preflight.run_preflight(db_path, SCHEMA)
        self.assertEqual(0, report.errors_count)
        self.assertEqual(0, report.warnings_count)

    def test_demo_sqlite_has_rows_in_core_tables(self):
        db_path = self.make_demo_db()
        with sqlite3.connect(db_path) as conn:
            for table in CORE_TABLES:
                with self.subTest(table=table):
                    count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    self.assertGreater(count, 0)

    def test_demo_sqlite_has_deterministic_inactive_user(self):
        db_path = self.make_demo_db()
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM users WHERE username = ?", ("ci-inactive",)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual("CI Inactive", row["display_name"])
        self.assertEqual("guest", row["role_key"])
        self.assertEqual("ci-inactive@example.invalid", row["email"])
        self.assertEqual(0, row["is_active"])
        self.assertEqual(0, row["must_change_password"])
        self.assertIsNone(row["password_hash"])
        self.assertIsNone(row["password_salt"])
        self.assertEqual("2026-07-12 10:00:00", row["created_at"])
        self.assertEqual("2026-07-12 10:00:00", row["updated_at"])

    def test_demo_sqlite_has_synthetic_inactive_tariff(self):
        db_path = self.make_demo_db()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT t.*, c.name AS country_name, p.name AS provider_name, cur.code AS currency_code
                FROM tariffs t
                JOIN countries c ON c.id = t.country_id
                JOIN providers p ON p.id = t.provider_id
                JOIN currencies cur ON cur.id = t.provider_currency_id
                WHERE c.name = ? AND p.name = ?
                """,
                ("Inactive Tariff Country", "Inactive Tariff Provider"),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)
        self.assertEqual("Inactive Tariff Country", row["country_name"])
        self.assertEqual("Inactive Tariff Provider", row["provider_name"])
        self.assertEqual("XTS", row["currency_code"])
        self.assertIsNone(row["provider_prefix_id"])
        self.assertEqual(Decimal("2.5"), Decimal(str(row["price_in_provider_currency"])))
        self.assertEqual(Decimal("0.4"), Decimal(str(row["conversion_rate_to_eur"])))
        self.assertEqual(Decimal("1"), Decimal(str(row["eur_price"])))
        self.assertEqual("alternative", row["priority_status"])
        self.assertEqual(1, row["is_estimated"])
        self.assertEqual(0, row["is_current"])
        self.assertEqual("2026-07-12 10:00:00", row["valid_to"])

    def test_demo_sqlite_has_stage_39_calling_company_fixtures(self):
        db_path = self.make_demo_db()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            manual = conn.execute(
                """
                SELECT cc.*, c.name AS country_name, s.name AS server_name
                FROM calling_companies cc
                JOIN countries c ON c.id = cc.country_id
                JOIN servers s ON s.id = cc.server_id
                WHERE cc.company_id_external = ?
                """,
                ("ci-manual-company",),
            ).fetchone()
            manual_settings = conn.execute(
                "SELECT * FROM company_routing_settings WHERE calling_company_id = ?",
                (manual["id"] if manual else -1,),
            ).fetchall()
            inactive = conn.execute(
                """
                SELECT cc.*, c.name AS country_name, s.name AS server_name
                FROM calling_companies cc
                JOIN countries c ON c.id = cc.country_id
                JOIN servers s ON s.id = cc.server_id
                WHERE cc.company_id_external = ?
                """,
                ("ci-inactive-company",),
            ).fetchone()
            inactive_settings = conn.execute(
                "SELECT * FROM company_routing_settings WHERE calling_company_id = ?",
                (inactive["id"] if inactive else -1,),
            ).fetchall()
            historical_event_count = conn.execute(
                "SELECT COUNT(*) FROM routing_events WHERE calling_company_id = ? AND comment = ?",
                (manual["id"] if manual else -1, "Synthetic historical autorotation setting"),
            ).fetchone()[0]
            historical_change_log_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM change_log cl
                JOIN company_routing_settings crs ON crs.id = cl.entity_id
                WHERE cl.entity_type = ?
                  AND crs.calling_company_id = ?
                  AND cl.comment = ?
                """,
                ("company_routing_setting", manual["id"] if manual else -1, "Synthetic historical autorotation setting"),
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertIsNotNone(manual)
        self.assertEqual("CI Manual Company Country", manual["country_name"])
        self.assertEqual("ci-manual-server-1", manual["server_name"])
        self.assertEqual(1, manual["has_autorotation"])
        self.assertEqual(1, manual["is_active"])
        self.assertEqual("2026-07-12 10:00:00", manual["created_at"])
        self.assertEqual("2026-07-12 10:00:00", manual["updated_at"])
        self.assertEqual(2, len(manual_settings))
        current = [row for row in manual_settings if row["valid_to"] is None]
        historical = [row for row in manual_settings if row["valid_to"] is not None]
        self.assertEqual(1, len(current))
        self.assertEqual(1, len(historical))
        self.assertEqual("server_priority", current[0]["routing_mode"])
        self.assertEqual(0, current[0]["has_autorotation"])
        self.assertEqual(1, current[0]["is_active"])
        self.assertIsNone(current[0]["valid_to"])
        self.assertIsNone(current[0]["route_id"])
        self.assertEqual(NOW, current[0]["valid_from"])
        self.assertEqual(NOW, current[0]["created_at"])
        self.assertEqual(NOW, current[0]["updated_at"])
        self.assertEqual("autorotation", historical[0]["routing_mode"])
        self.assertEqual(1, historical[0]["has_autorotation"])
        self.assertEqual(0, historical[0]["is_active"])
        self.assertEqual(HISTORY_FROM, historical[0]["valid_from"])
        self.assertEqual(NOW, historical[0]["valid_to"])
        self.assertEqual("Synthetic historical autorotation setting", historical[0]["comment"])
        self.assertIsNone(historical[0]["route_id"])
        self.assertEqual(manual["country_id"], current[0]["country_id"])
        self.assertEqual(manual["country_id"], historical[0]["country_id"])
        self.assertEqual(manual["server_id"], current[0]["server_id"])
        self.assertEqual(manual["server_id"], historical[0]["server_id"])
        self.assertEqual(0, historical_event_count)
        self.assertEqual(0, historical_change_log_count)

        self.assertIsNotNone(inactive)
        self.assertEqual("CI Inactive Company Country", inactive["country_name"])
        self.assertEqual("ci-inactive-server-1", inactive["server_name"])
        self.assertEqual(0, inactive["has_autorotation"])
        self.assertEqual(0, inactive["is_active"])
        self.assertEqual("2026-07-12 10:00:00", inactive["created_at"])
        self.assertEqual("2026-07-12 10:00:00", inactive["updated_at"])
        self.assertEqual([], inactive_settings)


    def test_stage_40_phone_number_fixtures(self):
        db_path = self.make_demo_db()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            review = conn.execute("""
                SELECT pn.*, c.name AS country_name, cur.code AS currency_code
                FROM phone_numbers pn
                JOIN countries c ON c.id = pn.country_id
                JOIN currencies cur ON cur.id = pn.currency_id
                WHERE pn.number = ?
            """, ("525550000010",)).fetchone()
            routed = conn.execute("""
                SELECT pn.*, c.name AS country_name, p.name AS provider_name, cur.code AS currency_code
                FROM phone_numbers pn
                JOIN countries c ON c.id = pn.country_id
                JOIN providers p ON p.id = pn.provider_id
                JOIN currencies cur ON cur.id = pn.currency_id
                WHERE pn.number = ?
            """, ("525550000020",)).fetchone()
            review_links = conn.execute("SELECT * FROM route_phone_numbers WHERE phone_number_id = ?", (review["id"],)).fetchall()
            routed_links = conn.execute("""
                SELECT r.name, rpn.*
                FROM route_phone_numbers rpn
                JOIN routes r ON r.id = rpn.route_id
                WHERE rpn.phone_number_id = ?
                ORDER BY r.name
            """, (routed["id"],)).fetchall()

        self.assertEqual("CI Review Phone Country", review["country_name"])
        self.assertIsNone(review["provider_id"])
        self.assertIsNone(review["provider_label"])
        self.assertEqual("problem", review["status"])
        self.assertEqual("ИТМ", review["project_label"])
        self.assertEqual("aon", review["assignment_type"])
        self.assertEqual(0, review["is_active"])
        self.assertEqual(1, review["review_required"])
        self.assertEqual("XPN", review["currency_code"])
        self.assertEqual("2026-07-12 10:00:00", review["deactivated_at"])
        self.assertEqual(Decimal("3.500000"), Decimal(str(review["connection_cost"])))
        self.assertEqual(Decimal("4.500000"), Decimal(str(review["monthly_fee"])))
        self.assertEqual(Decimal("0.150000"), Decimal(str(review["outgoing_rate"])))
        self.assertEqual(Decimal("0.050000"), Decimal(str(review["incoming_rate"])))
        self.assertEqual([], review_links)
        self.assertEqual("2026-07-12 10:00:00", review["created_at"])
        self.assertEqual("2026-07-12 10:00:00", review["updated_at"])

        self.assertEqual("CI Routed Phone Country", routed["country_name"])
        self.assertEqual("CI Phone Provider", routed["provider_name"])
        self.assertEqual("XPN", routed["currency_code"])
        self.assertEqual("free", routed["status"])
        self.assertEqual("CI Phone Project", routed["project_label"])
        self.assertEqual("ivr", routed["assignment_type"])
        self.assertEqual(1, routed["is_active"])
        self.assertEqual(0, routed["review_required"])
        self.assertIsNone(routed["deactivated_at"])
        self.assertEqual(Decimal("0.750000"), Decimal(str(routed["connection_cost"])))
        self.assertEqual(Decimal("1.500000"), Decimal(str(routed["monthly_fee"])))
        self.assertEqual(Decimal("0.030000"), Decimal(str(routed["outgoing_rate"])))
        self.assertEqual(Decimal("0.010000"), Decimal(str(routed["incoming_rate"])))
        self.assertEqual(3, len(routed_links))
        active_names = [row["name"] for row in routed_links if row["is_active"] == 1]
        inactive_names = [row["name"] for row in routed_links if row["is_active"] == 0]
        self.assertEqual(["CI Phone Route A", "CI Phone Route B"], active_names)
        self.assertEqual(["CI Phone Route Hidden"], inactive_names)
        self.assertEqual("2026-07-12 10:00:00", routed["created_at"])
        self.assertEqual("2026-07-12 10:00:00", routed["updated_at"])

    def test_demo_sqlite_has_no_empty_required_fields(self):
        db_path = self.make_demo_db()
        checks = (
            ("users", "username"),
            ("countries", "name"),
            ("providers", "name"),
            ("currencies", "code"),
            ("routes", "name"),
            ("phone_numbers", "number"),
            ("phone_numbers", "normalized_number"),
            ("calling_companies", "company_id_external"),
            ("calling_companies", "company_name"),
            ("servers", "name"),
            ("projects", "name"),
            ("projects", "code"),
        )
        with sqlite3.connect(db_path) as conn:
            for table, column in checks:
                with self.subTest(table=table, column=column):
                    count = conn.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NULL OR trim("{column}") = ""'
                    ).fetchone()[0]
                    self.assertEqual(0, count)

    def test_demo_sqlite_does_not_write_inside_repo_by_default(self):
        forbidden = REPO_ROOT / "mvp.sqlite3"
        before = forbidden.exists()
        db_path = self.make_demo_db()
        self.assertNotEqual(REPO_ROOT, db_path.parent)
        self.assertEqual(before, forbidden.exists())


if __name__ == "__main__":
    unittest.main()
