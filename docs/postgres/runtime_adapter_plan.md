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

### Stage 38 PostgreSQL tariff status-filter smoke status

- `STAGE_38_METHODS` contains only `list_tariffs`, and `list_tariffs` appears in
  `SMOKE_METHODS` exactly once.
- The migration demo SQLite fixture now includes a deterministic synthetic inactive
  tariff on separate synthetic dictionary entities: `Inactive Tariff Country`,
  `Inactive Tariff Provider`, and `XTS` test currency. It intentionally adds no route,
  phone number, calling company, provider prefix, currency rate, history, or change-log
  data for those entities.
- `list_tariffs` preserves its existing status contract: missing `status` defaults to
  active only; `status="active"` returns current tariffs; `status="inactive"` returns
  inactive tariffs; `status="all"`, `status=""`, and `status=None` omit the status
  predicate. No `priority_status` or other new tariff filter is added.
- Country/provider equality filters keep the Stage 37 `query_filters()` mapping order.
  Combined PostgreSQL parameters follow SQL order: `country_id`, then `provider_id`,
  then the status boolean; for example country `4`, provider `5`, inactive status binds
  `[4, 5, False]`.
- Active/inactive predicates bind backend-native booleans rather than embedding literals:
  SQLite receives `1`/`0`, PostgreSQL receives `True`/`False`. The smoke uses strict
  `_is_database_true`/`_is_database_false` helpers and `_decimal_equals` for numeric
  tariff values, so SQLite 1/0 and PostgreSQL True/False are accepted without loose
  `bool(value)` coercion or scale-dependent numeric string comparisons.
- The Repository smoke now performs **193 semantic checks**. Coverage includes default,
  explicit active, inactive, all, empty, and None status contracts; equality filters;
  order by country/provider/prefix-or-empty; existing row shape; and inactive-fixture
  numeric/boolean semantics.
- The PostgreSQL connection remains `SET TRANSACTION READ ONLY`. The smoke does not run
  tariff writes, currency recalculation, tariff history, migration writes, or the full
  application runtime.
- `DB_BACKEND=postgres` remains off, `psycopg` remains lazy and CI/smoke-only, and
  SQLite remains the operational production/development backend.


### Stage 39 PostgreSQL calling-company filter smoke status

- `STAGE_39_METHODS` contains only `list_calling_companies`; the method remains in
  `SMOKE_METHODS` exactly once from the Stage 34 unfiltered coverage.
- The migration demo fixture adds two independent synthetic calling companies:
  `CI Manual Company`, with base `cc.has_autorotation` true but an active current
  routing setting where autorotation is false, and `CI Inactive Company`, with no
  routing setting so the current autorotation fallback is false.
- Boolean filters are normalized backend-aware: `"1"`/`1`/`True` bind true,
  `"0"`/`0`/`False` bind false, all/empty/None are ignored, and unsupported nonempty
  values return `[]` without PostgreSQL cast errors or new business errors.
- Name and external-ID filters use the Stage 37 literal substring search semantics:
  SQLite uses `search_text_matches(column, ?) = 1` with casefolded parameters, while
  PostgreSQL uses parameterized `POSITION(LOWER(CAST(%s AS TEXT)) IN
  LOWER(COALESCE(CAST(column AS TEXT), ''))) > 0`; LIKE/ILIKE wildcards remain
  literals and exact Unicode locale equivalence is not claimed.
- Full PostgreSQL calling-company filter parameter order is SQL order: false SELECT
  fallback, true active-setting join flag, `server_id`, `country_id`, trimmed company
  search, trimmed external-ID search, current autorotation boolean, and `cc.is_active`
  boolean. SQLite receives the same order with `0`/`1` boolean values and casefolded
  search parameters.
- The Repository smoke now performs **259 semantic checks**. Coverage includes
  current-vs-base autorotation, explicit current false, missing-setting false fallback,
  server/country filters, search literal behavior, boolean variants, ignored/invalid
  boolean values, combined filters, row shape, and sort order.
- The PostgreSQL connection remains `SET TRANSACTION READ ONLY`. The smoke does not
  run calling-company writes, company-routing writes, history/event JSON paths,
  migration writes, or the full application runtime.
- `DB_BACKEND=postgres` remains off, `psycopg` remains lazy and CI/smoke-only, and
  SQLite remains the operational production/development backend.

## Stage 40 PostgreSQL phone-number list and route-name aggregation smoke status

