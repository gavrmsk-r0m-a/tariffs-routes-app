"""Transport-neutral, read-only Archivarius export model.

HTTP and the optional outbound relay both consume this module.  Keep transport
concerns (headers, bearer parsing and HTTP POSTs) out of the export model.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

API_VERSION = "v1"
SCHEMA_VERSION = "archivarius-v1"
EVENT_ENTITY_TYPES = frozenset({"route", "route_phone_number", "phone_number", "tariff", "routing_event"})
SNAPSHOT_DEFAULT_LIMIT = 500
EVENT_DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


class PaginationError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonicalize(value: Any) -> Any:
    """Return JSON-safe values without losing NUMERIC precision."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): canonicalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    return value


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(canonicalize(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def generated_at() -> str:
    return canonicalize(utc_now())


def parse_pagination(query: dict[str, str], *, events: bool = False) -> tuple[int, int]:
    default = EVENT_DEFAULT_LIMIT if events else SNAPSHOT_DEFAULT_LIMIT
    try:
        after_id = int(query.get("after_id", "0"))
        limit = int(query.get("limit", str(default)))
    except (TypeError, ValueError) as exc:
        raise PaginationError("after_id and limit must be integers") from exc
    if after_id < 0:
        raise PaginationError("after_id must be at least 0")
    if limit < 1 or limit > MAX_LIMIT:
        raise PaginationError(f"limit must be between 1 and {MAX_LIMIT}")
    return after_id, limit


def envelope(items: list[dict[str, Any]], after_id: int, has_more: bool) -> dict[str, Any]:
    return {
        "api_version": API_VERSION,
        "generated_at": generated_at(),
        "items": canonicalize(items),
        "next_cursor": int(items[-1]["id"]) if items else after_id,
        "has_more": bool(has_more),
    }


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _named(row: dict[str, Any], prefix: str, *, nullable: bool = False) -> dict[str, Any] | None:
    identifier = row.get(f"{prefix}_id")
    name = row.get(f"{prefix}_name")
    if nullable and identifier is None:
        return None
    return {"id": identifier, "name": name}


class ArchivariusExporter:
    """Canonical Archivarius reads over a Repository connection."""

    def __init__(self, repository):
        self.repository = repository
        self.conn = repository.conn

    @staticmethod
    def _page(rows, after_id: int, limit: int, shape):
        values = list(rows)
        more = len(values) > limit
        values = values[:limit]
        return envelope([shape(dict(row)) for row in values], after_id, more)

    def summary(self) -> dict[str, Any]:
        row = self.conn.execute("""
            SELECT
              (SELECT COUNT(*) FROM routes WHERE is_actual IS TRUE) AS active_routes,
              (SELECT COUNT(*) FROM phone_numbers WHERE is_active IS TRUE) AS active_phones,
              (SELECT COUNT(*) FROM phone_numbers WHERE is_active IS TRUE AND status='used') AS used_phones,
              (SELECT COUNT(*) FROM phone_numbers WHERE is_active IS TRUE AND status='unused') AS unused_phones,
              (SELECT COUNT(*) FROM phone_numbers WHERE is_active IS TRUE AND status='unknown') AS unknown_phones,
              (SELECT COUNT(*) FROM phone_numbers WHERE is_active IS TRUE AND review_required IS TRUE) AS review_required_phones,
              (SELECT COUNT(*) FROM phone_numbers WHERE is_active IS TRUE AND is_problematic IS TRUE) AS problematic_phones,
              (SELECT COUNT(*) FROM tariffs WHERE is_current IS TRUE) AS current_tariffs,
              (SELECT COUNT(*) FROM routing_events WHERE is_active IS TRUE) AS active_provider_changes
        """).fetchone()
        return {"api_version": API_VERSION, "generated_at": generated_at(), **canonicalize(dict(row))}

    def routes(self, after_id: int = 0, limit: int = SNAPSHOT_DEFAULT_LIMIT) -> dict[str, Any]:
        rows = list(self.conn.execute("""
            SELECT r.*, c.name country_name, p.name provider_name, pp.prefix prefix_value
            FROM routes r JOIN countries c ON c.id=r.country_id
            JOIN providers p ON p.id=r.provider_id
            LEFT JOIN provider_prefixes pp ON pp.id=r.provider_prefix_id
            WHERE r.id > %s ORDER BY r.id ASC LIMIT %s
        """, (after_id, limit + 1)))
        page_rows = rows[:limit]
        ids = [row["id"] for row in page_rows]
        links: dict[int, list[dict[str, Any]]] = {identifier: [] for identifier in ids}
        if ids:
            for link in self.conn.execute("""
                SELECT rpn.route_id, pn.id, pn.number, pn.status, pn.is_active, rpn.usage_type
                FROM route_phone_numbers rpn JOIN phone_numbers pn ON pn.id=rpn.phone_number_id
                WHERE rpn.is_active IS TRUE AND rpn.route_id = ANY(%s)
                ORDER BY rpn.route_id, pn.id
            """, (ids,)):
                item = dict(link)
                route_id = item.pop("route_id")
                item["working"] = bool(item["is_active"] and item["status"] == "used")
                links[route_id].append(canonicalize(item))

        def shape(row):
            return {
                "id": row["id"], "name": row["name"],
                "country": _named(row, "country"), "provider": _named(row, "provider"),
                "prefix": None if row.get("provider_prefix_id") is None else {"id": row["provider_prefix_id"], "value": row.get("prefix_value")},
                **{key: row.get(key) for key in ("project_label", "cli_source_type", "cli_source_label", "aon_pool", "rnd_type", "rnd_pool_owner", "priority_status", "inbound_line_available", "is_actual", "comment", "created_at", "updated_at")},
                "phones": links.get(row["id"], []),
            }
        return self._page(rows, after_id, limit, shape)

    def phones(self, after_id: int = 0, limit: int = SNAPSHOT_DEFAULT_LIMIT) -> dict[str, Any]:
        rows = list(self.conn.execute("""
            SELECT pn.*, COALESCE(pn.country_label,c.name) country_name,
                   COALESCE(pn.provider_label,p.name) provider_name,
                   COALESCE(pn.assignment_label,pat.name,pn.assignment_type) assignment_type_label,
                   COALESCE(pn.currency_label,cur.code) currency_code
            FROM phone_numbers pn JOIN countries c ON c.id=pn.country_id
            LEFT JOIN providers p ON p.id=pn.provider_id
            LEFT JOIN phone_assignment_types pat ON pat.code=pn.assignment_type
            LEFT JOIN currencies cur ON cur.id=pn.currency_id
            WHERE pn.id > %s ORDER BY pn.id ASC LIMIT %s
        """, (after_id, limit + 1)))
        ids = [row["id"] for row in rows[:limit]]
        memberships: dict[int, list[dict[str, Any]]] = {identifier: [] for identifier in ids}
        if ids:
            for membership in self.conn.execute("""
                SELECT rpn.phone_number_id, r.id, r.name, rpn.usage_type
                FROM route_phone_numbers rpn JOIN routes r ON r.id=rpn.route_id
                WHERE rpn.is_active IS TRUE AND rpn.phone_number_id = ANY(%s)
                ORDER BY rpn.phone_number_id, r.id
            """, (ids,)):
                item = dict(membership); phone_id = item.pop("phone_number_id")
                memberships[phone_id].append(item)

        def shape(row):
            return {
                "id": row["id"], "number": row["number"], "country": _named(row, "country"),
                "provider": _named(row, "provider", nullable=True), "project_label": row.get("project_label"),
                "assignment_type": row.get("assignment_type"), "assignment_label": row.get("assignment_type_label"),
                **{key: row.get(key) for key in ("phone_type", "tariff_label", "status", "is_active", "review_required", "is_problematic", "connection_cost", "monthly_fee", "outgoing_rate", "incoming_rate", "comment", "created_at", "updated_at", "deactivated_at")},
                "currency": row.get("currency_code"), "routes": memberships.get(row["id"], []),
            }
        return self._page(rows, after_id, limit, shape)

    def tariffs(self, after_id: int = 0, limit: int = SNAPSHOT_DEFAULT_LIMIT) -> dict[str, Any]:
        rows = self.conn.execute("""
            SELECT t.*, c.name country_name, p.name provider_name, pp.prefix prefix_value, cur.code currency_code
            FROM tariffs t JOIN countries c ON c.id=t.country_id JOIN providers p ON p.id=t.provider_id
            LEFT JOIN provider_prefixes pp ON pp.id=t.provider_prefix_id
            JOIN currencies cur ON cur.id=t.provider_currency_id
            WHERE t.is_current IS TRUE AND t.id > %s ORDER BY t.id ASC LIMIT %s
        """, (after_id, limit + 1))
        def shape(row):
            return {
                "id": row["id"], "country": _named(row, "country"), "provider": _named(row, "provider"),
                "prefix": None if row.get("provider_prefix_id") is None else {"id": row["provider_prefix_id"], "value": row.get("prefix_value")},
                "provider_currency": {"id": row["provider_currency_id"], "code": row.get("currency_code")},
                **{key: row.get(key) for key in ("price_in_provider_currency", "conversion_rate_to_eur", "conversion_rate_date", "eur_price", "priority_status", "is_estimated", "comment", "valid_from", "valid_to", "is_current", "created_at", "updated_at")},
            }
        return self._page(rows, after_id, limit, shape)

    def provider_changes(self, after_id: int = 0, limit: int = SNAPSHOT_DEFAULT_LIMIT) -> dict[str, Any]:
        rows = self.conn.execute("""
            SELECT re.*, c.name country_name, s.name server_name, p.name provider_name,
              ar.name affected_route_name, oldr.name old_route_name, newr.name new_route_name,
              cc.company_name calling_company_name, cc.company_id_external calling_company_external_id,
              oldcr.name old_company_route_name, newcr.name new_company_route_name, ofr.name overflow_route_name,
              COALESCE(u.display_name,u.username) author_name
            FROM routing_events re LEFT JOIN countries c ON c.id=re.country_id
            LEFT JOIN servers s ON s.id=re.server_id LEFT JOIN providers p ON p.id=re.provider_id
            LEFT JOIN routes ar ON ar.id=re.affected_route_id LEFT JOIN routes oldr ON oldr.id=re.old_route_id
            LEFT JOIN routes newr ON newr.id=re.new_route_id LEFT JOIN calling_companies cc ON cc.id=re.calling_company_id
            LEFT JOIN routes oldcr ON oldcr.id=re.old_company_route_id LEFT JOIN routes newcr ON newcr.id=re.new_company_route_id
            LEFT JOIN routes ofr ON ofr.id=re.overflow_route_id LEFT JOIN users u ON u.id=re.created_by
            WHERE re.id > %s ORDER BY re.id ASC LIMIT %s
        """, (after_id, limit + 1))
        def ref(row, prefix):
            return None if row.get(f"{prefix}_id") is None else {"id": row[f"{prefix}_id"], "name": row.get(f"{prefix}_name")}
        def shape(row):
            return {
                "id": row["id"], "event_at": row.get("event_at"), "apply_scope": row.get("apply_scope"), "reason": row.get("reason"), "comment": row.get("comment"),
                "country": ref(row,"country"), "server": ref(row,"server"), "provider": ref(row,"provider"),
                "affected_route": ref(row,"affected_route"), "old_route": ref(row,"old_route"), "new_route": ref(row,"new_route"),
                "calling_company": None if row.get("calling_company_id") is None else {"id": row["calling_company_id"], "name": row.get("calling_company_name"), "external_id": row.get("calling_company_external_id")},
                "company_change_type": row.get("company_change_type"), "old_company_routing_mode": row.get("old_company_routing_mode"), "new_company_routing_mode": row.get("new_company_routing_mode"),
                "old_company_route": ref(row,"old_company_route"), "new_company_route": ref(row,"new_company_route"),
                "old_company_has_autorotation": row.get("old_company_has_autorotation"), "new_company_has_autorotation": row.get("new_company_has_autorotation"),
                "has_overflow": row.get("has_overflow"), "overflow_route": ref(row,"overflow_route"), "snapshot": row.get("snapshot_json"),
                "is_active": row.get("is_active"), "created_at": row.get("created_at"), "author": row.get("author_name"),
            }
        return self._page(rows, after_id, limit, shape)

    def events(self, after_id: int = 0, limit: int = EVENT_DEFAULT_LIMIT) -> dict[str, Any]:
        rows = self.conn.execute("""
            SELECT cl.id, cl.changed_at, cl.entity_type, cl.entity_id, cl.change_type,
                   cl.old_values, cl.new_values, cl.summary, cl.source,
                   COALESCE(u.display_name,u.username) changed_by
            FROM change_log cl LEFT JOIN users u ON u.id=cl.changed_by
            WHERE cl.id > %s AND cl.entity_type = ANY(%s)
            ORDER BY cl.id ASC LIMIT %s
        """, (after_id, list(EVENT_ENTITY_TYPES), limit + 1))
        return self._page(rows, after_id, limit, lambda row: row)

    def complete_snapshot(self) -> dict[str, Any]:
        data: dict[str, Any] = {"api_version": API_VERSION, "generated_at": generated_at()}
        for name in ("routes", "phones", "tariffs", "provider_changes"):
            cursor = 0; items = []
            while True:
                page = getattr(self, name)(cursor, MAX_LIMIT)
                items.extend(page["items"])
                if not page["has_more"]:
                    break
                cursor = page["next_cursor"]
            data[name] = items
        data["summary"] = self.summary()
        return data
