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

### Stage 28 phone-number import UPDATE focused audit status

- Completed a focused audit of the existing phone-number import UPDATE and its
  adjacent `phone_number_history` INSERT, including imported creator metadata,
  sticky review behavior, active/deactivated transitions, counters, preview,
  summary, and transaction/failure boundaries.
- Stage 28 is documentation-only; no runtime code or import behavior changed.
- Extraction is deferred to Stage 29 only if implemented as the audited narrow
  UPDATE-plus-history Repository pair with existing importer responsibilities and
  commit behavior preserved.
- PostgreSQL runtime remains disabled; no PostgreSQL connection is created and no
  runtime `psycopg` dependency is added.
- SQLite remains the operational backend.

### Stage 29 phone-number import UPDATE and history extraction status

- Extracted the existing-number `UPDATE phone_numbers` and its required `INSERT phone_number_history` together into `Repository.update_phone_number_import_fields_with_history()`.
- The method executes both statements in order and performs their single immediate commit only after both statements; the importer's final commit is unchanged.
- Identity lookup, creator preservation, sticky review and deactivation decisions, history detail construction, validation, preview, and counters remain in the importer. The phone create path is untouched.
- PostgreSQL runtime remains disabled, no runtime psycopg dependency was added, and SQLite remains the operational backend.

### Stage 30 phone-number import CREATE focused audit status

- Completed the focused audit of phone-number import CREATE, including the
  `phone_numbers`, `phone_number_history`, and `change_log` writes, fields,
  validation, counters, preview/summary behavior, and transaction boundaries.
- Stage 30 is documentation-only; no runtime code or import behavior changed.
- Extraction is deferred to Stage 31 and is recommended only if the three-write
  create operation remains together with its existing commit behavior.
- PostgreSQL runtime remains disabled, no runtime psycopg dependency is added,
  and SQLite remains the operational backend.

### Stage 31 phone-number CREATE adapter compatibility status

- Adapted the existing combined `Repository.create_phone_number()` operation; no
  duplicate import-only create method was introduced.
- `INSERT phone_numbers` now uses backend placeholders, backend-specific
  `RETURNING id` preparation/id extraction, and backend boolean conversion.
  The following `INSERT phone_number_history` and `INSERT change_log` remain in
  the same Repository operation and precede its single optional commit.
- Import counters, preview, validation, creator/review decisions, and active
  state decisions remain in the importer, which was not changed.
- PostgreSQL runtime remains disabled, no runtime psycopg dependency was added,
  and SQLite remains the operational backend.

### Stage 32 destructive import section-clearing audit status

- Destructive section-clearing `DELETE` paths are under the focused audit in
  [`import_section_clearing_audit.md`](import_section_clearing_audit.md), including
  delete order, restrictive FK/history risks, and commit/rollback behavior.
- Stage 32 is documentation/planning only; importer, Repository, server, schemas,
  and all other runtime behavior are unchanged.
- PostgreSQL runtime remains disabled, no runtime `psycopg` dependency is added,
  and SQLite remains the operational backend.

### Stage 33 Repository read-only PostgreSQL smoke status

- CI now runs a Repository-level, read-only smoke after migration apply against
  the workflow's temporary PostgreSQL service.
- The smoke covers only adapter-ready read methods and starts a read-only
  transaction; it does not run Repository writes, migrations, or the full app.
- This is CI compatibility verification, not a runtime switch:
  `DB_BACKEND=postgres` remains disabled and SQLite remains the production and
  development backend.
- `psycopg` remains a workflow-only dependency and is imported lazily by the
  standalone smoke script.

### Stage 34 Repository read-only PostgreSQL smoke batch 2 status

- The smoke adds seven pure reads: `get_app_setting_value`,
  `get_hlr_daily_usage`, `get_hlr_limit_override`, `list_calling_companies`,
  `get_calling_company`, `latest_currency_rate`, and `get_currency_rate`.
- Compatibility changes are limited to backend placeholders and backend boolean
  values in those methods. SQLite return shapes, ordering, filters, and business
  semantics are unchanged. The synthetic demo adds only a deterministic
  `hlr_daily_limit_override=2500` application setting.
- The unfiltered `list_calling_companies()` smoke path uses backend boolean
  fallbacks for both `has_autorotation` and the active-setting join. Its optional
  search/filter path still depends on SQLite search semantics and remains
  deferred rather than being redesigned in Stage 34.
- The smoke performs 61 semantic checks (the Stage 33 baseline 44 plus 17) and
  verifies exact demo values as well as missing setting, HLR usage, company,
  and currency-rate results.
- User reads that depend on `PRAGMA`, search/filter lists that depend on
  `search_text_matches`/SQLite SQL, history and JSON reads, tariff reads, and
  methods without a suitable adapter-ready fixture lookup remain deferred. The
  focused rationale is recorded in
  [`repository_read_only_smoke_audit.md`](repository_read_only_smoke_audit.md).
