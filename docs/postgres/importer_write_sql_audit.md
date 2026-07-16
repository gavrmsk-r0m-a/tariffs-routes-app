# Stage 22 — importer write SQL audit and Repository extraction plan

## Scope and guardrails

Stage 22 is documentation/read-only planning for the remaining direct SQL in `app/importer.py` after the Stage 19–21 read-only cleanup.

Guardrails for this audit:

- no runtime code changes;
- no `app/importer.py`, `app/repository.py`, or `app/server.py` changes;
- no CSV parsing changes;
- no validation message changes;
- no preview or summary counter changes;
- no PostgreSQL runtime enablement;
- SQLite remains the operational database.

## Executive summary

`app/importer.py` has no remaining direct read-only `SELECT` statements. The remaining direct SQL is write/import flow SQL:

- 17 direct write SQL statements were found in `app/importer.py`:
  - 5 direct import update/history statements in entity apply paths;
  - 3 direct dictionary `INSERT OR IGNORE` statements;
  - 9 direct section-clearing `DELETE` statements.
- No direct `import_jobs`, `preview_data`, `summary`, or `error_report` writes were found in `app/importer.py`.
- No direct `change_log` writes were found in `app/importer.py`; change-log side effects happen through Repository create/update helpers that importer already calls.
- Stage 23 should extract only 1–2 low-risk, isolated write helpers, preferably dictionary `INSERT OR IGNORE` helpers.

## Transaction and counter model observed

The current importer write flow is intentionally mixed:

- `apply_import()` builds preview first, then loops parsed rows, then calls entity-specific apply helpers.
- Several existing Repository create helpers commit internally.
- Several direct importer update helpers also commit internally after a row update.
- `apply_import()` performs a final `conn.commit()` after the loop.
- Row-level exceptions are caught in `apply_import()`, increment `skipped_rows`, and for phone-number imports can increment `error_rows`.
- Preview and summary counters are computed outside the direct SQL statements and must remain unchanged during extraction.

Because commit boundaries are observable import behavior, any Repository extraction must preserve the current commit/rollback behavior for the specific helper being moved.

## Classification by requested group

### A. Dictionary insert-or-ignore paths

Found. These are the lowest-risk direct write SQL statements in `app/importer.py`.

| File | Function / area | SQL type | Tables | What it does | Side effects | Transaction participation | Validation order impact | Preview/summary counter impact | Risk | Safe to extract? | Recommended future stage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `app/importer.py` | `_apply_dictionary`, `kind == "project"` | Repository insert-ignore helper | `projects` | Adds an active project dictionary value if it does not already exist. | May create one dictionary row; no history/change-log write in current path. | Commits immediately through `Repository.ensure_project_exists()`, matching the previous immediate commit. | No, after `_business_key()` validates `type` and `name`. | No direct counter changes; `apply_import()` increments created/updated after helper returns. | low | Extracted in Stage 23. | Stage 23 extracted. |
| `app/importer.py` | `_apply_dictionary`, `kind == "phone_type"` | Repository insert-ignore helper | `phone_number_types` | Adds an active phone-number type dictionary value if missing. | May create one dictionary row; no history/change-log write in current path. | Commits immediately through `Repository.ensure_phone_number_type_exists()`, matching the previous immediate commit. | No, after `_business_key()` validates `type` and `name`. | No direct counter changes. | low | Extracted in Stage 23. | Stage 23 extracted. |
| `app/importer.py` | `_apply_dictionary`, `kind == "phone_assignment"` | Repository insert-ignore helper | `phone_assignment_types` | Adds an active phone assignment type by `code` and `name` if missing. | May create one dictionary row; no history/change-log write in current path. | Commits immediately through `Repository.ensure_phone_assignment_type_exists()`, matching the previous immediate commit. | No, the importer still resolves `code = _first(..., "code", "код") or name` before calling the helper. | No direct counter changes. | low | Extracted in Stage 24. | Stage 24 extracted. |

Assessment: Stage 24 completed the remaining low-risk dictionary insert-or-ignore candidate. `projects`, `phone_number_types`, and `phone_assignment_types` now use narrow Repository helpers with immediate commit behavior preserved; the phone assignment `code` fallback stays in the importer and is covered by focused tests.

### B. Section clearing

