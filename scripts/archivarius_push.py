#!/usr/bin/env python3
"""One-shot outbound Archivarius delivery (disabled unless invoked/configured)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.archivarius import ArchivariusExporter, MAX_LIMIT, SCHEMA_VERSION, generated_at, json_bytes
from app.db import connect_postgres
from app.repository import Repository


def read_cursor(path: Path) -> int:
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    cursor = int(data.get("events_cursor", 0))
    if cursor < 0:
        raise ValueError("state cursor cannot be negative")
    return cursor


def save_cursor(path: Path, cursor: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"events_cursor": cursor}) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.netloc:
        return
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    raise ValueError("push URL must use HTTPS (HTTP is allowed only for localhost)")


def post_payload(url: str, token: str, payload: dict, *, opener=urlopen) -> None:
    request = Request(url, data=json_bytes(payload), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "TeleRoute-Archivarius-Bridge/1",
    })
    with opener(request, timeout=30) as response:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        if not 200 <= status < 300:
            raise RuntimeError(f"relay returned HTTP {status}")


def run_once(database_url: str, url: str, token: str, state_file: Path, *, snapshot: bool = False, opener=urlopen) -> int:
    validate_url(url)
    cursor = read_cursor(state_file)
    conn = connect_postgres(database_url)
    try:
        exporter = ArchivariusExporter(Repository(conn))
        if snapshot:
            post_payload(url, token, {"schema_version": SCHEMA_VERSION, "kind": "snapshot", "generated_at": generated_at(), "data": exporter.complete_snapshot()}, opener=opener)
        page = exporter.events(cursor, MAX_LIMIT)
        post_payload(url, token, {"schema_version": SCHEMA_VERSION, "kind": "events", "generated_at": generated_at(), "data": page}, opener=opener)
        save_cursor(state_file, page["next_cursor"])
        return page["next_cursor"]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="perform one delivery and exit")
    parser.add_argument("--snapshot", action="store_true", help="send a complete snapshot before events")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--url", default=os.getenv("ARCHIVARIUS_PUSH_URL", ""))
    parser.add_argument("--token", default=os.getenv("ARCHIVARIUS_PUSH_TOKEN", ""))
    parser.add_argument("--state-file", default=os.getenv("ARCHIVARIUS_PUSH_STATE_FILE", ""))
    args = parser.parse_args(argv)
    if not args.once:
        print("No action: pass --once to enable a delivery.", file=sys.stderr); return 2
    if not all((args.database_url, args.url, args.token, args.state_file)):
        print("DATABASE_URL, push URL, push token, and state file are required.", file=sys.stderr); return 2
    try:
        cursor = run_once(args.database_url, args.url, args.token, Path(args.state_file), snapshot=args.snapshot)
    except Exception as exc:
        print(f"Archivarius delivery failed; cursor unchanged: {exc}", file=sys.stderr); return 1
    print(f"Archivarius delivery acknowledged; cursor={cursor}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
