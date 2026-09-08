import base64
import copy
import datetime
import hashlib
import uuid
from unittest import mock

from psycopg2 import IntegrityError
from psycopg2.errors import DeadlockDetected, SerializationFailure

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import SavepointCase
from odoo.tools import mute_logger

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.job import Job
from odoo.addons.queue_job.tests.common import trap_jobs

from ..models.application import IdentityConflictError
from ..services.adapter import (
    ProviderAdapter,
    ProviderPausedError,
    ProviderRateLimitError,
    TransientAdapterError,
    adapter_registry,
)
from ..services.dto import (
    AdapterResult,
    AddressDTO,
    CommandDTO,
    EventDTO,
    MediaDownloadResult,
)
from ..services.job import (
    PROVIDER_PAUSED_RETRY_SECONDS,
    QUEUE_ATTEMPT_CEILING,
    provider_paused_retry_seconds,
)
from ..services.media import (
    enabled_media_kinds,
    validate_provider_media_capability,
    validate_provider_media_caption_capability,
)
from ..services.tokens import CONTACT_CENTER_MEMBERSHIP_TOKEN, CONTACT_CENTER_POST_TOKEN

_TEST_OPUS_HEAD = (
    b"OpusHead\x01\x01\x00\x00" + (48000).to_bytes(4, "little") + b"\x00\x00\x00"
)


def _test_ogg_page(packet, *, granule=0, header_type=2, sequence=0):
    return (
        b"OggS\x00"
        + bytes((header_type,))
        + granule.to_bytes(8, "little")
        + (1).to_bytes(4, "little")
        + sequence.to_bytes(4, "little")
        + (b"\x00" * 4)
        + b"\x01"
        + bytes((len(packet),))
        + packet
    )


def _test_recorded_ogg(duration_seconds):
    return _test_ogg_page(_TEST_OPUS_HEAD) + _test_ogg_page(
        b"\xf8\xff\xfe",
        granule=duration_seconds * 48000,
        header_type=4,
        sequence=1,
    )


@adapter_registry.register("test.fake")
class FakeAdapter(ProviderAdapter):
    display_name = "Test Fake"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return EventDTO.from_dict(envelope)

    def execute_command(self, connection, command):
        return AdapterResult.success(
            external_message_id="external-%s" % command.command_id,
            provider_response={"accepted": True},
        )

    def prepare_request_snapshot(self, connection, command):
        return {
            "provider": self.key,
            "method": "POST",
            "endpoint": "/messages",
            "payload": {
                "command_id": command.command_id,
                "command_type": command.command_type,
            },
        }

    def get_capabilities(self, connection):
        return {"send_message": True, "sender_signature": True}

    def get_health(self, connection):
        return {"state": "connected"}

    def derive_client_message_id(self, command_id):
        return "client-%s" % command_id


@adapter_registry.register(
    "test.missing", module="contact_center_missing_provider_fixture"
)
class MissingOwnerAdapter(FakeAdapter):
    """Registered selection fixture whose owning addon is intentionally absent."""

    display_name = "Test Missing Owner"


