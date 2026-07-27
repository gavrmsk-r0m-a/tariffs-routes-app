# PostgreSQL Deployment Rollback Runbook

## Status after Stage 67E

- Deployment rollback gate is implemented.
- Production enablement is still blocked.
- Final enablement remains blocked.

## Rollback principles

- A tested rollback procedure is required before production enablement.
- Never restore directly into the production DB in-place. Restore into a fresh database first.
- Verify the backup manifest SHA-256 and public table counts before accepting a restore.
- Verify application smoke/read checks, `SELECT 1`, and a non-empty `users` table before switching `DATABASE_URL`.
- Keep `MVP_AUTH_SECRET` stable across rollback so existing cookies and sessions are not invalidated unexpectedly.
- Keep production security mode enabled throughout rollback.
- Never log database passwords, URLs containing credentials, or authentication secrets.

## Abort-before-enable path

When deploy or cutover has not been approved, abort without setting
`POSTGRES_RUNTIME_ENABLED=1`. Keep the SQLite/default runtime unchanged. No database
restore is needed because no runtime cutover occurred.

## App release rollback path

When the application release fails but the database is intact, redeploy the previous
release or commit while keeping the same PostgreSQL database. Run the application smoke
and read checks before returning traffic.

## Database restore rollback path

When migration or cutover produced a bad database state:

1. Preserve the affected database for investigation; do not overwrite or delete it before postmortem and approval.
2. Restore the pre-deployment backup into a fresh database.
3. Verify the manifest SHA-256 and all public table counts.
4. Verify `SELECT 1`, the non-empty `users` table, and application smoke/read checks.
5. Only after verification, repoint `DATABASE_URL` to the restored database through the approved deployment mechanism.

## Commands

Use environment variables supplied by the deployment secret store; never paste real secrets
into commands or logs.

```bash
python scripts/postgres_backup.py \
  --database-url "$DATABASE_URL" \
  --output "$ROLLBACK_WORKDIR/pre-deployment.dump" \
  --manifest "$ROLLBACK_WORKDIR/pre-deployment.manifest.json"

python scripts/postgres_restore_verify.py \
  --backup-file "$ROLLBACK_WORKDIR/pre-deployment.dump" \
  --target-database-url "$FRESH_ROLLBACK_DATABASE_URL" \
  --manifest "$ROLLBACK_WORKDIR/pre-deployment.manifest.json"

python scripts/postgres_deployment_rollback_check.py \
  --current-release-sha "$CURRENT_SHA" \
  --rollback-release-sha "$ROLLBACK_SHA" \
  --backup-manifest "$ROLLBACK_WORKDIR/pre-deployment.manifest.json" \
  --strict --format json

python scripts/postgres_deployment_rollback_smoke.py \
  --database-url "$DATABASE_URL" \
  --workdir "$ROLLBACK_WORKDIR/rehearsal" \
  --current-release-sha "$CURRENT_SHA" \
  --rollback-release-sha "$ROLLBACK_SHA"
```

The check can run without `--strict` for inventory review; it reports a warning when the
backup file is unavailable. Operational approval requires strict digest verification.

## CI rehearsal

Hosted PostgreSQL Migration Smoke applies the migration, performs backup/restore smoke,
and performs deployment rollback smoke. It then runs the read-only Repository smoke and
the rollback-only Repository write harness. The rollback rehearsal creates and removes a
disposable database; it does not enable production runtime.

## Not covered yet

- Final production enablement approval.
