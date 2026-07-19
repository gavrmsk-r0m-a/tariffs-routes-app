# PostgreSQL rollback-only Repository write harness

Run the CI-only harness against a migrated disposable PostgreSQL database:

```bash
python scripts/postgres_repository_write_harness.py --postgres-url "$DATABASE_URL" --json
```

`DATABASE_URL` is accepted when `--postgres-url` is omitted. `--output path` writes the JSON summary to a file. The optional `--probe-key` is a diagnostic label; the public HLR Repository API deliberately owns its fixed application-setting key. The default synthetic value is `5151`.

## Safety model

The harness does not import application runtime modules other than `Repository`, does not run migrations or DDL, and never calls `conn.commit()`. Its only write is `Repository.set_hlr_limit_override(..., commit=False)`. The harness records the existing override, opens an explicit transaction, verifies the new value is visible, rolls back in `finally`, and verifies the original value afterward. This leaves no probe write behind.

Two read-only transaction probes deliberately query a missing table. The aborted-transaction probe confirms PostgreSQL rejects a later `SELECT 1` until rollback; the SAVEPOINT probe uses `ROLLBACK TO SAVEPOINT` and then confirms `SELECT 1` works before rolling back the whole transaction.

## Summary and failures

The JSON summary contains `status`, a masked `postgres_url`, `checks_count`, `failures`, and per-probe status. `status: ok` means all three probes completed and rollback restoration succeeded. A rollback-probe failure means the database must be inspected before any later write adaptation. An aborted or SAVEPOINT failure means transaction semantics are not validated and later write stages must not proceed.
