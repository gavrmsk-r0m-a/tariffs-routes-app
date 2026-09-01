# PostgreSQL-only production filesystem layout

TeleRoute production deployments separate immutable application releases, external configuration, runtime files, backups, logs, and process state. The application runtime uses PostgreSQL only; there is no SQLite production runtime.

## Recommended layout

```text
/opt/teleroute/
  releases/
    <release>/
  current -> releases/<release>

/etc/teleroute/
  teleroute.env

/var/lib/teleroute/
  imports/
  exports/
  tmp/

/var/backups/teleroute/postgres/

/var/log/teleroute/

/run/teleroute/
```

## Directory responsibilities

- `/opt/teleroute/releases/<release>/` contains one versioned application checkout. It is read-only for the runtime account.
- `/opt/teleroute/current` is the atomic symlink to the active release. A deployment creates a new release rather than modifying the active checkout in place.
- `/etc/teleroute/teleroute.env` contains deployment configuration references and is readable only by the appropriate service account. Secrets and configuration are outside Git and outside every release.
- `/var/lib/teleroute/imports/` receives operator-approved input files; `/var/lib/teleroute/exports/` holds generated exports; `/var/lib/teleroute/tmp/` holds disposable runtime work. None belongs in the repository or release tree.
- `/var/backups/teleroute/postgres/` contains protected PostgreSQL backups and manifests. Apply appropriate retention, encryption, and access controls.
- `/var/log/teleroute/` contains application/service logs when the platform does not send them directly to centralized logging.
- `/run/teleroute/` contains ephemeral process state such as a Unix socket or PID file. The service manager recreates it at boot.

Create writable directories during provisioning and grant the runtime account only the permissions it needs. The application checkout and `current` release must remain non-writable.

## Database configuration and ownership

The application connects to PostgreSQL only through `DATABASE_URL`, supplied through the deployment secret/configuration mechanism:

```dotenv
DB_BACKEND=postgres
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE
```

Do not put a real connection URL in a release, repository, image layer, or deployment log. PostgreSQL's data directory is owned and managed by PostgreSQL itself or by the hosting provider. It is not `/var/lib/teleroute/`, and application deploys must never copy, edit, or back it up as ordinary application files.

Use the procedures in the [backup/restore runbook](../postgres/backup_restore_runbook.md). Backups remain outside the repository and are restored only into a fresh database for verification or an approved recovery. Deployment and database rollback are covered by the [deployment rollback runbook](../postgres/deployment_rollback_runbook.md).

## Deployment contract

1. Materialize a versioned release under `/opt/teleroute/releases/`.
2. Install/build release dependencies without writing runtime state into the checkout.
3. Validate externally managed configuration and `DATABASE_URL`.
4. Make the release read-only for the service account.
5. Atomically repoint `/opt/teleroute/current` and restart or reload the managed service.
6. Write imports, exports, temporary files, logs, and process state only to their designated external locations.
7. Retain the prior known-good release for the approved rollback procedure.

See the [PostgreSQL runtime contract](../postgres/runtime.md), [production readiness gate](../postgres/production_readiness_gate.md), and [security gate](../postgres/security_gate.md) for the remaining production requirements.

## Repository boundary

Do not commit secrets, `.env` files, databases, PostgreSQL data, dumps, backups, logs, imports, exports, temporary runtime files, or migration reports. Canonical schema and migration SQL under `docs/postgres/` are reviewed source files and remain tracked.
