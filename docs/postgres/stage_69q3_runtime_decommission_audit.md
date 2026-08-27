# Stage 69Q3: final PostgreSQL-only runtime audit

Date: 2026-08-27

## Conclusion

**READY FOR STAGE 70A.** No reachable PostgreSQL application operation was found
that depends on SQLite syntax, SQLite schema initialization, SQLite row types, or
an automatic fallback to SQLite. No runtime compatibility defect was found, so
69Q3 intentionally makes no application-code change.

This conclusion means the SQLite runtime can be removed in 70A by following the
map below. It does not mean SQLite has already been removed. In particular, the
current default remains SQLite and the transition tools intentionally remain.

## Method and search evidence

The audit inspected every `app/*.py` module, `app/schema.sql`, normal startup and
deployment scripts, readiness/security audits, migration scripts, and all
`Repository(...)` call sites. Repository-wide, case-insensitive searches covered
`sqlite3`, `sqlite`, `PRAGMA`, `sqlite_master`, `sqlite_sequence`, `INSERT OR
IGNORE`, `INSERT OR REPLACE`, `REPLACE INTO`, `lastrowid`, `DB_BACKEND`,
`mvp.sqlite3`, `schema.sql`, `AUTOINCREMENT`, SQLite date functions,
`GROUP_CONCAT`, `COLLATE NOCASE`, and `rowid`.

The raw-placeholder review examined executable `execute`/`executemany` call
sites rather than counting `?`. URL query strings, UI text, regular expressions,
CSV values such as `???`, and test business data are not SQL findings. Runtime
SQL either uses `placeholder(repo.backend)`/adapter builders, or has an explicit
PostgreSQL branch. Raw qmark SQL in `ensure_seed` is reachable only inside the
SQLite-only startup branch.

The static compatibility scanner still reports expected SQLite runtime and test
code. Its findings are an inventory, not proof that each string is reachable
under PostgreSQL. Manual control-flow classification produced the table below.

## Classified SQLite inventory

