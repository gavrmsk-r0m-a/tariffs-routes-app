import unittest

from scripts.postgres_full_app_smoke import wsgi_request


class FullAppSmokeHarnessTests(unittest.TestCase):
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
