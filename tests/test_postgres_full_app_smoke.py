import unittest
from unittest.mock import ANY, Mock, patch

from app.db import DbConfig
from scripts.postgres_full_app_smoke import PAGES, SmokeFailure, _isolated_smoke_currency, wsgi_request


class FullAppSmokeHarnessTests(unittest.TestCase):
    def test_prefix_update_uses_database_owner_and_postgres_placeholders(self):
        from app import server

        conn = Mock()
        conn.execute.return_value.fetchone.return_value = {"id": 81, "provider_id": 7, "prefix": "0808"}
        repo = Mock(backend="postgres", conn=conn)
        repo.dictionary_rename_preview.return_value = {}
        with patch.object(server, "ensure_dictionary_value_unique") as unique:
            location = server.handle_post(
                repo, "/admin/dictionaries/prefixes/81/update",
                {"prefix": "0809", "name": "normal edit", "is_active": "1"},
            )

        self.assertEqual(location, "/admin/dictionaries?section=prefixes")
        unique.assert_called_once_with(repo, "prefixes", "0809", entity_id=81, provider_id=7)
        update_sql, update_params = next(
            call.args for call in conn.execute.call_args_list if "UPDATE provider_prefixes" in call.args[0]
        )
        self.assertNotIn("provider_id", update_sql)
        self.assertNotIn("?", update_sql)
        self.assertEqual(update_params, ("0809", "normal edit", True, 81))

    def test_prefix_update_rejects_submitted_postgres_owner_substitution(self):
        from app import server

        conn = Mock()
        conn.execute.return_value.fetchone.return_value = {"id": 81, "provider_id": 7, "prefix": "0808"}
        repo = Mock(backend="postgres", conn=conn)
        with self.assertRaisesRegex(server.BusinessRuleError, "провайдера у существующего префикса менять нельзя"):
            server.handle_post(
                repo, "/admin/dictionaries/prefixes/81/update",
                {"provider_id": "8", "prefix": "0809", "name": "attack", "is_active": "1"},
            )

        self.assertFalse(any("UPDATE provider_prefixes" in call.args[0] for call in conn.execute.call_args_list))

    def test_dictionary_server_update_uses_only_postgres_placeholders(self):
        from app import server

        conn = Mock()
        conn.execute.return_value.fetchone.return_value = {"id": 71, "name": "OLD"}
        repo = Mock(backend="postgres", conn=conn)
        repo.dictionary_rename_preview.return_value = {}
        with patch.object(server, "ensure_dictionary_value_unique"):
            location = server.handle_post(
                repo,
                "/admin/dictionaries/servers/71/update",
                {"name": "NEW", "comment": "probe", "is_active": "1"},
            )

        self.assertEqual(location, "/admin/dictionaries?section=servers")
        statements = [call.args[0] for call in conn.execute.call_args_list]
        self.assertEqual(statements[0], "SELECT * FROM servers WHERE id = %s")
        self.assertTrue(any("UPDATE servers SET name = %s" in sql for sql in statements))
        self.assertTrue(all("?" not in sql for sql in statements))
        update_params = next(call.args[1] for call in conn.execute.call_args_list if "UPDATE servers" in call.args[0])
        self.assertIs(update_params[2], True)

    def test_direct_dictionary_creates_use_only_postgres_placeholders(self):
        from app import server

        cases = (
            ("phone-types", {"name": "Mobile CI", "comment": "probe"}, "phone_number_types"),
            ("projects", {"name": "Project CI", "comment": "probe"}, "projects"),
            ("phone-assignments", {"name": "Assignment CI", "code": "assign_ci", "comment": "probe"}, "phone_assignment_types"),
        )
        for kind, data, table in cases:
            with self.subTest(kind=kind):
                conn = Mock()
                repo = Mock(backend="postgres", conn=conn)
                with patch.object(server, "ensure_dictionary_value_unique"):
                    server.handle_post(repo, f"/admin/dictionaries/{kind}/create", data)
                sql, params = conn.execute.call_args.args
                self.assertIn(f"INSERT INTO {table}", sql)
                self.assertNotIn("?", sql)
                self.assertIn("%s", sql)
                self.assertIn(True, params)

    def test_currency_rate_smoke_uses_isolated_currency_and_always_cleans_it_up(self):
        create_conn = Mock()
        create_conn.execute.return_value.fetchone.return_value = {"id": 901}
        cleanup_conn = Mock()

        with patch(
            "scripts.postgres_full_app_smoke.connect_postgres",
            side_effect=[create_conn, cleanup_conn],
        ):
            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                with _isolated_smoke_currency("postgresql://ci.invalid/demo") as currency_id:
                    self.assertEqual(currency_id, 901)
                    raise RuntimeError("probe failed")

        create_sql, create_params = create_conn.execute.call_args.args
        self.assertIn("INSERT INTO currencies", create_sql)
        self.assertTrue(create_params[0].startswith("CI_SMOKE_"))
        self.assertNotIn("SELECT id FROM currencies ORDER BY id", create_sql)
        create_conn.commit.assert_called_once_with()
        create_conn.close.assert_called_once_with()

        cleanup_statements = [call.args for call in cleanup_conn.execute.call_args_list]
        self.assertEqual(len(cleanup_statements), 3)
        self.assertIn("DELETE FROM change_log", cleanup_statements[0][0])
        self.assertEqual(cleanup_statements[0][1], (901,))
        self.assertEqual(cleanup_statements[1], ("DELETE FROM currency_rates WHERE currency_id = %s", (901,)))
        self.assertEqual(cleanup_statements[2], ("DELETE FROM currencies WHERE id = %s", (901,)))
        cleanup_conn.commit.assert_called_once_with()
        cleanup_conn.close.assert_called_once_with()

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
