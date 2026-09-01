# TeleRoute

TeleRoute is an internal operations application for replacing spreadsheet-based
workflows around routes, tariffs, purchased phone numbers, calling campaigns,
provider changes, HLR checks, and administrative reference data.

## Runtime contract

TeleRoute has a PostgreSQL-only application runtime. Startup requires
`DB_BACKEND=postgres` and a non-empty `DATABASE_URL`; missing or unsupported
database configuration fails closed. There is no local-database fallback.

Old SQLite databases are supported only as input to explicit, offline migration
tools. See [`scripts/legacy/README.md`](scripts/legacy/README.md). Those tools are
not part of the application backend and are never a runtime mode.

## Main application areas

- Routes, route naming rules, and route-to-number assignments.
- Current tariffs and tariff conversion data.
- Purchased phone numbers and their lifecycle/history.
- Calling campaigns and routing settings.
- Provider-change and routing-event journals.
- CSV import preview/apply flows and CSV exports.
- HLR checks and usage controls.
- Administrative dictionaries, users, permissions, and technical change logs.

## Requirements

- Python 3.12 or newer.
- PostgreSQL 16 or a compatible supported PostgreSQL service.
- Python packages from `requirements.txt`.
- Docker with Docker Compose, optionally, for the local-development database only.
- PostgreSQL client tools such as `pg_dump` and `pg_restore` for backup and restore
  operations.

Install the Python dependencies in an isolated environment:

```bash
python -m venv .venv
python -m pip install -r requirements.txt
```

## Configuration

The required database settings are:

```dotenv
DB_BACKEND=postgres
DATABASE_URL=postgresql://<user>:<password>@<host>:5432/<database>
```

Use `.env.example` as the general application configuration reference and
`.env.postgres.local.example` for a local PostgreSQL setup. The application loads
a project-root `.env` when present without overriding variables already supplied
by the process environment. Examples contain placeholders or local-only values;
production values must come from the deployment platform's secret store.

Never commit a populated `.env` or resolved credentials.

## Local development

`docker-compose.postgres.yml` is a convenience for local development only. It
publishes PostgreSQL on the loopback interface, uses documented development
credentials, and stores data in a named local volume. It is not a production
deployment definition.

1. Start the local database and wait for its health check:

   ```bash
   docker compose -f docker-compose.postgres.yml up -d
   ```

2. Apply the canonical schema and migrations, and create/update the `local-dev`
   administrator. The generated login password is local-only.

   ```bash
   python scripts/setup_local_postgres.py \
     --database-url postgresql://postgres:postgres@localhost:5432/teleroute_local
   ```

3. Start the application, then open `http://127.0.0.1:8000/login`:

   ```bash
   python scripts/run_local_postgres_app.py \
     --database-url postgresql://postgres:postgres@localhost:5432/teleroute_local
   ```

4. Optionally run the authenticated local smoke check with the login password from
   setup:

   ```bash
   python scripts/local_postgres_app_smoke.py \
     --database-url postgresql://postgres:postgres@localhost:5432/teleroute_local \
     --password 'the-local-login-password'
   ```

Stop the local database without removing its named volume:

```bash
docker compose -f docker-compose.postgres.yml down
```

The setup and run helpers reject non-loopback database hosts so they cannot be
used accidentally against a remote or production database.

## Authentication and security

Users authenticate with credentials stored in PostgreSQL. Signed authentication
cookies identify the current user, and section-level permissions control read,
write, and export access. Production deployments must enable
`MVP_PRODUCTION_SECURITY=1` and provide a strong `MVP_AUTH_SECRET` (or
`SECRET_KEY`) through secret storage. Production security mode requires a secret
of at least 32 characters, uses secure cookie attributes, rejects known default
credentials, and enables login throttling.

Do not reuse the local-development administrator password, database credentials,
or example authentication secret in production.

## Schema and migrations

The canonical PostgreSQL schema is
[`docs/postgres/schema.postgres.sql`](docs/postgres/schema.postgres.sql). Ordered
SQL migrations live in [`docs/postgres/migrations/`](docs/postgres/migrations/).
Deployment tooling must apply the schema and migrations before starting a new
application release; the application runtime does not create or migrate its own
schema. The current runtime contract is documented in
[`docs/postgres/runtime.md`](docs/postgres/runtime.md).

## Tests

Run the complete test suite from the repository root:

```bash
python -m unittest discover -s tests
```

PostgreSQL integration tests require an available disposable PostgreSQL test
service and the environment described by the test support tooling. The focused
repository-layout contract can be run independently:

```bash
python -m unittest tests.test_filesystem_layout
```

## Backup and restore

Use `scripts/postgres_backup.py` for custom-format PostgreSQL backups and
`scripts/postgres_restore_verify.py` to verify a restore into a fresh database.
Never restore unverified data directly over production. Store backup files and
manifests outside the repository, verify their checksums and table counts, and
rehearse restore before a production cutover. Follow the
[`PostgreSQL backup and restore runbook`](docs/postgres/backup_restore_runbook.md).

## Production filesystem and deployment

Application releases are immutable, while configuration, runtime files, logs, and
backups live outside the checkout. See the
[`PostgreSQL-only production filesystem layout`](docs/deployment/filesystem_layout.md)
for the directory contract and ownership boundaries.

Before deployment, make sure the hosting platform provides `DATABASE_URL` and all
authentication secrets, the schema is current, backup/restore has been rehearsed,
and the release can be rolled back without editing the repository in place.

## Repository hygiene

Do not commit any of the following:

- Secrets or populated environment files.
- Local database files or PostgreSQL data directories.
- Database dumps, backups, or restore artifacts.
- Logs, coverage output, caches, build output, or editor metadata.
- Runtime imports, exports, temporary files, or generated reports.

Canonical schema files, migrations, test fixtures, and intentionally versioned CSV
fixtures remain tracked; do not hide them with broad ignore rules.
