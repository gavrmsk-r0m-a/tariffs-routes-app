from __future__ import annotations

import re
import sqlite3
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

PHONE_RE = re.compile(r"^[1-9][0-9]{6,20}$")
VALID_PHONE_STATUSES = {"used", "free", "problem", "unknown"}
OLD_PHONE_STATUS_MAP = {"reserved": "free", "blocked": "problem", "disabled": "problem"}


def normalize_phone_status(status: str | None) -> str:
    normalized = (status or "").strip().lower()
    if normalized in VALID_PHONE_STATUSES:
        return normalized
    return OLD_PHONE_STATUS_MAP.get(normalized, "unknown")

ROUTING_SCOPE_LABELS = {
    "none": "Не меняли настройки в нашей системе",
    "server_priority": "Серверный приоритет",
    "campaign_setting": "Настройка кампании",
}

COMPANY_CHANGE_LABELS = {
    "enable_autorotation": "Включили авторотацию",
    "disable_autorotation": "Выключили авторотацию",
    "set_campaign_route": "Прописали ручной маршрут",
    "remove_campaign_route": "Убрали ручной маршрут",
}



class BusinessRuleError(ValueError):
    """Raised when a confirmed MVP business rule is violated."""


@dataclass(frozen=True)
class PhoneLinkResult:
    route_phone_number_id: int
    phone_number_id: int


def normalize_provider_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def validate_phone_number(number: str) -> str:
    if not PHONE_RE.fullmatch(number):
        raise BusinessRuleError(
            "Phone number must be in international format without +, 00, spaces, brackets, or other symbols"
        )
    return number


