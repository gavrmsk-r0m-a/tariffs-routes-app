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


    def test_phone_preview_create_update_duplicate_and_apply_block(self):
        csv_create = "country,provider,project,number,assignment_type,Итоговый статус\nИталия,Miatel,Alpha,393331239001,gl,Используется\n"
        preview = preview_import(self.conn, "phone_numbers", csv_create)
        self.assertEqual(preview.rows[0]["action"], "create")
        self.assertEqual(preview.rows[0]["working_status"], "used")
        self.assertEqual(preview.rows[0]["active_provider"], "Да")
        apply_import(self.conn, "phone_numbers", csv_create, user_id=self.admin_id)
        preview_update = preview_import(self.conn, "phone_numbers", csv_create)
        self.assertEqual(preview_update.rows[0]["action"], "update")
        dup_csv = csv_create + "Италия,Miatel,Alpha,393331239001,gl,Используется\n"
        dup_preview = preview_import(self.conn, "phone_numbers", dup_csv)
        self.assertEqual(dup_preview.error_rows, 1)
        self.assertEqual(dup_preview.rows[1]["action"], "duplicate_in_file")
        self.assertIn("Номер уже встречался в строке 2 этого файла.", dup_preview.rows[1]["errors"])
        with self.assertRaisesRegex(BusinessRuleError, "Импорт невозможен"):
            apply_import(self.conn, "phone_numbers", dup_csv, user_id=self.admin_id)


    def test_importer_lookup_behavior_preserved(self):
        self.conn.execute("INSERT INTO phone_number_types(name, is_active) VALUES ('Mobile', 1)")
        self.conn.commit()
        csv_text = "country,provider,project,number,assignment_type,currency,phone_type,Итоговый статус\nИталия,Miatel,Alpha,393331239010,ГЛ,EUR,Mobile,Используется\n"

        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 0)
        result = apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)

        self.assertEqual(result.created_rows, 1)
        row = self.conn.execute("SELECT country_id, provider_id, currency_id, project_label, assignment_type, phone_type FROM phone_numbers WHERE number = '393331239010'").fetchone()
        self.assertEqual(row["country_id"], self.conn.execute("SELECT id FROM countries WHERE name = 'Италия'").fetchone()["id"])
        self.assertEqual(row["provider_id"], self.conn.execute("SELECT id FROM providers WHERE name = 'Miatel'").fetchone()["id"])
        self.assertEqual(row["currency_id"], self.conn.execute("SELECT id FROM currencies WHERE code = 'EUR'").fetchone()["id"])
        self.assertEqual(row["project_label"], "Alpha")
        self.assertEqual(row["assignment_type"], "gl")
        self.assertEqual(row["phone_type"], "Mobile")

    def test_importer_validation_messages_preserved(self):
        csv_text = "country,provider,project,number,assignment_type,Итоговый статус\nИталия,NoSuchProvider,Alpha,393331239011,gl,Используется\n"
        preview = preview_import(self.conn, "phone_numbers", csv_text)

        self.assertEqual(preview.error_rows, 1)
        self.assertIn("Значение ‘NoSuchProvider’ не найдено в справочнике Провайдер. Исправьте файл или добавьте значение в справочник вручную.", preview.rows[0]["errors"])

    def test_importer_preview_or_summary_preserved(self):
        csv_text = "country,provider,project,number,assignment_type,Итоговый статус\nИталия,Miatel,Alpha,393331239012,gl,Используется\n"

        preview = preview_import(self.conn, "phone_numbers", csv_text)
        result = apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)

        self.assertEqual((preview.total_rows, preview.new_rows, preview.error_rows), (1, 1, 0))
        self.assertEqual((result.created_rows, result.updated_rows, result.skipped_rows), (1, 0, 0))


    def test_dictionary_import_creates_project_and_preserves_summary(self):
        csv_text = "type,name\nproject,Stage 23 Dictionary Project\n"
        preview = preview_import(self.conn, "dictionaries", csv_text)
        result = apply_import(self.conn, "dictionaries", csv_text, user_id=self.admin_id)

        self.assertEqual((preview.total_rows, preview.new_rows, preview.error_rows), (1, 1, 0))
        self.assertEqual((result.created_rows, result.updated_rows, result.skipped_rows), (1, 0, 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM projects WHERE name = ?", ("Stage 23 Dictionary Project",)).fetchone()[0], 1)

    def test_dictionary_import_creates_phone_number_type_and_preserves_summary(self):
        csv_text = "type,name\nphone_type,Stage 23 Dictionary Type\n"
        preview = preview_import(self.conn, "dictionaries", csv_text)
        result = apply_import(self.conn, "dictionaries", csv_text, user_id=self.admin_id)

        self.assertEqual((preview.total_rows, preview.new_rows, preview.error_rows), (1, 1, 0))
        self.assertEqual((result.created_rows, result.updated_rows, result.skipped_rows), (1, 0, 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM phone_number_types WHERE name = ?", ("Stage 23 Dictionary Type",)).fetchone()[0], 1)

    def test_dictionary_import_creates_phone_assignment_type_and_preserves_summary(self):
        csv_text = "type,code,name\nphone_assignment,stage24_assignment,Stage 24 Assignment\n"
        preview = preview_import(self.conn, "dictionaries", csv_text)
        result = apply_import(self.conn, "dictionaries", csv_text, user_id=self.admin_id)

        self.assertEqual((preview.total_rows, preview.new_rows, preview.error_rows), (1, 1, 0))
        self.assertEqual((result.created_rows, result.updated_rows, result.skipped_rows), (1, 0, 0))
        row = self.conn.execute("SELECT code, name, is_active FROM phone_assignment_types WHERE code = ?", ("stage24_assignment",)).fetchone()
        self.assertEqual((row["code"], row["name"], row["is_active"]), ("stage24_assignment", "Stage 24 Assignment", 1))

    def test_dictionary_import_phone_assignment_fallback_code_preserved(self):
        csv_text = "type,name\nphone_assignment,Stage 24 Fallback Assignment\n"
        result = apply_import(self.conn, "dictionaries", csv_text, user_id=self.admin_id)

        self.assertEqual((result.created_rows, result.updated_rows, result.skipped_rows), (1, 0, 0))
        row = self.conn.execute("SELECT code, name FROM phone_assignment_types WHERE name = ?", ("Stage 24 Fallback Assignment",)).fetchone()
        self.assertEqual((row["code"], row["name"]), ("Stage 24 Fallback Assignment", "Stage 24 Fallback Assignment"))

    def test_dictionary_duplicate_phone_assignment_import_does_not_create_duplicates(self):
        csv_text = "type,code,name\nphone_assignment,stage24_duplicate,Stage 24 Duplicate Assignment\n"
        apply_import(self.conn, "dictionaries", csv_text, user_id=self.admin_id)
        preview = preview_import(self.conn, "dictionaries", csv_text)
        result = apply_import(self.conn, "dictionaries", csv_text, user_id=self.admin_id)

        self.assertEqual((preview.total_rows, preview.new_rows, preview.error_rows), (1, 1, 0))
        self.assertEqual((result.created_rows, result.updated_rows, result.skipped_rows), (1, 0, 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM phone_assignment_types WHERE code = ?", ("stage24_duplicate",)).fetchone()[0], 1)

    def test_dictionary_duplicate_import_does_not_create_duplicates(self):
        csv_text = "type,name\nproject,Stage 23 Dictionary Project\n"
        apply_import(self.conn, "dictionaries", csv_text, user_id=self.admin_id)
        preview = preview_import(self.conn, "dictionaries", csv_text)
        result = apply_import(self.conn, "dictionaries", csv_text, user_id=self.admin_id)

        self.assertEqual((preview.total_rows, preview.new_rows, preview.error_rows), (1, 1, 0))
        self.assertEqual((result.created_rows, result.updated_rows, result.skipped_rows), (1, 0, 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM projects WHERE name = ?", ("Stage 23 Dictionary Project",)).fetchone()[0], 1)

    def test_dictionary_validation_messages_preserved(self):
        preview = preview_import(self.conn, "dictionaries", "type,name\nproject,\n")

        self.assertEqual(preview.error_rows, 1)
        self.assertEqual(preview.rows[0]["message"], "type and name are required")

    def test_phone_missing_reference_errors_and_does_not_autocreate_provider(self):
        csv_text = "country,provider,project,number,assignment_type,Итоговый статус\nИталия,NoSuchProvider,Alpha,393331239002,gl,Используется\n"
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 1)
        self.assertIn("Значение ‘NoSuchProvider’ не найдено в справочнике Провайдер", preview.rows[0]["errors"])
        with self.assertRaisesRegex(BusinessRuleError, "Импорт невозможен"):
            apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertIsNone(self.conn.execute("SELECT id FROM providers WHERE name = 'NoSuchProvider'").fetchone())

    def test_phone_legacy_info_allowed_and_empty_fields_require_review(self):
        self.conn.execute("INSERT INTO phone_assignment_types(code, name, is_active) VALUES ('old', 'Old Assignment', 0)")
        self.conn.commit()
        legacy_csv = "country,provider,project,number,assignment_type,Итоговый статус\nИталия,Miatel,Alpha,393331239003,old,Используется\n"
        legacy_preview = preview_import(self.conn, "phone_numbers", legacy_csv)
        self.assertEqual(legacy_preview.error_rows, 0)
        self.assertEqual(legacy_preview.legacy_info_rows, 1)
        self.assertIn("legacy", legacy_preview.rows[0]["info"])
        self.assertEqual(legacy_preview.rows[0]["review_required"], "Нет")
        empty_csv = "country,provider,project,number,assignment_type,Итоговый статус\nИталия,,,393331239004,,???\n"
        empty_preview = preview_import(self.conn, "phone_numbers", empty_csv)
        self.assertEqual(empty_preview.error_rows, 0)
        self.assertEqual(empty_preview.rows[0]["review_required"], "Да")
        self.assertIn("пустой провайдер", empty_preview.rows[0]["review_reasons"])
        self.assertIn("пустой проект", empty_preview.rows[0]["review_reasons"])
        self.assertIn("пустое назначение", empty_preview.rows[0]["review_reasons"])
        self.assertEqual(empty_preview.rows[0]["working_status"], "unknown")
        self.assertEqual(empty_preview.rows[0]["active_provider"], "Да")
        result = apply_import(self.conn, "phone_numbers", empty_csv, user_id=self.admin_id)
        self.assertEqual(result.created_rows, 1)
        row = self.conn.execute("SELECT review_required, provider_id, project_label, assignment_type FROM phone_numbers WHERE number = '393331239004'").fetchone()
        self.assertEqual((row["review_required"], row["provider_id"], row["project_label"], row["assignment_type"]), (1, None, None, None))

    def test_replace_section_is_blocked_for_imports(self):
        with self.assertRaisesRegex(BusinessRuleError, "Режим замены раздела временно отключён"):
            apply_import(self.conn, "phone_numbers", "country,number\nИталия,393331239005\n", user_id=self.admin_id, mode="replace_section")

    def test_importer_exists_cleanup_preserves_route_update_preview_and_summary(self):
        country_id = self.repo.create_country("Испания")
        provider_id = self.conn.execute("SELECT id FROM providers WHERE name = ?", ("Miatel",)).fetchone()["id"]
        self.repo.create_route(country_id=country_id, provider_id=provider_id, name="Spain Route", cli_source_type="other", cli_source_label="OTHER", created_by=self.admin_id)
        csv_text = "country,name,provider,comment\nИспания,Spain Route,Miatel,Updated comment\n"

        preview = preview_import(self.conn, "routes", csv_text)
        result = apply_import(self.conn, "routes", csv_text, user_id=self.admin_id)

        self.assertEqual((preview.total_rows, preview.duplicate_rows, preview.new_rows, preview.error_rows), (1, 1, 0, 0))
        self.assertEqual((result.created_rows, result.updated_rows, result.skipped_rows), (0, 1, 0))
        self.assertEqual(preview.rows[0]["status"], "duplicate_in_db")
        self.assertEqual(preview.rows[0]["action"], "update")
        self.assertEqual(self.conn.execute("SELECT comment FROM routes WHERE name = 'Spain Route'").fetchone()["comment"], "Updated comment")

    def test_importer_exists_cleanup_preserves_phone_update_preview_and_summary(self):
        create_csv = "country,provider,project,number,assignment_type,Итоговый статус,comment\nИталия,Miatel,Alpha,393331239020,gl,Используется,Initial\n"
        apply_import(self.conn, "phone_numbers", create_csv, user_id=self.admin_id)
        update_csv = "country,provider,project,number,assignment_type,Итоговый статус,comment\nИталия,Miatel,Alpha,393331239020,gl,Используется,Updated\n"

        preview = preview_import(self.conn, "phone_numbers", update_csv)
        result = apply_import(self.conn, "phone_numbers", update_csv, user_id=self.admin_id)

        self.assertEqual((preview.total_rows, preview.duplicate_rows, preview.new_rows, preview.error_rows), (1, 1, 0, 0))
        self.assertEqual((result.created_rows, result.updated_rows, result.skipped_rows, result.error_rows), (0, 1, 0, 0))
        self.assertEqual(preview.rows[0]["message"], "Строка обновит существующий номер. ('393331239020',)")
        self.assertEqual(self.conn.execute("SELECT comment FROM phone_numbers WHERE number = '393331239020'").fetchone()["comment"], "Updated")

    def test_importer_exists_cleanup_preserves_calling_company_update_preview_and_summary(self):
        csv_text = "server,country,company_name,company_id_external,has_autorotation,is_active,comment\nEU1,Италия,Company One,cc-1,no,yes,Initial\n"
        apply_import(self.conn, "calling_companies", csv_text, user_id=self.admin_id)
        update_csv = "server,country,company_name,company_id_external,has_autorotation,is_active,comment\nEU1,Италия,Company One Updated,cc-1,yes,yes,Updated\n"

        preview = preview_import(self.conn, "calling_companies", update_csv)
        result = apply_import(self.conn, "calling_companies", update_csv, user_id=self.admin_id)

        self.assertEqual((preview.duplicate_rows, preview.new_rows, preview.error_rows), (1, 0, 0))
        self.assertEqual((result.created_rows, result.updated_rows, result.skipped_rows), (0, 1, 0))
        self.assertEqual(preview.rows[0]["status"], "duplicate_in_db")
        row = self.conn.execute("SELECT company_name, has_autorotation, comment FROM calling_companies WHERE company_id_external = 'cc-1'").fetchone()
        self.assertEqual((row["company_name"], row["has_autorotation"], row["comment"]), ("Company One Updated", 1, "Updated"))

    def test_importer_exists_cleanup_preserves_tariff_update_preview_and_summary(self):
        csv_text = "country,provider,prefix,currency,price,rate,rate_date,comment\nИталия,Miatel,,EUR,0.10,1,2026-07-14,Initial\n"
        apply_import(self.conn, "tariffs", csv_text, user_id=self.admin_id)
        update_csv = "country,provider,prefix,currency,price,rate,rate_date,comment\nИталия,Miatel,,EUR,0.20,1,2026-07-14,Updated\n"

        preview = preview_import(self.conn, "tariffs", update_csv)
        result = apply_import(self.conn, "tariffs", update_csv, user_id=self.admin_id)

        self.assertEqual((preview.duplicate_rows, preview.new_rows, preview.error_rows), (1, 0, 0))
        self.assertEqual((result.created_rows, result.updated_rows, result.skipped_rows), (0, 0, 1))
        self.assertEqual(preview.rows[0]["status"], "duplicate_in_db")
        rows = self.conn.execute("SELECT price_in_provider_currency, is_current FROM tariffs WHERE country_id = (SELECT id FROM countries WHERE name = 'Италия') ORDER BY id").fetchall()
        self.assertEqual([(str(row["price_in_provider_currency"]), row["is_current"]) for row in rows], [("0.1", 0)])

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

    def test_replace_phone_numbers_is_forbidden_and_does_not_clear_section(self):
        apply_import(self.conn, "phone_numbers", "country,project,number,assignment_type,status\nA,Alpha,1111111,gl,used\n", user_id=self.admin_id)
        with self.assertRaisesRegex(BusinessRuleError, "Режим замены раздела временно отключён"):
            apply_import(self.conn, "phone_numbers", "country,project,number,assignment_type,status\nB,Alpha,2222222,gl,used\n", user_id=self.admin_id, mode="replace_section")
        rows = self.conn.execute("SELECT number FROM phone_numbers ORDER BY number").fetchall()
        self.assertEqual([row["number"] for row in rows], ["1111111"])

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
