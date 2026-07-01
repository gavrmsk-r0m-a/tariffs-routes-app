import sqlite3
import unittest

from app.db import init_db
from app.importer import apply_import, preview_import
from app.repository import BusinessRuleError, Repository


class ImporterTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        self.repo = Repository(self.conn)
        self.repo.create_country('Италия')
        self.repo.create_country('A')
        self.repo.create_country('B')
        self.repo.create_currency('EUR', 'EUR')
        usd_id = self.repo.create_currency('USD', 'USD')
        self.repo.create_provider('Miatel', default_currency_id=usd_id)
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Alpha', 1)")
        self.conn.commit()
        self.admin_id = self.repo.create_user("admin", "Admin")

    def tearDown(self):
        self.conn.close()

    def test_routes_preview_detects_duplicate_business_key(self):
        country_id = self.repo.create_country("Мексика")
        provider_id = self.conn.execute("SELECT id FROM providers WHERE name = ?", ("Miatel",)).fetchone()["id"]
        self.repo.create_route(
            country_id=country_id,
            provider_id=provider_id,
            name="Мексика/Miatel/Pool_A@",
            cli_source_type="pool",
            cli_source_label="Pool_A",
            created_by=self.admin_id,
        )
        csv_text = "country,name,provider,cli_source_type,cli_source_label\nМексика,Мексика/Miatel/Pool_A@,Miatel,pool,Pool_A\nМексика,Мексика/Sancom/RND@,Sancom,rnd,RND\n"
        preview = preview_import(self.conn, "routes", csv_text)
        self.assertEqual(preview.total_rows, 2)
        self.assertEqual(preview.duplicate_rows, 1)
        self.assertEqual(preview.new_rows, 1)
        self.assertIn("duplicate_in_db", {row["status"] for row in preview.rows})

    def test_phone_import_rejects_invalid_numbers_and_imports_valid(self):
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Competitors', 1)")
        self.conn.commit()
        csv_text = "country,project,number,assignment_type,status\nИталия,Competitors,+393331234567,gl,used\nИталия,Competitors,393331234568,gl,used\n"
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 1)
        self.assertEqual(preview.new_rows, 1)
        with self.assertRaises(BusinessRuleError):
            apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        rows = self.conn.execute("SELECT number FROM phone_numbers").fetchall()
        self.assertEqual([row["number"] for row in rows], [])

    def test_calling_company_import_requires_external_id(self):
        csv_text = "server,country,company_name,company_id_external,has_autorotation\nEU1,Мексика,CC No Id,,yes\nEU1,Мексика,CC Good,1001,yes\n"
        preview = preview_import(self.conn, "calling_companies", csv_text)
        self.assertEqual(preview.error_rows, 1)
        self.assertEqual(preview.new_rows, 1)
        apply_import(self.conn, "calling_companies", csv_text, user_id=self.admin_id)
        rows = self.conn.execute("SELECT company_id_external FROM calling_companies").fetchall()
        self.assertEqual([row["company_id_external"] for row in rows], ["1001"])

    def test_phone_import_supports_extended_fields_and_reference_validation(self):
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Competitors', 1)")
        self.conn.execute("INSERT INTO phone_number_types(name, is_active) VALUES ('Mobile', 1)")
        self.conn.commit()
        csv_text = "number;country;provider;project;assignment_type;status;is_active;connection_fee;monthly_fee;currency;phone_type;tariff_label;comment;created_at\n393331234567;Италия;Miatel;Competitors;ГЛ;used;нет;12.50;3.25;USD;Mobile;Tariff A;Imported;2026-06-01 10:00:00\n"
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 0)
        result = apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertEqual(result.created_rows, 1)
        row = self.conn.execute("SELECT * FROM phone_numbers WHERE number = '393331234567'").fetchone()
        self.assertEqual(row["project_label"], "Competitors")
        self.assertEqual(row["assignment_type"], "gl")
        self.assertEqual(str(row["connection_cost"]), "12.5")
        self.assertEqual(str(row["monthly_fee"]), "3.25")
        self.assertEqual(row["phone_type"], "Mobile")
        self.assertEqual(row["tariff_label"], "Tariff A")
        self.assertEqual(row["created_at"], "2026-06-01 10:00:00")
        self.assertIsNotNone(row["deactivated_at"])

        bad_preview = preview_import(self.conn, "phone_numbers", "number;country;project;assignment_type\n393331234568;Италия;NoSuchProject;ГЛ\n")
        self.assertEqual(bad_preview.error_rows, 1)


    def test_phone_import_saves_excel_created_by_and_keeps_audit_user(self):
        self.repo.create_country('Мексика')
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Мех. деп.', 1)")
        self.conn.commit()
        csv_text = "Номер,Страна,Провайдер,Проект,Назначение,Итоговый статус,АП в EUR,Создал\n52555000201,Мексика,Miatel,Мех. деп.,АОН,Используется,10,old_admin\n"
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 0)
        self.assertIn("Создал в Excel: old_admin", preview.rows[0]["message"])
        apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        row = self.conn.execute("SELECT id, imported_created_by, review_required FROM phone_numbers WHERE number = '52555000201'").fetchone()
        self.assertEqual(row["imported_created_by"], "old_admin")
        self.assertEqual(row["review_required"], 0)
        history = self.conn.execute("SELECT changed_by, comment FROM phone_number_history WHERE phone_number_id = ? AND action = 'created'", (row["id"],)).fetchone()
        self.assertEqual(history["changed_by"], self.admin_id)
        self.assertIn("Создал в Excel: old_admin", history["comment"])

    def test_phone_import_missing_or_empty_created_by_does_not_fail_or_require_review(self):
        csv_missing = "country,provider,project,number,assignment_type,Итоговый статус\nИталия,Miatel,Alpha,393331234593,gl,Используется\n"
        csv_empty = "country,provider,project,number,assignment_type,Итоговый статус,Создал\nИталия,Miatel,Alpha,393331234594,gl,Используется,\n"
        self.assertEqual(preview_import(self.conn, "phone_numbers", csv_missing).error_rows, 0)
        self.assertEqual(preview_import(self.conn, "phone_numbers", csv_empty).error_rows, 0)
        apply_import(self.conn, "phone_numbers", csv_missing, user_id=self.admin_id)
        apply_import(self.conn, "phone_numbers", csv_empty, user_id=self.admin_id)
        rows = self.conn.execute("SELECT number, imported_created_by, review_required FROM phone_numbers WHERE number IN ('393331234593', '393331234594') ORDER BY number").fetchall()
        self.assertEqual([(r["imported_created_by"], r["review_required"]) for r in rows], [(None, 0), (None, 0)])

    def test_phone_import_update_created_by_behaviour(self):
        create_csv = "country,provider,project,number,assignment_type,Итоговый статус,Создал\nИталия,Miatel,Alpha,393331234595,gl,Используется,legacy_one\n"
        apply_import(self.conn, "phone_numbers", create_csv, user_id=self.admin_id)
        missing_csv = "country,provider,project,number,assignment_type,Итоговый статус\nИталия,Miatel,Alpha,393331234595,gl,Используется\n"
        apply_import(self.conn, "phone_numbers", missing_csv, user_id=self.admin_id)
        self.assertEqual(self.conn.execute("SELECT imported_created_by FROM phone_numbers WHERE number = '393331234595'").fetchone()["imported_created_by"], "legacy_one")
        empty_csv = "country,provider,project,number,assignment_type,Итоговый статус,Создал\nИталия,Miatel,Alpha,393331234595,gl,Используется,\n"
        apply_import(self.conn, "phone_numbers", empty_csv, user_id=self.admin_id)
        self.assertEqual(self.conn.execute("SELECT imported_created_by FROM phone_numbers WHERE number = '393331234595'").fetchone()["imported_created_by"], "legacy_one")
        update_csv = "country,provider,project,number,assignment_type,Итоговый статус,Создал\nИталия,Miatel,Alpha,393331234595,gl,Используется,legacy_two\n"
        apply_import(self.conn, "phone_numbers", update_csv, user_id=self.admin_id)
        row = self.conn.execute("SELECT id, imported_created_by FROM phone_numbers WHERE number = '393331234595'").fetchone()
        self.assertEqual(row["imported_created_by"], "legacy_two")
        history = self.conn.execute("SELECT changed_by, comment FROM phone_number_history WHERE phone_number_id = ? AND action = 'updated' ORDER BY id DESC", (row["id"],)).fetchone()
        self.assertEqual(history["changed_by"], self.admin_id)
        self.assertIn("Создал в Excel: было legacy_one, стало legacy_two", history["comment"])


    def test_phone_import_maps_excel_final_statuses(self):
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Competitors', 1)")
        self.conn.commit()
        csv_text = """country,provider,project,number,assignment_type,Итоговый статус
Италия,Miatel,Competitors,393331234580,gl,Отключен
Италия,Miatel,Competitors,393331234581,gl,???
Италия,Miatel,Competitors,393331234582,gl,Не используется
Италия,Miatel,Competitors,393331234583,gl,Не нужен
Италия,Miatel,Competitors,393331234584,gl,Свободен
Италия,Miatel,Competitors,393331234585,gl,Используется
"""
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 0)
        result = apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertEqual(result.created_rows, 6)
        rows = self.conn.execute("SELECT number, status, is_active, review_required FROM phone_numbers WHERE number BETWEEN '393331234580' AND '393331234585' ORDER BY number").fetchall()
        self.assertEqual([(r["status"], r["is_active"], r["review_required"]) for r in rows], [
            ("unused", 0, 0),
            ("unknown", 1, 1),
            ("unknown", 1, 1),
            ("unknown", 1, 1),
            ("unknown", 1, 1),
            ("used", 1, 0),
        ])

    def test_phone_import_rejects_empty_or_unknown_excel_final_status(self):
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Competitors', 1)")
        self.conn.commit()
        csv_text = """country,project,number,assignment_type,Итоговый статус
Италия,Competitors,393331234586,gl,
Италия,Competitors,393331234587,gl,Другое
"""
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 2)
        with self.assertRaises(BusinessRuleError):
            apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)

    def test_phone_import_uses_ap_eur_and_ignores_ap(self):
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Competitors', 1)")
        self.conn.commit()
        csv_text = """country;provider;project;number;assignment_type;Итоговый статус;АП;АП в EUR
Италия;Miatel;Competitors;393331234588;gl;Используется;999;46,63
Италия;Miatel;Competitors;393331234589;gl;Используется;999;?
Италия;Miatel;Competitors;393331234590;gl;Используется;999;Неизвестно
Италия;Miatel;Competitors;393331234591;gl;Используется;999;
Италия;Miatel;Competitors;393331234592;gl;Используется;999;-
"""
        result = apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertEqual(result.created_rows, 5)
        rows = self.conn.execute("SELECT number, monthly_fee, review_required FROM phone_numbers WHERE number BETWEEN '393331234588' AND '393331234592' ORDER BY number").fetchall()
        self.assertEqual(str(rows[0]["monthly_fee"]), "46.63")
        self.assertTrue(all(row["monthly_fee"] is None for row in rows[1:]))
        self.assertTrue(all(row["review_required"] == 0 for row in rows))

    def test_phone_import_requires_country_project_and_number(self):
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Competitors', 1)")
        self.conn.commit()
        cases = [
            "project,number\nCompetitors,393331234569\n",
            "country,project\nИталия,Competitors\n",
        ]
        for csv_text in cases:
            with self.subTest(csv_text=csv_text):
                preview = preview_import(self.conn, "phone_numbers", csv_text)
                self.assertEqual(preview.error_rows, 1)

    def test_phone_import_allows_empty_provider_and_sets_review_required(self):
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Competitors', 1)")
        self.conn.commit()
        csv_text = "country,project,number,provider,assignment_type,status,is_active\nИталия,Competitors,393331234570,, , , \n"
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 0)
        result = apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertEqual(result.created_rows, 1)
        row = self.conn.execute("SELECT provider_id, review_required, assignment_type, status, is_active FROM phone_numbers WHERE number = '393331234570'").fetchone()
        self.assertIsNone(row["provider_id"])
        self.assertEqual(row["review_required"], 1)
        self.assertIsNone(row["assignment_type"])
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["is_active"], 1)

    def test_phone_import_with_provider_resolves_provider_without_review_flag(self):
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Competitors', 1)")
        self.conn.commit()
        csv_text = "country,project,number,provider\nИталия,Competitors,393331234571,Miatel\n"
        result = apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertEqual(result.created_rows, 1)
        row = self.conn.execute("""
            SELECT pn.review_required, p.name AS provider_name
            FROM phone_numbers pn JOIN providers p ON p.id = pn.provider_id
            WHERE pn.number = '393331234571'
        """).fetchone()
        self.assertEqual(row["provider_name"], "Miatel")
        self.assertEqual(row["review_required"], 1)


    def test_phone_import_maps_old_statuses_to_new_statuses(self):
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Competitors', 1)")
        self.conn.commit()
        csv_text = "country,project,number,assignment_type,status\nИталия,Competitors,393331234573,gl,reserved\nИталия,Competitors,393331234574,gl,blocked\nИталия,Competitors,393331234575,gl,disabled\n"
        result = apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertEqual(result.created_rows, 3)
        rows = self.conn.execute("SELECT number, status FROM phone_numbers WHERE number IN ('393331234573', '393331234574', '393331234575') ORDER BY number").fetchall()
        self.assertEqual([(row["number"], row["status"]) for row in rows], [("393331234573", "free"), ("393331234574", "problem"), ("393331234575", "problem")])

    def test_phone_import_without_created_at_uses_timestamp(self):
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Competitors', 1)")
        self.conn.commit()
        apply_import(self.conn, "phone_numbers", "country,project,number\nИталия,Competitors,393331234572\n", user_id=self.admin_id)
        row = self.conn.execute("SELECT created_at FROM phone_numbers WHERE number = '393331234572'").fetchone()
        self.assertIsNotNone(row["created_at"])

    def test_tariff_replace_section_is_forbidden(self):
        with self.assertRaises(BusinessRuleError):
            apply_import(self.conn, "tariffs", "country,provider\nМексика,Miatel\n", user_id=self.admin_id, mode="replace_section")


