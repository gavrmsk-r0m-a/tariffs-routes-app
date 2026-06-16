from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import sqlite3
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from wsgiref.simple_server import make_server

from app.db import DEFAULT_DB_PATH, connect, init_db
from app.importer import apply_import, preview_import
from app.repository import BusinessRuleError, COMPANY_CHANGE_LABELS, ROUTING_SCOPE_LABELS, Repository, normalize_phone_status, normalize_provider_name, validate_phone_number

DB_PATH = Path(os.environ.get("MVP_DB_PATH", DEFAULT_DB_PATH))
ADMIN_ID = 1
CURRENT_USER_COOKIE = "mvp_current_user_id"
_REQUEST_CONTEXT: dict[str, object] = {}
STATUS_LABELS = {
    "used": "Используется",
    "free": "Свободен",
    "problem": "Проблемный",
    "unknown": "Неизвестно",
}


def phone_status_options(selected: str | None = None, *, empty: str | None = None) -> str:
    selected = normalize_phone_status(selected) if selected else selected
    html_options = [f"<option value=''>{esc(empty)}</option>"] if empty is not None else []
    for value, label in STATUS_LABELS.items():
        html_options.append(f"<option value='{value}' {'selected' if selected == value else ''}>{label}</option>")
    return "".join(html_options)

PAGE_SIZE = 50


def parse_page(q: dict[str, str]) -> int:
    try:
        page_number = int(q.get("page") or "1")
    except (TypeError, ValueError):
        return 1
    return page_number if page_number > 0 else 1


def paginate_rows(rows: list, q: dict[str, str], base_path: str) -> tuple[list, str]:
    total = len(rows)
    current = parse_page(q)
    page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if current > page_count:
        current = 1
    start = (current - 1) * PAGE_SIZE
    visible = rows[start:start + PAGE_SIZE]
    if total <= PAGE_SIZE:
        return visible, f"<p class='muted'>Всего записей: {total}</p>"

    def page_href(page_number: int) -> str:
        params = {key: value for key, value in q.items() if key not in {"page", "limit", "export"} and value not in (None, "")}
        params["page"] = str(page_number)
        return base_path + "?" + urlencode(params)

    previous_link = f"<a class='button' href='{esc(page_href(current - 1))}'>← Назад</a>" if current > 1 else ""
    next_link = f"<a class='button' href='{esc(page_href(current + 1))}'>Вперёд →</a>" if current < page_count else ""
    return visible, (
        "<nav class='pagination' aria-label='Пагинация'>"
        f"<span class='muted'>Всего записей: {total}. Страница {current} из {page_count}</span> "
        f"{previous_link} {next_link}</nav>"
    )


def export_link(base_path: str, q: dict[str, str]) -> str:
    params = {key: value for key, value in q.items() if key not in {"page", "limit", "export"} and value not in (None, "")}
    params["export"] = "csv"
    return f"<span class='action-icon export-action-icon' aria-hidden='true'>{nav_icon('export')}</span><a class='button export-button table-utility-button' href='{esc(base_path + '?' + urlencode(params))}'>Экспорт</a>"


def copy_column_button(column: str) -> str:
    return f"<button class='copy-column-button' type='button' data-copy-action='{esc(column)}' title='Скопировать колонку' aria-label='Скопировать колонку'>{nav_icon('copy')}</button>"