Stage 40 declares `STAGE_40_METHODS = ("list_phone_numbers",)` and includes the method in `SMOKE_METHODS` exactly once. The migration demo fixture now has isolated synthetic phone-list records: a no-provider review-required phone and an active routed phone with two active route links plus one inactive hidden route link.

`list_phone_numbers` keeps SQLite as the operational backend and does not enable `DB_BACKEND=postgres`. The method remains read-only in the PostgreSQL smoke, which still opens the connection with `SET TRANSACTION READ ONLY`; write paths and full application runtime remain outside scope.

The method uses a backend split for route-name aggregation: SQLite uses `GROUP_CONCAT`, PostgreSQL uses ordered `STRING_AGG`. The active route-link predicate appears in the SELECT before WHERE filters, so the active-link parameter is first, followed by `country_id`, `provider_id`, `project`, `project_like`, `assignment_type`, `status`, `number_like`, and `review_required` when present.

The smoke covers `provider_id=0` no-provider semantics through `COALESCE(pn.provider_id, 0)`, backend-aware `review_required` normalization, equality/search filter behavior, literal search metacharacters, combined filters, active/inactive `route_names` behavior, no-route `""` output, row shape, and `ORDER BY pn.number`.

The actual local Repository semantic smoke count for Stage 40 is 318 checks. PostgreSQL runtime remains disabled; SQLite remains the production/development backend.

## Stage 41 PostgreSQL company-routing settings list and detail smoke status

Stage 41 extends the read-only Repository smoke with:

```python
STAGE_41_METHODS = (
    "list_company_routing_settings",
    "get_company_routing_setting",
)
```

The synthetic migration-demo fixture now includes one historical CI Manual Company routing-setting version using the existing `CI Manual Company`, `CI Manual Company Country`, and `ci-manual-server-1` entities. The existing current version remains `server_priority`, active, `valid_to IS NULL`, and route-less; the synthetic historical version is inactive `autorotation`, route-less, has `valid_to = NOW`, and is not accompanied by a routing event, history row, change log, route, provider, phone, or tariff.

`list_company_routing_settings` keeps the current-only default (`crs.is_active` plus `crs.valid_to IS NULL`) and adds strict `include_history`/`show_history` alias normalization for history mode. The history-mode `is_active` filter is strict and backend-aware, and is ignored when history is disabled so callers cannot bypass the current-only contract. Equality/search SQL parameter order is deterministic: current mode starts with the active boolean and then country, server, routing mode, calling company, external-ID search; history mode uses country, server, routing mode, calling company, external-ID search, then optional active boolean.

The external-ID search uses the Stage 37 literal substring search foundation: SQLite uses `search_text_matches`, while PostgreSQL uses `POSITION(LOWER(CAST(%s AS TEXT)) IN LOWER(COALESCE(CAST(cc.company_id_external AS TEXT), ''))) > 0`; LIKE/ILIKE wildcard semantics are not introduced. `get_company_routing_setting` now uses backend placeholders for the ID lookup while preserving the existing joins, detail shape, and missing-ID `None` behavior.

The actual local semantic smoke count after Stage 41 is `387` checks. The smoke remains read-only under `SET TRANSACTION READ ONLY`; it does not execute Repository write methods or full application runtime paths. `DB_BACKEND=postgres` remains disabled, and SQLite remains the operational production/development backend.

## Stage 42 PostgreSQL provider-change list smoke status

- `STAGE_42_METHODS = ("list_provider_changes",)` is included in the read-only
  PostgreSQL Repository smoke plan.
- The synthetic migration-demo fixture now has two provider-change rows:
  - NEW: `provider_changed = 1`, route-before `Stage 42 Alpha`, route-after
    `Stage 42 Beta`, reason `Planned provider switch`, and servers A+B inserted in
    reverse order to prove ordered aggregation.
  - OLD: `provider_changed = 0`, the same route before/after, no linked servers,
    and reason `AON refresh without provider switch`.
- `server_names` aggregation has a backend split: SQLite keeps ordered
  `GROUP_CONCAT`, PostgreSQL uses ordered `STRING_AGG`.
- `server_names` is generated through a correlated subquery; the main query does
  not join provider-change servers and does not group by `pcl.id`.
- The provider filter checks both `provider_before_id` and `provider_after_id`.
- Route and reason searches use the Stage 37 literal substring foundation, so
  LIKE metacharacters such as `%` and `_` are not wildcards.
