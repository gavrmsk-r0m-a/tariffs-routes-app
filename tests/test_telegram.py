import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import app.server as server
from app.telegram import build_provider_change_message, provider_change_url, send_telegram_message


class TelegramMessageTest(unittest.TestCase):
    def test_server_priority_bolds_new_route_and_uses_app_base_url(self):
        event = {
            "apply_scope": "server_priority",
            "country_name": "Мексика",
            "affected_server_names": "EU1",
            "old_route_name": "Мексика/Sancom/Old@",
            "new_route_name": "Мексика/Miatel/RND@",
            "overflow_route_name": "Мексика/Overflow@",
            "reason": "Плохие показатели",
            "comment": "Перевели трафик на Miatel",
            "author_name": "Admin",
            "event_at": "2026-06-24 21:15",
        }
        with patch.dict(os.environ, {"APP_BASE_URL": "https://teleroute.example"}, clear=True):
            message = build_provider_change_message(event)
        self.assertIn("🚨 <b>Смена провайдера</b>", message)
        self.assertIn("📍 <b>Мексика</b> | <b>EU1</b>", message)
        self.assertIn("Мексика/Sancom/Old@\n→ <b>Мексика/Miatel/RND@</b>", message)
        self.assertIn("🌊 Перелив:\nМексика/Overflow@", message)
        self.assertIn("https://teleroute.example/provider-changes", message)

    def test_message_builder_uses_127_fallback_when_app_base_url_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            message = build_provider_change_message({})
        self.assertIn("http://127.0.0.1:8000/provider-changes", message)

    def test_provider_change_url_uses_configured_app_base_url_without_duplicate_slashes(self):
        with patch.dict(os.environ, {"APP_BASE_URL": "https://routes.company.com/"}, clear=True):
            self.assertEqual(provider_change_url(), "https://routes.company.com/provider-changes")

    def test_campaign_setting_bolds_new_route_and_new_states(self):
        event = {
            "apply_scope": "campaign_setting",
            "country_name": "Италия",
            "company_server_name": "Dialer 1",
            "company_id_external": "CMP-42",
            "company_name": "Main campaign",
            "old_company_routing_mode": "Авторотация",
            "new_company_routing_mode": "Ручной маршрут",
            "old_company_route_name": "Италия/Old@",
            "new_company_route_name": "Италия/New@",
            "old_company_has_autorotation": 1,
            "new_company_has_autorotation": 0,
            "reason": "Оптимизация",
            "comment": "Переключили",
            "author_name": "Admin",
            "event_at": "2026-06-24 21:15",
        }
        with patch.dict(os.environ, {}, clear=True):
            message = build_provider_change_message(event)
        self.assertIn("Авторотация → <b>Ручной маршрут</b>", message)
        self.assertIn("Италия/Old@\n→ <b>Италия/New@</b>", message)
        self.assertIn("Да → <b>Нет</b>", message)

    def test_none_scope_bolds_provider_and_route(self):
        event = {
            "apply_scope": "none",
            "country_name": "Мексика",
            "provider_name": "Miatel",
            "affected_route_name": "Мексика/Miatel/Route@",
            "reason": "Другое",
            "comment": "Внешнее изменение",
            "author_name": "Admin",
            "event_at": "2026-06-24 21:15",
        }
        with patch.dict(os.environ, {}, clear=True):
            message = build_provider_change_message(event)
        self.assertIn("📍 <b>Мексика</b>", message)
        self.assertIn("📡 Провайдер / Маршрут:\n<b>Miatel</b>\n<b>Мексика/Miatel/Route@</b>", message)
        self.assertNotIn("Сервер:", message)

    def test_html_escapes_comment_and_route_name(self):
        event = {
            "apply_scope": "server_priority",
            "country_name": "Мексика <MX>",
            "affected_server_names": "EU & US",
            "old_route_name": "Old <route> & one",
            "new_route_name": "New <route> & two",
            "reason": "A < B & C",
            "comment": "Use <safe> & fast",
            "author_name": "Admin <root>",
            "event_at": "2026-06-24 21:15",
        }
        with patch.dict(os.environ, {}, clear=True):
            message = build_provider_change_message(event)
        self.assertIn("<b>Мексика &lt;MX&gt;</b> | <b>EU &amp; US</b>", message)
        self.assertIn("Old &lt;route&gt; &amp; one\n→ <b>New &lt;route&gt; &amp; two</b>", message)
        self.assertIn("A &lt; B &amp; C", message)
        self.assertIn("Use &lt;safe&gt; &amp; fast", message)
        self.assertIn("Admin &lt;root&gt;", message)

    def test_empty_values_are_rendered_as_dash(self):
        event = {"apply_scope": "campaign_setting", "country_name": "", "company_server_name": ""}
        with patch.dict(os.environ, {}, clear=True):
            message = build_provider_change_message(event)
        self.assertIn("📍 <b>—</b> | <b>—</b>", message)
        self.assertIn("🎯 — / —", message)
        self.assertIn("— → <b>—</b>", message)
        self.assertIn("📞 Маршрут:\n—\n→ <b>—</b>", message)

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
