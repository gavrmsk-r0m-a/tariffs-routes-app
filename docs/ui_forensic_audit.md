# TeleRoute frontend forensic audit

**Audit baseline:** `bdba421e314ab7304d04437ce7f5864b7b4a6331` (`work`)
**Scope:** static forensic review of `app/server.py`, all files under `tests/`, and the other tracked application files.
**Method:** line-oriented searches, Python/regex inventories, focused source reading, and existing server tests. No production source, schema, API, HLR mapping, or runtime behavior was changed. This report deliberately separates an observed fact from an inference and from an item that needs a browser check.

## A. Executive summary

TeleRoute's frontend is a server-rendered application whose shell, global CSS, shared behavior, page HTML, and several page controllers are concentrated in the 10,998-line / 853,045-byte `app/server.py`. The main `page()` template alone contains one approximately 2,780-line style block and one approximately 806-line script block. Additional inline CSS/JS is emitted by Spam Checker, login, filters, route AON, HLR, bulk-phone, provider-change, and tariff-edit renderers. This makes source order—not a documented component contract—the effective frontend architecture.

The two selectable themes are **light-v2** and **dark**. Historic stored values are intentionally normalized: `mvp`, `calm-blue`, and `terminal-paper` become `light-v2`; `cyber-sketch` becomes `dark`. The selector cannot choose a legacy value. CSS remains for `mvp` and `terminal-paper`, but the normal page script immediately replaces either stored value before paint completion; `calm-blue` and `cyber-sketch` have no CSS blocks. These names are migration inputs, not current themes.

The audit counted **363 `!important` declarations**, 581 source lines mentioning `light-v2`, 195 mentioning `dark`, 51 `overflow:hidden` spellings, 17 fixed-position declarations, 25 absolute-position declarations, 6 sticky-position declarations, 35 `createElement` calls, 219 `querySelector` occurrences, and 108 event-listener registrations. Counts describe the Python source, including embedded templates, rather than a single rendered page.

### Major findings by severity

| Severity | Count | Summary |
|---|---:|---|
| CRITICAL | 0 | Static review found no proven data-loss, unusable-all-pages, or HLR-pipeline defect. |
| HIGH | 7 | Global all-form submit mutation; remote modal script execution/focus gaps; provider-change controller coupling; light-v2 cascade pile-up; modal overflow/footer contracts; theme asymmetry; desktop-first shell with no shell breakpoint. |
| MEDIUM | 12 | Nested scroll regions, column DOM reordering, fixed popovers, repeated modal implementations, global text/URL selector behavior, details-as-dialog semantics, CSS/behavior class coupling, page-local duplicate controllers, table width persistence, CDN dependency, short-viewport pressure, and uncertain stale selectors. |
| LOW | 8 | Benign duplicate tokens/rules, labels embedded around controls, intentional hidden helpers, cosmetic selector specificity, tooltip duplication, `mvp-*` storage naming, utility wrapper depth, and historical class names. |

### Highest-value conclusions

1. Do **not** start by deleting legacy theme blocks or `!important`; first freeze rendered contracts with page/theme/browser coverage.
2. The largest architectural risk is JavaScript mutating markup based on visual classes (`.modal-*`, `.edit-action`, `.admin-edit-*`, `.scope-card`) and broad selectors (`form`, `button:not([type])`, `td[data-col="actions"] details`).
3. The light-v2 layer is not merely a palette. It repeatedly changes layout, icon rendering, controls, tables, and page-specific components; therefore an external skin cannot safely be “CSS variables only” yet.
4. Dark is structurally served the same HTML, but is not cascade-equivalent: light-v2 has substantially more and later page-specific rules. Dark commonly falls back to generic CSS and several modal forms carry literal white/blue styling.
5. HLR is specialized but coherent: rows remain server rendered, and its JS filters/reorders existing rows/cells. Its nested scrolling and persisted widths merit regression tests, not immediate rewriting.

## B. Current frontend architecture

### B.1 Rendering and ownership map

| Concern | Current location | Coupling |
|---|---|---|
| Application shell | `sidebar()` (lines 554–604), `theme_selector()` (638–647), `breadcrumbs()` (649–681), `page()` (683–4302) | Python authorization and title matching decide navigation HTML; the same `page()` owns CSS, DOM, and behavior. |
| Global CSS | The `<style>` in `page()`, lines 693–3472 | Base components, both live themes, historical themes, page layouts, responsive rules, and successive polish patches share one cascade. |
| Global JS | The `<script>` in `page()`, lines 3493–4299 | Network status, form submission, theme/sidebar preferences, menus, modal enhancement, remote editing, copy/popovers, and table columns initialize on every normal page. |
| Shared tables | `table_card()`, `table_footer()`, `data_table()`, `column_settings()` (4845–4902) plus global CSS/JS | Server-rendered cells are moved, hidden, and resized by JS; preferences are keyed in localStorage. |
| Shared filters/forms | `filter_card()` and `form_card()` (4806–4833) | Server HTML generates `<details>` and an inline per-instance filter script; open state is also encoded in a hidden field/cookie workflow. |
| Major business pages | Dashboard 7201; routes 7434; tariffs 7573; phones 7620; bulk phones 7804; companies 7839; provider changes 8763; admin pages 8806 onward | Query/database output, HTML strings, component selection, classes, and page scripts are assembled in the same Python functions. |
| Specialized pages | Spam Checker 4366, login 4620, HLR 6644 | Each adds its own CSS and/or JS rather than consuming only shell components. Login is a separate document. |
| Provider-change UI | `routing_event_form()` (8022–8645), `provider_changes_page()` (8763–8804) | The most tightly coupled page: server data is serialized into JS, IDs/classes encode workflow steps, and JS creates/options/hides/moves states. |
| Edit modal path | edit-form/page functions around 9612–9790 plus global remote-modal code 3756–3884 | A full/partial HTTP response is parsed, imported into a generated card, enhanced, and its scripts are recreated and executed. |

