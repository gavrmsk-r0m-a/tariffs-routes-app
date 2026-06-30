from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode
from wsgiref.simple_server import make_server

from app.db import DEFAULT_DB_PATH, DEFAULT_PHONE_ASSIGNMENTS, DEFAULT_PROJECTS, connect, ensure_db_initialized, init_db
from app.importer import apply_import, preview_import
from app.repository import BusinessRuleError, COMPANY_CHANGE_LABELS, ROUTING_SCOPE_LABELS, Repository, normalize_phone_status, normalize_provider_name, normalize_real_prefix, validate_phone_number
from app.telegram import notify_provider_change_created

logger = logging.getLogger(__name__)


def load_dotenv_if_present(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE pairs from a local .env file without overriding env."""
    env_path = Path(path)
    if not env_path.exists():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value
    except OSError as exc:
        logger.warning("Could not load .env file %s: %s", env_path, exc)


load_dotenv_if_present()

DB_PATH = Path(os.environ.get("MVP_DB_PATH", DEFAULT_DB_PATH))
ADMIN_ID = 1
CURRENT_USER_COOKIE = "mvp_auth"
FILTER_STATE_COOKIE = "mvp_filter_state"
AUTH_COOKIE_SECRET = os.environ.get("SECRET_KEY") or os.environ.get("MVP_AUTH_SECRET") or "dev-mvp-auth-secret-change-me"

def sign_user_id(user_id: int) -> str:
    value = str(user_id)
    sig = hmac.new(AUTH_COOKIE_SECRET.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"

def auth_cookie_header(user_id: int) -> tuple[str, str]:
    return ("Set-Cookie", f"{CURRENT_USER_COOKIE}={sign_user_id(user_id)}; Path=/; HttpOnly; SameSite=Lax")
FILTER_SECTIONS = {
    "/routes": ("routes", ("country_id", "provider_id", "prefix_id", "is_actual", "search")),
    "/tariffs": ("tariffs", ("country_id", "provider_id", "priority_status", "status")),
    "/phones": ("phones", ("country_id", "provider_id", "project", "assignment_type", "status", "number", "review_required")),
    "/companies": ("companies", ("server_id", "country_id", "company", "external_id", "has_autorotation", "is_active")),
    "/provider-changes": ("provider_changes", ("date_from", "date_to", "country_id", "apply_scope", "server_id", "campaign_id", "provider_id", "include_inactive")),
    "/admin/server-priorities": ("admin_server_priorities", ("country_id", "server_id")),
    "/admin/company-routing-settings": ("admin_company_routing_settings", ("country_id", "server_id", "company_id_external", "routing_mode", "is_active", "show_history")),
}
FILTER_OPEN_KEY = "_filters_open"
FILTER_CONTROL_KEYS = {"page", "limit", "export", "reset_filters", "_filters_restored", FILTER_OPEN_KEY}
FILTER_DEFAULT_VALUES = {
    "tariffs": {"status": "active"},
}

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
    section = section_for_get_path(base_path)
    if section and not can_export(section):
        return ""
    params = {key: value for key, value in q.items() if key not in {"page", "limit", "export"} and value not in (None, "")}
    params["export"] = "csv"
    return f"<span class='action-icon export-action-icon' aria-hidden='true'>{nav_icon('export')}</span><a class='button export-button table-utility-button' href='{esc(base_path + '?' + urlencode(params))}'>Экспорт</a>"


def copy_column_button(column: str) -> str:
    return f"<button class='copy-column-button' type='button' data-copy-action='{esc(column)}' title='Скопировать колонку' aria-label='Скопировать колонку'>{nav_icon('copy')}</button>"


COPY_SUCCESS_ICON = "<span class='material-symbols-rounded' aria-hidden='true'>check_circle</span>"
COPY_SUCCESS_ICON_JS = json.dumps(COPY_SUCCESS_ICON)


def csv_response(filename: str, headers: list[str], rows: list[list[object]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(headers)
    writer.writerows([["" if value is None else value for value in row] for row in rows])
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def html_to_csv_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"</li>\s*<li[^>]*>", "; ", text)
    text = re.sub(r"<br\s*/?>", "; ", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:ul|ol)[^>]*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"</?li[^>]*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\s*;\s*", "; ", text)
    return text.strip(" ;")

ASSIGNMENT_LABELS = {code: name for code, name, _sort_order in DEFAULT_PHONE_ASSIGNMENTS}


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def plain_text(value: object) -> str:
    text = re.sub(r"<[^>]+>", " ", "" if value is None else str(value))
    return re.sub(r"\s+", " ", text).strip()


def plain_title(value: object) -> str:
    text = plain_text(value)
    for icon_name in ("content_copy", "download", "view_column", "info", "edit"):
        text = text.replace(f" {icon_name}", "").replace(icon_name, "")
    return esc(re.sub(r"\s+", " ", text).strip())


def selectable_text(content_html: str, value: object, *, classes: str = "compound-value-cell") -> str:
    text = plain_text(value)
    class_attr = f" {classes}" if classes else ""
    return (
        f"<span class='selectable-text{class_attr}' data-select-value='{esc(text)}' "
        "title='Повторный двойной клик — выделить всё значение'>"
        f"{content_html}</span>"
    )


def clamp_cell(col: str, content_html: str, title: object, *, extra_attrs: str = "", classes: str = "", selectable: bool = False, select_value: object | None = None) -> str:
    cell_classes = classes.split() if classes else []
    if selectable and "selectable-cell" not in cell_classes:
        cell_classes.append("selectable-cell")
    class_attr = f" class='{' '.join(cell_classes)}'" if cell_classes else ""
    attrs = f" {extra_attrs.strip()}" if extra_attrs.strip() else ""
    title_text = plain_text(title)
    title_attr = f" title='{esc(title_text)}'" if len(title_text) > 60 else ""
    full_text_attr = f" data-full-text='{esc(title_text)}'" if len(title_text) > 60 else ""
    inner_html = selectable_text(content_html, title_text if select_value is None else select_value) if selectable else content_html
    return f"<td data-col='{esc(col)}'{class_attr}{attrs}{title_attr}{full_text_attr}><span class='cell-clamp'>{inner_html}</span></td>"


SECTION_ALIASES = {"phones": "phone_numbers", "companies": "call_campaigns"}

SECTION_REGISTRY = [
    {"section_key": "dashboard", "display_name": "Главная", "supports_export": False},
    {"section_key": "routes", "display_name": "Маршруты", "supports_export": True},
    {"section_key": "tariffs", "display_name": "Тарифы", "supports_export": True},
    {"section_key": "phone_numbers", "display_name": "Купленные номера", "supports_export": True},
    {"section_key": "call_campaigns", "display_name": "Кампании прозвона", "supports_export": False},
    {"section_key": "provider_changes", "display_name": "Смена провайдеров", "supports_export": True},
    {"section_key": "admin", "display_name": "Администрирование", "supports_export": False},
    {"section_key": "change_log", "display_name": "Change log", "supports_export": False},
]
SECTION_BY_KEY = {section["section_key"]: section for section in SECTION_REGISTRY}

def normalize_section_key(section: str) -> str:
    return SECTION_ALIASES.get(section, section)

ROLE_PERMISSIONS = {
    "admin": {"read": {"*"}, "write": {"*"}, "export": {"*"}},
    "operator": {
        "read": {"dashboard", "routes", "tariffs", "phone_numbers", "call_campaigns", "provider_changes"},
        "write": {"phone_numbers", "call_campaigns", "provider_changes"},
        "export": {"phone_numbers", "provider_changes"},
    },
    "duty": {
        "read": {"dashboard", "routes", "tariffs", "phone_numbers", "call_campaigns", "provider_changes"},
        "write": {"phone_numbers", "call_campaigns", "provider_changes"},
        "export": {"phone_numbers", "provider_changes"},
    },
    "boss": {"read": {"dashboard", "tariffs"}, "write": set(), "export": {"tariffs"}},
    "guest": {"read": {"dashboard"}, "write": set(), "export": set()},
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
    if role_key == "operator":
        return "duty"
    return role_key if role_key in ROLE_PERMISSIONS else "guest"


def current_role_key() -> str:
    return normalize_role(_REQUEST_CONTEXT.get("current_role_key") if isinstance(_REQUEST_CONTEXT.get("current_role_key"), str) else None)


def role_allows(role_key: str | None, action: str, section: str) -> bool:
    section = normalize_section_key(section)
    allowed = ROLE_PERMISSIONS[normalize_role(role_key)][action]
    return "*" in allowed or section in allowed


def explicit_user_permissions(user_id: int, section: str) -> sqlite3.Row | None:
    repo = _REQUEST_CONTEXT.get("repo")
    if not user_id or not isinstance(repo, Repository):
        return None
    return repo.conn.execute(
        "SELECT can_read, can_write, can_export FROM user_permissions WHERE user_id = ? AND section_key = ?",
        (user_id, normalize_section_key(section)),
    ).fetchone()


def has_permission(user, section_key: str, action: str) -> bool:
    section = normalize_section_key(section_key)
    role_key = user["role_key"] if user is not None else current_role_key()
    if normalize_role(role_key) == "admin":
        return True
    user_id = int(user["id"]) if user is not None else int(_REQUEST_CONTEXT.get("current_user_id") or 0)
    row = explicit_user_permissions(user_id, section)
    if row is not None:
        column = {"read": "can_read", "write": "can_write", "export": "can_export"}[action]
        return bool(row[column])
    return role_allows(role_key, action, section)


def can_read(section: str) -> bool:
    return has_permission(None, section, "read")


def can_write(section: str) -> bool:
    return has_permission(None, section, "write")


def can_export(section: str) -> bool:
    return has_permission(None, section, "export")


def require_read(section_key: str):
    return lambda: require_permission("read", section_key)

def require_write(section_key: str):
    return lambda: require_permission("write", section_key)

def require_export(section_key: str):
    return lambda: require_permission("export", section_key)


def forbidden_page() -> bytes:
    return page("Нет доступа", "<section class='message-card error'><h1>Нет доступа</h1><p>У текущего пользователя нет прав для этого раздела или действия.</p></section>")


class ForbiddenError(Exception):
    pass


def material_icon(name: str) -> str:
    return f"<span class='material-symbols-rounded' aria-hidden='true'>{esc(name)}</span>"


SEMANTIC_ICONS = {
    "dashboard": material_icon("dashboard"),
    "routes": material_icon("route"),
    "tariffs": material_icon("sell"),
    "phones": material_icon("sim_card"),
    "companies": material_icon("campaign"),
    "provider_changes": material_icon("sync_alt"),
    "admin_hlr": material_icon("fact_check"),
    "admin_spam_checker": material_icon("report"),
    "admin": material_icon("admin_panel_settings"),
    "admin_server_priorities": material_icon("flag"),
    "admin_company_routing_settings": material_icon("account_tree"),
    "admin_users": material_icon("group"),
    "admin_dictionaries": material_icon("database"),
    "admin_route_naming": material_icon("rule"),
    "admin_import_export": material_icon("import_export"),
    "admin_currency_rates": material_icon("currency_exchange"),
    "admin_provider_reasons": material_icon("manage_history"),
    "admin_change_log": material_icon("history"),
    "admin_settings": material_icon("settings"),
    "admin_telegram": material_icon("send"),
    "info": material_icon("info"),
    "history": material_icon("info"),
    "edit": material_icon("edit"),
    "delete": material_icon("delete"),
    "export": material_icon("download"),
    "download": material_icon("download"),
    "columns": material_icon("view_column"),
    "copy": material_icon("content_copy"),
    "add": material_icon("add"),
    "save": material_icon("save"),
    "cancel": material_icon("close"),
    "show": material_icon("visibility"),
    "hide": material_icon("visibility_off"),
    "warning": material_icon("warning"),
    "success": material_icon("check_circle"),
    "inactive": material_icon("block"),
    "error": material_icon("error"),
    "user": material_icon("account_circle"),
    "theme": material_icon("contrast"),
    "collapse": material_icon("chevron_left"),
    "check": material_icon("check"),
}


NAV_ICONS = SEMANTIC_ICONS


def nav_icon(key: str) -> str:
    return SEMANTIC_ICONS.get(key, "")


def nav_icon_span(key: str) -> str:
    icon = nav_icon(key)
    return f"<span class='nav-icon' aria-hidden='true'>{icon}</span>" if icon else ""


def user_icon_svg() -> str:
    return nav_icon("user")

NAV_ITEMS = [
    ("dashboard", "/dashboard", "Главная", ("Главная",)),
    ("routes", "/routes", "Маршруты", ("Маршруты", "Номера маршрута", "Редактировать маршрут")),
    ("tariffs", "/tariffs", "Тарифы", ("Тарифы",)),
    ("phones", "/phones", "Купленные номера", ("Купленные номера", "Редактировать номер")),
    ("companies", "/companies", "Кампании прозвона", ("Кампании прозвона", "Редактировать кампанию")),
    ("provider_changes", "/provider-changes", "Смена провайдеров", ("Смена провайдеров", "Редактировать событие")),
    ("admin_server_priorities", "/admin/server-priorities", "Приоритет по серверам", ("Приоритет по серверам",)),
    ("admin_company_routing_settings", "/admin/company-routing-settings", "Схема маршрутизации кампаний", ("Схема маршрутизации кампаний",)),
]

ADMIN_NAV_ITEMS = [
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

    def disabled_nav_item(key: str, label: str) -> str:
        return (
            f"<button class='side-link side-link-disabled has-inline-icon' type='button' disabled "
            f"data-tooltip='Скоро' title='Скоро' aria-disabled='true'>"
            f"{nav_icon_span(key)}<span class='side-label'>{esc(label)}</span></button>"
        )

    main_links = "".join(nav_link(key, href, label) for key, href, label, _ in NAV_ITEMS if can_read(key))
    if current_role_key() == "admin":
        main_links += disabled_nav_item("admin_hlr", "HLR")
        main_links += disabled_nav_item("admin_spam_checker", "Spam Checker")
    admin_links = "".join(
        f"<a class='admin-link {'active' if active_admin_href == href else ''}' href='{href}'>{nav_icon_span(key)}<span>{esc(label)}</span></a>"
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
      <button class="sidebar-collapse" type="button" data-sidebar-toggle data-tooltip="Свернуть" aria-label="Свернуть боковую панель" title="Свернуть боковую панель"><span class="sidebar-collapse-icon" aria-hidden="true">{nav_icon("collapse")}</span></button>
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
        "admin": "Админ",
        "operator": "Дежурный",
        "guest": "Гость",
        "user": "Пользователь",
    }.get(role_key or "", role_key or "—")


def current_user_selector() -> str:
    repo = _REQUEST_CONTEXT.get("repo")
    current_user_id = _REQUEST_CONTEXT.get("current_user_id")
    if not isinstance(repo, Repository) or current_user_id is None:
        return ""
    current = repo.get_user(int(current_user_id))
    if not current or not current["is_active"]:
        return ""
    current_label = f"{current['display_name']} · {role_label(current['role_key'])}"
    return f"""
        <div class="current-user-selector" data-tooltip="{esc(current_label)}">
          <span class="side-icon user-icon" aria-hidden="true">{user_icon_svg()}</span>
          <span class="user-copy"><strong>{esc(current_label)}</strong><small>Текущий пользователь</small></span>
          <a class="logout-link" href="/logout">Выйти</a>
        </div>
    """

def theme_selector() -> str:
    return f"""
        <div class="theme-selector-wrap" data-theme-selector data-tooltip="Тема: MVP">
          <button class="theme-selector" type="button" data-theme-menu-toggle aria-haspopup="menu" aria-expanded="false"><span class="side-icon" aria-hidden="true">{nav_icon("theme")}</span><span class="side-label" data-theme-current>Тема: MVP ▾</span></button>
          <div class="theme-menu" data-theme-menu role="menu" aria-label="Выбор темы">
            <button type="button" role="menuitemradio" data-theme-option="mvp" aria-checked="true"><span class="theme-check" aria-hidden="true">{nav_icon("check")}</span><span>MVP</span></button>
            <button type="button" role="menuitemradio" data-theme-option="dark" aria-checked="false"><span class="theme-check" aria-hidden="true">{nav_icon("check")}</span><span>Тёмная</span></button>
            <button type="button" role="menuitemradio" data-theme-option="light-v2" aria-checked="false"><span class="theme-check" aria-hidden="true">{nav_icon("check")}</span><span>Светлая 2.0</span></button>
          </div>
        </div>
    """

def breadcrumbs(title: str) -> str:
    trails = {
        "Главная": [("Главная", None)],
        "Маршруты": [("Главная", "/dashboard"), ("Маршруты", None)],
        "Номера маршрута": [("Главная", "/dashboard"), ("Маршруты", "/routes"), ("Номера маршрута", None)],
        "История маршрута": [("Главная", "/dashboard"), ("Маршруты", "/routes"), ("История маршрута", None)],
        "История номера": [("Главная", "/dashboard"), ("Купленные номера", "/phones"), ("История номера", None)],
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
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,400,0,0">
  <style>
    html[data-theme="light-v2"] {{
      --bg: #F3F7F4;
      --surface: #FFFFFF;
      --surface-muted: #E9F2EE;
      --surface-soft: #EEF5F1;
      --surface-strong: #E1ECE7;
      --table-header-bg: #E3EEE9;
      --table-row-alt: #F6FAF8;
      --table-row-hover: #EAF6F2;
      --sidebar-bg: #EAF3EF;
      --text-strong: #1F2933;
      --text: #1F2933;
      --muted: #5F6F68;
      --text-soft: #7A8780;
      --border: #CCDAD4;
      --border-strong: #AFC4BA;
      --accent: #0F766E;
      --accent-strong: #0A4F49;
      --accent-hover: #0B5F59;
      --accent-soft: #DDF3EE;
      --accent-border: #A9D8CF;
      --cyber: #0F766E;
      --cyber-strong: #0A4F49;
      --cyber-soft: #DDF3EE;
      --pink: #6F7A3A;
      --pink-soft: #EEF1DE;
      --olive: #6F7A3A;
      --olive-soft: #EEF1DE;
      --warning: #D97706;
      --warning-hover: #B45309;
      --warning-soft: #FFF1DD;
      --warning-border: #F2C078;
      --provider-accent: #D97706;
      --provider-soft: #FFF4E5;
      --danger: #DC2626;
      --danger-strong: #B91C1C;
      --danger-soft: #FEE2E2;
      --danger-border: #FCA5A5;
      --success: #2F7D50;
      --success-soft: #E8F3EA;
      --success-border: #B9DEC0;
      --input-bg: #FFFFFF;
      --focus: #0F766E;
      --shadow-soft: 0 3px 10px rgba(31, 41, 51, 0.08);
      --shadow-card: 0 10px 28px rgba(31, 41, 51, 0.085);
      --shadow-card-hover: 0 14px 32px rgba(31, 41, 51, 0.12);
      --shadow-glow: 0 0 0 1px rgba(15, 118, 110, 0.18), 0 12px 26px rgba(15, 118, 110, 0.12);
      --radius-control: 9px;
      --radius-card: 14px;
    }}
    html[data-theme="mvp"] {{
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
    html[data-theme="dark"] {{
      --bg: #0B1020;
      --surface: #111827;
      --surface-muted: #151E2F;
      --surface-strong: #1E293B;
      --sidebar-bg: #080D19;
      --text-strong: #E5E7EB;
      --text: #CBD5E1;
      --muted: #94A3B8;
      --border: #263244;
      --border-strong: #334155;
      --accent: #38BDF8;
      --accent-strong: #0EA5E9;
      --accent-soft: rgba(56, 189, 248, 0.12);
      --cyber: #38BDF8;
      --cyber-strong: #0EA5E9;
      --cyber-soft: rgba(56, 189, 248, 0.12);
      --pink: #38BDF8;
      --pink-soft: rgba(56, 189, 248, 0.12);
      --success: #22C55E;
      --success-soft: rgba(34, 197, 94, 0.14);
      --warning: #F59E0B;
      --warning-soft: rgba(245, 158, 11, 0.14);
      --danger: #EF4444;
      --danger-soft: rgba(239, 68, 68, 0.14);
      --focus: var(--accent);
      --shadow-soft: 0 12px 28px rgba(0, 0, 0, .22);
      --shadow-card: 0 18px 45px rgba(0, 0, 0, .34);
      --shadow-glow: 0 0 0 1px rgba(56, 189, 248, .18), 0 18px 38px rgba(14, 165, 233, .14);
      --radius-control: 10px;
      --radius-card: 14px;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scrollbar-gutter: stable; }}
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: var(--text); background: var(--bg); font-size: 14px; line-height: 1.45; }}
    html[data-theme="light-v2"] body {{ background: var(--bg); }}
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
    .admin-link {{ display: flex; align-items: center; gap: 8px; padding: 6px 8px; font-size: 13px; line-height: 1.25; color: var(--text); }}
    .admin-link .nav-icon .material-symbols-rounded {{ font-size: 20px; }}
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
    .copy-column-button {{ display: inline-flex; align-items: center; justify-content: center; width: 30px; height: 24px; min-height: 24px; padding: 2px 6px; font-size: 12px; color: var(--accent-strong); border-color: var(--border-strong); background: var(--accent-soft); box-shadow: none; }}
    .copy-column-button svg {{ flex: 0 0 16px; width: 16px; height: 16px; }}
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
    input[type="checkbox"], input[type="radio"] {{ width: 14px; height: 14px; min-height: 14px; padding: 0; margin: 0 5px 0 0; vertical-align: -2px; accent-color: var(--accent); box-shadow: none; }}
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
    .server-checkbox-item {{ min-height: 30px; display: inline-flex; align-items: center; gap: 5px; border: 1px solid var(--border-strong); border-radius: var(--radius-control); padding: 5px 9px; background: var(--surface); margin: 0; font-weight: 700; line-height: 1; cursor: pointer; box-shadow: inset 0 1px 1px rgba(34, 48, 42, 0.03); transition: border-color 140ms ease, background 140ms ease, box-shadow 140ms ease, color 140ms ease; }}
    .server-checkbox-item:hover {{ border-color: var(--accent); background: var(--surface-muted); }}
    .server-checkbox-item input[type="checkbox"] {{ position: absolute; opacity: 0; pointer-events: none; }}
    .server-checkbox-item:has(input:checked) {{ border-color: var(--accent); background: var(--accent-soft); color: var(--text-strong); box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 24%, transparent) inset; }}
    .server-checkbox-item:has(input:checked)::before {{ content: "✓"; display: inline-flex; align-items: center; justify-content: center; width: 14px; height: 14px; border-radius: 4px; background: var(--accent); color: #fff; font-size: 10px; font-weight: 820; }}
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
    .required {{ display: inline-flex; align-items: baseline; color: var(--danger); font-weight: 760; line-height: 1; white-space: nowrap; }}
    .muted {{ color: var(--muted); font-weight: 500; }}
    .message-card, .error {{ border: 1px solid var(--danger); background: var(--danger-soft); color: var(--danger); padding: 14px; border-radius: var(--radius-card); }}

    .material-symbols-rounded {{
      font-family: 'Material Symbols Rounded';
      font-weight: normal;
      font-style: normal;
      font-size: 1em;
      line-height: 1;
      letter-spacing: normal;
      text-transform: none;
      display: inline-block;
      white-space: nowrap;
      word-wrap: normal;
      direction: ltr;
      -webkit-font-feature-settings: 'liga';
      -webkit-font-smoothing: antialiased;
      font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24;
    }}
    .sr-only {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}
    .nav-icon .material-symbols-rounded {{ font-size: 22px; }}
    .button .material-symbols-rounded, button .material-symbols-rounded, .action-icon .material-symbols-rounded, .history-link .material-symbols-rounded, .copy-column-button .material-symbols-rounded {{ font-size: 17px; }}
    .metric-icon .material-symbols-rounded {{ font-size: 26px; }}
    .quick-icon .material-symbols-rounded {{ font-size: 25px; }}
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
    .filter-grid label:not(.checkbox-inline):not(.scope-card), .form-grid label:not(.checkbox-inline):not(.scope-card):not(.spillover-checkbox):not(.important-checkbox) {{ display: block; white-space: nowrap; }}
    .filter-grid label:not(.checkbox-inline) > input, .filter-grid label:not(.checkbox-inline) > select, .filter-grid label:not(.checkbox-inline) > textarea, .form-grid label:not(.checkbox-inline):not(.scope-card):not(.spillover-checkbox):not(.important-checkbox) > input, .form-grid label:not(.checkbox-inline):not(.scope-card):not(.spillover-checkbox):not(.important-checkbox) > select, .form-grid label:not(.checkbox-inline):not(.scope-card):not(.spillover-checkbox):not(.important-checkbox) > textarea {{ display: block; margin-top: 4px; }}
    .filter-grid input, .filter-grid select, .form-grid input, .form-grid select {{ width: 100%; }}
    .form-grid .route-select-field {{ min-width: min(420px, 100%); width: clamp(420px, 44vw, 560px); grid-column: span 2; }}
    .form-grid .route-select-field .route-select {{ width: 100%; min-width: 0; font-size: 14px; }}
    .form-grid .route-select-field option {{ font-size: 13px; }}
    #routing-event-form {{ grid-template-columns: minmax(170px, 175px) minmax(360px, 520px) minmax(170px, 190px); column-gap: 12px; }}
    #routing-event-form .routing-provider-field {{ width: 175px; }}
    #routing-event-form .route-select-field {{ grid-column: auto; min-width: min(360px, 100%); width: clamp(360px, 38vw, 520px); }}
    #routing-event-form .routing-reason-field {{ width: 190px; }}
    #routing-event-form[data-current-scope='campaign_setting'] {{ grid-template-columns: minmax(0, 1fr); column-gap: 12px; }}
    #routing-event-form .provider-change-campaign-grid, #routing-event-form .provider-change-campaign-lower-grid {{ display: contents; }}
    #routing-event-form[data-current-scope='campaign_setting'] .provider-change-campaign-grid {{ grid-column: 1 / -1; display: grid; grid-template-columns: minmax(170px, 190px) minmax(220px, .95fr) minmax(260px, 1fr); gap: 12px; align-items: end; }}
    #routing-event-form[data-current-scope='campaign_setting'] .provider-change-campaign-lower-grid {{ grid-column: 1 / -1; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; align-items: start; }}
    #routing-event-form[data-current-scope='campaign_setting'] .provider-change-campaign-lower-grid > .routing-reason-field, #routing-event-form[data-current-scope='campaign_setting'] .provider-change-campaign-lower-grid > .campaign-company-field {{ align-self: start; min-width: 0; width: auto; }}
    #routing-event-form .provider-change-campaign-grid label, #routing-event-form .provider-change-campaign-lower-grid label {{ min-width: 0; width: auto; }}
    #routing-event-form .campaign-server-field, #routing-event-form .campaign-id-field, #routing-event-form .campaign-change-type-field, #routing-event-form .campaign-company-field, #routing-event-form .campaign-id-action-field {{ min-width: 0; width: auto; }}
    #routing-event-form .campaign-company-field {{ display: block; }}
    #routing-event-form .campaign-company-field .multi-select {{ margin: 4px 0 0; box-shadow: none; width: 100%; box-sizing: border-box; }}
    #routing-event-form .campaign-id-action-field {{ display: grid; grid-template-columns: minmax(0, 1fr); gap: 4px; align-items: end; }}
    #routing-event-form .campaign-id-inline-action {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: center; }}
    #routing-event-form .campaign-id-inline-action input {{ width: 100%; }}
    #routing-event-form .campaign-id-inline-action .small-button {{ width: 56px; min-height: 34px; padding: 5px 10px; }}
    #routing-event-form .field-error {{ display: block; min-height: 16px; color: var(--danger); font-size: 12px; font-weight: 600; }}
    #routing-event-form .field-label {{ display: inline-flex; align-items: baseline; gap: 4px; margin-bottom: 4px; font-weight: 650; white-space: nowrap; }}
    #routing-event-form .multi-select {{ position: relative; min-width: 0; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); }}
    #routing-event-form .multi-select > summary {{ min-height: 32px; padding: 6px 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 560; box-sizing: border-box; }}
    #routing-event-form .multi-select-panel {{ position: absolute; z-index: 20; inset-inline: 0; top: calc(100% + 4px); max-height: 280px; overflow: auto; padding: 8px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); box-shadow: var(--shadow-soft); }}
    #routing-event-form .multi-option {{ display: flex; gap: 8px; align-items: center; min-width: 0; padding: 6px 4px; font-weight: 560; cursor: pointer; }}
    #routing-event-form .multi-option input {{ width: auto; }}
    #routing-event-form .multi-option span {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    @media (max-width: 1020px) {{ #routing-event-form, #routing-event-form[data-current-scope='campaign_setting'] {{ grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }} #routing-event-form[data-current-scope='campaign_setting'] .provider-change-campaign-grid, #routing-event-form[data-current-scope='campaign_setting'] .provider-change-campaign-lower-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); overflow: visible; }} #routing-event-form .routing-provider-field, #routing-event-form .routing-reason-field, #routing-event-form .route-select-field, #routing-event-form .campaign-server-field, #routing-event-form .campaign-id-field, #routing-event-form .campaign-id-action-field, #routing-event-form .campaign-change-type-field, #routing-event-form .campaign-company-field {{ min-width: 0; }} }}
    @media (max-width: 720px) {{ .form-grid .route-select-field {{ grid-column: 1 / -1; width: 100%; min-width: 0; }} }}
    .filter-grid .checkbox-inline, .form-grid .checkbox-inline {{ min-width: auto; display: flex; align-items: center; gap: 5px; align-self: center; font-weight: 560; }}
    .important-checkbox, .form-grid .spillover-checkbox {{ min-width: 150px; min-height: 34px; display: inline-flex; align-items: center; gap: 8px; align-self: end; padding: 4px 0; font-weight: 720; white-space: nowrap; }}
    .important-checkbox input[type='checkbox'], .form-grid .spillover-checkbox input[type='checkbox'] {{ width: 22px; height: 22px; min-height: 22px; flex: 0 0 22px; margin: 0; border-color: var(--border-strong); accent-color: var(--accent); }}
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
    .column-settings-panel {{ position: absolute; right: 0; top: calc(100% + 6px); z-index: 30; display: grid; gap: 8px; min-width: 300px; max-height: 380px; overflow: auto; padding: 10px; border: 1px solid var(--border-strong); border-radius: var(--radius-control); background: #fff; box-shadow: var(--shadow-card); }}
    .column-settings-list {{ display: grid; gap: 4px; }}
    .column-settings-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: center; min-height: 30px; padding: 3px 4px; border-radius: 8px; }}
    .column-settings-row:hover {{ background: var(--surface-muted); }}
    .column-settings-row label {{ min-width: 0; display: flex; align-items: center; gap: 6px; margin: 0; font-size: 13px; font-weight: 560; white-space: nowrap; }}
    .column-settings-row.is-locked label {{ color: var(--muted); }}
    .column-order-controls {{ display: inline-flex; gap: 3px; }}
    .column-order-button {{ width: 24px; min-width: 24px; min-height: 24px; padding: 0; box-shadow: none; font-size: 12px; }}
    .column-reset {{ justify-content: flex-start; margin-top: 2px; padding: 5px 7px; min-height: 28px; border: 0; background: transparent; box-shadow: none; color: var(--accent-strong); font-size: 12px; }}
    [data-column-hidden="true"] {{ display: none !important; }}
    th[data-col] {{ position: sticky; }}
    .resizable-header {{ position: relative; padding-right: 16px; }}
    .column-resize-handle {{ position: absolute; top: 0; right: -3px; width: 8px; height: 100%; cursor: col-resize; user-select: none; touch-action: none; z-index: 6; }}
    .column-resize-handle::after {{ content: ""; position: absolute; top: 20%; bottom: 20%; left: 3px; width: 2px; border-radius: 999px; background: transparent; }}
    .resizable-header:hover .column-resize-handle::after, .column-resize-handle:hover::after, body.is-resizing-column .column-resize-handle::after {{ background: var(--accent); }}
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
    td.history-cell, th[data-col='history'] {{ width: 34px; min-width: 34px; max-width: 34px; padding-left: 6px; padding-right: 6px; text-align: center; }}
    .history-link {{ display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px; border-radius: 999px; color: var(--accent-strong); text-decoration: none; font-size: 16px; line-height: 1; font-weight: 800; }}
    .history-link:hover {{ background: var(--accent-soft); text-decoration: none; }}
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
    th[data-col="actions"], td[data-col="actions"], .dictionary-workspace th:last-child {{ width: 64px; min-width: 64px; max-width: 72px; text-align: center; }}
    th[data-col="actions"], .dictionary-workspace th:last-child {{ padding-left: 8px; padding-right: 8px; }}
    td[data-col="actions"] {{ padding: 6px 8px; overflow: visible; }}
    .actions, .compact-actions, td[data-col="actions"] {{ white-space: nowrap; text-align: center; }}
    td[data-col="actions"] form {{ justify-content: center; }}
    .route-numbers-action {{ min-height: 28px; padding: 4px 8px; font-size: 12px; box-shadow: none; }}
    .action-button, .actions .button, .actions button, .compact-actions .button, .compact-actions button, td[data-col="actions"] .button, td[data-col="actions"] button {{ min-width: 30px; min-height: 30px; padding: 4px 7px; border-radius: 8px; font-size: 12px; line-height: 1; box-shadow: none; }}
    .edit-action, td[data-col="actions"] details.edit-details > summary {{ position: relative; width: 32px; min-width: 32px; height: 32px; min-height: 32px; padding: 0; display: inline-flex; align-items: center; justify-content: center; overflow: visible; color: transparent; font-size: 0; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--surface); box-shadow: none; list-style: none; }}
    .edit-action:hover, td[data-col="actions"] details.edit-details > summary:hover {{ background: var(--accent-soft); border-color: var(--accent); color: transparent; }}
    .edit-action::before, td[data-col="actions"] details.edit-details > summary::before {{ content: "edit"; font-family: 'Material Symbols Rounded'; font-size: 17px; color: var(--accent-strong); line-height: 1; }}
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
    td[data-col="actions"] details.edit-details[open] > form {{
      position: fixed !important;
      left: 50% !important;
      right: auto !important;
      top: 50% !important;
      z-index: 1000 !important;
      width: min(560px, calc(100vw - 32px)) !important;
      max-width: min(560px, calc(100vw - 32px)) !important;
      max-height: calc(100vh - 48px) !important;
      overflow: auto !important;
      margin: 0 !important;
      padding: 16px !important;
      transform: translate(-50%, -50%) !important;
      border: 1px solid var(--border-strong) !important;
      border-radius: var(--radius-card) !important;
      background: var(--surface) !important;
      box-shadow: 0 22px 70px rgba(15, 23, 42, .22) !important;
      text-align: left !important;
      white-space: normal !important;
    }}
    td[data-col="actions"] details.edit-details[open]::before {{
      content: "";
      position: fixed;
      inset: 0;
      z-index: 999;
      background: rgba(15, 23, 42, .16);
    }}
    td[data-col="actions"] details.edit-details[open] > summary {{ z-index: 1001; }}
    td[data-col="actions"] details.edit-details > form input,
    td[data-col="actions"] details.edit-details > form select,
    td[data-col="actions"] details.edit-details > form textarea {{
      width: 100%;
      box-sizing: border-box;
    }}
    .admin-edit-actions {{
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      flex-basis: 100%;
      width: 100%;
      margin-top: 4px;
    }}
    .admin-edit-cancel {{
      background: var(--surface-muted);
      color: var(--text);
      border-color: var(--border-strong);
    }}

    .modal-form-card > summary {{ display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 36px; padding: 8px 14px; border: 1px solid var(--border-strong); border-radius: 10px; background: var(--accent-soft); color: var(--accent-strong); font-weight: 760; cursor: pointer; list-style: none; }}
    .modal-form-card > summary::-webkit-details-marker {{ display: none; }}
    .modal-form-card > summary::marker {{ content: ""; }}
    .modal-form-card[open]::before, .modal-overlay {{ content: ""; position: fixed; inset: 0; z-index: 980; background: rgba(0, 0, 0, 0.55); }}
    .modal-form-card[open] > form, .modal-form-card[open] > .modal-body, .modal-card {{ position: fixed; left: 50%; top: 50%; z-index: 990; width: min(1040px, calc(100vw - 32px)); max-height: calc(100vh - 48px); overflow: auto; scrollbar-gutter: stable; transform: translate(-50%, -50%); margin: 0; padding: 20px; border: 1px solid var(--border-strong); border-radius: 18px; background: var(--surface); color: var(--text); box-shadow: 0 24px 80px rgba(0,0,0,.28); box-sizing: border-box; }}
    .modal-card form, .modal-form-card[open] > form {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .modal-card form label, .modal-card form fieldset, .modal-form-card[open] > form label, .modal-form-card[open] > form fieldset {{ min-width: 0; }}
    .modal-card form .wide, .modal-card form p, .modal-card form fieldset, .modal-form-card[open] > form .wide, .modal-form-card[open] > form p, .modal-form-card[open] > form fieldset {{ grid-column: 1 / -1; }}
    .modal-card h2 {{ margin: 0 0 4px; color: var(--text-strong); }}
    .modal-description {{ margin: 0 0 16px; color: var(--muted); }}
    .modal-actions {{ grid-column: 1 / -1; display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; padding-top: 12px; border-top: 1px solid var(--border); }}
    .modal-save, .admin-edit-save {{ background: var(--success); border-color: var(--success); color: #fff; font-weight: 780; }}
    .modal-save:hover, .admin-edit-save:hover {{ background: color-mix(in srgb, var(--success) 88%, #000); border-color: var(--success); color: #fff; }}
    .modal-cancel, .admin-edit-cancel {{ background: var(--danger-soft); color: var(--danger); border-color: color-mix(in srgb, var(--danger) 42%, var(--border-strong)); }}
    .modal-cancel:hover, .admin-edit-cancel:hover {{ background: color-mix(in srgb, var(--danger-soft) 78%, var(--surface)); color: var(--danger); border-color: var(--danger); }}
    .modal-card input, .modal-card select, .modal-card textarea, .modal-form-card[open] input, .modal-form-card[open] select, .modal-form-card[open] textarea {{ width: 100%; box-sizing: border-box; background: var(--input-bg, var(--surface)); color: var(--text); border-color: var(--border-strong); }}
    html[data-theme="dark"] .modal-card, html[data-theme="dark"] .modal-form-card[open] > form, html[data-theme="dark"] .modal-form-card[open] > .modal-body {{ background: var(--surface); border-color: var(--border-strong); color: var(--text); }}
    html[data-theme="dark"] .modal-overlay, html[data-theme="dark"] .modal-form-card[open]::before {{ background: rgba(0, 0, 0, 0.55); }}
    @media (max-width: 720px) {{ .modal-card form, .modal-form-card[open] > form {{ grid-template-columns: 1fr; }} .modal-card, .modal-form-card[open] > form, .modal-form-card[open] > .modal-body {{ width: calc(100vw - 18px); max-height: calc(100vh - 18px); padding: 14px; }} }}
    .danger-action, form[action$="/deactivate"] button {{ min-height: 28px; min-width: auto; padding: 4px 8px; color: var(--danger-strong, var(--danger)); border-color: var(--danger); background: var(--danger-soft); font-size: 12px; font-weight: 720; box-shadow: none; }}
    .danger-action:hover, form[action$="/deactivate"] button:hover {{ background: color-mix(in srgb, var(--danger-soft) 78%, var(--surface)); border-color: var(--danger); color: var(--danger); }}
    html[data-theme="mvp"] .side-link:hover, html[data-theme="mvp"] .admin-link:hover, html[data-theme="terminal-paper"] .side-link:hover, html[data-theme="terminal-paper"] .admin-link:hover {{ background: color-mix(in srgb, var(--surface) 78%, transparent); border-color: var(--border); color: var(--text-strong); }}
    html[data-theme="mvp"] .side-link.active, html[data-theme="terminal-paper"] .side-link.active {{ background: var(--surface); border-color: var(--border-strong); color: var(--accent-strong); box-shadow: var(--shadow-soft); font-weight: 780; }}
    html[data-theme="mvp"] .side-link.active::before, html[data-theme="terminal-paper"] .side-link.active::before {{ content: none; }}
    html[data-theme="mvp"] .admin-link.active, html[data-theme="terminal-paper"] .admin-link.active {{ background: var(--surface); border-color: var(--border-strong); color: var(--accent-strong); font-weight: 730; box-shadow: var(--shadow-soft); }}
    html[data-theme="mvp"] .metric-card, html[data-theme="terminal-paper"] .metric-card {{ border: 1px solid var(--border); background: var(--surface); box-shadow: var(--shadow-soft); }}
    html[data-theme="mvp"] .status-badge, html[data-theme="terminal-paper"] .status-badge {{ border-color: var(--border); background: var(--surface-muted); color: var(--text); }}
    html[data-theme="dark"] .theme-selector select:focus, html[data-theme="dark"] .theme-selector select:focus-visible {{ border-color: var(--accent); outline-color: var(--accent); box-shadow: 0 0 0 3px rgba(109, 93, 252, 0.14); }}
    html[data-theme="dark"] th {{ background: var(--surface-strong); color: var(--text-strong); }}
    html[data-theme="dark"] tbody tr:hover {{ background: rgba(109, 93, 252, 0.08); }}
    html[data-theme="dark"] .table-card td[data-copy-column="phone-number"], html[data-theme="dark"] code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    html[data-theme="dark"] .hero-action {{ background: linear-gradient(135deg, var(--accent-strong), var(--accent)); box-shadow: var(--shadow-glow); }}
    html[data-theme="dark"] .quick-link-card {{ transition: transform 140ms ease, border-color 140ms ease, box-shadow 140ms ease, background 140ms ease; }}

    /* Figma-inspired light operations admin */
    :root, html[data-theme="mvp"] {{
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
    .nav-icon, .side-icon, .metric-icon, .quick-icon, .feed-icon {{ display: inline-flex; align-items: center; justify-content: center; }}
    .side-link:hover .nav-icon, .side-link.active .nav-icon {{ color: var(--accent-strong); }}
    .side-link.has-inline-icon::before, .side-link.has-inline-icon.active::before {{ content: none; display: none; }}
    .side-link::before {{ content: attr(data-icon); width: 22px; height: 22px; display: inline-flex; align-items: center; justify-content: center; color: #7786ad; font-size: 18px; position: static; flex: 0 0 22px; border-radius: 0; background: transparent; }}
    .side-link.active::before {{ content: attr(data-icon); color: var(--accent-strong); position: static; width: 22px; height: 22px; flex: 0 0 22px; background: transparent; }}
    .side-link:hover {{ background: #f3f6ff; color: var(--accent-strong); }}
    .side-link.active {{ background: #eef1ff; border-color: #d5ddff; color: var(--accent-strong); box-shadow: none; }}
    .side-link.active .side-icon {{ color: var(--accent-strong); }}
    .side-link-disabled, .side-link-disabled:hover, .side-link-disabled:disabled {{ color: var(--muted); background: transparent; border-color: transparent; box-shadow: none; cursor: not-allowed; opacity: .58; }}
    .side-link-disabled .nav-icon, .side-link-disabled:hover .nav-icon {{ color: var(--muted); }}
    .admin-tree {{ margin: 0 0 0 34px; padding-left: 10px; border-left: 1px solid var(--border); }}
    .admin-link {{ display: block; padding: 7px 10px; font-size: 12px; }}
    .sidebar-footer {{ margin-top: auto; display: grid; gap: 10px; padding-top: 12px; border-top: 1px solid var(--border); }}
    .theme-selector-wrap {{ position: relative; width: 100%; }}
    .theme-selector-wrap .theme-selector {{ justify-content: flex-start; box-shadow: none; }}
    .theme-menu {{ position: absolute; left: 0; right: 0; bottom: calc(100% + 6px); display: none; gap: 4px; padding: 6px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); box-shadow: var(--shadow-card); z-index: 80; }}
    .theme-selector-wrap.open .theme-menu {{ display: grid; }}
    .theme-menu button {{ justify-content: flex-start; min-height: 34px; padding: 7px 9px; border: 0; border-radius: 8px; background: transparent; box-shadow: none; color: var(--text); font-size: 13px; }}
    .theme-menu button[aria-checked="true"] {{ background: var(--accent-soft); color: var(--accent-strong); font-weight: 800; }}
    .theme-menu button[aria-checked="false"] .theme-check {{ visibility: hidden; }}
    .theme-menu button small {{ margin-left: auto; color: var(--muted); font-size: 11px; font-weight: 700; }}
    .theme-menu button:disabled {{ opacity: .62; }}
    html[data-theme="dark"] .sidebar, html[data-theme="dark"] .breadcrumbs {{ background: var(--sidebar-bg); }}
    html[data-theme="dark"] .side-link, html[data-theme="dark"] .sidebar-collapse {{ color: var(--text); }}
    html[data-theme="dark"] .side-link:hover {{ background: var(--surface-muted); color: var(--text-strong); }}
    html[data-theme="dark"] .side-link.active {{ background: var(--accent-soft); border-color: var(--border-strong); color: var(--accent); }}
    html[data-theme="dark"] .side-link-disabled, html[data-theme="dark"] .side-link-disabled:hover, html[data-theme="dark"] .side-link-disabled:disabled {{ color: var(--muted); background: transparent; border-color: transparent; opacity: .55; }}
    html[data-theme="dark"] .side-link-disabled .nav-icon, html[data-theme="dark"] .side-link-disabled:hover .nav-icon {{ color: var(--muted); }}
    html[data-theme="dark"] .current-user-selector {{ background: var(--surface-muted); border-color: var(--border); }}
    html[data-theme="dark"] td, html[data-theme="dark"] tbody tr:nth-child(even) td, html[data-theme="dark"] .metric-card {{ background: var(--surface); border-color: var(--border); }}
    html[data-theme="dark"] tbody tr:hover td {{ background: var(--surface-muted); }}
    html[data-theme="dark"] .metric-label, html[data-theme="dark"] .quick-copy small {{ color: var(--muted); }}
    html[data-theme="dark"] .quick-icon, html[data-theme="dark"] .metric-icon {{ background: var(--accent-soft); color: var(--accent); }}
    .selectable-cell, .selectable-text {{ cursor: text; }}
    .selectable-cell:hover {{ background: color-mix(in srgb, var(--accent-soft) 62%, var(--surface)) !important; }}
    .selectable-text::selection, .selectable-text *::selection, .data-table ::selection, .table-card table ::selection, .journal-card table ::selection {{ background: rgba(37, 99, 235, 0.22); color: var(--text-strong); }}
    html[data-theme="dark"] .data-table ::selection, html[data-theme="dark"] .table-card table ::selection, html[data-theme="dark"] .journal-card table ::selection {{ background: rgba(56, 189, 248, 0.35); color: var(--text-strong); }}
    .current-user-selector, .theme-selector, .sidebar-collapse {{ display: flex; align-items: center; gap: 10px; width: 100%; min-height: 42px; padding: 8px 10px; border: 1px solid transparent; border-radius: 12px; background: transparent; color: var(--text); text-align: left; }}
    .sidebar-collapse {{ width: 36px; min-width: 36px; max-width: 36px; height: 36px; min-height: 36px; padding: 0; justify-content: center; justify-self: end; color: #223158; border-color: var(--border); background: var(--surface); box-shadow: var(--shadow-soft); overflow: hidden; }}
    .sidebar-collapse:hover {{ background: var(--accent-soft); border-color: var(--accent); color: var(--accent-strong); }}
    .sidebar-collapse-icon {{ width: 18px; height: 18px; display: inline-flex; align-items: center; justify-content: center; }}
    .sidebar-collapse-icon .material-symbols-rounded {{ font-size: 20px; }}
    .current-user-selector {{ position: relative; background: #f4f6ff; border-color: #e2e8ff; }} .current-user-selector summary {{ display: flex; align-items: center; gap: 10px; cursor: pointer; list-style: none; }} .current-user-selector summary::-webkit-details-marker {{ display: none; }} .current-user-menu {{ position: absolute; left: 0; right: 0; bottom: calc(100% + 6px); display: grid; gap: 4px; padding: 6px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); box-shadow: var(--shadow-card); z-index: 50; }} .current-user-menu a {{ display: block; padding: 6px 8px; border-radius: 7px; color: var(--text); font-size: 12px; font-weight: 700; text-decoration: none; }} .current-user-menu a:hover {{ background: var(--accent-soft); color: var(--accent-strong); }} .current-user-menu .logout-link {{ color: #b42318; }} .user-icon {{ background: #4f46e5; color: #fff; border-radius: 9px; width: 32px; height: 32px; }} .user-icon .material-symbols-rounded {{ font-size: 20px; }} .login-body {{ min-height: 100vh; display: grid; place-items: center; padding: 24px; }} .login-shell {{ width: min(560px, 100%); }} .login-card {{ padding: 28px; border: 1px solid var(--border); border-radius: 18px; background: var(--surface); box-shadow: var(--shadow-card); }} .login-card h1 {{ margin-bottom: 6px; }} .login-users {{ display: grid; gap: 10px; margin: 20px 0; }} .login-user-card {{ display: flex; align-items: center; gap: 12px; padding: 12px; border: 1px solid var(--border); border-radius: 12px; cursor: pointer; }} .login-user-card:hover {{ border-color: var(--accent); background: var(--accent-soft); }} .login-user-card span strong, .login-user-card span small {{ display: block; }} .login-user-card span small, .muted {{ color: var(--muted); }} .login-error {{ padding: 10px 12px; border-radius: 10px; background: var(--danger-soft); color: #b42318; font-weight: 700; }}
    .user-copy strong, .user-copy small {{ display: block; }} .user-copy small {{ color: var(--muted); }}
    .app-shell.sidebar-collapsed {{ grid-template-columns: 70px minmax(0, 1fr); }}
    .sidebar-collapsed .sidebar {{ padding-left: 8px; padding-right: 8px; }}
    .sidebar-collapsed .brand-copy, .sidebar-collapsed .side-label, .sidebar-collapsed .user-copy, .sidebar-collapsed .current-user-selector .logout-link, .sidebar-collapsed .admin-tree {{ display: none; }}
    .sidebar-collapsed .side-link {{ font-size: 0; gap: 0; }}
    .sidebar-collapsed .sidebar-head {{ grid-template-columns: 1fr; gap: 10px; }}
    .sidebar-collapsed .sidebar-collapse {{ order: -1; justify-self: center; }}
    .sidebar-collapsed .sidebar-collapse-icon {{ transform: scaleX(-1); color: var(--accent-strong); }}
    .sidebar-collapsed .brand-block, .sidebar-collapsed .side-link, .sidebar-collapsed .current-user-selector, .sidebar-collapsed .theme-selector {{ justify-content: center; padding-left: 0; padding-right: 0; }}
    .sidebar-collapsed [data-tooltip] {{ position: relative; }}
    .sidebar-collapsed [data-tooltip]:hover::after {{ content: attr(data-tooltip); position: absolute; left: calc(100% + 10px); top: 50%; transform: translateY(-50%); z-index: 10000; pointer-events: none; white-space: nowrap; border-radius: 8px; padding: 7px 9px; background: #111827; color: #fff; font-size: 12px; box-shadow: var(--shadow-card); }}
    .metrics-grid {{ grid-template-columns: repeat(4, minmax(180px,1fr)); gap: 20px; margin: 8px 0 28px; }}
    .metric-card {{ min-height: 156px; padding: 20px; border: 1px solid var(--border); border-left: 1px solid var(--border); border-radius: 14px; background: #fff; box-shadow: var(--shadow-card); }}
    .metric-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }}
    .metric-icon {{ width: 38px; height: 38px; border-radius: 13px; background: #eef1ff; color: var(--accent-strong); }}
    .metric-card.green .metric-icon {{ background: #eafaf1; color: #16a34a; }} .metric-card.violet .metric-icon {{ background: #f1edff; color: #7c3aed; }} .metric-card.orange .metric-icon {{ background: #fff7e8; color: #f97316; }}
    .sparkline {{ width: 96px; height: 32px; }} .sparkline polyline {{ fill: none; stroke: currentColor; stroke-width: 2; }}
    .metric-label {{ min-height: 0; text-transform: none; letter-spacing: 0; font-size: 12px; color: var(--muted); }} .metric-value {{ font-size: 27px; margin: 4px 0 4px; }} .metric-hint {{ color: var(--muted); font-weight: 700; }} .metric-card.orange .metric-hint {{ color: var(--muted); }}
    .quick-links {{ grid-template-columns: repeat(3, minmax(240px, 1fr)); gap: 12px; }}
    .quick-link-card {{ grid-template-columns: 44px 1fr 20px; align-items: center; gap: 14px; min-height: 78px; padding: 16px 20px; border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow-card); }}
    .quick-icon {{ width: 40px; height: 40px; border-radius: 14px; background: #eef1ff; color: var(--accent-strong); }} .quick-copy strong {{ display:block; color: var(--text-strong); }} .quick-copy small {{ display:block; color: var(--muted); }} .quick-arrow {{ color: var(--muted); font-size: 22px; }}
    .dashboard-panel-title {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin: 0 0 10px; }}
    .dashboard-panel-title h2 {{ margin: 0; }}
    .event-feed {{ overflow: hidden; background: var(--surface); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow-card); }}
    .event-feed article {{ display: grid; grid-template-columns: 18px 1fr minmax(120px, auto); align-items: center; gap: 14px; min-height: 64px; padding: 12px 18px; border-bottom: 1px solid var(--border); }} .event-feed article:last-child {{ border-bottom: 0; }}
    .feed-icon {{ width: 10px; height: 10px; border-radius: 50%; background: var(--accent); color: transparent; box-shadow: 0 0 0 5px var(--accent-soft); }} .feed-icon.ok {{ background: var(--success); box-shadow: 0 0 0 5px var(--success-soft); }} .feed-icon.warn {{ background: var(--warning); box-shadow: 0 0 0 5px var(--warning-soft); }} .feed-icon.neutral {{ background: var(--muted); box-shadow: 0 0 0 5px var(--surface-muted); }} .event-feed small {{ display:block; color: var(--muted); }} .event-feed time {{ color: var(--muted); text-align:right; white-space: nowrap; }}
    .event-feed-empty {{ padding: 18px; color: var(--muted); background: var(--surface-muted); }}
    .content:has(> .table-page-container) {{ max-width: none; min-width: 0; }}
    .table-page-container {{ width: min(1580px, 100%); max-width: 100%; min-width: 0; margin-inline: auto; }}
    .table-page-container > *, .table-page-container details, .table-page-container fieldset {{ min-width: 0; }}
    .table-page-container .filter-card, .table-page-container .form-card, .table-page-container .table-card, .table-page-container .journal-card, .table-page-container .table-footer {{ width: 100%; max-width: 100%; }}
    .table-page-container .filter-grid, .table-page-container .form-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(190px, 100%), 1fr)); }}
    .table-page-container .filter-grid label, .table-page-container .form-grid label {{ min-width: 0; }}
    .table-page-container .table-scroll {{ max-width: 100%; }}
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
    .edit-action::before, td[data-col="actions"] details.edit-details > summary::before {{ content: "edit"; font-family: 'Material Symbols Rounded'; font-size: 17px; }}
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
    html[data-theme="mvp"] .side-link:not(.has-inline-icon)::before,
    html[data-theme="mvp"] .side-link.active:not(.has-inline-icon)::before,
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
    html[data-theme="mvp"] .side-link.active:not(.has-inline-icon)::before,
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
      content: "edit" !important;
      font-family: 'Material Symbols Rounded' !important;
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

    table[data-table-key="phones"] td[data-col="number"] .phone-number-cell {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      max-width: 100%;
      white-space: nowrap;
    }}

    html[data-theme="dark"] {{
      color-scheme: dark;
    }}

    html[data-theme="dark"],
    html[data-theme="dark"] body,
    html[data-theme="dark"] .app-shell {{
      background: #0B1020;
      color: var(--text);
    }}

    html[data-theme="dark"] * {{
      scrollbar-color: var(--border-strong) var(--surface);
    }}

    html[data-theme="dark"] *::-webkit-scrollbar {{
      width: 10px;
      height: 10px;
    }}

    html[data-theme="dark"] *::-webkit-scrollbar-track {{
      background: var(--surface);
      border-radius: 999px;
    }}

    html[data-theme="dark"] *::-webkit-scrollbar-thumb {{
      background: var(--border-strong);
      border: 2px solid var(--surface);
      border-radius: 999px;
    }}

    html[data-theme="dark"] *::-webkit-scrollbar-thumb:hover {{
      background: var(--accent);
    }}

    html[data-theme="dark"] .content {{
      background:
        radial-gradient(circle at 86% 0%, rgba(56, 189, 248, .10), transparent 24rem),
        radial-gradient(circle at 8% 4%, rgba(14, 165, 233, .07), transparent 20rem);
    }}

    html[data-theme="dark"] .breadcrumbs {{
      background: linear-gradient(180deg, #0b1224 0%, var(--bg) 100%);
      border-bottom-color: var(--border);
      color: var(--muted);
    }}

    html[data-theme="dark"] .breadcrumbs::after {{
      color: var(--text-strong);
    }}

    html[data-theme="dark"] .sidebar {{
      background: linear-gradient(180deg, #080D19 0%, #0A1020 100%);
      border-right-color: var(--border);
      overflow-y: auto;
    }}

    html[data-theme="dark"] .brand-mark {{
      background: linear-gradient(135deg, var(--accent-strong), var(--accent));
      box-shadow: 0 12px 28px rgba(14, 165, 233, .20);
    }}

    html[data-theme="dark"] .brand-copy strong,
    html[data-theme="dark"] .quick-copy strong {{
      color: var(--text-strong);
    }}

    html[data-theme="dark"] .brand-copy span,
    html[data-theme="dark"] .quick-copy small,
    html[data-theme="dark"] .event-feed small,
    html[data-theme="dark"] .event-feed time {{
      color: var(--muted);
    }}

    html[data-theme="dark"] .side-link,
    html[data-theme="dark"] .admin-link {{
      color: var(--text);
    }}

    html[data-theme="dark"] .side-icon,
    html[data-theme="dark"] .nav-icon,
    html[data-theme="dark"] .sidebar .side-link:not(.has-inline-icon)::before,
    html[data-theme="dark"] .sidebar-collapsed .side-link:not(.has-inline-icon)::before {{
      color: #7DD3FC !important;
    }}

    html[data-theme="dark"] .side-link:hover,
    html[data-theme="dark"] .admin-link:hover {{
      background: rgba(56, 189, 248, .10);
      border-color: rgba(56, 189, 248, .28);
      color: var(--text-strong);
    }}

    html[data-theme="dark"] .side-link.active,
    html[data-theme="dark"] .sidebar-collapsed .side-link.active {{
      background: linear-gradient(135deg, rgba(14, 165, 233, .22), rgba(56, 189, 248, .12)) !important;
      border-color: rgba(56, 189, 248, .42) !important;
      color: #E0F2FE;
      box-shadow: inset 3px 0 0 var(--accent), 0 10px 24px rgba(0, 0, 0, .18);
    }}

    html[data-theme="dark"] .side-link.active .nav-icon,
    html[data-theme="dark"] .side-link.active .side-icon,
    html[data-theme="dark"] .sidebar .side-link.active:not(.has-inline-icon)::before,
    html[data-theme="dark"] .sidebar-collapsed .side-link.active:not(.has-inline-icon)::before {{
      color: var(--accent) !important;
    }}

    html[data-theme="dark"] .admin-tree,
    html[data-theme="dark"] .current-user-selector,
    html[data-theme="dark"] .theme-selector,
    html[data-theme="dark"] .sidebar-collapse,
    html[data-theme="dark"] .theme-menu,
    html[data-theme="dark"] .current-user-menu {{
      background: var(--surface-muted);
      border-color: var(--border);
      color: var(--text);
    }}

    html[data-theme="dark"] .button,
    html[data-theme="dark"] button,
    html[data-theme="dark"] .table-utility-button,
    html[data-theme="dark"] .column-settings summary,
    html[data-theme="dark"] .route-numbers-action {{
      background: #101A2C;
      border-color: var(--border-strong);
      color: var(--text-strong);
      box-shadow: none;
    }}

    html[data-theme="dark"] .button:hover,
    html[data-theme="dark"] button:hover,
    html[data-theme="dark"] .column-settings summary:hover {{
      background: var(--accent-soft);
      border-color: var(--accent);
      color: #E0F2FE;
    }}

    html[data-theme="dark"] .hero-action,
    html[data-theme="dark"] form button[type="submit"],
    html[data-theme="dark"] .dictionary-add button[type="submit"] {{
      background: linear-gradient(135deg, var(--accent-strong), var(--accent));
      border-color: rgba(125, 211, 252, .55);
      color: #03111F;
      font-weight: 800;
      box-shadow: var(--shadow-glow);
    }}

    html[data-theme="dark"] .card,
    html[data-theme="dark"] details,
    html[data-theme="dark"] fieldset,
    html[data-theme="dark"] .filter-card,
    html[data-theme="dark"] .form-card,
    html[data-theme="dark"] .table-footer,
    html[data-theme="dark"] .table-card,
    html[data-theme="dark"] .journal-card,
    html[data-theme="dark"] .dictionary-card,
    html[data-theme="dark"] .dictionary-toolbar,
    html[data-theme="dark"] .event-feed,
    html[data-theme="dark"] .metric-card,
    html[data-theme="dark"] .quick-link-card,
    html[data-theme="dark"] .login-card {{
      background: var(--surface);
      border-color: var(--border);
      color: var(--text);
      box-shadow: var(--shadow-soft);
    }}

    html[data-theme="dark"] details[open] > summary,
    html[data-theme="dark"] .table-card h2,
    html[data-theme="dark"] .journal-card h2,
    html[data-theme="dark"] .empty-state {{
      background: var(--surface-muted);
      border-color: var(--border);
      color: var(--text-strong);
    }}

    html[data-theme="dark"] .filter-grid,
    html[data-theme="dark"] .form-grid,
    html[data-theme="dark"] details > form,
    html[data-theme="dark"] details > .card {{
      background: transparent;
      color: var(--text);
    }}

    html[data-theme="dark"] input,
    html[data-theme="dark"] select,
    html[data-theme="dark"] textarea {{
      background: #0E1728;
      border-color: var(--border-strong);
      color: var(--text-strong);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .03);
    }}

    html[data-theme="dark"] input::placeholder,
    html[data-theme="dark"] textarea::placeholder {{
      color: #718096;
    }}

    html[data-theme="dark"] input:disabled,
    html[data-theme="dark"] select:disabled,
    html[data-theme="dark"] textarea:disabled {{
      background: #0B1220;
      color: #64748B;
      border-color: #253244;
      opacity: .72;
    }}

    html[data-theme="dark"] table,
    html[data-theme="dark"] .table-card table,
    html[data-theme="dark"] .journal-card table {{
      background: var(--surface);
      color: var(--text);
    }}

    html[data-theme="dark"] th {{
      background: #1B2740;
      border-bottom-color: var(--border-strong);
      color: var(--text-strong);
    }}

    html[data-theme="dark"] th,
    html[data-theme="dark"] td {{
      border-right-color: var(--border);
      border-bottom-color: var(--border);
    }}

    html[data-theme="dark"] td,
    html[data-theme="dark"] tbody tr:nth-child(even) td {{
      background: var(--surface);
      color: var(--text);
    }}

    html[data-theme="dark"] tbody tr:nth-child(even) td {{
      background: var(--surface-muted);
    }}

    html[data-theme="dark"] tbody tr:hover td {{
      background: rgba(56, 189, 248, .10);
      color: var(--text-strong);
    }}

    html[data-theme="dark"] td[data-col="actions"] .edit-action,
    html[data-theme="dark"] td[data-col="actions"] details.edit-details > summary {{
      background: #101A2C !important;
      border-color: var(--border-strong) !important;
    }}

    html[data-theme="dark"] td[data-col="actions"] details.edit-details[open] > form {{
      background: var(--surface) !important;
      border-color: var(--border-strong) !important;
      box-shadow: 0 28px 90px rgba(0, 0, 0, .58) !important;
    }}

    html[data-theme="dark"] td[data-col="actions"] details.edit-details[open]::before {{
      background: rgba(3, 7, 18, .66);
      backdrop-filter: blur(2px);
    }}

    html[data-theme="dark"] .admin-edit-cancel {{
      background: #101A2C;
      border-color: var(--border-strong);
      color: var(--text);
      box-shadow: none;
    }}

    html[data-theme="dark"] .metric-icon,
    html[data-theme="dark"] .quick-icon,
    html[data-theme="dark"] .feed-icon {{
      background: var(--accent-soft);
      color: var(--accent);
    }}

    html[data-theme="dark"] .feed-icon.ok,
    html[data-theme="dark"] .dot-status.ok span {{
      background: var(--success);
      color: #052E16;
    }}

    html[data-theme="dark"] .feed-icon.warn,
    html[data-theme="dark"] .dot-status.warning span {{
      background: var(--warning);
      color: #331D03;
    }}

    html[data-theme="dark"] .feed-icon.danger,
    html[data-theme="dark"] .dot-status.danger span {{
      background: var(--danger);
      color: #450A0A;
    }}

    html[data-theme="dark"] .status-badge {{
      border: 1px solid rgba(56, 189, 248, .26);
      border-radius: 999px;
      background: var(--accent-soft);
      color: #BAE6FD;
      padding: 2px 7px;
    }}

    html[data-theme="dark"] .danger-action,
    html[data-theme="dark"] form[action$="/deactivate"] button,
    html[data-theme="dark"] button[onclick*="Деактив"],
    html[data-theme="dark"] button[onclick*="Удал"],
    html[data-theme="dark"] button[onclick*="Отключ"] {{
      background: var(--danger-soft);
      border-color: rgba(248, 113, 113, .48);
      color: #FCA5A5;
      box-shadow: none;
    }}

    html[data-theme="dark"] details.filter-card,
    html[data-theme="dark"] details.form-card,
    html[data-theme="dark"] details.table-controls,
    html[data-theme="dark"] .table-page-container details.filter-card,
    html[data-theme="dark"] .table-page-container details.form-card,
    html[data-theme="dark"] .filter-card,
    html[data-theme="dark"] .form-card,
    html[data-theme="dark"] .filter-panel,
    html[data-theme="dark"] .form-panel,
    html[data-theme="dark"] .dashboard-feed,
    html[data-theme="dark"] .event-feed,
    html[data-theme="dark"] .activity-feed,
    html[data-theme="dark"] .timeline-card,
    html[data-theme="dark"] .activity-list,
    html[data-theme="dark"] .notice,
    html[data-theme="dark"] .empty-state {{
      background: var(--surface) !important;
      border-color: var(--border) !important;
      color: var(--text) !important;
      box-shadow: var(--shadow-soft);
    }}

    html[data-theme="dark"] details.filter-card > summary,
    html[data-theme="dark"] details.form-card > summary,
    html[data-theme="dark"] details.table-controls > summary,
    html[data-theme="dark"] details > summary,
    html[data-theme="dark"] .filter-summary,
    html[data-theme="dark"] .form-summary {{
      background: var(--surface-muted) !important;
      border-color: var(--border) !important;
      color: var(--text-strong) !important;
    }}

    html[data-theme="dark"] details.filter-card > form,
    html[data-theme="dark"] details.form-card > form,
    html[data-theme="dark"] .filter-card .filter-grid,
    html[data-theme="dark"] .form-card .form-grid,
    html[data-theme="dark"] .filter-panel,
    html[data-theme="dark"] .form-panel {{
      background: var(--surface) !important;
      color: var(--text) !important;
    }}

    html[data-theme="dark"] fieldset,
    html[data-theme="dark"] .filter-grid fieldset,
    html[data-theme="dark"] .form-grid fieldset {{
      background: var(--surface-muted) !important;
      border-color: var(--border) !important;
      color: var(--text) !important;
    }}

    html[data-theme="dark"] .table-footer,
    html[data-theme="dark"] .table-page-container .table-footer,
    html[data-theme="dark"] .table-actions {{
      background: var(--surface) !important;
      border-color: var(--border) !important;
      color: var(--text) !important;
      box-shadow: var(--shadow-soft);
    }}

    html[data-theme="dark"] .table-footer .button,
    html[data-theme="dark"] .table-footer button,
    html[data-theme="dark"] .table-footer .table-utility-button,
    html[data-theme="dark"] .table-footer .column-settings > summary,
    html[data-theme="dark"] .table-actions .button,
    html[data-theme="dark"] .table-actions button {{
      background: var(--surface-muted) !important;
      border: 1px solid var(--border-strong) !important;
      color: var(--text-strong) !important;
      box-shadow: none;
    }}

    html[data-theme="dark"] .table-footer .button:hover,
    html[data-theme="dark"] .table-footer button:hover,
    html[data-theme="dark"] .table-footer .table-utility-button:hover,
    html[data-theme="dark"] .table-footer .column-settings > summary:hover,
    html[data-theme="dark"] .table-actions .button:hover,
    html[data-theme="dark"] .table-actions button:hover {{
      background: var(--accent-soft) !important;
      border-color: var(--accent) !important;
      color: var(--text-strong) !important;
    }}

    html[data-theme="dark"] .column-settings-panel,
    html[data-theme="dark"] .table-footer .column-settings-panel {{
      background: var(--surface) !important;
      border-color: var(--border-strong) !important;
      color: var(--text) !important;
      box-shadow: var(--shadow-card);
    }}

    html[data-theme="dark"] .activity-item,
    html[data-theme="dark"] .event-feed article,
    html[data-theme="dark"] .dashboard-feed article,
    html[data-theme="dark"] .activity-feed article,
    html[data-theme="dark"] .quick-copy {{
      background: var(--surface) !important;
      border-color: var(--border) !important;
      color: var(--text) !important;
    }}

    html[data-theme="dark"] .activity-item:hover,
    html[data-theme="dark"] .event-feed article:hover,
    html[data-theme="dark"] .dashboard-feed article:hover,
    html[data-theme="dark"] .activity-feed article:hover {{
      background: var(--accent-soft) !important;
      color: var(--text-strong) !important;
    }}

    .review-required-icon {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 16px;
      width: 16px;
      height: 16px;
      color: var(--warning);
      vertical-align: -2px;
    }}

    .review-required-icon .material-symbols-rounded {{ font-size: 18px; font-variation-settings: 'FILL' 1, 'wght' 500, 'GRAD' 0, 'opsz' 20; }}

    html[data-theme="light-v2"] ::selection {{ background: rgba(15, 118, 110, 0.28); color: #10201D; }}
    html[data-theme="light-v2"] * {{ scrollbar-color: #B7C8C1 #F3F7F4; }}
    html[data-theme="light-v2"] *::-webkit-scrollbar {{ width: 10px; height: 10px; }}
    html[data-theme="light-v2"] *::-webkit-scrollbar-track {{ background: var(--surface-muted); border-radius: 999px; }}
    html[data-theme="light-v2"] *::-webkit-scrollbar-thumb {{ background: #B7C8C1; border: 2px solid var(--surface-muted); border-radius: 999px; }}
    html[data-theme="light-v2"] *::-webkit-scrollbar-thumb:hover {{ background: #91AAA0; }}
    html[data-theme="light-v2"] .app-shell, html[data-theme="light-v2"] .content {{ background: var(--bg); }}
    html[data-theme="light-v2"] .breadcrumbs {{ background: var(--surface-soft, #EEF5F1); border-bottom-color: var(--border); color: var(--muted); }}
    html[data-theme="light-v2"] .breadcrumbs a {{ color: var(--accent-strong); }}
    html[data-theme="light-v2"] .sidebar {{ background: linear-gradient(180deg, #EAF3EF 0%, var(--sidebar-bg) 100%); border-right-color: var(--border-strong); }}
    html[data-theme="light-v2"] .brand-mark, html[data-theme="light-v2"] .user-icon {{ background: linear-gradient(135deg, var(--accent), var(--accent-strong)); box-shadow: 0 8px 18px rgba(15, 118, 110, .18); }}
    html[data-theme="light-v2"] .side-link {{ color: #243A34; border-left: 3px solid transparent; }}
    html[data-theme="light-v2"] .side-icon, html[data-theme="light-v2"] .nav-icon, html[data-theme="light-v2"] .side-link::before {{ color: #536C63 !important; }}
    html[data-theme="light-v2"] .side-link:hover, html[data-theme="light-v2"] .admin-link:hover {{ background: #F3FAF7; border-color: var(--accent-border); color: var(--accent-strong); }}
    html[data-theme="light-v2"] .side-link.active, html[data-theme="light-v2"] .sidebar-collapsed .side-link.active {{ background: var(--accent-soft) !important; border-color: var(--accent-border) !important; color: var(--accent-strong); box-shadow: inset 4px 0 0 var(--accent), 0 6px 16px rgba(15, 118, 110, .10); }}
    html[data-theme="light-v2"] .side-link.active .side-icon, html[data-theme="light-v2"] .side-link.active .nav-icon, html[data-theme="light-v2"] .side-link.active::before {{ color: var(--accent-strong) !important; }}
    html[data-theme="light-v2"] .side-link[href='/provider-changes'] .side-icon, html[data-theme="light-v2"] .side-link[href='/provider-changes'] .nav-icon {{ color: var(--warning) !important; }}
    html[data-theme="light-v2"] .side-link[href='/provider-changes'].active {{ background: var(--warning-soft) !important; border-color: var(--warning-border) !important; color: var(--warning-hover); box-shadow: inset 4px 0 0 var(--warning), 0 6px 16px rgba(217, 119, 6, .12); }}
    html[data-theme="light-v2"] .side-link-disabled, html[data-theme="light-v2"] .side-link-disabled:hover, html[data-theme="light-v2"] .side-link-disabled:disabled {{ color: var(--text-soft); opacity: .72; background: transparent; }}
    html[data-theme="light-v2"] .current-user-selector, html[data-theme="light-v2"] .theme-selector, html[data-theme="light-v2"] .sidebar-collapse {{ background: #F3FAF7; border-color: var(--border); color: var(--text); }}
    html[data-theme="light-v2"] .theme-menu, html[data-theme="light-v2"] .current-user-menu, html[data-theme="light-v2"] .column-settings-panel {{ background: var(--surface); border-color: var(--border-strong); box-shadow: var(--shadow-card); }}
    html[data-theme="light-v2"] .theme-menu button:hover, html[data-theme="light-v2"] .theme-menu button[aria-checked="true"], html[data-theme="light-v2"] .current-user-menu a:hover, html[data-theme="light-v2"] .column-settings-row:hover {{ background: var(--accent-soft); color: var(--accent-strong); }}
    html[data-theme="light-v2"] .card, html[data-theme="light-v2"] details, html[data-theme="light-v2"] fieldset, html[data-theme="light-v2"] .filter-card, html[data-theme="light-v2"] .form-card, html[data-theme="light-v2"] .table-footer, html[data-theme="light-v2"] .table-card, html[data-theme="light-v2"] .journal-card, html[data-theme="light-v2"] .dictionary-card, html[data-theme="light-v2"] .dictionary-toolbar, html[data-theme="light-v2"] .event-feed, html[data-theme="light-v2"] .metric-card, html[data-theme="light-v2"] .quick-link-card, html[data-theme="light-v2"] .login-card {{ background: var(--surface); border-color: var(--border); box-shadow: var(--shadow-card); }}
    html[data-theme="light-v2"] input, html[data-theme="light-v2"] select, html[data-theme="light-v2"] textarea {{ background: var(--input-bg); color: var(--text); border-color: var(--border-strong); }}
    html[data-theme="light-v2"] input:focus, html[data-theme="light-v2"] select:focus, html[data-theme="light-v2"] textarea:focus {{ border-color: var(--accent); outline-color: var(--accent); box-shadow: 0 0 0 3px rgba(15, 118, 110, .14); }}
    html[data-theme="light-v2"] input::placeholder, html[data-theme="light-v2"] textarea::placeholder {{ color: var(--text-soft); }}
    html[data-theme="light-v2"] th {{ background: #EAF3EF; color: #42574F; border-bottom-color: var(--border-strong); }}
    html[data-theme="light-v2"] th, html[data-theme="light-v2"] td {{ border-right-color: var(--border); }}
    html[data-theme="light-v2"] td {{ background: var(--surface); border-bottom-color: var(--border); color: var(--text); }}
    html[data-theme="light-v2"] tbody tr:nth-child(even) td {{ background: #F7FBF8; }}
    html[data-theme="light-v2"] tbody tr:hover td, html[data-theme="light-v2"] .selectable-cell:hover {{ background: #F0F8F5 !important; }}
    html[data-theme="light-v2"] form button[type="submit"], html[data-theme="light-v2"] .hero-action, html[data-theme="light-v2"] .modal-save, html[data-theme="light-v2"] .admin-edit-save {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    html[data-theme="light-v2"] form button[type="submit"]:hover, html[data-theme="light-v2"] .hero-action:hover, html[data-theme="light-v2"] .modal-save:hover, html[data-theme="light-v2"] .admin-edit-save:hover {{ background: var(--accent-hover); border-color: var(--accent-hover); color: #fff; }}
    html[data-theme="light-v2"] .metric-icon, html[data-theme="light-v2"] .quick-icon {{ background: var(--accent-soft); color: var(--accent-strong); box-shadow: 0 0 0 1px var(--accent-border) inset; }}
    html[data-theme="light-v2"] .metric-card.green .metric-icon, html[data-theme="light-v2"] .feed-icon.ok, html[data-theme="light-v2"] .dot-status.ok span {{ background: var(--accent); color: #fff; box-shadow: 0 0 0 5px var(--accent-soft); }}
    html[data-theme="light-v2"] .metric-card.orange .metric-icon, html[data-theme="light-v2"] .feed-icon.warn, html[data-theme="light-v2"] .dot-status.warning span {{ background: var(--provider-accent); color: #fff; box-shadow: 0 0 0 5px var(--provider-soft); }}
    html[data-theme="light-v2"] .dot-status.danger span, html[data-theme="light-v2"] .feed-icon.danger {{ background: var(--danger); box-shadow: 0 0 0 5px var(--danger-soft); }}
    html[data-theme="light-v2"] .status-badge, html[data-theme="light-v2"] .badge {{ border: 1px solid var(--border); background: var(--surface-muted); color: var(--text); border-radius: 999px; padding: 2px 8px; }}
    html[data-theme="light-v2"] .review-required-icon {{ color: var(--warning); }}

    html[data-theme="light-v2"] a:not(.side-link):not(.button) {{ color: var(--accent-strong); }}
    html[data-theme="light-v2"] a:not(.side-link):not(.button):hover {{ color: var(--accent-hover); }}
    html[data-theme="light-v2"] .card:hover, html[data-theme="light-v2"] .metric-card:hover, html[data-theme="light-v2"] .quick-link-card:hover {{ border-color: var(--accent-border); box-shadow: var(--shadow-card-hover); transform: translateY(-1px); }}
    html[data-theme="light-v2"] .quick-link-card[href='/provider-changes']:hover, html[data-theme="light-v2"] .quick-link-card[href='/provider-changes'] .quick-icon {{ background: var(--provider-soft); border-color: var(--warning-border); color: var(--warning-hover); }}
    html[data-theme="light-v2"] .quick-link-card[href='/provider-changes'] .quick-icon {{ box-shadow: 0 0 0 1px var(--warning-border) inset; }}
    html[data-theme="light-v2"] .metric-card.orange {{ border-color: var(--warning-border); }}
    html[data-theme="light-v2"] .sparkline polyline {{ stroke-width: 2.8; }}
    html[data-theme="light-v2"] .status-badge.warning, html[data-theme="light-v2"] .badge.warning, html[data-theme="light-v2"] .dot-status.warning {{ background: var(--warning-soft); border-color: var(--warning-border); color: var(--warning-hover); }}
    html[data-theme="light-v2"] .table-footer .button[aria-current="page"], html[data-theme="light-v2"] .pagination .active, html[data-theme="light-v2"] .button.active {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    html[data-theme="light-v2"] .modal-form-card[open]::before, html[data-theme="light-v2"] .modal-overlay {{ background: rgba(31, 41, 51, .42); }}
    html[data-theme="light-v2"] .modal-card, html[data-theme="light-v2"] .modal-form-card[open] > form, html[data-theme="light-v2"] .modal-form-card[open] > .modal-body {{ background: var(--surface); border-color: var(--border-strong); box-shadow: 0 24px 70px rgba(31, 41, 51, .22); }}
    html[data-theme="light-v2"] .button, html[data-theme="light-v2"] button {{ background: var(--surface); border-color: var(--border-strong); color: var(--text-strong); box-shadow: 0 1px 2px rgba(31,41,51,.05); }}
    html[data-theme="light-v2"] .button:hover, html[data-theme="light-v2"] button:hover {{ background: var(--accent-soft); border-color: var(--accent-border); color: var(--accent-strong); }}
    html[data-theme="light-v2"] form button[type="submit"], html[data-theme="light-v2"] .hero-action, html[data-theme="light-v2"] .modal-save, html[data-theme="light-v2"] .admin-edit-save {{ background: linear-gradient(135deg, var(--accent), var(--accent-strong)); border-color: var(--accent); color: #fff; box-shadow: 0 6px 14px rgba(15,118,110,.16); }}
    html[data-theme="light-v2"] .filter-grid button[type="submit"], html[data-theme="light-v2"] .filter-grid > button {{ background: var(--accent-soft); border-color: var(--accent-border); color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .filter-grid button[type="submit"]:hover, html[data-theme="light-v2"] .filter-grid > button:hover {{ background: #D2ECE6; border-color: var(--accent); color: var(--accent-strong); }}
    html[data-theme="light-v2"] .reset-filters, html[data-theme="light-v2"] .modal-cancel, html[data-theme="light-v2"] .admin-edit-cancel {{ background: var(--surface-muted); border-color: var(--border-strong); color: var(--text); box-shadow: none; }}
    html[data-theme="light-v2"] .reset-filters:hover, html[data-theme="light-v2"] .modal-cancel:hover, html[data-theme="light-v2"] .admin-edit-cancel:hover {{ background: var(--surface-strong); border-color: var(--border-strong); color: var(--text-strong); }}
    html[data-theme="light-v2"] .danger-action, html[data-theme="light-v2"] form[action$="/deactivate"] button, html[data-theme="light-v2"] button[onclick*="Удал"], html[data-theme="light-v2"] button[onclick*="Деактив"], html[data-theme="light-v2"] button[onclick*="Отключ"] {{ background: var(--danger-soft); border-color: var(--danger-border); color: var(--danger-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .filter-card {{ background: #F8FBF9; border-color: var(--border); box-shadow: 0 2px 8px rgba(31,41,51,.045); }}
    html[data-theme="light-v2"] .filter-summary {{ color: var(--muted); background: #F4F8F6; border-bottom: 1px solid transparent; }}
    html[data-theme="light-v2"] .filter-card[open] .filter-summary {{ border-bottom-color: var(--border); }}
    html[data-theme="light-v2"] .form-card {{ border-color: var(--border-strong); }}
    html[data-theme="light-v2"] th {{ background: var(--table-header-bg); color: #314A42; border-bottom-color: var(--border-strong); }}
    html[data-theme="light-v2"] th, html[data-theme="light-v2"] td {{ border-right-color: #DDE8E3; }}
    html[data-theme="light-v2"] td {{ border-bottom-color: #DDE8E3; }}
    html[data-theme="light-v2"] tbody tr:nth-child(even) td {{ background: var(--table-row-alt); }}
    html[data-theme="light-v2"] tbody tr:hover td, html[data-theme="light-v2"] .selectable-cell:hover {{ background: var(--table-row-hover) !important; }}
    html[data-theme="light-v2"] .copy-column-button, html[data-theme="light-v2"] .edit-action, html[data-theme="light-v2"] td[data-col="actions"] details.edit-details > summary {{ background: #F7FBF9; border-color: var(--border); color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .modal-form-card[open]::before, html[data-theme="light-v2"] .modal-overlay {{ background: rgba(31, 41, 51, .32); }}
    html[data-theme="light-v2"] .modal-card, html[data-theme="light-v2"] .modal-form-card[open] > form, html[data-theme="light-v2"] .modal-form-card[open] > .modal-body {{ background: #fff; border-color: var(--border-strong); box-shadow: 0 22px 60px rgba(31, 41, 51, .20); }}
    html[data-theme="light-v2"] .modal-card h2 {{ padding-bottom: 10px; border-bottom: 1px solid var(--border); }}
    html[data-theme="light-v2"] .modal-actions, html[data-theme="light-v2"] .admin-edit-actions {{ background: #F8FBF9; margin: 6px -20px -20px; padding: 12px 20px; border-top-color: var(--border); }}
    html[data-theme="light-v2"] .modal-card input, html[data-theme="light-v2"] .modal-card select, html[data-theme="light-v2"] .modal-card textarea, html[data-theme="light-v2"] .modal-form-card[open] input, html[data-theme="light-v2"] .modal-form-card[open] select, html[data-theme="light-v2"] .modal-form-card[open] textarea {{ border-color: var(--border-strong); }}
    html[data-theme="light-v2"] fieldset {{ border-color: var(--border-strong); background: #FBFDFC; }}
    html[data-theme="light-v2"] fieldset > legend {{ color: var(--accent-strong); font-weight: 820; }}
    html[data-theme="light-v2"] .checkbox-list label, html[data-theme="light-v2"] .permission-matrix label {{ padding: 4px 6px; border-radius: 8px; background: #F7FBF9; border: 1px solid var(--border); }}
    html[data-theme="light-v2"] .status-badge, html[data-theme="light-v2"] .badge {{ border: 1px solid var(--border-strong); background: var(--surface-muted); color: var(--text); border-radius: 999px; padding: 2px 8px; font-weight: 760; }}
    html[data-theme="light-v2"] .status-badge.ok, html[data-theme="light-v2"] .status-badge.success, html[data-theme="light-v2"] .badge.ok, html[data-theme="light-v2"] .badge.success {{ background: var(--success-soft); border-color: var(--success-border); color: var(--success); }}
    html[data-theme="light-v2"] .status-badge.warning, html[data-theme="light-v2"] .badge.warning, html[data-theme="light-v2"] .dot-status.warning {{ background: var(--warning-soft); border-color: var(--warning-border); color: var(--warning-hover); }}
    html[data-theme="light-v2"] .status-badge.danger, html[data-theme="light-v2"] .badge.danger {{ background: var(--danger-soft); border-color: var(--danger-border); color: var(--danger-strong); }}

    .connection-status {{ position: fixed; left: 50%; bottom: 16px; transform: translateX(-50%); z-index: 10001; display: none; align-items: center; gap: 10px; max-width: min(560px, calc(100vw - 24px)); padding: 10px 12px; border: 1px solid var(--border-strong); border-radius: 12px; background: var(--surface); color: var(--text-strong); box-shadow: var(--shadow-card); font-weight: 720; }}
    .connection-status.is-visible {{ display: flex; }}
    .connection-status.is-offline {{ border-color: var(--danger); background: var(--danger-soft); color: var(--danger); }}
    .connection-status.is-online {{ border-color: var(--accent); background: var(--success-soft); }}
    .connection-status button {{ min-height: 28px; padding: 4px 10px; }}
    .form-submit-error {{ flex-basis: 100%; border: 1px solid var(--danger); border-radius: 10px; background: var(--danger-soft); color: var(--danger); padding: 8px 10px; font-weight: 700; }}
  </style>
</head>
<body>
  <div class="connection-status" data-connection-status role="status" aria-live="polite">
    <span data-connection-message></span>
    <button type="button" data-connection-reload>Обновить</button>
  </div>
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

    const connectionStatus = document.querySelector("[data-connection-status]");
    const connectionMessage = document.querySelector("[data-connection-message]");
    const connectionReload = document.querySelector("[data-connection-reload]");
    function setConnectionStatus(state) {{
      if (!connectionStatus || !connectionMessage) return;
      connectionStatus.classList.remove("is-offline", "is-online", "is-visible");
      if (state === "offline") {{
        connectionMessage.textContent = "Нет соединения с интернетом. Проверьте подключение.";
        connectionStatus.classList.add("is-visible", "is-offline");
      }} else if (state === "online") {{
        connectionMessage.textContent = "Соединение восстановлено. Обновите страницу.";
        connectionStatus.classList.add("is-visible", "is-online");
      }}
    }}
    if (connectionReload) connectionReload.addEventListener("click", () => window.location.reload());
    window.addEventListener("offline", () => setConnectionStatus("offline"));
    window.addEventListener("online", () => setConnectionStatus("online"));
    if (navigator.onLine === false) setConnectionStatus("offline");

    function showFormSubmitError(form) {{
      let message = form.querySelector(".form-submit-error");
      if (!message) {{
        message = document.createElement("div");
        message.className = "form-submit-error";
        form.appendChild(message);
      }}
      message.textContent = "Не удалось отправить данные. Проверьте подключение и попробуйте ещё раз.";
    }}
    document.querySelectorAll("form").forEach((form) => {{
      form.addEventListener("submit", (event) => {{
        form.querySelectorAll(".form-submit-error").forEach((message) => message.remove());
        form.querySelectorAll("[data-submit-disabled-by-recovery]").forEach((element) => {{
          element.disabled = false;
          element.removeAttribute("data-submit-disabled-by-recovery");
        }});
        if (navigator.onLine === false) {{
          event.preventDefault();
          showFormSubmitError(form);
          return;
        }}
        const submitter = event.submitter;
        if (submitter && !submitter.disabled && submitter.matches('button[type="submit"], button:not([type]), input[type="submit"]')) {{
          submitter.disabled = true;
          submitter.setAttribute("data-submit-disabled-by-recovery", "true");
        }}
      }});
    }});
    window.addEventListener("pageshow", () => {{
      document.querySelectorAll("[data-submit-disabled-by-recovery]").forEach((element) => {{
        element.disabled = false;
        element.removeAttribute("data-submit-disabled-by-recovery");
      }});
    }});
    window.addEventListener("offline", () => {{
      document.querySelectorAll("[data-submit-disabled-by-recovery]").forEach((element) => {{
        element.disabled = false;
        element.removeAttribute("data-submit-disabled-by-recovery");
      }});
      document.querySelectorAll("form").forEach(showFormSubmitError);
    }});
    const themeLabels = {{ "mvp": "MVP", "dark": "Тёмная", "light-v2": "Светлая 2.0" }};
    const themeAliases = {{ "calm-blue": "mvp", "cyber-sketch": "dark", "terminal-paper": "mvp" }};
    const normalizeTheme = (theme) => themeAliases[theme] || (themeLabels[theme] ? theme : "mvp");
    let savedTheme = normalizeTheme(localStorage.getItem("mvp-theme") || "mvp");
    document.documentElement.dataset.theme = savedTheme;
    localStorage.setItem("mvp-theme", savedTheme);
    const updateThemeSelector = (theme) => {{
      const labelText = `Тема: ${{themeLabels[theme] || themeLabels.mvp}} ▾`;
      document.querySelectorAll("[data-theme-selector]").forEach((selector) => {{
        selector.dataset.tooltip = labelText.replace(" ▾", "");
        const current = selector.querySelector("[data-theme-current]");
        if (current) current.textContent = labelText;
        selector.querySelectorAll("[data-theme-option]").forEach((option) => {{
          option.setAttribute("aria-checked", option.dataset.themeOption === theme ? "true" : "false");
        }});
      }});
    }};
    updateThemeSelector(savedTheme);
    function expandSidebarIfCollapsed() {{
      if (!shell || !shell.classList.contains("sidebar-collapsed")) return false;
      shell.classList.remove("sidebar-collapsed");
      localStorage.setItem("mvp-sidebar-collapsed", "false");
      updateSidebarToggleLabels(false);
      return true;
    }}
    document.querySelectorAll("[data-theme-menu-toggle]").forEach((button) => {{
      button.addEventListener("click", (event) => {{
        event.stopPropagation();
        if (expandSidebarIfCollapsed()) {{
          button.setAttribute("aria-expanded", "false");
          return;
        }}
        const selector = button.closest("[data-theme-selector]");
        const isOpen = selector && selector.classList.toggle("open");
        button.setAttribute("aria-expanded", isOpen ? "true" : "false");
      }});
    }});
    document.querySelectorAll("[data-theme-option]").forEach((option) => {{
      option.addEventListener("click", (event) => {{
        event.stopPropagation();
        if (option.disabled) return;
        const theme = normalizeTheme(option.dataset.themeOption);
        document.documentElement.dataset.theme = theme;
        localStorage.setItem("mvp-theme", theme);
        updateThemeSelector(theme);
        document.querySelectorAll("[data-theme-selector]").forEach((selector) => selector.classList.remove("open"));
        document.querySelectorAll("[data-theme-menu-toggle]").forEach((toggle) => toggle.setAttribute("aria-expanded", "false"));
      }});
    }});
    document.addEventListener("click", () => {{
      document.querySelectorAll("[data-theme-selector]").forEach((selector) => selector.classList.remove("open"));
      document.querySelectorAll("[data-theme-menu-toggle]").forEach((toggle) => toggle.setAttribute("aria-expanded", "false"));
    }});

    const shell = document.querySelector(".app-shell");
    const savedSidebar = localStorage.getItem("mvp-sidebar-collapsed") === "true";
    if (shell) shell.classList.toggle("sidebar-collapsed", savedSidebar);
    function updateSidebarToggleLabels(collapsed) {{
      document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {{
        const sidebarAction = collapsed ? "Развернуть боковую панель" : "Свернуть боковую панель";
        button.dataset.tooltip = collapsed ? "Развернуть" : "Свернуть";
        button.setAttribute("aria-label", sidebarAction);
        button.title = sidebarAction;
      }});
    }}
    updateSidebarToggleLabels(savedSidebar);
    document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {{
      button.addEventListener("click", () => {{
        if (!shell) return;
        const collapsed = !shell.classList.contains("sidebar-collapsed");
        shell.classList.toggle("sidebar-collapsed", collapsed);
        localStorage.setItem("mvp-sidebar-collapsed", collapsed ? "true" : "false");
        updateSidebarToggleLabels(collapsed);
      }});
    }});
    document.querySelectorAll(".current-user-selector").forEach((selector) => {{
      selector.addEventListener("click", (event) => {{
        if (!expandSidebarIfCollapsed()) return;
        event.preventDefault();
        event.stopPropagation();
      }}, true);
    }});
    document.querySelectorAll(".admin-toggle").forEach((button) => {{
      button.addEventListener("click", () => {{
        const target = document.getElementById(button.getAttribute("aria-controls"));
        if (!target) return;
        const wasCollapsed = shell && shell.classList.contains("sidebar-collapsed");
        if (shell && shell.classList.contains("sidebar-collapsed")) {{
          shell.classList.remove("sidebar-collapsed");
          localStorage.setItem("mvp-sidebar-collapsed", "false");
          updateSidebarToggleLabels(false);
        }}
        const expanded = button.getAttribute("aria-expanded") === "true";
        const nextExpanded = wasCollapsed ? true : !expanded;
        button.setAttribute("aria-expanded", nextExpanded ? "true" : "false");
        target.classList.toggle("open", nextExpanded);
      }});
    }});
    function enhanceModalForm(form, closeCallback) {{
      if (!form || form.dataset.modalEnhanced === "1") return;
      form.dataset.modalEnhanced = "1";
      const saveButton = form.querySelector('button[type="submit"], button:not([type]), input[type="submit"]');
      const actions = document.createElement("div");
      actions.className = "modal-actions";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "modal-cancel";
      cancel.textContent = "Отмена";
      if (saveButton) {{
        saveButton.parentNode.insertBefore(actions, saveButton);
        saveButton.classList.add("modal-save");
        actions.appendChild(cancel);
        actions.appendChild(saveButton);
      }} else {{
        form.appendChild(actions);
        actions.appendChild(cancel);
      }}
      cancel.addEventListener("click", closeCallback);
    }}
    const modalDetails = Array.from(document.querySelectorAll("details.modal-form-card[data-modal-details]"));
    modalDetails.forEach((details) => {{
      const form = details.querySelector("form");
      enhanceModalForm(form, () => details.removeAttribute("open"));
      details.addEventListener("click", (event) => {{
        if (details.open && event.target === details) details.removeAttribute("open");
      }});
    }});
    document.addEventListener("keydown", (event) => {{
      if (event.key !== "Escape") return;
      const openDetails = modalDetails.find((details) => details.open);
      if (openDetails) {{
        openDetails.removeAttribute("open");
        event.preventDefault();
      }}
    }});
    function titleFromEditHref(href) {{
      if (href.includes("/routes/")) return "Редактировать маршрут";
      if (href.includes("/tariffs/")) return "Редактировать тариф";
      if (href.includes("/phones/")) return "Редактировать номер";
      if (href.includes("/companies/")) return "Редактировать кампанию";
      if (href.includes("/provider-changes/")) return "Редактировать событие";
      if (href.includes("/admin/users/")) return "Редактировать пользователя";
      return "Редактировать";
    }}
    function closeRemoteModal() {{
      document.querySelectorAll(".modal-overlay, .modal-card[data-remote-modal]").forEach((node) => node.remove());
    }}
    document.addEventListener("click", async (event) => {{
      const link = event.target.closest("a.edit-action");
      if (!link || !link.href) return;
      event.preventDefault();
      closeRemoteModal();
      try {{
        const response = await fetch(link.href, {{ headers: {{ "X-Requested-With": "fetch" }} }});
        const text = await response.text();
        const doc = new DOMParser().parseFromString(text, "text/html");
        const form = doc.querySelector("form[action*='/update']");
        if (!response.ok || !form) {{
          window.location.href = link.href;
          return;
        }}
        const overlay = document.createElement("div");
        overlay.className = "modal-overlay";
        const card = document.createElement("section");
        card.className = "modal-card";
        card.dataset.remoteModal = "1";
        card.innerHTML = `<h2>${{titleFromEditHref(link.getAttribute("href") || "")}}</h2><p class="modal-description">Поля и сохранение работают как раньше.</p>`;
        const importedForm = document.importNode(form, true);
        card.appendChild(importedForm);
        document.body.appendChild(overlay);
        document.body.appendChild(card);
        Array.from(doc.querySelectorAll("script")).forEach((script) => {{
          if (!script.src && script.textContent.trim()) {{
            const next = document.createElement("script");
            next.textContent = script.textContent;
            card.appendChild(next);
          }}
        }});
        enhanceModalForm(importedForm, closeRemoteModal);
        overlay.addEventListener("click", closeRemoteModal);
      }} catch (error) {{
        window.location.href = link.href;
      }}
    }});
    document.addEventListener("keydown", (event) => {{
      if (event.key === "Escape" && document.querySelector(".modal-card[data-remote-modal]")) {{
        closeRemoteModal();
        event.preventDefault();
      }}
    }});
    document.querySelectorAll('td[data-col="actions"] details > summary').forEach((summary) => {{
      const label = (summary.textContent || '').trim() || 'Редактировать';
      if (!summary.title) summary.title = label;
      if (!summary.getAttribute('aria-label')) summary.setAttribute('aria-label', label);
    }});
    const adminEditDetails = Array.from(document.querySelectorAll('td[data-col="actions"] details.edit-details'));
    function closeAdminEdit(except) {{
      adminEditDetails.forEach((details) => {{
        if (details !== except) details.removeAttribute("open");
      }});
    }}
    adminEditDetails.forEach((details) => {{
      const form = details.querySelector("form");
      if (form && !form.querySelector(".admin-edit-cancel")) {{
        const saveButton = form.querySelector('button[type="submit"], button:not([type]), input[type="submit"]');
        const actions = document.createElement("div");
        actions.className = "admin-edit-actions";
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "admin-edit-cancel";
        cancel.textContent = "Отмена";
        if (saveButton) {{
          saveButton.parentNode.insertBefore(actions, saveButton);
          saveButton.classList.add("admin-edit-save");
          actions.appendChild(saveButton);
        }} else {{
          form.appendChild(actions);
        }}
        actions.appendChild(cancel);
        cancel.addEventListener("click", () => details.removeAttribute("open"));
      }}
      details.addEventListener("toggle", () => {{
        if (details.open) closeAdminEdit(details);
      }});
    }});
    document.addEventListener("click", (event) => {{
      const openDetails = adminEditDetails.find((details) => details.open);
      if (!openDetails) return;
      if (openDetails.contains(event.target)) return;
      openDetails.removeAttribute("open");
    }});
    document.addEventListener("keydown", (event) => {{
      if (event.key !== "Escape") return;
      const openDetails = adminEditDetails.find((details) => details.open);
      if (!openDetails) return;
      openDetails.removeAttribute("open");
      event.preventDefault();
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
    const copySuccessIcon = {COPY_SUCCESS_ICON_JS};
    document.querySelectorAll("[data-copy-action]").forEach((button) => {{
      const defaultIcon = button.innerHTML;
      const defaultTitle = button.title;
      let successTimer = null;
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
        button.innerHTML = copySuccessIcon;
        button.title = "Скопировано";
        button.setAttribute("aria-label", "Скопировано");
        if (successTimer) window.clearTimeout(successTimer);
        successTimer = window.setTimeout(() => {{
          button.innerHTML = defaultIcon;
          button.title = defaultTitle;
          button.setAttribute("aria-label", defaultTitle);
          successTimer = null;
        }}, 1500);
      }});
    }});
    (() => {{
      let lastTarget = null;
      let lastAt = 0;
      const repeatMs = 1000;
      function selectNodeText(node) {{
        const selection = window.getSelection && window.getSelection();
        if (!selection || !node) return;
        const range = document.createRange();
        range.selectNodeContents(node);
        selection.removeAllRanges();
        selection.addRange(range);
      }}
      document.addEventListener("dblclick", (event) => {{
        const target = event.target.closest("[data-select-value]");
        if (!target) return;
        const now = Date.now();
        if (target === lastTarget && now - lastAt <= repeatMs) {{
          event.preventDefault();
          event.stopPropagation();
          selectNodeText(target);
        }}
        lastTarget = target;
        lastAt = now;
      }});
    }})();
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
      const storageKey = settings.dataset.storageKey || `teleRoute.table.${{tableKey}}`;
      const tables = Array.from(document.querySelectorAll(`[data-table-key="${{tableKey}}"]`));
      if (!tables.length) return;
      const list = settings.querySelector("[data-column-settings-list]");
      const defaults = Array.from(settings.querySelectorAll("[data-col-row]")).map((row) => ({{
        key: row.dataset.colRow,
        locked: row.dataset.locked === "true",
        visible: row.dataset.locked === "true" ? true : row.querySelector("[data-col-toggle]")?.checked !== false,
      }}));
      const defaultOrder = defaults.map((column) => column.key);
      const lockedColumns = new Set(defaults.filter((column) => column.locked).map((column) => column.key));
      const minWidth = 72;
      let state = defaultState();

      function defaultState() {{
        return {{
          visible: Object.fromEntries(defaults.map((column) => [column.key, true])),
          order: defaultOrder.slice(),
          widths: {{}},
        }};
      }}
      function normalizePrefs(prefs) {{
        const base = defaultState();
        const incomingOrder = Array.isArray(prefs?.order) ? prefs.order.filter((key) => defaultOrder.includes(key)) : [];
        base.order = incomingOrder.concat(defaultOrder.filter((key) => !incomingOrder.includes(key)));
        if (prefs?.visible && typeof prefs.visible === "object") {{
          defaultOrder.forEach((key) => {{ base.visible[key] = prefs.visible[key] !== false; }});
        }}
        lockedColumns.forEach((key) => {{ base.visible[key] = true; }});
        if (!defaultOrder.some((key) => !lockedColumns.has(key) && base.visible[key])) {{
          const first = defaultOrder.find((key) => !lockedColumns.has(key));
          if (first) base.visible[first] = true;
        }}
        if (prefs?.widths && typeof prefs.widths === "object") {{
          defaultOrder.forEach((key) => {{
            const width = Number.parseInt(prefs.widths[key], 10);
            if (Number.isFinite(width) && width >= minWidth) base.widths[key] = width;
          }});
        }}
        return base;
      }}
      function loadPrefs() {{
        try {{
          const raw = window.localStorage && window.localStorage.getItem(storageKey);
          return raw ? JSON.parse(raw) : null;
        }} catch (error) {{ return null; }}
      }}
      function savePrefs() {{
        try {{ if (window.localStorage) window.localStorage.setItem(storageKey, JSON.stringify(state)); }} catch (error) {{}}
      }}
      function clearPrefs() {{
        try {{ if (window.localStorage) window.localStorage.removeItem(storageKey); }} catch (error) {{}}
      }}
      function applyColumnOrder(table) {{
        table.querySelectorAll("tr").forEach((row) => {{
          state.order.forEach((key) => {{
            const cell = row.querySelector(`:scope > [data-col="${{key}}"]`);
            if (cell) row.appendChild(cell);
          }});
        }});
      }}
      function applyState(persist = true) {{
        tables.forEach((table) => {{
          applyColumnOrder(table);
          defaultOrder.forEach((key) => {{
            const width = state.widths[key];
            table.querySelectorAll(`[data-col="${{key}}"]`).forEach((cell) => {{
              cell.dataset.columnHidden = state.visible[key] ? "false" : "true";
              if (width) {{
                cell.style.width = `${{width}}px`;
                cell.style.minWidth = `${{width}}px`;
                cell.style.maxWidth = `${{width}}px`;
              }} else {{
                cell.style.width = "";
                cell.style.minWidth = "";
                cell.style.maxWidth = "";
              }}
            }});
          }});
        }});
        renderPanel();
        if (persist) savePrefs();
      }}
      function renderPanel() {{
        if (!list) return;
        state.order.forEach((key, index) => {{
          const row = list.querySelector(`[data-col-row="${{key}}"]`);
          if (!row) return;
          list.appendChild(row);
          const checkbox = row.querySelector("[data-col-toggle]");
          if (checkbox) {{
            checkbox.checked = state.visible[key] !== false;
            checkbox.disabled = lockedColumns.has(key);
          }}
          row.querySelectorAll("[data-column-move]").forEach((button) => {{
            const dir = button.dataset.columnMove;
            button.disabled = (dir === "up" && index === 0) || (dir === "down" && index >= state.order.length - 1);
          }});
        }});
      }}
      function moveColumn(key, direction) {{
        const index = state.order.indexOf(key);
        const next = direction === "up" ? index - 1 : index + 1;
        if (index < 0 || next < 0 || next >= state.order.length) return;
        [state.order[index], state.order[next]] = [state.order[next], state.order[index]];
        applyState(true);
      }}
      function setVisible(key, visible) {{
        if (lockedColumns.has(key)) return;
        if (!visible && !defaultOrder.some((column) => column !== key && !lockedColumns.has(column) && state.visible[column])) {{
          state.visible[key] = true;
          renderPanel();
          return;
        }}
        state.visible[key] = visible;
        applyState(true);
      }}
      function addResizeHandles() {{
        tables.forEach((table) => table.querySelectorAll("th[data-col]").forEach((th) => {{
          const key = th.dataset.col;
          if (lockedColumns.has(key) || th.querySelector(".column-resize-handle")) return;
          th.classList.add("resizable-header");
          const handle = document.createElement("span");
          handle.className = "column-resize-handle";
          handle.setAttribute("role", "separator");
          handle.setAttribute("aria-label", "Изменить ширину колонки");
          handle.addEventListener("click", (event) => event.stopPropagation());
          handle.addEventListener("mousedown", (event) => {{
            event.preventDefault();
            event.stopPropagation();
            const startX = event.clientX;
            const startWidth = th.getBoundingClientRect().width;
            document.body.classList.add("is-resizing-column");
            const onMove = (moveEvent) => {{
              state.widths[key] = Math.max(minWidth, Math.round(startWidth + moveEvent.clientX - startX));
              applyState(false);
            }};
            const onUp = () => {{
              document.removeEventListener("mousemove", onMove);
              document.removeEventListener("mouseup", onUp);
              document.body.classList.remove("is-resizing-column");
              savePrefs();
            }};
            document.addEventListener("mousemove", onMove);
            document.addEventListener("mouseup", onUp);
          }});
          th.appendChild(handle);
        }}));
      }}
      settings.addEventListener("change", (event) => {{
        const checkbox = event.target.closest("[data-col-toggle]");
        if (checkbox) setVisible(checkbox.dataset.colToggle, checkbox.checked);
      }});
      settings.addEventListener("click", (event) => {{
        const mover = event.target.closest("[data-column-move]");
        if (mover) {{
          moveColumn(mover.closest("[data-col-row]").dataset.colRow, mover.dataset.columnMove);
          return;
        }}
        if (event.target.closest("[data-column-reset]")) {{
          clearPrefs();
          state = defaultState();
          applyState(false);
        }}
      }});
      state = normalizePrefs(loadPrefs());
      addResizeHandles();
      applyState(false);
    }});
  </script>
  <script src="https://cdn.jsdelivr.net/npm/@tabler/core@1.0.0-beta20/dist/js/tabler.min.js"></script>
</body>
</html>""".encode("utf-8")

def no_store_headers() -> list[tuple[str, str]]:
    return [
        ("Cache-Control", "no-store, max-age=0"),
        ("Pragma", "no-cache"),
        ("Expires", "0"),
    ]


def html_headers() -> list[tuple[str, str]]:
    return [("Content-Type", "text/html; charset=utf-8"), *no_store_headers()]


def csv_headers(filename: str) -> list[tuple[str, str]]:
    return [("Content-Type", "text/csv; charset=utf-8"), ("Content-Disposition", f"attachment; filename={filename}"), *no_store_headers()]


def redirect(start_response, location: str, headers: list[tuple[str, str]] | None = None):
    response_headers = [("Location", location), *no_store_headers()]
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
        value, sig = morsel.value.split(".", 1)
        expected = sign_user_id(int(value)).split(".", 1)[1]
    except (ValueError, IndexError):
        return None
    return int(value) if hmac.compare_digest(sig, expected) else None

def resolve_current_user_id(repo: Repository, requested_id: int | None = None) -> int | None:
    if requested_id is None:
        return None
    user = repo.get_user(requested_id)
    if user and user["is_active"]:
        return int(user["id"])
    return None


def current_request_path(environ) -> str:
    path = environ.get("PATH_INFO", "/") or "/"
    query = environ.get("QUERY_STRING", "")
    return f"{path}?{query}" if query else path


def safe_redirect_target(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def current_actor_id() -> int:
    return int(_REQUEST_CONTEXT.get("current_user_id") or 0)


def clear_current_user_cookie() -> tuple[str, str]:
    return ("Set-Cookie", f"{CURRENT_USER_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")


def is_public_path(path: str) -> bool:
    return path in {"/login", "/logout", "/change-password", "/health", "/check"} or path.startswith("/static/")


def login_page(repo: Repository, message: str | None = None, notice_type: str = "error") -> bytes:
    notice_class = "login-ok" if notice_type == "success" else "login-error"
    notice = f"<div class='{notice_class}'>{esc(message)}</div>" if message else ""
    html = f"""<!doctype html>
<html lang='ru' data-theme='mvp'>
<head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>Вход · TeleRoute</title><style>body{{font-family:Arial,sans-serif;background:#eef2f7;color:#172554}}.login-body{{min-height:100vh;display:grid;place-items:center;padding:24px}}.login-card{{width:min(420px,100%);padding:28px;border:1px solid #e3eaf7;border-radius:18px;background:white;box-shadow:0 10px 24px rgba(32,50,90,.08)}}.brand-block{{display:flex;gap:12px;align-items:center}}.brand-mark{{display:grid;place-items:center;width:36px;height:36px;border-radius:11px;background:#4f46e5;color:white;font-weight:900}}.brand-copy strong,.brand-copy span{{display:block}}.brand-copy span,.muted{{color:#7180a4}}.login-form{{display:grid;gap:14px;margin-top:18px}}label{{display:grid;gap:6px;font-weight:700}}input{{border:1px solid #cdd6e8;border-radius:10px;padding:10px 12px;font:inherit}}.button{{border:0;border-radius:10px;background:#2547e8;color:white;font-weight:800;padding:10px 18px}}.login-error,.login-ok{{padding:10px 12px;border-radius:10px;font-weight:700}}.login-error{{background:#fff0f0;color:#b42318}}.login-ok{{background:#ecfdf3;color:#027a48}}</style></head>
<body class='login-body'>
  <main class='login-shell'>
    <section class='login-card'>
      <div class='brand-block'><div class='brand-mark'>⌁</div><div class='brand-copy'><strong>TeleRoute</strong><span>Вход в систему</span></div></div>
      <h1>Вход</h1>
      {notice}
      <form method='post' action='/login' class='login-form'>
        <label>Логин <input name='username' autocomplete='username' required autofocus></label>
        <label>Пароль <input name='password' type='password' autocomplete='current-password' required></label>
        <button class='button hero-action' type='submit'>Войти</button>
      </form>
    </section>
  </main>
</body>
</html>"""
    return html.encode("utf-8")


def change_password_page(message: str | None = None) -> bytes:
    notice = f"<div class='login-error'>{esc(message)}</div>" if message else ""
    html = f"""<!doctype html><html lang='ru' data-theme='mvp'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>Смена пароля · TeleRoute</title><style>body{{font-family:Arial,sans-serif;background:#eef2f7;color:#172554}}.login-body{{min-height:100vh;display:grid;place-items:center;padding:24px}}.login-card{{width:min(420px,100%);padding:28px;border:1px solid #e3eaf7;border-radius:18px;background:white;box-shadow:0 10px 24px rgba(32,50,90,.08)}}.login-form{{display:grid;gap:14px;margin-top:18px}}label{{display:grid;gap:6px;font-weight:700}}input{{border:1px solid #cdd6e8;border-radius:10px;padding:10px 12px;font:inherit}}.button{{border:0;border-radius:10px;background:#2547e8;color:white;font-weight:800;padding:10px 18px}}.login-error,.login-ok{{padding:10px 12px;border-radius:10px;font-weight:700}}.login-error{{background:#fff0f0;color:#b42318}}.login-ok{{background:#ecfdf3;color:#027a48}}</style></head><body class='login-body'><main class='login-card'><h1>Смена пароля</h1><p>Задайте новый пароль для продолжения работы.</p>{notice}<form method='post' action='/change-password' class='login-form'><label>Новый пароль <input name='password' type='password' autocomplete='new-password' required></label><label>Повтор нового пароля <input name='password_confirm' type='password' autocomplete='new-password' required></label><button class='button' type='submit'>Сохранить пароль</button></form><p><a href='/logout'>Выйти</a></p></main></body></html>"""
    return html.encode("utf-8")

def section_for_get_path(path: str) -> str | None:
    if path in {"/", "/dashboard"}:
        return "dashboard"
    if path == "/routes" or (path.startswith("/routes/") and (path.endswith("/numbers") or path.endswith("/numbers/manage") or path.endswith("/history"))):
        return "routes"
    if path == "/tariffs" or (path.startswith("/tariffs/") and (path.endswith("/history") or path.endswith("/edit"))):
        return "tariffs"
    if path == "/phones" or (path.startswith("/phones/") and path.endswith("/history")):
        return "phones"
    if path == "/companies" or path == "/calling-companies/history" or (path.startswith("/calling-companies/") and path.endswith("/history")) or (path.startswith("/companies/") and path.endswith("/history")):
        return "companies"
    if path == "/provider-changes":
        return "provider_changes"
    if path.startswith("/campaign-routing/") and path.endswith("/history"):
        return "admin_company_routing_settings"
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
    if section and not has_permission(None, section, action):
        raise ForbiddenError()

def request_query(environ) -> dict[str, str]:
    return {key: values[-1] for key, values in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}


def is_meaningful_filter_value(section: str | None, key: str, value: object) -> bool:
    if value in (None, ""):
        return False
    text = str(value)
    if text == "all":
        return False
    if section and FILTER_DEFAULT_VALUES.get(section, {}).get(key) == text:
        return False
    return True


def meaningful_filters(section: str | None, q: dict[str, str], keys: list[str] | tuple[str, ...]) -> dict[str, str]:
    return {key: q.get(key, "") for key in keys if is_meaningful_filter_value(section, key, q.get(key))}


def active_query(q: dict[str, str], keys: list[str] | tuple[str, ...]) -> bool:
    path = _REQUEST_CONTEXT.get("path")
    section = FILTER_SECTIONS.get(str(path), (None, ()))[0]
    return bool(meaningful_filters(section, q, keys))


def load_filter_state(environ) -> dict[str, dict[str, str]]:
    cookie = SimpleCookie()
    cookie.load(environ.get("HTTP_COOKIE", ""))
    morsel = cookie.get(FILTER_STATE_COOKIE)
    if not morsel:
        return {}
    try:
        data = json.loads(unquote(morsel.value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    state: dict[str, dict[str, str]] = {}
    for section, filters in data.items():
        if isinstance(section, str) and isinstance(filters, dict):
            state[section] = {str(key): str(value) for key, value in filters.items() if is_meaningful_filter_value(section, str(key), value)}
    return state


def filter_state_cookie(state: dict[str, dict[str, str]]) -> tuple[str, str]:
    value = quote(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
    return ("Set-Cookie", f"{FILTER_STATE_COOKIE}={value}; Path=/; SameSite=Lax")


def saved_filter_redirect(path: str, q: dict[str, str], state: dict[str, dict[str, str]]) -> str | None:
    config = FILTER_SECTIONS.get(path)
    if not config or q:
        return None
    section, _ = config
    saved = {key: value for key, value in state.get(section, {}).items() if is_meaningful_filter_value(section, key, value)}
    if not saved:
        return None
    saved["_filters_restored"] = "1"
    return path + "?" + urlencode(saved)


def update_filter_state_for_request(path: str, q: dict[str, str], state: dict[str, dict[str, str]]) -> tuple[dict[str, dict[str, str]], tuple[str, str] | None]:
    config = FILTER_SECTIONS.get(path)
    if not config:
        return state, None
    section, keys = config
    updated = dict(state)
    if q.get("reset_filters") == "1":
        updated.pop(section, None)
        return updated, filter_state_cookie(updated)
    if q.get("export") == "csv":
        return state, None
    submitted_filters = meaningful_filters(section, q, keys)
    has_user_query = any(key not in FILTER_CONTROL_KEYS for key in q)
    if has_user_query:
        if submitted_filters:
            updated[section] = dict(submitted_filters)
        else:
            updated.pop(section, None)
        return updated, filter_state_cookie(updated)
    return state, None


def filter_card(form_html: str, q: dict[str, str], keys: list[str] | tuple[str, ...]) -> str:
    path = _REQUEST_CONTEXT.get("path")
    section = FILTER_SECTIONS.get(str(path), (None, ()))[0]
    is_open = active_query(q, keys) or q.get(FILTER_OPEN_KEY) == "1" or q.get("reset_filters") == "1"
    open_attr = " open" if is_open else ""
    action_match = re.search(r'action=["\']([^"\']+)["\']', form_html)
    reset_href = action_match.group(1) if action_match else current_request_path({"PATH_INFO": "/", "QUERY_STRING": ""})
    reset_href = f"{reset_href}?reset_filters=1"
    reset_link = f"<a class='button reset-filters' href='{esc(reset_href)}'>Сбросить фильтры</a>"
    hidden_open = f'<input type="hidden" name="{FILTER_OPEN_KEY}" value="{'1' if is_open else '0'}" data-filters-open-field>'
    if "</form>" in form_html:
        form_html = form_html.replace("</form>", hidden_open + reset_link + "</form>", 1)
    else:
        form_html += reset_link
    script = "<script>(function(d){var f=d.querySelector('[data-filters-open-field]');if(!f)return;function sync(){f.value=d.open?'1':'0';}d.addEventListener('toggle',sync);var form=d.querySelector('form');if(form)form.addEventListener('submit',sync);})(document.currentScript.previousElementSibling);</script>"
    return f"<details class='filter-card'{open_attr}><summary class='filter-summary'>Фильтры</summary>{form_html}</details>{script}"


def form_card(summary: str, form_html: str, *, open_by_default: bool = False) -> str:
    open_attr = " open" if open_by_default else ""
    return f"<details class='form-card modal-form-card'{open_attr} data-modal-details><summary class='form-summary'>{summary}</summary>{form_html}</details>"


def table_page_container(inner_html: str) -> str:
    return f"<div class='table-page-container'>{inner_html}</div>"


def table_card(table_html: str, *, title: str | None = None, extra_class: str = "") -> str:
    title_html = f"<h2>{esc(title)}</h2>" if title else ""
    classes = f"table-card {extra_class}".strip()
    return f"<section class='{classes}'>{title_html}<div class='table-scroll'>{table_html}</div></section>"


def table_storage_key(table_key: str) -> str:
    return {
        "phones": "teleRoute.table.phones",
        "routes": "teleRoute.table.routes",
        "tariffs": "teleRoute.table.tariffs",
        "companies": "teleRoute.table.companies",
        "provider_changes": "teleRoute.table.providerChanges",
    }.get(table_key, f"teleRoute.table.{table_key}")


def column_settings(table_key: str, columns: list[tuple[str, str]]) -> str:
    rows = []
    for key, label in columns:
        locked = key == "actions"
        disabled = " disabled" if locked else ""
        locked_attr = " data-locked='true'" if locked else ""
        locked_class = " is-locked" if locked else ""
        lock_hint = "Системная колонка всегда видима" if locked else ""
        rows.append(
            f"<div class='column-settings-row{locked_class}' data-col-row='{esc(key)}'{locked_attr}>"
            f"<label title='{esc(lock_hint)}'><input type='checkbox' data-col-toggle='{esc(key)}' checked{disabled}> {esc(label)}</label>"
            "<span class='column-order-controls'>"
            "<button type='button' class='column-order-button' data-column-move='up' title='Выше' aria-label='Переместить выше'>↑</button>"
            "<button type='button' class='column-order-button' data-column-move='down' title='Ниже' aria-label='Переместить ниже'>↓</button>"
            "</span></div>"
        )
    return f"""<details class='column-settings' data-column-settings='{esc(table_key)}' data-storage-key='{esc(table_storage_key(table_key))}'>
<summary>Колонки</summary>
<div class='column-settings-panel'><div class='column-settings-list' data-column-settings-list>{''.join(rows)}</div><button type='button' class='column-reset' data-column-reset title='Сбросить колонки'>Сбросить вид таблицы</button></div>
</details>"""


def table_footer(summary_html: str, utility_html: str) -> str:
    return f"<div class='table-footer'><div class='table-footer-summary'>{summary_html}</div><div class='table-footer-tools'>{utility_html}</div></div>"

def data_table(table_key: str, columns: list[tuple[str, str]], rows_html: str) -> str:
    header = "".join(
        f"<th data-col='{esc(key)}' title='{plain_title(label)}'>{label}</th>"
        for key, label in columns
    )
    return f"<table class='data-table' data-table-key='{esc(table_key)}'><thead><tr>{header}</tr></thead><tbody>{rows_html}</tbody></table>"


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
    selected_text = "" if selected is None else str(selected)
    opts = f"<option value='' {'selected' if selected_text == '' else ''}>{esc(empty)}</option>" if empty is not None else ""
    if empty == "Все":
        opts += f"<option value='__none__' {'selected' if selected_text == '__none__' else ''}>Без префикса</option>"
    rows = repo.conn.execute(
        """
        SELECT pp.id, pp.prefix AS label
        FROM provider_prefixes pp JOIN providers p ON p.id = pp.provider_id
        WHERE (pp.is_active = 1 OR pp.id = ?)
          AND pp.prefix IS NOT NULL AND TRIM(pp.prefix) != ''
          AND TRIM(pp.prefix) NOT IN ('Без префикса', 'без префикса', 'no prefix', '—', '-')
        ORDER BY pp.prefix, p.name
        """,
        (selected or 0,),
    )
    for row in rows:
        opts += f"<option value='{row['id']}' {'selected' if str(row['id']) == selected_text else ''}>{esc(row['label'])}</option>"
    return opts


def phone_type_options(repo: Repository, selected: str | None = None, empty: str | None = None) -> str:
    opts = f"<option value=''>{esc(empty)}</option>" if empty is not None else ""
    for row in repo.conn.execute("SELECT name FROM phone_number_types WHERE is_active = 1 OR name = ? ORDER BY name", (selected or "",)):
        opts += f"<option value='{esc(row['name'])}' {'selected' if row['name'] == selected else ''}>{esc(row['name'])}</option>"
    return opts


def project_options(repo: Repository, selected: str | None = None, empty: str | None = None) -> str:
    opts = f"<option value=''>{esc(empty)}</option>" if empty is not None else ""
    for row in repo.conn.execute("SELECT name FROM projects WHERE is_active = 1 OR name = ? ORDER BY sort_order, name", (selected or "",)):
        opts += f"<option value='{esc(row['name'])}' {'selected' if row['name'] == selected else ''}>{esc(row['name'])}</option>"
    return opts


def assignment_options(repo: Repository, selected: str | None = None, empty: str | None = None) -> str:
    opts = f"<option value=''>{esc(empty)}</option>" if empty is not None else ""
    for row in repo.conn.execute("SELECT code, name FROM phone_assignment_types WHERE is_active = 1 OR code = ? ORDER BY sort_order, name", (selected or "",)):
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
        repo.conn.execute("UPDATE projects SET is_active = 0 WHERE name IN ('Междепы', 'Competitors', 'ITM', 'Monitoring', 'Test')")
        for code, name, sort_order, include_in_route_name in DEFAULT_PROJECTS:
            repo.conn.execute(
                """
                INSERT INTO projects(code, name, is_active, sort_order, include_in_route_name)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(name) DO UPDATE SET code = excluded.code, is_active = 1,
                    sort_order = excluded.sort_order, include_in_route_name = excluded.include_in_route_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (code, name, sort_order, include_in_route_name),
            )
        repo.conn.execute(
            "DELETE FROM phone_assignment_types WHERE code IN ('outgoing_cli', 'inbound_line', 'office_phone', 'sim_card', 'pool_number', 'other')"
        )
        for code, name, sort_order in DEFAULT_PHONE_ASSIGNMENTS:
            repo.conn.execute(
                """
                INSERT INTO phone_assignment_types(code, name, is_active, sort_order)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(code) DO UPDATE SET name = excluded.name, is_active = 1,
                    sort_order = excluded.sort_order, updated_at = CURRENT_TIMESTAMP
                """,
                (code, name, sort_order),
            )
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

    def ensure_prefix(provider_id: int, prefix: str | None, name: str) -> int | None:
        if prefix is None:
            return None
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
                assignment_type="gl",
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
                SET country_id = ?, provider_id = ?, assignment_type = 'gl', status = 'used',
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
            WHERE company_id_external = ?
            ORDER BY CASE WHEN server_id = ? AND country_id = ? THEN 0 ELSE 1 END, id
            LIMIT 1
            """,
            (company_id_external, server_id, country_id),
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
                SET server_id = ?, country_id = ?, company_name = ?, has_autorotation = 0, line_count = 10, dial_set_count = 2,
                    retry_interval_seconds = 60, comment = ?, is_active = 1, updated_by = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (server_id, country_id, company_name, "Demo calling campaign for MVP testing", admin_id, company_id),
            )
        repo.conn.execute(
            """
            UPDATE calling_companies
            SET is_active = 0, updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE company_id_external = ? AND id <> ?
            """,
            (admin_id, company_id_external, company_id),
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
        miatel_prefix = None
        demotel_prefix = None
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


AON_SOURCE_LABELS = {"pool": "Pool", "rnd": "RND", "sim": "SIM", "single_number": "Single", "other": "Other"}
POOL_TYPE_LABELS = {"purchased": "Пул купленных номеров", "local": "Локальный пул", "nonlocal": "Нелокальный пул", "sim_gateway": "SIM / GSM-шлюз"}
RND_TYPE_LABELS = {"local": "Локальный пул", "nonlocal": "Нелокальный пул"}

def normalize_route_aon_fields(data: dict[str, str]) -> tuple[str, str, str, str | None, str | None]:
    cli_source_type = (data.get("cli_source_type") or "").strip()
    if not cli_source_type:
        raise BusinessRuleError("Тип АОН обязателен")
    if cli_source_type not in {"pool", "rnd", "sim", "single_number", "other"}:
        raise BusinessRuleError("Некорректный тип АОН")
    rnd_type = (data.get("rnd_type") or "").strip() or None
    rnd_pool_owner = (data.get("rnd_pool_owner") or "").strip() or None
    aon_pool = (data.get("aon_pool") or "").strip()
    if cli_source_type == "pool":
        label = (data.get("cli_source_label") or "").strip()
        if not label:
            raise BusinessRuleError("Метка АОН обязательна для типа Pool")
        pool_type = aon_pool if aon_pool in {"Пул купленных номеров", "Локальный пул", "Нелокальный пул"} else "Пул купленных номеров"
        return cli_source_type, label, format_aon_pool(pool_type, rnd_pool_owner), None, rnd_pool_owner
    if cli_source_type == "rnd":
        if rnd_type not in {"local", "nonlocal"}:
            raise BusinessRuleError("Тип пула обязателен для RND")
        pool_type = POOL_TYPE_LABELS[rnd_type]
        return cli_source_type, "RND", format_aon_pool(pool_type, rnd_pool_owner), rnd_type, rnd_pool_owner
    if cli_source_type == "sim":
        return cli_source_type, "SIM", format_aon_pool("SIM / GSM-шлюз", rnd_pool_owner), None, rnd_pool_owner
    label = (data.get("cli_source_label") or "").strip()
    if not label:
        raise BusinessRuleError("Метка АОН обязательна")
    return cli_source_type, label, aon_pool, None, None

def aon_source_options(selected: object | None = None, *, include_legacy: bool = False) -> str:
    values = ["pool", "rnd", "sim"]
    if include_legacy and selected in {"single_number", "other"}:
        values.append(str(selected))
    return "".join(f"<option value='{esc(v)}' {'selected' if v == selected else ''}>{esc(AON_SOURCE_LABELS.get(v, v))}</option>" for v in values)

def format_aon_pool(pool_type: str, owner: str | None = None) -> str:
    pool_type = (pool_type or "").strip()
    owner = (owner or "").strip()
    return f"{pool_type}: {owner}" if pool_type and owner else pool_type

def pool_type_options(selected: object | None = None) -> str:
    values = ["Пул купленных номеров", "Локальный пул", "Нелокальный пул", "SIM / GSM-шлюз"]
    return "".join(f"<option value='{esc(v)}' {'selected' if v == selected else ''}>{esc(v)}</option>" for v in values)

def rnd_type_options(selected: object | None = None) -> str:
    return "<option value=''>—</option>" + "".join(f"<option value='{v}' {'selected' if v == selected else ''}>{label}</option>" for v, label in RND_TYPE_LABELS.items())

def route_aon_script() -> str:
    return """<script>
function routeSelectedText(select) { return select && select.selectedOptions && select.selectedOptions[0] ? select.selectedOptions[0].textContent.trim() : ''; }
function routeTemplateName(form) {
  const country = routeSelectedText(form.querySelector('[name=country_id]')) || (form.dataset.countryName || '');
  const provider = routeSelectedText(form.querySelector('[name=provider_id]'));
  const project = routeSelectedText(form.querySelector('[name=project_label]'));
  const label = (form.querySelector('[name=cli_source_label]')?.value || '').trim();
  const prefixValue = form.querySelector('[name=provider_prefix_id]')?.value || '';
  const prefixSelect = form.querySelector('[name=provider_prefix_id]');
  let prefix = '';
  if (prefixValue && prefixSelect) prefix = routeSelectedText(prefixSelect).replace(/ .*$/, '') + 'pfx';
  const parts = [country, (project && project !== '—' && project !== 'Меж.деп.') ? project : '', provider, label, prefix].map(v => (v || '').replace(new RegExp('^/+|/+$', 'g'), '').trim()).filter(Boolean);
  if (parts.length < 3) return '';
  return parts.join('/') + '@';
}
function updateRouteName(form, force) {
  const name = form.querySelector('[name=name]');
  if (!name) return;
  const generated = routeTemplateName(form);
  form.dataset.generatedRouteName = generated;
  if (!form.dataset.customRouteName || force) name.value = generated || 'Заполните обязательные поля для формирования названия';
}
function updateAonFields(root) {
  const source = root.querySelector('[name=cli_source_type]');
  if (!source) return;
  const label = root.querySelector('[name=cli_source_label]');
  const pool = root.querySelector('[name=aon_pool]');
  const rndType = root.querySelector('[name=rnd_type]');
  const value = source.value;
  if (label) {
    label.readOnly = value === 'rnd' || value === 'sim';
    if (value === 'rnd') label.value = 'RND';
    if (value === 'sim') label.value = 'SIM';
  }
  if (pool) {
    Array.from(pool.options).forEach(opt => {
      opt.hidden = (value === 'pool' && opt.value === 'SIM / GSM-шлюз') || (value === 'rnd' && !['Локальный пул','Нелокальный пул'].includes(opt.value)) || (value === 'sim' && opt.value !== 'SIM / GSM-шлюз');
    });
    pool.disabled = false; pool.setAttribute('aria-readonly', value === 'sim' ? 'true' : 'false');
    if (value === 'sim') pool.value = 'SIM / GSM-шлюз';
    if (value === 'rnd' && !['Локальный пул','Нелокальный пул'].includes(pool.value)) pool.value = 'Локальный пул';
    if (value === 'pool' && !['Пул купленных номеров','Локальный пул','Нелокальный пул'].includes(pool.value)) pool.value = 'Пул купленных номеров';
  }
  if (rndType && pool) rndType.value = pool.value === 'Нелокальный пул' ? 'nonlocal' : (pool.value === 'Локальный пул' ? 'local' : '');
  updateRouteName(root, false);
}
document.querySelectorAll('form').forEach(form => {
  if (form.querySelector('[name=cli_source_type]')) {
    const name = form.querySelector('[name=name]');
    if (name) name.addEventListener('input', () => { form.dataset.customRouteName = name.value !== (form.dataset.generatedRouteName || '') ? '1' : ''; });
    form.addEventListener('change', () => updateAonFields(form));
    form.addEventListener('submit', ev => {
      updateAonFields(form);
      if (name && !name.value.trim()) { ev.preventDefault(); alert('Название маршрута обязательно'); return; }
      if (name && form.dataset.generatedRouteName && name.value !== form.dataset.generatedRouteName && !confirm('Вы отредактировали название маршрута, оно не соответствует шаблону. Сохранить изменённое название?')) ev.preventDefault();
    });
    updateAonFields(form);
    if (name && !name.value) updateRouteName(form, true);
  }
});
</script>"""

def clean_parts(parts: list[str]) -> str:
    return "/".join([p.strip(" /") for p in parts if p and p.strip(" /")])


def build_route_name(repo: Repository, country_id: int, provider_id: int, project_label: str | None, cli_source_label: str, provider_prefix_id: int | None) -> str:
    country = repo.conn.execute("SELECT name FROM countries WHERE id = ?", (country_id,)).fetchone()
    provider = repo.conn.execute("SELECT name FROM providers WHERE id = ?", (provider_id,)).fetchone()
    prefix = repo.conn.execute("SELECT prefix FROM provider_prefixes WHERE id = ?", (provider_prefix_id,)).fetchone() if provider_prefix_id else None
    country_name = country["name"] if country else ""
    provider_name = provider["name"] if provider else ""
    prefix_part = f"{prefix['prefix']}pfx" if prefix and prefix["prefix"] else ""
    project_part = project_label if project_label and project_label != "Меж.деп." else ""
    base = clean_parts([country_name, project_part, provider_name, cli_source_label, prefix_part]) + "@"
    while "//@" in base or "//" in base:
        base = base.replace("//", "/")
    return base



ROUTING_MODE_LABELS = {
    "server_priority": "Приоритет серверов",
    "campaign_route": "Ручной маршрут",
    "autorotation": "Авторотация",
    "mixed": "Смешанный",
}


def routing_mode_label(value: object) -> str:
    return ROUTING_MODE_LABELS.get(str(value), "—" if value in (None, "") else str(value))


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

DASHBOARD_ENTITY_LABELS = {
    "route": "Маршруты",
    "tariff": "Тарифы",
    "phone_number": "Купленные номера",
    "calling_company": "Кампании прозвона",
    "routing_event": "Смена провайдеров",
    "server_priority": "Приоритет по серверам",
    "company_routing_setting": "Схема маршрутизации кампаний",
    "user": "Пользователи",
    "dictionary": "Справочные значения",
    "change_reason": "Справочные значения",
}

DASHBOARD_ENTITY_TONES = {
    "route": "ok",
    "tariff": "neutral",
    "phone_number": "warn",
    "calling_company": "ok",
    "routing_event": "neutral",
    "server_priority": "warn",
    "company_routing_setting": "neutral",
    "user": "neutral",
    "dictionary": "neutral",
    "change_reason": "neutral",
}


def dashboard_events(repo: Repository) -> str:
    if not can_read("admin_change_log"):
        return "<div class='event-feed-empty'>Лента событий недоступна для текущей роли.</div>"
    rows = repo.conn.execute(
        """
        SELECT cl.changed_at, cl.entity_type, cl.change_type, cl.summary, u.username
        FROM change_log cl
        LEFT JOIN users u ON u.id = cl.changed_by
        ORDER BY cl.changed_at DESC, cl.id DESC
        LIMIT 8
        """
    ).fetchall()
    if not rows:
        return "<div class='event-feed-empty'>Событий пока нет.</div>"
    items = []
    for row in rows:
        entity = row["entity_type"] or "—"
        entity_label = DASHBOARD_ENTITY_LABELS.get(entity, entity)
        tone = DASHBOARD_ENTITY_TONES.get(entity, "neutral")
        title = row["summary"] or row["change_type"] or "Изменение"
        actor = row["username"] or "система"
        subtitle = f"{entity_label} · {row['change_type'] or 'изменение'} · {actor}"
        items.append(
            f"<article><span class='feed-icon {esc(tone)}' aria-hidden='true'></span>"
            f"<div><strong>{esc(title)}</strong><small>{esc(subtitle)}</small></div>"
            f"<time>{esc(row['changed_at'] or '—')}</time></article>"
        )
    return "".join(items)


def dashboard_page(repo: Repository) -> bytes:
    metrics = "".join([
        dashboard_metric(repo, "SELECT COUNT(*) FROM routes WHERE is_actual = 1", "Активные маршруты", "Всего активных маршрутов", nav_icon("routes"), "blue", "0,22 18,22 32,16 46,22 62,17 78,17 96,10"),
        dashboard_metric(repo, "SELECT COUNT(*) FROM calling_companies WHERE is_active = 1", "Активные кампании", "Всего активных кампаний", nav_icon("companies"), "green", "0,22 12,17 28,16 44,16 58,15 72,10 84,15 96,9"),
        dashboard_metric(repo, "SELECT COUNT(*) FROM phone_numbers WHERE is_active = 1", "Купленные номера", "Всего активных номеров", nav_icon("phones"), "violet", "0,20 18,20 30,17 46,17 62,14 78,9 96,9"),
        dashboard_metric(repo, "SELECT COUNT(*) FROM routing_events WHERE is_active = 1", "Смены провайдеров", "Активные записи смен", nav_icon("provider_changes"), "orange", "0,8 16,10 32,10 48,12 64,12 80,14 96,14"),
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
    feed = dashboard_events(repo)
    body = f"""
<h1>Главная</h1>
<section class='metrics-grid'>{metrics}</section>
<section class='dashboard-section'><div class='dashboard-panel-title'><h2>Быстрые переходы</h2></div><div class='quick-links'>{work_links}{admin_links}</div></section>
<section class='dashboard-section'><div class='dashboard-panel-title'><h2>Лента событий</h2></div><div class='event-feed'>{feed}</div></section>
"""
    return page("Главная", body)



def history_icon_link(href: str) -> str:
    return f"<a class='history-link' href='{esc(href)}' title='История' aria-label='История'>{nav_icon('info')}<span class='sr-only'>ⓘ</span></a>"


def _json_dict(value: object) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def readable_bool(value: object) -> str:
    return "Да" if str(value) in {"1", "true", "True", "yes", "Да"} else "Нет"


def readable_company_history_event(row: sqlite3.Row) -> tuple[str, str, str]:
    new_values = _json_dict(row["new_value"])
    event = str(new_values.get("event") or row["comment"] or "Компания изменена")
    description = str(new_values.get("description") or event)
    details = str(new_values.get("details") or "—")
    return event, description, details

def readable_history_event(row: sqlite3.Row, *, subject: str) -> tuple[str, str, str]:
    source = row["source"]
    action = row["action"]
    reason = row["reason"] or ""
    comment = row["comment"] or ""
    old_values = _json_dict(row["old_value"])
    new_values = _json_dict(row["new_value"])
    details = comment or reason or ""
    if source == "route_phone":
        route_name = row["route_name"] or "маршрут"
        phone_number = row["phone_number"] or "номер"
        if action == "added":
            if subject == "phone":
                return "Номер добавлен в маршрут", f"Номер добавлен в маршрут: {route_name}", details
            return "Номер добавлен", f"Номер добавлен в маршрут: {phone_number}", details
        if reason == "phone_number.deactivated":
            return "Номер исключён из маршрута", "Номер автоматически исключён из маршрута из-за неактивности у провайдера", "Причина: номер стал неактивен у провайдера"
        if subject == "phone":
            message = f"Номер исключён из маршрута: {route_name}"
        else:
            message = f"Номер исключён из маршрута: {phone_number}"
        if reason:
            message += f". Причина: {reason}"
        return "Номер исключён", message, comment or reason
    if source == "phone":
        if action == "created":
            return "Номер создан", "Номер создан или импортирован", details
        if isinstance(new_values.get("changes"), list):
            return "Номер изменён", str(new_values.get("description") or f"Изменено полей: {len(new_values['changes'])}"), str(new_values.get("details") or "; ".join(new_values["changes"]))
        changes = []
        if "status" in old_values or "status" in new_values:
            changes.append(f"Рабочий статус изменён: {STATUS_LABELS.get(str(old_values.get('status')), old_values.get('status', '—'))} → {STATUS_LABELS.get(str(new_values.get('status')), new_values.get('status', '—'))}")
        if "is_active" in old_values or "is_active" in new_values:
            changes.append(f"Активен у провайдера изменено: {readable_bool(old_values.get('is_active'))} → {readable_bool(new_values.get('is_active'))}")
        if "review_required" in old_values or "review_required" in new_values:
            changes.append(f"Требует проверки изменено: {readable_bool(old_values.get('review_required'))} → {readable_bool(new_values.get('review_required'))}")
        if not changes:
            changes.append("Номер изменён")
        return "Номер изменён", "; ".join(changes), details
    if action == "created":
        return "Маршрут создан", "Маршрут создан", details
    if isinstance(new_values.get("changes"), list):
        return "Маршрут изменён", str(new_values.get("description") or f"Изменено полей: {len(new_values['changes'])}"), str(new_values.get("details") or "; ".join(new_values["changes"]))
    return "Маршрут изменён", "Маршрут изменён", details


def history_table(rows: list[sqlite3.Row], *, subject: str) -> str:
    if not rows:
        return "<div class='empty-state'>История пока пустая</div>"
    html_rows = []
    for row in rows:
        event, description, details = readable_history_event(row, subject=subject)
        html_rows.append(
            f"<tr><td>{esc(row['changed_at'])}</td><td>{esc(row['user_name'] or '—')}</td><td>{esc(event)}</td>"
            f"{clamp_cell('details', esc(description), description)}{clamp_cell('comment', esc(details or '—'), details or '—', classes='comment-cell')}</tr>"
        )
    return "<section class='journal-card'><div class='table-scroll'><table><thead><tr><th>Дата</th><th>Пользователь</th><th>Событие</th><th>Описание</th><th>Детали</th></tr></thead><tbody>" + "".join(html_rows) + "</tbody></table></div></section>"


def readable_tariff_history_event(row: sqlite3.Row) -> tuple[str, str, str]:
    reason = row["reason"] or ""
    details = row["comment"] or "—"
    if reason == "tariff.activated":
        return "Тариф активирован", "Тариф активирован", details
    if reason == "tariff.deactivated":
        return "Тариф деактивирован", "Тариф деактивирован", details
    if reason == "tariff.changed":
        return "Тариф изменён", details, details
    return "Тариф создан", "Тариф создан", row["comment"] or "—"

def tariff_history_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "<div class='empty-state'>История пока пустая</div>"
    html_rows = []
    for row in rows:
        event, description, details = readable_tariff_history_event(row)
        html_rows.append(f"<tr><td>{esc(row['changed_at'])}</td><td>{esc(row['user_name'] or '—')}</td><td>{esc(event)}</td>{clamp_cell('description', esc(description), description)}{clamp_cell('details', esc(details), details, classes='comment-cell')}</tr>")
    return "<section class='journal-card'><div class='table-scroll'><table><thead><tr><th>Дата</th><th>Пользователь</th><th>Событие</th><th>Описание</th><th>Детали</th></tr></thead><tbody>" + "".join(html_rows) + "</tbody></table></div></section>"

def tariff_history_page(repo: Repository, tariff_id: int) -> bytes:
    tariff = repo.get_tariff(tariff_id)
    if tariff is None:
        return page("Тариф не найден", "<h1>Тариф не найден</h1>")
    prefix = tariff["prefix"] or "—"
    body = f"""<h1>История тарифа</h1>
<section class='card'><h2>{esc(tariff['country_name'])} / {esc(tariff['provider_name'])} / {esc(prefix)}</h2><p class='muted'>{esc(tariff['price_in_provider_currency'])} {esc(tariff['currency_code'])} · {'Активный' if tariff['is_current'] else 'Неактивный'}</p><p><a href='/tariffs'>← Назад к тарифам</a>{' · <a href="/tariffs/' + str(tariff_id) + '/edit">Редактировать тариф</a>' if can_write('tariffs') else ''}</p></section>
{tariff_history_table(repo.list_tariff_history(tariff_id))}"""
    return page("История тарифа", body)

def phone_history_page(repo: Repository, phone_id: int) -> bytes:
    phone = repo.get_phone_number(phone_id)
    if phone is None:
        return page("Не найдено", "<h1>Номер не найден</h1>")
    body = f"""
<h1>История номера</h1>
<section class='card'><h2>{esc(phone['number'])}</h2><p class='muted'>{esc(phone['country_name'])} · {esc(phone['provider_name'] or 'Без провайдера')}</p><p><a href='/phones'>← Назад к купленным номерам</a> · <a href='/phones/{phone_id}/edit'>Редактировать номер</a></p></section>
{history_table(repo.list_phone_history(phone_id), subject='phone')}
"""
    return page("История номера", body)


def company_history_page(repo: Repository, company_id: int) -> bytes:
    company = repo.get_calling_company(company_id)
    if company is None:
        return page("Не найдено", "<h1>Компания прозвона не найдена</h1>")
    body = f"""
<h1>История компании прозвона</h1>
<section class='card'><h2>{esc(company['company_name'])}</h2><p class='muted'>ID компании: {esc(company['company_id_external'])} · Внутренний ID: {company_id}</p><p><a href='/companies'>← Назад к компаниям прозвона</a>{" · <a href='/companies/" + str(company_id) + "/edit'>Редактировать компанию</a>" if can_write("companies") else ""}</p></section>
{company_history_table(repo.list_calling_company_history(company_id))}
"""
    return page("История компании прозвона", body)

def company_history_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "<div class='empty-state'>История пока пустая</div>"
    html_rows = []
    for row in rows:
        event, description, details = readable_company_history_event(row)
        html_rows.append(f"<tr><td>{esc(row['changed_at'])}</td><td>{esc(row['user_name'] or '—')}</td><td>{esc(event)}</td>{clamp_cell('details', esc(description), description)}{clamp_cell('comment', esc(details or '—'), details or '—', classes='comment-cell')}</tr>")
    return "<section class='journal-card'><div class='table-scroll'><table><thead><tr><th>Дата</th><th>Пользователь</th><th>Событие</th><th>Описание</th><th>Детали</th></tr></thead><tbody>" + "".join(html_rows) + "</tbody></table></div></section>"

def company_events_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    search = q.get("search") or ""
    current = parse_page(q)
    total = repo.count_calling_company_events(search=search)
    page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if current > page_count:
        current = 1
    records = repo.list_calling_company_events(search=search, limit=PAGE_SIZE, offset=(current - 1) * PAGE_SIZE)
    rows = []
    for row in records:
        event, description, details = readable_company_history_event(row)
        rows.append(f"<tr><td data-col='date'>{esc(row['changed_at'])}</td><td data-col='user'>{esc(row['user_name'] or '—')}</td><td data-col='company_id'>{esc(row['company_id_external'] or row['company_id'])}</td><td data-col='current_name'>{esc(row['current_company_name'] or '—')}</td><td data-col='event'>{esc(event)}</td>{clamp_cell('description', esc(description), description)}{clamp_cell('details', esc(details), details, classes='comment-cell')}<td data-col='actions'><a class='button' href='/calling-companies/{row['company_id']}/history'>История</a></td></tr>")
    def page_href(page_number: int) -> str:
        params = {key: value for key, value in q.items() if key not in {"page", "limit", "export"} and value not in (None, "")}
        params["page"] = str(page_number)
        return "/calling-companies/history?" + urlencode(params)
    previous_link = f"<a class='button' href='{esc(page_href(current - 1))}'>← Назад</a>" if current > 1 else ""
    next_link = f"<a class='button' href='{esc(page_href(current + 1))}'>Вперёд →</a>" if current < page_count else ""
    pagination_html = f"<nav class='pagination' aria-label='Пагинация'><span class='muted'>Всего записей: {total}. Страница {current} из {page_count}</span> {previous_link} {next_link}</nav>"
    search_html = f"<form class='filter-grid' method='get' action='/calling-companies/history'><label>Поиск по журналу <input name='search' value='{esc(search)}'></label><button>Найти</button></form>"
    table_html = data_table('company_events', [('date','Дата'),('user','Пользователь'),('company_id','ID компании'),('current_name','Текущее название компании'),('event','Событие'),('description','Описание'),('details','Детали'),('actions','Действия')], ''.join(rows))
    body = f"""<h1>Журнал событий компаний прозвона</h1><p><a href='/companies'>← Назад к компаниям прозвона</a></p>{filter_card(search_html, q, ('search',))}{table_card(table_html)}{table_footer(pagination_html, '')}"""
    return page("Журнал событий компаний прозвона", table_page_container(body))

def route_history_page(repo: Repository, route_id: int) -> bytes:
    route = repo.get_route(route_id)
    if route is None:
        return page("Не найдено", "<h1>Маршрут не найден</h1>")
    body = f"""
<h1>История маршрута</h1>
<section class='card'><h2>{esc(route['name'])}</h2><p class='muted'>{esc(route['country_name'])} · {esc(route['provider_name'])}</p><p><a href='/routes'>← Назад к маршрутам</a> · <a href='/routes/{route_id}/edit'>Редактировать маршрут</a></p></section>
{history_table(repo.list_route_history(route_id), subject='route')}
"""
    return page("История маршрута", body)

def routes_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    filters = {"country_id": q.get("country_id"), "provider_id": q.get("provider_id"), "prefix_id": q.get("prefix_id"), "is_actual": q.get("is_actual"), "search_like": q.get("search")}
    records = list(repo.list_routes(filters))
    if q.get("export") == "csv":
        return csv_response("routes_export.csv", ["GEO", "Провайдер", "Маршрут", "АОН/пул", "Сервер", "Активен", "Комментарий"], [[r["country_name"], r["provider_name"], r["name"], r["aon_pool"] or "—", "", "Да" if r["is_actual"] else "Нет", r["comment"]] for r in records])
    records, pagination_html = paginate_rows(records, q, "/routes")
    rows = []
    for route in records:
        prefix = route["prefix"] or "—"
        numbers_label = "RND провайдера" if route["cli_source_type"] == "rnd" else f'{route["phone_count"]} номеров'
        numbers = f'{numbers_label} <a class="button route-numbers-action" href="/routes/{route["id"]}/numbers">Показать номера</a>'
        edit = f"<a class='button edit-action' href='/routes/{route['id']}/edit' title='Редактировать' aria-label='Редактировать' data-tooltip='Редактировать'>Редактировать</a>" if can_write("routes") else ""
        history = history_icon_link(f"/routes/{route['id']}/history")
        rows.append(f"<tr><td data-col='geo'>{esc(route['country_name'])}</td>{clamp_cell('route', esc(route['name']), route['name'], extra_attrs="data-copy-column='route-name'", classes='route-name-cell', selectable=True)}<td data-col='provider'>{esc(route['provider_name'])}</td><td data-col='prefix'>{esc(prefix)}</td><td data-col='actual'>{'Да' if route['is_actual'] else 'Нет'}</td>{clamp_cell('aon_pool', esc(route['aon_pool'] or '—'), route['aon_pool'] or '—')}{clamp_cell('comment', esc(route['comment']), route['comment'], classes='comment-cell')}<td data-col='numbers'>{numbers}</td><td data-col='history' class='history-cell'>{history}</td><td data-col='actions' class='actions'>{edit}</td></tr>")
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
  <label>Проект/метка <select name="project_label">{project_options(repo, empty='—')}</select></label>
  <label>Тип АОН <span class="required">*</span><select name="cli_source_type">{aon_source_options()}</select></label>
  <label>Метка АОН <span class="required">*</span><input name="cli_source_label" value="Pool_A"></label>
  <label>Тип пула <span class="required">*</span><select name="aon_pool">{pool_type_options("Пул купленных номеров")}</select></label>
  <input type="hidden" name="rnd_type">
  <label>Принадлежность пула <input name="rnd_pool_owner" placeholder="венгерский пул"></label>
  <label>Статус <span class="required">*</span><select name="is_actual"><option value="1">Активный</option><option value="0">Неактивный</option></select></label>
  <label>Комментарий <input name="comment"></label>
  <label class="wide">Название маршрута <span class="required">*</span><input name="name" placeholder="Заполните обязательные поля для формирования названия"></label>
  <button>Сохранить</button>
</form>""" + route_aon_script()
    table_html = f"{data_table('routes', [('geo', 'ГЕО'), ('route', f"<span class='copyable-header'>Название маршрута {copy_column_button('route-name')}</span>"), ('provider', 'Провайдер'), ('prefix', 'Префикс'), ('actual', 'Актуальный'), ('aon_pool', 'АОН/пул'), ('comment', 'Комментарий'), ('numbers', 'Номера'), ('history', 'Ист.'), ('actions', 'Действия')], ''.join(rows))}"
    body = f"""
<h1>Маршруты</h1>
{filter_card(filters_html, q, ('country_id', 'provider_id', 'prefix_id', 'is_actual', 'search'))}
{form_card('+ Добавить маршрут <span class="muted">Admin</span>', create_html) if can_write("routes") else ""}
{table_card(table_html)}
{table_footer(pagination_html, export_link('/routes', q) + column_settings('routes', [('geo', 'ГЕО'), ('route', 'Название маршрута'), ('provider', 'Провайдер'), ('prefix', 'Префикс'), ('actual', 'Актуальный'), ('aon_pool', 'АОН/пул'), ('comment', 'Комментарий'), ('numbers', 'Номера'), ('actions', 'Действия')]))}
"""
    return page("Маршруты", table_page_container(body))


def review_required_icon() -> str:
    return (
        "<span class='review-required-icon' title='Требует проверки' aria-label='Требует проверки'>"
        f"{nav_icon('warning')}"
        "<span class='sr-only'>Требует проверки</span>"
        "</span>"
    )

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
    filters = {"country_id": q.get("country_id"), "provider_id": q.get("provider_id"), "status": q.get("status", "active")}
    records = list(repo.list_tariffs(filters))
    if q.get("export") == "csv":
        return csv_response("tariffs_export.csv", ["GEO", "Provider", "Prefix", "Provider price", "Currency", "Price EUR", "Active", "Comment"], [[t["country_name"], t["provider_name"], t["prefix"] or "—", t["price_in_provider_currency"], t["currency_code"], t["eur_price"], "Да" if t["is_current"] else "Нет", t["comment"]] for t in records])
    records, pagination_html = paginate_rows(records, q, "/tariffs")
    rows = []
    for t in records:
        prefix = t["prefix"] or "—"
        actions = f"<a class='button edit-action' href='/tariffs/{t['id']}/edit' title='Редактировать' aria-label='Редактировать' data-tooltip='Редактировать'>Редактировать</a>" if can_write("tariffs") else ""
        history = history_icon_link(f"/tariffs/{t['id']}/history")
        rows.append(f"""<tr><td data-col='history' class='history-cell'>{history}</td><td data-col='actions' class='actions'>{actions}</td><td data-col='geo'>{esc(t['country_name'])}</td><td data-col='provider'>{esc(t['provider_name'])}</td><td data-col='prefix'>{esc(prefix)}</td><td data-col='provider_price'>{esc(t['price_in_provider_currency'])} {esc(t['currency_code'])}</td><td data-col='eur_price'>{esc(t['eur_price'])} EUR</td><td data-col='active'>{'Да' if t['is_current'] else 'Нет'}</td>{clamp_cell('comment', esc(t['comment']), t['comment'], classes='comment-cell')}</tr>""")
    filters_html = f"""<form class="filter-grid" method="get" action="/tariffs">
<label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Провайдер <select name="provider_id">{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
<label>Статус <select name="status"><option value="all" {'selected' if q.get('status')=='all' else ''}>Все</option><option value="active" {'selected' if q.get('status','active')=='active' else ''}>Активные</option><option value="inactive" {'selected' if q.get('status')=='inactive' else ''}>Неактивные</option></select></label><button>Найти</button></form>"""
    create_html = f"""<form class="form-grid" method="post" action="/tariffs/create">
<label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label>
<label>Провайдер <span class="required">*</span><select name="provider_id">{active_options(repo, 'providers')}</select></label>
<label>Префикс <select name="provider_prefix_id">{prefix_options(repo)}</select></label>
<label>Валюта <span class="required">*</span><select name="currency_id">{active_options(repo, 'currencies', 'code')}</select></label>
<label>Цена провайдера <span class="required">*</span><input name="price"></label>
<label>Активный <span class="required">*</span><select name="is_current"><option value="1">Да</option><option value="0">Нет</option></select></label>
<label>Комментарий <input name="comment"></label><p class="muted wide">Курс к EUR и дата курса берутся из Администрирование → Курсы валют.</p><button>Сохранить</button></form>"""
    columns = [('history', 'Инфо'), ('actions', 'Действия'), ('geo', 'ГЕО'), ('provider', 'Провайдер'), ('prefix', 'Префикс'), ('provider_price', 'Цена провайдера'), ('eur_price', 'Цена EUR'), ('active', 'Активный'), ('comment', 'Комментарий')]
    table_html = f"{data_table('tariffs', columns, ''.join(rows))}"
    body = f"""
<h1>Тарифы</h1>
{filter_card(filters_html, q, ('country_id', 'provider_id', 'status'))}
{form_card('+ Добавить тариф <span class="muted">Admin</span>', create_html) if can_write("tariffs") else ""}
{table_card(table_html)}
{table_footer(pagination_html, export_link('/tariffs', q) + column_settings('tariffs', columns))}"""
    return page("Тарифы", table_page_container(body))


def phones_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    filters = {"country_id": q.get("country_id"), "provider_id": q.get("provider_id"), "project": q.get("project"), "assignment_type": q.get("assignment_type"), "status": q.get("status"), "number_like": q.get("number"), "review_required": q.get("review_required")}
    records = list(repo.list_phone_numbers(filters))
    if q.get("export") == "csv":
        return csv_response("phones_export.csv", ["Номер", "GEO", "Провайдер", "Тип номера", "Кампания", "Рабочий статус", "Активен у провайдера", "Маршруты", "Требует проверки", "Комментарий"], [[p["number"], p["country_name"], p["provider_name"], p["phone_type"], p["project_label"], STATUS_LABELS.get(p["status"], p["status"]), "Да" if p["is_active"] else "Нет", p["route_names"] or "—", "Да" if p["review_required"] else "Нет", p["comment"]] for p in records])
    records, pagination_html = paginate_rows(records, q, "/phones")
    rows = []
    for phone in records:
        assignment_label = phone["assignment_type_label"] or ASSIGNMENT_LABELS.get(phone["assignment_type"], phone["assignment_type"])
        actions = f"<a class='button edit-action' href='/phones/{phone['id']}/edit' title='Редактировать' aria-label='Редактировать' data-tooltip='Редактировать'>Редактировать</a>" if can_write("phones") else ""
        history = history_icon_link(f"/phones/{phone['id']}/history")
        review_marker = review_required_icon() if phone["review_required"] else ""
        rows.append(f"""<tr><td data-col='number' class='selectable-cell' data-copy-column='phone-number'>{selectable_text(f"{esc(phone['number'])}{review_marker}", phone['number'], classes='phone-number-cell compound-value-cell')}</td><td data-col='geo'>{esc(phone['country_name'])}</td><td data-col='provider'>{esc(phone['provider_name'])}</td><td data-col='project'>{esc(phone['project_label'])}</td><td data-col='assignment'>{esc(assignment_label)}</td><td data-col='status'>{dot_status(STATUS_LABELS.get(phone['status'], phone['status']), 'danger' if phone['status'] == 'problem' else ('warning' if phone['status'] == 'unknown' else ('neutral' if phone['status'] == 'free' else 'ok')))}</td><td data-col='active'>{dot_status('Да' if phone['is_active'] else 'Нет', 'ok' if phone['is_active'] else 'danger')}</td>{clamp_cell('routes', esc(phone['route_names']), phone['route_names'], selectable=True) if phone['route_names'] else "<td data-col='routes'>—</td>"}<td data-col='connection'>{esc(phone['connection_cost'])}</td><td data-col='monthly'>{esc(phone['monthly_fee'])}</td><td data-col='currency'>{esc(phone['currency_code'])}</td><td data-col='phone_type'>{esc(phone['phone_type'])}</td><td data-col='tariff'>{esc(phone['tariff_label'])}</td><td data-col='created'>{esc(phone['created_at'])}</td><td data-col='updated'>{esc(phone['updated_at'])}</td><td data-col='deactivated'>{esc(phone['deactivated_at'])}</td>{clamp_cell('comment', esc(phone['comment'] or '—'), phone['comment'] or '—', classes='comment-cell')}<td data-col='history' class='history-cell'>{history}</td><td data-col='actions'>{actions}</td></tr>""")
    filters_html = f"""<form class="filter-grid" method="get" action="/phones">
<label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Провайдер <select name="provider_id">{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
    <label>Проект <select name="project">{project_options(repo, selected=q.get('project'), empty='Все')}</select></label>
    <label>Назначение <select name="assignment_type">{assignment_options(repo, selected=q.get('assignment_type'), empty='Все')}</select></label>
<label>Рабочий статус <select name="status">{phone_status_options(q.get('status'), empty='Все')}</select></label>
<label>Поиск по номеру <input name="number" value="{esc(q.get('number'))}"></label>
<label class="checkbox-inline"><input type="checkbox" name="review_required" value="1" {'checked' if q.get('review_required') == '1' else ''}> Требует проверки</label><button>Найти</button></form>"""
    create_html = f"""<form class="form-grid" method="post" action="/phones/create">
<label>Номер <span class="required">*</span><input name="number" placeholder="393331234567"></label><label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label><label>Провайдер <span class="required">*</span><select name="provider_id"><option value="">—</option>{active_options(repo, 'providers')}</select></label><label>Проект <select name="project_label">{project_options(repo, empty='—')}</select></label><label>Назначение <span class="required">*</span><select name="assignment_type">{assignment_options(repo)}</select></label><label>Рабочий статус <span class="required">*</span><select name="status">{phone_status_options('unknown')}</select></label><label>Стоимость подключения <input name="connection_cost"></label><label>Абонентская плата <input name="monthly_fee"></label><label>Валюта <select name="currency_id"><option value="">—</option>{active_options(repo, 'currencies', 'code')}</select></label><label>Тип номера <select name="phone_type">{phone_type_options(repo, empty='—')}</select></label><label>Тариф <input name="tariff_label"></label><label>Комментарий <input name="comment"></label><button>Сохранить</button></form>"""
    table_html = f"{data_table('phones', [('number', f"<span class='copyable-header'>Номер {copy_column_button('phone-number')}</span>"), ('geo', 'ГЕО'), ('provider', 'Провайдер'), ('project', 'Проект'), ('assignment', 'Назначение'), ('status', 'Рабочий статус'), ('active', 'Активен у провайдера'), ('routes', 'Маршруты'), ('connection', 'Подключение'), ('monthly', 'Абонплата'), ('currency', 'Валюта'), ('phone_type', 'Тип номера'), ('tariff', 'Тариф'), ('created', 'Дата создания'), ('updated', 'Дата изменения'), ('deactivated', 'Дата отключения'), ('comment', 'Комментарий'), ('history', 'Ист.'), ('actions', 'Действия')], ''.join(rows))}"
    body = f"""
<h1>Купленные номера</h1>
{filter_card(filters_html, q, ('country_id', 'provider_id', 'project', 'assignment_type', 'status', 'number', 'review_required'))}
{form_card('+ Добавить номер <span class="muted">Admin</span>', create_html) if can_write("phones") else ""}
{table_card(table_html)}
{table_footer(pagination_html, export_link('/phones', q) + column_settings('phones', [('number', 'Номер'), ('geo', 'ГЕО'), ('provider', 'Провайдер'), ('project', 'Проект'), ('assignment', 'Назначение'), ('status', 'Рабочий статус'), ('active', 'Активен у провайдера'), ('routes', 'Маршруты'), ('connection', 'Подключение'), ('monthly', 'Абонплата'), ('currency', 'Валюта'), ('phone_type', 'Тип номера'), ('tariff', 'Тариф'), ('created', 'Дата создания'), ('updated', 'Дата изменения'), ('deactivated', 'Дата отключения'), ('comment', 'Комментарий'), ('actions', 'Действия')]))}"""
    return page("Купленные номера", table_page_container(body))


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
        history = history_icon_link(f"/calling-companies/{cc['id']}/history")
        rows.append(f"<tr><td data-col='server'>{esc(cc['server_name'])}</td><td data-col='geo'>{esc(cc['country_name'])}</td>{clamp_cell('company_name', esc(cc['company_name']), cc['company_name'], selectable=True)}<td data-col='company_id' class='selectable-cell'>{selectable_text(esc(cc['company_id_external']), cc['company_id_external'])}</td><td data-col='lines'>{esc(cc['line_count'])}</td><td data-col='dial_sets'>{esc(cc['dial_set_count'])}</td><td data-col='autorotation'>{'Да' if cc['current_has_autorotation'] else 'Нет'}</td><td data-col='retry_interval'>{esc(cc['retry_interval_seconds'])}</td><td data-col='active'>{'Активна' if cc['is_active'] else 'Неактивна'}</td>{clamp_cell('comment', esc(cc['comment']), cc['comment'], classes='comment-cell')}<td data-col='history' class='history-cell'>{history}</td><td data-col='actions'>{actions}</td></tr>")
    filters_html = f"""<form class="filter-grid" method="get" action="/companies">
<label>Сервер <select name="server_id">{options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label><label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label><label>Название кампании <input name="company" value="{esc(q.get('company'))}"></label><label>ID кампании <input name="external_id" value="{esc(q.get('external_id'))}"></label><label>Авторотация <select name="has_autorotation"><option value="">Все</option><option value="1" {'selected' if q.get('has_autorotation')=='1' else ''}>Да</option><option value="0" {'selected' if q.get('has_autorotation')=='0' else ''}>Нет</option></select></label><label>Активность <select name="is_active"><option value="">Все</option><option value="1" {'selected' if q.get('is_active')=='1' else ''}>Активна</option><option value="0" {'selected' if q.get('is_active')=='0' else ''}>Неактивна</option></select></label><button>Найти</button></form>"""
    create_html = f"""<form class="form-grid" method="post" action="/companies/create"><label>Сервер <span class="required">*</span><select name="server_id">{active_options(repo, 'servers')}</select></label><label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label><label>ID кампании <span class="required">*</span><input name="company_id_external"></label><label>Название кампании <span class="required">*</span><input name="company_name"></label><label>Количество линий <span class="required">*</span><input name="line_count" value="0"></label><label>Количество наборов <span class="required">*</span><input name="dial_set_count" value="0"></label><label>Авторотация <span class="required">*</span><select name="has_autorotation"><option value="1">Да</option><option value="0">Нет</option></select></label><label>Интервал дозвона, сек. <span class="required">*</span><input name="retry_interval_seconds" value="0"></label><label>Активна <span class="required">*</span><select name="is_active"><option value="1">Да</option><option value="0">Нет</option></select></label><label>Комментарий <input name="comment"></label><button>Сохранить</button></form>"""
    table_html = f"{data_table('companies', [('server', 'Сервер'), ('geo', 'ГЕО'), ('company_name', 'Название кампании'), ('company_id', 'ID кампании'), ('lines', 'Количество линий'), ('dial_sets', 'Количество наборов'), ('autorotation', 'Авторотация'), ('retry_interval', 'Интервал между попытками дозвона (сек.)'), ('active', 'Активна'), ('comment', 'Комментарий'), ('history', 'Ист.'), ('actions', 'Действия')], ''.join(rows))}"
    body = f"""
<h1>Кампании прозвона</h1>
{filter_card(filters_html, q, ('server_id', 'country_id', 'company', 'external_id', 'has_autorotation', 'is_active'))}
{form_card('+ Добавить кампанию <span class="muted">Admin</span>', create_html) if can_write("companies") else ""}
{table_card(table_html)}
{table_footer(pagination_html, export_link('/companies', q) + "<a class='button table-utility-button' href='/calling-companies/history'>Журнал событий</a>" + column_settings('companies', [('server', 'Сервер'), ('geo', 'ГЕО'), ('company_name', 'Название кампании'), ('company_id', 'ID кампании'), ('lines', 'Количество линий'), ('dial_sets', 'Количество наборов'), ('autorotation', 'Авторотация'), ('retry_interval', 'Интервал дозвона'), ('active', 'Активна'), ('comment', 'Комментарий'), ('actions', 'Действия')]))}"""
    return page("Кампании прозвона", table_page_container(body))

def routing_reason_options(selected: str | None = None, scope: str = "campaign_setting") -> str:
    reasons = Repository.ROUTING_EVENT_REASONS_BY_SCOPE.get(scope, Repository.ROUTING_EVENT_REASONS)
    if selected and selected not in reasons:
        reasons = (*reasons, selected)
    return "".join(
        f"<option value='{esc(reason)}' {'selected' if reason == selected else ''}>{esc(reason)}</option>"
        for reason in reasons
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
            f"title='{esc(label)}' {'selected' if str(row['id']) == str(selected) else ''}>{esc(label)}</option>"
        )
    return opts


def overflow_route_options(repo: Repository, selected: object | None = None, empty: str | None = "—") -> str:
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
    ).fetchall()
    for row in rows:
        label = f"{row['country_name']} / {row['provider_name']} / {row['name']}"
        opts += (
            f"<option value='{row['id']}' data-country-id='{row['country_id']}' data-provider-id='{row['provider_id']}' "
            f"title='{esc(label)}' {'selected' if str(row['id']) == str(selected) else ''}>{esc(label)}</option>"
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
    rows = repo.conn.execute("""
        SELECT cc.id, cc.country_id, cc.server_id, cc.company_id_external, cc.company_name, s.name AS server_name
        FROM calling_companies cc
        JOIN servers s ON s.id = cc.server_id
        WHERE cc.is_active = 1
        ORDER BY cc.company_id_external
        """).fetchall()
    return json.dumps([
        {
            "id": row["id"],
            "country_id": row["country_id"],
            "server_id": row["server_id"],
            "server_name": row["server_name"],
            "external_id": row["company_id_external"],
            "label": f"{row['company_id_external']} / {row['company_name']}",
        }
        for row in rows
    ], ensure_ascii=False)


def routing_event_form(repo: Repository, event=None, error_message: str | None = None) -> str:
    is_existing_event = bool(event) and "id" in event.keys()
    if is_existing_event:
        def one(sql, value):
            if not value:
                return "—"
            row = repo.conn.execute(sql, (value,)).fetchone()
            return row[0] if row else "—"
        scope_labels = {"none": "Не меняли настройки в нашей системе", "server_priority": "Серверный приоритет", "campaign_setting": "Настройка кампании"}
        change_type_labels = {"enable_autorotation": "Включили авторотацию", "disable_autorotation": "Выключили авторотацию", "set_campaign_route": "Прописали ручной маршрут", "remove_campaign_route": "Убрали ручной маршрут"}
        mode_labels = {"server_priority": "Приоритет сервера", "autorotation": "Авторотация", "campaign_route": "Маршрут кампании", "mixed": "Смешанный"}
        server_names = [row["name"] for row in repo.conn.execute("""
            SELECT s.name FROM routing_event_servers res JOIN servers s ON s.id = res.server_id
            WHERE res.routing_event_id = ? ORDER BY s.name
        """, (event["id"],)).fetchall()]
        if not server_names and event["server_id"]:
            server_names = [one("SELECT name FROM servers WHERE id = ?", event["server_id"])]
        readonly_rows = [
            ("Дата события", event["event_at"]),
            ("Область применения", scope_labels.get(event["apply_scope"], event["apply_scope"] or "—")),
            ("GEO", one("SELECT name FROM countries WHERE id = ?", event["country_id"])),
            ("Серверы", ", ".join(server_names) if server_names else "—"),
            ("Провайдер", one("SELECT name FROM providers WHERE id = ?", event["provider_id"])),
            ("Маршрут/префикс", one("SELECT name FROM routes WHERE id = ?", event["affected_route_id"])),
            ("Старый маршрут", one("SELECT name FROM routes WHERE id = ?", event["old_route_id"])),
            ("Новый маршрут", one("SELECT name FROM routes WHERE id = ?", event["new_route_id"])),
            ("Перелив", one("SELECT name FROM routes WHERE id = ?", event["overflow_route_id"]) if event["apply_scope"] == "server_priority" and event["has_overflow"] else "—"),
            ("Кампания", one("SELECT company_id_external || ' / ' || company_name FROM calling_companies WHERE id = ?", event["calling_company_id"])),
            ("Тип изменения кампании", change_type_labels.get(event["company_change_type"], event["company_change_type"] or "—")),
            ("Режим маршрутизации", mode_labels.get(event["new_company_routing_mode"], event["new_company_routing_mode"] or "—")),
            ("Маршрут кампании", one("SELECT name FROM routes WHERE id = ?", event["new_company_route_id"])),
            ("Авторотация", "Да" if event["new_company_has_autorotation"] else ("Нет" if event["new_company_has_autorotation"] == 0 else "—")),
            ("Активна", "Да" if event["is_active"] else "Нет"),
            ("Причина", event["reason"]),
        ]
        readonly_html = "".join(f"<div><dt>{esc(label)}</dt><dd>{esc(value)}</dd></div>" for label, value in readonly_rows)
        return f"""
<details class='form-card modal-form-card' open data-modal-details><summary class='form-summary'>Редактировать комментарий</summary>
<p class='muted wide'>Событие смены провайдеров является неизменяемым операционным событием. Для исправления типа, маршрута, кампании или авторотации создайте новое корректирующее событие.</p>
<dl class='readonly-grid'>{readonly_html}</dl>
<form method='post' action='/provider-changes/{event['id']}/update' class='form-grid' id='routing-event-form'>
  {f"<div class='error wide'>{esc(error_message)}</div>" if error_message else ""}
  <input type='hidden' name='updated_at_original' value='{esc(event['updated_at'])}'>
  <label class='wide'>Комментарий <span class='required'>*</span><textarea name='comment' rows='3' cols='60' required>{esc(event['comment'] if event else '')}</textarea></label>
  <button>Сохранить комментарий</button>
</form>
</details>
"""
    event_at = (event["event_at"] if event else datetime.now().strftime("%Y-%m-%d %H:%M")).replace(" ", "T")[:16]
    scope = event["apply_scope"] if event else "none"
    route_opts = route_options_for_dynamic_form(repo, selected=event["affected_route_id"] if event else None, empty="—")
    new_route_opts = route_options_for_dynamic_form(repo, selected=event["new_route_id"] if event else None, empty="—")
    company_route_opts = route_options_for_dynamic_form(repo, selected=event["new_company_route_id"] if event else None, empty="—")
    overflow_route_selected = event.get("overflow_route_id") if isinstance(event, dict) else (event["overflow_route_id"] if event else None)
    has_overflow_value = event.get("has_overflow") if isinstance(event, dict) else (event["has_overflow"] if event else 0)
    overflow_opts = overflow_route_options(repo, selected=overflow_route_selected, empty="—")
    has_overflow_checked = "checked" if has_overflow_value else ""
    selected_company_ids = set()
    if event:
        raw_selected = event.get("calling_company_ids") if isinstance(event, dict) else None
        if raw_selected:
            selected_company_ids = {str(value) for value in raw_selected}
        elif event["calling_company_id"]:
            selected_company_ids = {str(event["calling_company_id"])}
    company_opts = ""
    for company in repo.conn.execute("""
        SELECT cc.id, cc.server_id, cc.company_id_external, cc.company_name, s.name AS server_name
        FROM calling_companies cc
        JOIN servers s ON s.id = cc.server_id
        WHERE cc.is_active = 1 OR cc.id = ?
        ORDER BY cc.company_id_external
        """, (event["calling_company_id"] if event else 0,)):
        label = f"{company['company_id_external']} / {company['company_name']}"
        checked = "checked" if str(company["id"]) in selected_company_ids else ""
        company_opts += (
            f"<label class='multi-option' data-server-id='{company['server_id']}' "
            f"data-campaign-id='{esc(company['company_id_external'])}' data-server-name='{esc(company['server_name'])}' "
            f"title='{esc(label)}'><input type='checkbox' name='calling_company_ids' value='{company['id']}' {checked}> "
            f"<span>{esc(label)}</span></label>"
        )
    selected_server_ids = {str(event["server_id"])} if event and event["server_id"] else set()
    server_priority_server_boxes = active_server_priority_checkboxes(repo, selected_server_ids, event["country_id"] if event else None)
    action = f"/provider-changes/{event['id']}/update" if is_existing_event else "/provider-changes/create"
    submit = "Сохранить изменения" if is_existing_event else "Создать событие"
    inactive_note = "<p class='muted'>Редактирование события не применяет повторно server_route_priorities. Для исправления текущего приоритета создайте новое событие.</p>" if is_existing_event else ""
    old_route_field = f"<label class='scope-field' data-scopes='server_priority'>Старый маршрут (только описание при редактировании) <select name='old_route_id'>{route_options_for_dynamic_form(repo, selected=event['old_route_id'] if event else None, empty='—')}</select></label>" if is_existing_event else ""
    provider_selected = event["provider_id"] if event else None
    error_html = f"<div class='error wide'>{esc(error_message)}</div>" if error_message else ""
    return f"""
<details class='form-card modal-form-card' {'open' if is_existing_event or error_message else ''} data-modal-details><summary class='form-summary'>{'Редактировать событие' if is_existing_event else '+ Добавить событие'}</summary>
<form method='post' action='{action}' class='form-grid' id='routing-event-form' data-current-scope='{esc(scope)}' data-default-country-id='{esc(active_country_id_if_single(repo) or '')}'>
  {error_html}
  <fieldset><legend>Область применения</legend>
    <div class='scope-cards'>
      <label class='card scope-card'><input type='radio' name='apply_scope' value='none' {'checked' if scope == 'none' else ''}><span class='scope-card-indicator' aria-hidden='true'></span><span class='scope-card-text'>Не меняли настройки в нашей системе</span></label>
      <label class='card scope-card'><input type='radio' name='apply_scope' value='server_priority' {'checked' if scope == 'server_priority' else ''}><span class='scope-card-indicator' aria-hidden='true'></span><span class='scope-card-text'>Серверный приоритет</span></label>
      <label class='card scope-card'><input type='radio' name='apply_scope' value='campaign_setting' {'checked' if scope == 'campaign_setting' else ''}><span class='scope-card-indicator' aria-hidden='true'></span><span class='scope-card-text'>Настройка кампании</span></label>
    </div>
  </fieldset>
  {inactive_note}
  <div class='provider-change-campaign-grid'>
    <label>Дата события <span class='required'>*</span><input type='datetime-local' name='event_at' value='{esc(event_at)}' required></label>
    <label class='scope-field campaign-helper-field campaign-server-field' data-scopes='campaign_setting'>Сервер <select name='server_id' id='campaign-server-filter'>{options(repo, 'servers', selected=event['server_id'] if event else None, empty='—')}</select></label>
    <label class='scope-field campaign-change-type-field' data-scopes='campaign_setting'>Тип изменения кампании <span class='required'>*</span><select name='company_change_type' id='company-change-type'>
      <option value=''>—</option>
      {''.join(f"<option value='{v}' {'selected' if event and event['company_change_type'] == v else ''}>{label}</option>" for v, label in [('enable_autorotation','Включили авторотацию'),('disable_autorotation','Выключили авторотацию'),('set_campaign_route','Прописали ручной маршрут'),('remove_campaign_route','Убрали ручной маршрут')])}
    </select></label>
    <div class='scope-field campaign-helper-field campaign-id-action-field' data-scopes='campaign_setting'><span class='field-label'>ID кампании</span><div class='campaign-id-inline-action'><input name='campaign_id_search' id='campaign-id-search' value='{esc(event['campaign_id_search'] if event and 'campaign_id_search' in event.keys() else '')}'><button type='button' id='campaign-id-search-button' class='small-button'>OK</button></div><span class='field-error' id='campaign-id-search-error' aria-live='polite'></span></div>
  </div>
  <label class='scope-field' data-scopes='none server_priority'>GEO <span class='required'>*</span><select name='country_id' id='event-country'>{active_options(repo, 'countries', selected=event['country_id'] if event else None, empty='—')}</select></label>
  <fieldset class='scope-field' data-scopes='server_priority'><legend>Серверы <span class='required'>*</span></legend>{server_priority_server_boxes}</fieldset>
  <label class='scope-field routing-provider-field' data-scopes='none server_priority'>Провайдер <span class='required provider-required'>*</span><select name='provider_id' id='event-provider'>{active_options(repo, 'providers', selected=provider_selected, empty='—')}</select></label>
  <label class='scope-field' data-scopes='none'>Маршрут/префикс <select name='affected_route_id' id='affected-route'>{route_opts}</select></label>
  {old_route_field}
  <label class='scope-field route-select-field' data-scopes='server_priority'>Новый маршрут <span class='required'>*</span><select name='new_route_id' id='new-route' class='route-select'>{new_route_opts}</select></label>
  <span class='scope-field route-empty-message muted' data-scopes='server_priority' id='new-route-empty' hidden>Нет маршрутов для выбранного провайдера и GEO</span>
  <label class='scope-field spillover-checkbox important-checkbox' data-scopes='server_priority'><input type='checkbox' name='has_overflow' id='has-overflow' value='1' {has_overflow_checked}> <span>Есть перелив</span></label>
  <label class='scope-field' data-scopes='server_priority' id='overflow-route-field'>Маршрут перелива <span class='required'>*</span><select name='overflow_route_id' id='overflow-route'>{overflow_opts}</select></label>
  <div class='provider-change-campaign-lower-grid'>
    <label class='routing-reason-field'>Причина <span class='required'>*</span><select name='reason' id='routing-reason' required>{routing_reason_options(event['reason'] if event else None, scope)}</select><span class='field-helper' id='routing-reason-helper'></span></label>
    <div class='scope-field campaign-company-field' data-scopes='campaign_setting'>
      <span class='field-label'>Кампания <span class='required'>*</span></span>
      <details class='multi-select' id='event-company' data-placeholder='—'>
        <summary id='event-company-summary'>—</summary>
        <div class='multi-select-panel'>
          <div class='multi-select-actions'>
            <button type='button' class='small-button' id='campaign-select-visible'>Выбрать все найденные</button>
            <button type='button' class='small-button' id='campaign-clear-selected'>Отменить выбранные</button>
          </div>
          {company_opts}
        </div>
      </details>
    </div>
  </div>
  <label class='scope-field conditional-field' data-scopes='campaign_setting' data-campaign-route-field='1'>Новый провайдер кампании <span class='required'>*</span><select name='campaign_provider_id' id='campaign-provider'>{active_options(repo, 'providers', selected=provider_selected, empty='—')}</select></label>
  <label class='scope-field conditional-field' data-scopes='campaign_setting' data-campaign-route-field='1'>Новый маршрут кампании <span class='required'>*</span><select name='new_company_route_id' id='company-route'>{company_route_opts}</select></label>
  <span class='scope-field route-empty-message muted' data-scopes='campaign_setting' id='company-route-empty' hidden>Нет маршрутов для выбранного провайдера и GEO кампании</span>
  <label class='wide'>Комментарий <span class='required comment-required'>*</span><textarea name='comment' id='routing-comment' rows='3' cols='60'>{esc(event['comment'] if event else '')}</textarea></label>
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
  const campaigns = {campaign_metadata_json(repo)};
  const reasonsByScope = {json.dumps(Repository.ROUTING_EVENT_REASONS_BY_SCOPE, ensure_ascii=False)};
  const currentReason = {json.dumps(event['reason'] if event else None, ensure_ascii=False)};
  const campaignCountries = Object.fromEntries(campaigns.map((company) => [String(company.id), company.country_id]));
  const routeNeeds = new Set(['set_campaign_route']);
  function selectedScope() {{ return (form.querySelector('input[name="apply_scope"]:checked') || {{value: 'none'}}).value; }}
  function setRequired(el, required) {{ if (el) el.required = !!required; }}
  function rebuildReasonSelect(scope) {{
    const reason = document.getElementById('routing-reason');
    if (!reason) return;
    const previous = reason.value || currentReason || '';
    reason.innerHTML = '';
    (reasonsByScope[scope] || []).forEach((value) => {{
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = value;
      if (value === previous) opt.selected = true;
      reason.appendChild(opt);
    }});
  }}
  function syncCommentRequirement(scope) {{
    const reason = document.getElementById('routing-reason');
    const comment = document.getElementById('routing-comment');
    const marker = form.querySelector('.comment-required');
    const helper = document.getElementById('routing-reason-helper');
    const requireComment = scope === 'none' && reason && reason.value === 'Другое';
    if (comment) comment.required = requireComment;
    if (marker) marker.hidden = !requireComment;
    if (helper) helper.textContent = requireComment ? 'Требуется понятный комментарий' : (scope === 'server_priority' && reason && reason.value === 'Обратная смена провайдера' ? 'например тех. проблемы' : '');
  }}
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
        opt.title = route.label;
        if (String(route.id) === String(current)) opt.selected = true;
        select.appendChild(opt);
        count += 1;
      }}
    }});
    updateSelectTitle(select);
    if (emptyEl) emptyEl.hidden = !(countryId && providerId && count === 0);
  }}
  function updateSelectTitle(select) {{
    if (!select) return;
    const selected = select.options[select.selectedIndex];
    select.title = selected ? selected.textContent : '';
  }}
  function setCampaignSearchError(message) {{
    const error = document.getElementById('campaign-id-search-error');
    if (error) error.textContent = message || '';
  }}
  function selectedCampaignBoxes() {{ return Array.from(form.querySelectorAll('input[name="calling_company_ids"]:checked')); }}
  function updateCompanySummary() {{
    const summary = document.getElementById('event-company-summary');
    if (!summary) return;
    const checked = selectedCampaignBoxes();
    if (checked.length === 0) {{ summary.textContent = '—'; return; }}
    if (checked.length === 1) {{
      const label = checked[0].closest('.multi-option');
      summary.textContent = label ? label.textContent.trim() : checked[0].value;
      return;
    }}
    summary.textContent = `Выбрано: ${{checked.length}} кампании`;
  }}
  function filterCompanyOptions() {{
    const showNotice = arguments.length > 0 ? arguments[0] : false;
    const container = document.getElementById('event-company');
    const server = document.getElementById('campaign-server-filter');
    if (!container || !server) return;
    const selectedServerId = server.value;
    let cleared = false;
    container.querySelectorAll('.multi-option').forEach((option) => {{
      const show = !selectedServerId || String(option.dataset.serverId) === String(selectedServerId);
      option.hidden = !show;
      const box = option.querySelector('input');
      if (box) {{
        box.disabled = !show;
        if (!show && box.checked) {{ box.checked = false; cleared = true; }}
      }}
    }});
    if (cleared && showNotice) setCampaignSearchError('Выбор кампаний обновлён по выбранному серверу');
    updateCompanySummary();
  }}
  function findCampaignByVisibleId() {{
    const input = document.getElementById('campaign-id-search');
    const container = document.getElementById('event-company');
    const server = document.getElementById('campaign-server-filter');
    if (!input || !container || !server) return;
    const campaignId = input.value.trim();
    setCampaignSearchError('');
    if (!campaignId) return;
    const found = campaigns.find((company) => String(company.external_id) === campaignId);
    if (!found) {{
      setCampaignSearchError('Кампания с таким ID не найдена');
      return;
    }}
    if (server.value && String(found.server_id) !== String(server.value)) {{
      const selectedServerName = (server.options[server.selectedIndex] && server.options[server.selectedIndex].textContent) || server.value;
      setCampaignSearchError(`Кампания с ID ${{campaignId}} находится на сервере ${{found.server_name}}, а выбран сервер ${{selectedServerName}}`);
      return;
    }}
    const box = container.querySelector(`input[name="calling_company_ids"][value="${{found.id}}"]`);
    if (box && !box.disabled) box.checked = true;
    updateCompanySummary();
    sync();
  }}
  function sync() {{
    const scope = selectedScope();
    form.dataset.currentScope = scope;
    rebuildReasonSelect(scope);
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
    rebuildRouteSelect(document.getElementById('overflow-route'), country && country.value, null, null);
    filterCompanyOptions(false);
    const checkedCampaign = selectedCampaignBoxes()[0];
    const campaignProvider = document.getElementById('campaign-provider');
    const companyCountry = checkedCampaign ? campaignCountries[checkedCampaign.value] : '';
    rebuildRouteSelect(document.getElementById('company-route'), companyCountry, campaignProvider && campaignProvider.value, document.getElementById('company-route-empty'));
    const ctype = document.getElementById('company-change-type');
    const needsRoute = scope === 'campaign_setting' && routeNeeds.has(ctype && ctype.value);
    form.querySelectorAll('[data-campaign-route-field]').forEach((el) => {{ el.hidden = !needsRoute; el.querySelectorAll('select').forEach((f) => f.required = needsRoute); }});
    setRequired(country, scope === 'server_priority');
    setRequired(provider, scope === 'none' || scope === 'server_priority');
    updateSelectTitle(document.getElementById('new-route'));
    updateSelectTitle(document.getElementById('company-route'));
    setRequired(document.getElementById('new-route'), scope === 'server_priority');
    const hasOverflow = document.getElementById('has-overflow');
    const overflowField = document.getElementById('overflow-route-field');
    const overflowRoute = document.getElementById('overflow-route');
    const overflowEnabled = scope === 'server_priority' && hasOverflow && hasOverflow.checked;
    if (overflowField) overflowField.hidden = !overflowEnabled;
    if (overflowRoute) {{ overflowRoute.disabled = !overflowEnabled; overflowRoute.required = !!overflowEnabled; if (!overflowEnabled) overflowRoute.value = ''; }}
    setRequired(ctype, scope === 'campaign_setting');
    syncCommentRequirement(scope);
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
  form.querySelectorAll('input[name="apply_scope"], #event-country, #event-provider, #campaign-provider, #company-change-type, #has-overflow').forEach((el) => el.addEventListener('change', sync));
  const reasonSelect = document.getElementById('routing-reason');
  if (reasonSelect) reasonSelect.addEventListener('change', () => syncCommentRequirement(selectedScope()));
  form.querySelectorAll('input[name="calling_company_ids"]').forEach((el) => el.addEventListener('change', sync));
  const campaignServerFilter = document.getElementById('campaign-server-filter');
  if (campaignServerFilter) campaignServerFilter.addEventListener('change', () => {{ filterCompanyOptions(true); sync(); }});
  const campaignDropdown = document.getElementById('event-company');
  const selectVisible = document.getElementById('campaign-select-visible');
  if (selectVisible) selectVisible.addEventListener('click', () => {{
    document.querySelectorAll('#event-company .multi-option:not([hidden]) input[name="calling_company_ids"]').forEach((box) => {{ if (!box.disabled) box.checked = true; }});
    sync();
  }});
  const clearSelected = document.getElementById('campaign-clear-selected');
  if (clearSelected) clearSelected.addEventListener('click', () => {{
    form.querySelectorAll('input[name="calling_company_ids"]:checked').forEach((box) => {{ box.checked = false; }});
    sync();
  }});
  if (campaignDropdown) {{
    document.addEventListener('click', (event) => {{
      if (campaignDropdown.open && !campaignDropdown.contains(event.target)) campaignDropdown.open = false;
    }});
    campaignDropdown.addEventListener('keydown', (event) => {{
      if (campaignDropdown.open && (event.key === 'Enter' || event.key === 'Escape')) {{
        event.preventDefault();
        event.stopPropagation();
        campaignDropdown.open = false;
      }}
    }});
  }}
  const campaignSearchButton = document.getElementById('campaign-id-search-button');
  if (campaignSearchButton) campaignSearchButton.addEventListener('click', findCampaignByVisibleId);
  form.querySelectorAll('.route-select').forEach((el) => el.addEventListener('change', () => updateSelectTitle(el)));
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
                overflow = ev["overflow_route_name"] if ev["has_overflow"] else "—"
                return server_text, "—", "Серверы:<ul class='event-server-list'>" + "".join(items) + f"</ul>; Перелив: {esc(overflow)}"
        route_text = f"{esc(ev['old_route_name'] or '—')} → {esc(ev['new_route_name'] or '—')}"
        overflow = ev["overflow_route_name"] if ev["has_overflow"] else "—"
        return ev["server_name"] or "—", "—", route_text + f"; Перелив: {esc(overflow)}"
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
    return ev["company_server_name"] or "—", campaign, "; ".join(details) or "—"


def provider_changes_date_filter_values(q: dict[str, str]) -> tuple[dict[str, str], str | None]:
    date_from = (q.get("date_from") or "").strip()
    date_to = (q.get("date_to") or "").strip()
    for value in (date_from, date_to):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return {}, "Некорректный формат даты"
    if date_from and date_to and date_from > date_to:
        return {}, "Дата от не может быть позже даты до"
    filters: dict[str, str] = {}
    if date_from:
        filters["date_from"] = f"{date_from} 00:00:00"
    if date_to:
        filters["date_to"] = f"{date_to} 23:59:59"
    return filters, None


def provider_changes_page(repo: Repository, q: dict[str, str] | None = None, form_error: str | None = None, form_data: dict | None = None) -> bytes:
    q = q or {}
    date_filters, filter_error = provider_changes_date_filter_values(q)
    filters = {"country_id": q.get("country_id"), "apply_scope": q.get("apply_scope"), "server_id": q.get("server_id"), "campaign_id": q.get("campaign_id"), "provider_id": q.get("provider_id"), "include_inactive": q.get("include_inactive") == "1", **date_filters}
    records = [] if filter_error else list(repo.list_routing_events(filters))
    if q.get("export") == "csv":
        export_rows = []
        for ev in records:
            server_text, _, details_text = provider_event_details(ev)
            details_plain = html_to_csv_text(details_text)
            export_rows.append([ev["event_at"], ROUTING_SCOPE_LABELS.get(ev["apply_scope"], ev["apply_scope"]), ev["country_name"], server_text, ev["company_id_external"] or ev["company_name"] or "—", details_plain, ev["reason"], ev["comment"], ev["author_name"] or "—"])
        return csv_response("provider_changes_export.csv", ["Дата события", "Область применения", "GEO", "Сервер", "Кампания", "Детали", "Причина", "Комментарий", "Пользователь / Автор"], export_rows)
    records, pagination_html = paginate_rows(records, q, "/provider-changes")
    rows = []
    for ev in records:
        server_text, campaign_text, details_text = provider_event_details(ev)
        actions = f"<a class='button edit-action' href='/provider-changes/{ev['id']}/edit' title='Редактировать' aria-label='Редактировать' data-tooltip='Редактировать'>Редактировать</a>" if can_write("provider_changes") else ""
        comment_text = (ev["comment"] or "").strip() or "—"
        rows.append(f"<tr class='{'' if ev['is_active'] else 'inactive-row'}'><td data-col='event_at'>{esc(ev['event_at'])}</td><td data-col='scope'>{esc(ROUTING_SCOPE_LABELS.get(ev['apply_scope'], ev['apply_scope']))}</td><td data-col='geo'>{esc(ev['country_name'])}</td><td data-col='server'>{esc(server_text)}</td><td data-col='campaign' class='selectable-cell'>{selectable_text(esc(campaign_text), campaign_text)}</td>{clamp_cell('details', details_text, html_to_csv_text(details_text), selectable=True)}{clamp_cell('comment', esc(comment_text), comment_text)}{clamp_cell('reason', esc(ev['reason']), ev['reason'])}<td data-col='actions' class='actions'>{actions}</td></tr>")
    if not rows:
        rows.append("<tr><td colspan='9'><div class='empty-state'>Событий пока нет</div></td></tr>")
    filters_html = f"""<form class='filter-grid' method='get' action='/provider-changes'>
<label>Дата от <input type='date' name='date_from' value='{esc(q.get('date_from'))}'></label>
<label>Дата до <input type='date' name='date_to' value='{esc(q.get('date_to'))}'></label>
<label>GEO <select name='country_id'>{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Область применения <select name='apply_scope'>{routing_scope_options(q.get('apply_scope'))}</select></label>
<label>Сервер <select name='server_id'>{options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label>
<label>Кампания ID <input name='campaign_id' value='{esc(q.get('campaign_id'))}'></label>
<label>Провайдер <select name='provider_id'>{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
<label class='checkbox-inline'><input type='checkbox' name='include_inactive' value='1' {'checked' if q.get('include_inactive') == '1' else ''}> Показывать архив/неактивные</label>
<button>Найти</button></form>"""
    journal_html = f"{data_table('provider_changes', [('event_at', 'Дата события'), ('scope', 'Область применения'), ('geo', 'GEO'), ('server', 'Сервер'), ('campaign', 'Кампания'), ('details', 'Детали'), ('comment', 'Комментарий'), ('reason', 'Причина'), ('actions', 'Действия')], ''.join(rows))}"
    body = f"""
<h1>Смена провайдеров</h1>
{routing_event_form(repo, form_data, form_error) if can_write("provider_changes") else ""}
{filter_card(filters_html, q, ('date_from', 'date_to', 'country_id', 'apply_scope', 'server_id', 'campaign_id', 'provider_id', 'include_inactive'))}
{f"<div class='notice ok'>{esc(q.get('notice'))}</div>" if q.get('notice') else ""}
{f"<div class='notice error'>{esc(filter_error)}</div>" if filter_error else ""}
{table_card(journal_html, title='Журнал событий', extra_class='journal-card')}
{table_footer(pagination_html, export_link('/provider-changes', q) + column_settings('provider_changes', [('event_at', 'Дата события'), ('scope', 'Область применения'), ('geo', 'GEO'), ('server', 'Сервер'), ('campaign', 'Кампания'), ('details', 'Детали'), ('comment', 'Комментарий'), ('reason', 'Причина'), ('actions', 'Действия')]))}
"""
    return page("Смена провайдеров", table_page_container(body))


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
    for value, label in (("admin", "Админ"), ("operator", "Дежурный"), ("boss", "Руководитель"), ("guest", "Гость")):
        opts += f"<option value='{value}' {'selected' if value == selected else ''}>{esc(label)}</option>"
    return opts



def permission_matrix_form(repo: Repository, user_id: int | None = None) -> str:
    existing = {}
    if user_id is not None:
        existing = {row["section_key"]: row for row in repo.conn.execute("SELECT section_key, can_read, can_write, can_export FROM user_permissions WHERE user_id = ?", (user_id,))}
    rows = []
    for section in SECTION_REGISTRY:
        key = section["section_key"]
        row = existing.get(key)
        read_checked = " checked" if row and row["can_read"] else ""
        write_checked = " checked" if row and row["can_write"] else ""
        export_checked = " checked" if row and row["can_export"] else ""
        export_cell = (
            f"<input type='checkbox' name='perm__{key}__export' value='1'{export_checked}>"
            if section["supports_export"] else "<span class='muted'>—</span>"
        )
        rows.append(f"<tr><td>{esc(section['display_name'])}<br><code>{esc(key)}</code></td><td><input type='checkbox' name='perm__{key}__read' value='1'{read_checked}></td><td><input type='checkbox' name='perm__{key}__write' value='1'{write_checked}></td><td>{export_cell}</td></tr>")
    return f"<fieldset><legend>Права доступа</legend><table><thead><tr><th>Раздел</th><th>Чтение</th><th>Запись</th><th>Экспорт</th></tr></thead><tbody>{''.join(rows)}</tbody></table><p class='muted'>Если явные права не сохранены, применяются права роли по умолчанию.</p></fieldset>"


def save_user_permissions(repo: Repository, user_id: int, data: dict[str, str]) -> None:
    for section in SECTION_REGISTRY:
        key = section["section_key"]
        can_read_value = 1 if data.get(f"perm__{key}__read") == "1" else 0
        can_write_value = 1 if data.get(f"perm__{key}__write") == "1" else 0
        can_export_value = 1 if section["supports_export"] and data.get(f"perm__{key}__export") == "1" else 0
        repo.conn.execute(
            """
            INSERT INTO user_permissions(user_id, section_key, can_read, can_write, can_export)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, section_key) DO UPDATE SET
                can_read = excluded.can_read,
                can_write = excluded.can_write,
                can_export = excluded.can_export
            """,
            (user_id, key, can_read_value, can_write_value, can_export_value),
        )
    repo.conn.commit()

def users_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    rows = []
    for user in repo.list_users(active_only=False):
        rows.append(f"""
<tr class="{'inactive-row' if not user['is_active'] else ''}">
  <td>{user['id']}</td>
  <td><code>{esc(user['username'])}</code></td>
  <td>{esc(user['display_name'])}</td>
  <td>{esc(user['email'] or '—')}</td>
  <td><span class='status-badge'>{esc(role_label(user['role_key']))}</span></td>
  <td>{'Да' if user['is_active'] else 'Нет'}</td>
  <td>{'Да' if user['must_change_password'] else 'Нет'}</td>
  <td>{esc(user['created_at'])}</td>
  <td>{esc(user['updated_at'])}</td>
  <td data-col='actions' class='actions'>
    <details class='edit-details'><summary>Редактировать</summary>
      <form class='form-grid' method='post' action='/admin/users/{user['id']}/update'>
        <label>Логин <input name='username' value='{esc(user['username'])}' required></label>
        <label>Email <input name='email' type='email' value='{esc(user['email'] or '')}'></label>
        <label>Отображаемое имя <input name='display_name' value='{esc(user['display_name'])}' required></label>
        <label>Роль <select name='role_key'>{role_options(user['role_key'])}</select></label>
        <label>Активен <select name='is_active'><option value='1' {'selected' if user['is_active'] else ''}>Да</option><option value='0' {'selected' if not user['is_active'] else ''}>Нет</option></select></label>
        <fieldset><legend>Сбросить пароль</legend><label>Новый временный пароль <input name='password' type='password' minlength='6' autocomplete='new-password'></label><label>Повторите пароль <input name='password_confirm' type='password' minlength='6' autocomplete='new-password'></label><p class='muted'>При сбросе пользователь будет обязан сменить пароль при следующем входе.</p></fieldset>
        {permission_matrix_form(repo, int(user['id']))}
        <button>Сохранить</button>
      </form>
    </details>
  </td>
</tr>""")
    create_html = f"""<form class='form-grid' method='post' action='/admin/users/create'>
<label>Логин <span class='required'>*</span><input name='username' placeholder='operator2' required></label>
<label>Email <input name='email' type='email' placeholder='user@example.com'></label>
<label>Отображаемое имя <span class='required'>*</span><input name='display_name' placeholder='Оператор' required></label>
<label>Роль <select name='role_key'>{role_options('operator')}</select></label>
<label>Временный пароль <span class='required'>*</span><input name='password' type='password' minlength='6' required autocomplete='new-password'></label>
<label>Повторите пароль <span class='required'>*</span><input name='password_confirm' type='password' minlength='6' required autocomplete='new-password'></label>
<label class='checkbox-inline'><input type='checkbox' name='must_change_password' value='1' checked> Требовать смену пароля при первом входе</label>
{permission_matrix_form(repo)}
<button>Создать</button></form>"""
    table_html = f"<table><thead><tr><th>ID</th><th>Логин</th><th>Отображаемое имя</th><th>Email</th><th>Роль</th><th>Активен</th><th>Смена пароля</th><th>Создан</th><th>Обновлён</th><th data-col='actions'>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    body = f"""
<h1>Пользователи</h1>
{f"<div class='notice ok'>{esc(q.get('notice'))}</div>" if q.get('notice') else ""}
<p class='muted'>Пользователи входят по логину и паролю. Права доступа берутся из индивидуальной матрицы; если она не заполнена, применяются права роли по умолчанию.</p>
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
               overflowr.name AS overflow_route_name,
               u.username AS changed_by_username
        FROM server_route_priorities srp
        JOIN countries c ON c.id = srp.country_id JOIN servers s ON s.id = srp.server_id
        LEFT JOIN routes cr ON cr.id = srp.current_route_id LEFT JOIN providers cp ON cp.id = cr.provider_id
        LEFT JOIN routes pr ON pr.id = srp.previous_route_id LEFT JOIN providers pp ON pp.id = pr.provider_id
        LEFT JOIN routes overflowr ON overflowr.id = srp.overflow_route_id
        LEFT JOIN users u ON u.id = srp.changed_by
        {priority_where}
        ORDER BY s.name, c.name
    """, priority_params))
    if q.get("export") == "csv":
        return csv_response("server_priorities_export.csv", ["GEO", "Сервер", "Провайдер/маршрут", "Приоритет", "Активен", "Комментарий"], [[row["country_name"], row["server_name"], f"{row['current_provider_name'] or '—'} / {row['current_route_name'] or '—'}", row["current_route_name"] or "—", "Да" if row["is_active"] else "Нет", row["comment"]] for row in priority_records])

    def previous_overflow_route_id(row: sqlite3.Row) -> int | None:
        if not row["previous_route_id"]:
            return None
        event = repo.conn.execute(
            """
            SELECT re.snapshot_json
            FROM routing_events re
            JOIN routing_event_servers res ON res.routing_event_id = re.id
            WHERE re.apply_scope = 'server_priority'
              AND re.country_id = ?
              AND res.server_id = ?
              AND res.old_route_id = ?
              AND res.new_route_id = ?
              AND datetime(re.event_at) = datetime(?)
            ORDER BY re.id DESC
            LIMIT 1
            """,
            (row["country_id"], row["server_id"], row["previous_route_id"], row["current_route_id"], row["changed_at"]),
        ).fetchone()
        if not event or not event["snapshot_json"]:
            return None
        try:
            snapshot = json.loads(event["snapshot_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        for affected in snapshot.get("affected_servers", []):
            if int(affected.get("server_id") or 0) != int(row["server_id"]):
                continue
            if not affected.get("old_has_overflow"):
                return None
            overflow_route_id = affected.get("old_overflow_route_id")
            return int(overflow_route_id) if overflow_route_id else None
        return None

    previous_overflow_ids = {overflow_id for row in priority_records if (overflow_id := previous_overflow_route_id(row))}
    previous_overflow_names = {}
    if previous_overflow_ids:
        placeholders = ",".join("?" for _ in previous_overflow_ids)
        previous_overflow_names = {
            route["id"]: route["name"]
            for route in repo.conn.execute(f"SELECT id, name FROM routes WHERE id IN ({placeholders})", tuple(previous_overflow_ids))
        }

    priority_records, pagination_html = paginate_rows(priority_records, q, "/admin/server-priorities")
    for row in priority_records:
        current_route = row["current_route_name"] or "—"
        previous_route = row["previous_route_name"] or "—"
        current_overflow = esc(row["overflow_route_name"]) if row["has_overflow"] and row["overflow_route_id"] and row["overflow_route_name"] else "—"
        previous_overflow_id = previous_overflow_route_id(row)
        previous_overflow = esc(previous_overflow_names.get(previous_overflow_id)) if previous_overflow_id and previous_overflow_names.get(previous_overflow_id) else "—"
        current_priority_value = f"{current_route}; Перелив: {plain_text(current_overflow)}" if row["current_route_id"] else "—; Перелив: —"
        current_priority = selectable_text(f"{esc(current_route)}<br><span class='muted'>Перелив: {current_overflow}</span>", current_priority_value) if row["current_route_id"] else "—<br><span class='muted'>Перелив: —</span>"
        previous_priority_value = f"{previous_route}; Перелив: {plain_text(previous_overflow)}" if row["previous_route_id"] else "—; Перелив: —"
        previous_priority = selectable_text(f"{esc(previous_route)}<br><span class='muted'>Перелив: {previous_overflow}</span>", previous_priority_value) if row["previous_route_id"] else "—<br><span class='muted'>Перелив: —</span>"
        if row["server_id"] in server_rows:
            server_rows[row["server_id"]].append(
                f"<tr><td data-col='geo'>{esc(row['country_name'])}</td><td data-col='current_priority' class='selectable-cell'>{current_priority}</td><td data-col='previous_priority' class='selectable-cell'>{previous_priority}</td></tr>"
            )
    blocks = []
    for server_id in server_names:
        rows = server_rows[server_id] or ["<tr><td colspan='3' class='muted'>Нет настроенных приоритетов</td></tr>"]
        table_html = f"{data_table('server_priorities', [('geo', 'GEO'), ('current_priority', 'Текущий приоритет'), ('previous_priority', 'Предыдущий приоритет')], ''.join(rows))}"
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
{table_footer(pagination_html, export_link('/admin/server-priorities', q) + column_settings('server_priorities', [('geo', 'GEO'), ('current_priority', 'Текущий приоритет'), ('previous_priority', 'Предыдущий приоритет')]))}"""
    return page("Приоритет по серверам", body)



COMPANY_ROUTING_SETTINGS_COLUMN_LABELS = [
    ("server", "Сервер"),
    ("geo", "GEO"),
    ("company_id", "ID кампании"),
    ("company_name", "Название кампании"),
    ("routing_mode", "Режим маршрутизации"),
    ("autorotation", "Авторотация"),
    ("route", "Маршрут кампании"),
    ("active", "Активна"),
    ("valid_from", "Действует с"),
    ("valid_to", "Действует до"),
    ("comment", "Комментарий"),
    ("history", ""),
]
COMPANY_ROUTING_SETTINGS_COLUMNS = [key for key, _label in COMPANY_ROUTING_SETTINGS_COLUMN_LABELS]


def campaign_routing_event_details(ev: sqlite3.Row) -> tuple[str, str, str]:
    event = COMPANY_CHANGE_LABELS.get(ev["company_change_type"], ev["company_change_type"] or "Режим маршрутизации изменён")
    description = ev["reason"] or "—"

    def route_label(prefix: str) -> str:
        route = ev[f"{prefix}_route_name"]
        provider = ev[f"{prefix}_provider_name"]
        if not route:
            return "—"
        return f"{provider} / {route}" if provider else route

    details: list[str] = []
    old_mode = ev["old_company_routing_mode"]
    new_mode = ev["new_company_routing_mode"]
    if old_mode or new_mode:
        details.append(f"Режим: {routing_mode_label(old_mode)} → {routing_mode_label(new_mode)}")
    if ev["old_company_has_autorotation"] is not None or ev["new_company_has_autorotation"] is not None:
        old_auto = "Да" if ev["old_company_has_autorotation"] else "Нет"
        new_auto = "Да" if ev["new_company_has_autorotation"] else "Нет"
        details.append(f"Авторотация: {old_auto} → {new_auto}")
    if ev["old_company_route_id"] or ev["new_company_route_id"]:
        details.append(f"Маршрут: {route_label('old')} → {route_label('new')}")
    return event, description, "; ".join(details) or "—"


def campaign_routing_history_page(repo: Repository, setting_id: int) -> bytes:
    setting = repo.get_company_routing_setting(setting_id)
    if not setting:
        raise BusinessRuleError("Схема маршрутизации кампании не найдена")
    header_rows = [
        ("GEO", setting["country_name"]),
        ("ID кампании", setting["company_id_external"]),
        ("Название кампании", setting["company_name"]),
        ("Сервер", setting["server_name"]),
        ("Current routing mode", routing_mode_label(setting["routing_mode"])),
    ]
    header = "".join(f"<dt>{esc(label)}</dt><dd>{esc(value or '—')}</dd>" for label, value in header_rows)
    rows = []
    for ev in repo.list_company_routing_setting_history(setting_id):
        event, description, details = campaign_routing_event_details(ev)
        rows.append(
            "<tr>"
            f"<td data-col='date'>{esc(ev['event_at'])}</td>"
            f"<td data-col='user'>{esc(ev['user_name'] or '—')}</td>"
            f"<td data-col='event'>{esc(event)}</td>"
            f"<td data-col='description'>{esc(description)}</td>"
            f"<td data-col='details'>{esc(details)}</td>"
            f"<td data-col='comment'>{esc(ev['comment'])}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='6' class='muted'>История маршрутизации кампании пока пуста</td></tr>")
    table = data_table('campaign_routing_history', [('date', 'Дата'), ('user', 'Пользователь'), ('event', 'Событие'), ('description', 'Описание'), ('details', 'Детали'), ('comment', 'Комментарий')], ''.join(rows))
    body = f"""
<h1>История маршрутизации кампании</h1>
<p><a href='/admin/company-routing-settings'>← Вернуться к схеме маршрутизации кампаний</a></p>
<section class='card'><dl class='details-grid'>{header}</dl></section>
{table_card(table)}
"""
    return page("История маршрутизации кампании", body)

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
    records = list(repo.list_company_routing_settings(filters))
    if q.get("export") == "csv":
        return csv_response("company_routing_settings_export.csv", ["Кампания", "GEO", "Маршрут", "Авторотация", "Активен", "Комментарий"], [[f"{r['company_id_external']} — {r['company_name']}", r["country_name"], r["route_name"] or "—", "Да" if r["has_autorotation"] else "Нет", "Да" if r["is_active"] else "Нет", r["comment"]] for r in records])
    records, pagination_html = paginate_rows(records, q, "/admin/company-routing-settings")
    rows = []
    for setting in records:
        route_label = setting["route_name"] or "—"
        provider_label = f"<br><span class='muted'>Провайдер: {esc(setting['provider_name'])}</span>" if setting["provider_name"] else ""
        active_badge = "Да" if setting["is_active"] else "Нет"
        row = {
            "server": esc(setting["server_name"]),
            "geo": esc(setting["country_name"]),
            "company_id": selectable_text(esc(setting["company_id_external"]), setting["company_id_external"]),
            "company_name": selectable_text(esc(setting["company_name"]), setting["company_name"]),
            "routing_mode": esc(routing_mode_label(setting["routing_mode"])),
            "autorotation": "Да" if setting["has_autorotation"] else "Нет",
            "route": selectable_text(f"{esc(route_label)}{provider_label}", route_label),
            "active": active_badge,
            "valid_from": esc(setting["valid_from"]),
            "valid_to": esc(setting["valid_to"] or "—"),
            "comment": esc(setting["comment"]),
            "history": history_icon_link(f"/campaign-routing/{setting['id']}/history"),
        }
        rows.append(
            "<tr>"
            + "".join(f"<td data-col='{key}' class='selectable-cell'>{row[key]}</td>" if key in {'company_id', 'company_name', 'route'} else f"<td data-col='{key}'>{row[key]}</td>" for key in COMPANY_ROUTING_SETTINGS_COLUMNS)
            + "</tr>"
        )
    filters_html = f"""<form class="filter-grid" method="get" action="/admin/company-routing-settings">
<label>GEO <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Сервер <select name="server_id">{options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label>
<label>ID кампании <input name="company_id_external" value="{esc(q.get('company_id_external'))}"></label>
<label>Режим маршрутизации <select name="routing_mode">{routing_mode_options(q.get('routing_mode'), empty='Все')}</select></label>
<label>Активность <select name="is_active"><option value="" {'selected' if not q.get('is_active') else ''}>Все</option><option value="1" {'selected' if q.get('is_active')=='1' else ''}>Активна</option><option value="0" {'selected' if q.get('is_active')=='0' else ''}>Неактивна</option></select></label>
<label class="checkbox-inline"><input type="checkbox" name="show_history" value="1" {'checked' if show_history else ''}> Показывать историю</label>
<button>Найти</button></form>"""
    table_html = f"""{data_table('company_routing_settings', COMPANY_ROUTING_SETTINGS_COLUMN_LABELS, ''.join(rows))}"""
    body = f"""
<h1>Администрирование → Схема маршрутизации кампаний</h1>
<p class='muted'>Схема маршрутизации кампаний показывает текущие исключения из стандартных правил прозвона. Изменения маршрутизации выполняются через раздел ‘Смена провайдеров’.</p>
{filter_card(filters_html, q, ('country_id', 'server_id', 'company_id_external', 'routing_mode', 'is_active', 'show_history'))}
{table_card(table_html)}
{table_footer(pagination_html, export_link('/admin/company-routing-settings', q) + column_settings('company_routing_settings', COMPANY_ROUTING_SETTINGS_COLUMN_LABELS))}
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
        source = list(repo.conn.execute("SELECT * FROM projects ORDER BY sort_order, name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/projects/{row['id']}/update'><input name='name' value='{esc(row['name'])}'><input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    else:
        headers = ["Назначение", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM phone_assignment_types ORDER BY sort_order, name"))
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


def tariff_edit_page(repo: Repository, tariff_id: int) -> bytes:
    tariff = repo.get_tariff(tariff_id)
    if tariff is None:
        return page("Тариф не найден", "<h1>Тариф не найден</h1>")
    prefix = tariff["prefix"] or "—"
    body = f"""<h1>Редактировать тариф</h1><p><a href='/tariffs'>← Назад</a></p>
<form method='post' action='/tariffs/{tariff_id}/update'>
<label>ГЕО <input value='{esc(tariff['country_name'])}' readonly></label>
<label>Провайдер <input value='{esc(tariff['provider_name'])}' readonly></label>
<label>Префикс <input value='{esc(prefix)}' readonly></label>
<label>Цена провайдера <span class='required'>*</span><input name='price' value='{esc(tariff['price_in_provider_currency'])}'></label>
<label>Валюта <span class='required'>*</span><select name='currency_id' id='tariff-currency' data-original-currency='{esc(tariff['provider_currency_id'])}'>{active_options(repo, 'currencies', 'code', selected=tariff['provider_currency_id'])}</select></label>
<p class='muted wide' id='currency-warning' hidden>Вы меняете валюту тарифа. Проверьте, что цена указана в новой валюте.</p>
<label>Комментарий <input name='comment' value='{esc(tariff['comment'])}'></label>
<label>Активен <span class='required'>*</span><select name='is_current'><option value='1' {'selected' if tariff['is_current'] else ''}>Да</option><option value='0' {'selected' if not tariff['is_current'] else ''}>Нет</option></select></label>
<p class='muted wide'>GEO, провайдер и префикс задают идентичность тарифа и не редактируются.</p>
<button>Сохранить</button></form>
<script>
const currencySelect = document.getElementById('tariff-currency');
const currencyWarning = document.getElementById('currency-warning');
function updateCurrencyWarning() {{ currencyWarning.hidden = currencySelect.value === currencySelect.dataset.originalCurrency; }}
currencySelect.addEventListener('change', updateCurrencyWarning);
updateCurrencyWarning();
</script>"""
    return page("Редактировать тариф", body)

def route_edit_page(repo: Repository, route_id: int) -> bytes:
    route = repo.conn.execute("SELECT r.*, c.name AS country_name FROM routes r JOIN countries c ON c.id = r.country_id WHERE r.id = ?", (route_id,)).fetchone()
    if route is None:
        return page("Маршрут не найден", "<h1>Маршрут не найден</h1>")
    body = f"""<h1>Редактировать маршрут</h1><p><a href='/routes'>← Назад</a></p>
<form method='post' action='/routes/{route_id}/update' data-country-name='{esc(route['country_name']) if 'country_name' in route.keys() else ''}'>
<label>Название маршрута <span class='required'>*</span><input name='name' value='{esc(route['name'])}' size='60'></label>
<label>Провайдер <span class='required'>*</span><select name='provider_id'>{active_options(repo, 'providers', selected=route['provider_id'])}</select></label>
<label>Префикс <select name='provider_prefix_id'>{prefix_options(repo, selected=route['provider_prefix_id'])}</select></label>
<label>Тип АОН <span class='required'>*</span><select name='cli_source_type'>{aon_source_options(route['cli_source_type'], include_legacy=True)}</select></label>
<label>Метка АОН <span class='required'>*</span><input name='cli_source_label' value='{esc(route['cli_source_label'])}'></label>
<label>Тип пула <span class='required'>*</span><select name='aon_pool'>{pool_type_options((route['aon_pool'] or '').split(':', 1)[0])}</select></label>
<input type='hidden' name='rnd_type' value='{esc(route['rnd_type'] or '')}'>
<label>Принадлежность пула <input name='rnd_pool_owner' value='{esc(route['rnd_pool_owner'] or '')}'></label>
<label>Комментарий <input name='comment' value='{esc(route['comment'])}'></label>
<label>Актуальный <select name='is_actual'><option value='1' {'selected' if route['is_actual'] else ''}>Активный</option><option value='0' {'selected' if not route['is_actual'] else ''}>Неактивный</option></select></label>
<label>Приоритет <select name='priority_status'><option value='priority' {'selected' if route['priority_status']=='priority' else ''}>priority</option><option value='alternative' {'selected' if route['priority_status']=='alternative' else ''}>alternative</option><option value='unknown' {'selected' if route['priority_status']=='unknown' else ''}>unknown</option></select></label>
<button>Сохранить</button></form>
{route_aon_script()}
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
<label class='important-checkbox'><input type='checkbox' name='review_required' value='1' {'checked' if phone['review_required'] else ''}> <span>Требует проверки</span></label>
<p class='muted'>Поле «Маршрутов» не редактируется и считается автоматически.</p>
<button>Сохранить</button></form>"""
    return page("Редактировать номер", body)


def company_edit_page(repo: Repository, company_id: int) -> bytes:
    cc = repo.conn.execute(
        """
        SELECT cc.*, s.name AS server_name, COALESCE(active_crs.has_autorotation, 0) AS current_has_autorotation
        FROM calling_companies cc
        JOIN servers s ON s.id = cc.server_id
        LEFT JOIN company_routing_settings active_crs
          ON active_crs.calling_company_id = cc.id
         AND active_crs.is_active = 1
         AND active_crs.valid_to IS NULL
        WHERE cc.id = ?
        """,
        (company_id,),
    ).fetchone()
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
<div class='muted'>Авторотация: {'Да' if cc['current_has_autorotation'] else 'Нет'}</div>
<p class='muted'>Маршрутизация компании изменяется через ‘Смена провайдеров’.</p>
<label>Интервал дозвона, сек. <input name='retry_interval_seconds' value='{esc(cc['retry_interval_seconds'])}'></label>
<label>Активна <select name='is_active'><option value='1' {'selected' if cc['is_active'] else ''}>Да</option><option value='0' {'selected' if not cc['is_active'] else ''}>Нет</option></select></label>
<label>Комментарий <input name='comment' value='{esc(cc['comment'])}'></label>
<button>Сохранить</button></form>"""
    return page("Редактировать кампанию", body)


def provider_change_edit_page(repo: Repository, change_id: int) -> bytes:
    event = repo.conn.execute("SELECT * FROM routing_events WHERE id = ?", (change_id,)).fetchone()
    if event is None:
        return page("Событие не найдено", "<h1>Событие не найдено</h1>")
    body = f"""<h1>Редактировать событие смены провайдеров</h1><p><a href='/provider-changes'>← Назад</a></p>
{routing_event_form(repo, event)}
<p class='muted'>Создано: {esc(event['created_at'])}; обновлено: {esc(event['updated_at'])}</p>"""
    return page("Редактировать событие", body)


def provider_change_edit_page_with_error(repo: Repository, change_id: int, error_message: str) -> bytes:
    event = repo.conn.execute("SELECT * FROM routing_events WHERE id = ?", (change_id,)).fetchone()
    if event is None:
        return page("Событие не найдено", f"<h1>Событие не найдено</h1><div class='error'>{esc(error_message)}</div>")
    body = f"""<h1>Редактировать событие смены провайдеров</h1><p><a href='/provider-changes'>← Назад</a></p>
{routing_event_form(repo, event, error_message)}
<p class='muted'>Создано: {esc(event['created_at'])}; обновлено: {esc(event['updated_at'])}</p>"""
    return page("Редактировать событие", body)


def parse_int(value: str | None) -> int | None:
    return int(value) if value not in (None, "") else None


def send_provider_change_notification(repo: Repository, event_id: int) -> None:
    try:
        event = repo.get_routing_event(event_id)
        if event is None:
            return
        notify_provider_change_created(dict(event))
    except Exception as exc:
        logger.error("Telegram provider-change notification failed: %s", exc)


def handle_post(repo: Repository, path: str, data: dict[str, str]):
    actor_id = current_actor_id()
    if path == "/admin/users/create":
        username = data["username"].strip()
        display_name = data["display_name"].strip()
        password = data.get("password", "")
        password_confirm = data.get("password_confirm", "")
        if not username or not display_name:
            raise BusinessRuleError("Логин и отображаемое имя обязательны")
        if len(password) < 6:
            raise BusinessRuleError("Пароль обязателен и должен быть не короче 6 символов")
        if password != password_confirm:
            raise BusinessRuleError("Пароли не совпадают")
        new_user_id = repo.create_user(username, data.get("role_key") or "operator", display_name, password=password, email=data.get("email"), must_change_password=data.get("must_change_password") == "1")
        save_user_permissions(repo, new_user_id, data)
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
            username=data.get("username"),
            email=data.get("email"),
        )
        password = data.get("password", "")
        password_confirm = data.get("password_confirm", "")
        if password or password_confirm:
            if len(password) < 6:
                raise BusinessRuleError("Новый пароль должен быть не короче 6 символов")
            if password != password_confirm:
                raise BusinessRuleError("Пароли не совпадают")
            repo.update_user_password(user_id, password, must_change_password=True)
            target_user = repo.get_user(user_id)
            target_name = target_user["display_name"] or target_user["username"] if target_user is not None else display_name
            if user_id == actor_id:
                return f"/login?notice={quote('Ваш пароль сброшен. Войдите с временным паролем и смените его.')}&notice_type=success"
            notice = f"Пароль пользователя {target_name} сброшен. При следующем входе пользователь должен сменить пароль."
            save_user_permissions(repo, user_id, data)
            return f"/admin/users?notice={quote(notice)}"
        save_user_permissions(repo, user_id, data)
        return "/admin/users"
    if path == "/routes/create":
        country_id = int(data["country_id"]); provider_id = int(data["provider_id"]); prefix_id = parse_int(data.get("provider_prefix_id"))
        if prefix_id:
            prefix_provider = repo.conn.execute("SELECT provider_id FROM provider_prefixes WHERE id = ?", (prefix_id,)).fetchone()
            if prefix_provider and int(prefix_provider["provider_id"]) != provider_id:
                raise BusinessRuleError("Префикс не принадлежит выбранному провайдеру")
        cli_source_type, cli_source_label, aon_pool, rnd_type, rnd_pool_owner = normalize_route_aon_fields(data)
        name = (data.get("name") or "").strip()
        if not name:
            name = build_route_name(repo, country_id, provider_id, data.get("project_label"), cli_source_label, prefix_id)
        if len(name.replace("/", "").replace("@", "").strip()) < 4:
            raise BusinessRuleError("Некорректное название маршрута: заполните ГЕО, провайдера и источник АОН")
        repo.create_route(country_id=country_id, provider_id=provider_id, provider_prefix_id=prefix_id, name=name, project_label=data.get("project_label"), cli_source_type=cli_source_type, cli_source_label=cli_source_label, aon_pool=aon_pool, rnd_type=rnd_type, rnd_pool_owner=rnd_pool_owner, comment=data.get("comment"), created_by=actor_id, is_actual=data.get("is_actual") == "1")
        return "/routes"
    if path.startswith("/routes/") and path.endswith("/update"):
        route_id = int(path.strip("/").split("/")[1])
        name = data.get("name", "").strip()
        provider_id = int(data["provider_id"])
        prefix_id = parse_int(data.get("provider_prefix_id"))
        if prefix_id:
            prefix_provider = repo.conn.execute("SELECT provider_id FROM provider_prefixes WHERE id = ?", (prefix_id,)).fetchone()
            if prefix_provider and int(prefix_provider["provider_id"]) != provider_id:
                raise BusinessRuleError("Префикс не принадлежит провайдеру маршрута")
        if not name:
            raise BusinessRuleError("Название маршрута обязательно")
        route_existing = repo.conn.execute("SELECT cli_source_type, cli_source_label, aon_pool, rnd_type, rnd_pool_owner FROM routes WHERE id = ?", (route_id,)).fetchone()
        if route_existing is None:
            raise BusinessRuleError("Route not found")
        aon_data = dict(data)
        for key in ("cli_source_type", "cli_source_label", "aon_pool", "rnd_type", "rnd_pool_owner"):
            if key not in aon_data:
                aon_data[key] = route_existing[key] or ""
        cli_source_type, cli_source_label, aon_pool, rnd_type, rnd_pool_owner = normalize_route_aon_fields(aon_data)
        repo.update_route(route_id, name=name, provider_id=provider_id, provider_prefix_id=prefix_id, cli_source_type=cli_source_type, cli_source_label=cli_source_label, aon_pool=aon_pool, rnd_type=rnd_type, rnd_pool_owner=rnd_pool_owner, comment=data.get("comment"), is_actual=data.get("is_actual") == "1", priority_status=data.get("priority_status") or "unknown", updated_by=actor_id)
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
            existing_phone = repo.conn.execute("SELECT is_active FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
            if is_active == 1 and existing_phone and int(existing_phone["is_active"]) == 0:
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
        tariff_id = repo.create_tariff(country_id=int(data["country_id"]), provider_id=int(data["provider_id"]), provider_prefix_id=prefix_id, provider_currency_id=currency_id, price_in_provider_currency=data["price"], conversion_rate_to_eur=rate["rate_to_eur"], conversion_rate_date=rate["rate_date"], currency_rate_id=rate["id"], created_by=actor_id, comment=data.get("comment"))
        if data.get("is_current") == "0":
            repo.set_tariff_active(tariff_id, is_current=False, changed_by=actor_id)
        return "/tariffs"
    if path.startswith("/tariffs/") and path.endswith("/update"):
        tariff_id = int(path.strip("/").split("/")[1])
        currency_id = int(data["currency_id"])
        rate = repo.latest_currency_rate(currency_id)
        if rate is None:
            raise BusinessRuleError("Для выбранной валюты нет курса к EUR. Добавьте курс в Администрирование → Курсы валют")
        repo.update_tariff(tariff_id, provider_currency_id=currency_id, price_in_provider_currency=data["price"], conversion_rate_to_eur=rate["rate_to_eur"], conversion_rate_date=rate["rate_date"], currency_rate_id=rate["id"], comment=data.get("comment"), updated_by=actor_id, is_current=data.get("is_current") == "1")
        return f"/tariffs/{tariff_id}/edit"
    if path.startswith("/tariffs/") and (path.endswith("/deactivate") or path.endswith("/activate")):
        raise BusinessRuleError("Активность тарифа изменяется только через форму редактирования")
    if path == "/companies/create":
        repo.create_calling_company(server_id=int(data["server_id"]), country_id=int(data["country_id"]), company_name=data["company_name"], company_id_external=data["company_id_external"], has_autorotation=data.get("has_autorotation") == "1", created_by=actor_id, comment=data.get("comment"), is_active=data.get("is_active") == "1", line_count=int(data.get("line_count") or 0), dial_set_count=int(data.get("dial_set_count") or 0), retry_interval_seconds=int(data.get("retry_interval_seconds") or 0))
        return "/companies"
    if path.startswith("/companies/") and path.endswith("/update"):
        company_id = int(path.strip("/").split("/")[1])
        existing = repo.get_calling_company(company_id)
        repo.update_calling_company(company_id, server_id=int(data["server_id"]), country_id=int(data["country_id"]), company_name=data["company_name"], line_count=int(data.get("line_count") or 0), dial_set_count=int(data.get("dial_set_count") or 0), has_autorotation=bool(existing["has_autorotation"]) if existing else False, retry_interval_seconds=int(data.get("retry_interval_seconds") or 0), is_active=data.get("is_active") == "1", comment=data.get("comment"), updated_by=actor_id)
        return "/companies"
    if path == "/provider-changes/create":
        apply_scope = data.get("apply_scope")
        provider_id = parse_int(data.get("campaign_provider_id")) if apply_scope == "campaign_setting" else parse_int(data.get("provider_id"))
        selected_server_ids = parse_qs(data.get("_raw", ""), keep_blank_values=True).get("server_ids") if apply_scope == "server_priority" else None
        raw_values = parse_qs(data.get("_raw", ""), keep_blank_values=True)
        calling_company_ids = [parse_int(value) for value in raw_values.get("calling_company_ids", [])]
        calling_company_ids = [value for value in calling_company_ids if value]
        legacy_calling_company_id = parse_int(data.get("calling_company_id"))
        if legacy_calling_company_id and legacy_calling_company_id not in calling_company_ids:
            calling_company_ids.append(legacy_calling_company_id)
        if apply_scope == "campaign_setting" and (data.get("campaign_id_search") or "").strip():
            campaign_id_search = (data.get("campaign_id_search") or "").strip()
            found_company = repo.conn.execute("""
                SELECT cc.id, cc.server_id, cc.company_id_external, s.name AS server_name
                FROM calling_companies cc
                JOIN servers s ON s.id = cc.server_id
                WHERE cc.company_id_external = ? AND cc.is_active = 1
                """, (campaign_id_search,)).fetchone()
            if not found_company:
                raise BusinessRuleError("Кампания с таким ID не найдена")
            helper_server_id = parse_int(data.get("server_id"))
            if helper_server_id and int(found_company["server_id"]) != helper_server_id:
                selected_server = repo.conn.execute("SELECT name FROM servers WHERE id = ?", (helper_server_id,)).fetchone()
                selected_server_name = selected_server["name"] if selected_server else str(helper_server_id)
                raise BusinessRuleError(f"Кампания с ID {campaign_id_search} находится на сервере {found_company['server_name']}, а выбран сервер {selected_server_name}")
            if int(found_company["id"]) not in calling_company_ids:
                calling_company_ids.append(int(found_company["id"]))
        if apply_scope == "campaign_setting":
            if not calling_company_ids:
                raise BusinessRuleError("Выберите хотя бы одну кампанию")
            if not raw_values.get("calling_company_ids") and legacy_calling_company_id and not (data.get("campaign_id_search") or "").strip():
                event_id = repo.create_routing_event(
                    event_at=data.get("event_at"), apply_scope=apply_scope, reason=data.get("reason"), comment=data.get("comment"),
                    country_id=parse_int(data.get("country_id")), server_id=parse_int(data.get("server_id")), server_ids=selected_server_ids, provider_id=provider_id,
                    affected_route_id=parse_int(data.get("affected_route_id")), old_route_id=parse_int(data.get("old_route_id")), new_route_id=parse_int(data.get("new_route_id")),
                    calling_company_id=legacy_calling_company_id, company_change_type=data.get("company_change_type") or None,
                    new_company_routing_mode=data.get("new_company_routing_mode") or None, new_company_route_id=parse_int(data.get("new_company_route_id")),
                    new_company_has_autorotation=parse_int(data.get("new_company_has_autorotation")), created_by=actor_id,
                )
                send_provider_change_notification(repo, event_id)
                return "/provider-changes"
            helper_server_id = parse_int(data.get("server_id"))
            if helper_server_id:
                visible_ids = {int(row["id"]) for row in repo.conn.execute("SELECT id FROM calling_companies WHERE server_id = ? AND is_active = 1", (helper_server_id,)).fetchall()}
                calling_company_ids = [company_id for company_id in calling_company_ids if company_id in visible_ids]
                if not calling_company_ids:
                    raise BusinessRuleError("Выберите хотя бы одну кампанию")
            created_count = 0
            skipped_count = 0
            noop_markers = ("уже включена авторотация", "авторотация уже выключена", "Этот маршрут уже прописан", "ручной маршрут не задан")
            seen_company_ids = list(dict.fromkeys(calling_company_ids))
            for calling_company_id in seen_company_ids:
                try:
                    event_id = repo.create_routing_event(
                        event_at=data.get("event_at"), apply_scope=apply_scope, reason=data.get("reason"), comment=data.get("comment"),
                        country_id=parse_int(data.get("country_id")), server_id=parse_int(data.get("server_id")), server_ids=selected_server_ids, provider_id=provider_id,
                        affected_route_id=parse_int(data.get("affected_route_id")), old_route_id=parse_int(data.get("old_route_id")), new_route_id=parse_int(data.get("new_route_id")),
                        calling_company_id=calling_company_id, company_change_type=data.get("company_change_type") or None,
                        new_company_routing_mode=data.get("new_company_routing_mode") or None, new_company_route_id=parse_int(data.get("new_company_route_id")),
                        new_company_has_autorotation=parse_int(data.get("new_company_has_autorotation")), created_by=actor_id,
                    )
                    send_provider_change_notification(repo, event_id)
                    created_count += 1
                except BusinessRuleError as exc:
                    if any(marker in str(exc) for marker in noop_markers):
                        skipped_count += 1
                        continue
                    raise
            if created_count == 0:
                raise BusinessRuleError("Изменений нет: выбранные кампании уже находятся в этом состоянии")
            if skipped_count:
                return f"/provider-changes?notice={quote(f'Создано событий: {created_count}. Пропущено без изменений: {skipped_count}.')}"
            return "/provider-changes"
        event_id = repo.create_routing_event(
            event_at=data.get("event_at"), apply_scope=apply_scope, reason=data.get("reason"), comment=data.get("comment"),
            country_id=parse_int(data.get("country_id")), server_id=parse_int(data.get("server_id")), server_ids=selected_server_ids, provider_id=provider_id,
            affected_route_id=parse_int(data.get("affected_route_id")), old_route_id=parse_int(data.get("old_route_id")), new_route_id=parse_int(data.get("new_route_id")),
            calling_company_id=legacy_calling_company_id, company_change_type=data.get("company_change_type") or None,
            new_company_routing_mode=data.get("new_company_routing_mode") or None, new_company_route_id=parse_int(data.get("new_company_route_id")),
            new_company_has_autorotation=parse_int(data.get("new_company_has_autorotation")),
            has_overflow=(data.get("has_overflow") == "1"), overflow_route_id=parse_int(data.get("overflow_route_id")), created_by=actor_id,
        )
        send_provider_change_notification(repo, event_id)
        return "/provider-changes"
    if path.startswith("/provider-changes/") and path.endswith("/update"):
        change_id = int(path.strip("/").split("/")[1])
        repo.update_routing_event(change_id, comment=data.get("comment"), updated_at_original=data.get("updated_at_original"), updated_by=actor_id)
        return "/provider-changes"
    if path.startswith("/provider-changes/") and path.endswith("/deactivate"):
        raise BusinessRuleError("События смены провайдеров нельзя деактивировать")
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
        prefix = normalize_real_prefix(prefix)
        repo.create_prefix(int(data["provider_id"]), prefix); return "/tariffs"
    if path == "/admin/providers/create":
        repo.create_provider(data["name"], default_currency_id=parse_int(data.get("currency_id"))); return "/admin/dictionaries"
    if path == "/admin/countries/create":
        repo.create_country(data["name"], data.get("code") or None); return "/admin/dictionaries"
    if path == "/admin/prefixes/create":
        prefix = data.get("prefix") or None
        prefix = normalize_real_prefix(prefix)
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
            prefix = normalize_real_prefix(prefix)
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
            prefix = normalize_real_prefix(prefix)
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
    if path == "/admin/server-priorities/create":
        raise ForbiddenError()
    if path.startswith("/admin/server-priorities/") and (
        path.endswith("/update") or path.endswith("/comment") or path.endswith("/deactivate") or path.endswith("/delete")
    ):
        raise ForbiddenError()
    if path == "/admin/company-routing-settings/create":
        raise BusinessRuleError("Схема маршрутизации кампаний доступна только для просмотра текущего состояния; создание выполняется через ‘Смена провайдеров’")
    if path.startswith("/admin/company-routing-settings/") and path.endswith("/update"):
        raise BusinessRuleError("Схема маршрутизации кампаний доступна только для просмотра текущего состояния; изменения выполняются через ‘Смена провайдеров’")
    if path.startswith("/admin/company-routing-settings/") and (path.endswith("/deactivate") or path.endswith("/delete")):
        raise BusinessRuleError("Схема маршрутизации кампаний доступна только для просмотра текущего состояния; деактивация и удаление выполняются через ‘Смена провайдеров’")
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
    if path.startswith("/tariffs/") and path.endswith("/update"):
        return "/tariffs/" + path.strip("/").split("/")[1] + "/edit"
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
    ensure_db_initialized(conn, DB_PATH)
    repo = Repository(conn)
    ensure_seed(repo)
    method = environ["REQUEST_METHOD"]
    path = environ.get("PATH_INFO", "/")
    q = request_query(environ)
    cookie_id = cookie_user_id(environ)
    current_user_id = resolve_current_user_id(repo, cookie_id)
    current_user = repo.get_user(current_user_id) if current_user_id is not None else None
    filter_state = load_filter_state(environ)
    _REQUEST_CONTEXT.clear()
    _REQUEST_CONTEXT.update({
        "repo": repo,
        "current_user_id": current_user_id,
        "current_role_key": normalize_role(current_user["role_key"] if current_user else None),
        "redirect_to": current_request_path(environ),
        "path": path,
        "filter_state": filter_state,
    })
    try:
        if path == "/logout":
            return redirect(start_response, "/login", [clear_current_user_cookie()])
        if method == "GET" and path == "/login":
            start_response("200 OK", html_headers())
            return [login_page(repo, q.get("notice"), q.get("notice_type") or "error")]
        if method == "POST" and path == "/login":
            raw_size = int(environ.get("CONTENT_LENGTH") or "0")
            raw_body = environ["wsgi.input"].read(raw_size).decode("utf-8")
            parsed = {key: values[-1] for key, values in parse_qs(raw_body, keep_blank_values=True).items()}
            user = repo.authenticate_user(parsed.get("username", ""), parsed.get("password", ""))
            if user is None:
                start_response("401 Unauthorized", [*html_headers(), clear_current_user_cookie()])
                return [login_page(repo, "Неверный логин или пароль")]
            target = "/change-password" if user["must_change_password"] else safe_redirect_target(parsed.get("redirect_to") or "/routes")
            return redirect(start_response, target, [auth_cookie_header(int(user["id"]))])
        if method == "GET" and path == "/change-password":
            if current_user_id is None:
                return redirect(start_response, "/login", [clear_current_user_cookie()] if cookie_id is not None else None)
            if current_user is None or not current_user["must_change_password"]:
                return redirect(start_response, "/routes")
            start_response("200 OK", html_headers())
            return [change_password_page()]
        if method == "POST" and path == "/change-password":
            if current_user_id is None:
                return redirect(start_response, "/login", [clear_current_user_cookie()] if cookie_id is not None else None)
            raw_size = int(environ.get("CONTENT_LENGTH") or "0")
            raw_body = environ["wsgi.input"].read(raw_size).decode("utf-8")
            parsed = {key: values[-1] for key, values in parse_qs(raw_body, keep_blank_values=True).items()}
            password = parsed.get("password", "")
            if not password:
                start_response("400 Bad Request", html_headers())
                return [change_password_page("Пароль не может быть пустым")]
            if password != parsed.get("password_confirm", ""):
                start_response("400 Bad Request", html_headers())
                return [change_password_page("Пароли не совпадают")]
            repo.update_user_password(current_user_id, password, must_change_password=False)
            return redirect(start_response, "/routes")
        if current_user is not None and current_user["must_change_password"] and path not in {"/change-password", "/logout"}:
            return redirect(start_response, "/change-password")
        if not is_public_path(path) and current_user_id is None:
            return redirect(start_response, "/login", [clear_current_user_cookie()] if cookie_id is not None else None)
        if method == "POST":
            raw_size = int(environ.get("CONTENT_LENGTH") or "0")
            raw_body = environ["wsgi.input"].read(raw_size).decode("utf-8")
            parsed = {key: values[-1] for key, values in parse_qs(raw_body, keep_blank_values=True).items()}
            parsed["_raw"] = raw_body
            require_permission("write", section_for_write_path(path))
            if path == "/admin/import/preview":
                if parsed["entity_type"] == "tariffs" and parsed.get("mode") == "replace_section":
                    raise BusinessRuleError("Для тарифов доступен только режим Дополнить / обновить")
                preview = preview_import(conn, parsed["entity_type"], parsed.get("csv_data", ""))
                rows = "".join(f"<tr><td>{r['line']}</td><td>{esc(r['status'])}</td><td>{esc(r['action'])}</td><td>{esc(r['message'])}</td></tr>" for r in preview.rows)
                html_preview = f"<h2>Предпросмотр</h2><p>Всего: {preview.total_rows}, новых: {preview.new_rows}, дублей: {preview.duplicate_rows}, ошибок: {preview.error_rows}</p><table><tr><th>Строка</th><th>Статус</th><th>Действие</th><th>Комментарий</th></tr>{rows}</table>"
                start_response("200 OK", html_headers())
                return [import_page(repo, html_preview, selected_entity=parsed["entity_type"], selected_mode=parsed.get("mode", "append_update"), csv_data=parsed.get("csv_data", ""))]
            if path == "/admin/import/apply":
                result = apply_import(conn, parsed["entity_type"], parsed.get("csv_data", ""), user_id=current_actor_id(), mode=parsed.get("mode", "append_update"))
                notice = f"<h2>Импорт завершён</h2><ul><li>создано {result.created_rows}</li><li>обновлено {result.updated_rows}</li><li>пропущено {result.skipped_rows}</li><li>ошибок {result.error_rows}</li></ul>"
                start_response("200 OK", html_headers())
                return [import_page(repo, notice, selected_entity=parsed["entity_type"], selected_mode=parsed.get("mode", "append_update"), csv_data=parsed.get("csv_data", ""))]
            location = handle_post(repo, path, parsed)
            extra_redirect_headers = None
            if path.startswith("/admin/users/") and path.endswith("/update") and parsed.get("password"):
                target_user_id = int(path.strip("/").split("/")[2])
                if current_user_id is not None and target_user_id == current_user_id:
                    extra_redirect_headers = [clear_current_user_cookie()]
            return redirect(start_response, location, extra_redirect_headers)
        require_permission("read", section_for_get_path(path))
        if path.startswith(("/routes/", "/phones/", "/companies/", "/tariffs/")) and path.endswith("/edit"):
            require_permission("write", section_for_write_path(path.replace("/edit", "/update")))
        if path.startswith("/provider-changes/") and path.endswith("/edit"):
            require_permission("write", "provider_changes")
        filter_redirect = saved_filter_redirect(path, q, filter_state)
        if filter_redirect:
            return redirect(start_response, filter_redirect)
        filter_state, filter_cookie = update_filter_state_for_request(path, q, filter_state)
        _REQUEST_CONTEXT["filter_state"] = filter_state
        if path in {"/", "/dashboard"}: response = dashboard_page(repo)
        elif path == "/routes": response = routes_page(repo, q)
        elif path == "/tariffs": response = tariffs_page(repo, q)
        elif path == "/phones": response = phones_page(repo, q)
        elif path == "/companies": response = companies_page(repo, q)
        elif path == "/calling-companies/history": response = company_events_page(repo, q)
        elif path == "/provider-changes": response = provider_changes_page(repo, q)
        elif path == "/admin": response = admin_page(repo)
        elif path == "/admin/server-priorities": response = server_priorities_page(repo, q)
        elif path == "/admin/company-routing-settings": response = company_routing_settings_page(repo, q)
        elif path == "/admin/naming-rules": response = naming_rules_page(repo)
        elif path == "/admin/import": response = import_page(repo)
        elif path == "/admin/currency-rates": response = currency_rates_page(repo)
        elif path == "/admin/change-reasons": response = change_reasons_page(repo)
        elif path == "/admin/users": response = users_page(repo, q)
        elif path == "/admin/dictionaries": response = dictionaries_page(repo, q)
        elif path == "/admin/telegram": response = telegram_page(repo)
        elif path == "/admin/change-log": response = change_log_page(repo)
        elif path.startswith("/routes/") and path.endswith("/history"): response = route_history_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/phones/") and path.endswith("/history"): response = phone_history_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/tariffs/") and path.endswith("/history"): response = tariff_history_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/calling-companies/") and path.endswith("/history"): response = company_history_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/companies/") and path.endswith("/history"): response = company_history_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/campaign-routing/") and path.endswith("/history"): response = campaign_routing_history_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/routes/") and path.endswith("/edit"): response = route_edit_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/phones/") and path.endswith("/edit"): response = phone_edit_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/tariffs/") and path.endswith("/edit"): response = tariff_edit_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/companies/") and path.endswith("/edit"): response = company_edit_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/provider-changes/") and path.endswith("/edit"): response = provider_change_edit_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/routes/") and path.endswith("/numbers/manage"):
            require_permission("write", "routes")
            response = route_numbers_manage_page(repo, int(path.strip("/").split("/")[1]), q)
        elif path.startswith("/routes/") and path.endswith("/numbers"): response = route_numbers_page(repo, int(path.strip("/").split("/")[1]), q)
        else:
            start_response("404 Not Found", html_headers()); return [page("404", "<h1>404</h1>")]
        extra_headers = [filter_cookie] if filter_cookie else []
        if q.get("export") == "csv" and path in EXPORT_FILENAMES:
            require_permission("export", section_for_get_path(path))
            start_response("200 OK", csv_headers(EXPORT_FILENAMES[path]) + extra_headers)
        else:
            start_response("200 OK", html_headers() + extra_headers)
        return [response]
    except ForbiddenError:
        start_response("403 Forbidden", html_headers())
        return [forbidden_page()]
    except (BusinessRuleError, ValueError, sqlite3.IntegrityError) as exc:
        start_response("400 Bad Request", html_headers())
        return_path = error_return_path(path)
        if return_path.startswith("/routes/") and return_path.endswith("/numbers/manage"):
            route_id = int(return_path.strip("/").split("/")[1])
            return [route_numbers_manage_page(repo, route_id, {"notice": user_error(exc), "notice_type": "error"})]
        if path.startswith("/provider-changes/") and path.endswith("/update"):
            change_id = int(path.strip("/").split("/")[1])
            return [provider_change_edit_page_with_error(repo, change_id, user_error(exc))]
        if path == "/provider-changes/create":
            form_data = {
                "event_at": parsed.get("event_at") or datetime.now().strftime("%Y-%m-%d %H:%M"),
                "apply_scope": parsed.get("apply_scope") or "none",
                "country_id": parse_int(parsed.get("country_id")),
                "server_id": parse_int(parsed.get("server_id")),
                "provider_id": parse_int(parsed.get("campaign_provider_id")) if parsed.get("apply_scope") == "campaign_setting" else parse_int(parsed.get("provider_id")),
                "affected_route_id": parse_int(parsed.get("affected_route_id")),
                "old_route_id": parse_int(parsed.get("old_route_id")),
                "new_route_id": parse_int(parsed.get("new_route_id")),
                "calling_company_id": parse_int(parsed.get("calling_company_id")),
                "calling_company_ids": [parse_int(value) for value in parse_qs(parsed.get("_raw", ""), keep_blank_values=True).get("calling_company_ids", []) if parse_int(value)],
                "company_change_type": parsed.get("company_change_type") or None,
                "new_company_route_id": parse_int(parsed.get("new_company_route_id")),
                "has_overflow": 1 if parsed.get("has_overflow") == "1" else 0,
                "overflow_route_id": parse_int(parsed.get("overflow_route_id")),
                "reason": parsed.get("reason"),
                "comment": parsed.get("comment"),
                "campaign_id_search": parsed.get("campaign_id_search"),
            }
            return [provider_changes_page(repo, {}, form_error=user_error(exc), form_data=form_data)]
        return [validation_error_page(return_path, user_error(exc))]
    finally:
        _REQUEST_CONTEXT.clear()
        conn.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    with make_server("0.0.0.0", port, app) as httpd:
        print(f"Serving on http://127.0.0.1:{port}")
        httpd.serve_forever()
