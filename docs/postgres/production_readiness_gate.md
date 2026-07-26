# PostgreSQL Production Readiness Gate

## Current status after Stage 66F

- Read-only smoke: **611 checks**.
- Coverage: **112 / 61 / 0 / 50 / 1 / 100.0%**.
- Write plan: **50/50** `write_or_mutating` public methods rollback-smoked.
- Write harness: **25 probes**.
- Production runtime: **disabled**.
- `DB_BACKEND=postgres`: not allowed for production yet.

## What is ready

- Migration apply smoke exists.
- Read-only Repository smoke exists.
- Rollback-only write harness exists.
- All public Repository write methods have rollback-smoke coverage.

These checks establish surface coverage; they do not establish production readiness.
The remaining backup, security, runtime, and deployment rollback gates are explicit
blockers rather than deferred assumptions.

## What is not ready yet

- Production PostgreSQL runtime adapter.
- Backup/restore scripts and a verified restore runbook.
- Security gate: no default production passwords, an implemented brute-force/login
  throttling decision, and documented secret/session configuration.
- Deployment rollback gate and documented rollback procedure.
- Final production runbook.

## Required gates before enabling DB_BACKEND=postgres

1. Runtime adapter PR, implemented and tested behind an explicit environment guard.
2. Backup/restore PR, including a verified restore exercise and runbook.
3. Security hardening PR covering production credentials, login throttling, secrets,
   and session configuration.
4. Production dry-run PR, including the deployment rollback procedure.
5. Explicit final enablement PR after every preceding gate is green.

## Hard rule

`DB_BACKEND=postgres` **must not be enabled** by default and must not be enabled in
production until all gates are green. Stage 67A intentionally leaves the gate
blocked and does not implement a PostgreSQL runtime connection.

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
not a signal that Stage 67A is production-ready.
