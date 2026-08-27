import os
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.db import init_db
from app.security import (
    DEV_AUTH_SECRET,
    auth_cookie_attributes,
    clear_login_failures,
    get_auth_cookie_secret,
    login_is_locked,
    record_login_failure,
    security_gate_facts,
    validate_auth_secret,
)
from app import server


STRONG_SECRET = "stage-67d-auth-secret-with-more-than-32-characters"


class PostgresSecurityGateTests(unittest.TestCase):
    def test_dev_secret_fallback_and_cookie_policy(self):
        self.assertEqual(get_auth_cookie_secret({}), DEV_AUTH_SECRET)
        self.assertEqual(validate_auth_secret({}), [])
        attributes = auth_cookie_attributes({})
        self.assertTrue(attributes["HttpOnly"])
        self.assertEqual(attributes["SameSite"], "Lax")
        self.assertEqual(attributes["Path"], "/")
        self.assertNotIn("Secure", attributes)

    def test_production_requires_non_obvious_strong_secret(self):
        for secret in (None, "secret", "changeme", DEV_AUTH_SECRET, "too-short"):
            env = {"MVP_PRODUCTION_SECURITY": "1"}
            if secret is not None:
                env["MVP_AUTH_SECRET"] = secret
            with self.subTest(secret=secret):
                self.assertTrue(validate_auth_secret(env))
                with self.assertRaises(RuntimeError):
                    get_auth_cookie_secret(env)
        env = {"MVP_PRODUCTION_SECURITY": "1", "MVP_AUTH_SECRET": STRONG_SECRET}
        self.assertEqual(get_auth_cookie_secret(env), STRONG_SECRET)

    def test_production_cookie_is_secure_without_exposing_secret(self):
        with patch.dict(os.environ, {"MVP_PRODUCTION_SECURITY": "1", "MVP_AUTH_SECRET": STRONG_SECRET}, clear=True):
            header = server.auth_cookie_header(7)[1]
        for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/"):
            self.assertIn(flag, header)
        self.assertNotIn(STRONG_SECRET, header)

    def test_production_bootstraps_only_configured_admin(self):
        env = {
            "MVP_PRODUCTION_SECURITY": "1",
            "MVP_BOOTSTRAP_ADMIN_USERNAME": "ops-admin",
            "MVP_BOOTSTRAP_ADMIN_PASSWORD": "Long-Random-Bootstrap-Password",
        }
        with patch.dict(os.environ, env, clear=True):
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            init_db(conn)
        users = conn.execute("SELECT username FROM users").fetchall()
        self.assertEqual([row["username"] for row in users], ["ops-admin"])

    def test_missing_or_weak_production_bootstrap_is_rejected(self):
        cases = ({"MVP_PRODUCTION_SECURITY": "1"}, {
            "MVP_PRODUCTION_SECURITY": "1",
            "MVP_BOOTSTRAP_ADMIN_USERNAME": "admin",
            "MVP_BOOTSTRAP_ADMIN_PASSWORD": "admin",
        })
        for env in cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
                with self.assertRaises(RuntimeError):
                    init_db(conn)

    def test_sliding_window_lockout_expiry_and_clear(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE login_attempts(id INTEGER PRIMARY KEY, username_normalized TEXT, client_key TEXT, failed_at TEXT, reason TEXT)")
        now = datetime(2026, 7, 27, tzinfo=timezone.utc)
        settings = {"MVP_LOGIN_MAX_FAILED_ATTEMPTS": "5", "MVP_LOGIN_FAILURE_WINDOW_SECONDS": "900", "MVP_LOGIN_LOCKOUT_SECONDS": "900"}
        for offset in range(5):
            record_login_failure(conn, " User ", "client-hash", now=now + timedelta(seconds=offset))
        self.assertTrue(login_is_locked(conn, "user", "client-hash", environ=settings, now=now + timedelta(seconds=5)))
        self.assertFalse(login_is_locked(conn, "user", "client-hash", environ=settings, now=now + timedelta(seconds=905)))
        clear_login_failures(conn, "user", "client-hash")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0], 0)

    def test_old_failures_do_not_lock_and_no_password_is_stored(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE login_attempts(id INTEGER PRIMARY KEY, username_normalized TEXT, client_key TEXT, failed_at TEXT, reason TEXT)")
        now = datetime(2026, 7, 27, tzinfo=timezone.utc)
        for _ in range(5):
            record_login_failure(conn, "missing", "client-hash", now=now - timedelta(seconds=901))
        self.assertFalse(login_is_locked(conn, "missing", "client-hash", now=now))
        columns = {row[1] for row in conn.execute("PRAGMA table_info(login_attempts)")}
        self.assertNotIn("password", columns)

    def test_production_forbids_passwordless_user_switching(self):
        self.assertFalse(security_gate_facts({"MVP_PRODUCTION_SECURITY": "1"})["passwordless_user_switching_allowed"])


if __name__ == "__main__":
    unittest.main()
