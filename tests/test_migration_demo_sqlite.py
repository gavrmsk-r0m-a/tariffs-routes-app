import sqlite3
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

from scripts import migrate_sqlite_to_postgres as migration
from scripts import postgres_preflight
from scripts.create_migration_demo_sqlite import create_demo_sqlite

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
