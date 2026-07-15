"""Backend-neutral database adapter primitives.

Stage 14 intentionally does not enable PostgreSQL runtime support. These helpers
provide small compatibility building blocks that future stages can use while the
application remains SQLite-backed.

Error classification remains owned by :mod:`app.db_errors`; this module does not
replace that mapping. Transaction behavior also remains in
``Repository.transaction`` for now; Stage 16+ can unify transaction handling if
needed.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

SUPPORTED_BACKENDS = frozenset({"sqlite", "postgres"})
_BACKEND_ALIASES = {"sqlite": "sqlite", "postgres": "postgres", "postgresql": "postgres"}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def normalize_backend_name(value: str) -> str:
    """Return the canonical backend name for a supported backend value."""
    if not isinstance(value, str):
        raise ValueError(f"Unsupported database backend {value!r}; expected sqlite or postgres")
    normalized = value.strip().lower()
    backend = _BACKEND_ALIASES.get(normalized)
    if backend is None:
        raise ValueError(f"Unsupported database backend {value!r}; expected sqlite or postgres")
    return backend


def placeholder(backend: str) -> str:
    """Return the DB-API placeholder token for *backend*."""
    normalized = normalize_backend_name(backend)
    return "?" if normalized == "sqlite" else "%s"


def placeholders(count: int, backend: str) -> str:
    """Return a comma-separated placeholder list for *count* values."""
    if count <= 0:
        raise ValueError("Placeholder count must be greater than zero")
    return ", ".join(placeholder(backend) for _ in range(count))


def validate_identifier(name: str) -> str:
    """Validate a simple or qualified SQL identifier and return it unchanged.

    Accepted forms include ``id``, ``route_id``, ``routes.id``, and
    ``user_permissions.section_key``. Quotes, spaces, operators, parentheses,
    semicolons, and empty parts are rejected.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("SQL identifier must be a non-empty string")
    if _IDENTIFIER_RE.fullmatch(name) is None:
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def quote_identifier(name: str) -> str:
    """Return a double-quoted simple or qualified SQL identifier."""
    validated = validate_identifier(name)
    return ".".join(f'"{part}"' for part in validated.split("."))


def build_in_clause(column: str, values: Sequence[Any], backend: str) -> tuple[str, list[Any]]:
    """Build a safe dynamic ``IN`` clause fragment and parameter list."""
    safe_column = validate_identifier(column)
    params = list(values)
    if not params:
        return "1 = 0", []
    return f"{safe_column} IN ({placeholders(len(params), backend)})", params



def insert_ignore_statement(
    table: str,
    columns: Sequence[str],
    conflict_columns: Sequence[str],
    backend: str,
) -> str:
    """Build a small backend-specific insert-if-missing statement."""
    normalized = normalize_backend_name(backend)
    safe_table = validate_identifier(table)
    safe_columns = [validate_identifier(column) for column in columns]
    safe_conflict_columns = [validate_identifier(column) for column in conflict_columns]
    if not safe_columns:
        raise ValueError("Insert-ignore columns must not be empty")
    if not safe_conflict_columns:
        raise ValueError("Insert-ignore conflict columns must not be empty")
    column_sql = ", ".join(safe_columns)
    values_sql = placeholders(len(safe_columns), normalized)
    if normalized == "sqlite":
        return f"INSERT OR IGNORE INTO {safe_table}({column_sql}) VALUES ({values_sql})"
    conflict_sql = ", ".join(safe_conflict_columns)
    return f"INSERT INTO {safe_table}({column_sql}) VALUES ({values_sql}) ON CONFLICT ({conflict_sql}) DO NOTHING"

def prepare_insert_returning_id(sql: str, backend: str, id_column: str = "id") -> str:
    """Prepare an INSERT statement for backend-specific inserted-id retrieval."""
    normalized = normalize_backend_name(backend)
    if normalized == "sqlite":
        return sql
    returning_column = validate_identifier(id_column)
    if re.search(r"\bRETURNING\b", sql, flags=re.IGNORECASE):
        return sql
    return f"{sql.rstrip().rstrip(';')} RETURNING {returning_column}"


def extract_inserted_id(cursor: Any, backend: str) -> int:
    """Extract the inserted row id from a DB-API cursor."""
    normalized = normalize_backend_name(backend)
    if normalized == "sqlite":
        inserted_id = getattr(cursor, "lastrowid", None)
    else:
        row = cursor.fetchone()
        if row is None:
            inserted_id = None
        elif isinstance(row, dict):
            inserted_id = row.get("id")
        elif hasattr(row, "keys"):
            try:
                inserted_id = row["id"]
            except Exception:
                inserted_id = None
        else:
            try:
                inserted_id = row[0]
            except (IndexError, KeyError, TypeError):
                inserted_id = None
    if inserted_id is None:
        raise RuntimeError(f"Could not extract inserted id for {normalized} cursor")
    return int(inserted_id)


def row_to_dict(row: Any) -> dict[str, Any] | None:
    """Normalize a mapping-style database row to a plain dict."""
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    if isinstance(row, (tuple, list)):
        raise TypeError("Cannot convert tuple/list database row without column names")
    try:
        return dict(row)
    except (TypeError, ValueError) as exc:
        raise TypeError("Cannot convert database row to dict") from exc


def rows_to_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Normalize an iterable of mapping-style database rows to plain dicts."""
    result: list[dict[str, Any]] = []
    for row in rows:
        converted = row_to_dict(row)
        if converted is None:
            raise TypeError("rows_to_dicts does not accept None rows")
        result.append(converted)
    return result


def to_db_bool(value: bool | int | None, backend: str) -> Any:
    """Convert a Python boolean-ish value to backend-specific storage form."""
    normalized = normalize_backend_name(backend)
    if value is None:
        return None
    if isinstance(value, bool):
        boolean = value
    elif isinstance(value, int) and value in (0, 1):
        boolean = bool(value)
    else:
        raise ValueError(f"Invalid boolean value for database storage: {value!r}")
    return 1 if normalized == "sqlite" and boolean else 0 if normalized == "sqlite" else boolean


def from_db_bool(value: Any) -> bool | None:
    """Convert a database boolean value to ``bool`` without loose coercion."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if value == "1":
        return True
    if value == "0":
        return False
    raise ValueError(f"Invalid database boolean value: {value!r}")
