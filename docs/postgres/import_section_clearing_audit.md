# Stage 32 — destructive import section-clearing DELETE audit

## Scope and runtime status

This is a documentation-only audit of every section-clearing `DELETE` in
`app/importer.py`. No runtime code, schema, import behavior, validation message,
or counter is changed. PostgreSQL runtime remains disabled, no `psycopg` runtime
dependency is added, and SQLite remains the operational database.

The most important runtime fact is that clearing is currently **unreachable via
`apply_import()`**: `mode == "replace_section"` raises `BusinessRuleError` before
preview and before the later `_clear_section()` call. The second mode check and
call remain dead defensive/legacy code. Direct calls to the private helper are
still destructive, and a future re-enable would expose all risks below.

The audit found **11 `DELETE` executions (9 distinct SQL strings)**: the route branch has
five statements, tariff one, phone four, and calling company one. Two statements
(`route_phone_numbers` and `route_phone_number_history`) occur in both the route
and phone branches. Every statement is an unqualified whole-table delete: none
has a `WHERE` clause.

## Statement inventory

All entries are in `_clear_section(conn, entity_type)`. It is intended to be
called by `apply_import()` only for `mode == "replace_section"` and the selected
`entity_type`, but that mode is currently rejected first. `_clear_section()` has
no local `commit`, `rollback`, exception handling, or savepoint.

| Order | Branch / source area | SQL statement | Table / `WHERE` | Data removed and relations/history affected | Risk |
| ---: | --- | --- | --- | --- | --- |
| 1 | `entity_type == "routes"` | `DELETE FROM route_phone_numbers` | `route_phone_numbers`; no `WHERE` | Every route-to-phone operational link, including links to phones outside the imported route set. Child of both `routes` and `phone_numbers`; not itself a history table. | high |
| 2 | routes | `DELETE FROM route_phone_number_history` | `route_phone_number_history`; no `WHERE` | The entire historical audit trail for route/phone association changes. It references routes and up to three phone-number columns. History is deliberately destroyed before routes. | high |
| 3 | routes | `DELETE FROM route_history` | `route_history`; no `WHERE` | All route history, not merely history created by imports. This is a child of `routes`. | high |
| 4 | routes | `DELETE FROM server_route_priorities` | `server_route_priorities`; no `WHERE` | All server/country route priority state, including current, previous, and overflow route references. Downstream `routing_event_servers.server_route_priority_id` may restrict this delete. | blocker |
| 5 | routes | `DELETE FROM routes` | `routes`; no `WHERE` | Every route. The preceding list does not cover all route references: `company_routing_settings`, `provider_change_logs`, `routing_events`, and `routing_event_servers` can still reference routes with `ON DELETE RESTRICT`. | blocker |
| 1 | `entity_type == "tariffs"` | `DELETE FROM tariffs` | `tariffs`; no `WHERE` | Every current and non-current tariff; therefore all current-tariff flags disappear with their rows. `tariff_change_history` and `provider_change_logs` are not cleared and have restrictive tariff FKs. | blocker |
| 1 | `entity_type == "phone_numbers"` | `DELETE FROM route_phone_numbers` | `route_phone_numbers`; no `WHERE` | Every route-to-phone link across all routes. This destroys route association state as a side effect of replacing phones. | high |
| 2 | phone_numbers | `DELETE FROM route_phone_number_history` | `route_phone_number_history`; no `WHERE` | All route/phone link history, including records whose relevance is primarily a route. | high |
| 3 | phone_numbers | `DELETE FROM phone_number_history` | `phone_number_history`; no `WHERE` | All phone history before deleting its parent rows. | high |
| 4 | phone_numbers | `DELETE FROM phone_numbers` | `phone_numbers`; no `WHERE` | Every phone number. Its direct restrictive children named in the schema are cleared first, but the broad loss of links and history is irreversible after commit. | high |
| 1 | `entity_type == "calling_companies"` | `DELETE FROM calling_companies` | `calling_companies`; no `WHERE` | Every calling company. `company_routing_settings`, `provider_change_logs`, and `routing_events` are not cleared and can restrict the delete. | blocker |

The comments say business logs/`change_log` are intentionally retained.
`change_log.entity_id` is not an FK, so retained entries can describe IDs whose
entity rows no longer exist. Other retained “log” tables do have restrictive FKs
and therefore block parent deletion rather than becoming orphaned.

## Clearing groups

### A. Phone clearing

Found. Actual order is:

1. `route_phone_numbers` (child/link);
2. `route_phone_number_history` (history child);
3. `phone_number_history` (history child);
4. `phone_numbers` (parent).

The order satisfies the direct restrictive FKs from these three child tables to
`phone_numbers`. It nevertheless erases operational route links and both kinds
of history globally. It does not preserve association history for routes that
survive the phone replacement. Overall risk: **high**.

### B. Route clearing

Found. Actual order is:

1. `route_phone_numbers`;
2. `route_phone_number_history`;
3. `route_history`;
4. `server_route_priorities`;
5. `routes`.

