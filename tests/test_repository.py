import sqlite3
import unittest

from app.db import init_db
from app.repository import BusinessRuleError, Repository


class RepositoryBusinessRulesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        self.repo = Repository(self.conn)
        self.admin_id = self.repo.create_user("admin", "Admin")
        self.country_id = self.repo.create_country("Италия", "IT")
        self.currency_id = self.repo.create_currency("EUR", "Euro", "€")
        self.provider_id = self.repo.create_provider("Miatel", "voip", self.currency_id)
        self.route_id = self.repo.create_route(
            country_id=self.country_id,
            provider_id=self.provider_id,
            name="Италия/Miatel/Pool_A@",
            cli_source_type="pool",
            cli_source_label="Pool_A",
            created_by=self.admin_id,
        )

    def tearDown(self):
        self.conn.close()

    def create_phone(self, status="used", is_active=True, number="393331234567"):
        return self.repo.create_phone_number(
            country_id=self.country_id,
            provider_id=self.provider_id,
            number=number,
            assignment_type="pool_number",
            status=status,
            created_by=self.admin_id,
            currency_id=self.currency_id,
            is_active=is_active,
        )

    def test_valid_phone_can_be_added_to_route(self):
        phone_id = self.create_phone()
        result = self.repo.add_phone_to_route(
            route_id=self.route_id,
            phone_number_id=phone_id,
            usage_type="pool_member",
            added_by=self.admin_id,
        )
        self.assertGreater(result.route_phone_number_id, 0)

    def test_disabled_phone_cannot_be_added_to_route(self):
        phone_id = self.create_phone(status="disabled")
        with self.assertRaisesRegex(BusinessRuleError, "Disabled or blocked"):
            self.repo.add_phone_to_route(
                route_id=self.route_id,
                phone_number_id=phone_id,
                usage_type="pool_member",
                added_by=self.admin_id,
            )

    def test_blocked_phone_cannot_be_added_to_route(self):
        phone_id = self.create_phone(status="blocked")
        with self.assertRaisesRegex(BusinessRuleError, "Disabled or blocked"):
            self.repo.add_phone_to_route(
                route_id=self.route_id,
                phone_number_id=phone_id,
                usage_type="pool_member",
                added_by=self.admin_id,
            )

    def test_inactive_phone_cannot_be_added_to_route(self):
        phone_id = self.create_phone(is_active=False)
        with self.assertRaisesRegex(BusinessRuleError, "Inactive"):
            self.repo.add_phone_to_route(
                route_id=self.route_id,
                phone_number_id=phone_id,
                usage_type="pool_member",
                added_by=self.admin_id,
            )

    def test_phone_number_must_use_strict_international_format(self):
        invalid_numbers = ["+393331234567", "00393331234567", "393 331 234567", "(393)331234567"]
        for invalid in invalid_numbers:
            with self.subTest(invalid=invalid):
                with self.assertRaises(BusinessRuleError):
                    self.create_phone(number=invalid)

    def test_calling_company_requires_external_id_and_unique_business_key(self):
        server_id = self.repo.create_server("EU1")
        with self.assertRaises(BusinessRuleError):
            self.repo.create_calling_company(
                server_id=server_id,
                country_id=self.country_id,
                company_name="CC Italy",
                company_id_external=" ",
                has_autorotation=False,
                created_by=self.admin_id,
            )
        self.repo.create_calling_company(
            server_id=server_id,
            country_id=self.country_id,
            company_name="CC Italy",
            company_id_external="123",
            has_autorotation=True,
            created_by=self.admin_id,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.create_calling_company(
                server_id=server_id,
                country_id=self.country_id,
                company_name="CC Italy Duplicate",
                company_id_external="123",
                has_autorotation=False,
                created_by=self.admin_id,
            )

    def test_route_unique_by_country_and_name(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.create_route(
                country_id=self.country_id,
                provider_id=self.provider_id,
                name="Италия/Miatel/Pool_A@",
                cli_source_type="pool",
                cli_source_label="Pool_A",
                created_by=self.admin_id,
            )


if __name__ == "__main__":
    unittest.main()
