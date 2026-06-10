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
        for server_name in ("EU1", "EU2", "EU3", "EU4", "EU5", "EU6", "EU7", "EU8", "EU9"):
            repo.conn.execute("INSERT OR IGNORE INTO servers(name, is_active, comment) VALUES (?, 1, ?)", (server_name, "Demo server for MVP testing"))
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
    sancom_id = repo.create_provider("Sancom", "voip", eur_id, comment="Demo provider for MVP testing")
    miatel_id = repo.create_provider("Miatel", "voip", usdt_id, comment="Demo provider for MVP testing")
    demotel_id = repo.create_provider("DemoTel", "voip", eur_id, comment="Demo provider for MVP testing")
    sancom_0827_prefix = repo.create_prefix(sancom_id, "0827", "Demo 0827")
    sancom_0828_prefix = repo.create_prefix(sancom_id, "0828", "Demo 0828")
    miatel_prefix = repo.create_prefix(miatel_id, None, "Demo no prefix")
    demotel_prefix = repo.create_prefix(demotel_id, None, "Demo no prefix")
    repo.conn.execute("INSERT INTO currency_rates(currency_id, rate_to_eur, rate_date, updated_by, comment) VALUES (?, 1, '2026-06-07', ?, 'Demo EUR')", (eur_id, admin_id))
    repo.conn.execute("INSERT INTO currency_rates(currency_id, rate_to_eur, rate_date, updated_by, comment) VALUES (?, 0.93, '2026-06-07', ?, 'Demo USDT')", (usdt_id, admin_id))
    for reason in ("Плохие показатели", "Провайдер починил", "Обновлен пул номеров"):
        repo.conn.execute("INSERT OR IGNORE INTO change_reasons(name, description, is_active) VALUES (?, ?, 1)", (reason, reason))

    sancom_0827_route = repo.create_route(
        country_id=country_id,
        provider_id=sancom_id,
        provider_prefix_id=sancom_0827_prefix,
        name="Мексика/Sancom/Demo_0827@",
        cli_source_type="rnd",
        cli_source_label="Demo_0827",
        created_by=admin_id,
        comment="Demo route for MVP testing",
        priority_status="priority",
    )
    miatel_demo_a_route = repo.create_route(
        country_id=country_id,
        provider_id=miatel_id,
        provider_prefix_id=miatel_prefix,
        name="Мексика/Miatel/Demo_A@",
        cli_source_type="pool",
        cli_source_label="Demo_A",
        created_by=admin_id,
        comment="Demo route for MVP testing",
        priority_status="priority",
    )
    repo.create_route(country_id=country_id, provider_id=miatel_id, provider_prefix_id=miatel_prefix, name="Мексика/Miatel/Demo_B@", cli_source_type="pool", cli_source_label="Demo_B", created_by=admin_id, comment="Demo route for MVP testing", priority_status="normal")
    repo.create_route(country_id=country_id, provider_id=sancom_id, provider_prefix_id=sancom_0828_prefix, name="Мексика/Sancom/Demo_0828@", cli_source_type="rnd", cli_source_label="Demo_0828", created_by=admin_id, comment="Demo route for MVP testing", priority_status="normal")
    repo.create_route(country_id=country_id, provider_id=demotel_id, provider_prefix_id=demotel_prefix, name="Мексика/DemoTel/Demo_A@", cli_source_type="pool", cli_source_label="Demo_A", created_by=admin_id, comment="Demo route for MVP testing", priority_status="normal")
    repo.create_route(country_id=country_id, provider_id=demotel_id, provider_prefix_id=demotel_prefix, name="Мексика/DemoTel/Demo_B@", cli_source_type="pool", cli_source_label="Demo_B", created_by=admin_id, comment="Demo route for MVP testing", priority_status="normal")

    repo.create_tariff(country_id=country_id, provider_id=sancom_id, provider_prefix_id=sancom_0827_prefix, provider_currency_id=eur_id, price_in_provider_currency="2.00", conversion_rate_to_eur="1", conversion_rate_date="2026-06-07", created_by=admin_id, priority_status="priority", comment="Demo tariff for MVP testing")
    repo.create_tariff(country_id=country_id, provider_id=miatel_id, provider_prefix_id=miatel_prefix, provider_currency_id=usdt_id, price_in_provider_currency="3.00", conversion_rate_to_eur="0.93", conversion_rate_date="2026-06-07", created_by=admin_id, priority_status="priority", comment="Demo tariff for MVP testing")
    repo.create_tariff(country_id=country_id, provider_id=demotel_id, provider_prefix_id=demotel_prefix, provider_currency_id=eur_id, price_in_provider_currency="2.50", conversion_rate_to_eur="1", conversion_rate_date="2026-06-07", created_by=admin_id, priority_status="normal", comment="Demo tariff for MVP testing")

    provider_numbers = (
        (miatel_id, "525550000001"),
        (miatel_id, "525550000002"),
        (miatel_id, "525550000003"),
        (sancom_id, "525550000004"),
        (sancom_id, "525550000005"),
        (sancom_id, "525550000006"),
        (demotel_id, "525550000007"),
        (demotel_id, "525550000008"),
        (demotel_id, "525550000009"),
        (demotel_id, "525550000010"),
    )
    first_phone_id = None
    for provider_id, number in provider_numbers:
        phone_id = repo.create_phone_number(
            country_id=country_id,
            provider_id=provider_id,
            number=number,
            assignment_type="pool_number",
            status="used",
            created_by=admin_id,
            currency_id=eur_id,
            monthly_fee="1.00",
            comment="Demo number for testing.",
        )
        first_phone_id = first_phone_id or phone_id
    if first_phone_id is not None:
        repo.add_phone_to_route(route_id=miatel_demo_a_route, phone_number_id=first_phone_id, usage_type="pool_member", added_by=admin_id, comment="Demo route number link")

    server_ids = {row["name"]: row["id"] for row in repo.conn.execute("SELECT id, name FROM servers WHERE name LIKE 'EU%'")}
    for index, external_id in enumerate(("1001", "1002", "1003", "1004", "1005"), start=1):
        repo.create_calling_company(
            server_id=server_ids[f"EU{index}"],
            country_id=country_id,
            company_name=f"CC Mexico Demo {index}",
            company_id_external=external_id,
            has_autorotation=False,
            created_by=admin_id,
            is_active=True,
            line_count=10,
            dial_set_count=2,
            retry_interval_seconds=60,
            comment="Demo calling campaign for MVP testing",
        )
    repo.conn.execute("""
        INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, changed_by, created_by, comment)
        VALUES (?, ?, ?, NULL, ?, ?, ?)
    """, (country_id, server_ids["EU1"], miatel_demo_a_route, admin_id, admin_id, "Demo initial priority"))
    repo.conn.execute("""
        INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, changed_by, created_by, comment)
        VALUES (?, ?, ?, NULL, ?, ?, ?)
    """, (country_id, server_ids["EU2"], sancom_0827_route, admin_id, admin_id, "Demo initial priority"))
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


