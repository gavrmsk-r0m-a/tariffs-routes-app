# Stage 30 — аудит CREATE при импорте телефонных номеров

## Scope и итог

Этот документ фиксирует фактический create-flow из `app/importer.py` и
`Repository.create_phone_number()`. Stage 30 не меняет runtime-код, SQL,
валидацию, счётчики или границы транзакций. PostgreSQL runtime остаётся
выключенным; рабочая БД — SQLite.

**Решение для Stage 31: вариант B.** Адаптерную extraction следует делать только
для единой операции `INSERT phone_numbers` + обязательный
`INSERT phone_number_history` + `INSERT change_log`. Выносить только первый
INSERT небезопасно: сегодня все три записи выполняются одним Repository-методом,
а единственный commit расположен после audit side effects. При этом Stage 31 не
должен переносить в Repository preview, validation или counter logic.

## Полный путь строки до CREATE

1. `preview_import()` парсит CSV, строит business key и повторно разбирает поля и
   справочники. Ошибка строки сохраняется в preview; для phone import она также
   увеличивает `error_rows`.
2. Business key — кортеж `(normalized_number,)`. Фактически
   `validate_phone_number(number)` сейчас не преобразует номер: он проверяет
   исходную строку регулярным выражением и возвращает её. Ни country, ни raw
   number отдельно не входят в identity key.
3. Повтор того же key в файле получает `duplicate_in_file`, `duplicate_rows += 1`
   и `error_rows += 1`. Существование в БД проверяется по
   `phone_numbers.normalized_number`; существующая строка показывается как
   `update`, отсутствующая — как `create` и увеличивает preview `new_rows`.
4. `apply_import()` сначала полностью вызывает preview. Если в phone preview есть
   хотя бы одна ошибка, apply прекращается сообщением «Импорт невозможен: в
   предпросмотре есть ошибки. Исправьте файл и повторите предпросмотр.» — никаких
   create-записей ещё нет.
5. В apply строка снова проходит business-key/exists проверки. Дубликат внутри
   файла или существующий номер при `duplicate_action == "skip"` увеличивает
   `skipped_rows`; `_apply_phone()` не вызывается.
6. `_apply_phone()` снова валидирует номер, требует ГЕО, разрешает справочники,
   вычисляет import fields и review flag. Только ветка `exists == False` вызывает
   `Repository.create_phone_number()`.

### Проверки до записи и пользовательские сообщения

- Обязательны country и number: business-key выдаёт `country and number are
  required`; `_apply_phone()` дополнительно выдаёт «ГЕО обязателен для импорта
  номеров» (обычный preview отсекает отсутствие раньше).
- Номер обязан иметь международный формат без `+`, `00`, пробелов, скобок и иных
  символов; сообщение Repository: `Phone number must be in international format
  without +, 00, spaces, brackets, or other symbols`.
- Непустые ГЕО, провайдер, проект, тип номера, назначение и валюта должны
  существовать в соответствующем справочнике. Сообщения имеют форму
  `Значение ‘…’ не найдено в справочнике …. Исправьте файл или добавьте значение
  в справочник вручную.`; обязательная reference выдаёт `… обязателен`.
- Пустой provider разрешён. Пустые provider/project/assignment не являются
  ошибками, а включают review. Валюта по умолчанию — `EUR` и должна разрешиться.
- `АП в EUR`/`monthly_fee`: пусто, `?`, `-`, «неизвестно» становятся `NULL`;
  нечисловое значение даёт «Некорректная АП в EUR: …».
- При наличии колонки итогового статуса пустое значение запрещено
  («Итоговый статус обязателен для импорта номеров»), неизвестное —
  «Неизвестный Итоговый статус: …». Без этой колонки legacy status нормализуется,
  а `is_active` разбирается отдельно.
- Дубликат в CSV сообщает номер исходной строки. Любая preview error блокирует
  весь phone apply указанным выше общим сообщением.

## Карта write SQL

