"""Central production authentication security policy for TeleRoute."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Mapping

DEV_AUTH_SECRET = "dev-mvp-auth-secret-change-me"
OBVIOUS_SECRETS = {"secret", "changeme", "password", "admin", DEV_AUTH_SECRET}
KNOWN_DEFAULT_PASSWORDS = {"admin", "roman", "duty123", "guest123"}
GENERIC_LOGIN_ERROR = "Неверный логин или пароль"


def _env(environ: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def production_security_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return (_env(environ).get("MVP_PRODUCTION_SECURITY") or "").strip() == "1"


def validate_auth_secret(environ: Mapping[str, str] | None = None) -> list[str]:
    env = _env(environ)
    if not production_security_enabled(env):
        return []
    secret = (env.get("MVP_AUTH_SECRET") or env.get("SECRET_KEY") or "").strip()
    errors: list[str] = []
    if not secret:
        errors.append("production auth secret is required")
    elif len(secret) < 32:
        errors.append("production auth secret must contain at least 32 characters")
    if secret.lower() in OBVIOUS_SECRETS:
        errors.append("development or obvious auth secrets are forbidden in production security mode")
    return errors


def get_auth_cookie_secret(environ: Mapping[str, str] | None = None) -> str:
    env = _env(environ)
    errors = validate_auth_secret(env)
    if errors:
        raise RuntimeError("Invalid production security configuration: " + "; ".join(errors))
    return env.get("MVP_AUTH_SECRET") or env.get("SECRET_KEY") or DEV_AUTH_SECRET


def auth_cookie_attributes(environ: Mapping[str, str] | None = None) -> dict[str, str | bool]:
    attributes: dict[str, str | bool] = {"Path": "/", "HttpOnly": True, "SameSite": "Lax"}
    if production_security_enabled(environ):
        attributes["Secure"] = True
    return attributes


def render_cookie_attributes(environ: Mapping[str, str] | None = None) -> str:
    return "; ".join(key if value is True else f"{key}={value}" for key, value in auth_cookie_attributes(environ).items())


def validate_bootstrap_password(username: str, password: str) -> list[str]:
    errors: list[str] = []
    if len(password) < 12:
        errors.append("bootstrap admin password must contain at least 12 characters")
    if password.casefold() == username.strip().casefold():
        errors.append("bootstrap admin password must differ from username")
    if password.casefold() in KNOWN_DEFAULT_PASSWORDS:
        errors.append("known default passwords are forbidden in production security mode")
    return errors


def login_throttle_settings(environ: Mapping[str, str] | None = None) -> dict[str, int]:
    env = _env(environ)
    values = {
        "max_failed_attempts": int(env.get("MVP_LOGIN_MAX_FAILED_ATTEMPTS", "5")),
        "failure_window_seconds": int(env.get("MVP_LOGIN_FAILURE_WINDOW_SECONDS", "900")),
        "lockout_seconds": int(env.get("MVP_LOGIN_LOCKOUT_SECONDS", "900")),
    }
    if any(value <= 0 for value in values.values()):
        raise ValueError("login throttling settings must be positive integers")
    return values


def normalize_username(username: str) -> str:
    return username.strip().casefold()


def client_key(environ: Mapping[str, str]) -> str:
    identity = f"{environ.get('REMOTE_ADDR', '')}\n{environ.get('HTTP_USER_AGENT', '')}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


def _params(conn, count: int) -> str:
    marker = "%s" if conn.__class__.__module__.startswith("psycopg") else "?"
    return ", ".join(marker for _ in range(count))


def login_is_locked(conn, username: str, client: str, *, environ=None, now=None) -> bool:
    settings = login_throttle_settings(environ)
    current = _now(now)
    cutoff = current - timedelta(seconds=settings["failure_window_seconds"])
    markers = _params(conn, 2).split(", ")
    rows = conn.execute(f"SELECT failed_at FROM login_attempts WHERE username_normalized = {markers[0]} AND client_key = {markers[1]}", (normalize_username(username), client)).fetchall()
    failures = [row["failed_at"] if isinstance(row["failed_at"], datetime) else datetime.fromisoformat(str(row["failed_at"])) for row in rows]
    failures = [item if item.tzinfo else item.replace(tzinfo=timezone.utc) for item in failures]
    recent = [item for item in failures if item >= cutoff]
    if len(recent) < settings["max_failed_attempts"]:
        return False
    return current < max(recent) + timedelta(seconds=settings["lockout_seconds"])


def record_login_failure(conn, username: str, client: str, *, now=None) -> None:
    conn.execute(f"INSERT INTO login_attempts(username_normalized, client_key, failed_at, reason) VALUES ({_params(conn, 4)})", (normalize_username(username), client, _now(now).isoformat(), "invalid_credentials"))
    conn.commit()


def clear_login_failures(conn, username: str, client: str) -> None:
    markers = _params(conn, 2).split(", ")
    conn.execute(f"DELETE FROM login_attempts WHERE username_normalized = {markers[0]} AND client_key = {markers[1]}", (normalize_username(username), client))
    conn.commit()


def security_gate_facts(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    env = _env(environ)
    return {
        "production_security_enabled": production_security_enabled(env),
        "auth_secret_errors": validate_auth_secret(env),
        "cookie_attributes": auth_cookie_attributes(env),
        "login_throttle": login_throttle_settings(env),
        "default_credentials_forbidden_in_production": True,
        "passwordless_user_switching_allowed": not production_security_enabled(env),
    }
