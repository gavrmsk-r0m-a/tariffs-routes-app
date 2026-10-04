"""Phase 0 contracts for the server-rendered UI.

These tests intentionally protect structure and JavaScript hooks, not visual styling.
They are a migration safety net: changing a contract should be a deliberate decision.
"""

import os
import re
import unittest
from collections import Counter
from html.parser import HTMLParser
from unittest.mock import MagicMock, patch

os.environ.setdefault("DB_BACKEND", "postgres")
os.environ.setdefault("DATABASE_URL", "postgresql://ui-baseline:ui-baseline@localhost/ui-baseline")

import app.server as server


class _DocumentInventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.elements = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if attributes.get("id"):
            self.ids.append(attributes["id"])


def document_inventory(document):
    """Parse one document; IDs from separately rendered pages never interact."""
    inventory = _DocumentInventory()
    inventory.feed(document)
    return inventory


def duplicate_ids(document):
    counts = Counter(document_inventory(document).ids)
    return sorted(element_id for element_id, count in counts.items() if count > 1)


def assert_no_duplicate_ids(test_case, document, page_name):
    test_case.assertEqual([], duplicate_ids(document), f"duplicate IDs in {page_name}")


class _EmptyCursor:
    def __iter__(self):
        return iter(())

    def fetchall(self):
        return []

    def fetchone(self):
        return None


def _repo():
    repo = MagicMock()
    repo.backend = "postgres"
    repo.conn.execute.return_value = _EmptyCursor()
    repo.dashboard_summary.return_value = {
        "provider_change_series": [], "active_routes": 0, "active_companies": 0,
        "active_phones": 0, "attention_phones": 0, "missing_working_routes": 0,
        "review_phones": 0, "problematic_phones": 0, "manual_campaigns": 0,
    }
    for method in (
        "list_routes", "list_tariffs", "list_phone_numbers", "list_calling_companies",
        "list_provider_changes", "list_servers", "list_change_reasons", "list_countries",
        "list_providers",
    ):
        getattr(repo, method).return_value = []
    return repo


def _render_pages():
    repo = _repo()
    with patch.dict(server._REQUEST_CONTEXT, {"current_role_key": "admin", "path": "/"}, clear=True):
        return {
            "Dashboard": server.dashboard_page(repo).decode(),
            "Routes": server.routes_page(repo).decode(),
            "Tariffs": server.tariffs_page(repo).decode(),
            "Purchased phones": server.phones_page(repo).decode(),
            "Calling campaigns": server.companies_page(repo).decode(),
            "Provider Changes": server.provider_changes_page(repo).decode(),
            "HLR": server.hlr_page().decode(),
            "Admin": server.admin_page(repo).decode(),
        }


class UiServerRenderedBaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pages = _render_pages()

    def test_representative_pages_keep_shell_and_theme_menu(self):
        for name, document in self.pages.items():
            with self.subTest(page=name):
                for contract in ("class=\"app-shell\"", "class=\"sidebar\"", "class=\"workspace\"",
                                 "class=\"page-top\"", "class=\"page-crumbs\"", "data-theme-selector",
                                 "data-theme-menu-toggle", 'role="menu"'):
                    self.assertIn(contract, document)
                self.assertRegex(document, r"data-theme-option=['\"]light-v2['\"]")
                self.assertRegex(document, r"data-theme-option=['\"]dark['\"]")
                self.assertNotRegex(document, r"data-theme-option=['\"](?:mvp|calm-blue|terminal-paper|cyber-sketch)['\"]")

    def test_theme_aliases_are_inputs_and_light_v2_is_fallback(self):
        document = self.pages["Dashboard"]
        self.assertIn('"mvp": "light-v2"', document)
        self.assertIn('"calm-blue": "light-v2"', document)
        self.assertIn('"terminal-paper": "light-v2"', document)
        self.assertIn('"cyber-sketch": "dark"', document)
        self.assertIn('themeLabels[theme] ? theme : "light-v2"', document)
        self.assertIn('localStorage.getItem("mvp-theme") || "light-v2"', document)

    def test_standard_table_filter_footer_and_column_contract(self):
        document = self.pages["Routes"]
        for contract in ("class='filter-card'", "class='filter-summary'", "class='table-card'",
                         "class='table-scroll'", "class='table-footer table-status-action-bar'",
                         "data-table-key='routes'", "data-column-settings='routes'",
                         "data-column-settings-list", "data-col-toggle=", "data-column-move="):
            self.assertIn(contract, document)
        self.assertRegex(document, r"<th data-col='geo'")
        self.assertRegex(document, r"<th data-col='actions'")
        self.assertEqual(len(re.findall(r"data-filters-open-field", document)), 2)  # generated control + filter script

    def test_standard_modal_keeps_body_footer_and_actions(self):
        document = self.pages["Routes"]
        self.assertIn("data-modal-details", document)
        self.assertIn("class=\"route-dialog route-dialog-form\"", document)
        self.assertIn("class=\"route-dialog-body\"", document)
        self.assertIn("class=\"route-dialog-footer\"", document)
        self.assertRegex(document, r"<button type=\"submit\" class=\"modal-save\">Сохранить")
        self.assertRegex(document, r"<button type=\"button\" class=\"modal-cancel\" data-modal-close>Отмена")

    def test_provider_changes_create_workflow_structure_is_frozen(self):
        document = self.pages["Provider Changes"]
        form_start = document.index("id='provider-change-create-form'")
        body = document.index("class='provider-change-scroll-body'", form_start)
        footer = document.index("class='modal-actions provider-change-create-actions'", body)
        body_end = document.index("</div>\n</form>", footer)
        self.assertLess(form_start, body)
        self.assertLess(body, footer)
        self.assertLess(footer, body_end)
        for contract in ("provider-change-shell-scope", "id='event-company'", "id='campaign-selection-step'",
                         "id='campaign-route-step'", "data-campaign-route-field='1'",
                         "id='campaign-provider'", "id='company-route'", "id='campaign-reason-step'",
                         "id='campaign-routing-reason'", "id='campaign-routing-comment'"):
            self.assertIn(contract, document)
        self.assertRegex(document, r"id='campaign-route-step'[^>]*hidden")
        for control in ("campaign-provider", "company-route", "campaign-routing-reason", "campaign-routing-comment"):
            self.assertRegex(document, rf"id='{control}'[^>]*disabled")
        self.assertIn("data-modal-close>Отмена", document)
        self.assertIn("type='submit' id='provider-change-submit' disabled>Создать событие", document)

    def test_provider_changes_campaign_picker_contract(self):
        document = self.pages["Provider Changes"]
        with open(server.__file__, encoding="utf-8") as source_file:
            source = source_file.read()
        self.assertNotIn("campaign-picker-meta", source)
        self.assertIn("<span class='campaign-picker-main' title='{esc(label)}'>{esc(label)}</span>", source)
        for attribute in ("data-server-id", "data-country-id", "data-campaign-id",
                          "data-server-name", "data-campaign-name"):
            self.assertIn(attribute, source)
        self.assertIn("height: 250px; min-height: 0; max-height: 250px;", source)
        self.assertIn("overflow-y: auto; overflow-x: hidden;", source)
        self.assertIn("id='campaign-picker-toggle' aria-expanded='true' aria-controls='campaign-picker-panel'", document)
        self.assertIn("id='campaign-selection-summary' aria-live='polite'", document)
        self.assertIn("id='campaign-picker-panel'", document)
        self.assertIn("setCampaignPickerExpanded", document)
        self.assertIn("String(option.dataset.serverId) === String(selectedServerId)", document)
        self.assertIn("String(option.dataset.countryId) === String(selectedCountryId)", document)
        self.assertIn("String(option.dataset.campaignId || '').toLocaleLowerCase().includes(query)", document)
        self.assertIn("String(option.dataset.campaignName || '').toLocaleLowerCase().includes(query)", document)

    def test_hlr_results_remain_server_rendered_and_hooked(self):
        result = {key: "—" for key, _, _ in server.HLR_TABLE_COLUMNS}
        result.update({"original_number": "+15551234567", "normalized_number": "15551234567", "hlr_status_raw": "LIVE"})
        with patch.dict(server._REQUEST_CONTEXT, {"current_role_key": "admin"}, clear=True):
            document = server.hlr_page(results=[result]).decode()
        self.assertIn("id='hlr-table'", document)
        self.assertRegex(document, r"class='hlr-result-row\s+hlr-row-severity-")
        self.assertRegex(document, r"class='hlr-result-row[^']*'[^>]*data-hlr-status='")
        self.assertIn("+15551234567", document)
        self.assertLess(document.index("class='hlr-result-row "), document.index("document.addEventListener"))
        for hook in ("hlr-form", "hlr-filter-panel", "hlr-columns-button", "hlr-column-panel", "hlr-column-list"):
            self.assertIn(f"id='{hook}'", document)

    def test_frontend_js_hook_inventory(self):
        shell = self.pages["Dashboard"]
        for hook in ("data-theme-selector", "data-theme-menu-toggle", "data-theme-option",
                     "data-sidebar-toggle", "data-modal-close", "data-modal-details", "data-col",
                     "data-column-settings", "data-col-toggle", "data-column-move", "data-column-reset"):
            self.assertIn(hook, shell)
        provider = self.pages["Provider Changes"]
        for hook in ("provider-change-create-form", "data-scope-content", "data-campaign-step",
                     "data-campaign-route-field", "data-scope-hint"):
            self.assertIn(hook, provider)

    def test_existing_accessibility_structure(self):
        inventory = document_inventory(self.pages["Dashboard"])
        theme_toggle = [attrs for tag, attrs in inventory.elements if "data-theme-menu-toggle" in attrs][0]
        self.assertEqual("button", next(tag for tag, attrs in inventory.elements if attrs is theme_toggle))
        self.assertEqual("menu", theme_toggle["aria-haspopup"])
        self.assertEqual("false", theme_toggle["aria-expanded"])
        options = [(tag, attrs) for tag, attrs in inventory.elements if "data-theme-option" in attrs]
        self.assertTrue(all(tag == "button" and attrs.get("role") == "menuitemradio" for tag, attrs in options))
        disabled_nav = [(tag, attrs) for tag, attrs in inventory.elements if "side-link-disabled" in attrs.get("class", "")]
        self.assertTrue(all(tag == "button" and "disabled" in attrs and attrs.get("aria-disabled") == "true" for tag, attrs in disabled_nav))
        # Current forms use nested labels; preserve that concrete association rather than inventing new IDs.
        self.assertRegex(self.pages["Routes"], r"<label>ГЕО\s*<select name=\"country_id\">")

    def test_no_duplicate_ids_within_each_rendered_document(self):
        for name, document in self.pages.items():
            with self.subTest(page=name):
                assert_no_duplicate_ids(self, document, name)


class UiCssArchitectureBaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(server.__file__, encoding="utf-8") as source_file:
            cls.source = source_file.read()

    def test_critical_layout_and_overflow_ownership(self):
        for contract in (
            ".app-shell {{ display: grid; grid-template-columns: 258px minmax(0, 1fr);",
            "position: sticky; top: 0; height: 100vh; overflow-y: auto;",
            ".table-scroll {{ overflow-x: auto; overscroll-behavior-x: contain; }}",
            ".table-scroll {{ max-height: calc(100vh - 270px); overflow: auto; position: relative; }}",
            ".provider-change-create-shell .provider-change-scroll-body {{ grid-row: 2; align-self: stretch; min-height: 0;",
            "overflow-y: auto; overflow-x: hidden;",
            "grid-template-rows: auto minmax(0, 1fr);",
            "max-height: calc(100vh - 48px); overflow: hidden;",
            ".modal-actions {{ grid-column: 1 / -1; display: flex;",
        ):
            self.assertIn(contract, self.source)

    def test_theme_roots_and_responsive_boundaries_remain_present(self):
        self.assertIn('html[data-theme="light-v2"] {{', self.source)
        self.assertIn('html[data-theme="dark"] {{', self.source)
        for breakpoint in ("@media (max-width: 1020px)", "@media (max-width: 900px)", "@media (max-width: 720px)"):
            self.assertIn(breakpoint, self.source)


if __name__ == "__main__":
    unittest.main()
