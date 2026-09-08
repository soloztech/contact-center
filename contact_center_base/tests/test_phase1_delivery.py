import datetime
import uuid
from unittest import mock

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import SavepointCase
from odoo.tools import mute_logger

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.job import Job

from ..services.adapter import (
    ProviderAdapter,
    TransientAdapterError,
    UnsupportedEventError,
    adapter_registry,
)
from ..services.dto import AdapterResult, CommandDTO, EventDTO
from ..services.job import QUEUE_ATTEMPT_CEILING
from .test_structured_content import outbound_specs


@adapter_registry.register("test.phase1.delivery")
class Phase1DeliveryAdapter(ProviderAdapter):
    display_name = "Test Phase 1 Delivery"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return EventDTO.from_dict(envelope)

    def execute_command(self, connection, command):
        return AdapterResult.success(
            external_message_id=command.client_message_id,
            provider_response={"accepted": True},
        )

    def prepare_request_snapshot(self, connection, command):
        return {
            "provider": self.key,
            "method": "POST",
            "endpoint": "/messages",
            "payload": {"client_message_id": command.client_message_id},
        }

    def get_capabilities(self, connection):
        return {"send_message": True}

    def get_health(self, connection):
        return {"state": "connected"}

    def derive_client_message_id(self, command_id):
        return "phase1-%s" % command_id


