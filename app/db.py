from __future__ import annotations

import sqlite3
import threading
from app.repository import hash_password
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_DB_PATH = ROOT / "mvp.sqlite3"
SQLITE_TIMEOUT_SECONDS = 5
SQLITE_BUSY_TIMEOUT_MS = 5000

_INIT_LOCK = threading.Lock()
_INITIALIZED_DB_KEYS: set[str] = set()


DEFAULT_USERS = (
    ("admin", "Admin", "admin", "admin"),
    ("roman", "Roman", "admin", "roman"),
    ("duty", "Дежурный", "operator", "duty123"),
    ("guest", "Гость", "guest", "guest123"),
)

DEFAULT_PROJECTS = (
    ("mezhdep", "Меж.деп.", 1, 0),
    ("rep", "REP", 2, 1),
    ("itm", "ИТМ", 3, 1),
    ("prepayment", "Предоплата", 4, 1),
    ("legal", "Юр.деп.", 5, 1),
)

DEFAULT_PHONE_ASSIGNMENTS = (
    ("gl", "ГЛ", 1),
    ("aon", "АОН", 2),
    ("scratchcards", "Scratchcards", 3),
    ("competitors", "Competitors", 4),
    ("sms", "SMS", 5),
    ("corporate_telephony", "Корп.телефония", 6),
    ("dozhim", "Дожим", 7),
    ("ivr", "IVR", 8),
)

PHONE_STATUS_SQL = "status TEXT NOT NULL DEFAULT 'unknown' CHECK (status IN ('used', 'free', 'problem', 'unknown'))"


def _phone_status_expr(column: str = "status") -> str:
    return (
        f"CASE "
        f"WHEN {column} = 'used' THEN 'used' "
        f"WHEN {column} IN ('free', 'reserved') THEN 'free' "
        f"WHEN {column} IN ('blocked', 'disabled') THEN 'problem' "
        f"WHEN {column} = 'unknown' THEN 'unknown' "
        f"ELSE 'unknown' END"
    )


def apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    apply_connection_pragmas(conn)
    return conn


def _db_key(path: str | Path) -> str:
    if str(path) == ":memory:":
        return ":memory:"
    return str(Path(path).resolve())


