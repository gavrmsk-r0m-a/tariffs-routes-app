# TeleRoute first-production-release runbook

Use a maintenance window and record the release commit, operators, timestamps, backup
artifacts, and results. Secrets belong in the service manager/secret store outside Git.
Never enable shell tracing while handling them.

## Pre-cutover

1. Freeze and pull the exact approved release commit; record `git rev-parse HEAD`. Run the
   complete test suite, PostgreSQL integration suite, UI baseline tests, and repository/runtime
   audits. A failure is a stop condition.
2. Create a pre-clean custom-format backup and manifest with `scripts/postgres_backup.py`.
   Independently verify its checksum and rehearse restoration into a fresh database with
   `scripts/postgres_restore_verify.py` and the existing backup/restore smoke tooling.
3. With `DB_BACKEND=postgres` and `DATABASE_URL` supplied by secret storage, preview cleanup:
   `python scripts/prepare_release_database.py`. Review every printed count. Dry run rolls back
   and changes nothing.
4. During the maintenance window, stop application writes and run:
   `python scripts/prepare_release_database.py --apply --confirm CLEAN_TEST_OPERATIONAL_DATA`.
   Review before/after counts and every preserved-table verification.
5. Immediately create and verify a second, post-clean backup. Retain both backups according to
   the operations retention policy.

## Server deployment

1. Install Docker Engine with the Compose plugin on the Linux host.
2. Configure values from `.env.production.example` in external secret storage. Database and
   authentication secrets must be strong, stable across restarts, and never committed.
3. Validate the release with
   `docker compose --env-file /etc/teleroute/teleroute.env -f docker-compose.production.yml config`.
4. Start the stack with
   `docker compose --env-file /etc/teleroute/teleroute.env -f docker-compose.production.yml up -d --build`.
   The one-shot `db_migrate` service waits for PostgreSQL 16, applies the canonical schema and
   ordered migrations, and must succeed before the application container starts.
5. Verify that the application is published only on host loopback
   (`127.0.0.1:8000` by default), then configure the reverse proxy to terminate HTTPS and proxy
   to that endpoint. Do not expose Waitress or PostgreSQL directly.
6. Follow `docs/deployment/docker_production.md` for upgrades and rollback. The legacy direct
   Python/systemd launcher remains available as a fallback but is not the primary container path.

## Administrator cutover

The existing `local-dev` bootstrap account is for local development, not a permanent production
administrator. Do not place its password in deployment files.

1. Enable production security and initially expose the site only to the deployment operator or
   internal network.
2. Sign in with the bootstrap administrator already present in the migrated database.
3. Create a named permanent administrator with a strong unique password.
4. In a separate session, verify that account can log in and administer users.
5. Disable `local-dev`; do not change user passwords as part of database cleanup.
6. Only after verification, expose the production HTTPS URL to normal users.

## Post-deploy smoke checklist

- [ ] `/health` and `/login` respond successfully.
- [ ] A permanent administrator and a normal operator can log in.
- [ ] Two simultaneous browser sessions remain independent.
- [ ] Routes, Tariffs, Purchased Numbers, and Calling Companies open.
- [ ] Provider Changes opens and its create modal works.
- [ ] Server priorities and company routing settings open.
- [ ] `/api/archivarius/v1/health` succeeds.
- [ ] An authenticated Archivarius request succeeds with a preserved API token.

Record actual results; do not claim browser behavior without testing it in two browsers/sessions.

## Rollback

Keep the previous approved application image/release and the verified pre-clean backup until
release acceptance. Never patch a running container or production data files manually.

1. Remove normal-user traffic and stop the TeleRoute application container.
2. Redeploy the recorded previous approved image/release with the same external configuration.
3. If database rollback is required, verify the pre-clean backup checksum, restore it into a
   **fresh** PostgreSQL database, and run restore verification. Never restore over the failed
   database.
4. Point `DATABASE_URL` at the verified restored database through secret storage, start the
   previous application image/release, and repeat health, login, data-page, and Archivarius smoke
   checks.
5. Reopen traffic only after the rollback owner signs off; preserve the failed database and logs
   for investigation without recording credentials.
