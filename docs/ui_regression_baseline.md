# Phase 0 UI regression baseline

This baseline freezes the current server-rendered frontend contracts before cleanup. It protects structure, behavior hooks, reachability, and overflow ownership—not exact colors, shadows, radii, typography, or cosmetic spacing.

## Automated scope

`tests/test_ui_baseline.py` renders Dashboard, Routes, Tariffs, Purchased phones, Calling campaigns, Provider Changes, HLR, and Admin. It covers the shared shell and theme menu, normal table/filter/footer and column settings, a standard create modal, the Provider Changes create workflow, server-rendered HLR rows, accessibility semantics already present, critical CSS structure, and same-document duplicate IDs.

The duplicate-ID helper parses each rendered document independently. An ID repeated on two different pages is therefore valid; only repetition within one document fails.

## Browser automation status

**Browser baseline not automated yet.** The repository has no existing Playwright, Selenium, or equivalent browser stack, so Phase 0 does not add a large dependency. The assertions do not claim real viewport behavior or browser interaction coverage.

## Manual desktop viewport checklist

Run this checklist at **1920×1080**, **1600×900**, and **1366×768**, in both **Light v2** and **Dark**.

Pages: Dashboard, Routes, Tariffs, Phones, Campaigns, Provider Changes, HLR, and Admin.

For every page and viewport:

- [ ] There is no page-level horizontal drift or unintended document scrollbar.
- [ ] The sidebar is usable and its collapse/expand action works.
- [ ] The topbar and theme menu are usable; switching Light v2 ↔ Dark works.
- [ ] Tables, filters, and column controls are reachable where present.
- [ ] Standard modal content is reachable; Escape and Cancel close it.
- [ ] Modal footer and submit/cancel actions remain reachable.
- [ ] Light v2 is readable.
- [ ] Dark is readable.
- [ ] No controls are clipped.
- [ ] No browser console errors appear during these basic flows.

### Provider Changes additional flow

- [ ] Open **Add event** and verify the scope/header area remains fixed.
- [ ] Select campaign scope and verify campaign → route step progression.
- [ ] Verify provider selection controls the available route choices.
- [ ] Verify route selection reveals the reason/comment step.
- [ ] Verify hidden workflow fields cannot be submitted while hidden.
- [ ] Verify the middle modal body scrolls without creating document-level drift.
- [ ] Verify the footer stays visible and its actions remain reachable.

### HLR caution

- [ ] Confirm result rows are present in the initial server-rendered HTML.
- [ ] Confirm filters, column manager, and CSV export remain reachable.
- [ ] Do not treat this baseline as permission to replace the HLR table with frontend-rendered state.