`page()` is therefore simultaneously a layout template, theme registry, design-system approximation, responsive stylesheet, client runtime, and compatibility layer. Page functions then add business rendering and page-specific scripts on top. The major concern is not file size by itself; it is that changes to one concern enter the same source-order cascade and global initialization path as unrelated concerns.

### B.2 Inline style/script inventory

* **Shell style:** lines 693–3472.
* **Shell behavior:** lines 3493–4299, followed by externally hosted Tabler JS at line 4300.
* **Spam Checker:** compressed page-local `<style>` at line 4452.
* **Login:** standalone CSS at line 4627 and lock-timer script at 4639.
* **Change-password:** separate compact page marked `data-theme='mvp'` at line 4649; it does not consume the shell's historical `mvp` CSS.
* **Filter card:** generated inline script at 4824 for `<details>` open-state synchronization.
* **Route AON:** script returned by `route_aon_script()` at 5091–5152.
* **HLR:** large page-local script begins at 6697 and operates on the server-rendered results table.
* **Bulk phones:** compact script at 7834.
* **Provider changes:** large controller script begins at 8273 inside the generated form.
* **Tariff edit:** `tariff_currency_script()` at 9612–9620; remote modal loading can recreate and execute it.

### B.3 Preferences and state

| Storage/state | Behavior | Risk |
|---|---|---|
| `mvp-theme` | Read, normalized through aliases, immediately rewritten as `light-v2`/`dark` (3555–3561). | Compatibility behavior is test-pinned; changing the key or alias semantics is a migration. |
| `mvp-sidebar-collapsed` | Controls `.sidebar-collapsed`; opening theme/user/admin UI may expand the sidebar (3572–3659). | A visual state class is also a behavioral contract. |
| `teleRoute.table.<key>` | Column visibility, order, and pixel widths (4132 onward). | DOM cell order changes; stale widths can cause horizontal expansion after layout changes. |
| HLR-specific storage | HLR column/filter preferences appear in its page-local controller. | It overlaps conceptually with the shared column manager but must remain independent until parity is proven. |
| Filter cookie/hidden field | Python `load_filter_state()` and `filter_card()` preserve filters/open state. | This is server/client/cookie coupling, not merely presentation. |

## C. Major risks ranked by severity

### HIGH

1. **The global form handler changes every form's submission lifecycle.** It disables the submitter for any `form`, restores it on `pageshow`, and injects errors into every form on offline events (3523–3554). Dynamic and remote-modal forms created after initial load do not receive the per-form submit listener. This is both broad and inconsistent.
2. **Remote-edit modals execute scripts copied from fetched documents.** `runRemoteModalScripts()` recreates inline scripts (3768–3775); form markup is imported and replaced during validation (3792–3806). This is a fragile lifecycle and creates risk of duplicate global identifiers/listeners if a page-specific script assumes one document instance.
3. **Modal accessibility/lifecycle is incomplete.** Generated remote cards use a `<section>` and overlay but no `role="dialog"`, `aria-modal`, labelled-by relation, focus trap, focus restoration, or body scroll lock (3815–3874). Escape and overlay close work, but keyboard containment does not.
4. **light-v2 has successive, conflicting overrides.** For example active side links are restyled at 2396, 2521, and 2713; the last equally/more-specific `!important` declarations win. A local fix can silently invalidate earlier provider/nav decisions.
5. **Modal layout is implemented as multiple fixed grid contracts.** Generic modal CSS (1337–1437), provider-change modal CSS, and route/tariff/phone/company/naming/reason/user dialogs (3108–3436) each define fixed positioning, max-height, internal overflow, and footer placement. Short-height regressions can hide content or create multiple scroll regions.
6. **Dark is not cascade-equivalent to light-v2.** There are 581 vs 195 source lines mentioning the themes, and 184 `!important` occurrences in the late light-polish range vs 34 in the main dark range. Literal `background:#fff` and blue actions in page-specific dialog rules can bypass dark tokens.
7. **The shell is desktop-first without a shell-level breakpoint.** `.app-shell` remains a 258px + content grid and the sidebar remains `height:100vh; position:sticky` (839–840); component breakpoints exist, but no audited rule converts the shell for narrow widths. At 1366 it is viable but leaves roughly 1,108px before workspace padding; below that, tables rely heavily on internal scrolling.

### MEDIUM

