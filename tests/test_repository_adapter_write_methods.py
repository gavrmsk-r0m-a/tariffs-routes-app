import sqlite3
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from app.db import init_db
from app.repository import BusinessRuleError, Repository


class RepositoryAdapterWriteMethodsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        self.repo = Repository(self.conn)

    def tearDown(self):
        self.conn.close()

    def _route_phone_fixture(self):
        user_id = self.conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
        country_id = self.repo.create_country("Stage 66C GEO", "66C")
        provider_id = self.repo.create_provider("Stage 66C provider")
        route_id = self.repo.create_route(
            country_id=country_id, provider_id=provider_id, name="Stage 66C route",
            cli_source_type="other", cli_source_label="Stage 66C", created_by=user_id,
        )
        phone_ids = [self.repo.create_phone_number(
            country_id=country_id, provider_id=provider_id, number=f"7999666010{index}",
            assignment_type="other", status="used", created_by=user_id,
        ) for index in (1, 2)]
        return user_id, route_id, phone_ids

    def test_route_phone_methods_support_sqlite_caller_owned_rollback(self):
        user_id, route_id, phone_ids = self._route_phone_fixture()
        self.conn.commit()
        first = self.repo.add_phone_to_route(
            route_id=route_id, phone_number_id=phone_ids[0], usage_type="cli",
            added_by=user_id, commit=False,
        )
        second = self.repo.add_phone_to_route_by_number(
            route_id=route_id, number="79996660102", usage_type="other",
            added_by=user_id, commit=False,
        )
        self.assertEqual(1, self.repo.remove_phone_links_from_route(
            route_id=route_id, link_ids=[first.route_phone_number_id],
            removed_by=user_id, reason="rollback", commit=False,
        ))
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM route_phone_numbers WHERE id = ?", (second.route_phone_number_id,)).fetchone())
        self.conn.rollback()
        self.assertIsNone(self.conn.execute("SELECT 1 FROM route_phone_numbers WHERE route_id = ?", (route_id,)).fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM route_phone_number_history WHERE route_id = ?", (route_id,)).fetchone())

    def test_route_phone_add_uses_postgres_sql_returning_and_commit_contract(self):
        class Cursor:
            def __init__(self, row=None): self.row = row
            def fetchone(self): return self.row
        class Connection:
            def __init__(self): self.calls=[]; self.commits=0; self.rollbacks=0
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "FROM phone_numbers" in sql: return Cursor({"id": 2, "number": "79996660101", "is_active": True, "status": "used"})
                if "INSERT INTO route_phone_numbers" in sql: return Cursor({"id": 66})
                return Cursor()
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        connection = Connection(); repo = Repository(connection, backend="postgres")
        result = repo.add_phone_to_route(route_id=1, phone_number_id=2, usage_type="cli", added_by=3, commit=False)
        self.assertEqual(66, result.route_phone_number_id)
        self.assertEqual(0, connection.commits)
        self.assertEqual(0, connection.rollbacks)
        self.assertFalse(any("?" in sql for sql, _ in connection.calls))
        insert = next((sql, params) for sql, params in connection.calls if "INSERT INTO route_phone_numbers" in sql)
        self.assertIn("RETURNING id", insert[0])
        self.assertIn("VALUES (%s, %s, %s, %s, %s, %s)", insert[0])
        self.assertIs(insert[1][3], True)

    def test_route_phone_by_number_disables_nested_commit_and_uses_postgres_placeholders(self):
        class Cursor:
            def __init__(self, row=None): self.row = row
            def fetchone(self): return self.row
        class Connection:
            def __init__(self): self.calls=[]; self.commits=0; self.rollbacks=0
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "FROM phone_numbers" in sql: return Cursor({"id": 2})
                return Cursor()
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        connection = Connection(); repo = Repository(connection, backend="postgres")
        expected = object()
        with patch.object(repo, "add_phone_to_route", return_value=expected) as add:
            self.assertIs(expected, repo.add_phone_to_route_by_number(
                route_id=1, number="79996660101", usage_type="cli", added_by=3, commit=False,
            ))
        self.assertFalse(add.call_args.kwargs["commit"])
        self.assertFalse(any("?" in sql for sql, _ in connection.calls))
        self.assertIn("number = %s OR normalized_number = %s", connection.calls[0][0])
        self.assertEqual((0, 0), (connection.commits, connection.rollbacks))

    def test_route_phone_remove_uses_postgres_placeholders_and_caller_transaction(self):
        class Cursor:
            def __init__(self, row=None): self.row = row
            def fetchone(self): return self.row
        class Connection:
            def __init__(self): self.calls=[]; self.commits=0; self.rollbacks=0
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "SELECT id, phone_number_id" in sql: return Cursor({"id": 4, "phone_number_id": 2})
                return Cursor()
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        connection = Connection(); repo = Repository(connection, backend="postgres")
        self.assertEqual(1, repo.remove_phone_links_from_route(route_id=1, link_ids=[4], removed_by=3, commit=False))
        self.assertFalse(any("?" in sql for sql, _ in connection.calls))
        update = next((sql, params) for sql, params in connection.calls if "UPDATE route_phone_numbers" in sql)
        self.assertIn("is_active = %s", update[0])
        self.assertIs(update[1][0], False)
        self.assertEqual((0, 0), (connection.commits, connection.rollbacks))

    def test_route_phone_methods_do_not_rollback_caller_owned_failures(self):
        class FailingConnection:
            def __init__(self): self.rollbacks=0
            def execute(self, sql, params=()): raise RuntimeError("route-phone failed")
            def commit(self): raise AssertionError("unexpected commit")
            def rollback(self): self.rollbacks += 1
        for method, kwargs in (
            ("add_phone_to_route", dict(route_id=1, phone_number_id=2, usage_type="cli", added_by=3)),
            ("add_phone_to_route_by_number", dict(route_id=1, number="79996660101", usage_type="cli", added_by=3)),
            ("remove_phone_links_from_route", dict(route_id=1, link_ids=[4], removed_by=3)),
        ):
            connection = FailingConnection()
            with self.assertRaisesRegex(RuntimeError, "route-phone failed"):
                getattr(Repository(connection, backend="postgres"), method)(commit=False, **kwargs)
            self.assertEqual(0, connection.rollbacks)

    def test_selected_create_method_returns_id_on_sqlite(self):
        country_id = self.repo.create_country("Бельгия", "BE")
        provider_id = self.repo.create_provider("AdapterWriteTel")
        server_id = self.repo.create_server("adapter-write-server")

        self.assertIsInstance(country_id, int)
        self.assertIsInstance(provider_id, int)
        self.assertIsInstance(server_id, int)
        self.assertGreater(country_id, 0)
        self.assertGreater(provider_id, 0)
        self.assertGreater(server_id, 0)

    def test_selected_create_method_persists_row(self):
        provider_id = self.repo.create_provider("PersistTel", "voip", comment="created by adapter test")

        row = self.conn.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "PersistTel")
        self.assertEqual(row["normalized_name"], "persisttel")
        self.assertEqual(row["provider_type"], "voip")
        self.assertEqual(row["is_active"], 1)
        self.assertEqual(row["comment"], "created by adapter test")

    def test_insert_returning_helper_used_without_changing_sqlite_behavior(self):
        with patch("app.repository.prepare_insert_returning_id", wraps=__import__("app.db_adapter", fromlist=["prepare_insert_returning_id"]).prepare_insert_returning_id) as prepare, \
             patch("app.repository.extract_inserted_id", wraps=__import__("app.db_adapter", fromlist=["extract_inserted_id"]).extract_inserted_id) as extract:
            country_id = self.repo.create_country("Австрия", "AT")

        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(prepare.call_args.args[1], "sqlite")
        self.assertEqual(extract.call_args.args[1], "sqlite")
        self.assertEqual(self.repo.get_country(country_id)["code"], "AT")

    def test_boolean_write_uses_sqlite_integer(self):
        active_id = self.repo.create_change_reason("Active reason", is_active=True)
        inactive_id = self.repo.create_change_reason("Inactive reason", is_active=False)

        active = self.conn.execute("SELECT is_active FROM change_reasons WHERE id = ?", (active_id,)).fetchone()
        inactive = self.conn.execute("SELECT is_active FROM change_reasons WHERE id = ?", (inactive_id,)).fetchone()

        self.assertEqual(active["is_active"], 1)
        self.assertEqual(inactive["is_active"], 0)

    def test_existing_dictionary_write_still_works(self):
        country_id = self.repo.create_country("Мексика", "MX")
        provider_id = self.repo.create_provider("DictionaryTel")
        server_id = self.repo.create_server("dictionary-server", "plain write path")
        reason_id = self.repo.create_change_reason("Dictionary reason", comment="plain write path")

        self.assertEqual(self.repo.get_country(country_id)["name"], "Мексика")
        self.assertEqual(self.conn.execute("SELECT name FROM providers WHERE id = ?", (provider_id,)).fetchone()["name"], "DictionaryTel")
        self.assertEqual(self.conn.execute("SELECT comment FROM servers WHERE id = ?", (server_id,)).fetchone()["comment"], "plain write path")
        self.assertEqual(self.conn.execute("SELECT description FROM change_reasons WHERE id = ?", (reason_id,)).fetchone()["description"], "plain write path")

    def test_change_reason_caller_transaction_rolls_back_reason_and_audit_row(self):
        reason_id = self.repo.create_change_reason("Rollback reason", comment="rollback", commit=False)
        self.assertIsNotNone(self.conn.execute("SELECT id FROM change_reasons WHERE id = ?", (reason_id,)).fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT id FROM change_log WHERE entity_type = ? AND entity_id = ?", ("change_reason", reason_id)).fetchone())
        self.conn.rollback()
        self.assertIsNone(self.conn.execute("SELECT id FROM change_reasons WHERE id = ?", (reason_id,)).fetchone())
        self.assertIsNone(self.conn.execute("SELECT id FROM change_log WHERE entity_type = ? AND entity_id = ?", ("change_reason", reason_id)).fetchone())

    def test_create_routing_event_none_supports_caller_owned_sqlite_transaction(self):
        user_id = self.conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
        country_id = self.repo.create_country("Stage 64 GEO", "S64")
        provider_id = self.repo.create_provider("Stage 64 provider")
        route_id = self.repo.create_route(
            country_id=country_id, provider_id=provider_id, name="Stage 64 route",
            cli_source_type="other", cli_source_label="Stage 64", created_by=user_id,
        )
        event_id = self.repo.create_routing_event(
            event_at="2026-07-22 15:00:00", apply_scope="none", reason="Другое",
            country_id=country_id, provider_id=provider_id, affected_route_id=route_id,
            comment="__stage64_routing_event_none_comment__", created_by=user_id, commit=False,
        )
        self.assertIsNotNone(self.conn.execute("SELECT id FROM routing_events WHERE id = ?", (event_id,)).fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT id FROM change_log WHERE entity_type = ? AND entity_id = ?", ("routing_event", event_id)).fetchone())
        self.conn.rollback()
        self.assertIsNone(self.conn.execute("SELECT id FROM routing_events WHERE id = ?", (event_id,)).fetchone())
        self.assertIsNone(self.conn.execute("SELECT id FROM change_log WHERE entity_type = ? AND entity_id = ?", ("routing_event", event_id)).fetchone())

    def test_create_routing_event_rolls_back_only_when_it_owns_transaction(self):
        class FailingConnection:
            def __init__(self): self.rollbacks = 0
            def execute(self, sql, params=()): raise RuntimeError("routing event failed")
            def commit(self): raise AssertionError("unexpected commit")
            def rollback(self): self.rollbacks += 1
        kwargs = dict(event_at="2026-07-22 15:00:00", apply_scope="none", reason="Другое", provider_id=1, affected_route_id=1, comment="marker", created_by=1)
        owned = FailingConnection()
        with self.assertRaisesRegex(RuntimeError, "routing event failed"):
            Repository(owned, backend="postgres").create_routing_event(**kwargs)
        self.assertEqual(owned.rollbacks, 1)
        caller_owned = FailingConnection()
        with self.assertRaisesRegex(RuntimeError, "routing event failed"):
            Repository(caller_owned, backend="postgres").create_routing_event(commit=False, **kwargs)
        self.assertEqual(caller_owned.rollbacks, 0)

    def _campaign_fixture(self):
        user_id = self.conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
        country_id = self.repo.create_country("Stage 65B GEO", "65B")
        provider_id = self.repo.create_provider("Stage 65B provider")
        server_id = self.repo.create_server("stage-65b-server")
        routes = [self.repo.create_route(country_id=country_id, provider_id=provider_id,
                  name=f"Stage 65B route {index}", cli_source_type="other",
                  cli_source_label="Stage 65B", created_by=user_id) for index in (1, 2)]
        company_id = self.repo.create_calling_company(
            server_id=server_id, country_id=country_id, company_name="Stage 65B company",
            company_id_external="stage65b", has_autorotation=False, line_count=0,
            dial_set_count=0, retry_interval_seconds=0, comment="stage65b", created_by=user_id,
        )
        return user_id, country_id, server_id, routes, company_id

    def test_create_routing_event_campaign_create_is_caller_owned_and_rolls_back(self):
        user_id, _, _, routes, company_id = self._campaign_fixture()
        self.conn.commit()
        event_id = self.repo.create_routing_event(
            event_at="2026-07-22 16:00:00", apply_scope="campaign_setting",
            reason="Задача руководства", calling_company_id=company_id,
            company_change_type="set_campaign_route", new_company_route_id=routes[0],
            comment="__stage65b_campaign_create__", created_by=user_id, commit=False,
        )
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM routing_events WHERE id = ?", (event_id,)).fetchone())
        self.assertEqual(routes[0], self.conn.execute("SELECT route_id FROM company_routing_settings WHERE calling_company_id = ? AND is_active = 1", (company_id,)).fetchone()["route_id"])
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM change_log WHERE entity_type = 'routing_event' AND entity_id = ? AND change_type = 'routing_event.created'", (event_id,)).fetchone())
        self.conn.rollback()
        self.assertIsNone(self.conn.execute("SELECT 1 FROM routing_events WHERE id = ?", (event_id,)).fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM company_routing_settings WHERE calling_company_id = ?", (company_id,)).fetchone())

    def test_create_routing_event_campaign_remove_deactivates_and_rolls_back(self):
        user_id, country_id, server_id, routes, company_id = self._campaign_fixture()
        setting_id = self.repo.create_company_routing_setting(
            calling_company_id=company_id, country_id=country_id, server_id=server_id,
            route_id=routes[0], routing_mode="campaign_route", has_autorotation=False,
            comment="before", created_by=user_id,
        )
        self.conn.commit()
        event_id = self.repo.create_routing_event(
            event_at="2026-07-22 16:00:00", apply_scope="campaign_setting",
            reason="Задача руководства", calling_company_id=company_id,
            company_change_type="remove_campaign_route", comment="__stage65b_campaign_remove__",
            created_by=user_id, commit=False,
        )
        self.assertEqual(0, self.conn.execute("SELECT is_active FROM company_routing_settings WHERE id = ?", (setting_id,)).fetchone()["is_active"])
        self.conn.rollback()
        self.assertEqual(1, self.conn.execute("SELECT is_active FROM company_routing_settings WHERE id = ?", (setting_id,)).fetchone()["is_active"])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM routing_events WHERE id = ?", (event_id,)).fetchone())

    def test_campaign_helpers_disable_nested_commits(self):
        repo = Repository(object(), backend="postgres")
        values = dict(calling_company_id=1, country_id=2, server_id=3,
                      new_company_route_id=4, new_company_routing_mode="campaign_route",
                      new_company_has_autorotation=0, comment="marker", event_at="2026-07-22 16:00:00")
        with patch.object(repo, "_active_company_routing_setting", side_effect=[None, {"id": 8}, {"id": 9}]), \
             patch.object(repo, "create_company_routing_setting") as create, \
             patch.object(repo, "update_company_routing_setting") as update, \
             patch.object(repo, "deactivate_company_routing_setting") as deactivate:
            repo._upsert_company_routing_setting_from_event(values, updated_by=7)
            repo._upsert_company_routing_setting_from_event(values, updated_by=7)
            repo._deactivate_company_routing_setting_from_event(values, updated_by=7)
        self.assertFalse(create.call_args.kwargs["commit"])
        self.assertFalse(update.call_args.kwargs["commit"])
        self.assertFalse(deactivate.call_args.kwargs["commit"])

    def test_create_routing_event_campaign_uses_postgres_sql_and_top_level_commit(self):
        class Cursor:
            def __init__(self, row=None): self.row = row
            def fetchone(self): return self.row
        class Connection:
            def __init__(self): self.calls=[]; self.commits=0; self.rollbacks=0
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "FROM calling_companies" in sql:
                    return Cursor({"country_id": 2, "server_id": 3})
                if "FROM routes WHERE id" in sql:
                    return Cursor({"country_id": 2, "provider_id": 5})
                if "INSERT INTO routing_events" in sql:
                    return Cursor({"id": 650})
                return Cursor()
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        connection = Connection(); repo = Repository(connection, backend="postgres")
        kwargs = dict(event_at="2026-07-22 16:00:00", apply_scope="campaign_setting",
                      reason="Задача руководства", calling_company_id=1,
                      company_change_type="set_campaign_route", new_company_route_id=4,
                      comment="stage65b", created_by=7)
        with patch.object(repo, "_company_old_state", return_value={"routing_mode": "server_priority", "route_id": None, "has_autorotation": False}), \
             patch.object(repo, "_routing_event_snapshot", return_value={"comment": "stage65b"}), \
             patch.object(repo, "_routing_event_summary", return_value="stage65b"), \
             patch.object(repo, "_change_log") as change_log, \
             patch.object(repo, "_apply_campaign_setting_event") as apply:
            self.assertEqual(650, repo.create_routing_event(**kwargs))
            self.assertEqual(1, connection.commits)
            self.assertEqual(0, connection.rollbacks)
            self.assertFalse(any("?" in sql for sql, _ in connection.calls))
            apply.assert_called_once()
            change_log.assert_called_once()
            self.assertEqual("routing_event.created", change_log.call_args.args[2])
            repo.create_routing_event(commit=False, **kwargs)
            self.assertEqual(1, connection.commits)

    def test_change_reason_uses_postgres_placeholders_and_commit_contract(self):
        class Cursor:
            def fetchone(self): return {"id": 901}
        class RecordingConnection:
            def __init__(self): self.calls=[]; self.commits=0; self.rollbacks=0
            def execute(self, sql, params=()): self.calls.append((sql, params)); return Cursor()
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        connection = RecordingConnection(); repo = Repository(connection, backend="postgres")
        self.assertEqual(repo.create_change_reason(" Причина ", comment="комментарий"), 901)
        self.assertIn("VALUES (%s, %s, %s) RETURNING id", connection.calls[0][0])
        self.assertEqual(connection.calls[0][1], ("Причина", "комментарий", True))
        self.assertIn("VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", connection.calls[1][0])
        self.assertNotIn("?", connection.calls[1][0])
        self.assertEqual(connection.calls[1][1][-1], "ui")
        self.assertIn('"name": "Причина"', connection.calls[1][1][5])
        self.assertEqual(connection.commits, 1)
        repo.create_change_reason("No commit", commit=False)
        self.assertEqual(connection.commits, 1)

    def test_change_reason_rolls_back_only_when_it_owns_transaction(self):
        class FailingConnection:
            def __init__(self): self.rollbacks=0
            def execute(self, sql, params=()): raise RuntimeError("write failed")
            def commit(self): raise AssertionError("unexpected commit")
            def rollback(self): self.rollbacks += 1
        owned = FailingConnection()
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            Repository(owned, backend="postgres").create_change_reason("broken")
        self.assertEqual(owned.rollbacks, 1)
        caller_owned = FailingConnection()
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            Repository(caller_owned, backend="postgres").create_change_reason("broken", commit=False)
        self.assertEqual(caller_owned.rollbacks, 0)

    def test_server_priority_update_uses_postgres_placeholders_and_optional_commit(self):
        class Cursor:
            def __init__(self, row=None): self.row = row
            def fetchone(self): return self.row
        class RecordingConnection:
            def __init__(self): self.calls=[]; self.commits=0; self.rollbacks=0
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "FROM server_route_priorities" in sql:
                    return Cursor({"id": 7, "country_id": 1, "server_id": 2, "current_route_id": 3, "previous_route_id": None, "comment": "before"})
                if "FROM routes WHERE id" in sql:
                    return Cursor({"id": 4, "country_id": 1})
                return Cursor()
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        connection = RecordingConnection()
        repo = Repository(connection, backend="postgres")
        with patch.object(repo, "_server_route_priority_summary", return_value="summary"):
            repo.update_server_route_priority(priority_id=7, current_route_id=4, comment="changed", changed_by=9, commit=False)
        sql = "\n".join(call[0] for call in connection.calls)
        self.assertIn("current_route_id = %s", sql)
        self.assertIn("previous_route_id = current_route_id", sql)
        self.assertNotIn("?", sql)
        self.assertEqual(connection.commits, 0)
        repo.update_server_route_priority(priority_id=7, current_route_id=4, comment="changed", changed_by=9)
        self.assertEqual(connection.commits, 1)

    def test_hlr_limit_override_keeps_sqlite_commit_default_and_allows_caller_transaction(self):
        self.repo.set_hlr_limit_override("2500")
        self.assertEqual(self.repo.get_hlr_limit_override(), "2500")
        self.repo.set_hlr_limit_override("5151", commit=False)
        self.assertEqual(self.repo.get_hlr_limit_override(), "5151")
        self.conn.rollback()
        self.assertEqual(self.repo.get_hlr_limit_override(), "2500")

    def test_hlr_limit_override_records_postgres_placeholder_and_upsert(self):
        class RecordingConnection:
            def __init__(self): self.calls = []; self.commits = 0
            def execute(self, sql, params=()): self.calls.append((sql, params))
            def commit(self): self.commits += 1
            def rollback(self): raise AssertionError("unexpected rollback")
        connection = RecordingConnection()
        Repository(connection, backend="postgres").set_hlr_limit_override("5151", commit=False)
        sql, params = connection.calls[0]
        self.assertIn("VALUES (%s, %s, CURRENT_TIMESTAMP, %s)", sql)
        self.assertIn("ON CONFLICT(key) DO UPDATE", sql)
        self.assertEqual(params, ("hlr_daily_limit_override", "5151", None))
        self.assertEqual(connection.commits, 0)

    def test_stage52_app_settings_use_postgres_placeholders_and_commit_contract(self):
        class RecordingConnection:
            def __init__(self): self.calls=[]; self.commits=0; self.rollbacks=0
            def execute(self, sql, params=()): self.calls.append((sql, params))
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        connection = RecordingConnection(); repo = Repository(connection, backend="postgres")
        repo.set_app_setting_value("key", "value", 7)
        self.assertIn("VALUES (%s, %s, CURRENT_TIMESTAMP, %s)", connection.calls[0][0])
        self.assertIn("ON CONFLICT(key) DO UPDATE", connection.calls[0][0])
        self.assertEqual(connection.calls[0][1], ("key", "value", 7)); self.assertEqual(connection.commits, 1)
        repo.delete_app_setting_value("key", commit=False)
        self.assertEqual(connection.calls[-1], ("DELETE FROM app_settings WHERE key = %s", ("key",)))
        self.assertEqual(connection.commits, 1)

    def test_stage52_hlr_usage_uses_postgres_placeholders_and_returns_usage(self):
        class Cursor:
            def fetchone(self): return None
        class RecordingConnection:
            def __init__(self): self.calls=[]; self.commits=0
            def execute(self, sql, params=()): self.calls.append((sql, params)); return Cursor()
            def commit(self): self.commits += 1
            def rollback(self): raise AssertionError("unexpected rollback")
        connection = RecordingConnection(); repo = Repository(connection, backend="postgres")
        with patch.object(repo, "get_hlr_daily_usage", return_value={"usage_date": "2099-12-31"}) as getter:
            result = repo.upsert_hlr_daily_usage("2099-12-31", 3, "0.75", "2099-12-31 10:00", commit=False)
        self.assertIn("WHERE usage_date = %s", connection.calls[0][0])
        self.assertIn("VALUES (%s, %s, %s, %s, %s, %s)", connection.calls[1][0])
        self.assertIn("ON CONFLICT(usage_date) DO UPDATE", connection.calls[1][0])
        self.assertEqual(connection.calls[1][1], ("2099-12-31", 3, 0.75, 3, "0.75", "2099-12-31 10:00"))
        self.assertEqual(connection.commits, 0); getter.assert_called_once_with("2099-12-31")
        self.assertEqual(result, {"usage_date": "2099-12-31"})

    def test_stage52_sqlite_app_settings_and_hlr_usage_keep_caller_owned_commit(self):
        self.repo.set_app_setting_value("stage52", "value", commit=False)
        self.assertEqual(self.repo.get_app_setting_value("stage52"), "value")
        self.conn.rollback()
        self.assertIsNone(self.repo.get_app_setting_value("stage52"))
        usage = self.repo.upsert_hlr_daily_usage("2099-12-31", 3, "0.75", "2099-12-31 10:00", commit=False)
        self.assertEqual(usage["checked_today"], 3)
        self.conn.rollback()
        self.assertEqual(self.repo.get_hlr_daily_usage("2099-12-31")["checked_today"], 0)

    def test_stage54_dictionary_creates_use_postgres_returning_and_caller_commit_contract(self):
        class Cursor:
            def fetchone(self): return {"id": 91}
        class RecordingConnection:
            def __init__(self): self.calls=[]; self.commits=0; self.rollbacks=0
            def execute(self, sql, params=()): self.calls.append((sql, params)); return Cursor()
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        conn = RecordingConnection(); repo = Repository(conn, backend="postgres")
        self.assertEqual(repo.create_country("Stage 54", "S54", commit=False), 91)
        self.assertEqual(repo.create_currency("S54", "Stage 54 Currency", "S54", commit=False), 91)
        self.assertEqual(repo.create_provider(" Stage 54 Provider ", provider_type="voice", default_currency_id=91, comment="probe", commit=False), 91)
        self.assertEqual(repo.create_prefix(91, " 9954 ", "Stage 54 Prefix", commit=False), 91)
        sql = "\n".join(call[0] for call in conn.calls)
        self.assertIn("INSERT INTO countries(name, code, is_active) VALUES (%s, %s, %s) RETURNING id", sql)
        self.assertIn("INSERT INTO currencies(code, name, symbol, is_active) VALUES (%s, %s, %s, %s) RETURNING id", sql)
        self.assertIn("INSERT INTO providers", sql); self.assertIn("INSERT INTO provider_prefixes", sql)
        self.assertEqual(conn.calls[2][1], (" Stage 54 Provider ", "stage 54 provider", "voice", 91, True, "probe"))
        self.assertEqual(conn.calls[3][1], (91, "9954", "Stage 54 Prefix", True))
        self.assertEqual(conn.commits, 0)
        repo.create_country("Committed", "SC")
        self.assertEqual(conn.commits, 1)

    def test_stage54_sqlite_dictionary_creates_roll_back_when_caller_owns_transaction(self):
        country_id = self.repo.create_country("Stage 54 Country", "S54", commit=False)
        currency_id = self.repo.create_currency("S54", "Stage 54 Currency", "S54", commit=False)
        provider_id = self.repo.create_provider("Stage 54 Provider", default_currency_id=currency_id, commit=False)
        prefix_id = self.repo.create_prefix(provider_id, "9954", "Stage 54 Prefix", commit=False)
        self.assertTrue(all(identifier > 0 for identifier in (country_id, currency_id, provider_id, prefix_id)))
        self.conn.rollback()
        self.assertIsNone(self.conn.execute("SELECT 1 FROM countries WHERE id = ?", (country_id,)).fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM currencies WHERE id = ?", (currency_id,)).fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM providers WHERE id = ?", (provider_id,)).fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM provider_prefixes WHERE id = ?", (prefix_id,)).fetchone())

    def test_stage56_sqlite_dictionary_ensures_ignore_duplicates_and_roll_back(self):
        self.assertEqual(self.repo.ensure_project_exists("Stage 56 Project", commit=False), 1)
        self.assertEqual(self.repo.ensure_project_exists("Stage 56 Project", commit=False), 0)
        self.assertEqual(self.repo.ensure_phone_number_type_exists("Stage 56 Type", commit=False), 1)
        self.assertEqual(self.repo.ensure_phone_number_type_exists("Stage 56 Type", commit=False), 0)
        self.assertEqual(self.repo.ensure_phone_assignment_type_exists("stage56", commit=False), 1)
        self.assertEqual(self.repo.ensure_phone_assignment_type_exists("stage56", commit=False), 0)
        self.conn.rollback()
        self.assertIsNone(self.conn.execute("SELECT 1 FROM projects WHERE name = ?", ("Stage 56 Project",)).fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM phone_number_types WHERE name = ?", ("Stage 56 Type",)).fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM phone_assignment_types WHERE code = ?", ("stage56",)).fetchone())

    def test_stage56_postgres_dictionary_ensures_use_insert_ignore_and_commit_contract(self):
        class Cursor:
            rowcount = 1
        class RecordingConnection:
            def __init__(self, fail=False): self.calls=[]; self.commits=0; self.rollbacks=0; self.fail=fail
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if self.fail: raise RuntimeError("write failed")
                return Cursor()
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1

        conn = RecordingConnection(); repo = Repository(conn, backend="postgres")
        self.assertEqual(repo.ensure_project_exists("Project"), 1)
        self.assertEqual(repo.ensure_phone_number_type_exists("Number type", commit=False), 1)
        self.assertEqual(repo.ensure_phone_assignment_type_exists("assignment", None, False), 1)
        sql = "\n".join(call[0] for call in conn.calls)
        self.assertEqual(sql.count("ON CONFLICT"), 3)
        self.assertIn("INSERT INTO projects(name, is_active) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING", sql)
        self.assertIn("INSERT INTO phone_number_types(name, is_active) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING", sql)
        self.assertIn("INSERT INTO phone_assignment_types(code, name, is_active) VALUES (%s, %s, %s) ON CONFLICT (code) DO NOTHING", sql)
        self.assertEqual(conn.calls[-1][1], ("assignment", "assignment", True))
        self.assertEqual(conn.commits, 1)

        for method, args in (("ensure_project_exists", ("Project",)), ("ensure_phone_number_type_exists", ("Type",)), ("ensure_phone_assignment_type_exists", ("assignment",))):
            failing = RecordingConnection(fail=True)
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                getattr(Repository(failing, backend="postgres"), method)(*args)
            self.assertEqual(failing.rollbacks, 1)
            caller_owned = RecordingConnection(fail=True)
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                getattr(Repository(caller_owned, backend="postgres"), method)(*args, commit=False)
            self.assertEqual(caller_owned.rollbacks, 0)

    def test_update_calling_company_import_fields_updates_row_and_booleans(self):
        user_id = self.repo.create_user("company-import-admin", "Company Import Admin")
        country_id = self.repo.create_country("Италия", "IT")
        server_id = self.repo.create_server("company-import-server")
        company_id = self.repo.create_calling_company(
            server_id=server_id,
            country_id=country_id,
            company_name="Before",
            company_id_external="import-1",
            has_autorotation=False,
            created_by=user_id,
            is_active=True,
        )

        rowcount = self.repo.update_calling_company_import_fields(
            server_id=server_id,
            country_id=country_id,
            company_id_external="import-1",
            company_name="After",
            has_autorotation=True,
            comment="Imported update",
            is_active=False,
            updated_by=user_id,
        )

        row = self.conn.execute("SELECT * FROM calling_companies WHERE id = ?", (company_id,)).fetchone()
        self.assertEqual(rowcount, 1)
        self.assertEqual(row["company_name"], "After")
        self.assertEqual(row["has_autorotation"], 1)
        self.assertEqual(row["comment"], "Imported update")
        self.assertEqual(row["is_active"], 0)
        self.assertEqual(row["updated_by"], user_id)

    def test_update_calling_company_import_fields_returns_zero_for_missing_row(self):
        rowcount = self.repo.update_calling_company_import_fields(
            server_id=999,
            country_id=999,
            company_id_external="missing",
            company_name="Missing",
            has_autorotation=False,
            comment=None,
            is_active=True,
            updated_by=999,
        )

        self.assertEqual(rowcount, 0)


if __name__ == "__main__":
    unittest.main()

class Stage53UserAdminWriteMethodsTest(unittest.TestCase):
    class Cursor:
        lastrowid = 91
        def __init__(self, row=None): self.row = row
        def fetchone(self): return self.row
        def fetchall(self): return []
    class RecordingConnection:
        def __init__(self): self.calls=[]; self.commits=0; self.rollbacks=0
        def execute(self, sql, params=()):
            self.calls.append((sql, params))
            if 'information_schema.columns' in sql: return Stage53UserAdminWriteMethodsTest.Cursor()
            if sql.startswith('SELECT id FROM users'): return Stage53UserAdminWriteMethodsTest.Cursor()
            if 'RETURNING id' in sql: return Stage53UserAdminWriteMethodsTest.Cursor({'id': 91})
            return Stage53UserAdminWriteMethodsTest.Cursor()
        def commit(self): self.commits += 1
        def rollback(self): self.rollbacks += 1

    def test_postgres_user_admin_writes_use_adapter_contracts(self):
        conn = self.RecordingConnection(); repo = Repository(conn, backend='postgres')
        with patch.object(repo, '_user_columns', return_value={'role_key','role','email','must_change_password','password_hash','password_salt','auth_provider'}):
            self.assertEqual(repo.create_user('stage53', password='pw', must_change_password=True, commit=False), 91)
            repo.update_user(91, display_name=' Updated ', role_key='admin', is_active=True, username='stage53', email='a@example.test', commit=False)
            repo.update_user_password(91, 'new', must_change_password=False, commit=False)
            repo.set_user_permissions(91, {'routes': {'can_read': True, 'can_write': True, 'can_export': False}}, commit=False)
        sql = '\n'.join(call[0] for call in conn.calls)
        self.assertIn('SELECT id FROM users WHERE username = %s', sql)
        self.assertIn('INSERT INTO users', sql); self.assertIn('RETURNING id', sql)
        self.assertIn('UPDATE users SET display_name = %s, is_active = %s', sql)
        self.assertIn('password_hash = %s, password_salt = %s', sql)
        self.assertIn('VALUES (%s, %s, %s, %s, %s)', sql); self.assertIn('ON CONFLICT(user_id, section_key) DO UPDATE', sql)
        self.assertEqual(conn.commits, 0)

    def test_sqlite_user_admin_writes_commit_and_rollback_when_caller_owned(self):
        conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row; init_db(conn); repo = Repository(conn)
        user_id = repo.create_user('stage53-sqlite', password='old', must_change_password=True)
        self.assertTrue(repo.authenticate_user('stage53-sqlite', 'old'))
        repo.update_user(user_id, display_name='Updated', role_key='admin', is_active=True, email='after@example.test', commit=False)
        repo.set_user_permissions(user_id, {'routes': {'can_read': True, 'can_write': True, 'can_export': False}}, commit=False)
        repo.update_user_password(user_id, 'new', must_change_password=False, commit=False)
        self.assertTrue(repo.authenticate_user('stage53-sqlite', 'new')); self.assertFalse(repo.authenticate_user('stage53-sqlite', 'old'))
        conn.rollback()
        self.assertEqual(repo.get_user(user_id)['display_name'], 'stage53-sqlite')
        self.assertEqual(repo.get_user_permissions(user_id), {})
        conn.close()

# Stage 55 keeps get-or-create PostgreSQL adapter coverage separate from Stage 54 creates.
class RepositoryStage55GetOrCreateTest(unittest.TestCase):
    def test_postgres_selects_use_placeholders_and_missing_paths_forward_commit(self):
        class Cursor:
            def __init__(self, row): self.row = row
            def fetchone(self): return self.row
        class Connection:
            def __init__(self): self.calls = []; self.commits = 0
            def execute(self, sql, params=()): self.calls.append((sql, params)); return Cursor(None)
            def commit(self): self.commits += 1
        conn = Connection(); repo = Repository(conn, backend="postgres")
        with patch.object(repo, "create_country", return_value=1) as country, patch.object(repo, "create_currency", return_value=2) as currency, patch.object(repo, "create_provider", return_value=3) as provider, patch.object(repo, "create_prefix", return_value=4) as prefix:
            self.assertEqual(repo.get_or_create_country("Country", commit=False), 1)
            self.assertEqual(repo.get_or_create_currency("S55", commit=False), 2)
            self.assertEqual(repo.get_or_create_provider(" Provider ", 2, commit=False), 3)
            self.assertEqual(repo.get_or_create_prefix(3, " 9955 ", commit=False), 4)
            self.assertIsNone(repo.get_or_create_prefix(3, "без префикса", commit=False))
        self.assertTrue(all("?" not in sql and "%s" in sql for sql, _ in conn.calls))
        self.assertEqual(conn.calls[2][1], ("provider",))
        self.assertEqual(conn.calls[3][1], (3, "9955"))
        country.assert_called_once_with("Country", commit=False)
        currency.assert_called_once_with("S55", "S55", commit=False)
        provider.assert_called_once_with(" Provider ", default_currency_id=2, commit=False)
        prefix.assert_called_once_with(3, "9955", commit=False)
        self.assertEqual(conn.commits, 0)

    def test_postgres_existing_paths_return_ids_without_write_or_commit(self):
        class Cursor:
            def fetchone(self): return {"id": 55}
        class Connection:
            def __init__(self): self.calls = []; self.commits = 0
            def execute(self, sql, params=()): self.calls.append((sql, params)); return Cursor()
            def commit(self): self.commits += 1
        conn = Connection(); repo = Repository(conn, backend="postgres")
        self.assertEqual(repo.get_or_create_country("Country"), 55)
        self.assertEqual(repo.get_or_create_currency("S55"), 55)
        self.assertEqual(repo.get_or_create_provider("Provider"), 55)
        self.assertEqual(repo.get_or_create_prefix(55, "9955"), 55)
        self.assertEqual(conn.commits, 0)

    def test_sqlite_get_or_create_rows_roll_back_when_caller_owns_transaction(self):
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row; init_db(conn)
        repo = Repository(conn)
        country = repo.get_or_create_country("Stage 55 Country", commit=False)
        currency = repo.get_or_create_currency("S55", commit=False)
        provider = repo.get_or_create_provider("Stage 55 Provider", currency, commit=False)
        prefix = repo.get_or_create_prefix(provider, "9955", commit=False)
        self.assertEqual(repo.get_or_create_country("Stage 55 Country", commit=False), country)
        self.assertIsNone(repo.get_or_create_prefix(provider, "без префикса", commit=False))
        conn.rollback()
        self.assertIsNone(conn.execute("SELECT 1 FROM countries WHERE id = ?", (country,)).fetchone())
        self.assertIsNone(conn.execute("SELECT 1 FROM currencies WHERE id = ?", (currency,)).fetchone())
        self.assertIsNone(conn.execute("SELECT 1 FROM providers WHERE id = ?", (provider,)).fetchone())
        self.assertIsNone(conn.execute("SELECT 1 FROM provider_prefixes WHERE id = ?", (prefix,)).fetchone())
        conn.close()

class RepositoryStage57ServerWriteTest(unittest.TestCase):
    def test_postgres_create_server_uses_returning_bool_and_commit_contract(self):
        class Cursor:
            def fetchone(self): return {"id": 57}
        class Connection:
            def __init__(self): self.calls=[]; self.commits=0; self.rollbacks=0
            def execute(self, sql, params=()): self.calls.append((sql, params)); return Cursor()
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        conn = Connection(); repo = Repository(conn, backend="postgres")
        self.assertEqual(repo.create_server("Stage 57", "probe", commit=False), 57)
        self.assertIn("INSERT INTO servers(name, comment, is_active) VALUES (%s, %s, %s) RETURNING id", conn.calls[0][0])
        self.assertEqual(conn.calls[0][1], ("Stage 57", "probe", True))
        self.assertEqual(conn.commits, 0)
        self.assertEqual(repo.create_server("Committed"), 57)
        self.assertEqual(conn.commits, 1)

    def test_postgres_create_server_rolls_back_only_when_it_owns_commit(self):
        class Connection:
            def __init__(self): self.commits=0; self.rollbacks=0
            def execute(self, sql, params=()): raise RuntimeError("insert failed")
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        conn = Connection(); repo = Repository(conn, backend="postgres")
        with self.assertRaisesRegex(RuntimeError, "insert failed"):
            repo.create_server("failure")
        self.assertEqual(conn.rollbacks, 1)
        with self.assertRaisesRegex(RuntimeError, "insert failed"):
            repo.create_server("caller failure", commit=False)
        self.assertEqual(conn.rollbacks, 1)

    def test_sqlite_create_server_caller_transaction_rolls_back(self):
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row; init_db(conn)
        repo = Repository(conn)
        server_id = repo.create_server("Stage 57 SQLite", "probe", commit=False)
        row = conn.execute("SELECT name, is_active FROM servers WHERE id = ?", (server_id,)).fetchone()
        self.assertEqual((row["name"], row["is_active"]), ("Stage 57 SQLite", 1))
        conn.rollback()
        self.assertIsNone(conn.execute("SELECT 1 FROM servers WHERE id = ?", (server_id,)).fetchone())
        conn.close()

    def test_provider_change_caller_transaction_updates_priorities_and_rolls_back(self):
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row; init_db(conn); repo = Repository(conn)
        country = repo.create_country("Stage 61 Country", "S61")
        before_provider = repo.create_provider("Stage 61 Before")
        after_provider = repo.create_provider("Stage 61 After")
        conn.execute("INSERT INTO routes(country_id, provider_id, name, cli_source_type, cli_source_label, created_by) VALUES (?, ?, ?, ?, ?, ?)", (country, before_provider, "before", "other", "none", 1))
        before_route = conn.execute("SELECT id FROM routes WHERE name = 'before'").fetchone()["id"]
        conn.execute("INSERT INTO routes(country_id, provider_id, name, cli_source_type, cli_source_label, created_by) VALUES (?, ?, ?, ?, ?, ?)", (country, after_provider, "after", "other", "none", 1))
        after_route = conn.execute("SELECT id FROM routes WHERE name = 'after'").fetchone()["id"]
        existing, new = repo.create_server("stage61-existing"), repo.create_server("stage61-new")
        conn.execute("INSERT INTO server_route_priorities(country_id, server_id, current_route_id, created_by, updated_by) VALUES (?, ?, ?, ?, ?)", (country, existing, before_route, 1, 1))
        conn.commit()
        change_id = repo.create_provider_change(changed_at="2026-07-22 12:00:00", country_id=country, provider_before_id=before_provider, provider_after_id=after_provider, created_by=1, route_before_id=before_route, route_after_id=after_route, reason_text="stage61", comment="stage61 changed", server_ids=[existing, new], commit=False)
        self.assertIsInstance(change_id, int)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM provider_change_log_servers WHERE provider_change_log_id = ?", (change_id,)).fetchone()[0], 2)
        self.assertEqual(tuple(conn.execute("SELECT current_route_id, previous_route_id FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (country, existing)).fetchone()), (after_route, before_route))
        self.assertIsNotNone(conn.execute("SELECT 1 FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (country, new)).fetchone())
        conn.rollback()
        self.assertIsNone(conn.execute("SELECT 1 FROM provider_change_logs WHERE id = ?", (change_id,)).fetchone())
        self.assertEqual(conn.execute("SELECT current_route_id FROM server_route_priorities WHERE country_id = ? AND server_id = ?", (country, existing)).fetchone()[0], before_route)
        conn.close()

class RepositoryStage59DictionarySnapshotsTest(unittest.TestCase):
    def test_postgres_branches_use_backend_placeholders_and_never_commit(self):
        class Cursor:
            def __init__(self, row=None): self.row = row
            def fetchone(self): return self.row
        class Connection:
            def __init__(self): self.calls=[]; self.commits=0
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "SELECT code FROM currencies" in sql: return Cursor({"code": "EUR"})
                if "SELECT code FROM phone_assignment_types" in sql: return Cursor({"code": "assigned"})
                return Cursor({"count": 1})
            def commit(self): self.commits += 1
        conn=Connection(); repo=Repository(conn, backend="postgres")
        for kind, entity, old, new in (("countries",1,"Country","New country"),("providers",2,"Provider","New provider"),("currencies",3,"EUR","unused"),("phone-types",4,"Old type","New type"),("projects",5,"Old project","New project"),("phone-assignments",6,"Old assignment","New assignment"),("unknown",7,"old","new")):
            with patch.object(repo, "dictionary_rename_preview", return_value={"Купленные номера": 1}) as preview:
                self.assertEqual(repo.update_dictionary_snapshots(kind, entity, old, new), {"Купленные номера": 1})
                preview.assert_called_once_with(kind, entity)
        sql="\n".join(query for query, _ in conn.calls)
        self.assertIn("UPDATE phone_numbers SET country_label = %s", sql)
        self.assertIn("UPDATE phone_numbers SET provider_label = %s", sql)
        self.assertIn("SELECT code FROM currencies WHERE id = %s", sql)
        self.assertIn("UPDATE routes SET project_label = %s", sql)
        self.assertIn("SELECT code FROM phone_assignment_types WHERE id = %s", sql)
        self.assertNotIn("?", sql)
        self.assertEqual(conn.commits, 0)

    def test_sqlite_snapshot_branches_and_rollback_preserve_rows(self):
        conn=sqlite3.connect(":memory:"); conn.row_factory=sqlite3.Row; init_db(conn); repo=Repository(conn)
        country=repo.create_country("Stage 59 Country", "S59")
        provider=repo.create_provider("Stage 59 Provider")
        currency=repo.create_currency("S59", "Stage 59 Currency")
        conn.execute("INSERT INTO phone_number_types(name, is_active) VALUES ('old type', 1)")
        phone_type=conn.execute("SELECT id FROM phone_number_types WHERE name='old type'").fetchone()["id"]
        conn.execute("INSERT INTO projects(name, is_active) VALUES ('old project', 1)")
        project=conn.execute("SELECT id FROM projects WHERE name='old project'").fetchone()["id"]
        conn.execute("INSERT INTO phone_assignment_types(code, name, is_active) VALUES ('old-assignment', 'Old assignment', 1)")
        assignment=conn.execute("SELECT id FROM phone_assignment_types WHERE code='old-assignment'").fetchone()["id"]
        conn.execute("INSERT INTO phone_numbers(country_id,provider_id,currency_id,number,normalized_number,created_by,country_label,provider_label,currency_label,phone_type,project_label,assignment_type,assignment_label) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (country,provider,currency,"1234567","1234567",1,"old country","old provider","old currency","old type","old project","old-assignment","old assignment"))
        phone=conn.execute("SELECT id FROM phone_numbers").fetchone()["id"]
        conn.execute("INSERT INTO routes(country_id,provider_id,name,project_label,cli_source_type,cli_source_label,created_by) VALUES (?,?,?,?,?,?,?)", (country,provider,"stage59 route","old project","other","none",1))
        route=conn.execute("SELECT id FROM routes").fetchone()["id"]; conn.commit()
        before_phone=dict(conn.execute("SELECT * FROM phone_numbers WHERE id=?",(phone,)).fetchone()); before_route=dict(conn.execute("SELECT * FROM routes WHERE id=?",(route,)).fetchone())
        repo.update_dictionary_snapshots("countries",country,"Stage 59 Country","new country"); repo.update_dictionary_snapshots("providers",provider,"Stage 59 Provider","new provider"); repo.update_dictionary_snapshots("currencies",currency,"S59","unused"); repo.update_dictionary_snapshots("phone-types",phone_type,"old type","new type"); repo.update_dictionary_snapshots("projects",project,"old project","new project"); repo.update_dictionary_snapshots("phone-assignments",assignment,"Old assignment","new assignment")
        row=conn.execute("SELECT * FROM phone_numbers WHERE id=?",(phone,)).fetchone()
        self.assertEqual((row["country_label"],row["provider_label"],row["currency_label"],row["phone_type"],row["project_label"],row["assignment_label"]),("new country","new provider","S59","new type","new project","new assignment"))
        self.assertEqual(conn.execute("SELECT project_label FROM routes WHERE id=?",(route,)).fetchone()["project_label"],"new project")
        repo.update_dictionary_snapshots("unknown", 999, "old", "new")
        conn.rollback()
        self.assertEqual(dict(conn.execute("SELECT * FROM phone_numbers WHERE id=?",(phone,)).fetchone()),before_phone)
        self.assertEqual(dict(conn.execute("SELECT * FROM routes WHERE id=?",(route,)).fetchone()),before_route)
        conn.close()

class RepositoryStage62RoutingEventDeactivateTest(unittest.TestCase):
    def test_postgres_sql_commit_and_rollback_contract(self):
        class Cursor:
            def __init__(self, row): self.row = row
            def fetchone(self): return self.row
        class Connection:
            def __init__(self, fail=False): self.calls=[]; self.commits=0; self.rollbacks=0; self.fail=fail
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if self.fail and "UPDATE routing_events" in sql: raise RuntimeError("update failed")
                return Cursor({"id": 62, "is_active": True})
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        conn=Connection(); repo=Repository(conn, backend="postgres")
        with patch.object(repo, "_change_log") as log:
            self.assertIsNone(repo.deactivate_routing_event(62, reason="stage62", deactivated_by=1))
        sql="\n".join(q for q,_ in conn.calls)
        self.assertIn("SELECT * FROM routing_events WHERE id = %s", sql)
        self.assertIn("SET is_active = %s", sql)
        self.assertIn("CURRENT_TIMESTAMP", sql)
        self.assertNotIn("?", sql)
        self.assertEqual(conn.calls[1][1][0], False)
        self.assertEqual(conn.commits,1); log.assert_called_once()
        conn=Connection(); repo=Repository(conn, backend="postgres")
        with patch.object(repo, "_change_log"):
            repo.deactivate_routing_event(62, reason="stage62", deactivated_by=1, commit=False)
        self.assertEqual((conn.commits,conn.rollbacks),(0,0))
        conn=Connection(fail=True); repo=Repository(conn, backend="postgres")
        with self.assertRaisesRegex(RuntimeError,"update failed"): repo.deactivate_routing_event(62, reason="stage62", deactivated_by=1)
        self.assertEqual(conn.rollbacks,1)
        with self.assertRaisesRegex(RuntimeError,"update failed"): repo.deactivate_routing_event(62, reason="stage62", deactivated_by=1, commit=False)
        self.assertEqual(conn.rollbacks,1)

    def test_postgres_audit_old_values_serializes_timestamps(self):
        class Cursor:
            def fetchone(self): return {"id": 62, "is_active": True, "event_at": datetime(2026, 7, 22, 13, tzinfo=timezone.utc)}
        class Connection:
            def execute(self, sql, params=()): return Cursor()
            def commit(self): pass
            def rollback(self): pass
        repo=Repository(Connection(), backend="postgres")
        with patch.object(repo, "_change_log") as log:
            repo.deactivate_routing_event(62, reason="stage62", deactivated_by=1, commit=False)
        self.assertEqual(log.call_args.kwargs["old_values"]["event_at"], "2026-07-22 13:00:00+00:00")

    def test_validations_and_sqlite_rollback(self):
        conn=sqlite3.connect(":memory:"); conn.row_factory=sqlite3.Row; init_db(conn); repo=Repository(conn)
        with self.assertRaisesRegex(BusinessRuleError, "Событие маршрутизации не найдено"):
            repo.deactivate_routing_event(999, reason="x", deactivated_by=1, commit=False)
        conn.execute("INSERT INTO routing_events(event_at, apply_scope, reason, comment, is_active, created_by, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)", ("2026-07-22 13:00:00", "none", "Другое", "stage62", 1, 1, 1))
        event_id=conn.execute("SELECT id FROM routing_events").fetchone()["id"]; conn.commit()
        repo.deactivate_routing_event(event_id, reason="stage62 reason", deactivated_by=1, commit=False)
        row=conn.execute("SELECT is_active,deactivation_reason,deactivated_by,updated_by FROM routing_events WHERE id=?",(event_id,)).fetchone()
        self.assertEqual(tuple(row),(0,"stage62 reason",1,1)); self.assertIsNotNone(conn.execute("SELECT 1 FROM change_log WHERE entity_type='routing_event' AND entity_id=?",(event_id,)).fetchone())
        with self.assertRaisesRegex(BusinessRuleError,"Событие уже деактивировано"): repo.deactivate_routing_event(event_id, reason="x", deactivated_by=1, commit=False)
        conn.rollback(); self.assertEqual(conn.execute("SELECT is_active FROM routing_events WHERE id=?",(event_id,)).fetchone()[0],1)
        with self.assertRaisesRegex(BusinessRuleError,"Причина деактивации обязательна"): repo.deactivate_routing_event(event_id, reason="", deactivated_by=1, commit=False)
        conn.close()


class RepositoryStage63RoutingEventUpdateTest(unittest.TestCase):
    def test_postgres_sql_transaction_and_validation_contract(self):
        class Cursor:
            def __init__(self, row): self.row = row
            def fetchone(self): return self.row
        class Connection:
            def __init__(self, row=None, fail=False):
                self.row=row; self.fail=fail; self.calls=[]; self.commits=0; self.rollbacks=0
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if self.fail and "UPDATE routing_events" in sql: raise RuntimeError("update failed")
                return Cursor(self.row if "SELECT * FROM routing_events" in sql else None)
            def commit(self): self.commits += 1
            def rollback(self): self.rollbacks += 1
        row={"id":63,"comment":"before","updated_at":datetime(2026,7,22,14,tzinfo=timezone.utc),"apply_scope":"none","calling_company_id":None}
        conn=Connection(row); repo=Repository(conn,backend="postgres")
        with patch.object(repo,"_change_log") as log:
            repo.update_routing_event(63,updated_by=1,comment="after",updated_at_original="2026-07-22T14:00:00+00:00")
        sql="\n".join(query for query,_ in conn.calls)
        self.assertIn("WHERE id = %s",sql); self.assertIn("comment = %s",sql); self.assertIn("CURRENT_TIMESTAMP",sql); self.assertNotIn("?",sql)
        self.assertEqual(conn.commits,1); self.assertEqual(conn.rollbacks,0); log.assert_called_once()
        conn=Connection(row); repo=Repository(conn,backend="postgres")
        with patch.object(repo,"_change_log"):
            repo.update_routing_event(63,updated_by=1,comment="after",commit=False)
        self.assertEqual((conn.commits,conn.rollbacks),(0,0))
        conn=Connection(row,fail=True); repo=Repository(conn,backend="postgres")
        with self.assertRaisesRegex(RuntimeError,"update failed"): repo.update_routing_event(63,updated_by=1,comment="after")
        self.assertEqual(conn.rollbacks,1)
        with self.assertRaisesRegex(RuntimeError,"update failed"): repo.update_routing_event(63,updated_by=1,comment="after",commit=False)
        self.assertEqual(conn.rollbacks,1)
        conn=Connection(row); repo=Repository(conn,backend="postgres")
        with patch.object(repo,"_change_log") as log:
            repo.update_routing_event(63,updated_by=1,comment="before")
        self.assertEqual(conn.commits,0); log.assert_not_called()
        with self.assertRaisesRegex(BusinessRuleError,"изменена другим пользователем"):
            repo.update_routing_event(63,updated_by=1,comment="after",updated_at_original="stale",commit=False)
        with self.assertRaisesRegex(BusinessRuleError,"Комментарий обязателен"):
            repo.update_routing_event(63,updated_by=1,comment="",commit=False)
        with self.assertRaisesRegex(BusinessRuleError,"Событие маршрутизации не найдено"):
            Repository(Connection(),backend="postgres").update_routing_event(999,updated_by=1,comment="after",commit=False)

    def test_postgres_private_campaign_sync_uses_placeholders_and_boolean(self):
        class Cursor:
            def __init__(self,row): self.row=row
            def fetchone(self): return self.row
        class Connection:
            def __init__(self): self.calls=[]
            def execute(self,sql,params=()):
                self.calls.append((sql,params))
                if "FROM company_routing_settings" in sql: return Cursor({"id":7,"routing_mode":"campaign_route","route_id":8,"has_autorotation":False})
                return Cursor(None)
        conn=Connection(); repo=Repository(conn,backend="postgres")
        event={"id":63,"apply_scope":"campaign_setting","calling_company_id":6,"event_at":datetime(2026,7,22,14,tzinfo=timezone.utc),"new_company_routing_mode":"campaign_route","new_company_route_id":8,"new_company_has_autorotation":0}
        repo._sync_company_routing_comment_from_event(event,comment="updated",updated_by=1)
        sql="\n".join(q for q,_ in conn.calls)
        self.assertNotIn("?",sql); self.assertIn("calling_company_id = %s",sql); self.assertIn("is_active = %s",sql); self.assertIn("UPDATE company_routing_settings",sql)
        self.assertIs(conn.calls[0][1][1],True); self.assertIs(conn.calls[1][1][1],True)

    def test_sqlite_update_noop_and_caller_rollback(self):
        conn=sqlite3.connect(":memory:"); conn.row_factory=sqlite3.Row; init_db(conn); repo=Repository(conn)
        conn.execute("INSERT INTO routing_events(event_at,apply_scope,reason,comment,is_active,created_by,updated_by) VALUES (?,?,?,?,?,?,?)",("2026-07-22 14:00:00","none","Другое","before",1,1,1))
        event_id=conn.execute("SELECT id FROM routing_events").fetchone()[0]; conn.commit()
        repo.update_routing_event(event_id,updated_by=1,comment="after",commit=False)
        self.assertEqual(conn.execute("SELECT comment FROM routing_events WHERE id=?",(event_id,)).fetchone()[0],"after")
        count=conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type='routing_event' AND entity_id=?",(event_id,)).fetchone()[0]
        self.assertEqual(count,1)
        repo.update_routing_event(event_id,updated_by=1,comment="after",commit=False)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type='routing_event' AND entity_id=?",(event_id,)).fetchone()[0],count)
        conn.rollback()
        self.assertEqual(conn.execute("SELECT comment FROM routing_events WHERE id=?",(event_id,)).fetchone()[0],"before")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM change_log WHERE entity_type='routing_event' AND entity_id=?",(event_id,)).fetchone()[0],0)
        conn.close()

class RepositoryStage65ACompanyRoutingTest(unittest.TestCase):
    def test_sqlite_lifecycle_caller_owned_transaction_rolls_back(self):
        conn=sqlite3.connect(":memory:"); conn.row_factory=sqlite3.Row; init_db(conn); repo=Repository(conn)
        user=conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()[0]
        country=repo.create_country("Stage 65A GEO","65A"); server=repo.create_server("stage65a-server")
        provider=repo.create_provider("Stage65A provider")
        route1=repo.create_route(country_id=country,provider_id=provider,name="stage65a-r1",cli_source_type="other",cli_source_label="65A",created_by=user)
        route2=repo.create_route(country_id=country,provider_id=provider,name="stage65a-r2",cli_source_type="other",cli_source_label="65A",created_by=user)
        conn.execute("INSERT INTO calling_companies(server_id,country_id,company_name,company_id_external,created_by,updated_by) VALUES(?,?,?,?,?,?)",(server,country,"Stage65A","stage65a",user,user)); company=conn.execute("SELECT last_insert_rowid()").fetchone()[0]; conn.commit()
        created=repo.create_company_routing_setting(calling_company_id=company,country_id=country,server_id=server,route_id=route1,routing_mode="campaign_route",has_autorotation=False,comment="created",created_by=user,commit=False)
        conn.rollback(); self.assertIsNone(conn.execute("SELECT 1 FROM company_routing_settings WHERE id=?",(created,)).fetchone())
        created=repo.create_company_routing_setting(calling_company_id=company,country_id=country,server_id=server,route_id=route1,routing_mode="campaign_route",has_autorotation=False,comment="created",created_by=user); baseline=conn.execute("SELECT * FROM company_routing_settings WHERE id=?",(created,)).fetchone()
        updated=repo.update_company_routing_setting(setting_id=created,country_id=country,server_id=server,route_id=route2,routing_mode="mixed",has_autorotation=True,comment="updated",updated_by=user,commit=False)
        conn.rollback(); restored=conn.execute("SELECT * FROM company_routing_settings WHERE id=?",(created,)).fetchone(); self.assertEqual((restored["is_active"],restored["valid_to"],restored["route_id"]),(baseline["is_active"],baseline["valid_to"],baseline["route_id"])); self.assertIsNone(conn.execute("SELECT 1 FROM company_routing_settings WHERE id=?",(updated,)).fetchone())
        repo.deactivate_company_routing_setting(setting_id=created,updated_by=user,commit=False); self.assertEqual(conn.execute("SELECT is_active FROM company_routing_settings WHERE id=?",(created,)).fetchone()[0],0)
        conn.rollback(); self.assertEqual(conn.execute("SELECT is_active FROM company_routing_settings WHERE id=?",(created,)).fetchone()[0],1); conn.close()

    def test_postgres_create_sql_boolean_identity_and_commit_contract(self):
        class Cursor:
            def __init__(self,row=None): self.row=row
            def fetchone(self): return self.row
        class Conn:
            def __init__(self,fail=False): self.calls=[]; self.commits=0; self.rollbacks=0; self.fail=fail
            def execute(self,sql,params=()):
                self.calls.append((sql,params))
                if self.fail: raise RuntimeError("failed")
                if "RETURNING id" in sql: return Cursor({"id":650})
                return Cursor(None)
            def commit(self): self.commits+=1
            def rollback(self): self.rollbacks+=1
        kwargs=dict(calling_company_id=1,country_id=2,server_id=3,route_id=4,routing_mode="mixed",has_autorotation=True,comment="x",created_by=5,effective_at="2026-07-25")
        conn=Conn(); repo=Repository(conn,backend="postgres")
        with patch.object(repo,"_validate_company_routing_values"),patch.object(repo,"_active_company_routing_setting",return_value=None),patch.object(repo,"_company_routing_summary",return_value="summary"),patch.object(repo,"_change_log"):
            self.assertEqual(repo.create_company_routing_setting(**kwargs),650)
            repo.create_company_routing_setting(commit=False,**kwargs)
        sql="\n".join(q for q,_ in conn.calls); self.assertNotIn("?",sql); self.assertIn("VALUES (%s",sql); self.assertIn("RETURNING id",sql); insert_params=next(params for sql,params in conn.calls if "RETURNING id" in sql); self.assertIs(insert_params[5],True); self.assertIs(insert_params[6],True); self.assertEqual(conn.commits,1)
        for commit,expected in ((True,1),(False,0)):
            bad=Conn(True); method=Repository(bad,backend="postgres").create_company_routing_setting
            with self.assertRaisesRegex(RuntimeError,"failed"): method(commit=commit,**kwargs)
            self.assertEqual(bad.rollbacks,expected)

    def test_postgres_update_and_deactivate_sql_and_commit_contract(self):
        existing={"id":7,"calling_company_id":1,"country_id":2,"server_id":3,"route_id":4,"routing_mode":"campaign_route","has_autorotation":False,"comment":"before","is_active":True,"valid_to":None}
        class Cursor:
            def __init__(self,row=None): self.row=row
            def fetchone(self): return self.row
        class Conn:
            def __init__(self): self.calls=[]; self.commits=0; self.rollbacks=0
            def execute(self,sql,params=()):
                self.calls.append((sql,params))
                if "SELECT * FROM company_routing_settings" in sql:return Cursor(existing)
                if "RETURNING id" in sql:return Cursor({"id":8})
                return Cursor(None)
            def commit(self):self.commits+=1
            def rollback(self):self.rollbacks+=1
        conn=Conn(); repo=Repository(conn,backend="postgres")
        with patch.object(repo,"_validate_company_routing_values"),patch.object(repo,"_company_routing_summary",return_value="summary"),patch.object(repo,"_change_log"):
            self.assertEqual(repo.update_company_routing_setting(setting_id=7,country_id=2,server_id=3,route_id=5,routing_mode="mixed",has_autorotation=True,comment="after",updated_by=9,effective_at="2026-07-25",commit=False),8)
            repo.deactivate_company_routing_setting(setting_id=7,updated_by=9,effective_at="2026-07-25",commit=False)
        sql="\n".join(q for q,_ in conn.calls); self.assertNotIn("?",sql); self.assertIn("is_active = %s",sql); self.assertIn("RETURNING id",sql); self.assertEqual((conn.commits,conn.rollbacks),(0,0)); self.assertTrue(any(False in params for _,params in conn.calls))

class RepositoryStage66ARouteImportTest(unittest.TestCase):
    def test_sqlite_route_lifecycle_is_caller_owned_and_rollback_safe(self):
        conn=sqlite3.connect(':memory:'); conn.row_factory=sqlite3.Row; init_db(conn); repo=Repository(conn)
        user=conn.execute('SELECT id FROM users ORDER BY id LIMIT 1').fetchone()[0]
        country=repo.create_country('Stage 66A GEO','66A'); provider=repo.create_provider('Stage66A provider')
        route=repo.create_route(country_id=country,provider_id=provider,name='stage66a-original',cli_source_type='other',cli_source_label='original',created_by=user,commit=False)
        self.assertIsNotNone(conn.execute('SELECT 1 FROM routes WHERE id=?',(route,)).fetchone()); conn.rollback()
        self.assertIsNone(conn.execute('SELECT 1 FROM routes WHERE id=?',(route,)).fetchone())
        route=repo.create_route(country_id=country,provider_id=provider,name='stage66a-original',cli_source_type='other',cli_source_label='original',created_by=user)
        repo.update_route(route,name='stage66a-updated',provider_prefix_id=None,comment='updated',is_actual=True,priority_status='unknown',updated_by=user,commit=False)
        self.assertEqual(conn.execute('SELECT name FROM routes WHERE id=?',(route,)).fetchone()[0],'stage66a-updated'); conn.rollback()
        self.assertEqual(conn.execute('SELECT name FROM routes WHERE id=?',(route,)).fetchone()[0],'stage66a-original')
        repo.update_route_import_fields(country_id=country,name='stage66a-original',provider_id=provider,provider_prefix_id=None,project_label='imported',cli_source_type='other',cli_source_label='imported',comment='imported',updated_by=user,commit=False)
        self.assertEqual(conn.execute('SELECT project_label FROM routes WHERE id=?',(route,)).fetchone()[0],'imported'); conn.rollback()
        self.assertIsNone(conn.execute('SELECT project_label FROM routes WHERE id=?',(route,)).fetchone()[0]); conn.close()

    def test_postgres_route_sql_identity_and_transaction_contracts(self):
        class Cursor:
            def __init__(self,row=None,rowcount=1): self.row,self.rowcount=row,rowcount
            def fetchone(self): return self.row
        existing={'id':66,'provider_id':2,'cli_source_type':'other','cli_source_label':'old','name':'old','provider_prefix_id':None,'aon_pool':None,'rnd_type':None,'rnd_pool_owner':None,'comment':None,'is_actual':True,'priority_status':'unknown','country_id':1}
        class Conn:
            def __init__(self,fail=False): self.calls=[]; self.commits=0; self.rollbacks=0; self.fail=fail
            def execute(self,sql,params=()):
                self.calls.append((sql,params))
                if self.fail: raise RuntimeError('failed')
                if 'RETURNING id' in sql:return Cursor({'id':66})
                if 'SELECT * FROM routes' in sql:return Cursor(existing)
                return Cursor()
            def commit(self):self.commits+=1
            def rollback(self):self.rollbacks+=1
        conn=Conn(); repo=Repository(conn,backend='postgres')
        route=repo.create_route(country_id=1,provider_id=2,name='route',cli_source_type='other',cli_source_label='label',created_by=3)
        self.assertEqual(route,66)
        repo.update_route(66,name='updated',provider_prefix_id=None,comment='updated',is_actual=True,priority_status='unknown',updated_by=3,commit=False)
        repo.update_route_import_fields(country_id=1,name='updated',provider_id=2,provider_prefix_id=None,project_label='p',cli_source_type='other',cli_source_label='import',comment='import',updated_by=3,commit=False)
        sql='\n'.join(q for q,_ in conn.calls)
        self.assertNotIn('?',sql); self.assertIn('RETURNING id',sql); self.assertIn('VALUES (%s',sql); self.assertIn('WHERE id = %s',sql); self.assertEqual(conn.commits,1)
        for method,args in (
            ('create_route',dict(country_id=1,provider_id=2,name='x',cli_source_type='other',cli_source_label='x',created_by=3)),
            ('update_route',dict(route_id=66,name='x',provider_prefix_id=None,comment=None,is_actual=True,priority_status='unknown',updated_by=3)),
            ('update_route_import_fields',dict(country_id=1,name='x',provider_id=2,provider_prefix_id=None,project_label=None,cli_source_type='other',cli_source_label='x',comment=None,updated_by=3))):
            for commit,rollbacks in ((True,1),(False,0)):
                bad=Conn(True)
                with self.assertRaisesRegex(RuntimeError,'failed'): getattr(Repository(bad,backend='postgres'),method)(commit=commit,**args)
                self.assertEqual(bad.rollbacks,rollbacks)

class RepositoryStage66BPhoneImportTest(unittest.TestCase):
    class Cursor:
        def __init__(self, row=None, rowcount=1): self.row, self.rowcount = row, rowcount
        def fetchone(self): return self.row

    class Conn:
        def __init__(self, fail=False): self.calls=[]; self.commits=0; self.rollbacks=0; self.fail=fail
        def execute(self, sql, params=()):
            self.calls.append((sql, params))
            if self.fail: raise RuntimeError('failed')
            if 'SELECT name FROM countries' in sql: return RepositoryStage66BPhoneImportTest.Cursor({'name':'GEO'})
            if 'SELECT name FROM providers' in sql: return RepositoryStage66BPhoneImportTest.Cursor({'name':'Provider'})
            if 'SELECT name FROM phone_assignment_types' in sql: return RepositoryStage66BPhoneImportTest.Cursor({'name':'Assignment'})
            if 'SELECT code FROM currencies' in sql: return RepositoryStage66BPhoneImportTest.Cursor({'code':'USD'})
            if 'RETURNING id' in sql: return RepositoryStage66BPhoneImportTest.Cursor({'id':661})
            if 'SELECT * FROM phone_numbers' in sql:
                return RepositoryStage66BPhoneImportTest.Cursor({'id':661,'number':'79996660001','status':'used','is_active':True,'review_required':False,'comment':'before','country_id':1,'provider_id':2,'project_label':'p','assignment_type':'gl','connection_cost':None,'monthly_fee':None,'currency_id':3,'phone_type':'Mobile','tariff_label':None})
            if 'SELECT id, route_id' in sql: return []
            return RepositoryStage66BPhoneImportTest.Cursor(rowcount=1)
        def commit(self): self.commits += 1
        def rollback(self): self.rollbacks += 1

    @staticmethod
    def create_kwargs():
        return dict(country_id=1,provider_id=2,currency_id=3,number='79996660001',assignment_type='gl',status='used',created_by=4,comment='stage66b')

    @staticmethod
    def update_kwargs():
        return dict(phone_id=661,country_id=1,provider_id=2,currency_id=3,number='79996660001',assignment_type='gl',status='free',is_active=True,updated_by=4,comment='after')

    @staticmethod
    def import_kwargs():
        return dict(normalized_number='79996660001',phone_number_id=661,country_id=1,provider_id=2,project_label='imported',assignment_type='gl',status='unused',is_active=True,connection_cost=None,monthly_fee=None,outgoing_rate=None,incoming_rate=None,currency_id=3,phone_type='Mobile',tariff_label=None,comment='imported',review_required=False,imported_created_by='stage66b',deactivated_at=None,updated_by=4,history_changed_by=4,history_new_value='stage66b',history_comment='stage66b')

    def test_postgres_phone_sql_and_top_level_commit_contracts(self):
        conn=self.Conn(); repo=Repository(conn,backend='postgres')
        self.assertEqual(repo.create_phone_number(**self.create_kwargs()),661)
        repo.update_phone_number(commit=False,**self.update_kwargs())
        repo.record_phone_update_history(661,4,{'comment':'old'},{'comment':'new'},commit=False)
        self.assertEqual(repo.update_phone_number_import_fields_with_history(commit=False,**self.import_kwargs()),1)
        sql='\n'.join(q for q,_ in conn.calls)
        self.assertNotIn('?',sql); self.assertIn('RETURNING id',sql); self.assertIn('VALUES (%s',sql)
        self.assertIn('WHERE id = %s',sql); self.assertIn('normalized_number = %s',sql)
        self.assertEqual((conn.commits,conn.rollbacks),(1,0))

    def test_phone_methods_only_rollback_owned_transactions(self):
        cases=(('create_phone_number',self.create_kwargs()),('update_phone_number',self.update_kwargs()),('record_phone_update_history',dict(phone_id=661,changed_by=4,old_values={'comment':'a'},new_values={'comment':'b'})),('update_phone_number_import_fields_with_history',self.import_kwargs()))
        for method,kwargs in cases:
            for commit, expected in ((True,1),(False,0)):
                conn=self.Conn(fail=True)
                with self.assertRaisesRegex(RuntimeError,'failed'):
                    getattr(Repository(conn,backend='postgres'),method)(commit=commit,**kwargs)
                self.assertEqual(conn.rollbacks,expected)
                self.assertEqual(conn.commits,0)

    def test_sqlite_phone_history_is_caller_owned_and_rollback_safe(self):
        conn=sqlite3.connect(':memory:'); conn.row_factory=sqlite3.Row; init_db(conn); repo=Repository(conn)
        user=conn.execute('SELECT id FROM users ORDER BY id LIMIT 1').fetchone()[0]
        country=repo.create_country('Stage 66B GEO','S6B'); provider=repo.create_provider('Stage66B provider')
        phone=repo.create_phone_number(country_id=country,provider_id=provider,number='79996660001',assignment_type='gl',status='used',created_by=user)
        before=conn.execute('SELECT COUNT(*) FROM phone_number_history WHERE phone_number_id=?',(phone,)).fetchone()[0]
        repo.record_phone_update_history(phone,user,{'comment':'before'},{'comment':'after'},commit=False)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM phone_number_history WHERE phone_number_id=?',(phone,)).fetchone()[0],before+1)
        conn.rollback()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM phone_number_history WHERE phone_number_id=?',(phone,)).fetchone()[0],before)
        conn.close()
