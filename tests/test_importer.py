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
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Alpha', 1)")
        self.conn.commit()
        self.repo = Repository(self.conn)
        self.admin_id = self.repo.create_user("admin", "Admin")

    def tearDown(self):
        self.conn.close()

    def test_routes_preview_detects_duplicate_business_key(self):
        country_id = self.repo.create_country("Мексика")
        provider_id = self.repo.create_provider("Miatel")
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
        csv_text = "country,project,number,assignment_type,status\nИталия,Competitors,+393331234567,pool_number,used\nИталия,Competitors,393331234568,pool_number,used\n"
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 1)
        self.assertEqual(preview.new_rows, 1)
        apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        rows = self.conn.execute("SELECT number FROM phone_numbers").fetchall()
        self.assertEqual([row["number"] for row in rows], ["393331234568"])

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
        csv_text = "number;country;provider;project;assignment_type;status;is_active;connection_fee;monthly_fee;currency;phone_type;tariff_label;comment;created_at\n393331234567;Италия;Miatel;Competitors;Номер из пула;used;нет;12.50;3.25;USD;Mobile;Tariff A;Imported;2026-06-01 10:00:00\n"
        preview = preview_import(self.conn, "phone_numbers", csv_text)
        self.assertEqual(preview.error_rows, 0)
        result = apply_import(self.conn, "phone_numbers", csv_text, user_id=self.admin_id)
        self.assertEqual(result.created_rows, 1)
        row = self.conn.execute("SELECT * FROM phone_numbers WHERE number = '393331234567'").fetchone()
        self.assertEqual(row["project_label"], "Competitors")
        self.assertEqual(row["assignment_type"], "pool_number")
        self.assertEqual(str(row["connection_cost"]), "12.5")
        self.assertEqual(str(row["monthly_fee"]), "3.25")
        self.assertEqual(row["phone_type"], "Mobile")
        self.assertEqual(row["tariff_label"], "Tariff A")
        self.assertEqual(row["created_at"], "2026-06-01 10:00:00")
        self.assertIsNotNone(row["deactivated_at"])

        bad_preview = preview_import(self.conn, "phone_numbers", "number;country;project;assignment_type\n393331234568;Италия;NoSuchProject;Номер из пула\n")
        self.assertEqual(bad_preview.error_rows, 1)


    def test_phone_import_requires_country_project_and_number(self):
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Competitors', 1)")
        self.conn.commit()
        cases = [
            "project,number\nCompetitors,393331234569\n",
            "country,number\nИталия,393331234569\n",
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
        self.assertEqual(row["assignment_type"], "other")
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
        self.assertEqual(row["review_required"], 0)


    def test_phone_import_maps_old_statuses_to_new_statuses(self):
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Competitors', 1)")
        self.conn.commit()
        csv_text = "country,project,number,assignment_type,status\nИталия,Competitors,393331234573,pool_number,reserved\nИталия,Competitors,393331234574,pool_number,blocked\nИталия,Competitors,393331234575,pool_number,disabled\n"
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
        self.conn.execute("INSERT INTO projects(name, is_active) VALUES ('Alpha', 1)")
        self.conn.commit()
        self.repo = Repository(self.conn)
        self.admin_id = self.repo.create_user("admin2", "Admin")

    def tearDown(self):
        self.conn.close()

    def test_replace_phone_numbers_clears_only_phone_section(self):
        apply_import(self.conn, "phone_numbers", "country,project,number,assignment_type,status\nA,Alpha,1111111,pool_number,used\n", user_id=self.admin_id)
        apply_import(self.conn, "phone_numbers", "country,project,number,assignment_type,status\nB,Alpha,2222222,pool_number,used\n", user_id=self.admin_id, mode="replace_section")
        rows = self.conn.execute("SELECT number FROM phone_numbers ORDER BY number").fetchall()
        self.assertEqual([row["number"] for row in rows], ["2222222"])
