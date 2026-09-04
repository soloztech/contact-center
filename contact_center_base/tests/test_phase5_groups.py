import base64
import copy
import datetime
import hashlib
import uuid
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import SavepointCase
from odoo.tools import mute_logger

from odoo.addons.queue_job.exception import RetryableJobError

from ..models.application import GroupRosterRefreshRequired
from ..services.adapter import (
    AdapterError,
    AdapterResult,
    AmbiguousTimeoutError,
    ProviderAdapter,
    TransientAdapterError,
    UnsupportedEventError,
    adapter_registry,
)
from ..services.dto import AddressDTO, CommandDTO, DTOValidationError, EventDTO


@adapter_registry.register("test.group")
class GroupTestAdapter(ProviderAdapter):
    display_name = "Group Test"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return EventDTO.from_dict(envelope)

    def execute_command(self, connection, command):
        return AdapterResult.success(external_message_id="unexpected")

    def prepare_request_snapshot(self, connection, command):
        return {
            "provider": self.key,
            "method": "POST",
            "endpoint": "/unexpected-group-outbound",
            "payload": {"command_id": command.command_id},
        }

    def derive_client_message_id(self, command_id):
        return uuid.UUID(str(command_id)).hex.upper()

    def prepare_reply_reference(
        self,
        connection,
        *,
        conversation_type,
        external_message_id,
        protocol_snapshot,
        protocol_participant,
    ):
        if conversation_type != "group":
            return super().prepare_reply_reference(
                connection,
                conversation_type=conversation_type,
                external_message_id=external_message_id,
                protocol_snapshot=protocol_snapshot,
                protocol_participant=protocol_participant,
            )
        try:
            participant = AddressDTO.from_dict(protocol_participant)
        except DTOValidationError as error:
            raise AdapterError("invalid group reply participant") from error
        if participant.role != "sender" or participant.confidence != "protocol":
            raise AdapterError("invalid group reply participant")
        return {
            "external_message_id": external_message_id,
            "protocol_participant": participant.to_dict(),
        }

    def get_capabilities(self, connection):
        return {
            "send_message": True,
            "media": {
                kind: {"enabled": True}
                for kind in ("image", "audio", "video", "document")
            },
            "react": True,
            "edit_message": True,
            "delete_message": True,
            "conversation_types": {
                "group": {
                    "send_message": True,
                    "sender_signature": True,
                    "media": {
                        kind: {"enabled": True}
                        for kind in ("image", "audio", "video", "document")
                    },
                    "reply": True,
                    "reply_requires_participant": True,
                    "react": True,
                    "edit_message": True,
                    "delete_message": True,
                    "delivery_receipts": True,
                }
            },
        }

    def get_health(self, connection):
        return {"state": "connected"}


