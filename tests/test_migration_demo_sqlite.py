import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

from scripts import migrate_sqlite_to_postgres as migration
from scripts import postgres_preflight
from scripts.create_migration_demo_sqlite import create_demo_sqlite, HISTORY_FROM, NOW, STAGE43_CAMPAIGN_AT, STAGE43_INACTIVE_AT, STAGE43_NONE_AT, STAGE43_SERVER_AT

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


    def test_stage_43_routing_event_and_tariff_fixtures(self):
        db_path = self.make_demo_db()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            tariffs = conn.execute("""
                SELECT t.*, c.name AS country_name, p.name AS provider_name, cur.code AS currency_code
                FROM tariffs t
                JOIN countries c ON c.id = t.country_id
                JOIN providers p ON p.id = t.provider_id
                JOIN currencies cur ON cur.id = t.provider_currency_id
                WHERE t.comment IN (?, ?)
                ORDER BY t.comment
            """, ("Synthetic Stage 43 new route tariff", "Synthetic Stage 43 old route tariff")).fetchall()
            tariff_ids = [row["id"] for row in tariffs]
            tariff_history_count = conn.execute(
                f"SELECT COUNT(*) FROM tariff_change_history WHERE tariff_id IN ({','.join('?' for _ in tariff_ids)})",
                tariff_ids,
            ).fetchone()[0]
            events = conn.execute("""
                SELECT re.*, c.name AS country_name, p.name AS provider_name, ar.name AS affected_route_name,
                       oldr.name AS old_route_name, newr.name AS new_route_name, overflowr.name AS overflow_route_name,
                       cc.company_id_external, cc.company_name, s.name AS server_name
                FROM routing_events re
                JOIN countries c ON c.id = re.country_id
                LEFT JOIN providers p ON p.id = re.provider_id
                LEFT JOIN routes ar ON ar.id = re.affected_route_id
                LEFT JOIN routes oldr ON oldr.id = re.old_route_id
                LEFT JOIN routes newr ON newr.id = re.new_route_id
                LEFT JOIN routes overflowr ON overflowr.id = re.overflow_route_id
                LEFT JOIN calling_companies cc ON cc.id = re.calling_company_id
                LEFT JOIN servers s ON s.id = re.server_id
                WHERE re.reason LIKE 'Stage 43%'
                ORDER BY re.event_at
            """).fetchall()
            event_ids = [row["id"] for row in events]
            event_servers = conn.execute("""
                SELECT res.*, s.name AS server_name, oldr.name AS old_route_name, newr.name AS new_route_name
                FROM routing_event_servers res
                JOIN servers s ON s.id = res.server_id
                JOIN routes oldr ON oldr.id = res.old_route_id
                JOIN routes newr ON newr.id = res.new_route_id
                WHERE res.routing_event_id IN (SELECT id FROM routing_events WHERE reason LIKE 'Stage 43%')
                ORDER BY res.id
            """).fetchall()
            change_log_count = conn.execute("SELECT COUNT(*) FROM change_log WHERE summary LIKE '%Stage 43%' OR comment LIKE '%Stage 43%' OR old_values LIKE '%Stage 43%' OR new_values LIKE '%Stage 43%'").fetchone()[0]

        self.assertEqual(2, len(tariffs))
        by_comment = {row["comment"]: row for row in tariffs}
        old_tariff = by_comment["Synthetic Stage 43 old route tariff"]
        new_tariff = by_comment["Synthetic Stage 43 new route tariff"]
        self.assertEqual("CI Routed Phone Country", old_tariff["country_name"])
        self.assertEqual("CI Phone Provider", old_tariff["provider_name"])
        self.assertEqual("CI Provider Change After", new_tariff["provider_name"])
        for row in tariffs:
            self.assertIsNone(row["provider_prefix_id"])
            self.assertEqual("XPN", row["currency_code"])
            self.assertEqual(1, row["is_current"])
            self.assertEqual(0, row["is_estimated"])
            self.assertEqual(NOW, row["created_at"])
            self.assertEqual(NOW, row["updated_at"])
        self.assertEqual(Decimal("1"), Decimal(str(old_tariff["eur_price"])))
        self.assertEqual(Decimal("1.5"), Decimal(str(new_tariff["eur_price"])))
        self.assertEqual(0, tariff_history_count)

        self.assertEqual(4, len(events))
        self.assertEqual([STAGE43_INACTIVE_AT, STAGE43_NONE_AT, STAGE43_SERVER_AT, STAGE43_CAMPAIGN_AT], [row["event_at"] for row in events])
        by_reason = {row["reason"]: row for row in events}
        self.assertEqual(0, by_reason["Stage 43 none inactive"]["is_active"])
        self.assertEqual("Synthetic Stage 43 archive", by_reason["Stage 43 none inactive"]["deactivation_reason"])
        self.assertIsNotNone(by_reason["Stage 43 none inactive"]["deactivated_at"])
        self.assertEqual(1, by_reason["Stage 43 none active"]["is_active"])
        self.assertEqual("none", by_reason["Stage 43 none active"]["apply_scope"])
        self.assertEqual("server_priority", by_reason["Stage 43 server priority"]["apply_scope"])
        self.assertEqual(1, by_reason["Stage 43 server priority"]["has_overflow"])
        self.assertEqual("CI Phone Route B", by_reason["Stage 43 server priority"]["overflow_route_name"])
        self.assertEqual("campaign_setting", by_reason["Stage 43 campaign setting"]["apply_scope"])
        self.assertIsNone(by_reason["Stage 43 campaign setting"]["server_id"])
        for row in events:
            self.assertEqual(NOW, row["created_at"])
            self.assertEqual(NOW, row["updated_at"])
            self.assertIsInstance(json.loads(row["snapshot_json"]), dict)
            self.assertIsNotNone(row["created_by"])
            self.assertIsNotNone(row["updated_by"])
        self.assertEqual(2, len(event_servers))
        self.assertEqual(["Stage 42 Server B", "Stage 42 Server A"], [row["server_name"] for row in event_servers])
        self.assertEqual(["Stage 42 Alpha", "Stage 42 Alpha"], [row["old_route_name"] for row in event_servers])
        self.assertEqual(["Stage 42 Beta", "Stage 42 Beta"], [row["new_route_name"] for row in event_servers])
        self.assertEqual(["applied", "applied"], [row["status"] for row in event_servers])
        self.assertEqual({by_reason["Stage 43 server priority"]["id"]}, {row["routing_event_id"] for row in event_servers})
        self.assertEqual(0, change_log_count)

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

