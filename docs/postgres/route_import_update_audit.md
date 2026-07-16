# Stage 26 — route import UPDATE focused audit and extraction plan

## Scope and conclusion

This is a documentation-only audit of the existing-route branch in `app/importer.py`.
Stage 26 does not change runtime code, SQL, CSV parsing, validation, counters, preview,
summary, history, or relation behavior. PostgreSQL runtime remains disabled and SQLite
remains the operational backend.

**Decision:** Stage 27 may safely extract **only the existing-route `UPDATE routes`**
into a narrow Repository method. The importer update branch has no adjacent
`route_history`, `change_log`, relation, priority, overflow, or phone-pool write to
move with it. The absence of update history is current observable behavior and must
be preserved; adding history would be a separate product/audit-policy change.

The candidate is medium risk rather than low risk because reference helpers commit,
the update itself commits immediately, failures are converted to skipped rows, and
the imported provider/AON metadata is operational data. These boundaries must not
move during extraction.

## End-to-end route UPDATE path

1. `apply_import()` rejects `mode="replace_section"` before preview or clearing.
2. `preview_import()` parses the CSV and calls `_business_key()` for every row.
   For routes the key is the literal `(country, name)` obtained from aliases
   `country`/`страна`/`гео` and `name`/`route`/`название маршрута`/`маршрут`.
   Empty country or name raises `BusinessRuleError("country and route name are required")`.
3. A repeated key in the same file is marked `duplicate_in_file` and skipped. For
   the first occurrence, `_exists()` delegates to
   `Repository.route_exists_by_country_name_and_name(country, name)`. Thus existence
   is determined by country **name** plus route name; the Repository query joins
   `routes` to `countries` and returns a boolean.
4. Preview records an existing route as `duplicate_in_db`, action `update`, message
   `str((country, name))`; it increments `duplicate_rows`. It does not resolve or
   validate provider/prefix values and performs no writes.
5. Apply parses the CSV again, recreates the business key, skips repeated in-file
   keys, repeats the existence check, and honors `duplicate_action="skip"` before
   `_apply_route()`.
6. `_apply_route()` resolves, in order:
   `country_id = get_or_create_country(country)`,
   `provider_id = get_or_create_provider(provider or "Unknown")`, and
   `provider_prefix_id = get_or_create_prefix(provider_id, prefix or None)`.
   These helpers may create reference rows and have their own immediate commits.
7. The existing branch executes the audited `UPDATE routes`, commits immediately,
   returns to `apply_import()`, and only then `updated_rows` is incremented.
8. Per-row exceptions are caught by `apply_import()`: `skipped_rows` is incremented
   and processing continues. Route errors do not increment `error_rows` and do not
   append a new apply-error preview row. After the loop, `apply_import()` calls a
   final `conn.commit()` and returns the same `ImportPreview` object populated with
   apply counters.

## Route-related write SQL inventory

### 1. Existing route update — extraction candidate

