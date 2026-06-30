# RouteOps Light 2.0 Theme

## Purpose

RouteOps Light 2.0 is the main modern light theme for the TeleRoute / RouteOps internal operational application.

This theme is intended for daily work with:

* routes;
* tariffs;
* purchased numbers;
* calling campaigns;
* provider changes;
* server priorities;
* routing schemes;
* administration pages;
* dense operational tables and modals.

The goal is not to create a decorative landing-page style.
The goal is to create a calm, readable, modern internal product UI with a clear visual identity.

---

## Design Direction

Theme direction:

**Teal / Eucalyptus + Warm Orange**

The theme should feel:

* light;
* calm;
* modern;
* operational;
* readable;
* slightly more distinctive than a generic admin panel;
* more expressive than the old MVP theme.

The theme should not feel:

* like the old MVP theme with slightly different colors;
* like Windows-blue enterprise UI;
* like Apple liquid / glass design;
* like a transparent blurred interface;
* like a marketing landing page;
* like a colorful toy dashboard.

---

## Core Visual Idea

Use a soft eucalyptus / sage base for the application shell and navigation.

Use teal as the main product accent.

Use warm orange for provider changes, warnings, and operational attention states.

Use white cards and solid surfaces.
Do not use glassmorphism, blur, transparent panels, or liquid effects.

---

## Color System

Recommended token direction:

### Base

```css
--bg: #F3F7F4;
--surface: #FFFFFF;
--surface-soft: #EEF5F1;
--surface-muted: #EAF3EF;

--sidebar-bg: #EAF3EF;

--border: #D8E3DE;
--border-strong: #C3D4CD;

--text: #1F2933;
--text-muted: #5F6F68;
--text-soft: #7A8780;
```

### Primary Accent — Teal / Eucalyptus

```css
--accent: #0F766E;
--accent-hover: #0B5F59;
--accent-strong: #0A4F49;
--accent-soft: #DDF3EE;
--accent-border: #A9D8CF;
```

Use teal for:

* primary buttons;
* active navigation;
* links;
* active tabs;
* selected pagination;
* focus states;
* key icons;
* active badges;
* positive operational emphasis.

Do not use default bright blue as the main accent.

### Sage / Green

```css
--success: #2F7D50;
--success-soft: #E8F3EA;
--success-border: #B9DEC0;
```

Use green for:

* active states;
* success states;
* enabled statuses;
* positive indicators.

### Warm Olive

```css
--olive: #6F7A3A;
--olive-soft: #EEF1DE;
--olive-border: #CDD6A7;
```

Use olive carefully for:

* secondary calm accents;
* neutral operational categories;
* soft supporting visual details.

### Warm Orange / Provider Change

```css
--warning: #D97706;
--warning-hover: #B45309;
--warning-soft: #FFF1DD;
--warning-border: #F2C078;

--provider-accent: #D97706;
--provider-soft: #FFF4E5;
--provider-border: #F2C078;
```

Use warm orange for:

* provider changes;
* warning states;
* attention states;
* operational switching;
* “Смена провайдеров” navigation item, dashboard card, quick link, and related statuses.

Orange should be noticeable, but not aggressive.

### Danger

```css
--danger: #DC2626;
--danger-soft: #FEE2E2;
--danger-border: #FCA5A5;
```

Use danger only for:

* destructive actions;
* serious errors;
* critical statuses.

---

## Layout Principles

The layout should remain practical and dense enough for operational work.

Do not introduce:

* large decorative hero banners;
* marketing blocks;
* unnecessary illustrations;
* big empty areas;
* animation-heavy UI.

Use:

* clear page hierarchy;
* readable headings;
* compact cards;
* strong tables;
* consistent spacing;
* calm shadows and borders.

---

## Sidebar

Sidebar should be one of the main visual identity areas.

Expected style:

* eucalyptus / sage background;
* active item clearly visible;
* active item should use teal accent;
* hover state should be visible but soft;
* disabled items should be muted but still readable;
* icons should use theme tokens;
* “Смена провайдеров” may use warm orange accent.

