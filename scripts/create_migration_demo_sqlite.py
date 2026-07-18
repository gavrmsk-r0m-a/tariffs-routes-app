#!/usr/bin/env python3
"""Create a synthetic SQLite database for PostgreSQL migration smoke tests.

The fixture is intentionally generated outside the repository by the caller and
contains no production/user data.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db import init_db

NOW = "2026-07-12 10:00:00"
TODAY = "2026-07-12"
HISTORY_FROM = "2026-07-11 08:00:00"
STAGE43_INACTIVE_AT = "2026-07-13 09:00:00"
STAGE43_NONE_AT = "2026-07-14 10:00:00"
STAGE43_SERVER_AT = "2026-07-15 11:00:00"
STAGE43_CAMPAIGN_AT = "2026-07-16 12:00:00"
STAGE46_PHONE_AT = "2026-07-17 08:00:00"
STAGE46_REPLACE_AT = "2026-07-17 09:00:00"
STAGE46_LINK_AT = "2026-07-17 10:00:00"
STAGE46_ROUTE_AT = "2026-07-17 11:00:00"
STAGE46_TARIFF_CREATED_AT = "2026-07-17 12:00:00"
STAGE46_TARIFF_CHANGED_AT = "2026-07-17 13:00:00"


def q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    cur = conn.execute(sql, params)
    return int(cur.lastrowid)


def create_demo_sqlite(output: str | Path) -> Path:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    conn = sqlite3.connect(output)
    conn.row_factory = sqlite3.Row
    try:
        init_db(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS demo_data_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        admin_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        conn.execute(
            """INSERT INTO users(
                username, display_name, role_key, email, password_hash, password_salt,
                must_change_password, is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, 0, 0, ?, ?)""",
            ("ci-inactive", "CI Inactive", "guest", "ci-inactive@example.invalid", NOW, NOW),
        )
        country_id = q(conn, "INSERT INTO countries(name, code, is_active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)", ("Demo Country", "DC", NOW, NOW))
        eur_id = q(conn, "INSERT INTO currencies(code, name, symbol, is_active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)", ("EUR", "Euro", "€", NOW, NOW))
        provider_id = q(conn, "INSERT INTO providers(name, normalized_name, provider_type, default_currency_id, is_active, comment, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)", ("Demo Provider", "demo provider", "voip", eur_id, "Synthetic CI provider", NOW, NOW))
        prefix_id = q(conn, "INSERT INTO provider_prefixes(provider_id, prefix, name, comment, is_active, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)", (provider_id, "123", "Demo Prefix", "Synthetic prefix", NOW, NOW))
        server_id = q(conn, "INSERT INTO servers(name, comment, is_active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)", ("demo-server-1", "Synthetic CI server", NOW, NOW))
        reason_id = q(conn, "INSERT INTO change_reasons(name, description, is_active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)", ("CI smoke", "Synthetic migration smoke reason", NOW, NOW))
        phone_type_id = q(conn, "INSERT INTO phone_number_types(name, is_active, comment, created_at, updated_at) VALUES (?, 1, ?, ?, ?)", ("Mobile", "Synthetic type", NOW, NOW))
        assignment = conn.execute("SELECT code, name FROM phone_assignment_types WHERE code='gl'").fetchone()
        project = conn.execute("SELECT name FROM projects WHERE code='rep'").fetchone()

        route_id = q(conn, """
            INSERT INTO routes(country_id, provider_id, provider_prefix_id, name, project_label, cli_source_type,
                cli_source_label, aon_pool, rnd_type, rnd_pool_owner, comment, is_actual, priority_status,
                inbound_line_available, created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 1, ?, ?, ?, ?)
        """, (country_id, provider_id, prefix_id, "Demo Route", project["name"], "pool", "Demo CLI", "Demo AON", "local", "Demo Owner", "Synthetic route", "normal", admin_id, NOW, admin_id, NOW))
        rate_id = q(conn, "INSERT INTO currency_rates(currency_id, rate_to_eur, rate_date, updated_by, comment, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (eur_id, "1.000000", TODAY, admin_id, "Synthetic rate", "manual", NOW, NOW))
        phone_id = q(conn, """
            INSERT INTO phone_numbers(country_id, provider_id, country_label, provider_label, number, normalized_number,
                project_label, assignment_type, assignment_label, phone_type, tariff_label, status, connection_cost,
                monthly_fee, outgoing_rate, incoming_rate, currency_id, currency_label, comment, is_active,
                review_required, imported_created_by, created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?)
        """, (country_id, provider_id, "Demo Country", "Demo Provider", "525550000001", "525550000001", project["name"], assignment["code"], assignment["name"], "Mobile", "Demo Tariff", "used", "0", "1.25", "0.05", "0.02", eur_id, "EUR", "Synthetic phone", "ci", admin_id, NOW, admin_id, NOW))
        tariff_id = q(conn, """
            INSERT INTO tariffs(country_id, provider_id, provider_prefix_id, provider_currency_id,
                price_in_provider_currency, conversion_rate_to_eur, conversion_rate_date, currency_rate_id,
                eur_price, priority_status, is_estimated, comment, valid_from, valid_to, is_current,
                created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, 1, ?, ?, ?, ?)
        """, (country_id, provider_id, prefix_id, eur_id, "0.100000", "1.000000", TODAY, rate_id, "0.100000", "normal", "Synthetic tariff", NOW, admin_id, NOW, admin_id, NOW))
        inactive_country_id = q(conn, "INSERT INTO countries(name, code, is_active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)", ("Inactive Tariff Country", "IC", NOW, NOW))
        xts_id = q(conn, "INSERT INTO currencies(code, name, symbol, is_active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)", ("XTS", "Test Currency", "¤", NOW, NOW))
        inactive_provider_id = q(conn, "INSERT INTO providers(name, normalized_name, provider_type, default_currency_id, is_active, comment, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)", ("Inactive Tariff Provider", "inactive tariff provider", "voip", xts_id, "Synthetic inactive tariff provider", NOW, NOW))
        xpn_id = q(conn, "INSERT INTO currencies(code, name, symbol, is_active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)", ("XPN", "Phone Test Currency", "¤", NOW, NOW))
        review_country_id = q(conn, "INSERT INTO countries(name, code, is_active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)", ("CI Review Phone Country", "PR", NOW, NOW))
        review_phone_id = q(conn, """
            INSERT INTO phone_numbers(country_id, provider_id, country_label, provider_label, number, normalized_number,
                project_label, assignment_type, assignment_label, phone_type, tariff_label, status, connection_cost,
                monthly_fee, outgoing_rate, incoming_rate, currency_id, currency_label, comment, is_active,
                review_required, imported_created_by, created_by, created_at, updated_by, updated_at, deactivated_at)
            VALUES (?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?, ?, ?, ?)
        """, (review_country_id, "CI Review Phone Country", "525550000010", "525550000010", "ИТМ", "aon", "АОН", "Fixed", "CI Review Tariff", "problem", "3.500000", "4.500000", "0.150000", "0.050000", xpn_id, "XPN", "Synthetic review-required phone", "ci-review", admin_id, NOW, admin_id, NOW, NOW))
        routed_country_id = q(conn, "INSERT INTO countries(name, code, is_active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)", ("CI Routed Phone Country", "PT", NOW, NOW))
        phone_provider_id = q(conn, "INSERT INTO providers(name, normalized_name, provider_type, default_currency_id, is_active, comment, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)", ("CI Phone Provider", "ci phone provider", "voip", xpn_id, "Synthetic phone provider", NOW, NOW))
        routed_phone_id = q(conn, """
            INSERT INTO phone_numbers(country_id, provider_id, country_label, provider_label, number, normalized_number,
                project_label, assignment_type, assignment_label, phone_type, tariff_label, status, connection_cost,
                monthly_fee, outgoing_rate, incoming_rate, currency_id, currency_label, comment, is_active,
                review_required, imported_created_by, created_by, created_at, updated_by, updated_at, deactivated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, NULL)
        """, (routed_country_id, phone_provider_id, "CI Routed Phone Country", "CI Phone Provider", "525550000020", "525550000020", "CI Phone Project", "ivr", "IVR", "VoIP", "CI Routed Tariff", "free", "0.750000", "1.500000", "0.030000", "0.010000", xpn_id, "XPN", "Synthetic routed phone", "ci-routed", admin_id, NOW, admin_id, NOW))
        ci_route_ids = {}
        for route_name in ("CI Phone Route A", "CI Phone Route B", "CI Phone Route Hidden"):
            ci_route_ids[route_name] = q(conn, """
                INSERT INTO routes(country_id, provider_id, provider_prefix_id, name, project_label, cli_source_type,
                    cli_source_label, aon_pool, rnd_type, rnd_pool_owner, comment, is_actual, priority_status,
                    inbound_line_available, created_by, created_at, updated_by, updated_at)
                VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, NULL, NULL, ?, 1, ?, 0, ?, ?, ?, ?)
            """, (routed_country_id, phone_provider_id, route_name, "CI Phone Project", "pool", route_name, "Synthetic phone route", "normal", admin_id, NOW, admin_id, NOW))
        q(conn, "INSERT INTO route_phone_numbers(route_id, phone_number_id, usage_type, is_active, added_at, added_by, comment) VALUES (?, ?, ?, 1, ?, ?, ?)", (ci_route_ids["CI Phone Route A"], routed_phone_id, "cli", NOW, admin_id, "Synthetic active phone link"))
        q(conn, "INSERT INTO route_phone_numbers(route_id, phone_number_id, usage_type, is_active, added_at, added_by, comment) VALUES (?, ?, ?, 1, ?, ?, ?)", (ci_route_ids["CI Phone Route B"], routed_phone_id, "pool_member", NOW, admin_id, "Synthetic active phone link"))
        q(conn, "INSERT INTO route_phone_numbers(route_id, phone_number_id, usage_type, is_active, added_at, removed_at, added_by, removed_by, comment) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)", (ci_route_ids["CI Phone Route Hidden"], routed_phone_id, "backup_number", NOW, NOW, admin_id, admin_id, "Synthetic inactive phone link"))
        stage42_provider_after_id = q(conn, "INSERT INTO providers(name, normalized_name, provider_type, default_currency_id, is_active, comment, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)", ("CI Provider Change After", "ci provider change after", "voip", xpn_id, "Synthetic provider-change after provider", NOW, NOW))
        stage42_route_alpha_id = q(conn, """
            INSERT INTO routes(country_id, provider_id, provider_prefix_id, name, project_label, cli_source_type,
                cli_source_label, aon_pool, rnd_type, rnd_pool_owner, comment, is_actual, priority_status,
                inbound_line_available, created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, NULL, NULL, ?, 1, ?, 0, ?, ?, ?, ?)
        """, (routed_country_id, phone_provider_id, "Stage 42 Alpha", "CI Phone Project", "pool", "Stage 42 Alpha", "Synthetic provider-change route", "normal", admin_id, NOW, admin_id, NOW))
        stage42_route_beta_id = q(conn, """
            INSERT INTO routes(country_id, provider_id, provider_prefix_id, name, project_label, cli_source_type,
                cli_source_label, aon_pool, rnd_type, rnd_pool_owner, comment, is_actual, priority_status,
                inbound_line_available, created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, NULL, NULL, ?, 1, ?, 0, ?, ?, ?, ?)
        """, (routed_country_id, stage42_provider_after_id, "Stage 42 Beta", "CI Phone Project", "pool", "Stage 42 Beta", "Synthetic provider-change route", "normal", admin_id, NOW, admin_id, NOW))
        stage42_server_a_id = q(conn, "INSERT INTO servers(name, comment, is_active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)", ("Stage 42 Server A", "Synthetic provider-change server", NOW, NOW))
        stage42_server_b_id = q(conn, "INSERT INTO servers(name, comment, is_active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)", ("Stage 42 Server B", "Synthetic provider-change server", NOW, NOW))
        stage42_old_id = q(conn, """
            INSERT INTO provider_change_logs(changed_at, country_id, route_before_id, provider_before_id, route_after_id,
                provider_after_id, price_delta_eur, provider_changed, reason_id, reason_text, comment, telegram_status,
                created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("2026-07-10 09:00:00", routed_country_id, stage42_route_alpha_id, phone_provider_id, stage42_route_alpha_id, phone_provider_id, "0.000000", reason_id, "AON refresh without provider switch", "Synthetic Stage 42 old provider-change", "not_sent", admin_id, "2026-07-10 09:00:00", admin_id, "2026-07-10 09:00:00"))
        stage42_new_id = q(conn, """
            INSERT INTO provider_change_logs(changed_at, country_id, route_before_id, provider_before_id, route_after_id,
                provider_after_id, price_delta_eur, provider_changed, reason_id, reason_text, comment, telegram_status,
                created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("2026-07-12 11:00:00", routed_country_id, stage42_route_alpha_id, phone_provider_id, stage42_route_beta_id, stage42_provider_after_id, "0.000000", reason_id, "Planned provider switch", "Synthetic Stage 42 new provider-change", "not_sent", admin_id, "2026-07-12 11:00:00", admin_id, "2026-07-12 11:00:00"))
        q(conn, "INSERT INTO provider_change_log_servers(provider_change_log_id, server_id) VALUES (?, ?)", (stage42_new_id, stage42_server_b_id))
        q(conn, "INSERT INTO provider_change_log_servers(provider_change_log_id, server_id) VALUES (?, ?)", (stage42_new_id, stage42_server_a_id))
        stage43_old_tariff_id = q(conn, """
            INSERT INTO tariffs(country_id, provider_id, provider_prefix_id, provider_currency_id,
                price_in_provider_currency, conversion_rate_to_eur, conversion_rate_date, currency_rate_id,
                eur_price, priority_status, is_estimated, comment, valid_from, valid_to, is_current,
                created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, 0, ?, ?, NULL, 1, ?, ?, ?, ?)
        """, (routed_country_id, phone_provider_id, xpn_id, "2.500000", "0.400000", TODAY, "1.000000", "normal", "Synthetic Stage 43 old route tariff", NOW, admin_id, NOW, admin_id, NOW))
        stage43_new_tariff_id = q(conn, """
            INSERT INTO tariffs(country_id, provider_id, provider_prefix_id, provider_currency_id,
                price_in_provider_currency, conversion_rate_to_eur, conversion_rate_date, currency_rate_id,
                eur_price, priority_status, is_estimated, comment, valid_from, valid_to, is_current,
                created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, 0, ?, ?, NULL, 1, ?, ?, ?, ?)
        """, (routed_country_id, stage42_provider_after_id, xpn_id, "3.750000", "0.400000", TODAY, "1.500000", "normal", "Synthetic Stage 43 new route tariff", NOW, admin_id, NOW, admin_id, NOW))
        stage43_none_active_id = q(conn, """
            INSERT INTO routing_events(event_at, apply_scope, reason, country_id, server_id, provider_id,
                affected_route_id, old_route_id, new_route_id, calling_company_id, company_change_type,
                old_company_routing_mode, new_company_routing_mode, old_company_route_id, new_company_route_id,
                old_company_has_autorotation, new_company_has_autorotation, has_overflow, overflow_route_id,
                comment, snapshot_json, is_active, created_at, created_by, updated_at, updated_by)
            VALUES (?, 'none', 'Stage 43 none active', ?, NULL, ?, ?, NULL, NULL, NULL, NULL,
                NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, ?, ?, 1, ?, ?, ?, ?)
        """, (STAGE43_NONE_AT, routed_country_id, phone_provider_id, stage42_route_alpha_id, "Synthetic Stage 43 none active event", json.dumps({"stage": 43, "scope": "none", "state": "active"}, sort_keys=True), NOW, admin_id, NOW, admin_id))
        stage43_none_inactive_id = q(conn, """
            INSERT INTO routing_events(event_at, apply_scope, reason, country_id, server_id, provider_id,
                affected_route_id, old_route_id, new_route_id, calling_company_id, company_change_type,
                old_company_routing_mode, new_company_routing_mode, old_company_route_id, new_company_route_id,
                old_company_has_autorotation, new_company_has_autorotation, has_overflow, overflow_route_id,
                comment, snapshot_json, is_active, deactivation_reason, deactivated_at, deactivated_by,
                created_at, created_by, updated_at, updated_by)
            VALUES (?, 'none', 'Stage 43 none inactive', ?, NULL, ?, ?, NULL, NULL, NULL, NULL,
                NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
        """, (STAGE43_INACTIVE_AT, routed_country_id, phone_provider_id, stage42_route_alpha_id, "Synthetic Stage 43 inactive event", json.dumps({"stage": 43, "scope": "none", "state": "inactive"}, sort_keys=True), "Synthetic Stage 43 archive", NOW, admin_id, NOW, admin_id, NOW, admin_id))
        stage43_server_event_id = q(conn, """
            INSERT INTO routing_events(event_at, apply_scope, reason, country_id, server_id, provider_id,
                affected_route_id, old_route_id, new_route_id, calling_company_id, company_change_type,
                old_company_routing_mode, new_company_routing_mode, old_company_route_id, new_company_route_id,
                old_company_has_autorotation, new_company_has_autorotation, has_overflow, overflow_route_id,
                comment, snapshot_json, is_active, created_at, created_by, updated_at, updated_by)
            VALUES (?, 'server_priority', 'Stage 43 server priority', ?, ?, ?, ?, ?, ?, NULL, NULL,
                NULL, NULL, NULL, NULL, NULL, NULL, 1, ?, ?, ?, 1, ?, ?, ?, ?)
        """, (STAGE43_SERVER_AT, routed_country_id, stage42_server_a_id, stage42_provider_after_id, stage42_route_beta_id, stage42_route_alpha_id, stage42_route_beta_id, ci_route_ids["CI Phone Route B"], "Synthetic Stage 43 server priority event", json.dumps({"stage": 43, "scope": "server_priority", "affected_servers": ["Stage 42 Server A", "Stage 42 Server B"]}, sort_keys=True), NOW, admin_id, NOW, admin_id))
        q(conn, "INSERT INTO routing_event_servers(routing_event_id, server_id, old_route_id, new_route_id, server_route_priority_id, status, created_at) VALUES (?, ?, ?, ?, NULL, 'applied', ?)", (stage43_server_event_id, stage42_server_b_id, stage42_route_alpha_id, stage42_route_beta_id, NOW))
        q(conn, "INSERT INTO routing_event_servers(routing_event_id, server_id, old_route_id, new_route_id, server_route_priority_id, status, created_at) VALUES (?, ?, ?, ?, NULL, 'applied', ?)", (stage43_server_event_id, stage42_server_a_id, stage42_route_alpha_id, stage42_route_beta_id, NOW))
        q(conn, """
            INSERT INTO tariffs(country_id, provider_id, provider_prefix_id, provider_currency_id,
                price_in_provider_currency, conversion_rate_to_eur, conversion_rate_date, currency_rate_id,
                eur_price, priority_status, is_estimated, comment, valid_from, valid_to, is_current,
                created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, 1, ?, ?, ?, 0, ?, ?, ?, ?)
        """, (inactive_country_id, inactive_provider_id, xts_id, "2.500000", "0.400000", TODAY, "1.000000", "alternative", "Synthetic inactive tariff", NOW, NOW, admin_id, NOW, admin_id, NOW))
        company_id = q(conn, """
            INSERT INTO calling_companies(server_id, country_id, company_name, company_id_external, has_autorotation,
                line_count, dial_set_count, retry_interval_seconds, comment, is_active, created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, ?, ?, 1, 2, 1, 30, ?, 1, ?, ?, ?, ?)
        """, (server_id, country_id, "Demo Company", "demo-company-1", "Synthetic company", admin_id, NOW, admin_id, NOW))
        setting_id = q(conn, """
            INSERT INTO company_routing_settings(calling_company_id, country_id, server_id, route_id, routing_mode,
                has_autorotation, is_active, comment, valid_from, valid_to, created_at, created_by, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?, NULL, ?, ?, ?, ?)
        """, (company_id, country_id, server_id, route_id, "autorotation", "Synthetic setting", NOW, NOW, admin_id, NOW, admin_id))
        stage43_campaign_event_id = q(conn, """
            INSERT INTO routing_events(event_at, apply_scope, reason, country_id, server_id, provider_id,
                affected_route_id, old_route_id, new_route_id, calling_company_id, company_change_type,
                old_company_routing_mode, new_company_routing_mode, old_company_route_id, new_company_route_id,
                old_company_has_autorotation, new_company_has_autorotation, has_overflow, overflow_route_id,
                comment, snapshot_json, is_active, created_at, created_by, updated_at, updated_by)
            VALUES (?, 'campaign_setting', 'Stage 43 campaign setting', ?, NULL, NULL, NULL, NULL, NULL, ?, ?,
                ?, ?, ?, ?, 1, 1, 0, NULL, ?, ?, 1, ?, ?, ?, ?)
        """, (STAGE43_CAMPAIGN_AT, country_id, company_id, "set_campaign_route", "autorotation", "mixed", route_id, route_id, "Synthetic Stage 43 campaign event", json.dumps({"stage": 43, "scope": "campaign_setting"}, sort_keys=True), NOW, admin_id, NOW, admin_id))
        manual_country_id = q(conn, "INSERT INTO countries(name, code, is_active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)", ("CI Manual Company Country", "CM", NOW, NOW))
        manual_server_id = q(conn, "INSERT INTO servers(name, comment, is_active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)", ("ci-manual-server-1", "Synthetic manual company server", NOW, NOW))
        manual_company_id = q(conn, """
            INSERT INTO calling_companies(server_id, country_id, company_name, company_id_external, has_autorotation,
                line_count, dial_set_count, retry_interval_seconds, comment, is_active, created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, ?, ?, 1, 4, 2, 45, ?, 1, ?, ?, ?, ?)
        """, (manual_server_id, manual_country_id, "CI Manual Company", "ci-manual-company", "Synthetic active manual company", admin_id, NOW, admin_id, NOW))
        q(conn, """
            INSERT INTO company_routing_settings(calling_company_id, country_id, server_id, route_id, routing_mode,
                has_autorotation, is_active, comment, valid_from, valid_to, created_at, created_by, updated_at, updated_by)
            VALUES (?, ?, ?, NULL, ?, 0, 1, ?, ?, NULL, ?, ?, ?, ?)
        """, (manual_company_id, manual_country_id, manual_server_id, "server_priority", "Synthetic current autorotation disabled", NOW, NOW, admin_id, NOW, admin_id))
        q(conn, """
            INSERT INTO company_routing_settings(calling_company_id, country_id, server_id, route_id, routing_mode,
                has_autorotation, is_active, comment, valid_from, valid_to, created_at, created_by, updated_at, updated_by)
            VALUES (?, ?, ?, NULL, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?)
        """, (manual_company_id, manual_country_id, manual_server_id, "autorotation", "Synthetic historical autorotation setting", HISTORY_FROM, NOW, HISTORY_FROM, admin_id, NOW, admin_id))
        inactive_company_country_id = q(conn, "INSERT INTO countries(name, code, is_active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)", ("CI Inactive Company Country", "CN", NOW, NOW))
        inactive_company_server_id = q(conn, "INSERT INTO servers(name, comment, is_active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)", ("ci-inactive-server-1", "Synthetic inactive company server", NOW, NOW))
        q(conn, """
            INSERT INTO calling_companies(server_id, country_id, company_name, company_id_external, has_autorotation,
                line_count, dial_set_count, retry_interval_seconds, comment, is_active, created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, ?, ?, 0, 1, 1, 60, ?, 0, ?, ?, ?, ?)
        """, (inactive_company_server_id, inactive_company_country_id, "CI Inactive Company", "ci-inactive-company", "Synthetic inactive company", admin_id, NOW, admin_id, NOW))
        rpn_id = q(conn, "INSERT INTO route_phone_numbers(route_id, phone_number_id, usage_type, is_active, added_at, added_by, comment) VALUES (?, ?, ?, 1, ?, ?, ?)", (route_id, phone_id, "cli", NOW, admin_id, "Synthetic link"))
        event_id = q(conn, """
            INSERT INTO routing_events(event_at, apply_scope, reason, country_id, server_id, provider_id,
                affected_route_id, old_route_id, new_route_id, calling_company_id, company_change_type,
                old_company_routing_mode, new_company_routing_mode, old_company_route_id, new_company_route_id,
                old_company_has_autorotation, new_company_has_autorotation, has_overflow, overflow_route_id,
                comment, snapshot_json, is_active, created_at, created_by, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, ?, NULL, ?, 0, 1, 0, NULL, ?, ?, 1, ?, ?, ?, ?)
        """, (NOW, "campaign_setting", "CI smoke", country_id, server_id, provider_id, route_id, route_id, company_id, "enable_autorotation", "autorotation", route_id, "Synthetic event", '{"source":"ci"}', NOW, admin_id, NOW, admin_id))
        priority_id = q(conn, """
            INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, has_overflow,
                overflow_route_id, changed_at, changed_by, reason, comment, is_active, created_by, created_at, updated_by, updated_at)
            VALUES (?, ?, ?, NULL, 0, NULL, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        """, (country_id, server_id, route_id, NOW, admin_id, "CI smoke", "Synthetic priority", admin_id, NOW, admin_id, NOW))
        q(conn, "INSERT INTO routing_event_servers(routing_event_id, server_id, old_route_id, new_route_id, server_route_priority_id, status, created_at) VALUES (?, ?, NULL, ?, ?, ?, ?)", (event_id, server_id, route_id, priority_id, "applied", NOW))
        q(conn, "INSERT INTO route_history(route_id, action, changed_by, changed_at, field_name, old_value, new_value, reason, comment) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (route_id, "create", admin_id, NOW, "name", None, "Demo Route", "CI smoke", "Synthetic history"))
        q(conn, "INSERT INTO route_phone_number_history(route_id, phone_number_id, old_phone_number_id, new_phone_number_id, action, changed_by, changed_at, old_values, new_values, reason, comment) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)", (route_id, phone_id, phone_id, "attach", admin_id, NOW, "plain old", "plain new", "CI smoke", "Synthetic history"))
        q(conn, "INSERT INTO phone_number_history(phone_number_id, action, changed_by, changed_at, field_name, old_value, new_value, reason, comment) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (phone_id, "create", admin_id, NOW, "status", None, "used", "CI smoke", "Synthetic history"))
        q(conn, "INSERT INTO phone_number_history(phone_number_id, action, changed_by, changed_at, field_name, old_value, new_value, reason, comment) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (phone_id, "updated", admin_id, STAGE46_PHONE_AT, "status", "problem", "used", "Stage 46 phone status", "Synthetic Stage 46 phone history"))
        q(conn, "INSERT INTO route_phone_number_history(route_id, phone_number_id, old_phone_number_id, new_phone_number_id, action, changed_by, changed_at, old_values, new_values, reason, comment) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (route_id, phone_id, routed_phone_id, "replaced", admin_id, STAGE46_REPLACE_AT, json.dumps({"number": "525550000001"}, sort_keys=True), json.dumps({"number": "525550000020"}, sort_keys=True), "Stage 46 phone replaced", "Synthetic Stage 46 replacement history"))
        q(conn, "INSERT INTO route_phone_number_history(route_id, phone_number_id, old_phone_number_id, new_phone_number_id, action, changed_by, changed_at, old_values, new_values, reason, comment) VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?)", (route_id, phone_id, "added", admin_id, STAGE46_LINK_AT, None, json.dumps({"usage_type": "cli"}, sort_keys=True), "Stage 46 phone linked", "Synthetic Stage 46 route-phone history"))
        q(conn, "INSERT INTO route_history(route_id, action, changed_by, changed_at, field_name, old_value, new_value, reason, comment) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (route_id, "updated", admin_id, STAGE46_ROUTE_AT, "comment", "Temporary Stage 46 route comment", "Synthetic route", "Stage 46 route comment", "Synthetic Stage 46 route history"))
        q(conn, """
            INSERT INTO tariff_change_history(tariff_id, changed_at, changed_by, country_id, country_name_snapshot,
                provider_id, provider_name_snapshot, provider_prefix_id, prefix_snapshot, old_provider_currency_id,
                new_provider_currency_id, old_price_in_provider_currency, new_price_in_provider_currency,
                old_conversion_rate_to_eur, new_conversion_rate_to_eur, old_conversion_rate_date,
                new_conversion_rate_date, old_eur_price, new_eur_price, eur_price_delta, reason, comment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, NULL, ?, NULL, ?, NULL, ?, ?, ?, ?, ?)
        """, (tariff_id, NOW, admin_id, country_id, "Demo Country", provider_id, "Demo Provider", prefix_id, "123", eur_id, "0.100000", "1.000000", TODAY, "0.100000", "0.000000", "CI smoke", "Synthetic tariff history", NOW))
        q(conn, """
            INSERT INTO tariff_change_history(tariff_id, changed_at, changed_by, country_id, country_name_snapshot,
                provider_id, provider_name_snapshot, provider_prefix_id, prefix_snapshot, old_provider_currency_id,
                new_provider_currency_id, old_price_in_provider_currency, new_price_in_provider_currency,
                old_conversion_rate_to_eur, new_conversion_rate_to_eur, old_conversion_rate_date,
                new_conversion_rate_date, old_eur_price, new_eur_price, eur_price_delta, reason, comment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, NULL, ?, NULL, ?, NULL, ?, NULL, ?, ?, ?)
        """, (tariff_id, STAGE46_TARIFF_CREATED_AT, admin_id, country_id, "Demo Country", provider_id, "Demo Provider", prefix_id, "123", eur_id, "0.200000", "1.000000", TODAY, "0.200000", "tariff.created", "Synthetic Stage 46 tariff created", STAGE46_TARIFF_CREATED_AT))
        q(conn, """
            INSERT INTO tariff_change_history(tariff_id, changed_at, changed_by, country_id, country_name_snapshot,
                provider_id, provider_name_snapshot, provider_prefix_id, prefix_snapshot, old_provider_currency_id,
                new_provider_currency_id, old_price_in_provider_currency, new_price_in_provider_currency,
                old_conversion_rate_to_eur, new_conversion_rate_to_eur, old_conversion_rate_date,
                new_conversion_rate_date, old_eur_price, new_eur_price, eur_price_delta, reason, comment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tariff_id, STAGE46_TARIFF_CHANGED_AT, admin_id, country_id, "Demo Country", provider_id, "Demo Provider", prefix_id, "123", eur_id, eur_id, "0.200000", "0.100000", "1.000000", "1.000000", TODAY, TODAY, "0.200000", "0.100000", "-0.100000", "tariff.changed", "Synthetic Stage 46 tariff changed", STAGE46_TARIFF_CHANGED_AT))
        q(conn, "INSERT INTO change_log(entity_type, entity_id, change_type, changed_by, changed_at, old_values, new_values, summary, comment, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("route", route_id, "create", admin_id, NOW, '{"name":null}', '{"name":"Demo Route"}', "Created demo route", "Synthetic change log", "ci", NOW))
        q(conn, "INSERT INTO route_naming_rules(name, template, is_active, comment, created_by, created_at, updated_by, updated_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?)", ("Demo rule", "{country}-{provider}", "Synthetic naming rule", admin_id, NOW, admin_id, NOW))
        q(conn, "INSERT INTO import_jobs(entity_type, mode, file_name, status, total_rows, new_rows, duplicate_rows, skipped_rows, updated_rows, replaced_rows, error_rows, preview_data, summary, error_report, created_by, created_at, started_at, finished_at) VALUES (?, ?, ?, ?, 1, 1, 0, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?)", ("routes", "append_update", "demo.csv", "completed", '[{"row":1}]', '{"inserted":1}', '{"errors":[]}', admin_id, NOW, NOW, NOW))
        conn.execute("INSERT INTO app_settings(key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)", ("demo_setting", "enabled", NOW, admin_id))
        conn.execute("INSERT INTO app_settings(key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)", ("hlr_daily_limit_override", "2500", NOW, admin_id))
        conn.execute("INSERT INTO hlr_daily_usage(usage_date, checked_count, credits_spent, last_check_count, last_check_credits, updated_at) VALUES (?, 1, ?, 1, ?, ?)", (TODAY, "0.500000", "0.500000", NOW))
        conn.execute("INSERT OR REPLACE INTO user_permissions(user_id, section_key, can_read, can_write, can_export) VALUES (?, ?, 1, 1, 1)", (admin_id, "routes"))
        conn.execute("INSERT INTO demo_data_state(key, value, updated_at) VALUES (?, ?, ?)", ("demo_data_version", "ci-smoke", NOW))
        conn.commit()
    finally:
        conn.close()
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create synthetic SQLite migration demo database")
    parser.add_argument("--output", required=True, help="Output SQLite path, preferably outside the repository")
    args = parser.parse_args(argv)
    path = create_demo_sqlite(args.output)
    print(f"Created migration demo SQLite database: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
