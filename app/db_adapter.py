"""Small PostgreSQL DB-API adapter primitives used by TeleRoute runtime."""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

SUPPORTED_BACKENDS = frozenset({"postgres"})
_BACKEND_ALIASES = {"postgres": "postgres", "postgresql": "postgres"}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def normalize_backend_name(value: str) -> str:
    if not isinstance(value, str) or value.strip().lower() not in _BACKEND_ALIASES:
        raise ValueError(f"Unsupported database backend {value!r}; expected postgres")
    return "postgres"


def placeholder(backend: str = "postgres") -> str:
    normalize_backend_name(backend)
    return "%s"


def placeholders(count: int, backend: str = "postgres") -> str:
    normalize_backend_name(backend)
    if count <= 0:
        raise ValueError("Placeholder count must be greater than zero")
    return ", ".join("%s" for _ in range(count))


def validate_identifier(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("SQL identifier must be a non-empty string")
    if _IDENTIFIER_RE.fullmatch(name) is None:
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def quote_identifier(name: str) -> str:
    return ".".join(f'"{part}"' for part in validate_identifier(name).split("."))


def build_in_clause(column: str, values: Sequence[Any], backend: str = "postgres") -> tuple[str, list[Any]]:
    safe_column = validate_identifier(column)
    params = list(values)
    if not params:
        return "1 = 0", []
    return f"{safe_column} IN ({placeholders(len(params), backend)})", params


def insert_ignore_statement(table: str, columns: Sequence[str], conflict_columns: Sequence[str], backend: str = "postgres") -> str:
    normalize_backend_name(backend)
    safe_table = validate_identifier(table)
    safe_columns = [validate_identifier(column) for column in columns]
    safe_conflicts = [validate_identifier(column) for column in conflict_columns]
    if not safe_columns or not safe_conflicts:
        raise ValueError("Insert-ignore columns and conflict columns must not be empty")
    return (f"INSERT INTO {safe_table}({', '.join(safe_columns)}) VALUES "
            f"({placeholders(len(safe_columns))}) ON CONFLICT ({', '.join(safe_conflicts)}) DO NOTHING")


def prepare_insert_returning_id(sql: str, backend: str = "postgres", id_column: str = "id") -> str:
    normalize_backend_name(backend)
    if re.search(r"\bRETURNING\b", sql, flags=re.IGNORECASE):
        return sql
    return f"{sql.rstrip().rstrip(';')} RETURNING {validate_identifier(id_column)}"


def extract_inserted_id(cursor: Any, backend: str = "postgres") -> int:
    normalize_backend_name(backend)
    row = cursor.fetchone()
    if row is None:
        inserted_id = None
    elif isinstance(row, dict) or hasattr(row, "keys"):
        inserted_id = row["id"]
    else:
        inserted_id = row[0]
    if inserted_id is None:
        raise RuntimeError("Could not extract inserted id for postgres cursor")
    return int(inserted_id)


def row_to_dict(row: Any) -> dict[str, Any] | None:
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
    result = []
    for row in rows:
        converted = row_to_dict(row)
        if converted is None:
            raise TypeError("rows_to_dicts does not accept None rows")
        result.append(converted)
    return result


def to_db_bool(value: bool | int | None, backend: str = "postgres") -> bool | None:
    normalize_backend_name(backend)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"Invalid boolean value for database storage: {value!r}")


def from_db_bool(value: Any) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if value in {"0", "1"}:
        return value == "1"
    raise ValueError(f"Invalid database boolean value: {value!r}")
