import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import DbConfig, connect_database
from scripts.postgres_final_enablement_check import check_final_enablement, main
from scripts.postgres_runtime_enablement_smoke import run_smoke


APPROVAL = """APPROVED_FOR_POSTGRES_RUNTIME_ENABLEMENT
No production credentials are stored in this repository.
Actual production deployment requires operator/hosting configuration; it is not performed by this PR.
"""
URL = "postgresql://ci-user:super-private-password@localhost/test"
SECRET = "ci-only-runtime-enable-secret-at-least-32-chars"


class FakeResult:
    def __init__(self, value): self.value = value
    def fetchone(self): return {"value": self.value}


class FakeConnection:
    def __init__(self): self.queries = []; self.closed = False
    def execute(self, query):
        self.queries.append(query)
        if query == "SELECT 1": return FakeResult(1)
        if "table_name='users'" in query: return FakeResult(True)
        if query == "SELECT COUNT(*) FROM users": return FakeResult(2)
        return FakeResult(30)
    def close(self): self.closed = True


class PostgresFinalEnablementGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backup = self.root / "backup.dump"
        self.backup.write_bytes(b"verified-backup")
        self.manifest = self.root / "backup.manifest.json"
        self.approval = self.root / "approval.md"
        self.approval.write_text(APPROVAL, encoding="utf-8")
        self.write_manifest()
        self.env = {
            "DB_BACKEND": "postgres", "POSTGRES_RUNTIME_ENABLED": "1",
            "DATABASE_URL": URL, "MVP_PRODUCTION_SECURITY": "1", "MVP_AUTH_SECRET": SECRET,
        }

    def tearDown(self): self.temp.cleanup()

    def write_manifest(self, **changes):
        value = {"status": "ok", "format": "pg_dump_custom",
                 "sha256": hashlib.sha256(self.backup.read_bytes()).hexdigest(),
                 "size_bytes": self.backup.stat().st_size, "table_counts": {"users": 2},
                 "backup_file": str(self.backup)}
        value.update(changes)
        self.manifest.write_text(json.dumps(value), encoding="utf-8")

    def check(self, *, env=None, strict=True, approval=None):
        return check_final_enablement(URL, "current", "rollback", self.manifest,
                                      approval or self.approval, strict=strict,
                                      environ=self.env if env is None else env)

    def test_valid_artifacts_and_strong_secret_pass(self):
        result = self.check()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["final_enablement_ready"])
        self.assertIn(":***@", result["database_url_sanitized"])

    def test_required_environment_failures(self):
        cases = (("DB_BACKEND", None), ("DB_BACKEND", "sqlite"),
                 ("POSTGRES_RUNTIME_ENABLED", None), ("POSTGRES_RUNTIME_ENABLED", "true"),
                 ("DATABASE_URL", None), ("MVP_PRODUCTION_SECURITY", None),
                 ("MVP_AUTH_SECRET", "weak"))
        for name, value in cases:
            with self.subTest(name=name, value=value):
                env = dict(self.env)
                if value is None: env.pop(name, None)
                else: env[name] = value
                supplied_url = None if name == "DATABASE_URL" else URL
                with self.assertRaises(ValueError):
                    check_final_enablement(supplied_url, "current", "rollback", self.manifest,
                                           self.approval, environ=env)

    def test_approval_validation(self):
        with self.assertRaises(FileNotFoundError): self.check(approval=self.root / "missing")
        self.approval.write_text("No production credentials are stored in this repository.", encoding="utf-8")
        with self.assertRaises(ValueError): self.check()
        self.approval.write_text(APPROVAL + "postgresql://user:password@production/db", encoding="utf-8")
        with self.assertRaises(ValueError): self.check()

    def test_manifest_required_fields(self):
        for changes in ({"sha256": ""}, {"table_counts": {}}, {"size_bytes": 0}):
            with self.subTest(changes=changes):
                self.write_manifest(**changes)
                with self.assertRaises(ValueError): self.check()

    def test_missing_backup_warns_non_strict_and_fails_strict(self):
        self.backup.unlink()
        result = self.check(strict=False)
        self.assertTrue(result["warnings"])
        self.assertFalse(result["backup_sha256_verified"])
        with self.assertRaises(ValueError): self.check(strict=True)

    def test_strict_detects_digest_mismatch(self):
        self.write_manifest(sha256="0" * 64)
        with self.assertRaises(ValueError): self.check()

    def test_cli_redacts_database_password_and_auth_secret(self):
        output, error = io.StringIO(), io.StringIO()
        env = dict(self.env); env["MVP_AUTH_SECRET"] = "weak-private-secret"
        args = ["--database-url", URL, "--current-release-sha", "current",
                "--rollback-release-sha", "rollback", "--backup-manifest", str(self.manifest),
                "--approval", str(self.approval), "--format", "json"]
        with patch.dict("os.environ", env, clear=True), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(main(args), 1)
        rendered = output.getvalue() + error.getvalue()
        self.assertNotIn("super-private-password", rendered)
        self.assertNotIn("weak-private-secret", rendered)

    def test_runtime_smoke_uses_guarded_app_db_path(self):
        connection = FakeConnection()
        with patch("app.db.load_db_config", return_value=DbConfig("postgres", Path(":"), URL)) as load, \
             patch("app.db.connect_database", return_value=connection) as connect:
            result = run_smoke(URL, SECRET)
        environ = load.call_args.args[0]
        self.assertEqual(environ["DB_BACKEND"], "postgres")
        self.assertEqual(environ["POSTGRES_RUNTIME_ENABLED"], "1")
        connect.assert_called_once_with(load.return_value, environ)
        self.assertIn("SELECT 1", connection.queries)
        self.assertIn("SELECT COUNT(*) FROM users", connection.queries)
        self.assertNotIn("super-private-password", json.dumps(result))
        self.assertNotIn(SECRET, json.dumps(result))

    def test_connect_database_guard_and_missing_url_remain_enforced(self):
        with self.assertRaises(NotImplementedError):
            connect_database(DbConfig("postgres", Path(":"), URL), {})
        with self.assertRaises(ValueError):
            connect_database(DbConfig("postgres", Path(":"), None), {"POSTGRES_RUNTIME_ENABLED": "1"})


if __name__ == "__main__": unittest.main()
