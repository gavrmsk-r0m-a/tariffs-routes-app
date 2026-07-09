# UI Style Guide

This guide documents the target UI design system for future TeleRoute UI work.

## 1. Page structure

- Breadcrumbs stay in the global page-top area.
- Use one visible page title where needed.
- Do not duplicate the breadcrumb hierarchy in the page title.
- For pages like HLR, a big title can be omitted if breadcrumbs and the panel title are enough.

## 2. Action toolbar

Standard action toolbar pattern:

- primary actions on the left;
- export, columns, and utilities on the right;
- table-local actions can go near or below the table.

## 3. Tables

Standard table rules:

- use `data_table()` where possible;
- every `th` and `td` should have a matching `data-col`;
- wrap tables in `table_card()`;
- use `table_footer()`;
- use `column_settings()` unless the page is special;
- the actions column should be last and locked;
- avoid horizontal page scroll; the table container may scroll.

## 4. Filters

Standard filter rules:

- use `filter_card()`;
- collapse filters by default if there are no active filters;
- open filters if active or restored;
- the reset link must clear saved state;
- export should respect active filters.

## 5. HLR exception

HLR can use the custom HLR Tech Spec layout and HLR status chips.

HLR must preserve server-rendered table rows and must not use fragile frontend table hydration.

## 6. Modals

- Save and cancel buttons belong in the footer.
- Save goes on the left.
- Cancel goes next to save.
- Destructive actions use danger style only.
- Avoid inconsistent modal footer placement.

## 7. Buttons

Use these button roles consistently:

- **Primary**: the main page or form action.
- **Secondary**: a non-destructive alternative action.
- **Danger**: destructive or irreversible actions.
- **Ghost**: low-emphasis actions that should not dominate the page.
- **Icon**: compact icon-only controls; include accessible labels.
- **Export**: export/download actions, usually grouped with utilities.

## 8. Status colors

Use status colors consistently:

- success, ok, live, active = green;
- warning, review, unknown = orange/yellow;
- danger, error, dead, bad_format = red;
- neutral, default = grey.

## 9. Compact panels

Use compact panel patterns where space matters:

- `compact-card`;
- `compact-card-header`;
- `compact-card-body`.

Use compact panels for HLR, provider changes, and admin pages where space matters.

## 10. Tooltips/help

- Use `title` for simple native tooltips.
- Use `aria-label` for icon-only controls.
- Use `data-tooltip` only where a custom tooltip is already used.
- Avoid huge help panels unless needed.
