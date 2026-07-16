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

### Stage 15 batch 1 status

- Started Repository read-only adapter usage in a small low-risk dictionary/read batch.
- Converted only dictionary/list lookup methods and simple parameterized reads.
- No insert/update/delete paths were converted.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created.
- SQLite remains the operational backend.

## Stage 16 — Repository SQL compatibility batch 2

Scope:

- Start a small write-path compatibility batch for low-risk Repository create methods only.
- Replace selected `cursor.lastrowid` flows with `prepare_insert_returning_id()` and `extract_inserted_id()`.
- Use adapter placeholders and boolean conversion only inside the selected methods.
- Add tests for inserted ID return and persisted SQLite rows.

Focus areas:

- simple dictionary create methods;
- entity creates without complex transactions, recalculation, Telegram, HLR, optimistic concurrency, import flows, or route/tariff/currency side effects.

Non-goals:

- No PostgreSQL runtime backend.
- No PostgreSQL connection.
- No runtime psycopg import or dependency.
- No mass Repository rewrite.
- No importer/server direct SQL cleanup in this stage.
- No routes, tariffs, currency recalculation, HLR, user permissions, import, or Telegram write-path changes.

### Stage 16 small write-path status

- Started the small write-path batch in `Repository` only.
- Applied insert-id abstraction to selected low-risk create methods with `prepare_insert_returning_id()` and `extract_inserted_id()`.
- Kept SQLite behavior as `lastrowid`-compatible; SQLite SQL remains without `RETURNING`.
- Used adapter boolean storage for selected `is_active` writes while preserving SQLite `1`/`0` values.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created.
- No mass Repository rewrite was performed.
- SQLite remains the operational backend.

## Stage 17 — `server.py` / `importer.py` direct SQL cleanup

Scope:

- Start direct SQL cleanup outside `Repository` with small, low-risk batches only.
- Prefer `app/server.py` dictionary/lookup reads and simple dictionary create flows that can call existing or narrowly added Repository methods.
- Touch `app/importer.py` only for obvious read-only lookups; otherwise defer importer cleanup to a later stage.
- Keep SQLite behavior and UI output unchanged while moving selected SQL behind Repository boundaries.

Non-goals:

- No mass rewrite of `server.py` or `importer.py`.
- No routes, tariffs, currency recalculation, HLR, Telegram, optimistic concurrency, or import business-flow changes.
- No PostgreSQL runtime backend, PostgreSQL connection, runtime psycopg import, or psycopg dependency.
- No schema changes.
- No UI redesign.
- No HLR API/data pipeline rewrite.
- No frontend-rendered HLR tables.

### Stage 17 batch 1 status

- Started direct SQL cleanup outside `Repository` with a small low-risk server-side dictionary/lookup batch.
- Moved selected `app/server.py` dictionary reads for change reasons, countries, providers with currency labels, servers, and phone number types to Repository methods.
- Added only narrow read-only Repository helpers where needed.
- Left `app/importer.py` unchanged; importer cleanup remains deferred.
- Did not touch routes, tariffs, currency recalculation, or HLR flows.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created.
- SQLite remains the operational backend.

## Stage 18 — Direct SQL cleanup batch 2

Scope:

- Continue direct SQL cleanup outside `Repository` with another small, low-risk batch.
- Limit changes to read-only `app/server.py` dictionary/lookup sections.
- Prefer projects, phone assignment types, provider prefixes, currencies, and simple admin dictionary render/count reads.
- Defer `app/importer.py` unless an obvious safe read-only lookup is covered by tests.

Non-goals/status:

- No PostgreSQL runtime backend; PostgreSQL runtime remains disabled.
- No runtime psycopg import or psycopg dependency.
- No routes, tariffs, currency recalculation, HLR, Telegram, import parsing, or optimistic concurrency changes.
- No UI behavior changes.
- SQLite remains the operational backend.

### Stage 18 batch 2 status

- Continued direct SQL cleanup in `app/server.py` for read-only admin dictionary/lookup rendering.
- Moved dictionary counts plus currencies, provider prefixes with provider labels, projects, and phone assignment type reads behind Repository methods.
- Left `app/importer.py` unchanged; importer cleanup remains deferred.
- Did not touch routes, tariffs, currency recalculation, or HLR flows.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created.
- SQLite remains the operational backend.

## Stage 19 — PostgreSQL runtime smoke in CI

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

### Stage 19 importer read-only cleanup status

- Started the `app/importer.py` read-only direct SQL cleanup with a small lookup-only batch.
- Moved selected dictionary/reference lookup `SELECT` calls behind narrow Repository methods.
- Import write behavior, CSV parsing, validation messages, preview behavior, and summary counters remain unchanged.
- Routes, tariffs, currency recalculation, and HLR flows were not changed.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created.
- SQLite remains the operational backend.

