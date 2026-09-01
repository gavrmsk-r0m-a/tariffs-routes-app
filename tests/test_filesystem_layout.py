import gc
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path

from scripts.relocate_sqlite_db import RelocatePlan, relocate_sqlite_db


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
            # The legacy helper's sqlite3 context managers release their file
            # handles during collection; force it before Windows temp cleanup.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="unclosed database in .*",
                    category=ResourceWarning,
                )
                gc.collect()

            self.assertTrue(source.exists())
            self.assertTrue(target.exists())
            backups = list(backup_dir.glob("mvp.backup.*.sqlite3"))
            self.assertEqual(len(backups), 1)
            conn = sqlite3.connect(target)
            try:
                self.assertEqual(conn.execute("SELECT name FROM sample").fetchone()[0], "alpha")
            finally:
                conn.close()
            conn = sqlite3.connect(backups[0])
            try:
                self.assertEqual(conn.execute("SELECT name FROM sample").fetchone()[0], "alpha")
            finally:
                conn.close()

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
    @staticmethod
    def _patterns() -> set[str]:
        return {
            line.strip()
            for line in Path(".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

    def test_gitignore_contains_repository_hygiene_patterns(self):
        patterns = self._patterns()
        required_patterns = {
            ".env",
            ".env.*",
            "!.env.example",
            "!.env.postgres.local.example",
            "__pycache__/",
            "*.pyc",
            "*.pyo",
            "*.pyd",
            ".pytest_cache/",
            ".mypy_cache/",
            ".pyright/",
            ".ruff_cache/",
            ".coverage",
            ".coverage.*",
            "htmlcov/",
            "coverage.xml",
            ".tox/",
            ".nox/",
            ".venv/",
            "venv/",
            "env/",
            "build/",
            "dist/",
            "*.egg-info/",
            "*.sqlite",
            "*.sqlite3",
            "*.db",
            "*.sqlite3-wal",
            "*.sqlite3-shm",
            "*.db-wal",
            "*.db-shm",
            "*.sqlite-journal",
            "*.sqlite3-journal",
            "*.db-journal",
            "*.backup*.sqlite3",
            "*.bak",
            "*.backup",
            "*.backup.*",
            "*.orig",
            "*.dump",
            "*.sql.dump",
            "*.sql.gz",
            "*.dump.gz",
            "*.pgdump",
            "*.pgbackup",
            "pgdata/",
            ".postgres/",
            "*~",
            "*.swp",
            "*.swo",
            "preflight_report.json",
            "migration_report.json",
            "/data/",
            "/.data/",
            "/backups/",
            "/imports/",
            "/exports/",
            "/tmp/",
            "/temp/",
            "/reports/",
            "*.log",
            "*.log.*",
            "/logs/",
            ".idea/",
            ".vscode/",
            ".DS_Store",
            "Thumbs.db",
            "Desktop.ini",
            "docker-compose.override.yml",
            "compose.override.yml",
        }

        for pattern in required_patterns:
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, patterns)

    def test_gitignore_keeps_versioned_schema_and_fixtures_visible(self):
        patterns = self._patterns()

        self.assertNotIn("*.sql", patterns)
        self.assertNotIn("*.csv", patterns)
        self.assertNotIn("tests/", patterns)
        self.assertNotIn("/tests/", patterns)


class PostgreSQLProductionFilesystemContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = Path("docs/deployment/filesystem_layout.md").read_text(
            encoding="utf-8"
        )
        cls.layout = " ".join(text.split())

    def test_layout_documents_all_external_production_paths(self):
        required_paths = {
            "/opt/teleroute/releases/<release>/",
            "/opt/teleroute/current",
            "/etc/teleroute/teleroute.env",
            "/var/lib/teleroute/imports/",
            "/var/lib/teleroute/exports/",
            "/var/lib/teleroute/tmp/",
            "/var/backups/teleroute/postgres/",
            "/var/log/teleroute/",
            "/run/teleroute/",
        }

        for path in required_paths:
            with self.subTest(path=path):
                self.assertIn(path, self.layout)

    def test_layout_is_postgres_only_and_release_is_read_only(self):
        for statement in (
            "PostgreSQL-only production filesystem layout",
            "SQLite is not a production runtime",
            "read-only to the TeleRoute service user",
            "connects to PostgreSQL through `DATABASE_URL`",
            "managed by PostgreSQL itself or by the hosting provider",
            "must never be committed",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, self.layout)

        for obsolete_instruction in (
            "SQLITE_DB_PATH",
            "MVP_DB_PATH",
            "APP_DATA_DIR",
            "mvp.sqlite3",
            "relocate_sqlite_db.py",
        ):
            with self.subTest(obsolete_instruction=obsolete_instruction):
                self.assertNotIn(obsolete_instruction, self.layout)


class PostgreSQLOnlyRepositoryDocumentationTest(unittest.TestCase):
    def test_readme_describes_postgres_only_runtime_and_offline_legacy_tools(self):
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("PostgreSQL-only application runtime", readme)
        self.assertIn("explicit, offline migration", readme)
        self.assertIn("`DB_BACKEND=postgres`", readme)
        self.assertIn("`DATABASE_URL`", readme)
        for obsolete_instruction in (
            "POSTGRES_RUNTIME_ENABLED",
            "SQLITE_DB_PATH",
            "MVP_DB_PATH",
            "mvp.sqlite3",
        ):
            with self.subTest(obsolete_instruction=obsolete_instruction):
                self.assertNotIn(obsolete_instruction, readme)

    def test_local_environment_example_has_no_retired_runtime_guard(self):
        example = Path(".env.postgres.local.example").read_text(encoding="utf-8")

        self.assertIn("DB_BACKEND=postgres", example)
        self.assertIn("DATABASE_URL=", example)
        self.assertNotIn("POSTGRES_RUNTIME_ENABLED", example)

    def test_legacy_boundary_is_documented(self):
        legacy_readme = " ".join(
            Path("scripts/legacy/README.md")
            .read_text(encoding="utf-8")
            .split()
        )

        for statement in (
            "offline/manual migration",
            "must not import `scripts/legacy`",
            "outside the Git repository",
            "must not contain sensitive row samples",
            "not an application backend",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, legacy_readme)


if __name__ == "__main__":
    unittest.main()