# Stage 46 history rows deliberately document read-only smoke contracts.
class Stage46MigrationDemoFixtureTests(MigrationDemoSqliteTests):
    def test_stage_46_history_fixture_contract(self):
        db_path = self.make_demo_db()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            admin = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
            demo_phone = conn.execute("SELECT * FROM phone_numbers WHERE normalized_number = ?", ("525550000001",)).fetchone()
            routed_phone = conn.execute("SELECT * FROM phone_numbers WHERE normalized_number = ?", ("525550000020",)).fetchone()
            route = conn.execute("SELECT * FROM routes WHERE name = ?", ("Demo Route",)).fetchone()
            tariff = conn.execute("""SELECT t.* FROM tariffs t JOIN countries c ON c.id=t.country_id JOIN providers p ON p.id=t.provider_id JOIN provider_prefixes pp ON pp.id=t.provider_prefix_id WHERE c.name=? AND p.name=? AND pp.prefix=?""", ("Demo Country", "Demo Provider", "123")).fetchone()
            phone_history = conn.execute("SELECT * FROM phone_number_history WHERE reason = ?", ("Stage 46 phone status",)).fetchone()
            added = conn.execute("SELECT * FROM route_phone_number_history WHERE reason = ?", ("Stage 46 phone linked",)).fetchone()
            replaced = conn.execute("SELECT * FROM route_phone_number_history WHERE reason = ?", ("Stage 46 phone replaced",)).fetchone()
            route_history = conn.execute("SELECT * FROM route_history WHERE reason = ?", ("Stage 46 route comment",)).fetchone()
            tariff_history = conn.execute("SELECT * FROM tariff_change_history WHERE tariff_id = ? AND reason IN ('tariff.created', 'tariff.changed') ORDER BY changed_at", (tariff["id"],)).fetchall()
            stage46_log_count = conn.execute("SELECT COUNT(*) FROM change_log WHERE change_type LIKE 'Stage 46%' OR comment LIKE 'Synthetic Stage 46%'").fetchone()[0]
        self.assertEqual((demo_phone["id"], "updated", admin, "2026-07-17 10:00:00", "status", "problem", "used", "Synthetic Stage 46 phone history"), (phone_history["phone_number_id"], phone_history["action"], phone_history["changed_by"], phone_history["changed_at"], phone_history["field_name"], phone_history["old_value"], phone_history["new_value"], phone_history["comment"]))
        self.assertEqual((route["id"], demo_phone["id"], None, None, "added", admin, "2026-07-17 11:00:00", "usage_type=cli", "Stage 46 phone linked", "Synthetic Stage 46 route-phone history"), (added["route_id"], added["phone_number_id"], added["old_phone_number_id"], added["new_phone_number_id"], added["action"], added["changed_by"], added["changed_at"], added["new_values"], added["reason"], added["comment"]))
        self.assertEqual((route["id"], None, demo_phone["id"], routed_phone["id"], "replaced", admin, "2026-07-17 12:00:00", "525550000001", "525550000020", "Synthetic Stage 46 replacement history"), (replaced["route_id"], replaced["phone_number_id"], replaced["old_phone_number_id"], replaced["new_phone_number_id"], replaced["action"], replaced["changed_by"], replaced["changed_at"], replaced["old_values"], replaced["new_values"], replaced["comment"]))
        self.assertEqual(("updated", "comment", "Temporary Stage 46 route comment", "Synthetic route", "Stage 46 route comment", admin, "2026-07-17 09:00:00"), (route_history["action"], route_history["field_name"], route_history["old_value"], route_history["new_value"], route_history["reason"], route_history["changed_by"], route_history["changed_at"]))
        self.assertEqual(2, len(tariff_history))
        created, changed = tariff_history
        self.assertEqual("2026-07-17 08:00:00", created["changed_at"])
        self.assertIsNone(created["old_price_in_provider_currency"])
        self.assertIsNone(created["eur_price_delta"])
        self.assertEqual(Decimal("0.2"), Decimal(str(created["new_price_in_provider_currency"])))
        self.assertEqual(Decimal("0.2"), Decimal(str(created["new_eur_price"])))
        self.assertEqual("2026-07-17 13:00:00", changed["changed_at"])
        self.assertEqual(Decimal("0.2"), Decimal(str(changed["old_price_in_provider_currency"])))
        self.assertEqual(Decimal("0.1"), Decimal(str(changed["new_price_in_provider_currency"])))
        self.assertEqual(Decimal("-0.1"), Decimal(str(changed["eur_price_delta"])))
        self.assertEqual(admin, changed["changed_by"])
        self.assertEqual("used", demo_phone["status"])
        self.assertEqual("Synthetic route", route["comment"])
        self.assertEqual(Decimal("0.1"), Decimal(str(tariff["price_in_provider_currency"])))
        self.assertEqual(1, tariff["is_current"])
        self.assertEqual(0, stage46_log_count)