1. `.table-card` hides overflow while `.table-scroll` owns horizontal/vertical scrolling; HLR adds `.hlr-workspace {overflow-x:clip}`, `.hlr-results-area {overflow:hidden}`, and a viewport-derived inner scroller. This can intentionally contain tables but complicates floating panels and keyboard scroll discovery.
2. Shared column code physically appends cells to reorder them (4170–4176). Visual and accessibility order remain aligned after the mutation, but any page code relying on original sibling order can break.
3. The cell popover is `position:fixed`, hand-positioned, and updated on every capturing scroll (899–904, 4026–4091). It has dialog semantics but no focus trap/restoration.
4. Provider-change JS relies on many IDs, visual classes, and workflow order; `sync()` centrally toggles descendants and disabled/required state. Skins that wrap or relocate controls can break it.
5. Modal enhancement moves the submit button and creates footer/cancel elements for older forms (3662–3691); admin-edit enhancement independently repeats this pattern (3891–3918).
6. Behavior recognizes danger buttons through `onclick` text and action URL suffixes in CSS (base rules around 873 and dark rules 2241–2245). Localization or changed confirmation wording changes styling.
7. `<details>/<summary>` serve filters, dropdowns, create dialogs, edit controls, and modal shells. Their native behavior differs from a dialog/menu contract and is then selectively overridden.
8. Several page controllers use IDs (`#routing-event-form`, HLR IDs, tariff currency IDs). They assume a single instance and can collide if full-page markup is imported into a modal.
9. Persisted column pixel widths apply `width`, `min-width`, and `max-width` inline (4191–4197). Those inline styles outrank skin CSS and can preserve obsolete geometry.
10. Google Fonts, Tabler CSS, and Tabler JS are runtime CDN dependencies. The offline banner does not make the initial UI independent of them.
11. `max-height:calc(100vh - …)` values coexist with fixed headers/footers and nested scroll areas. At 768px height they are plausible; at shorter viewports, the fixed modal/table budgets become tight.
12. Multiple unused-looking selectors remain in the main cascade. Most are safe only after rendered-page/browser coverage because markup may be conditional or JS-created.

### LOW

The duplicated `--accent-soft` property in the `mvp` token block is harmless because both values match. The `mvp-*` names for cookies/storage are historical naming rather than active theme selection. Screen-reader-only legends and hidden native radio inputs are intentional accessibility structures. Repeated wrapper layers (`table-page-container` → card → scroll → table) have real CSS/JS consumers. Tooltip `title`, `data-tooltip`, and ARIA labels duplicate text but serve different mechanisms. Several repeated declarations are identical, so their present result is stable even though maintenance cost is high.

## D. Theme and legacy-theme findings

| Name | Selectable now? | CSS exists? | JS reference? | Can become active in normal shell? | Classification | Evidence/conclusion |
|---|---|---|---|---|---|---|
| `light-v2` | Yes; default | Extensive | Label, option, normalization, storage | Yes | **ACTIVE** | Default fallback and one of two menu options. |
| `dark` | Yes | Extensive | Label, option, normalization, storage | Yes | **ACTIVE** | Second menu option. |
| `mvp` | No | Yes: token block plus shared historical selectors | Alias to `light-v2` | Not after normal initializer; a stored `mvp` is rewritten | **COMPATIBILITY / MIGRATION** | Tests explicitly assert the alias. Shell CSS is likely unreachable in steady state; `mvp_auth` and `mvp_filter_state` are unrelated cookie names. |
| `terminal-paper` | No | Yes: tokens and shared rules | Alias to `light-v2` | Not after normal initializer | **COMPATIBILITY / MIGRATION** | Stored-value migration is active; CSS is a high-confidence cleanup candidate only after checking pre-init/failure behavior. |
| `calm-blue` | No | No dedicated CSS found | Alias to `light-v2` | No; normalized | **COMPATIBILITY / MIGRATION** | Alias is live migration code; no theme block to delete. |
| `cyber-sketch` | No | No dedicated CSS found | Alias to `dark` | No; normalized | **COMPATIBILITY / MIGRATION** | Alias is live migration code; no theme block to delete. |

The standalone change-password document uses `data-theme='mvp'`, but it supplies its own simple inline style and does not load `page()` CSS/JS. Therefore it is evidence that the *name* still appears in active markup, not evidence that the large shell `html[data-theme="mvp"]` block is active there.

**Runtime nuance:** the normal document has no `data-theme` attribute in its opening `<html>`. Theme assignment occurs in the body-end script. Until that executes, variable-dependent styles have no theme token source except the later `:root, html[data-theme="mvp"]` compatibility token block. That fallback makes the old `mvp` association architecturally relevant even though JS normalizes the final state.

## E. CSS cascade findings

The table below prioritizes conflicts that change effective output. “Later wins” assumes the same state and no still-more-specific selector.

