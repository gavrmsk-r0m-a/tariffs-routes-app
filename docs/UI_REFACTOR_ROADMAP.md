# UI Refactor Roadmap

Step-by-step UI refactor plan based on the UI/UX audit.

## Step 1 — Create new theme shell

- Theme name: `tele-route-pro`.
- Scope the theme under `html[data-theme="tele-route-pro"]`.
- Do not modify `light-v2`.
- Acceptance: existing themes are unchanged.

## Step 2 — Standardize tables

- Use `data_table()`, `table_card()`, `table_footer()`, and `data-col`.
- Do not change queries, exports, or permissions.
- Acceptance: tables render the same data.

## Step 3 — Standardize filters

- Use `filter_card()`.
- Do not change query semantics.
- Preserve saved filter behavior.

## Step 4 — Standardize modals

- Align footer, save, and cancel placement.
- Preserve remote edit fallback.
- Do not half-migrate admin inline edits.

## Step 5 — Standardize action toolbars

- Introduce an `action-toolbar` pattern.
- Preserve exports and permissions.

## Step 6 — Polish HLR

- Keep the work UI-only.
- Preserve server-rendered HLR rows.
- Preserve the API and data pipeline.
- Preserve CSV export.

## Step 7 — Polish provider changes

- Preserve the operational workflow.
- Preserve Telegram notifications.
- Preserve filters and export.

## Step 8 — Polish admin pages

- Preserve roles, permissions, and dictionary behavior.
- Align tables, badges, and forms.
