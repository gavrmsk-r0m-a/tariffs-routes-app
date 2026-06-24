from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from urllib.parse import urljoin

from app.repository import COMPANY_CHANGE_LABELS, ROUTING_SCOPE_LABELS

logger = logging.getLogger(__name__)

TELEGRAM_API_TIMEOUT_SECONDS = 5


def _text(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return text or "—"


def _append_if_present(lines: list[str], label: str, value: object) -> None:
    if value is not None and str(value).strip() != "":
        lines.append(f"{label}: {value}")


def provider_change_url(base_url: str | None) -> str | None:
    if not base_url or not base_url.strip():
        return None
    return urljoin(base_url.rstrip("/") + "/", "provider-changes")


def _route_transition(event: dict, old_key: str, new_key: str) -> str | None:
    old_value = event.get(old_key)
    new_value = event.get(new_key)
    if old_value or new_value:
        return f"{_text(old_value)} → {_text(new_value)}"
    return None


def provider_change_details(event: dict) -> str:
    scope = event.get("apply_scope")
    if scope == "server_priority":
        if event.get("old_route_name") and event.get("new_route_name"):
            return f"Маршрут: {_route_transition(event, 'old_route_name', 'new_route_name')}"
        route = event.get("new_route_name") or event.get("old_route_name")
        if route:
            return f"Маршрут: {route}"
        return _text(event.get("provider_name"))
    if scope == "campaign_setting":
        details = []
        change_type = event.get("company_change_type")
        if change_type:
            details.append(str(COMPANY_CHANGE_LABELS.get(change_type, change_type)))
        mode = _route_transition(event, "old_company_routing_mode", "new_company_routing_mode")
        if mode:
            details.append(f"Режим: {mode}")
        route = _route_transition(event, "old_company_route_name", "new_company_route_name")
        if route:
            details.append(f"Маршрут: {route}")
        if event.get("old_company_has_autorotation") is not None or event.get("new_company_has_autorotation") is not None:
            old_auto = "Да" if event.get("old_company_has_autorotation") else "Нет"
            new_auto = "Да" if event.get("new_company_has_autorotation") else "Нет"
            details.append(f"Авторотация: {old_auto} → {new_auto}")
        return "; ".join(details) or _text(event.get("provider_name"))
    route = event.get("affected_route_name")
    provider = event.get("provider_name")
    if route and provider:
        return f"Провайдер: {provider}; Маршрут/префикс: {route}"
    return _text(route or provider)


def build_provider_change_message(event: dict) -> str:
    scope = event.get("apply_scope")
    server = event.get("affected_server_names") or event.get("company_server_name") or event.get("server_name")
    campaign = " / ".join(str(part) for part in (event.get("company_id_external"), event.get("company_name")) if part)
    lines = [
        "🚨 Новая смена провайдера",
        "",
        f"Дата: {_text(event.get('event_at'))}",
        f"Область: {ROUTING_SCOPE_LABELS.get(scope, _text(scope))}",
        f"GEO: {_text(event.get('country_name'))}",
        f"Сервер: {_text(server)}",
        f"Кампания: {_text(campaign)}",
        f"Причина: {_text(event.get('reason'))}",
        f"Детали: {provider_change_details(event)}",
        f"Комментарий: {_text(event.get('comment'))}",
        f"Создал: {_text(event.get('author_name'))}",
    ]
    url = provider_change_url(os.environ.get("APP_BASE_URL"))
    if url:
        lines.extend(["", "Открыть:", url])
    return "\n".join(lines)

def send_telegram_message(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.debug("Telegram provider-change notification skipped: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
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
