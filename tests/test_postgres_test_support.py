import unittest
from tests.postgres_test_support import TemporaryPostgresDatabase, _database_name, _with_database, require_admin_url

class PostgresTestSupportSafetyTest(unittest.TestCase):
    def test_unique_database_name_is_explicitly_test_only(self):
        fixture = TemporaryPostgresDatabase("postgresql://ci:ci@localhost/postgres")
        self.assertRegex(fixture.name, r"^teleroute_test_[0-9a-f]{32}$")
        self.assertEqual(_database_name(fixture.database_url), fixture.name)

    def test_database_url_replacement_preserves_admin_url(self):
        self.assertEqual(_with_database("postgresql://ci:ci@localhost/postgres?sslmode=disable", "teleroute_test_abc"), "postgresql://ci:ci@localhost/teleroute_test_abc?sslmode=disable")

    def test_dangerous_admin_targets_are_rejected(self):
        for name in ("teleroute", "production", "prod", "teleroute_test_deadbeef"):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                require_admin_url({"POSTGRES_TEST_ADMIN_URL": f"postgresql://ci:ci@localhost/{name}"})
