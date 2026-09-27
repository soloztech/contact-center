"""Shared fixture for the conversation lifecycle and attendance tests.

Lifecycle events take their processing time from a controllable clock; message
provider times and delivery times are explicit. Nothing depends on the wall clock
ordering, which may step backwards on development machines.
"""

import datetime
import uuid
from unittest import mock

from odoo import fields
from odoo.tests.common import SavepointCase

from ..services.adapter import adapter_registry
from ..services.dto import AddressDTO, DirectAddressResult, EventDTO
from .test_contact_center import FakeAdapter

# A day a week before today (L09-T1): posts dated by the wall clock (agent and
# automation messages) always sort after the fixture messages, as in production,
# and a report period around DAY and today stays far below the one-year limit
# whatever day the suite runs.
DAY = fields.Date.today() - datetime.timedelta(days=7)


@adapter_registry.register("test.attendance")
class AttendanceTestAdapter(FakeAdapter):
    display_name = "Attendance Test"

    def supports_direct_conversation_start(self, connection):
        return True

    def resolve_direct_address(self, connection, normalized_phone):
        jid = normalized_phone + "@s.whatsapp.net"
        return DirectAddressResult(
            state="ready",
            conversation_ref=jid,
            addresses=(
                AddressDTO(
                    namespace="whatsapp.pn",
                    value=jid,
                    value_normalized=jid,
                    confidence="protocol",
                    resolution_scope="company",
                ),
            ),
        )


def at(hour, minute=0, second=0, day=DAY):
    return datetime.datetime.combine(day, datetime.time(hour, minute, second))