| Item | Observed behavior |
| --- | --- |
| Function / area | `app/importer.py`, `_apply_route(repo, row, user_id, exists=True)` |
| SQL type and table | `UPDATE routes` |
| Predicate | `WHERE country_id = ? AND name = ?`; name is not updated and there is no preliminary route-id lookup in this branch |
| Updated fields | `provider_id`, `provider_prefix_id`, `project_label`, `cli_source_type`, `cli_source_label`, `comment`, `updated_by`, and `updated_at = CURRENT_TIMESTAMP` |
| Value sources | Provider/country/prefix IDs come from the three Repository reference helpers. Project comes from `project_label`/`проект` or `NULL`. CLI type comes from `cli_source_type`/`тип аон` or `"other"`. CLI label comes from `cli_source_label`/`аон`/`источник аон` or `"OTHER"`. Comment comes from `comment`/`комментарий` or `NULL`. `updated_by` is the `user_id` passed to `apply_import()`. Country ID and imported route name identify the row. |
| Validation before write | `_business_key()` requires country and name; in-file duplicate handling and the database existence check run before `_apply_route()`. Reference helpers normalize/find-or-create country, provider and prefix. There is no importer-specific enum validation for CLI type; the database constraint can reject an unsupported value. |
| Counters | Preview: an existing DB key increments `duplicate_rows`; no apply counter changes occur in `_apply_route()`. Apply: successful return increments `updated_rows`; an exception increments `skipped_rows`. `created_rows` is unchanged. Route apply exceptions do not increment `error_rows`. `duplicate_action="skip"` increments `skipped_rows` without calling the update. |
| Preview | Generic row: `line`, `status="duplicate_in_db"`, `action="update"`, and tuple-string `message`. Preview does not show field deltas and does not resolve provider/prefix, so an apply-time constraint/reference failure can differ from preview. |
| Summary | The server's apply summary displays `created_rows`, `updated_rows`, `skipped_rows`, and `error_rows`. Preview displays total/new/duplicate/error counts. No summary row is persisted by importer SQL. |
| Commit / rollback | `_apply_route()` calls `repo.conn.commit()` immediately after the update; `apply_import()` also commits after all rows. There is no explicit `rollback()` or savepoint. Exceptions are caught per row, so Stage 27 must not introduce an implicit rollback or defer a successful update to the final batch commit. |
| User-facing messages | `country and route name are required`; generic preview statuses/actions above; database/Repository exception text can appear in preview's generic error `message`. Apply-time route exceptions only affect counts and are not appended as route error rows. The global disabled replacement message and unsupported import-type message surround, but are not specific to, this update. |
| History / change log | **None for the existing-route import update.** No `INSERT route_history` and no `change_log` call is adjacent to or triggered by this SQL. This differs from route creation and from the normal Repository route-edit method. |
| Risk | **medium** — narrow SQL, but operational provider/CLI data, immediate commit, reference-helper commits, DB constraints, and counter-on-exception behavior are observable. Not a blocker if extraction remains mechanical. |

The statement does not update `country_id`, `name`, `aon_pool`, `rnd_type`,
`rnd_pool_owner`, `is_actual`, `priority_status`, `inbound_line_available`,
`created_by`, or `created_at`.

### 2. New route creation (related write, not a Stage 27 candidate)

`_apply_route(..., exists=False)` calls `Repository.create_route()`. That method
inserts `routes`, then inserts a `route_history` row with action `created`, calls
`_change_log()` for `route.created`, and commits. It receives the same resolved
country/provider/prefix and imported name/project/CLI/comment values. Defaults in
the existing create method supply `is_actual=True`, `priority_status="unknown"`,
`inbound_line_available=False`, and null AON/RND pool fields.

Its success increments `created_rows`; an exception increments `skipped_rows`.
This is a **medium-risk, already Repository-backed** path and must remain outside
Stage 27. In particular, its create history/change-log side effects do not imply
that the legacy update path writes history.

### 3. Route section clearing (present but unreachable in normal apply)

`_clear_section(entity_type="routes")` contains, in order:

- `DELETE FROM route_phone_numbers`;
- `DELETE FROM route_phone_number_history`;
- `DELETE FROM route_history`;
- `DELETE FROM server_route_priorities`;
- `DELETE FROM routes`.

These statements have no local commit and would participate in the caller's final
commit. No explicit rollback exists. However, `apply_import()` currently raises
`Режим замены раздела временно отключён. Используйте Дополнить / обновить.` before
the `_clear_section()` call, making this batch unreachable through normal apply.
It is **high risk** because it destroys relations, history, priorities, and routes;
it is not part of the existing-route update extraction and must not be moved or
enabled in Stage 27.

### 4. Reference writes that can precede the route update

The three resolution calls are Repository-backed rather than direct SQL in
`importer.py`, but they are part of the route apply write path and therefore affect
its transaction boundary:

| Function / area | SQL type / tables | Inputs and fields | Validation, side effects, counters, commit | Risk |
| --- | --- | --- | --- | --- |
| `_apply_route` → `get_or_create_country()` | Possible `INSERT countries` (otherwise lookup only) | Imported `country` alias; the Repository controls normalized/default country fields | Runs after the repeated apply business-key validation and existence check. A newly created dictionary row commits inside the helper and can survive a later route-update failure. No preview detail or direct counter mutation; an exception becomes an apply skip. | medium in this flow because an “existing” key should normally imply the country already exists, but commit ordering must remain unchanged |
| `_apply_route` → `get_or_create_provider()` | Possible `INSERT providers` (otherwise lookup only) | Imported provider, falling back to literal `Unknown`; Repository supplies normalized/default provider fields | Preview does not resolve it. Creation commits immediately and can survive later failure. No route history/change-log or direct counter mutation; exception becomes an apply skip. | medium |
| `_apply_route` → `get_or_create_prefix()` | Possible `INSERT provider_prefixes` (otherwise lookup only) | Resolved provider ID and imported prefix or `None` | Runs after provider resolution, normalizes the prefix, and commits on creation. It does not change route relations/history/counters; exception becomes an apply skip. | medium |