| Участок | SQL и таблица | Поля/источник | Side effects и counters | Commit/rollback | Риск |
| --- | --- | --- | --- | --- | --- |
| `Repository.create_phone_number()` | `INSERT phone_numbers` | Все поля перечислены ниже; импорт передаёт разобранные CSV/reference/user значения, Repository добавляет normalized value и snapshot labels | После успешного возврата importer увеличивает `created_rows`; сам Repository счётчиков не меняет | Commit ещё не выполняется | **high**: operational row и id нужны двум следующим INSERT |
| тот же метод, сразу после получения `phone_id` | `INSERT phone_number_history` | `phone_number_id`; literal `created`; `changed_by=created_by`; literal `field_name=number`; `new_value=raw number`; comment из import comment и imported creator | Обязательная пользовательская история; отдельных counters/preview нет | Всё ещё до единственного commit | **high**: пропуск меняет видимый audit trail |
| `_change_log()` из того же метода | `INSERT change_log` | `entity_type=phone_number`, новый id, `change_type=phone_number.created`, `changed_by=created_by`, `old_values=NULL`, JSON `new_values={number, imported_created_by}`, `summary=NULL`, `source=ui` | Обязательный business audit; несмотря на import, фактический source сейчас `ui` | Затем `create_phone_number()` вызывает `conn.commit()` | **high**: payload/source являются текущим контрактом |

Других create-related write statements нет. В частности, create не пишет
`route_phone_numbers`, `route_phone_number_history`, routes или import-job/summary
таблицы. `_clear_section()` содержит отдельные destructive DELETE, но normal apply
до него не доходит, потому что `replace_section` отключён; это не side effect
create и не входит в Stage 31.

### Транзакционная оговорка

Три INSERT идут последовательно на одном connection, после чего выполняется один
`commit()`. Явного `rollback()` ни в `create_phone_number()`, ни в row-level
`except` у `apply_import()` нет. Поэтому это единая commit-группа, но не
гарантированная exception-safe атомарная операция: исключение после первого
INSERT оставляет незакоммиченные изменения в connection, а последующий успешный
row-level commit или финальный `apply_import()` commit потенциально может их
зафиксировать. Stage 31 должен **сохранить** наблюдаемую границу commit и не
добавлять скрытый rollback в рамках совместимого extraction; улучшение rollback
требует отдельного решения и regression tests.

## Фактические поля `phone_numbers`

| Поле | Фактическое значение на import create |
| --- | --- |
| `country_id` | id обязательного ГЕО из справочника |
| `provider_id` | id непустого provider либо `NULL` |
| `country_label`, `provider_label` | snapshot labels из `_phone_snapshot_labels()` |
| `number` | исходная CSV-строка без преобразования |
| `normalized_number` | результат `validate_phone_number(number)`; сейчас совпадает с `number` |
| `project_label` | CSV project либо `NULL`; reference предварительно валидируется |
| `assignment_type` | code разрешённого assignment либо `NULL` |
| `assignment_label` | snapshot label Repository |
| `phone_type`, `tariff_label` | CSV values либо `NULL`; phone type валидируется как reference |
| `status` | разобранный status, затем `normalize_phone_status()` в Repository |
| `connection_cost`, `outgoing_rate`, `incoming_rate` | CSV text либо `NULL` |
| `monthly_fee` | нормализованная decimal string либо `NULL` |
| `currency_id` | id CSV currency или default EUR |
| `currency_label` | snapshot label Repository |
| `comment` | CSV comment либо `NULL` |
| `is_active` | вычисленный state, сохраняется SQLite integer `1/0` |
| `review_required` | OR пустых provider/project/assignment и status-review, SQLite `1/0` |
| `imported_created_by` | значение из `Создал`/поддерживаемых legacy aliases либо `NULL` |
| `created_by` | текущий `user_id`, не Excel creator |
| `created_at` | CSV `created_at`, иначе SQL `CURRENT_TIMESTAMP` через `COALESCE` |
| `deactivated_at` | importer передаёт CSV `created_at` для inactive и `NULL` для active; Repository для inactive с `NULL` использует `created_at` либо текущее локальное время |