class AttendanceCase(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.country_id = cls.env.ref("base.br")
        cls.agent_a = cls._user("Ana", "group_contact_center_agent")
        cls.agent_b = cls._user("Bruno", "group_contact_center_agent")
        cls.supervisor = cls._user("Sofia", "group_contact_center_supervisor")
        cls.admin = cls._user("Adm", "group_contact_center_admin")
        cls.outsider = cls._user("Otto", "group_contact_center_supervisor")
        cls.technical_author = cls.env["res.partner"].create(
            {"name": "Attendance phone"}
        )
        cls.account = cls._account("Atendimento")
        cls.connection = cls._connection(cls.account)
        cls.reason = (
            cls.env["contact.center.resolution.reason"]
            .with_user(cls.supervisor)
            .create({"name": "Atendimento concluído %s" % uuid.uuid4().hex[:6]})
        )
        cls.event_model = cls.env["contact.center.conversation.event"]
        cls.application = cls.env["contact.center.application"]

    @classmethod
    def _user(cls, name, group):
        token = uuid.uuid4().hex
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Attendance %s" % name,
                    "login": "attendance-%s-%s" % (name.lower(), token),
                    "email": "attendance-%s@example.invalid" % token,
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [
                        (6, 0, cls.env.ref("contact_center_base.%s" % group).ids)
                    ],
                }
            )
        )

    @classmethod
    def _account(cls, name, **values):
        return cls.env["contact.center.account"].create(
            dict(
                {
                    "name": "%s %s" % (name, uuid.uuid4().hex[:6]),
                    "company_id": cls.env.company.id,
                    "platform": "whatsapp",
                    "external_ref": "attendance-%s" % uuid.uuid4(),
                    "access_user_ids": [
                        (6, 0, (cls.agent_a | cls.agent_b | cls.supervisor).ids)
                    ],
                    "technical_author_id": cls.technical_author.id,
                },
                **values
            )
        )

    @classmethod
    def _connection(cls, account):
        return cls.env["contact.center.provider.connection"].create(
            {
                "name": "Attendance provider %s" % uuid.uuid4().hex[:6],
                "account_id": account.id,
                "adapter_key": "test.attendance",
                "external_ref": "attendance-provider-%s" % uuid.uuid4(),
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
        self.clock = [at(8)]
        patcher = mock.patch.object(
            type(self.event_model),
            "_contact_center_lifecycle_now",
            lambda model: self.clock[0],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # Clock and messages -------------------------------------------------------

    def tick(self, hour, minute=0, second=0):
        self.clock[0] = at(hour, minute, second)
        return self.clock[0]

    @staticmethod
    def remote():
        return "5511%s@s.whatsapp.net" % str(uuid.uuid4().int)[:9]

    def _address(self, remote, role="primary"):
        return {
            "namespace": "whatsapp.pn",
            "value": remote,
            "value_normalized": remote,
            "role": role,
        }

    def _event(self, remote, sent_at, *, connection=None, **message):
        connection = connection or self.connection
        key = message.pop("key", None) or uuid.uuid4().hex
        return EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "attendance-event-%s" % key,
                "event_type": "message.created",
                "occurred_at": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "account_ref": connection.account_id.external_ref,
                "connection_ref": connection.external_ref,
                "conversation_ref": remote,
                "platform": connection.account_id.platform,
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {
                    "display_name": "Cliente",
                    "addresses": [self._address(remote)],
                },
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [self._address(remote)],
                },
                "message": dict(
                    {
                        "external_message_id": "attendance-message-%s" % key,
                        "content_type": "text",
                        "text": "Mensagem do cliente",
                    },
                    **message
                ),
            }
        )

    def _binding_of(self, message):
        return (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", message.id)], limit=1)
        )

    def inbound(self, remote, sent_at, *, connection=None, **message):
        """Process one customer message whose provider time is ``sent_at``."""

        event = self._event(remote, sent_at, connection=connection, **message)
        result = self.application.with_context(
            contact_center_skip_enqueue=True
        )._process_event(connection or self.connection, event)
        return self._binding_of(result)

    def phone_first(self, remote, sent_at):
        """A reply written on the phone to a contact without a conversation yet."""

        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "attendance-phone-first-%s" % uuid.uuid4(),
                "event_type": "message.created",
                "occurred_at": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": remote,
                "platform": self.account.platform,
                "direction": "outbound",
                "is_from_me": True,
                "origin": "external_device",
                "actor": {"display_name": "Own WhatsApp account", "addresses": []},
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [self._address(remote)],
                },
                "message": {
                    "external_message_id": "attendance-phone-first-%s" % uuid.uuid4(),
                    "client_message_id": "",
                    "content_type": "text",
                    "text": "Conversa iniciada pelo celular",
                },
            }
        )
        result = self.application.with_context(
            contact_center_skip_enqueue=True
        )._process_event(self.connection, event)
        return self._binding_of(result)

    def phone(self, channel, sent_at, key=None):
        """Process a reply written on the phone (``external_device``)."""

        binding = self._channel_binding(channel)
        alias = binding.alias_ids[:1]
        key = key or uuid.uuid4().hex
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "attendance-phone-%s" % key,
                "event_type": "message.created",
                "occurred_at": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": binding.conversation_ref,
                "platform": self.account.platform,
                "direction": "outbound",
                "is_from_me": True,
                "origin": "provider",
                "actor": {"addresses": []},
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [
                        {
                            "namespace": alias.namespace,
                            "value": alias.value_raw,
                            "value_normalized": alias.value_normalized,
                            "role": alias.role,
                        }
                    ],
                },
                "message": {
                    "external_message_id": "attendance-phone-message-%s" % key,
                    "content_type": "text",
                    "text": "Resposta pelo celular",
                    "protocol_snapshot": {"from_me": True},
                },
            }
        )
        result = self.application.with_context(
            contact_center_skip_enqueue=True
        )._process_event(self.connection, event)
        return self._binding_of(result)

    def send(self, channel, user=None, body="Resposta do agente"):
        result = (
            self.env["contact.center.ui.api"]
            .with_user(user or self.agent_a)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(channel.id, body, client_request_id=str(uuid.uuid4()))
        )
        return (
            self.env["contact.center.message.binding"]
            .sudo()
            .browse(
                self.env["contact.center.outbox.command"]
                .sudo()
                .browse(result["outbox_command_id"])
                .message_binding_id.id
            )
        )

    def automation(self, channel, user=None):
        result = (
            self.env["contact.center.ui.api"]
            .with_user(user or self.agent_a)
            .with_context(contact_center_skip_enqueue=True)
            ._send_automation_message(
                channel.id, "Mensagem automática", client_request_id=str(uuid.uuid4())
            )
        )
        return (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
            .message_binding_id
        )

    def deliver(self, binding, delivered_at, state="sent"):
        binding.sudo()._contact_center_apply_delivery(state, occurred_at=delivered_at)
        return binding

    # Conversation operations --------------------------------------------------

    def api(self, user=None):
        return self.env["contact.center.ui.api"].with_user(user or self.agent_a)

    def channel_of(self, binding):
        return binding.sudo().channel_binding_id.channel_id

    def _channel_binding(self, channel):
        return (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search([("channel_id", "=", channel.id)], limit=1)
        )

    def claim(self, channel, user=None):
        return self.api(user).claim_conversation(channel.id)

    def assign(self, channel, user):
        return self.api(self.supervisor).update_conversation(
            channel.id, {"responsible_id": user.id if user else False}
        )

    def set_state(self, channel, state, user=None):
        return self.api(user).update_conversation(channel.id, {"state": state})

    def events(self, channel):
        return self.event_model.sudo().search(
            [("channel_id", "=", channel.id)], order="sequence"
        )

    def counter(self, channel):
        channel.invalidate_recordset(["contact_center_lifecycle_seq"])
        return channel.sudo().contact_center_lifecycle_seq

    def episodes(self, channel):
        self.env.flush_all()
        model = self.env["contact.center.attendance.episode"].sudo()
        model.invalidate_model()
        return model.search([("channel_id", "in", channel.ids)], order="start_at, id")

    def placements(self, channel):
        self.env.flush_all()
        model = self.env["contact.center.attendance.message"].sudo()
        model.invalidate_model()
        return model.search([("channel_id", "=", channel.id)], order="id")
