from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from html import escape
from urllib.parse import urljoin

from app.repository import COMPANY_CHANGE_LABELS

logger = logging.getLogger(__name__)

TELEGRAM_API_TIMEOUT_SECONDS = 5
DEFAULT_APP_BASE_URL = "http://127.0.0.1:8000"


def _text(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return text or "—"


def _html(value: object) -> str:
    return escape(_text(value), quote=False)


def _bold(value: object) -> str:
    return f"<b>{_html(value)}</b>"


def _bool_text(value: object) -> str:
    if value is None or str(value).strip() == "":
        return "—"
    return "Да" if str(value) in {"1", "true", "True", "yes", "Да"} else "Нет"


def app_base_url() -> str:
    return os.environ.get("APP_BASE_URL", "").strip() or DEFAULT_APP_BASE_URL


def provider_change_url(base_url: str | None = None) -> str:
    base = base_url.strip() if base_url and base_url.strip() else app_base_url()
    return urljoin(base.rstrip("/") + "/", "provider-changes")


def _reason_comment_block(event: dict) -> list[str]:
    return [
        "📝 Причина / Комментарий:",
        _html(event.get("reason")),
        _html(event.get("comment")),
    ]


def _footer_block(event: dict) -> list[str]:
    return [
        "",
        f"👤 {_html(event.get('author_name'))}",
        f"🕒 {_html(event.get('event_at'))}",
        "",
        "🔗 Открыть:",
        _html(provider_change_url()),
    ]


def _server_priority_message(event: dict) -> str:
    server = event.get("affected_server_names") or event.get("server_name")
    old_route = event.get("old_route_name") or event.get("affected_route_name")
    lines = [
        "🚨 <b>Смена провайдера</b>",
        "",
        f"📍 {_bold(event.get('country_name'))} | {_bold(server)}",
        "⚙️ Серверный приоритет",
        "",
        "🔄 Маршрут:",
        _html(old_route),
        f"→ {_bold(event.get('new_route_name'))}",
        "",
        "🌊 Перелив:",
        _html(event.get("overflow_route_name")),
        "",
        *_reason_comment_block(event),
        *_footer_block(event),
    ]
    return "\n".join(lines)


def _campaign_setting_message(event: dict) -> str:
    server = event.get("company_server_name") or event.get("server_name")
    campaign = f"{_text(event.get('company_id_external'))} / {_text(event.get('company_name'))}"
    old_mode = event.get("old_company_routing_mode")
    new_mode = event.get("new_company_routing_mode")
    if old_mode is None and new_mode is None:
        change_type = event.get("company_change_type")
        new_mode = COMPANY_CHANGE_LABELS.get(change_type, change_type) if change_type else None
    lines = [
        "🚨 <b>Смена провайдера</b>",
        "",
        f"📍 {_bold(event.get('country_name'))} | {_bold(server)}",
        "📦 Настройка кампании",
        f"🎯 {_html(campaign)}",
        "",
        "🔄 Изменение:",
        f"{_html(old_mode)} → {_bold(new_mode)}",
        "",
        "📞 Маршрут:",
        _html(event.get("old_company_route_name")),
        f"→ {_bold(event.get('new_company_route_name'))}",
        "",
        "🔁 Авторотация:",
        f"{_html(_bool_text(event.get('old_company_has_autorotation')))} → {_bold(_bool_text(event.get('new_company_has_autorotation')))}",
        "",
        *_reason_comment_block(event),
        *_footer_block(event),
    ]
    return "\n".join(lines)


def _none_scope_message(event: dict) -> str:
    route = event.get("affected_route_name") or event.get("new_route_name") or event.get("old_route_name")
    lines = [
        "🚨 <b>Смена провайдера</b>",
        "",
        f"📍 {_bold(event.get('country_name'))}",
        "",
        "📡 Провайдер / Маршрут:",
        _bold(event.get("provider_name")),
        _bold(route),
        "",
        *_reason_comment_block(event),
        *_footer_block(event),
    ]
    return "\n".join(lines)


def build_provider_change_message(event: dict) -> str:
    scope = event.get("apply_scope")
    if scope == "server_priority":
        return _server_priority_message(event)
    if scope == "campaign_setting":
        return _campaign_setting_message(event)
    return _none_scope_message(event)


def send_telegram_message(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.debug("Telegram provider-change notification skipped: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TELEGRAM_API_TIMEOUT_SECONDS) as response:
            if response.status >= 400:
                logger.error("Telegram provider-change notification failed with HTTP %s", response.status)
                return False
            return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.error("Telegram provider-change notification failed: %s", exc)
        return False


def notify_provider_change_created(event: dict) -> bool:
    return send_telegram_message(build_provider_change_message(event))