if __name__ == "__main__":
    unittest.main()

class ImportReplaceModeTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        self.repo = Repository(self.conn)
        self.repo.create_country('A')
        self.repo.create_country('B')
        self.repo.create_currency('EUR', 'EUR')
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Alpha', 1)")
        self.conn.commit()
        self.admin_id = self.repo.create_user("admin2", "Admin")

    def tearDown(self):
        self.conn.close()

    def test_replace_phone_numbers_clears_only_phone_section(self):
        apply_import(self.conn, "phone_numbers", "country,project,number,assignment_type,status\nA,Alpha,1111111,gl,used\n", user_id=self.admin_id)
        apply_import(self.conn, "phone_numbers", "country,project,number,assignment_type,status\nB,Alpha,2222222,gl,used\n", user_id=self.admin_id, mode="replace_section")
        rows = self.conn.execute("SELECT number FROM phone_numbers ORDER BY number").fetchall()
        self.assertEqual([row["number"] for row in rows], ["2222222"])

class PhoneNumbersProductionSafeImportTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        self.repo = Repository(self.conn)
        self.repo.create_country("Мексика")
        self.repo.create_currency("EUR", "EUR")
        self.repo.create_provider("Miatel")
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Мех. деп.', 1), ('Legacy project', 0)")
        self.conn.execute("INSERT INTO phone_assignment_types(code, name, is_active) VALUES ('leaflets', 'Leaflets', 0)")
        self.conn.commit()
        self.admin_id = self.repo.create_user("import-admin", "Import Admin")

    def tearDown(self):
        self.conn.close()

    def test_active_reference_imports_without_review_required(self):
        csv_text = "Номер,Страна,Провайдер,Проект,Назначение,Итоговый статус,АП в EUR\n52555000101,Мексика,Miatel,Мех. деп.,АОН,Используется,10\n"
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 0)
        result = apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertEqual(result.created_rows, 1)
        row = self.conn.execute("SELECT assignment_type, review_required FROM phone_numbers WHERE number = '52555000101'").fetchone()
        self.assertEqual(row["assignment_type"], "aon")
        self.assertEqual(row["review_required"], 0)

    def test_inactive_legacy_reference_imports_and_does_not_set_review_required(self):
        csv_text = "Номер,Страна,Провайдер,Проект,Назначение,Итоговый статус\n52555000102,Мексика,Miatel,Legacy project,Leaflets,Используется\n"
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 0)
        self.assertIn("историческое", preview.rows[0]["message"])
        result = apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertEqual(result.created_rows, 1)
        row = self.conn.execute("SELECT project_label, assignment_type, review_required FROM phone_numbers WHERE number = '52555000102'").fetchone()
        self.assertEqual(row["project_label"], "Legacy project")
        self.assertEqual(row["assignment_type"], "leaflets")
        self.assertEqual(row["review_required"], 0)

    def test_missing_reference_blocks_apply_and_is_not_created(self):
        csv_text = "Номер,Страна,Провайдер,Проект,Назначение,Итоговый статус\n52555000103,Мексика,Maitell,Мех. деп.,Leafletss,Используется\n"
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 1)
        with self.assertRaises(BusinessRuleError):
            apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM providers WHERE name = 'Maitell'").fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM phone_assignment_types WHERE name = 'Leafletss'").fetchone())

    def test_empty_provider_project_assignment_import_as_null_review_and_no_placeholder_dictionaries(self):
        csv_text = "Номер,Страна,Провайдер,Проект,Назначение,Итоговый статус\n52555000104,Мексика,,,,Используется\n"
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 0)
        self.assertIn("пустой провайдер", preview.rows[0]["message"])
        self.assertIn("пустой проект", preview.rows[0]["message"])
        self.assertIn("пустое назначение", preview.rows[0]["message"])
        result = apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertEqual(result.created_rows, 1)
        row = self.conn.execute("SELECT provider_id, project_label, assignment_type, review_required FROM phone_numbers WHERE number = '52555000104'").fetchone()
        self.assertIsNone(row["provider_id"])
        self.assertIsNone(row["project_label"])
        self.assertIsNone(row["assignment_type"])
        self.assertEqual(row["review_required"], 1)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM providers WHERE name = 'Unknown'").fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM phone_assignment_types WHERE name = 'Пустые'").fetchone())

    def test_duplicate_number_inside_file_blocks_apply(self):
        csv_text = "Номер,Страна,Провайдер,Проект,Назначение,Итоговый статус\n52555000105,Мексика,Miatel,Мех. деп.,АОН,Используется\n52555000105,Мексика,Miatel,Мех. деп.,АОН,Используется\n"
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 1)
        self.assertEqual(preview.rows[1]["status"], "duplicate_in_file")
        with self.assertRaises(BusinessRuleError):
            apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS c FROM phone_numbers WHERE number = '52555000105'").fetchone()["c"], 0)

    def test_apply_revalidates_references_before_writing(self):
        csv_text = "Номер,Страна,Провайдер,Проект,Назначение,Итоговый статус\n52555000106,Мексика,Miatel,Мех. деп.,АОН,Используется\n"
        self.assertEqual(preview_import(self.conn, "phone_numbers", csv_text).error_rows, 0)
        self.conn.execute("DELETE FROM providers WHERE name = 'Miatel'")
        self.conn.commit()
        with self.assertRaises(BusinessRuleError):
            apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM phone_numbers WHERE number = '52555000106'").fetchone())
