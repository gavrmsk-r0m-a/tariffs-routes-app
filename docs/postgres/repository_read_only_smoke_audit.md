# Repository read-only PostgreSQL smoke audit (through Stage 44)

Earlier stages added filtered `list_calling_companies`, `list_phone_numbers`, `list_company_routing_settings`, and now Stage 42 added `list_provider_changes` and Stage 43 adds `list_routing_events`/`get_routing_event` to the PostgreSQL Repository smoke.

Previously, Stage 39 added the filtered `list_calling_companies` read path to the PostgreSQL
Repository smoke. The unfiltered `list_calling_companies` path was already covered
from Stage 34; Stage 39 specifically covers server, country, literal name/external
ID search, current autorotation, and `cc.is_active` filters without enabling
PostgreSQL runtime, calling-company writes, routing-setting writes, events, history,
migration changes, or full application execution.

## `list_tariffs()` read-only audit and contract

`list_tariffs()` was audited before adaptation. The method performs one `SELECT`
with joins to countries, providers, optional provider prefixes, and currencies. It
does not call `commit()`, `rollback()`, `Repository.transaction()`, write Repository
methods, tariff mutation paths, tariff history, currency-rate recalculation,
`change_log`, HTTP, Telegram, HLR, import, or other external side-effect flows.

The preserved contract is:

- missing `status` key defaults to `"active"`;
- `status="active"` returns current tariffs only;
- `status="inactive"` returns inactive tariffs only;
- `status="all"`, `status=""`, and `status=None` omit the status predicate;
- `country_id` and `provider_id` equality filters are handled by `query_filters()`
  using the Stage 37 backend-aware placeholder foundation;
- result ordering remains `ORDER BY c.name, p.name, COALESCE(pp.prefix, '')`;
- no new tariff filters are added; `priority_status` is not a Stage 38 filter.

Active/inactive status predicates now bind backend-native boolean parameters:
SQLite receives `1`/`0`, PostgreSQL receives `True`/`False`. Boolean values are not
embedded directly in SQL, PostgreSQL-specific `IS TRUE`/`IS FALSE` is not used, and
unknown status strings keep the existing no-status-predicate behavior.

## Filter and search inventory

`query_filters()` is called by four Repository methods. Every call explicitly passes
`backend=self.backend`, while the helper retains its backward-compatible
`backend="sqlite"` default.

| Method | Equality filters | `*_like` filters | Stage 38 status / remaining blocker |
| --- | --- | --- | --- |
| `list_routes` | `country_id`, `provider_id`, `is_actual`; separate `prefix_id` | `search_like` | Added in Stage 37. Backend placeholders, native boolean values, prefix/null-prefix behavior, and phone-count boolean are smoked. |
| `list_tariffs` | `country_id`, `provider_id`; separate status logic | none | **Added in Stage 38.** Country/provider equality uses Stage 37 `query_filters()`; active/inactive status uses backend-native parameterized boolean; default active and all/empty/None contracts are preserved; numeric and strict boolean return semantics are checked. |
| `list_phone_numbers` | `country_id`, `provider_id`, `project`, `assignment_type`, `status`, `review_required` | `project_like`, `number_like` | **Added in Stage 40.** Uses backend-aware search and aggregation while preserving SQLite output. |
| filtered `list_calling_companies` | `server_id`, `country_id`, `has_autorotation`, `is_active` | `company_like`, `external_id_like` | **Added in Stage 39.** Server/country use backend-aware equality. Name/external ID use the Stage 37 literal substring search foundation. `has_autorotation` means the current active `company_routing_settings` value; no active setting means false. `is_active` means `cc.is_active`. Boolean filter values are normalized backend-aware, and base `cc.has_autorotation` never substitutes for the current setting. Exact Unicode locale equivalence is still not claimed. |

Direct `search_text_matches` SQL outside `query_filters()` remains intentionally
unchanged and deferred:

- `list_calling_company_events` and `count_calling_company_events`: JSON extraction,
  combined text search, placeholders, and event paging/count behavior;
