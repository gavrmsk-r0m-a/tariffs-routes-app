import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.audit_postgres_runtime_compat import main, scan_path


class PostgresRuntimeAuditHelperTests(unittest.TestCase):
    def scan_text(self, text: str):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.py").write_text(text, encoding="utf-8")
            return scan_path(root)

    def test_audit_detects_sqlite3_import(self):
        findings = self.scan_text("import sqlite3\n")
        self.assertTrue(any(f.pattern == "sqlite3" for f in findings))

    def test_audit_detects_lastrowid(self):
        findings = self.scan_text("new_id = cursor.lastrowid\n")
        self.assertTrue(any(f.pattern == "lastrowid" for f in findings))

    def test_audit_detects_pragma(self):
        findings = self.scan_text('conn.execute("PRAGMA foreign_keys=ON")\n')
        self.assertTrue(any(f.pattern == "PRAGMA" for f in findings))

    def test_audit_detects_question_placeholders(self):
        findings = self.scan_text('conn.execute("SELECT * FROM users WHERE id = ?", (1,))\n')
        self.assertTrue(any(f.category == "placeholders" for f in findings))

    def test_audit_json_output_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.py").write_text("import sqlite3\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(["--root", str(root), "--format", "json"])

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["file"], "sample.py")

    def test_audit_ignores_data_backups_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for dirname in ("data", "backups", "logs"):
                directory = root / dirname
                directory.mkdir()
                (directory / "ignored.py").write_text("import sqlite3\n", encoding="utf-8")
            self.assertEqual(scan_path(root), [])

    def test_audit_does_not_modify_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = root / "sample.py"
            sample.write_text("import sqlite3\n", encoding="utf-8")
            before = sample.read_text(encoding="utf-8")
            scan_path(root)
            after = sample.read_text(encoding="utf-8")
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
