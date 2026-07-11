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
from datetime import date, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from wsgiref.simple_server import make_server

from app.db import DEFAULT_DB_PATH, DEFAULT_PHONE_ASSIGNMENTS, DEFAULT_PROJECTS, connect, ensure_db_initialized, init_db
from app.importer import apply_import, preview_import
from app.repository import BusinessRuleError, COMPANY_CHANGE_LABELS, ROUTING_SCOPE_LABELS, Repository, normalize_phone_status, normalize_provider_name, normalize_real_prefix, validate_phone_number
from app.telegram import notify_provider_change_created

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOTENV_LOADED = False
DOTENV_SOURCE_KEYS: set[str] = set()
HLR_STARTUP_LOGGED = False


def _parse_dotenv_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key:
                keys.add(key)
    except OSError as exc:
        logger.warning("Could not inspect .env file %s: %s", path, exc)
    return keys


def load_dotenv_if_present(path: str | Path | None = None) -> None:
    """Load project-root .env without overriding OS/hosting environment variables."""
    global DOTENV_LOADED, DOTENV_SOURCE_KEYS
    env_path = Path(path) if path is not None else PROJECT_ROOT / ".env"
    DOTENV_SOURCE_KEYS = _parse_dotenv_keys(env_path)
    if not env_path.exists():
        return
    import importlib.util
    if importlib.util.find_spec("dotenv") is not None:
        import importlib
        dotenv = importlib.import_module("dotenv")
        DOTENV_LOADED = bool(dotenv.load_dotenv(env_path, override=False))
        return
    try:
        before = set(os.environ)
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
        DOTENV_LOADED = bool(set(os.environ) - before)
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
    "unused": "Не используется",
    "free": "Свободен",
    "problem": "Проблемный",
    "unknown": "Не известно",
}


def display_monthly_fee(value: object) -> str:
    return "???" if value is None or str(value).strip() == "" else str(value)


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

    def page_href(page_number: int) -> str:
        params = {key: value for key, value in q.items() if key not in {"page", "limit", "export"} and value not in (None, "")}
        params["page"] = str(page_number)
        return base_path + "?" + urlencode(params)

    previous_link = f"<a class='button pagination-button' href='{esc(page_href(current - 1))}' aria-label='Предыдущая страница'>←</a>" if current > 1 else "<span class='button pagination-button disabled' aria-disabled='true'>←</span>"
    next_link = f"<a class='button pagination-button' href='{esc(page_href(current + 1))}' aria-label='Следующая страница'>→</a>" if current < page_count else "<span class='button pagination-button disabled' aria-disabled='true'>→</span>"
    summary = f"<span class='table-status-item'>Всего записей: {total}</span><span class='table-status-item table-selection-status' data-selected-count hidden>Выбрано: <strong>0</strong></span><span class='table-status-item'>Страница {current} из {page_count}</span>"
    return visible, (
        "<nav class='pagination table-status-nav' aria-label='Статус и пагинация таблицы'>"
        f"<span class='table-status-summary'>{summary}</span>"
        f"<span class='pagination-controls'>{previous_link}{next_link}</span></nav>"
    )


def export_link(base_path: str, q: dict[str, str], *, text: bool = False) -> str:
    section = section_for_get_path(base_path)
    if section and not can_export(section):
        return ""
    params = {key: value for key, value in q.items() if key not in {"page", "limit", "export"} and value not in (None, "")}
    params["export"] = "csv"
    href = esc(base_path + '?' + urlencode(params))
    if text:
        return f"<a class='button export-button table-utility-button' href='{href}' title='Экспорт CSV' aria-label='Экспорт CSV' data-tooltip='Экспорт CSV'>Экспорт CSV</a>"
    return f"<a class='button export-button table-utility-button icon-button' href='{href}' title='Экспорт CSV' aria-label='Экспорт CSV' data-tooltip='Экспорт CSV'>{nav_icon('export')}<span class='sr-only'>Экспорт</span></a><a class='sr-only' href='{href}'>Экспорт</a>"


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




PHONE_IMPORT_TEMPLATE_HEADERS = ["Номер", "Страна", "Провайдер", "Проект", "Назначение", "Итоговый статус", "АП", "АП в EUR", "Тариф", "Комментарий", "Создал", "Создано"]
PHONE_IMPORT_TEMPLATE_ROWS = [
    ["393331234567", "Италия", "Miatel", "Competitors", "ГЛ", "Используется", "", "46,63", "Базовый", "Рабочий номер", "admin_excel", "2026-06-01"],
    ["52555000201", "Мексика", "Telmex", "REP", "АОН", "Отключен", "100", "12.50", "Архивный", "Отключен у провайдера", "legacy_admin", "2026-06-02"],
    ["442071234567", "Великобритания", "", "ИТМ", "", "???", "", "?", "", "Нужна проверка справочных полей", "", "2026-06-03"],
]


def phone_import_template_csv() -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=",")
    writer.writerow(PHONE_IMPORT_TEMPLATE_HEADERS)
    writer.writerows(PHONE_IMPORT_TEMPLATE_ROWS)
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
    {"section_key": "hlr", "display_name": "HLR", "supports_export": True},
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
        "read": {"dashboard", "routes", "tariffs", "phone_numbers", "call_campaigns", "provider_changes", "hlr"},
        "write": {"phone_numbers", "call_campaigns", "provider_changes", "hlr"},
        "export": {"phone_numbers", "provider_changes", "hlr"},
    },
    "duty": {
        "read": {"dashboard", "routes", "tariffs", "phone_numbers", "call_campaigns", "provider_changes", "hlr"},
        "write": {"phone_numbers", "call_campaigns", "provider_changes", "hlr"},
        "export": {"phone_numbers", "provider_changes", "hlr"},
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
    "dashboard": material_icon("home"),
    "routes": material_icon("route"),
    "tariffs": material_icon("sell"),
    "phones": material_icon("sim_card"),
    "companies": material_icon("campaign"),
    "provider_changes": material_icon("sync_alt"),
    "hlr": material_icon("fact_check"),
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
    ("provider_changes", "/provider-changes", "Смена провайдеров", ("Смена провайдеров", "Редактировать событие")),
    ("routes", "/routes", "Маршруты", ("Маршруты", "Номера маршрута", "Редактировать маршрут")),
    ("tariffs", "/tariffs", "Тарифы", ("Тарифы",)),
    ("phones", "/phones", "Купленные номера", ("Купленные номера", "Редактировать номер")),
    ("companies", "/companies", "Кампании прозвона", ("Кампании прозвона", "Редактировать кампанию")),
    ("hlr", "/hlr", "HLR", ("HLR",)),
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
        <details class="current-user-selector" data-tooltip="{esc(current_label)}">
          <summary aria-label="Меню пользователя">
            <span class="side-icon user-icon" aria-hidden="true">{user_icon_svg()}</span>
            <span class="user-copy"><strong>{esc(current_label)}</strong></span>
            <span class="user-caret" aria-hidden="true">▾</span>
          </summary>
          <div class="current-user-menu">
            <div class="current-user-menu-info"><strong>{esc(current['display_name'])}</strong><small>{esc(role_label(current['role_key']))} · Текущий пользователь</small></div>
            <a class="logout-link" href="/logout">Выйти</a>
          </div>
        </details>
    """

def theme_selector() -> str:
    return f"""
        <div class="theme-selector-wrap" data-theme-selector data-tooltip="Тема: Светлая 2.0">
          <button class="theme-selector" type="button" data-theme-menu-toggle aria-haspopup="menu" aria-expanded="false"><span class="side-icon" aria-hidden="true">{nav_icon("theme")}</span><span class="side-label" data-theme-current>Тема: Светлая 2.0 ▾</span></button>
          <div class="theme-menu" data-theme-menu role="menu" aria-label="Выбор темы">
            <button type="button" role="menuitemradio" data-theme-option="light-v2" aria-checked="true"><span class="theme-check" aria-hidden="true">{nav_icon("check")}</span><span>Светлая 2.0</span></button>
            <button type="button" role="menuitemradio" data-theme-option="dark" aria-checked="false"><span class="theme-check" aria-hidden="true">{nav_icon("check")}</span><span>Тёмная</span></button>
            <button type="button" role="menuitemradio" data-theme-option="tele-route-pro" aria-checked="false"><span class="theme-check" aria-hidden="true">{nav_icon("check")}</span><span>TeleRoute Pro</span></button>
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
        "HLR": [("Главная", "/dashboard"), ("HLR", None)],
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
      --bg: #F6F7F8;
      --surface: #FFFFFF;
      --surface-muted: #F8FAF9;
      --surface-soft: #F3F4F6;
      --surface-strong: #EEF1F3;
      --table-header-bg: #F3F4F6;
      --table-row-alt: #FBFCFC;
      --table-row-hover: #F1F7F6;
      --sidebar-bg: #FFFFFF;
      --text-strong: #0F172A;
      --text: #1C2733;
      --muted: #475867;
      --text-soft: #667481;
      --border: #D2DAE1;
      --border-strong: #AEBBC6;
      --border-ink: #8D9BA7;
      --accent: #0F766E;
      --accent-strong: #115E59;
      --accent-hover: #0B5F59;
      --accent-soft: #E6F3F1;
      --accent-border: #A7D8D2;
      --cyber: #0F766E;
      --cyber-strong: #115E59;
      --cyber-soft: #E6F3F1;
      --pink: #6F7A3A;
      --pink-soft: #F0F2E6;
      --olive: #6F7A3A;
      --olive-soft: #F0F2E6;
      --warning: #D97706;
      --warning-hover: #B45309;
      --warning-soft: #FFF3E2;
      --warning-border: #F2C078;
      --provider-accent: #D97706;
      --provider-hover: #B45309;
      --provider-soft: #FFF3E2;
      --provider-border: #F2C078;
      --danger: #DC2626;
      --danger-strong: #B91C1C;
      --danger-soft: #FEE2E2;
      --danger-border: #FCA5A5;
      --success: #2F7D50;
      --success-soft: #EAF6EE;
      --success-border: #B9DEC7;
      --input-bg: #FFFFFF;
      --focus: #0F766E;
      --shadow-soft: 0 1px 2px rgba(31, 41, 51, .045);
      --shadow-card: 0 2px 8px rgba(31, 41, 51, .055);
      --shadow-card-hover: 0 4px 12px rgba(31, 41, 51, .075);
      --shadow-glow: 0 0 0 1px rgba(15, 118, 110, 0.14), 0 4px 10px rgba(15, 118, 110, 0.08);
      --radius-control: 6px;
      --radius-card: 10px;
    }}
    html[data-theme="tele-route-pro"] {{
      --bg: #f4f6f9;
      --surface: #ffffff;
      --surface-muted: #f8fafc;
      --surface-soft: #f3f6fa;
      --surface-strong: #eef2f7;
      --table-header-bg: #f3f6fa;
      --table-row-alt: #f8fafc;
      --table-row-hover: #eff6ff;
      --sidebar-bg: #ffffff;
      --text-strong: #111827;
      --text: #243244;
      --muted: #667085;
      --text-soft: #667085;
      --border: #e5eaf1;
      --border-strong: #d7dee8;
      --border-ink: #b8c2cf;
      --accent: #2563eb;
      --accent-strong: #1d4ed8;
      --accent-hover: #1e40af;
      --accent-soft: #eff6ff;
      --accent-border: #bfdbfe;
      --cyber: #2563eb;
      --cyber-strong: #1d4ed8;
      --cyber-soft: #eff6ff;
      --pink: #2563eb;
      --pink-soft: #eff6ff;
      --olive: #2563eb;
      --olive-soft: #eff6ff;
      --success: #16a34a;
      --success-soft: #dcfce7;
      --success-border: #bbf7d0;
      --warning: #f59e0b;
      --warning-hover: #d97706;
      --warning-soft: #fef3c7;
      --warning-border: #fde68a;
      --provider-accent: #f59e0b;
      --provider-hover: #d97706;
      --provider-soft: #fef3c7;
      --provider-border: #fde68a;
      --danger: #dc2626;
      --danger-strong: #b91c1c;
      --danger-soft: #fee2e2;
      --danger-border: #fecaca;
      --input-bg: #ffffff;
      --focus: #2563eb;
      --shadow-soft: 0 1px 2px rgba(17, 24, 39, .045);
      --shadow-card: 0 2px 8px rgba(17, 24, 39, .055);
      --shadow-card-hover: 0 4px 12px rgba(17, 24, 39, .075);
      --shadow-glow: 0 0 0 1px rgba(37, 99, 235, .14), 0 4px 10px rgba(37, 99, 235, .08);
      --radius-control: 6px;
      --radius-card: 10px;
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
    .page-top {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin: 0 0 10px; }}
    .page-crumbs {{ min-width: 0; }}
    .topbar {{ display: flex; justify-content: flex-end; align-items: center; gap: 8px; min-height: 36px; margin: 0; flex-wrap: wrap; }}
    .current-user-selector summary, .theme-selector {{ display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; font-weight: 700; }}
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
    .server-checkbox-item input[type="checkbox"] {{ flex: 0 0 14px; margin: 0; }}
    .server-checkbox-item:has(input:checked) {{ border-color: var(--accent); background: var(--accent-soft); color: var(--text-strong); box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 24%, transparent) inset; }}
    .server-checkbox-copy {{ min-width: 0; display: inline-flex; align-items: center; }}
    .server-checkbox-main {{ font-weight: 760; line-height: 1.15; color: inherit; }}
    .server-current-routes {{ display: grid; gap: 5px; max-height: 210px; overflow: auto; margin-top: 10px; padding: 9px 10px; border: 1px solid var(--border); border-radius: var(--radius-control); background: var(--surface); }}
    .server-current-route-row {{ display: grid; grid-template-columns: 34px minmax(0, 1fr); gap: 6px; align-items: baseline; min-height: 20px; font-size: 12px; line-height: 1.25; }}
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
    .provider-changes-page .form-summary::after, .provider-changes-page details[open] > .form-summary::after {{ content: none; }}
    .provider-changes-page .provider-change-primary-summary {{ width: max-content; min-height: 34px; box-sizing: border-box; margin: 0; padding: 7px 12px; border: 1px solid #2563eb !important; border-radius: var(--radius-control); background: #2563eb !important; color: #fff !important; box-shadow: 0 4px 10px rgba(37, 99, 235, .16); font-weight: 760; transition: background-color 140ms ease, border-color 140ms ease, box-shadow 140ms ease, color 140ms ease; }}
    .provider-changes-page .provider-change-primary-summary:hover {{ border-color: #1d4ed8 !important; background: #1d4ed8 !important; color: #fff !important; }}
    .provider-changes-page .provider-change-primary-summary:focus-visible {{ outline: none; border-color: #1d4ed8 !important; background: #2563eb !important; color: #fff !important; box-shadow: 0 0 0 3px rgba(37, 99, 235, .22); }}
    .provider-changes-page .provider-change-primary-summary:active, .provider-changes-page .provider-change-create-shell[open] > .provider-change-primary-summary {{ border-color: #1e40af !important; background: #1e40af !important; color: #fff !important; }}
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
    .provider-changes-page .filter-grid .checkbox-inline {{ min-height: 34px; box-sizing: border-box; padding: 6px 10px; align-self: end; border: 1px solid var(--border-strong); border-radius: var(--radius-control); background: var(--input-bg, var(--surface)); white-space: nowrap; }}
    .provider-changes-page .filter-grid .checkbox-inline input[type='checkbox'] {{ width: 16px; height: 16px; min-height: 16px; flex: 0 0 16px; margin: 0; accent-color: var(--accent); }}
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
    .column-settings-panel {{ position: absolute; right: 0; top: calc(100% + 6px); z-index: 30; display: grid; gap: 8px; min-width: 300px; max-height: min(380px, 70vh); overflow: auto; overscroll-behavior: contain; padding: 10px; border: 1px solid var(--border-strong); border-radius: var(--radius-control); background: #fff; box-shadow: var(--shadow-card); }}
    .column-settings-panel.open-up {{ top: auto; bottom: calc(100% + 6px); }}
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
    .hlr-workspace {{ display: grid; gap: 10px; min-width: 0; overflow-x: clip; }}
    .hlr-tech-spec {{ border: 1px solid var(--border); border-radius: var(--radius-card); background: var(--surface); box-shadow: var(--shadow-soft); overflow: visible; }}
    .hlr-tech-spec > summary {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; cursor: pointer; font-weight: 820; list-style: none; }}
    .hlr-tech-spec > summary::-webkit-details-marker {{ display: none; }}
    .hlr-tech-spec-title::before {{ content: "▶"; display: inline-block; width: 18px; color: var(--muted); }}
    .hlr-tech-spec[open] .hlr-tech-spec-title::before {{ content: "▼"; }}
    .hlr-tech-spec-summary {{ display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; color: var(--muted); font-size: 12px; font-weight: 720; }}
    .hlr-tech-spec-body {{ display: grid; grid-template-columns: minmax(280px, 1fr) minmax(360px, 1fr); gap: 12px; padding: 0 12px 12px; }}
    .hlr-input-panel {{ display: flex; margin: 0; }}
    .hlr-input-form {{ display: grid; grid-template-rows: minmax(220px, 1fr) auto auto auto; gap: 6px; align-items: end; width: 100%; padding: 12px; }}
    .hlr-input-form label {{ display: grid; grid-template-rows: auto minmax(190px, 1fr); gap: 5px; align-self: stretch; min-width: 0; width: 100%; }}
    .hlr-input-form textarea {{ min-width: 0; width: 100%; height: 100%; min-height: 190px; max-height: none; resize: vertical; box-sizing: border-box; }}
    .hlr-counter-line, .hlr-input-hint {{ margin: 0; }}
    .hlr-input-hint {{ align-self: end; line-height: inherit; }}
    .hlr-input-actions {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; align-self: end; }}
    .hlr-daily-limit-admin {{ display: grid; gap: 8px; padding: 12px; border-bottom: 1px solid var(--border); }}
    .hlr-daily-limit-title {{ margin: 0; font-weight: 820; }}
    .hlr-daily-limit-form {{ display: flex; align-items: end; gap: 8px; flex-wrap: wrap; }}
    .hlr-daily-limit-form label {{ display: grid; gap: 4px; font-weight: 720; }}
    .hlr-daily-limit-form input {{ width: 130px; }}
    .hlr-daily-limit-meta {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; margin: 0; }}
    .hlr-daily-limit-meta span {{ display: block; color: var(--muted); font-size: 12px; font-weight: 720; }}
    .hlr-daily-limit-meta strong {{ display: block; color: var(--text-strong); }}
    .hlr-progress {{ display: none; align-items: center; flex: 1 1 320px; min-width: min(320px, 100%); max-width: calc(100% - 8px); min-height: 36px; }}
    .hlr-progress.is-active {{ display: inline-flex; }}
    .hlr-progress-track {{ position: relative; display: block; width: 100%; height: 30px; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-small); background: var(--surface-muted); box-shadow: inset 0 1px 2px color-mix(in srgb, var(--text-strong) 10%, transparent); }}
    .hlr-progress-bar {{ position: absolute; inset: 0 auto 0 0; width: 45%; border-radius: inherit; background: repeating-linear-gradient(45deg, color-mix(in srgb, var(--accent) 82%, var(--surface)), color-mix(in srgb, var(--accent) 82%, var(--surface)) 8px, color-mix(in srgb, var(--accent) 58%, var(--surface)) 8px, color-mix(in srgb, var(--accent) 58%, var(--surface)) 16px); animation: hlr-progress-slide 1s linear infinite; }}
    @keyframes hlr-progress-slide {{ 0% {{ transform: translateX(-110%); }} 100% {{ transform: translateX(230%); }} }}
    .hlr-severity-good, .hlr-severity-green {{ border-color: color-mix(in srgb, var(--success) 60%, var(--border)); background: color-mix(in srgb, var(--success) 12%, var(--surface)); color: var(--success, var(--text-strong)); }}
    .hlr-severity-neutral, .hlr-severity-unknown {{ border-color: var(--border); background: color-mix(in srgb, var(--surface-muted) 70%, var(--surface)); color: var(--muted); }}
    .hlr-severity-bad, .hlr-severity-red {{ border-color: color-mix(in srgb, #dc2626 72%, var(--border)); background: color-mix(in srgb, #dc2626 12%, var(--surface)); color: #b91c1c; }}
    .hlr-severity-warning, .hlr-severity-yellow, .hlr-severity-orange {{ border-color: color-mix(in srgb, #d6b94f 46%, var(--border)); background: color-mix(in srgb, #fffbea 62%, var(--surface)); color: #5f4700; }}
    .hlr-severity-api_error {{ border-color: color-mix(in srgb, var(--danger) 55%, var(--border)); background: repeating-linear-gradient(135deg, color-mix(in srgb, var(--danger) 9%, var(--surface)), color-mix(in srgb, var(--danger) 9%, var(--surface)) 6px, var(--surface-muted) 6px, var(--surface-muted) 12px); color: var(--danger); }}
    #hlr-table tbody tr.hlr-row-severity-bad td, #hlr-table tbody tr.hlr-row-severity-red td {{ background: color-mix(in srgb, #dc2626 5%, var(--surface)); }}
    #hlr-table tbody tr.hlr-row-severity-api_error td {{ background: color-mix(in srgb, #dc2626 3%, var(--surface)); }}
    #hlr-table tbody tr.hlr-row-severity-warning td, #hlr-table tbody tr.hlr-row-severity-yellow td, #hlr-table tbody tr.hlr-row-severity-orange td {{ background: color-mix(in srgb, #fffbea 48%, var(--surface)); }}
    #hlr-table tbody tr.hlr-row-severity-good td, #hlr-table tbody tr.hlr-row-severity-green td {{ background: color-mix(in srgb, var(--success) 4%, var(--surface)); }}
    #hlr-table tbody tr.hlr-row-severity-neutral td, #hlr-table tbody tr.hlr-row-severity-unknown td {{ background: color-mix(in srgb, var(--surface-muted) 55%, var(--surface)); }}
    #hlr-table tbody tr td[data-col='comment'] .hlr-cell-text {{ color: inherit; }}
    #hlr-table tbody tr.hlr-row-severity-bad td[data-col='comment'] .hlr-cell-text, #hlr-table tbody tr.hlr-row-severity-red td[data-col='comment'] .hlr-cell-text, #hlr-table tbody tr.hlr-row-severity-api_error td[data-col='comment'] .hlr-cell-text {{ color: var(--danger); }}
    #hlr-table tbody tr.hlr-row-severity-warning td[data-col='comment'] .hlr-cell-text, #hlr-table tbody tr.hlr-row-severity-yellow td[data-col='comment'] .hlr-cell-text, #hlr-table tbody tr.hlr-row-severity-orange td[data-col='comment'] .hlr-cell-text {{ color: #5f4700; }}
    .hlr-demo-note {{ margin-left: 8px; font-size: 12px; font-weight: 500; }}
    .hlr-table-toolbar {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; margin-top: 0; }}
    .hlr-side-panel {{ display: grid; align-content: start; gap: 8px; min-width: 0; }}
    .hlr-filter-panel {{ display: block; min-width: 0; }}
    .hlr-filter-group {{ display: grid; gap: 10px; padding: 12px; border: 1px solid var(--border); border-radius: var(--radius-card); background: var(--surface-muted); }}
    .hlr-filter-group-title {{ color: var(--text-strong); font-size: 12px; font-weight: 840; text-transform: uppercase; letter-spacing: .04em; }}
    .hlr-filter-service-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
    .hlr-filter-status-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }}
    .hlr-filter-chip {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 42px; border: 1px solid var(--border); border-radius: var(--radius-small); padding: 7px 9px; background: var(--surface); color: var(--text); box-shadow: none; font-size: 11px; font-weight: 800; line-height: 1.15; text-align: left; white-space: normal; overflow-wrap: anywhere; }}
    .hlr-filter-chip:not(.is-empty) {{ cursor: pointer; }}
    .hlr-filter-chip.is-empty {{ opacity: .45; cursor: not-allowed; filter: grayscale(.25); }}
    .hlr-filter-chip.is-active:not(.is-empty),
    .hlr-filter-chip[aria-pressed="true"]:not(.is-empty) {{ border-color: var(--accent); background: color-mix(in srgb, var(--accent) 18%, var(--surface)); color: var(--text-strong); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 45%, transparent), 0 0 0 2px color-mix(in srgb, var(--accent) 18%, transparent); }}
    .hlr-filter-chip.is-active:not(.is-empty) .hlr-filter-count,
    .hlr-filter-chip[aria-pressed="true"]:not(.is-empty) .hlr-filter-count {{ color: var(--text-strong); }}
    .hlr-filter-chip.is-empty,
    .hlr-filter-chip.is-empty[aria-pressed="true"] {{ border-color: var(--border); background: var(--surface); box-shadow: none; color: var(--muted); }}
    .hlr-filter-chip:focus-visible {{ outline: 3px solid color-mix(in srgb, var(--accent) 65%, transparent); outline-offset: 2px; }}
    .hlr-filter-chip.hlr-status-live {{ border-color: color-mix(in srgb, var(--success) 65%, var(--border)); background: color-mix(in srgb, var(--success) 12%, var(--surface)); }}
    .hlr-filter-chip.hlr-status-dead, .hlr-filter-chip.hlr-status-bad_format {{ border-color: color-mix(in srgb, #dc2626 72%, var(--border)); background: color-mix(in srgb, #dc2626 12%, var(--surface)); color: #b91c1c; }}
    .hlr-filter-chip.hlr-status-absent_subscriber, .hlr-filter-chip.hlr-status-no_teleservice_provisioned, .hlr-filter-chip.hlr-status-not_available_network_only, .hlr-filter-chip.hlr-status-no_coverage, .hlr-filter-chip.hlr-status-inconclusive {{ border-color: color-mix(in srgb, #d6b94f 46%, var(--border)); background: color-mix(in srgb, #fffbea 62%, var(--surface)); color: #5f4700; }}
    .hlr-filter-chip.hlr-status-not_applicable {{ border-color: var(--border); background: var(--surface); color: var(--muted); }}
    .hlr-filter-count {{ flex: 0 0 auto; color: var(--muted); font-weight: 900; }}
    .hlr-table-empty-message {{ margin: 10px 0 0; }}
    .hlr-copy-status {{ min-width: 150px; color: var(--muted); font-size: 12px; font-weight: 720; }}
    .hlr-copy-status.is-success {{ color: var(--success); }}
    .hlr-copy-status.is-error {{ color: var(--danger); }}
    .hlr-column-manager {{ position: relative; display: inline-flex; }}
    .hlr-column-panel {{ position: absolute; right: 0; top: calc(100% + 6px); z-index: 20; display: none; width: min(420px, 88vw); max-height: min(430px, 70vh); overflow: auto; overscroll-behavior: contain; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius-card); background: var(--surface); box-shadow: var(--shadow-card); }}
    .hlr-column-panel.is-open {{ display: grid; gap: 8px; }}
    .hlr-column-panel.open-up {{ top: auto; bottom: calc(100% + 6px); }}
    .hlr-column-list {{ display: grid; gap: 6px; }}
    .hlr-column-item {{ display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 6px; padding: 6px; border: 1px solid var(--border); border-radius: var(--radius-small); background: var(--surface-muted); }}
    .hlr-column-item label {{ display: flex; align-items: center; gap: 7px; min-width: 0; margin: 0; font-weight: 650; }}
    .hlr-column-item span {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .hlr-column-move {{ min-width: 32px; padding: 3px 7px; box-shadow: none; }}
    .hlr-column-panel-actions {{ display: flex; justify-content: space-between; gap: 8px; align-items: center; }}
    .column-drag-handle {{ color: var(--muted); margin-right: 4px; cursor: grab; }}
    .hlr-results-area, .hlr-results-area .table-card {{ min-width: 0; overflow: hidden; }}
    .hlr-results-area .table-card {{ margin-top: 0; min-height: 320px; }}
    .hlr-results-area .table-scroll {{ min-height: 520px; max-height: calc(100vh - 170px); overflow: auto; }}
    #hlr-table {{ table-layout: fixed; width: max-content; min-width: 100%; }}
    #hlr-table thead {{ position: sticky; top: 0; z-index: 3; }}
    #hlr-table th {{ position: sticky; top: 0; z-index: 2; }}
    #hlr-table th.is-drag-resizing {{ border-right-color: var(--accent); }}
    #hlr-table tbody tr[hidden] {{ display: none !important; }}
    #hlr-table th, #hlr-table td {{ max-width: none; overflow: hidden; padding: 7px 9px; }}
    #hlr-table .status-badge {{ min-height: 20px; padding: 1px 7px; }}
    .hlr-cell-content {{ display: flex; align-items: center; gap: 6px; min-width: 0; }}
    .hlr-cell-text {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .hlr-cell-text.hlr-long-text {{ display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; white-space: normal; }}
    .hlr-details-stack {{ display: grid; gap: 6px; min-width: 0; }}
    .hlr-api-fields {{ padding: 0; margin: 0; border: 1px solid var(--border); border-radius: var(--radius-small); background: var(--surface); box-shadow: none; overflow: hidden; }}
    .hlr-api-fields > summary {{ display: flex; align-items: center; min-height: 34px; padding: 6px 10px; cursor: pointer; font-weight: 760; list-style-position: inside; }}
    .hlr-api-fields > summary:focus-visible {{ outline: 3px solid color-mix(in srgb, var(--accent) 55%, transparent); outline-offset: -3px; }}
    .hlr-api-fields[open] {{ background: var(--surface-muted); }}
    .hlr-api-fields[open] > summary {{ border-bottom: 1px solid var(--border); background: var(--surface); }}
    .hlr-api-fields > :not(summary) {{ margin: 10px; }}
    .hlr-api-fields .hlr-api-fields {{ margin: 10px; background: var(--surface); }}
    .hlr-api-field-list {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
    .hlr-api-field-list code {{ padding: 2px 6px; border-radius: 999px; background: var(--surface-muted); border: 1px solid var(--border); font-size: 12px; }}
    .hlr-usage-dashboard {{ display: grid; gap: 10px; min-width: 0; padding: 12px; border: 1px solid var(--border); border-radius: var(--radius-card); background: var(--surface-muted); }}
    .hlr-usage-title {{ margin: 0; color: var(--text-strong); font-size: 12px; font-weight: 840; letter-spacing: .04em; text-transform: uppercase; }}
    .hlr-usage-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border: 1px solid var(--border); border-radius: var(--radius-small); background: var(--surface); overflow: hidden; }}
    .hlr-usage-cell {{ display: grid; gap: 3px; min-width: 0; padding: 8px 9px; border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); background: color-mix(in srgb, var(--surface-muted) 45%, var(--surface)); }}
    .hlr-usage-balance-cell {{ position: relative; padding-right: 42px; }}
    .hlr-usage-header {{ display: flex; align-items: center; justify-content: space-between; gap: 6px; min-width: 0; }}
    .hlr-usage-cell:nth-child(2n) {{ border-right: 0; }}
    .hlr-usage-cell:nth-last-child(-n+2) {{ border-bottom: 0; }}
    .hlr-usage-label {{ color: var(--muted); font-size: 11px; font-weight: 760; }}
    .hlr-usage-value {{ color: var(--text-strong); font-size: 15px; font-weight: 850; line-height: 1.15; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .hlr-usage-meta {{ color: var(--muted); font-size: 11px; line-height: 1.2; }}
    .hlr-usage-cell.is-warning .hlr-usage-value {{ color: var(--warning); }}
    .hlr-usage-cell.is-danger .hlr-usage-value {{ color: var(--danger); }}
    .hlr-balance-refresh {{ position: absolute; top: 6px; right: 7px; display: inline-grid; place-items: center; width: 28px; height: 28px; min-height: 0; padding: 0; border-radius: 999px; box-shadow: none; }}
    .hlr-balance-refresh .material-symbols-rounded {{ font-size: 17px; }}
    .hlr-balance-refresh[aria-busy="true"] .material-symbols-rounded {{ animation: hlr-refresh-spin .8s linear infinite; }}
    @keyframes hlr-refresh-spin {{ to {{ transform: rotate(360deg); }} }}
    .hlr-help-card {{ padding: 0; margin: 0; border: 0; background: transparent; }}
    .hlr-help-card h3 {{ margin: 0 0 8px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
    .hlr-help-list {{ display: grid; gap: 7px; margin: 0; }}
    .hlr-help-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: center; }}
    .hlr-help-info {{ cursor: help; color: var(--accent); font-weight: 900; }}
    @media (max-width: 900px) {{ .hlr-tech-spec-body {{ grid-template-columns: 1fr; }} .hlr-filter-status-grid {{ grid-template-columns: 1fr; }} .hlr-input-form textarea {{ min-height: 150px; }} .hlr-results-area .table-scroll {{ max-height: calc(100vh - 300px); }} }}
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
    .route-numbers-cell {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; min-width: 190px; }}
    .route-numbers-label {{ min-width: 0; text-align: left; }}
    .route-numbers-action {{ margin-left: auto; min-height: 28px; padding: 4px 8px; font-size: 12px; box-shadow: none; white-space: nowrap; }}
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
      background: rgba(15, 23, 42, .40);
    }}
    td[data-col="actions"] details.edit-details[open] > summary {{ z-index: 1001; }}
    td[data-col="actions"] details.edit-details > form input,
    td[data-col="actions"] details.edit-details > form select,
    td[data-col="actions"] details.edit-details > form textarea {{
      width: 100%;
      box-sizing: border-box;
    }}
    td[data-col="actions"] details.edit-details > form label {{
      display: grid;
      gap: 5px;
      width: 100%;
      color: var(--muted);
      font-size: 12px;
      font-weight: 760;
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
    .modal-form-card[open]::before, .modal-overlay {{ content: ""; position: fixed; inset: 0; z-index: 980; background: rgba(15, 23, 42, 0.42); }}
    .modal-form-card[open] > form, .modal-form-card[open] > .modal-body, .modal-card {{ position: fixed; left: 50%; top: 50%; z-index: 990; width: min(1040px, calc(100vw - 32px)); max-height: calc(100vh - 48px); overflow: auto; scrollbar-gutter: stable; transform: translate(-50%, -50%); margin: 0; padding: 20px; border: 1px solid var(--border-strong); border-radius: 18px; background: var(--surface); color: var(--text); box-shadow: 0 24px 80px rgba(0,0,0,.28); box-sizing: border-box; }}
    .modal-card form, .modal-form-card[open] > form {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .modal-card form label, .modal-card form fieldset, .modal-form-card[open] > form label, .modal-form-card[open] > form fieldset {{ min-width: 0; }}
    .modal-card form .wide, .modal-card form p, .modal-card form fieldset, .modal-form-card[open] > form .wide, .modal-form-card[open] > form p, .modal-form-card[open] > form fieldset {{ grid-column: 1 / -1; }}
    .modal-card h2 {{ margin: 0 0 4px; color: var(--text-strong); }}
    .modal-description {{ margin: 0 0 16px; color: var(--muted); }}
    .modal-actions {{ grid-column: 1 / -1; display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; padding-top: 12px; border-top: 1px solid var(--border); }}
    .modal-save, .admin-edit-save {{ background: var(--accent-strong); border-color: var(--accent-strong); color: #fff; font-weight: 780; }}
    .modal-save:hover, .admin-edit-save:hover {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    .modal-cancel, .admin-edit-cancel {{ background: var(--surface); color: var(--text); border-color: var(--border-strong); }}
    .provider-change-create-shell[open] > #routing-event-form {{ min-height: min(740px, calc(100vh - 48px)); grid-template-rows: auto auto minmax(0, 1fr) auto auto; align-content: start; }}
    .provider-change-create-shell[open] > #routing-event-form [data-scope-content]:not([hidden]) {{ align-self: stretch; }}
    .provider-change-create-shell[open] > #routing-event-form .provider-change-shell-hint {{ align-self: end; }}
    .provider-change-create-shell[open] > #routing-event-form > button[type='submit'] {{ align-self: end; justify-self: end; }}
    .modal-cancel:hover, .admin-edit-cancel:hover {{ background: var(--warning-soft); color: var(--accent-strong); border-color: var(--warning-border); }}
    .modal-card input, .modal-card select, .modal-card textarea, .modal-form-card[open] input, .modal-form-card[open] select, .modal-form-card[open] textarea {{ width: 100%; box-sizing: border-box; background: var(--input-bg, var(--surface)); color: var(--text); border-color: var(--border-strong); }}
    html[data-theme="dark"] .modal-card, html[data-theme="dark"] .modal-form-card[open] > form, html[data-theme="dark"] .modal-form-card[open] > .modal-body {{ background: var(--surface); border-color: var(--border-strong); color: var(--text); }}
    html[data-theme="dark"] .modal-overlay, html[data-theme="dark"] .modal-form-card[open]::before {{ background: rgba(0, 0, 0, 0.55); }}
    .modal-form-card[open] > form.currency-rate-form {{ width: min(760px, calc(100vw - 32px)); grid-template-columns: minmax(220px, .85fr) minmax(320px, 1.15fr); align-items: end; }}
    .currency-rate-form .currency-rate-value {{ min-width: 0; }}
    .currency-rate-inline {{ display: grid; grid-template-columns: max-content minmax(130px, 1fr) max-content; align-items: center; gap: 8px; margin-top: 4px; white-space: nowrap; }}
    .currency-rate-prefix, .currency-rate-suffix {{ color: var(--muted); font-size: 13px; font-weight: 700; }}
    .currency-rate-actions {{ margin-top: 8px; }}
    @media (max-width: 720px) {{ .modal-form-card[open] > form.currency-rate-form {{ grid-template-columns: 1fr; }} .currency-rate-inline {{ grid-template-columns: 1fr; align-items: stretch; white-space: normal; }} }}
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
      --bg: #f4f6f9; --surface: #ffffff; --surface-muted: #fbfcfe; --surface-soft: #f7f9fc; --surface-strong: #f2f5f9;
      --table-header-bg: #fafbfe; --table-row-alt: #fcfdff; --table-row-hover: #f8fafc;
      --sidebar-bg: #ffffff; --text-strong: #111827; --text: #243244; --muted: #667085; --text-soft: #7b8794;
      --border: #e5eaf1; --border-strong: #d7dee8; --border-ink: #a8b2c1;
      --accent: #394150; --accent-strong: #1f2937; --accent-hover: #fff6e6; --accent-soft: #f4f6f8; --accent-border: #cfd6df;
      --cyber: #475569; --cyber-strong: #334155; --cyber-soft: #f4f6f8;
      --success: #2f7d50; --success-soft: #eaf6ee; --success-border: #b9dec7;
      --warning: #f59e0b; --warning-hover: #d97706; --warning-soft: #fff6e6; --warning-border: #f6c979;
      --danger: #dc2626; --danger-strong: #b91c1c; --danger-soft: #fff1f1; --danger-border: #f3b5b5;
      --input-bg: #ffffff; --focus: #f59e0b;
      --shadow-soft: 0 2px 8px rgba(15, 23, 42, .045); --shadow-card: 0 10px 24px rgba(15, 23, 42, .07); --shadow-card-hover: 0 14px 30px rgba(15, 23, 42, .10);
      --shadow-glow: 0 0 0 1px rgba(245, 158, 11, .16), 0 10px 22px rgba(15, 23, 42, .08);
      --radius-control: 8px; --radius-card: 14px;
    }}
    body {{ background: var(--bg); color: var(--text); font-size: 14px; }}
    .app-shell {{ grid-template-columns: 260px minmax(0, 1fr); background: var(--bg); }}
    .workspace {{ padding: 0; min-width: 0; }}
    .content {{ padding: 18px 30px 42px; }}
    .content > h1:first-of-type {{ margin-bottom: 12px; padding-bottom: 8px; }}
    .page-top {{ margin: -18px -30px 14px; padding: 10px 30px; min-height: 56px; border-bottom: 1px solid var(--border); background: #fafbfe; }}
    .breadcrumbs {{ margin: 0; padding: 0; min-height: auto; display: flex; align-content: center; border-bottom: 0; background: transparent; }}
    .breadcrumbs::after {{ content: none; }}
    .breadcrumbs .separator {{ font-size: 0; }} .breadcrumbs .separator::before {{ content: '›'; font-size: 12px; }}
    .sidebar {{ display: flex; flex-direction: column; gap: 14px; padding: 14px 12px; background: #fff; height: 100vh; max-height: 100vh; overflow: hidden; z-index: 100; box-sizing: border-box; }}
    .sidebar-head {{ display: grid; grid-template-columns: minmax(0, 1fr) 42px; gap: 8px; align-items: start; padding-bottom: 14px; border-bottom: 1px solid var(--border); }}
    .brand-block {{ display: flex; align-items: center; gap: 12px; padding: 0 0 0 10px; border-bottom: 0; }}
    .brand-mark, .side-icon, .metric-icon, .quick-icon, .feed-icon {{ display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; }}
    .brand-mark {{ width: 36px; height: 36px; border-radius: 11px; background: linear-gradient(135deg,#394150,#1f2937); color: #fff; box-shadow: 0 8px 18px rgba(31,41,55,.20); font-weight: 900; }}
    .brand-copy strong, .brand-copy span {{ display: block; }} .brand-copy strong {{ color: var(--text-strong); }} .brand-copy span {{ color: var(--muted); font-size: 12px; }}
    .app-title {{ display: none; }}
    .side-nav {{ --sidebar-item-height: 40px; --sidebar-item-padding-y: 7px; --sidebar-item-padding-x: 12px; --sidebar-item-gap: 10px; display: grid; grid-auto-rows: min-content; align-content: start; gap: 6px; flex: 1 1 auto; min-height: 0; overflow-y: auto; overflow-x: hidden; overscroll-behavior: contain; padding-right: 10px; scrollbar-gutter: stable; }}
    @supports (scrollbar-gutter: stable both-edges) {{
      .side-nav {{ scrollbar-gutter: stable; }}
    }}
    .side-link {{ justify-content: flex-start; gap: var(--sidebar-item-gap); height: var(--sidebar-item-height); min-height: var(--sidebar-item-height); max-height: var(--sidebar-item-height); padding: var(--sidebar-item-padding-y) var(--sidebar-item-padding-x); line-height: 1.2; border-radius: 12px; color: #223158; font-weight: 700; }}
    .side-icon {{ width: 22px; height: 22px; color: #7786ad; font-size: 18px; }}
    .nav-icon {{ display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; flex: 0 0 24px; color: #7786ad; }}
    .nav-icon, .side-icon, .metric-icon, .quick-icon, .feed-icon {{ display: inline-flex; align-items: center; justify-content: center; }}
    .side-link:hover .nav-icon, .side-link.active .nav-icon {{ color: var(--accent-strong); }}
    .side-link.has-inline-icon::before, .side-link.has-inline-icon.active::before {{ content: none; display: none; }}
    .side-link::before {{ content: attr(data-icon); width: 22px; height: 22px; display: inline-flex; align-items: center; justify-content: center; color: #7786ad; font-size: 18px; position: static; flex: 0 0 22px; border-radius: 0; background: transparent; }}
    .side-link.active::before {{ content: attr(data-icon); color: var(--accent-strong); position: static; width: 22px; height: 22px; flex: 0 0 22px; background: transparent; }}
    .side-link:hover {{ background: var(--warning-soft); color: var(--accent-strong); border-color: var(--warning-border); }}
    .side-link.active {{ background: var(--accent-soft); border-color: var(--accent-border); color: var(--accent-strong); box-shadow: none; }}
    .side-link.active .side-icon {{ color: var(--accent-strong); }}
    .side-link-disabled, .side-link-disabled:hover, .side-link-disabled:disabled {{ color: var(--muted); background: transparent; border-color: transparent; box-shadow: none; cursor: not-allowed; opacity: .58; }}
    .side-link-disabled .nav-icon, .side-link-disabled:hover .nav-icon {{ color: var(--muted); }}
    .admin-tree {{ margin: 0 0 0 34px; padding-left: 10px; border-left: 1px solid var(--border); }}
    .admin-link {{ display: block; padding: 7px 10px; font-size: 12px; }}
    .sidebar-footer {{ display: none; }}
    .theme-selector-wrap {{ position: relative; width: auto; }}
    .theme-selector-wrap .theme-selector {{ justify-content: flex-start; box-shadow: none; min-height: 34px; padding: 6px 10px; white-space: nowrap; }}
    .theme-menu {{ position: absolute; left: auto; right: 0; top: calc(100% + 6px); min-width: 180px; display: none; gap: 4px; padding: 6px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); box-shadow: var(--shadow-card); z-index: 80; }}
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
    .current-user-selector, .theme-selector, .sidebar-collapse {{ display: flex; align-items: center; gap: 10px; width: auto; min-height: 34px; padding: 8px 10px; border: 1px solid transparent; border-radius: 12px; background: transparent; color: var(--text); text-align: left; }}
    .sidebar-collapse {{ width: 36px; min-width: 36px; max-width: 36px; height: 36px; min-height: 36px; padding: 0; justify-content: center; justify-self: end; color: #223158; border-color: var(--border); background: var(--surface); box-shadow: var(--shadow-soft); overflow: hidden; }}
    .sidebar-collapse:hover {{ background: var(--accent-soft); border-color: var(--accent); color: var(--accent-strong); }}
    .sidebar-collapse-icon {{ width: 18px; height: 18px; display: inline-flex; align-items: center; justify-content: center; }}
    .sidebar-collapse-icon .material-symbols-rounded {{ font-size: 20px; }}
    .current-user-selector {{ position: relative; background: var(--surface-soft); border-color: var(--border); padding: 0; min-height: 34px; }} .current-user-selector summary {{ display: flex; align-items: center; gap: 8px; cursor: pointer; list-style: none; min-height: 32px; padding: 3px 9px; }} .current-user-selector summary::-webkit-details-marker {{ display: none; }} .current-user-menu {{ position: absolute; left: 0; right: 0; top: calc(100% + 6px); display: grid; gap: 5px; padding: 7px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); box-shadow: var(--shadow-card); z-index: 50; }} .current-user-menu-info {{ display: grid; gap: 1px; padding: 6px 8px 8px; border-bottom: 1px solid var(--border); }} .current-user-menu-info small {{ color: var(--muted); font-size: 12px; }} .current-user-menu a {{ display: block; padding: 7px 8px; border-radius: 8px; color: var(--text); font-size: 12px; font-weight: 700; text-decoration: none; }} .current-user-menu a:hover {{ background: var(--accent-soft); color: var(--accent-strong); }} .current-user-menu .logout-link {{ margin-top: 1px; color: #b42318; background: #fff5f4; border: 1px solid #ffd8d3; }} .current-user-menu .logout-link:hover {{ background: #ffeceb; border-color: #fda29b; color: #9f1f17; }} .user-icon {{ background: var(--accent-strong); color: #fff; border-radius: 8px; width: 24px; height: 24px; }} .user-icon .material-symbols-rounded {{ font-size: 18px; }} .login-body {{ min-height: 100vh; display: grid; place-items: center; padding: 24px; }} .login-shell {{ width: min(560px, 100%); }} .login-card {{ padding: 28px; border: 1px solid var(--border); border-radius: 18px; background: var(--surface); box-shadow: var(--shadow-card); }} .login-card h1 {{ margin-bottom: 6px; }} .login-users {{ display: grid; gap: 10px; margin: 20px 0; }} .login-user-card {{ display: flex; align-items: center; gap: 12px; padding: 12px; border: 1px solid var(--border); border-radius: 12px; cursor: pointer; }} .login-user-card:hover {{ border-color: var(--accent); background: var(--accent-soft); }} .login-user-card span strong, .login-user-card span small {{ display: block; }} .login-user-card span small, .muted {{ color: var(--muted); }} .login-error {{ padding: 10px 12px; border-radius: 10px; background: var(--danger-soft); color: #b42318; font-weight: 700; }}
    .user-copy strong, .user-copy small {{ display: block; white-space: nowrap; }} .user-copy strong {{ line-height: 1.1; }} .user-copy small {{ color: var(--muted); }}
    .app-shell.sidebar-collapsed {{ grid-template-columns: 70px minmax(0, 1fr); }}
    .sidebar-collapsed .sidebar {{ padding-left: 8px; padding-right: 8px; }}
    .sidebar-collapsed .brand-copy, .sidebar-collapsed .side-label, .sidebar-collapsed .user-copy, .sidebar-collapsed .current-user-selector .logout-link, .sidebar-collapsed .admin-tree {{ display: none; }}
    .sidebar-collapsed .sidebar {{ overflow: visible; }}
    .sidebar-collapsed .side-nav {{ overflow: visible; padding-right: 0; scrollbar-gutter: auto; }}
    .sidebar-collapsed .side-link {{ font-size: 0; gap: 0; height: var(--sidebar-item-height); min-height: var(--sidebar-item-height); max-height: var(--sidebar-item-height); }}
    .sidebar-collapsed .sidebar-head {{ grid-template-columns: 1fr; gap: 10px; }}
    .sidebar-collapsed .sidebar-collapse {{ order: -1; justify-self: center; }}
    .sidebar-collapsed .sidebar-collapse-icon {{ transform: scaleX(-1); color: var(--accent-strong); }}
    .sidebar-collapsed .brand-block, .sidebar-collapsed .side-link {{ justify-content: center; padding-left: 0; padding-right: 0; }}
    .sidebar-collapsed [data-tooltip] {{ position: relative; }}
    .sidebar-collapsed [data-tooltip]:hover::after {{ content: attr(data-tooltip); position: absolute; left: calc(100% + 10px); top: 50%; transform: translateY(-50%); z-index: 10000; pointer-events: none; white-space: nowrap; border-radius: 8px; padding: 7px 9px; background: #111827; color: #fff; font-size: 12px; box-shadow: var(--shadow-card); }}
    .metrics-grid {{ grid-template-columns: repeat(4, minmax(180px,1fr)); gap: 20px; margin: 8px 0 28px; }}
    .metric-card {{ position: relative; overflow: hidden; min-height: 156px; padding: 20px; border: 1px solid var(--border); border-left: 1px solid var(--border); border-radius: 14px; background: #fff; box-shadow: var(--shadow-card); }}
    .metric-card::before {{ content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--accent); opacity: .85; }}
    .metric-card.green::before {{ background: var(--accent-border); }} .metric-card.violet::before {{ background: var(--cyber); }} .metric-card.orange::before {{ background: var(--warning); }}
    .metric-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }}
    .metric-icon {{ width: 38px; height: 38px; border-radius: 13px; background: var(--accent-soft); color: var(--accent-strong); box-shadow: inset 3px 0 0 var(--accent-border); }}
    .metric-card.green .metric-icon {{ background: var(--accent-soft); color: var(--accent-strong); }} .metric-card.violet .metric-icon {{ background: var(--surface-strong); color: var(--cyber-strong); }} .metric-card.orange .metric-icon {{ background: var(--warning-soft); color: var(--warning-hover); }}
    .sparkline {{ width: 96px; height: 32px; }} .sparkline polyline {{ fill: none; stroke: currentColor; stroke-width: 2; }}
    .metric-label {{ min-height: 0; text-transform: none; letter-spacing: 0; font-size: 12px; color: var(--muted); }} .metric-value {{ font-size: 27px; margin: 4px 0 4px; }} .metric-hint {{ color: var(--muted); font-weight: 700; }} .metric-card.orange .metric-hint {{ color: var(--muted); }}
    .quick-links {{ grid-template-columns: repeat(3, minmax(240px, 1fr)); gap: 12px; }}
    .quick-link-card {{ position: relative; grid-template-columns: 44px 1fr 20px; align-items: center; gap: 14px; min-height: 78px; padding: 16px 20px; border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow-card); transition: transform 140ms ease, border-color 140ms ease, box-shadow 140ms ease, background 140ms ease; }}
    .quick-link-card::before {{ content: ""; position: absolute; left: 0; top: 14px; bottom: 14px; width: 3px; border-radius: 999px; background: var(--accent-border); opacity: 0; transition: opacity 140ms ease, background 140ms ease; }}
    .quick-link-card:hover {{ transform: translateY(-1px); border-color: var(--accent-border); background: #fffdf8; color: var(--text-strong); text-decoration: none; box-shadow: var(--shadow-card-hover); }}
    .quick-link-card:hover::before {{ opacity: 1; background: var(--warning); }}
    .quick-icon {{ width: 40px; height: 40px; border-radius: 14px; background: var(--accent-soft); color: var(--accent-strong); }} .quick-copy strong {{ display:block; color: var(--text-strong); }} .quick-copy small {{ display:block; color: var(--muted); }} .quick-arrow {{ color: var(--muted); font-size: 22px; }}
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
    th {{ height: 36px; background: var(--table-header-bg); color: #667085; font-size: 11px; letter-spacing: .06em; text-transform: uppercase; border-bottom: 1px solid var(--border-strong); }}
    th, td {{ border-right: 1px solid #e8eef9; }} th:last-child, td:last-child {{ border-right: 0; }}
    td {{ height: 44px; padding: 8px 12px; line-height: 1.25; vertical-align: middle; border-bottom: 1px solid #e8eef9; background: #fff; }} tbody tr:nth-child(even) td {{ background: var(--table-row-alt); }} tbody tr:hover td {{ background: var(--table-row-hover); }}
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

    html[data-theme="dark"] .page-top {{
      background: linear-gradient(180deg, #0b1224 0%, var(--bg) 100%);
      border-bottom-color: var(--border);
    }}

    html[data-theme="dark"] .breadcrumbs::after {{
      content: none;
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
    html[data-theme="light-v2"] * {{ scrollbar-color: #CCD3DA #F6F7F8; }}
    html[data-theme="light-v2"] *::-webkit-scrollbar {{ width: 10px; height: 10px; }}
    html[data-theme="light-v2"] *::-webkit-scrollbar-track {{ background: var(--surface-muted); border-radius: 999px; }}
    html[data-theme="light-v2"] *::-webkit-scrollbar-thumb {{ background: #CCD3DA; border: 2px solid var(--surface-muted); border-radius: 999px; }}
    html[data-theme="light-v2"] *::-webkit-scrollbar-thumb:hover {{ background: #A7B1BC; }}
    html[data-theme="light-v2"] .app-shell, html[data-theme="light-v2"] .content {{ background: var(--bg); }}
    html[data-theme="light-v2"] body {{ color: var(--text); -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }}
    html[data-theme="light-v2"] .content > h1:first-of-type {{ margin-bottom: 14px; padding-bottom: 10px; border-bottom-color: var(--border); font-size: 24px; font-weight: 760; letter-spacing: -0.018em; }}
    html[data-theme="light-v2"] h1, html[data-theme="light-v2"] h2, html[data-theme="light-v2"] h3, html[data-theme="light-v2"] summary, html[data-theme="light-v2"] .card h2, html[data-theme="light-v2"] .table-card h2, html[data-theme="light-v2"] .journal-card h2 {{ color: var(--text-strong); }}
    html[data-theme="light-v2"] p, html[data-theme="light-v2"] label, html[data-theme="light-v2"] td, html[data-theme="light-v2"] .button, html[data-theme="light-v2"] button {{ color: var(--text); }}
    html[data-theme="light-v2"] .muted, html[data-theme="light-v2"] .metric-label, html[data-theme="light-v2"] .metric-hint, html[data-theme="light-v2"] .quick-link-card small, html[data-theme="light-v2"] .empty-state, html[data-theme="light-v2"] .dictionary-card-count, html[data-theme="light-v2"] .server-selection-count, html[data-theme="light-v2"] .server-current-route-label {{ color: var(--muted); }}
    html[data-theme="light-v2"] .breadcrumbs {{ background: var(--surface-soft, #F3F4F6); border-bottom-color: var(--border); color: var(--muted); }}
    html[data-theme="light-v2"] .breadcrumbs a {{ color: var(--accent-strong); }}
    html[data-theme="light-v2"] .sidebar {{ background: linear-gradient(180deg, #FFFFFF 0%, #F8F9FA 100%); border-right-color: var(--border-strong); }}
    html[data-theme="light-v2"] .brand-mark, html[data-theme="light-v2"] .user-icon {{ background: linear-gradient(135deg, var(--accent), var(--accent-strong)); box-shadow: 0 8px 18px rgba(15, 118, 110, .18); }}
    html[data-theme="light-v2"] .side-link {{ color: #344054; border-left: 3px solid transparent; }}
    html[data-theme="light-v2"] .side-link .side-label, html[data-theme="light-v2"] .admin-link span:last-child {{ font-weight: 680; letter-spacing: -0.003em; color: inherit; }}
    html[data-theme="light-v2"] .admin-link {{ color: #3B4956; }}
    html[data-theme="light-v2"] .side-icon, html[data-theme="light-v2"] .nav-icon, html[data-theme="light-v2"] .side-link::before {{ color: #667085 !important; }}
    html[data-theme="light-v2"] .side-link:hover, html[data-theme="light-v2"] .admin-link:hover {{ background: #F8FAFB; border-color: var(--accent-border); color: var(--accent-strong); }}
    html[data-theme="light-v2"] .side-link.active, html[data-theme="light-v2"] .sidebar-collapsed .side-link.active {{ background: #F8FAFB !important; border-color: var(--accent-border) !important; color: var(--accent-strong); box-shadow: inset 4px 0 0 var(--accent), 0 6px 16px rgba(15, 118, 110, .08); }}
    html[data-theme="light-v2"] .side-link.active .side-icon, html[data-theme="light-v2"] .side-link.active .nav-icon, html[data-theme="light-v2"] .side-link.active::before {{ color: var(--accent-strong) !important; }}
    html[data-theme="light-v2"] .side-link[href="/provider-changes"] {{ border-color: rgba(217, 119, 6, .18); }}
    html[data-theme="light-v2"] .side-link[href="/provider-changes"] .side-icon, html[data-theme="light-v2"] .side-link[href="/provider-changes"] .nav-icon {{ color: var(--provider-accent) !important; }}
    html[data-theme="light-v2"] .side-link[href="/provider-changes"]:hover {{ background: var(--provider-soft); border-color: var(--provider-border); color: var(--provider-hover); }}
    html[data-theme="light-v2"] .side-link[href="/provider-changes"].active {{ background: var(--provider-soft) !important; border-color: var(--provider-border) !important; color: var(--provider-hover); box-shadow: inset 4px 0 0 var(--provider-accent), 0 6px 16px rgba(217, 119, 6, .12); }}
    html[data-theme="light-v2"] .side-link-disabled, html[data-theme="light-v2"] .side-link-disabled:hover, html[data-theme="light-v2"] .side-link-disabled:disabled {{ color: #7A8793; opacity: .78; background: transparent; }}
    html[data-theme="light-v2"] .current-user-selector, html[data-theme="light-v2"] .theme-selector, html[data-theme="light-v2"] .sidebar-collapse {{ background: #F8FAFB; border-color: var(--border); color: var(--text); }}
    html[data-theme="light-v2"] .theme-menu, html[data-theme="light-v2"] .current-user-menu, html[data-theme="light-v2"] .column-settings-panel {{ background: var(--surface); border-color: var(--border-strong); box-shadow: var(--shadow-card); }}
    html[data-theme="light-v2"] .theme-menu button:hover, html[data-theme="light-v2"] .theme-menu button[aria-checked="true"], html[data-theme="light-v2"] .current-user-menu a:hover, html[data-theme="light-v2"] .column-settings-row:hover {{ background: var(--accent-soft); color: var(--accent-strong); }}
    html[data-theme="light-v2"] .card, html[data-theme="light-v2"] details, html[data-theme="light-v2"] fieldset, html[data-theme="light-v2"] .filter-card, html[data-theme="light-v2"] .form-card, html[data-theme="light-v2"] .table-footer, html[data-theme="light-v2"] .table-card, html[data-theme="light-v2"] .journal-card, html[data-theme="light-v2"] .dictionary-card, html[data-theme="light-v2"] .dictionary-toolbar, html[data-theme="light-v2"] .event-feed, html[data-theme="light-v2"] .metric-card, html[data-theme="light-v2"] .quick-link-card, html[data-theme="light-v2"] .login-card {{ background: var(--surface); border-color: var(--border); box-shadow: var(--shadow-card); }}
    html[data-theme="light-v2"] input, html[data-theme="light-v2"] select, html[data-theme="light-v2"] textarea {{ background: var(--input-bg); color: var(--text); border-color: var(--border-strong); }}
    html[data-theme="light-v2"] input:focus, html[data-theme="light-v2"] select:focus, html[data-theme="light-v2"] textarea:focus {{ border-color: var(--accent); outline-color: var(--accent); box-shadow: 0 0 0 3px rgba(15, 118, 110, .14); }}
    html[data-theme="light-v2"] input::placeholder, html[data-theme="light-v2"] textarea::placeholder {{ color: var(--text-soft); }}
    html[data-theme="light-v2"] th {{ background: var(--table-header-bg); color: var(--text); border-bottom-color: var(--border-strong); font-weight: 780; letter-spacing: .02em; }}
    html[data-theme="light-v2"] th, html[data-theme="light-v2"] td {{ border-right-color: var(--border); }}
    html[data-theme="light-v2"] td {{ background: var(--surface); border-bottom-color: var(--border); color: var(--text); font-weight: 440; }}
    html[data-theme="light-v2"] td .cell-clamp, html[data-theme="light-v2"] .compound-value-cell {{ color: inherit; }}
    html[data-theme="light-v2"] td .muted, html[data-theme="light-v2"] .cell-clamp .muted {{ color: var(--text-soft); font-weight: 520; }}
    html[data-theme="light-v2"] tbody tr:nth-child(even) td {{ background: var(--table-row-alt); }}
    html[data-theme="light-v2"] tbody tr:hover td, html[data-theme="light-v2"] .selectable-cell:hover {{ background: var(--table-row-hover) !important; }}
    html[data-theme="light-v2"] form button[type="submit"], html[data-theme="light-v2"] .hero-action, html[data-theme="light-v2"] .modal-save, html[data-theme="light-v2"] .admin-edit-save {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    html[data-theme="light-v2"] form button[type="submit"]:hover, html[data-theme="light-v2"] .hero-action:hover, html[data-theme="light-v2"] .modal-save:hover, html[data-theme="light-v2"] .admin-edit-save:hover {{ background: var(--accent-hover); border-color: var(--accent-hover); color: #fff; }}
    html[data-theme="light-v2"] .metric-icon, html[data-theme="light-v2"] .quick-icon {{ background: var(--accent-soft); color: var(--accent-strong); box-shadow: 0 0 0 1px var(--accent-border) inset; }}
    html[data-theme="light-v2"] .metric-card.green .metric-icon, html[data-theme="light-v2"] .feed-icon.ok, html[data-theme="light-v2"] .dot-status.ok span {{ background: var(--success); color: #fff; box-shadow: 0 0 0 5px var(--success-soft); }}
    html[data-theme="light-v2"] .metric-card.orange .metric-icon, html[data-theme="light-v2"] .feed-icon.warn, html[data-theme="light-v2"] .dot-status.warning span {{ background: var(--provider-accent); color: #fff; box-shadow: 0 0 0 5px var(--provider-soft); }}
    html[data-theme="light-v2"] .metric-card.orange {{ border-color: var(--provider-border); box-shadow: inset 3px 0 0 var(--provider-accent), var(--shadow-card); }}
    html[data-theme="light-v2"] .metric-card.orange .sparkline {{ color: var(--provider-accent); }}
    html[data-theme="light-v2"] .dot-status.danger span, html[data-theme="light-v2"] .feed-icon.danger {{ background: var(--danger); box-shadow: 0 0 0 5px var(--danger-soft); }}
    html[data-theme="light-v2"] .status-badge, html[data-theme="light-v2"] .badge {{ border: 1px solid var(--border); background: var(--surface-muted); color: var(--text); border-radius: 999px; padding: 2px 8px; }}
    html[data-theme="light-v2"] .review-required-icon {{ color: var(--warning); }}
    html[data-theme="light-v2"] .button:not(.export-button), html[data-theme="light-v2"] button:not(.theme-trigger):not(.current-user-trigger):not(.sidebar-collapse):not(.copy-button) {{ border-color: var(--border-strong); }}
    html[data-theme="light-v2"] .modal-cancel, html[data-theme="light-v2"] .admin-edit-cancel {{ background: var(--surface); color: var(--muted); border-color: var(--border-strong); }}
    html[data-theme="light-v2"] .modal-cancel:hover, html[data-theme="light-v2"] .admin-edit-cancel:hover {{ background: var(--surface-soft); color: var(--text); border-color: var(--border-strong); }}
    html[data-theme="light-v2"] .modal-card, html[data-theme="light-v2"] .modal-form-card[open] > form, html[data-theme="light-v2"] .modal-form-card[open] > .modal-body {{ background: var(--surface); border-color: var(--border); }}
    html[data-theme="light-v2"] .modal-actions {{ background: #FBFCFD; }}
    html[data-theme="light-v2"] .modal-overlay, html[data-theme="light-v2"] .modal-form-card[open]::before {{ background: rgba(23, 32, 42, .32); }}
    html[data-theme="light-v2"] .status-badge.success, html[data-theme="light-v2"] .badge.success, html[data-theme="light-v2"] .dot-status.ok {{ background: var(--success-soft); border-color: var(--success-border); color: var(--success); }}
    html[data-theme="light-v2"] .status-badge.danger, html[data-theme="light-v2"] .badge.danger, html[data-theme="light-v2"] .dot-status.danger {{ background: var(--danger-soft); border-color: var(--danger-border); color: var(--danger-strong); }}

    html[data-theme="light-v2"] a:not(.side-link):not(.button) {{ color: var(--accent-strong); }}
    html[data-theme="light-v2"] a:not(.side-link):not(.button):hover {{ color: var(--accent-hover); }}
    html[data-theme="light-v2"] .card:hover, html[data-theme="light-v2"] .metric-card:hover, html[data-theme="light-v2"] .quick-link-card:hover {{ border-color: var(--accent-border); box-shadow: var(--shadow-card-hover); transform: translateY(-1px); }}
    html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"] {{ border-color: var(--provider-border); }}
    html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"]:hover {{ background: var(--provider-soft); border-color: var(--provider-accent); color: var(--provider-hover); }}
    html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"] .quick-icon {{ background: var(--provider-soft); border-color: var(--provider-border); color: var(--provider-hover); box-shadow: 0 0 0 1px var(--provider-border) inset; }}
    html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"]:hover .quick-arrow {{ color: var(--provider-accent); }}
    html[data-theme="light-v2"] .sparkline polyline {{ stroke-width: 2.8; }}
    html[data-theme="light-v2"] .status-badge.warning, html[data-theme="light-v2"] .badge.warning, html[data-theme="light-v2"] .dot-status.warning {{ background: var(--warning-soft); border-color: var(--warning-border); color: var(--warning-hover); }}

    html[data-theme="light-v2"] .provider-changes-page h1 {{ border-left: 4px solid var(--provider-accent); padding-left: 12px; }}
    html[data-theme="light-v2"] .provider-changes-page .form-card, html[data-theme="light-v2"] .provider-changes-page .filter-card, html[data-theme="light-v2"] .provider-changes-page .journal-card {{ border-color: var(--border); }}
    html[data-theme="light-v2"] .provider-changes-page .form-summary {{ color: var(--provider-hover); }}
    html[data-theme="light-v2"] .provider-changes-page .journal-card {{ box-shadow: inset 3px 0 0 var(--provider-accent), var(--shadow-card); }}
    html[data-theme="light-v2"] .provider-changes-page th {{ background: var(--table-header-bg); }}
    html[data-theme="light-v2"] .provider-changes-page #routing-event-form button:not([type="button"]), html[data-theme="light-v2"] .provider-changes-page .modal-save {{ background: linear-gradient(135deg, var(--provider-accent), var(--provider-hover)); border-color: var(--provider-accent); color: #fff; box-shadow: 0 6px 14px rgba(217,119,6,.16); }}
    html[data-theme="light-v2"] .provider-changes-page #routing-event-form button:not([type="button"]):hover, html[data-theme="light-v2"] .provider-changes-page .modal-save:hover {{ background: var(--provider-hover); border-color: var(--provider-hover); color: #fff; }}
    html[data-theme="light-v2"] .provider-changes-page .scope-card:has(input:checked), html[data-theme="light-v2"] .provider-changes-page .important-checkbox {{ border-color: var(--provider-border); background: var(--provider-soft); }}
    html[data-theme="light-v2"] .table-footer .button[aria-current="page"], html[data-theme="light-v2"] .pagination .active, html[data-theme="light-v2"] .button.active {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    html[data-theme="light-v2"] .modal-form-card[open]::before, html[data-theme="light-v2"] .modal-overlay {{ background: rgba(23, 32, 42, .32); }}
    html[data-theme="light-v2"] .modal-card, html[data-theme="light-v2"] .modal-form-card[open] > form, html[data-theme="light-v2"] .modal-form-card[open] > .modal-body {{ background: var(--surface); border-color: var(--border); box-shadow: 0 24px 70px rgba(23, 32, 42, .18); }}
    html[data-theme="light-v2"] .button, html[data-theme="light-v2"] button {{ background: var(--surface); border-color: var(--border-strong); color: var(--text-strong); box-shadow: 0 1px 2px rgba(31,41,51,.05); }}
    html[data-theme="light-v2"] .button:hover, html[data-theme="light-v2"] button:hover {{ background: var(--accent-soft); border-color: var(--accent-border); color: var(--accent-strong); }}
    html[data-theme="light-v2"] form button[type="submit"], html[data-theme="light-v2"] .hero-action, html[data-theme="light-v2"] .modal-save, html[data-theme="light-v2"] .admin-edit-save {{ background: var(--accent); border-color: var(--accent); color: #fff; box-shadow: 0 4px 10px rgba(15,118,110,.14); }}
    html[data-theme="light-v2"] .filter-grid button[type="submit"], html[data-theme="light-v2"] .filter-grid > button {{ background: var(--accent-soft); border-color: var(--accent-border); color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .filter-grid button[type="submit"]:hover, html[data-theme="light-v2"] .filter-grid > button:hover {{ background: var(--accent-soft); border-color: var(--accent); color: var(--accent-strong); }}
    html[data-theme="light-v2"] .provider-changes-page .filter-grid button[type="submit"], html[data-theme="light-v2"] .provider-changes-page .filter-grid > button {{ background: var(--accent); border-color: var(--accent); color: #fff; box-shadow: 0 4px 10px rgba(15,118,110,.14); }}
    html[data-theme="light-v2"] .provider-changes-page .filter-grid button[type="submit"]:hover, html[data-theme="light-v2"] .provider-changes-page .filter-grid > button:hover {{ background: var(--accent-strong); border-color: var(--accent-strong); color: #fff; }}
    html[data-theme="light-v2"] .provider-changes-page .form-summary::after {{ content: none; }}
    html[data-theme="light-v2"] .provider-changes-page .table-footer-tools .export-button {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    html[data-theme="light-v2"] .provider-changes-page .table-footer-tools .export-button:hover {{ background: var(--accent-strong); border-color: var(--accent-strong); color: #fff; }}
    html[data-theme="light-v2"] .provider-changes-page .hlr-like-column-panel {{ width: min(420px, 88vw); max-height: min(430px, 70vh); padding: 10px; border-color: var(--border); border-radius: var(--radius-card); background: var(--surface); box-shadow: var(--shadow-card); gap: 8px; }}
    html[data-theme="light-v2"] .provider-changes-page .hlr-like-column-panel .column-settings-panel-actions {{ display: flex; justify-content: space-between; gap: 8px; align-items: center; }}
    html[data-theme="light-v2"] .provider-changes-page .hlr-like-column-panel .column-settings-list {{ display: grid; gap: 6px; overflow: auto; }}
    html[data-theme="light-v2"] .provider-changes-page .hlr-like-column-panel .column-settings-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 6px; padding: 6px; border: 1px solid var(--border); border-radius: var(--radius-small); background: var(--surface-muted); }}
    html[data-theme="light-v2"] .provider-changes-page .hlr-like-column-panel .column-settings-row label {{ display: flex; align-items: center; gap: 7px; min-width: 0; margin: 0; font-weight: 650; }}
    html[data-theme="light-v2"] .provider-changes-page .hlr-like-column-panel .column-order-button {{ min-width: 32px; padding: 3px 7px; box-shadow: none; }}
    html[data-theme="light-v2"] .reset-filters, html[data-theme="light-v2"] .modal-cancel, html[data-theme="light-v2"] .admin-edit-cancel {{ background: var(--surface-muted); border-color: var(--border-strong); color: var(--text); box-shadow: none; }}
    html[data-theme="light-v2"] .reset-filters:hover, html[data-theme="light-v2"] .modal-cancel:hover, html[data-theme="light-v2"] .admin-edit-cancel:hover {{ background: var(--surface-strong); border-color: var(--border-strong); color: var(--text-strong); }}
    html[data-theme="light-v2"] .danger-action, html[data-theme="light-v2"] form[action$="/deactivate"] button, html[data-theme="light-v2"] button[onclick*="Удал"], html[data-theme="light-v2"] button[onclick*="Деактив"], html[data-theme="light-v2"] button[onclick*="Отключ"] {{ background: var(--danger-soft); border-color: var(--danger-border); color: var(--danger-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .filter-card {{ background: #FBFCFD; border-color: var(--border); box-shadow: 0 2px 8px rgba(31,41,51,.045); }}
    html[data-theme="light-v2"] .filter-summary {{ color: var(--muted); background: var(--surface-soft); border-bottom: 1px solid transparent; }}
    html[data-theme="light-v2"] .filter-card[open] .filter-summary {{ border-bottom-color: var(--border); }}
    html[data-theme="light-v2"] .form-card {{ border-color: var(--border-strong); }}
    html[data-theme="light-v2"] th {{ background: var(--table-header-bg); color: var(--muted); border-bottom-color: var(--border-strong); }}
    html[data-theme="light-v2"] th, html[data-theme="light-v2"] td {{ border-right-color: var(--border); }}
    html[data-theme="light-v2"] td {{ border-bottom-color: var(--border); }}
    html[data-theme="light-v2"] tbody tr:nth-child(even) td {{ background: var(--table-row-alt); }}
    html[data-theme="light-v2"] tbody tr:hover td, html[data-theme="light-v2"] .selectable-cell:hover {{ background: var(--table-row-hover) !important; }}
    html[data-theme="light-v2"] .copy-column-button, html[data-theme="light-v2"] .edit-action, html[data-theme="light-v2"] td[data-col="actions"] details.edit-details > summary {{ background: #FBFCFD; border-color: var(--border); color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .modal-form-card[open]::before, html[data-theme="light-v2"] .modal-overlay {{ background: rgba(31, 41, 51, .32); }}
    html[data-theme="light-v2"] .modal-card, html[data-theme="light-v2"] .modal-form-card[open] > form, html[data-theme="light-v2"] .modal-form-card[open] > .modal-body {{ background: var(--surface); border-color: var(--border); box-shadow: 0 22px 60px rgba(23, 32, 42, .18); }}
    html[data-theme="light-v2"] .modal-card h2 {{ padding-bottom: 10px; border-bottom: 1px solid var(--border); }}
    html[data-theme="light-v2"] .modal-actions, html[data-theme="light-v2"] .admin-edit-actions {{ background: #FBFCFD; margin: 6px -20px -20px; padding: 12px 20px; border-top-color: var(--border); }}
    html[data-theme="light-v2"] .modal-card input, html[data-theme="light-v2"] .modal-card select, html[data-theme="light-v2"] .modal-card textarea, html[data-theme="light-v2"] .modal-form-card[open] input, html[data-theme="light-v2"] .modal-form-card[open] select, html[data-theme="light-v2"] .modal-form-card[open] textarea {{ border-color: var(--border-strong); }}
    html[data-theme="light-v2"] fieldset {{ border-color: var(--border-strong); background: #FBFCFD; }}
    html[data-theme="light-v2"] fieldset > legend {{ color: var(--accent-strong); font-weight: 820; }}
    html[data-theme="light-v2"] .checkbox-list label, html[data-theme="light-v2"] .permission-matrix label {{ padding: 4px 6px; border-radius: 8px; background: #FBFCFD; border: 1px solid var(--border); }}
    html[data-theme="light-v2"] .status-badge, html[data-theme="light-v2"] .badge {{ border: 1px solid var(--border-strong); background: var(--surface-muted); color: var(--text); border-radius: 999px; padding: 2px 8px; font-weight: 760; }}
    html[data-theme="light-v2"] .status-badge.ok, html[data-theme="light-v2"] .status-badge.success, html[data-theme="light-v2"] .badge.ok, html[data-theme="light-v2"] .badge.success {{ background: var(--success-soft); border-color: var(--success-border); color: var(--success); }}
    html[data-theme="light-v2"] .status-badge.warning, html[data-theme="light-v2"] .badge.warning, html[data-theme="light-v2"] .dot-status.warning {{ background: var(--warning-soft); border-color: var(--warning-border); color: var(--warning-hover); }}

    html[data-theme="light-v2"] .provider-changes-page h1 {{ border-left: 4px solid var(--provider-accent); padding-left: 12px; }}
    html[data-theme="light-v2"] .provider-changes-page .form-card, html[data-theme="light-v2"] .provider-changes-page .filter-card, html[data-theme="light-v2"] .provider-changes-page .journal-card {{ border-color: var(--border); }}
    html[data-theme="light-v2"] .provider-changes-page .form-summary {{ color: var(--provider-hover); }}
    html[data-theme="light-v2"] .provider-changes-page .journal-card {{ box-shadow: inset 3px 0 0 var(--provider-accent), var(--shadow-card); }}
    html[data-theme="light-v2"] .provider-changes-page th {{ background: var(--table-header-bg); }}
    html[data-theme="light-v2"] .provider-changes-page #routing-event-form button:not([type="button"]), html[data-theme="light-v2"] .provider-changes-page .modal-save {{ background: linear-gradient(135deg, var(--provider-accent), var(--provider-hover)); border-color: var(--provider-accent); color: #fff; box-shadow: 0 6px 14px rgba(217,119,6,.16); }}
    html[data-theme="light-v2"] .provider-changes-page #routing-event-form button:not([type="button"]):hover, html[data-theme="light-v2"] .provider-changes-page .modal-save:hover {{ background: var(--provider-hover); border-color: var(--provider-hover); color: #fff; }}
    html[data-theme="light-v2"] .provider-changes-page .scope-card:has(input:checked), html[data-theme="light-v2"] .provider-changes-page .important-checkbox {{ border-color: var(--provider-border); background: var(--provider-soft); }}
    html[data-theme="light-v2"] .status-badge.danger, html[data-theme="light-v2"] .badge.danger {{ background: var(--danger-soft); border-color: var(--danger-border); color: var(--danger-strong); }}

    /* Light 2.0 product-polish layer: scoped to the modern light theme only. */
    html[data-theme="light-v2"] body {{ color: var(--text); -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }}
    html[data-theme="light-v2"] .content > h1:first-of-type {{ margin-bottom: 14px; padding-bottom: 10px; border-bottom-color: var(--border); font-size: 24px; font-weight: 760; letter-spacing: -0.018em; }}
    html[data-theme="light-v2"] h1, html[data-theme="light-v2"] h2, html[data-theme="light-v2"] h3, html[data-theme="light-v2"] label, html[data-theme="light-v2"] .metric-value, html[data-theme="light-v2"] .quick-copy strong, html[data-theme="light-v2"] .brand-copy strong {{ color: var(--text-strong); }}
    html[data-theme="light-v2"] .muted, html[data-theme="light-v2"] .metric-label, html[data-theme="light-v2"] .metric-hint, html[data-theme="light-v2"] .quick-copy small, html[data-theme="light-v2"] .event-feed small, html[data-theme="light-v2"] .event-feed time, html[data-theme="light-v2"] .brand-copy span {{ color: var(--muted); }}
    html[data-theme="light-v2"] .brand-mark, html[data-theme="light-v2"] .user-icon {{ border-radius: 8px; background: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .sidebar {{ background: #FFFFFF; border-right: 1px solid var(--border-strong); box-shadow: 1px 0 0 rgba(17, 24, 39, .025); }}
    html[data-theme="light-v2"] .side-link, html[data-theme="light-v2"] .admin-link, html[data-theme="light-v2"] .current-user-selector, html[data-theme="light-v2"] .theme-selector, html[data-theme="light-v2"] .sidebar-collapse {{ border-radius: 7px; }}
    html[data-theme="light-v2"] .side-link {{ height: var(--sidebar-item-height); min-height: var(--sidebar-item-height); max-height: var(--sidebar-item-height); border: 1px solid transparent; border-left: 3px solid transparent; color: #25313C; font-weight: 660; letter-spacing: -0.002em; }}
    html[data-theme="light-v2"] .nav-icon {{ color: #5C6A76; }}
    html[data-theme="light-v2"] .side-link-disabled, html[data-theme="light-v2"] .side-link-disabled:hover, html[data-theme="light-v2"] .side-link-disabled:disabled {{ color: #788692; opacity: .66; }}
    html[data-theme="light-v2"] .side-link-disabled .nav-icon, html[data-theme="light-v2"] .side-link-disabled:hover .nav-icon {{ color: #8A96A1; }}
    html[data-theme="light-v2"] .side-link:hover, html[data-theme="light-v2"] .admin-link:hover {{ background: #F4F7F7; border-color: var(--border-strong); border-left-color: var(--accent-border); color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .side-link.active, html[data-theme="light-v2"] .sidebar-collapsed .side-link.active {{ background: #EFF6F5 !important; border-color: var(--border-strong) !important; border-left-color: var(--accent) !important; color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .current-user-selector, html[data-theme="light-v2"] .theme-selector, html[data-theme="light-v2"] .sidebar-collapse {{ background: #F7F9F9; border-color: var(--border-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .theme-menu, html[data-theme="light-v2"] .current-user-menu, html[data-theme="light-v2"] .column-settings-panel {{ border-radius: 8px; border-color: var(--border-strong); box-shadow: var(--shadow-card-hover); }}
    html[data-theme="light-v2"] .card, html[data-theme="light-v2"] details, html[data-theme="light-v2"] fieldset, html[data-theme="light-v2"] .filter-card, html[data-theme="light-v2"] .form-card, html[data-theme="light-v2"] .table-footer, html[data-theme="light-v2"] .table-card, html[data-theme="light-v2"] .journal-card, html[data-theme="light-v2"] .dictionary-card, html[data-theme="light-v2"] .dictionary-toolbar, html[data-theme="light-v2"] .event-feed, html[data-theme="light-v2"] .metric-card, html[data-theme="light-v2"] .quick-link-card, html[data-theme="light-v2"] .login-card {{ border: 1px solid var(--border-strong); border-radius: var(--radius-card); background: var(--surface); box-shadow: var(--shadow-card); }}
    html[data-theme="light-v2"] .card:hover, html[data-theme="light-v2"] .metric-card:hover, html[data-theme="light-v2"] .quick-link-card:hover {{ transform: none; border-color: var(--border-ink); box-shadow: var(--shadow-card-hover); }}
    html[data-theme="light-v2"] .metric-card {{ border-left: 3px solid var(--accent-border); background: #FFFFFF; transition: border-color 140ms ease, background 140ms ease, box-shadow 140ms ease; }}
    html[data-theme="light-v2"] .metric-card:hover, html[data-theme="light-v2"] .metric-card:focus-within {{ border-left-color: var(--accent); background: #FCFEFE; }}
    html[data-theme="light-v2"] .metric-card.orange {{ border-left-color: var(--provider-border); }}
    html[data-theme="light-v2"] .metric-card.orange:hover, html[data-theme="light-v2"] .metric-card.orange:focus-within {{ border-left-color: var(--provider-accent); }}
    html[data-theme="light-v2"] .metric-icon, html[data-theme="light-v2"] .quick-icon {{ border: 1px solid var(--accent-border); border-radius: 8px; background: #EEF7F6; color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .quick-link-card {{ min-height: 76px; border-left: 3px solid rgba(167, 216, 210, .42); background: #FFFFFF; transition: border-color 140ms ease, background 140ms ease, box-shadow 140ms ease; }}
    html[data-theme="light-v2"] .quick-link-card:hover, html[data-theme="light-v2"] .quick-link-card:focus-visible {{ border-color: var(--border-ink); border-left-color: var(--accent); background: #F7FBFA; color: var(--text-strong); outline: none; }}
    html[data-theme="light-v2"] .quick-link-card:hover .quick-arrow, html[data-theme="light-v2"] .quick-link-card:focus-visible .quick-arrow {{ color: var(--accent-strong); }}
    html[data-theme="light-v2"] .quick-link-card:hover .quick-icon, html[data-theme="light-v2"] .quick-link-card:focus-visible .quick-icon {{ border-color: var(--accent); color: var(--accent-strong); background: #EEF7F6; }}
    html[data-theme="light-v2"] .quick-link-card.provider {{ border-left-color: rgba(242, 192, 120, .54); }}
    html[data-theme="light-v2"] .quick-link-card.provider:hover, html[data-theme="light-v2"] .quick-link-card.provider:focus-visible {{ border-left-color: var(--provider-accent); background: #FFFCF7; }}
    html[data-theme="light-v2"] .quick-link-card.provider:hover .quick-icon, html[data-theme="light-v2"] .quick-link-card.provider:focus-visible .quick-icon {{ border-color: var(--provider-border); background: var(--provider-soft); color: var(--provider-hover); }}
    html[data-theme="light-v2"] .quick-link-card.provider:hover .quick-arrow, html[data-theme="light-v2"] .quick-link-card.provider:focus-visible .quick-arrow {{ color: var(--provider-hover); }}
    html[data-theme="light-v2"] .button, html[data-theme="light-v2"] button, html[data-theme="light-v2"] .column-settings summary, html[data-theme="light-v2"] .modal-form-card > summary {{ border-radius: var(--radius-control); border-color: var(--border-strong); background: #FFFFFF; color: var(--text-strong); box-shadow: none; font-weight: 720; }}
    html[data-theme="light-v2"] .button:hover, html[data-theme="light-v2"] button:hover, html[data-theme="light-v2"] .column-settings summary:hover {{ background: #F1F7F6; border-color: var(--accent); color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .button:active, html[data-theme="light-v2"] button:active {{ background: #E6F3F1; border-color: var(--accent-strong); }}
    html[data-theme="light-v2"] form button[type="submit"], html[data-theme="light-v2"] .hero-action, html[data-theme="light-v2"] .modal-save, html[data-theme="light-v2"] .admin-edit-save {{ background: var(--accent); border-color: var(--accent-strong); color: #fff; box-shadow: none; }}
    html[data-theme="light-v2"] form button[type="submit"]:hover, html[data-theme="light-v2"] .hero-action:hover, html[data-theme="light-v2"] .modal-save:hover, html[data-theme="light-v2"] .admin-edit-save:hover {{ background: var(--accent-hover); border-color: var(--accent-hover); }}
    html[data-theme="light-v2"] .reset-filters, html[data-theme="light-v2"] .modal-cancel, html[data-theme="light-v2"] .admin-edit-cancel {{ background: #F7F9F9; border-color: var(--border-strong); color: var(--text); }}
    html[data-theme="light-v2"] .danger-action, html[data-theme="light-v2"] form[action$="/deactivate"] button, html[data-theme="light-v2"] button[onclick*="Удал"], html[data-theme="light-v2"] button[onclick*="Деактив"], html[data-theme="light-v2"] button[onclick*="Отключ"] {{ background: #FFF5F5; border-color: var(--danger-border); color: var(--danger-strong); }}
    html[data-theme="light-v2"] input, html[data-theme="light-v2"] select, html[data-theme="light-v2"] textarea {{ border-radius: var(--radius-control); border-color: var(--border-strong); background: #FFFFFF; color: var(--text-strong); box-shadow: inset 0 1px 0 rgba(17, 24, 39, .025); }}
    html[data-theme="light-v2"] input:hover, html[data-theme="light-v2"] select:hover, html[data-theme="light-v2"] textarea:hover {{ border-color: var(--border-ink); }}
    html[data-theme="light-v2"] input:focus, html[data-theme="light-v2"] select:focus, html[data-theme="light-v2"] textarea:focus {{ border-color: var(--accent); outline: 2px solid rgba(15, 118, 110, .18); outline-offset: 1px; box-shadow: none; }}
    html[data-theme="light-v2"] input[type="checkbox"] {{ appearance: none; -webkit-appearance: none; width: 16px; height: 16px; min-height: 16px; margin: 0 6px 0 0; border: 1px solid var(--border-ink); border-radius: 4px; background: #FFFFFF; vertical-align: -3px; box-shadow: inset 0 1px 0 rgba(17,24,39,.04); }}
    html[data-theme="light-v2"] input[type="checkbox"]:hover {{ border-color: var(--accent); background: #F7FBFA; }}
    html[data-theme="light-v2"] input[type="checkbox"]:checked {{ border-color: var(--accent-strong); background: var(--accent); background-image: linear-gradient(45deg, transparent 56%, #fff 56%), linear-gradient(135deg, #fff 42%, transparent 42%), linear-gradient(45deg, #fff 45%, transparent 45%); background-position: 3px 7px, 6px 8px, 7px 3px; background-size: 4px 8px, 4px 8px, 7px 11px; background-repeat: no-repeat; }}
    html[data-theme="light-v2"] input[type="checkbox"]:focus-visible {{ outline: 2px solid rgba(15, 118, 110, .24); outline-offset: 2px; }}
    html[data-theme="light-v2"] .important-checkbox input[type="checkbox"], html[data-theme="light-v2"] .form-grid .spillover-checkbox input[type="checkbox"] {{ width: 18px; height: 18px; min-height: 18px; flex-basis: 18px; }}
    html[data-theme="light-v2"] .checkbox-list label, html[data-theme="light-v2"] .permission-matrix label {{ border-radius: 6px; background: #FAFBFB; border-color: var(--border); color: var(--text); }}
    html[data-theme="light-v2"] .table-card, html[data-theme="light-v2"] .journal-card {{ border-color: var(--border-strong); }}
    html[data-theme="light-v2"] .table-card h2, html[data-theme="light-v2"] .journal-card h2, html[data-theme="light-v2"] details[open] > summary {{ background: #F4F6F7; border-bottom-color: var(--border-strong); }}
    html[data-theme="light-v2"] th {{ background: #EDF1F2; color: #27323D; border-bottom: 1px solid var(--border-strong); font-weight: 800; letter-spacing: .018em; }}
    html[data-theme="light-v2"] th, html[data-theme="light-v2"] td {{ border-right: 1px solid var(--border); }}
    html[data-theme="light-v2"] td {{ color: var(--text); border-bottom: 1px solid var(--border); font-weight: 440; }}
    html[data-theme="light-v2"] td .muted, html[data-theme="light-v2"] .cell-clamp .muted {{ color: var(--text-soft); font-weight: 520; }}
    html[data-theme="light-v2"] tbody tr:nth-child(even) td {{ background: #FAFBFB; }}
    html[data-theme="light-v2"] tbody tr:hover td, html[data-theme="light-v2"] .selectable-cell:hover {{ background: #ECF5F3 !important; }}
    html[data-theme="light-v2"] .copy-column-button, html[data-theme="light-v2"] .edit-action, html[data-theme="light-v2"] td[data-col="actions"] details.edit-details > summary, html[data-theme="light-v2"] .action-button, html[data-theme="light-v2"] td[data-col="actions"] button {{ border-radius: 6px; border-color: var(--border-strong); background: #FFFFFF; color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .status-badge, html[data-theme="light-v2"] .badge {{ border-radius: 6px; border: 1px solid var(--border-strong); background: #F7F9F9; color: var(--text); font-weight: 780; }}
    html[data-theme="light-v2"] .status-badge.ok, html[data-theme="light-v2"] .status-badge.success, html[data-theme="light-v2"] .badge.ok, html[data-theme="light-v2"] .badge.success {{ background: var(--success-soft); border-color: var(--success-border); color: #1F6B43; }}
    html[data-theme="light-v2"] .status-badge.warning, html[data-theme="light-v2"] .badge.warning, html[data-theme="light-v2"] .dot-status.warning {{ background: var(--warning-soft); border-color: var(--warning-border); color: var(--warning-hover); }}
    html[data-theme="light-v2"] .status-badge.danger, html[data-theme="light-v2"] .badge.danger {{ background: var(--danger-soft); border-color: var(--danger-border); color: var(--danger-strong); }}
    html[data-theme="light-v2"] .modal-card, html[data-theme="light-v2"] .modal-form-card[open] > form, html[data-theme="light-v2"] .modal-form-card[open] > .modal-body {{ border-radius: 12px; border-color: var(--border-strong); box-shadow: 0 18px 48px rgba(17, 24, 39, .20); }}
    html[data-theme="light-v2"] .modal-card h2 {{ border-bottom: 1px solid var(--border-strong); }}
    html[data-theme="light-v2"] .modal-actions, html[data-theme="light-v2"] .admin-edit-actions {{ background: #F7F9F9; border-top: 1px solid var(--border-strong); }}
    html[data-theme="light-v2"] fieldset {{ background: #FAFBFB; border-color: var(--border-strong); }}
    html[data-theme="light-v2"] .provider-changes-page #routing-event-form button:not([type="button"]), html[data-theme="light-v2"] .provider-changes-page .modal-save {{ background: var(--accent); border-color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .provider-changes-page #routing-event-form button:not([type="button"]):hover, html[data-theme="light-v2"] .provider-changes-page .modal-save:hover {{ background: var(--accent-hover); border-color: var(--accent-hover); }}
    html[data-theme="light-v2"] .side-link[href="/provider-changes"] {{ border-color: transparent; }}
    html[data-theme="light-v2"] .side-link[href="/provider-changes"] .side-icon, html[data-theme="light-v2"] .side-link[href="/provider-changes"] .nav-icon {{ color: #5C6A76 !important; }}
    html[data-theme="light-v2"] .side-link[href="/provider-changes"]:hover {{ background: #F4F7F7; border-color: var(--border-strong); border-left-color: var(--accent-border); color: var(--accent-strong); }}
    html[data-theme="light-v2"] .side-link[href="/provider-changes"].active {{ background: #EFF6F5 !important; border-color: var(--border-strong) !important; border-left-color: var(--accent) !important; color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"], html[data-theme="light-v2"] .quick-link-card.provider {{ border-left-color: rgba(167, 216, 210, .42); border-color: var(--border-strong); }}
    html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"] .quick-icon {{ border-color: var(--accent-border); background: #EEF7F6; color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"]:hover, html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"]:focus-visible, html[data-theme="light-v2"] .quick-link-card.provider:hover, html[data-theme="light-v2"] .quick-link-card.provider:focus-visible {{ border-color: var(--border-ink); border-left-color: var(--accent); background: #F7FBFA; color: var(--text-strong); }}
    html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"]:hover .quick-icon, html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"]:focus-visible .quick-icon {{ border-color: var(--accent); background: #EEF7F6; color: var(--accent-strong); }}
    html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"]:hover .quick-arrow, html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"]:focus-visible .quick-arrow {{ color: var(--accent-strong); }}
    html[data-theme="light-v2"] .metric-card.green .metric-icon {{ background: #EEF7F6; border-color: var(--accent-border); color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .metric-card.orange, html[data-theme="light-v2"] .metric-card.teal {{ border-left-color: var(--accent-border); box-shadow: var(--shadow-card); }}
    html[data-theme="light-v2"] .metric-card.orange:hover, html[data-theme="light-v2"] .metric-card.orange:focus-within, html[data-theme="light-v2"] .metric-card.teal:hover, html[data-theme="light-v2"] .metric-card.teal:focus-within {{ border-left-color: var(--accent); background: #FCFEFE; }}
    html[data-theme="light-v2"] .metric-card.orange .metric-icon, html[data-theme="light-v2"] .metric-card.teal .metric-icon {{ background: #EEF7F6; border-color: var(--accent-border); color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .metric-card.orange .sparkline, html[data-theme="light-v2"] .metric-card.teal .sparkline {{ color: var(--accent); }}

    /* Light 2.0 strict admin redesign: final system overrides. */
    html[data-theme="light-v2"] {{
      --bg: #F2F4F5;
      --surface: #FFFFFF;
      --surface-muted: #F7F8F8;
      --surface-soft: #EEF1F1;
      --surface-strong: #E6EAEB;
      --table-header-bg: #E9EDEE;
      --table-row-alt: #FAFBFB;
      --table-row-hover: #EDF6F3;
      --sidebar-bg: #FFFFFF;
      --text-strong: #111820;
      --text: #26323D;
      --muted: #5F6D78;
      --text-soft: #788691;
      --border: #D8DFE2;
      --border-strong: #B7C2C8;
      --border-ink: #87949B;
      --accent: #1E6B4E;
      --accent-strong: #174F3B;
      --accent-hover: #123F31;
      --accent-soft: #EAF4EF;
      --accent-border: #AACFC0;
      --warning: #D27A12;
      --warning-hover: #A85B08;
      --warning-soft: #FFF4E4;
      --warning-border: #E9B766;
      --success: #23734D;
      --success-soft: #E8F5EE;
      --success-border: #A8D4BD;
      --shadow-card: 0 1px 2px rgba(16,24,32,.07), 0 8px 22px rgba(16,24,32,.045);
      --shadow-card-hover: 0 2px 4px rgba(16,24,32,.08), 0 12px 28px rgba(16,24,32,.07);
      --radius-control: 5px;
      --radius-card: 9px;
    }}
    html[data-theme="light-v2"] .content {{ padding: 18px 28px 38px; }}
    html[data-theme="light-v2"] .page-top {{ background: #EEF1F2; border-bottom-color: var(--border-strong); }}
    html[data-theme="light-v2"] .sidebar {{ padding: 12px 10px; overflow: hidden; scrollbar-gutter: stable; }}
    html[data-theme="light-v2"] .sidebar-head {{ padding-bottom: 12px; }}
    html[data-theme="light-v2"] .side-nav {{ --sidebar-item-height: 36px; gap: 4px; padding-right: 8px; scrollbar-gutter: stable both-edges; }}
    html[data-theme="light-v2"] .side-link {{ padding: 6px 10px; font-size: 13px; }}
    html[data-theme="light-v2"] .admin-tree {{ margin-left: 30px; padding: 4px 0 4px 8px; border-radius: 0; background: transparent; border-top: 0; border-right: 0; border-bottom: 0; }}
    html[data-theme="light-v2"] .admin-link {{ min-height: 30px; padding: 5px 8px; border-radius: 5px; font-size: 12px; }}
    html[data-theme="light-v2"] .filter-card, html[data-theme="light-v2"] .form-card {{ border-radius: var(--radius-card); }}
    html[data-theme="light-v2"] .filter-card {{ background: #FFFFFF; }}
    html[data-theme="light-v2"] .filter-summary, html[data-theme="light-v2"] .form-summary {{ padding: 10px 14px; background: #F3F5F5; color: var(--text-strong); font-size: 13px; font-weight: 780; }}
    html[data-theme="light-v2"] .filter-grid, html[data-theme="light-v2"] .form-grid {{ padding: 12px 14px; gap: 10px; align-items: end; }}
    html[data-theme="light-v2"] .table-page-container .filter-grid {{ grid-template-columns: repeat(auto-fit, minmax(min(170px, 100%), 1fr)); }}
    html[data-theme="light-v2"] label {{ gap: 5px; font-size: 12px; font-weight: 740; color: #34424D; }}
    html[data-theme="light-v2"] input, html[data-theme="light-v2"] select, html[data-theme="light-v2"] textarea {{ min-height: 31px; padding: 5px 8px; font-size: 13px; }}
    html[data-theme="light-v2"] .button, html[data-theme="light-v2"] button {{ min-height: 31px; padding: 5px 10px; font-size: 13px; }}
    html[data-theme="light-v2"] .reset-filters:hover, html[data-theme="light-v2"] .modal-cancel:hover, html[data-theme="light-v2"] .admin-edit-cancel:hover {{ background: var(--warning-soft); border-color: var(--warning-border); color: var(--warning-hover); }}
    html[data-theme="light-v2"] .table-card, html[data-theme="light-v2"] .journal-card {{ overflow: hidden; }}
    html[data-theme="light-v2"] .table-scroll {{ max-height: calc(100vh - 260px); }}
    html[data-theme="light-v2"] table {{ font-size: 13px; line-height: 1.28; }}
    html[data-theme="light-v2"] th {{ height: 34px; padding: 7px 10px; background: var(--table-header-bg); color: #33414B; border-bottom: 1px solid var(--border-ink); border-right-color: #CCD5D9; }}
    html[data-theme="light-v2"] td {{ height: 38px; padding: 6px 10px; border-bottom-color: var(--border); border-right-color: #E1E7E9; vertical-align: middle; }}
    html[data-theme="light-v2"] tbody tr:hover td {{ background: var(--table-row-hover) !important; }}
    html[data-theme="light-v2"] .table-footer {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 10px; min-height: 42px; margin: 0; padding: 7px 10px; border-top: 0; border-radius: 0 0 var(--radius-card) var(--radius-card); background: #F3F5F5; box-shadow: none; }}
    html[data-theme="light-v2"] .table-card + .table-footer {{ margin-top: -1px; }}
    html[data-theme="light-v2"] .table-footer-summary, html[data-theme="light-v2"] .pagination {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px; min-width: 0; }}
    html[data-theme="light-v2"] .table-footer-tools {{ display: flex; align-items: center; justify-content: flex-end; gap: 6px; }}
    html[data-theme="light-v2"] .table-footer .muted, html[data-theme="light-v2"] .pagination .muted {{ font-size: 12px; font-weight: 700; color: var(--muted); }}
    html[data-theme="light-v2"] .table-footer .button, html[data-theme="light-v2"] .table-utility-button, html[data-theme="light-v2"] .column-settings summary {{ min-height: 28px; padding: 4px 8px; font-size: 12px; }}
    html[data-theme="light-v2"] .export-action-icon {{ margin-right: 2px; color: var(--accent-strong); }}
    html[data-theme="light-v2"] .dot-status span {{ width: 8px !important; height: 8px !important; min-width: 8px !important; border: 1px solid rgba(17,24,32,.18) !important; box-shadow: 0 0 0 2px #fff !important; }}
    html[data-theme="light-v2"] .dot-status.ok span {{ background: var(--success) !important; }}
    html[data-theme="light-v2"] .dot-status.warning span {{ background: var(--warning) !important; }}
    html[data-theme="light-v2"] .dot-status.danger span {{ background: var(--danger) !important; }}
    html[data-theme="light-v2"] .dot-status.neutral span {{ background: #9AA6AD !important; }}
    html[data-theme="light-v2"] .modal-card, html[data-theme="light-v2"] .modal-form-card[open] > form, html[data-theme="light-v2"] .modal-form-card[open] > .modal-body {{ padding: 18px; border-radius: 10px; }}
    html[data-theme="light-v2"] .modal-card form, html[data-theme="light-v2"] .modal-form-card[open] > form {{ gap: 10px 12px; }}
    html[data-theme="light-v2"] .modal-actions, html[data-theme="light-v2"] .admin-edit-actions {{ justify-content: flex-start; gap: 8px; margin: 8px -18px -18px; padding: 12px 18px; }}
    html[data-theme="light-v2"] .modal-save, html[data-theme="light-v2"] .admin-edit-save {{ order: 1; }}
    html[data-theme="light-v2"] .modal-cancel, html[data-theme="light-v2"] .admin-edit-cancel {{ order: 2; }}
    html[data-theme="light-v2"] .metric-card {{ min-height: 138px; padding: 16px; border-radius: var(--radius-card); }}
    html[data-theme="light-v2"] .metric-value {{ font-size: 26px; }}
    html[data-theme="light-v2"] .quick-link-card {{ border-radius: var(--radius-card); padding: 13px 16px; }}


    /* Light 2.0 blue accent system: compact Light 2.0 rhythm with MVP-inspired primary actions. */
    html[data-theme="light-v2"] {{
      --bg: #F4F7FB;
      --surface: #FFFFFF;
      --surface-muted: #F8FAFC;
      --surface-soft: #EFF6FF;
      --surface-strong: #E7EEF8;
      --table-header-bg: #EEF3FA;
      --table-row-alt: #FAFCFF;
      --table-row-hover: #EFF6FF;
      --text-strong: #0F172A;
      --text: #1E293B;
      --muted: #475569;
      --text-soft: #64748B;
      --border: #D6DEE9;
      --border-strong: #B8C5D6;
      --border-ink: #8EA0B8;
      --accent: #2563EB;
      --accent-strong: #1D4ED8;
      --accent-hover: #1E40AF;
      --accent-soft: #EFF6FF;
      --accent-border: #93C5FD;
      --cyber: #0EA5E9;
      --cyber-strong: #0284C7;
      --cyber-soft: #E0F2FE;
      --warning: #D97706;
      --warning-hover: #B45309;
      --warning-soft: #FFF7ED;
      --warning-border: #FDBA74;
      --success: #16A34A;
      --success-soft: #ECFDF3;
      --success-border: #86EFAC;
      --focus: #2563EB;
      --shadow-card: 0 1px 2px rgba(15,23,42,.06), 0 8px 20px rgba(37,99,235,.06);
      --shadow-card-hover: 0 2px 4px rgba(15,23,42,.08), 0 14px 30px rgba(37,99,235,.10);
      --radius-control: 4px;
      --radius-card: 7px;
    }}
    html[data-theme="light-v2"] body,
    html[data-theme="light-v2"] .app-shell {{ background: var(--bg); color: var(--text); }}
    html[data-theme="light-v2"] .page-top {{ background: #EEF4FB; border-bottom: 1px solid var(--border); }}
    html[data-theme="light-v2"] .sidebar {{ background: #FFFFFF; border-right: 1px solid var(--border-strong); }}
    html[data-theme="light-v2"] .side-link {{ color: #1B2630; border-radius: 5px; font-weight: 740; }}
    html[data-theme="light-v2"] .side-link:hover {{ background: var(--accent-soft); border-color: var(--accent-border); color: var(--accent-strong); }}
    html[data-theme="light-v2"] .side-link.active {{ position: relative; background: linear-gradient(90deg, #DBEAFE 0%, #EFF6FF 100%) !important; border-color: var(--accent-border) !important; border-left-color: var(--accent) !important; color: var(--accent-hover) !important; box-shadow: inset 3px 0 0 var(--accent); }}
    html[data-theme="light-v2"] .side-link.active .nav-icon,
    html[data-theme="light-v2"] .side-link.active .side-icon,
    html[data-theme="light-v2"] .side-link.active::before {{ color: var(--accent) !important; }}
    html[data-theme="light-v2"] .admin-tree {{ background: transparent; border-left-color: var(--border-strong); }}
    html[data-theme="light-v2"] .admin-link {{ color: #26323A; }}
    html[data-theme="light-v2"] .admin-link.active {{ background: var(--accent-soft); border-color: var(--accent-border); color: var(--accent-hover); box-shadow: inset 2px 0 0 var(--accent); }}
    html[data-theme="light-v2"] .sidebar .side-link,
    html[data-theme="light-v2"] .sidebar .admin-link {{
      display: flex;
      align-items: center;
      justify-content: flex-start;
      gap: var(--sidebar-item-gap);
      height: var(--sidebar-item-height);
      min-height: var(--sidebar-item-height);
      max-height: var(--sidebar-item-height);
      padding: var(--sidebar-item-padding-y) var(--sidebar-item-padding-x);
      border: 1px solid transparent;
      border-left: 3px solid transparent;
      border-radius: 5px;
      background: transparent;
      color: #1B2630;
      font-size: 13px;
      font-weight: 740;
      line-height: 1.2;
      letter-spacing: -0.002em;
      box-shadow: none;
      box-sizing: border-box;
    }}
    html[data-theme="light-v2"] .sidebar .admin-link {{
      width: 100%;
      font-size: 12px;
      font-weight: 680;
    }}
    html[data-theme="light-v2"] .sidebar .side-link .nav-icon,
    html[data-theme="light-v2"] .sidebar .admin-link .nav-icon {{ color: #5C6A76 !important; }}
    html[data-theme="light-v2"] .sidebar .side-link:hover:not(.side-link-disabled):not(:disabled),
    html[data-theme="light-v2"] .sidebar .admin-link:hover {{
      background: var(--accent-soft);
      border-color: var(--accent-border);
      border-left-color: var(--accent-border);
      color: var(--accent-strong);
      box-shadow: none;
    }}
    html[data-theme="light-v2"] .sidebar .side-link:hover:not(.side-link-disabled):not(:disabled) .nav-icon,
    html[data-theme="light-v2"] .sidebar .admin-link:hover .nav-icon {{ color: var(--accent-strong) !important; }}
    html[data-theme="light-v2"] .sidebar .side-link.active,
    html[data-theme="light-v2"] .sidebar .admin-link.active,
    html[data-theme="light-v2"] .sidebar-collapsed .side-link.active {{
      background: linear-gradient(90deg, #DBEAFE 0%, #EFF6FF 100%) !important;
      border-color: var(--accent-border) !important;
      border-left-color: var(--accent) !important;
      color: var(--accent-hover) !important;
      box-shadow: inset 3px 0 0 var(--accent) !important;
    }}
    html[data-theme="light-v2"] .sidebar .side-link.active .nav-icon,
    html[data-theme="light-v2"] .sidebar .admin-link.active .nav-icon,
    html[data-theme="light-v2"] .sidebar .side-link.active .side-icon,
    html[data-theme="light-v2"] .sidebar .side-link.active::before {{ color: var(--accent) !important; }}
    html[data-theme="light-v2"] .sidebar .side-link[href="/provider-changes"],
    html[data-theme="light-v2"] .sidebar .side-link[href="/provider-changes"]:hover,
    html[data-theme="light-v2"] .sidebar .side-link[href="/provider-changes"].active {{ border-left-width: 3px; }}
    html[data-theme="light-v2"] .sidebar .side-link[href="/provider-changes"]:not(.active) {{
      background: transparent;
      border-color: transparent;
      border-left-color: transparent;
      color: #1B2630;
      box-shadow: none;
    }}
    html[data-theme="light-v2"] .sidebar .side-link[href="/provider-changes"]:not(.active) .nav-icon {{ color: #5C6A76 !important; }}
    html[data-theme="light-v2"] .sidebar .side-link[href="/provider-changes"]:hover:not(.active) {{
      background: var(--accent-soft);
      border-color: var(--accent-border);
      border-left-color: var(--accent-border);
      color: var(--accent-strong);
    }}
    html[data-theme="light-v2"] .sidebar .side-link[href="/provider-changes"]:hover:not(.active) .nav-icon {{ color: var(--accent-strong) !important; }}
    html[data-theme="light-v2"] .sidebar .side-link-disabled,
    html[data-theme="light-v2"] .sidebar .side-link-disabled:hover,
    html[data-theme="light-v2"] .sidebar .side-link-disabled:disabled {{
      background: transparent;
      border-color: transparent;
      border-left-color: transparent;
      color: #8A96A1;
      opacity: .72;
      box-shadow: none;
      cursor: not-allowed;
    }}
    html[data-theme="light-v2"] .sidebar .side-link-disabled:hover {{ background: #F6F7F8; }}
    html[data-theme="light-v2"] .sidebar .side-link-disabled .nav-icon,
    html[data-theme="light-v2"] .sidebar .side-link-disabled:hover .nav-icon {{ color: #9AA4AD !important; }}
    html[data-theme="light-v2"] .user-icon,
    html[data-theme="light-v2"] .user-icon .material-symbols-rounded {{ color: #FFFFFF !important; }}
    html[data-theme="light-v2"] .sidebar .admin-tree {{
      display: none;
      margin: 4px 0 0 0;
      padding: 0 0 0 14px;
      border: 0;
      border-left: 1px solid var(--border-strong);
      background: transparent;
      gap: 4px;
    }}
    html[data-theme="light-v2"] .sidebar .admin-tree.open {{ display: grid; }}
    html[data-theme="light-v2"] .filter-card,
    html[data-theme="light-v2"] .table-page-container > .form-card,
    html[data-theme="light-v2"] .dictionary-add {{ border: 1px solid var(--border-strong); border-radius: var(--radius-card); background: #FFFFFF; box-shadow: 0 1px 2px rgba(11,17,23,.06); margin: 8px 0 10px; overflow: hidden; }}
    html[data-theme="light-v2"] .filter-card > .filter-summary,
    html[data-theme="light-v2"] .table-page-container > .form-card > .form-summary,
    html[data-theme="light-v2"] .dictionary-add > summary {{ min-height: 38px; padding: 8px 12px; background: #F1F6FD; border-bottom: 1px solid var(--border); color: var(--text-strong); font-size: 12px; letter-spacing: .035em; text-transform: uppercase; }}
    html[data-theme="light-v2"] .filter-card .filter-grid,
    html[data-theme="light-v2"] .table-page-container > .form-card .form-grid {{ display: flex; flex-wrap: wrap; align-items: end; gap: 8px; padding: 9px 10px; }}
    html[data-theme="light-v2"] .filter-card label {{ flex: 0 1 176px; color: var(--text-strong); font-weight: 780; }}
    html[data-theme="light-v2"] label {{ color: #26323A; font-weight: 760; }}
    html[data-theme="light-v2"] input, html[data-theme="light-v2"] select, html[data-theme="light-v2"] textarea {{ border-color: var(--border-strong); color: var(--text-strong); background: #FFFFFF; }}
    html[data-theme="light-v2"] .muted,
    html[data-theme="light-v2"] .metric-label,
    html[data-theme="light-v2"] .metric-hint,
    html[data-theme="light-v2"] .quick-copy small {{ color: var(--muted); font-weight: 650; }}
    html[data-theme="light-v2"] .button,
    html[data-theme="light-v2"] button,
    html[data-theme="light-v2"] .column-settings summary {{ border-radius: var(--radius-control); border-color: var(--border-strong); background: #FFFFFF; color: var(--text-strong); box-shadow: none; min-height: 31px; }}
    html[data-theme="light-v2"] .button:hover,
    html[data-theme="light-v2"] button:hover,
    html[data-theme="light-v2"] .column-settings summary:hover {{ background: var(--accent-soft); border-color: var(--accent-border); color: var(--accent-strong); }}
    html[data-theme="light-v2"] form button[type="submit"],
    html[data-theme="light-v2"] .modal-save,
    html[data-theme="light-v2"] .admin-edit-save,
    html[data-theme="light-v2"] .hero-action {{ background: var(--accent); border-color: var(--accent-strong); color: #FFFFFF; }}
    html[data-theme="light-v2"] form button[type="submit"]:hover,
    html[data-theme="light-v2"] .modal-save:hover,
    html[data-theme="light-v2"] .admin-edit-save:hover,
    html[data-theme="light-v2"] .hero-action:hover {{ background: var(--accent-hover); border-color: var(--accent-hover); color: #FFFFFF; }}
    html[data-theme="light-v2"] .table-card,
    html[data-theme="light-v2"] .journal-card {{ border: 1px solid var(--border-ink); border-radius: var(--radius-card) var(--radius-card) 0 0; box-shadow: none; background: #FFFFFF; }}
    html[data-theme="light-v2"] .table-card h2,
    html[data-theme="light-v2"] .journal-card h2 {{ background: #EEF3FA; border-bottom: 1px solid var(--border); color: var(--text-strong); }}
    html[data-theme="light-v2"] table {{ border-collapse: separate; border-spacing: 0; font-size: 13px; color: var(--text); }}
    html[data-theme="light-v2"] th {{ height: 32px; background: var(--table-header-bg); color: var(--text-strong); border-right: 1px solid var(--border-strong); border-bottom: 1px solid var(--border-ink); font-weight: 820; text-transform: uppercase; letter-spacing: .035em; }}
    html[data-theme="light-v2"] td {{ height: 36px; color: #18232D; border-right: 1px solid #C9D1D6; border-bottom: 1px solid #C9D1D6; font-weight: 500; background: #FFFFFF; }}
    html[data-theme="light-v2"] tbody tr:nth-child(even) td {{ background: #F8F9FA; }}
    html[data-theme="light-v2"] tbody tr:hover td,
    html[data-theme="light-v2"] .selectable-cell:hover {{ background: var(--table-row-hover) !important; }}
    html[data-theme="light-v2"] .table-footer.table-status-action-bar {{ min-height: 38px; margin: 0 0 14px; padding: 5px 8px; border: 1px solid var(--border-ink); border-top: 0; border-radius: 0 0 var(--radius-card) var(--radius-card); background: #EEF3FA; box-shadow: none; }}
    html[data-theme="light-v2"] .table-status-nav,
    html[data-theme="light-v2"] .table-status-summary,
    html[data-theme="light-v2"] .pagination-controls {{ display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
    html[data-theme="light-v2"] .table-status-item {{ display: inline-flex; align-items: center; gap: 3px; min-height: 24px; padding: 2px 8px; border-right: 1px solid var(--border-strong); color: #26323A; font-size: 12px; font-weight: 720; }}
    html[data-theme="light-v2"] .pagination-button,
    html[data-theme="light-v2"] .icon-button {{ width: 28px; min-width: 28px; min-height: 28px; padding: 0; }}
    html[data-theme="light-v2"] .pagination-button.disabled {{ opacity: .46; cursor: not-allowed; }}
    html[data-theme="light-v2"] .export-button {{ color: var(--accent-strong); background: #FFFFFF; }}
    html[data-theme="light-v2"] .modal-card,
    html[data-theme="light-v2"] .modal-form-card[open] > form,
    html[data-theme="light-v2"] .modal-form-card[open] > .modal-body {{ padding: 0; border-radius: 8px; border-color: var(--border-strong); background: #FFFFFF; color: var(--text); }}
    html[data-theme="light-v2"] .modal-card h2,
    html[data-theme="light-v2"] .modal-form-card[open] > form > h2 {{ margin: 0; padding: 13px 16px; border-bottom: 1px solid var(--border-strong); background: #F8FAFC; }}
    html[data-theme="light-v2"] .modal-card form,
    html[data-theme="light-v2"] .modal-form-card[open] > form {{ gap: 10px 12px; padding: 14px 16px 0; }}
    html[data-theme="light-v2"] .modal-actions,
    html[data-theme="light-v2"] .admin-edit-actions {{ justify-content: flex-start; gap: 8px; margin: 10px -16px 0; padding: 10px 16px; border-top: 1px solid var(--border-strong); background: #F8FAFC; }}
    html[data-theme="light-v2"] .modal-cancel,
    html[data-theme="light-v2"] .admin-edit-cancel {{ background: #FFFFFF; color: #26323A; border-color: var(--border-strong); }}
    html[data-theme="light-v2"] .modal-cancel:hover,
    html[data-theme="light-v2"] .admin-edit-cancel:hover {{ background: var(--warning-soft); border-color: var(--warning-border); color: var(--warning-hover); }}
    html[data-theme="light-v2"] .metric-card {{ border: 1px solid var(--border-strong); border-left: 3px solid var(--accent); background: #FFFFFF; box-shadow: var(--shadow-card); }}
    html[data-theme="light-v2"] .metric-card.green .metric-icon,
    html[data-theme="light-v2"] .metric-card.orange .metric-icon,
    html[data-theme="light-v2"] .metric-card.teal .metric-icon {{ background: var(--accent-soft); border-color: var(--border-strong); color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .quick-link-card {{ border: 1px solid var(--border-strong); border-left: 3px solid var(--accent); border-radius: var(--radius-card); background: #FFFFFF; box-shadow: var(--shadow-card); }}
    html[data-theme="light-v2"] .quick-link-card:hover,
    html[data-theme="light-v2"] .quick-link-card:focus-visible {{ background: #F4F6F7; border-color: var(--border-ink); border-left-color: var(--accent-hover); color: var(--text-strong); box-shadow: var(--shadow-card-hover); transform: translateY(-1px); }}
    html[data-theme="light-v2"] .quick-icon {{ background: var(--accent-soft); border: 1px solid var(--border-strong); color: var(--accent-strong); }}


    /* Light 2.0 final blue polish: remove orange accents, normalize modal footers and option states. */
    html[data-theme="light-v2"] {{
      --provider-accent: var(--accent);
      --provider-hover: var(--accent-hover);
      --provider-soft: var(--accent-soft);
      --provider-border: var(--accent-border);
      --table-row-hover: #EFF6FF;
    }}
    html[data-theme="light-v2"] ::selection {{ background: rgba(37, 99, 235, .22); color: var(--text-strong); }}
    html[data-theme="light-v2"] input:focus,
    html[data-theme="light-v2"] select:focus,
    html[data-theme="light-v2"] textarea:focus {{ box-shadow: 0 0 0 3px rgba(37, 99, 235, .16); }}
    html[data-theme="light-v2"] .provider-changes-page h1 {{ border-left: 4px solid var(--accent) !important; padding-left: 12px; }}
    html[data-theme="light-v2"] .provider-changes-page .form-summary {{ color: var(--accent-strong); }}
    html[data-theme="light-v2"] .provider-changes-page .journal-card {{ box-shadow: inset 3px 0 0 var(--accent), var(--shadow-card); }}
    html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"] {{ border-color: var(--border-strong); }}
    html[data-theme="light-v2"] .side-link[href="/provider-changes"] .side-icon,
    html[data-theme="light-v2"] .side-link[href="/provider-changes"] .nav-icon {{ color: #667085 !important; }}
    html[data-theme="light-v2"] .side-link[href="/provider-changes"]:hover,
    html[data-theme="light-v2"] .side-link[href="/provider-changes"].active {{ background: var(--accent-soft) !important; border-color: var(--accent-border) !important; border-left-color: var(--accent) !important; color: var(--accent-strong) !important; box-shadow: inset 3px 0 0 var(--accent); }}
    html[data-theme="light-v2"] .quick-link-card {{ border: 1px solid var(--border-strong) !important; border-left: 3px solid var(--border-strong) !important; background: #fff; }}
    html[data-theme="light-v2"] .quick-link-card::before {{ display: none; }}
    html[data-theme="light-v2"] .quick-link-card:hover,
    html[data-theme="light-v2"] .quick-link-card:focus-visible,
    html[data-theme="light-v2"] .quick-link-card.active {{ background: var(--accent-soft) !important; border-color: var(--border-ink) !important; border-left-color: var(--accent) !important; color: var(--text-strong) !important; }}
    html[data-theme="light-v2"] .quick-link-card:hover .quick-arrow,
    html[data-theme="light-v2"] .quick-link-card:focus-visible .quick-arrow {{ color: var(--accent-strong); }}
    html[data-theme="light-v2"] .quick-link-card[href="/provider-changes"] .quick-icon {{ background: var(--accent-soft); border-color: var(--accent-border); color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="light-v2"] .modal-card form,
    html[data-theme="light-v2"] .modal-form-card[open] > form {{ align-items: start; }}
    html[data-theme="light-v2"] .modal-actions,
    html[data-theme="light-v2"] .admin-edit-actions {{ grid-column: 1 / -1; display: flex; justify-content: flex-start !important; align-items: center; gap: 8px; width: auto; margin: 12px -16px 0 !important; padding: 12px 16px !important; border-top: 1px solid var(--border-strong); background: #F8FAFC !important; }}
    html[data-theme="light-v2"] .modal-cancel:hover,
    html[data-theme="light-v2"] .admin-edit-cancel:hover,
    html[data-theme="light-v2"] .reset-filters:hover {{ background: var(--accent-soft) !important; border-color: var(--accent-border) !important; color: var(--accent-strong) !important; }}
    html[data-theme="light-v2"] .provider-changes-page .modal-form-card[open] > form {{ box-sizing: border-box; width: min(940px, calc(100vw - 32px)); max-width: calc(100vw - 32px); min-height: 560px; padding: 16px; }}
    html[data-theme="light-v2"] .provider-change-create-shell #routing-event-form {{ display: flex; flex-direction: column; align-items: stretch; gap: 14px; width: min(940px, calc(100vw - 32px)); max-width: calc(100vw - 32px); min-width: 0; height: min(740px, calc(100vh - 48px)); min-height: min(740px, calc(100vh - 48px)); padding: 16px 16px 0; overflow: hidden; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-shell-scope {{ margin: 0; padding: 0; border: 0; min-inline-size: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-shell-scope > legend {{ margin: 0 0 10px; padding: 0; font-weight: 700; color: var(--text-strong); }}
    html[data-theme="light-v2"] .provider-change-create-shell .scope-cards {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; width: 100%; }}
    html[data-theme="light-v2"] .provider-change-create-shell .scope-card {{ min-width: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-content-grid {{ flex: 1 1 0; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); grid-template-rows: repeat(3, max-content) minmax(150px, 1fr); gap: 12px; align-content: stretch; min-height: 0; padding: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-content-grid label {{ min-width: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-content-grid .span-2 {{ grid-column: span 2; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-content-grid .wide {{ grid-column: 1 / -1; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-content-grid textarea {{ width: 100%; min-height: 170px; height: 100%; resize: vertical; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid {{ flex: 1 1 0; display: grid; grid-template-columns: minmax(125px, .85fr) minmax(125px, .85fr) minmax(205px, 1.2fr) minmax(150px, 1fr) 48px; grid-template-rows: repeat(2, max-content) minmax(170px, 1fr); gap: 12px; align-content: stretch; align-items: start; min-height: 0; padding: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid label,
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-id-action-field,
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-company-field {{ min-width: 0; width: auto; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid label {{ display: block; white-space: nowrap; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid label > input,
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid label > select,
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid label > textarea {{ display: block; margin-top: 4px; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .field-label {{ display: inline-flex; align-items: baseline; gap: 4px; margin-bottom: 4px; color: #26323A; font-size: 12px; font-weight: 760; line-height: inherit; white-space: nowrap; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-id-action-field {{ display: block; align-self: start; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-id-action-field input {{ display: block; margin-top: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-id-action-button {{ align-self: start; margin-top: calc(1.25em + 4px); width: 48px; min-width: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-id-action-field input,
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-id-action-button {{ box-sizing: border-box; min-height: 31px; height: 31px; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-id-action-button {{ padding: 5px 8px; font-size: 13px; line-height: 1.2; box-shadow: none; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-reason-field {{ grid-column: 1 / span 2; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-company-field {{ grid-column: 3 / span 2; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-company-field .company-select-control {{ position: relative; box-sizing: border-box; width: 100%; min-width: 0; margin: 4px 0 0; border: 0; border-radius: 0; background: transparent; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-company-field .company-select-control > summary {{ position: relative; display: block; box-sizing: border-box; width: 100%; min-height: 32px; padding: 6px 28px 6px 8px; overflow: hidden; border: 1px solid var(--border-strong); border-radius: var(--radius-control); background: var(--input-bg); color: var(--text); font: inherit; line-height: normal; list-style: none; text-overflow: ellipsis; white-space: nowrap; box-shadow: inset 0 1px 1px rgba(34, 48, 42, 0.03); cursor: pointer; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-company-field .company-select-control > summary::-webkit-details-marker {{ display: none; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-company-field .company-select-control > summary::after {{ content: "▾"; position: absolute; right: 9px; top: 50%; transform: translateY(-50%); color: var(--muted); font-size: 12px; pointer-events: none; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-company-field .company-select-control:hover > summary {{ border-color: var(--border-ink); }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-company-field .company-select-control:focus-within > summary {{ border-color: var(--accent); box-shadow: 0 0 0 3px rgba(37, 99, 235, .16); outline: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-company-field .company-select-control[open] > summary {{ border-bottom-color: var(--border-strong); border-radius: var(--radius-control); background: var(--input-bg); }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .campaign-company-field .company-select-panel {{ position: absolute; z-index: 20; inset-inline: 0; top: calc(100% + 4px); max-height: 280px; overflow: auto; padding: 8px; border: 1px solid var(--border-strong); border-radius: var(--radius-control); background: #fff; box-shadow: var(--shadow-soft); }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .span-2 {{ grid-column: span 2; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid .wide {{ grid-column: 1 / -1; display: flex; flex-direction: column; min-height: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid textarea {{ width: 100%; min-height: 180px; height: 100%; resize: vertical; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-server-priority-create {{ flex: 1 1 0; display: flex; flex-direction: column; min-height: 0; min-width: 0; overflow: hidden; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-columns {{ flex: 0 0 auto; display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 3fr); gap: 14px; align-items: start; min-width: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-left {{ display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; min-width: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-row {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; align-items: end; min-width: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-left label,
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-comment {{ min-width: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-comment {{ flex: 1 1 auto; display: flex; flex-direction: column; margin-top: 12px; min-height: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-comment textarea {{ flex: 1 1 auto; width: 100%; min-width: 0; min-height: 150px; box-sizing: border-box; resize: vertical; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-right {{ min-width: 0; align-self: start; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-title {{ display: block; margin: 0 0 6px; text-align: center; color: #26323A; font-size: 12px; font-weight: 760; line-height: 1.25; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-right .server-checkbox-toolbar {{ display: none; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-right .server-checkbox-grid {{ display: flex; flex-wrap: wrap; gap: 5px; align-items: center; justify-content: flex-start; margin: 0; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-right .server-checkbox-item {{ min-width: 42px; min-height: 24px; justify-content: center; padding: 3px 8px; gap: 0; border-radius: var(--radius-control); background: #fff; font-size: 12px; line-height: 1; text-align: center; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-right .server-checkbox-item input[type="checkbox"] {{ position: absolute; width: 1px; height: 1px; min-height: 1px; margin: 0; padding: 0; opacity: 0; pointer-events: none; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-right .server-checkbox-copy {{ justify-content: center; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-right .server-current-routes {{ max-height: 145px; overflow-y: auto; overflow-x: hidden; margin-top: 8px; padding: 7px 8px; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-right .server-current-route-row {{ display: flex; min-width: 0; gap: 5px; align-items: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-right .server-current-route-name {{ flex: 0 0 auto; }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-right .server-current-route-text {{ color: var(--muted); }}
    html[data-theme="light-v2"] .provider-change-create-shell .server-priority-create-right .server-current-route-text.has-route {{ color: #C2410C; }}
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-shell-hint {{ flex: 0 0 24px; min-height: 24px; margin: 0; color: var(--muted); }}
    html[data-theme="light-v2"] .provider-change-create-shell #routing-event-form .modal-actions {{ flex: 0 0 auto; width: calc(100% + 32px); box-sizing: border-box; margin: 0 -16px; padding: 14px 16px; }}
    html[data-theme="light-v2"] .scope-cards {{ grid-column: 1 / -1; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); width: 100%; gap: 10px; }}
    html[data-theme="light-v2"] .scope-card {{ position: relative; display: flex; align-items: stretch; min-height: 58px; padding: 10px 12px 10px 14px; border: 1px solid var(--border-strong); border-left: 3px solid var(--border-strong); background: #fff; box-shadow: none; cursor: pointer; }}
    html[data-theme="light-v2"] .scope-card input[type="radio"] {{ position: absolute; opacity: 0; pointer-events: none; }}
    html[data-theme="light-v2"] .scope-card-indicator {{ display: none; }}
    html[data-theme="light-v2"] .scope-card-indicator::after {{ content: none; }}
    html[data-theme="light-v2"] .scope-card-text {{ display: flex; align-items: center; min-width: 0; line-height: 1.25; }}
    html[data-theme="light-v2"] .scope-card:hover {{ background: var(--accent-soft); border-color: var(--accent-border); border-left-color: var(--accent); }}
    html[data-theme="light-v2"] .scope-card.selected,
    html[data-theme="light-v2"] .scope-card:has(input:checked),
    html[data-theme="light-v2"] .provider-changes-page .scope-card:has(input:checked) {{ background: var(--accent-soft) !important; border-color: var(--accent-border) !important; border-left-color: var(--accent) !important; box-shadow: inset 3px 0 0 var(--accent); color: var(--text-strong); }}
    html[data-theme="light-v2"] .scope-card.selected .scope-card-indicator,
    html[data-theme="light-v2"] .scope-card:has(input:checked) .scope-card-indicator {{ background: var(--accent); border-color: var(--accent); }}
    html[data-theme="light-v2"] #routing-event-form {{ width: 100%; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px 16px; align-items: end; }}
    html[data-theme="light-v2"] #routing-event-form > fieldset:first-of-type {{ grid-column: 1 / -1; width: 100%; box-sizing: border-box; margin: 0 0 4px; }}
    html[data-theme="light-v2"] #routing-event-form label,
    html[data-theme="light-v2"] #routing-event-form .scope-field {{ min-width: 0 !important; width: auto !important; }}
    html[data-theme="light-v2"] #routing-event-form .wide {{ grid-column: 1 / -1; width: auto; }}
    html[data-theme="light-v2"] #routing-event-form .provider-change-comment-field textarea {{ width: 100%; min-width: 0; box-sizing: border-box; }}
    html[data-theme="light-v2"] #routing-event-form .route-select-field {{ grid-column: auto; width: auto; min-width: 0; }}
    html[data-theme="light-v2"] #routing-event-form fieldset.scope-field[data-scopes="server_priority"] {{ grid-column: 2; grid-row: 3 / span 6; align-self: stretch; background: #F8FAFC; border-color: var(--border-strong); }}
    html[data-theme="light-v2"] #routing-event-form .modal-actions {{ grid-column: 1 / -1; display: flex; justify-content: flex-start; margin: 0 -16px -16px; padding: 14px 16px; border-top: 1px solid var(--border-strong); background: #F8FAFC; }}
    html[data-theme="light-v2"] #routing-event-form .provider-change-service-note {{ grid-column: 1 / -1; margin: -2px 0 0; }}
    html[data-theme="light-v2"] #routing-event-form[data-current-scope='server_priority'] .provider-change-campaign-grid {{ display: contents; }}
    html[data-theme="light-v2"] #routing-event-form[data-current-scope='server_priority'] .provider-change-date-field {{ grid-column: 1; grid-row: 3; }}
    html[data-theme="light-v2"] #routing-event-form[data-current-scope='server_priority'] .routing-geo-field {{ grid-column: 1; grid-row: 4; }}
    html[data-theme="light-v2"] #routing-event-form[data-current-scope='server_priority'] .spillover-checkbox {{ grid-column: 1; grid-row: 5; align-self: center; }}
    html[data-theme="light-v2"] #routing-event-form[data-current-scope='server_priority'] .routing-provider-field {{ grid-column: 1; grid-row: 6; }}
    html[data-theme="light-v2"] #routing-event-form[data-current-scope='server_priority'] .route-select-field {{ grid-column: 1; grid-row: 7; }}
    html[data-theme="light-v2"] #routing-event-form[data-current-scope='server_priority'] #overflow-route-field {{ grid-column: 1; grid-row: 8; }}
    html[data-theme="light-v2"] #routing-event-form[data-current-scope='server_priority'] .provider-change-campaign-lower-grid {{ grid-column: 1; grid-row: 9; display: grid; grid-template-columns: 1fr; gap: 12px; }}
    html[data-theme="light-v2"] #routing-event-form[data-current-scope='campaign_setting'] .conditional-field {{ min-width: 0; }}
    html[data-theme="light-v2"] #routing-event-form .server-checkbox-grid {{ display: grid; grid-template-columns: repeat(3, minmax(64px, 1fr)); gap: 6px 8px; }}
    html[data-theme="light-v2"] #routing-event-form .server-checkbox-item {{ min-height: 26px; justify-content: flex-start; padding: 4px 6px; border-radius: var(--radius-control); background: #fff; font-size: 13px; }}
    html[data-theme="light-v2"] #routing-event-form .server-route-hint {{ display: none; }}
    html[data-theme="light-v2"] #routing-event-form .server-current-routes {{ max-height: 150px; margin-top: 8px; padding: 8px; }}
    html[data-theme="light-v2"] #routing-event-form .server-checkbox-item:has(input:checked) {{ background: var(--accent-soft); border-color: var(--accent); color: var(--accent-strong); }}
    html[data-theme="light-v2"] #routing-event-form[data-current-scope='campaign_setting'] .provider-change-campaign-grid {{ grid-template-columns: minmax(145px, .85fr) minmax(145px, .85fr) minmax(205px, 1.05fr) minmax(260px, 1.35fr); gap: 12px; align-items: end; }}
    html[data-theme="light-v2"] #routing-event-form[data-current-scope='campaign_setting'] .provider-change-campaign-lower-grid {{ grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); gap: 12px; }}
    html[data-theme="light-v2"] #routing-event-form .campaign-id-inline-action {{ grid-template-columns: minmax(0, 1fr) auto; align-items: end; }}
    html[data-theme="light-v2"] .provider-change-create-shell #routing-event-form,
    html[data-theme="light-v2"] .provider-change-create-shell #routing-event-form > *,
    html[data-theme="light-v2"] .provider-change-create-shell .scope-cards,
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-placeholder,
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-content-grid,
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-campaign-create-grid,
    html[data-theme="light-v2"] .provider-change-create-shell .provider-change-shell-hint {{ box-sizing: border-box; max-width: 100%; min-width: 0; }}
    html[data-theme="light-v2"] .important-checkbox {{ background: #fff !important; border-color: var(--border-strong) !important; }}
    html[data-theme="light-v2"] .important-checkbox:has(input:checked) {{ background: var(--accent-soft) !important; border-color: var(--accent-border) !important; }}
    html[data-theme="light-v2"] .safe-rename-block {{ display: grid; gap: 8px; }}
    html[data-theme="light-v2"] .safe-rename-option {{ display: grid !important; grid-template-columns: 10px minmax(0, 1fr); align-items: start; gap: 10px; padding: 10px 12px; border: 1px solid var(--border-strong); border-left: 3px solid var(--border-strong); border-radius: var(--radius-card); background: #fff; cursor: pointer; white-space: normal !important; }}
    html[data-theme="light-v2"] .safe-rename-option input[type="radio"] {{ position: absolute; opacity: 0; pointer-events: none; }}
    html[data-theme="light-v2"] .safe-rename-indicator {{ width: 10px; height: 10px; margin-top: 4px; border: 1px solid var(--border-strong); border-radius: 3px; background: #fff; }}
    html[data-theme="light-v2"] .safe-rename-option strong,
    html[data-theme="light-v2"] .safe-rename-option .muted {{ display: block; }}
    html[data-theme="light-v2"] .safe-rename-option:hover,
    html[data-theme="light-v2"] .safe-rename-option:has(input:checked) {{ background: var(--accent-soft); border-color: var(--accent-border); border-left-color: var(--accent); color: var(--text-strong); }}
    html[data-theme="light-v2"] .safe-rename-option:has(input:checked) {{ box-shadow: inset 3px 0 0 var(--accent); }}
    html[data-theme="light-v2"] .safe-rename-option:has(input:checked) .safe-rename-indicator {{ background: var(--accent); border-color: var(--accent); }}
    html[data-theme="light-v2"] .dictionary-card {{ position: relative; border-left: 3px solid var(--border-strong); }}
    html[data-theme="light-v2"] .dictionary-card:hover {{ background: var(--accent-soft); border-color: var(--accent-border); border-left-color: var(--accent); color: var(--accent-strong); }}
    html[data-theme="light-v2"] .dictionary-card.active {{ background: linear-gradient(90deg, #DBEAFE 0%, #EFF6FF 100%) !important; border-color: var(--accent-border) !important; border-left-color: var(--accent) !important; box-shadow: inset 3px 0 0 var(--accent); color: var(--accent-hover); }}
    html[data-theme="light-v2"] .permission-matrix label,
    html[data-theme="light-v2"] .checkbox-list label,
    html[data-theme="light-v2"] label.checkbox-inline {{ display: inline-flex; align-items: center; gap: 8px; background: #fff; border: 1px solid var(--border); border-radius: 7px; padding: 6px 8px; }}

    /* TeleRoute Pro filters foundation v1: visual-only standard filter polish scoped to the new theme. */
    html[data-theme="tele-route-pro"] details.filter-card {{ margin: 8px 0 12px; border: 1px solid var(--border-strong); border-radius: 10px; background: linear-gradient(180deg, #ffffff 0%, #fbfdff 100%); box-shadow: 0 1px 2px rgba(17, 24, 39, .04); overflow: hidden; }}
    html[data-theme="tele-route-pro"] details.filter-card[open] {{ border-color: var(--accent-border); box-shadow: 0 0 0 1px rgba(37, 99, 235, .045), 0 4px 14px rgba(17, 24, 39, .055); }}
    html[data-theme="tele-route-pro"] details.filter-card > .filter-summary {{ min-height: 34px; padding: 8px 12px; border-bottom: 1px solid transparent; background: linear-gradient(180deg, #f8fafc 0%, #f3f6fa 100%); color: #334155; font-size: 13px; font-weight: 780; letter-spacing: .01em; cursor: pointer; }}
    html[data-theme="tele-route-pro"] details.filter-card > .filter-summary:hover {{ background: #eef6ff; color: var(--accent-strong); }}
    html[data-theme="tele-route-pro"] details.filter-card[open] > .filter-summary {{ border-bottom-color: var(--accent-border); background: linear-gradient(180deg, #eff6ff 0%, #f8fbff 100%); color: var(--accent-strong); }}
    html[data-theme="tele-route-pro"] details.filter-card > .filter-summary::after {{ color: var(--muted); font-size: 11px; font-weight: 740; text-transform: uppercase; letter-spacing: .04em; }}
    html[data-theme="tele-route-pro"] details.filter-card[open] > .filter-summary::after {{ color: var(--accent-strong); }}
    html[data-theme="tele-route-pro"] details.filter-card > form {{ margin: 0; }}
    html[data-theme="tele-route-pro"] .filter-card .filter-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(176px, 100%), 1fr)); align-items: end; gap: 9px 10px; padding: 10px 12px 12px; background: #ffffff; }}
    html[data-theme="tele-route-pro"] .filter-card .filter-grid label {{ min-width: 0; color: #334155; font-size: 12px; font-weight: 760; line-height: 1.25; }}
    html[data-theme="tele-route-pro"] .filter-card .filter-grid label:not(.checkbox-inline) > input,
    html[data-theme="tele-route-pro"] .filter-card .filter-grid label:not(.checkbox-inline) > select {{ margin-top: 4px; }}
    html[data-theme="tele-route-pro"] .filter-card .filter-grid input,
    html[data-theme="tele-route-pro"] .filter-card .filter-grid select {{ min-height: 32px; padding: 5px 8px; border: 1px solid var(--border-strong); border-radius: 7px; background-color: var(--input-bg); color: var(--text); font-size: 13px; box-shadow: inset 0 1px 1px rgba(17, 24, 39, .025); }}
    html[data-theme="tele-route-pro"] .filter-card .filter-grid input:hover,
    html[data-theme="tele-route-pro"] .filter-card .filter-grid select:hover {{ border-color: var(--border-ink); }}
    html[data-theme="tele-route-pro"] .filter-card .filter-grid input:focus,
    html[data-theme="tele-route-pro"] .filter-card .filter-grid select:focus {{ border-color: var(--accent); box-shadow: 0 0 0 3px rgba(37, 99, 235, .12); outline: none; }}
    html[data-theme="tele-route-pro"] .filter-card .filter-grid button,
    html[data-theme="tele-route-pro"] .filter-card .reset-filters {{ min-height: 32px; padding: 5px 10px; border-radius: 7px; font-size: 13px; font-weight: 760; box-shadow: none; }}
    html[data-theme="tele-route-pro"] .filter-card .filter-grid button[type="submit"],
    html[data-theme="tele-route-pro"] .filter-card .filter-grid > button {{ border-color: var(--accent-border); background: var(--accent-soft); color: var(--accent-strong); }}
    html[data-theme="tele-route-pro"] .filter-card .filter-grid button[type="submit"]:hover,
    html[data-theme="tele-route-pro"] .filter-card .filter-grid > button:hover {{ border-color: var(--accent); background: #dbeafe; color: var(--accent-hover); }}
    html[data-theme="tele-route-pro"] .filter-card .reset-filters {{ display: inline-flex; align-items: center; justify-content: center; border-color: var(--border-strong); background: #f8fafc; color: #475569; text-decoration: none; }}
    html[data-theme="tele-route-pro"] .filter-card .reset-filters:hover {{ border-color: var(--accent-border); background: #eef6ff; color: var(--accent-strong); }}
    html[data-theme="tele-route-pro"] .filter-card .muted,
    html[data-theme="tele-route-pro"] .filter-card .metric-hint,
    html[data-theme="tele-route-pro"] .filter-card .form-hint,
    html[data-theme="tele-route-pro"] .filter-card small {{ color: var(--muted); font-size: 12px; line-height: 1.35; }}
    html[data-theme="tele-route-pro"] .filter-card .filter-grid .checkbox-inline {{ min-height: 32px; padding: 5px 8px; border: 1px solid var(--border); border-radius: 7px; background: #f8fafc; color: var(--text); }}

    /* TeleRoute Pro tables foundation v1: visual-only table polish scoped to the new theme. */
    html[data-theme="tele-route-pro"] .table-card {{ border: 1px solid var(--border-strong); border-radius: 10px; background: var(--surface); box-shadow: 0 1px 2px rgba(17, 24, 39, .05), 0 8px 20px rgba(17, 24, 39, .055); overflow: hidden; }}
    html[data-theme="tele-route-pro"] .table-scroll {{ background: var(--surface); scrollbar-color: #cbd5e1 var(--surface-soft); }}
    html[data-theme="tele-route-pro"] table {{ background: var(--surface); border-color: var(--border); border-collapse: separate; border-spacing: 0; }}
    html[data-theme="tele-route-pro"] th {{ background: linear-gradient(180deg, #f4f7fb 0%, #edf3fa 100%); color: #1f2a3a; border-bottom: 1px solid var(--border-ink); border-right: 1px solid var(--border); font-weight: 800; letter-spacing: .018em; text-transform: none; }}
    html[data-theme="tele-route-pro"] td {{ background: var(--surface); color: var(--text); border-bottom: 1px solid var(--border); border-right: 1px solid var(--border); font-weight: 440; }}
    html[data-theme="tele-route-pro"] tbody tr:nth-child(even) td {{ background: #fbfdff; }}
    html[data-theme="tele-route-pro"] tbody tr:hover td {{ background: #eef6ff !important; }}
    html[data-theme="tele-route-pro"] .table-footer {{ display: flex; align-items: center; justify-content: space-between; gap: 8px 12px; min-height: 38px; margin: 0 0 12px; padding: 6px 9px; border: 1px solid var(--border-strong); border-radius: 0 0 10px 10px; background: linear-gradient(180deg, #fbfdff 0%, #f4f7fb 100%); box-shadow: none; color: var(--muted); }}
    html[data-theme="tele-route-pro"] .table-card + .table-footer {{ margin-top: -13px; border-top-color: var(--border); }}
    html[data-theme="tele-route-pro"] .table-status-action-bar {{ border-color: var(--border-strong); background: linear-gradient(180deg, #fbfdff 0%, #f4f7fb 100%); color: var(--text); box-shadow: none; }}
    html[data-theme="tele-route-pro"] .table-footer-summary,
    html[data-theme="tele-route-pro"] .table-status-nav,
    html[data-theme="tele-route-pro"] .table-status-summary,
    html[data-theme="tele-route-pro"] .pagination-controls {{ display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; min-width: 0; }}
    html[data-theme="tele-route-pro"] .table-footer-summary p,
    html[data-theme="tele-route-pro"] .table-footer-summary nav {{ margin: 0; }}
    html[data-theme="tele-route-pro"] .table-status-summary {{ color: var(--muted); font-size: 12px; font-weight: 700; line-height: 1.25; }}
    html[data-theme="tele-route-pro"] .table-status-item {{ display: inline-flex; align-items: center; gap: 3px; min-height: 24px; padding: 2px 7px; border: 1px solid var(--border); border-radius: 999px; background: rgba(255, 255, 255, .72); color: #475569; white-space: nowrap; }}
    html[data-theme="tele-route-pro"] .table-status-item strong {{ color: var(--text-strong); font-weight: 800; }}
    html[data-theme="tele-route-pro"] .table-footer-tools {{ display: inline-flex; align-items: center; justify-content: flex-end; gap: 6px; flex-wrap: wrap; margin-left: auto; }}
    html[data-theme="tele-route-pro"] .pagination-button,
    html[data-theme="tele-route-pro"] .export-button,
    html[data-theme="tele-route-pro"] .table-utility-button,
    html[data-theme="tele-route-pro"] .column-settings > summary,
    html[data-theme="tele-route-pro"] td .button,
    html[data-theme="tele-route-pro"] td a.button,
    html[data-theme="tele-route-pro"] td button {{ display: inline-flex; align-items: center; justify-content: center; gap: 5px; min-height: 28px; padding: 4px 8px; border: 1px solid var(--border-strong); border-radius: 7px; background: #ffffff; color: #334155; box-shadow: none; font-size: 12px; font-weight: 740; line-height: 1.2; text-decoration: none; }}
    html[data-theme="tele-route-pro"] .pagination-button {{ min-width: 30px; padding-inline: 9px; color: var(--accent-strong); }}
    html[data-theme="tele-route-pro"] .pagination-button:hover,
    html[data-theme="tele-route-pro"] .export-button:hover,
    html[data-theme="tele-route-pro"] .table-utility-button:hover,
    html[data-theme="tele-route-pro"] .column-settings > summary:hover,
    html[data-theme="tele-route-pro"] td .button:hover,
    html[data-theme="tele-route-pro"] td a.button:hover,
    html[data-theme="tele-route-pro"] td button:hover {{ border-color: var(--accent-border); background: #eef6ff; color: var(--accent-strong); box-shadow: none; }}
    html[data-theme="tele-route-pro"] .pagination-button[aria-current="page"],
    html[data-theme="tele-route-pro"] .pagination .active,
    html[data-theme="tele-route-pro"] .button.active {{ border-color: var(--accent); background: var(--accent); color: #ffffff; }}
    html[data-theme="tele-route-pro"] .pagination-button.disabled,
    html[data-theme="tele-route-pro"] .pagination-button[aria-disabled="true"],
    html[data-theme="tele-route-pro"] .export-button.disabled,
    html[data-theme="tele-route-pro"] .export-button[aria-disabled="true"] {{ border-color: var(--border); background: #f1f5f9; color: #94a3b8; opacity: 1; cursor: not-allowed; pointer-events: none; }}
    html[data-theme="tele-route-pro"] .export-button,
    html[data-theme="tele-route-pro"] .table-utility-button {{ color: #334155; }}
    html[data-theme="tele-route-pro"] .export-button.icon-button {{ width: 30px; min-width: 30px; padding: 4px; }}
    html[data-theme="tele-route-pro"] .export-button svg,
    html[data-theme="tele-route-pro"] .table-utility-button svg {{ width: 15px; height: 15px; flex: 0 0 auto; }}
    html[data-theme="tele-route-pro"] .column-settings {{ color: var(--text); }}
    html[data-theme="tele-route-pro"] .column-settings summary {{ cursor: pointer; list-style: none; }}
    html[data-theme="tele-route-pro"] .column-settings summary::-webkit-details-marker {{ display: none; }}
    html[data-theme="tele-route-pro"] .column-settings[open] summary,
    html[data-theme="tele-route-pro"] .column-settings[open] > summary {{ background: #eef6ff; border-color: var(--accent-border); color: var(--accent-strong); box-shadow: inset 0 0 0 1px rgba(37, 99, 235, .06); }}
    html[data-theme="tele-route-pro"] .column-settings-panel {{ gap: 6px; min-width: 292px; padding: 8px; border: 1px solid var(--border-strong); border-radius: 10px; background: #ffffff; box-shadow: 0 12px 24px rgba(17, 24, 39, .12); color: var(--text); }}
    html[data-theme="tele-route-pro"] .column-settings-list {{ gap: 3px; }}
    html[data-theme="tele-route-pro"] .column-settings-row {{ min-height: 28px; padding: 3px 6px; border: 1px solid transparent; border-radius: 7px; color: var(--text); }}
    html[data-theme="tele-route-pro"] .column-settings-row:hover {{ background: #eef6ff; color: var(--text-strong); }}
    html[data-theme="tele-route-pro"] .column-settings-row label {{ font-size: 13px; font-weight: 600; color: inherit; }}
    html[data-theme="tele-route-pro"] .column-settings-row.is-locked label {{ color: var(--muted); }}
    html[data-theme="tele-route-pro"] .column-settings input[type="checkbox"] {{ width: 15px; height: 15px; min-height: 15px; margin: 0 6px 0 0; accent-color: var(--accent); }}
    html[data-theme="tele-route-pro"] .column-settings .column-order-button {{ width: 24px; min-width: 24px; min-height: 24px; padding: 0; border-color: var(--border); border-radius: 6px; background: #ffffff; color: #64748b; box-shadow: none; font-size: 12px; }}
    html[data-theme="tele-route-pro"] .column-settings .column-order-button:hover {{ border-color: var(--accent-border); background: #eff6ff; color: var(--accent-strong); }}
    html[data-theme="tele-route-pro"] .column-settings .column-reset {{ justify-content: center; min-height: 28px; padding: 4px 8px; border: 1px solid var(--border); border-radius: 7px; background: #f8fafc; color: #475569; box-shadow: none; font-size: 12px; font-weight: 700; }}
    html[data-theme="tele-route-pro"] .column-settings .column-reset:hover {{ background: #eef6ff; border-color: var(--accent-border); color: var(--accent-strong); }}
    html[data-theme="tele-route-pro"] .status-badge {{ border: 1px solid var(--border-strong); border-radius: 999px; background: #f3f6fa; color: #475569; font-weight: 780; box-shadow: inset 0 1px 0 rgba(255, 255, 255, .68); }}
    html[data-theme="tele-route-pro"] .status-badge.success,
    html[data-theme="tele-route-pro"] .status-badge.ok,
    html[data-theme="tele-route-pro"] .status-badge.active,
    html[data-theme="tele-route-pro"] .status-badge.live,
    html[data-theme="tele-route-pro"] .status-badge.hlr-severity-ok {{ background: var(--success-soft); border-color: var(--success-border); color: #15803d; }}
    html[data-theme="tele-route-pro"] .status-badge.warning,
    html[data-theme="tele-route-pro"] .status-badge.review,
    html[data-theme="tele-route-pro"] .status-badge.unknown,
    html[data-theme="tele-route-pro"] .status-badge.hlr-severity-warning,
    html[data-theme="tele-route-pro"] .status-badge.hlr-severity-unknown {{ background: var(--warning-soft); border-color: var(--warning-border); color: #b45309; }}
    html[data-theme="tele-route-pro"] .status-badge.danger,
    html[data-theme="tele-route-pro"] .status-badge.error,
    html[data-theme="tele-route-pro"] .status-badge.inactive,
    html[data-theme="tele-route-pro"] .status-badge.dead,
    html[data-theme="tele-route-pro"] .status-badge.bad_format,
    html[data-theme="tele-route-pro"] .status-badge.hlr-severity-danger {{ background: var(--danger-soft); border-color: var(--danger-border); color: var(--danger-strong); }}


    /* TeleRoute Pro component polish v2: forms, modals, details, states, controls. Visual-only and scoped to the new theme. */
    html[data-theme="tele-route-pro"] {{ --accent: #111827; --accent-strong: #0f172a; --accent-hover: #020617; --accent-soft: #f1f5f9; --accent-border: #cbd5e1; --info: #2563eb; --info-soft: #eff6ff; --info-border: #bfdbfe; --control-height: 34px; }}
    html[data-theme="tele-route-pro"] input,
    html[data-theme="tele-route-pro"] select,
    html[data-theme="tele-route-pro"] textarea {{ max-width: 100%; min-width: 0; box-sizing: border-box; border: 1px solid var(--border-strong); border-radius: 7px; background: var(--input-bg); color: var(--text); box-shadow: inset 0 1px 1px rgba(17, 24, 39, .025); font: inherit; transition: border-color .14s ease, box-shadow .14s ease, background-color .14s ease, color .14s ease; }}
    html[data-theme="tele-route-pro"] input:not([type="checkbox"]):not([type="radio"]):not([type="file"]),
    html[data-theme="tele-route-pro"] select {{ min-height: var(--control-height); padding: 6px 9px; }}
    html[data-theme="tele-route-pro"] textarea {{ min-height: 82px; padding: 8px 9px; resize: vertical; line-height: 1.45; }}
    html[data-theme="tele-route-pro"] input::placeholder,
    html[data-theme="tele-route-pro"] textarea::placeholder {{ color: #7b8794; opacity: 1; }}
    html[data-theme="tele-route-pro"] input:hover,
    html[data-theme="tele-route-pro"] select:hover,
    html[data-theme="tele-route-pro"] textarea:hover {{ border-color: var(--border-ink); }}
    html[data-theme="tele-route-pro"] input:focus,
    html[data-theme="tele-route-pro"] select:focus,
    html[data-theme="tele-route-pro"] textarea:focus,
    html[data-theme="tele-route-pro"] button:focus-visible,
    html[data-theme="tele-route-pro"] a.button:focus-visible,
    html[data-theme="tele-route-pro"] summary:focus-visible {{ outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(17, 24, 39, .13); }}
    html[data-theme="tele-route-pro"] input:disabled,
    html[data-theme="tele-route-pro"] select:disabled,
    html[data-theme="tele-route-pro"] textarea:disabled,
    html[data-theme="tele-route-pro"] button:disabled {{ background: #eef2f7; border-color: var(--border); color: #7b8794; opacity: 1; cursor: not-allowed; }}
    html[data-theme="tele-route-pro"] input[readonly],
    html[data-theme="tele-route-pro"] textarea[readonly] {{ background: #f8fafc; border-color: var(--border); color: #475569; box-shadow: none; }}
    html[data-theme="tele-route-pro"] label,
    html[data-theme="tele-route-pro"] .form-label {{ color: #334155; font-size: 12px; font-weight: 760; line-height: 1.28; }}
    html[data-theme="tele-route-pro"] label > input:not([type="checkbox"]):not([type="radio"]),
    html[data-theme="tele-route-pro"] label > select,
    html[data-theme="tele-route-pro"] label > textarea {{ margin-top: 4px; }}
    html[data-theme="tele-route-pro"] label.required::after,
    html[data-theme="tele-route-pro"] .required::after {{ content: " *"; color: var(--danger); font-weight: 850; }}
    html[data-theme="tele-route-pro"] .form-hint,
    html[data-theme="tele-route-pro"] .help-text,
    html[data-theme="tele-route-pro"] .metric-hint,
    html[data-theme="tele-route-pro"] small {{ color: var(--muted); font-size: 12px; line-height: 1.38; }}
    html[data-theme="tele-route-pro"] fieldset {{ min-width: 0; border: 1px solid var(--border-strong); border-radius: 10px; background: #fbfdff; }}
    html[data-theme="tele-route-pro"] legend {{ padding: 0 6px; color: var(--text-strong); font-size: 12px; font-weight: 820; }}
    html[data-theme="tele-route-pro"] .form-grid,
    html[data-theme="tele-route-pro"] .modal-form,
    html[data-theme="tele-route-pro"] form .grid {{ gap: 10px 12px; }}
    html[data-theme="tele-route-pro"] .campaign-id-inline-action,
    html[data-theme="tele-route-pro"] .inline-action-group {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px; align-items: end; }}
    html[data-theme="tele-route-pro"] .modal-overlay,
    html[data-theme="tele-route-pro"] .modal-backdrop,
    html[data-theme="tele-route-pro"] .dialog-backdrop {{ background: rgba(15, 23, 42, .48); }}
    html[data-theme="tele-route-pro"] .modal-card,
    html[data-theme="tele-route-pro"] .modal-content,
    html[data-theme="tele-route-pro"] dialog,
    html[data-theme="tele-route-pro"] .edit-details[open] > form {{ border: 1px solid var(--border-ink); border-radius: 12px; background: #fff; box-shadow: 0 18px 44px rgba(15, 23, 42, .22); color: var(--text); }}
    html[data-theme="tele-route-pro"] .modal-card > h2,
    html[data-theme="tele-route-pro"] .modal-header,
    html[data-theme="tele-route-pro"] .modal-title {{ border-bottom: 1px solid var(--border-strong); background: #f8fafc; color: var(--text-strong); font-weight: 850; }}
    html[data-theme="tele-route-pro"] .modal-body {{ background: #fff; }}
    html[data-theme="tele-route-pro"] .modal-actions,
    html[data-theme="tele-route-pro"] .modal-footer,
    html[data-theme="tele-route-pro"] .form-actions,
    html[data-theme="tele-route-pro"] .admin-edit-actions {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: flex-end; border-top: 1px solid var(--border-strong); background: #f8fafc; }}
    html[data-theme="tele-route-pro"] button,
    html[data-theme="tele-route-pro"] .button,
    html[data-theme="tele-route-pro"] input[type="submit"] {{ border: 1px solid var(--border-strong); border-radius: 7px; background: #fff; color: #334155; box-shadow: none; font-weight: 760; }}
    html[data-theme="tele-route-pro"] button:hover,
    html[data-theme="tele-route-pro"] .button:hover,
    html[data-theme="tele-route-pro"] input[type="submit"]:hover {{ border-color: var(--border-ink); background: #f8fafc; color: var(--text-strong); }}
    html[data-theme="tele-route-pro"] button[type="submit"],
    html[data-theme="tele-route-pro"] .button.primary,
    html[data-theme="tele-route-pro"] .primary-button {{ border-color: var(--accent); background: var(--accent); color: #fff; }}
    html[data-theme="tele-route-pro"] button[type="submit"]:hover,
    html[data-theme="tele-route-pro"] .button.primary:hover,
    html[data-theme="tele-route-pro"] .primary-button:hover {{ border-color: var(--accent-hover); background: var(--accent-hover); color: #fff; }}
    html[data-theme="tele-route-pro"] .danger,
    html[data-theme="tele-route-pro"] .button.danger,
    html[data-theme="tele-route-pro"] button.danger {{ border-color: var(--danger-border); background: var(--danger-soft); color: var(--danger-strong); }}
    html[data-theme="tele-route-pro"] details:not(.current-user-selector):not(.column-settings):not(.filter-card) {{ border: 1px solid var(--border-strong); border-radius: 10px; background: #fff; overflow: hidden; }}
    html[data-theme="tele-route-pro"] details:not(.current-user-selector):not(.column-settings):not(.filter-card) > summary {{ display: flex; align-items: center; gap: 8px; min-height: 36px; padding: 8px 12px; list-style: none; border-bottom: 1px solid transparent; background: #f8fafc; color: #334155; cursor: pointer; font-weight: 800; }}
    html[data-theme="tele-route-pro"] details:not(.current-user-selector):not(.column-settings):not(.filter-card) > summary::-webkit-details-marker {{ display: none; }}
    html[data-theme="tele-route-pro"] details:not(.current-user-selector):not(.column-settings):not(.filter-card) > summary::before {{ content: "▸"; color: var(--muted); font-size: 11px; transition: transform .14s ease; }}
    html[data-theme="tele-route-pro"] details[open]:not(.current-user-selector):not(.column-settings):not(.filter-card) > summary {{ border-bottom-color: var(--border-strong); background: #f1f5f9; color: var(--text-strong); }}
    html[data-theme="tele-route-pro"] details[open]:not(.current-user-selector):not(.column-settings):not(.filter-card) > summary::before {{ transform: rotate(90deg); color: var(--text-strong); }}
    html[data-theme="tele-route-pro"] .empty-state,
    html[data-theme="tele-route-pro"] .empty-row,
    html[data-theme="tele-route-pro"] td.empty {{ padding: 18px 16px; border: 1px dashed var(--border-strong); border-radius: 10px; background: #fbfdff; color: var(--muted); text-align: center; font-weight: 650; }}
    html[data-theme="tele-route-pro"] .alert,
    html[data-theme="tele-route-pro"] .flash,
    html[data-theme="tele-route-pro"] .message,
    html[data-theme="tele-route-pro"] .validation-error {{ border: 1px solid var(--info-border); border-left-width: 3px; border-radius: 10px; background: var(--info-soft); color: #1d4ed8; padding: 9px 11px; font-weight: 700; }}
    html[data-theme="tele-route-pro"] .success {{ border-color: var(--success-border); background: var(--success-soft); color: #15803d; }}
    html[data-theme="tele-route-pro"] .warning,
    html[data-theme="tele-route-pro"] .review-required {{ border-color: var(--warning-border); background: var(--warning-soft); color: #b45309; }}
    html[data-theme="tele-route-pro"] .error,
    html[data-theme="tele-route-pro"] .form-submit-error {{ border-color: var(--danger-border); background: var(--danger-soft); color: var(--danger-strong); }}
    html[data-theme="tele-route-pro"] input[type="checkbox"],
    html[data-theme="tele-route-pro"] input[type="radio"] {{ appearance: none; -webkit-appearance: none; display: inline-grid; place-content: center; width: 16px; height: 16px; min-width: 16px; min-height: 16px; margin: 0 6px 0 0; border: 1px solid var(--border-ink); background: #fff; vertical-align: -3px; }}
    html[data-theme="tele-route-pro"] input[type="checkbox"] {{ border-radius: 4px; }}
    html[data-theme="tele-route-pro"] input[type="radio"] {{ border-radius: 50%; }}
    html[data-theme="tele-route-pro"] input[type="checkbox"]::before {{ content: ""; width: 8px; height: 5px; border-left: 2px solid #fff; border-bottom: 2px solid #fff; transform: rotate(-45deg) scale(0); transform-origin: center; }}
    html[data-theme="tele-route-pro"] input[type="radio"]::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%; background: #fff; transform: scale(0); }}
    html[data-theme="tele-route-pro"] input[type="checkbox"]:checked,
    html[data-theme="tele-route-pro"] input[type="radio"]:checked {{ border-color: var(--accent); background: var(--accent); }}
    html[data-theme="tele-route-pro"] input[type="checkbox"]:checked::before {{ transform: rotate(-45deg) scale(1); }}
    html[data-theme="tele-route-pro"] input[type="radio"]:checked::before {{ transform: scale(1); }}
    html[data-theme="tele-route-pro"] input[type="checkbox"]:focus-visible,
    html[data-theme="tele-route-pro"] input[type="radio"]:focus-visible {{ outline: none; box-shadow: 0 0 0 3px rgba(17, 24, 39, .15); }}
    html[data-theme="tele-route-pro"] .important-checkbox,
    html[data-theme="tele-route-pro"] .spillover-checkbox,
    html[data-theme="tele-route-pro"] label:has(input[name="review_required"]),
    html[data-theme="tele-route-pro"] label:has(input[name="has_overflow"]) {{ border: 1px solid var(--warning-border); border-radius: 9px; background: #fffbeb; padding: 7px 9px; font-weight: 780; }}
    html[data-theme="tele-route-pro"] .scope-cards,
    html[data-theme="tele-route-pro"] .segmented-control,
    html[data-theme="tele-route-pro"] .tabs {{ gap: 4px; padding: 3px; border: 1px solid var(--border-strong); border-radius: 10px; background: #f1f5f9; }}
    html[data-theme="tele-route-pro"] .scope-card,
    html[data-theme="tele-route-pro"] .segment,
    html[data-theme="tele-route-pro"] .tab {{ border: 1px solid transparent; border-radius: 7px; background: transparent; color: #475569; font-weight: 760; }}
    html[data-theme="tele-route-pro"] .scope-card.selected,
    html[data-theme="tele-route-pro"] .scope-card:has(input:checked),
    html[data-theme="tele-route-pro"] .segment.active,
    html[data-theme="tele-route-pro"] .tab.active {{ border-color: var(--border-ink); background: #fff; color: var(--text-strong); box-shadow: 0 1px 2px rgba(17, 24, 39, .08); }}
    html[data-theme="tele-route-pro"] .table-footer,
    html[data-theme="tele-route-pro"] .table-status-action-bar {{ border-color: var(--border-strong); background: #f8fafc; }}
    html[data-theme="tele-route-pro"] .hlr-panel,
    html[data-theme="tele-route-pro"] .hlr-summary,
    html[data-theme="tele-route-pro"] .hlr-results {{ border-color: var(--border-strong); background: #fff; }}


    /* Routes page UI polish: dedicated route create/edit modal rebuilt as a compact portrait card. */
    .routes-page > h1 {{ display: none; }}
    .routes-page .route-create-shell {{ width: auto; max-width: max-content; margin: 0 0 10px auto; border: 0; background: transparent; box-shadow: none; overflow: visible; }}
    .routes-page .route-primary-summary {{ display: inline-flex; align-items: center; justify-content: center; width: max-content; min-height: 36px; box-sizing: border-box; margin: 0; padding: 8px 14px; border: 1px solid #2563eb !important; border-radius: var(--radius-control); background: #2563eb !important; color: #fff !important; box-shadow: 0 4px 12px rgba(37, 99, 235, .18); font-size: 13px; font-weight: 820; letter-spacing: .01em; text-transform: uppercase; transition: background-color 140ms ease, border-color 140ms ease, box-shadow 140ms ease, color 140ms ease; }}
    .routes-page .route-primary-summary::after, .routes-page .route-create-shell[open] > .route-primary-summary::after {{ content: none !important; }}
    .routes-page .route-primary-summary:hover {{ border-color: #1d4ed8 !important; background: #1d4ed8 !important; color: #fff !important; }}
    .routes-page .route-primary-summary:focus-visible {{ outline: none; border-color: #1d4ed8 !important; background: #2563eb !important; color: #fff !important; box-shadow: 0 0 0 3px rgba(37, 99, 235, .22); }}
    .routes-page .route-primary-summary:active, .routes-page .route-create-shell[open] > .route-primary-summary {{ border-color: #1e40af !important; background: #1e40af !important; color: #fff !important; }}
    .routes-page .table-footer-tools {{ align-items: center; justify-content: flex-end; gap: 8px; }}
    .routes-page .table-footer-tools .column-settings {{ order: 1; }}
    .routes-page .table-footer-tools .export-button {{ order: 2; min-width: auto; width: auto; min-height: 31px; padding: 5px 11px; border-color: var(--accent-strong); background: var(--accent); color: #fff; font-size: 12px; font-weight: 750; }}
    .routes-page .table-footer-tools .export-button:hover {{ border-color: var(--accent-hover); background: var(--accent-hover); color: #fff; }}
    .routes-page .hlr-like-column-panel {{ width: min(420px, 88vw); max-height: min(430px, 70vh); padding: 10px; border-radius: var(--radius-card); gap: 8px; overflow: hidden; }}
    .routes-page .hlr-like-column-panel .column-settings-panel-actions {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
    .routes-page .hlr-like-column-panel .column-settings-list {{ display: grid; gap: 6px; max-height: min(340px, 56vh); overflow: auto; overscroll-behavior: contain; padding-right: 2px; }}
    .routes-page .hlr-like-column-panel .column-settings-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 6px; padding: 6px; border: 1px solid var(--border); border-radius: var(--radius-small); background: var(--surface-muted); }}
    .routes-page .hlr-like-column-panel .column-settings-row label {{ display: flex; align-items: center; gap: 7px; min-width: 0; margin: 0; font-weight: 650; }}
    .routes-page .hlr-like-column-panel .column-order-button {{ min-width: 32px; padding: 3px 7px; box-shadow: none; }}
    .modal-form-card[open] > form.route-dialog, .route-dialog.route-dialog {{ position: fixed; left: 50%; top: 50%; z-index: 990; width: min(590px, calc(100vw - 48px)); max-width: calc(100vw - 48px); max-height: min(780px, calc(100vh - 48px)); margin: 0; padding: 0; transform: translate(-50%, -50%); display: grid; grid-template-columns: 1fr; grid-template-rows: auto minmax(0, 1fr) auto; gap: 0; overflow: hidden; border: 1px solid var(--border-strong); border-radius: 14px; background: #fff; color: var(--text); box-shadow: 0 22px 62px rgba(15, 23, 42, .22); box-sizing: border-box; }}
    .route-dialog.route-dialog-page-form {{ position: relative; left: auto; top: auto; transform: none; z-index: auto; margin: 0 0 16px; }}
    .route-dialog-header {{ grid-column: 1 / -1; align-self: stretch; width: 100%; max-width: none; box-sizing: border-box; margin: 0; padding: 15px 20px 13px; border-bottom: 1px solid var(--border-strong); background: linear-gradient(180deg, #fff 0%, #f8fafc 100%); }}
    .route-dialog-header h2 {{ margin: 0; color: var(--text-strong); font-size: 18px; font-weight: 860; line-height: 1.2; }}
    .route-dialog-body {{ min-height: 0; overflow-y: auto; overflow-x: hidden; scrollbar-gutter: stable; }}
    .route-dialog-section {{ display: grid; gap: 10px; min-width: 0; margin: 0; padding: 14px 20px 16px; border: 0; border-bottom: 1px solid #e5edf7; background: #fff; }}
    .route-dialog-section h3 {{ margin: 0; color: #1e3a5f; font-size: 12px; font-weight: 850; letter-spacing: .03em; text-transform: uppercase; }}
    .route-dialog-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 11px 12px; }}
    .route-dialog label {{ min-width: 0; margin: 0; color: var(--text); font-size: 12px; font-weight: 740; }}
    .route-dialog input, .route-dialog select, .route-dialog textarea {{ width: 100%; min-height: 36px; box-sizing: border-box; }}
    .route-dialog textarea {{ min-height: 58px; resize: vertical; line-height: 1.35; }}
    .route-dialog .route-dialog-full {{ grid-column: 1 / -1; }}
    .route-dialog-footer {{ display: flex; justify-content: flex-start; align-items: center; gap: 10px; grid-column: 1 / -1; align-self: stretch; width: 100%; box-sizing: border-box; margin: 0; padding: 14px 20px; border-top: 1px solid var(--border-strong); background: #eef5ff; }}
    .route-dialog-footer .modal-save {{ order: 1; border-color: #2563eb; background: #2563eb; color: #fff; }}
    .route-dialog-footer .modal-save:hover {{ border-color: #1d4ed8; background: #1d4ed8; color: #fff; }}
    .route-dialog-footer .modal-cancel {{ order: 2; }}
    @media (max-width: 720px) {{ .modal-form-card[open] > form.route-dialog, .route-dialog.route-dialog {{ width: calc(100vw - 18px); max-width: calc(100vw - 18px); max-height: calc(100vh - 18px); }} .route-dialog-grid {{ grid-template-columns: 1fr; }} .route-dialog-section, .route-dialog-header, .route-dialog-footer {{ padding-left: 16px; padding-right: 16px; }} }}
    html[data-theme="tele-route-pro"] .routes-page .table-footer-tools .export-button {{ width: auto; min-width: auto; padding: 5px 11px; border-color: var(--info); background: var(--info); color: #fff; }}
    html[data-theme="tele-route-pro"] .routes-page .route-create-shell > .form-summary {{ border-color: #2563eb; background: #2563eb; color: #fff; }}
    html[data-theme="tele-route-pro"] .routes-page .route-create-shell > .form-summary::after, html[data-theme="tele-route-pro"] .routes-page .route-create-shell[open] > .form-summary::after {{ content: none; }}

    /* TeleRoute Pro forms/modals/details/states foundation v1. UI-only theme layer. */
    html[data-theme="tele-route-pro"] select {{ width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    html[data-theme="tele-route-pro"] input[type="date"],
    html[data-theme="tele-route-pro"] input[type="number"],
    html[data-theme="tele-route-pro"] input[type="search"],
    html[data-theme="tele-route-pro"] input[type="text"],
    html[data-theme="tele-route-pro"] input[type="email"],
    html[data-theme="tele-route-pro"] input[type="password"],
    html[data-theme="tele-route-pro"] input[type="tel"],
    html[data-theme="tele-route-pro"] input[type="url"],
    html[data-theme="tele-route-pro"] select,
    html[data-theme="tele-route-pro"] textarea {{ width: 100%; }}
    html[data-theme="tele-route-pro"] input[type="file"] {{ min-height: var(--control-height); padding: 5px 8px; border: 1px dashed var(--border-ink); border-radius: 8px; background: #fbfdff; color: var(--muted); }}
    html[data-theme="tele-route-pro"] input[type="file"]::file-selector-button {{ min-height: 26px; margin-right: 8px; border: 1px solid var(--border-strong); border-radius: 6px; background: #fff; color: var(--text-strong); font-weight: 760; }}
    html[data-theme="tele-route-pro"] .form-grid label,
    html[data-theme="tele-route-pro"] .filter-grid label,
    html[data-theme="tele-route-pro"] .modal-card label,
    html[data-theme="tele-route-pro"] .modal-form-card label {{ min-width: 0; }}
    html[data-theme="tele-route-pro"] .form-grid label:not(.checkbox-inline):not(.scope-card):not(.spillover-checkbox):not(.important-checkbox),
    html[data-theme="tele-route-pro"] .modal-card label:not(.checkbox-inline):not(.scope-card):not(.spillover-checkbox):not(.important-checkbox),
    html[data-theme="tele-route-pro"] .modal-form-card label:not(.checkbox-inline):not(.scope-card):not(.spillover-checkbox):not(.important-checkbox) {{ display: flex; flex-direction: column; gap: 4px; white-space: normal; }}
    html[data-theme="tele-route-pro"] label.required,
    html[data-theme="tele-route-pro"] .required {{ display: inline-flex; align-items: baseline; gap: 3px; }}
    html[data-theme="tele-route-pro"] label.required::after,
    html[data-theme="tele-route-pro"] .required::after {{ display: inline; flex: 0 0 auto; margin-left: 1px; }}
    html[data-theme="tele-route-pro"] .form-actions button,
    html[data-theme="tele-route-pro"] .modal-actions button,
    html[data-theme="tele-route-pro"] .modal-footer button,
    html[data-theme="tele-route-pro"] .admin-edit-actions button {{ min-height: 32px; padding: 6px 11px; }}
    html[data-theme="tele-route-pro"] .modal-form-card > summary {{ border-color: var(--border-ink); background: #111827; color: #fff; box-shadow: none; }}
    html[data-theme="tele-route-pro"] .modal-form-card > summary:hover {{ border-color: #020617; background: #020617; color: #fff; }}
    html[data-theme="tele-route-pro"] .modal-form-card[open]::before {{ background: rgba(15, 23, 42, .50); backdrop-filter: none; }}
    html[data-theme="tele-route-pro"] .modal-form-card[open] > form,
    html[data-theme="tele-route-pro"] .modal-form-card[open] > .modal-body,
    html[data-theme="tele-route-pro"] .modal-card {{ border-radius: 14px; border-color: var(--border-ink); box-shadow: 0 22px 54px rgba(15, 23, 42, .24); }}
    html[data-theme="tele-route-pro"] .modal-form-card[open] > form {{ gap: 11px 12px; }}
    html[data-theme="tele-route-pro"] .modal-save,
    html[data-theme="tele-route-pro"] .admin-edit-save {{ border-color: #111827; background: #111827; color: #fff; }}
    html[data-theme="tele-route-pro"] .modal-save:hover,
    html[data-theme="tele-route-pro"] .admin-edit-save:hover {{ border-color: #020617; background: #020617; color: #fff; }}
    html[data-theme="tele-route-pro"] .routes-page .route-primary-summary {{ border-color: #2563eb !important; background: #2563eb !important; color: #fff !important; }}
    html[data-theme="tele-route-pro"] .routes-page .route-primary-summary:hover {{ border-color: #1d4ed8 !important; background: #1d4ed8 !important; color: #fff !important; }}
    html[data-theme="tele-route-pro"] .routes-page .route-primary-summary:focus-visible {{ border-color: #1d4ed8 !important; background: #2563eb !important; color: #fff !important; }}
    html[data-theme="tele-route-pro"] .routes-page .route-primary-summary:active, html[data-theme="tele-route-pro"] .routes-page .route-create-shell[open] > .route-primary-summary {{ border-color: #1e40af !important; background: #1e40af !important; color: #fff !important; }}
    html[data-theme="tele-route-pro"] .routes-page .route-primary-summary::after, html[data-theme="tele-route-pro"] .routes-page .route-create-shell[open] > .route-primary-summary::after {{ content: none !important; }}
    html[data-theme="tele-route-pro"] .modal-form-card[open] > form.route-dialog, html[data-theme="tele-route-pro"] .route-dialog.route-dialog {{ gap: 0; padding: 0; }}
    html[data-theme="tele-route-pro"] .route-dialog-header, html[data-theme="tele-route-pro"] .route-dialog-footer {{ grid-column: 1 / -1; width: 100%; max-width: none; box-sizing: border-box; margin: 0; }}
    html[data-theme="tele-route-pro"] .route-dialog-footer {{ justify-content: flex-start; }}
    html[data-theme="tele-route-pro"] .modal-cancel,
    html[data-theme="tele-route-pro"] .admin-edit-cancel {{ border-color: var(--border-strong); background: #fff; color: #334155; }}
    html[data-theme="tele-route-pro"] .modal-cancel:hover,
    html[data-theme="tele-route-pro"] .admin-edit-cancel:hover {{ border-color: var(--border-ink); background: #f1f5f9; color: var(--text-strong); }}
    html[data-theme="tele-route-pro"] details:not(.current-user-selector):not(.column-settings):not(.filter-card):not(.modal-form-card) > form,
    html[data-theme="tele-route-pro"] details:not(.current-user-selector):not(.column-settings):not(.filter-card):not(.modal-form-card) > .card,
    html[data-theme="tele-route-pro"] details:not(.current-user-selector):not(.column-settings):not(.filter-card):not(.modal-form-card) > p,
    html[data-theme="tele-route-pro"] details:not(.current-user-selector):not(.column-settings):not(.filter-card):not(.modal-form-card) > table {{ margin: 0; padding: 12px; background: #fff; }}
    html[data-theme="tele-route-pro"] details.filter-card > .filter-summary::before,
    html[data-theme="tele-route-pro"] details.modal-form-card > summary::before {{ content: none; }}
    html[data-theme="tele-route-pro"] .empty-state strong,
    html[data-theme="tele-route-pro"] .empty-row strong,
    html[data-theme="tele-route-pro"] td.empty strong {{ display: block; margin-bottom: 2px; color: var(--text-strong); }}
    html[data-theme="tele-route-pro"] .badge,
    html[data-theme="tele-route-pro"] .status-badge {{ gap: 5px; border-radius: 6px; }}
    html[data-theme="tele-route-pro"] .badge::before,
    html[data-theme="tele-route-pro"] .status-badge::before {{ content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; opacity: .85; }}
    html[data-theme="tele-route-pro"] .alert.success,
    html[data-theme="tele-route-pro"] .flash.success,
    html[data-theme="tele-route-pro"] .message.success {{ border-color: var(--success-border); background: var(--success-soft); color: #15803d; }}
    html[data-theme="tele-route-pro"] .alert.warning,
    html[data-theme="tele-route-pro"] .flash.warning,
    html[data-theme="tele-route-pro"] .message.warning {{ border-color: var(--warning-border); background: var(--warning-soft); color: #b45309; }}
    html[data-theme="tele-route-pro"] .alert.danger,
    html[data-theme="tele-route-pro"] .alert.error,
    html[data-theme="tele-route-pro"] .flash.danger,
    html[data-theme="tele-route-pro"] .flash.error,
    html[data-theme="tele-route-pro"] .message.danger,
    html[data-theme="tele-route-pro"] .message.error {{ border-color: var(--danger-border); background: var(--danger-soft); color: var(--danger-strong); }}
    html[data-theme="tele-route-pro"] .alert.neutral,
    html[data-theme="tele-route-pro"] .flash.neutral,
    html[data-theme="tele-route-pro"] .message.neutral {{ border-color: var(--border-strong); background: #f1f5f9; color: #475569; }}
    html[data-theme="tele-route-pro"] .checkbox-list label,
    html[data-theme="tele-route-pro"] .permission-matrix label,
    html[data-theme="tele-route-pro"] label.checkbox-inline {{ display: inline-flex; align-items: center; gap: 7px; min-height: 30px; padding: 5px 8px; border: 1px solid var(--border); border-radius: 7px; background: #fff; color: var(--text); }}
    html[data-theme="tele-route-pro"] .checkbox-list label:has(input:checked),
    html[data-theme="tele-route-pro"] .permission-matrix label:has(input:checked),
    html[data-theme="tele-route-pro"] label.checkbox-inline:has(input:checked) {{ border-color: var(--border-ink); background: #f1f5f9; color: var(--text-strong); }}
    html[data-theme="tele-route-pro"] .important-checkbox input[type="checkbox"],
    html[data-theme="tele-route-pro"] .spillover-checkbox input[type="checkbox"],
    html[data-theme="tele-route-pro"] label:has(input[name="review_required"]) input[type="checkbox"],
    html[data-theme="tele-route-pro"] label:has(input[name="has_overflow"]) input[type="checkbox"] {{ width: 18px; height: 18px; min-width: 18px; min-height: 18px; }}
    html[data-theme="tele-route-pro"] .important-checkbox:has(input:checked),
    html[data-theme="tele-route-pro"] .spillover-checkbox:has(input:checked),
    html[data-theme="tele-route-pro"] label:has(input[name="review_required"]:checked),
    html[data-theme="tele-route-pro"] label:has(input[name="has_overflow"]:checked) {{ border-color: var(--warning); background: var(--warning-soft); color: #92400e; }}
    html[data-theme="tele-route-pro"] .scope-cards,
    html[data-theme="tele-route-pro"] .segmented-control,
    html[data-theme="tele-route-pro"] .tabs,
    html[data-theme="tele-route-pro"] .safe-rename-options {{ display: inline-flex; align-items: stretch; flex-wrap: wrap; }}
    html[data-theme="tele-route-pro"] .safe-rename-option {{ border: 1px solid var(--border); border-radius: 7px; background: transparent; color: #475569; }}
    html[data-theme="tele-route-pro"] .safe-rename-option:has(input:checked) {{ border-color: var(--border-ink); background: #fff; color: var(--text-strong); box-shadow: 0 1px 2px rgba(17, 24, 39, .08); }}
    html[data-theme="tele-route-pro"] #hlr-form input,
    html[data-theme="tele-route-pro"] #hlr-form select,
    html[data-theme="tele-route-pro"] #hlr-form textarea {{ border-color: var(--border-strong); background: #fff; }}

    @media (max-width: 1020px) {{
      html[data-theme="light-v2"] #routing-event-form,
      html[data-theme="light-v2"] #routing-event-form[data-current-scope='campaign_setting'] .provider-change-campaign-grid,
      html[data-theme="light-v2"] #routing-event-form[data-current-scope='campaign_setting'] .provider-change-campaign-lower-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      html[data-theme="light-v2"] #routing-event-form fieldset.scope-field[data-scopes="server_priority"] {{ grid-column: 1 / -1; }}
    }}

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
        <div class="page-top">
          <div class="page-crumbs">{breadcrumbs(title)}</div>
          <div class="topbar" aria-label="Настройки интерфейса">{theme_selector()}{current_user_selector()}</div>
        </div>
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
    const themeLabels = {{ "dark": "Тёмная", "light-v2": "Светлая 2.0", "tele-route-pro": "TeleRoute Pro" }};
    const themeAliases = {{ "mvp": "light-v2", "calm-blue": "light-v2", "cyber-sketch": "dark", "terminal-paper": "light-v2" }};
    const normalizeTheme = (theme) => themeAliases[theme] || (themeLabels[theme] ? theme : "light-v2");
    let savedTheme = normalizeTheme(localStorage.getItem("mvp-theme") || "light-v2");
    document.documentElement.dataset.theme = savedTheme;
    localStorage.setItem("mvp-theme", savedTheme);
    const updateThemeSelector = (theme) => {{
      const labelText = `Тема: ${{themeLabels[theme] || themeLabels["light-v2"]}} ▾`;
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
      if (form.classList.contains("route-dialog-form")) {{
        form.dataset.modalEnhanced = "1";
        form.querySelectorAll("[data-modal-close]").forEach((button) => button.addEventListener("click", closeCallback));
        return;
      }}
      form.dataset.modalEnhanced = "1";
      const saveButton = form.querySelector('button[type="submit"], button:not([type]), input[type="submit"]');
      let actions = form.querySelector(":scope > .modal-actions");
      if (!actions) {{
        actions = document.createElement("div");
        actions.className = "modal-actions";
        if (saveButton) saveButton.parentNode.insertBefore(actions, saveButton);
        else form.appendChild(actions);
      }}
      let cancel = actions.querySelector(":scope > .modal-cancel, :scope > [data-modal-close]");
      if (!cancel) {{
        cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "modal-cancel";
        cancel.textContent = "Отмена";
      }}
      if (saveButton) {{
        saveButton.classList.add("modal-save");
        actions.appendChild(saveButton);
      }}
      actions.appendChild(cancel);
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
        placePanel();
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
      function placePanel() {{
        const panel = settings.querySelector(".column-settings-panel");
        const summary = settings.querySelector("summary");
        if (!panel || !summary || !settings.open) return;
        panel.classList.remove("open-up");
        const buttonRect = summary.getBoundingClientRect();
        const panelHeight = Math.min(panel.scrollHeight || 380, Math.round(window.innerHeight * 0.7));
        const spaceBelow = window.innerHeight - buttonRect.bottom;
        const spaceAbove = buttonRect.top;
        if (panel.classList.contains("hlr-like-column-panel") || (spaceBelow < panelHeight + 12 && spaceAbove > spaceBelow)) panel.classList.add("open-up");
      }}
      function closePanel() {{
        settings.open = false;
        settings.querySelector(".column-settings-panel")?.classList.remove("open-up");
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
      settings.addEventListener("toggle", () => {{ if (settings.open) window.requestAnimationFrame(placePanel); }});
      settings.addEventListener("click", (event) => {{ if (event.target.closest(".column-settings-panel")) event.stopPropagation(); }});
      document.addEventListener("click", (event) => {{
        if (settings.open && !settings.contains(event.target)) closePanel();
      }});
      document.addEventListener("keydown", (event) => {{
        if (event.key === "Escape" && settings.open) closePanel();
      }});
      window.addEventListener("resize", placePanel);
      window.addEventListener("scroll", placePanel, true);
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
    if path == "/hlr":
        return "hlr"
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
        "/admin/import/template": "admin_import_export",
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
    if path in {"/hlr/check", "/hlr/export.csv", "/hlr/balance", "/hlr/config/daily-limit", "/hlr/config/daily-limit/reset"}:
        return "hlr"
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


def form_card(summary: str, form_html: str, *, open_by_default: bool = False, extra_class: str = "", summary_class: str = "") -> str:
    open_attr = " open" if open_by_default else ""
    classes = f"form-card modal-form-card {extra_class}".strip()
    summary_classes = f"form-summary {summary_class}".strip()
    return f"<details class='{classes}'{open_attr} data-modal-details><summary class='{summary_classes}'>{summary}</summary>{form_html}</details>"


def table_page_container(inner_html: str, *, extra_class: str = "") -> str:
    classes = f"table-page-container {extra_class}".strip()
    return f"<div class='{classes}'>{inner_html}</div>"


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


def column_settings(table_key: str, columns: list[tuple[str, str]], *, hlr_style: bool = False) -> str:
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
    panel_class = "column-settings-panel hlr-like-column-panel" if hlr_style else "column-settings-panel"
    panel_header = "<div class='column-settings-panel-actions'><strong>Вид таблицы</strong><button type='button' class='column-reset' data-column-reset title='Сбросить колонки'>Сбросить вид таблицы</button></div>" if hlr_style else ""
    panel_footer = "" if hlr_style else "<button type='button' class='column-reset' data-column-reset title='Сбросить колонки'>Сбросить вид таблицы</button>"
    return f"""<details class='column-settings' data-column-settings='{esc(table_key)}' data-storage-key='{esc(table_storage_key(table_key))}'>
<summary>Колонки</summary>
<div class='{panel_class}'>{panel_header}<div class='column-settings-list' data-column-settings-list>{''.join(rows)}</div>{panel_footer}</div>
</details>"""


def table_footer(summary_html: str, utility_html: str = "") -> str:
    if not summary_html:
        summary_html = "<nav class='pagination table-status-nav' aria-label='Статус таблицы'><span class='table-status-summary'><span class='table-status-item'>Всего записей: 0</span><span class='table-status-item table-selection-status' data-selected-count hidden>Выбрано: <strong>0</strong></span><span class='table-status-item'>Страница 1 из 1</span></span></nav>"
    return f"<div class='table-footer table-status-action-bar'><div class='table-footer-summary'>{summary_html}</div><div class='table-footer-tools'>{utility_html}</div></div>"

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
            f"<span class='server-route-hint' data-current-route-hint data-server-id='{row['id']}' title='{esc(hint)}'>{esc(hint)}</span></span></label>"
        )
    return (
        "<div class='server-checkbox-toolbar'>"
        "<span class='server-selection-count' data-server-selection-count>0 из 0 выбрано</span>"
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


HLR_MAX_NUMBERS = 500
HLR_BATCH_SIZE = 80
HLR_RESULT_HEADERS = ["Исходный номер", "Нормализованный номер", "Detected number", "Formatted number", "Формат", "Страна", "Тип номера", "Raw telephone_number_type", "Оператор / сеть", "HLR статус", "Live status", "Итог", "Оценка лида", "Почему такая оценка", "Комментарий"]
HLR_EXTRA_CSV_FIELDS = [("current_network", "Current network"), ("current_operator", "Current operator"), ("current_mccmnc", "Current MCCMNC"), ("current_country", "Current country"), ("current_country_iso3", "Current ISO3"), ("current_country_prefix", "Current country prefix"), ("original_network", "Original network"), ("original_operator", "Original operator"), ("original_mccmnc", "Original MCCMNC"), ("original_country", "Original country"), ("original_country_iso3", "Original ISO3"), ("original_area", "Original area"), ("original_country_prefix", "Original country prefix"), ("is_ported", "Is ported"), ("ported_date", "Ported date"), ("uuid", "UUID"), ("timestamp", "Timestamp"), ("credits_spent", "Credits spent"), ("raw_error", "Raw error"), ("raw_message", "Raw message")]
HLR_SENSITIVE_KEY_PARTS = ("secret", "key", "token", "auth", "authorization", "password", "credential")
HLR_STATUS_MAP = {
    "LIVE": ("ok", "OK", "Номер назначен абоненту и HLR подтверждает активный статус. Это хороший сигнал, но не гарантия, что клиент ответит на звонок именно сейчас."),
    "DEAD": ("bad", "DEAD", "Номер подтверждён сетью как не назначенный абоненту/SIM. Такой номер, как правило, не сможет принимать звонки или SMS. Сильный плохой сигнал качества лида."),
    "ABSENT_SUBSCRIBER": ("warning", "WARNING", "Номер назначен мобильной SIM, но сеть считает абонента недоступным: телефон может быть выключен, вне зоны сети, давно не регистрировался в сети, либо SIM ещё не активирована пользователем. HLR Lookup не раскрывает точный срок ‘долгой неактивности’. Один такой статус не доказывает мусорный лид, но массовая доля ABSENT_SUBSCRIBER может объяснять высокий недозвон."),
    "NO_TELESERVICE_PROVISIONED": ("warning", "WARNING", "Номер есть в HLR и выглядит активным, но у подписки не подключён нужный телесервис, например SMS. Для голосового прозвона трактовать осторожно: это warning, а не прямое доказательство плохого номера."),
    "NOT_AVAILABLE_NETWORK_ONLY": ("unknown", "UNKNOWN", "Сеть не отдаёт live/dead статус для этого номера. Номер может быть реальным, но HLR не может подтвердить активность. Можно использовать network/porting данные, если они есть, но нельзя считать такой номер DEAD."),
    "NO_COVERAGE": ("unknown", "UNKNOWN", "У HLR Lookup нет покрытия для определения live-status по этой сети. Это ограничение проверки, а не доказательство плохого номера."),
    "NOT_APPLICABLE": ("unknown", "UNKNOWN", "Live-status неприменим: например, HLR не смог определить номер или тип номера не подходит для такой проверки. Смотреть вместе с форматом и типом номера."),
    "INCONCLUSIVE": ("unknown", "UNKNOWN", "HLR не смог однозначно определить статус. Номер не подтверждён как плохой, но и не подтверждён как активный."),
}
HLR_BAD_FORMAT_COMMENT = "Номер не удалось распознать как корректный международный телефонный номер. Часто это ошибка склейки номера источником трафика: код страны, лишний 0, пропущенная цифра, лишние символы или неверная длина."
HLR_ERROR_COMMENT = "Проверка не выполнена из-за ошибки API, таймаута, недостатка кредитов или ошибки провайдера. Это не статус номера, а статус самой проверки."
HLR_UNEXPECTED_COMMENT = "Неожиданный формат ответа HLR API."
HLR_MISSING_LIVE_STATUS_COMMENT = "HLR вернул данные о номере и сети, но не вернул live-status. Номер не подтверждён как LIVE и не подтверждён как DEAD. Формат и сеть определены, но live/dead статус отсутствует. Это не плохой номер, но активность не подтверждена."


def hlr_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or str(default))
    except ValueError:
        return default


def hlr_env_daily_limit() -> tuple[int, str]:
    raw = os.environ.get("HLR_DAILY_CHECK_LIMIT")
    if raw not in (None, ""):
        try:
            value = int(raw)
            if value > 0:
                return value, "env"
        except ValueError:
            pass
    return 2000, "fallback"


def app_setting_value(key: str) -> str | None:
    repo = _REQUEST_CONTEXT.get("repo")
    if repo is None:
        return None
    row = repo.conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] in (None, ""):
        return None
    return str(row["value"])


def set_app_setting_value(key: str, value: str, updated_by: int | None = None) -> None:
    repo = _REQUEST_CONTEXT.get("repo")
    if repo is None:
        raise BusinessRuleError("Хранилище настроек недоступно.")
    repo.conn.execute(
        """
        INSERT INTO app_settings(key, value, updated_at, updated_by)
        VALUES (?, ?, CURRENT_TIMESTAMP, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at,
            updated_by = excluded.updated_by
        """,
        (key, value, updated_by),
    )
    repo.conn.commit()


def delete_app_setting_value(key: str) -> None:
    repo = _REQUEST_CONTEXT.get("repo")
    if repo is None:
        raise BusinessRuleError("Хранилище настроек недоступно.")
    repo.conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    repo.conn.commit()


HLR_DAILY_LIMIT_OVERRIDE_KEY = "hlr_daily_limit_override"
HLR_DAILY_LIMIT_MIN = 1
HLR_DAILY_LIMIT_MAX = 100000
HLR_DAILY_LIMIT_ERROR = "Дневной лимит должен быть целым числом от 1 до 100000."


def hlr_daily_limit_state() -> dict[str, object]:
    env_limit, env_source = hlr_env_daily_limit()
    override_raw = app_setting_value(HLR_DAILY_LIMIT_OVERRIDE_KEY)
    override_value = None
    if override_raw not in (None, ""):
        try:
            parsed = int(str(override_raw))
            if HLR_DAILY_LIMIT_MIN <= parsed <= HLR_DAILY_LIMIT_MAX:
                override_value = parsed
        except ValueError:
            override_value = None
    effective = override_value if override_value is not None else env_limit
    source = "admin_override" if override_value is not None else env_source
    return {
        "daily_limit_effective": effective,
        "daily_limit_source": source,
        "daily_limit_env": env_limit,
        "daily_limit_env_source": env_source,
        "daily_limit_override": override_value,
    }


def validate_hlr_daily_limit_override(value: object) -> int:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d+", text):
        raise BusinessRuleError(HLR_DAILY_LIMIT_ERROR)
    parsed = int(text)
    if parsed < HLR_DAILY_LIMIT_MIN or parsed > HLR_DAILY_LIMIT_MAX:
        raise BusinessRuleError(HLR_DAILY_LIMIT_ERROR)
    return parsed


def save_hlr_daily_limit_override(value: object) -> None:
    parsed = validate_hlr_daily_limit_override(value)
    actor_id = int(_REQUEST_CONTEXT.get("current_user_id") or 0) or None
    set_app_setting_value(HLR_DAILY_LIMIT_OVERRIDE_KEY, str(parsed), actor_id)


def reset_hlr_daily_limit_override() -> None:
    delete_app_setting_value(HLR_DAILY_LIMIT_OVERRIDE_KEY)


def hlr_config() -> dict[str, object]:
    return {
        "mode": (os.environ.get("HLR_MODE") or "demo").strip().lower(),
        "api_url": os.environ.get("HLR_API_URL") or "",
        "api_key": os.environ.get("HLR_API_KEY") or "",
        "api_secret": os.environ.get("HLR_API_SECRET") or "",
        "balance_url": os.environ.get("HLR_BALANCE_URL") or os.environ.get("HLR_API_BALANCE_URL") or "",
        "timeout_ms": hlr_int_env("HLR_TIMEOUT_MS", 30000),
        "concurrency": hlr_int_env("HLR_CONCURRENCY", 1),
        "daily_limit": hlr_daily_limit_state()["daily_limit_effective"],
    }


def hlr_config_source(name: str) -> str:
    if name in DOTENV_SOURCE_KEYS and os.environ.get(name):
        return ".env"
    if os.environ.get(name):
        return "os.environ"
    return "unknown"


def hlr_safe_api_url(api_url: object) -> str:
    value = str(api_url or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value.split("@")[-1]
    host = parts.hostname or ""
    netloc = host
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return parts._replace(netloc=netloc).geturl()


def hlr_safe_config_summary() -> dict[str, object]:
    config = hlr_config()
    return {
        "mode": config["mode"],
        "api_url_present": bool(config["api_url"]),
        "api_url": hlr_safe_api_url(config["api_url"]),
        "api_key_present": bool(config["api_key"]),
        "api_secret_present": bool(config["api_secret"]),
        "balance_url_present": bool(config.get("balance_url")),
        "timeout_ms": config["timeout_ms"],
        "concurrency": config["concurrency"],
        "daily_limit": config["daily_limit"],
        **hlr_daily_limit_state(),
        "dotenv_loaded": "yes" if DOTENV_LOADED else ("no" if DOTENV_SOURCE_KEYS else "unknown"),
        "config_source": hlr_config_source("HLR_MODE"),
    }



def hlr_balance_url(config: dict[str, object]) -> str:
    explicit = str(config.get("balance_url") or "").strip()
    if explicit:
        return explicit
    api_url = str(config.get("api_url") or "").strip()
    if not api_url:
        return ""
    if api_url.rstrip("/").endswith("/hlr"):
        return api_url.rstrip("/")[:-4] + "/balance"
    return api_url.rstrip("/") + "/balance"


def hlr_balance_empty_state(status: str = "unavailable", error_message: str | None = "Нажмите «Обновить баланс», чтобы запросить API.") -> dict[str, object]:
    return {"status": status, "credits": None, "updated_at": None, "error_message": error_message}


def get_hlr_balance() -> dict[str, object]:
    config = hlr_config()
    if config.get("mode") in {"demo", ""}:
        return hlr_balance_empty_state("unavailable", "Demo mode: реальный API баланса не вызывается.")
    if hlr_config_incomplete(config):
        return hlr_balance_empty_state("not_configured", "HLR API credentials не настроены.")
    balance_url = hlr_balance_url(config)
    if not balance_url:
        return hlr_balance_empty_state("unavailable", "Endpoint/helper баланса не настроен.")
    timeout = max(1, min(30, int(config.get("timeout_ms") or 30000) / 1000))
    body = json.dumps({"api_key": str(config.get("api_key") or ""), "api_secret": str(config.get("api_secret") or "")}, ensure_ascii=False).encode("utf-8")
    req = Request(balance_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            response_text = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(response_text)
    except HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        message = hlr_sanitized_api_message(response_text, config) or f"HTTP {exc.code}"
        return hlr_balance_empty_state("error", message[:180])
    except TimeoutError:
        return hlr_balance_empty_state("unavailable", "HLR balance API timeout.")
    except URLError as exc:
        reason = str(getattr(exc, "reason", "") or "connection error")
        return hlr_balance_empty_state("unavailable", hlr_sanitize_text(reason, config)[:180])
    except (ValueError, json.JSONDecodeError):
        return hlr_balance_empty_state("error", "HLR balance API вернул неожиданный формат ответа.")
    if not isinstance(payload, dict):
        return hlr_balance_empty_state("error", "HLR balance API вернул неожиданный формат ответа.")
    status = str(payload.get("Status") or payload.get("status") or "").strip().upper()
    credits = payload.get("Credits", payload.get("credits"))
    if status == "OK" and isinstance(credits, (int, float)):
        return {"status": "ok", "credits": credits, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "error_message": None}
    safe_message = hlr_sanitize_text(str(payload.get("Message") or payload.get("message") or payload.get("Error") or "HLR balance API returned an error."), config)
    return hlr_balance_empty_state("error", safe_message[:180])

def hlr_config_incomplete(config: dict[str, object]) -> bool:
    return not (config.get("api_url") and config.get("api_key") and config.get("api_secret"))


def hlr_log_startup_config() -> None:
    global HLR_STARTUP_LOGGED
    if HLR_STARTUP_LOGGED:
        return
    HLR_STARTUP_LOGGED = True
    summary = hlr_safe_config_summary()
    print(
        "HLR config:\n"
        f"- mode: {summary['mode']}\n"
        f"- api_url_present: {str(summary['api_url_present']).lower()}\n"
        f"- api_key_present: {str(summary['api_key_present']).lower()}\n"
        f"- api_secret_present: {str(summary['api_secret_present']).lower()}\n"
        f"- timeout_ms: {summary['timeout_ms']}\n"
        f"- concurrency: {summary['concurrency']}"
    )


hlr_log_startup_config()


def hlr_normalize(raw: str) -> tuple[str, bool]:
    value = (raw or "").strip()
    if not value:
        return "", False
    cleaned = re.sub(r"[\s\-()./\\]+", "", value)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if cleaned.count("+") > 1 or ("+" in cleaned and not cleaned.startswith("+")):
        return cleaned, False
    if cleaned.startswith("+"):
        digits = cleaned[1:]
    elif re.fullmatch(r"\d+", cleaned or ""):
        digits = cleaned
        cleaned = "+" + cleaned
    else:
        return cleaned, False
    if not re.fullmatch(r"\d{10,15}", digits):
        return cleaned, False
    return "+" + digits, True


HLR_NUMBER_TYPE_LABELS = {
    "bad_format": "Некорректный формат",
    "mobile": "Мобильный",
    "landline": "Городской",
    "mobile_or_landline": "Мобильный или городской",
    "toll_free": "Toll-free",
    "premium": "Premium",
    "shared_cost": "Shared cost",
    "voip": "VoIP",
    "stage_and_screen": "Stage/screen",
    "pager": "Pager",
    "universal_access_number": "Universal access",
    "personal_number": "Personal number",
    "voicemail_only": "Voicemail only",
    "machine_to_machine": "Machine-to-machine",
    "unknown": "Неизвестно",
    "other": "Другое",
}
HLR_LEAD_QUALITY_LABELS = {
    "strong_good": "Хороший сигнал",
    "weak_good": "Скорее норм",
    "warning": "Warning",
    "strong_bad": "Плохой сигнал",
    "technical_bad": "Ошибка формата",
    "unknown": "Неизвестно",
    "check_error": "Ошибка проверки",
}


def hlr_number_type(raw_type: object, bad_format: bool = False) -> str:
    if bad_format:
        return "bad_format"
    normalized = str(raw_type or "").strip().upper().replace("-", "_").replace(" ", "_")
    mapping = {
        "BAD_FORMAT": "bad_format",
        "MOBILE": "mobile",
        "LANDLINE": "landline",
        "FIXED_LINE": "landline",
        "MOBILE_OR_LANDLINE": "mobile_or_landline",
        "TOLL_FREE": "toll_free",
        "PREMIUM": "premium",
        "SHARED_COST": "shared_cost",
        "VOIP": "voip",
        "STAGE_AND_SCREEN": "stage_and_screen",
        "PAGER": "pager",
        "UNIVERSAL_ACCESS_NUMBER": "universal_access_number",
        "PERSONAL_NUMBER": "personal_number",
        "VOICEMAIL_ONLY": "voicemail_only",
        "MACHINE_TO_MACHINE": "machine_to_machine",
        "UNKNOWN": "unknown",
        "": "unknown",
    }
    return mapping.get(normalized, "other")


def hlr_type_label(number_type: str) -> str:
    return HLR_NUMBER_TYPE_LABELS.get(number_type, "Другое")


def hlr_lead_quality_signal(live_status: object, number_type: str, final_category: str) -> str:
    live = str(live_status or "").strip().upper()
    if final_category == "error" or live == "ERROR":
        return "check_error"
    if live == "BAD_FORMAT" or number_type == "bad_format":
        return "technical_bad"
    if live == "DEAD":
        return "strong_bad"
    if number_type in {"stage_and_screen", "machine_to_machine", "voicemail_only", "premium", "shared_cost", "pager"}:
        return "strong_bad" if live != "LIVE" else "warning"
    if live == "LIVE":
        return "strong_good" if number_type == "mobile" else ("weak_good" if number_type in {"landline", "mobile_or_landline", "voip", "toll_free", "personal_number", "universal_access_number"} else "warning")
    if live in {"ABSENT_SUBSCRIBER", "NO_TELESERVICE_PROVISIONED"}:
        return "warning"
    if live in {"NOT_AVAILABLE_NETWORK_ONLY", "NO_COVERAGE", "NOT_APPLICABLE", "INCONCLUSIVE"}:
        return "unknown"
    return "unknown"


def hlr_lead_quality_label(signal: object) -> str:
    return HLR_LEAD_QUALITY_LABELS.get(str(signal or "unknown"), "Неизвестно")


def hlr_lead_quality_reason(live_status: object, number_type: str, final_category: str, final_result: object = "") -> str:
    live = str(live_status or "").strip().upper()
    result = str(final_result or "").strip().upper()
    if final_category == "error" or result == "ERROR":
        return "Это ошибка проверки/API, а не статус номера."
    if number_type == "bad_format" or live == "BAD_FORMAT" or result == "BAD_FORMAT":
        return "Номер не распознан как корректный международный номер."
    if live == "LIVE":
        if number_type == "landline":
            return "HLR подтвердил LIVE, но номер определён как городской/стационарный, а для лидов ожидается мобильный."
        return "HLR подтвердил, что номер назначен абоненту и live status = LIVE."
    if live == "DEAD" or result == "DEAD":
        return "HLR подтвердил, что номер не назначен абоненту / DEAD."
    if number_type == "landline":
        return "Номер определён как городской/стационарный, а для лидов ожидается мобильный."
    if live == "ABSENT_SUBSCRIBER":
        return "Номер назначен, но абонент сейчас недоступен или не зарегистрирован в сети."
    if live == "NO_TELESERVICE_PROVISIONED":
        return "Номер существует, но часть сервисов недоступна. Для звонков нужно трактовать осторожно."
    if live in {"NO_COVERAGE", "NOT_AVAILABLE_NETWORK_ONLY", "INCONCLUSIVE", "NOT_APPLICABLE"}:
        return "HLR не смог подтвердить live/dead статус из-за ограничений сети или покрытия проверки."
    if result == "NETWORK_INFO_ONLY" or final_category == "unknown":
        return "Получены тип номера и сеть, но live/dead статус не получен."
    return "Оценка построена по HLR live status, типу номера и итоговой категории проверки."


def hlr_format_label(format_status: str) -> str:
    return "Корректный" if format_status == "valid" else "Некорректный"



def hlr_is_blank(value: object) -> bool:
    return value is None or str(value).strip() in {"", "—"}


def hlr_sanitize_api_item(value: object) -> object:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in HLR_SENSITIVE_KEY_PARTS):
                sanitized[key_text] = "***"
            else:
                sanitized[key_text] = hlr_sanitize_api_item(item)
        return sanitized
    if isinstance(value, list):
        return [hlr_sanitize_api_item(item) for item in value]
    return value


def hlr_field_paths(value: object, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in HLR_SENSITIVE_KEY_PARTS):
                continue
            path = f"{prefix}.{key_text}" if prefix else key_text
            if isinstance(item, dict):
                nested = hlr_field_paths(item, path)
                paths.extend(nested or [path])
            elif isinstance(item, list):
                paths.append(path)
                for entry in item[:3]:
                    paths.extend(hlr_field_paths(entry, path + "[]"))
            else:
                paths.append(path)
        return paths
    return []


def hlr_nested_value(raw: dict[str, object], path: str) -> object:
    value: object = raw
    for key in path.split("."):
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return value


def hlr_first_present(raw: dict[str, object], *keys: str) -> object:
    for key in keys:
        value = hlr_nested_value(raw, key) if "." in key else raw.get(key)
        if not hlr_is_blank(value):
            return value
    return ""


def hlr_make_result(original: str, normalized: str, *, format_status: str = "valid", country: object = "—", number_type_raw: object = "", operator: object = "—", hlr_status_raw: object = "", live_status_raw: object = "", final_category: str = "unknown", final_result: str = "UNKNOWN", comment: str = "HLR не вернул понятный live-status.", is_demo_result: bool = False, **extra: object) -> dict[str, object]:
    number_type = hlr_number_type(number_type_raw, format_status == "bad_format")
    lead_quality_signal = str(extra.pop("lead_quality_signal", "") or hlr_lead_quality_signal(live_status_raw or hlr_status_raw, number_type, final_category))
    result = {
        "original_number": original,
        "normalized_number": normalized,
        "format_status": format_status,
        "country": str(country or "—"),
        "number_type": number_type,
        "number_type_raw": str(number_type_raw or ""),
        "operator": str(operator or "—"),
        "hlr_status_raw": str(hlr_status_raw or ""),
        "live_status_raw": str(live_status_raw or ""),
        "final_category": final_category,
        "final_result": final_result,
        "lead_quality_signal": lead_quality_signal,
        "lead_quality_label": hlr_lead_quality_label(lead_quality_signal),
        "lead_quality_reason": str(extra.pop("lead_quality_reason", "") or hlr_lead_quality_reason(live_status_raw or hlr_status_raw, number_type, final_category, final_result)),
        "status_severity": str(extra.pop("status_severity", "") or hlr_status_severity({"final_category": final_category, "final_result": final_result, "live_status_raw": live_status_raw, "hlr_status_raw": hlr_status_raw, "number_type": number_type})),
        "comment": comment,
        "is_demo_result": is_demo_result,
    }
    result.update(extra)
    return result


def hlr_bad_result(raw: str, normalized: str, is_demo: bool = False) -> dict[str, object]:
    return hlr_make_result(raw, normalized, format_status="bad_format", number_type_raw="BAD_FORMAT", hlr_status_raw="BAD_FORMAT", live_status_raw="BAD_FORMAT", final_category="bad", final_result="BAD_FORMAT", comment=HLR_BAD_FORMAT_COMMENT, is_demo_result=is_demo)


def hlr_status_result(live_status: object, hlr_status: object = "") -> tuple[str, str, str, str]:
    raw = str(live_status or hlr_status or "").strip().upper()
    if raw == "BAD_FORMAT":
        return "bad", "BAD_FORMAT", HLR_BAD_FORMAT_COMMENT, raw
    if raw == "ERROR":
        return "error", "ERROR", HLR_ERROR_COMMENT, raw
    if not raw or raw == "NONE":
        return "unknown", "UNKNOWN", HLR_MISSING_LIVE_STATUS_COMMENT, raw
    mapped = HLR_STATUS_MAP.get(raw)
    if mapped:
        return mapped[0], mapped[1], mapped[2], raw
    return "unknown", "UNKNOWN", f"HLR вернул неизвестный статус: {raw}.", raw


def hlr_api_error_comment(error_value: object) -> str:
    error = str(error_value or "").strip().upper()
    if error == "INSUFFICIENT_CREDIT":
        return "Недостаточно кредитов HLR API."
    if error == "INTERNAL_ERROR":
        return "HLR API вернул внутреннюю ошибку."
    return f"HLR API вернул ошибку: {error}"


def hlr_is_api_error(error_value: object) -> bool:
    error = str(error_value or "").strip().upper()
    return bool(error and error != "NONE")


def hlr_prepare_numbers(input_text: str) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    seen: set[str] = set()
    prepared: list[dict[str, str]] = []
    invalid: list[dict[str, object]] = []
    for raw_line in (input_text or "").splitlines():
        raw = raw_line.strip()
        if not raw:
            continue
        normalized, valid = hlr_normalize(raw)
        dedupe_key = normalized if valid else f"bad:{raw}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if valid:
            prepared.append({"original": raw, "normalized": normalized})
        else:
            invalid.append(hlr_bad_result(raw, normalized, hlr_config()["mode"] == "demo"))
    if len(prepared) + len(invalid) > HLR_MAX_NUMBERS:
        raise BusinessRuleError("За одну проверку можно отправить максимум 500 уникальных номеров")
    if not prepared and not invalid:
        raise BusinessRuleError("Нет номеров для HLR-проверки")
    return prepared, invalid


def hlr_demo_check(numbers: list[dict[str, str]]) -> list[dict[str, object]]:
    rows = []
    for item in numbers:
        normalized = item["normalized"]
        demo_extra = {
            "uuid": f"demo-{normalized[-6:]}",
            "credits_spent": "0",
            "detected_telephone_number": normalized,
            "formatted_telephone_number": normalized,
            "timestamp": "2026-07-04T00:00:00Z",
            "current_network": "Demo current network",
            "original_network": "Demo original network",
            "current_operator": "DemoNet",
            "original_operator": "DemoOriginal",
            "current_mccmnc": "250001",
            "original_mccmnc": "250099",
            "current_country": "Demo",
            "original_country": "Demo",
            "current_country_iso3": "DEM",
            "original_country_iso3": "DEM",
            "original_area": "Demo area",
            "current_country_prefix": "+0",
            "original_country_prefix": "+0",
            "is_ported": "YES" if normalized.endswith("77") else "NO",
            "ported_date": "2026-01-15" if normalized.endswith("77") else "",
            "raw_api_item_sanitized": {"uuid": f"demo-{normalized[-6:]}", "live_status": "DEMO", "current_network_details": {"name": "DemoNet", "mccmnc": "250001"}},
            "extra_fields": ["uuid", "credits_spent", "detected_telephone_number", "formatted_telephone_number", "live_status", "telephone_number_type", "current_network_details.name", "current_network_details.mccmnc", "original_network_details.mccmnc", "is_ported", "ported_date"],
        }
        if normalized.endswith("00"):
            rows.append(hlr_make_result(item["original"], normalized, country="Demo", number_type_raw="MOBILE", operator="DemoNet", hlr_status_raw="ERROR", live_status_raw="ERROR", final_category="error", final_result="ERROR", comment=HLR_ERROR_COMMENT, is_demo_result=True, **demo_extra))
            continue
        if normalized.endswith("11"):
            status, ntype = "DEAD", "MOBILE"
        elif normalized.endswith("22"):
            status, ntype = "INCONCLUSIVE", "MOBILE"
        elif normalized.endswith("33"):
            status, ntype = "NOT_APPLICABLE", "LANDLINE"
        elif normalized.endswith("44"):
            status, ntype = "ABSENT_SUBSCRIBER", "MOBILE"
        elif normalized.endswith("55"):
            status, ntype = "NO_TELESERVICE_PROVISIONED", "MOBILE"
        elif normalized.endswith("66"):
            status, ntype = "LIVE", "MACHINE_TO_MACHINE"
        elif normalized.endswith("77"):
            status, ntype = "LIVE", "STAGE_AND_SCREEN"
        else:
            status, ntype = "LIVE", "MOBILE"
        category, final, comment, raw = hlr_status_result(status, status)
        rows.append(hlr_make_result(item["original"], normalized, country="Demo", number_type_raw=ntype, operator="DemoNet", hlr_status_raw=raw, live_status_raw=raw, final_category=category, final_result=final, comment=comment, is_demo_result=True, **demo_extra))
    return rows


def hlr_extract_api_rows(payload: object) -> list[object] | None:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for path in (("results",), ("body", "results"), ("data", "results"), ("result",)):
        value = payload
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
    return None


def hlr_network_value(raw: dict[str, object], field: str) -> object:
    current = raw.get("current_network_details")
    original = raw.get("original_network_details")
    if isinstance(current, dict) and current.get(field):
        return current.get(field)
    if isinstance(original, dict) and original.get(field):
        return original.get(field)
    return "—"


def hlr_result_from_api_item(item: dict[str, str], raw_result: object) -> dict[str, object]:
    if not isinstance(raw_result, dict):
        return hlr_make_result(item["original"], item["normalized"], hlr_status_raw="ERROR", live_status_raw="ERROR", final_category="error", final_result="ERROR", comment=HLR_UNEXPECTED_COMMENT)
    sanitized = hlr_sanitize_api_item(raw_result)
    current_operator = hlr_first_present(raw_result, "current_network_details.name", "current_operator")
    original_operator = hlr_first_present(raw_result, "original_network_details.name", "original_operator")
    current_country = hlr_first_present(raw_result, "current_network_details.country_name", "current_country")
    original_country = hlr_first_present(raw_result, "original_network_details.country_name", "original_country")
    current_network = hlr_first_present(raw_result, "current_network")
    original_network = hlr_first_present(raw_result, "original_network")
    extras = {
        "uuid": hlr_first_present(raw_result, "uuid", "request_id", "lookup_id"),
        "credits_spent": hlr_first_present(raw_result, "credits_spent", "cost", "price"),
        "detected_telephone_number": hlr_first_present(raw_result, "detected_telephone_number", "telephone_number", "number", "msisdn"),
        "formatted_telephone_number": hlr_first_present(raw_result, "formatted_telephone_number"),
        "timestamp": hlr_first_present(raw_result, "timestamp"),
        "telephone_number": hlr_first_present(raw_result, "telephone_number"),
        "current_network": current_network,
        "original_network": original_network,
        "current_operator": current_operator or "—",
        "original_operator": original_operator or "—",
        "current_mccmnc": hlr_first_present(raw_result, "current_network_details.mccmnc", "current_mccmnc"),
        "original_mccmnc": hlr_first_present(raw_result, "original_network_details.mccmnc", "original_mccmnc"),
        "current_country": current_country or "—",
        "original_country": original_country or "—",
        "current_country_iso3": hlr_first_present(raw_result, "current_network_details.country_iso3", "current_country_iso3"),
        "original_country_iso3": hlr_first_present(raw_result, "original_network_details.country_iso3", "original_country_iso3"),
        "original_area": hlr_first_present(raw_result, "original_network_details.area", "original_area"),
        "current_country_prefix": hlr_first_present(raw_result, "current_network_details.country_prefix", "current_country_prefix"),
        "original_country_prefix": hlr_first_present(raw_result, "original_network_details.country_prefix", "original_country_prefix"),
        "current_mcc": hlr_first_present(raw_result, "current_network_details.mcc"),
        "current_mnc": hlr_first_present(raw_result, "current_network_details.mnc"),
        "original_mcc": hlr_first_present(raw_result, "original_network_details.mcc"),
        "original_mnc": hlr_first_present(raw_result, "original_network_details.mnc"),
        "is_ported": hlr_first_present(raw_result, "is_ported", "ported", "mnp"),
        "ported_date": hlr_first_present(raw_result, "ported_date"),
        "landline_status": hlr_first_present(raw_result, "landline_status"),
        "usa_status": hlr_first_present(raw_result, "usa_status"),
        "line_status": hlr_first_present(raw_result, "line_status"),
        "request_parameters": hlr_first_present(raw_result, "request_parameters"),
        "raw_error": hlr_first_present(raw_result, "error", "error_message"),
        "raw_message": hlr_first_present(raw_result, "message"),
        "extra_fields": sorted(set(hlr_field_paths(sanitized))),
        "raw_api_item_sanitized": sanitized,
    }
    error_value = raw_result.get("error") or raw_result.get("error_message")
    status_value = raw_result.get("status") or raw_result.get("message")
    country = current_country or original_country or "—"
    operator = current_operator or original_operator or current_network or original_network or "—"
    number_type_raw = raw_result.get("telephone_number_type") or raw_result.get("number_type") or ""
    if hlr_number_type(number_type_raw) == "bad_format":
        return hlr_make_result(item["original"], item["normalized"], country=country, number_type_raw=number_type_raw, operator=operator, hlr_status_raw="BAD_FORMAT", live_status_raw="BAD_FORMAT", final_category="bad", final_result="BAD_FORMAT", comment=HLR_BAD_FORMAT_COMMENT, **extras)
    if hlr_is_api_error(error_value):
        return hlr_make_result(item["original"], item["normalized"], country=country, number_type_raw=number_type_raw, operator=operator, hlr_status_raw=status_value or "ERROR", live_status_raw="ERROR", final_category="error", final_result="ERROR", comment=hlr_api_error_comment(error_value), **extras)
    live_status = raw_result.get("live_status")
    hlr_status = raw_result.get("hlr_status") or live_status
    category, final, comment, raw_status = hlr_status_result(live_status, hlr_status)
    return hlr_make_result(item["original"], item["normalized"], country=country, number_type_raw=number_type_raw, operator=operator, hlr_status_raw=hlr_status or raw_status, live_status_raw=live_status or "", final_category=category, final_result=final, comment=comment, **extras)



def hlr_user_error_comment(error_type: str, http_status: object = "", api_message: object = "") -> str:
    if str(http_status) in {"401", "403"}:
        return "HLR API authorization failed. Check API key/secret."
    if str(http_status) == "400":
        message = str(api_message or "").strip()
        return f"HLR API rejected request format: {message}" if message else "HLR API rejected request format."
    if str(http_status) == "429":
        return "HLR API rate limit."
    if error_type == "timeout":
        return "HLR API timeout."
    if error_type == "connection_error":
        return "HLR API connection error."
    if error_type == "missing_config":
        return "HLR API config is incomplete."
    return "HLR API returned unexpected response format."


def hlr_sanitize_text(value: str, config: dict[str, object]) -> str:
    sanitized = value or ""
    for secret in (config.get("api_key"), config.get("api_secret")):
        secret_text = str(secret or "")
        if secret_text:
            sanitized = sanitized.replace(secret_text, "***")
    return sanitized[:1000]


def hlr_request_payload(numbers: list[dict[str, str]], config: dict[str, object]) -> dict[str, object]:
    return {
        "api_key": str(config.get("api_key") or ""),
        "api_secret": str(config.get("api_secret") or ""),
        "requests": [{"telephone_number": item["normalized"], "output_format": "PLUS_E164"} for item in numbers],
    }


def hlr_sanitize_request_shape_item(value: object) -> object:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in HLR_SENSITIVE_KEY_PARTS):
                sanitized[key_text] = "***"
            elif key_text == "telephone_number":
                sanitized[key_text] = "+..." if item else ""
            else:
                sanitized[key_text] = hlr_sanitize_request_shape_item(item)
        return sanitized
    if isinstance(value, list):
        return [hlr_sanitize_request_shape_item(item) for item in value]
    return value


def hlr_sanitized_request_shape(payload: dict[str, object]) -> str:
    return json.dumps(hlr_sanitize_request_shape_item(payload), ensure_ascii=False, sort_keys=True)


def hlr_sanitized_api_message(response_text: str, config: dict[str, object]) -> str:
    sanitized_text = hlr_sanitize_text(response_text, config)
    try:
        payload = json.loads(sanitized_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return sanitized_text[:300]
    if isinstance(payload, dict):
        for key in ("message", "error", "error_message", "detail", "description"):
            value = payload.get(key)
            if value:
                return hlr_sanitize_text(str(value), config)[:300]
    return sanitized_text[:300]


def hlr_error_details(config: dict[str, object], error_type: str, *, http_method: object = "", api_url: object = "", request_shape_sanitized: object = "", http_status: object = "", response_content_type: object = "", response_preview: str = "", parsed_container_detected: object = "", api_message: object = "") -> dict[str, object]:
    return {
        "error_type": error_type,
        "http_method": str(http_method or ""),
        "api_url": hlr_safe_api_url(api_url or config.get("api_url") or ""),
        "request_shape_sanitized": str(request_shape_sanitized or ""),
        "http_status": str(http_status or ""),
        "response_content_type": str(response_content_type or ""),
        "response_preview_sanitized": hlr_sanitize_text(response_preview, config),
        "api_message_sanitized": hlr_sanitize_text(str(api_message or ""), config),
        "parsed_container_detected": str(parsed_container_detected or ""),
        "api_url_present": "yes" if config.get("api_url") else "no",
        "api_key_present": "yes" if config.get("api_key") else "no",
        "api_secret_present": "yes" if config.get("api_secret") else "no",
    }


def hlr_error_result(item: dict[str, str], config: dict[str, object], error_type: str, **details: object) -> dict[str, object]:
    http_status = details.get("http_status", "")
    api_message = details.get("api_message", "")
    return hlr_make_result(
        item["original"],
        item["normalized"],
        hlr_status_raw="ERROR",
        live_status_raw="ERROR",
        final_category="error",
        final_result="ERROR",
        comment=hlr_user_error_comment(error_type, http_status, api_message),
        **hlr_error_details(config, error_type, **details),
    )

def hlr_real_api_check(numbers: list[dict[str, str]], config: dict[str, object]) -> list[dict[str, object]]:
    if hlr_config_incomplete(config):
        return [hlr_error_result(item, config, "missing_config") for item in numbers]
    timeout = max(1, int(config["timeout_ms"]) / 1000)
    results = []
    for start in range(0, len(numbers), HLR_BATCH_SIZE):
        batch = numbers[start:start + HLR_BATCH_SIZE]
        request_body = hlr_request_payload(batch, config)
        request_shape = hlr_sanitized_request_shape(request_body)
        payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        diagnostics = {"http_method": "POST", "api_url": config["api_url"], "request_shape_sanitized": request_shape}
        req = Request(str(config["api_url"]), data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=timeout) as resp:
                response_bytes = resp.read()
                response_text = response_bytes.decode("utf-8", errors="replace")
                content_type = resp.headers.get("Content-Type", "")
                diagnostics["http_status"] = str(getattr(resp, "status", "") or getattr(resp, "code", "") or "200")
                diagnostics["response_content_type"] = content_type
            body = json.loads(response_text)
            api_rows = hlr_extract_api_rows(body)
            if api_rows is None:
                results.extend([hlr_error_result(item, config, "unexpected_response", response_preview=response_text, parsed_container_detected="no", **diagnostics) for item in batch])
                continue
            by_number = {str(r.get("detected_telephone_number") or r.get("telephone_number") or r.get("formatted_telephone_number") or r.get("number") or r.get("normalized") or r.get("msisdn") or ""): r for r in api_rows if isinstance(r, dict)}
            for item, fallback in zip(batch, api_rows):
                raw_result = by_number.get(item["normalized"]) or by_number.get(item["normalized"].lstrip("+")) or fallback
                results.append(hlr_result_from_api_item(item, raw_result))
        except HTTPError as exc:
            response_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
            api_message = hlr_sanitized_api_message(response_text, config)
            results.extend([hlr_error_result(item, config, "http_error", http_status=exc.code, response_content_type=content_type, response_preview=response_text, parsed_container_detected="unknown", api_message=api_message, **diagnostics) for item in batch])
        except TimeoutError:
            results.extend([hlr_error_result(item, config, "timeout", **diagnostics) for item in batch])
        except URLError as exc:
            reason = getattr(exc, "reason", "")
            error_type = "timeout" if "timed out" in str(reason).lower() else "connection_error"
            results.extend([hlr_error_result(item, config, error_type, response_preview=str(reason), **diagnostics) for item in batch])
        except (ValueError, json.JSONDecodeError) as exc:
            results.extend([hlr_error_result(item, config, "unexpected_response", response_preview=str(exc), parsed_container_detected="no", **diagnostics) for item in batch])
    return results


def hlr_usage_checked_count(rows: list[dict[str, object]]) -> int:
    return sum(1 for row in rows if not row.get("error_type"))


def hlr_run_check(input_text: str) -> tuple[list[dict[str, object]], dict[str, int]]:
    config = hlr_config()
    prepared, invalid = hlr_prepare_numbers(input_text)
    usage = hlr_usage_with_limits()
    daily_limit = int(usage.get("daily_limit") or 0)
    remaining_today = usage.get("remaining_today")
    if daily_limit and remaining_today is not None and len(prepared) > int(remaining_today):
        if int(remaining_today) <= 0:
            raise BusinessRuleError("Дневной лимит HLR исчерпан.")
        raise BusinessRuleError(f"Нельзя запустить проверку: осталось {int(remaining_today)} номеров из дневного лимита, а выбрано {len(prepared)}.")
    checked = hlr_demo_check(prepared) if config["mode"] in {"demo", ""} else hlr_real_api_check(prepared, config)
    usage_checked_count = hlr_usage_checked_count(checked)
    if usage_checked_count:
        hlr_record_daily_usage(usage_checked_count, hlr_sum_credits(checked))
    results = invalid + checked
    return results, hlr_summary(results)


def hlr_summary(results: list[dict[str, object]]) -> dict[str, int]:
    return {
        "ALL": len(results),
        "OK": sum(1 for r in results if r.get("final_result") == "OK" or r.get("final_category") == "ok"),
        "LIVE": sum(1 for r in results if r.get("live_status_raw") == "LIVE" or r.get("hlr_status_raw") == "LIVE"),
        "DEAD": sum(1 for r in results if r.get("final_result") == "DEAD" or r.get("live_status_raw") == "DEAD" or r.get("hlr_status_raw") == "DEAD"),
        "BAD_FORMAT": sum(1 for r in results if r.get("final_result") == "BAD_FORMAT"),
        "WARNING": sum(1 for r in results if r.get("final_category") == "warning"),
        "UNKNOWN": sum(1 for r in results if r.get("final_category") == "unknown" or r.get("final_result") == "UNKNOWN"),
        "MOBILE": sum(1 for r in results if r.get("number_type") == "mobile" or r.get("number_type_raw") == "MOBILE"),
        "FIXED_LINE": sum(1 for r in results if r.get("number_type") == "landline" or r.get("number_type_raw") in {"LANDLINE", "FIXED_LINE"}),
        # Backward-compatible aliases used by server-side tests and exports.
        "all": len(results),
        "ok": sum(1 for r in results if r.get("final_result") == "OK" or r.get("final_category") == "ok"),
        "warning": sum(1 for r in results if r.get("final_category") == "warning"),
        "bad_format": sum(1 for r in results if r.get("final_result") == "BAD_FORMAT"),
        "dead": sum(1 for r in results if r.get("final_result") == "DEAD" or r.get("live_status_raw") == "DEAD" or r.get("hlr_status_raw") == "DEAD"),
        "mobile": sum(1 for r in results if r.get("number_type") == "mobile" or r.get("number_type_raw") == "MOBILE"),
        "fixed": sum(1 for r in results if r.get("number_type") == "landline" or r.get("number_type_raw") in {"LANDLINE", "FIXED_LINE"}),
        "unknown": sum(1 for r in results if r.get("final_category") == "unknown" or r.get("final_result") == "UNKNOWN"),
        "errors": sum(1 for r in results if r.get("final_category") == "error"),
    }


def hlr_display_value(value: object) -> str:
    if hlr_is_blank(value):
        return "—"
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    return str(value)


def hlr_status_severity(row: dict[str, object]) -> str:
    category = str(row.get("final_category") or "").lower()
    result = str(row.get("final_result") or "").upper()
    live = str(row.get("live_status_raw") or row.get("hlr_status_raw") or "").upper()
    number_type = str(row.get("number_type") or "").lower()
    api_error_markers = {"API ERROR", "ERROR", "TIMEOUT", "CONNECTION ERROR", "PARSER ERROR", "INSUFFICIENT_CREDIT", "INTERNAL_ERROR"}
    if category == "error" or result in api_error_markers or live in api_error_markers:
        return "api_error"
    if result in {"DEAD", "BAD_FORMAT", "INVALID_FORMAT"} or live in {"DEAD", "BAD_FORMAT", "INVALID_FORMAT"}:
        return "red"
    if number_type in {"bad_format", "landline", "toll_free", "premium", "shared_cost", "stage_and_screen", "pager", "voicemail_only", "machine_to_machine"}:
        return "red"
    if result == "OK" and live == "LIVE":
        return "green"
    if live == "LIVE" or category == "ok":
        return "green"
    if live in {"ABSENT_SUBSCRIBER", "NO_TELESERVICE_PROVISIONED", "INCONCLUSIVE", "NOT_AVAILABLE_NETWORK_ONLY", "NO_COVERAGE", "NETWORK_INFO_ONLY", "WARNING"} or result in {"WARNING", "INCONCLUSIVE"} or number_type in {"mobile_or_landline", "voip"}:
        return "yellow"
    if live in {"NOT_APPLICABLE", "UNKNOWN"} or number_type == "unknown":
        return "neutral"
    if category == "warning":
        return "yellow"
    if category == "unknown" or result == "UNKNOWN":
        return "neutral"
    return "neutral"


def hlr_display_status(row: dict[str, object]) -> str:
    number_type = hlr_filter_attr(row.get("number_type_raw") or row.get("telephone_number_type") or row.get("number_type"), "")
    if number_type == "BAD_FORMAT" or str(row.get("number_type") or "").strip().lower() == "bad_format":
        return "BAD_FORMAT"
    live = hlr_filter_attr(row.get("live_status_raw") or row.get("live_status") or row.get("hlr_status_raw") or row.get("hlr_status"), "")
    if live in {"LIVE", "DEAD", "ABSENT_SUBSCRIBER", "NO_TELESERVICE_PROVISIONED", "NOT_AVAILABLE_NETWORK_ONLY", "NO_COVERAGE", "NOT_APPLICABLE", "INCONCLUSIVE"}:
        return live
    return "—"


def hlr_display_row(row: dict[str, object]) -> dict[str, str]:
    current_operator = row.get("current_operator")
    original_operator = row.get("original_operator")
    current_country = row.get("current_country")
    original_country = row.get("original_country")
    return {
        "original_number": str(row.get("original_number") or row.get("original") or ""),
        "normalized_number": str(row.get("normalized_number") or row.get("normalized") or ""),
        "detected_telephone_number": hlr_display_value(row.get("detected_telephone_number")),
        "formatted_telephone_number": hlr_display_value(row.get("formatted_telephone_number")),
        "format_status": hlr_format_label(str(row.get("format_status") or row.get("format") or "valid")),
        "country": hlr_display_value(current_country or original_country or row.get("country")),
        "number_type": hlr_type_label(str(row.get("number_type") or "unknown")),
        "number_type_raw": hlr_display_value(row.get("number_type_raw")),
        "operator": hlr_display_value(current_operator or original_operator or row.get("operator") or row.get("network")),
        "hlr_status_raw": hlr_display_status(row),
        "live_status_raw": str(row.get("live_status_raw") or row.get("live_status") or "—"),
        "final_result": str(row.get("final_result") or row.get("outcome") or "UNKNOWN"),
        "lead_quality_signal": hlr_lead_quality_label(row.get("lead_quality_signal")),
        "lead_quality_reason": hlr_display_value(row.get("lead_quality_reason") or hlr_lead_quality_reason(row.get("live_status_raw") or row.get("hlr_status_raw"), str(row.get("number_type") or "unknown"), str(row.get("final_category") or "unknown"), row.get("final_result"))),
        "comment": str(row.get("comment") or ""),
        "current_network": hlr_display_value(row.get("current_network")),
        "original_network": hlr_display_value(row.get("original_network")),
        "current_operator": hlr_display_value(current_operator),
        "original_operator": hlr_display_value(original_operator),
        "current_mccmnc": hlr_display_value(row.get("current_mccmnc") or row.get("current_mcc")),
        "original_mccmnc": hlr_display_value(row.get("original_mccmnc") or row.get("original_mcc")),
        "current_country": hlr_display_value(current_country),
        "original_country": hlr_display_value(original_country),
        "current_country_iso3": hlr_display_value(row.get("current_country_iso3")),
        "original_country_iso3": hlr_display_value(row.get("original_country_iso3")),
        "original_area": hlr_display_value(row.get("original_area")),
        "current_country_prefix": hlr_display_value(row.get("current_country_prefix")),
        "original_country_prefix": hlr_display_value(row.get("original_country_prefix")),
        "is_ported": hlr_display_value(row.get("is_ported") or row.get("ported")),
        "ported_date": hlr_display_value(row.get("ported_date")),
        "landline_status": hlr_display_value(row.get("landline_status")),
        "uuid": hlr_display_value(row.get("uuid") or row.get("request_id") or row.get("lookup_id")),
        "timestamp": hlr_display_value(row.get("timestamp")),
        "credits_spent": hlr_display_value(row.get("credits_spent")),
        "raw_error": hlr_display_value(row.get("raw_error")),
        "raw_message": hlr_display_value(row.get("raw_message")),
        "error_type": hlr_display_value(row.get("error_type")),
        "http_status": hlr_display_value(row.get("http_status")),
        "response_content_type": hlr_display_value(row.get("response_content_type")),
        "response_preview_sanitized": hlr_display_value(row.get("response_preview_sanitized")),
        "api_message_sanitized": hlr_display_value(row.get("api_message_sanitized")),
        "http_method": hlr_display_value(row.get("http_method")),
        "api_url": hlr_display_value(row.get("api_url")),
        "request_shape_sanitized": hlr_display_value(row.get("request_shape_sanitized")),
        "parsed_container_detected": hlr_display_value(row.get("parsed_container_detected")),
        "api_url_present": hlr_display_value(row.get("api_url_present")),
        "api_key_present": hlr_display_value(row.get("api_key_present")),
        "api_secret_present": hlr_display_value(row.get("api_secret_present")),
    }

def hlr_has_value(results: list[dict[str, object]], *keys: str) -> bool:
    return any(any(not hlr_is_blank(row.get(key)) for key in keys) for row in results)


def hlr_csv_headers_and_keys(results: list[dict[str, object]]) -> tuple[list[str], list[str]]:
    keys = [key for key, _label, _width in HLR_TABLE_COLUMNS]
    headers = [label for _key, label, _width in HLR_TABLE_COLUMNS]
    return headers, keys



def hlr_filter_results_for_export(results: list[dict[str, object]], selected_statuses: list[object] | None, show_all_statuses: bool) -> list[dict[str, object]]:
    safe_results = [row for row in results if isinstance(row, dict)]
    if show_all_statuses:
        return safe_results
    statuses = {hlr_filter_attr(status, "") for status in (selected_statuses or []) if hlr_filter_attr(status, "")}
    if not statuses:
        return []
    return [row for row in safe_results if hlr_filter_attr(hlr_display_status(row), "") in statuses]

def hlr_results_rows(results: list[dict[str, object]]) -> list[list[str]]:
    _, keys = hlr_csv_headers_and_keys(results)
    return [[hlr_display_row(row).get(key, "") for key in keys] for row in results]


def hlr_details_html(row: dict[str, object]) -> str:
    display = hlr_display_row(row)
    main_keys = ["original_number", "normalized_number", "detected_telephone_number", "formatted_telephone_number", "format_status", "country", "number_type", "number_type_raw", "operator", "hlr_status_raw", "live_status_raw", "final_result", "lead_quality_signal", "lead_quality_reason", "comment"]
    network_keys = ["current_network", "current_operator", "current_mccmnc", "current_country", "current_country_iso3", "current_country_prefix"]
    original_keys = ["original_network", "original_operator", "original_mccmnc", "original_country", "original_country_iso3", "original_area", "original_country_prefix"]
    porting_keys = ["is_ported", "ported_date", "landline_status"]
    meta_keys = ["uuid", "timestamp", "credits_spent", "raw_error", "raw_message"]
    error_keys = ["http_method", "api_url", "request_shape_sanitized", "http_status", "response_content_type", "response_preview_sanitized", "api_message_sanitized", "error_type", "parsed_container_detected", "api_url_present", "api_key_present", "api_secret_present"]
    labels = {
        "original_number": "Исходный номер", "normalized_number": "Нормализованный номер", "detected_telephone_number": "Detected number", "formatted_telephone_number": "Formatted number", "format_status": "Формат", "country": "Страна", "operator": "Оператор / сеть", "hlr_status_raw": "HLR статус", "live_status_raw": "Live status", "number_type": "Telephone number type", "number_type_raw": "Raw telephone_number_type", "final_result": "Итог", "lead_quality_signal": "Оценка лида", "lead_quality_reason": "Почему такая оценка", "comment": "Комментарий", "current_network": "Current network", "current_operator": "Current operator", "current_mccmnc": "Current MCCMNC", "current_country": "Current country", "current_country_iso3": "Current ISO3", "current_country_prefix": "Current country prefix", "original_network": "Original network", "original_operator": "Original operator", "original_mccmnc": "Original MCCMNC", "original_country": "Original country", "original_country_iso3": "Original ISO3", "original_area": "Original area", "original_country_prefix": "Original country prefix", "is_ported": "Is ported", "ported_date": "Ported date", "landline_status": "Landline status", "uuid": "UUID", "timestamp": "Timestamp", "credits_spent": "Credits spent", "raw_error": "Raw error", "raw_message": "Raw message", "error_type": "Error type", "http_status": "HTTP status", "response_content_type": "Response content type", "response_preview_sanitized": "Response preview (sanitized)", "api_message_sanitized": "API message (sanitized)", "http_method": "HTTP method", "api_url": "API URL", "request_shape_sanitized": "Request shape (sanitized)", "parsed_container_detected": "Parsed container detected", "api_url_present": "API URL present", "api_key_present": "API key present", "api_secret_present": "API secret present"
    }
    def dl(keys: list[str], skip_empty: bool = False) -> str:
        parts = []
        for key in keys:
            value = display.get(key, "—")
            if skip_empty and value == "—":
                continue
            title = f" title='{esc(value)}'" if key in {"lead_quality_signal", "lead_quality_reason", "comment"} else ""
            parts.append(f"<dt{title}>{esc(labels.get(key, key))}</dt><dd{title}>{esc(value)}</dd>")
        return "<dl class='hlr-detail-list'>" + "".join(parts) + "</dl>"
    raw_json = json.dumps(row.get("raw_api_item_sanitized") or {}, ensure_ascii=False, indent=2, sort_keys=True)
    raw_block = f"<div><pre class='hlr-raw-json'>{esc(raw_json)}</pre></div>" if raw_json != "{}" else "<p class='muted'>Raw API item недоступен для этой строки.</p>"
    error_section = f"<section><h4>Safe error diagnostics</h4>{dl(error_keys, True)}</section>" if any(display.get(key, "—") != "—" for key in error_keys) else ""
    return f"<div class='hlr-details-grid'><section><h4>Main</h4>{dl(main_keys)}</section><section><h4>Network</h4>{dl(network_keys, True)}</section><section><h4>Original network</h4>{dl(original_keys, True)}</section><section><h4>Porting</h4>{dl(porting_keys, True)}</section><section><h4>Meta</h4>{dl(meta_keys, True)}</section>{error_section}<section><h4>Sanitized raw API item</h4>{raw_block}</section></div>"


HLR_TABLE_COLUMNS = [
    ("original_number", "Исходный номер", 150),
    ("normalized_number", "Нормализованный номер", 170),
    ("detected_telephone_number", "Detected number", 170),
    ("formatted_telephone_number", "Formatted number", 180),
    ("format_status", "Формат", 120),
    ("country", "Страна", 140),
    ("number_type", "Тип номера", 150),
    ("number_type_raw", "Raw telephone_number_type", 200),
    ("operator", "Оператор / сеть", 220),
    ("hlr_status_raw", "HLR статус", 150),
    ("live_status_raw", "Live status", 150),
    ("final_result", "Итог", 140),
    ("lead_quality_signal", "Оценка лида", 170),
    ("comment", "Комментарий", 360),
    ("current_network", "Current network", 220),
    ("current_operator", "Current operator", 220),
    ("current_mccmnc", "Current MCCMNC", 150),
    ("current_country", "Current country", 160),
    ("current_country_iso3", "Current ISO3", 120),
    ("current_country_prefix", "Current country prefix", 180),
    ("original_network", "Original network", 220),
    ("original_operator", "Original operator", 220),
    ("original_mccmnc", "Original MCCMNC", 160),
    ("original_country", "Original country", 160),
    ("original_country_iso3", "Original ISO3", 120),
    ("original_area", "Original area", 160),
    ("original_country_prefix", "Original country prefix", 190),
    ("is_ported", "Is ported", 110),
    ("uuid", "UUID", 280),
    ("timestamp", "Timestamp", 190),
    ("credits_spent", "Credits", 140),
    ("raw_error", "Raw error", 220),
    ("raw_message", "API message", 280),
    ("request_shape_sanitized", "Request parameters", 320),
]


def hlr_table_cell(display: dict[str, str], key: str, severity: str) -> str:
    value = display.get(key) or "—"
    long_class = " hlr-long-text" if key in {"comment", "raw_error", "raw_message", "request_shape_sanitized"} else ""
    title = f" title='{esc(value)}'" if long_class or key in {"lead_quality_signal"} else ""
    status_keys = {"format_status", "hlr_status_raw", "live_status_raw", "final_result", "lead_quality_signal"}
    badge_class = f" status-badge hlr-severity-{esc(severity)}" if key in status_keys and value != "—" else ""
    return f"<td data-col='{esc(key)}'{title}><span class='hlr-cell-text{long_class}{badge_class}'>{esc(value)}</span></td>"


def hlr_filter_attr(value: object, default: str = "UNKNOWN") -> str:
    text = str(value or "").strip().upper().replace(" ", "_").replace("-", "_")
    return text or default


def hlr_number_type_filter_attr(row: dict[str, object]) -> str:
    normalized_type = str(row.get("number_type") or "").strip().lower()
    mapping = {
        "mobile": "MOBILE",
        "landline": "FIXED_LINE",
        "mobile_or_landline": "MOBILE_OR_LANDLINE",
        "voip": "VOIP",
        "bad_format": "BAD_FORMAT",
        "unknown": "UNKNOWN",
    }
    if normalized_type in mapping:
        return mapping[normalized_type]
    return hlr_filter_attr(row.get("number_type_raw") or normalized_type)


def hlr_row_filter_attrs(row: dict[str, object], severity: str) -> str:
    format_status = str(row.get("format_status") or row.get("format") or "valid").strip().lower() or "unknown"
    filter_severity = {"green": "good", "red": "bad", "yellow": "warning", "orange": "warning"}.get(str(severity).lower(), str(severity or "unknown").lower())
    attrs = {
        "hlr-status": hlr_filter_attr(hlr_display_status(row)),
        "live-status": hlr_filter_attr(row.get("live_status_raw") or row.get("live_status")),
        "final-result": hlr_filter_attr(row.get("final_result")),
        "number-type": hlr_number_type_filter_attr(row),
        "format-status": format_status,
        "severity": filter_severity.strip() or "unknown",
    }
    return " ".join(f"data-{name}='{esc(value)}'" for name, value in attrs.items())


def hlr_table(results: list[dict[str, object]]) -> str:
    colgroup = "".join(f"<col data-col='{esc(key)}' style='width:{width}px'>" for key, _label, width in HLR_TABLE_COLUMNS)
    header_cells = []
    for key, label, _width in HLR_TABLE_COLUMNS:
        header_label = esc(label)
        if key == "original_number":
            header_label = (
                "<span class='copyable-header'>"
                f"{header_label} "
                "<button class='copy-column-button' type='button' id='hlr-copy-source-button' "
                "data-hlr-copy-source='1' title='Скопировать исходные номера' "
                "aria-label='Скопировать исходные номера'>"
                f"{nav_icon('copy')}"
                "</button>"
                "</span>"
            )
        header_cells.append(f"<th data-col='{esc(key)}'>{header_label}</th>")
    thead = "<thead><tr>" + "".join(header_cells) + "</tr></thead>"
    rows = []
    for index, row in enumerate(results):
        display = hlr_display_row(row)
        severity = esc(str(row.get("status_severity") or hlr_status_severity(row)))
        cells = "".join(hlr_table_cell(display, key, severity) for key, _label, _width in HLR_TABLE_COLUMNS)
        row_attrs = hlr_row_filter_attrs(row, severity)
        source_number = esc(display.get("original_number", ""))
        rows.append(f"<tr class='hlr-result-row hlr-row-severity-{severity}' data-result-index='{index}' data-source-number='{source_number}' {row_attrs}>{cells}</tr>")
    tbody = "<tbody>" + "".join(rows) + "</tbody>"
    empty_hidden = " hidden" if rows else ""
    empty_text = "Выберите один или несколько HLR-статусов для отображения результатов." if rows else "Запустите проверку, чтобы увидеть результаты."
    content = f"<div class='table-scroll'><table id='hlr-table'><colgroup>{colgroup}</colgroup>{thead}{tbody}</table></div><div class='empty-state hlr-table-empty-message' id='hlr-empty-state'{empty_hidden}><strong>{esc(empty_text)}</strong></div>"
    return "<section class='hlr-results-area'>" + table_card(content) + "</section>"


def current_repo() -> Repository | None:
    repo = _REQUEST_CONTEXT.get("repo")
    return repo if isinstance(repo, Repository) else None


def hlr_today_key() -> str:
    return date.today().isoformat()


def hlr_empty_usage() -> dict[str, object]:
    return {
        "date": hlr_today_key(),
        "usage_date": hlr_today_key(),
        "usage_source": "database",
        "checked_today": 0,
        "credits_spent_today": None,
        "last_check_count": 0,
        "last_check_credits": None,
        "updated_at": None,
    }


def hlr_daily_usage() -> dict[str, object]:
    repo = current_repo()
    if repo is None:
        return hlr_empty_usage()
    usage_date = hlr_today_key()
    row = repo.conn.execute("SELECT * FROM hlr_daily_usage WHERE usage_date = ?", (usage_date,)).fetchone()
    if row is None:
        return hlr_empty_usage()
    return {
        "date": row["usage_date"],
        "usage_date": row["usage_date"],
        "usage_source": "database",
        "checked_today": int(row["checked_count"] or 0),
        "credits_spent_today": row["credits_spent"],
        "last_check_count": int(row["last_check_count"] or 0),
        "last_check_credits": row["last_check_credits"],
        "updated_at": row["updated_at"],
    }


def hlr_sum_credits(rows: list[dict[str, object]]) -> object | None:
    total = 0.0
    found = False
    for row in rows:
        value = row.get("credits_spent")
        if value in (None, "", "—"):
            continue
        try:
            total += float(value)
            found = True
        except (TypeError, ValueError):
            continue
    if not found:
        return None
    return int(total) if total.is_integer() else round(total, 4)


def hlr_record_daily_usage(checked_count: int, credits_spent: object | None) -> dict[str, object]:
    repo = current_repo()
    if repo is None or checked_count <= 0:
        return hlr_daily_usage()
    usage_date = hlr_today_key()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    existing = repo.conn.execute("SELECT checked_count, credits_spent FROM hlr_daily_usage WHERE usage_date = ?", (usage_date,)).fetchone()
    previous_checked = int(existing["checked_count"] or 0) if existing else 0
    previous_credits = existing["credits_spent"] if existing else None
    if credits_spent is None:
        next_credits = previous_credits
    else:
        next_credits = float(previous_credits or 0) + float(credits_spent)
        if float(next_credits).is_integer():
            next_credits = int(next_credits)
    repo.conn.execute(
        """
        INSERT INTO hlr_daily_usage(usage_date, checked_count, credits_spent, last_check_count, last_check_credits, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(usage_date) DO UPDATE SET
            checked_count = excluded.checked_count,
            credits_spent = excluded.credits_spent,
            last_check_count = excluded.last_check_count,
            last_check_credits = excluded.last_check_credits,
            updated_at = excluded.updated_at
        """,
        (usage_date, previous_checked + checked_count, next_credits, checked_count, credits_spent, now),
    )
    repo.conn.commit()
    return hlr_daily_usage()


def hlr_usage_with_limits() -> dict[str, object]:
    usage = hlr_daily_usage()
    daily_limit = int(hlr_config()["daily_limit"] or 0)
    checked_today = int(usage.get("checked_today") or 0)
    remaining = max(daily_limit - checked_today, 0) if daily_limit else None
    usage.update({"daily_limit": daily_limit, "remaining_today": remaining})
    return usage


def hlr_format_metric(value: object | None, empty: str = "—") -> str:
    if value is None or value == "":
        return empty
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def hlr_balance_usage_value(balance: dict[str, object]) -> str:
    status = str(balance.get("status") or "unavailable")
    credits = balance.get("credits")
    if credits is not None:
        return hlr_format_metric(credits)
    return {"error": "ошибка", "not_configured": "не настроен", "unavailable": "недоступен"}.get(status, "недоступен")


def hlr_usage_dashboard_html(balance: dict[str, object] | None = None, usage: dict[str, object] | None = None) -> str:
    state = usage or hlr_usage_with_limits()
    balance_state = balance or hlr_balance_empty_state()
    daily_limit = int(state.get("daily_limit") or 0)
    checked_today = int(state.get("checked_today") or 0)
    remaining = state.get("remaining_today")
    remaining_class = ""
    if daily_limit and remaining is not None:
        if int(remaining) == 0:
            remaining_class = " is-danger"
        elif int(remaining) <= daily_limit * 0.2:
            remaining_class = " is-warning"
    checked_text = f"{checked_today} / {daily_limit}" if daily_limit else str(checked_today)
    last_count = int(state.get("last_check_count") or 0)
    last_text = f"{last_count} номера" if last_count == 1 else f"{last_count} номеров"
    updated = str(state.get("updated_at") or "")
    updated_meta = f"Обновлено: {esc(updated[-5:])}" if updated else "Обновлено: —"
    credits_today = hlr_format_metric(state.get("credits_spent_today"))
    last_credits = hlr_format_metric(state.get("last_check_credits"))
    return f"""
        <section class='hlr-usage-dashboard' aria-label='HLR usage'>
          <h3 class='hlr-usage-title'>HLR usage</h3>
          <div class='hlr-usage-grid'>
            <div class='hlr-usage-cell hlr-usage-balance-cell' id='hlr-usage-balance-card'><div class='hlr-usage-header'><span class='hlr-usage-label'>Баланс API</span><form method='post' action='/hlr/balance' id='hlr-balance-refresh-form'><button class='secondary hlr-balance-refresh' id='hlr-balance-refresh-button' type='submit' title='Обновить баланс' aria-label='Обновить баланс'>{material_icon('refresh')}</button></form></div><strong class='hlr-usage-value'>{esc(hlr_balance_usage_value(balance_state))}</strong><span class='hlr-usage-meta'>{esc(str(balance_state.get('status') or 'unavailable'))}</span></div>
            <div class='hlr-usage-cell{remaining_class}'><span class='hlr-usage-label'>Осталось сегодня</span><strong class='hlr-usage-value'>{esc(hlr_format_metric(remaining))}</strong><span class='hlr-usage-meta'>Лимит: {esc(hlr_format_metric(daily_limit))}</span></div>
            <div class='hlr-usage-cell'><span class='hlr-usage-label'>Проверено сегодня</span><strong class='hlr-usage-value'>{esc(checked_text)}</strong><span class='hlr-usage-meta'>Кредиты сегодня: {esc(credits_today)}</span></div>
            <div class='hlr-usage-cell'><span class='hlr-usage-label'>Последняя проверка</span><strong class='hlr-usage-value'>{esc(last_text)}</strong><span class='hlr-usage-meta'>Кредиты: {esc(last_credits)} · {esc(updated_meta)}</span></div>
          </div>
        </section>"""


def hlr_balance_config_rows(balance: dict[str, object]) -> list[tuple[str, object]]:
    credits = balance.get("credits")
    status = str(balance.get("status") or "unavailable")
    return [
        ("balance", credits if credits is not None else "unavailable"),
        ("balance_status", status),
        ("balance_updated_at", balance.get("updated_at") or "—"),
        ("balance_error", balance.get("error_message") or "—"),
    ]


def hlr_daily_limit_config_control_html() -> str:
    state = hlr_daily_limit_state()
    is_admin = current_role_key() == "admin"
    override = state.get("daily_limit_override")
    override_text = hlr_format_metric(override)
    input_value = override if override is not None else state["daily_limit_effective"]
    action_html = ""
    if is_admin:
        reset_html = ""
        if override is not None:
            reset_html = "<form method='post' action='/hlr/config/daily-limit/reset'><button class='secondary' type='submit'>Сбросить к env</button></form>"
        action_html = f"""
        <div class='hlr-daily-limit-form'>
          <form method='post' action='/hlr/config/daily-limit' class='hlr-daily-limit-form'>
            <label>Лимит <input name='daily_limit_override' type='number' min='{HLR_DAILY_LIMIT_MIN}' max='{HLR_DAILY_LIMIT_MAX}' step='1' value='{esc(str(input_value))}' required></label>
            <button type='submit'>Сохранить</button>
          </form>
          {reset_html}
        </div>
        """
    else:
        action_html = f"<p class='muted'>Текущий лимит: <strong>{esc(str(state['daily_limit_effective']))}</strong>. Редактирование доступно только админу.</p>"
    return f"""
    <section class='hlr-daily-limit-admin' aria-label='Дневной лимит HLR'>
      <p class='hlr-daily-limit-title'>Дневной лимит HLR</p>
      {action_html}
      <div class='hlr-daily-limit-meta'>
        <div><span>Источник</span><strong>{esc(str(state['daily_limit_source']))}</strong></div>
        <div><span>Значение из env</span><strong>{esc(str(state['daily_limit_env']))}</strong></div>
        <div><span>Override</span><strong>{esc(override_text)}</strong></div>
      </div>
    </section>
    """


def hlr_config_diagnostics_html(balance: dict[str, object] | None = None) -> str:
    summary = hlr_safe_config_summary()
    balance_state = balance or hlr_balance_empty_state()
    warning = ""
    if summary["mode"] == "production" and not (summary["api_url_present"] and summary["api_key_present"] and summary["api_secret_present"]):
        warning = "<p class='flash error'>HLR production mode is enabled, but API configuration is incomplete.</p>"
    usage = hlr_usage_with_limits()
    rows = [
        ("mode", summary["mode"]),
        ("api_url_present", "yes" if summary["api_url_present"] else "no"),
        ("api_key_present", "yes" if summary["api_key_present"] else "no"),
        ("api_secret_present", "yes" if summary["api_secret_present"] else "no"),
        ("api_url", summary["api_url"] or "—"),
        ("timeout_ms", summary["timeout_ms"]),
        ("concurrency", summary["concurrency"]),
        ("daily_limit", summary["daily_limit"]),
        ("daily_limit_effective", summary["daily_limit_effective"]),
        ("daily_limit_source", summary["daily_limit_source"]),
        ("daily_limit_env", summary["daily_limit_env"]),
        ("daily_limit_override", hlr_format_metric(summary["daily_limit_override"])),
        ("checked_today", usage.get("checked_today", 0)),
        ("remaining_today", hlr_format_metric(usage.get("remaining_today"))),
        ("credits_spent_today", hlr_format_metric(usage.get("credits_spent_today"))),
        ("last_check_count", usage.get("last_check_count", 0)),
        ("last_check_credits", hlr_format_metric(usage.get("last_check_credits"))),
        ("usage_date", usage.get("usage_date") or usage.get("date") or "—"),
        ("usage_source", usage.get("usage_source") or "database"),
        ("dotenv_loaded", summary["dotenv_loaded"]),
        ("config_source", summary["config_source"]),
    ] + hlr_balance_config_rows(balance_state)
    body = "".join(f"<dt>{esc(label)}</dt><dd>{esc(str(value))}</dd>" for label, value in rows)
    return f"<details class='card hlr-api-fields' id='hlr-config-details'><summary>HLR config</summary>{warning}{hlr_daily_limit_config_control_html()}<dl class='hlr-detail-list' id='hlr-config-balance-fields'>{body}</dl></details>"


def hlr_api_fields_html(results: list[dict[str, object]], is_demo_mode: bool) -> str:
    if not results:
        return ""
    fields: set[str] = set()
    for row in results:
        for field in row.get("extra_fields") or []:
            if isinstance(field, str) and not any(part in field.lower() for part in HLR_SENSITIVE_KEY_PARTS):
                fields.add(field)
    if not fields:
        return ""
    note = " <span class='muted'>(demo-generated)</span>" if is_demo_mode else ""
    chips = "".join(f"<code>{esc(field)}</code>" for field in sorted(fields))
    return f"<details class='card hlr-api-fields'><summary>Поля, найденные в ответе API{note}</summary><div class='hlr-api-field-list'>{chips}</div></details>"



def hlr_help_html() -> str:
    api_fields = [
        ("Detected number", "Номер, который HLR сервис распознал после обработки входных данных."),
        ("Formatted number", "Номер после приведения к стандартному международному формату."),
        ("Phone number type", "Тип номера: мобильный, городской и другие варианты."),
        ("Current network", "Текущая сеть оператора, в которой зарегистрирован номер."),
        ("Operator", "Оператор или сеть, определённая по результату HLR проверки."),
        ("Country", "Страна номера или текущей сети, если API вернул эти данные."),
        ("HLR status", "Статус доступности номера, который вернул HLR сервис."),
    ]
    field_rows = "".join(f"<div class='hlr-help-row'><span>{esc(label)}</span><span class='hlr-help-info' title='{esc(tip)}' aria-label='{esc(tip)}'>ⓘ</span></div>" for label, tip in api_fields)
    return f"""<section class='hlr-help-card' aria-label='Справка HLR'><h3>Поля таблицы/API</h3><div class='hlr-help-list'>{field_rows}</div></section>"""

def hlr_page(input_text: str = "", results: list[dict[str, object]] | None = None, summary: dict[str, int] | None = None, error: str | None = None, balance: dict[str, object] | None = None, notice_message: str | None = None, notice_type: str = "success") -> bytes:
    results = results or []
    config = hlr_config()
    is_demo_mode = config["mode"] in {"demo", ""}
    write_allowed = can_write("hlr")
    export_allowed = can_export("hlr") and bool(results)
    usage_state = hlr_usage_with_limits()
    daily_limit = int(usage_state.get("daily_limit") or 0)
    remaining_today = int(usage_state.get("remaining_today") or 0)
    export_results_json = esc(json.dumps(results, ensure_ascii=False))
    if error:
        notice = f"<div class='flash error'>{esc(error)}</div>"
    elif notice_message:
        notice_class = "error" if notice_type == "error" else "ok"
        notice = f"<div class='flash {notice_class}'>{esc(notice_message)}</div>"
    else:
        notice = ""
    demo_badge = "<span class='badge'>Demo mode</span><span class='muted hlr-demo-note'>Результаты сгенерированы для проверки интерфейса. Реальный HLR API не вызывался.</span>" if is_demo_mode else ""
    export_form = f"<form method='post' action='/hlr/export.csv' id='hlr-export-form'><input type='hidden' name='results_json' value='{export_results_json}'><input type='hidden' name='selected_statuses_json' value='[]'><input type='hidden' name='show_all_statuses' value='1'><button type='submit' {'disabled' if not export_allowed else ''}>Экспорт CSV</button></form>"
    body = f"""
{notice}
<section class='hlr-workspace'>
  <details class='hlr-tech-spec' open>
    <summary><span class='hlr-tech-spec-title'>HLR Tech Spec {demo_badge}</span><span class='hlr-tech-spec-summary' id='hlr-tech-spec-summary'><span>Проверено: {len(results)}</span></span></summary>
    <div class='hlr-tech-spec-body'>
      <section class='form-card hlr-input-panel'>
        <form class='hlr-input-form' method='post' action='/hlr/check' id='hlr-form' data-hlr-daily-limit='{daily_limit}' data-hlr-remaining-today='{remaining_today}'>
          <label>Номера для проверки <textarea name='numbers' id='hlr-numbers-input' rows='12' {'disabled' if not write_allowed else ''}>{esc(input_text)}</textarea></label>
          <p class='hlr-input-hint hlr-usage-label'>Один номер на строке. Можно вставлять номера с пробелами, +, скобками и дефисами.</p>
          <p class='muted hlr-counter-line'>Максимум 500 номеров за одну проверку · <span id='hlr-input-counter'>0 / 500</span></p>
          <div class='hlr-input-actions'>
            <button type='submit' id='hlr-submit-button' {'disabled' if not write_allowed else ''}>Запустить проверку</button>
            <button type='button' id='hlr-clear-button' {'disabled' if not write_allowed else ''}>Очистить</button>
            <div class='hlr-progress' id='hlr-progress' role='status' aria-label='Проверка выполняется' aria-hidden='true'>
              <span class='hlr-progress-track' aria-hidden='true'><span class='hlr-progress-bar'></span></span>
            </div>
          </div>
        </form>
      </section>
      <aside class='hlr-side-panel' aria-label='HLR status and details'>
        <div class='hlr-filter-panel' id='hlr-filter-panel' aria-label='Фильтры HLR'></div>
        <div class='hlr-details-stack'>
          {hlr_usage_dashboard_html(balance)}
          <details class='card hlr-api-fields'><summary>Справка по HLR</summary>{hlr_help_html()}{hlr_api_fields_html(results, is_demo_mode)}</details>
          {hlr_config_diagnostics_html(balance)}
        </div>
      </aside>
    </div>
  </details>
  <div class='hlr-table-toolbar'><span class='muted' id='hlr-visible-count'>Показано: {len(results)} из {len(results)}</span></div>
  {hlr_table(results)}
  <div class='hlr-table-toolbar'><span class='muted' id='hlr-export-hint'>Экспортирует текущую отфильтрованную выборку.</span><div class='table-footer-tools'><div class='hlr-column-manager'><button type='button' id='hlr-columns-button' aria-expanded='false' aria-controls='hlr-column-panel'>Колонки</button><div class='hlr-column-panel' id='hlr-column-panel' aria-label='Настройки колонок'><div class='hlr-column-panel-actions'><strong>Вид таблицы</strong><button type='button' id='hlr-columns-reset'>Сбросить вид таблицы</button></div><div class='hlr-column-list' id='hlr-column-list'></div></div></div>{export_form}</div></div>
</section>
<script>
document.addEventListener("DOMContentLoaded", function () {{
  const form = document.getElementById("hlr-form");
  const input = document.getElementById("hlr-numbers-input");
  const clearButton = document.getElementById("hlr-clear-button");
  const submitButton = document.getElementById("hlr-submit-button");
  const progress = document.getElementById("hlr-progress");
  const counter = document.getElementById("hlr-input-counter");
  let hlrSubmitting = false;

  function parseHlrInputLines() {{
    if (!input) return [];
    return input.value
      .replace(/\\r/g, "")
      .split("\\n")
      .map((v) => v.trim())
      .filter(Boolean);
  }}

  function updateCounter() {{
    if (!counter) return;
    const values = parseHlrInputLines();
    counter.textContent = values.length + " / 500";
  }}

  function setHlrLoading(isLoading, count) {{
    if (submitButton) {{
      submitButton.disabled = isLoading;
      submitButton.textContent = isLoading ? "Проверяется..." : "Запустить проверку";
    }}
    if (clearButton) clearButton.disabled = isLoading;
    if (progress) {{
      progress.classList.toggle("is-active", isLoading);
      progress.setAttribute("aria-hidden", isLoading ? "false" : "true");
    }}
  }}

  if (form) {{
    form.addEventListener("submit", function (event) {{
      const lines = parseHlrInputLines();
      const dailyLimit = Number(form.dataset.hlrDailyLimit || "0");
      const remainingToday = Number(form.dataset.hlrRemainingToday || "0");
      if (hlrSubmitting) {{
        event.preventDefault();
        return;
      }}
      if (lines.length < 1 || lines.length > 500 || (dailyLimit > 0 && remainingToday < 1)) return;
      event.preventDefault();
      hlrSubmitting = true;
      setHlrLoading(true, lines.length);
      requestAnimationFrame(() => {{
        HTMLFormElement.prototype.submit.call(form);
      }});
    }});
  }}

  if (input && clearButton) {{
    clearButton.addEventListener("click", function (event) {{
      event.preventDefault();
      event.stopPropagation();
      input.value = "";
      updateCounter();
      input.focus();
    }});

    input.addEventListener("input", updateCounter);
    updateCounter();
  }}

  const table = document.getElementById("hlr-table");
  const filterPanel = document.getElementById("hlr-filter-panel");
  const visibleCount = document.getElementById("hlr-visible-count");
  const techSpecSummary = document.getElementById("hlr-tech-spec-summary");
  const resultRows = table ? Array.from(table.querySelectorAll("tbody tr.hlr-result-row")) : [];
  const exportForm = document.getElementById("hlr-export-form");
  const exportInput = exportForm ? exportForm.querySelector("input[name='results_json']") : null;
  const exportStatusesInput = exportForm ? exportForm.querySelector("input[name='selected_statuses_json']") : null;
  const exportShowAllInput = exportForm ? exportForm.querySelector("input[name='show_all_statuses']") : null;
  const exportButton = exportForm ? exportForm.querySelector("button[type='submit']") : null;
  const exportHint = document.getElementById("hlr-export-hint");
  const copySourceButton = document.getElementById("hlr-copy-source-button");
  const copySourceDefaultIcon = copySourceButton ? copySourceButton.innerHTML : "";
  const copySourceSuccessIcon = {COPY_SUCCESS_ICON_JS};
  let copySourceSuccessTimer = null;
  const emptyState = document.getElementById("hlr-empty-state");
  const originalExportJson = exportInput ? exportInput.value : "[]";
  const activeFilters = new Set();
  const selectedStatuses = activeFilters;
  let showAllStatuses = true;
  const resetEmptyMessage = "Выберите один или несколько HLR-статусов для отображения результатов.";
  const initialEmptyMessage = "Запустите проверку, чтобы увидеть результаты.";
  const statusDefinitions = [
    {{ key: "LIVE", severity: "live", tooltip: "Номер активен / доступен в сети." }},
    {{ key: "DEAD", severity: "dead", tooltip: "Номер неактивен или не обслуживается." }},
    {{ key: "BAD_FORMAT", severity: "bad_format", tooltip: "Неверный формат номера. Проверьте международный формат, код страны и длину номера." }},
    {{ key: "ABSENT_SUBSCRIBER", severity: "absent_subscriber", tooltip: "Абонент отсутствует или не зарегистрирован в сети." }},
    {{ key: "NO_TELESERVICE_PROVISIONED", severity: "no_teleservice_provisioned", tooltip: "Для номера не предоставлена нужная телеслужба." }},
    {{ key: "NOT_AVAILABLE_NETWORK_ONLY", severity: "not_available_network_only", tooltip: "Доступна только информация о сети, полноценный live-статус недоступен." }},
    {{ key: "NO_COVERAGE", severity: "no_coverage", tooltip: "Нет покрытия или сеть не вернула полноценный ответ." }},
    {{ key: "NOT_APPLICABLE", severity: "not_applicable", tooltip: "HLR-проверка неприменима к этому типу номера." }},
    {{ key: "INCONCLUSIVE", severity: "inconclusive", tooltip: "Проверка не дала однозначного результата." }},
  ];

  function statusCount(key) {{
    return resultRows.filter((row) => row.dataset.hlrStatus === key).length;
  }}

  function visibleRows() {{
    return resultRows.filter((row) => !row.hidden);
  }}

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

  async function copyText(text) {{
    if (navigator.clipboard && window.isSecureContext) {{
      await navigator.clipboard.writeText(text);
    }} else {{
      fallbackCopyText(text);
    }}
  }}

  function updateCopySourceButton(rows) {{
    if (!copySourceButton) return;
    copySourceButton.disabled = rows.length < 1;
    copySourceButton.title = rows.length > 0 ? "Скопировать исходные номера" : "Нет строк для копирования";
    copySourceButton.setAttribute("aria-label", copySourceButton.title);
  }}

  function updateExportPayload(rows) {{
    if (!exportButton) return;
    if (exportInput) exportInput.value = originalExportJson;
    if (exportStatusesInput) exportStatusesInput.value = JSON.stringify(Array.from(selectedStatuses));
    if (exportShowAllInput) exportShowAllInput.value = showAllStatuses ? "1" : "0";
    const hasRows = rows.length > 0;
    exportButton.disabled = !hasRows;
    if (exportHint) exportHint.textContent = hasRows ? "Экспортирует текущую отфильтрованную выборку." : "Нет строк для экспорта в текущей выборке.";
  }}

  function updateVisibleCount() {{
    const rows = visibleRows();
    if (visibleCount) visibleCount.textContent = "Показано: " + rows.length + " из " + resultRows.length;
    // Legacy invariant: visibleCount.textContent = "Показано: " + visible + " из " + resultRows.length;
    if (techSpecSummary) {{
      techSpecSummary.innerHTML = "<span>Проверено: " + resultRows.length + "</span><span>LIVE: " + statusCount("LIVE") + "</span><span>DEAD: " + statusCount("DEAD") + "</span>";
    }}
    if (emptyState) {{
      emptyState.hidden = rows.length > 0;
      emptyState.innerHTML = resultRows.length === 0 ? "<strong>" + initialEmptyMessage + "</strong>" : "<strong>" + resetEmptyMessage + "</strong>";
    }}
    updateExportPayload(rows);
    updateCopySourceButton(rows);
  }}

  function applyRowFilters() {{
    resultRows.forEach((row) => {{
      // Legacy invariant: row.hidden = selected.length > 0 && !selected.some
      row.hidden = showAllStatuses ? false : !selectedStatuses.has(row.dataset.hlrStatus);
    }});
    if (filterPanel) {{
      filterPanel.querySelectorAll(".hlr-filter-chip[data-filter]").forEach((chip) => {{
        const key = chip.dataset.filter;
        const active = key === "ALL" ? showAllStatuses : selectedStatuses.has(key);
        chip.classList.toggle("is-active", active);
        chip.setAttribute("aria-pressed", active ? "true" : "false");
      }});
    }}
    updateVisibleCount();
  }}

  function buildFilterPanel() {{
    if (!filterPanel) return;
    filterPanel.innerHTML = "";
    const groupEl = document.createElement("section");
    groupEl.className = "hlr-filter-group";
    groupEl.innerHTML = "<div class='hlr-filter-group-title'>HLR STATUS</div>";
    const serviceRow = document.createElement("div");
    serviceRow.className = "hlr-filter-service-row";
    const allButton = document.createElement("button");
    allButton.type = "button";
    allButton.className = "hlr-filter-chip hlr-severity-neutral";
    allButton.dataset.filter = "ALL";
    allButton.title = "Показать все результаты HLR";
    allButton.setAttribute("aria-pressed", "true");
    allButton.innerHTML = "Все <span class='hlr-filter-count'>" + resultRows.length + "</span>";
    allButton.addEventListener("click", () => {{ selectedStatuses.clear(); showAllStatuses = true; applyRowFilters(); }});
    const resetButton = document.createElement("button");
    resetButton.type = "button";
    resetButton.className = "hlr-filter-chip";
    resetButton.dataset.filter = "RESET";
    resetButton.title = "Снять все HLR-фильтры и скрыть таблицу";
    resetButton.setAttribute("aria-pressed", "false");
    resetButton.innerHTML = "Сбросить <span class='hlr-filter-count'>0</span>";
    resetButton.addEventListener("click", () => {{ selectedStatuses.clear(); showAllStatuses = false; applyRowFilters(); }});
    serviceRow.append(allButton, resetButton);
    groupEl.appendChild(serviceRow);
    const grid = document.createElement("div");
    grid.className = "hlr-filter-status-grid";
    statusDefinitions.forEach((definition) => {{
      const count = statusCount(definition.key);
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "hlr-filter-chip hlr-status-" + definition.severity + (count < 1 ? " is-empty" : "");
      chip.dataset.filter = definition.key;
      chip.title = definition.tooltip;
      chip.setAttribute("aria-label", definition.key + ": " + count + ". " + definition.tooltip);
      chip.setAttribute("aria-pressed", "false");
      if (count < 1) chip.setAttribute("aria-disabled", "true");
      chip.innerHTML = definition.key + " <span class='hlr-filter-count'>" + count + "</span>";
      chip.addEventListener("click", () => {{
        if (count < 1) return;
        showAllStatuses = false;
        if (selectedStatuses.has(definition.key)) selectedStatuses.delete(definition.key);
        else selectedStatuses.add(definition.key);
        applyRowFilters();
      }});
      grid.appendChild(chip);
    }});
    groupEl.appendChild(grid);
    filterPanel.appendChild(groupEl);
    applyRowFilters();
  }}

  buildFilterPanel();

  function replaceBalanceFragments(htmlText) {{
    const parser = new DOMParser();
    const nextDocument = parser.parseFromString(htmlText, "text/html");
    const nextBalanceCard = nextDocument.getElementById("hlr-usage-balance-card");
    const currentBalanceCard = document.getElementById("hlr-usage-balance-card");
    if (nextBalanceCard && currentBalanceCard) currentBalanceCard.replaceWith(nextBalanceCard);
    const nextConfigList = nextDocument.getElementById("hlr-config-balance-fields");
    const currentConfigList = document.getElementById("hlr-config-balance-fields");
    if (nextConfigList && currentConfigList) currentConfigList.replaceWith(nextConfigList);
  }}

  document.addEventListener("submit", async (event) => {{
    const balanceRefreshForm = event.target && event.target.closest ? event.target.closest("#hlr-balance-refresh-form") : null;
    if (!balanceRefreshForm) return;
    event.preventDefault();
    const balanceRefreshButton = balanceRefreshForm.querySelector("#hlr-balance-refresh-button");
    const configDetails = document.getElementById("hlr-config-details");
    const wasConfigOpen = configDetails ? configDetails.open : null;
    if (balanceRefreshButton) {{
      balanceRefreshButton.disabled = true;
      balanceRefreshButton.setAttribute("aria-busy", "true");
      balanceRefreshButton.title = "Обновление...";
      balanceRefreshButton.setAttribute("aria-label", "Обновление баланса");
    }}
    try {{
      const response = await fetch(balanceRefreshForm.action, {{ method: "POST", body: new FormData(balanceRefreshForm), credentials: "same-origin" }});
      const htmlText = await response.text();
      if (!response.ok) throw new Error("Balance refresh failed");
      replaceBalanceFragments(htmlText);
    }} catch (error) {{
      const currentButton = document.getElementById("hlr-balance-refresh-button");
      if (currentButton) {{
        currentButton.disabled = false;
        currentButton.removeAttribute("aria-busy");
        currentButton.title = "Обновить баланс";
        currentButton.setAttribute("aria-label", "Обновить баланс");
      }}
    }} finally {{
      const currentConfigDetails = document.getElementById("hlr-config-details");
      if (currentConfigDetails && wasConfigOpen !== null) currentConfigDetails.open = wasConfigOpen;
    }}
  }});

  if (copySourceButton) {{
    copySourceButton.addEventListener("click", async () => {{
      const rows = visibleRows();
      const values = rows.map((row) => (row.dataset.sourceNumber || "").trim()).filter(Boolean);
      if (values.length < 1) {{
        updateCopySourceButton(rows);
        return;
      }}
      try {{
        await copyText(values.join("\\n"));
      }} catch (error) {{
        try {{
          fallbackCopyText(values.join("\\n"));
        }} catch (fallbackError) {{
          updateCopySourceButton(rows);
          return;
        }}
      }}
      copySourceButton.innerHTML = copySourceSuccessIcon;
      copySourceButton.title = "Скопировано";
      copySourceButton.setAttribute("aria-label", "Скопировано");
      if (copySourceSuccessTimer) window.clearTimeout(copySourceSuccessTimer);
      copySourceSuccessTimer = window.setTimeout(() => {{
        copySourceButton.innerHTML = copySourceDefaultIcon;
        updateCopySourceButton(visibleRows());
        copySourceSuccessTimer = null;
      }}, 1500);
    }});
  }}

  if (exportForm) {{
    exportForm.addEventListener("submit", (event) => {{
      const rows = visibleRows();
      updateExportPayload(rows);
      if (rows.length < 1) {{
        event.preventDefault();
        if (exportHint) exportHint.textContent = "Нет строк для экспорта в текущей выборке.";
        return;
      }}
      window.setTimeout(() => {{
        if (exportButton && visibleRows().length > 0) exportButton.disabled = false;
      }}, 0);
    }});
  }}

  const columnsButton = document.getElementById("hlr-columns-button");
  const columnsPanel = document.getElementById("hlr-column-panel");
  const columnsList = document.getElementById("hlr-column-list");
  const resetButton = document.getElementById("hlr-columns-reset");
  const storageKey = "hlr_safe_column_settings_v2";
  const defaultVisibleColumns = new Set([
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
  ]);

  if (!table || !columnsButton || !columnsPanel || !columnsList) return;

  const columns = Array.from(table.querySelectorAll("thead th[data-col]")).map((th) => ({{
    key: th.dataset.col,
    label: th.textContent.trim(),
  }})).filter((column) => column.key);
  const businessColumnOrder = [
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
  ];
  const tableColumnOrder = columns.map((column) => column.key);
  const defaultOrder = businessColumnOrder
    .filter((key) => tableColumnOrder.includes(key))
    .concat(tableColumnOrder.filter((key) => !businessColumnOrder.includes(key)));
  let settings = loadColumnSettings();

  function loadColumnSettings() {{
    try {{
      const parsed = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (parsed && Array.isArray(parsed.order) && Array.isArray(parsed.visible)) {{
        const validKeys = new Set(defaultOrder);
        return {{
          order: parsed.order.filter((key) => validKeys.has(key)).concat(defaultOrder.filter((key) => !parsed.order.includes(key))),
          visible: parsed.visible.filter((key) => validKeys.has(key)),
        }};
      }}
    }} catch (error) {{
      console.warn("Could not load HLR column settings", error);
    }}
    return {{ order: defaultOrder.slice(), visible: defaultOrder.filter((key) => defaultVisibleColumns.has(key)) }};
  }}

  function saveColumnSettings() {{
    localStorage.setItem(storageKey, JSON.stringify(settings));
  }}

  function orderedColumns() {{
    const byKey = new Map(columns.map((column) => [column.key, column]));
    return settings.order.map((key) => byKey.get(key)).filter(Boolean);
  }}

  function moveColumnDom(parent, selector, key) {{
    if (!parent) return;
    const element = parent.querySelector(selector + "[data-col='" + CSS.escape(key) + "']");
    if (element) parent.appendChild(element);
  }}

  function applyColumnSettings() {{
    const visible = new Set(settings.visible);
    settings.order.forEach((key) => moveColumnDom(table.querySelector("colgroup"), "col", key));
    const headerRow = table.querySelector("thead tr");
    settings.order.forEach((key) => moveColumnDom(headerRow, "th", key));
    table.querySelectorAll("tbody tr").forEach((row) => {{
      settings.order.forEach((key) => moveColumnDom(row, "td", key));
    }});
    table.querySelectorAll("col[data-col], th[data-col], td[data-col]").forEach((cell) => {{
      cell.hidden = !visible.has(cell.dataset.col);
    }});
  }}

  function moveColumn(key, direction) {{
    const index = settings.order.indexOf(key);
    const nextIndex = index + direction;
    if (index < 0 || nextIndex < 0 || nextIndex >= settings.order.length) return;
    const nextOrder = settings.order.slice();
    [nextOrder[index], nextOrder[nextIndex]] = [nextOrder[nextIndex], nextOrder[index]];
    settings.order = nextOrder;
    saveColumnSettings();
    applyColumnSettings();
    renderColumnPanel();
  }}

  function renderColumnPanel() {{
    columnsList.innerHTML = "";
    const visible = new Set(settings.visible);
    orderedColumns().forEach((column, index) => {{
      const item = document.createElement("div");
      item.className = "hlr-column-item";
      item.dataset.col = column.key;
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = visible.has(column.key);
      checkbox.addEventListener("change", () => {{
        settings.visible = checkbox.checked
          ? Array.from(new Set(settings.visible.concat(column.key)))
          : settings.visible.filter((key) => key !== column.key);
        saveColumnSettings();
        applyColumnSettings();
      }});
      const text = document.createElement("span");
      text.textContent = column.label;
      label.append(checkbox, text);
      const up = document.createElement("button");
      up.type = "button";
      up.className = "hlr-column-move";
      up.textContent = "↑";
      up.disabled = index === 0;
      up.addEventListener("click", () => moveColumn(column.key, -1));
      const down = document.createElement("button");
      down.type = "button";
      down.className = "hlr-column-move";
      down.textContent = "↓";
      down.disabled = index === settings.order.length - 1;
      down.addEventListener("click", () => moveColumn(column.key, 1));
      item.append(label, up, down);
      columnsList.appendChild(item);
    }});
  }}

  function placeColumnsPanel() {{
    columnsPanel.classList.remove("open-up");
    const buttonRect = columnsButton.getBoundingClientRect();
    const panelHeight = Math.min(columnsPanel.scrollHeight || 430, Math.round(window.innerHeight * 0.7));
    const spaceBelow = window.innerHeight - buttonRect.bottom;
    const spaceAbove = buttonRect.top;
    if (spaceBelow < panelHeight + 12 && spaceAbove > spaceBelow) {{
      columnsPanel.classList.add("open-up");
    }}
  }}

  columnsButton.addEventListener("click", () => {{
    const isOpen = columnsPanel.classList.toggle("is-open");
    columnsButton.setAttribute("aria-expanded", isOpen ? "true" : "false");
    if (isOpen) placeColumnsPanel();
  }});
  window.addEventListener("resize", () => {{
    if (columnsPanel.classList.contains("is-open")) placeColumnsPanel();
  }});
  document.addEventListener("click", (event) => {{
    if (!columnsPanel.contains(event.target) && !columnsButton.contains(event.target)) {{
      columnsPanel.classList.remove("is-open");
      columnsButton.setAttribute("aria-expanded", "false");
    }}
  }});
  if (resetButton) {{
    resetButton.addEventListener("click", () => {{
      settings = {{ order: defaultOrder.slice(), visible: defaultOrder.filter((key) => defaultVisibleColumns.has(key)) }};
      saveColumnSettings();
      applyColumnSettings();
      renderColumnPanel();
    }});
  }}

  applyColumnSettings();
  renderColumnPanel();
}});
</script>
"""
    return page("HLR", body)

def dashboard_page(repo: Repository) -> bytes:
    metrics = "".join([
        dashboard_metric(repo, "SELECT COUNT(*) FROM routes WHERE is_actual = 1", "Активные маршруты", "Всего активных маршрутов", nav_icon("routes"), "blue", "0,22 18,22 32,16 46,22 62,17 78,17 96,10"),
        dashboard_metric(repo, "SELECT COUNT(*) FROM calling_companies WHERE is_active = 1", "Активные кампании", "Всего активных кампаний", nav_icon("companies"), "green", "0,22 12,17 28,16 44,16 58,15 72,10 84,15 96,9"),
        dashboard_metric(repo, "SELECT COUNT(*) FROM phone_numbers WHERE is_active = 1", "Купленные номера", "Всего активных номеров", nav_icon("phones"), "violet", "0,20 18,20 30,17 46,17 62,14 78,9 96,9"),
        dashboard_metric(repo, "SELECT COUNT(*) FROM routing_events WHERE is_active = 1", "Смены провайдеров", "Активные записи смен", nav_icon("provider_changes"), "teal", "0,8 16,10 32,10 48,12 64,12 80,14 96,14"),
    ])
    work_links = "".join([
        dashboard_link("/provider-changes", "Смена провайдеров", "Операционный журнал изменений", "provider_changes"),
        dashboard_link("/routes", "Маршруты", "Управление маршрутами и номерами", "routes"),
        dashboard_link("/tariffs", "Тарифы", "Актуальные цены и приоритеты", "tariffs"),
        dashboard_link("/phones", "Купленные номера", "Пул номеров и статусы", "phones"),
        dashboard_link("/companies", "Кампании прозвона", "Кампании, серверы и авторотация", "companies"),
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
        numbers = f'<div class="route-numbers-cell"><span class="route-numbers-label">{numbers_label}</span><a class="button route-numbers-action" href="/routes/{route["id"]}/numbers">Показать номера</a></div>'
        edit = f"<a class='button edit-action' href='/routes/{route['id']}/edit' title='Редактировать' aria-label='Редактировать' data-tooltip='Редактировать'>Редактировать</a>" if can_write("routes") else ""
        history = history_icon_link(f"/routes/{route['id']}/history")
        rows.append(f"<tr><td data-col='geo'>{esc(route['country_name'])}</td>{clamp_cell('route', esc(route['name']), route['name'], extra_attrs="data-copy-column='route-name'", classes='route-name-cell', selectable=True)}<td data-col='provider'>{esc(route['provider_name'])}</td><td data-col='prefix'>{esc(prefix)}</td><td data-col='actual'>{'Да' if route['is_actual'] else 'Нет'}</td>{clamp_cell('aon_pool', esc(route['aon_pool'] or '—'), route['aon_pool'] or '—')}{clamp_cell('comment', esc(route['comment']), route['comment'], classes='comment-cell')}<td data-col='numbers'>{numbers}</td><td data-col='history' class='history-cell'>{history}</td><td data-col='actions' class='actions'>{edit}</td></tr>")
    filters_html = f"""<form class="filter-grid" method="get" action="/routes">
<label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Провайдер <select name="provider_id">{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
<label>Префикс <select name="prefix_id">{prefix_options(repo, selected=q.get('prefix_id'), empty='Все')}</select></label>
<label>Актуальный <select name="is_actual"><option value="">Все</option><option value="1" {'selected' if q.get('is_actual')=='1' else ''}>Да</option><option value="0" {'selected' if q.get('is_actual')=='0' else ''}>Нет</option></select></label>
<label>Поиск <input name="search" value="{esc(q.get('search'))}"></label><button>Найти</button></form>"""
    create_html = f"""<form class="route-dialog route-dialog-form" method="post" action="/routes/create">
  <header class="route-dialog-header"><h2>Добавить маршрут</h2></header>
  <div class="route-dialog-body">
    <section class="route-dialog-section"><h3>Основные параметры</h3><div class="route-dialog-grid">
      <label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label>
      <label>Провайдер <span class="required">*</span><select name="provider_id">{active_options(repo, 'providers')}</select></label>
      <label>Префикс <select name="provider_prefix_id">{prefix_options(repo)}</select></label>
      <label>Проект/метка <select name="project_label">{project_options(repo, empty='—')}</select></label>
    </div></section>
    <section class="route-dialog-section"><h3>AON / пул</h3><div class="route-dialog-grid">
      <label>Тип АОН <span class="required">*</span><select name="cli_source_type">{aon_source_options()}</select></label>
      <label>Метка АОН <span class="required">*</span><input name="cli_source_label" value="Pool_A"></label>
      <label>Тип пула <span class="required">*</span><select name="aon_pool">{pool_type_options("Пул купленных номеров")}</select></label>
      <input type="hidden" name="rnd_type">
      <label>Принадлежность пула <input name="rnd_pool_owner" placeholder="венгерский пул"></label>
    </div></section>
    <section class="route-dialog-section"><h3>Статус и описание</h3><div class="route-dialog-grid">
      <label>Статус <span class="required">*</span><select name="is_actual"><option value="1">Активный</option><option value="0">Неактивный</option></select></label>
      <label class="route-dialog-full">Комментарий <textarea name="comment" rows="2"></textarea></label>
      <label class="route-dialog-full">Название маршрута <span class="required">*</span><input name="name" placeholder="Заполните обязательные поля для формирования названия"></label>
    </div></section>
  </div>
  <footer class="route-dialog-footer"><button type="submit" class="modal-save">Сохранить</button><button type="button" class="modal-cancel" data-modal-close>Отмена</button></footer>
</form>""" + route_aon_script()
    table_html = f"{data_table('routes', [('geo', 'ГЕО'), ('route', f"<span class='copyable-header'>Название маршрута {copy_column_button('route-name')}</span>"), ('provider', 'Провайдер'), ('prefix', 'Префикс'), ('actual', 'Актуальный'), ('aon_pool', 'АОН/пул'), ('comment', 'Комментарий'), ('numbers', 'Номера'), ('history', 'Ист.'), ('actions', 'Действия')], ''.join(rows))}"
    body = f"""
{filter_card(filters_html, q, ('country_id', 'provider_id', 'prefix_id', 'is_actual', 'search'))}
{form_card('+ Добавить маршрут', create_html, extra_class='route-create-shell', summary_class='route-primary-summary') if can_write("routes") else ""}
{table_card(table_html)}
{table_footer(pagination_html, column_settings('routes', [('geo', 'ГЕО'), ('route', 'Название маршрута'), ('provider', 'Провайдер'), ('prefix', 'Префикс'), ('actual', 'Актуальный'), ('aon_pool', 'АОН/пул'), ('comment', 'Комментарий'), ('numbers', 'Номера'), ('actions', 'Действия')], hlr_style=True) + export_link('/routes', q, text=True))}
"""
    return page("Маршруты", table_page_container(body, extra_class="routes-page"))


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
        cost = f"Подкл: {phone['connection_cost'] or '—'} / Абон: {display_monthly_fee(phone['monthly_fee'])} / Исх: {phone['outgoing_rate'] or '—'} / Вх: {phone['incoming_rate'] or '—'}"
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
        rows.append(f"""<tr><td data-col='number' class='selectable-cell' data-copy-column='phone-number'>{selectable_text(f"{esc(phone['number'])}{review_marker}", phone['number'], classes='phone-number-cell compound-value-cell')}</td><td data-col='geo'>{esc(phone['country_name'])}</td><td data-col='provider'>{esc(phone['provider_name'])}</td><td data-col='project'>{esc(phone['project_label'])}</td><td data-col='assignment'>{esc(assignment_label)}</td><td data-col='status'>{dot_status(STATUS_LABELS.get(phone['status'], phone['status']), 'danger' if phone['status'] == 'problem' else ('warning' if phone['status'] == 'unknown' else ('neutral' if phone['status'] == 'free' else 'ok')))}</td><td data-col='active'>{dot_status('Да' if phone['is_active'] else 'Нет', 'ok' if phone['is_active'] else 'danger')}</td>{clamp_cell('routes', esc(phone['route_names']), phone['route_names'], selectable=True) if phone['route_names'] else "<td data-col='routes'>—</td>"}<td data-col='connection'>{esc(phone['connection_cost'])}</td><td data-col='monthly'>{esc(display_monthly_fee(phone['monthly_fee']))}</td><td data-col='currency'>{esc(phone['currency_code'])}</td><td data-col='phone_type'>{esc(phone['phone_type'])}</td><td data-col='tariff'>{esc(phone['tariff_label'])}</td><td data-col='created'>{esc(phone['created_at'])}</td><td data-col='updated'>{esc(phone['updated_at'])}</td><td data-col='deactivated'>{esc(phone['deactivated_at'])}</td>{clamp_cell('comment', esc(phone['comment'] or '—'), phone['comment'] or '—', classes='comment-cell')}<td data-col='history' class='history-cell'>{history}</td><td data-col='actions'>{actions}</td></tr>""")
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
        label = row['name']
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
        label = row['name']
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
            "label": row["name"],
        }
        for row in rows
    ], ensure_ascii=False)


def current_priorities_json(repo: Repository) -> str:
    rows = repo.conn.execute(
        """
        SELECT srp.country_id, srp.server_id, COALESCE(r.name, '—') AS route_label
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
    overflow_provider_selected = event.get("overflow_provider_id") if isinstance(event, dict) else None
    if not overflow_provider_selected and overflow_route_selected:
        overflow_provider_row = repo.conn.execute("SELECT provider_id FROM routes WHERE id = ?", (overflow_route_selected,)).fetchone()
        overflow_provider_selected = overflow_provider_row["provider_id"] if overflow_provider_row else None
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
    if not is_existing_event:
        return f"""
<details class='form-card modal-form-card provider-change-create-shell' {'open' if error_message else ''} data-modal-details><summary class='form-summary provider-change-primary-summary'>+ Добавить событие</summary>
<form method='post' action='{action}' class='form-grid' id='routing-event-form' data-current-scope='{esc(scope)}'>
  {error_html}
  <fieldset class='provider-change-shell-scope'><legend>Область применения</legend>
    <div class='scope-cards'>
      <label class='card scope-card'><input type='radio' name='apply_scope' value='none' {'checked' if scope == 'none' else ''}><span class='scope-card-indicator' aria-hidden='true'></span><span class='scope-card-text'>Не меняли настройки в нашей системе</span></label>
      <label class='card scope-card'><input type='radio' name='apply_scope' value='server_priority' {'checked' if scope == 'server_priority' else ''}><span class='scope-card-indicator' aria-hidden='true'></span><span class='scope-card-text'>Серверный приоритет</span></label>
      <label class='card scope-card'><input type='radio' name='apply_scope' value='campaign_setting' {'checked' if scope == 'campaign_setting' else ''}><span class='scope-card-indicator' aria-hidden='true'></span><span class='scope-card-text'>Настройка кампании</span></label>
    </div>
  </fieldset>
  <div class='provider-change-content-grid' data-scope-content='none' data-scopes='none'>
    <label>Дата события <span class='required'>*</span><input type='datetime-local' name='event_at' value='{esc(event_at)}' required></label>
    <label>GEO <span class='required'>*</span><select name='country_id' id='event-country'>{active_options(repo, 'countries', selected=event['country_id'] if event else None, empty='—')}</select></label>
    <label>Провайдер <span class='required'>*</span><select name='provider_id' id='event-provider'>{active_options(repo, 'providers', selected=provider_selected, empty='—')}</select></label>
    <label>Маршрут/префикс <select name='affected_route_id' id='affected-route'>{route_opts}</select></label>
    <label class='span-2'>Причина <span class='required'>*</span><select name='reason' id='routing-reason' required>{routing_reason_options(event['reason'] if event else None, 'none')}</select></label>
    <label class='wide'>Комментарий <span class='required comment-required' hidden>*</span><textarea name='comment' id='routing-comment' rows='3' cols='60'>{esc(event['comment'] if event else '')}</textarea></label>
  </div>
  <div class='provider-change-server-priority-create' data-scope-content='server_priority' data-scopes='server_priority' hidden>
    <div class='server-priority-create-columns'>
      <div class='server-priority-create-left'>
        <div class='server-priority-create-row'>
          <label>Дата события <span class='required'>*</span><input type='datetime-local' name='event_at' value='{esc(event_at)}' required disabled></label>
          <label>GEO <span class='required'>*</span><select name='country_id' id='server-event-country' disabled>{active_options(repo, 'countries', selected=event['country_id'] if event else None, empty='—')}</select></label>
        </div>
        <label class='spillover-checkbox important-checkbox'><input type='checkbox' name='has_overflow' id='server-has-overflow' value='1' {has_overflow_checked} disabled> <span>Есть перелив</span></label>
        <label>Провайдер <span class='required'>*</span><select name='provider_id' id='server-event-provider' disabled>{active_options(repo, 'providers', selected=provider_selected, empty='—')}</select></label>
        <label>Новый маршрут <span class='required'>*</span><select name='new_route_id' id='server-new-route' class='route-select' disabled>{new_route_opts}</select></label>
        <span class='route-empty-message muted' id='server-new-route-empty' hidden>Нет маршрутов для выбранного провайдера и GEO</span>
        <div class='server-priority-overflow-block' id='server-overflow-block' hidden>
          <strong>Перелив</strong>
          <label>Провайдер перелива <span class='required'>*</span><select name='overflow_provider_id' id='server-overflow-provider' disabled>{active_options(repo, 'providers', selected=overflow_provider_selected, empty='—')}</select></label>
          <label>Маршрут перелива <span class='required'>*</span><select name='overflow_route_id' id='server-overflow-route' disabled>{overflow_opts}</select></label>
        </div>
        <label>Причина <span class='required'>*</span><select name='reason' id='server-routing-reason' required disabled>{routing_reason_options(event['reason'] if event else None, 'server_priority')}</select><span class='field-helper' id='server-routing-reason-helper'></span></label>
      </div>
      <div class='server-priority-create-right'>
        <span class='server-priority-create-title'>Серверы</span>
        {server_priority_server_boxes}
      </div>
    </div>
    <label class='server-priority-create-comment'>Комментарий <span class='required comment-required' hidden>*</span><textarea name='comment' id='server-routing-comment' rows='3' cols='60' disabled>{esc(event['comment'] if event else '')}</textarea></label>
  </div>
  <div class='provider-change-campaign-create-grid' data-scope-content='campaign_setting' data-scopes='campaign_setting' hidden>
    <label>Дата события <span class='required'>*</span><input type='datetime-local' name='event_at' value='{esc(event_at)}' required disabled></label>
    <label>Сервер <select name='server_id' id='campaign-server-filter' disabled>{options(repo, 'servers', selected=event['server_id'] if event else None, empty='—')}</select></label>
    <label>Тип изменения кампании <span class='required'>*</span><select name='company_change_type' id='company-change-type' required disabled>
      <option value=''>—</option>
      {''.join(f"<option value='{v}' {'selected' if event and event['company_change_type'] == v else ''}>{label}</option>" for v, label in [('enable_autorotation','Включили авторотацию'),('disable_autorotation','Выключили авторотацию'),('set_campaign_route','Прописали ручной маршрут'),('remove_campaign_route','Убрали ручной маршрут')])}
    </select></label>
    <div class='campaign-id-action-field'><span class='field-label'>ID кампании</span><input name='campaign_id_search' id='campaign-id-search' value='{esc(event['campaign_id_search'] if event and 'campaign_id_search' in event.keys() else '')}' disabled><span class='field-error' id='campaign-id-search-error' aria-live='polite'></span></div>
    <button type='button' id='campaign-id-search-button' class='small-button campaign-id-action-button' disabled>OK</button>
    <label class='campaign-reason-field'>Причина <span class='required'>*</span><select name='reason' id='campaign-routing-reason' required disabled>{routing_reason_options(event['reason'] if event else None, 'campaign_setting')}</select></label>
    <div class='campaign-company-field'>
      <span class='field-label'>Кампания <span class='required'>*</span></span>
      <details class='company-select-control' id='event-company' data-placeholder='—'>
        <summary id='event-company-summary'>—</summary>
        <div class='company-select-panel'>
          <div class='multi-select-actions'>
            <button type='button' class='small-button' id='campaign-select-visible'>Выбрать все найденные</button>
            <button type='button' class='small-button' id='campaign-clear-selected'>Отменить выбранные</button>
          </div>
          {company_opts}
        </div>
      </details>
    </div>
    <label class='wide'>Комментарий <textarea name='comment' id='campaign-routing-comment' rows='3' cols='60' disabled>{esc(event['comment'] if event else '')}</textarea></label>
  </div>
  <p class='provider-change-shell-hint' data-scope-hint='none'>Событие без изменения настроек фиксирует внешний или ручной контекст без применения изменений в системе.</p>
  <p class='provider-change-shell-hint' data-scope-hint='server_priority' hidden>Старый маршрут подтягивается автоматически из текущего server_route_priorities при создании.</p>
  <p class='provider-change-shell-hint' data-scope-hint='campaign_setting' hidden>Событие будет сохранено в журнале и применено к Схеме маршрутизации кампаний.</p>
  <button type='submit'>{submit}</button>
</form>
<script>
(function() {{
  const form = document.getElementById('routing-event-form');
  if (!form || !form.closest('.provider-change-create-shell')) return;
  const routes = {route_metadata_json(repo)};
  const priorities = {current_priorities_json(repo)};
  const campaigns = {campaign_metadata_json(repo)};
  function selectedScope() {{ return (form.querySelector('input[name="apply_scope"]:checked') || {{value: 'none'}}).value; }}
  function updateSelectTitle(select) {{
    if (!select) return;
    const selected = select.options[select.selectedIndex];
    select.title = selected ? selected.textContent : '';
  }}
  function rebuildAffectedRouteSelect() {{
    const select = document.getElementById('affected-route');
    const country = document.getElementById('event-country');
    const provider = document.getElementById('event-provider');
    if (!select) return;
    const current = select.value;
    const countryId = country ? country.value : '';
    const providerId = provider ? provider.value : '';
    select.innerHTML = '<option value="">—</option>';
    if (providerId) {{
      routes.forEach((route) => {{
        if ((!countryId || String(route.country_id) === String(countryId)) && String(route.provider_id) === String(providerId)) {{
          const opt = document.createElement('option');
          opt.value = route.id;
          opt.textContent = route.label;
          opt.title = route.label;
          if (String(route.id) === String(current)) opt.selected = true;
          select.appendChild(opt);
        }}
      }});
    }}
    updateSelectTitle(select);
  }}

  function rebuildServerRouteSelect(select, countryId, providerId, emptyEl, requireProvider) {{
    if (!select) return;
    const current = select.value;
    select.innerHTML = '<option value="">—</option>';
    let count = 0;
    if (!requireProvider || providerId) {{
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
    }}
    if (emptyEl) emptyEl.hidden = !(countryId && providerId && count === 0);
    updateSelectTitle(select);
  }}
  function renderCurrentRoutes() {{
    const panel = form.querySelector('[data-server-current-routes]');
    if (!panel) return;
    panel.innerHTML = '';
    const boxes = Array.from(form.querySelectorAll('.provider-change-server-priority-create input[name="server_ids"]'));
    if (!boxes.length) {{ panel.innerHTML = '<span class="server-current-routes-empty">Нет активных серверов</span>'; return; }}
    boxes.forEach((box) => {{
      const chip = box.closest('[data-server-chip]');
      const name = chip ? chip.dataset.serverName : box.value;
      const route = (chip && chip.dataset.currentRoute) || (chip && chip.dataset.initialRoute) || '—';
      const row = document.createElement('div');
      row.className = 'server-current-route-row';
      const server = document.createElement('span');
      server.className = 'server-current-route-name';
      server.textContent = `${{name}} —`;
      const text = document.createElement('span');
      text.className = 'server-current-route-text';
      if (route && route !== '—') text.classList.add('has-route');
      text.textContent = route || '—';
      text.title = route || '—';
      row.append(server, text);
      panel.appendChild(row);
    }});
  }}
  function updateServerSelectionCount() {{
    const boxes = Array.from(form.querySelectorAll('.provider-change-server-priority-create input[name="server_ids"]'));
    const counter = form.querySelector('[data-server-selection-count]');
    if (counter) counter.textContent = `${{boxes.filter((box) => box.checked).length}} из ${{boxes.length}} выбрано`;
    renderCurrentRoutes();
  }}

  function syncCommentRequirement() {{
    const reason = document.getElementById('routing-reason');
    const comment = document.getElementById('routing-comment');
    const marker = form.querySelector('.comment-required');
    const required = selectedScope() === 'none' && reason && reason.value === 'Другое';
    if (comment) comment.required = !!required;
    if (marker) marker.hidden = !required;
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
        box.disabled = !show || selectedScope() !== 'campaign_setting';
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
    if (!found) {{ setCampaignSearchError('Кампания с таким ID не найдена'); return; }}
    if (server.value && String(found.server_id) !== String(server.value)) {{
      const selectedServerName = (server.options[server.selectedIndex] && server.options[server.selectedIndex].textContent) || server.value;
      setCampaignSearchError(`Кампания с ID ${{campaignId}} находится на сервере ${{found.server_name}}, а выбран сервер ${{selectedServerName}}`);
      return;
    }}
    const box = container.querySelector(`input[name="calling_company_ids"][value="${{found.id}}"]`);
    if (box && !box.disabled) box.checked = true;
    updateCompanySummary();
  }}
  function sync() {{
    const scope = selectedScope();
    form.dataset.currentScope = scope;
    form.querySelectorAll('.scope-card').forEach((card) => card.classList.toggle('selected', card.querySelector('input').checked));
    form.querySelectorAll('[data-scope-content]').forEach((content) => {{
      const show = content.dataset.scopeContent === scope;
      content.hidden = !show;
      content.querySelectorAll('input, select, textarea, button').forEach((field) => {{ field.disabled = !show; }});
    }});
    form.querySelectorAll('[data-scope-hint]').forEach((hint) => {{ hint.hidden = hint.dataset.scopeHint !== scope; }});
    const serverCountry = document.getElementById('server-event-country');
    const serverProvider = document.getElementById('server-event-provider');
    const hintCountryId = (serverCountry && serverCountry.value) || '';
    form.querySelectorAll('[data-server-chip]').forEach((chip) => {{
      const route = hintCountryId ? (priorities[`${{hintCountryId}}:${{chip.dataset.serverId}}`] || '—') : '—';
      chip.dataset.currentRoute = route;
      const hint = chip.querySelector('[data-current-route-hint]');
      if (hint) {{ hint.textContent = route; hint.title = route; }}
    }});
    rebuildAffectedRouteSelect();
    rebuildServerRouteSelect(document.getElementById('server-new-route'), serverCountry && serverCountry.value, serverProvider && serverProvider.value, document.getElementById('server-new-route-empty'), true);
    const serverOverflowEnabled = scope === 'server_priority' && document.getElementById('server-has-overflow') && document.getElementById('server-has-overflow').checked;
    const serverOverflowBlock = document.getElementById('server-overflow-block');
    const serverOverflowProvider = document.getElementById('server-overflow-provider');
    const serverOverflowRoute = document.getElementById('server-overflow-route');
    if (serverOverflowBlock) serverOverflowBlock.hidden = !serverOverflowEnabled;
    if (serverOverflowProvider) {{ serverOverflowProvider.disabled = !serverOverflowEnabled; serverOverflowProvider.required = !!serverOverflowEnabled; if (!serverOverflowEnabled) serverOverflowProvider.value = ''; }}
    rebuildServerRouteSelect(serverOverflowRoute, serverCountry && serverCountry.value, serverOverflowProvider && serverOverflowProvider.value, null, true);
    if (serverOverflowRoute) {{ serverOverflowRoute.disabled = !serverOverflowEnabled || !(serverCountry && serverCountry.value) || !(serverOverflowProvider && serverOverflowProvider.value); serverOverflowRoute.required = !!serverOverflowEnabled; if (!serverOverflowEnabled) serverOverflowRoute.value = ''; }}
    updateServerSelectionCount();
    filterCompanyOptions(false);
    syncCommentRequirement();
  }}
  form.querySelectorAll('input[name="apply_scope"], #event-country, #event-provider, #server-event-country, #server-event-provider, #server-has-overflow, #server-overflow-provider').forEach((el) => el.addEventListener('change', sync));
  form.querySelectorAll('.provider-change-server-priority-create [data-server-select]').forEach((button) => button.addEventListener('click', () => {{
    const checked = button.dataset.serverSelect === 'all';
    form.querySelectorAll('.provider-change-server-priority-create input[name="server_ids"]').forEach((box) => {{ box.checked = checked; }});
    updateServerSelectionCount();
  }}));
  form.querySelectorAll('.provider-change-server-priority-create input[name="server_ids"]').forEach((box) => box.addEventListener('change', updateServerSelectionCount));
  const affectedRoute = document.getElementById('affected-route');
  if (affectedRoute) affectedRoute.addEventListener('change', () => updateSelectTitle(affectedRoute));
  const reason = document.getElementById('routing-reason');
  if (reason) reason.addEventListener('change', syncCommentRequirement);
  form.querySelectorAll('input[name="calling_company_ids"]').forEach((el) => el.addEventListener('change', updateCompanySummary));
  const campaignServerFilter = document.getElementById('campaign-server-filter');
  if (campaignServerFilter) campaignServerFilter.addEventListener('change', () => {{ filterCompanyOptions(true); }});
  const campaignSearchButton = document.getElementById('campaign-id-search-button');
  if (campaignSearchButton) campaignSearchButton.addEventListener('click', findCampaignByVisibleId);
  const selectVisible = document.getElementById('campaign-select-visible');
  if (selectVisible) selectVisible.addEventListener('click', () => {{
    document.querySelectorAll('#event-company .multi-option:not([hidden]) input[name="calling_company_ids"]').forEach((box) => {{ if (!box.disabled) box.checked = true; }});
    updateCompanySummary();
  }});
  const clearSelected = document.getElementById('campaign-clear-selected');
  if (clearSelected) clearSelected.addEventListener('click', () => {{
    form.querySelectorAll('input[name="calling_company_ids"]:checked').forEach((box) => {{ box.checked = false; }});
    updateCompanySummary();
  }});
  sync();
}})();
</script>
</details>
"""
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
    <label class='provider-change-date-field'>Дата события <span class='required'>*</span><input type='datetime-local' name='event_at' value='{esc(event_at)}' required></label>
    <label class='scope-field campaign-helper-field campaign-server-field' data-scopes='campaign_setting'>Сервер <select name='server_id' id='campaign-server-filter'>{options(repo, 'servers', selected=event['server_id'] if event else None, empty='—')}</select></label>
    <label class='scope-field campaign-change-type-field' data-scopes='campaign_setting'>Тип изменения кампании <span class='required'>*</span><select name='company_change_type' id='company-change-type'>
      <option value=''>—</option>
      {''.join(f"<option value='{v}' {'selected' if event and event['company_change_type'] == v else ''}>{label}</option>" for v, label in [('enable_autorotation','Включили авторотацию'),('disable_autorotation','Выключили авторотацию'),('set_campaign_route','Прописали ручной маршрут'),('remove_campaign_route','Убрали ручной маршрут')])}
    </select></label>
    <div class='scope-field campaign-helper-field campaign-id-action-field' data-scopes='campaign_setting'><span class='field-label'>ID кампании</span><div class='campaign-id-inline-action'><input name='campaign_id_search' id='campaign-id-search' value='{esc(event['campaign_id_search'] if event and 'campaign_id_search' in event.keys() else '')}'><button type='button' id='campaign-id-search-button' class='small-button'>OK</button></div><span class='field-error' id='campaign-id-search-error' aria-live='polite'></span></div>
  </div>
  <label class='scope-field routing-geo-field' data-scopes='none server_priority'>GEO <span class='required'>*</span><select name='country_id' id='event-country'>{active_options(repo, 'countries', selected=event['country_id'] if event else None, empty='—')}</select></label>
  <fieldset class='scope-field' data-scopes='server_priority'><legend>Серверы <span class='required'>*</span></legend>{server_priority_server_boxes}</fieldset>
  <label class='scope-field routing-provider-field' data-scopes='none server_priority'>Провайдер <span class='required provider-required'>*</span><select name='provider_id' id='event-provider'>{active_options(repo, 'providers', selected=provider_selected, empty='—')}</select></label>
  <label class='scope-field' data-scopes='none'>Маршрут/префикс <select name='affected_route_id' id='affected-route'>{route_opts}</select></label>
  {old_route_field}
  <label class='scope-field route-select-field' data-scopes='server_priority'>Новый маршрут <span class='required'>*</span><select name='new_route_id' id='new-route' class='route-select'>{new_route_opts}</select></label>
  <span class='scope-field route-empty-message muted' data-scopes='server_priority' id='new-route-empty' hidden>Нет маршрутов для выбранного провайдера и GEO</span>
  <label class='scope-field spillover-checkbox important-checkbox' data-scopes='server_priority'><input type='checkbox' name='has_overflow' id='has-overflow' value='1' {has_overflow_checked}> <span>Есть перелив</span></label>
  <div class='scope-field server-priority-overflow-block' data-scopes='server_priority' id='overflow-block' hidden>
    <strong>Перелив</strong>
    <label>Провайдер перелива <span class='required'>*</span><select name='overflow_provider_id' id='overflow-provider'>{active_options(repo, 'providers', selected=overflow_provider_selected, empty='—')}</select></label>
    <label id='overflow-route-field'>Маршрут перелива <span class='required'>*</span><select name='overflow_route_id' id='overflow-route'>{overflow_opts}</select></label>
  </div>
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
  <label class='wide provider-change-comment-field'>Комментарий <span class='required comment-required'>*</span><textarea name='comment' id='routing-comment' rows='3' cols='60'>{esc(event['comment'] if event else '')}</textarea></label>
  <p class='scope-field muted wide provider-change-service-note' data-scopes='campaign_setting'>Событие будет сохранено в журнале и применено к ‘Схеме маршрутизации кампаний’.</p>
  <p class='scope-field muted wide provider-change-service-note' data-scopes='server_priority'>Старый маршрут подтягивается автоматически из текущего server_route_priorities при создании.</p>
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
  function rebuildRouteSelect(select, countryId, providerId, emptyEl, requireProvider) {{
    if (!select) return;
    const current = select.value;
    select.innerHTML = '<option value="">—</option>';
    let count = 0;
    if (!requireProvider || providerId) {{
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
    }}
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
    rebuildRouteSelect(document.getElementById('affected-route'), country && country.value, provider && provider.value, null, true);
    rebuildRouteSelect(document.getElementById('new-route'), country && country.value, provider && provider.value, document.getElementById('new-route-empty'));
    const overflowProvider = document.getElementById('overflow-provider');
    rebuildRouteSelect(document.getElementById('overflow-route'), country && country.value, overflowProvider && overflowProvider.value, null, true);
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
    const overflowBlock = document.getElementById('overflow-block');
    const overflowRoute = document.getElementById('overflow-route');
    const overflowEnabled = scope === 'server_priority' && hasOverflow && hasOverflow.checked;
    if (overflowBlock) overflowBlock.hidden = !overflowEnabled;
    if (overflowProvider) {{ overflowProvider.disabled = !overflowEnabled; overflowProvider.required = !!overflowEnabled; if (!overflowEnabled) overflowProvider.value = ''; }}
    if (overflowRoute) {{ overflowRoute.disabled = !overflowEnabled || !(country && country.value) || !(overflowProvider && overflowProvider.value); overflowRoute.required = !!overflowEnabled; if (!overflowEnabled) overflowRoute.value = ''; }}
    setRequired(ctype, scope === 'campaign_setting');
    syncCommentRequirement(scope);
  }}
  function renderCurrentRoutes() {{
    const panel = form.querySelector('[data-server-current-routes]');
    if (!panel) return;
    const boxes = Array.from(form.querySelectorAll('input[name="server_ids"]'));
    if (!boxes.length) {{
      panel.innerHTML = '<span class="server-current-routes-empty">Нет активных серверов</span>';
      return;
    }}
    panel.innerHTML = '';
    boxes.forEach((box) => {{
      const chip = box.closest('[data-server-chip]');
      const name = chip ? chip.dataset.serverName : box.value;
      const route = (chip && chip.dataset.currentRoute) || (chip && chip.dataset.initialRoute) || '—';
      const row = document.createElement('div');
      row.className = 'server-current-route-row';
      const server = document.createElement('span');
      server.className = 'server-current-route-name';
      server.textContent = `${{name}} —`;
      const text = document.createElement('span');
      text.className = 'server-current-route-text';
      if (route && route !== '—') text.classList.add('has-route');
      text.textContent = route || '—';
      text.title = route || '—';
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
  form.querySelectorAll('input[name="apply_scope"], #event-country, #event-provider, #campaign-provider, #company-change-type, #has-overflow, #overflow-provider').forEach((el) => el.addEventListener('change', sync));
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
                if ev["has_overflow"]:
                    overflow_provider = snapshot.get("overflow_provider_name") or "—"
                    overflow_route = ev["overflow_route_name"] or "—"
                    overflow_text = f"Перелив: да; Провайдер перелива: {esc(overflow_provider)}; Маршрут перелива: {esc(overflow_route)}"
                else:
                    overflow_text = "Перелив: нет"
                return server_text, "—", "Серверы:<ul class='event-server-list'>" + "".join(items) + f"</ul>; {overflow_text}"
        route_text = f"{esc(ev['old_route_name'] or '—')} → {esc(ev['new_route_name'] or '—')}"
        if ev["has_overflow"]:
            overflow_provider = snapshot.get("overflow_provider_name") or "—"
            overflow_route = ev["overflow_route_name"] or "—"
            overflow_text = f"Перелив: да; Провайдер перелива: {esc(overflow_provider)}; Маршрут перелива: {esc(overflow_route)}"
        else:
            overflow_text = "Перелив: нет"
        return ev["server_name"] or "—", "—", route_text + f"; {overflow_text}"
    campaign = "—"
    if ev["company_id_external"] or ev["company_name"]:
        campaign = f"{ev['company_id_external'] or '—'} / {ev['company_name'] or '—'}"
    def company_route_label(prefix: str) -> str:
        route_id = ev[f"{prefix}_company_route_id"]
        route_name = ev[f"{prefix}_company_route_name"] if f"{prefix}_company_route_name" in ev.keys() else None
        if not route_id:
            return "—"
        return route_name or str(route_id)

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
<label class='checkbox-inline'><input type='checkbox' name='include_inactive' value='1' {'checked' if q.get('include_inactive') == '1' else ''}> <span>Показывать архив</span></label>
<button type='submit'>Найти</button></form>"""
    journal_html = f"{data_table('provider_changes', [('event_at', 'Дата события'), ('scope', 'Область применения'), ('geo', 'GEO'), ('server', 'Сервер'), ('campaign', 'Кампания'), ('details', 'Детали'), ('comment', 'Комментарий'), ('reason', 'Причина'), ('actions', 'Действия')], ''.join(rows))}"
    body = f"""
{routing_event_form(repo, form_data, form_error) if can_write("provider_changes") else ""}
{filter_card(filters_html, q, ('date_from', 'date_to', 'country_id', 'apply_scope', 'server_id', 'campaign_id', 'provider_id', 'include_inactive'))}
{f"<div class='notice ok'>{esc(q.get('notice'))}</div>" if q.get('notice') else ""}
{f"<div class='notice error'>{esc(filter_error)}</div>" if filter_error else ""}
{table_card(journal_html, title='Журнал событий', extra_class='journal-card')}
{table_footer(pagination_html, column_settings('provider_changes', [('event_at', 'Дата события'), ('scope', 'Область применения'), ('geo', 'GEO'), ('server', 'Сервер'), ('campaign', 'Кампания'), ('details', 'Детали'), ('comment', 'Комментарий'), ('reason', 'Причина'), ('actions', 'Действия')], hlr_style=True) + export_link('/provider-changes', q, text=True))}
"""
    return page("Смена провайдеров", table_page_container(body, extra_class="provider-changes-page"))


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
{table_card(table_html)}
{table_footer(f"<nav class='pagination table-status-nav' aria-label='Статус таблицы'><span class='table-status-summary'><span class='table-status-item'>Всего записей: {len(rows)}</span><span class='table-status-item table-selection-status' data-selected-count hidden>Выбрано: <strong>0</strong></span><span class='table-status-item'>Страница 1 из 1</span></span></nav>")}"""
    return page("Пользователи", table_page_container(body))

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
    phone_requirements_hidden = "" if selected_entity == "phone_numbers" else " hidden"
    phone_requirements = f"""<section id="phone-import-requirements" class="card"{phone_requirements_hidden}>
<h2>Требования к файлу</h2>
<p><a class="button" href="/admin/import/template?entity=phone_numbers">Скачать шаблон CSV</a></p>
<p><strong>Поддерживаемые колонки:</strong> Номер, Страна, Провайдер, Проект, Назначение, Итоговый статус, АП, АП в EUR, Тариф, Комментарий, Создал, Создано.</p>
<p><strong>Обязательные поля:</strong> Номер, Страна, Итоговый статус.</p>
<p><strong>Справочные поля:</strong> Страна, Провайдер, Проект, Назначение, Тип номера, если используется; Валюта, если используется. Значения должны быть заранее заведены в справочники. Active и inactive/legacy значения импортируются, missing значения блокируют импорт.</p>
<ul>
<li>Пустой Провайдер, Проект или Назначение → номер получит «Требует проверки».</li>
<li>Пустая АП в EUR / ? / Неизвестно → Абонплата = NULL; в UI отображается «???», и «Требует проверки» только из-за этого не ставится.</li>
<li>Отключен → Активен у провайдера: Нет; Рабочий статус: Не используется.</li>
<li>??? → Активен у провайдера: Да; Рабочий статус: Не известно; Требует проверки.</li>
<li>Не используется / Не нужен / Свободен → Активен у провайдера: Да; Рабочий статус: Не известно; Требует проверки.</li>
<li>Используется → Активен у провайдера: Да; Рабочий статус: Используется.</li>
<li>Колонка «АП» игнорируется; «АП в EUR» импортируется в «Абонплата». Поддерживается запятая: 46,63 → 46.63. Значения ?, Неизвестно, пусто, - → NULL; NULL показывается как «???».</li>
</ul>
</section>"""
    body = f"""<h1>Администрирование → Импорт / экспорт</h1><form method="post" action="/admin/import/preview"><label>Раздел <span class="required">*</span><select name="entity_type" id="entity_type"><option value="routes" {sel('routes')}>Маршруты</option><option value="tariffs" {sel('tariffs')}>Тарифы</option><option value="phone_numbers" {sel('phone_numbers')}>Купленные номера</option><option value="calling_companies" {sel('calling_companies')}>Кампании прозвона</option><option value="dictionaries" {sel('dictionaries')}>Справочники</option></select></label><label>Режим <select name="mode" id="import_mode"><option value="append_update" {mode_sel('append_update')}>Дополнить / обновить</option></select></label><p class='muted'>Режим «Заменить выбранный раздел» временно отключён. Используйте «Дополнить / обновить».</p>{phone_requirements}<br><textarea name="csv_data" rows="12" cols="110" placeholder="Вставьте CSV с заголовками">{esc(csv_data)}</textarea><br><button>Предпросмотр</button><button formaction="/admin/import/apply">{nav_icon("import")}<span>Импортировать</span></button></form>{preview_html}<script>
const entity = document.getElementById('entity_type');
const mode = document.getElementById('import_mode');
const phoneRequirements = document.getElementById('phone-import-requirements');
function syncImportMode() {{
  mode.value = 'append_update';
  if (phoneRequirements) {{ phoneRequirements.hidden = entity.value !== 'phone_numbers'; }}
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
    create_html = f"""<form class="form-grid currency-rate-form" method="post" action="/admin/currency-rates/upsert">
<label class="currency-rate-currency">Валюта провайдера <span class="required">*</span><select name="currency_id">{active_options(repo, 'currencies', 'code')}</select></label>
<label class="currency-rate-value"><span>Курс к EUR</span><span class="currency-rate-inline"><span class="currency-rate-prefix">1 единица валюты провайдера =</span><input name="rate_to_eur" placeholder="0.92"><span class="currency-rate-suffix">EUR</span></span></label>
<div class="modal-actions currency-rate-actions"><button type="submit" class="modal-save">Применить</button><button type="button" class="modal-cancel" data-modal-close>Отмена</button></div></form>"""
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

    def edit_field(label: str, control_html: str) -> str:
        return f"<label>{label} {control_html}</label>"

    def row_class(row: sqlite3.Row) -> str:
        return " class='inactive-row'" if not row["is_active"] else ""

    def rename_policy_block(section: str, entity_id: int) -> str:
        counts = repo.dictionary_rename_preview(section, int(entity_id))
        count_items = "".join(f"<li>{esc(label)}: {count}</li>" for label, count in counts.items()) or "<li>Связанные записи не найдены</li>"
        return f"""<fieldset class='safe-rename-block'><legend>Что сделать со связанными записями?</legend>
<label class='safe-rename-option'><input type='radio' name='rename_mode' value='dictionary_only' checked><span class='safe-rename-indicator' aria-hidden='true'></span><span><strong>Только переименовать справочник</strong><span class='muted'>Новые записи будут использовать новое название. Уже связанные записи сохранят текущее отображаемое значение.</span></span></label>
<label class='safe-rename-option'><input type='radio' name='rename_mode' value='update_linked'><span class='safe-rename-indicator' aria-hidden='true'></span><span><strong>Переименовать справочник и обновить связанные записи</strong><span class='muted'>Все связанные записи будут показывать новое название. Используйте для исправления опечаток или неправильных названий.</span></span></label>
<div class='notice warning'><strong>Preview массового обновления:</strong><ul>{count_items}</ul><label><input type='checkbox' name='confirm_update_linked' value='1'> Подтверждаю обновление связанных записей, если выбран массовый режим.</label></div>
</fieldset>"""

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
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td>{esc(row['code'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td class='muted'>—</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/countries/{row['id']}/update'>{edit_field('Название GEO', f"<input name='name' value='{esc(row['name'])}'>")}{edit_field('Код GEO', f"<input name='code' value='{esc(row['code'])}' placeholder='Код'>")}{edit_field('Статус', active_select(row['is_active']))}{rename_policy_block('countries', row['id'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "providers":
        headers = ["Название", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT p.*, c.code AS currency_code FROM providers p LEFT JOIN currencies c ON c.id = p.default_currency_id ORDER BY p.name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/providers/{row['id']}/update'>{edit_field('Название провайдера', f"<input name='name' value='{esc(row['name'])}'>")}{edit_field('Валюта провайдера', f"<select name='default_currency_id'><option value=''>—</option>{options(repo, 'currencies', 'code', selected=row['default_currency_id'])}</select>")}{edit_field('Комментарий', f"<input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>")}{edit_field('Статус', active_select(row['is_active']))}{rename_policy_block('providers', row['id'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "currencies":
        headers = ["Код валюты", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM currencies ORDER BY code"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['code'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['name'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/currencies/{row['id']}/update'>{edit_field('Код валюты', f"<input name='code' value='{esc(row['code'])}'>")}{edit_field('Название валюты / комментарий', f"<input name='name' value='{esc(row['name'])}' placeholder='Комментарий'>")}{edit_field('Статус', active_select(row['is_active']))}{rename_policy_block('currencies', row['id'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "prefixes":
        headers = ["Префикс", "Провайдер", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("""
            SELECT pp.*, p.name AS provider_name
            FROM provider_prefixes pp JOIN providers p ON p.id = pp.provider_id
            ORDER BY p.name, COALESCE(pp.prefix, '')
        """))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['prefix'] or 'Без префикса')}</td><td>{esc(row['provider_name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['name'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/prefixes/{row['id']}/update'>{edit_field('Провайдер префикса', f"<select name='provider_id'>{options(repo, 'providers', selected=row['provider_id'])}</select>")}{edit_field('Префикс', f"<input name='prefix' value='{esc(row['prefix'])}' placeholder='Без префикса или цифры'>")}{edit_field('Комментарий', f"<input name='name' value='{esc(row['name'])}' placeholder='Комментарий'>")}{edit_field('Статус', active_select(row['is_active']))}{rename_policy_block('prefixes', row['id'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "servers":
        headers = ["Сервер", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM servers ORDER BY name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/servers/{row['id']}/update'>{edit_field('Название сервера', f"<input name='name' value='{esc(row['name'])}'>")}{edit_field('Комментарий', f"<input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>")}{edit_field('Статус', active_select(row['is_active']))}{rename_policy_block('servers', row['id'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "phone-types":
        headers = ["Тип номера", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM phone_number_types ORDER BY name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/phone-types/{row['id']}/update'>{edit_field('Тип номера', f"<input name='name' value='{esc(row['name'])}'>")}{edit_field('Комментарий', f"<input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>")}{edit_field('Статус', active_select(row['is_active']))}{rename_policy_block('phone-types', row['id'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "projects":
        headers = ["Название проекта", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM projects ORDER BY sort_order, name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/projects/{row['id']}/update'>{edit_field('Название проекта', f"<input name='name' value='{esc(row['name'])}'>")}{edit_field('Комментарий', f"<input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>")}{edit_field('Статус', active_select(row['is_active']))}{rename_policy_block('projects', row['id'])}<button>Сохранить</button></form></details></td></tr>""")
    else:
        headers = ["Назначение", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM phone_assignment_types ORDER BY sort_order, name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td data-col='actions'><details class='edit-details'><summary title='Редактировать' aria-label='Редактировать'>Редактировать</summary><form method='post' action='/admin/dictionaries/phone-assignments/{row['id']}/update'>{edit_field('Назначение номера', f"<input name='name' value='{esc(row['name'])}'>")}{edit_field('Код / системное значение', f"<input name='code' value='{esc(row['code'])}' readonly>")}{edit_field('Комментарий', f"<input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>")}{edit_field('Статус', active_select(row['is_active']))}{rename_policy_block('phone-assignments', row['id'])}<button>Сохранить</button></form></details></td></tr>""")

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
    {table_footer(f"<nav class='pagination table-status-nav' aria-label='Статус таблицы'><span class='table-status-summary'><span class='table-status-item'>Всего записей: {len(source)}</span><span class='table-status-item table-selection-status' data-selected-count hidden>Выбрано: <strong>0</strong></span><span class='table-status-item'>Страница 1 из 1</span></span></nav>")}
  </section>
</div>"""
    return page("Справочные значения", table_page_container(body))


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
<form class='route-dialog route-dialog-form route-dialog-page-form' method='post' action='/routes/{route_id}/update' data-country-name='{esc(route['country_name']) if 'country_name' in route.keys() else ''}'>
<header class='route-dialog-header'><h2>Редактировать маршрут</h2></header>
<div class='route-dialog-body'>
<section class='route-dialog-section'><h3>Основные параметры</h3><div class='route-dialog-grid'>
<label>ГЕО <input value='{esc(route['country_name']) if 'country_name' in route.keys() else ''}' readonly></label>
<label>Провайдер <span class='required'>*</span><select name='provider_id'>{active_options(repo, 'providers', selected=route['provider_id'])}</select></label>
<label>Префикс <select name='provider_prefix_id'>{prefix_options(repo, selected=route['provider_prefix_id'])}</select></label>
<label>Проект/метка <input name='project_label' value='{esc(route['project_label'] or '')}' readonly></label>
</div></section>
<section class='route-dialog-section'><h3>AON / пул</h3><div class='route-dialog-grid'>
<label>Тип АОН <span class='required'>*</span><select name='cli_source_type'>{aon_source_options(route['cli_source_type'], include_legacy=True)}</select></label>
<label>Метка АОН <span class='required'>*</span><input name='cli_source_label' value='{esc(route['cli_source_label'])}'></label>
<label>Тип пула <span class='required'>*</span><select name='aon_pool'>{pool_type_options((route['aon_pool'] or '').split(':', 1)[0])}</select></label>
<input type='hidden' name='rnd_type' value='{esc(route['rnd_type'] or '')}'>
<label>Принадлежность пула <input name='rnd_pool_owner' value='{esc(route['rnd_pool_owner'] or '')}'></label>
</div></section>
<section class='route-dialog-section'><h3>Статус и описание</h3><div class='route-dialog-grid'>
<label>Актуальный <select name='is_actual'><option value='1' {'selected' if route['is_actual'] else ''}>Активный</option><option value='0' {'selected' if not route['is_actual'] else ''}>Неактивный</option></select></label>
<label>Приоритет <select name='priority_status'><option value='priority' {'selected' if route['priority_status']=='priority' else ''}>priority</option><option value='alternative' {'selected' if route['priority_status']=='alternative' else ''}>alternative</option><option value='unknown' {'selected' if route['priority_status']=='unknown' else ''}>unknown</option></select></label>
<label class='route-dialog-full'>Комментарий <textarea name='comment' rows='2'>{esc(route['comment'])}</textarea></label>
<label class='route-dialog-full'>Название маршрута <span class='required'>*</span><input name='name' value='{esc(route['name'])}' size='60'></label>
</div></section>
</div>
<footer class='route-dialog-footer'><button type='submit' class='modal-save'>Сохранить</button><button type='button' class='modal-cancel' onclick="history.back()">Отмена</button></footer></form>
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
<label>Абонентская плата <input name='monthly_fee' value='{esc(phone['monthly_fee'] or '')}'></label>
<label>Валюта <select name='currency_id'><option value=''>—</option>{active_options(repo, 'currencies', 'code', selected=phone['currency_id'])}</select></label>
<label>Тип номера <select name='phone_type'>{phone_type_options(repo, selected=phone['phone_type'], empty='—')}</select></label>
<label>Тариф <input name='tariff_label' value='{esc(phone['tariff_label'])}'></label>
<label>Комментарий <input name='comment' value='{esc(phone['comment'])}'></label>
{f"<p class='muted'><strong>Создал в Excel:</strong> {esc(phone['imported_created_by'])}</p>" if phone['imported_created_by'] else ""}
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
            has_overflow=(data.get("has_overflow") == "1"), overflow_route_id=parse_int(data.get("overflow_route_id")), overflow_provider_id=parse_int(data.get("overflow_provider_id")), created_by=actor_id,
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
        label_tables = {
            "countries": ("countries", "name"),
            "providers": ("providers", "name"),
            "currencies": ("currencies", "code"),
            "prefixes": ("provider_prefixes", "prefix"),
            "servers": ("servers", "name"),
            "phone-types": ("phone_number_types", "name"),
            "projects": ("projects", "name"),
            "phone-assignments": ("phone_assignment_types", "name"),
        }
        table, label_column = label_tables.get(kind, (None, None))
        if table is None:
            raise BusinessRuleError("Неизвестный справочник")
        before = repo.conn.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
        old_label = before[label_column] if before else None
        if kind == "countries":
            new_label = data["name"].strip()
            repo.conn.execute("UPDATE countries SET name = ?, code = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_label, data.get("code") or None, is_active, entity_id))
        elif kind == "providers":
            new_label = data["name"].strip()
            repo.conn.execute("UPDATE providers SET name = ?, normalized_name = ?, default_currency_id = ?, comment = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_label, normalize_provider_name(data["name"]), parse_int(data.get("default_currency_id")), data.get("comment") or None, is_active, entity_id))
        elif kind == "currencies":
            new_label = data["code"].strip().upper()
            repo.conn.execute("UPDATE currencies SET code = ?, name = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_label, data.get("name") or new_label, is_active, entity_id))
        elif kind == "prefixes":
            prefix = normalize_real_prefix(data.get("prefix") or None)
            new_label = prefix
            repo.conn.execute("UPDATE provider_prefixes SET provider_id = ?, prefix = ?, name = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (int(data["provider_id"]), prefix, data.get("name") or None, is_active, entity_id))
        elif kind == "servers":
            new_label = data["name"].strip()
            repo.conn.execute("UPDATE servers SET name = ?, comment = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_label, data.get("comment") or None, is_active, entity_id))
        elif kind == "phone-types":
            new_label = data["name"].strip()
            repo.conn.execute("UPDATE phone_number_types SET name = ?, comment = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_label, data.get("comment") or None, is_active, entity_id))
        elif kind == "projects":
            new_label = data["name"].strip()
            repo.conn.execute("UPDATE projects SET name = ?, comment = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_label, data.get("comment") or None, is_active, entity_id))
        elif kind == "phone-assignments":
            new_label = data["name"].strip()
            repo.conn.execute("UPDATE phone_assignment_types SET name = ?, comment = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_label, data.get("comment") or None, is_active, entity_id))
        update_linked = data.get("rename_mode") == "update_linked" and old_label != new_label
        if update_linked and data.get("confirm_update_linked") != "1":
            raise BusinessRuleError("Подтвердите массовое обновление связанных записей")
        counts = repo.update_dictionary_snapshots(kind, entity_id, old_label, new_label) if update_linked else repo.dictionary_rename_preview(kind, entity_id)
        repo._change_log(kind, entity_id, "dictionary.updated", actor_id, old_values={"label": old_label}, new_values={"label": new_label, "is_active": is_active, "update_linked": update_linked, "updated_counts": counts}, summary=f"{old_label} → {new_label}; linked update: {'yes' if update_linked else 'no'}; counts: {counts}")
        repo.conn.commit()
        return f"/admin/dictionaries?section={kind}"
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
            if path == "/hlr/config/daily-limit":
                if current_role_key() != "admin":
                    raise ForbiddenError()
                try:
                    save_hlr_daily_limit_override(parsed.get("daily_limit_override", ""))
                    start_response("200 OK", html_headers())
                    return [hlr_page(notice_message="Дневной лимит HLR сохранён.")]
                except BusinessRuleError as exc:
                    start_response("400 Bad Request", html_headers())
                    return [hlr_page(error=user_error(exc))]
            if path == "/hlr/config/daily-limit/reset":
                if current_role_key() != "admin":
                    raise ForbiddenError()
                reset_hlr_daily_limit_override()
                start_response("200 OK", html_headers())
                return [hlr_page(notice_message="Дневной лимит HLR сброшен к значению из env.")]
            if path == "/hlr/check":
                try:
                    results, summary = hlr_run_check(parsed.get("numbers", ""))
                    start_response("200 OK", html_headers())
                    return [hlr_page(parsed.get("numbers", ""), results, summary)]
                except BusinessRuleError as exc:
                    start_response("400 Bad Request", html_headers())
                    return [hlr_page(parsed.get("numbers", ""), error=user_error(exc))]
            if path == "/hlr/balance":
                start_response("200 OK", html_headers())
                return [hlr_page(balance=get_hlr_balance())]
            if path == "/hlr/export.csv":
                require_permission("export", "hlr")
                try:
                    results = json.loads(parsed.get("results_json", "[]"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    results = []
                if not isinstance(results, list):
                    results = []
                start_response("200 OK", csv_headers("hlr_results.csv"))
                try:
                    selected_statuses = json.loads(parsed.get("selected_statuses_json", "[]"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    selected_statuses = []
                if not isinstance(selected_statuses, list):
                    selected_statuses = []
                show_all_statuses = str(parsed.get("show_all_statuses", "1")).lower() in {"1", "true", "yes", "on"}
                safe_results = hlr_filter_results_for_export(results, selected_statuses, show_all_statuses)
                headers, _ = hlr_csv_headers_and_keys(safe_results)
                return [csv_response("hlr_results.csv", headers, hlr_results_rows(safe_results))]
            if path == "/admin/import/preview":
                if parsed.get("mode") == "replace_section":
                    raise BusinessRuleError("Режим замены раздела временно отключён. Используйте Дополнить / обновить.")
                preview = preview_import(conn, parsed["entity_type"], parsed.get("csv_data", ""))
                if parsed["entity_type"] == "phone_numbers":
                    rows = "".join(f"<tr><td>{r['line']}</td><td>{esc(r.get('number', ''))}</td><td>{esc(r['action'])}</td><td>{esc(r.get('working_status', ''))}</td><td>{esc(r.get('active_provider', ''))}</td><td>{esc(r.get('review_required', ''))}</td><td>{esc(r.get('review_reasons', ''))}</td><td>{esc(r.get('errors', ''))}</td><td>{esc(r.get('info', ''))}</td><td>{esc(r['message'])}</td></tr>" for r in preview.rows)
                    info_blocks = ""
                    if preview.error_rows:
                        info_blocks += "<p class='flash error'>Есть ошибки. Импорт заблокирован до исправления файла.</p>"
                    if preview.legacy_info_rows:
                        info_blocks += "<p class='muted'>В файле есть исторические справочные значения. Они будут импортированы.</p>"
                    if preview.review_required_rows:
                        info_blocks += "<p class='muted'>Часть номеров будет импортирована со статусом ‘Требует проверки’.</p>"
                    duplicate_in_file_rows = sum(1 for r in preview.rows if r.get("action") == "duplicate_in_file")
                    update_rows = sum(1 for r in preview.rows if r.get("action") == "update")
                    summary = f"Всего: {preview.total_rows}, будет создано: {preview.new_rows}, будет обновлено: {update_rows}, дублей внутри файла: {duplicate_in_file_rows}, ошибок: {preview.error_rows}, legacy/info значений: {preview.legacy_info_rows}, требуют проверки: {preview.review_required_rows}"
                    html_preview = f"<h2>Предпросмотр</h2>{info_blocks}<p>{summary}</p><table><tr><th>Строка</th><th>Номер</th><th>Действие</th><th>Рабочий статус</th><th>Активен у провайдера</th><th>Требует проверки</th><th>Причины проверки</th><th>Errors</th><th>Info / legacy</th><th>Сообщение</th></tr>{rows}</table>"
                else:
                    rows = "".join(f"<tr><td>{r['line']}</td><td>{esc(r['status'])}</td><td>{esc(r['action'])}</td><td>{esc(r['message'])}</td></tr>" for r in preview.rows)
                    html_preview = f"<h2>Предпросмотр</h2><p>Всего: {preview.total_rows}, новых: {preview.new_rows}, дублей: {preview.duplicate_rows}, ошибок: {preview.error_rows}</p><table><tr><th>Строка</th><th>Статус</th><th>Действие</th><th>Комментарий</th></tr>{rows}</table>"
                start_response("200 OK", html_headers())
                return [import_page(repo, html_preview, selected_entity=parsed["entity_type"], selected_mode=parsed.get("mode", "append_update"), csv_data=parsed.get("csv_data", ""))]
            if path == "/admin/import/apply":
                result = apply_import(conn, parsed["entity_type"], parsed.get("csv_data", ""), user_id=current_actor_id(), mode=parsed.get("mode", "append_update"))
                extra = ""
                if parsed["entity_type"] == "phone_numbers":
                    extra = f"<li>требуют проверки {result.review_required_rows}</li><li>исторических справочных значений {result.legacy_info_rows}</li>"
                warning = "<p class='flash warning'>Во время применения возникли ошибки. Проверьте данные.</p>" if result.error_rows else ""
                notice = f"<h2>Импорт завершён</h2>{warning}<ul><li>создано {result.created_rows}</li><li>обновлено {result.updated_rows}</li><li>пропущено {result.skipped_rows}</li><li>ошибок {result.error_rows}</li>{extra}</ul>"
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
        if path == "/admin/import/template":
            if q.get("entity") != "phone_numbers":
                start_response("404 Not Found", html_headers()); return [page("404", "<h1>404</h1>")]
            start_response("200 OK", csv_headers("phone_numbers_import_template.csv"))
            return [phone_import_template_csv()]
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
        elif path == "/hlr": response = hlr_page()
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
    hlr_log_startup_config()
    port = int(os.environ.get("PORT", "8000"))
    with make_server("0.0.0.0", port, app) as httpd:
        print(f"Serving on http://127.0.0.1:{port}")
        httpd.serve_forever()
