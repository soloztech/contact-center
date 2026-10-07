from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_base.tests.test_start_conversation import (
    DirectStartTestAdapter,
)

from .test_conversation_crm import ConversationCrmCase


class TestLeadConversationStart(ConversationCrmCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.country_id = cls.env.ref("base.br")
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "CRM start fixture",
                "account_id": cls.account.id,
                "adapter_key": "test.direct_start",
                "external_ref": "crm-start-fixture",
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {"send_message": True},
            }
        )

    def setUp(self):
        super().setUp()
        self.connection.write(
            {
                "last_state_observed_at": fields.Datetime.now(),
                "last_state_source": "health_job",
                "health_detail": "healthy",
            }
        )
        self.prospect = self.env["crm.lead"].create(
            {
                "name": "Meta prospect without customer",
                "type": "lead",
                "phone": "+55 (11) 99876-5432",
                "company_id": self.env.company.id,
                "user_id": self.agent.id,
            }
        )

    def _start(self, lead=None, phone=None, user=None):
        return (
            (lead or self.prospect)
            .with_user(user or self.agent)
            ._contact_center_start_and_link(self.account, phone=phone, writer="manual")
        )

    def test_start_and_repeat_reuse_link_without_partner_or_message(self):
        before_partners = self.env["res.partner"].search_count([])
        before_outbox = self.env["contact.center.outbox.command"].search_count([])
        channel = self._start()
        repeated = self._start(phone="(11) 99876-5432")
        self.assertEqual(channel, repeated)
        links = self.prospect._conversation_links()
        self.assertEqual(len(links), 1)
        self.assertEqual(links.channel_id.id, channel.id)
        self.assertFalse(self.prospect.partner_id)
        self.assertEqual(self.prospect.type, "lead")
        self.assertEqual(self.env["res.partner"].search_count([]), before_partners)
        self.assertEqual(
            self.env["contact.center.outbox.command"].search_count([]), before_outbox
        )
        result = self.api.get_customer_records(channel.id)
        self.assertFalse(result["partner"])
        self.assertEqual([item["id"] for item in result["items"]], [self.prospect.id])
        self.assertTrue(result["items"][0]["linked"])

    def test_exact_phone_suggestions_require_explicit_choice_for_each_lead(self):
        other = self.prospect.copy({"name": "Second enquiry", "phone": "11 99876-5432"})
        wrong = self.prospect.copy({"name": "Other number", "phone": "11 99876-5433"})
        channel_id = self.api.start_conversation(self.account.id, self.prospect.phone)[
            "channel_id"
        ]
        result = self.api.get_customer_records(channel_id)
        self.assertEqual(
            {item["id"] for item in result["items"]}, {self.prospect.id, other.id}
        )
        self.assertTrue(all(item["phone_match"] for item in result["items"]))
        self.assertFalse(any(item["linked"] for item in result["items"]))
        self.assertFalse((self.prospect | other)._conversation_links())
        self.api.link_crm_opportunity(channel_id, other.id)
        self.assertEqual(other._conversation_links().channel_id.id, channel_id)
        self.assertFalse(self.prospect._conversation_links())
        with self.assertRaises(ValidationError):
            self.api.link_crm_opportunity(channel_id, wrong.id)

    def test_mobile_and_phone_match_but_ninth_digit_is_not_guessed(self):
        self.prospect.mobile = "(11) 98765-4321"
        channel_id = self.api.start_conversation(self.account.id, self.prospect.mobile)[
            "channel_id"
        ]
        self.assertEqual(
            [item["id"] for item in self.api.get_customer_records(channel_id)["items"]],
            [self.prospect.id],
        )
        self.prospect.mobile = "(11) 8765-4321"
        self.assertFalse(self.api.get_customer_records(channel_id)["items"])

    def test_start_rejects_unrelated_phone_before_provider_lookup(self):
        with patch.object(DirectStartTestAdapter, "resolve_direct_address") as lookup:
            with self.assertRaises(ValidationError):
                self._start(phone="11 99876-5433")
        lookup.assert_not_called()
        self.assertFalse(self.prospect._conversation_links())

    def test_link_failure_rolls_back_conversation_admission(self):
        before = self.env["mail.channel"].search_count([])
        with patch.object(
            type(self.env["contact.center.crm.conversation.link"]),
            "_link",
            side_effect=ValidationError("CRM link rejected"),
        ):
            with self.assertRaises(ValidationError):
                self._start()
        self.assertEqual(self.env["mail.channel"].search_count([]), before)
        self.assertFalse(self.prospect._conversation_links())

    def test_missing_or_invalid_phone_does_not_block_native_lead_creation(self):
        for phone in (False, "not a phone", "123"):
            with self.subTest(phone=phone):
                self.prospect.phone = phone
                self.assertFalse(self.prospect.contact_center_phone_normalized)
                with self.assertRaises(ValidationError):
                    self._start()

    def test_missing_sales_and_inbox_permissions_are_rejected(self):
        self.prospect.user_id = self.other
        self.env.flush_all()
        with patch.object(DirectStartTestAdapter, "resolve_direct_address") as lookup:
            for user in (self.agent, self.other, self.non_sales):
                with self.subTest(user=user), self.assertRaises(AccessError):
                    self._start(user=user)
        lookup.assert_not_called()

    def test_different_company_rejected_even_with_both_companies_allowed(self):
        other = self.env["res.company"].create({"name": "Different lead company"})
        self.agent.company_ids |= other
        # Create the foreign lead in its final company: installed Marketing anchors
        # immutable evidence at creation and correctly forbids moving it later.
        foreign = self.prospect.copy(
            {"company_id": other.id, "user_id": False, "team_id": False}
        )
        lead = foreign.with_user(self.agent).with_context(
            allowed_company_ids=[self.env.company.id, other.id]
        )
        with patch.object(DirectStartTestAdapter, "resolve_direct_address") as lookup:
            with self.assertRaises(ValidationError):
                lead._contact_center_start_and_link(self.account, writer="manual")
        lookup.assert_not_called()

    def test_phone_candidates_obey_crm_record_rules(self):
        channel = self._start()
        self.prospect.user_id = self.other
        self.env.flush_all()
        self.assertFalse(self.api.get_customer_records(channel.id)["items"])

    def test_company_scope_applies_to_phone_candidates(self):
        channel = self._start()
        company = self.env["res.company"].create({"name": "Foreign phone lead"})
        self.agent.company_ids |= company
        foreign = self.prospect.copy(
            {"company_id": company.id, "user_id": False, "team_id": False}
        )
        result = self.api.with_context(
            allowed_company_ids=[self.env.company.id, company.id]
        ).get_customer_records(channel.id)
        self.assertNotIn(foreign.id, [item["id"] for item in result["items"]])

    def test_wizard_opens_inbox_with_explicit_channel_and_no_send(self):
        action = self.prospect.with_user(self.agent).action_contact_center_converse()
        wizard = (
            self.env["contact.center.crm.start"]
            .with_user(self.agent)
            .create(
                {
                    "lead_id": self.prospect.id,
                    "account_id": self.account.id,
                    "phone": action["context"]["default_phone"],
                }
            )
        )
        self.assertIn(self.account, wizard.available_account_ids)
        result = wizard.action_start()
        self.assertEqual(result["tag"], "contact_center_ui.inbox")
        self.assertEqual(
            result["params"]["channel_id"],
            self.prospect._conversation_links().channel_id.id,
        )

    def test_phone_link_is_rechecked_after_lead_phone_changes(self):
        channel_id = self.api.start_conversation(self.account.id, self.prospect.phone)[
            "channel_id"
        ]
        self.prospect.phone = "11 99876-5433"
        with self.assertRaises(ValidationError):
            self.api.link_crm_opportunity(channel_id, self.prospect.id)

    def test_unconfirmed_phone_alias_does_not_suggest_a_lead(self):
        channel = self._start()
        self.api.unlink_crm_opportunity(channel.id, self.prospect.id)
        binding = self.api._binding_for_channel(channel)
        aliases = binding.identity_id.alias_ids
        # Remove confidence in the alias without deleting the channel. LID or
        # observed provider hints must not become phone evidence for CRM linking.
        aliases.sudo().write({"confidence": "observed"})
        self.assertFalse(self.api.get_customer_records(channel.id)["items"])
