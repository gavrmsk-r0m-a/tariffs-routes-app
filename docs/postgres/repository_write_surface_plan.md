# PostgreSQL write-surface plan — Stage 50

## Executive summary

- Read-only Repository coverage: 61/61 = 100%.
- Deferred read-only methods: 0.
- Write/mutating Repository methods: 50.
- Infrastructure/mixed: 1 (`transaction`).
- Runtime direct SQL still exists; DB_BACKEND=postgres remains disabled.
- No write methods were adapted in Stage 50.

## Why 100% read-only coverage is not runtime readiness

Read coverage proves only the audited read surface. Write methods remain unadapted, direct SQL remains outside Repository, the runtime connection remains SQLite, migrations/init_db are SQLite-oriented, and transaction semantics are not unified.

## Write surface batches

| Batch | Risk | Methods count | Purpose | Recommended order |
| --- | --- | ---: | --- | ---: |
| `app_settings_and_admin_low_risk` | high | 7 | App settings and admin low risk | 2 |
| `company_routing_setting_writes` | high | 6 | Company routing-setting writes | 6 |
| `dictionary_and_snapshot_writes` | high | 14 | Dictionary and snapshot writes | 3 |
| `importer_and_bulk_mutation_writes` | critical | 3 | Importer and bulk mutation writes | 8 |
| `phone_route_tariff_core_writes` | high | 14 | Phone, route, and tariff core writes | 4 |
| `provider_change_and_priority_writes` | high | 2 | Provider-change and priority writes | 5 |
| `routing_event_application_writes` | high | 3 | Routing event application writes | 7 |
| `write_test_harness_and_transaction_foundation` | critical | 1 | Write test harness and transaction foundation | 1 |

`runtime_direct_sql_extraction` is a separate future runtime scope, not a Repository-method batch.

## Method inventory

