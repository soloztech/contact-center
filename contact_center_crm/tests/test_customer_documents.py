import hashlib
from unittest import SkipTest
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import Form, HttpCase, tagged

from .test_conversation_crm import ConversationCrmCase

PDF_CONTENT = b"%PDF-1.4\nCustomer document fixture\n%%EOF"


class CustomerSaleDocumentsCase(ConversationCrmCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if "sale.order" not in cls.env.registry:
            raise SkipTest("Sales documents require optional native sale")
        cls.pricelist = cls.env["product.pricelist"].create(
            {
                "name": "Customer document prices",
                "currency_id": cls.env.company.currency_id.id,
            }
        )
        cls.sale = cls._sale_document(cls.person)
        cls.original = cls._attachment(cls.sale)
        cls.env.flush_all()

    @classmethod
    def _sale_document(cls, partner, **values):
        return cls.env["sale.order"].create(
            {
                "partner_id": partner.id,
                "company_id": cls.env.company.id,
                "user_id": cls.agent.id,
                "pricelist_id": cls.pricelist.id,
                **values,
            }
        )

    @classmethod
    def _attachment(cls, order, **values):
        return cls.env["ir.attachment"].create(
            {
                "name": "Customer document.pdf",
                "res_model": "sale.order",
                "res_id": order.id,
                "type": "binary",
                "raw": PDF_CONTENT,
                "mimetype": "application/pdf",
                **values,
            }
        )


@tagged("post_install", "-at_install")
class TestCustomerSaleDocuments(CustomerSaleDocumentsCase):
    def test_document_tools_and_quotation_action_use_native_customer_defaults(self):
        result = self.api.get_customer_records(self.channel.id, tab="quotations")
        item = next(item for item in result["items"] if item["id"] == self.sale.id)
        self.assertEqual(
            item["document_tools"],
            {
                "pdf_url": "/contact_center/customer/%s/sale/%s/pdf"
                % (self.channel.id, self.sale.id),
                "attachments": True,
            },
        )
        self.assertTrue(result["can_create_quotation"])
        self.assertTrue(
            self.api.get_customer_records(self.channel.id)["can_create_quotation"]
        )
        before = self.env["sale.order"].search_count([])
        action = self.api.get_customer_quotation_action(self.channel.id)
        self.assertEqual(action["channel_id"], self.channel.id)
        self.assertEqual(action["action"]["res_model"], "sale.order")
        self.assertEqual(action["action"]["type"], "ir.actions.act_window")
        self.assertEqual(action["action"]["target"], "new")
        self.assertEqual(action["action"]["views"], [(False, "form")])
        self.assertNotIn("res_id", action["action"])
        self.assertEqual(
            action["action"]["context"],
            {
                "default_partner_id": self.customer.id,
                "default_company_id": self.env.company.id,
                "allowed_company_ids": self.env.company.ids,
            },
        )
        self.assertEqual(self.env["sale.order"].search_count([]), before)

    def test_quotation_form_uses_company_billing_and_shipping_defaults(self):
        self.agent.groups_id |= self.env.ref("sale.group_delivery_invoice_address")
        invoice_address, delivery_address = self.env["res.partner"].create(
            [
                {
                    "name": "Customer Billing",
                    "parent_id": self.customer.id,
                    "type": "invoice",
                },
                {
                    "name": "Customer Delivery",
                    "parent_id": self.customer.id,
                    "type": "delivery",
                },
            ]
        )
        action = self.api.get_customer_quotation_action(self.channel.id)["action"]
        before = self.env["sale.order"].search_count([])
        form = Form(
            self.env["sale.order"]
            .with_user(self.agent)
            .with_context(**action["context"])
        )
        self.assertEqual(form.partner_id, self.customer)
        self.assertEqual(form.partner_invoice_id, invoice_address)
        self.assertEqual(form.partner_shipping_id, delivery_address)
        self.assertEqual(form.company_id, self.env.company)
        self.assertEqual(self.env["sale.order"].search_count([]), before)

    def test_quotation_customer_supports_standalone_and_centralized_contacts(self):
        individual = self.env["res.partner"].create({"name": "Individual Buyer"})
        individual_channel = self._channel(self.account, individual)
        central_channel = self._channel(self.account)
        self.api.link_central_company(central_channel.id, self.customer.id)
        for channel, expected_customer in (
            (individual_channel, individual),
            (central_channel, self.customer),
        ):
            with self.subTest(channel=channel.id):
                self.assertTrue(
                    self.api.get_customer_records(channel.id)["can_create_quotation"]
                )
                action = self.api.get_customer_quotation_action(channel.id)["action"]
                self.assertEqual(
                    action["context"]["default_partner_id"], expected_customer.id
                )

    def test_quotation_keeps_channel_company_and_respects_customer_read_rules(self):
        other_company = self.env["res.company"].create(
            {"name": "Another Sales Company"}
        )
        self.agent.company_ids |= other_company
        api = self.api.with_context(
            allowed_company_ids=[other_company.id, self.env.company.id]
        )
        action = api.get_customer_quotation_action(self.channel.id)["action"]
        self.assertEqual(
            action["context"],
            {
                "default_partner_id": self.customer.id,
                "default_company_id": self.env.company.id,
                "allowed_company_ids": self.env.company.ids,
            },
        )
        self.env["ir.rule"].create(
            {
                "name": "Hide the commercial customer",
                "model_id": self.env["ir.model"]._get_id("res.partner"),
                "domain_force": "[('id', '!=', %s)]" % self.customer.id,
            }
        )
        self.person.with_user(self.agent).check_access_rule("read")
        self.assertFalse(
            api.get_customer_records(self.channel.id)["can_create_quotation"]
        )
        with self.assertRaises(AccessError):
            api.get_customer_quotation_action(self.channel.id)

    def test_sales_tools_require_customer_sales_acl_and_optional_module(self):
        channel = self._customer_channel_for(self.non_sales)
        no_sales = self.api.with_user(self.non_sales)
        self.assertFalse(
            no_sales.get_customer_records(channel.id)["can_create_quotation"]
        )
        with self.assertRaises(AccessError):
            no_sales.get_customer_quotation_action(channel.id)
        with self.assertRaises(AccessError):
            no_sales.get_customer_sale_attachments(channel.id, self.sale.id)
        no_customer = self._channel(self.account)
        self.assertFalse(
            self.api.get_customer_records(no_customer.id)["can_create_quotation"]
        )
        with self.assertRaises(AccessError):
            self.api.get_customer_quotation_action(no_customer.id)
        with patch.dict(self.env.registry.models):
            self.env.registry.models.pop("sale.order")
            self.assertFalse(
                self.api.get_customer_records(self.channel.id)["can_create_quotation"]
            )
            with self.assertRaises(AccessError):
                self.api.get_customer_quotation_action(self.channel.id)
            with self.assertRaises(AccessError):
                self.api.get_customer_sale_attachments(self.channel.id, self.sale.id)

    def test_sales_documents_revalidate_commercial_customer_company_and_record_rules(
        self,
    ):
        foreign_customer = self.env["res.partner"].create({"name": "Other buyer"})
        company = self.env["res.company"].create({"name": "Other document company"})
        self.agent.company_ids |= company
        forbidden = self._sale_document(foreign_customer)
        wrong_company = self._sale_document(self.person, company_id=company.id)
        hidden = self._sale_document(self.person, user_id=self.other.id)
        cancelled = self._sale_document(self.person, state="cancel")
        sibling = self._sale_document(self.sibling)
        company_order = self._sale_document(self.customer, state="sale")
        self.env.flush_all()
        api = self.api.with_context(
            allowed_company_ids=[self.env.company.id, company.id]
        )
        for order in (forbidden, wrong_company, hidden, cancelled):
            with self.subTest(order=order.id), self.assertRaises(AccessError):
                api.get_customer_sale_attachments(self.channel.id, order.id)
        for order in (sibling, company_order):
            self.assertEqual(
                api.get_customer_sale_attachments(self.channel.id, order.id)[
                    "order_id"
                ],
                order.id,
            )
        with self.assertRaises(AccessError):
            self.api.with_user(self.other).get_customer_sale_attachments(
                self.channel.id, self.sale.id
            )

    def test_attachment_list_excludes_foreign_url_and_binary_field_files(self):
        second = self._attachment(self.sale, name="Second.pdf")
        other = self._sale_document(self.person)
        foreign = self._attachment(other)
        linked_url = self._attachment(
            self.sale, type="url", raw=False, url="https://example.invalid/private"
        )
        field_file = self._attachment(self.sale, res_field="signature")
        chatter = self._attachment(self.sale, res_model="mail.message", res_id=0)
        self.env.flush_all()
        first_page = self.api.get_customer_sale_attachments(
            self.channel.id, self.sale.id, limit=1
        )
        second_page = self.api.get_customer_sale_attachments(
            self.channel.id, self.sale.id, offset=1, limit=1
        )
        self.assertEqual([item["id"] for item in first_page["items"]], [second.id])
        self.assertEqual(
            [item["id"] for item in second_page["items"]], [self.original.id]
        )
        self.assertTrue(first_page["has_more"])
        self.assertFalse(second_page["has_more"])
        self.assertEqual(second_page["items"][0]["size_bytes"], len(PDF_CONTENT))
        self.assertEqual(
            second_page["items"][0]["url"],
            "/contact_center/customer/%s/sale/%s/attachment/%s"
            % (self.channel.id, self.sale.id, self.original.id),
        )
        _channel, order = self.api._customer_sale_document(
            self.channel.id, self.sale.id
        )
        for attachment in (foreign, linked_url, field_file, chatter):
            with self.subTest(attachment=attachment.id), self.assertRaises(AccessError):
                self.api._customer_sale_attachment(order, attachment.id)
        self.assertEqual(self.original.res_model, "sale.order")
        self.assertEqual(self.original.res_id, self.sale.id)
        self.env["ir.rule"].create(
            {
                "name": "Hide one customer document",
                "model_id": self.env["ir.model"]._get_id("ir.attachment"),
                "domain_force": "[('id', '!=', %s)]" % self.original.id,
            }
        )
        self.assertNotIn(
            self.original.id,
            [
                item["id"]
                for item in self.api.get_customer_sale_attachments(
                    self.channel.id, self.sale.id
                )["items"]
            ],
        )
        with self.assertRaises(AccessError):
            self.api._customer_sale_attachment(order, self.original.id)


@tagged("post_install", "-at_install")
class TestCustomerSaleDocumentsHttp(CustomerSaleDocumentsCase, HttpCase):
    PASSWORD = "customer-documents-http-test"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent.with_context(no_reset_password=True).password = cls.PASSWORD
        cls.other.with_context(no_reset_password=True).password = cls.PASSWORD
        cls.pdf_url = "/contact_center/customer/%s/sale/%s/pdf" % (
            cls.channel.id,
            cls.sale.id,
        )
        cls.attachment_url = "/contact_center/customer/%s/sale/%s/attachment/%s" % (
            cls.channel.id,
            cls.sale.id,
            cls.original.id,
        )
        cls.env.flush_all()

    def test_pdf_route_renders_only_fixed_sale_report_after_authorization(self):
        self.authenticate(self.agent.login, self.PASSWORD)
        with patch.object(
            type(self.env["ir.actions.report"]),
            "_render_qweb_pdf",
            return_value=(PDF_CONTENT, "pdf"),
        ) as render:
            response = self.url_open(self.pdf_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PDF_CONTENT)
        render.assert_called_once_with(
            "sale.action_report_saleorder", res_ids=self.sale.ids
        )
        self.assertEqual(response.headers["Content-Type"], "application/pdf")
        self.assertIn("inline", response.headers["Content-Disposition"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.authenticate(self.other.login, self.PASSWORD)
        with patch.object(
            type(self.env["ir.actions.report"]), "_render_qweb_pdf"
        ) as render:
            response = self.url_open(self.pdf_url)
        self.assertEqual(response.status_code, 404)
        render.assert_not_called()

    def test_attachment_route_preserves_original_and_rechecks_ownership(self):
        self.authenticate(self.agent.login, self.PASSWORD)
        before = (self.original.res_model, self.original.res_id, self.original.checksum)
        response = self.url_open(self.attachment_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PDF_CONTENT)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.original.invalidate_recordset()
        self.assertEqual(
            (self.original.res_model, self.original.res_id, self.original.checksum),
            before,
        )
        self.assertEqual(self.original.checksum, hashlib.sha1(PDF_CONTENT).hexdigest())
        other = self._sale_document(self.person)
        wrong_order_url = "/contact_center/customer/%s/sale/%s/attachment/%s" % (
            self.channel.id,
            other.id,
            self.original.id,
        )
        self.env.flush_all()
        self.assertEqual(self.url_open(wrong_order_url).status_code, 404)
        self.authenticate(self.other.login, self.PASSWORD)
        self.assertEqual(self.url_open(self.attachment_url).status_code, 404)

    def test_pdf_route_rejects_html_fallback_instead_of_mislabeling_content(self):
        self.authenticate(self.agent.login, self.PASSWORD)
        with patch.object(
            type(self.env["ir.actions.report"]),
            "_render_qweb_pdf",
            return_value=(b"<html>Report fallback</html>", "html"),
        ):
            response = self.url_open(self.pdf_url)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"Report fallback", response.content)