class Stage47MigrationDemoFixtureTests(MigrationDemoSqliteTests):
    def test_stage_47_routing_event_fixture_contract(self):
        db_path = self.make_demo_db()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT re.*, cc.company_id_external, old_route.name AS old_route_name, new_route.name AS new_route_name FROM routing_events re JOIN calling_companies cc ON cc.id=re.calling_company_id LEFT JOIN routes old_route ON old_route.id=re.old_company_route_id LEFT JOIN routes new_route ON new_route.id=re.new_company_route_id WHERE re.reason LIKE 'Stage 47%' ORDER BY re.event_at").fetchall()
            settings = conn.execute("SELECT COUNT(*) FROM company_routing_settings").fetchone()[0]
            links = conn.execute("SELECT COUNT(*) FROM routing_event_servers res JOIN routing_events re ON re.id=res.routing_event_id WHERE re.reason LIKE 'Stage 47%'").fetchone()[0]
            logs = conn.execute("SELECT COUNT(*) FROM change_log WHERE change_type LIKE 'Stage 47%' OR comment LIKE 'Synthetic Stage 47%'").fetchone()[0]
        self.assertEqual(3, len(rows)); self.assertEqual(0, links); self.assertEqual(0, logs); self.assertEqual(3, settings)
        active, inactive, manual = rows
        self.assertEqual(("2026-07-18 08:00:00", "campaign_setting", 1, "demo-company-1", "Demo Route", "Demo Route", 1, 0), (active["event_at"], active["apply_scope"], active["is_active"], active["company_id_external"], active["old_route_name"], active["new_route_name"], active["old_company_has_autorotation"], active["new_company_has_autorotation"]))
        self.assertEqual(("2026-07-18 09:00:00", 0, "Synthetic Stage 47 archive"), (inactive["event_at"], inactive["is_active"], inactive["deactivation_reason"])); self.assertIsNotNone(inactive["deactivated_at"]); self.assertIsNotNone(inactive["deactivated_by"])
        self.assertEqual(("2026-07-18 10:00:00", 1, "ci-manual-company", None, None, 1, 0), (manual["event_at"], manual["is_active"], manual["company_id_external"], manual["old_route_name"], manual["new_route_name"], manual["old_company_has_autorotation"], manual["new_company_has_autorotation"]))
        for row in rows:
            self.assertEqual(47, json.loads(row["snapshot_json"])["stage"])