- SQL parameters are bound in this order: `date_from`, `date_to`, `country_id`,
  `provider_id`, `provider_id`, `route_like`, `route_like`, `reason_like`,
  `user_id`.
- The `NULL server_names` contract is explicit for provider-change rows without
  linked servers.
- The confirmed smoke `checks_count` is **403**.
- The PostgreSQL smoke connection remains `READ ONLY` through
  `SET TRANSACTION READ ONLY`.
- `DB_BACKEND=postgres` remains disabled; SQLite remains the operational backend.
- Provider-change writes, routing events, JSON/history reads, migration logic, and
  full app runtime remain outside Stage 42 scope.

## Stage 43 PostgreSQL routing-event list and detail smoke status

Stage 43 declares `STAGE_43_METHODS = ("list_routing_events", "get_routing_event")` and includes both methods in `SMOKE_METHODS` exactly once. The synthetic fixture adds four deterministic routing events (active none, inactive none, multi-server `server_priority`, and `campaign_setting`) plus two current tariffs for Stage 42 routes.

`list_routing_events` now uses backend-aware placeholders for active filtering and for all four current-tariff lookups. Parameter order is guarded: four tariff current flags first, then optional active/date/equality/server/campaign parameters. The smoke verifies include-inactive normalization, inclusive dates, country/scope/company/provider equality filters, server filtering via `re.server_id`, `routing_event_servers EXISTS`, and `cc.server_id`, and Stage 37 literal campaign search without LIKE/ILIKE wildcard behavior.

The Stage 43 semantic smoke checks old/new/delta prices for server-priority (`1`, `1.5`, `0.5`) and campaign-setting (`0.1`, `0.1`, `0`), preserves backend-native `snapshot_json` values (SQLite TEXT and PostgreSQL JSONB/dict), and verifies `get_routing_event` adds sorted `affected_server_names` only for `server_priority` detail rows. No routing-event write paths, full app runtime, DDL, JSON SQL predicates, SQLAlchemy, or Alembic are enabled.

The confirmed local Repository semantic smoke count is **459** checks. PostgreSQL smoke still runs inside `SET TRANSACTION READ ONLY`; `DB_BACKEND=postgres` remains disabled, psycopg remains smoke/CI-only with lazy import, and SQLite remains the operational backend.

## Stage 44 Repository read-surface audit status

Stage 44 adds an audit-only, machine-verifiable Repository read-surface classification gate. The current static classification counts are: **112** public Repository methods, **54** smoke-covered read methods, **7** deferred read-only methods, **50** write/mutating methods, and **1** infrastructure/mixed method. The classified Repository read-surface coverage is **88.52%**: 54 smoke-covered reads out of 61 classified read-only methods. This percentage is only the Repository read-surface coverage metric; it is not full PostgreSQL runtime readiness.

The direct runtime SQL census still finds direct SQL outside Repository: **53** SELECT calls, **65** write calls, **32** schema/PRAGMA calls, and **11** dynamic/unknown calls across `app/db.py`, `app/importer.py`, and `app/server.py`. That runtime boundary remains explicit: write/runtime work is still ahead.

The recommended next implementation batch is **`route_phone_tariff_history`** for `list_phone_history`, `list_route_history`, and `list_tariff_history`. The main blockers are placeholder/boolean/history-shape adaptation and deterministic PostgreSQL semantic fixtures. Calling-company JSON/event-search methods and routing-setting event history remain later batches.

The PostgreSQL Repository smoke remains at **459** semantic checks and continues to run in `SET TRANSACTION READ ONLY`. `DB_BACKEND=postgres` remains disabled, SQLite remains the operational production/development backend, and PostgreSQL remains CI/smoke-only for this migration surface.


## Stage 45 PostgreSQL read-surface audit hardening status

Stage 45 hardens the Stage 44 read-surface audit without changing Repository behavior or expanding the read-only smoke surface. The audit now enforces strict manifest metadata validation, including exact top-level keys, exact per-category metadata fields, non-empty deferred blockers, allowed write `mutation_kind` values, and object metadata for infrastructure/mixed entries.