def _has_schema(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone() is not None


def ensure_db_initialized(conn: sqlite3.Connection, path: str | Path = DEFAULT_DB_PATH) -> None:
    """Run SQLite initialization once per process for request-time connections."""
    key = _db_key(path)
    if key in _INITIALIZED_DB_KEYS:
        return
    with _INIT_LOCK:
        if key in _INITIALIZED_DB_KEYS:
            return
        init_db(conn)
        _INITIALIZED_DB_KEYS.add(key)


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _rebuild_phone_numbers_if_needed(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'phone_numbers'").fetchone()
    table_sql = row[0] or "" if row else ""
    if not row:
        return
    needs_assignment_rebuild = "assignment_type TEXT NOT NULL CHECK" in table_sql
    needs_status_rebuild = (
        "'disabled'" in table_sql
        or "'reserved'" in table_sql
        or "'blocked'" in table_sql
        or "'problem'" not in table_sql
    )
    invalid_status = conn.execute(
        "SELECT 1 FROM phone_numbers WHERE COALESCE(status, '') NOT IN ('used', 'free', 'problem', 'unknown') LIMIT 1"
    ).fetchone()
    if not (needs_assignment_rebuild or needs_status_rebuild or invalid_status):
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(f"""
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
            {PHONE_STATUS_SQL},
            connection_cost NUMERIC CHECK (connection_cost IS NULL OR connection_cost >= 0),
            monthly_fee NUMERIC CHECK (monthly_fee IS NULL OR monthly_fee >= 0),
            outgoing_rate NUMERIC CHECK (outgoing_rate IS NULL OR outgoing_rate >= 0),
            incoming_rate NUMERIC CHECK (incoming_rate IS NULL OR incoming_rate >= 0),
            currency_id INTEGER REFERENCES currencies(id) ON DELETE RESTRICT,
            comment TEXT,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            review_required INTEGER NOT NULL DEFAULT 0 CHECK (review_required IN (0, 1)),
            created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deactivated_at TEXT,
            CHECK (number GLOB '[1-9]*' AND number NOT GLOB '*[^0-9]*' AND length(number) BETWEEN 7 AND 21),
            CHECK (normalized_number = number)
        )
    """)
    conn.execute(f"""
        INSERT INTO phone_numbers_new(
            id, country_id, provider_id, number, normalized_number, project_label,
            assignment_type, phone_type, tariff_label, status, connection_cost, monthly_fee,
            outgoing_rate, incoming_rate, currency_id, comment, is_active, review_required, created_by,
            created_at, updated_by, updated_at, deactivated_at
        )
        SELECT id, country_id, provider_id, number, normalized_number, project_label,
            assignment_type, phone_type, tariff_label, {_phone_status_expr("status")}, connection_cost, monthly_fee,
            outgoing_rate, incoming_rate, currency_id, comment, is_active, review_required, created_by,
            created_at, updated_by, updated_at, deactivated_at
        FROM phone_numbers
    """)
    conn.execute("DROP TABLE phone_numbers")
    conn.execute("ALTER TABLE phone_numbers_new RENAME TO phone_numbers")
    conn.execute("PRAGMA foreign_keys = ON")


def _seed_default_users_if_empty(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] != 0:
        return
    columns = _column_names(conn, "users")
    for username, display_name, role_key, password in DEFAULT_USERS:
        insert_columns = ["username", "display_name", "is_active"]
        values: list[object] = [username, display_name, 1]
        if "role_key" in columns:
            insert_columns.append("role_key")
            values.append(role_key)
        if "role" in columns:
            insert_columns.append("role")
            values.append("Admin" if role_key == "admin" else "User")
        if "password_hash" in columns and "password_salt" in columns:
            password_hash, password_salt = hash_password(password)
            insert_columns.extend(["password_hash", "password_salt"])
            values.extend([password_hash, password_salt])
        if "auth_provider" in columns:
            insert_columns.append("auth_provider")
            values.append("local")
        placeholders = ", ".join("?" for _ in insert_columns)
        conn.execute(
            f"INSERT INTO users({', '.join(insert_columns)}) VALUES ({placeholders})",
            tuple(values),
        )


def run_lightweight_migrations(conn: sqlite3.Connection) -> None:
    """Keep already-created MVP databases compatible with additive UI changes."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            role_key TEXT NOT NULL DEFAULT 'operator',
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            password_hash TEXT,
            password_salt TEXT
        )
    """)
    _add_column_if_missing(conn, "users", "role_key", "TEXT NOT NULL DEFAULT 'operator'")
    _add_column_if_missing(conn, "users", "display_name", "TEXT")
    _add_column_if_missing(conn, "users", "is_active", "INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))")
    _add_column_if_missing(conn, "users", "created_at", "TEXT")
    _add_column_if_missing(conn, "users", "updated_at", "TEXT")
    _add_column_if_missing(conn, "users", "password_hash", "TEXT")
    _add_column_if_missing(conn, "users", "password_salt", "TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            section_key TEXT NOT NULL,
            can_read INTEGER NOT NULL DEFAULT 0 CHECK (can_read IN (0, 1)),
            can_write INTEGER NOT NULL DEFAULT 0 CHECK (can_write IN (0, 1)),
            can_export INTEGER NOT NULL DEFAULT 0 CHECK (can_export IN (0, 1)),
            UNIQUE(user_id, section_key)
        )
    """)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'routing_events'").fetchone() is not None:
        _add_column_if_missing(conn, "routing_events", "updated_at", "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'server_route_priorities'").fetchone() is not None:
        _add_column_if_missing(conn, "server_route_priorities", "has_overflow", "INTEGER NOT NULL DEFAULT 0 CHECK (has_overflow IN (0, 1))")
        _add_column_if_missing(conn, "server_route_priorities", "overflow_route_id", "INTEGER REFERENCES routes(id) ON DELETE RESTRICT")
    conn.execute("UPDATE users SET display_name = username WHERE display_name IS NULL OR TRIM(display_name) = ''")
    if "role" in _column_names(conn, "users"):
        conn.execute("UPDATE users SET role_key = CASE WHEN role = 'Admin' THEN 'admin' ELSE 'operator' END WHERE role_key IS NULL OR role_key = ''")
    _seed_default_users_if_empty(conn)
    for username, _display_name, _role_key, password in DEFAULT_USERS:
        row = conn.execute("SELECT id, password_hash, password_salt FROM users WHERE username = ?", (username,)).fetchone()
        if row and (not row["password_hash"] or not row["password_salt"]):
            password_hash, password_salt = hash_password(password)
            conn.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?", (password_hash, password_salt, row["id"]))
    _add_column_if_missing(conn, "calling_companies", "line_count", "INTEGER NOT NULL DEFAULT 0 CHECK (line_count >= 0)")
    _add_column_if_missing(conn, "calling_companies", "dial_set_count", "INTEGER NOT NULL DEFAULT 0 CHECK (dial_set_count >= 0)")
    _add_column_if_missing(conn, "calling_companies", "retry_interval_seconds", "INTEGER NOT NULL DEFAULT 0 CHECK (retry_interval_seconds >= 0)")
    _add_column_if_missing(conn, "phone_numbers", "phone_type", "TEXT")
    _add_column_if_missing(conn, "phone_numbers", "tariff_label", "TEXT")
    _add_column_if_missing(conn, "phone_numbers", "deactivated_at", "TEXT")
    _add_column_if_missing(conn, "phone_numbers", "review_required", "INTEGER NOT NULL DEFAULT 0 CHECK (review_required IN (0, 1))")
    _add_column_if_missing(conn, "routes", "aon_pool", "TEXT")
    _add_column_if_missing(conn, "routes", "rnd_type", "TEXT CHECK (rnd_type IN ('local', 'nonlocal') OR rnd_type IS NULL)")
    _add_column_if_missing(conn, "routes", "rnd_pool_owner", "TEXT")
    _rebuild_phone_numbers_if_needed(conn)
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
            code TEXT UNIQUE,
            name TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            sort_order INTEGER NOT NULL DEFAULT 0,
            include_in_route_name INTEGER NOT NULL DEFAULT 1 CHECK (include_in_route_name IN (0, 1)),
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
            sort_order INTEGER NOT NULL DEFAULT 0,
            comment TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    _add_column_if_missing(conn, "projects", "code", "TEXT")
    _add_column_if_missing(conn, "projects", "sort_order", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "projects", "include_in_route_name", "INTEGER NOT NULL DEFAULT 1 CHECK (include_in_route_name IN (0, 1))")
    _add_column_if_missing(conn, "phone_assignment_types", "sort_order", "INTEGER NOT NULL DEFAULT 0")
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS routing_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_at TEXT NOT NULL,
            apply_scope TEXT NOT NULL CHECK (apply_scope IN ('none', 'server_priority', 'campaign_setting')),
            reason TEXT NOT NULL,
            country_id INTEGER REFERENCES countries(id) ON DELETE RESTRICT,
            server_id INTEGER REFERENCES servers(id) ON DELETE RESTRICT,
            provider_id INTEGER REFERENCES providers(id) ON DELETE RESTRICT,
            affected_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
            old_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
            new_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
            calling_company_id INTEGER REFERENCES calling_companies(id) ON DELETE RESTRICT,
            company_change_type TEXT CHECK (company_change_type IN ('enable_autorotation', 'disable_autorotation', 'set_campaign_route', 'remove_campaign_route') OR company_change_type IS NULL),
            old_company_routing_mode TEXT,
            new_company_routing_mode TEXT,
            old_company_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
            new_company_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
            old_company_has_autorotation INTEGER CHECK (old_company_has_autorotation IN (0, 1) OR old_company_has_autorotation IS NULL),
            new_company_has_autorotation INTEGER CHECK (new_company_has_autorotation IN (0, 1) OR new_company_has_autorotation IS NULL),
            has_overflow INTEGER NOT NULL DEFAULT 0 CHECK (has_overflow IN (0, 1)),
            overflow_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
            comment TEXT NOT NULL,
            snapshot_json TEXT,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            deactivation_reason TEXT,
            deactivated_at TEXT,
            deactivated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT
        )
    """)
    _add_column_if_missing(conn, "routing_events", "has_overflow", "INTEGER NOT NULL DEFAULT 0 CHECK (has_overflow IN (0, 1))")
    _add_column_if_missing(conn, "routing_events", "overflow_route_id", "INTEGER REFERENCES routes(id) ON DELETE RESTRICT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_routing_events_event_at ON routing_events(event_at DESC, id DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_routing_events_scope ON routing_events(apply_scope)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_routing_events_active ON routing_events(is_active)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS routing_event_servers (
            id INTEGER PRIMARY KEY,
            routing_event_id INTEGER NOT NULL REFERENCES routing_events(id) ON DELETE RESTRICT,
            server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE RESTRICT,
            old_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
            new_route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE RESTRICT,
            server_route_priority_id INTEGER REFERENCES server_route_priorities(id) ON DELETE RESTRICT,
            status TEXT NOT NULL DEFAULT 'applied' CHECK (status IN ('applied', 'skipped_noop')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_routing_event_servers_event ON routing_event_servers(routing_event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_routing_event_servers_server ON routing_event_servers(server_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_routing_event_servers_new_route ON routing_event_servers(new_route_id)")
    legacy_no_prefix_filter = """
        SELECT id FROM provider_prefixes
        WHERE prefix IS NULL
           OR TRIM(prefix) = ''
           OR TRIM(prefix) IN ('Без префикса', 'без префикса', 'no prefix')
           OR TRIM(prefix) IN ('—', '-')
    """
    conn.execute(f"UPDATE routes SET provider_prefix_id = NULL WHERE provider_prefix_id IN ({legacy_no_prefix_filter})")
    conn.execute(f"UPDATE tariffs SET provider_prefix_id = NULL WHERE provider_prefix_id IN ({legacy_no_prefix_filter})")
    conn.execute(f"""
        UPDATE provider_prefixes
        SET is_active = 0, updated_at = CURRENT_TIMESTAMP
        WHERE id IN ({legacy_no_prefix_filter})
    """)
    conn.execute("UPDATE projects SET is_active = 0 WHERE name IN ('Междепы', 'Competitors', 'ITM', 'Monitoring', 'Test')")
    for code, name, sort_order, include_in_route_name in DEFAULT_PROJECTS:
        conn.execute(
            """
            INSERT INTO projects(code, name, is_active, sort_order, include_in_route_name)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(name) DO UPDATE SET code = excluded.code, is_active = 1,
                sort_order = excluded.sort_order, include_in_route_name = excluded.include_in_route_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            (code, name, sort_order, include_in_route_name),
        )
    conn.execute(
        "DELETE FROM phone_assignment_types WHERE code IN ('outgoing_cli', 'inbound_line', 'office_phone', 'sim_card', 'pool_number', 'other')"
    )
    for code, name, sort_order in DEFAULT_PHONE_ASSIGNMENTS:
        conn.execute(
            """
            INSERT INTO phone_assignment_types(code, name, is_active, sort_order)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(code) DO UPDATE SET name = excluded.name, is_active = 1,
                sort_order = excluded.sort_order, updated_at = CURRENT_TIMESTAMP
            """,
            (code, name, sort_order),
        )


def init_db(conn: sqlite3.Connection) -> None:
    apply_connection_pragmas(conn)
    if not _has_schema(conn):
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    run_lightweight_migrations(conn)
    conn.commit()
