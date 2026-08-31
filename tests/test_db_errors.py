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


class FakeDiag:
    def __init__(self, *, table_name=None, constraint_name=None, column_name=None):
        self.table_name = table_name
        self.constraint_name = constraint_name
        self.column_name = column_name


class FakePgError(Exception):
    def __init__(self, message, *, sqlstate, table=None, constraint=None, column=None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.diag = FakeDiag(
            table_name=table,
            constraint_name=constraint,
            column_name=column,
        )


class DbErrorMapperTest(unittest.TestCase):
    def test_postgres_sqlstate_unique_violation_is_mapped(self):
        info = map_database_error(FakePgError("duplicate", sqlstate="23505"))

        self.assertEqual(info.kind, UNIQUE_VIOLATION)
        self.assertEqual(info.backend, "postgres")
        self.assertEqual(info.sqlstate, "23505")

    def test_named_route_constraint_restores_columns_missing_from_postgres_diag(self):
        info = map_database_error(
            FakePgError(
                "duplicate route",
                sqlstate="23505",
                table="routes",
                constraint="uq_routes_country_name",
            )
        )

        self.assertEqual(info.columns, ("country_id", "name"))

    def test_named_phone_constraint_restores_columns_missing_from_postgres_diag(self):
        info = map_database_error(
            FakePgError(
                "duplicate phone",
                sqlstate="23505",
                table="phone_numbers",
                constraint="uq_phone_numbers_normalized_number",
            )
        )

        self.assertEqual(info.columns, ("normalized_number",))

    def test_postgres_foreign_key_violation_is_mapped(self):
        info = map_database_error(FakePgError("foreign key", sqlstate="23503"))

        self.assertEqual(info.kind, FOREIGN_KEY_VIOLATION)

    def test_postgres_not_null_diagnostics_preserve_column(self):
        info = map_database_error(
            FakePgError("not null", sqlstate="23502", table="users", column="username")
        )

        self.assertEqual(info.kind, NOT_NULL_VIOLATION)
        self.assertEqual(info.table, "users")
        self.assertEqual(info.columns, ("username",))

    def test_postgres_check_violation_is_mapped(self):
        info = map_database_error(
            FakePgError(
                "check failed",
                sqlstate="23514",
                table="phone_numbers",
                constraint="phone_numbers_status_check",
            )
        )

        self.assertEqual(info.kind, CHECK_VIOLATION)

    def test_postgres_lock_timeout_is_mapped(self):
        info = map_database_error(FakePgError("lock unavailable", sqlstate="55P03"))

        self.assertEqual(info.kind, LOCK_TIMEOUT)

    def test_unknown_database_error_returns_unknown(self):
        info = map_database_error(RuntimeError("test"))

        self.assertEqual(info.kind, UNKNOWN_DATABASE_ERROR)
        self.assertEqual(info.raw_message, "test")

    def test_non_postgres_backend_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported database backend"):
            map_database_error(RuntimeError("test"), backend="sqlite")


class UserErrorMessageTest(unittest.TestCase):
    def test_duplicate_route_error_message_uses_postgres_constraint(self):
        message = user_error(
            FakePgError(
                "duplicate route",
                sqlstate="23505",
                table="routes",
                constraint="uq_routes_country_name",
            )
        )

        self.assertEqual(message, "Маршрут уже существует")

    def test_duplicate_phone_error_message_uses_postgres_constraint(self):
        message = user_error(
            FakePgError(
                "duplicate phone",
                sqlstate="23505",
                table="phone_numbers",
                constraint="uq_phone_numbers_normalized_number",
            )
        )

        self.assertEqual(message, "Номер уже существует")

    def test_dictionary_duplicate_message_uses_postgres_table(self):
        message = user_error(
            FakePgError(
                "duplicate server",
                sqlstate="23505",
                table="servers",
                constraint="uq_servers_name",
            )
        )

        self.assertEqual(message, "Кажется, такой сервер у нас уже есть. Давай назовём его иначе.")

    def test_unknown_db_error_fallback_is_preserved(self):
        self.assertEqual(user_error(RuntimeError("test fallback")), "test fallback")


if __name__ == "__main__":
    unittest.main()
