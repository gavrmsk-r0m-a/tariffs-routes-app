from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

from app.repository import Repository


class HybridRow(dict):
    """Mapping row with temporary numeric indexing for pre-cutover assertions."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def hybrid_row(cursor):
    description = cursor.description or ()
    names = [column.name for column in description]
    return lambda values: HybridRow(zip(names, values))

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs/postgres/schema.postgres.sql"
ADMIN_DATABASE_URL_ENV = "POSTGRES_TEST_ADMIN_URL"
TEST_DATABASE_PREFIX = "teleroute_test_"


def _database_name(database_url: str) -> str:
    return urlsplit(database_url).path.lstrip("/")


def _with_database(database_url: str, database: str) -> str:
    parsed = urlsplit(database_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/" + database, parsed.query, parsed.fragment))


def require_admin_url(environ=None) -> str:
    env = os.environ if environ is None else environ
    value = (env.get(ADMIN_DATABASE_URL_ENV) or "").strip()
    if not value:
        raise RuntimeError(f"{ADMIN_DATABASE_URL_ENV} is required for PostgreSQL application integration tests")
    name = _database_name(value).lower()
    if name in {"teleroute", "production", "prod"} or name.startswith(TEST_DATABASE_PREFIX):
        raise RuntimeError("PostgreSQL test admin URL must target an administrative database, not TeleRoute or a test database")
    return value


class TemporaryPostgresDatabase:
    def __init__(self, admin_url: str | None = None):
        self.admin_url = admin_url or require_admin_url()
        self.name = TEST_DATABASE_PREFIX + uuid.uuid4().hex
        self.database_url = _with_database(self.admin_url, self.name)

    def _assert_safe_name(self):
        if not re.fullmatch(r"teleroute_test_[0-9a-f]{32}", self.name):
            raise RuntimeError("Refusing unsafe PostgreSQL test database name")
        if _database_name(self.database_url) != self.name:
            raise RuntimeError("Refusing PostgreSQL test URL/name mismatch")

    def create(self):
        self._assert_safe_name()
        with psycopg.connect(self.admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.name)))
        try:
            self.reset()
        except Exception:
            self.drop()
            raise
        return self

    def connect(self):
        self._assert_safe_name()
        return psycopg.connect(self.database_url, row_factory=hybrid_row)

    def reset(self, *, seed: bool = True):
        self._assert_safe_name()
        with self.connect() as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            if seed:
                seed_postgres(conn)
            conn.commit()

    def drop(self):
        self._assert_safe_name()
        with psycopg.connect(self.admin_url, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (self.name,))
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(self.name)))

DEFAULT_PROJECTS = (
    ("mezhdep", "Меж.деп.", 1, False),
    ("rep", "REP", 2, True),
    ("itm", "ИТМ", 3, True),
    ("prepayment", "Предоплата", 4, True),
    ("legal", "Юр.деп.", 5, True),
)
DEFAULT_PHONE_ASSIGNMENTS = (("gl", "ГЛ", 1), ("aon", "АОН", 2), ("scratchcards", "Scratchcards", 3), ("competitors", "Competitors", 4), ("sms", "SMS", 5), ("corporate_telephony", "Корп.телефония", 6), ("dozhim", "Дожим", 7), ("ivr", "IVR", 8))

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


def seed_postgres(conn) -> None:
    repo = Repository(conn)
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
            VALUES ('demo_data_version', %s, CURRENT_TIMESTAMP)
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
                    VALUES (%s, TRUE, %s)
                    ON CONFLICT(name) DO UPDATE SET is_active = TRUE, comment = excluded.comment, updated_at = CURRENT_TIMESTAMP
                    """,
                    (server_name, "Demo server for MVP testing"),
                )
            else:
                repo.conn.execute(
                    "INSERT INTO servers(name, is_active, comment) VALUES (%s, TRUE, %s) ON CONFLICT(name) DO NOTHING",
                    (server_name, "Demo server for MVP testing"),
                )
        for type_name in ("Mobile", "Fixed Line", "Toll-Free", "VoIP", "Unknown"):
            repo.conn.execute("INSERT INTO phone_number_types(name, is_active) VALUES (%s, TRUE) ON CONFLICT(name) DO NOTHING", (type_name,))
        repo.conn.execute("UPDATE projects SET is_active = FALSE WHERE name IN ('Междепы', 'Competitors', 'ITM', 'Monitoring', 'Test')")
        for code, name, sort_order, include_in_route_name in DEFAULT_PROJECTS:
            repo.conn.execute(
                """
                INSERT INTO projects(code, name, is_active, sort_order, include_in_route_name)
                VALUES (%s, %s, TRUE, %s, %s)
                ON CONFLICT(name) DO UPDATE SET code = excluded.code, is_active = TRUE,
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
                VALUES (%s, %s, TRUE, %s)
                ON CONFLICT(code) DO UPDATE SET name = excluded.name, is_active = TRUE,
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
        country_id = scalar_id("SELECT id FROM countries WHERE name = %s", (name,))
        if country_id is None:
            return repo.create_country(name, code)
        repo.conn.execute("UPDATE countries SET code = %s, is_active = TRUE, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (code, country_id))
        return country_id

    def ensure_currency(code: str, name: str, symbol: str) -> int:
        currency_id = scalar_id("SELECT id FROM currencies WHERE code = %s", (code,))
        if currency_id is None:
            return repo.create_currency(code, name, symbol)
        repo.conn.execute("UPDATE currencies SET name = %s, symbol = %s, is_active = TRUE, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (name, symbol, currency_id))
        return currency_id

    def ensure_provider(name: str, provider_type: str, default_currency_id: int) -> int:
        normalized = normalize_provider_name(name)
        provider_id = scalar_id("SELECT id FROM providers WHERE normalized_name = %s", (normalized,))
        if provider_id is None:
            return repo.create_provider(name, provider_type, default_currency_id, comment="Demo provider for MVP testing")
        repo.conn.execute(
            """
            UPDATE providers
            SET name = %s, provider_type = %s, default_currency_id = %s, is_active = TRUE,
                comment = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (name, provider_type, default_currency_id, "Demo provider for MVP testing", provider_id),
        )
        return provider_id

    def ensure_prefix(provider_id: int, prefix: str | None, name: str) -> int | None:
        if prefix is None:
            return None
        prefix_id = scalar_id(
            "SELECT id FROM provider_prefixes WHERE provider_id = %s AND COALESCE(prefix, '') = COALESCE(%s, '')",
            (provider_id, prefix),
        )
        if prefix_id is None:
            return repo.create_prefix(provider_id, prefix, name)
        repo.conn.execute(
            "UPDATE provider_prefixes SET name = %s, is_active = TRUE, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
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
        route_id = scalar_id("SELECT id FROM routes WHERE country_id = %s AND name = %s", (country_id, name))
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
            SET provider_id = %s, provider_prefix_id = %s, cli_source_type = %s, cli_source_label = %s,
                comment = %s, is_actual = TRUE, priority_status = %s, updated_by = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
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
            WHERE country_id = %s AND provider_id = %s AND COALESCE(provider_prefix_id, 0) = COALESCE(%s, 0) AND is_current = TRUE
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
        phone_id = scalar_id("SELECT id FROM phone_numbers WHERE number = %s OR normalized_number = %s", (number, number))
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
                SET country_id = %s, provider_id = %s, assignment_type = 'gl', status = 'used',
                    currency_id = %s, comment = %s, is_active = TRUE, updated_by = %s, updated_at = CURRENT_TIMESTAMP,
                    deactivated_at = NULL
                WHERE id = %s
                """,
                (country_id, provider_id, currency_id, "Demo number for testing", admin_id, phone_id),
            )
        active_link = repo.conn.execute(
            "SELECT id FROM route_phone_numbers WHERE route_id = %s AND phone_number_id = %s AND is_active = TRUE",
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
            WHERE company_id_external = %s
            ORDER BY CASE WHEN server_id = %s AND country_id = %s THEN 0 ELSE 1 END, id
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
                SET server_id = %s, country_id = %s, company_name = %s, has_autorotation = FALSE, line_count = 10, dial_set_count = 2,
                    retry_interval_seconds = 60, comment = %s, is_active = TRUE, updated_by = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (server_id, country_id, company_name, "Demo calling campaign for MVP testing", admin_id, company_id),
            )
        repo.conn.execute(
            """
            UPDATE calling_companies
            SET is_active = FALSE, updated_by = %s, updated_at = CURRENT_TIMESTAMP
            WHERE company_id_external = %s AND id <> %s
            """,
            (admin_id, company_id_external, company_id),
        )
        return company_id

    def upsert_server_priority(country_id: int, server_id: int, current_route_id: int, admin_id: int) -> None:
        priority_id = scalar_id("SELECT id FROM server_route_priorities WHERE country_id = %s AND server_id = %s", (country_id, server_id))
        if priority_id is None:
            repo.conn.execute(
                """
                INSERT INTO server_route_priorities(country_id, server_id, current_route_id, previous_route_id, changed_by, created_by, comment)
                VALUES (%s, %s, %s, NULL, %s, %s, %s)
                """,
                (country_id, server_id, current_route_id, admin_id, admin_id, "Demo initial priority"),
            )
        else:
            repo.conn.execute(
                """
                UPDATE server_route_priorities
                SET current_route_id = %s, previous_route_id = NULL, changed_by = %s, comment = %s, is_active = TRUE,
                    updated_by = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
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
        repo.conn.execute("INSERT INTO currency_rates(currency_id, rate_to_eur, rate_date, updated_by, comment) SELECT %s, 1, '2026-06-07', %s, 'Demo EUR' WHERE NOT EXISTS (SELECT 1 FROM currency_rates WHERE currency_id = %s AND rate_date = '2026-06-07' AND comment = 'Demo EUR')", (eur_id, admin_id, eur_id))
        repo.conn.execute("INSERT INTO currency_rates(currency_id, rate_to_eur, rate_date, updated_by, comment) SELECT %s, 0.93, '2026-06-07', %s, 'Demo USDT' WHERE NOT EXISTS (SELECT 1 FROM currency_rates WHERE currency_id = %s AND rate_date = '2026-06-07' AND comment = 'Demo USDT')", (usdt_id, admin_id, usdt_id))
        for reason in ("Плохие показатели", "Провайдер починил", "Обновлен пул номеров"):
            repo.conn.execute("INSERT INTO change_reasons(name, description, is_active) VALUES (%s, %s, TRUE)", (reason, reason))

        server_ids = {row["name"]: row["id"] for row in repo.conn.execute("SELECT id, name FROM servers WHERE name IN (%s)" % ",".join("%s" for _ in DEMO_SERVER_NAMES), DEMO_SERVER_NAMES)}
        repo.conn.execute(
            "UPDATE servers SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP WHERE name NOT IN (%s)" % ",".join("%s" for _ in DEMO_SERVER_NAMES),
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
            "UPDATE routes SET is_actual = FALSE, updated_by = %s, updated_at = CURRENT_TIMESTAMP WHERE country_id = %s AND name NOT IN (%s)" % ",".join("%s" for _ in DEMO_ROUTE_NAMES),
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
            "UPDATE phone_numbers SET is_active = FALSE, deactivated_at = COALESCE(deactivated_at, CURRENT_TIMESTAMP), updated_by = %s, updated_at = CURRENT_TIMESTAMP WHERE country_id = %s AND number NOT IN (%s)" % ",".join("%s" for _ in DEMO_PHONE_NUMBERS),
            (admin_id, country_id, *DEMO_PHONE_NUMBERS),
        )

        for index, external_id in enumerate(DEMO_COMPANY_EXTERNAL_IDS, start=1):
            ensure_calling_company(server_id=server_ids[f"EU{index}"], country_id=country_id, company_id_external=external_id, company_name=f"CC Mexico Demo {index}", admin_id=admin_id)
        repo.conn.execute(
            "UPDATE calling_companies SET is_active = FALSE, updated_by = %s, updated_at = CURRENT_TIMESTAMP WHERE country_id = %s AND company_id_external NOT IN (%s)" % ",".join("%s" for _ in DEMO_COMPANY_EXTERNAL_IDS),
            (admin_id, country_id, *DEMO_COMPANY_EXTERNAL_IDS),
        )

        repo.conn.execute(
            "DELETE FROM server_route_priorities WHERE server_id IN (SELECT id FROM servers WHERE name IN ('EU3', 'EU4', 'EU5', 'EU6', 'EU7', 'EU8', 'EU9')) OR (country_id = %s AND server_id NOT IN (%s, %s))",
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


_SHARED_DATABASE: TemporaryPostgresDatabase | None = None


def shared_database() -> TemporaryPostgresDatabase:
    global _SHARED_DATABASE
    if _SHARED_DATABASE is None:
        import atexit
        _SHARED_DATABASE = TemporaryPostgresDatabase().create()
        atexit.register(_SHARED_DATABASE.drop)
    return _SHARED_DATABASE
