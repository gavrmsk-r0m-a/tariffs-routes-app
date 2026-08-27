import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PostgresRuntimeAuditTests(unittest.TestCase):
    SCRIPT = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "audit_postgres_runtime_compat.py"
    )

    def run_audit(self, source: str = ""):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            app_dir.mkdir()

            if source:
                (app_dir / "sample.py").write_text(
                    source,
                    encoding="utf-8",
                )

            result = subprocess.run(
                [
                    sys.executable,
                    str(self.SCRIPT),
                    "--root",
                    str(root),
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            payload = json.loads(result.stdout)
            return result.returncode, payload

    def assert_detected(self, source: str, expected_token: str):
        code, payload = self.run_audit(source)

        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["runtime_backend"], "postgres")

        tokens = {
            finding["token"]
            for finding in payload["sqlite_runtime_findings"]
        }
        self.assertIn(expected_token, tokens)

    def test_clean_runtime_is_postgresql_only(self):
        code, payload = self.run_audit("print('postgres only')\n")

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "postgresql_only")
        self.assertEqual(payload["runtime_backend"], "postgres")
        self.assertEqual(payload["sqlite_runtime_findings"], [])

    def test_detects_sqlite3(self):
        self.assert_detected(
            "import sqlite3\n",
            "sqlite3",
        )

    def test_detects_sqlite_word(self):
        self.assert_detected(
            "backend = 'sqlite'\n",
            "sqlite",
        )

    def test_detects_sqlite_capitalized(self):
        self.assert_detected(
            "backend = 'SQLite'\n",
            "SQLite",
        )

    def test_detects_pragma(self):
        self.assert_detected(
            'sql = "PRAGMA table_info(test)"\n',
            "PRAGMA",
        )

    def test_detects_sqlite_master(self):
        self.assert_detected(
            'sql = "SELECT * FROM sqlite_master"\n',
            "sqlite_master",
        )

    def test_detects_lastrowid(self):
        self.assert_detected(
            "value = cursor.lastrowid\n",
            "lastrowid",
        )

    def test_detects_legacy_database_path(self):
        self.assert_detected(
            "path = 'mvp.sqlite3'\n",
            "mvp.sqlite3",
        )

    def test_detects_legacy_schema_reference(self):
        self.assert_detected(
            "path = 'app/schema.sql'\n",
            "schema.sql",
        )


if __name__ == "__main__":
    unittest.main()