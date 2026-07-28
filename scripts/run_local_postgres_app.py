#!/usr/bin/env python3
"""Run the real TeleRoute WSGI application against a local PostgreSQL database."""
from __future__ import annotations

import argparse
import importlib
import os
import secrets
import sys
from pathlib import Path
from wsgiref.simple_server import make_server

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.setup_local_postgres import validate_local_database_url


def install_runtime_environment(database_url: str, auth_secret: str) -> None:
    validate_local_database_url(database_url)
    if len(auth_secret) < 32:
        raise ValueError("auth secret must contain at least 32 characters")
    os.environ.update({
        "DB_BACKEND": "postgres",
        "POSTGRES_RUNTIME_ENABLED": "1",
        "DATABASE_URL": database_url,
        "MVP_PRODUCTION_SECURITY": "1",
        "MVP_AUTH_SECRET": auth_secret,
    })


def load_application(database_url: str, auth_secret: str):
    install_runtime_environment(database_url, auth_secret)
    # app.server freezes its DB configuration and cookie secret during import.
    return importlib.import_module("app.server").app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--auth-secret", help="local-only secret (generated when omitted)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    auth_secret = args.auth_secret or ("local-only-" + secrets.token_urlsafe(32))
    application = load_application(args.database_url, auth_secret)
    url = f"http://127.0.0.1:{args.port}/login"
    print(f"TeleRoute local PostgreSQL runtime: {url}")
    with make_server("127.0.0.1", args.port, application) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