### Stage 20 importer exists-check cleanup status

- Started the next `app/importer.py` read-only cleanup batch for SELECT-based existence checks.
- Moved route, phone number, calling company, and current tariff existence checks behind narrow Repository bool methods.
- Import write behavior, CSV parsing, validation messages, preview behavior, and summary counters remain unchanged.
- Routes, tariffs, currency recalculation, HLR flows, Telegram, and UI flows were not changed.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created and no runtime psycopg dependency is added.
- SQLite remains the operational backend.

### Stage 21 importer remaining read-only cleanup status

- Continued the final small `app/importer.py` read-only cleanup batch after manually reviewing remaining direct `SELECT` usage.
- Moved the phone-number import identity lookup (`id`, `imported_created_by`) behind a narrow Repository read method used by the existing phone update import path.
- Import write behavior, CSV parsing, validation messages, preview behavior, and summary counters remain unchanged.
- Remaining direct SQL in `app/importer.py` is classified as write/import flow (`UPDATE`, `INSERT`, `DELETE`, section clearing, and dictionary insert-or-ignore paths) for later stages.
- Routes, tariffs, currency recalculation, HLR flows, Telegram, and UI flows were not changed.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created and no runtime psycopg dependency is added.
- SQLite remains the operational backend.

### Stage 22 importer write SQL audit status

- Importer read-only cleanup from Stages 19–21 is complete; no direct read-only `SELECT` statements remain in `app/importer.py`.
- Remaining direct importer SQL has been classified as write/import flow: entity updates, phone history insert, dictionary `INSERT OR IGNORE` paths, and section-clearing `DELETE` statements.
- Stage 22 is documentation/read-only planning only and does not change runtime behavior.
- Stage 23 should extract only 1–2 low-risk write candidates, preferably isolated dictionary insert-or-ignore helpers, while preserving commit/rollback behavior and import counters.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created and no runtime `psycopg` dependency is added.
- SQLite remains the operational backend.

### Stage 23 importer dictionary insert-ignore extraction status

- Dictionary `INSERT OR IGNORE` extraction has started for the low-risk importer dictionary paths.
- The `projects` and `phone_number_types` insert-if-missing paths now go through narrow Repository methods; the `phone_assignment_types` path remains classified for a later small batch.
- Import write behavior, CSV parsing, validation messages, preview behavior, and summary counters remain unchanged.
- Immediate commit behavior is preserved inside the new Repository methods, matching the previous importer statements.
- Routes, phone-number complex writes, tariffs, calling companies, history tables, section clearing, currency, HLR, Telegram, and UI flows were not changed.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created and no runtime `psycopg` dependency is added.
- SQLite remains the operational backend.

### Stage 24 importer dictionary insert-ignore extraction status

- The `phone_assignment_types` insert-if-missing path now goes through `Repository.ensure_phone_assignment_type_exists()`.
- The safe dictionary insert-ignore candidates (`projects`, `phone_number_types`, and `phone_assignment_types`) are now extracted from `app/importer.py`.
- Import behavior is preserved: CSV parsing, validation messages, preview behavior, summary counters, fallback `code` resolution, and immediate commit semantics remain unchanged.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created and no runtime `psycopg` dependency is added.
- SQLite remains the operational backend.

### Stage 25 existing calling-company update extraction status

- Started the first medium-risk importer write extraction.
- Moved only the existing `calling_companies` update into the narrow `Repository.update_calling_company_import_fields()` method.
- Preserved import branching, counters, validation, preview/summary behavior, and the immediate commit.
- Left company creation, `company_routing_settings`, and section clearing untouched.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created and no runtime `psycopg` dependency is added.
- SQLite remains the operational backend.

### Stage 26 route import UPDATE focused audit status

- Completed a focused audit of the existing-route import UPDATE, including fields,
  validation order, counters, preview/summary behavior, history and relation side
  effects, and commit/failure boundaries.
- Stage 26 is documentation-only; no runtime code or import behavior changed.
- Extraction is deferred to Stage 27 and is recommended only as a narrow
  `UPDATE routes` Repository method that preserves the current absence of update
  history and the immediate commit.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created and no
  runtime `psycopg` dependency is added.
- SQLite remains the operational backend.

### Stage 27 existing route import UPDATE extraction status

- Moved only the existing-route `UPDATE routes` statement into the narrow
  `Repository.update_route_import_fields()` method.
- Route creation, route history/change-log behavior, phone-number links, server
  priorities, overflow, and AON/pool fields remain untouched.
- Import branching, validation, preview/summary counters, exception handling, and
  the existing immediate commit are preserved.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created and no
  runtime `psycopg` dependency is added.
- SQLite remains the operational backend.
