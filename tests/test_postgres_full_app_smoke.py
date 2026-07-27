import unittest
from unittest.mock import Mock, patch

from app.db import DbConfig
from scripts.postgres_full_app_smoke import wsgi_request


class FullAppSmokeHarnessTests(unittest.TestCase):
    def test_server_constructs_repository_with_postgres_backend(self):
        from app import server

        conn = Mock()
        repo = Mock()
        postgres_config = DbConfig("postgres", server.DB_PATH, "postgresql://ci.invalid/test")
        with (
            patch.object(server, "DB_CONFIG", postgres_config),
            patch.object(server, "connect_database", return_value=conn),
            patch.object(server, "Repository", return_value=repo) as repository,
            patch.object(server, "ensure_db_initialized") as initialize,
            patch.object(server, "ensure_seed") as seed,
        ):
            status, _, body = wsgi_request(server.app, "/login")

        self.assertEqual(status, "200 OK")
        self.assertIn(b"TeleRoute", body)
        repository.assert_called_once_with(conn, backend="postgres")
        initialize.assert_not_called()
        seed.assert_not_called()
        conn.close.assert_called_once_with()

    def test_wsgi_request_sends_form_and_cookie_to_real_callable_shape(self):
        observed = {}

        def application(environ, start_response):
            observed["method"] = environ["REQUEST_METHOD"]
            observed["path"] = environ["PATH_INFO"]
            observed["cookie"] = environ["HTTP_COOKIE"]
            observed["body"] = environ["wsgi.input"].read(int(environ["CONTENT_LENGTH"]))
            start_response("201 Created", [("Set-Cookie", "session=next")])
            return [b"ok"]

        status, headers, body = wsgi_request(
            application, "/login", method="POST", data={"username": "ci user"}, cookie="session=old"
        )

        self.assertEqual(status, "201 Created")
        self.assertEqual(headers, [("Set-Cookie", "session=next")])
        self.assertEqual(body, b"ok")
        self.assertEqual(observed, {
            "method": "POST", "path": "/login", "cookie": "session=old", "body": b"username=ci+user"
        })


if __name__ == "__main__":
    unittest.main()
