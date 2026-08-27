"""PostgreSQL SQLSTATE error classification for application runtime."""
from __future__ import annotations
from dataclasses import dataclass

UNIQUE_VIOLATION = "unique_violation"
FOREIGN_KEY_VIOLATION = "foreign_key_violation"
NOT_NULL_VIOLATION = "not_null_violation"
CHECK_VIOLATION = "check_violation"
LOCK_TIMEOUT = "lock_timeout"
DEADLOCK_DETECTED = "deadlock_detected"
SERIALIZATION_FAILURE = "serialization_failure"
UNKNOWN_DATABASE_ERROR = "unknown_database_error"
SQLSTATE_ERROR_KINDS = {"23505": UNIQUE_VIOLATION, "23503": FOREIGN_KEY_VIOLATION,
 "23502": NOT_NULL_VIOLATION, "23514": CHECK_VIOLATION, "40001": SERIALIZATION_FAILURE,
 "40P01": DEADLOCK_DETECTED, "55P03": LOCK_TIMEOUT}

@dataclass(frozen=True)
class DbErrorInfo:
    kind: str
    backend: str
    table: str | None = None
    columns: tuple[str, ...] = ()
    constraint: str | None = None
    sqlstate: str | None = None
    raw_message: str = ""

def _get_sqlstate(exc: Exception) -> str | None:
    for current in (exc, getattr(exc, "__cause__", None)):
        if current is not None:
            state = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
            if state:
                return str(state)
    return None

def map_database_error(exc: Exception, backend: str = "postgres") -> DbErrorInfo:
    if backend not in {"postgres", "postgresql"}:
        raise ValueError(f"Unsupported database backend {backend!r}; expected postgres")
    raw_message = str(exc)
    sqlstate = _get_sqlstate(exc)
    if sqlstate in SQLSTATE_ERROR_KINDS:
        diagnostic = getattr(exc, "diag", None) or getattr(getattr(exc, "__cause__", None), "diag", None)
        table = getattr(diagnostic, "table_name", None)
        constraint = getattr(diagnostic, "constraint_name", None)
        column = getattr(diagnostic, "column_name", None)
        return DbErrorInfo(SQLSTATE_ERROR_KINDS[sqlstate], "postgres",
            table=str(table) if table else None, columns=(str(column),) if column else (),
            constraint=str(constraint) if constraint else None, sqlstate=sqlstate, raw_message=raw_message)
    return DbErrorInfo(UNKNOWN_DATABASE_ERROR, "postgres", sqlstate=sqlstate, raw_message=raw_message)
