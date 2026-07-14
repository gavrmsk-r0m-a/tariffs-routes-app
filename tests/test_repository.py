import json
import sqlite3
import unittest
from decimal import Decimal

from app.db import init_db, run_lightweight_migrations
from app.repository import hash_password, verify_password, BusinessRuleError, ConcurrencyConflict, Repository, _values_equal


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
            assignment_type="gl",
            status=status,
            created_by=self.admin_id,
            currency_id=self.currency_id,
            is_active=is_active,
        )


    def _create_basic_routing_event(self, comment="initial"):
        return self.repo.create_routing_event(
            event_at="2026-06-22T12:00",
            apply_scope="none",
            reason="Провайдер сменил маршрут",
            country_id=self.country_id,
            provider_id=self.provider_id,
            affected_route_id=self.route_id,
            comment=comment,
            created_by=self.admin_id,
        )





    def test_repository_new_lookup_methods(self):
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Lookup Project', 0)")
        self.conn.execute("INSERT INTO phone_number_types(name, is_active) VALUES ('Lookup Type', 1)")
        self.conn.execute("INSERT INTO phone_assignment_types(code, name, is_active) VALUES ('lookup_code', 'Lookup Assignment', 0)")
        server_id = self.repo.create_server("Lookup Server")
        self.conn.commit()

        self.assertEqual(self.repo.get_country_by_name("Италия")["id"], self.country_id)
        self.assertEqual(self.repo.get_provider_by_normalized_name("miatel")["id"], self.provider_id)
        self.assertEqual(self.repo.get_currency_by_code("EUR")["id"], self.currency_id)
        self.assertEqual(self.repo.get_project_by_name("Lookup Project")["is_active"], 0)
        self.assertEqual(self.repo.get_phone_number_type_by_name("Lookup Type")["name"], "Lookup Type")
        self.assertEqual(self.repo.get_phone_assignment_type_by_code_or_name("Lookup Assignment")["code"], "lookup_code")
        self.assertEqual(self.repo.get_server_by_name("Lookup Server")["id"], server_id)
        self.assertIsNone(self.repo.get_currency_by_code("ZZZ"))

    def test_repository_importer_exists_methods_return_bools(self):
        phone_id = self.create_phone(number="393331239099")
        server_id = self.repo.create_server("Exists Server")
        self.repo.create_calling_company(
            server_id=server_id,
            country_id=self.country_id,
            company_name="Exists Company",
            company_id_external="ext-100",
            has_autorotation=False,
            created_by=self.admin_id,
        )
        self.repo.create_tariff(
            country_id=self.country_id,
            provider_id=self.provider_id,
            provider_currency_id=self.currency_id,
            price_in_provider_currency="0.10",
            conversion_rate_to_eur="1",
            conversion_rate_date="2026-07-14",
            created_by=self.admin_id,
        )

        self.assertIsInstance(self.repo.route_exists_by_country_name_and_name("Италия", "Италия/Miatel/Pool_A@"), bool)
        self.assertTrue(self.repo.route_exists_by_country_name_and_name("Италия", "Италия/Miatel/Pool_A@"))
        self.assertFalse(self.repo.route_exists_by_country_name_and_name("Италия", "Missing"))
        self.assertTrue(self.repo.phone_number_exists_by_normalized_number("393331239099"))
        self.assertFalse(self.repo.phone_number_exists_by_normalized_number("393331239098"))
        self.assertTrue(self.repo.calling_company_exists_by_server_country_external_id("Exists Server", "Италия", "ext-100"))
        self.assertFalse(self.repo.calling_company_exists_by_server_country_external_id("Exists Server", "Италия", "missing"))
        self.assertTrue(self.repo.current_tariff_exists_by_country_provider_prefix("Италия", "Miatel", None))
        self.assertFalse(self.repo.current_tariff_exists_by_country_provider_prefix("Италия", "Miatel", "999"))
        self.assertIsInstance(phone_id, int)

    def test_get_user_permissions_returns_existing_permissions(self):
        user_id = self.repo.create_user("permissions-user", "operator", "Permissions User")
        self.conn.execute(
            "INSERT INTO user_permissions(user_id, section_key, can_read, can_write, can_export) VALUES (?, ?, ?, ?, ?)",
            (user_id, "routes", 1, 0, 1),
        )
        self.conn.execute(
            "INSERT INTO user_permissions(user_id, section_key, can_read, can_write, can_export) VALUES (?, ?, ?, ?, ?)",
            (user_id, "tariffs", 1, 1, 0),
        )
        self.conn.commit()

        permissions = self.repo.get_user_permissions(user_id)

        self.assertEqual(permissions["routes"]["can_read"], 1)
        self.assertEqual(permissions["routes"]["can_write"], 0)
        self.assertEqual(permissions["routes"]["can_export"], 1)
        self.assertEqual(permissions["tariffs"]["can_read"], 1)
        self.assertEqual(permissions["tariffs"]["can_write"], 1)
        self.assertEqual(permissions["tariffs"]["can_export"], 0)

    def test_set_user_permissions_inserts_permissions(self):
        user_id = self.repo.create_user("insert-permissions", "operator", "Insert Permissions")

        self.repo.set_user_permissions(user_id, {"routes": {"can_read": True, "can_write": False, "can_export": True}})

        row = self.conn.execute(
            "SELECT can_read, can_write, can_export FROM user_permissions WHERE user_id = ? AND section_key = ?",
            (user_id, "routes"),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(dict(row), {"can_read": 1, "can_write": 0, "can_export": 1})

    def test_set_user_permissions_updates_existing_permissions(self):
        user_id = self.repo.create_user("update-permissions", "operator", "Update Permissions")
        self.repo.set_user_permissions(user_id, {"routes": {"can_read": True, "can_write": False, "can_export": False}})

        self.repo.set_user_permissions(user_id, {"routes": {"can_read": True, "can_write": True, "can_export": False}})

        rows = self.conn.execute(
            "SELECT can_read, can_write, can_export FROM user_permissions WHERE user_id = ? AND section_key = ?",
            (user_id, "routes"),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(dict(rows[0]), {"can_read": 1, "can_write": 1, "can_export": 0})

    def test_set_user_permissions_preserves_current_checkbox_semantics(self):
        user_id = self.repo.create_user("checkbox-permissions", "operator", "Checkbox Permissions")

        self.repo.set_user_permissions(user_id, {"routes": {"can_read": True}})

        row = self.conn.execute(
            "SELECT can_read, can_write, can_export FROM user_permissions WHERE user_id = ? AND section_key = ?",
            (user_id, "routes"),
        ).fetchone()
        self.assertEqual(dict(row), {"can_read": 1, "can_write": 0, "can_export": 0})

    def test_repository_transaction_commits_on_success(self):
        usd_id = self.repo.create_currency("USD", "US Dollar", "$")

        with self.repo.transaction():
            rate_id = self.repo.create_currency_rate(
                usd_id,
                "0.91",
                "2026-07-10",
                self.admin_id,
                commit=False,
            )

        row = self.conn.execute(
            "SELECT id, rate_to_eur FROM currency_rates WHERE id = ?",
            (rate_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(str(row["rate_to_eur"]), "0.91")

    def test_repository_transaction_rolls_back_on_error(self):
        usd_id = self.repo.create_currency("USD", "US Dollar", "$")

        with self.assertRaises(RuntimeError):
            with self.repo.transaction():
                self.repo.create_currency_rate(
                    usd_id,
                    "0.91",
                    "2026-07-10",
                    self.admin_id,
                    commit=False,
                )
                raise RuntimeError("force rollback")

        count = self.conn.execute(
            "SELECT COUNT(*) AS count FROM currency_rates WHERE currency_id = ?",
            (usd_id,),
        ).fetchone()["count"]
        self.assertEqual(count, 0)

    def test_create_currency_rate_is_append_only(self):
        usd_id = self.repo.create_currency("USD", "US Dollar", "$")

        first_id = self.repo.create_currency_rate(usd_id, "0.91", "2026-07-10", self.admin_id)
        second_id = self.repo.create_currency_rate(usd_id, "0.92", "2026-07-10", self.admin_id)

        rows = self.conn.execute(
            "SELECT id, rate_to_eur FROM currency_rates WHERE currency_id = ? ORDER BY id",
            (usd_id,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], first_id)
        self.assertEqual(str(rows[0]["rate_to_eur"]), "0.91")
        self.assertEqual(rows[1]["id"], second_id)
        self.assertEqual(str(rows[1]["rate_to_eur"]), "0.92")
        latest = self.repo.latest_currency_rate(usd_id)
        self.assertEqual(latest["id"], second_id)
        self.assertEqual(str(latest["rate_to_eur"]), "0.92")

        for invalid in ("", "0", "-1", "abc"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(BusinessRuleError):
                    self.repo.create_currency_rate(usd_id, invalid, "2026-07-10", self.admin_id)

        count_after_invalid = self.conn.execute(
            "SELECT COUNT(*) AS count FROM currency_rates WHERE currency_id = ?",
            (usd_id,),
        ).fetchone()["count"]
        self.assertEqual(count_after_invalid, 2)


    def test_currency_rate_recalculates_current_tariffs(self):
        usdt_id = self.repo.create_currency("USDT", "Tether", "₮")
        first_rate_id = self.repo.create_currency_rate(usdt_id, "500", "2026-07-10", self.admin_id)
        tariff_id = self.repo.create_tariff(
            country_id=self.country_id, provider_id=self.provider_id, provider_currency_id=usdt_id,
            price_in_provider_currency="3", conversion_rate_to_eur="500", conversion_rate_date="2026-07-10",
            currency_rate_id=first_rate_id, created_by=self.admin_id,
        )
        new_rate_id = self.repo.create_currency_rate(usdt_id, "300", "2026-07-11", self.admin_id, commit=False)

        recalculated = self.repo.recalculate_current_tariffs_for_currency_rate(new_rate_id, self.admin_id)

        self.assertEqual(len(recalculated), 1)
        tariff = self.conn.execute("SELECT * FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()
        self.assertEqual(str(tariff["eur_price"]), "900")
        self.assertEqual(tariff["currency_rate_id"], new_rate_id)
        self.assertEqual(str(tariff["conversion_rate_to_eur"]), "300")
        self.assertEqual(tariff["conversion_rate_date"], "2026-07-11")

    def test_currency_rate_recalculation_skips_inactive_tariffs(self):
        usdt_id = self.repo.create_currency("USDT", "Tether", "₮")
        first_rate_id = self.repo.create_currency_rate(usdt_id, "500", "2026-07-10", self.admin_id)
        tariff_id = self.repo.create_tariff(
            country_id=self.country_id, provider_id=self.provider_id, provider_currency_id=usdt_id,
            price_in_provider_currency="3", conversion_rate_to_eur="500", conversion_rate_date="2026-07-10",
            currency_rate_id=first_rate_id, created_by=self.admin_id,
        )
        self.repo.set_tariff_active(tariff_id, is_current=False, changed_by=self.admin_id)
        before = self.conn.execute("SELECT * FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()
        new_rate_id = self.repo.create_currency_rate(usdt_id, "300", "2026-07-11", self.admin_id, commit=False)

        recalculated = self.repo.recalculate_current_tariffs_for_currency_rate(new_rate_id, self.admin_id)

        self.assertEqual(recalculated, [])
        after = self.conn.execute("SELECT * FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()
        self.assertEqual(str(after["eur_price"]), str(before["eur_price"]))
        self.assertEqual(after["currency_rate_id"], before["currency_rate_id"])
        self.assertEqual(str(after["conversion_rate_to_eur"]), str(before["conversion_rate_to_eur"]))

    def test_currency_rate_recalculation_writes_tariff_history(self):
        usdt_id = self.repo.create_currency("USDT", "Tether", "₮")
        first_rate_id = self.repo.create_currency_rate(usdt_id, "500", "2026-07-10", self.admin_id)
        tariff_id = self.repo.create_tariff(
            country_id=self.country_id, provider_id=self.provider_id, provider_currency_id=usdt_id,
            price_in_provider_currency="3", conversion_rate_to_eur="500", conversion_rate_date="2026-07-10",
            currency_rate_id=first_rate_id, created_by=self.admin_id,
        )
        new_rate_id = self.repo.create_currency_rate(usdt_id, "300", "2026-07-11", self.admin_id, commit=False)

        self.repo.recalculate_current_tariffs_for_currency_rate(new_rate_id, self.admin_id)

        history = self.conn.execute("SELECT * FROM tariff_change_history WHERE tariff_id = ? ORDER BY id DESC LIMIT 1", (tariff_id,)).fetchone()
        self.assertEqual(history["reason"], "tariff.currency_rate_recalculated")
        self.assertEqual(str(history["old_conversion_rate_to_eur"]), "500")
        self.assertEqual(str(history["new_conversion_rate_to_eur"]), "300")
        self.assertEqual(str(history["old_eur_price"]), "1500")
        self.assertEqual(str(history["new_eur_price"]), "900")
        self.assertIn("currency_rate_id", history["comment"])

    def test_currency_rate_recalculation_writes_change_log(self):
        usdt_id = self.repo.create_currency("USDT", "Tether", "₮")
        old_rate_id = self.repo.create_currency_rate(usdt_id, "500", "2026-07-10", self.admin_id)
        self.repo.create_tariff(
            country_id=self.country_id, provider_id=self.provider_id, provider_currency_id=usdt_id,
            price_in_provider_currency="3", conversion_rate_to_eur="500", conversion_rate_date="2026-07-10",
            currency_rate_id=old_rate_id, created_by=self.admin_id,
        )
        old_rate = self.repo.get_currency_rate(old_rate_id)
        new_rate_id = self.repo.create_currency_rate(usdt_id, "300", "2026-07-11", self.admin_id, commit=False)
        new_rate = self.repo.get_currency_rate(new_rate_id)
        recalculated = self.repo.recalculate_current_tariffs_for_currency_rate(new_rate_id, self.admin_id)
        self.repo.log_currency_rate_change(new_rate_id, usdt_id, "USDT", old_rate, new_rate, self.admin_id, recalculated_active_tariffs_count=len(recalculated))

        currency_log = self.conn.execute("SELECT * FROM change_log WHERE entity_type = 'currency_rate' ORDER BY id DESC LIMIT 1").fetchone()
        tariff_log = self.conn.execute("SELECT * FROM change_log WHERE entity_type = 'tariff' AND change_type = 'tariff.currency_rate_recalculated' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(json.loads(currency_log["new_values"])["recalculated_active_tariffs_count"], 1)
        self.assertIn("Активных тарифов пересчитано: 1", currency_log["summary"])
        self.assertIsNotNone(tariff_log)
        self.assertEqual(json.loads(tariff_log["old_values"])["currency_rate_id"], old_rate_id)
        self.assertEqual(json.loads(tariff_log["new_values"])["currency_rate_id"], new_rate_id)
        self.assertEqual(Decimal(json.loads(tariff_log["new_values"])["eur_price"]), Decimal("900"))

    def test_dictionary_rename_without_updating_linked_records_keeps_phone_snapshots(self):
        phone_id = self.create_phone(number="393331234570")
        before = self.repo.list_phone_numbers({"number_like": "393331234570"})[0]
        self.assertEqual(before["provider_name"], "Miatel")
        self.assertEqual(before["assignment_type_label"], "ГЛ")

        self.conn.execute("UPDATE providers SET name = ?, normalized_name = ? WHERE id = ?", ("Miatel New", "miatel new", self.provider_id))
        assignment_id = self.conn.execute("SELECT id FROM phone_assignment_types WHERE code = 'gl'").fetchone()["id"]
        self.conn.execute("UPDATE phone_assignment_types SET name = ? WHERE id = ?", ("ГЛ NEW", assignment_id))
        self.conn.commit()

        after = self.repo.list_phone_numbers({"number_like": "393331234570"})[0]
        self.assertEqual(after["provider_name"], "Miatel")
        self.assertEqual(after["assignment_type_label"], "ГЛ")

        new_phone_id = self.repo.create_phone_number(
            country_id=self.country_id, provider_id=self.provider_id, number="393331234571",
            assignment_type="gl", status="used", created_by=self.admin_id, currency_id=self.currency_id,
        )
        new_row = self.repo.list_phone_numbers({"number_like": "393331234571"})[0]
        self.assertEqual(new_row["provider_name"], "Miatel New")
        self.assertEqual(new_row["assignment_type_label"], "ГЛ NEW")

    def test_dictionary_rename_with_updating_linked_records_refreshes_phone_snapshots(self):
        self.create_phone(number="393331234572")
        old = self.conn.execute("SELECT name FROM providers WHERE id = ?", (self.provider_id,)).fetchone()["name"]
        self.conn.execute("UPDATE providers SET name = ?, normalized_name = ? WHERE id = ?", ("Miatel Fixed", "miatel fixed", self.provider_id))
        counts = self.repo.update_dictionary_snapshots("providers", self.provider_id, old, "Miatel Fixed")
        self.conn.commit()

        row = self.repo.list_phone_numbers({"number_like": "393331234572"})[0]
        self.assertEqual(row["provider_name"], "Miatel Fixed")
        self.assertEqual(counts["Купленные номера"], 1)

    def test_inactive_dictionary_rename_does_not_reactivate_value(self):
        project_id = self.conn.execute("SELECT id FROM projects WHERE name = 'ИТМ'").fetchone()["id"]
        self.conn.execute("UPDATE projects SET is_active = 0 WHERE id = ?", (project_id,))
        self.conn.commit()
        self.repo.create_phone_number(
            country_id=self.country_id, provider_id=self.provider_id, number="393331234573",
            assignment_type="gl", status="used", created_by=self.admin_id, project_label="ИТМ",
        )

        self.conn.execute("UPDATE projects SET name = ?, is_active = 0 WHERE id = ?", ("ИТМ legacy", project_id))
        self.conn.commit()

        phone = self.repo.list_phone_numbers({"number_like": "393331234573"})[0]
        project = self.conn.execute("SELECT is_active FROM projects WHERE id = ?", (project_id,)).fetchone()
        self.assertEqual(phone["project_label"], "ИТМ")
        self.assertEqual(project["is_active"], 0)

    def test_routing_event_created_with_updated_at(self):
        event_id = self._create_basic_routing_event()
        updated_at = self.conn.execute("SELECT updated_at FROM routing_events WHERE id = ?", (event_id,)).fetchone()["updated_at"]
        self.assertTrue(updated_at)

    def test_routing_event_update_changes_updated_at(self):
        event_id = self._create_basic_routing_event()
        original = self.conn.execute("SELECT updated_at FROM routing_events WHERE id = ?", (event_id,)).fetchone()["updated_at"]
        self.repo.update_routing_event(event_id, comment="updated", updated_at_original=original, updated_by=self.admin_id)
        row = self.conn.execute("SELECT comment, updated_at FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        self.assertEqual(row["comment"], "updated")
        self.assertNotEqual(row["updated_at"], original)

    def test_routing_event_stale_updated_at_does_not_save(self):
        event_id = self._create_basic_routing_event()
        original = self.conn.execute("SELECT updated_at FROM routing_events WHERE id = ?", (event_id,)).fetchone()["updated_at"]
        self.repo.update_routing_event(event_id, comment="other user", updated_at_original=original, updated_by=self.admin_id)
        with self.assertRaisesRegex(BusinessRuleError, "Запись была изменена другим пользователем"):
            self.repo.update_routing_event(event_id, comment="stale save", updated_at_original=original, updated_by=self.admin_id)
        row = self.conn.execute("SELECT comment FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        self.assertEqual(row["comment"], "other user")

    def test_existing_admin_without_password_gets_default_hashed_password(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(conn)
        conn.execute("UPDATE users SET password_hash = NULL, password_salt = NULL WHERE username = 'admin'")
        run_lightweight_migrations(conn)
        row = conn.execute("SELECT password_hash, password_salt FROM users WHERE username = 'admin'").fetchone()
        self.assertIsNotNone(row["password_hash"])
        self.assertIsNotNone(row["password_salt"])
        self.assertNotEqual(row["password_hash"], "admin")
        self.assertNotEqual(row["password_salt"], "admin")
        self.assertTrue(verify_password("admin", row["password_hash"], row["password_salt"]))
        conn.close()

    def test_reference_defaults_seed_current_projects_and_assignments(self):
        projects = [tuple(row) for row in self.conn.execute(
            "SELECT code, name, is_active, sort_order, include_in_route_name FROM projects WHERE is_active = 1 ORDER BY sort_order"
        )]
        self.assertEqual(projects, [
            ("mezhdep", "Меж.деп.", 1, 1, 0),
            ("rep", "REP", 1, 2, 1),
            ("itm", "ИТМ", 1, 3, 1),
            ("prepayment", "Предоплата", 1, 4, 1),
            ("legal", "Юр.деп.", 1, 5, 1),
        ])
        assignments = [tuple(row) for row in self.conn.execute(
            """
            SELECT code, name, is_active, sort_order FROM phone_assignment_types
            WHERE is_active = 1
            ORDER BY sort_order
            """
        )]
        self.assertEqual(assignments, [
            ("gl", "ГЛ", 1, 1),
            ("aon", "АОН", 1, 2),
            ("scratchcards", "Scratchcards", 1, 3),
            ("competitors", "Competitors", 1, 4),
            ("sms", "SMS", 1, 5),
            ("corporate_telephony", "Корп.телефония", 1, 6),
            ("dozhim", "Дожим", 1, 7),
            ("ivr", "IVR", 1, 8),
        ])
        obsolete_count = self.conn.execute(
            """
            SELECT COUNT(*) FROM phone_assignment_types
            WHERE code IN ('outgoing_cli', 'inbound_line', 'office_phone', 'sim_card', 'pool_number', 'other')
               OR name IN ('SIM-карта', 'Входящая линия', 'Горячая линия', 'Другое', 'Номер из пула')
            """
        ).fetchone()[0]
        self.assertEqual(obsolete_count, 0)

    def test_init_db_can_be_called_multiple_times_without_resetting_data(self):
        user_id = self.repo.create_user("repeat_init_user", "Repeat Init User")
        init_db(self.conn)
        init_db(self.conn)
        row = self.conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["username"], "repeat_init_user")

    def _tariff_counts(self):
        return {
            "tariffs": self.conn.execute("SELECT COUNT(*) FROM tariffs").fetchone()[0],
            "history": self.conn.execute("SELECT COUNT(*) FROM tariff_change_history").fetchone()[0],
            "change_log": self.conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'tariff'").fetchone()[0],
        }

    def _create_tariff(self, price):
        return self.repo.create_tariff(
            country_id=self.country_id,
            provider_id=self.provider_id,
            provider_currency_id=self.currency_id,
            price_in_provider_currency=price,
            conversion_rate_to_eur="1",
            conversion_rate_date="2026-06-22",
            created_by=self.admin_id,
        )

    def test_text_search_filters_are_unicode_case_insensitive_and_partial(self):
        mexico_id = self.repo.create_country("Мексика", "MEX")
        demotel_id = self.repo.create_provider("DemoTel", "voip", self.currency_id)
        cyrillic_route_id = self.repo.create_route(
            country_id=mexico_id,
            provider_id=demotel_id,
            name="Мексика/DemoTel/Pool@",
            cli_source_type="pool",
            cli_source_label="Pool",
            created_by=self.admin_id,
        )
        latin_route_id = self.repo.create_route(
            country_id=mexico_id,
            provider_id=demotel_id,
            name="CC Mexico Demo",
            cli_source_type="pool",
            cli_source_label="Pool",
            created_by=self.admin_id,
        )

        for search in ("Мексика", "мексика", "МЕКС", "  мекс  "):
            with self.subTest(search=search):
                route_ids = {row["id"] for row in self.repo.list_routes({"search_like": search})}
                self.assertIn(cyrillic_route_id, route_ids)

        for search in ("Mexico", "mexico", "MEXICO", "cc mexico demo", "DEMO"):
            with self.subTest(search=search):
                route_ids = {row["id"] for row in self.repo.list_routes({"search_like": search})}
                self.assertIn(latin_route_id, route_ids)

        filtered_ids = {row["id"] for row in self.repo.list_routes({"country_id": mexico_id, "provider_id": demotel_id, "search_like": "мекс"})}
        self.assertIn(cyrillic_route_id, filtered_ids)
        self.assertNotIn(self.route_id, filtered_ids)

        blank_search_ids = {row["id"] for row in self.repo.list_routes({"search_like": "   "})}
        self.assertIn(cyrillic_route_id, blank_search_ids)
        self.assertIn(latin_route_id, blank_search_ids)

    def _insert_routing_event(self, event_at: str, comment: str) -> None:
        self.conn.execute(
            """
            INSERT INTO routing_events(
                event_at, apply_scope, reason, country_id, provider_id, comment,
                snapshot_json, created_by, updated_by
            ) VALUES (?, 'none', 'Другое', ?, ?, ?, '{}', ?, ?)
            """,
            (event_at, self.country_id, self.provider_id, comment, self.admin_id, self.admin_id),
        )
        self.conn.commit()

    def test_list_routing_events_filters_by_date_from(self):
        self._insert_routing_event("2026-06-21 23:59:59", "repo before from")
        self._insert_routing_event("2026-06-22 00:00:00", "repo from match")
        comments = [row["comment"] for row in self.repo.list_routing_events({"date_from": "2026-06-22 00:00:00"})]
        self.assertIn("repo from match", comments)
        self.assertNotIn("repo before from", comments)

    def test_list_routing_events_filters_by_date_to_inclusively(self):
        self._insert_routing_event("2026-06-22 23:59:59", "repo to match")
        self._insert_routing_event("2026-06-23 00:00:00", "repo after to")
        comments = [row["comment"] for row in self.repo.list_routing_events({"date_to": "2026-06-22 23:59:59"})]
        self.assertIn("repo to match", comments)
        self.assertNotIn("repo after to", comments)

    def test_create_tariff_rejects_invalid_prices_without_audit(self):
        for price, message in (("", "Цена обязательна"), ("   ", "Цена обязательна"), ("abc", "Цена должна быть числом"), ("0", "Цена должна быть больше 0"), ("-1", "Цена должна быть больше 0")):
            with self.subTest(price=price):
                before = self._tariff_counts()
                with self.assertRaisesRegex(BusinessRuleError, message):
                    self._create_tariff(price)
                self.assertEqual(self._tariff_counts(), before)

    def test_create_tariff_accepts_positive_comma_decimal_price(self):
        tariff_id = self._create_tariff("2,5")
        tariff = self.conn.execute("SELECT price_in_provider_currency, eur_price FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()
        self.assertEqual(str(tariff["price_in_provider_currency"]), "2.5")
        self.assertEqual(str(tariff["eur_price"]), "2.5")

    def test_tariff_duplicate_identity_checks_active_and_inactive_records(self):
        self._create_tariff("2.5")
        before = self._tariff_counts()
        with self.assertRaisesRegex(BusinessRuleError, "Такой тариф уже существует"):
            self._create_tariff("2.9")
        self.assertEqual(self._tariff_counts(), before)

        tariff_id = self.conn.execute("SELECT id FROM tariffs").fetchone()["id"]
        self.repo.set_tariff_active(tariff_id, is_current=False, changed_by=self.admin_id)
        before = self._tariff_counts()
        with self.assertRaisesRegex(BusinessRuleError, "уже существует, но неактивен"):
            self._create_tariff("2.9")
        self.assertEqual(self._tariff_counts(), before)

    def test_tariff_prefix_and_no_prefix_identities_can_coexist(self):
        prefix_id = self.repo.create_prefix(self.provider_id, "0333")
        no_prefix_id = self._create_tariff("2.5")
        prefixed_id = self.repo.create_tariff(
            country_id=self.country_id, provider_id=self.provider_id, provider_prefix_id=prefix_id,
            provider_currency_id=self.currency_id, price_in_provider_currency="2.5", conversion_rate_to_eur="1",
            conversion_rate_date="2026-06-22", created_by=self.admin_id,
        )
        self.assertNotEqual(no_prefix_id, prefixed_id)
        with self.assertRaisesRegex(BusinessRuleError, "Такой тариф уже существует"):
            self._create_tariff("3.0")

    def test_tariff_update_and_activation_history(self):
        usd_id = self.repo.create_currency("USD", "US Dollar", "$")
        tariff_id = self._create_tariff("2.5")
        self.repo.update_tariff(
            tariff_id, provider_currency_id=usd_id, price_in_provider_currency="3.1",
            conversion_rate_to_eur="0.9", conversion_rate_date="2026-06-22", currency_rate_id=None,
            comment="new terms", updated_by=self.admin_id,
        )
        row = self.conn.execute("SELECT reason, comment FROM tariff_change_history WHERE tariff_id = ? ORDER BY id DESC LIMIT 1", (tariff_id,)).fetchone()
        self.assertEqual(row["reason"], "tariff.changed")
        self.assertIn("Цена провайдера: 2.5 → 3.1", row["comment"])
        self.assertIn("Валюта: EUR → USD", row["comment"])
        self.assertIn("Комментарий: — → new terms", row["comment"])

        history_count = self.conn.execute("SELECT COUNT(*) FROM tariff_change_history WHERE tariff_id = ?", (tariff_id,)).fetchone()[0]
        self.assertFalse(self.repo.update_tariff(
            tariff_id, provider_currency_id=usd_id, price_in_provider_currency="3.1",
            conversion_rate_to_eur="0.9", conversion_rate_date="2026-06-22", currency_rate_id=None,
            comment="new terms", updated_by=self.admin_id,
        ))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tariff_change_history WHERE tariff_id = ?", (tariff_id,)).fetchone()[0], history_count)

        self.repo.update_tariff(
            tariff_id, provider_currency_id=usd_id, price_in_provider_currency="3.1",
            conversion_rate_to_eur="0.9", conversion_rate_date="2026-06-22", currency_rate_id=None,
            comment="new terms", updated_by=self.admin_id, is_current=False,
        )
        row = self.conn.execute("SELECT reason, comment FROM tariff_change_history WHERE tariff_id = ? ORDER BY id DESC LIMIT 1", (tariff_id,)).fetchone()
        self.assertEqual(row["reason"], "tariff.deactivated")
        self.assertIn("Тариф деактивирован", row["comment"])

        self.repo.update_tariff(
            tariff_id, provider_currency_id=usd_id, price_in_provider_currency="3.2",
            conversion_rate_to_eur="0.9", conversion_rate_date="2026-06-22", currency_rate_id=None,
            comment="reactivated with new price", updated_by=self.admin_id, is_current=True,
        )
        row = self.conn.execute("SELECT reason, comment FROM tariff_change_history WHERE tariff_id = ? ORDER BY id DESC LIMIT 1", (tariff_id,)).fetchone()
        self.assertEqual(row["reason"], "tariff.changed")
        self.assertIn("Цена провайдера: 3.1 → 3.2", row["comment"])
        self.assertIn("Комментарий: new terms → reactivated with new price", row["comment"])
        self.assertIn("Активность: Нет → Да", row["comment"])

        history_count = self.conn.execute("SELECT COUNT(*) FROM tariff_change_history WHERE tariff_id = ?", (tariff_id,)).fetchone()[0]
        self.assertFalse(self.repo.update_tariff(
            tariff_id, provider_currency_id=usd_id, price_in_provider_currency="3.2",
            conversion_rate_to_eur="0.9", conversion_rate_date="2026-06-22", currency_rate_id=None,
            comment="reactivated with new price", updated_by=self.admin_id, is_current=True,
        ))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tariff_change_history WHERE tariff_id = ?", (tariff_id,)).fetchone()[0], history_count)


    def test_tariff_update_succeeds_with_current_token(self):
        tariff_id = self._create_tariff("2.5")
        token = self.conn.execute("SELECT updated_at FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()["updated_at"]

        self.repo.update_tariff(
            tariff_id, provider_currency_id=self.currency_id, price_in_provider_currency="2.7",
            conversion_rate_to_eur="1", conversion_rate_date="2026-06-22", currency_rate_id=None,
            comment="current token", updated_by=self.admin_id, expected_updated_at=token,
        )

        row = self.conn.execute("SELECT price_in_provider_currency, comment FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()
        self.assertEqual(str(row["price_in_provider_currency"]), "2.7")
        self.assertEqual(row["comment"], "current token")

    def test_tariff_update_rejects_stale_token(self):
        tariff_id = self._create_tariff("2.5")
        stale_token = self.conn.execute("SELECT updated_at FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()["updated_at"]
        self.repo.update_tariff(
            tariff_id, provider_currency_id=self.currency_id, price_in_provider_currency="2.6",
            conversion_rate_to_eur="1", conversion_rate_date="2026-06-22", currency_rate_id=None,
            comment="fresh user b", updated_by=self.admin_id, expected_updated_at=stale_token,
        )

        with self.assertRaisesRegex(ConcurrencyConflict, "Запись была изменена другим пользователем"):
            self.repo.update_tariff(
                tariff_id, provider_currency_id=self.currency_id, price_in_provider_currency="9.9",
                conversion_rate_to_eur="1", conversion_rate_date="2026-06-22", currency_rate_id=None,
                comment="stale user a", updated_by=self.admin_id, expected_updated_at=stale_token,
            )

        row = self.conn.execute("SELECT price_in_provider_currency, comment FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()
        self.assertEqual(str(row["price_in_provider_currency"]), "2.6")
        self.assertEqual(row["comment"], "fresh user b")

    def test_tariff_update_stale_token_does_not_write_history_or_change_log(self):
        tariff_id = self._create_tariff("2.5")
        stale_token = self.conn.execute("SELECT updated_at FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()["updated_at"]
        self.repo.update_tariff(
            tariff_id, provider_currency_id=self.currency_id, price_in_provider_currency="2.6",
            conversion_rate_to_eur="1", conversion_rate_date="2026-06-22", currency_rate_id=None,
            comment="fresh history", updated_by=self.admin_id, expected_updated_at=stale_token,
        )
        before_history = self.conn.execute("SELECT COUNT(*) FROM tariff_change_history WHERE tariff_id = ?", (tariff_id,)).fetchone()[0]
        before_log = self.conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'tariff' AND entity_id = ?", (tariff_id,)).fetchone()[0]

        with self.assertRaises(ConcurrencyConflict):
            self.repo.update_tariff(
                tariff_id, provider_currency_id=self.currency_id, price_in_provider_currency="9.9",
                conversion_rate_to_eur="1", conversion_rate_date="2026-06-22", currency_rate_id=None,
                comment="stale history", updated_by=self.admin_id, expected_updated_at=stale_token,
            )

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tariff_change_history WHERE tariff_id = ?", (tariff_id,)).fetchone()[0], before_history)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'tariff' AND entity_id = ?", (tariff_id,)).fetchone()[0], before_log)

    def test_tariff_update_without_token_preserves_existing_internal_behavior(self):
        tariff_id = self._create_tariff("2.5")

        self.repo.update_tariff(
            tariff_id, provider_currency_id=self.currency_id, price_in_provider_currency="2.8",
            conversion_rate_to_eur="1", conversion_rate_date="2026-06-22", currency_rate_id=None,
            comment="without token", updated_by=self.admin_id,
        )

        row = self.conn.execute("SELECT price_in_provider_currency, comment FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()
        self.assertEqual(str(row["price_in_provider_currency"]), "2.8")
        self.assertEqual(row["comment"], "without token")

    def test_existing_admin_password_is_not_overwritten_by_fallback(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(conn)
        password_hash, password_salt = hash_password("custom123")
        conn.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE username = 'admin'", (password_hash, password_salt))
        run_lightweight_migrations(conn)
        row = conn.execute("SELECT password_hash, password_salt FROM users WHERE username = 'admin'").fetchone()
        self.assertEqual(row["password_hash"], password_hash)
        self.assertEqual(row["password_salt"], password_salt)
        self.assertTrue(verify_password("custom123", row["password_hash"], row["password_salt"]))
        self.assertFalse(verify_password("admin", row["password_hash"], row["password_salt"]))
        conn.close()

    def test_user_password_is_hashed_and_salted(self):
        user_id = self.repo.create_user("operator2", "operator", "Operator", password="test123")
        row = self.conn.execute("SELECT password_hash, password_salt FROM users WHERE id = ?", (user_id,)).fetchone()
        self.assertIsNotNone(row["password_hash"])
        self.assertIsNotNone(row["password_salt"])
        self.assertNotEqual(row["password_hash"], "test123")
        self.assertNotEqual(row["password_salt"], "test123")
        self.assertTrue(verify_password("test123", row["password_hash"], row["password_salt"]))

    def test_valid_phone_can_be_added_to_route(self):
        phone_id = self.create_phone()
        result = self.repo.add_phone_to_route(
            route_id=self.route_id,
            phone_number_id=phone_id,
            usage_type="pool_member",
            added_by=self.admin_id,
        )
        self.assertGreater(result.route_phone_number_id, 0)

    def test_non_used_provider_active_phones_cannot_be_added_to_route(self):
        for index, status in enumerate(("free", "problem", "unknown")):
            with self.subTest(status=status):
                phone_id = self.create_phone(status=status, number=f"39333123457{index}")
                with self.assertRaisesRegex(BusinessRuleError, "рабочий статус номера должен быть ‘Используется’"):
                    self.repo.add_phone_to_route(
                        route_id=self.route_id,
                        phone_number_id=phone_id,
                        usage_type="pool_member",
                        added_by=self.admin_id,
                    )

    def test_inactive_phone_cannot_be_added_to_route(self):
        phone_id = self.create_phone(is_active=False)
        with self.assertRaisesRegex(BusinessRuleError, "не активен у провайдера"):
            self.repo.add_phone_to_route(
                route_id=self.route_id,
                phone_number_id=phone_id,
                usage_type="pool_member",
                added_by=self.admin_id,
            )

    def test_route_numbers_only_lists_currently_usable_provider_active_numbers(self):
        visible_statuses = ["used", "free", "problem", "unknown"]
        for index, status in enumerate(visible_statuses):
            phone_id = self.create_phone(status=status, number=f"39333123456{index}")
            self.conn.execute(
                "INSERT INTO route_phone_numbers(route_id, phone_number_id, usage_type, is_active, added_by) VALUES (?, ?, 'pool_member', 1, ?)",
                (self.route_id, phone_id, self.admin_id),
            )
        inactive_id = self.create_phone(status="used", number="393331234580")
        for phone_id in (inactive_id,):
            self.conn.execute(
                "INSERT INTO route_phone_numbers(route_id, phone_number_id, usage_type, is_active, added_by) VALUES (?, ?, 'pool_member', 1, ?)",
                (self.route_id, phone_id, self.admin_id),
            )
        self.conn.execute("UPDATE phone_numbers SET is_active = 0 WHERE id = ?", (inactive_id,))
        self.conn.commit()

        numbers = {row["number"] for row in self.repo.route_numbers(self.route_id)}

        self.assertEqual(numbers, {f"39333123456{index}" for index in range(len(visible_statuses))})

    def test_deactivating_phone_closes_route_links_and_logs_history_and_change_log(self):
        phone_id = self.create_phone(number="393331234590")
        link_id = self.repo.add_phone_to_route(
            route_id=self.route_id,
            phone_number_id=phone_id,
            usage_type="pool_member",
            added_by=self.admin_id,
        ).route_phone_number_id

        self.repo.update_phone_number(
            phone_id,
            country_id=self.country_id,
            provider_id=self.provider_id,
            number="393331234590",
            assignment_type="gl",
            status="used",
            is_active=False,
            updated_by=self.admin_id,
            currency_id=self.currency_id,
        )

        link = self.conn.execute("SELECT is_active, removed_at, removed_by FROM route_phone_numbers WHERE id = ?", (link_id,)).fetchone()
        self.assertEqual(link["is_active"], 0)
        self.assertIsNotNone(link["removed_at"])
        self.assertEqual(link["removed_by"], self.admin_id)
        self.assertEqual(self.repo.route_numbers(self.route_id), [])
        history = self.conn.execute(
            "SELECT * FROM route_phone_number_history WHERE route_id = ? AND phone_number_id = ? AND action = 'removed'",
            (self.route_id, phone_id),
        ).fetchone()
        self.assertIsNotNone(history)
        log = self.conn.execute(
            "SELECT * FROM change_log WHERE entity_type = 'route_phone_number' AND entity_id = ? AND change_type = 'route_phone_number.removed_by_phone_deactivation'",
            (link_id,),
        ).fetchone()
        self.assertIsNotNone(log)

    def test_reactivating_previously_deactivated_phone_sets_review_required(self):
        phone_id = self.create_phone(status="problem", is_active=False, number="393331234591")
        self.repo.update_phone_number(
            phone_id,
            country_id=self.country_id,
            provider_id=self.provider_id,
            number="393331234591",
            assignment_type="gl",
            status="free",
            is_active=True,
            updated_by=self.admin_id,
            currency_id=self.currency_id,
            review_required=False,
        )

        row = self.conn.execute("SELECT is_active, status, review_required, deactivated_at FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
        self.assertEqual(row["is_active"], 1)
        self.assertEqual(row["status"], "free")
        self.assertEqual(row["review_required"], 1)
        self.assertIsNotNone(row["deactivated_at"])

    def test_phone_update_history_records_readable_field_changes(self):
        phone_id = self.create_phone(status="free", number="393331234593")
        new_provider_id = self.repo.create_provider("Zadarma", "voip", self.currency_id)

        self.repo.update_phone_number(
            phone_id,
            country_id=self.country_id,
            provider_id=new_provider_id,
            number="393331234593",
            assignment_type="gl",
            status="problem",
            is_active=True,
            updated_by=self.admin_id,
            project_label="ИТМ",
            currency_id=self.currency_id,
            comment="new comment",
        )

        row = self.conn.execute(
            "SELECT action, old_value, new_value FROM phone_number_history WHERE phone_number_id = ? AND action = 'updated' ORDER BY id DESC",
            (phone_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["action"], "updated")
        payload = json.loads(row["new_value"])
        self.assertIn("Рабочий статус: Свободен → Проблемный", payload["details"])
        self.assertIn("Провайдер: Miatel → Zadarma", payload["details"])
        self.assertIn("Проект: — → ИТМ", payload["details"])
        self.assertIn("Комментарий: — → new comment", payload["details"])
        self.assertNotIn("provider_id", payload["details"])

    def test_phone_reactivation_history_includes_forced_review_required_change(self):
        phone_id = self.create_phone(status="problem", is_active=False, number="393331234594")

        self.repo.update_phone_number(
            phone_id,
            country_id=self.country_id,
            provider_id=self.provider_id,
            number="393331234594",
            assignment_type="gl",
            status="free",
            is_active=True,
            updated_by=self.admin_id,
            currency_id=self.currency_id,
            review_required=False,
        )

        row = self.conn.execute(
            "SELECT new_value FROM phone_number_history WHERE phone_number_id = ? AND action = 'updated' ORDER BY id DESC",
            (phone_id,),
        ).fetchone()
        payload = json.loads(row["new_value"])
        self.assertIn("Активен у провайдера: Нет → Да", payload["details"])
        self.assertIn("Требует проверки: Нет → Да", payload["details"])

    def test_route_update_history_records_readable_field_changes(self):
        new_provider_id = self.repo.create_provider("DemoTel", "voip", self.currency_id)
        self.repo.update_route(
            self.route_id,
            name="Италия/Miatel/Pool_B@",
            provider_id=new_provider_id,
            provider_prefix_id=None,
            comment="changed",
            is_actual=False,
            priority_status="priority",
            updated_by=self.admin_id,
        )

        row = self.conn.execute(
            "SELECT new_value FROM route_history WHERE route_id = ? AND action = 'updated' ORDER BY id DESC",
            (self.route_id,),
        ).fetchone()
        payload = json.loads(row["new_value"])
        self.assertIn("Название маршрута: Италия/Miatel/Pool_A@ → Италия/Miatel/Pool_B@", payload["details"])
        self.assertIn("Провайдер: Miatel → DemoTel", payload["details"])
        self.assertIn("Активность маршрута: Да → Нет", payload["details"])
        self.assertIn("Комментарий: — → changed", payload["details"])
        self.assertIn("Приоритет: Неизвестно → Приоритетный", payload["details"])


    def test_route_update_succeeds_with_current_token(self):
        token = self.conn.execute("SELECT updated_at FROM routes WHERE id = ?", (self.route_id,)).fetchone()["updated_at"]

        self.repo.update_route(
            self.route_id,
            name="Италия/Miatel/Pool_Token_OK@",
            provider_id=self.provider_id,
            provider_prefix_id=None,
            comment="updated with current token",
            is_actual=True,
            priority_status="unknown",
            updated_by=self.admin_id,
            expected_updated_at=token,
        )

        updated = self.conn.execute("SELECT name, comment FROM routes WHERE id = ?", (self.route_id,)).fetchone()
        self.assertEqual(updated["name"], "Италия/Miatel/Pool_Token_OK@")
        self.assertEqual(updated["comment"], "updated with current token")

    def test_route_update_rejects_stale_token(self):
        stale_token = self.conn.execute("SELECT updated_at FROM routes WHERE id = ?", (self.route_id,)).fetchone()["updated_at"]
        self.repo.update_route(
            self.route_id,
            name="Италия/Miatel/User_B_Fresh@",
            provider_id=self.provider_id,
            provider_prefix_id=None,
            comment="fresh route change",
            is_actual=True,
            priority_status="unknown",
            updated_by=self.admin_id,
            expected_updated_at=stale_token,
        )

        with self.assertRaisesRegex(ConcurrencyConflict, "Запись была изменена другим пользователем"):
            self.repo.update_route(
                self.route_id,
                name="Италия/Miatel/User_A_Stale@",
                provider_id=self.provider_id,
                provider_prefix_id=None,
                comment="stale route change",
                is_actual=True,
                priority_status="unknown",
                updated_by=self.admin_id,
                expected_updated_at=stale_token,
            )

        current = self.conn.execute("SELECT name, comment FROM routes WHERE id = ?", (self.route_id,)).fetchone()
        self.assertEqual(current["name"], "Италия/Miatel/User_B_Fresh@")
        self.assertEqual(current["comment"], "fresh route change")

    def test_route_update_stale_token_does_not_write_history(self):
        stale_token = self.conn.execute("SELECT updated_at FROM routes WHERE id = ?", (self.route_id,)).fetchone()["updated_at"]
        before_count = self.conn.execute("SELECT COUNT(*) AS count FROM route_history WHERE route_id = ?", (self.route_id,)).fetchone()["count"]
        self.repo.update_route(
            self.route_id,
            name="Италия/Miatel/History_User_B@",
            provider_id=self.provider_id,
            provider_prefix_id=None,
            comment="fresh history change",
            is_actual=True,
            priority_status="unknown",
            updated_by=self.admin_id,
            expected_updated_at=stale_token,
        )
        after_fresh_count = self.conn.execute("SELECT COUNT(*) AS count FROM route_history WHERE route_id = ?", (self.route_id,)).fetchone()["count"]

        with self.assertRaises(ConcurrencyConflict):
            self.repo.update_route(
                self.route_id,
                name="Италия/Miatel/History_User_A@",
                provider_id=self.provider_id,
                provider_prefix_id=None,
                comment="stale history change",
                is_actual=True,
                priority_status="unknown",
                updated_by=self.admin_id,
                expected_updated_at=stale_token,
            )

        after_stale_count = self.conn.execute("SELECT COUNT(*) AS count FROM route_history WHERE route_id = ?", (self.route_id,)).fetchone()["count"]
        self.assertEqual(after_fresh_count, before_count + 1)
        self.assertEqual(after_stale_count, after_fresh_count)

    def test_route_update_without_token_preserves_existing_internal_behavior(self):
        self.repo.update_route(
            self.route_id,
            name="Италия/Miatel/No_Token@",
            provider_id=self.provider_id,
            provider_prefix_id=None,
            comment="updated without token",
            is_actual=False,
            priority_status="alternative",
            updated_by=self.admin_id,
        )

        updated = self.conn.execute("SELECT name, comment, is_actual, priority_status FROM routes WHERE id = ?", (self.route_id,)).fetchone()
        self.assertEqual(updated["name"], "Италия/Miatel/No_Token@")
        self.assertEqual(updated["comment"], "updated without token")
        self.assertEqual(updated["is_actual"], 0)
        self.assertEqual(updated["priority_status"], "alternative")

    def test_route_long_comment_history_is_truncated(self):
        old_comment = "Старый " + "комментарий " * 20
        new_comment = "Новый " + "комментарий " * 20
        route_id = self.repo.create_route(
            country_id=self.country_id, provider_id=self.provider_id, name="Италия/Miatel/Pool_Long@",
            cli_source_type="pool", cli_source_label="Pool_Long", created_by=self.admin_id, comment=old_comment,
        )

        self.repo.update_route(
            route_id, name="Италия/Miatel/Pool_Long@", provider_id=self.provider_id, provider_prefix_id=None,
            comment=new_comment, is_actual=True, priority_status="unknown", updated_by=self.admin_id,
        )

        row = self.conn.execute(
            "SELECT new_value FROM route_history WHERE route_id = ? AND action = 'updated' ORDER BY id DESC",
            (route_id,),
        ).fetchone()
        payload = json.loads(row["new_value"])
        self.assertIn("Комментарий: Старый", payload["details"])
        self.assertIn("→ Новый", payload["details"])
        self.assertIn("…", payload["details"])
        self.assertLess(len(payload["details"]), 230)

    def test_route_phone_add_remove_history_is_not_duplicated_by_field_history(self):
        phone_id = self.create_phone(number="393331234595")
        result = self.repo.add_phone_to_route(
            route_id=self.route_id,
            phone_number_id=phone_id,
            usage_type="pool_member",
            added_by=self.admin_id,
        )
        self.repo.remove_phone_links_from_route(route_id=self.route_id, link_ids=[result.route_phone_number_id], removed_by=self.admin_id)

        events = self.conn.execute(
            "SELECT action, COUNT(*) AS count FROM route_phone_number_history WHERE route_id = ? AND phone_number_id = ? GROUP BY action",
            (self.route_id, phone_id),
        ).fetchall()
        self.assertEqual({row["action"]: row["count"] for row in events}, {"added": 1, "removed": 1})



    def _latest_phone_update_details(self, phone_id):
        row = self.conn.execute(
            "SELECT new_value FROM phone_number_history WHERE phone_number_id = ? AND action = 'updated' ORDER BY id DESC",
            (phone_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row["new_value"])["details"]

    def test_phone_money_history_ignores_unchanged_numeric_equivalents(self):
        phone_id = self.repo.create_phone_number(
            country_id=self.country_id, provider_id=self.provider_id, number="393331234596",
            assignment_type="gl", status="used", created_by=self.admin_id,
            connection_cost="50", monthly_fee="50.00", currency_id=self.currency_id,
        )

        self.repo.update_phone_number(
            phone_id, country_id=self.country_id, provider_id=self.provider_id, number="393331234596",
            assignment_type="gl", status="used", is_active=True, updated_by=self.admin_id,
            connection_cost="50.0", monthly_fee="50.000000", currency_id=self.currency_id, comment="real change",
        )

        details = self._latest_phone_update_details(phone_id)
        self.assertIn("Комментарий: — → real change", details)
        self.assertNotIn("Стоимость подключения", details)
        self.assertNotIn("Абонентская плата", details)
        self.assertTrue(_values_equal(Decimal("50.000000"), "50", "money"))

    def test_phone_monthly_fee_only_logs_monthly_fee(self):
        phone_id = self.repo.create_phone_number(
            country_id=self.country_id, provider_id=self.provider_id, number="393331234597",
            assignment_type="gl", status="used", created_by=self.admin_id,
            connection_cost="50", monthly_fee="50", currency_id=self.currency_id,
        )

        self.repo.update_phone_number(
            phone_id, country_id=self.country_id, provider_id=self.provider_id, number="393331234597",
            assignment_type="gl", status="used", is_active=True, updated_by=self.admin_id,
            connection_cost="50.00", monthly_fee="60", currency_id=self.currency_id,
        )

        details = self._latest_phone_update_details(phone_id)
        self.assertIn("Абонентская плата: 50 → 60", details)
        self.assertNotIn("Стоимость подключения", details)

    def test_phone_connection_cost_only_logs_connection_cost(self):
        phone_id = self.repo.create_phone_number(
            country_id=self.country_id, provider_id=self.provider_id, number="393331234598",
            assignment_type="gl", status="used", created_by=self.admin_id,
            connection_cost="50", monthly_fee="50", currency_id=self.currency_id,
        )

        self.repo.update_phone_number(
            phone_id, country_id=self.country_id, provider_id=self.provider_id, number="393331234598",
            assignment_type="gl", status="used", is_active=True, updated_by=self.admin_id,
            connection_cost="60", monthly_fee="50.00", currency_id=self.currency_id,
        )

        details = self._latest_phone_update_details(phone_id)
        self.assertIn("Стоимость подключения: 50 → 60", details)
        self.assertNotIn("Абонентская плата", details)

    def test_reactivation_review_can_be_cleared_without_repeating_history(self):
        phone_id = self.create_phone(status="problem", is_active=False, number="393331234589")
        self.repo.update_phone_number(
            phone_id, country_id=self.country_id, provider_id=self.provider_id, number="393331234589",
            assignment_type="gl", status="used", is_active=True, updated_by=self.admin_id,
            connection_cost="50", monthly_fee="50", currency_id=self.currency_id, review_required=False,
        )
        first_details = self._latest_phone_update_details(phone_id)
        self.assertIn("Активен у провайдера: Нет → Да", first_details)
        self.assertIn("Требует проверки: Нет → Да", first_details)

        self.repo.update_phone_number(
            phone_id, country_id=self.country_id, provider_id=self.provider_id, number="393331234589",
            assignment_type="gl", status="used", is_active=True, updated_by=self.admin_id,
            connection_cost="50.00", monthly_fee="50.000000", currency_id=self.currency_id, review_required=False,
        )

        row = self.conn.execute("SELECT review_required FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
        self.assertEqual(row["review_required"], 0)
        details = self._latest_phone_update_details(phone_id)
        self.assertEqual(details, "Требует проверки: Да → Нет")
        self.assertNotIn("Активен у провайдера: Нет → Да", details)
        self.assertNotIn("Стоимость подключения", details)
        self.repo.add_phone_to_route(route_id=self.route_id, phone_number_id=phone_id, usage_type="pool_member", added_by=self.admin_id)

    def test_review_required_clear_is_rejected_without_provider(self):
        phone_id = self.repo.create_phone_number(
            country_id=self.country_id, provider_id=None, number="393331234588",
            assignment_type="gl", status="unknown", created_by=self.admin_id,
            review_required=True,
        )

        with self.assertRaisesRegex(BusinessRuleError, "Нельзя снять флаг проверки"):
            self.repo.update_phone_number(
                phone_id, country_id=self.country_id, provider_id=None, number="393331234588",
                assignment_type="gl", status="unknown", is_active=True,
                updated_by=self.admin_id, review_required=False,
            )

        row = self.conn.execute("SELECT review_required FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
        self.assertEqual(row["review_required"], 1)

    def test_past_deactivation_does_not_force_review_after_manual_clear(self):
        phone_id = self.create_phone(status="problem", is_active=False, number="393331234587")
        self.repo.update_phone_number(
            phone_id, country_id=self.country_id, provider_id=self.provider_id, number="393331234587",
            assignment_type="gl", status="used", is_active=True, updated_by=self.admin_id,
            currency_id=self.currency_id, review_required=False,
        )
        self.repo.update_phone_number(
            phone_id, country_id=self.country_id, provider_id=self.provider_id, number="393331234587",
            assignment_type="gl", status="used", is_active=True, updated_by=self.admin_id,
            currency_id=self.currency_id, review_required=False,
        )
        self.repo.update_phone_number(
            phone_id, country_id=self.country_id, provider_id=self.provider_id, number="393331234587",
            assignment_type="gl", status="used", is_active=True, updated_by=self.admin_id,
            currency_id=self.currency_id, comment="verified", review_required=False,
        )

        row = self.conn.execute("SELECT review_required, deactivated_at FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
        self.assertEqual(row["review_required"], 0)
        self.assertIsNotNone(row["deactivated_at"])
        details = self._latest_phone_update_details(phone_id)
        self.assertEqual(details, "Комментарий: — → verified")
        self.assertNotIn("Требует проверки: Нет → Да", details)

    def test_old_phone_statuses_are_normalized_on_create(self):
        cases = {"reserved": "free", "blocked": "problem", "disabled": "problem", "": "unknown", "invalid": "unknown"}
        for index, (old_status, expected) in enumerate(cases.items()):
            with self.subTest(old_status=old_status):
                phone_id = self.create_phone(status=old_status, number=f"3933312350{index:02d}")
                row = self.conn.execute("SELECT status FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
                self.assertEqual(row["status"], expected)


    def test_lightweight_migration_maps_old_statuses_without_deleting_links(self):
        phone_id = self.create_phone(status="used", number="393331234599")
        link_id = self.repo.add_phone_to_route(
            route_id=self.route_id,
            phone_number_id=phone_id,
            usage_type="pool_member",
            added_by=self.admin_id,
        ).route_phone_number_id
        self.conn.execute("PRAGMA ignore_check_constraints = ON")
        self.conn.execute("UPDATE phone_numbers SET status = 'reserved' WHERE id = ?", (phone_id,))
        self.conn.execute("PRAGMA writable_schema = ON")
        self.conn.execute(
            "UPDATE sqlite_master SET sql = REPLACE(sql, ?, ?) WHERE type = 'table' AND name = 'phone_numbers'",
            ("'used', 'free', 'problem', 'unknown'", "'used', 'free', 'disabled', 'reserved', 'blocked', 'unknown'"),
        )
        self.conn.execute("PRAGMA writable_schema = OFF")
        self.conn.commit()

        run_lightweight_migrations(self.conn)

        phone = self.conn.execute("SELECT status FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
        link = self.conn.execute("SELECT route_id, phone_number_id, is_active FROM route_phone_numbers WHERE id = ?", (link_id,)).fetchone()
        self.assertEqual(phone["status"], "free")
        self.assertEqual(dict(link), {"route_id": self.route_id, "phone_number_id": phone_id, "is_active": 1})

    def test_valid_phone_statuses_are_simplified_set(self):
        table_sql = self.conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'phone_numbers'").fetchone()["sql"]
        self.assertIn("'used', 'unused', 'free', 'problem', 'unknown'", table_sql)
        self.assertNotIn("'reserved'", table_sql)
        self.assertNotIn("'blocked'", table_sql)
        self.assertNotIn("'disabled'", table_sql)


    def test_reactivated_phone_can_be_added_to_route(self):
        phone_id = self.create_phone(status="free", is_active=False, number="393331234592")
        self.repo.update_phone_number(
            phone_id,
            country_id=self.country_id,
            provider_id=self.provider_id,
            number="393331234592",
            assignment_type="gl",
            status="free",
            is_active=True,
            updated_by=self.admin_id,
            currency_id=self.currency_id,
            review_required=True,
        )
        with self.assertRaisesRegex(BusinessRuleError, "рабочий статус номера должен быть ‘Используется’"):
            self.repo.add_phone_to_route(route_id=self.route_id, phone_number_id=phone_id, usage_type="pool_member", added_by=self.admin_id)
        self.repo.update_phone_number(
            phone_id,
            country_id=self.country_id,
            provider_id=self.provider_id,
            number="393331234592",
            assignment_type="gl",
            status="used",
            is_active=True,
            updated_by=self.admin_id,
            currency_id=self.currency_id,
            review_required=True,
        )
        result = self.repo.add_phone_to_route(route_id=self.route_id, phone_number_id=phone_id, usage_type="pool_member", added_by=self.admin_id)
        self.assertGreater(result.route_phone_number_id, 0)

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
        with self.assertRaisesRegex(BusinessRuleError, "Кампания с ID 123 уже существует: CC Italy / EU1"):
            self.repo.create_calling_company(
                server_id=server_id,
                country_id=self.country_id,
                company_name="CC Italy Duplicate",
                company_id_external="123",
                has_autorotation=False,
                created_by=self.admin_id,
            )

    def test_calling_company_external_id_is_globally_unique_across_servers_and_inactive(self):
        eu1_id = self.repo.create_server("EU1-global")
        eu2_id = self.repo.create_server("EU2-global")
        self.repo.create_calling_company(
            server_id=eu1_id,
            country_id=self.country_id,
            company_name="CC Mexico Demo 1",
            company_id_external="1001",
            has_autorotation=False,
            created_by=self.admin_id,
            is_active=False,
        )

        before_companies = self.conn.execute("SELECT COUNT(*) FROM calling_companies").fetchone()[0]
        before_history = self.conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'calling_company'").fetchone()[0]
        before_settings = self.conn.execute("SELECT COUNT(*) FROM company_routing_settings").fetchone()[0]
        with self.assertRaisesRegex(BusinessRuleError, "Кампания с ID 1001 уже существует: CC Mexico Demo 1 / EU1-global"):
            self.repo.create_calling_company(
                server_id=eu2_id,
                country_id=self.country_id,
                company_name="CC Mexico Demo 1",
                company_id_external="1001",
                has_autorotation=True,
                created_by=self.admin_id,
            )

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM calling_companies").fetchone()[0], before_companies)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'calling_company'").fetchone()[0], before_history)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM company_routing_settings").fetchone()[0], before_settings)

    def test_calling_company_same_name_different_external_id_is_allowed(self):
        eu1_id = self.repo.create_server("EU1-name")
        eu2_id = self.repo.create_server("EU2-name")
        self.repo.create_calling_company(server_id=eu1_id, country_id=self.country_id, company_name="CC Mexico Demo 1", company_id_external="1001-name", has_autorotation=False, created_by=self.admin_id)
        company_id = self.repo.create_calling_company(server_id=eu2_id, country_id=self.country_id, company_name="CC Mexico Demo 1", company_id_external="1002-name", has_autorotation=False, created_by=self.admin_id)
        self.assertIsNotNone(self.repo.get_calling_company(company_id))

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

    def test_calling_company_creation_and_edits_write_readable_history(self):
        server_id = self.repo.create_server("IT1")
        company_id = self.repo.create_calling_company(server_id=server_id, country_id=self.country_id, company_name="Mexico_old", company_id_external="cmp-1", has_autorotation=False, created_by=self.admin_id, comment="initial", is_active=False, line_count=10, dial_set_count=5, retry_interval_seconds=30)
        created = self.repo.list_calling_company_history(company_id)[0]
        self.assertIn("Компания создана", created["comment"])
        self.assertIn("Название: Mexico_old", created["new_value"])
        self.repo.update_calling_company(company_id, server_id=server_id, country_id=self.country_id, company_name="Mexico_new", line_count=10, dial_set_count=7, has_autorotation=False, retry_interval_seconds=45, is_active=False, comment="new comment", updated_by=self.admin_id)
        changed = self.repo.list_calling_company_history(company_id)[0]
        self.assertIn("Компания изменена", changed["comment"])
        self.assertIn("Название: Mexico_old → Mexico_new", changed["new_value"])
        self.assertIn("Количество наборов: 5 → 7", changed["new_value"])
        self.assertIn("Интервал, сек.: 30 → 45", changed["new_value"])
        self.assertIn("Комментарий: initial → new comment", changed["new_value"])


    def test_calling_company_update_succeeds_with_current_token(self):
        server_id = self.repo.create_server("EU-token-ok")
        company_id = self.repo.create_calling_company(
            server_id=server_id,
            country_id=self.country_id,
            company_name="Token OK",
            company_id_external="token-ok",
            has_autorotation=False,
            created_by=self.admin_id,
        )
        token = self.repo.get_calling_company(company_id)["updated_at"]

        self.repo.update_calling_company(
            company_id,
            server_id=server_id,
            country_id=self.country_id,
            company_name="Token OK Updated",
            line_count=3,
            dial_set_count=4,
            has_autorotation=False,
            retry_interval_seconds=5,
            is_active=True,
            comment="updated with token",
            updated_by=self.admin_id,
            expected_updated_at=token,
        )

        updated = self.repo.get_calling_company(company_id)
        self.assertEqual(updated["company_name"], "Token OK Updated")
        self.assertEqual(updated["line_count"], 3)

    def test_calling_company_update_rejects_stale_token(self):
        server_id = self.repo.create_server("EU-token-stale")
        company_id = self.repo.create_calling_company(
            server_id=server_id,
            country_id=self.country_id,
            company_name="Token Original",
            company_id_external="token-stale",
            has_autorotation=False,
            created_by=self.admin_id,
        )
        stale_token = self.repo.get_calling_company(company_id)["updated_at"]
        self.repo.update_calling_company(
            company_id,
            server_id=server_id,
            country_id=self.country_id,
            company_name="User B Fresh",
            line_count=7,
            dial_set_count=8,
            has_autorotation=False,
            retry_interval_seconds=9,
            is_active=True,
            comment="fresh change",
            updated_by=self.admin_id,
            expected_updated_at=stale_token,
        )

        with self.assertRaisesRegex(ConcurrencyConflict, "Запись была изменена другим пользователем"):
            self.repo.update_calling_company(
                company_id,
                server_id=server_id,
                country_id=self.country_id,
                company_name="User A Stale",
                line_count=1,
                dial_set_count=1,
                has_autorotation=False,
                retry_interval_seconds=1,
                is_active=True,
                comment="stale change",
                updated_by=self.admin_id,
                expected_updated_at=stale_token,
            )

        current = self.repo.get_calling_company(company_id)
        self.assertEqual(current["company_name"], "User B Fresh")
        self.assertEqual(current["comment"], "fresh change")

    def test_calling_company_update_without_token_preserves_existing_internal_behavior(self):
        server_id = self.repo.create_server("EU-no-token")
        company_id = self.repo.create_calling_company(
            server_id=server_id,
            country_id=self.country_id,
            company_name="No Token Original",
            company_id_external="no-token",
            has_autorotation=False,
            created_by=self.admin_id,
        )

        self.repo.update_calling_company(
            company_id,
            server_id=server_id,
            country_id=self.country_id,
            company_name="No Token Updated",
            line_count=2,
            dial_set_count=3,
            has_autorotation=False,
            retry_interval_seconds=4,
            is_active=True,
            comment="legacy caller",
            updated_by=self.admin_id,
        )

        updated = self.repo.get_calling_company(company_id)
        self.assertEqual(updated["company_name"], "No Token Updated")
        self.assertEqual(updated["comment"], "legacy caller")

    def test_calling_company_creation_autorotation_creates_routing_source_of_truth(self):
        server_id = self.repo.create_server("IT-auto")
        company_id = self.repo.create_calling_company(
            server_id=server_id,
            country_id=self.country_id,
            company_name="Auto company",
            company_id_external="auto-1",
            has_autorotation=True,
            created_by=self.admin_id,
            is_active=True,
            line_count=3,
            dial_set_count=4,
            retry_interval_seconds=20,
        )

        setting = self.conn.execute("SELECT * FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL", (company_id,)).fetchone()
        self.assertIsNotNone(setting)
        self.assertEqual(setting["routing_mode"], "autorotation")
        self.assertEqual(setting["has_autorotation"], 1)
        self.assertIsNone(setting["route_id"])
        listed = self.repo.list_calling_companies({"has_autorotation": "1"})
        self.assertTrue(any(row["id"] == company_id and row["current_has_autorotation"] == 1 for row in listed))
        created = self.repo.list_calling_company_history(company_id)[0]
        self.assertIn("Компания создана", created["comment"])
        self.assertIn("ID кампании: auto-1", created["new_value"])
        self.assertIn("Авторотация: Да", created["new_value"])

    def test_calling_company_list_autorotation_ignores_stale_company_field(self):
        server_id = self.repo.create_server("IT-stale")
        company_id = self.repo.create_calling_company(
            server_id=server_id,
            country_id=self.country_id,
            company_name="Stale company",
            company_id_external="stale-1",
            has_autorotation=True,
            created_by=self.admin_id,
        )
        active = self.conn.execute("SELECT id FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL", (company_id,)).fetchone()
        self.repo.deactivate_company_routing_setting(setting_id=active["id"], updated_by=self.admin_id)

        row = next(row for row in self.repo.list_calling_companies({}) if row["id"] == company_id)
        self.assertEqual(row["has_autorotation"], 1)
        self.assertEqual(row["current_has_autorotation"], 0)
        filtered = self.repo.list_calling_companies({"has_autorotation": "0"})
        self.assertTrue(any(row["id"] == company_id for row in filtered))

    def test_calling_company_activation_deactivation_and_journal_search(self):
        server_id = self.repo.create_server("IT1")
        company_id = self.repo.create_calling_company(server_id=server_id, country_id=self.country_id, company_name="Searchable", company_id_external="cmp-search", has_autorotation=False, created_by=self.admin_id, is_active=False)
        self.repo.update_calling_company(company_id, server_id=server_id, country_id=self.country_id, company_name="Searchable", line_count=0, dial_set_count=0, has_autorotation=False, retry_interval_seconds=0, is_active=True, comment=None, updated_by=self.admin_id)
        self.assertEqual(self.repo.list_calling_company_history(company_id)[0]["comment"], "Компания активирована")
        self.repo.update_calling_company(company_id, server_id=server_id, country_id=self.country_id, company_name="Searchable", line_count=0, dial_set_count=0, has_autorotation=False, retry_interval_seconds=0, is_active=False, comment=None, updated_by=self.admin_id)
        self.assertEqual(self.repo.list_calling_company_history(company_id)[0]["comment"], "Компания деактивирована")
        events = self.repo.list_calling_company_events(search="CMP-SEARCH", limit=50, offset=0)
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0]["company_id_external"], "cmp-search")
        self.assertEqual(self.repo.count_calling_company_events(search="searchable"), self.repo.count_calling_company_events(search="SEARCHABLE"))

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

    def test_company_routing_comment_update_method_changes_comment_only(self):
        setting_id = self.create_routing_setting(comment="before", routing_mode="autorotation", has_autorotation=True)
        before_events = self.conn.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0]
        before_company_history = self.conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'routing_event'").fetchone()[0]

        returned_id = self.repo.update_company_routing_setting_comment(setting_id=setting_id, comment="after", updated_by=self.admin_id)

        self.assertEqual(returned_id, setting_id)
        rows = self.conn.execute("SELECT * FROM company_routing_settings").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["comment"], "after")
        self.assertEqual(rows[0]["routing_mode"], "autorotation")
        self.assertEqual(rows[0]["has_autorotation"], 1)
        self.assertEqual(rows[0]["is_active"], 1)
        self.assertIsNone(rows[0]["valid_to"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0], before_events)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'routing_event'").fetchone()[0], before_company_history)
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

    def test_none_scope_clears_server_ids_in_change_log_new_values(self):
        event_id = self.create_event(apply_scope="none", provider_id=self.provider_id, server_id=self.server_id, server_ids=[self.server_id])

        raw_new_values = self.conn.execute(
            "SELECT new_values FROM change_log WHERE entity_type = 'routing_event' AND entity_id = ?",
            (event_id,),
        ).fetchone()[0]
        new_values = json.loads(raw_new_values)

        self.assertIsNone(new_values["server_ids"])
        self.assertIsNone(new_values["affected_servers"])

    def test_campaign_setting_event_uses_company_id_server_for_journal_and_filter(self):
        event_id = self.create_event(
            apply_scope="campaign_setting",
            calling_company_id=self.company_id,
            company_change_type="enable_autorotation",
            provider_id=self.provider_id,
            server_id=None,
        )
        stored = self.conn.execute("SELECT server_id, snapshot_json FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        self.assertIsNone(stored["server_id"])
        self.assertIsNone(json.loads(stored["snapshot_json"])["server_name"])

        rows = self.repo.list_routing_events({"server_id": self.server_id})
        self.assertEqual([row["id"] for row in rows], [event_id])
        self.assertEqual(rows[0]["company_server_name"], "EU1")

        other_server_id = self.repo.create_server("EU3")
        self.repo.create_calling_company(
            server_id=other_server_id,
            country_id=self.country_id,
            company_name="CC Mexico",
            company_id_external="1003",
            has_autorotation=False,
            created_by=self.admin_id,
        )
        rows = self.repo.list_routing_events({"server_id": self.server_id})
        self.assertEqual([row["id"] for row in rows], [event_id])
        self.assertEqual(rows[0]["company_server_name"], "EU1")
        self.assertEqual(self.repo.list_routing_events({"server_id": other_server_id}), [])

    def test_calling_company_server_can_be_changed_on_edit(self):
        other_server_id = self.repo.create_server("EU3")
        self.repo.update_calling_company(
            self.company_id,
            server_id=other_server_id,
            country_id=self.country_id,
            company_name="CC Mexico",
            line_count=0,
            dial_set_count=0,
            has_autorotation=False,
            retry_interval_seconds=0,
            is_active=True,
            comment=None,
            updated_by=self.admin_id,
        )
        updated = self.repo.get_calling_company(self.company_id)
        self.assertEqual(updated["server_id"], other_server_id)
        self.assertIn("Сервер: EU1 → EU3", self.repo.list_calling_company_history(self.company_id)[0]["new_value"])

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

    def test_server_priority_single_server_creates_application_row(self):
        event_id = self.create_event(apply_scope="server_priority", country_id=self.country_id, server_id=self.server_id, new_route_id=self.route_id, provider_id=self.provider_id)

        application = self.conn.execute("SELECT * FROM routing_event_servers WHERE routing_event_id = ?", (event_id,)).fetchone()
        priority = self.conn.execute("SELECT * FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (self.country_id, self.server_id)).fetchone()
        snapshot = json.loads(self.conn.execute("SELECT snapshot_json FROM routing_events WHERE id = ?", (event_id,)).fetchone()[0])

        self.assertEqual(application["server_id"], self.server_id)
        self.assertIsNone(application["old_route_id"])
        self.assertEqual(application["new_route_id"], self.route_id)
        self.assertEqual(application["server_route_priority_id"], priority["id"])
        self.assertEqual(application["status"], "applied")
        self.assertEqual(snapshot["affected_servers"][0]["status"], "applied")

    def test_server_priority_multiple_server_ids_create_one_event_and_many_application_rows(self):
        server_2 = self.repo.create_server("EU2")
        self.conn.execute("INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, changed_by, created_by) VALUES (?, ?, ?, NULL, ?, ?)", (self.country_id, self.server_id, self.route_id, self.admin_id, self.admin_id))
        self.conn.execute("INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, changed_by, created_by) VALUES (?, ?, ?, NULL, ?, ?)", (self.country_id, server_2, self.route_id, self.admin_id, self.admin_id))
        self.conn.commit()

        event_id = self.create_event(apply_scope="server_priority", country_id=self.country_id, server_ids=[self.server_id, server_2], provider_id=self.alt_provider_id, new_route_id=self.alt_route_id)

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM routing_events WHERE id = ?", (event_id,)).fetchone()[0], 1)
        rows = self.conn.execute("SELECT * FROM routing_event_servers WHERE routing_event_id = ? ORDER BY server_id", (event_id,)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "applied" for row in rows))
        priorities = self.conn.execute("SELECT * FROM server_route_priorities WHERE country_id = ? ORDER BY server_id", (self.country_id,)).fetchall()
        self.assertEqual([row["current_route_id"] for row in priorities], [self.alt_route_id, self.alt_route_id])
        self.assertEqual([row["previous_route_id"] for row in priorities], [self.route_id, self.route_id])

    def test_server_priority_multi_server_creates_missing_priority_with_null_previous(self):
        server_2 = self.repo.create_server("EU2")
        self.conn.execute("INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, changed_by, created_by) VALUES (?, ?, ?, NULL, ?, ?)", (self.country_id, self.server_id, self.route_id, self.admin_id, self.admin_id))
        self.conn.commit()

        self.create_event(apply_scope="server_priority", country_id=self.country_id, server_ids=[self.server_id, server_2], provider_id=self.alt_provider_id, new_route_id=self.alt_route_id)

        priority = self.conn.execute("SELECT * FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (self.country_id, server_2)).fetchone()
        self.assertIsNone(priority["previous_route_id"])
        self.assertEqual(priority["current_route_id"], self.alt_route_id)

    def test_server_priority_full_noop_does_not_create_event(self):
        server_2 = self.repo.create_server("EU2")
        for sid in (self.server_id, server_2):
            self.conn.execute("INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, changed_by, created_by) VALUES (?, ?, ?, NULL, ?, ?)", (self.country_id, sid, self.route_id, self.admin_id, self.admin_id))
        self.conn.commit()

        with self.assertRaisesRegex(BusinessRuleError, "уже установлен для всех"):
            self.create_event(apply_scope="server_priority", country_id=self.country_id, server_ids=[self.server_id, server_2], provider_id=self.provider_id, new_route_id=self.route_id)

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM routing_event_servers").fetchone()[0], 0)

    def test_server_priority_partial_noop_records_applied_and_skipped(self):
        server_2 = self.repo.create_server("EU2")
        self.conn.execute("INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, changed_by, created_by) VALUES (?, ?, ?, NULL, ?, ?)", (self.country_id, self.server_id, self.route_id, self.admin_id, self.admin_id))
        self.conn.execute("INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, changed_by, created_by) VALUES (?, ?, ?, NULL, ?, ?)", (self.country_id, server_2, self.alt_route_id, self.admin_id, self.admin_id))
        self.conn.commit()

        event_id = self.create_event(apply_scope="server_priority", country_id=self.country_id, server_ids=[self.server_id, server_2], provider_id=self.alt_provider_id, new_route_id=self.alt_route_id)

        rows = self.conn.execute("SELECT * FROM routing_event_servers WHERE routing_event_id = ? ORDER BY server_id", (event_id,)).fetchall()
        self.assertEqual([row["status"] for row in rows], ["applied", "skipped_noop"])
        changed = self.conn.execute("SELECT * FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (self.country_id, self.server_id)).fetchone()
        skipped = self.conn.execute("SELECT * FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (self.country_id, server_2)).fetchone()
        self.assertEqual(changed["current_route_id"], self.alt_route_id)
        self.assertEqual(changed["previous_route_id"], self.route_id)
        self.assertEqual(skipped["current_route_id"], self.alt_route_id)
        self.assertIsNone(skipped["previous_route_id"])
        snapshot = json.loads(self.conn.execute("SELECT snapshot_json FROM routing_events WHERE id = ?", (event_id,)).fetchone()[0])
        self.assertEqual([row["status"] for row in snapshot["affected_servers"]], ["applied", "skipped_noop"])

    def test_server_priority_requires_server_id_or_server_ids(self):
        with self.assertRaisesRegex(BusinessRuleError, "Сервер обязателен"):
            self.create_event(apply_scope="server_priority", country_id=self.country_id, server_id=None, server_ids=None, provider_id=self.provider_id, new_route_id=self.route_id)

    def test_server_priority_rejects_inactive_server(self):
        self.conn.execute("UPDATE servers SET is_active = 0 WHERE id = ?", (self.server_id,))
        self.conn.commit()
        with self.assertRaisesRegex(BusinessRuleError, "неактивный сервер"):
            self.create_event(apply_scope="server_priority", country_id=self.country_id, server_id=self.server_id, provider_id=self.provider_id, new_route_id=self.route_id)

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
        setting = self.conn.execute("SELECT * FROM company_routing_settings WHERE calling_company_id = ?", (self.company_id,)).fetchone()
        self.assertIsNotNone(setting)
        self.assertEqual(setting["routing_mode"], "autorotation")
        self.assertEqual(setting["has_autorotation"], 1)
        self.assertIsNone(setting["route_id"])

    def test_campaign_setting_with_active_setting_uses_old_values(self):
        self.repo.create_company_routing_setting(calling_company_id=self.company_id, country_id=self.country_id, server_id=self.server_id, route_id=self.route_id, routing_mode="mixed", has_autorotation=True, comment="old", created_by=self.admin_id)
        event_id = self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="set_campaign_route", new_company_route_id=self.alt_route_id, provider_id=None)
        row = self.conn.execute("SELECT * FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        self.assertEqual(row["old_company_routing_mode"], "mixed")
        self.assertEqual(row["old_company_route_id"], self.route_id)
        self.assertEqual(row["old_company_has_autorotation"], 1)

    def test_campaign_setting_enable_autorotation_versions_existing_setting(self):
        old_id = self.repo.create_company_routing_setting(calling_company_id=self.company_id, country_id=self.country_id, server_id=self.server_id, route_id=self.route_id, routing_mode="campaign_route", has_autorotation=False, comment="old", created_by=self.admin_id)
        self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="enable_autorotation", provider_id=None)
        old = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (old_id,)).fetchone()
        active = self.conn.execute("SELECT * FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL", (self.company_id,)).fetchone()
        self.assertEqual(old["is_active"], 0)
        self.assertIsNotNone(old["valid_to"])
        self.assertNotEqual(active["id"], old_id)
        self.assertEqual(active["routing_mode"], "mixed")
        self.assertEqual(active["has_autorotation"], 1)
        self.assertEqual(active["route_id"], self.route_id)


    def test_campaign_routing_history_preserves_event_comments(self):
        self.create_event(
            apply_scope="campaign_setting",
            calling_company_id=self.company_id,
            company_change_type="enable_autorotation",
            provider_id=None,
            comment="1111111",
        )
        self.create_event(
            apply_scope="campaign_setting",
            calling_company_id=self.company_id,
            company_change_type="set_campaign_route",
            new_company_route_id=self.alt_route_id,
            provider_id=None,
            comment="2222222",
            event_at="2026-06-10 13:00",
        )

        active = self.conn.execute("SELECT * FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL", (self.company_id,)).fetchone()
        self.assertEqual(active["comment"], "2222222")
        rows = self.repo.list_company_routing_setting_history(active["id"])
        self.assertEqual([row["comment"] for row in rows], ["2222222", "1111111"])
        self.assertEqual([row["company_change_type"] for row in rows], ["set_campaign_route", "enable_autorotation"])

    def test_campaign_setting_duplicate_enable_autorotation_is_blocked_without_logs(self):
        self.repo.create_company_routing_setting(calling_company_id=self.company_id, country_id=self.country_id, server_id=self.server_id, route_id=None, routing_mode="autorotation", has_autorotation=True, comment="old", created_by=self.admin_id)
        before_events = self.conn.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0]
        before_settings = self.conn.execute("SELECT COUNT(*) FROM company_routing_settings WHERE calling_company_id = ?", (self.company_id,)).fetchone()[0]
        before_setting_logs = self.conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'company_routing_setting'").fetchone()[0]
        before_company_journal = self.conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'routing_event' AND json_extract(new_values, '$.calling_company_id') = ?", (self.company_id,)).fetchone()[0]

        with self.assertRaisesRegex(BusinessRuleError, "В этой компании уже включена авторотация"):
            self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="enable_autorotation", provider_id=None)

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0], before_events)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM company_routing_settings WHERE calling_company_id = ?", (self.company_id,)).fetchone()[0], before_settings)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'company_routing_setting'").fetchone()[0], before_setting_logs)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'routing_event' AND json_extract(new_values, '$.calling_company_id') = ?", (self.company_id,)).fetchone()[0], before_company_journal)

    def test_campaign_setting_duplicate_disable_autorotation_is_blocked(self):
        with self.assertRaisesRegex(BusinessRuleError, "авторотация уже выключена"):
            self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="disable_autorotation", provider_id=None)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM company_routing_settings WHERE calling_company_id = ?", (self.company_id,)).fetchone()[0], 0)

    def test_campaign_setting_set_campaign_route_requires_valid_geo_and_creates_setting(self):
        with self.assertRaisesRegex(BusinessRuleError, "обязателен"):
            self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="set_campaign_route", provider_id=None, new_company_route_id=None)
        with self.assertRaisesRegex(BusinessRuleError, "выбранному GEO"):
            self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="set_campaign_route", provider_id=self.provider_id, new_company_route_id=self.other_route_id)
        self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="set_campaign_route", provider_id=self.alt_provider_id, new_company_route_id=self.alt_route_id)
        active = self.conn.execute("SELECT * FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL", (self.company_id,)).fetchone()
        self.assertEqual(active["routing_mode"], "campaign_route")
        self.assertEqual(active["route_id"], self.alt_route_id)
        self.assertEqual(active["has_autorotation"], 0)

    def test_campaign_setting_set_campaign_route_versions_existing_manual_route(self):
        old_id = self.repo.create_company_routing_setting(calling_company_id=self.company_id, country_id=self.country_id, server_id=self.server_id, route_id=self.route_id, routing_mode="campaign_route", has_autorotation=False, comment="old", created_by=self.admin_id)
        self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="set_campaign_route", provider_id=self.alt_provider_id, new_company_route_id=self.alt_route_id)
        old = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (old_id,)).fetchone()
        active = self.conn.execute("SELECT * FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL", (self.company_id,)).fetchone()
        self.assertEqual(old["is_active"], 0)
        self.assertEqual(active["route_id"], self.alt_route_id)
        self.assertEqual(active["routing_mode"], "campaign_route")

    def test_campaign_setting_disable_autorotation_deactivates_when_no_manual_route(self):
        old_id = self.repo.create_company_routing_setting(calling_company_id=self.company_id, country_id=self.country_id, server_id=self.server_id, route_id=None, routing_mode="autorotation", has_autorotation=True, comment="old", created_by=self.admin_id)
        self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="disable_autorotation", provider_id=None)
        old = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (old_id,)).fetchone()
        self.assertEqual(old["is_active"], 0)
        self.assertIsNotNone(old["valid_to"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL", (self.company_id,)).fetchone()[0], 0)

    def test_campaign_setting_disable_autorotation_for_mixed_preserves_route(self):
        old_id = self.repo.create_company_routing_setting(calling_company_id=self.company_id, country_id=self.country_id, server_id=self.server_id, route_id=self.route_id, routing_mode="mixed", has_autorotation=True, comment="old", created_by=self.admin_id)
        self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="disable_autorotation", provider_id=None)
        old = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (old_id,)).fetchone()
        active = self.conn.execute("SELECT * FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL", (self.company_id,)).fetchone()
        self.assertEqual(old["is_active"], 0)
        self.assertEqual(active["route_id"], self.route_id)
        self.assertEqual(active["routing_mode"], "campaign_route")
        self.assertEqual(active["has_autorotation"], 0)

    def test_campaign_setting_set_campaign_route_for_autorotation_becomes_mixed(self):
        old_id = self.repo.create_company_routing_setting(calling_company_id=self.company_id, country_id=self.country_id, server_id=self.server_id, route_id=None, routing_mode="autorotation", has_autorotation=True, comment="old", created_by=self.admin_id)
        self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="set_campaign_route", provider_id=self.provider_id, new_company_route_id=self.route_id)
        old = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (old_id,)).fetchone()
        active = self.conn.execute("SELECT * FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL", (self.company_id,)).fetchone()
        self.assertEqual(old["is_active"], 0)
        self.assertEqual(active["route_id"], self.route_id)
        self.assertEqual(active["routing_mode"], "mixed")
        self.assertEqual(active["has_autorotation"], 1)

    def test_campaign_setting_set_campaign_route_for_mixed_preserves_autorotation(self):
        old_id = self.repo.create_company_routing_setting(calling_company_id=self.company_id, country_id=self.country_id, server_id=self.server_id, route_id=self.route_id, routing_mode="mixed", has_autorotation=True, comment="old", created_by=self.admin_id)
        self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="set_campaign_route", provider_id=self.alt_provider_id, new_company_route_id=self.alt_route_id)
        old = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (old_id,)).fetchone()
        active = self.conn.execute("SELECT * FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL", (self.company_id,)).fetchone()
        self.assertEqual(old["is_active"], 0)
        self.assertEqual(active["route_id"], self.alt_route_id)
        self.assertEqual(active["routing_mode"], "mixed")
        self.assertEqual(active["has_autorotation"], 1)

    def test_campaign_setting_set_campaign_route_rejects_same_active_route(self):
        self.repo.create_company_routing_setting(calling_company_id=self.company_id, country_id=self.country_id, server_id=self.server_id, route_id=self.route_id, routing_mode="campaign_route", has_autorotation=False, comment="old", created_by=self.admin_id)
        with self.assertRaisesRegex(BusinessRuleError, "уже прописан"):
            self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="set_campaign_route", provider_id=self.provider_id, new_company_route_id=self.route_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM company_routing_settings WHERE calling_company_id = ?", (self.company_id,)).fetchone()[0], 1)

    def test_campaign_setting_remove_route_for_mixed_preserves_autorotation(self):
        old_id = self.repo.create_company_routing_setting(calling_company_id=self.company_id, country_id=self.country_id, server_id=self.server_id, route_id=self.route_id, routing_mode="mixed", has_autorotation=True, comment="old", created_by=self.admin_id)
        self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="remove_campaign_route", provider_id=None)
        old = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (old_id,)).fetchone()
        active = self.conn.execute("SELECT * FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL", (self.company_id,)).fetchone()
        self.assertEqual(old["is_active"], 0)
        self.assertIsNone(active["route_id"])
        self.assertEqual(active["routing_mode"], "autorotation")
        self.assertEqual(active["has_autorotation"], 1)

    def test_campaign_setting_remove_route_and_server_priority_deactivate_or_log_without_active_setting(self):
        old_id = self.repo.create_company_routing_setting(calling_company_id=self.company_id, country_id=self.country_id, server_id=self.server_id, route_id=self.route_id, routing_mode="campaign_route", has_autorotation=False, comment="old", created_by=self.admin_id)
        self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="remove_campaign_route", provider_id=None)
        old = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (old_id,)).fetchone()
        self.assertEqual(old["is_active"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL", (self.company_id,)).fetchone()[0], 0)
        event_id = self.create_event(apply_scope="campaign_setting", calling_company_id=self.company_id, company_change_type="remove_campaign_route", provider_id=None)
        self.assertIsNotNone(self.conn.execute("SELECT id FROM routing_events WHERE id = ?", (event_id,)).fetchone())

    def test_deactivation_does_not_roll_back_server_priority(self):
        event_id = self.create_event(apply_scope="server_priority", country_id=self.country_id, server_id=self.server_id, new_route_id=self.route_id)
        self.repo.deactivate_routing_event(event_id, reason="ошибка записи", deactivated_by=self.admin_id)
        priority = self.conn.execute("SELECT * FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (self.country_id, self.server_id)).fetchone()
        self.assertEqual(priority["current_route_id"], self.route_id)
        self.assertEqual(self.conn.execute("SELECT is_active FROM routing_events WHERE id = ?", (event_id,)).fetchone()[0], 0)

    def test_editing_event_updates_comment_only_and_does_not_reapply_server_priority(self):
        event_id = self.create_event(apply_scope="server_priority", country_id=self.country_id, server_id=self.server_id, new_route_id=self.route_id)
        self.repo.update_routing_event(event_id, event_at="2026-06-11 13:00", reason="Другое", comment="Исправили описание", country_id=self.country_id, server_id=self.server_id, provider_id=self.provider_id, affected_route_id=None, old_route_id=None, new_route_id=self.alt_route_id, calling_company_id=None, company_change_type=None, new_company_routing_mode=None, new_company_route_id=None, new_company_has_autorotation=None, updated_by=self.admin_id)
        priority = self.conn.execute("SELECT * FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (self.country_id, self.server_id)).fetchone()
        event = self.conn.execute("SELECT * FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        self.assertEqual(priority["current_route_id"], self.route_id)
        self.assertEqual(event["new_route_id"], self.route_id)
        self.assertEqual(event["comment"], "Исправили описание")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM change_log WHERE change_type = 'routing_event.comment_updated'").fetchone()[0], 1)

    def test_editing_campaign_setting_event_syncs_active_routing_comment_only(self):
        event_id = self.create_event(
            apply_scope="campaign_setting",
            calling_company_id=self.company_id,
            company_change_type="set_campaign_route",
            provider_id=self.alt_provider_id,
            new_company_route_id=self.alt_route_id,
            comment="старый комментарий маршрута",
        )
        active_before = self.conn.execute(
            "SELECT * FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL",
            (self.company_id,),
        ).fetchone()
        before_event_count = self.conn.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0]
        before_routing_log_count = self.conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'company_routing_setting'").fetchone()[0]

        self.repo.update_routing_event(event_id, comment="новый комментарий маршрута", updated_by=self.admin_id)

        active_after = self.conn.execute(
            "SELECT * FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL",
            (self.company_id,),
        ).fetchone()
        event = self.conn.execute("SELECT * FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        self.assertEqual(event["comment"], "новый комментарий маршрута")
        self.assertEqual(active_after["comment"], "новый комментарий маршрута")
        self.assertEqual(active_after["id"], active_before["id"])
        self.assertEqual(active_after["routing_mode"], active_before["routing_mode"])
        self.assertEqual(active_after["has_autorotation"], active_before["has_autorotation"])
        self.assertEqual(active_after["route_id"], active_before["route_id"])
        self.assertEqual(active_after["valid_from"], active_before["valid_from"])
        self.assertEqual(active_after["valid_to"], active_before["valid_to"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0], before_event_count)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'company_routing_setting'").fetchone()[0], before_routing_log_count)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM change_log WHERE change_type = 'routing_event.comment_updated'").fetchone()[0], 1)

    def test_editing_older_campaign_setting_event_does_not_overwrite_newer_setting_comment(self):
        first_event_id = self.create_event(
            apply_scope="campaign_setting",
            calling_company_id=self.company_id,
            company_change_type="enable_autorotation",
            provider_id=None,
            comment="первый комментарий",
        )
        self.create_event(
            event_at="2026-06-11 12:00",
            apply_scope="campaign_setting",
            calling_company_id=self.company_id,
            company_change_type="set_campaign_route",
            provider_id=self.alt_provider_id,
            new_company_route_id=self.alt_route_id,
            comment="последний комментарий",
        )

        self.repo.update_routing_event(first_event_id, comment="обновили старое событие", updated_by=self.admin_id)

        active = self.conn.execute(
            "SELECT * FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL",
            (self.company_id,),
        ).fetchone()
        self.assertEqual(active["comment"], "последний комментарий")
        self.assertEqual(active["routing_mode"], "mixed")
        self.assertEqual(active["has_autorotation"], 1)
        self.assertEqual(active["route_id"], self.alt_route_id)

    def test_snapshot_json_is_saved(self):
        event_id = self.create_event(apply_scope="server_priority", country_id=self.country_id, server_id=self.server_id, new_route_id=self.route_id)
        snapshot = self.conn.execute("SELECT snapshot_json FROM routing_events WHERE id = ?", (event_id,)).fetchone()[0]
        self.assertIn("Мексика", snapshot)
        self.assertIn("Sancom", snapshot)


class RepositoryHlrUsageConfigTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        self.repo = Repository(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_get_hlr_daily_usage_returns_zero_when_missing(self):
        usage = self.repo.get_hlr_daily_usage("2026-07-12")

        self.assertEqual(usage["usage_date"], "2026-07-12")
        self.assertEqual(usage["checked_today"], 0)
        self.assertIsNone(usage["credits_spent_today"])
        self.assertEqual(usage["last_check_count"], 0)
        self.assertIsNone(usage["last_check_credits"])
        self.assertIsNone(usage["updated_at"])

    def test_upsert_hlr_daily_usage_inserts_new_day(self):
        usage = self.repo.upsert_hlr_daily_usage("2026-07-12", 3, 1.5, "2026-07-12 10:30")

        self.assertEqual(usage["checked_today"], 3)
        self.assertEqual(float(usage["credits_spent_today"]), 1.5)
        self.assertEqual(usage["last_check_count"], 3)
        self.assertEqual(float(usage["last_check_credits"]), 1.5)
        self.assertEqual(usage["updated_at"], "2026-07-12 10:30")

    def test_upsert_hlr_daily_usage_updates_existing_day(self):
        self.repo.upsert_hlr_daily_usage("2026-07-12", 3, 1, "2026-07-12 10:30")

        usage = self.repo.upsert_hlr_daily_usage("2026-07-12", 2, 0.5, "2026-07-12 11:00")

        rows = self.conn.execute("SELECT * FROM hlr_daily_usage WHERE usage_date = ?", ("2026-07-12",)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(usage["checked_today"], 5)
        self.assertEqual(float(usage["credits_spent_today"]), 1.5)
        self.assertEqual(usage["last_check_count"], 2)
        self.assertEqual(float(usage["last_check_credits"]), 0.5)
        self.assertEqual(usage["updated_at"], "2026-07-12 11:00")

    def test_get_hlr_limit_override_returns_current_value(self):
        self.repo.set_hlr_limit_override(1000)

        self.assertEqual(self.repo.get_hlr_limit_override(), "1000")

    def test_set_hlr_limit_override_updates_existing_value(self):
        self.repo.set_hlr_limit_override(1000)
        self.repo.set_hlr_limit_override(2000)

        rows = self.conn.execute("SELECT value FROM app_settings WHERE key = ?", ("hlr_daily_limit_override",)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], "2000")

    def test_set_hlr_limit_override_can_clear_value(self):
        self.repo.set_hlr_limit_override(1000)

        self.repo.set_hlr_limit_override(None)

        self.assertIsNone(self.repo.get_hlr_limit_override())

if __name__ == "__main__":
    unittest.main()
