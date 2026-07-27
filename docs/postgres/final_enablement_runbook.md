# PostgreSQL Final Enablement Runbook

## Status after Stage 67F

- Final enablement gate is implemented.
- Production readiness gate reports `ready_for_runtime_enablement=true`.
- This PR does not deploy production or store secrets.

## Required production environment

- `DB_BACKEND=postgres`
- `POSTGRES_RUNTIME_ENABLED=1`
- `DATABASE_URL` from secret store
- `MVP_PRODUCTION_SECURITY=1`
- `MVP_AUTH_SECRET` or `SECRET_KEY` from secret store
- `MVP_BOOTSTRAP_ADMIN_USERNAME` / `MVP_BOOTSTRAP_ADMIN_PASSWORD` only for first empty DB bootstrap, if needed

## Pre-cutover checklist

- PostgreSQL DB provisioned.
- SQLite is not used as initial production DB.
- Migration/preflight completed.
- Backup created.
- Backup manifest sha256 verified.
- Restore rehearsal completed.
- Deployment rollback rehearsal completed.
- Security mode enabled.
- Final enablement check strict passes.
- Hosted PostgreSQL Migration Smoke green.

## Enablement command examples

Use shell variables populated from secret storage; never paste real values into the repository.

```bash
python scripts/postgres_final_enablement_check.py --database-url "$DATABASE_URL" \
  --current-release-sha "$CURRENT_RELEASE_SHA" --rollback-release-sha "$ROLLBACK_RELEASE_SHA" \
  --backup-manifest /secure/path/backup.manifest.json \
  --approval docs/postgres/final_enablement_approval.md --strict --format json
python scripts/postgres_runtime_enablement_smoke.py --database-url "$DATABASE_URL" \
  --auth-secret "$MVP_AUTH_SECRET" --format json
python scripts/audit_postgres_production_gate.py --strict
```

## Cutover procedure

1. Set the required environment variables in hosting configuration.
2. Deploy the selected release.
3. Run health and smoke checks; verify login and read-only pages.
4. Verify one safe non-destructive write path only if approved.
5. Keep the rollback release and backup manifest available.

## Abort/rollback

- If the final check fails, do not enable `POSTGRES_RUNTIME_ENABLED`.
- If runtime smoke fails, revert the hosting environment/release.
- If DB state is bad, restore the backup into a fresh DB, never perform an in-place production restore.

## Post-cutover

- Remove or rotate the bootstrap admin password if used.
- Confirm there are no default credentials.
- Keep production security enabled.
- Archive the backup manifest and release SHAs.