Found, but currently inactive from normal runtime because `apply_import()` raises `BusinessRuleError` before `_clear_section()` when `mode == "replace_section"`. The helper still contains direct `DELETE` SQL and should be treated as high-risk because enabling or moving it incorrectly could destroy section data.

| File | Function / area | SQL type | Tables | What it does | Side effects | Transaction participation | Validation order impact | Preview/summary counter impact | Risk | Safe to extract? | Recommended future stage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `app/importer.py` | `_clear_section`, `entity_type == "routes"` | section clearing / `DELETE` batch | `route_phone_numbers`, `route_phone_number_history`, `route_history`, `server_route_priorities`, `routes` | Clears route data and related route link/history/priority rows. | Destructive; intentionally does not clear business logs/change_log. | No local commit; relies on caller transaction/final commit. | Potentially high if replacement mode is ever re-enabled. | Could affect created/updated counts if replacement mode is re-enabled. | high | Not for Stage 23. Extract only after replacement-mode policy is decided. | Later dedicated section-clearing stage. |
| `app/importer.py` | `_clear_section`, `entity_type == "tariffs"` | section clearing / `DELETE` | `tariffs` | Clears tariffs. | Destructive; tariff history/change_log not cleared here. | No local commit. | Potentially high if replacement mode is re-enabled. | Could affect import counters if replacement mode is re-enabled. | high | Not for Stage 23. | Later dedicated section-clearing stage. |
| `app/importer.py` | `_clear_section`, `entity_type == "phone_numbers"` | section clearing / `DELETE` batch | `route_phone_numbers`, `route_phone_number_history`, `phone_number_history`, `phone_numbers` | Clears phone numbers and dependent route phone links/history. | Destructive; can remove phone history; business logs/change_log intentionally not cleared. | No local commit. | Potentially high if replacement mode is re-enabled. | Could affect import counters if replacement mode is re-enabled. | high | Not for Stage 23. | Later dedicated section-clearing stage after explicit tests. |
| `app/importer.py` | `_clear_section`, `entity_type == "calling_companies"` | section clearing / `DELETE` | `calling_companies` | Clears calling companies. | Destructive; related routing settings are not directly cleared here and may rely on schema constraints/policy. | No local commit. | Potentially high if replacement mode is re-enabled. | Could affect import counters if replacement mode is re-enabled. | medium | Not for Stage 23. | Later dedicated section-clearing stage. |
| `app/importer.py` | `_clear_section`, `entity_type == "dictionaries"` | section clearing | none | No-op by design; dictionary replacement remains conservative. | None. | None. | No. | No. | low | No extraction needed. | Keep as is until replacement mode is revisited. |

### C. Phone number import writes

Found. The create path already goes through `Repository.create_phone_number()`, but the existing update path still contains direct write SQL in importer.

| File | Function / area | SQL type | Tables | What it does | Side effects | Transaction participation | Validation order impact | Preview/summary counter impact | Risk | Safe to extract? | Recommended future stage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `app/importer.py` | `_apply_phone`, existing phone update | `UPDATE` | `phone_numbers` | Updates country, provider, project, assignment, status, active flag, costs, rates, currency, type, tariff label, comment, review flag, imported creator, deactivation timestamp, and audit fields for an existing normalized phone number. | Can set or clear `deactivated_at`; can preserve or update `imported_created_by`; affects operational phone state. | Commits after update and history insert. | Medium/high: validation/reference resolution happens before write; extraction must not reorder `validate_phone_number()`, `_phone_reference_ids()`, or `_phone_import_values()`. | No direct counter changes, but exceptions control skipped/error counters. | high | Yes eventually, but not as Stage 23; requires focused tests around status/deactivation/imported creator/review behavior. | Later phone-update extraction stage. |
| `app/importer.py` | `_apply_phone`, update history | history/log write / `INSERT` | `phone_number_history` | Adds an import update history row for the existing phone number. | User-visible history; comment can include imported-created-by delta. | Same immediate commit boundary as phone update. | Medium: history details depend on prior identity lookup and update decision. | No direct counter changes, but failures affect skipped/error counters. | high | Extract together with phone update, not separately. | Later phone-update extraction stage. |
| `app/importer.py` | `_apply_phone`, new phone create | batch write through Repository | `phone_numbers`, `phone_number_history`, `change_log` | Calls `Repository.create_phone_number()` for new numbers. | Creates phone row, phone history, and change log through Repository. | Repository helper commits internally. | Existing Repository path; do not change in Stage 22/23. | No direct counter changes inside helper; success drives created counter. | medium | Already repository-backed. | No Stage 23 work. |

