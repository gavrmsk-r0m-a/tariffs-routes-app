# Stage 28 — phone-number import UPDATE focused audit

## Scope and decision

This is a documentation-only audit of the existing-number branch in
`app/importer.py`. Stage 28 does not change runtime code, SQL, validation,
counters, preview/summary output, or transaction boundaries. PostgreSQL runtime
remains disabled and SQLite remains the operational database.

**Stage 29 decision: choose option B.** Extract the existing-number
`UPDATE phone_numbers` and its immediately following
`INSERT phone_number_history` into one narrow Repository method. They form one
successful-row write unit and currently share one immediate commit. Moving only
the UPDATE (option A) would split that unit across persistence layers and make it
easier to change its commit/failure ordering. Option C is not necessary provided
the importer retains all parsing, validation, identity lookup, history-text
construction, preview, branching, and counters.

Risk for the pair is **high**: it changes operational status and audit history,
has sticky review semantics, preserves legacy creator metadata conditionally,
and controls deactivation timestamps. Extraction is nevertheless bounded and
safe for a focused Stage 29 with the tests below.

## End-to-end dependency map

### Existing-number detection and key

1. `_business_key()` requires both country and number. Its returned phone key is
   the one-element tuple `(validate_phone_number(number),)`. Country is validated
   but is deliberately **not** part of identity.
2. `preview_import()` tracks that normalized tuple in `seen_keys`. A repeated key
   in one file is an error (`duplicate_in_file`) for phone imports.
3. `_exists()` calls
   `Repository.phone_number_exists_by_normalized_number(normalized_number)`.
   Thus the actual lookup/update key is `phone_numbers.normalized_number`, not
   `phone_numbers.id`, raw `number`, country, or provider.
4. During apply, the key is checked again. An existing row enters `_apply_phone`
   unless `duplicate_action == "skip"`. `_apply_phone` retrieves `id` and
   `imported_created_by` with
   `get_phone_number_import_identity_by_normalized_number()`; `id` is used only
   as the history foreign key. The UPDATE still uses `normalized_number`.

There is a narrow race between the existence check and identity lookup. In the
current single SQLite connection this is normally immaterial, but Stage 29 must
preserve the current ordering and must return the UPDATE rowcount so a missing
row can be observed in Repository tests.

### Validation and value production before the UPDATE

The apply path performs the following checks before either write:

- `validate_phone_number()` validates/normalizes the number; `_business_key()`
  has already required `country and number` (English message), while
  `_apply_phone()` separately emits `ГЕО обязателен для импорта номеров` for a
  missing GEO if called after that boundary.
- `_phone_reference_ids()` requires an existing country and currency (default
  currency `EUR`), resolves an optional provider, and rejects unknown provided
  reference values with the existing `Значение … не найдено в справочнике …`
  message. It also detects inactive/legacy reference rows.
- `_phone_import_values()` validates optional project, phone type, and assignment
  references; parses monthly fee and can emit `Некорректная АП в EUR: …`;
  normalizes ordinary status; and parses active state.
- When the `Итоговый статус`/`final_status` column is present,
  `_map_final_status()` requires a value, accepts the mappings documented below,
  and otherwise emits `Неизвестный Итоговый статус: …`.
- Before apply begins at all, any phone preview error aborts the whole import with
  `Импорт невозможен: в предпросмотре есть ошибки. Исправьте файл и повторите предпросмотр.`
  Replacement mode is independently blocked with
  `Режим замены раздела временно отключён. Используйте Дополнить / обновить.`

The references are resolved again during apply and again for successful-row
review/legacy counting. Stage 29 should not move, remove, or reorder these reads.

## Write SQL inventory

