# Stage 13 — PostgreSQL runtime compatibility audit

## Executive summary

The application runtime is **not PostgreSQL-ready**. Stage 12 proved the offline SQLite-to-PostgreSQL migration path in CI, but request-time code still assumes SQLite connection, cursor, row, placeholder, DDL, error, and transaction behavior.

What is already safe:

- `DB_BACKEND=postgres` / `postgresql` is explicitly rejected by `connect_database()` with `POSTGRES_NOT_IMPLEMENTED_MESSAGE`; this is the correct production guard for Stage 13.
- A PostgreSQL schema draft and migration tooling already exist outside the runtime path.
- Repository centralization exists for much business logic, so future adapter work can be introduced incrementally.

Primary blockers before `DB_BACKEND=postgres` can run:

1. `app/db.py` creates `sqlite3.Connection`, applies SQLite PRAGMAs, sets `sqlite3.Row`, executes `app/schema.sql`, and performs SQLite-specific lightweight migrations at startup.
2. `app/repository.py`, `app/server.py`, and `app/importer.py` contain extensive `?` placeholders and direct `repo.conn.execute(...)` calls.
3. Insert methods depend on `cursor.lastrowid`; PostgreSQL needs `RETURNING id` or an adapter helper.
4. Runtime error handling is typed to `sqlite3.IntegrityError` / `sqlite3.OperationalError` and parses SQLite error text.
5. Runtime DDL and schema inspection use `sqlite_master`, `PRAGMA table_info`, `AUTOINCREMENT`, `GLOB`, partial-index syntax, and SQLite boolean-as-integer assumptions.

Static helper baseline from `python scripts/audit_postgres_runtime_compat.py --format json` on Stage 13 found **1524 known-pattern findings**: `placeholders=893`, `sqlite3_api=334`, `ddl_runtime=208`, `sqlite_sql=89`. The helper is intentionally broad; it is an audit aid, not a gating linter.

## SQLite-specific dependencies