### D. Route import writes

Found. The create path is Repository-backed, and Stage 27 moved the existing update
path behind a narrow Repository method.

> **Stage 27 status:** the existing-route UPDATE audited in Stage 26 is now extracted
> to `Repository.update_route_import_fields()`. Only the `UPDATE routes` statement
> moved; route create/history and relation writes remain classified for later work.
>
> **Stage 26 status:** the existing-route UPDATE was placed under focused audit in
> [`route_import_update_audit.md`](route_import_update_audit.md). Its recommended
> narrow extraction was completed in Stage 27 with the immediate commit, no-history
> behavior, counters, and relation behavior preserved.

| File | Function / area | SQL type | Tables | What it does | Side effects | Transaction participation | Validation order impact | Preview/summary counter impact | Risk | Safe to extract? | Recommended future stage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `app/repository.py` | `update_route_import_fields`, existing route update | `UPDATE` | `routes` | Updates provider, provider prefix, project label, CLI source type/label, comment, updated_by, and updated_at for an existing `(country_id, name)` route. | Changes active routing metadata; unlike create path, no route history/change_log is written here today. | Default `commit=True` preserves the immediate commit. | Preserved: importer resolves country/provider/prefix before calling the method. | Preserved: the method does not change counters. | medium | Extracted in Stage 27. | Keep isolated; create/history/relations remain later classifications. |
| `app/importer.py` | `_apply_route`, new route create | batch write through Repository | `routes`, `route_history`, `change_log` | Calls `Repository.create_route()` for new routes. | Creates route row plus route history and change log through Repository. | Repository helper commits internally. | Existing Repository path. | No direct counter changes inside helper. | medium | Already repository-backed. | No Stage 23 work. |

### E. Tariff import writes

Found. The importer directly closes current tariff rows before creating a new tariff through Repository.

| File | Function / area | SQL type | Tables | What it does | Side effects | Transaction participation | Validation order impact | Preview/summary counter impact | Risk | Safe to extract? | Recommended future stage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `app/importer.py` | `_apply_tariff`, existing tariff close-current step | `UPDATE` | `tariffs` | Sets matching current tariffs to `is_current = 0` before creating a replacement tariff. | Changes current tariff state; no direct tariff history/change_log for this close-current step in importer. | No local commit before `Repository.create_tariff()`; effective transaction joins create helper commit. | High: must happen before new tariff creation. | No direct counter changes, but exceptions affect skipped counter. | high | Yes eventually, but not Stage 23. Extract only with tests proving current flag behavior. | Later tariff-specific extraction stage. |
| `app/importer.py` | `_apply_tariff`, new tariff create | batch write through Repository | `tariffs`, `tariff_change_history`, `change_log` | Calls `Repository.create_tariff()` after optional current-row closure. | Creates tariff, tariff history, and change log. | Repository helper commits internally. | Existing Repository path. | No direct counter changes inside helper. | high | Already repository-backed for create. | No Stage 23 work. |

### F. Calling company import writes

Stage 25 extracted the existing-company update to the narrow
`Repository.update_calling_company_import_fields()` method. The create path remains Repository-backed and unchanged.

| File | Function / area | SQL type | Tables | What it does | Side effects | Transaction participation | Validation order impact | Preview/summary counter impact | Risk | Safe to extract? | Recommended future stage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `app/repository.py` | `update_calling_company_import_fields`, existing company update | `UPDATE` | `calling_companies` | Updates company name, autorotation flag, comment, active flag, updated_by, and updated_at by server/country/external id. | Changes operational company metadata; no company history/change_log was added. | Default `commit=True` preserves the importer's immediate commit. | Preserved: importer still resolves server/country and parses booleans before the call. | Preserved: method does not change counters. | medium | Extracted in Stage 25. | Keep isolated; PostgreSQL runtime remains disabled. |
| `app/importer.py` | `_apply_company`, new company create | batch write through Repository | `calling_companies`, `change_log` | Calls `Repository.create_calling_company()`. | Creates company and change-log row through Repository. | Repository helper commits internally. | Existing Repository path. | No direct counter changes inside helper. | medium | Already repository-backed. | No Stage 23 work. |
| `app/importer.py` | `_apply_company`, server create if missing | batch write through Repository | `servers` | Calls `Repository.create_server()` if the server does not exist. | May create a server dictionary row. | Repository helper commits internally. | Existing Repository path. | No direct counter changes inside helper. | low | Already repository-backed. | No Stage 23 work. |

