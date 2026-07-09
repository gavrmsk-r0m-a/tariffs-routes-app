# UI Audit Summary

The UI/UX audit found that TeleRoute is a mostly server-rendered Python HTML application. UI rendering, CSS, JavaScript, and page rendering are concentrated mainly in `app/server.py`.

## Key findings

- The app is server-rendered.
- UI is concentrated in `app/server.py`.
- `light-v2` works, but it is fragile because it is implemented as several layers of CSS overrides.
- Reusable helpers already exist and should be preferred for future UI work.

## Biggest risks

- Inline JavaScript inside Python strings.
- Duplicated CSS.
- Duplicated table logic.
- Page-specific hacks.
- HLR custom frontend behavior.
- Remote modal fetch/import behavior.

## Safest strategy

- Document rules before broad refactors.
- Create an isolated new theme instead of editing `light-v2` directly.
- Refactor in small, reviewable steps.