| Function / area | SQL type and tables | Fields / source | Counters and output | Transaction / rollback | Other side effects | Risk |
| --- | --- | --- | --- | --- | --- | --- |
| `_apply_phone(..., exists=True)` | `UPDATE phone_numbers` | See the exact field map below. Parsed values come from `_phone_import_values()` and reference IDs from `_phone_reference_ids()`; audit user is `user_id`; key is normalized number. | No counter is changed inside `_apply_phone`. On return, `apply_import()` increments `updated_rows`. An exception increments `skipped_rows` and `error_rows` and appends a line-0 error row. Preview already labels the DB duplicate as `update`. | No commit after UPDATE alone. The history INSERT follows, then `repo.conn.commit()` commits both. There is **no explicit rollback**. A caught exception can therefore leave pending work on the connection for the final `conn.commit()` (or an intervening helper commit); Stage 29 must not silently add rollback semantics. | Changes live phone state; no `change_log` write and no route relation write. | **high** |
| `_apply_phone(..., exists=True)` | `INSERT phone_number_history` | Inserts `phone_number_id=existing["id"]`, `action='updated'`, `changed_by=user_id`, `field_name='import'`, and identical `new_value`/`comment` detail strings. `old_value` and `reason` are omitted/NULL. | No direct counter/output change. INSERT failure follows the same caught-error path as UPDATE failure. | Shares the one immediate commit after the INSERT with the UPDATE; no explicit rollback/savepoint. | One user-visible history row for every successfully executed update branch, even if field values are unchanged. No `change_log`. | **high** |
| `_apply_phone(..., exists=False)` via `Repository.create_phone_number()` | Repository-backed INSERT batch: `phone_numbers`, `phone_number_history`, `change_log` | Uses the same assembled import data, excluding only helper flag `has_imported_created_by`; inactive create passes CSV `created_at` as `deactivated_at`. | Successful return increments `created_rows`; failure follows skipped/error handling. | Existing Repository create commits internally. Out of Stage 29 scope. | Created history and change log are intentionally different from update behavior. | **medium; unchanged** |
| `_clear_section(..., entity_type='phone_numbers')` | DELETE in order: `route_phone_numbers`, `route_phone_number_history`, `phone_number_history`, `phone_numbers` | No row fields; clears the section. | Not reached in normal import because `replace_section` is rejected before preview/apply. If re-enabled it would affect the whole section rather than a row counter directly. | No local commit or rollback; caller transaction would apply. | Destructive clearing of phone-route relations and history; intentionally does not clear `change_log`. | **blocker for extraction; out of scope/inactive** |

No `INSERT` or `UPDATE route_phone_numbers`, no route write, and no direct
`change_log` statement occurs in the phone update branch. The only route-phone
SQL related to this entity is the currently unreachable replacement-section
DELETE batch. There are no persisted import-job, preview, or summary writes.

## Exact `phone_numbers` UPDATE field map

Only these actual columns are assigned by the importer UPDATE:

| Column | Value source and semantics |
| --- | --- |
| `country_id` | Resolved from required CSV country/GEO. |
| `provider_id` | Resolved provider ID, or NULL when provider is empty. |
| `project_label` | CSV project label after validating it against `projects`, or NULL. |
| `assignment_type` | Resolved assignment code, or NULL. There is no `assignment_type_id` column assignment. |
| `status` | Mapped final status or `normalize_phone_status()` result. |
| `is_active` | Parsed/mapped boolean converted by importer to SQLite integer 1/0. |
| `connection_cost` | Optional `connection_fee`/`connection_cost`/localized CSV text. |
| `monthly_fee` | Optional parsed non-invalid decimal text. |
| `outgoing_rate`, `incoming_rate` | Optional CSV values, otherwise NULL. |
| `currency_id` | Resolved currency ID; omitted input defaults to EUR. |
| `phone_type` | Validated optional phone-type name, or NULL. There is no `phone_number_type_id` assignment. |
| `tariff_label` | Optional CSV tariff label, or NULL. |
| `comment` | Optional CSV comment, or NULL. |
| `review_required` | Sticky SQL: `CASE WHEN incoming = 1 THEN 1 ELSE review_required END`. Import can set but cannot clear it. |
| `imported_created_by` | Existing value unless a creator column is present **and non-empty**; then the CSV value replaces it. |
| `deactivated_at` | Transition CASE described below. CSV `created_at` is not used on update. |
| `updated_by` | Applying `user_id`. |
| `updated_at` | Database `CURRENT_TIMESTAMP`. |

The UPDATE does **not** assign `number`, `normalized_number`, `created_by`,
`created_at`, `assignment_label`, or `currency_label`. In particular, the audit
found no ID fields named `phone_number_type_id` or `assignment_type_id` in this
write.

## Special behavior

### `imported_created_by`

- `_has_any()` distinguishes an absent creator column from a present empty one,
  but both preserve the old database value because update is allowed only when
  the column exists and its parsed value is non-empty.
- A present non-empty value replaces the old value. It never makes the row require
  review.
- Preview does not query/show the old value. It adds `Создал в Excel: VALUE` to
  `info` and the message only for a non-empty incoming value; preview itself does
  not write.
- Apply fetches the old value and builds history detail. A changed value produces
  `Создал в Excel: было OLD_OR_—, стало NEW`; an equal supplied value produces
  `Создал в Excel: NEW`; absent/empty input leaves the base detail only.

### `review_required`

