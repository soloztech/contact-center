import datetime
import uuid
from unittest import mock

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import SavepointCase

from ..models.application import IdentityConflictError
from ..models.conversation_actions import _policy_context
from ..services.adapter import (
    AdapterError,
    ProviderPausedError,
    ProviderRateLimitError,
    TransientAdapterError,
    adapter_registry,
)
from ..services.dto import AddressDTO, DirectAddressResult, EventDTO
from .test_contact_center import FakeAdapter


@adapter_registry.register("test.direct_start")
class DirectStartTestAdapter(FakeAdapter):
    display_name = "Direct Start Test"

    def supports_direct_conversation_start(self, connection):
        return True

    def resolve_direct_address(self, connection, normalized_phone):
        return direct_result(normalized_phone)


def direct_result(phone="5511998765432", lid=None, alternate=None):
    jid = phone + "@s.whatsapp.net"
    addresses = [
        AddressDTO(
            namespace="whatsapp.pn",
            value=jid,
            value_normalized=jid,
            confidence="protocol",
            resolution_scope="company",
        )
    ]
    if lid:
        addresses.append(
            AddressDTO(
                namespace="whatsapp.lid",
                value=lid,
                value_normalized=lid,
                role="alternate",
                confidence="protocol",
            )
        )
    if alternate:
        addresses.append(
            AddressDTO(
                namespace="whatsapp.pn",
                value=alternate + "@s.whatsapp.net",
                value_normalized=alternate + "@s.whatsapp.net",
                role="alternate",
                confidence="observed",
            )
        )
    return DirectAddressResult(
        state="ready", conversation_ref=jid, addresses=tuple(addresses)
    )


