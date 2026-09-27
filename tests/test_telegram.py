import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import app.server as server
from app.telegram import build_provider_change_message, provider_change_url, send_telegram_message


class TelegramMessageTest(unittest.TestCase):
    def build(self, event):
        with patch.dict(os.environ, {}, clear=True):
            return build_provider_change_message(event)

    def test_none_scope_is_provider_focused_and_omits_empty_optional_blocks(self):
        message = self.build({
            "apply_scope": "none", "country_name": "Мексика", "provider_name": "DemoTel",
            "affected_route_name": "Мексика/DemoTel/RND@", "reason": "Провайдер сменил маршрут",
            "comment": "", "author_name": "Admin", "event_at": "2026-09-27 20:20",
        })
        self.assertIn("📡 <b>Событие у провайдера</b>", message)
        self.assertIn("📍 <b>Мексика</b>", message)
        self.assertIn("🏢 <b>Провайдер:</b> DemoTel", message)
        self.assertIn("🛣 <b>Маршрут:</b> Мексика/DemoTel/RND@", message)
        self.assertNotIn("Смена провайдера", message)
        self.assertNotIn("Разница", message)
        self.assertNotIn("💬", message)

    def test_server_priority_has_separate_geo_servers_and_only_real_optional_blocks(self):
        message = self.build({
            "apply_scope": "server_priority", "country_name": "Чехия",
            "affected_server_names": "1234544, EU1", "old_route_name": None,
            "new_route_name": "Чехия/Sancom/Pool_A/0827pfx@", "reason": "Массовый отбой",
        })
        self.assertIn("⚙️ <b>Серверный приоритет</b>", message)
        self.assertIn("📍 <b>Чехия</b>\n🖥 <b>Серверы:</b> 1234544, EU1", message)
        self.assertIn("🛣 <b>Основной маршрут</b>\nБыло: —\nСтало: <b>Чехия/Sancom/Pool_A/0827pfx@</b>", message)
        self.assertNotIn("Перелив", message)
        self.assertNotIn("Разница", message)

    def test_server_priority_preserves_overflow_and_numeric_difference(self):
        message = self.build({"apply_scope": "server_priority", "overflow_route_name": "CZ/Overflow@", "price_delta_eur": "-0.08"})
        self.assertIn("🌊 <b>Перелив:</b> CZ/Overflow@", message)
        self.assertIn("🟢 Разница: -0.08 EUR", message)

    def test_fixed_geo_campaign_autorotation_is_action_aware(self):
        message = self.build({
            "apply_scope": "campaign_setting", "company_country_id": 7, "company_country_name": "Бразилия",
            "company_id_external": "1001", "company_name": "CC Mexico Demo 1", "company_server_name": "EU2",
            "company_change_type": "disable_autorotation", "old_company_has_autorotation": 1,
            "new_company_has_autorotation": 0, "old_company_routing_mode": "autorotation",
            "new_company_routing_mode": "server_priority", "reason": "Другое", "comment": "тест на гео",
        })
        self.assertIn("🔧 <b>Настройка кампании</b>", message)
        self.assertIn("📍 <b>Бразилия</b>\n🎯 <b>1001 · CC Mexico Demo 1</b>\n🖥 EU2", message)
        self.assertIn("🔄 <b>Авторотация:</b> Да → <b>Нет</b>", message)
        for technical in ("autorotation", "server_priority", "Маршрут", "Разница"):
            self.assertNotIn(technical, message)

    def test_multi_geo_campaign_does_not_use_event_geo(self):
        message = self.build({
            "apply_scope": "campaign_setting", "company_country_id": None, "company_country_name": None,
            "country_name": "Мексика", "company_change_type": "enable_autorotation",
            "old_company_has_autorotation": 0, "new_company_has_autorotation": 1,
        })
        self.assertIn("📍 <b>Несколько GEO</b>", message)
        self.assertNotIn("📍 <b>Мексика</b>", message)

    def test_set_and_remove_manual_route_show_only_route_state(self):
        base = {"apply_scope": "campaign_setting", "company_country_id": None, "reason": "Другое"}
        set_message = self.build({**base, "company_change_type": "set_campaign_route", "old_company_route_name": None, "new_company_route_name": "MX/New@"})
        remove_message = self.build({**base, "company_change_type": "remove_campaign_route", "old_company_route_name": "MX/Old@", "new_company_route_name": None})
        self.assertIn("🛣 <b>Ручной маршрут</b>\nБыло: —\nСтало: <b>MX/New@</b>", set_message)
        self.assertIn("🛣 <b>Ручной маршрут</b>\nБыло: <b>MX/Old@</b>\nСтало: —", remove_message)
        self.assertNotIn("set_campaign_route", set_message)
        self.assertNotIn("remove_campaign_route", remove_message)

    def test_dynamic_values_are_html_escaped(self):
        message = self.build({
            "apply_scope": "campaign_setting", "company_country_id": 1, "company_country_name": "Braz<il>",
            "company_id_external": "1&2", "company_name": "C<co>", "company_server_name": "EU&2",
            "company_change_type": "set_campaign_route", "old_company_route_name": "Old<a>",
            "new_company_route_name": "New&b", "reason": "A < B", "comment": "Use <safe>",
            "author_name": "Admin <root>",
        })
        for escaped in ("Braz&lt;il&gt;", "1&amp;2 · C&lt;co&gt;", "EU&amp;2", "Old&lt;a&gt;", "New&amp;b", "A &lt; B", "Use &lt;safe&gt;", "Admin &lt;root&gt;"):
            self.assertIn(escaped, message)

    def test_message_builder_uses_127_fallback_when_app_base_url_missing(self):
        self.assertIn("http://127.0.0.1:8000/provider-changes", self.build({}))

    def test_provider_change_url_uses_configured_app_base_url_without_duplicate_slashes(self):
        with patch.dict(os.environ, {"APP_BASE_URL": "https://routes.company.com/"}, clear=True):
            self.assertEqual(provider_change_url(), "https://routes.company.com/provider-changes")

    def test_send_uses_html_parse_mode(self):
        response = Mock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"}, clear=True), patch("urllib.request.urlopen", return_value=response) as urlopen:
            self.assertTrue(send_telegram_message("<b>hello</b>"))
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(payload["text"], "<b>hello</b>")

    def test_load_dotenv_if_present_does_not_override_existing_env(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as env_file:
            env_file.write("TELEGRAM_BOT_TOKEN=from-file\nTELEGRAM_CHAT_ID=chat-from-file\nAPP_BASE_URL=\"https://from-file.example\"\n")
            env_path = env_file.name
        try:
            with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "existing-token"}, clear=True):
                server.load_dotenv_if_present(env_path)
                self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "existing-token")
                self.assertEqual(os.environ["TELEGRAM_CHAT_ID"], "chat-from-file")
                self.assertEqual(os.environ["APP_BASE_URL"], "https://from-file.example")
        finally:
            os.unlink(env_path)

    def test_load_dotenv_if_missing_is_noop(self):
        with patch.dict(os.environ, {}, clear=True):
            server.load_dotenv_if_present("/tmp/tariffs-routes-app-missing.env")
            self.assertNotIn("TELEGRAM_BOT_TOKEN", os.environ)

    def test_send_skips_when_config_missing(self):
        with patch.dict(os.environ, {}, clear=True), patch("urllib.request.urlopen") as urlopen:
            self.assertFalse(send_telegram_message("hello"))
            urlopen.assert_not_called()
