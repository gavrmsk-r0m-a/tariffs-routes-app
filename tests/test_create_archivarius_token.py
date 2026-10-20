import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from app.archivarius import token_hash
from scripts import create_archivarius_token as token_script


class _FakeConnection:
    def __init__(self):
        self.insert_params = None
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT id FROM users"):
            return _FakeCursor({"id": 7})
        if normalized.startswith("SELECT id FROM api_tokens"):
            return _FakeCursor(None)
        if normalized.startswith("INSERT INTO api_tokens"):
            self.insert_params = params
            return _FakeCursor(None)
        raise AssertionError(f"Unexpected SQL: {normalized}")

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class _FakeCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class ArchivariusTokenProvisioningTests(unittest.TestCase):
    def test_supplied_token_is_hashed_before_database_insert(self):
        plaintext = "A" * 48
        connection = _FakeConnection()

        with mock.patch.object(token_script, "connect_postgres", return_value=connection):
            returned = token_script.provision(
                "postgresql://example",
                "archivarius-prod",
                "admin",
                rotate=False,
                comment="test",
                token=plaintext,
            )

        self.assertEqual(plaintext, returned)
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)
        self.assertIsNotNone(connection.insert_params)
        self.assertEqual(token_hash(plaintext), connection.insert_params[1])
        self.assertNotIn(plaintext, connection.insert_params)

    def test_stdin_token_is_not_printed(self):
        plaintext = "B" * 48
        output = io.StringIO()
        errors = io.StringIO()
        captured = {}

        def fake_provision(database_url, name, created_by_user, **kwargs):
            captured.update(
                database_url=database_url,
                name=name,
                created_by_user=created_by_user,
                token=kwargs.get("token"),
                rotate=kwargs.get("rotate"),
            )
            return kwargs.get("token")

        with (
            mock.patch.object(token_script, "provision", side_effect=fake_provision),
            mock.patch.object(token_script.sys, "stdin", io.StringIO(plaintext)),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            result = token_script.main([
                "--database-url", "postgresql://example",
                "--name", "archivarius-prod",
                "--created-by-user", "admin",
                "--rotate",
                "--token-stdin",
            ])

        self.assertEqual(0, result)
        self.assertEqual(plaintext, captured["token"])
        self.assertTrue(captured["rotate"])
        self.assertNotIn(plaintext, output.getvalue())
        self.assertNotIn(plaintext, errors.getvalue())
        self.assertIn("plaintext token was not printed", output.getvalue())

    def test_database_url_can_come_from_environment(self):
        plaintext = "C" * 48
        captured = {}

        def fake_provision(database_url, name, created_by_user, **kwargs):
            captured["database_url"] = database_url
            return kwargs.get("token")

        with (
            mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://from-env"}, clear=False),
            mock.patch.object(token_script, "provision", side_effect=fake_provision),
            mock.patch.object(token_script.sys, "stdin", io.StringIO(plaintext)),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            result = token_script.main([
                "--name", "archivarius-prod",
                "--created-by-user", "admin",
                "--token-stdin",
            ])

        self.assertEqual(0, result)
        self.assertEqual("postgresql://from-env", captured["database_url"])

    def test_short_supplied_token_is_rejected_without_printing_it(self):
        plaintext = "too-short"
        output = io.StringIO()
        errors = io.StringIO()

        with (
            mock.patch.object(token_script, "provision") as provision,
            mock.patch.object(token_script.sys, "stdin", io.StringIO(plaintext)),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            result = token_script.main([
                "--database-url", "postgresql://example",
                "--name", "archivarius-prod",
                "--created-by-user", "admin",
                "--token-stdin",
            ])

        self.assertEqual(1, result)
        provision.assert_not_called()
        self.assertNotIn(plaintext, output.getvalue())
        self.assertNotIn(plaintext, errors.getvalue())
        self.assertIn("at least", errors.getvalue())

    def test_generated_token_is_printed_for_manual_creation(self):
        generated = "D" * 48
        output = io.StringIO()

        with (
            mock.patch.object(token_script, "provision", return_value=generated),
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            result = token_script.main([
                "--database-url", "postgresql://example",
                "--name", "archivarius-prod",
                "--created-by-user", "admin",
            ])

        self.assertEqual(0, result)
        self.assertIn(generated, output.getvalue())


if __name__ == "__main__":
    unittest.main()