These helpers are not Stage 27 extraction candidates and their ordering, defaults,
messages, and commits must stay in importer exactly as they are.

### 5. Writes not found in the existing-route update branch

There is no direct or Repository-mediated write to `route_history`,
`route_phone_numbers`, `route_phone_number_history`, `server_route_priorities`,
`routing_events`, or `change_log` in the existing update branch. There is also no
INSERT, DELETE, current/actual-flag transition, overflow/GSM transition, or AON pool
membership synchronization in that branch.

## Dependency map

| Dependency question | Finding |
| --- | --- |
| How is an existing route found? | The business key is `(country text, route name)`. `_exists()` calls `route_exists_by_country_name_and_name`; update then targets `(resolved country_id, same route name)`. It does not carry a route ID from the existence check. |
| Which route fields change? | Provider ID, provider-prefix ID, project label, CLI/AON source type and label, comment, updated-by, and updated-at only. |
| Is route history inserted? | No for update. Yes only on create through `create_route()`. The general UI-oriented Repository route update method does write history, but importer does not call it and it has materially different fields/concurrency semantics. |
| Relation to `route_phone_numbers`? | Existing update neither reads nor writes links. Existing links remain attached to the same route row. Only disabled section clearing deletes them. |
| Relation to `server_route_priorities`? | Existing update neither reads nor writes priorities. Rows referencing the route remain intact. Only disabled section clearing deletes them. |
| Overflow route / GSM overflow? | No update dependency. `has_overflow` and `overflow_route_id` live in priority/event flows and are untouched. Changing imported provider/CLI metadata does not synchronize an overflow configuration. |
| AON/pool fields? | Imported `cli_source_type` and `cli_source_label` are updated and may describe AON semantics. The actual `routes.aon_pool`, `rnd_type`, and `rnd_pool_owner` columns and route-phone pool links are not updated. |
| Current/actual flags? | `routes.is_actual`, `priority_status`, and `inbound_line_available` are not updated. There is no “current” route field in this SQL and no tariff-like closure. |
| Audit timestamps/users? | `updated_by=user_id` and `updated_at=CURRENT_TIMESTAMP` are set. Created audit fields remain unchanged. |
| Counters? | `created_rows`: create success only. `updated_rows`: existing update success. `duplicate_rows`: preview DB duplicates plus in-file duplicates. `skipped_rows`: apply in-file duplicate, duplicate-action skip, or caught apply exception. `error_rows`: preview validation/SQL-resolution errors; route apply exceptions do not add to it. |
| Preview versus apply? | Preview is read-only and validates only the business key/existence for routes. Apply repeats those checks, resolves/possibly creates references, executes SQL, and updates apply counters. Therefore preview can announce an update that apply later skips on an exception. |

## Transaction and failure semantics

- There is no transaction wrapped around the whole import and no per-row savepoint.
- Reference get-or-create helpers can commit before the route update. Consequently a
  reference row can remain committed even if the later route update fails.
- A successful route update is committed inside `_apply_route()` before
  `updated_rows` is incremented.
- A failed update/commit is caught by the outer row loop, which increments
  `skipped_rows` and continues. No explicit rollback cleans the connection.
- A final `conn.commit()` occurs outside the row-level `try` after the loop.
- The current SQL does not inspect `cursor.rowcount`. With the preceding existence
  check it normally updates one row due to `UNIQUE(country_id, name)`, but a
  concurrent deletion or otherwise stale check could yield zero and the importer
  would still count the helper's normal return as updated. Stage 27 may return
  rowcount for Repository API consistency, but importer must initially ignore it to
  preserve this edge behavior.

## Stage 27 Repository boundary

Choose **Variant A**: mechanically extract only `UPDATE routes`. Keep all parsing,
aliases, defaults, business-key/existence logic, reference resolution, branching,
counter changes, exception handling, and create behavior in importer. Keep update
history absent.

