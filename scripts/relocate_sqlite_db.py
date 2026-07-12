from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class RelocatePlan:
    source: Path
    target: Path
    backup_dir: Path
    dry_run: bool
    overwrite: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely relocate a SQLite database to a persistent data directory."
    )
    parser.add_argument("--source", required=True, type=Path, help="Current SQLite database path")
    parser.add_argument("--target", required=True, type=Path, help="New SQLite database path")
    parser.add_argument("--backup-dir", required=True, type=Path, help="Directory for pre-move backups")
    parser.add_argument("--dry-run", action="store_true", help="Plan only; do not write files (default)")
    parser.add_argument("--apply", action="store_true", help="Create backup and target database")
    parser.add_argument("--yes", action="store_true", help="Required with --apply")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing target database")
    return parser


def backup_sqlite_database(source: Path, destination: Path) -> None:
    """Copy source into destination through the sqlite3 backup API, including WAL content."""
    with sqlite3.connect(source) as source_conn:
        with sqlite3.connect(destination) as destination_conn:
            source_conn.backup(destination_conn)


def backup_path(backup_dir: Path, source: Path, now: datetime | None = None) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    stem = source.stem or "mvp"
    return backup_dir / f"{stem}.backup.{timestamp}.sqlite3"


def validate_plan(plan: RelocatePlan) -> None:
    if not plan.source.exists():
        raise FileNotFoundError(f"Source database does not exist: {plan.source}")
    if not plan.source.is_file():
        raise ValueError(f"Source is not a file: {plan.source}")
    if plan.target.exists() and not plan.overwrite:
        raise FileExistsError(
            f"Target already exists: {plan.target}. Re-run with --overwrite if replacement is intentional."
        )
    if plan.source.resolve() == plan.target.resolve():
        raise ValueError("Source and target must be different paths")


def relocate_sqlite_db(plan: RelocatePlan) -> tuple[Path, Path]:
    validate_plan(plan)
    planned_backup = backup_path(plan.backup_dir, plan.source)
    if plan.dry_run:
        return planned_backup, plan.target

    plan.backup_dir.mkdir(parents=True, exist_ok=True)
    plan.target.parent.mkdir(parents=True, exist_ok=True)
    backup_sqlite_database(plan.source, planned_backup)
    backup_sqlite_database(plan.source, plan.target)
    return planned_backup, plan.target


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    apply = bool(args.apply)
    dry_run = not apply or bool(args.dry_run)
    if args.apply and args.dry_run:
        parser.error("Use either --dry-run or --apply, not both")
    if args.apply and not args.yes:
        parser.error("--apply requires --yes")

    plan = RelocatePlan(
        source=args.source,
        target=args.target,
        backup_dir=args.backup_dir,
        dry_run=dry_run,
        overwrite=bool(args.overwrite),
    )
    try:
        planned_backup, target = relocate_sqlite_db(plan)
    except Exception as exc:  # CLI boundary: show concise message.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"Mode: {mode}")
    print(f"Source: {plan.source}")
    print(f"Backup: {planned_backup}")
    print(f"Target: {target}")
    if dry_run:
        print("No files were created or modified.")
    else:
        print("Relocation copy completed. Source database was left in place.")
        print("Add this line to your .env:")
        print(f"SQLITE_DB_PATH={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