The CLI exit-code contract is stable: **0** for a valid `status=ok` audit, **1** for classification/coverage `status=failed` violations, and **2** for input, parser, and manifest configuration `status=error` failures. The runtime SQL census now scans `app/**/*.py` recursively while excluding service/data directories such as `__pycache__`, `.venv`, `venv`, `data`, `backups`, and `logs`, and still excludes the analyzed Repository file by resolved path. The no-code-execution regression test uses an absolute side-effect marker to verify that the audit does not import or execute analyzed Python modules.

Classification counts remain unchanged: **112** public Repository methods, **54** smoke-covered reads, **7** deferred read-only methods, **50** write/mutating methods, **1** infrastructure/mixed method, and **88.52%** read-surface coverage. The direct runtime SQL census remains **53** SELECT calls, **65** write calls, **32** schema/PRAGMA calls, and **11** dynamic/unknown calls across `app/db.py`, `app/importer.py`, and `app/server.py`. The PostgreSQL Repository smoke remains **459** checks, `DB_BACKEND=postgres` remains disabled, and SQLite remains the operational backend.


## Stage 46: route, phone, and tariff history smoke

Stage 46 declares `STAGE_46_METHODS = ("list_phone_history", "list_route_history", "list_tariff_history")` and includes each method in `SMOKE_METHODS` exactly once. The synthetic migration fixture supplies deterministic history-only records, and the smoke verifies history shapes, ordering, missing-ID contracts, old/new route-phone matching, Decimal tariff values, and unchanged current entity state. The confirmed smoke `checks_count` is **497**.

## Stage 47: PostgreSQL company routing-setting event history smoke

`STAGE_47_METHODS = ("list_company_routing_setting_history",)` adds synthetic active/inactive campaign events and verifies active-only, company-scoped semantics across current/historical setting versions, aliases, and snapshot JSON. The confirmed smoke count is **522**. Audit counts are **112 / 58 / 3 / 50 / 1 / 95.08%**. Remaining deferred methods are `list_calling_company_history`, `list_calling_company_events`, and `count_calling_company_events`; `DB_BACKEND=postgres` remains disabled.


## Stage 48 PostgreSQL calling-company JSON history smoke

`STAGE_48_METHODS = ("list_calling_company_history",)` adapts this one-SELECT, read-only history method with backend-aware company ID extraction: SQLite uses `json_extract(cl.new_values, '$.calling_company_id')`; PostgreSQL uses `NULLIF(cl.new_values ->> 'calling_company_id', '')::BIGINT`. It combines direct calling-company logs and routing-event logs through one OR predicate, preserves `ORDER BY cl.changed_at DESC, cl.id DESC`, and returns the existing exact shape with `cl.summary AS comment`.

The deterministic fixture adds exactly three Stage 48 change-log rows: Demo Company direct history, Demo Company routing-event JSON history using the existing Stage 43 event, and isolated CI Manual Company direct history. Old/new values remain backend-native (SQLite TEXT; PostgreSQL JSONB/psycopg dict) and smoke normalizes only for assertions. The smoke verifies direct and JSON history, aliases, summary-as-comment, company isolation, missing-company `[]`, exact field order, and no Repository writes under `SET TRANSACTION READ ONLY`. The actual semantic smoke count is **540**.

Audit counts are **112 public / 59 smoke reads / 2 deferred reads / 50 writes / 1 infrastructure / 96.72%**. The remaining final read-only batch is `company_event_search_and_count`: `list_calling_company_events` and `count_calling_company_events`; it requires PostgreSQL JSONB extraction, literal text search, list/count predicate parity, pagination, and deterministic ordering. `DB_BACKEND=postgres` remains disabled and SQLite remains the operational backend.


## Stage 49 PostgreSQL Repository read-surface completion

`STAGE_49_METHODS` adds `list_calling_company_events` and `count_calling_company_events`, completing **112 / 61 / 0 / 50 / 1 / 100.0%** (public/smoke/deferred/writes/infrastructure/coverage) with **611** smoke checks. No deferred Repository reads remain. Runtime census is unchanged (53 SELECT, 65 writes, 32 schema/PRAGMA, 11 dynamic/unknown); 50 write methods remain, `DB_BACKEND=postgres` stays disabled, and Stage 50 is audit/write sequencing only.

## Stage 50 PostgreSQL write-surface sequencing status

Read coverage is 100%, while 50 Repository write methods remain. Stage 50 adds a machine-checked write plan; runtime direct SQL remains and `DB_BACKEND=postgres` is disabled. The recommended Stage 51 is the PostgreSQL write test harness and transaction foundation, before any domain write adaptation.