- `list_routing_events` and `get_routing_event`: routing/history joins, search,
  JSON/runtime business dependencies, and SQLite placeholders.

`list_routing_events`/`get_routing_event`, history/JSON/event reads, PostgreSQL full
application runtime, and all write paths remain outside `SMOKE_METHODS`.

## Stage decision and safeguards

`STAGE_39_METHODS = ("list_calling_companies",)` and the expanded smoke performs
**259** semantic checks. Stage 39 coverage includes the existing unfiltered path plus
server/country equality filters, case-insensitive literal name and external-ID
substring filters, backend-aware boolean normalization for `"1"`/`1`/`True` and
`"0"`/`0`/`False`, ignored all/empty/None values, invalid nonempty boolean values
returning `[]`, current-vs-base autorotation assertions, no-active-setting false
fallback, combined boolean filters, full combined filter, output shape, and order.

`list_calling_companies()` was audited as read-only: it performs one SELECT, does not
call commit/rollback/`Repository.transaction()`, invokes no Repository write methods,
does not mutate `calling_companies` or `company_routing_settings`, writes no history,
`change_log`, or `routing_events`, calls no Telegram/HTTP/HLR/importer code, and does
not change application or session state. The smoke retains its recording proxy and
`SET TRANSACTION READ ONLY`; it executes no direct fixture SQL, Repository write, full
application flow, DDL, or migration logic. `query_filters` is a helper and is not a
smoke method. `list_routing_events`/`get_routing_event`, calling-company
event/history JSON paths, PostgreSQL full application runtime, and all write paths
remain deferred. PostgreSQL application runtime and `DB_BACKEND=postgres` remain
disabled, `psycopg` remains a lazy CI/smoke-only import, and SQLite remains the
operational production/development backend.

## Stage 40 — `list_phone_numbers`

Stage 40 adds `list_phone_numbers` to the PostgreSQL Repository read-only smoke while keeping the method read-only and preserving the existing SQLite output contract. The method uses the Stage 37 backend-aware `query_filters` foundation for equality and literal substring search filters in this order: `country_id`, `provider_id`, `project`, `project_like`, `assignment_type`, `status`, `number_like`, and `review_required`.

The `review_required` filter is normalized with the Repository private optional-boolean helper so SQLite receives `0`/`1` values and PostgreSQL receives native booleans. Unsupported non-empty values safely return an empty result without executing SQL.

Phone provider filtering keeps the existing `COALESCE(pn.provider_id, 0)` expression, so `provider_id=0` continues to mean phones without a provider. The `LEFT JOIN providers` remains in place; no synthetic provider is introduced for the no-provider fixture.

The `route_names` output remains the final text column. SQLite keeps `GROUP_CONCAT`, while PostgreSQL uses ordered `STRING_AGG`. Both backends aggregate only active `route_phone_numbers` links through a backend placeholder and `to_db_bool(True, backend)`. Phones with no active route links return `""`. No `r.is_actual` filter, `DISTINCT`, output field additions, or output reordering were introduced.

The observable contract remains `ORDER BY pn.number`. Case-insensitive literal substring matching is covered for the smoke fixtures, but exact Unicode locale equivalence is still not claimed.

Deferred areas after Stage 42 are phone write paths, phone/route history, `list_routing_events`/`get_routing_event`, calling-company event/history JSON paths, PostgreSQL full application runtime, and all write paths.

## Stage 41 company-routing settings list/detail smoke

Stage 41 adds `list_company_routing_settings` and `get_company_routing_setting` to the PostgreSQL Repository read-only smoke. The pre-change audit confirmed both methods are read paths: the list method builds a `SELECT` over `company_routing_settings`, `calling_companies`, countries, servers, routes, providers, and users, and the detail method performs a single `SELECT` by routing-setting ID. Neither method calls `commit()`, `rollback()`, `Repository.transaction()`, Repository write methods, Telegram/HTTP/HLR/importer code, or application/session state mutation.