| Method | Mutation kind | Batch | Risk | Main blockers | Transaction contract |
| --- | --- | --- | --- | --- | --- |
| `add_phone_to_route` | `mixed_read_write` | `phone_route_tariff_core_writes` | medium | dynamic_sql, needs_integration_test, postgres_returning_required | `multi_statement_atomic` |
| `add_phone_to_route_by_number` | `mixed_read_write` | `phone_route_tariff_core_writes` | high | multi_statement_atomicity, needs_integration_test, sqlite_placeholder | `multi_statement_atomic` |
| `create_calling_company` | `insert` | `company_routing_setting_writes` | high | dynamic_sql, history_change_log_coupling, multi_statement_atomicity | `multi_statement_atomic` |
| `create_change_reason` | `insert` | `dictionary_and_snapshot_writes` | medium | dynamic_sql, needs_integration_test, postgres_transaction_aborted_behavior | `explicit_commit` |
| `create_company_routing_setting` | `insert` | `company_routing_setting_writes` | medium | history_change_log_coupling, needs_integration_test, postgres_returning_required | `explicit_commit` |
| `create_country` | `insert` | `dictionary_and_snapshot_writes` | medium | dynamic_sql, needs_integration_test, postgres_transaction_aborted_behavior | `explicit_commit` |
| `create_currency` | `insert` | `dictionary_and_snapshot_writes` | medium | needs_integration_test, postgres_returning_required, postgres_transaction_aborted_behavior | `explicit_commit` |
| `create_currency_rate` | `insert` | `phone_route_tariff_core_writes` | medium | needs_integration_test, postgres_returning_required, postgres_transaction_aborted_behavior | `explicit_commit` |
| `create_phone_number` | `insert` | `phone_route_tariff_core_writes` | medium | dynamic_sql, needs_integration_test, postgres_transaction_aborted_behavior | `explicit_commit` |
| `create_prefix` | `insert` | `dictionary_and_snapshot_writes` | medium | needs_integration_test, postgres_returning_required, postgres_transaction_aborted_behavior | `explicit_commit` |
| `create_provider` | `insert` | `dictionary_and_snapshot_writes` | medium | dynamic_sql, needs_integration_test, postgres_transaction_aborted_behavior | `explicit_commit` |
| `create_provider_change` | `insert` | `provider_change_and_priority_writes` | medium | dynamic_sql, history_change_log_coupling, needs_integration_test | `explicit_commit` |
| `create_route` | `insert` | `phone_route_tariff_core_writes` | medium | needs_integration_test, postgres_returning_required, postgres_transaction_aborted_behavior | `explicit_commit` |
| `create_routing_event` | `insert` | `routing_event_application_writes` | medium | history_change_log_coupling, needs_integration_test, postgres_returning_required | `explicit_commit` |
| `create_server` | `insert` | `dictionary_and_snapshot_writes` | medium | dynamic_sql, needs_integration_test, postgres_transaction_aborted_behavior | `explicit_commit` |
| `create_tariff` | `insert` | `phone_route_tariff_core_writes` | medium | needs_integration_test, postgres_returning_required, postgres_transaction_aborted_behavior | `explicit_commit` |
| `create_user` | `insert` | `app_settings_and_admin_low_risk` | medium | dynamic_sql, needs_integration_test, postgres_returning_required | `explicit_commit` |
| `deactivate_company_routing_setting` | `mixed_read_write` | `company_routing_setting_writes` | medium | needs_integration_test, postgres_transaction_aborted_behavior, sqlite_datetime_function | `multi_statement_atomic` |
| `deactivate_routing_event` | `mixed_read_write` | `routing_event_application_writes` | medium | dynamic_sql, needs_integration_test, postgres_transaction_aborted_behavior | `multi_statement_atomic` |
| `delete_app_setting_value` | `delete` | `app_settings_and_admin_low_risk` | medium | needs_integration_test, postgres_transaction_aborted_behavior, sqlite_placeholder | `explicit_commit` |
| `ensure_phone_assignment_type_exists` | `multi_write` | `dictionary_and_snapshot_writes` | high | needs_integration_test, postgres_transaction_aborted_behavior, sqlite_placeholder | `multi_statement_atomic` |
| `ensure_phone_number_type_exists` | `multi_write` | `dictionary_and_snapshot_writes` | high | needs_integration_test, postgres_transaction_aborted_behavior, sqlite_placeholder | `multi_statement_atomic` |
| `ensure_project_exists` | `multi_write` | `dictionary_and_snapshot_writes` | high | needs_integration_test, postgres_transaction_aborted_behavior, sqlite_placeholder | `multi_statement_atomic` |
| `get_or_create_country` | `mixed_read_write` | `dictionary_and_snapshot_writes` | high | multi_statement_atomicity, needs_integration_test, sqlite_placeholder | `multi_statement_atomic` |
| `get_or_create_currency` | `mixed_read_write` | `dictionary_and_snapshot_writes` | high | multi_statement_atomicity, needs_integration_test, sqlite_placeholder | `multi_statement_atomic` |
| `get_or_create_prefix` | `mixed_read_write` | `dictionary_and_snapshot_writes` | high | multi_statement_atomicity, needs_integration_test, sqlite_placeholder | `multi_statement_atomic` |
| `get_or_create_provider` | `mixed_read_write` | `dictionary_and_snapshot_writes` | high | multi_statement_atomicity, needs_integration_test, sqlite_placeholder | `multi_statement_atomic` |
| `log_currency_rate_change` | `write_with_history` | `phone_route_tariff_core_writes` | medium | dynamic_sql, needs_integration_test, sqlite_placeholder | `multi_statement_atomic` |
| `recalculate_current_tariffs_for_currency_rate` | `multi_write` | `phone_route_tariff_core_writes` | high | dynamic_sql, needs_integration_test, sqlite_datetime_function | `multi_statement_atomic` |
| `record_phone_update_history` | `write_with_history` | `phone_route_tariff_core_writes` | medium | dynamic_sql, needs_integration_test, sqlite_placeholder | `multi_statement_atomic` |
| `remove_phone_links_from_route` | `mixed_read_write` | `phone_route_tariff_core_writes` | medium | needs_integration_test, postgres_transaction_aborted_behavior, sqlite_datetime_function | `multi_statement_atomic` |
| `set_app_setting_value` | `update` | `app_settings_and_admin_low_risk` | medium | needs_integration_test, postgres_transaction_aborted_behavior, sqlite_datetime_function | `explicit_commit` |
| `set_hlr_limit_override` | `mixed_read_write` | `write_test_harness_and_transaction_foundation` | high | multi_statement_atomicity, needs_integration_test, sqlite_placeholder | `multi_statement_atomic` |
| `set_tariff_active` | `update` | `phone_route_tariff_core_writes` | medium | needs_integration_test, postgres_transaction_aborted_behavior, sqlite_datetime_function | `explicit_commit` |
| `set_user_permissions` | `update` | `app_settings_and_admin_low_risk` | medium | needs_integration_test, postgres_transaction_aborted_behavior, sqlite_placeholder | `explicit_commit` |
| `update_calling_company` | `multi_write` | `company_routing_setting_writes` | high | dynamic_sql, history_change_log_coupling, needs_integration_test | `multi_statement_atomic` |
| `update_calling_company_import_fields` | `multi_write` | `importer_and_bulk_mutation_writes` | critical | bulk_destructive_operation, dynamic_sql, history_change_log_coupling | `multi_statement_atomic` |
| `update_company_routing_setting` | `multi_write` | `company_routing_setting_writes` | high | history_change_log_coupling, needs_integration_test, postgres_returning_required | `multi_statement_atomic` |
| `update_company_routing_setting_comment` | `multi_write` | `company_routing_setting_writes` | high | history_change_log_coupling, needs_integration_test, postgres_transaction_aborted_behavior | `multi_statement_atomic` |
| `update_dictionary_snapshots` | `multi_write` | `dictionary_and_snapshot_writes` | high | history_change_log_coupling, needs_integration_test, sqlite_placeholder | `multi_statement_atomic` |
| `update_phone_number` | `multi_write` | `phone_route_tariff_core_writes` | high | history_change_log_coupling, multi_statement_atomicity, needs_integration_test | `multi_statement_atomic` |
| `update_phone_number_import_fields_with_history` | `multi_write` | `importer_and_bulk_mutation_writes` | critical | bulk_destructive_operation, dynamic_sql, history_change_log_coupling | `multi_statement_atomic` |
| `update_route` | `multi_write` | `phone_route_tariff_core_writes` | high | dynamic_sql, history_change_log_coupling, needs_integration_test | `multi_statement_atomic` |
| `update_route_import_fields` | `multi_write` | `importer_and_bulk_mutation_writes` | critical | bulk_destructive_operation, dynamic_sql, history_change_log_coupling | `multi_statement_atomic` |
| `update_routing_event` | `multi_write` | `routing_event_application_writes` | high | history_change_log_coupling, needs_integration_test, postgres_transaction_aborted_behavior | `multi_statement_atomic` |
| `update_server_route_priority` | `multi_write` | `provider_change_and_priority_writes` | high | history_change_log_coupling, needs_integration_test, postgres_transaction_aborted_behavior | `multi_statement_atomic` |
| `update_tariff` | `multi_write` | `phone_route_tariff_core_writes` | high | dynamic_sql, history_change_log_coupling, needs_integration_test | `multi_statement_atomic` |
| `update_user` | `multi_write` | `app_settings_and_admin_low_risk` | high | dynamic_sql, history_change_log_coupling, needs_integration_test | `multi_statement_atomic` |
| `update_user_password` | `multi_write` | `app_settings_and_admin_low_risk` | high | dynamic_sql, history_change_log_coupling, needs_integration_test | `multi_statement_atomic` |
| `upsert_hlr_daily_usage` | `upsert` | `app_settings_and_admin_low_risk` | medium | needs_integration_test, postgres_transaction_aborted_behavior, sqlite_placeholder | `explicit_commit` |

