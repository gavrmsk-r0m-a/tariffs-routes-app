# PostgreSQL read-surface audit — Stage 46

## Executive summary

Stage 44 adds a machine-verifiable, audit-only gate for the current PostgreSQL Repository read surface. Stage 45 hardens that gate with strict manifest metadata schema validation, stable configuration-error handling, and recursive runtime SQL census coverage. The audit statically parses `app/repository.py`, `scripts/postgres_repository_smoke.py`, and `docs/postgres/repository_method_coverage.json`; it does not import Repository code, execute top-level code, open databases, or rewrite inputs.

- Public `Repository` methods: **112**.
- Smoke-covered read methods: **57**.
- Deferred read-only methods: **4**.
- Write/mutating methods: **50**.
- Infrastructure/mixed methods: **1**.
- Unclassified methods: **0**.
- Duplicate classifications: **0**.
- Current local PostgreSQL Repository smoke semantic checks: **497**.
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

Stage 46 moves `list_phone_history`, `list_route_history`, and `list_tariff_history` into the PostgreSQL read-only smoke. The deterministic synthetic fixture contains phone, route-phone replacement/addition, route, and tariff-created/tariff-changed history records without changing the current Demo Phone, Demo Route, or Demo Tariff state. The smoke now has **497** semantic checks.

The next deferred Repository method is **`list_company_routing_setting_history`**.

## Runtime boundary

PostgreSQL full runtime is still not ready. Repository writes are not adapted, direct SQL outside Repository still exists, `DB_BACKEND=postgres` remains disabled, and SQLite remains the operational production/development backend. The PostgreSQL smoke remains a read-only CI/smoke surface using `SET TRANSACTION READ ONLY`, not a full runtime enablement.