It is both possible and preferable to extract the update without history: there is
no update-history operation to preserve. Do **not** combine this with the existing
general `update_route()` method, because that method selects a snapshot, supports
optimistic concurrency, updates additional fields, computes diffs, and writes
`route_history`. UPDATE + history must therefore **not** be introduced together.

Preserve the immediate commit with a default `commit=True` argument. The importer
should call the method with its default, just as `_apply_route()` commits today.
No extra SELECT should be added and the returned rowcount should not drive counters.

### Candidate method draft

The factual predicate is country ID plus name, so a route-ID signature would require
an extra lookup and is deliberately rejected:

```python
def update_route_import_fields(
    self,
    *,
    country_id: int,
    name: str,
    provider_id: int,
    provider_prefix_id: int | None,
    project_label: str | None,
    cli_source_type: str,
    cli_source_label: str,
    comment: str | None,
    updated_by: int | None,
    commit: bool = True,
) -> int:
    ...
```

Implementation constraints for Stage 27:

- business-specific and narrow; no generic update helper;
- use `placeholder(self.backend)` rather than hard-coded SQLite placeholders;
- return `cursor.rowcount`, with no extra SELECT;
- commit only when `commit=True`;
- do not update counters or add exception translation in the method;
- there are no boolean parameters in the factual SQL, so `to_db_bool` is not
  applicable. If scope changes to include any boolean field, that value must use
  `to_db_bool(value, self.backend)`—but expanding fields is not recommended;
- retain the database's timestamp expression appropriate to the supported adapter
  convention without enabling PostgreSQL runtime.

## Focused test plan for Stage 27

### Repository tests

1. Create a route, call `update_route_import_fields()`, and assert every factual
   field changes while country/name, AON/RND fields, actual/priority/inbound flags,
   and created audit fields remain unchanged.
2. Assert an existing route returns rowcount `1` and `updated_by`/`updated_at` are
   populated as before.
3. Call the method for a missing `(country_id, name)` and assert rowcount `0`, no
   exception, and no extra row. This matches the direct UPDATE's behavior.
4. Assert `commit=True` makes the update visible across SQLite connections; add a
   focused `commit=False` transaction test if that option is part of the method.
5. The candidate has no boolean fields. Keep the adapter write-method suite's
   existing SQLite `to_db_bool` coverage; do not invent an importer boolean merely
   to test 1/0. If a boolean enters the final signature, explicitly assert SQLite
   stores `1`/`0`.
6. Assert no `route_history` or `change_log` row is added by this method.

### Importer tests

1. Strengthen the existing-route test to import distinct provider, prefix, project,
   CLI source type/label, comment, and actor values, and assert exactly the same
   columns are updated.
2. Assert preview remains `(total=1, duplicate=1, new=0, errors=0)` with
   `duplicate_in_db`/`update` and the unchanged tuple message; assert apply remains
   `(created=0, updated=1, skipped=0)`.
3. Assert `duplicate_action="skip"` leaves the row unchanged and increments only
   `skipped_rows`; assert a duplicate key later in the same CSV is skipped.
4. Assert missing country/name preserves `country and route name are required` and
   preview error shape. Add an invalid CLI-type apply case to pin the current
   skipped/error counter distinction and unchanged validation/constraint message
   handling.
5. Assert the existing route's `route_history` count is unchanged by update, while
   the new-route path still creates its current history/change-log entries.
6. Include one new and one existing route in one import to confirm create/update and
   duplicate behavior remain independent.
7. Assert existing `route_phone_numbers`, `server_route_priorities`, and overflow
   references survive the update unchanged; no relation fixtures should be created
   or modified by the candidate method itself.
8. Preserve the immediate-commit boundary with a focused connection/transaction
   test or a commit-spy test; do not rely only on the final `apply_import()` commit.

## Recommended Stage 27

Implement only `Repository.update_route_import_fields()` and replace the one direct
existing-route UPDATE/commit pair in `_apply_route()` with that call. Add the focused
Repository and importer coverage above. Do not touch route creation, general route
editing/history, relations, priorities, overflow, section clearing, counters,
preview/summary formatting, PostgreSQL runtime, or dependencies.
