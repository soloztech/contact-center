import uuid

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from ..services.adapter import ProviderAdapter, adapter_registry
from ..services.dto import AdapterResult, EventDTO


@adapter_registry.register("test.source.webhook")
class SourceWebhookAdapter(ProviderAdapter):
    display_name = "Test Source Webhook"

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
            "payload": {"client_message_id": command.client_message_id},
        }

    def get_capabilities(self, connection):
        return {"send_message": True}

    def get_health(self, connection):
        return {"state": "connected"}

    def derive_client_message_id(self, command_id):
        return "source-webhook-%s" % command_id


class TestSourceWebhookLink(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        admin_group = cls.env.ref("contact_center_base.group_contact_center_admin")
        system_group = cls.env.ref("base.group_system")
        cls.agent = cls._create_user("Source Webhook Agent", [agent_group.id])
        cls.contact_center_admin = cls._create_user(
            "Source Webhook CC Admin", [admin_group.id]
        )
        cls.system_admin = cls._create_user(
            "Source Webhook System Admin", [system_group.id]
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Source Webhook Team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
                "supervisor_ids": [
                    (
                        6,
                        0,
                        (cls.contact_center_admin | cls.system_admin).ids,
                    )
                ],
            }
        )
        cls.technical_author = cls.env["res.partner"].create(
            {"name": "Source Webhook External Device"}
        )
        cls.account = cls._create_account("Primary")
        cls.connection = cls._create_connection(cls.account, "Primary")

    @classmethod
    def _create_user(cls, name, group_ids):
        key = uuid.uuid4()
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": name,
                    "login": "cc-source-webhook-%s" % key,
                    "email": "cc-source-webhook-%s@example.invalid" % key,
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, group_ids)],
                }
            )
        )

    @classmethod
    def _create_account(cls, suffix):
        return cls.env["contact.center.account"].create(
            {
                "name": "Source Webhook %s" % suffix,
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "source-webhook-account-%s-%s"
                % (suffix.lower(), uuid.uuid4()),
                "access_team_ids": [(6, 0, cls.team.ids)],
                "technical_author_id": cls.technical_author.id,
            }
        )

    @classmethod
    def _create_connection(cls, account, suffix, *, primary=True):
        return cls.env["contact.center.provider.connection"].create(
            {
                "name": "Source Webhook %s" % suffix,
                "account_id": account.id,
                "adapter_key": "test.source.webhook",
                "external_ref": "source-webhook-connection-%s-%s"
                % (suffix.lower(), uuid.uuid4()),
                "provider_schema_version": "source-webhook-v1",
                "state": "connected",
                "active": True,
                "role": "primary" if primary else "standby",
                "inbound_active": primary,
                "outbound_active": primary,
                "capabilities_json": {"send_message": True},
                "last_state_observed_at": fields.Datetime.now(),
                "last_state_source": "health_job",
                "health_detail": "healthy",
            }
        )

    def _channel_binding(self, suffix=None):
        suffix = suffix or uuid.uuid4().hex
        guest = self.env["mail.guest"].sudo().create({"name": "Remote Person"})
        identity = (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": "Remote Person",
                    "company_id": self.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            identity=identity,
            teams=self.team,
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
                    "conversation_ref": "source-webhook-conversation-%s" % suffix,
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
        return channel, binding

    def _inbox_event(self, event, connection=None):
        connection = connection or self.connection
        return (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": connection.id,
                    "inbox_dedupe_key": event.event_id,
                    "provider_schema_version": connection.provider_schema_version,
                    "raw_envelope_json": event.to_dict(),
                }
            )
        )

    def _empty_inbox_event(self, connection=None):
        connection = connection or self.connection
        return (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": connection.id,
                    "inbox_dedupe_key": "source-webhook-%s" % uuid.uuid4(),
                    "provider_schema_version": connection.provider_schema_version,
                    "raw_envelope_json": {"fixture": "source-webhook"},
                }
            )
        )

    def _inbound_event(self):
        key = uuid.uuid4().hex
        return EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": self.connection.provider_schema_version,
                "event_id": "source-webhook-inbound-%s" % key,
                "event_type": "message.created",
                "occurred_at": "2026-08-31T12:00:00Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": "source-webhook-direct-%s" % key,
                "platform": self.account.platform,
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {
                    "display_name": "Webhook Sender",
                    "addresses": [
                        {
                            "namespace": "whatsapp.lid",
                            "value": "%s@lid" % key,
                            "value_normalized": "%s@lid" % key,
                            "role": "primary",
                        }
                    ],
                },
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [
                        {
                            "namespace": "whatsapp.pn",
                            "value": "55%s@s.whatsapp.net" % key,
                            "value_normalized": "55%s@s.whatsapp.net" % key,
                            "role": "primary",
                        }
                    ],
                },
                "message": {
                    "external_message_id": "source-webhook-message-%s" % key,
                    "content_type": "text",
                    "text": "Inbound webhook evidence",
                },
            }
        )

    def _from_me_event(
        self, channel_binding, external_message_id, *, client_message_id=""
    ):
        alias = channel_binding.alias_ids[0]
        return EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": self.connection.provider_schema_version,
                "event_id": "source-webhook-from-me-%s" % uuid.uuid4(),
                "event_type": "message.created",
                "occurred_at": "2026-08-31T12:01:00Z",
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
                    "text": "Message sent outside Odoo",
                    "protocol_snapshot": {"from_me": True},
                },
            }
        )

    def _manual_binding(self, channel, channel_binding, source_event):
        message = channel.sudo()._contact_center_post(
            origin="inbound",
            body="Source webhook constraint fixture",
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
                    "source_inbox_event_id": source_event.id,
                    "direction": "inbound",
                    "origin": "provider",
                    "content_type": "text",
                    "external_message_id": "source-webhook-manual-%s" % uuid.uuid4(),
                    "delivery_state": "delivered",
                }
            )
        )

    def test_application_links_only_new_webhook_message_projections(self):
        application = self.env["contact.center.application"]

        inbound = self._inbound_event()
        inbound_source = self._inbox_event(inbound)
        inbound_message = application._process_event(
            self.connection, inbound, inbox_event=inbound_source
        )
        inbound_binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", inbound_message.id)], limit=1
        )
        self.assertEqual(inbound_binding.source_inbox_event_id, inbound_source)

        channel, channel_binding = self._channel_binding()
        sent = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .send_message(channel.id, "Composer message")
        )
        composer_binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", sent["message_id"])], limit=1
        )
        self.assertFalse(composer_binding.source_inbox_event_id)

        echo = self._from_me_event(
            channel_binding,
            composer_binding.client_message_id,
            client_message_id=composer_binding.client_message_id,
        )
        echo_source = self._inbox_event(echo)
        reconciled = application._process_event(
            self.connection, echo, inbox_event=echo_source
        )
        composer_binding.invalidate_recordset(["source_inbox_event_id"])
        self.assertEqual(reconciled, composer_binding.message_id)
        self.assertFalse(composer_binding.source_inbox_event_id)

        external_device = self._from_me_event(
            channel_binding, "source-webhook-device-%s" % uuid.uuid4()
        )
        external_source = self._inbox_event(external_device)
        external_message = application._process_event(
            self.connection, external_device, inbox_event=external_source
        )
        external_binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", external_message.id)], limit=1
        )
        self.assertEqual(external_binding.origin, "external_device")
        self.assertEqual(external_binding.source_inbox_event_id, external_source)

    def test_source_event_is_connection_scoped_and_write_once(self):
        channel, channel_binding = self._channel_binding()
        original_source = self._empty_inbox_event()
        binding = self._manual_binding(channel, channel_binding, original_source)

        replacement_source = self._empty_inbox_event()
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            binding.write({"source_inbox_event_id": replacement_source.id})
        binding.invalidate_recordset(["source_inbox_event_id"])
        self.assertEqual(binding.source_inbox_event_id, original_source)

        same_account_connection = self._create_connection(
            self.account, "Secondary", primary=False
        )
        wrong_connection_source = self._empty_inbox_event(same_account_connection)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._manual_binding(channel, channel_binding, wrong_connection_source)

        other_account = self._create_account("Other Account")
        other_connection = self._create_connection(other_account, "Other Account")
        wrong_account_source = self._empty_inbox_event(other_connection)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._manual_binding(channel, channel_binding, wrong_account_source)

    def test_ui_exposes_source_only_to_system_administrators(self):
        channel, channel_binding = self._channel_binding()
        source = self._empty_inbox_event()
        binding = self._manual_binding(channel, channel_binding, source)

        cc_admin_api = self.env["contact.center.ui.api"].with_user(
            self.contact_center_admin
        )
        cc_admin_message = (
            self.env["mail.message"]
            .with_user(self.contact_center_admin)
            .browse(binding.message_id.id)
        )
        cc_admin_binding = (
            self.env["contact.center.message.binding"]
            .with_user(self.contact_center_admin)
            .browse(binding.id)
        )
        serialized = cc_admin_api._serialize_message(cc_admin_message, cc_admin_binding)
        self.assertNotIn("source_inbox_event_id", serialized)
        self.assertFalse(
            cc_admin_api.bootstrap()["capabilities"]["view_source_webhook"]
        )

        system_api = self.env["contact.center.ui.api"].with_user(self.system_admin)
        system_message = (
            self.env["mail.message"]
            .with_user(self.system_admin)
            .browse(binding.message_id.id)
        )
        system_binding = (
            self.env["contact.center.message.binding"]
            .with_user(self.system_admin)
            .browse(binding.id)
        )
        serialized = system_api._serialize_message(system_message, system_binding)
        self.assertEqual(serialized["source_inbox_event_id"], source.id)
        self.assertTrue(system_api.bootstrap()["capabilities"]["view_source_webhook"])