| File / symbol | Finding | Class | Blocks PG-only runtime? | Stage 70A action |
|---|---|---:|---:|---|
| `app/db.py:load_db_config` | SQLite is the default; resolves `SQLITE_DB_PATH`, `MVP_DB_PATH`, `APP_DATA_DIR`, and `mvp.sqlite3` | A | No today; must change for 70A fail-closed startup | Require PostgreSQL and `DATABASE_URL`; remove SQLite path resolution |
| `app/db.py:connect_database` | Explicit SQLite connection branch; PostgreSQL branch is guarded and never falls back after an error | A | No | Delete SQLite branch; retain direct PostgreSQL connection and clear errors |
| `app/db.py:connect`, `apply_connection_pragmas`, `ensure_db_initialized`, `init_db`, `run_lightweight_migrations`, and SQLite rebuild/seed helpers | SQLite connection, schema creation, PRAGMAs, metadata, lightweight migrations, `lastrowid`, and qmark SQL | A | No: WSGI calls initialization only when backend is SQLite | Delete runtime-only functions/constants after transition tooling is decoupled |
| `app/schema.sql` | Complete SQLite DDL (`PRAGMA`, `AUTOINCREMENT`) | A | No: loaded only by SQLite `init_db` | Delete after callers in `app/db.py` and legacy fixture creator are decoupled |
| `app/server.py:app` | Constructs `Repository(conn, backend=DB_CONFIG.backend)`; SQLite-only initialization/seed block | A | No | Remove SQLite branch, `DB_PATH`, and SQLite-only imports |
| `app/server.py:ensure_seed` | qmark, `INSERT OR IGNORE`, and SQLite-oriented demo bootstrap SQL | A | No: only invoked by SQLite startup and SQLite audit/tests | Remove from production runtime; retain a fixture copy only if legacy tooling needs it |
| `app/server.py` SQLite annotations and SQLite exception compatibility | `sqlite3.Row` annotations and SQLite exception handling | A | No | Replace annotations with mapping types; remove SQLite exception clauses/import |
| `app/repository.py:Repository.__init__` | Backend defaults to SQLite and installs a SQLite UDF | A | No production caller omits backend | Make PostgreSQL the sole/default backend and delete UDF registration |
| `app/repository.py` backend branches | SQLite qmarks, `PRAGMA`, `GROUP_CONCAT`, `COLLATE NOCASE`, SQLite timestamps, booleans, and ID extraction coexist with PostgreSQL branches | A | No: PostgreSQL branches are selected by explicit backend | Delete only SQLite alternatives; keep PostgreSQL SQL and business behavior |
| `app/db_adapter.py` SQLite alternatives | qmark placeholders, `INSERT OR IGNORE`, `lastrowid`, integer booleans | A | No | Remove SQLite branches; retain identifier validation, PG placeholders, `ON CONFLICT`, `RETURNING`, and row normalization |
| `app/db_errors.py` SQLite parser | Imports and classifies SQLite exceptions | A | No | Remove SQLite parser/import; retain SQLSTATE mapping and `DbErrorInfo` |
| `app/importer.py` default backend arguments | Defaults are SQLite, but server passes `repo.backend`; SQL uses adapter placeholders and explicit backend repositories | A | No | Remove SQLite defaults or make PostgreSQL mandatory; retain import logic |
| `scripts/audit_postgres_production_gate.py:_authenticated_logout_contract_is_complete` | Uses temporary SQLite to exercise the security/layout contract | A (readiness tooling) | No application-runtime blocker, but must be rewritten before SQLite dependency removal | Exercise the PostgreSQL WSGI harness or replace with a driver-independent contract fixture |
| `scripts/postgres_preflight.py` | Read-only SQLite source inspection using PRAGMA, metadata, rowid, and `GROUP_CONCAT` | B | No | Retain for the one-release legacy migration window |
| `scripts/migrate_sqlite_to_postgres.py` | SQLite source reader and PostgreSQL loader | B | No | Retain for the one-release legacy migration window |
| `scripts/create_migration_demo_sqlite.py` | Creates a representative SQLite migration source | B | No | Retain with migration tooling, then remove with it |
| `scripts/relocate_sqlite_db.py` | SQLite backup/relocation utility | B | No | Remove in 70A if operations no longer relocate legacy DBs; otherwise keep in the legacy tool bundle only |
| `tests/test_db.py`, `tests/test_server.py`, `tests/test_repository.py`, `tests/test_importer.py`, adapter tests | SQLite runtime/unit fixtures, qmarks, `lastrowid`, PRAGMAs | C | No | Rewrite core runtime coverage around PostgreSQL/fakes, then remove SQLite variants |
| `tests/test_sqlite_to_postgres_migration.py`, `tests/test_migration_demo_sqlite.py`, `tests/test_filesystem_layout.py` | Legacy source and filesystem fixtures | C | No | Keep while the matching legacy tools remain; remove one release after baseline |
| `tests/test_postgres_*` incidental SQLite usage | Driver-independent fixtures and security audit setup; PostgreSQL-specific constructors are explicit where semantic SQL is exercised | C | No | Remove incidental SQLite fixtures as part of 70A test rewrite |
| PostgreSQL docs and old plans mentioning SQLite | Historical decisions, migration instructions, and comparison strings | D | No | Preserve history; update active README/deployment instructions in 70A |
| `README.md`, `docs/deployment/filesystem_layout.md`, `.env.example` | Active SQLite setup documentation | D | No runtime block, but operationally stale after 70A | Replace with fail-closed PostgreSQL instructions |

## PostgreSQL runtime findings

### Startup, schema, and fallback

`load_db_config` chooses one backend. `connect_database` calls
`connect_postgres` for either PostgreSQL alias, requires the enablement guard and
`DATABASE_URL`, and propagates connection/import errors. It has no exception
handler that opens SQLite after PostgreSQL failure. The WSGI application passes
the configured backend explicitly to `Repository`.

PostgreSQL startup never calls `ensure_db_initialized`, `init_db`,
`run_lightweight_migrations`, `ensure_seed`, or reads `app/schema.sql`. The
canonical PostgreSQL owner is `docs/postgres/schema.postgres.sql`; PostgreSQL
schema deployment remains external to request startup.

### Reads, writes, transactions, and rows

The repository's PostgreSQL branches use `%s`, PostgreSQL aggregation and
ordering, boolean values, `RETURNING id`, SQLSTATE error mapping, and psycopg
dictionary rows. `Repository.transaction` commits on success and rolls back on
failure for either DB-API connection. The PostgreSQL harness covers reads and all
51 planned mutating methods, including savepoint/rollback behavior.

The remaining `sqlite3.Row` return annotations do not influence runtime values.
PostgreSQL receives mapping rows through psycopg's `dict_row`; consumers use
mapping access. No production PostgreSQL caller relies on `lastrowid`.

### Repository construction

The only real application constructors are:

* `app/server.py:app`: explicit `backend=DB_CONFIG.backend`;
* `app/importer.py`: every constructor receives the caller's backend;
* PostgreSQL smoke/write scripts: explicit `backend="postgres"`.

Implicit constructors occur only in SQLite tests/fixtures and the readiness
audit's temporary SQLite security check. Backend inference is not performed and
is not relied upon by PostgreSQL production.

### Import, export, users, HLR, and audit

Import preview/apply propagates the backend, uses adapter placeholders, and calls
backend-aware repository methods. Clearing operations are portable `DELETE`
statements. Exports read the same repository mappings and add no SQLite SQL.

