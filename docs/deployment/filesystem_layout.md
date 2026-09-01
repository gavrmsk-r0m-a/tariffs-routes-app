# PostgreSQL-only production filesystem layout

TeleRoute production deployments keep immutable application releases separate
from configuration, transient runtime files, logs, and PostgreSQL backups. The
application checkout must be replaceable without moving or deleting operational
data.

SQLite is not a production runtime. Legacy SQLite source data may be handled only
by explicit offline migration tooling and must remain outside application
releases and the Git repository.

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

## Directory contract

| Path | Role | Runtime access |
| --- | --- | --- |
| `/opt/teleroute/releases/<release>/` | Versioned application code and installed release assets. | Read-only. The service must not write into a release. |
| `/opt/teleroute/current` | Symlink to the active release. | Read-only; changed only by the deployment mechanism. |
| `/etc/teleroute/teleroute.env` | Environment configuration and secret references. | Readable only by the service identity and authorized operators. Never stored in Git or copied into a release. |
| `/var/lib/teleroute/imports/` | Operator-supplied import input staged for processing. | Read/write only as required by the import workflow. |
| `/var/lib/teleroute/exports/` | Generated exports awaiting delivery or retention cleanup. | Read/write by the application or an approved export worker. |
| `/var/lib/teleroute/tmp/` | Short-lived application work files. | Read/write; contents must be safe to clear while the service is stopped. |
| `/var/backups/teleroute/postgres/` | PostgreSQL backups, manifests, and restore-verification artifacts. | Written by the approved backup job, not by normal web requests. |
| `/var/log/teleroute/` | Application logs when the platform does not capture standard output/error. | Append/write by the service; read by operators or log shipping. |
| `/run/teleroute/` | Ephemeral PID, socket, or process-manager state. | Recreated at boot; never used for persistent data. |

## Application release boundary

Each deployment creates a new directory under `/opt/teleroute/releases/`. The
release contains application code, versioned documentation, and versioned schema
artifacts only. It is owned by the deployment identity and is read-only to the
TeleRoute service user. Promote a verified release by updating
`/opt/teleroute/current`; do not patch files inside the active release.

Rollback selects a previously verified release and restarts the service with the
same external configuration. Runtime imports, exports, logs, temporary files, and
backups therefore survive release replacement.

## Configuration and secrets

Production configuration belongs in `/etc/teleroute/teleroute.env` or the hosting
platform's equivalent secret/environment store. It must never be committed,
embedded in a release, printed to deployment logs, or copied into a backup
manifest.

The application database contract requires `DB_BACKEND=postgres` and connects to
PostgreSQL through `DATABASE_URL`. The resolved URL is a secret because it can
contain credentials. Authentication secrets are managed through the same external
configuration boundary.

Restrict the environment file to the service identity and authorized operators.
Prefer a secret manager that injects values at process start when the hosting
platform supports one.

## PostgreSQL data and backups

The PostgreSQL data directory is managed by PostgreSQL itself or by the hosting
provider. It is not part of `/opt/teleroute`, `/var/lib/teleroute`, or the Git
repository. The application service must not manage or copy PostgreSQL internal
data files.

Write logical backups and their manifests to
`/var/backups/teleroute/postgres/` or an external managed backup destination.
Protect them as sensitive operational data, apply an explicit retention policy,
and copy them off-host where required. Restore verification must use a fresh
database and follow
[`docs/postgres/backup_restore_runbook.md`](../postgres/backup_restore_runbook.md).

## Imports, exports, temporary files, and logs

Imports, exports, and temporary runtime files belong under
`/var/lib/teleroute/`, never inside the current release. Apply retention and
cleanup rules appropriate to their data sensitivity. Source files used for a
one-time legacy migration follow the same outside-repository rule.

Prefer the hosting platform's standard output/error collection for logs. If file
logging is required, write only under `/var/log/teleroute/`, configure rotation,
and prevent credentials or sensitive row contents from being logged.

## Deployment checklist

1. Create a new immutable release under `/opt/teleroute/releases/<release>/`.
2. Verify that the service identity cannot write to the release directory.
3. Provision configuration and secrets outside the release.
4. Verify the PostgreSQL schema/migrations and tested backup/restore evidence.
5. Ensure runtime data, backup, log, and run directories exist with least-privilege
   ownership and permissions.
6. Update `/opt/teleroute/current`, restart the managed service, and run health and
   application smoke checks.
7. Keep the previous verified release available for rollback.

No production database, dump, backup, secret, import, export, temporary file, or
log belongs in the Git repository or an application release.