class TestContactCenter(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.admin_group = cls.env.ref("contact_center_base.group_contact_center_admin")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Contact Center Agent",
                    "login": "cc-agent-%s" % uuid.uuid4(),
                    "email": "cc-agent@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, [cls.agent_group.id])],
                }
            )
        )
        cls.admin = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Contact Center Administrator",
                    "login": "cc-admin-%s" % uuid.uuid4(),
                    "email": "cc-admin@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, [cls.admin_group.id])],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Test Team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "WhatsApp Test",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "account-test-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, cls.team.ids)],
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Fake Connection",
                "account_id": cls.account.id,
                "adapter_key": "test.fake",
                "external_ref": "connection-test-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {
                    "send_message": True,
                    "sender_signature": True,
                },
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

    def _identity(self, name="Remote Person"):
        guest = self.env["mail.guest"].sudo().create({"name": name})
        return (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": name,
                    "company_id": self.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )

    def _channel_binding(self, suffix=None):
        suffix = suffix or str(uuid.uuid4())
        identity = self._identity()
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            identity=identity,
            teams=self.team,
            partner_ids=self.agent.partner_id.ids,
            guest_ids=identity.mail_guest_id.ids,
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
                    "conversation_ref": "conversation-%s" % suffix,
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

    def _send_message_without_enqueue(self, channel, body):
        return (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(channel.id, body)
        )

    def _mark_send_dead(self, result, error=None):
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        outbox._finish_failure(
            "dead", error or ValidationError("fixture permanent failure")
        )
        outbox.invalidate_recordset()
        return outbox

    def _outbound_mutation_target(self, body="Mutation target"):
        channel, channel_binding, identity = self._channel_binding()
        result = self._send_message_without_enqueue(channel, body)
        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", result["message_id"])], limit=1)
        )
        target.write({"external_message_id": "target-%s" % uuid.uuid4()})
        return channel, channel_binding, identity, target

    def _mutation(self, target, mutation_type, occurred_at, **values):
        return (
            self.env["contact.center.message.mutation"]
            .sudo()
            .create(
                {
                    "target_message_binding_id": target.id,
                    "provider_connection_id": self.connection.id,
                    "external_event_id": "%s-%s" % (mutation_type, uuid.uuid4()),
                    "mutation_type": mutation_type,
                    "direction": "inbound",
                    "actor_partner_id": self.agent.partner_id.id,
                    "occurred_at": occurred_at,
                    **values,
                }
            )
        )

    def _process_outbox(self, outbox, job_uuid=None):
        job_uuid = job_uuid or outbox.queue_job_uuid or str(uuid.uuid4())
        if not outbox.queue_job_uuid:
            outbox.sudo().write({"queue_job_uuid": job_uuid})

        def flush_without_committing(record):
            # Production jobs commit at the durable provider boundary.  Test
            # transactions cannot commit, so flush the same writes instead.
            record.env.flush_all()
            return True

        with mock.patch.object(
            type(outbox),
            "_commit_job_transaction",
            autospec=True,
            side_effect=flush_without_committing,
        ):
            return outbox.with_context(job_uuid=job_uuid)._job_process()

    def _process_inbox(self, inbox, job_uuid=None):
        job_uuid = job_uuid or inbox.queue_job_uuid or str(uuid.uuid4())
        if not inbox.queue_job_uuid:
            inbox.sudo().write({"queue_job_uuid": job_uuid})
        return inbox.with_context(job_uuid=job_uuid)._job_process()

    def _process_media(self, media, job_uuid=None):
        job_uuid = job_uuid or media.queue_job_uuid or str(uuid.uuid4())
        if not media.queue_job_uuid:
            media.sudo().write({"queue_job_uuid": job_uuid})
        return media.with_context(job_uuid=job_uuid)._job_download()

    def _media_binding(self):
        channel, channel_binding, _identity = self._channel_binding()
        message = channel.sudo()._contact_center_post(
            origin="inbound",
            body="Inbound media retry fixture",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
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
                    "content_type": "image",
                    "external_message_id": "media-message-%s" % uuid.uuid4(),
                    "delivery_state": "delivered",
                }
            )
        )
        return (
            self.env["contact.center.media.binding"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "message_binding_id": message_binding.id,
                    "kind": "image",
                    "external_media_id": "media-%s" % uuid.uuid4(),
                    "remote_locator_json": {"path": "/fixture/media"},
                    "mime_type": "image/png",
                    "size_bytes": 1,
                }
            )
        )

    def test_factory_removes_implicit_creator_and_keeps_exact_members(self):
        channel, _binding, identity = self._channel_binding()
        members = channel.sudo().channel_member_ids
        self.assertEqual(members.partner_id, self.agent.partner_id)
        self.assertEqual(members.guest_id, identity.mail_guest_id)
        self.assertNotIn(self.env.user.partner_id, members.partner_id)
        self.assertEqual(channel.channel_type, "contact_center")
        self.assertEqual(channel.contact_center_state, "open")
        self.assertTrue(channel.avatar_128)
        self.assertNotIn(
            channel,
            self.agent.partner_id.with_user(self.agent)._get_channels_as_member(),
        )

    def test_native_membership_entrypoints_cannot_bypass_exact_factory(self):
        channel, _binding, identity = self._channel_binding()
        channel_as_agent = channel.with_user(self.agent)
        members_before = set(channel.sudo().channel_member_ids.ids)

        with self.assertRaises(AccessError):
            self.env["mail.channel"].with_user(self.agent).create(
                {
                    "name": "Bypass",
                    "channel_type": "contact_center",
                    "contact_center_company_id": self.env.company.id,
                    "contact_center_state": "open",
                }
            )
        with self.assertRaises(AccessError):
            (
                self.env["mail.channel"]
                .with_user(self.agent)
                .with_context(default_channel_type="contact_center")
                .create(
                    {
                        "name": "Context Bypass",
                        "contact_center_company_id": self.env.company.id,
                        "contact_center_state": "open",
                    }
                )
            )
        with self.assertRaises(AccessError):
            channel_as_agent.add_members(
                partner_ids=[self.env.ref("base.partner_root").id]
            )
        with self.assertRaises(AccessError):
            channel_as_agent.action_unfollow()

        extra_guest = self.env["mail.guest"].sudo().create({"name": "Bypass Guest"})
        with self.assertRaises(AccessError):
            self.env["mail.channel.member"].with_user(self.agent).create(
                {"channel_id": channel.id, "guest_id": extra_guest.id}
            )
        with self.assertRaises(AccessError):
            self.env["mail.channel.member"].sudo().create(
                {"channel_id": str(channel.id), "guest_id": extra_guest.id}
            )
        with self.assertRaises(AccessError):
            self.env["mail.channel.member"].sudo().create(
                {"channel_id": channel.id - 0.4, "guest_id": extra_guest.id}
            )
        agent_member = channel.sudo().channel_member_ids.filtered(
            lambda member: member.partner_id == self.agent.partner_id
        )
        with self.assertRaises(AccessError):
            agent_member.with_user(self.agent).unlink()
        with self.assertRaises(AccessError):
            agent_member.with_user(self.agent).write(
                {"partner_id": False, "guest_id": extra_guest.id}
            )
        native_channel = (
            self.env["mail.channel"]
            .sudo()
            .create({"name": "Native membership source", "channel_type": "group"})
        )
        native_member = native_channel.channel_member_ids[0]
        with self.assertRaises(AccessError):
            native_member.sudo().write({"channel_id": str(channel.id)})
        with self.assertRaises(AccessError):
            native_member.sudo().write({"channel_id": channel.id - 0.4})
        with self.assertRaises(AccessError):
            self.agent.partner_id.with_user(self.agent).write(
                {"channel_ids": [(3, channel.id)]}
            )
        for relation_value in (
            False,
            (),
            [channel.id],
            [self.env.ref("mail.channel_all_employees").id],
        ):
            with self.subTest(relation_value=relation_value), self.assertRaises(
                AccessError
            ):
                self.agent.partner_id.with_user(self.agent).write(
                    {"channel_ids": relation_value}
                )
        with self.assertRaises(AccessError):
            identity.mail_guest_id.sudo().write({"channel_ids": [(3, channel.id)]})
        with self.assertRaises(AccessError):
            self.env["res.partner"].sudo().create(
                {
                    "name": "Bypass Partner",
                    "channel_ids": [(4, channel.id)],
                }
            )
        with self.assertRaises(AccessError):
            self.env["res.partner"].sudo().with_context(
                default_channel_ids=[channel.id]
            ).create({"name": "Default-context bypass"})
        with self.assertRaises(AccessError):
            self.env["mail.channel.member"].sudo().with_context(
                default_channel_id=channel.id
            ).create({"guest_id": extra_guest.id})

        original_company = channel.contact_center_company_id
        original_team = channel.contact_center_access_team_ids
        original_responsible = channel.contact_center_responsible_id
        original_state = channel.contact_center_state
        for operational_values in (
            {"contact_center_company_id": False},
            {"contact_center_access_team_ids": [(5, 0, 0)]},
            {"contact_center_responsible_id": False},
            {"contact_center_state": "resolved"},
            {"contact_center_tag_ids": [(5, 0, 0)]},
        ):
            with self.subTest(values=operational_values), self.assertRaises(
                AccessError
            ):
                channel_as_agent.write(operational_values)
        with self.assertRaises(AccessError):
            channel.sudo().unlink()

        channel.invalidate_recordset(["channel_member_ids"])
        self.assertEqual(set(channel.sudo().channel_member_ids.ids), members_before)
        self.assertEqual(channel.contact_center_company_id, original_company)
        self.assertEqual(channel.contact_center_access_team_ids, original_team)
        self.assertEqual(channel.contact_center_responsible_id, original_responsible)
        self.assertEqual(channel.contact_center_state, original_state)

    def test_factory_rejects_public_or_technical_partner(self):
        identity = self._identity()
        with self.assertRaises(AccessError):
            self.env["mail.channel"]._contact_center_create_channel(
                account=self.account,
                identity=identity,
                partner_ids=[self.env.ref("base.partner_root").id],
            )
        unassigned_account = self.env["contact.center.account"].create(
            {
                "name": "Unassigned account",
                "company_id": self.env.company.id,
                "platform": "whatsapp",
                "external_ref": "unassigned-%s" % uuid.uuid4(),
            }
        )
        with self.assertRaises(ValidationError):
            self.env["mail.channel"]._contact_center_create_channel(
                account=unassigned_account,
                identity=identity,
            )

    def test_native_channel_has_no_contact_center_state(self):
        channel = self.env["mail.channel"].create(
            {"name": "Native Group", "channel_type": "group"}
        )
        self.assertFalse(channel.contact_center_state)

    def test_stable_account_and_connection_references_are_immutable(self):
        with self.assertRaises(ValidationError):
            self.account.write({"external_ref": "changed-account-ref"})
        with self.assertRaises(ValidationError):
            self.connection.write({"external_ref": "changed-connection-ref"})
        with self.assertRaises(ValidationError):
            self.connection.write({"adapter_key": "another.adapter"})

    def test_team_rejects_internal_users_without_contact_center_group(self):
        user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Internal Non Agent",
                    "login": "non-agent-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
                }
            )
        )
        with self.assertRaises(ValidationError):
            self.env["contact.center.team"].create(
                {
                    "name": "Invalid Team",
                    "company_id": self.env.company.id,
                    "agent_ids": [(6, 0, user.ids)],
                }
            )
        fresh_agent = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Agent without conversation",
                    "login": "fresh-agent-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, self.agent_group.ids)],
                }
            )
        )
        self.env["contact.center.team"].create(
            {
                "name": "Team without conversation",
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, fresh_agent.ids)],
            }
        )
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env["res.users"].with_context(no_reset_password=True).create(
                {
                    "name": "Shared partner before conversation",
                    "login": "fresh-shared-agent-%s" % uuid.uuid4(),
                    "partner_id": fresh_agent.partner_id.id,
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, self.agent_group.ids)],
                }
            )

    def test_team_roster_reconciles_membership_and_blocks_stale_grants(self):
        channel, _binding, identity = self._channel_binding()
        supervisor_group = self.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        supervisor = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Contact Center Supervisor",
                    "login": "cc-supervisor-%s" % uuid.uuid4(),
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, [supervisor_group.id])],
                }
            )
        )

        self.team.write({"supervisor_ids": [(6, 0, supervisor.ids)]})
        channel.invalidate_recordset(["channel_member_ids"])
        self.assertEqual(
            set(channel.channel_member_ids.partner_id.ids),
            {self.agent.partner_id.id, supervisor.partner_id.id},
        )
        self.assertEqual(channel.channel_member_ids.guest_id, identity.mail_guest_id)

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.agent.write({"active": False})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.agent.write({"company_ids": [(5, 0, 0)]})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env.company.write({"user_ids": [(3, self.agent.id)]})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.agent.write({"in_group_%s" % self.agent_group.id: False})
        with self.assertRaises(AccessError):
            self.agent_group.write({"users": [(3, self.agent.id)]})
        with self.assertRaises(AccessError):
            self.admin_group.write({"users": [(3, self.agent.id)]})
        with self.assertRaises(AccessError):
            self.env.ref("base.group_system").write({"users": [(3, self.agent.id)]})
        with self.assertRaises(AccessError):
            self.agent.unlink()
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env["res.users"].with_context(no_reset_password=True).create(
                {
                    "name": "Shared Partner Without CC Grant",
                    "login": "cc-shared-partner-%s" % uuid.uuid4(),
                    "partner_id": self.agent.partner_id.id,
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
                }
            )
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env["res.users"].with_context(no_reset_password=True).create(
                {
                    "name": "Shared Partner With CC Role",
                    "login": "cc-shared-agent-%s" % uuid.uuid4(),
                    "partner_id": self.agent.partner_id.id,
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, [self.agent_group.id])],
                }
            )
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            supervisor.write({"partner_id": self.agent.partner_id.id})
        with self.assertRaisesRegex(ValidationError, "different Odoo users"):
            with self.env.cr.savepoint():
                self.env["base.partner.merge.automatic.wizard"]._merge(
                    [self.agent.partner_id.id, supervisor.partner_id.id],
                    dst_partner=supervisor.partner_id,
                )
        original_agent_partner = self.agent.partner_id
        merge_target = self.env["res.partner"].create({"name": "Merge Target"})
        self.env["base.partner.merge.automatic.wizard"]._merge(
            [original_agent_partner.id, merge_target.id],
            dst_partner=merge_target,
        )
        self.agent.invalidate_recordset(["partner_id"])
        channel.invalidate_recordset(["channel_member_ids"])
        self.assertFalse(original_agent_partner.exists())
        self.assertEqual(self.agent.partner_id, merge_target)
        self.assertIn(merge_target, channel.channel_member_ids.partner_id)

        channel.with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write({"active": False})
        self.team.write({"agent_ids": [(5, 0, 0)]})
        channel.invalidate_recordset(["channel_member_ids"])
        self.assertEqual(channel.channel_member_ids.partner_id, supervisor.partner_id)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.team.write({"supervisor_ids": [(5, 0, 0)]})
        self.agent.write({"active": False})
        self.assertFalse(self.agent.active)

        other_company = self.env["res.company"].create({"name": "Other CC Company"})
        with self.assertRaises(ValidationError):
            self.team.write({"company_id": other_company.id})
        with self.assertRaises(ValidationError):
            self.team.write({"active": False})
        with self.assertRaises(ValidationError):
            self.team.unlink()
        archived_team = self.env["contact.center.team"].create(
            {"name": "Archived team", "company_id": self.env.company.id}
        )
        archived_team.write({"active": False})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.account.write({"access_team_ids": [(6, 0, archived_team.ids)]})
        tag = self.env["contact.center.tag"].create(
            {"name": "Immutable company", "company_id": self.env.company.id}
        )
        with self.assertRaises(ValidationError):
            tag.write({"company_id": other_company.id})

    def test_generic_comment_is_rejected_but_note_is_allowed(self):
        channel, _binding, _identity = self._channel_binding()
        channel_as_agent = channel.with_user(self.agent)
        outbox_before = self.env["contact.center.outbox.command"].search_count([])
        notification_before = self.env["mail.notification"].sudo().search_count([])
        mail_before = self.env["mail.mail"].sudo().search_count([])
        with self.assertRaises(UserError):
            channel_as_agent.message_post(
                body="This must not look sent",
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
            )
        with self.assertRaises(UserError):
            channel_as_agent.message_post(
                body="This must not become email",
                message_type="email",
                partner_ids=self.env.user.partner_id.ids,
            )
        custom_subtype = self.env["mail.message.subtype"].create(
            {"name": "Contact Center forbidden subtype"}
        )
        with self.assertRaises(UserError):
            channel_as_agent.message_post(
                body="This subtype is not a note",
                message_type="comment",
                subtype_id=custom_subtype.id,
            )
        with self.assertRaises(UserError):
            channel_as_agent.message_post(
                body="This note cannot impersonate another author",
                message_type="comment",
                author_id=self.env.user.partner_id.id,
            )
        note = channel_as_agent.message_post(
            body="Internal note", message_type="comment"
        )
        self.assertEqual(note.subtype_id, self.env.ref("mail.mt_note"))
        self.assertEqual(
            self.env["contact.center.outbox.command"].search_count([]), outbox_before
        )
        self.assertEqual(
            self.env["mail.notification"].sudo().search_count([]),
            notification_before,
        )
        self.assertEqual(self.env["mail.mail"].sudo().search_count([]), mail_before)

    def test_native_message_crud_cannot_bypass_application_service(self):
        channel, _binding, _identity = self._channel_binding()
        comment_subtype = self.env.ref("mail.mt_comment")
        message_model = self.env["mail.message"].with_user(self.agent)
        direct_values = {
            "model": "mail.channel",
            "res_id": channel.id,
            "body": "Direct bypass",
            "message_type": "comment",
            "subtype_id": comment_subtype.id,
        }
        with self.assertRaises(AccessError):
            message_model.create(direct_values)
        fractional_target = dict(direct_values)
        fractional_target["res_id"] = channel.id - 0.4
        with self.assertRaises(AccessError):
            message_model.create(fractional_target)
        with self.assertRaises(AccessError):
            message_model.create(
                {
                    "model": "mail.channel",
                    "res_id": channel.id,
                    "body": "Direct email bypass",
                    "message_type": "email",
                }
            )
        with self.assertRaises(AccessError):
            message_model.with_context(
                default_model="mail.channel",
                default_res_id=channel.id,
                default_message_type="comment",
                default_subtype_id=comment_subtype.id,
            ).create({"body": "Default-context bypass"})

        note = channel.with_user(self.agent).message_post(
            body="Internal note", message_type="comment"
        )
        with self.assertRaises(AccessError):
            note.with_user(self.agent).write({"subtype_id": comment_subtype.id})
        with self.assertRaises(AccessError):
            note.with_user(self.agent).write({"subtype_id": str(comment_subtype.id)})
        with self.assertRaises(AccessError):
            note.with_user(self.agent).write({"message_type": "email"})

        external = channel.with_user(self.agent)._contact_center_post(
            origin="outbound",
            body="Immutable external message",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        with self.assertRaises(AccessError):
            external.with_user(self.agent).write({"body": "Tampered"})
        with self.assertRaises(AccessError):
            external.with_user(self.agent).unlink()

        external.with_user(self.agent).write(
            {"starred_partner_ids": [(4, self.agent.partner_id.id)]}
        )
        self.assertIn(self.agent.partner_id, external.starred_partner_ids)

    def test_native_channel_matrix_blocks_local_reaction_and_native_archive(self):
        channel, _binding, _identity = self._channel_binding()
        channel_as_agent = channel.with_user(self.agent)
        info = channel_as_agent.channel_info()[0]
        self.assertEqual(info["channel"]["channel_type"], "contact_center")
        self.assertEqual(channel_as_agent._channel_fetch_message(), [])
        for method_name, argument in (
            ("channel_pin", True),
            ("channel_fold", "open"),
            ("channel_set_custom_name", "Discuss alias"),
            ("channel_rename", "Discuss rename"),
            ("channel_change_description", "Discuss description"),
        ):
            with self.assertRaises(AccessError):
                getattr(channel_as_agent, method_name)(argument)
        message = channel_as_agent._contact_center_post(
            origin="outbound",
            body="Attachment",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
            attachments=[("evidence.txt", b"content")],
        )
        self.assertEqual(message.attachment_ids.name, "evidence.txt")
        attachment = message.attachment_ids
        with self.assertRaises(AccessError):
            attachment.with_user(self.agent).write({"name": "tampered.txt"})
        with self.assertRaises(AccessError):
            attachment.with_user(self.agent).unlink()
        self.assertEqual(attachment.name, "evidence.txt")
        outbox_before = self.env["contact.center.outbox.command"].search_count([])
        with self.assertRaises(AccessError):
            message.with_user(self.agent)._message_add_reaction("👍")
        self.assertFalse(message.sudo().reaction_ids)
        self.env["mail.message.reaction"].sudo().create(
            {
                "message_id": message.id,
                "partner_id": self.agent.partner_id.id,
                "content": "👍",
            }
        )
        with self.assertRaises(AccessError):
            message.with_user(self.agent)._message_remove_reaction("👍")
        self.assertTrue(message.sudo().reaction_ids)
        self.assertEqual(
            self.env["contact.center.outbox.command"].search_count([]), outbox_before
        )
        with self.assertRaises(AccessError):
            channel_as_agent.write({"active": False})
        with self.assertRaises(AccessError):
            channel_as_agent.action_archive()
        with self.assertRaises(AccessError):
            channel_as_agent.with_context(
                contact_center_membership_token="forged-rpc-token"
            ).write({"active": False})
        channel.invalidate_recordset(["active"])
        self.assertTrue(channel.active)
        self.env["contact.center.ui.api"].with_user(self.agent).update_conversation(
            channel.id, {"state": "archived"}
        )
        self.assertEqual(channel.contact_center_state, "archived")
        self.assertTrue(channel.active)
        explicit = channel_as_agent._contact_center_post(
            origin="outbound",
            body="Explicit but not queued by message_post",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        self.assertEqual(
            channel._channel_message_notifications(explicit, message_format={}), []
        )
        self.assertEqual(
            self.env["contact.center.outbox.command"].search_count([]), outbox_before
        )

    def test_inbound_creates_guest_message_without_partner_or_native_notification(self):
        partner_count = self.env["res.partner"].search_count([])
        outbox_count = self.env["contact.center.outbox.command"].search_count([])
        actor_key = str(uuid.uuid4())
        conversation_key = str(uuid.uuid4())
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "event-%s" % actor_key,
                "event_type": "message.created",
                "occurred_at": "2026-08-21T13:00:00Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": "conversation-%s" % conversation_key,
                "platform": "whatsapp",
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {
                    "display_name": "Unknown Sender",
                    "addresses": [
                        {
                            "namespace": "whatsapp.lid",
                            "value": "%s@lid" % actor_key,
                            "value_normalized": "%s@lid" % actor_key,
                            "role": "primary",
                        }
                    ],
                },
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [
                        {
                            "namespace": "whatsapp.pn",
                            "value": "%s@s.whatsapp.net" % conversation_key,
                            "value_normalized": "%s@s.whatsapp.net" % conversation_key,
                            "role": "primary",
                        }
                    ],
                },
                "message": {
                    "external_message_id": "message-%s" % actor_key,
                    "content_type": "text",
                    "text": "Inbound text",
                },
            }
        )
        application = self.env["contact.center.application"]
        with mock.patch.object(
            type(application), "_notify_ui", autospec=True
        ) as notify_ui:
            message = application._process_event(self.connection, event)
        message_created_payloads = [
            call.args[3]
            for call in notify_ui.call_args_list
            if call.args[2] == "message_created"
        ]
        self.assertEqual(
            message_created_payloads,
            [{"message_id": message.id, "direction": "inbound"}],
        )
        self.assertEqual(self.env["res.partner"].search_count([]), partner_count)
        self.assertTrue(message.author_guest_id)
        self.assertFalse(message.author_id)
        binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", message.id)]
        )
        self.assertTrue(binding)
        self.assertFalse(binding.is_forwarded)
        self.assertFalse(binding.forwarding_score_observed)

        replay_values = event.to_dict()
        replay_values["message"].update({"is_forwarded": True, "forwarding_score": 1})
        replayed = self.env["contact.center.application"]._process_event(
            self.connection, EventDTO.from_dict(replay_values)
        )
        self.assertEqual(replayed, message)
        binding.invalidate_recordset(
            ["is_forwarded", "forwarding_score", "forwarding_score_observed"]
        )
        self.assertTrue(binding.is_forwarded)
        self.assertEqual(binding.forwarding_score, 1)
        self.assertTrue(binding.forwarding_score_observed)
        serialized = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._serialize_message(message, binding)
        )
        self.assertTrue(serialized["is_forwarded"])

        stale_replay = event.to_dict()
        stale_replay["message"].update({"is_forwarded": False, "forwarding_score": 0})
        self.env["contact.center.application"]._process_event(
            self.connection, EventDTO.from_dict(stale_replay)
        )
        binding.invalidate_recordset(
            ["is_forwarded", "forwarding_score", "forwarding_score_observed"]
        )
        self.assertTrue(binding.is_forwarded)
        self.assertEqual(binding.forwarding_score, 1)
        self.assertFalse(
            self.env["mail.notification"]
            .sudo()
            .search([("mail_message_id", "=", message.id)])
        )
        self.assertFalse(
            self.env["mail.mail"].sudo().search([("mail_message_id", "=", message.id)])
        )
        self.assertEqual(
            self.env["contact.center.outbox.command"].search_count([]), outbox_count
        )
        message_count = self.env["mail.message"].search_count(
            [("model", "=", "mail.channel")]
        )
        missing_route_values = event.to_dict()
        missing_route_values["event_id"] = "missing-route-%s" % uuid.uuid4()
        missing_route_values["conversation"] = {
            "conversation_type": "direct",
            "addresses": [],
        }
        with self.assertRaises(ValidationError):
            self.env["contact.center.application"]._process_event(
                self.connection, EventDTO.from_dict(missing_route_values)
            )
        for index in range(2):
            missing_id_values = event.to_dict()
            missing_id_values["event_id"] = "missing-message-id-%s-%s" % (
                index,
                uuid.uuid4(),
            )
            missing_id_values["message"]["external_message_id"] = ""
            with self.assertRaises(ValidationError):
                self.env["contact.center.application"]._process_event(
                    self.connection, EventDTO.from_dict(missing_id_values)
                )
        self.assertEqual(
            self.env["mail.message"].search_count([("model", "=", "mail.channel")]),
            message_count,
        )

    def test_send_message_is_atomic_and_capability_gated(self):
        channel, _binding, _identity = self._channel_binding()
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        application = self.env["contact.center.application"]
        message_count = self.env["mail.message"].search_count(
            [("model", "=", "mail.channel"), ("res_id", "=", channel.id)]
        )
        with trap_jobs() as trap:
            self.connection.capabilities_json = {"send_message": False}
            with self.assertRaises(UserError):
                api.send_message(channel.id, "Blocked")
            trap.assert_jobs_count(0)
            self.assertEqual(
                self.env["mail.message"].search_count(
                    [("model", "=", "mail.channel"), ("res_id", "=", channel.id)]
                ),
                message_count,
            )

            self.connection.capabilities_json = {"send_message": True}
            with mock.patch.object(
                type(application), "_notify_ui", autospec=True
            ) as notify_ui:
                result = api.send_message(channel.id, "Queued")
            message_created_payloads = [
                call.args[3]
                for call in notify_ui.call_args_list
                if call.args[2] == "message_created"
            ]
            self.assertEqual(
                message_created_payloads,
                [{"message_id": result["message_id"], "direction": "outbound"}],
            )
            outbox = self.env["contact.center.outbox.command"].browse(
                result["outbox_command_id"]
            )
            trap.assert_jobs_count(1)
            trap.assert_enqueued_job(
                outbox.sudo().with_company(outbox.company_id)._job_process,
                properties={
                    "identity_key": "contact_center:outbox:%s" % outbox.id,
                    "max_retries": 0,
                    "description": "Contact Center outbox command %s" % outbox.id,
                },
            )
            queued_job = trap.enqueued_jobs[0]
            self.assertEqual(queued_job.channel, "root.contact_center.outbox")
            self.assertEqual(outbox.queue_job_uuid, queued_job.uuid)
        self.assertEqual(outbox.state, "pending")
        self.assertEqual(outbox.message_binding_id.message_id.id, result["message_id"])
        self.assertTrue(
            outbox.message_binding_id.client_message_id.startswith("client-")
        )
        self.assertFalse(
            self.env["mail.notification"]
            .sudo()
            .search([("mail_message_id", "=", result["message_id"])])
        )

    def test_primary_switch_waits_for_active_outbound_command(self):
        channel, _binding, _identity = self._channel_binding()
        result = self._send_message_without_enqueue(channel, "Pending before cutover")
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        replacement = self.connection.copy(
            {
                "name": "Cutover candidate",
                "external_ref": "cutover-%s" % uuid.uuid4(),
            }
        )

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            replacement.action_use_as_primary()

        self.assertEqual(self.connection.role, "primary")
        self.assertEqual(replacement.role, "standby")
        outbox.write({"state": "done", "processed_at": fields.Datetime.now()})

        replacement.action_use_as_primary()
        self.connection.invalidate_recordset(
            ["role", "inbound_active", "outbound_active"]
        )
        replacement.invalidate_recordset(["role", "inbound_active", "outbound_active"])
        self.assertEqual(self.connection.role, "standby")
        self.assertEqual(replacement.role, "primary")

    def test_direct_primary_demotions_wait_for_active_outbound_command(self):
        channel, _binding, _identity = self._channel_binding()
        self._send_message_without_enqueue(channel, "Pending before direct demotion")

        for action_name in (
            "action_set_standby",
            "action_set_migration",
            "action_set_historical",
        ):
            with self.subTest(action=action_name):
                with self.assertRaises(ValidationError), self.env.cr.savepoint():
                    getattr(self.connection, action_name)()
                self.connection.invalidate_recordset(
                    ["active", "role", "inbound_active", "outbound_active"]
                )
                self.assertTrue(self.connection.active)
                self.assertEqual(self.connection.role, "primary")
                self.assertTrue(self.connection.inbound_active)
                self.assertTrue(self.connection.outbound_active)

    def test_outbound_admission_revision_changes_once_per_durable_command(self):
        channel, _binding, _identity = self._channel_binding()
        request_id = str(uuid.uuid4())
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        before = self.connection.outbound_admission_revision
        account_revision = self.account.access_topology_revision

        first = api.send_message(
            channel.id,
            "Revision barrier",
            client_request_id=request_id,
        )
        self.connection.invalidate_recordset(["outbound_admission_revision"])
        self.account.invalidate_recordset(["access_topology_revision"])
        self.assertEqual(self.connection.outbound_admission_revision, before + 1)
        self.assertEqual(self.account.access_topology_revision, account_revision)

        replay = api.send_message(
            channel.id,
            "Revision barrier",
            client_request_id=request_id,
        )
        self.connection.invalidate_recordset(["outbound_admission_revision"])
        self.account.invalidate_recordset(["access_topology_revision"])
        self.assertEqual(replay["outbox_command_id"], first["outbox_command_id"])
        self.assertEqual(self.connection.outbound_admission_revision, before + 1)
        self.assertEqual(self.account.access_topology_revision, account_revision)

    def test_delete_replay_is_idempotent_after_successful_projection(self):
        channel, _binding, _identity, target = self._outbound_mutation_target()
        self.connection.capabilities_json = {
            "send_message": True,
            "delete_message": True,
        }
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        request_id = str(uuid.uuid4()).upper()
        canonical_request_id = str(uuid.UUID(request_id))
        self.connection.invalidate_recordset(["outbound_admission_revision"])
        before = self.connection.outbound_admission_revision

        first = api.delete_message(
            channel.id,
            target.message_id.id,
            request_id,
        )
        self.assertEqual(first["channel_id"], channel.id)
        self.assertEqual(first["client_request_id"], canonical_request_id)
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(first["outbox_command_id"])
        )
        mutation_id = outbox.mutation_id.id
        self._process_outbox(outbox)
        outbox.invalidate_recordset(["state"])
        target.invalidate_recordset(["message_state"])
        self.assertEqual(outbox.state, "done")
        self.assertEqual(target.message_state, "deleted")

        replay = api.delete_message(
            channel.id,
            target.message_id.id,
            request_id,
        )

        self.assertEqual(replay["outbox_command_id"], outbox.id)
        self.assertEqual(replay["state"], "done")
        self.assertEqual(replay["channel_id"], channel.id)
        self.assertEqual(replay["client_request_id"], outbox.ui_request_id)
        self.assertEqual(replay["message"]["message_id"], target.message_id.id)
        self.assertEqual(outbox.mutation_id.id, mutation_id)
        self.assertEqual(
            self.env["contact.center.message.mutation"]
            .sudo()
            .search_count([("client_request_id", "=", canonical_request_id)]),
            1,
        )
        self.connection.invalidate_recordset(["outbound_admission_revision"])
        self.assertEqual(self.connection.outbound_admission_revision, before + 1)

    def test_direct_actions_fail_closed_across_provider_cutover(self):
        channel, _binding, _identity, target = self._outbound_mutation_target()
        capabilities = {
            "send_message": True,
            "reply": True,
            "react": True,
            "edit_message": True,
            "delete_message": True,
        }
        self.connection.capabilities_json = capabilities
        self.account.technical_author_id = self.agent.partner_id
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )

        # Complete both commands on the original provider so cutover is allowed
        # and the exact mutation replay represents a terminal durable result.
        target.external_message_id = False
        original_send = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search(
                [
                    ("message_binding_id", "=", target.id),
                    ("command_type", "=", "send_message"),
                ],
                limit=1,
            )
        )
        self.assertTrue(original_send)
        self._process_outbox(original_send)
        target.invalidate_recordset(["external_message_id", "delivery_state"])
        self.assertTrue(target.external_message_id)
        self.assertEqual(target.delivery_state, "sent")

        replay_request_id = str(uuid.uuid4())
        original_edit = api.edit_message(
            channel.id,
            target.message_id.id,
            "Edited before provider cutover",
            replay_request_id,
        )
        original_edit_outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(original_edit["outbox_command_id"])
        )
        self._process_outbox(original_edit_outbox)
        original_edit_outbox.invalidate_recordset(["state"])
        target.invalidate_recordset(["message_state"])
        self.assertEqual(original_edit_outbox.state, "done")
        self.assertEqual(target.message_state, "edited")

        replacement = self.env["contact.center.provider.connection"].create(
            {
                "name": "Direct affinity replacement",
                "account_id": self.account.id,
                "adapter_key": "test.fake",
                "external_ref": "direct-affinity-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "standby",
                "inbound_active": False,
                "outbound_active": False,
                "capabilities_json": capabilities,
                "last_state_observed_at": fields.Datetime.now(),
                "last_state_source": "health_job",
            }
        )
        replacement.action_use_as_primary()
        replacement.write({"outbound_active": True})
        self.account.invalidate_recordset(["connection_ids"])
        (self.connection | replacement).invalidate_recordset(
            ["active", "role", "inbound_active", "outbound_active"]
        )
        self.assertEqual(self.connection.role, "standby")
        self.assertEqual(replacement.role, "primary")
        self.assertTrue(replacement.outbound_active)

        # Stable idempotency wins over current provider affinity: the old request
        # can still recover its already-completed result without new provider I/O.
        replay = api.edit_message(
            channel.id,
            target.message_id.id,
            "Edited before provider cutover",
            replay_request_id,
        )
        self.assertEqual(replay["outbox_command_id"], original_edit_outbox.id)
        self.assertEqual(replay["state"], "done")
        self.assertEqual(
            original_edit_outbox.provider_connection_id,
            self.connection,
        )

        serialized = api._serialize_message(target.message_id, target)
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

        rejected_actions = {
            "reply": lambda: api.send_message(
                channel.id,
                "Reply after provider cutover",
                reply_to_message_id=target.message_id.id,
                client_request_id=str(uuid.uuid4()),
            ),
            "edit": lambda: api.edit_message(
                channel.id,
                target.message_id.id,
                "Edit after provider cutover",
                str(uuid.uuid4()),
            ),
            "react": lambda: api.react_message(
                channel.id,
                target.message_id.id,
                "👍",
                "add",
                str(uuid.uuid4()),
            ),
            "delete": lambda: api.delete_message(
                channel.id,
                target.message_id.id,
                str(uuid.uuid4()),
            ),
        }
        for action_name, action in rejected_actions.items():
            with self.subTest(action=action_name):
                with self.assertRaises(ValidationError), self.env.cr.savepoint():
                    action()

        fresh = api.send_message(
            channel.id,
            "New message after provider cutover",
            client_request_id=str(uuid.uuid4()),
        )
        fresh_outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(fresh["outbox_command_id"])
        )
        self.assertEqual(fresh_outbox.provider_connection_id, replacement)
        self.assertEqual(fresh_outbox.state, "pending")
        self.assertFalse(fresh_outbox.message_binding_id.reply_to_binding_id)

    def test_outbound_edit_rejects_non_text_content_without_new_commands(self):
        channel, _binding, _identity, target = self._outbound_mutation_target()
        self.connection.capabilities_json = {
            "send_message": True,
            "edit_message": True,
            "delete_message": True,
        }
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        outbox_model = self.env["contact.center.outbox.command"].sudo()
        mutation_model = self.env["contact.center.message.mutation"].sudo()
        outbox_count = outbox_model.search_count([])
        mutation_count = mutation_model.search_count([])
        original_body = target.message_id.body
        admission_revision = self.connection.outbound_admission_revision
        for content_type in (
            "image",
            "audio",
            "video",
            "document",
            "location",
            "contact",
            "sticker",
            "poll",
            "interactive",
            "text",
        ):
            with self.subTest(content_type=content_type):
                target.content_type = content_type
                if content_type == "text":
                    # A text label cannot make a message with media editable.
                    self.env["contact.center.media.binding"].sudo().with_context(
                        contact_center_skip_enqueue=True
                    ).create({"message_binding_id": target.id, "kind": "image"})
                message = api._serialize_message(target.message_id, target)
                self.assertFalse(message["actions"]["edit"])
                self.assertTrue(message["actions"]["delete"])
                with self.assertRaisesRegex(UserError, "Only text messages"):
                    api.edit_message(
                        channel.id,
                        target.message_id.id,
                        "Replacement caption",
                        str(uuid.uuid4()),
                    )
                self.assertEqual(outbox_model.search_count([]), outbox_count)
                self.assertEqual(mutation_model.search_count([]), mutation_count)
                self.assertEqual(target.message_id.body, original_body)
                self.assertEqual(
                    self.connection.outbound_admission_revision, admission_revision
                )

    def test_outbound_text_edit_remains_available_and_dispatches(self):
        channel, _binding, _identity, target = self._outbound_mutation_target()
        self.connection.capabilities_json = {
            "send_message": True,
            "edit_message": True,
        }
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        self.assertTrue(
            api._serialize_message(target.message_id, target)["actions"]["edit"]
        )
        result = api.edit_message(
            channel.id, target.message_id.id, "Corrected text", str(uuid.uuid4())
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        self._process_outbox(outbox)
        self.assertEqual(outbox.state, "done")
        self.assertEqual(target.message_state, "edited")
        self.assertEqual(api._body_text(target.message_id.body), "Corrected text")

    def test_queued_media_edit_is_rejected_before_provider_dispatch(self):
        channel, _binding, _identity, target = self._outbound_mutation_target()
        self.connection.capabilities_json = {
            "send_message": True,
            "edit_message": True,
        }
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .edit_message(
                channel.id, target.message_id.id, "Invalid caption", str(uuid.uuid4())
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        # Reproduce a media edit already admitted before the content guard existed.
        target.content_type = "image"
        original_body = target.message_id.body
        with mock.patch.object(
            FakeAdapter, "execute_command", autospec=True
        ) as execute, mock.patch.object(
            FakeAdapter, "prepare_request_snapshot", autospec=True
        ) as prepare:
            self._process_outbox(outbox)
        execute.assert_not_called()
        prepare.assert_not_called()
        self.assertEqual(outbox.state, "dead")
        self.assertEqual(outbox.mutation_id.state, "failed")
        self.assertIn(
            "target_message_binding.editable_content", outbox.last_error_message
        )
        self.assertFalse(outbox.dispatch_started_at)
        self.assertEqual(target.message_state, "active")
        self.assertEqual(target.message_id.body, original_body)

    def test_optional_agent_signature_only_changes_provider_text(self):
        channel, binding, _identity = self._channel_binding()
        self.account.outbound_signature_enabled = True
        self.connection.capabilities_json = {
            **(self.connection.capabilities_json or {}),
            "edit_message": True,
        }
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )

        send_request_id = str(uuid.uuid4())
        result = api.send_message(
            channel.id,
            "Mensagem para o cliente",
            client_request_id=send_request_id,
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        message = outbox.message_binding_id.message_id
        self.assertEqual(
            outbox.command_json["message"]["text"],
            "Mensagem para o cliente",
        )
        self.assertEqual(
            outbox.command_json["options"]["sender_signature"],
            {"display_name": "Contact Center Agent"},
        )
        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(message.body),
            "Mensagem para o cliente",
        )
        self.account.outbound_signature_enabled = False
        self.agent.sudo().name = "Agente Renomeado"
        replay = api.send_message(
            channel.id,
            "Mensagem para o cliente",
            client_request_id=send_request_id,
        )
        self.assertEqual(replay["outbox_command_id"], result["outbox_command_id"])
        self.account.outbound_signature_enabled = True
        self.agent.sudo().name = "Contact Center Agent"
        self.assertEqual(
            self.env["contact.center.application"]
            .with_user(self.agent)
            ._outbound_signature(self.account, ""),
            {},
            "media-only sends must not gain an artificial caption",
        )

        outbox.message_binding_id.external_message_id = "signature-target"
        edit_request_id = str(uuid.uuid4()).upper()
        canonical_edit_request_id = str(uuid.UUID(edit_request_id))
        edited = api.edit_message(
            channel.id,
            message.id,
            "Texto corrigido",
            edit_request_id,
        )
        self.assertEqual(edited["channel_id"], channel.id)
        self.assertEqual(edited["client_request_id"], canonical_edit_request_id)
        edit_outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(edited["outbox_command_id"])
        )
        self.assertEqual(
            edit_outbox.command_json["options"]["new_text"],
            "Texto corrigido",
        )
        self.assertEqual(
            edit_outbox.command_json["options"]["sender_signature"],
            {"display_name": "Contact Center Agent"},
        )
        self.assertEqual(edit_outbox.command_json["message"]["text"], "Texto corrigido")
        self.assertEqual(edit_outbox.mutation_id.new_text, "Texto corrigido")

        self.account.outbound_signature_enabled = False
        self.agent.sudo().name = "Agente Renomeado"
        edit_replay = api.edit_message(
            channel.id,
            message.id,
            "Texto corrigido",
            edit_request_id,
        )
        self.assertEqual(edit_replay["outbox_command_id"], edited["outbox_command_id"])
        self.account.outbound_signature_enabled = True
        self.agent.sudo().name = "Contact Center Agent"

        edit_outbox.sudo().write({"state": "processing"})
        edit_outbox.mutation_id._apply_projection()
        newer_edit = api.edit_message(
            channel.id,
            message.id,
            "Texto mais novo",
            str(uuid.uuid4()),
        )
        newer_outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(newer_edit["outbox_command_id"])
        )
        newer_outbox.sudo().write({"state": "done"})
        newer_outbox.mutation_id._apply_projection()
        conversation_addresses = [
            {
                "namespace": alias.namespace,
                "value": alias.value_raw,
                "value_normalized": alias.value_normalized,
                "role": alias.role,
            }
            for alias in binding.alias_ids
        ]
        signed_echo = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": self.connection.provider_schema_version,
                "event_id": "signed-edit-echo-%s" % uuid.uuid4(),
                "event_type": "message.updated",
                "occurred_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
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
                    "addresses": conversation_addresses,
                },
                "mutation": {
                    "type": "edit",
                    "target_external_message_id": "signature-target",
                    "new_text": "*Contact Center Agent:*\nTexto corrigido",
                },
            }
        )

        def signed_wire_match(_adapter, _connection, command, event_mutation):
            return event_mutation.get("new_text") == "*Contact Center Agent:*\n%s" % (
                (command.options or {}).get("new_text") or ""
            )

        with mock.patch.object(
            FakeAdapter,
            "matches_outbound_mutation_echo",
            autospec=True,
            side_effect=signed_wire_match,
        ) as matches_echo:
            with self.assertRaisesRegex(
                TransientAdapterError, "waiting for dispatch finalization"
            ), self.env.cr.savepoint():
                self.env["contact.center.application"]._apply_inbound_mutation(
                    self.connection, signed_echo
                )
            edit_outbox.mutation_id.invalidate_recordset(["external_event_id"])
            self.assertFalse(edit_outbox.mutation_id.external_event_id)
            edit_outbox.sudo().write(
                {
                    "state": "uncertain",
                    "last_error_class": "AmbiguousTimeoutError",
                    "last_error_message": "provider outcome unknown",
                }
            )
            self.env["contact.center.application"]._apply_inbound_mutation(
                self.connection, signed_echo
            )

        self.assertEqual(matches_echo.call_count, 4)
        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(message.body),
            "Texto mais novo",
            "a delayed older wire echo must not revert the newer clean edit",
        )
        edit_outbox.invalidate_recordset(
            [
                "state",
                "provider_response_json",
                "last_error_class",
                "last_error_message",
            ]
        )
        edit_outbox.mutation_id.invalidate_recordset(["external_event_id"])
        self.assertEqual(edit_outbox.state, "done")
        self.assertEqual(
            edit_outbox.provider_response_json["reconciled_by"],
            "from_me_mutation_echo",
        )
        self.assertEqual(
            edit_outbox.mutation_id.external_event_id,
            signed_echo.event_id,
        )
        self.assertFalse(newer_outbox.mutation_id.external_event_id)
        self.assertEqual(
            self.env["contact.center.message.mutation"]
            .sudo()
            .search_count(
                [
                    (
                        "target_message_binding_id",
                        "=",
                        edit_outbox.target_message_binding_id.id,
                    )
                ]
            ),
            2,
            "the provider echo confirms the clean mutation instead of duplicating it",
        )

    def test_inbox_create_enqueues_oca_job_with_stable_identity(self):
        with trap_jobs() as trap:
            inbox = (
                self.env["contact.center.inbox.event"]
                .sudo()
                .create(
                    {
                        "provider_connection_id": self.connection.id,
                        "inbox_dedupe_key": "queued-event-%s" % uuid.uuid4(),
                        "provider_schema_version": "fixture-v1",
                        "raw_envelope_json": {"fixture": True},
                    }
                )
            )
            trap.assert_jobs_count(1)
            trap.assert_enqueued_job(
                inbox.sudo().with_company(inbox.company_id)._job_process,
                properties={
                    "identity_key": "contact_center:inbox:%s" % inbox.id,
                    "max_retries": 0,
                    "description": "Contact Center inbox event %s" % inbox.id,
                },
            )
            queued_job = trap.enqueued_jobs[0]
            self.assertEqual(queued_job.channel, "root.contact_center.inbox")
            self.assertEqual(inbox.queue_job_uuid, queued_job.uuid)
        self.assertEqual(inbox.state, "pending")

    def test_failed_media_can_be_manually_retried_from_a_clean_attempt_budget(self):
        channel, _binding, _identity = self._channel_binding()
        result = self._send_message_without_enqueue(channel, "Media placeholder")
        message_binding = (
            self.env["contact.center.outbox.command"]
            .browse(result["outbox_command_id"])
            .message_binding_id
        )
        media = (
            self.env["contact.center.media.binding"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "message_binding_id": message_binding.id,
                    "kind": "image",
                    "mime_type": "image/png",
                    "file_name": "retry.png",
                    "state": "failed",
                    "attempts": 13,
                    "queue_job_uuid": str(uuid.uuid4()),
                    "last_error_class": "TransientAdapterError",
                    "last_error_message": "provider temporarily unavailable",
                }
            )
        )

        with trap_jobs() as trap:
            self.assertTrue(media.with_user(self.admin).action_retry_download())
            trap.assert_jobs_count(1)
            trap.assert_enqueued_job(
                media.sudo().with_company(media.company_id)._job_download,
                properties={
                    "identity_key": "contact_center:media:%s" % media.id,
                    "max_retries": 0,
                    "description": "Contact Center media %s" % media.id,
                },
            )

        media.invalidate_recordset(
            [
                "state",
                "attempts",
                "queue_job_uuid",
                "last_error_class",
                "last_error_message",
            ]
        )
        self.assertEqual(media.state, "pending")
        self.assertEqual(media.attempts, 0)
        self.assertTrue(media.queue_job_uuid)
        self.assertFalse(media.last_error_class)
        self.assertFalse(media.last_error_message)

    def test_inbox_attempt_number_preserves_prior_job_attempts(self):
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "resumed-attempts-%s" % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": {"fixture": True},
                    "attempts": 4,
                }
            )
        )
        inbox._enqueue()
        queue_job = (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", inbox.queue_job_uuid)], limit=1)
        )
        self.assertTrue(queue_job)
        queue_job.retry = 3

        self.assertEqual(inbox._job_attempt_number(queue_job.uuid), 4 + 3 + 1)
        self.assertEqual(inbox._job_attempt_number(str(uuid.uuid4())), 4 + 1)

    def test_inbox_database_contention_does_not_consume_retry_budget(self):
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "db-contention-%s" % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": {"fixture": True},
                }
            )
        )
        inbox._enqueue()
        job = Job.load(self.env, inbox.queue_job_uuid)
        for error_class in (SerializationFailure, DeadlockDetected) * (
            QUEUE_ATTEMPT_CEILING + 1
        ):
            with self.subTest(error_class=error_class.__name__):
                error = error_class("concurrent account update")
                with mock.patch.object(
                    type(self.env["contact.center.application"]),
                    "_lock_inbound_account_scope",
                    side_effect=error,
                ), mock.patch.object(type(inbox), "_finish_failure") as finish:
                    with self.assertRaises(RetryableJobError) as raised:
                        job.perform()
                self.assertIs(raised.exception.__cause__, error)
                self.assertTrue(raised.exception.ignore_retry)
                self.assertEqual(raised.exception.seconds, 5)
                self.assertEqual(job.retry, 0)
                finish.assert_not_called()
                self.assertEqual(inbox.state, "pending")
                self.assertEqual(inbox.attempts, 0)

        # A real provider failure still gets its first retry after many database
        # conflicts. Job.perform must not mistake those conflicts for attempts.
        with mock.patch.object(
            type(inbox), "_normalize_one", side_effect=TransientAdapterError("offline")
        ), self.assertRaises(RetryableJobError) as raised, self.env.cr.savepoint():
            job.perform()
        self.assertFalse(raised.exception.ignore_retry)
        self.assertEqual(job.retry, 1)
        inbox.invalidate_recordset()
        self.assertEqual(inbox.state, "pending")
        self.assertEqual(inbox.attempts, 0)

    def test_outbox_worker_dispatches_persisted_command(self):
        channel, _binding, _identity = self._channel_binding()
        result = self._send_message_without_enqueue(channel, "Dispatch me")
        outbox = self.env["contact.center.outbox.command"].browse(
            result["outbox_command_id"]
        )
        self._process_outbox(outbox)
        self.assertEqual(outbox.state, "done")
        self.assertEqual(outbox.message_binding_id.delivery_state, "sent")
        self.assertEqual(
            outbox.provider_request_json,
            {
                "provider": "test.fake",
                "method": "POST",
                "endpoint": "/messages",
                "payload": {
                    "command_id": outbox.outbox_idempotency_key,
                    "command_type": "send_message",
                },
            },
        )
        self.assertTrue(
            outbox.message_binding_id.external_message_id.startswith("external-")
        )

    def test_outbox_pacing_is_shared_between_sibling_commands(self):
        channel, _binding, _identity = self._channel_binding()
        database_now = datetime.datetime(2026, 8, 28, 12, 0, 0, 500000)
        first_result = self._send_message_without_enqueue(channel, "Dispatch first")
        second_result = self._send_message_without_enqueue(channel, "Dispatch second")
        first_outbox = self.env["contact.center.outbox.command"].browse(
            first_result["outbox_command_id"]
        )
        second_outbox = self.env["contact.center.outbox.command"].browse(
            second_result["outbox_command_id"]
        )

        with mock.patch.object(
            type(first_outbox),
            "_database_clock",
            autospec=True,
            return_value=database_now,
        ), mock.patch.object(
            FakeAdapter,
            "outbound_min_interval_seconds",
            autospec=True,
            return_value=10,
        ), mock.patch.object(
            FakeAdapter,
            "execute_command",
            autospec=True,
            return_value=AdapterResult.success(external_message_id="first-external"),
        ) as execute_command:
            self.assertTrue(self._process_outbox(first_outbox))
            raised = None
            try:
                self._process_outbox(second_outbox)
            except RetryableJobError as error:
                raised = error
            else:
                self.fail("shared pacing must postpone the sibling queue job")

        self.assertIsNotNone(raised)
        self.assertTrue(raised.ignore_retry)
        self.assertEqual(raised.seconds, 11)
        self.assertEqual(execute_command.call_count, 1)
        self.connection.invalidate_recordset(
            [
                "outbound_dispatch_not_before",
                "outbound_dispatch_not_before_reason",
            ]
        )
        self.assertTrue(self.connection.outbound_dispatch_not_before)
        self.assertEqual(
            self.connection.outbound_dispatch_not_before,
            datetime.datetime(2026, 8, 28, 12, 0, 11),
        )
        self.assertEqual(
            self.connection.outbound_dispatch_not_before_reason,
            "pacing",
        )
        second_outbox.invalidate_recordset(
            [
                "state",
                "attempts",
                "dispatch_job_uuid",
                "dispatch_started_at",
                "provider_request_json",
                "last_error_class",
            ]
        )
        self.assertEqual(second_outbox.state, "pending")
        self.assertEqual(second_outbox.attempts, 0)
        self.assertFalse(second_outbox.dispatch_job_uuid)
        self.assertFalse(second_outbox.dispatch_started_at)
        self.assertFalse(second_outbox.provider_request_json)
        self.assertEqual(second_outbox.last_error_class, "OutboundThrottleError")

    def test_outbox_retry_after_is_shared_between_sibling_commands(self):
        channel, _binding, _identity = self._channel_binding()
        database_now = datetime.datetime(2026, 8, 28, 12, 0, 0)
        first_result = self._send_message_without_enqueue(channel, "Provider busy")
        second_result = self._send_message_without_enqueue(channel, "Wait globally")
        first_outbox = self.env["contact.center.outbox.command"].browse(
            first_result["outbox_command_id"]
        )
        second_outbox = self.env["contact.center.outbox.command"].browse(
            second_result["outbox_command_id"]
        )

        with mock.patch.object(
            type(first_outbox),
            "_database_clock",
            autospec=True,
            return_value=database_now,
        ), mock.patch.object(
            FakeAdapter,
            "outbound_min_interval_seconds",
            autospec=True,
            return_value=0,
        ), mock.patch.object(
            FakeAdapter,
            "execute_command",
            autospec=True,
            return_value=AdapterResult(
                status="transient",
                error_code="rate_limited",
                error_message="retry later",
                retry_after_seconds=45,
            ),
        ) as execute_command:
            provider_retry = None
            try:
                self._process_outbox(first_outbox)
            except RetryableJobError as error:
                provider_retry = error
            else:
                self.fail("the provider Retry-After must postpone its queue job")
            shared_retry = None
            try:
                self._process_outbox(second_outbox)
            except RetryableJobError as error:
                shared_retry = error
            else:
                self.fail("the shared Retry-After must postpone the sibling job")

        self.assertIsNotNone(provider_retry)
        self.assertTrue(provider_retry.ignore_retry)
        expected_rate_limit = ProviderRateLimitError("retry later")
        expected_rate_limit.retry_after_seconds = 45
        self.assertEqual(
            provider_retry.seconds,
            provider_paused_retry_seconds(
                expected_rate_limit,
                ("outbox", first_outbox.id),
            ),
        )
        self.assertIsNotNone(shared_retry)
        self.assertTrue(shared_retry.ignore_retry)
        self.assertEqual(shared_retry.seconds, 45)
        self.assertEqual(execute_command.call_count, 1)
        self.connection.invalidate_recordset(
            [
                "outbound_dispatch_not_before",
                "outbound_dispatch_not_before_reason",
            ]
        )
        self.assertTrue(self.connection.outbound_dispatch_not_before)
        self.assertEqual(
            self.connection.outbound_dispatch_not_before_reason,
            "provider_retry_after",
        )
        first_outbox.invalidate_recordset(["state", "attempts", "last_error_class"])
        self.assertEqual(first_outbox.state, "pending")
        self.assertEqual(first_outbox.attempts, 0)
        self.assertEqual(first_outbox.last_error_class, "ProviderRateLimitError")
        second_outbox.invalidate_recordset(
            ["state", "attempts", "provider_request_json", "last_error_class"]
        )
        self.assertEqual(second_outbox.state, "pending")
        self.assertEqual(second_outbox.attempts, 0)
        self.assertFalse(second_outbox.provider_request_json)
        self.assertEqual(second_outbox.last_error_class, "OutboundThrottleError")

    def test_outbound_dispatch_deadline_is_orm_protected(self):
        deadline = fields.Datetime.now() + datetime.timedelta(seconds=30)
        protected_values = (
            {"outbound_dispatch_not_before": deadline},
            {"outbound_dispatch_not_before_reason": "pacing"},
            {
                "outbound_dispatch_not_before": deadline,
                "outbound_dispatch_not_before_reason": "pacing",
            },
        )
        for values in protected_values:
            with self.subTest(values=values), self.assertRaises(AccessError):
                self.connection.with_user(self.admin).write(values)
        with self.assertRaises(AccessError):
            self.connection.with_user(self.admin).with_context(
                contact_center_outbound_dispatch_token=object()
            ).write(
                {
                    "outbound_dispatch_not_before": deadline,
                    "outbound_dispatch_not_before_reason": "pacing",
                }
            )
        with self.assertRaises(AccessError):
            self.env["contact.center.provider.connection"].with_user(self.admin).create(
                {
                    "name": "Forged Dispatch Deadline",
                    "account_id": self.account.id,
                    "adapter_key": "test.fake",
                    "external_ref": "forged-deadline-%s" % uuid.uuid4(),
                    "active": True,
                    "role": "standby",
                    "inbound_active": False,
                    "outbound_active": False,
                    "outbound_dispatch_not_before": deadline,
                    "outbound_dispatch_not_before_reason": "pacing",
                }
            )

        self.connection._contact_center_set_outbound_dispatch_deadline(
            deadline,
            "pacing",
        )
        self.connection.invalidate_recordset(
            [
                "outbound_dispatch_not_before",
                "outbound_dispatch_not_before_reason",
            ]
        )
        self.assertEqual(self.connection.outbound_dispatch_not_before, deadline)
        self.assertEqual(
            self.connection.outbound_dispatch_not_before_reason,
            "pacing",
        )
        self.connection._contact_center_set_outbound_dispatch_deadline(False)
        self.connection.invalidate_recordset(
            [
                "outbound_dispatch_not_before",
                "outbound_dispatch_not_before_reason",
            ]
        )
        self.assertFalse(self.connection.outbound_dispatch_not_before)
        self.assertFalse(self.connection.outbound_dispatch_not_before_reason)

    def test_outbox_request_snapshot_is_persisted_before_io_and_survives_uncertain(
        self,
    ):
        channel, _binding, _identity = self._channel_binding()
        result = self._send_message_without_enqueue(channel, "Audit before dispatch")
        outbox = self.env["contact.center.outbox.command"].browse(
            result["outbox_command_id"]
        )
        observed = {}

        def fail_after_boundary(_adapter, _connection, _command):
            outbox.invalidate_recordset(["provider_request_json", "state"])
            observed["state"] = outbox.state
            observed["request"] = outbox.provider_request_json
            raise RuntimeError("transport failed after provider I/O began")

        with mock.patch.object(
            FakeAdapter,
            "execute_command",
            autospec=True,
            side_effect=fail_after_boundary,
        ):
            self.assertTrue(self._process_outbox(outbox))

        outbox.invalidate_recordset(["provider_request_json", "state"])
        self.assertEqual(observed["state"], "processing")
        self.assertEqual(observed["request"], outbox.provider_request_json)
        self.assertEqual(outbox.state, "uncertain")
        self.assertEqual(
            outbox.provider_request_json["payload"]["command_id"],
            outbox.outbox_idempotency_key,
        )

    def test_uncertain_adapter_result_preserves_provider_evidence_without_redispatch(
        self,
    ):
        channel, _binding, _identity = self._channel_binding()
        result = self._send_message_without_enqueue(channel, "Correlate once")
        outbox = self.env["contact.center.outbox.command"].browse(
            result["outbox_command_id"]
        )
        calls = []

        def uncertain_result(_adapter, _connection, _command):
            calls.append(True)
            return AdapterResult(
                status="uncertain",
                error_code="correlation_mismatch",
                error_message="provider returned another message ID",
                provider_response={"data": {"id": "ANOTHER-ID"}},
            )

        with mock.patch.object(
            FakeAdapter,
            "execute_command",
            autospec=True,
            side_effect=uncertain_result,
        ):
            self.assertTrue(self._process_outbox(outbox))
            self.assertFalse(self._process_outbox(outbox))

        outbox.invalidate_recordset(["state", "provider_response_json"])
        self.assertEqual(outbox.state, "uncertain")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            outbox.provider_response_json,
            {
                "dispatch_outcome": "provider_returned_uncertain",
                "error_code": "correlation_mismatch",
                "provider_response": {"data": {"id": "ANOTHER-ID"}},
            },
        )

    def test_serialization_after_provider_success_reconciles_without_redispatch(self):
        channel, _binding, _identity = self._channel_binding()
        result = self._send_message_without_enqueue(
            channel, "Provider accepted before local serialization"
        )
        outbox = self.env["contact.center.outbox.command"].browse(
            result["outbox_command_id"]
        )
        message_binding = outbox.message_binding_id
        binding_model_class = type(message_binding)
        outbox_model_class = type(outbox)
        original_apply_delivery = binding_model_class._contact_center_apply_delivery
        original_reconcile = (
            outbox_model_class._contact_center_reconcile_provider_success
        )
        adapter_calls = []
        projection_calls = []
        reconciliation_calls = []
        transaction_restarts = []

        def provider_success(_adapter, _connection, command):
            adapter_calls.append(command.command_id)
            return AdapterResult.success(
                external_message_id="provider-%s" % command.command_id,
                provider_response={"accepted": True},
            )

        def serialize_once(records, state, **kwargs):
            if records.ids == message_binding.ids and state == "sent":
                projection_calls.append(state)
                if len(projection_calls) == 1:
                    raise SerializationFailure(
                        "concurrent account tuple changed after provider success"
                    )
            return original_apply_delivery(records, state, **kwargs)

        def deadlock_first_reconciliation(records):
            reconciliation_calls.append(records.id)
            if len(reconciliation_calls) == 1:
                raise DeadlockDetected("concurrent topology writer won")
            return original_reconcile(records)

        def preserve_test_transaction(records):
            # Production rolls back the failed transaction here. SavepointCase
            # must retain its outer fixture, so prove the restart boundary via a
            # test double while the inner savepoint performs the actual rollback.
            transaction_restarts.append(records.id)
            records.env.invalidate_all(flush=False)
            return True

        with mock.patch.object(
            FakeAdapter,
            "execute_command",
            autospec=True,
            side_effect=provider_success,
        ), mock.patch.object(
            binding_model_class,
            "_contact_center_apply_delivery",
            new=serialize_once,
        ), mock.patch.object(
            outbox_model_class,
            "_contact_center_reconcile_provider_success",
            new=deadlock_first_reconciliation,
        ), mock.patch.object(
            outbox_model_class,
            "_restart_provider_success_reconciliation_transaction",
            new=preserve_test_transaction,
        ):
            self.assertTrue(self._process_outbox(outbox))
            self.assertFalse(self._process_outbox(outbox))

        outbox.invalidate_recordset(
            [
                "state",
                "provider_response_json",
                "last_error_class",
                "last_error_message",
            ]
        )
        message_binding.invalidate_recordset(["delivery_state", "external_message_id"])
        self.assertEqual(len(adapter_calls), 1)
        self.assertEqual(len(projection_calls), 2)
        self.assertEqual(reconciliation_calls, [outbox.id, outbox.id])
        self.assertEqual(transaction_restarts, [outbox.id])
        self.assertEqual(outbox.state, "done")
        self.assertEqual(message_binding.delivery_state, "sent")
        self.assertEqual(
            message_binding.external_message_id,
            "provider-%s" % adapter_calls[0],
        )
        self.assertFalse(outbox.last_error_class)
        self.assertFalse(outbox.last_error_message)
        self.assertEqual(
            outbox.provider_response_json["dispatch_outcome"],
            "provider_returned_success",
        )
        self.assertEqual(
            outbox.provider_response_json["reconciled_by"],
            "positive_provider_response",
        )
        self.assertEqual(
            self.env["contact.center.delivery.event"].search_count(
                [
                    ("message_binding_id", "=", message_binding.id),
                    (
                        "external_event_id",
                        "=",
                        "provider-success-outbox:%s" % outbox.id,
                    ),
                ]
            ),
            1,
        )

    def test_admin_reconciles_old_strict_provider_success_idempotently(self):
        channel, _binding, _identity = self._channel_binding()
        result = self._send_message_without_enqueue(
            channel, "Historical accepted provider send"
        )
        outbox = self.env["contact.center.outbox.command"].browse(
            result["outbox_command_id"]
        )
        external_message_id = "historical-%s" % uuid.uuid4()
        job_uuid = str(uuid.uuid4())
        outbox.write(
            {
                "state": "uncertain",
                "attempts": 1,
                "queue_job_uuid": job_uuid,
                "dispatch_job_uuid": job_uuid,
                "dispatch_started_at": fields.Datetime.now(),
                "processed_at": fields.Datetime.now(),
                "provider_request_json": {"provider": "test.fake"},
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

        with mock.patch.object(
            FakeAdapter, "execute_command", autospec=True
        ) as execute_command:
            outbox.sudo().write({"queue_job_uuid": str(uuid.uuid4())})
            self.assertFalse(
                outbox.with_user(self.admin).action_reconcile_provider_success()
            )
            outbox.sudo().write({"queue_job_uuid": job_uuid})
            self.assertTrue(
                outbox.with_user(self.admin).action_reconcile_provider_success()
            )
            self.assertFalse(
                outbox.with_user(self.admin).action_reconcile_provider_success()
            )

        execute_command.assert_not_called()
        outbox.invalidate_recordset(["state", "provider_response_json"])
        outbox.message_binding_id.invalidate_recordset(
            ["delivery_state", "external_message_id"]
        )
        self.assertEqual(outbox.state, "done")
        self.assertEqual(outbox.message_binding_id.delivery_state, "sent")
        self.assertEqual(
            outbox.message_binding_id.external_message_id, external_message_id
        )
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

    def test_outbox_rejects_unsafe_provider_snapshot_before_dispatch(self):
        channel, _binding, _identity = self._channel_binding()
        result = self._send_message_without_enqueue(channel, "Reject unsafe audit")
        outbox = self.env["contact.center.outbox.command"].browse(
            result["outbox_command_id"]
        )
        unsafe_snapshot = {
            "provider": "test.fake",
            "method": "POST",
            "endpoint": "/messages",
            "headers": {"Token": "must-not-persist"},
        }

        with mock.patch.object(
            FakeAdapter,
            "prepare_request_snapshot",
            autospec=True,
            return_value=unsafe_snapshot,
        ), mock.patch.object(FakeAdapter, "execute_command") as execute_command:
            self.assertFalse(self._process_outbox(outbox))

        execute_command.assert_not_called()
        outbox.invalidate_recordset(
            ["provider_request_json", "state", "last_error_class"]
        )
        self.assertFalse(outbox.provider_request_json)
        self.assertEqual(outbox.state, "dead")
        self.assertEqual(outbox.last_error_class, "AdapterError")

    def test_outbox_never_redispatches_after_success_and_dead_marks_failed(self):
        channel, binding, _identity = self._channel_binding()
        result = self._send_message_without_enqueue(channel, "Dispatch once")
        outbox = self.env["contact.center.outbox.command"].browse(
            result["outbox_command_id"]
        )
        duplicate_message = channel.with_user(self.agent)._contact_center_post(
            origin="outbound",
            body="Existing provider echo",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        self.env["contact.center.message.binding"].sudo().create(
            {
                "message_id": duplicate_message.id,
                "channel_binding_id": binding.id,
                "provider_connection_id": self.connection.id,
                "direction": "inbound",
                "origin": "provider",
                "content_type": "text",
                "external_message_id": "external-%s"
                % outbox.command_json["command_id"],
                "delivery_state": "delivered",
            }
        )

        with mute_logger("odoo.sql_db"):
            self._process_outbox(outbox)
        self.assertEqual(outbox.state, "uncertain")
        self.assertEqual(
            outbox.provider_response_json["dispatch_outcome"],
            "provider_returned_success",
        )
        self.assertEqual(outbox.message_binding_id.delivery_state, "queued")
        self.assertFalse(self._process_outbox(outbox))

        other_channel, _other_binding, _other_identity = self._channel_binding()
        other_result = self._send_message_without_enqueue(
            other_channel, "Invalid persisted DTO"
        )
        dead_outbox = self.env["contact.center.outbox.command"].browse(
            other_result["outbox_command_id"]
        )
        dead_outbox.command_json = {"invalid": True}
        self._process_outbox(dead_outbox)
        self.assertEqual(dead_outbox.state, "dead")
        self.assertEqual(dead_outbox.message_binding_id.delivery_state, "failed")
        failed_events = self.env["contact.center.delivery.event"].search(
            [
                ("message_binding_id", "=", dead_outbox.message_binding_id.id),
                ("state", "=", "failed"),
            ]
        )
        self.assertEqual(len(failed_events), 1)

        scope_result = self._send_message_without_enqueue(
            other_channel, "Wrong persisted scope"
        )
        scope_outbox = self.env["contact.center.outbox.command"].browse(
            scope_result["outbox_command_id"]
        )
        wrong_scope = dict(scope_outbox.command_json)
        wrong_scope["account_ref"] = "another-account"
        scope_outbox.command_json = wrong_scope
        self._process_outbox(scope_outbox)
        self.assertEqual(scope_outbox.state, "dead")
        self.assertEqual(scope_outbox.last_error_class, "ValidationError")

        outbox.with_user(self.admin)._resolve_uncertain(
            "closed_without_send",
            "Release the provider route for the missing-adapter fixture.",
        )
        self.connection.outbound_active = False
        self.connection.flush_recordset(["outbound_active"])
        unknown_connection = self.env["contact.center.provider.connection"].create(
            {
                "name": "Missing adapter",
                "account_id": self.account.id,
                "adapter_key": "test.missing",
                "external_ref": "missing-adapter-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "standby",
                "inbound_active": False,
                "outbound_active": False,
                "capabilities_json": {"send_message": True},
                "last_state_observed_at": fields.Datetime.now(),
                "last_state_source": "health_job",
                "health_detail": "healthy",
            }
        )
        unknown_connection.action_use_as_primary()
        unknown_connection.outbound_active = True
        # Build one canonical command as if its provider addon were available at
        # admission, then let dispatch observe that the owning addon disappeared.
        # This keeps message/provider affinity valid while exercising the worker's
        # fail-closed missing-adapter path.
        with mock.patch.object(
            type(unknown_connection),
            "get_adapter",
            autospec=True,
            return_value=FakeAdapter(self.env),
        ):
            missing_result = self._send_message_without_enqueue(
                other_channel, "Missing adapter owner"
            )
        missing_adapter_outbox = self.env["contact.center.outbox.command"].browse(
            missing_result["outbox_command_id"]
        )
        self.assertEqual(
            missing_adapter_outbox.provider_connection_id, unknown_connection
        )
        self._process_outbox(missing_adapter_outbox)
        self.assertEqual(missing_adapter_outbox.state, "dead")
        self.assertEqual(missing_adapter_outbox.last_error_class, "ValidationError")

    def test_media_retry_delay_uses_provider_hint_or_queue_job_pattern(self):
        self.assertEqual(PROVIDER_PAUSED_RETRY_SECONDS, 60)
        default_pause = ProviderPausedError("provider paused")
        default_delay = provider_paused_retry_seconds(default_pause, ("media", 123))
        self.assertGreater(default_delay, PROVIDER_PAUSED_RETRY_SECONDS)
        self.assertLessEqual(default_delay, 78)
        self.assertEqual(
            default_delay,
            provider_paused_retry_seconds(default_pause, ("media", 123)),
        )
        hinted_pause = ProviderPausedError("provider rate limited")
        hinted_pause.retry_after_seconds = 3590
        self.assertGreaterEqual(
            provider_paused_retry_seconds(hinted_pause, ("outbox", 321)),
            3590,
        )
        self.assertLessEqual(
            provider_paused_retry_seconds(hinted_pause, ("outbox", 321)),
            3600,
        )
        capped_pause = ProviderPausedError("provider ceiling")
        capped_pause.retry_after_seconds = 3600
        self.assertEqual(
            provider_paused_retry_seconds(capped_pause, ("outbox", 654)),
            3600,
        )
        retry_cases = (
            (TransientAdapterError("provider unavailable"), None),
            (RuntimeError("unexpected transport failure"), None),
        )
        for provider_error, expected_seconds in retry_cases:
            media = self._media_binding()
            with self.subTest(
                error_class=provider_error.__class__.__name__
            ), mock.patch.object(
                FakeAdapter,
                "download_media",
                side_effect=provider_error,
            ), self.assertRaises(
                RetryableJobError
            ) as raised:
                self._process_media(media)
            self.assertFalse(raised.exception.ignore_retry)
            self.assertEqual(raised.exception.seconds, expected_seconds)

        hinted_error = TransientAdapterError("provider requested a delay")
        hinted_error.retry_after_seconds = 37
        hinted_media = self._media_binding()
        with mock.patch.object(
            FakeAdapter,
            "download_media",
            side_effect=hinted_error,
        ), self.assertRaises(RetryableJobError) as raised:
            self._process_media(hinted_media)
        self.assertFalse(raised.exception.ignore_retry)
        self.assertEqual(raised.exception.seconds, 37)

        paused_media = self._media_binding()
        with mock.patch.object(
            FakeAdapter,
            "download_media",
            side_effect=ProviderPausedError("provider paused"),
        ), self.assertRaises(RetryableJobError) as raised:
            self._process_media(paused_media)
        self.assertTrue(raised.exception.ignore_retry)
        self.assertEqual(
            raised.exception.seconds,
            provider_paused_retry_seconds(
                ProviderPausedError("provider paused"),
                ("media", paused_media.id),
            ),
        )

    def test_media_pause_preflight_never_calls_provider(self):
        media = self._media_binding()
        self.connection.write(
            {
                "state": "authentication_required",
                "last_state_observed_at": fields.Datetime.now(),
            }
        )
        with mock.patch.object(FakeAdapter, "download_media") as download_media:
            with self.assertRaises(RetryableJobError) as raised:
                self._process_media(media)
        self.assertTrue(raised.exception.ignore_retry)
        self.assertEqual(media.state, "pending")
        download_media.assert_not_called()

    def test_queue_workers_reject_missing_and_stale_ownership_before_effects(self):
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "ownership-fence-%s" % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": {"fixture": "ownership"},
                }
            )
        )
        channel, _binding, _identity = self._channel_binding()
        outbound = self._send_message_without_enqueue(channel, "Ownership fence")
        outbox = self.env["contact.center.outbox.command"].browse(
            outbound["outbox_command_id"]
        )
        media = self._media_binding()
        stale_uuid = str(uuid.uuid4())
        current_uuid = str(uuid.uuid4())

        with mock.patch.object(
            type(inbox), "_normalize_one", autospec=True
        ) as normalize, mock.patch.object(
            type(outbox), "_prepare_dispatch_for_job", autospec=True
        ) as prepare, mock.patch.object(
            FakeAdapter, "download_media", autospec=True
        ) as download:
            self.assertFalse(inbox.with_context(job_uuid=current_uuid)._job_process())
            self.assertFalse(outbox.with_context(job_uuid=current_uuid)._job_process())
            self.assertFalse(media.with_context(job_uuid=current_uuid)._job_download())

            inbox.sudo().write({"queue_job_uuid": stale_uuid})
            outbox.sudo().write({"queue_job_uuid": stale_uuid})
            media.sudo().write({"queue_job_uuid": stale_uuid})
            self.assertFalse(inbox.with_context(job_uuid=current_uuid)._job_process())
            self.assertFalse(outbox.with_context(job_uuid=current_uuid)._job_process())
            self.assertFalse(media.with_context(job_uuid=current_uuid)._job_download())

        normalize.assert_not_called()
        prepare.assert_not_called()
        download.assert_not_called()
        self.assertEqual(inbox.state, "pending")
        self.assertEqual(outbox.state, "pending")
        self.assertEqual(media.state, "pending")

    def test_media_download_persists_raw_attachment_and_checks_provider_metadata(self):
        content = b"\x89PNG\r\n\x1a\ncontact-center-media"
        result = MediaDownloadResult(
            content=content,
            mime_type="image/png",
            file_name="download.png",
        )
        media = self._media_binding()
        media.write(
            {
                "size_bytes": result.size_bytes,
                "sha256": result.sha256,
            }
        )
        with mock.patch.object(FakeAdapter, "download_media", return_value=result):
            self.assertTrue(self._process_media(media))
        media.invalidate_recordset(
            ["state", "attachment_id", "size_bytes", "sha256", "downloaded_at"]
        )
        self.assertEqual(media.state, "ready")
        self.assertTrue(media.attachment_id)
        self.assertEqual(media.attachment_id.sudo().raw, content)
        self.assertEqual(media.size_bytes, len(content))
        self.assertEqual(media.sha256, hashlib.sha256(content).hexdigest())
        self.assertTrue(media.downloaded_at)

        mismatch_cases = (
            {"size_bytes": len(content) + 1, "sha256": ""},
            {"size_bytes": len(content), "sha256": "0" * 64},
        )
        for expected in mismatch_cases:
            mismatched_media = self._media_binding()
            mismatched_media.write(expected)
            with self.subTest(expected=expected), mock.patch.object(
                FakeAdapter, "download_media", return_value=result
            ):
                self.assertFalse(self._process_media(mismatched_media))
            self.assertEqual(mismatched_media.state, "failed")
            self.assertFalse(mismatched_media.attachment_id)
            self.assertEqual(mismatched_media.last_error_class, "AdapterError")

    def test_media_download_discards_bytes_when_message_is_deleted_during_io(self):
        content = b"\x89PNG\r\n\x1a\nlate-deleted-media"
        result = MediaDownloadResult(
            content=content,
            mime_type="image/png",
            file_name="late-delete.png",
        )
        media = self._media_binding()
        media.write(
            {
                "size_bytes": result.size_bytes,
                "sha256": result.sha256,
            }
        )

        def delete_while_downloading(_adapter, _connection, _media_dto):
            media.message_binding_id.write(
                {
                    "message_state": "deleted",
                    "deleted_display_mode": "redact",
                    "deleted_at": fields.Datetime.now(),
                }
            )
            return result

        with mock.patch.object(
            FakeAdapter,
            "download_media",
            autospec=True,
            side_effect=delete_while_downloading,
        ):
            self.assertTrue(self._process_media(media))

        media.invalidate_recordset(
            ["state", "attachment_id", "last_error_class", "last_error_message"]
        )
        self.assertEqual(media.state, "discarded")
        self.assertFalse(media.attachment_id)
        self.assertEqual(
            media.last_error_class,
            "DeletedMessageContentHidden",
        )
        self.assertIn("deletion policy", media.last_error_message)

    def test_media_retry_pattern_and_twelve_attempt_ceiling(self):
        self.assertEqual(QUEUE_ATTEMPT_CEILING, 12)
        media_job_function = self.env.ref(
            "contact_center_base.queue_job_function_contact_center_media_download"
        )
        self.assertEqual(
            media_job_function.retry_pattern,
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
        media = self._media_binding()
        media.write({"attempts": QUEUE_ATTEMPT_CEILING - 1})
        with mock.patch.object(
            FakeAdapter,
            "download_media",
            side_effect=TransientAdapterError("provider still unavailable"),
        ):
            self.assertFalse(self._process_media(media))
        self.assertEqual(media.state, "failed")
        self.assertEqual(media.attempts, QUEUE_ATTEMPT_CEILING)
        self.assertEqual(media.last_error_class, "TransientAdapterError")

    def test_outbox_adapter_retry_delays_are_delegated_to_queue_job(self):
        transient_channel, _binding, _identity = self._channel_binding()
        transient_result = self._send_message_without_enqueue(
            transient_channel, "Transient provider failure"
        )
        transient_outbox = self.env["contact.center.outbox.command"].browse(
            transient_result["outbox_command_id"]
        )
        with mock.patch.object(
            FakeAdapter,
            "execute_command",
            return_value=AdapterResult(
                status="transient",
                error_code="provider_busy",
                error_message="try again",
                retry_after_seconds=37,
            ),
        ):
            transient_error = None
            try:
                self._process_outbox(transient_outbox)
            except RetryableJobError as error:
                transient_error = error
            else:
                self.fail("transient provider result must postpone the queue job")
        self.assertIsNotNone(transient_error)
        self.assertFalse(transient_error.ignore_retry)
        self.assertEqual(transient_error.seconds, 37)
        self.env.cr.execute(
            """
            SELECT state, attempts, dispatch_job_uuid, last_error_class
              FROM contact_center_outbox_command
             WHERE id = %s
            """,
            [transient_outbox.id],
        )
        transient_values = self.env.cr.fetchone()
        self.assertEqual(
            transient_values,
            ("retry", 1, None, "TransientAdapterError"),
        )
        self.connection._contact_center_set_outbound_dispatch_deadline(False)

        paused_channel, _binding, _identity = self._channel_binding()
        paused_result = self._send_message_without_enqueue(
            paused_channel, "Paused provider"
        )
        paused_outbox = self.env["contact.center.outbox.command"].browse(
            paused_result["outbox_command_id"]
        )
        with mock.patch.object(
            FakeAdapter,
            "execute_command",
            return_value=AdapterResult(
                status="paused",
                error_code="provider_paused",
                error_message="wait for reconnect",
                retry_after_seconds=91,
            ),
        ):
            paused_error = None
            try:
                self._process_outbox(paused_outbox)
            except RetryableJobError as error:
                paused_error = error
            else:
                self.fail("paused provider result must postpone the queue job")
        self.assertIsNotNone(paused_error)
        self.assertTrue(paused_error.ignore_retry)
        hinted_pause = ProviderPausedError("wait for reconnect")
        hinted_pause.retry_after_seconds = 91
        self.assertEqual(
            paused_error.seconds,
            provider_paused_retry_seconds(
                hinted_pause,
                ("outbox", paused_outbox.id),
            ),
        )
        self.env.cr.execute(
            """
            SELECT state, attempts, dispatch_job_uuid, last_error_class
              FROM contact_center_outbox_command
             WHERE id = %s
            """,
            [paused_outbox.id],
        )
        paused_values = self.env.cr.fetchone()
        self.assertEqual(
            paused_values,
            ("pending", 0, None, "ProviderPausedError"),
        )

    def test_outbox_stops_at_the_shared_retry_ceiling(self):
        channel, _binding, _identity = self._channel_binding()
        result = self._send_message_without_enqueue(channel, "Retry ceiling")
        outbox = self.env["contact.center.outbox.command"].browse(
            result["outbox_command_id"]
        )
        outbox.write(
            {
                "state": "retry",
                "attempts": QUEUE_ATTEMPT_CEILING - 1,
            }
        )

        with mock.patch.object(
            FakeAdapter,
            "execute_command",
            return_value=AdapterResult(
                status="transient",
                error_code="provider_still_unavailable",
            ),
        ):
            self.assertFalse(self._process_outbox(outbox))
        outbox.invalidate_recordset(["state", "attempts", "last_error_class"])
        self.assertEqual(outbox.state, "dead")
        self.assertEqual(outbox.attempts, QUEUE_ATTEMPT_CEILING)
        self.assertEqual(outbox.last_error_class, "TransientAdapterError")

    def test_outbox_processing_reentry_is_never_redispatched(self):
        same_channel, _binding, _identity = self._channel_binding()
        same_result = self._send_message_without_enqueue(
            same_channel, "Same job resumed"
        )
        same_outbox = self.env["contact.center.outbox.command"].browse(
            same_result["outbox_command_id"]
        )
        same_uuid = str(uuid.uuid4())
        same_outbox.write(
            {
                "state": "processing",
                "attempts": 1,
                "queue_job_uuid": same_uuid,
                "dispatch_job_uuid": same_uuid,
            }
        )
        with mock.patch.object(FakeAdapter, "execute_command") as execute_command:
            self.assertFalse(self._process_outbox(same_outbox, same_uuid))
        execute_command.assert_not_called()
        self.assertEqual(same_outbox.state, "uncertain")
        self.assertEqual(same_outbox.last_error_class, "RuntimeError")

        other_channel, _binding, _identity = self._channel_binding()
        other_result = self._send_message_without_enqueue(
            other_channel, "Different job observed in-flight dispatch"
        )
        other_outbox = self.env["contact.center.outbox.command"].browse(
            other_result["outbox_command_id"]
        )
        dispatch_uuid = str(uuid.uuid4())
        current_job_uuid = str(uuid.uuid4())
        other_outbox.write(
            {
                "state": "processing",
                "attempts": 1,
                "queue_job_uuid": current_job_uuid,
                "dispatch_job_uuid": dispatch_uuid,
            }
        )
        with mock.patch.object(FakeAdapter, "execute_command") as execute_command:
            self.assertFalse(self._process_outbox(other_outbox, current_job_uuid))
        execute_command.assert_not_called()
        self.assertEqual(other_outbox.state, "uncertain")
        self.assertEqual(other_outbox.dispatch_job_uuid, dispatch_uuid)

    def test_action_requeue_skips_active_job_and_replaces_stale_references(self):
        queue_job_model = self.env["queue.job"].sudo()

        active_channel, _binding, _identity = self._channel_binding()
        active_result = self._send_message_without_enqueue(
            active_channel, "Keep active queue job"
        )
        active_outbox = self.env["contact.center.outbox.command"].browse(
            active_result["outbox_command_id"]
        )
        active_outbox._enqueue()
        active_uuid = active_outbox.queue_job_uuid
        active_identity = "contact_center:outbox:%s" % active_outbox.id
        active_jobs = queue_job_model.search([("identity_key", "=", active_identity)])
        self.assertEqual(len(active_jobs), 1)

        active_outbox.with_user(self.admin).action_requeue()
        active_outbox.invalidate_recordset(["queue_job_uuid"])
        self.assertEqual(active_outbox.queue_job_uuid, active_uuid)
        self.assertEqual(
            queue_job_model.search_count([("identity_key", "=", active_identity)]),
            1,
        )

        terminal_channel, _binding, _identity = self._channel_binding()
        terminal_result = self._send_message_without_enqueue(
            terminal_channel, "Replace terminal queue job"
        )
        terminal_outbox = self.env["contact.center.outbox.command"].browse(
            terminal_result["outbox_command_id"]
        )
        terminal_outbox._enqueue()
        terminal_uuid = terminal_outbox.queue_job_uuid
        terminal_job = queue_job_model.search([("uuid", "=", terminal_uuid)], limit=1)
        terminal_job.state = "done"

        terminal_outbox.with_user(self.admin).action_requeue()
        terminal_outbox.invalidate_recordset(["queue_job_uuid"])
        self.assertNotEqual(terminal_outbox.queue_job_uuid, terminal_uuid)
        replacement_job = queue_job_model.search(
            [("uuid", "=", terminal_outbox.queue_job_uuid)], limit=1
        )
        self.assertEqual(replacement_job.state, "pending")
        self.assertEqual(
            queue_job_model.search_count(
                [("identity_key", "=", "contact_center:outbox:%s" % terminal_outbox.id)]
            ),
            2,
        )

        missing_channel, _binding, _identity = self._channel_binding()
        missing_result = self._send_message_without_enqueue(
            missing_channel, "Replace missing queue job"
        )
        missing_outbox = self.env["contact.center.outbox.command"].browse(
            missing_result["outbox_command_id"]
        )
        nonexistent_uuid = str(uuid.uuid4())
        missing_outbox.queue_job_uuid = nonexistent_uuid

        missing_outbox.with_user(self.admin).action_requeue()
        missing_outbox.invalidate_recordset(["queue_job_uuid"])
        self.assertNotEqual(missing_outbox.queue_job_uuid, nonexistent_uuid)
        missing_replacement = queue_job_model.search(
            [("uuid", "=", missing_outbox.queue_job_uuid)], limit=1
        )
        self.assertEqual(missing_replacement.state, "pending")
        self.assertEqual(
            queue_job_model.search_count(
                [("identity_key", "=", "contact_center:outbox:%s" % missing_outbox.id)]
            ),
            1,
        )

    def test_inbox_terminal_event_can_be_replayed_after_a_code_fix(self):
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "unsupported-%s" % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": {"fixture": True},
                    "normalized_dto_json": {"stale": True},
                    "metadata_json": {"source": "test"},
                    "state": "unsupported",
                    "attempts": 4,
                    "processed_at": fields.Datetime.now(),
                    "last_error_class": "UnsupportedEventError",
                    "last_error_message": "not implemented yet",
                }
            )
        )

        inbox.with_user(self.admin).action_requeue()

        inbox.invalidate_recordset()
        self.assertEqual(inbox.state, "pending")
        self.assertEqual(inbox.attempts, 0)
        self.assertFalse(inbox.processed_at)
        self.assertFalse(inbox.last_error_class)
        self.assertFalse(inbox.normalized_dto_json)
        self.assertTrue(inbox.queue_job_uuid)
        self.assertEqual(inbox.metadata_json["manual_requeue_count"], 1)
        self.assertEqual(inbox.metadata_json["manual_requeue_user_id"], self.admin.id)
        first_job_uuid = inbox.queue_job_uuid
        self.assertTrue(
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", first_job_uuid)], limit=1)
        )

        inbox.with_user(self.admin).action_requeue()
        inbox.invalidate_recordset(["metadata_json", "queue_job_uuid"])
        self.assertEqual(inbox.metadata_json["manual_requeue_count"], 1)
        self.assertEqual(inbox.queue_job_uuid, first_job_uuid)

    def test_blocked_inbox_requires_conflict_resolution_before_replay(self):
        identity_a = self._identity("Conflict A")
        identity_b = self._identity("Conflict B")
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "blocked-%s" % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": {"fixture": True},
                    "state": "blocked",
                }
            )
        )
        conflict = (
            self.env["contact.center.identity.conflict"]
            .sudo()
            .create(
                {
                    "account_id": self.account.id,
                    "identity_ids": [(6, 0, (identity_a | identity_b).ids)],
                    "address_evidence_json": [{"fixture": True}],
                    "inbox_event_id": inbox.id,
                }
            )
        )

        with self.assertRaisesRegex(ValidationError, "Resolve the identity conflict"):
            inbox.with_user(self.admin).action_requeue()

        conflict.action_resolve()
        with trap_jobs() as trap:
            inbox.with_user(self.admin).action_requeue()
            trap.assert_jobs_count(1)
        self.assertEqual(inbox.state, "pending")

    def test_mark_seen_validates_channel_and_never_regresses(self):
        channel, _binding, _identity = self._channel_binding()
        channel_as_agent = channel.with_user(self.agent)
        first = channel_as_agent._contact_center_post(
            origin="outbound",
            body="First",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        second = channel_as_agent._contact_center_post(
            origin="outbound",
            body="Second",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        api.mark_seen(channel.id, second.id)
        api.mark_seen(channel.id, first.id)
        member = channel.with_user(self.agent)._contact_center_member_for_current_user()
        member.invalidate_recordset(["seen_message_id", "fetched_message_id"])
        self.assertEqual(member.seen_message_id, second)
        self.assertEqual(member.fetched_message_id, second)

        with self.assertRaises(AccessError):
            member.with_user(self.agent).write({"seen_message_id": first.id})
        with self.assertRaises(AccessError):
            member.with_user(self.agent).write(
                {
                    "fetched_message_id": first.id,
                    "seen_message_id": first.id,
                    "last_seen_dt": False,
                }
            )

        other_channel, _other_binding, _other_identity = self._channel_binding()
        other_message = other_channel.with_user(self.agent)._contact_center_post(
            origin="outbound",
            body="Other",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        with self.assertRaises(ValidationError):
            api.mark_seen(channel.id, other_message.id)
        with self.assertRaises(AccessError):
            member.with_user(self.agent).write({"seen_message_id": other_message.id})

    def test_ui_conversation_list_orders_and_paginates_by_last_message(self):
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        application = self.env["contact.center.application"]
        dated_channels = []
        with mock.patch.object(
            type(application), "_notify_ui", autospec=True
        ) as notify_ui:
            for label, message_date in (
                ("oldest", "2026-08-21 10:00:00"),
                ("middle", "2026-08-21 11:00:00"),
                ("newest", "2026-08-21 12:00:00"),
            ):
                channel, _binding, _identity = self._channel_binding(
                    suffix="ui-search-%s" % label
                )
                message = channel.with_user(self.agent)._contact_center_post(
                    origin="outbound",
                    body=label,
                    message_type="comment",
                    subtype_xmlid="mail.mt_comment",
                    partner_ids=[],
                    date=fields.Datetime.to_datetime(message_date),
                )
                application._publish_message_created(channel, message)
                dated_channels.append((channel, message, label))
            application._publish_message_created(
                dated_channels[-1][0],
                dated_channels[-1][1],
                direction="unsupported",
            )

        self.assertEqual(
            [
                call.args[3]
                for call in notify_ui.call_args_list
                if call.args[2] == "message_created"
            ],
            [{"message_id": item[1].id} for item in dated_channels]
            + [{"message_id": dated_channels[-1][1].id}],
        )

        first_page = api.list_conversations(limit=2)
        self.assertEqual(
            [item["channel_id"] for item in first_page["items"]],
            [dated_channels[2][0].id, dated_channels[1][0].id],
        )
        self.assertEqual(first_page["total"], 3)
        self.assertTrue(first_page["has_more"])
        self.assertEqual(
            first_page["next_cursor"],
            {
                "segment": "activity",
                "last_activity_at": fields.Datetime.to_string(
                    dated_channels[1][1].date
                ),
                "channel_id": dated_channels[1][0].id,
            },
        )
        self.assertEqual(first_page["items"][0]["last_message"]["body_text"], "newest")

        second_page = api.list_conversations(limit=2, cursor=first_page["next_cursor"])
        self.assertEqual(
            [item["channel_id"] for item in second_page["items"]],
            [dated_channels[0][0].id],
        )
        self.assertFalse(second_page["has_more"])
        self.assertFalse(second_page["next_cursor"])
        search_result = api.list_conversations(
            limit=10, filters={"query": "ui-search-newest"}
        )
        self.assertEqual(
            [item["channel_id"] for item in search_result["items"]],
            [dated_channels[2][0].id],
        )
        dated_channels[2][0].sudo().with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write({"contact_center_responsible_id": self.agent.id})
        mine_result = api.list_conversations(
            limit=1, filters={"responsibility": "mine"}
        )
        self.assertEqual(
            [item["channel_id"] for item in mine_result["items"]],
            [dated_channels[2][0].id],
        )
        self.assertEqual(mine_result["total"], 1)
        self.assertFalse(mine_result["has_more"])

        unassigned_first = api.list_conversations(
            limit=1, filters={"responsibility": "unassigned"}
        )
        self.assertEqual(
            [item["channel_id"] for item in unassigned_first["items"]],
            [dated_channels[1][0].id],
        )
        self.assertEqual(unassigned_first["total"], 2)
        self.assertTrue(unassigned_first["has_more"])
        unassigned_second = api.list_conversations(
            limit=1,
            cursor=unassigned_first["next_cursor"],
            filters={"responsibility": "unassigned"},
        )
        self.assertEqual(
            [item["channel_id"] for item in unassigned_second["items"]],
            [dated_channels[0][0].id],
        )
        self.assertFalse(unassigned_second["has_more"])
        for invalid_scope in (False, 7, "assigned"):
            with self.assertRaises(ValidationError):
                api.list_conversations(
                    limit=1, filters={"responsibility": invalid_scope}
                )
        dated_channels[2][0].invalidate_recordset(
            ["contact_center_last_message_id", "contact_center_last_message_at"]
        )
        self.assertEqual(
            dated_channels[2][0].contact_center_last_message_id,
            dated_channels[2][1],
        )
        self.assertEqual(
            dated_channels[2][0].contact_center_last_message_at,
            dated_channels[2][1].date,
        )
        delayed = (
            dated_channels[2][0]
            .with_user(self.agent)
            ._contact_center_post(
                origin="outbound",
                body="delayed older event",
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
                partner_ids=[],
                date=fields.Datetime.to_datetime("2026-08-21 09:00:00"),
            )
        )
        application._publish_message_created(dated_channels[2][0], delayed)
        dated_channels[2][0].invalidate_recordset(
            ["contact_center_last_message_id", "contact_center_last_message_at"]
        )
        self.assertEqual(
            dated_channels[2][0].contact_center_last_message_id,
            dated_channels[2][1],
        )
        self.assertEqual(
            dated_channels[2][0].contact_center_last_message_at,
            dated_channels[2][1].date,
        )

    def test_ui_timeline_returns_plain_text_and_stable_cursor_pages(self):
        channel, _binding, _identity = self._channel_binding()
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        bodies = [
            "<img src=x onerror=alert(1)> Olá & emoji 😀\nlinha",
            "second",
            "third",
            "fourth",
            "fifth",
        ]
        results = [
            api.send_message(
                channel.id,
                body,
                client_request_id=str(uuid.uuid4()),
            )
            for body in bodies
        ]
        message_ids = [result["message_id"] for result in results]

        newest_page = api.get_timeline(channel.id, limit=2)
        self.assertEqual(
            [item["message_id"] for item in newest_page["items"]], message_ids[-2:]
        )
        self.assertTrue(newest_page["has_more"])
        self.assertEqual(newest_page["next_before_message_id"], message_ids[-2])
        self.assertFalse(newest_page["has_more_forward"])
        self.assertFalse(newest_page["next_after_message_id"])
        positional_page = api.get_timeline(channel.id, None, 2)
        self.assertEqual(positional_page["items"], newest_page["items"])

        middle_page = api.get_timeline(
            channel.id,
            before_message_id=newest_page["next_before_message_id"],
            limit=2,
        )
        self.assertEqual(
            [item["message_id"] for item in middle_page["items"]], message_ids[1:3]
        )
        self.assertTrue(middle_page["has_more"])
        self.assertEqual(middle_page["next_before_message_id"], message_ids[1])

        oldest_page = api.get_timeline(
            channel.id,
            before_message_id=middle_page["next_before_message_id"],
            limit=2,
        )
        self.assertEqual(
            [item["message_id"] for item in oldest_page["items"]], message_ids[:1]
        )
        self.assertFalse(oldest_page["has_more"])
        self.assertFalse(oldest_page["next_before_message_id"])

        chronological = (
            oldest_page["items"] + middle_page["items"] + newest_page["items"]
        )
        self.assertEqual([item["message_id"] for item in chronological], message_ids)
        malicious_item = chronological[0]
        self.assertEqual(malicious_item["body_text"], bodies[0])
        self.assertTrue(malicious_item["author"]["is_current_user"])
        self.assertNotIn("&lt;img", malicious_item["body_text"])
        self.assertNotIn("body", malicious_item)
        self.assertEqual(malicious_item["dispatch_state"], "pending")

    def test_ui_timeline_forward_cursor_returns_chronological_delta_pages(self):
        channel, _binding, _identity = self._channel_binding()
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        results = [
            api.send_message(
                channel.id,
                "forward %s" % index,
                client_request_id=str(uuid.uuid4()),
            )
            for index in range(5)
        ]
        message_ids = [result["message_id"] for result in results]

        first_delta = api.get_timeline(
            channel.id,
            after_message_id=message_ids[0],
            limit=2,
        )
        self.assertEqual(
            [item["message_id"] for item in first_delta["items"]],
            message_ids[1:3],
        )
        self.assertFalse(first_delta["has_more"])
        self.assertFalse(first_delta["next_before_message_id"])
        self.assertTrue(first_delta["has_more_forward"])
        self.assertEqual(first_delta["next_after_message_id"], message_ids[2])

        final_delta = api.get_timeline(
            channel.id,
            after_message_id=first_delta["next_after_message_id"],
            limit=2,
        )
        self.assertEqual(
            [item["message_id"] for item in final_delta["items"]],
            message_ids[3:],
        )
        self.assertFalse(final_delta["has_more"])
        self.assertFalse(final_delta["next_before_message_id"])
        self.assertFalse(final_delta["has_more_forward"])
        self.assertEqual(final_delta["next_after_message_id"], message_ids[-1])

        empty_delta = api.get_timeline(
            channel.id,
            after_message_id=final_delta["next_after_message_id"],
            limit=2,
        )
        self.assertFalse(empty_delta["items"])
        self.assertFalse(empty_delta["has_more_forward"])
        self.assertFalse(empty_delta["next_after_message_id"])

        with self.assertRaisesRegex(ValidationError, "mutually exclusive"):
            api.get_timeline(
                channel.id,
                before_message_id=message_ids[3],
                after_message_id=message_ids[0],
                limit=2,
            )
        with self.assertRaises(ValidationError):
            api.get_timeline(channel.id, after_message_id="01", limit=2)

    def test_ui_conversation_cursor_keeps_records_from_the_same_second(self):
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        application = self.env["contact.center.application"]
        channels = []
        for microsecond in (700000, 800000, 900000):
            channel, _binding, _identity = self._channel_binding()
            message_date = fields.Datetime.to_datetime("2026-08-21 12:00:00").replace(
                microsecond=microsecond
            )
            message = channel.with_user(self.agent)._contact_center_post(
                origin="outbound",
                body="same second %s" % microsecond,
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
                partner_ids=[],
                date=message_date,
            )
            application._publish_message_created(channel, message)
            channels.append(channel)

        channel_records = self.env["mail.channel"].browse(
            [channel.id for channel in channels]
        )
        channel_records.flush_recordset(["contact_center_last_message_at"])
        channel_records.invalidate_recordset(["contact_center_last_message_at"])
        self.assertEqual(
            channel_records.mapped("contact_center_last_message_at"),
            [fields.Datetime.to_datetime("2026-08-21 12:00:00")] * 3,
        )

        found = []
        cursor = False
        for _index in range(3):
            page = api.list_conversations(limit=1, cursor=cursor)
            self.assertEqual(len(page["items"]), 1)
            found.append(page["items"][0]["channel_id"])
            cursor = page["next_cursor"]
        self.assertEqual(found, list(reversed([channel.id for channel in channels])))
        self.assertFalse(cursor)

    def test_ui_send_message_is_idempotent_by_client_request_id(self):
        channel, binding, _identity = self._channel_binding()
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        client_request_id = str(uuid.uuid4())

        first = api.send_message(
            channel.id,
            "Send exactly once",
            client_request_id=client_request_id,
        )
        second = api.send_message(
            channel.id,
            "Send exactly once",
            client_request_id=client_request_id,
        )

        self.assertEqual(first["message_id"], second["message_id"])
        self.assertEqual(first["outbox_command_id"], second["outbox_command_id"])
        self.assertEqual(first["client_request_id"], client_request_id)
        self.assertEqual(second["client_request_id"], client_request_id)
        self.assertEqual(
            self.env["contact.center.outbox.command"]
            .sudo()
            .search_count(
                [
                    ("channel_binding_id", "=", binding.id),
                    ("ui_request_id", "=", client_request_id),
                ]
            ),
            1,
        )
        self.assertEqual(
            self.env["contact.center.message.binding"]
            .sudo()
            .search_count([("channel_binding_id", "=", binding.id)]),
            1,
        )
        with self.assertRaises(ValidationError):
            api.send_message(
                channel.id,
                "A different payload",
                client_request_id=client_request_id,
            )
        with self.assertRaises(ValidationError):
            api.send_message(
                channel.id,
                "Invalid request identifier",
                client_request_id="not-a-uuid",
            )

        reply_request_id = str(uuid.uuid4())
        first_reply = api.send_message(
            channel.id,
            "Reply before provider correlation",
            reply_to_message_id=first["message_id"],
            client_request_id=reply_request_id,
        )
        repeated_reply = api.send_message(
            channel.id,
            "Reply before provider correlation",
            reply_to_message_id=first["message_id"],
            client_request_id=reply_request_id,
        )
        self.assertEqual(first_reply["message_id"], repeated_reply["message_id"])
        self.assertEqual(
            first_reply["outbox_command_id"], repeated_reply["outbox_command_id"]
        )

    def test_ui_resend_creates_one_new_audited_attempt_and_never_reopens_source(self):
        channel, binding, _identity = self._channel_binding()
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        source_result = api.send_message(
            channel.id,
            "Retry this exact text",
            client_request_id=str(uuid.uuid4()),
        )
        source = self._mark_send_dead(source_result)
        first_request = str(uuid.uuid4())
        second_request = str(uuid.uuid4())

        failed_item = next(
            item
            for item in api.get_timeline(channel.id)["items"]
            if item["message_id"] == source_result["message_id"]
        )
        self.assertEqual(failed_item["dispatch_state"], "dead")
        self.assertEqual(failed_item["delivery_state"], "failed")
        self.assertEqual(failed_item["dispatch_reason"], "permanent_failure")
        self.assertTrue(failed_item["actions"]["resend"])

        first = api.resend_message(
            channel.id, source_result["message_id"], first_request
        )
        replay = api.resend_message(
            channel.id, source_result["message_id"], first_request
        )
        competing_click = api.resend_message(
            channel.id, source_result["message_id"], second_request
        )

        source.invalidate_recordset()
        retry = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(first["outbox_command_id"])
        )
        self.assertEqual(source.state, "dead")
        self.assertEqual(source.message_binding_id.delivery_state, "failed")
        self.assertEqual(source.retry_admission_revision, 1)
        self.assertEqual(len(source.retry_child_ids), 1)
        self.assertNotEqual(first["message_id"], source_result["message_id"])
        self.assertEqual(replay["message_id"], first["message_id"])
        self.assertEqual(competing_click["message_id"], first["message_id"])
        self.assertEqual(retry.retry_of_id, source)
        self.assertEqual(retry.retry_requested_by_id, self.agent)
        self.assertTrue(retry.retry_requested_at)
        self.assertEqual(retry.ui_request_id, first_request)
        self.assertEqual(retry.state, "pending")
        self.assertEqual(first["message"]["body_text"], "Retry this exact text")
        self.assertEqual(
            first["message"]["retry_of_message_id"], source_result["message_id"]
        )
        self.assertEqual(first["message"]["dispatch_reason"], "waiting_queue")
        self.assertFalse(first["source_message"]["actions"]["resend"])
        self.assertEqual(first["source_message"]["dispatch_reason"], "retry_created")
        self.assertEqual(
            self.env["contact.center.outbox.command"]
            .sudo()
            .search_count([("channel_binding_id", "=", binding.id)]),
            2,
        )
        self.assertEqual(
            self.env["contact.center.message.binding"]
            .sudo()
            .search_count([("channel_binding_id", "=", binding.id)]),
            2,
        )

        source.message_binding_id._contact_center_apply_delivery(
            "sent", external_message_id="late-source-evidence"
        )
        with mock.patch.object(FakeAdapter, "execute_command") as execute_command:
            self.assertFalse(self._process_outbox(retry))
        execute_command.assert_not_called()
        retry.invalidate_recordset(["state", "last_error_class"])
        self.assertEqual(retry.state, "cancelled")
        self.assertEqual(retry.last_error_class, "RetrySourceInvalidatedError")

    def test_ui_exposes_honest_outbox_wait_reasons_without_inbox_states(self):
        channel, _binding, _identity = self._channel_binding()
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        result = api.send_message(
            channel.id,
            "Waiting state",
            client_request_id=str(uuid.uuid4()),
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )

        def serialized():
            return next(
                item
                for item in api.get_timeline(channel.id)["items"]
                if item["message_id"] == result["message_id"]
            )

        self.assertEqual(serialized()["dispatch_reason"], "waiting_queue")
        outbox._persist_safe_retry(
            ProviderPausedError("fixture connection wait"), state="pending"
        )
        self.assertEqual(serialized()["dispatch_reason"], "waiting_connection")
        outbox.write({"last_error_class": "OutboundThrottleError"})
        self.assertEqual(serialized()["dispatch_reason"], "waiting_provider_limit")
        outbox._persist_safe_retry(
            ProviderRateLimitError("fixture provider rate limit"), state="retry"
        )
        self.assertEqual(serialized()["dispatch_reason"], "waiting_provider_limit")
        outbox._persist_safe_retry(
            RuntimeError("fixture automatic retry"), state="retry"
        )
        self.assertEqual(serialized()["dispatch_reason"], "automatic_retry")
        self.assertFalse(serialized()["actions"]["resend"])

        outbox_states = dict(outbox._fields["state"].selection)
        self.assertNotIn("blocked", outbox_states)
        self.assertNotIn("unsupported", outbox_states)

    def test_ui_resend_requires_dead_failed_send_capability_health_and_membership(self):
        channel, _binding, _identity = self._channel_binding()
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        result = api.send_message(
            channel.id, "Safe source", client_request_id=str(uuid.uuid4())
        )
        source = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        source.write({"state": "uncertain"})
        uncertain_item = next(
            item
            for item in api.get_timeline(channel.id)["items"]
            if item["message_id"] == result["message_id"]
        )
        self.assertEqual(uncertain_item["dispatch_reason"], "uncertain")
        self.assertFalse(uncertain_item["actions"]["resend"])
        with self.assertRaises(ValidationError):
            api.resend_message(channel.id, result["message_id"], str(uuid.uuid4()))
        source.write({"state": "cancelled"})
        cancelled_item = next(
            item
            for item in api.get_timeline(channel.id)["items"]
            if item["message_id"] == result["message_id"]
        )
        self.assertEqual(cancelled_item["dispatch_reason"], "cancelled")
        self.assertFalse(cancelled_item["actions"]["resend"])
        with self.assertRaises(ValidationError):
            api.resend_message(channel.id, result["message_id"], str(uuid.uuid4()))
        source.write({"state": "dead"})
        source.message_binding_id._contact_center_apply_delivery("failed")

        self.connection.capabilities_json = {"send_message": False}
        unavailable_item = next(
            item
            for item in api.get_timeline(channel.id)["items"]
            if item["message_id"] == result["message_id"]
        )
        self.assertEqual(
            unavailable_item["dispatch_reason"], "retry_capability_unavailable"
        )
        self.assertFalse(unavailable_item["actions"]["resend"])
        with self.assertRaises(UserError):
            api.resend_message(channel.id, result["message_id"], str(uuid.uuid4()))
        self.connection.capabilities_json = {"send_message": True}
        self.connection.state = "disconnected"
        unhealthy_item = next(
            item
            for item in api.get_timeline(channel.id)["items"]
            if item["message_id"] == result["message_id"]
        )
        self.assertEqual(
            unhealthy_item["dispatch_reason"], "retry_connection_unavailable"
        )
        self.assertFalse(unhealthy_item["actions"]["resend"])
        with self.assertRaisesRegex(UserError, "healthy"):
            api.resend_message(channel.id, result["message_id"], str(uuid.uuid4()))

        self.connection.state = "connected"
        outsider = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Contact Center Outsider",
                    "login": "cc-outsider-%s" % uuid.uuid4(),
                    "email": "cc-outsider@example.invalid",
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, [self.agent_group.id])],
                }
            )
        )
        with self.assertRaises(AccessError):
            self.env["contact.center.ui.api"].with_user(outsider).resend_message(
                channel.id, result["message_id"], str(uuid.uuid4())
            )
        self.assertFalse(source.retry_child_ids)

        source.message_binding_id._contact_center_apply_delivery(
            "sent", external_message_id="late-positive-evidence"
        )
        positive_evidence_item = next(
            item
            for item in api.get_timeline(channel.id)["items"]
            if item["message_id"] == result["message_id"]
        )
        self.assertFalse(positive_evidence_item["actions"]["resend"])
        with self.assertRaises(ValidationError):
            api.resend_message(channel.id, result["message_id"], str(uuid.uuid4()))
        self.assertFalse(source.retry_child_ids)

    def test_ui_resend_preserves_reply_relation_with_a_fresh_provider_snapshot(self):
        channel, _binding, _identity, target = self._outbound_mutation_target(
            "Reply target"
        )
        self.connection.capabilities_json = {
            **(self.connection.capabilities_json or {}),
            "reply": True,
        }
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        result = api.send_message(
            channel.id,
            "Preserved reply",
            reply_to_message_id=target.message_id.id,
            client_request_id=str(uuid.uuid4()),
        )
        source = self._mark_send_dead(result)
        target.protocol_snapshot_json = {"fresh": "provider evidence"}

        retried = api.resend_message(
            channel.id, result["message_id"], str(uuid.uuid4())
        )
        retry = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(retried["outbox_command_id"])
        )
        command = CommandDTO.from_dict(retry.command_json)

        self.assertEqual(retry.retry_of_id, source)
        self.assertEqual(retry.message_binding_id.reply_to_binding_id, target)
        self.assertEqual(
            retry.message_binding_id.message_id.parent_id, target.message_id
        )
        self.assertEqual(command.message.text, "Preserved reply")
        self.assertEqual(
            command.reply_to,
            {
                "external_message_id": target.external_message_id,
                "protocol_snapshot": {"fresh": "provider evidence"},
            },
        )

        target.write(
            {
                "message_state": "deleted",
                "deleted_display_mode": "redact",
                "deleted_at": fields.Datetime.now(),
            }
        )
        with mock.patch.object(FakeAdapter, "execute_command") as execute_command:
            self.assertFalse(self._process_outbox(retry))
        execute_command.assert_not_called()
        retry.invalidate_recordset(["state"])
        self.assertEqual(retry.state, "cancelled")

    def test_ui_operations_preserve_guest_and_historical_authorship(self):
        channel, _binding, identity = self._channel_binding()
        tag = self.env["contact.center.tag"].create(
            {
                "name": "Priority",
                "company_id": self.env.company.id,
                "color": 2,
            }
        )
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        bootstrap = api.bootstrap()
        self.assertTrue(
            bootstrap["capabilities"]["create_contact"],
            "the capability follows the scoped promotion endpoint policy",
        )
        self.assertEqual(
            bootstrap["states"]["conversation"],
            [
                {"key": "open", "label": "Aberta"},
                {"key": "resolved", "label": "Resolvida"},
                {"key": "archived", "label": "Arquivada"},
            ],
        )
        with self.assertRaises(ValidationError):
            api.list_conversations(filters={"state": "pending"})

        updated = api.update_conversation(
            channel.id, {"state": "resolved", "tag_ids": [tag.id]}
        )
        channel.invalidate_recordset(["contact_center_state", "contact_center_tag_ids"])
        self.assertEqual(updated["item"]["state"], "resolved")
        self.assertEqual(
            updated["item"]["tags"], [{"id": tag.id, "name": tag.name, "color": 2}]
        )
        self.assertEqual(channel.contact_center_state, "resolved")
        self.assertEqual(channel.contact_center_tag_ids, tag)

        with self.assertRaises(ValidationError):
            api.update_conversation(channel.id, {"state": "pending"})

        claimed = api.claim_conversation(channel.id)
        channel.invalidate_recordset(["contact_center_responsible_id"])
        self.assertEqual(claimed["item"]["responsible"]["id"], self.agent.id)
        self.assertEqual(channel.contact_center_responsible_id, self.agent)
        self.team.write({"supervisor_ids": [(4, self.admin.id)]})
        channel.sudo().with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write({"contact_center_responsible_id": self.admin.id})
        with self.assertRaises(AccessError):
            api.claim_conversation(channel.id)

        original_guest = identity.mail_guest_id
        guest_member_ids = channel.sudo().channel_member_ids.filtered("guest_id").ids
        public_user = self.env.ref("base.public_user")
        inbound = (
            channel.with_user(public_user)
            .sudo()
            .with_context(guest=original_guest.sudo())
            ._contact_center_post(
                origin="inbound",
                body="Historical guest message",
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
                partner_ids=[],
            )
        )
        self.env["contact.center.application"]._publish_message_created(
            channel, inbound
        )
        self.assertEqual(inbound.author_guest_id, original_guest)
        self.assertFalse(inbound.author_id)

        partner_count = self.env["res.partner"].search_count([])
        promoted = api.create_and_link_partner(
            channel.id,
            {
                "name": "Promoted Person",
                "email": "promoted@example.invalid",
                "phone": "+5511999999999",
            },
        )
        duplicate_submit = api.create_and_link_partner(
            channel.id,
            {
                "name": "Duplicate Submit Must Not Create",
                "phone": "+5511888888888",
            },
        )
        self.assertTrue(promoted["created"])
        self.assertFalse(duplicate_submit["created"])
        self.assertEqual(duplicate_submit["partner"]["id"], promoted["partner"]["id"])
        self.assertEqual(self.env["res.partner"].search_count([]), partner_count + 1)
        identity.invalidate_recordset(["partner_id", "mail_guest_id"])
        channel.invalidate_recordset(["channel_member_ids"])
        inbound.invalidate_recordset(["author_guest_id", "author_id"])

        self.assertEqual(identity.partner_id.id, promoted["partner"]["id"])
        self.assertEqual(identity.partner_link_kind, "person")
        self.assertEqual(identity.mail_guest_id, original_guest)
        self.assertEqual(
            channel.sudo().channel_member_ids.filtered("guest_id").ids,
            guest_member_ids,
        )
        self.assertEqual(inbound.author_guest_id, original_guest)
        self.assertFalse(inbound.author_id)
        self.assertEqual(promoted["identity"]["persona_kind"], "contact")
        self.assertEqual(promoted["identity"]["guest"]["id"], original_guest.id)
        conversation = api.get_conversation(channel.id)["item"]
        self.assertEqual(conversation["identity"]["persona_kind"], "contact")
        self.assertEqual(
            conversation["identity"]["partner"]["id"], identity.partner_id.id
        )

    def test_ui_links_or_creates_a_company_for_an_existing_contact(self):
        channel, _binding, identity = self._channel_binding()
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        capabilities = api.bootstrap()["capabilities"]
        self.assertFalse(capabilities["link_company"])
        self.assertFalse(capabilities["create_company"])
        with self.assertRaises(AccessError):
            api.search_partner_companies(channel.id, 999999, "Candidate")
        with self.assertRaises(AccessError):
            api.create_and_link_partner_company(
                channel.id, 999999, {"name": "Company Before Contact"}
            )

        supervisor_group = self.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        self.agent.write({"groups_id": [(4, supervisor_group.id)]})
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        capabilities = api.bootstrap()["capabilities"]
        self.assertTrue(capabilities["link_company"])
        self.assertTrue(capabilities["create_company"])
        with self.assertRaises(ValidationError):
            api.search_partner_companies(channel.id, 999999, "Candidate")
        with self.assertRaises(ValidationError):
            api.create_and_link_partner_company(
                channel.id, 999999, {"name": "Company Before Contact"}
            )

        person = self.env["res.partner"].create(
            {
                "name": "Leonardo Hirata",
                "email": "leonardo@example.invalid",
                "phone": "+5515991093512",
                "street": "Personal Street 1",
                "company_id": self.env.company.id,
                "company_type": "person",
                "type": "contact",
            }
        )
        identity.with_user(self.agent).action_link_partner(person.id)
        original_guest = identity.mail_guest_id
        original_person_values = person.read(["name", "email", "phone"])[0]

        local_company = self.env["res.partner"].create(
            {
                "name": "Candidate Local Company",
                "email": "local-company@example.invalid",
                "vat": "12345678000195",
                "street": "Company Avenue 5",
                "is_company": True,
                "company_id": self.env.company.id,
                "type": "contact",
            }
        )
        global_company = self.env["res.partner"].create(
            {
                "name": "Candidate Global Company",
                "is_company": True,
                "company_id": False,
                "type": "contact",
            }
        )
        another_local_company = self.env["res.partner"].create(
            {
                "name": "Candidate Replacement Company",
                "is_company": True,
                "company_id": self.env.company.id,
                "type": "contact",
            }
        )
        individual = self.env["res.partner"].create(
            {
                "name": "Candidate Individual",
                "company_id": self.env.company.id,
                "company_type": "person",
                "type": "contact",
            }
        )
        archived_company = self.env["res.partner"].create(
            {
                "name": "Candidate Archived Company",
                "active": False,
                "is_company": True,
                "company_id": self.env.company.id,
                "type": "contact",
            }
        )
        other_company = self.env["res.company"].create(
            {"name": "Contact Center Foreign Company"}
        )
        foreign_company = self.env["res.partner"].create(
            {
                "name": "Candidate Foreign Company",
                "is_company": True,
                "company_id": other_company.id,
                "type": "contact",
            }
        )

        company_items = api.search_partner_companies(
            channel.id, person.id, "Candidate"
        )["items"]
        company_ids = {item["id"] for item in company_items}
        self.assertIn(local_company.id, company_ids)
        self.assertIn(global_company.id, company_ids)
        self.assertNotIn(individual.id, company_ids)
        self.assertNotIn(archived_company.id, company_ids)
        self.assertNotIn(foreign_company.id, company_ids)
        contact_ids = {
            item["id"] for item in api.search_partners(channel.id, "Candidate")["items"]
        }
        self.assertIn(individual.id, contact_ids)
        self.assertNotIn(local_company.id, contact_ids)
        internal_contact_ids = {
            item["id"]
            for item in api.search_partners(channel.id, "Contact Center Administrator")[
                "items"
            ]
        }
        self.assertNotIn(self.admin.partner_id.id, internal_contact_ids)

        with self.assertRaises(ValidationError):
            api.link_partner_company(channel.id, individual.id, local_company.id)
        with self.assertRaises(ValidationError):
            api.link_partner_company(channel.id, person.id, individual.id)
        with self.assertRaises(ValidationError):
            api.link_partner_company(channel.id, person.id, foreign_company.id)
        with self.assertRaises(ValidationError):
            identity.with_user(self.agent).action_link_partner(local_company.id)
        with self.assertRaises(AccessError):
            person.with_user(self.agent).write({"parent_id": local_company.id})

        person_user_checks = 0

        def internal_user_appears_after_initial_check(_api, candidate):
            nonlocal person_user_checks
            if candidate.id == person.id:
                person_user_checks += 1
                return person_user_checks > 1
            return False

        api_model_type = type(self.env["contact.center.ui.api"])
        with mock.patch.object(
            api_model_type,
            "_partner_has_internal_user",
            autospec=True,
            side_effect=internal_user_appears_after_initial_check,
        ), self.assertRaises(ValidationError):
            api.link_partner_company(channel.id, person.id, local_company.id)
        self.assertEqual(person_user_checks, 2)
        self.assertFalse(person.parent_id)

        shared_channel, _shared_binding, shared_identity = self._channel_binding()
        shared_identity.with_user(self.agent).action_link_partner(person.id)
        notified_channels = []

        def capture_event(_application, notified_channel, event_type, _payload, **_kw):
            if event_type == "identity_updated":
                notified_channels.append(notified_channel.id)
            return True

        application_type = type(self.env["contact.center.application"])
        with mock.patch.object(
            application_type,
            "_notify_ui",
            autospec=True,
            side_effect=capture_event,
        ):
            linked = api.link_partner_company(channel.id, person.id, local_company.id)

        person.invalidate_recordset(
            ["parent_id", "display_name", "name", "email", "phone", "street", "vat"]
        )
        identity.invalidate_recordset(["partner_id", "mail_guest_id"])
        self.assertEqual(person.parent_id, local_company)
        self.assertEqual(
            person.read(["name", "email", "phone"])[0], original_person_values
        )
        # parent_id is deliberately the native Odoo commercial relation: its
        # standard field sync adopts the commercial entity's fiscal/address
        # values while preserving the person's own identity fields above.
        self.assertEqual(person.street, local_company.street)
        self.assertEqual(person.vat, local_company.vat)
        self.assertEqual(identity.partner_id, person)
        self.assertEqual(identity.mail_guest_id, original_guest)
        self.assertEqual(linked["identity"]["name"], "Leonardo Hirata")
        self.assertEqual(linked["identity"]["partner"]["name"], "Leonardo Hirata")
        self.assertEqual(
            linked["identity"]["partner"]["company"]["id"], local_company.id
        )
        self.assertFalse(linked["identity"]["partner"]["company_linking_allowed"])
        self.assertCountEqual(notified_channels, [channel.id, shared_channel.id])

        with mock.patch.object(
            application_type, "_notify_ui", autospec=True
        ) as notify_retry:
            repeated = api.link_partner_company(channel.id, person.id, local_company.id)
        notify_retry.assert_not_called()
        self.assertEqual(repeated["company"]["id"], local_company.id)
        with self.assertRaises(ValidationError):
            api.link_partner_company(channel.id, person.id, another_local_company.id)
        self.assertEqual(
            api.search_partner_companies(channel.id, person.id, "Candidate")["items"],
            [],
        )

        (
            global_target_channel,
            _global_target_binding,
            global_target_identity,
        ) = self._channel_binding()
        global_target_person = self.env["res.partner"].create(
            {
                "name": "Contact For Global Company",
                "company_id": self.env.company.id,
                "company_type": "person",
                "type": "contact",
            }
        )
        global_target_identity.with_user(self.agent).action_link_partner(
            global_target_person.id
        )
        linked_global = api.link_partner_company(
            global_target_channel.id, global_target_person.id, global_company.id
        )
        self.assertEqual(global_target_person.parent_id, global_company)
        self.assertEqual(linked_global["company"]["id"], global_company.id)

        company_count = self.env["res.partner"].search_count(
            [("is_company", "=", True)]
        )
        with self.assertRaises(ValidationError):
            api.create_and_link_partner_company(
                channel.id, person.id, {"name": "Must Not Duplicate"}
            )
        self.assertEqual(
            self.env["res.partner"].search_count([("is_company", "=", True)]),
            company_count,
        )

        create_channel, _create_binding, create_identity = self._channel_binding()
        create_person = self.env["res.partner"].create(
            {
                "name": "Contact For Company Creation",
                "email": "person@example.invalid",
                "phone": "+5511999990000",
                "company_id": self.env.company.id,
                "company_type": "person",
                "type": "contact",
            }
        )
        create_identity.with_user(self.agent).action_link_partner(create_person.id)
        company_count = self.env["res.partner"].search_count(
            [("is_company", "=", True)]
        )
        with mock.patch.object(
            application_type, "_notify_ui", autospec=True
        ) as notify_create:
            created = api.create_and_link_partner_company(
                create_channel.id,
                create_person.id,
                {
                    "name": "Created Company",
                    "vat": "98765432000198",
                    "email": "created-company@example.invalid",
                    "phone": "+551132165400",
                },
            )
        self.assertEqual(notify_create.call_count, 1)
        self.assertEqual(notify_create.call_args.args[1], create_channel)
        self.assertEqual(notify_create.call_args.args[2], "identity_updated")
        self.assertEqual(
            notify_create.call_args.args[3], {"identity_id": create_identity.id}
        )
        with self.assertRaises(ValidationError):
            api.create_and_link_partner_company(
                create_channel.id,
                create_person.id,
                {"name": "Duplicate Submit Must Not Create"},
            )
        created_company = self.env["res.partner"].browse(created["company"]["id"])
        create_person.invalidate_recordset(["parent_id", "name", "email", "phone"])
        self.assertEqual(
            self.env["res.partner"].search_count([("is_company", "=", True)]),
            company_count + 1,
        )
        self.assertTrue(created_company.is_company)
        self.assertEqual(created_company.type, "contact")
        self.assertEqual(created_company.company_id, self.env.company)
        self.assertEqual(created_company.vat, "98765432000198")
        self.assertEqual(create_person.parent_id, created_company)
        self.assertEqual(create_person.name, "Contact For Company Creation")
        self.assertEqual(create_person.email, "person@example.invalid")
        self.assertEqual(create_person.phone, "+5511999990000")
        with self.assertRaises(ValidationError):
            api.create_and_link_partner_company(
                create_channel.id,
                create_person.id,
                {"name": "Unsupported", "country_id": 1},
            )
        with self.assertRaises(ValidationError):
            api.create_and_link_partner_company(
                create_channel.id,
                create_person.id,
                {"name": "Invalid field type", "phone": 0},
            )

        global_channel, _global_binding, global_identity = self._channel_binding()
        global_person = self.env["res.partner"].create(
            {
                "name": "Global Person",
                "company_id": False,
                "company_type": "person",
                "type": "contact",
            }
        )
        global_identity.with_user(self.agent).action_link_partner(global_person.id)
        self.assertFalse(
            api.get_conversation(global_channel.id)["item"]["identity"]["partner"][
                "company_linking_allowed"
            ]
        )
        with self.assertRaises(ValidationError):
            api.create_and_link_partner_company(
                global_channel.id,
                global_person.id,
                {"name": "Forbidden Global Mutation"},
            )

    def test_ui_links_or_creates_an_explicit_central_company(self):
        channel, _binding, identity = self._channel_binding()
        original_guest = identity.mail_guest_id
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        capabilities = api.bootstrap()["capabilities"]
        self.assertFalse(capabilities["link_central_company"])
        self.assertFalse(capabilities["create_central_company"])
        with self.assertRaises(AccessError):
            api.search_central_companies(channel.id, "Central")

        local_company = self.env["res.partner"].create(
            {
                "name": "Centralized Service Company",
                "is_company": True,
                "company_id": self.env.company.id,
                "type": "contact",
                "vat": "12345678000195",
            }
        )
        with self.assertRaises(AccessError):
            identity.with_user(self.agent).action_link_central_company(local_company.id)

        supervisor_group = self.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        self.agent.write({"groups_id": [(4, supervisor_group.id)]})
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        capabilities = api.bootstrap()["capabilities"]
        self.assertTrue(capabilities["link_central_company"])
        self.assertTrue(capabilities["create_central_company"])

        global_company = self.env["res.partner"].create(
            {
                "name": "Centralized Global Company",
                "is_company": True,
                "company_id": False,
                "type": "contact",
            }
        )
        person = self.env["res.partner"].create(
            {
                "name": "Centralized Named Person",
                "company_id": self.env.company.id,
                "company_type": "person",
                "type": "contact",
            }
        )
        other_odoo_company = self.env["res.company"].create(
            {"name": "Central Company Foreign Scope"}
        )
        foreign_company = self.env["res.partner"].create(
            {
                "name": "Centralized Foreign Company",
                "is_company": True,
                "company_id": other_odoo_company.id,
                "type": "contact",
            }
        )
        company_ids = {
            item["id"]
            for item in api.search_central_companies(channel.id, "Centralized")["items"]
        }
        self.assertIn(local_company.id, company_ids)
        self.assertIn(global_company.id, company_ids)
        self.assertNotIn(person.id, company_ids)
        self.assertNotIn(foreign_company.id, company_ids)
        with self.assertRaises(ValidationError):
            api.link_central_company(channel.id, person.id)
        # The ordinary promotion contract remains person-only.
        with self.assertRaises(ValidationError):
            identity.with_user(self.agent).action_link_partner(local_company.id)

        second_account = self.env["contact.center.account"].create(
            {
                "name": "Second Central Inbox",
                "company_id": self.env.company.id,
                "platform": "whatsapp",
                "external_ref": "central-account-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, self.team.ids)],
            }
        )
        second_channel = self.env["mail.channel"]._contact_center_create_channel(
            account=second_account,
            identity=identity,
            teams=self.team,
            partner_ids=self.agent.partner_id.ids,
            guest_ids=identity.mail_guest_id.ids,
        )
        self.env["contact.center.channel.binding"].sudo().create(
            {
                "channel_id": second_channel.id,
                "account_id": second_account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": "central-conversation-%s" % uuid.uuid4(),
            }
        )

        notified_channels = []

        def capture_event(_application, notified_channel, event_type, _payload, **_kw):
            if event_type == "identity_updated":
                notified_channels.append(notified_channel.id)
            return True

        application_type = type(self.env["contact.center.application"])
        with mock.patch.object(
            application_type,
            "_notify_ui",
            autospec=True,
            side_effect=capture_event,
        ):
            linked = api.link_central_company(channel.id, local_company.id)
        identity.invalidate_recordset(["partner_id", "mail_guest_id"])
        self.assertEqual(identity.partner_id, local_company)
        self.assertEqual(identity.mail_guest_id, original_guest)
        self.assertTrue(linked["identity"]["partner"]["is_company"])
        self.assertEqual(linked["identity"]["link_kind"], "central_company")
        self.assertTrue(linked["identity"]["link_invariant_valid"])
        self.assertEqual(linked["identity"]["partner"]["vat"], "12345678000195")
        self.assertEqual(linked["company"]["id"], local_company.id)
        self.assertCountEqual(notified_channels, [channel.id, second_channel.id])

        with mock.patch.object(
            application_type, "_notify_ui", autospec=True
        ) as notify_retry:
            repeated = api.link_central_company(channel.id, local_company.id)
        notify_retry.assert_not_called()
        self.assertEqual(repeated["company"]["id"], local_company.id)
        with self.assertRaises(ValidationError):
            api.link_central_company(channel.id, global_company.id)
        self.assertEqual(
            api.search_central_companies(channel.id, "Centralized")["items"], []
        )
        self.agent.write({"groups_id": [(3, supervisor_group.id)]})
        # A linked central number is now a durable company classification:
        # Contacts may maintain ordinary fields, but cannot silently turn the
        # linked company into a person and leave a drifted identity behind.
        with self.assertRaises(ValidationError):
            local_company.write({"is_company": False})
        identity.invalidate_recordset(["partner_id", "partner_link_kind"])
        linked = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .get_conversation(channel.id)["item"]["identity"]
        )
        self.assertEqual(identity.partner_link_kind, "central_company")
        self.assertTrue(linked["link_invariant_valid"])
        with self.assertRaises(AccessError):
            self.env["contact.center.ui.api"].with_user(self.agent).unlink_partner(
                channel.id, local_company.id
            )
        self.assertEqual(identity.partner_id, local_company)
        self.agent.write({"groups_id": [(4, supervisor_group.id)]})

        create_channel, _create_binding, create_identity = self._channel_binding()
        create_guest = create_identity.mail_guest_id
        company_count = self.env["res.partner"].search_count(
            [("is_company", "=", True)]
        )
        created = api.create_and_link_central_company(
            create_channel.id,
            {
                "name": "Created Central Company",
                "vat": "98765432000198",
                "phone": "+551132165400",
            },
        )
        duplicate_submit = api.create_and_link_central_company(
            create_channel.id, {"name": "Must Not Create a Duplicate"}
        )
        self.assertTrue(created["created"])
        self.assertFalse(duplicate_submit["created"])
        create_identity.invalidate_recordset(["partner_id", "mail_guest_id"])
        self.assertEqual(
            self.env["res.partner"].search_count([("is_company", "=", True)]),
            company_count + 1,
        )
        self.assertEqual(duplicate_submit["company"]["id"], created["company"]["id"])
        self.assertEqual(create_identity.partner_id.id, created["company"]["id"])
        self.assertTrue(create_identity.partner_id.is_company)
        self.assertEqual(create_identity.partner_link_kind, "central_company")
        self.assertEqual(create_identity.partner_id.vat, "98765432000198")
        self.assertEqual(create_identity.mail_guest_id, create_guest)
        with self.assertRaises(ValidationError):
            api.create_and_link_central_company(
                create_channel.id,
                {"name": "Unsupported", "country_id": self.env.ref("base.us").id},
            )

    def test_ui_partner_link_and_unlink_are_serialized_and_idempotent(self):
        channel, _binding, identity = self._channel_binding()
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        first = self.env["res.partner"].create(
            {
                "name": "First Serialized Contact",
                "company_id": self.env.company.id,
                "company_type": "person",
                "type": "contact",
            }
        )
        second = self.env["res.partner"].create(
            {
                "name": "Second Serialized Contact",
                "company_id": self.env.company.id,
                "company_type": "person",
                "type": "contact",
            }
        )
        application_type = type(self.env["contact.center.application"])
        with mock.patch.object(
            application_type, "_notify_ui", autospec=True
        ) as notify_link:
            api.link_partner(channel.id, first.id)
            api.link_partner(channel.id, first.id)
        self.assertEqual(notify_link.call_count, 1)
        with self.assertRaises(ValidationError):
            api.link_partner(channel.id, second.id)
        with self.assertRaises(ValidationError):
            api.unlink_partner(channel.id, second.id)
        self.assertEqual(identity.partner_id, first)
        with mock.patch.object(
            application_type, "_notify_ui", autospec=True
        ) as notify_unlink:
            api.unlink_partner(channel.id, first.id)
            api.unlink_partner(channel.id, first.id)
        self.assertEqual(notify_unlink.call_count, 1)
        with self.assertRaises(ValidationError):
            api.unlink_partner(channel.id)
        identity.invalidate_recordset(["partner_id"])
        self.assertFalse(identity.partner_id)

    def test_ui_guest_rename_is_scoped_and_becomes_manual(self):
        channel, _binding, identity = self._channel_binding()
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        self.assertTrue(api.bootstrap()["capabilities"]["rename_guest"])

        renamed = api.rename_guest(channel.id, "  Cliente do laboratório  ")
        identity.invalidate_recordset(["name", "name_source", "mail_guest_id"])
        channel.invalidate_recordset(["name"])
        self.assertEqual(renamed["identity"]["name"], "Cliente do laboratório")
        self.assertEqual(identity.name, "Cliente do laboratório")
        self.assertEqual(identity.name_source, "manual")
        self.assertEqual(identity.mail_guest_id.name, "Cliente do laboratório")
        self.assertEqual(channel.name, "Cliente do laboratório")

        with self.assertRaises(ValidationError):
            api.rename_guest(channel.id, "  ...  ")

        contact = self.env["res.partner"].create({"name": "Contato vinculado"})
        identity.with_user(self.agent).action_link_partner(contact.id)
        with self.assertRaises(UserError):
            api.rename_guest(channel.id, "Não deve substituir contato")

    def test_ui_api_rejects_channel_outside_allowed_companies(self):
        other_company = self.env["res.company"].create(
            {"name": "Contact Center Other Company"}
        )
        self.agent.write({"company_ids": [(4, other_company.id)]})
        other_team = self.env["contact.center.team"].create(
            {
                "name": "Other Company Team",
                "company_id": other_company.id,
                "agent_ids": [(6, 0, self.agent.ids)],
            }
        )
        other_account = self.env["contact.center.account"].create(
            {
                "name": "Other Company WhatsApp",
                "company_id": other_company.id,
                "platform": "whatsapp",
                "external_ref": "other-company-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, other_team.ids)],
            }
        )
        guest = self.env["mail.guest"].sudo().create({"name": "Other Company Guest"})
        identity = (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": "Other Company Identity",
                    "company_id": other_company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=other_account,
            identity=identity,
            teams=other_team,
            partner_ids=self.agent.partner_id.ids,
            guest_ids=guest.ids,
        )
        self.env["contact.center.channel.binding"].sudo().create(
            {
                "channel_id": channel.id,
                "account_id": other_account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": "other-company-conversation",
            }
        )

        api = self.env["contact.center.ui.api"].with_user(self.agent)
        with self.assertRaises(AccessError):
            api.with_context(allowed_company_ids=self.env.company.ids).get_conversation(
                channel.id
            )
        allowed = api.with_context(
            allowed_company_ids=(self.env.company | other_company).ids
        ).get_conversation(channel.id)
        self.assertEqual(allowed["item"]["channel_id"], channel.id)

    def test_outbox_delivery_transitions_publish_sanitized_ui_events(self):
        sent_channel, _binding, _identity = self._channel_binding()
        sent_result = self._send_message_without_enqueue(sent_channel, "Will be sent")
        sent_outbox = self.env["contact.center.outbox.command"].browse(
            sent_result["outbox_command_id"]
        )

        failed_channel, _binding, _identity = self._channel_binding()
        failed_result = self._send_message_without_enqueue(
            failed_channel, "Will fail permanently"
        )
        failed_outbox = self.env["contact.center.outbox.command"].browse(
            failed_result["outbox_command_id"]
        )
        failed_outbox.command_json = {"invalid": True}

        uncertain_channel, _binding, _identity = self._channel_binding()
        uncertain_result = self._send_message_without_enqueue(
            uncertain_channel, "Provider outcome is uncertain"
        )
        uncertain_outbox = self.env["contact.center.outbox.command"].browse(
            uncertain_result["outbox_command_id"]
        )
        uncertain_job_uuid = str(uuid.uuid4())
        uncertain_outbox.write(
            {
                "state": "processing",
                "attempts": 1,
                "queue_job_uuid": uncertain_job_uuid,
                "dispatch_job_uuid": uncertain_job_uuid,
            }
        )

        captured = []

        def capture_event(_application, channel, event_type, payload, partner_ids=None):
            captured.append(
                {
                    "channel_id": channel.id,
                    "event_type": event_type,
                    "payload": dict(payload),
                    "partner_ids": partner_ids,
                }
            )
            return True

        application_type = type(self.env["contact.center.application"])
        with mock.patch.object(
            application_type,
            "_notify_ui",
            autospec=True,
            side_effect=capture_event,
        ):
            self.assertTrue(self._process_outbox(sent_outbox))
            self.assertFalse(self._process_outbox(failed_outbox))
            self.assertFalse(self._process_outbox(uncertain_outbox, uncertain_job_uuid))

        delivery_events = [
            event for event in captured if event["event_type"] == "delivery_updated"
        ]
        self.assertEqual(len(delivery_events), 3)
        by_message = {
            event["payload"]["message_id"]: event for event in delivery_events
        }
        self.assertEqual(
            by_message[sent_result["message_id"]]["payload"],
            {
                "message_id": sent_result["message_id"],
                "state": "sent",
                "dispatch_state": "done",
            },
        )
        self.assertEqual(
            by_message[failed_result["message_id"]]["payload"],
            {
                "message_id": failed_result["message_id"],
                "state": "failed",
                "dispatch_state": "dead",
            },
        )
        self.assertEqual(
            by_message[uncertain_result["message_id"]]["payload"],
            {
                "message_id": uncertain_result["message_id"],
                "state": "queued",
                "dispatch_state": "uncertain",
            },
        )
        self.assertEqual(
            by_message[sent_result["message_id"]]["channel_id"], sent_channel.id
        )
        self.assertEqual(
            by_message[failed_result["message_id"]]["channel_id"], failed_channel.id
        )
        self.assertEqual(
            by_message[uncertain_result["message_id"]]["channel_id"],
            uncertain_channel.id,
        )

    def test_identity_and_alias_constraints(self):
        identity = self._identity()
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env["contact.center.identity"].sudo().create(
                {
                    "name": "Duplicate Guest",
                    "company_id": self.env.company.id,
                    "mail_guest_id": identity.mail_guest_id.id,
                }
            )
        values = {
            "identity_id": identity.id,
            "account_id": self.account.id,
            "namespace": "whatsapp.lid",
            "value_raw": "unique@lid",
            "value_normalized": "unique@lid",
        }
        self.env["contact.center.identity.alias"].sudo().create(values)
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env["contact.center.identity.alias"].sudo().create(values)

    def test_alias_last_seen_is_throttled_but_material_updates_are_immediate(self):
        _channel, binding, identity = self._channel_binding()
        identity_value = "5511999990000@s.whatsapp.net"
        identity_alias = (
            self.env["contact.center.identity.alias"]
            .sudo()
            .create(
                {
                    "identity_id": identity.id,
                    "account_id": self.account.id,
                    "namespace": "whatsapp.pn",
                    "value_raw": identity_value,
                    "value_normalized": identity_value,
                    "confidence": "observed",
                    "resolution_scope": "account",
                }
            )
        )
        channel_alias = binding.alias_ids[0]
        recent = fields.Datetime.to_datetime(fields.Datetime.now()).replace(
            microsecond=0
        ) - datetime.timedelta(minutes=1)
        identity_alias.last_seen_at = recent
        channel_alias.last_seen_at = recent
        identity_address = AddressDTO(
            namespace=identity_alias.namespace,
            value=identity_alias.value_raw,
            value_normalized=identity_alias.value_normalized,
        )
        channel_address = AddressDTO(
            namespace=channel_alias.namespace,
            value=channel_alias.value_raw,
            value_normalized=channel_alias.value_normalized,
            role=channel_alias.role,
        )
        application = self.env["contact.center.application"]

        application._enrich_identity_aliases(self.account, identity, [identity_address])
        application._enrich_channel_aliases(binding, [channel_address])
        identity_alias.invalidate_recordset(["last_seen_at"])
        channel_alias.invalidate_recordset(["last_seen_at"])
        self.assertEqual(identity_alias.last_seen_at, recent)
        self.assertEqual(channel_alias.last_seen_at, recent)

        stale = recent - datetime.timedelta(minutes=5)
        identity_alias.last_seen_at = stale
        channel_alias.last_seen_at = stale
        application._enrich_identity_aliases(self.account, identity, [identity_address])
        application._enrich_channel_aliases(binding, [channel_address])
        identity_alias.invalidate_recordset(["last_seen_at"])
        channel_alias.invalidate_recordset(["last_seen_at"])
        self.assertGreater(identity_alias.last_seen_at, stale)
        self.assertGreater(channel_alias.last_seen_at, stale)

        identity_alias.last_seen_at = recent
        promoted_address = AddressDTO(
            namespace=identity_alias.namespace,
            value=identity_value.upper(),
            value_normalized=identity_value,
            source_field="provider.phone_number",
            confidence="protocol",
            resolution_scope="company",
        )
        application._enrich_identity_aliases(self.account, identity, [promoted_address])
        identity_alias.invalidate_recordset(
            [
                "value_raw",
                "source_field",
                "confidence",
                "resolution_scope",
                "last_seen_at",
            ]
        )
        self.assertEqual(identity_alias.value_raw, identity_value.upper())
        self.assertEqual(identity_alias.source_field, "provider.phone_number")
        self.assertEqual(identity_alias.confidence, "protocol")
        self.assertEqual(identity_alias.resolution_scope, "company")
        self.assertGreater(identity_alias.last_seen_at, recent)

    def test_suggested_phone_never_uses_whatsapp_lid(self):
        _channel, binding, identity = self._channel_binding()
        alias_model = self.env["contact.center.identity.alias"].sudo()
        alias_model.create(
            {
                "identity_id": identity.id,
                "account_id": self.account.id,
                "namespace": "whatsapp.lid",
                "value_raw": "700000000000396@lid",
                "value_normalized": "700000000000396@lid",
                "confidence": "protocol",
            }
        )
        ui_api = self.env["contact.center.ui.api"]

        self.assertEqual(ui_api._suggested_phone(identity, binding=binding), "")
        self.assertEqual(
            ui_api._serialize_identity(identity, binding=binding)["suggested_phone"],
            "",
        )

        alias_model.create(
            {
                "identity_id": identity.id,
                "account_id": self.account.id,
                "namespace": "whatsapp.pn",
                "value_raw": "5511900000396@s.whatsapp.net",
                "value_normalized": "5511900000396@s.whatsapp.net",
                "confidence": "protocol",
                "resolution_scope": "company",
            }
        )
        self.assertEqual(
            ui_api._suggested_phone(identity, binding=binding), "+5511900000396"
        )

    def test_identity_metadata_is_member_scoped_and_promotion_is_explicit(self):
        _channel, binding, identity = self._channel_binding()
        outsider = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Contact Center Outsider",
                    "login": "cc-outsider-%s" % uuid.uuid4(),
                    "email": "cc-outsider@example.invalid",
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [(6, 0, [self.agent_group.id])],
                }
            )
        )
        contact = self.env["res.partner"].create({"name": "Promoted Contact"})

        self.assertEqual(
            self.env["contact.center.identity"]
            .with_user(self.agent)
            .search([("id", "=", identity.id)]),
            identity,
        )
        self.assertEqual(
            self.env["contact.center.channel.binding"]
            .with_user(self.agent)
            .search([("id", "=", binding.id)]),
            binding,
        )
        self.assertFalse(
            self.env["contact.center.identity"]
            .with_user(outsider)
            .search([("id", "=", identity.id)])
        )
        self.assertFalse(
            self.env["contact.center.identity.alias"]
            .with_user(outsider)
            .search([("identity_id", "=", identity.id)])
        )
        with self.assertRaises(AccessError):
            identity.with_user(outsider).action_link_partner(contact.id)
        with self.assertRaises(AccessError):
            identity.with_user(self.agent).write({"partner_id": contact.id})

        identity.with_user(self.agent).action_link_partner(contact.id)
        self.assertEqual(identity.partner_id, contact)
        self.assertEqual(identity.partner_link_kind, "person")
        self.assertEqual(identity.partner_linked_by_id, self.agent)
        self.assertTrue(identity.partner_linked_at)
        identity.with_user(self.agent).action_unlink_partner(contact.id)
        self.assertFalse(identity.partner_id)
        self.assertFalse(identity.partner_link_kind)

    def test_identity_conflict_survives_worker_savepoint_rollback(self):
        first_identity = self._identity("First Candidate")
        second_identity = self._identity("Second Candidate")
        lid = "%s@lid" % uuid.uuid4()
        pn = "%s@s.whatsapp.net" % uuid.uuid4()
        alias_model = self.env["contact.center.identity.alias"].sudo()
        alias_model.create(
            {
                "identity_id": first_identity.id,
                "account_id": self.account.id,
                "namespace": "whatsapp.lid",
                "value_raw": lid,
                "value_normalized": lid,
            }
        )
        alias_model.create(
            {
                "identity_id": second_identity.id,
                "account_id": self.account.id,
                "namespace": "whatsapp.pn",
                "value_raw": pn,
                "value_normalized": pn,
            }
        )
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "conflict-%s" % uuid.uuid4(),
                "event_type": "message.created",
                "occurred_at": "2026-08-21T13:00:00Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": "conflict-conversation-%s" % uuid.uuid4(),
                "platform": "whatsapp",
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {
                    "addresses": [
                        {
                            "namespace": "whatsapp.lid",
                            "value": lid,
                            "value_normalized": lid,
                        },
                        {
                            "namespace": "whatsapp.pn",
                            "value": pn,
                            "value_normalized": pn,
                        },
                    ]
                },
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [
                        {
                            "namespace": "whatsapp.pn",
                            "value": pn,
                            "value_normalized": pn,
                            "role": "primary",
                        }
                    ],
                },
                "message": {
                    "external_message_id": "conflicting-message",
                    "content_type": "text",
                    "text": "Must be blocked",
                },
            }
        )
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": event.event_id,
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": event.to_dict(),
                }
            )
        )
        self._process_inbox(inbox)
        self.assertEqual(inbox.state, "blocked")
        self.assertEqual(inbox.attempts, 1)
        self.assertEqual(inbox.normalized_dto_json, event.to_dict())
        conflict = (
            self.env["contact.center.identity.conflict"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)])
        )
        self.assertEqual(len(conflict), 1)
        self.assertEqual(
            set(conflict.identity_ids.ids), {first_identity.id, second_identity.id}
        )

    def test_invalid_normalized_scope_is_permanent_and_not_retried(self):
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "wrong-scope-%s" % uuid.uuid4(),
                "event_type": "message.created",
                "occurred_at": "2026-08-21T13:00:00Z",
                "account_ref": "wrong-account",
                "connection_ref": self.connection.external_ref,
                "conversation_ref": "wrong-scope-conversation",
                "platform": "whatsapp",
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {"addresses": []},
                "conversation": {"conversation_type": "direct", "addresses": []},
                "message": {"content_type": "text", "text": "Invalid"},
            }
        )
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": event.event_id,
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": event.to_dict(),
                }
            )
        )

        self._process_inbox(inbox)

        self.assertEqual(inbox.state, "dead")
        self.assertEqual(inbox.attempts, 1)
        self.assertEqual(inbox.last_error_class, "ValidationError")
        self.assertEqual(inbox.normalized_dto_json, event.to_dict())
        inbox_job_function = self.env.ref(
            "contact_center_base.queue_job_function_contact_center_inbox_process"
        )
        outbox_job_function = self.env.ref(
            "contact_center_base.queue_job_function_contact_center_outbox_process"
        )
        self.assertEqual(
            inbox_job_function.channel_id.complete_name,
            "root.contact_center.inbox",
        )
        self.assertEqual(
            outbox_job_function.channel_id.complete_name,
            "root.contact_center.outbox",
        )
        self.assertFalse(inbox_job_function.allow_commit)
        self.assertTrue(outbox_job_function.allow_commit)

    def test_channel_alias_conflict_carries_actionable_evidence(self):
        _first_channel, first_binding, first_identity = self._channel_binding()
        _second_channel, second_binding, second_identity = self._channel_binding()
        second_alias = second_binding.alias_ids[0]
        address = AddressDTO(
            namespace=second_alias.namespace,
            value=second_alias.value_raw,
            value_normalized=second_alias.value_normalized,
            role=second_alias.role,
        )
        with self.assertRaises(IdentityConflictError) as raised:
            self.env["contact.center.application"]._enrich_channel_aliases(
                first_binding, [address]
            )
        values = raised.exception.conflict_values
        identity_command = values["identity_ids"][0]
        self.assertEqual(
            set(identity_command[2]), {first_identity.id, second_identity.id}
        )
        evidence = values["address_evidence_json"][0]
        self.assertEqual(evidence["existing_channel_binding_id"], second_binding.id)
        self.assertEqual(evidence["candidate_channel_binding_id"], first_binding.id)

    def test_media_capability_contract_requires_structured_map(self):
        self.assertEqual(
            validate_provider_media_capability(
                {"media": {"image": {"enabled": True}}}, "image", "image/png", 4
            ),
            "image/png",
        )
        capabilities = {
            "media": {
                "image": {
                    "enabled": True,
                    "max_bytes": 4,
                    "mimetypes": ["image/png", "image/webp"],
                },
                "audio": {"enabled": False},
            }
        }
        self.assertEqual(enabled_media_kinds(capabilities), ("image",))
        self.assertEqual(enabled_media_kinds({"media": ["image"]}), ())
        with self.assertRaises(ValidationError):
            validate_provider_media_capability(
                {"media": ["image"]}, "image", "image/png", 4
            )
        self.assertEqual(
            validate_provider_media_capability(capabilities, "image", "image/png", 4),
            "image/png",
        )
        with self.assertRaises(UserError):
            validate_provider_media_capability(capabilities, "image", "image/jpeg", 4)
        with self.assertRaises(UserError):
            validate_provider_media_capability(capabilities, "image", "image/png", 5)
        with self.assertRaises(UserError):
            validate_provider_media_capability(capabilities, "audio", "audio/ogg", 4)
        with self.assertRaises(ValidationError):
            validate_provider_media_capability(
                {"media": {"image": {"enabled": "yes"}}},
                "image",
                "image/png",
                4,
            )
        with self.assertRaises(ValidationError):
            validate_provider_media_capability(
                {"media": {"image": {"enabled": True, "caption": "yes"}}},
                "image",
                "image/png",
                4,
            )
        with self.assertRaises(UserError):
            validate_provider_media_caption_capability(
                {"media": {"audio": {"enabled": True, "caption": False}}},
                "audio",
                True,
            )

    def test_voice_note_upload_metadata_reaches_the_outbound_dto(self):
        _channel, channel_binding, _identity = self._channel_binding()
        content = b"OggS" + (b"\x00" * 32)
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .with_context(image_no_postprocess=True)
            .create(
                {
                    "name": "voice-note.ogg",
                    "type": "binary",
                    "datas": base64.b64encode(content),
                    "mimetype": "audio/ogg",
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
                    "channel_binding_id": channel_binding.id,
                    "uploaded_by_user_id": self.agent.id,
                    "attachment_id": attachment.id,
                    "kind": "audio",
                    "mime_type": "audio/ogg",
                    "file_name": "voice-note.ogg",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "is_voice_note": True,
                    "duration_seconds": 12,
                }
            )
        )
        attachment.write({"res_id": upload.id})

        media = upload._as_outbound_media_dto(attachment)

        self.assertTrue(media.is_voice_note)
        self.assertEqual(media.duration_seconds, 12)
        self.assertEqual(media.kind, "audio")
        self.assertEqual(media.mime_type, "audio/ogg")

    def test_media_upload_cleanup_removes_only_pending_expired_attachment(self):
        channel, channel_binding, _identity = self._channel_binding()

        def create_pending_upload(name, content):
            attachment = (
                self.env["ir.attachment"]
                .sudo()
                .with_context(image_no_postprocess=True)
                .create(
                    {
                        "name": name,
                        "type": "binary",
                        "raw": content,
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
                        "channel_binding_id": channel_binding.id,
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
            return upload, attachment

        expired_pending, expired_pending_attachment = create_pending_upload(
            "expired-pending.png", b"expired-pending"
        )
        consumed, consumed_attachment = create_pending_upload(
            "expired-consumed.png", b"expired-consumed"
        )
        current_pending, current_pending_attachment = create_pending_upload(
            "current-pending.png", b"current-pending"
        )
        self.connection.capabilities_json = {
            "send_message": True,
            "media": {
                "image": {
                    "enabled": True,
                    "mimetypes": ["image/png"],
                }
            },
        }
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        result = api.send_message(
            channel.id,
            "",
            media_refs=[consumed.reference],
            client_request_id=str(uuid.uuid4()),
        )
        now = fields.Datetime.now()
        expired_at = now - datetime.timedelta(minutes=1)
        expired_pending.expires_at = expired_at
        consumed.expires_at = expired_at
        current_pending.expires_at = now + datetime.timedelta(minutes=1)

        self.env["contact.center.media.upload"]._cron_cleanup_expired()

        self.assertFalse(expired_pending.exists())
        self.assertFalse(consumed.exists())
        self.assertTrue(current_pending.exists())
        self.assertFalse(expired_pending_attachment.exists())
        self.assertTrue(consumed_attachment.exists())
        self.assertEqual(consumed_attachment.res_model, "mail.message")
        self.assertEqual(consumed_attachment.res_id, result["message_id"])
        self.assertTrue(current_pending_attachment.exists())

    def test_outbound_media_capability_is_rechecked_before_consumption(self):
        channel, channel_binding, _identity = self._channel_binding()
        content = b"phase-3-media"
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .with_context(image_no_postprocess=True)
            .create(
                {
                    "name": "evidence.png",
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
                    "channel_binding_id": channel_binding.id,
                    "uploaded_by_user_id": self.agent.id,
                    "attachment_id": attachment.id,
                    "kind": "image",
                    "mime_type": "image/png",
                    "file_name": "evidence.png",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        )
        attachment.write({"res_id": upload.id})
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        self.connection.capabilities_json = {
            "send_message": True,
            "media": {"image": {"enabled": False}},
        }
        with self.assertRaises(UserError):
            api.send_message(channel.id, "", media_refs=[upload.reference])
        self.assertEqual(upload.state, "pending")

        self.connection.capabilities_json = {
            "send_message": True,
            "media": {
                "image": {
                    "enabled": True,
                    "max_bytes": len(content),
                    "mimetypes": ["image/*"],
                }
            },
        }
        result = api.send_message(channel.id, "", media_refs=[upload.reference])
        upload.invalidate_recordset(["state", "consumed_message_binding_id"])
        self.assertEqual(upload.state, "consumed")
        self.assertEqual(
            upload.consumed_message_binding_id.message_id.id, result["message_id"]
        )
        self.assertEqual(attachment.res_model, "mail.message")
        self.assertEqual(attachment.res_id, result["message_id"])

    def test_ui_resend_clones_safe_media_without_reusing_the_consumed_upload(self):
        channel, channel_binding, _identity = self._channel_binding()
        content = b"safe-retry-image"
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .with_context(image_no_postprocess=True)
            .create(
                {
                    "name": "retry.png",
                    "type": "binary",
                    "raw": content,
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
                    "channel_binding_id": channel_binding.id,
                    "uploaded_by_user_id": self.agent.id,
                    "attachment_id": attachment.id,
                    "kind": "image",
                    "mime_type": "image/png",
                    "file_name": "retry.png",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        )
        attachment.write({"res_id": upload.id})
        self.connection.capabilities_json = {
            "send_message": True,
            "media": {
                "image": {
                    "enabled": True,
                    "max_bytes": len(content),
                    "mimetypes": ["image/png"],
                }
            },
        }
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )
        result = api.send_message(
            channel.id,
            "Preserved caption",
            media_refs=[upload.reference],
            client_request_id=str(uuid.uuid4()),
        )
        source = self._mark_send_dead(result)
        source_attachment = source.message_binding_id.media_ids.attachment_id

        retried = api.resend_message(
            channel.id, result["message_id"], str(uuid.uuid4())
        )
        retry = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(retried["outbox_command_id"])
        )
        retry_media = retry.message_binding_id.media_ids
        retry_command_media = retry.command_json["message"]["media"][0]

        upload.invalidate_recordset(["state", "consumed_message_binding_id"])
        self.assertEqual(upload.state, "consumed")
        self.assertEqual(upload.consumed_message_binding_id, source.message_binding_id)
        self.assertNotEqual(retry_media.attachment_id, source_attachment)
        self.assertEqual(retry_media.attachment_id.raw, content)
        self.assertEqual(retry_media.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(
            retry_command_media["remote_locator"],
            {"attachment_id": retry_media.attachment_id.id},
        )
        self.assertNotIn("upload_ref", retry_command_media["remote_locator"])
        self.assertEqual(retried["message"]["body_text"], "Preserved caption")

    def test_recorded_audio_duration_is_rechecked_before_consumption(self):
        channel, channel_binding, _identity = self._channel_binding()
        content = _test_recorded_ogg(12)
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .with_context(image_no_postprocess=True)
            .create(
                {
                    "name": "recording.ogg",
                    "type": "binary",
                    "datas": base64.b64encode(content),
                    "mimetype": "audio/ogg",
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
                    "channel_binding_id": channel_binding.id,
                    "uploaded_by_user_id": self.agent.id,
                    "attachment_id": attachment.id,
                    "kind": "audio",
                    "mime_type": "audio/ogg",
                    "file_name": "recording.ogg",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "is_voice_note": True,
                    "duration_seconds": 1,
                }
            )
        )
        attachment.write({"res_id": upload.id})
        self.connection.capabilities_json = {
            "send_message": True,
            "media": {
                "audio": {
                    "enabled": True,
                    "caption": False,
                    "max_bytes": len(content),
                    "mimetypes": ["audio/ogg"],
                    "recording_mimetypes": ["audio/ogg;codecs=opus"],
                    "voice_note_mimetypes": ["audio/ogg;codecs=opus"],
                    "max_duration_seconds": 900,
                }
            },
        }
        api = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
        )

        with self.assertRaises(ValidationError):
            api.send_message(channel.id, "", media_refs=[upload.reference])
        upload.invalidate_recordset(["state", "duration_seconds"])
        self.assertEqual(upload.state, "pending")

        upload.duration_seconds = 12
        result = api.send_message(channel.id, "", media_refs=[upload.reference])
        self.assertTrue(result["outbox_command_id"])
        upload.invalidate_recordset(["state"])
        self.assertEqual(upload.state, "consumed")
        media = (
            self.env["contact.center.outbox.command"]
            .browse(result["outbox_command_id"])
            .command_json["message"]["media"][0]
        )
        self.assertEqual(media["duration_seconds"], 12)

    def test_reply_action_requires_a_provider_confirmed_binding(self):
        channel, _channel_binding, _identity = self._channel_binding()
        result = self._send_message_without_enqueue(channel, "Awaiting provider ID")
        self.assertFalse(result["message"]["actions"]["reply"])
        binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", result["message_id"])], limit=1)
        )
        binding.write({"external_message_id": "confirmed-%s" % uuid.uuid4()})
        serialized = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._serialize_message(binding.message_id, binding)
        )
        self.assertTrue(serialized["actions"]["reply"])

    def test_direct_reply_dispatch_is_anchored_to_the_persisted_target(self):
        channel, _channel_binding, _identity, target = self._outbound_mutation_target()
        target.protocol_snapshot_json = {"participant": "observed-at-ingress"}
        self.connection.capabilities_json = {
            **(self.connection.capabilities_json or {}),
            "reply": True,
        }
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(
                channel.id,
                "Ledger-anchored reply",
                reply_to_message_id=target.message_id.id,
                client_request_id=str(uuid.uuid4()),
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        canonical = CommandDTO.from_dict(outbox.command_json)
        outbox._validate_command_scope(canonical)

        tampered = copy.deepcopy(outbox.command_json)
        forged_reference = {
            "external_message_id": "forged-external-message",
            "protocol_snapshot": {"participant": "forged"},
        }
        tampered["reply_to"] = forged_reference
        tampered["message"]["reply_to_external_id"] = forged_reference[
            "external_message_id"
        ]
        tampered["message"]["protocol_snapshot"] = forged_reference["protocol_snapshot"]

        with self.assertRaisesRegex(ValidationError, "does not match its outbox scope"):
            outbox._validate_command_scope(CommandDTO.from_dict(tampered))

    def test_mutation_keeps_an_empty_original_body_across_edit_and_delete(self):
        channel, _channel_binding, _identity = self._channel_binding()
        result = self._send_message_without_enqueue(channel, "Temporary body")
        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", result["message_id"])], limit=1)
        )
        target.write({"external_message_id": "target-%s" % uuid.uuid4()})
        target.message_id.sudo().with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).write({"body": False})
        mutation_model = self.env["contact.center.message.mutation"].sudo()
        edit = mutation_model.create(
            {
                "target_message_binding_id": target.id,
                "provider_connection_id": self.connection.id,
                "external_event_id": "edit-%s" % uuid.uuid4(),
                "mutation_type": "edit",
                "direction": "inbound",
                "actor_partner_id": self.agent.partner_id.id,
                "new_text": "Edited body",
            }
        )
        edit._apply_projection()
        self.assertEqual(target.message_state, "edited")
        self.assertFalse(target.original_body)

        delete = mutation_model.create(
            {
                "target_message_binding_id": target.id,
                "provider_connection_id": self.connection.id,
                "external_event_id": "delete-%s" % uuid.uuid4(),
                "mutation_type": "delete",
                "direction": "inbound",
                "actor_partner_id": self.agent.partner_id.id,
            }
        )
        delete._apply_projection()
        target.invalidate_recordset(["message_state", "original_body"])
        self.assertEqual(target.message_state, "deleted")
        self.assertFalse(target.original_body)

    def test_mutation_ledger_is_read_only_for_admin_and_never_unlinks(self):
        _channel, _binding, _identity, target = self._outbound_mutation_target()
        mutation = self._mutation(
            target,
            "edit",
            "2026-09-03 10:00:00",
            new_text="Immutable provider evidence",
        )

        with self.assertRaises(AccessError):
            mutation.with_user(self.admin).write({"new_text": "Rewritten evidence"})
        with self.assertRaisesRegex(AccessError, "cannot be deleted"):
            mutation.sudo().unlink()

        self.assertTrue(mutation.exists())
        self.assertEqual(mutation.new_text, "Immutable provider evidence")

    def test_deleted_message_content_policy_defaults_to_redaction(self):
        self.assertFalse(self.account.show_deleted_message_content)
        _channel, _channel_binding, _identity, target = self._outbound_mutation_target(
            "Default-redaction target"
        )

        deletion = self._mutation(target, "delete", "2026-08-23 18:03:00")

        self.assertEqual(deletion.deletion_display_mode, "redact")
        deletion._apply_projection()
        target.invalidate_recordset(
            ["message_state", "deleted_display_mode", "deleted_body"]
        )
        self.assertEqual(target.message_state, "deleted")
        self.assertEqual(target.deleted_display_mode, "redact")
        self.assertFalse(target.deleted_body)

    def test_redact_delete_hides_all_message_content_from_ui_dto(self):
        (
            channel,
            _channel_binding,
            _identity,
            reply_target,
        ) = self._outbound_mutation_target("Quoted sensitive body")
        self.connection.capabilities_json = {
            **(self.connection.capabilities_json or {}),
            "reply": True,
        }
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(
                channel.id,
                "Sensitive reply body",
                reply_to_message_id=reply_target.message_id.id,
                client_request_id=str(uuid.uuid4()),
            )
        )
        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", result["message_id"])], limit=1)
        )
        target.write(
            {
                "external_message_id": "redact-target-%s" % uuid.uuid4(),
                "is_forwarded": True,
            }
        )
        content = b"redacted-image"
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .with_context(image_no_postprocess=True)
            .create(
                {
                    "name": "redacted.png",
                    "type": "binary",
                    "raw": content,
                    "mimetype": "image/png",
                    "res_model": "mail.message",
                    "res_id": target.message_id.id,
                }
            )
        )
        target.message_id.sudo().with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).write({"attachment_ids": [(4, attachment.id)]})
        media = (
            self.env["contact.center.media.binding"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "message_binding_id": target.id,
                    "kind": "image",
                    "external_media_id": "redacted-media-%s" % uuid.uuid4(),
                    "remote_locator_json": {"path": "/private/redacted.png"},
                    "mime_type": "image/png",
                    "file_name": "redacted.png",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "state": "ready",
                    "attachment_id": attachment.id,
                }
            )
        )
        reaction = (
            self.env["mail.message.reaction"]
            .sudo()
            .create(
                {
                    "message_id": target.message_id.id,
                    "partner_id": self.agent.partner_id.id,
                    "content": "👍",
                }
            )
        )

        deletion = self._mutation(target, "delete", "2026-08-23 18:04:00")
        deletion._apply_projection()
        target.invalidate_recordset(["original_body", "media_ids"])
        media.invalidate_recordset(["state", "attachment_id"])
        self.assertFalse(target.original_body)
        self.assertFalse(attachment.exists())
        self.assertFalse(reaction.exists())
        self.assertEqual(media.state, "discarded")
        self.assertFalse(media.attachment_id)
        serialized = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._serialize_message(target.message_id, target)
        )

        self.assertTrue(serialized["is_deleted"])
        self.assertIs(serialized["deleted_content_visible"], False)
        self.assertEqual(serialized["body_text"], "")
        self.assertFalse(serialized["reply_to"])
        self.assertEqual(serialized["attachment_count"], 0)
        self.assertEqual(serialized["media"], [])
        self.assertEqual(serialized["reactions"], [])
        self.assertFalse(serialized["is_forwarded"])
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

    def test_strike_delete_snapshots_current_edited_body_independently_of_account(self):
        self.account.show_deleted_message_content = True
        _channel, _channel_binding, _identity, target = self._outbound_mutation_target(
            "Original body before edit"
        )
        edit = self._mutation(
            target,
            "edit",
            "2026-08-23 18:05:00",
            new_text="Current edited body",
        )
        edit._apply_projection()
        deletion = self._mutation(target, "delete", "2026-08-23 18:06:00")
        self.assertEqual(deletion.deletion_display_mode, "strike")

        self.account.show_deleted_message_content = False
        deletion._apply_projection()
        target.invalidate_recordset(
            ["message_state", "deleted_display_mode", "deleted_body"]
        )
        self.assertEqual(target.message_state, "deleted")
        self.assertEqual(target.deleted_display_mode, "strike")
        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(target.deleted_body),
            "Current edited body",
        )
        first_snapshot = str(target.deleted_body)
        serialized = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._serialize_message(target.message_id, target)
        )
        self.assertIs(serialized["deleted_content_visible"], True)
        self.assertEqual(serialized["body_text"], "Current edited body")
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

        deletion._apply_projection()
        target.invalidate_recordset(["deleted_display_mode", "deleted_body"])
        self.assertEqual(target.deleted_display_mode, "strike")
        self.assertEqual(str(target.deleted_body), first_snapshot)

    def test_edit_projection_is_monotonic_when_events_arrive_out_of_order(self):
        _channel, _channel_binding, _identity, target = self._outbound_mutation_target()
        newer = self._mutation(
            target,
            "edit",
            "2026-08-23 18:02:00",
            new_text="Newest body",
        )
        older = self._mutation(
            target,
            "edit",
            "2026-08-23 18:01:00",
            new_text="Stale body",
        )

        newer._apply_projection()
        older._apply_projection()

        target.invalidate_recordset(["message_state", "edited_at"])
        older.invalidate_recordset(["state", "details_json"])
        self.assertEqual(target.message_state, "edited")
        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(target.message_id.body),
            "Newest body",
        )
        self.assertEqual(older.state, "applied")
        self.assertEqual(older.details_json["projection"]["reason"], "superseded")
        self.assertEqual(older.details_json["projection"]["superseded_by_id"], newer.id)

    def test_edit_projection_uses_record_id_to_break_timestamp_ties(self):
        _channel, _channel_binding, _identity, target = self._outbound_mutation_target()
        first = self._mutation(
            target,
            "edit",
            "2026-08-23 18:02:00",
            new_text="First body in the same second",
        )
        second = self._mutation(
            target,
            "edit",
            "2026-08-23 18:02:00",
            new_text="Second body in the same second",
        )

        second._apply_projection()
        first._apply_projection()

        first.invalidate_recordset(["state", "details_json"])
        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(target.message_id.body),
            "Second body in the same second",
        )
        self.assertEqual(first.state, "applied")
        self.assertEqual(first.details_json["projection"]["reason"], "superseded")
        self.assertEqual(
            first.details_json["projection"]["superseded_by_id"], second.id
        )

    def test_edit_provider_revision_dominates_event_time_in_both_arrival_orders(self):
        def revisioned_edit(target, revision, occurred_at, body):
            return self._mutation(
                target,
                "edit",
                occurred_at,
                new_text=body,
                details_json={"provider_revision": revision},
            )

        _channel, _binding, _identity, first_target = self._outbound_mutation_target(
            "First revision target"
        )
        authoritative_first = revisioned_edit(
            first_target, 7, "2026-08-23 18:01:00", "Revision seven"
        )
        stale_second = revisioned_edit(
            first_target, 6, "2026-08-23 18:05:00", "Revision six"
        )

        authoritative_first._apply_projection()
        stale_second._apply_projection()

        stale_second.invalidate_recordset(["state", "details_json"])
        self.assertTrue(authoritative_first.has_provider_revision)
        self.assertEqual(authoritative_first.provider_revision, 7)
        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(first_target.message_id.body),
            "Revision seven",
        )
        self.assertEqual(
            stale_second.details_json["projection"]["reason"], "superseded"
        )
        self.assertEqual(
            stale_second.details_json["projection"]["superseded_by_id"],
            authoritative_first.id,
        )

        _channel, _binding, _identity, second_target = self._outbound_mutation_target(
            "Second revision target"
        )
        stale_first = revisioned_edit(
            second_target, 6, "2026-08-23 18:05:00", "Revision six"
        )
        authoritative_second = revisioned_edit(
            second_target, 7, "2026-08-23 18:01:00", "Revision seven"
        )

        stale_first._apply_projection()
        authoritative_second._apply_projection()

        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(second_target.message_id.body),
            "Revision seven",
        )

    def test_equal_edit_provider_revisions_keep_timestamp_and_id_ordering(self):
        _channel, _binding, _identity, target = self._outbound_mutation_target()
        first = self._mutation(
            target,
            "edit",
            "2026-08-23 18:02:00",
            new_text="First edit at revision nine",
            details_json={"provider_revision": 9},
        )
        second = self._mutation(
            target,
            "edit",
            "2026-08-23 18:02:00",
            new_text="Second edit at revision nine",
            details_json={"provider_revision": 9},
        )

        second._apply_projection()
        first._apply_projection()

        first.invalidate_recordset(["state", "details_json"])
        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(target.message_id.body),
            "Second edit at revision nine",
        )
        self.assertEqual(first.details_json["projection"]["reason"], "superseded")
        self.assertEqual(
            first.details_json["projection"]["superseded_by_id"], second.id
        )

    def test_mutation_model_rejects_invalid_provider_revision_evidence(self):
        _channel, _binding, _identity, target = self._outbound_mutation_target()
        with self.assertRaisesRegex(ValidationError, "bounded non-negative integer"):
            self._mutation(
                target,
                "edit",
                "2026-08-23 18:02:00",
                new_text="Invalid revision",
                details_json={"provider_revision": True},
            )

    def test_delete_is_terminal_and_later_edit_becomes_applied_noop(self):
        _channel, _channel_binding, _identity, target = self._outbound_mutation_target()
        delete = self._mutation(target, "delete", "2026-08-23 18:01:00")
        edit = self._mutation(
            target,
            "edit",
            "2026-08-23 18:02:00",
            new_text="Must not resurrect",
        )

        delete._apply_projection()
        edit._apply_projection()

        target.invalidate_recordset(["message_state", "deleted_at"])
        edit.invalidate_recordset(["state", "details_json"])
        self.assertEqual(target.message_state, "deleted")
        self.assertEqual(edit.state, "applied")
        self.assertEqual(edit.details_json["projection"]["reason"], "target_deleted")
        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(target.message_id.body),
            "Message deleted",
        )

    def test_delete_dominates_a_newer_edit_regardless_of_processing_order(self):
        _channel, _channel_binding, _identity, target = self._outbound_mutation_target()
        edit = self._mutation(
            target,
            "edit",
            "2026-08-23 18:02:00",
            new_text="Newer edit that is still revoked",
        )
        delete = self._mutation(target, "delete", "2026-08-23 18:01:00")

        edit._apply_projection()
        delete._apply_projection()

        target.invalidate_recordset(["message_state", "deleted_at"])
        self.assertEqual(target.message_state, "deleted")
        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(target.message_id.body),
            "Message deleted",
        )

    def test_delete_is_terminal_and_later_reaction_becomes_applied_noop(self):
        _channel, _binding, identity, target = self._outbound_mutation_target()
        deletion = self._mutation(target, "delete", "2026-08-23 18:01:00")
        reaction = self._mutation(
            target,
            "react",
            "2026-08-23 18:02:00",
            actor_partner_id=False,
            actor_guest_id=identity.mail_guest_id.id,
            reaction_emoji="👍",
            reaction_operation="add",
        )

        deletion._apply_projection()
        reaction._apply_projection()

        reaction.invalidate_recordset(["state", "details_json"])
        self.assertEqual(target.message_state, "deleted")
        self.assertFalse(target.message_id.sudo().reaction_ids)
        self.assertEqual(reaction.state, "applied")
        self.assertEqual(
            reaction.details_json["projection"]["reason"], "target_deleted"
        )

    def test_reaction_projection_is_monotonic_per_actor(self):
        _channel, _channel_binding, identity, target = self._outbound_mutation_target()
        other_guest = self.env["mail.guest"].sudo().create({"name": "Other actor"})
        newer = self._mutation(
            target,
            "react",
            "2026-08-23 18:02:00",
            actor_partner_id=False,
            actor_guest_id=identity.mail_guest_id.id,
            reaction_emoji="👍",
            reaction_operation="add",
        )
        older = self._mutation(
            target,
            "react",
            "2026-08-23 18:01:00",
            actor_partner_id=False,
            actor_guest_id=identity.mail_guest_id.id,
            reaction_emoji="❤️",
            reaction_operation="add",
        )
        independent = self._mutation(
            target,
            "react",
            "2026-08-23 18:00:00",
            actor_partner_id=False,
            actor_guest_id=other_guest.id,
            reaction_emoji="😂",
            reaction_operation="add",
        )

        newer._apply_projection()
        older._apply_projection()
        independent._apply_projection()

        older.invalidate_recordset(["state", "details_json"])
        reactions = target.message_id.sudo().reaction_ids
        self.assertEqual(set(reactions.mapped("content")), {"👍", "😂"})
        self.assertEqual(older.details_json["projection"]["reason"], "superseded")

    def test_reaction_projection_uses_record_id_to_break_timestamp_ties(self):
        _channel, _channel_binding, identity, target = self._outbound_mutation_target()
        common = {
            "actor_partner_id": False,
            "actor_guest_id": identity.mail_guest_id.id,
            "reaction_operation": "add",
        }
        first = self._mutation(
            target,
            "react",
            "2026-08-23 18:02:00",
            reaction_emoji="👍",
            **common,
        )
        second = self._mutation(
            target,
            "react",
            "2026-08-23 18:02:00",
            reaction_emoji="❤️",
            **common,
        )

        second._apply_projection()
        first._apply_projection()

        first.invalidate_recordset(["state", "details_json"])
        self.assertEqual(target.message_id.sudo().reaction_ids.content, "❤️")
        self.assertEqual(first.details_json["projection"]["reason"], "superseded")
        self.assertEqual(
            first.details_json["projection"]["superseded_by_id"], second.id
        )

    def test_self_reaction_echo_uses_account_actor_and_client_message_id(self):
        technical_author = self.env["res.partner"].create(
            {"name": "WhatsApp Test Number"}
        )
        self.account.technical_author_id = technical_author
        self.connection.capabilities_json = {
            "send_message": True,
            "react": True,
        }
        channel, channel_binding, identity = self._channel_binding()
        message = channel.sudo()._contact_center_post(
            origin="inbound",
            body="React to this inbound message",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_guest_id=identity.mail_guest_id.id,
            partner_ids=[],
        )
        target = (
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
                    "external_message_id": "external-%s" % uuid.uuid4(),
                    "client_message_id": "client-%s" % uuid.uuid4(),
                    "delivery_state": "delivered",
                }
            )
        )
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .react_message(
                channel.id,
                message.id,
                "👍",
                "add",
                str(uuid.uuid4()),
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        self.assertEqual(outbox.mutation_id.actor_user_id, self.agent)
        self.assertEqual(outbox.mutation_id.actor_partner_id, technical_author)
        outbox.mutation_id._apply_projection()

        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": self.connection.provider_schema_version,
                "event_id": "reaction-echo-%s" % uuid.uuid4(),
                "event_type": "message.reaction",
                "occurred_at": "2026-08-23T18:05:00Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": channel_binding.conversation_ref,
                "platform": self.account.platform,
                "direction": "outbound",
                "is_from_me": True,
                "origin": "provider",
                "actor": {"addresses": []},
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [],
                },
                "mutation": {
                    "type": "react",
                    "target_external_message_id": target.client_message_id,
                    "emoji": "👍",
                    "operation": "add",
                },
            }
        )
        self.env["contact.center.application"]._apply_inbound_mutation(
            self.connection, event
        )

        reactions = message.sudo().reaction_ids
        self.assertEqual(len(reactions), 1)
        self.assertEqual(reactions.partner_id, technical_author)
        serialized = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._serialize_message(message, target)
        )
        self.assertTrue(serialized["reactions"][0]["reacted_by_me"])

    def test_mutation_rejects_direction_inconsistent_with_from_me(self):
        _channel, channel_binding, _identity, target = self._outbound_mutation_target()
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": self.connection.provider_schema_version,
                "event_id": "invalid-direction-%s" % uuid.uuid4(),
                "event_type": "message.reaction",
                "occurred_at": "2026-08-23T18:05:00Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": channel_binding.conversation_ref,
                "platform": self.account.platform,
                "direction": "inbound",
                "is_from_me": True,
                "origin": "provider",
                "actor": {"addresses": []},
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [],
                },
                "mutation": {
                    "type": "react",
                    "target_external_message_id": target.external_message_id,
                    "emoji": "👍",
                    "operation": "add",
                },
            }
        )

        with self.assertRaisesRegex(
            ValidationError, "direction does not match its from_me"
        ):
            self.env["contact.center.application"]._apply_inbound_mutation(
                self.connection, event
            )

    def test_self_delete_without_technical_author_is_applied_and_replayable(self):
        _channel, channel_binding, _identity, target = self._outbound_mutation_target()
        self.account.technical_author_id = False
        conversation_addresses = [
            {
                "namespace": alias.namespace,
                "value": alias.value_raw,
                "value_normalized": alias.value_normalized,
                "role": alias.role,
            }
            for alias in channel_binding.alias_ids
        ]
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": self.connection.provider_schema_version,
                "event_id": "delete-echo-%s" % uuid.uuid4(),
                "event_type": "message.deleted",
                "occurred_at": "2026-08-23T18:05:00Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": channel_binding.conversation_ref,
                "platform": self.account.platform,
                "direction": "outbound",
                "is_from_me": True,
                "origin": "provider",
                "actor": {"addresses": []},
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": conversation_addresses,
                },
                "mutation": {
                    "type": "delete",
                    "target_external_message_id": target.external_message_id,
                    "operation": "delete",
                },
            }
        )

        result = self.env["contact.center.application"]._apply_inbound_mutation(
            self.connection, event
        )

        target.invalidate_recordset(["message_state"])
        mutation = target.mutation_ids.filtered(
            lambda item: item.external_event_id == event.event_id
        )
        self.assertEqual(result, target.message_id)
        self.assertEqual(target.message_state, "deleted")
        self.assertEqual(mutation.state, "applied")
        self.assertFalse(mutation.actor_partner_id)
        self.assertFalse(mutation.reaction_operation)
        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(target.message_id.body),
            "Message deleted",
        )

    def test_mutation_correlation_is_scoped_by_conversation(self):
        _channel_a, binding_a, _identity_a, target_a = self._outbound_mutation_target(
            "Conversation A"
        )
        _channel_b, _binding_b, _identity_b, target_b = self._outbound_mutation_target(
            "Conversation B"
        )
        shared_id = "shared-correlation-%s" % uuid.uuid4()
        target_a.external_message_id = shared_id
        target_b.client_message_id = shared_id
        conversation_addresses = [
            {
                "namespace": alias.namespace,
                "value": alias.value_raw,
                "value_normalized": alias.value_normalized,
                "role": alias.role,
            }
            for alias in binding_a.alias_ids
        ]
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": self.connection.provider_schema_version,
                "event_id": "scoped-edit-%s" % uuid.uuid4(),
                "event_type": "message.updated",
                "occurred_at": "2026-08-23T18:06:00Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": binding_a.conversation_ref,
                "platform": self.account.platform,
                "direction": "outbound",
                "is_from_me": True,
                "origin": "provider",
                "actor": {"addresses": []},
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": conversation_addresses,
                },
                "mutation": {
                    "type": "edit",
                    "target_external_message_id": shared_id,
                    "new_text": "Only conversation A",
                },
            }
        )

        self.env["contact.center.application"]._apply_inbound_mutation(
            self.connection, event
        )

        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(target_a.message_id.body),
            "Only conversation A",
        )
        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(target_b.message_id.body),
            "Conversation B",
        )

    def test_self_reaction_lane_survives_author_change_without_deleting_native_actor(
        self,
    ):
        technical_author = self.env["res.partner"].create(
            {"name": "First WhatsApp account actor"}
        )
        replacement_author = self.env["res.partner"].create(
            {"name": "Replacement WhatsApp account actor"}
        )
        native_actor = self.env["res.partner"].create(
            {"name": "Unrelated native Odoo actor"}
        )
        self.account.technical_author_id = technical_author
        _channel, _channel_binding, _identity, target = self._outbound_mutation_target()
        self.env["mail.message.reaction"].sudo().create(
            {
                "message_id": target.message_id.id,
                "partner_id": native_actor.id,
                "content": "😂",
            }
        )
        first = self._mutation(
            target,
            "react",
            "2026-08-23 18:01:00",
            direction="outbound",
            actor_partner_id=technical_author.id,
            actor_user_id=self.agent.id,
            reaction_emoji="👍",
            reaction_operation="add",
        )
        first._apply_projection()

        self.account.technical_author_id = replacement_author
        second = self._mutation(
            target,
            "react",
            "2026-08-23 18:02:00",
            direction="outbound",
            actor_partner_id=replacement_author.id,
            actor_user_id=self.agent.id,
            reaction_emoji="❤️",
            reaction_operation="add",
        )
        second._apply_projection()

        reactions = target.message_id.sudo().reaction_ids
        self.assertEqual(
            set(reactions.mapped("partner_id")), {replacement_author, native_actor}
        )
        self.assertEqual(set(reactions.mapped("content")), {"❤️", "😂"})

    def test_permanent_mutation_dispatch_marks_ledger_failed(self):
        channel, _channel_binding, _identity = self._channel_binding()
        target_result = self._send_message_without_enqueue(channel, "Original")
        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "=", target_result["message_id"])], limit=1)
        )
        target.write({"external_message_id": "target-%s" % uuid.uuid4()})
        self.connection.capabilities_json = {
            "send_message": True,
            "edit_message": True,
        }
        action_result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .edit_message(
                channel.id,
                target.message_id.id,
                "Rejected edit",
                str(uuid.uuid4()),
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(action_result["outbox_command_id"])
        )
        with mock.patch.object(
            FakeAdapter,
            "execute_command",
            return_value=AdapterResult(
                status="permanent", error_code="provider_rejected"
            ),
        ):
            self._process_outbox(outbox)
        outbox.invalidate_recordset(["state"])
        outbox.mutation_id.invalidate_recordset(["state"])
        self.assertEqual(outbox.state, "dead")
        self.assertEqual(outbox.mutation_id.state, "failed")
        self.assertEqual(
            outbox.mutation_id.details_json["dispatch"],
            {
                "state": "dead",
                "error_class": "AdapterError",
                "error_message": "provider_rejected",
            },
        )

    def _uncertain_send_for_resolution(self, body="Uncertain outbound message"):
        channel, _binding, _identity = self._channel_binding()
        result = self._send_message_without_enqueue(channel, body)
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        outbox.write(
            {
                "state": "uncertain",
                "last_error_class": "AmbiguousTimeoutError",
                "last_error_message": "provider outcome unknown",
            }
        )
        return outbox

    def _uncertain_edit_for_resolution(self, new_body="Externally applied edit"):
        channel, _channel_binding, _identity, target = self._outbound_mutation_target()
        self.connection.capabilities_json = {
            "send_message": True,
            "edit_message": True,
        }
        result = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .edit_message(
                channel.id,
                target.message_id.id,
                new_body,
                str(uuid.uuid4()),
            )
        )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse(result["outbox_command_id"])
        )
        outbox.write(
            {
                "state": "uncertain",
                "last_error_class": "AmbiguousTimeoutError",
                "last_error_message": "provider outcome unknown",
            }
        )
        return outbox, target

    def test_admin_confirms_uncertain_delivery_once_without_redispatch(self):
        outbox = self._uncertain_send_for_resolution()
        message = outbox.message_binding_id.message_id
        original_body = str(message.body)
        wizard_action = outbox.with_user(self.admin).action_confirm_external_delivery()
        self.assertEqual(
            wizard_action["context"]["default_requested_resolution"],
            "external_delivery_confirmed",
        )

        with mock.patch.object(
            type(outbox), "_enqueue", autospec=True
        ) as enqueue, mock.patch.object(
            FakeAdapter, "execute_command", autospec=True
        ) as execute_command:
            wizard = (
                self.env["contact.center.outbox.resolution.wizard"]
                .with_user(self.admin)
                .with_context(**wizard_action["context"])
                .create({"reason": "Confirmed in the provider message history."})
            )
            self.assertEqual(wizard.requested_resolution, "external_delivery_confirmed")
            self.assertEqual(
                wizard.action_confirm(), {"type": "ir.actions.act_window_close"}
            )
            enqueue.assert_not_called()
            execute_command.assert_not_called()

        outbox.invalidate_recordset()
        outbox.message_binding_id.invalidate_recordset(["delivery_state"])
        self.assertEqual(outbox.state, "done")
        self.assertEqual(outbox.resolution, "external_delivery_confirmed")
        self.assertEqual(
            outbox.resolution_reason,
            "Confirmed in the provider message history.",
        )
        self.assertEqual(outbox.resolved_by_id, self.admin)
        self.assertTrue(outbox.resolved_at)
        self.assertEqual(outbox.message_binding_id.delivery_state, "delivered")
        self.assertEqual(str(message.body), original_body)
        delivery_domain = [
            ("message_binding_id", "=", outbox.message_binding_id.id),
            ("external_event_id", "=", "admin-outbox-resolution:%s" % outbox.id),
        ]
        delivery_events = (
            self.env["contact.center.delivery.event"].sudo().search(delivery_domain)
        )
        self.assertEqual(len(delivery_events), 1)
        first_audit = (
            outbox.resolution_reason,
            outbox.resolved_at,
            outbox.resolved_by_id,
        )

        self.assertTrue(
            outbox.with_user(self.admin)._resolve_uncertain(
                "external_delivery_confirmed", "A repeated client request."
            )
        )
        outbox.invalidate_recordset()
        self.assertEqual(
            (
                outbox.resolution_reason,
                outbox.resolved_at,
                outbox.resolved_by_id,
            ),
            first_audit,
        )
        self.assertEqual(
            self.env["contact.center.delivery.event"]
            .sudo()
            .search_count(delivery_domain),
            1,
        )

    def test_admin_confirms_uncertain_mutation_and_records_projection_evidence(self):
        outbox, target = self._uncertain_edit_for_resolution()
        original_body = str(target.message_id.body)

        with mock.patch.object(
            FakeAdapter, "execute_command", autospec=True
        ) as execute_command:
            outbox.with_user(self.admin)._resolve_uncertain(
                "external_application_confirmed",
                "The edited text is visible in the external client.",
            )
            execute_command.assert_not_called()

        outbox.invalidate_recordset()
        outbox.mutation_id.invalidate_recordset(["state", "details_json"])
        target.invalidate_recordset(["message_state"])
        self.assertEqual(outbox.state, "done")
        self.assertEqual(outbox.resolution, "external_application_confirmed")
        self.assertEqual(outbox.mutation_id.state, "applied")
        self.assertEqual(target.message_state, "edited")
        self.assertNotEqual(str(target.message_id.body), original_body)
        self.assertEqual(
            self.env["contact.center.ui.api"]._body_text(target.message_id.body),
            "Externally applied edit",
        )
        self.assertEqual(
            outbox.mutation_id.details_json["dispatch"]["resolution"],
            "external_application_confirmed",
        )

    def test_admin_closes_uncertain_commands_without_projecting_messages(self):
        send_outbox = self._uncertain_send_for_resolution("Never sent")
        send_message = send_outbox.message_binding_id.message_id
        send_body = str(send_message.body)
        edit_outbox, target = self._uncertain_edit_for_resolution("Must not appear")
        target_body = str(target.message_id.body)
        self.assertNotIn(
            self.admin,
            self.team.agent_ids | self.team.supervisor_ids,
            "The administrative resolution must not depend on team membership.",
        )

        with mock.patch.object(
            FakeAdapter, "execute_command", autospec=True
        ) as execute_command:
            send_outbox.with_user(self.admin)._resolve_uncertain(
                "closed_without_send", "Provider audit proves no request was accepted."
            )
            edit_outbox.with_user(self.admin)._resolve_uncertain(
                "closed_without_send", "The external message was never edited."
            )
            execute_command.assert_not_called()

        send_outbox.invalidate_recordset()
        edit_outbox.invalidate_recordset()
        edit_outbox.mutation_id.invalidate_recordset(["state", "details_json"])
        self.assertEqual(send_outbox.state, "cancelled")
        self.assertEqual(send_outbox.resolution, "closed_without_send")
        self.assertEqual(send_outbox.message_binding_id.delivery_state, "queued")
        self.assertEqual(str(send_message.body), send_body)
        self.assertEqual(edit_outbox.state, "cancelled")
        self.assertEqual(edit_outbox.mutation_id.state, "failed")
        self.assertEqual(str(target.message_id.body), target_body)
        self.assertEqual(
            edit_outbox.mutation_id.details_json["dispatch"]["state"], "cancelled"
        )

    def test_uncertain_resolution_is_admin_only_company_scoped_and_immutable(self):
        outbox = self._uncertain_send_for_resolution()
        with self.assertRaises(AccessError):
            outbox.with_user(self.agent)._resolve_uncertain(
                "closed_without_send", "Unauthorized attempt"
            )
        with self.assertRaises(AccessError):
            self.env["contact.center.outbox.resolution.wizard"].with_user(
                self.agent
            ).create(
                {
                    "outbox_command_id": outbox.id,
                    "requested_resolution": "closed_without_send",
                    "reason": "Unauthorized attempt",
                }
            )
        with self.assertRaises(AccessError):
            outbox.sudo().write({"resolution_reason": "Direct tampering"})

        other_company = self.env["res.company"].sudo().create({"name": "Other CC"})
        self.admin.sudo().write({"company_ids": [(4, other_company.id)]})
        restricted = outbox.with_user(self.admin).with_context(
            allowed_company_ids=[other_company.id]
        )
        with self.assertRaises(AccessError):
            restricted._resolve_uncertain("closed_without_send", "Wrong active company")

        outbox.with_user(self.admin)._resolve_uncertain(
            "closed_without_send", "Correct company resolution"
        )
        with self.assertRaises(ValidationError):
            outbox.sudo().with_context(contact_center_uncertain_resolution=True).write(
                {"resolution_reason": "Changed after resolution"}
            )

    def test_uncertain_close_rejects_existing_positive_provider_evidence(self):
        outbox = self._uncertain_send_for_resolution()
        outbox.message_binding_id._contact_center_apply_delivery("sent")
        message_body = str(outbox.message_binding_id.message_id.body)

        with self.assertRaises(ValidationError):
            outbox.with_user(self.admin)._resolve_uncertain(
                "closed_without_send", "This claim conflicts with the ledger."
            )
        outbox.invalidate_recordset()
        self.assertEqual(outbox.state, "uncertain")
        self.assertFalse(outbox.resolution)
        self.assertEqual(str(outbox.message_binding_id.message_id.body), message_body)

    def test_uncertain_resolution_requires_reason_and_matching_command_type(self):
        send_outbox = self._uncertain_send_for_resolution()
        with self.assertRaises(ValidationError):
            send_outbox.with_user(self.admin)._resolve_uncertain(
                "external_delivery_confirmed", "   "
            )
        with self.assertRaises(ValidationError):
            send_outbox.with_user(self.admin)._resolve_uncertain(
                "external_application_confirmed", "Wrong resolution kind"
            )