def csv_response(filename: str, headers: list[str], rows: list[list[object]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(headers)
    writer.writerows([["" if value is None else value for value in row] for row in rows])
    return ("\ufeff" + output.getvalue()).encode("utf-8")

ASSIGNMENT_LABELS = {
    "pool_number": "Номер из пула",
    "outgoing_cli": "АОН",
    "inbound_line": "Входящая линия",
    "office_phone": "Офисная телефония",
    "sim_card": "SIM-карта",
    "other": "Другое",
}


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def plain_text(value: object) -> str:
    text = re.sub(r"<[^>]+>", " ", "" if value is None else str(value))
    return re.sub(r"\s+", " ", text).strip()


def plain_title(value: object) -> str:
    return esc(plain_text(value))


def clamp_cell(col: str, content_html: str, title: object, *, extra_attrs: str = "", classes: str = "") -> str:
    class_attr = f" class='{classes}'" if classes else ""
    attrs = f" {extra_attrs.strip()}" if extra_attrs.strip() else ""
    title_text = plain_text(title)
    title_attr = f" title='{esc(title_text)}'" if len(title_text) > 60 else ""
    full_text_attr = f" data-full-text='{esc(title_text)}'" if len(title_text) > 60 else ""
    return f"<td data-col='{esc(col)}'{class_attr}{attrs}{title_attr}{full_text_attr}><span class='cell-clamp'>{content_html}</span></td>"


ROLE_PERMISSIONS = {
    "admin": {"read": {"*"}, "write": {"*"}},
    "operator": {
        "read": {"dashboard", "routes", "tariffs", "phones", "companies", "provider_changes", "admin_server_priorities", "admin_company_routing_settings"},
        "write": {"provider_changes"},
    },
    "guest": {"read": {"dashboard", "routes", "tariffs"}, "write": set()},
}

EXPORT_FILENAMES = {
    "/routes": "routes_export.csv",
    "/tariffs": "tariffs_export.csv",
    "/phones": "phones_export.csv",
    "/companies": "companies_export.csv",
    "/provider-changes": "provider_changes_export.csv",
    "/admin/server-priorities": "server_priorities_export.csv",
    "/admin/company-routing-settings": "company_routing_settings_export.csv",
}

ADMIN_SECTION_KEYS = {
    "admin",
    "admin_server_priorities",
    "admin_company_routing_settings",
    "admin_route_naming",
    "admin_import_export",
    "admin_currency_rates",
    "admin_provider_reasons",
    "admin_users",
    "admin_dictionaries",
    "admin_change_log",
}


def normalize_role(role_key: str | None) -> str:
    return role_key if role_key in ROLE_PERMISSIONS else "guest"


def current_role_key() -> str:
    return normalize_role(_REQUEST_CONTEXT.get("current_role_key") if isinstance(_REQUEST_CONTEXT.get("current_role_key"), str) else None)


def role_allows(role_key: str | None, action: str, section: str) -> bool:
    allowed = ROLE_PERMISSIONS[normalize_role(role_key)][action]
    return "*" in allowed or section in allowed


def can_read(section: str) -> bool:
    return role_allows(current_role_key(), "read", section)


def can_write(section: str) -> bool:
    return role_allows(current_role_key(), "write", section)



def forbidden_page() -> bytes:
    return page("Нет доступа", "<section class='message-card error'><h1>Нет доступа</h1><p>У текущего пользователя нет прав для этого раздела или действия.</p></section>")


class ForbiddenError(Exception):
    pass


def svg_icon(path: str, view_box: str = "0 0 24 24") -> str:
    normalized_path = path.replace('<path ', '<path fill="currentColor" ') if "fill=" not in path else path
    normalized_path = normalized_path.replace(" 0.", " .").replace("-0.", "-.")
    return f'<svg viewBox="{view_box}" focusable="false" aria-hidden="true">{normalized_path}</svg>'

SEMANTIC_ICONS = {
    "dashboard": svg_icon('<path d="m12 2.9751 11 8.2 -0.85 1.175 -2.15 -1.625v10.275H4v-10.275l-2.15 1.625 -0.85 -1.175 11 -8.2Zm-4.25 9.075c0 0.8 0.43335 1.70835 1.3 2.725s1.85 2.01665 2.95 3c1.1 -0.98335 2.08335 -1.98335 2.95 -3 0.86665 -1.01665 1.3 -1.925 1.3 -2.725 0 -0.65 -0.2125 -1.1875 -0.6375 -1.6125 -0.425 -0.425 -0.9625 -0.6375 -1.6125 -0.6375 -0.38335 0 -0.75 0.09585 -1.1 0.2875 -0.35 0.19165 -0.65 0.42085 -0.9 0.6875 -0.25 -0.26665 -0.55 -0.49585 -0.9 -0.6875s-0.71665 -0.2875 -1.1 -0.2875c-0.65 0 -1.1875 0.2125 -1.6125 0.6375 -0.425 0.425 -0.6375 0.9625 -0.6375 1.6125Z"/>'),
    "routes": svg_icon('<path d="M11.25 22v-4.75h-5.5L3 14.5l2.75 -2.75h5.5V9.5H4V4h7.25V2h1.5v2h5.5l2.75 2.75 -2.75 2.75h-5.5v2.25H20v5.5H12.75V22h-1.5Z"/>'),
    "tariffs": svg_icon('<path d="m21.575 13.9 -7.65 7.675c-0.15 0.14165 -0.31875 0.2479 -0.50625 0.31875 -0.1875 0.07085 -0.375 0.10625 -0.5625 0.10625s-0.3729 -0.0375 -0.55625 -0.1125c-0.18335 -0.075 -0.35 -0.17915 -0.5 -0.3125L2.45 12.2c-0.133335 -0.13335 -0.241665 -0.29135 -0.325 -0.474 -0.083335 -0.1825 -0.125 -0.3745 -0.125 -0.576V3.5c0 -0.4125 0.146915 -0.765665 0.44075 -1.0595C2.734415 2.146835 3.0875 2 3.5 2h7.675c0.20115 0 0.3961 0.040585 0.58475 0.12175 0.1885 0.081335 0.3519 0.19075 0.49025 0.32825l9.325 9.325c0.1565 0.15 0.27065 0.31875 0.3425 0.50625 0.07165 0.1875 0.1075 0.375 0.1075 0.5625s-0.0375 0.3771 -0.1125 0.56875c-0.075 0.19165 -0.1875 0.35415 -0.3375 0.4875Zm-15.45 -6.5c0.35 0 0.65415 -0.12915 0.9125 -0.3875 0.25835 -0.25835 0.3875 -0.5625 0.3875 -0.9125s-0.12915 -0.65415 -0.3875 -0.9125c-0.25835 -0.258335 -0.5625 -0.3875 -0.9125 -0.3875s-0.65415 0.129165 -0.9125 0.3875c-0.258335 0.25835 -0.3875 0.5625 -0.3875 0.9125s0.129165 0.65415 0.3875 0.9125c0.25835 0.25835 0.5625 0.3875 0.9125 0.3875Z"/>'),
    "phones": svg_icon('<path d="M6.85 19.175h1.5v-1.5h-1.5v1.5Zm0 -3.9h1.5v-4.15h-1.5v4.15Zm4.35 3.9h1.5v-4.25h-1.5v4.25Zm0 -6.55h1.5v-1.5h-1.5v1.5Zm4.55 6.55h1.5v-1.5h-1.5v1.5Zm0 -3.9h1.5v-4.15h-1.5v4.15ZM5.5 22c-0.4 0 -0.75 -0.15 -1.05 -0.45 -0.3 -0.3 -0.45 -0.65 -0.45 -1.05V7.975L9.975 2H18.5c0.4 0 0.75 0.15 1.05 0.45 0.3 0.3 0.45 0.65 0.45 1.05v17c0 0.4 -0.15 0.75 -0.45 1.05 -0.3 0.3 -0.65 0.45 -1.05 0.45H5.5Z"/>'),
    "companies": svg_icon('<path d="M4.95 17.0502c-0.95 -0.95 -1.679165 -2.02915 -2.1875 -3.2375C2.254165 12.60435 2 11.33355 2 10.0002c0 -1.33335 0.254165 -2.60415 0.7625 -3.8125C3.270835 4.97936 4 3.900195 4.95 2.950195l0.875 0.875c-0.81665 0.833335 -1.45 1.779155 -1.9 2.837505 -0.45 1.05835 -0.675 2.17085 -0.675 3.3375 0 1.16665 0.225 2.27915 0.675 3.3375 0.45 1.05835 1.08335 2.00415 1.9 2.8375l-0.875 0.875Zm2.3 -2.3c-0.63335 -0.63335 -1.11665 -1.35835 -1.45 -2.175 -0.33335 -0.81665 -0.5 -1.675 -0.5 -2.575 0 -0.9 0.16665 -1.75835 0.5 -2.575 0.33335 -0.81665 0.81665 -1.54165 1.45 -2.175l0.875 0.875c-0.5 0.51665 -0.8875 1.10835 -1.1625 1.775 -0.275 0.66665 -0.4125 1.36665 -0.4125 2.1 0 0.73335 0.1375 1.42915 0.4125 2.0875 0.275 0.65835 0.6625 1.25415 1.1625 1.7875l-0.875 0.875Zm4 6.25v-8.85c-0.46665 -0.15 -0.8375 -0.425 -1.1125 -0.825 -0.275 -0.4 -0.4125 -0.84165 -0.4125 -1.325 0 -0.63335 0.22085 -1.17085 0.6625 -1.6125 0.44165 -0.44165 0.97915 -0.6625 1.6125 -0.6625 0.63335 0 1.17085 0.22085 1.6125 0.6625 0.44165 0.44165 0.6625 0.97915 0.6625 1.6125 0 0.48335 -0.1375 0.925 -0.4125 1.325 -0.275 0.4 -0.64585 0.675 -1.1125 0.825v8.85h-1.5Zm5.5 -6.25 -0.875 -0.875c0.5 -0.53335 0.8875 -1.12915 1.1625 -1.7875 0.275 -0.65835 0.4125 -1.35415 0.4125 -2.0875s-0.1375 -1.42915 -0.4125 -2.0875c-0.275 -0.65835 -0.6625 -1.25415 -1.1625 -1.7875l0.875 -0.875c0.63335 0.63335 1.11665 1.35835 1.45 2.175 0.33335 0.81665 0.5 1.675 0.5 2.575 0 0.9 -0.16665 1.75835 -0.5 2.575 -0.33335 0.81665 -0.81665 1.54165 -1.45 2.175Zm2.3 2.3 -0.875 -0.875c0.81665 -0.83335 1.45 -1.77915 1.9 -2.8375 0.45 -1.05835 0.675 -2.17085 0.675 -3.3375 0 -1.16665 -0.225 -2.27915 -0.675 -3.3375 -0.45 -1.05835 -1.08335 -2.00417 -1.9 -2.837505l0.875 -0.875c0.95 0.95 1.67915 2.029165 2.1875 3.237505C21.74585 7.39605 22 8.66685 22 10.0002c0 1.33335 -0.25415 2.60415 -0.7625 3.8125 -0.50835 1.20835 -1.2375 2.2875 -2.1875 3.2375Z"/>'),
    "provider_changes": svg_icon('<path d="M10.75 21.9498c-1.2 -0.15 -2.32085 -0.50835 -3.3625 -1.075 -1.04165 -0.56665 -1.94165 -1.2875 -2.7 -2.1625 -0.758335 -0.875 -1.354165 -1.88335 -1.7875 -3.025 -0.433335 -1.14165 -0.65 -2.37085 -0.65 -3.6875 0 -1.46665 0.345835 -2.86665 1.0375 -4.2 0.691665 -1.33335 1.620835 -2.51665 2.7875 -3.549995h-3.05v-1.5H8.75V8.4748h-1.5v-3.225c-1.06665 0.85 -1.91665 1.8625 -2.55 3.0375 -0.633335 1.175 -0.95 2.4125 -0.95 3.7125 0 2.2 0.666665 4.07915 2 5.6375 1.33335 1.55835 3 2.4875 5 2.7875v1.525Zm-0.175 -5.7 -3.875 -3.875 1.05 -1.05 2.825 2.825 5.675 -5.675 1.05 1.05 -6.725 6.725Zm4.675 5v-5.725h1.5v3.225c1.06665 -0.86665 1.91665 -1.88335 2.55 -3.05 0.63335 -1.16665 0.95 -2.4 0.95 -3.7 0 -2.2 -0.66665 -4.07915 -2 -5.6375 -1.33335 -1.55833 -3 -2.487495 -5 -2.787495v-1.525c2.43335 0.3 4.45835 1.375 6.075 3.224995 1.61665 1.85 2.425 4.09165 2.425 6.725 0 1.46665 -0.34585 2.86665 -1.0375 4.2 -0.69165 1.33335 -1.62085 2.51665 -2.7875 3.55h3.05v1.5H15.25Z"/>'),
    "admin": svg_icon('<path d="M20.4 6.8 17.2 10l-3.1-3.1 3.2-3.2a5 5 0 0 0-6.4 6.3l-7.1 7.1a2.2 2.2 0 0 0 3.1 3.1l7.1-7.1a5 5 0 0 0 6.4-6.3Z"/>'),
    "admin_server_priorities": svg_icon('<path d="M5 21V4h8.575l0.475 2.15H20v9.25H13.6l-0.475 -2.125H6.5V21h-1.5Z"/>'),
    "admin_company_routing_settings": svg_icon('<path d="M15.1 21v-3.125h-3.85v-10.25h-2.325v3.25H2V3h6.925v3.125H15.1V3H22v7.875H15.1v-3.25h-2.35v8.75h2.35v-3.25H22V21H15.1Z"/>'),
    "copy": svg_icon('<path d="M3 18v-1.5h1.5v1.5H3Zm0 -4v-1.5h1.5v1.5H3Zm0 -4v-1.5h1.5v1.5H3Zm4 12v-1.5h1.5v1.5h-1.5Zm1.5 -4c-0.4 0 -0.75 -0.15 -1.05 -0.45 -0.3 -0.3 -0.45 -0.65 -0.45 -1.05V3.5c0 -0.4 0.15 -0.75 0.45 -1.05 0.3 -0.3 0.65 -0.45 1.05 -0.45h10c0.4 0 0.75 0.15 1.05 0.45 0.3 0.3 0.45 0.65 0.45 1.05v13c0 0.4 -0.15 0.75 -0.45 1.05 -0.3 0.3 -0.65 0.45 -1.05 0.45H8.5Zm2.5 4v-1.5h1.5v1.5h-1.5ZM4.5 22c-0.4125 0 -0.765585 -0.1469 -1.05925 -0.44075C3.146915 21.2656 3 20.9125 3 20.5h1.5v1.5Zm10.5 0v-1.5h1.5c0 0.41665 -0.14685 0.77085 -0.4405 1.0625 -0.29385 0.29165 -0.647 0.4375 -1.0595 0.4375ZM3 6c0 -0.4125 0.146915 -0.76565 0.44075 -1.0595C3.734415 4.646835 4.0875 4.5 4.5 4.5v1.5H3Z"/>'),
    "import": svg_icon('<path d="M3.5 20c-0.4 0 -0.75 -0.15415 -1.05 -0.4625C2.15 19.22915 2 18.88335 2 18.5V5.5c0 -0.38335 0.15 -0.729165 0.45 -1.0375C2.75 4.154165 3.1 4 3.5 4h7.025l1.5 1.5H20.5c0.38335 0 0.72915 0.15415 1.0375 0.4625 0.30835 0.30835 0.4625 0.65415 0.4625 1.0375v11.5c0 0.38335 -0.15415 0.72915 -0.4625 1.0375 -0.30835 0.30835 -0.65415 0.4625 -1.0375 0.4625H3.5Zm7.75 -3h1.5V11.325l1.85 1.85 1.05 -1.05 -3.65 -3.65 -3.65 3.65 1.05 1.05 1.85 -1.85V17Z"/>'),
    "export": svg_icon('<path d="M5.525 15.075h3.175v-1.25h-2.75v-3.65h2.75v-1.25h-3.175c-0.23365 0 -0.4296 0.0815 -0.58775 0.2445 -0.158165 0.16285 -0.23725 0.36465 -0.23725 0.6055v4.475c0 0.23365 0.079085 0.4296 0.23725 0.58775 0.15815 0.15815 0.3541 0.23725 0.58775 0.23725Zm4.15 0h3.425c0.23385 0 0.42975 -0.0791 0.58775 -0.23725 0.15815 -0.15815 0.23725 -0.3541 0.23725 -0.58775v-1.95c0 -0.21665 -0.0791 -0.4 -0.23725 -0.55 -0.158 -0.15 -0.3539 -0.225 -0.58775 -0.225h-2.175v-1.35h3v-1.25H10.5c-0.23365 0 -0.4296 0.0791 -0.58775 0.23725 -0.15815 0.158 -0.23725 0.3539 -0.23725 0.58775v1.95c0 0.23335 0.0791 0.42915 0.23725 0.5875 0.15815 0.15835 0.3541 0.2375 0.58775 0.2375h2.175v1.3h-3v1.25Zm6.775 0h1.425l1.875 -6.15H18.5L17.175 13.5 16 8.925h-1.25l1.7 6.15ZM3.5 20c-0.4 0 -0.75 -0.15 -1.05 -0.45 -0.3 -0.3 -0.45 -0.65 -0.45 -1.05V5.5c0 -0.4 0.15 -0.75 0.45 -1.05C2.75 4.15 3.1 4 3.5 4h17c0.4 0 0.75 0.15 1.05 0.45 0.3 0.3 0.45 0.65 0.45 1.05v13c0 0.4 -0.15 0.75 -0.45 1.05 -0.3 0.3 -0.65 0.45 -1.05 0.45H3.5Z"/>'),
    "admin_users": svg_icon('<path d="M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm6.5.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM9 13c-3.6 0-6.3 2-6.3 4.4 0 .7.6 1.1 1.3 1.1h10c.7 0 1.3-.4 1.3-1.1C15.3 15 12.6 13 9 13Zm6.5.5c-.8 0-1.5.1-2.2.4 1.4.9 2.3 2.1 2.6 3.6H20c.7 0 1.3-.4 1.3-1.1 0-1.6-2.4-2.9-5.8-2.9Z"/>'),
    "admin_dictionaries": svg_icon('<path d="M5 4h6c1.1 0 2 .9 2 2v14H7c-1.1 0-2-.9-2-2V4Zm8 2c0-1.1.9-2 2-2h4v14c0 1.1-.9 2-2 2h-4V6ZM7 7v2h4V7H7Zm0 4v2h4v-2H7Z"/>'),
}


NAV_ICONS = SEMANTIC_ICONS


def nav_icon(key: str) -> str:
    return SEMANTIC_ICONS.get(key, "")


def nav_icon_span(key: str) -> str:
    icon = nav_icon(key)
    return f"<span class='nav-icon' aria-hidden='true'>{icon}</span>" if icon else ""


def user_icon_svg() -> str:
    return svg_icon("<path d='M12 12.2a4.2 4.2 0 1 0 0-8.4 4.2 4.2 0 0 0 0 8.4Zm0 1.8c-4.1 0-7.4 2.3-7.4 5.1 0 .7.6 1.1 1.3 1.1h12.2c.7 0 1.3-.4 1.3-1.1 0-2.8-3.3-5.1-7.4-5.1Z'/>")

NAV_ITEMS = [
    ("dashboard", "/dashboard", "Главная", ("Главная",)),
    ("routes", "/routes", "Маршруты", ("Маршруты", "Номера маршрута", "Редактировать маршрут")),
    ("tariffs", "/tariffs", "Тарифы", ("Тарифы",)),
    ("phones", "/phones", "Купленные номера", ("Купленные номера", "Редактировать номер")),
    ("companies", "/companies", "Кампании прозвона", ("Кампании прозвона", "Редактировать кампанию")),
    ("provider_changes", "/provider-changes", "Смена провайдеров", ("Смена провайдеров", "Редактировать событие")),
]

ADMIN_NAV_ITEMS = [
    ("admin_server_priorities", "/admin/server-priorities", "Приоритет по серверам", ("Приоритет по серверам",)),
    ("admin_company_routing_settings", "/admin/company-routing-settings", "Схема маршрутизации кампаний", ("Схема маршрутизации кампаний",)),
    ("admin_route_naming", "/admin/naming-rules", "Правила нейминга маршрутов", ("Правила нейминга",)),
    ("admin_import_export", "/admin/import", "Импорт / экспорт", ("Импорт",)),
    ("admin_currency_rates", "/admin/currency-rates", "Курсы валют", ("Курсы валют",)),
    ("admin_provider_reasons", "/admin/change-reasons", "Причины смены провайдера", ("Причины смены провайдера",)),
    ("admin_users", "/admin/users", "Пользователи", ("Пользователи",)),
    ("admin_dictionaries", "/admin/dictionaries", "Справочные значения", ("Справочные значения",)),
    ("admin_change_log", "/admin/change-log", "Change log", ("Change log",)),
]


def active_nav(title: str) -> tuple[str, str | None]:
    for key, _, _, titles in NAV_ITEMS:
        if title in titles:
            return key, None
    for _, href, _, titles in ADMIN_NAV_ITEMS:
        if title in titles:
            return "admin", href
    if title == "Администрирование":
        return "admin", None
    if title == "Главная":
        return "dashboard", None
    return "", None


def sidebar(title: str) -> str:
    active_key, active_admin_href = active_nav(title)
    admin_open = active_key == "admin"

    def nav_link(key: str, href: str, label: str) -> str:
        tooltip = "Журнал изменений" if key == "provider_changes" else esc(label)
        return (
            f"<a class='side-link {'active' if active_key == key else ''} has-inline-icon' "
            f"data-tooltip='{tooltip}' href='{href}'>"
            f"{nav_icon_span(key)}<span class='side-label'>{esc(label)}</span></a>"
        )

    main_links = "".join(nav_link(key, href, label) for key, href, label, _ in NAV_ITEMS if can_read(key))
    admin_links = "".join(
        f"<a class='admin-link {'active' if active_admin_href == href else ''}' href='{href}'>{esc(label)}</a>"
        for key, href, label, _ in ADMIN_NAV_ITEMS
        if can_read(key)
    )
    admin_toggle_html = ""
    if admin_links:
        admin_toggle_html = (
            f"<button class='side-link admin-toggle has-inline-icon {'active' if admin_open else ''}' type='button' "
            f"aria-expanded='{'true' if admin_open else 'false'}' aria-controls='admin-nav' data-tooltip='Администрирование'>"
            f"{nav_icon_span('admin')}Администрирование</button>"
        )
    return f"""
  <aside class="sidebar">
    <div class="sidebar-head">
      <div class="brand-block">
        <div class="brand-mark">⌁</div>
        <div class="brand-copy"><strong>TeleRoute</strong><span>Admin Panel</span></div>
      </div>
      <button class="sidebar-collapse" type="button" data-sidebar-toggle data-tooltip="Свернуть" aria-label="Свернуть боковую панель" title="Свернуть боковую панель"><span class="sidebar-collapse-icon" aria-hidden="true"><svg viewBox="0 0 24 24" focusable="false"><path d="M7 5l7 7-7 7M13 5l7 7-7 7"/></svg></span></button>
    </div>
    <nav class="side-nav" aria-label="Основная навигация">
      {main_links}
      {admin_toggle_html}
      <div class="admin-tree {'open' if admin_open else ''}" id="admin-nav">
        {admin_links}
      </div>
    </nav>
    <div class="sidebar-footer">
      {current_user_selector()}
      {theme_selector()}
    </div>
  </aside>"""

def role_label(role_key: str | None) -> str:
    return {
        "admin": "Admin",
        "operator": "Дежурный",
        "guest": "Гость",
        "user": "Пользователь",
    }.get(role_key or "", role_key or "—")


def current_user_selector() -> str:
    repo = _REQUEST_CONTEXT.get("repo")
    current_user_id = _REQUEST_CONTEXT.get("current_user_id")
    redirect_to = _REQUEST_CONTEXT.get("redirect_to") or "/"
    if not isinstance(repo, Repository):
        return ""
    users = repo.list_users(active_only=True)
    if not users:
        return ""
    current = next((user for user in users if int(user["id"]) == int(current_user_id or 0)), users[0])
    options_html = "".join(
        f"<option value='{user['id']}' {'selected' if int(user['id']) == int(current['id']) else ''}>{esc(user['display_name'])} · {esc(role_label(user['role_key']))}</option>"
        for user in users
    )
    return f"""
        <form class="current-user-selector" method="post" action="/users/select" aria-label="Текущий пользователь">
          <input type="hidden" name="redirect_to" value="{esc(redirect_to)}">
          <span class="side-icon user-icon" aria-hidden="true">{user_icon_svg()}</span>
          <span class="user-copy"><strong>Admin · Admin</strong><small>Администратор</small></span>
          <label class="user-select-label">Текущий пользователь
            <select name="user_id" onchange="this.form.submit()">{options_html}</select>
          </label>
          <noscript><button>Выбрать</button></noscript>
        </form>
    """

def theme_selector() -> str:
    return """
        <button class="theme-selector" type="button" data-theme-toggle data-tooltip="Светлая тема"><span class="side-icon">☼</span><span class="side-label">Светлая тема</span></button>
    """

def breadcrumbs(title: str) -> str:
    trails = {
        "Главная": [("Главная", None)],
        "Маршруты": [("Главная", "/dashboard"), ("Маршруты", None)],
        "Номера маршрута": [("Главная", "/dashboard"), ("Маршруты", "/routes"), ("Номера маршрута", None)],
        "Тарифы": [("Главная", "/dashboard"), ("Тарифы", None)],
        "Купленные номера": [("Главная", "/dashboard"), ("Купленные номера", None)],
        "Кампании прозвона": [("Главная", "/dashboard"), ("Кампании прозвона", None)],
        "Смена провайдеров": [("Главная", "/dashboard"), ("Смена провайдеров", None)],
        "Приоритет по серверам": [("Главная", "/dashboard"), ("Администрирование", "/admin"), ("Приоритет по серверам", None)],
        "Пользователи": [("Главная", "/dashboard"), ("Администрирование", "/admin"), ("Пользователи", None)],
        "Справочные значения": [("Главная", "/dashboard"), ("Администрирование", "/admin"), ("Справочные значения", None)],
        "Схема маршрутизации кампаний": [("Главная", "/dashboard"), ("Администрирование", "/admin"), ("Схема маршрутизации кампаний", None)],
        "Правила нейминга": [("Главная", "/dashboard"), ("Администрирование", "/admin"), ("Правила нейминга", None)],
        "Импорт": [("Главная", "/dashboard"), ("Администрирование", "/admin"), ("Импорт / экспорт", None)],
        "Курсы валют": [("Главная", "/dashboard"), ("Администрирование", "/admin"), ("Курсы валют", None)],
        "Причины смены провайдера": [("Главная", "/dashboard"), ("Администрирование", "/admin"), ("Причины смены провайдера", None)],
        "Change log": [("Главная", "/dashboard"), ("Администрирование", "/admin"), ("Change log", None)],
    }
    trail = trails.get(title)
    if not trail:
        return ""
    parts = []
    for label, href in trail:
        section = section_for_get_path(href or "") if href else None
        if href and (section is None or can_read(section)):
            parts.append(f"<a href='{esc(href)}'>{esc(label)}</a>")
        else:
            parts.append(f"<span>{esc(label)}</span>")
    return f"<nav class='breadcrumbs' aria-label='Хлебные крошки' data-current='{esc(title)}'>" + "<span class='separator'>→</span>".join(parts) + "</nav>"

def page(title: str, body: str, notice: str | None = None, notice_type: str = "success") -> bytes:
    notice_class = "error" if notice_type == "error" else "ok"
    notice_html = f"<div class='{notice_class}'>{esc(notice)}</div>" if notice else ""
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>{esc(title)}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/core@1.0.0-beta20/dist/css/tabler.min.css">
  <style>
    :root,
    html[data-theme="cyber-sketch"] {{
      --bg: #f7f5ff;
      --surface: #ffffff;
      --surface-muted: #fbfaff;
      --surface-strong: #f0edff;
      --sidebar-bg: #f3f0ff;
      --text-strong: #14111f;
      --text: #312f44;
      --muted: #68657d;
      --border: #dfdaf4;
      --border-strong: #c8bff0;
      --accent: #6d5dfc;
      --accent-strong: #4f46e5;
      --accent-soft: #efedff;
      --cyber: #00bfa6;
      --cyber-strong: #009e8a;
      --cyber-soft: #ddfff8;
      --pink: #ff4fd8;
      --pink-soft: #ffe8fa;
      --warning: #f59e0b;
      --warning-soft: #fff3d6;
      --danger: #ef4444;
      --danger-strong: #dc2626;
      --danger-soft: #ffe3e3;
      --success: var(--cyber);
      --success-soft: var(--cyber-soft);
      --focus: var(--accent);
      --shadow-soft: 0 1px 2px rgba(20, 17, 31, 0.07);
      --shadow-card: 0 16px 38px rgba(62, 51, 140, 0.12);
      --shadow-glow: 0 0 0 1px rgba(109, 93, 252, 0.16), 0 14px 34px rgba(109, 93, 252, 0.12);
      --radius-control: 8px;
      --radius-card: 12px;
    }}
    html[data-theme="calm-blue"] {{
      --bg: #f5f7fb;
      --surface: #ffffff;
      --surface-muted: #f8fafc;
      --surface-strong: #eef2f7;
      --sidebar-bg: #eef2f7;
      --text-strong: #111827;
      --text: #334155;
      --muted: #64748b;
      --border: #e2e8f0;
      --border-strong: #cbd5e1;
      --accent: #2563eb;
      --accent-strong: #1d4ed8;
      --accent-soft: #eff6ff;
      --cyber: #0ea5e9;
      --cyber-soft: #e0f2fe;
      --warning: #d97706;
      --warning-soft: #fef3c7;
      --danger: #dc2626;
      --danger-soft: #fee2e2;
      --success: var(--cyber);
      --success-soft: var(--cyber-soft);
      --focus: var(--accent);
    }}
    html[data-theme="terminal-paper"] {{
      --bg: #f5f2e8;
      --surface: #fffdf7;
      --surface-muted: #faf7ed;
      --surface-strong: #eee8d8;
      --sidebar-bg: #eee8d8;
      --text-strong: #1f2933;
      --text: #374151;
      --muted: #6b7280;
      --border: #ddd6c4;
      --border-strong: #c8bea6;
      --accent: #16803c;
      --accent-strong: #0f6b2f;
      --accent-soft: #e7f7ec;
      --cyber: #d97706;
      --cyber-soft: #fff3d6;
      --warning: #d97706;
      --warning-soft: #fff3d6;
      --danger: #b42318;
      --danger-soft: #ffe4df;
      --success: var(--accent);
      --success-soft: var(--accent-soft);
      --focus: var(--accent);
    }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: var(--text); background: var(--bg); font-size: 14px; line-height: 1.45; }}
    html[data-theme="cyber-sketch"] body {{ background:
      radial-gradient(circle at 16% 10%, rgba(0, 191, 166, 0.12), transparent 24rem),
      radial-gradient(circle at 88% 2%, rgba(255, 79, 216, 0.08), transparent 20rem),
      linear-gradient(rgba(109, 93, 252, 0.035) 1px, transparent 1px),
      linear-gradient(90deg, rgba(109, 93, 252, 0.035) 1px, transparent 1px),
      var(--bg); background-size: auto, auto, 28px 28px, 28px 28px, auto; }}
    .breadcrumbs {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 0 0 10px; color: var(--muted); font-size: 12px; font-weight: 650; }}
    .breadcrumbs a {{ color: var(--accent-strong); text-decoration: none; }}
    .breadcrumbs a:hover {{ text-decoration: underline; }}
    .breadcrumbs .separator {{ color: var(--muted); }}
    h1 {{ margin: 0 0 14px; font-size: 26px; line-height: 1.18; letter-spacing: -0.02em; color: var(--text-strong); font-weight: 760; }}
    h2 {{ margin: 18px 0 10px; font-size: 18px; line-height: 1.25; letter-spacing: -0.01em; color: var(--text-strong); font-weight: 740; }}
    h3 {{ margin: 14px 0 8px; font-size: 15px; letter-spacing: -0.005em; color: var(--text-strong); font-weight: 720; }}
    p {{ margin: 8px 0; }}
    .app-shell {{ display: grid; grid-template-columns: 258px minmax(0, 1fr); min-height: 100vh; }}
    .sidebar {{ background: linear-gradient(180deg, var(--surface-muted) 0%, var(--sidebar-bg) 100%); border-right: 1px solid var(--border); padding: 20px 14px; position: sticky; top: 0; height: 100vh; overflow-y: auto; }}
    .app-title {{ color: var(--text-strong); font-weight: 820; font-size: 17px; letter-spacing: -0.01em; margin: 2px 8px 20px; padding: 0 0 14px; border-bottom: 1px solid var(--border); }}
    .side-nav {{ display: grid; gap: 5px; }}
    .side-link, .admin-link, .button, button {{ border: 1px solid transparent; border-radius: var(--radius-control); color: var(--text); padding: 7px 10px; text-decoration: none; background: transparent; cursor: pointer; font: inherit; transition: background-color 120ms ease, border-color 120ms ease, color 120ms ease, box-shadow 120ms ease; }}
    .side-link {{ display: flex; width: 100%; align-items: center; justify-content: space-between; text-align: left; font-weight: 650; min-height: 36px; }}
    .side-link:hover, .admin-link:hover {{ background: color-mix(in srgb, var(--accent-soft) 62%, var(--cyber-soft)); border-color: var(--border-strong); color: var(--text-strong); }}
    .side-link.active {{ position: relative; background: linear-gradient(135deg, var(--accent-strong) 0%, var(--accent) 100%); border-color: var(--accent); color: #fff; box-shadow: var(--shadow-glow, var(--shadow-soft)); font-weight: 780; }}
    .side-link.active::before {{ content: ""; position: absolute; left: -1px; top: 8px; bottom: 8px; width: 3px; border-radius: 999px; background: var(--cyber); }}
    .admin-toggle::after {{ content: "›"; color: var(--muted); font-size: 14px; line-height: 1; }}
    .admin-toggle[aria-expanded="true"]::after {{ content: "⌄"; }}
    .admin-tree {{ display: none; margin: 4px 0 8px; padding: 6px; border: 1px solid var(--border); border-radius: 10px; background: color-mix(in srgb, var(--surface) 55%, transparent); }}
    .admin-tree.open {{ display: grid; gap: 2px; }}
    .admin-link {{ display: block; padding: 6px 8px; font-size: 13px; line-height: 1.25; color: var(--text); }}
    .admin-link.active {{ background: linear-gradient(135deg, var(--accent-soft), var(--cyber-soft)); border-color: var(--accent); color: var(--accent-strong); font-weight: 730; box-shadow: var(--shadow-soft); }}
    .workspace {{ min-width: 0; padding: 20px 28px 42px; background: transparent; }}
    .topbar {{ display: flex; justify-content: flex-end; align-items: center; gap: 10px; min-height: 40px; margin: 0 0 10px; flex-wrap: wrap; }}
    .current-user-selector label, .theme-selector {{ display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; font-weight: 700; }}
    .current-user-selector select {{ min-width: 190px; }}
    .theme-selector select {{ min-width: 138px; max-width: 150px; }}
    .content {{ max-width: 1460px; margin: 0 auto; }}
    .content > h1:first-of-type {{ margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid var(--border); }}
    a {{ color: var(--accent-strong); text-underline-offset: 2px; }}
    a:hover {{ color: var(--accent-strong); }}
    .button, button {{ background: var(--surface); border-color: var(--border-strong); color: var(--text-strong); min-height: 32px; display: inline-flex; align-items: center; justify-content: center; gap: 5px; font-weight: 650; box-shadow: var(--shadow-soft); }}
    .admin-toggle {{ background: transparent; border-color: transparent; box-shadow: none; }}
    .button svg, button svg, .action-icon svg {{ width: 16px; height: 16px; display: block; fill: currentColor; flex: 0 0 16px; }}
    .action-icon {{ display: inline-flex; align-items: center; justify-content: center; color: var(--accent-strong); margin-right: -2px; }}
    .button:hover, button:hover {{ background: var(--accent-soft); border-color: var(--accent); color: var(--accent-strong); }}
    .button:active, button:active {{ background: var(--surface-strong); }}
    .button:disabled, button:disabled, input:disabled, select:disabled, textarea:disabled {{ opacity: 0.62; cursor: not-allowed; }}
    button[onclick*="Деактив"], button[onclick*="Удал"], button[onclick*="Отключ"], form[action$="/deactivate"] button {{ color: var(--danger); border-color: var(--danger); background: var(--danger-soft); }}
    button[onclick*="Деактив"]:hover, button[onclick*="Удал"]:hover, button[onclick*="Отключ"]:hover, form[action$="/deactivate"] button:hover {{ background: var(--danger-soft); border-color: var(--danger); }}
    .button:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, summary:focus-visible, a:focus-visible {{ outline: 2px solid var(--focus); outline-offset: 2px; }}
    table {{ border-collapse: separate; border-spacing: 0; width: max-content; min-width: 100%; background: var(--surface); }}
    th, td {{ border: 0; border-bottom: 1px solid var(--border); padding: 5px 10px; vertical-align: top; line-height: 1.25; }}
    tr:last-child td {{ border-bottom: 0; }}
    th {{ background: linear-gradient(180deg, var(--surface-muted) 0%, var(--surface-strong) 100%); text-align: left; font-weight: 760; color: var(--text); position: sticky; top: 0; z-index: 1; font-size: 12px; letter-spacing: .015em; white-space: nowrap; }}
    .copyable-header {{ display: inline-flex; align-items: center; gap: 6px; }}
    .copy-column-button {{ min-height: 24px; padding: 2px 6px; font-size: 12px; color: var(--accent-strong); border-color: var(--border-strong); background: var(--accent-soft); box-shadow: none; }}
    .copy-column-button svg {{ width: 15px; height: 15px; }}
    .copy-column-button:hover {{ background: var(--cyber-soft); border-color: var(--accent); }}
    .copy-column-status {{ margin-left: 4px; color: var(--success); font-size: 12px; font-weight: 720; white-space: nowrap; }}
    tbody tr:nth-child(even) {{ background: var(--surface-muted); }}
    tbody tr:hover {{ background: var(--accent-soft); }}
    tbody tr:hover td {{ border-bottom-color: var(--border-strong); }}
    td {{ max-width: 360px; overflow: hidden; text-overflow: ellipsis; overflow-wrap: normal; font-weight: 400; }}
    .table-card td, .journal-card td {{ white-space: nowrap; }}
    .table-card td[data-copy-column="phone-number"], .table-card td:nth-child(1), .table-card td:nth-child(2), .status-badge {{ white-space: nowrap; }}
    .table-card td.comment-cell, .journal-card td.comment-cell,
    .table-card td[data-col="route"], .table-card td[data-col="routes"], .table-card td[data-col="company_name"],
    .journal-card td[data-col="details"], .journal-card td[data-col="reason"], .journal-card td[data-col="comment"] {{
      min-width: 180px; max-width: 360px; white-space: normal; overflow: hidden; overflow-wrap: anywhere; word-break: normal;
    }}
    .cell-clamp {{ display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; }}
    td[data-full-text] {{ cursor: pointer; }}
    td[data-full-text]:hover .cell-clamp {{ color: var(--accent-strong); }}
    .cell-popover {{ position: fixed; z-index: 1000; width: min(640px, calc(100vw - 24px)); max-height: 340px; display: grid; grid-template-rows: auto minmax(0, 1fr); gap: 8px; padding: 10px; border: 1px solid var(--border-strong); border-radius: var(--radius-control); background: var(--surface); box-shadow: 0 18px 45px rgba(15, 23, 42, .16), 0 4px 12px rgba(15, 23, 42, .10); color: var(--text); }}
    .cell-popover-header {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
    .cell-popover-actions {{ display: inline-flex; align-items: center; gap: 6px; margin-left: auto; }}
    .cell-popover-copy, .cell-popover-close {{ min-height: 26px; padding: 3px 8px; font-size: 12px; }}
    .cell-popover-close {{ width: 28px; padding: 0; font-size: 18px; line-height: 1; }}
    .cell-popover-text {{ max-height: 280px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; user-select: text; font-size: 13px; line-height: 1.35; }}
    input, select, textarea {{ border: 1px solid var(--border-strong); border-radius: var(--radius-control); padding: 6px 8px; margin: 0; max-width: 100%; background: var(--surface); color: var(--text-strong); font: inherit; min-height: 32px; box-shadow: inset 0 1px 1px rgba(34, 48, 42, 0.03); }}
    input:hover, select:hover, textarea:hover {{ border-color: var(--accent); }}
    input:focus, select:focus, textarea:focus {{ border-color: var(--focus); background: var(--surface); }}
    input::placeholder, textarea::placeholder {{ color: var(--muted); }}
    textarea {{ width: 100%; }}
    input[type="checkbox"], input[type="radio"] {{ width: auto; margin: 0 6px 0 0; vertical-align: middle; accent-color: var(--accent); }}
    label {{ display: inline-grid; gap: 4px; margin: 0; align-items: start; color: var(--text); font-weight: 620; }}
    form {{ display: flex; flex-wrap: wrap; gap: 8px 10px; align-items: end; }}
    form button {{ align-self: end; }}
    .checkbox-list {{ display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 4px 0; }}
    .checkbox-list label {{ margin: 0; font-weight: 520; }}
    .server-checkbox-toolbar {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 0 0 8px; flex-wrap: wrap; }}
    .server-checkbox-actions {{ display: inline-flex; gap: 6px; flex-wrap: wrap; }}
    .server-checkbox-toolbar button {{ min-height: 28px; padding: 3px 9px; border-radius: var(--radius-control); font-size: 12px; font-weight: 620; box-shadow: none; }}
    .server-selection-count {{ color: var(--muted); font-size: 12px; font-weight: 620; white-space: nowrap; }}
    .server-checkbox-grid {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }}
    .server-checkbox-item {{ min-height: 36px; display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--border-strong); border-radius: var(--radius-control); padding: 7px 12px; background: var(--surface); margin: 0; font-weight: 720; line-height: 1; cursor: pointer; box-shadow: inset 0 1px 1px rgba(34, 48, 42, 0.03); transition: border-color 140ms ease, background 140ms ease, box-shadow 140ms ease, color 140ms ease; }}
    .server-checkbox-item:hover {{ border-color: var(--accent); background: var(--surface-muted); }}
    .server-checkbox-item input[type="checkbox"] {{ position: absolute; opacity: 0; pointer-events: none; }}
    .server-checkbox-item:has(input:checked) {{ border-color: var(--accent); background: var(--accent-soft); color: var(--text-strong); box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 24%, transparent) inset; }}
    .server-checkbox-item:has(input:checked)::before {{ content: "✓"; display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; border-radius: 4px; background: var(--accent); color: #fff; font-size: 11px; font-weight: 820; }}
    .server-checkbox-copy {{ min-width: 0; display: inline-flex; align-items: center; }}
    .server-checkbox-main {{ font-weight: 760; line-height: 1.15; color: inherit; }}
    .server-current-routes {{ display: grid; gap: 5px; max-height: 210px; overflow: auto; margin-top: 10px; padding: 9px 10px; border: 1px solid var(--border); border-radius: var(--radius-control); background: var(--surface); }}
    .server-current-route-row {{ display: grid; grid-template-columns: 46px minmax(0, 1fr); gap: 8px; align-items: baseline; min-height: 22px; font-size: 13px; line-height: 1.3; }}
    .server-current-route-name {{ color: var(--accent-strong); font-weight: 800; }}
    .server-current-route-text {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .server-current-route-label {{ color: var(--muted); }}
    .server-current-routes-empty {{ color: var(--muted); font-size: 13px; }}
    .server-route-hint {{ display: none; }}
    .event-server-list {{ margin: 4px 0 0 18px; padding: 0; }}
    .event-server-list li {{ margin: 2px 0; }}
    fieldset {{ border: 1px solid var(--border); border-radius: var(--radius-card); margin: 12px 0; padding: 12px; background: var(--surface); }}
    fieldset > legend {{ padding: 0 6px; color: var(--text); font-weight: 750; }}
    h1 + fieldset, h1 + p + fieldset {{ margin-top: 6px; }}
    .required {{ color: var(--danger); font-weight: 760; }}
    .muted {{ color: var(--muted); font-weight: 500; }}
    .message-card, .error {{ border: 1px solid var(--danger); background: var(--danger-soft); color: var(--danger); padding: 14px; border-radius: var(--radius-card); }}
    .message-card h1 {{ border: 0; padding: 0; margin-bottom: 8px; }}
    .ok {{ border: 1px solid var(--cyber); background: var(--success-soft); color: var(--text-strong); padding: 12px 14px; border-radius: var(--radius-card); margin: 10px 0 14px; box-shadow: var(--shadow-soft); }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }}
    .card {{ border: 1px solid var(--border); border-radius: var(--radius-card); padding: 14px; background: var(--surface); box-shadow: var(--shadow-soft); transition: transform 140ms ease, border-color 140ms ease, box-shadow 140ms ease; }}
    .card:hover {{ transform: translateY(-1px); border-color: var(--accent); box-shadow: var(--shadow-glow, var(--shadow-card)); }}
    details {{ border: 1px solid var(--border); border-radius: var(--radius-card); padding: 0; margin: 12px 0; background: var(--surface); box-shadow: var(--shadow-soft); }}
    summary {{ cursor: pointer; padding: 8px 12px; font-weight: 750; color: var(--text-strong); }}
    details[open] > summary {{ border-bottom: 1px solid var(--border); background: var(--surface-muted); border-radius: var(--radius-card) var(--radius-card) 0 0; }}
    details > form, details > .card, details > textarea, details > p, details > table {{ margin: 12px; }}
    .filter-card, .form-card {{ border-color: var(--border); box-shadow: var(--shadow-soft); }}
    .filter-card {{ margin: 8px 0 12px; background: color-mix(in srgb, var(--surface) 86%, transparent); }}
    .filter-summary, .form-summary {{ min-height: 38px; display: flex; align-items: center; justify-content: space-between; }}
    .filter-summary::after, .form-summary::after {{ content: "Настроить"; color: var(--muted); font-size: 12px; font-weight: 650; }}
    details[open] > .filter-summary::after, details[open] > .form-summary::after {{ content: "Свернуть"; }}
    .filter-grid, .form-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, max-content)); gap: 10px 12px; align-items: end; padding: 14px; }}
    .filter-grid {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: end; }}
    .filter-grid label, .form-grid label {{ min-width: 150px; }}
    .filter-grid input, .filter-grid select, .form-grid input, .form-grid select {{ width: 100%; }}
    .filter-grid .checkbox-inline, .form-grid .checkbox-inline {{ min-width: auto; display: flex; align-items: center; gap: 5px; align-self: center; font-weight: 560; }}
    .form-grid .wide, .filter-grid .wide {{ grid-column: 1 / -1; }}
    .form-grid fieldset, .filter-grid fieldset {{ grid-column: 1 / -1; margin: 0; }}
    .form-grid textarea {{ min-width: min(620px, 100%); }}
    .filter-grid button[type="submit"], .filter-grid > button, .form-grid button[type="submit"], .form-grid > button {{ background: linear-gradient(135deg, var(--accent-strong), var(--accent)); border-color: var(--accent-strong); color: #fff; }}
    .filter-grid button[type="submit"]:hover, .filter-grid > button:hover, .form-grid button[type="submit"]:hover, .form-grid > button:hover {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    .form-grid button[onclick*="Деактив"], .form-grid button[onclick*="Удал"], .form-grid button[onclick*="Отключ"], .filter-grid button[onclick*="Деактив"], .filter-grid button[onclick*="Удал"], .filter-grid button[onclick*="Отключ"] {{ color: var(--danger); border-color: var(--danger); background: var(--danger-soft); }}
    .form-grid button[onclick*="Деактив"]:hover, .form-grid button[onclick*="Удал"]:hover, .form-grid button[onclick*="Отключ"]:hover, .filter-grid button[onclick*="Деактив"]:hover, .filter-grid button[onclick*="Удал"]:hover, .filter-grid button[onclick*="Отключ"]:hover {{ background: var(--danger-soft); border-color: var(--danger); color: var(--danger); }}
    .reset-filters {{ background: var(--surface-muted); color: var(--accent-strong); }}
    .table-footer {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; margin: 8px 0 12px; }}
    .table-footer-summary p, .table-footer-summary nav {{ margin: 0; }}
    .table-footer-tools {{ display: flex; align-items: center; justify-content: flex-end; gap: 6px; flex-wrap: wrap; margin-left: auto; }}
    .table-utility-button, .table-footer-tools .column-settings > summary {{ min-height: 28px; padding: 4px 8px; font-size: 12px; font-weight: 650; }}
    .column-settings {{ position: relative; display: inline-block; }}
    .column-settings summary {{ min-height: 28px; padding: 4px 9px; border: 1px solid var(--border-strong); border-radius: var(--radius-control); background: var(--surface); color: var(--accent-strong); font-size: 12px; font-weight: 720; list-style: none; box-shadow: 0 1px 0 rgba(34, 48, 42, 0.03); }}
    .column-settings summary::-webkit-details-marker {{ display: none; }}
    .column-settings[open] summary, .column-settings summary:hover {{ background: var(--surface-muted); border-color: var(--accent); }}
    .column-settings-panel {{ position: absolute; right: 0; top: calc(100% + 6px); z-index: 5; display: grid; gap: 4px; min-width: 210px; max-height: 320px; overflow: auto; padding: 10px; border: 1px solid var(--border-strong); border-radius: var(--radius-control); background: var(--surface); box-shadow: var(--shadow-card); }}
    .column-settings-panel label {{ display: flex; align-items: center; gap: 6px; margin: 0; font-size: 13px; font-weight: 560; white-space: nowrap; }}
    .column-reset {{ justify-content: flex-start; margin-top: 5px; padding: 4px 0; min-height: 24px; border: 0; background: transparent; box-shadow: none; color: var(--accent-strong); font-size: 12px; }}
    [data-column-hidden="true"] {{ display: none !important; }}
    .table-card, .journal-card {{ border: 1px solid var(--border-strong); border-radius: var(--radius-card); background: var(--surface); margin: 12px 0; overflow: hidden; box-shadow: var(--shadow-card); }}
    .table-card h2, .journal-card h2 {{ margin: 0; padding: 12px 14px; border-bottom: 1px solid var(--border); background: var(--surface-muted); color: var(--text-strong); }}
    .journal-card h2 {{ font-size: 19px; }}
    .table-scroll {{ overflow-x: auto; overscroll-behavior-x: contain; }}
    .table-card table, .journal-card table {{ margin: 0; border: 0; border-radius: 0; }}
    .table-card .button, .journal-card .button, .table-card button, .journal-card button {{ min-height: 28px; padding: 3px 8px; font-size: 12px; }}
    .journal-card {{ min-height: 420px; border-color: var(--border-strong); }}
    .journal-card .table-scroll {{ min-height: 360px; }}
    .empty-state {{ padding: 24px 14px; color: var(--muted); background: var(--surface-muted); }}
    .compact-actions, .actions {{ white-space: nowrap; }}
    .scope-cards {{ display: grid; grid-template-columns: repeat(3, minmax(190px, 1fr)); gap: 8px; }}
    .scope-card {{ min-height: 58px; cursor: pointer; display: flex; align-items: center; gap: 9px; padding: 9px 10px; border-radius: var(--radius-control); box-shadow: none; font-weight: 650; line-height: 1.2; }}
    .scope-card:hover {{ transform: none; background: var(--surface-muted); }}
    .scope-card input[type="radio"] {{ position: absolute; opacity: 0; pointer-events: none; }}
    .scope-card-indicator {{ flex: 0 0 18px; width: 18px; height: 18px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--border-strong); border-radius: 999px; background: var(--surface); transition: border-color 140ms ease, background 140ms ease; }}
    .scope-card-indicator::after {{ content: ""; width: 8px; height: 8px; border-radius: 999px; background: var(--accent); opacity: 0; transform: scale(.6); transition: opacity 140ms ease, transform 140ms ease; }}
    .scope-card-text {{ min-width: 0; }}
    .scope-card.selected {{ border-color: var(--accent); background: var(--accent-soft); box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 24%, transparent) inset; color: var(--text-strong); }}
    .scope-card.selected .scope-card-indicator {{ border-color: var(--accent); background: var(--surface); }}
    .scope-card.selected .scope-card-indicator::after {{ opacity: 1; transform: scale(1); }}
    .scope-field[hidden], .conditional-field[hidden], .route-empty-message[hidden] {{ display: none !important; }}
    .current-route-box {{ display: block; border: 1px dashed var(--border-strong); border-radius: var(--radius-card); padding: 8px; margin: 4px 12px 4px 0; background: var(--surface-muted); }}
    .star {{ color: var(--warning); font-weight: 800; }}
    .dictionary-layout {{ display: grid; grid-template-columns: minmax(210px, 260px) minmax(0, 1fr); gap: 16px; align-items: start; }}
    .dictionary-sidebar {{ display: grid; gap: 6px; position: sticky; top: 12px; }}
    .dictionary-sidebar-title {{ margin: 0 0 2px; color: var(--muted); font-size: 12px; font-weight: 780; text-transform: uppercase; letter-spacing: .04em; }}
    .dictionary-card {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; border: 1px solid var(--border); border-radius: 10px; padding: 7px 9px; background: var(--surface); color: var(--text); text-decoration: none; box-shadow: none; }}
    .dictionary-card:hover {{ border-color: var(--border-strong); background: var(--surface-muted); }}
    .dictionary-card.active {{ border-color: var(--accent); background: var(--accent-soft); box-shadow: 0 0 0 2px var(--border-strong) inset; color: var(--text-strong); }}
    .dictionary-card-title {{ font-weight: 760; color: inherit; text-decoration: none; }}
    .dictionary-card-count {{ color: var(--muted); font-size: 12px; white-space: nowrap; }}
    .dictionary-workspace {{ min-width: 0; display: grid; gap: 10px; }}
    .dictionary-toolbar {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; border: 1px solid var(--border); border-radius: var(--radius-card); padding: 10px 12px; background: var(--surface); box-shadow: var(--shadow-soft); }}
    .dictionary-toolbar h2 {{ margin: 0; }}
    .dictionary-total {{ color: var(--muted); font-weight: 700; white-space: nowrap; }}
    .dictionary-add {{ margin: 0; box-shadow: var(--shadow-soft); }}
    .dictionary-add .form-grid {{ grid-template-columns: repeat(auto-fit, minmax(170px, 260px)); }}
    .dictionary-add input, .dictionary-add select {{ width: 100%; box-sizing: border-box; }}
    .inactive-row {{ color: var(--muted); background: var(--surface-strong); }}
    .status-badge {{ display: inline-flex; align-items: center; min-height: 22px; padding: 2px 7px; border: 1px solid var(--cyber); border-radius: 999px; background: var(--cyber-soft); color: var(--text-strong); font-size: 12px; font-weight: 720; white-space: nowrap; }}

    .dashboard-hero {{ position: relative; overflow: hidden; display: flex; align-items: center; justify-content: space-between; gap: 18px; border: 1px solid var(--border-strong); border-radius: 18px; padding: 26px; margin: 0 0 18px; background:
      radial-gradient(circle at 82% 20%, rgba(0, 191, 166, 0.20), transparent 16rem),
      linear-gradient(135deg, var(--surface) 0%, var(--surface-strong) 58%, var(--cyber-soft) 100%); box-shadow: var(--shadow-glow, var(--shadow-card)); }}
    .dashboard-hero::after {{ content: ""; position: absolute; inset: 0; pointer-events: none; background: linear-gradient(rgba(79, 70, 229, 0.045) 1px, transparent 1px), linear-gradient(90deg, rgba(79, 70, 229, 0.045) 1px, transparent 1px); background-size: 22px 22px; mask-image: linear-gradient(120deg, transparent, #000 18%, #000 70%, transparent); }}
    .dashboard-hero > * {{ position: relative; z-index: 1; }}
    .dashboard-hero h1 {{ margin: 0 0 8px; padding: 0; border: 0; font-size: 34px; }}
    .eyebrow {{ margin: 0 0 6px; color: var(--accent-strong); font-size: 12px; font-weight: 820; text-transform: uppercase; letter-spacing: .08em; }}
    .hero-text {{ max-width: 760px; margin: 0; color: var(--muted); font-size: 16px; }}
    .hero-action {{ background: var(--accent-strong); border-color: var(--accent-strong); color: #fff; white-space: nowrap; }}
    .hero-action:hover {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin: 0 0 18px; }}
    .metric-card {{ border: 1px solid var(--border-strong); border-left: 4px solid var(--cyber); border-radius: var(--radius-card); padding: 15px; background: linear-gradient(180deg, var(--surface), var(--surface-muted)); box-shadow: var(--shadow-soft); }}
    .metric-label {{ display: block; min-height: 36px; color: var(--muted); font-size: 12px; font-weight: 760; text-transform: uppercase; letter-spacing: .04em; }}
    .metric-value {{ display: block; margin: 6px 0 3px; color: var(--text-strong); font-size: 30px; line-height: 1; letter-spacing: -0.03em; }}
    .metric-hint {{ color: var(--muted); font-size: 12px; }}
    .dashboard-section {{ margin: 16px 0; }}
    .dashboard-section h2 {{ margin-bottom: 10px; }}
    .quick-links {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px; }}
    .quick-link-card {{ display: grid; gap: 4px; min-height: 84px; border: 1px solid var(--border); border-radius: var(--radius-card); padding: 13px 14px; background: var(--surface); color: var(--text); text-decoration: none; box-shadow: var(--shadow-soft); }}
    .quick-link-card:hover {{ transform: translateY(-1px); border-color: var(--accent); background: linear-gradient(135deg, var(--accent-soft), var(--cyber-soft)); color: var(--text-strong); text-decoration: none; box-shadow: var(--shadow-glow, var(--shadow-card)); }}
    .quick-link-card span {{ font-weight: 780; }}
    .quick-link-card small {{ color: var(--muted); line-height: 1.35; }}
    .table-scroll {{ max-height: calc(100vh - 270px); overflow: auto; position: relative; }}
    th[data-col="actions"], td[data-col="actions"], .dictionary-workspace th:last-child {{ position: sticky; right: 0; z-index: 2; width: 64px; min-width: 64px; max-width: 72px; text-align: center; box-shadow: -10px 0 16px rgba(34, 48, 42, 0.08); }}
    th[data-col="actions"], .dictionary-workspace th:last-child {{ z-index: 4; padding-left: 8px; padding-right: 8px; }}
    td[data-col="actions"] {{ background: inherit; padding: 6px 8px; overflow: visible; }}
    tbody tr:nth-child(even) td[data-col="actions"] {{ background: var(--surface-muted); }}
    tbody tr:hover td[data-col="actions"] {{ background: var(--accent-soft); }}
    .actions, .compact-actions, td[data-col="actions"] {{ white-space: nowrap; text-align: center; }}
    td[data-col="actions"] form {{ justify-content: center; }}
    .route-numbers-action {{ min-height: 28px; padding: 4px 8px; font-size: 12px; box-shadow: none; }}
    .action-button, .actions .button, .actions button, .compact-actions .button, .compact-actions button, td[data-col="actions"] .button, td[data-col="actions"] button {{ min-width: 30px; min-height: 30px; padding: 4px 7px; border-radius: 8px; font-size: 12px; line-height: 1; box-shadow: none; }}
    .edit-action, td[data-col="actions"] details.edit-details > summary {{ position: relative; width: 32px; min-width: 32px; height: 32px; min-height: 32px; padding: 0; display: inline-flex; align-items: center; justify-content: center; overflow: visible; color: transparent; font-size: 0; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--surface); box-shadow: none; list-style: none; }}
    .edit-action:hover, td[data-col="actions"] details.edit-details > summary:hover {{ background: var(--accent-soft); border-color: var(--accent); color: transparent; }}
    .edit-action::before, td[data-col="actions"] details.edit-details > summary::before {{ content: "✏️"; color: var(--accent-strong); font-size: 14px; line-height: 1; }}
    td[data-col="actions"] details.edit-details > summary::-webkit-details-marker {{ display: none; }}
    td[data-col="actions"] details.edit-details > summary::marker {{ content: ""; display: none; }}
    td[data-col="actions"] details {{ position: relative; display: inline-block; margin: 0; border: 0; background: transparent; box-shadow: none; }}
    td[data-col="actions"] details.edit-details[open] > summary {{ border-radius: 8px; background: var(--accent-soft); border-color: var(--accent); }}
    td[data-col="actions"] details:not(.edit-details) > summary {{ width: 32px; min-width: 32px; height: 30px; min-height: 30px; padding: 0; display: inline-flex; align-items: center; justify-content: center; overflow: hidden; color: transparent; font-size: 0; border: 1px solid var(--danger); border-radius: 8px; background: var(--danger-soft); box-shadow: none; list-style: none; }}
    td[data-col="actions"] details:not(.edit-details) > summary::before {{ content: "!"; color: var(--danger-strong, var(--danger)); font-size: 14px; font-weight: 820; line-height: 1; }}
    td[data-col="actions"] details:not(.edit-details) > summary::-webkit-details-marker {{ display: none; }}
    td[data-col="actions"] details:not(.edit-details)[open] > summary, td[data-col="actions"] details:not(.edit-details) > summary:hover {{ background: color-mix(in srgb, var(--danger-soft) 78%, var(--surface)); border-color: var(--danger); }}
    td[data-col="actions"] details > form {{ text-align: left; }}
    td[data-col="actions"] details[open] > form {{ position: absolute; right: 0; top: calc(100% + 6px); z-index: 20; min-width: 280px; max-width: min(520px, 70vw); margin: 0; padding: 12px; border: 1px solid var(--border-strong); border-radius: var(--radius-card); background: var(--surface); box-shadow: var(--shadow-card); }}
    .danger-action, form[action$="/deactivate"] button {{ min-height: 28px; min-width: auto; padding: 4px 8px; color: var(--danger-strong, var(--danger)); border-color: var(--danger); background: var(--danger-soft); font-size: 12px; font-weight: 720; box-shadow: none; }}
    .danger-action:hover, form[action$="/deactivate"] button:hover {{ background: color-mix(in srgb, var(--danger-soft) 78%, var(--surface)); border-color: var(--danger); color: var(--danger); }}
    html[data-theme="calm-blue"] .side-link:hover, html[data-theme="calm-blue"] .admin-link:hover, html[data-theme="terminal-paper"] .side-link:hover, html[data-theme="terminal-paper"] .admin-link:hover {{ background: color-mix(in srgb, var(--surface) 78%, transparent); border-color: var(--border); color: var(--text-strong); }}
    html[data-theme="calm-blue"] .side-link.active, html[data-theme="terminal-paper"] .side-link.active {{ background: var(--surface); border-color: var(--border-strong); color: var(--accent-strong); box-shadow: var(--shadow-soft); font-weight: 780; }}
    html[data-theme="calm-blue"] .side-link.active::before, html[data-theme="terminal-paper"] .side-link.active::before {{ content: none; }}
    html[data-theme="calm-blue"] .admin-link.active, html[data-theme="terminal-paper"] .admin-link.active {{ background: var(--surface); border-color: var(--border-strong); color: var(--accent-strong); font-weight: 730; box-shadow: var(--shadow-soft); }}
    html[data-theme="calm-blue"] .dashboard-hero, html[data-theme="terminal-paper"] .dashboard-hero {{ background: linear-gradient(135deg, var(--surface) 0%, var(--accent-soft) 100%); box-shadow: var(--shadow-card); }}
    html[data-theme="calm-blue"] .dashboard-hero::after, html[data-theme="terminal-paper"] .dashboard-hero::after {{ content: none; }}
    html[data-theme="calm-blue"] .metric-card, html[data-theme="terminal-paper"] .metric-card {{ border: 1px solid var(--border); background: var(--surface); box-shadow: var(--shadow-soft); }}
    html[data-theme="calm-blue"] .status-badge, html[data-theme="terminal-paper"] .status-badge {{ border-color: var(--border); background: var(--surface-muted); color: var(--text); }}
    html[data-theme="cyber-sketch"] .theme-selector select:focus, html[data-theme="cyber-sketch"] .theme-selector select:focus-visible {{ border-color: var(--accent); outline-color: var(--accent); box-shadow: 0 0 0 3px rgba(109, 93, 252, 0.14); }}
    html[data-theme="cyber-sketch"] th {{ background: var(--surface-strong); color: var(--text-strong); }}
    html[data-theme="cyber-sketch"] tbody tr:hover {{ background: rgba(109, 93, 252, 0.08); }}
    html[data-theme="cyber-sketch"] .table-card td[data-copy-column="phone-number"], html[data-theme="cyber-sketch"] code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    html[data-theme="cyber-sketch"] .hero-action {{ background: linear-gradient(135deg, var(--accent-strong), var(--accent)); box-shadow: var(--shadow-glow); }}
    html[data-theme="cyber-sketch"] .eyebrow {{ display: inline-flex; align-items: center; gap: 6px; color: var(--cyber-strong); background: var(--cyber-soft); border: 1px solid rgba(0, 191, 166, 0.28); border-radius: 999px; padding: 3px 8px; }}
    html[data-theme="cyber-sketch"] .eyebrow::before {{ content: "◈"; color: var(--cyber); }}
    html[data-theme="cyber-sketch"] .quick-link-card {{ transition: transform 140ms ease, border-color 140ms ease, box-shadow 140ms ease, background 140ms ease; }}

    /* Figma-inspired light operations admin */
    :root, html[data-theme="calm-blue"] {{
      --bg: #eef3fb; --surface: #ffffff; --surface-muted: #f8fafe; --surface-strong: #f1f5ff;
      --sidebar-bg: #ffffff; --text-strong: #0f172a; --text: #172554; --muted: #7180a4;
      --border: #e3eaf7; --border-strong: #d9e3f5; --accent: #4661f2; --accent-strong: #2547e8;
      --accent-soft: #eef1ff; --success: #22c55e; --success-soft: #eafaf1; --warning: #f59e0b;
      --warning-soft: #fff7e8; --danger: #ef4444; --danger-soft: #fff0f0;
      --shadow-soft: 0 3px 10px rgba(28, 42, 74, .05); --shadow-card: 0 10px 24px rgba(32, 50, 90, .08);
      --radius-control: 8px; --radius-card: 14px;
    }}
    body {{ background: var(--bg); color: var(--text); font-size: 14px; }}
    .app-shell {{ grid-template-columns: 252px minmax(0, 1fr); background: var(--bg); }}
    .workspace {{ padding: 0; min-width: 0; }}
    .content {{ padding: 18px 30px 42px; }}
    .content > h1 {{ display: none; }}
    .breadcrumbs {{ margin: -18px -30px 20px; padding: 12px 30px 10px; min-height: 60px; display: flex; align-content: center; border-bottom: 1px solid var(--border); background: #f3f6fc; }}
    .breadcrumbs::after {{ content: attr(data-current); display: block; flex-basis: 100%; color: var(--text-strong); font-size: 16px; font-weight: 800; }}
    .breadcrumbs .separator {{ font-size: 0; }} .breadcrumbs .separator::before {{ content: '›'; font-size: 12px; }}
    .sidebar {{ display: flex; flex-direction: column; gap: 18px; padding: 14px 12px; background: #fff; height: 100vh; overflow: visible; z-index: 100; }}
    .sidebar-head {{ display: grid; grid-template-columns: minmax(0, 1fr) 42px; gap: 8px; align-items: start; padding-bottom: 14px; border-bottom: 1px solid var(--border); }}
    .brand-block {{ display: flex; align-items: center; gap: 12px; padding: 0 0 0 10px; border-bottom: 0; }}
    .brand-mark, .side-icon, .metric-icon, .quick-icon, .feed-icon {{ display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; }}
    .brand-mark {{ width: 36px; height: 36px; border-radius: 11px; background: linear-gradient(135deg,#4f46e5,#3525c8); color: #fff; box-shadow: 0 8px 18px rgba(79,70,229,.25); font-weight: 900; }}
    .brand-copy strong, .brand-copy span {{ display: block; }} .brand-copy strong {{ color: var(--text-strong); }} .brand-copy span {{ color: var(--muted); font-size: 12px; }}
    .app-title, .topbar {{ display: none; }}
    .side-nav {{ gap: 8px; }}
    .side-link {{ justify-content: flex-start; gap: 12px; min-height: 48px; padding: 10px 14px; border-radius: 12px; color: #223158; font-weight: 700; }}
    .side-icon {{ width: 22px; height: 22px; color: #7786ad; font-size: 18px; }}
    .nav-icon {{ display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; flex: 0 0 24px; color: #7786ad; }}
    .nav-icon svg {{ width: 22px; height: 22px; display: block; fill: currentColor; }}
    .metric-icon svg, .quick-icon svg, .feed-icon svg {{ width: 22px; height: 22px; display: block; fill: currentColor; }}
    .metric-icon svg {{ width: 21px; height: 21px; }}
    .feed-icon svg {{ width: 19px; height: 19px; }}
    .side-link:hover .nav-icon, .side-link.active .nav-icon {{ color: var(--accent-strong); }}
    .side-link.has-inline-icon::before, .side-link.has-inline-icon.active::before {{ content: none; display: none; }}
    .side-link::before {{ content: attr(data-icon); width: 22px; height: 22px; display: inline-flex; align-items: center; justify-content: center; color: #7786ad; font-size: 18px; position: static; flex: 0 0 22px; border-radius: 0; background: transparent; }}
    .side-link.active::before {{ content: attr(data-icon); color: var(--accent-strong); position: static; width: 22px; height: 22px; flex: 0 0 22px; background: transparent; }}
    .side-link:hover {{ background: #f3f6ff; color: var(--accent-strong); }}
    .side-link.active {{ background: #eef1ff; border-color: #d5ddff; color: var(--accent-strong); box-shadow: none; }}
    .side-link.active .side-icon {{ color: var(--accent-strong); }}
    .admin-tree {{ margin: 0 0 0 34px; padding-left: 10px; border-left: 1px solid var(--border); }}
    .admin-link {{ display: block; padding: 7px 10px; font-size: 12px; }}
    .sidebar-footer {{ margin-top: auto; display: grid; gap: 10px; padding-top: 12px; border-top: 1px solid var(--border); }}
    .current-user-selector, .theme-selector, .sidebar-collapse {{ display: flex; align-items: center; gap: 10px; width: 100%; min-height: 42px; padding: 8px 10px; border: 1px solid transparent; border-radius: 12px; background: transparent; color: var(--text); text-align: left; }}
    .sidebar-collapse {{ width: 36px; min-width: 36px; max-width: 36px; height: 36px; min-height: 36px; padding: 0; justify-content: center; justify-self: end; color: #223158; border-color: var(--border); background: var(--surface); box-shadow: var(--shadow-soft); overflow: hidden; }}
    .sidebar-collapse:hover {{ background: var(--accent-soft); border-color: var(--accent); color: var(--accent-strong); }}
    .sidebar-collapse-icon {{ width: 18px; height: 18px; display: inline-flex; align-items: center; justify-content: center; transform: scaleX(-1); }}
    .sidebar-collapse-icon svg {{ width: 18px; height: 18px; display: block; fill: none; stroke: currentColor; stroke-width: 2.4; stroke-linecap: round; stroke-linejoin: round; }}
    .current-user-selector {{ background: #f4f6ff; border-color: #e2e8ff; }} .current-user-selector .user-select-label {{ position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; overflow: hidden; }} .user-icon {{ background: #4f46e5; color: #fff; border-radius: 9px; width: 32px; height: 32px; }} .user-icon svg {{ width: 19px; height: 19px; display: block; fill: currentColor; }}
    .user-copy strong, .user-copy small {{ display: block; }} .user-copy small {{ color: var(--muted); }}
    .app-shell.sidebar-collapsed {{ grid-template-columns: 70px minmax(0, 1fr); }}
    .sidebar-collapsed .sidebar {{ padding-left: 8px; padding-right: 8px; }}
    .sidebar-collapsed .brand-copy, .sidebar-collapsed .side-label, .sidebar-collapsed .user-copy, .sidebar-collapsed .admin-tree {{ display: none; }}
    .sidebar-collapsed .side-link {{ font-size: 0; gap: 0; }}
    .sidebar-collapsed .sidebar-head {{ grid-template-columns: 1fr; gap: 10px; }}
    .sidebar-collapsed .sidebar-collapse {{ order: -1; justify-self: center; }}
    .sidebar-collapsed .sidebar-collapse-icon {{ transform: none; color: var(--accent-strong); }}
    .sidebar-collapsed .brand-block, .sidebar-collapsed .side-link, .sidebar-collapsed .current-user-selector, .sidebar-collapsed .theme-selector {{ justify-content: center; padding-left: 0; padding-right: 0; }}
    .sidebar-collapsed [data-tooltip] {{ position: relative; }}
    .sidebar-collapsed [data-tooltip]:hover::after {{ content: attr(data-tooltip); position: absolute; left: calc(100% + 10px); top: 50%; transform: translateY(-50%); z-index: 10000; pointer-events: none; white-space: nowrap; border-radius: 8px; padding: 7px 9px; background: #111827; color: #fff; font-size: 12px; box-shadow: var(--shadow-card); }}
    .metrics-grid {{ grid-template-columns: repeat(4, minmax(180px,1fr)); gap: 20px; margin: 8px 0 28px; }}
    .metric-card {{ min-height: 156px; padding: 20px; border: 1px solid var(--border); border-left: 1px solid var(--border); border-radius: 14px; background: #fff; box-shadow: var(--shadow-card); }}
    .metric-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }}
    .metric-icon {{ width: 38px; height: 38px; border-radius: 13px; background: #eef1ff; color: var(--accent-strong); }}
    .metric-card.green .metric-icon {{ background: #eafaf1; color: #16a34a; }} .metric-card.violet .metric-icon {{ background: #f1edff; color: #7c3aed; }} .metric-card.orange .metric-icon {{ background: #fff7e8; color: #f97316; }}
    .sparkline {{ width: 96px; height: 32px; }} .sparkline polyline {{ fill: none; stroke: currentColor; stroke-width: 2; }}
    .metric-label {{ min-height: 0; text-transform: none; letter-spacing: 0; font-size: 12px; color: #657399; }} .metric-value {{ font-size: 27px; margin: 4px 0 4px; }} .metric-hint {{ color: #16a34a; font-weight: 700; }} .metric-card.orange .metric-hint {{ color: #f97316; }}
    .quick-links {{ grid-template-columns: repeat(3, minmax(240px, 1fr)); gap: 12px; }}
    .quick-link-card {{ grid-template-columns: 44px 1fr 20px; align-items: center; gap: 14px; min-height: 72px; padding: 16px 20px; border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow-card); }}
    .quick-icon {{ width: 40px; height: 40px; border-radius: 14px; background: #eef1ff; color: var(--accent-strong); }} .quick-copy strong {{ display:block; color: var(--text-strong); }} .quick-copy small {{ display:block; color: #586892; }} .quick-arrow {{ color: #a8b3d0; font-size: 22px; }}
    .event-feed {{ overflow: hidden; background: #fff; border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow-card); }}
    .event-feed article {{ display: grid; grid-template-columns: 42px 1fr 110px; align-items: center; gap: 12px; min-height: 66px; padding: 12px 24px; border-bottom: 1px solid var(--border); }} .event-feed article:last-child {{ border-bottom: 0; }}
    .feed-icon {{ width: 34px; height: 34px; border-radius: 50%; background: #eef1ff; color: var(--accent-strong); }} .feed-icon.ok {{ background:#eafaf1; color:#16a34a; }} .feed-icon.warn {{ background:#fff7e8; color:#f59e0b; }} .event-feed small {{ display:block; color:#5f6f99; }} .event-feed time {{ color:#8b98ba; text-align:right; }}
    .filter-card, .form-card {{ border: 1px solid var(--border); border-radius: 14px; background: #fff; box-shadow: var(--shadow-card); }}
    .filter-summary, .form-summary {{ padding: 12px 18px; color: var(--text-strong); }}
    .filter-grid, .form-grid {{ padding: 14px 18px; gap: 12px; }}
    input, select, textarea {{ border-color: #d7e1f5; border-radius: 8px; }}
    .table-footer {{ margin: 12px 0; padding: 10px 14px; border: 1px solid var(--border); border-radius: 14px; background: #fff; box-shadow: var(--shadow-card); }}
    .table-card {{ border: 1px solid var(--border); border-radius: 14px; background: #fff; box-shadow: var(--shadow-card); }}
    table {{ border-collapse: separate; border-spacing: 0; width: 100%; font-size: 13px; }}
    th {{ height: 36px; background: #f6f8fc; color: #7985a8; font-size: 11px; letter-spacing: .06em; text-transform: uppercase; border-bottom: 1px solid #d9e3f5; }}
    th, td {{ border-right: 1px solid #e8eef9; }} th:last-child, td:last-child {{ border-right: 0; }}
    td {{ height: 44px; padding: 8px 12px; line-height: 1.25; vertical-align: middle; border-bottom: 1px solid #e8eef9; background: #fff; }} tbody tr:nth-child(even) td {{ background: #fbfcff; }} tbody tr:hover td {{ background: #f7f9ff; }}
    td[data-col='number'], td[data-col='routes'], td[data-col='route'] {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .status-badge, .badge {{ border: 0; background: transparent; padding: 0; border-radius: 0; }}
    .dot-status {{ display: inline-flex; align-items: center; gap: 6px; min-height: 0; white-space: nowrap; color: inherit; font: inherit; font-weight: 400; }} .dot-status span {{ width: 6px; height: 6px; border-radius: 50%; background:#c5ccdc; font-size: 0; line-height: 0; }} .dot-status.ok span {{ background:#22c55e; }} .dot-status.warning span {{ background:#f59e0b; }} .dot-status.danger span {{ background:#ef4444; }} .dot-status.neutral span {{ background:#c5ccdc; }}
    th[data-col="actions"], td[data-col="actions"], .dictionary-workspace th:last-child {{ width: 78px; min-width: 78px; max-width: 82px; background: inherit; }}
    .edit-action, td[data-col="actions"] details.edit-details > summary {{ width: 30px; min-width: 30px; height: 30px; min-height: 30px; }}
    td[data-col="actions"] details.edit-details > summary {{ appearance: none; -webkit-appearance: none; }}
    .edit-action::before, td[data-col="actions"] details.edit-details > summary::before {{ content: "✎"; font-size: 16px; }}
    @media (max-width: 900px) {{
      .app-shell {{ grid-template-columns: 1fr; }}
      .sidebar {{ position: static; height: auto; }}
      .workspace {{ padding: 18px 14px 28px; }}
      .dictionary-layout {{ grid-template-columns: 1fr; }}
      .dictionary-sidebar {{ position: static; }}
      .scope-cards {{ grid-template-columns: 1fr; }}
    }}
    /* Manual UI hotfix: sidebar active icons + table edit action */

    .sidebar .side-link:not(.has-inline-icon)::before,
    .sidebar .side-link.active:not(.has-inline-icon)::before,
    .sidebar-collapsed .side-link:not(.has-inline-icon)::before,
    .sidebar-collapsed .side-link.active:not(.has-inline-icon)::before,
    html[data-theme="calm-blue"] .side-link:not(.has-inline-icon)::before,
    html[data-theme="calm-blue"] .side-link.active:not(.has-inline-icon)::before,
    html[data-theme="terminal-paper"] .side-link:not(.has-inline-icon)::before,
    html[data-theme="terminal-paper"] .side-link.active:not(.has-inline-icon)::before {{
      content: attr(data-icon) !important;
      display: inline-flex !important;
      align-items: center !important;
      justify-content: center !important;
      width: 22px !important;
      height: 22px !important;
      min-width: 22px !important;
      flex: 0 0 22px !important;
      position: static !important;
      inset: auto !important;
      border: 0 !important;
      border-radius: 0 !important;
      background: transparent !important;
      color: #7786ad !important;
      font-size: 18px !important;
      line-height: 1 !important;
      opacity: 1 !important;
      visibility: visible !important;
    }}

    .sidebar .side-link.active:not(.has-inline-icon)::before,
    .sidebar-collapsed .side-link.active:not(.has-inline-icon)::before,
    html[data-theme="calm-blue"] .side-link.active:not(.has-inline-icon)::before,
    html[data-theme="terminal-paper"] .side-link.active:not(.has-inline-icon)::before {{
      content: attr(data-icon) !important;
      color: var(--accent-strong) !important;
    }}

    .sidebar-collapsed .side-link {{
      justify-content: center !important;
      font-size: 0 !important;
    }}

    .sidebar .side-link.has-inline-icon::before,
    .sidebar .side-link.has-inline-icon.active::before,
    .sidebar-collapsed .side-link.has-inline-icon::before,
    .sidebar-collapsed .side-link.has-inline-icon.active::before {{
      content: none !important;
      display: none !important;
    }}

    .sidebar-collapsed .side-link.active {{
      background: #eef1ff !important;
      border-color: #d5ddff !important;
    }}

    td[data-col="actions"] {{
      text-align: center !important;
      vertical-align: middle !important;
    }}

    td[data-col="actions"] .edit-action,
    td[data-col="actions"] details.edit-details > summary {{
      position: relative !important;
      display: inline-grid !important;
      place-items: center !important;
      width: 30px !important;
      min-width: 30px !important;
      max-width: 30px !important;
      height: 30px !important;
      min-height: 30px !important;
      max-height: 30px !important;
      padding: 0 !important;
      margin: 0 auto !important;
      overflow: hidden !important;
      border: 1px solid var(--border-strong) !important;
      border-radius: 8px !important;
      background: var(--surface) !important;
      box-shadow: none !important;
      color: transparent !important;
      font-size: 0 !important;
      line-height: 0 !important;
      text-indent: -9999px !important;
      white-space: nowrap !important;
    }}

    td[data-col="actions"] .edit-action::before,
    td[data-col="actions"] details.edit-details > summary::before {{
      content: "✎" !important;
      position: absolute !important;
      inset: 0 !important;
      display: grid !important;
      place-items: center !important;
      color: var(--accent-strong) !important;
      font-size: 16px !important;
      line-height: 1 !important;
      text-indent: 0 !important;
      transform: none !important;
    }}

    td[data-col="actions"] .edit-action:hover,
    td[data-col="actions"] details.edit-details > summary:hover {{
      background: var(--accent-soft) !important;
      border-color: var(--accent) !important;
    }}

    td[data-col="actions"] details.edit-details > summary::-webkit-details-marker {{
      display: none !important;
    }}

    /* /phones table statuses: plain text + CSS dot, without pill/badge chrome. */
    table[data-table-key="phones"] td {{
      height: auto;
      padding-top: 5px;
      padding-bottom: 5px;
      line-height: 1.25;
    }}

    table[data-table-key="phones"] td[data-col="status"] .dot-status,
    table[data-table-key="phones"] td[data-col="active"] .dot-status {{
      display: inline-flex !important;
      align-items: center !important;
      gap: 6px !important;
      min-width: 0 !important;
      min-height: 0 !important;
      height: auto !important;
      padding: 0 !important;
      border: 0 !important;
      border-radius: 0 !important;
      background: transparent !important;
      box-shadow: none !important;
      color: inherit !important;
      font: inherit !important;
      font-weight: 400 !important;
      line-height: inherit !important;
      white-space: nowrap !important;
    }}

    table[data-table-key="phones"] td[data-col="status"] .dot-status > span,
    table[data-table-key="phones"] td[data-col="active"] .dot-status > span {{
      flex: 0 0 6px !important;
      width: 6px !important;
      min-width: 6px !important;
      height: 6px !important;
      min-height: 6px !important;
      padding: 0 !important;
      border: 0 !important;
      border-radius: 50% !important;
      box-shadow: none !important;
      font-size: 0 !important;
      line-height: 0 !important;
    }}
  </style>
</head>
<body>
  <div class="app-shell">
    {sidebar(title)}
    <main class="workspace">
      
      <div class="content">
        {breadcrumbs(title)}
        {notice_html}
        {body}
      </div>
    </main>
  </div>
  <script>
    const themeSelect = document.querySelector("[data-theme-select]");
    const savedTheme = localStorage.getItem("mvp-theme") || "calm-blue";
    document.documentElement.dataset.theme = savedTheme;
    if (themeSelect) themeSelect.value = savedTheme;
    if (themeSelect) {{
      themeSelect.addEventListener("change", () => {{
        const theme = themeSelect.value || "cyber-sketch";
        document.documentElement.dataset.theme = theme;
        localStorage.setItem("mvp-theme", theme);
      }});
    }}

    const shell = document.querySelector(".app-shell");
    const savedSidebar = localStorage.getItem("mvp-sidebar-collapsed") === "true";
    if (shell) shell.classList.toggle("sidebar-collapsed", savedSidebar);
    document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {{
      const label = button.querySelector(".side-label");
      const sidebarAction = savedSidebar ? "Развернуть боковую панель" : "Свернуть боковую панель";
      button.dataset.tooltip = savedSidebar ? "Развернуть" : "Свернуть";
      button.setAttribute("aria-label", sidebarAction);
      button.title = sidebarAction;
    }});
    document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {{
      button.addEventListener("click", () => {{
        if (!shell) return;
        const collapsed = !shell.classList.contains("sidebar-collapsed");
        shell.classList.toggle("sidebar-collapsed", collapsed);
        localStorage.setItem("mvp-sidebar-collapsed", collapsed ? "true" : "false");
        const label = button.querySelector(".side-label");
        const sidebarAction = collapsed ? "Развернуть боковую панель" : "Свернуть боковую панель";
        button.dataset.tooltip = collapsed ? "Развернуть" : "Свернуть";
        button.setAttribute("aria-label", sidebarAction);
        button.title = sidebarAction;
      }});
    }});
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {{
      button.addEventListener("click", () => {{
        document.documentElement.dataset.theme = "calm-blue";
        localStorage.setItem("mvp-theme", "calm-blue");
      }});
    }});
    document.querySelectorAll(".admin-toggle").forEach((button) => {{
      button.addEventListener("click", () => {{
        const target = document.getElementById(button.getAttribute("aria-controls"));
        const expanded = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", expanded ? "false" : "true");
        target.classList.toggle("open", !expanded);
      }});
    }});
    document.querySelectorAll('td[data-col="actions"] details > summary').forEach((summary) => {{
      const label = (summary.textContent || '').trim() || 'Редактировать';
      if (!summary.title) summary.title = label;
      if (!summary.getAttribute('aria-label')) summary.setAttribute('aria-label', label);
    }});
    function fallbackCopyText(text) {{
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.top = "-1000px";
      document.body.appendChild(textarea);
      textarea.select();
      try {{
        document.execCommand("copy");
      }} finally {{
        document.body.removeChild(textarea);
      }}
    }}
    document.querySelectorAll("[data-copy-action]").forEach((button) => {{
      button.addEventListener("click", async () => {{
        const table = button.closest("table");
        if (!table) return;
        const column = button.dataset.copyAction;
        const values = Array.from(table.querySelectorAll(`[data-copy-column="${{column}}"]`))
          .map((cell) => cell.textContent.trim())
          .filter(Boolean);
        const text = values.join("\\n");
        try {{
          if (navigator.clipboard && window.isSecureContext) {{
            await navigator.clipboard.writeText(text);
          }} else {{
            fallbackCopyText(text);
          }}
        }} catch (error) {{
          fallbackCopyText(text);
        }}
        let status = button.parentElement.querySelector(".copy-column-status");
        if (!status) {{
          status = document.createElement("span");
          status.className = "copy-column-status";
          button.parentElement.appendChild(status);
        }}
        status.textContent = "Скопировано";
        window.setTimeout(() => {{
          status.textContent = "";
        }}, 1800);
      }});
    }});
    const fullTextPopover = (() => {{
      let popover = null;
      let activeCell = null;
      function close() {{
        if (popover) popover.remove();
        popover = null;
        activeCell = null;
      }}
      function place(cell) {{
        if (!popover) return;
        const rect = cell.getBoundingClientRect();
        const margin = 8;
        const width = Math.min(640, window.innerWidth - margin * 2);
        popover.style.width = `${{width}}px`;
        let left = Math.min(Math.max(rect.left, margin), window.innerWidth - width - margin);
        let top = rect.bottom + margin;
        popover.style.left = `${{left}}px`;
        popover.style.top = `${{top}}px`;
        const popRect = popover.getBoundingClientRect();
        if (popRect.bottom > window.innerHeight - margin) {{
          top = Math.max(margin, rect.top - popRect.height - margin);
          popover.style.top = `${{top}}px`;
        }}
      }}
      async function copy(text, button) {{
        try {{
          if (navigator.clipboard && window.isSecureContext) {{
            await navigator.clipboard.writeText(text);
          }} else {{
            fallbackCopyText(text);
          }}
        }} catch (error) {{
          fallbackCopyText(text);
        }}
        const oldText = button.textContent;
        button.textContent = "Скопировано";
        window.setTimeout(() => {{ button.textContent = oldText; }}, 1400);
      }}
      function open(cell) {{
        const text = cell.dataset.fullText || "";
        if (!text.trim()) return;
        close();
        activeCell = cell;
        popover = document.createElement("div");
        popover.className = "cell-popover";
        popover.setAttribute("role", "dialog");
        popover.setAttribute("aria-label", "Полный текст ячейки");
        const header = document.createElement("div");
        header.className = "cell-popover-header";
        const actions = document.createElement("div");
        actions.className = "cell-popover-actions";
        const copyButton = document.createElement("button");
        copyButton.type = "button";
        copyButton.className = "cell-popover-copy";
        copyButton.textContent = "Копировать";
        const closeButton = document.createElement("button");
        closeButton.type = "button";
        closeButton.className = "cell-popover-close";
        closeButton.setAttribute("aria-label", "Закрыть");
        closeButton.textContent = "×";
        const textBox = document.createElement("div");
        textBox.className = "cell-popover-text";
        textBox.textContent = text;
        copyButton.addEventListener("click", () => copy(text, copyButton));
        closeButton.addEventListener("click", close);
        actions.append(copyButton, closeButton);
        header.append(actions);
        popover.append(header, textBox);
        document.body.appendChild(popover);
        place(cell);
        copyButton.focus();
      }}
      document.addEventListener("click", (event) => {{
        const cell = event.target.closest("td[data-full-text]");
        if (cell) {{
          if (popover && activeCell === cell && popover.contains(event.target)) return;
          open(cell);
          event.stopPropagation();
          return;
        }}
        if (popover && !popover.contains(event.target)) close();
      }});
      document.addEventListener("keydown", (event) => {{
        if (event.key === "Escape") close();
      }});
      window.addEventListener("resize", () => activeCell && place(activeCell));
      window.addEventListener("scroll", () => activeCell && place(activeCell), true);
      return {{ close }};
    }})();
    document.querySelectorAll("[data-column-settings]").forEach((settings) => {{
      const tableKey = settings.dataset.columnSettings;
      const tables = Array.from(document.querySelectorAll(`[data-table-key="${{tableKey}}"]`));
      if (!tables.length) return;
      const storageKey = `tableColumns:${{tableKey}}`;
      const checkboxes = Array.from(settings.querySelectorAll("input[type='checkbox'][data-col-toggle]"));
      const columns = checkboxes.map((box) => box.dataset.colToggle);
      function loadPrefs() {{
        try {{
          const raw = window.localStorage && window.localStorage.getItem(storageKey);
          if (!raw) return null;
          const parsed = JSON.parse(raw);
          return parsed && typeof parsed === "object" ? parsed : null;
        }} catch (error) {{
          return null;
        }}
      }}
      function savePrefs(visible) {{
        try {{
          if (window.localStorage) window.localStorage.setItem(storageKey, JSON.stringify(visible));
        }} catch (error) {{}}
      }}
      function clearPrefs() {{
        try {{
          if (window.localStorage) window.localStorage.removeItem(storageKey);
        }} catch (error) {{}}
      }}
      function apply(visible, persist) {{
        columns.forEach((column) => {{
          const isVisible = visible[column] !== false;
          tables.forEach((table) => table.querySelectorAll(`[data-col="${{column}}"]`).forEach((cell) => {{
            cell.dataset.columnHidden = isVisible ? "false" : "true";
          }}));
          checkboxes.filter((box) => box.dataset.colToggle === column).forEach((box) => {{ box.checked = isVisible; }});
        }});
        if (persist) savePrefs(visible);
      }}
      const defaults = Object.fromEntries(columns.map((column) => [column, true]));
      apply(Object.assign(defaults, loadPrefs() || {{}}), false);
      checkboxes.forEach((box) => {{
        box.addEventListener("change", () => {{
          const visible = Object.fromEntries(checkboxes.map((item) => [item.dataset.colToggle, item.checked]));
          apply(visible, true);
        }});
      }});
      const reset = settings.querySelector("[data-column-reset]");
      if (reset) reset.addEventListener("click", () => {{
        clearPrefs();
        apply(Object.fromEntries(columns.map((column) => [column, true])), false);
      }});
    }});
  </script>
  <script src="https://cdn.jsdelivr.net/npm/@tabler/core@1.0.0-beta20/dist/js/tabler.min.js"></script>
</body>
</html>""".encode("utf-8")

def redirect(start_response, location: str, headers: list[tuple[str, str]] | None = None):
    response_headers = [("Location", location)]
    if headers:
        response_headers.extend(headers)
    start_response("303 See Other", response_headers)
    return [b""]


def cookie_user_id(environ) -> int | None:
    cookie = SimpleCookie()
    cookie.load(environ.get("HTTP_COOKIE", ""))
    morsel = cookie.get(CURRENT_USER_COOKIE)
    if not morsel:
        return None
    try:
        return int(morsel.value)
    except ValueError:
        return None


def resolve_current_user_id(repo: Repository, requested_id: int | None = None) -> int:
    if requested_id is not None:
        user = repo.get_user(requested_id)
        if user and user["is_active"]:
            return int(user["id"])
    first_active = repo.conn.execute("SELECT id FROM users WHERE is_active = 1 ORDER BY id LIMIT 1").fetchone()
    if first_active:
        return int(first_active["id"])
    return ADMIN_ID


def current_request_path(environ) -> str:
    path = environ.get("PATH_INFO", "/") or "/"
    query = environ.get("QUERY_STRING", "")
    return f"{path}?{query}" if query else path


def safe_redirect_target(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def current_actor_id() -> int:
    return int(_REQUEST_CONTEXT.get("current_user_id") or ADMIN_ID)



def section_for_get_path(path: str) -> str | None:
    if path in {"/", "/dashboard"}:
        return "dashboard"
    if path == "/routes" or (path.startswith("/routes/") and (path.endswith("/numbers") or path.endswith("/numbers/manage"))):
        return "routes"
    if path == "/tariffs":
        return "tariffs"
    if path == "/phones":
        return "phones"
    if path == "/companies":
        return "companies"
    if path == "/provider-changes":
        return "provider_changes"
    if path == "/admin":
        return "admin"
    admin_paths = {
        "/admin/server-priorities": "admin_server_priorities",
        "/admin/company-routing-settings": "admin_company_routing_settings",
        "/admin/naming-rules": "admin_route_naming",
        "/admin/import": "admin_import_export",
        "/admin/currency-rates": "admin_currency_rates",
        "/admin/change-reasons": "admin_provider_reasons",
        "/admin/users": "admin_users",
        "/admin/dictionaries": "admin_dictionaries",
        "/admin/change-log": "admin_change_log",
        "/admin/telegram": "admin",
    }
    if path in admin_paths:
        return admin_paths[path]
    if path.startswith("/admin"):
        return "admin"
    return None


def section_for_write_path(path: str) -> str | None:
    if path == "/users/select":
        return None
    if path.startswith("/provider-changes/") or path == "/provider-changes/create":
        return "provider_changes"
    if path.startswith("/routes/") or path == "/routes/create":
        return "routes"
    if path.startswith("/tariffs/") or path == "/tariffs/create" or path.startswith("/tariffs/"):
        return "tariffs"
    if path.startswith("/phones/") or path == "/phones/create":
        return "phones"
    if path.startswith("/companies/") or path == "/companies/create":
        return "companies"
    if path.startswith("/admin/server-priorities/"):
        return "admin_server_priorities"
    if path.startswith("/admin/company-routing-settings/") or path == "/admin/company-routing-settings/create":
        return "admin_company_routing_settings"
    if path.startswith("/admin/"):
        return "admin"
    return None


def require_permission(action: str, section: str | None) -> None:
    if section and not role_allows(current_role_key(), action, section):
        raise ForbiddenError()

def request_query(environ) -> dict[str, str]:
    return {key: values[-1] for key, values in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}


def active_query(q: dict[str, str], keys: list[str] | tuple[str, ...]) -> bool:
    return any(q.get(key) not in (None, "") for key in keys)


def filter_card(form_html: str, q: dict[str, str], keys: list[str] | tuple[str, ...]) -> str:
    open_attr = " open" if active_query(q, keys) else ""
    action_match = re.search(r'action=["\']([^"\']+)["\']', form_html)
    reset_href = action_match.group(1) if action_match else current_request_path({"PATH_INFO": "/", "QUERY_STRING": ""})
    reset_link = f"<a class='button reset-filters' href='{esc(reset_href)}'>Сбросить фильтры</a>"
    if "</form>" in form_html:
        form_html = form_html.replace("</form>", reset_link + "</form>", 1)
    else:
        form_html += reset_link
    return f"<details class='filter-card'{open_attr}><summary class='filter-summary'>Фильтры</summary>{form_html}</details>"


def form_card(summary: str, form_html: str, *, open_by_default: bool = False) -> str:
    open_attr = " open" if open_by_default else ""
    return f"<details class='form-card'{open_attr}><summary class='form-summary'>{summary}</summary>{form_html}</details>"


def table_card(table_html: str, *, title: str | None = None, extra_class: str = "") -> str:
    title_html = f"<h2>{esc(title)}</h2>" if title else ""
    classes = f"table-card {extra_class}".strip()
    return f"<section class='{classes}'>{title_html}<div class='table-scroll'>{table_html}</div></section>"


def column_settings(table_key: str, columns: list[tuple[str, str]]) -> str:
    checks = "".join(
        f"<label><input type='checkbox' data-col-toggle='{esc(key)}' checked> {esc(label)}</label>"
        for key, label in columns
    )
    return f"""<details class='column-settings' data-column-settings='{esc(table_key)}'>
<summary>Колонки</summary>
<div class='column-settings-panel'>{checks}<button type='button' class='column-reset' data-column-reset>Сбросить колонки</button></div>
</details>"""


def table_footer(summary_html: str, utility_html: str) -> str:
    return f"<div class='table-footer'><div class='table-footer-summary'>{summary_html}</div><div class='table-footer-tools'>{utility_html}</div></div>"

def data_table(table_key: str, columns: list[tuple[str, str]], rows_html: str) -> str:
    header = "".join(f"<th data-col='{esc(key)}'>{label}</th>" for key, label in columns)
    return f"<table data-table-key='{esc(table_key)}'><thead><tr>{header}</tr></thead><tbody>{rows_html}</tbody></table>"


def select_options(repo: Repository, sql: str, params: tuple = (), selected: object | None = None, empty: str | None = None) -> str:
    opts = f"<option value=''>{esc(empty)}</option>" if empty is not None else ""
    for row in repo.conn.execute(sql, params):
        value = row["id"]
        label = row["label"]
        opts += f"<option value='{value}' {'selected' if str(value) == str(selected) else ''}>{esc(label)}</option>"
    return opts


def options(repo: Repository, table: str, label: str = "name", selected: object | None = None, empty: str | None = None) -> str:
    return select_options(repo, f"SELECT id, {label} AS label FROM {table} ORDER BY {label}", selected=selected, empty=empty)


def active_options(repo: Repository, table: str, label: str = "name", selected: object | None = None, empty: str | None = None) -> str:
    return select_options(
        repo,
        f"SELECT id, {label} AS label FROM {table} WHERE is_active = 1 OR id = ? ORDER BY {label}",
        (selected or 0,),
        selected=selected,
        empty=empty,
    )


def prefix_options(repo: Repository, selected: object | None = None, empty: str | None = "Без префикса") -> str:
    return select_options(
        repo,
        """
        SELECT pp.id, COALESCE(NULLIF(pp.prefix, ''), 'Без префикса') AS label
        FROM provider_prefixes pp JOIN providers p ON p.id = pp.provider_id
        WHERE pp.is_active = 1 OR pp.id = ?
        ORDER BY COALESCE(pp.prefix, ''), p.name
        """,
        (selected or 0,),
        selected=selected,
        empty=empty,
    )


def phone_type_options(repo: Repository, selected: str | None = None, empty: str | None = None) -> str:
    opts = f"<option value=''>{esc(empty)}</option>" if empty is not None else ""
    for row in repo.conn.execute("SELECT name FROM phone_number_types WHERE is_active = 1 OR name = ? ORDER BY name", (selected or "",)):
        opts += f"<option value='{esc(row['name'])}' {'selected' if row['name'] == selected else ''}>{esc(row['name'])}</option>"
    return opts


def project_options(repo: Repository, selected: str | None = None, empty: str | None = None) -> str:
    opts = f"<option value=''>{esc(empty)}</option>" if empty is not None else ""
    for row in repo.conn.execute("SELECT name FROM projects WHERE is_active = 1 OR name = ? ORDER BY name", (selected or "",)):
        opts += f"<option value='{esc(row['name'])}' {'selected' if row['name'] == selected else ''}>{esc(row['name'])}</option>"
    return opts


def assignment_options(repo: Repository, selected: str | None = None, empty: str | None = None) -> str:
    opts = f"<option value=''>{esc(empty)}</option>" if empty is not None else ""
    for row in repo.conn.execute("SELECT code, name FROM phone_assignment_types WHERE is_active = 1 OR code = ? ORDER BY name", (selected or "",)):
        opts += f"<option value='{esc(row['code'])}' {'selected' if row['code'] == selected else ''}>{esc(row['name'])}</option>"
    return opts


def server_checkboxes(repo: Repository, selected: set[str] | None = None) -> str:
    selected = selected or set()
    boxes = []
    for row in repo.conn.execute("SELECT id, name FROM servers WHERE is_active = 1 OR id IN (%s) ORDER BY name" % (",".join(selected) if selected else "0")):
        checked = "checked" if str(row["id"]) in selected else ""
        boxes.append(f"<label><input type='checkbox' name='server_ids' value='{row['id']}' {checked}> {esc(row['name'])}</label>")
    return "<details><summary>Выбрать серверы</summary>" + "".join(boxes) + "<p class='muted'>Отмеченные серверы будут сохранены в журнале.</p></details>"


def active_server_priority_checkboxes(repo: Repository, selected: set[str] | None = None, country_id: object | None = None) -> str:
    selected = selected or set()
    priority_rows = repo.conn.execute(
        """
        SELECT srp.country_id, srp.server_id, c.name AS country_name, p.name AS provider_name, r.name AS route_name
        FROM server_route_priorities srp
        JOIN countries c ON c.id = srp.country_id
        LEFT JOIN routes r ON r.id = srp.current_route_id
        LEFT JOIN providers p ON p.id = r.provider_id
        """
    ).fetchall()
    route_hints = {
        (str(row["country_id"]), str(row["server_id"])): f"{row['country_name']} / {row['provider_name']} / {row['route_name']}"
        for row in priority_rows
        if row["route_name"]
    }
    initial_country_id = str(country_id or "")
    if not initial_country_id:
        active_countries = repo.conn.execute("SELECT id FROM countries WHERE is_active = 1 ORDER BY name").fetchall()
        if len(active_countries) == 1:
            initial_country_id = str(active_countries[0]["id"])
    boxes = []
    for row in repo.conn.execute("SELECT id, name FROM servers WHERE is_active = 1 ORDER BY name"):
        checked = "checked" if str(row["id"]) in selected else ""
        hint = route_hints.get((initial_country_id, str(row["id"])), "—")
        boxes.append(
            f"<label class='server-checkbox-item' data-server-chip data-server-id='{row['id']}' data-server-name='{esc(row['name'])}' data-initial-route='{esc(hint)}'>"
            f"<input type='checkbox' name='server_ids' value='{row['id']}' {checked}>"
            f"<span class='server-checkbox-copy'><span class='server-checkbox-main'>{esc(row['name'])}</span>"
            f"<span class='server-route-hint' data-current-route-hint data-server-id='{row['id']}' title='текущий: {esc(hint)}'>текущий: {esc(hint)}</span></span></label>"
        )
    return (
        "<div class='server-checkbox-toolbar'>"
        "<span class='server-selection-count' data-server-selection-count>0 выбрано</span>"
        "<span class='server-checkbox-actions'><button type='button' data-server-select='all'>Выбрать все</button>"
        "<button type='button' data-server-select='none'>Снять все</button></span>"
        "</div><div class='server-checkbox-grid'>"
        + "".join(boxes)
        + "</div><div class='server-current-routes' data-server-current-routes aria-live='polite' aria-label='Текущий маршрут выбранных серверов'></div>"
    )


DEMO_DATA_VERSION = "mvp_mexico_demo_v2"
DEMO_SERVER_NAMES = tuple(f"EU{i}" for i in range(1, 10))
DEMO_ROUTE_NAMES = (
    "Мексика/Miatel/Demo_A@",
    "Мексика/Miatel/Demo_B@",
    "Мексика/Sancom/Demo_0827@",
    "Мексика/Sancom/Demo_0828@",
    "Мексика/DemoTel/Demo_A@",
    "Мексика/DemoTel/Demo_B@",
)
DEMO_PHONE_NUMBERS = tuple(f"5255500000{i:02d}" for i in range(1, 11))
DEMO_COMPANY_EXTERNAL_IDS = tuple(str(1000 + i) for i in range(1, 6))


def ensure_seed(repo: Repository) -> None:
    def ensure_demo_state_table() -> None:
        repo.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS demo_data_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    def demo_version_applied() -> bool:
        row = repo.conn.execute("SELECT value FROM demo_data_state WHERE key = 'demo_data_version'").fetchone()
        return bool(row and row["value"] == DEMO_DATA_VERSION)

    def mark_demo_version_applied() -> None:
        repo.conn.execute(
            """
            INSERT INTO demo_data_state(key, value, updated_at)
            VALUES ('demo_data_version', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (DEMO_DATA_VERSION,),
        )

    def ensure_reference_defaults(*, activate_demo_servers: bool = True) -> None:
        for server_name in DEMO_SERVER_NAMES:
            if activate_demo_servers:
                repo.conn.execute(
                    """
                    INSERT INTO servers(name, is_active, comment)
                    VALUES (?, 1, ?)
                    ON CONFLICT(name) DO UPDATE SET is_active = 1, comment = excluded.comment, updated_at = CURRENT_TIMESTAMP
                    """,
                    (server_name, "Demo server for MVP testing"),
                )
            else:
                repo.conn.execute(
                    "INSERT OR IGNORE INTO servers(name, is_active, comment) VALUES (?, 1, ?)",
                    (server_name, "Demo server for MVP testing"),
                )
        for type_name in ("Mobile", "Fixed Line", "Toll-Free", "VoIP", "Unknown"):
            repo.conn.execute("INSERT OR IGNORE INTO phone_number_types(name, is_active) VALUES (?, 1)", (type_name,))
        for project_name in ("Междепы", "Competitors", "ITM", "Monitoring", "Test"):
            repo.conn.execute("INSERT OR IGNORE INTO projects(name, is_active) VALUES (?, 1)", (project_name,))
        for code, name in (
            ("outgoing_cli", "АОН"),
            ("inbound_line", "Входящая линия"),
            ("office_phone", "Горячая линия"),
            ("sim_card", "SIM-карта"),
            ("pool_number", "Номер из пула"),
            ("other", "Другое"),
        ):
            repo.conn.execute("INSERT OR IGNORE INTO phone_assignment_types(code, name, is_active) VALUES (?, ?, 1)", (code, name))
        repo.conn.commit()

    def scalar_id(sql: str, params: tuple = ()) -> int | None:
        row = repo.conn.execute(sql, params).fetchone()
        return int(row["id"]) if row else None

    def ensure_admin_user() -> int:
        admin_id = scalar_id("SELECT id FROM users WHERE username = 'admin' ORDER BY id LIMIT 1")
        if admin_id is not None:
            return admin_id
        return repo.create_user("admin", "Admin", "Admin")

    def ensure_country(name: str, code: str) -> int:
        country_id = scalar_id("SELECT id FROM countries WHERE name = ?", (name,))
        if country_id is None:
            return repo.create_country(name, code)
        repo.conn.execute("UPDATE countries SET code = ?, is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (code, country_id))
        return country_id

    def ensure_currency(code: str, name: str, symbol: str) -> int:
        currency_id = scalar_id("SELECT id FROM currencies WHERE code = ?", (code,))
        if currency_id is None:
            return repo.create_currency(code, name, symbol)
        repo.conn.execute("UPDATE currencies SET name = ?, symbol = ?, is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (name, symbol, currency_id))
        return currency_id

    def ensure_provider(name: str, provider_type: str, default_currency_id: int) -> int:
        normalized = normalize_provider_name(name)
        provider_id = scalar_id("SELECT id FROM providers WHERE normalized_name = ?", (normalized,))
        if provider_id is None:
            return repo.create_provider(name, provider_type, default_currency_id, comment="Demo provider for MVP testing")
        repo.conn.execute(
            """
            UPDATE providers
            SET name = ?, provider_type = ?, default_currency_id = ?, is_active = 1,
                comment = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (name, provider_type, default_currency_id, "Demo provider for MVP testing", provider_id),
        )
        return provider_id

    def ensure_prefix(provider_id: int, prefix: str | None, name: str) -> int:
        prefix_id = scalar_id(
            "SELECT id FROM provider_prefixes WHERE provider_id = ? AND COALESCE(prefix, '') = COALESCE(?, '')",
            (provider_id, prefix),
        )
        if prefix_id is None:
            return repo.create_prefix(provider_id, prefix, name)
        repo.conn.execute(
            "UPDATE provider_prefixes SET name = ?, is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (name, prefix_id),
        )
        return prefix_id

    def ensure_route(
        *,
        country_id: int,
        provider_id: int,
        provider_prefix_id: int | None,
        name: str,
        cli_source_type: str,
        cli_source_label: str,
        priority_status: str,
        admin_id: int,
    ) -> int:
        route_id = scalar_id("SELECT id FROM routes WHERE country_id = ? AND name = ?", (country_id, name))
        if route_id is None:
            return repo.create_route(
                country_id=country_id,
                provider_id=provider_id,
                provider_prefix_id=provider_prefix_id,
                name=name,
                cli_source_type=cli_source_type,
                cli_source_label=cli_source_label,
                created_by=admin_id,
                comment="Demo route for MVP testing",
                priority_status=priority_status,
            )
        repo.conn.execute(
            """
            UPDATE routes
            SET provider_id = ?, provider_prefix_id = ?, cli_source_type = ?, cli_source_label = ?,
                comment = ?, is_actual = 1, priority_status = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (provider_id, provider_prefix_id, cli_source_type, cli_source_label, "Demo route for MVP testing", priority_status, admin_id, route_id),
        )
        return route_id

    def ensure_tariff(
        *,
        country_id: int,
        provider_id: int,
        provider_prefix_id: int | None,
        provider_currency_id: int,
        price: str,
        rate: str,
        admin_id: int,
        priority_status: str,
    ) -> None:
        tariff_id = scalar_id(
            """
            SELECT id FROM tariffs
            WHERE country_id = ? AND provider_id = ? AND COALESCE(provider_prefix_id, 0) = COALESCE(?, 0) AND is_current = 1
            """,
            (country_id, provider_id, provider_prefix_id),
        )
        if tariff_id is None:
            repo.create_tariff(
                country_id=country_id,
                provider_id=provider_id,
                provider_prefix_id=provider_prefix_id,
                provider_currency_id=provider_currency_id,
                price_in_provider_currency=price,
                conversion_rate_to_eur=rate,
                conversion_rate_date="2026-06-07",
                created_by=admin_id,
                priority_status=priority_status,
                comment="Demo tariff for MVP testing",
            )

    def ensure_phone_number(
        *,
        country_id: int,
        provider_id: int,
        number: str,
        currency_id: int,
        route_id: int,
        admin_id: int,
    ) -> int:
        phone_id = scalar_id("SELECT id FROM phone_numbers WHERE number = ? OR normalized_number = ?", (number, number))
        if phone_id is None:
            phone_id = repo.create_phone_number(
                country_id=country_id,
                provider_id=provider_id,
                number=number,
                assignment_type="pool_number",
                status="used",
                created_by=admin_id,
                currency_id=currency_id,
                monthly_fee="1.00",
                comment="Demo number for testing",
            )
        else:
            repo.conn.execute(
                """
                UPDATE phone_numbers
                SET country_id = ?, provider_id = ?, assignment_type = 'pool_number', status = 'used',
                    currency_id = ?, comment = ?, is_active = 1, updated_by = ?, updated_at = CURRENT_TIMESTAMP,
                    deactivated_at = NULL
                WHERE id = ?
                """,
                (country_id, provider_id, currency_id, "Demo number for testing", admin_id, phone_id),
            )
        active_link = repo.conn.execute(
            "SELECT id FROM route_phone_numbers WHERE route_id = ? AND phone_number_id = ? AND is_active = 1",
            (route_id, phone_id),
        ).fetchone()
        if active_link is None:
            repo.add_phone_to_route(route_id=route_id, phone_number_id=phone_id, usage_type="pool_member", added_by=admin_id, comment="Demo route number link")
        return phone_id

    def ensure_calling_company(
        *,
        server_id: int,
        country_id: int,
        company_id_external: str,
        company_name: str,
        admin_id: int,
    ) -> int:
        company_id = scalar_id(
            """
            SELECT id FROM calling_companies
            WHERE server_id = ? AND country_id = ? AND company_id_external = ?
            """,
            (server_id, country_id, company_id_external),
        )
        if company_id is None:
            company_id = repo.create_calling_company(
                server_id=server_id,
                country_id=country_id,
                company_name=company_name,
                company_id_external=company_id_external,
                has_autorotation=False,
                created_by=admin_id,
                is_active=True,
                line_count=10,
                dial_set_count=2,
                retry_interval_seconds=60,
                comment="Demo calling campaign for MVP testing",
            )
        else:
            repo.conn.execute(
                """
                UPDATE calling_companies
                SET company_name = ?, has_autorotation = 0, line_count = 10, dial_set_count = 2,
                    retry_interval_seconds = 60, comment = ?, is_active = 1, updated_by = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (company_name, "Demo calling campaign for MVP testing", admin_id, company_id),
            )
        repo.conn.execute(
            """
            UPDATE calling_companies
            SET is_active = 0, updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE country_id = ? AND company_id_external = ? AND id <> ?
            """,
            (admin_id, country_id, company_id_external, company_id),
        )
        return company_id

    def upsert_server_priority(country_id: int, server_id: int, current_route_id: int, admin_id: int) -> None:
        priority_id = scalar_id("SELECT id FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (country_id, server_id))
        if priority_id is None:
            repo.conn.execute(
                """
                INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, changed_by, created_by, comment)
                VALUES (?, ?, ?, NULL, ?, ?, ?)
                """,
                (country_id, server_id, current_route_id, admin_id, admin_id, "Demo initial priority"),
            )
        else:
            repo.conn.execute(
                """
                UPDATE server_route_priorities
                SET current_route_id = ?, previous_route_id = NULL, changed_by = ?, comment = ?, is_active = 1,
                    updated_by = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (current_route_id, admin_id, "Demo initial priority", admin_id, priority_id),
            )

    def normalize_demo_dataset() -> None:
        ensure_reference_defaults(activate_demo_servers=True)
        admin_id = ensure_admin_user()
        country_id = ensure_country("Мексика", "MEX")
        eur_id = ensure_currency("EUR", "Euro", "€")
        usdt_id = ensure_currency("USDT", "Tether", "₮")
        sancom_id = ensure_provider("Sancom", "voip", eur_id)
        miatel_id = ensure_provider("Miatel", "voip", usdt_id)
        demotel_id = ensure_provider("DemoTel", "voip", eur_id)
        sancom_0827_prefix = ensure_prefix(sancom_id, "0827", "Demo 0827")
        sancom_0828_prefix = ensure_prefix(sancom_id, "0828", "Demo 0828")
        miatel_prefix = ensure_prefix(miatel_id, None, "Demo no prefix")
        demotel_prefix = ensure_prefix(demotel_id, None, "Demo no prefix")
        repo.conn.execute("INSERT INTO currency_rates(currency_id, rate_to_eur, rate_date, updated_by, comment) SELECT ?, 1, '2026-06-07', ?, 'Demo EUR' WHERE NOT EXISTS (SELECT 1 FROM currency_rates WHERE currency_id = ? AND rate_date = '2026-06-07' AND comment = 'Demo EUR')", (eur_id, admin_id, eur_id))
        repo.conn.execute("INSERT INTO currency_rates(currency_id, rate_to_eur, rate_date, updated_by, comment) SELECT ?, 0.93, '2026-06-07', ?, 'Demo USDT' WHERE NOT EXISTS (SELECT 1 FROM currency_rates WHERE currency_id = ? AND rate_date = '2026-06-07' AND comment = 'Demo USDT')", (usdt_id, admin_id, usdt_id))
        for reason in ("Плохие показатели", "Провайдер починил", "Обновлен пул номеров"):
            repo.conn.execute("INSERT OR IGNORE INTO change_reasons(name, description, is_active) VALUES (?, ?, 1)", (reason, reason))

        server_ids = {row["name"]: row["id"] for row in repo.conn.execute("SELECT id, name FROM servers WHERE name IN (%s)" % ",".join("?" for _ in DEMO_SERVER_NAMES), DEMO_SERVER_NAMES)}
        repo.conn.execute(
            "UPDATE servers SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE name NOT IN (%s)" % ",".join("?" for _ in DEMO_SERVER_NAMES),
            DEMO_SERVER_NAMES,
        )

        route_ids = {
            "sancom_0827": ensure_route(country_id=country_id, provider_id=sancom_id, provider_prefix_id=sancom_0827_prefix, name="Мексика/Sancom/Demo_0827@", cli_source_type="rnd", cli_source_label="Demo_0827", priority_status="priority", admin_id=admin_id),
            "miatel_a": ensure_route(country_id=country_id, provider_id=miatel_id, provider_prefix_id=miatel_prefix, name="Мексика/Miatel/Demo_A@", cli_source_type="pool", cli_source_label="Demo_A", priority_status="priority", admin_id=admin_id),
            "miatel_b": ensure_route(country_id=country_id, provider_id=miatel_id, provider_prefix_id=miatel_prefix, name="Мексика/Miatel/Demo_B@", cli_source_type="pool", cli_source_label="Demo_B", priority_status="normal", admin_id=admin_id),
            "sancom_0828": ensure_route(country_id=country_id, provider_id=sancom_id, provider_prefix_id=sancom_0828_prefix, name="Мексика/Sancom/Demo_0828@", cli_source_type="rnd", cli_source_label="Demo_0828", priority_status="normal", admin_id=admin_id),
            "demotel_a": ensure_route(country_id=country_id, provider_id=demotel_id, provider_prefix_id=demotel_prefix, name="Мексика/DemoTel/Demo_A@", cli_source_type="pool", cli_source_label="Demo_A", priority_status="normal", admin_id=admin_id),
            "demotel_b": ensure_route(country_id=country_id, provider_id=demotel_id, provider_prefix_id=demotel_prefix, name="Мексика/DemoTel/Demo_B@", cli_source_type="pool", cli_source_label="Demo_B", priority_status="normal", admin_id=admin_id),
        }
        repo.conn.execute(
            "UPDATE routes SET is_actual = 0, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE country_id = ? AND name NOT IN (%s)" % ",".join("?" for _ in DEMO_ROUTE_NAMES),
            (admin_id, country_id, *DEMO_ROUTE_NAMES),
        )

        ensure_tariff(country_id=country_id, provider_id=sancom_id, provider_prefix_id=sancom_0827_prefix, provider_currency_id=eur_id, price="2.00", rate="1", admin_id=admin_id, priority_status="priority")
        ensure_tariff(country_id=country_id, provider_id=miatel_id, provider_prefix_id=miatel_prefix, provider_currency_id=usdt_id, price="3.00", rate="0.93", admin_id=admin_id, priority_status="priority")
        ensure_tariff(country_id=country_id, provider_id=demotel_id, provider_prefix_id=demotel_prefix, provider_currency_id=eur_id, price="2.50", rate="1", admin_id=admin_id, priority_status="normal")

        phone_specs = (
            (miatel_id, "525550000001", route_ids["miatel_a"]),
            (miatel_id, "525550000002", route_ids["miatel_a"]),
            (miatel_id, "525550000003", route_ids["miatel_a"]),
            (sancom_id, "525550000004", route_ids["sancom_0827"]),
            (sancom_id, "525550000005", route_ids["sancom_0827"]),
            (sancom_id, "525550000006", route_ids["sancom_0827"]),
            (demotel_id, "525550000007", route_ids["demotel_a"]),
            (demotel_id, "525550000008", route_ids["demotel_a"]),
            (demotel_id, "525550000009", route_ids["demotel_a"]),
            (demotel_id, "525550000010", route_ids["demotel_a"]),
        )
        for provider_id, number, route_id in phone_specs:
            ensure_phone_number(country_id=country_id, provider_id=provider_id, number=number, currency_id=eur_id, route_id=route_id, admin_id=admin_id)
        repo.conn.execute(
            "UPDATE phone_numbers SET is_active = 0, deactivated_at = COALESCE(deactivated_at, CURRENT_TIMESTAMP), updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE country_id = ? AND number NOT IN (%s)" % ",".join("?" for _ in DEMO_PHONE_NUMBERS),
            (admin_id, country_id, *DEMO_PHONE_NUMBERS),
        )

        for index, external_id in enumerate(DEMO_COMPANY_EXTERNAL_IDS, start=1):
            ensure_calling_company(server_id=server_ids[f"EU{index}"], country_id=country_id, company_id_external=external_id, company_name=f"CC Mexico Demo {index}", admin_id=admin_id)
        repo.conn.execute(
            "UPDATE calling_companies SET is_active = 0, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE country_id = ? AND company_id_external NOT IN (%s)" % ",".join("?" for _ in DEMO_COMPANY_EXTERNAL_IDS),
            (admin_id, country_id, *DEMO_COMPANY_EXTERNAL_IDS),
        )

        repo.conn.execute(
            "DELETE FROM server_route_priorities WHERE server_id IN (SELECT id FROM servers WHERE name IN ('EU3', 'EU4', 'EU5', 'EU6', 'EU7', 'EU8', 'EU9')) OR (country_id = ? AND server_id NOT IN (?, ?))",
            (country_id, server_ids["EU1"], server_ids["EU2"]),
        )
        upsert_server_priority(country_id, server_ids["EU1"], route_ids["miatel_a"], admin_id)
        upsert_server_priority(country_id, server_ids["EU2"], route_ids["sancom_0827"], admin_id)
        mark_demo_version_applied()
        repo.conn.commit()

    ensure_demo_state_table()
    if demo_version_applied():
        ensure_reference_defaults(activate_demo_servers=False)
        return
    normalize_demo_dataset()

def clean_parts(parts: list[str]) -> str:
    return "/".join([p.strip(" /") for p in parts if p and p.strip(" /")])


def build_route_name(repo: Repository, country_id: int, provider_id: int, project_label: str | None, cli_source_label: str, provider_prefix_id: int | None) -> str:
    country = repo.conn.execute("SELECT name FROM countries WHERE id = ?", (country_id,)).fetchone()
    provider = repo.conn.execute("SELECT name FROM providers WHERE id = ?", (provider_id,)).fetchone()
    prefix = repo.conn.execute("SELECT prefix FROM provider_prefixes WHERE id = ?", (provider_prefix_id,)).fetchone() if provider_prefix_id else None
    country_name = country["name"] if country else ""
    provider_name = provider["name"] if provider else ""
    prefix_part = f"{prefix['prefix']}pfx" if prefix and prefix["prefix"] else ""
    base = clean_parts([country_name, project_label, provider_name, cli_source_label, prefix_part]) + "@"
    while "//@" in base or "//" in base:
        base = base.replace("//", "/")
    return base



ROUTING_MODE_LABELS = {
    "server_priority": "server_priority",
    "campaign_route": "campaign_route",
    "autorotation": "autorotation",
    "mixed": "mixed",
}


def routing_mode_options(selected: str | None = None, empty: str | None = None) -> str:
    opts = f"<option value=''>{esc(empty)}</option>" if empty is not None else ""
    for value, label in ROUTING_MODE_LABELS.items():
        opts += f"<option value='{esc(value)}' {'selected' if value == selected else ''}>{esc(label)}</option>"
    return opts


def company_options(repo: Repository, selected: object | None = None, empty: str | None = None) -> str:
    return select_options(
        repo,
        """
        SELECT cc.id, cc.company_id_external || ' — ' || cc.company_name || ' (' || c.name || ' / ' || s.name || ')' AS label
        FROM calling_companies cc
        JOIN countries c ON c.id = cc.country_id
        JOIN servers s ON s.id = cc.server_id
        ORDER BY c.name, s.name, cc.company_name
        """,
        selected=selected,
        empty=empty,
    )


def route_options_for_country(repo: Repository, country_id: object | None = None, selected: object | None = None, empty: str | None = "—") -> str:
    if country_id in (None, ""):
        return select_options(
            repo,
            """
            SELECT r.id, r.name AS label
            FROM routes r
            ORDER BY r.name
            """,
            selected=selected,
            empty=empty,
        )
    return select_options(
        repo,
        """
        SELECT r.id, r.name AS label
        FROM routes r
        WHERE r.country_id = ?
        ORDER BY r.name
        """,
        (country_id,),
        selected=selected,
        empty=empty,
    )



def dot_status(label: str, tone: str = "neutral") -> str:
    return f"<span class='dot-status {esc(tone)}'><span aria-hidden='true'></span>{esc(label)}</span>"

def dashboard_metric(repo: Repository, sql: str, label: str, hint: str, icon: str, tone: str, points: str) -> str:
    row = repo.conn.execute(sql).fetchone()
    value = row[0] if row else 0
    return f"<article class='metric-card {tone}'><div class='metric-top'><span class='metric-icon'>{icon}</span><svg class='sparkline' viewBox='0 0 96 32' aria-hidden='true'><polyline points='{points}' /></svg></div><span class='metric-label'>{esc(label)}</span><strong class='metric-value'>{esc(value)}</strong><span class='metric-hint'>{esc(hint)}</span></article>"


def dashboard_link(href: str, label: str, description: str, section: str) -> str:
    if not can_read(section):
        return ""
    return f"<a class='quick-link-card' href='{esc(href)}'><span class='quick-icon'>{NAV_ICONS.get(section, '•')}</span><span class='quick-copy'><strong>{esc(label)}</strong><small>{esc(description)}</small></span><span class='quick-arrow'>→</span></a>"


def dashboard_page(repo: Repository) -> bytes:
    metrics = "".join([
        dashboard_metric(repo, "SELECT COUNT(*) FROM routes WHERE is_actual = 1", "Активные маршруты", "↗ +1 за неделю", nav_icon("routes"), "blue", "0,22 18,22 32,16 46,22 62,17 78,17 96,10"),
        dashboard_metric(repo, "SELECT COUNT(*) FROM calling_companies WHERE is_active = 1", "Активные кампании", "↗ +1 за неделю", nav_icon("companies"), "green", "0,22 12,17 28,16 44,16 58,15 72,10 84,15 96,9"),
        dashboard_metric(repo, "SELECT COUNT(*) FROM phone_numbers WHERE is_active = 1", "Купленные номера", "↗ +2 за неделю", nav_icon("phones"), "violet", "0,20 18,20 30,17 46,17 62,14 78,9 96,9"),
        dashboard_metric(repo, "SELECT COUNT(*) FROM routing_events WHERE is_active = 1", "Смены провайдеров", "↘ −3 за неделю", nav_icon("provider_changes"), "orange", "0,8 16,10 32,10 48,12 64,12 80,14 96,14"),
    ])
    work_links = "".join([
        dashboard_link("/routes", "Маршруты", "Управление маршрутами и номерами", "routes"),
        dashboard_link("/tariffs", "Тарифы", "Актуальные цены и приоритеты", "tariffs"),
        dashboard_link("/phones", "Купленные номера", "Пул номеров и статусы", "phones"),
        dashboard_link("/companies", "Кампании прозвона", "Кампании, серверы и авторотация", "companies"),
        dashboard_link("/provider-changes", "Смена провайдеров", "Операционный журнал изменений", "provider_changes"),
    ])
    admin_links = "".join([
        dashboard_link("/admin/server-priorities", "Приоритет по серверам", "Текущий маршрут по GEO и серверу", "admin_server_priorities"),
        dashboard_link("/admin/company-routing-settings", "Схема маршрутизации кампаний", "Правила кампаний и авторотации", "admin_company_routing_settings"),
        dashboard_link("/admin/users", "Пользователи", "Роли и доступы", "admin_users"),
        dashboard_link("/admin/dictionaries", "Справочные значения", "Страны, провайдеры, валюты и префиксы", "admin_dictionaries"),
    ])
    body = f"""
<section class='metrics-grid'>{metrics}</section>
<section class='dashboard-section'><h2>Быстрые переходы</h2><div class='quick-links'>{work_links}{admin_links}</div></section>
<section class='dashboard-section'><h2>Лента событий</h2><div class='event-feed'><article><span class='feed-icon ok'>{nav_icon("routes")}</span><div><strong>Маршрут Mexico/Miatel/Demo_A@ активирован</strong><small>Маршруты · провайдер Miatel</small></div><time>2 мин назад</time></article><article><span class='feed-icon warn'>{nav_icon("phones")}</span><div><strong>14 номеров помечены «Требует проверки»</strong><small>Купленные номера · автопроверка</small></div><time>18 мин назад</time></article><article><span class='feed-icon info'>{nav_icon("companies")}</span><div><strong>Кампания "Mexico Demo 1" обновлена</strong><small>Кампании прозвона · ручное обновление</small></div><time>41 мин назад</time></article><article><span class='feed-icon neutral'>{nav_icon("provider_changes")}</span><div><strong>Изменён приоритет сервера EU2</strong><small>Смена провайдеров · авторотация серверов</small></div><time>1 ч назад</time></article><article><span class='feed-icon ok'>{nav_icon("admin")}</span><div><strong>Обновлён справочник префиксов</strong><small>Администрирование · справочники</small></div><time>3 ч назад</time></article></div></section>
"""
    return page("Главная", body)

def routes_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    filters = {"country_id": q.get("country_id"), "provider_id": q.get("provider_id"), "prefix_id": q.get("prefix_id"), "is_actual": q.get("is_actual"), "search_like": q.get("search")}
    records = list(repo.list_routes(filters))
    if q.get("export") == "csv":
        return csv_response("routes_export.csv", ["GEO", "Провайдер", "Маршрут", "Сервер", "Активен", "Комментарий"], [[r["country_name"], r["provider_name"], r["name"], "", "Да" if r["is_actual"] else "Нет", r["comment"]] for r in records])
    records, pagination_html = paginate_rows(records, q, "/routes")
    rows = []
    for route in records:
        prefix = route["prefix"] or "Без префикса"
        numbers_label = "RND провайдера" if route["cli_source_type"] == "rnd" else f'{route["phone_count"]} номеров'
        numbers = f'{numbers_label} <a class="button route-numbers-action" href="/routes/{route["id"]}/numbers">Показать номера</a>'
        edit = f"<a class='button edit-action' href='/routes/{route['id']}/edit' title='Редактировать' aria-label='Редактировать' data-tooltip='Редактировать'>Редактировать</a>" if can_write("routes") else ""
        rows.append(f"<tr><td data-col='geo'>{esc(route['country_name'])}</td>{clamp_cell('route', esc(route['name']), route['name'], extra_attrs="data-copy-column='route-name'")}<td data-col='provider'>{esc(route['provider_name'])}</td><td data-col='prefix'>{esc(prefix)}</td><td data-col='actual'>{'Да' if route['is_actual'] else 'Нет'}</td>{clamp_cell('comment', esc(route['comment']), route['comment'], classes='comment-cell')}<td data-col='numbers'>{numbers}</td><td data-col='actions' class='actions'>{edit}</td></tr>")
    filters_html = f"""<form class="filter-grid" method="get" action="/routes">
<label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Провайдер <select name="provider_id">{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
<label>Префикс <select name="prefix_id">{prefix_options(repo, selected=q.get('prefix_id'), empty='Все')}</select></label>
<label>Актуальный <select name="is_actual"><option value="">Все</option><option value="1" {'selected' if q.get('is_actual')=='1' else ''}>Да</option><option value="0" {'selected' if q.get('is_actual')=='0' else ''}>Нет</option></select></label>
<label>Поиск <input name="search" value="{esc(q.get('search'))}"></label><button>Найти</button></form>"""
    create_html = f"""<form class="form-grid" method="post" action="/routes/create">
  <label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label>
  <label>Провайдер <span class="required">*</span><select name="provider_id">{active_options(repo, 'providers')}</select></label>
  <label>Префикс <select name="provider_prefix_id">{prefix_options(repo)}</select></label>
  <label>Проект/метка <input name="project_label"></label>
  <label>Источник АОН <span class="required">*</span><select name="cli_source_type"><option value="pool">Pool</option><option value="rnd">RND</option><option value="sim">SIM</option><option value="single_number">Single</option><option value="other">Other</option></select></label>
  <label>Метка АОН <span class="required">*</span><input name="cli_source_label" value="Pool_A"></label>
  <label>Статус <span class="required">*</span><select name="is_actual"><option value="1">Активный</option><option value="0">Неактивный</option></select></label>
  <label>Комментарий <input name="comment"></label>
  <p class="muted wide">Название будет сформировано автоматически по выбранным полям. Свободный ввод названия отключён.</p>
  <button>Сохранить</button>
</form>"""
    table_html = f"{data_table('routes', [('geo', 'ГЕО'), ('route', f"<span class='copyable-header'>Название маршрута {copy_column_button('route-name')}</span>"), ('provider', 'Провайдер'), ('prefix', 'Префикс'), ('actual', 'Актуальный'), ('comment', 'Комментарий'), ('numbers', 'Номера'), ('actions', 'Действия')], ''.join(rows))}"
    body = f"""
<h1>Маршруты</h1>
{filter_card(filters_html, q, ('country_id', 'provider_id', 'prefix_id', 'is_actual', 'search'))}
{form_card('+ Добавить маршрут <span class="muted">Admin</span>', create_html) if can_write("routes") else ""}
{table_card(table_html)}
{table_footer(pagination_html, export_link('/routes', q) + column_settings('routes', [('geo', 'ГЕО'), ('route', 'Название маршрута'), ('provider', 'Провайдер'), ('prefix', 'Префикс'), ('actual', 'Актуальный'), ('comment', 'Комментарий'), ('numbers', 'Номера'), ('actions', 'Действия')]))}
"""
    return page("Маршруты", body)


def route_number_rows(repo: Repository, route_id: int, *, selectable: bool = False) -> tuple[list[sqlite3.Row], str, str]:
    numbers = repo.route_numbers(route_id)
    rows = []
    for phone in numbers:
        cost = f"Подкл: {phone['connection_cost'] or '—'} / Абон: {phone['monthly_fee'] or '—'} / Исх: {phone['outgoing_rate'] or '—'} / Вх: {phone['incoming_rate'] or '—'}"
        select_cell = f"<td><input type='checkbox' name='link_ids' value='{phone['link_id']}'></td>" if selectable else ""
        rows.append(f"<tr>{select_cell}<td>{esc(phone['number'])}</td><td>{esc(STATUS_LABELS.get(phone['status'], phone['status']))}</td><td>{esc(ASSIGNMENT_LABELS.get(phone['assignment_type'], phone['assignment_type']))}</td><td>{esc(cost)}</td><td class='comment-cell'>{esc(phone['link_comment'] or phone['phone_comment'])}</td></tr>")
    select_header = "<th></th>" if selectable else ""
    table_html = f"<table><thead><tr>{select_header}<th>Номер</th><th>Статус</th><th>Назначение</th><th>Стоимость</th><th>Комментарий</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    all_numbers = chr(10).join([p["number"] for p in numbers])
    return numbers, all_numbers, table_html


def route_numbers_page(repo: Repository, route_id: int, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    route = repo.conn.execute("SELECT name FROM routes WHERE id = ?", (route_id,)).fetchone()
    if route is None:
        return page("Не найдено", "<h1>Маршрут не найден</h1>")
    _, all_numbers, table_html = route_number_rows(repo, route_id, selectable=False)
    body = f"""
<h1>Номера в маршруте: {esc(route['name'])}</h1><p><a href="/routes">← Назад</a></p>
<div class="grid"><div class="card"><h2>Скопировать все</h2><textarea rows="7" cols="40" readonly>{esc(all_numbers)}</textarea></div></div>
{table_html}
"""
    return page("Номера маршрута", body, q.get("notice"), "error" if q.get("notice_type") == "error" else "success")


def route_numbers_manage_page(repo: Repository, route_id: int, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    route = repo.conn.execute("SELECT name FROM routes WHERE id = ?", (route_id,)).fetchone()
    if route is None:
        return page("Маршрут не найден", "<h1>Маршрут не найден</h1>")
    _, all_numbers, table_html = route_number_rows(repo, route_id, selectable=True)
    add_tools = f"""
<div class="card"><h2>+ Добавить номер <span class="muted">Admin</span></h2><form method="post" action="/routes/{route_id}/numbers/add"><label>Номер телефона <span class="required">*</span><input name="phone_number"></label><label>Назначение <span class="required">*</span><input name="usage_type" value="pool_member"></label><label>Комментарий <input name="comment"></label><button>Добавить</button></form></div>
<div class="card"><h2>Массовое добавление</h2><form method="post" action="/routes/{route_id}/numbers/bulk-add"><textarea name="phone_numbers" rows="7" cols="40" placeholder="по одному номеру в строке"></textarea><br><button>Добавить список</button></form></div>"""
    remove_form = f"""<form method="post" action="/routes/{route_id}/numbers/remove"><label>Причина <input name="reason"></label><button onclick="return confirm('Исключить выбранные номера из маршрута?')">Исключить из маршрута</button>{table_html}</form>"""
    body = f"""
<h1>Номера маршрута / АОНы: {esc(route['name'])}</h1><p><a href="/routes/{route_id}/edit">← Назад к маршруту</a> · <a href="/routes/{route_id}/numbers">Только просмотр</a></p>
<div class="grid"><div class="card"><h2>Скопировать все</h2><textarea rows="7" cols="40" readonly>{esc(all_numbers)}</textarea></div>{add_tools}</div>
{remove_form}
"""
    return page("Номера маршрута / АОНы", body, q.get("notice"), "error" if q.get("notice_type") == "error" else "success")

def tariffs_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    filters = {"country_id": q.get("country_id"), "provider_id": q.get("provider_id"), "priority_status": q.get("priority_status"), "status": q.get("status", "active")}
    records = list(repo.list_tariffs(filters))
    if q.get("export") == "csv":
        return csv_response("tariffs_export.csv", ["GEO", "Валюта", "Цена/Тариф", "Название", "Активен", "Комментарий"], [[t["country_name"], t["currency_code"], t["price_in_provider_currency"], f"{t['provider_name']} / {t['prefix'] or 'Без префикса'}", "Да" if t["is_current"] else "Нет", t["comment"]] for t in records])
    records, pagination_html = paginate_rows(records, q, "/tariffs")
    rows = []
    for t in records:
        prefix = t["prefix"] or "Без префикса"
        actions = f"""<form method='post' action='/tariffs/{t['id']}/deactivate'><button class='danger-action' title='Деактивировать' onclick="return confirm('Деактивировать тариф?')">Деактивировать</button></form>""" if can_write("tariffs") else ""
        rows.append(f"""<tr><td data-col='geo'>{esc(t['country_name'])}</td><td data-col='provider'>{esc(t['provider_name'])}</td><td data-col='prefix'>{esc(prefix)}</td><td data-col='provider_price'>{esc(t['price_in_provider_currency'])} {esc(t['currency_code'])}</td><td data-col='eur_price'>{esc(t['eur_price'])} EUR</td><td data-col='priority'>{esc(t['priority_status'])}</td><td data-col='active'>{'Да' if t['is_current'] else 'Нет'}</td>{clamp_cell('info', esc(t['comment']), t['comment'], classes='comment-cell')}<td data-col='actions'>{actions}</td></tr>""")
    filters_html = f"""<form class="filter-grid" method="get" action="/tariffs">
<label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Провайдер <select name="provider_id">{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
<label>Приоритет <select name="priority_status"><option value="">Все</option><option value="priority" {'selected' if q.get('priority_status')=='priority' else ''}>priority</option><option value="alternative" {'selected' if q.get('priority_status')=='alternative' else ''}>alternative</option><option value="unknown" {'selected' if q.get('priority_status')=='unknown' else ''}>unknown</option></select></label>
<label>Статус <select name="status"><option value="all" {'selected' if q.get('status')=='all' else ''}>Все</option><option value="active" {'selected' if q.get('status','active')=='active' else ''}>Активные</option><option value="inactive" {'selected' if q.get('status')=='inactive' else ''}>Неактивные</option></select></label><button>Найти</button></form>"""
    create_html = f"""<form class="form-grid" method="post" action="/tariffs/create">
<label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label>
<label>Провайдер <span class="required">*</span><select name="provider_id">{active_options(repo, 'providers')}</select></label>
<label>Префикс <span class="required">*</span><select name="provider_prefix_id">{prefix_options(repo)}</select></label>
<label>Валюта <span class="required">*</span><select name="currency_id">{active_options(repo, 'currencies', 'code')}</select></label>
<label>Цена <span class="required">*</span><input name="price"></label>
<label>Приоритет <span class="required">*</span><select name="priority_status"><option value="priority">priority</option><option value="alternative">alternative</option><option value="unknown">unknown</option></select></label>
<label>Активный <span class="required">*</span><select name="is_current"><option value="1">Да</option><option value="0">Нет</option></select></label>
<label>Комментарий <input name="comment"></label><p class="muted wide">Курс к EUR и дата курса берутся из Администрирование → Курсы валют.</p><button>Сохранить</button></form>"""
    table_html = f"{data_table('tariffs', [('geo', 'ГЕО'), ('provider', 'Провайдер'), ('prefix', 'Префикс'), ('provider_price', 'Цена провайдера'), ('eur_price', 'Цена EUR'), ('priority', 'Приоритет'), ('active', 'Активный'), ('info', 'Инфо'), ('actions', 'Действия')], ''.join(rows))}"
    body = f"""
<h1>Тарифы</h1>
{filter_card(filters_html, q, ('country_id', 'provider_id', 'priority_status', 'status'))}
{form_card('+ Добавить тариф <span class="muted">Admin</span>', create_html) if can_write("tariffs") else ""}
{table_card(table_html)}
{table_footer(pagination_html, export_link('/tariffs', q) + column_settings('tariffs', [('geo', 'ГЕО'), ('provider', 'Провайдер'), ('prefix', 'Префикс'), ('provider_price', 'Цена провайдера'), ('eur_price', 'Цена EUR'), ('priority', 'Приоритет'), ('active', 'Активный'), ('info', 'Инфо'), ('actions', 'Действия')]))}"""
    return page("Тарифы", body)


def phones_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    filters = {"country_id": q.get("country_id"), "provider_id": q.get("provider_id"), "project": q.get("project"), "assignment_type": q.get("assignment_type"), "status": q.get("status"), "number_like": q.get("number")}
    records = list(repo.list_phone_numbers(filters))
    if q.get("export") == "csv":
        return csv_response("phones_export.csv", ["Номер", "GEO", "Провайдер", "Тип номера", "Кампания", "Рабочий статус", "Активен у провайдера", "Маршруты", "Требует проверки", "Комментарий"], [[p["number"], p["country_name"], p["provider_name"], p["phone_type"], p["project_label"], STATUS_LABELS.get(p["status"], p["status"]), "Да" if p["is_active"] else "Нет", p["route_names"] or "—", "Да" if p["review_required"] else "Нет", p["comment"]] for p in records])
    records, pagination_html = paginate_rows(records, q, "/phones")
    rows = []
    for phone in records:
        assignment_label = phone["assignment_type_label"] or ASSIGNMENT_LABELS.get(phone["assignment_type"], phone["assignment_type"])
        actions = f"<a class='button edit-action' href='/phones/{phone['id']}/edit' title='Редактировать' aria-label='Редактировать' data-tooltip='Редактировать'>Редактировать</a>" if can_write("phones") else ""
        review_badge = "<span class='badge'>Требует проверки</span>" if phone["review_required"] else ""
        rows.append(f"""<tr><td data-col='number' data-copy-column='phone-number'>{esc(phone['number'])} {review_badge}</td><td data-col='geo'>{esc(phone['country_name'])}</td><td data-col='provider'>{esc(phone['provider_name'])}</td><td data-col='project'>{esc(phone['project_label'])}</td><td data-col='assignment'>{esc(assignment_label)}</td><td data-col='status'>{dot_status(STATUS_LABELS.get(phone['status'], phone['status']), 'danger' if phone['status'] == 'problem' else ('warning' if phone['status'] == 'unknown' else ('neutral' if phone['status'] == 'free' else 'ok')))}</td><td data-col='active'>{dot_status('Да' if phone['is_active'] else 'Нет', 'ok' if phone['is_active'] else 'danger')}</td>{clamp_cell('routes', esc(phone['route_names']), phone['route_names']) if phone['route_names'] else "<td data-col='routes'>—</td>"}<td data-col='connection'>{esc(phone['connection_cost'])}</td><td data-col='monthly'>{esc(phone['monthly_fee'])}</td><td data-col='currency'>{esc(phone['currency_code'])}</td><td data-col='phone_type'>{esc(phone['phone_type'])}</td><td data-col='tariff'>{esc(phone['tariff_label'])}</td><td data-col='created'>{esc(phone['created_at'])}</td><td data-col='updated'>{esc(phone['updated_at'])}</td><td data-col='deactivated'>{esc(phone['deactivated_at'])}</td>{clamp_cell('comment', esc(phone['comment'] or '—'), phone['comment'] or '—', classes='comment-cell')}<td data-col='actions'>{actions}</td></tr>""")
    filters_html = f"""<form class="filter-grid" method="get" action="/phones">
<label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Провайдер <select name="provider_id">{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
    <label>Проект <select name="project">{project_options(repo, selected=q.get('project'), empty='Все')}</select></label>
    <label>Назначение <select name="assignment_type">{assignment_options(repo, selected=q.get('assignment_type'), empty='Все')}</select></label>
<label>Рабочий статус <select name="status">{phone_status_options(q.get('status'), empty='Все')}</select></label>
<label>Поиск по номеру <input name="number" value="{esc(q.get('number'))}"></label><button>Найти</button></form>"""
    create_html = f"""<form class="form-grid" method="post" action="/phones/create">
<label>Номер <span class="required">*</span><input name="number" placeholder="393331234567"></label><label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label><label>Провайдер <span class="required">*</span><select name="provider_id"><option value="">—</option>{active_options(repo, 'providers')}</select></label><label>Проект <select name="project_label">{project_options(repo, empty='—')}</select></label><label>Назначение <span class="required">*</span><select name="assignment_type">{assignment_options(repo)}</select></label><label>Рабочий статус <span class="required">*</span><select name="status">{phone_status_options('unknown')}</select></label><label>Стоимость подключения <input name="connection_cost"></label><label>Абонентская плата <input name="monthly_fee"></label><label>Валюта <select name="currency_id"><option value="">—</option>{active_options(repo, 'currencies', 'code')}</select></label><label>Тип номера <select name="phone_type">{phone_type_options(repo, empty='—')}</select></label><label>Тариф <input name="tariff_label"></label><label>Комментарий <input name="comment"></label><button>Сохранить</button></form>"""
    table_html = f"{data_table('phones', [('number', f"<span class='copyable-header'>Номер {copy_column_button('phone-number')}</span>"), ('geo', 'ГЕО'), ('provider', 'Провайдер'), ('project', 'Проект'), ('assignment', 'Назначение'), ('status', 'Рабочий статус'), ('active', 'Активен у провайдера'), ('routes', 'Маршруты'), ('connection', 'Подключение'), ('monthly', 'Абонплата'), ('currency', 'Валюта'), ('phone_type', 'Тип номера'), ('tariff', 'Тариф'), ('created', 'Дата создания'), ('updated', 'Дата изменения'), ('deactivated', 'Дата отключения'), ('comment', 'Комментарий'), ('actions', 'Действия')], ''.join(rows))}"
    body = f"""
<h1>Купленные номера</h1>
{filter_card(filters_html, q, ('country_id', 'provider_id', 'project', 'assignment_type', 'status', 'number'))}
{form_card('+ Добавить номер <span class="muted">Admin</span>', create_html) if can_write("phones") else ""}
{table_card(table_html)}
{table_footer(pagination_html, export_link('/phones', q) + column_settings('phones', [('number', 'Номер'), ('geo', 'ГЕО'), ('provider', 'Провайдер'), ('project', 'Проект'), ('assignment', 'Назначение'), ('status', 'Рабочий статус'), ('active', 'Активен у провайдера'), ('routes', 'Маршруты'), ('connection', 'Подключение'), ('monthly', 'Абонплата'), ('currency', 'Валюта'), ('phone_type', 'Тип номера'), ('tariff', 'Тариф'), ('created', 'Дата создания'), ('updated', 'Дата изменения'), ('deactivated', 'Дата отключения'), ('comment', 'Комментарий'), ('actions', 'Действия')]))}"""
    return page("Купленные номера", body)


def companies_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    filters = {"server_id": q.get("server_id"), "country_id": q.get("country_id"), "company_like": q.get("company"), "external_id_like": q.get("external_id"), "has_autorotation": q.get("has_autorotation"), "is_active": q.get("is_active")}
    records = list(repo.list_calling_companies(filters))
    if q.get("export") == "csv":
        return csv_response("companies_export.csv", ["Название", "GEO", "Проект", "Активен", "Комментарий"], [[c["company_name"], c["country_name"], c["company_id_external"], "Да" if c["is_active"] else "Нет", c["comment"]] for c in records])
    records, pagination_html = paginate_rows(records, q, "/companies")
    rows = []
    for cc in records:
        actions = f"<a class='button edit-action' href='/companies/{cc['id']}/edit' title='Редактировать' aria-label='Редактировать' data-tooltip='Редактировать'>Редактировать</a>" if can_write("companies") else ""
        rows.append(f"<tr><td data-col='server'>{esc(cc['server_name'])}</td><td data-col='geo'>{esc(cc['country_name'])}</td>{clamp_cell('company_name', esc(cc['company_name']), cc['company_name'])}<td data-col='company_id'>{esc(cc['company_id_external'])}</td><td data-col='lines'>{esc(cc['line_count'])}</td><td data-col='dial_sets'>{esc(cc['dial_set_count'])}</td><td data-col='autorotation'>{'Да' if cc['has_autorotation'] else 'Нет'}</td><td data-col='retry_interval'>{esc(cc['retry_interval_seconds'])}</td><td data-col='active'>{'Активна' if cc['is_active'] else 'Неактивна'}</td>{clamp_cell('comment', esc(cc['comment']), cc['comment'], classes='comment-cell')}<td data-col='actions'>{actions}</td></tr>")
    filters_html = f"""<form class="filter-grid" method="get" action="/companies">
<label>Сервер <select name="server_id">{options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label><label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label><label>Название кампании <input name="company" value="{esc(q.get('company'))}"></label><label>ID кампании <input name="external_id" value="{esc(q.get('external_id'))}"></label><label>Авторотация <select name="has_autorotation"><option value="">Все</option><option value="1" {'selected' if q.get('has_autorotation')=='1' else ''}>Да</option><option value="0" {'selected' if q.get('has_autorotation')=='0' else ''}>Нет</option></select></label><label>Активность <select name="is_active"><option value="">Все</option><option value="1" {'selected' if q.get('is_active')=='1' else ''}>Активна</option><option value="0" {'selected' if q.get('is_active')=='0' else ''}>Неактивна</option></select></label><button>Найти</button></form>"""
    create_html = f"""<form class="form-grid" method="post" action="/companies/create"><label>Сервер <span class="required">*</span><select name="server_id">{active_options(repo, 'servers')}</select></label><label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label><label>ID кампании <span class="required">*</span><input name="company_id_external"></label><label>Название кампании <span class="required">*</span><input name="company_name"></label><label>Количество линий <span class="required">*</span><input name="line_count" value="0"></label><label>Количество наборов <span class="required">*</span><input name="dial_set_count" value="0"></label><label>Авторотация <span class="required">*</span><select name="has_autorotation"><option value="1">Да</option><option value="0">Нет</option></select></label><label>Интервал дозвона, сек. <span class="required">*</span><input name="retry_interval_seconds" value="0"></label><label>Активна <span class="required">*</span><select name="is_active"><option value="1">Да</option><option value="0">Нет</option></select></label><label>Комментарий <input name="comment"></label><button>Сохранить</button></form>"""
    table_html = f"{data_table('companies', [('server', 'Сервер'), ('geo', 'ГЕО'), ('company_name', 'Название кампании'), ('company_id', 'ID кампании'), ('lines', 'Количество линий'), ('dial_sets', 'Количество наборов'), ('autorotation', 'Авторотация'), ('retry_interval', 'Интервал между попытками дозвона (сек.)'), ('active', 'Активна'), ('comment', 'Комментарий'), ('actions', 'Действия')], ''.join(rows))}"
    body = f"""
<h1>Кампании прозвона</h1>
{filter_card(filters_html, q, ('server_id', 'country_id', 'company', 'external_id', 'has_autorotation', 'is_active'))}
{form_card('+ Добавить кампанию <span class="muted">Admin</span>', create_html) if can_write("companies") else ""}
{table_card(table_html)}
{table_footer(pagination_html, export_link('/companies', q) + column_settings('companies', [('server', 'Сервер'), ('geo', 'ГЕО'), ('company_name', 'Название кампании'), ('company_id', 'ID кампании'), ('lines', 'Количество линий'), ('dial_sets', 'Количество наборов'), ('autorotation', 'Авторотация'), ('retry_interval', 'Интервал дозвона'), ('active', 'Активна'), ('comment', 'Комментарий'), ('actions', 'Действия')]))}"""
    return page("Кампании прозвона", body)

def routing_reason_options(selected: str | None = None) -> str:
    return "".join(
        f"<option value='{esc(reason)}' {'selected' if reason == selected else ''}>{esc(reason)}</option>"
        for reason in Repository.ROUTING_EVENT_REASONS
    )


def routing_scope_options(selected: str | None = None, empty: str | None = "Все") -> str:
    labels = {
        "none": "Не меняли настройки в нашей системе",
        "server_priority": "Серверный приоритет",
        "campaign_setting": "Настройка кампании",
    }
    opts = f"<option value=''>{esc(empty)}</option>" if empty is not None else ""
    return opts + "".join(f"<option value='{key}' {'selected' if key == selected else ''}>{label}</option>" for key, label in labels.items())



def active_country_id_if_single(repo: Repository) -> int | None:
    rows = repo.conn.execute("SELECT id FROM countries WHERE is_active = 1 ORDER BY name").fetchall()
    return rows[0]["id"] if len(rows) == 1 else None

def route_options_for_dynamic_form(repo: Repository, selected: object | None = None, empty: str | None = "—") -> str:
    opts = f"<option value=''>{esc(empty)}</option>" if empty is not None else ""
    rows = repo.conn.execute(
        """
        SELECT r.id, r.name, r.country_id, r.provider_id, c.name AS country_name, p.name AS provider_name
        FROM routes r
        JOIN countries c ON c.id = r.country_id
        JOIN providers p ON p.id = r.provider_id
        WHERE r.is_actual = 1 OR r.id = ?
        ORDER BY c.name, p.name, r.name
        """,
        (selected or 0,),
    )
    for row in rows:
        label = f"{row['country_name']} / {row['provider_name']} / {row['name']}"
        opts += (
            f"<option value='{row['id']}' data-country-id='{row['country_id']}' data-provider-id='{row['provider_id']}' "
            f"{'selected' if str(row['id']) == str(selected) else ''}>{esc(label)}</option>"
        )
    return opts


def route_metadata_json(repo: Repository) -> str:
    rows = repo.conn.execute(
        """
        SELECT r.id, r.name, r.country_id, r.provider_id, c.name AS country_name, p.name AS provider_name
        FROM routes r
        JOIN countries c ON c.id = r.country_id
        JOIN providers p ON p.id = r.provider_id
        WHERE r.is_actual = 1
        ORDER BY c.name, p.name, r.name
        """
    ).fetchall()
    return json.dumps([
        {
            "id": row["id"],
            "country_id": row["country_id"],
            "provider_id": row["provider_id"],
            "label": f"{row['country_name']} / {row['provider_name']} / {row['name']}",
        }
        for row in rows
    ], ensure_ascii=False)


def current_priorities_json(repo: Repository) -> str:
    rows = repo.conn.execute(
        """
        SELECT srp.country_id, srp.server_id, COALESCE(c.name || ' / ' || p.name || ' / ' || r.name, '—') AS route_label
        FROM server_route_priorities srp
        LEFT JOIN countries c ON c.id = srp.country_id
        LEFT JOIN routes r ON r.id = srp.current_route_id
        LEFT JOIN providers p ON p.id = r.provider_id
        """
    ).fetchall()
    return json.dumps({f"{row['country_id']}:{row['server_id']}": row["route_label"] for row in rows}, ensure_ascii=False)


def campaign_metadata_json(repo: Repository) -> str:
    rows = repo.conn.execute("SELECT id, country_id FROM calling_companies ORDER BY id").fetchall()
    return json.dumps({str(row["id"]): row["country_id"] for row in rows}, ensure_ascii=False)


def routing_event_form(repo: Repository, event=None) -> str:
    event_at = (event["event_at"] if event else datetime.now().strftime("%Y-%m-%d %H:%M")).replace(" ", "T")[:16]
    scope = event["apply_scope"] if event else "none"
    route_opts = route_options_for_dynamic_form(repo, selected=event["affected_route_id"] if event else None, empty="—")
    new_route_opts = route_options_for_dynamic_form(repo, selected=event["new_route_id"] if event else None, empty="—")
    company_route_opts = route_options_for_dynamic_form(repo, selected=event["new_company_route_id"] if event else None, empty="—")
    company_opts = select_options(repo, "SELECT id, company_id_external || ' / ' || company_name AS label FROM calling_companies WHERE is_active = 1 OR id = ? ORDER BY company_id_external", (event["calling_company_id"] if event else 0,), selected=event["calling_company_id"] if event else None, empty="—")
    selected_server_ids = {str(event["server_id"])} if event and event["server_id"] else set()
    server_priority_server_boxes = active_server_priority_checkboxes(repo, selected_server_ids, event["country_id"] if event else None)
    action = f"/provider-changes/{event['id']}/update" if event else "/provider-changes/create"
    submit = "Сохранить изменения" if event else "Создать событие"
    inactive_note = "<p class='muted'>Редактирование события не применяет повторно server_route_priorities. Для исправления текущего приоритета создайте новое событие.</p>" if event else ""
    old_route_field = f"<label class='scope-field' data-scopes='server_priority'>Старый маршрут (только описание при редактировании) <select name='old_route_id'>{route_options_for_dynamic_form(repo, selected=event['old_route_id'] if event else None, empty='—')}</select></label>" if event else ""
    provider_selected = event["provider_id"] if event else None
    return f"""
<details class='form-card' {'open' if event else ''}><summary class='form-summary'>{'Редактировать событие' if event else '+ Добавить событие'}</summary>
<form method='post' action='{action}' class='form-grid' id='routing-event-form' data-default-country-id='{esc(active_country_id_if_single(repo) or '')}'>
  <fieldset><legend>Область применения</legend>
    <div class='scope-cards'>
      <label class='card scope-card'><input type='radio' name='apply_scope' value='none' {'checked' if scope == 'none' else ''}><span class='scope-card-indicator' aria-hidden='true'></span><span class='scope-card-text'>Не меняли настройки в нашей системе</span></label>
      <label class='card scope-card'><input type='radio' name='apply_scope' value='server_priority' {'checked' if scope == 'server_priority' else ''}><span class='scope-card-indicator' aria-hidden='true'></span><span class='scope-card-text'>Серверный приоритет</span></label>
      <label class='card scope-card'><input type='radio' name='apply_scope' value='campaign_setting' {'checked' if scope == 'campaign_setting' else ''}><span class='scope-card-indicator' aria-hidden='true'></span><span class='scope-card-text'>Настройка кампании</span></label>
    </div>
  </fieldset>
  {inactive_note}
  <label>Дата события <span class='required'>*</span><input type='datetime-local' name='event_at' value='{esc(event_at)}' required></label>
  <label class='scope-field' data-scopes='none server_priority'>GEO <select name='country_id' id='event-country'>{active_options(repo, 'countries', selected=event['country_id'] if event else None, empty='—')}</select></label>
  <fieldset class='scope-field' data-scopes='server_priority'><legend>Серверы <span class='required'>*</span></legend>{server_priority_server_boxes}</fieldset>
  <label class='scope-field' data-scopes='none server_priority'>Провайдер <span class='required provider-required'>*</span><select name='provider_id' id='event-provider'>{active_options(repo, 'providers', selected=provider_selected, empty='—')}</select></label>
  <label class='scope-field' data-scopes='none'>Маршрут/префикс <select name='affected_route_id' id='affected-route'>{route_opts}</select></label>
  {old_route_field}
  <label class='scope-field' data-scopes='server_priority'>Новый маршрут <span class='required'>*</span><select name='new_route_id' id='new-route'>{new_route_opts}</select></label>
  <span class='scope-field route-empty-message muted' data-scopes='server_priority' id='new-route-empty' hidden>Нет маршрутов для выбранного провайдера и GEO</span>
  <label class='scope-field' data-scopes='campaign_setting'>Кампания <span class='required'>*</span><select name='calling_company_id' id='event-company'>{company_opts}</select></label>
  <label class='scope-field' data-scopes='campaign_setting'>Тип изменения кампании <span class='required'>*</span><select name='company_change_type' id='company-change-type'>
    <option value=''>—</option>
    {''.join(f"<option value='{v}' {'selected' if event and event['company_change_type'] == v else ''}>{label}</option>" for v, label in [('enable_autorotation','Включили авторотацию'),('disable_autorotation','Выключили авторотацию'),('set_campaign_route','Прописали ручной маршрут'),('remove_campaign_route','Убрали ручной маршрут')])}
  </select></label>
  <label class='scope-field conditional-field' data-scopes='campaign_setting' data-campaign-route-field='1'>Новый провайдер кампании <span class='required'>*</span><select name='campaign_provider_id' id='campaign-provider'>{active_options(repo, 'providers', selected=provider_selected, empty='—')}</select></label>
  <label class='scope-field conditional-field' data-scopes='campaign_setting' data-campaign-route-field='1'>Новый маршрут кампании <span class='required'>*</span><select name='new_company_route_id' id='company-route'>{company_route_opts}</select></label>
  <span class='scope-field route-empty-message muted' data-scopes='campaign_setting' id='company-route-empty' hidden>Нет маршрутов для выбранного провайдера и GEO кампании</span>
  <label>Причина <span class='required'>*</span><select name='reason' required>{routing_reason_options(event['reason'] if event else None)}</select></label>
  <label class='wide'>Комментарий <span class='required'>*</span><textarea name='comment' rows='3' cols='60' required>{esc(event['comment'] if event else '')}</textarea></label>
  <p class='scope-field muted wide' data-scopes='campaign_setting'>Событие будет сохранено в журнале и применено к ‘Схеме маршрутизации кампаний’.</p>
  <p class='scope-field muted wide' data-scopes='server_priority'>Старый маршрут подтягивается автоматически из текущего server_route_priorities при создании.</p>
  <button>{submit}</button>
</form>
<script>
(function() {{
  const form = document.getElementById('routing-event-form');
  if (!form) return;
  const routes = {route_metadata_json(repo)};
  const priorities = {current_priorities_json(repo)};
  const campaignCountries = {campaign_metadata_json(repo)};
  const routeNeeds = new Set(['set_campaign_route']);
  function selectedScope() {{ return (form.querySelector('input[name="apply_scope"]:checked') || {{value: 'none'}}).value; }}
  function setRequired(el, required) {{ if (el) el.required = !!required; }}
  function rebuildRouteSelect(select, countryId, providerId, emptyEl) {{
    if (!select) return;
    const current = select.value;
    select.innerHTML = '<option value="">—</option>';
    let count = 0;
    routes.forEach((route) => {{
      if ((!countryId || String(route.country_id) === String(countryId)) && (!providerId || String(route.provider_id) === String(providerId))) {{
        const opt = document.createElement('option');
        opt.value = route.id;
        opt.textContent = route.label;
        if (String(route.id) === String(current)) opt.selected = true;
        select.appendChild(opt);
        count += 1;
      }}
    }});
    if (emptyEl) emptyEl.hidden = !(countryId && providerId && count === 0);
  }}
  function sync() {{
    const scope = selectedScope();
    form.querySelectorAll('.scope-card').forEach((card) => card.classList.toggle('selected', card.querySelector('input').checked));
    form.querySelectorAll('.scope-field').forEach((el) => {{
      const show = (el.dataset.scopes || '').split(' ').includes(scope);
      el.hidden = !show;
      el.querySelectorAll('input, select, textarea').forEach((field) => {{ if (!show) field.required = false; }});
    }});
    const country = document.getElementById('event-country');
    const provider = document.getElementById('event-provider');
    const hintCountryId = (country && country.value) || form.dataset.defaultCountryId || '';
    form.querySelectorAll('[data-server-chip]').forEach((chip) => {{
      const key = `${{hintCountryId}}:${{chip.dataset.serverId}}`;
      const route = hintCountryId ? (priorities[key] || '—') : '—';
      chip.dataset.currentRoute = route;
      const hint = chip.querySelector('[data-current-route-hint]');
      if (hint) {{
        hint.textContent = `текущий: ${{route}}`;
        hint.title = hint.textContent;
      }}
    }});
    renderCurrentRoutes();
    rebuildRouteSelect(document.getElementById('affected-route'), country && country.value, provider && provider.value, null);
    rebuildRouteSelect(document.getElementById('new-route'), country && country.value, provider && provider.value, document.getElementById('new-route-empty'));
    const company = document.getElementById('event-company');
    const campaignProvider = document.getElementById('campaign-provider');
    const companyCountry = company ? campaignCountries[company.value] : '';
    rebuildRouteSelect(document.getElementById('company-route'), companyCountry, campaignProvider && campaignProvider.value, document.getElementById('company-route-empty'));
    const ctype = document.getElementById('company-change-type');
    const needsRoute = scope === 'campaign_setting' && routeNeeds.has(ctype && ctype.value);
    form.querySelectorAll('[data-campaign-route-field]').forEach((el) => {{ el.hidden = !needsRoute; el.querySelectorAll('select').forEach((f) => f.required = needsRoute); }});
    setRequired(country, scope === 'server_priority');
    setRequired(provider, scope === 'none' || scope === 'server_priority');
    setRequired(document.getElementById('new-route'), scope === 'server_priority');
    setRequired(company, scope === 'campaign_setting');
    setRequired(ctype, scope === 'campaign_setting');
  }}
  function renderCurrentRoutes() {{
    const panel = form.querySelector('[data-server-current-routes]');
    if (!panel) return;
    const checkedBoxes = Array.from(form.querySelectorAll('input[name="server_ids"]:checked'));
    if (!checkedBoxes.length) {{
      panel.innerHTML = '<span class="server-current-routes-empty">Выберите серверы, чтобы увидеть текущие маршруты</span>';
      return;
    }}
    panel.innerHTML = '';
    checkedBoxes.forEach((box) => {{
      const chip = box.closest('[data-server-chip]');
      const name = chip ? chip.dataset.serverName : box.value;
      const route = (chip && chip.dataset.currentRoute) || (chip && chip.dataset.initialRoute) || '—';
      const row = document.createElement('div');
      row.className = 'server-current-route-row';
      const server = document.createElement('span');
      server.className = 'server-current-route-name';
      server.textContent = name;
      const text = document.createElement('span');
      text.className = 'server-current-route-text';
      text.title = `текущий: ${{route}}`;
      const label = document.createElement('span');
      label.className = 'server-current-route-label';
      label.textContent = 'текущий: ';
      text.append(label, document.createTextNode(route));
      row.append(server, text);
      panel.appendChild(row);
    }});
  }}
  function updateServerSelectionCount() {{
    const boxes = Array.from(form.querySelectorAll('input[name="server_ids"]'));
    const counter = form.querySelector('[data-server-selection-count]');
    if (counter) {{
      counter.textContent = `${{boxes.filter((box) => box.checked).length}} из ${{boxes.length}} выбрано`;
    }}
    renderCurrentRoutes();
  }}
  form.querySelectorAll('[data-server-select]').forEach((button) => button.addEventListener('click', () => {{
    const checked = button.dataset.serverSelect === 'all';
    form.querySelectorAll('input[name="server_ids"]').forEach((box) => {{ box.checked = checked; }});
    updateServerSelectionCount();
  }}));
  form.querySelectorAll('input[name="server_ids"]').forEach((box) => box.addEventListener('change', updateServerSelectionCount));
  updateServerSelectionCount();
  form.querySelectorAll('input[name="apply_scope"], #event-country, #event-provider, #event-company, #campaign-provider, #company-change-type').forEach((el) => el.addEventListener('change', sync));
  sync();
}})();
</script>
</details>"""


def routing_event_status_label(status: str | None) -> str:
    labels = {
        "applied": "применено",
        "skipped_noop": "пропущено: уже был выбран этот маршрут",
    }
    return labels.get(status or "", status or "")


def routing_event_snapshot(ev) -> dict:
    raw = ev["snapshot_json"] if "snapshot_json" in ev.keys() else None
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def provider_event_details(ev) -> tuple[str, str, str]:
    """Return server, campaign and route/provider details appropriate for a routing event scope."""
    scope = ev["apply_scope"]
    if scope == "none":
        route_parts = []
        if ev["provider_name"]:
            route_parts.append(f"Провайдер: {esc(ev['provider_name'])}")
        if ev["affected_route_name"]:
            route_parts.append(f"Маршрут/префикс: {esc(ev['affected_route_name'])}")
        return "—", "—", "; ".join(route_parts) or "—"
    if scope == "server_priority":
        snapshot = routing_event_snapshot(ev)
        affected_servers = snapshot.get("affected_servers")
        if isinstance(affected_servers, list) and affected_servers:
            items = []
            server_names = []
            for affected in affected_servers:
                if not isinstance(affected, dict):
                    continue
                server_name = affected.get("server_name") or "—"
                old_route = affected.get("old_route") or "—"
                new_route = affected.get("new_route") or "—"
                status = routing_event_status_label(affected.get("status"))
                server_names.append(str(server_name))
                status_text = f" · {esc(status)}" if status else ""
                items.append(f"<li>{esc(server_name)}: {esc(old_route)} → {esc(new_route)}{status_text}</li>")
            if items:
                unique_names = list(dict.fromkeys(server_names))
                server_text = ", ".join(unique_names) if unique_names else "—"
                return server_text, "—", "Серверы:<ul class='event-server-list'>" + "".join(items) + "</ul>"
        route_text = f"{esc(ev['old_route_name'] or '—')} → {esc(ev['new_route_name'] or '—')}"
        return ev["server_name"] or "—", "—", route_text
    campaign = "—"
    if ev["company_id_external"] or ev["company_name"]:
        campaign = f"{ev['company_id_external'] or '—'} / {ev['company_name'] or '—'}"
    def company_route_label(prefix: str) -> str:
        route_id = ev[f"{prefix}_company_route_id"]
        route_name = ev[f"{prefix}_company_route_name"] if f"{prefix}_company_route_name" in ev.keys() else None
        provider_name = ev[f"{prefix}_company_route_provider_name"] if f"{prefix}_company_route_provider_name" in ev.keys() else None
        if not route_id:
            return "—"
        return f"{provider_name} / {route_name}" if route_name and provider_name else str(route_id)

    details = []
    if ev["company_change_type"]:
        details.append(esc(COMPANY_CHANGE_LABELS.get(ev["company_change_type"], ev["company_change_type"])))
    if ev["old_company_routing_mode"] or ev["new_company_routing_mode"]:
        details.append(f"Режим: {esc(ev['old_company_routing_mode'] or '—')} → {esc(ev['new_company_routing_mode'] or '—')}")
    if ev["old_company_route_id"] or ev["new_company_route_id"]:
        details.append(f"Маршрут: {esc(company_route_label('old'))} → {esc(company_route_label('new'))}")
    if ev["old_company_has_autorotation"] is not None or ev["new_company_has_autorotation"] is not None:
        old_auto = 'Да' if ev["old_company_has_autorotation"] else 'Нет'
        new_auto = 'Да' if ev["new_company_has_autorotation"] else 'Нет'
        details.append(f"Авторотация: {old_auto} → {new_auto}")
    return "—", campaign, "; ".join(details) or "—"


def provider_changes_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    filters = {"country_id": q.get("country_id"), "apply_scope": q.get("apply_scope"), "server_id": q.get("server_id"), "campaign_id": q.get("campaign_id"), "provider_id": q.get("provider_id"), "include_inactive": q.get("include_inactive") == "1"}
    records = list(repo.list_routing_events(filters))
    if q.get("export") == "csv":
        export_rows = []
        for ev in records:
            server_text, campaign_text, details_text = provider_event_details(ev)
            details_plain = re.sub(r"<[^>]+>", " ", details_text)
            export_rows.append([ev["event_at"], ev["country_name"], ev["old_route_name"] or "—", ev["new_route_name"] or ev["new_company_route_name"] or "—", ev["reason"], ROUTING_SCOPE_LABELS.get(ev["apply_scope"], ev["apply_scope"]), "Активна" if ev["is_active"] else "Неактивна", ev["comment"] or details_plain])
        return csv_response("provider_changes_export.csv", ["Дата", "GEO", "Старый провайдер", "Новый провайдер", "Причина", "Scope", "Статус", "Комментарий"], export_rows)
    records, pagination_html = paginate_rows(records, q, "/provider-changes")
    rows = []
    for ev in records:
        server_text, campaign_text, details_text = provider_event_details(ev)
        actions = f"<a class='button edit-action' href='/provider-changes/{ev['id']}/edit' title='Редактировать' aria-label='Редактировать' data-tooltip='Редактировать'>Редактировать</a>" if can_write("provider_changes") else ""
        if ev["is_active"] and can_write("provider_changes"):
            actions += f"<details><summary>Деактивировать</summary><form method='post' action='/provider-changes/{ev['id']}/deactivate'><label>Причина <span class='required'>*</span><input name='deactivation_reason' required></label><button>Деактивировать</button></form></details>"
        rows.append(f"<tr class='{'' if ev['is_active'] else 'inactive-row'}'><td data-col='event_at'>{esc(ev['event_at'])}</td><td data-col='scope'>{esc(ROUTING_SCOPE_LABELS.get(ev['apply_scope'], ev['apply_scope']))}</td><td data-col='geo'>{esc(ev['country_name'])}</td><td data-col='server'>{esc(server_text)}</td><td data-col='campaign'>{esc(campaign_text)}</td>{clamp_cell('details', details_text, details_text)}{clamp_cell('reason', esc(ev['reason']), ev['reason'])}{clamp_cell('comment', esc(ev['comment']), ev['comment'])}<td data-col='active'>{'Да' if ev['is_active'] else 'Нет'}</td><td data-col='actions' class='actions'>{actions}</td></tr>")
    if not rows:
        rows.append("<tr><td colspan='10'><div class='empty-state'>Событий пока нет</div></td></tr>")
    filters_html = f"""<form class='filter-grid' method='get' action='/provider-changes'>
<label>GEO <select name='country_id'>{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Область применения <select name='apply_scope'>{routing_scope_options(q.get('apply_scope'))}</select></label>
<label>Сервер <select name='server_id'>{options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label>
<label>Кампания ID <input name='campaign_id' value='{esc(q.get('campaign_id'))}'></label>
<label>Провайдер <select name='provider_id'>{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
<label class='checkbox-inline'><input type='checkbox' name='include_inactive' value='1' {'checked' if q.get('include_inactive') == '1' else ''}> Показывать архив/неактивные</label>
<button>Найти</button></form>"""
    journal_html = f"{data_table('provider_changes', [('event_at', 'Дата события'), ('scope', 'Область применения'), ('geo', 'GEO'), ('server', 'Сервер'), ('campaign', 'Кампания'), ('details', 'Детали'), ('reason', 'Причина'), ('comment', 'Комментарий'), ('active', 'Активна'), ('actions', 'Действия')], ''.join(rows))}"
    body = f"""
<h1>Смена провайдеров</h1>
{routing_event_form(repo) if can_write("provider_changes") else ""}
{filter_card(filters_html, q, ('country_id', 'apply_scope', 'server_id', 'campaign_id', 'provider_id', 'include_inactive'))}
{table_card(journal_html, title='Журнал событий', extra_class='journal-card')}
{table_footer(pagination_html, export_link('/provider-changes', q) + column_settings('provider_changes', [('event_at', 'Дата события'), ('scope', 'Область применения'), ('geo', 'GEO'), ('server', 'Сервер'), ('campaign', 'Кампания'), ('details', 'Детали'), ('reason', 'Причина'), ('comment', 'Комментарий'), ('active', 'Активна'), ('actions', 'Действия')]))}"""
    return page("Смена провайдеров", body)


def admin_page(repo: Repository) -> bytes:
    cards = "".join(
        f'<a class="card" href="{href}">{label}</a>'
        for key, href, label, _ in ADMIN_NAV_ITEMS
        if can_read(key)
    )
    body = f"""
<h1>Администрирование</h1><div class="grid">
{cards}
</div>"""
    return page("Администрирование", body)



def role_options(selected: str | None = None) -> str:
    opts = ""
    for value, label in (("admin", "Admin"), ("operator", "Дежурный"), ("guest", "Гость")):
        opts += f"<option value='{value}' {'selected' if value == selected else ''}>{esc(label)}</option>"
    return opts


def users_page(repo: Repository) -> bytes:
    rows = []
    for user in repo.list_users(active_only=False):
        rows.append(f"""
<tr class="{'inactive-row' if not user['is_active'] else ''}">
  <td>{user['id']}</td>
  <td><code>{esc(user['username'])}</code></td>
  <td>{esc(user['display_name'])}</td>
  <td><span class='status-badge'>{esc(role_label(user['role_key']))}</span></td>
  <td>{'Да' if user['is_active'] else 'Нет'}</td>
  <td>{esc(user['created_at'])}</td>
  <td>{esc(user['updated_at'])}</td>
  <td data-col='actions' class='actions'>
    <details class='edit-details'><summary>Редактировать</summary>
      <form class='form-grid' method='post' action='/admin/users/{user['id']}/update'>
        <label>Отображаемое имя <input name='display_name' value='{esc(user['display_name'])}' required></label>
        <label>Роль-метка <select name='role_key'>{role_options(user['role_key'])}</select></label>
        <label>Активен <select name='is_active'><option value='1' {'selected' if user['is_active'] else ''}>Да</option><option value='0' {'selected' if not user['is_active'] else ''}>Нет</option></select></label>
        <button>Сохранить</button>
      </form>
    </details>
  </td>
</tr>""")
    create_html = f"""<form class='form-grid' method='post' action='/admin/users/create'>
<label>Код пользователя <span class='required'>*</span><input name='username' placeholder='operator2' required></label>
<label>Отображаемое имя <span class='required'>*</span><input name='display_name' placeholder='Оператор' required></label>
<label>Роль-метка <select name='role_key'>{role_options('operator')}</select></label>
<p class='muted wide'>Пароли не используются. Доступ определяется ролью: admin, operator или guest.</p>
<button>Создать</button></form>"""
    table_html = f"<table><thead><tr><th>ID</th><th>Код</th><th>Имя</th><th>Роль-метка</th><th>Активен</th><th>Создан</th><th>Обновлён</th><th data-col='actions'>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    body = f"""
<h1>Пользователи</h1>
<p class='muted'>Это лёгкий выбор текущего пользователя для MVP, без паролей. Права доступа зависят от роли. Индивидуальные права по разделам для отдельных пользователей пока не реализованы.</p>
{form_card('+ Создать пользователя', create_html)}
{table_card(table_html)}"""
    return page("Пользователи", body)

def server_priorities_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    server_params: list[object] = []
    server_where = "WHERE is_active = 1"
    if q.get("server_id"):
        server_where += " AND id = ?"
        server_params.append(q["server_id"])
    servers = list(repo.conn.execute(f"SELECT id, name FROM servers {server_where} ORDER BY name", server_params))
    server_names = {row["id"]: row["name"] for row in servers}
    server_rows: dict[int, list[str]] = {row["id"]: [] for row in servers}

    priority_clauses = ["s.is_active = 1"]
    priority_params: list[object] = []
    if q.get("country_id"):
        priority_clauses.append("srp.country_id = ?")
        priority_params.append(q["country_id"])
    if q.get("server_id"):
        priority_clauses.append("srp.server_id = ?")
        priority_params.append(q["server_id"])
    priority_where = " WHERE " + " AND ".join(priority_clauses)
    priority_records = list(repo.conn.execute(f"""
        SELECT srp.*, c.name AS country_name, s.name AS server_name,
               cp.name AS current_provider_name, pp.name AS previous_provider_name,
               cr.name AS current_route_name, pr.name AS previous_route_name,
               u.username AS changed_by_username
        FROM server_route_priorities srp
        JOIN countries c ON c.id = srp.country_id JOIN servers s ON s.id = srp.server_id
        LEFT JOIN routes cr ON cr.id = srp.current_route_id LEFT JOIN providers cp ON cp.id = cr.provider_id
        LEFT JOIN routes pr ON pr.id = srp.previous_route_id LEFT JOIN providers pp ON pp.id = pr.provider_id
        LEFT JOIN users u ON u.id = srp.changed_by
        {priority_where}
        ORDER BY s.name, c.name
    """, priority_params))
    if q.get("export") == "csv":
        return csv_response("server_priorities_export.csv", ["GEO", "Сервер", "Провайдер/маршрут", "Приоритет", "Активен", "Комментарий"], [[row["country_name"], row["server_name"], f"{row['current_provider_name'] or '—'} / {row['current_route_name'] or '—'}", row["current_route_name"] or "—", "Да" if row["is_active"] else "Нет", row["comment"]] for row in priority_records])
    priority_records, pagination_html = paginate_rows(priority_records, q, "/admin/server-priorities")
    for row in priority_records:
        route_opts = select_options(repo, """
            SELECT r.id, r.name || ' — ' || p.name AS label
            FROM routes r
            JOIN providers p ON p.id = r.provider_id
            WHERE r.country_id = ?
            ORDER BY r.name
        """, (row["country_id"],), selected=row["current_route_id"])
        current_provider = row["current_provider_name"] or "—"
        current_route = row["current_route_name"] or "—"
        previous_provider = row["previous_provider_name"] or "—"
        previous_route = row["previous_route_name"] or "—"
        current_priority = "—" if not row["current_route_id"] else f"{esc(current_provider)} / {esc(current_route)}"
        previous_priority = "—" if not row["previous_route_id"] else f"{esc(previous_provider)} / {esc(previous_route)}"
        actions = ""
        if can_write("admin_server_priorities"):
            actions = f"""
        <details class='edit-details'><summary>Редактировать</summary>
          <div class='card'>
            ГЕО: {esc(row['country_name'])}<br>
            Сервер: {esc(row['server_name'])}<br>
            Текущий провайдер: {esc(current_provider)}<br>
            Текущий маршрут: {esc(current_route)}<br>
            Предыдущий провайдер: {esc(previous_provider)}<br>
            Предыдущий маршрут: {esc(previous_route)}<br>
            Комментарий: {esc(row['comment'])}<br>
            Дата изменения: {esc(row['changed_at'])}<br>
            Пользователь: {esc(row['changed_by_username'])}
            <form method='post' action='/admin/server-priorities/{row['id']}/update'>
              <label>Текущий маршрут <span class='required'>*</span><select name='current_route_id'>{route_opts}</select></label>
              <label>Комментарий <input name='comment' value='{esc(row['comment'])}'></label>
              <button>Сохранить текущий маршрут</button>
            </form>
          </div>
        </details>"""
        if row["server_id"] in server_rows:
            server_rows[row["server_id"]].append(
                f"<tr><td data-col='geo'>{esc(row['country_name'])}</td><td data-col='current_priority'>{current_priority}</td><td data-col='previous_priority'>{previous_priority}</td><td data-col='actions' class='actions'>{actions}</td></tr>"
            )
    blocks = []
    for server_id in server_names:
        rows = server_rows[server_id] or ["<tr><td colspan='4' class='muted'>Нет настроенных приоритетов</td></tr>"]
        table_html = f"{data_table('server_priorities', [('geo', 'GEO'), ('current_priority', 'Текущий приоритет'), ('previous_priority', 'Предыдущий приоритет'), ('actions', 'Действия')], ''.join(rows))}"
        blocks.append(f"""
<section class='server-priority-block'>
  <h2>Сервер: {esc(server_names[server_id])}</h2>
  {table_card(table_html)}
</section>""")
    filters_html = f"""<form class="filter-grid" method="get" action="/admin/server-priorities"><label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label><label>Сервер <select name="server_id">{active_options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label><button>Найти</button></form>"""
    body = f"""
<h1>Администрирование → Приоритет по серверам</h1>
{filter_card(filters_html, q, ('country_id', 'server_id'))}
{''.join(blocks)}
{table_footer(pagination_html, export_link('/admin/server-priorities', q) + column_settings('server_priorities', [('geo', 'GEO'), ('current_priority', 'Текущий приоритет'), ('previous_priority', 'Предыдущий приоритет'), ('actions', 'Действия')]))}"""
    return page("Приоритет по серверам", body)



def company_routing_settings_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    show_history = q.get("show_history") == "1"
    filters = {
        "country_id": q.get("country_id"),
        "server_id": q.get("server_id"),
        "routing_mode": q.get("routing_mode"),
        "company_id_external": q.get("company_id_external"),
        "is_active": q.get("is_active"),
        "show_history": show_history,
    }
    create_country_id = q.get("country_id") or None
    records = list(repo.list_company_routing_settings(filters))
    if q.get("export") == "csv":
        return csv_response("company_routing_settings_export.csv", ["Кампания", "GEO", "Маршрут", "Авторотация", "Активен", "Комментарий"], [[f"{r['company_id_external']} — {r['company_name']}", r["country_name"], r["route_name"] or "—", "Да" if r["has_autorotation"] else "Нет", "Да" if r["is_active"] else "Нет", r["comment"]] for r in records])
    records, pagination_html = paginate_rows(records, q, "/admin/company-routing-settings")
    rows = []
    for setting in records:
        route_label = setting["route_name"] or "—"
        provider_label = f"<br><span class='muted'>Провайдер: {esc(setting['provider_name'])}</span>" if setting["provider_name"] else ""
        active_badge = "Да" if setting["is_active"] else "Нет"
        actions = ""
        if can_write("admin_company_routing_settings") and setting["is_active"] and setting["valid_to"] is None:
            actions = f"""
            <details class='edit-details'><summary>Редактировать</summary>
              <form method='post' action='/admin/company-routing-settings/{setting['id']}/update'>
                <p class='muted'>Кампания: {esc(setting['company_id_external'])} — {esc(setting['company_name'])}</p>
                <label>GEO <select name='country_id'>{options(repo, 'countries', selected=setting['country_id'])}</select></label>
                <label>Сервер <select name='server_id'>{options(repo, 'servers', selected=setting['server_id'])}</select></label>
                <label>Режим маршрутизации <select name='routing_mode'>{routing_mode_options(setting['routing_mode'])}</select></label>
                <label>Маршрут кампании <select name='route_id'>{route_options_for_country(repo, setting['country_id'], selected=setting['route_id'])}</select></label>
                <label>Авторотация <input type='checkbox' name='has_autorotation' value='1' {'checked' if setting['has_autorotation'] else ''}></label>
                <label>Активна <input type='checkbox' name='is_active' value='1' checked></label>
                <label>Комментарий <input name='comment' value='{esc(setting['comment'])}'></label>
                <button>Сохранить</button>
              </form>
              <form method='post' action='/admin/company-routing-settings/{setting['id']}/deactivate'>
                <button onclick="return confirm('Деактивировать схему маршрутизации?')">Деактивировать</button>
              </form>
            </details>
            """
        rows.append(
            f"<tr><td data-col='geo'>{esc(setting['country_name'])}</td><td data-col='server'>{esc(setting['server_name'])}</td>"
            f"<td data-col='company_id'>{esc(setting['company_id_external'])}</td><td data-col='company_name'>{esc(setting['company_name'])}</td>"
            f"<td data-col='routing_mode'>{esc(setting['routing_mode'])}</td><td data-col='autorotation'>{'Да' if setting['has_autorotation'] else 'Нет'}</td>"
            f"<td data-col='route'>{esc(route_label)}{provider_label}</td><td data-col='active'>{active_badge}</td>"
            f"<td data-col='valid_from'>{esc(setting['valid_from'])}</td><td data-col='valid_to'>{esc(setting['valid_to'])}</td>"
            f"<td data-col='comment'>{esc(setting['comment'])}</td><td data-col='actions'>{actions}</td></tr>"
        )
    filters_html = f"""<form class="filter-grid" method="get" action="/admin/company-routing-settings">
<label>GEO <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Сервер <select name="server_id">{options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label>
<label>ID кампании <input name="company_id_external" value="{esc(q.get('company_id_external'))}"></label>
<label>Режим маршрутизации <select name="routing_mode">{routing_mode_options(q.get('routing_mode'), empty='Все')}</select></label>
<label>Активность <select name="is_active"><option value="" {'selected' if not q.get('is_active') else ''}>Все</option><option value="1" {'selected' if q.get('is_active')=='1' else ''}>Активна</option><option value="0" {'selected' if q.get('is_active')=='0' else ''}>Неактивна</option></select></label>
<label class="checkbox-inline"><input type="checkbox" name="show_history" value="1" {'checked' if show_history else ''}> Показывать историю</label>
<button>Найти</button></form>"""
    create_html = f"""<form class="form-grid" method="post" action="/admin/company-routing-settings/create">
  <label>Кампания <span class="required">*</span><select name="calling_company_id">{company_options(repo)}</select></label>
  <label>GEO <span class="required">*</span><select name="country_id">{options(repo, 'countries', selected=create_country_id)}</select></label>
  <label>Сервер <span class="required">*</span><select name="server_id">{options(repo, 'servers', selected=q.get('server_id'))}</select></label>
  <label>Режим маршрутизации <span class="required">*</span><select name="routing_mode">{routing_mode_options(q.get('routing_mode') or 'server_priority')}</select></label>
  <label>Маршрут кампании <select name="route_id">{route_options_for_country(repo, create_country_id)}</select></label>
  <label>Авторотация <input type="checkbox" name="has_autorotation" value="1"></label>
  <label>Активна <input type="checkbox" name="is_active" value="1" checked></label>
  <label>Комментарий <input name="comment"></label>
  <button>Создать</button>
</form>"""
    table_html = f"""{data_table('company_routing_settings', [('geo', 'GEO'), ('server', 'Сервер'), ('company_id', 'ID кампании'), ('company_name', 'Название кампании'), ('routing_mode', 'Режим маршрутизации'), ('autorotation', 'Авторотация'), ('route', 'Маршрут кампании'), ('active', 'Активна'), ('valid_from', 'Действует с'), ('valid_to', 'Действует до'), ('comment', 'Комментарий'), ('actions', 'Действия')], ''.join(rows))}"""
    body = f"""
<h1>Администрирование → Схема маршрутизации кампаний</h1>
{filter_card(filters_html, q, ('country_id', 'server_id', 'company_id_external', 'routing_mode', 'is_active', 'show_history'))}
{form_card('+ Добавить схему маршрутизации кампании', create_html) if can_write("admin_company_routing_settings") else ""}
<script>
document.querySelectorAll('form').forEach(form => {{
  const mode = form.querySelector('select[name="routing_mode"]');
  const autorotation = form.querySelector('input[name="has_autorotation"]');
  if (!mode || !autorotation) return;
  function syncAutorotation() {{ if (mode.value === 'autorotation') autorotation.checked = true; }}
  mode.addEventListener('change', syncAutorotation);
  syncAutorotation();
}});
</script>
{table_card(table_html)}
{table_footer(pagination_html, export_link('/admin/company-routing-settings', q) + column_settings('company_routing_settings', [('geo', 'GEO'), ('server', 'Сервер'), ('company_id', 'ID кампании'), ('company_name', 'Название кампании'), ('routing_mode', 'Режим маршрутизации'), ('autorotation', 'Авторотация'), ('route', 'Маршрут кампании'), ('active', 'Активна'), ('valid_from', 'Действует с'), ('valid_to', 'Действует до'), ('comment', 'Комментарий'), ('actions', 'Действия')]))}
"""
    return page("Схема маршрутизации кампаний", body)


def naming_rules_page(repo: Repository) -> bytes:
    rows = []
    for rule in repo.conn.execute("SELECT * FROM route_naming_rules ORDER BY is_active DESC, name"):
        rows.append(f"<tr><td>{esc(rule['name'])}</td><td>{esc(rule['template'])}</td><td>{'Да' if rule['is_active'] else 'Нет'}</td><td>{esc(rule['comment'])}</td></tr>")
    create_html = f"""<form class="form-grid" method="post" action="/admin/naming-rules/create"><label>Название <span class="required">*</span><input name="name"></label><label>Шаблон <span class="required">*</span><input name="template" value="{{country}}/{{project_label}}/{{provider}}/{{cli_source_label}}@" size="70"></label><label class="checkbox-inline"><input type="checkbox" name="is_active" value="1"> Активно</label><label>Тип номера <input name="phone_type"></label><label>Тариф <input name="tariff_label"></label><label>Комментарий <input name="comment"></label><button>Сохранить</button></form>"""
    table_html = f"<table><thead><tr><th>Название</th><th>Шаблон</th><th>Активен</th><th>Комментарий</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    body = f"""<h1>Администрирование → Правила нейминга маршрутов</h1><p class="muted">Пока без изменений: изменение шаблона не переименовывает существующие маршруты автоматически.</p>{form_card('Добавить правило', create_html)}{table_card(table_html)}"""
    return page("Правила нейминга", body)


def import_page(repo: Repository, preview_html: str = "", *, selected_entity: str = "routes", selected_mode: str = "append_update", csv_data: str = "") -> bytes:
    def sel(value: str) -> str:
        return "selected" if selected_entity == value else ""
    def mode_sel(value: str) -> str:
        return "selected" if selected_mode == value else ""
    body = f"""<h1>Администрирование → Импорт / экспорт</h1><form method="post" action="/admin/import/preview"><label>Раздел <span class="required">*</span><select name="entity_type" id="entity_type"><option value="routes" {sel('routes')}>Маршруты</option><option value="tariffs" {sel('tariffs')}>Тарифы</option><option value="phone_numbers" {sel('phone_numbers')}>Купленные номера</option><option value="calling_companies" {sel('calling_companies')}>Кампании прозвона</option><option value="dictionaries" {sel('dictionaries')}>Справочники</option></select></label><label>Режим <select name="mode" id="import_mode"><option value="append_update" {mode_sel('append_update')}>Дополнить / обновить</option><option value="replace_section" {mode_sel('replace_section')}>Заменить выбранный раздел</option></select></label><p class='muted'>Для тарифов режим «Заменить выбранный раздел» недоступен: используйте только «Дополнить / обновить».</p><br><textarea name="csv_data" rows="12" cols="110" placeholder="Вставьте CSV с заголовками">{esc(csv_data)}</textarea><br><button>Предпросмотр</button><button formaction="/admin/import/apply">{nav_icon("import")}<span>Импортировать</span></button></form>{preview_html}<script>
const entity = document.getElementById('entity_type');
const mode = document.getElementById('import_mode');
function syncImportMode() {{
  const replace = [...mode.options].find(o => o.value === 'replace_section');
  if (entity.value === 'tariffs') {{ mode.value = 'append_update'; replace.disabled = true; }}
  else {{ replace.disabled = false; }}
}}
entity.addEventListener('change', syncImportMode); syncImportMode();
</script>"""
    return page("Импорт", body)


def currency_rates_page(repo: Repository) -> bytes:
    rows = []
    for rate in repo.conn.execute("""
        SELECT cr.*, c.code AS currency_code
        FROM currency_rates cr
        JOIN currencies c ON c.id = cr.currency_id
        WHERE cr.id = (
            SELECT cr2.id FROM currency_rates cr2
            WHERE cr2.currency_id = cr.currency_id
            ORDER BY cr2.rate_date DESC, cr2.created_at DESC, cr2.id DESC
            LIMIT 1
        )
        ORDER BY c.code
    """):
        rows.append(f"<tr><td>{esc(rate['currency_code'])}</td><td>{esc(rate['rate_to_eur'])}</td><td>{esc(rate['rate_date'])}</td></tr>")
    create_html = f"""<form class="form-grid" method="post" action="/admin/currency-rates/upsert">
<label>Валюта провайдера <span class="required">*</span><select name="currency_id">{active_options(repo, 'currencies', 'code')}</select></label>
<label>1 единица валюты провайдера = <input name="rate_to_eur" placeholder="0.92"> EUR</label>
<button>Применить</button></form>"""
    table_html = f"<table><thead><tr><th>Валюта</th><th>Курс к EUR</th><th>Дата курса</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    body = f"""<h1>Администрирование → Курсы валют</h1>
{form_card('Обновить курс', create_html, open_by_default=True)}
<p class="muted">Формула: Цена EUR = Цена провайдера × Курс к EUR.</p>
{table_card(table_html)}"""
    return page("Курсы валют", body)


def change_reasons_page(repo: Repository) -> bytes:
    rows = []
    for reason in repo.conn.execute("SELECT * FROM change_reasons ORDER BY is_active DESC, name"):
        rows.append(f"""<tr><td>{esc(reason['name'])}</td><td>{'Да' if reason['is_active'] else 'Нет'}</td><td>{esc(reason['description'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/change-reasons/{reason['id']}/update'><label>Название <input name='name' value='{esc(reason['name'])}'></label><label>Активна <select name='is_active'><option value='1' {'selected' if reason['is_active'] else ''}>Да</option><option value='0' {'selected' if not reason['is_active'] else ''}>Нет</option></select></label><label>Комментарий <input name='comment' value='{esc(reason['description'])}'></label><button>Сохранить</button></form></details></td></tr>""")
    create_html = "<form class='form-grid' method='post' action='/admin/change-reasons/create'><label>Название причины <span class='required'>*</span><input name='name'></label><label>Активна <select name='is_active'><option value='1'>Да</option><option value='0'>Нет</option></select></label><label>Комментарий <input name='comment'></label><button>Сохранить</button></form>"
    table_html = f"<table><thead><tr><th>Название причины</th><th>Активна</th><th>Комментарий</th><th data-col='actions'>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    return page("Причины смены провайдера", f"<h1>Администрирование → Причины смены провайдера</h1>{form_card('Добавить причину', create_html)}{table_card(table_html)}")


def dictionaries_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    sections = [
        ("countries", "GEO"),
        ("providers", "Провайдер"),
        ("currencies", "Валюта"),
        ("prefixes", "Префикс"),
        ("servers", "Сервер"),
        ("phone-types", "Тип номера"),
        ("projects", "Проект"),
        ("phone-assignments", "Назначение номера"),
    ]
    titles = dict(sections)
    active_section = q.get("section") if q.get("section") in titles else "countries"

    def active_label(value: object) -> str:
        return "Активен" if value else "Неактивен"

    def active_select(value: object) -> str:
        return f"""<select name='is_active'><option value='1' {'selected' if value else ''}>Активен</option><option value='0' {'selected' if not value else ''}>Неактивен</option></select>"""

    def row_class(row: sqlite3.Row) -> str:
        return " class='inactive-row'" if not row["is_active"] else ""

    def add_form(section: str) -> str:
        if section == "countries":
            return "<form class='form-grid' method='post' action='/admin/dictionaries/countries/create'><label>GEO <input name='name' placeholder='GEO'></label><label>Код <input name='code' placeholder='Код'></label><button>Добавить</button></form>"
        if section == "providers":
            return f"<form class='form-grid' method='post' action='/admin/dictionaries/providers/create'><label>Провайдер <input name='name' placeholder='Название провайдера'></label><label>Валюта <select name='default_currency_id'><option value=''>—</option>{options(repo, 'currencies', 'code')}</select></label><label>Комментарий <input name='comment' placeholder='Комментарий'></label><button>Добавить</button></form>"
        if section == "currencies":
            return "<form class='form-grid' method='post' action='/admin/dictionaries/currencies/create'><label>Код <input name='code' placeholder='USD'></label><label>Название <input name='name' placeholder='Название'></label><button>Добавить</button></form>"
        if section == "prefixes":
            return f"<form class='form-grid' method='post' action='/admin/dictionaries/prefixes/create'><label>Провайдер <select name='provider_id'>{options(repo, 'providers')}</select></label><label>Префикс <input name='prefix' placeholder='0827 или пусто'></label><label>Комментарий <input name='name' placeholder='Комментарий'></label><button>Добавить</button></form>"
        if section == "servers":
            return "<form class='form-grid' method='post' action='/admin/dictionaries/servers/create'><label>Сервер <input name='name' placeholder='EU3'></label><label>Комментарий <input name='comment' placeholder='Комментарий'></label><button>Добавить</button></form>"
        if section == "phone-types":
            return "<form class='form-grid' method='post' action='/admin/dictionaries/phone-types/create'><label>Тип номера <input name='name' placeholder='Mobile'></label><label>Комментарий <input name='comment' placeholder='Комментарий'></label><button>Добавить</button></form>"
        if section == "projects":
            return "<form class='form-grid' method='post' action='/admin/dictionaries/projects/create'><label>Проект <input name='name' placeholder='Competitors'></label><label>Комментарий <input name='comment' placeholder='Комментарий'></label><button>Добавить</button></form>"
        if section == "phone-assignments":
            return "<form class='form-grid' method='post' action='/admin/dictionaries/phone-assignments/create'><label>Назначение <input name='name' placeholder='Мониторинг'></label><label>Код <input name='code' placeholder='Код необязательно'></label><label>Комментарий <input name='comment' placeholder='Комментарий'></label><button>Добавить</button></form>"
        return ""

    count_queries = {
        "countries": "SELECT COUNT(*) FROM countries",
        "providers": "SELECT COUNT(*) FROM providers",
        "currencies": "SELECT COUNT(*) FROM currencies",
        "prefixes": "SELECT COUNT(*) FROM provider_prefixes",
        "servers": "SELECT COUNT(*) FROM servers",
        "phone-types": "SELECT COUNT(*) FROM phone_number_types",
        "projects": "SELECT COUNT(*) FROM projects",
        "phone-assignments": "SELECT COUNT(*) FROM phone_assignment_types",
    }

    cards = []
    for section, title in sections:
        active = " active" if section == active_section else ""
        count = repo.conn.execute(count_queries[section]).fetchone()[0]
        cards.append(f"""
<a class='dictionary-card{active}' href='/admin/dictionaries?section={section}' aria-current='{'page' if section == active_section else 'false'}'>
  <span class='dictionary-card-title'>{esc(title)}</span>
  <span class='dictionary-card-count'>{count}</span>
</a>""")

    rows: list[str] = []
    headers: list[str]
    if active_section == "countries":
        headers = ["GEO", "Код", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM countries ORDER BY name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td>{esc(row['code'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td class='muted'>—</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/countries/{row['id']}/update'><input name='name' value='{esc(row['name'])}'><input name='code' value='{esc(row['code'])}' placeholder='Код'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "providers":
        headers = ["Название", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT p.*, c.code AS currency_code FROM providers p LEFT JOIN currencies c ON c.id = p.default_currency_id ORDER BY p.name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/providers/{row['id']}/update'><input name='name' value='{esc(row['name'])}'><select name='default_currency_id'><option value=''>—</option>{options(repo, 'currencies', 'code', selected=row['default_currency_id'])}</select><input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "currencies":
        headers = ["Код валюты", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM currencies ORDER BY code"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['code'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['name'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/currencies/{row['id']}/update'><input name='code' value='{esc(row['code'])}'><input name='name' value='{esc(row['name'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "prefixes":
        headers = ["Префикс", "Провайдер", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("""
            SELECT pp.*, p.name AS provider_name
            FROM provider_prefixes pp JOIN providers p ON p.id = pp.provider_id
            ORDER BY p.name, COALESCE(pp.prefix, '')
        """))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['prefix'] or 'Без префикса')}</td><td>{esc(row['provider_name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['name'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/prefixes/{row['id']}/update'><select name='provider_id'>{options(repo, 'providers', selected=row['provider_id'])}</select><input name='prefix' value='{esc(row['prefix'])}' placeholder='Без префикса или цифры'><input name='name' value='{esc(row['name'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "servers":
        headers = ["Сервер", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM servers ORDER BY name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/servers/{row['id']}/update'><input name='name' value='{esc(row['name'])}'><input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "phone-types":
        headers = ["Тип номера", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM phone_number_types ORDER BY name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/phone-types/{row['id']}/update'><input name='name' value='{esc(row['name'])}'><input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "projects":
        headers = ["Название проекта", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM projects ORDER BY name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/projects/{row['id']}/update'><input name='name' value='{esc(row['name'])}'><input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    else:
        headers = ["Назначение", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM phone_assignment_types ORDER BY name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/phone-assignments/{row['id']}/update'><input name='name' value='{esc(row['name'])}'><input name='code' value='{esc(row['code'])}' readonly><input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")

    header_html = "".join(f"<th>{esc(header)}</th>" for header in headers)
    table_html = f"<table><thead><tr>{header_html}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    body = f"""
<h1>Администрирование → Справочные значения</h1>
<p class='muted'>Неактивные значения остаются в таблицах, но не показываются в формах создания новых записей.</p>
<div class='dictionary-layout'>
  <aside class='dictionary-sidebar'><p class='dictionary-sidebar-title'>Справочники</p>{''.join(cards)}</aside>
  <section class='dictionary-workspace'>
    <div class='dictionary-toolbar'><h2>Справочник: {esc(titles[active_section])}</h2><span class='dictionary-total'>Всего записей: {len(source)}</span></div>
    <details class='dictionary-add'><summary>+ Добавить значение</summary>{add_form(active_section)}</details>
    {table_card(table_html)}
  </section>
</div>"""
    return page("Справочные значения", body)


def telegram_page(repo: Repository) -> bytes:
    settings = repo.conn.execute("SELECT * FROM telegram_settings ORDER BY id DESC LIMIT 1").fetchone()
    body = f"""<h1>Администрирование → Telegram</h1><form method="post" action="/admin/telegram/save"><label>Включен <input type="checkbox" name="is_enabled" value="1" {'checked' if settings and settings['is_enabled'] else ''}></label><label>Chat ID <input name="chat_id" value="{esc(settings['chat_id'] if settings else '')}"></label><label>Bot token secret ref <input name="bot_token_secret_ref" value="{esc(settings['bot_token_secret_ref'] if settings else '')}"></label><br><label>Шаблон сообщения<br><textarea name="message_template" rows="6" cols="100">{esc(settings['message_template'] if settings else '')}</textarea></label><br><button>Сохранить настройки</button><button formaction="/admin/telegram/test">Отправить тестовое сообщение</button></form><p>Последний тест: {esc(settings['last_test_status'] if settings else '—')} {esc(settings['last_test_at'] if settings else '')}</p>"""
    return page("Telegram", body)


def change_log_page(repo: Repository) -> bytes:
    rows = []
    for log in repo.conn.execute("SELECT cl.*, u.username FROM change_log cl LEFT JOIN users u ON u.id = cl.changed_by ORDER BY cl.changed_at DESC, cl.id DESC LIMIT 100"):
        rows.append(f"<tr><td>{esc(log['changed_at'])}</td><td>{esc(log['entity_type'])}</td><td>{esc(log['entity_id'])}</td><td>{esc(log['change_type'])}</td><td>{esc(log['username'])}</td><td>{esc(log['summary'])}</td></tr>")
    table_html = f"<table><thead><tr><th>Дата (UTC/server time)</th><th>Entity</th><th>ID</th><th>Change</th><th>Кто</th><th>Summary</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    return page("Change log", f"<h1>Change log</h1>{table_card(table_html)}")


def yes_no(value: object) -> str:
    return "1" if str(value) in {"1", "True", "true"} else "0"


def route_edit_page(repo: Repository, route_id: int) -> bytes:
    route = repo.conn.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if route is None:
        return page("Маршрут не найден", "<h1>Маршрут не найден</h1>")
    body = f"""<h1>Редактировать маршрут</h1><p><a href='/routes'>← Назад</a></p>
<form method='post' action='/routes/{route_id}/update'>
<label>Название <span class='required'>*</span><input name='name' value='{esc(route['name'])}' size='60'></label>
<label>Префикс <select name='provider_prefix_id'>{prefix_options(repo, selected=route['provider_prefix_id'])}</select></label>
<label>Комментарий <input name='comment' value='{esc(route['comment'])}'></label>
<label>Актуальный <select name='is_actual'><option value='1' {'selected' if route['is_actual'] else ''}>Активный</option><option value='0' {'selected' if not route['is_actual'] else ''}>Неактивный</option></select></label>
<label>Приоритет <select name='priority_status'><option value='priority' {'selected' if route['priority_status']=='priority' else ''}>priority</option><option value='alternative' {'selected' if route['priority_status']=='alternative' else ''}>alternative</option><option value='unknown' {'selected' if route['priority_status']=='unknown' else ''}>unknown</option></select></label>
<button onclick="return confirm('Сохранить изменения?')">Сохранить</button></form>
<div class='card'><h2>Номера маршрута / АОНы</h2><p>Управление купленными номерами доступно для каждого маршрута, даже если номеров пока нет.</p><p><a class='button' href='/routes/{route_id}/numbers/manage'>Номера маршрута / АОНы</a></p></div>"""
    return page("Редактировать маршрут", body)


def phone_edit_page(repo: Repository, phone_id: int) -> bytes:
    phone = repo.conn.execute("SELECT * FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
    if phone is None:
        return page("Номер не найден", "<h1>Номер не найден</h1>")
    body = f"""<h1>Редактировать номер</h1><p><a href='/phones'>← Назад</a></p>
<form method='post' action='/phones/{phone_id}/update'>
<label>Номер <span class='required'>*</span><input name='number' value='{esc(phone['number'])}'></label>
<label>ГЕО <span class='required'>*</span><select name='country_id'>{active_options(repo, 'countries', selected=phone['country_id'])}</select></label>
<label>Провайдер <select name='provider_id'><option value=''>—</option>{active_options(repo, 'providers', selected=phone['provider_id'])}</select></label>
<label>Проект <select name='project_label'>{project_options(repo, selected=phone['project_label'], empty='—')}</select></label>
<label>Назначение <select name='assignment_type'>{assignment_options(repo, selected=phone['assignment_type'])}</select></label>
<label>Рабочий статус <select name='status'>{phone_status_options(phone['status'])}</select></label>
<label>Активен у провайдера <select name='is_active'><option value='1' {'selected' if phone['is_active'] else ''}>Да</option><option value='0' {'selected' if not phone['is_active'] else ''}>Нет</option></select></label>
<label>Стоимость подключения <input name='connection_cost' value='{esc(phone['connection_cost'])}'></label>
<label>Абонентская плата <input name='monthly_fee' value='{esc(phone['monthly_fee'])}'></label>
<label>Валюта <select name='currency_id'><option value=''>—</option>{active_options(repo, 'currencies', 'code', selected=phone['currency_id'])}</select></label>
<label>Тип номера <select name='phone_type'>{phone_type_options(repo, selected=phone['phone_type'], empty='—')}</select></label>
<label>Тариф <input name='tariff_label' value='{esc(phone['tariff_label'])}'></label>
<label>Комментарий <input name='comment' value='{esc(phone['comment'])}'></label>
<label><input type='checkbox' name='review_required' value='1' {'checked' if phone['review_required'] else ''}> Требует проверки</label>
<p class='muted'>Поле «Маршрутов» не редактируется и считается автоматически.</p>
<button onclick="return confirm('Сохранить изменения?')">Сохранить</button></form>"""
    return page("Редактировать номер", body)


def company_edit_page(repo: Repository, company_id: int) -> bytes:
    cc = repo.conn.execute("SELECT * FROM calling_companies WHERE id = ?", (company_id,)).fetchone()
    if cc is None:
        return page("Кампания не найдена", "<h1>Кампания не найдена</h1>")
    body = f"""<h1>Редактировать кампанию</h1><p><a href='/companies'>← Назад</a></p>
<form method='post' action='/companies/{company_id}/update'>
<label>ID кампании <input value='{esc(cc['company_id_external'])}' readonly></label>
<label>Сервер <span class='required'>*</span><select name='server_id'>{active_options(repo, 'servers', selected=cc['server_id'])}</select></label>
<label>ГЕО <span class='required'>*</span><select name='country_id'>{active_options(repo, 'countries', selected=cc['country_id'])}</select></label>
<label>Название кампании <span class='required'>*</span><input name='company_name' value='{esc(cc['company_name'])}'></label>
<label>Количество линий <span class='required'>*</span><input name='line_count' value='{esc(cc['line_count'])}'></label>
<label>Количество наборов <span class='required'>*</span><input name='dial_set_count' value='{esc(cc['dial_set_count'])}'></label>
<label>Авторотация <select name='has_autorotation'><option value='1' {'selected' if cc['has_autorotation'] else ''}>Да</option><option value='0' {'selected' if not cc['has_autorotation'] else ''}>Нет</option></select></label>
<label>Интервал дозвона, сек. <input name='retry_interval_seconds' value='{esc(cc['retry_interval_seconds'])}'></label>
<label>Активна <select name='is_active'><option value='1' {'selected' if cc['is_active'] else ''}>Да</option><option value='0' {'selected' if not cc['is_active'] else ''}>Нет</option></select></label>
<label>Комментарий <input name='comment' value='{esc(cc['comment'])}'></label>
<button onclick="return confirm('Сохранить изменения?')">Сохранить</button></form>"""
    return page("Редактировать кампанию", body)


def provider_change_edit_page(repo: Repository, change_id: int) -> bytes:
    event = repo.conn.execute("SELECT * FROM routing_events WHERE id = ?", (change_id,)).fetchone()
    if event is None:
        return page("Событие не найдено", "<h1>Событие не найдено</h1>")
    body = f"""<h1>Редактировать событие смены провайдеров</h1><p><a href='/provider-changes'>← Назад</a></p>
{routing_event_form(repo, event)}
<p class='muted'>Создано: {esc(event['created_at'])}; обновлено: {esc(event['updated_at'])}</p>"""
    return page("Редактировать событие", body)


def parse_int(value: str | None) -> int | None:
    return int(value) if value not in (None, "") else None


def handle_post(repo: Repository, path: str, data: dict[str, str]):
    actor_id = current_actor_id()
    if path == "/admin/users/create":
        username = data["username"].strip()
        display_name = data["display_name"].strip()
        if not username or not display_name:
            raise BusinessRuleError("Код пользователя и имя обязательны")
        repo.create_user(username, data.get("role_key") or "operator", display_name)
        return "/admin/users"
    if path.startswith("/admin/users/") and path.endswith("/update"):
        user_id = int(path.strip("/").split("/")[2])
        display_name = data["display_name"].strip()
        if not display_name:
            raise BusinessRuleError("Отображаемое имя обязательно")
        repo.update_user(
            user_id,
            display_name=display_name,
            role_key=data.get("role_key") or "operator",
            is_active=data.get("is_active") == "1",
        )
        return "/admin/users"
    if path == "/routes/create":
        country_id = int(data["country_id"]); provider_id = int(data["provider_id"]); prefix_id = parse_int(data.get("provider_prefix_id"))
        if prefix_id:
            prefix_provider = repo.conn.execute("SELECT provider_id FROM provider_prefixes WHERE id = ?", (prefix_id,)).fetchone()
            if prefix_provider and int(prefix_provider["provider_id"]) != provider_id:
                raise BusinessRuleError("Префикс не принадлежит выбранному провайдеру")
        name = build_route_name(repo, country_id, provider_id, data.get("project_label"), data.get("cli_source_label", ""), prefix_id)
        if len(name.replace("/", "").replace("@", "").strip()) < 4:
            raise BusinessRuleError("Некорректное название маршрута: заполните ГЕО, провайдера и источник АОН")
        repo.create_route(country_id=country_id, provider_id=provider_id, provider_prefix_id=prefix_id, name=name, project_label=data.get("project_label"), cli_source_type=data["cli_source_type"], cli_source_label=data["cli_source_label"], comment=data.get("comment"), created_by=actor_id, is_actual=data.get("is_actual") == "1")
        return "/routes"
    if path.startswith("/routes/") and path.endswith("/update"):
        route_id = int(path.strip("/").split("/")[1])
        old = repo.conn.execute("SELECT name, provider_prefix_id, comment, is_actual, priority_status FROM routes WHERE id = ?", (route_id,)).fetchone()
        name = data.get("name", "").strip()
        prefix_id = parse_int(data.get("provider_prefix_id"))
        route_provider = repo.conn.execute("SELECT provider_id FROM routes WHERE id = ?", (route_id,)).fetchone()
        if prefix_id and route_provider:
            prefix_provider = repo.conn.execute("SELECT provider_id FROM provider_prefixes WHERE id = ?", (prefix_id,)).fetchone()
            if prefix_provider and int(prefix_provider["provider_id"]) != int(route_provider["provider_id"]):
                raise BusinessRuleError("Префикс не принадлежит провайдеру маршрута")
        if not name:
            raise BusinessRuleError("Название маршрута обязательно")
        repo.conn.execute("UPDATE routes SET name = ?, provider_prefix_id = ?, comment = ?, is_actual = ?, priority_status = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (name, prefix_id, data.get("comment"), 1 if data.get("is_actual") == "1" else 0, data.get("priority_status") or "unknown", actor_id, route_id))
        repo.conn.execute("INSERT INTO route_history(route_id, action, changed_by, field_name, old_value, new_value, comment) VALUES (?, 'updated', ?, 'route', ?, ?, ?)", (route_id, actor_id, str(dict(old)) if old else None, str({"name": name, "provider_prefix_id": data.get("provider_prefix_id"), "comment": data.get("comment"), "is_actual": data.get("is_actual"), "priority_status": data.get("priority_status")}), data.get("comment")))
        repo.conn.commit()
        return "/routes"
    if path.startswith("/routes/") and path.endswith("/numbers/add"):
        route_id = int(path.strip("/").split("/")[1])
        repo.add_phone_to_route_by_number(route_id=route_id, number=data["phone_number"], usage_type=data.get("usage_type") or "pool_member", added_by=actor_id, comment=data.get("comment"))
        return f"/routes/{route_id}/numbers/manage"
    if path.startswith("/routes/") and path.endswith("/numbers/bulk-add"):
        route_id = int(path.strip("/").split("/")[1])
        added, errors = 0, []
        for number in [n.strip() for n in data.get("phone_numbers", "").replace(",", "\n").splitlines() if n.strip()]:
            try:
                repo.add_phone_to_route_by_number(route_id=route_id, number=number, usage_type="pool_member", added_by=actor_id)
                added += 1
            except (BusinessRuleError, sqlite3.IntegrityError) as exc:
                errors.append(f"{number}: {exc}")
        from urllib.parse import quote
        report = "Массовое добавление завершено. Добавлено %s из %s. Не добавлены: %s" % (added, added + len(errors), "; ".join(errors) or "—")
        notice_type = "error" if errors else "success"
        return f"/routes/{route_id}/numbers/manage?notice={quote(report)}&notice_type={notice_type}"
    if path.startswith("/routes/") and path.endswith("/numbers/remove"):
        route_id = int(path.strip("/").split("/")[1])
        link_ids = [int(v) for v in parse_qs(data.get("_raw", "")).get("link_ids", []) if v]
        removed = repo.remove_phone_links_from_route(route_id=route_id, link_ids=link_ids, removed_by=actor_id, reason=data.get("reason"))
        if removed == 0:
            raise BusinessRuleError("Выберите номера для исключения из маршрута")
        return f"/routes/{route_id}/numbers/manage"
    if path == "/phones/create":
        provider_id = parse_int(data.get("provider_id"))
        if provider_id is None:
            raise BusinessRuleError("Провайдер обязателен для создания номера")
        repo.create_phone_number(country_id=int(data["country_id"]), provider_id=provider_id, number=data["number"], assignment_type=data["assignment_type"], status=data["status"], created_by=actor_id, project_label=data.get("project_label") or None, connection_cost=data.get("connection_cost") or None, monthly_fee=data.get("monthly_fee") or None, currency_id=parse_int(data.get("currency_id")), phone_type=data.get("phone_type") or None, tariff_label=data.get("tariff_label") or None, comment=data.get("comment"))
        return "/phones"
    if path.startswith("/phones/") and path.endswith("/update"):
        phone_id = int(path.strip("/").split("/")[1])
        normalized = validate_phone_number(data["number"])
        is_active = 1 if data.get("is_active") == "1" else 0
        provider_id = parse_int(data.get("provider_id"))
        review_required = 1 if data.get("review_required") == "1" else 0
        if provider_id is None and review_required == 0:
            existing_phone = repo.conn.execute("SELECT deactivated_at FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
            if is_active == 1 and existing_phone and existing_phone["deactivated_at"] is not None:
                review_required = 1
            else:
                raise BusinessRuleError("Нельзя снять флаг проверки, пока не выбран провайдер")
        repo.update_phone_number(
            phone_id,
            country_id=int(data["country_id"]),
            provider_id=provider_id,
            number=normalized,
            assignment_type=data.get("assignment_type"),
            status=data.get("status"),
            is_active=is_active == 1,
            updated_by=actor_id,
            project_label=data.get("project_label") or None,
            connection_cost=data.get("connection_cost") or None,
            monthly_fee=data.get("monthly_fee") or None,
            currency_id=parse_int(data.get("currency_id")),
            phone_type=data.get("phone_type") or None,
            tariff_label=data.get("tariff_label") or None,
            comment=data.get("comment"),
            review_required=review_required == 1,
        )
        return "/phones"
    if path == "/tariffs/create":
        currency_id = int(data["currency_id"])
        rate = repo.latest_currency_rate(currency_id)
        if rate is None:
            raise BusinessRuleError("Для выбранной валюты нет курса к EUR. Добавьте курс в Администрирование → Курсы валют")
        prefix_id = parse_int(data.get("provider_prefix_id"))
        tariff_id = repo.create_tariff(country_id=int(data["country_id"]), provider_id=int(data["provider_id"]), provider_prefix_id=prefix_id, provider_currency_id=currency_id, price_in_provider_currency=data["price"], conversion_rate_to_eur=rate["rate_to_eur"], conversion_rate_date=rate["rate_date"], currency_rate_id=rate["id"], created_by=actor_id, priority_status=data["priority_status"], comment=data.get("comment"))
        if data.get("is_current") == "0":
            repo.conn.execute("UPDATE tariffs SET is_current = 0 WHERE id = ?", (tariff_id,)); repo.conn.commit()
        return "/tariffs"
    if path.startswith("/tariffs/") and path.endswith("/deactivate"):
        tariff_id = int(path.strip("/").split("/")[1])
        repo.conn.execute("UPDATE tariffs SET is_current = 0, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (actor_id, tariff_id)); repo.conn.commit(); return "/tariffs"
    if path == "/companies/create":
        repo.create_calling_company(server_id=int(data["server_id"]), country_id=int(data["country_id"]), company_name=data["company_name"], company_id_external=data["company_id_external"], has_autorotation=data.get("has_autorotation") == "1", created_by=actor_id, comment=data.get("comment"), is_active=data.get("is_active") == "1", line_count=int(data.get("line_count") or 0), dial_set_count=int(data.get("dial_set_count") or 0), retry_interval_seconds=int(data.get("retry_interval_seconds") or 0))
        return "/companies"
    if path.startswith("/companies/") and path.endswith("/update"):
        company_id = int(path.strip("/").split("/")[1])
        repo.conn.execute("""UPDATE calling_companies SET server_id = ?, country_id = ?, company_name = ?, line_count = ?, dial_set_count = ?, has_autorotation = ?, retry_interval_seconds = ?, is_active = ?, comment = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                          (int(data["server_id"]), int(data["country_id"]), data["company_name"], int(data.get("line_count") or 0), int(data.get("dial_set_count") or 0), 1 if data.get("has_autorotation") == "1" else 0, int(data.get("retry_interval_seconds") or 0), 1 if data.get("is_active") == "1" else 0, data.get("comment"), actor_id, company_id))
        repo._change_log("calling_company", company_id, "calling_company.updated", actor_id, new_values={"company_name": data["company_name"]})
        repo.conn.commit(); return "/companies"
    if path == "/provider-changes/create":
        apply_scope = data.get("apply_scope")
        provider_id = parse_int(data.get("campaign_provider_id")) if apply_scope == "campaign_setting" else parse_int(data.get("provider_id"))
        selected_server_ids = parse_qs(data.get("_raw", ""), keep_blank_values=True).get("server_ids") if apply_scope == "server_priority" else None
        repo.create_routing_event(
            event_at=data.get("event_at"), apply_scope=apply_scope, reason=data.get("reason"), comment=data.get("comment"),
            country_id=parse_int(data.get("country_id")), server_id=parse_int(data.get("server_id")), server_ids=selected_server_ids, provider_id=provider_id,
            affected_route_id=parse_int(data.get("affected_route_id")), old_route_id=parse_int(data.get("old_route_id")), new_route_id=parse_int(data.get("new_route_id")),
            calling_company_id=parse_int(data.get("calling_company_id")), company_change_type=data.get("company_change_type") or None,
            new_company_routing_mode=data.get("new_company_routing_mode") or None, new_company_route_id=parse_int(data.get("new_company_route_id")),
            new_company_has_autorotation=parse_int(data.get("new_company_has_autorotation")), created_by=actor_id,
        )
        return "/provider-changes"
    if path.startswith("/provider-changes/") and path.endswith("/update"):
        change_id = int(path.strip("/").split("/")[1])
        repo.update_routing_event(
            change_id, event_at=data.get("event_at"), reason=data.get("reason"), comment=data.get("comment"),
            country_id=parse_int(data.get("country_id")), server_id=parse_int(data.get("server_id")), provider_id=parse_int(data.get("campaign_provider_id")) if data.get("apply_scope") == "campaign_setting" else parse_int(data.get("provider_id")),
            affected_route_id=parse_int(data.get("affected_route_id")), old_route_id=parse_int(data.get("old_route_id")), new_route_id=parse_int(data.get("new_route_id")),
            calling_company_id=parse_int(data.get("calling_company_id")), company_change_type=data.get("company_change_type") or None,
            new_company_routing_mode=data.get("new_company_routing_mode") or None, new_company_route_id=parse_int(data.get("new_company_route_id")),
            new_company_has_autorotation=parse_int(data.get("new_company_has_autorotation")), updated_by=actor_id,
        )
        return "/provider-changes"
    if path.startswith("/provider-changes/") and path.endswith("/deactivate"):
        change_id = int(path.strip("/").split("/")[1])
        repo.deactivate_routing_event(change_id, reason=data.get("deactivation_reason"), deactivated_by=actor_id)
        return "/provider-changes"
    if path in {"/admin/currency-rates/create", "/admin/currency-rates/upsert"}:
        currency_id = int(data["currency_id"])
        today = datetime.now().strftime("%Y-%m-%d")
        existing = repo.conn.execute("SELECT id FROM currency_rates WHERE currency_id = ? ORDER BY rate_date DESC, created_at DESC, id DESC LIMIT 1", (currency_id,)).fetchone()
        if existing:
            repo.conn.execute("UPDATE currency_rates SET rate_to_eur = ?, rate_date = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (data["rate_to_eur"], today, actor_id, existing["id"]))
        else:
            repo.conn.execute("INSERT INTO currency_rates(currency_id, rate_to_eur, rate_date, updated_by, source) VALUES (?, ?, ?, ?, 'manual')", (currency_id, data["rate_to_eur"], today, actor_id))
        repo.conn.commit(); return "/admin/currency-rates"
    if path == "/admin/change-reasons/create":
        repo.create_change_reason(data["name"], created_by=actor_id, comment=data.get("comment"), is_active=data.get("is_active") == "1"); return "/admin/change-reasons"
    if path.startswith("/admin/change-reasons/") and path.endswith("/update"):
        reason_id = int(path.strip("/").split("/")[2])
        repo.conn.execute("UPDATE change_reasons SET name = ?, description = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (data["name"].strip(), data.get("comment"), 1 if data.get("is_active") == "1" else 0, reason_id))
        repo._change_log("change_reason", reason_id, "change_reason.updated", actor_id, new_values={"name": data["name"].strip(), "is_active": data.get("is_active")})
        repo.conn.commit(); return "/admin/change-reasons"
    if path == "/tariffs/countries/create":
        repo.create_country(data["name"].strip()); return "/tariffs"
    if path == "/tariffs/providers/create":
        repo.create_provider(data["name"].strip()); return "/tariffs"
    if path == "/tariffs/currencies/create":
        code = data["code"].strip().upper()
        repo.create_currency(code, code); return "/tariffs"
    if path == "/tariffs/prefixes/create":
        prefix = data.get("prefix") or None
        if prefix is not None and not prefix.isdigit():
            raise BusinessRuleError("Префикс должен быть пустым (Без префикса) или состоять только из цифр")
        repo.create_prefix(int(data["provider_id"]), prefix); return "/tariffs"
    if path == "/admin/providers/create":
        repo.create_provider(data["name"], default_currency_id=parse_int(data.get("currency_id"))); return "/admin/dictionaries"
    if path == "/admin/countries/create":
        repo.create_country(data["name"], data.get("code") or None); return "/admin/dictionaries"
    if path == "/admin/prefixes/create":
        prefix = data.get("prefix") or None
        if prefix is not None and not prefix.isdigit():
            raise BusinessRuleError("Префикс должен быть пустым (Без префикса) или состоять только из цифр")
        repo.create_prefix(int(data["provider_id"]), prefix, data.get("name") or None); return "/admin/dictionaries"
    if path.startswith("/admin/dictionaries/") and path.endswith("/create"):
        parts = path.strip("/").split("/")
        kind = parts[2]
        if kind == "countries":
            repo.create_country(data["name"].strip(), data.get("code") or None)
        elif kind == "providers":
            repo.create_provider(data["name"].strip(), default_currency_id=parse_int(data.get("default_currency_id")), comment=data.get("comment") or None)
        elif kind == "currencies":
            code = data["code"].strip().upper()
            repo.create_currency(code, data.get("name") or code)
        elif kind == "prefixes":
            prefix = data.get("prefix") or None
            if prefix is not None and not prefix.isdigit():
                raise BusinessRuleError("Префикс должен быть пустым (Без префикса) или состоять только из цифр")
            repo.create_prefix(int(data["provider_id"]), prefix, data.get("name") or None)
        elif kind == "servers":
            repo.create_server(data["name"].strip(), data.get("comment") or None)
        elif kind == "phone-types":
            repo.conn.execute("INSERT INTO phone_number_types(name, is_active, comment) VALUES (?, 1, ?)", (data["name"].strip(), data.get("comment")))
            repo.conn.commit()
        elif kind == "projects":
            repo.conn.execute("INSERT INTO projects(name, is_active, comment) VALUES (?, 1, ?)", (data["name"].strip(), data.get("comment")))
            repo.conn.commit()
        elif kind == "phone-assignments":
            name = data["name"].strip()
            code = (data.get("code") or name).strip()
            repo.conn.execute("INSERT INTO phone_assignment_types(code, name, is_active, comment) VALUES (?, ?, 1, ?)", (code, name, data.get("comment")))
            repo.conn.commit()
        else:
            raise BusinessRuleError("Неизвестный справочник")
        return "/admin/dictionaries"
    if path.startswith("/admin/dictionaries/") and path.endswith("/update"):
        parts = path.strip("/").split("/")
        kind = parts[2]
        entity_id = int(parts[3])
        is_active = 1 if data.get("is_active") == "1" else 0
        if kind == "countries":
            repo.conn.execute("UPDATE countries SET name = ?, code = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (data["name"].strip(), data.get("code") or None, is_active, entity_id))
        elif kind == "providers":
            repo.conn.execute("UPDATE providers SET name = ?, normalized_name = ?, default_currency_id = ?, comment = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (data["name"].strip(), normalize_provider_name(data["name"]), parse_int(data.get("default_currency_id")), data.get("comment") or None, is_active, entity_id))
        elif kind == "currencies":
            code = data["code"].strip().upper()
            repo.conn.execute("UPDATE currencies SET code = ?, name = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (code, data.get("name") or code, is_active, entity_id))
        elif kind == "prefixes":
            prefix = data.get("prefix") or None
            if prefix is not None and not prefix.isdigit():
                raise BusinessRuleError("Префикс должен быть пустым (Без префикса) или состоять только из цифр")
            repo.conn.execute("UPDATE provider_prefixes SET provider_id = ?, prefix = ?, name = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (int(data["provider_id"]), prefix, data.get("name") or None, is_active, entity_id))
        elif kind == "servers":
            repo.conn.execute("UPDATE servers SET name = ?, comment = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (data["name"].strip(), data.get("comment") or None, is_active, entity_id))
        elif kind == "phone-types":
            old = repo.conn.execute("SELECT name FROM phone_number_types WHERE id = ?", (entity_id,)).fetchone()
            new_name = data["name"].strip()
            repo.conn.execute("UPDATE phone_number_types SET name = ?, comment = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_name, data.get("comment") or None, is_active, entity_id))
            if old and old["name"] != new_name:
                repo.conn.execute("UPDATE phone_numbers SET phone_type = ? WHERE phone_type = ?", (new_name, old["name"]))
        elif kind == "projects":
            old = repo.conn.execute("SELECT name FROM projects WHERE id = ?", (entity_id,)).fetchone()
            new_name = data["name"].strip()
            repo.conn.execute("UPDATE projects SET name = ?, comment = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_name, data.get("comment") or None, is_active, entity_id))
            if old and old["name"] != new_name:
                repo.conn.execute("UPDATE phone_numbers SET project_label = ? WHERE project_label = ?", (new_name, old["name"]))
        elif kind == "phone-assignments":
            repo.conn.execute("UPDATE phone_assignment_types SET name = ?, comment = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (data["name"].strip(), data.get("comment") or None, is_active, entity_id))
        else:
            raise BusinessRuleError("Неизвестный справочник")
        repo._change_log(kind, entity_id, "dictionary.updated", actor_id, new_values={"is_active": is_active})
        repo.conn.commit()
        return "/admin/dictionaries"
    if path.startswith("/admin/server-priorities/") and path.endswith("/update"):
        priority_id = int(path.strip("/").split("/")[2])
        repo.update_server_route_priority(
            priority_id=priority_id,
            current_route_id=int(data["current_route_id"]),
            comment=data.get("comment"),
            changed_by=actor_id,
        )
        return "/admin/server-priorities"
    if path.startswith("/admin/server-priorities/") and path.endswith("/comment"):
        priority_id = int(path.strip("/").split("/")[2])
        current = repo.conn.execute("SELECT current_route_id FROM server_route_priorities WHERE id = ?", (priority_id,)).fetchone()
        if not current:
            raise BusinessRuleError("Приоритет по серверу не найден")
        repo.update_server_route_priority(
            priority_id=priority_id,
            current_route_id=int(current["current_route_id"]),
            comment=data.get("comment"),
            changed_by=actor_id,
        )
        return "/admin/server-priorities"
    if path == "/admin/company-routing-settings/create":
        setting_id = repo.create_company_routing_setting(
            calling_company_id=int(data["calling_company_id"]),
            country_id=int(data["country_id"]),
            server_id=int(data["server_id"]),
            route_id=parse_int(data.get("route_id")),
            routing_mode=data["routing_mode"],
            has_autorotation=data.get("has_autorotation") == "1",
            comment=data.get("comment"),
            created_by=actor_id,
        )
        if data.get("is_active") != "1":
            repo.deactivate_company_routing_setting(setting_id=setting_id, updated_by=actor_id)
        return "/admin/company-routing-settings"
    if path.startswith("/admin/company-routing-settings/") and path.endswith("/update"):
        setting_id = int(path.strip("/").split("/")[2])
        if data.get("is_active") != "1":
            repo.deactivate_company_routing_setting(setting_id=setting_id, updated_by=actor_id)
        else:
            repo.update_company_routing_setting(
                setting_id=setting_id,
                country_id=int(data["country_id"]),
                server_id=int(data["server_id"]),
                route_id=parse_int(data.get("route_id")),
                routing_mode=data["routing_mode"],
                has_autorotation=data.get("has_autorotation") == "1",
                comment=data.get("comment"),
                updated_by=actor_id,
            )
        return "/admin/company-routing-settings"
    if path.startswith("/admin/company-routing-settings/") and path.endswith("/deactivate"):
        setting_id = int(path.strip("/").split("/")[2])
        repo.deactivate_company_routing_setting(setting_id=setting_id, updated_by=actor_id)
        return "/admin/company-routing-settings"
    if path == "/admin/telegram/save":
        repo.conn.execute("INSERT INTO telegram_settings(is_enabled, chat_id, bot_token_secret_ref, message_template, updated_by) VALUES (?, ?, ?, ?, ?)", (1 if data.get("is_enabled") == "1" else 0, data.get("chat_id"), data.get("bot_token_secret_ref"), data.get("message_template"), actor_id)); repo.conn.commit(); return "/admin/telegram"
    if path == "/admin/telegram/test":
        repo.conn.execute("INSERT INTO telegram_settings(is_enabled, chat_id, bot_token_secret_ref, message_template, last_test_status, last_test_at, last_test_by, updated_by) VALUES (?, ?, ?, ?, 'success', CURRENT_TIMESTAMP, ?, ?)", (1 if data.get("is_enabled") == "1" else 0, data.get("chat_id"), data.get("bot_token_secret_ref"), data.get("message_template"), actor_id, actor_id)); repo.conn.execute("INSERT INTO change_log(entity_type, change_type, changed_by, summary, source) VALUES ('telegram', 'telegram.test_message_sent', ?, 'Test Telegram message requested', 'ui')", (actor_id,)); repo.conn.commit(); return "/admin/telegram"
    if path == "/admin/naming-rules/create":
        if data.get("is_active") == "1": repo.conn.execute("UPDATE route_naming_rules SET is_active = 0")
        repo.conn.execute("INSERT INTO route_naming_rules(name, template, is_active, comment, created_by) VALUES (?, ?, ?, ?, ?)", (data["name"], data["template"], 1 if data.get("is_active") == "1" else 0, data.get("comment"), actor_id)); repo.conn.commit(); return "/admin/naming-rules"
    raise BusinessRuleError("Unsupported form action")



def error_return_path(path: str) -> str:
    if path.startswith("/routes/") and "/numbers/" in path:
        return "/routes/" + path.strip("/").split("/")[1] + "/numbers/manage"
    if path.startswith("/routes/") and path.endswith("/update"):
        return "/routes/" + path.strip("/").split("/")[1] + "/edit"
    if path == "/routes/create":
        return "/routes"
    if path.startswith("/phones/") and path.endswith("/update"):
        return "/phones/" + path.strip("/").split("/")[1] + "/edit"
    if path == "/phones/create":
        return "/phones"
    if path.startswith("/companies/") and path.endswith("/update"):
        return "/companies/" + path.strip("/").split("/")[1] + "/edit"
    if path == "/companies/create":
        return "/companies"
    if path.startswith("/provider-changes/") and path.endswith("/update"):
        return "/provider-changes/" + path.strip("/").split("/")[1] + "/edit"
    if path.startswith("/provider-changes/") or path == "/provider-changes/create":
        return "/provider-changes"
    if path.startswith("/tariffs/") or path == "/tariffs/create":
        return "/tariffs"
    if path.startswith("/admin/server-priorities/"):
        return "/admin/server-priorities"
    if path.startswith("/admin/users/") or path == "/admin/users/create":
        return "/admin/users"
    if path.startswith("/admin/dictionaries/"):
        return "/admin/dictionaries"
    if path.startswith("/admin/company-routing-settings/") or path == "/admin/company-routing-settings/create":
        return "/admin/company-routing-settings"
    if path.startswith("/admin/"):
        parts = path.strip("/").split("/")
        return "/" + "/".join(parts[:2]) if len(parts) >= 2 else "/admin"
    return "/routes"


def validation_error_page(return_path: str, message: str) -> bytes:
    titles = {
        "/routes": "Маршруты",
        "/tariffs": "Тарифы",
        "/phones": "Купленные номера",
        "/companies": "Кампании прозвона",
        "/provider-changes": "Смена провайдеров",
        "/admin/server-priorities": "Приоритет по серверам",
        "/admin/users": "Пользователи",
        "/admin/dictionaries": "Справочные значения",
        "/admin/company-routing-settings": "Схема маршрутизации кампаний",
    }
    title = titles.get(return_path, "Ошибка")
    body = f"<div class='error'>{esc(message)}</div><h1>{esc(title)}</h1><p><a class='button' href='{esc(return_path)}'>Вернуться и исправить</a></p>"
    return page(title, body)

def user_error(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, sqlite3.IntegrityError):
        if "routes.country_id, routes.name" in text or "UNIQUE constraint failed: routes.country_id, routes.name" in text:
            return "Маршрут уже существует"
        if "phone_numbers.normalized_number" in text:
            return "Номер уже существует"
        if "calling_companies" in text:
            return "Кампания с таким ID уже существует"
        if "tariffs" in text:
            return "Активный тариф с такой связкой ГЕО + провайдер + префикс уже существует"
        if "providers.normalized_name" in text:
            return "Провайдер уже существует"
        if "countries.name" in text:
            return "ГЕО уже существует"
        return "Нарушено ограничение уникальности или обязательности данных"
    return text


def app(environ, start_response):
    conn = connect(DB_PATH)
    init_db(conn)
    repo = Repository(conn)
    ensure_seed(repo)
    method = environ["REQUEST_METHOD"]
    path = environ.get("PATH_INFO", "/")
    q = request_query(environ)
    current_user_id = resolve_current_user_id(repo, cookie_user_id(environ))
    current_user = repo.get_user(current_user_id)
    _REQUEST_CONTEXT.clear()
    _REQUEST_CONTEXT.update({
        "repo": repo,
        "current_user_id": current_user_id,
        "current_role_key": normalize_role(current_user["role_key"] if current_user else None),
        "redirect_to": current_request_path(environ),
    })
    try:
        if method == "POST":
            raw_size = int(environ.get("CONTENT_LENGTH") or "0")
            raw_body = environ["wsgi.input"].read(raw_size).decode("utf-8")
            parsed = {key: values[-1] for key, values in parse_qs(raw_body, keep_blank_values=True).items()}
            parsed["_raw"] = raw_body
            if path != "/users/select":
                require_permission("write", section_for_write_path(path))
            if path == "/admin/import/preview":
                if parsed["entity_type"] == "tariffs" and parsed.get("mode") == "replace_section":
                    raise BusinessRuleError("Для тарифов доступен только режим Дополнить / обновить")
                preview = preview_import(conn, parsed["entity_type"], parsed.get("csv_data", ""))
                rows = "".join(f"<tr><td>{r['line']}</td><td>{esc(r['status'])}</td><td>{esc(r['action'])}</td><td>{esc(r['message'])}</td></tr>" for r in preview.rows)
                html_preview = f"<h2>Предпросмотр</h2><p>Всего: {preview.total_rows}, новых: {preview.new_rows}, дублей: {preview.duplicate_rows}, ошибок: {preview.error_rows}</p><table><tr><th>Строка</th><th>Статус</th><th>Действие</th><th>Комментарий</th></tr>{rows}</table>"
                start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
                return [import_page(repo, html_preview, selected_entity=parsed["entity_type"], selected_mode=parsed.get("mode", "append_update"), csv_data=parsed.get("csv_data", ""))]
            if path == "/admin/import/apply":
                result = apply_import(conn, parsed["entity_type"], parsed.get("csv_data", ""), user_id=current_actor_id(), mode=parsed.get("mode", "append_update"))
                notice = f"<h2>Импорт завершён</h2><ul><li>создано {result.created_rows}</li><li>обновлено {result.updated_rows}</li><li>пропущено {result.skipped_rows}</li><li>ошибок {result.error_rows}</li></ul>"
                start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
                return [import_page(repo, notice, selected_entity=parsed["entity_type"], selected_mode=parsed.get("mode", "append_update"), csv_data=parsed.get("csv_data", ""))]
            if path == "/users/select":
                selected_user_id = resolve_current_user_id(repo, parse_int(parsed.get("user_id")))
                location = safe_redirect_target(parsed.get("redirect_to"))
                return redirect(
                    start_response,
                    location,
                    [("Set-Cookie", f"{CURRENT_USER_COOKIE}={selected_user_id}; Path=/; SameSite=Lax")],
                )
            location = handle_post(repo, path, parsed)
            return redirect(start_response, location)
        require_permission("read", section_for_get_path(path))
        if path.startswith(("/routes/", "/phones/", "/companies/")) and path.endswith("/edit"):
            require_permission("write", section_for_write_path(path.replace("/edit", "/update")))
        if path.startswith("/provider-changes/") and path.endswith("/edit"):
            require_permission("write", "provider_changes")
        if path in {"/", "/dashboard"}: response = dashboard_page(repo)
        elif path == "/routes": response = routes_page(repo, q)
        elif path == "/tariffs": response = tariffs_page(repo, q)
        elif path == "/phones": response = phones_page(repo, q)
        elif path == "/companies": response = companies_page(repo, q)
        elif path == "/provider-changes": response = provider_changes_page(repo, q)
        elif path == "/admin": response = admin_page(repo)
        elif path == "/admin/server-priorities": response = server_priorities_page(repo, q)
        elif path == "/admin/company-routing-settings": response = company_routing_settings_page(repo, q)
        elif path == "/admin/naming-rules": response = naming_rules_page(repo)
        elif path == "/admin/import": response = import_page(repo)
        elif path == "/admin/currency-rates": response = currency_rates_page(repo)
        elif path == "/admin/change-reasons": response = change_reasons_page(repo)
        elif path == "/admin/users": response = users_page(repo)
        elif path == "/admin/dictionaries": response = dictionaries_page(repo, q)
        elif path == "/admin/telegram": response = telegram_page(repo)
        elif path == "/admin/change-log": response = change_log_page(repo)
        elif path.startswith("/routes/") and path.endswith("/edit"): response = route_edit_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/phones/") and path.endswith("/edit"): response = phone_edit_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/companies/") and path.endswith("/edit"): response = company_edit_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/provider-changes/") and path.endswith("/edit"): response = provider_change_edit_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/routes/") and path.endswith("/numbers/manage"):
            require_permission("write", "routes")
            response = route_numbers_manage_page(repo, int(path.strip("/").split("/")[1]), q)
        elif path.startswith("/routes/") and path.endswith("/numbers"): response = route_numbers_page(repo, int(path.strip("/").split("/")[1]), q)
        else:
            start_response("404 Not Found", [("Content-Type", "text/html; charset=utf-8")]); return [page("404", "<h1>404</h1>")]
        if q.get("export") == "csv" and path in EXPORT_FILENAMES:
            start_response("200 OK", [("Content-Type", "text/csv; charset=utf-8"), ("Content-Disposition", f"attachment; filename={EXPORT_FILENAMES[path]}")])
        else:
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [response]
    except ForbiddenError:
        start_response("403 Forbidden", [("Content-Type", "text/html; charset=utf-8")])
        return [forbidden_page()]
    except (BusinessRuleError, ValueError, sqlite3.IntegrityError) as exc:
        start_response("400 Bad Request", [("Content-Type", "text/html; charset=utf-8")])
        return_path = error_return_path(path)
        if return_path.startswith("/routes/") and return_path.endswith("/numbers/manage"):
            route_id = int(return_path.strip("/").split("/")[1])
            return [route_numbers_manage_page(repo, route_id, {"notice": user_error(exc), "notice_type": "error"})]
        return [validation_error_page(return_path, user_error(exc))]
    finally:
        _REQUEST_CONTEXT.clear()
        conn.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    with make_server("0.0.0.0", port, app) as httpd:
        print(f"Serving on http://127.0.0.1:{port}")
        httpd.serve_forever()
