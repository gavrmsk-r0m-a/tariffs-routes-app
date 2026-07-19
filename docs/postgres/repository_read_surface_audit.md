# PostgreSQL read-surface audit — Stage 49

## Executive summary

Stage 44 adds a machine-verifiable, audit-only gate for the current PostgreSQL Repository read surface. Stage 45 hardens that gate with strict manifest metadata schema validation, stable configuration-error handling, and recursive runtime SQL census coverage. The audit statically parses `app/repository.py`, `scripts/postgres_repository_smoke.py`, and `docs/postgres/repository_method_coverage.json`; it does not import Repository code, execute top-level code, open databases, or rewrite inputs.

- Public `Repository` methods: **112**.
- Smoke-covered read methods: **61**.
- Deferred read-only methods: **0**.
- Write/mutating methods: **50**.
- Infrastructure/mixed methods: **1**.
- Unclassified methods: **0**.
- Duplicate classifications: **0**.
- Current local PostgreSQL Repository smoke semantic checks: **611**.
- Classified Repository read-surface coverage: **100.0%** (61 smoke-covered reads out of 61 classified read-only methods). This is not full application runtime readiness.

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
| `list_calling_company_events` | Calling-company event list/search page. | `sqlite_placeholder`, `json_text_vs_jsonb`, `search_text_matches`, `pagination_contract`, `requires_fixture`, `no_postgres_semantic_test` | `company_event_search_and_count` |
| `count_calling_company_events` | Count companion for calling-company event search. | `sqlite_placeholder`, `search_text_matches`, `requires_fixture`, `no_postgres_semantic_test` | `company_event_search_and_count` |

## Runtime SQL outside Repository

The direct runtime SQL census is intentionally informational. It proves that Repository smoke coverage is not the same as full application runtime PostgreSQL coverage while runtime modules still execute SQL directly. Stage 45 scans `app/**/*.py` recursively, excluding service/data directories such as `__pycache__`, `.venv`, `venv`, `data`, `backups`, and `logs`, while continuing to exclude `app/repository.py` by resolved path.

| Runtime area | Files/functions | SQL profile |
| --- | --- | --- |
| SQLite connection and lightweight migrations | `app/db.py` connection pragmas, schema checks, migrations, seed helpers | PRAGMA/DDL plus SELECT and write calls |
| Import pipeline | `app/importer.py` tariff import and section clearing helpers | UPDATE/DELETE calls |
| Web runtime/admin helpers | `app/server.py` option builders, demo-data helpers, dashboard/forms/admin POST handlers | SELECT, INSERT, UPDATE, and dynamic SQL calls |

Census totals: **53** SELECT calls, **65** write calls, **32** schema/PRAGMA calls, **11** dynamic/unknown calls, across **3** files: `app/db.py`, `app/importer.py`, and `app/server.py`.

## Stage 45 audit hardening

Stage 45 strictly validates the manifest top-level schema and every category entry. Invalid manifest configuration, parse/input errors, or an unreadable static `SMOKE_METHODS` literal return CLI exit code **2** with `status: error`; classification or coverage violations still return exit code **1** with `status: failed`; a valid audit returns exit code **0** with `status: ok`.

Deferred read-only entries require `reason`, non-empty unique `blockers`, and `recommended_batch`; write/mutating entries require `reason` and an allowed `mutation_kind`; infrastructure/mixed entries require `reason`. Unknown top-level keys or metadata fields are configuration errors so schema expansion requires a future `schema_version` bump.

## Stage 46 history smoke

Stage 46 moves `list_phone_history`, `list_route_history`, and `list_tariff_history` into the PostgreSQL read-only smoke. The deterministic synthetic fixture contains phone, route-phone replacement/addition, route, and tariff-created/tariff-changed history records without changing the current Demo Phone, Demo Route, or Demo Tariff state. The smoke has **497** semantic checks at this stage.

## Runtime boundary

PostgreSQL full runtime is still not ready. Repository writes are not adapted, direct SQL outside Repository still exists, `DB_BACKEND=postgres` remains disabled, and SQLite remains the operational production/development backend. The PostgreSQL smoke remains a read-only CI/smoke surface using `SET TRANSACTION READ ONLY`, not a full runtime enablement.

## Stage 47 company routing-setting event history smoke

Stage 47 moves `list_company_routing_setting_history` into the read-only smoke. It preserves company-scoped history semantics across current and historical settings, filters to active `campaign_setting` events with backend-aware placeholders/booleans, and validates exact aliases, ordering, and TEXT/JSONB snapshot behavior. The current local smoke has **522** semantic checks.

## Recommended next implementation Stage

The final read-only batch is **`company_event_search_and_count`** for `list_calling_company_events` and `count_calling_company_events`; it requires PostgreSQL JSONB extraction, literal text search, list/count predicate parity, pagination, and deterministic ordering.


## Stage 48 PostgreSQL calling-company JSON history smoke

`STAGE_48_METHODS = ("list_calling_company_history",)` adapts this one-SELECT, read-only history method with backend-aware company ID extraction: SQLite uses `json_extract(cl.new_values, '$.calling_company_id')`; PostgreSQL uses `NULLIF(cl.new_values ->> 'calling_company_id', '')::BIGINT`. It combines direct calling-company logs and routing-event logs through one OR predicate, preserves `ORDER BY cl.changed_at DESC, cl.id DESC`, and returns the existing exact shape with `cl.summary AS comment`.

The deterministic fixture adds exactly three Stage 48 change-log rows: Demo Company direct history, Demo Company routing-event JSON history using the existing Stage 43 event, and isolated CI Manual Company direct history. Old/new values remain backend-native (SQLite TEXT; PostgreSQL JSONB/psycopg dict) and smoke normalizes only for assertions. The smoke verifies direct and JSON history, aliases, summary-as-comment, company isolation, missing-company `[]`, exact field order, and no Repository writes under `SET TRANSACTION READ ONLY`. The actual semantic smoke count is **540**.

Audit counts are **112 public / 59 smoke reads / 2 deferred reads / 50 writes / 1 infrastructure / 96.72%**. The remaining final read-only batch is `company_event_search_and_count`: `list_calling_company_events` and `count_calling_company_events`; it requires PostgreSQL JSONB extraction, literal text search, list/count predicate parity, pagination, and deterministic ordering. `DB_BACKEND=postgres` remains disabled and SQLite remains the operational backend.


## Stage 49 calling-company event search/count smoke

This final Repository read-only batch adds `STAGE_49_METHODS = ("list_calling_company_events", "count_calling_company_events")`. Both SELECT-only methods share one private JOIN/predicate builder, use JSON number/string company-ID extraction (`NULLIF(cl.new_values ->> 'calling_company_id', '')::BIGINT` on PostgreSQL and SQLite `CAST(NULLIF(json_extract(...), '') AS INTEGER)`), and apply six-field literal case-insensitive substring search without LIKE. Smoke verifies list/count parity, output shape, excluded route/orphan routing rows, pagination, and tie ordering under `SET TRANSACTION READ ONLY`; the confirmed local check count is **611**.

Audit counts are **112 public / 61 smoke reads / 0 deferred reads / 50 writes / 1 infrastructure / 100.0%**. No deferred public Repository read-only methods remain. This does **not** mean full PostgreSQL runtime readiness: 50 Repository write/mutating methods and direct SQL in `app/db.py`, `app/importer.py`, and `app/server.py` remain; `DB_BACKEND=postgres` remains disabled and SQLite is operational. Recommended next stage: **Stage 50 — audit-only PostgreSQL write-surface sequencing and transaction plan**.
