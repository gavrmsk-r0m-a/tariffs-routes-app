# Stage 69H — audit of confirmation-flow state preservation

## UX rule

An expected, user-correctable validation or confirmation error must not make the
operator enter already submitted data again. Modal, inline, and separate review
flows must preserve their applicable text, textarea, select, radio, checkbox,
entity-selection, and preview state. The error belongs beside the control that
can correct it. Server-side business guards remain authoritative; client-side
checks may only improve the interaction.

## Method and scope

The audit inspected `app/server.py` forms, POST dispatch and error rendering for
confirmation fields and copy, acknowledgement controls, destructive and bulk
actions, preview/apply endpoints, and back/edit links. Ordinary Save/Cancel
actions were excluded. The following are the **two real confirmation flows** in
the current application.

| Flow | Route / endpoint | UI type | Confirmation mechanism | State preserved now? | Error location | Back / edit behavior | Bug found? | Changed? |
|---|---|---|---|---|---|---|---|---|
| Dictionary linked-record rename | `POST /admin/dictionaries/<section>/<id>/update` | A — modal | `rename_mode=update_linked`, linked-record count preview, and required `confirm_update_linked` checkbox | Yes: entity id, submitted inputs, textarea/comment, selects, active toggle, rename-mode radio, checkbox, and recalculated counts | Inline in the modal's mass-update warning beside the checkbox | Not a separate step; the modal remains open with submitted state | No (Stage 69G behavior verified) | No |
| Remove selected numbers from a route | `POST /routes/<id>/numbers/remove` | B — inline form | Required `confirm_remove` acknowledgement checkbox; destructive submit (replacing the former browser-only `confirm()` guard) | Yes: reason, selected link IDs, and acknowledgement state survive expected errors | Inline in the form's confirmation warning beside the checkbox | Not a multi-step flow; correction happens in the same form | Yes: error rendering retained neither reason nor selected IDs, and confirmation was client-side only | Yes, minimal server-rendering and guard fix |

There are **no type C separate review/confirmation pages**, and consequently no
application-owned “Вернуться и исправить” action belonging to a multi-step
confirmation flow. The generic validation error page uses that label, but it is
not a review step and was not counted as one.

## Findings and coverage

### Dictionary linked-record rename — verified unchanged

The server rejects linked snapshot updates unless `confirm_update_linked=1`.
On the expected error, the dictionary page reconstructs the edit modal from the
submitted form data. The modal remains open, displays the friendly confirmation
message once beside the checkbox, preserves the selected rename mode and all
other submitted controls, and rebuilds the linked-record preview from the
server-side source of truth. Existing focused tests cover the missing-confirmation
guard, checked and unchecked checkbox rendering, mode/input preservation,
preview preservation, and non-execution without acknowledgement.

### Route-number removal — fixed

Previously, a browser `confirm()` was the only acknowledgement and the generic
error rerender discarded the removal form's reason and selected link IDs. The
flow now has a required, server-validated `confirm_remove` checkbox. Expected
errors rerender the same inline form with its reason, selected link checkboxes,
and confirmation checkbox restored. The friendly error is inside the
confirmation block. Unexpected database/runtime errors still follow the existing
error classification and are not converted into friendly validation.

Focused regression tests cover:

- missing acknowledgement blocks the operation and preserves reason and selected
  entity ID;
- a later known business-rule error preserves reason, selected entity ID, and
  checked acknowledgement;
- missing entity selection remains blocked, preserves the reason and checked
  acknowledgement, and removes no links;
- existing successful removal/history tests now submit the explicit server-side
  acknowledgement.

No new client-side JavaScript was added.

## Audited candidates that are not confirmation flows

- `POST /admin/import/preview` renders an optional preview within the same import
  form and already preserves `entity_type`, `mode`, and `csv_data`. Apply remains
  directly available from the initial form, so this is not a required
  confirmation/review step and has no Back/Edit transition.
- `password_confirm` on user creation, password reset, and forced password change
  verifies duplicate secret entry; it is field validation rather than an
  acknowledgement of an operational action. Password fields are intentionally
  not echoed into HTML.
- Provider-change bulk scopes and route bulk-add perform bulk work but expose no
  acknowledgement or review step, so they were not counted as confirmation flows.
- `review_required` on phone records is a business-data flag, not a user
  acknowledgement or review-page transition.
- Ordinary deactivate/delete buttons without an acknowledgement control or
  review step were not counted, consistent with the audit definition.
