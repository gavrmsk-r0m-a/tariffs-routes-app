from __future__ import annotations

import sqlite3
from dataclasses import dataclass

UNIQUE_VIOLATION = "unique_violation"
FOREIGN_KEY_VIOLATION = "foreign_key_violation"
NOT_NULL_VIOLATION = "not_null_violation"
CHECK_VIOLATION = "check_violation"
LOCK_TIMEOUT = "lock_timeout"
DEADLOCK_DETECTED = "deadlock_detected"
SERIALIZATION_FAILURE = "serialization_failure"
UNKNOWN_DATABASE_ERROR = "unknown_database_error"

SQLSTATE_ERROR_KINDS = {
    "23505": UNIQUE_VIOLATION,
    "23503": FOREIGN_KEY_VIOLATION,
    "23502": NOT_NULL_VIOLATION,
    "23514": CHECK_VIOLATION,
    "40001": SERIALIZATION_FAILURE,
    "40P01": DEADLOCK_DETECTED,
    "55P03": LOCK_TIMEOUT,
}


@dataclass(frozen=True)
class DbErrorInfo:
    kind: str
    backend: str
    table: str | None = None
    columns: tuple[str, ...] = ()
    constraint: str | None = None
    sqlstate: str | None = None
    raw_message: str = ""


def _split_sqlite_table_columns(value: str) -> tuple[str | None, tuple[str, ...]]:
    refs = [part.strip() for part in value.split(",") if part.strip()]
    table: str | None = None
    columns: list[str] = []
    for ref in refs:
        ref_table, sep, column = ref.partition(".")
        if sep:
            table = table or ref_table
            columns.append(column)
        else:
            columns.append(ref)
    return table, tuple(columns)


def _sqlite_error_info(exc: Exception, backend: str, raw_message: str) -> DbErrorInfo | None:
    if isinstance(exc, sqlite3.IntegrityError):
        if raw_message.startswith("UNIQUE constraint failed: "):
            table, columns = _split_sqlite_table_columns(raw_message.removeprefix("UNIQUE constraint failed: "))
            return DbErrorInfo(UNIQUE_VIOLATION, backend, table=table, columns=columns, raw_message=raw_message)
        if raw_message == "FOREIGN KEY constraint failed":
            return DbErrorInfo(FOREIGN_KEY_VIOLATION, backend, raw_message=raw_message)
        if raw_message.startswith("NOT NULL constraint failed: "):
            table, columns = _split_sqlite_table_columns(raw_message.removeprefix("NOT NULL constraint failed: "))
            return DbErrorInfo(NOT_NULL_VIOLATION, backend, table=table, columns=columns, raw_message=raw_message)
        if raw_message.startswith("CHECK constraint failed"):
            constraint = raw_message.partition(":")[2].strip() or None
            return DbErrorInfo(CHECK_VIOLATION, backend, constraint=constraint, raw_message=raw_message)
    if isinstance(exc, sqlite3.OperationalError):
        if raw_message.lower() in {"database is locked", "database table is locked"}:
            return DbErrorInfo(LOCK_TIMEOUT, backend, raw_message=raw_message)
    return None


def _get_sqlstate(exc: Exception) -> str | None:
    for current in (exc, getattr(exc, "__cause__", None)):
        if current is None:
            continue
        sqlstate = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if sqlstate:
            return str(sqlstate)
    return None


def map_database_error(exc: Exception, backend: str = "sqlite") -> DbErrorInfo:
    normalized_backend = "postgres" if backend == "postgresql" else backend
    raw_message = str(exc)

    sqlstate = _get_sqlstate(exc)
    if sqlstate in SQLSTATE_ERROR_KINDS:
        return DbErrorInfo(SQLSTATE_ERROR_KINDS[sqlstate], normalized_backend, sqlstate=sqlstate, raw_message=raw_message)

    if normalized_backend == "sqlite":
        sqlite_info = _sqlite_error_info(exc, normalized_backend, raw_message)
        if sqlite_info is not None:
            return sqlite_info

    return DbErrorInfo(UNKNOWN_DATABASE_ERROR, normalized_backend, sqlstate=sqlstate, raw_message=raw_message)