The list contract remains current-only by default: when neither `include_history` nor `show_history` is enabled, rows must satisfy backend-aware `crs.is_active = true` plus `crs.valid_to IS NULL`. `include_history` and `show_history` are strict aliases. Only `True`, `1`, and `"1"` enable history; `False`, `0`, `"0"`, `None`, `""`, and `"all"` disable it; unsupported non-empty values safely return `[]` before SQL execution. In history mode, `is_active` accepts only the same strict true/false representations, while `None`, `""`, and `"all"` omit the predicate. Outside history mode, `is_active` does not weaken the current-only contract.

The public `company_id_external` filter is preserved and is internally routed through the Stage 37 backend-aware literal search foundation. SQLite continues to use `search_text_matches(cc.company_id_external, ?) = 1`; PostgreSQL uses `POSITION(LOWER(CAST(%s AS TEXT)) IN LOWER(COALESCE(CAST(cc.company_id_external AS TEXT), ''))) > 0`. LIKE/ILIKE wildcard behavior is intentionally not used, so `%`, `_`, and backslash remain literal input characters. Exact Unicode/locale equivalence between SQLite `casefold` and PostgreSQL `LOWER` is not claimed.

Output order and shape are preserved. The list keeps `ORDER BY c.name, s.name, cc.company_name, crs.valid_from DESC, crs.id DESC` and includes `updated_by_username`; the detail lookup keeps its previous column order and intentionally does not include `updated_by_username`.

Deferred after Stage 42 remain: create/update/deactivate company routing settings; company routing setting history based on `routing_events`; `list_routing_events`/`get_routing_event`; calling-company event/history JSON paths; PostgreSQL full application runtime; and all write paths.


Stage 42 adds the provider-change journal list read path to the PostgreSQL
Repository smoke while preserving SQLite as the operational backend and keeping
PostgreSQL runtime/write paths out of scope.

## Stage 42 provider-change list smoke

`list_provider_changes` is now included in PostgreSQL Repository smoke through
`STAGE_42_METHODS = ("list_provider_changes",)`. The method remains SELECT-only:
it does not call `commit()`, `rollback()`, `Repository.transaction()`, Repository
write methods, routing-event writes, provider-change writes, `change_log`, HLR,
importer, Telegram/HTTP, or application runtime flows.

The Stage 42 SQL contract is:

- SQLite uses ordered `GROUP_CONCAT` for `server_names`.
- PostgreSQL uses ordered `STRING_AGG` for `server_names`.
- `server_names` is built by a correlated subquery for both backends.
- The main provider-change query no longer uses server joins or `GROUP BY pcl.id`.
- Rows without linked servers preserve `server_names` as `NULL`; no `COALESCE` is
  used for this output.
- `provider_id` filters both sides of the change with
  `provider_before_id OR provider_after_id`, preserving the double-bound parameter
  contract.
- `route_like` searches both route-before and route-after names.
- `reason_like` searches `pcl.reason_text`.
- Route and reason search use the Stage 37 literal substring foundation via
  `query_filters`, so `%`, `_`, and other LIKE metacharacters are treated as input
  characters rather than wildcards.
- `date_from` and `date_to` remain inclusive bounds.
- Output shape remains `pcl.*` followed by the existing aliases, including
  `server_names` as the final alias.

The PostgreSQL smoke still opens its PostgreSQL transaction with
`SET TRANSACTION READ ONLY`. PostgreSQL application runtime remains disabled,
`DB_BACKEND=postgres` is not enabled, and all provider-change writes, routing
writes, migration logic, and full app runtime paths remain outside this stage.

The confirmed Stage 42 smoke count is **403** checks.

## Stage 43 routing-event list and detail smoke

Stage 43 adds `list_routing_events` and `get_routing_event` to the PostgreSQL Repository read-only smoke. Both methods are audited as read-only: `list_routing_events` builds and executes one SELECT-only list query, and `get_routing_event` calls that list path with `include_inactive=True` before optionally running one additional SELECT for server names when the event scope is `server_priority`. Neither method commits, rolls back, opens `Repository.transaction()`, calls write methods, mutates routing-event/server-priority/company-routing tables, writes history/change-log rows, or calls Telegram, HTTP, HLR, importer, application, or session state.

