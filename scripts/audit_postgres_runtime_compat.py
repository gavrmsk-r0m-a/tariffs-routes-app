#!/usr/bin/env python3
"""Audit the normal application package for PostgreSQL-only runtime semantics."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


FORBIDDEN = (
    "sqlite3",
    "SQLite",
    "sqlite",
    "PRAGMA",
    "sqlite_master",
    "lastrowid",
    "mvp.sqlite3",
    "schema.sql",
)


def audit(root: Path) -> dict:
    findings = []

    for path in sorted((root / "app").rglob("*")):
        if path.is_file() and path.suffix in {".py", ".sql"}:
            source = path.read_text(encoding="utf-8")

            for number, line in enumerate(source.splitlines(), 1):
                for token in FORBIDDEN:
                    if token in line:
                        findings.append(
                            {
                                "file": str(path.relative_to(root)),
                                "line": number,
                                "token": token,
                            }
                        )

    return {
        "status": "postgresql_only" if not findings else "failed",
        "runtime_backend": "postgres",
        "sqlite_runtime_findings": findings,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
    )

    args = parser.parse_args(argv)
    result = audit(args.root.resolve())

    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(
            f"status: {result['status']}\n"
            f"sqlite_runtime_findings: "
            f"{len(result['sqlite_runtime_findings'])}"
        )

    return 0 if result["status"] == "postgresql_only" else 2


if __name__ == "__main__":
    raise SystemExit(main())