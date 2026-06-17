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

    def request(self, path, method="GET", body="", cookie=""):
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
        if cookie:
            environ["HTTP_COOKIE"] = cookie
        content = b"".join(server.app(environ, start_response)).decode("utf-8")
        return captured, content

    def user_cookie(self, username):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
        finally:
            conn.close()
        return f"mvp_current_user_id={user_id}"


    def test_phone_status_options_expose_only_simplified_statuses(self):
        html = server.phone_status_options(empty="Все")
        for label in ("Используется", "Свободен", "Проблемный", "Неизвестно"):
            self.assertIn(label, html)
        for old_value in ("reserved", "blocked", "disabled"):
            self.assertNotIn(f"value='{old_value}'", html)

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


    def test_users_admin_page_returns_200_and_defaults_exist(self):
        captured, content = self.request("/admin/users")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Пользователи", content)
        self.assertIn("Roman", content)
        self.assertIn("Дежурный", content)
        self.assertIn("Гость", content)
        self.assertIn("без паролей", content)

    def test_current_user_selector_appears_in_layout(self):
        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn('class="current-user-selector"', content)
        self.assertIn('action="/users/select"', content)
        self.assertIn("Текущий пользователь", content)
        self.assertIn("Roman", content)

    def test_selecting_user_persists_cookie_and_redirects(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            duty_id = conn.execute("SELECT id FROM users WHERE username = 'duty'").fetchone()["id"]
        finally:
            conn.close()
        body = urlencode({"user_id": str(duty_id), "redirect_to": "/phones"})
        captured, content = self.request("/users/select", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/phones"), captured["headers"])
        set_cookie = dict(captured["headers"]).get("Set-Cookie", "")
        self.assertIn(f"mvp_current_user_id={duty_id}", set_cookie)

        captured, content = self.request("/routes", cookie=f"mvp_current_user_id={duty_id}")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(f"<option value='{duty_id}' selected>Дежурный", content)

    def test_inactive_users_are_not_selectable(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            guest_id = conn.execute("SELECT id FROM users WHERE username = 'guest'").fetchone()["id"]
            conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (guest_id,))
            conn.commit()
        finally:
            conn.close()
        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        selector = content.split('<form class="current-user-selector"', 1)[1].split("</form>", 1)[0]
        self.assertNotIn(f"<option value='{guest_id}'", selector)


    def test_breadcrumbs_appear_on_representative_pages(self):
        for path, crumbs in [
            ("/routes", ["Главная", "Маршруты"]),
            ("/phones", ["Главная", "Купленные номера"]),
            ("/admin/server-priorities", ["Главная", "Администрирование", "Приоритет по серверам"]),
        ]:
            with self.subTest(path=path):
                captured, content = self.request(path)
                self.assertEqual(captured["status"], "200 OK")
                self.assertIn("class='breadcrumbs'", content)
                for crumb in crumbs:
                    self.assertIn(crumb, content)

    def test_reset_filters_links_point_to_base_pages(self):
        for path, base in [
            ("/routes?country_id=1", "/routes"),
            ("/tariffs?country_id=1", "/tariffs"),
            ("/phones?number=525", "/phones"),
            ("/companies?company=Demo", "/companies"),
            ("/provider-changes?country_id=1", "/provider-changes"),
            ("/admin/server-priorities?server_id=1", "/admin/server-priorities"),
        ]:
            with self.subTest(path=path):
                captured, content = self.request(path)
                self.assertEqual(captured["status"], "200 OK")
                self.assertIn("Сбросить фильтры", content)
                self.assertIn(f"href='{base}'", content)

    def test_provider_change_page_has_three_apply_scopes(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Не меняли настройки в нашей системе", content)
        self.assertIn("Серверный приоритет", content)
        self.assertIn("Настройка кампании", content)

    def test_routes_filters_are_collapsible_and_keep_field_names(self):
        self.request("/routes")
        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("class='filter-card'", content)
        self.assertIn("<summary class='filter-summary'>Фильтры</summary>", content)
        self.assertIn('name="country_id"', content)
        self.assertIn('name="provider_id"', content)
        self.assertIn('name="prefix_id"', content)
        self.assertIn('name="is_actual"', content)
        self.assertIn('name="search"', content)

        captured, content = self.request("/routes?country_id=1&search=Demo")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("<details class='filter-card' open>", content)
        self.assertIn('name="search" value="Demo"', content)

    def test_routes_table_renders_route_name_quick_copy(self):
        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Название маршрута", content)
        self.assertIn("data-copy-action='route-name'", content)
        self.assertIn("title='Скопировать колонку'", content)
        self.assertIn("data-copy-column='route-name'", content)

    def test_purchased_numbers_table_renders_number_quick_copy(self):
        captured, content = self.request("/phones")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Купленные номера", content)
        self.assertIn("data-copy-action='phone-number'", content)
        self.assertIn("title='Скопировать колонку'", content)
        self.assertIn("data-copy-column='phone-number'", content)


    def test_table_pages_render_column_visibility_controls(self):
        for path, table_key, sample_col in (
            ("/routes", "routes", "route"),
            ("/phones", "phones", "number"),
            ("/tariffs", "tariffs", "provider_price"),
            ("/companies", "companies", "company_name"),
            ("/provider-changes", "provider_changes", "event_at"),
            ("/admin/server-priorities", "server_priorities", "current_priority"),
            ("/admin/company-routing-settings", "company_routing_settings", "company_id"),
        ):
            captured, content = self.request(path)
            self.assertEqual(captured["status"], "200 OK")
            self.assertIn("<summary>Колонки</summary>", content)
            self.assertIn(f"data-column-settings='{table_key}'", content)
            self.assertIn(f"data-table-key='{table_key}'", content)
            self.assertIn(f"data-col='{sample_col}'", content)
            self.assertIn("Сбросить колонки", content)


    def test_configurable_table_headers_have_full_title_tooltips(self):
        for path, expected_headers in {
            "/tariffs": ["Цена провайдера"],
            "/companies": ["Количество наборов", "Количество линий"],
            "/phones": ["Активен у провайдера"],
            "/provider-changes": ["Область применения", "Комментарий", "Детали"],
            "/routes": ["Название маршрута"],
        }.items():
            with self.subTest(path=path):
                captured, content = self.request(path)
                self.assertEqual(captured["status"], "200 OK")
                for header in expected_headers:
                    self.assertIn(f"title='{header}'", content)

    def test_change_log_has_no_column_visibility_control(self):
        captured, content = self.request("/admin/change-log")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("<summary>Колонки</summary>", content)
        self.assertNotIn("data-column-settings=", content)

    def test_provider_changes_journal_workspace_and_create_form_survive(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Журнал событий", content)
        self.assertIn("journal-card", content)
        self.assertIn("table-scroll", content)
        self.assertIn("name='apply_scope' value='none'", content)
        self.assertIn("name='apply_scope' value='server_priority'", content)
        self.assertIn("name='apply_scope' value='campaign_setting'", content)
        self.assertIn("name='server_ids' value='1'", content)

    def test_sidebar_admin_tree_regression_for_phase_two_layout(self):
        captured, content = self.request("/admin/server-priorities")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("side-nav", content)
        self.assertIn("Администрирование</button>", content)
        self.assertIn("admin-tree open", content)
        self.assertIn("href='/routes'><span class='nav-icon'", content)
        self.assertIn("<span class='side-label'>Маршруты</span></a>", content)
        self.assertIn("href='/provider-changes'><span class='nav-icon'", content)
        self.assertIn("<span class='side-label'>Смена провайдеров</span></a>", content)
        self.assertIn("href='/admin/company-routing-settings'>Схема маршрутизации кампаний</a>", content)
        self.assertIn("admin-link active", content)

    def test_provider_changes_sidebar_link_renders_attributes_and_icon_safely(self):
        for path in ("/dashboard", "/provider-changes"):
            with self.subTest(path=path):
                captured, content = self.request(path)
                self.assertEqual(captured["status"], "200 OK")
                sidebar_item = content.split("href='/provider-changes'", 1)[0].rsplit("<a class='side-link", 1)[1]
                sidebar_item += content.split("href='/provider-changes'", 1)[1].split("</a>", 1)[0] + "</a>"
                self.assertIn("data-tooltip='Журнал изменений'", sidebar_item)
                self.assertIn("<span class='nav-icon' aria-hidden='true'><svg", sidebar_item)
                self.assertIn("<span class='side-label'>Смена провайдеров</span>", sidebar_item)
                self.assertNotIn("data-icon='<svg", sidebar_item)
                self.assertNotIn("viewBox='0 0 24 24' focusable='false' aria-hidden='true'><path", sidebar_item.split(">", 1)[0])
        _, active_content = self.request("/provider-changes")
        active_item = active_content.split("href='/provider-changes'", 1)[0].rsplit("<a class='side-link", 1)[1]
        self.assertIn("active", active_item)

    def test_provider_change_form_is_dynamic_and_defaults_to_none_scope(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("value='none' checked", content)
        self.assertIn("data-scopes='none'", content)
        self.assertIn("Маршрут/префикс", content)
        self.assertIn("data-scopes='server_priority'", content)
        self.assertIn("Текущий маршрут", content)
        self.assertIn("Новый провайдер кампании", content)
        self.assertIn("data-campaign-route-field='1'", content)
        self.assertIn("Включили авторотацию", content)
        self.assertIn("Выключили авторотацию", content)
        self.assertIn("Прописали ручной маршрут", content)
        self.assertIn("Убрали ручной маршрут", content)
        self.assertNotIn("Изменили ручной маршрут", content)
        self.assertNotIn("Вернули на server_priority", content)
        self.assertNotIn("Новый route", content)
        self.assertNotIn("Новая авторотация", content)

    def test_provider_change_new_route_select_is_wide_and_has_titles(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        create_form = content.split("<form method='post' action='/provider-changes/create'", 1)[1].split("</form>", 1)[0]
        self.assertIn("class='scope-field route-select-field' data-scopes='server_priority'>Новый маршрут", create_form)
        self.assertIn("<select name='new_route_id' id='new-route' class='route-select'>", create_form)
        self.assertIn("title='Мексика / Miatel / Мексика/Miatel/Demo_A@'", create_form)
        self.assertIn(".form-grid .route-select-field { min-width: min(420px, 100%); width: clamp(420px, 44vw, 560px); grid-column: span 2; }", content)
        self.assertIn(".form-grid .route-select-field .route-select { width: 100%; min-width: 0; font-size: 14px; }", content)
        self.assertIn(".form-grid .route-select-field option { font-size: 13px; }", content)
        self.assertIn("@media (max-width: 720px)", content)

    def test_provider_change_server_priority_uses_active_server_checkboxes(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        create_form = content.split("<form method='post' action='/provider-changes/create'", 1)[1].split("</form>", 1)[0]
        self.assertIn("<legend>Серверы <span class='required'>*</span></legend>", create_form)
        self.assertIn("name='server_ids' value='1'", create_form)
        self.assertIn("name='server_ids' value='2'", create_form)
        self.assertIn("EU1", create_form)
        self.assertIn("EU2", create_form)
        self.assertNotIn("<select name='server_id' id='event-server'", create_form)

    def test_provider_change_server_priority_checkbox_controls_are_non_submit_buttons(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        create_form = content.split("<form method='post' action='/provider-changes/create'", 1)[1].split("</form>", 1)[0]
        self.assertIn("data-server-select='all'>Выбрать все", create_form)
        self.assertIn("data-server-select='none'>Снять все", create_form)
        self.assertIn("<button type='button' data-server-select='all'>Выбрать все</button>", create_form)
        self.assertIn("<button type='button' data-server-select='none'>Снять все</button>", create_form)

    def test_provider_change_server_priority_shows_current_route_hints(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        create_form = content.split("<form method='post' action='/provider-changes/create'", 1)[1].split("</form>", 1)[0]
        eu1_item = create_form.split("<span class='server-checkbox-main'>EU1</span>", 1)[1].split("</label>", 1)[0]
        eu3_item = create_form.split("<span class='server-checkbox-main'>EU3</span>", 1)[1].split("</label>", 1)[0]
        self.assertIn("текущий: Мексика / Miatel / Мексика/Miatel/Demo_A@", eu1_item)
        self.assertIn("текущий: —", eu3_item)

    def test_provider_changes_navigation_is_top_level_only(self):
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("<h1>Смена провайдеров</h1>", content)
        self.assertNotIn("Администрирование → Смена провайдеров", content)
        captured, content = self.request("/admin")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("href='/provider-changes'><span class='nav-icon'", content)
        self.assertIn("<span class='side-label'>Смена провайдеров</span></a>", content)
        self.assertNotIn('<a class="card" href="/provider-changes">Смена провайдеров</a>', content)
        self.assertEqual(content.count("<span class='side-label'>Смена провайдеров</span>"), 1)

    def test_default_seed_contains_mvp_demo_dataset(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            servers = [row["name"] for row in conn.execute("SELECT name FROM servers WHERE is_active = 1 ORDER BY name")]
            self.assertEqual(servers, [f"EU{i}" for i in range(1, 10)])
            legacy_servers = ["ASIA1", "LATAM1", "LATAM2", "NL1", "US1", "US2", "DE1"]
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM servers WHERE name IN ({','.join('?' for _ in legacy_servers)})", legacy_servers).fetchone()[0], 0)
            self.assertIsNotNone(conn.execute("SELECT id FROM countries WHERE name = 'Мексика'").fetchone())
            providers = [row["name"] for row in conn.execute("SELECT name FROM providers ORDER BY name")]
            self.assertEqual(providers, ["DemoTel", "Miatel", "Sancom"])
            routes = [row["name"] for row in conn.execute("SELECT r.name FROM routes r JOIN countries c ON c.id = r.country_id WHERE c.name = 'Мексика' ORDER BY r.name")]
            self.assertEqual(routes, [
                "Мексика/DemoTel/Demo_A@",
                "Мексика/DemoTel/Demo_B@",
                "Мексика/Miatel/Demo_A@",
                "Мексика/Miatel/Demo_B@",
                "Мексика/Sancom/Demo_0827@",
                "Мексика/Sancom/Demo_0828@",
            ])
            companies = [(row["company_id_external"], row["company_name"]) for row in conn.execute("SELECT company_id_external, company_name FROM calling_companies ORDER BY company_id_external")]
            self.assertEqual(companies, [(str(1000 + i), f"CC Mexico Demo {i}") for i in range(1, 6)])
            numbers = [row["number"] for row in conn.execute("SELECT number FROM phone_numbers ORDER BY number")]
            self.assertEqual(numbers, [f"5255500000{i:02d}" for i in range(1, 11)])
        finally:
            conn.close()

    def test_default_seed_server_priorities_only_for_eu1_and_eu2(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            rows = conn.execute("""
                SELECT s.name AS server_name, c.name AS country_name, cr.name AS current_route_name,
                       pr.name AS previous_route_name, srp.comment
                FROM server_route_priorities srp
                JOIN servers s ON s.id = srp.server_id
                JOIN countries c ON c.id = srp.country_id
                JOIN routes cr ON cr.id = srp.current_route_id
                LEFT JOIN routes pr ON pr.id = srp.previous_route_id
                ORDER BY s.name
            """).fetchall()
            self.assertEqual([(row["server_name"], row["country_name"], row["current_route_name"], row["previous_route_name"], row["comment"]) for row in rows], [
                ("EU1", "Мексика", "Мексика/Miatel/Demo_A@", None, "Demo initial priority"),
                ("EU2", "Мексика", "Мексика/Sancom/Demo_0827@", None, "Demo initial priority"),
            ])
            empty_priorities = conn.execute("""
                SELECT COUNT(*)
                FROM server_route_priorities srp
                JOIN servers s ON s.id = srp.server_id
                WHERE s.name IN ('EU3', 'EU4', 'EU5', 'EU6', 'EU7', 'EU8', 'EU9')
            """).fetchone()[0]
            self.assertEqual(empty_priorities, 0)
        finally:
            conn.close()



    def test_demo_normalization_updates_existing_partial_demo_db_and_is_idempotent(self):
        conn = server.connect(server.DB_PATH)
        try:
            server.init_db(conn)
            repo = server.Repository(conn)
            admin_id = repo.create_user("admin", "Admin")
            for server_name in ("ASIA1", "DE1", "LATAM1", "US1", "OLDDEMO1"):
                conn.execute("INSERT INTO servers(name, is_active) VALUES (?, 1)", (server_name,))
            country_id = repo.create_country("Мексика", "MEX")
            eur_id = repo.create_currency("EUR", "Euro", "€")
            miatel_id = repo.create_provider("Miatel", "voip", eur_id)
            sancom_id = repo.create_provider("Sancom", "voip", eur_id)
            miatel_prefix = repo.create_prefix(miatel_id, None, "Без префикса")
            sancom_prefix = repo.create_prefix(sancom_id, "0827")
            old_route_id = repo.create_route(
                country_id=country_id,
                provider_id=miatel_id,
                provider_prefix_id=miatel_prefix,
                name="Мексика/Miatel/Pool_A@",
                cli_source_type="pool",
                cli_source_label="Pool_A",
                created_by=admin_id,
            )
            repo.create_route(
                country_id=country_id,
                provider_id=sancom_id,
                provider_prefix_id=sancom_prefix,
                name="Мексика/Sancom/RND/0827pfx@",
                cli_source_type="rnd",
                cli_source_label="RND",
                created_by=admin_id,
            )
            old_phone_id = repo.create_phone_number(
                country_id=country_id,
                provider_id=miatel_id,
                number="525512345001",
                assignment_type="pool_number",
                status="used",
                created_by=admin_id,
                currency_id=eur_id,
                comment="Демо-номер",
            )
            repo.add_phone_to_route(route_id=old_route_id, phone_number_id=old_phone_id, usage_type="pool_member", added_by=admin_id)
            asia_id = conn.execute("SELECT id FROM servers WHERE name = 'ASIA1'").fetchone()["id"]
            repo.create_calling_company(
                server_id=asia_id,
                country_id=country_id,
                company_name="CC Mexico Demo",
                company_id_external="1001",
                has_autorotation=True,
                created_by=admin_id,
                is_active=True,
            )
            conn.execute(
                """
                INSERT INTO server_route_priorities(country_id, server_id, current_route_id, changed_by, created_by, comment)
                VALUES (?, ?, ?, ?, ?, 'Old demo priority')
                """,
                (country_id, asia_id, old_route_id, admin_id, admin_id),
            )
            conn.commit()
        finally:
            conn.close()

        self.request("/routes")

        def normalized_counts():
            check_conn = server.connect(server.DB_PATH)
            try:
                active_servers = [row["name"] for row in check_conn.execute("SELECT name FROM servers WHERE is_active = 1 ORDER BY name")]
                inactive_old_servers = [row["name"] for row in check_conn.execute("SELECT name FROM servers WHERE name IN ('ASIA1', 'DE1', 'LATAM1', 'US1', 'OLDDEMO1') AND is_active = 0 ORDER BY name")]
                active_routes = [row["name"] for row in check_conn.execute("""
                    SELECT r.name
                    FROM routes r
                    JOIN countries c ON c.id = r.country_id
                    WHERE c.name = 'Мексика' AND r.is_actual = 1
                    ORDER BY r.name
                """)]
                active_companies = [row["company_id_external"] for row in check_conn.execute("""
                    SELECT cc.company_id_external
                    FROM calling_companies cc
                    JOIN countries c ON c.id = cc.country_id
                    WHERE c.name = 'Мексика' AND cc.is_active = 1
                    ORDER BY cc.company_id_external
                """)]
                active_numbers = [row["number"] for row in check_conn.execute("""
                    SELECT pn.number
                    FROM phone_numbers pn
                    JOIN countries c ON c.id = pn.country_id
                    WHERE c.name = 'Мексика' AND pn.is_active = 1
                    ORDER BY pn.number
                """)]
                priorities = [(row["server_name"], row["route_name"], row["previous_route_id"], row["comment"]) for row in check_conn.execute("""
                    SELECT s.name AS server_name, r.name AS route_name, srp.previous_route_id, srp.comment
                    FROM server_route_priorities srp
                    JOIN servers s ON s.id = srp.server_id
                    JOIN routes r ON r.id = srp.current_route_id
                    JOIN countries c ON c.id = srp.country_id
                    WHERE c.name = 'Мексика'
                    ORDER BY s.name
                """)]
                return {
                    "active_servers": active_servers,
                    "inactive_old_servers": inactive_old_servers,
                    "active_routes": active_routes,
                    "active_companies": active_companies,
                    "active_numbers": active_numbers,
                    "priorities": priorities,
                }
            finally:
                check_conn.close()

        first_counts = normalized_counts()
        self.assertEqual(first_counts["active_servers"], [f"EU{i}" for i in range(1, 10)])
        self.assertEqual(first_counts["inactive_old_servers"], ["ASIA1", "DE1", "LATAM1", "OLDDEMO1", "US1"])
        self.assertEqual(first_counts["active_routes"], [
            "Мексика/DemoTel/Demo_A@",
            "Мексика/DemoTel/Demo_B@",
            "Мексика/Miatel/Demo_A@",
            "Мексика/Miatel/Demo_B@",
            "Мексика/Sancom/Demo_0827@",
            "Мексика/Sancom/Demo_0828@",
        ])
        self.assertEqual(first_counts["active_companies"], [str(1000 + i) for i in range(1, 6)])
        self.assertEqual(first_counts["active_numbers"], [f"5255500000{i:02d}" for i in range(1, 11)])
        self.assertEqual(first_counts["priorities"], [
            ("EU1", "Мексика/Miatel/Demo_A@", None, "Demo initial priority"),
            ("EU2", "Мексика/Sancom/Demo_0827@", None, "Demo initial priority"),
        ])

        self.request("/routes")
        self.assertEqual(normalized_counts(), first_counts)


    def test_routes_filter_applies_country(self):
        self.request("/routes")
        captured, content = self.request("/routes?country_id=999")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("Мексика/Miatel/Demo_A@", content)


    def test_manual_phone_creation_without_provider_is_rejected(self):
        self.request("/routes")
        body = urlencode({"number": "525550009901", "country_id": "1", "provider_id": "", "assignment_type": "pool_number", "status": "used"})
        captured, content = self.request("/phones/create", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Провайдер обязателен", content)

    def test_review_required_badge_and_edit_rules(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            conn.execute("""
                INSERT INTO phone_numbers(country_id, provider_id, number, normalized_number, project_label, assignment_type, status, comment, is_active, review_required, created_by)
                VALUES (1, NULL, '525550009902', '525550009902', 'Demo', 'other', 'unknown', 'Needs review', 1, 1, 1)
            """)
            conn.commit()
            phone_id = conn.execute("SELECT id FROM phone_numbers WHERE number = '525550009902'").fetchone()["id"]
        finally:
            conn.close()
        captured, content = self.request("/phones")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Требует проверки", content)
        body = urlencode({"number": "525550009902", "country_id": "1", "provider_id": "", "assignment_type": "other", "status": "unknown", "is_active": "1"})
        captured, content = self.request(f"/phones/{phone_id}/update", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Нельзя снять флаг проверки, пока не выбран провайдер", content)
        body = urlencode({"number": "525550009902", "country_id": "1", "provider_id": "", "assignment_type": "other", "status": "unknown", "is_active": "1", "review_required": "1", "comment": "Edited"})
        captured, _ = self.request(f"/phones/{phone_id}/update", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT review_required, provider_id, comment FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
            self.assertEqual(row["review_required"], 1)
            self.assertIsNone(row["provider_id"])
            self.assertEqual(row["comment"], "Edited")
        finally:
            conn.close()
        body = urlencode({"number": "525550009902", "country_id": "1", "provider_id": "1", "assignment_type": "other", "status": "unknown", "is_active": "1"})
        captured, _ = self.request(f"/phones/{phone_id}/update", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT review_required, provider_id FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
            self.assertEqual(row["review_required"], 0)
            self.assertEqual(row["provider_id"], 1)
        finally:
            conn.close()

    def test_phone_csv_export_includes_review_required(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            conn.execute("UPDATE phone_numbers SET review_required = 1 WHERE number = '525550000001'")
            conn.commit()
        finally:
            conn.close()
        captured, content = self.request("/phones?export=csv")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Требует проверки", content)
        self.assertIn("Да", content)

    def test_duplicate_phone_returns_user_message(self):
        self.request("/routes")
        body = urlencode({"number": "525550000001", "country_id": "1", "provider_id": "2", "assignment_type": "pool_number", "status": "used"})
        captured, content = self.request("/phones/create", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Номер уже существует", content)
        self.assertIn("Купленные номера", content)
        self.assertNotIn("<h1>Маршруты</h1>", content)

    def test_route_number_add_uses_phone_number_not_internal_id(self):
        self.request("/routes")
        body = urlencode({"phone_number": "525550000001", "usage_type": "pool_member"})
        captured, content = self.request("/routes/2/numbers/add", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Номер уже добавлен", content)

    def test_route_number_add_rejects_non_used_phone_status(self):
        self.request("/routes")
        body = urlencode({"number": "525550099998", "country_id": "1", "provider_id": "1", "assignment_type": "pool_number", "status": "free", "is_active": "1"})
        self.request("/phones/create", method="POST", body=body)
        body = urlencode({"phone_number": "525550099998", "usage_type": "pool_member"})
        captured, content = self.request("/routes/1/numbers/add", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("рабочий статус номера должен быть ‘Используется’", content)

    def test_route_number_bulk_add_reports_status_errors_and_adds_used(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            route_id = conn.execute("SELECT id FROM routes WHERE id NOT IN (SELECT route_id FROM route_phone_numbers WHERE is_active = 1) LIMIT 1").fetchone()["id"]
            conn.execute("UPDATE phone_numbers SET status = 'free' WHERE number = '525550000005'")
            conn.commit()
        finally:
            conn.close()
        body = urlencode({"phone_numbers": "525550000005\n525550000006"})
        captured, _ = self.request(f"/routes/{route_id}/numbers/bulk-add", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        location = dict(captured["headers"])["Location"]
        self.assertIn("numbers/manage?notice=", location)
        captured, content = self.request(location)
        self.assertIn("Добавлено 1 из 2", content)
        self.assertIn("рабочий статус номера должен быть ‘Используется’", content)
        self.assertIn("525550000006", content)


    def test_route_numbers_read_only_page_shows_numbers_without_management_forms(self):
        self.request("/routes")
        captured, content = self.request("/routes/1/numbers")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Скопировать все", content)
        self.assertIn("525550000004", content)
        self.assertNotIn('action="/routes/1/numbers/add"', content)
        self.assertNotIn('action="/routes/1/numbers/bulk-add"', content)
        self.assertNotIn('action="/routes/1/numbers/remove"', content)
        self.assertNotIn("Причина", content)

    def test_route_number_management_errors_stay_in_context_and_use_error_style(self):
        self.request("/routes")
        body = urlencode({"number": "525550099997", "country_id": "1", "provider_id": "1", "assignment_type": "pool_number", "status": "free", "is_active": "1"})
        self.request("/phones/create", method="POST", body=body)
        body = urlencode({"phone_number": "525550099997", "usage_type": "pool_member"})
        captured, content = self.request("/routes/1/numbers/add", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Номера маршрута / АОНы", content)
        self.assertIn("class='error'", content)
        self.assertIn("рабочий статус номера должен быть ‘Используется’", content)
        self.assertIn('action="/routes/1/numbers/add"', content)

    def test_route_number_bulk_add_error_notice_uses_error_style(self):
        self.request("/routes")
        body = urlencode({"number": "525550099996", "country_id": "1", "provider_id": "1", "assignment_type": "pool_number", "status": "free", "is_active": "1"})
        self.request("/phones/create", method="POST", body=body)
        body = urlencode({"phone_numbers": "525550099996"})
        captured, _ = self.request("/routes/1/numbers/bulk-add", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        location = dict(captured["headers"])["Location"]
        self.assertIn("notice_type=error", location)
        captured, content = self.request(location)
        self.assertIn("class='error'", content)
        self.assertNotIn("class='ok'", content)

    def test_route_number_provider_inactive_error_text_is_preserved(self):
        self.request("/routes")
        body = urlencode({"number": "525550099995", "country_id": "1", "provider_id": "1", "assignment_type": "pool_number", "status": "used"})
        self.request("/phones/create", method="POST", body=body)
        conn = server.connect(server.DB_PATH)
        try:
            conn.execute("UPDATE phone_numbers SET is_active = 0 WHERE number = '525550099995'")
            conn.commit()
        finally:
            conn.close()
        body = urlencode({"phone_number": "525550099995", "usage_type": "pool_member"})
        captured, content = self.request("/routes/1/numbers/add", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Нельзя добавить номер в маршрут: номер не активен у провайдера", content)
        self.assertIn("class='error'", content)

    def test_phones_page_and_export_show_route_names(self):
        self.request("/routes")
        body = urlencode({"number": "525550099999", "country_id": "1", "provider_id": "1", "assignment_type": "pool_number", "status": "used", "is_active": "1"})
        self.request("/phones/create", method="POST", body=body)
        captured, content = self.request("/phones")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Маршруты", content)
        self.assertIn("Мексика/Sancom/Demo_0827@", content)
        self.assertIn("<td data-col='routes'>—</td>", content)
        self.assertNotIn("Маршрутов</button>", content)

        conn = server.connect(server.DB_PATH)
        try:
            phone_id = conn.execute("SELECT id FROM phone_numbers WHERE number = '525550000001'").fetchone()["id"]
            route_id = conn.execute("SELECT id FROM routes WHERE name != 'Мексика/Sancom/Demo_0827@' LIMIT 1").fetchone()["id"]
            conn.execute(
                "INSERT INTO route_phone_numbers(route_id, phone_number_id, usage_type, is_active, added_by) VALUES (?, ?, 'pool_member', 1, 1)",
                (route_id, phone_id),
            )
            conn.commit()
        finally:
            conn.close()
        _, content = self.request("/phones")
        self.assertIn("Мексика/Sancom/Demo_0827@", content)
        self.assertIn(", ", content)

        captured, content = self.request("/phones?export=csv")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Маршруты", content)
        self.assertIn("Мексика/Sancom/Demo_0827@", content)

    def test_route_numbers_and_edit_pages_are_available_without_numbers(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            route_id = conn.execute("SELECT id FROM routes WHERE id NOT IN (SELECT route_id FROM route_phone_numbers WHERE is_active = 1) LIMIT 1").fetchone()["id"]
        finally:
            conn.close()
        captured, content = self.request(f"/routes/{route_id}/numbers")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Скопировать все", content)
        self.assertNotIn("+ Добавить номер", content)
        self.assertNotIn("Массовое добавление", content)
        self.assertNotIn("Исключить из маршрута", content)
        captured, content = self.request(f"/routes/{route_id}/edit")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Номера маршрута / АОНы", content)
        self.assertIn(f"/routes/{route_id}/numbers/manage", content)
        captured, content = self.request(f"/routes/{route_id}/numbers/manage")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("+ Добавить номер", content)
        self.assertIn("Массовое добавление", content)
        self.assertIn("Исключить из маршрута", content)
        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(f"/routes/{route_id}/numbers", content)
        self.assertIn("Показать номера", content)

    def test_duplicate_route_returns_user_message(self):
        self.request("/routes")
        body = urlencode({"country_id": "1", "provider_id": "2", "provider_prefix_id": "3", "project_label": "", "cli_source_type": "pool", "cli_source_label": "Demo_A", "is_actual": "1"})
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
        captured, content = self.request("/admin/dictionaries?section=providers")
        self.assertIn("NewTel", content)
        captured, content = self.request("/admin/dictionaries?section=currencies")
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
        self.assertIn("Мексика/Miatel/Demo_A@", content)

    def test_phone_type_dictionary_drives_phone_forms(self):
        self.request("/routes")
        captured, content = self.request("/phones/1/edit")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Mobile", content)
        self.assertIn("Fixed Line", content)
        self.assertNotIn("name='phone_type' value", content)

    def test_provider_change_can_create_none_scope_event(self):
        self.request("/routes")
        body = urlencode({"apply_scope": "none", "event_at": "2026-06-10T10:00", "provider_id": "1", "reason": "Другое", "comment": "Провайдер сообщил о работах"})
        captured, _ = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Провайдер сообщил о работах", content)



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



    def test_server_priorities_show_all_active_server_blocks_empty_rows_and_route_details(self):
        self.request("/routes")
        captured, content = self.request("/admin/server-priorities")
        self.assertEqual(captured["status"], "200 OK")
        for server_name in ("EU1", "EU2", "EU3", "EU4", "EU5", "EU6", "EU7", "EU8", "EU9"):
            self.assertIn(f"Сервер: {server_name}", content)
        self.assertLess(content.index("Сервер: EU1"), content.index("Сервер: EU2"))
        self.assertLess(content.index("Сервер: EU2"), content.index("Сервер: EU3"))
        self.assertIn("<th data-col='geo' title='GEO'>GEO</th><th data-col='current_priority' title='Текущий приоритет'>Текущий приоритет</th><th data-col='previous_priority' title='Предыдущий приоритет'>Предыдущий приоритет</th><th data-col='actions' title='Действия'>Действия</th>", content)
        self.assertIn("Нет настроенных приоритетов", content)
        eu3_block = content.split("Сервер: EU3", 1)[1].split("</section>", 1)[0]
        self.assertIn("Нет настроенных приоритетов", eu3_block)
        eu1_block = content.split("Сервер: EU1", 1)[1].split("</section>", 1)[0]
        self.assertIn("<td data-col='geo'>Мексика</td><td data-col='current_priority'>Miatel / Мексика/Miatel/Demo_A@</td><td data-col='previous_priority'>—</td>", eu1_block)
        self.assertIn("<summary>Редактировать</summary>", eu1_block)
        self.assertIn("Текущий провайдер: Miatel", eu1_block)
        self.assertIn("Текущий маршрут: Мексика/Miatel/Demo_A@", eu1_block)
        self.assertIn("Предыдущий провайдер: —", eu1_block)
        self.assertIn("Предыдущий маршрут: —", eu1_block)
        self.assertIn("name='current_route_id'", eu1_block)
        self.assertIn("Сохранить текущий маршрут", eu1_block)

    def test_server_priorities_server_filter_keeps_empty_selected_server_block(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            eu3_id = conn.execute("SELECT id FROM servers WHERE name = 'EU3'").fetchone()["id"]
        finally:
            conn.close()
        captured, content = self.request(f"/admin/server-priorities?server_id={eu3_id}")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Сервер: EU3", content)
        self.assertNotIn("Сервер: EU1", content)
        eu3_block = content.split("Сервер: EU3", 1)[1].split("</section>", 1)[0]
        self.assertIn("Нет настроенных приоритетов", eu3_block)
        self.assertNotIn("<summary>Редактировать</summary>", eu3_block)

    def test_server_priorities_geo_filter_keeps_server_blocks_and_filters_rows(self):
        self.request("/routes")
        captured, content = self.request("/admin/server-priorities?country_id=1")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Сервер: EU3", content)
        self.assertIn("Сервер: EU1", content)
        eu3_block = content.split("Сервер: EU3", 1)[1].split("</section>", 1)[0]
        eu1_block = content.split("Сервер: EU1", 1)[1].split("</section>", 1)[0]
        self.assertIn("Нет настроенных приоритетов", eu3_block)
        self.assertIn("<td data-col='geo'>Мексика</td><td data-col='current_priority'>Miatel / Мексика/Miatel/Demo_A@</td>", eu1_block)

    def test_server_priority_manual_route_update_changes_current_previous_and_logs_event(self):
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
            event = conn.execute("""
                SELECT * FROM change_log
                WHERE entity_type = 'server_route_priority'
                  AND entity_id = 1
                  AND change_type = 'server_route_priority.current_route_updated'
            """).fetchone()
            self.assertIsNotNone(event)
            self.assertTrue(event["summary"])
        finally:
            conn.close()
        captured, content = self.request("/admin/server-priorities")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("<td data-col='geo'>Мексика</td><td data-col='current_priority'>Sancom / Мексика/Sancom/Demo_0827@</td><td data-col='previous_priority'>Miatel / Мексика/Miatel/Demo_A@</td>", content)



    def test_change_log_labels_date_as_server_time(self):
        captured, content = self.request("/admin/change-log")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Дата (UTC/server time)", content)

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
        self.assertIn('name="company_id_external"', content)
        self.assertIn('name="routing_mode"', content)
        self.assertIn('name="show_history"', content)
        self.assertIn("syncAutorotation", content)

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
        captured, content = self.request("/admin/company-routing-settings?country_id=1&server_id=1&routing_mode=server_priority&company_id_external=1001&is_active=1")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("CC Mexico Demo", content)
        self.assertIn("1001", content)
        self.assertIn("server_priority", content)
        self.assertIn("manual routing note", content)
        captured, content = self.request("/admin/company-routing-settings?company_id_external=no-match")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("manual routing note", content)

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



class RoutingEventsServerSmokeTest(unittest.TestCase):
    setUp = ServerSmokeTest.setUp
    tearDown = ServerSmokeTest.tearDown
    request = ServerSmokeTest.request
    user_cookie = ServerSmokeTest.user_cookie

    def test_server_priority_event_updates_dashboard_and_change_log(self):
        self.request("/routes")
        body = urlencode({
            "apply_scope": "server_priority",
            "event_at": "2026-06-10T11:00",
            "country_id": "1",
            "server_id": "1",
            "provider_id": "1",
            "new_route_id": "1",
            "reason": "Плановое переключение",
            "comment": "Переключили EU1 на Sancom",
        })
        captured, _ = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        captured, content = self.request("/admin/server-priorities")
        self.assertEqual(captured["status"], "200 OK")
        eu1_block = content.split("Сервер: EU1", 1)[1].split("</section>", 1)[0]
        self.assertIn("Sancom / Мексика/Sancom/Demo_0827@", eu1_block)
        captured, content = self.request("/admin/change-log")
        self.assertIn("routing_event.created", content)
        self.assertIn("routing_event.applied_to_server_priority", content)

    def test_server_priority_event_accepts_multiple_server_ids_from_ui(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            target = conn.execute("""
                SELECT r.id AS route_id, p.id AS provider_id
                FROM routes r
                JOIN providers p ON p.id = r.provider_id
                WHERE r.country_id = 1 AND p.name = 'DemoTel'
                ORDER BY r.id
                LIMIT 1
            """).fetchone()
        finally:
            conn.close()
        body = urlencode([
            ("apply_scope", "server_priority"),
            ("event_at", "2026-06-10T11:30"),
            ("country_id", "1"),
            ("server_ids", "1"),
            ("server_ids", "2"),
            ("provider_id", str(target["provider_id"])),
            ("new_route_id", str(target["route_id"])),
            ("reason", "Плановое переключение"),
            ("comment", "Переключили EU1 и EU2"),
        ])
        captured, _ = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0], 1)
            server_ids = [row["server_id"] for row in conn.execute("SELECT server_id FROM routing_event_servers ORDER BY server_id")]
            self.assertEqual(server_ids, [1, 2])
            priority_rows = conn.execute("""
                SELECT server_id, current_route_id
                FROM server_route_priorities
                WHERE country_id = 1 AND server_id IN (1, 2)
                ORDER BY server_id
            """).fetchall()
            self.assertEqual([(row["server_id"], row["current_route_id"]) for row in priority_rows], [(1, target["route_id"]), (2, target["route_id"])])
        finally:
            conn.close()

    def test_provider_changes_journal_shows_affected_servers_human_readable(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            target = conn.execute("SELECT id, provider_id FROM routes WHERE name = 'Мексика/Sancom/Demo_0827@'").fetchone()
        finally:
            conn.close()
        body = urlencode([
            ("apply_scope", "server_priority"),
            ("event_at", "2026-06-10T11:45"),
            ("country_id", "1"),
            ("server_ids", "1"),
            ("server_ids", "2"),
            ("server_ids", "3"),
            ("provider_id", str(target["provider_id"])),
            ("new_route_id", str(target["id"])),
            ("reason", "Плановое переключение"),
            ("comment", "Проверяем журнал по нескольким серверам"),
        ])
        captured, _ = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Серверы:<ul class='event-server-list'>", content)
        self.assertIn("EU1", content)
        self.assertIn("EU2", content)
        self.assertIn("EU3", content)
        self.assertIn("Miatel / Мексика/Miatel/Demo_A@ → Sancom / Мексика/Sancom/Demo_0827@", content)
        self.assertIn("Sancom / Мексика/Sancom/Demo_0827@ → Sancom / Мексика/Sancom/Demo_0827@", content)
        self.assertIn("— → Sancom / Мексика/Sancom/Demo_0827@", content)
        self.assertIn("применено", content)
        self.assertIn("пропущено: уже был выбран этот маршрут", content)
        self.assertNotIn("skipped_noop", content)
        self.assertNotIn("affected_servers", content)

    def test_provider_changes_journal_keeps_legacy_single_server_event_display(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            conn.execute(
                """
                INSERT INTO routing_events(
                    event_at, apply_scope, reason, country_id, server_id, provider_id,
                    old_route_id, new_route_id, comment, snapshot_json, created_by, updated_by
                ) VALUES (?, 'server_priority', ?, 1, 1, 2, 2, 1, ?, ?, ?, ?)
                """,
                ("2026-06-10 09:30", "Другое", "legacy single server event", "{}", server.ADMIN_ID, server.ADMIN_ID),
            )
            conn.commit()
        finally:
            conn.close()
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("legacy single server event", content)
        self.assertIn("EU1", content)
        self.assertIn("Мексика/Miatel/Demo_A@ → Мексика/Sancom/Demo_0827@", content)

    def test_provider_changes_server_filter_finds_multi_server_events(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            target = conn.execute("SELECT id, provider_id FROM routes WHERE name = 'Мексика/Sancom/Demo_0827@'").fetchone()
        finally:
            conn.close()
        body = urlencode([
            ("apply_scope", "server_priority"),
            ("event_at", "2026-06-10T12:15"),
            ("country_id", "1"),
            ("server_ids", "1"),
            ("server_ids", "3"),
            ("provider_id", str(target["provider_id"])),
            ("new_route_id", str(target["id"])),
            ("reason", "Плановое переключение"),
            ("comment", "Фильтр должен найти EU1 и EU3"),
        ])
        captured, _ = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")

        captured, content = self.request("/provider-changes?server_id=3")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Фильтр должен найти EU1 и EU3", content)
        self.assertIn("EU3", content)

        captured, content = self.request("/provider-changes?server_id=2")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("Фильтр должен найти EU1 и EU3", content)

    def test_provider_changes_server_filter_keeps_legacy_single_server_events(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            conn.execute(
                """
                INSERT INTO routing_events(
                    event_at, apply_scope, reason, country_id, server_id, provider_id,
                    old_route_id, new_route_id, comment, snapshot_json, created_by, updated_by
                ) VALUES (?, 'server_priority', ?, 1, 1, 2, 2, 1, ?, ?, ?, ?)
                """,
                ("2026-06-10 12:45", "Другое", "legacy filter single server event", "{}", server.ADMIN_ID, server.ADMIN_ID),
            )
            conn.commit()
        finally:
            conn.close()

        captured, content = self.request("/provider-changes?server_id=1")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("legacy filter single server event", content)
        self.assertIn("Мексика/Miatel/Demo_A@ → Мексика/Sancom/Demo_0827@", content)

        captured, content = self.request("/provider-changes?server_id=2")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("legacy filter single server event", content)

    def test_provider_changes_none_and_campaign_setting_forms_still_render(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("value='none' checked", content)
        self.assertIn("data-scopes='none'", content)
        self.assertIn("Настройка кампании", content)
        self.assertIn("data-scopes='campaign_setting'", content)
        self.assertIn("Событие будет сохранено в журнале и применено", content)

    def test_campaign_setting_route_is_preserved_when_toggling_autorotation(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            route_id = conn.execute("SELECT id FROM routes WHERE name = 'Мексика/Sancom/Demo_0827@'").fetchone()[0]
            repo = server.Repository(conn)
            repo.create_company_routing_setting(
                calling_company_id=1,
                country_id=1,
                server_id=1,
                route_id=route_id,
                routing_mode="campaign_route",
                has_autorotation=False,
                comment="manual route before autorotation",
                created_by=server.ADMIN_ID,
            )
        finally:
            conn.close()

        enable_body = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T12:00", "calling_company_id": "1", "company_change_type": "enable_autorotation", "reason": "Тест нового маршрута", "comment": "Включаем авторотацию"})
        captured, _ = self.request("/provider-changes/create", method="POST", body=enable_body)
        self.assertEqual(captured["status"], "303 See Other")
        captured, content = self.request("/admin/company-routing-settings")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("mixed", content)
        self.assertIn("Да", content)
        self.assertIn("Мексика/Sancom/Demo_0827@", content)
        self.assertIn("Провайдер: Sancom", content)

        disable_body = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T13:00", "calling_company_id": "1", "company_change_type": "disable_autorotation", "reason": "Тест нового маршрута", "comment": "Выключаем авторотацию"})
        captured, _ = self.request("/provider-changes/create", method="POST", body=disable_body)
        self.assertEqual(captured["status"], "303 See Other")
        captured, content = self.request("/admin/company-routing-settings")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("campaign_route", content)
        self.assertIn("Нет", content)
        self.assertIn("Мексика/Sancom/Demo_0827@", content)
        self.assertIn("Провайдер: Sancom", content)

    def test_campaign_setting_enable_autorotation_ui_and_applied_settings(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("В MVP это только логирует событие", content)
        self.assertIn("Событие будет сохранено в журнале и применено", content)
        self.assertNotIn("id='company-autorotation'", content)
        body = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T12:00", "calling_company_id": "2", "company_change_type": "enable_autorotation", "reason": "Тест нового маршрута", "comment": "Включаем авторотацию"})
        captured, _ = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        captured, content = self.request("/admin/company-routing-settings")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("1002", content)
        self.assertIn("autorotation", content)

    def test_campaign_setting_event_for_company_without_settings(self):
        self.request("/routes")
        body = urlencode({"server_id": "1", "country_id": "1", "company_name": "CC Mexico 1002", "company_id_external": "1002", "line_count": "1", "dial_set_count": "1", "retry_interval_seconds": "30", "has_autorotation": "0", "is_active": "1", "comment": ""})
        self.request("/companies/create", method="POST", body=body)
        body = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T12:00", "calling_company_id": "2", "company_change_type": "enable_autorotation", "reason": "Тест нового маршрута", "comment": "Логируем включение авторотации"})
        captured, _ = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            event = conn.execute("SELECT * FROM routing_events WHERE calling_company_id = 2").fetchone()
            self.assertEqual(event["old_company_routing_mode"], "server_priority")
            self.assertEqual(event["old_company_has_autorotation"], 0)
            setting = conn.execute("SELECT * FROM company_routing_settings WHERE calling_company_id = 2 AND is_active = 1 AND valid_to IS NULL").fetchone()
            self.assertIsNotNone(setting)
            self.assertEqual(setting["routing_mode"], "autorotation")
            self.assertEqual(setting["has_autorotation"], 1)
            self.assertIsNone(setting["route_id"])
        finally:
            conn.close()

    def test_event_list_sorted_by_event_at_desc_and_inactive_filter(self):
        self.request("/routes")
        first = urlencode({"apply_scope": "none", "event_at": "2026-06-09T10:00", "provider_id": "1", "reason": "Другое", "comment": "старое событие"})
        second = urlencode({"apply_scope": "none", "event_at": "2026-06-10T10:00", "provider_id": "1", "reason": "Другое", "comment": "новое событие"})
        self.request("/provider-changes/create", method="POST", body=first)
        self.request("/provider-changes/create", method="POST", body=second)
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertLess(content.index("новое событие"), content.index("старое событие"))
        self.request("/provider-changes/1/deactivate", method="POST", body=urlencode({"deactivation_reason": "архив"}))
        _, content = self.request("/provider-changes")
        self.assertNotIn("старое событие", content)
        _, content = self.request("/provider-changes?include_inactive=1")
        self.assertIn("старое событие", content)

    def add_extra_routes(self, count=55, prefix="BulkRoute"):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            country_id = conn.execute("SELECT id FROM countries WHERE code = 'MEX'").fetchone()["id"]
            provider_id = conn.execute("SELECT id FROM providers WHERE name = 'DemoTel'").fetchone()["id"]
            admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
            for index in range(count):
                conn.execute(
                    """
                    INSERT INTO routes(country_id, provider_id, name, cli_source_type, cli_source_label, is_actual, priority_status, comment, created_by)
                    VALUES (?, ?, ?, 'rnd', ?, 1, 'normal', ?, ?)
                    """,
                    (country_id, provider_id, f"{prefix} {index:03d}", f"{prefix}{index:03d}", "bulk export row", admin_id),
                )
            conn.commit()
        finally:
            conn.close()

    def test_table_pages_limit_routes_to_50_and_preserve_filter_pagination(self):
        self.add_extra_routes(55)
        captured, content = self.request("/routes?provider_id=3&page=not-a-number")
        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(content.count("bulk export row"), 50)
        self.assertIn("Страница 1 из", content)
        self.assertIn("provider_id=3&amp;page=2", content)

        captured, content = self.request("/routes?provider_id=3&page=2")
        self.assertEqual(captured["status"], "200 OK")
        self.assertLessEqual(content.count("bulk export row"), 50)
        self.assertIn("provider_id=3&amp;page=1", content)

    def test_export_link_preserves_filters_and_removes_page_limit(self):
        captured, content = self.request("/routes?provider_id=3&page=3&limit=50")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(">Экспорт</a>", content)
        self.assertIn("/routes?provider_id=3&amp;export=csv", content)
        self.assertNotIn("export=csv&amp;page", content)
        self.assertNotIn("limit=50&amp;export=csv", content)

    def test_csv_export_format_and_all_filtered_rows(self):
        self.add_extra_routes(55)
        captured, content = self.request("/routes?provider_id=3&page=2&export=csv")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(("Content-Type", "text/csv; charset=utf-8"), captured["headers"])
        self.assertIn(("Content-Disposition", "attachment; filename=routes_export.csv"), captured["headers"])
        self.assertTrue(content.startswith("\ufeffGEO;Провайдер;Маршрут;Сервер;Активен;Комментарий"))
        self.assertIn(";", content.splitlines()[0])
        self.assertEqual(content.count("bulk export row"), 55)

    def test_csv_export_respects_permissions(self):
        captured, content = self.request("/routes?export=csv", cookie=self.user_cookie("guest"))
        self.assertEqual(captured["status"], "200 OK")
        self.assertTrue(content.startswith("\ufeff"))

        captured, content = self.request("/provider-changes?export=csv", cookie=self.user_cookie("guest"))
        self.assertEqual(captured["status"], "403 Forbidden")
        self.assertIn("Нет доступа", content)

        captured, content = self.request("/admin/server-priorities?export=csv", cookie=self.user_cookie("duty"))
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("GEO;Сервер;Провайдер/маршрут;Приоритет;Активен;Комментарий", content)


if __name__ == "__main__":
    unittest.main()

class RolePermissionTest(ServerSmokeTest):
    def user_cookie(self, username):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
        finally:
            conn.close()
        return f"mvp_current_user_id={user_id}"

    def test_admin_sees_all_main_navigation_and_admin_navigation(self):
        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        for label in ["Маршруты", "Тарифы", "Купленные номера", "Кампании прозвона", "Смена провайдеров", "Администрирование"]:
            self.assertIn(label, content)
        self.assertIn("href='/admin/users'", content)

    def test_operator_sees_working_sections_and_allowed_admin_navigation_only(self):
        captured, content = self.request("/routes", cookie=self.user_cookie("duty"))
        self.assertEqual(captured["status"], "200 OK")
        for label in ["Маршруты", "Тарифы", "Купленные номера", "Кампании прозвона", "Смена провайдеров"]:
            self.assertIn(label, content)
        self.assertIn("Администрирование</button>", content)
        self.assertIn("href='/admin/server-priorities'", content)
        self.assertIn("Приоритет по серверам", content)
        self.assertIn("href='/admin/company-routing-settings'", content)
        self.assertIn("Схема маршрутизации кампаний", content)
        self.assertNotIn("href='/admin/users'", content)
        self.assertNotIn("href='/admin/dictionaries'", content)
        self.assertNotIn("href='/admin/import'", content)
        self.assertNotIn("href='/admin/change-log'", content)
        self.assertNotIn("href='/admin/currency-rates'", content)
        self.assertNotIn("href='/admin/change-reasons'", content)
        self.assertNotIn("href='/admin/naming-rules'", content)

    def test_guest_sees_only_routes_and_tariffs(self):
        captured, content = self.request("/routes", cookie=self.user_cookie("guest"))
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("href='/routes'", content)
        self.assertIn("href='/tariffs'", content)
        self.assertNotIn("href='/phones'", content)
        self.assertNotIn("href='/companies'", content)
        self.assertNotIn("href='/provider-changes'", content)
        self.assertNotIn("Администрирование</button>", content)

    def test_operator_can_open_provider_changes(self):
        captured, content = self.request("/provider-changes", cookie=self.user_cookie("duty"))
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Смена провайдеров", content)

    def test_guest_cannot_open_provider_changes(self):
        captured, content = self.request("/provider-changes", cookie=self.user_cookie("guest"))
        self.assertEqual(captured["status"], "403 Forbidden")
        self.assertIn("Нет доступа", content)

    def test_operator_can_read_but_not_write_server_priorities(self):
        cookie = self.user_cookie("duty")
        captured, content = self.request("/admin/server-priorities", cookie=cookie)
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Приоритет по серверам", content)
        self.assertNotIn("Сохранить текущий маршрут", content)
        captured, content = self.request("/admin/server-priorities/1/update", method="POST", body=urlencode({"current_route_id": "1", "comment": "blocked"}), cookie=cookie)
        self.assertEqual(captured["status"], "403 Forbidden")
        self.assertIn("Нет доступа", content)

    def test_operator_can_read_but_not_write_company_routing_settings(self):
        cookie = self.user_cookie("duty")
        captured, content = self.request("/admin/company-routing-settings", cookie=cookie)
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Схема маршрутизации кампаний", content)
        self.assertNotIn("+ Добавить схему маршрутизации кампании", content)
        self.assertNotIn("/admin/company-routing-settings/create", content)
        self.assertNotIn("/admin/company-routing-settings/1/update", content)
        self.assertNotIn("/admin/company-routing-settings/1/deactivate", content)

        captured, content = self.request(
            "/admin/company-routing-settings/create",
            method="POST",
            body=urlencode({"calling_company_id": "1", "country_id": "1", "server_id": "1", "routing_mode": "server_priority"}),
            cookie=cookie,
        )
        self.assertEqual(captured["status"], "403 Forbidden")
        self.assertIn("Нет доступа", content)

        captured, content = self.request(
            "/admin/company-routing-settings/1/update",
            method="POST",
            body=urlencode({"country_id": "1", "server_id": "1", "routing_mode": "server_priority"}),
            cookie=cookie,
        )
        self.assertEqual(captured["status"], "403 Forbidden")
        self.assertIn("Нет доступа", content)

        captured, content = self.request("/admin/company-routing-settings/1/deactivate", method="POST", body=urlencode({}), cookie=cookie)
        self.assertEqual(captured["status"], "403 Forbidden")
        self.assertIn("Нет доступа", content)

    def test_guest_cannot_access_company_routing_settings(self):
        captured, content = self.request("/admin/company-routing-settings", cookie=self.user_cookie("guest"))
        self.assertEqual(captured["status"], "403 Forbidden")
        self.assertIn("Нет доступа", content)

    def test_operator_still_cannot_access_other_admin_sections(self):
        cookie = self.user_cookie("duty")
        for path in ["/admin/users", "/admin/dictionaries", "/admin/import", "/admin/change-log", "/admin/currency-rates", "/admin/change-reasons", "/admin/naming-rules"]:
            with self.subTest(path=path):
                captured, content = self.request(path, cookie=cookie)
                self.assertEqual(captured["status"], "403 Forbidden")
                self.assertIn("Нет доступа", content)

    def test_guest_cannot_access_server_priorities(self):
        captured, content = self.request("/admin/server-priorities", cookie=self.user_cookie("guest"))
        self.assertEqual(captured["status"], "403 Forbidden")
        self.assertIn("Нет доступа", content)

    def test_operator_and_guest_cannot_open_admin_users(self):
        for username in ("duty", "guest"):
            with self.subTest(username=username):
                captured, content = self.request("/admin/users", cookie=self.user_cookie(username))
                self.assertEqual(captured["status"], "403 Forbidden")
                self.assertIn("Нет доступа", content)

    def test_guest_cannot_post_create_or_edit_actions(self):
        cookie = self.user_cookie("guest")
        captured, content = self.request("/tariffs/create", method="POST", body=urlencode({}), cookie=cookie)
        self.assertEqual(captured["status"], "403 Forbidden")
        self.assertIn("Нет доступа", content)
        captured, content = self.request("/routes/1/update", method="POST", body=urlencode({"name": "Blocked"}), cookie=cookie)
        self.assertEqual(captured["status"], "403 Forbidden")

    def test_operator_can_post_provider_change_create(self):
        body = urlencode({
            "event_at": "2026-06-14T10:00",
            "apply_scope": "none",
            "provider_id": "1",
            "reason": "Плановая смена",
            "comment": "operator allowed",
        })
        captured, content = self.request("/provider-changes/create", method="POST", body=body, cookie=self.user_cookie("duty"))
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/provider-changes"), captured["headers"])

    def test_operator_cannot_post_admin_dictionary_import_or_user_actions(self):
        cookie = self.user_cookie("duty")
        for path, body in [
            ("/admin/dictionaries/countries/create", urlencode({"name": "Blocked"})),
            ("/admin/import/preview", urlencode({"entity_type": "routes", "csv_data": ""})),
            ("/admin/users/create", urlencode({"username": "blocked", "display_name": "Blocked"})),
        ]:
            with self.subTest(path=path):
                captured, content = self.request(path, method="POST", body=body, cookie=cookie)
                self.assertEqual(captured["status"], "403 Forbidden")
                self.assertIn("Нет доступа", content)

    def test_edit_create_buttons_hidden_for_read_only_sections(self):
        cookie = self.user_cookie("guest")
        captured, content = self.request("/routes", cookie=cookie)
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("+ Добавить маршрут", content)
        self.assertNotIn("✏️ Редактировать", content)
        captured, content = self.request("/tariffs", cookie=cookie)
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("+ Добавить тариф", content)
        self.assertNotIn("Деактивировать", content)
