# TeleRoute

TeleRoute is an internal operations application that replaces spreadsheet-based workflows for routes, tariffs, purchased phone numbers, provider changes, calling companies, HLR checks, and administrative reference data. It is deliberately a focused operations tool, not a CRM or ERP.

## Runtime and features

TeleRoute uses **PostgreSQL exclusively at application runtime**. `DB_BACKEND=postgres` selects the supported backend and `DATABASE_URL` is required; startup fails closed when the database configuration is absent or invalid.

The web interface provides:

- routes and their assigned phone numbers;
- tariffs and currency conversion data;
- purchased phone pools;
- calling companies/campaigns;
- provider-change history and server priorities;
- HLR operations;
- CSV import preview/apply workflows;
- administrative dictionaries, naming rules, change reasons, users, and audit data.

## Requirements

- Python 3.12 or newer;
- PostgreSQL 16 or a compatible hosted PostgreSQL service;
- Python dependencies from `requirements.txt`;
- Docker with Compose, optionally, for the local-development database only.

Install the Python dependencies in a virtual environment:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

## Configuration

Copy `.env.example` when preparing an environment. It documents the application and integration settings without containing usable production credentials. `.env.postgres.local.example` is a local-development example for the bundled PostgreSQL workflow. Never commit a populated `.env` file.

The runtime database connection is supplied only through:

```dotenv
DB_BACKEND=postgres
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE
```

Supply real credentials through the deployment platform's secret store. Production authentication also requires `MVP_PRODUCTION_SECURITY=1` and a strong, secret `MVP_AUTH_SECRET` (or `SECRET_KEY`); see the [security gate](docs/postgres/security_gate.md). Do not reuse example or development values.

## Local development

`docker-compose.postgres.yml` runs a PostgreSQL container **for local development only**. Its documented credentials are not suitable for production.

1. Start the local database:

   ```bash
   docker compose -f docker-compose.postgres.yml up -d
   ```

2. Initialize the schema and create the idempotent `local-dev` administrator. The command generates a local login password unless `--password` is supplied:

   ```bash
   python scripts/setup_local_postgres.py \
     --database-url postgresql://postgres:postgres@localhost:5432/teleroute_local
   ```

3. Start the application and open `http://127.0.0.1:8000/login`:

   ```bash
   python scripts/run_local_postgres_app.py \
     --database-url postgresql://postgres:postgres@localhost:5432/teleroute_local
   ```

4. Optionally run the authenticated local smoke check:

   ```bash
   python scripts/local_postgres_app_smoke.py \
     --database-url postgresql://postgres:postgres@localhost:5432/teleroute_local \
     --password 'your-local-login-password'
   ```

The local setup and runner accept only loopback database hosts. Stop the container with `docker compose -f docker-compose.postgres.yml down`; add `-v` only when intentionally deleting the local database volume.

## Authentication and security

TeleRoute uses application users and signed authentication cookies. Production mode fails fast unless a sufficiently strong authentication secret is provided. Database credentials, authentication secrets, HLR credentials, and other integration secrets belong in external secret/config management, never in Git or command output. Deploy over HTTPS with the controls described in the [security gate](docs/postgres/security_gate.md).

## Schema and migrations

The canonical PostgreSQL schema is [`docs/postgres/schema.postgres.sql`](docs/postgres/schema.postgres.sql). Incremental SQL migrations are tracked in [`docs/postgres/migrations/`](docs/postgres/migrations/). These SQL files are source artifacts and intentionally are not ignored by Git.

## Tests

Run the complete unit-test suite:

```bash
python -m unittest discover -s tests
```

PostgreSQL integration suites require a disposable test database; the hosted migration-smoke workflow documents the CI setup and commands.

## Backup and restore

Use `scripts/postgres_backup.py` for a custom-format PostgreSQL backup plus manifest, and verify restoration into a **fresh, non-production database** with `scripts/postgres_restore_verify.py`. Never restore over the live database. Follow the [backup/restore runbook](docs/postgres/backup_restore_runbook.md) and keep backup files and manifests outside the repository, such as `/var/backups/teleroute/postgres/`.

## Legacy migration tools

SQLite is supported only as an offline source format for manual migration into PostgreSQL. `scripts/legacy/` documents this boundary; migration-related scripts and tests are not an application backend and must never be imported by the runtime. Source databases and generated migration reports must remain outside the repository.

## Production and deployment documentation

- [PostgreSQL runtime contract](docs/postgres/runtime.md)
- [Production filesystem layout](docs/deployment/filesystem_layout.md)
- [Production readiness gate](docs/postgres/production_readiness_gate.md)
- [Security gate](docs/postgres/security_gate.md)
- [Backup and restore runbook](docs/postgres/backup_restore_runbook.md)
- [Deployment and rollback runbook](docs/postgres/deployment_rollback_runbook.md)

## Repository hygiene

Never commit secrets, populated `.env` files, local databases, PostgreSQL data directories, database dumps or backups, logs, runtime imports/exports, temporary reports, or generated coverage/build output. CSV files and test fixtures are not globally ignored because reviewed fixtures may be legitimate source artifacts; verify every such file before committing it.
