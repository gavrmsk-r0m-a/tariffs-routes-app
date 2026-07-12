import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import DEFAULT_DB_PATH, load_db_config
from scripts.relocate_sqlite_db import RelocatePlan, relocate_sqlite_db


class FilesystemDbConfigTest(unittest.TestCase):
    def test_db_config_prefers_sqlite_db_path(self):
        config = load_db_config({
            "SQLITE_DB_PATH": "/tmp/teleroute/sqlite.sqlite3",
            "MVP_DB_PATH": "/tmp/teleroute/legacy.sqlite3",
            "APP_DATA_DIR": "/tmp/teleroute/data",
        })

        self.assertEqual(config.sqlite_path, Path("/tmp/teleroute/sqlite.sqlite3"))

    def test_db_config_supports_legacy_mvp_db_path(self):
        config = load_db_config({
            "MVP_DB_PATH": "/tmp/teleroute/legacy.sqlite3",
            "APP_DATA_DIR": "/tmp/teleroute/data",
        })

        self.assertEqual(config.sqlite_path, Path("/tmp/teleroute/legacy.sqlite3"))

    def test_db_config_supports_app_data_dir(self):
        config = load_db_config({"APP_DATA_DIR": "/tmp/teleroute/data"})

        self.assertEqual(config.sqlite_path, Path("/tmp/teleroute/data") / "mvp.sqlite3")

    def test_db_config_fallback_unchanged(self):
        config = load_db_config({})

        self.assertEqual(config.sqlite_path, DEFAULT_DB_PATH)


class RelocateSqliteDbTest(unittest.TestCase):
    def _create_source_db(self, directory: Path) -> Path:
        source = directory / "mvp.sqlite3"
        conn = sqlite3.connect(source)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            conn.execute("INSERT INTO sample (name) VALUES ('alpha')")
            conn.commit()
        finally:
            conn.close()
        return source

    def test_relocate_sqlite_db_dry_run_no_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = self._create_source_db(root)
            target = root / "data" / "mvp.sqlite3"
            backup_dir = root / "backups"

            relocate_sqlite_db(RelocatePlan(source, target, backup_dir, dry_run=True, overwrite=False))

            self.assertFalse(target.exists())
            self.assertFalse(backup_dir.exists())

    def test_relocate_sqlite_db_apply_creates_backup_and_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = self._create_source_db(root)
            target = root / "data" / "mvp.sqlite3"
            backup_dir = root / "backups"

            relocate_sqlite_db(RelocatePlan(source, target, backup_dir, dry_run=False, overwrite=False))

            self.assertTrue(source.exists())
            self.assertTrue(target.exists())
            backups = list(backup_dir.glob("mvp.backup.*.sqlite3"))
            self.assertEqual(len(backups), 1)
            with sqlite3.connect(target) as conn:
                self.assertEqual(conn.execute("SELECT name FROM sample").fetchone()[0], "alpha")
            with sqlite3.connect(backups[0]) as conn:
                self.assertEqual(conn.execute("SELECT name FROM sample").fetchone()[0], "alpha")

    def test_relocate_does_not_overwrite_existing_target_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = self._create_source_db(root)
            target = root / "data" / "mvp.sqlite3"
            target.parent.mkdir()
            target.write_text("existing target")
            backup_dir = root / "backups"

            with self.assertRaises(FileExistsError):
                relocate_sqlite_db(RelocatePlan(source, target, backup_dir, dry_run=False, overwrite=False))

            self.assertEqual(target.read_text(), "existing target")
            self.assertFalse(backup_dir.exists())


class GitignoreFilesystemLayoutTest(unittest.TestCase):
    def test_gitignore_contains_sqlite_wal_backup_patterns(self):
        gitignore = Path(".gitignore").read_text()
        required_patterns = {
            ".env",
            "*.sqlite",
            "*.sqlite3",
            "*.db",
            "*.sqlite3-wal",
            "*.sqlite3-shm",
            "*.db-wal",
            "*.db-shm",
            "*.backup*.sqlite3",
            "*.dump",
            "*.sql.dump",
            "preflight_report.json",
            "migration_report.json",
            "/data/",
            "/.data/",
            "/backups/",
            "/logs/",
        }

        for pattern in required_patterns:
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, gitignore)
        self.assertNotIn("*.sql\n", gitignore)


if __name__ == "__main__":
    unittest.main()
