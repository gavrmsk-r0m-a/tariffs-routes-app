#!/usr/bin/env python3
"""Apply the canonical PostgreSQL schema and ordered migrations."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect_postgres  # noqa: E402

SCHEMA_PATH = ROOT / "docs/postgres/schema.postgres.sql"
MIGRATIONS_PATH = ROOT / "docs/postgres/migrations"


def apply_schema_and_migrations(database_url: str) -> list[str]:
    """Apply the canonical schema and every ordered SQL migration transactionally."""
    conn = connect_postgres(database_url)
    applied: list[str] = []
    try:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        for migration_path in sorted(MIGRATIONS_PATH.glob("*.sql")):
            conn.execute(migration_path.read_text(encoding="utf-8"))
            applied.append(migration_path.name)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return applied


def main() -> int:
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        applied = apply_schema_and_migrations(database_url)
    except Exception as exc:
        print(
            f"PostgreSQL schema/migration apply failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    print(f"PostgreSQL schema is ready; migrations processed: {len(applied)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
