#!/usr/bin/env python3
"""Stage 70A PostgreSQL-only production runtime gate."""
from __future__ import annotations
import argparse
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_FILES = tuple((ROOT / "app").glob("*.py"))
FORBIDDEN_RUNTIME_TOKENS = ("sqlite3", "mvp.sqlite3", "DB_PATH", "PRAGMA", "sqlite_master")

def audit() -> dict:
    blockers: list[str] = []
    db_source = (ROOT / "app/db.py").read_text(encoding="utf-8")
    try:
        for path in APP_FILES:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        blockers.append(f"application syntax error: {exc}")
    for path in APP_FILES:
        source = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_RUNTIME_TOKENS:
            if token in source:
                blockers.append(f"runtime token {token!r} remains in {path.relative_to(ROOT)}")
    required = ("DB_BACKEND_REQUIRED_MESSAGE", "POSTGRES_DATABASE_URL_REQUIRED_MESSAGE", "connect_postgres")
    if not all(item in db_source for item in required):
        blockers.append("fail-closed PostgreSQL configuration contract is incomplete")
    security_ok = (ROOT / "tests/test_postgres_security_gate.py").is_file()
    if not security_ok:
        blockers.append("PostgreSQL security gate is missing")
    return {
        "status": "ready" if not blockers else "failed",
        "security_gate": "ok" if security_ok else "failed",
        "runtime_backend": "postgres",
        "sqlite_fallback": False,
        "blockers": blockers,
    }

def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    result = audit()
    print(json.dumps(result, indent=2, sort_keys=True) if args.format == "json" else "\n".join(f"{k}: {v}" for k,v in result.items()))
    return 0 if result["status"] == "ready" else 2

if __name__ == "__main__":
    raise SystemExit(main())
