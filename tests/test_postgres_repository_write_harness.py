import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "postgres_repository_write_harness.py"
SPEC = importlib.util.spec_from_file_location("stage51_write_harness", SCRIPT)
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


class FakeConnection:
    def __init__(self, fail_restore=False):
        self.commands, self.aborted = [], False
        self.rollbacks = 0
        self.commits = 0
        self.fail_restore = fail_restore
        self.closed = False
        self.dictionary = {}
    class Cursor:
        def __init__(self, row=None): self.row = row
        def fetchone(self): return self.row
    def execute(self, sql, params=()):
        self.commands.append(sql)
        if "FROM countries WHERE name" in sql: return self.Cursor(self.dictionary.get("country"))
        if "FROM currencies WHERE code" in sql: return self.Cursor(self.dictionary.get("currency"))
        if "FROM providers WHERE name" in sql: return self.Cursor(self.dictionary.get("provider"))
        if "FROM provider_prefixes WHERE prefix" in sql: return self.Cursor(self.dictionary.get("prefix"))
        if sql == "BEGIN":
            self.aborted = False
        if "definitely_missing" in sql:
            self.aborted = True
            raise RuntimeError("missing table")
        if sql == "ROLLBACK TO SAVEPOINT stage51_probe":
            self.aborted = False
        if sql == "SELECT 1" and self.aborted:
            raise RuntimeError("current transaction is aborted")
    def rollback(self): self.rollbacks += 1; self.aborted = False; self.dictionary = {}
    def commit(self): self.commits += 1; raise AssertionError("harness must never commit")
    def close(self): self.closed = True


class FakeRepo:
    def __init__(self, conn): self.conn, self.value, self.calls = conn, None, []
    def get_hlr_limit_override(self):
        if self.conn.rollbacks >= 2:
            return "changed" if self.conn.fail_restore else None
        return self.value
    def set_hlr_limit_override(self, value, **kwargs):
        self.calls.append((value, kwargs)); self.value = value
    def get_app_setting_value(self, key):
        return getattr(self, "setting", None)
    def set_app_setting_value(self, key, value, updated_by=None, **kwargs):
        self.calls.append((key, value, updated_by, kwargs)); self.setting = value
    def delete_app_setting_value(self, key, **kwargs):
        self.calls.append((key, kwargs)); self.setting = None
    def get_hlr_daily_usage(self, usage_date):
        if getattr(self, "usage_transaction_rollbacks", self.conn.rollbacks) < self.conn.rollbacks:
            return {"checked_today": 0, "credits_spent_today": None, "last_check_count": 0, "last_check_credits": None, "updated_at": None}
        return getattr(self, "usage", {"checked_today": 0, "credits_spent_today": None, "last_check_count": 0, "last_check_credits": None, "updated_at": None})
    def upsert_hlr_daily_usage(self, usage_date, checked_count_delta, credits_delta=None, last_check_at=None, **kwargs):
        if not hasattr(self, "usage_transaction_rollbacks"):
            self.usage_transaction_rollbacks = self.conn.rollbacks
        current = self.get_hlr_daily_usage(usage_date)
        self.usage = {"checked_today": current["checked_today"] + checked_count_delta,
                      "credits_spent_today": Decimal(str(current["credits_spent_today"] or 0)) + Decimal(str(credits_delta or 0)) if credits_delta is not None else current["credits_spent_today"],
                      "last_check_count": checked_count_delta, "last_check_credits": credits_delta, "updated_at": last_check_at}
        self.calls.append((usage_date, checked_count_delta, credits_delta, last_check_at, kwargs))
    def get_user_by_username(self, username):
        if getattr(self, "user_rollback", None) is not None and self.conn.rollbacks > self.user_rollback:
            return None
        return getattr(self, "user", None)
    def create_user(self, username, **kwargs):
        self.calls.append(("create_user", username, kwargs)); self.user_rollback = self.conn.rollbacks; self.user = {"id": 53, "username": username, "display_name": kwargs["display_name"], "email": kwargs["email"], "role_key": kwargs["role"], "is_active": True, "must_change_password": kwargs["must_change_password"]}; self.password = kwargs["password"]; return 53
    def update_user(self, user_id, **kwargs):
        self.calls.append(("update_user", user_id, kwargs)); self.user.update(display_name=kwargs["display_name"], email=kwargs["email"], role_key=kwargs["role_key"], is_active=kwargs["is_active"])
    def set_user_permissions(self, user_id, permissions, **kwargs):
        self.calls.append(("set_user_permissions", user_id, permissions, kwargs)); self.permissions = {key: dict(value) for key, value in permissions.items()}
    def get_user_section_permission(self, user_id, section): return getattr(self, "permissions", {}).get(section)
    def get_user_permissions(self, user_id): return {} if getattr(self, "user_rollback", None) is not None and self.conn.rollbacks > self.user_rollback else getattr(self, "permissions", {})
    def update_user_password(self, user_id, password, **kwargs):
        self.calls.append(("update_user_password", user_id, password, kwargs)); self.password = password; self.user["must_change_password"] = kwargs["must_change_password"]
    def authenticate_user(self, username, password): return self.user if getattr(self, "user", None) and password == getattr(self, "password", None) else None
    def create_country(self, name, code, **kwargs):
        self.calls.append(("create_country", name, code, kwargs)); self.conn.dictionary["country"] = {"id": 54, "is_active": True}; return 54
    def create_currency(self, code, name, symbol, **kwargs):
        self.calls.append(("create_currency", code, name, symbol, kwargs)); self.conn.dictionary["currency"] = {"id": 55, "is_active": True}; return 55
    def create_provider(self, name, **kwargs):
        self.calls.append(("create_provider", name, kwargs)); self.conn.dictionary["provider"] = {"id": 56, "default_currency_id": kwargs["default_currency_id"], "is_active": True}; return 56
    def create_prefix(self, provider_id, prefix, name, **kwargs):
        self.calls.append(("create_prefix", provider_id, prefix, name, kwargs)); self.conn.dictionary["prefix"] = {"id": 57, "provider_id": provider_id, "prefix": prefix, "is_active": True}; return 57