Collapsed sidebar behavior must not be broken.

Do not change menu structure while working on theme polish.

---

## Dashboard

Dashboard should feel like a modern operational control panel.

Metric cards:

* white surfaces;
* visible but soft borders;
* subtle shadows;
* colored icon backgrounds;
* teal/sage for normal metrics;
* warm orange for provider change metric;
* red only for real incident/error states.

Quick links:

* should not all look identical;
* use subtle icon backgrounds;
* provider change quick link can use orange accent;
* normal links use teal/sage accents.

Event feed:

* readable rows;
* clear status dots;
* soft row hover;
* visible separation;
* no excessive decoration.

Do not reintroduce a hero banner.

---

## Tables

Tables are the most important working component.

They must be readable, practical, and visually clear.

Requirements:

* table header should be visually distinct from rows;
* row borders should be visible but soft;
* row hover should be noticeable;
* statuses and badges should be readable;
* action buttons should remain visible;
* horizontal scroll should remain usable;
* dense data should not feel washed out.

Recommended direction:

```css
--table-header-bg: #EAF3EF;
--table-row-hover: #F0F8F5;
--table-border: #D8E3DE;
```

Avoid extremely pale table styling where rows and headers blend into the page.

---

## Forms

Inputs, selects, and textareas should be solid and readable.

Requirements:

* white or near-white background;
* visible borders;
* teal focus state;
* readable placeholder;
* consistent height;
* no blue focus ring unless it is replaced by theme teal;
* required markers must remain correctly aligned.

---

## Modals

Modals should be solid light surfaces, not glass panels.

Requirements:

* white modal background;
* no blur/glass/liquid effect;
* clear heading;
* visible input borders;
* clear footer buttons;
* readable form labels;
* good contrast inside permission tables and dense modal content.

Special attention:

* edit phone modal;
* edit user modal;
* permission matrix;
* provider change modal.

---

## Buttons

Primary:

* teal background;
* white text;
* darker teal hover.

Secondary:

* white or soft surface;
* visible border;
* calm hover.

Warning / provider action:

* warm orange;
* use for provider changes and warning actions.

Danger:

* calm red;
* only for destructive actions.

Avoid using bright default blue as the main button color.

---

## Badges and Statuses

Use consistent status colors:

* active / success: green;
* warning / pause / attention: warm orange;
* provider change: warm orange;
* error / critical: red;
* archive / disabled / neutral: grey;
* requires review: amber/orange, not red.

Status should not rely only on color if text or icon can clarify the meaning.

---

## Selection and Copy UX

Text selection must be visible in Light 2.0.

Recommended:

```css
html[data-theme="light-v2"] ::selection {
  background: rgba(15, 118, 110, 0.22);
  color: #10201D;
}
```

Do not break the existing enhanced text selection / double-click behavior in tables.

Do not add row-level copy icons unless explicitly requested.

---

## Interaction States

Every important UI component should have clear states:

* default;
* hover;
* active;
* focus;
* disabled;
* selected.

Especially check:

* sidebar items;
* table rows;
* buttons;
* input/select/textarea;
* dropdown menu items;
* theme selector;
* user menu;
* pagination;
* action icons.

---

## What Not To Do

Do not:

* make Light 2.0 a copy of MVP theme;
* use blue as the main accent;
* add glassmorphism;
* add liquid design;
* add blur effects;
* add decorative hero banners;
* make the UI look like a landing page;
* add new business features;
* change database structure;
* change URLs;
* change endpoints;
* change permission logic;
* change export behavior;
* change menu structure;
* break MVP theme;
* break dark theme.

---

## Success Criteria

Light 2.0 should look like a separate, intentional modern light theme.

It should be:

* more expressive than MVP;
* calmer than a colorful dashboard;
* more modern than a generic admin panel;
* clear enough for daily operational work;
* consistent across dashboard, tables, forms, modals, and sidebar.

A good result should feel like:

**“This is the main working theme of the product.”**

Not:

**“This is the old MVP theme with a green tint.”**

