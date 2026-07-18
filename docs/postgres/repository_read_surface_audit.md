# PostgreSQL read-surface audit — Stage 46

## Executive summary

Stage 46 updates the machine-verifiable, audit-only gate for the current PostgreSQL Repository read surface after adding route, phone, and tariff history smoke coverage. The audit statically parses `app/repository.py`, `scripts/postgres_repository_smoke.py`, and `docs/postgres/repository_method_coverage.json`; it does not import Repository code, open databases, or rewrite inputs.

- Public `Repository` methods: **112**.
- Smoke-covered read methods: **57**.
- Deferred read-only methods: **4**.
- Write/mutating methods: **50**.
- Infrastructure/mixed methods: **1**.
- Unclassified methods: **0**.
- Duplicate classifications: **0**.
- Current local PostgreSQL Repository smoke semantic checks: **490**.
- Classified Repository read-surface coverage: **93.44%** (57 smoke-covered reads out of 61 classified read-only methods). This is not full application runtime readiness.

## Covered Repository read surface

The existing PostgreSQL Repository smoke covers adapter-ready read groups without adding Stage 44 smoke methods or semantic assertions:

- dictionaries/lookups;
- users/auth;
- routes;
- tariffs;
- phone numbers;
- calling companies;
- company routing settings;
- provider changes;
- routing events.

## Remaining deferred reads

| Method | Purpose | Blockers | Recommended batch |
| --- | --- | --- | --- |
| `list_calling_company_history` | Calling-company history with JSON snapshots. | `sqlite_placeholder`, `json_text_vs_jsonb`, `history_shape`, `requires_fixture`, `no_postgres_semantic_test` | `company_history_json` |
| `list_calling_company_events` | Calling-company event list/search page. | `sqlite_placeholder`, `json_text_vs_jsonb`, `search_text_matches`, `pagination_contract`, `requires_fixture`, `no_postgres_semantic_test` | `company_event_search_and_count` |
| `count_calling_company_events` | Count companion for calling-company event search. | `sqlite_placeholder`, `search_text_matches`, `requires_fixture`, `no_postgres_semantic_test` | `company_event_search_and_count` |
| `list_company_routing_setting_history` | Company routing-setting history from routing events. | `sqlite_placeholder`, `history_shape`, `requires_fixture`, `no_postgres_semantic_test` | `routing_setting_event_history` |

## Runtime SQL outside Repository

The direct runtime SQL census is intentionally informational. It proves that Repository smoke coverage is not the same as full application runtime PostgreSQL coverage while runtime modules still execute SQL directly.

| Runtime area | Files/functions | SQL profile |
| --- | --- | --- |
| SQLite connection and lightweight migrations | `app/db.py` connection pragmas, schema checks, migrations, seed helpers | PRAGMA/DDL plus SELECT and write calls |
| Import pipeline | `app/importer.py` tariff import and section clearing helpers | UPDATE/DELETE calls |
| Web runtime/admin helpers | `app/server.py` option builders, demo-data helpers, dashboard/forms/admin POST handlers | SELECT, INSERT, UPDATE, and dynamic SQL calls |

Census totals: **53** SELECT calls, **65** write calls, **32** schema/PRAGMA calls, **11** dynamic/unknown calls, across **3** files: `app/db.py`, `app/importer.py`, and `app/server.py`.

## Recommended next implementation Stage

Recommended next implementation batch: **`routing_setting_event_history`**.

Next concrete method:

- `list_company_routing_setting_history`

Why this batch is next:

- It is one SELECT-only method.
- It does not require calling-company JSON search/count pagination.
- It uses the already adapted routing-events domain.
- It is the smallest coherent remaining read batch.

Company JSON/history search methods remain intentionally deferred to later stages.

## Runtime boundary

PostgreSQL full runtime is still not ready. Repository writes are not adapted, direct SQL outside Repository still exists, `DB_BACKEND=postgres` remains disabled, and SQLite remains the operational production/development backend. The PostgreSQL smoke remains a read-only CI/smoke surface using `SET TRANSACTION READ ONLY`, not a full runtime enablement.

## Stage 46 executive summary

- Public Repository methods: 112.
- Smoke-covered reads: 57.
- Deferred read-only methods: 4.
- Write/mutating methods: 50.
- Infrastructure/mixed methods: 1.
- Read-surface coverage: 93.44%.
- Unclassified methods: 0.
- Current semantic smoke `checks_count`: 490.

Remaining deferred reads are `list_calling_company_history`, `list_calling_company_events`, `count_calling_company_events`, and `list_company_routing_setting_history`. The recommended next implementation batch is `routing_setting_event_history`, beginning with `list_company_routing_setting_history`; company JSON/history search methods remain deferred to later stages.
