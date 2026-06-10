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
        self.assertTrue(event["summary"])
        self.assertIn("GEO: Италия", event["summary"])
        self.assertIn("Сервер: IT1", event["summary"])
        self.assertIn("Старый current route: Италия/Miatel/Pool_A@", event["summary"])
        self.assertIn("Старый provider: Miatel", event["summary"])
        self.assertIn("Новый current route: Италия/Sancom/RND@", event["summary"])
        self.assertIn("Новый provider: Sancom", event["summary"])
        self.assertIn("Previous route после изменения", event["summary"])
        self.assertIn("manual switch", event["summary"])

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
        if company_id is None:
            server_id = overrides.get("server_id") or self.repo.create_server(f"route-srv-{overrides.get('comment', 'default')}")
            company_id = self.create_company(server_id=server_id)
            country_id = self.country_id
        else:
            company = self.conn.execute("SELECT country_id, server_id FROM calling_companies WHERE id = ?", (company_id,)).fetchone()
            country_id = company["country_id"]
            server_id = company["server_id"]
        values = {
            "calling_company_id": company_id,
            "country_id": country_id,
            "server_id": server_id,
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

    def test_company_routing_validates_campaign_geo_server_and_autorotation_flag(self):
        server_id = self.repo.create_server("routing-srv")
        company_id = self.create_company(server_id=server_id)
        other_country_id = self.repo.create_country("Франция", "FR")
        other_server_id = self.repo.create_server("routing-srv-2")

        with self.assertRaisesRegex(BusinessRuleError, "GEO выбранной кампании"):
            self.repo.create_company_routing_setting(calling_company_id=company_id, country_id=other_country_id, server_id=server_id, route_id=None, routing_mode="server_priority", has_autorotation=False, comment=None, created_by=self.admin_id)
        with self.assertRaisesRegex(BusinessRuleError, "сервером выбранной кампании"):
            self.repo.create_company_routing_setting(calling_company_id=company_id, country_id=self.country_id, server_id=other_server_id, route_id=None, routing_mode="server_priority", has_autorotation=False, comment=None, created_by=self.admin_id)
        with self.assertRaisesRegex(BusinessRuleError, "должна быть включена авторотация"):
            self.repo.create_company_routing_setting(calling_company_id=company_id, country_id=self.country_id, server_id=server_id, route_id=None, routing_mode="autorotation", has_autorotation=False, comment=None, created_by=self.admin_id)

        setting_id = self.repo.create_company_routing_setting(calling_company_id=company_id, country_id=self.country_id, server_id=server_id, route_id=None, routing_mode="autorotation", has_autorotation=True, comment=None, created_by=self.admin_id)
        row = self.conn.execute("SELECT routing_mode, has_autorotation FROM company_routing_settings WHERE id = ?", (setting_id,)).fetchone()
        self.assertEqual(row["routing_mode"], "autorotation")
        self.assertEqual(row["has_autorotation"], 1)

    def test_company_routing_filters_by_external_campaign_id(self):
        server_id = self.repo.create_server("routing-srv")
        matching_company_id = self.create_company(server_id=server_id, external_id="campaign-1001")
        other_company_id = self.create_company(external_id="campaign-2002")
        matching_setting_id = self.create_routing_setting(company_id=matching_company_id, server_id=server_id)
        self.repo.update_company_routing_setting(
            setting_id=matching_setting_id,
            country_id=self.country_id,
            server_id=server_id,
            route_id=None,
            routing_mode="mixed",
            has_autorotation=False,
            comment="historical match",
            updated_by=self.admin_id,
        )
        self.create_routing_setting(company_id=other_company_id)

        rows = self.repo.list_company_routing_settings({"company_id_external": "1001"})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company_id_external"], "campaign-1001")

        rows_with_history = self.repo.list_company_routing_settings({"company_id_external": "1001", "show_history": True})
        self.assertEqual(len(rows_with_history), 2)
        self.assertTrue(all(row["company_id_external"] == "campaign-1001" for row in rows_with_history))

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
        setting_id = self.repo.update_company_routing_setting(setting_id=setting_id, country_id=self.country_id, server_id=server_id, route_id=None, routing_mode="server_priority", has_autorotation=True, comment="mode changed", updated_by=self.admin_id)

        versions = self.repo.list_company_routing_settings({"calling_company_id": company_id, "show_history": True})
        self.assertEqual(len(versions), 4)
        self.assertEqual(versions[0]["id"], setting_id)
        self.assertEqual(versions[0]["routing_mode"], "server_priority")
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



class RoutingEventsRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        self.repo = Repository(self.conn)
        self.admin_id = self.repo.create_user("admin", "Admin")
        self.country_id = self.repo.create_country("Мексика", "MEX")
        self.other_country_id = self.repo.create_country("Перу", "PER")
        self.currency_id = self.repo.create_currency("EUR", "Euro", "€")
        self.provider_id = self.repo.create_provider("Sancom", "voip", self.currency_id)
        self.alt_provider_id = self.repo.create_provider("Miatel", "voip", self.currency_id)
        self.route_id = self.repo.create_route(country_id=self.country_id, provider_id=self.provider_id, name="Мексика/Sancom/RND@", cli_source_type="rnd", cli_source_label="RND", created_by=self.admin_id)
        self.alt_route_id = self.repo.create_route(country_id=self.country_id, provider_id=self.alt_provider_id, name="Мексика/Miatel/Pool@", cli_source_type="pool", cli_source_label="Pool", created_by=self.admin_id)
        self.other_route_id = self.repo.create_route(country_id=self.other_country_id, provider_id=self.provider_id, name="Перу/Sancom/RND@", cli_source_type="rnd", cli_source_label="RND", created_by=self.admin_id)
        self.server_id = self.repo.create_server("EU1")
        self.company_id = self.repo.create_calling_company(server_id=self.server_id, country_id=self.country_id, company_name="CC Mexico", company_id_external="1002", has_autorotation=False, created_by=self.admin_id)

    def tearDown(self):
        self.conn.close()

    def create_event(self, **overrides):
        data = dict(event_at="2026-06-10 12:00", apply_scope="none", reason="Другое", comment="Зафиксировали событие", provider_id=self.provider_id, created_by=self.admin_id)
        data.update(overrides)
        return self.repo.create_routing_event(**data)

    def test_can_create_none_without_old_or_new_route(self):
        event_id = self.create_event(apply_scope="none", old_route_id=None, new_route_id=None)
        row = self.conn.execute("SELECT * FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        self.assertEqual(row["apply_scope"], "none")
        self.assertIsNone(row["old_route_id"])
        self.assertIsNone(row["new_route_id"])

    def test_none_scope_ignores_irrelevant_server_and_campaign_fields(self):
        event_id = self.create_event(
            apply_scope="none",
            country_id=self.country_id,
            provider_id=self.provider_id,
            affected_route_id=self.route_id,
            server_id=self.server_id,
            new_route_id=self.alt_route_id,
            calling_company_id=self.company_id,
            company_change_type="set_campaign_route",
            new_company_route_id=self.alt_route_id,
            new_company_has_autorotation=1,
        )
        row = self.conn.execute("SELECT * FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        for field in ("server_id", "old_route_id", "new_route_id", "calling_company_id", "company_change_type", "old_company_routing_mode", "new_company_routing_mode", "old_company_route_id", "new_company_route_id", "old_company_has_autorotation", "new_company_has_autorotation"):
            self.assertIsNone(row[field], field)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM server_route_priorities").fetchone()[0], 0)

    def test_none_scope_summary_contains_provider_and_route_without_server_or_campaign(self):
        event_id = self.create_event(apply_scope="none", country_id=self.country_id, provider_id=self.provider_id, affected_route_id=self.route_id, server_id=self.server_id, calling_company_id=self.company_id)
        log = self.conn.execute("SELECT summary FROM change_log WHERE entity_type = 'routing_event' AND entity_id = ?", (event_id,)).fetchone()[0]
        self.assertIn("Не меняли настройки в нашей системе", log)
        self.assertIn("Sancom", log)
        self.assertIn("Мексика/Sancom/RND@", log)
        self.assertNotIn("Сервер:", log)
        self.assertNotIn("Кампания:", log)

    def test_server_priority_new_route_must_belong_to_provider(self):
        with self.assertRaisesRegex(BusinessRuleError, "новому провайдеру"):
            self.create_event(apply_scope="server_priority", country_id=self.country_id, server_id=self.server_id, provider_id=self.provider_id, new_route_id=self.alt_route_id)

    def test_server_priority_creates_priority_when_missing(self):
        event_id = self.create_event(apply_scope="server_priority", country_id=self.country_id, server_id=self.server_id, new_route_id=self.route_id, provider_id=self.provider_id)
        priority = self.conn.execute("SELECT * FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (self.country_id, self.server_id)).fetchone()
        self.assertIsNotNone(priority)
        self.assertEqual(priority["current_route_id"], self.route_id)
        self.assertIsNone(priority["previous_route_id"])
        self.assertEqual(self.conn.execute("SELECT old_route_id FROM routing_events WHERE id = ?", (event_id,)).fetchone()[0], None)

    def test_server_priority_updates_existing_current_to_previous(self):
        self.conn.execute("INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, changed_by, created_by) VALUES (?, ?, ?, NULL, ?, ?)", (self.country_id, self.server_id, self.route_id, self.admin_id, self.admin_id))
        self.conn.commit()
        self.create_event(apply_scope="server_priority", country_id=self.country_id, server_id=self.server_id, provider_id=self.alt_provider_id, new_route_id=self.alt_route_id)
        priority = self.conn.execute("SELECT * FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (self.country_id, self.server_id)).fetchone()
        self.assertEqual(priority["previous_route_id"], self.route_id)
        self.assertEqual(priority["current_route_id"], self.alt_route_id)

    def test_server_priority_new_route_must_belong_to_geo(self):
        with self.assertRaisesRegex(BusinessRuleError, "выбранному GEO"):
            self.create_event(apply_scope="server_priority", country_id=self.country_id, server_id=self.server_id, provider_id=self.provider_id, new_route_id=self.other_route_id)

    def test_campaign_setting_without_active_setting_uses_server_priority_defaults(self):
        event_id = self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="enable_autorotation", provider_id=None)
        row = self.conn.execute("SELECT * FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        self.assertEqual(row["old_company_routing_mode"], "server_priority")
        self.assertIsNone(row["old_company_route_id"])
        self.assertEqual(row["old_company_has_autorotation"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM company_routing_settings").fetchone()[0], 0)

    def test_campaign_setting_with_active_setting_uses_old_values(self):
        self.repo.create_company_routing_setting(calling_company_id=self.company_id, country_id=self.country_id, server_id=self.server_id, route_id=self.route_id, routing_mode="campaign_route", has_autorotation=True, comment="old", created_by=self.admin_id)
        event_id = self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="change_campaign_route", new_company_route_id=self.alt_route_id, provider_id=None)
        row = self.conn.execute("SELECT * FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        self.assertEqual(row["old_company_routing_mode"], "campaign_route")
        self.assertEqual(row["old_company_route_id"], self.route_id)
        self.assertEqual(row["old_company_has_autorotation"], 1)

    def test_deactivation_does_not_roll_back_server_priority(self):
        event_id = self.create_event(apply_scope="server_priority", country_id=self.country_id, server_id=self.server_id, new_route_id=self.route_id)
        self.repo.deactivate_routing_event(event_id, reason="ошибка записи", deactivated_by=self.admin_id)
        priority = self.conn.execute("SELECT * FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (self.country_id, self.server_id)).fetchone()
        self.assertEqual(priority["current_route_id"], self.route_id)
        self.assertEqual(self.conn.execute("SELECT is_active FROM routing_events WHERE id = ?", (event_id,)).fetchone()[0], 0)

    def test_editing_event_does_not_reapply_server_priority(self):
        event_id = self.create_event(apply_scope="server_priority", country_id=self.country_id, server_id=self.server_id, new_route_id=self.route_id)
        self.repo.update_routing_event(event_id, event_at="2026-06-11 13:00", reason="Другое", comment="Исправили описание", country_id=self.country_id, server_id=self.server_id, provider_id=self.provider_id, affected_route_id=None, old_route_id=None, new_route_id=self.alt_route_id, calling_company_id=None, company_change_type=None, new_company_routing_mode=None, new_company_route_id=None, new_company_has_autorotation=None, updated_by=self.admin_id)
        priority = self.conn.execute("SELECT * FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (self.country_id, self.server_id)).fetchone()
        self.assertEqual(priority["current_route_id"], self.route_id)
        self.assertNotEqual(priority["current_route_id"], self.alt_route_id)

    def test_snapshot_json_is_saved(self):
        event_id = self.create_event(apply_scope="server_priority", country_id=self.country_id, server_id=self.server_id, new_route_id=self.route_id)
        snapshot = self.conn.execute("SELECT snapshot_json FROM routing_events WHERE id = ?", (event_id,)).fetchone()[0]
        self.assertIn("Мексика", snapshot)
        self.assertIn("Sancom", snapshot)


if __name__ == "__main__":
    unittest.main()
