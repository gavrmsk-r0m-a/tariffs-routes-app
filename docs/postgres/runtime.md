# PostgreSQL-only application runtime

TeleRoute's normal application runtime requires both `DB_BACKEND=postgres` and a
non-empty `DATABASE_URL`. `postgresql` remains an accepted alias and is normalized
to `postgres`. Missing values, unsupported backends, driver errors, invalid URLs,
and connection failures stop startup; there is no local database fallback.

The canonical schema remains `docs/postgres/schema.postgres.sql` and must be
provisioned by deployment tooling before the application starts. Runtime does not
create, migrate, or seed database schemas.

Utilities that read an old SQLite file under `scripts/` are retained solely for a
manual legacy SQLite-to-PostgreSQL migration. They are not application backends.
`POSTGRES_RUNTIME_ENABLED` has been removed; PostgreSQL is no longer gated.
