from __future__ import annotations

import html
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from app.db import DEFAULT_DB_PATH, connect, init_db
from app.importer import apply_import, preview_import
from app.repository import BusinessRuleError, COMPANY_CHANGE_LABELS, ROUTING_SCOPE_LABELS, Repository, normalize_provider_name, validate_phone_number

DB_PATH = Path(os.environ.get("MVP_DB_PATH", DEFAULT_DB_PATH))
ADMIN_ID = 1
STATUS_LABELS = {
    "used": "Используется",
    "free": "Свободен",
    "disabled": "Отключён",
    "reserved": "Резерв",
    "blocked": "Заблокирован",
    "unknown": "Неизвестно",
}
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




NAV_ITEMS = [
    ("routes", "/routes", "Маршруты", ("Маршруты", "Номера маршрута", "Редактировать маршрут")),
    ("tariffs", "/tariffs", "Тарифы", ("Тарифы",)),
    ("phones", "/phones", "Купленные номера", ("Купленные номера", "Редактировать номер")),
    ("companies", "/companies", "Кампании прозвона", ("Кампании прозвона", "Редактировать кампанию")),
    ("provider-changes", "/provider-changes", "Смена провайдеров", ("Смена провайдеров", "Редактировать событие")),
]

ADMIN_NAV_ITEMS = [
    ("/admin/server-priorities", "Приоритет по серверам", ("Приоритет по серверам",)),
    ("/admin/company-routing-settings", "Схема маршрутизации кампаний", ("Схема маршрутизации кампаний",)),
    ("/admin/naming-rules", "Правила нейминга маршрутов", ("Правила нейминга",)),
    ("/admin/import", "Импорт / экспорт", ("Импорт",)),
    ("/admin/currency-rates", "Курсы валют", ("Курсы валют",)),
    ("/admin/change-reasons", "Причины смены провайдера", ("Причины смены провайдера",)),
    ("/admin/dictionaries", "Справочные значения", ("Справочные значения",)),
    ("/admin/change-log", "Change log", ("Change log",)),
    ("/companies", "Кампании прозвона", ()),
]


def active_nav(title: str) -> tuple[str, str | None]:
    for key, _, _, titles in NAV_ITEMS:
        if title in titles:
            return key, None
    for href, _, titles in ADMIN_NAV_ITEMS:
        if title in titles:
            return "admin", href
    if title == "Администрирование":
        return "admin", None
    return "", None


def sidebar(title: str) -> str:
    active_key, active_admin_href = active_nav(title)
    admin_open = active_key == "admin"
    main_links = "".join(
        f"<a class='side-link {'active' if active_key == key else ''}' href='{href}'>{label}</a>"
        for key, href, label, _ in NAV_ITEMS
    )
    admin_links = "".join(
        f"<a class='admin-link {'active' if active_admin_href == href else ''}' href='{href}'>{label}</a>"
        for href, label, _ in ADMIN_NAV_ITEMS
    )
    return f"""
  <aside class="sidebar">
    <div class="app-title">MVP маршрутов</div>
    <nav class="side-nav" aria-label="Основная навигация">
      {main_links}
      <button class="side-link admin-toggle {'active' if admin_open else ''}" type="button" aria-expanded="{'true' if admin_open else 'false'}" aria-controls="admin-nav">Администрирование</button>
      <div class="admin-tree {'open' if admin_open else ''}" id="admin-nav">
        {admin_links}
      </div>
    </nav>
  </aside>"""


