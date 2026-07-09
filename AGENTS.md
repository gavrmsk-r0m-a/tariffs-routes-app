# AGENTS.md

## Project purpose

TeleRoute is an internal operations MVP for replacing Excel-based workflows around routes, provider changes, phone pools, tariffs, companies, HLR, and admin data.

TeleRoute is not a CRM or ERP. Keep it simple, stable, and operational.

## General rule

Do not refactor working business logic unless explicitly asked.

## UI-only task rule

If a task says UI, design, layout, or theme:

- do not change backend logic;
- do not change database schema;
- do not change API request or response handling;
- do not change the HLR data pipeline;
- do not change CSV export behavior unless explicitly asked.

## HLR caution rule

HLR is high-risk.

- Do not replace server-rendered HLR tables with frontend-rendered state.
- Do not reintroduce JSON hydration for the HLR result table.
- Do not touch HLR API or data mapping for UI-only tasks.

HLR UI changes must preserve:

- server-rendered rows;
- existing HLR API;
- existing CSV export;
- existing column manager;
- existing filters unless the task explicitly targets filters.

## New theme rule

Do not edit `light-v2` directly for large refactors.

Create an isolated new theme under:

```css
html[data-theme="tele-route-pro"]
```

Existing `light-v2` and `dark` themes must remain untouched.

## Table rule

Prefer existing helpers:

- `data_table()`
- `table_card()`
- `table_footer()`
- `column_settings()`

Do not create custom table systems unless there is a strong reason.

## Filter rule

Prefer existing `filter_card()` for normal pages.

HLR can remain custom because it has special status chips.

## Modal rule

Use existing modal and form patterns.

Do not half-migrate admin inline forms or remote edit modals.

## Testing rule

For any UI change:

- run `python -m py_compile app/server.py`;
- run relevant focused tests if available;
- if not possible, state what was not tested;
- do not claim browser behavior unless actually checked manually or via browser runner.
