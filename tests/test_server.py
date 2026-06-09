import os
import tempfile
import unittest
from urllib.parse import urlencode

import app.server as server


class ServerSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.old_path = server.DB_PATH
        server.DB_PATH = self.tmp.name

    def tearDown(self):
        server.DB_PATH = self.old_path
        os.unlink(self.tmp.name)

    def request(self, path, method="GET", body=""):
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        import io

        path_info, _, query = path.partition("?")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path_info,
            "QUERY_STRING": query,
            "CONTENT_LENGTH": str(len(body.encode("utf-8"))),
            "wsgi.input": io.BytesIO(body.encode("utf-8")),
        }
        content = b"".join(server.app(environ, start_response)).decode("utf-8")
        return captured, content

    def test_main_screens_render(self):
        for path, marker in [
            ("/routes", "Маршруты"),
            ("/tariffs", "Тарифы"),
            ("/phones", "Купленные номера"),
            ("/companies", "Кампании прозвона"),
            ("/provider-changes", "Смена провайдеров"),
            ("/admin", "Администрирование"),
            ("/admin/import", "Импорт / экспорт"),
            ("/admin/currency-rates", "Курсы валют"),
            ("/admin/telegram", "Telegram"),
            ("/admin/naming-rules", "Правила нейминга"),
            ("/admin/change-log", "Change log"),
        ]:
            with self.subTest(path=path):
                captured, content = self.request(path)
                self.assertEqual(captured["status"], "200 OK")
                self.assertIn(marker, content)

    def test_provider_change_requires_server_when_provider_changes(self):
        self.request("/routes")  # seed database
        body = urlencode(
            {
                "changed_at": "2026-06-07 15:30",
                "country_id": "1",
                "provider_before_id": "1",
                "provider_after_id": "2",
                "route_after_id": "2",
                "reason_text": "Дешевле",
                "comment": "test",
            }
        )
        captured, content = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Сервер обязателен", content)


    def test_routes_filter_applies_country(self):
        self.request("/routes")
        captured, content = self.request("/routes?country_id=999")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("Мексика/Miatel/Pool_A@", content)

    def test_duplicate_phone_returns_user_message(self):
        self.request("/routes")
        body = urlencode({"number": "525512345001", "country_id": "1", "provider_id": "2", "assignment_type": "pool_number", "status": "used"})
        captured, content = self.request("/phones/create", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Номер уже существует", content)

    def test_route_number_add_uses_phone_number_not_internal_id(self):
        self.request("/routes")
        body = urlencode({"phone_number": "525512345001", "usage_type": "pool_member"})
        captured, content = self.request("/routes/2/numbers/add", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Номер уже добавлен", content)

    def test_duplicate_route_returns_user_message(self):
        self.request("/routes")
        body = urlencode({"country_id": "1", "provider_id": "2", "provider_prefix_id": "2", "project_label": "", "cli_source_type": "pool", "cli_source_label": "Pool_A", "is_actual": "1"})
        captured, content = self.request("/routes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Маршрут уже существует", content)

    def test_admin_dictionaries_can_add_reference_values_and_simple_prefix_labels(self):
        self.request("/tariffs")
        captured, content = self.request("/tariffs")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("Добавить справочные значения", content)
        captured, _ = self.request("/admin/dictionaries/countries/create", method="POST", body=urlencode({"name": "Аргентина", "code": "ARG"}))
        self.assertEqual(captured["status"], "303 See Other")
        captured, _ = self.request("/admin/dictionaries/providers/create", method="POST", body=urlencode({"name": "NewTel", "default_currency_id": ""}))
        self.assertEqual(captured["status"], "303 See Other")
        captured, _ = self.request("/admin/dictionaries/currencies/create", method="POST", body=urlencode({"code": "USD", "name": "US Dollar"}))
        self.assertEqual(captured["status"], "303 See Other")
        captured, _ = self.request("/admin/dictionaries/prefixes/create", method="POST", body=urlencode({"provider_id": "1", "prefix": "0333", "name": ""}))
        self.assertEqual(captured["status"], "303 See Other")
        captured, content = self.request("/admin/dictionaries")
        self.assertIn("Аргентина", content)
        self.assertIn("NewTel", content)
        self.assertIn("USD", content)
        captured, content = self.request("/tariffs")
        self.assertIn(">0333<", content)
        self.assertNotIn("Sancom / 0333", content)

    def test_edit_pages_render_single_record_forms(self):
        self.request("/routes")
        for path, marker in [
            ("/routes/1/edit", "Редактировать маршрут"),
            ("/phones/1/edit", "Редактировать номер"),
            ("/companies/1/edit", "ID кампании"),
        ]:
            with self.subTest(path=path):
                captured, content = self.request(path)
                self.assertEqual(captured["status"], "200 OK")
                self.assertIn(marker, content)

    def test_currency_rate_upsert_updates_latest_row(self):
        self.request("/tariffs")
        body = urlencode({"currency_id": "2", "rate_to_eur": "0.91"})
        self.request("/admin/currency-rates/upsert", method="POST", body=body)
        self.request("/admin/currency-rates/upsert", method="POST", body=urlencode({"currency_id": "2", "rate_to_eur": "0.92"}))
        captured, content = self.request("/admin/currency-rates")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("0.92", content)
        self.assertNotIn("0.91", content)

    def test_change_reason_can_be_deactivated_and_hidden_from_provider_change_form(self):
        self.request("/routes")
        body = urlencode({"name": "Временно не использовать", "is_active": "1", "comment": "test"})
        self.request("/admin/change-reasons/create", method="POST", body=body)
        self.request("/admin/change-reasons/4/update", method="POST", body=urlencode({"name": "Временно не использовать", "is_active": "0", "comment": "test"}))
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("Временно не использовать", content)

    def test_deactivated_dictionary_values_hidden_from_new_record_forms(self):
        self.request("/routes")
        self.request("/admin/dictionaries/countries/1/update", method="POST", body=urlencode({"name": "Мексика", "code": "MEX", "is_active": "0"}))
        captured, content = self.request("/tariffs")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn('<select name="country_id"></select>', content)
        captured, content = self.request("/routes")
        self.assertIn("Мексика/Miatel/Pool_A@", content)

    def test_phone_type_dictionary_drives_phone_forms(self):
        self.request("/routes")
        captured, content = self.request("/phones/1/edit")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Mobile", content)
        self.assertIn("Fixed Line", content)
        self.assertNotIn("name='phone_type' value", content)

    def test_provider_change_uses_server_checkboxes(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("type='checkbox' name='server_ids'", content)
        self.assertNotIn('name="server_ids" multiple', content)

    def test_route_edit_allows_name_and_prefix_fields(self):
        self.request("/routes")
        captured, content = self.request("/routes/1/edit")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("name='name'", content)
        self.assertIn("name='provider_prefix_id'", content)

    def test_import_preview_preserves_form_values_and_tariff_replace_is_disabled(self):
        self.request("/routes")
        csv_text = "country,provider\nМексика,Miatel\n"
        body = urlencode({"entity_type": "tariffs", "mode": "append_update", "csv_data": csv_text})
        captured, content = self.request("/admin/import/preview", method="POST", body=body)
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn('<option value="tariffs" selected>Тарифы</option>', content)
        self.assertIn(csv_text, content)
        self.assertIn("replace.disabled = true", content)

    def test_import_apply_shows_summary(self):
        self.request("/routes")
        body = urlencode({"entity_type": "dictionaries", "mode": "append_update", "csv_data": "type,name\ncountry,Перу\n"})
        captured, content = self.request("/admin/import/apply", method="POST", body=body)
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Импорт завершён", content)
        self.assertIn("создано 1", content)

    def test_projects_and_assignments_are_admin_dictionaries_and_phone_dropdowns(self):
        self.request("/routes")
        captured, content = self.request("/admin/dictionaries")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Проект", content)
        self.assertIn("Назначение номера", content)
        self.request("/admin/dictionaries/projects/create", method="POST", body=urlencode({"name": "NewProject", "comment": ""}))
        self.request("/admin/dictionaries/phone-assignments/create", method="POST", body=urlencode({"name": "Мониторинг", "code": "monitoring", "comment": ""}))
        captured, content = self.request("/phones")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("NewProject", content)
        self.assertIn("Мониторинг", content)
        self.assertIn("Дата создания", content)
        self.assertIn("Дата отключения", content)

    def test_dictionaries_layout_selects_one_workspace_section(self):
        self.request("/routes")
        captured, content = self.request("/admin/dictionaries?section=providers")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("dictionary-layout", content)
        self.assertIn("dictionary-card active", content)
        self.assertIn("Справочник: Провайдер", content)
        self.assertIn("Всего записей:", content)
        self.assertIn("<th>Название</th><th>Активен</th><th>Комментарий</th><th>Действия</th>", content)
        self.assertNotIn("Справочник: GEO", content)



    def test_server_priorities_show_route_provider_details_and_edit_form(self):
        self.request("/routes")
        captured, content = self.request("/admin/server-priorities")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Текущий приоритет", content)
        self.assertIn("<span class='star'>★</span> Miatel", content)
        self.assertIn("Предыдущий приоритет", content)
        self.assertIn("<span class='star'>☆</span> Sancom", content)
        self.assertIn("Текущий провайдер: Miatel", content)
        self.assertIn("Текущий маршрут: Мексика/Miatel/Pool_A@", content)
        self.assertIn("Предыдущий провайдер: Sancom", content)
        self.assertIn("Предыдущий маршрут: Мексика/Sancom/RND/0827pfx@", content)
        self.assertIn("name='current_route_id'", content)
        self.assertIn("Сохранить текущий маршрут", content)

    def test_server_priority_manual_route_update_changes_current_and_previous(self):
        self.request("/routes")
        body = urlencode({"current_route_id": "1", "comment": "manual admin update"})
        captured, _ = self.request("/admin/server-priorities/1/update", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT current_route_id, previous_route_id, comment FROM server_route_priorities WHERE id = 1").fetchone()
            self.assertEqual(row["current_route_id"], 1)
            self.assertEqual(row["previous_route_id"], 2)
            self.assertEqual(row["comment"], "manual admin update")
            event = conn.execute("SELECT * FROM change_log WHERE entity_type = 'server_route_priority' AND entity_id = 1").fetchone()
            self.assertIsNotNone(event)
        finally:
            conn.close()
        captured, content = self.request("/admin/server-priorities")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("<span class='star'>★</span> Sancom", content)
        self.assertIn("<span class='star'>☆</span> Miatel", content)



    def test_company_routing_settings_admin_link_and_screen_render(self):
        self.request("/routes")
        captured, content = self.request("/admin")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Схема маршрутизации кампаний", content)
        self.assertIn('/admin/company-routing-settings', content)
        captured, content = self.request("/admin/company-routing-settings")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Администрирование → Схема маршрутизации кампаний", content)
        self.assertIn("+ Добавить схему маршрутизации кампании", content)
        self.assertIn('name="calling_company_id"', content)
        self.assertIn('name="routing_mode"', content)
        self.assertIn('name="show_history"', content)

    def test_company_routing_setting_create_visible_and_filters_render(self):
        self.request("/routes")
        body = urlencode({
            "calling_company_id": "1",
            "country_id": "1",
            "server_id": "1",
            "routing_mode": "server_priority",
            "route_id": "",
            "has_autorotation": "",
            "is_active": "1",
            "comment": "manual routing note",
        })
        captured, _ = self.request("/admin/company-routing-settings/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        captured, content = self.request("/admin/company-routing-settings?country_id=1&server_id=1&routing_mode=server_priority&is_active=1")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("CC Mexico Demo", content)
        self.assertIn("1001", content)
        self.assertIn("server_priority", content)
        self.assertIn("manual routing note", content)

    def test_company_routing_history_hidden_by_default_and_visible_when_enabled(self):
        self.request("/routes")
        create_body = urlencode({
            "calling_company_id": "1",
            "country_id": "1",
            "server_id": "1",
            "routing_mode": "server_priority",
            "route_id": "",
            "is_active": "1",
            "comment": "old routing state",
        })
        self.request("/admin/company-routing-settings/create", method="POST", body=create_body)
        update_body = urlencode({
            "country_id": "1",
            "server_id": "1",
            "routing_mode": "autorotation",
            "route_id": "",
            "has_autorotation": "1",
            "is_active": "1",
            "comment": "new routing state",
        })
        captured, _ = self.request("/admin/company-routing-settings/1/update", method="POST", body=update_body)
        self.assertEqual(captured["status"], "303 See Other")
        captured, content = self.request("/admin/company-routing-settings")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("new routing state", content)
        self.assertNotIn("old routing state", content)
        captured, content = self.request("/admin/company-routing-settings?show_history=1")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("new routing state", content)
        self.assertIn("old routing state", content)



if __name__ == "__main__":
    unittest.main()
