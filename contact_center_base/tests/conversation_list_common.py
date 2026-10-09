"""Synthetic, provider-free fixtures for list and authorized delta proofs."""

import datetime
import uuid

from odoo import fields

from ..services.adapter import ProviderAdapter, adapter_registry
from ..services.dto import EventDTO


@adapter_registry.register("test.conversation.list")
class ConversationListAdapter(ProviderAdapter):
    display_name = "Synthetic Conversation List"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return EventDTO.from_dict(envelope)

    def execute_command(self, connection, command):
        raise AssertionError("List tests must not invoke a provider")

    def prepare_request_snapshot(self, connection, command):
        raise AssertionError("List tests must not invoke a provider")

    def get_capabilities(self, connection):
        return {"send_message": True}

    def get_health(self, connection):
        return {"state": "connected"}


class ConversationListFixture:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Synthetic List Agent",
                    "login": "cc-list-%s" % uuid.uuid4(),
                    "company_id": cls.env.company.id,
                    "company_ids": [fields.Command.set(cls.env.company.ids)],
                    "groups_id": [fields.Command.set(group.ids)],
                }
            )
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Synthetic List Account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "list-%s" % uuid.uuid4(),
                "access_user_ids": [fields.Command.set(cls.agent.ids)],
                "conversation_delete_enabled": True,
                "conversation_ignore_enabled": True,
                "group_inbound_enabled": True,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Synthetic List Provider",
                "account_id": cls.account.id,
                "adapter_key": "test.conversation.list",
                "external_ref": "list-provider-%s" % uuid.uuid4(),
                "provider_schema_version": "list-fixture-v1",
                "state": "connected",
                "role": "primary",
                "active": True,
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {"send_message": True},
            }
        )

    def _api(self):
        return self.env["contact.center.ui.api"].with_user(self.agent)

    def _filters(self, **extra):
        return dict(extra, account_id=self.account.id)

    def _conversation(self, index, *, group=False, linked=False, name=None):
        identity = self.env["contact.center.identity"]
        if not group:
            guest = self.env["mail.guest"].create(
                {"name": name or "Synthetic Identity %s" % index}
            )
            values = {
                "name": guest.name,
                "company_id": self.env.company.id,
                "mail_guest_id": guest.id,
            }
            if linked:
                partner = self.env["res.partner"].create(
                    {
                        "name": "Linked Partner Name %s" % index,
                        "email": "synthetic-%s@example.invalid" % index,
                        "company_id": self.env.company.id,
                    }
                )
                values.update({"partner_id": partner.id, "partner_link_kind": "person"})
            identity = self.env["contact.center.identity"].create(values)
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            identity=identity,
            conversation_type="group" if group else "direct",
            name="Channel Name %s" % index,
        )
        binding = self.env["contact.center.channel.binding"].create(
            {
                "channel_id": channel.id,
                "account_id": self.account.id,
                "identity_id": identity.id,
                "conversation_type": "group" if group else "direct",
                "conversation_ref": "list-conversation-%s-%s" % (index, uuid.uuid4()),
            }
        )
        if group:
            self.env["contact.center.group.profile"]._get_or_create(
                binding, self.connection
            ).write(
                {
                    "name": "Group Profile Name %s" % index,
                    "last_synced_at": fields.Datetime.now(),
                    "roster_complete": True,
                    "participant_count": 3,
                    "admin_count": 1,
                    "own_role": "admin",
                    "metadata_state": "ready",
                }
            )
        message = channel.with_user(self.agent)._contact_center_post(
            origin="outbound",
            body="Synthetic list preview %s" % index,
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
            date=datetime.datetime(2026, 8, 1) + datetime.timedelta(minutes=index),
        )
        self.env["contact.center.application"]._publish_message_created(
            channel, message
        )
        return channel, binding, identity, message