class TestContactCenterGroups(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = cls._create_user("Group Agent", agent_group)
        cls.outsider = cls._create_user("Group Outsider", agent_group)
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Group Team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "WhatsApp Groups",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "group-account-%s" % uuid.uuid4(),
                "default_team_id": cls.team.id,
                "technical_author_id": cls.agent.partner_id.id,
                "group_inbound_enabled": True,
                "group_outbound_enabled": True,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Group Provider",
                "account_id": cls.account.id,
                "adapter_key": "test.group",
                "external_ref": "group-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {
                    "send_message": True,
                    "media": {
                        kind: {"enabled": True}
                        for kind in ("image", "audio", "video", "document")
                    },
                    "react": True,
                    "edit_message": True,
                    "delete_message": True,
                    "conversation_types": {
                        "group": {
                            "send_message": True,
                            "sender_signature": True,
                            "media": {
                                kind: {"enabled": True}
                                for kind in ("image", "audio", "video", "document")
                            },
                            "reply": True,
                            "reply_requires_participant": True,
                            "react": True,
                            "edit_message": True,
                            "delete_message": True,
                            "delivery_receipts": True,
                        }
                    },
                },
            }
        )
        cls.group_ref = "120363000000000@g.us"

    def test_new_accounts_opt_in_to_group_receiving(self):
        account = self.env["contact.center.account"].create(
            {
                "name": "Group inbound opt-in",
                "company_id": self.env.company.id,
                "platform": "whatsapp",
            }
        )

        self.assertFalse(account.group_inbound_enabled)
        self.assertFalse(account._contact_center_accepts_group_inbound())
        account.group_inbound_enabled = True
        self.assertTrue(account._contact_center_accepts_group_inbound())

    def test_disabled_group_receiving_is_terminal_before_projection(self):
        self.account.group_inbound_enabled = False
        binding_count = self.env["contact.center.channel.binding"].search_count([])
        message_count = self.env["mail.message"].search_count(
            [("model", "=", "mail.channel")]
        )

        with self.assertRaisesRegex(
            UnsupportedEventError, "inbound group messages are disabled"
        ):
            self.env["contact.center.application"]._process_event(
                self.connection, self._event(message_id="disabled-group-message")
            )

        self.assertEqual(
            self.env["contact.center.channel.binding"].search_count([]), binding_count
        )
        self.assertEqual(
            self.env["mail.message"].search_count([("model", "=", "mail.channel")]),
            message_count,
        )

    @classmethod
    def _create_user(cls, name, group):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": name,
                    "login": "cc-group-%s" % uuid.uuid4(),
                    "email": "cc-group-%s@example.invalid" % uuid.uuid4(),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )

    def _event(
        self,
        *,
        message_id=None,
        sender_lid="700000000000811@lid",
        sender_pn="5511900000811@s.whatsapp.net",
        sender_name="Participant One",
        event_type="message.created",
        direction="inbound",
        is_from_me=False,
        origin="provider",
        message=True,
        delivery=None,
        mutation=None,
        conversation_type="group",
        conversation_ref=None,
        actor_addresses=None,
        conversation_addresses=None,
        extensions=None,
        reply_to_external_id="",
        content_type="text",
        text="Human group message",
        media=None,
    ):
        event_id = "group-event-%s" % uuid.uuid4()
        conversation_ref = conversation_ref or self.group_ref
        actor_addresses = (
            actor_addresses
            if actor_addresses is not None
            else [
                {
                    "namespace": "whatsapp.lid",
                    "value": sender_lid,
                    "value_normalized": sender_lid,
                    "role": "sender",
                    "source_field": "Info.Sender",
                    "confidence": "protocol",
                },
                {
                    "namespace": "whatsapp.pn",
                    "value": sender_pn,
                    "value_normalized": sender_pn,
                    "role": "alternate",
                    "source_field": "Info.SenderAlt",
                    "confidence": "protocol",
                },
            ]
        )
        values = {
            "provider_schema_version": "fixture-v1",
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": "2026-08-24T12:00:00Z",
            "account_ref": self.account.external_ref,
            "connection_ref": self.connection.external_ref,
            "conversation_ref": conversation_ref,
            "platform": "whatsapp",
            "direction": direction,
            "is_from_me": is_from_me,
            "origin": origin,
            "actor": {
                "display_name": sender_name,
                "addresses": actor_addresses,
            },
            "conversation": {
                "conversation_type": conversation_type,
                "addresses": (
                    conversation_addresses
                    if conversation_addresses is not None
                    else [
                        {
                            "namespace": "whatsapp.group",
                            "value": conversation_ref,
                            "value_normalized": conversation_ref,
                            "role": "group",
                            "source_field": "Info.Chat",
                            "confidence": "protocol",
                        }
                    ]
                ),
            },
            "delivery": delivery or {},
            "mutation": mutation or {},
            "extensions": {
                "conversation_name": "Operations Group",
                **(extensions or {}),
            },
        }
        if message:
            values["message"] = {
                "external_message_id": message_id or "group-message-%s" % uuid.uuid4(),
                "content_type": content_type,
                "text": text,
                "reply_to_external_id": reply_to_external_id,
                "protocol_snapshot": {"fixture": True},
                "media": media or [],
            }
        return EventDTO.from_dict(values)

    def _process(self, event):
        return (
            self.env["contact.center.application"]
            .with_context(contact_center_skip_enqueue=True)
            ._process_event(self.connection, event)
        )

    def _inbox_for_event(self, event):
        return (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "group-fixture-%s" % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": event.to_dict(),
                }
            )
        )

    def _run_inbox_job(self, inbox, job_uuid=None):
        job_uuid = job_uuid or inbox.queue_job_uuid or str(uuid.uuid4())
        if not inbox.queue_job_uuid:
            inbox.sudo().write({"queue_job_uuid": job_uuid})
        return inbox.with_context(job_uuid=job_uuid)._job_process()

    def _group_binding(self):
        return (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    ("conversation_type", "=", "group"),
                    ("conversation_ref", "=", self.group_ref),
                    ("active", "=", True),
                    ("merged_into_id", "=", False),
                ],
                limit=1,
            )
        )

    def _message_binding(self, message):
        return (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", message.id)], limit=1)
        )

    def _projection_counts(self):
        account_domain = [("account_id", "=", self.account.id)]
        company_domain = [("company_id", "=", self.account.company_id.id)]
        return {
            "channels": self.env["mail.channel"]
            .sudo()
            .search_count(
                [
                    ("channel_type", "=", "contact_center"),
                    (
                        "contact_center_company_id",
                        "=",
                        self.account.company_id.id,
                    ),
                ]
            ),
            "channel_bindings": self.env["contact.center.channel.binding"]
            .sudo()
            .search_count(account_domain),
            "channel_aliases": self.env["contact.center.channel.alias"]
            .sudo()
            .search_count(account_domain),
            "group_profiles": self.env["contact.center.group.profile"]
            .sudo()
            .search_count(account_domain),
            "group_participants": self.env["contact.center.group.participant"]
            .sudo()
            .search_count([("group_profile_id.account_id", "=", self.account.id)]),
            "identities": self.env["contact.center.identity"]
            .sudo()
            .search_count(company_domain),
            "identity_aliases": self.env["contact.center.identity.alias"]
            .sudo()
            .search_count(account_domain),
            "guests": self.env["mail.guest"].sudo().search_count([]),
            "mail_messages": self.env["mail.message"]
            .sudo()
            .search_count([("model", "=", "mail.channel")]),
            "messages": self.env["contact.center.message.binding"]
            .sudo()
            .search_count(account_domain),
            "media": self.env["contact.center.media.binding"]
            .sudo()
            .search_count(account_domain),
            "outbox": self.env["contact.center.outbox.command"]
            .sudo()
            .search_count(account_domain),
            "deliveries": self.env["contact.center.delivery.event"]
            .sudo()
            .search_count(account_domain),
        }

    def _seed_outbound_group_target(self, binding, own_participant, external_id):
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        sent = api.send_message(
            binding.channel_id.id,
            "Outbound mutation target",
            client_request_id=str(uuid.uuid4()),
        )
        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", sent["message_id"])], limit=1)
        )
        target.write(
            {
                "external_message_id": external_id,
                "delivery_state": "sent",
            }
        )
        target._contact_center_set_protocol_participant(own_participant)
        return target

    def _seed_own_group_participant(self, binding=None):
        binding = binding or self._group_binding()
        profile = binding.group_profile_ids.sudo()[:1]
        self.assertTrue(profile)
        lid = "700000000000811@lid"
        pn = "5511900000811@s.whatsapp.net"
        participant = (
            self.env["contact.center.group.participant"]
            .sudo()
            .search(
                [
                    ("group_profile_id", "=", profile.id),
                    ("participant_ref", "=", lid),
                ],
                limit=1,
            )
        )
        if not participant:
            participant = (
                self.env["contact.center.group.participant"]
                .sudo()
                .create(
                    {
                        "group_profile_id": profile.id,
                        "participant_ref": lid,
                        "name": "Own Session",
                        "role": "member",
                    }
                )
            )
            self.env["contact.center.group.participant.alias"].sudo().create(
                [
                    {
                        "participant_id": participant.id,
                        "namespace": "whatsapp.lid",
                        "value_raw": lid,
                        "value_normalized": lid,
                        "role": "primary",
                        "source_field": "data.Participants.JID",
                        "confidence": "protocol",
                    },
                    {
                        "participant_id": participant.id,
                        "namespace": "whatsapp.pn",
                        "value_raw": pn,
                        "value_normalized": pn,
                        "role": "alternate",
                        "source_field": "data.Participants.PhoneNumber",
                        "confidence": "protocol",
                    },
                ]
            )
        candidate = AddressDTO(
            namespace="whatsapp.lid",
            value=lid,
            value_normalized=lid,
            role="sender",
            source_field="data.Participants.JID",
            confidence="protocol",
        )
        observed_at = fields.Datetime.now().replace(microsecond=0)
        profile.sudo().write(
            {
                "metadata_state": "ready",
                "roster_complete": True,
                "participant_count": max(1, profile.participant_count),
                "own_role": "member",
                "own_protocol_participant_json": candidate.to_dict(),
                "own_protocol_participant_health_revision": (
                    self.connection.health_configuration_revision
                ),
                "sync_requested_at": observed_at,
                "last_synced_at": observed_at,
                "next_sync_at": observed_at + datetime.timedelta(hours=6),
                "applied_revision": profile.sync_revision,
            }
        )
        return profile, candidate

    def _seed_remote_group_participant(
        self,
        binding,
        lid,
        pn,
        name="Remote Member",
        *,
        role="member",
        active=True,
    ):
        profile = binding.group_profile_ids.sudo()[:1]
        participant = (
            self.env["contact.center.group.participant"]
            .sudo()
            .create(
                {
                    "group_profile_id": profile.id,
                    "participant_ref": lid,
                    "name": name,
                    "role": role,
                    "active": active,
                }
            )
        )
        self.env["contact.center.group.participant.alias"].sudo().create(
            [
                {
                    "participant_id": participant.id,
                    "namespace": "whatsapp.lid",
                    "value_raw": lid,
                    "value_normalized": lid,
                    "role": "primary",
                    "source_field": "data.Participants.JID",
                    "confidence": "protocol",
                },
                {
                    "participant_id": participant.id,
                    "namespace": "whatsapp.pn",
                    "value_raw": pn,
                    "value_normalized": pn,
                    "role": "alternate",
                    "source_field": "data.Participants.PhoneNumber",
                    "confidence": "protocol",
                },
            ]
        )
        profile.sudo().write(
            {
                "participant_count": self.env["contact.center.group.participant"]
                .sudo()
                .search_count(
                    [("group_profile_id", "=", profile.id), ("active", "=", True)]
                )
            }
        )
        return participant

    def _pending_upload(self, binding, content=b"group-image", name="group.png"):
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .with_context(image_no_postprocess=True)
            .create(
                {
                    "name": name,
                    "type": "binary",
                    "datas": base64.b64encode(content),
                    "mimetype": "image/png",
                    "res_model": "contact.center.media.upload",
                    "res_id": 0,
                }
            )
        )
        upload = (
            self.env["contact.center.media.upload"]
            .sudo()
            .create(
                {
                    "reference": str(uuid.uuid4()),
                    "channel_binding_id": binding.id,
                    "uploaded_by_user_id": self.agent.id,
                    "attachment_id": attachment.id,
                    "kind": "image",
                    "mime_type": "image/png",
                    "file_name": name,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        )
        attachment.write({"res_id": upload.id})
        return upload

    def _create_two_participant_group(self):
        first_event = self._event(message_id="group-message-one")
        first_message = self._process(first_event)
        second_event = self._event(
            message_id="group-message-two",
            sender_lid="999111222333@lid",
            sender_pn="551199999999@s.whatsapp.net",
            sender_name="Participant Two",
        )
        second_message = self._process(second_event)
        return first_event, second_event, first_message, second_message

    def test_two_participants_share_one_group_channel_and_keep_distinct_guests(self):
        (
            first_event,
            second_event,
            first_message,
            second_message,
        ) = self._create_two_participant_group()
        binding = self._group_binding()

        self.assertTrue(binding)
        self.assertFalse(binding.identity_id)
        self.assertEqual(binding.channel_id.name, "Operations Group")
        self.assertEqual(
            self.env["contact.center.channel.binding"]
            .sudo()
            .search_count(
                [
                    ("account_id", "=", self.account.id),
                    ("conversation_type", "=", "group"),
                    ("conversation_ref", "=", self.group_ref),
                    ("active", "=", True),
                    ("merged_into_id", "=", False),
                ]
            ),
            1,
        )
        self.assertEqual(first_message.res_id, binding.channel_id.id)
        self.assertEqual(second_message.res_id, binding.channel_id.id)
        self.assertTrue(first_message.author_guest_id)
        self.assertTrue(second_message.author_guest_id)
        self.assertNotEqual(
            first_message.author_guest_id, second_message.author_guest_id
        )

        identities = (
            self.env["contact.center.identity"]
            .sudo()
            .search(
                [
                    (
                        "mail_guest_id",
                        "in",
                        (first_message | second_message).author_guest_id.ids,
                    )
                ]
            )
        )
        self.assertEqual(len(identities), 2)
        self.assertEqual(
            set(binding.channel_id.sudo().channel_member_ids.guest_id.ids),
            set(identities.mail_guest_id.ids),
        )
        self.assertEqual(
            set(binding.alias_ids.mapped("value_normalized")), {self.group_ref}
        )
        self.assertEqual(set(binding.alias_ids.mapped("role")), {"group"})

        actor_values = {
            address.value_normalized
            for event in (first_event, second_event)
            for address in event.actor.addresses
        }
        identity_values = set(identities.alias_ids.mapped("value_normalized"))
        self.assertEqual(identity_values, actor_values)
        self.assertNotIn(self.group_ref, identity_values)
        self.assertFalse(
            binding.alias_ids.filtered(
                lambda alias: alias.value_normalized in actor_values
            )
        )

    def test_replayed_group_message_is_idempotent(self):
        event = self._event(message_id="group-replay-idempotent")
        first_message = self._process(event)
        second_message = self._process(event)
        binding = self._group_binding()

        self.assertEqual(first_message, second_message)
        self.assertEqual(
            self.env["contact.center.channel.binding"]
            .sudo()
            .search_count(
                [
                    ("account_id", "=", self.account.id),
                    ("conversation_type", "=", "group"),
                ]
            ),
            1,
        )
        self.assertEqual(
            self.env["contact.center.message.binding"]
            .sudo()
            .search_count(
                [
                    ("channel_binding_id", "=", binding.id),
                    ("external_message_id", "=", "group-replay-idempotent"),
                ]
            ),
            1,
        )

    def test_direct_participant_identity_and_guest_are_reused_in_group(self):
        sender_lid = "direct-first-participant@lid"
        sender_pn = "551188800011@s.whatsapp.net"
        direct_event = self._event(
            message_id="direct-before-group",
            sender_lid=sender_lid,
            sender_pn=sender_pn,
            conversation_type="direct",
            conversation_ref=sender_pn,
            conversation_addresses=[
                {
                    "namespace": "whatsapp.pn",
                    "value": sender_pn,
                    "value_normalized": sender_pn,
                    "role": "primary",
                    "source_field": "Info.Chat",
                    "confidence": "protocol",
                }
            ],
        )
        direct_message = self._process(direct_event)
        group_message = self._process(
            self._event(
                message_id="group-after-direct",
                sender_lid=sender_lid,
                sender_pn=sender_pn,
            )
        )
        aliases = (
            self.env["contact.center.identity.alias"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    ("value_normalized", "in", [sender_lid, sender_pn]),
                ]
            )
        )

        self.assertEqual(len(aliases.identity_id), 1)
        identity = aliases.identity_id
        self.assertEqual(direct_message.author_guest_id, identity.mail_guest_id)
        self.assertEqual(group_message.author_guest_id, identity.mail_guest_id)
        self.assertEqual(
            self.env["contact.center.channel.binding"]
            .sudo()
            .search_count([("account_id", "=", self.account.id)]),
            2,
        )
        group_binding = self._group_binding()
        self.assertFalse(group_binding.identity_id)
        self.assertIn(
            identity.mail_guest_id,
            group_binding.channel_id.sudo().channel_member_ids.guest_id,
        )

    def test_lid_first_then_pn_enriches_the_same_group_participant_identity(self):
        sender_lid = "lid-first-participant@lid"
        sender_pn = "551177700022@s.whatsapp.net"
        lid_address = {
            "namespace": "whatsapp.lid",
            "value": sender_lid,
            "value_normalized": sender_lid,
            "role": "sender",
            "source_field": "Info.Sender",
            "confidence": "protocol",
        }
        pn_address = {
            "namespace": "whatsapp.pn",
            "value": sender_pn,
            "value_normalized": sender_pn,
            "role": "alternate",
            "source_field": "Info.SenderAlt",
            "confidence": "protocol",
        }
        first_message = self._process(
            self._event(
                message_id="lid-only-first",
                sender_name="LID First",
                actor_addresses=[lid_address],
            )
        )
        second_message = self._process(
            self._event(
                message_id="lid-with-pn-later",
                sender_name="LID First",
                actor_addresses=[lid_address, pn_address],
            )
        )
        aliases = (
            self.env["contact.center.identity.alias"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    ("value_normalized", "in", [sender_lid, sender_pn]),
                ]
            )
        )

        self.assertEqual(len(aliases), 2)
        self.assertEqual(len(aliases.identity_id), 1)
        self.assertEqual(first_message.author_guest_id, second_message.author_guest_id)
        self.assertEqual(
            first_message.author_guest_id, aliases.identity_id.mail_guest_id
        )

    def test_group_core_rejects_mixed_routing_and_group_jid_as_actor(self):
        mixed_addresses = [
            {
                "namespace": "whatsapp.group",
                "value": self.group_ref,
                "value_normalized": self.group_ref,
                "role": "group",
            },
            {
                "namespace": "whatsapp.pn",
                "value": "551166600033@s.whatsapp.net",
                "value_normalized": "551166600033@s.whatsapp.net",
                "role": "routing",
            },
        ]
        with self.assertRaisesRegex(ValidationError, "only group routing identifiers"):
            self._process(self._event(conversation_addresses=mixed_addresses))

        group_actor = {
            "namespace": "whatsapp.group",
            "value": "120363999999999@g.us",
            "value_normalized": "120363999999999@g.us",
            "role": "group",
        }
        with self.assertRaisesRegex(ValidationError, "not a group routing identifier"):
            self._process(self._event(actor_addresses=[group_actor]))

        same_group_value = {
            "namespace": "whatsapp.pn",
            "value": self.group_ref,
            "value_normalized": self.group_ref,
            "role": "sender",
        }
        with self.assertRaisesRegex(
            ValidationError, "participant cannot use the group"
        ):
            self._process(self._event(actor_addresses=[same_group_value]))

        self.assertFalse(
            self.env["contact.center.channel.binding"]
            .sudo()
            .search([("account_id", "=", self.account.id)])
        )
        self.assertFalse(
            self.env["contact.center.identity.alias"]
            .sudo()
            .search([("account_id", "=", self.account.id)])
        )

    def test_group_constraints_require_no_identity_and_unique_active_reference(self):
        self._process(self._event())
        binding = self._group_binding()
        guest = self.env["mail.guest"].sudo().create({"name": "Invalid Group Owner"})
        identity = (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": guest.name,
                    "company_id": self.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        invalid_channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            conversation_type="group",
            name="Invalid owner",
            team=self.team,
        )
        with self.assertRaises(IntegrityError), mute_logger(
            "odoo.sql_db"
        ), self.env.cr.savepoint():
            self.env["contact.center.channel.binding"].sudo().create(
                {
                    "channel_id": invalid_channel.id,
                    "account_id": self.account.id,
                    "identity_id": identity.id,
                    "conversation_type": "group",
                    "conversation_ref": "invalid-owner@g.us",
                }
            )

        duplicate_channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            conversation_type="group",
            name="Duplicate",
            team=self.team,
        )
        with self.assertRaises(IntegrityError), mute_logger(
            "odoo.sql_db"
        ), self.env.cr.savepoint():
            self.env["contact.center.channel.binding"].sudo().create(
                {
                    "channel_id": duplicate_channel.id,
                    "account_id": self.account.id,
                    "identity_id": False,
                    "conversation_type": "group",
                    "conversation_ref": binding.conversation_ref,
                }
            )

    def test_group_non_message_flows_are_terminal_unsupported(self):
        self._process(self._event(message_id="projected-target"))
        binding = self._group_binding()
        projection_counts = {
            "channels": self.env["contact.center.channel.binding"]
            .sudo()
            .search_count([("account_id", "=", self.account.id)]),
            "messages": self.env["contact.center.message.binding"]
            .sudo()
            .search_count([("account_id", "=", self.account.id)]),
            "mutations": self.env["contact.center.message.mutation"]
            .sudo()
            .search_count([("account_id", "=", self.account.id)]),
            "deliveries": self.env["contact.center.delivery.event"]
            .sudo()
            .search_count([("account_id", "=", self.account.id)]),
        }
        unsupported_events = (
            self._event(
                event_type="delivery.updated",
                message=False,
                delivery={
                    "state": "read",
                    "external_message_ids": ["projected-target"],
                },
            ),
            self._event(event_type="group.info", message=False),
            self._event(event_type="message.created", message=False),
        )
        for event in unsupported_events:
            with self.subTest(event_type=event.event_type, event_id=event.event_id):
                with self.assertRaises(UnsupportedEventError):
                    self._process(event)
                self.assertEqual(
                    self.env["contact.center.channel.binding"]
                    .sudo()
                    .search_count([("account_id", "=", self.account.id)]),
                    projection_counts["channels"],
                )
                self.assertEqual(
                    self.env["contact.center.message.binding"]
                    .sudo()
                    .search_count([("account_id", "=", self.account.id)]),
                    projection_counts["messages"],
                )
                self.assertEqual(
                    self.env["contact.center.message.mutation"]
                    .sudo()
                    .search_count([("account_id", "=", self.account.id)]),
                    projection_counts["mutations"],
                )
                self.assertEqual(
                    self.env["contact.center.delivery.event"]
                    .sudo()
                    .search_count([("account_id", "=", self.account.id)]),
                    projection_counts["deliveries"],
                )
        self.assertEqual(binding.conversation_type, "group")

        queued = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "group-unsupported-%s" % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": unsupported_events[0].to_dict(),
                }
            )
        )
        self._run_inbox_job(queued)
        self.assertEqual(queued.state, "unsupported")
        self.assertEqual(queued.last_error_class, "UnsupportedEventError")

    def test_group_from_me_echo_reconciles_the_existing_outbound_message(self):
        self._process(self._event(message_id="group-echo-bootstrap"))
        binding = self._group_binding()
        self._seed_own_group_participant(binding)
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        result = api.send_message(
            binding.channel_id.id,
            "Correlate this group text",
            client_request_id=str(uuid.uuid4()),
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        message_binding = outbox.message_binding_id
        outbox.state = "uncertain"
        message_count = self.env["mail.message"].search_count(
            [("model", "=", "mail.channel"), ("res_id", "=", binding.channel_id.id)]
        )

        echo = self._event(
            message_id=message_binding.client_message_id,
            direction="outbound",
            is_from_me=True,
            origin="external_device",
        )
        reconciled = self._process(echo)
        replayed = self._process(echo)

        self.assertEqual(reconciled.id, result["message_id"])
        self.assertEqual(replayed, reconciled)
        self.assertEqual(message_binding.delivery_state, "sent")
        self.assertEqual(
            message_binding.external_message_id, message_binding.client_message_id
        )
        self.assertEqual(outbox.state, "done")
        self.assertEqual(
            self.env["mail.message"].search_count(
                [
                    ("model", "=", "mail.channel"),
                    ("res_id", "=", binding.channel_id.id),
                ]
            ),
            message_count,
        )

    def test_group_echo_preserves_historical_lid_after_health_rotation(self):
        self._process(self._event(message_id="group-historical-echo-bootstrap"))
        binding = self._group_binding()
        _profile, own_participant = self._seed_own_group_participant(binding)
        now = fields.Datetime.now()
        self.connection.sudo().write(
            {
                "last_health_at": now,
                "last_state_observed_at": now,
            }
        )
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(
                binding.channel_id.id,
                "Historical participant",
                client_request_id=str(uuid.uuid4()),
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        outbox._process_one()
        message_binding = outbox.message_binding_id
        self.assertEqual(
            AddressDTO.from_dict(message_binding.protocol_participant_json),
            own_participant,
        )

        self.connection.sudo().write(
            {
                "health_configuration_revision": (
                    self.connection.health_configuration_revision + 1
                )
            }
        )
        binding.group_profile_ids.invalidate_recordset(
            [
                "own_protocol_participant_json",
                "own_protocol_participant_health_revision",
            ]
        )
        self.assertFalse(binding.group_profile_ids.own_protocol_participant_json)
        echo = self._event(
            message_id=message_binding.external_message_id,
            direction="outbound",
            is_from_me=True,
            origin="external_device",
            actor_addresses=[
                {
                    "namespace": "whatsapp.pn",
                    "value": "5511900000811:7@s.whatsapp.net",
                    "value_normalized": "5511900000811@s.whatsapp.net",
                    "role": "sender",
                    "source_field": "event.Info.Sender",
                    "confidence": "protocol",
                },
                {
                    "namespace": "whatsapp.lid",
                    "value": "700000000000811@lid",
                    "value_normalized": "700000000000811@lid",
                    "role": "alternate",
                    "source_field": "event.Info.SenderAlt",
                    "confidence": "protocol",
                },
            ],
        )

        reconciled = self._process(echo)
        replayed = self._process(echo)

        message_binding.invalidate_recordset(["protocol_participant_json"])
        self.assertEqual(reconciled, message_binding.message_id)
        self.assertEqual(replayed, reconciled)
        self.assertEqual(
            AddressDTO.from_dict(message_binding.protocol_participant_json),
            own_participant,
        )

    def test_group_from_me_echo_cannot_cross_an_unknown_conversation(self):
        self._process(self._event(message_id="group-cross-echo-bootstrap"))
        binding = self._group_binding()
        self._seed_own_group_participant(binding)
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(
                binding.channel_id.id,
                "Keep this echo in its original group",
                client_request_id=str(uuid.uuid4()),
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        message_binding = outbox.message_binding_id
        outbox.state = "uncertain"
        unknown_group = "120363999999999@g.us"

        with self.assertRaisesRegex(
            UnsupportedEventError, "requires an existing conversation"
        ):
            self._process(
                self._event(
                    message_id=message_binding.client_message_id,
                    conversation_ref=unknown_group,
                    direction="outbound",
                    is_from_me=True,
                    origin="external_device",
                )
            )

        self.assertEqual(message_binding.delivery_state, "queued")
        self.assertFalse(message_binding.external_message_id)
        self.assertEqual(outbox.state, "uncertain")
        self.assertFalse(
            self.env["contact.center.channel.alias"]
            .sudo()
            .search_count(
                [
                    ("account_id", "=", self.account.id),
                    ("namespace", "=", "whatsapp.group"),
                    ("value_normalized", "=", unknown_group),
                ]
            )
        )

    def test_unknown_group_external_device_text_bootstraps_and_replays_once(self):
        group_ref = "120363888888881@g.us"
        message_id = "unknown-group-external-device-text"
        counts_before = self._projection_counts()
        event = self._event(
            message_id=message_id,
            conversation_ref=group_ref,
            direction="outbound",
            is_from_me=True,
            origin="external_device",
        )

        first = self._process(event)
        exact_replay = self._process(event)
        duplicate_callback = self._process(
            self._event(
                message_id=message_id,
                conversation_ref=group_ref,
                direction="outbound",
                is_from_me=True,
                origin="external_device",
            )
        )

        binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    ("conversation_type", "=", "group"),
                    ("conversation_ref", "=", group_ref),
                    ("active", "=", True),
                    ("merged_into_id", "=", False),
                ]
            )
        )
        message_binding = self._message_binding(first)
        profile = binding.group_profile_ids.sudo()
        alias = (
            self.env["contact.center.channel.alias"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    ("namespace", "=", "whatsapp.group"),
                    ("value_normalized", "=", group_ref),
                ]
            )
        )
        counts_after = self._projection_counts()

        self.assertEqual(exact_replay, first)
        self.assertEqual(duplicate_callback, first)
        self.assertEqual(len(binding), 1)
        self.assertFalse(binding.identity_id)
        self.assertEqual(binding.channel_id.name, "Operations Group")
        self.assertFalse(binding.channel_id.channel_member_ids.guest_id)
        self.assertEqual(alias.channel_binding_id, binding)
        self.assertEqual(len(profile), 1)
        self.assertEqual(profile.provider_connection_id, self.connection)
        self.assertEqual(profile.metadata_state, "pending")
        self.assertEqual(profile.sync_revision, 1)
        self.assertFalse(profile.queue_job_uuid)
        self.assertEqual(first.author_id, self.account.technical_author_id)
        self.assertIn("Human group message", first.body)
        self.assertEqual(message_binding.channel_binding_id, binding)
        self.assertEqual(message_binding.provider_connection_id, self.connection)
        self.assertEqual(message_binding.direction, "outbound")
        self.assertEqual(message_binding.origin, "external_device")
        self.assertEqual(message_binding.content_type, "text")
        self.assertEqual(message_binding.external_message_id, message_id)
        self.assertEqual(message_binding.delivery_state, "sent")
        self.assertFalse(message_binding.media_ids)
        self.assertEqual(
            AddressDTO.from_dict(message_binding.protocol_participant_json),
            event.actor.addresses[0],
        )
        self.assertEqual(counts_after["channels"], counts_before["channels"] + 1)
        self.assertEqual(
            counts_after["channel_bindings"],
            counts_before["channel_bindings"] + 1,
        )
        self.assertEqual(
            counts_after["channel_aliases"], counts_before["channel_aliases"] + 1
        )
        self.assertEqual(
            counts_after["group_profiles"], counts_before["group_profiles"] + 1
        )
        self.assertEqual(counts_after["messages"], counts_before["messages"] + 1)
        for side_effect in (
            "group_participants",
            "identities",
            "identity_aliases",
            "guests",
            "media",
            "outbox",
        ):
            self.assertEqual(
                counts_after[side_effect],
                counts_before[side_effect],
                side_effect,
            )

    def test_unknown_group_external_device_respects_group_inbound_opt_in(self):
        self.account.group_inbound_enabled = False
        counts_before = self._projection_counts()

        with self.assertRaisesRegex(UnsupportedEventError, "disabled"):
            self._process(
                self._event(
                    message_id="unknown-group-disabled-external-device",
                    conversation_ref="120363888888882@g.us",
                    direction="outbound",
                    is_from_me=True,
                    origin="external_device",
                )
            )

        self.assertEqual(self._projection_counts(), counts_before)

    def test_unknown_group_external_device_requires_technical_author_first(self):
        self.account.technical_author_id = False
        counts_before = self._projection_counts()

        with self.assertRaisesRegex(UnsupportedEventError, "technical author"):
            self._process(
                self._event(
                    message_id="unknown-group-no-technical-author",
                    conversation_ref="120363888888883@g.us",
                    direction="outbound",
                    is_from_me=True,
                    origin="external_device",
                )
            )

        self.assertEqual(self._projection_counts(), counts_before)

    def test_unknown_group_bootstrap_requires_external_device_origin(self):
        counts_before = self._projection_counts()

        with self.assertRaisesRegex(
            UnsupportedEventError,
            "only external-device group messages may create a conversation",
        ):
            self._process(
                self._event(
                    message_id="unknown-group-invalid-origin",
                    conversation_ref="120363888888886@g.us",
                    direction="outbound",
                    is_from_me=True,
                    origin="provider",
                )
            )

        self.assertEqual(self._projection_counts(), counts_before)

    def test_unknown_group_bootstrap_requires_one_exact_routing_address(self):
        fixtures = (
            (
                "multiple",
                "120363888888887@g.us",
                ("120363888888887@g.us", "120363888888897@g.us"),
            ),
            (
                "mismatch",
                "120363888888888@g.us",
                ("120363888888898@g.us",),
            ),
        )
        for label, conversation_ref, address_values in fixtures:
            with self.subTest(case=label):
                counts_before = self._projection_counts()
                addresses = [
                    {
                        "namespace": "whatsapp.group",
                        "value": value,
                        "value_normalized": value,
                        "role": "group",
                        "source_field": "Info.Chat",
                        "confidence": "protocol",
                    }
                    for value in address_values
                ]

                with self.assertRaisesRegex(
                    ValidationError, "one exact protocol routing address"
                ):
                    self._process(
                        self._event(
                            message_id="unknown-group-invalid-route-%s" % label,
                            conversation_ref=conversation_ref,
                            conversation_addresses=addresses,
                            direction="outbound",
                            is_from_me=True,
                            origin="external_device",
                        )
                    )

                self.assertEqual(self._projection_counts(), counts_before)

    def test_unknown_group_reply_failure_rolls_back_bootstrap_projection(self):
        counts_before = self._projection_counts()

        with self.assertRaisesRegex(
            TransientAdapterError, "reply arrived before its target"
        ), self.env.cr.savepoint():
            self._process(
                self._event(
                    message_id="unknown-group-reply-before-target",
                    conversation_ref="120363888888889@g.us",
                    direction="outbound",
                    is_from_me=True,
                    origin="external_device",
                    reply_to_external_id="unknown-group-reply-target",
                )
            )

        self.assertEqual(self._projection_counts(), counts_before)

    def test_unknown_group_external_device_media_bootstraps_and_replays_once(self):
        fixtures = (
            {
                "group_ref": "120363888888884@g.us",
                "message_id": "unknown-group-external-device-image",
                "kind": "image",
                "mime_type": "image/jpeg",
                "file_name": "first-image.jpg",
                "sha256": "a" * 64,
                "width": 800,
                "height": 600,
            },
            {
                "group_ref": "120363888888885@g.us",
                "message_id": "unknown-group-external-device-document",
                "kind": "document",
                "mime_type": "application/pdf",
                "file_name": "first-document.pdf",
                "sha256": "b" * 64,
                "width": 0,
                "height": 0,
            },
        )
        for fixture in fixtures:
            with self.subTest(kind=fixture["kind"]):
                counts_before = self._projection_counts()
                media = {
                    "kind": fixture["kind"],
                    "external_media_id": "%s-media" % fixture["message_id"],
                    "remote_locator": {
                        "direct_path": "/fixture/%s" % fixture["file_name"],
                    },
                    "mime_type": fixture["mime_type"],
                    "file_name": fixture["file_name"],
                    "size_bytes": 321,
                    "sha256": fixture["sha256"],
                    "width": fixture["width"],
                    "height": fixture["height"],
                }
                event = self._event(
                    message_id=fixture["message_id"],
                    conversation_ref=fixture["group_ref"],
                    direction="outbound",
                    is_from_me=True,
                    origin="external_device",
                    content_type=fixture["kind"],
                    text="",
                    media=[media],
                )

                first = self._process(event)
                exact_replay = self._process(event)
                duplicate_callback = self._process(
                    self._event(
                        message_id=fixture["message_id"],
                        conversation_ref=fixture["group_ref"],
                        direction="outbound",
                        is_from_me=True,
                        origin="external_device",
                        content_type=fixture["kind"],
                        text="",
                        media=[media],
                    )
                )

                binding = (
                    self.env["contact.center.channel.binding"]
                    .sudo()
                    .search(
                        [
                            ("account_id", "=", self.account.id),
                            ("conversation_type", "=", "group"),
                            ("conversation_ref", "=", fixture["group_ref"]),
                            ("active", "=", True),
                            ("merged_into_id", "=", False),
                        ]
                    )
                )
                message_binding = self._message_binding(first)
                media_binding = message_binding.media_ids
                counts_after = self._projection_counts()

                self.assertEqual(exact_replay, first)
                self.assertEqual(duplicate_callback, first)
                self.assertEqual(len(binding), 1)
                self.assertFalse(binding.identity_id)
                self.assertEqual(len(binding.group_profile_ids), 1)
                self.assertEqual(first.author_id, self.account.technical_author_id)
                self.assertEqual(message_binding.channel_binding_id, binding)
                self.assertEqual(message_binding.direction, "outbound")
                self.assertEqual(message_binding.origin, "external_device")
                self.assertEqual(message_binding.content_type, fixture["kind"])
                self.assertEqual(message_binding.delivery_state, "sent")
                self.assertEqual(len(media_binding), 1)
                self.assertEqual(media_binding.kind, fixture["kind"])
                self.assertEqual(
                    media_binding.external_media_id, media["external_media_id"]
                )
                self.assertEqual(
                    media_binding.remote_locator_json, media["remote_locator"]
                )
                self.assertEqual(media_binding.mime_type, fixture["mime_type"])
                self.assertEqual(media_binding.file_name, fixture["file_name"])
                self.assertEqual(media_binding.state, "pending")
                self.assertFalse(media_binding.queue_job_uuid)
                self.assertEqual(
                    counts_after["channels"], counts_before["channels"] + 1
                )
                self.assertEqual(
                    counts_after["channel_bindings"],
                    counts_before["channel_bindings"] + 1,
                )
                self.assertEqual(
                    counts_after["group_profiles"],
                    counts_before["group_profiles"] + 1,
                )
                self.assertEqual(
                    counts_after["messages"], counts_before["messages"] + 1
                )
                self.assertEqual(counts_after["media"], counts_before["media"] + 1)
                for side_effect in (
                    "group_participants",
                    "identities",
                    "identity_aliases",
                    "guests",
                    "outbox",
                ):
                    self.assertEqual(
                        counts_after[side_effect],
                        counts_before[side_effect],
                        side_effect,
                    )

    def test_group_echo_rejects_an_unknown_conflicting_reply_target(self):
        target = self._process(self._event(message_id="group-reply-target-a"))
        binding = self._group_binding()
        self._seed_own_group_participant(binding)
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(
                binding.channel_id.id,
                "Persist reply A",
                reply_to_message_id=target.id,
                client_request_id=str(uuid.uuid4()),
            )
        )
        message_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", result["message_id"])], limit=1)
        )
        echo = self._event(
            message_id=message_binding.client_message_id,
            direction="outbound",
            is_from_me=True,
            origin="external_device",
            reply_to_external_id="unknown-reply-target-b",
        )

        with self.assertRaisesRegex(
            ValidationError, "reply target conflicts"
        ), self.env.cr.savepoint():
            self._process(echo)

        message_binding.invalidate_recordset(
            ["external_message_id", "protocol_participant_json"]
        )
        self.assertFalse(message_binding.external_message_id)
        self.assertFalse(message_binding.protocol_participant_json)

    def test_group_external_device_text_uses_the_technical_author_once(self):
        self._process(self._event(message_id="group-device-bootstrap"))
        event = self._event(
            message_id="group-external-device-text",
            direction="outbound",
            is_from_me=True,
            origin="external_device",
        )

        first = self._process(event)
        second = self._process(event)
        message_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", first.id)], limit=1)
        )

        self.assertEqual(second, first)
        self.assertEqual(first.author_id, self.account.technical_author_id)
        self.assertEqual(message_binding.origin, "external_device")
        self.assertEqual(message_binding.direction, "outbound")
        self.assertEqual(message_binding.delivery_state, "sent")

    def test_group_send_without_own_roster_identity_creates_no_projection(self):
        self._process(self._event(message_id="group-own-identity-gate"))
        binding = self._group_binding()
        upload = self._pending_upload(binding)
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        message_count = self.env["mail.message"].sudo().search_count([])
        binding_count = (
            self.env["contact.center.message.binding"].sudo().search_count([])
        )
        outbox_count = self.env["contact.center.outbox.command"].sudo().search_count([])

        self.assertFalse(
            api.get_conversation(binding.channel_id.id)["item"]["can_send"]
        )
        with self.assertRaisesRegex(UserError, "waiting for the provider"):
            api.send_message(
                binding.channel_id.id,
                "Must not consume this upload",
                client_request_id=str(uuid.uuid4()),
                media_refs=[upload.reference],
            )

        upload.invalidate_recordset(["state", "consumed_message_binding_id"])
        self.assertEqual(upload.state, "pending")
        self.assertFalse(upload.consumed_message_binding_id)
        self.assertEqual(
            self.env["mail.message"].sudo().search_count([]), message_count
        )
        self.assertEqual(
            self.env["contact.center.message.binding"].sudo().search_count([]),
            binding_count,
        )
        self.assertEqual(
            self.env["contact.center.outbox.command"].sudo().search_count([]),
            outbox_count,
        )

    def test_group_dispatch_success_projects_own_participant_without_echo(self):
        self._process(self._event(message_id="group-no-echo-bootstrap"))
        binding = self._group_binding()
        _profile, own_participant = self._seed_own_group_participant(binding)
        now = fields.Datetime.now()
        self.connection.sudo().write(
            {
                "last_health_at": now,
                "last_state_observed_at": now,
            }
        )
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        result = api.send_message(
            binding.channel_id.id,
            "Confirmed without an HTTP echo",
            client_request_id=str(uuid.uuid4()),
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        message_binding = outbox.message_binding_id

        self.assertFalse(message_binding.protocol_participant_json)
        self.assertFalse(result["message"]["actions"]["reply"])
        outbox._process_one()
        message_binding.invalidate_recordset(
            ["protocol_participant_json", "external_message_id", "delivery_state"]
        )
        self.assertEqual(outbox.state, "done")
        self.assertEqual(message_binding.external_message_id, "unexpected")
        self.assertEqual(
            AddressDTO.from_dict(message_binding.protocol_participant_json),
            own_participant,
        )
        item = next(
            message
            for message in api.get_timeline(binding.channel_id.id)["items"]
            if message["message_id"] == message_binding.message_id.id
        )
        self.assertTrue(item["actions"]["reply"])

    def test_group_success_projection_uses_account_connection_lock_order(self):
        self._process(self._event(message_id="group-success-lock-bootstrap"))
        binding = self._group_binding()
        self._seed_own_group_participant(binding)
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(
                binding.channel_id.id,
                "Lock the aggregate before projecting provider success",
                client_request_id=str(uuid.uuid4()),
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        command_dto = CommandDTO.from_dict(outbox.command_json)
        observed_locks = []
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute

        def tracked_execute(cursor, query, params=None, *args, **kwargs):
            normalized = " ".join(str(query).lower().split())
            if " for " in normalized:
                lock_tables = (
                    ("contact_center_account", "account"),
                    ("contact_center_provider_connection", "connection"),
                    ("contact_center_outbox_command", "outbox"),
                    ("contact_center_group_profile", "profile"),
                    ("contact_center_message_binding", "message"),
                )
                for table, label in lock_tables:
                    if "from %s" % table in normalized:
                        observed_locks.append(label)
                        break
            return original_execute(cursor, query, params, *args, **kwargs)

        with patch.object(cursor_class, "execute", new=tracked_execute):
            outbox._finalize_dispatch_success(
                self.connection,
                AdapterResult.success(
                    external_message_id="group-lock-order-%s" % uuid.uuid4(),
                    provider_response={"accepted": True},
                ),
                command_dto,
            )

        self.assertEqual(
            list(dict.fromkeys(observed_locks))[:5],
            ["account", "connection", "outbox", "profile", "message"],
        )

    def test_group_provider_success_reconciliation_restores_full_projection(self):
        self._process(self._event(message_id="group-reconciliation-bootstrap"))
        binding = self._group_binding()
        _profile, own_participant = self._seed_own_group_participant(binding)
        now = fields.Datetime.now()
        self.connection.sudo().write(
            {
                "last_health_at": now,
                "last_state_observed_at": now,
                "last_success_at": False,
            }
        )
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(
                binding.channel_id.id,
                "Recover every local group projection",
                client_request_id=str(uuid.uuid4()),
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        accepted_at = now - datetime.timedelta(seconds=1)
        external_message_id = "reconciled-group-%s" % uuid.uuid4()
        job_uuid = str(uuid.uuid4())
        outbox.write(
            {
                "state": "uncertain",
                "attempts": 1,
                "queue_job_uuid": job_uuid,
                "dispatch_job_uuid": job_uuid,
                "dispatch_started_at": accepted_at,
                "processed_at": accepted_at,
                "provider_request_json": {"provider": "test.group"},
                "provider_response_json": {
                    "dispatch_outcome": "provider_returned_success",
                    "external_message_id": external_message_id,
                    "provider_response": {"accepted": True},
                    "local_error_class": "SerializationFailure",
                },
                "last_error_class": "PostDispatchPersistenceError",
                "last_error_message": "provider succeeded; local write serialized",
            }
        )

        with patch.object(
            GroupTestAdapter, "execute_command", autospec=True
        ) as execute_command:
            self.assertTrue(outbox._contact_center_reconcile_provider_success())
            self.assertFalse(outbox._contact_center_reconcile_provider_success())

        execute_command.assert_not_called()
        outbox.invalidate_recordset(["state"])
        outbox.message_binding_id.invalidate_recordset(
            ["protocol_participant_json", "external_message_id", "delivery_state"]
        )
        self.connection.invalidate_recordset(["last_success_at"])
        self.assertEqual(outbox.state, "done")
        self.assertEqual(outbox.message_binding_id.delivery_state, "sent")
        self.assertEqual(
            outbox.message_binding_id.external_message_id, external_message_id
        )
        self.assertEqual(
            AddressDTO.from_dict(outbox.message_binding_id.protocol_participant_json),
            own_participant,
        )
        self.assertEqual(self.connection.last_success_at, accepted_at)
        self.assertEqual(
            self.env["contact.center.delivery.event"].search_count(
                [
                    ("message_binding_id", "=", outbox.message_binding_id.id),
                    (
                        "external_event_id",
                        "=",
                        "provider-success-outbox:%s" % outbox.id,
                    ),
                ]
            ),
            1,
        )

    def test_group_uncertain_result_never_projects_own_participant(self):
        self._process(self._event(message_id="group-uncertain-bootstrap"))
        binding = self._group_binding()
        self._seed_own_group_participant(binding)
        now = fields.Datetime.now()
        self.connection.sudo().write(
            {
                "last_health_at": now,
                "last_state_observed_at": now,
            }
        )
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(
                binding.channel_id.id,
                "Ambiguous provider response",
                client_request_id=str(uuid.uuid4()),
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        with patch.object(
            GroupTestAdapter,
            "execute_command",
            return_value=AdapterResult(
                status="uncertain",
                error_code="fixture_uncertain",
            ),
        ), self.assertRaises(AmbiguousTimeoutError):
            outbox._process_one()

        outbox.message_binding_id.invalidate_recordset(
            ["protocol_participant_json", "external_message_id"]
        )
        self.assertFalse(outbox.message_binding_id.protocol_participant_json)
        self.assertFalse(outbox.message_binding_id.external_message_id)

    def test_group_roster_change_during_dispatch_keeps_success_fail_closed(self):
        self._process(self._event(message_id="group-race-bootstrap"))
        binding = self._group_binding()
        profile, _own_participant = self._seed_own_group_participant(binding)
        now = fields.Datetime.now()
        self.connection.sudo().write(
            {
                "last_health_at": now,
                "last_state_observed_at": now,
            }
        )
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(
                binding.channel_id.id,
                "Roster changes while provider accepts",
                client_request_id=str(uuid.uuid4()),
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )

        def rotate_addressing_mode(_adapter, _connection, _command):
            pn = "5511900000811@s.whatsapp.net"
            profile.sudo().write(
                {
                    "own_protocol_participant_json": AddressDTO(
                        namespace="whatsapp.pn",
                        value=pn,
                        value_normalized=pn,
                        role="sender",
                        source_field="data.Participants.JID",
                        confidence="protocol",
                    ).to_dict()
                }
            )
            return AdapterResult.success(external_message_id="race-success")

        with patch.object(
            GroupTestAdapter,
            "execute_command",
            autospec=True,
            side_effect=rotate_addressing_mode,
        ):
            outbox._process_one()

        outbox.invalidate_recordset(["state"])
        outbox.message_binding_id.invalidate_recordset(
            ["protocol_participant_json", "external_message_id", "delivery_state"]
        )
        self.assertEqual(outbox.state, "done")
        self.assertEqual(outbox.message_binding_id.delivery_state, "sent")
        self.assertEqual(outbox.message_binding_id.external_message_id, "race-success")
        self.assertFalse(outbox.message_binding_id.protocol_participant_json)

    def test_group_text_send_is_scoped_and_idempotent(self):
        inbound_message = self._process(
            self._event(message_id="group-text-outbound-target")
        )
        binding = self._group_binding()
        _profile, own_participant = self._seed_own_group_participant(binding)
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        outbox_count = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search_count([("account_id", "=", self.account.id)])
        )

        request_id = str(uuid.uuid4())
        result = api.send_message(
            binding.channel_id.id,
            "Group text from Odoo",
            client_request_id=request_id,
        )
        message = self.env["mail.message"].browse(result["message_id"])
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        message_binding = outbox.message_binding_id
        command = CommandDTO.from_dict(outbox.command_json)

        self.assertEqual(message.author_id, self.agent.partner_id)
        self.assertEqual(message_binding.direction, "outbound")
        self.assertEqual(message_binding.origin, "agent")
        self.assertEqual(message_binding.content_type, "text")
        self.assertEqual(message_binding.provider_connection_id, self.connection)
        self.assertEqual(outbox.provider_connection_id, self.connection)
        self.assertEqual(command.conversation.conversation_type, "group")
        self.assertEqual(command.target_address.role, "group")
        self.assertEqual(command.target_address.namespace, "whatsapp.group")
        self.assertEqual(command.target_address.value_normalized, self.group_ref)
        self.assertEqual(command.message.text, "Group text from Odoo")
        self.assertFalse(command.message.media)
        self.assertFalse(command.reply_to)
        self.assertEqual(command.own_protocol_participant, own_participant)

        replay = api.send_message(
            binding.channel_id.id,
            "Group text from Odoo",
            client_request_id=request_id,
        )
        self.assertEqual(replay["message_id"], result["message_id"])
        self.assertEqual(replay["outbox_command_id"], result["outbox_command_id"])
        with self.assertRaises(ValidationError):
            api.send_message(
                binding.channel_id.id,
                "Different group text",
                client_request_id=request_id,
            )

        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", inbound_message.id)], limit=1)
        )
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["contact.center.outbox.command"].sudo().with_context(
                contact_center_skip_enqueue=True
            ).create(
                {
                    "account_id": self.account.id,
                    "provider_connection_id": self.connection.id,
                    "channel_binding_id": binding.id,
                    "message_binding_id": target.id,
                    "outbox_idempotency_key": str(uuid.uuid4()),
                    "command_type": "send_message",
                    "command_json": {"message": {"text": "must not dispatch"}},
                }
            )
        self.assertEqual(
            self.env["contact.center.outbox.command"]
            .sudo()
            .search_count([("account_id", "=", self.account.id)]),
            outbox_count + 1,
        )

        with self.assertRaises(AccessError):
            (
                self.env["contact.center.ui.api"]
                .with_user(self.outsider)
                .with_context(contact_center_skip_enqueue=True)
                .send_message(
                    binding.channel_id.id,
                    "Outsider text",
                    client_request_id=str(uuid.uuid4()),
                )
            )

    def test_signed_group_send_survives_the_durable_dispatch_scope(self):
        self._process(self._event(message_id="group-signed-dispatch-bootstrap"))
        binding = self._group_binding()
        self._seed_own_group_participant(binding)
        self.account.outbound_signature_enabled = True
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(
                binding.channel_id.id,
                "Clean canonical group text",
                client_request_id=str(uuid.uuid4()),
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        now = fields.Datetime.now()
        self.connection.write(
            {
                "last_state_observed_at": now,
                "last_health_at": now,
            }
        )

        _connection, command, _adapter, _snapshot = outbox._prepare_dispatch()

        self.assertEqual(command.message.text, "Clean canonical group text")
        self.assertEqual(
            command.options,
            {"sender_signature": {"display_name": "Group Agent"}},
        )
        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(
                outbox.message_binding_id.message_id.body
            ),
            "Clean canonical group text",
        )

        tampered = copy.deepcopy(outbox.command_json)
        tampered["options"]["provider_specific"] = True
        with self.assertRaisesRegex(ValidationError, "command.options"):
            outbox._validate_command_scope(CommandDTO.from_dict(tampered))

        self.account.outbound_signature_enabled = False
        _connection, persisted, _adapter, _snapshot = outbox._prepare_dispatch()
        self.assertEqual(
            persisted.options,
            {"sender_signature": {"display_name": "Group Agent"}},
            "the accepted signature is an immutable outbox snapshot",
        )

    def test_group_dispatch_revalidates_connection_and_client_message_ids(self):
        self._process(self._event(message_id="group-dispatch-scope-bootstrap"))
        binding = self._group_binding()
        profile, _own_participant = self._seed_own_group_participant(binding)
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(
                binding.channel_id.id,
                "Validate the durable group boundary",
                client_request_id=str(uuid.uuid4()),
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        canonical_command = CommandDTO.from_dict(outbox.command_json)

        same_identity_new_evidence = dict(profile.own_protocol_participant_json or {})
        same_identity_new_evidence.update(
            {
                "value": "700000000000811:9@lid",
                "source_field": "data.Participants.SenderAlt",
            }
        )
        profile.own_protocol_participant_json = same_identity_new_evidence
        outbox._validate_command_scope(canonical_command)

        tampered_top_level = copy.deepcopy(outbox.command_json)
        tampered_top_level["client_message_id"] = "A" * 32
        with self.assertRaisesRegex(ValidationError, "client_message_id"):
            outbox._validate_command_scope(CommandDTO.from_dict(tampered_top_level))

        tampered_message = copy.deepcopy(outbox.command_json)
        tampered_message["message"]["client_message_id"] = "B" * 32
        with self.assertRaisesRegex(ValidationError, "client_message_id"):
            outbox._validate_command_scope(CommandDTO.from_dict(tampered_message))

        tampered_participant = copy.deepcopy(outbox.command_json)
        tampered_participant["own_protocol_participant"].update(
            {
                "namespace": "whatsapp.pn",
                "value": "5511900000811@s.whatsapp.net",
                "value_normalized": "5511900000811@s.whatsapp.net",
            }
        )
        with self.assertRaisesRegex(ValidationError, "own_protocol_participant"):
            outbox._validate_command_scope(CommandDTO.from_dict(tampered_participant))

        profile.own_protocol_participant_health_revision = (
            self.connection.health_configuration_revision + 1
        )
        with self.assertRaisesRegex(ValidationError, "own_protocol_participant"):
            outbox._validate_command_scope(canonical_command)
        profile.own_protocol_participant_health_revision = (
            self.connection.health_configuration_revision
        )

        secondary_connection = self.env["contact.center.provider.connection"].create(
            {
                "name": "Secondary Group Provider",
                "account_id": self.account.id,
                "adapter_key": "test.group",
                "external_ref": "group-secondary-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "standby",
                "inbound_active": False,
                "outbound_active": False,
                "capabilities_json": self.connection.capabilities_json,
            }
        )
        outbox.message_binding_id.provider_connection_id = secondary_connection

        with self.assertRaisesRegex(
            ValidationError, "message_binding.provider_connection_id"
        ):
            outbox._validate_command_scope(canonical_command)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["contact.center.outbox.command"].sudo().with_context(
                contact_center_skip_enqueue=True
            ).create(
                {
                    "account_id": self.account.id,
                    "provider_connection_id": self.connection.id,
                    "channel_binding_id": binding.id,
                    "message_binding_id": outbox.message_binding_id.id,
                    "ui_request_id": str(uuid.uuid4()),
                    "outbox_idempotency_key": str(uuid.uuid4()),
                    "command_type": "send_message",
                    "command_json": outbox.command_json,
                }
            )

    def test_group_media_reply_is_idempotent_and_uses_canonical_participant(self):
        inbound_message = self._process(
            self._event(message_id="group-media-reply-target")
        )
        binding = self._group_binding()
        self._seed_own_group_participant(binding)
        upload = self._pending_upload(binding)
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        request_id = str(uuid.uuid4())

        result = api.send_message(
            binding.channel_id.id,
            "Group image caption",
            reply_to_message_id=inbound_message.id,
            client_request_id=request_id,
            media_refs=[upload.reference],
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        command = CommandDTO.from_dict(outbox.command_json)
        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", inbound_message.id)], limit=1)
        )

        upload.invalidate_recordset(["state", "consumed_message_binding_id"])
        self.assertEqual(upload.state, "consumed")
        self.assertEqual(outbox.message_binding_id.content_type, "image")
        self.assertEqual(outbox.message_binding_id.reply_to_binding_id, target)
        self.assertEqual(
            outbox.message_binding_id.message_id.parent_id, inbound_message
        )
        self.assertEqual(len(command.message.media), 1)
        self.assertEqual(command.message.media[0].kind, "image")
        self.assertEqual(command.message.text, "Group image caption")
        self.assertEqual(
            command.reply_to,
            {
                "external_message_id": "group-media-reply-target",
                "protocol_participant": target.protocol_participant_json,
            },
        )
        outbox._validate_command_scope(command)

        replay = api.send_message(
            binding.channel_id.id,
            "Group image caption",
            reply_to_message_id=inbound_message.id,
            client_request_id=request_id,
            media_refs=[upload.reference],
        )
        self.assertEqual(replay["message_id"], result["message_id"])
        self.assertEqual(replay["outbox_command_id"], result["outbox_command_id"])

    def test_group_reply_without_protocol_participant_fails_before_projection(self):
        inbound_message = self._process(
            self._event(message_id="group-reply-without-participant")
        )
        binding = self._group_binding()
        self._seed_own_group_participant(binding)
        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", inbound_message.id)], limit=1)
        )
        target.protocol_participant_json = {}
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        message_count = self.env["mail.message"].sudo().search_count([])
        outbox_count = self.env["contact.center.outbox.command"].sudo().search_count([])

        with self.assertRaisesRegex(ValidationError, "protocol participant"):
            api.send_message(
                binding.channel_id.id,
                "Must fail closed",
                reply_to_message_id=inbound_message.id,
                client_request_id=str(uuid.uuid4()),
            )

        self.assertEqual(
            self.env["mail.message"].sudo().search_count([]), message_count
        )
        self.assertEqual(
            self.env["contact.center.outbox.command"].sudo().search_count([]),
            outbox_count,
        )

    def test_group_reply_adapter_error_is_generic_and_fails_before_projection(self):
        inbound_message = self._process(
            self._event(message_id="group-reply-adapter-error")
        )
        binding = self._group_binding()
        self._seed_own_group_participant(binding)
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        message_count = self.env["mail.message"].sudo().search_count([])
        outbox_count = self.env["contact.center.outbox.command"].sudo().search_count([])
        technical_detail = "provider-token-and-upstream-payload-must-not-leak"

        with patch.object(
            GroupTestAdapter,
            "prepare_reply_reference",
            side_effect=AdapterError(technical_detail),
        ), self.assertRaises(ValidationError) as raised:
            api.send_message(
                binding.channel_id.id,
                "Must fail without leaking provider details",
                reply_to_message_id=inbound_message.id,
                client_request_id=str(uuid.uuid4()),
            )

        self.assertNotIn(technical_detail, str(raised.exception))
        self.assertIn("safe reply reference", str(raised.exception))
        self.assertEqual(
            self.env["mail.message"].sudo().search_count([]), message_count
        )
        self.assertEqual(
            self.env["contact.center.outbox.command"].sudo().search_count([]),
            outbox_count,
        )

    def test_group_outbound_reply_enables_only_after_provider_echo(self):
        self._process(self._event(message_id="group-own-reply-bootstrap"))
        binding = self._group_binding()
        self._seed_own_group_participant(binding)
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        result = api.send_message(
            binding.channel_id.id,
            "Own outbound",
            client_request_id=str(uuid.uuid4()),
        )
        local_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", result["message_id"])], limit=1)
        )
        self.assertFalse(result["message"]["actions"]["reply"])

        echo = self._event(
            message_id=local_binding.client_message_id,
            direction="outbound",
            is_from_me=True,
            origin="external_device",
        )
        echoed = self._process(echo)
        self.assertEqual(echoed.id, result["message_id"])
        local_binding.invalidate_recordset(["protocol_participant_json"])
        self.assertTrue(local_binding.protocol_participant_json)
        item = next(
            message
            for message in api.get_timeline(binding.channel_id.id)["items"]
            if message["message_id"] == result["message_id"]
        )
        self.assertTrue(item["actions"]["reply"])

    def test_group_reaction_uses_structured_target_and_projects_after_success(self):
        remote_lid = "999111222444@lid"
        remote_pn = "5511999998888@s.whatsapp.net"
        inbound_message = self._process(
            self._event(
                message_id="group-reaction-target",
                sender_lid=remote_lid,
                sender_pn=remote_pn,
            )
        )
        binding = self._group_binding()
        _profile, own_participant = self._seed_own_group_participant(binding)
        remote_participant = self._seed_remote_group_participant(
            binding, remote_lid, remote_pn
        )
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )

        reaction_request_id = str(uuid.uuid4()).upper()
        canonical_request_id = str(uuid.UUID(reaction_request_id))
        result = api.react_message(
            binding.channel_id.id,
            inbound_message.id,
            "👍",
            "add",
            reaction_request_id,
        )
        self.assertEqual(result["channel_id"], binding.channel_id.id)
        self.assertEqual(result["client_request_id"], canonical_request_id)
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        command = CommandDTO.from_dict(outbox.command_json)
        target = outbox.target_message_binding_id

        self.assertEqual(command.command_type, "react")
        self.assertEqual(command.own_protocol_participant, own_participant)
        self.assertEqual(
            command.target_protocol_participant.to_dict(),
            target.protocol_participant_json,
        )
        self.assertNotEqual(
            command.target_protocol_participant,
            command.own_protocol_participant,
        )
        own_roster_participant = (
            binding.group_profile_ids._resolve_protocol_participant(
                (own_participant,), active_only=True
            )
        )
        self.assertNotEqual(remote_participant, own_roster_participant)
        self.assertFalse(command.options.get("target_participant"))
        self.assertFalse(command.options["target_from_me"])
        outbox._validate_command_scope(command)
        self.assertFalse(inbound_message.reaction_ids)

        outbox.mutation_id._apply_projection()
        self.assertEqual(inbound_message.reaction_ids.content, "👍")
        self.assertEqual(
            inbound_message.reaction_ids.partner_id,
            self.account.technical_author_id,
        )

        tampered = copy.deepcopy(outbox.command_json)
        tampered["target_protocol_participant"].update(
            {
                "namespace": "whatsapp.lid",
                "value": "999999999999@lid",
                "value_normalized": "999999999999@lid",
            }
        )
        with self.assertRaisesRegex(ValidationError, "target_protocol_participant"):
            outbox._validate_command_scope(CommandDTO.from_dict(tampered))

    def test_group_agent_can_edit_and_delete_only_own_correlated_message(self):
        self._process(self._event(message_id="group-own-mutation-bootstrap"))
        binding = self._group_binding()
        _profile, own_participant = self._seed_own_group_participant(binding)
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        sent = api.send_message(
            binding.channel_id.id,
            "Original group body",
            client_request_id=str(uuid.uuid4()),
        )
        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", sent["message_id"])], limit=1)
        )
        target.write(
            {
                "external_message_id": "group-own-mutation-target",
                "delivery_state": "sent",
            }
        )
        target._contact_center_set_protocol_participant(own_participant)

        edited = api.edit_message(
            binding.channel_id.id,
            target.message_id.id,
            "Edited group body",
            str(uuid.uuid4()),
        )
        edit_outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(edited["outbox_command_id"])
        )
        edit_command = CommandDTO.from_dict(edit_outbox.command_json)
        self.assertTrue(edit_command.options["target_from_me"])
        self.assertEqual(edit_command.target_protocol_participant, own_participant)
        edit_outbox._validate_command_scope(edit_command)
        edit_outbox.mutation_id._apply_projection()
        self.assertIn("Edited group body", str(target.message_id.body))

        deleted = api.delete_message(
            binding.channel_id.id,
            target.message_id.id,
            str(uuid.uuid4()),
        )
        delete_outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(deleted["outbox_command_id"])
        )
        delete_command = CommandDTO.from_dict(delete_outbox.command_json)
        delete_outbox._validate_command_scope(delete_command)
        delete_outbox.mutation_id._apply_projection()
        self.assertEqual(target.message_state, "deleted")

        inbound = self._process(self._event(message_id="group-inbound-no-edit"))
        with self.assertRaises(AccessError):
            api.edit_message(
                binding.channel_id.id,
                inbound.id,
                "Must remain remote",
                str(uuid.uuid4()),
            )

    def test_group_inbound_mutation_accepts_roster_proven_pn_lid_equivalence(self):
        remote_lid = "999111222555@lid"
        remote_pn = "5511999997777@s.whatsapp.net"
        message = self._process(
            self._event(
                message_id="group-remote-edit-target",
                sender_lid=remote_lid,
                sender_pn=remote_pn,
            )
        )
        binding = self._group_binding()
        profile, own_participant = self._seed_own_group_participant(binding)
        expected_remote_participant = self._seed_remote_group_participant(
            binding, remote_lid, remote_pn
        )
        target_participant = {
            "namespace": "whatsapp.pn",
            "value": remote_pn,
            "value_normalized": remote_pn,
            "role": "sender",
            "source_field": "Message.key.participant",
            "confidence": "protocol",
        }
        event = self._event(
            event_type="message.updated",
            message=False,
            sender_lid=remote_lid,
            sender_pn=remote_pn,
            mutation={
                "type": "edit",
                "target_external_message_id": "group-remote-edit-target",
                "target_from_me": False,
                "target_protocol_participant": target_participant,
                "new_text": "Remote participant edit",
            },
        )
        remote_participant = profile._resolve_protocol_participant(
            (AddressDTO.from_dict(target_participant),)
        )
        own_roster_participant = profile._resolve_protocol_participant(
            (own_participant,), active_only=True
        )
        self.assertNotEqual(remote_participant, own_roster_participant)
        self.assertEqual(remote_participant, expected_remote_participant)

        projected = self._process(event)
        self.assertEqual(projected, message)
        self.assertIn("Remote participant edit", str(message.body))
        mutation = (
            self.env["contact.center.message.mutation"]
            .sudo()
            .search([("external_event_id", "=", event.event_id)], limit=1)
        )
        self.assertEqual(mutation.state, "applied")

    def test_group_inbound_reactions_use_account_lanes_and_persisted_target(self):
        target_lid = "999111223001@lid"
        target_pn = "5511999903001@s.whatsapp.net"
        remote_message = self._process(
            self._event(
                message_id="group-remote-reaction-target",
                sender_lid=target_lid,
                sender_pn=target_pn,
                sender_name="Remote Target",
            )
        )
        binding = self._group_binding()
        _profile, own_participant = self._seed_own_group_participant(binding)
        self._seed_remote_group_participant(binding, target_lid, target_pn)
        outbound_target = self._seed_outbound_group_target(
            binding, own_participant, "group-own-reaction-target"
        )

        remote_actor_lid = "999111223002@lid"
        remote_actor_pn = "5511999903002@s.whatsapp.net"
        self._seed_remote_group_participant(
            binding, remote_actor_lid, remote_actor_pn, "Remote Reactor"
        )
        remote_reaction = self._event(
            event_type="message.reaction",
            message=False,
            sender_lid=remote_actor_lid,
            sender_pn=remote_actor_pn,
            sender_name="Remote Reactor",
            mutation={
                "type": "react",
                "target_external_message_id": "group-own-reaction-target",
                # Some providers omit nested key.fromMe. The correlated target
                # binding, not this optional observation, is the canonical lane.
                "emoji": "👍",
                "operation": "add",
            },
        )
        self._process(remote_reaction)
        remote_mutation = (
            self.env["contact.center.message.mutation"]
            .sudo()
            .search([("external_event_id", "=", remote_reaction.event_id)], limit=1)
        )
        self.assertEqual(remote_mutation.state, "applied")
        self.assertTrue(remote_mutation.actor_guest_id)
        self.assertEqual(
            outbound_target.message_id.reaction_ids.guest_id,
            remote_mutation.actor_guest_id,
        )

        own_reaction = self._event(
            event_type="message.reaction",
            message=False,
            direction="outbound",
            is_from_me=True,
            mutation={
                "type": "react",
                "target_external_message_id": "group-remote-reaction-target",
                "target_from_me": True,
                "emoji": "✅",
                "operation": "add",
            },
        )
        self._process(own_reaction)
        own_mutation = (
            self.env["contact.center.message.mutation"]
            .sudo()
            .search([("external_event_id", "=", own_reaction.event_id)], limit=1)
        )
        self.assertEqual(own_mutation.state, "applied")
        self.assertEqual(
            remote_message.reaction_ids.partner_id,
            self.account.technical_author_id,
        )

        mismatched_observation = self._event(
            event_type="message.reaction",
            message=False,
            sender_lid=remote_actor_lid,
            sender_pn=remote_actor_pn,
            sender_name="Remote Reactor",
            mutation={
                "type": "react",
                "target_external_message_id": "group-remote-reaction-target",
                "target_from_me": False,
                "target_protocol_participant": {
                    "namespace": "whatsapp.lid",
                    "value": remote_actor_lid,
                    "value_normalized": remote_actor_lid,
                    "role": "sender",
                    "source_field": "Message.key.participant",
                    "confidence": "protocol",
                },
                "emoji": "🚫",
                "operation": "add",
            },
        )
        with self.assertRaisesRegex(ValidationError, "another group participant"):
            self._process(mismatched_observation)

    def test_direct_mutation_cannot_target_or_enrich_a_group_conversation(self):
        target = self._process(
            self._event(
                message_id="group-target-for-direct-mutation",
                sender_lid="999111223050@lid",
                sender_pn="5511999903050@s.whatsapp.net",
            )
        )
        binding = self._group_binding()
        alias_ids_before = set(binding.alias_ids.ids)
        direct_pn = "5511999903051@s.whatsapp.net"
        event = self._event(
            event_type="message.reaction",
            message=False,
            conversation_type="direct",
            conversation_ref=direct_pn,
            conversation_addresses=[
                {
                    "namespace": "whatsapp.pn",
                    "value": direct_pn,
                    "value_normalized": direct_pn,
                    "role": "primary",
                    "source_field": "Info.Chat",
                    "confidence": "protocol",
                }
            ],
            sender_lid="999111223051@lid",
            sender_pn=direct_pn,
            mutation={
                "type": "react",
                "target_external_message_id": "group-target-for-direct-mutation",
                "emoji": "⛔",
                "operation": "add",
            },
        )

        with self.assertRaisesRegex(
            ValidationError, "conversation type does not match"
        ):
            self._process(event)

        binding.invalidate_recordset(["alias_ids"])
        self.assertEqual(set(binding.alias_ids.ids), alias_ids_before)
        self.assertFalse(target.reaction_ids)

    def test_remote_group_admin_delete_retries_cleanly_during_health_rotation(self):
        self._process(self._event(message_id="group-health-rotation-bootstrap"))
        binding = self._group_binding()
        profile, own_participant = self._seed_own_group_participant(binding)
        target = self._seed_outbound_group_target(
            binding,
            own_participant,
            "group-health-rotation-own-target",
        )
        admin_lid = "999111223090@lid"
        admin_pn = "5511999903090@s.whatsapp.net"
        self._seed_remote_group_participant(
            binding,
            admin_lid,
            admin_pn,
            "Remote Group Admin",
            role="admin",
        )
        event = self._event(
            event_type="message.deleted",
            message=False,
            sender_lid=admin_lid,
            sender_pn=admin_pn,
            sender_name="Remote Group Admin",
            mutation={
                "type": "delete",
                "target_external_message_id": "group-health-rotation-own-target",
                "target_from_me": False,
                "operation": "delete",
            },
        )
        self.connection.sudo().write(
            {
                "health_configuration_revision": (
                    self.connection.health_configuration_revision + 1
                )
            }
        )
        profile.invalidate_recordset(
            [
                "metadata_state",
                "own_protocol_participant_json",
                "own_protocol_participant_health_revision",
            ]
        )
        self.assertEqual(profile.metadata_state, "stale")
        self.assertFalse(profile.own_protocol_participant_json)
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "group-health-rotation-%s" % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": event.to_dict(),
                }
            )
        )

        with patch(
            "odoo.addons.contact_center_base.models.queue._logger.exception"
        ) as unexpected_log, self.assertRaises(RetryableJobError) as raised:
            self._run_inbox_job(inbox)

        self.assertFalse(raised.exception.ignore_retry)
        self.assertIsNone(raised.exception.seconds)
        self.assertIsInstance(raised.exception.__cause__, TransientAdapterError)
        self.assertIn("roster correlation", str(raised.exception.__cause__))
        unexpected_log.assert_not_called()
        target.invalidate_recordset(["message_state"])
        self.assertEqual(target.message_state, "active")

        profile.invalidate_recordset(["sync_revision"])
        refreshed_at = fields.Datetime.now().replace(microsecond=0)
        profile.sudo().write(
            {
                "metadata_state": "ready",
                "roster_complete": True,
                "own_protocol_participant_json": own_participant.to_dict(),
                "own_protocol_participant_health_revision": (
                    self.connection.health_configuration_revision
                ),
                "last_synced_at": refreshed_at,
                "next_sync_at": refreshed_at + datetime.timedelta(hours=6),
                "applied_revision": profile.sync_revision,
            }
        )
        self.assertTrue(self._run_inbox_job(inbox))
        inbox.invalidate_recordset(["state", "last_error_class"])
        target.invalidate_recordset(["message_state"])
        self.assertEqual(inbox.state, "done")
        self.assertFalse(inbox.last_error_class)
        self.assertEqual(target.message_state, "deleted")

    def test_invalid_persisted_own_participant_is_permanent_in_the_inbox(self):
        self._process(self._event(message_id="group-invalid-own-bootstrap"))
        binding = self._group_binding()
        profile, own_participant = self._seed_own_group_participant(binding)
        target = self._seed_outbound_group_target(
            binding,
            own_participant,
            "group-invalid-own-target",
        )
        admin_lid = "999111223091@lid"
        admin_pn = "5511999903091@s.whatsapp.net"
        self._seed_remote_group_participant(
            binding,
            admin_lid,
            admin_pn,
            "Invalid Own Admin",
            role="admin",
        )
        event = self._event(
            event_type="message.deleted",
            message=False,
            sender_lid=admin_lid,
            sender_pn=admin_pn,
            sender_name="Invalid Own Admin",
            mutation={
                "type": "delete",
                "target_external_message_id": "group-invalid-own-target",
                "operation": "delete",
            },
        )
        inbox = self._inbox_for_event(event)
        observed_at = fields.Datetime.now().replace(microsecond=0)
        invalid_own = AddressDTO(
            namespace="whatsapp.lid",
            value="999111223099@lid",
            value_normalized="999111223099@lid",
            role="sender",
            source_field="fixture.invalid_own",
            confidence="protocol",
        )
        profile.sudo().write(
            {
                "metadata_state": "ready",
                "roster_complete": True,
                "own_protocol_participant_json": invalid_own.to_dict(),
                "sync_requested_at": observed_at,
                "last_synced_at": observed_at,
                "applied_revision": profile.sync_revision,
            }
        )

        self.assertTrue(self._run_inbox_job(inbox))

        inbox.invalidate_recordset(["state", "last_error_class", "attempts"])
        target.invalidate_recordset(["message_state"])
        self.assertEqual(inbox.state, "dead")
        self.assertEqual(inbox.last_error_class, "ValidationError")
        self.assertEqual(inbox.attempts, 1)
        self.assertEqual(target.message_state, "active")

    def test_group_inbound_author_deletes_both_lanes_without_observed_target(self):
        remote_lid = "999111223011@lid"
        remote_pn = "5511999903011@s.whatsapp.net"
        remote_message = self._process(
            self._event(
                message_id="group-remote-delete-target",
                sender_lid=remote_lid,
                sender_pn=remote_pn,
                sender_name="Remote Author",
            )
        )
        binding = self._group_binding()
        _profile, own_participant = self._seed_own_group_participant(binding)
        self._seed_remote_group_participant(binding, remote_lid, remote_pn)
        outbound_target = self._seed_outbound_group_target(
            binding, own_participant, "group-own-delete-target"
        )

        self._process(
            self._event(
                event_type="message.deleted",
                message=False,
                sender_lid=remote_lid,
                sender_pn=remote_pn,
                sender_name="Remote Author",
                mutation={
                    "type": "delete",
                    "target_external_message_id": "group-remote-delete-target",
                    "target_from_me": True,
                    "operation": "delete",
                },
            )
        )
        remote_target = self._message_binding(remote_message)
        remote_target.invalidate_recordset(["message_state"])
        self.assertEqual(remote_target.message_state, "deleted")

        self._process(
            self._event(
                event_type="message.deleted",
                message=False,
                direction="outbound",
                is_from_me=True,
                mutation={
                    "type": "delete",
                    "target_external_message_id": "group-own-delete-target",
                    "target_from_me": False,
                    "operation": "delete",
                },
            )
        )
        outbound_target.invalidate_recordset(["message_state"])
        self.assertEqual(outbound_target.message_state, "deleted")

    def test_group_admin_delete_requires_active_roster_role_and_edit_stays_author_only(
        self,
    ):
        binding = self.env["contact.center.channel.binding"]
        _profile = self.env["contact.center.group.profile"]

        for index, role in enumerate(("admin", "superadmin"), start=1):
            target_lid = "999111224%03d@lid" % index
            target_pn = "5511999914%03d@s.whatsapp.net" % index
            target_message = self._process(
                self._event(
                    message_id="group-admin-delete-target-%s" % role,
                    sender_lid=target_lid,
                    sender_pn=target_pn,
                    sender_name="Delete Target %s" % role,
                )
            )
            binding = self._group_binding()
            if not _profile:
                _profile, _own = self._seed_own_group_participant(binding)
            self._seed_remote_group_participant(binding, target_lid, target_pn)
            actor_lid = "999111225%03d@lid" % index
            actor_pn = "5511999915%03d@s.whatsapp.net" % index
            self._seed_remote_group_participant(
                binding,
                actor_lid,
                actor_pn,
                "Group %s" % role,
                role=role,
            )

            self._process(
                self._event(
                    event_type="message.deleted",
                    message=False,
                    sender_lid=actor_lid,
                    sender_pn=actor_pn,
                    sender_name="Group %s" % role,
                    mutation={
                        "type": "delete",
                        "target_external_message_id": (
                            "group-admin-delete-target-%s" % role
                        ),
                        "target_from_me": False,
                        "operation": "delete",
                    },
                )
            )
            target = self._message_binding(target_message)
            target.invalidate_recordset(["message_state"])
            self.assertEqual(target.message_state, "deleted")

        rejected_roles = (
            ("member", True, "member"),
            ("admin", False, "inactive-admin"),
        )
        for index, (role, active, suffix) in enumerate(rejected_roles, start=1):
            target_lid = "999111226%03d@lid" % index
            target_pn = "5511999916%03d@s.whatsapp.net" % index
            target_message = self._process(
                self._event(
                    message_id="group-rejected-delete-target-%s" % suffix,
                    sender_lid=target_lid,
                    sender_pn=target_pn,
                    sender_name="Rejected Delete Target",
                )
            )
            self._seed_remote_group_participant(binding, target_lid, target_pn)
            actor_lid = "999111227%03d@lid" % index
            actor_pn = "5511999917%03d@s.whatsapp.net" % index
            self._seed_remote_group_participant(
                binding,
                actor_lid,
                actor_pn,
                "Rejected %s" % suffix,
                role=role,
                active=active,
            )
            with self.assertRaisesRegex(ValidationError, "active group administrator"):
                self._process(
                    self._event(
                        event_type="message.deleted",
                        message=False,
                        sender_lid=actor_lid,
                        sender_pn=actor_pn,
                        sender_name="Rejected %s" % suffix,
                        mutation={
                            "type": "delete",
                            "target_external_message_id": (
                                "group-rejected-delete-target-%s" % suffix
                            ),
                            "target_from_me": False,
                            "operation": "delete",
                        },
                    )
                )
            target = self._message_binding(target_message)
            target.invalidate_recordset(["message_state"])
            self.assertEqual(target.message_state, "active")

        edit_target_lid = "999111228001@lid"
        edit_target_pn = "5511999918001@s.whatsapp.net"
        edit_target_message = self._process(
            self._event(
                message_id="group-admin-edit-other-target",
                sender_lid=edit_target_lid,
                sender_pn=edit_target_pn,
                sender_name="Edit Target",
            )
        )
        self._seed_remote_group_participant(binding, edit_target_lid, edit_target_pn)
        editor_lid = "999111228002@lid"
        editor_pn = "5511999918002@s.whatsapp.net"
        self._seed_remote_group_participant(
            binding, editor_lid, editor_pn, "Editing Admin", role="admin"
        )
        with self.assertRaisesRegex(ValidationError, "edits must be performed"):
            self._process(
                self._event(
                    event_type="message.updated",
                    message=False,
                    sender_lid=editor_lid,
                    sender_pn=editor_pn,
                    sender_name="Editing Admin",
                    mutation={
                        "type": "edit",
                        "target_external_message_id": ("group-admin-edit-other-target"),
                        "target_from_me": False,
                        "new_text": "Administrator must not edit another author",
                    },
                )
            )
        edit_target = self._message_binding(edit_target_message)
        edit_target.invalidate_recordset(["message_state"])
        self.assertEqual(edit_target.message_state, "active")

    def test_group_delete_waits_for_post_event_roster_before_accepting_promotion(self):
        target_lid = "999111229001@lid"
        target_pn = "5511999919001@s.whatsapp.net"
        target_message = self._process(
            self._event(
                message_id="group-promoted-admin-delete-target",
                sender_lid=target_lid,
                sender_pn=target_pn,
                sender_name="Promotion Target",
            )
        )
        binding = self._group_binding()
        profile, _own = self._seed_own_group_participant(binding)
        self._seed_remote_group_participant(binding, target_lid, target_pn)
        actor_lid = "999111229002@lid"
        actor_pn = "5511999919002@s.whatsapp.net"
        actor = self._seed_remote_group_participant(
            binding,
            actor_lid,
            actor_pn,
            "Promoted Administrator",
            role="member",
        )
        event = self._event(
            event_type="message.deleted",
            message=False,
            sender_lid=actor_lid,
            sender_pn=actor_pn,
            sender_name="Promoted Administrator",
            mutation={
                "type": "delete",
                "target_external_message_id": "group-promoted-admin-delete-target",
                "operation": "delete",
            },
        )
        inbox = self._inbox_for_event(event)
        profile.sudo().write(
            {
                "metadata_state": "ready",
                "roster_complete": True,
                # This emulates a request made before the delete whose response
                # completed afterwards. Response time alone is not causal proof.
                "sync_requested_at": fields.Datetime.to_datetime(
                    inbox.create_date
                ).replace(microsecond=0)
                - datetime.timedelta(seconds=1),
                "last_synced_at": fields.Datetime.to_datetime(
                    inbox.create_date
                ).replace(microsecond=0)
                + datetime.timedelta(seconds=1),
                "applied_revision": profile.sync_revision,
            }
        )

        self.assertFalse(self._run_inbox_job(inbox))
        inbox.invalidate_recordset(
            [
                "state",
                "queue_job_uuid",
                "waiting_group_profile_id",
                "waiting_group_roster_after",
                "last_error_class",
            ]
        )
        profile.invalidate_recordset(
            ["metadata_state", "sync_revision", "queue_job_uuid"]
        )
        self.assertEqual(inbox.state, "pending")
        self.assertFalse(inbox.queue_job_uuid)
        self.assertEqual(inbox.waiting_group_profile_id, profile)
        self.assertEqual(
            inbox.last_error_class,
            "GroupRosterRefreshRequired",
        )
        self.assertEqual(profile.metadata_state, "stale")
        self.assertTrue(profile.queue_job_uuid)

        actor.sudo().write({"role": "admin"})
        profile.sudo().write(
            {
                "metadata_state": "ready",
                "roster_complete": True,
                "sync_requested_at": inbox.waiting_group_roster_after
                + datetime.timedelta(seconds=1),
                "last_synced_at": inbox.waiting_group_roster_after
                + datetime.timedelta(seconds=1),
                "applied_revision": profile.sync_revision,
                "queue_job_uuid": False,
            }
        )
        self.assertEqual(profile._release_roster_waiters(), 1)
        inbox.invalidate_recordset(
            [
                "queue_job_uuid",
                "waiting_group_profile_id",
                "waiting_group_roster_after",
            ]
        )
        self.assertTrue(inbox.queue_job_uuid)
        self.assertFalse(inbox.waiting_group_profile_id)
        self.assertTrue(
            inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
        )
        inbox.invalidate_recordset(["state", "last_error_class"])
        target = self._message_binding(target_message)
        target.invalidate_recordset(["message_state"])
        self.assertEqual(inbox.state, "done")
        self.assertFalse(inbox.last_error_class)
        self.assertEqual(target.message_state, "deleted")

    def test_unknown_group_delete_actor_requests_a_durable_roster_refresh(self):
        target_lid = "999111229021@lid"
        target_pn = "5511999919021@s.whatsapp.net"
        self._process(
            self._event(
                message_id="group-new-admin-delete-target",
                sender_lid=target_lid,
                sender_pn=target_pn,
                sender_name="New Administrator Target",
            )
        )
        binding = self._group_binding()
        profile, _own = self._seed_own_group_participant(binding)
        self._seed_remote_group_participant(binding, target_lid, target_pn)
        actor_lid = "999111229022@lid"
        actor_pn = "5511999919022@s.whatsapp.net"
        event = self._event(
            event_type="message.deleted",
            message=False,
            sender_lid=actor_lid,
            sender_pn=actor_pn,
            sender_name="New Administrator",
            mutation={
                "type": "delete",
                "target_external_message_id": "group-new-admin-delete-target",
                "operation": "delete",
            },
        )
        inbox = self._inbox_for_event(event)
        profile.sudo().write(
            {
                "metadata_state": "ready",
                "roster_complete": True,
                "last_synced_at": fields.Datetime.to_datetime(
                    inbox.create_date
                ).replace(microsecond=0)
                - datetime.timedelta(seconds=1),
                "applied_revision": profile.sync_revision,
            }
        )

        self.assertFalse(self._run_inbox_job(inbox))
        inbox.invalidate_recordset(
            ["state", "queue_job_uuid", "waiting_group_profile_id"]
        )
        profile.invalidate_recordset(["metadata_state", "queue_job_uuid"])
        self.assertEqual(inbox.state, "pending")
        self.assertFalse(inbox.queue_job_uuid)
        self.assertEqual(inbox.waiting_group_profile_id, profile)
        self.assertEqual(profile.metadata_state, "stale")
        self.assertTrue(profile.queue_job_uuid)

    def test_group_delete_waits_for_post_event_roster_before_rejecting_demotion(self):
        target_lid = "999111229011@lid"
        target_pn = "5511999919011@s.whatsapp.net"
        target_message = self._process(
            self._event(
                message_id="group-demoted-admin-delete-target",
                sender_lid=target_lid,
                sender_pn=target_pn,
                sender_name="Demotion Target",
            )
        )
        binding = self._group_binding()
        profile, _own = self._seed_own_group_participant(binding)
        self._seed_remote_group_participant(binding, target_lid, target_pn)
        actor_lid = "999111229012@lid"
        actor_pn = "5511999919012@s.whatsapp.net"
        actor = self._seed_remote_group_participant(
            binding,
            actor_lid,
            actor_pn,
            "Demoted Administrator",
            role="admin",
        )
        event = self._event(
            event_type="message.deleted",
            message=False,
            sender_lid=actor_lid,
            sender_pn=actor_pn,
            sender_name="Demoted Administrator",
            mutation={
                "type": "delete",
                "target_external_message_id": "group-demoted-admin-delete-target",
                "operation": "delete",
            },
        )
        inbox = self._inbox_for_event(event)
        profile.sudo().write(
            {
                "metadata_state": "ready",
                "roster_complete": True,
                "last_synced_at": fields.Datetime.to_datetime(
                    inbox.create_date
                ).replace(microsecond=0)
                - datetime.timedelta(seconds=1),
                "applied_revision": profile.sync_revision,
            }
        )

        self.assertFalse(self._run_inbox_job(inbox))
        inbox.invalidate_recordset(
            ["state", "waiting_group_profile_id", "waiting_group_roster_after"]
        )
        self.assertEqual(inbox.state, "pending")
        self.assertEqual(inbox.waiting_group_profile_id, profile)

        actor.sudo().write({"role": "member"})
        profile.invalidate_recordset(["sync_revision"])
        profile.sudo().write(
            {
                "metadata_state": "ready",
                "roster_complete": True,
                "sync_requested_at": inbox.waiting_group_roster_after
                + datetime.timedelta(seconds=1),
                "last_synced_at": inbox.waiting_group_roster_after
                + datetime.timedelta(seconds=1),
                "applied_revision": profile.sync_revision,
                "queue_job_uuid": False,
            }
        )
        self.assertEqual(profile._release_roster_waiters(), 1)
        inbox.invalidate_recordset(["queue_job_uuid"])
        self.assertTrue(inbox.queue_job_uuid)
        self.assertTrue(
            inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
        )
        inbox.invalidate_recordset(["state", "last_error_class"])
        target = self._message_binding(target_message)
        target.invalidate_recordset(["message_state"])
        self.assertEqual(inbox.state, "dead")
        self.assertEqual(inbox.last_error_class, "ValidationError")
        self.assertEqual(target.message_state, "active")

    def test_group_roster_defer_honors_the_inbox_attempt_ceiling(self):
        target_lid = "999111229031@lid"
        target_pn = "5511999919031@s.whatsapp.net"
        target_message = self._process(
            self._event(
                message_id="group-roster-ceiling-target",
                sender_lid=target_lid,
                sender_pn=target_pn,
            )
        )
        binding = self._group_binding()
        profile, _own = self._seed_own_group_participant(binding)
        self._seed_remote_group_participant(binding, target_lid, target_pn)
        actor_lid = "999111229032@lid"
        actor_pn = "5511999919032@s.whatsapp.net"
        self._seed_remote_group_participant(
            binding, actor_lid, actor_pn, "Deferred Member", role="member"
        )
        event = self._event(
            event_type="message.deleted",
            message=False,
            sender_lid=actor_lid,
            sender_pn=actor_pn,
            sender_name="Deferred Member",
            mutation={
                "type": "delete",
                "target_external_message_id": "group-roster-ceiling-target",
                "operation": "delete",
            },
        )
        inbox = self._inbox_for_event(event)
        before_event = fields.Datetime.to_datetime(inbox.create_date).replace(
            microsecond=0
        ) - datetime.timedelta(seconds=1)
        profile.sudo().write(
            {
                "metadata_state": "ready",
                "roster_complete": True,
                "sync_requested_at": before_event,
                "last_synced_at": before_event,
                "applied_revision": profile.sync_revision,
                "queue_job_uuid": False,
            }
        )
        inbox.sudo().write({"attempts": 11})
        previous_revision = profile.sync_revision

        self.assertFalse(self._run_inbox_job(inbox))

        inbox.invalidate_recordset(
            [
                "state",
                "attempts",
                "group_roster_wait_count",
                "first_group_roster_wait_at",
                "waiting_group_profile_id",
            ]
        )
        profile.invalidate_recordset(["sync_revision", "queue_job_uuid"])
        target = self._message_binding(target_message)
        target.invalidate_recordset(["message_state"])
        self.assertEqual(inbox.state, "dead")
        self.assertEqual(inbox.attempts, 12)
        self.assertEqual(inbox.group_roster_wait_count, 1)
        self.assertTrue(inbox.first_group_roster_wait_at)
        self.assertFalse(inbox.waiting_group_profile_id)
        self.assertEqual(profile.sync_revision, previous_revision)
        self.assertFalse(profile.queue_job_uuid)
        self.assertEqual(target.message_state, "active")

    def test_group_roster_defer_rejects_an_out_of_scope_dependency(self):
        self._process(self._event(message_id="group-roster-scope-bootstrap"))
        profile = self._group_binding().group_profile_ids.sudo()[:1]
        event = self._event(
            event_type="message.deleted",
            message=False,
            mutation={
                "type": "delete",
                "target_external_message_id": "group-roster-scope-bootstrap",
                "operation": "delete",
            },
        )
        inbox = self._inbox_for_event(event)
        error = GroupRosterRefreshRequired(
            profile.id,
            self.connection.id + 1000000,
            fields.Datetime.now(),
        )

        self.assertFalse(inbox._defer_for_group_roster(error, 1))

        inbox.invalidate_recordset(
            ["state", "last_error_class", "waiting_group_profile_id"]
        )
        self.assertEqual(inbox.state, "dead")
        self.assertEqual(inbox.last_error_class, "ValidationError")
        self.assertFalse(inbox.waiting_group_profile_id)

    def test_group_roster_defer_coalesces_a_covering_active_refresh(self):
        target_lid = "999111229041@lid"
        target_pn = "5511999919041@s.whatsapp.net"
        self._process(
            self._event(
                message_id="group-roster-coalescing-target",
                sender_lid=target_lid,
                sender_pn=target_pn,
            )
        )
        binding = self._group_binding()
        profile, _own = self._seed_own_group_participant(binding)
        self._seed_remote_group_participant(binding, target_lid, target_pn)
        actor_lid = "999111229042@lid"
        actor_pn = "5511999919042@s.whatsapp.net"
        self._seed_remote_group_participant(
            binding, actor_lid, actor_pn, "Coalesced Member", role="member"
        )
        event = self._event(
            event_type="message.deleted",
            message=False,
            sender_lid=actor_lid,
            sender_pn=actor_pn,
            sender_name="Coalesced Member",
            mutation={
                "type": "delete",
                "target_external_message_id": "group-roster-coalescing-target",
                "operation": "delete",
            },
        )
        inbox = self._inbox_for_event(event)
        requested_at = fields.Datetime.to_datetime(inbox.create_date).replace(
            microsecond=0
        )
        profile.sudo().write(
            {
                "metadata_state": "stale",
                "roster_complete": True,
                "sync_requested_at": requested_at,
                "last_synced_at": requested_at - datetime.timedelta(seconds=1),
                "applied_revision": profile.sync_revision,
                "queue_job_uuid": False,
            }
        )
        profile._enqueue_sync(profile.sync_revision)
        profile.invalidate_recordset(["sync_revision", "queue_job_uuid"])
        previous_revision = profile.sync_revision
        previous_job_uuid = profile.queue_job_uuid

        self.assertFalse(self._run_inbox_job(inbox))

        profile.invalidate_recordset(["sync_revision", "queue_job_uuid"])
        inbox.invalidate_recordset(
            ["state", "waiting_group_profile_id", "group_roster_wait_count"]
        )
        self.assertEqual(inbox.state, "pending")
        self.assertEqual(inbox.waiting_group_profile_id, profile)
        self.assertEqual(inbox.group_roster_wait_count, 1)
        self.assertEqual(profile.sync_revision, previous_revision)
        self.assertEqual(profile.queue_job_uuid, previous_job_uuid)

    def test_invisible_concurrent_group_alias_is_retryable_not_a_false_conflict(self):
        remote_lid = "999222333445@lid"
        remote_pn = "5511988887778@s.whatsapp.net"
        self._process(
            self._event(
                message_id="group-alias-race-bootstrap",
                sender_lid=remote_lid,
                sender_pn=remote_pn,
            )
        )
        binding = self._group_binding()
        participant = self._seed_remote_group_participant(
            binding, remote_lid, remote_pn
        )
        profile = binding.group_profile_ids.sudo()
        participant.alias_ids.filtered(
            lambda alias: alias.namespace == "whatsapp.pn"
        ).unlink()
        addresses = (
            AddressDTO(
                namespace="whatsapp.lid",
                value=remote_lid,
                value_normalized=remote_lid,
                role="sender",
                confidence="protocol",
            ),
            AddressDTO(
                namespace="whatsapp.pn",
                value=remote_pn,
                value_normalized=remote_pn,
                role="alternate",
                confidence="protocol",
            ),
        )
        alias_model_type = type(self.env["contact.center.group.participant.alias"])

        with patch.object(
            alias_model_type,
            "create",
            side_effect=IntegrityError("concurrent unique alias"),
        ):
            with self.assertRaisesRegex(
                TransientAdapterError, "concurrent group alias observation"
            ):
                profile._resolve_protocol_participant(addresses, enrich=True)

        self.assertEqual(
            profile._resolve_protocol_participant((addresses[0],)), participant
        )

    def test_group_participant_receipts_keep_aggregate_sent_and_expose_counts(self):
        remote_lid = "999222333444@lid"
        remote_pn = "5511988887777@s.whatsapp.net"
        self._process(
            self._event(
                message_id="group-receipt-bootstrap",
                sender_lid=remote_lid,
                sender_pn=remote_pn,
            )
        )
        binding = self._group_binding()
        _profile, own_participant = self._seed_own_group_participant(binding)
        expected_remote_participant = self._seed_remote_group_participant(
            binding, remote_lid, remote_pn
        )
        remote_participant = binding.group_profile_ids._resolve_protocol_participant(
            (
                AddressDTO(
                    namespace="whatsapp.lid",
                    value=remote_lid,
                    value_normalized=remote_lid,
                    role="sender",
                    source_field="Info.Sender",
                    confidence="protocol",
                ),
            )
        )
        own_roster_participant = (
            binding.group_profile_ids._resolve_protocol_participant(
                (own_participant,), active_only=True
            )
        )
        self.assertTrue(remote_participant)
        self.assertTrue(own_roster_participant)
        self.assertEqual(remote_participant, expected_remote_participant)
        self.assertNotEqual(remote_participant, own_roster_participant)
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        sent = api.send_message(
            binding.channel_id.id,
            "Receipt target",
            client_request_id=str(uuid.uuid4()),
        )
        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", sent["message_id"])], limit=1)
        )
        target.write(
            {
                "external_message_id": "group-receipt-target",
                "delivery_state": "sent",
            }
        )
        target._contact_center_set_protocol_participant(own_participant)
        outbox = self.env["contact.center.outbox.command"].browse(
            sent["outbox_command_id"]
        )
        outbox.write(
            {
                "state": "uncertain",
                "last_error_class": "PostDispatchPersistenceError",
                "last_error_message": "provider succeeded; local serialization failed",
            }
        )

        for state in ("read", "delivered"):
            event = self._event(
                event_type="delivery.updated",
                message=False,
                direction="outbound",
                is_from_me=False,
                sender_lid=remote_lid,
                sender_pn=remote_pn,
                delivery={
                    "state": state,
                    "external_message_ids": ["group-receipt-target"],
                    "external_event_id": "group-receipt-%s" % state,
                },
            )
            self._process(event)

        target.invalidate_recordset(["delivery_state"])
        outbox.invalidate_recordset(
            ["state", "provider_response_json", "last_error_class"]
        )
        self.assertEqual(target.delivery_state, "sent")
        self.assertEqual(outbox.state, "done")
        self.assertFalse(outbox.last_error_class)
        self.assertEqual(
            outbox.provider_response_json["reconciled_by"],
            "group_delivery_receipt",
        )
        self.assertEqual(
            self.env["contact.center.group.delivery.event"]
            .sudo()
            .search_count([("message_binding_id", "=", target.id)]),
            2,
        )
        item = next(
            value
            for value in api.get_timeline(binding.channel_id.id)["items"]
            if value["message_id"] == target.message_id.id
        )
        self.assertEqual(
            item["group_delivery"],
            {"delivered_count": 1, "read_count": 1},
        )

    def test_group_multi_message_receipt_locks_bindings_in_id_order(self):
        remote_lid = "999222333445@lid"
        remote_pn = "5511988887778@s.whatsapp.net"
        self._process(
            self._event(
                message_id="group-ordered-receipt-bootstrap",
                sender_lid=remote_lid,
                sender_pn=remote_pn,
            )
        )
        binding = self._group_binding()
        _profile, own_participant = self._seed_own_group_participant(binding)
        self._seed_remote_group_participant(binding, remote_lid, remote_pn)
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        results = [
            api.send_message(
                binding.channel_id.id,
                "First ordered group receipt target",
                client_request_id=str(uuid.uuid4()),
            ),
            api.send_message(
                binding.channel_id.id,
                "Second ordered group receipt target",
                client_request_id=str(uuid.uuid4()),
            ),
        ]
        message_bindings = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [("message_id", "in", [item["message_id"] for item in results])],
                order="id",
            )
        )
        self.assertEqual(len(message_bindings), 2)
        external_ids = {
            message_binding.id: "group-ordered-receipt-%s-%s"
            % (message_binding.id, uuid.uuid4())
            for message_binding in message_bindings
        }
        for message_binding in message_bindings:
            message_binding.write(
                {
                    "external_message_id": external_ids[message_binding.id],
                    "delivery_state": "sent",
                }
            )
            message_binding._contact_center_set_protocol_participant(own_participant)
        payload_order = list(reversed(message_bindings.ids))
        event = self._event(
            event_type="delivery.updated",
            message=False,
            direction="outbound",
            is_from_me=False,
            sender_lid=remote_lid,
            sender_pn=remote_pn,
            delivery={
                "state": "delivered",
                "external_message_ids": [
                    external_ids[message_binding_id]
                    for message_binding_id in payload_order
                ],
                "external_event_id": "group-ordered-receipt-event-%s" % uuid.uuid4(),
            },
        )
        receipt_model_type = type(self.env["contact.center.group.delivery.event"])
        original_record_receipt = receipt_model_type._record_receipt
        observed_order = []

        def record_order(record, *args, **kwargs):
            observed_order.append(kwargs["message_binding"].id)
            return original_record_receipt(record, *args, **kwargs)

        with patch.object(
            receipt_model_type,
            "_record_receipt",
            autospec=True,
            side_effect=record_order,
        ):
            self._process(event)

        self.assertEqual(observed_order, sorted(message_bindings.ids))

    def test_group_receipt_locks_complete_scope_in_parent_first_order(self):
        remote_lid = "999222333446@lid"
        remote_pn = "5511988887779@s.whatsapp.net"
        self._process(
            self._event(
                message_id="group-receipt-lock-bootstrap",
                sender_lid=remote_lid,
                sender_pn=remote_pn,
            )
        )
        binding = self._group_binding()
        _profile, own_participant = self._seed_own_group_participant(binding)
        self._seed_remote_group_participant(binding, remote_lid, remote_pn)
        target = self._seed_outbound_group_target(
            binding,
            own_participant,
            "group-receipt-lock-target",
        )
        event = self._event(
            event_type="delivery.updated",
            message=False,
            direction="outbound",
            is_from_me=False,
            sender_lid=remote_lid,
            sender_pn=remote_pn,
            delivery={
                "state": "delivered",
                "external_message_ids": [target.external_message_id],
                "external_event_id": "group-receipt-lock-event",
            },
        )
        observed_locks = []
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute

        def tracked_execute(cursor, query, params=None, *args, **kwargs):
            normalized = " ".join(str(query).lower().split())
            if " for " in normalized:
                lock_tables = (
                    ("contact_center_account", "account"),
                    ("contact_center_provider_connection", "connection"),
                    ("mail_channel", "channel"),
                    ("contact_center_channel_binding", "binding"),
                    ("contact_center_group_profile", "profile"),
                    ("contact_center_outbox_command", "outbox"),
                    ("contact_center_group_participant ", "participant"),
                    ("contact_center_message_binding", "message"),
                )
                for table, label in lock_tables:
                    if "from %s" % table in normalized:
                        observed_locks.append(label)
                        break
            return original_execute(cursor, query, params, *args, **kwargs)

        with patch.object(cursor_class, "execute", new=tracked_execute):
            self._process(event)

        first_lock_per_table = list(dict.fromkeys(observed_locks))
        self.assertEqual(
            first_lock_per_table,
            [
                "account",
                "connection",
                "channel",
                "binding",
                "profile",
                "outbox",
                "participant",
                "message",
            ],
        )

    def test_group_receipt_rejects_non_sequence_provider_message_ids(self):
        for external_message_ids in (42, {"unexpected": "message-id"}):
            with self.subTest(external_message_ids=external_message_ids):
                event = self._event(
                    event_type="delivery.updated",
                    message=False,
                    direction="outbound",
                    is_from_me=False,
                    delivery={
                        "state": "delivered",
                        "external_message_ids": external_message_ids,
                    },
                )
                with self.assertRaisesRegex(
                    ValidationError, "must be a string or a list"
                ):
                    self._process(event)

    def test_group_receipt_mixed_batch_keeps_correlated_messages(self):
        remote_lid = "999333444555@lid"
        remote_pn = "5511977776666@s.whatsapp.net"
        self._process(
            self._event(
                message_id="group-mixed-receipt-bootstrap",
                sender_lid=remote_lid,
                sender_pn=remote_pn,
            )
        )
        binding = self._group_binding()
        _profile, own_participant = self._seed_own_group_participant(binding)
        self._seed_remote_group_participant(binding, remote_lid, remote_pn)
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        sent = api.send_message(
            binding.channel_id.id,
            "Mixed receipt target",
            client_request_id=str(uuid.uuid4()),
        )
        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", sent["message_id"])], limit=1)
        )
        target.write(
            {
                "external_message_id": "group-mixed-receipt-known",
                "delivery_state": "sent",
            }
        )
        target._contact_center_set_protocol_participant(own_participant)
        event = self._event(
            event_type="delivery.updated",
            message=False,
            direction="outbound",
            is_from_me=False,
            sender_lid=remote_lid,
            sender_pn=remote_pn,
            delivery={
                "state": "delivered",
                "external_message_ids": [
                    "group-mixed-receipt-known",
                    "group-mixed-receipt-historical",
                ],
                "external_event_id": "group-mixed-receipt-event",
            },
        )
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "group-mixed-receipt-%s" % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": event.to_dict(),
                }
            )
        )

        inbox._process_normalized(event)

        self.assertEqual(inbox.state, "done")
        self.assertEqual(
            inbox.metadata_json,
            {
                "group_receipt_partial": True,
                "group_receipt_uncorrelated_count": 1,
            },
        )
        receipt = (
            self.env["contact.center.group.delivery.event"]
            .sudo()
            .search(
                [
                    ("message_binding_id", "=", target.id),
                    ("state", "=", "delivered"),
                ]
            )
        )
        self.assertEqual(len(receipt), 1)
        self.assertEqual(receipt.inbox_event_id, inbox)

        all_unknown = self._event(
            event_type="delivery.updated",
            message=False,
            direction="outbound",
            is_from_me=False,
            sender_lid=remote_lid,
            sender_pn=remote_pn,
            delivery={
                "state": "delivered",
                "external_message_ids": ["group-receipt-not-correlated"],
                "external_event_id": "group-receipt-not-correlated-event",
            },
        )
        with self.assertRaisesRegex(
            TransientAdapterError, "before message correlation"
        ):
            self._process(all_unknown)

    def test_group_send_requires_account_opt_in_and_explicit_provider_capability(self):
        self._process(self._event(message_id="group-capability-gate"))
        binding = self._group_binding()
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )

        self.account.group_outbound_enabled = False
        self.assertFalse(
            api.get_conversation(binding.channel_id.id)["item"]["can_send"]
        )
        with self.assertRaisesRegex(UserError, "not enabled"):
            api.send_message(
                binding.channel_id.id,
                "Blocked by account policy",
                client_request_id=str(uuid.uuid4()),
            )
        self.account.group_outbound_enabled = True

        capabilities = dict(self.connection.capabilities_json or {})
        capabilities.pop("conversation_types", None)
        self.connection.capabilities_json = capabilities
        self.assertFalse(
            api.get_conversation(binding.channel_id.id)["item"]["can_send"]
        )
        with self.assertRaisesRegex(UserError, "cannot send"):
            api.send_message(
                binding.channel_id.id,
                "Blocked by provider capability",
                client_request_id=str(uuid.uuid4()),
            )

    def test_group_ui_dto_exposes_media_and_per_message_reply_only(self):
        self._process(self._event(message_id="group-ui"))
        binding = self._group_binding()
        self._seed_own_group_participant(binding)
        item = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .get_conversation(binding.channel_id.id)["item"]
        )
        self.assertEqual(item["conversation_type"], "group")
        self.assertTrue(item["can_send"])
        self.assertEqual(
            item["capabilities"],
            {
                "send_message": True,
                "sender_signature": True,
                "media": {
                    kind: {"enabled": True}
                    for kind in ("image", "audio", "video", "document")
                },
                "reply": True,
                "react": True,
                "edit_message": True,
                "delete_message": True,
                "delivery_receipts": True,
                "view_attribution": False,
            },
        )
        self.assertEqual(
            item["last_message"]["actions"],
            {
                "reply": True,
                "react": True,
                "edit": False,
                "delete": False,
                "resend": False,
            },
        )
        timeline = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .get_timeline(binding.channel_id.id)["items"]
        )
        self.assertTrue(timeline)
        self.assertTrue(all(message["actions"]["reply"] for message in timeline))
        self.assertTrue(all(message["actions"]["react"] for message in timeline))
        self.assertTrue(
            all(
                not message["actions"][action]
                for message in timeline
                for action in ("edit", "delete")
            )
        )

    def test_group_list_degrades_gracefully_when_profile_is_missing(self):
        self._process(self._event(message_id="group-list-missing-profile"))
        binding = self._group_binding()
        binding.group_profile_ids.sudo().unlink()

        page = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .list_conversations(limit=100)
        )
        item = next(
            entry
            for entry in page["items"]
            if entry["channel_id"] == binding.channel_id.id
        )

        self.assertEqual(item["conversation_type"], "group")
        self.assertEqual(item["name"], binding.channel_id.name)
        self.assertEqual(item["group"]["metadata_state"], "unavailable")
        self.assertFalse(item["group"]["participant_count"])

    def test_group_access_exposes_guest_authors_not_participant_identity_records(self):
        self._create_two_participant_group()
        binding = self._group_binding()
        participant_identities = (
            self.env["contact.center.identity"]
            .sudo()
            .search(
                [
                    (
                        "mail_guest_id",
                        "in",
                        binding.channel_id.sudo().channel_member_ids.guest_id.ids,
                    )
                ]
            )
        )
        self.assertEqual(len(participant_identities), 2)
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        conversation = api.get_conversation(binding.channel_id.id)["item"]
        timeline = api.get_timeline(binding.channel_id.id)["items"]
        self.assertEqual(conversation["conversation_type"], "group")
        self.assertEqual(
            {message["author"]["id"] for message in timeline},
            set(participant_identities.mail_guest_id.ids),
        )
        self.assertTrue(
            all(message["author"]["type"] == "guest" for message in timeline)
        )
        self.assertFalse(binding.identity_id)

        self.assertFalse(
            self.env["contact.center.identity"]
            .with_user(self.agent)
            .search([("id", "in", participant_identities.ids)])
        )
        self.assertFalse(
            self.env["contact.center.identity.alias"]
            .with_user(self.agent)
            .search([("identity_id", "in", participant_identities.ids)])
        )
        with self.assertRaises(AccessError):
            participant_identities[0].with_user(self.agent)._check_promotion_access()
        self.assertFalse(
            self.env["contact.center.identity"]
            .with_user(self.outsider)
            .search([("id", "in", participant_identities.ids)])
        )
        with self.assertRaises(AccessError):
            self.env["contact.center.ui.api"].with_user(self.outsider).get_conversation(
                binding.channel_id.id
            )
        with self.assertRaises(AccessError):
            participant_identities[0].with_user(self.outsider)._check_promotion_access()
