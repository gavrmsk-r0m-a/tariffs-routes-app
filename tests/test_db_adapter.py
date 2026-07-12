from __future__ import annotations

import sqlite3
import unittest

from app.db_adapter import (
    build_in_clause,
    extract_inserted_id,
    from_db_bool,
    normalize_backend_name,
    placeholder,
    placeholders,
    prepare_insert_returning_id,
    row_to_dict,
    rows_to_dicts,
    to_db_bool,
    validate_identifier,
)


class DbAdapterTest(unittest.TestCase):
    def test_normalize_backend_name(self):
        self.assertEqual(normalize_backend_name("sqlite"), "sqlite")
        self.assertEqual(normalize_backend_name(" SQLite "), "sqlite")
        self.assertEqual(normalize_backend_name("postgres"), "postgres")
        self.assertEqual(normalize_backend_name("PostgreSQL"), "postgres")
        with self.assertRaises(ValueError):
            normalize_backend_name("mysql")

    def test_placeholder_styles(self):
        self.assertEqual(placeholder("sqlite"), "?")
        self.assertEqual(placeholder("postgres"), "%s")

    def test_placeholders_count(self):
        self.assertEqual(placeholders(3, "sqlite"), "?, ?, ?")
        self.assertEqual(placeholders(3, "postgres"), "%s, %s, %s")
        with self.assertRaises(ValueError):
            placeholders(0, "sqlite")

    def test_build_in_clause_sqlite(self):
        clause, params = build_in_clause("id", [1, 2, 3], "sqlite")
        self.assertEqual(clause, "id IN (?, ?, ?)")
        self.assertEqual(params, [1, 2, 3])

    def test_build_in_clause_postgres(self):
        clause, params = build_in_clause("id", [1, 2], "postgres")
        self.assertEqual(clause, "id IN (%s, %s)")
        self.assertEqual(params, [1, 2])

    def test_build_in_clause_empty_values(self):
        clause, params = build_in_clause("id", [], "sqlite")
        self.assertEqual(clause, "1 = 0")
        self.assertEqual(params, [])

    def test_validate_identifier_accepts_safe_names(self):
        for name in ("id", "route_id", "routes.id", "user_permissions.section_key"):
            self.assertEqual(validate_identifier(name), name)

    def test_validate_identifier_rejects_unsafe_names(self):
        for name in ("id; DROP TABLE users", "id = ?", "routes.id OR 1=1", "", "bad name", '"id"'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_identifier(name)

    def test_prepare_insert_returning_id_sqlite_noop(self):
        sql = "INSERT INTO users (username) VALUES (?)"
        self.assertEqual(prepare_insert_returning_id(sql, "sqlite"), sql)

    def test_prepare_insert_returning_id_postgres_adds_returning(self):
        sql = "INSERT INTO users (username) VALUES (%s)"
        self.assertEqual(prepare_insert_returning_id(sql, "postgres"), f"{sql} RETURNING id")

    def test_prepare_insert_returning_id_does_not_duplicate_returning(self):
        sql = "INSERT INTO users (username) VALUES (%s) RETURNING id"
        self.assertEqual(prepare_insert_returning_id(sql, "postgres"), sql)

    def test_extract_inserted_id_sqlite_lastrowid(self):
        class Cursor:
            lastrowid = 123

        self.assertEqual(extract_inserted_id(Cursor(), "sqlite"), 123)

    def test_extract_inserted_id_postgres_fetchone_tuple(self):
        class Cursor:
            def fetchone(self):
                return (123,)

        self.assertEqual(extract_inserted_id(Cursor(), "postgres"), 123)

    def test_extract_inserted_id_postgres_fetchone_dict(self):
        class Cursor:
            def fetchone(self):
                return {"id": 123}

        self.assertEqual(extract_inserted_id(Cursor(), "postgres"), 123)

    def test_row_to_dict_none_dict_sqlite_row_like(self):
        self.assertIsNone(row_to_dict(None))
        source = {"id": 1, "name": "Alice"}
        self.assertEqual(row_to_dict(source), source)
        self.assertIsNot(row_to_dict(source), source)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE users (id INTEGER, name TEXT)")
        conn.execute("INSERT INTO users VALUES (?, ?)", (1, "Alice"))
        row = conn.execute("SELECT id, name FROM users").fetchone()
        self.assertEqual(row_to_dict(row), {"id": 1, "name": "Alice"})

        class RowLike:
            def __init__(self):
                self._data = {"id": 2, "name": "Bob"}

            def keys(self):
                return self._data.keys()

            def __getitem__(self, key):
                return self._data[key]

        self.assertEqual(row_to_dict(RowLike()), {"id": 2, "name": "Bob"})
        self.assertEqual(rows_to_dicts([RowLike()]), [{"id": 2, "name": "Bob"}])
        with self.assertRaises(TypeError):
            row_to_dict((1, "Alice"))

    def test_to_db_bool_sqlite(self):
        self.assertEqual(to_db_bool(True, "sqlite"), 1)
        self.assertEqual(to_db_bool(False, "sqlite"), 0)
        self.assertIsNone(to_db_bool(None, "sqlite"))

    def test_to_db_bool_postgres(self):
        self.assertIs(to_db_bool(True, "postgres"), True)
        self.assertIs(to_db_bool(False, "postgres"), False)
        self.assertIsNone(to_db_bool(None, "postgres"))

    def test_from_db_bool_valid(self):
        for value in (True, 1, "1"):
            self.assertIs(from_db_bool(value), True)
        for value in (False, 0, "0"):
            self.assertIs(from_db_bool(value), False)
        self.assertIsNone(from_db_bool(None))

    def test_from_db_bool_rejects_invalid(self):
        for value in ("yes", 2, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    from_db_bool(value)


if __name__ == "__main__":
    unittest.main()
