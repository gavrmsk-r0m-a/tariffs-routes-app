# Stage 14+ PostgreSQL runtime adapter plan

This plan intentionally does **not** implement PostgreSQL runtime support. It defines small follow-up stages that preserve SQLite behavior while removing PostgreSQL blockers incrementally.

## Adapter design goals

- Keep the application operational on SQLite at every stage.
- Keep `DB_BACKEND=postgres` disabled until explicit experimental Stage 19.
- Avoid SQLAlchemy/Alembic and avoid runtime psycopg imports before PostgreSQL mode exists.
- Avoid business-logic refactors while moving SQL execution boundaries.
- Make converted code testable against SQLite first, then PostgreSQL smoke later.

## Proposed adapter surface

A future `app/db_adapter.py` or equivalent module should expose:

- `DatabaseAdapter` protocol/class
  - `backend: Literal["sqlite", "postgres"]`
  - `connect(config) -> connection wrapper`
  - `execute(sql, params=())`
  - `executemany(sql, seq_of_params)`
  - `fetchone(sql, params=())`
  - `fetchall(sql, params=())`
  - `transaction()` context manager
  - `commit()` / `rollback()` compatibility while legacy callers remain
- Placeholder helpers
  - `param()` or renderer for logical placeholders
  - `placeholders(count)` for dynamic lists
  - SQLite renders `?`
  - PostgreSQL renders `%s`
- Insert helpers
  - `insert_returning_id(table, columns, values)` or `execute_insert_id(sql, params, id_column="id")`
  - SQLite reads `cursor.lastrowid`
  - PostgreSQL uses `RETURNING id`
- Row normalization
  - Mapping-style row access by column name
  - Optional index access for compatibility where needed
  - Consistent `None` behavior for `fetchone()`
- Error mapping
  - `map_database_error(exc)` accepts backend-specific exceptions
  - backend-neutral categories for unique, foreign key, not-null, check, lock/timeout, unknown
- Schema/bootstrap hooks
  - SQLite: existing schema and lightweight migrations
  - PostgreSQL: remain disabled until later stage; never execute `app/schema.sql`

## Stage 14 — DB adapter foundation

Scope:

- Add `app/db_adapter.py` as a backend-neutral helper module without enabling PostgreSQL runtime mode.
- Add backend normalization for `sqlite`, `postgres`, and the `postgresql` alias.
- Add placeholder helpers for SQLite `?` and PostgreSQL `%s`, including dynamic placeholder lists.
- Add a safe dynamic `IN` clause helper backed by strict simple/qualified identifier validation.
- Add inserted-id helpers for SQLite `cursor.lastrowid` and future PostgreSQL `RETURNING id` flows.
- Add row normalization helpers for mapping-style rows and SQLite rows.
- Add strict boolean conversion helpers for SQLite integer booleans and PostgreSQL native booleans.
- Keep `db_errors` as the canonical exception classification surface; the adapter only documents that boundary.
- Leave transaction behavior in `Repository.transaction` for now; Stage 16+ can unify it if needed.
- Preserve current SQLite runtime behavior and keep PostgreSQL runtime disabled.

Non-goals:

- No Repository mass rewrite.
- No PostgreSQL connection.
- No runtime psycopg dependency.
- No schema changes.
- No UI changes.

Suggested PR size:

- New adapter module.
- Focused tests.
- Documentation note only; no `app/db.py` integration unless needed for compatibility.

## Stage 15 — Repository SQL compatibility batch 1

Scope:

- Convert a small read-only Repository method set to adapter execution helpers.
- Good candidates: dictionary/list/read helpers with simple `SELECT ... WHERE id = ?` and no transaction behavior.
- Add tests proving SQLite results are unchanged.
- Establish code style for converted Repository methods.

Non-goals:

- No insert/update conversions yet.
- No server/importer direct SQL cleanup yet.
- No PostgreSQL runtime mode.

Suggested PR size:

- 5-10 low-risk read methods.
- Tests only around changed methods.

## Stage 16 — Repository SQL compatibility batch 2

Scope:

- Convert insert/update Repository paths.
- Replace `cursor.lastrowid` with adapter `insert_returning_id()`.
- Convert selected `commit=True` methods to adapter transaction conventions.
- Add tests for insert ID return and rollback behavior on SQLite.

Focus areas:

- entity create methods;
- route/phone/tariff mutations;
- user/admin mutations;
- methods that currently combine insert + history rows.

Non-goals:

- No importer/server direct SQL mass migration in this stage unless needed by a converted Repository method.

## Stage 17 — `server.py` / `importer.py` direct SQL cleanup

Scope:

- Move remaining direct SQL mutations from `app/server.py` into Repository or adapter-backed service methods.
- Move importer persistence operations behind Repository/adapter helpers while leaving CSV parsing and validation behavior unchanged.
- Replace `INSERT OR IGNORE` with backend-neutral upsert helper.
- Replace direct `sqlite3.IntegrityError` catches with backend-neutral error mapping.

Priority order:

1. Direct mutations with commits.
2. Direct inserts using SQLite-only syntax.
3. Dynamic `IN` placeholder generation.
4. Read-only helper queries used by templates.

Non-goals:

- No UI redesign.
- No HLR API/data pipeline rewrite.
- No frontend-rendered HLR tables.

## Stage 18 — PostgreSQL runtime smoke in CI

Scope:

- Add app-level smoke tests that can run against PostgreSQL service in CI.
- Use migrated demo database or controlled fixture data.
- Test only adapter/Repository paths already converted.
- Keep production switch disabled.

Smoke targets:

- connect adapter in PostgreSQL test mode;
- simple read query;
- insert returning ID in isolated table or safe fixture;
- transaction rollback;
- unique constraint error mapping.

Non-goals:

- No production deployment.
- No full application coverage claim.

## Stage 19 — `DB_BACKEND=postgres` experimental mode

Scope:

- Enable PostgreSQL runtime only behind explicit environment configuration.
- CI-only or developer-only at first.
- Fail closed when required settings are missing.
- Document unsupported flows still routed through SQLite-only code if any remain.

Requirements before merging:

- PostgreSQL app smoke passes in CI.
- SQLite test suite remains green.
- Known unsupported runtime flows are documented and guarded.

Non-goals:

- No production cutover.

## Stage 20 — production migration readiness checklist

Checklist topics:

- Verified migration plan and rollback plan.
- Backup and restore procedure tested.
- PostgreSQL schema ownership decided.
- App runtime smoke and critical user flows tested on PostgreSQL.
- HLR server-rendered behavior, CSV export, filters, and column manager verified unchanged.
- Error messages verified for unique/foreign-key/not-null/check violations.
- Performance checks for key listing/filter pages.
- Operational settings: connection limits, timeouts, logs, secrets, backups.
- Explicit decision date and owner for switching production.

## First modules to change

1. `app/db.py`: adapter foundation, connection setup split, row normalization, PostgreSQL remains blocked.
2. `app/db_errors.py`: backend-neutral error shape while preserving SQLite mappings.
3. `app/repository.py`: read-only batch, then write/transaction batch.
4. `app/importer.py`: persistence cleanup after Repository helpers exist.
5. `app/server.py`: direct SQL cleanup after Repository methods are available.

## Review guardrails

- Every stage must state whether PostgreSQL runtime is still disabled.
- Every stage must run SQLite tests for changed behavior.
- Do not modify `app/schema.sql` for PostgreSQL compatibility; it remains the SQLite schema.
- Do not add generated audit reports, local databases, dumps, `.env`, or production data.
