from __future__ import annotations

import html
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from app.db import DEFAULT_DB_PATH, connect, init_db
from app.importer import apply_import, preview_import
from app.repository import BusinessRuleError, Repository, normalize_provider_name, validate_phone_number

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


def page(title: str, body: str, notice: str | None = None) -> bytes:
    notice_html = f"<div class='ok'>{esc(notice)}</div>" if notice else ""
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>{esc(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; color: #1f2937; background: #fff; }}
    nav {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    nav a, .button, button {{ border: 1px solid #d1d5db; border-radius: 8px; color: #111827; padding: 6px 10px; text-decoration: none; background: #f9fafb; cursor: pointer; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px; vertical-align: top; }}
    th {{ background: #f3f4f6; text-align: left; }}
    input, select, textarea {{ border: 1px solid #9ca3af; border-radius: 6px; padding: 6px; margin: 4px; max-width: 100%; }}
    label {{ display: inline-block; margin: 4px 12px 4px 0; }}
    fieldset {{ border: 1px solid #d1d5db; border-radius: 8px; margin: 14px 0; padding: 12px; }}
    .required {{ color: #b91c1c; font-weight: 700; }}
    .muted {{ color: #6b7280; }}
    .error {{ border: 1px solid #dc2626; background: #fee2e2; padding: 12px; border-radius: 8px; }}
    .ok {{ border: 1px solid #16a34a; background: #dcfce7; padding: 12px; border-radius: 8px; margin: 10px 0; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }}
    .card {{ border: 1px solid #d1d5db; border-radius: 10px; padding: 12px; background: #f9fafb; }}
    .star {{ color: #f59e0b; font-weight: 800; }}
    .actions {{ white-space: nowrap; }}
    .dictionary-layout {{ display: grid; grid-template-columns: minmax(220px, 20%) 1fr; gap: 18px; align-items: start; }}
    .dictionary-sidebar {{ display: grid; gap: 10px; }}
    .dictionary-card {{ border: 1px solid #d1d5db; border-radius: 10px; padding: 10px; background: #f9fafb; }}
    .dictionary-card.active {{ border-color: #2563eb; background: #eff6ff; box-shadow: 0 0 0 2px #bfdbfe inset; }}
    .dictionary-card-title {{ display: block; font-weight: 800; color: #111827; text-decoration: none; margin-bottom: 8px; }}
    .dictionary-card form {{ display: grid; gap: 6px; }}
    .dictionary-card input, .dictionary-card select {{ width: 100%; box-sizing: border-box; margin: 0; }}
    .dictionary-workspace {{ min-width: 0; }}
    .dictionary-toolbar {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; border: 1px solid #d1d5db; border-radius: 10px; padding: 10px 12px; background: #f9fafb; }}
    .dictionary-toolbar h2 {{ margin: 0; }}
    .inactive-row {{ color: #6b7280; background: #f3f4f6; }}
    .status-badge {{ white-space: nowrap; }}
  </style>
</head>
<body>
  <nav>
    <a href="/routes">Маршруты</a>
    <a href="/tariffs">Тарифы</a>
    <a href="/phones">Купленные номера</a>
    <a href="/companies">Кампании прозвона</a>
    <a href="/provider-changes">Смена провайдеров</a>
    <a href="/admin">Администрирование</a>
  </nav>
  <hr>
  {notice_html}
  {body}
</body>
</html>""".encode("utf-8")


def redirect(start_response, location: str):
    start_response("303 See Other", [("Location", location)])
    return [b""]


def request_query(environ) -> dict[str, str]:
    return {key: values[-1] for key, values in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}


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


def ensure_seed(repo: Repository) -> None:
    def ensure_reference_defaults() -> None:
        for server_name in ("EU1", "EU2", "US1", "US2", "ASIA1", "LATAM1", "LATAM2", "DE1", "NL1"):
            repo.conn.execute("INSERT OR IGNORE INTO servers(name, is_active) VALUES (?, 1)", (server_name,))
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

    if repo.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        ensure_reference_defaults()
        return
    ensure_reference_defaults()
    admin_id = repo.create_user("admin", "Admin", "Admin")
    country_id = repo.create_country("Мексика", "MEX")
    eur_id = repo.create_currency("EUR", "Euro", "€")
    usdt_id = repo.create_currency("USDT", "Tether", "₮")
    sancom_id = repo.create_provider("Sancom", "voip", eur_id)
    miatel_id = repo.create_provider("Miatel", "voip", usdt_id)
    sancom_prefix = repo.create_prefix(sancom_id, "0827")
    miatel_prefix = repo.create_prefix(miatel_id, None, "Без префикса")
    repo.conn.execute("INSERT INTO currency_rates(currency_id, rate_to_eur, rate_date, updated_by, comment) VALUES (?, 1, '2026-06-07', ?, 'Demo EUR')", (eur_id, admin_id))
    repo.conn.execute("INSERT INTO currency_rates(currency_id, rate_to_eur, rate_date, updated_by, comment) VALUES (?, 0.93, '2026-06-07', ?, 'Demo USDT')", (usdt_id, admin_id))
    for reason in ("Плохие показатели", "Провайдер починил", "Обновлен пул номеров"):
        repo.conn.execute("INSERT OR IGNORE INTO change_reasons(name, description, is_active) VALUES (?, ?, 1)", (reason, reason))
    sancom_route = repo.create_route(country_id=country_id, provider_id=sancom_id, provider_prefix_id=sancom_prefix, name="Мексика/Sancom/RND/0827pfx@", cli_source_type="rnd", cli_source_label="RND", created_by=admin_id, comment="RND провайдера", priority_status="alternative")
    miatel_route = repo.create_route(country_id=country_id, provider_id=miatel_id, provider_prefix_id=miatel_prefix, name="Мексика/Miatel/Pool_A@", cli_source_type="pool", cli_source_label="Pool_A", created_by=admin_id, comment="Демо-маршрут после первичного запуска", priority_status="priority")
    repo.create_tariff(country_id=country_id, provider_id=sancom_id, provider_prefix_id=sancom_prefix, provider_currency_id=eur_id, price_in_provider_currency="2.00", conversion_rate_to_eur="1", conversion_rate_date="2026-06-07", created_by=admin_id, priority_status="alternative")
    repo.create_tariff(country_id=country_id, provider_id=miatel_id, provider_prefix_id=miatel_prefix, provider_currency_id=usdt_id, price_in_provider_currency="3.00", conversion_rate_to_eur="0.93", conversion_rate_date="2026-06-07", created_by=admin_id, priority_status="priority")
    phone_id = repo.create_phone_number(country_id=country_id, provider_id=miatel_id, number="525512345001", assignment_type="pool_number", status="used", created_by=admin_id, currency_id=eur_id, monthly_fee="1.00", comment="Демо-номер")
    repo.add_phone_to_route(route_id=miatel_route, phone_number_id=phone_id, usage_type="pool_member", added_by=admin_id)
    server_id = int(repo.conn.execute("SELECT id FROM servers WHERE name = 'EU1'").fetchone()["id"])
    repo.create_calling_company(server_id=server_id, country_id=country_id, company_name="CC Mexico Demo", company_id_external="1001", has_autorotation=True, created_by=admin_id, is_active=True, line_count=10, dial_set_count=2, retry_interval_seconds=60)
    repo.conn.execute("""
        INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, changed_by, created_by, comment)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (country_id, server_id, miatel_route, sancom_route, admin_id, admin_id, "Демо-приоритет"))
    repo.conn.commit()


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


def routes_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    filters = {"country_id": q.get("country_id"), "provider_id": q.get("provider_id"), "prefix_id": q.get("prefix_id"), "is_actual": q.get("is_actual"), "search_like": q.get("search")}
    rows = []
    for route in repo.list_routes(filters):
        prefix = route["prefix"] or "Без префикса"
        numbers = f'{route["phone_count"]} номеров <a class="button" href="/routes/{route["id"]}/numbers">Показать номера</a>' if route["cli_source_type"] in {"pool", "sim"} else ("RND провайдера" if route["cli_source_type"] == "rnd" else "—")
        edit = f"<a class='button' href='/routes/{route['id']}/edit'>✏️ Редактировать</a>"
        rows.append(f"<tr><td>{esc(route['country_name'])}</td><td>{esc(route['name'])}</td><td>{esc(route['provider_name'])}</td><td>{esc(prefix)}</td><td>{'Да' if route['is_actual'] else 'Нет'}</td><td>{esc(route['comment'])}</td><td>{numbers}</td><td class='actions'>{edit}</td></tr>")
    body = f"""
<h1>Маршруты</h1>
<fieldset><legend>Фильтры</legend><form method="get" action="/routes">
<label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Провайдер <select name="provider_id">{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
<label>Префикс <select name="prefix_id">{prefix_options(repo, selected=q.get('prefix_id'), empty='Все')}</select></label>
<label>Актуальный <select name="is_actual"><option value="">Все</option><option value="1" {'selected' if q.get('is_actual')=='1' else ''}>Да</option><option value="0" {'selected' if q.get('is_actual')=='0' else ''}>Нет</option></select></label>
<label>Поиск <input name="search" value="{esc(q.get('search'))}"></label><button>Поиск</button></form></fieldset>
<details><summary>+ Добавить маршрут <span class="muted">Admin</span></summary>
<form method="post" action="/routes/create">
  <label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label>
  <label>Провайдер <span class="required">*</span><select name="provider_id">{active_options(repo, 'providers')}</select></label>
  <label>Префикс <select name="provider_prefix_id">{prefix_options(repo)}</select></label>
  <label>Проект/метка <input name="project_label"></label>
  <label>Источник АОН <span class="required">*</span><select name="cli_source_type"><option value="pool">Pool</option><option value="rnd">RND</option><option value="sim">SIM</option><option value="single_number">Single</option><option value="other">Other</option></select></label>
  <label>Метка АОН <span class="required">*</span><input name="cli_source_label" value="Pool_A"></label>
  <label>Статус <span class="required">*</span><select name="is_actual"><option value="1">Активный</option><option value="0">Неактивный</option></select></label>
  <label>Комментарий <input name="comment"></label>
  <p class="muted">Название будет сформировано автоматически по выбранным полям. Свободный ввод названия отключён.</p>
  <button>Сохранить</button>
</form></details>
<table><thead><tr><th>ГЕО</th><th>Название маршрута</th><th>Провайдер</th><th>Префикс</th><th>Актуальный</th><th>Комментарий</th><th>Номера</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
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
    body = f"""
<h1>Тарифы</h1><fieldset><legend>Фильтры</legend><form method="get" action="/tariffs">
<label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Провайдер <select name="provider_id">{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
<label>Приоритет <select name="priority_status"><option value="">Все</option><option value="priority" {'selected' if q.get('priority_status')=='priority' else ''}>priority</option><option value="alternative" {'selected' if q.get('priority_status')=='alternative' else ''}>alternative</option><option value="unknown" {'selected' if q.get('priority_status')=='unknown' else ''}>unknown</option></select></label>
<label>Статус <select name="status"><option value="all" {'selected' if q.get('status')=='all' else ''}>Все</option><option value="active" {'selected' if q.get('status','active')=='active' else ''}>Активные</option><option value="inactive" {'selected' if q.get('status')=='inactive' else ''}>Неактивные</option></select></label><button>Поиск</button></form></fieldset>
<details><summary>+ Добавить тариф <span class="muted">Admin</span></summary><form method="post" action="/tariffs/create">
<label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label>
<label>Провайдер <span class="required">*</span><select name="provider_id">{active_options(repo, 'providers')}</select></label>
<label>Префикс <span class="required">*</span><select name="provider_prefix_id">{prefix_options(repo)}</select></label>
<label>Валюта <span class="required">*</span><select name="currency_id">{active_options(repo, 'currencies', 'code')}</select></label>
<label>Цена <span class="required">*</span><input name="price"></label>
<label>Приоритет <span class="required">*</span><select name="priority_status"><option value="priority">priority</option><option value="alternative">alternative</option><option value="unknown">unknown</option></select></label>
<label>Активный <span class="required">*</span><select name="is_current"><option value="1">Да</option><option value="0">Нет</option></select></label>
<label>Комментарий <input name="comment"></label><p class="muted">Курс к EUR и дата курса берутся из Администрирование → Курсы валют.</p><button>Сохранить</button></form></details>
<table><thead><tr><th>ГЕО</th><th>Провайдер</th><th>Префикс</th><th>Цена провайдера</th><th>Цена EUR</th><th>Приоритет</th><th>Активный</th><th>Инфо</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""
    return page("Тарифы", body)


def phones_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    rows = []
    for phone in repo.list_phone_numbers({"country_id": q.get("country_id"), "provider_id": q.get("provider_id"), "project": q.get("project"), "assignment_type": q.get("assignment_type"), "status": q.get("status"), "number_like": q.get("number")}):
        assignment_label = phone["assignment_type_label"] or ASSIGNMENT_LABELS.get(phone["assignment_type"], phone["assignment_type"])
        rows.append(f"""<tr><td>{esc(phone['number'])}</td><td>{esc(phone['country_name'])}</td><td>{esc(phone['provider_name'])}</td><td>{esc(phone['project_label'])}</td><td>{esc(assignment_label)}</td><td>{esc(STATUS_LABELS.get(phone['status'], phone['status']))}</td><td>{'Да' if phone['is_active'] else 'Нет'}</td><td>{phone['route_count']}</td><td>{esc(phone['connection_cost'])}</td><td>{esc(phone['monthly_fee'])}</td><td>{esc(phone['currency_code'])}</td><td>{esc(phone['phone_type'])}</td><td>{esc(phone['tariff_label'])}</td><td>{esc(phone['created_at'])}</td><td>{esc(phone['updated_at'])}</td><td>{esc(phone['deactivated_at'])}</td><td>{esc(phone['comment'])}</td><td><a class='button' href='/phones/{phone['id']}/edit'>✏️ Редактировать</a></td></tr>""")
    body = f"""
<h1>Купленные номера</h1><fieldset><legend>Фильтры</legend><form method="get" action="/phones">
<label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Провайдер <select name="provider_id">{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
    <label>Проект <select name="project">{project_options(repo, selected=q.get('project'), empty='Все')}</select></label>
    <label>Назначение <select name="assignment_type">{assignment_options(repo, selected=q.get('assignment_type'), empty='Все')}</select></label>
<label>Статус <select name="status"><option value="">Все</option><option value="used">Используется</option><option value="free">Свободен</option><option value="disabled">Отключён</option><option value="blocked">Заблокирован</option></select></label>
<label>Поиск по номеру <input name="number" value="{esc(q.get('number'))}"></label><button>Поиск</button></form></fieldset>
<details><summary>+ Добавить номер <span class="muted">Admin</span></summary><form method="post" action="/phones/create">
<label>Номер <span class="required">*</span><input name="number" placeholder="393331234567"></label><label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label><label>Провайдер <select name="provider_id"><option value="">—</option>{active_options(repo, 'providers')}</select></label><label>Проект <select name="project_label">{project_options(repo, empty='—')}</select></label><label>Назначение <span class="required">*</span><select name="assignment_type">{assignment_options(repo)}</select></label><label>Статус <span class="required">*</span><select name="status"><option value="used">Используется</option><option value="free">Свободен</option><option value="disabled">Отключён</option><option value="blocked">Заблокирован</option></select></label><label>Стоимость подключения <input name="connection_cost"></label><label>Абонентская плата <input name="monthly_fee"></label><label>Валюта <select name="currency_id"><option value="">—</option>{active_options(repo, 'currencies', 'code')}</select></label><label>Тип номера <select name="phone_type">{phone_type_options(repo, empty='—')}</select></label><label>Тариф <input name="tariff_label"></label><label>Комментарий <input name="comment"></label><button>Сохранить</button></form></details>
<table><thead><tr><th>Номер</th><th>ГЕО</th><th>Провайдер</th><th>Проект</th><th>Назначение</th><th>Статус</th><th>Активен</th><th>Маршрутов</th><th>Подключение</th><th>Абонплата</th><th>Валюта</th><th>Тип номера</th><th>Тариф</th><th>Дата создания</th><th>Дата изменения</th><th>Дата отключения</th><th>Комментарий</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""
    return page("Купленные номера", body)


def companies_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    rows = []
    for cc in repo.list_calling_companies({"server_id": q.get("server_id"), "country_id": q.get("country_id"), "company_like": q.get("company"), "external_id_like": q.get("external_id"), "has_autorotation": q.get("has_autorotation"), "is_active": q.get("is_active")}):
        rows.append(f"<tr><td>{esc(cc['server_name'])}</td><td>{esc(cc['country_name'])}</td><td>{esc(cc['company_name'])}</td><td>{esc(cc['company_id_external'])}</td><td>{esc(cc['line_count'])}</td><td>{esc(cc['dial_set_count'])}</td><td>{'Да' if cc['has_autorotation'] else 'Нет'}</td><td>{esc(cc['retry_interval_seconds'])}</td><td>{'Активна' if cc['is_active'] else 'Неактивна'}</td><td>{esc(cc['comment'])}</td><td><a class='button' href='/companies/{cc['id']}/edit'>✏️ Редактировать</a></td></tr>")
    body = f"""
<h1>Кампании прозвона</h1><fieldset><legend>Фильтры</legend><form method="get" action="/companies">
<label>Сервер <select name="server_id">{options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label><label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label><label>Название кампании <input name="company" value="{esc(q.get('company'))}"></label><label>ID кампании <input name="external_id" value="{esc(q.get('external_id'))}"></label><label>Авторотация <select name="has_autorotation"><option value="">Все</option><option value="1">Да</option><option value="0">Нет</option></select></label><label>Активность <select name="is_active"><option value="">Все</option><option value="1">Активна</option><option value="0">Неактивна</option></select></label><button>Поиск</button></form></fieldset>
<details><summary>+ Добавить кампанию <span class="muted">Admin</span></summary><form method="post" action="/companies/create"><label>Сервер <span class="required">*</span><select name="server_id">{active_options(repo, 'servers')}</select></label><label>ГЕО <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label><label>ID кампании <span class="required">*</span><input name="company_id_external"></label><label>Название кампании <span class="required">*</span><input name="company_name"></label><label>Количество линий <span class="required">*</span><input name="line_count" value="0"></label><label>Количество наборов <span class="required">*</span><input name="dial_set_count" value="0"></label><label>Авторотация <span class="required">*</span><select name="has_autorotation"><option value="1">Да</option><option value="0">Нет</option></select></label><label>Интервал дозвона, сек. <span class="required">*</span><input name="retry_interval_seconds" value="0"></label><label>Активна <span class="required">*</span><select name="is_active"><option value="1">Да</option><option value="0">Нет</option></select></label><label>Комментарий <input name="comment"></label><button>Сохранить</button></form></details>
<table><thead><tr><th>Сервер</th><th>ГЕО</th><th>Название кампании</th><th>ID кампании</th><th>Количество линий</th><th>Количество наборов</th><th>Авторотация</th><th>Интервал между попытками дозвона (сек.)</th><th>Активна</th><th>Комментарий</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""
    return page("Кампании прозвона", body)

def provider_changes_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    rows = []
    for ch in repo.list_provider_changes({"date_from": q.get("date_from"), "date_to": q.get("date_to"), "country_id": q.get("country_id"), "provider_id": q.get("provider_id"), "route_like": q.get("route"), "reason_like": q.get("reason"), "user_id": q.get("user_id")}):
        rows.append(f"<tr><td>{esc(ch['changed_at'])}</td><td>{esc(ch['country_name'])}</td><td>{esc(ch['provider_before_name'])}</td><td>{esc(ch['route_before_name'])}</td><td>{esc(ch['provider_after_name'])}</td><td>{esc(ch['route_after_name'])}</td><td>{esc(ch['price_delta_eur'])}</td><td>{esc(ch['reason_text'])}</td><td>{esc(ch['comment'])}</td><td>{esc(ch['created_by_username'])}</td><td>{esc(ch['server_names'])}</td><td><a class='button' href='/provider-changes/{ch['id']}/edit'>✏️ Редактировать</a></td></tr>")
    route_opts = options(repo, 'routes', empty='—')
    reasons = ''.join(f"<option value='{esc(r['name'])}'>{esc(r['name'])}</option>" for r in repo.list_active_change_reasons())
    if not reasons:
        reasons = "<option value='Плохие показатели'>Плохие показатели</option>"
    body = f"""
<h1>Смена провайдеров</h1>
<details open><summary>1. Добавить смену</summary><form method="post" action="/provider-changes/create"><label>Дата/время <span class="required">*</span><input name="changed_at" value="{datetime.now().strftime('%Y-%m-%d %H:%M')}" readonly></label><label>Страна <span class="required">*</span><select name="country_id">{active_options(repo, 'countries')}</select></label><label>Провайдер до <span class="required">*</span><select name="provider_before_id">{active_options(repo, 'providers')}</select></label><label>Маршрут до <select name="route_before_id">{route_opts}</select></label><label>Провайдер после <span class="required">*</span><select name="provider_after_id">{active_options(repo, 'providers')}</select></label><label>Маршрут после <select name="route_after_id">{route_opts}</select></label><label>Серверы <span class="required">*</span>{server_checkboxes(repo)}<span class="muted">обязательно, если провайдер меняется</span></label><label>Причина замены <span class="required">*</span><select name="reason_text">{reasons}</select></label><label>Комментарий <input name="comment"></label><button>Сохранить и подготовить Telegram</button></form></details>
<fieldset><legend>2. Фильтры журнала</legend><form method="get" action="/provider-changes"><label>Дата от <input type="date" name="date_from" value="{esc(q.get('date_from'))}"></label><label>Дата до <input type="date" name="date_to" value="{esc(q.get('date_to'))}"></label><label>Страна <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label><label>Провайдер <select name="provider_id">{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label><label>Маршрут <input name="route" value="{esc(q.get('route'))}"></label><label>Причина <input name="reason" value="{esc(q.get('reason'))}"></label><label>Пользователь <select name="user_id">{options(repo, 'users', 'username', selected=q.get('user_id'), empty='Все')}</select></label><button>Поиск</button></form></fieldset>
<h2>3. Журнал изменений</h2><table><thead><tr><th>Дата/время</th><th>Страна</th><th>Провайдер до</th><th>Маршрут до</th><th>Провайдер после</th><th>Маршрут после</th><th>Разница EUR</th><th>Причина замены</th><th>Комментарий</th><th>Пользователь</th><th>Сервер</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""
    return page("Смена провайдеров", body)


def admin_page(repo: Repository) -> bytes:
    body = """
<h1>Администрирование</h1><div class="grid">
<a class="card" href="/admin/server-priorities">Приоритет по серверам</a>
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
    clauses, params = [], []
    if q.get("country_id"):
        clauses.append("srp.country_id = ?"); params.append(q["country_id"])
    if q.get("server_id"):
        clauses.append("srp.server_id = ?"); params.append(q["server_id"])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = []
    for row in repo.conn.execute(f"""
        SELECT srp.*, c.name AS country_name, s.name AS server_name,
               cp.name AS current_provider_name, pp.name AS previous_provider_name,
               cr.name AS current_route_name, pr.name AS previous_route_name,
               u.username AS changed_by_username
        FROM server_route_priorities srp
        JOIN countries c ON c.id = srp.country_id JOIN servers s ON s.id = srp.server_id
        JOIN routes cr ON cr.id = srp.current_route_id JOIN providers cp ON cp.id = cr.provider_id
        LEFT JOIN routes pr ON pr.id = srp.previous_route_id LEFT JOIN providers pp ON pp.id = pr.provider_id
        LEFT JOIN users u ON u.id = srp.changed_by
        {where}
        ORDER BY c.name, s.name
    """, params):
        current_card = f"<details><summary><span class='star'>★</span> {esc(row['current_provider_name'])}</summary><div class='card'>ГЕО: {esc(row['country_name'])}<br>Сервер: {esc(row['server_name'])}<br>Провайдер: {esc(row['current_provider_name'])}<br>Маршрут: {esc(row['current_route_name'])}<br>Тип: текущий приоритет<br>Комментарий: {esc(row['comment'])}<br>Дата: {esc(row['changed_at'])}<br>Пользователь: {esc(row['changed_by_username'])}<form method='post' action='/admin/server-priorities/{row['id']}/comment'><label>Комментарий <input name='comment' value='{esc(row['comment'])}'></label><button>Редактировать</button></form></div></details>"
        previous_card = "—" if not row['previous_provider_name'] else f"<details><summary><span class='star'>☆</span> {esc(row['previous_provider_name'])}</summary><div class='card'>ГЕО: {esc(row['country_name'])}<br>Сервер: {esc(row['server_name'])}<br>Провайдер: {esc(row['previous_provider_name'])}<br>Маршрут: {esc(row['previous_route_name'])}<br>Тип: предыдущий приоритет<br>Комментарий: {esc(row['comment'])}<br>Дата: {esc(row['changed_at'])}<br>Пользователь: {esc(row['changed_by_username'])}<form method='post' action='/admin/server-priorities/{row['id']}/comment'><label>Комментарий <input name='comment' value='{esc(row['comment'])}'></label><button>Редактировать</button></form></div></details>"
        rows.append(f"<tr><td>{esc(row['country_name'])}</td><td>{esc(row['server_name'])}</td><td>{current_card}</td><td>{previous_card}</td></tr>")
    body = f"""
<h1>Администрирование → Приоритет по серверам</h1><fieldset><legend>Фильтры</legend><form method="get" action="/admin/server-priorities"><label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label><label>Сервер <select name="server_id">{options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label><button>Поиск</button></form></fieldset>
<table><thead><tr><th>ГЕО</th><th>Сервер</th><th>Текущий приоритет</th><th>Предыдущий приоритет</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""
    return page("Приоритет по серверам", body)


def naming_rules_page(repo: Repository) -> bytes:
    rows = []
    for rule in repo.conn.execute("SELECT * FROM route_naming_rules ORDER BY is_active DESC, name"):
        rows.append(f"<tr><td>{esc(rule['name'])}</td><td>{esc(rule['template'])}</td><td>{'Да' if rule['is_active'] else 'Нет'}</td><td>{esc(rule['comment'])}</td></tr>")
    body = f"""<h1>Администрирование → Правила нейминга маршрутов</h1><p class="muted">Пока без изменений: изменение шаблона не переименовывает существующие маршруты автоматически.</p><details><summary>Добавить правило</summary><form method="post" action="/admin/naming-rules/create"><label>Название <span class="required">*</span><input name="name"></label><label>Шаблон <span class="required">*</span><input name="template" value="{{country}}/{{project_label}}/{{provider}}/{{cli_source_label}}@" size="70"></label><label>Активно <input type="checkbox" name="is_active" value="1"></label><label>Тип номера <input name="phone_type"></label><label>Тариф <input name="tariff_label"></label><label>Комментарий <input name="comment"></label><button>Сохранить</button></form></details><table><thead><tr><th>Название</th><th>Шаблон</th><th>Активен</th><th>Комментарий</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""
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
    body = f"""<h1>Администрирование → Курсы валют</h1>
<form method="post" action="/admin/currency-rates/upsert">
<label>Валюта провайдера <span class="required">*</span><select name="currency_id">{active_options(repo, 'currencies', 'code')}</select></label>
<label>1 единица валюты провайдера = <input name="rate_to_eur" placeholder="0.92"> EUR</label>
<button>Применить</button></form>
<p class="muted">Формула: Цена EUR = Цена провайдера × Курс к EUR.</p>
<table><thead><tr><th>Валюта</th><th>Курс к EUR</th><th>Дата курса</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""
    return page("Курсы валют", body)


def change_reasons_page(repo: Repository) -> bytes:
    rows = []
    for reason in repo.conn.execute("SELECT * FROM change_reasons ORDER BY is_active DESC, name"):
        rows.append(f"""<tr><td>{esc(reason['name'])}</td><td>{'Да' if reason['is_active'] else 'Нет'}</td><td>{esc(reason['description'])}</td><td><details><summary>✏️</summary><form method='post' action='/admin/change-reasons/{reason['id']}/update'><label>Название <input name='name' value='{esc(reason['name'])}'></label><label>Активна <select name='is_active'><option value='1' {'selected' if reason['is_active'] else ''}>Да</option><option value='0' {'selected' if not reason['is_active'] else ''}>Нет</option></select></label><label>Комментарий <input name='comment' value='{esc(reason['description'])}'></label><button>Сохранить</button></form></details></td></tr>""")
    return page("Причины смены провайдера", f"<h1>Администрирование → Причины смены провайдера</h1><details><summary>Добавить причину</summary><form method='post' action='/admin/change-reasons/create'><label>Название причины <span class='required'>*</span><input name='name'></label><label>Активна <select name='is_active'><option value='1'>Да</option><option value='0'>Нет</option></select></label><label>Комментарий <input name='comment'></label><button>Сохранить</button></form></details><table><thead><tr><th>Название причины</th><th>Активна</th><th>Комментарий</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>")


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
    body = f"""
<h1>Администрирование → Справочные значения</h1>
<p class='muted'>Неактивные значения остаются в таблицах, но не показываются в формах создания новых записей.</p>
<div class='dictionary-layout'>
  <aside class='dictionary-sidebar'>{''.join(cards)}</aside>
  <section class='dictionary-workspace'>
    <div class='dictionary-toolbar'><h2>Справочник: {esc(titles[active_section])}</h2><span>Всего записей: {len(source)}</span></div>
    <table><thead><tr>{header_html}</tr></thead><tbody>{''.join(rows)}</tbody></table>
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
    return page("Change log", f"<h1>Change log</h1><table><thead><tr><th>Дата</th><th>Entity</th><th>ID</th><th>Change</th><th>Кто</th><th>Summary</th></tr></thead><tbody>{''.join(rows)}</tbody></table>")


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
    ch = repo.conn.execute("SELECT * FROM provider_change_logs WHERE id = ?", (change_id,)).fetchone()
    if ch is None:
        return page("Запись не найдена", "<h1>Запись не найдена</h1>")
    selected_servers = {str(r['server_id']) for r in repo.conn.execute("SELECT server_id FROM provider_change_log_servers WHERE provider_change_log_id = ?", (change_id,))}
    server_opts = ''.join(f"<option value='{r['id']}' {'selected' if str(r['id']) in selected_servers else ''}>{esc(r['name'])}</option>" for r in repo.conn.execute("SELECT id, name FROM servers ORDER BY name"))
    reasons = ''.join(f"<option value='{esc(r['name'])}' {'selected' if r['name']==ch['reason_text'] else ''}>{esc(r['name'])}</option>" for r in repo.list_active_change_reasons())
    body = f"""<h1>Редактировать смену провайдера</h1><p><a href='/provider-changes'>← Назад</a></p>
<form method='post' action='/provider-changes/{change_id}/update'>
<label>Дата/время <span class='required'>*</span><input name='changed_at' value='{esc(ch['changed_at'])}'></label>
<label>Страна <select name='country_id'>{options(repo, 'countries', selected=ch['country_id'])}</select></label>
<label>Провайдер до <select name='provider_before_id'>{options(repo, 'providers', selected=ch['provider_before_id'])}</select></label>
<label>Маршрут до <select name='route_before_id'>{options(repo, 'routes', selected=ch['route_before_id'], empty='—')}</select></label>
<label>Провайдер после <select name='provider_after_id'>{options(repo, 'providers', selected=ch['provider_after_id'])}</select></label>
<label>Маршрут после <select name='route_after_id'>{options(repo, 'routes', selected=ch['route_after_id'], empty='—')}</select></label>
<label>Серверы {server_checkboxes(repo, selected_servers)}</label>
<label>Причина <select name='reason_text'>{reasons}</select></label>
<label>Комментарий <input name='comment' value='{esc(ch['comment'])}'></label>
<label>Пользователь <select name='created_by'>{options(repo, 'users', 'username', selected=ch['created_by'])}</select></label>
<p>Разница EUR: {esc(ch['price_delta_eur'])} — рассчитывается автоматически и не редактируется.</p>
<button onclick="return confirm('Сохранить изменения?')">Сохранить</button></form>"""
    return page("Редактировать смену", body)

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
        server_ids = [int(v) for v in parse_qs(data.get("_raw", "")).get("server_ids", []) if v]
        repo.create_provider_change(changed_at=data["changed_at"], country_id=int(data["country_id"]), provider_before_id=int(data["provider_before_id"]), provider_after_id=int(data["provider_after_id"]), route_before_id=parse_int(data.get("route_before_id")), route_after_id=parse_int(data.get("route_after_id")), reason_text=data.get("reason_text"), comment=data.get("comment"), server_ids=server_ids, created_by=ADMIN_ID)
        return "/provider-changes"
    if path.startswith("/provider-changes/") and path.endswith("/update"):
        change_id = int(path.strip("/").split("/")[1])
        server_ids = [int(v) for v in parse_qs(data.get("_raw", "")).get("server_ids", []) if v]
        provider_before_id = int(data["provider_before_id"]); provider_after_id = int(data["provider_after_id"])
        if provider_before_id != provider_after_id and not server_ids:
            raise BusinessRuleError("Сервер обязателен при смене провайдера")
        route_before_id = parse_int(data.get("route_before_id")); route_after_id = parse_int(data.get("route_after_id"))
        for route_id, provider_id, label in ((route_before_id, provider_before_id, "Маршрут до"), (route_after_id, provider_after_id, "Маршрут после")):
            if route_id:
                route = repo.conn.execute("SELECT provider_id FROM routes WHERE id = ?", (route_id,)).fetchone()
                if route and int(route["provider_id"]) != provider_id:
                    raise BusinessRuleError(f"{label} не принадлежит выбранному провайдеру")
        before_prefix = repo._route_prefix_id(route_before_id); after_prefix = repo._route_prefix_id(route_after_id)
        tariff_before = repo._current_tariff(int(data["country_id"]), provider_before_id, before_prefix)
        tariff_after = repo._current_tariff(int(data["country_id"]), provider_after_id, after_prefix)
        delta = None
        if tariff_before and tariff_after:
            from app.repository import eur_price
            delta = eur_price(tariff_after["eur_price"], "1") - eur_price(tariff_before["eur_price"], "1")
        repo.conn.execute("""UPDATE provider_change_logs SET changed_at = ?, country_id = ?, route_before_id = ?, provider_before_id = ?, provider_prefix_before_id = ?, tariff_before_id = ?, price_before_eur = ?, route_after_id = ?, provider_after_id = ?, provider_prefix_after_id = ?, tariff_after_id = ?, price_after_eur = ?, price_delta_eur = ?, provider_changed = ?, reason_text = ?, comment = ?, created_by = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                          (data["changed_at"], int(data["country_id"]), route_before_id, provider_before_id, before_prefix, tariff_before["id"] if tariff_before else None, tariff_before["eur_price"] if tariff_before else None, route_after_id, provider_after_id, after_prefix, tariff_after["id"] if tariff_after else None, tariff_after["eur_price"] if tariff_after else None, str(delta) if delta is not None else None, 1 if provider_before_id != provider_after_id else 0, data.get("reason_text"), data.get("comment"), int(data.get("created_by") or ADMIN_ID), ADMIN_ID, change_id))
        repo.conn.execute("DELETE FROM provider_change_log_servers WHERE provider_change_log_id = ?", (change_id,))
        for server_id in server_ids:
            repo.conn.execute("INSERT OR IGNORE INTO provider_change_log_servers(provider_change_log_id, server_id) VALUES (?, ?)", (change_id, server_id))
        repo._change_log("provider_change_log", change_id, "provider_change_log.updated", ADMIN_ID, new_values={"server_ids": server_ids})
        repo.conn.commit(); return "/provider-changes"
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
    if path.startswith("/admin/server-priorities/") and path.endswith("/comment"):
        priority_id = int(path.strip("/").split("/")[2])
        repo.conn.execute("UPDATE server_route_priorities SET comment = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (data.get("comment"), ADMIN_ID, priority_id))
        repo._change_log("server_route_priority", priority_id, "server_route_priority.comment_updated", ADMIN_ID, new_values={"comment": data.get("comment")})
        repo.conn.commit(); return "/admin/server-priorities"
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
