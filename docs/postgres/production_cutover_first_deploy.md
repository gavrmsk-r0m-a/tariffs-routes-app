# PostgreSQL production cutover: first deploy

This operator checklist applies to baseline release
`e5ee9dd663d604b17936882e465b94f242bdf09c`.

It does not perform a deployment and contains no production credentials. All secret values
must be supplied through the hosting platform's environment configuration or secret storage.

## 1. Required production environment

Configure these values in the production hosting environment. Do not write their resolved
values to a shell history, CI log, ticket, or repository file.

```text
DB_BACKEND=postgres
POSTGRES_RUNTIME_ENABLED=1
DATABASE_URL=<production PostgreSQL URL from secret storage>
MVP_PRODUCTION_SECURITY=1
MVP_AUTH_SECRET=<strong secret from secret storage>
```

For an empty production database only, bootstrap the first administrator if one is required:

```text
MVP_BOOTSTRAP_ADMIN_USERNAME=<admin username>
MVP_BOOTSTRAP_ADMIN_PASSWORD=<strong temporary password>
MVP_BOOTSTRAP_ADMIN_DISPLAY_NAME=<optional display name>
```

Rotate or remove the bootstrap password immediately after the initial administrator is
created and access is verified.

## 2. Pre-cutover evidence

Do not start the cutover until every item below has an evidence link or artifact location.

- [ ] The production PostgreSQL database exists and is reachable from the hosting runtime.
- [ ] PostgreSQL is the initial production database; production has never started on SQLite.
- [ ] PostgreSQL preflight and initialization or migration completed successfully.
- [ ] `scripts/postgres_backup.py` produced a backup and manifest.
- [ ] The backup manifest SHA-256 was independently verified.
- [ ] A restore rehearsal into a fresh database passed, including table counts, users, and smoke checks.
- [ ] The deployment rollback rehearsal passed.
- [ ] The strict production gate, runtime smoke, and final enablement check below passed.

Record evidence without secret values:

| Evidence | Artifact or result location |
| --- | --- |
| Preflight / initialization | |
| Backup and verified manifest | |
| Fresh-database restore rehearsal | |
| Deployment rollback rehearsal | |
| Strict production gate | |
| Runtime enablement smoke | |
| Final enablement check | |

## 3. Mandatory checks

Run these commands in production or staging with the real environment supplied by secret
storage. Ensure command tracing is disabled and review captured output before attaching it
to an operational record.

```bash
python scripts/audit_postgres_production_gate.py --strict

python scripts/postgres_runtime_enablement_smoke.py \
  --database-url "$DATABASE_URL" \
  --auth-secret "$MVP_AUTH_SECRET" \
  --format json

python scripts/postgres_final_enablement_check.py \
  --database-url "$DATABASE_URL" \
  --current-release-sha "e5ee9dd663d604b17936882e465b94f242bdf09c" \
  --rollback-release-sha "$PREVIOUS_KNOWN_GOOD_RELEASE_SHA" \
  --backup-manifest "$VERIFIED_BACKUP_MANIFEST" \
  --approval docs/postgres/final_enablement_approval.md \
  --strict \
  --format json
```

Any non-zero result is a stop condition. Do not enable production traffic until the cause is
understood and the complete check is rerun successfully.

## 4. Cutover

1. Confirm the previous known-good release SHA and verified backup manifest are available.
2. Deploy release `e5ee9dd663d604b17936882e465b94f242bdf09c`.
3. Apply the production environment from section 1 through hosting or secret storage.
4. Start the application and verify that its runtime database backend is PostgreSQL.
5. Verify login with a named operator account.
6. Verify the read-only pages for routes, tariffs, phones, companies, and provider changes.
7. Do not run bulk writes or imports during the observation period.
8. Perform one safe write only when it was explicitly agreed in advance, then verify its result.

## 5. Rollback

### Application does not start

- Disable `POSTGRES_RUNTIME_ENABLED` or roll back to the previous known-good release according
  to the rehearsed deployment procedure.
- Do not manually patch the production database.

Disabling the runtime flag is an abort mechanism, not permission to operate production on
SQLite. Keep traffic disabled unless the rehearsed rollback release is known to use the
intended production datastore safely.

### Database state is invalid

1. Keep writes and production traffic disabled.
2. Restore the verified backup into a fresh PostgreSQL database; never restore in place.
3. Verify the backup SHA-256, table counts, users, and smoke checks in the fresh database.
4. Change `DATABASE_URL` through secret storage only after verification succeeds.
5. Restart and repeat the mandatory checks and read-only verification.

## 6. Closeout record

- [ ] Bootstrap administrator password removed or rotated.
- [ ] Default credentials confirmed not to work.
- [ ] `MVP_PRODUCTION_SECURITY=1` remains enabled.
- [ ] Backup manifest and deployed/rollback release SHAs archived.
- [ ] Actual cutover time and responsible operator recorded below.

| Field | Recorded value (no secrets) |
| --- | --- |
| Cutover start (UTC) | |
| Cutover complete (UTC) | |
| Operator | |
| Deployed release SHA | `e5ee9dd663d604b17936882e465b94f242bdf09c` |
| Previous known-good release SHA | |
| Backup manifest location | |
| Observation-period outcome | |
