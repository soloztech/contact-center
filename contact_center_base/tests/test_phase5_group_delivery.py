import datetime
import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..services.dto import AddressDTO


class TestGroupParticipantDeliveryLedger(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        admin_group = cls.env.ref("contact_center_base.group_contact_center_admin")
        cls.agent = cls._create_user("Group Receipt Agent", agent_group)
        cls.admin = cls._create_user("Group Receipt Administrator", admin_group)
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Group Receipt Team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Group Receipt Account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "group-receipt-account-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, cls.team.ids)],
                "technical_author_id": cls.agent.partner_id.id,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Group Receipt Connection",
                "account_id": cls.account.id,
                "adapter_key": "test.fake",
                "external_ref": "group-receipt-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {"send_message": True},
            }
        )
        cls.channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=cls.account,
            conversation_type="group",
            name="Group Receipt Conversation",
            teams=cls.team,
            partner_ids=cls.agent.partner_id.ids,
        )
        cls.channel_binding = (
            cls.env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": cls.channel.id,
                    "account_id": cls.account.id,
                    "conversation_type": "group",
                    "conversation_ref": "120363000000099@g.us",
                }
            )
        )
        cls.profile = (
            cls.env["contact.center.group.profile"]
            .sudo()
            .create(
                {
                    "channel_binding_id": cls.channel_binding.id,
                    "provider_connection_id": cls.connection.id,
                    "name": "Group Receipt Conversation",
                    "metadata_state": "ready",
                    "roster_complete": True,
                    "participant_count": 1,
                }
            )
        )
        cls.participant = (
            cls.env["contact.center.group.participant"]
            .sudo()
            .create(
                {
                    "group_profile_id": cls.profile.id,
                    "participant_ref": "200000000000099@lid",
                    "name": "Receipt Participant",
                    "role": "member",
                }
            )
        )
        cls.participant_lid = "200000000000099@lid"
        cls.participant_pn = "5511900000099@s.whatsapp.net"
        cls.env["contact.center.group.participant.alias"].sudo().create(
            [
                {
                    "participant_id": cls.participant.id,
                    "namespace": "whatsapp.lid",
                    "value_raw": cls.participant_lid,
                    "value_normalized": cls.participant_lid,
                    "role": "primary",
                    "source_field": "data.Participants.JID",
                    "confidence": "protocol",
                },
                {
                    "participant_id": cls.participant.id,
                    "namespace": "whatsapp.pn",
                    "value_raw": cls.participant_pn,
                    "value_normalized": cls.participant_pn,
                    "role": "alternate",
                    "source_field": "data.Participants.PhoneNumber",
                    "confidence": "protocol",
                },
            ]
        )
        cls.message = cls.channel.sudo()._contact_center_post(
            origin="outbound",
            body="Group delivery ledger fixture",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_id=cls.agent.partner_id.id,
            partner_ids=[],
        )
        cls.message_binding = (
            cls.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": cls.message.id,
                    "channel_binding_id": cls.channel_binding.id,
                    "provider_connection_id": cls.connection.id,
                    "direction": "outbound",
                    "origin": "agent",
                    "content_type": "text",
                    "external_message_id": "GROUP-DELIVERY-MESSAGE-0001",
                    "delivery_state": "sent",
                }
            )
        )

    @classmethod
    def _create_user(cls, name, group):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": name,
                    "login": "cc-group-receipt-%s" % uuid.uuid4(),
                    "email": "cc-group-receipt-%s@example.invalid" % uuid.uuid4(),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )

    def _address(self, namespace="whatsapp.lid", value=None):
        value = value or (
            self.participant_lid if namespace == "whatsapp.lid" else self.participant_pn
        )
        return AddressDTO(
            namespace=namespace,
            value=value,
            value_normalized=value,
            role="sender",
            source_field="event.Sender",
            confidence="protocol",
        )

    def _record(
        self,
        state,
        *,
        address=None,
        occurred_at=None,
        observed_at=None,
        external_event_id=None,
        message_binding=None,
        participant=None,
        group_profile=None,
    ):
        ledger = self.env["contact.center.group.delivery.event"]
        if observed_at:
            ledger = ledger.with_context(
                contact_center_group_receipt_observed_at=observed_at
            )
        return ledger._record_receipt(
            message_binding=message_binding or self.message_binding,
            group_profile=group_profile or self.profile,
            participant=participant or self.participant,
            state=state,
            occurred_at=occurred_at or datetime.datetime(2026, 8, 24, 12, 0, 0),
            external_event_id=external_event_id or "receipt-%s" % uuid.uuid4(),
            protocol_address=address or self._address(),
        )

    def test_pn_lid_replays_converge_without_changing_aggregate_delivery(self):
        tracked_models = (
            "mail.guest",
            "res.partner",
            "mail.channel.member",
            "contact.center.identity",
            "contact.center.identity.alias",
            "contact.center.group.profile",
            "contact.center.group.participant",
            "contact.center.group.participant.alias",
        )
        counts_before = {
            model: self.env[model].sudo().search_count([]) for model in tracked_models
        }
        first = self._record(
            "delivered",
            occurred_at=datetime.datetime(2026, 8, 24, 12, 0, 0),
            observed_at=datetime.datetime(2026, 8, 24, 12, 5, 0),
            external_event_id="receipt-delivered-lid",
        )
        replay = self._record(
            "delivered",
            address=self._address("whatsapp.pn"),
            occurred_at=datetime.datetime(2026, 8, 24, 11, 59, 0),
            observed_at=datetime.datetime(2026, 8, 24, 12, 6, 0),
            external_event_id="receipt-delivered-pn",
        )
        read = self._record(
            "read",
            observed_at=datetime.datetime(2026, 8, 24, 12, 7, 0),
            external_event_id="receipt-read-lid",
        )

        self.assertEqual(first, replay)
        self.assertNotEqual(first, read)
        self.assertEqual(
            self.env["contact.center.group.delivery.event"]
            .sudo()
            .search_count([("message_binding_id", "=", self.message_binding.id)]),
            2,
        )
        self.assertEqual(replay.occurred_at, datetime.datetime(2026, 8, 24, 11, 59, 0))
        self.assertEqual(
            replay.last_observed_at, datetime.datetime(2026, 8, 24, 12, 6, 0)
        )
        self.assertEqual(replay.external_event_id, "receipt-delivered-pn")
        self.assertEqual(
            replay.protocol_address_json["value_normalized"], self.participant_pn
        )
        self.assertEqual(
            self.env["contact.center.group.delivery.event"]._summary_by_binding(
                self.message_binding
            ),
            {
                self.message_binding.id: {
                    "delivered_count": 1,
                    "read_count": 1,
                }
            },
        )
        self.message_binding.invalidate_recordset(["delivery_state"])
        self.assertEqual(self.message_binding.delivery_state, "sent")
        self.assertEqual(
            {
                model: self.env[model].sudo().search_count([])
                for model in tracked_models
            },
            counts_before,
        )

    def test_read_before_delivered_is_commutative(self):
        read = self._record(
            "read",
            occurred_at=datetime.datetime(2026, 8, 24, 12, 2, 0),
            external_event_id="receipt-read-first",
        )
        self.assertEqual(
            self.env["contact.center.group.delivery.event"]._summary_by_binding(
                self.message_binding
            ),
            {
                self.message_binding.id: {
                    "delivered_count": 1,
                    "read_count": 1,
                }
            },
        )
        delivered = self._record(
            "delivered",
            occurred_at=datetime.datetime(2026, 8, 24, 12, 1, 0),
            external_event_id="receipt-delivered-second",
        )

        self.assertEqual({read.state, delivered.state}, {"delivered", "read"})
        self.assertEqual(read.participant_id, delivered.participant_id)
        self.assertEqual(
            self.env["contact.center.group.delivery.event"]._summary_by_binding(
                self.message_binding
            ),
            {
                self.message_binding.id: {
                    "delivered_count": 1,
                    "read_count": 1,
                }
            },
        )
        self.message_binding.invalidate_recordset(["delivery_state"])
        self.assertEqual(self.message_binding.delivery_state, "sent")

    def test_scope_and_protocol_address_fail_closed(self):
        unknown = self._address("whatsapp.lid", "299999999999999@lid")
        with self.assertRaisesRegex(ValidationError, "does not belong"):
            with self.env.cr.savepoint():
                self._record("delivered", address=unknown)

        invalid_role = AddressDTO(
            namespace="whatsapp.lid",
            value=self.participant_lid,
            value_normalized=self.participant_lid,
            role="alternate",
            source_field="event.SenderAlt",
            confidence="protocol",
        )
        with self.assertRaisesRegex(ValidationError, "protocol-observed sender"):
            with self.env.cr.savepoint():
                self._record("delivered", address=invalid_role)

        inbound_message = self.channel.sudo()._contact_center_post(
            origin="inbound",
            body="Inbound fixture",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_id=self.account.technical_author_id.id,
            partner_ids=[],
        )
        inbound_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": inbound_message.id,
                    "channel_binding_id": self.channel_binding.id,
                    "provider_connection_id": self.connection.id,
                    "direction": "inbound",
                    "origin": "provider",
                    "content_type": "text",
                    "external_message_id": "GROUP-INBOUND-NO-RECEIPT",
                    "delivery_state": "delivered",
                }
            )
        )
        with self.assertRaisesRegex(ValidationError, "outbound group"):
            with self.env.cr.savepoint():
                self._record("delivered", message_binding=inbound_binding)

        self.assertFalse(
            self.env["contact.center.group.delivery.event"]
            .sudo()
            .search(
                [
                    (
                        "message_binding_id",
                        "in",
                        self.message_binding.ids + inbound_binding.ids,
                    )
                ]
            )
        )

    def test_protocol_address_is_provider_neutral_and_not_conversation_routing(self):
        telegram_address = self._address(
            "telegram.user", "telegram-user-group-receipt-99"
        )
        self.env["contact.center.group.participant.alias"].sudo().create(
            {
                "participant_id": self.participant.id,
                "namespace": telegram_address.namespace,
                "value_raw": telegram_address.value,
                "value_normalized": telegram_address.value_normalized,
                "role": "alternate",
                "source_field": "fixture.telegram.sender",
                "confidence": "protocol",
            }
        )

        event = self._record("delivered", address=telegram_address)

        self.assertEqual(
            event.protocol_address_json["namespace"], telegram_address.namespace
        )
        self.env["contact.center.channel.alias"].sudo().create(
            {
                "channel_binding_id": self.channel_binding.id,
                "account_id": self.account.id,
                "namespace": telegram_address.namespace,
                "value_raw": telegram_address.value,
                "value_normalized": telegram_address.value_normalized,
                "role": "routing",
                "source_field": "fixture.telegram.group",
                "confidence": "protocol",
            }
        )
        with self.assertRaisesRegex(ValidationError, "conversation address"):
            with self.env.cr.savepoint():
                self._record("read", address=telegram_address)

    def test_ledger_is_read_only_and_visible_only_to_administrators(self):
        event = self._record("delivered")
        ledger = self.env["contact.center.group.delivery.event"]

        with self.assertRaises(AccessError):
            ledger.with_user(self.agent).search([("id", "=", event.id)])
        self.assertEqual(
            ledger.with_user(self.admin).search([("id", "=", event.id)]), event
        )
        with self.assertRaises(AccessError):
            ledger.with_user(self.admin).create(
                {
                    "message_binding_id": self.message_binding.id,
                    "participant_id": self.participant.id,
                    "state": "read",
                    "occurred_at": datetime.datetime(2026, 8, 24, 12, 0, 0),
                    "last_observed_at": datetime.datetime(2026, 8, 24, 12, 1, 0),
                    "external_event_id": "forbidden-create",
                    "protocol_address_json": self._address().to_dict(),
                }
            )