def routing_event_form(repo: Repository, event=None) -> str:
    event_at = (event["event_at"] if event else datetime.now().strftime("%Y-%m-%d %H:%M")).replace(" ", "T")[:16]
    scope = event["apply_scope"] if event else "none"
    route_opts = options(repo, "routes", selected=event["affected_route_id"] if event else None, empty="—")
    new_route_opts = options(repo, "routes", selected=event["new_route_id"] if event else None, empty="—")
    company_route_opts = options(repo, "routes", selected=event["new_company_route_id"] if event else None, empty="—")
    company_opts = select_options(repo, "SELECT id, company_id_external || ' / ' || company_name AS label FROM calling_companies ORDER BY company_id_external", selected=event["calling_company_id"] if event else None, empty="—")
    action = f"/provider-changes/{event['id']}/update" if event else "/provider-changes/create"
    submit = "Сохранить изменения" if event else "Создать событие"
    inactive_note = "<p class='muted'>Редактирование события не применяет повторно server_route_priorities. Для исправления текущего приоритета создайте новое событие.</p>" if event else ""
    old_route_field = f"<label>Старый route (только описание при редактировании) <select name='old_route_id'>{options(repo, 'routes', selected=event['old_route_id'] if event else None, empty='—')}</select></label>" if event else ""
    return f"""
<details open><summary>{'Редактировать событие' if event else '+ Добавить событие'}</summary>
<form method='post' action='{action}'>
  <fieldset><legend>Область применения</legend>
    <label class='card'><input type='radio' name='apply_scope' value='none' {'checked' if scope == 'none' else ''}> Не меняли настройки в нашей системе</label>
    <label class='card'><input type='radio' name='apply_scope' value='server_priority' {'checked' if scope == 'server_priority' else ''}> Серверный приоритет</label>
    <label class='card'><input type='radio' name='apply_scope' value='campaign_setting' {'checked' if scope == 'campaign_setting' else ''}> Настройка кампании</label>
  </fieldset>
  {inactive_note}
  <label>Дата события <span class='required'>*</span><input type='datetime-local' name='event_at' value='{esc(event_at)}' required></label>
  <label>GEO <select name='country_id'>{active_options(repo, 'countries', selected=event['country_id'] if event else None, empty='—')}</select></label>
  <label>Сервер <select name='server_id'>{active_options(repo, 'servers', selected=event['server_id'] if event else None, empty='—')}</select></label>
  <label>Провайдер <select name='provider_id'>{active_options(repo, 'providers', selected=event['provider_id'] if event else None, empty='—')}</select></label>
  <label>Маршрут/префикс <select name='affected_route_id'>{route_opts}</select></label>
  {old_route_field}
  <label>Новый route <select name='new_route_id'>{new_route_opts}</select></label>
  <label>Кампания <select name='calling_company_id'>{company_opts}</select></label>
  <label>Тип изменения кампании <select name='company_change_type'>
    <option value=''>—</option>
    {''.join(f"<option value='{v}' {'selected' if event and event['company_change_type'] == v else ''}>{v}</option>" for v in ('enable_autorotation','disable_autorotation','set_campaign_route','remove_campaign_route','change_campaign_route','set_server_priority'))}
  </select></label>
  <label>Новый режим кампании <select name='new_company_routing_mode'>
    <option value=''>Авто по типу изменения</option>
    {''.join(f"<option value='{v}' {'selected' if event and event['new_company_routing_mode'] == v else ''}>{v}</option>" for v in ('server_priority','campaign_route','autorotation','mixed'))}
  </select></label>
  <label>Новый route кампании <select name='new_company_route_id'>{company_route_opts}</select></label>
  <label>Новая авторотация <select name='new_company_has_autorotation'><option value=''>Авто</option><option value='1' {'selected' if event and event['new_company_has_autorotation'] == 1 else ''}>Да</option><option value='0' {'selected' if event and event['new_company_has_autorotation'] == 0 else ''}>Нет</option></select></label>
  <label>Причина <span class='required'>*</span><select name='reason' required>{routing_reason_options(event['reason'] if event else None)}</select></label>
  <label>Комментарий <span class='required'>*</span><textarea name='comment' rows='3' cols='60' required>{esc(event['comment'] if event else '')}</textarea></label>
  <p class='muted'>Для «Серверный приоритет» старый route подтягивается автоматически из текущего server_route_priorities при создании. Для «Настройка кампании» MVP только логирует событие и не меняет company_routing_settings.</p>
  <button>{submit}</button>
</form></details>"""