| File | Line / function | Category | Description | Risk | Suggested fix | Stage |
| --- | --- | --- | --- | --- | --- | --- |
| `app/db.py` | `import sqlite3`, `connect_database()`, `connect()` | sqlite3 API | Runtime connection type is `sqlite3.Connection`; PostgreSQL backend is currently blocked intentionally. | blocker | Add backend-neutral adapter/factory while keeping PostgreSQL disabled until smoke tests exist. | 14, 19 |
| `app/db.py` | `apply_connection_pragmas()` | sqlite3 API / concurrency | Uses `PRAGMA journal_mode=WAL`, `busy_timeout`, and `foreign_keys`. | blocker | Move per-backend connection setup into adapter; PostgreSQL should use server/session settings only if needed. | 14 |
| `app/db.py` | `connect()` | sqlite3 API / row shape | Sets `conn.row_factory = sqlite3.Row`; many templates and repository methods use dict-like row access. | blocker | Normalize rows through adapter (`MappingRow`/dict-like protocol) for both backends. | 14 |
| `app/db.py` | `_has_schema()`, `run_lightweight_migrations()` | DDL/schema inspection | Checks `sqlite_master` and mutates schema during initialization. | high | Split SQLite bootstrap/migrations from future PostgreSQL startup; PostgreSQL should not run SQLite bootstrap SQL. | 14, 20 |
| `app/db.py` | `_column_names()` | DDL/schema inspection | Uses `PRAGMA table_info({table})`. | high | Add adapter schema-inspection helper or remove runtime inspection from PostgreSQL path. | 14 |
| `app/db.py` | `_add_column_if_missing()` | runtime DDL | Executes `ALTER TABLE ... ADD COLUMN` at startup. | high | Keep SQLite-only compatibility migration; PostgreSQL runtime should rely on managed schema. | 14, 20 |
| `app/db.py` | `_rebuild_phone_numbers_if_needed()` | SQLite SQL / DDL | Rebuilds table with `AUTOINCREMENT`, `GLOB`, `PRAGMA foreign_keys=OFF/ON`, `DROP TABLE`, `ALTER TABLE RENAME`. | blocker | Make this SQLite-only; never run this rewrite on PostgreSQL. | 14 |
| `app/db.py` | `_seed_default_users_if_empty()` | placeholders | Dynamically generates `?` placeholders. | medium | Use adapter placeholder renderer. | 14 |
| `app/db.py` | `run_lightweight_migrations()` | SQLite SQL | `INTEGER PRIMARY KEY AUTOINCREMENT`, `TEXT` timestamps, integer booleans, partial index, `CURRENT_TIMESTAMP`. | high | Keep as SQLite migration path; PostgreSQL uses schema draft and future migrations. | 14, 20 |
| `app/db.py` | `init_db()` | sqlite3 API / DDL | Executes `app/schema.sql` through `conn.executescript()`. | blocker | Adapter needs backend-specific schema bootstrap; PostgreSQL runtime should not execute SQLite schema. | 14 |
| `app/schema.sql` | whole file | SQLite schema | Contains `PRAGMA`, `AUTOINCREMENT`, `GLOB`, integer booleans, `TEXT` timestamps. | blocker for PostgreSQL runtime | Keep as SQLite schema only; use `docs/postgres/schema.postgres.sql` or later managed PostgreSQL DDL. | 14, 20 |
| `app/repository.py` | `import sqlite3`, `Repository.__init__` | sqlite3 API | Repository constructor requires `sqlite3.Connection`. | blocker | Type to adapter protocol instead of concrete sqlite3 connection. | 14 |
| `app/repository.py` | `Repository.transaction()` | transaction behavior | Manual `commit()`/`rollback()` on SQLite connection. | high | Provide adapter transaction context with consistent rollback and exception semantics. | 16 |
| `app/repository.py` | `_user_columns()` | DDL/schema inspection | Uses `PRAGMA table_info(users)`. | medium | Replace with adapter schema helper or make SQLite-only startup concern. | 14 |
| `app/repository.py` | many read/write methods | placeholders | Uses `?` in selects, updates, inserts, dynamic WHERE clauses and `IN (...)`. | blocker | Route all execution through placeholder adapter (`?` for SQLite, `%s` for PostgreSQL). | 15-16 |
| `app/repository.py` | create methods | return values | Multiple insert methods return `int(cur.lastrowid)`. | blocker | Add `insert_returning_id()` helper; SQLite reads `lastrowid`, PostgreSQL appends/uses `RETURNING id`. | 16 |
| `app/repository.py` | queries with `is_active = 1`, booleans | value semantics | Booleans are stored and compared as `0/1`. | medium | Adapter/coercion layer should normalize bool values; PostgreSQL SQL should use boolean columns or explicit casts per schema. | 15-16 |
| `app/importer.py` | `import sqlite3`, annotations | sqlite3 API | Importer accepts raw `sqlite3.Connection`. | high | Type to adapter/Repository protocol. | 17 |
| `app/importer.py` | `_resolve_reference()`, `_exists()`, import methods | direct SQL outside Repository | Importer uses raw SQL for lookups, updates, inserts, `INSERT OR IGNORE`, and commits. | high | Move persistence into Repository or adapter-backed gateway; keep CSV parsing unchanged. | 17 |
| `app/importer.py` | `_ensure_dictionary_value()` | SQLite SQL | Uses `INSERT OR IGNORE`. | medium | Replace with backend-neutral upsert helper. | 17 |
| `app/importer.py` | import methods | transaction behavior | Calls `repo.conn.commit()` inside import steps. | high | Use Repository/adapter transaction boundary for all import modes. | 16-17 |
| `app/server.py` | `import sqlite3`, row annotations | sqlite3 API / row shape | Presentation helpers annotate and consume `sqlite3.Row`. | medium | Type to mapping protocol or normalized row object. | 15 |
| `app/server.py` | route handlers and form handlers | direct SQL outside Repository | Many handlers call `repo.conn.execute()` directly for lookups, dictionary edits, route-number management, routing events, HLR/admin settings. | high | Gradually move mutations and reusable reads into Repository or adapter helpers. | 17 |
| `app/server.py` | exception handlers | error handling | Catches `sqlite3.IntegrityError` in UI flows. | high | Catch backend-neutral `DatabaseIntegrityError` or use `map_database_error()` wrapper for both backends. | 14, 17 |
| `app/db_errors.py` | whole module | error handling | Maps `sqlite3.IntegrityError`/`OperationalError` and SQLite error messages. | high | Add backend-neutral error classification accepting SQLite and future psycopg errors without runtime psycopg import in Stage 13. | 14 |
| `scripts/postgres_preflight.py` | tooling | sqlite3 API | Intentionally reads SQLite source DB and uses PRAGMAs. | low | Keep as migration tooling, not runtime adapter target. | done/tooling |
| `scripts/migrate_sqlite_to_postgres.py` | tooling | sqlite3 + psycopg | Intentionally bridges SQLite source to PostgreSQL destination. | low | Keep outside runtime; no app backend behavior. | done/tooling |
| `scripts/create_migration_demo_sqlite.py` | tooling | sqlite3 / lastrowid | Intentionally creates demo SQLite DB for CI migration smoke. | low | Keep outside runtime. | done/tooling |
| `tests/*.py` | tests | sqlite3 API | Tests create SQLite fixtures and assert SQLite migration behavior. | low | Keep SQLite tests; add PostgreSQL runtime smoke only in later stages. | 18 |

