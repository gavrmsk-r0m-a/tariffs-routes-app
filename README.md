# Tariffs and Routes MVP

Python/PostgreSQL MVP foundation for replacing the Excel-based tariffs, routes, purchased numbers, provider-change logs and admin reference data workflow.

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

## Local PostgreSQL runtime

PostgreSQL is the recommended local application runtime. The compose file uses only
the documented `postgres/postgres` development credential and stores its data in a
named Docker volume; it contains no production credentials.

1. Start PostgreSQL and wait until its health check passes:

   ```bash
   docker compose -f docker-compose.postgres.yml up -d
   ```

2. Create the schema and an idempotent `local-dev` administrator. The command prints
   a newly generated **local login password**; save it for the next steps. It never
   prints the database password:

   ```bash
   python scripts/setup_local_postgres.py \
     --database-url postgresql://postgres:postgres@localhost:5432/teleroute_local
   ```

   Pass `--password 'a-local-password-you-chose'` when a stable local login is more
   convenient. Re-running setup safely reapplies `CREATE ... IF NOT EXISTS` schema
   statements and updates that user's password.

3. Run the real `app.server.app` WSGI callable, then open the printed login URL:

   ```bash
   python scripts/run_local_postgres_app.py \
     --database-url postgresql://postgres:postgres@localhost:5432/teleroute_local
   # http://127.0.0.1:8000/login
   ```

   Optionally copy `.env.postgres.local.example` to `.env` as a reference for the
   same guarded runtime settings. Both local scripts refuse database hosts other
   than `localhost` and `127.0.0.1`.

4. To exercise login plus the routes, tariffs, phones, companies, and provider
   changes pages without a browser, use the password printed/chosen during setup:

   ```bash
   python scripts/local_postgres_app_smoke.py \
     --database-url postgresql://postgres:postgres@localhost:5432/teleroute_local \
     --password 'the-local-login-password'
   ```

Stop the container without deleting its data:

```bash
docker compose -f docker-compose.postgres.yml down
```

To completely reset **only this local database**, stop the stack and delete its
named volume, then repeat setup:

```bash
docker compose -f docker-compose.postgres.yml down -v
```

SQLite is no longer the recommended local runtime. It remains physically present
and supported as a legacy, migration, and demo/test path; for example:

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

The PostgreSQL runtime remains protected by `POSTGRES_RUNTIME_ENABLED=1`. Local
bootstrap does not alter production secrets or deployment configuration.
