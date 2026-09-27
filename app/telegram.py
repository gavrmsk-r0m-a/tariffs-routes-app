from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from html import escape
from urllib.parse import urljoin

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


def _decimal(value: object) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _price_difference_block(event: dict) -> list[str]:
    delta = _decimal(event.get("price_delta_eur"))
    if delta is None:
        old_price = _decimal(event.get("old_price_eur"))
        new_price = _decimal(event.get("new_price_eur"))
        if old_price is not None and new_price is not None:
            delta = new_price - old_price
    if delta is None:
        return []
    if delta < 0:
        indicator = "🟢"
        value = f"{delta:.2f} EUR"
    elif delta > 0:
        indicator = "🔴"
        value = f"+{delta:.2f} EUR"
    else:
        indicator = "⚪"
        value = "0.00 EUR"
    return [f"{indicator} Разница: {value}"]

def app_base_url() -> str:
    return os.environ.get("APP_BASE_URL", "").strip() or DEFAULT_APP_BASE_URL


def provider_change_url(base_url: str | None = None) -> str:
    base = base_url.strip() if base_url and base_url.strip() else app_base_url()
    return urljoin(base.rstrip("/") + "/", "provider-changes")


def _reason_comment_block(event: dict) -> list[str]:
    lines = [f"📝 <b>Причина:</b> {_html(event.get('reason'))}"]
    if _text(event.get("comment")) != "—":
        lines.append(f"💬 {_html(event.get('comment'))}")
    return lines


def _footer_block(event: dict) -> list[str]:
    return [
        "",
        f"👤 {_html(event.get('author_name'))}",
        f"🕒 {_html(event.get('event_at'))}",
        "",
        "🔗 Открыть:",
        _html(provider_change_url()),
    ]


def _server_priority_route_block(event: dict) -> list[str]:
    old_route = event.get("old_route_name") or event.get("affected_route_name")
    return [
        "🛣 <b>Основной маршрут</b>",
        f"Было: {_html(old_route)}",
        f"Стало: {_bold(event.get('new_route_name'))}",
    ]


def _server_priority_overflow_block(event: dict) -> list[str]:
    if _text(event.get("overflow_route_name")) == "—":
        return []
    return [f"🌊 <b>Перелив:</b> {_html(event.get('overflow_route_name'))}"]


def _server_priority_message(event: dict) -> str:
    server = event.get("affected_server_names") or event.get("server_name")
    lines = [
        "⚙️ <b>Серверный приоритет</b>",
        "",
        f"📍 {_bold(event.get('country_name'))}",
        f"🖥 <b>Серверы:</b> {_html(server)}",
        "",
        *_server_priority_route_block(event),
        "",
        *_server_priority_overflow_block(event),
        "",
        *_price_difference_block(event),
        "",
        *_reason_comment_block(event),
        *_footer_block(event),
    ]
    return "\n".join(lines)


def _campaign_setting_message(event: dict) -> str:
    server = event.get("company_server_name") or event.get("server_name")
    campaign = f"{_text(event.get('company_id_external'))} · {_text(event.get('company_name'))}"
    if "company_country_id" in event:
        campaign_geo = event.get("company_country_name") if event.get("company_country_id") is not None else "Несколько GEO"
    else:
        campaign_geo = None
    change_type = event.get("company_change_type")
    lines = [
        "🔧 <b>Настройка кампании</b>",
        "",
        f"📍 {_bold(campaign_geo)}",
        f"🎯 {_bold(campaign)}",
        f"🖥 {_html(server)}",
        "",
    ]
    if change_type in {"enable_autorotation", "disable_autorotation"}:
        lines.append(f"🔄 <b>Авторотация:</b> {_html(_bool_text(event.get('old_company_has_autorotation')))} → {_bold(_bool_text(event.get('new_company_has_autorotation')))}")
    elif change_type in {"set_campaign_route", "remove_campaign_route"}:
        lines.append("🛣 <b>Ручной маршрут</b>")
        if change_type == "remove_campaign_route":
            lines.extend([
                f"Было: {_bold(event.get('old_company_route_name'))}",
                f"Стало: {_html(event.get('new_company_route_name'))}",
            ])
        else:
            lines.extend([
                f"Было: {_html(event.get('old_company_route_name'))}",
                f"Стало: {_bold(event.get('new_company_route_name'))}",
            ])
        difference = _price_difference_block(event)
        if difference:
            lines.extend(["", *difference])
    lines.extend(["", *_reason_comment_block(event), *_footer_block(event)])
    return "\n".join(lines)


def _none_scope_message(event: dict) -> str:
    route = event.get("affected_route_name") or event.get("new_route_name") or event.get("old_route_name")
    lines = [
        "📡 <b>Событие у провайдера</b>",
        "",
        f"📍 {_bold(event.get('country_name'))}",
        f"🏢 <b>Провайдер:</b> {_html(event.get('provider_name'))}",
        f"🛣 <b>Маршрут:</b> {_html(route)}",
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