def provider_changes_page(repo: Repository, q: dict[str, str] | None = None) -> bytes:
    q = q or {}
    rows = []
    for ev in repo.list_routing_events({"country_id": q.get("country_id"), "apply_scope": q.get("apply_scope"), "server_id": q.get("server_id"), "campaign_id": q.get("campaign_id"), "provider_id": q.get("provider_id"), "include_inactive": q.get("include_inactive") == "1"}):
        route_text = ev["provider_name"] or ev["affected_route_name"] or ev["new_route_name"] or ev["old_route_name"] or "—"
        campaign = "—"
        if ev["company_id_external"] or ev["company_name"]:
            campaign = f"{esc(ev['company_id_external'])} / {esc(ev['company_name'])}"
        actions = f"<a class='button' href='/provider-changes/{ev['id']}/edit'>Редактировать</a>"
        if ev["is_active"]:
            actions += f"<details><summary>Деактивировать</summary><form method='post' action='/provider-changes/{ev['id']}/deactivate'><label>Причина <span class='required'>*</span><input name='deactivation_reason' required></label><button>Деактивировать</button></form></details>"
        rows.append(f"<tr class='{'' if ev['is_active'] else 'inactive-row'}'><td>{esc(ev['event_at'])}</td><td>{esc(ev['apply_scope'])}</td><td>{esc(ev['country_name'])}</td><td>{esc(ev['server_name'])}</td><td>{campaign}</td><td>{esc(route_text)}</td><td>{esc(ev['reason'])}</td><td>{esc(ev['comment'])}</td><td>{'Да' if ev['is_active'] else 'Нет'}</td><td class='actions'>{actions}</td></tr>")
    body = f"""
<h1>Смена провайдеров</h1>
{routing_event_form(repo)}
<fieldset><legend>Фильтры MVP</legend><form method='get' action='/provider-changes'>
<label>GEO <select name='country_id'>{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Область применения <select name='apply_scope'>{routing_scope_options(q.get('apply_scope'))}</select></label>
<label>Сервер <select name='server_id'>{options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label>
<label>Кампания ID <input name='campaign_id' value='{esc(q.get('campaign_id'))}'></label>
<label>Провайдер <select name='provider_id'>{options(repo, 'providers', selected=q.get('provider_id'), empty='Все')}</select></label>
<label><input type='checkbox' name='include_inactive' value='1' {'checked' if q.get('include_inactive') == '1' else ''}> Показывать архив/неактивные</label>
<button>Поиск</button></form></fieldset>
<h2>Журнал событий</h2>
<table><thead><tr><th>Дата события</th><th>Область применения</th><th>GEO</th><th>Сервер</th><th>Кампания</th><th>Провайдер/маршрут</th><th>Причина</th><th>Комментарий</th><th>Активна</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""
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
        blocks.append(f"""
