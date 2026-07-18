# Repository read-only PostgreSQL smoke audit (Stage 39)

Stage 39 adds the filtered `list_calling_companies` read path to the PostgreSQL
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
| `list_phone_numbers` | `country_id`, `provider_id`, `project`, `assignment_type`, `status`, `review_required` | `project_like`, `number_like` | Deferred: `GROUP_CONCAT`, active boolean literals, aggregation, and additional boolean/search behavior remain SQLite-specific. |
| filtered `list_calling_companies` | `server_id`, `country_id`, `has_autorotation`, `is_active` | `company_like`, `external_id_like` | **Added in Stage 39.** Server/country use backend-aware equality. Name/external ID use the Stage 37 literal substring search foundation. `has_autorotation` means the current active `company_routing_settings` value; no active setting means false. `is_active` means `cc.is_active`. Boolean filter values are normalized backend-aware, and base `cc.has_autorotation` never substitutes for the current setting. Exact Unicode locale equivalence is still not claimed. |

Direct `search_text_matches` SQL outside `query_filters()` remains intentionally
unchanged and deferred:

- `list_calling_company_events` and `count_calling_company_events`: JSON extraction,
  combined text search, placeholders, and event paging/count behavior;
- `list_company_routing_settings`: hand-built equality/search filters, active
  boolean literals, and history mode;
- `list_provider_changes`: route-name and reason searches plus its broader list logic;
- `list_routing_events` and `get_routing_event`: routing/history joins, search,
  JSON/runtime business dependencies, and SQLite placeholders.

`list_phone_numbers`,
`list_company_routing_settings`, `list_provider_changes`,
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
smoke method. `list_phone_numbers` remains deferred because of GROUP_CONCAT and
additional boolean/search paths; `list_company_routing_settings`,
`list_provider_changes`, `list_routing_events`/`get_routing_event`, calling-company
event/history JSON paths, PostgreSQL full application runtime, and all write paths
remain deferred. PostgreSQL application runtime and `DB_BACKEND=postgres` remain
disabled, `psycopg` remains a lazy CI/smoke-only import, and SQLite remains the
operational production/development backend.