- The PostgreSQL transaction remains `READ ONLY`. No Repository write path,
  migration, schema change, full application flow, or PostgreSQL application
  runtime is exercised.
- `DB_BACKEND=postgres` remains disabled, `psycopg` remains CI/smoke-only, and
  SQLite remains the operational production and development backend.

### Stage 35 Repository detail and permission read smoke status

- The Repository-only smoke adds eight pure reads:
  `dictionary_rename_preview`, `get_user_section_permission`,
  `get_user_permissions`, `get_phone_number`, `get_route`, `route_numbers`,
  `find_tariff_by_identity`, and `get_tariff`.
- Compatibility fixes are limited to backend placeholders, mapping access to
  named `COUNT(*) AS count` results, and parameterized backend boolean values
  for active route-number relations. Existing SQLite row/dict/list contracts,
  nullable tariff-prefix identity, and business semantics remain intact. For
  `route_numbers`, the existing columns retain their exact order, while
  `usage_type` and `is_active` are additive trailing relation fields.
- The smoke performs 103 semantic checks. Coverage includes every supported
  dictionary-preview branch and unknown kind; positive and negative permission,
  phone, route, relation, tariff-identity, and tariff-detail results; strict
  SQLite/PostgreSQL boolean representations; and Decimal-based tariff numeric
  comparisons.
- Demo IDs are obtained only through earlier Repository results. The smoke does
  not issue direct fixture SQL, and its PostgreSQL transaction remains
  `SET TRANSACTION READ ONLY`.
- The full application and Repository write paths are not run. PostgreSQL
  application runtime and `DB_BACKEND=postgres` remain disabled, while SQLite
  remains the operational production and development backend.

### Stage 36 PostgreSQL user read and authentication smoke status

- The Repository-only smoke adds `list_users`, `get_user`,
  `get_user_by_username`, and `authenticate_user`; `_user_columns` is an adapted
  private dependency and is deliberately excluded from the public smoke plan.
- User schema introspection retains SQLite's `PRAGMA table_info(users)` path and
  adds a parameterized PostgreSQL `information_schema.columns` query scoped by
  `current_schema()`. It uses the existing Repository connection and performs no
  DDL, migration, schema, or search-path change.
- SQLite retains its exact `COLLATE NOCASE` ordering. PostgreSQL orders active
  users first, then uses the effective display name and username through
  `LOWER`, with `id` as a stable tie-breaker. Exact locale equivalence between
  database engines is not claimed.
- The smoke performs 131 semantic checks. The added coverage includes complete
  and active-only lists, strict database booleans, absent-record contracts,
  credential-column boundaries, trimmed exact username lookup, successful and
  rejected password verification, and rejection of the deterministic inactive
  user. No user credential values are emitted in diagnostics.
- `authenticate_user` was audited as read-only: it delegates to
  `get_user_by_username` and verifies the password hash locally without user
  writes, commit/rollback, last-login state, HTTP login, cookies, or sessions.
- The PostgreSQL connection remains in `SET TRANSACTION READ ONLY`; user writes,
  the full application, and PostgreSQL runtime are not run.
  `DB_BACKEND=postgres` remains disabled and SQLite remains the operational
  production and development backend.

### Stage 37 PostgreSQL search-filter foundation and routes list smoke status

- `query_filters()` now has a backward-compatible `backend="sqlite"` default, and
  all four Repository callers explicitly supply their backend. Equality predicates
  use backend placeholders while values remain parameters in mapping order.
- SQLite continues to use the registered `search_text_matches(column, ?) = 1` UDF:
  inputs are stripped and Python-Unicode-casefolded, `NULL` haystacks act as empty
  strings, and contains matching treats `%`, `_`, backslash, and LIKE symbols literally.
- PostgreSQL uses parameterized
  `POSITION(LOWER(CAST(%s AS TEXT)) IN LOWER(COALESCE(CAST(column AS TEXT), ''))) > 0`.
  Search input is trimmed only and PostgreSQL performs `LOWER`; no wildcard matching,
  regex, extension, custom function, or DDL is involved.
- Exact Unicode/locale equivalence between Python `casefold` and database `LOWER` is
  not claimed. Ordinary ASCII, digits, and standard Cyrillic have the expected
  case-insensitive contains behavior; `%` and `_` are literals on both engines.
- `STAGE_37_METHODS` contains only `list_routes`. Its unfiltered values and phone
  count, country/provider equality filters, boolean `is_actual` variants, prefix
  variants, case/trim/partial/missing searches, literal wildcard characters, and
  combined filter are independently asserted.
- The smoke now performs **156 semantic checks**. Demo IDs are sourced only from
  prior Repository reads and the transaction remains `SET TRANSACTION READ ONLY`.
- Filtered phone, tariff, company, routing-settings, history/event/JSON, and routing
  lists remain deferred. No Repository write or full application path is run.
- This remains compatibility smoke only: `DB_BACKEND=postgres` is disabled,
  `psycopg` is lazy and CI/smoke-only, and SQLite remains the operational backend.
