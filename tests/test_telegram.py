import os
import tempfile
import unittest
from unittest.mock import patch

import app.server as server
from app.telegram import build_provider_change_message, provider_change_url, send_telegram_message


class TelegramMessageTest(unittest.TestCase):
    def test_message_builder_includes_common_fields(self):
        event = {
            "apply_scope": "server_priority",
            "country_name": "Мексика",
            "affected_server_names": "EU1",
            "new_route_name": "Мексика/Miatel/RND@",
            "provider_name": "Miatel",
            "reason": "Плохие показатели",
            "comment": "Перевели трафик на Miatel",
            "author_name": "Admin",
            "event_at": "2026-06-24 21:15",
        }
        with patch.dict(os.environ, {"APP_BASE_URL": "https://teleroute.example"}, clear=True):
            message = build_provider_change_message(event)
        self.assertIn("Область: Серверный приоритет", message)
        self.assertIn("GEO: Мексика", message)
        self.assertIn("Сервер: EU1", message)
        self.assertIn("Маршрут: Мексика/Miatel/RND@", message)
        self.assertIn("Причина: Плохие показатели", message)
        self.assertIn("Комментарий: Перевели трафик на Miatel", message)
        self.assertIn("Создал: Admin", message)
        self.assertIn("Дата: 2026-06-24 21:15", message)
        self.assertIn("https://teleroute.example/provider-changes", message)

    def test_message_builder_uses_localhost_fallback_when_app_base_url_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            message = build_provider_change_message({})
        self.assertIn("http://localhost:8000/provider-changes", message)

    def test_provider_change_url_uses_configured_app_base_url_without_duplicate_slashes(self):
        with patch.dict(os.environ, {"APP_BASE_URL": "https://routes.company.com/"}, clear=True):
            self.assertEqual(provider_change_url(), "https://routes.company.com/provider-changes")

    def test_none_scope_does_not_invent_server_field(self):
        event = {
            "apply_scope": "none",
            "country_name": "Мексика",
            "provider_name": "Miatel",
            "reason": "Другое",
            "comment": "Внешнее изменение",
            "author_name": "Admin",
            "event_at": "2026-06-24 21:15",
        }
        with patch.dict(os.environ, {}, clear=True):
            message = build_provider_change_message(event)
        self.assertIn("Область: Не меняли настройки в нашей системе", message)
        self.assertIn("Сервер: —", message)

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
