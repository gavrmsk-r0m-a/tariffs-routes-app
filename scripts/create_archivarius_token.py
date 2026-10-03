#!/usr/bin/env python3
"""Create or deliberately rotate an Archivarius service token."""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.archivarius import token_hash
from app.db import connect_postgres


def provision(database_url: str, name: str, created_by_user: str, *, rotate: bool, comment: str | None = None) -> str:
    token = secrets.token_urlsafe(48)
    digest = token_hash(token)
    conn = connect_postgres(database_url)
    try:
        user = conn.execute("SELECT id FROM users WHERE username=%s AND is_active IS TRUE", (created_by_user,)).fetchone()
        if user is None:
            raise ValueError("created-by user does not exist or is inactive")
        existing = conn.execute("SELECT id FROM api_tokens WHERE name=%s", (name,)).fetchone()
        if existing and not rotate:
            raise ValueError("token name already exists; pass --rotate to replace it deliberately")
        if existing:
            conn.execute("""UPDATE api_tokens SET token_hash=%s, is_active=TRUE, created_by=%s,
                         created_at=CURRENT_TIMESTAMP, last_used_at=NULL, comment=%s WHERE id=%s""",
                         (digest, user["id"], comment, existing["id"]))
        else:
            conn.execute("INSERT INTO api_tokens(name,token_hash,is_active,created_by,comment) VALUES (%s,%s,TRUE,%s,%s)",
                         (name, digest, user["id"], comment))
        conn.commit()
        return token
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--created-by-user", required=True)
    parser.add_argument("--comment")
    parser.add_argument("--rotate", action="store_true", help="replace an existing named token")
    args = parser.parse_args(argv)
    try:
        token = provision(args.database_url, args.name, args.created_by_user, rotate=args.rotate, comment=args.comment)
    except Exception as exc:
        print(f"Token was not created: {exc}", file=sys.stderr)
        return 1
    print("Store this token securely. It will not be shown again:")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
