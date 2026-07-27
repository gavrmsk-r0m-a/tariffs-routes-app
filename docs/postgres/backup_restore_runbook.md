# PostgreSQL Backup and Restore Runbook

## Status after Stage 67C

- The backup/restore gate is implemented and verified.
- Production enablement is still blocked.
- Security, deployment rollback, and final enablement remain blocked.

## Backup command

Supply the source URL through the environment; the value below is only a placeholder and
must be provided by the operator's secret store.

```bash
python scripts/postgres_backup.py \
  --database-url "$DATABASE_URL" \
  --output /secure/backups/teleroute.dump \
  --manifest /secure/backups/teleroute.manifest.json
```

The command uses `pg_dump` custom format with owner and ACL data excluded. It refuses to
replace an existing backup or manifest unless `--overwrite` is explicitly supplied.

## Restore verification command

Restore only into a new staging or temporary database. Set `RESTORE_DATABASE_URL` through
the secret store rather than placing credentials in shell history.

```bash
python scripts/postgres_restore_verify.py \
  --backup-file /secure/backups/teleroute.dump \
  --target-database-url "$RESTORE_DATABASE_URL" \
  --manifest /secure/backups/teleroute.manifest.json
```

The target must be empty by default. The verifier checks the backup SHA-256 digest before
restore and compares every public-table row count with the sorted manifest after restore.

## CI smoke

Hosted **PostgreSQL Migration Smoke** applies the SQLite-to-PostgreSQL migration, creates a
`pg_dump` backup, restores it into a uniquely named temporary database, compares table
counts, and drops that database in cleanup. It then continues with the unchanged read-only
Repository smoke and rollback-only write harness.

## Safety rules

- Never restore into a production database without separate approval.
- Never log `DATABASE_URL` or other database credentials.
- Verify the manifest SHA-256 digest before restore.
- Verify all public-table counts after restore.
- A backup before a production cutover is mandatory.
- A restore rehearsal before enablement is mandatory.

## Not covered yet

- Security hardening.
- Deployment rollback procedure.
- Final enablement approval.
