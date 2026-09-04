import uuid

from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from ..services.dto import EventDTO


class TestContactCenterAutoAssignment(SavepointCase):
    """Exercise inbox auto-assignment through the provider-neutral core path."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.primary_agent = cls._create_user("Primary", agent_group)
        cls.secondary_agent = cls._create_user("Secondary", agent_group)
        cls.outsider = cls._create_user("Outsider", agent_group)
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Auto assignment %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, (cls.primary_agent | cls.secondary_agent).ids)],
            }
        )
        # Deliberately use a non-WhatsApp platform and generic address namespace.
        # The policy belongs to the provider-neutral Contact Center core.
        cls.account = cls._create_account(team=cls.team)
        cls.connection = cls._create_connection(cls.account)

    @classmethod
    def _create_user(cls, label, group):
        token = uuid.uuid4().hex
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Auto Assignment %s" % label,
                    "login": "cc-auto-%s-%s" % (label.lower(), token),
                    "email": "cc-auto-%s-%s@example.invalid" % (label.lower(), token),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )

    @classmethod
    def _create_account(cls, *, team=None, owner=None, auto_user=None):
        token = uuid.uuid4().hex
        values = {
            "name": "Generic inbox %s" % token,
            "company_id": cls.env.company.id,
            "platform": "telegram",
            "external_ref": "auto-account-%s" % token,
        }
        if team:
            values["default_team_id"] = team.id
        if owner:
            values["owner_user_id"] = owner.id
        if auto_user:
            values["auto_assignment_user_id"] = auto_user.id
        return cls.env["contact.center.account"].create(values)

    @classmethod
    def _create_connection(cls, account):
        token = uuid.uuid4().hex
        return cls.env["contact.center.provider.connection"].create(
            {
                "name": "Generic provider %s" % token,
                "account_id": account.id,
                "adapter_key": "test.fake",
                "external_ref": "auto-connection-%s" % token,
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": False,
            }
        )

    def _event(self, connection, conversation_key, message_key=None):
        message_key = message_key or uuid.uuid4().hex
        address = "remote-%s" % conversation_key
        return EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "event-%s" % message_key,
                "event_type": "message.created",
                "occurred_at": "2026-09-01T12:00:00Z",
                "account_ref": connection.account_id.external_ref,
                "connection_ref": connection.external_ref,
                "conversation_ref": "conversation-%s" % conversation_key,
                "platform": connection.account_id.platform,
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {
                    "display_name": "Generic remote sender",
                    "addresses": [
                        {
                            "namespace": "telegram.user",
                            "value": address,
                            "value_normalized": address,
                            "role": "primary",
                        }
                    ],
                },
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [
                        {
                            "namespace": "telegram.user",
                            "value": address,
                            "value_normalized": address,
                            "role": "routing",
                        }
                    ],
                },
                "message": {
                    "external_message_id": "message-%s" % message_key,
                    "content_type": "text",
                    "text": "Provider-neutral inbound message",
                },
            }
        )

    def _process(self, connection, conversation_key, message_key=None):
        message = self.env["contact.center.application"]._process_event(
            connection,
            self._event(connection, conversation_key, message_key=message_key),
        )
        message_binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", message.id)], limit=1
        )
        self.assertTrue(message_binding)
        return message, message_binding.channel_binding_id.channel_id

    def test_new_provider_neutral_conversation_is_assigned(self):
        self.account.write({"auto_assignment_user_id": self.primary_agent.id})

        _message, channel = self._process(self.connection, uuid.uuid4().hex)

        self.assertEqual(channel.contact_center_responsible_id, self.primary_agent)
        default_case = channel.contact_center_case_ids.filtered("is_default")
        self.assertEqual(len(default_case), 1)
        self.assertEqual(default_case.responsible_user_id, self.primary_agent)

    def test_next_inbound_assigns_an_existing_unassigned_conversation(self):
        conversation_key = uuid.uuid4().hex
        first_message_key = uuid.uuid4().hex
        first_message, channel = self._process(
            self.connection, conversation_key, message_key=first_message_key
        )
        self.assertFalse(channel.contact_center_responsible_id)

        self.account.write({"auto_assignment_user_id": self.primary_agent.id})
        replayed_message, replayed_channel = self._process(
            self.connection, conversation_key, message_key=first_message_key
        )
        channel.invalidate_recordset(["contact_center_responsible_id"])
        self.assertEqual(first_message, replayed_message)
        self.assertEqual(channel, replayed_channel)
        self.assertFalse(channel.contact_center_responsible_id)

        self._process(self.connection, conversation_key, message_key=uuid.uuid4().hex)
        channel.invalidate_recordset(["contact_center_responsible_id"])

        self.assertEqual(channel.contact_center_responsible_id, self.primary_agent)

    def test_existing_assignment_is_never_overwritten(self):
        conversation_key = uuid.uuid4().hex
        _message, channel = self._process(self.connection, conversation_key)
        self.env["contact.center.ui.api"].with_user(
            self.secondary_agent
        ).claim_conversation(channel.id)
        self.account.write({"auto_assignment_user_id": self.primary_agent.id})

        self._process(self.connection, conversation_key)
        channel.invalidate_recordset(["contact_center_responsible_id"])

        self.assertEqual(channel.contact_center_responsible_id, self.secondary_agent)

    def test_user_outside_owner_team_union_is_rejected(self):
        self.assertEqual(
            set(self.account.auto_assignment_eligible_user_ids.ids),
            set((self.primary_agent | self.secondary_agent).ids),
        )
        self.assertNotIn(self.outsider, self.account.auto_assignment_eligible_user_ids)
        with self.assertRaises(ValidationError):
            self.account.write({"auto_assignment_user_id": self.outsider.id})
        self.account.invalidate_recordset(["auto_assignment_user_id"])
        self.assertFalse(self.account.auto_assignment_user_id)

    def test_owner_removal_clears_configuration_and_assignment(self):
        account = self._create_account(
            team=self.team,
            owner=self.outsider,
            auto_user=self.outsider,
        )
        connection = self._create_connection(account)
        _message, channel = self._process(connection, uuid.uuid4().hex)
        self.assertEqual(channel.contact_center_responsible_id, self.outsider)

        account.write({"owner_user_id": False})
        account.invalidate_recordset(["auto_assignment_user_id"])
        channel.invalidate_recordset(["contact_center_responsible_id"])

        self.assertFalse(account.auto_assignment_user_id)
        self.assertFalse(channel.contact_center_responsible_id)

    def test_roster_removal_clears_configuration_and_assignment(self):
        self.account.write({"auto_assignment_user_id": self.primary_agent.id})
        _message, channel = self._process(self.connection, uuid.uuid4().hex)

        self.team.write({"agent_ids": [(3, self.primary_agent.id)]})
        self.account.invalidate_recordset(["auto_assignment_user_id"])
        channel.invalidate_recordset(["contact_center_responsible_id"])

        self.assertFalse(self.account.auto_assignment_user_id)
        self.assertFalse(channel.contact_center_responsible_id)

    def test_team_removal_clears_configuration_and_assignment(self):
        account = self._create_account(
            team=self.team,
            owner=self.outsider,
            auto_user=self.primary_agent,
        )
        connection = self._create_connection(account)
        _message, channel = self._process(connection, uuid.uuid4().hex)

        account.write({"default_team_id": False})
        account.invalidate_recordset(["auto_assignment_user_id"])
        channel.invalidate_recordset(["contact_center_responsible_id"])

        self.assertFalse(account.auto_assignment_user_id)
        self.assertFalse(channel.contact_center_responsible_id)

    def test_configuration_survives_when_user_remains_in_union(self):
        account = self._create_account(
            team=self.team,
            owner=self.primary_agent,
            auto_user=self.primary_agent,
        )
        account.write({"owner_user_id": False})
        account.invalidate_recordset(["auto_assignment_user_id"])

        self.assertEqual(account.auto_assignment_user_id, self.primary_agent)

    def test_duplicate_event_keeps_one_message_and_one_assignment(self):
        self.account.write({"auto_assignment_user_id": self.primary_agent.id})
        conversation_key = uuid.uuid4().hex
        message_key = uuid.uuid4().hex

        first_message, channel = self._process(
            self.connection, conversation_key, message_key=message_key
        )
        second_message, repeated_channel = self._process(
            self.connection, conversation_key, message_key=message_key
        )

        self.assertEqual(first_message, second_message)
        self.assertEqual(channel, repeated_channel)
        self.assertEqual(channel.contact_center_responsible_id, self.primary_agent)
        self.assertEqual(
            self.env["contact.center.message.binding"].search_count(
                [
                    ("channel_binding_id.channel_id", "=", channel.id),
                    ("external_message_id", "=", "message-%s" % message_key),
                ]
            ),
            1,
        )

    def test_resolved_conversation_stays_resolved_by_default(self):
        conversation_key = uuid.uuid4().hex
        _message, channel = self._process(self.connection, conversation_key)
        self.env["contact.center.ui.api"].with_user(
            self.primary_agent
        ).update_conversation(channel.id, {"state": "resolved"})

        self._process(self.connection, conversation_key)
        channel.invalidate_recordset(["contact_center_state"])

        self.assertFalse(self.account.reopen_resolved_on_inbound)
        self.assertEqual(channel.contact_center_state, "resolved")

    def test_new_inbound_reopens_resolved_conversation_when_enabled(self):
        conversation_key = uuid.uuid4().hex
        self.account.write({"auto_assignment_user_id": self.primary_agent.id})
        _message, channel = self._process(self.connection, conversation_key)
        self.env["contact.center.ui.api"].with_user(
            self.primary_agent
        ).update_conversation(channel.id, {"state": "resolved"})
        self.account.write({"reopen_resolved_on_inbound": True})

        self._process(self.connection, conversation_key)
        channel.invalidate_recordset(["contact_center_state"])

        self.assertEqual(channel.contact_center_state, "open")
        self.assertEqual(channel.contact_center_responsible_id, self.primary_agent)

    def test_duplicate_inbound_does_not_reopen_resolved_conversation(self):
        conversation_key = uuid.uuid4().hex
        message_key = uuid.uuid4().hex
        first_message, channel = self._process(
            self.connection, conversation_key, message_key=message_key
        )
        self.env["contact.center.ui.api"].with_user(
            self.primary_agent
        ).update_conversation(channel.id, {"state": "resolved"})
        self.account.write({"reopen_resolved_on_inbound": True})

        replayed_message, replayed_channel = self._process(
            self.connection, conversation_key, message_key=message_key
        )
        channel.invalidate_recordset(["contact_center_state"])

        self.assertEqual(replayed_message, first_message)
        self.assertEqual(replayed_channel, channel)
        self.assertEqual(channel.contact_center_state, "resolved")