PostgreSQL harness and full-app smoke coverage exercise routes, providers,
tariffs, numbers, dictionaries, campaigns, user administration/authentication/
permissions/password operations, HLR settings/daily usage/limits, and change-log
writes/history reads. HLR result rendering and CSV export remain server-side and
unchanged. No persistent HLR result history was introduced.

## Exact Stage 70A readiness map

### A. Safe to delete in Stage 70A

1. SQLite constants, config/path selection, connection branch, initialization,
   schema loading, migrations, rebuilds, and seeds in `app/db.py`.
2. `app/schema.sql`, after the legacy fixture creator no longer imports runtime
   `init_db`.
3. The SQLite startup/seed branch, SQLite type annotations/import, and SQLite
   exception compatibility in `app/server.py`.
4. SQLite-only branches in `Repository`, `db_adapter`, and `db_errors`; do not
   remove their PostgreSQL/generic portions.
5. SQLite default backend arguments in `Repository` and importer entry points.
6. Active SQLite deployment/config documentation and obsolete relocation tooling
   if the latter is not placed in the temporary legacy bundle.
7. SQLite-only core application tests once equivalent PostgreSQL coverage exists.

### B. Keep temporarily as legacy migration tooling

* `scripts/migrate_sqlite_to_postgres.py`: the actual one-way data transition.
* `scripts/postgres_preflight.py`: validates a legacy SQLite source before import.
* `scripts/create_migration_demo_sqlite.py`: deterministic migration fixture.
* `scripts/relocate_sqlite_db.py`: only if legacy source relocation/backup remains
  an operational requirement during the transition release.
* Their focused tests: `tests/test_sqlite_to_postgres_migration.py`,
  `tests/test_migration_demo_sqlite.py`, and the applicable filesystem tests.

These should be isolated from `app` (for example under `scripts/legacy`) in 70A.
They may retain `sqlite3` for one release and can be removed after the production
PostgreSQL baseline and final legacy import acceptance are recorded.

### C. Keep as PostgreSQL runtime

* `DbConfig`'s PostgreSQL URL/backend information, `connect_postgres`, and a
  simplified fail-closed `connect_database`.
* Repository business methods and their PostgreSQL SQL branches.
* Adapter identifier validation, `%s` placeholders, `ON CONFLICT`, `RETURNING`
  ID extraction, row normalization, and PostgreSQL boolean handling.
* SQLSTATE-based error mapping and generic error data.
* Import/export, users, HLR usage/settings, audit/change-log, campaign, route,
  provider, tariff, number, and dictionary behavior.
* `docs/postgres/schema.postgres.sql` as canonical schema ownership.

### D. Blockers before 70A

**NONE.** The items under A are the planned 70A removal work, not unresolved
PostgreSQL compatibility defects. The production readiness audit's temporary
SQLite security fixture must be rewritten in the same 70A change that removes
the SQLite dependency.

## Fail-closed changes required in 70A

1. Change `load_db_config` so missing/blank `DB_BACKEND`, SQLite aliases, and
   missing/blank `DATABASE_URL` produce a clear startup configuration error.
2. Remove `sqlite_path` from the runtime config and remove `DB_PATH` replacement
   in WSGI startup.
3. Simplify `connect_database` to PostgreSQL only. Continue propagating psycopg
   import and connection failures; never catch them to create a local file.
4. Decide whether the temporary `POSTGRES_RUNTIME_ENABLED` rollout guard becomes
   mandatory or is removed; it must not select a different backend.
5. Make `Repository` PostgreSQL-only (or require an explicit backend during the
   short cleanup) so an omitted argument cannot select SQLite semantics.
6. Rewrite `_authenticated_logout_contract_is_complete` in the production audit
   to avoid `server.init_db`/SQLite.
7. Update environment examples, README, deployment docs, and tests so no normal
   startup instruction creates `mvp.sqlite3`.

## Verification record

* `tests.test_server`: **547 run; failures 0; errors 0; skipped 0**.
* `tests.test_repository`: 161 run, all passed.
* `tests.test_db`: 12 run, all passed.
* `tests.test_importer`: 50 run, all passed.
* PostgreSQL security gate: 8 run, all passed.
* PostgreSQL production gate: 7 run, all passed.
* PostgreSQL full-app smoke tests: 29 run, all passed.
* Migration/runtime/harness selection: 101 run, all passed; emitted non-failing
  `ResourceWarning` messages from legacy SQLite fixture connections.
* Production audit: `status=ready`, `security_gate=ok`, `blockers=[]`,
  `ready_for_runtime_enablement=true`.

No application bug was fixed and no product behavior, schema, HLR pipeline,
import/export behavior, permissions, or SQLite transition tool was changed.