The incoming flag is true when at least one of these is true: provider is empty,
project is empty, assignment is empty, or mapped final status requires review.
Unknown-but-present inactive/legacy references contribute informational legacy
state but do not themselves set review. Missing phone type also does not set it.

Final-status mappings are:

- `Отключен` -> `status='unused'`, inactive, no status review reason;
- `Используется` -> `status='used'`, active, no status review reason;
- `???`, `Не используется`, `Не нужен`, `Свободен` -> `status='unknown'`,
  active, status review required.

The database update is intentionally sticky: incoming true sets the stored flag
to 1, while incoming false preserves either existing 0 or existing 1. Therefore
import cannot clear a prior review requirement. Preview and review counters show
the reasons derived from the incoming row, not the old sticky stored value.

### Active/deactivated transition

- Inactive input stores `is_active=0`. If `deactivated_at` is NULL it becomes
  `CURRENT_TIMESTAMP`; if already non-NULL it is preserved.
- Active input stores `is_active=1` and always clears `deactivated_at` to NULL.
- These rules are driven by parsed `is_active`, or by the final-status mappings
  above. `status` alone does not drive the timestamp outside those mappings.
- `created_at` from CSV is ignored by UPDATE. It is used only on create, where an
  inactive new row receives it as the requested deactivation timestamp.

### History payload and relations

Every update branch writes exactly one history row after UPDATE. It does not
record per-field old/new values: `old_value` is NULL, while both `new_value` and
`comment` receive the same human-readable `details` string. `field_name` is
`import`, action is `updated`, and `changed_by` is the applying user. No update
`change_log` entry is written. There are no `route_phone_numbers` or route import
section side effects in append/update mode.

## Preview, summary counters, and errors

`ImportPreview` is both the preview result and the returned apply summary; none of
its fields are persisted by this flow.

- `total_rows`: parsed CSV row count.
- `new_rows`: preview-only classification for missing normalized keys.
- `duplicate_rows`: incremented in preview for an existing normalized key and for
  an in-file duplicate. Existing-in-DB is displayed as action/status `update`, not
  as an error.
- `updated_rows`: incremented after `_apply_phone()` returns for an existing row.
- `created_rows`: incremented after the create helper returns for a new row.
- `skipped_rows`: incremented during apply for a repeated apply key,
  `duplicate_action='skip'`, or any caught row exception.
- `error_rows`: preview validation/in-file-duplicate errors; on caught phone apply
  exceptions it is incremented again and an error output row is appended.
- `review_required_rows` and `legacy_info_rows`: preview increments these while
  classifying rows. Apply then increments the same returned object again after a
  successful phone write, so successful applicable rows are counted a second
  time in the returned apply summary. Stage 29 must preserve this current behavior.

Preview never executes either write. Apply first runs the full preview and refuses
all phone writes if preview has any error. Preview rows include normalized number,
mapped working status, a label currently named `active_provider` that reflects
`is_active`, review yes/no and reasons, errors, legacy/creator info, and a message.
Apply retains those preview rows and counters, then adds apply counters/errors.

## Transaction and failure boundary

On the normal existing-row path, UPDATE and history INSERT execute on the same
connection followed by one immediate commit. `apply_import()` later performs an
additional final commit. There are no explicit calls to `rollback()` anywhere in
this row flow. The apply loop catches all row exceptions and continues.

Consequently, Stage 29 must preserve statement order and the current single
post-pair commit. It must not commit between UPDATE and history, add a savepoint,
or introduce rollback as an incidental cleanup. Atomic rollback policy can only
be changed in a separate explicitly behavioral stage. A Repository exception
must continue to reach the importer loop so skipped/error counters remain there.

## Stage 29 extraction boundary and candidate

### Direct answers

- **Can UPDATE alone be extracted safely?** Not recommended. It is mechanically
  possible, but it would split an adjacent audit pair and invites commit drift.
- **Must UPDATE and history move together?** Yes: option B is the selected safe
  Stage 29 candidate, preserving their order and shared commit.
- **Where do counters stay?** Entirely in `apply_import()`.
- **Where does preview stay?** Entirely in importer parsing/preview helpers.
- **What else stays in importer?** Existence branching, all validation/reference
  resolution, identity lookup, creator preservation decision, and exact history
  detail construction. Create and section clearing remain untouched.
- **How is commit behavior preserved?** Repository executes UPDATE then history
  INSERT and, with default `commit=True`, commits once afterward. It performs no
  rollback; `commit=False` executes both without committing for focused tests or
  future controlled composition.

### Draft narrow signature

The payload below contains only fields used by the actual SQL. `phone_number_id`
and the already resolved creator/history text come from the existing identity
lookup, avoiding a new SELECT and keeping creator business decisions in importer.