class WriteHarnessTest(unittest.TestCase):
    def test_import_is_driver_free_and_masks_password(self):
        self.assertEqual(harness.mask_postgres_url("postgresql://user:secret@host/db"), "postgresql://user:***@host/db")
        self.assertNotIn("psycopg", harness.__dict__)

    def test_rollback_probe_uses_caller_owned_write_and_never_commits(self):
        conn, repo = FakeConnection(), FakeRepo(None)
        repo.conn = conn
        harness.run_rollback_probe(repo, conn, "key", "5151")
        self.assertEqual(repo.calls, [("5151", {"commit": False})])
        self.assertGreaterEqual(conn.rollbacks, 3)
        self.assertEqual(conn.commits, 0)

    def test_rollback_probe_rolls_back_on_failure_and_detects_restore_failure(self):
        conn, repo = FakeConnection(fail_restore=True), FakeRepo(None)
        repo.conn = conn
        with self.assertRaisesRegex(AssertionError, "did not restore"):
            harness.run_rollback_probe(repo, conn, "key", "5151")
        self.assertGreaterEqual(conn.rollbacks, 3)

    def test_aborted_transaction_and_savepoint_sequences(self):
        aborted = FakeConnection()
        harness.run_aborted_transaction_probe(aborted)
        self.assertEqual(aborted.commands[:2], ["BEGIN", "SELECT * FROM definitely_missing_stage51_table"])
        self.assertGreaterEqual(aborted.rollbacks, 2)
        savepoint = FakeConnection()
        harness.run_savepoint_probe(savepoint)
        self.assertIn("SAVEPOINT stage51_probe", savepoint.commands)
        self.assertIn("ROLLBACK TO SAVEPOINT stage51_probe", savepoint.commands)
        self.assertEqual(savepoint.commits, 0)

    def test_stage52_app_setting_probe_is_rollback_only(self):
        conn, repo = FakeConnection(), FakeRepo(None); repo.conn = conn
        harness.run_app_setting_probe(repo, conn)
        self.assertIn((harness.APP_SETTING_PROBE_KEY, "stage52-value", None, {"commit": False}), repo.calls)
        self.assertEqual(conn.commits, 0)
        self.assertGreaterEqual(conn.rollbacks, 3)

    def test_stage52_hlr_usage_probe_is_decimal_safe_and_rollback_only(self):
        conn, repo = FakeConnection(), FakeRepo(None); repo.conn = conn
        harness.run_hlr_daily_usage_probe(repo, conn)
        self.assertEqual(repo.calls[-1][1:4], (2, "0.25", "2099-12-31 10:05"))
        self.assertEqual(conn.commits, 0)
        self.assertGreaterEqual(conn.rollbacks, 3)

    def test_stage52_usage_timestamp_assertion_accepts_postgres_datetime(self):
        expected = {
            "checked_today": 3,
            "credits_spent_today": "0.75",
            "last_check_count": 3,
            "last_check_credits": "0.75",
            "updated_at": "2099-12-31 10:00",
        }
        harness._assert_usage({**expected, "updated_at": "2099-12-31 10:00"}, expected)
        harness._assert_usage({**expected, "updated_at": datetime(2099, 12, 31, 10, 0)}, expected)
        with self.assertRaisesRegex(AssertionError, "updated_at"):
            harness._assert_usage({**expected, "updated_at": datetime(2099, 12, 31, 10, 5)}, expected)

    def test_stage53_user_admin_probe_is_rollback_only_and_uses_caller_owned_writes(self):
        conn, repo = FakeConnection(), FakeRepo(None); repo.conn = conn
        harness.run_user_admin_probe(repo, conn)
        writes = [call for call in repo.calls if isinstance(call[0], str) and call[0] in {"create_user", "update_user", "set_user_permissions", "update_user_password"}]
        self.assertEqual([call[0] for call in writes], ["create_user", "update_user", "set_user_permissions", "update_user_password"])
        self.assertTrue(all(call[-1]["commit"] is False for call in writes))
        self.assertEqual(conn.commits, 0)
        self.assertGreaterEqual(conn.rollbacks, 3)

    def test_stage54_dictionary_probe_is_rollback_only_and_uses_caller_owned_writes(self):
        conn, repo = FakeConnection(), FakeRepo(None); repo.conn = conn
        harness.run_dictionary_create_probe(repo, conn)
        writes = [call for call in repo.calls if call[0] in {"create_country", "create_currency", "create_provider", "create_prefix"}]
        self.assertEqual([call[0] for call in writes], ["create_country", "create_currency", "create_provider", "create_prefix"])
        self.assertTrue(all(call[-1]["commit"] is False for call in writes))
        self.assertEqual(conn.dictionary, {})
        self.assertEqual(conn.commits, 0)
        self.assertGreaterEqual(conn.rollbacks, 3)

    def test_missing_url_is_parser_error(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(SystemExit) as caught:
            harness.main([])
        self.assertEqual(caught.exception.code, 2)

    def test_json_and_output_with_fake_driver(self):
        class Driver:
            @staticmethod
            def connect(*args, **kwargs): return FakeConnection()
        import types
        with tempfile.TemporaryDirectory() as directory, patch.dict("sys.modules", {"psycopg": Driver, "psycopg.rows": types.SimpleNamespace(dict_row=dict)}), patch.object(harness, "Repository", lambda conn, backend: FakeRepo(conn)):
            output = Path(directory) / "summary.json"
            self.assertEqual(harness.main(["--postgres-url", "postgresql://u:pw@h/db", "--json", "--output", str(output)]), 0)
            summary = json.loads(output.read_text())
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(set(summary), {"status", "postgres_url", "checks_count", "failures", "probes"})
        self.assertEqual(summary["probes"], {"rollback_probe": "ok", "aborted_transaction_probe": "ok", "savepoint_probe": "ok", "app_setting_probe": "ok", "hlr_daily_usage_probe": "ok", "user_admin_probe": "ok", "dictionary_create_probe": "ok"})

    def test_repository_uses_postgres_backend(self):
        class Driver:
            @staticmethod
            def connect(*args, **kwargs): return FakeConnection()
        import types
        class CapturingRepo(FakeRepo):
            backend_seen = None
            def __init__(self, conn, backend):
                super().__init__(conn)
                self.backend = backend
                type(self).backend_seen = backend
        with patch.dict("sys.modules", {"psycopg": Driver, "psycopg.rows": types.SimpleNamespace(dict_row=dict)}), patch.object(harness, "Repository", CapturingRepo):
            harness.run_harness("postgresql://u:pw@h/db")
        self.assertEqual(CapturingRepo.backend_seen, "postgres")