`updated_by` и `updated_at` CREATE явно не вставляет; применяются только schema
defaults, если они определены. Отдельного raw/original-number поля кроме `number`
нет. Иных costs/rates create не вставляет.

## Зависимости и состояния

### `imported_created_by`

Наличие/значение читается из `Создал`, `imported_created_by`,
`source_created_by` или `legacy_created_by`. На create фактическое значение (или
`NULL`) без sticky merge передаётся в `phone_numbers`. Оно также добавляется в
history comment как `Создал в Excel: …` и присутствует в JSON change-log. Audit
actor при этом всегда `created_by=user_id`. Само наличие Excel creator не включает
`review_required`; preview лишь показывает его в `info`/message и считает строку
в `legacy_info_rows` только по legacy references, не по creator.

### Review и active/deactivated

`review_required` — OR четырёх причин: пустой provider, project, assignment или
status, требующий проверки. Неактивный legacy reference отмечает
`reference_legacy`/`legacy_info_rows`, но сам по себе review flag не включает.
Итоговые статусы: «отключен» -> `unused`, inactive, без status review;
«используется» -> `used`, active; `???`/«не используется»/«не нужен»/«свободен»
-> `unknown`, active, review. Без final-status active берётся из `is_active`
(default true).

Для active create `deactivated_at=NULL`. Для inactive importer намеренно передаёт
`created_at`; если оно пусто, Repository ставит текущее время. Таким образом
inactive imported row с заданным creation timestamp получает тот же timestamp как
deactivation timestamp. Это поведение нельзя «исправлять» при extraction.

### History и change log payload

- History: `(new_phone_id, 'created', user_id, 'number', raw number, comment)`;
  comment равен `"{comment}. "` при наличии comment плюс
  `"Создал в Excel: {imported_created_by}"` при наличии creator. При отсутствии
  обоих это пустая строка; при одном обычном comment остаётся завершающее `. `.
- Change log: entity/type/actor как в таблице выше; `new_values` содержит только
  raw `number` и `imported_created_by`, включая JSON `null`; source остаётся `ui`.

## Counters, preview и summary

- Preview: `total_rows` — CSV rows; новый key увеличивает `new_rows`; существующий
  или duplicate-in-file увеличивает `duplicate_rows`; row parse/reference error
  увеличивает `error_rows`. `review_required_rows` и `legacy_info_rows` считаются
  по разобранным данным. Preview rows содержат line, create status/action,
  normalized number, working status, значение is_active под историческим ключом
  `active_provider`, review flag/reasons, errors, legacy/creator info и message.
- Apply использует тот же объект preview, поэтому `new_rows`, `duplicate_rows` и
  preview rows сохраняются. После успешного create добавляется `created_rows`.
  `updated_rows` относится только к существующим номерам. Duplicate/skip action
  увеличивает `skipped_rows`; caught row exception увеличивает `skipped_rows` и
  для phones также `error_rows` и добавляет error row. Отдельного поля `errors`
  counter нет: фактический counter — `error_rows`.
- Apply повторно увеличивает `review_required_rows` и `legacy_info_rows` после
  успешной записи. Следовательно summary на успешном apply содержит preview count
  плюс apply count для этих двух показателей. Это фактическое, хотя и необычное,
  поведение следует сохранить. Repository не должен менять ни один counter.
- Preview никогда не вызывает create/history/change-log и не commit-ит. Apply
  вызывает все три write; успешный Repository commit происходит до увеличения
  `created_rows`. Финальный `conn.commit()` выполняется после цикла.

## Граница extraction для Stage 31

**Только `INSERT phone_numbers` выносить нельзя.** History и change log уже
находятся рядом в `create_phone_number()` и являются частью одной commit-группы.
Рекомендуется сохранить combined operation, порядок statements и commit после
третьего INSERT. Validation/reference resolution, new-vs-existing branch,
preview, counters и exception-to-summary mapping остаются в importer.

Точный кандидат — narrow import-specific Repository method (не generic insert):