def page(title: str, body: str, notice: str | None = None) -> bytes:
    notice_html = f"<div class='ok'>{esc(notice)}</div>" if notice else ""
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>{esc(title)}</title>
  <style>
    :root {{
      --bg: #f6f8f5;
      --surface: #ffffff;
      --surface-muted: #f8faf7;
      --surface-strong: #edf3ee;
      --sidebar-bg: #eef3ef;
      --text: #2b3030;
      --text-strong: #161a1a;
      --muted: #687272;
      --border: #dce5de;
      --border-strong: #c7d4ca;
      --accent: #5f7f6f;
      --accent-strong: #3f6353;
      --accent-soft: #eaf3ed;
      --danger: #9a3f38;
      --danger-soft: #f7e8e5;
      --success: #476a55;
      --success-soft: #e8f2ea;
      --warning: #8a5f45;
      --focus: #6f917f;
      --shadow-soft: 0 1px 2px rgba(34, 48, 42, 0.06);
      --shadow-card: 0 8px 24px rgba(34, 48, 42, 0.05);
      --radius-control: 6px;
      --radius-card: 9px;
    }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: var(--text); background: var(--bg); font-size: 14px; line-height: 1.45; }}
    h1 {{ margin: 0 0 14px; font-size: 26px; line-height: 1.18; letter-spacing: -0.02em; color: var(--text-strong); font-weight: 760; }}
    h2 {{ margin: 18px 0 10px; font-size: 18px; line-height: 1.25; letter-spacing: -0.01em; color: var(--text-strong); font-weight: 740; }}
    h3 {{ margin: 14px 0 8px; font-size: 15px; letter-spacing: -0.005em; color: var(--text-strong); font-weight: 720; }}
    p {{ margin: 8px 0; }}
    .app-shell {{ display: grid; grid-template-columns: 258px minmax(0, 1fr); min-height: 100vh; }}
    .sidebar {{ background: var(--sidebar-bg); border-right: 1px solid var(--border); padding: 18px 14px; position: sticky; top: 0; height: 100vh; overflow-y: auto; }}
    .app-title {{ color: var(--text-strong); font-weight: 820; font-size: 17px; letter-spacing: -0.01em; margin: 2px 8px 18px; }}
    .side-nav {{ display: grid; gap: 4px; }}
    .side-link, .admin-link, .button, button {{ border: 1px solid transparent; border-radius: var(--radius-control); color: var(--text); padding: 7px 10px; text-decoration: none; background: transparent; cursor: pointer; font: inherit; transition: background-color 120ms ease, border-color 120ms ease, color 120ms ease, box-shadow 120ms ease; }}
    .side-link {{ display: flex; width: 100%; align-items: center; justify-content: space-between; text-align: left; font-weight: 650; }}
    .side-link:hover, .admin-link:hover {{ background: rgba(255, 255, 255, 0.7); border-color: var(--border); color: var(--text-strong); }}
    .side-link.active {{ background: var(--surface); border-color: var(--border-strong); color: var(--accent-strong); box-shadow: var(--shadow-soft); }}
    .admin-toggle::after {{ content: "›"; color: var(--muted); font-size: 14px; line-height: 1; }}
    .admin-toggle[aria-expanded="true"]::after {{ content: "⌄"; }}
    .admin-tree {{ display: none; margin: 3px 0 7px 13px; padding: 4px 0 4px 12px; border-left: 1px solid var(--border-strong); }}
    .admin-tree.open {{ display: grid; gap: 2px; }}
    .admin-link {{ display: block; padding: 6px 8px; font-size: 13px; line-height: 1.25; color: #44504a; }}
    .admin-link.active {{ background: var(--surface); border-color: var(--border-strong); color: var(--accent-strong); font-weight: 730; box-shadow: var(--shadow-soft); }}
    .workspace {{ min-width: 0; padding: 22px 26px 38px; }}
    .content {{ max-width: 1460px; margin: 0 auto; }}
    a {{ color: #426a5a; text-underline-offset: 2px; }}
    a:hover {{ color: var(--accent-strong); }}
    .button, button {{ background: var(--surface); border-color: var(--border-strong); color: var(--text-strong); min-height: 30px; display: inline-flex; align-items: center; justify-content: center; gap: 5px; font-weight: 650; box-shadow: 0 1px 0 rgba(34, 48, 42, 0.03); }}
    .admin-toggle {{ background: transparent; border-color: transparent; box-shadow: none; }}
    .button:hover, button:hover {{ background: var(--surface-muted); border-color: var(--accent); }}
    .button:active, button:active {{ background: var(--surface-strong); }}
    .button:disabled, button:disabled, input:disabled, select:disabled, textarea:disabled {{ opacity: 0.62; cursor: not-allowed; }}
    button[onclick*="Деактив"], button[onclick*="Удал"], button[onclick*="Отключ"], form[action$="/deactivate"] button {{ color: var(--danger); border-color: #dfbbb6; background: var(--danger-soft); }}
    button[onclick*="Деактив"]:hover, button[onclick*="Удал"]:hover, button[onclick*="Отключ"]:hover, form[action$="/deactivate"] button:hover {{ background: #f2d9d5; border-color: #c9938b; }}
    .button:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, summary:focus-visible, a:focus-visible {{ outline: 2px solid var(--focus); outline-offset: 2px; }}
    table {{ border-collapse: separate; border-spacing: 0; width: 100%; background: var(--surface); min-width: 760px; }}
    th, td {{ border: 0; border-bottom: 1px solid #e5ebe6; padding: 7px 9px; vertical-align: top; }}
    tr:last-child td {{ border-bottom: 0; }}
    th {{ background: var(--surface-strong); text-align: left; font-weight: 750; color: #44504a; position: sticky; top: 0; z-index: 1; }}
    tbody tr:nth-child(even) {{ background: #fbfcfb; }}
    tbody tr:hover {{ background: var(--accent-soft); }}
    td {{ max-width: 360px; overflow-wrap: anywhere; }}
    input, select, textarea {{ border: 1px solid var(--border-strong); border-radius: var(--radius-control); padding: 6px 8px; margin: 0; max-width: 100%; background: var(--surface); color: var(--text-strong); font: inherit; min-height: 32px; box-shadow: inset 0 1px 1px rgba(34, 48, 42, 0.03); }}
    input:hover, select:hover, textarea:hover {{ border-color: var(--accent); }}
    input:focus, select:focus, textarea:focus {{ border-color: var(--focus); background: #ffffff; }}
    input::placeholder, textarea::placeholder {{ color: #95a09a; }}
    textarea {{ width: 100%; }}
    input[type="checkbox"], input[type="radio"] {{ width: auto; margin: 0 6px 0 0; vertical-align: middle; accent-color: var(--accent); }}
    label {{ display: inline-grid; gap: 4px; margin: 0; align-items: start; color: #44504a; font-weight: 620; }}
    form {{ display: flex; flex-wrap: wrap; gap: 8px 10px; align-items: end; }}
    form button {{ align-self: end; }}
    .checkbox-list {{ display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 4px 0; }}
    .checkbox-list label {{ margin: 0; font-weight: 520; }}
    .server-checkbox-toolbar {{ display: flex; gap: 8px; margin: 0 0 8px; }}
    .server-checkbox-toolbar button {{ padding: 3px 8px; font-size: 0.9em; }}
    .server-checkbox-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 6px 10px; margin-top: 6px; }}
    .server-checkbox-item {{ display: flex; align-items: flex-start; gap: 6px; border: 1px solid var(--border); border-radius: var(--radius-control); padding: 6px 8px; background: var(--surface); margin: 0; font-weight: 520; }}
    .server-checkbox-item:has(input:checked) {{ border-color: var(--accent); background: var(--accent-soft); box-shadow: 0 0 0 1px #d3e1d7 inset; }}
    .server-checkbox-main {{ font-weight: 720; }}
    .server-route-hint {{ display: block; margin-top: 2px; font-size: 0.9em; color: var(--muted); line-height: 1.25; }}
    .event-server-list {{ margin: 4px 0 0 18px; padding: 0; }}
    .event-server-list li {{ margin: 2px 0; }}
    fieldset {{ border: 1px solid var(--border); border-radius: var(--radius-card); margin: 12px 0; padding: 12px; background: var(--surface); }}
    fieldset > legend {{ padding: 0 6px; color: #52605a; font-weight: 750; }}
    h1 + fieldset, h1 + p + fieldset {{ margin-top: 6px; }}
    .required {{ color: var(--danger); font-weight: 760; }}
    .muted {{ color: var(--muted); font-weight: 500; }}
    .error {{ border: 1px solid #c8796f; background: var(--danger-soft); color: #71322d; padding: 12px; border-radius: var(--radius-card); }}
    .ok {{ border: 1px solid #abc6b2; background: var(--success-soft); color: #345541; padding: 12px; border-radius: var(--radius-card); margin: 10px 0; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }}
    .card {{ border: 1px solid var(--border); border-radius: var(--radius-card); padding: 12px; background: var(--surface); box-shadow: var(--shadow-soft); }}
    details {{ border: 1px solid var(--border); border-radius: var(--radius-card); padding: 0; margin: 12px 0; background: var(--surface); box-shadow: var(--shadow-soft); }}
    summary {{ cursor: pointer; padding: 8px 12px; font-weight: 750; color: var(--text-strong); }}
    details[open] > summary {{ border-bottom: 1px solid #e6ece7; background: var(--surface-muted); border-radius: var(--radius-card) var(--radius-card) 0 0; }}
    details > form, details > .card, details > textarea, details > p, details > table {{ margin: 12px; }}
    .filter-card, .form-card {{ border-color: var(--border); box-shadow: var(--shadow-soft); }}
    .filter-card {{ margin: 8px 0 10px; }}
    .filter-summary, .form-summary {{ min-height: 34px; display: flex; align-items: center; justify-content: space-between; }}
    .filter-grid, .form-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, max-content)); gap: 8px 10px; align-items: end; padding: 12px; }}
    .filter-grid label, .form-grid label {{ min-width: 150px; }}
    .filter-grid input, .filter-grid select, .form-grid input, .form-grid select {{ width: 100%; }}
    .filter-grid .checkbox-inline, .form-grid .checkbox-inline {{ min-width: auto; display: flex; align-items: center; gap: 5px; align-self: center; font-weight: 560; }}
    .form-grid .wide, .filter-grid .wide {{ grid-column: 1 / -1; }}
    .form-grid fieldset, .filter-grid fieldset {{ grid-column: 1 / -1; margin: 0; }}
    .form-grid textarea {{ min-width: min(620px, 100%); }}
    .table-card, .journal-card {{ border: 1px solid var(--border); border-radius: var(--radius-card); background: var(--surface); margin: 12px 0; overflow: hidden; box-shadow: var(--shadow-card); }}
    .table-card h2, .journal-card h2 {{ margin: 0; padding: 12px 14px; border-bottom: 1px solid #e6ece7; background: var(--surface-muted); color: var(--text-strong); }}
    .journal-card h2 {{ font-size: 19px; }}
    .table-scroll {{ overflow-x: auto; }}
    .table-card table, .journal-card table {{ margin: 0; border: 0; border-radius: 0; }}
    .journal-card {{ min-height: 420px; border-color: var(--border-strong); }}
    .journal-card .table-scroll {{ min-height: 360px; }}
    .empty-state {{ padding: 24px 14px; color: var(--muted); background: #fbfcfb; }}
    .compact-actions, .actions {{ white-space: nowrap; min-width: 130px; }}
    .actions .button, .actions button, .compact-actions .button, .compact-actions button {{ min-height: 28px; padding: 4px 8px; font-size: 12px; }}
    .actions details, .compact-actions details {{ margin: 6px 0 0; box-shadow: none; }}
    .actions summary, .compact-actions summary {{ padding: 4px 7px; font-size: 12px; }}
    .scope-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px; }}
    .scope-card {{ cursor: pointer; display: block; box-shadow: none; }}
    .scope-card.selected {{ border-color: var(--accent); background: var(--accent-soft); box-shadow: 0 0 0 2px #d3e1d7 inset; }}
    .scope-field[hidden], .conditional-field[hidden], .route-empty-message[hidden] {{ display: none !important; }}
    .current-route-box {{ display: block; border: 1px dashed var(--border-strong); border-radius: var(--radius-card); padding: 8px; margin: 4px 12px 4px 0; background: var(--surface-muted); }}
    .star {{ color: var(--warning); font-weight: 800; }}
    .dictionary-layout {{ display: grid; grid-template-columns: minmax(220px, 20%) 1fr; gap: 18px; align-items: start; }}
    .dictionary-sidebar {{ display: grid; gap: 10px; }}
    .dictionary-card {{ border: 1px solid var(--border); border-radius: var(--radius-card); padding: 10px; background: var(--surface); box-shadow: var(--shadow-soft); }}
    .dictionary-card.active {{ border-color: var(--accent); background: var(--accent-soft); box-shadow: 0 0 0 2px #d3e1d7 inset; }}
    .dictionary-card-title {{ display: block; font-weight: 780; color: var(--text-strong); text-decoration: none; margin-bottom: 8px; }}
    .dictionary-card form {{ display: grid; gap: 6px; }}
    .dictionary-card input, .dictionary-card select {{ width: 100%; box-sizing: border-box; margin: 0; }}
    .dictionary-workspace {{ min-width: 0; }}
    .dictionary-toolbar {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; border: 1px solid var(--border); border-radius: var(--radius-card); padding: 10px 12px; background: var(--surface); box-shadow: var(--shadow-soft); }}
    .dictionary-toolbar h2 {{ margin: 0; }}
    .inactive-row {{ color: var(--muted); background: #f0f4f1; }}
    .status-badge {{ display: inline-flex; align-items: center; min-height: 22px; padding: 2px 7px; border: 1px solid var(--border); border-radius: 999px; background: var(--surface-muted); color: #4d5a54; font-size: 12px; font-weight: 720; white-space: nowrap; }}
    @media (max-width: 900px) {{
      .app-shell {{ grid-template-columns: 1fr; }}
      .sidebar {{ position: static; height: auto; }}
      .workspace {{ padding: 18px 14px 28px; }}
    }}
  </style>
</head>
<body>
  <div class="app-shell">
    {sidebar(title)}
    <main class="workspace">
      <div class="content">
        {notice_html}
        {body}
      </div>
    </main>
  </div>
  <script>
    document.querySelectorAll(".admin-toggle").forEach((button) => {{
      button.addEventListener("click", () => {{
        const target = document.getElementById(button.getAttribute("aria-controls"));
        const expanded = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", expanded ? "false" : "true");
        target.classList.toggle("open", !expanded);
      }});
    }});
  </script>
</body>
</html>""".encode("utf-8")

def redirect(start_response, location: str):
    start_response("303 See Other", [("Location", location)])
    return [b""]


def request_query(environ) -> dict[str, str]:
    return {key: values[-1] for key, values in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}


def active_query(q: dict[str, str], keys: list[str] | tuple[str, ...]) -> bool:
    return any(q.get(key) not in (None, "") for key in keys)


def filter_card(form_html: str, q: dict[str, str], keys: list[str] | tuple[str, ...]) -> str:
    open_attr = " open" if active_query(q, keys) else ""
    return f"<details class='filter-card'{open_attr}><summary class='filter-summary'>Фильтры</summary>{form_html}</details>"


def form_card(summary: str, form_html: str, *, open_by_default: bool = False) -> str:
    open_attr = " open" if open_by_default else ""
    return f"<details class='form-card'{open_attr}><summary class='form-summary'>{summary}</summary>{form_html}</details>"


def table_card(table_html: str, *, title: str | None = None, extra_class: str = "") -> str:
    title_html = f"<h2>{esc(title)}</h2>" if title else ""
    classes = f"table-card {extra_class}".strip()
    return f"<section class='{classes}'>{title_html}<div class='table-scroll'>{table_html}</div></section>"


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
            f"<label class='server-checkbox-item'><input type='checkbox' name='server_ids' value='{row['id']}' {checked}> "
            f"<span><span class='server-checkbox-main'>{esc(row['name'])}</span> "
            f"<span class='server-route-hint' data-current-route-hint data-server-id='{row['id']}'>текущий: {esc(hint)}</span></span></label>"
        )
    return (
        "<div class='server-checkbox-toolbar'>"
        "<button type='button' data-server-select='all'>Выбрать все</button>"
        "<button type='button' data-server-select='none'>Снять все</button>"
        "</div><div class='server-checkbox-grid'>"
        + "".join(boxes)
        + "</div>"
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


def routes_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    filters = {"country_id": q.get("country_id"), "provider_id": q.get("provider_id"), "prefix_id": q.get("prefix_id"), "is_actual": q.get("is_actual"), "search_like": q.get("search")}
    rows = []
    for route in repo.list_routes(filters):
        prefix = route["prefix"] or "Без префикса"
        numbers = f'{route["phone_count"]} номеров <a class="button" href="/routes/{route["id"]}/numbers">Показать номера</a>' if route["cli_source_type"] in {"pool", "sim"} else ("RND провайдера" if route["cli_source_type"] == "rnd" else "—")
        edit = f"<a class='button' href='/routes/{route['id']}/edit'>✏️ Редактировать</a>"
        rows.append(f"<tr><td>{esc(route['country_name'])}</td><td>{esc(route['name'])}</td><td>{esc(route['provider_name'])}</td><td>{esc(prefix)}</td><td>{'Да' if route['is_actual'] else 'Нет'}</td><td>{esc(route['comment'])}</td><td>{numbers}</td><td class='actions'>{edit}</td></tr>")
    filters_html = f"""<form class="filter-grid" method="get" action="/routes">
<label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Провайдер <select name="provider_id">{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
<label>Префикс <select name="prefix_id">{prefix_options(repo, selected=q.get('prefix_id'), empty='Все')}</select></label>
<label>Актуальный <select name="is_actual"><option value="">Все</option><option value="1" {'selected' if q.get('is_actual')=='1' else ''}>Да</option><option value="0" {'selected' if q.get('is_actual')=='0' else ''}>Нет</option></select></label>
<label>Поиск <input name="search" value="{esc(q.get('search'))}"></label><button>Поиск</button></form>"""
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
    table_html = f"<table><thead><tr><th>ГЕО</th><th>Название маршрута</th><th>Провайдер</th><th>Префикс</th><th>Актуальный</th><th>Комментарий</th><th>Номера</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    body = f"""
<h1>Маршруты</h1>
{filter_card(filters_html, q, ('country_id', 'provider_id', 'prefix_id', 'is_actual', 'search'))}
{form_card('+ Добавить маршрут <span class="muted">Admin</span>', create_html)}
{table_card(table_html)}
"""
    return page("Маршруты", body)


def route_numbers_page(repo: Repository, route_id: int, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    route = repo.conn.execute("SELECT name FROM routes WHERE id = ?", (route_id,)).fetchone()
    if route is None:
        return page("Не найдено", "<h1>Маршрут не найден</h1>")
    numbers = repo.route_numbers(route_id)
    rows = []
    for phone in numbers:
        cost = f"Подкл: {phone['connection_cost'] or '—'} / Абон: {phone['monthly_fee'] or '—'} / Исх: {phone['outgoing_rate'] or '—'} / Вх: {phone['incoming_rate'] or '—'}"
        rows.append(f"<tr><td><input type='checkbox' name='link_ids' value='{phone['link_id']}'></td><td>{esc(phone['number'])}</td><td>{esc(STATUS_LABELS.get(phone['status'], phone['status']))}</td><td>{esc(ASSIGNMENT_LABELS.get(phone['assignment_type'], phone['assignment_type']))}</td><td>{esc(cost)}</td><td>{esc(phone['link_comment'] or phone['phone_comment'])}</td></tr>")
    all_numbers = chr(10).join([p["number"] for p in numbers])
    body = f"""
<h1>Номера в маршруте: {esc(route['name'])}</h1><p><a href="/routes">← Назад</a></p>
<div class="grid"><div class="card"><h2>Скопировать все</h2><textarea rows="7" cols="40" readonly>{esc(all_numbers)}</textarea></div>
<div class="card"><h2>+ Добавить номер <span class="muted">Admin</span></h2><form method="post" action="/routes/{route_id}/numbers/add"><label>Номер телефона <span class="required">*</span><input name="phone_number"></label><label>Назначение <span class="required">*</span><input name="usage_type" value="pool_member"></label><label>Комментарий <input name="comment"></label><button>Добавить</button></form></div>
<div class="card"><h2>Массовое добавление</h2><form method="post" action="/routes/{route_id}/numbers/bulk-add"><textarea name="phone_numbers" rows="7" cols="40" placeholder="по одному номеру в строке"></textarea><br><button>Добавить список</button></form></div></div>
<form method="post" action="/routes/{route_id}/numbers/remove"><label>Причина <input name="reason"></label><button onclick="return confirm('Исключить выбранные номера из маршрута?')">Исключить из маршрута</button>
<table><thead><tr><th></th><th>Номер</th><th>Статус</th><th>Назначение</th><th>Стоимость</th><th>Комментарий</th></tr></thead><tbody>{''.join(rows)}</tbody></table></form>
"""
    return page("Номера маршрута", body, q.get("notice"))

def tariffs_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    rows = []
    for t in repo.list_tariffs({"country_id": q.get("country_id"), "provider_id": q.get("provider_id"), "priority_status": q.get("priority_status"), "status": q.get("status", "active")}):
        prefix = t["prefix"] or "Без префикса"
        rows.append(f"""<tr><td>{esc(t['country_name'])}</td><td>{esc(t['provider_name'])}</td><td>{esc(prefix)}</td><td>{esc(t['price_in_provider_currency'])} {esc(t['currency_code'])}</td><td>{esc(t['eur_price'])} EUR</td><td>{esc(t['priority_status'])}</td><td>{'Да' if t['is_current'] else 'Нет'}</td><td>{esc(t['comment'])}</td><td><form method='post' action='/tariffs/{t['id']}/deactivate'><button onclick="return confirm('Деактивировать тариф?')">⛔ Деактивировать</button></form></td></tr>""")
    filters_html = f"""<form class="filter-grid" method="get" action="/tariffs">
<label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Провайдер <select name="provider_id">{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
<label>Приоритет <select name="priority_status"><option value="">Все</option><option value="priority" {'selected' if q.get('priority_status')=='priority' else ''}>priority</option><option value="alternative" {'selected' if q.get('priority_status')=='alternative' else ''}>alternative</option><option value="unknown" {'selected' if q.get('priority_status')=='unknown' else ''}>unknown</option></select></label>
<label>Статус <select name="status"><option value="all" {'selected' if q.get('status')=='all' else ''}>Все</option><option value="active" {'selected' if q.get('status','active')=='active' else ''}>Активные</option><option value="inactive" {'selected' if q.get('status')=='inactive' else ''}>Неактивные</option></select></label><button>Поиск</button></form>"""
    create_html = f"""<form class="form-grid" method="post" action="/tariffs/create">
<label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label>
<label>Провайдер <span class="required">*</span><select name="provider_id">{active_options(repo, 'providers')}</select></label>
<label>Префикс <span class="required">*</span><select name="provider_prefix_id">{prefix_options(repo)}</select></label>
<label>Валюта <span class="required">*</span><select name="currency_id">{active_options(repo, 'currencies', 'code')}</select></label>
<label>Цена <span class="required">*</span><input name="price"></label>
<label>Приоритет <span class="required">*</span><select name="priority_status"><option value="priority">priority</option><option value="alternative">alternative</option><option value="unknown">unknown</option></select></label>
<label>Активный <span class="required">*</span><select name="is_current"><option value="1">Да</option><option value="0">Нет</option></select></label>
<label>Комментарий <input name="comment"></label><p class="muted wide">Курс к EUR и дата курса берутся из Администрирование → Курсы валют.</p><button>Сохранить</button></form>"""
    table_html = f"<table><thead><tr><th>ГЕО</th><th>Провайдер</th><th>Префикс</th><th>Цена провайдера</th><th>Цена EUR</th><th>Приоритет</th><th>Активный</th><th>Инфо</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    body = f"""
<h1>Тарифы</h1>
{filter_card(filters_html, q, ('country_id', 'provider_id', 'priority_status', 'status'))}
{form_card('+ Добавить тариф <span class="muted">Admin</span>', create_html)}
{table_card(table_html)}"""
    return page("Тарифы", body)


def phones_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    rows = []
    for phone in repo.list_phone_numbers({"country_id": q.get("country_id"), "provider_id": q.get("provider_id"), "project": q.get("project"), "assignment_type": q.get("assignment_type"), "status": q.get("status"), "number_like": q.get("number")}):
        assignment_label = phone["assignment_type_label"] or ASSIGNMENT_LABELS.get(phone["assignment_type"], phone["assignment_type"])
        rows.append(f"""<tr><td>{esc(phone['number'])}</td><td>{esc(phone['country_name'])}</td><td>{esc(phone['provider_name'])}</td><td>{esc(phone['project_label'])}</td><td>{esc(assignment_label)}</td><td>{esc(STATUS_LABELS.get(phone['status'], phone['status']))}</td><td>{'Да' if phone['is_active'] else 'Нет'}</td><td>{phone['route_count']}</td><td>{esc(phone['connection_cost'])}</td><td>{esc(phone['monthly_fee'])}</td><td>{esc(phone['currency_code'])}</td><td>{esc(phone['phone_type'])}</td><td>{esc(phone['tariff_label'])}</td><td>{esc(phone['created_at'])}</td><td>{esc(phone['updated_at'])}</td><td>{esc(phone['deactivated_at'])}</td><td>{esc(phone['comment'])}</td><td><a class='button' href='/phones/{phone['id']}/edit'>✏️ Редактировать</a></td></tr>""")
    filters_html = f"""<form class="filter-grid" method="get" action="/phones">
<label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Провайдер <select name="provider_id">{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
    <label>Проект <select name="project">{project_options(repo, selected=q.get('project'), empty='Все')}</select></label>
    <label>Назначение <select name="assignment_type">{assignment_options(repo, selected=q.get('assignment_type'), empty='Все')}</select></label>
<label>Статус <select name="status"><option value="">Все</option><option value="used">Используется</option><option value="free">Свободен</option><option value="disabled">Отключён</option><option value="blocked">Заблокирован</option></select></label>
<label>Поиск по номеру <input name="number" value="{esc(q.get('number'))}"></label><button>Поиск</button></form>"""
    create_html = f"""<form class="form-grid" method="post" action="/phones/create">
<label>Номер <span class="required">*</span><input name="number" placeholder="393331234567"></label><label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label><label>Провайдер <select name="provider_id"><option value="">—</option>{active_options(repo, 'providers')}</select></label><label>Проект <select name="project_label">{project_options(repo, empty='—')}</select></label><label>Назначение <span class="required">*</span><select name="assignment_type">{assignment_options(repo)}</select></label><label>Статус <span class="required">*</span><select name="status"><option value="used">Используется</option><option value="free">Свободен</option><option value="disabled">Отключён</option><option value="blocked">Заблокирован</option></select></label><label>Стоимость подключения <input name="connection_cost"></label><label>Абонентская плата <input name="monthly_fee"></label><label>Валюта <select name="currency_id"><option value="">—</option>{active_options(repo, 'currencies', 'code')}</select></label><label>Тип номера <select name="phone_type">{phone_type_options(repo, empty='—')}</select></label><label>Тариф <input name="tariff_label"></label><label>Комментарий <input name="comment"></label><button>Сохранить</button></form>"""
    table_html = f"<table><thead><tr><th>Номер</th><th>ГЕО</th><th>Провайдер</th><th>Проект</th><th>Назначение</th><th>Статус</th><th>Активен</th><th>Маршрутов</th><th>Подключение</th><th>Абонплата</th><th>Валюта</th><th>Тип номера</th><th>Тариф</th><th>Дата создания</th><th>Дата изменения</th><th>Дата отключения</th><th>Комментарий</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    body = f"""
<h1>Купленные номера</h1>
{filter_card(filters_html, q, ('country_id', 'provider_id', 'project', 'assignment_type', 'status', 'number'))}
{form_card('+ Добавить номер <span class="muted">Admin</span>', create_html)}
{table_card(table_html)}"""
    return page("Купленные номера", body)


def companies_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    rows = []
    for cc in repo.list_calling_companies({"server_id": q.get("server_id"), "country_id": q.get("country_id"), "company_like": q.get("company"), "external_id_like": q.get("external_id"), "has_autorotation": q.get("has_autorotation"), "is_active": q.get("is_active")}):
        rows.append(f"<tr><td>{esc(cc['server_name'])}</td><td>{esc(cc['country_name'])}</td><td>{esc(cc['company_name'])}</td><td>{esc(cc['company_id_external'])}</td><td>{esc(cc['line_count'])}</td><td>{esc(cc['dial_set_count'])}</td><td>{'Да' if cc['has_autorotation'] else 'Нет'}</td><td>{esc(cc['retry_interval_seconds'])}</td><td>{'Активна' if cc['is_active'] else 'Неактивна'}</td><td>{esc(cc['comment'])}</td><td><a class='button' href='/companies/{cc['id']}/edit'>✏️ Редактировать</a></td></tr>")
    filters_html = f"""<form class="filter-grid" method="get" action="/companies">
<label>Сервер <select name="server_id">{options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label><label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label><label>Название кампании <input name="company" value="{esc(q.get('company'))}"></label><label>ID кампании <input name="external_id" value="{esc(q.get('external_id'))}"></label><label>Авторотация <select name="has_autorotation"><option value="">Все</option><option value="1" {'selected' if q.get('has_autorotation')=='1' else ''}>Да</option><option value="0" {'selected' if q.get('has_autorotation')=='0' else ''}>Нет</option></select></label><label>Активность <select name="is_active"><option value="">Все</option><option value="1" {'selected' if q.get('is_active')=='1' else ''}>Активна</option><option value="0" {'selected' if q.get('is_active')=='0' else ''}>Неактивна</option></select></label><button>Поиск</button></form>"""
    create_html = f"""<form class="form-grid" method="post" action="/companies/create"><label>Сервер <span class="required">*</span><select name="server_id">{active_options(repo, 'servers')}</select></label><label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label><label>ID кампании <span class="required">*</span><input name="company_id_external"></label><label>Название кампании <span class="required">*</span><input name="company_name"></label><label>Количество линий <span class="required">*</span><input name="line_count" value="0"></label><label>Количество наборов <span class="required">*</span><input name="dial_set_count" value="0"></label><label>Авторотация <span class="required">*</span><select name="has_autorotation"><option value="1">Да</option><option value="0">Нет</option></select></label><label>Интервал дозвона, сек. <span class="required">*</span><input name="retry_interval_seconds" value="0"></label><label>Активна <span class="required">*</span><select name="is_active"><option value="1">Да</option><option value="0">Нет</option></select></label><label>Комментарий <input name="comment"></label><button>Сохранить</button></form>"""
    table_html = f"<table><thead><tr><th>Сервер</th><th>ГЕО</th><th>Название кампании</th><th>ID кампании</th><th>Количество линий</th><th>Количество наборов</th><th>Авторотация</th><th>Интервал между попытками дозвона (сек.)</th><th>Активна</th><th>Комментарий</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    body = f"""
<h1>Кампании прозвона</h1>
{filter_card(filters_html, q, ('server_id', 'country_id', 'company', 'external_id', 'has_autorotation', 'is_active'))}
{form_card('+ Добавить кампанию <span class="muted">Admin</span>', create_html)}
{table_card(table_html)}"""
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
      <label class='card scope-card'><input type='radio' name='apply_scope' value='none' {'checked' if scope == 'none' else ''}> Не меняли настройки в нашей системе</label>
      <label class='card scope-card'><input type='radio' name='apply_scope' value='server_priority' {'checked' if scope == 'server_priority' else ''}> Серверный приоритет</label>
      <label class='card scope-card'><input type='radio' name='apply_scope' value='campaign_setting' {'checked' if scope == 'campaign_setting' else ''}> Настройка кампании</label>
    </div>
  </fieldset>
  {inactive_note}
  <label>Дата события <span class='required'>*</span><input type='datetime-local' name='event_at' value='{esc(event_at)}' required></label>
  <label class='scope-field' data-scopes='none server_priority'>GEO <select name='country_id' id='event-country'>{active_options(repo, 'countries', selected=event['country_id'] if event else None, empty='—')}</select></label>
  <fieldset class='scope-field' data-scopes='server_priority'><legend>Серверы <span class='required'>*</span></legend>{server_priority_server_boxes}</fieldset>
  <span class='scope-field current-route-box' data-scopes='server_priority' id='current-route-box'>Текущий маршрут: —</span>
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
    form.querySelectorAll('[data-current-route-hint]').forEach((hint) => {{
      const key = `${{hintCountryId}}:${{hint.dataset.serverId}}`;
      hint.textContent = priorities[key] ? `текущий: ${{priorities[key]}}` : 'текущий: —';
    }});
    const currentBox = document.getElementById('current-route-box');
    if (currentBox) currentBox.textContent = hintCountryId ? 'Текущий маршрут показан рядом с каждым сервером.' : 'Выберите GEO, чтобы увидеть текущие маршруты серверов.';
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
  form.querySelectorAll('[data-server-select]').forEach((button) => button.addEventListener('click', () => {{
    const checked = button.dataset.serverSelect === 'all';
    form.querySelectorAll('input[name="server_ids"]').forEach((box) => {{ box.checked = checked; }});
  }}));
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
    rows = []
    for ev in repo.list_routing_events({"country_id": q.get("country_id"), "apply_scope": q.get("apply_scope"), "server_id": q.get("server_id"), "campaign_id": q.get("campaign_id"), "provider_id": q.get("provider_id"), "include_inactive": q.get("include_inactive") == "1"}):
        server_text, campaign_text, details_text = provider_event_details(ev)
        actions = f"<a class='button' href='/provider-changes/{ev['id']}/edit'>Редактировать</a>"
        if ev["is_active"]:
            actions += f"<details><summary>Деактивировать</summary><form method='post' action='/provider-changes/{ev['id']}/deactivate'><label>Причина <span class='required'>*</span><input name='deactivation_reason' required></label><button>Деактивировать</button></form></details>"
        rows.append(f"<tr class='{'' if ev['is_active'] else 'inactive-row'}'><td>{esc(ev['event_at'])}</td><td>{esc(ROUTING_SCOPE_LABELS.get(ev['apply_scope'], ev['apply_scope']))}</td><td>{esc(ev['country_name'])}</td><td>{esc(server_text)}</td><td>{esc(campaign_text)}</td><td>{details_text}</td><td>{esc(ev['reason'])}</td><td>{esc(ev['comment'])}</td><td>{'Да' if ev['is_active'] else 'Нет'}</td><td class='actions'>{actions}</td></tr>")
    if not rows:
        rows.append("<tr><td colspan='10'><div class='empty-state'>Событий пока нет</div></td></tr>")
    filters_html = f"""<form class='filter-grid' method='get' action='/provider-changes'>
<label>GEO <select name='country_id'>{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Область применения <select name='apply_scope'>{routing_scope_options(q.get('apply_scope'))}</select></label>
<label>Сервер <select name='server_id'>{options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label>
<label>Кампания ID <input name='campaign_id' value='{esc(q.get('campaign_id'))}'></label>
<label>Провайдер <select name='provider_id'>{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
<label class='checkbox-inline'><input type='checkbox' name='include_inactive' value='1' {'checked' if q.get('include_inactive') == '1' else ''}> Показывать архив/неактивные</label>
<button>Поиск</button></form>"""
    journal_html = f"<table><thead><tr><th>Дата события</th><th>Область применения</th><th>GEO</th><th>Сервер</th><th>Кампания</th><th>Детали</th><th>Причина</th><th>Комментарий</th><th>Активна</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    body = f"""
<h1>Смена провайдеров</h1>
{routing_event_form(repo)}
{filter_card(filters_html, q, ('country_id', 'apply_scope', 'server_id', 'campaign_id', 'provider_id', 'include_inactive'))}
{table_card(journal_html, title='Журнал событий', extra_class='journal-card')}"""
    return page("Смена провайдеров", body)


def admin_page(repo: Repository) -> bytes:
    body = """
<h1>Администрирование</h1><div class="grid">
<a class="card" href="/admin/server-priorities">Приоритет по серверам</a>
<a class="card" href="/admin/company-routing-settings">Схема маршрутизации кампаний</a>
<a class="card" href="/admin/naming-rules">Правила нейминга маршрутов</a>
<a class="card" href="/admin/import">Импорт / экспорт</a>
<a class="card" href="/admin/currency-rates">Курсы валют</a>
<a class="card" href="/admin/change-reasons">Причины смены провайдера</a>
<a class="card" href="/admin/dictionaries">Справочные значения</a>
<a class="card" href="/admin/change-log">Change log</a>
<a class="card" href="/companies">Кампании прозвона</a>
</div>"""
    return page("Администрирование", body)


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
    for row in repo.conn.execute(f"""
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
    """, priority_params):
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
        actions = f"""
        <details><summary>Редактировать</summary>
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
                f"<tr><td>{esc(row['country_name'])}</td><td>{current_priority}</td><td>{previous_priority}</td><td class='actions'>{actions}</td></tr>"
            )
    blocks = []
    for server_id in server_names:
        rows = server_rows[server_id] or ["<tr><td colspan='4' class='muted'>Нет настроенных приоритетов</td></tr>"]
        table_html = f"<table><thead><tr><th>GEO</th><th>Текущий приоритет</th><th>Предыдущий приоритет</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
        blocks.append(f"""
<section class='server-priority-block'>
  <h2>Сервер: {esc(server_names[server_id])}</h2>
  {table_card(table_html)}
</section>""")
    filters_html = f"""<form class="filter-grid" method="get" action="/admin/server-priorities"><label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label><label>Сервер <select name="server_id">{active_options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label><button>Поиск</button></form>"""
    body = f"""
<h1>Администрирование → Приоритет по серверам</h1>
{filter_card(filters_html, q, ('country_id', 'server_id'))}
{''.join(blocks)}"""
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
    rows = []
    for setting in repo.list_company_routing_settings(filters):
        route_label = setting["route_name"] or "—"
        provider_label = f"<br><span class='muted'>Провайдер: {esc(setting['provider_name'])}</span>" if setting["provider_name"] else ""
        active_badge = "Да" if setting["is_active"] else "Нет"
        actions = ""
        if setting["is_active"] and setting["valid_to"] is None:
            actions = f"""
            <details><summary>Редактировать</summary>
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
            f"<tr><td>{esc(setting['country_name'])}</td><td>{esc(setting['server_name'])}</td>"
            f"<td>{esc(setting['company_id_external'])}</td><td>{esc(setting['company_name'])}</td>"
            f"<td>{esc(setting['routing_mode'])}</td><td>{'Да' if setting['has_autorotation'] else 'Нет'}</td>"
            f"<td>{esc(route_label)}{provider_label}</td><td>{active_badge}</td>"
            f"<td>{esc(setting['valid_from'])}</td><td>{esc(setting['valid_to'])}</td>"
            f"<td>{esc(setting['comment'])}</td><td>{actions}</td></tr>"
        )
    filters_html = f"""<form class="filter-grid" method="get" action="/admin/company-routing-settings">
<label>GEO <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Сервер <select name="server_id">{options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label>
<label>ID кампании <input name="company_id_external" value="{esc(q.get('company_id_external'))}"></label>
<label>Режим маршрутизации <select name="routing_mode">{routing_mode_options(q.get('routing_mode'), empty='Все')}</select></label>
<label>Активность <select name="is_active"><option value="" {'selected' if not q.get('is_active') else ''}>Все</option><option value="1" {'selected' if q.get('is_active')=='1' else ''}>Активна</option><option value="0" {'selected' if q.get('is_active')=='0' else ''}>Неактивна</option></select></label>
<label class="checkbox-inline"><input type="checkbox" name="show_history" value="1" {'checked' if show_history else ''}> Показывать историю</label>
<button>Поиск</button></form>"""
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
    table_html = f"""<table><thead><tr><th>GEO</th><th>Сервер</th><th>ID кампании</th><th>Название кампании</th><th>Режим маршрутизации</th><th>Авторотация</th><th>Маршрут кампании</th><th>Активна</th><th>Действует с</th><th>Действует до</th><th>Комментарий</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""
    body = f"""
<h1>Администрирование → Схема маршрутизации кампаний</h1>
{filter_card(filters_html, q, ('country_id', 'server_id', 'company_id_external', 'routing_mode', 'is_active', 'show_history'))}
{form_card('+ Добавить схему маршрутизации кампании', create_html)}
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
    body = f"""<h1>Администрирование → Импорт / экспорт</h1><form method="post" action="/admin/import/preview"><label>Раздел <span class="required">*</span><select name="entity_type" id="entity_type"><option value="routes" {sel('routes')}>Маршруты</option><option value="tariffs" {sel('tariffs')}>Тарифы</option><option value="phone_numbers" {sel('phone_numbers')}>Купленные номера</option><option value="calling_companies" {sel('calling_companies')}>Кампании прозвона</option><option value="dictionaries" {sel('dictionaries')}>Справочники</option></select></label><label>Режим <select name="mode" id="import_mode"><option value="append_update" {mode_sel('append_update')}>Дополнить / обновить</option><option value="replace_section" {mode_sel('replace_section')}>Заменить выбранный раздел</option></select></label><p class='muted'>Для тарифов режим «Заменить выбранный раздел» недоступен: используйте только «Дополнить / обновить».</p><br><textarea name="csv_data" rows="12" cols="110" placeholder="Вставьте CSV с заголовками">{esc(csv_data)}</textarea><br><button>Предпросмотр</button><button formaction="/admin/import/apply">Импортировать</button></form>{preview_html}<script>
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
        rows.append(f"""<tr><td>{esc(reason['name'])}</td><td>{'Да' if reason['is_active'] else 'Нет'}</td><td>{esc(reason['description'])}</td><td><details><summary>✏️</summary><form method='post' action='/admin/change-reasons/{reason['id']}/update'><label>Название <input name='name' value='{esc(reason['name'])}'></label><label>Активна <select name='is_active'><option value='1' {'selected' if reason['is_active'] else ''}>Да</option><option value='0' {'selected' if not reason['is_active'] else ''}>Нет</option></select></label><label>Комментарий <input name='comment' value='{esc(reason['description'])}'></label><button>Сохранить</button></form></details></td></tr>""")
    create_html = "<form class='form-grid' method='post' action='/admin/change-reasons/create'><label>Название причины <span class='required'>*</span><input name='name'></label><label>Активна <select name='is_active'><option value='1'>Да</option><option value='0'>Нет</option></select></label><label>Комментарий <input name='comment'></label><button>Сохранить</button></form>"
    table_html = f"<table><thead><tr><th>Название причины</th><th>Активна</th><th>Комментарий</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
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
            return "<form method='post' action='/admin/dictionaries/countries/create'><input name='name' placeholder='GEO'><input name='code' placeholder='Код'><button>Добавить</button></form>"
        if section == "providers":
            return f"<form method='post' action='/admin/dictionaries/providers/create'><input name='name' placeholder='Название провайдера'><select name='default_currency_id'><option value=''>—</option>{options(repo, 'currencies', 'code')}</select><input name='comment' placeholder='Комментарий'><button>Добавить</button></form>"
        if section == "currencies":
            return "<form method='post' action='/admin/dictionaries/currencies/create'><input name='code' placeholder='USD'><input name='name' placeholder='Название'><button>Добавить</button></form>"
        if section == "prefixes":
            return f"<form method='post' action='/admin/dictionaries/prefixes/create'><select name='provider_id'>{options(repo, 'providers')}</select><input name='prefix' placeholder='0827 или пусто'><input name='name' placeholder='Комментарий'><button>Добавить</button></form>"
        if section == "servers":
            return "<form method='post' action='/admin/dictionaries/servers/create'><input name='name' placeholder='EU3'><input name='comment' placeholder='Комментарий'><button>Добавить</button></form>"
        if section == "phone-types":
            return "<form method='post' action='/admin/dictionaries/phone-types/create'><input name='name' placeholder='Mobile'><input name='comment' placeholder='Комментарий'><button>Добавить</button></form>"
        if section == "projects":
            return "<form method='post' action='/admin/dictionaries/projects/create'><input name='name' placeholder='Competitors'><input name='comment' placeholder='Комментарий'><button>Добавить</button></form>"
        if section == "phone-assignments":
            return "<form method='post' action='/admin/dictionaries/phone-assignments/create'><input name='name' placeholder='Мониторинг'><input name='code' placeholder='Код необязательно'><input name='comment' placeholder='Комментарий'><button>Добавить</button></form>"
        return ""

    cards = []
    for section, title in sections:
        active = " active" if section == active_section else ""
        cards.append(f"""
<div class='dictionary-card{active}'>
  <a class='dictionary-card-title' href='/admin/dictionaries?section={section}'>{esc(title)}</a>
  {add_form(section)}
</div>""")

    rows: list[str] = []
    headers: list[str]
    if active_section == "countries":
        headers = ["GEO", "Код", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM countries ORDER BY name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td>{esc(row['code'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td class='muted'>—</td><td><details><summary>Редактировать</summary><form method='post' action='/admin/dictionaries/countries/{row['id']}/update'><input name='name' value='{esc(row['name'])}'><input name='code' value='{esc(row['code'])}' placeholder='Код'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "providers":
        headers = ["Название", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT p.*, c.code AS currency_code FROM providers p LEFT JOIN currencies c ON c.id = p.default_currency_id ORDER BY p.name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td><details><summary>Редактировать</summary><form method='post' action='/admin/dictionaries/providers/{row['id']}/update'><input name='name' value='{esc(row['name'])}'><select name='default_currency_id'><option value=''>—</option>{options(repo, 'currencies', 'code', selected=row['default_currency_id'])}</select><input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "currencies":
        headers = ["Код валюты", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM currencies ORDER BY code"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['code'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['name'])}</td><td><details><summary>Редактировать</summary><form method='post' action='/admin/dictionaries/currencies/{row['id']}/update'><input name='code' value='{esc(row['code'])}'><input name='name' value='{esc(row['name'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "prefixes":
        headers = ["Префикс", "Провайдер", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("""
            SELECT pp.*, p.name AS provider_name
            FROM provider_prefixes pp JOIN providers p ON p.id = pp.provider_id
            ORDER BY p.name, COALESCE(pp.prefix, '')
        """))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['prefix'] or 'Без префикса')}</td><td>{esc(row['provider_name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['name'])}</td><td><details><summary>Редактировать</summary><form method='post' action='/admin/dictionaries/prefixes/{row['id']}/update'><select name='provider_id'>{options(repo, 'providers', selected=row['provider_id'])}</select><input name='prefix' value='{esc(row['prefix'])}' placeholder='Без префикса или цифры'><input name='name' value='{esc(row['name'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "servers":
        headers = ["Сервер", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM servers ORDER BY name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td><details><summary>Редактировать</summary><form method='post' action='/admin/dictionaries/servers/{row['id']}/update'><input name='name' value='{esc(row['name'])}'><input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "phone-types":
        headers = ["Тип номера", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM phone_number_types ORDER BY name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td><details><summary>Редактировать</summary><form method='post' action='/admin/dictionaries/phone-types/{row['id']}/update'><input name='name' value='{esc(row['name'])}'><input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    elif active_section == "projects":
        headers = ["Название проекта", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM projects ORDER BY name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td><details><summary>Редактировать</summary><form method='post' action='/admin/dictionaries/projects/{row['id']}/update'><input name='name' value='{esc(row['name'])}'><input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")
    else:
        headers = ["Назначение", "Активен", "Комментарий", "Действия"]
        source = list(repo.conn.execute("SELECT * FROM phone_assignment_types ORDER BY name"))
        for row in source:
            rows.append(f"""<tr{row_class(row)}><td>{esc(row['name'])}</td><td><span class='status-badge'>{active_label(row['is_active'])}</span></td><td>{esc(row['comment'])}</td><td><details><summary>Редактировать</summary><form method='post' action='/admin/dictionaries/phone-assignments/{row['id']}/update'><input name='name' value='{esc(row['name'])}'><input name='code' value='{esc(row['code'])}' readonly><input name='comment' value='{esc(row['comment'])}' placeholder='Комментарий'>{active_select(row['is_active'])}<button>Сохранить</button></form></details></td></tr>""")

    header_html = "".join(f"<th>{esc(header)}</th>" for header in headers)
    table_html = f"<table><thead><tr>{header_html}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    body = f"""
<h1>Администрирование → Справочные значения</h1>
<p class='muted'>Неактивные значения остаются в таблицах, но не показываются в формах создания новых записей.</p>
<div class='dictionary-layout'>
  <aside class='dictionary-sidebar'>{''.join(cards)}</aside>
  <section class='dictionary-workspace'>
    <div class='dictionary-toolbar'><h2>Справочник: {esc(titles[active_section])}</h2><span>Всего записей: {len(source)}</span></div>
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
<button onclick="return confirm('Сохранить изменения?')">Сохранить</button></form>"""
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
<label>Статус <select name='status'><option value='used'>Используется</option><option value='free'>Свободен</option><option value='disabled'>Отключён</option><option value='reserved'>Резерв</option><option value='blocked'>Заблокирован</option><option value='unknown'>Неизвестно</option></select></label>
<label>Активен <select name='is_active'><option value='1' {'selected' if phone['is_active'] else ''}>Да</option><option value='0' {'selected' if not phone['is_active'] else ''}>Нет</option></select></label>
<label>Стоимость подключения <input name='connection_cost' value='{esc(phone['connection_cost'])}'></label>
<label>Абонентская плата <input name='monthly_fee' value='{esc(phone['monthly_fee'])}'></label>
<label>Валюта <select name='currency_id'><option value=''>—</option>{active_options(repo, 'currencies', 'code', selected=phone['currency_id'])}</select></label>
<label>Тип номера <select name='phone_type'>{phone_type_options(repo, selected=phone['phone_type'], empty='—')}</select></label>
<label>Тариф <input name='tariff_label' value='{esc(phone['tariff_label'])}'></label>
<label>Комментарий <input name='comment' value='{esc(phone['comment'])}'></label>
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
    if path == "/routes/create":
        country_id = int(data["country_id"]); provider_id = int(data["provider_id"]); prefix_id = parse_int(data.get("provider_prefix_id"))
        if prefix_id:
            prefix_provider = repo.conn.execute("SELECT provider_id FROM provider_prefixes WHERE id = ?", (prefix_id,)).fetchone()
            if prefix_provider and int(prefix_provider["provider_id"]) != provider_id:
                raise BusinessRuleError("Префикс не принадлежит выбранному провайдеру")
        name = build_route_name(repo, country_id, provider_id, data.get("project_label"), data.get("cli_source_label", ""), prefix_id)
        if len(name.replace("/", "").replace("@", "").strip()) < 4:
            raise BusinessRuleError("Некорректное название маршрута: заполните ГЕО, провайдера и источник АОН")
        repo.create_route(country_id=country_id, provider_id=provider_id, provider_prefix_id=prefix_id, name=name, project_label=data.get("project_label"), cli_source_type=data["cli_source_type"], cli_source_label=data["cli_source_label"], comment=data.get("comment"), created_by=ADMIN_ID, is_actual=data.get("is_actual") == "1")
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
        repo.conn.execute("UPDATE routes SET name = ?, provider_prefix_id = ?, comment = ?, is_actual = ?, priority_status = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (name, prefix_id, data.get("comment"), 1 if data.get("is_actual") == "1" else 0, data.get("priority_status") or "unknown", ADMIN_ID, route_id))
        repo.conn.execute("INSERT INTO route_history(route_id, action, changed_by, field_name, old_value, new_value, comment) VALUES (?, 'updated', ?, 'route', ?, ?, ?)", (route_id, ADMIN_ID, str(dict(old)) if old else None, str({"name": name, "provider_prefix_id": data.get("provider_prefix_id"), "comment": data.get("comment"), "is_actual": data.get("is_actual"), "priority_status": data.get("priority_status")}), data.get("comment")))
        repo.conn.commit()
        return "/routes"
    if path.startswith("/routes/") and path.endswith("/numbers/add"):
        route_id = int(path.strip("/").split("/")[1])
        repo.add_phone_to_route_by_number(route_id=route_id, number=data["phone_number"], usage_type=data.get("usage_type") or "pool_member", added_by=ADMIN_ID, comment=data.get("comment"))
        return f"/routes/{route_id}/numbers"
    if path.startswith("/routes/") and path.endswith("/numbers/bulk-add"):
        route_id = int(path.strip("/").split("/")[1])
        added, errors = 0, []
        for number in [n.strip() for n in data.get("phone_numbers", "").replace(",", "\n").splitlines() if n.strip()]:
            try:
                repo.add_phone_to_route_by_number(route_id=route_id, number=number, usage_type="pool_member", added_by=ADMIN_ID)
                added += 1
            except (BusinessRuleError, sqlite3.IntegrityError) as exc:
                errors.append(f"{number}: {exc}")
        from urllib.parse import quote
        report = "Массовое добавление завершено. Добавлено %s из %s. Не добавлены: %s" % (added, added + len(errors), "; ".join(errors) or "—")
        return f"/routes/{route_id}/numbers?notice={quote(report)}"
    if path.startswith("/routes/") and path.endswith("/numbers/remove"):
        route_id = int(path.strip("/").split("/")[1])
        link_ids = [int(v) for v in parse_qs(data.get("_raw", "")).get("link_ids", []) if v]
        removed = repo.remove_phone_links_from_route(route_id=route_id, link_ids=link_ids, removed_by=ADMIN_ID, reason=data.get("reason"))
        if removed == 0:
            raise BusinessRuleError("Выберите номера для исключения из маршрута")
        return f"/routes/{route_id}/numbers"
    if path == "/phones/create":
        repo.create_phone_number(country_id=int(data["country_id"]), provider_id=parse_int(data.get("provider_id")), number=data["number"], assignment_type=data["assignment_type"], status=data["status"], created_by=ADMIN_ID, project_label=data.get("project_label") or None, connection_cost=data.get("connection_cost") or None, monthly_fee=data.get("monthly_fee") or None, currency_id=parse_int(data.get("currency_id")), phone_type=data.get("phone_type") or None, tariff_label=data.get("tariff_label") or None, comment=data.get("comment"))
        return "/phones"
    if path.startswith("/phones/") and path.endswith("/update"):
        phone_id = int(path.strip("/").split("/")[1])
        normalized = validate_phone_number(data["number"])
        is_active = 1 if data.get("is_active") == "1" else 0
        repo.conn.execute("""
            UPDATE phone_numbers
            SET number = ?, normalized_number = ?, country_id = ?, provider_id = ?, project_label = ?,
                assignment_type = ?, status = ?, is_active = ?, connection_cost = ?, monthly_fee = ?,
                currency_id = ?, phone_type = ?, tariff_label = ?, comment = ?,
                deactivated_at = CASE WHEN ? = 0 AND deactivated_at IS NULL THEN CURRENT_TIMESTAMP WHEN ? = 1 THEN NULL ELSE deactivated_at END,
                updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (normalized, normalized, int(data["country_id"]), parse_int(data.get("provider_id")), data.get("project_label") or None,
              data.get("assignment_type"), data.get("status"), is_active, data.get("connection_cost") or None,
              data.get("monthly_fee") or None, parse_int(data.get("currency_id")), data.get("phone_type") or None, data.get("tariff_label") or None,
              data.get("comment"), is_active, is_active, ADMIN_ID, phone_id))
        repo.conn.execute("INSERT INTO phone_number_history(phone_number_id, action, changed_by, field_name, new_value, comment) VALUES (?, 'updated', ?, 'phone', ?, ?)", (phone_id, ADMIN_ID, str({"number": normalized, "status": data.get("status"), "is_active": data.get("is_active")}), data.get("comment")))
        repo.conn.commit(); return "/phones"
    if path == "/tariffs/create":
        currency_id = int(data["currency_id"])
        rate = repo.latest_currency_rate(currency_id)
        if rate is None:
            raise BusinessRuleError("Для выбранной валюты нет курса к EUR. Добавьте курс в Администрирование → Курсы валют")
        prefix_id = parse_int(data.get("provider_prefix_id"))
        tariff_id = repo.create_tariff(country_id=int(data["country_id"]), provider_id=int(data["provider_id"]), provider_prefix_id=prefix_id, provider_currency_id=currency_id, price_in_provider_currency=data["price"], conversion_rate_to_eur=rate["rate_to_eur"], conversion_rate_date=rate["rate_date"], currency_rate_id=rate["id"], created_by=ADMIN_ID, priority_status=data["priority_status"], comment=data.get("comment"))
        if data.get("is_current") == "0":
            repo.conn.execute("UPDATE tariffs SET is_current = 0 WHERE id = ?", (tariff_id,)); repo.conn.commit()
        return "/tariffs"
    if path.startswith("/tariffs/") and path.endswith("/deactivate"):
        tariff_id = int(path.strip("/").split("/")[1])
        repo.conn.execute("UPDATE tariffs SET is_current = 0, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (ADMIN_ID, tariff_id)); repo.conn.commit(); return "/tariffs"
    if path == "/companies/create":
        repo.create_calling_company(server_id=int(data["server_id"]), country_id=int(data["country_id"]), company_name=data["company_name"], company_id_external=data["company_id_external"], has_autorotation=data.get("has_autorotation") == "1", created_by=ADMIN_ID, comment=data.get("comment"), is_active=data.get("is_active") == "1", line_count=int(data.get("line_count") or 0), dial_set_count=int(data.get("dial_set_count") or 0), retry_interval_seconds=int(data.get("retry_interval_seconds") or 0))
        return "/companies"
    if path.startswith("/companies/") and path.endswith("/update"):
        company_id = int(path.strip("/").split("/")[1])
        repo.conn.execute("""UPDATE calling_companies SET server_id = ?, country_id = ?, company_name = ?, line_count = ?, dial_set_count = ?, has_autorotation = ?, retry_interval_seconds = ?, is_active = ?, comment = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                          (int(data["server_id"]), int(data["country_id"]), data["company_name"], int(data.get("line_count") or 0), int(data.get("dial_set_count") or 0), 1 if data.get("has_autorotation") == "1" else 0, int(data.get("retry_interval_seconds") or 0), 1 if data.get("is_active") == "1" else 0, data.get("comment"), ADMIN_ID, company_id))
        repo._change_log("calling_company", company_id, "calling_company.updated", ADMIN_ID, new_values={"company_name": data["company_name"]})
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
            new_company_has_autorotation=parse_int(data.get("new_company_has_autorotation")), created_by=ADMIN_ID,
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
            new_company_has_autorotation=parse_int(data.get("new_company_has_autorotation")), updated_by=ADMIN_ID,
        )
        return "/provider-changes"
    if path.startswith("/provider-changes/") and path.endswith("/deactivate"):
        change_id = int(path.strip("/").split("/")[1])
        repo.deactivate_routing_event(change_id, reason=data.get("deactivation_reason"), deactivated_by=ADMIN_ID)
        return "/provider-changes"
    if path in {"/admin/currency-rates/create", "/admin/currency-rates/upsert"}:
        currency_id = int(data["currency_id"])
        today = datetime.now().strftime("%Y-%m-%d")
        existing = repo.conn.execute("SELECT id FROM currency_rates WHERE currency_id = ? ORDER BY rate_date DESC, created_at DESC, id DESC LIMIT 1", (currency_id,)).fetchone()
        if existing:
            repo.conn.execute("UPDATE currency_rates SET rate_to_eur = ?, rate_date = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (data["rate_to_eur"], today, ADMIN_ID, existing["id"]))
        else:
            repo.conn.execute("INSERT INTO currency_rates(currency_id, rate_to_eur, rate_date, updated_by, source) VALUES (?, ?, ?, ?, 'manual')", (currency_id, data["rate_to_eur"], today, ADMIN_ID))
        repo.conn.commit(); return "/admin/currency-rates"
    if path == "/admin/change-reasons/create":
        repo.create_change_reason(data["name"], created_by=ADMIN_ID, comment=data.get("comment"), is_active=data.get("is_active") == "1"); return "/admin/change-reasons"
    if path.startswith("/admin/change-reasons/") and path.endswith("/update"):
        reason_id = int(path.strip("/").split("/")[2])
        repo.conn.execute("UPDATE change_reasons SET name = ?, description = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (data["name"].strip(), data.get("comment"), 1 if data.get("is_active") == "1" else 0, reason_id))
        repo._change_log("change_reason", reason_id, "change_reason.updated", ADMIN_ID, new_values={"name": data["name"].strip(), "is_active": data.get("is_active")})
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
        repo._change_log(kind, entity_id, "dictionary.updated", ADMIN_ID, new_values={"is_active": is_active})
        repo.conn.commit()
        return "/admin/dictionaries"
    if path.startswith("/admin/server-priorities/") and path.endswith("/update"):
        priority_id = int(path.strip("/").split("/")[2])
        repo.update_server_route_priority(
            priority_id=priority_id,
            current_route_id=int(data["current_route_id"]),
            comment=data.get("comment"),
            changed_by=ADMIN_ID,
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
            changed_by=ADMIN_ID,
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
            created_by=ADMIN_ID,
        )
        if data.get("is_active") != "1":
            repo.deactivate_company_routing_setting(setting_id=setting_id, updated_by=ADMIN_ID)
        return "/admin/company-routing-settings"
    if path.startswith("/admin/company-routing-settings/") and path.endswith("/update"):
        setting_id = int(path.strip("/").split("/")[2])
        if data.get("is_active") != "1":
            repo.deactivate_company_routing_setting(setting_id=setting_id, updated_by=ADMIN_ID)
        else:
            repo.update_company_routing_setting(
                setting_id=setting_id,
                country_id=int(data["country_id"]),
                server_id=int(data["server_id"]),
                route_id=parse_int(data.get("route_id")),
                routing_mode=data["routing_mode"],
                has_autorotation=data.get("has_autorotation") == "1",
                comment=data.get("comment"),
                updated_by=ADMIN_ID,
            )
        return "/admin/company-routing-settings"
    if path.startswith("/admin/company-routing-settings/") and path.endswith("/deactivate"):
        setting_id = int(path.strip("/").split("/")[2])
        repo.deactivate_company_routing_setting(setting_id=setting_id, updated_by=ADMIN_ID)
        return "/admin/company-routing-settings"
    if path == "/admin/telegram/save":
        repo.conn.execute("INSERT INTO telegram_settings(is_enabled, chat_id, bot_token_secret_ref, message_template, updated_by) VALUES (?, ?, ?, ?, ?)", (1 if data.get("is_enabled") == "1" else 0, data.get("chat_id"), data.get("bot_token_secret_ref"), data.get("message_template"), ADMIN_ID)); repo.conn.commit(); return "/admin/telegram"
    if path == "/admin/telegram/test":
        repo.conn.execute("INSERT INTO telegram_settings(is_enabled, chat_id, bot_token_secret_ref, message_template, last_test_status, last_test_at, last_test_by, updated_by) VALUES (?, ?, ?, ?, 'success', CURRENT_TIMESTAMP, ?, ?)", (1 if data.get("is_enabled") == "1" else 0, data.get("chat_id"), data.get("bot_token_secret_ref"), data.get("message_template"), ADMIN_ID, ADMIN_ID)); repo.conn.execute("INSERT INTO change_log(entity_type, change_type, changed_by, summary, source) VALUES ('telegram', 'telegram.test_message_sent', ?, 'Test Telegram message requested', 'ui')", (ADMIN_ID,)); repo.conn.commit(); return "/admin/telegram"
    if path == "/admin/naming-rules/create":
        if data.get("is_active") == "1": repo.conn.execute("UPDATE route_naming_rules SET is_active = 0")
        repo.conn.execute("INSERT INTO route_naming_rules(name, template, is_active, comment, created_by) VALUES (?, ?, ?, ?, ?)", (data["name"], data["template"], 1 if data.get("is_active") == "1" else 0, data.get("comment"), ADMIN_ID)); repo.conn.commit(); return "/admin/naming-rules"
    raise BusinessRuleError("Unsupported form action")


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
    try:
        if method == "POST":
            raw_size = int(environ.get("CONTENT_LENGTH") or "0")
            raw_body = environ["wsgi.input"].read(raw_size).decode("utf-8")
            parsed = {key: values[-1] for key, values in parse_qs(raw_body, keep_blank_values=True).items()}
            parsed["_raw"] = raw_body
            if path == "/admin/import/preview":
                if parsed["entity_type"] == "tariffs" and parsed.get("mode") == "replace_section":
                    raise BusinessRuleError("Для тарифов доступен только режим Дополнить / обновить")
                preview = preview_import(conn, parsed["entity_type"], parsed.get("csv_data", ""))
                rows = "".join(f"<tr><td>{r['line']}</td><td>{esc(r['status'])}</td><td>{esc(r['action'])}</td><td>{esc(r['message'])}</td></tr>" for r in preview.rows)
                html_preview = f"<h2>Предпросмотр</h2><p>Всего: {preview.total_rows}, новых: {preview.new_rows}, дублей: {preview.duplicate_rows}, ошибок: {preview.error_rows}</p><table><tr><th>Строка</th><th>Статус</th><th>Действие</th><th>Комментарий</th></tr>{rows}</table>"
                start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
                return [import_page(repo, html_preview, selected_entity=parsed["entity_type"], selected_mode=parsed.get("mode", "append_update"), csv_data=parsed.get("csv_data", ""))]
            if path == "/admin/import/apply":
                result = apply_import(conn, parsed["entity_type"], parsed.get("csv_data", ""), user_id=ADMIN_ID, mode=parsed.get("mode", "append_update"))
                notice = f"<h2>Импорт завершён</h2><ul><li>создано {result.created_rows}</li><li>обновлено {result.updated_rows}</li><li>пропущено {result.skipped_rows}</li><li>ошибок {result.error_rows}</li></ul>"
                start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
                return [import_page(repo, notice, selected_entity=parsed["entity_type"], selected_mode=parsed.get("mode", "append_update"), csv_data=parsed.get("csv_data", ""))]
            location = handle_post(repo, path, parsed)
            return redirect(start_response, location)
        if path in {"/", "/routes"}: response = routes_page(repo, q)
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
        elif path == "/admin/dictionaries": response = dictionaries_page(repo, q)
        elif path == "/admin/telegram": response = telegram_page(repo)
        elif path == "/admin/change-log": response = change_log_page(repo)
        elif path.startswith("/routes/") and path.endswith("/edit"): response = route_edit_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/phones/") and path.endswith("/edit"): response = phone_edit_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/companies/") and path.endswith("/edit"): response = company_edit_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/provider-changes/") and path.endswith("/edit"): response = provider_change_edit_page(repo, int(path.strip("/").split("/")[1]))
        elif path.startswith("/routes/") and path.endswith("/numbers"): response = route_numbers_page(repo, int(path.strip("/").split("/")[1]), q)
        else:
            start_response("404 Not Found", [("Content-Type", "text/html; charset=utf-8")]); return [page("404", "<h1>404</h1>")]
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")]); return [response]
    except (BusinessRuleError, ValueError, sqlite3.IntegrityError) as exc:
        start_response("400 Bad Request", [("Content-Type", "text/html; charset=utf-8")])
        return [page("Ошибка", f"<div class='error'>{esc(user_error(exc))}</div><p><a href='/'>На главную</a></p>")]
    finally:
        conn.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    with make_server("0.0.0.0", port, app) as httpd:
        print(f"Serving on http://127.0.0.1:{port}")
        httpd.serve_forever()
