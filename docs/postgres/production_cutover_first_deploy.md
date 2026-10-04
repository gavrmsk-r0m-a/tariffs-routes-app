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

1. Install Python 3.12 or newer, create a virtual environment, activate it, and run
   `python -m pip install -r requirements.txt`.
2. Configure variables based on `.env.production.example` in external secret storage. The auth
   secret must be random, at least 32 characters, and stable across restarts and releases.
3. Apply the canonical schema and all migrations explicitly **before** startup using the
   established migration procedure. The launcher never initializes or migrates the database.
4. Start `python scripts/run_production.py` under a process supervisor (for example systemd),
   with restart policy and boot-time auto-start. It uses Waitress and defaults to
   `127.0.0.1:8000`.
5. Configure the reverse proxy to terminate HTTPS and proxy only to `127.0.0.1:8000`. Do not
   expose Waitress directly to the Internet.

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

Keep the previous immutable application release and the verified pre-clean backup until release
acceptance. Never patch the active release directory manually.

1. Remove normal-user traffic and stop the TeleRoute service.
2. Switch the service symlink/configuration to the recorded previous release (do not edit either
   release directory).
3. If database rollback is required, verify the pre-clean backup checksum, restore it into a
   **fresh** PostgreSQL database, and run restore verification. Never restore over the failed
   database.
4. Point `DATABASE_URL` at the verified restored database through secret storage, restart the
   previous release, and repeat health, login, data-page, and Archivarius smoke checks.
5. Reopen traffic only after the rollback owner signs off; preserve the failed database and logs
   for investigation without recording credentials.