`company_routing_settings` writes were not found in `app/importer.py`.

### G. Import job / preview / summary writes

Not found in `app/importer.py`.

- No direct writes to `import_jobs` were found.
- No direct writes to `preview_data`, `summary`, or `error_report` were found.
- Preview/summary counters are in-memory `ImportPreview` fields, not persisted by direct importer SQL in the audited file.

### H. Change log/history writes

Partially found.

| File | Function / area | SQL type | Tables | What it does | Side effects | Transaction participation | Validation order impact | Preview/summary counter impact | Risk | Safe to extract? | Recommended future stage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `app/importer.py` | `_apply_phone`, update history | history/log write / `INSERT` | `phone_number_history` | Writes phone update history for import updates. | User-visible phone history. | Commits together with direct phone update. | Medium/high because message content depends on imported-created-by logic. | No direct counter changes, but exceptions affect skipped/error counters. | high | Extract with phone update only. | Later phone-update extraction stage. |
| `app/importer.py` | create paths through Repository | history/log write through Repository | `route_history`, `phone_number_history`, `tariff_change_history`, `change_log` | Repository create helpers write history/change_log for new routes, phone numbers, tariffs, calling companies, and related entities. | User-visible audit trail. | Repository helpers commit internally. | Existing behavior; not direct importer SQL. | Success/failure affects counters through helper return/exception. | medium/high by entity | Already repository-backed; not Stage 23. | Entity-specific stages only if needed. |

No direct `change_log` SQL statement was found in `app/importer.py`.

## Stage 23 candidate selection

Recommended Stage 23 scope: extract dictionary `INSERT OR IGNORE` helpers only.

Preferred 1–2 candidates:

1. `projects` dictionary insert-or-ignore from `_apply_dictionary(kind == "project")` — extracted in Stage 23.
2. `phone_number_types` dictionary insert-or-ignore from `_apply_dictionary(kind == "phone_type")` — extracted in Stage 23.
3. `phone_assignment_types` dictionary insert-or-ignore from `_apply_dictionary(kind == "phone_assignment")` — extracted in Stage 24.

Why these are safe:

- small isolated write methods;
- clear scalar parameters;
- no cascade;
- no import validation order change required;
- no preview/summary counter logic inside the SQL;
- no user-facing validation message change required;
- existing immediate commit behavior can be preserved inside the Repository method or by keeping the caller commit in the same place;
- focused tests can cover idempotency and unchanged import counters.

Do not choose for Stage 23:

- route create/update;
- tariff create/update/current-flag closure;
- complex phone-number update/history write;
- section clearing;
- currency/recalculation paths;
- HLR;
- Telegram;
- any flow with broad cascade or user-visible history semantics.

## Suggested small-PR sequence after Stage 22

1. **Stage 23 — dictionary insert-or-ignore extraction**
   - Added narrow Repository methods for `projects` and `phone_number_types` insert-or-ignore.
   - Preserved immediate commit behavior and importer counters.

2. **Stage 24 — remaining dictionary insert-or-ignore extraction**
   - Added a narrow Repository method for `phone_assignment_types` insert-or-ignore.
   - Preserved the importer-side `code` fallback and immediate commit behavior.

3. **Recommended Stage 25 — simple calling-company update extraction**
   - Move the existing-company `calling_companies` update to Repository.
   - Preserve the fact that the current importer update path does not add change-log/history entries.

3. **Stage 25 — route update extraction**
   - Move the existing-route update to Repository.
   - Preserve no-history behavior unless a later explicit product decision changes it.

4. **Stage 26 — phone update and history extraction**
   - Move phone update plus phone history insert together.
   - Cover imported-created-by, review flag, active/deactivated transitions, and history comments.

5. **Stage 27 — tariff current-flag extraction**
   - Move current-tariff closure with tests around old/current and new/current behavior.

6. **Later dedicated stage — section clearing policy**
   - Decide whether replacement mode remains disabled.
   - Only then extract destructive clearing helpers with explicit transaction and cascade tests.

## Runtime status

- Runtime code was not changed in Stage 22.
- PostgreSQL runtime backend remains disabled.
- No runtime `psycopg` import or dependency is added.
- SQLite remains the working database.
