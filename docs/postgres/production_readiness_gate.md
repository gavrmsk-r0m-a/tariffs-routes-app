# PostgreSQL Production Readiness Gate

## Current status after Stage 67B

- Read-only smoke: **611 checks**.
- Coverage: **112 / 61 / 0 / 50 / 1 / 100.0%**.
- Write plan: **50/50** `write_or_mutating` public methods rollback-smoked.
- Write harness: **25 probes**.
- A guarded PostgreSQL runtime adapter exists, while SQLite remains the default.
- `DB_BACKEND=postgres` requires both `POSTGRES_RUNTIME_ENABLED=1` (the exact
  value) and `DATABASE_URL`.
- This is not production enablement; the production gate remains blocked.

## What is ready

- Migration apply smoke exists.
- Read-only Repository smoke exists.
- Rollback-only write harness exists.
- All public Repository write methods have rollback-smoke coverage.

These checks establish surface coverage; they do not establish production readiness.
The remaining backup, security, deployment rollback, and final enablement gates are explicit
blockers rather than deferred assumptions.

## What is not ready yet

- Backup/restore scripts and a verified restore runbook.
- Security gate: no default production passwords, an implemented brute-force/login
  throttling decision, and documented secret/session configuration.
- Deployment rollback gate and documented rollback procedure.
- Final production runbook.

## Required gates before enabling DB_BACKEND=postgres

1. Guarded runtime adapter (completed in Stage 67B, without enabling production).
2. Backup/restore PR, including a verified restore exercise and runbook.
3. Security hardening PR covering production credentials, login throttling, secrets,
   and session configuration.
4. Production dry-run PR, including the deployment rollback procedure.
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

Strict mode is expected to fail until production gates are completed. A successful
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
