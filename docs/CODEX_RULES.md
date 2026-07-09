# Codex Rules

Practical checklist for future Codex tasks in TeleRoute.

## 1. Before editing

- Identify whether the task is UI-only, backend, database, API, or mixed.
- If the task is UI-only, do not touch backend, data, or API behavior.

## 2. For HLR tasks

- Never change HLR API integration unless explicitly requested.
- Never replace the server-rendered table with a frontend-rendered table.
- Preserve CSV export.
- Preserve status mapping.
- Preserve the column manager unless asked.

## 3. For theme tasks

- Create a new theme first.
- Do not modify `light-v2` directly for broad visual changes.
- Keep the dark theme working.
- Keep the old theme removable.

## 4. For table tasks

- Use existing table helpers when possible.
- Keep `data-col` consistency.
- Do not break CSV export.
- Preserve permissions.

## 5. For modal tasks

- Do not break remote edit fallback.
- Do not change form `action`, `method`, or `name` fields.
- Preserve save and cancel behavior.

## 6. For tests

- Run `py_compile`.
- Run focused tests.
- Report unrelated failures honestly.
- Do not claim the full suite passed if it did not.