| Selector/component | Earlier declaration | Later declaration | Conflict and winner | Risk |
|---|---|---|---|---|
| light-v2 active side link | 2396: `#F8FAFB`, accent border, inset 4px teal | 2521: `#EFF6F5`, strong border/left accent; 2713 later changes it again to a blue gradient and different shadow | Later `!important` values win by source order/specificity | **High:** three design decisions coexist; editing the first is ineffective. |
| provider-change nav icon/link | 2398–2401: orange provider accent and orange active surface | 2575–2577 neutralize icon and make active state teal; 2782/2789 add sidebar-scoped neutral/hover rules | Later/scoped important rules win | **High:** semantic orange special case is partly overwritten, making intent unclear. |
| light-v2 row hover | 2416 and 2484 use token; 2562 hard-codes `#ECF5F3`; 2646 returns to token | Last applicable important declaration wins; token currently resolves similarly but not contractually identical | **Medium:** future token changes may appear not to work. |
| `.side-link.active` generic/history/live themes | Base 846 uses gradient/white; legacy 1448 makes historical themes surface-based; light/dark later specialize | Theme-qualified rules win; intended | **Low:** normal theming, but legacy CSS complicates reasoning. |
| action-cell edit controls | Base action button rules 1331 onward | light-v2 block 1837–1882 force 30px icon-only control with text-indent and pseudo-icon | Later high-specificity `!important` wins | **High:** accessibility text remains but presentation depends on generated pseudo-content/font and many forced dimensions. |
| status/dot badges | Base status rules, then light theme 2420–2425 | 2654–2658 force dot geometry/colors | Later important rules win | **Medium:** another component where theme polish defeats component tokens. |
| cards/details | Base `.card`, `details`, filter/form/table card declarations | broad dark grouping 2095–2103 and 2252–2270; light broad grouping 2406 and later page rules | Qualified/later rules win | **Medium:** unrelated card types share a selector list, so adding a class can inherit unintended shadows/backgrounds. |
| modal surface | Generic `.modal-card`/`.modal-form-card` (1337–1437) | theme rules 2190–2196 and 2430–2432; dedicated dialogs 3227 onward use literal `#fff` | Dedicated dialog rules are later and equally/more specific; literal white can win in dark | **High:** layout and color ownership are mixed. |
| modal action footer | Generic `.modal-actions` and page/provider footer rules | dedicated `*-dialog-footer` implementations with order and blue literals | Dedicated classes win | **Medium:** one concept has multiple non-equivalent DOM/CSS contracts. |
| filter/form grids | Base 985 onward | page-specific grids and late light-v2 filter rules | Page wrapper specificity generally wins | **Medium:** normal specialization, but shared markup cannot predict width without page context. |
| theme selector | Obsolete-looking `.theme-selector select` at 860–861 and focus selectors at 1453 | Actual selector is a button/menu | Rules do not match current generated theme selector | **Low:** stale CSS candidate, not a live conflict. |

Other meaningful duplication includes primary `<summary>` button state blocks separately repeated for provider changes, routes, tariffs, phones, companies, users, and admin filters. They are visually similar but not one component and use `!important` to overcome generic summary/details styling.

## F. `!important` findings

### Inventory

There are **363 declarations**. By coarse source region: 73 in base/early CSS (693–1694), 70 in the first light-v2 structural block (1695–2057), 34 in the dark block (2058–2374), 184 in later light-v2 polish (2375–3472), and 2 in page-local material after the shell. This is an approximate architectural distribution, not a semantic categorization.

Because one declaration can serve multiple purposes, the categories below are representative rather than mutually exclusive totals:

* **A — likely necessary compatibility override:** `[data-column-hidden="true"] {display:none!important}` and hidden workflow rows; these must beat table/grid display rules. The icon compatibility block for legacy links (1644–1691) also intentionally normalizes old markup.
* **B — defeats an earlier conflicting rule:** light-v2 active navigation at 2396/2521/2713, row hover at 2416/2484/2562/2646, and repeated provider-change link rules. These are the clearest cascade-debt cluster.
* **C — page-specific emergency patch:** provider-change primary summary (981–984), admin-user primary summary (1152–1161), and repeated route/tariff/phone/company summary states (3111–3173).
* **D — theme override:** dark cards/forms/footers (2186–2358) and light-v2 navigation/status/filter/table polish (2394 onward).
* **E — probably unnecessary:** declarations on already page-qualified selectors with late source position, such as some dedicated dialog checkbox display/margins (3363, 3412), likely do not need importance; prove through computed-style tests before removal.
* **F — dangerous/masks architecture:** the generic edit-action icon conversion uses dozens of important dimensions/content/text-hiding declarations (1837–1882); remote/admin modal positioning uses a long important block (1346–1363); primary-action state blocks repeat across pages. These make future skins unable to override components without escalating specificity.

### Accumulated-fix clusters

1. **Sidebar/active/provider-change navigation:** multiple generations at 1695–1827, 2391–2403, 2516–2527, 2573–2577, and 2701–2810.
2. **Action/edit controls:** base compact actions plus light-v2 icon replacement at 1829–1882 and behavior that adds `.admin-edit-save/.admin-edit-cancel`.
3. **Filter/create summaries:** provider, users, routes, tariffs, phones, companies, and admin-filter submit buttons each reproduce blue state rules.
4. **Light-v2 filters/cards/status rows:** late page polish repeatedly restates backgrounds, borders, and hover states after the nominal light theme.
5. **Provider-change modal:** an already specialized component has further important overrides and a dedicated JS controller; it should be frozen until focused tests exist.

No recommendation here is “remove all importance.” Hidden-state and compatibility declarations may be correct. The safe unit is one component contract with computed-style screenshots/tests in both themes.

## G. DOM / HTML findings

