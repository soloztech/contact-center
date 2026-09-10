import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase


class TestPartnerCompanies(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Relationship agent",
                    "login": "relationship-%s" % uuid.uuid4(),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Relationship inbox",
                "platform": "whatsapp",
                "company_id": cls.env.company.id,
                "external_ref": "relationship-%s" % uuid.uuid4(),
                "access_user_ids": [(6, 0, cls.agent.ids)],
            }
        )
        guest = cls.env["mail.guest"].sudo().create({"name": "Relationship guest"})
        cls.identity = (
            cls.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": guest.name,
                    "company_id": cls.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        cls.channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=cls.account,
            identity=cls.identity,
            partner_ids=cls.agent.partner_id.ids,
            guest_ids=guest.ids,
        )
        cls.env["contact.center.channel.binding"].sudo().create(
            {
                "channel_id": cls.channel.id,
                "account_id": cls.account.id,
                "identity_id": cls.identity.id,
                "conversation_type": "direct",
                "conversation_ref": "relationship-%s" % uuid.uuid4(),
            }
        )
        cls.person = cls.env["res.partner"].create(
            {
                "name": "Related person",
                "company_type": "person",
                "type": "contact",
                "company_id": cls.env.company.id,
            }
        )
        cls.primary, cls.secondary = cls.env["res.partner"].create(
            [
                {
                    "name": name,
                    "is_company": True,
                    "type": "contact",
                    "company_id": cls.env.company.id,
                }
                for name in ("Primary company", "Secondary company")
            ]
        )
        cls.api = cls.env["contact.center.ui.api"].with_user(cls.agent)
        cls.api.link_partner(cls.channel.id, cls.person.id)
        cls.api.link_partner_company(cls.channel.id, cls.person.id, cls.primary.id)

    def test_agent_can_link_secondary_without_changing_commercial_entity(self):
        result = self.api.link_partner_company(
            self.channel.id, self.person.id, self.secondary.id, "secondary"
        )
        self.assertEqual(self.person.parent_id, self.primary)
        self.assertEqual(self.person.commercial_partner_id, self.primary)
        self.assertEqual(
            result["identity"]["partner"]["secondary_companies"][0]["id"],
            self.secondary.id,
        )
        self.api.link_partner_company(
            self.channel.id, self.person.id, self.secondary.id, "secondary"
        )
        self.assertEqual(
            self.person.contact_center_secondary_company_ids, self.secondary
        )
        listed = self.api.list_conversations()["items"]
        projection = next(
            item for item in listed if item["channel_id"] == self.channel.id
        )["identity"]["partner"]
        self.assertEqual(
            projection["secondary_companies"],
            result["identity"]["partner"]["secondary_companies"],
        )

    def test_agent_can_create_secondary_and_remove_each_relationship(self):
        result = self.api.create_and_link_partner_company(
            self.channel.id, self.person.id, {"name": "Created secondary"}, "secondary"
        )
        company_id = result["company"]["id"]
        self.assertEqual(self.person.parent_id, self.primary)
        self.api.unlink_partner_company(
            self.channel.id, self.person.id, self.primary.id
        )
        self.assertEqual(self.identity.partner_id, self.person)
        self.assertFalse(self.person.parent_id)
        self.assertEqual(self.person.commercial_partner_id, self.person)
        self.assertEqual(
            self.person.contact_center_secondary_company_ids.ids, [company_id]
        )
        self.api.unlink_partner_company(
            self.channel.id, self.person.id, company_id, "secondary"
        )
        self.assertFalse(self.person.contact_center_secondary_company_ids)
        self.assertTrue(self.env["res.partner"].browse(company_id).exists())
        self.api.unlink_partner(self.channel.id, self.person.id)
        self.assertFalse(self.identity.partner_id)
        self.assertTrue(self.person.exists())

    def test_correcting_wrong_person_keeps_global_company_relationships(self):
        self.api.link_partner_company(
            self.channel.id, self.person.id, self.secondary.id, "secondary"
        )
        self.api.unlink_partner(self.channel.id, self.person.id)
        self.assertFalse(self.identity.partner_id)
        self.assertEqual(self.person.parent_id, self.primary)
        self.assertEqual(
            self.person.contact_center_secondary_company_ids, self.secondary
        )

    def test_disabling_feature_preserves_existing_and_allows_removal(self):
        self.api.link_partner_company(
            self.channel.id, self.person.id, self.secondary.id, "secondary"
        )
        self.env.company.contact_center_secondary_companies_enabled = False
        with self.assertRaises(ValidationError):
            self.api.create_and_link_partner_company(
                self.channel.id,
                self.person.id,
                {"name": "Disabled secondary"},
                "secondary",
            )
        projection = self.api.get_conversation(self.channel.id)["item"]["identity"][
            "partner"
        ]
        self.assertFalse(projection["secondary_company_linking_allowed"])
        self.assertEqual(projection["secondary_companies"][0]["id"], self.secondary.id)
        self.api.unlink_partner_company(
            self.channel.id, self.person.id, self.secondary.id, "secondary"
        )
        self.assertFalse(self.person.contact_center_secondary_company_ids)

    def test_invalid_and_stale_relationship_requests_are_rejected(self):
        other_company = self.env["res.company"].create({"name": "Other operator"})
        foreign = self.env["res.partner"].create(
            {
                "name": "Foreign customer",
                "is_company": True,
                "company_id": other_company.id,
            }
        )
        for target in (self.person, self.primary, foreign):
            with self.assertRaises(ValidationError):
                self.api.link_partner_company(
                    self.channel.id, self.person.id, target.id, "secondary"
                )
        with self.assertRaises(ValidationError):
            self.api.unlink_partner_company(
                self.channel.id, self.secondary.id, self.primary.id
            )
        with self.assertRaises(ValidationError):
            self.api.unlink_partner_company(
                self.channel.id, self.person.id, self.secondary.id
            )

    def test_agent_cannot_mutate_relations_through_inaccessible_conversation(self):
        self.account.access_user_ids = False
        with self.assertRaises(AccessError):
            self.api.link_partner_company(
                self.channel.id, self.person.id, self.secondary.id, "secondary"
            )
        with self.assertRaises(AccessError):
            self.api.unlink_partner_company(
                self.channel.id, self.person.id, self.primary.id
            )
        self.assertEqual(self.person.parent_id, self.primary)
        self.assertFalse(self.person.contact_center_secondary_company_ids)

    def test_inverse_company_type_and_scope_changes_preserve_invariant(self):
        self.api.link_partner_company(
            self.channel.id, self.person.id, self.secondary.id, "secondary"
        )
        other_company = self.env["res.company"].create(
            {"name": "Other relationship tenant"}
        )
        for values in (
            {"is_company": False},
            {"company_id": other_company.id},
            {"type": "delivery"},
        ):
            with self.assertRaises(ValidationError), self.cr.savepoint():
                self.secondary.write(values)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.person.write(
                {"contact_center_secondary_company_ids": [(4, self.primary.id)]}
            )
