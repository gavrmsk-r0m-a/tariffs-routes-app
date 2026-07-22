import sqlite3
import unittest
from unittest.mock import patch

from app.db import init_db
from app.repository import Repository


class RepositoryAdapterWriteMethodsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        self.repo = Repository(self.conn)

    def tearDown(self):
        self.conn.close()

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
