import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import archivarius_push as push


class Response:
    status = 204
    def __enter__(self): return self
    def __exit__(self, *args): return False


class ArchivariusPushTest(unittest.TestCase):
    def test_post_payload_uses_canonical_event_payload(self):
        requests = []
        def opener(request, timeout): requests.append(request); return Response()
        payload = {"schema_version": "archivarius-v1", "kind": "events", "data": {"items": []}}
        push.post_payload("https://relay.example/ingest", "relay-secret", payload, opener=opener)
        self.assertEqual(payload, json.loads(requests[0].data))
        self.assertEqual("Bearer relay-secret", requests[0].headers["Authorization"])

    def test_cursor_advances_only_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            connection = Mock()
            page = {"items": [{"id": 8}], "next_cursor": 8, "has_more": False}
            with patch.object(push, "connect_postgres", return_value=connection), \
                 patch.object(push.ArchivariusExporter, "events", return_value=page), \
                 patch.object(push, "post_payload", side_effect=RuntimeError("offline")):
                with self.assertRaises(RuntimeError): push.run_once("db", "https://relay.example", "token", state)
            self.assertFalse(state.exists())
            with patch.object(push, "connect_postgres", return_value=connection), \
                 patch.object(push.ArchivariusExporter, "events", return_value=page), \
                 patch.object(push, "post_payload"):
                self.assertEqual(8, push.run_once("db", "https://relay.example", "token", state))
            self.assertEqual(8, json.loads(state.read_text())["events_cursor"])

    def test_snapshot_mode_uses_canonical_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = Mock(); posts = []
            with patch.object(push, "connect_postgres", return_value=connection), \
                 patch.object(push.ArchivariusExporter, "complete_snapshot", return_value={"routes": []}), \
                 patch.object(push.ArchivariusExporter, "events", return_value={"items": [], "next_cursor": 0, "has_more": False}), \
                 patch.object(push, "post_payload", side_effect=lambda url, token, payload, **kw: posts.append(payload)):
                push.run_once("db", "https://relay.example", "token", Path(directory) / "state", snapshot=True)
            self.assertEqual(["snapshot", "events"], [item["kind"] for item in posts])
            self.assertEqual({"routes": []}, posts[0]["data"])


if __name__ == "__main__":
    unittest.main()