This is partly child-before-parent, but incomplete. Related restrictive route
references in `company_routing_settings`, `provider_change_logs`,
`routing_events`, and `routing_event_servers` are not cleared or detached.
Furthermore, `routing_event_servers` can reference `server_route_priorities`, so
even step 4 can fail. Overall risk: **blocker**.

### C. Tariff clearing

Found: only `DELETE FROM tariffs`. It removes historical and current tariff rows
together; there is no special handling of `is_current`. It neither deletes nor
detaches `tariff_change_history`, and it does not clear the before/after tariff
references in `provider_change_logs`. Because those FKs use `ON DELETE RESTRICT`,
existing history/log rows can block it in SQLite with foreign keys enabled and
will block it in PostgreSQL. Overall risk: **blocker**.

### D. Calling company clearing

Found: only `DELETE FROM calling_companies`. There is no clearing for
`company_routing_settings`; additionally, `provider_change_logs.company_id` and
`routing_events.calling_company_id` are retained restrictive relations. This is
not child-before-parent and is expected to fail once referenced companies exist.
Overall risk: **blocker**.

### E. Dictionary clearing

No dictionary `DELETE` was found. The `entity_type == "dictionaries"` branch is
an explicit no-op and returns without changing data. Risk in the current helper:
**low**, and there is no clearing operation to extract.

## Ordering and PostgreSQL FK assessment

The implementation attempts child-before-parent ordering for route and phone
core tables, and it deletes history before the main records. Deleting history
first is necessary under the current `ON DELETE RESTRICT` schema, but it also
means audit evidence is the first data sacrificed. Tariff and calling-company
branches do not handle even their immediate history/settings children.

The code relies neither on cascades nor on deferred constraints: relevant schema
FKs are `ON DELETE RESTRICT`. Instead, it relies on a manually enumerated delete
order. That enumeration is incomplete for routes, tariffs, and companies.
PostgreSQL will enforce the restrictive references and can reject a statement;
SQLite does likewise when foreign-key enforcement is enabled. This is primarily
a correctness/data-policy blocker, not a SQL-dialect syntax issue.

There is also a cross-section ownership problem: route clearing destroys every
route-phone link/history; phone clearing destroys those same tables for every
route. Even when FK order succeeds, links/history that product policy may expect
to survive are removed. Because all deletes omit `WHERE`, “section” means an
entire entity table, not the rows represented by the incoming file, tenant,
country, server, or import job.

## Transaction, commit, rollback, and failure behavior

- `_clear_section()` issues its branch statements sequentially on the caller's
  connection. It does not commit after each delete and has no common local
  commit.
- It has no explicit transaction context, savepoint, or rollback.
- In the currently unreachable intended path, the statements would initially
  participate in the connection's current implicit transaction.
- `apply_import()` performs a final `conn.commit()` after its row loop. However,
  Repository row helpers may commit internally. Thus the first successful row
  operation that commits could also commit all preceding deletes; clearing and
  rebuilding are not guaranteed to be one atomic operation.
- Row exceptions are caught and converted into skipped/error counters, after
  which processing continues and the final commit still occurs. Consequently a
  hypothetical re-enabled replacement could commit a cleared section followed
  by only a partial rebuild.
- A failure inside `_clear_section()` itself occurs outside the row-level
  `try/except`, propagates, and skips `apply_import()`'s final commit. But there is
  no rollback, so earlier successful deletes remain pending on the connection;
  their eventual outcome depends on caller/connection cleanup or a later commit.
- There is no commit after each individual `DELETE`; atomicity should not be
  inferred from that fact because later helper commits can cross the boundary.

If replacement is ever approved, the Repository boundary should ultimately be
one explicit atomic **clear-and-rebuild** transaction (not merely a delete-only
method), with rollback on any delete or imported-row failure and Repository
helpers prevented from committing inside it. Before implementation, product
policy must decide which histories/logs/relations are retained, detached,
snapshotted, or deleted.

## Stage 33 recommendation

Choose **A — audit only; do not extract clearing yet**.

No isolated safe dictionary clearing exists. Extracting one of the destructive
groups would merely relocate unsafe behavior: three groups have unhandled
restrictive relations, phone clearing intentionally destroys cross-section
links/history, replacement mode is disabled, and tests currently establish that
the mode is rejected rather than validating successful atomic replacement.
Option B therefore has no suitable candidate. Option C is also unsafe because
the current flow is not a proven atomic clear-and-rebuild operation and is not
well covered for successful replacement.

Recommended Stage 33 work is a documentation/test-design policy stage: keep
replacement disabled; define retention semantics for history, `change_log`,
provider/routing logs and cross-section links; inventory transaction-owning
Repository calls; and specify rollback/FK integration tests for both SQLite and
the PostgreSQL schema draft. Only a later explicitly approved implementation
should introduce a single transaction-scoped Repository clear-and-rebuild
operation. A delete-only Repository extraction is not recommended.
