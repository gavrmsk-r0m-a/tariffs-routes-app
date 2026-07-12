# Production-like filesystem layout

TeleRoute should keep application code separate from persistent runtime data. In production, and in production-like local setups, the SQLite database must not live inside the git working tree/app root.

## Why the SQLite database should not be in the app root

The app directory is updated by git/deploy operations. If the SQLite database, WAL files, backups, or logs are stored there, an application update can accidentally delete, overwrite, or package runtime data. Keeping data outside the repository makes the app replaceable while preserving operations data.

## Directory roles

- `app/` — application code and git repository. This directory can be updated or replaced during deploys.
- `data/` — persistent SQLite database and SQLite WAL/SHM files.
- `backups/` — database backups created before manual relocation or maintenance.
- `logs/` — future runtime log files.

## Recommended local production-like Windows layout

```text
C:\TeleRoute\app\
  app\
  docs\
  scripts\
  tests\
  README.md
  .env

C:\TeleRoute\data\
  mvp.sqlite3
  mvp.sqlite3-wal
  mvp.sqlite3-shm

C:\TeleRoute\backups\
  mvp.backup.YYYYMMDD-HHMMSS.sqlite3

C:\TeleRoute\logs\
  future log files
```

For this layout, configure:

```dotenv
SQLITE_DB_PATH=C:\TeleRoute\data\mvp.sqlite3
```

## Windows production example

```text
D:\TeleRoute\app\
D:\TeleRoute\data\
D:\TeleRoute\backups\
D:\TeleRoute\logs\
D:\TeleRoute\.env
```

Example database setting:

```dotenv
SQLITE_DB_PATH=D:\TeleRoute\data\mvp.sqlite3
```

## Linux production example

```text
/opt/teleroute/app/
/var/lib/teleroute/mvp.sqlite3
/var/backups/teleroute/
/var/log/teleroute/
/etc/teleroute/.env
```

Example database setting:

```dotenv
SQLITE_DB_PATH=/var/lib/teleroute/mvp.sqlite3
```

## SQLite path configuration

Runtime SQLite path priority is:

1. `SQLITE_DB_PATH` — recommended production setting; use an absolute path.
2. `MVP_DB_PATH` — legacy backward-compatible setting.
3. `APP_DATA_DIR/mvp.sqlite3` — convenient directory-based alternative.
4. `./mvp.sqlite3` in the app root — old development fallback only.

The app does not automatically move an existing database at startup. Configure the path after manually relocating the database.

## APP_DATA_DIR alternative

If you prefer to configure a directory instead of a full file path, set:

```dotenv
APP_DATA_DIR=C:\TeleRoute\data
```

The application will use:

```text
C:\TeleRoute\data\mvp.sqlite3
```

Use `SQLITE_DB_PATH` when you need a non-default filename or want the clearest production configuration.

## Relocating an existing SQLite database

Use the helper script to copy a database from the app root to the persistent data directory. The script uses the SQLite backup API, so committed WAL content is copied safely. It does not delete the source database.

Dry-run example:

```bash
python scripts/relocate_sqlite_db.py \
  --source ./mvp.sqlite3 \
  --target C:\TeleRoute\data\mvp.sqlite3 \
  --backup-dir C:\TeleRoute\backups \
  --dry-run
```

Apply example:

```bash
python scripts/relocate_sqlite_db.py \
  --source ./mvp.sqlite3 \
  --target C:\TeleRoute\data\mvp.sqlite3 \
  --backup-dir C:\TeleRoute\backups \
  --apply --yes
```

Apply mode creates the target parent directory and backup directory if needed, creates a timestamped backup first, copies to the target, and prints the `SQLITE_DB_PATH=...` line to add to `.env`. If the target already exists, the script refuses to overwrite it unless `--overwrite` is provided. The script does not edit `.env` automatically.

## Updating the application

When updating TeleRoute:

1. Update only the `app/` directory through git/deploy.
2. Do not delete or replace `data/`, `backups/`, or `logs/`.
3. Keep `.env` pointed at the persistent database path.
4. Verify the application starts against the configured database path.

## What must not be committed

Do not commit:

- `.env` with secrets or local paths.
- SQLite databases: `*.sqlite`, `*.sqlite3`, `*.db`.
- SQLite WAL/SHM files: `*.sqlite3-wal`, `*.sqlite3-shm`, `*.db-wal`, `*.db-shm`.
- Backups, dumps, migration reports, preflight reports, runtime logs, or local `data/` directories.

SQL schema files that are documentation or migrations, such as `docs/postgres/schema.postgres.sql`, remain valid repository files and must not be hidden by a broad `*.sql` ignore rule.

## Future PostgreSQL transition

When TeleRoute switches to PostgreSQL runtime:

- the SQLite file path will no longer be needed for runtime;
- configuration will use `DATABASE_URL`;
- PostgreSQL's data directory will be managed by PostgreSQL/hosting infrastructure, not by the app repository;
- `data/`, `backups/`, and `logs/` separation remains the same operational principle.
