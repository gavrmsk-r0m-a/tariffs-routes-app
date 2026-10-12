# Docker production deployment

TeleRoute can run as a containerized production stack without changing the
existing local-development workflow.

The local helper remains unchanged:

```text
docker-compose.postgres.yml
scripts/setup_local_postgres.py
scripts/run_local_postgres_app.py
```

The production stack is separate:

```text
docker-compose.production.yml
├── postgres   PostgreSQL 16 with a persistent named volume
├── db_migrate one-shot schema/migration job
└── app        TeleRoute on Python 3.12 + Waitress
```

## Production configuration

Copy `.env.production.example` to secret storage on the server and provide the
real values through the deployment platform or an external environment file.
Never commit a populated production environment file.

For the bundled PostgreSQL container, the following values must agree:

```dotenv
POSTGRES_DB=teleroute
POSTGRES_USER=teleroute
POSTGRES_PASSWORD=<strong-random-password>
DATABASE_URL=postgresql://teleroute:<same-password>@postgres:5432/teleroute
```

Also set a stable strong `MVP_AUTH_SECRET` and the real HTTPS
`APP_BASE_URL`.

The Compose stack forces the application container to listen on
`0.0.0.0:8000` inside its network, while publishing it only on host loopback:

```text
127.0.0.1:8000 -> app:8000
```

A host-level reverse proxy can therefore terminate HTTPS and proxy to
`127.0.0.1:8000` without exposing Waitress directly.

## Validate configuration

From the release checkout:

```bash
docker compose --env-file /etc/teleroute/teleroute.env \
  -f docker-compose.production.yml config
```

This must succeed before a deployment.

## First start

After the production environment is provisioned:

```bash
docker compose --env-file /etc/teleroute/teleroute.env \
  -f docker-compose.production.yml up -d --build
```

The `db_migrate` service waits for PostgreSQL, applies the canonical schema and
ordered SQL migrations, exits successfully, and only then allows `app` to
start. It does not create users or reset passwords.

For an existing production database, take and verify a logical backup before
running a new release. Schema/migration failure is a deployment stop condition.

## Persistence and secrets

- PostgreSQL data lives in the named volume `teleroute_postgres_data`.
- Application source code is immutable inside the image.
- No production secrets are baked into the image.
- No PostgreSQL port is published to the host by the production Compose file.
- The application runs as the unprivileged `teleroute` user.
- Logs are emitted to stdout/stderr for the container platform to collect.

## Upgrade

A normal code release is:

```bash
docker compose --env-file /etc/teleroute/teleroute.env \
  -f docker-compose.production.yml build app db_migrate

docker compose --env-file /etc/teleroute/teleroute.env \
  -f docker-compose.production.yml up -d
```

Run the post-deploy smoke checklist from
`docs/postgres/production_cutover_first_deploy.md` before reopening normal
traffic.

## Rollback

Keep the previously approved image/release available. If an application rollback
is needed, redeploy that image/release with the same external environment. If a
database rollback is required, use a verified logical backup and the existing
backup/restore runbook; never overwrite a failed database in place.

The GitLab deployment job should be added only after the production server,
runner/SSH path, registry policy, and secret variables are confirmed by the
administrator.