## Direct SQL map

### Repository SQL

`app/repository.py` owns the majority of runtime business SQL. It includes:

- entity listing, search, filters, and pagination using `?` placeholders;
- CRUD for countries, providers, routes, tariffs, phone numbers, route assignments, servers, companies, provider changes, routing events, users, permissions, app settings, HLR usage, and audit/change logs;
- transaction context via `Repository.transaction()`;
- insert methods using `lastrowid`.

Repository SQL should be the first large compatibility target because it is already close to a single data-access boundary. Do not rewrite all methods at once; Stage 15 should start with a small read-only batch.

### Direct SQL outside Repository

`app/server.py` has substantial direct SQL in request handlers and rendering helpers. Examples include route-number helper lookups, provider-change forms, dictionary CRUD, telegram settings, naming rules, admin updates, and routing-event detail rendering. These calls often rely on `sqlite3.Row`, `?`, integer booleans, `CURRENT_TIMESTAMP`, and explicit `repo.conn.commit()`.

`app/importer.py` also has direct SQL for reference resolution, existence checks, updates, inserts, dictionary seeding, and section clearing. This is runtime code and should be migrated after the adapter and core Repository paths exist.

Tooling direct SQL in `scripts/postgres_preflight.py`, `scripts/migrate_sqlite_to_postgres.py`, and `scripts/create_migration_demo_sqlite.py` is expected and should remain isolated from runtime backend work.

## Placeholder map

Static audit found **893 `?` placeholder pattern hits** across Python/SQL files. Runtime categories:

- `app/repository.py`: dominant source. Includes simple predicates, dynamic search clauses, `IN (?, ?, ...)` generation, `LIMIT/OFFSET` values, update/insert statements, and nested lookup helpers.
- `app/server.py`: many direct `repo.conn.execute()` calls use `?`, plus dynamic `IN` placeholder generation for route/overflow/history flows.
- `app/importer.py`: lookup, update, insert, `INSERT OR IGNORE`, and cleanup statements use `?`.
- `app/db.py`: seed/default data inserts and startup migration updates use `?`.

Adapter requirement:

- Define one SQL execution boundary that accepts SQLite-style logical placeholders or a structured SQL object and renders backend placeholders (`?` for SQLite, `%s` for PostgreSQL).
- Treat dynamic `IN` lists as first-class helper (`placeholders(count)` or `where_in(column, values)`) to avoid ad-hoc string joins.
- Keep Stage 14 purely infrastructural; do not mass-edit business SQL until Stage 15+.

## `lastrowid` / insert-returning map

Runtime `lastrowid` usage is concentrated in Repository insert/create methods and must be abstracted before PostgreSQL runtime mode can insert data.

Required behavior:

- SQLite: execute insert, read `cursor.lastrowid`.
- PostgreSQL: execute insert with `RETURNING id` and read `fetchone()[0]`.
- Repository callers should receive `int` IDs exactly as today.

Tooling `lastrowid` in `scripts/create_migration_demo_sqlite.py` is not runtime and can remain unchanged.

## DDL / runtime schema mutation map

Runtime startup currently performs SQLite schema work:

- `connect_database()` routes only SQLite to `connect()` and rejects PostgreSQL.
- `connect()` applies SQLite connection PRAGMAs and `sqlite3.Row`.
- `ensure_db_initialized()` calls `init_db()` once per process/path.
- `init_db()` runs `app/schema.sql` via `executescript()`, then `run_lightweight_migrations()`.
- `run_lightweight_migrations()` creates tables/indexes, adds columns, updates seed/default rows, and rebuilds `phone_numbers` if old constraints are detected.

PostgreSQL recommendation:

- Do not run SQLite `app/schema.sql` or lightweight migrations for PostgreSQL.
- Stage 14 adapter should preserve current SQLite path and create a hard fail or no-op PostgreSQL runtime bootstrap until Stage 19 experimental mode.
- Stage 20 should define production migration readiness and schema ownership (manual SQL, future migration tool, or controlled bootstrap), but not in Stage 13.

## Transaction map

Observed transaction patterns:

