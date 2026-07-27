import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import postgres_backup, postgres_backup_restore_smoke, postgres_restore_verify


DATABASE_URL = "postgresql://backup_user:super-secret@localhost:5432/teleroute"
TARGET_URL = "postgresql://restore_user:target-secret@localhost:5432/restore"


class PostgresBackupRestoreTests(unittest.TestCase):
    def test_url_sanitization_masks_password(self):
        rendered = postgres_backup.sanitize_database_url(DATABASE_URL)
        self.assertEqual(rendered, "postgresql://backup_user:***@localhost:5432/teleroute")
        self.assertNotIn("super-secret", rendered)
        error = postgres_backup.sanitize_text(f"failed: {DATABASE_URL}; super-secret", DATABASE_URL)
        self.assertNotIn("super-secret", error)

    def test_backup_command_and_manifest(self):
        payload = b"custom backup bytes"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "backup.dump"
            manifest_path = Path(directory) / "manifest.json"

            def fake_run(command, **kwargs):
                self.assertTrue(kwargs["capture_output"])
                self.assertTrue(kwargs["check"])
                if "--version" in command:
                    return subprocess.CompletedProcess(command, 0, "pg_dump 16.4\n", "")
                output.write_bytes(payload)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(postgres_backup, "collect_table_counts", return_value={"users": 2}), \
                    patch.object(postgres_backup.subprocess, "run", side_effect=fake_run) as run:
                summary = postgres_backup.create_backup(
                    DATABASE_URL, output, manifest_path, pg_dump_bin="custom-pg-dump"
                )

            command = run.call_args_list[1].args[0]
            self.assertEqual(command[0], "custom-pg-dump")
            for argument in ("--format=custom", "--no-owner", "--no-acl", "--file"):
                self.assertIn(argument, command)
            self.assertEqual(command[command.index("--file") + 1], str(output))
            self.assertEqual(summary["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(summary["size_bytes"], len(payload))
            self.assertEqual(summary["table_counts"], {"users": 2})
            rendered = manifest_path.read_text(encoding="utf-8")
            self.assertEqual(rendered, json.dumps(summary, indent=2, sort_keys=True) + "\n")
            self.assertNotIn("super-secret", rendered)

    def test_backup_does_not_overwrite_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "backup.dump"
            manifest = Path(directory) / "manifest.json"
            output.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                postgres_backup.create_backup(DATABASE_URL, output, manifest)
            with patch.object(postgres_backup, "collect_table_counts", return_value={}), \
                    patch.object(postgres_backup, "_run") as run:
                def fake_run(command, _database_url):
                    if "--version" in command:
                        return subprocess.CompletedProcess(command, 0, "pg_dump 16", "")
                    output.write_bytes(b"new")
                    return subprocess.CompletedProcess(command, 0, "", "")

                run.side_effect = fake_run
                result = postgres_backup.create_backup(DATABASE_URL, output, manifest, overwrite=True)
            self.assertEqual(result["status"], "ok")

    def _manifest(self, directory: str, backup: bytes = b"backup", counts=None):
        backup_path = Path(directory) / "backup.dump"
        backup_path.write_bytes(backup)
        manifest_path = Path(directory) / "manifest.json"
        manifest_path.write_text(json.dumps({
            "sha256": hashlib.sha256(backup).hexdigest(),
            "table_counts": counts or {"users": 2},
        }), encoding="utf-8")
        return backup_path, manifest_path

    def test_restore_command_and_matching_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            backup, manifest = self._manifest(directory)
            with patch.object(postgres_restore_verify, "target_is_empty", return_value=True), \
                    patch.object(postgres_restore_verify, "collect_table_counts", return_value={"users": 2}), \
                    patch.object(postgres_restore_verify.subprocess, "run") as run:
                result = postgres_restore_verify.restore_and_verify(
                    backup, TARGET_URL, manifest, pg_restore_bin="custom-pg-restore"
                )
            command = run.call_args.args[0]
            self.assertEqual(command[0], "custom-pg-restore")
            for argument in ("--no-owner", "--no-acl", "--exit-on-error", "--dbname"):
                self.assertIn(argument, command)
            self.assertEqual(command[command.index("--dbname") + 1], TARGET_URL)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["verified_table_counts"], {"users": 2})
            self.assertNotIn("target-secret", json.dumps(result))

    def test_restore_count_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            backup, manifest = self._manifest(directory)
            with patch.object(postgres_restore_verify, "target_is_empty", return_value=True), \
                    patch.object(postgres_restore_verify, "collect_table_counts", return_value={"users": 1}), \
                    patch.object(postgres_restore_verify.subprocess, "run"):
                with self.assertRaisesRegex(ValueError, "counts do not match"):
                    postgres_restore_verify.restore_and_verify(backup, TARGET_URL, manifest)

    def test_restore_errors_are_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            backup, manifest = self._manifest(directory)
            failure = subprocess.CalledProcessError(1, [], stderr=f"connection failed: {TARGET_URL}")
            with patch.object(postgres_restore_verify, "target_is_empty", return_value=True), \
                    patch.object(postgres_restore_verify.subprocess, "run", side_effect=failure):
                with self.assertRaises(RuntimeError) as raised:
                    postgres_restore_verify.restore_and_verify(backup, TARGET_URL, manifest)
            self.assertNotIn("target-secret", str(raised.exception))

    def test_smoke_always_drops_created_database_after_restore_failure(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(postgres_backup_restore_smoke, "create_backup"), \
                patch.object(postgres_backup_restore_smoke, "create_database"), \
                patch.object(postgres_backup_restore_smoke, "restore_and_verify", side_effect=RuntimeError("fail")), \
                patch.object(postgres_backup_restore_smoke, "drop_database") as drop:
            with self.assertRaises(RuntimeError):
                postgres_backup_restore_smoke.run_smoke(DATABASE_URL, Path(directory))
            drop.assert_called_once()
            self.assertTrue(drop.call_args.args[1].startswith("teleroute_restore_verify_"))

    def test_restore_database_names_are_unique(self):
        first = postgres_backup_restore_smoke.restore_database_name()
        second = postgres_backup_restore_smoke.restore_database_name()
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("teleroute_restore_verify_"))

    def test_smoke_cli_does_not_render_password(self):
        stderr = io.StringIO()
        with patch.object(postgres_backup_restore_smoke, "run_smoke", side_effect=RuntimeError(DATABASE_URL)), \
                contextlib.redirect_stderr(stderr):
            code = postgres_backup_restore_smoke.main([
                "--database-url", DATABASE_URL, "--workdir", "/tmp/unused"
            ])
        self.assertEqual(code, 1)
        self.assertNotIn("super-secret", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