class Stage48MigrationDemoFixtureTests(MigrationDemoSqliteTests):
    def test_stage_48_calling_company_history_fixture_contract(self):
        db_path = self.make_demo_db()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            admin = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
            demo = conn.execute("SELECT * FROM calling_companies WHERE company_id_external='demo-company-1'").fetchone()
            manual = conn.execute("SELECT * FROM calling_companies WHERE company_id_external='ci-manual-company'").fetchone()
            rows = conn.execute("SELECT * FROM change_log WHERE summary LIKE 'Stage 48%' ORDER BY changed_at").fetchall()
            campaign = conn.execute("SELECT id FROM routing_events WHERE reason='Stage 43 campaign setting'").fetchone()["id"]
            routing_count = conn.execute("SELECT COUNT(*) FROM routing_events WHERE reason LIKE 'Stage 48%'").fetchone()[0]
            setting_count = conn.execute("SELECT COUNT(*) FROM company_routing_settings WHERE comment LIKE 'Stage 48%'").fetchone()[0]
        self.assertEqual(3, len(rows)); self.assertEqual(["2026-07-19 08:00:00", "2026-07-19 09:00:00", "2026-07-19 10:00:00"], [r["changed_at"] for r in rows])
        self.assertTrue(all(r["changed_by"] == admin and r["source"] == "ci" and r["created_at"] == r["changed_at"] for r in rows))
        direct, routing, manual_log = rows
        self.assertEqual(("calling_company", demo["id"], "calling_company.updated", "Stage 48 company changed", "Synthetic Stage 48 direct company change log"), (direct["entity_type"], direct["entity_id"], direct["change_type"], direct["summary"], direct["comment"]))
        self.assertEqual({"company_name":"Demo Company", "line_count":1, "comment":"Temporary Stage 48 company comment"}, json.loads(direct["old_values"]))
        self.assertEqual(("routing_event", campaign, "routing_event.created"), (routing["entity_type"], routing["entity_id"], routing["change_type"])); self.assertNotEqual(demo["id"], routing["entity_id"])
        self.assertEqual({"calling_company_id": demo["id"], "routing_mode":"mixed", "stage":48}, json.loads(routing["new_values"]))
        self.assertEqual(("calling_company", manual["id"], "calling_company.updated"), (manual_log["entity_type"], manual_log["entity_id"], manual_log["change_type"]))
        self.assertEqual({"comment":"Temporary Stage 48 manual company comment"}, json.loads(manual_log["old_values"]))
        self.assertEqual(("Demo Company", 2, "Synthetic company", "demo-company-1"), (demo["company_name"], demo["line_count"], demo["comment"], demo["company_id_external"]))
        self.assertEqual(("CI Manual Company", "Synthetic active manual company", "ci-manual-company"), (manual["company_name"], manual["comment"], manual["company_id_external"]))
        self.assertEqual((0, 0), (routing_count, setting_count))