class TestContactCenterStartConversation(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.country_id = cls.env.ref("base.br")
        cls.agent = cls._user("agent", "group_contact_center_agent")
        cls.colleague = cls._user("colleague", "group_contact_center_agent")
        cls.supervisor = cls._user("supervisor", "group_contact_center_supervisor")
        cls.outsider = cls._user("outsider", "group_contact_center_agent")
        cls.admin = cls._user("admin", "group_contact_center_admin")
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Start inbox",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "start-%s" % uuid.uuid4(),
                "access_user_ids": [
                    (6, 0, (cls.agent | cls.colleague | cls.supervisor).ids)
                ],
                "auto_assignment_user_id": cls.colleague.id,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Start connection",
                "account_id": cls.account.id,
                "adapter_key": "test.direct_start",
                "external_ref": "start-provider-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {"send_message": True},
            }
        )

    @classmethod
    def _user(cls, name, group):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Start %s" % name,
                    "login": "start-%s-%s" % (name, uuid.uuid4()),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [
                        (6, 0, cls.env.ref("contact_center_base.%s" % group).ids)
                    ],
                }
            )
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

    def _api(self, user=None):
        return self.env["contact.center.ui.api"].with_user(user or self.agent)

    def _start(self, phone="(11) 99876-5432", user=None):
        return self._api(user).start_conversation(self.account.id, phone)

    def _footprint(self):
        return {
            model: self.env[model]
            .sudo()
            .with_context(active_test=False)
            .search_count([])
            for model in (
                "mail.channel",
                "mail.guest",
                "res.partner",
                "mail.message",
                "contact.center.identity",
                "contact.center.identity.alias",
                "contact.center.channel.binding",
                "contact.center.channel.alias",
                "contact.center.outbox.command",
                "contact.center.inbox.event",
                "contact.center.internal.note.request",
            )
        }

    def test_preview_and_bootstrap_do_not_lookup_or_write(self):
        before = self._footprint()
        with mock.patch.object(
            DirectStartTestAdapter, "resolve_direct_address"
        ) as lookup:
            bootstrap = self._api().bootstrap()
            preview = self._api().normalize_start_phone(
                self.account.id, "(55) 99999-1234"
            )
        lookup.assert_not_called()
        self.assertEqual(preview["normalized_phone"], "5555999991234")
        self.assertEqual(preview["formatted_phone"], "+5555999991234")
        self.assertEqual(before, self._footprint())
        item = next(
            row for row in bootstrap["accounts"] if row["id"] == self.account.id
        )
        self.assertTrue(item["can_start_conversation"])
        self.assertEqual(item["start_phone_country"]["code"], "BR")

    def test_creates_members_default_assignee_without_sending(self):
        before = self._footprint()
        revision = self.account.access_topology_revision
        with mock.patch.object(
            type(self.env["contact.center.application"]), "_notify_ui"
        ) as notify:
            result = self._start()
        self.assertTrue(result["created"])
        self.assertEqual(result["normalized_phone"], "5511998765432")
        self.assertTrue(result["item"]["can_send"])
        channel = self.env["mail.channel"].browse(result["channel_id"])
        self.assertEqual(channel.contact_center_responsible_id, self.colleague)
        self.assertEqual(channel.contact_center_state, "open")
        self.assertEqual(
            set(channel.channel_member_ids.partner_id.ids),
            set((self.agent | self.colleague | self.supervisor).partner_id.ids),
        )
        self.assertEqual(self.account.access_topology_revision, revision)
        notify.assert_any_call(channel, "conversation_updated", {})
        after = self._footprint()
        for model in (
            "res.partner",
            "mail.message",
            "contact.center.outbox.command",
            "contact.center.inbox.event",
            "contact.center.internal.note.request",
        ):
            self.assertEqual(before[model], after[model], model)
        if "contact.center.case" in self.env.registry:
            self.assertEqual(
                self.env["contact.center.case"].search_count(
                    [("channel_id", "=", channel.id)]
                ),
                1,
            )

    def test_repeated_formatted_input_reuses_one_channel(self):
        first = self._start()
        after_first = self._footprint()
        for phone in ("5511998765432", "+55 (11) 99876-5432", "005511998765432"):
            result = self._start(phone)
            self.assertFalse(result["created"])
            self.assertEqual(result["channel_id"], first["channel_id"])
        self.assertEqual(after_first, self._footprint())

    def test_registered_ninth_digit_variant_reuses_proven_identity(self):
        first = self._start()
        with mock.patch.object(
            DirectStartTestAdapter,
            "resolve_direct_address",
            return_value=direct_result(alternate="551198765432"),
        ):
            second = self._start("11 9876-5432")
        self.assertEqual(first["channel_id"], second["channel_id"])
        self.assertFalse(second["created"])

    def test_existing_state_assignee_tags_and_history_preserved(self):
        result = self._start()
        channel = self.env["mail.channel"].browse(result["channel_id"])
        tag = self.env["contact.center.tag"].create({"name": "Existing start tag"})
        for state in ("resolved", "archived"):
            self._api(self.supervisor).update_conversation(
                channel.id,
                {"state": state, "responsible_id": self.agent.id, "tag_ids": tag.ids},
            )
            before = self._footprint()
            again = self._start()
            self.assertFalse(again["created"])
            self.assertEqual(channel.contact_center_state, state)
            self.assertEqual(channel.contact_center_responsible_id, self.agent)
            self.assertEqual(channel.contact_center_tag_ids, tag)
            self.assertEqual(before, self._footprint())

    def test_non_agent_and_unassigned_admin_have_no_lookup(self):
        employee = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Non agent start",
                    "login": "non-agent-start-%s" % uuid.uuid4(),
                    "groups_id": [(6, 0, self.env.ref("base.group_user").ids)],
                }
            )
        )
        with mock.patch.object(
            DirectStartTestAdapter, "resolve_direct_address"
        ) as lookup:
            for user in (employee, self.outsider, self.admin):
                with self.assertRaises(AccessError):
                    self._start(user=user)
        lookup.assert_not_called()

    def test_bad_input_has_no_lookup(self):
        before = self._footprint()
        with mock.patch.object(
            DirectStartTestAdapter, "resolve_direct_address"
        ) as lookup:
            for phone in (
                "99876-5432",
                "not a phone",
                "33715510104278@lid",
                True,
                None,
            ):
                with self.assertRaises(ValidationError):
                    self._start(phone)
        lookup.assert_not_called()
        self.assertEqual(before, self._footprint())

    def test_inactive_account_has_no_lookup(self):
        self.connection.write(
            {
                "active": False,
                "role": "historical",
                "outbound_active": False,
                "inbound_active": False,
            }
        )
        self.account.active = False
        with mock.patch.object(
            DirectStartTestAdapter, "resolve_direct_address"
        ) as lookup, self.assertRaises(ValidationError):
            self._start()
        lookup.assert_not_called()

    def test_unavailable_connection_has_no_lookup(self):
        with mock.patch.object(
            DirectStartTestAdapter, "resolve_direct_address"
        ) as lookup:
            for values in (
                {"state": "disconnected"},
                {"state": "connected", "identity_mismatch_latched": True},
                {
                    "identity_mismatch_latched": False,
                    "last_state_observed_at": fields.Datetime.now()
                    - datetime.timedelta(days=1),
                },
            ):
                self.connection.write(values)
                with self.assertRaises(UserError):
                    self._start()
        lookup.assert_not_called()

    def test_unsupported_adapter_is_not_offered(self):
        with mock.patch.object(
            DirectStartTestAdapter,
            "supports_direct_conversation_start",
            return_value=False,
        ), mock.patch.object(
            DirectStartTestAdapter, "resolve_direct_address"
        ) as lookup:
            bootstrap = self._api().bootstrap()
            item = next(
                row for row in bootstrap["accounts"] if row["id"] == self.account.id
            )
            self.assertFalse(item["can_start_conversation"])
            with self.assertRaises(UserError):
                self._start()
        lookup.assert_not_called()

    def test_other_platform_never_instantiates_phone_adapter(self):
        self.account.platform = "telegram"
        with mock.patch.object(type(self.connection), "get_adapter") as get_adapter:
            bootstrap = self._api().bootstrap()
            item = next(
                row for row in bootstrap["accounts"] if row["id"] == self.account.id
            )
            self.assertFalse(item["can_start_conversation"])
            with self.assertRaises(UserError):
                self._start()
        get_adapter.assert_not_called()

    def test_unavailable_adapter_does_not_break_bootstrap(self):
        with mock.patch.object(
            type(self.connection),
            "get_adapter",
            side_effect=ValidationError("Unavailable provider fixture"),
        ):
            bootstrap = self._api().bootstrap()
            item = next(
                row for row in bootstrap["accounts"] if row["id"] == self.account.id
            )
            self.assertFalse(item["can_start_conversation"])
            with self.assertRaises(UserError):
                self._start()

    def test_provider_absence_or_errors_have_no_projection(self):
        before = self._footprint()
        with mock.patch.object(
            DirectStartTestAdapter,
            "resolve_direct_address",
            return_value=DirectAddressResult(state="not_registered"),
        ), self.assertRaises(UserError):
            self._start()
        for error_type in (
            AdapterError,
            TransientAdapterError,
            ProviderPausedError,
            ProviderRateLimitError,
        ):
            with mock.patch.object(
                DirectStartTestAdapter,
                "resolve_direct_address",
                side_effect=error_type("PRIVATE PROVIDER CONTENT"),
            ):
                with self.assertRaises(UserError) as raised:
                    self._start()
                self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertEqual(before, self._footprint())

    def test_provider_reconfiguration_during_lookup_aborts(self):
        before = self._footprint()

        def changed(_connection, phone):
            self.connection.write({"external_ref": "changed-start-provider"})
            return direct_result(phone)

        with mock.patch.object(
            DirectStartTestAdapter, "resolve_direct_address", side_effect=changed
        ), self.assertRaises(UserError):
            self._start()
        self.assertEqual(before, self._footprint())

    def test_access_revoked_during_lookup_aborts(self):
        before = self._footprint()

        def revoked(_connection, phone):
            self.account.write({"access_user_ids": [(3, self.agent.id)]})
            return direct_result(phone)

        with mock.patch.object(
            DirectStartTestAdapter, "resolve_direct_address", side_effect=revoked
        ), self.assertRaises(AccessError):
            self._start()
        self.assertEqual(before, self._footprint())

    def test_ignore_before_projection_and_late_conflict_roll_back(self):
        before = self._footprint()
        with mock.patch.object(
            type(self.env["contact.center.conversation.ignore"]),
            "_matches_route",
            return_value=True,
        ), self.assertRaises(UserError):
            self._start()
        self.assertEqual(before, self._footprint())
        with mock.patch.object(
            type(self.env["contact.center.application"]),
            "_enrich_channel_aliases",
            side_effect=IdentityConflictError("PRIVATE IDENTITY EVIDENCE"),
        ):
            with self.assertRaises(UserError) as raised:
                self._start()
            self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertEqual(before, self._footprint())

    def test_ignored_existing_conversation_is_not_reopened(self):
        result = self._start()
        channel = self.env["mail.channel"].browse(result["channel_id"])
        binding = channel.contact_center_binding_ids
        self.env["contact.center.conversation.ignore"].sudo().with_context(
            **_policy_context()
        ).create(
            {
                "name": "Ignored start fixture",
                "policy_key": "identity:%s" % binding.identity_id.id,
                "account_id": self.account.id,
                "conversation_type": "direct",
                "conversation_ref": binding.conversation_ref,
                "identity_id": binding.identity_id.id,
                "address_keys_json": [["whatsapp.pn", binding.conversation_ref]],
            }
        )
        before = self._footprint()
        with self.assertRaises(UserError):
            self._start()
        self.assertEqual(before, self._footprint())

    def test_late_policy_and_serialization_failure_roll_back_projection(self):
        before = self._footprint()
        with mock.patch.object(
            type(self.env["contact.center.conversation.ignore"]),
            "_for_binding",
            return_value=True,
        ), self.assertRaises(UserError):
            self._start()
        self.assertEqual(before, self._footprint())
        with mock.patch.object(
            type(self._api()),
            "_serialize_conversation",
            side_effect=UserError("Late serialization fixture"),
        ), self.assertRaises(UserError):
            self._start()
        self.assertEqual(before, self._footprint())

    def test_own_number_before_and_after_canonical_lookup(self):
        self.account.own_external_identity = "5511998765432@s.whatsapp.net"
        with mock.patch.object(
            DirectStartTestAdapter, "resolve_direct_address"
        ) as lookup, self.assertRaises(ValidationError):
            self._start()
        lookup.assert_not_called()
        with mock.patch.object(
            DirectStartTestAdapter,
            "resolve_direct_address",
            return_value=direct_result(alternate="551198765432"),
        ), self.assertRaises(ValidationError):
            self._start("11 9876-5432")

    def test_lid_reply_uses_prepared_identity_and_channel(self):
        lid = "123456789012345@lid"
        resolved = direct_result(lid=lid)
        with mock.patch.object(
            DirectStartTestAdapter, "resolve_direct_address", return_value=resolved
        ):
            started = self._start()
        # The provider sent only LID on a later real customer message; the
        # registration-time mapping is enough to correlate it without a merge.
        address = AddressDTO(
            namespace="whatsapp.lid",
            value=lid,
            value_normalized=lid,
            confidence="protocol",
        ).to_dict()
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "start-reply-%s" % uuid.uuid4(),
                "event_type": "message.created",
                "occurred_at": "2026-09-10T15:00:00Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": lid,
                "platform": "whatsapp",
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {"addresses": [address]},
                "conversation": {"conversation_type": "direct", "addresses": [address]},
                "message": {
                    "external_message_id": "start-reply-message-%s" % uuid.uuid4(),
                    "content_type": "text",
                    "text": "Reply fixture only",
                },
            }
        )
        message = self.env["contact.center.application"]._process_event(
            self.connection, event
        )
        self.assertEqual(message.res_id, started["channel_id"])

    def test_tag_catalog_menu_is_supervisor_only_and_reuses_action(self):
        menu = self.env.ref("contact_center_base.menu_contact_center_tags")
        self.assertEqual(
            menu.groups_id,
            self.env.ref("contact_center_base.group_contact_center_supervisor"),
        )
        self.assertEqual(
            menu.parent_id,
            self.env.ref("contact_center_base.menu_contact_center_operations"),
        )
        self.assertEqual(
            menu.action, self.env.ref("contact_center_base.action_contact_center_tags")
        )
        self.assertIn(
            menu.id,
            self.env["ir.ui.menu"].with_user(self.supervisor)._visible_menu_ids(),
        )
        self.assertNotIn(
            menu.id, self.env["ir.ui.menu"].with_user(self.agent)._visible_menu_ids()
        )
        with self.assertRaises(AccessError):
            self.env["contact.center.tag"].with_user(self.agent).create(
                {"name": "Agent cannot register"}
            )
        self.assertTrue(
            self.env["contact.center.tag"]
            .with_user(self.supervisor)
            .create({"name": "Supervisor reusable catalog"})
        )
