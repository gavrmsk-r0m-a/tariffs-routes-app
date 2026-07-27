# PostgreSQL Production Readiness Gate

## Current status after Stage 67E

- Read-only smoke: **611 checks**.
- Coverage: **112 / 61 / 0 / 50 / 1 / 100.0%**.
- Write plan: **50/50** `write_or_mutating` public methods rollback-smoked.
- Write harness: **25 probes**.
- A guarded PostgreSQL runtime adapter exists, while SQLite remains the default.
- `DB_BACKEND=postgres` requires both `POSTGRES_RUNTIME_ENABLED=1` (the exact
  value) and `DATABASE_URL`.
- This is not production enablement; the production gate remains blocked.
- The backup/restore gate is `ok`; the hosted smoke verifies a custom-format backup,
  temporary restore, manifest digest, and public-table counts.
- The security gate is `ok`; strong production secrets, hardened cookies, production
  credential policy, login throttling, and passwordless-switching restrictions are verified.
- The deployment rollback gate is `ok`; a pre-deployment backup is restored and verified
  in a fresh disposable database, never in-place in the production database.

## What is ready

- Migration apply smoke exists.
- Read-only Repository smoke exists.
- Rollback-only write harness exists.
- All public Repository write methods have rollback-smoke coverage.

These checks establish surface coverage; they do not establish production readiness.
Final enablement approval is the sole remaining blocker.

## What is not ready yet

- Final production runbook.

## Required gates before enabling DB_BACKEND=postgres

1. Guarded runtime adapter (completed in Stage 67B, without enabling production).
2. Backup/restore PR, including a verified restore exercise and runbook (completed in Stage 67C).
3. Security hardening PR covering production credentials, login throttling, secrets,
   and session configuration (completed in Stage 67D).
4. Production dry-run PR, including the deployment rollback procedure (completed in Stage 67E).
5. Explicit final enablement PR after every preceding gate is green.

## Hard rule

`DB_BACKEND=postgres` **must not be enabled** by default and must not be enabled in
production until all gates are green. Stage 67B leaves SQLite as the default and
blocks PostgreSQL unless the explicit runtime guard is set. The adapter guard is
not approval to use it in production.

## Current audit command

```bash
python scripts/audit_postgres_production_gate.py --format json
```

The non-strict audit exits successfully when the project facts are valid and the
gate correctly remains blocked. This is the mode suitable for current CI.

## Strict mode

```bash
python scripts/audit_postgres_production_gate.py --strict
```

Strict mode is still expected to fail until the final enablement gate is completed. A successful
strict audit is a prerequisite for the later explicit runtime-enablement decision,
not a signal that Stage 67B permits production enablement.

## Stage 67B guarded runtime adapter

The runtime adapter now connects with `psycopg` and dictionary rows only when
`POSTGRES_RUNTIME_ENABLED=1` is set and `DATABASE_URL` is present. Other truthy
spellings do not enable it. The adapter does not run SQLite initialization,
PRAGMAs, schema creation, or default-user seeding.

`runtime_adapter_gate` is now `ok`, but backup/restore, security, deployment
rollback, and explicit final enablement remain blocked. Consequently the strict
audit is still expected to return non-zero.

## Stage 67C backup/restore gate

The backup/restore gate is now `ok`. Backups use `pg_dump` custom format, and restore
verification checks the manifest SHA-256 digest and all public-table row counts. Hosted CI
runs the rehearsal after migration apply and before the existing Repository smoke checks.

The overall production gate remains `blocked` and
`ready_for_runtime_enablement=false`. Security hardening, the deployment rollback procedure,
and explicit final enablement approval remain blockers, so strict audit intentionally
returns non-zero.

## Stage 67D security gate

The `security_gate` is now `ok`. Production security mode requires a strong auth secret,
uses `Secure`, `HttpOnly`, and `SameSite=Lax` cookies, rejects known default credentials,
throttles password logins, and disables passwordless user switching. The overall gate remains
`status=blocked` with `ready_for_runtime_enablement=false`; deployment rollback documentation
and final enablement approval remain blockers. Strict audit therefore still intentionally fails.

## Stage 67E deployment rollback gate

The `deployment_rollback_gate` is now `ok`. The hosted rehearsal creates a pre-deployment
backup, verifies its manifest SHA-256 and table counts, restores it into a fresh temporary
database, runs connectivity and users checks, and removes the temporary database in `finally`.
In-place restore into production is prohibited by the rollback runbook.

The overall production gate remains `status=blocked` and
`ready_for_runtime_enablement=false`. Final enablement approval is the remaining blocker,
so strict audit is still expected to return non-zero. There is no production enablement yet.
