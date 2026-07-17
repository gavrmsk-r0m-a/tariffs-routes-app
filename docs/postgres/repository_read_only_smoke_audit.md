# Repository read-only PostgreSQL smoke audit (Stage 36)

This is a focused inventory of public Repository reads that were not in the
the earlier smoke stages. Each candidate was checked for SQL-only read behavior and for
calls to transactions, writes, application state, and external services.

| Method(s) | Purpose | PostgreSQL status | Blocker found | Stage decision | Reason |
| --- | --- | --- | --- | --- | --- |
| `get_app_setting_value` | Read one application setting by key. | small safe fix required | SQLite `?` placeholder | added to smoke | One backend placeholder; deterministic positive and missing fixtures. |
| `get_hlr_daily_usage` | Read the stored daily HLR usage summary. | small safe fix required | SQLite `?` placeholder | added to smoke | It is a pure `SELECT` with no HLR request or state change; the demo has a deterministic row. |
| `get_hlr_limit_override` | Read the optional stored HLR limit override. | ready after `get_app_setting_value` fix | no SQL beyond the underlying setting read | added to smoke | A deterministic synthetic setting gives this wrapper an exact positive assertion. |
| `list_calling_companies`, `get_calling_company` | List companies and retrieve one detail row. | small safe fix required | integer boolean literal in the list join; SQLite `?` placeholder in detail | added to smoke | The no-filter list and ID detail are deterministic on the synthetic company. |
| `latest_currency_rate`, `get_currency_rate` | Resolve the newest rate and retrieve it by ID. | small safe fix required | SQLite `?` placeholders | added to smoke | Direct lookups with an unambiguous synthetic EUR rate; no recalculation is invoked. |
| `dictionary_rename_preview` | Count references affected by a dictionary rename. | compatible | SQLite `?` placeholders and positional aggregate row access removed with backend placeholders and `COUNT(*) AS count` mapping access | added in Stage 35 | All six supported demo branches and the unknown-kind result are asserted independently. |
| `_user_columns` | Private schema introspection supporting user reads. | compatible | SQLite retains `PRAGMA table_info(users)` with mapping-style column-name access; PostgreSQL queries parameterized `information_schema.columns` in `current_schema()` | added in Stage 36 (supporting helper; not a public smoke method) | Uses the Repository connection only and performs no DDL or schema/search-path change. |
| `list_users`, `get_user`, `get_user_by_username` | User administration reads. | compatible | Backend placeholders and booleans; backend-specific ordering | added in Stage 36 | SQLite retains its exact `COLLATE NOCASE` ordering. PostgreSQL uses `LOWER(COALESCE(NULLIF(display_name, ''), username))`, then `LOWER(username)` and `id`; exact cross-backend locale equivalence is not claimed. Credential columns remain limited to the authentication lookup. |
| `authenticate_user` | Read a user and locally verify the stored password hash. | compatible | Depends on the adapted username lookup | added in Stage 36 | Audited before adaptation: it calls only `get_user_by_username` and `verify_password`; it issues no write, commit, rollback, last-login update, cookie, session, or other state change. |
| `get_user_section_permission`, `get_user_permissions` | Permission lookups. | compatible | SQLite `?` placeholders replaced; the demo user ID is obtained from the already-read calling-company row | added in Stage 35 | Pure permission `SELECT` methods; user schema introspection and authentication remain untouched. |
| `list_company_routing_settings`, `get_company_routing_setting` | List current campaign routing settings and retrieve one detail row. | blocked/deferred | SQLite-only search UDF/filter placeholders and integer boolean literal | deferred | The list exposes search/filter behavior that needs a separate PostgreSQL semantics decision. |
| `get_phone_number`, `get_route`, `route_numbers` | Phone/route details and route relation lookup. | compatible | Backend placeholders and parameterized active booleans added | added in Stage 35 | IDs come only from existing Repository results; no direct smoke SQL is used. In `route_numbers`, the prior columns retain their order and `usage_type`/`is_active` are trailing fields for relation semantics. |
| `find_tariff_by_identity`, `get_tariff` | Tariff identity and detail reads. | compatible | SQLite `?` placeholders replaced while nullable prefix identity semantics remain unchanged | added in Stage 35 | Identity is resolved before detail lookup; Decimal comparisons cover numeric values without fixed scale. |
| `list_routes`, `list_phone_numbers`, `list_tariffs`, `list_provider_changes`, `list_routing_events` | Operational filtered lists. | blocked/deferred | `query_filters` uses SQLite placeholders/UDF; integer booleans; `GROUP_CONCAT` in phone list | deferred | Search/filter semantics need a separately designed PostgreSQL batch. |
| `list_phone_history`, `list_route_history`, `list_tariff_history`, `list_calling_company_history`, `list_company_routing_setting_history` | History views. | blocked/deferred | SQLite placeholders; some JSON functions; history/business dependencies | deferred | History coverage is deliberately outside this small read batch. |
| `list_calling_company_events`, `count_calling_company_events` | Search and page the combined company event log. | blocked/deferred | SQLite-only `search_text_matches`, `json_extract`, placeholders and positional aggregate access | deferred | PostgreSQL Unicode search and JSON semantics require explicit design. |
| `list_routing_events`, `get_routing_event` | Routing-event list/detail. | blocked/deferred | complex filtering, SQLite placeholders/UDF and runtime business joins | deferred | Too broad for the safe batch; routing writes and application flows remain untested. |

All eight Stage 35 methods and the four public Stage 36 methods execute only `SELECT` (with local password verification for authentication), do not call `commit`,
`rollback`, `Repository.transaction`, or another write method, and have no HTTP,
session, Telegram, import, history-write, change-log, or external HLR side effect.

Still deferred are `create_user`, `update_user`, `update_user_password`,
`set_user_permissions`, password-reset flows, server login/cookies/session,
`query_filters` and `search_text_matches`;
filtered operational lists; history/JSON reads; routing-event reads; PostgreSQL
application runtime; and all Repository write paths.
