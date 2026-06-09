import sqlite3
import unittest

from app.db import init_db
from app.repository import BusinessRuleError, Repository


class RepositoryBusinessRulesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        self.repo = Repository(self.conn)
        self.admin_id = self.repo.create_user("admin", "Admin")
        self.country_id = self.repo.create_country("Италия", "IT")
        self.currency_id = self.repo.create_currency("EUR", "Euro", "€")
        self.provider_id = self.repo.create_provider("Miatel", "voip", self.currency_id)
        self.route_id = self.repo.create_route(
            country_id=self.country_id,
            provider_id=self.provider_id,
            name="Италия/Miatel/Pool_A@",
            cli_source_type="pool",
            cli_source_label="Pool_A",
            created_by=self.admin_id,
        )

    def tearDown(self):
        self.conn.close()

    def create_phone(self, status="used", is_active=True, number="393331234567"):
        return self.repo.create_phone_number(
            country_id=self.country_id,
            provider_id=self.provider_id,
            number=number,
            assignment_type="pool_number",
            status=status,
            created_by=self.admin_id,
            currency_id=self.currency_id,
            is_active=is_active,
        )

    def test_valid_phone_can_be_added_to_route(self):
        phone_id = self.create_phone()
        result = self.repo.add_phone_to_route(
            route_id=self.route_id,
            phone_number_id=phone_id,
            usage_type="pool_member",
            added_by=self.admin_id,
        )
        self.assertGreater(result.route_phone_number_id, 0)

    def test_disabled_phone_cannot_be_added_to_route(self):
        phone_id = self.create_phone(status="disabled")
        with self.assertRaisesRegex(BusinessRuleError, "Disabled or blocked"):
            self.repo.add_phone_to_route(
                route_id=self.route_id,
                phone_number_id=phone_id,
                usage_type="pool_member",
                added_by=self.admin_id,
            )

    def test_blocked_phone_cannot_be_added_to_route(self):
        phone_id = self.create_phone(status="blocked")
        with self.assertRaisesRegex(BusinessRuleError, "Disabled or blocked"):
            self.repo.add_phone_to_route(
                route_id=self.route_id,
                phone_number_id=phone_id,
                usage_type="pool_member",
                added_by=self.admin_id,
            )

    def test_inactive_phone_cannot_be_added_to_route(self):
        phone_id = self.create_phone(is_active=False)
        with self.assertRaisesRegex(BusinessRuleError, "Inactive"):
            self.repo.add_phone_to_route(
                route_id=self.route_id,
                phone_number_id=phone_id,
                usage_type="pool_member",
                added_by=self.admin_id,
            )

    def test_phone_number_must_use_strict_international_format(self):
        invalid_numbers = ["+393331234567", "00393331234567", "393 331 234567", "(393)331234567"]
        for invalid in invalid_numbers:
            with self.subTest(invalid=invalid):
                with self.assertRaises(BusinessRuleError):
                    self.create_phone(number=invalid)

    def test_calling_company_requires_external_id_and_unique_business_key(self):
        server_id = self.repo.create_server("EU1")
        with self.assertRaises(BusinessRuleError):
            self.repo.create_calling_company(
                server_id=server_id,
                country_id=self.country_id,
                company_name="CC Italy",
                company_id_external=" ",
                has_autorotation=False,
                created_by=self.admin_id,
            )
        self.repo.create_calling_company(
            server_id=server_id,
            country_id=self.country_id,
            company_name="CC Italy",
            company_id_external="123",
            has_autorotation=True,
            created_by=self.admin_id,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.create_calling_company(
                server_id=server_id,
                country_id=self.country_id,
                company_name="CC Italy Duplicate",
                company_id_external="123",
                has_autorotation=False,
                created_by=self.admin_id,
            )

    def test_route_unique_by_country_and_name(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.create_route(
                country_id=self.country_id,
                provider_id=self.provider_id,
                name="Италия/Miatel/Pool_A@",
                cli_source_type="pool",
                cli_source_label="Pool_A",
                created_by=self.admin_id,
            )


    def create_priority(self, current_route_id: int | None = None, previous_route_id: int | None = None) -> tuple[int, int]:
        server_id = self.repo.create_server("IT1")
        cur = self.conn.execute(
            """
            INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, changed_by, created_by, comment)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (self.country_id, server_id, current_route_id or self.route_id, previous_route_id, self.admin_id, self.admin_id, "initial"),
        )
        self.conn.commit()
        return int(cur.lastrowid), server_id

    def test_manual_server_priority_route_change_moves_current_to_previous_and_logs_event(self):
        priority_id, _ = self.create_priority()
        alt_provider_id = self.repo.create_provider("Sancom", "voip", self.currency_id)
        alt_route_id = self.repo.create_route(
            country_id=self.country_id,
            provider_id=alt_provider_id,
            name="Италия/Sancom/RND@",
            cli_source_type="rnd",
            cli_source_label="RND",
            created_by=self.admin_id,
        )

        self.repo.update_server_route_priority(
            priority_id=priority_id,
            current_route_id=alt_route_id,
            comment="manual switch",
            changed_by=self.admin_id,
        )

        row = self.conn.execute("SELECT * FROM server_route_priorities WHERE id = ?", (priority_id,)).fetchone()
        self.assertEqual(row["current_route_id"], alt_route_id)
        self.assertEqual(row["previous_route_id"], self.route_id)
        self.assertEqual(row["comment"], "manual switch")
        self.assertEqual(row["changed_by"], self.admin_id)
        self.assertEqual(row["updated_by"], self.admin_id)
        self.assertIsNotNone(row["changed_at"])
        event = self.conn.execute(
            "SELECT * FROM change_log WHERE entity_type = 'server_route_priority' AND entity_id = ? AND change_type = 'server_route_priority.current_route_updated'",
            (priority_id,),
        ).fetchone()
        self.assertIsNotNone(event)
        self.assertIn(str(alt_route_id), event["new_values"])

    def test_same_server_priority_route_keeps_previous_route_and_updates_comment(self):
        alt_provider_id = self.repo.create_provider("Sancom", "voip", self.currency_id)
        previous_route_id = self.repo.create_route(
            country_id=self.country_id,
            provider_id=alt_provider_id,
            name="Италия/Sancom/RND@",
            cli_source_type="rnd",
            cli_source_label="RND",
            created_by=self.admin_id,
        )
        priority_id, _ = self.create_priority(previous_route_id=previous_route_id)

        self.repo.update_server_route_priority(
            priority_id=priority_id,
            current_route_id=self.route_id,
            comment="comment only",
            changed_by=self.admin_id,
        )

        row = self.conn.execute("SELECT * FROM server_route_priorities WHERE id = ?", (priority_id,)).fetchone()
        self.assertEqual(row["current_route_id"], self.route_id)
        self.assertEqual(row["previous_route_id"], previous_route_id)
        self.assertEqual(row["comment"], "comment only")

    def test_server_priority_unique_by_country_and_server(self):
        _, server_id = self.create_priority()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO server_route_priorities(country_id, server_id, current_route_id, changed_by, created_by)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self.country_id, server_id, self.route_id, self.admin_id, self.admin_id),
            )
            self.conn.commit()



    def create_company(self, *, server_id: int | None = None, country_id: int | None = None, external_id: str = "cc-1") -> int:
        return self.repo.create_calling_company(
            server_id=server_id or self.repo.create_server(f"srv-{external_id}"),
            country_id=country_id or self.country_id,
            company_name=f"Company {external_id}",
            company_id_external=external_id,
            has_autorotation=False,
            created_by=self.admin_id,
        )

    def create_routing_setting(self, company_id: int | None = None, **overrides) -> int:
        values = {
            "calling_company_id": company_id or self.create_company(),
            "country_id": self.country_id,
            "server_id": self.repo.create_server("route-srv"),
            "route_id": None,
            "routing_mode": "server_priority",
            "has_autorotation": False,
            "comment": "initial routing",
            "created_by": self.admin_id,
        }
        values.update(overrides)
        return self.repo.create_company_routing_setting(**values)

    def test_create_first_company_routing_setting_and_logs_event(self):
        server_id = self.repo.create_server("routing-srv")
        company_id = self.create_company(server_id=server_id)
        setting_id = self.repo.create_company_routing_setting(
            calling_company_id=company_id,
            country_id=self.country_id,
            server_id=server_id,
            route_id=None,
            routing_mode="server_priority",
            has_autorotation=False,
            comment="uses server priority",
            created_by=self.admin_id,
        )

        row = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (setting_id,)).fetchone()
        self.assertEqual(row["calling_company_id"], company_id)
        self.assertEqual(row["routing_mode"], "server_priority")
        self.assertIsNone(row["route_id"])
        self.assertEqual(row["is_active"], 1)
        self.assertIsNone(row["valid_to"])
        self.assertIsNotNone(row["valid_from"])
        event = self.conn.execute("SELECT * FROM change_log WHERE entity_type = 'company_routing_setting' AND entity_id = ?", (setting_id,)).fetchone()
        self.assertEqual(event["change_type"], "company_routing_setting.created")
        self.assertIn("GEO", event["summary"])

    def test_company_routing_validates_company_and_route_geo(self):
        server_id = self.repo.create_server("routing-srv")
        with self.assertRaisesRegex(BusinessRuleError, "Кампания прозвона не найдена"):
            self.repo.create_company_routing_setting(calling_company_id=999, country_id=self.country_id, server_id=server_id, route_id=None, routing_mode="server_priority", has_autorotation=False, comment=None, created_by=self.admin_id)
        other_country_id = self.repo.create_country("Франция", "FR")
        other_route_id = self.repo.create_route(country_id=other_country_id, provider_id=self.provider_id, name="Франция/Miatel/Pool_A@", cli_source_type="pool", cli_source_label="Pool_A", created_by=self.admin_id)
        company_id = self.create_company(server_id=server_id)
        with self.assertRaisesRegex(BusinessRuleError, "выбранному GEO"):
            self.repo.create_company_routing_setting(calling_company_id=company_id, country_id=self.country_id, server_id=server_id, route_id=other_route_id, routing_mode="campaign_route", has_autorotation=False, comment=None, created_by=self.admin_id)

    def test_company_routing_prevents_second_active_setting_for_company(self):
        server_id = self.repo.create_server("routing-srv")
        company_id = self.create_company(server_id=server_id)
        self.repo.create_company_routing_setting(calling_company_id=company_id, country_id=self.country_id, server_id=server_id, route_id=None, routing_mode="server_priority", has_autorotation=False, comment=None, created_by=self.admin_id)
        with self.assertRaisesRegex(BusinessRuleError, "уже есть активная"):
            self.repo.create_company_routing_setting(calling_company_id=company_id, country_id=self.country_id, server_id=server_id, route_id=None, routing_mode="autorotation", has_autorotation=True, comment=None, created_by=self.admin_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO company_routing_settings(calling_company_id, country_id, server_id, routing_mode, has_autorotation, created_by)
                VALUES (?, ?, ?, 'mixed', 0, ?)
                """,
                (company_id, self.country_id, server_id, self.admin_id),
            )
            self.conn.commit()

    def test_company_routing_mode_route_requirements(self):
        for mode in ("server_priority", "autorotation", "mixed"):
            with self.subTest(mode=mode):
                server_id = self.repo.create_server(f"srv-{mode}")
                company_id = self.create_company(server_id=server_id, external_id=mode)
                setting_id = self.repo.create_company_routing_setting(calling_company_id=company_id, country_id=self.country_id, server_id=server_id, route_id=None, routing_mode=mode, has_autorotation=mode == "autorotation", comment=None, created_by=self.admin_id)
                self.assertGreater(setting_id, 0)
        server_id = self.repo.create_server("campaign-route-srv")
        company_id = self.create_company(server_id=server_id, external_id="campaign-route")
        with self.assertRaisesRegex(BusinessRuleError, "campaign_route"):
            self.repo.create_company_routing_setting(calling_company_id=company_id, country_id=self.country_id, server_id=server_id, route_id=None, routing_mode="campaign_route", has_autorotation=False, comment=None, created_by=self.admin_id)
        setting_id = self.repo.create_company_routing_setting(calling_company_id=company_id, country_id=self.country_id, server_id=server_id, route_id=self.route_id, routing_mode="campaign_route", has_autorotation=False, comment=None, created_by=self.admin_id)
        self.assertGreater(setting_id, 0)

    def test_company_routing_mode_change_versions_history_and_logs(self):
        server_id = self.repo.create_server("routing-srv")
        company_id = self.create_company(server_id=server_id)
        old_id = self.repo.create_company_routing_setting(calling_company_id=company_id, country_id=self.country_id, server_id=server_id, route_id=None, routing_mode="autorotation", has_autorotation=True, comment="old", created_by=self.admin_id)
        new_id = self.repo.update_company_routing_setting(setting_id=old_id, country_id=self.country_id, server_id=server_id, route_id=None, routing_mode="server_priority", has_autorotation=False, comment="new", updated_by=self.admin_id)

        old_row = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (old_id,)).fetchone()
        new_row = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (new_id,)).fetchone()
        self.assertEqual(old_row["is_active"], 0)
        self.assertIsNotNone(old_row["valid_to"])
        self.assertEqual(new_row["is_active"], 1)
        self.assertIsNone(new_row["valid_to"])
        self.assertEqual(new_row["routing_mode"], "server_priority")
        self.assertEqual(len(self.repo.list_company_routing_settings({"calling_company_id": company_id})), 1)
        self.assertEqual(len(self.repo.list_company_routing_settings({"calling_company_id": company_id, "show_history": True})), 2)
        event = self.conn.execute("SELECT * FROM change_log WHERE entity_type = 'company_routing_setting' AND entity_id = ?", (new_id,)).fetchone()
        self.assertEqual(event["change_type"], "company_routing_setting.version_created")
        self.assertIn("valid_to старой версии", event["summary"])

    def test_company_routing_route_autorotation_country_and_server_changes_create_versions(self):
        server_id = self.repo.create_server("routing-srv")
        company_id = self.create_company(server_id=server_id)
        alt_route_id = self.repo.create_route(country_id=self.country_id, provider_id=self.provider_id, name="Италия/Miatel/Pool_B@", cli_source_type="pool", cli_source_label="Pool_B", created_by=self.admin_id)
        setting_id = self.repo.create_company_routing_setting(calling_company_id=company_id, country_id=self.country_id, server_id=server_id, route_id=self.route_id, routing_mode="campaign_route", has_autorotation=False, comment="v1", created_by=self.admin_id)
        setting_id = self.repo.update_company_routing_setting(setting_id=setting_id, country_id=self.country_id, server_id=server_id, route_id=alt_route_id, routing_mode="campaign_route", has_autorotation=False, comment="route changed", updated_by=self.admin_id)
        setting_id = self.repo.update_company_routing_setting(setting_id=setting_id, country_id=self.country_id, server_id=server_id, route_id=alt_route_id, routing_mode="campaign_route", has_autorotation=True, comment="autorotation changed", updated_by=self.admin_id)
        other_country_id = self.repo.create_country("Испания", "ES")
        other_server_id = self.repo.create_server("routing-srv-2")
        setting_id = self.repo.update_company_routing_setting(setting_id=setting_id, country_id=other_country_id, server_id=other_server_id, route_id=None, routing_mode="server_priority", has_autorotation=True, comment="geo server changed", updated_by=self.admin_id)

        versions = self.repo.list_company_routing_settings({"calling_company_id": company_id, "show_history": True})
        self.assertEqual(len(versions), 4)
        self.assertEqual(versions[0]["id"], setting_id)
        self.assertEqual(versions[0]["country_id"], other_country_id)
        self.assertEqual(versions[0]["server_id"], other_server_id)
        self.assertTrue(all(row["valid_to"] is not None for row in versions[1:]))

    def test_company_routing_comment_only_update_does_not_create_new_version_but_logs(self):
        setting_id = self.create_routing_setting(comment="before")
        returned_id = self.repo.update_company_routing_setting(setting_id=setting_id, country_id=self.country_id, server_id=self.conn.execute("SELECT server_id FROM company_routing_settings WHERE id = ?", (setting_id,)).fetchone()["server_id"], route_id=None, routing_mode="server_priority", has_autorotation=False, comment="after", updated_by=self.admin_id)

        self.assertEqual(returned_id, setting_id)
        rows = self.conn.execute("SELECT * FROM company_routing_settings").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["comment"], "after")
        event = self.conn.execute("SELECT * FROM change_log WHERE entity_type = 'company_routing_setting' AND entity_id = ? ORDER BY id DESC", (setting_id,)).fetchone()
        self.assertEqual(event["change_type"], "company_routing_setting.updated")

    def test_company_routing_deactivation_closes_active_version_without_new_version_and_logs(self):
        setting_id = self.create_routing_setting()
        self.repo.deactivate_company_routing_setting(setting_id=setting_id, updated_by=self.admin_id)

        row = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (setting_id,)).fetchone()
        self.assertEqual(row["is_active"], 0)
        self.assertIsNotNone(row["valid_to"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM company_routing_settings").fetchone()[0], 1)
        event = self.conn.execute("SELECT * FROM change_log WHERE entity_type = 'company_routing_setting' AND entity_id = ? ORDER BY id DESC", (setting_id,)).fetchone()
        self.assertEqual(event["change_type"], "company_routing_setting.deactivated")



if __name__ == "__main__":
    unittest.main()
