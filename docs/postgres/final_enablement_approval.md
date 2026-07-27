# PostgreSQL Runtime Enablement Approval

`APPROVED_FOR_POSTGRES_RUNTIME_ENABLEMENT`

- This approval is for runtime enablement readiness, not a production deployment.
- No production credentials are stored in this repository.
- Actual production deployment requires operator/hosting configuration; it is not performed by this PR.
- Production runtime must set `DB_BACKEND=postgres`.
- Production runtime must set `POSTGRES_RUNTIME_ENABLED=1`.
- Production runtime must set `DATABASE_URL` from secret storage.
- Production runtime must set `MVP_PRODUCTION_SECURITY=1`.
- Production runtime must use a strong `MVP_AUTH_SECRET` or `SECRET_KEY` from secret storage.
- SQLite must not be used as the initial production database.
- Backup/restore, security, and deployment rollback gates are completed.
- Final production cutover must run the final enablement check and smoke checks.

This artifact approves the readiness gate only. It does not authorize credentials to be
committed, alter the local SQLite default, or make a production deployment.
