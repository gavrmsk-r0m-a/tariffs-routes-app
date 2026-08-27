from __future__ import annotations

import os
from dataclasses import dataclass

SUPPORTED_DB_BACKENDS = frozenset({"postgres", "postgresql"})
DB_BACKEND_REQUIRED_MESSAGE = "DB_BACKEND is required and must be set to postgres"
POSTGRES_DATABASE_URL_REQUIRED_MESSAGE = "DATABASE_URL is required for PostgreSQL runtime"


@dataclass(frozen=True)
class DbConfig:
    """Validated PostgreSQL runtime configuration."""

    backend: str
    database_url: str


def load_db_config(environ: dict[str, str] | None = None) -> DbConfig:
    """Load the fail-closed PostgreSQL-only runtime configuration."""
    env = os.environ if environ is None else environ
    raw_backend = env.get("DB_BACKEND")
    if raw_backend is None or not raw_backend.strip():
        raise ValueError(DB_BACKEND_REQUIRED_MESSAGE)
    backend = raw_backend.strip().lower()
    if backend not in SUPPORTED_DB_BACKENDS:
        raise ValueError(
            f"Unsupported DB_BACKEND: {backend}; TeleRoute runtime requires PostgreSQL"
        )
    database_url = (env.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise ValueError(POSTGRES_DATABASE_URL_REQUIRED_MESSAGE)
    return DbConfig(backend="postgres", database_url=database_url)


def connect_postgres(database_url: str):
    if not database_url or not database_url.strip():
        raise ValueError(POSTGRES_DATABASE_URL_REQUIRED_MESSAGE)
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is required for PostgreSQL runtime; install psycopg[binary]"
        ) from exc
    return psycopg.connect(database_url, row_factory=dict_row)


def connect_database(config: DbConfig, environ: dict[str, str] | None = None):
    """Open PostgreSQL; connection and driver failures are never downgraded."""
    del environ  # retained as a harmless compatibility argument for callers
    if config.backend not in SUPPORTED_DB_BACKENDS:
        raise ValueError(
            f"Unsupported DB_BACKEND: {config.backend}; TeleRoute runtime requires PostgreSQL"
        )
    return connect_postgres(config.database_url)