| Suspicious structure | Classification | Evidence and disposition |
|---|---|---|
| `table-page-container` → filter/form details → `table-card` → `table-scroll` → table | **POSSIBLY USED BY CSS / JS / TESTS** | Each layer has selectors; table scroll and column tools need containment. Do not flatten based on depth. |
| `<details class="modal-form-card">` used as create-modal state | **POSSIBLY USED BY JS / CSS / TESTS** | Global JS selects `details.modal-form-card[data-modal-details]`; tests assert modal markers. Not removable. |
| JS-created `.modal-actions` and cancel buttons | **POSSIBLY USED BY JS / CSS** | Compatibility enhancement for forms without dedicated footer. It can duplicate conceptual actions, but guards prevent an actual second cancel within the same enhanced form. Runtime verification is required across every form type. |
| JS-created `.admin-edit-actions` | **POSSIBLY USED BY JS / CSS / TESTS** | Separate fallback for inline admin edit forms; tests explicitly distinguish dedicated modal-close markup from generated admin cancel markup. |
| `data-modal-ready` wrapper in partial responses | **POSSIBLY USED BY JS / TESTS** | Remote loader imports its child nodes; multiple tests assert it. Safe removal: **no**. |
| Provider-change scroll-body and footer wrappers | **POSSIBLY USED BY CSS / TESTS** | Tests verify the action footer is outside the scroll body. It protects footer visibility and is not redundant. |
| HLR result/card/scroll wrappers | **POSSIBLY USED BY JS / CSS** | Preserve server-rendered rows and the existing column/filter/export pipeline. Do not normalize into generic tables yet. |
| `.page-crumbs` empty when no trail exists | **POSSIBLY USED BY CSS** | `page()` always emits the wrapper. It can be empty for unknown titles, but participates in `.page-top` layout. **NEEDS RUNTIME VERIFICATION** before removal/conditional rendering. |
| `.topbar` when both child helpers return empty | **POSSIBLY USED BY CSS** | Theme selector is normally always present, so it is not generally empty. |
| hidden native radios in `.scope-card` | **ACCESSIBILITY PURPOSE** | The input remains the semantic control while the label/card is visual. Do not replace with clickable divs. |
| hidden legends and `.sr-only` text | **ACCESSIBILITY PURPOSE** | Visually clipped, not stale. |
| change-password `data-theme='mvp'` | **POSSIBLY USED BY TESTS / COMPATIBILITY** | Separate page naming is historical; changing it provides little value without a broader auth-page contract. |
| `<section class="remote-edit-card">` generated by JS | **NEEDS RUNTIME VERIFICATION** | Not duplicate server DOM: it imports fetched form content. It lacks full modal semantics/focus management. |

### Duplicate IDs, forms, and stale markup

Static source contains repeated IDs because mutually exclusive page templates and create/edit variants use the same controllers. No concrete same-render duplicate ID was proven from static review. The remote-modal path can, however, import a form containing IDs already present on the list page; tariff currency IDs are the clearest collision candidate if a create form uses the same IDs. This requires rendered DOM checks, not an assertion of a present duplicate.

No concrete nested `<form>` was found. The global modal enhancer deliberately moves a submit button within its existing form rather than creating a form. Provider-change tests explicitly ensure the create form contract and footer placement. No hidden duplicate HLR result form/table was found; the results remain server-rendered.

The CSS classes prefixed `obsolete-` are still referenced by dashboard helpers/styles and therefore are not dead merely because of their names.

## H. JavaScript ↔ DOM coupling findings

### Fragile contracts

* **Visual classes as behavior:** `.modal-form-card`, `.modal-actions`, `.modal-cancel`, `.admin-edit-*`, `.remote-edit-*`, `.edit-action`, `.scope-card`, `.multi-option`, and `.column-settings-panel` are both styling and controller hooks.
* **Layout-sensitive traversal:** `saveButton.parentNode.insertBefore(...)`, `closest("table")`, `closest("form")`, `closest("[data-theme-selector]")`, `mover.closest("[data-col-row]")`, and `:scope >` queries require specific containment.
* **Broad document selectors:** every `form`; action-cell `details > summary`; every matching edit link; global Escape/click handlers. These can capture future skin controls unless isolated.
* **Text/URL coupling:** CSS uses `button[onclick*="Деактив"]`, `Удал`, `Отключ` and `form[action$="/deactivate"]`; remote edit identifies URLs with `/edit` and update forms with `/update`; title fallback derives entity text from an edit URL.
* **Element order mutation:** table columns are reordered with `appendChild`; modal save buttons are moved into generated actions; fetched modal children are imported/replaced.
* **Dynamic DOM:** connection errors, overlays/cards, modal footers/cancel buttons, fallback copy textarea, popovers, column resize handles, and provider-change options are created at runtime.
* **Listener multiplicity:** each column-settings instance adds document click/keydown and window resize/scroll listeners. It is bounded by settings instances, but a page with multiple tables multiplies global listeners. Remote scripts may attach again after each modal replacement.
* **Scroll manipulation:** popovers reposition on capturing scroll; provider-change workflow calls smooth `scrollIntoView`; column panels choose open-up based on viewport geometry.

No production use of JS `nth-child` selection was found; the `nth-child` occurrences are CSS row styling. Fragility instead comes from deep class/ID queries and containment/order assumptions.

### Recommended future hooks (not implemented)

Introduce hooks only after inventories/tests, keeping visual classes during migration:

* `data-component="modal|table|filter|sidebar|column-settings"`
* `data-action="open-modal|close-modal|save|edit|copy-column|toggle-sidebar"`
* `data-ui="modal-body|modal-footer|table-scroll|status|empty-state"`
* entity-neutral identifiers such as `data-edit-url`, `data-update-form`, and `data-danger-action`, replacing URL/text inference.

Controllers should initialize per component root, return a teardown function, and use event delegation where dynamic content is expected. Do not retrofit these hooks in the audit PR.

## I. Layout / overflow / position findings

