import json
import uuid

from odoo.exceptions import AccessError, MissingError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_MEMBERSHIP_TOKEN,
)


@tagged("post_install", "-at_install")
class TestSaleConversations(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cc = cls.env.ref("contact_center_base.group_contact_center_agent")
        sales = cls.env.ref("sales_team.group_sale_salesman")
        cls.agent = cls._user("Sales agent", cc | sales)
        cls.other = cls._user("Other agent", cc | sales)
        cls.seller = cls._user("Sales only", sales)
        cls.support = cls._user("Support only", cc)
        cls.customer = cls.env["res.partner"].create(
            {"name": "Empresa Y", "is_company": True}
        )
        cls.person = cls.env["res.partner"].create(
            {"name": "Bruno", "parent_id": cls.customer.id}
        )
        cls.account = cls._account("Comercial", cls.agent)
        cls.restricted = cls._account("Alice", cls.other)
        cls.binding = cls._binding(cls.account, cls.person)
        cls.hidden = cls._binding(
            cls.restricted, cls.person, identity=cls.binding.identity_id
        )
        cls.order = cls._order(cls.customer)
        cls.sale = cls.order.with_user(cls.agent)

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
    def _account(cls, name, user, company=None):
        return cls.env["contact.center.account"].create(
            {
                "name": name,
                "platform": "whatsapp",
                "external_ref": str(uuid.uuid4()),
                "company_id": (company or cls.env.company).id,
                "access_user_ids": [(6, 0, user.ids)],
            }
        )

    @classmethod
    def _binding(cls, account, partner, identity=None):
        if not identity:
            guest = cls.env["mail.guest"].create({"name": "+55 11 99999-1234"})
            identity = cls.env["contact.center.identity"].create(
                {
                    "name": guest.name,
                    "company_id": account.company_id.id,
                    "mail_guest_id": guest.id,
                    "partner_id": partner.id,
                    "partner_link_kind": "central_company"
                    if partner.is_company
                    else "person",
                }
            )
        channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=account, identity=identity, guest_ids=identity.mail_guest_id.ids
        )
        return cls.env["contact.center.channel.binding"].create(
            {
                "channel_id": channel.id,
                "account_id": account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": str(uuid.uuid4()),
            }
        )

    @classmethod
    def _order(cls, partner, **values):
        return cls.env["sale.order"].create(
            {
                "partner_id": partner.id,
                "company_id": cls.env.company.id,
                "user_id": cls.agent.id,
                **values,
            }
        )

    def test_two_inboxes_one_contact_expose_only_safe_metadata(self):
        self.hidden.channel_id._contact_center_post(
            "inbound",
            body="SECRET MESSAGE",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_guest_id=self.hidden.identity_id.mail_guest_id.id,
        )
        payload = self.sale.get_contact_center_conversations()
        self.assertEqual(self.sale.contact_center_conversation_count, 2)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(
            payload["items"],
            [
                {
                    "channel_id": self.binding.channel_id.id,
                    "contact_name": "Bruno",
                    "inbox_name": "Comercial",
                    "can_open": True,
                },
                {
                    "channel_id": False,
                    "contact_name": "Bruno",
                    "inbox_name": "Alice",
                    "can_open": False,
                },
            ],
        )
        self.assertNotIn("SECRET", json.dumps(payload))
        self.assertNotIn("99999", json.dumps(payload))
        api_model = self.env["contact.center.ui.api"].with_user(self.agent)
        for method in (api_model.get_conversation, api_model.get_timeline):
            with self.assertRaises(AccessError):
                method(self.hidden.channel_id.id)
        with self.assertRaises(AccessError):
            self.sale.open_contact_center_conversation(self.hidden.channel_id.id)

    def test_company_contact_and_secondary_company_relationships(self):
        self.assertEqual(
            self._order(self.person)
            .with_user(self.agent)
            .get_contact_center_conversations()["total"],
            2,
        )
        sibling = self.env["res.partner"].create(
            {"name": "Colega", "parent_id": self.customer.id}
        )
        self._binding(self.account, sibling)
        secondary_person = self.env["res.partner"].create(
            {
                "name": "Contato secundário",
                "contact_center_secondary_company_ids": [(6, 0, self.customer.ids)],
            }
        )
        self._binding(self.account, secondary_person)
        self._binding(self.account, self.customer)
        payload = self.sale.get_contact_center_conversations()
        self.assertEqual(payload["total"], 5)
        self.assertEqual(
            {row["contact_name"] for row in payload["items"]},
            {"Bruno", "Colega", "Contato secundário", "Empresa Y"},
        )
        self.assertEqual(secondary_person.commercial_partner_id, secondary_person)

    def test_standalone_person_does_not_expand_to_secondary_company(self):
        self.person.write(
            {
                "parent_id": False,
                "contact_center_secondary_company_ids": [(6, 0, self.customer.ids)],
            }
        )
        self._binding(self.account, self.customer)
        self.assertEqual(
            self._order(self.person)
            .with_user(self.agent)
            .get_contact_center_conversations()["total"],
            2,
        )

    def test_single_conversation_and_no_conversations(self):
        person = self.env["res.partner"].create({"name": "Single"})
        order = self._order(person).with_user(self.agent)
        self.assertEqual(order.contact_center_conversation_count, 0)
        self.assertFalse(order.get_contact_center_conversations()["items"])
        binding = self._binding(self.account, person)
        result = order.open_contact_center_conversation(binding.channel_id.id)
        self.assertEqual(result["item"]["channel_id"], binding.channel_id.id)
        self.assertEqual(
            order.action_contact_center_conversations()["params"],
            {"order_id": order.id},
        )
        new = (
            self.env["sale.order"].with_user(self.agent).new({"partner_id": person.id})
        )
        self.assertEqual(new.contact_center_conversation_count, 0)

    def test_single_restricted_conversation_is_still_counted(self):
        self.binding.active = False
        self.assertEqual(self.sale.contact_center_conversation_count, 1)
        row = self.sale.get_contact_center_conversations()["items"][0]
        self.assertFalse(row["can_open"])
        self.assertFalse(row["channel_id"])

    def test_resolved_archived_states_and_inactive_inbox_remain_visible(self):
        for state in ("resolved", "archived"):
            self.binding.channel_id.with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
            ).write({"contact_center_state": state})
            self.assertEqual(self.sale.get_contact_center_conversations()["total"], 2)
            self.assertEqual(
                self.sale.open_contact_center_conversation(self.binding.channel_id.id)[
                    "item"
                ]["state"],
                state,
            )
        self.account.active = False
        self.assertEqual(self.sale.get_contact_center_conversations()["total"], 2)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.person.active = False

    def test_retired_bindings_and_groups_are_not_customer_conversations(self):
        retired = self._binding(self.account, self.person)
        retired.write({"active": False, "merged_into_id": self.binding.id})
        group_channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account, name="Customer group", conversation_type="group"
        )
        self.env["contact.center.channel.binding"].create(
            {
                "account_id": self.account.id,
                "channel_id": group_channel.id,
                "conversation_type": "group",
                "conversation_ref": str(uuid.uuid4()),
            }
        )
        self.assertEqual(self.sale.get_contact_center_conversations()["total"], 2)
        with self.assertRaises(AccessError):
            self.sale.open_contact_center_conversation(retired.channel_id.id)

    def test_non_agents_and_non_sales_users_cannot_use_endpoints(self):
        seller_order = self._order(self.customer, user_id=self.seller.id).with_user(
            self.seller
        )
        self.assertEqual(seller_order.contact_center_conversation_count, 0)
        for order in (seller_order, self.order.with_user(self.support)):
            for method, args in (
                (order.action_contact_center_conversations, ()),
                (order.get_contact_center_conversations, ()),
                (order.open_contact_center_conversation, (self.binding.channel_id.id,)),
            ):
                with self.assertRaises(AccessError):
                    method(*args)

    def test_sale_record_rules_apply_before_projection(self):
        self.env["ir.rule"].create(
            {
                "name": "Hidden sales order",
                "model_id": self.env.ref("sale.model_sale_order").id,
                "domain_force": "[('id', '!=', %s)]" % self.order.id,
            }
        )
        with self.assertRaises(AccessError):
            self.sale.get_contact_center_conversations()
        with self.assertRaises(AccessError):
            self.sale.open_contact_center_conversation(self.binding.channel_id.id)

    def test_partner_rule_fallback_even_when_name_is_cached_by_sudo(self):
        self.assertEqual(self.person.sudo().name, "Bruno")
        self.env["ir.rule"].create(
            {
                "name": "Hidden person",
                "model_id": self.env.ref("base.model_res_partner").id,
                "domain_force": "[('id', '!=', %s)]" % self.person.id,
            }
        )
        rows = self.sale.get_contact_center_conversations()["items"]
        self.assertEqual([row["contact_name"] for row in rows], ["Contato", "Contato"])

    def test_order_company_bounds_projection_with_two_active_companies(self):
        foreign = self.env["res.company"].create({"name": "Other operational company"})
        self.agent.company_ids += foreign
        account = self._account("Other tenant inbox", self.agent, foreign)
        binding = self._binding(account, self.person)
        order = self.sale.with_context(
            allowed_company_ids=(self.env.company | foreign).ids
        )
        self.assertEqual(order.get_contact_center_conversations()["total"], 2)
        with self.assertRaises(AccessError):
            order.open_contact_center_conversation(binding.channel_id.id)
        with self.assertRaises(AccessError):
            self.sale.with_context(
                allowed_company_ids=foreign.ids
            ).get_contact_center_conversations()

    def test_access_is_revalidated_after_inbox_revocation(self):
        self.assertTrue(
            self.sale.get_contact_center_conversations()["items"][0]["can_open"]
        )
        self.account.write({"access_user_ids": [(6, 0, self.other.ids)]})
        with self.assertRaises(AccessError):
            self.sale.open_contact_center_conversation(self.binding.channel_id.id)
        self.assertFalse(
            self.sale.get_contact_center_conversations()["items"][0]["can_open"]
        )

    def test_changed_customer_and_unlinked_identity_reject_stale_selection(self):
        self.sale.get_contact_center_conversations()
        self.order.partner_id = self.env["res.partner"].create(
            {"name": "Different customer"}
        )
        with self.assertRaises(AccessError):
            self.sale.open_contact_center_conversation(self.binding.channel_id.id)
        self.order.partner_id = self.customer
        self.env["contact.center.ui.api"].with_user(self.agent).unlink_partner(
            self.binding.channel_id.id, self.person.id
        )
        self.assertFalse(self.sale.get_contact_center_conversations()["items"])
        with self.assertRaises(AccessError):
            self.sale.open_contact_center_conversation(self.binding.channel_id.id)

    def test_unrelated_channel_and_deleted_order_rejected(self):
        outsider = self._binding(
            self.account, self.env["res.partner"].create({"name": "Outsider"})
        )
        with self.assertRaises(AccessError):
            self.sale.open_contact_center_conversation(outsider.channel_id.id)
        absent = self.env["sale.order"].with_user(self.agent).browse(999999999)
        with self.assertRaises(MissingError):
            absent.get_contact_center_conversations()

    def test_pagination_is_bounded_stable_and_validated(self):
        first = self.sale.get_contact_center_conversations(limit=1)
        second = self.sale.get_contact_center_conversations(offset=1, limit=1)
        self.assertEqual(first["total"], 2)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(
            [first["items"][0]["inbox_name"], second["items"][0]["inbox_name"]],
            ["Comercial", "Alice"],
        )
        self.assertEqual(
            self.sale.get_contact_center_conversations(limit=999)["limit"], 100
        )
        for kwargs in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": "50"}):
            with self.assertRaises(ValidationError):
                self.sale.get_contact_center_conversations(**kwargs)
        with self.assertRaises(ValidationError):
            self.sale.open_contact_center_conversation(True)

    def test_restricted_unrelated_and_absent_channels_have_same_rejection(self):
        outsider = self._binding(
            self.account, self.env["res.partner"].create({"name": "Unrelated customer"})
        )
        rejections = []
        for channel_id in (
            self.hidden.channel_id.id,
            outsider.channel_id.id,
            999999999,
        ):
            with self.assertRaises(AccessError) as caught:
                self.sale.open_contact_center_conversation(channel_id)
            rejections.append(str(caught.exception))
            self.assertIsNone(caught.exception.__context__)
        self.assertEqual(len(set(rejections)), 1)

    def test_listing_does_not_mark_read_or_change_documents(self):
        member = self.binding.channel_id.with_user(
            self.agent
        )._contact_center_member_for_current_user()
        before = (member.seen_message_id.id, self.order.write_date, self.order.state)
        self.sale.get_contact_center_conversations()
        self.assertEqual(
            (member.seen_message_id.id, self.order.write_date, self.order.state), before
        )