- `Repository.transaction()` wraps manual commit/rollback.
- Many Repository methods accept `commit=True` or perform explicit `self.conn.commit()`.
- `app/importer.py` calls `repo.conn.commit()` after multi-step operations.
- `app/server.py` direct mutations often call `repo.conn.commit()` inline.
- Startup migrations in `app/db.py` call `conn.commit()` and temporarily disable SQLite foreign keys during table rebuild.

PostgreSQL differences:

- Transaction state and autocommit defaults differ by driver.
- DDL rollback behavior and lock scopes differ from SQLite.
- PostgreSQL has row-level locking and MVCC; SQLite has database/write-lock behavior and busy timeout.

Adapter requirement:

- Provide `transaction()` on the adapter and make Repository use it.
- Keep manual commits working for SQLite until code is migrated, but forbid mixed commit strategies in converted methods.

## Error handling map

Current coverage:

- `app/db_errors.py` maps common SQLite `IntegrityError` messages for unique, foreign-key, not-null, check, and lock errors.
- `app/server.py` uses `user_error()` in some UI flows.

Gaps:

- Error classification relies on `isinstance(exc, sqlite3.IntegrityError)` and SQLite message text.
- Some handlers catch `sqlite3.IntegrityError` directly, so PostgreSQL exceptions would bypass existing UI messages.
- Lock/timeout handling is SQLite-specific (`database is locked`, busy timeout).

Adapter requirement:

- Introduce backend-neutral exception classification (`DatabaseErrorInfo`) that accepts raw driver exceptions.
- Avoid importing psycopg in runtime until PostgreSQL experimental stage; use duck-typing/message extraction or optional adapter module when later enabled.

## Return value and row-shape map

- `sqlite3.Row` supports dict-like key access and index access. Server templates and Repository helpers use both.
- `fetchone()` returns `None` or row; PostgreSQL driver rows need a configured row factory or adapter normalization.
- `rowcount` is not prominent in runtime, but any future adapter should not rely on identical semantics across drivers.
- Booleans are currently stored and rendered as `0/1`; PostgreSQL schema draft uses booleans in several places, so row normalization or SQL conversion must be explicit.
- Timestamps are mostly `TEXT`/`CURRENT_TIMESTAMP` in SQLite; PostgreSQL returns timestamp/date objects unless coerced.

## Concurrency / locking map

SQLite-specific behavior in runtime:

- Connection timeout and `PRAGMA busy_timeout` manage database-level write contention.
- WAL mode is enabled at connect time.
- Some flows rely on quick inline writes followed by immediate commit.
- Optimistic update tokens based on `updated_at` are text/timestamp-sensitive and may behave differently if PostgreSQL returns typed timestamps or higher precision.

PostgreSQL plan:

- Do not emulate SQLite locks.
- Use adapter transaction boundaries and connection pooling/session management in later stages.
- Review any `updated_at` comparison or display token before enabling writes in PostgreSQL runtime.

## Risk classification

### Blocker

- SQLite-only connection/bootstrap path in `app/db.py`.
- SQLite `app/schema.sql` and `executescript()` used for runtime initialization.
- Repository and direct runtime SQL use `?` placeholders everywhere.
- `lastrowid` insert ID behavior.
- SQLite row shape (`sqlite3.Row`) assumed across Repository/server/importer.

### High

- Runtime DDL/lightweight migrations at startup.
- Direct SQL outside Repository in `app/server.py` and `app/importer.py`.
- SQLite exception classes and message parsing.
- Manual commit/rollback spread across Repository, server, importer, and startup.

### Medium

- Integer boolean assumptions (`0/1`) and `TEXT` timestamp assumptions.
- SQLite-specific SQL conveniences (`INSERT OR IGNORE`, `GLOB`, `sqlite_master`, `PRAGMA table_info`).
- Dynamic `IN` placeholder generation.

### Low

- Migration/preflight/demo scripts intentionally use SQLite and PostgreSQL tooling APIs.
- Tests that create SQLite fixtures are expected to remain SQLite-focused until Stage 18.

## Migration recommendation

Do **not** enable PostgreSQL runtime in this PR. Keep `DB_BACKEND=postgres` blocked. Proceed through a small adapter-first sequence:

1. Stage 14: adapter foundation only, no business behavior changes.
2. Stage 15: convert a small read-only Repository slice through adapter helpers.
3. Stage 16: convert insert/update paths and `lastrowid`.
4. Stage 17: reduce direct SQL in `server.py` and `importer.py`.
5. Stage 18+: add CI PostgreSQL app-level smoke before any experimental runtime switch.

