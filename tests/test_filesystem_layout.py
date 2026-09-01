import unittest
from pathlib import Path


class GitignoreRepositoryHygieneTest(unittest.TestCase):
    def test_gitignore_contains_required_repository_hygiene_patterns(self):
        patterns = set(Path(".gitignore").read_text().splitlines())
        required_patterns = {
            ".env",
            "*.sqlite",
            "*.sqlite3",
            "*.db",
            "*.sqlite-journal",
            "*.sqlite3-journal",
            "*.db-journal",
            "*.sqlite3-wal",
            "*.sqlite3-shm",
            "*.db-wal",
            "*.db-shm",
            "*.bak",
            "*.backup",
            "*.backup.*",
            "*.orig",
            "*~",
            "*.swp",
            "*.swo",
            "*.dump",
            "*.sql.dump",
            "*.sql.gz",
            "*.dump.gz",
            "*.pgdump",
            "*.pgbackup",
            "pgdata/",
            ".postgres/",
            "*.log",
            "*.log.*",
            "/imports/",
            "/exports/",
            "/tmp/",
            "/temp/",
            "/reports/",
            ".venv/",
            "venv/",
            "env/",
            "build/",
            "dist/",
            "*.egg-info/",
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

    def test_gitignore_keeps_schema_and_reviewed_csv_files_trackable(self):
        patterns = set(Path(".gitignore").read_text().splitlines())

        self.assertNotIn("*.sql", patterns)
        self.assertNotIn("*.csv", patterns)


class ProductionFilesystemLayoutTest(unittest.TestCase):
    def test_layout_documents_postgres_only_external_runtime_paths(self):
        layout = Path("docs/deployment/filesystem_layout.md").read_text()
        required_fragments = {
            "/opt/teleroute/releases/",
            "current -> releases/<release>",
            "/etc/teleroute/",
            "teleroute.env",
            "/var/lib/teleroute/",
            "imports/",
            "exports/",
            "tmp/",
            "/var/backups/teleroute/postgres/",
            "/var/log/teleroute/",
            "/run/teleroute/",
            "DATABASE_URL",
            "read-only",
            "there is no SQLite production runtime",
        }

        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, layout)

    def test_layout_does_not_document_legacy_runtime_configuration(self):
        layout = Path("docs/deployment/filesystem_layout.md").read_text()

        self.assertNotIn("SQLITE_DB_PATH", layout)
        self.assertNotIn("MVP_DB_PATH", layout)
        self.assertNotIn("APP_DATA_DIR", layout)
        self.assertNotIn("POSTGRES_RUNTIME_ENABLED", layout)


if __name__ == "__main__":
    unittest.main()