| Severity | Pattern | Static assessment |
|---|---|---|
| HIGH | Sidebar: sticky, `height:100vh`, internal `overflow-y:auto` | Correct for desktop, but browser UI/dynamic viewport height can make `100vh` awkward; independent sidebar/page scroll is a scroll trap risk. No shell breakpoint was found. |
| HIGH | Fixed modal families with `overflow:hidden` outer grid and scrollable body | Footer separation is intentional, but every dialog family must keep the body as the only flexible row. A missing/changed wrapper can clip fields behind the footer. |
| HIGH | Provider-change modal/controller | Dense multi-step grids, fixed modal dimensions, internal scroll body, dynamic reveal/scroll, and footer tests make it the highest-risk layout. Do not alter without dedicated viewport checks. |
| MEDIUM | Generic tables: `width:max-content; min-width:100%`; cards hide overflow; inner `.table-scroll` scrolls | Intentional horizontal containment, but long persisted widths and sticky columns/headers can create large inner canvases and nested page/table scroll. |
| MEDIUM | HLR `min-height:520px; max-height:calc(100vh - 170px)` | At 768px, max is ~598px and min is 520px; with surrounding controls it can force page scrolling. At shorter heights min can exceed max behavior expectations. Preserve until real viewport tests. |
| MEDIUM | Column panels are absolute and may open upward | JS measures viewport, but containing overflow can clip panels depending on ancestor; HLR and generic panels have distinct rules. |
| MEDIUM | Cell popover fixed at z-index 1000 | It shares the modal z-index neighborhood and can overlap a modal if not closed before opening one. |
| MEDIUM | Admin user permissions has a 620px-min table inside a 300px-max-height scroller inside a fixed modal | Intentional, but creates nested two-axis scrolling on smaller viewports. |
| LOW | Sticky table headers | Appropriate inside scrolling table containers; z-index 1–3 is locally coherent. |
| LOW | `td {max-width:360px; overflow:hidden}` plus clamp/popover | Deliberate truncation with a disclosure path; ensure all truncated semantic values receive `data-full-text`. |

### Static viewport assessment

* **1920×1080:** content caps at 1460px; the 258px sidebar plus workspace comfortably fits. Modal maximum heights and table scroll budgets have room. Main risk is very wide `max-content` tables retaining stored pixel widths.
* **1600×900:** content has about 1,342px before workspace padding and caps; most auto-fit grids remain multi-column. 780px modal max plus 48px viewport margin fits narrowly; header/footer/body accounting matters.
* **1366×768:** approximately 1,108px remains after sidebar before workspace padding. The provider-change base grid has minimum columns totaling roughly 753px plus gaps, so it can fit, but dense page chrome and long localized labels reduce slack. A nominal 780px dialog is capped to 720px (`100vh - 48px`); HLR inner table is bounded near 598px and has a 520px minimum. Short-height testing is more important than width at this target.
* **Narrower than audited desktop widths:** component breakpoints at 1020/900/760/720 exist, but the shell itself remains two-column. This is the primary narrow-width risk.

Intentional modal scrolling is not classified as a bug. The risk is inconsistency among the several modal contracts and ancestor clipping.

## J. Component consistency findings

| Concept | Current variants | Candidate future shared contract |
|---|---|---|
| Buttons/actions | `.button`, bare `button`, `.hero-action`, `.action-button`, `.edit-action`, `.danger-action`, `.modal-save`, `.admin-edit-save`, per-page primary summaries/filter submits | Semantic variants (`primary`, `secondary`, `danger`, `icon`) independent of element and page. |
| Cancel | links styled as buttons on full pages; server-rendered `data-modal-close`; JS-generated `.modal-cancel`; JS-generated `.admin-edit-cancel` | One cancel action contract with navigation vs dismiss behavior explicit. |
| Filters | `filter_card()`, HLR custom panel/status chips, Spam Checker tabs/forms, page-specific grid widths | Keep HLR specialized; normalize normal pages around `filter_card()` and a stable action slot. |
| Tables | `data_table()` + helpers; history tables; HLR custom table; dictionary/admin layouts | Shared shell/scroll/footer semantics, with HLR adapter rather than replacing server rows. |
| Pagination | `paginate_rows()` generated links; summaries in differing table footers | One footer slot contract while preserving server URLs. |
| Modals | generic details modal; dedicated seven dialog families; remote fetched card; inline admin edit details; provider-change modal | One lifecycle and semantic shell, migrated entity-by-entity, not wholesale. |
| Forms | generic `.form-grid`; provider workflow; dedicated dialog grids; login/auth forms | Shared field/action/error primitives; page workflow remains page-owned. |
| Dropdowns | native select; `<details>` user menu; custom theme button/menu; custom multi-select details; column settings details | Separate semantic menu/listbox/disclosure contracts. |
| Status | `.status-badge`, `.badge`, `.dot-status`, HLR statuses, notices `.ok/.error`, connection banner | Shared tone vocabulary without forcing identical DOM. |
| Cards | `.card`, `.table-card`, `.journal-card`, `.dictionary-card`, metric/quick-link/activity cards | Base surface/layout contract plus explicit variants. |
| Empty state | `.empty-state`, route/provider empty messages, HLR table empty output, page-local messages | Shared empty-state markup with optional action. |
| Tooltips | native `title`, `data-tooltip`, aria-label, full-text cell popover | Separate accessible name from optional visual tooltip hook. |
| Sidebar items | `.side-link`, `.admin-link`, disabled buttons, special provider-change URL styling | One nav-item DOM with depth/state data rather than URL-specific visual selectors. |

## K. Accessibility and semantics

Only concrete source findings are listed:

