import csv
import json
import io
import os
import re
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlencode

import app.server as server


class _FakeBalanceResponse:
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.payload


class HlrBalanceHelperTest(unittest.TestCase):
    def test_hlr_balance_url_is_derived_from_hlr_endpoint(self):
        self.assertEqual(
            server.hlr_balance_url({"api_url": "https://api.hlrlookup.com/apiv2/hlr"}),
            "https://api.hlrlookup.com/apiv2/balance",
        )

    def test_get_hlr_balance_posts_credentials_and_returns_credits(self):
        env = {
            "HLR_MODE": "production",
            "HLR_API_URL": "https://api.hlrlookup.com/apiv2/hlr",
            "HLR_API_KEY": "key-secret",
            "HLR_API_SECRET": "secret-token",
            "HLR_TIMEOUT_MS": "5000",
        }
        with patch.dict(os.environ, env, clear=False), patch("app.server.urlopen", return_value=_FakeBalanceResponse(b'{"Status":"OK","Credits":1234.5}')) as mocked_urlopen:
            balance = server.get_hlr_balance()
        self.assertEqual(balance["status"], "ok")
        self.assertEqual(balance["credits"], 1234.5)
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.hlrlookup.com/apiv2/balance")
        self.assertIn(b'"api_key": "key-secret"', request.data)
        self.assertIn(b'"api_secret": "secret-token"', request.data)

    def test_get_hlr_balance_reports_missing_credentials_without_calling_api(self):
        with patch.dict(os.environ, {"HLR_MODE": "production", "HLR_API_URL": "https://api.hlrlookup.com/apiv2/hlr", "HLR_API_KEY": "", "HLR_API_SECRET": ""}, clear=False), patch("app.server.urlopen") as mocked_urlopen:
            balance = server.get_hlr_balance()
        self.assertEqual(balance["status"], "not_configured")
        self.assertIsNone(balance["credits"])
        mocked_urlopen.assert_not_called()


class HlrDailyUsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.conn = server.connect(self.tmp.name)
        server.init_db(self.conn)
        self.repo = server.Repository(self.conn)
        server._REQUEST_CONTEXT.clear()
        server._REQUEST_CONTEXT["repo"] = self.repo

    def tearDown(self):
        server._REQUEST_CONTEXT.clear()
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_hlr_run_check_records_valid_numbers_only_and_sums_credits(self):
        with patch.dict(os.environ, {"HLR_MODE": "demo", "HLR_DAILY_CHECK_LIMIT": "5"}, clear=False):
            results, _summary = server.hlr_run_check("+48123456789\nbad-number")
            usage = server.hlr_usage_with_limits()
        self.assertEqual(len(results), 2)
        self.assertEqual(usage["checked_today"], 1)
        self.assertEqual(usage["remaining_today"], 4)
        self.assertEqual(usage["last_check_count"], 1)
        self.assertEqual(usage["last_check_credits"], 0)
        self.assertEqual(usage["credits_spent_today"], 0)

    def test_hlr_daily_limit_blocks_before_api_call_and_does_not_increment_usage(self):
        server.hlr_record_daily_usage(2, None)
        with patch.dict(os.environ, {"HLR_MODE": "production", "HLR_DAILY_CHECK_LIMIT": "2"}, clear=False), patch("app.server.hlr_real_api_check") as mocked_check:
            with self.assertRaises(server.BusinessRuleError) as ctx:
                server.hlr_run_check("+48123456789")
            usage = server.hlr_usage_with_limits()
        self.assertEqual(str(ctx.exception), "Дневной лимит HLR исчерпан.")
        mocked_check.assert_not_called()
        self.assertEqual(usage["checked_today"], 2)
        self.assertEqual(usage["remaining_today"], 0)

    def test_hlr_transport_failure_rows_are_not_counted_as_daily_usage(self):
        with patch.dict(os.environ, {"HLR_MODE": "production", "HLR_DAILY_CHECK_LIMIT": "5"}, clear=False), patch("app.server.hlr_real_api_check", return_value=[server.hlr_error_result({"original": "+48123456789", "normalized": "+48123456789"}, server.hlr_config(), "connection_error")]):
            results, _summary = server.hlr_run_check("+48123456789")
            usage = server.hlr_usage_with_limits()
        self.assertEqual(len(results), 1)
        self.assertEqual(usage["checked_today"], 0)
        self.assertEqual(usage["remaining_today"], 5)
        self.assertEqual(usage["last_check_count"], 0)

    def test_hlr_daily_limit_uses_env_when_no_override(self):
        with patch.dict(os.environ, {"HLR_DAILY_CHECK_LIMIT": "7"}, clear=False):
            state = server.hlr_daily_limit_state()
            usage = server.hlr_usage_with_limits()
        self.assertEqual(state["daily_limit_effective"], 7)
        self.assertEqual(state["daily_limit_source"], "env")
        self.assertEqual(usage["daily_limit"], 7)
        self.assertEqual(usage["remaining_today"], 7)

    def test_hlr_daily_limit_uses_admin_override_and_reset_returns_to_env(self):
        with patch.dict(os.environ, {"HLR_DAILY_CHECK_LIMIT": "7"}, clear=False):
            server.save_hlr_daily_limit_override("9")
            state = server.hlr_daily_limit_state()
            self.assertEqual(state["daily_limit_effective"], 9)
            self.assertEqual(state["daily_limit_source"], "admin_override")
            self.assertEqual(state["daily_limit_env"], 7)
            self.assertEqual(state["daily_limit_override"], 9)
            server.reset_hlr_daily_limit_override()
            reset_state = server.hlr_daily_limit_state()
        self.assertEqual(reset_state["daily_limit_effective"], 7)
        self.assertEqual(reset_state["daily_limit_source"], "env")
        self.assertIsNone(reset_state["daily_limit_override"])

    def test_hlr_daily_limit_rejects_invalid_override(self):
        for value in ("", "0", "-1", "1.5", "abc", "100001"):
            with self.subTest(value=value):
                with self.assertRaises(server.BusinessRuleError) as ctx:
                    server.save_hlr_daily_limit_override(value)
                self.assertEqual(str(ctx.exception), server.HLR_DAILY_LIMIT_ERROR)
        self.assertIsNone(server.hlr_daily_limit_state()["daily_limit_override"])

    def test_hlr_daily_limit_enforcement_uses_effective_override(self):
        server.hlr_record_daily_usage(2, None)
        with patch.dict(os.environ, {"HLR_MODE": "production", "HLR_DAILY_CHECK_LIMIT": "5"}, clear=False), patch("app.server.hlr_real_api_check") as mocked_check:
            server.save_hlr_daily_limit_override("2")
            with self.assertRaises(server.BusinessRuleError) as ctx:
                server.hlr_run_check("+48123456789")
            usage = server.hlr_usage_with_limits()
        self.assertEqual(str(ctx.exception), "Дневной лимит HLR исчерпан.")
        mocked_check.assert_not_called()
        self.assertEqual(usage["daily_limit"], 2)
        self.assertEqual(usage["remaining_today"], 0)


class HlrApiMappingTest(unittest.TestCase):
    def _row(self, raw):
        return server.hlr_result_from_api_item({"original": "48789662838", "normalized": "+48789662838"}, raw)

    def test_error_none_with_network_only_data_is_unknown_not_error(self):
        row = self._row({
            "error": "NONE",
            "uuid": "test",
            "credits_spent": 0,
            "detected_telephone_number": "48789662838",
            "formatted_telephone_number": "48789662838",
            "live_status": "NONE",
            "original_network": "AVAILABLE",
            "original_network_details": {
                "name": "Orange Polska S.A.",
                "mccmnc": "26003",
                "country_name": "Poland",
                "country_iso3": "POL",
                "area": "Poland",
                "country_prefix": "48",
            },
            "current_network": "AVAILABLE",
            "current_network_details": {
                "name": "Orange Polska S.A.",
                "mccmnc": "26003",
                "country_name": "Poland",
                "country_iso3": "POL",
                "country_prefix": "48",
            },
            "is_ported": "NO",
            "timestamp": "2026-07-01T15:01:14Z",
            "telephone_number_type": "MOBILE",
        })
        self.assertEqual(row["format_status"], "valid")
        self.assertEqual(row["number_type"], "mobile")
        self.assertEqual(row["country"], "Poland")
        self.assertEqual(row["operator"], "Orange Polska S.A.")
        self.assertEqual(row["raw_error"], "NONE")
        self.assertEqual(row["credits_spent"], 0)
        self.assertEqual(row["hlr_status_raw"], "NONE")
        self.assertEqual(row["live_status_raw"], "NONE")
        self.assertEqual(row["final_category"], "unknown")
        self.assertEqual(row["final_result"], "UNKNOWN")
        self.assertEqual(row["lead_quality_signal"], "unknown")
        self.assertIn("не вернул live-status", row["comment"])
        summary = server.hlr_summary([row])
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["unknown"], 1)

    def test_hlr_api_errors_are_mapped_from_non_none_error_values(self):
        insufficient = self._row({"error": "INSUFFICIENT_CREDIT", "telephone_number_type": "MOBILE"})
        self.assertEqual(insufficient["final_category"], "error")
        self.assertEqual(insufficient["final_result"], "ERROR")
        self.assertEqual(insufficient["comment"], "Недостаточно кредитов HLR API.")

        internal = self._row({"error": "INTERNAL_ERROR", "telephone_number_type": "MOBILE"})
        self.assertEqual(internal["final_category"], "error")
        self.assertEqual(internal["final_result"], "ERROR")
        self.assertEqual(internal["comment"], "HLR API вернул внутреннюю ошибку.")

    def test_live_dead_and_bad_format_mapping_still_work(self):
        live = self._row({"error": "NONE", "live_status": "LIVE", "telephone_number_type": "MOBILE"})
        self.assertEqual(live["final_category"], "ok")
        self.assertEqual(live["final_result"], "OK")

        dead = self._row({"error": "NONE", "live_status": "DEAD", "telephone_number_type": "MOBILE"})
        self.assertEqual(dead["final_category"], "bad")
        self.assertEqual(dead["final_result"], "DEAD")

        bad_format = self._row({"error": "NONE", "live_status": "NONE", "telephone_number_type": "BAD_FORMAT"})
        self.assertEqual(bad_format["final_category"], "bad")
        self.assertEqual(bad_format["final_result"], "BAD_FORMAT")


class ServerSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.old_path = server.DB_PATH
        server.DB_PATH = self.tmp.name

    def tearDown(self):
        server.DB_PATH = self.old_path
        os.unlink(self.tmp.name)

    def request(self, path, method="GET", body="", cookie="", auto_login=True):
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
        elif auto_login and path not in ("/login", "/logout"):
            conn = server.connect(server.DB_PATH)
            try:
                server.init_db(conn)
                server.ensure_seed(server.Repository(conn))
                row = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
            finally:
                conn.close()
            if row:
                environ["HTTP_COOKIE"] = f"{server.CURRENT_USER_COOKIE}={server.sign_user_id(row['id'])}"
        content = b"".join(server.app(environ, start_response)).decode("utf-8")
        return captured, content

    def user_cookie(self, username):
        self.request("/login")
        conn = server.connect(server.DB_PATH)
        try:
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
        finally:
            conn.close()
        return f"{server.CURRENT_USER_COOKIE}={server.sign_user_id(user_id)}"

    def grant_user_read(self, username, *sections):
        self.request("/login")
        conn = server.connect(server.DB_PATH)
        try:
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
            for section in sections:
                conn.execute(
                    """
                    INSERT INTO user_permissions(user_id, section_key, can_read, can_write, can_export)
                    VALUES (?, ?, 1, 0, 0)
                    ON CONFLICT(user_id, section_key) DO UPDATE SET can_read = excluded.can_read
                    """,
                    (user_id, section),
                )
            conn.commit()
        finally:
            conn.close()


    def test_hlr_daily_limit_endpoint_saves_reset_and_rejects_non_admin(self):
        with patch.dict(os.environ, {"HLR_DAILY_CHECK_LIMIT": "11"}, clear=False):
            captured, html = self.request("/hlr/config/daily-limit", method="POST", body=urlencode({"daily_limit_override": "13"}))
            self.assertEqual(captured["status"], "200 OK")
            self.assertIn("Дневной лимит HLR сохранён.", html)
            self.assertIn("<dt>daily_limit_source</dt><dd>admin_override</dd>", html)
            conn = server.connect(server.DB_PATH)
            try:
                value = conn.execute("SELECT value FROM app_settings WHERE key = ?", (server.HLR_DAILY_LIMIT_OVERRIDE_KEY,)).fetchone()["value"]
            finally:
                conn.close()
            self.assertEqual(value, "13")

            operator_cookie = self.user_cookie("duty")
            captured, _html = self.request("/hlr/config/daily-limit", method="POST", body=urlencode({"daily_limit_override": "15"}), cookie=operator_cookie)
            self.assertEqual(captured["status"], "403 Forbidden")
            conn = server.connect(server.DB_PATH)
            try:
                value = conn.execute("SELECT value FROM app_settings WHERE key = ?", (server.HLR_DAILY_LIMIT_OVERRIDE_KEY,)).fetchone()["value"]
            finally:
                conn.close()
            self.assertEqual(value, "13")

            captured, html = self.request("/hlr/config/daily-limit/reset", method="POST")
            self.assertEqual(captured["status"], "200 OK")
            self.assertIn("Дневной лимит HLR сброшен к значению из env.", html)
            self.assertIn("<dt>daily_limit_source</dt><dd>env</dd>", html)

    def test_hlr_daily_limit_endpoint_rejects_invalid_value(self):
        captured, html = self.request("/hlr/config/daily-limit", method="POST", body=urlencode({"daily_limit_override": "100001"}))
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn(server.HLR_DAILY_LIMIT_ERROR, html)
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (server.HLR_DAILY_LIMIT_OVERRIDE_KEY,)).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row)



    def test_provider_change_edit_form_contains_updated_at_original(self):
        self.request("/login")
        conn = server.connect(server.DB_PATH)
        try:
            server.init_db(conn)
            repo = server.Repository(conn)
            event_id = repo.create_routing_event(
                event_at="2026-06-22T12:00", apply_scope="none", reason="Провайдер сменил маршрут",
                country_id=1, provider_id=1, affected_route_id=1, comment="initial", created_by=1,
            )
            updated_at = conn.execute("SELECT updated_at FROM routing_events WHERE id = ?", (event_id,)).fetchone()["updated_at"]
        finally:
            conn.close()
        _captured, html = self.request(f"/provider-changes/{event_id}/edit")
        self.assertIn("name='updated_at_original'", html)
        self.assertIn(f"value='{updated_at}'", html)

    def test_provider_change_update_conflict_shows_error(self):
        self.request("/login")
        conn = server.connect(server.DB_PATH)
        try:
            server.init_db(conn)
            repo = server.Repository(conn)
            event_id = repo.create_routing_event(
                event_at="2026-06-22T12:00", apply_scope="none", reason="Провайдер сменил маршрут",
                country_id=1, provider_id=1, affected_route_id=1, comment="initial", created_by=1,
            )
            stale = conn.execute("SELECT updated_at FROM routing_events WHERE id = ?", (event_id,)).fetchone()["updated_at"]
            repo.update_routing_event(event_id, comment="other user", updated_at_original=stale, updated_by=1)
        finally:
            conn.close()
        body = urlencode({"comment": "stale save", "updated_at_original": stale})
        captured, html = self.request(f"/provider-changes/{event_id}/update", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Запись была изменена другим пользователем. Обновите страницу и повторите действие.", html)
        conn = server.connect(server.DB_PATH)
        try:
            self.assertEqual(conn.execute("SELECT comment FROM routing_events WHERE id = ?", (event_id,)).fetchone()["comment"], "other user")
        finally:
            conn.close()

    def test_provider_change_update_with_current_updated_at_saves(self):
        self.request("/login")
        conn = server.connect(server.DB_PATH)
        try:
            server.init_db(conn)
            repo = server.Repository(conn)
            event_id = repo.create_routing_event(
                event_at="2026-06-22T12:00", apply_scope="none", reason="Провайдер сменил маршрут",
                country_id=1, provider_id=1, affected_route_id=1, comment="initial", created_by=1,
            )
            current = conn.execute("SELECT updated_at FROM routing_events WHERE id = ?", (event_id,)).fetchone()["updated_at"]
        finally:
            conn.close()
        body = urlencode({"comment": "saved", "updated_at_original": current})
        captured, _html = self.request(f"/provider-changes/{event_id}/update", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            self.assertEqual(conn.execute("SELECT comment FROM routing_events WHERE id = ?", (event_id,)).fetchone()["comment"], "saved")
        finally:
            conn.close()

    def test_phone_status_options_expose_only_simplified_statuses(self):
        html = server.phone_status_options(empty="Все")
        for label in ("Используется", "Не используется", "Свободен", "Проблемный", "Не известно"):
            self.assertIn(label, html)
        for old_value in ("reserved", "blocked", "disabled"):
            self.assertNotIn(f"value='{old_value}'", html)

    def test_head_root_initializes_database_without_crashing(self):
        captured, _content = self.request("/", method="HEAD", auto_login=False)
        self.assertIn(captured["status"], {"200 OK", "303 See Other"})
        conn = server.connect(server.DB_PATH)
        try:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM users LIMIT 1").fetchone())
        finally:
            conn.close()

    def test_tariffs_page_omits_priority_and_export_excludes_priority(self):
        captured, content = self.request("/tariffs")
        self.assertEqual(captured["status"], "200 OK")
        table_fragment = content.split('<table', 1)[1].split('</table>', 1)[0]
        self.assertNotIn("Приоритет", table_fragment)
        self.assertIn("Цена провайдера", table_fragment)
        self.assertIn("Инфо", table_fragment)

        captured, csv_content = self.request("/tariffs?export=csv")
        self.assertEqual(captured["status"], "200 OK")
        header = csv_content.splitlines()[0]
        self.assertNotIn("Priority", header)
        self.assertNotIn("Приоритет", header)

    def test_tariff_edit_form_locks_identity_and_warns_on_currency_change(self):
        self.request("/tariffs")
        conn = server.connect(server.DB_PATH)
        try:
            repo = server.Repository(conn)
            admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
            country_id = conn.execute("SELECT id FROM countries LIMIT 1").fetchone()["id"]
            provider_id = conn.execute("SELECT id FROM providers LIMIT 1").fetchone()["id"]
            currency_id = conn.execute("SELECT id FROM currencies WHERE code = 'EUR' LIMIT 1").fetchone()["id"]
            existing = repo.find_tariff_by_identity(country_id, provider_id, None)
            tariff_id = existing["id"] if existing else repo.create_tariff(country_id=country_id, provider_id=provider_id, provider_currency_id=currency_id, price_in_provider_currency="2.5", conversion_rate_to_eur="1", conversion_rate_date="2026-06-22", created_by=admin_id)
        finally:
            conn.close()
        captured, content = self.request(f"/tariffs/{tariff_id}/edit")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("name='price'", content)
        self.assertIn("name='currency_id'", content)
        self.assertIn("name='comment'", content)
        self.assertIn("Активен", content)
        self.assertIn("name='is_current'", content)
        self.assertNotIn("name='country_id'", content)
        self.assertNotIn("name='provider_id'", content)
        self.assertNotIn("name='provider_prefix_id'", content)
        self.assertIn("Вы меняете валюту тарифа", content)


    def test_tariffs_table_has_no_one_click_activation_actions(self):
        captured, content = self.request("/tariffs?status=all")
        self.assertEqual(captured["status"], "200 OK")
        table_fragment = content.split('<table', 1)[1].split('</table>', 1)[0]
        self.assertIn("Редактировать", table_fragment)
        self.assertIn("Инфо", content)
        self.assertNotIn("Деактивировать", table_fragment)
        self.assertNotIn("Активировать", table_fragment)
        self.assertNotIn("/deactivate", table_fragment)
        self.assertNotIn("/activate", table_fragment)

    def test_tariff_active_status_changes_only_through_edit_form(self):
        self.request("/tariffs")
        conn = server.connect(server.DB_PATH)
        try:
            repo = server.Repository(conn)
            admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
            country_id = conn.execute("SELECT id FROM countries LIMIT 1").fetchone()["id"]
            provider_id = conn.execute("SELECT id FROM providers LIMIT 1").fetchone()["id"]
            currency_id = conn.execute("SELECT id FROM currencies WHERE code = 'EUR' LIMIT 1").fetchone()["id"]
            existing = repo.find_tariff_by_identity(country_id, provider_id, None)
            tariff_id = existing["id"] if existing else repo.create_tariff(country_id=country_id, provider_id=provider_id, provider_currency_id=currency_id, price_in_provider_currency="2.5", conversion_rate_to_eur="1", conversion_rate_date="2026-06-22", created_by=admin_id)
            original_price = conn.execute("SELECT price_in_provider_currency FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()["price_in_provider_currency"]
        finally:
            conn.close()

        body = urlencode({"price": str(original_price), "currency_id": str(currency_id), "comment": "deactivated through edit", "is_current": "0"})
        captured, _content = self.request(f"/tariffs/{tariff_id}/update", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT is_current FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()
            self.assertEqual(row["is_current"], 0)
            history = conn.execute("SELECT reason, comment FROM tariff_change_history WHERE tariff_id = ? ORDER BY id DESC LIMIT 1", (tariff_id,)).fetchone()
            self.assertIn("Активность: Да → Нет", history["comment"])
        finally:
            conn.close()

        captured, inactive_content = self.request("/tariffs?status=inactive")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(f"/tariffs/{tariff_id}/edit", inactive_content)

        body = urlencode({"price": str(original_price), "currency_id": str(currency_id), "comment": "activated through edit", "is_current": "1"})
        captured, _content = self.request(f"/tariffs/{tariff_id}/update", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT is_current FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()
            self.assertEqual(row["is_current"], 1)
            history = conn.execute("SELECT reason, comment FROM tariff_change_history WHERE tariff_id = ? ORDER BY id DESC LIMIT 1", (tariff_id,)).fetchone()
            self.assertIn("Активность: Нет → Да", history["comment"])
        finally:
            conn.close()

    def test_tariff_comment_save_without_status_change_has_no_activation_history(self):
        self.request("/tariffs")
        conn = server.connect(server.DB_PATH)
        try:
            tariff = conn.execute("SELECT id, provider_currency_id, price_in_provider_currency, is_current FROM tariffs LIMIT 1").fetchone()
            tariff_id = tariff["id"]
            body = urlencode({"price": str(tariff["price_in_provider_currency"]), "currency_id": str(tariff["provider_currency_id"]), "comment": "comment only", "is_current": str(tariff["is_current"])})
        finally:
            conn.close()
        captured, _content = self.request(f"/tariffs/{tariff_id}/update", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            history = conn.execute("SELECT reason, comment FROM tariff_change_history WHERE tariff_id = ? ORDER BY id DESC LIMIT 1", (tariff_id,)).fetchone()
            self.assertEqual(history["reason"], "tariff.changed")
            self.assertNotIn("Тариф активирован", history["comment"] or "")
            self.assertNotIn("Тариф деактивирован", history["comment"] or "")
            self.assertNotIn("Активность:", history["comment"] or "")
        finally:
            conn.close()

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
        self.assertIn("Логин", content)

    def test_admin_inline_edit_uses_single_modal_with_cancel_outside_and_escape_close(self):
        for path, form_action in [
            ("/admin/change-reasons", "/admin/change-reasons/"),
            ("/admin/users", "/admin/users/"),
            ("/admin/dictionaries?section=countries", "/admin/dictionaries/countries/"),
            ("/admin/dictionaries?section=providers", "/admin/dictionaries/providers/"),
            ("/admin/dictionaries?section=currencies", "/admin/dictionaries/currencies/"),
            ("/admin/dictionaries?section=prefixes", "/admin/dictionaries/prefixes/"),
            ("/admin/dictionaries?section=servers", "/admin/dictionaries/servers/"),
            ("/admin/dictionaries?section=phone-types", "/admin/dictionaries/phone-types/"),
            ("/admin/dictionaries?section=projects", "/admin/dictionaries/projects/"),
            ("/admin/dictionaries?section=phone-assignments", "/admin/dictionaries/phone-assignments/"),
        ]:
            with self.subTest(path=path):
                captured, content = self.request(path)
                self.assertEqual(captured["status"], "200 OK")
                self.assertIn("details.edit-details[open] > form", content)
                self.assertIn('const adminEditDetails = Array.from', content)
                self.assertIn('closeAdminEdit(details)', content)
                self.assertIn('event.key !== "Escape"', content)
                self.assertIn('cancel.textContent = "Отмена"', content)
                self.assertIn(form_action, content)

    def test_login_page_returns_user_selection_and_active_users(self):
        captured, content = self.request("/login")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Логин", content)
        self.assertIn("Пароль", content)
        self.assertIn("Войти", content)
        self.assertNotIn("Выберите пользователя", content)

    def test_current_user_card_appears_in_layout(self):
        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn('class="current-user-selector"', content)
        self.assertIn('href="/logout"', content)
        self.assertIn("Текущий пользователь", content)
        self.assertIn("Admin · Админ", content)
        summary = content.split('<summary aria-label="Меню пользователя">', 1)[1].split("</summary>", 1)[0]
        self.assertNotIn("Текущий пользователь", summary)

    def test_app_pages_are_not_cached_and_include_connection_recovery_ui(self):
        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        headers = dict(captured["headers"])
        self.assertEqual(headers.get("Cache-Control"), "no-store, max-age=0")
        self.assertEqual(headers.get("Pragma"), "no-cache")
        self.assertEqual(headers.get("Expires"), "0")
        self.assertIn('data-connection-status', content)
        self.assertIn("Нет соединения с интернетом. Проверьте подключение.", content)
        self.assertIn("Соединение восстановлено. Обновите страницу.", content)
        self.assertIn("window.location.reload()", content)

    def test_form_submit_recovery_script_reenables_failed_submits(self):
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn('data-submit-disabled-by-recovery', content)
        self.assertIn("Не удалось отправить данные. Проверьте подключение и попробуйте ещё раз.", content)
        self.assertIn('window.addEventListener("pageshow"', content)

    def test_login_with_password_persists_cookie_and_redirects(self):
        body = urlencode({"username": "duty", "password": "duty123", "redirect_to": "/phones"})
        captured, content = self.request("/login", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/phones"), captured["headers"])
        set_cookie = dict(captured["headers"]).get("Set-Cookie", "")
        self.assertIn(f"{server.CURRENT_USER_COOKIE}=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)

        captured, content = self.request("/routes", cookie=set_cookie)
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Дежурный · Дежурный", content)

    def test_wrong_password_does_not_authenticate(self):
        body = urlencode({"username": "duty", "password": "wrong"})
        captured, content = self.request("/login", method="POST", body=body)
        self.assertEqual(captured["status"], "401 Unauthorized")
        self.assertIn("Неверный логин или пароль", content)

    def test_admin_default_fallback_login_opens_user_management_without_exposing_password(self):
        body = urlencode({"username": "admin", "password": "admin"})
        captured, _ = self.request("/login", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        set_cookie = dict(captured["headers"]).get("Set-Cookie", "")
        captured, content = self.request("/admin/users", cookie=set_cookie)
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Пользователи", content)
        self.assertNotIn("admin/admin", content)

    def test_admin_can_create_user_with_password_and_no_password_leak(self):
        body = urlencode({
            "username": "operator2",
            "display_name": "Оператор",
            "role_key": "operator",
            "password": "test123",
            "password_confirm": "test123",
        })
        captured, _ = self.request("/admin/users/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT password_hash, password_salt FROM users WHERE username = 'operator2'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertNotEqual(row["password_hash"], "test123")
        captured, content = self.request("/admin/users")
        self.assertNotIn("test123", content)
        body = urlencode({"username": "operator2", "password": "test123"})
        captured, _ = self.request("/login", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")


    def test_admin_can_create_user_with_permissions(self):
        body = urlencode({
            "username": "permuser",
            "display_name": "Perm User",
            "role_key": "operator",
            "password": "test123",
            "password_confirm": "test123",
            "perm__routes__read": "1",
            "perm__routes__export": "1",
            "perm__tariffs__read": "1",
            "perm__tariffs__write": "1",
        })

        captured, _ = self.request("/admin/users/create", method="POST", body=body)

        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            user_id = conn.execute("SELECT id FROM users WHERE username = 'permuser'").fetchone()["id"]
            routes = conn.execute("SELECT can_read, can_write, can_export FROM user_permissions WHERE user_id = ? AND section_key = 'routes'", (user_id,)).fetchone()
            tariffs = conn.execute("SELECT can_read, can_write, can_export FROM user_permissions WHERE user_id = ? AND section_key = 'tariffs'", (user_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(dict(routes), {"can_read": 1, "can_write": 0, "can_export": 1})
        self.assertEqual(dict(tariffs), {"can_read": 1, "can_write": 1, "can_export": 0})

        captured, content = self.request("/admin/users")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Perm User", content)
        self.assertIn("name='perm__routes__read' value='1' checked", content)
        self.assertIn("name='perm__routes__export' value='1' checked", content)
        self.assertIn("name='perm__tariffs__write' value='1' checked", content)

    def test_admin_can_edit_user_permissions(self):
        conn = server.connect(server.DB_PATH)
        try:
            server.init_db(conn)
            user_id = server.Repository(conn).create_user("editperm", "operator", "Edit Permissions", password="old123")
        finally:
            conn.close()

        body = urlencode({
            "username": "editperm",
            "display_name": "Edit Permissions",
            "role_key": "operator",
            "is_active": "1",
            "perm__routes__read": "1",
            "perm__routes__write": "1",
        })
        captured, _ = self.request(f"/admin/users/{user_id}/update", method="POST", body=body)

        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            rows = conn.execute("SELECT can_read, can_write, can_export FROM user_permissions WHERE user_id = ? AND section_key = 'routes'", (user_id,)).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(dict(rows[0]), {"can_read": 1, "can_write": 1, "can_export": 0})

        captured, content = self.request("/admin/users")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("name='perm__routes__read' value='1' checked", content)
        self.assertIn("name='perm__routes__write' value='1' checked", content)

    def test_duplicate_username_update_returns_user_friendly_error(self):
        conn = server.connect(server.DB_PATH)
        try:
            server.init_db(conn)
            repo = server.Repository(conn)
            first_id = repo.create_user("firstdup", "operator", "First Duplicate", password="old123")
            repo.create_user("seconddup", "operator", "Second Duplicate", password="old123")
        finally:
            conn.close()

        body = urlencode({
            "username": "seconddup",
            "display_name": "First Duplicate",
            "role_key": "operator",
            "is_active": "1",
        })
        captured, content = self.request(f"/admin/users/{first_id}/update", method="POST", body=body)

        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Нарушено ограничение уникальности или обязательности данных", content)
        self.assertNotIn("UNIQUE constraint failed", content)

    def test_password_confirmation_mismatch_blocks_user_creation(self):
        body = urlencode({
            "username": "operator3",
            "display_name": "Оператор",
            "role_key": "operator",
            "password": "test123",
            "password_confirm": "test124",
        })
        captured, content = self.request("/admin/users/create", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Пароли не совпадают", content)

    def test_admin_can_reset_user_password(self):
        conn = server.connect(server.DB_PATH)
        try:
            server.init_db(conn)
            user_id = server.Repository(conn).create_user("operator4", "operator", "Оператор", password="old123")
        finally:
            conn.close()
        admin_cookie = self.user_cookie("admin")
        body = urlencode({
            "display_name": "Оператор",
            "role_key": "operator",
            "is_active": "1",
            "password": "new123",
            "password_confirm": "new123",
        })
        captured, _ = self.request(f"/admin/users/{user_id}/update", method="POST", body=body, cookie=admin_cookie, auto_login=False)
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/admin/users?notice=%D0%9F%D0%B0%D1%80%D0%BE%D0%BB%D1%8C%20%D0%BF%D0%BE%D0%BB%D1%8C%D0%B7%D0%BE%D0%B2%D0%B0%D1%82%D0%B5%D0%BB%D1%8F%20%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%B1%D1%80%D0%BE%D1%88%D0%B5%D0%BD.%20%D0%9F%D1%80%D0%B8%20%D1%81%D0%BB%D0%B5%D0%B4%D1%83%D1%8E%D1%89%D0%B5%D0%BC%20%D0%B2%D1%85%D0%BE%D0%B4%D0%B5%20%D0%BF%D0%BE%D0%BB%D1%8C%D0%B7%D0%BE%D0%B2%D0%B0%D1%82%D0%B5%D0%BB%D1%8C%20%D0%B4%D0%BE%D0%BB%D0%B6%D0%B5%D0%BD%20%D1%81%D0%BC%D0%B5%D0%BD%D0%B8%D1%82%D1%8C%20%D0%BF%D0%B0%D1%80%D0%BE%D0%BB%D1%8C."), captured["headers"])
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT must_change_password FROM users WHERE id = ?", (user_id,)).fetchone()
            admin_row = conn.execute("SELECT must_change_password FROM users WHERE username = 'admin'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["must_change_password"], 1)
        self.assertEqual(admin_row["must_change_password"], 0)
        captured, content = self.request("/admin/users", cookie=admin_cookie, auto_login=False)
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Admin · Админ", content)
        captured, _ = self.request("/routes", cookie=admin_cookie, auto_login=False)
        self.assertEqual(captured["status"], "200 OK")
        captured, _ = self.request("/login", method="POST", body=urlencode({"username": "operator4", "password": "new123"}))
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/change-password"), captured["headers"])

    def test_admin_self_password_reset_logs_out_current_user_only(self):
        admin_cookie = self.user_cookie("admin")
        conn = server.connect(server.DB_PATH)
        try:
            admin = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
        finally:
            conn.close()
        body = urlencode({
            "username": "admin",
            "display_name": "Админ",
            "role_key": "admin",
            "is_active": "1",
            "password": "selfnew123",
            "password_confirm": "selfnew123",
        })
        captured, _ = self.request(f"/admin/users/{admin['id']}/update", method="POST", body=body, cookie=admin_cookie, auto_login=False)
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn("/login?notice=", dict(captured["headers"])["Location"])
        self.assertIn("Max-Age=0", dict(captured["headers"]).get("Set-Cookie", ""))
        captured, _ = self.request("/routes", cookie=admin_cookie, auto_login=False)
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/change-password"), captured["headers"])
        captured, _ = self.request("/login", method="POST", body=urlencode({"username": "admin", "password": "selfnew123"}), auto_login=False)
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/change-password"), captured["headers"])


    def test_sidebar_displays_selected_user_and_selector_is_single_source(self):
        cookie = self.user_cookie("duty")
        captured, content = self.request("/routes", cookie=cookie)
        self.assertEqual(captured["status"], "200 OK")
        selector = content.split('<details class="current-user-selector"', 1)[1].split("</details>", 1)[0]
        summary = selector.split('<summary aria-label="Меню пользователя">', 1)[1].split("</summary>", 1)[0]
        self.assertIn("Дежурный · Дежурный", summary)
        self.assertNotIn("Текущий пользователь", summary)
        self.assertIn("Текущий пользователь", selector)
        self.assertEqual(content.count('href="/logout"'), 1)


    def test_must_change_password_redirects_until_changed(self):
        conn = server.connect(server.DB_PATH)
        try:
            server.init_db(conn)
            repo = server.Repository(conn)
            user_id = repo.create_user("tempuser", "operator", "Temp", password="temp123", must_change_password=True)
        finally:
            conn.close()
        captured, _ = self.request("/login", method="POST", body=urlencode({"username": "tempuser", "password": "temp123"}))
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/change-password"), captured["headers"])
        set_cookie = dict(captured["headers"]).get("Set-Cookie", "")
        captured, _ = self.request("/routes", cookie=set_cookie, auto_login=False)
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/change-password"), captured["headers"])
        body = urlencode({"password": "newtemp123", "password_confirm": "newtemp123"})
        captured, _ = self.request("/change-password", method="POST", body=body, cookie=set_cookie, auto_login=False)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT must_change_password FROM users WHERE id = ?", (user_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["must_change_password"], 0)
        captured, _ = self.request("/login", method="POST", body=urlencode({"username": "tempuser", "password": "newtemp123"}))
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/routes"), captured["headers"])

    def test_inactive_user_cannot_login(self):
        conn = server.connect(server.DB_PATH)
        try:
            server.init_db(conn)
            repo = server.Repository(conn)
            user_id = repo.create_user("inactive", "operator", "Inactive", password="inactive123")
            repo.update_user(user_id, display_name="Inactive", role_key="operator", is_active=False)
        finally:
            conn.close()
        captured, content = self.request("/login", method="POST", body=urlencode({"username": "inactive", "password": "inactive123"}))
        self.assertEqual(captured["status"], "401 Unauthorized")
        self.assertIn("Неверный логин или пароль", content)

    def test_logout_clears_selected_user_and_routes_require_login(self):
        cookie = self.user_cookie("admin")
        captured, _ = self.request("/logout", cookie=cookie)
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/login"), captured["headers"])
        self.assertIn("Max-Age=0", dict(captured["headers"]).get("Set-Cookie", ""))

        captured, _ = self.request("/routes", auto_login=False)
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/login"), captured["headers"])

    def test_inactive_or_missing_selected_user_redirects_to_login(self):
        cookie = self.user_cookie("guest")
        conn = server.connect(server.DB_PATH)
        try:
            conn.execute("UPDATE users SET is_active = 0 WHERE username = 'guest'")
            conn.commit()
        finally:
            conn.close()
        captured, _ = self.request("/routes", cookie=cookie, auto_login=False)
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/login"), captured["headers"])
        self.assertIn("Max-Age=0", dict(captured["headers"]).get("Set-Cookie", ""))

    def test_theme_toggle_is_clickable_and_persistent_scripted_control(self):
        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn('data-theme-selector', content)
        self.assertIn('data-theme-current>Тема: Светлая 2.0 ▾', content)
        self.assertIn('data-theme-option="dark"', content)
        self.assertIn('data-theme-option="light-v2"', content)
        self.assertIn('data-theme-option="tele-route-pro"', content)
        self.assertIn('const themeAliases = { "mvp": "light-v2", "calm-blue": "light-v2", "cyber-sketch": "dark", "terminal-paper": "light-v2" };', content)
        self.assertIn('let savedTheme = normalizeTheme(localStorage.getItem("mvp-theme") || "light-v2")', content)
        self.assertIn('localStorage.setItem("mvp-theme", theme)', content)
        self.assertIn('Тёмная', content)
        self.assertIn('Светлая 2.0', content)
        self.assertIn('TeleRoute Pro', content)
        self.assertIn('\"tele-route-pro\": \"TeleRoute Pro\"', content)

    def test_selected_user_records_route_and_phone_history_actor(self):
        self.request("/routes")
        admin_cookie = self.user_cookie("admin")
        body = urlencode({"number": "525550077001", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "used", "is_active": "1"})
        self.request("/phones/create", method="POST", body=body, cookie=admin_cookie)
        add_body = urlencode({"phone_number": "525550077001", "usage_type": "pool_member", "comment": "admin actor"})
        self.request("/routes/1/numbers/add", method="POST", body=add_body, cookie=admin_cookie)
        conn = server.connect(server.DB_PATH)
        try:
            admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
            phone_id = conn.execute("SELECT id FROM phone_numbers WHERE number = '525550077001'").fetchone()["id"]
            link = conn.execute("SELECT id, added_by FROM route_phone_numbers WHERE route_id = 1 AND phone_number_id = ?", (phone_id,)).fetchone()
            self.assertEqual(link["added_by"], admin_id)
            route_hist = conn.execute("SELECT changed_by FROM route_phone_number_history WHERE route_id = 1 AND phone_number_id = ? AND action = 'added'", (phone_id,)).fetchone()
            phone_hist = conn.execute("SELECT changed_by FROM route_phone_number_history WHERE phone_number_id = ? AND action = 'added'", (phone_id,)).fetchone()
            self.assertEqual(route_hist["changed_by"], admin_id)
            self.assertEqual(phone_hist["changed_by"], admin_id)
        finally:
            conn.close()

    def test_switching_to_roman_records_roman_and_remove_actor(self):
        self.request("/routes")
        roman_cookie = self.user_cookie("roman")
        body = urlencode({"number": "525550077002", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "used", "is_active": "1"})
        self.request("/phones/create", method="POST", body=body, cookie=roman_cookie)
        self.request("/routes/1/numbers/add", method="POST", body=urlencode({"phone_number": "525550077002", "usage_type": "pool_member"}), cookie=roman_cookie)
        conn = server.connect(server.DB_PATH)
        try:
            roman_id = conn.execute("SELECT id FROM users WHERE username = 'roman'").fetchone()["id"]
            phone_id = conn.execute("SELECT id FROM phone_numbers WHERE number = '525550077002'").fetchone()["id"]
            link_id = conn.execute("SELECT id FROM route_phone_numbers WHERE route_id = 1 AND phone_number_id = ?", (phone_id,)).fetchone()["id"]
        finally:
            conn.close()
        self.request("/routes/1/numbers/remove", method="POST", body=urlencode({"link_ids": str(link_id), "reason": "roman remove"}), cookie=roman_cookie)
        conn = server.connect(server.DB_PATH)
        try:
            added = conn.execute("SELECT changed_by FROM route_phone_number_history WHERE route_id = 1 AND phone_number_id = ? AND action = 'added'", (phone_id,)).fetchone()
            removed_link = conn.execute("SELECT removed_by FROM route_phone_numbers WHERE id = ?", (link_id,)).fetchone()
            removed = conn.execute("SELECT changed_by FROM route_phone_number_history WHERE route_id = 1 AND phone_number_id = ? AND action = 'removed'", (phone_id,)).fetchone()
            self.assertEqual(added["changed_by"], roman_id)
            self.assertEqual(removed_link["removed_by"], roman_id)
            self.assertEqual(removed["changed_by"], roman_id)
        finally:
            conn.close()

    def test_provider_deactivation_closes_route_links_with_selected_actor(self):
        self.request("/routes")
        roman_cookie = self.user_cookie("roman")
        body = urlencode({"number": "525550077003", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "used", "is_active": "1"})
        self.request("/phones/create", method="POST", body=body, cookie=roman_cookie)
        self.request("/routes/1/numbers/add", method="POST", body=urlencode({"phone_number": "525550077003", "usage_type": "pool_member"}), cookie=roman_cookie)
        conn = server.connect(server.DB_PATH)
        try:
            roman_id = conn.execute("SELECT id FROM users WHERE username = 'roman'").fetchone()["id"]
            phone_id = conn.execute("SELECT id FROM phone_numbers WHERE number = '525550077003'").fetchone()["id"]
            link_id = conn.execute("SELECT id FROM route_phone_numbers WHERE route_id = 1 AND phone_number_id = ?", (phone_id,)).fetchone()["id"]
        finally:
            conn.close()
        update = urlencode({"number": "525550077003", "country_id": "1", "provider_id": "1", "project_label": "", "assignment_type": "gl", "status": "used", "is_active": "0", "connection_cost": "", "monthly_fee": "", "currency_id": "", "phone_type": "", "tariff_label": "", "comment": "deactivate"})
        self.request(f"/phones/{phone_id}/update", method="POST", body=update, cookie=roman_cookie)
        conn = server.connect(server.DB_PATH)
        try:
            link = conn.execute("SELECT removed_by FROM route_phone_numbers WHERE id = ?", (link_id,)).fetchone()
            self.assertEqual(link["removed_by"], roman_id)
        finally:
            conn.close()

        captured, content = self.request(f"/phones/{phone_id}/history", cookie=roman_cookie)
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Номер исключён из маршрута", content)
        self.assertIn("Номер автоматически исключён из маршрута из-за неактивности у провайдера", content)
        self.assertIn("Причина: номер стал неактивен у провайдера", content)
        self.assertNotIn("Связь с маршрутом закрыта из-за деактивации номера у провайдера", content)

    def test_inactive_users_are_not_selectable(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            guest_id = conn.execute("SELECT id FROM users WHERE username = 'guest'").fetchone()["id"]
            conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (guest_id,))
            conn.commit()
        finally:
            conn.close()
        captured, content = self.request("/login")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn(f"value='{guest_id}'", content)


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

    def test_routes_search_is_unicode_case_insensitive_with_filters_and_counts(self):
        captured, content = self.request("/routes?search=мекс")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Мексика/Sancom/Demo_0828@", content)
        self.assertIn("Всего записей", content)

        captured, upper_content = self.request("/routes?search=МЕКС")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Мексика/Sancom/Demo_0828@", upper_content)

        captured, filtered = self.request("/routes?provider_id=1&search=мекс")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Мексика/Sancom/Demo_0828@", filtered)
        self.assertNotIn("Мексика/Miatel/Demo_A@", filtered)

        captured, blank = self.request("/routes?search=+++")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Мексика/Sancom/Demo_0828@", blank)

    def test_routes_search_pagination_count_uses_case_insensitive_filter(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            country_id = conn.execute("SELECT id FROM countries WHERE code = 'MEX'").fetchone()["id"]
            provider_id = conn.execute("SELECT id FROM providers WHERE name = 'DemoTel'").fetchone()["id"]
            admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
            for index in range(55):
                conn.execute(
                    """
                    INSERT INTO routes(country_id, provider_id, name, cli_source_type, cli_source_label, is_actual, priority_status, comment, created_by)
                    VALUES (?, ?, ?, 'rnd', ?, 1, 'normal', ?, ?)
                    """,
                    (country_id, provider_id, f"CASEDEMO {index:03d}", f"CASEDEMO{index:03d}", "bulk export row", admin_id),
                )
            conn.commit()
        finally:
            conn.close()
        captured, content = self.request("/routes?search=casedemo&limit=50")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Всего записей: 55", content)
        self.assertEqual(content.count("bulk export row"), 50)
        self.assertIn("page=2", content)
        self.assertIn("search=casedemo", content)

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

    def route_prefix_id(self, prefix):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            return conn.execute("SELECT id FROM provider_prefixes WHERE prefix = ?", (prefix,)).fetchone()["id"]
        finally:
            conn.close()

    def test_routes_prefix_filter_by_id_returns_only_matching_prefix_routes(self):
        prefix_id = self.route_prefix_id("0828")
        captured, content = self.request(f"/routes?prefix_id={prefix_id}")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Мексика/Sancom/Demo_0828@", content)
        self.assertNotIn("Мексика/Sancom/Demo_0827@", content)
        self.assertNotIn("Мексика/Miatel/Demo_A@", content)

    def test_routes_prefix_all_does_not_filter_routes(self):
        captured, content = self.request("/routes?prefix_id=")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Мексика/Sancom/Demo_0828@", content)
        self.assertIn("Мексика/Sancom/Demo_0827@", content)
        self.assertIn("Мексика/Miatel/Demo_A@", content)

    def test_saved_route_prefix_filter_restores_and_reset_clears_it(self):
        prefix_id = self.route_prefix_id("0828")
        captured, content = self.request(f"/routes?prefix_id={prefix_id}")
        self.assertEqual(captured["status"], "200 OK")
        routes_cookie = dict(captured["headers"]).get("Set-Cookie", "")
        self.assertIn("mvp_filter_state=", routes_cookie)
        self.assertIn(str(prefix_id), routes_cookie)

        captured, _ = self.request("/routes", cookie=f"{self.user_cookie('admin')}; {routes_cookie}")
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", f"/routes?prefix_id={prefix_id}&_filters_restored=1"), captured["headers"])

        captured, content = self.request(f"/routes?prefix_id={prefix_id}&_filters_restored=1", cookie=f"{self.user_cookie('admin')}; {routes_cookie}")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("<details class='filter-card' open>", content)
        self.assertIn(f"<option value='{prefix_id}' selected>0828</option>", content)
        self.assertIn("Мексика/Sancom/Demo_0828@", content)
        self.assertNotIn("Мексика/Sancom/Demo_0827@", content)

        captured, content = self.request("/routes?reset_filters=1", cookie=f"{self.user_cookie('admin')}; {routes_cookie}")
        self.assertEqual(captured["status"], "200 OK")
        reset_cookie = dict(captured["headers"]).get("Set-Cookie", "")
        self.assertNotIn(str(prefix_id), reset_cookie)
        self.assertIn("Мексика/Sancom/Demo_0828@", content)
        self.assertIn("Мексика/Sancom/Demo_0827@", content)


    def test_no_prefix_dictionary_rows_are_hidden_and_routes_use_null(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            demotel_id = conn.execute("SELECT id FROM providers WHERE name = 'DemoTel'").fetchone()["id"]
            legacy_id = conn.execute(
                "INSERT INTO provider_prefixes(provider_id, prefix, name, is_active) VALUES (?, ?, ?, 1)",
                (demotel_id, "Без префикса", "legacy no prefix"),
            ).lastrowid
            route_id = conn.execute(
                "INSERT INTO routes(country_id, provider_id, provider_prefix_id, name, cli_source_type, cli_source_label, is_actual, created_by) VALUES (1, ?, ?, 'Legacy no prefix route', 'pool', 'Legacy', 1, 1)",
                (demotel_id, legacy_id),
            ).lastrowid
            conn.commit()
            server.init_db(conn)
            route = conn.execute("SELECT provider_prefix_id FROM routes WHERE id = ?", (route_id,)).fetchone()
            legacy = conn.execute("SELECT is_active FROM provider_prefixes WHERE id = ?", (legacy_id,)).fetchone()
            self.assertIsNone(route["provider_prefix_id"])
            self.assertEqual(legacy["is_active"], 0)
        finally:
            conn.close()

        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn(f"<option value='{legacy_id}'>Без префикса</option>", content)

    def test_route_create_and_edit_save_no_prefix_as_null_and_table_shows_dash(self):
        self.request("/routes")
        body = urlencode({"country_id": "1", "provider_id": "2", "provider_prefix_id": "", "project_label": "", "cli_source_type": "pool", "cli_source_label": "NullPrefix", "is_actual": "1"})
        captured, _ = self.request("/routes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            route = conn.execute("SELECT id, provider_prefix_id, name FROM routes WHERE cli_source_label = 'NullPrefix'").fetchone()
            self.assertIsNone(route["provider_prefix_id"])
            route_id = route["id"]
        finally:
            conn.close()

        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("<td data-col='prefix'>—</td>", content)

        captured, _ = self.request(f"/routes/{route_id}/update", method="POST", body=urlencode({"name": route["name"], "provider_id": "2", "provider_prefix_id": "", "comment": "", "is_actual": "1", "priority_status": "unknown"}))
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            self.assertIsNone(conn.execute("SELECT provider_prefix_id FROM routes WHERE id = ?", (route_id,)).fetchone()["provider_prefix_id"])
        finally:
            conn.close()

    def test_routes_no_prefix_filter_is_single_and_provider_scoped(self):
        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(content.count("value='__none__'"), 1)

        captured, content = self.request("/routes?prefix_id=__none__")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Мексика/Miatel/Demo_A@", content)
        self.assertIn("Мексика/DemoTel/Demo_A@", content)
        self.assertNotIn("Мексика/Sancom/Demo_0827@", content)

        captured, content = self.request("/routes?provider_id=2&prefix_id=__none__")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Мексика/Miatel/Demo_A@", content)
        self.assertNotIn("Мексика/DemoTel/Demo_A@", content)

        captured, content = self.request("/routes?provider_id=3&prefix_id=__none__")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Мексика/DemoTel/Demo_A@", content)
        self.assertNotIn("Мексика/Miatel/Demo_A@", content)

    def test_saved_no_prefix_filter_restores_and_reset_clears_it(self):
        captured, _ = self.request("/routes?prefix_id=__none__")
        self.assertEqual(captured["status"], "200 OK")
        routes_cookie = dict(captured["headers"]).get("Set-Cookie", "")
        self.assertIn("__none__", routes_cookie)

        captured, _ = self.request("/routes", cookie=f"{self.user_cookie('admin')}; {routes_cookie}")
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/routes?prefix_id=__none__&_filters_restored=1"), captured["headers"])

        captured, content = self.request("/routes?reset_filters=1", cookie=f"{self.user_cookie('admin')}; {routes_cookie}")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("__none__", dict(captured["headers"]).get("Set-Cookie", ""))

    def test_prefix_dictionary_rejects_no_prefix_values(self):
        self.request("/routes")
        for value in ("", "   ", "Без префикса", "без префикса", "no prefix", "—"):
            captured, content = self.request("/admin/dictionaries/prefixes/create", method="POST", body=urlencode({"provider_id": "1", "prefix": value, "name": "bad"}))
            self.assertEqual(captured["status"], "400 Bad Request")
            self.assertIn("Префикс должен быть реальным кодом", content)

    def test_dictionary_create_requires_visible_fields_and_returns_active_section(self):
        self.request("/routes")
        cases = [
            ("countries", {"name": "   ", "code": "	"}, "/admin/dictionaries?section=countries", "Заполните название GEO"),
            ("providers", {"name": "NewTel", "default_currency_id": "", "comment": "comment"}, "/admin/dictionaries?section=providers", "Выберите валюту провайдера"),
            ("currencies", {"code": " ", "name": "US Dollar"}, "/admin/dictionaries?section=currencies", "Заполните код валюты"),
            ("prefixes", {"provider_id": "", "prefix": "0333", "name": "prefix"}, "/admin/dictionaries?section=prefixes", "Выберите провайдера префикса"),
            ("servers", {"name": "EU9", "comment": "  "}, "/admin/dictionaries?section=servers", "Заполните комментарий"),
            ("phone-types", {"name": " ", "comment": "comment"}, "/admin/dictionaries?section=phone-types", "Заполните тип номера"),
            ("projects", {"name": "Project", "comment": "\n"}, "/admin/dictionaries?section=projects", "Заполните комментарий"),
            ("phone-assignments", {"name": "Monitor", "code": " ", "comment": "comment"}, "/admin/dictionaries?section=phone-assignments", "Заполните код назначения номера"),
        ]
        for kind, body, return_path, message in cases:
            with self.subTest(kind=kind):
                captured, content = self.request(f"/admin/dictionaries/{kind}/create", method="POST", body=urlencode(body))
                self.assertEqual(captured["status"], "400 Bad Request")
                self.assertIn(message, content)
                self.assertIn(f"href='{return_path}'", content)

    def test_routes_csv_export_respects_prefix_filter(self):
        prefix_id = self.route_prefix_id("0828")
        captured, content = self.request(f"/routes?prefix_id={prefix_id}&export=csv")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(("Content-Type", "text/csv; charset=utf-8"), captured["headers"])
        self.assertIn("Мексика/Sancom/Demo_0828@", content)
        self.assertNotIn("Мексика/Sancom/Demo_0827@", content)
        self.assertNotIn("Мексика/Miatel/Demo_A@", content)

    def test_filter_state_persists_per_section_and_empty_search_stays_open_current_response(self):
        captured, content = self.request("/phones?number=525550000001&page=2")
        self.assertEqual(captured["status"], "200 OK")
        filter_cookie = dict(captured["headers"]).get("Set-Cookie", "")
        self.assertIn("mvp_filter_state=", filter_cookie)
        self.assertIn('name="number" value="525550000001"', content)
        self.assertIn("<details class='filter-card' open>", content)

        captured, _ = self.request("/phones", cookie=f"{self.user_cookie('admin')}; {filter_cookie}")
        self.assertEqual(captured["status"], "303 See Other")
        self.assertIn(("Location", "/phones?number=525550000001&_filters_restored=1"), captured["headers"])

        captured, content = self.request("/phones?number=525550000001&_filters_restored=1", cookie=f"{self.user_cookie('admin')}; {filter_cookie}")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn('name="number" value="525550000001"', content)
        self.assertIn("525550000001", content)
        self.assertNotIn("525550000002", content)
        self.assertIn("<details class='filter-card' open>", content)
        self.assertNotIn("page=2", content)

        captured, content = self.request("/routes", cookie=f"{self.user_cookie('admin')}; {filter_cookie}")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("525550000001", content)
        self.assertNotIn('value="525550000001"', content)

        captured, content = self.request("/routes?search=Demo_A")
        self.assertEqual(captured["status"], "200 OK")
        routes_cookie = dict(captured["headers"]).get("Set-Cookie", "")
        self.assertIn("routes", routes_cookie)
        self.assertIn("Demo_A", routes_cookie)
        self.assertIn("<details class='filter-card' open>", content)

        captured, content = self.request("/phones?reset_filters=1", cookie=f"{self.user_cookie('admin')}; {filter_cookie}")
        self.assertEqual(captured["status"], "200 OK")
        reset_cookie = dict(captured["headers"]).get("Set-Cookie", "")
        self.assertIn("mvp_filter_state=", reset_cookie)
        self.assertNotIn("525550000001", reset_cookie)
        self.assertIn("<details class='filter-card' open>", content)
        self.assertIn('name="number" value=""', content)
        self.assertIn("525550000001", content)
        self.assertIn("525550000002", content)

        captured, content = self.request("/routes?country_id=&provider_id=&prefix_id=&is_actual=&search=&_filters_open=1")
        self.assertEqual(captured["status"], "200 OK")
        empty_search_cookie = dict(captured["headers"]).get("Set-Cookie", "")
        self.assertIn("mvp_filter_state=", empty_search_cookie)
        self.assertNotIn("_filters_open", empty_search_cookie)
        self.assertIn("<details class='filter-card' open>", content)

        captured, content = self.request("/routes", cookie=f"{self.user_cookie('admin')}; {empty_search_cookie}")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("<details class='filter-card' open>", content)
        self.assertNotIn("_filters_restored=1", content)

    def test_csv_export_uses_active_filters_without_changing_saved_filter_state(self):
        captured, content = self.request("/phones?number=525550000001&export=csv")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(("Content-Type", "text/csv; charset=utf-8"), captured["headers"])
        self.assertNotIn("Set-Cookie", dict(captured["headers"]))
        self.assertIn("525550000001", content)
        self.assertNotIn("525550000002", content)

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
        self.assertIn("href='/routes'><span class='nav-icon'", content)
        self.assertIn("<span class='side-label'>Маршруты</span></a>", content)
        self.assertIn("href='/provider-changes'><span class='nav-icon'", content)
        self.assertIn("<span class='side-label'>Смена провайдеров</span></a>", content)
        self.assertIn("href='/admin/server-priorities'><span class='nav-icon'", content)
        self.assertIn("<span class='side-label'>Приоритет по серверам</span></a>", content)
        self.assertIn("href='/admin/company-routing-settings'><span class='nav-icon'", content)
        self.assertIn("<span class='side-label'>Схема маршрутизации кампаний</span></a>", content)
        admin_tree = content.split('id="admin-nav"', 1)[1].split("</div>", 1)[0]
        self.assertNotIn("Приоритет по серверам", admin_tree)
        self.assertNotIn("Схема маршрутизации кампаний", admin_tree)
        self.assertIn("side-link active", content)

    def test_provider_changes_sidebar_link_renders_attributes_and_icon_safely(self):
        for path in ("/dashboard", "/provider-changes"):
            with self.subTest(path=path):
                captured, content = self.request(path)
                self.assertEqual(captured["status"], "200 OK")
                sidebar_item = content.split("href='/provider-changes'", 1)[0].rsplit("<a class='side-link", 1)[1]
                sidebar_item += content.split("href='/provider-changes'", 1)[1].split("</a>", 1)[0] + "</a>"
                self.assertIn("data-tooltip='Журнал изменений'", sidebar_item)
                self.assertIn("<span class='material-symbols-rounded' aria-hidden='true'>sync_alt</span>", sidebar_item)
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


    def test_provider_change_reason_lists_and_helpers_match_scopes(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        create_form = content.split("<form method='post' action='/provider-changes/create'", 1)[1].split("</form>", 1)[0]
        for expected in ("Обновление/смена АОНов", "Провайдер сменил маршрут", "Другое"):
            self.assertIn(f">{expected}</option>", create_form)
        for obsolete in ("Массовые отбои / занято", "Плохой дозвон", "Обновление пула / АОН"):
            self.assertNotIn(f">{obsolete}</option>", create_form)
        self.assertIn("Массовый отбои/занято", content)
        self.assertIn("Обратная смена провайдера", content)
        self.assertIn("Требуется понятный комментарий", content)
        self.assertIn("например тех. проблемы", content)
        self.assertIn("id='routing-comment'", create_form)

    def test_provider_change_none_other_requires_comment_but_named_reason_does_not(self):
        ok_body = urlencode({"apply_scope": "none", "event_at": "2026-06-10T10:00", "provider_id": "1", "reason": "Провайдер сменил маршрут", "comment": ""})
        captured, _ = self.request("/provider-changes/create", method="POST", body=ok_body)
        self.assertEqual(captured["status"], "303 See Other")
        bad_body = urlencode({"apply_scope": "none", "event_at": "2026-06-10T11:00", "provider_id": "1", "reason": "Другое", "comment": ""})
        captured, content = self.request("/provider-changes/create", method="POST", body=bad_body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Требуется понятный комментарий", content)

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

    def test_provider_change_none_scope_route_select_filters_by_geo_and_provider(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("const routes = [", content)
        self.assertIn("function rebuildAffectedRouteSelect()", content)
        self.assertIn("select.innerHTML = '<option value=\"\">—</option>';", content)
        self.assertIn("if (providerId)", content)
        self.assertIn("String(route.provider_id) === String(providerId)", content)
        self.assertIn("String(route.country_id) === String(countryId)", content)
        self.assertIn("input[name=\"apply_scope\"], #event-country, #event-provider", content)

    def _create_overflow_route(self, name="Резервный ШЛЮЗ GSM", is_actual=1, country_id=1, provider_id=1):
        conn = server.connect(server.DB_PATH)
        try:
            server.init_db(conn)
            server.ensure_seed(server.Repository(conn))
            if not conn.execute("SELECT 1 FROM countries WHERE id = ?", (country_id,)).fetchone():
                conn.execute("INSERT INTO countries(id, name, code, is_active) VALUES (?, ?, ?, 1)", (country_id, f"Test GEO {country_id}", f"TG{country_id}"))
            cur = conn.execute(
                """
                INSERT INTO routes(country_id, provider_id, name, cli_source_type, cli_source_label, created_by, is_actual)
                VALUES (?, ?, ?, 'rnd', 'test', 1, ?)
                """,
                (country_id, provider_id, name, is_actual),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def test_provider_change_overflow_block_is_server_priority_only(self):
        self.request("/routes")
        overflow_id = self._create_overflow_route()
        _captured, content = self.request("/provider-changes")
        self.assertIn("data-scopes='server_priority'><input type='checkbox' name='has_overflow'", content)
        self.assertIn("id='overflow-route-field'>Маршрут перелива", content)
        self.assertIn(f"<option value='{overflow_id}'", content)

    def test_provider_change_non_server_priority_does_not_save_overflow(self):
        self.request("/routes")
        overflow_id = self._create_overflow_route()
        body = urlencode({
            "apply_scope": "none", "event_at": "2026-06-10T10:00", "provider_id": "1",
            "reason": "Провайдер сменил маршрут", "comment": "без применения",
            "has_overflow": "1", "overflow_route_id": str(overflow_id),
        })
        captured, _content = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT has_overflow, overflow_route_id FROM routing_events ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(row["has_overflow"], 0)
            self.assertIsNone(row["overflow_route_id"])
        finally:
            conn.close()

    def test_server_priority_overflow_validation_and_display(self):
        self.request("/routes")
        overflow_id = self._create_overflow_route("GSM шлюз резерв")
        body_without_route = urlencode({
            "apply_scope": "server_priority", "event_at": "2026-06-10T11:00", "country_id": "1",
            "server_ids": "1", "provider_id": "1", "new_route_id": "1", "reason": "Задача руководства",
            "comment": "нет маршрута", "has_overflow": "1",
        })
        captured, content = self.request("/provider-changes/create", method="POST", body=body_without_route)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Маршрут перелива обязателен", content)

        body = urlencode({
            "apply_scope": "server_priority", "event_at": "2026-06-10T11:05", "country_id": "1",
            "server_ids": "1", "provider_id": "1", "new_route_id": "1", "reason": "Задача руководства",
            "comment": "с переливом", "has_overflow": "1", "overflow_route_id": str(overflow_id),
        })
        captured, _content = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        _captured, content = self.request("/provider-changes")
        self.assertIn("Перелив: GSM шлюз резерв", content)

        body_no_overflow = urlencode({
            "apply_scope": "server_priority", "event_at": "2026-06-10T11:10", "country_id": "1",
            "server_ids": "1", "provider_id": "1", "new_route_id": str(overflow_id), "reason": "Задача руководства",
            "comment": "без перелива",
        })
        captured, _content = self.request("/provider-changes/create", method="POST", body=body_no_overflow)
        self.assertEqual(captured["status"], "303 See Other")
        _captured, content = self.request("/provider-changes")
        self.assertIn("Перелив: —", content)

    def test_server_priorities_page_shows_current_overflow_route_only(self):
        self.request("/routes")
        overflow_id = self._create_overflow_route("GSM шлюз резерв")
        body = urlencode({
            "apply_scope": "server_priority", "event_at": "2026-06-10T11:05", "country_id": "1",
            "server_ids": "1", "provider_id": "1", "new_route_id": "1", "reason": "Задача руководства",
            "comment": "с переливом", "has_overflow": "1", "overflow_route_id": str(overflow_id),
        })
        captured, _content = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")

        captured, content = self.request("/admin/server-priorities?server_id=1")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("data-col='overflow'", content)
        self.assertIn("Перелив", content)
        eu1_block = content.split("<h2>Сервер: EU1</h2>", 1)[1].split("</section>", 1)[0]
        self.assertIn("data-col='current_priority'>Мексика/Sancom/Demo_0827@<br><span class='muted'>Перелив: GSM шлюз резерв</span></td>", eu1_block)
        self.assertIn("data-col='previous_priority'>Мексика/Miatel/Demo_A@<br><span class='muted'>Перелив: —</span></td>", eu1_block)

    def test_server_priorities_page_shows_dash_without_current_overflow(self):
        self.request("/routes")
        captured, content = self.request("/admin/server-priorities?server_id=1")
        self.assertEqual(captured["status"], "200 OK")
        eu1_block = content.split("<h2>Сервер: EU1</h2>", 1)[1].split("</section>", 1)[0]
        self.assertIn("data-col='current_priority'>Мексика/Miatel/Demo_A@<br><span class='muted'>Перелив: —</span></td>", eu1_block)

    def test_server_priorities_page_shows_previous_priority_own_overflow_after_current_priority_changes(self):
        self.request("/routes")
        overflow_id = self._create_overflow_route("GSM шлюз резерв")
        with_overflow = urlencode({
            "apply_scope": "server_priority", "event_at": "2026-06-10T11:05", "country_id": "1",
            "server_ids": "1", "provider_id": "1", "new_route_id": "1", "reason": "Задача руководства",
            "comment": "с переливом", "has_overflow": "1", "overflow_route_id": str(overflow_id),
        })
        captured, _content = self.request("/provider-changes/create", method="POST", body=with_overflow)
        self.assertEqual(captured["status"], "303 See Other")
        without_overflow = urlencode({
            "apply_scope": "server_priority", "event_at": "2026-06-10T11:10", "country_id": "1",
            "server_ids": "1", "provider_id": "2", "new_route_id": "2", "reason": "Задача руководства",
            "comment": "без перелива",
        })
        captured, _content = self.request("/provider-changes/create", method="POST", body=without_overflow)
        self.assertEqual(captured["status"], "303 See Other")

        captured, content = self.request("/admin/server-priorities?server_id=1")
        self.assertEqual(captured["status"], "200 OK")
        eu1_block = content.split("<h2>Сервер: EU1</h2>", 1)[1].split("</section>", 1)[0]
        self.assertIn("data-col='current_priority'>Мексика/Miatel/Demo_A@<br><span class='muted'>Перелив: —</span></td>", eu1_block)
        self.assertIn("data-col='previous_priority'>Мексика/Sancom/Demo_0827@<br><span class='muted'>Перелив: GSM шлюз резерв</span></td>", eu1_block)

    def test_overflow_route_select_and_validation_allow_active_routes_for_selected_geo(self):
        self.request("/routes")
        active_gateway = self._create_overflow_route("Активный шлюз")
        inactive_gateway = self._create_overflow_route("Неактивный шлюз", is_actual=0)
        active_non_gateway = self._create_overflow_route("Активный резерв")
        other_geo = self._create_overflow_route("Казахстан резерв", country_id=2, provider_id=1)
        _captured, content = self.request("/provider-changes")
        overflow_select = content.split("id='overflow-route'", 1)[1].split("</select>", 1)[0]
        self.assertIn(f"<option value='{active_gateway}'", overflow_select)
        self.assertNotIn(f"<option value='{inactive_gateway}'", overflow_select)
        self.assertIn(f"<option value='{active_non_gateway}'", overflow_select)
        self.assertIn(f"<option value='{other_geo}'", overflow_select)
        self.assertIn("rebuildRouteSelect(document.getElementById('overflow-route'), country && country.value, null, null);", content)

        for route_id in (inactive_gateway, other_geo):
            body = urlencode({
                "apply_scope": "server_priority", "event_at": "2026-06-10T11:15", "country_id": "1",
                "server_ids": "1", "provider_id": "1", "new_route_id": "1", "reason": "Задача руководства",
                "comment": "bad overflow", "has_overflow": "1", "overflow_route_id": str(route_id),
            })
            captured, content = self.request("/provider-changes/create", method="POST", body=body)
            self.assertEqual(captured["status"], "400 Bad Request")
            self.assertIn("Маршрут перелива должен быть активным и относиться к выбранному GEO", content)


    def test_server_priority_allows_same_route_when_overflow_state_changes(self):
        self.request("/routes")
        overflow_id = self._create_overflow_route("Активный резерв без gateway word")
        body = urlencode({
            "apply_scope": "server_priority", "event_at": "2026-06-10T11:20", "country_id": "1",
            "server_ids": "1", "provider_id": "1", "new_route_id": "1", "reason": "Задача руководства",
            "comment": "add overflow", "has_overflow": "1", "overflow_route_id": str(overflow_id),
        })
        captured, _content = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT current_route_id, has_overflow, overflow_route_id FROM server_route_priorities WHERE country_id = 1 AND server_id = 1").fetchone()
            self.assertEqual((row["current_route_id"], row["has_overflow"], row["overflow_route_id"]), (1, 1, overflow_id))
        finally:
            conn.close()

        captured, content = self.request("/admin/server-priorities?server_id=1")
        self.assertEqual(captured["status"], "200 OK")
        eu1_block = content.split("<h2>Сервер: EU1</h2>", 1)[1].split("</section>", 1)[0]
        self.assertIn("data-col='current_priority'>Мексика/Sancom/Demo_0827@<br><span class='muted'>Перелив: Активный резерв без gateway word</span></td>", eu1_block)
        self.assertIn("data-col='previous_priority'>Мексика/Miatel/Demo_A@<br><span class='muted'>Перелив: —</span></td>", eu1_block)

    def test_server_priority_rejects_only_identical_route_and_overflow_state(self):
        self.request("/routes")
        overflow_id = self._create_overflow_route("Активный резерв")
        body = urlencode({
            "apply_scope": "server_priority", "event_at": "2026-06-10T11:25", "country_id": "1",
            "server_ids": "1", "provider_id": "1", "new_route_id": "1", "reason": "Задача руководства",
            "comment": "add overflow", "has_overflow": "1", "overflow_route_id": str(overflow_id),
        })
        self.assertEqual(self.request("/provider-changes/create", method="POST", body=body)[0]["status"], "303 See Other")
        captured, content = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Выбранный маршрут уже установлен для всех выбранных серверов", content)

    def test_server_priority_allows_same_route_when_overflow_route_changes(self):
        self.request("/routes")
        first = self._create_overflow_route("Первый резерв")
        second = self._create_overflow_route("Второй резерв")
        initial = urlencode({
            "apply_scope": "server_priority", "event_at": "2026-06-10T11:30", "country_id": "1",
            "server_ids": "1", "provider_id": "1", "new_route_id": "1", "reason": "Задача руководства",
            "comment": "first overflow", "has_overflow": "1", "overflow_route_id": str(first),
        })
        self.assertEqual(self.request("/provider-changes/create", method="POST", body=initial)[0]["status"], "303 See Other")
        changed = urlencode({
            "apply_scope": "server_priority", "event_at": "2026-06-10T11:35", "country_id": "1",
            "server_ids": "1", "provider_id": "1", "new_route_id": "1", "reason": "Задача руководства",
            "comment": "second overflow", "has_overflow": "1", "overflow_route_id": str(second),
        })
        captured, _content = self.request("/provider-changes/create", method="POST", body=changed)
        self.assertEqual(captured["status"], "303 See Other")

    def test_provider_change_server_priority_create_hides_server_block_temporarily(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        create_form = content.split("<form method='post' action='/provider-changes/create'", 1)[1].split("</form>", 1)[0]
        server_priority_content = create_form.split("data-scope-content='server_priority'", 1)[1].split("data-scope-content='campaign_setting'", 1)[0]
        self.assertNotIn("<legend>Серверы", server_priority_content)
        self.assertNotIn("name='server_ids'", server_priority_content)
        self.assertNotIn("data-server-select='all'", server_priority_content)
        self.assertNotIn("data-server-select='none'", server_priority_content)
        self.assertNotIn("server-current-routes", server_priority_content)
        self.assertIn("Дата события", server_priority_content)
        self.assertIn("GEO", server_priority_content)
        self.assertIn("Есть перелив", server_priority_content)
        self.assertIn("Провайдер", server_priority_content)
        self.assertIn("Новый маршрут", server_priority_content)
        self.assertIn("Причина", server_priority_content)
        self.assertIn("Комментарий", server_priority_content)
        self.assertIn("server-priority-create-right", server_priority_content)

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
            miatel_prefix = conn.execute("INSERT INTO provider_prefixes(provider_id, prefix, name, is_active) VALUES (?, NULL, 'Без префикса', 1)", (miatel_id,)).lastrowid
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
                assignment_type="gl",
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
        body = urlencode({"number": "525550009901", "country_id": "1", "provider_id": "", "assignment_type": "gl", "status": "used"})
        captured, content = self.request("/phones/create", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Провайдер обязателен", content)

    def test_review_required_badge_and_edit_rules(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            conn.execute("""
                INSERT INTO phone_numbers(country_id, provider_id, number, normalized_number, project_label, assignment_type, status, comment, is_active, review_required, created_by)
                VALUES (1, NULL, '525550009902', '525550009902', 'Demo', 'gl', 'unknown', 'Needs review', 1, 1, 1)
            """)
            conn.commit()
            phone_id = conn.execute("SELECT id FROM phone_numbers WHERE number = '525550009902'").fetchone()["id"]
        finally:
            conn.close()
        captured, content = self.request("/phones")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("class='review-required-icon'", content)
        self.assertIn("title='Требует проверки'", content)
        phone_row = content[content.index("525550009902"):content.index("525550009902") + 700]
        self.assertNotIn("<span class='badge'>Требует проверки</span>", phone_row)
        body = urlencode({"number": "525550009902", "country_id": "1", "provider_id": "", "assignment_type": "gl", "status": "unknown", "is_active": "1"})
        captured, content = self.request(f"/phones/{phone_id}/update", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Нельзя снять флаг проверки, пока не выбран провайдер", content)
        body = urlencode({"number": "525550009902", "country_id": "1", "provider_id": "", "assignment_type": "gl", "status": "unknown", "is_active": "1", "review_required": "1", "comment": "Edited"})
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
        body = urlencode({"number": "525550009902", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "unknown", "is_active": "1"})
        captured, _ = self.request(f"/phones/{phone_id}/update", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT review_required, provider_id FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
            self.assertEqual(row["review_required"], 0)
            self.assertEqual(row["provider_id"], 1)
        finally:
            conn.close()

    def test_phone_edit_and_history_render_imported_created_by(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            conn.execute("""
                INSERT INTO phone_numbers(country_id, provider_id, number, normalized_number, project_label, assignment_type, status, is_active, review_required, imported_created_by, created_by)
                VALUES (1, 1, '525550009920', '525550009920', 'Demo', 'gl', 'used', 1, 0, 'old_admin', 1)
            """)
            phone_id = conn.execute("SELECT id FROM phone_numbers WHERE number = '525550009920'").fetchone()["id"]
            conn.execute("""
                INSERT INTO phone_number_history(phone_number_id, action, changed_by, field_name, new_value, comment)
                VALUES (?, 'created', 1, 'number', '525550009920', 'Создал в Excel: old_admin')
            """, (phone_id,))
            conn.commit()
        finally:
            conn.close()
        captured, content = self.request(f"/phones/{phone_id}/edit")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Создал в Excel:", content)
        self.assertIn("old_admin", content)
        captured, history = self.request(f"/phones/{phone_id}/history")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Admin", history)
        self.assertIn("Создал в Excel: old_admin", history)


    def test_reactivation_review_can_be_cleared_from_phone_edit(self):
        self.request("/routes")
        body = urlencode({"number": "525550009909", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "used", "is_active": "1", "connection_cost": "50", "monthly_fee": "50"})
        captured, _ = self.request("/phones/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            phone_id = conn.execute("SELECT id FROM phone_numbers WHERE number = '525550009909'").fetchone()["id"]
        finally:
            conn.close()

        deactivate = urlencode({"number": "525550009909", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "used", "is_active": "0", "connection_cost": "50", "monthly_fee": "50"})
        captured, _ = self.request(f"/phones/{phone_id}/update", method="POST", body=deactivate)
        self.assertEqual(captured["status"], "303 See Other")
        reactivate = urlencode({"number": "525550009909", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "used", "is_active": "1", "connection_cost": "50", "monthly_fee": "50"})
        captured, _ = self.request(f"/phones/{phone_id}/update", method="POST", body=reactivate)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT review_required FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
            self.assertEqual(row["review_required"], 1)
        finally:
            conn.close()

        captured, content = self.request("/phones")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("525550009909", content)
        self.assertIn("class='review-required-icon'", content)

        clear = urlencode({"number": "525550009909", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "used", "is_active": "1", "connection_cost": "50.00", "monthly_fee": "50.000000"})
        captured, _ = self.request(f"/phones/{phone_id}/update", method="POST", body=clear)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT review_required FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
            self.assertEqual(row["review_required"], 0)
            history = conn.execute("SELECT new_value FROM phone_number_history WHERE phone_number_id = ? AND action = 'updated' ORDER BY id DESC", (phone_id,)).fetchone()["new_value"]
        finally:
            conn.close()
        self.assertIn("Требует проверки: Да → Нет", history)
        self.assertNotIn("Активен у провайдера: Нет → Да", history)
        self.assertNotIn("Требует проверки: Нет → Да", history)
        self.assertNotIn("Стоимость подключения", history)
        self.assertNotIn("Абонентская плата", history)
        captured, content = self.request("/phones")
        self.assertEqual(captured["status"], "200 OK")
        phone_row = content[content.index("525550009909"):content.index("525550009909") + 300]
        self.assertNotIn("Требует проверки", phone_row)



    def test_phones_page_displays_unknown_monthly_fee_as_question_marks(self):
        self.request("/routes")
        body = urlencode({"number": "525550009910", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "used", "is_active": "1", "monthly_fee": ""})
        captured, _ = self.request("/phones/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        captured, content = self.request("/phones?number=525550009910")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("<td data-col='monthly'>???</td>", content)

    def test_phones_review_required_filter_shows_only_review_numbers(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            conn.execute("""
                INSERT INTO phone_numbers(country_id, provider_id, number, normalized_number, project_label, assignment_type, status, comment, is_active, review_required, created_by)
                VALUES (1, NULL, '525550009910', '525550009910', 'Demo', 'gl', 'unknown', 'Needs review', 1, 1, 1)
            """)
            conn.execute("""
                INSERT INTO phone_numbers(country_id, provider_id, number, normalized_number, project_label, assignment_type, status, comment, is_active, review_required, created_by)
                VALUES (1, 1, '525550009911', '525550009911', 'Demo', 'gl', 'unknown', 'No review', 1, 0, 1)
            """)
            conn.commit()
        finally:
            conn.close()

        captured, content = self.request("/phones?review_required=1")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn('name="review_required" value="1" checked', content)
        self.assertIn("525550009910", content)
        self.assertNotIn("525550009911", content)
        self.assertIn("<details class='filter-card' open>", content)

        captured, content = self.request("/phones?reset_filters=1")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("525550009910", content)
        self.assertIn("525550009911", content)
        self.assertNotIn('name="review_required" value="1" checked', content)

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


    def test_history_icon_links_are_in_phone_and_route_tables_not_exports(self):
        self.request("/routes")
        captured, content = self.request("/phones")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("data-col='history'", content)
        self.assertIn("title='История'", content)
        self.assertRegex(content, r"href='/phones/\d+/history'.*material-symbols-rounded.*info")

        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("data-col='history'", content)
        self.assertRegex(content, r"href='/routes/\d+/history'.*material-symbols-rounded.*info")

        captured, content = self.request("/phones?export=csv")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("Ист.", content)
        self.assertNotIn("История", content)
        self.assertNotIn("history", content)

        captured, content = self.request("/routes?export=csv")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("Ист.", content)
        self.assertNotIn("История", content)
        self.assertNotIn("history", content)

    def test_phone_and_route_history_pages_show_titles_subjects_and_empty_state(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            phone = conn.execute("SELECT id, number FROM phone_numbers ORDER BY id LIMIT 1").fetchone()
            route = conn.execute("SELECT id, name FROM routes ORDER BY id LIMIT 1").fetchone()
            conn.execute("DELETE FROM phone_number_history WHERE phone_number_id = ?", (phone["id"],))
            conn.execute("DELETE FROM route_phone_number_history WHERE phone_number_id = ?", (phone["id"],))
            conn.commit()
        finally:
            conn.close()

        captured, content = self.request(f"/phones/{phone['id']}/history")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("История номера", content)
        self.assertIn(phone["number"], content)
        self.assertIn("История пока пустая", content)

        captured, content = self.request(f"/routes/{route['id']}/history")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("История маршрута", content)
        self.assertIn(route["name"], content)

    def test_history_pages_include_route_phone_add_remove_events(self):
        self.request("/routes")
        body = urlencode({"number": "525550088888", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "used", "is_active": "1"})
        self.request("/phones/create", method="POST", body=body)
        body = urlencode({"phone_number": "525550088888", "usage_type": "pool_member", "comment": "for history"})
        self.request("/routes/1/numbers/add", method="POST", body=body)
        conn = server.connect(server.DB_PATH)
        try:
            phone_id = conn.execute("SELECT id FROM phone_numbers WHERE number = '525550088888'").fetchone()["id"]
            link_id = conn.execute("SELECT id FROM route_phone_numbers WHERE route_id = 1 AND phone_number_id = ?", (phone_id,)).fetchone()["id"]
        finally:
            conn.close()
        self.request("/routes/1/numbers/remove", method="POST", body=urlencode({"link_ids": str(link_id), "reason": "Проверка"}))

        captured, content = self.request(f"/phones/{phone_id}/history")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Номер добавлен в маршрут", content)
        self.assertIn("Номер исключён из маршрута", content)
        self.assertIn("Проверка", content)

        captured, content = self.request("/routes/1/history")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("525550088888", content)
        self.assertIn("Номер добавлен", content)
        self.assertIn("Номер исключён", content)

    def test_user_without_phone_read_permission_cannot_access_phone_history(self):
        cookie = self.user_cookie("guest")
        captured, content = self.request("/phones/1/history", cookie=cookie)
        self.assertEqual(captured["status"], "403 Forbidden")
        self.assertIn("Нет доступа", content)

    def test_duplicate_phone_returns_user_message(self):
        self.request("/routes")
        body = urlencode({"number": "525550000001", "country_id": "1", "provider_id": "2", "assignment_type": "gl", "status": "used"})
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
        body = urlencode({"number": "525550099998", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "free", "is_active": "1"})
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
        body = urlencode({"number": "525550099997", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "free", "is_active": "1"})
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
        body = urlencode({"number": "525550099996", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "free", "is_active": "1"})
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
        body = urlencode({"number": "525550099995", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "used"})
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
        body = urlencode({"number": "525550099999", "country_id": "1", "provider_id": "1", "assignment_type": "gl", "status": "used", "is_active": "1"})
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


    def test_route_aon_source_dropdown_offers_only_new_sources(self):
        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Тип АОН", content)
        self.assertNotIn("Источник АОН", content)
        source_select = content.split('name="cli_source_type"', 1)[1].split('</select>', 1)[0]
        self.assertIn("value='pool'", source_select)
        self.assertIn("value='rnd'", source_select)
        self.assertIn("value='sim'", source_select)
        self.assertNotIn('single_number', source_select)
        self.assertNotIn('other', source_select)

    def test_pool_route_saves_manual_aon_label_and_pool_info(self):
        self.request("/routes")
        body = urlencode({"country_id": "1", "provider_id": "1", "provider_prefix_id": "", "project_label": "", "cli_source_type": "pool", "cli_source_label": "pool_A_stage1", "aon_pool": "Пул купленных номеров", "is_actual": "1"})
        captured, _ = self.request("/routes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT cli_source_type, cli_source_label, aon_pool FROM routes WHERE cli_source_label = 'pool_A_stage1'").fetchone()
            self.assertEqual((row["cli_source_type"], row["cli_source_label"], row["aon_pool"]), ("pool", "pool_A_stage1", "Пул купленных номеров"))
        finally:
            conn.close()
        captured, content = self.request("/routes")
        self.assertIn("Пул купленных номеров", content)

    def test_rnd_local_sets_label_and_pool_info(self):
        self.request("/routes")
        body = urlencode({"country_id": "1", "provider_id": "1", "provider_prefix_id": "", "project_label": "", "cli_source_type": "rnd", "cli_source_label": "ignored", "rnd_type": "local", "is_actual": "1"})
        captured, _ = self.request("/routes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT cli_source_label, aon_pool, rnd_type FROM routes WHERE cli_source_type = 'rnd' AND aon_pool = 'Локальный пул' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual((row["cli_source_label"], row["aon_pool"], row["rnd_type"]), ("RND", "Локальный пул", "local"))
        finally:
            conn.close()

    def test_rnd_nonlocal_requires_and_saves_pool_ownership(self):
        self.request("/routes")
        invalid = urlencode({"country_id": "1", "provider_id": "1", "cli_source_type": "rnd", "is_actual": "1"})
        captured, content = self.request("/routes/create", method="POST", body=invalid)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Тип пула обязателен для RND", content)

        valid = urlencode({"country_id": "1", "provider_id": "1", "provider_prefix_id": "", "project_label": "", "cli_source_type": "rnd", "rnd_type": "nonlocal", "rnd_pool_owner": "венгерский пул", "is_actual": "1"})
        captured, _ = self.request("/routes/create", method="POST", body=valid)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT cli_source_label, aon_pool, rnd_type, rnd_pool_owner FROM routes WHERE aon_pool = 'Нелокальный пул: венгерский пул'").fetchone()
            self.assertEqual((row["cli_source_label"], row["rnd_type"], row["rnd_pool_owner"]), ("RND", "nonlocal", "венгерский пул"))
        finally:
            conn.close()

    def test_sim_sets_label_and_pool_info(self):
        self.request("/routes")
        body = urlencode({"country_id": "1", "provider_id": "1", "provider_prefix_id": "", "project_label": "", "cli_source_type": "sim", "cli_source_label": "ignored", "is_actual": "1"})
        captured, _ = self.request("/routes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT cli_source_type, cli_source_label, aon_pool FROM routes WHERE cli_source_type = 'sim' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual((row["cli_source_type"], row["cli_source_label"], row["aon_pool"]), ("sim", "SIM", "SIM / GSM-шлюз"))
        finally:
            conn.close()

    def test_existing_legacy_aon_source_route_still_displays(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            cur = conn.execute("INSERT INTO routes(country_id, provider_id, name, cli_source_type, cli_source_label, created_by) VALUES (1, 1, 'Legacy/Single@', 'single_number', 'old_single', 1)")
            route_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Legacy/Single@", content)
        captured, content = self.request(f"/routes/{route_id}/edit")
        self.assertIn("Single", content)


    def test_route_name_template_examples_and_custom_saved_name(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            repo = server.Repository(conn)
            admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
            br_id = repo.create_country("Бразилия", "BR")
            it_id = repo.create_country("Италия", "IT")
            at_id = repo.create_country("Австрия", "AT")
            miatel_id = conn.execute("SELECT id FROM providers WHERE name = 'Miatel'").fetchone()["id"]
            prefix_id = repo.create_prefix(miatel_id, "0333")
            self.assertEqual(server.build_route_name(repo, br_id, miatel_id, "Меж.деп.", "RND", None), "Бразилия/Miatel/RND@")
            self.assertEqual(server.build_route_name(repo, br_id, miatel_id, "", "Pool_B", None), "Бразилия/Miatel/Pool_B@")
            self.assertEqual(server.build_route_name(repo, it_id, miatel_id, "ITM", "Pool_B", None), "Италия/ITM/Miatel/Pool_B@")
            self.assertEqual(server.build_route_name(repo, at_id, miatel_id, "ITM", "RND", prefix_id), "Австрия/ITM/Miatel/RND/0333pfx@")
            conn.commit()
        finally:
            conn.close()

        body = urlencode({"country_id": "1", "provider_id": "1", "provider_prefix_id": "", "project_label": "", "cli_source_type": "pool", "cli_source_label": "custom_label", "aon_pool": "Пул купленных номеров", "name": "Custom/Confirmed@", "is_actual": "1"})
        captured, _ = self.request("/routes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT name FROM routes WHERE cli_source_label = 'custom_label'").fetchone()
            self.assertEqual(row["name"], "Custom/Confirmed@")
        finally:
            conn.close()

    def test_duplicate_route_returns_user_message(self):
        self.request("/routes")
        body = urlencode({"country_id": "1", "provider_id": "2", "provider_prefix_id": "", "project_label": "", "cli_source_type": "pool", "cli_source_label": "Demo_A", "is_actual": "1"})
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
        captured, _ = self.request("/admin/dictionaries/providers/create", method="POST", body=urlencode({"name": "NewTel", "default_currency_id": "1", "comment": "New provider"}))
        self.assertEqual(captured["status"], "303 See Other")
        captured, _ = self.request("/admin/dictionaries/currencies/create", method="POST", body=urlencode({"code": "USD", "name": "US Dollar"}))
        self.assertEqual(captured["status"], "303 See Other")
        captured, _ = self.request("/admin/dictionaries/prefixes/create", method="POST", body=urlencode({"provider_id": "1", "prefix": "0333", "name": "New prefix"}))
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

    def test_currency_rate_upsert_creates_historical_rows_and_shows_latest(self):
        self.request("/tariffs")
        body = urlencode({"currency_id": "2", "rate_to_eur": "0.91"})
        self.request("/admin/currency-rates/upsert", method="POST", body=body)
        self.request("/admin/currency-rates/upsert", method="POST", body=urlencode({"currency_id": "2", "rate_to_eur": "0.92"}))

        conn = server.connect(server.DB_PATH)
        try:
            rows = conn.execute("SELECT rate_to_eur FROM currency_rates WHERE currency_id = 2 ORDER BY id").fetchall()
            latest = server.Repository(conn).latest_currency_rate(2)
        finally:
            conn.close()
        self.assertEqual([str(row["rate_to_eur"]) for row in rows[-2:]], ["0.91", "0.92"])
        self.assertEqual(str(latest["rate_to_eur"]), "0.92")

        captured, content = self.request("/admin/currency-rates")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("0.92", content)
        self.assertNotIn("0.91", content)

    def test_currency_rate_upsert_writes_change_log(self):
        self.request("/tariffs")
        self.request("/admin/currency-rates/upsert", method="POST", body=urlencode({"currency_id": "2", "rate_to_eur": "1.01"}))
        self.request("/admin/currency-rates/upsert", method="POST", body=urlencode({"currency_id": "2", "rate_to_eur": "500"}))

        conn = server.connect(server.DB_PATH)
        try:
            log = conn.execute(
                """
                SELECT * FROM change_log
                WHERE entity_type = 'currency_rate'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(log)
        self.assertEqual(log["change_type"], "currency_rate.manual_created")
        self.assertIsNotNone(log["changed_by"])
        self.assertEqual(log["source"], "ui")
        self.assertIn("Курс USDT к EUR обновлён вручную: 1.01 → 500", log["summary"])
        self.assertIn("Активных тариф", log["summary"])
        old_values = json.loads(log["old_values"])
        new_values = json.loads(log["new_values"])
        self.assertEqual(old_values["currency_code"], "USDT")
        self.assertEqual(old_values["rate_to_eur"], "1.01")
        self.assertEqual(new_values["currency_code"], "USDT")
        self.assertEqual(new_values["rate_to_eur"], "500")
        self.assertEqual(log["entity_id"], new_values["currency_rate_id"])


    def test_currency_rate_upsert_recalculates_active_tariffs(self):
        self.request("/tariffs")
        conn = server.connect(server.DB_PATH)
        try:
            tariff = conn.execute("SELECT id, price_in_provider_currency FROM tariffs WHERE provider_currency_id = 2 AND is_current = 1 LIMIT 1").fetchone()
            self.assertIsNotNone(tariff)
            tariff_id = tariff["id"]
        finally:
            conn.close()

        self.request("/admin/currency-rates/upsert", method="POST", body=urlencode({"currency_id": "2", "rate_to_eur": "300"}))

        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT eur_price, conversion_rate_to_eur, currency_rate_id FROM tariffs WHERE id = ?", (tariff_id,)).fetchone()
            rate = conn.execute("SELECT id FROM currency_rates WHERE currency_id = 2 ORDER BY id DESC LIMIT 1").fetchone()
            tariff_log = conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'tariff' AND entity_id = ? AND change_type = 'tariff.currency_rate_recalculated'", (tariff_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(str(row["conversion_rate_to_eur"]), "300")
        self.assertEqual(row["currency_rate_id"], rate["id"])
        self.assertEqual(Decimal(str(row["eur_price"])), Decimal(str(tariff["price_in_provider_currency"])) * Decimal("300"))
        self.assertEqual(tariff_log, 1)
        captured, content = self.request("/tariffs")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(f"{row['eur_price']} EUR", content)

    def test_currency_rate_invalid_value_does_not_recalculate_tariffs(self):
        self.request("/tariffs")
        conn = server.connect(server.DB_PATH)
        try:
            before_tariff = conn.execute("SELECT id, eur_price, conversion_rate_to_eur, currency_rate_id FROM tariffs WHERE provider_currency_id = 2 AND is_current = 1 LIMIT 1").fetchone()
            before_history = conn.execute("SELECT COUNT(*) FROM tariff_change_history").fetchone()[0]
            before_tariff_logs = conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'tariff'").fetchone()[0]
        finally:
            conn.close()

        captured, content = self.request("/admin/currency-rates/upsert", method="POST", body=urlencode({"currency_id": "2", "rate_to_eur": "abc"}))
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Ошибка", content)

        conn = server.connect(server.DB_PATH)
        try:
            after_tariff = conn.execute("SELECT eur_price, conversion_rate_to_eur, currency_rate_id FROM tariffs WHERE id = ?", (before_tariff["id"],)).fetchone()
            after_history = conn.execute("SELECT COUNT(*) FROM tariff_change_history").fetchone()[0]
            after_tariff_logs = conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'tariff'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(str(after_tariff["eur_price"]), str(before_tariff["eur_price"]))
        self.assertEqual(str(after_tariff["conversion_rate_to_eur"]), str(before_tariff["conversion_rate_to_eur"]))
        self.assertEqual(after_tariff["currency_rate_id"], before_tariff["currency_rate_id"])
        self.assertEqual(after_history, before_history)
        self.assertEqual(after_tariff_logs, before_tariff_logs)

    def test_currency_rate_invalid_value_does_not_write_change_log(self):
        self.request("/tariffs")
        conn = server.connect(server.DB_PATH)
        try:
            before_rates = conn.execute("SELECT COUNT(*) FROM currency_rates").fetchone()[0]
            before_logs = conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'currency_rate'").fetchone()[0]
        finally:
            conn.close()

        captured, content = self.request("/admin/currency-rates/upsert", method="POST", body=urlencode({"currency_id": "2", "rate_to_eur": "0"}))
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Ошибка", content)

        conn = server.connect(server.DB_PATH)
        try:
            after_rates = conn.execute("SELECT COUNT(*) FROM currency_rates").fetchone()[0]
            after_logs = conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'currency_rate'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(after_rates, before_rates)
        self.assertEqual(after_logs, before_logs)

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

    def test_calling_company_history_pages_links_and_export(self):
        captured, content = self.request("/companies")
        self.assertEqual(captured["status"].split()[0], "200")
        self.assertIn("data-col='history'", content)
        self.assertRegex(content, r"href='/calling-companies/\d+/history'.*material-symbols-rounded.*info")
        self.assertIn("Журнал событий", content)
        self.assertIn("<a class='button table-utility-button' href='/calling-companies/history'>Журнал событий</a>", content)
        self.assertNotIn("href='/calling-companies/history' target='_blank'", content)
        self.assertNotIn("target='_blank'", content)
        captured, history = self.request("/calling-companies/1/history")
        self.assertEqual(captured["status"].split()[0], "200")
        self.assertIn("История компании прозвона", history)
        self.assertIn("ID компании", history)
        captured, csv = self.request("/companies?export=csv")
        self.assertNotIn("history", csv.lower())
        self.assertNotIn("Журнал событий", csv)

    def test_company_edit_shows_autorotation_read_only_from_routing_settings(self):
        body = urlencode({"server_id": "1", "country_id": "1", "company_id_external": "readonly-auto", "company_name": "Readonly Auto", "line_count": "1", "dial_set_count": "2", "has_autorotation": "1", "retry_interval_seconds": "30", "is_active": "1", "comment": ""})
        self.request("/companies/create", method="POST", body=body)
        conn = server.connect(server.DB_PATH)
        try:
            company_id = conn.execute("SELECT id FROM calling_companies WHERE company_id_external = 'readonly-auto'").fetchone()["id"]
        finally:
            conn.close()

        captured, content = self.request(f"/companies/{company_id}/edit")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Авторотация: Да", content)
        self.assertIn("Маршрутизация компании изменяется через ‘Смена провайдеров’.", content)
        self.assertNotIn("name='has_autorotation'", content)

        update = urlencode({"server_id": "1", "country_id": "1", "company_name": "Readonly Auto Updated", "line_count": "5", "dial_set_count": "6", "retry_interval_seconds": "45", "is_active": "1", "comment": "base fields changed"})
        captured, _ = self.request(f"/companies/{company_id}/update", method="POST", body=update)
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            setting = conn.execute("SELECT * FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL", (company_id,)).fetchone()
            self.assertEqual(setting["has_autorotation"], 1)
        finally:
            conn.close()

    def test_global_calling_company_journal_searches_old_names(self):
        body = urlencode({"server_id": "1", "country_id": "1", "company_id_external": "history-search-id", "company_name": "HistoryOld", "line_count": "1", "dial_set_count": "2", "has_autorotation": "0", "retry_interval_seconds": "30", "is_active": "1", "comment": "old comment"})
        self.request("/companies/create", method="POST", body=body)
        conn = server.connect(server.DB_PATH)
        try:
            company_id = conn.execute("SELECT id FROM calling_companies WHERE company_id_external = 'history-search-id'").fetchone()["id"]
        finally:
            conn.close()
        body = urlencode({"server_id": "1", "country_id": "1", "company_name": "HistoryNew", "line_count": "1", "dial_set_count": "2", "has_autorotation": "0", "retry_interval_seconds": "30", "is_active": "1", "comment": "find this comment"})
        self.request(f"/companies/{company_id}/update", method="POST", body=body)
        captured, content = self.request("/calling-companies/history?search=HistoryOld")
        self.assertEqual(captured["status"].split()[0], "200")
        self.assertIn("Журнал событий компаний прозвона", content)
        self.assertIn("Поиск по журналу", content)
        self.assertIn("HistoryOld", content)
        self.assertIn("HistoryNew", content)
        self.assertIn("history-search-id", content)
        self.assertIn(f"/calling-companies/{company_id}/history", content)

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
        self.assertIn("Режим «Заменить выбранный раздел» временно отключён", content)


    def test_phone_import_requirements_and_csv_template(self):
        self.request("/routes")
        captured, content = self.request("/admin/import")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn('id="phone-import-requirements"', content)
        self.assertIn("Требования к файлу", content)
        self.assertIn("Скачать шаблон CSV", content)
        self.assertIn("Номер, Страна, Провайдер, Проект, Назначение, Итоговый статус", content)
        self.assertIn("Колонка «АП» игнорируется", content)
        self.assertIn("АП в EUR» импортируется в «Абонплата»", content)

        captured, template = self.request("/admin/import/template?entity=phone_numbers")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(("Content-Type", "text/csv; charset=utf-8"), captured["headers"])
        self.assertIn(("Content-Disposition", "attachment; filename=phone_numbers_import_template.csv"), captured["headers"])
        self.assertTrue(template.startswith("\ufeffНомер,Страна,Провайдер,Проект,Назначение,Итоговый статус,АП,АП в EUR,Тариф,Комментарий,Создал,Создано"))
        self.assertIn("Используется", template)
        self.assertIn("Отключен", template)
        self.assertIn("???", template)

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
        captured, content = self.request("/admin/dictionaries?section=projects")
        self.assertEqual(captured["status"], "200 OK")
        for expected in ("Меж.деп.", "REP", "ИТМ", "Предоплата", "Юр.деп."):
            self.assertIn(expected, content)
        self.assertLess(content.index("Меж.деп."), content.index("REP"))
        self.assertLess(content.index("REP"), content.index("ИТМ"))
        captured, content = self.request("/admin/dictionaries?section=phone-assignments")
        self.assertEqual(captured["status"], "200 OK")
        for expected in ("ГЛ", "АОН", "Scratchcards", "Competitors", "SMS", "Корп.телефония", "Дожим", "IVR"):
            self.assertIn(expected, content)
        for obsolete in ("SIM-карта", "Входящая линия", "Горячая линия", "Другое", "Номер из пула"):
            self.assertNotIn(obsolete, content)
        self.assertLess(content.index("ГЛ"), content.index("АОН"))
        self.assertLess(content.index("АОН"), content.index("Scratchcards"))
        self.request("/admin/dictionaries/projects/create", method="POST", body=urlencode({"name": "NewProject", "comment": "Project comment"}))
        self.request("/admin/dictionaries/phone-assignments/create", method="POST", body=urlencode({"name": "Мониторинг", "code": "monitoring", "comment": "Assignment comment"}))
        captured, content = self.request("/phones")
        self.assertEqual(captured["status"], "200 OK")
        for expected in ("Меж.деп.", "REP", "ИТМ", "Предоплата", "Юр.деп."):
            self.assertIn(expected, content)
        for expected in ("ГЛ", "АОН", "Scratchcards", "Competitors", "SMS", "Корп.телефония", "Дожим", "IVR"):
            self.assertIn(expected, content)
        self.assertNotIn(">Номер из пула</option>", content)
        self.assertNotIn(">Другое</option>", content)
        self.assertIn("NewProject", content)
        self.assertIn("Мониторинг", content)
        self.assertIn("Дата создания", content)
        self.assertIn("Дата отключения", content)

        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Меж.деп.", content)

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
        self.assertIn("<th data-col='geo' title='GEO'>GEO</th><th data-col='current_priority' title='Текущий приоритет'>Текущий приоритет</th><th data-col='previous_priority' title='Предыдущий приоритет'>Предыдущий приоритет</th>", content)
        self.assertNotIn("data-col='overflow'", content)
        self.assertNotIn("<th data-col='actions' title='Действия'>Действия</th>", content)
        self.assertIn("Нет настроенных приоритетов", content)
        eu3_block = content.split("Сервер: EU3", 1)[1].split("</section>", 1)[0]
        self.assertIn("Нет настроенных приоритетов", eu3_block)
        eu1_block = content.split("Сервер: EU1", 1)[1].split("</section>", 1)[0]
        self.assertIn("<td data-col='geo'>Мексика</td><td data-col='current_priority'>Мексика/Miatel/Demo_A@<br><span class='muted'>Перелив: —</span></td><td data-col='previous_priority'>—<br><span class='muted'>Перелив: —</span></td>", eu1_block)
        self.assertNotIn("<summary>Редактировать</summary>", eu1_block)
        self.assertNotIn("name='current_route_id'", eu1_block)
        self.assertNotIn("Сохранить текущий маршрут", eu1_block)
        self.assertNotIn("/admin/server-priorities/1/update", eu1_block)

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
        self.assertIn("<td data-col='geo'>Мексика</td><td data-col='current_priority'>Мексика/Miatel/Demo_A@<br><span class='muted'>Перелив: —</span></td>", eu1_block)

    def test_server_priority_direct_update_is_not_allowed(self):
        self.request("/routes")
        body = urlencode({"current_route_id": "1", "comment": "manual admin update"})
        captured, content = self.request("/admin/server-priorities/1/update", method="POST", body=body)
        self.assertEqual(captured["status"], "403 Forbidden")
        self.assertIn("Нет доступа", content)
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT current_route_id, previous_route_id, comment FROM server_route_priorities WHERE id = 1").fetchone()
            self.assertEqual(row["current_route_id"], 2)
            self.assertIsNone(row["previous_route_id"])
            self.assertEqual(row["comment"], "Demo initial priority")
            event_count = conn.execute("""
                SELECT COUNT(*) FROM change_log
                WHERE entity_type = 'server_route_priority'
                  AND entity_id = 1
                  AND change_type = 'server_route_priority.current_route_updated'
            """).fetchone()[0]
            self.assertEqual(event_count, 0)
        finally:
            conn.close()

    def test_server_priority_direct_create_comment_deactivate_and_delete_are_not_allowed(self):
        self.request("/routes")
        for path in (
            "/admin/server-priorities/create",
            "/admin/server-priorities/1/comment",
            "/admin/server-priorities/1/deactivate",
            "/admin/server-priorities/1/delete",
        ):
            with self.subTest(path=path):
                captured, content = self.request(path, method="POST", body=urlencode({"comment": "blocked", "current_route_id": "1"}))
                self.assertEqual(captured["status"], "403 Forbidden")
                self.assertIn("Нет доступа", content)



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
        self.assertIn("Схема маршрутизации кампаний показывает текущие исключения", content)
        self.assertIn('name="company_id_external"', content)
        self.assertIn('name="routing_mode"', content)
        self.assertIn('name="show_history"', content)
        self.assertNotIn("+ Добавить схему маршрутизации кампании", content)
        self.assertNotIn('name="calling_company_id"', content)
        self.assertNotIn("syncAutorotation", content)
        self.assertNotIn("Действия", content)
        self.assertNotIn("Редактировать комментарий", content)
        self.assertNotIn("/admin/company-routing-settings/1/deactivate", content)
        self.assertNotIn("/admin/company-routing-settings/1/delete", content)

    def test_company_routing_settings_info_icon_and_history_page(self):
        self.request("/routes")
        body = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T12:00", "calling_company_id": "2", "company_change_type": "enable_autorotation", "reason": "Тест нового маршрута", "comment": "1111111"})
        captured, _ = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        route_id = ""
        conn = server.connect(server.DB_PATH)
        try:
            route_id = str(conn.execute("SELECT id FROM routes WHERE name = 'Мексика/Sancom/Demo_0827@'").fetchone()[0])
        finally:
            conn.close()
        body = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T13:00", "calling_company_id": "2", "company_change_type": "set_campaign_route", "campaign_provider_id": "1", "new_company_route_id": route_id, "reason": "Тест нового маршрута", "comment": "2222222"})
        captured, _ = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")

        captured, content = self.request("/admin/company-routing-settings")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("/campaign-routing/", content)
        self.assertIn("aria-label='История'", content)
        self.assertIn("2222222", content)

        conn = server.connect(server.DB_PATH)
        try:
            setting_id = conn.execute("SELECT id FROM company_routing_settings WHERE calling_company_id = 2 AND is_active = 1 AND valid_to IS NULL").fetchone()[0]
        finally:
            conn.close()
        captured, history = self.request(f"/campaign-routing/{setting_id}/history")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("История маршрутизации кампании", history)
        self.assertIn("Current routing mode", history)
        self.assertIn("Включили авторотацию", history)
        self.assertIn("Прописали ручной маршрут", history)
        self.assertIn("1111111", history)
        self.assertIn("2222222", history)
        self.assertIn("Авторотация: Нет → Да", history)

    def test_company_routing_setting_create_endpoint_blocked_and_filters_render(self):
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
        captured, content = self.request("/admin/company-routing-settings/create", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("создание выполняется через", content)

        company_body = urlencode({
            "server_id": "1",
            "country_id": "1",
            "company_id_external": "1235",
            "company_name": "CC Mexico Filter Demo",
            "line_count": "1",
            "dial_set_count": "1",
            "retry_interval_seconds": "30",
            "has_autorotation": "1",
            "is_active": "1",
            "comment": "autorotation filter mapping",
        })
        self.request("/companies/create", method="POST", body=company_body)
        captured, content = self.request("/admin/company-routing-settings?country_id=1&server_id=1&routing_mode=autorotation&company_id_external=1235&is_active=1")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("CC Mexico Filter Demo", content)
        self.assertIn("1235", content)
        self.assertIn("Авторотация", content)
        captured, content = self.request("/admin/company-routing-settings?company_id_external=no-match")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("CC Mexico Filter Demo", content)

    def test_company_routing_settings_table_maps_autorotation_company_columns(self):
        self.request("/routes")
        body = urlencode({
            "server_id": "1",
            "country_id": "1",
            "company_id_external": "1234",
            "company_name": "CC Mexico Demo 22",
            "line_count": "1",
            "dial_set_count": "1",
            "retry_interval_seconds": "30",
            "has_autorotation": "1",
            "is_active": "1",
            "comment": "autorotation table mapping",
        })
        captured, _ = self.request("/companies/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")

        captured, content = self.request("/admin/company-routing-settings?company_id_external=1234")
        self.assertEqual(captured["status"], "200 OK")
        rows = re.findall(r"<tr>.*?</tr>", content, flags=re.S)
        row_html = next(row for row in rows if "CC Mexico Demo 22" in row)

        expected_cells = {
            "server": "EU1",
            "geo": "Мексика",
            "company_id": "1234",
            "company_name": "CC Mexico Demo 22",
            "routing_mode": "Авторотация",
            "autorotation": "Да",
            "route": "—",
            "active": "Да",
            "comment": "Начальная авторотация при создании кампании",
        }
        for data_col, expected in expected_cells.items():
            with self.subTest(data_col=data_col):
                self.assertIn(f"<td data-col='{data_col}'>{expected}</td>", row_html)
        self.assertNotIn("<td data-col='route'>Да</td>", row_html)
        self.assertNotIn("<td data-col='route'>Нет</td>", row_html)

    def test_company_routing_settings_page_has_no_edit_create_or_deactivate_actions(self):
        self.request("/routes")
        company_body = urlencode({
            "server_id": "1",
            "country_id": "1",
            "company_id_external": "1236",
            "company_name": "CC Readonly Demo",
            "line_count": "1",
            "dial_set_count": "1",
            "retry_interval_seconds": "30",
            "has_autorotation": "1",
            "is_active": "1",
            "comment": "readonly mapping",
        })
        self.request("/companies/create", method="POST", body=company_body)

        captured, content = self.request("/admin/company-routing-settings")
        self.assertEqual(captured["status"], "200 OK")
        self.assertNotIn("/admin/company-routing-settings/create", content)
        self.assertNotIn("/admin/company-routing-settings/1/update", content)
        self.assertNotIn("/admin/company-routing-settings/1/deactivate", content)
        self.assertNotIn("Деактивировать схему маршрутизации", content)
        self.assertNotIn("Редактировать комментарий", content)
        self.assertNotIn("<th data-col='actions'>Действия</th>", content)
        self.assertNotIn("<td data-col='actions'", content)
        self.assertNotIn("<select name='routing_mode'", content)
        self.assertNotIn("name='has_autorotation' value='1'", content)
        self.assertNotIn("name='is_active' value='1'", content)

    def test_company_routing_settings_update_ignores_malicious_business_fields(self):
        self.request("/routes")
        self.request("/companies/create", method="POST", body=urlencode({"server_id": "1", "country_id": "1", "company_id_external": "1237", "company_name": "CC Update Block Demo", "line_count": "1", "dial_set_count": "1", "retry_interval_seconds": "30", "has_autorotation": "1", "is_active": "1", "comment": "old routing state"}))
        update_body = urlencode({
            "country_id": "2",
            "server_id": "2",
            "routing_mode": "autorotation",
            "route_id": "1",
            "has_autorotation": "1",
            "is_active": "0",
            "comment": "new routing state",
        })
        captured, content = self.request("/admin/company-routing-settings/1/update", method="POST", body=update_body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("изменения выполняются через", content)

        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT * FROM company_routing_settings WHERE id = 1").fetchone()
            self.assertEqual(row["comment"], "Начальная авторотация при создании кампании")
            self.assertEqual(row["country_id"], 1)
            self.assertEqual(row["server_id"], 1)
            self.assertEqual(row["routing_mode"], "autorotation")
            self.assertEqual(row["has_autorotation"], 1)
            self.assertEqual(row["is_active"], 1)
            self.assertIsNone(row["valid_to"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM company_routing_settings").fetchone()[0], 1)
        finally:
            conn.close()

    def test_company_routing_settings_direct_deactivate_is_not_allowed(self):
        self.request("/routes")
        self.request("/companies/create", method="POST", body=urlencode({"server_id": "1", "country_id": "1", "company_id_external": "1238", "company_name": "CC Deactivate Block Demo", "line_count": "1", "dial_set_count": "1", "retry_interval_seconds": "30", "has_autorotation": "1", "is_active": "1", "comment": "routing state"}))
        captured, content = self.request("/admin/company-routing-settings/1/deactivate", method="POST", body=urlencode({}))
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("деактивация и удаление выполняются через", content)
        conn = server.connect(server.DB_PATH)
        try:
            row = conn.execute("SELECT is_active, valid_to FROM company_routing_settings WHERE id = 1").fetchone()
            self.assertEqual(row["is_active"], 1)
            self.assertIsNone(row["valid_to"])
        finally:
            conn.close()

    def test_company_routing_settings_direct_delete_is_not_allowed(self):
        self.request("/routes")
        self.request("/companies/create", method="POST", body=urlencode({"server_id": "1", "country_id": "1", "company_id_external": "1239", "company_name": "CC Delete Block Demo", "line_count": "1", "dial_set_count": "1", "retry_interval_seconds": "30", "has_autorotation": "1", "is_active": "1", "comment": "routing state"}))
        captured, content = self.request("/admin/company-routing-settings/1/delete", method="POST", body=urlencode({}))
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("деактивация и удаление выполняются через", content)
        conn = server.connect(server.DB_PATH)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM company_routing_settings WHERE id = 1 AND is_active = 1 AND valid_to IS NULL").fetchone()[0], 1)
        finally:
            conn.close()


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
            "reason": "Задача руководства",
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
            ("reason", "Задача руководства"),
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
            ("reason", "Задача руководства"),
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
            ("reason", "Задача руководства"),
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

    def _insert_routing_event(self, event_at, comment, *, country_id=1, apply_scope="none", server_id=None):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            conn.execute(
                """
                INSERT INTO routing_events(
                    event_at, apply_scope, reason, country_id, server_id, provider_id,
                    comment, snapshot_json, created_by, updated_by
                ) VALUES (?, ?, 'Другое', ?, ?, 1, ?, '{}', ?, ?)
                """,
                (event_at, apply_scope, country_id, server_id, comment, server.ADMIN_ID, server.ADMIN_ID),
            )
            conn.commit()
        finally:
            conn.close()

    def _provider_changes_journal_html(self):
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        return content.split("<h2>Журнал событий</h2>", 1)[1].split("<div class='table-footer'>", 1)[0]

    def test_provider_changes_journal_shows_server_priority_comment(self):
        self._insert_routing_event("2026-06-22 10:00:00", "server priority journal comment", apply_scope="server_priority", server_id=1)
        journal = self._provider_changes_journal_html()
        self.assertIn("data-col='comment'><span class='cell-clamp'>server priority journal comment</span>", journal)

    def test_provider_changes_journal_shows_campaign_settings_comment(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            conn.execute(
                """
                INSERT INTO routing_events(
                    event_at, apply_scope, reason, country_id, provider_id, calling_company_id,
                    comment, snapshot_json, created_by, updated_by
                ) VALUES (?, 'campaign_setting', 'Другое', 1, 1, 1, ?, '{}', ?, ?)
                """,
                ("2026-06-22 10:05:00", "campaign settings journal comment", server.ADMIN_ID, server.ADMIN_ID),
            )
            conn.commit()
        finally:
            conn.close()
        journal = self._provider_changes_journal_html()
        self.assertIn("data-col='comment'><span class='cell-clamp'>campaign settings journal comment</span>", journal)

    def test_provider_changes_journal_shows_no_system_change_comment(self):
        self._insert_routing_event("2026-06-22 10:10:00", "no system change journal comment", apply_scope="none")
        journal = self._provider_changes_journal_html()
        self.assertIn("data-col='comment'><span class='cell-clamp'>no system change journal comment</span>", journal)

    def test_provider_changes_journal_shows_empty_comment_dash(self):
        self._insert_routing_event("2026-06-22 10:15:00", "", apply_scope="none")
        journal = self._provider_changes_journal_html()
        self.assertIn("data-col='comment'><span class='cell-clamp'>—</span>", journal)

    def test_provider_changes_journal_supports_date_from_filter(self):
        self._insert_routing_event("2026-06-21 23:59:59", "before date from")
        self._insert_routing_event("2026-06-22 00:00:00", "on date from")
        captured, content = self.request("/provider-changes?date_from=2026-06-22")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Дата от", content)
        self.assertIn("value='2026-06-22'", content)
        self.assertIn("on date from", content)
        self.assertNotIn("before date from", content)

    def test_provider_changes_journal_supports_date_to_filter(self):
        self._insert_routing_event("2026-06-22 23:59:59", "on date to")
        self._insert_routing_event("2026-06-23 00:00:00", "after date to")
        captured, content = self.request("/provider-changes?date_to=2026-06-22")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("on date to", content)
        self.assertNotIn("after date to", content)

    def test_provider_changes_journal_supports_exact_inclusive_date_range(self):
        self._insert_routing_event("2026-06-21 23:59:59", "previous day exact")
        self._insert_routing_event("2026-06-22 00:00:00", "start of exact day")
        self._insert_routing_event("2026-06-22 23:59:59", "end of exact day")
        self._insert_routing_event("2026-06-23 00:00:00", "next day exact")
        captured, content = self.request("/provider-changes?date_from=2026-06-22&date_to=2026-06-22")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("start of exact day", content)
        self.assertIn("end of exact day", content)
        self.assertNotIn("previous day exact", content)
        self.assertNotIn("next day exact", content)

    def test_provider_changes_invalid_date_range_shows_validation_error(self):
        captured, content = self.request("/provider-changes?date_from=2026-06-25&date_to=2026-06-22")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Дата от не может быть позже даты до", content)

    def test_provider_changes_date_filters_combine_with_geo_scope_and_server_filters(self):
        self._insert_routing_event("2026-06-22 12:00:00", "matching geo scope server", apply_scope="server_priority", server_id=1)
        self._insert_routing_event("2026-06-22 12:00:00", "wrong scope", apply_scope="none", server_id=1)
        self._insert_routing_event("2026-06-22 12:00:00", "wrong server", apply_scope="server_priority", server_id=2)
        conn = server.connect(server.DB_PATH)
        try:
            other_country_id = conn.execute("INSERT INTO countries(name, code) VALUES ('Испания', 'ES')").lastrowid
            conn.execute(
                """
                INSERT INTO routing_events(event_at, apply_scope, reason, country_id, server_id, provider_id, comment, snapshot_json, created_by, updated_by)
                VALUES ('2026-06-22 12:00:00', 'server_priority', 'Другое', ?, 1, 1, 'wrong geo', '{}', ?, ?)
                """,
                (other_country_id, server.ADMIN_ID, server.ADMIN_ID),
            )
            conn.commit()
        finally:
            conn.close()
        captured, content = self.request("/provider-changes?date_from=2026-06-22&date_to=2026-06-22&country_id=1&apply_scope=server_priority&server_id=1")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("matching geo scope server", content)
        self.assertNotIn("wrong scope", content)
        self.assertNotIn("wrong server", content)
        self.assertNotIn("wrong geo", content)

    def test_provider_changes_reset_clears_date_fields(self):
        captured, content = self.request("/provider-changes?reset_filters=1")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("name='date_from' value=''", content)
        self.assertIn("name='date_to' value=''", content)

    def test_provider_changes_csv_export_respects_date_range_filter(self):
        self._insert_routing_event("2026-06-22 12:00:00", "export included by date")
        self._insert_routing_event("2026-06-23 12:00:00", "export excluded by date")
        captured, content = self.request("/provider-changes?date_from=2026-06-22&date_to=2026-06-22&export=csv")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("export included by date", content)
        self.assertNotIn("export excluded by date", content)

    def test_provider_changes_none_and_campaign_setting_forms_still_render(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("value='none' checked", content)
        self.assertIn("data-scopes='none'", content)
        self.assertIn("Настройка кампании", content)
        self.assertIn("data-scopes='campaign_setting'", content)
        self.assertIn("Событие будет сохранено в журнале и применено", content)


    def test_provider_change_company_setting_form_renders_campaign_helper_filters(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        create_form = content.split("<form method='post' action='/provider-changes/create'", 1)[1].split("</form>", 1)[0]
        self.assertIn("id='campaign-server-filter'", create_form)
        self.assertIn("Сервер <select name='server_id' id='campaign-server-filter'", create_form)
        self.assertIn("ID кампании", create_form)
        self.assertIn("id='campaign-id-search'", create_form)
        self.assertIn("id='campaign-id-search-button'", create_form)
        self.assertLess(create_form.index("id='campaign-server-filter'"), create_form.index("id='event-company'"))

    def test_provider_change_company_dropdown_has_server_filter_metadata(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        create_form = content.split("<form method='post' action='/provider-changes/create'", 1)[1].split("</form>", 1)[0]
        self.assertIn("data-server-id=", create_form)
        self.assertIn("data-campaign-id=", create_form)
        self.assertIn("function filterCompanyOptions()", content)
        self.assertIn("!selectedServerId || String(option.dataset.serverId) === String(selectedServerId)", content)
        self.assertIn("Кампания с таким ID не найдена", content)
        self.assertIn("находится на сервере", content)

    def test_provider_change_campaign_id_search_post_selects_external_id_company(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            eu1_id = conn.execute("SELECT id FROM servers WHERE name = 'EU1'").fetchone()["id"]
            country_id = conn.execute("SELECT id FROM countries WHERE name = 'Мексика'").fetchone()["id"]
            repo = server.Repository(conn)
            repo.create_calling_company(server_id=eu1_id, country_id=country_id, company_name="Search Select Demo", company_id_external="search-select", has_autorotation=False, created_by=server.ADMIN_ID, is_active=True)
        finally:
            conn.close()
        body = urlencode({
            "apply_scope": "campaign_setting",
            "event_at": "2026-06-10T12:00",
            "server_id": "",
            "campaign_id_search": "search-select",
            "company_change_type": "enable_autorotation",
            "reason": "Тест нового маршрута",
            "comment": "Нашли кампанию по видимому ID",
        })
        captured, _ = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        captured, content = self.request("/provider-changes")
        self.assertIn("search-select / Search Select Demo", content)
        self.assertIn("Нашли кампанию по видимому ID", content)

    def test_provider_change_campaign_id_search_post_ignores_internal_id(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            eu1_id = conn.execute("SELECT id FROM servers WHERE name = 'EU1'").fetchone()["id"]
            country_id = conn.execute("SELECT id FROM countries WHERE name = 'Мексика'").fetchone()["id"]
            repo = server.Repository(conn)
            company_id = repo.create_calling_company(server_id=eu1_id, country_id=country_id, company_name="Internal ID Search Guard", company_id_external="visible-guard", has_autorotation=False, created_by=server.ADMIN_ID, is_active=True)
        finally:
            conn.close()
        body = urlencode({
            "apply_scope": "campaign_setting",
            "event_at": "2026-06-10T12:00",
            "campaign_id_search": str(company_id),
            "company_change_type": "enable_autorotation",
            "reason": "Тест нового маршрута",
            "comment": "Не искать по внутреннему ID",
        })
        captured, content = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Кампания с таким ID не найдена", content)

    def test_provider_change_campaign_id_search_post_reports_server_conflict(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            eu1_id = conn.execute("SELECT id FROM servers WHERE name = 'EU1'").fetchone()["id"]
            eu3_id = conn.execute("SELECT id FROM servers WHERE name = 'EU3'").fetchone()["id"]
            country_id = conn.execute("SELECT id FROM countries WHERE name = 'Мексика'").fetchone()["id"]
            repo = server.Repository(conn)
            repo.create_calling_company(server_id=eu3_id, country_id=country_id, company_name="EU3 Conflict", company_id_external="eu3-conflict", has_autorotation=False, created_by=server.ADMIN_ID, is_active=True)
        finally:
            conn.close()
        body = urlencode({
            "apply_scope": "campaign_setting",
            "event_at": "2026-06-10T12:00",
            "server_id": str(eu1_id),
            "campaign_id_search": "eu3-conflict",
            "company_change_type": "enable_autorotation",
            "reason": "Тест нового маршрута",
            "comment": "Конфликт сервера",
        })
        captured, content = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Кампания с ID eu3-conflict находится на сервере EU3, а выбран сервер EU1", content)

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


    def test_campaign_setting_form_renders_checkbox_multi_select(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        create_form = content.split("<form method='post' action='/provider-changes/create'", 1)[1].split("</form>", 1)[0]
        self.assertIn("class='multi-select' id='event-company'", create_form)
        self.assertIn("name='calling_company_ids'", create_form)
        self.assertIn("Выбрать все найденные", create_form)
        self.assertIn("Отменить выбранные", create_form)
        self.assertIn("id='campaign-clear-selected'", create_form)

    def test_campaign_setting_form_clear_selected_and_close_dropdown_scripts_render(self):
        self.request("/routes")
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        create_form = content.split("<form method='post' action='/provider-changes/create'", 1)[1].split("</form>", 1)[0]
        self.assertIn("const clearSelected = document.getElementById('campaign-clear-selected')", content)
        self.assertIn("input[name=\"calling_company_ids\"]:checked", content)
        self.assertIn("campaignDropdown.open && !campaignDropdown.contains(event.target)", content)
        self.assertIn("event.key === 'Enter' || event.key === 'Escape'", content)
        self.assertIn("event.preventDefault()", content)

    def test_bulk_campaign_autorotation_creates_event_per_changed_campaign_and_skips_noop(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            repo = server.Repository(conn)
            admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
            country_id = conn.execute("SELECT id FROM countries ORDER BY id LIMIT 1").fetchone()["id"]
            server_id = conn.execute("SELECT id FROM servers ORDER BY id LIMIT 1").fetchone()["id"]
            changed_one = repo.create_calling_company(server_id=server_id, country_id=country_id, company_name="Bulk One", company_id_external="9101", has_autorotation=False, created_by=admin_id)
            changed_two = repo.create_calling_company(server_id=server_id, country_id=country_id, company_name="Bulk Two", company_id_external="9102", has_autorotation=False, created_by=admin_id)
            noop = repo.create_calling_company(server_id=server_id, country_id=country_id, company_name="Bulk Noop", company_id_external="9103", has_autorotation=True, created_by=admin_id)
        finally:
            conn.close()
        body = urlencode([
            ("apply_scope", "campaign_setting"),
            ("event_at", "2026-06-10T12:00"),
            ("calling_company_ids", str(changed_one)),
            ("calling_company_ids", str(changed_two)),
            ("calling_company_ids", str(noop)),
            ("company_change_type", "enable_autorotation"),
            ("reason", "Тест нового маршрута"),
            ("comment", "bulk enable"),
        ])
        captured, _ = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")
        location = dict(captured["headers"])["Location"]
        self.assertIn("notice=", location)
        conn = server.connect(server.DB_PATH)
        try:
            rows = conn.execute("SELECT calling_company_id FROM routing_events WHERE apply_scope = 'campaign_setting' AND comment = ? ORDER BY calling_company_id", ("bulk enable",)).fetchall()
            self.assertEqual([row["calling_company_id"] for row in rows], [changed_one, changed_two])
            active_count = conn.execute("SELECT COUNT(*) FROM company_routing_settings WHERE calling_company_id IN (?, ?) AND is_active = 1 AND valid_to IS NULL AND has_autorotation = 1", (changed_one, changed_two)).fetchone()[0]
            self.assertEqual(active_count, 2)
        finally:
            conn.close()

    def test_bulk_campaign_setting_requires_at_least_one_campaign(self):
        self.request("/routes")
        body = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T12:00", "company_change_type": "enable_autorotation", "reason": "Тест нового маршрута", "comment": "empty"})
        captured, content = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Выберите хотя бы одну кампанию", content)

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


    def test_duplicate_campaign_autorotation_error_stays_on_form_and_preserves_values(self):
        self.request("/routes")
        body = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T12:00", "calling_company_id": "2", "company_change_type": "enable_autorotation", "reason": "Тест нового маршрута", "comment": "Повторное включение"})
        captured, _ = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")

        captured, content = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("В этой компании уже включена авторотация.", content)
        self.assertIn("<form method='post' action='/provider-changes/create'", content)
        self.assertIn("value='campaign_setting' checked", content)
        self.assertIn("value='enable_autorotation' selected", content)
        self.assertIn("Повторное включение", content)
        self.assertNotIn("Вернуться и исправить", content)

    def test_campaign_setting_autorotation_change_updates_company_list_and_company_journals(self):
        self.request("/routes")
        body = urlencode({"server_id": "1", "country_id": "1", "company_name": "Journal Auto", "company_id_external": "journal-auto", "line_count": "1", "dial_set_count": "1", "retry_interval_seconds": "30", "has_autorotation": "0", "is_active": "1", "comment": ""})
        self.request("/companies/create", method="POST", body=body)
        conn = server.connect(server.DB_PATH)
        try:
            company_id = conn.execute("SELECT id FROM calling_companies WHERE company_id_external = 'journal-auto'").fetchone()["id"]
        finally:
            conn.close()

        body = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T12:00", "calling_company_id": str(company_id), "company_change_type": "enable_autorotation", "reason": "Тест нового маршрута", "comment": "Журнальное включение авторотации"})
        captured, _ = self.request("/provider-changes/create", method="POST", body=body)
        self.assertEqual(captured["status"], "303 See Other")

        captured, content = self.request("/companies?has_autorotation=1")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("journal-auto", content)
        self.assertIn("<td data-col='autorotation'>Да</td>", content)
        captured, history = self.request(f"/calling-companies/{company_id}/history")
        self.assertIn("Журнальное включение авторотации", history)
        self.assertIn("Авторотация: Нет → Да", history)
        captured, journal = self.request("/calling-companies/history?search=Журнальное")
        self.assertIn("journal-auto", journal)
        self.assertIn("Журнальное включение авторотации", journal)

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

    def provider_changes_csv_rows(self):
        captured, content = self.request("/provider-changes?export=csv")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(("Content-Type", "text/csv; charset=utf-8"), captured["headers"])
        return list(csv.DictReader(io.StringIO(content.lstrip("\ufeff")), delimiter=";"))


    def test_provider_changes_journal_does_not_render_active_column(self):
        captured, content = self.request("/provider-changes")

        self.assertEqual(captured["status"], "200 OK")
        journal = content.split("<h2>Журнал событий</h2>", 1)[1].split("<div class='table-footer'>", 1)[0]
        self.assertNotIn("Активна", journal)
        self.assertNotIn("data-col='active'", journal)

    def test_provider_changes_csv_export_includes_details_column_and_no_actions(self):
        self.request("/routes")
        body = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T12:00", "calling_company_id": "2", "company_change_type": "enable_autorotation", "reason": "Тест нового маршрута", "comment": "тест на коммент"})
        self.request("/provider-changes/create", method="POST", body=body)

        rows = self.provider_changes_csv_rows()

        self.assertEqual(["Дата события", "Область применения", "GEO", "Сервер", "Кампания", "Детали", "Причина", "Комментарий", "Пользователь / Автор"], list(rows[0].keys()))
        self.assertNotIn("Активна", rows[0].keys())
        self.assertNotIn("Статус", rows[0].keys())
        self.assertEqual("тест на коммент", rows[0]["Комментарий"])
        self.assertIn("Включили авторотацию", rows[0]["Детали"])
        self.assertIn("Авторотация: Нет → Да", rows[0]["Детали"])
        self.assertNotIn("/provider-changes/", rows[0]["Детали"])
        self.assertNotIn("Редактировать", rows[0]["Детали"])
        self.assertNotIn("<", rows[0]["Детали"])
        self.assertNotIn(">", rows[0]["Детали"])

    def test_provider_changes_csv_export_includes_autorotation_disabled_details(self):
        self.request("/routes")
        enable = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T12:00", "calling_company_id": "2", "company_change_type": "enable_autorotation", "reason": "Тест нового маршрута", "comment": "enable"})
        disable = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T13:00", "calling_company_id": "2", "company_change_type": "disable_autorotation", "reason": "Тест нового маршрута", "comment": "disable"})
        self.request("/provider-changes/create", method="POST", body=enable)
        self.request("/provider-changes/create", method="POST", body=disable)

        rows = self.provider_changes_csv_rows()

        self.assertIn("Выключили авторотацию", rows[0]["Детали"])
        self.assertIn("Авторотация: Да → Нет", rows[0]["Детали"])

    def test_provider_changes_csv_export_includes_manual_route_details(self):
        self.request("/routes")
        conn = server.connect(server.DB_PATH)
        try:
            route_id = conn.execute("SELECT id FROM routes WHERE name = 'Мексика/Sancom/Demo_0827@'").fetchone()[0]
        finally:
            conn.close()
        body = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T12:00", "calling_company_id": "2", "company_change_type": "set_campaign_route", "new_company_route_id": str(route_id), "reason": "Тест нового маршрута", "comment": "manual"})
        self.request("/provider-changes/create", method="POST", body=body)

        rows = self.provider_changes_csv_rows()

        self.assertIn("Прописали ручной маршрут", rows[0]["Детали"])
        self.assertIn("Маршрут: — → Sancom / Мексика/Sancom/Demo_0827@", rows[0]["Детали"])

    def test_provider_changes_csv_export_includes_server_priority_details_without_html(self):
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
            ("reason", "Задача руководства"),
            ("comment", "server details"),
        ])
        self.request("/provider-changes/create", method="POST", body=body)

        rows = self.provider_changes_csv_rows()

        self.assertIn("Серверы:", rows[0]["Детали"])
        self.assertIn("EU1", rows[0]["Детали"])
        self.assertIn("EU2", rows[0]["Детали"])
        self.assertIn("EU3", rows[0]["Детали"])
        self.assertIn("Miatel / Мексика/Miatel/Demo_A@ → Sancom / Мексика/Sancom/Demo_0827@", rows[0]["Детали"])
        self.assertIn("пропущено: уже был выбран этот маршрут", rows[0]["Детали"])
        self.assertNotIn("<ul", rows[0]["Детали"])
        self.assertNotIn("<li", rows[0]["Детали"])

    def test_event_list_sorted_by_event_at_desc_and_does_not_render_deactivate(self):
        self.request("/routes")
        first = urlencode({"apply_scope": "none", "event_at": "2026-06-09T10:00", "provider_id": "1", "reason": "Другое", "comment": "старое событие"})
        second = urlencode({"apply_scope": "none", "event_at": "2026-06-10T10:00", "provider_id": "1", "reason": "Другое", "comment": "новое событие"})
        self.request("/provider-changes/create", method="POST", body=first)
        self.request("/provider-changes/create", method="POST", body=second)
        captured, content = self.request("/provider-changes")
        self.assertEqual(captured["status"], "200 OK")
        self.assertLess(content.index("новое событие"), content.index("старое событие"))
        self.assertNotIn("<summary>Деактивировать</summary>", content)
        self.assertNotIn("action='/provider-changes/1/deactivate'", content)
        captured, content = self.request("/provider-changes/1/deactivate", method="POST", body=urlencode({"deactivation_reason": "архив"}))
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("нельзя деактивировать", content)
        _, content = self.request("/provider-changes")
        self.assertIn("старое событие", content)

    def test_provider_change_edit_form_allows_comment_only_and_does_not_reapply(self):
        self.request("/routes")
        body = urlencode({"apply_scope": "campaign_setting", "event_at": "2026-06-10T12:00", "calling_company_id": "2", "company_change_type": "enable_autorotation", "reason": "Тест нового маршрута", "comment": "исходный комментарий"})
        self.request("/provider-changes/create", method="POST", body=body)

        captured, content = self.request("/provider-changes/1/edit")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Редактировать комментарий", content)
        self.assertIn("name='comment'", content)
        self.assertNotIn("name='company_change_type'", content)
        self.assertNotIn("name='calling_company_id'", content)
        self.assertNotIn("name='new_company_route_id'", content)
        self.assertNotIn("name='apply_scope'", content)
        self.assertIn("Включили авторотацию", content)

        captured, _ = self.request("/provider-changes/1/update", method="POST", body=urlencode({
            "comment": "только новый комментарий",
            "company_change_type": "disable_autorotation",
            "calling_company_id": "1",
            "new_company_route_id": "1",
        }))
        self.assertEqual(captured["status"], "303 See Other")
        conn = server.connect(server.DB_PATH)
        try:
            event = conn.execute("SELECT * FROM routing_events WHERE id = 1").fetchone()
            self.assertEqual(event["comment"], "только новый комментарий")
            self.assertEqual(event["company_change_type"], "enable_autorotation")
            self.assertEqual(event["calling_company_id"], 2)
            setting = conn.execute("SELECT * FROM company_routing_settings WHERE calling_company_id = 2 AND is_active = 1 AND valid_to IS NULL").fetchone()
            self.assertEqual(setting["routing_mode"], "autorotation")
            self.assertEqual(setting["has_autorotation"], 1)
            self.assertEqual(setting["comment"], "только новый комментарий")
            routing_log_count = conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'company_routing_setting'").fetchone()[0]
            self.assertEqual(routing_log_count, 1)
        finally:
            conn.close()

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
        self.assertTrue(content.startswith("\ufeffGEO;Провайдер;Маршрут;АОН/пул;Сервер;Активен;Комментарий"))
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
        self.request("/login")
        conn = server.connect(server.DB_PATH)
        try:
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
        finally:
            conn.close()
        return f"{server.CURRENT_USER_COOKIE}={server.sign_user_id(user_id)}"

    def test_admin_sees_all_main_navigation_and_admin_navigation(self):
        captured, content = self.request("/routes")
        self.assertEqual(captured["status"], "200 OK")
        for label in [
            "Маршруты",
            "Тарифы",
            "Купленные номера",
            "Кампании прозвона",
            "Смена провайдеров",
            "Приоритет по серверам",
            "Схема маршрутизации кампаний",
            "HLR",
            "Spam Checker",
            "Администрирование",
        ]:
            self.assertIn(label, content)
        self.assertIn("side-link-disabled", content)
        self.assertIn("title='Скоро'", content)
        self.assertIn("fact_check", content)
        self.assertIn("report", content)
        self.assertIn("href='/admin/users'", content)

    def test_operator_sees_working_sections_and_allowed_admin_navigation_only(self):
        self.grant_user_read("duty", "admin_server_priorities", "admin_company_routing_settings")
        captured, content = self.request("/routes", cookie=self.user_cookie("duty"))
        self.assertEqual(captured["status"], "200 OK")
        for label in ["Маршруты", "Тарифы", "Купленные номера", "Кампании прозвона", "Смена провайдеров"]:
            self.assertIn(label, content)
        self.assertNotIn("Администрирование</button>", content)
        self.assertIn("href='/admin/server-priorities'", content)
        self.assertIn("Приоритет по серверам", content)
        self.assertIn("href='/admin/company-routing-settings'", content)
        self.assertIn("Схема маршрутизации кампаний", content)
        self.assertNotIn("HLR", content)
        self.assertNotIn("Spam Checker", content)
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

    def _valid_tariff_body(self, price="2.5"):
        return urlencode({
            "country_id": "1",
            "provider_id": "1",
            "provider_prefix_id": "2",
            "currency_id": "1",
            "price": price,
            "priority_status": "unknown",
            "is_current": "1",
            "comment": "validation test",
        })

    def _tariff_audit_counts(self):
        conn = server.connect(server.DB_PATH)
        try:
            server.init_db(conn)
            server.ensure_seed(server.Repository(conn))
            return {
                "tariffs": conn.execute("SELECT COUNT(*) FROM tariffs").fetchone()[0],
                "history": conn.execute("SELECT COUNT(*) FROM tariff_change_history").fetchone()[0],
                "change_log": conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type = 'tariff'").fetchone()[0],
            }
        finally:
            conn.close()

    def test_tariff_create_empty_price_shows_validation_error_without_audit(self):
        before = self._tariff_audit_counts()
        captured, content = self.request("/tariffs/create", method="POST", body=self._valid_tariff_body(price=""))
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Цена обязательна", content)
        self.assertNotIn("A server error occurred", content)
        self.assertEqual(self._tariff_audit_counts(), before)

    def test_tariff_create_invalid_price_shows_validation_error_without_audit(self):
        before = self._tariff_audit_counts()
        captured, content = self.request("/tariffs/create", method="POST", body=self._valid_tariff_body(price="abc"))
        self.assertEqual(captured["status"], "400 Bad Request")
        self.assertIn("Цена должна быть числом", content)
        self.assertNotIn("A server error occurred", content)
        self.assertEqual(self._tariff_audit_counts(), before)

    def test_tariff_create_zero_or_negative_price_shows_validation_error_without_audit(self):
        for price in ("0", "-1"):
            with self.subTest(price=price):
                before = self._tariff_audit_counts()
                captured, content = self.request("/tariffs/create", method="POST", body=self._valid_tariff_body(price=price))
                self.assertEqual(captured["status"], "400 Bad Request")
                self.assertIn("Цена должна быть больше 0", content)
                self.assertNotIn("A server error occurred", content)
                self.assertEqual(self._tariff_audit_counts(), before)

    def test_tariff_create_valid_price_still_creates_tariff(self):
        before = self._tariff_audit_counts()
        captured, _ = self.request("/tariffs/create", method="POST", body=self._valid_tariff_body(price="2.5"))
        self.assertEqual(captured["status"], "303 See Other")
        after = self._tariff_audit_counts()
        self.assertEqual(after["tariffs"], before["tariffs"] + 1)
        self.assertEqual(after["history"], before["history"] + 1)
        self.assertEqual(after["change_log"], before["change_log"] + 1)
        captured, content = self.request("/tariffs")
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("2.5 EUR", content)

    def test_operator_can_post_provider_change_create(self):
        body = urlencode({
            "event_at": "2026-06-14T10:00",
            "apply_scope": "none",
            "provider_id": "1",
            "reason": "Провайдер сменил маршрут",
            "comment": "",
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

class ProviderChangeTelegramServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.conn = server.connect(self.tmp.name)
        server.init_db(self.conn)
        server.ensure_seed(server.Repository(self.conn))
        self.repo = server.Repository(self.conn)
        self.admin_id = self.conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
        server._REQUEST_CONTEXT["current_user_id"] = self.admin_id
        self.country_id = self.repo.create_country("Телеграм GEO", "TGM")
        self.currency_id = self.conn.execute("SELECT id FROM currencies WHERE code = 'EUR'").fetchone()["id"]
        self.provider_id = self.repo.create_provider("Telegram Miatel", "voip", self.currency_id)

    def tearDown(self):
        server._REQUEST_CONTEXT.clear()
        self.conn.close()
        os.unlink(self.tmp.name)

    def _create_none_scope_post(self):
        return {
            "_actor_id": str(self.admin_id),
            "event_at": "2026-06-24 21:15",
            "apply_scope": "none",
            "reason": "Провайдер сменил маршрут",
            "comment": "Перевели трафик на Miatel",
            "country_id": str(self.country_id),
            "provider_id": str(self.provider_id),
            "_raw": "",
        }

    def test_provider_change_creation_calls_telegram_when_env_config_exists(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"}, clear=True), \
             patch("app.server.notify_provider_change_created", return_value=True) as notify:
            location = server.handle_post(self.repo, "/provider-changes/create", self._create_none_scope_post())
        self.assertEqual(location, "/provider-changes")
        notify.assert_called_once()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0], 1)

    def test_provider_change_creation_does_not_make_telegram_request_when_env_missing(self):
        with patch.dict(os.environ, {}, clear=True), patch("urllib.request.urlopen") as urlopen:
            location = server.handle_post(self.repo, "/provider-changes/create", self._create_none_scope_post())
        self.assertEqual(location, "/provider-changes")
        urlopen.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0], 1)

    def test_telegram_failure_does_not_prevent_event_creation(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"}, clear=True), \
             patch("app.server.notify_provider_change_created", side_effect=RuntimeError("telegram down")):
            location = server.handle_post(self.repo, "/provider-changes/create", self._create_none_scope_post())
        self.assertEqual(location, "/provider-changes")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM routing_events").fetchone()[0], 1)

    def test_comment_edit_does_not_send_telegram_notification(self):
        event_id = self.repo.create_routing_event(
            event_at="2026-06-24 21:15", apply_scope="none", reason="Другое", comment="old",
            country_id=self.country_id, provider_id=self.provider_id, created_by=self.admin_id,
        )
        with patch("app.server.notify_provider_change_created") as notify:
            location = server.handle_post(self.repo, f"/provider-changes/{event_id}/update", {"_actor_id": str(self.admin_id), "comment": "new"})
        self.assertEqual(location, "/provider-changes")
        notify.assert_not_called()


class HlrUiStateScriptTest(unittest.TestCase):
    def _content(self):
        rows = [
            server.hlr_result_from_api_item({"original": "48789662838", "normalized": "+48789662838"}, {"error": "NONE", "live_status": "LIVE", "telephone_number_type": "MOBILE"}),
            server.hlr_result_from_api_item({"original": "48123456789", "normalized": "+48123456789"}, {"error": "NONE", "live_status": "DEAD", "telephone_number_type": "FIXED_LINE"}),
        ]
        return server.hlr_page("", rows).decode("utf-8")

    def test_hlr_rows_are_server_rendered_with_safe_filter_attributes(self):
        content = self._content()
        self.assertIn("class='hlr-result-row hlr-row-severity-green'", content)
        self.assertIn("data-hlr-status='LIVE'", content)
        self.assertIn("data-live-status='LIVE'", content)
        self.assertIn("data-final-result='OK'", content)
        self.assertIn("data-number-type='MOBILE'", content)
        self.assertIn("data-format-status='valid'", content)
        self.assertIn("data-severity='good'", content)
        self.assertIn("class='hlr-result-row hlr-row-severity-red'", content)
        self.assertIn("data-live-status='DEAD'", content)
        self.assertIn("data-number-type='FIXED_LINE'", content)

    def test_hlr_filters_are_dom_only_and_do_not_reintroduce_state_rendering(self):
        content = self._content()
        self.assertIn("id='hlr-filter-panel'", content)
        self.assertIn("id='hlr-visible-count'>Показано: 2 из 2", content)
        self.assertIn('const resultRows = table ? Array.from(table.querySelectorAll("tbody tr.hlr-result-row")) : [];', content)
        self.assertIn("const activeFilters = new Set();", content)
        self.assertIn('key: "LIVE"', content)
        self.assertIn('key: "DEAD"', content)
        self.assertIn('key: "BAD_FORMAT"', content)
        self.assertIn("row.hidden = selected.length > 0 && !selected.some", content)
        self.assertIn('visibleCount.textContent = "Показано: " + visible + " из " + resultRows.length;', content)
        self.assertNotIn("function renderTable", content)
        self.assertNotIn("rawResults", content)
        self.assertNotIn("filteredResults", content)
        self.assertNotIn("hlr-results-data", content)

    def test_hlr_right_panel_is_compact_and_separates_balance_from_config(self):
        content = self._content()
        self.assertIn("id='hlr-filter-panel'", content)
        self.assertNotIn("class='hlr-balance-card", content)
        self.assertNotIn("Баланс API:", content)
        self.assertIn("<h3 class='hlr-usage-title'>HLR usage</h3>", content)
        self.assertIn("<span class='hlr-usage-label'>Баланс API</span>", content)
        self.assertIn("id='hlr-balance-refresh-button'", content)
        self.assertIn(">refresh</span>", content)
        self.assertIn("<span class='hlr-usage-label'>Осталось сегодня</span>", content)
        self.assertIn("<span class='hlr-usage-label'>Проверено сегодня</span>", content)
        self.assertIn("<p class='hlr-input-hint hlr-usage-label'>Один номер на строке. Можно вставлять номера с пробелами, +, скобками и дефисами.</p>", content)
        self.assertIn("<span class='hlr-usage-label'>Последняя проверка</span>", content)
        self.assertIn("<summary>Справка по HLR</summary>", content)
        with patch("app.server.current_role_key", return_value="admin"):
            admin_content = self._content()
        self.assertIn("<summary>HLR config</summary>", admin_content)
        self.assertIn("<dt>balance</dt><dd>unavailable</dd>", admin_content)
        self.assertIn("<dt>balance_status</dt><dd>unavailable</dd>", admin_content)
        self.assertIn("<dt>checked_today</dt><dd>0</dd>", admin_content)
        self.assertIn("<dt>remaining_today</dt><dd>2000</dd>", admin_content)
        self.assertIn("<dt>daily_limit_source</dt><dd>fallback</dd>", admin_content)
        self.assertIn("Дневной лимит HLR", admin_content)
        self.assertIn("action='/hlr/config/daily-limit'", admin_content)
        self.assertNotIn("<button class='secondary hlr-balance-refresh' type='submit'>Обновить баланс</button>", admin_content)
        self.assertNotIn("data-hlr-help-tab", content)
        self.assertNotIn("Поля API</button>", content)
        self.assertNotIn("HLR статусы</button>", content)
        self.assertIn("<dt>api_url</dt>", admin_content)
        self.assertNotIn("<dt>Баланс API</dt>", admin_content)
        self.assertNotIn("api_secret</dd>", admin_content)

    def test_hlr_balance_refresh_keeps_config_open_and_updates_usage_dashboard(self):
        with patch("app.server.current_role_key", return_value="admin"):
            content = server.hlr_page(balance={"status": "ok", "credits": 1234.5, "updated_at": "2026-07-10 18:14", "error_message": None}).decode("utf-8")
        self.assertIn("<details class='card hlr-api-fields' id='hlr-config-details'><summary>HLR config</summary>", content)
        self.assertNotIn("<details class='card hlr-api-fields' id='hlr-config-details' open>", content)
        self.assertIn("id='hlr-usage-balance-card'", content)
        self.assertIn("<strong class='hlr-usage-value'>1234.5</strong>", content)
        self.assertIn("<dt>balance</dt><dd>1234.5</dd>", content)
        self.assertIn("wasConfigOpen", content)
        self.assertIn("replaceBalanceFragments", content)

    def test_hlr_check_submit_shows_compact_loading_state(self):
        content = self._content()
        self.assertIn("id='hlr-submit-button'", content)
        self.assertIn("id='hlr-progress'", content)
        self.assertIn("class='hlr-progress-track'", content)
        self.assertIn("flex: 1 1 320px", content)
        self.assertIn("max-width: calc(100% - 8px)", content)
        self.assertIn("height: 30px", content)
        self.assertIn("border-radius: var(--radius-small)", content)
        self.assertNotIn("hlr-progress-text", content)
        self.assertNotIn("Проверка выполняется:", content)
        self.assertIn("@keyframes hlr-progress-slide", content)
        self.assertIn('let hlrSubmitting = false;', content)
        self.assertIn('if (hlrSubmitting) {', content)
        self.assertIn('event.preventDefault();', content)
        self.assertIn('setHlrLoading(true, lines.length);', content)
        self.assertIn('requestAnimationFrame(() => {', content)
        self.assertIn('HTMLFormElement.prototype.submit.call(form);', content)
        self.assertIn('submitButton.textContent = isLoading ? "Проверяется..." : "Запустить проверку";', content)
        self.assertIn('if (clearButton) clearButton.disabled = isLoading;', content)
        self.assertIn('lines.length < 1 || lines.length > 500 || (dailyLimit > 0 && remainingToday < 1)', content)

    def test_hlr_inline_script_uses_safe_newline_escaping_and_stable_controls(self):
        content = self._content()
        self.assertIn("id='hlr-clear-button'", content)
        self.assertIn("id='hlr-columns-button'", content)
        self.assertIn("id='hlr-column-panel'", content)
        self.assertIn(".hlr-column-panel.open-up", content)
        self.assertIn("function placeColumnsPanel()", content)
        self.assertIn('const storageKey = "hlr_safe_column_settings_v2";', content)
        self.assertIn('function parseHlrInputLines()', content)
        self.assertIn('.replace(/\\r/g, "")', content)
        self.assertIn('.split("\\n")', content)
        self.assertIn('const values = parseHlrInputLines();', content)
        self.assertIn('const lines = parseHlrInputLines();', content)
        self.assertNotIn('function hlrInputLines()', content)
        self.assertNotIn('.split(/\r?\n/)', content)
        self.assertNotIn('split("\n")', content)
        self.assertIn('clearButton.addEventListener("click"', content)
        self.assertIn('input.value = "";', content)
        self.assertIn('updateCounter();', content)
        self.assertIn('input.focus();', content)
        self.assertIn('event.stopPropagation();', content)
        self.assertIn('if (input && clearButton) {', content)
        self.assertIn("id='hlr-filter-panel'", content)
        self.assertIn('const statusDefinitions = [', content)
        self.assertIn('key: "LIVE"', content)
        self.assertIn('key: "INCONCLUSIVE"', content)
        self.assertIn('function applyRowFilters()', content)
        self.assertIn('if (!table || !columnsButton || !columnsPanel || !columnsList) return;', content)

    def test_hlr_default_column_settings_use_business_columns_and_new_storage_key(self):
        content = self._content()
        expected_default = [
            "original_number",
            "normalized_number",
            "format_status",
            "country",
            "number_type",
            "operator",
            "hlr_status_raw",
            "live_status_raw",
            "final_result",
            "lead_quality_signal",
            "comment",
        ]
        self.assertIn('const storageKey = "hlr_safe_column_settings_v2";', content)
        self.assertNotIn('const storageKey = "hlr_safe_column_settings";', content)
        for key in expected_default:
            self.assertIn(f'    "{key}",', content)
        self.assertIn('visible: defaultOrder.filter((key) => defaultVisibleColumns.has(key))', content)
        self.assertIn('settings = { order: defaultOrder.slice(), visible: defaultOrder.filter((key) => defaultVisibleColumns.has(key)) };', content)


    def test_hlr_copy_source_numbers_uses_filtered_rows_and_data_attribute(self):
        content = self._content()
        self.assertIn("<span class='copyable-header'>", content)
        self.assertIn("id='hlr-copy-source-button'", content)
        self.assertIn("class='copy-column-button'", content)
        self.assertIn("title='Скопировать исходные номера'", content)
        self.assertNotIn("id='hlr-copy-source-status'", content)
        self.assertNotIn("<span>Копировать исходные номера</span>", content)
        self.assertNotIn("Копировать исходные номера текущей выборки", content)
        self.assertIn("data-source-number='48789662838'", content)
        self.assertIn('const copySourceButton = document.getElementById("hlr-copy-source-button");', content)
        self.assertIn('const values = rows.map((row) => (row.dataset.sourceNumber || "").trim()).filter(Boolean);', content)
        self.assertIn('await copyText(values.join("\\n"));', content)
        self.assertIn('copySourceButton.innerHTML = copySourceSuccessIcon;', content)
        self.assertIn('}, 1500);', content)
        self.assertNotIn('setCopySourceStatus("Нет строк для копирования", "error");', content)
        self.assertIn('copySourceButton.disabled = rows.length < 1;', content)

    def test_hlr_export_uses_status_payload_and_keeps_full_results_for_repeated_submits(self):
        content = self._content()
        self.assertIn("name='selected_statuses_json' value='[]'", content)
        self.assertIn("name='show_all_statuses' value='1'", content)
        self.assertIn('if (exportInput) exportInput.value = originalExportJson;', content)
        self.assertIn('if (exportStatusesInput) exportStatusesInput.value = JSON.stringify(Array.from(selectedStatuses));', content)
        self.assertIn('if (exportShowAllInput) exportShowAllInput.value = showAllStatuses ? "1" : "0";', content)
        self.assertIn('const rows = visibleRows();', content)
        self.assertIn('updateExportPayload(rows);', content)
        self.assertIn('if (rows.length < 1) {', content)
        self.assertIn('Нет строк для экспорта в текущей выборке.', content)
        self.assertIn('window.setTimeout(() => {', content)
        self.assertIn('if (exportButton && visibleRows().length > 0) exportButton.disabled = false;', content)

    def test_hlr_export_filter_helper_matches_current_status_selection(self):
        rows = [
            server.hlr_result_from_api_item({"original": "48789662838", "normalized": "+48789662838"}, {"error": "NONE", "live_status": "LIVE", "telephone_number_type": "MOBILE"}),
            server.hlr_result_from_api_item({"original": "48123456789", "normalized": "+48123456789"}, {"error": "NONE", "live_status": "DEAD", "telephone_number_type": "FIXED_LINE"}),
            server.hlr_result_from_api_item({"original": "bad", "normalized": ""}, {"error": "NONE", "live_status": "INCONCLUSIVE", "telephone_number_type": "BAD_FORMAT"}),
        ]
        self.assertEqual(server.hlr_filter_results_for_export(rows, [], True), rows)
        live_rows = server.hlr_filter_results_for_export(rows, ["LIVE"], False)
        self.assertEqual([server.hlr_display_status(row) for row in live_rows], ["LIVE"])
        multi_rows = server.hlr_filter_results_for_export(rows, ["LIVE", "BAD_FORMAT"], False)
        self.assertEqual([server.hlr_display_status(row) for row in multi_rows], ["LIVE", "BAD_FORMAT"])
        self.assertEqual(server.hlr_filter_results_for_export(rows, [], False), [])

    def test_hlr_export_rows_always_use_canonical_table_columns(self):
        row = server.hlr_result_from_api_item(
            {"original": "48789662838", "normalized": "+48789662838"},
            {"error": "NONE", "live_status": "LIVE", "telephone_number_type": "MOBILE", "credits_spent": 1, "uuid": "abc"},
        )
        headers, keys = server.hlr_csv_headers_and_keys([row])
        export_row = server.hlr_results_rows([row])[0]
        self.assertEqual(keys, [key for key, _label, _width in server.HLR_TABLE_COLUMNS])
        self.assertEqual(headers, [label for _key, label, _width in server.HLR_TABLE_COLUMNS])
        self.assertEqual(len(export_row), len(server.HLR_TABLE_COLUMNS))
        self.assertIn("UUID", headers)
        self.assertIn("Timestamp", headers)
        self.assertIn("Credits", headers)

    def test_hlr_column_manager_keeps_technical_columns_available_after_business_columns(self):
        content = self._content()
        self.assertIn('const businessColumnOrder = [', content)
        self.assertIn('    "original_number",\n    "normalized_number",\n    "format_status",\n    "country",\n    "number_type",\n    "operator",\n    "hlr_status_raw",\n    "live_status_raw",\n    "final_result",\n    "lead_quality_signal",\n    "comment",', content)
        self.assertIn('.concat(tableColumnOrder.filter((key) => !businessColumnOrder.includes(key)))', content)
        technical_labels = {label for _key, label, _width in server.HLR_TABLE_COLUMNS}
        for label in [
            "UUID",
            "Timestamp",
            "Credits",
            "Raw error",
            "API message",
            "Request parameters",
            "Detected number",
            "Formatted number",
            "Current network",
            "Original operator",
            "Is ported",
        ]:
            self.assertIn(label, technical_labels)
