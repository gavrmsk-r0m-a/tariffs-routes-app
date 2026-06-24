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


def build_provider_change_message(event: dict) -> str:
    scope = event.get("apply_scope")
    lines = [
        "Новая смена провайдера",
        "",
        f"Область: {ROUTING_SCOPE_LABELS.get(scope, _text(scope))}",
    ]
    _append_if_present(lines, "GEO", event.get("country_name"))

    if scope == "server_priority":
        server_names = event.get("affected_server_names") or event.get("server_name")
        _append_if_present(lines, "Сервер", server_names)
        route_parts = []
        if event.get("old_route_name"):
            route_parts.append(str(event["old_route_name"]))
        if event.get("new_route_name"):
            route_parts.append(str(event["new_route_name"]))
        if route_parts:
            lines.append(f"Маршрут: {' → '.join(route_parts)}")
        _append_if_present(lines, "Провайдер", event.get("provider_name"))
    elif scope == "campaign_setting":
        company = " / ".join(part for part in (event.get("company_id_external"), event.get("company_name")) if part)
        _append_if_present(lines, "Кампания", company)
        _append_if_present(lines, "Сервер", event.get("company_server_name"))
        if event.get("company_change_type"):
            lines.append(f"Тип изменения: {COMPANY_CHANGE_LABELS.get(event.get('company_change_type'), event.get('company_change_type'))}")
        if event.get("new_company_routing_mode"):
            lines.append(f"Режим: {_text(event.get('old_company_routing_mode'))} → {_text(event.get('new_company_routing_mode'))}")
        if event.get("old_company_route_name") or event.get("new_company_route_name"):
            lines.append(f"Маршрут кампании: {_text(event.get('old_company_route_name'))} → {_text(event.get('new_company_route_name'))}")
        if event.get("old_company_has_autorotation") is not None or event.get("new_company_has_autorotation") is not None:
            old_auto = "Да" if event.get("old_company_has_autorotation") else "Нет"
            new_auto = "Да" if event.get("new_company_has_autorotation") else "Нет"
            lines.append(f"Авторотация: {old_auto} → {new_auto}")
        _append_if_present(lines, "Провайдер", event.get("provider_name"))
    else:
        _append_if_present(lines, "Провайдер", event.get("provider_name"))
        _append_if_present(lines, "Маршрут", event.get("affected_route_name"))

    lines.extend([
        f"Причина: {_text(event.get('reason'))}",
    ])
    if event.get("comment") and str(event.get("comment")).strip():
        lines.append(f"Комментарий: {event.get('comment')}")
    lines.extend([
        f"Создал: {_text(event.get('author_name'))}",
        f"Время: {_text(event.get('event_at'))}",
    ])

    url = provider_change_url(os.environ.get("APP_BASE_URL"))
    if url:
        lines.extend(["", "Открыть в TeleRoute:", url])
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
