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

The JSON summary contains `status`, a masked `postgres_url`, `checks_count`, `failures`, and per-probe status. `status: ok` means all five probes completed and rollback restoration succeeded. A write-probe failure means the database must be inspected before any later write adaptation. An aborted or SAVEPOINT failure means transaction semantics are not validated and later write stages must not proceed.

## Stage 52 app-settings and HLR usage probes

Stage 52 extends the same rollback-only transaction boundary with `app_setting_probe` and `hlr_daily_usage_probe`. The app-setting probe writes and deletes `__stage52_app_setting_probe__`, verifies each state through Repository reads, and verifies the exact pre-probe value after rollback. The usage probe incrementally upserts `2099-12-31`, verifies first and second values with Decimal-safe comparisons, and verifies the exact prior usage state after rollback.

The harness still never calls `conn.commit()`: every probe rolls back in `finally`, including failure paths. It uses no DDL, migrations, runtime backend switch, or direct SQL state assertions. Consequently no probe app-setting value or `hlr_daily_usage` row/change can remain after a completed rollback.
