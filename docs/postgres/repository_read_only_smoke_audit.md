# Repository read-only PostgreSQL smoke audit (Stage 37)

Stage 37 adds only the backend-aware filter foundation and one operational list,
`list_routes`, to the PostgreSQL read-only smoke. This is a compact classification
of the filter/search surface; it is not a claim that every caller is PostgreSQL-ready.

## Filter and search inventory

`query_filters()` is called by four Repository methods. Every call now explicitly
passes `backend=self.backend`, while the helper retains its backward-compatible
`backend="sqlite"` default.

| Method | Equality filters | `*_like` filters | Stage 37 status / remaining blocker |
| --- | --- | --- | --- |
| `list_routes` | `country_id`, `provider_id`, `is_actual`; separate `prefix_id` | `search_like` | **Stage 37 implementation target and smoke method.** Backend placeholders, native boolean values, prefix/null-prefix behavior, and phone-count boolean are adapted. |
| `list_phone_numbers` | `country_id`, `provider_id`, `project`, `assignment_type`, `status`, `review_required` | `project_like`, `number_like` | Deferred: `GROUP_CONCAT`, active boolean literals, aggregation, and additional boolean/search behavior remain SQLite-specific. |
| `list_tariffs` | `country_id`, `provider_id`; separate status logic | none | Deferred to a dedicated boolean/status-filter batch (`is_current = 1/0` remains). |
| filtered `list_calling_companies` | `server_id`, `country_id`, `has_autorotation`, `is_active` | `company_like`, `external_id_like` | Deferred to a separate filter batch. The already-smoked unfiltered path remains supported; filtered boolean expression compatibility is not claimed. |

Direct `search_text_matches` SQL outside `query_filters()` remains intentionally
unchanged and deferred:

- `list_calling_company_events` and `count_calling_company_events`: JSON extraction,
  combined text search, placeholders, and event paging/count behavior;
- `list_company_routing_settings`: hand-built equality/search filters, active
  boolean literals, and history mode;
- `list_provider_changes`: route-name and reason searches plus its broader list logic;
- `list_routing_events` and `get_routing_event`: routing/history joins, search,
  JSON/runtime business dependencies, and SQLite placeholders.

`list_company_routing_settings`, direct JSON/history searches, routing-event reads,
and all write paths therefore remain outside `SMOKE_METHODS`.

## Backend-aware search foundation

- SQLite intentionally retains the registered `search_text_matches` UDF and SQL
  form `search_text_matches(column, ?) = 1`. Its Python normalization is trim plus
  Unicode `casefold`; `NULL` haystacks are empty strings and substring search treats
  `%`, `_`, backslash, and other LIKE metacharacters literally.
- PostgreSQL performs parameterized literal substring search with
  `POSITION(LOWER(CAST(%s AS TEXT)) IN LOWER(COALESCE(CAST(column AS TEXT), ''))) > 0`.
  Input is trimmed only before binding; no `LIKE`, `ILIKE`, regex, SQL function,
  extension, or DDL is used.
- Mapping expressions are internal hardcoded Repository mappings, never user input;
  dynamic values remain parameters and mapping order determines clause/parameter order.
- SQLite uses Python Unicode casefold while PostgreSQL uses the database's `LOWER`.
  Exact Unicode/locale equivalence is not claimed. Expected case-insensitive contains
  behavior is covered for ordinary ASCII, digits, and standard Cyrillic; `%` and `_`
  have literal rather than wildcard semantics on both engines.

## Stage decision and safeguards

`STAGE_37_METHODS = ("list_routes",)` and the expanded smoke performs **156**
semantic checks (the existing 131 plus 25 route-list checks). Route coverage includes
unfiltered shape/values, individual and combined country/provider equality filters,
all specified `is_actual` representations, concrete/null/missing prefix filters,
case/trim/partial/missing searches, literal `_` and `%`, and the full combined filter.
Demo country, provider, and prefix IDs come only from earlier Repository reads.

The smoke retains its recording proxy and `SET TRANSACTION READ ONLY`; it executes no
direct fixture SQL, Repository write, full application flow, DDL, or migration logic.
`query_filters` is a helper and is not a smoke method. PostgreSQL application runtime
and `DB_BACKEND=postgres` remain disabled, `psycopg` remains a lazy CI/smoke-only
import, and SQLite remains the operational production/development backend.