class Stage49MigrationDemoFixtureTests(unittest.TestCase):
    def test_stage_49_change_log_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            conn=sqlite3.connect(create_demo_sqlite(Path(directory)/"demo.db")); conn.row_factory=sqlite3.Row
            try:
                rows=list(conn.execute("SELECT cl.*, u.username FROM change_log cl JOIN users u ON u.id=cl.changed_by WHERE cl.summary LIKE 'Stage 49%' ORDER BY cl.id"))
                self.assertEqual(7, len(rows)); self.assertTrue(all(r["username"]=="admin" and r["source"]=="ci" and r["created_at"]==r["changed_at"] for r in rows))
                values={r["summary"]:r for r in rows}; demo=conn.execute("SELECT * FROM calling_companies WHERE company_name='Demo Company'").fetchone(); manual=conn.execute("SELECT * FROM calling_companies WHERE company_name='CI Manual Company'").fetchone()
                self.assertEqual((2,"Synthetic company","Demo Company"),(demo["line_count"],demo["comment"],demo["company_name"])); self.assertEqual(("Synthetic active manual company","CI Manual Company"),(manual["comment"],manual["company_name"]))
                routing=values["Stage 49 routing beta"]; new=json.loads(routing["new_values"]); old=json.loads(routing["old_values"])
                self.assertEqual("routing_event",routing["entity_type"]); self.assertIsInstance(new["calling_company_id"],str); self.assertEqual(demo["id"],int(new["calling_company_id"])); self.assertEqual(("mixed","campaign_route","routing-old-needle-49","routing-new-needle-49"),(old["routing_mode"],new["routing_mode"],old["marker"],new["marker"]))
                gamma,delta=values["Stage 49 manual gamma"],values["Stage 49 manual delta"]; self.assertEqual(gamma["changed_at"],delta["changed_at"]); self.assertGreater(delta["id"],gamma["id"])
                self.assertEqual("route",values["Stage 49 excluded-route-needle-49"]["entity_type"]); self.assertNotIn("calling_company_id",json.loads(values["Stage 49 orphan-routing-needle-49"]["new_values"]))
                self.assertEqual(0,conn.execute("SELECT COUNT(*) FROM routing_events WHERE created_at LIKE '2026-07-20%'").fetchone()[0]); self.assertEqual(0,conn.execute("SELECT COUNT(*) FROM company_routing_settings WHERE created_at LIKE '2026-07-20%'").fetchone()[0]); self.assertEqual(0,conn.execute("SELECT COUNT(*) FROM routing_event_servers WHERE created_at LIKE '2026-07-20%'").fetchone()[0])
            finally: conn.close()