```python
def create_phone_number_import_record_with_history_and_log(
    self,
    *,
    country_id: int,
    number: str,
    assignment_type: str | None,
    status: str,
    created_by: int,
    phone_type: str | None = None,
    tariff_label: str | None = None,
    provider_id: int | None = None,
    project_label: str | None = None,
    connection_cost: str | None = None,
    monthly_fee: str | None = None,
    outgoing_rate: str | None = None,
    incoming_rate: str | None = None,
    currency_id: int | None = None,
    comment: str | None = None,
    is_active: bool = True,
    review_required: bool = False,
    created_at: str | None = None,
    deactivated_at: str | None = None,
    imported_created_by: str | None = None,
    commit: bool = True,
) -> int:
    ...
```

Сигнатура повторяет только фактические create inputs; отдельные invented
`history_payload`/`change_log_payload` не нужны, поскольку текущие payloads
детерминированно строятся из `number`, `comment`, `imported_created_by` и
`created_by`. Nullable `assignment_type` отражает фактический разрешённый import
flow (текущая общая сигнатура Repository типизирована строже реального вызова).

Реализация Stage 31 должна использовать `placeholder(self.backend)`,
`prepare_insert_returning_id()`/`extract_inserted_id()` для id телефона и
`to_db_bool()` для `is_active`/`review_required`. Она должна сохранить snapshot
labels, status normalization, deactivation fallback, history formatting,
change-log payload/source и порядок INSERT. `commit=False` означает отсутствие
внутреннего commit после всех трёх statements; caller управляет транзакцией.
Нельзя добавлять counters, preview calculation или generic insert helper.

## Focused tests для Stage 31

### Repository

- создаёт все фактические поля и возвращает новый phone id;
- сохраняет booleans как SQLite `1/0`;
- создаёт ровно одну history row с точными action/actor/field/value/comment;
- создаёт ровно одну change-log row и сохраняет JSON, `NULL`, source и actor;
- сохраняет raw и normalized number, snapshot labels, creator, review и
  active/deactivated combinations;
- `created_at` override и `CURRENT_TIMESTAMP` fallback;
- `commit=False`: данные видны текущему connection, не зафиксированы для второго
  connection до внешнего commit; все три statements участвуют вместе;
- ошибка второго/третьего INSERT документирует сохранённое transaction behavior
  (не добавлять неоговорённый rollback).

### Importer

- новый номер создаёт те же поля, history и change-log payloads;
- preview не пишет БД и сохраняет new/duplicate/error/review/legacy counters и
  user-facing messages;
- apply сохраняет created/skipped/error и удвоенный review/legacy summary model;
- missing/present imported creator сохраняет actor и history/log semantics;
- review reasons и empty-provider create не меняются;
- active/inactive с/без `created_at` сохраняет deactivation semantics;
- duplicate-in-file, duplicate-in-DB update и duplicate-action skip не меняются;
- Stage 29 existing-phone update/history tests продолжают проходить;
- invalid number, reference, monthly fee и final-status messages остаются
  дословными.

## Рекомендация

Stage 31 может быть маленьким adapter-compatibility PR по **варианту B**, если он
переводит combined create/history/log boundary целиком и не меняет observable
semantics. Route-phone links, section clearing и routes/import sections должны
остаться вне scope. PostgreSQL runtime включать нельзя.

## Итог Stage 31

Stage 31 завершён адаптацией существующего
`Repository.create_phone_number()`; отдельный import-only метод не понадобился.
`INSERT phone_numbers` теперь строится через backend placeholder и
`prepare_insert_returning_id()`, а новый id извлекается через
`extract_inserted_id()` (`cursor.lastrowid` для SQLite, `RETURNING id` для
будущего PostgreSQL). `is_active` и `review_required` проходят через
`to_db_bool()`.

Следующие `INSERT phone_number_history` и `INSERT change_log` сохранены в том же
методе, в прежнем порядке и с прежними history text, JSON payload и `source=ui`.
Все три записи по-прежнему предшествуют единственному commit; опциональный
`commit=False` оставляет управление транзакцией caller. Importer, его counters,
preview и validation не менялись. PostgreSQL runtime остаётся выключенным, а
операционной БД остаётся SQLite.
