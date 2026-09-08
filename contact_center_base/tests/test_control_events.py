import hashlib
import uuid
from unittest import mock

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import SavepointCase

from ..services.adapter import AdapterResult, ProviderAdapter, adapter_registry
from ..services.dto import EventDTO


@adapter_registry.register("test.control.events")
class ControlEventTestAdapter(ProviderAdapter):
    display_name = "Control Event Test"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return EventDTO.from_dict(envelope)

    def execute_command(self, connection, command):
        return AdapterResult.success(provider_response={"accepted": True})

    def prepare_request_snapshot(self, connection, command):
        return {
            "provider": self.key,
            "method": "POST",
            "endpoint": "/fixture",
            "payload": {"command_id": command.command_id},
        }

    def get_capabilities(self, connection):
        return {
            "send_message": True,
            "mark_read": True,
            "react": True,
            "edit_message": True,
            "delete_message": True,
        }

    def get_health(self, connection):
        return {"state": "connected"}


class TestControlEvents(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Control Event Agent",
                    "login": "cc-control-%s" % uuid.uuid4(),
                    "email": "cc-control@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, agent_group.ids)],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Control Event Team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Control Event Account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "control-account-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, cls.team.ids)],
                "mark_read_enabled": True,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Control Event Connection",
                "account_id": cls.account.id,
                "adapter_key": "test.control.events",
                "external_ref": "control-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "control-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {
                    "send_message": True,
                    "mark_read": True,
                    "react": True,
                    "edit_message": True,
                    "delete_message": True,
                },
                "last_state_observed_at": fields.Datetime.now(),
                "last_state_source": "health_job",
                "health_detail": "healthy",
            }
        )
        cls.application = cls.env["contact.center.application"]

    def _address(self, jid, role):
        return {
            "namespace": "whatsapp.pn",
            "value": jid,
            "value_normalized": jid,
            "role": role,
            "confidence": "protocol",
            "resolution_scope": "company",
        }

    def _event_values(self, jid, event_type, *, event_id=None):
        return {
            "schema_version": 1,
            "provider_schema_version": self.connection.provider_schema_version,
            "event_id": event_id or "control-event-%s" % uuid.uuid4(),
            "event_type": event_type,
            "occurred_at": "2026-08-29T12:00:00Z",
            "account_ref": self.account.external_ref,
            "connection_ref": self.connection.external_ref,
            "conversation_ref": jid,
            "platform": self.account.platform,
            "direction": "inbound",
            "is_from_me": False,
            "origin": "provider",
            "actor": {
                "display_name": "Pessoa da chamada",
                "addresses": [self._address(jid, "sender")],
            },
            "conversation": {
                "conversation_type": "direct",
                "addresses": [self._address(jid, "primary")],
            },
        }

    def _call_event(
        self,
        jid,
        state,
        *,
        call_ref=None,
        direction="inbound",
        event_id=None,
        occurred_at=None,
    ):
        values = self._event_values(jid, "conversation.call.updated", event_id=event_id)
        values["occurred_at"] = occurred_at or values["occurred_at"]
        values["extensions"] = {
            "call": {
                "ref": call_ref or hashlib.sha256(jid.encode("utf-8")).hexdigest(),
                "state": state,
                "direction": direction,
            },
            "provider.fixture": {"version": 1},
        }
        return EventDTO.from_dict(values)

    def _identity_security_event(self, jid, *, event_id=None, implicit=False):
        values = self._event_values(jid, "identity.security.changed", event_id=event_id)
        values["extensions"] = {
            "identity_security": {
                "change_kind": "primary_device",
                "implicit": implicit,
            }
        }
        return EventDTO.from_dict(values)

    def _direct_conversation(self, jid, name="Known Person"):
        guest = self.env["mail.guest"].sudo().create({"name": name})
        identity = self.env["contact.center.identity"]._contact_center_create_managed(
            {
                "name": name,
                "company_id": self.env.company.id,
                "mail_guest_id": guest.id,
            }
        )
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            identity=identity,
            teams=self.team,
            partner_ids=self.agent.partner_id.ids,
            guest_ids=guest.ids,
        )
        binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": channel.id,
                    "account_id": self.account.id,
                    "identity_id": identity.id,
                    "conversation_type": "direct",
                    "conversation_ref": jid,
                }
            )
        )
        address = self._address(jid, "primary")
        self.env["contact.center.identity.alias"].sudo().create(
            {
                "identity_id": identity.id,
                "account_id": self.account.id,
                "namespace": address["namespace"],
                "value_raw": address["value"],
                "value_normalized": address["value_normalized"],
                "role": address["role"],
                "confidence": address["confidence"],
                "resolution_scope": address["resolution_scope"],
            }
        )
        self.env["contact.center.channel.alias"].sudo().create(
            {
                "channel_binding_id": binding.id,
                "account_id": self.account.id,
                "namespace": address["namespace"],
                "value_raw": address["value"],
                "value_normalized": address["value_normalized"],
                "role": address["role"],
                "confidence": address["confidence"],
            }
        )
        return identity, channel, binding

    def _binding_for_message(self, message):
        return (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", message.id)], limit=1)
        )

    def _real_inbound(self, channel, binding, external_id, *, date=None):
        guest = binding.identity_id.mail_guest_id
        message_values = {"date": date} if date else {}
        message = channel._contact_center_post(
            origin="inbound",
            body="Mensagem humana",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_guest_id=guest.id,
            partner_ids=[],
            **message_values,
        )
        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": binding.id,
                    "provider_connection_id": self.connection.id,
                    "direction": "inbound",
                    "origin": "provider",
                    "content_type": "text",
                    "external_message_id": external_id,
                    "delivery_state": "delivered",
                }
            )
        )
        return message, target

    def test_call_cards_are_idempotent_and_do_not_rewrite_out_of_order_states(self):
        jid = "5511900004101@s.whatsapp.net"
        call_ref = hashlib.sha256(b"one-provider-call").hexdigest()
        accepted = self._call_event(
            jid,
            "accepted",
            call_ref=call_ref,
            occurred_at="2026-08-29T12:01:00Z",
        )
        offered = self._call_event(
            jid,
            "offered",
            call_ref=call_ref,
            direction="outbound",
            occurred_at="2026-08-29T12:00:00Z",
        )
        terminated = self._call_event(
            jid,
            "terminated",
            call_ref=call_ref,
            occurred_at="2026-08-29T12:02:00Z",
        )

        accepted_message = self.application._process_event(self.connection, accepted)
        offered_message = self.application._process_event(self.connection, offered)
        terminated_message = self.application._process_event(
            self.connection, terminated
        )
        replay = self.application._process_event(self.connection, offered)

        self.assertEqual(replay, offered_message)
        bindings = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    (
                        "channel_binding_id",
                        "=",
                        self._binding_for_message(
                            offered_message
                        ).channel_binding_id.id,
                    ),
                    ("content_type", "like", "call.%"),
                ]
            )
        )
        self.assertEqual(len(bindings), 3)
        self.assertEqual(
            set(bindings.mapped("content_type")),
            {"call.offer", "call.accept", "call.terminate"},
        )
        self.assertIn("realizada", offered_message.body)
        self.assertIn("atendida", accepted_message.body)
        self.assertIn("encerrada", terminated_message.body)
        self.assertEqual(
            set(bindings.mapped("external_message_id")),
            {
                "control:call:%s:offered" % call_ref,
                "control:call:%s:accepted" % call_ref,
                "control:call:%s:terminated" % call_ref,
            },
        )

    def test_call_unknown_conversation_uses_normal_direct_identity_resolution(self):
        jid = "5511900004102@s.whatsapp.net"

        with mock.patch.object(
            type(self.application), "_notify_ui", autospec=True
        ) as notify_ui:
            message = self.application._process_event(
                self.connection, self._call_event(jid, "offered")
            )

        message_created_payloads = [
            call.args[3]
            for call in notify_ui.call_args_list
            if call.args[2] == "message_created"
        ]
        self.assertEqual(
            message_created_payloads,
            [{"message_id": message.id, "direction": "inbound"}],
        )

        binding = self._binding_for_message(message).channel_binding_id
        self.assertEqual(binding.conversation_type, "direct")
        self.assertTrue(binding.identity_id)
        self.assertTrue(binding.identity_id.mail_guest_id)
        self.assertEqual(binding.channel_id.channel_type, "contact_center")

    def test_identity_security_is_existing_only_idempotent_and_does_not_enrich(self):
        unknown_jid = "5511900004103@s.whatsapp.net"
        counts = {
            model: self.env[model].sudo().search_count([])
            for model in (
                "contact.center.identity",
                "mail.guest",
                "mail.channel",
                "contact.center.channel.binding",
            )
        }

        missing = self.application._process_event(
            self.connection,
            self._identity_security_event(unknown_jid),
        )

        self.assertFalse(missing)
        self.assertEqual(
            counts,
            {model: self.env[model].sudo().search_count([]) for model in counts},
        )

        known_jid = "5511900004104@s.whatsapp.net"
        identity, _channel, binding = self._direct_conversation(known_jid)
        alias_count = len(identity.alias_ids)
        channel_alias_count = len(binding.alias_ids)
        event_id = "identity-security-stable-event"
        event = self._identity_security_event(
            known_jid, event_id=event_id, implicit=True
        )
        values = event.to_dict()
        values["actor"] = {
            "display_name": "This must not replace the identity",
            "addresses": [self._address(known_jid, "sender")],
        }
        event = EventDTO.from_dict(values)

        message = self.application._process_event(self.connection, event)
        replay = self.application._process_event(self.connection, event)

        identity.invalidate_recordset()
        self.assertEqual(replay, message)
        self.assertEqual(identity.name, "Known Person")
        self.assertFalse(identity.observed_name)
        self.assertEqual(len(identity.alias_ids), alias_count)
        self.assertEqual(len(binding.alias_ids), channel_alias_count)
        message_binding = self._binding_for_message(message)
        self.assertEqual(message_binding.channel_binding_id, binding)
        self.assertEqual(message_binding.content_type, "identity.security.changed")
        self.assertIn("possível alteração automática", message.body)
        self.assertEqual(
            message_binding.external_message_id,
            "control:identity-security:%s"
            % hashlib.sha256(event_id.encode("utf-8")).hexdigest(),
        )

    def test_control_event_shapes_are_strict(self):
        jid = "5511900004105@s.whatsapp.net"
        valid_call = self._call_event(jid, "offered").to_dict()
        invalid_cases = []
        with_message = dict(valid_call)
        with_message["message"] = {
            "external_message_id": "not-allowed",
            "content_type": "text",
            "text": "not allowed",
        }
        invalid_cases.append(with_message)
        extra_extension = dict(valid_call)
        extra_extension["extensions"] = dict(valid_call["extensions"], raw={})
        invalid_cases.append(extra_extension)
        bad_call = dict(valid_call)
        bad_call["extensions"] = {
            "call": {
                "ref": "raw-provider-id",
                "state": "offered",
                "direction": "inbound",
            }
        }
        invalid_cases.append(bad_call)
        bad_direction = dict(valid_call)
        bad_direction["direction"] = "outbound"
        invalid_cases.append(bad_direction)
        mismatched_actor = dict(valid_call)
        mismatched_actor["actor"] = {
            "display_name": "Wrong person",
            "addresses": [self._address("5511900099998@s.whatsapp.net", "sender")],
        }
        invalid_cases.append(mismatched_actor)

        for values in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    self.application._process_event(
                        self.connection, EventDTO.from_dict(values)
                    )

        identity_event = self._identity_security_event(jid).to_dict()
        identity_event["extensions"]["identity_security"]["extra"] = True
        with self.assertRaises(ValidationError):
            self.application._process_event(
                self.connection, EventDTO.from_dict(identity_event)
            )

        for missing_field in ("actor", "conversation"):
            identity_event = self._identity_security_event(jid).to_dict()
            if missing_field == "actor":
                identity_event["actor"]["addresses"] = []
            else:
                identity_event["conversation"]["addresses"][0]["role"] = "alternate"
            with self.subTest(missing_field=missing_field):
                with self.assertRaises(ValidationError):
                    self.application._process_event(
                        self.connection, EventDTO.from_dict(identity_event)
                    )

        identity_event = self._identity_security_event(jid).to_dict()
        identity_event["actor"]["addresses"] = [
            self._address("5511900099997@s.whatsapp.net", "sender")
        ]
        with self.assertRaises(ValidationError):
            self.application._process_event(
                self.connection, EventDTO.from_dict(identity_event)
            )

    def test_control_cards_have_no_ui_actions_and_reject_api_targets(self):
        jid = "5511900004106@s.whatsapp.net"
        message = self.application._process_event(
            self.connection, self._call_event(jid, "offered")
        )
        target = self._binding_for_message(message)
        ui = self.env["contact.center.ui.api"].with_user(self.agent)

        serialized = ui._serialize_message(message, target)

        self.assertEqual(
            serialized["actions"],
            {
                "reply": False,
                "react": False,
                "edit": False,
                "delete": False,
                "resend": False,
            },
        )
        with self.assertRaises(UserError):
            self.application._outbound_reply_binding(
                target.channel_binding_id, self.connection, message.id
            )
        with self.assertRaises(UserError):
            self.application._validate_outbound_mutation(
                target, "react", "👍", "add", ""
            )

    def test_mark_read_skips_a_later_control_card(self):
        jid = "5511900004107@s.whatsapp.net"
        _identity, channel, binding = self._direct_conversation(jid)
        human_message, human_target = self._real_inbound(
            channel, binding, "human-before-call", date="2026-08-29 11:59:59"
        )
        control_message = self.application._process_event(
            self.connection, self._call_event(jid, "offered")
        )
        self.assertLess(human_message.date, control_message.date)

        outbox = self.application._queue_direct_mark_read(channel, control_message)

        self.assertTrue(outbox)
        self.assertEqual(outbox.target_message_binding_id, human_target)
        self.assertEqual(
            outbox.command_json["options"]["external_message_ids"],
            ["human-before-call"],
        )
        control_target = self._binding_for_message(control_message)
        with self.assertRaises(ValidationError):
            outbox.write({"target_message_binding_id": control_target.id})