## Transaction model risks

- Repository methods currently own commits and, in several paths, rollbacks; `set_app_setting_value`, `delete_app_setting_value`, `upsert_hlr_daily_usage`, and `set_user_permissions` do both.
- `commit=False`/caller-owned patterns and nested public write calls require an explicit ownership policy before adaptation.
- Multi-statement history, snapshot, routing-event, and importer paths must remain atomic. PostgreSQL marks a transaction aborted after an error until rollback.
- SQLite `lastrowid`, conflict syntax, dynamic SQL, and side effects after partial writes make write adaptation without a transaction harness unsafe.

## Runtime direct SQL

The unchanged runtime census is `app/db.py`, `app/server.py`, and `app/importer.py`: 53 SELECT, 65 writes, 32 schema/PRAGMA, and 11 dynamic/unknown calls. Stage 50 does not move or adapt these calls.

## Recommended Stage 51

**Stage 51: PostgreSQL write test harness and transaction foundation.** It must keep production runtime and `DB_BACKEND=postgres` disabled, add a rollback-only PostgreSQL write harness, establish transaction ownership, verify PostgreSQL error/rollback behavior, and prepare RETURNING/boolean/upsert patterns. It must not adapt business domain writes except minimal synthetic harness probes.

## Stage 51 boundary clarification

