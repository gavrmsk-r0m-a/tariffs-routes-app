import unittest
from unittest.mock import ANY, Mock, patch

from app.db import DbConfig
from scripts.postgres_full_app_smoke import PAGES, SmokeFailure, wsgi_request


class FullAppSmokeHarnessTests(unittest.TestCase):
    def test_pages_cover_dashboard_and_admin_routing_views(self):
        self.assertTrue({
            "/", "/dashboard", "/admin/company-routing-settings", "/admin/server-priorities"
        }.issubset(PAGES))

    def test_dashboard_metrics_use_portable_boolean_predicates(self):
        from app import server

        statements = []
        with (
            patch.object(server, "dashboard_metric", side_effect=lambda _repo, sql, *_args: statements.append(sql) or ""),
            patch.object(server, "dashboard_events", return_value=""),
        ):
            server.dashboard_page(Mock())

        self.assertEqual(len(statements), 4)
        self.assertTrue(all("COUNT(*) AS value" in sql for sql in statements))
        self.assertTrue(all("IS TRUE" in sql for sql in statements))
        self.assertTrue(all("= 1" not in sql for sql in statements))

    def test_dashboard_metric_accepts_postgres_dict_row(self):
        from app import server

        conn = Mock()
        conn.execute.return_value.fetchone.return_value = {"value": 123}
        content = server.dashboard_metric(
            Mock(conn=conn), "SELECT COUNT(*) AS value FROM routes", "label", "hint", "icon", "tone", "points"
        )

        self.assertIn("<strong class='metric-value'>123</strong>", content)

    def test_dashboard_metric_keeps_legacy_positional_row_fallback(self):
        from app import server

        conn = Mock()
        conn.execute.return_value.fetchone.return_value = (7,)
        content = server.dashboard_metric(
            Mock(conn=conn), "SELECT COUNT(*) AS value FROM routes", "label", "hint", "icon", "tone", "points"
        )

        self.assertIn("<strong class='metric-value'>7</strong>", content)

    def test_server_priorities_page_uses_postgres_sql(self):
        from app import server

        conn = Mock()
        conn.execute.return_value = []
        repo = Mock(backend="postgres", conn=conn)
        server.server_priorities_page(repo, {"country_id": "1", "server_id": "2"})

        statements = [call.args[0] for call in conn.execute.call_args_list]
        self.assertTrue(statements)
        self.assertTrue(all("?" not in sql for sql in statements))
        self.assertTrue(all("= 1" not in sql for sql in statements))
        self.assertTrue(any("is_active IS TRUE" in sql for sql in statements))
        self.assertTrue(any("%s" in sql for sql in statements))

    def test_routing_event_snapshot_accepts_sqlite_json_text(self):
        from app.server import routing_event_snapshot

        self.assertEqual(
            routing_event_snapshot({"snapshot_json": '{"affected_servers": [{"server_name": "EU1"}]}'}),
            {"affected_servers": [{"server_name": "EU1"}]},
        )

    def test_routing_event_snapshot_accepts_psycopg_decoded_dict(self):
        from app.server import routing_event_snapshot

        snapshot = {"affected_servers": [{"server_name": "EU2"}]}
        self.assertIs(routing_event_snapshot({"snapshot_json": snapshot}), snapshot)

    def test_routing_event_snapshot_decodes_json_bytes(self):
        from app.server import routing_event_snapshot

        expected = {"overflow_provider_name": "Demo Provider"}
        encoded = b'{"overflow_provider_name": "Demo Provider"}'
        self.assertEqual(routing_event_snapshot({"snapshot_json": encoded}), expected)
        self.assertEqual(routing_event_snapshot({"snapshot_json": bytearray(encoded)}), expected)

    def test_routing_event_snapshot_returns_empty_for_missing_or_empty_value(self):
        from app.server import routing_event_snapshot

        for event in ({}, {"snapshot_json": None}, {"snapshot_json": ""}):
            with self.subTest(event=event):
                self.assertEqual(routing_event_snapshot(event), {})

    def test_routing_event_snapshot_returns_empty_for_invalid_or_non_object_json(self):
        from app.server import routing_event_snapshot

        for raw in ("{invalid", "[]", b"not-json", bytearray(b"[1]"), 42):
            with self.subTest(raw=raw):
                self.assertEqual(routing_event_snapshot({"snapshot_json": raw}), {})

    def test_wsgi_request_reports_path_and_original_exception_type(self):
        def application(environ, start_response):
            raise TypeError("bad page SQL")

        with self.assertRaises(SmokeFailure) as raised:
            wsgi_request(application, "/routes")

        self.assertEqual(raised.exception.path, "/routes")
        self.assertIsInstance(raised.exception.__cause__, TypeError)
        self.assertIn("TypeError", str(raised.exception))

    def test_page_option_helpers_emit_postgres_placeholders_and_boolean_predicates(self):
        from app import server

        conn = Mock()
        conn.execute.return_value = []
        repo = Mock(backend="postgres", conn=conn)

        server.active_options(repo, "countries")
        server.prefix_options(repo)
        server.phone_type_options(repo)
        server.project_options(repo)
        server.assignment_options(repo)

        statements = [call.args[0] for call in conn.execute.call_args_list]
        self.assertTrue(statements)
        self.assertTrue(all("?" not in sql for sql in statements))
        self.assertTrue(all("= 1" not in sql for sql in statements))
        self.assertTrue(all("IS TRUE" in sql for sql in statements))
        self.assertTrue(all("%s" in sql for sql in statements))

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

    def test_currency_rate_upsert_uses_postgres_placeholder_and_completes_flow(self):
        from app import server
        from app.repository import Repository

        conn = Mock()
        conn.execute.return_value.fetchone.return_value = {"code": "USD"}
        repo = Repository(conn, backend="postgres")
        old_rate = {"id": 40, "currency_id": 2, "rate_to_eur": "1.00"}
        new_rate = {"id": 41, "currency_id": 2, "rate_to_eur": "1.01"}
        recalculated = [{"tariff_id": 7}]
        server._REQUEST_CONTEXT.clear()
        server._REQUEST_CONTEXT["current_user_id"] = 1
        try:
            with (
                patch.object(repo, "latest_currency_rate", return_value=old_rate),
                patch.object(repo, "create_currency_rate", return_value=41) as create_rate,
                patch.object(repo, "get_currency_rate", return_value=new_rate),
                patch.object(repo, "recalculate_current_tariffs_for_currency_rate", return_value=recalculated) as recalculate,
                patch.object(repo, "log_currency_rate_change") as log_change,
            ):
                location = server.handle_post(
                    repo,
                    "/admin/currency-rates/upsert",
                    {"currency_id": "2", "rate_to_eur": "1.01"},
                )
        finally:
            server._REQUEST_CONTEXT.clear()

        self.assertEqual(location, "/admin/currency-rates")
        sql, params = conn.execute.call_args_list[0].args
        self.assertEqual(sql, "SELECT code FROM currencies WHERE id = %s")
        self.assertNotIn("?", sql)
        self.assertEqual(params, (2,))
        create_rate.assert_called_once_with(
            currency_id=2,
            rate_to_eur="1.01",
            rate_date=ANY,
            updated_by=1,
            source="manual",
            comment=None,
            commit=False,
        )
        recalculate.assert_called_once_with(41, 1)
        self.assertEqual(log_change.call_args.kwargs["recalculated_active_tariffs_count"], 1)
        conn.commit.assert_called_once_with()
        conn.rollback.assert_not_called()

    def test_currency_rate_upsert_succeeds_without_active_tariffs(self):
        from app import server
        from app.repository import Repository

        conn = Mock()
        conn.execute.return_value.fetchone.return_value = {"code": "USD"}
        repo = Repository(conn, backend="postgres")
        rate = {"id": 42, "currency_id": 2, "rate_to_eur": "1.01"}
        server._REQUEST_CONTEXT.clear()
        server._REQUEST_CONTEXT["current_user_id"] = 1
        try:
            with (
                patch.object(repo, "latest_currency_rate", return_value=None),
                patch.object(repo, "create_currency_rate", return_value=42),
                patch.object(repo, "get_currency_rate", return_value=rate),
                patch.object(repo, "recalculate_current_tariffs_for_currency_rate", return_value=[]),
                patch.object(repo, "log_currency_rate_change") as log_change,
            ):
                location = server.handle_post(
                    repo,
                    "/admin/currency-rates/create",
                    {"currency_id": "2", "rate_to_eur": "1.01"},
                )
        finally:
            server._REQUEST_CONTEXT.clear()

        self.assertEqual(location, "/admin/currency-rates")
        self.assertEqual(log_change.call_args.kwargs["recalculated_active_tariffs_count"], 0)
        conn.commit.assert_called_once_with()
        conn.rollback.assert_not_called()

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
