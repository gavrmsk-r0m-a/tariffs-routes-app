#!/usr/bin/env python3
"""Start TeleRoute with the production Waitress WSGI server."""
from __future__ import annotations

import os
import sys
from typing import Mapping


def validate_production_environment(environ: Mapping[str, str]) -> tuple[str, int]:
    """Validate the production gate without importing the application."""
    if (environ.get("DB_BACKEND") or "").strip().lower() != "postgres":
        raise RuntimeError("DB_BACKEND must equal postgres")
    if not (environ.get("DATABASE_URL") or "").strip():
        raise RuntimeError("DATABASE_URL is required")
    if (environ.get("MVP_PRODUCTION_SECURITY") or "").strip() != "1":
        raise RuntimeError("MVP_PRODUCTION_SECURITY must equal 1")

    # Reuse the application's canonical secret policy.
    from app.security import validate_auth_secret

    errors = validate_auth_secret(environ)
    if errors:
        raise RuntimeError("Invalid production security configuration: " + "; ".join(errors))
    host = (environ.get("HOST") or "127.0.0.1").strip()
    try:
        port = int((environ.get("PORT") or "8000").strip())
    except ValueError as exc:
        raise RuntimeError("PORT must be an integer") from exc
    if not host:
        raise RuntimeError("HOST must not be empty")
    if not 1 <= port <= 65535:
        raise RuntimeError("PORT must be between 1 and 65535")
    return host, port


def main() -> int:
    try:
        host, port = validate_production_environment(os.environ)
    except Exception as exc:
        # Validation messages contain configuration names only, never values.
        print(f"TeleRoute production startup failed: {exc}", file=sys.stderr)
        return 1
    try:
        # Deliberately import only after validation. Importing app.server loads
        # runtime configuration, but does not create schemas or users.
        from app.server import app
        from waitress import serve

        print(f"Starting TeleRoute production server on {host}:{port}")
        serve(app, host=host, port=port)
    except Exception as exc:
        # Driver/server errors can contain credentials; report only their type.
        print(f"TeleRoute production startup failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
