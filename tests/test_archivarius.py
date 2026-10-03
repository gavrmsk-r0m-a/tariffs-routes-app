import io
import json
import os
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

os.environ.setdefault("DB_BACKEND", "postgres")
os.environ.setdefault("DATABASE_URL", "postgresql://unused/archivarius_tests")

from app.archivarius import EVENT_ENTITY_TYPES, PaginationError, canonicalize, parse_pagination, token_hash
from app import server
from scripts import create_archivarius_token


class Cursor:
    def __init__(self, rows=()): self.rows = list(rows)
    def __iter__(self): return iter(self.rows)
    def fetchone(self): return self.rows[0] if self.rows else None


class ApiConnection:
    def __init__(self, tokens=()):
        self.tokens = list(tokens); self.committed = False; self.queries = []
    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        if "FROM api_tokens" in sql: return Cursor(self.tokens)
        if "UPDATE api_tokens" in sql: return Cursor()
        raise AssertionError(sql)
    def commit(self): self.committed = True
    def rollback(self): pass


class Repo:
    def __init__(self, conn): self.conn = conn


def call_api(repo, path, method="GET", authorization=None, query=None):
    captured = {}
    def start(status, headers): captured.update(status=status, headers=dict(headers))
    environ = {"HTTP_AUTHORIZATION": authorization or ""}
    body = b"".join(server.archivarius_api(repo, environ, start, method, path, query or {}))
    return captured, json.loads(body)


class ArchivariusContractTest(unittest.TestCase):
    def test_canonical_json_types_and_unicode(self):
        value = canonicalize({"price": Decimal("1.2300"), "day": date(2026, 10, 3),
                              "at": datetime(2026, 10, 3, 12, tzinfo=timezone.utc), "label": "Россия"})
        self.assertEqual("1.2300", value["price"])
        self.assertEqual("2026-10-03", value["day"])
        self.assertEqual("2026-10-03T12:00:00Z", value["at"])
        self.assertEqual("Россия", value["label"])

    def test_pagination_validation_and_maximum(self):
        self.assertEqual((12, 1000), parse_pagination({"after_id": "12", "limit": "1000"}))
        for query in ({"after_id": "-1"}, {"limit": "0"}, {"limit": "1001"}, {"limit": "x"}):
            with self.assertRaises(PaginationError): parse_pagination(query)

    def test_event_whitelist_is_exact(self):
        self.assertEqual({"route", "route_phone_number", "phone_number", "tariff", "routing_event"}, set(EVENT_ENTITY_TYPES))
        self.assertTrue({"users", "api_tokens", "authentication"}.isdisjoint(EVENT_ENTITY_TYPES))

    def test_provisioning_stores_hash_never_plaintext(self):
        class ProvisionConnection:
            def __init__(self): self.calls = []
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "FROM users" in sql: return Cursor([{"id": 9}])
                if "FROM api_tokens" in sql: return Cursor()
                return Cursor()
            def commit(self): pass
            def rollback(self): pass
            def close(self): pass
        conn = ProvisionConnection()
        with patch.object(create_archivarius_token, "connect_postgres", return_value=conn), \
             patch.object(create_archivarius_token.secrets, "token_urlsafe", return_value="plain-once"):
            token = create_archivarius_token.provision("db", "archivarius", "admin", rotate=False)
        self.assertEqual("plain-once", token)
        insert = next(params for sql, params in conn.calls if "INSERT INTO api_tokens" in sql)
        self.assertNotIn("plain-once", insert)
        self.assertIn(token_hash("plain-once"), insert)

    def test_health_is_minimal_unauthed_json_no_store(self):
        headers, payload = call_api(Repo(ApiConnection()), server.ARCHIVARIUS_PREFIX + "/health")
        self.assertEqual("200 OK", headers["status"])
        self.assertEqual("no-store", headers["headers"]["Cache-Control"])
        self.assertEqual({"status": "ok", "service": "teleroute-archivarius-api", "api_version": "v1"}, payload)
        self.assertNotIn("database", json.dumps(payload).lower())

    def test_missing_bad_and_inactive_tokens_are_401_not_redirects(self):
        digest = token_hash("valid")
        for conn, authorization in ((ApiConnection(), None), (ApiConnection([{"id": 1, "name": "x", "token_hash": digest}]), "Bearer bad"), (ApiConnection(), "Bearer valid")):
            headers, payload = call_api(Repo(conn), server.ARCHIVARIUS_PREFIX + "/summary", authorization=authorization)
            self.assertEqual("401 Unauthorized", headers["status"])
            self.assertEqual("unauthorized", payload["error"]["code"])
            self.assertNotIn("Location", headers["headers"])

    def test_valid_token_accepted_and_usage_recorded(self):
        conn = ApiConnection([{"id": 7, "name": "archive", "token_hash": token_hash("secret")}])
        with patch.object(server.ArchivariusExporter, "summary", return_value={"api_version": "v1", "generated_at": "now"}):
            headers, _ = call_api(Repo(conn), server.ARCHIVARIUS_PREFIX + "/summary", authorization="Bearer secret")
        self.assertEqual("200 OK", headers["status"])
        self.assertTrue(conn.committed)
        self.assertTrue(any("UPDATE api_tokens" in sql for sql, _ in conn.queries))

    def test_methods_unknown_path_and_invalid_page_are_json(self):
        repo = Repo(ApiConnection())
        self.assertEqual("405 Method Not Allowed", call_api(repo, server.ARCHIVARIUS_PREFIX + "/routes", method="POST")[0]["status"])
        self.assertEqual("404 Not Found", call_api(repo, server.ARCHIVARIUS_PREFIX + "/nope")[0]["status"])
        conn = ApiConnection([{"id": 1, "name": "x", "token_hash": token_hash("ok")}])
        headers, payload = call_api(Repo(conn), server.ARCHIVARIUS_PREFIX + "/events", authorization="Bearer ok", query={"limit": "1001"})
        self.assertEqual("400 Bad Request", headers["status"])
        self.assertEqual("invalid_pagination", payload["error"]["code"])


if __name__ == "__main__":
    unittest.main()
