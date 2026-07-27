import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import postgres_deployment_rollback_check as rollback_check
from scripts import postgres_deployment_rollback_smoke as rollback_smoke


DATABASE_URL = "postgresql://rollback_user:database-secret@localhost:5432/teleroute"


class PostgresDeploymentRollbackGateTests(unittest.TestCase):
    def _artifacts(self, directory, *, overrides=None, include_backup=True):
        backup = Path(directory) / "backup.dump"
        if include_backup:
            backup.write_bytes(b"deployment rollback backup")
        manifest = {
            "status": "ok",
            "format": "pg_dump_custom",
            "sha256": hashlib.sha256(b"deployment rollback backup").hexdigest(),
            "size_bytes": len(b"deployment rollback backup"),
            "table_counts": {"users": 2},
            "backup_file": str(backup),
        }
        manifest.update(overrides or {})
        path = Path(directory) / "backup.manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return backup, path

    def test_valid_manifest_is_ok(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manifest = self._artifacts(directory)
            result = rollback_check.check_rollback_artifacts("current", "previous", manifest, strict=True)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["manifest_verified"])
        self.assertTrue(result["backup_sha256_verified"])
        self.assertTrue(result["table_counts_present"])

    def test_missing_manifest_fails(self):
        with self.assertRaises(FileNotFoundError):
            rollback_check.check_rollback_artifacts("current", "previous", Path("/missing/manifest"))

    def test_invalid_manifest_fields_fail(self):
        cases = ({"sha256": ""}, {"table_counts": {}}, {"size_bytes": 0})
        for overrides in cases:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as directory:
                _, manifest = self._artifacts(directory, overrides=overrides)
                with self.assertRaises(ValueError):
                    rollback_check.check_rollback_artifacts("current", "previous", manifest)

    def test_matching_release_shas_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manifest = self._artifacts(directory)
            with self.assertRaisesRegex(ValueError, "different"):
                rollback_check.check_rollback_artifacts("same", "same", manifest)

    def test_missing_backup_warns_non_strict_and_fails_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manifest = self._artifacts(directory, include_backup=False)
            result = rollback_check.check_rollback_artifacts("current", "previous", manifest)
            self.assertFalse(result["backup_sha256_verified"])
            self.assertTrue(result["warnings"])
            with self.assertRaisesRegex(ValueError, "unavailable"):
                rollback_check.check_rollback_artifacts("current", "previous", manifest, strict=True)

    def test_strict_sha256_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            backup, manifest = self._artifacts(directory)
            backup.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "sha256"):
                rollback_check.check_rollback_artifacts("current", "previous", manifest, strict=True)

    def test_database_names_are_unique(self):
        first = rollback_smoke.rollback_database_name()
        second = rollback_smoke.rollback_database_name()
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("teleroute_deployment_rollback_"))

    def test_smoke_uses_fresh_database_and_reports_drop(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(rollback_smoke, "create_backup"), \
                patch.object(rollback_smoke, "check_rollback_artifacts"), \
                patch.object(rollback_smoke, "create_database") as create, \
                patch.object(rollback_smoke, "restore_and_verify") as restore, \
                patch.object(rollback_smoke, "verify_rollback_database") as verify, \
                patch.object(rollback_smoke, "drop_database") as drop:
            result = rollback_smoke.run_smoke(DATABASE_URL, Path(directory), "current", "previous")
        name = result["rollback_database"]
        admin_url = rollback_smoke.database_url_with_name(DATABASE_URL, "postgres")
        rollback_url = rollback_smoke.database_url_with_name(DATABASE_URL, name)
        create.assert_called_once_with(admin_url, name)
        self.assertEqual(restore.call_args.args[1], rollback_url)
        verify.assert_called_once_with(rollback_url)
        drop.assert_called_once_with(admin_url, name)
        self.assertNotEqual(name, "teleroute")
        self.assertTrue(result["rollback_database_dropped"])

    def test_smoke_drops_database_when_restore_fails(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(rollback_smoke, "create_backup"), \
                patch.object(rollback_smoke, "check_rollback_artifacts"), \
                patch.object(rollback_smoke, "create_database"), \
                patch.object(rollback_smoke, "restore_and_verify", side_effect=RuntimeError("failed")), \
                patch.object(rollback_smoke, "drop_database") as drop:
            with self.assertRaises(RuntimeError):
                rollback_smoke.run_smoke(DATABASE_URL, Path(directory), "current", "previous")
        drop.assert_called_once()
        self.assertNotEqual(drop.call_args.args[1], "teleroute")

    def test_cli_errors_redact_database_password_and_auth_secret(self):
        auth_secret = "auth-secret-must-not-leak"
        stderr = io.StringIO()
        error = RuntimeError(f"failed {DATABASE_URL} database-secret {auth_secret}")
        with patch.dict("os.environ", {"MVP_AUTH_SECRET": auth_secret}), \
                patch.object(rollback_smoke, "run_smoke", side_effect=error), \
                contextlib.redirect_stderr(stderr):
            code = rollback_smoke.main([
                "--database-url", DATABASE_URL, "--workdir", "/tmp/unused",
                "--current-release-sha", "current", "--rollback-release-sha", "previous",
            ])
        self.assertEqual(code, 1)
        self.assertNotIn("database-secret", stderr.getvalue())
        self.assertNotIn(auth_secret, stderr.getvalue())

    def test_check_output_sanitizes_optional_url(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manifest = self._artifacts(directory)
            result = rollback_check.check_rollback_artifacts(
                "current", "previous", manifest, database_url=DATABASE_URL
            )
        rendered = json.dumps(result)
        self.assertNotIn("database-secret", rendered)
        self.assertIn(":***@", result["database_url_sanitized"])


if __name__ == "__main__":
    unittest.main()
