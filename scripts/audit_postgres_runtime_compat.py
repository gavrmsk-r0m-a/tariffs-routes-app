#!/usr/bin/env python3
"""Static helper for PostgreSQL runtime compatibility audits.

The scanner is intentionally conservative and read-only.  It looks for known
SQLite/API and SQL patterns that usually require an adapter before PostgreSQL
runtime support can be enabled.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_EXCLUDES = {".git", "__pycache__", ".venv", "venv", "backups", "data", "logs"}
SCAN_SUFFIXES = {".py", ".sql"}


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    category: str
    pattern: str
    context: str


PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("sqlite3_api", "sqlite3", re.compile(r"\bsqlite3\b")),
    ("sqlite3_api", "sqlite3.Row", re.compile(r"\bsqlite3\.Row\b")),
    ("sqlite3_api", "row_factory", re.compile(r"\brow_factory\b")),
    ("sqlite3_api", "lastrowid", re.compile(r"\blastrowid\b")),
    ("sqlite3_api", "executescript", re.compile(r"\bexecutescript\b")),
    ("sqlite3_api", "total_changes", re.compile(r"\btotal_changes\b")),
    ("sqlite3_api", "sqlite exception", re.compile(r"\bsqlite3\.(?:IntegrityError|OperationalError|DatabaseError)\b")),
    ("ddl_runtime", "PRAGMA", re.compile(r"\bPRAGMA\b", re.I)),
    ("ddl_runtime", "CREATE TABLE IF NOT EXISTS", re.compile(r"\bCREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\b", re.I)),
    ("ddl_runtime", "ALTER TABLE", re.compile(r"\bALTER\s+TABLE\b", re.I)),
    ("ddl_runtime", "CREATE INDEX IF NOT EXISTS", re.compile(r"\bCREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\b", re.I)),
    ("placeholders", "? placeholder", re.compile(r"\?")),
    ("sqlite_sql", "INSERT OR", re.compile(r"\bINSERT\s+OR\b", re.I)),
    ("sqlite_sql", "AUTOINCREMENT", re.compile(r"\bAUTOINCREMENT\b", re.I)),
    ("sqlite_sql", "GLOB", re.compile(r"\bGLOB\b", re.I)),
    ("sqlite_sql", "json_extract", re.compile(r"\bjson_extract\b", re.I)),
    ("sqlite_sql", "json_each", re.compile(r"\bjson_each\b", re.I)),
    ("sqlite_sql", "datetime('now')", re.compile(r"datetime\(\s*['\"]now['\"]\s*\)", re.I)),
    ("sqlite_sql", "strftime", re.compile(r"\bstrftime\b", re.I)),
    ("sqlite_sql", "julianday", re.compile(r"\bjulianday\b", re.I)),
    ("sqlite_sql", "COLLATE NOCASE", re.compile(r"\bCOLLATE\s+NOCASE\b", re.I)),
)


def iter_scan_files(root: Path, excludes: set[str] | None = None) -> Iterable[Path]:
    excludes = excludes or DEFAULT_EXCLUDES
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if any(part in excludes for part in path.relative_to(root).parts):
            continue
        yield path


def scan_path(root: str | Path) -> list[Finding]:
    root_path = Path(root).resolve()
    findings: list[Finding] = []
    for path in iter_scan_files(root_path):
        rel = path.relative_to(root_path).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for number, line in enumerate(lines, start=1):
            for category, label, pattern in PATTERNS:
                if pattern.search(line):
                    findings.append(Finding(rel, number, category, label, line.strip()[:220]))
    return findings


def format_text(findings: Iterable[Finding]) -> str:
    lines = []
    for item in findings:
        lines.append(f"{item.file}:{item.line}: {item.category}: {item.pattern}: {item.context}")
    return "\n".join(lines) + ("\n" if lines else "")


def format_json(findings: Iterable[Finding]) -> str:
    return json.dumps([asdict(item) for item in findings], ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan .py/.sql files for SQLite/PostgreSQL runtime compatibility patterns.")
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1], type=Path, help="Project root to scan.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--output", type=Path, help="Optional output path. The scanner never modifies scanned project files.")
    args = parser.parse_args(argv)

    findings = scan_path(args.root)
    output = format_json(findings) if args.format == "json" else format_text(findings)
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
