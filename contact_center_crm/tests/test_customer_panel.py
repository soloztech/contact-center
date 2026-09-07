import uuid
from unittest import SkipTest
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged

from .test_conversation_crm import ConversationCrmCase


class TestCustomerPanel(ConversationCrmCase):
    def test_same_customer_in_another_conversation_has_the_same_records(self):
        other_channel = self._channel(self.account, self.person)
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        self.env.flush_all()
        with patch.object(
            type(self.api), "_crm_links", side_effect=AssertionError("Ledger queried")
        ):
            first = self.api.get_customer_records(self.channel.id)
            second = self.api.get_customer_records(other_channel.id)
        self.assertEqual(first["items"], second["items"])
        self.assertNotEqual(first["channel_id"], second["channel_id"])

    def test_an_old_link_to_a_different_customer_does_not_leak_into_panel(self):
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        unrelated = self.env["res.partner"].create({"name": "Different customer"})
        self.lead.partner_id = unrelated
        self.lead.flush_recordset()
        result = self.api.get_customer_records(self.channel.id)
        self.assertNotIn(self.lead.id, [item["id"] for item in result["items"]])
        self.assertTrue(
            self.env["contact.center.crm.conversation.link"].search_count(
                [("channel_id", "=", self.channel.id), ("lead_id", "=", self.lead.id)]
            )
        )

    def test_company_scope_still_applies_with_both_companies_enabled(self):
        company = self.env["res.company"].create({"name": "Other panel company"})
        self.agent.company_ids |= company
        foreign = self._lead(
            "Other company document", self.person, company_id=company.id, user_id=False
        )
        self.env.flush_all()
        result = self.api.with_context(
            allowed_company_ids=[self.env.company.id, company.id]
        ).get_customer_records(self.channel.id)
        self.assertNotIn(foreign.id, [item["id"] for item in result["items"]])
        self.assertIn(self.lead.id, [item["id"] for item in result["items"]])

    def test_missing_sales_permission_returns_unavailable_without_records(self):
        channel = self._customer_channel_for(self.non_sales)
        result = self.api.with_user(self.non_sales).get_customer_records(channel.id)
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["items"])
        self.assertFalse(result["has_more"])
        self.assertIn({"id": "opportunities", "available": False}, result["tabs"])

    def test_missing_optional_models_make_only_their_tabs_unavailable(self):
        with patch.dict(self.env.registry.models):
            self.env.registry.models.pop("sale.order", None)
            self.env.registry.models.pop("account.move", None)
            for tab in ("quotations", "orders", "invoices"):
                result = self.api.get_customer_records(self.channel.id, tab=tab)
                self.assertEqual(result["status"], "unavailable")
                self.assertFalse(result["items"])
                self.assertIn(
                    {"id": "opportunities", "available": True}, result["tabs"]
                )

    def test_leads_and_opportunities_have_type_stage_and_currency(self):
        lead = self._lead("Customer lead", self.person, type="lead")
        self.env.flush_all()
        result = self.api.get_customer_records(self.channel.id)
        items = {item["id"]: item for item in result["items"]}
        self.assertEqual(items[lead.id]["type"], "lead")
        self.assertEqual(items[self.lead.id]["type"], "opportunity")
        self.assertTrue(items[lead.id]["type_label"])
        self.assertTrue(items[self.lead.id]["stage"])
        self.assertEqual(items[self.lead.id]["model"], "crm.lead")
        self.assertEqual(
            items[self.lead.id]["currency"]["decimal_places"],
            self.env.company.currency_id.decimal_places,
        )
        self.assertRegex(items[lead.id]["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertNotIn("linked", items[lead.id])

    def test_invalid_tab_and_search_cannot_select_arbitrary_models(self):
        for tab in ("res.users", "", ["opportunities"]):
            with self.subTest(tab=tab), self.assertRaises(ValidationError):
                self.api.get_customer_records(self.channel.id, tab=tab)
        with self.assertRaises(ValidationError):
            self.api.get_customer_records(self.channel.id, query=["name"])


@tagged("post_install", "-at_install")
class TestCustomerPanelDocuments(ConversationCrmCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if (
            "sale.order" not in cls.env.registry
            or "account.move" not in cls.env.registry
        ):
            raise SkipTest("Customer documents require optional native sale/account")
        cls.billing = cls._user(
            "Billing support",
            cls.env.ref("contact_center_base.group_contact_center_agent")
            | cls.env.ref("account.group_account_invoice"),
        )
        cls.billing_channel = cls._customer_channel_for(cls.billing)
        cls.journal = cls.env["account.journal"].create(
            {"name": "Panel sales", "code": uuid.uuid4().hex[:5], "type": "sale"}
        )
        cls.income = cls.env["account.account"].create(
            {
                "name": "Panel income",
                "code": uuid.uuid4().hex[:8],
                "account_type": "income",
            }
        )
        cls.receivable = cls.env["account.account"].create(
            {
                "name": "Panel receivable",
                "code": uuid.uuid4().hex[:8],
                "account_type": "asset_receivable",
                "reconcile": True,
            }
        )
        cls.customer.property_account_receivable_id = cls.receivable
        cls.pricelist = cls.env["product.pricelist"].create(
            {"name": "Panel prices", "currency_id": cls.env.company.currency_id.id}
        )
        cls.product = cls.env["product.product"].create(
            {"name": "Panel fixture service", "type": "service", "list_price": 125}
        )
        cls.env.flush_all()

    def _sale(self, partner, state="draft", **values):
        return self.env["sale.order"].create(
            {
                "partner_id": partner.id,
                "company_id": self.env.company.id,
                "user_id": self.agent.id,
                "pricelist_id": self.pricelist.id,
                "state": state,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "name": self.product.name,
                            "product_uom_qty": 1,
                            "product_uom": self.product.uom_id.id,
                            "price_unit": 125,
                            "tax_id": [(5, 0, 0)],
                        },
                    )
                ],
                **values,
            }
        )

    def _invoice(self, partner, move_type="out_invoice", **values):
        return self.env["account.move"].create(
            {
                "partner_id": partner.id,
                "company_id": self.env.company.id,
                "journal_id": self.journal.id,
                "move_type": move_type,
                "invoice_date": fields.Date.today(),
                "invoice_line_ids": [
                    (
                        0,
                        0,
                        {
                            "name": "Panel fixture service",
                            "account_id": self.income.id,
                            "quantity": 1,
                            "price_unit": 125,
                            "tax_ids": [(5, 0, 0)],
                        },
                    )
                ],
                **values,
            }
        )

    def test_quotations_and_orders_are_split_by_native_state_and_customer(self):
        draft = self._sale(self.person)
        sent = self._sale(self.customer, state="sent")
        order = self._sale(self.sibling, state="sale")
        locked = self._sale(self.customer, state="done")
        self._sale(self.person, state="cancel")
        self._sale(self.env["res.partner"].create({"name": "Other buyer"}))
        self.env.flush_all()
        quotations = self.api.get_customer_records(self.channel.id, tab="quotations")
        orders = self.api.get_customer_records(self.channel.id, tab="orders")
        self.assertEqual(
            {item["id"] for item in quotations["items"]}, {draft.id, sent.id}
        )
        self.assertEqual(
            {item["id"] for item in orders["items"]}, {order.id, locked.id}
        )
        self.assertEqual(orders["items"][0]["model"], "sale.order")
        self.assertEqual(orders["items"][0]["amount"], 125)
        self.assertEqual(orders["items"][0]["state"]["key"], "done")

    def test_sale_search_pagination_and_native_record_rules(self):
        first = self._sale(self.person, name="Panel Quote A")
        second = self._sale(self.customer, name="Panel Quote B")
        self._sale(self.person, name="Panel Quote Hidden", user_id=self.other.id)
        self.env.flush_all()
        page1 = self.api.get_customer_records(
            self.channel.id, tab="quotations", query="Panel Quote", limit=1
        )
        page2 = self.api.get_customer_records(
            self.channel.id, tab="quotations", query="Panel Quote", offset=1, limit=1
        )
        self.assertEqual(
            [page1["items"][0]["id"], page2["items"][0]["id"]], [second.id, first.id]
        )
        self.assertTrue(page1["has_more"])
        self.assertFalse(page2["has_more"])

    def test_invoice_only_user_can_open_panel_without_crm_permission(self):
        invoice = self._invoice(self.person)
        self.env.flush_all()
        api = self.api.with_user(self.billing)
        self.assertFalse(
            self.env["crm.lead"]
            .with_user(self.billing)
            .check_access_rights("read", raise_exception=False)
        )
        self.assertTrue(api.bootstrap()["capabilities"]["view_crm"])
        result = api.get_customer_records(self.billing_channel.id, tab="invoices")
        self.assertEqual([item["id"] for item in result["items"]], [invoice.id])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(
            api.get_customer_records(self.billing_channel.id)["status"], "unavailable"
        )

    def test_invoices_show_native_status_credit_sign_and_draft_title(self):
        invoice = self._invoice(self.person)
        credit = self._invoice(self.customer, move_type="out_refund")
        cancelled = self._invoice(self.sibling)
        cancelled.button_cancel()
        self.env.flush_all()
        result = self.api.with_user(self.billing).get_customer_records(
            self.billing_channel.id, tab="invoices"
        )
        items = {item["id"]: item for item in result["items"]}
        self.assertEqual(set(items), {invoice.id, credit.id})
        self.assertEqual(items[invoice.id]["amount"], 125)
        self.assertEqual(items[credit.id]["amount"], -125)
        self.assertEqual(items[credit.id]["type"], "out_refund")
        self.assertTrue(items[credit.id]["type_label"])
        self.assertEqual(items[invoice.id]["state"]["key"], "draft")
        self.assertTrue(items[invoice.id]["payment_state"]["label"])
        self.assertNotIn(items[invoice.id]["name"], (False, "", "/"))

    def test_invoice_native_rules_and_missing_acl_are_respected(self):
        visible = self._invoice(self.customer)
        hidden = self._invoice(self.person)
        self.env["ir.rule"].create(
            {
                "name": "Panel invoice restriction",
                "model_id": self.env["ir.model"]._get_id("account.move"),
                "domain_force": "[('id', '!=', %s)]" % hidden.id,
                "perm_write": False,
                "perm_create": False,
                "perm_unlink": False,
            }
        )
        self.env.flush_all()
        result = self.api.with_user(self.billing).get_customer_records(
            self.billing_channel.id, tab="invoices"
        )
        self.assertEqual([item["id"] for item in result["items"]], [visible.id])
        channel = self._customer_channel_for(self.non_sales)
        result = self.api.with_user(self.non_sales).get_customer_records(
            channel.id, tab="invoices"
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["items"])
        with self.assertRaises(AccessError):
            self.api.with_user(self.other).get_customer_records(
                self.channel.id, tab="invoices"
            )

    def test_vendor_bills_and_credit_notes_are_excluded_for_the_same_partner(self):
        invoice = self._invoice(self.person)
        journal = self.env["account.journal"].create(
            {
                "name": "Panel purchases",
                "code": uuid.uuid4().hex[:5],
                "type": "purchase",
            }
        )
        vendor_documents = self.env["account.move"].create(
            [
                {
                    "partner_id": self.person.id,
                    "company_id": self.env.company.id,
                    "journal_id": journal.id,
                    "move_type": move_type,
                    "invoice_date": fields.Date.today(),
                }
                for move_type in ("in_invoice", "in_refund")
            ]
        )
        self.env.flush_all()
        self.assertEqual(
            set(
                self.env["account.move"]
                .with_user(self.billing)
                .search([("id", "in", vendor_documents.ids)])
                .ids
            ),
            set(vendor_documents.ids),
        )
        result = self.api.with_user(self.billing).get_customer_records(
            self.billing_channel.id, tab="invoices"
        )
        self.assertEqual([item["id"] for item in result["items"]], [invoice.id])

    def test_documents_stay_in_channel_company_with_both_companies_enabled(self):
        quotation = self._sale(self.person)
        invoice = self._invoice(self.person)
        company = self.env["res.company"].create({"name": "Other document company"})
        (self.agent | self.billing).write({"company_ids": [(4, company.id)]})
        journal = (
            self.env["account.journal"]
            .with_company(company)
            .create(
                {
                    "name": "Other company sales",
                    "code": uuid.uuid4().hex[:5],
                    "type": "sale",
                    "company_id": company.id,
                }
            )
        )
        foreign_quotation = (
            self.env["sale.order"]
            .with_company(company)
            .create(
                {
                    "partner_id": self.person.id,
                    "company_id": company.id,
                    "user_id": self.agent.id,
                    "pricelist_id": self.pricelist.id,
                }
            )
        )
        foreign_invoice = (
            self.env["account.move"]
            .with_company(company)
            .create(
                {
                    "partner_id": self.person.id,
                    "company_id": company.id,
                    "journal_id": journal.id,
                    "move_type": "out_invoice",
                    "invoice_date": fields.Date.today(),
                }
            )
        )
        self.env.flush_all()
        companies = [self.env.company.id, company.id]
        sales_api = self.api.with_context(allowed_company_ids=companies)
        billing_api = self.api.with_user(self.billing).with_context(
            allowed_company_ids=companies
        )
        # Prove native rules permit the foreign documents: exclusion must come
        # from the panel's conversation-company scope, not missing user access.
        self.assertEqual(
            sales_api.env["sale.order"].search([("id", "=", foreign_quotation.id)]),
            foreign_quotation,
        )
        self.assertEqual(
            billing_api.env["account.move"].search([("id", "=", foreign_invoice.id)]),
            foreign_invoice,
        )
        quotations = sales_api.get_customer_records(self.channel.id, tab="quotations")
        invoices = billing_api.get_customer_records(
            self.billing_channel.id, tab="invoices"
        )
        self.assertEqual([item["id"] for item in quotations["items"]], [quotation.id])
        self.assertEqual([item["id"] for item in invoices["items"]], [invoice.id])
