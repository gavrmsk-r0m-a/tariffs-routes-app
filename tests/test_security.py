import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from app.security import auth_cookie_attributes, login_failure_count, login_lock_remaining, record_login_failure


class LoginThrottleTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE login_attempts (username_normalized TEXT, client_key TEXT, failed_at TEXT, reason TEXT)"
        )
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()

    def test_three_failures_lock_for_one_minute_then_reset(self):
        for _ in range(3):
            record_login_failure(self.conn, "Duty", "client", now=self.now)

        self.assertEqual(login_failure_count(self.conn, "duty", "client", now=self.now), 3)
        self.assertEqual(login_lock_remaining(self.conn, "duty", "client", now=self.now), 60)
        self.assertEqual(login_lock_remaining(self.conn, "duty", "client", now=self.now + timedelta(seconds=13)), 47)
        self.assertEqual(login_lock_remaining(self.conn, "duty", "client", now=self.now + timedelta(seconds=60)), 0)
        self.assertEqual(login_failure_count(self.conn, "duty", "client", now=self.now + timedelta(seconds=60)), 0)


if __name__ == "__main__":
    unittest.main()


class ProductionCookiePolicyTest(unittest.TestCase):
    def test_secure_cookie_is_default_in_production(self):
        attrs = auth_cookie_attributes({
            "MVP_PRODUCTION_SECURITY": "1",
            "MVP_AUTH_SECRET": "x" * 32,
        })
        self.assertTrue(attrs["Secure"])

    def test_internal_http_override_removes_secure_cookie_only_when_explicit(self):
        attrs = auth_cookie_attributes({
            "MVP_PRODUCTION_SECURITY": "1",
            "MVP_AUTH_SECRET": "x" * 32,
            "MVP_ALLOW_INSECURE_HTTP": "1",
        })
        self.assertNotIn("Secure", attrs)