class TestPhase1Delivery(SavepointCase):
    def test_structured_send_is_persisted_and_idempotency_compares_options(self):
        channel, _binding, _identity = self._channel_binding()
        card = {
            "type": "buttons",
            "buttons": [{"type": "reply", "id": "yes", "title": "Sim"}],
        }
        self.connection.capabilities_json = {
            "send_message": True,
            "outbound_structured_content": outbound_specs("buttons"),
        }
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        request_id = str(uuid.uuid4())
        with mock.patch.object(
            Phase1DeliveryAdapter,
            "validate_outbound_structured_content",
            return_value=True,
        ):
            result = api.send_message(
                channel.id,
                "Confirmar?",
                client_request_id=request_id,
                structured_content=card,
            )
            replay = api.send_message(
                channel.id,
                "Confirmar?",
                client_request_id=request_id,
                structured_content=card,
            )
            self.assertEqual(result["message_id"], replay["message_id"])
            self.assertEqual(result["message"]["structured_content"], card)
            outbox = self.env["contact.center.outbox.command"].browse(
                result["outbox_command_id"]
            )
            self.assertEqual(outbox.message_binding_id.structured_content_json, card)
            self.assertTrue(
                outbox._validate_command_scope(
                    CommandDTO.from_dict(outbox.command_json)
                )
            )
            changed = {
                "type": "buttons",
                "buttons": [{"type": "reply", "id": "no", "title": "Não"}],
            }
            with self.assertRaises(ValidationError):
                api.send_message(
                    channel.id,
                    "Confirmar?",
                    client_request_id=request_id,
                    structured_content=changed,
                )
            command = dict(outbox.command_json)
            command["message"] = dict(command["message"], structured_content=changed)
            with self.assertRaises(ValidationError):
                outbox._validate_command_scope(CommandDTO.from_dict(command))
            self.connection.capabilities_json = {"send_message": True}
            with self.assertRaises(ValidationError):
                outbox._validate_command_scope(
                    CommandDTO.from_dict(outbox.command_json)
                )

    def test_structured_safe_retry_keeps_card_and_creates_only_one_attempt(self):
        channel, _binding, _identity = self._channel_binding()
        card = {
            "type": "contacts",
            "contacts": [{"name": "Ana", "phones": ["+5511999999999"]}],
        }
        self.connection.capabilities_json = {
            "send_message": True,
            "outbound_structured_content": outbound_specs("contacts"),
        }
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        with mock.patch.object(
            Phase1DeliveryAdapter,
            "validate_outbound_structured_content",
            return_value=True,
        ):
            result = api.send_message(channel.id, "", structured_content=card)
            source = self.env["contact.center.outbox.command"].browse(
                result["outbox_command_id"]
            )
            source._finish_failure("dead", ValidationError("Rejected by provider"))
            retried = api.resend_message(
                channel.id, result["message_id"], str(uuid.uuid4())
            )
            replay = api.resend_message(
                channel.id, result["message_id"], str(uuid.uuid4())
            )
            self.assertEqual(retried["message_id"], replay["message_id"])
            retry = self.env["contact.center.outbox.command"].browse(
                retried["outbox_command_id"]
            )
            self.assertEqual(retry.retry_of_id, source)
            self.assertEqual(retry.message_binding_id.structured_content_json, card)
            self.assertEqual(retry.command_json["message"]["structured_content"], card)
            self.assertEqual(source.state, "dead")
            self.assertTrue(
                retry._validate_command_scope(CommandDTO.from_dict(retry.command_json))
            )

    def test_structured_send_rejects_unsupported_before_creating_message(self):
        channel, _binding, _identity = self._channel_binding()
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        count = self.env["mail.message"].search_count([])
        card = {"type": "location", "latitude": -23.5, "longitude": -46.6}
        with self.assertRaises(UserError):
            api.send_message(channel.id, "", structured_content=card)
        self.assertEqual(self.env["mail.message"].search_count([]), count)
        self.connection.capabilities_json = {
            "send_message": True,
            "outbound_structured_content": outbound_specs("location"),
        }
        with self.assertRaises(ValidationError):
            api.send_message(
                channel.id, "caption cannot be sent", structured_content=card
            )
        with self.assertRaises(ValidationError):
            api.send_message(
                channel.id, "", media_refs=[str(uuid.uuid4())], structured_content=card
            )

    def test_contact_card_has_preview_and_no_signature(self):
        channel, _binding, _identity = self._channel_binding()
        card = {
            "type": "contacts",
            "contacts": [{"name": "Ana", "phones": ["+5511999999999"]}],
        }
        self.account.outbound_signature_enabled = True
        self.connection.capabilities_json = {
            "send_message": True,
            "outbound_structured_content": outbound_specs("contacts"),
        }
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        with mock.patch.object(
            Phase1DeliveryAdapter,
            "validate_outbound_structured_content",
            return_value=True,
        ):
            result = api.send_message(channel.id, "", structured_content=card)
        self.assertEqual(result["message"]["body_text"], "Ana")
        outbox = self.env["contact.center.outbox.command"].browse(
            result["outbox_command_id"]
        )
        self.assertFalse(outbox.command_json["options"].get("sender_signature"))
        self.assertEqual(outbox.command_json["message"]["text"], "")

    def test_structured_provider_limits_apply_before_admission_and_again_in_queue(self):
        channel, _binding, _identity = self._channel_binding()
        specs = outbound_specs("buttons")
        specs["buttons"].update(
            action_types=["reply"],
            max_buttons=5,
            max_button_title_length=40,
            max_body_length=2048,
        )
        self.connection.capabilities_json = {
            "send_message": True,
            "outbound_structured_content": specs,
        }
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        forbidden = {
            "type": "buttons",
            "buttons": [{"type": "url", "url": "https://example.com", "title": "Open"}],
        }
        count = self.env["mail.message"].search_count([])
        with mock.patch.object(
            Phase1DeliveryAdapter,
            "validate_outbound_structured_content",
            return_value=True,
        ):
            with self.assertRaises(ValidationError):
                api.send_message(channel.id, "Choose", structured_content=forbidden)
            self.assertEqual(self.env["mail.message"].search_count([]), count)
            card = {
                "type": "buttons",
                "buttons": [
                    {"type": "reply", "id": str(i), "title": "A" * 40} for i in range(5)
                ],
            }
            result = api.send_message(channel.id, "B" * 1500, structured_content=card)
            outbox = self.env["contact.center.outbox.command"].browse(
                result["outbox_command_id"]
            )
            command = CommandDTO.from_dict(outbox.command_json)
            self.assertTrue(outbox._validate_command_scope(command))
            specs["buttons"]["max_buttons"] = 3
            self.connection.capabilities_json = {
                "send_message": True,
                "outbound_structured_content": specs,
            }
            with self.assertRaises(ValidationError):
                outbox._validate_command_scope(command)

    def test_contact_body_is_real_payload_and_preview_does_not_change_replay_or_retry(
        self,
    ):
        channel, _binding, _identity = self._channel_binding()
        specs = outbound_specs("contacts")
        specs["contacts"].update(body_mode="optional", max_body_length=200)
        self.connection.capabilities_json = {
            "send_message": True,
            "outbound_structured_content": specs,
        }
        self.account.outbound_signature_enabled = True
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        card = {"type": "contacts", "contacts": [{"name": "Ana"}]}
        with mock.patch.object(
            Phase1DeliveryAdapter,
            "validate_outbound_structured_content",
            return_value=True,
        ):
            for body in ("", "Contact for tomorrow"):
                with self.subTest(body=body):
                    request_id = str(uuid.uuid4())
                    result = api.send_message(
                        channel.id,
                        body,
                        client_request_id=request_id,
                        structured_content=card,
                    )
                    replay = api.send_message(
                        channel.id,
                        body,
                        client_request_id=request_id,
                        structured_content=card,
                    )
                    self.assertEqual(result["message_id"], replay["message_id"])
                    self.assertEqual(result["message"]["body_text"], body or "Ana")
                    with self.assertRaises(ValidationError):
                        api.send_message(
                            channel.id,
                            "Changed",
                            client_request_id=request_id,
                            structured_content=card,
                        )
                    source = self.env["contact.center.outbox.command"].browse(
                        result["outbox_command_id"]
                    )
                    self.assertEqual(source.command_json["message"]["text"], body)
                    self.assertFalse(
                        source.command_json["options"].get("sender_signature")
                    )
                    source._finish_failure("dead", ValidationError("Provider refusal"))
                    retried = api.resend_message(
                        channel.id, result["message_id"], str(uuid.uuid4())
                    )
                    retry = self.env["contact.center.outbox.command"].browse(
                        retried["outbox_command_id"]
                    )
                    self.assertEqual(retry.command_json["message"]["text"], body)
                    self.assertEqual(retried["message"]["body_text"], body or "Ana")
                    retry._process_one()
                    self.assertEqual(retry.state, "done")

    def test_malformed_card_capabilities_are_hidden_and_rejected_server_side(self):
        channel, _binding, _identity = self._channel_binding()
        self.connection.capabilities_json = {
            "send_message": True,
            "outbound_structured_content": {"location": {"allow_live": True}},
        }
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        capabilities = api.get_conversation(channel.id)["item"]["capabilities"]
        self.assertEqual(capabilities["outbound_structured_content"], {})
        self.assertNotIn("structured_content", capabilities)
        card = {"type": "location", "latitude": 1, "longitude": 2}
        with mock.patch.object(
            Phase1DeliveryAdapter,
            "validate_outbound_structured_content",
            return_value=True,
        ), self.assertRaises(ValidationError):
            api.send_message(channel.id, "", structured_content=card)

    def test_external_device_card_is_projected_once_and_redacted_on_delete(self):
        _channel, binding, _identity = self._channel_binding()
        event = self._from_me_event(
            binding, "rich-card-1", text="Shared contact"
        ).to_dict()
        card = {
            "type": "contacts",
            "contacts": [{"name": "Ana", "phones": ["+5511999999999"]}],
        }
        event["message"].update(content_type="contacts", structured_content=card)
        application = self.env["contact.center.application"]
        event = EventDTO.from_dict(event)
        application._process_event(self.connection, event)
        application._process_event(self.connection, event)
        projected = self.env["contact.center.message.binding"].search(
            [
                ("channel_binding_id", "=", binding.id),
                ("external_message_id", "=", "rich-card-1"),
            ]
        )
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected.structured_content_json, card)
        projected.write({"message_state": "deleted", "deleted_display_mode": "redact"})
        serialized = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._serialize_message(projected.message_id, projected)
        )
        self.assertEqual(serialized["structured_content"], {})

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Phase 1 Delivery Agent",
                    "login": "cc-phase1-delivery-%s" % uuid.uuid4(),
                    "email": "cc-phase1-delivery@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, agent_group.ids)],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Phase 1 Delivery Team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.technical_author = cls.env["res.partner"].create(
            {"name": "Phase 1 External Device"}
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Phase 1 WhatsApp Account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "phase1-account-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, cls.team.ids)],
                "technical_author_id": cls.technical_author.id,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Phase 1 Fake Connection",
                "account_id": cls.account.id,
                "adapter_key": "test.phase1.delivery",
                "external_ref": "phase1-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "phase1-fixture-v1",
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

    def _channel_binding(self):
        suffix = str(uuid.uuid4())
        guest = self.env["mail.guest"].sudo().create({"name": "Phase 1 Remote Person"})
        identity = (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": "Phase 1 Remote Person",
                    "company_id": self.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
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
                    "conversation_ref": "phase1-conversation-%s" % suffix,
                }
            )
        )
        self.env["contact.center.channel.alias"].sudo().create(
            {
                "channel_binding_id": binding.id,
                "account_id": self.account.id,
                "namespace": "whatsapp.pn",
                "value_raw": "%s@s.whatsapp.net" % suffix,
                "value_normalized": "%s@s.whatsapp.net" % suffix,
                "role": "primary",
            }
        )
        return channel, binding, identity

    def _send_message(self, channel, body="Phase 1 outbound"):
        return (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(channel.id, body)
        )

    def _from_me_event(
        self,
        binding,
        external_message_id,
        *,
        client_message_id="",
        event_id=None,
        text="Sent outside Odoo",
    ):
        alias = binding.alias_ids[0]
        return EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": self.connection.provider_schema_version,
                "event_id": event_id or "from-me-%s" % uuid.uuid4(),
                "event_type": "message.created",
                "occurred_at": "2026-08-21T16:00:00Z",
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
                    "external_message_id": external_message_id,
                    "client_message_id": client_message_id,
                    "content_type": "text",
                    "text": text,
                    "protocol_snapshot": {
                        "chat_address": alias.value_raw,
                        "from_me": True,
                    },
                },
            }
        )

    def _unknown_direct_from_me_event(self, suffix=None):
        suffix = suffix or uuid.uuid4().hex[:18]
        lid = "%s@lid" % suffix
        pn = "55%s@s.whatsapp.net" % suffix
        return EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": self.connection.provider_schema_version,
                "event_id": "unknown-from-me-%s" % uuid.uuid4(),
                "event_type": "message.created",
                "occurred_at": "2026-08-21T15:59:00Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": lid,
                "platform": self.account.platform,
                "direction": "outbound",
                "is_from_me": True,
                "origin": "external_device",
                "actor": {"display_name": "Own WhatsApp account", "addresses": []},
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [
                        {
                            "namespace": "whatsapp.lid",
                            "value": lid,
                            "value_normalized": lid,
                            "role": "primary",
                            "confidence": "protocol",
                        },
                        {
                            "namespace": "whatsapp.pn",
                            "value": pn,
                            "value_normalized": pn,
                            "role": "alternate",
                            "confidence": "protocol",
                        },
                    ],
                },
                "message": {
                    "external_message_id": "external-device-%s" % uuid.uuid4(),
                    "client_message_id": "",
                    "content_type": "text",
                    "text": "Conversation started from the external device",
                },
            }
        )

    def _inbound_reply_event(self, target_external_id):
        values = self._unknown_direct_from_me_event().to_dict()
        values.update(
            direction="inbound",
            is_from_me=False,
            origin="provider",
            actor={
                "display_name": "Remote reply author",
                "addresses": values["conversation"]["addresses"],
            },
            reply_to={"external_message_id": target_external_id},
        )
        values["message"].update(
            text="Human content with a quoted message",
            reply_to_external_id=target_external_id,
            protocol_snapshot={"reply_to": {"external_message_id": target_external_id}},
        )
        return EventDTO.from_dict(values)

    def _receipt_event(
        self,
        binding,
        external_message_id,
        state,
        *,
        event_id=None,
        occurred_at="2026-08-21T16:01:00Z",
        is_from_me=False,
    ):
        event_id = event_id or "receipt-%s" % uuid.uuid4()
        return EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": self.connection.provider_schema_version,
                "event_id": event_id,
                "event_type": "delivery.updated",
                "occurred_at": occurred_at,
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": binding.conversation_ref,
                "platform": self.account.platform,
                "direction": "inbound" if is_from_me else "outbound",
                "is_from_me": is_from_me,
                "origin": "provider",
                "actor": {"addresses": []},
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [],
                },
                "delivery": {
                    "state": state,
                    "external_message_ids": [external_message_id],
                    "external_event_id": event_id,
                },
            }
        )

    def _watermark_receipt_event(
        self,
        binding,
        state,
        watermark,
        *,
        event_id=None,
        occurred_at="2026-08-21T16:01:00Z",
    ):
        values = self._receipt_event(
            binding,
            "watermark-placeholder",
            state,
            event_id=event_id,
            occurred_at=occurred_at,
        ).to_dict()
        values["delivery"].pop("external_message_ids")
        values["delivery"]["watermark"] = watermark
        return EventDTO.from_dict(values)

    def _watermark_message(
        self,
        channel,
        channel_binding,
        external_message_id,
        message_date,
        *,
        connection=None,
    ):
        connection = connection or self.connection
        message = channel.sudo()._contact_center_post(
            origin="outbound",
            body="Watermark receipt target",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            date=fields.Datetime.to_datetime(message_date),
            partner_ids=[],
        )
        return (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": channel_binding.id,
                    "provider_connection_id": connection.id,
                    "direction": "outbound",
                    "origin": "agent",
                    "content_type": "text",
                    "external_message_id": external_message_id,
                    "client_message_id": "watermark-%s" % uuid.uuid4(),
                    "delivery_state": "sent",
                }
            )
        )

    @staticmethod
    def _epoch_milliseconds(value):
        observed_at = fields.Datetime.to_datetime(value).replace(
            tzinfo=datetime.timezone.utc
        )
        return int(observed_at.timestamp() * 1000)

    def _mutation_event(self, binding, target_external_message_id):
        alias = binding.alias_ids[0]
        return EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": self.connection.provider_schema_version,
                "event_id": "early-mutation-%s" % uuid.uuid4(),
                "event_type": "message.edited",
                "occurred_at": "2026-08-21T16:01:00Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": binding.conversation_ref,
                "platform": self.account.platform,
                "direction": "inbound",
                "is_from_me": False,
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
                "mutation": {
                    "type": "edit",
                    "target_external_message_id": target_external_message_id,
                    "new_text": "Edited before the original arrived",
                },
            }
        )

    def _inbox_event(self, event):
        return (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": event.event_id,
                    "provider_schema_version": self.connection.provider_schema_version,
                    "raw_envelope_json": event.to_dict(),
                }
            )
        )

    def _run_inbox_job(self, inbox, job_uuid=None):
        job_uuid = job_uuid or inbox.queue_job_uuid or str(uuid.uuid4())
        if not inbox.queue_job_uuid:
            inbox.sudo().write({"queue_job_uuid": job_uuid})
        return inbox.with_context(job_uuid=job_uuid)._job_process()

    def _post_bound_message(self, channel, channel_binding, client_message_id):
        message = channel.sudo()._contact_center_post(
            origin="outbound",
            body="Correlation uniqueness fixture",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        return (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": channel_binding.id,
                    "provider_connection_id": self.connection.id,
                    "direction": "outbound",
                    "origin": "agent",
                    "content_type": "text",
                    "client_message_id": client_message_id,
                    "delivery_state": "queued",
                }
            )
        )

    def test_from_me_echo_reconciles_without_creating_domain_records(self):
        channel, channel_binding, _identity = self._channel_binding()
        result = self._send_message(channel)
        message_binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", result["message_id"])], limit=1
        )
        client_message_id = message_binding.client_message_id
        event = self._from_me_event(
            channel_binding,
            client_message_id,
            client_message_id=client_message_id,
            event_id="echo-%s" % uuid.uuid4(),
        )
        counts_before = {
            model_name: self.env[model_name].sudo().search_count([])
            for model_name in (
                "mail.guest",
                "mail.message",
                "contact.center.identity",
                "contact.center.channel.binding",
                "contact.center.message.binding",
                "contact.center.outbox.command",
            )
        }

        first_result = self.env["contact.center.application"]._process_event(
            self.connection, event
        )
        second_result = self.env["contact.center.application"]._process_event(
            self.connection, event
        )

        self.assertEqual(first_result.id, result["message_id"])
        self.assertEqual(second_result, first_result)
        for model_name, count_before in counts_before.items():
            self.assertEqual(
                self.env[model_name].sudo().search_count([]),
                count_before,
                model_name,
            )
        message_binding.invalidate_recordset(
            ["external_message_id", "delivery_state", "protocol_snapshot_json"]
        )
        outbox = self.env["contact.center.outbox.command"].search(
            [("message_binding_id", "=", message_binding.id)], limit=1
        )
        outbox.invalidate_recordset(["state", "provider_response_json"])
        self.assertEqual(message_binding.external_message_id, client_message_id)
        self.assertEqual(message_binding.delivery_state, "sent")
        self.assertTrue(message_binding.protocol_snapshot_json["from_me"])
        self.assertEqual(outbox.state, "done")
        self.assertEqual(outbox.provider_response_json["reconciled_by"], "from_me_echo")
        self.assertEqual(
            outbox.provider_response_json["provider_event_id"], event.event_id
        )
        self.assertEqual(
            self.env["contact.center.delivery.event"].search_count(
                [
                    ("message_binding_id", "=", message_binding.id),
                    ("external_event_id", "=", event.event_id),
                ]
            ),
            1,
        )

    def test_external_device_uses_technical_author_without_guest_or_outbox(self):
        channel, channel_binding, _identity = self._channel_binding()
        event = self._from_me_event(
            channel_binding,
            "external-device-%s" % uuid.uuid4(),
            event_id="external-device-event-%s" % uuid.uuid4(),
        )
        guest_count = self.env["mail.guest"].sudo().search_count([])
        identity_count = self.env["contact.center.identity"].sudo().search_count([])
        outbox_count = self.env["contact.center.outbox.command"].search_count([])
        channel_message_count = self.env["mail.message"].search_count(
            [("model", "=", "mail.channel"), ("res_id", "=", channel.id)]
        )

        application = self.env["contact.center.application"]
        with mock.patch.object(
            type(application), "_notify_ui", autospec=True
        ) as notify_ui:
            message = application._process_event(self.connection, event)
        duplicate = self.env["contact.center.application"]._process_event(
            self.connection, event
        )

        message_created_payloads = [
            call.args[3]
            for call in notify_ui.call_args_list
            if call.args[2] == "message_created"
        ]
        self.assertEqual(
            message_created_payloads,
            [{"message_id": message.id, "direction": "outbound"}],
        )

        self.assertEqual(duplicate, message)
        self.assertEqual(message.author_id, self.technical_author)
        self.assertFalse(message.author_guest_id)
        self.assertEqual(self.env["mail.guest"].sudo().search_count([]), guest_count)
        self.assertEqual(
            self.env["contact.center.identity"].sudo().search_count([]),
            identity_count,
        )
        self.assertEqual(
            self.env["contact.center.outbox.command"].search_count([]), outbox_count
        )
        self.assertEqual(
            self.env["mail.message"].search_count(
                [("model", "=", "mail.channel"), ("res_id", "=", channel.id)]
            ),
            channel_message_count + 1,
        )
        message_binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", message.id)], limit=1
        )
        self.assertEqual(message_binding.channel_binding_id, channel_binding)
        self.assertEqual(message_binding.direction, "outbound")
        self.assertEqual(message_binding.origin, "external_device")
        self.assertEqual(message_binding.delivery_state, "sent")

    def test_external_device_can_create_an_unknown_direct_conversation(self):
        event = self._unknown_direct_from_me_event()
        counts_before = {
            model_name: self.env[model_name].sudo().search_count([])
            for model_name in (
                "mail.guest",
                "mail.channel",
                "mail.message",
                "contact.center.identity",
                "contact.center.channel.binding",
                "contact.center.message.binding",
                "contact.center.outbox.command",
            )
        }

        message = self.env["contact.center.application"]._process_event(
            self.connection, event
        )
        duplicate = self.env["contact.center.application"]._process_event(
            self.connection, event
        )

        self.assertEqual(duplicate, message)
        self.assertEqual(message.author_id, self.technical_author)
        self.assertFalse(message.author_guest_id)
        message_binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", message.id)], limit=1
        )
        channel_binding = message_binding.channel_binding_id
        identity = channel_binding.identity_id
        self.assertEqual(channel_binding.conversation_type, "direct")
        self.assertEqual(channel_binding.conversation_ref, event.conversation_ref)
        self.assertEqual(identity.name, event.conversation_ref)
        self.assertEqual(identity.mail_guest_id.name, event.conversation_ref)
        self.assertEqual(message_binding.direction, "outbound")
        self.assertEqual(message_binding.origin, "external_device")
        self.assertEqual(message_binding.delivery_state, "sent")
        self.assertEqual(
            {(alias.namespace, alias.value_normalized) for alias in identity.alias_ids},
            {
                (address.namespace, address.value_normalized)
                for address in event.conversation.addresses
            },
        )
        for model_name in (
            "mail.guest",
            "mail.channel",
            "mail.message",
            "contact.center.identity",
            "contact.center.channel.binding",
            "contact.center.message.binding",
        ):
            self.assertEqual(
                self.env[model_name].sudo().search_count([]),
                counts_before[model_name] + 1,
                model_name,
            )
        self.assertEqual(
            self.env["contact.center.outbox.command"].sudo().search_count([]),
            counts_before["contact.center.outbox.command"],
        )

    def test_unknown_external_device_conversation_requests_remote_profile(self):
        event = self._unknown_direct_from_me_event()
        binding_type = type(self.env["contact.center.channel.binding"])

        with mock.patch.object(
            binding_type,
            "_request_identity_avatar_sync",
            autospec=True,
            return_value=True,
        ) as request_profile:
            self.env["contact.center.application"]._process_event(
                self.connection, event
            )

        self.assertEqual(request_profile.call_count, 1)
        requested_binding, requested_connection = request_profile.call_args.args[:2]
        self.assertEqual(requested_binding.conversation_type, "direct")
        self.assertEqual(requested_connection, self.connection)

    def test_unknown_external_device_requires_author_before_creating_records(self):
        event = self._unknown_direct_from_me_event()
        models = (
            "mail.guest",
            "mail.channel",
            "mail.message",
            "contact.center.identity",
            "contact.center.channel.binding",
            "contact.center.message.binding",
        )
        counts_before = {
            model_name: self.env[model_name].sudo().search_count([])
            for model_name in models
        }
        self.account.technical_author_id = False

        with self.assertRaisesRegex(
            UnsupportedEventError, "require an account technical author"
        ):
            self.env["contact.center.application"]._process_event(
                self.connection, event
            )

        for model_name in models:
            self.assertEqual(
                self.env[model_name].sudo().search_count([]),
                counts_before[model_name],
                model_name,
            )

    def test_delivery_receipts_are_monotonic_and_idempotent(self):
        channel, channel_binding, _identity = self._channel_binding()
        result = self._send_message(channel, "Track delivery")
        message_binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", result["message_id"])], limit=1
        )
        client_message_id = message_binding.client_message_id
        delivered = self._receipt_event(
            channel_binding,
            client_message_id,
            "delivered",
            event_id="delivered-%s" % uuid.uuid4(),
        )
        read = self._receipt_event(
            channel_binding,
            client_message_id,
            "read",
            event_id="read-%s" % uuid.uuid4(),
            occurred_at="2026-08-21T16:02:00Z",
        )
        late_delivered = self._receipt_event(
            channel_binding,
            client_message_id,
            "delivered",
            event_id="late-delivered-%s" % uuid.uuid4(),
            occurred_at="2026-08-21T16:03:00Z",
        )
        application = self.env["contact.center.application"]

        application._process_event(self.connection, delivered)
        application._process_event(self.connection, delivered)
        message_binding.invalidate_recordset(["delivery_state", "external_message_id"])
        self.assertEqual(message_binding.delivery_state, "delivered")
        self.assertEqual(message_binding.external_message_id, client_message_id)
        self.assertEqual(
            self.env["contact.center.delivery.event"].search_count(
                [
                    ("message_binding_id", "=", message_binding.id),
                    ("external_event_id", "=", delivered.event_id),
                ]
            ),
            1,
        )

        application._process_event(self.connection, read)
        application._process_event(self.connection, read)
        application._process_event(self.connection, late_delivered)

        message_binding.invalidate_recordset(["delivery_state"])
        self.assertEqual(message_binding.delivery_state, "read")
        for receipt in (read, late_delivered):
            self.assertEqual(
                self.env["contact.center.delivery.event"].search_count(
                    [
                        ("message_binding_id", "=", message_binding.id),
                        ("external_event_id", "=", receipt.event_id),
                    ]
                ),
                1,
            )

    def test_exact_receipt_reconciles_uncertain_send_without_redispatch(self):
        channel, channel_binding, _identity = self._channel_binding()
        result = self._send_message(channel, "Already accepted by provider")
        message_binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", result["message_id"])], limit=1
        )
        outbox = self.env["contact.center.outbox.command"].browse(
            result["outbox_command_id"]
        )
        evidence = {
            "dispatch_outcome": "provider_returned_success",
            "external_message_id": message_binding.client_message_id,
        }
        outbox.write(
            {
                "state": "uncertain",
                "provider_response_json": evidence,
                "last_error_class": "PostDispatchPersistenceError",
                "last_error_message": "provider succeeded; local serialization failed",
            }
        )
        event = self._receipt_event(
            channel_binding,
            message_binding.client_message_id,
            "delivered",
            event_id="reconcile-receipt-%s" % uuid.uuid4(),
        )
        application = self.env["contact.center.application"]

        with mock.patch.object(
            Phase1DeliveryAdapter, "execute_command", autospec=True
        ) as execute_command, mock.patch.object(
            type(application), "_notify_ui", autospec=True
        ) as notify_ui:
            application._process_event(self.connection, event)

        execute_command.assert_not_called()
        message_binding.invalidate_recordset(["delivery_state", "external_message_id"])
        outbox.invalidate_recordset(
            [
                "state",
                "processed_at",
                "provider_response_json",
                "last_error_class",
                "last_error_message",
            ]
        )
        self.assertEqual(message_binding.delivery_state, "delivered")
        self.assertEqual(
            message_binding.external_message_id, message_binding.client_message_id
        )
        self.assertEqual(outbox.state, "done")
        self.assertTrue(outbox.processed_at)
        self.assertFalse(outbox.last_error_class)
        self.assertFalse(outbox.last_error_message)
        self.assertEqual(
            outbox.provider_response_json["dispatch_outcome"],
            "provider_returned_success",
        )
        self.assertEqual(
            outbox.provider_response_json["reconciled_by"], "delivery_receipt"
        )
        self.assertEqual(
            outbox.provider_response_json["provider_event_id"], event.event_id
        )
        delivery_payloads = [
            call.args[3]
            for call in notify_ui.call_args_list
            if call.args[2] == "delivery_updated"
        ]
        self.assertIn(
            {
                "message_id": message_binding.message_id.id,
                "state": "delivered",
                "dispatch_state": "done",
            },
            delivery_payloads,
        )

    def test_exact_receipt_waits_for_processing_send_before_projection(self):
        channel, channel_binding, _identity = self._channel_binding()
        result = self._send_message(channel, "Receipt raced local finalization")
        message_binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", result["message_id"])], limit=1
        )
        outbox = self.env["contact.center.outbox.command"].browse(
            result["outbox_command_id"]
        )
        outbox.write({"state": "processing"})
        event = self._receipt_event(
            channel_binding,
            message_binding.client_message_id,
            "delivered",
            event_id="processing-receipt-%s" % uuid.uuid4(),
        )
        application = self.env["contact.center.application"]

        with self.assertRaisesRegex(
            TransientAdapterError, "waiting for dispatch finalization"
        ):
            application._apply_delivery_event(self.connection, event)

        message_binding.invalidate_recordset(["delivery_state", "external_message_id"])
        self.assertEqual(message_binding.delivery_state, "queued")
        self.assertFalse(message_binding.external_message_id)
        self.assertFalse(
            self.env["contact.center.delivery.event"].search_count(
                [
                    ("message_binding_id", "=", message_binding.id),
                    ("external_event_id", "=", event.event_id),
                ]
            )
        )

        # The worker's safe ambiguous outcome is durable before queue_job retries
        # this authenticated receipt. The replay then closes it without I/O.
        outbox.write(
            {
                "state": "uncertain",
                "last_error_class": "PostDispatchPersistenceError",
            }
        )
        application._apply_delivery_event(self.connection, event)
        outbox.invalidate_recordset(["state"])
        message_binding.invalidate_recordset(["delivery_state"])
        self.assertEqual(outbox.state, "done")
        self.assertEqual(message_binding.delivery_state, "delivered")

    def test_multi_message_receipt_locks_bindings_in_id_order(self):
        channel, channel_binding, _identity = self._channel_binding()
        results = [
            self._send_message(channel, "First receipt target"),
            self._send_message(channel, "Second receipt target"),
        ]
        bindings = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [("message_id", "in", [item["message_id"] for item in results])],
                order="id",
            )
        )
        self.assertEqual(len(bindings), 2)
        external_ids = {
            binding.id: "ordered-receipt-%s-%s" % (binding.id, uuid.uuid4())
            for binding in bindings
        }
        for binding in bindings:
            binding.write(
                {
                    "external_message_id": external_ids[binding.id],
                    "delivery_state": "sent",
                }
            )
        payload_order = list(reversed(bindings.ids))
        values = self._receipt_event(
            channel_binding,
            external_ids[payload_order[0]],
            "delivered",
        ).to_dict()
        values["delivery"]["external_message_ids"] = [
            external_ids[binding_id] for binding_id in payload_order
        ]
        event = EventDTO.from_dict(values)

        application = self.env["contact.center.application"]
        binding_type = type(bindings)
        original_apply_delivery = binding_type._contact_center_apply_delivery
        observed_order = []

        def record_order(record, *args, **kwargs):
            observed_order.append(record.id)
            return original_apply_delivery(record, *args, **kwargs)

        with mock.patch.object(
            binding_type,
            "_contact_center_apply_delivery",
            autospec=True,
            side_effect=record_order,
        ):
            application._apply_delivery_event(self.connection, event)

        self.assertEqual(observed_order, sorted(bindings.ids))

    def test_watermark_receipt_is_temporal_and_idempotent(self):
        channel, channel_binding, _identity = self._channel_binding()
        before = self._watermark_message(
            channel,
            channel_binding,
            "watermark-before-%s" % uuid.uuid4(),
            "2026-08-21 15:59:00",
        )
        boundary = self._watermark_message(
            channel,
            channel_binding,
            "watermark-boundary-%s" % uuid.uuid4(),
            "2026-08-21 16:00:00",
        )
        after = self._watermark_message(
            channel,
            channel_binding,
            "watermark-after-%s" % uuid.uuid4(),
            "2026-08-21 16:00:01",
        )
        event = self._watermark_receipt_event(
            channel_binding,
            "delivered",
            self._epoch_milliseconds("2026-08-21 16:00:00"),
            event_id="watermark-delivered-%s" % uuid.uuid4(),
        )
        application = self.env["contact.center.application"]

        first = application._process_event(self.connection, event)
        second = application._process_event(self.connection, event)

        self.assertEqual(first, before | boundary)
        self.assertEqual(second, first)
        (before | boundary | after).invalidate_recordset(["delivery_state"])
        self.assertEqual(before.delivery_state, "delivered")
        self.assertEqual(boundary.delivery_state, "delivered")
        self.assertEqual(after.delivery_state, "sent")
        for binding in before | boundary:
            self.assertEqual(
                self.env["contact.center.delivery.event"].search_count(
                    [
                        ("message_binding_id", "=", binding.id),
                        ("external_event_id", "=", event.event_id),
                    ]
                ),
                1,
            )
        self.assertFalse(
            self.env["contact.center.delivery.event"].search(
                [
                    ("message_binding_id", "=", after.id),
                    ("external_event_id", "=", event.event_id),
                ]
            )
        )

    def test_receipt_with_ids_and_watermark_applies_both_evidence_lanes(self):
        channel, channel_binding, _identity = self._channel_binding()
        exact = self._watermark_message(
            channel,
            channel_binding,
            "watermark-exact-%s" % uuid.uuid4(),
            "2026-08-21 15:58:00",
        )
        cumulative = self._watermark_message(
            channel,
            channel_binding,
            "watermark-cumulative-%s" % uuid.uuid4(),
            "2026-08-21 15:59:00",
        )
        after = self._watermark_message(
            channel,
            channel_binding,
            "watermark-after-%s" % uuid.uuid4(),
            "2026-08-21 16:00:01",
        )
        values = self._receipt_event(
            channel_binding,
            exact.external_message_id,
            "delivered",
        ).to_dict()
        late_external_id = "watermark-late-%s" % uuid.uuid4()
        values["delivery"]["external_message_ids"].append(late_external_id)
        values["delivery"]["watermark"] = self._epoch_milliseconds(
            "2026-08-21 16:00:00"
        )
        event = EventDTO.from_dict(values)

        result = self.env["contact.center.application"]._process_event(
            self.connection, event
        )

        self.assertEqual(result, exact | cumulative)
        (exact | cumulative | after).invalidate_recordset(["delivery_state"])
        self.assertEqual(exact.delivery_state, "delivered")
        self.assertEqual(cumulative.delivery_state, "delivered")
        self.assertEqual(after.delivery_state, "sent")

        late_message = self.env["contact.center.application"]._process_event(
            self.connection,
            self._from_me_event(channel_binding, late_external_id),
        )
        late_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", late_message.id)], limit=1)
        )
        self.assertEqual(late_binding.delivery_state, "delivered")

    def test_watermark_is_replayed_when_external_device_echo_arrives_later(self):
        _channel, channel_binding, _identity = self._channel_binding()
        receipt = self._watermark_receipt_event(
            channel_binding,
            "read",
            self._epoch_milliseconds("2026-08-21 16:00:00"),
            event_id="early-watermark-%s" % uuid.uuid4(),
        )
        application = self.env["contact.center.application"]

        self.assertFalse(application._process_event(self.connection, receipt))
        cursor = (
            self.env["contact.center.delivery.watermark"]
            .sudo()
            .search(
                [
                    ("provider_connection_id", "=", self.connection.id),
                    ("channel_binding_id", "=", channel_binding.id),
                    ("state", "=", "read"),
                ]
            )
        )
        self.assertEqual(len(cursor), 1)

        message = application._process_event(
            self.connection,
            self._from_me_event(
                channel_binding,
                "late-echo-%s" % uuid.uuid4(),
                event_id="late-echo-event-%s" % uuid.uuid4(),
            ),
        )
        message_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", message.id)], limit=1)
        )

        self.assertEqual(message_binding.delivery_state, "read")
        self.assertEqual(
            self.env["contact.center.delivery.event"]
            .sudo()
            .search_count(
                [
                    ("message_binding_id", "=", message_binding.id),
                    ("external_event_id", "=", receipt.event_id),
                ]
            ),
            1,
        )

    def test_watermark_receipt_does_not_cross_conversation_or_connection(self):
        channel, channel_binding, _identity = self._channel_binding()
        other_channel, other_channel_binding, _identity = self._channel_binding()
        in_scope = self._watermark_message(
            channel,
            channel_binding,
            "watermark-in-scope-%s" % uuid.uuid4(),
            "2026-08-21 15:59:00",
        )
        other_conversation = self._watermark_message(
            other_channel,
            other_channel_binding,
            "watermark-other-conversation-%s" % uuid.uuid4(),
            "2026-08-21 15:59:00",
        )
        other_connection = self.env["contact.center.provider.connection"].create(
            {
                "name": "Other Watermark Connection",
                "account_id": self.account.id,
                "adapter_key": "test.phase1.delivery",
                "external_ref": "watermark-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "phase1-fixture-v1",
                "state": "connected",
                "active": True,
                "role": "standby",
                "inbound_active": False,
                "outbound_active": False,
                "capabilities_json": {"send_message": True},
            }
        )
        other_provider = self._watermark_message(
            channel,
            channel_binding,
            "watermark-other-provider-%s" % uuid.uuid4(),
            "2026-08-21 15:59:00",
            connection=other_connection,
        )
        event = self._watermark_receipt_event(
            channel_binding,
            "read",
            self._epoch_milliseconds("2026-08-21 16:00:00"),
        )

        result = self.env["contact.center.application"]._process_event(
            self.connection, event
        )

        self.assertEqual(result, in_scope)
        (in_scope | other_conversation | other_provider).invalidate_recordset(
            ["delivery_state"]
        )
        self.assertEqual(in_scope.delivery_state, "read")
        self.assertEqual(other_conversation.delivery_state, "sent")
        self.assertEqual(other_provider.delivery_state, "sent")

    def test_delivery_without_ids_or_watermark_is_rejected(self):
        _channel, channel_binding, _identity = self._channel_binding()
        values = self._watermark_receipt_event(
            channel_binding,
            "read",
            self._epoch_milliseconds("2026-08-21 16:00:00"),
        ).to_dict()
        values["delivery"].pop("watermark")
        event = EventDTO.from_dict(values)

        with self.assertRaisesRegex(
            ValidationError, "message IDs or an epoch millisecond watermark"
        ):
            self.env["contact.center.application"]._process_event(
                self.connection, event
            )

    def test_delivery_before_message_is_transient(self):
        _channel, channel_binding, _identity = self._channel_binding()
        missing_external_id = "not-persisted-%s" % uuid.uuid4()
        event = self._receipt_event(
            channel_binding,
            missing_external_id,
            "read",
            event_id="early-receipt-%s" % uuid.uuid4(),
        )

        with self.assertRaises(TransientAdapterError) as raised:
            self.env["contact.center.application"]._process_event(
                self.connection, event
            )

        self.assertIsNone(getattr(raised.exception, "retry_after_seconds", None))
        self.assertIn(missing_external_id, str(raised.exception))
        self.assertFalse(
            self.env["contact.center.delivery.event"].search(
                [("external_event_id", "=", event.event_id)]
            )
        )

    def test_self_read_receipt_is_not_treated_as_outbound_delivery(self):
        channel, channel_binding, identity = self._channel_binding()
        external_message_id = "inbound-%s" % uuid.uuid4()
        message = channel.sudo()._contact_center_post(
            origin="inbound",
            body="Inbound message already read on the external device",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_guest_id=identity.mail_guest_id.id,
            partner_ids=[],
        )
        message_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": channel_binding.id,
                    "provider_connection_id": self.connection.id,
                    "direction": "inbound",
                    "origin": "provider",
                    "content_type": "text",
                    "external_message_id": external_message_id,
                    "delivery_state": "delivered",
                }
            )
        )
        event = self._receipt_event(
            channel_binding,
            external_message_id,
            "read",
            is_from_me=True,
        )

        with self.assertRaisesRegex(
            UnsupportedEventError, "self-side direct read evidence"
        ):
            self.env["contact.center.application"]._process_event(
                self.connection, event
            )

        message_binding.invalidate_recordset(["delivery_state"])
        self.assertEqual(message_binding.delivery_state, "delivered")
        self.assertFalse(
            self.env["contact.center.delivery.event"].search(
                [("external_event_id", "=", event.event_id)]
            )
        )

    def test_echo_and_receipt_correlation_are_scoped_by_conversation(self):
        channel_a, binding_a, _identity_a = self._channel_binding()
        channel_b, binding_b, _identity_b = self._channel_binding()
        result_a = self._send_message(channel_a, "Conversation A")
        result_b = self._send_message(channel_b, "Conversation B")
        message_a = self.env["contact.center.message.binding"].search(
            [("message_id", "=", result_a["message_id"])], limit=1
        )
        message_b = self.env["contact.center.message.binding"].search(
            [("message_id", "=", result_b["message_id"])], limit=1
        )
        shared_id = "cross-channel-%s" % uuid.uuid4()
        message_a.external_message_id = shared_id
        message_b.client_message_id = shared_id

        echo = self._from_me_event(binding_a, shared_id)
        receipt = self._receipt_event(binding_a, shared_id, "delivered")
        application = self.env["contact.center.application"]
        self.assertEqual(
            application._process_event(self.connection, echo), message_a.message_id
        )
        application._process_event(self.connection, receipt)

        message_a.invalidate_recordset(["delivery_state"])
        message_b.invalidate_recordset(["delivery_state"])
        self.assertEqual(message_a.delivery_state, "delivered")
        self.assertEqual(message_b.delivery_state, "queued")
        self.assertEqual(message_a.channel_binding_id, binding_a)
        self.assertEqual(message_b.channel_binding_id, binding_b)

    def test_inbound_reply_waits_briefly_then_links_its_late_target(self):
        target_external_id = "late-quote-%s" % uuid.uuid4()
        event = self._inbound_reply_event(target_external_id)
        inbox = self._inbox_event(event)
        inbox._enqueue()
        job = Job.load(self.env, inbox.queue_job_uuid)
        binding_model = self.env["contact.center.message.binding"].sudo()

        for retry, seconds in enumerate((5, 10), start=1):
            with self.assertRaises(RetryableJobError) as raised:
                job.perform()
            self.assertEqual(raised.exception.seconds, seconds)
            self.assertFalse(raised.exception.ignore_retry)
            self.assertEqual(job.retry, retry)
            self.assertEqual(inbox.attempts, 0)
            # OCA rolls back the failed business transaction, then persists its
            # retry counter separately before the next execution.
            job.store()
            self.assertFalse(
                binding_model.search(
                    [("external_message_id", "=", event.message.external_message_id)]
                )
            )

        target_values = event.to_dict()
        target_values.update(event_id="late-target-%s" % uuid.uuid4(), reply_to={})
        target_values["message"].update(
            external_message_id=target_external_id,
            text="The original arrived after its reply",
            reply_to_external_id="",
            protocol_snapshot={},
        )
        original = (
            self.env["contact.center.application"]
            .with_context(contact_center_skip_enqueue=True)
            ._process_event(self.connection, EventDTO.from_dict(target_values))
        )

        self.assertTrue(job.perform())
        reply = binding_model.search(
            [("source_inbox_event_id", "=", inbox.id)]
        ).ensure_one()
        self.assertEqual(inbox.state, "done")
        self.assertEqual(inbox.attempts, 3)
        self.assertEqual(reply.message_id.parent_id, original)
        self.assertEqual(reply.reply_to_binding_id.message_id, original)
        self.assertEqual(reply.direction, "inbound")
        self.assertEqual(reply.message_id.author_guest_id, original.author_guest_id)

    def test_orphan_inbound_reply_preserves_media_at_retry_ceiling(self):
        target_external_id = "uncaptured-quote-%s" % uuid.uuid4()
        values = self._inbound_reply_event(target_external_id).to_dict()
        values["message"].update(
            content_type="image",
            media=[
                {
                    "kind": "image",
                    "mime_type": "image/png",
                    "file_name": "human-evidence.png",
                    "remote_locator": {"fixture": True},
                }
            ],
        )
        event = EventDTO.from_dict(values)
        inbox = self._inbox_event(event)
        inbox.attempts = QUEUE_ATTEMPT_CEILING - 1

        self.assertTrue(self._run_inbox_job(inbox))
        reply = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("source_inbox_event_id", "=", inbox.id)])
            .ensure_one()
        )
        self.assertEqual(inbox.state, "done")
        self.assertEqual(inbox.attempts, QUEUE_ATTEMPT_CEILING)
        self.assertFalse(inbox.last_error_class)
        self.assertFalse(reply.reply_to_binding_id)
        self.assertFalse(reply.message_id.parent_id)
        self.assertIn(event.message.text, reply.message_id.body)
        self.assertEqual(reply.media_ids.kind, "image")
        self.assertEqual(reply.media_ids.file_name, "human-evidence.png")
        self.assertEqual(
            reply.protocol_snapshot_json["reply_to"]["external_message_id"],
            target_external_id,
        )
        self.assertEqual(
            inbox.normalized_dto_json["message"]["reply_to_external_id"],
            target_external_id,
        )
        replay_values = event.to_dict()
        replay_values["event_id"] = "duplicate-quote-%s" % uuid.uuid4()
        replay = self._inbox_event(EventDTO.from_dict(replay_values))
        self.assertTrue(self._run_inbox_job(replay))
        self.assertEqual(replay.state, "done")
        self.assertEqual(
            self.env["contact.center.message.binding"]
            .sudo()
            .search_count(
                [
                    ("channel_binding_id", "=", reply.channel_binding_id.id),
                    ("external_message_id", "=", event.message.external_message_id),
                ]
            ),
            1,
        )
        self.assertEqual(len(reply.media_ids), 1)
        self.assertEqual(reply.source_inbox_event_id, inbox)

    def test_orphan_reply_never_links_another_conversation(self):
        channel, binding, _identity = self._channel_binding()
        target_external_id = "other-conversation-quote-%s" % uuid.uuid4()
        original = self._post_bound_message(channel, binding, target_external_id)
        original.external_message_id = target_external_id
        event = self._inbound_reply_event(target_external_id)
        inbox = self._inbox_event(event)
        inbox.attempts = 2

        self.assertTrue(self._run_inbox_job(inbox))
        reply = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("source_inbox_event_id", "=", inbox.id)])
            .ensure_one()
        )
        self.assertEqual(inbox.state, "done")
        self.assertNotEqual(reply.channel_binding_id, binding)
        self.assertFalse(reply.reply_to_binding_id)
        self.assertFalse(reply.message_id.parent_id)
        self.assertIn(event.message.text, reply.message_id.body)

    def test_orphan_receipt_and_mutation_use_inbox_retry_pattern(self):
        _channel, channel_binding, _identity = self._channel_binding()
        missing_external_id = "not-persisted-%s" % uuid.uuid4()
        events = (
            self._receipt_event(channel_binding, missing_external_id, "read"),
            self._mutation_event(channel_binding, missing_external_id),
        )
        for event in events:
            inbox = self._inbox_event(event)
            with self.subTest(event_type=event.event_type), self.assertRaises(
                RetryableJobError
            ) as raised:
                self._run_inbox_job(inbox)
            self.assertFalse(raised.exception.ignore_retry)
            self.assertIsNone(raised.exception.seconds)

        inbox_job_function = self.env.ref(
            "contact_center_base.queue_job_function_contact_center_inbox_process"
        )
        self.assertEqual(
            inbox_job_function.retry_pattern,
            {
                "1": 5,
                "2": 10,
                "3": 20,
                "4": 40,
                "5": 80,
                "6": 160,
                "7": 320,
                "8": 640,
                "9": 1280,
                "10": 2560,
                "11": 3600,
            },
        )

    def test_orphan_inbox_stops_at_the_shared_retry_ceiling(self):
        _channel, channel_binding, _identity = self._channel_binding()
        event = self._receipt_event(
            channel_binding,
            "not-persisted-%s" % uuid.uuid4(),
            "read",
        )
        inbox = self._inbox_event(event)
        inbox.write({"attempts": QUEUE_ATTEMPT_CEILING - 1})

        self.assertFalse(self._run_inbox_job(inbox))
        self.assertEqual(inbox.state, "dead")
        self.assertEqual(inbox.attempts, QUEUE_ATTEMPT_CEILING)
        self.assertEqual(inbox.last_error_class, "TransientAdapterError")

    def test_client_message_id_is_unique_per_provider_connection(self):
        first_channel, first_channel_binding, _identity = self._channel_binding()
        second_channel, second_channel_binding, _identity = self._channel_binding()
        client_message_id = "duplicate-client-id-%s" % uuid.uuid4()
        first = self._post_bound_message(
            first_channel, first_channel_binding, client_message_id
        )

        with mute_logger("odoo.sql_db"), self.assertRaises(
            IntegrityError
        ), self.env.cr.savepoint():
            self._post_bound_message(
                second_channel, second_channel_binding, client_message_id
            )

        self.assertEqual(
            self.env["contact.center.message.binding"].search_count(
                [
                    ("provider_connection_id", "=", self.connection.id),
                    ("client_message_id", "=", client_message_id),
                ]
            ),
            1,
        )
        self.assertTrue(first.exists())

        other_connection = self.env["contact.center.provider.connection"].create(
            {
                "name": "Second Phase 1 Connection",
                "account_id": self.account.id,
                "adapter_key": "test.phase1.delivery",
                "external_ref": "phase1-second-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "phase1-fixture-v1",
                "state": "connected",
                "active": True,
                "role": "standby",
                "inbound_active": False,
                "outbound_active": False,
                "capabilities_json": {"send_message": True},
            }
        )
        third_channel, third_channel_binding, _identity = self._channel_binding()
        third_message = third_channel.sudo()._contact_center_post(
            origin="outbound",
            body="Same ID on another connection",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        other_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": third_message.id,
                    "channel_binding_id": third_channel_binding.id,
                    "provider_connection_id": other_connection.id,
                    "direction": "outbound",
                    "origin": "agent",
                    "content_type": "text",
                    "client_message_id": client_message_id,
                    "delivery_state": "queued",
                }
            )
        )
        self.assertTrue(other_binding.exists())
