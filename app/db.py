from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_DB_PATH = ROOT / "mvp.sqlite3"


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _rebuild_phone_numbers_if_assignment_check(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'phone_numbers'").fetchone()
    if not row or "assignment_type TEXT NOT NULL CHECK" not in (row[0] or ""):
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("""
        CREATE TABLE phone_numbers_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            country_id INTEGER NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
            provider_id INTEGER REFERENCES providers(id) ON DELETE RESTRICT,
            number TEXT NOT NULL,
            normalized_number TEXT NOT NULL UNIQUE,
            project_label TEXT,
            assignment_type TEXT NOT NULL,
            phone_type TEXT,
            tariff_label TEXT,
            status TEXT NOT NULL CHECK (status IN ('used', 'free', 'disabled', 'reserved', 'blocked', 'unknown')),
            connection_cost NUMERIC CHECK (connection_cost IS NULL OR connection_cost >= 0),
            monthly_fee NUMERIC CHECK (monthly_fee IS NULL OR monthly_fee >= 0),
            outgoing_rate NUMERIC CHECK (outgoing_rate IS NULL OR outgoing_rate >= 0),
            incoming_rate NUMERIC CHECK (incoming_rate IS NULL OR incoming_rate >= 0),
            currency_id INTEGER REFERENCES currencies(id) ON DELETE RESTRICT,
            comment TEXT,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deactivated_at TEXT,
            CHECK (number GLOB '[1-9]*' AND number NOT GLOB '*[^0-9]*' AND length(number) BETWEEN 7 AND 21),
            CHECK (normalized_number = number)
        )
    """)
    conn.execute("""
        INSERT INTO phone_numbers_new(
            id, country_id, provider_id, number, normalized_number, project_label,
            assignment_type, phone_type, tariff_label, status, connection_cost, monthly_fee,
            outgoing_rate, incoming_rate, currency_id, comment, is_active, created_by,
            created_at, updated_by, updated_at, deactivated_at
        )
        SELECT id, country_id, provider_id, number, normalized_number, project_label,
            assignment_type, phone_type, tariff_label, status, connection_cost, monthly_fee,
            outgoing_rate, incoming_rate, currency_id, comment, is_active, created_by,
            created_at, updated_by, updated_at, deactivated_at
        FROM phone_numbers
    """)
    conn.execute("DROP TABLE phone_numbers")
    conn.execute("ALTER TABLE phone_numbers_new RENAME TO phone_numbers")
    conn.execute("PRAGMA foreign_keys = ON")


def run_lightweight_migrations(conn: sqlite3.Connection) -> None:
    """Keep already-created MVP databases compatible with additive UI changes."""
    _add_column_if_missing(conn, "calling_companies", "line_count", "INTEGER NOT NULL DEFAULT 0 CHECK (line_count >= 0)")
    _add_column_if_missing(conn, "calling_companies", "dial_set_count", "INTEGER NOT NULL DEFAULT 0 CHECK (dial_set_count >= 0)")
    _add_column_if_missing(conn, "calling_companies", "retry_interval_seconds", "INTEGER NOT NULL DEFAULT 0 CHECK (retry_interval_seconds >= 0)")
    _add_column_if_missing(conn, "phone_numbers", "phone_type", "TEXT")
    _add_column_if_missing(conn, "phone_numbers", "tariff_label", "TEXT")
    _add_column_if_missing(conn, "phone_numbers", "deactivated_at", "TEXT")
    _rebuild_phone_numbers_if_assignment_check(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phone_number_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            comment TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            comment TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phone_assignment_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            comment TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS company_routing_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            calling_company_id INTEGER NOT NULL REFERENCES calling_companies(id) ON DELETE RESTRICT,
            country_id INTEGER NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
            server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE RESTRICT,
            route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
            routing_mode TEXT NOT NULL CHECK (routing_mode IN ('server_priority', 'campaign_route', 'autorotation', 'mixed')),
            has_autorotation INTEGER NOT NULL DEFAULT 0 CHECK (has_autorotation IN (0, 1)),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            comment TEXT,
            valid_from TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            valid_to TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
            CHECK ((is_active = 1 AND valid_to IS NULL) OR is_active = 0)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_routing_settings_company_id ON company_routing_settings(calling_company_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_routing_settings_country_id ON company_routing_settings(country_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_routing_settings_server_id ON company_routing_settings(server_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_routing_settings_route_id ON company_routing_settings(route_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_company_routing_settings_one_active ON company_routing_settings(calling_company_id) WHERE is_active = 1 AND valid_to IS NULL")
    for code, name in (
        ("outgoing_cli", "АОН"),
        ("inbound_line", "Входящая линия"),
        ("office_phone", "Горячая линия"),
        ("sim_card", "SIM-карта"),
        ("pool_number", "Номер из пула"),
        ("other", "Другое"),
    ):
        conn.execute("INSERT OR IGNORE INTO phone_assignment_types(code, name, is_active) VALUES (?, ?, 1)", (code, name))


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    run_lightweight_migrations(conn)
    conn.commit()
