# Codex Design Rules

## Purpose

These rules define how Codex should work on UI/design tasks in this repository.

The application is an internal operational tool for routes, tariffs, purchased numbers, calling campaigns, provider changes, server priorities, routing schemes, and administration.

Design work must improve readability, visual hierarchy, consistency, and usability without changing business behavior.

---

## Core Rule

For design tasks, Codex must not change business logic.

UI polish is allowed.  
Business behavior changes are not allowed unless explicitly requested.

---

## Before Making Changes

For design review tasks, Codex must first inspect the current UI and provide a plan.

When the user asks for a design review:
- do not edit files;
- do not refactor code;
- do not apply changes immediately;
- first explain what is visually weak;
- then propose a small set of design directions;
- wait for user approval before changing code.

---

## Allowed Design Work

Codex may improve:
- CSS variables;
- theme tokens;
- spacing;
- borders;
- shadows;
- typography;
- table readability;
- sidebar states;
- button states;
- form states;
- modal styling;
- hover states;
- focus states;
- disabled states;
- selected states;
- badge/status colors;
- scrollbars;
- text selection styling.

Codex may adjust HTML classes only when needed for styling.

---

## Forbidden Without Explicit Permission

Do not change:
- database structure;
- migrations;
- business logic;
- route handling;
- POST endpoints;
- URLs;
- permission logic;
- role matrix behavior;
- authentication logic;
- password reset behavior;
- exports;
- filters;
- column settings behavior;
- table data calculations;
- dashboard metric logic;
- snapshot logic;
- CRUD behavior;
- existing page availability;
- menu structure.

Do not add:
- new dependencies;
- new frameworks;
- glassmorphism;
- liquid design;
- blur-heavy effects;
- decorative hero banners;
- marketing-style sections;
- unnecessary animations;
- new business features.

Do not implement:
- HLR;
- Spam Checker;
- Check-in;
- HR functionality;
unless the user explicitly starts that task.

---

## Theme Safety

The repository currently has multiple themes.

When working on Light 2.0:
- scope changes to `light-v2` whenever possible;
- do not break MVP theme;
- do not break dark theme;
- do not rename theme localStorage keys;
- do not remove theme migration logic;
- do not disable theme selector behavior.

---

## Light 2.0 Direction

When working on Light 2.0, follow:

`docs/design/themes/routeops-light-2-theme.md`

The theme direction is:

**Teal / Eucalyptus + Warm Orange**

Light 2.0 must not become:
- a copy of MVP theme;
- a generic pale admin panel;
- a blue Windows-style UI;
- a glass/liquid Apple-like UI.

---

## UI Review Checklist

When performing a UI review, use:

`docs/design/UI_REVIEW_CHECKLIST.md`

Do not only inspect the dashboard.

Always consider:
- dashboard;
- routes;
- tariffs;
- purchased numbers;
- calling campaigns;
- provider changes;
- server priorities;
- company routing settings;
- users;
- dictionaries;
- modals;
- dense tables;
- collapsed sidebar;
- theme selector.

---

## Density and Practicality

This app is used for operational work.

Do not make the UI too spacious or decorative.

Tables must remain practical.  
Forms must remain clear.  
Actions must remain fast.  
Modals must remain readable.

A good result is a polished internal product, not a landing page.

---

## Visual Hierarchy

Codex should avoid making every card and button look identical.

Use hierarchy:
- primary actions;
- secondary actions;
- warning/provider actions;
- destructive actions;
- disabled actions;
- neutral navigation.

Provider-change related UI may use warm orange.

Normal operational UI should use teal/eucalyptus/sage.

Danger should use calm red only when appropriate.

---

## Accessibility and Readability

Always preserve or improve:
- text contrast;
- focus states;
- hover states;
- disabled readability;
- keyboard usability;
- visible text selection;
- readable table headers;
- readable form labels.

Do not rely on color alone when text or icons are available.

---

## Testing

For design-only changes, run at minimum:

```bash
python3 -m py_compile app/server.py
python3 -m pytest tests/test_server.py::ServerSmokeTest::test_theme_toggle_is_clickable_and_persistent_scripted_control -q
