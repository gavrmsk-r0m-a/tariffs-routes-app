# Repository read-only PostgreSQL smoke audit (Stage 34)

This is a focused inventory of public Repository reads that were not in the
Stage 33 smoke. Each candidate was checked for SQL-only read behavior and for
calls to transactions, writes, application state, and external services.

| Method(s) | Purpose | PostgreSQL status | Blocker found | Stage 34 decision | Reason |
| --- | --- | --- | --- | --- | --- |
| `get_app_setting_value` | Read one application setting by key. | small safe fix required | SQLite `?` placeholder | added to smoke | One backend placeholder; deterministic positive and missing fixtures. |
| `get_hlr_daily_usage` | Read the stored daily HLR usage summary. | small safe fix required | SQLite `?` placeholder | added to smoke | It is a pure `SELECT` with no HLR request or state change; the demo has a deterministic row. |
| `get_hlr_limit_override` | Read the optional stored HLR limit override. | ready after `get_app_setting_value` fix | no SQL beyond the underlying setting read | added to smoke | A deterministic synthetic setting gives this wrapper an exact positive assertion. |
| `list_calling_companies`, `get_calling_company` | List companies and retrieve one detail row. | small safe fix required | integer boolean literal in the list join; SQLite `?` placeholder in detail | added to smoke | The no-filter list and ID detail are deterministic on the synthetic company. |
| `latest_currency_rate`, `get_currency_rate` | Resolve the newest rate and retrieve it by ID. | small safe fix required | SQLite `?` placeholders | added to smoke | Direct lookups with an unambiguous synthetic EUR rate; no recalculation is invoked. |
| `dictionary_rename_preview` | Count references affected by a dictionary rename. | small safe fix required | SQLite `?` placeholders and positional aggregate row access | deferred | It has several kind-specific branches; converting and furnishing all branches is larger than this batch. |
| `list_users`, `get_user`, `get_user_by_username` | User administration reads. | blocked/deferred | `PRAGMA` via `_user_columns`, SQLite placeholders and `COLLATE NOCASE` | deferred | Requires a backend-aware schema-introspection and collation decision, not a narrow read fix. |
| `get_user_section_permission`, `get_user_permissions` | Permission lookups. | small safe fix required | SQLite `?` placeholders; suitable demo user ID is not exposed by an adapter-ready lookup | deferred | Safe future batch once user lookup/introspection is addressed. |
| `list_company_routing_settings`, `get_company_routing_setting` | List current campaign routing settings and retrieve one detail row. | blocked/deferred | SQLite-only search UDF/filter placeholders and integer boolean literal | deferred | The list exposes search/filter behavior that needs a separate PostgreSQL semantics decision. |
| `get_phone_number`, `get_route`, `route_numbers` | Phone/route details and route relation lookup. | small safe fix required | SQLite `?` placeholders; no adapter-ready natural-key lookup exposes route/phone IDs | deferred | Avoid direct SQL in the smoke and avoid broad list/search conversion merely to obtain IDs. |
| `find_tariff_by_identity`, `get_tariff`, `get_currency_rate`, `latest_currency_rate` | Tariff identity/detail and currency-rate reads. | mixed | tariff reads use SQLite `?`; rate reads need the same small fix | partially added | Rate methods are in this batch; tariff reads are deferred with tariff/recalculation work kept out of scope. |
| `list_routes`, `list_phone_numbers`, `list_tariffs`, `list_provider_changes`, `list_routing_events` | Operational filtered lists. | blocked/deferred | `query_filters` uses SQLite placeholders/UDF; integer booleans; `GROUP_CONCAT` in phone list | deferred | Search/filter semantics need a separately designed PostgreSQL batch. |
| `list_phone_history`, `list_route_history`, `list_tariff_history`, `list_calling_company_history`, `list_company_routing_setting_history` | History views. | blocked/deferred | SQLite placeholders; some JSON functions; history/business dependencies | deferred | History coverage is deliberately outside this small read batch. |
| `list_calling_company_events`, `count_calling_company_events` | Search and page the combined company event log. | blocked/deferred | SQLite-only `search_text_matches`, `json_extract`, placeholders and positional aggregate access | deferred | PostgreSQL Unicode search and JSON semantics require explicit design. |
| `list_routing_events`, `get_routing_event` | Routing-event list/detail. | blocked/deferred | complex filtering, SQLite placeholders/UDF and runtime business joins | deferred | Too broad for the safe batch; routing writes and application flows remain untested. |

All seven selected methods execute only `SELECT`, do not call `commit`,
`rollback`, `Repository.transaction`, or another write method, and have no HTTP,
session, Telegram, import, history-write, change-log, or external HLR side effect.
