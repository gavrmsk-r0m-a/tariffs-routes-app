# PostgreSQL rollback-only Repository write harness

Run the CI-only harness against a migrated disposable PostgreSQL database:

```bash
python scripts/postgres_repository_write_harness.py --postgres-url "$DATABASE_URL" --json
```

`DATABASE_URL` is accepted when `--postgres-url` is omitted. `--output path` writes the JSON summary to a file. The optional `--probe-key` is a diagnostic label; the public HLR Repository API deliberately owns its fixed application-setting key. The default synthetic value is `5151`.

## Safety model

The harness does not import application runtime modules other than `Repository`, does not run migrations or DDL, and never calls `conn.commit()`. Every Repository write is called with `commit=False`; each probe opens an explicit transaction and rolls it back in `finally` before checking restoration. This leaves no probe write behind.

Two read-only transaction probes deliberately query a missing table. The aborted-transaction probe confirms PostgreSQL rejects a later `SELECT 1` until rollback; the SAVEPOINT probe uses `ROLLBACK TO SAVEPOINT` and then confirms `SELECT 1` works before rolling back the whole transaction.

## Summary and failures

The JSON summary contains `status`, a masked `postgres_url`, `checks_count`, `failures`, and per-probe status. `status: ok` means all 13 probes completed and rollback restoration succeeded. A write-probe failure means the database must be inspected before any later write adaptation. An aborted or SAVEPOINT failure means transaction semantics are not validated and later write stages must not proceed.

## Stage 52 app-settings and HLR usage probes

Stage 52 extends the same rollback-only transaction boundary with `app_setting_probe` and `hlr_daily_usage_probe`. The app-setting probe writes and deletes `__stage52_app_setting_probe__`, verifies each state through Repository reads, and verifies the exact pre-probe value after rollback. The usage probe incrementally upserts `2099-12-31`, verifies first and second values with Decimal-safe comparisons, and verifies the exact prior usage state after rollback.

The harness still never calls `conn.commit()`: every probe rolls back in `finally`, including failure paths. It uses no DDL, migrations, runtime backend switch, or direct SQL state assertions. Consequently no probe app-setting value or `hlr_daily_usage` row/change can remain after a completed rollback.

## Stage 53 user/admin probe

`user_admin_probe` uses the deterministic `__stage53_user_admin_probe__` username. In one explicit transaction it creates an admin user, verifies transaction-local identity and authentication, updates profile data, upserts routes/settings permissions, changes the password, and verifies the old-to-new authentication switch. All four Repository writes use `commit=False`. Its `finally` block rolls back, then Repository reads prove that neither the user nor its permissions remain.

## Stage 54 dictionary-create probe

`dictionary_create_probe` uses deterministic Stage 54 values to create a country, currency, provider (with that currency as default), and provider prefix in that order. All four Repository calls pass `commit=False`. Read-only PostgreSQL queries verify each active entity and its transaction-local relationships before `finally` rolls the transaction back. A second read-only check confirms that no Stage 54 country, currency, provider, or prefix row remains. The harness still never calls `conn.commit()`.

## Stage 55 dictionary get-or-create probe

`dictionary_get_or_create_probe` uses separate deterministic Stage 55 values. Within one explicit transaction it calls each dictionary `get_or_create_*` method twice, proving both the create path and existing-row path return the same identity. It also verifies active rows, the normalized provider name and currency relationship, the normalized prefix relationship, and that the Russian no-prefix text returns `None` without a prefix row. All calls use `commit=False`; `finally` rolls back on success or failure, and read-only checks then prove no Stage 55 country, currency, provider, or prefix rows remain. The harness still never calls `conn.commit()`.

## Stage 56 dictionary ensure probe

`dictionary_ensure_probe` uses separate deterministic project, phone-number-type, and phone-assignment-type values. It calls each `ensure_*` method twice with `commit=False`, proving the first insert path returns `1` and the existing/ignore path returns `0`. Read-only PostgreSQL queries verify the transaction-local rows are active and that the assignment code and name are preserved. Its `finally` rollback runs on both success and failure, and post-rollback read-only checks prove that no Stage 56 dictionary rows remain. The harness never calls `conn.commit()`.

## Stage 57 dictionary server probe

`dictionary_server_probe` creates `__stage57_server_probe__` through `create_server(..., commit=False)` inside an explicit PostgreSQL transaction. Read-only `%s`-placeholder checks prove that the returned identity, name, and active flag are visible before the `finally` rollback. A post-rollback read confirms no probe server row remains. The harness never calls `conn.commit()`.

## Stage 58: change-reason dictionary probe

`dictionary_change_reason_probe` creates `__stage58_change_reason_probe__` with its deterministic comment through `create_change_reason(..., commit=False)`. It verifies that both the `change_reasons` row and the `change_log` `change_reason.created` side effect are visible within the explicit PostgreSQL transaction. The probe always rolls back and confirms that neither row remains; the harness never calls `conn.commit()`.

## Stage 59: dictionary snapshot probe

`dictionary_snapshot_probe` uses only transaction-local direct SQL fixture setup, then invokes `update_dictionary_snapshots` for countries, providers, currencies, phone-types, projects, and phone-assignments (plus the unknown-kind path). It verifies every update is visible in the open transaction and always rolls back in `finally`; post-rollback checks require the selected phone and route rows to match their original values and reject residual `__stage59_` labels. The harness never calls `conn.commit()`.

## Stage 60: provider-change/priority probe

`provider_change_priority_probe` starts the `provider_change_and_priority_writes` batch without adapting `create_provider_change`. It prepares a transaction-local server-priority fixture with direct PostgreSQL SQL, invokes `update_server_route_priority(..., commit=False)` for the route-changed branch, and verifies the new `current_route_id`, the old route in `previous_route_id`, audit values, and the visible `change_log` side effect. It then invokes the same-route/comment branch and proves that both route identifiers are retained while the comment and latest audit entry change. A missing-route validation failure is isolated with a SAVEPOINT. The probe always rolls back in `finally` and rejects residual Stage 60 comments or audit rows afterward; it never calls `conn.commit()`.

## Stage 61: provider-change creation probe

`provider_change_create_probe` completes the provider-change/priority batch. In one explicit rollback-only transaction it selects a country with routes from two providers, prepares an existing priority plus a server without one, and calls `create_provider_change(..., commit=False)`. The changed-provider path verifies the provider-change row, both `provider_change_log_servers` rows, existing-priority update, new-priority insert, and `change_log` side effect. It then verifies the provider-unchanged path accepts no server IDs and creates no server links or priority changes. Missing reason, missing servers, and route/provider mismatch validations are isolated with SAVEPOINTs. Its `finally` rolls back, and cleanup queries reject all Stage 61 provider-change, priority, and audit markers. The harness still never calls `conn.commit()`.