## Stage 51 PostgreSQL rollback-only write harness foundation

Stage 51 adds a CI-only PostgreSQL write harness after migration and the 611-check read-only smoke. It opens an explicit read-write transaction, uses the HLR override only as a synthetic `commit=False` caller-owned write, confirms the value inside that transaction, and rolls it back before verifying that the previous value remains. The harness never calls `conn.commit()`.

It separately verifies PostgreSQL's aborted-transaction rule after a deliberate missing-table `SELECT`, and verifies that `ROLLBACK TO SAVEPOINT` permits subsequent work before the whole transaction is rolled back. `DB_BACKEND=postgres` remains disabled; there is no runtime connection factory or production PostgreSQL path. Stage 52 may be the first small write adaptation batch only after this harness is green; read-only smoke remains **611** checks.

## Stage 52 PostgreSQL app-settings and HLR usage rollback smoke

`set_app_setting_value`, `delete_app_setting_value`, and `upsert_hlr_daily_usage` now use backend-aware placeholders and PostgreSQL-compatible UPSERTs only for rollback-only harness coverage. The harness verifies transaction-local visibility and full rollback restoration for app settings and HLR daily usage; it never commits. Read-only smoke remains **611** checks and the coverage audit remains **112 / 61 / 0 / 50 / 1 / 100.0%**. `DB_BACKEND=postgres` remains disabled and no runtime write enablement is included.

## Stage 53 PostgreSQL user/admin rollback write smoke

Stage 53 rollback-smokes the user/admin create, update, password, and permissions writes on PostgreSQL in a single caller-owned transaction. It verifies transaction-local visibility and authentication change, then full rollback removal of the probe user and permissions. The read-only smoke remains **611** checks and the coverage audit remains **112 / 61 / 0 / 50 / 1 / 100.0%**. `DB_BACKEND=postgres` is still disabled; this is not runtime write enablement.

## Stage 54 PostgreSQL core dictionary rollback write smoke

Stage 54 rollback-smokes `create_country`, `create_currency`, `create_provider`, and `create_prefix` in one PostgreSQL transaction and proves that all four rows disappear after rollback. Read-only smoke remains **611** checks and the coverage audit remains **112 / 61 / 0 / 50 / 1 / 100.0%**. `DB_BACKEND=postgres` remains disabled, and this introduces no production runtime write enablement.

## Stage 55 — dictionary get-or-create rollback smoke

The four dictionary `get_or_create_*` writes are PostgreSQL rollback-smoked only, including their create and existing paths. Read-only smoke remains 611 checks and the coverage audit remains 112/61/0/50/1/100%. `DB_BACKEND=postgres` remains disabled: this does not enable runtime PostgreSQL writes.

## Stage 56 PostgreSQL dictionary ensure rollback write smoke

Stage 56 rollback-smokes the three dictionary ensure writes on PostgreSQL: `ensure_project_exists`, `ensure_phone_number_type_exists`, and `ensure_phone_assignment_type_exists`. It verifies insert and existing/ignore paths and rollback cleanup only in the CI harness. The read-only smoke remains **611** checks and the coverage audit remains **112 / 61 / 0 / 50 / 1 / 100.0%**. `DB_BACKEND=postgres` remains disabled; this introduces no runtime write enablement.

## Stage 57 PostgreSQL dictionary server rollback write smoke

Stage 57 rollback-smokes `create_server` on PostgreSQL using a caller-owned transaction and verifies no server probe row remains after rollback. Read-only smoke remains **611** checks and the coverage audit remains **112 / 61 / 0 / 50 / 1 / 100.0%**. `DB_BACKEND=postgres` remains disabled and this adds no runtime write enablement.

## Stage 58: rollback-only change reasons

`create_change_reason` is PostgreSQL rollback-smoked with `_change_log` adapted as a private placeholder-only dependency. Read-only smoke remains at 611 checks and the coverage audit remains 112/61/0/50/1/100%. `DB_BACKEND=postgres` stays disabled; this does not enable production runtime writes.

## Stage 59: dictionary snapshot rollback smoke

`update_dictionary_snapshots` is rollback-smoked on PostgreSQL, including all six snapshot branches. The read-only smoke remains at 611 checks and the coverage audit remains 112/61/0/50/1/100%. `DB_BACKEND=postgres` remains disabled and this stage does not enable runtime writes.