The Stage 43 list contract keeps default active-only results (`re.is_active` bound as a backend-native boolean), while strict `include_inactive` normalization accepts only `True`, `1`, and `"1"` to remove the active predicate. `False`, `0`, `"0"`, `None`, `""`, and `"all"` keep active-only behavior; unsupported non-empty values such as `"true"`, `"false"`, `"yes"`, and `"invalid"` return `[]` before SQL execution.

Date filters remain inclusive (`re.event_at >= placeholder`, `re.event_at <= placeholder`) and are not parsed inside the Repository. Equality filters for `country_id`, `apply_scope`, `calling_company_id`, and `provider_id` use `query_filters(..., backend=self.backend)` in the existing mapping order. The `server_id` filter preserves the current business semantics: `server_priority` events match either `re.server_id` or an `EXISTS` lookup in `routing_event_servers`, while `campaign_setting` events match through `cc.server_id`; `none` events are not promoted into server-scoped events.

The public `campaign_id` filter keeps the Stage 37 literal substring search foundation against `cc.company_id_external`: SQLite uses `search_text_matches(..., ?) = 1` with trimmed Python casefolded input, while PostgreSQL uses parameterized `POSITION(LOWER(CAST(%s AS TEXT)) IN LOWER(COALESCE(CAST(cc.company_id_external AS TEXT), ''))) > 0`. LIKE, ILIKE, regex, and wildcard semantics are not introduced.

The four current-tariff lookup predicates for old/new route and old/new company prices are backend-aware (`t.is_current = placeholder`). SQL parameters are ordered as four current-tariff booleans first, followed by active flag when present, date range, equality filters, three server-id values, and campaign search. Price CASE expressions, provider/country/prefix matching, `ORDER BY created_at DESC, id DESC`, `LIMIT 1`, and delta subtraction semantics are unchanged.

`snapshot_json` remains backend-native: SQLite can expose TEXT JSON and PostgreSQL/psycopg can expose JSONB as a dict. The smoke normalizes snapshots only for semantic assertions; runtime/UI JSON normalization is not enabled, and PostgreSQL full-application runtime remains disabled.

`get_routing_event` preserves the detail contract: found rows are returned as `dict`, missing IDs return `None`, and only `server_priority` details append trailing `affected_server_names` sorted by server name (for Stage 43, `Stage 42 Server A, Stage 42 Server B`). The list output shape and key order are explicitly guarded, and `affected_server_names` is not added to list rows.

The confirmed local semantic smoke `checks_count` is **459**. The PostgreSQL connection remains `SET TRANSACTION READ ONLY`; `DB_BACKEND=postgres` remains disabled, and SQLite remains the operational production/development backend. Deferred work still includes create/update/deactivate routing events, server-priority write application, campaign-routing write application, company routing-setting history, calling-company event/history JSON search, route/phone/tariff history methods, PostgreSQL full runtime, and all Repository write paths.

## Stage 44 machine-verifiable read-surface coverage audit

Stage 44 updates this audit to include a machine-verifiable classification/coverage gate for all public `Repository` methods. The gate derives the smoke-covered read category from `SMOKE_METHODS` and verifies that every other public method is classified exactly once in `docs/postgres/repository_method_coverage.json` as deferred read-only, write/mutating, or infrastructure/mixed.

No new Repository methods were added to the PostgreSQL Repository smoke in Stage 44, `SMOKE_METHODS` was not expanded, and the local semantic smoke `checks_count` remains **459**. The full list of remaining deferred read-only methods and recommended implementation batches is maintained in `docs/postgres/repository_read_surface_audit.md`.

The expected successful audit state is `unclassified == []` and `duplicate_classifications == []`. This Repository read-surface coverage does not mean the full PostgreSQL runtime application is ready: direct SQL remains in runtime modules, Repository write paths are still out of scope, `DB_BACKEND=postgres` remains disabled, and SQLite remains the operational backend.