1. **Remote edit modal:** generated `<section>` lacks dialog semantics, modal labelling, focus trap, initial focus policy, and trigger focus restoration. Escape support alone is incomplete. **HIGH.**
2. **Cell popover:** receives `role="dialog"` and focuses Copy, but does not trap focus or restore focus to the originating cell on close. The clickable table cell itself is not necessarily keyboard-operable as a button. **MEDIUM.**
3. **Column resize handle:** a `<span role="separator">` responds only to mouse `mousedown`; no tabindex, keyboard resizing, or ARIA value is supplied. **MEDIUM.**
4. **Visual DOM reordering:** column order is changed by moving actual cells, so screen-reader and visual order agree after JS. Before JS completes they can briefly differ; this is preferable to CSS-only reordering and not itself a defect.
5. **Theme custom menu:** it uses `role=menu/menuitemradio` and aria-expanded/checked, but no roving focus/arrow-key handler was found. Tab/click works; expected menu keyboard semantics are incomplete. **MEDIUM.**
6. **Details-as-modal:** native summary is keyboard operable, but opening a fixed “modal” does not itself establish modal semantics or constrain focus. **MEDIUM.**
7. **Icon-only edit conversion:** visible text is hidden with font-size/text-indent and a Material Symbols pseudo-element. Summary enhancement adds title/aria-label, but other `.edit-action` links must retain an accessible text/name through the conversion. **NEEDS RUNTIME VERIFICATION.**
8. **Hidden workflow content:** provider-change `sync()` sets `hidden` and disables descendant controls together, which prevents hidden controls from remaining focusable/submittable. This is a positive, concrete behavior.
9. **Disabled sidebar future items:** actual disabled buttons include `aria-disabled`, rather than clickable divs. This is semantically safe, though redundant.
10. **Form labels:** most controls are nested in `<label>`, so absence of `for` is not automatically a missing label. No broad missing-label claim is made.

No concrete same-document duplicate ID or nested form was proven. These remain runtime checks for modal-import combinations.

## L. Dead-code candidates and confidence model

Searches covered `app/server.py`, `tests/`, and other app files. “Dead” below means frontend rule/branch reachability, not that a historic storage value can never exist.

| Candidate | Confidence | Evidence | Action now |
|---|---|---|---|
| Duplicate first `--accent-soft:#eff6ff` inside `html[data-theme="mvp"]` | **CERTAIN** | Immediately repeated with the identical property/value in the same rule; no intervening declaration. | One confirmed dead declaration; leave for cleanup PR. |
| Dedicated CSS for `calm-blue` | **CERTAIN (absent, not dead code)** | Only occurrence repo-wide is the live alias map asserted by tests. | Nothing to delete. |
| Dedicated CSS for `cyber-sketch` | **CERTAIN (absent, not dead code)** | Only occurrence repo-wide is the live alias map asserted by tests. | Nothing to delete. |
| `.theme-selector select` and its theme-qualified focus rules | **HIGH CONFIDENCE** | Current helper emits button/menu, global JS uses data hooks, and tests assert that structure; no current generator for a select under this class was found. | Verify all rendered pages, then remove in a small CSS-only PR. |
| `.app-title` | **HIGH CONFIDENCE** | Definition found; sidebar emits `.brand-block` instead; no JS/test/app markup reference found. | Browser/source snapshot check before deletion. |
| Shell `mvp` visual rules | **MEDIUM CONFIDENCE** | Normalizer immediately maps stored `mvp` to `light-v2`, but `:root` shares its token block and no-script/script-failure/manual DOM scenarios complicate reachability. Tests pin alias behavior. | Do not remove until theme initialization is moved earlier and fallback behavior is specified. |
| Shell `terminal-paper` visual rules | **MEDIUM CONFIDENCE** | Normalizer maps it to `light-v2`; no selectable option or other app/test references. Still a historic pre-init/failure compatibility possibility. | Treat as later migration cleanup, not phase-zero deletion. |
| `obsolete-*` dashboard classes | **LOW CONFIDENCE as dead (likely live)** | Source contains helpers/markup/styles using these names. | Do not remove based on naming. |
| Empty `.page-crumbs` wrapper on titles without trails | **LOW CONFIDENCE** | Can be empty, but it is structural and CSS-addressed. | Runtime layout check; likely keep or make conditional only with snapshots. |
| Generic modal compatibility enhancer branches | **LOW CONFIDENCE** | Guards and test assertions show both legacy and dedicated footer paths. Conditional admin pages may depend on them. | Inventory rendered forms before any deletion. |

**Confirmed dead-code candidates:** one redundant CSS declaration (`--accent-soft` duplicate).
**Uncertain candidates requiring runtime verification:** `.theme-selector select`, `.app-title`, steady-state `mvp` CSS, `terminal-paper` CSS, empty breadcrumb wrapper, and generic modal compatibility branches (six candidate areas). The first two are high-confidence, but not promoted to CERTAIN without rendering every conditional page and dynamic path.

## M. Suggested cleanup phases