```python
def update_phone_number_import_fields_with_history(
    self,
    *,
    normalized_number: str,
    phone_number_id: int,
    country_id: int,
    provider_id: int | None,
    project_label: str | None,
    assignment_type: str | None,
    status: str,
    is_active: bool,
    connection_cost: str | None,
    monthly_fee: str | None,
    outgoing_rate: str | None,
    incoming_rate: str | None,
    currency_id: int,
    phone_type: str | None,
    tariff_label: str | None,
    comment: str | None,
    review_required: bool,
    imported_created_by: str | None,
    history_details: str,
    changed_by: int,
    commit: bool = True,
) -> int:
    ...
```

Stage 29 should use `placeholder(self.backend)`, `to_db_bool()` for
`is_active` and `review_required`, database `CURRENT_TIMESTAMP`, and return the
UPDATE cursor's `rowcount`. It must not become a generic update helper, alter
counters, add SELECTs, write `change_log`, or touch route relations. The current
schema and current importer both require the applying integer `user_id` as the
non-NULL history `changed_by`.

## Focused Stage 29 test plan

### Repository tests

1. Update an existing normalized number and assert every listed field, audit
   field, and rowcount 1.
2. A missing normalized number returns rowcount 0. Because the combined method
   also receives `phone_number_id`, define and test that history is **not** written
   when rowcount is 0; this is the safest explicit guard and should be reconciled
   with the currently assumed `exists=True` invariant.
3. Verify SQLite stores both boolean parameters as 1/0 and adapter SQL uses backend
   placeholders.
4. Verify false review input preserves stored 1, and true input sets stored 1.
5. Verify inactive/null timestamp sets it, repeated inactive preserves it, and
   active clears it.
6. Verify supplied resolved `imported_created_by` is stored and a preserved value
   remains unchanged.
7. Verify exactly one history row on rowcount 1, with action, field name, user,
   NULL old value, duplicated details in new value/comment; verify no `change_log`
   or route-relation write.
8. Verify `commit=False` leaves the pair uncommitted to a second connection and
   default commit commits after both statements. Do not add a rollback behavior
   assertion that would redefine the importer.

### Importer regression tests

1. Existing-number update produces the same complete field set and one history
   row; new/duplicate/skip behavior and create path remain unchanged.
2. Missing, empty, equal, and changed `imported_created_by` preserve the current DB,
   preview, and exact history-comment behavior.
3. Review reasons and sticky stored review behavior remain unchanged for missing
   provider/project/assignment and mapped statuses; inactive legacy references
   remain informational only.
4. Active/inactive transitions preserve/set/clear `deactivated_at` exactly as now,
   including the fact that update ignores CSV `created_at`.
5. Preview has no writes, existing rows remain action `update`, and preview/apply
   `new_rows`, `duplicate_rows`, `created_rows`, `updated_rows`, `skipped_rows`,
   `error_rows`, and the current double-counted apply review/legacy totals remain
   identical.
6. Preserve all user-facing validation messages and the all-phone-import preview
   error gate.
7. Inject UPDATE/history failures to confirm exceptions still reach importer
   skipped/error accounting and that no new rollback semantics were introduced.

## Recommended Stage 29

Implement only the combined business-specific Repository method above and replace
only the two direct statements plus their one commit in the existing update
branch. Keep identity lookup and `details` construction in importer, add focused
Repository/adapter/importer tests, and leave phone create, clearing, routes,
tariffs, UI, schema, PostgreSQL enablement, and dependencies untouched.

## Stage 29 completion

Stage 29 completed the selected paired extraction as `Repository.update_phone_number_import_fields_with_history()`. The method updates by `normalized_number` and receives the separately looked-up phone ID for its one history row. It writes country/provider, project, assignment, status, active state, connection/monthly/outgoing/incoming rates, currency, phone type, tariff, comment, resolved review flag, resolved imported creator, resolved deactivation timestamp, updater, and database-managed update timestamp.

The same method then writes exactly one successful-update history record with `action='updated'`, `field_name='import'`, NULL old value, the applying user, and the importer-built detail payload as new value and comment. Its default commit occurs once after the pair; `commit=False` leaves both statements pending. A missing update key returns zero and does not create a spurious history record.

Parsing, validation messages, identity lookup, imported-creator preservation, sticky-review resolution, active/deactivated transition resolution, history text, exception accounting, counters, preview, and summary remain intentionally in the importer. Phone creation, route-phone relations, section clearing, other entity writes, and schemas were not moved. PostgreSQL runtime remains disabled and SQLite remains the operational database.