<section class='server-priority-block'>
  <h2>Сервер: {esc(server_names[server_id])}</h2>
  <table><thead><tr><th>GEO</th><th>Текущий приоритет</th><th>Предыдущий приоритет</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</section>""")
    body = f"""
<h1>Администрирование → Приоритет по серверам</h1><fieldset><legend>Фильтры</legend><form method="get" action="/admin/server-priorities"><label>ГЕО <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label><label>Сервер <select name="server_id">{active_options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label><button>Поиск</button></form></fieldset>
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
    body = f"""
<h1>Администрирование → Схема маршрутизации кампаний</h1>
<fieldset><legend>Фильтры</legend><form method="get" action="/admin/company-routing-settings">
<label>GEO <select name="country_id">{options(repo, 'countries', selected=q.get('country_id'), empty='Все')}</select></label>
<label>Сервер <select name="server_id">{options(repo, 'servers', selected=q.get('server_id'), empty='Все')}</select></label>
<label>ID кампании <input name="company_id_external" value="{esc(q.get('company_id_external'))}"></label>
<label>Режим маршрутизации <select name="routing_mode">{routing_mode_options(q.get('routing_mode'), empty='Все')}</select></label>
<label>Активность <select name="is_active"><option value="" {'selected' if not q.get('is_active') else ''}>Все</option><option value="1" {'selected' if q.get('is_active')=='1' else ''}>Активна</option><option value="0" {'selected' if q.get('is_active')=='0' else ''}>Неактивна</option></select></label>
<label>Показывать историю <input type="checkbox" name="show_history" value="1" {'checked' if show_history else ''}></label>
<button>Поиск</button></form></fieldset>
<details open><summary>+ Добавить схему маршрутизации кампании</summary>
<form method="post" action="/admin/company-routing-settings/create">
  <label>Кампания <span class="required">*</span><select name="calling_company_id">{company_options(repo)}</select></label>
  <label>GEO <span class="required">*</span><select name="country_id">{options(repo, 'countries', selected=create_country_id)}</select></label>
  <label>Сервер <span class="required">*</span><select name="server_id">{options(repo, 'servers', selected=q.get('server_id'))}</select></label>
  <label>Режим маршрутизации <span class="required">*</span><select name="routing_mode">{routing_mode_options(q.get('routing_mode') or 'server_priority')}</select></label>
  <label>Маршрут кампании <select name="route_id">{route_options_for_country(repo, create_country_id)}</select></label>
  <label>Авторотация <input type="checkbox" name="has_autorotation" value="1"></label>
  <label>Активна <input type="checkbox" name="is_active" value="1" checked></label>
  <label>Комментарий <input name="comment"></label>
  <button>Создать</button>
</form></details>
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
<table><thead><tr><th>GEO</th><th>Сервер</th><th>ID кампании</th><th>Название кампании</th><th>Режим маршрутизации</th><th>Авторотация</th><th>Маршрут кампании</th><th>Активна</th><th>Действует с</th><th>Действует до</th><th>Комментарий</th><th>Действия</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
"""
    return page("Схема маршрутизации кампаний", body)


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
    return page("Change log", f"<h1>Change log</h1><table><thead><tr><th>Дата (UTC/server time)</th><th>Entity</th><th>ID</th><th>Change</th><th>Кто</th><th>Summary</th></tr></thead><tbody>{''.join(rows)}</tbody></table>")


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
        repo.create_routing_event(
            event_at=data.get("event_at"), apply_scope=data.get("apply_scope"), reason=data.get("reason"), comment=data.get("comment"),
            country_id=parse_int(data.get("country_id")), server_id=parse_int(data.get("server_id")), provider_id=parse_int(data.get("provider_id")),
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
            country_id=parse_int(data.get("country_id")), server_id=parse_int(data.get("server_id")), provider_id=parse_int(data.get("provider_id")),
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