| Phase | Work | Expected risk | Approximate scope / PR size | Regression areas |
|---|---|---|---|---|
| 0 | Add representative rendered-HTML, browser, computed-style, keyboard, and screenshot baselines for both themes and 1920/1600/1366 viewports. | Low | 1–2 test-only PRs, 200–500 lines each | Shell, dashboard, normal table/filter, all modal families, HLR, provider changes, admin. |
| 1 | Remove only CERTAIN/HIGH-confidence stale declarations after coverage; preserve alias normalization. | Low | One CSS-only PR under ~100 changed lines | Auth pages, theme initialization/no-script behavior, selector snapshots. |
| 2 | Consolidate one component's duplicate base rules at a time, starting with sidebar active states and primary summary buttons. | Medium | 4–6 PRs, ~100–250 lines each | Active/hover/focus/disabled states in light-v2 and dark; collapsed sidebar. |
| 3 | Separate structural layout rules from color/token theme rules without changing selectors or rendered DOM. | Medium–High | Several PRs, 200–400 lines each | Cascade order, dialogs, tables, dashboard, all page wrappers. |
| 4 | Normalize modal contract entity-by-entity; begin with low-risk route/tariff/phone/company forms, leave provider changes and admin inline edit last. | High | One modal family per PR, 150–350 lines plus tests | Focus, Escape/overlay, validation replacement, submit recovery, short heights, full-page fallback. |
| 5 | Add stable `data-component/data-ui/data-action` hooks alongside current classes, migrate JS in bounded controllers, then retire behavior dependence on visual classes. | Medium–High | One controller/component per PR, 150–300 lines | Dynamic content, remote modal replacement, menus, copy, column settings. |
| 6 | Unify normal filter/table/footer/button primitives while preserving HLR's server-rendered special path. | Medium | 3–5 PRs | Filters/cookies, exports, pagination, column storage, history/admin tables. |
| 7 | Extract frozen CSS and JS from `server.py` into versioned static assets, preserving exact ordering and initialization first; split only after parity. | High | Initial mechanical PR then small module PRs | CSP/cache paths, deployment static serving, all inline page scripts, remote fetched scripts. |
| 8 | Define and validate an external-skin API over semantic tokens/component states; retain light-v2/dark fallback. | High | Design/RFC PR, then one implementation PR per layer | Both built-ins, missing skin, partial skin, persisted preferences, all components. |

Recommended sequence is coverage → verified deletion → component cascade consolidation → layout/theme separation → modal contracts → stable behavior hooks → shared primitives → extraction → skins. The original example's “remove legacy first” is unsafe here because legacy alias semantics are explicitly tested and the fallback token initialization is entangled with `mvp`.

## N. Do not touch yet

* HLR row rendering, API mapping, filter/export pipeline, custom column manager, and its server-rendered table.
* Provider Changes modal structure, scroll-body/footer boundary, serialized metadata, IDs, or controller sequencing.
* Theme alias map or `mvp-theme` key until a documented migration and pre-script fallback are tested.
* Remote modal script replay or fallback-to-full-page behavior without integration coverage.
* Admin inline edit forms and generated cancel/footer compatibility path.
* Table cell order/visibility/width persistence or storage keys.
* Filter-state cookie and hidden open-state synchronization.
* Any wrapper selected by CSS, JS, or tests—even if empty in one page.
* `!important` hidden-state rules and modal layout rules in isolation.
* Dark or light-v2 visual design; cleanup must prove equivalence rather than “improve” appearance.
* Runtime CDN dependencies as a side effect of CSS extraction; treat dependency policy separately.

## O. Recommended target frontend architecture before external skins

The target should remain server-rendered and operationally simple:

1. **Python view layer:** page functions provide business data and invoke small HTML component helpers; HLR continues to render rows on the server.
2. **Explicit DOM contract:** documented component roots and action hooks (`data-component`, `data-ui`, `data-action`) are stable; classes are presentation-only. IDs are reserved for labels/form relationships and truly unique page controllers.
3. **Layered CSS:** ordered layers for reset/tokens → shell/layout → components → page exceptions → theme tokens/state. Built-in themes primarily supply semantic tokens; justified theme-specific component rules are documented and parity-tested.
4. **Component-scoped controllers:** initialize from a root, use delegated events for dynamic children, expose teardown/reinitialize for fetched modal content, and avoid global selectors based on text/URL/class appearance.
5. **One modal lifecycle:** consistent server markup, dialog semantics, initial focus/trap/restore, Escape/overlay behavior, scroll lock, error slot, body scroller, footer, and full-page fallback.
6. **Table contract:** shared card/scroll/footer/column metadata; adapters allow HLR specialization without client-side row hydration.
7. **Preference registry:** versioned keys and schemas for theme/sidebar/table/HLR state, with normalization and invalid-state recovery centrally documented.
8. **Static assets only after parity:** extract the existing blocks mechanically with order preserved, then modularize. Page-local data can remain inline as inert JSON/data attributes rather than executable scripts where safe.
9. **Skin boundary:** external skins may set approved semantic tokens and component-state variables, but may not alter required DOM, visibility, positioning contracts, or behavior hooks. Missing/incomplete skins fall back to light-v2/dark tokens.
10. **Regression gate:** render snapshots plus browser checks for both themes, three target desktop sizes, short height, keyboard modal/menu flows, horizontal tables, HLR, and provider changes.

## Audit evidence commands

The following read-only command families produced the inventories used above:

```text
wc -lc app/server.py
rg -n '^def ...|<style|<script|light-v2|dark|mvp|terminal-paper|calm-blue|cyber-sketch' app/server.py tests
rg -n '@media|overflow|position|z-index|max-height|min-width|grid-template-columns' app/server.py
rg -n 'modal|createElement|appendChild|insertBefore|querySelector|closest|addEventListener|localStorage' app/server.py tests
rg -n 'data-modal|data-theme|sidebar-collapsed|hlr-table|routing-event-form' tests app
```

Counts were checked with a small read-only Python script over `app/server.py`. This was a static audit: no claim is made that browser behavior was manually verified. The six explicitly uncertain areas above should be resolved by Phase 0 runtime coverage.
