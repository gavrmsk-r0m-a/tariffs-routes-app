# PostgreSQL Production Readiness Gate

## Status after Stage 67F

- `status=ready`.
- `ready_for_runtime_enablement=true`.
- `runtime_adapter_gate`, `postgres_runtime_default_guarded`, `backup_restore_gate`,
  `security_gate`, `deployment_rollback_gate`, and `final_enablement_gate` are all `ok`.
- `blockers=[]`.
- Read-only smoke remains **611 checks**.
- Coverage remains **113 / 61 / 0 / 51 / 1 / 100.0%**.
- The write plan remains **51/51** rollback-smoked and the harness remains **25 probes**.

This readiness result does not store production secrets and does not deploy production.
Actual deployment remains a separate operator/hosting action using secret-managed environment
configuration. SQLite remains the local/development default and must not be the initial
production database.

## Explicit runtime requirements

Production requires all of the following: `DB_BACKEND=postgres`,
`POSTGRES_RUNTIME_ENABLED=1`, `DATABASE_URL` from secret storage,
`MVP_PRODUCTION_SECURITY=1`, and a strong `MVP_AUTH_SECRET` or `SECRET_KEY` from
secret storage. Missing the exact runtime guard continues to block the PostgreSQL adapter.
PostgreSQL runtime must not be enabled unless every required environment fact and gate artifact
has been verified.

## Completed gates

1. The guarded runtime adapter is implemented and SQLite remains the default.
2. Custom-format backup and fresh-database restore rehearsal are verified.
3. Production security validates strong secrets and hardened authentication behavior.
4. Deployment rollback restores only into a fresh database and verifies the result.
5. Final approval, strict artifact validation, and guarded runtime smoke are implemented.

## Audit commands

```bash
python scripts/audit_postgres_production_gate.py --format json
python scripts/audit_postgres_production_gate.py --strict
```

Both commands are now expected to exit 0. A passing strict audit approves readiness for
runtime enablement; it is not a deployment and does not bypass the explicit environment guard.
Operators must follow `final_enablement_runbook.md` for cutover and rollback.
