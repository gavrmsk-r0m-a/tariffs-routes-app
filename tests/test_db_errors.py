import sqlite3
import unittest

from app.db_errors import (
    CHECK_VIOLATION,
    FOREIGN_KEY_VIOLATION,
    LOCK_TIMEOUT,
    NOT_NULL_VIOLATION,
    UNKNOWN_DATABASE_ERROR,
    UNIQUE_VIOLATION,
    map_database_error,
)
from app.server import user_error


class DbErrorMapperTest(unittest.TestCase):
    def test_sqlite_unique_violation_is_mapped(self):
        info = map_database_error(sqlite3.IntegrityError("UNIQUE constraint failed: routes.country_id, routes.name"))

        self.assertEqual(info.kind, UNIQUE_VIOLATION)
        self.assertEqual(info.backend, "sqlite")
        self.assertEqual(info.table, "routes")
        self.assertEqual(info.columns, ("country_id", "name"))

    def test_sqlite_unique_violation_multiple_columns_parsed(self):
        info = map_database_error(
            sqlite3.IntegrityError(
                "UNIQUE constraint failed: tariffs.country_id, tariffs.provider_id, tariffs.route_id"
            )
        )

        self.assertEqual(info.kind, UNIQUE_VIOLATION)
        self.assertEqual(info.table, "tariffs")
        self.assertEqual(info.columns, ("country_id", "provider_id", "route_id"))

    def test_sqlite_foreign_key_violation_is_mapped(self):
        info = map_database_error(sqlite3.IntegrityError("FOREIGN KEY constraint failed"))

        self.assertEqual(info.kind, FOREIGN_KEY_VIOLATION)

    def test_sqlite_not_null_violation_is_mapped(self):
        info = map_database_error(sqlite3.IntegrityError("NOT NULL constraint failed: users.username"))

        self.assertEqual(info.kind, NOT_NULL_VIOLATION)
        self.assertEqual(info.table, "users")
        self.assertEqual(info.columns, ("username",))

    def test_sqlite_check_violation_is_mapped(self):
        info = map_database_error(sqlite3.IntegrityError("CHECK constraint failed: phone_numbers"))

        self.assertEqual(info.kind, CHECK_VIOLATION)
        self.assertEqual(info.constraint, "phone_numbers")

    def test_sqlite_database_locked_is_mapped(self):
        info = map_database_error(sqlite3.OperationalError("database is locked"))

        self.assertEqual(info.kind, LOCK_TIMEOUT)

    def test_postgres_sqlstate_unique_violation_is_mapped_without_psycopg(self):
        class FakePgError(Exception):
            sqlstate = "23505"

        info = map_database_error(FakePgError("duplicate"), backend="postgres")

        self.assertEqual(info.kind, UNIQUE_VIOLATION)
        self.assertEqual(info.backend, "postgres")
        self.assertEqual(info.sqlstate, "23505")

    def test_postgres_sqlstate_foreign_key_violation_is_mapped_without_psycopg(self):
        class FakePgError(Exception):
            sqlstate = "23503"

        info = map_database_error(FakePgError("foreign key"), backend="postgres")

        self.assertEqual(info.kind, FOREIGN_KEY_VIOLATION)
        self.assertEqual(info.backend, "postgres")
        self.assertEqual(info.sqlstate, "23503")

    def test_unknown_database_error_returns_unknown(self):
        info = map_database_error(RuntimeError("test"))

        self.assertEqual(info.kind, UNKNOWN_DATABASE_ERROR)
        self.assertEqual(info.raw_message, "test")


class UserErrorMessageTest(unittest.TestCase):
    def test_duplicate_route_error_message_is_preserved(self):
        message = user_error(sqlite3.IntegrityError("UNIQUE constraint failed: routes.country_id, routes.name"))

        self.assertEqual(message, "Маршрут уже существует")
        self.assertNotIn("UNIQUE constraint failed", message)

    def test_duplicate_phone_error_message_is_preserved(self):
        message = user_error(sqlite3.IntegrityError("UNIQUE constraint failed: phone_numbers.normalized_number"))

        self.assertEqual(message, "Номер уже существует")
        self.assertNotIn("UNIQUE constraint failed", message)

    def test_duplicate_tariff_error_message_is_preserved(self):
        message = user_error(
            sqlite3.IntegrityError(
                "UNIQUE constraint failed: tariffs.country_id, tariffs.provider_id, tariffs.route_id"
            )
        )

        self.assertEqual(message, "Активный тариф с такой связкой ГЕО + провайдер + префикс уже существует")
        self.assertNotIn("UNIQUE constraint failed", message)

    def test_unknown_db_error_fallback_is_preserved(self):
        message = user_error(RuntimeError("test fallback"))

        self.assertEqual(message, "test fallback")


if __name__ == "__main__":
    unittest.main()