`set_hlr_limit_override` is listed in the Stage 51 foundation batch solely as a minimal rollback-only synthetic probe. Stage 51 does **not** adapt it as a production/business write, does not enable `DB_BACKEND=postgres`, and does not start business-domain write adaptation. Every probe must run in an explicit transaction and finish with transaction rollback or `SAVEPOINT` rollback.

## Stage 51 status: foundation added

The CI-only rollback harness is now the transaction foundation for later write stages. It uses `set_hlr_limit_override` only as a synthetic caller-owned (`commit=False`) probe, verifies visibility before a full rollback and verifies the prior value afterward. It also documents PostgreSQL's aborted-transaction behavior and SAVEPOINT recovery. No business write adaptation or production PostgreSQL runtime is included; the first small adaptation batch remains after this harness is green.

## Stage 52 status: first app-settings rollback smoke

Stage 52 rollback-smokes `set_app_setting_value`, `delete_app_setting_value`, and `upsert_hlr_daily_usage` on PostgreSQL, alongside the unchanged foundation-only `set_hlr_limit_override` probe. The machine-readable plan records all four methods. This is not production runtime enablement: `DB_BACKEND=postgres` remains disabled, and user/admin writes are still not adapted. A reviewed next candidate may be user/admin low-risk or dictionary writes.

## Stage 53 status: user/admin rollback smoke

`create_user`, `update_user`, `update_user_password`, and `set_user_permissions` are PostgreSQL-compatible and rollback-smoked only through the CI harness. The `app_settings_and_admin_low_risk` batch now has seven rollback-smoked methods; the separate foundation batch retains `set_hlr_limit_override`. There is still no production PostgreSQL runtime or `DB_BACKEND=postgres` enablement. A next candidate is dictionary writes, or a smaller dedicated audit before dictionaries.

## Stage 54 status: core dictionary creates rollback-smoked

`create_country`, `create_currency`, `create_provider`, and `create_prefix` are PostgreSQL-compatible and rollback-smoked through the CI-only dictionary probe. Their `get_or_create_*` counterparts and dictionary snapshot writes are not adapted. Production PostgreSQL runtime remains disabled; a future candidate is the dictionary `get_or_create` methods or the `ensure_*` dictionary methods.
