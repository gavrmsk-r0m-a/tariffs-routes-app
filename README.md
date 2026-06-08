# Tariffs and Routes MVP

Dependency-free Python/SQLite MVP foundation for replacing the Excel-based tariffs, routes, purchased numbers, provider-change logs and admin reference data workflow.

## Implemented MVP foundation

- SQLite schema for the confirmed MVP data model.
- Repository/business-rule layer for core entities.
- Validation that a phone number cannot be linked to a route when it is inactive, disabled, or blocked.
- Strict international phone format validation: digits only, no `+`, no leading `00`, no spaces/brackets.
- Minimal stdlib WSGI web UI for the main MVP screens.
- CSV import preview/apply flow for key entities.
- Unit and smoke tests for key business rules, import checks, and screen rendering.

## Available screens

Run the app and open the links in the top navigation:

- `/routes` — routes list, filters, auto-named route creation, route edit page with name/prefix editing, and route number side-page.
- `/tariffs` — current tariffs table, filters, and tariff creation using active admin-managed reference values.
- `/phones` — purchased numbers list, filters, creation form, and full number edit page.
- `/companies` — calling campaigns list, filters, creation form, and edit page with immutable external campaign ID.
- `/provider-changes` — provider-change log, filters, checkbox-based server selection, creation form, and edit page with automatic EUR delta recalculation.
- `/admin` — admin landing page.
- `/admin/server-priorities` — server priorities with current `★` and previous `☆` providers plus expandable route details.
- `/admin/naming-rules` — route naming rule management.
- `/admin/import` — CSV import preview/apply for routes, tariffs, phone numbers, calling campaigns, and dictionaries; preview keeps the selected section/mode/CSV text and apply shows created/updated/skipped/error totals.
- `/admin/currency-rates` — simplified manual currency rate upsert used by tariff EUR conversion.
- `/admin/change-reasons` — active/inactive editable reasons used by provider-change forms.
- `/admin/dictionaries` — admin reference values for countries, providers, currencies, prefixes, servers, projects, phone assignments, and phone number types with activation/deactivation.
- `/admin/change-log` — technical change log for audit/API/AI archivist integration later.

## Run locally

```bash
python -m app.server
```

By default the app creates/uses `mvp.sqlite3` in the repository root. You can override it:

```bash
MVP_DB_PATH=/tmp/tariffs-routes.sqlite3 python -m app.server
```

## Run tests

```bash
python -m unittest discover -s tests
```

## Notes

The current implementation intentionally uses only the Python standard library so the project can run in restricted environments without downloading packages. The schema mirrors the confirmed MVP model and can be migrated to PostgreSQL later if needed.
