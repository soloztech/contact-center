import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase


class TestConversationCrm(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        groups = cls.env.ref(
            "contact_center_base.group_contact_center_agent"
        ) | cls.env.ref("sales_team.group_sale_salesman")
        cls.agent = cls._user("Agent", groups)
        cls.other = cls._user("Other", groups)
        cls.non_sales = cls._user(
            "Support", cls.env.ref("contact_center_base.group_contact_center_agent")
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "CRM chat",
                "platform": "whatsapp",
                "external_ref": str(uuid.uuid4()),
                "company_id": cls.env.company.id,
                "owner_user_id": cls.agent.id,
            }
        )
        cls.customer = cls.env["res.partner"].create(
            {"name": "Customer Company", "is_company": True}
        )
        cls.person = cls.env["res.partner"].create(
            {"name": "Customer Person", "parent_id": cls.customer.id}
        )
        cls.sibling = cls.env["res.partner"].create(
            {"name": "Customer Colleague", "parent_id": cls.customer.id}
        )
        cls.channel = cls._channel(cls.account, cls.person)
        cls.lead = cls._lead("Person opportunity", cls.person)
        cls.company_lead = cls._lead("Company opportunity", cls.customer)
        cls.sibling_lead = cls._lead("Colleague opportunity", cls.sibling)
        cls.api = cls.env["contact.center.ui.api"].with_user(cls.agent)

    @classmethod
    def _user(cls, name, groups):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": name,
                    "login": str(uuid.uuid4()),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, groups.ids)],
                }
            )
        )

    @classmethod
    def _channel(cls, account, partner=None):
        guest = cls.env["mail.guest"].create({"name": "CRM Guest"})
        identity = cls.env["contact.center.identity"].create(
            {
                "company_id": cls.env.company.id,
                "name": "CRM Customer",
                "mail_guest_id": guest.id,
                "partner_id": partner.id if partner else False,
                "partner_link_kind": "person" if partner else False,
            }
        )
        channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=account, identity=identity, guest_ids=guest.ids
        )
        cls.env["contact.center.channel.binding"].create(
            {
                "channel_id": channel.id,
                "account_id": account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": str(uuid.uuid4()),
            }
        )
        return channel

    @classmethod
    def _lead(cls, name, partner, **values):
        return cls.env["crm.lead"].create(
            {
                "name": name,
                "partner_id": partner.id,
                "company_id": cls.env.company.id,
                "type": "opportunity",
                "user_id": cls.agent.id,
                **values,
            }
        )

    def _links(self):
        return (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search([("channel_id", "=", self.channel.id), ("state", "=", "active")])
        )

    def test_sales_agent_has_crm_panel_capability(self):
        self.assertTrue(self.api.bootstrap()["capabilities"]["view_crm"])

    def test_panel_lists_person_company_and_sibling_opportunities(self):
        unrelated = self._lead(
            "Unrelated", self.env["res.partner"].create({"name": "Unrelated"})
        )
        result = self.api.get_crm_opportunities(self.channel.id)
        self.assertEqual(
            {item["id"] for item in result["items"]},
            {self.lead.id, self.company_lead.id, self.sibling_lead.id},
        )
        self.assertNotIn(unrelated.id, result["linked_opportunity_ids"])
        self.assertEqual(result["partner"]["id"], self.person.id)
        self.assertEqual(result["commercial_partner"]["id"], self.customer.id)

    def test_link_multiple_idempotently_without_pipeline_or_stage_changes(self):
        stage = self.lead.stage_id
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        self.api.link_crm_opportunity(self.channel.id, self.company_lead.id)
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        self.assertEqual(
            set(self._links().mapped("lead_id").ids),
            {self.lead.id, self.company_lead.id},
        )
        self.assertEqual(self.lead.stage_id, stage)
        self.assertEqual(
            self.lead.with_user(self.agent).contact_center_conversation_count, 1
        )

    def test_remove_one_association_preserves_other_opportunities_and_contact(self):
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        self.api.link_crm_opportunity(self.channel.id, self.company_lead.id)
        self.api.unlink_crm_opportunity(self.channel.id, self.lead.id)
        self.api.unlink_crm_opportunity(self.channel.id, self.lead.id)
        self.assertEqual(self._links().mapped("lead_id"), self.company_lead)
        self.assertTrue(self.lead.active)
        self.assertEqual(
            self.api.get_crm_opportunities(self.channel.id)["partner"]["id"],
            self.person.id,
        )

    def test_relink_retains_audit_and_only_one_active_association(self):
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        first = self._links()
        self.api.unlink_crm_opportunity(self.channel.id, self.lead.id)
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        self.assertEqual(first.state, "unlinked")
        self.assertFalse(first.lead_id)
        self.assertNotEqual(first.id, self._links().id)

    def test_no_partner_returns_empty_panel_without_searching_all_crm(self):
        channel = self._channel(self.account)
        result = self.api.get_crm_opportunities(channel.id)
        self.assertFalse(result["partner"])
        self.assertFalse(result["items"])
        self.assertFalse(result["capabilities"]["link"])

    def test_customer_changes_do_not_silently_remove_explicit_links(self):
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        self.api.unlink_partner(self.channel.id, expected_partner_id=self.person.id)
        result = self.api.get_crm_opportunities(self.channel.id)
        self.assertFalse(result["partner"])
        self.assertEqual(result["linked_opportunity_ids"], [self.lead.id])
        self.assertEqual([item["id"] for item in result["items"]], [self.lead.id])

    def test_pagination_keeps_linked_first_without_duplicates(self):
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        pages = [
            self.api.get_crm_opportunities(self.channel.id, offset=offset, limit=1)
            for offset in range(3)
        ]
        self.assertEqual(pages[0]["items"][0]["id"], self.lead.id)
        self.assertEqual(len({page["items"][0]["id"] for page in pages}), 3)
        self.assertTrue(pages[0]["has_more"])
        self.assertFalse(pages[2]["has_more"])
        result = self.api.get_crm_opportunities(self.channel.id, query="Company")
        self.assertEqual(
            [item["id"] for item in result["items"]], [self.company_lead.id]
        )

    def test_archived_link_remains_visible_but_not_a_new_candidate(self):
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        self.lead.active = False
        self.company_lead.active = False
        result = self.api.get_crm_opportunities(self.channel.id)
        self.assertIn(self.lead.id, [item["id"] for item in result["items"]])
        self.assertNotIn(self.company_lead.id, [item["id"] for item in result["items"]])

    def test_customer_scope_prevents_linking_an_arbitrary_crm_record(self):
        lead = self._lead(
            "Other customer", self.env["res.partner"].create({"name": "Other"})
        )
        with self.assertRaises(ValidationError):
            self.api.link_crm_opportunity(self.channel.id, lead.id)
        self.assertFalse(self._links())

    def test_crm_record_rules_filter_candidates_and_linked_ids(self):
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        self.lead.user_id = self.other
        result = self.api.get_crm_opportunities(self.channel.id)
        self.assertNotIn(self.lead.id, result["linked_opportunity_ids"])
        self.assertNotIn(self.lead.id, [item["id"] for item in result["items"]])
        with self.assertRaises(AccessError):
            self.api.unlink_crm_opportunity(self.channel.id, self.lead.id)

    def test_inaccessible_conversation_and_missing_sales_access_are_rejected(self):
        with self.assertRaises(AccessError):
            self.api.with_user(self.other).get_crm_opportunities(self.channel.id)
        self.assertFalse(
            self.api.with_user(self.non_sales).bootstrap()["capabilities"]["view_crm"]
        )
        with self.assertRaises(AccessError):
            self.api.with_user(self.non_sales).link_crm_opportunity(
                self.channel.id, self.lead.id
            )

    def test_direct_ledger_mutation_is_not_an_rpc_escape_hatch(self):
        model = self.env["contact.center.crm.conversation.link"]
        with self.assertRaises(AccessError):
            model.create({"channel_id": self.channel.id, "lead_id": self.lead.id})
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        with self.assertRaises(AccessError):
            self._links().write({"lead_id": self.company_lead.id})
        with self.assertRaises(AccessError):
            self._links().unlink()
        with self.assertRaises(AccessError):
            model.with_user(self.agent).search_read([], ["lead_id", "channel_id"])

    def test_cross_company_candidate_and_link_are_rejected(self):
        company = self.env["res.company"].create({"name": "CRM Other Company"})
        lead = self._lead(
            "Cross company", self.person, company_id=company.id, user_id=False
        )
        with self.assertRaises(AccessError):
            self.api.link_crm_opportunity(self.channel.id, lead.id)
        with self.assertRaises(ValidationError):
            self.env["contact.center.crm.conversation.link"]._link(self.channel, lead)

    def test_company_change_cannot_break_existing_links(self):
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        company = self.env["res.company"].create({"name": "CRM Other Company"})
        with self.assertRaises(ValidationError):
            self.lead.write({"company_id": company.id})

    def test_native_lead_deletion_retires_link_preserving_conversation(self):
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        link = self._links()
        self.lead.unlink()
        self.assertEqual(link.state, "unlinked")
        self.assertEqual(link.unlinked_reason, "lead_deleted")
        self.assertTrue(self.channel.exists())

    def test_native_merge_transfers_links_and_collapses_duplicates(self):
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        self.api.link_crm_opportunity(self.channel.id, self.company_lead.id)
        survivor = (self.lead | self.company_lead)._merge_opportunity()
        self.assertEqual(self._links().mapped("lead_id"), survivor)
        self.assertEqual(len(self._links()), 1)
        self.assertEqual(
            self.api.get_crm_opportunities(self.channel.id)["linked_opportunity_ids"],
            survivor.ids,
        )

    def test_computed_company_change_cannot_break_conversation_association(self):
        self.api.link_crm_opportunity(self.channel.id, self.lead.id)
        company = self.env["res.company"].create({"name": "Other Sales Company"})
        team = self.env["crm.team"].create(
            {"name": "Other Sales Team", "company_id": company.id, "user_id": False}
        )
        unlinked = self._lead("Unlinked company recomputation", self.person)
        unlinked.write({"team_id": team.id, "user_id": False})
        self.assertEqual(unlinked.company_id, company)
        previous_team = self.lead.team_id
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.lead.write({"team_id": team.id, "user_id": False})
        self.assertEqual(self.lead.company_id, self.env.company)
        self.assertEqual(self.lead.team_id, previous_team)
        self.assertEqual(self._links().lead_id, self.lead)