def eur_price(price: str | Decimal, rate: str | Decimal) -> Decimal:
    value = Decimal(str(price)) * Decimal(str(rate))
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def query_filters(filters: dict | None, mapping: dict[str, str]) -> tuple[str, list]:
    if not filters:
        return "", []
    clauses: list[str] = []
    params: list = []
    for key, column in mapping.items():
        value = filters.get(key)
        if value in (None, "", "all"):
            continue
        if key.endswith("_like"):
            clauses.append(f"{column} LIKE ?")
            params.append(f"%{value}%")
        else:
            clauses.append(f"{column} = ?")
            params.append(value)
    return (" WHERE " + " AND ".join(clauses), params) if clauses else ("", [])


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _user_columns(self) -> set[str]:
        return {row[1] for row in self.conn.execute("PRAGMA table_info(users)")}

    def _role_key(self, role: str) -> str:
        normalized = (role or "operator").strip().lower()
        if normalized in {"admin", "operator", "guest"}:
            return normalized
        if normalized == "user":
            return "operator"
        return normalized or "operator"

    def create_user(self, username: str, role: str = "admin", display_name: str | None = None) -> int:
        username = username.strip()
        existing = self.conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            return int(existing["id"])
        columns = self._user_columns()
        role_key = self._role_key(role)
        insert_columns = ["username", "display_name", "is_active"]
        values: list[object] = [username, display_name or username, 1]
        if "role_key" in columns:
            insert_columns.append("role_key")
            values.append(role_key)
        if "role" in columns:
            insert_columns.append("role")
            values.append("Admin" if role_key == "admin" else "User")
        if "password_hash" in columns:
            insert_columns.append("password_hash")
            values.append("")
        if "auth_provider" in columns:
            insert_columns.append("auth_provider")
            values.append("local")
        placeholders = ", ".join("?" for _ in insert_columns)
        cur = self.conn.execute(
            f"INSERT INTO users({', '.join(insert_columns)}) VALUES ({placeholders})",
            tuple(values),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_users(self, active_only: bool = False) -> list[sqlite3.Row]:
        where = "WHERE is_active = 1" if active_only else ""
        role_expr = "role_key" if "role_key" in self._user_columns() else "LOWER(role)"
        return list(self.conn.execute(
            f"""
            SELECT id, username, COALESCE(NULLIF(display_name, ''), username) AS display_name,
                   {role_expr} AS role_key, is_active, created_at, updated_at
            FROM users
            {where}
            ORDER BY is_active DESC, display_name COLLATE NOCASE, username COLLATE NOCASE
            """
        ))

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        role_expr = "role_key" if "role_key" in self._user_columns() else "LOWER(role)"
        return self.conn.execute(
            f"""
            SELECT id, username, COALESCE(NULLIF(display_name, ''), username) AS display_name,
                   {role_expr} AS role_key, is_active, created_at, updated_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()

    def update_user(self, user_id: int, *, display_name: str, role_key: str, is_active: bool) -> None:
        columns = self._user_columns()
        assignments = ["display_name = ?", "is_active = ?", "updated_at = CURRENT_TIMESTAMP"]
        values: list[object] = [display_name.strip(), 1 if is_active else 0]
        normalized_role = self._role_key(role_key)
        if "role_key" in columns:
            assignments.append("role_key = ?")
            values.append(normalized_role)
        if "role" in columns:
            assignments.append("role = ?")
            values.append("Admin" if normalized_role == "admin" else "User")
        values.append(user_id)
        self.conn.execute(f"UPDATE users SET {', '.join(assignments)} WHERE id = ?", tuple(values))
        self.conn.commit()

    def create_country(self, name: str, code: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO countries(name, code, is_active) VALUES (?, ?, 1)",
            (name, code),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def create_currency(self, code: str, name: str, symbol: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO currencies(code, name, symbol, is_active) VALUES (?, ?, ?, 1)",
            (code, name, symbol),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def create_provider(
        self,
        name: str,
        provider_type: str = "unknown",
        default_currency_id: int | None = None,
        comment: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO providers(name, normalized_name, provider_type, default_currency_id, is_active, comment)
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (name, normalize_provider_name(name), provider_type, default_currency_id, comment),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def create_prefix(self, provider_id: int, prefix: str | None, name: str | None = None) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO provider_prefixes(provider_id, prefix, name, is_active)
            VALUES (?, ?, ?, 1)
            """,
            (provider_id, prefix or None, name),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def create_route(
        self,
        *,
        country_id: int,
        provider_id: int,
        name: str,
        cli_source_type: str,
        cli_source_label: str,
        created_by: int,
        provider_prefix_id: int | None = None,
        project_label: str | None = None,
        comment: str | None = None,
        is_actual: bool = True,
        priority_status: str = "unknown",
        inbound_line_available: bool = False,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO routes(
                country_id, provider_id, provider_prefix_id, name, project_label,
                cli_source_type, cli_source_label, comment, is_actual, priority_status,
                inbound_line_available, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                country_id,
                provider_id,
                provider_prefix_id,
                name,
                project_label,
                cli_source_type,
                cli_source_label,
                comment,
                1 if is_actual else 0,
                priority_status,
                1 if inbound_line_available else 0,
                created_by,
            ),
        )
        route_id = int(cur.lastrowid)
        self.conn.execute(
            """
            INSERT INTO route_history(route_id, action, changed_by, field_name, new_value, comment)
            VALUES (?, 'created', ?, 'route', ?, ?)
            """,
            (route_id, created_by, name, comment),
        )
        self._change_log("route", route_id, "route.created", created_by, new_values={"name": name})
        self.conn.commit()
        return route_id

    def create_phone_number(
        self,
        *,
        country_id: int,
        number: str,
        assignment_type: str,
        status: str,
        created_by: int,
        phone_type: str | None = None,
        tariff_label: str | None = None,
        provider_id: int | None = None,
        project_label: str | None = None,
        connection_cost: str | None = None,
        monthly_fee: str | None = None,
        outgoing_rate: str | None = None,
        incoming_rate: str | None = None,
        currency_id: int | None = None,
        comment: str | None = None,
        is_active: bool = True,
        review_required: bool = False,
        created_at: str | None = None,
        deactivated_at: str | None = None,
    ) -> int:
        normalized = validate_phone_number(number)
        if not is_active and deactivated_at is None:
            deactivated_at = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = self.conn.execute(
            """
            INSERT INTO phone_numbers(
                country_id, provider_id, number, normalized_number, project_label,
                assignment_type, phone_type, tariff_label, status, connection_cost, monthly_fee, outgoing_rate,
                incoming_rate, currency_id, comment, is_active, review_required, created_by, created_at, deactivated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?)
            """,
            (
                country_id,
                provider_id,
                number,
                normalized,
                project_label,
                assignment_type,
                phone_type,
                tariff_label,
                normalize_phone_status(status),
                connection_cost,
                monthly_fee,
                outgoing_rate,
                incoming_rate,
                currency_id,
                comment,
                1 if is_active else 0,
                1 if review_required else 0,
                created_by,
                created_at,
                deactivated_at,
            ),
        )
        phone_id = int(cur.lastrowid)
        self.conn.execute(
            """
            INSERT INTO phone_number_history(phone_number_id, action, changed_by, field_name, new_value, comment)
            VALUES (?, 'created', ?, 'number', ?, ?)
            """,
            (phone_id, created_by, number, comment),
        )
        self._change_log("phone_number", phone_id, "phone_number.created", created_by, new_values={"number": number})
        self.conn.commit()
        return phone_id

    def add_phone_to_route(
        self,
        *,
        route_id: int,
        phone_number_id: int,
        usage_type: str,
        added_by: int,
        comment: str | None = None,
    ) -> PhoneLinkResult:
        phone = self.conn.execute(
            "SELECT id, number, is_active, status FROM phone_numbers WHERE id = ?",
            (phone_number_id,),
        ).fetchone()
        if phone is None:
            raise BusinessRuleError("Phone number not found")
        if int(phone["is_active"]) != 1:
            raise BusinessRuleError("Нельзя добавить номер в маршрут: номер не активен у провайдера")

        cur = self.conn.execute(
            """
            INSERT INTO route_phone_numbers(route_id, phone_number_id, usage_type, is_active, added_by, comment)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (route_id, phone_number_id, usage_type, added_by, comment),
        )
        link_id = int(cur.lastrowid)
        self.conn.execute(
            """
            INSERT INTO route_phone_number_history(
                route_id, phone_number_id, action, changed_by, new_values, comment
            )
            VALUES (?, ?, 'added', ?, ?, ?)
            """,
            (route_id, phone_number_id, added_by, f'{{"usage_type": "{usage_type}"}}', comment),
        )
        self._change_log(
            "route_phone_number",
            link_id,
            "route_phone_number.added",
            added_by,
            new_values={"route_id": route_id, "phone_number_id": phone_number_id, "usage_type": usage_type},
        )
        self.conn.commit()
        return PhoneLinkResult(route_phone_number_id=link_id, phone_number_id=phone_number_id)

    def create_calling_company(
        self,
        *,
        server_id: int,
        country_id: int,
        company_name: str,
        company_id_external: str,
        has_autorotation: bool,
        created_by: int,
        comment: str | None = None,
        is_active: bool = True,
        line_count: int = 0,
        dial_set_count: int = 0,
        retry_interval_seconds: int = 0,
    ) -> int:
        if not company_id_external.strip():
            raise BusinessRuleError("Company external ID is required")
        cur = self.conn.execute(
            """
            INSERT INTO calling_companies(
                server_id, country_id, company_name, company_id_external,
                has_autorotation, line_count, dial_set_count, retry_interval_seconds,
                comment, is_active, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                server_id,
                country_id,
                company_name,
                company_id_external.strip(),
                1 if has_autorotation else 0,
                int(line_count),
                int(dial_set_count),
                int(retry_interval_seconds),
                comment,
                1 if is_active else 0,
                created_by,
            ),
        )
        company_id = int(cur.lastrowid)
        self._change_log(
            "calling_company",
            company_id,
            "calling_company.created",
            created_by,
            new_values={"company_id_external": company_id_external.strip(), "company_name": company_name},
        )
        self.conn.commit()
        return company_id

    def create_server(self, name: str, comment: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO servers(name, comment, is_active) VALUES (?, ?, 1)",
            (name, comment),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_routes(self, filters: dict | None = None) -> list[sqlite3.Row]:
        where, params = query_filters(
            filters,
            {
                "country_id": "r.country_id",
                "provider_id": "r.provider_id",
                "prefix_id": "COALESCE(r.provider_prefix_id, 0)",
                "is_actual": "r.is_actual",
                "search_like": "r.name",
            },
        )
        return list(
            self.conn.execute(
                f"""
                SELECT r.*, c.name AS country_name, p.name AS provider_name, pp.prefix AS prefix,
                    (SELECT COUNT(*) FROM route_phone_numbers rpn WHERE rpn.route_id = r.id AND rpn.is_active = 1) AS phone_count
                FROM routes r
                JOIN countries c ON c.id = r.country_id
                JOIN providers p ON p.id = r.provider_id
                LEFT JOIN provider_prefixes pp ON pp.id = r.provider_prefix_id
                {where}
                ORDER BY c.name, r.name
                """,
                params,
            )
        )

    def list_phone_numbers(self, filters: dict | None = None) -> list[sqlite3.Row]:
        where, params = query_filters(
            filters,
            {
                "country_id": "pn.country_id",
                "provider_id": "COALESCE(pn.provider_id, 0)",
                "project": "pn.project_label",
                "project_like": "pn.project_label",
                "assignment_type": "pn.assignment_type",
                "status": "pn.status",
                "number_like": "pn.number",
            },
        )
        return list(
            self.conn.execute(
                f"""
                SELECT pn.*, c.name AS country_name, p.name AS provider_name, cur.code AS currency_code,
                    pat.name AS assignment_type_label,
                    (SELECT COUNT(*) FROM route_phone_numbers rpn WHERE rpn.phone_number_id = pn.id AND rpn.is_active = 1) AS route_count
                FROM phone_numbers pn
                JOIN countries c ON c.id = pn.country_id
                LEFT JOIN providers p ON p.id = pn.provider_id
                LEFT JOIN currencies cur ON cur.id = pn.currency_id
                LEFT JOIN phone_assignment_types pat ON pat.code = pn.assignment_type
                {where}
                ORDER BY pn.number
                """,
                params,
            )
        )

    def route_numbers(self, route_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT rpn.id AS link_id, pn.id AS phone_id, pn.number, pn.status, pn.assignment_type, pn.connection_cost, pn.monthly_fee,
                    pn.outgoing_rate, pn.incoming_rate, pn.comment AS phone_comment, rpn.comment AS link_comment
                FROM route_phone_numbers rpn
                JOIN phone_numbers pn ON pn.id = rpn.phone_number_id
                WHERE rpn.route_id = ?
                  AND rpn.is_active = 1
                  AND pn.is_active = 1
                ORDER BY pn.number
                """,
                (route_id,),
            )
        )

    def add_phone_to_route_by_number(
        self,
        *,
        route_id: int,
        number: str,
        usage_type: str,
        added_by: int,
        comment: str | None = None,
    ) -> PhoneLinkResult:
        normalized = validate_phone_number(number)
        phone = self.conn.execute(
            "SELECT id FROM phone_numbers WHERE number = ? OR normalized_number = ?",
            (normalized, normalized),
        ).fetchone()
        if phone is None:
            raise BusinessRuleError("Номер не найден в справочнике купленных номеров")
        existing = self.conn.execute(
            "SELECT id FROM route_phone_numbers WHERE route_id = ? AND phone_number_id = ? AND is_active = 1",
            (route_id, phone["id"]),
        ).fetchone()
        if existing:
            raise BusinessRuleError("Номер уже добавлен в этот маршрут")
        return self.add_phone_to_route(
            route_id=route_id,
            phone_number_id=int(phone["id"]),
            usage_type=usage_type,
            added_by=added_by,
            comment=comment,
        )

    def remove_phone_links_from_route(self, *, route_id: int, link_ids: list[int], removed_by: int, reason: str | None = None) -> int:
        removed = 0
        for link_id in link_ids:
            link = self.conn.execute(
                "SELECT id, phone_number_id FROM route_phone_numbers WHERE id = ? AND route_id = ? AND is_active = 1",
                (link_id, route_id),
            ).fetchone()
            if not link:
                continue
            self.conn.execute(
                "UPDATE route_phone_numbers SET is_active = 0, removed_at = CURRENT_TIMESTAMP, removed_by = ? WHERE id = ?",
                (removed_by, link_id),
            )
            self.conn.execute(
                "INSERT INTO route_phone_number_history(route_id, phone_number_id, action, changed_by, reason) VALUES (?, ?, 'removed', ?, ?)",
                (route_id, link["phone_number_id"], removed_by, reason),
            )
            self._change_log("route_phone_number", link_id, "route_phone_number.removed", removed_by, summary=reason)
            removed += 1
        self.conn.commit()
        return removed


    def update_phone_number(
        self,
        phone_id: int,
        *,
        country_id: int,
        provider_id: int | None,
        number: str,
        assignment_type: str,
        status: str,
        is_active: bool,
        updated_by: int,
        project_label: str | None = None,
        connection_cost: str | None = None,
        monthly_fee: str | None = None,
        currency_id: int | None = None,
        phone_type: str | None = None,
        tariff_label: str | None = None,
        comment: str | None = None,
        review_required: bool = False,
    ) -> None:
        existing = self.conn.execute("SELECT * FROM phone_numbers WHERE id = ?", (phone_id,)).fetchone()
        if existing is None:
            raise BusinessRuleError("Phone number not found")
        normalized = validate_phone_number(number)
        old_values = dict(existing)
        requested_active = 1 if is_active else 0
        forced_review_required = 1 if (requested_active == 1 and existing["deactivated_at"] is not None) else 0
        final_review_required = 1 if review_required or forced_review_required else 0
        final_status = normalize_phone_status(status)
        if requested_active == 0 and int(existing["is_active"]) == 1 and existing["status"] == "used":
            final_status = "problem"
        self.conn.execute(
            """
            UPDATE phone_numbers
            SET number = ?, normalized_number = ?, country_id = ?, provider_id = ?, project_label = ?,
                assignment_type = ?, status = ?, is_active = ?, connection_cost = ?, monthly_fee = ?,
                currency_id = ?, phone_type = ?, tariff_label = ?, comment = ?, review_required = ?,
                deactivated_at = CASE WHEN ? = 0 AND deactivated_at IS NULL THEN CURRENT_TIMESTAMP ELSE deactivated_at END,
                updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                normalized, normalized, country_id, provider_id, project_label, assignment_type, final_status,
                requested_active, connection_cost, monthly_fee, currency_id, phone_type, tariff_label, comment,
                final_review_required, requested_active, updated_by, phone_id,
            ),
        )
        if requested_active == 0:
            links = list(self.conn.execute(
                "SELECT id, route_id, phone_number_id FROM route_phone_numbers WHERE phone_number_id = ? AND is_active = 1",
                (phone_id,),
            ))
            for link in links:
                self.conn.execute(
                    "UPDATE route_phone_numbers SET is_active = 0, removed_at = CURRENT_TIMESTAMP, removed_by = ? WHERE id = ?",
                    (updated_by, link["id"]),
                )
                self.conn.execute(
                    """
                    INSERT INTO route_phone_number_history(route_id, phone_number_id, action, changed_by, old_values, new_values, reason)
                    VALUES (?, ?, 'removed', ?, ?, ?, ?)
                    """,
                    (link["route_id"], link["phone_number_id"], updated_by,
                     json.dumps({"is_active": 1}, ensure_ascii=False),
                     json.dumps({"is_active": 0, "removed_by": updated_by}, ensure_ascii=False),
                     "phone_number.deactivated"),
                )
                self._change_log(
                    "route_phone_number",
                    int(link["id"]),
                    "route_phone_number.removed_by_phone_deactivation",
                    updated_by,
                    old_values={"is_active": 1},
                    new_values={"is_active": 0, "phone_number_id": phone_id},
                    summary="Phone provider deactivation closed active route link",
                )
        self.conn.execute(
            "INSERT INTO phone_number_history(phone_number_id, action, changed_by, field_name, old_value, new_value, comment) VALUES (?, 'updated', ?, 'phone', ?, ?, ?)",
            (phone_id, updated_by, json.dumps(old_values, ensure_ascii=False), json.dumps({"number": normalized, "status": final_status, "is_active": requested_active, "review_required": final_review_required}, ensure_ascii=False), comment),
        )
        self._change_log(
            "phone_number",
            phone_id,
            "phone_number.updated",
            updated_by,
            old_values={"number": existing["number"], "status": existing["status"], "is_active": existing["is_active"], "review_required": existing["review_required"]},
            new_values={"number": normalized, "status": final_status, "is_active": requested_active, "review_required": final_review_required},
        )
        self.conn.commit()

    def create_tariff(
        self,
        *,
        country_id: int,
        provider_id: int,
        provider_currency_id: int,
        price_in_provider_currency: str,
        conversion_rate_to_eur: str,
        conversion_rate_date: str,
        created_by: int,
        provider_prefix_id: int | None = None,
        currency_rate_id: int | None = None,
        priority_status: str = "unknown",
        is_estimated: bool = False,
        comment: str | None = None,
    ) -> int:
        price_eur = eur_price(price_in_provider_currency, conversion_rate_to_eur)
        cur = self.conn.execute(
            """
            INSERT INTO tariffs(
                country_id, provider_id, provider_prefix_id, provider_currency_id,
                price_in_provider_currency, conversion_rate_to_eur, conversion_rate_date,
                currency_rate_id, eur_price, priority_status, is_estimated, comment,
                is_current, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                country_id,
                provider_id,
                provider_prefix_id,
                provider_currency_id,
                str(price_in_provider_currency),
                str(conversion_rate_to_eur),
                conversion_rate_date,
                currency_rate_id,
                str(price_eur),
                priority_status,
                1 if is_estimated else 0,
                comment,
                created_by,
            ),
        )
        tariff_id = int(cur.lastrowid)
        country = self.conn.execute("SELECT name FROM countries WHERE id = ?", (country_id,)).fetchone()
        provider = self.conn.execute("SELECT name FROM providers WHERE id = ?", (provider_id,)).fetchone()
        prefix = None
        if provider_prefix_id:
            prefix = self.conn.execute("SELECT prefix FROM provider_prefixes WHERE id = ?", (provider_prefix_id,)).fetchone()
        self.conn.execute(
            """
            INSERT INTO tariff_change_history(
                tariff_id, changed_by, country_id, country_name_snapshot,
                provider_id, provider_name_snapshot, provider_prefix_id, prefix_snapshot,
                new_provider_currency_id, new_price_in_provider_currency,
                new_conversion_rate_to_eur, new_conversion_rate_date, new_eur_price, comment
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tariff_id,
                created_by,
                country_id,
                country["name"] if country else "",
                provider_id,
                provider["name"] if provider else "",
                provider_prefix_id,
                prefix["prefix"] if prefix else None,
                provider_currency_id,
                str(price_in_provider_currency),
                str(conversion_rate_to_eur),
                conversion_rate_date,
                str(price_eur),
                comment,
            ),
        )
        self._change_log("tariff", tariff_id, "tariff.created", created_by, new_values={"eur_price": str(price_eur)})
        self.conn.commit()
        return tariff_id

    def latest_currency_rate(self, currency_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM currency_rates
            WHERE currency_id = ?
            ORDER BY rate_date DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (currency_id,),
        ).fetchone()

    def list_tariffs(self, filters: dict | None = None) -> list[sqlite3.Row]:
        filters = dict(filters or {})
        status = filters.pop("status", "active")
        where, params = query_filters(
            filters,
            {
                "country_id": "t.country_id",
                "provider_id": "t.provider_id",
                "priority_status": "t.priority_status",
            },
        )
        clauses = []
        if where:
            clauses.append(where[7:])
        if status == "active":
            clauses.append("t.is_current = 1")
        elif status == "inactive":
            clauses.append("t.is_current = 0")
        final_where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return list(
            self.conn.execute(
                f"""
                SELECT t.*, c.name AS country_name, p.name AS provider_name, pp.prefix AS prefix,
                       cur.code AS currency_code
                FROM tariffs t
                JOIN countries c ON c.id = t.country_id
                JOIN providers p ON p.id = t.provider_id
                LEFT JOIN provider_prefixes pp ON pp.id = t.provider_prefix_id
                JOIN currencies cur ON cur.id = t.provider_currency_id
                {final_where}
                ORDER BY c.name, p.name, COALESCE(pp.prefix, '')
                """,
                params,
            )
        )

    def list_calling_companies(self, filters: dict | None = None) -> list[sqlite3.Row]:
        where, params = query_filters(
            filters,
            {
                "server_id": "cc.server_id",
                "country_id": "cc.country_id",
                "company_like": "cc.company_name",
                "external_id_like": "cc.company_id_external",
                "has_autorotation": "cc.has_autorotation",
                "is_active": "cc.is_active",
            },
        )
        return list(
            self.conn.execute(
                f"""
                SELECT cc.*, s.name AS server_name, c.name AS country_name
                FROM calling_companies cc
                JOIN servers s ON s.id = cc.server_id
                JOIN countries c ON c.id = cc.country_id
                {where}
                ORDER BY c.name, s.name, cc.company_name
                """,
                params,
            )
        )

    def _validate_company_routing_values(
        self,
        *,
        calling_company_id: int,
        country_id: int,
        server_id: int,
        route_id: int | None,
        routing_mode: str,
        has_autorotation: bool,
    ) -> None:
        if routing_mode not in {"server_priority", "campaign_route", "autorotation", "mixed"}:
            raise BusinessRuleError("Некорректный режим маршрутизации")
        if routing_mode == "campaign_route" and not route_id:
            raise BusinessRuleError("Для режима campaign_route обязателен маршрут")
        if routing_mode == "autorotation" and not has_autorotation:
            raise BusinessRuleError("Для режима autorotation должна быть включена авторотация")
        company = self.conn.execute("SELECT id, country_id, server_id FROM calling_companies WHERE id = ?", (calling_company_id,)).fetchone()
        if not company:
            raise BusinessRuleError("Кампания прозвона не найдена")
        if int(company["country_id"]) != int(country_id):
            raise BusinessRuleError("GEO схемы маршрутизации должен совпадать с GEO выбранной кампании")
        if int(company["server_id"]) != int(server_id):
            raise BusinessRuleError("Сервер схемы маршрутизации должен совпадать с сервером выбранной кампании")
        if not self.conn.execute("SELECT id FROM countries WHERE id = ?", (country_id,)).fetchone():
            raise BusinessRuleError("GEO не найден")
        if not self.conn.execute("SELECT id FROM servers WHERE id = ?", (server_id,)).fetchone():
            raise BusinessRuleError("Сервер не найден")
        if route_id:
            route = self.conn.execute("SELECT id, country_id FROM routes WHERE id = ?", (route_id,)).fetchone()
            if not route:
                raise BusinessRuleError("Маршрут не найден")
            if int(route["country_id"]) != int(country_id):
                raise BusinessRuleError("Маршрут кампании должен относиться к выбранному GEO")

    def _company_routing_summary(
        self,
        *,
        calling_company_id: int,
        country_id: int,
        server_id: int,
        old_values: dict | None = None,
        new_values: dict | None = None,
    ) -> str:
        company = self.conn.execute("SELECT company_id_external, company_name FROM calling_companies WHERE id = ?", (calling_company_id,)).fetchone()
        country = self.conn.execute("SELECT name FROM countries WHERE id = ?", (country_id,)).fetchone()
        server = self.conn.execute("SELECT name FROM servers WHERE id = ?", (server_id,)).fetchone()

        def route_label(route_id: int | None) -> str:
            if not route_id:
                return "—"
            route = self.conn.execute("SELECT name FROM routes WHERE id = ?", (route_id,)).fetchone()
            return f"{route_id} / {route['name']}" if route else str(route_id)

        parts = [
            f"GEO: {country['name'] if country else country_id}",
            f"Сервер: {server['name'] if server else server_id}",
            f"ID кампании: {company['company_id_external'] if company else calling_company_id}",
            f"Кампания: {company['company_name'] if company else calling_company_id}",
        ]
        if old_values:
            parts.extend([
                f"Старый routing_mode: {old_values.get('routing_mode')}",
                f"Старый route: {route_label(old_values.get('route_id'))}",
                f"Старая авторотация: {old_values.get('has_autorotation')}",
            ])
            if old_values.get("valid_to"):
                parts.append(f"valid_to старой версии: {old_values['valid_to']}")
        if new_values:
            parts.extend([
                f"Новый routing_mode: {new_values.get('routing_mode')}",
                f"Новый route: {route_label(new_values.get('route_id'))}",
                f"Новая авторотация: {new_values.get('has_autorotation')}",
            ])
            if new_values.get("valid_from"):
                parts.append(f"valid_from новой версии: {new_values['valid_from']}")
        return "; ".join(parts)

    def list_company_routing_settings(self, filters: dict | None = None) -> list[sqlite3.Row]:
        filters = filters or {}
        include_history = filters.get("include_history") or filters.get("show_history")
        clauses: list[str] = []
        params: list = []
        if not include_history:
            clauses.extend(["crs.is_active = 1", "crs.valid_to IS NULL"])
        for key, column in {
            "country_id": "crs.country_id",
            "server_id": "crs.server_id",
            "routing_mode": "crs.routing_mode",
            "calling_company_id": "crs.calling_company_id",
            "company_id_external": "cc.company_id_external",
        }.items():
            value = filters.get(key)
            if value in (None, "", "all"):
                continue
            if key == "company_id_external":
                clauses.append(f"{column} LIKE ?")
                params.append(f"%{value}%")
            else:
                clauses.append(f"{column} = ?")
                params.append(value)
        if include_history and filters.get("is_active") not in (None, "", "all"):
            clauses.append("crs.is_active = ?")
            params.append(filters["is_active"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return list(
            self.conn.execute(
                f"""
                SELECT crs.*, c.name AS country_name, s.name AS server_name,
                       cc.company_id_external, cc.company_name,
                       r.name AS route_name, p.name AS provider_name,
                       u.username AS updated_by_username
                FROM company_routing_settings crs
                JOIN calling_companies cc ON cc.id = crs.calling_company_id
                JOIN countries c ON c.id = crs.country_id
                JOIN servers s ON s.id = crs.server_id
                LEFT JOIN routes r ON r.id = crs.route_id
                LEFT JOIN providers p ON p.id = r.provider_id
                LEFT JOIN users u ON u.id = crs.updated_by
                {where}
                ORDER BY c.name, s.name, cc.company_name, crs.valid_from DESC, crs.id DESC
                """,
                params,
            )
        )

    def create_company_routing_setting(
        self,
        *,
        calling_company_id: int,
        country_id: int,
        server_id: int,
        route_id: int | None,
        routing_mode: str,
        has_autorotation: bool,
        comment: str | None,
        created_by: int,
        effective_at: str | None = None,
    ) -> int:
        self._validate_company_routing_values(
            calling_company_id=calling_company_id,
            country_id=country_id,
            server_id=server_id,
            route_id=route_id,
            routing_mode=routing_mode,
            has_autorotation=has_autorotation,
        )
        if self.conn.execute(
            "SELECT id FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL",
            (calling_company_id,),
        ).fetchone():
            raise BusinessRuleError("У кампании уже есть активная схема маршрутизации")
        now = effective_at or self.conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        cur = self.conn.execute(
            """
            INSERT INTO company_routing_settings(
                calling_company_id, country_id, server_id, route_id, routing_mode,
                has_autorotation, is_active, comment, valid_from, created_by, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (calling_company_id, country_id, server_id, route_id, routing_mode, 1 if has_autorotation else 0, comment, now, created_by, created_by),
        )
        setting_id = int(cur.lastrowid)
        new_values = {
            "routing_mode": routing_mode,
            "route_id": route_id,
            "has_autorotation": 1 if has_autorotation else 0,
            "country_id": country_id,
            "server_id": server_id,
            "valid_from": now,
        }
        self._change_log(
            "company_routing_setting",
            setting_id,
            "company_routing_setting.created",
            created_by,
            new_values=new_values,
            summary=self._company_routing_summary(calling_company_id=calling_company_id, country_id=country_id, server_id=server_id, new_values=new_values),
        )
        self.conn.commit()
        return setting_id

    def update_company_routing_setting(
        self,
        *,
        setting_id: int,
        country_id: int,
        server_id: int,
        route_id: int | None,
        routing_mode: str,
        has_autorotation: bool,
        comment: str | None,
        updated_by: int,
        effective_at: str | None = None,
    ) -> int:
        existing = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (setting_id,)).fetchone()
        if not existing:
            raise BusinessRuleError("Схема маршрутизации кампании не найдена")
        if not existing["is_active"] or existing["valid_to"] is not None:
            raise BusinessRuleError("Можно редактировать только активную схему маршрутизации")
        self._validate_company_routing_values(
            calling_company_id=existing["calling_company_id"],
            country_id=country_id,
            server_id=server_id,
            route_id=route_id,
            routing_mode=routing_mode,
            has_autorotation=has_autorotation,
        )
        new_autorotation = 1 if has_autorotation else 0
        routing_changed = any(
            int(existing[key]) != int(value) if key in {"country_id", "server_id"} else existing[key] != value
            for key, value in {
                "country_id": country_id,
                "server_id": server_id,
                "route_id": route_id,
                "routing_mode": routing_mode,
                "has_autorotation": new_autorotation,
            }.items()
        )
        old_values = {
            "routing_mode": existing["routing_mode"],
            "route_id": existing["route_id"],
            "has_autorotation": existing["has_autorotation"],
            "country_id": existing["country_id"],
            "server_id": existing["server_id"],
            "comment": existing["comment"],
        }
        if not routing_changed:
            self.conn.execute(
                "UPDATE company_routing_settings SET comment = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
                (comment, updated_by, setting_id),
            )
            self._change_log(
                "company_routing_setting",
                setting_id,
                "company_routing_setting.updated",
                updated_by,
                old_values=old_values,
                new_values={**old_values, "comment": comment},
                summary=self._company_routing_summary(
                    calling_company_id=existing["calling_company_id"],
                    country_id=country_id,
                    server_id=server_id,
                    old_values=old_values,
                    new_values={**old_values, "comment": comment},
                ),
            )
            self.conn.commit()
            return setting_id

        now = effective_at or self.conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        self.conn.execute(
            """
            UPDATE company_routing_settings
            SET valid_to = ?, is_active = 0, updated_at = CURRENT_TIMESTAMP, updated_by = ?
            WHERE id = ?
            """,
            (now, updated_by, setting_id),
        )
        cur = self.conn.execute(
            """
            INSERT INTO company_routing_settings(
                calling_company_id, country_id, server_id, route_id, routing_mode,
                has_autorotation, is_active, comment, valid_from, created_by, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (existing["calling_company_id"], country_id, server_id, route_id, routing_mode, new_autorotation, comment, now, updated_by, updated_by),
        )
        new_id = int(cur.lastrowid)
        closed_old_values = {**old_values, "valid_to": now}
        new_values = {
            "routing_mode": routing_mode,
            "route_id": route_id,
            "has_autorotation": new_autorotation,
            "country_id": country_id,
            "server_id": server_id,
            "comment": comment,
            "valid_from": now,
        }
        self._change_log(
            "company_routing_setting",
            new_id,
            "company_routing_setting.version_created",
            updated_by,
            old_values=closed_old_values,
            new_values=new_values,
            summary=self._company_routing_summary(
                calling_company_id=existing["calling_company_id"],
                country_id=country_id,
                server_id=server_id,
                old_values=closed_old_values,
                new_values=new_values,
            ),
        )
        self.conn.commit()
        return new_id

    def deactivate_company_routing_setting(self, *, setting_id: int, updated_by: int, effective_at: str | None = None) -> None:
        existing = self.conn.execute("SELECT * FROM company_routing_settings WHERE id = ?", (setting_id,)).fetchone()
        if not existing:
            raise BusinessRuleError("Схема маршрутизации кампании не найдена")
        if not existing["is_active"] or existing["valid_to"] is not None:
            raise BusinessRuleError("Схема маршрутизации уже неактивна")
        now = effective_at or self.conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        self.conn.execute(
            """
            UPDATE company_routing_settings
            SET valid_to = ?, is_active = 0, updated_at = CURRENT_TIMESTAMP, updated_by = ?
            WHERE id = ?
            """,
            (now, updated_by, setting_id),
        )
        old_values = {
            "routing_mode": existing["routing_mode"],
            "route_id": existing["route_id"],
            "has_autorotation": existing["has_autorotation"],
            "country_id": existing["country_id"],
            "server_id": existing["server_id"],
            "comment": existing["comment"],
            "valid_to": now,
        }
        self._change_log(
            "company_routing_setting",
            setting_id,
            "company_routing_setting.deactivated",
            updated_by,
            old_values=old_values,
            summary=self._company_routing_summary(
                calling_company_id=existing["calling_company_id"],
                country_id=existing["country_id"],
                server_id=existing["server_id"],
                old_values=old_values,
            ),
        )
        self.conn.commit()


    ROUTING_EVENT_REASONS = (
        "Задача руководства",
        "Массовые отбои / занято",
        "Плохой дозвон",
        "Провайдер не отвечает",
        "Авария у провайдера",
        "Тест нового маршрута",
        "Плановое переключение",
        "Обновление пула / АОН",
        "Проблема с префиксом",
        "Другое",
    )

    def _require_text(self, value: str | None, message: str) -> str:
        if not value or not value.strip():
            raise BusinessRuleError(message)
        return value.strip()

    def _name_by_id(self, table: str, row_id: int | None, column: str = "name") -> str | None:
        if not row_id:
            return None
        row = self.conn.execute(f"SELECT {column} FROM {table} WHERE id = ?", (row_id,)).fetchone()
        return row[column] if row else None

    def _route_label(self, route_id: int | None) -> str | None:
        if not route_id:
            return None
        row = self.conn.execute(
            """
            SELECT r.name, p.name AS provider_name
            FROM routes r JOIN providers p ON p.id = r.provider_id
            WHERE r.id = ?
            """,
            (route_id,),
        ).fetchone()
        return f"{row['provider_name']} / {row['name']}" if row else str(route_id)

    def _active_company_routing_setting(self, calling_company_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT *
            FROM company_routing_settings
            WHERE calling_company_id = ? AND is_active = 1 AND valid_to IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (calling_company_id,),
        ).fetchone()

    def _company_routing_mode_for_state(self, route_id: int | None, has_autorotation: bool) -> str:
        if route_id and has_autorotation:
            return "mixed"
        if route_id:
            return "campaign_route"
        if has_autorotation:
            return "autorotation"
        return "server_priority"

    def _company_setting_state_requires_active_version(self, route_id: int | None, has_autorotation: bool) -> bool:
        return bool(route_id) or bool(has_autorotation)

    def _company_old_state(self, calling_company_id: int) -> dict:
        setting = self._active_company_routing_setting(calling_company_id)
        if setting:
            return {
                "routing_mode": setting["routing_mode"],
                "route_id": setting["route_id"],
                "has_autorotation": bool(setting["has_autorotation"]),
            }
        return {"routing_mode": "server_priority", "route_id": None, "has_autorotation": False}

    def _routing_event_snapshot(self, values: dict) -> dict:
        company = None
        if values.get("calling_company_id"):
            company = self.conn.execute(
                "SELECT company_id_external, company_name FROM calling_companies WHERE id = ?",
                (values.get("calling_company_id"),),
            ).fetchone()
        snapshot = {
            "country_name": self._name_by_id("countries", values.get("country_id")),
            "server_name": self._name_by_id("servers", values.get("server_id")),
            "provider_name": self._name_by_id("providers", values.get("provider_id")),
            "affected_route_name": self._route_label(values.get("affected_route_id")),
            "old_route_name": self._route_label(values.get("old_route_id")),
            "new_route_name": self._route_label(values.get("new_route_id")),
            "calling_company_external_id": company["company_id_external"] if company else None,
            "calling_company_name": company["company_name"] if company else None,
            "old_company_routing_mode": values.get("old_company_routing_mode"),
            "new_company_routing_mode": values.get("new_company_routing_mode"),
            "comment": values.get("comment"),
            "reason": values.get("reason"),
            "apply_scope": values.get("apply_scope"),
        }
        if values.get("apply_scope") == "server_priority":
            snapshot["affected_servers"] = values.get("affected_servers", [])
        return snapshot

    def _routing_event_summary(self, values: dict) -> str:
        scope = values.get("apply_scope")
        parts = [
            f"Дата события: {values.get('event_at')}",
            f"Область: {ROUTING_SCOPE_LABELS.get(scope, scope)}",
        ]
        if values.get("country_id"):
            parts.append(f"GEO: {self._name_by_id('countries', values.get('country_id')) or '—'}")
        if scope == "none":
            if values.get("provider_id"):
                parts.append(f"Провайдер: {self._name_by_id('providers', values.get('provider_id'))}")
            if values.get("affected_route_id"):
                parts.append(f"Маршрут/префикс: {self._route_label(values.get('affected_route_id'))}")
        elif scope == "server_priority":
            if values.get("server_id"):
                parts.append(f"Сервер: {self._name_by_id('servers', values.get('server_id'))}")
            parts.append(f"Маршрут: {self._route_label(values.get('old_route_id')) or '—'} → {self._route_label(values.get('new_route_id')) or '—'}")
        elif scope == "campaign_setting":
            if values.get("calling_company_id"):
                company = self.conn.execute("SELECT company_id_external, company_name FROM calling_companies WHERE id = ?", (values.get("calling_company_id"),)).fetchone()
                if company:
                    parts.append(f"Кампания: {company['company_id_external']} / {company['company_name']}")
            if values.get("company_change_type"):
                parts.append(f"Тип изменения кампании: {COMPANY_CHANGE_LABELS.get(values.get('company_change_type'), values.get('company_change_type'))}")
            if values.get("old_company_routing_mode") or values.get("new_company_routing_mode"):
                parts.append(f"Режим кампании: {values.get('old_company_routing_mode') or '—'} → {values.get('new_company_routing_mode') or '—'}")
            if values.get("old_company_route_id") or values.get("new_company_route_id"):
                parts.append(f"Маршрут кампании: {self._route_label(values.get('old_company_route_id')) or '—'} → {self._route_label(values.get('new_company_route_id')) or '—'}")
            if values.get("old_company_has_autorotation") is not None or values.get("new_company_has_autorotation") is not None:
                old_auto = 'Да' if values.get("old_company_has_autorotation") else 'Нет'
                new_auto = 'Да' if values.get("new_company_has_autorotation") else 'Нет'
                parts.append(f"Авторотация: {old_auto} → {new_auto}")
        parts.append(f"Причина: {values.get('reason')}")
        parts.append(f"Комментарий: {values.get('comment')}")
        return "; ".join(parts)


    def _server_priority_apply_summary(self, *, country_id: int, server_id: int, old_route_id: int | None, new_route_id: int, previous_after_update: int | None, comment: str) -> str:
        return "; ".join([
            f"GEO: {self._name_by_id('countries', country_id) or country_id}",
            f"Сервер: {self._name_by_id('servers', server_id) or server_id}",
            f"Старый current route: {self._route_label(old_route_id) or '—'}",
            f"Новый current route: {self._route_label(new_route_id) or new_route_id}",
            f"previous route after update: {self._route_label(previous_after_update) or '—'}",
            f"Комментарий: {comment}",
        ])

    def _normalize_server_priority_server_ids(self, *, server_id: int | None, server_ids) -> list[int]:
        raw_ids = server_ids if server_ids is not None else ([server_id] if server_id else [])
        normalized: list[int] = []
        for raw_id in raw_ids:
            if raw_id in (None, ""):
                continue
            sid = int(raw_id)
            if sid not in normalized:
                normalized.append(sid)
        if not normalized:
            raise BusinessRuleError("Сервер обязателен для серверного приоритета")
        return normalized

    def _server_priority_affected_servers(self, *, country_id: int, server_ids: list[int], new_route_id: int) -> list[dict]:
        affected = []
        for server_id in server_ids:
            server = self.conn.execute("SELECT id, name, is_active FROM servers WHERE id = ?", (server_id,)).fetchone()
            if not server:
                raise BusinessRuleError("Сервер не найден")
            if not server["is_active"]:
                raise BusinessRuleError("Нельзя выбрать неактивный сервер")
            current = self.conn.execute(
                "SELECT id, current_route_id FROM server_route_priorities WHERE country_id = ? AND server_id = ?",
                (country_id, server_id),
            ).fetchone()
            old_route_id = current["current_route_id"] if current else None
            affected.append({
                "server_id": server_id,
                "server_name": server["name"],
                "old_route_id": old_route_id,
                "old_route": self._route_label(old_route_id),
                "new_route_id": new_route_id,
                "new_route": self._route_label(new_route_id),
                "server_route_priority_id": current["id"] if current else None,
                "status": "skipped_noop" if old_route_id is not None and int(old_route_id) == int(new_route_id) else "applied",
            })
        if all(row["status"] == "skipped_noop" for row in affected):
            raise BusinessRuleError("Выбранный маршрут уже установлен для всех выбранных серверов")
        return affected

    def _upsert_company_routing_setting_from_event(self, values: dict, *, updated_by: int) -> None:
        active = self._active_company_routing_setting(values["calling_company_id"])
        if active is None:
            self.create_company_routing_setting(
                calling_company_id=values["calling_company_id"],
                country_id=values["country_id"],
                server_id=values["server_id"],
                route_id=values["new_company_route_id"],
                routing_mode=values["new_company_routing_mode"],
                has_autorotation=bool(values["new_company_has_autorotation"]),
                comment=values["comment"],
                created_by=updated_by,
                effective_at=values["event_at"],
            )
            return

        self.update_company_routing_setting(
            setting_id=active["id"],
            country_id=values["country_id"],
            server_id=values["server_id"],
            route_id=values["new_company_route_id"],
            routing_mode=values["new_company_routing_mode"],
            has_autorotation=bool(values["new_company_has_autorotation"]),
            comment=values["comment"],
            updated_by=updated_by,
            effective_at=values["event_at"],
        )

    def _deactivate_company_routing_setting_from_event(self, values: dict, *, updated_by: int) -> None:
        active = self._active_company_routing_setting(values["calling_company_id"])
        if active is not None:
            self.deactivate_company_routing_setting(
                setting_id=active["id"],
                updated_by=updated_by,
                effective_at=values["event_at"],
            )

    def _apply_campaign_setting_event(self, values: dict, *, updated_by: int) -> None:
        if self._company_setting_state_requires_active_version(
            values["new_company_route_id"], bool(values["new_company_has_autorotation"])
        ):
            self._upsert_company_routing_setting_from_event(values, updated_by=updated_by)
        else:
            self._deactivate_company_routing_setting_from_event(values, updated_by=updated_by)

    def create_routing_event(self, **kwargs) -> int:
        apply_scope = kwargs.get("apply_scope")
        if apply_scope not in {"none", "server_priority", "campaign_setting"}:
            raise BusinessRuleError("Некорректная область применения")
        values = {
            "event_at": self._require_text(kwargs.get("event_at"), "Дата события обязательна").replace("T", " "),
            "apply_scope": apply_scope,
            "reason": self._require_text(kwargs.get("reason"), "Причина обязательна"),
            "comment": self._require_text(kwargs.get("comment"), "Комментарий обязателен"),
            "country_id": kwargs.get("country_id"),
            "server_id": kwargs.get("server_id"),
            "server_ids": kwargs.get("server_ids"),
            "provider_id": kwargs.get("provider_id"),
            "affected_route_id": kwargs.get("affected_route_id"),
            "old_route_id": kwargs.get("old_route_id"),
            "new_route_id": kwargs.get("new_route_id"),
            "calling_company_id": kwargs.get("calling_company_id"),
            "company_change_type": kwargs.get("company_change_type"),
            "old_company_routing_mode": kwargs.get("old_company_routing_mode"),
            "new_company_routing_mode": kwargs.get("new_company_routing_mode"),
            "old_company_route_id": kwargs.get("old_company_route_id"),
            "new_company_route_id": kwargs.get("new_company_route_id"),
            "old_company_has_autorotation": kwargs.get("old_company_has_autorotation"),
            "new_company_has_autorotation": kwargs.get("new_company_has_autorotation"),
        }
        created_by = kwargs.get("created_by")
        if not created_by:
            raise BusinessRuleError("Пользователь обязателен")

        if apply_scope == "none":
            if not values["provider_id"]:
                raise BusinessRuleError("Провайдер обязателен")
            if values["affected_route_id"]:
                route = self.conn.execute("SELECT country_id, provider_id FROM routes WHERE id = ?", (values["affected_route_id"],)).fetchone()
                if not route:
                    raise BusinessRuleError("Маршрут/префикс не найден")
                if int(route["provider_id"]) != int(values["provider_id"]):
                    raise BusinessRuleError("Маршрут/префикс должен относиться к выбранному провайдеру")
                if values["country_id"] and int(route["country_id"]) != int(values["country_id"]):
                    raise BusinessRuleError("Маршрут/префикс должен относиться к выбранному GEO")
            for field in (
                "server_id", "old_route_id", "new_route_id", "calling_company_id", "company_change_type",
                "old_company_routing_mode", "new_company_routing_mode", "old_company_route_id", "new_company_route_id",
                "old_company_has_autorotation", "new_company_has_autorotation",
            ):
                values[field] = None
            values["server_ids"] = None
            values["affected_servers"] = None
        elif apply_scope == "server_priority":
            if not values["country_id"] or not values["new_route_id"]:
                raise BusinessRuleError("GEO, сервер и новый маршрут обязательны для серверного приоритета")
            server_ids = self._normalize_server_priority_server_ids(server_id=values["server_id"], server_ids=values.get("server_ids"))
            route = self.conn.execute("SELECT country_id, provider_id FROM routes WHERE id = ?", (values["new_route_id"],)).fetchone()
            if not route:
                raise BusinessRuleError("Новый маршрут не найден")
            if int(route["country_id"]) != int(values["country_id"]):
                raise BusinessRuleError("Новый маршрут должен относиться к выбранному GEO")
            if values["provider_id"] and int(route["provider_id"]) != int(values["provider_id"]):
                raise BusinessRuleError("Новый маршрут должен относиться к выбранному новому провайдеру")
            values["provider_id"] = route["provider_id"]
            values["server_ids"] = server_ids
            values["server_id"] = server_ids[0] if len(server_ids) == 1 else None
            values["affected_servers"] = self._server_priority_affected_servers(
                country_id=values["country_id"], server_ids=server_ids, new_route_id=values["new_route_id"]
            )
            values["old_route_id"] = values["affected_servers"][0]["old_route_id"] if len(server_ids) == 1 else None
        else:
            if not values["calling_company_id"] or not values["company_change_type"]:
                raise BusinessRuleError("Кампания и тип изменения обязательны")
            company = self.conn.execute("SELECT country_id, server_id FROM calling_companies WHERE id = ?", (values["calling_company_id"],)).fetchone()
            if not company:
                raise BusinessRuleError("Кампания прозвона не найдена")
            values["country_id"] = values["country_id"] or company["country_id"]
            values["server_id"] = values["server_id"] or company["server_id"]
            old_state = self._company_old_state(values["calling_company_id"])
            values["old_company_routing_mode"] = old_state["routing_mode"]
            values["old_company_route_id"] = old_state["route_id"]
            values["old_company_has_autorotation"] = 1 if old_state["has_autorotation"] else 0
            ctype = values["company_change_type"]
            if ctype == "enable_autorotation":
                values["new_company_route_id"] = old_state["route_id"]
                values["new_company_has_autorotation"] = 1
            elif ctype == "disable_autorotation":
                values["new_company_route_id"] = old_state["route_id"]
                values["new_company_has_autorotation"] = 0
            elif ctype == "set_campaign_route":
                if not values["new_company_route_id"]:
                    raise BusinessRuleError("Новый маршрут кампании обязателен")
                route = self.conn.execute("SELECT country_id, provider_id FROM routes WHERE id = ?", (values["new_company_route_id"],)).fetchone()
                if not route or int(route["country_id"]) != int(values["country_id"]):
                    raise BusinessRuleError("Маршрут кампании должен относиться к выбранному GEO")
                if values["provider_id"] and int(route["provider_id"]) != int(values["provider_id"]):
                    raise BusinessRuleError("Маршрут кампании должен относиться к выбранному провайдеру")
                if old_state["route_id"] and int(values["new_company_route_id"]) == int(old_state["route_id"]):
                    raise BusinessRuleError("Новый маршрут кампании должен отличаться от текущего")
                if not values["provider_id"]:
                    values["provider_id"] = route["provider_id"]
                values["new_company_has_autorotation"] = values["old_company_has_autorotation"]
            elif ctype == "remove_campaign_route":
                values["new_company_route_id"] = None
                values["new_company_has_autorotation"] = values["old_company_has_autorotation"]
            else:
                raise BusinessRuleError("Некорректный тип изменения кампании")
            values["new_company_routing_mode"] = self._company_routing_mode_for_state(
                values["new_company_route_id"], bool(values["new_company_has_autorotation"])
            )
            values["server_ids"] = None
            values["affected_servers"] = None

        values["snapshot_json"] = json.dumps(self._routing_event_snapshot(values), ensure_ascii=False)
        cur = self.conn.execute(
            """
            INSERT INTO routing_events(
                event_at, apply_scope, reason, country_id, server_id, provider_id, affected_route_id,
                old_route_id, new_route_id, calling_company_id, company_change_type,
                old_company_routing_mode, new_company_routing_mode, old_company_route_id, new_company_route_id,
                old_company_has_autorotation, new_company_has_autorotation, comment, snapshot_json,
                created_by, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["event_at"], values["apply_scope"], values["reason"], values["country_id"], values["server_id"],
                values["provider_id"], values["affected_route_id"], values["old_route_id"], values["new_route_id"],
                values["calling_company_id"], values["company_change_type"], values["old_company_routing_mode"],
                values["new_company_routing_mode"], values["old_company_route_id"], values["new_company_route_id"],
                values["old_company_has_autorotation"], values["new_company_has_autorotation"], values["comment"],
                values["snapshot_json"], created_by, created_by,
            ),
        )
        event_id = int(cur.lastrowid)
        self._change_log("routing_event", event_id, "routing_event.created", created_by, new_values=values, summary=self._routing_event_summary(values))

        if apply_scope == "server_priority":
            for affected in values["affected_servers"]:
                priority_id = affected["server_route_priority_id"]
                previous_after = affected["old_route_id"]
                if affected["status"] == "applied":
                    if priority_id:
                        self.conn.execute(
                            """
                            UPDATE server_route_priorities
                            SET previous_route_id = current_route_id, current_route_id = ?, changed_at = ?,
                                changed_by = ?, reason = ?, comment = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (values["new_route_id"], values["event_at"], created_by, values["reason"], values["comment"], created_by, priority_id),
                        )
                    else:
                        cur2 = self.conn.execute(
                            """
                            INSERT INTO server_route_priorities(
                                country_id, server_id, current_route_id, previous_route_id, changed_at,
                                changed_by, reason, comment, created_by, updated_by
                            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                            """,
                            (values["country_id"], affected["server_id"], values["new_route_id"], values["event_at"], created_by, values["reason"], values["comment"], created_by, created_by),
                        )
                        priority_id = int(cur2.lastrowid)
                    affected["server_route_priority_id"] = priority_id
                    self._change_log(
                        "server_route_priority",
                        priority_id,
                        "routing_event.applied_to_server_priority",
                        created_by,
                        old_values={"current_route_id": affected["old_route_id"]},
                        new_values={"current_route_id": values["new_route_id"], "previous_route_id": previous_after, "routing_event_id": event_id},
                        summary=self._server_priority_apply_summary(country_id=values["country_id"], server_id=affected["server_id"], old_route_id=affected["old_route_id"], new_route_id=values["new_route_id"], previous_after_update=previous_after, comment=values["comment"]),
                    )
                self.conn.execute(
                    """
                    INSERT INTO routing_event_servers(
                        routing_event_id, server_id, old_route_id, new_route_id, server_route_priority_id, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (event_id, affected["server_id"], affected["old_route_id"], values["new_route_id"], priority_id, affected["status"]),
                )
        elif apply_scope == "campaign_setting":
            self._apply_campaign_setting_event(values, updated_by=created_by)
        self.conn.commit()
        return event_id

    def list_routing_events(self, filters: dict | None = None) -> list[sqlite3.Row]:
        filters = filters or {}
        clauses = []
        params: list = []
        if not filters.get("include_inactive"):
            clauses.append("re.is_active = 1")
        for key, column in {
            "country_id": "re.country_id",
            "apply_scope": "re.apply_scope",
            "calling_company_id": "re.calling_company_id",
            "provider_id": "re.provider_id",
        }.items():
            if filters.get(key):
                clauses.append(f"{column} = ?")
                params.append(filters[key])
        if filters.get("server_id"):
            clauses.append(
                """
                re.apply_scope = 'server_priority'
                AND (
                    re.server_id = ?
                    OR EXISTS (
                        SELECT 1
                        FROM routing_event_servers res
                        WHERE res.routing_event_id = re.id
                          AND res.server_id = ?
                    )
                )
                """
            )
            params.extend([filters["server_id"], filters["server_id"]])
        if filters.get("campaign_id"):
            clauses.append("cc.company_id_external LIKE ?")
            params.append(f"%{filters['campaign_id']}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return list(self.conn.execute(f"""
            SELECT re.*, c.name AS country_name, s.name AS server_name, p.name AS provider_name,
                   ar.name AS affected_route_name, nr.name AS new_route_name, oldr.name AS old_route_name,
                   oldcr.name AS old_company_route_name, newcr.name AS new_company_route_name,
                   oldcp.name AS old_company_route_provider_name, newcp.name AS new_company_route_provider_name,
                   cc.company_id_external, cc.company_name
            FROM routing_events re
            LEFT JOIN countries c ON c.id = re.country_id
            LEFT JOIN servers s ON s.id = re.server_id
            LEFT JOIN providers p ON p.id = re.provider_id
            LEFT JOIN routes ar ON ar.id = re.affected_route_id
            LEFT JOIN routes nr ON nr.id = re.new_route_id
            LEFT JOIN routes oldr ON oldr.id = re.old_route_id
            LEFT JOIN routes oldcr ON oldcr.id = re.old_company_route_id
            LEFT JOIN routes newcr ON newcr.id = re.new_company_route_id
            LEFT JOIN providers oldcp ON oldcp.id = oldcr.provider_id
            LEFT JOIN providers newcp ON newcp.id = newcr.provider_id
            LEFT JOIN calling_companies cc ON cc.id = re.calling_company_id
            {where}
            ORDER BY re.event_at DESC, re.id DESC
        """, params))

    def update_routing_event(self, event_id: int, *, updated_by: int, **kwargs) -> None:
        existing = self.conn.execute("SELECT * FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        if not existing:
            raise BusinessRuleError("Событие маршрутизации не найдено")
        reason = self._require_text(kwargs.get("reason"), "Причина обязательна")
        comment = self._require_text(kwargs.get("comment"), "Комментарий обязателен")
        new_values = {
            "event_at": self._require_text(kwargs.get("event_at"), "Дата события обязательна").replace("T", " "),
            "reason": reason,
            "comment": comment,
            "country_id": kwargs.get("country_id"),
            "server_id": kwargs.get("server_id"),
            "provider_id": kwargs.get("provider_id"),
            "affected_route_id": kwargs.get("affected_route_id"),
            "old_route_id": kwargs.get("old_route_id"),
            "new_route_id": kwargs.get("new_route_id"),
            "calling_company_id": kwargs.get("calling_company_id"),
            "company_change_type": kwargs.get("company_change_type"),
            "new_company_routing_mode": kwargs.get("new_company_routing_mode"),
            "new_company_route_id": kwargs.get("new_company_route_id"),
            "new_company_has_autorotation": kwargs.get("new_company_has_autorotation"),
        }
        self.conn.execute(
            """
            UPDATE routing_events
            SET event_at = ?, reason = ?, comment = ?, country_id = ?, server_id = ?, provider_id = ?,
                affected_route_id = ?, old_route_id = ?, new_route_id = ?, calling_company_id = ?,
                company_change_type = ?, new_company_routing_mode = ?, new_company_route_id = ?,
                new_company_has_autorotation = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                new_values["event_at"], new_values["reason"], new_values["comment"], new_values["country_id"],
                new_values["server_id"], new_values["provider_id"], new_values["affected_route_id"], new_values["old_route_id"],
                new_values["new_route_id"], new_values["calling_company_id"], new_values["company_change_type"],
                new_values["new_company_routing_mode"], new_values["new_company_route_id"], new_values["new_company_has_autorotation"],
                updated_by, event_id,
            ),
        )
        self._change_log("routing_event", event_id, "routing_event.updated", updated_by, old_values=dict(existing), new_values=new_values, summary=self._routing_event_summary({**dict(existing), **new_values}))
        self.conn.commit()

    def deactivate_routing_event(self, event_id: int, *, reason: str, deactivated_by: int) -> None:
        existing = self.conn.execute("SELECT * FROM routing_events WHERE id = ?", (event_id,)).fetchone()
        if not existing:
            raise BusinessRuleError("Событие маршрутизации не найдено")
        if not existing["is_active"]:
            raise BusinessRuleError("Событие уже деактивировано")
        reason = self._require_text(reason, "Причина деактивации обязательна")
        self.conn.execute(
            """
            UPDATE routing_events
            SET is_active = 0, deactivation_reason = ?, deactivated_at = CURRENT_TIMESTAMP,
                deactivated_by = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (reason, deactivated_by, deactivated_by, event_id),
        )
        self._change_log("routing_event", event_id, "routing_event.deactivated", deactivated_by, old_values=dict(existing), new_values={"deactivation_reason": reason}, summary=f"Событие #{event_id} деактивировано. Причина: {reason}")
        self.conn.commit()

    def list_provider_changes(self, filters: dict | None = None) -> list[sqlite3.Row]:
        filters = filters or {}
        clauses = []
        params: list = []
        if filters.get("date_from"):
            clauses.append("pcl.changed_at >= ?")
            params.append(filters["date_from"])
        if filters.get("date_to"):
            clauses.append("pcl.changed_at <= ?")
            params.append(filters["date_to"])
        if filters.get("country_id"):
            clauses.append("pcl.country_id = ?")
            params.append(filters["country_id"])
        if filters.get("provider_id"):
            clauses.append("(pcl.provider_before_id = ? OR pcl.provider_after_id = ?)")
            params.extend([filters["provider_id"], filters["provider_id"]])
        if filters.get("route_like"):
            clauses.append("(rb.name LIKE ? OR ra.name LIKE ?)")
            params.extend([f"%{filters['route_like']}%", f"%{filters['route_like']}%"])
        if filters.get("reason_like"):
            clauses.append("pcl.reason_text LIKE ?")
            params.append(f"%{filters['reason_like']}%")
        if filters.get("user_id"):
            clauses.append("pcl.created_by = ?")
            params.append(filters["user_id"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return list(
            self.conn.execute(
                f"""
                SELECT pcl.*, c.name AS country_name,
                       pb.name AS provider_before_name, pa.name AS provider_after_name,
                       rb.name AS route_before_name, ra.name AS route_after_name,
                       u.username AS created_by_username,
                       GROUP_CONCAT(s.name, ', ') AS server_names
                FROM provider_change_logs pcl
                JOIN countries c ON c.id = pcl.country_id
                JOIN providers pb ON pb.id = pcl.provider_before_id
                JOIN providers pa ON pa.id = pcl.provider_after_id
                LEFT JOIN routes rb ON rb.id = pcl.route_before_id
                LEFT JOIN routes ra ON ra.id = pcl.route_after_id
                JOIN users u ON u.id = pcl.created_by
                LEFT JOIN provider_change_log_servers pcls ON pcls.provider_change_log_id = pcl.id
                LEFT JOIN servers s ON s.id = pcls.server_id
                {where}
                GROUP BY pcl.id
                ORDER BY pcl.changed_at DESC, pcl.id DESC
                """,
                params,
            )
        )

    def _route_prefix_id(self, route_id: int | None) -> int | None:
        if not route_id:
            return None
        row = self.conn.execute("SELECT provider_prefix_id FROM routes WHERE id = ?", (route_id,)).fetchone()
        return int(row["provider_prefix_id"]) if row and row["provider_prefix_id"] is not None else None

    def _current_tariff(self, country_id: int, provider_id: int, provider_prefix_id: int | None) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM tariffs
            WHERE country_id = ? AND provider_id = ?
              AND COALESCE(provider_prefix_id, 0) = COALESCE(?, 0)
              AND is_current = 1
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (country_id, provider_id, provider_prefix_id),
        ).fetchone()

    def create_provider_change(
        self,
        *,
        changed_at: str,
        country_id: int,
        provider_before_id: int,
        provider_after_id: int,
        created_by: int,
        route_before_id: int | None = None,
        route_after_id: int | None = None,
        provider_prefix_before_id: int | None = None,
        provider_prefix_after_id: int | None = None,
        reason_text: str | None = None,
        comment: str | None = None,
        server_ids: list[int] | None = None,
    ) -> int:
        provider_changed = provider_before_id != provider_after_id
        if not reason_text or not reason_text.strip():
            raise BusinessRuleError("Причина замены обязательна")
        if provider_changed and not server_ids:
            raise BusinessRuleError("Сервер обязателен при смене провайдера")
        for route_id, provider_id, label in (
            (route_before_id, provider_before_id, "Маршрут до"),
            (route_after_id, provider_after_id, "Маршрут после"),
        ):
            if route_id:
                route = self.conn.execute("SELECT provider_id FROM routes WHERE id = ?", (route_id,)).fetchone()
                if route is None:
                    raise BusinessRuleError(f"{label} не найден")
                if int(route["provider_id"]) != int(provider_id):
                    raise BusinessRuleError(f"{label} не принадлежит выбранному провайдеру")
        provider_prefix_before_id = provider_prefix_before_id if provider_prefix_before_id is not None else self._route_prefix_id(route_before_id)
        provider_prefix_after_id = provider_prefix_after_id if provider_prefix_after_id is not None else self._route_prefix_id(route_after_id)
        tariff_before = self._current_tariff(country_id, provider_before_id, provider_prefix_before_id)
        tariff_after = self._current_tariff(country_id, provider_after_id, provider_prefix_after_id)
        price_delta_eur = None
        if tariff_before and tariff_after:
            price_delta_eur = eur_price(tariff_after["eur_price"], "1") - eur_price(tariff_before["eur_price"], "1")
        cur = self.conn.execute(
            """
            INSERT INTO provider_change_logs(
                changed_at, country_id, route_before_id, provider_before_id, provider_prefix_before_id,
                tariff_before_id, price_before_provider_currency_id, price_before_in_provider_currency,
                price_before_conversion_rate_to_eur, price_before_conversion_rate_date, price_before_eur,
                route_after_id, provider_after_id, provider_prefix_after_id,
                tariff_after_id, price_after_provider_currency_id, price_after_in_provider_currency,
                price_after_conversion_rate_to_eur, price_after_conversion_rate_date, price_after_eur,
                price_delta_eur, provider_changed, reason_text, comment, telegram_status, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'not_sent', ?)
            """,
            (
                changed_at,
                country_id,
                route_before_id,
                provider_before_id,
                provider_prefix_before_id,
                tariff_before["id"] if tariff_before else None,
                tariff_before["provider_currency_id"] if tariff_before else None,
                tariff_before["price_in_provider_currency"] if tariff_before else None,
                tariff_before["conversion_rate_to_eur"] if tariff_before else None,
                tariff_before["conversion_rate_date"] if tariff_before else None,
                tariff_before["eur_price"] if tariff_before else None,
                route_after_id,
                provider_after_id,
                provider_prefix_after_id,
                tariff_after["id"] if tariff_after else None,
                tariff_after["provider_currency_id"] if tariff_after else None,
                tariff_after["price_in_provider_currency"] if tariff_after else None,
                tariff_after["conversion_rate_to_eur"] if tariff_after else None,
                tariff_after["conversion_rate_date"] if tariff_after else None,
                tariff_after["eur_price"] if tariff_after else None,
                str(price_delta_eur) if price_delta_eur is not None else None,
                1 if provider_changed else 0,
                reason_text,
                comment,
                created_by,
            ),
        )
        change_id = int(cur.lastrowid)
        for server_id in server_ids or []:
            self.conn.execute(
                "INSERT INTO provider_change_log_servers(provider_change_log_id, server_id) VALUES (?, ?)",
                (change_id, server_id),
            )
            if provider_changed and route_after_id:
                existing = self.conn.execute(
                    "SELECT id, current_route_id FROM server_route_priorities WHERE country_id = ? AND server_id = ?",
                    (country_id, server_id),
                ).fetchone()
                if existing:
                    self.conn.execute(
                        """
                        UPDATE server_route_priorities
                        SET previous_route_id = current_route_id,
                            current_route_id = ?, provider_change_log_id = ?, changed_at = CURRENT_TIMESTAMP,
                            changed_by = ?, reason = ?, comment = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (route_after_id, change_id, created_by, reason_text, comment, created_by, existing["id"]),
                    )
                else:
                    self.conn.execute(
                        """
                        INSERT INTO server_route_priorities(
                            country_id, server_id, current_route_id, provider_change_log_id,
                            changed_at, changed_by, reason, comment, created_by
                        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
                        """,
                        (country_id, server_id, route_after_id, change_id, created_by, reason_text, comment, created_by),
                    )
        self._change_log(
            "provider_change_log",
            change_id,
            "provider_change_log.created",
            created_by,
            new_values={"provider_changed": provider_changed, "server_ids": server_ids or []},
        )
        self.conn.commit()
        return change_id

    def _server_route_priority_summary(
        self,
        *,
        country_id: int,
        server_id: int,
        old_values: dict,
        new_values: dict,
    ) -> str:
        country = self.conn.execute("SELECT name FROM countries WHERE id = ?", (country_id,)).fetchone()
        server = self.conn.execute("SELECT name FROM servers WHERE id = ?", (server_id,)).fetchone()

        def route_details(route_id: int | None) -> tuple[str, str, str]:
            if not route_id:
                return "—", "—", "—"
            route = self.conn.execute(
                """
                SELECT r.name AS route_name, p.name AS provider_name
                FROM routes r
                JOIN providers p ON p.id = r.provider_id
                WHERE r.id = ?
                """,
                (route_id,),
            ).fetchone()
            if not route:
                return str(route_id), str(route_id), "—"
            return str(route_id), route["route_name"], route["provider_name"]

        _, old_route, old_provider = route_details(old_values.get("current_route_id"))
        _, new_route, new_provider = route_details(new_values.get("current_route_id"))
        previous_route_id, previous_route, _ = route_details(new_values.get("previous_route_id"))
        previous_label = previous_route if previous_route_id == "—" else f"{previous_route_id} / {previous_route}"
        parts = [
            f"GEO: {country['name'] if country else country_id}",
            f"Сервер: {server['name'] if server else server_id}",
            f"Старый current route: {old_route}",
            f"Старый provider: {old_provider}",
            f"Новый current route: {new_route}",
            f"Новый provider: {new_provider}",
            f"Previous route после изменения: {previous_label}",
        ]
        if new_values.get("comment"):
            parts.append(f"Комментарий: {new_values['comment']}")
        return "; ".join(parts)

    def update_server_route_priority(
        self,
        *,
        priority_id: int,
        current_route_id: int,
        comment: str | None,
        changed_by: int,
    ) -> None:
        existing = self.conn.execute(
            """
            SELECT id, country_id, server_id, current_route_id, previous_route_id, comment
            FROM server_route_priorities
            WHERE id = ?
            """,
            (priority_id,),
        ).fetchone()
        if not existing:
            raise BusinessRuleError("Приоритет по серверу не найден")

        route = self.conn.execute(
            "SELECT id, country_id FROM routes WHERE id = ?",
            (current_route_id,),
        ).fetchone()
        if not route:
            raise BusinessRuleError("Маршрут не найден")
        if int(route["country_id"]) != int(existing["country_id"]):
            raise BusinessRuleError("Маршрут должен принадлежать GEO приоритета")

        old_values = {
            "current_route_id": existing["current_route_id"],
            "previous_route_id": existing["previous_route_id"],
            "comment": existing["comment"],
        }
        route_changed = int(existing["current_route_id"]) != int(current_route_id)
        if route_changed:
            self.conn.execute(
                """
                UPDATE server_route_priorities
                SET previous_route_id = current_route_id,
                    current_route_id = ?,
                    changed_at = CURRENT_TIMESTAMP,
                    changed_by = ?,
                    comment = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    updated_by = ?
                WHERE id = ?
                """,
                (current_route_id, changed_by, comment, changed_by, priority_id),
            )
        else:
            self.conn.execute(
                """
                UPDATE server_route_priorities
                SET changed_at = CURRENT_TIMESTAMP,
                    changed_by = ?,
                    comment = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    updated_by = ?
                WHERE id = ?
                """,
                (changed_by, comment, changed_by, priority_id),
            )
        new_values = {
            "current_route_id": current_route_id,
            "previous_route_id": existing["current_route_id"] if route_changed else existing["previous_route_id"],
            "comment": comment,
        }
        self._change_log(
            "server_route_priority",
            priority_id,
            "server_route_priority.current_route_updated",
            changed_by,
            old_values=old_values,
            new_values=new_values,
            summary=self._server_route_priority_summary(
                country_id=existing["country_id"],
                server_id=existing["server_id"],
                old_values=old_values,
                new_values=new_values,
            ),
        )
        self.conn.commit()

    def list_active_change_reasons(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM change_reasons WHERE is_active = 1 ORDER BY name"))

    def create_change_reason(self, name: str, created_by: int | None = None, comment: str | None = None, is_active: bool = True) -> int:
        cur = self.conn.execute(
            "INSERT INTO change_reasons(name, description, is_active) VALUES (?, ?, ?)",
            (name.strip(), comment, 1 if is_active else 0),
        )
        reason_id = int(cur.lastrowid)
        self._change_log("change_reason", reason_id, "change_reason.created", created_by, new_values={"name": name.strip()})
        self.conn.commit()
        return reason_id

    def get_or_create_country(self, name: str) -> int:
        row = self.conn.execute("SELECT id FROM countries WHERE name = ?", (name,)).fetchone()
        return int(row["id"]) if row else self.create_country(name)

    def get_or_create_currency(self, code: str) -> int:
        row = self.conn.execute("SELECT id FROM currencies WHERE code = ?", (code,)).fetchone()
        return int(row["id"]) if row else self.create_currency(code, code)

    def get_or_create_provider(self, name: str, currency_id: int | None = None) -> int:
        normalized = normalize_provider_name(name)
        row = self.conn.execute("SELECT id FROM providers WHERE normalized_name = ?", (normalized,)).fetchone()
        return int(row["id"]) if row else self.create_provider(name, default_currency_id=currency_id)

    def get_or_create_prefix(self, provider_id: int, prefix: str | None) -> int | None:
        normalized_prefix = prefix or None
        row = self.conn.execute(
            "SELECT id FROM provider_prefixes WHERE provider_id = ? AND COALESCE(prefix, '') = COALESCE(?, '')",
            (provider_id, normalized_prefix),
        ).fetchone()
        return int(row["id"]) if row else self.create_prefix(provider_id, normalized_prefix)

    def _change_log(
        self,
        entity_type: str,
        entity_id: int,
        change_type: str,
        changed_by: int | None,
        *,
        old_values: dict | None = None,
        new_values: dict | None = None,
        summary: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO change_log(entity_type, entity_id, change_type, changed_by, old_values, new_values, summary, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'ui')
            """,
            (
                entity_type,
                entity_id,
                change_type,
                changed_by,
                json.dumps(old_values, ensure_ascii=False) if old_values is not None else None,
                json.dumps(new_values, ensure_ascii=False) if new_values is not None else None,
                summary,
            ),
        )
