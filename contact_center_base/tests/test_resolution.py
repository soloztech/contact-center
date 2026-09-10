import hashlib
import uuid
from unittest import mock

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import SavepointCase, TransactionCase
from odoo.tools import html2plaintext

from ..services.adapter import ProviderAdapter, adapter_registry
from ..services.dto import EventDTO


@adapter_registry.register("test.resolution")
class ResolutionTestAdapter(ProviderAdapter):
    display_name = "Resolution Test"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return EventDTO.from_dict(envelope)

    def prepare_request_snapshot(self, connection, command):
        raise AssertionError("Resolution must not prepare customer traffic")

    def execute_command(self, connection, command):
        raise AssertionError("Resolution must not contact a customer")

    def get_capabilities(self, connection):
        return {"send_message": True}

    def get_health(self, connection):
        return {"state": "connected"}


class TestContactCenterResolution(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = cls._user("Agent", "group_contact_center_agent")
        cls.colleague = cls._user("Colleague", "group_contact_center_agent")
        cls.supervisor = cls._user("Supervisor", "group_contact_center_supervisor")
        cls.outsider = cls._user("Outsider", "group_contact_center_agent")
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Resolution inbox",
                "company_id": cls.env.company.id,
                "platform": "telegram",
                "external_ref": "resolution-account-%s" % uuid.uuid4(),
                "access_user_ids": [
                    (6, 0, (cls.agent | cls.colleague | cls.supervisor).ids)
                ],
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Resolution provider",
                "account_id": cls.account.id,
                "adapter_key": "test.resolution",
                "external_ref": "resolution-provider-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": False,
            }
        )
        cls.reason = (
            cls.env["contact.center.resolution.reason"]
            .with_user(cls.supervisor)
            .create({"name": "Atendimento continuado em outra caixa"})
        )
        cls.application = cls.env["contact.center.application"]

    @classmethod
    def _user(cls, name, group):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Resolution %s" % name,
                    "login": "resolution-%s-%s" % (name, uuid.uuid4()),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [
                        (6, 0, cls.env.ref("contact_center_base.%s" % group).ids)
                    ],
                }
            )
        )

    def _event(self, remote=None, message_key=None):
        remote = remote or "resolution-remote-%s" % uuid.uuid4()
        message_key = message_key or str(uuid.uuid4())
        address = {
            "namespace": "telegram.user",
            "value": remote,
            "value_normalized": remote,
            "role": "primary",
        }
        return EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "event-%s" % message_key,
                "event_type": "message.created",
                "occurred_at": "2026-09-10T12:00:00Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": remote,
                "platform": "telegram",
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {"display_name": "Resolution guest", "addresses": [address]},
                "conversation": {"conversation_type": "direct", "addresses": [address]},
                "message": {
                    "external_message_id": "message-%s" % message_key,
                    "content_type": "text",
                    "text": "New customer message",
                },
            }
        )

    def _process(self, event):
        message = self.application._process_event(self.connection, event)
        return message, self.env["mail.channel"].browse(message.res_id)

    def _conversation(self):
        event = self._event()
        _message, channel = self._process(event)
        return channel, event

    def _api(self, user=None):
        return self.env["contact.center.ui.api"].with_user(user or self.agent)

    def _arguments(self, channel, **values):
        return {
            "channel_id": channel.id,
            "reason_id": self.reason.id,
            "justification": "O atendimento segue com a equipe responsável.",
            "client_request_id": str(uuid.uuid4()),
            "expected_revision": self._api().resolution_reason_catalog(channel.id)[
                "revision"
            ],
            **values,
        }

    def _notes(self, channel):
        return (
            self.env["contact.center.internal.note.request"]
            .sudo()
            .search([("channel_id", "=", channel.id)], order="id")
        )

    def _reopen_notes(self, channel):
        return self._notes(channel).filtered(
            lambda note: html2plaintext(note.message_id.body).strip()
            == "Conversation reopened by a new customer message."
        )

    def test_resolution_creates_one_internal_audit_note_and_no_customer_traffic(self):
        channel, _event = self._conversation()
        arguments = self._arguments(
            channel, justification="  Cliente encaminhado <sem enviar mensagem>.  "
        )
        outbox = self.env["contact.center.outbox.command"].sudo().search([]).ids
        emails = self.env["mail.mail"].sudo().search([]).ids
        last_message = channel.contact_center_last_message_id
        result = self._api().resolve_conversation(**arguments)
        self.assertEqual(result["item"]["state"], "resolved")
        self.assertFalse(result["replayed"])
        note = self._notes(channel)
        self.assertEqual(len(note), 1)
        self.assertEqual(note.message_id.author_id, self.agent.partner_id)
        self.assertEqual(note.requested_by_id, self.agent)
        self.assertEqual(note.message_id.subtype_id, self.env.ref("mail.mt_note"))
        self.assertFalse(note.message_id.partner_ids)
        self.assertFalse(
            self.env["contact.center.message.binding"].search_count(
                [("message_id", "=", note.message_id.id)]
            )
        )
        body = html2plaintext(note.message_id.body)
        self.assertIn(self.agent.name, body)
        self.assertIn(self.reason.name, body)
        self.assertIn("Cliente encaminhado <sem enviar mensagem>.", body)
        self.assertEqual(channel.contact_center_last_message_id, last_message)
        self.assertEqual(
            self.env["contact.center.outbox.command"].sudo().search([]).ids, outbox
        )
        self.assertEqual(self.env["mail.mail"].sudo().search([]).ids, emails)

    def test_retry_returns_current_state_without_resolving_again_after_reopen(self):
        channel, event = self._conversation()
        arguments = self._arguments(channel)
        self._api().resolve_conversation(**arguments)
        self.assertTrue(self._api().resolve_conversation(**arguments)["replayed"])
        self.assertEqual(len(self._notes(channel)), 1)
        self._process(self._event(event.conversation_ref))
        result = self._api().resolve_conversation(**arguments)
        self.assertTrue(result["replayed"])
        self.assertEqual(result["item"]["state"], "open")
        self.assertEqual(len(self._notes(channel)), 2)
        self.assertEqual(len(self._reopen_notes(channel)), 1)

    def test_replay_validates_actor_and_original_payload(self):
        channel, _event = self._conversation()
        arguments = self._arguments(channel)
        self._api().resolve_conversation(**arguments)
        with self.assertRaises(ValidationError):
            self._api(self.colleague).resolve_conversation(**arguments)
        with self.assertRaises(ValidationError):
            self._api().resolve_conversation(
                **{**arguments, "justification": "Other justification"}
            )
        self.assertEqual(len(self._notes(channel)), 1)

    def test_stale_dialog_cannot_resolve_a_new_customer_message(self):
        channel, event = self._conversation()
        arguments = self._arguments(channel)
        self._process(self._event(event.conversation_ref))
        with self.assertRaises(ValidationError):
            self._api().resolve_conversation(**arguments)
        self.assertEqual(channel.contact_center_state, "open")
        self.assertFalse(self._notes(channel))

    def test_two_submissions_from_the_same_snapshot_create_only_one_resolution(self):
        channel, _event = self._conversation()
        first = self._arguments(channel)
        second = {**first, "client_request_id": str(uuid.uuid4())}
        self._api().resolve_conversation(**first)
        with self.assertRaises(ValidationError):
            self._api(self.colleague).resolve_conversation(**second)
        self.assertEqual(len(self._notes(channel)), 1)

    def test_resolution_requires_active_same_company_reason_and_justification(self):
        channel, _event = self._conversation()
        other_company = self.env["res.company"].create(
            {"name": "Resolution other company"}
        )
        other = self.env["contact.center.resolution.reason"].create(
            {"name": "Other reason", "company_id": other_company.id}
        )
        inactive = self.env["contact.center.resolution.reason"].create(
            {"name": "Inactive reason", "active": False}
        )
        for values in (
            {"reason_id": False},
            {"reason_id": other.id},
            {"reason_id": inactive.id},
            {"justification": "   "},
            {"justification": "x" * 501},
            {"client_request_id": "not-a-uuid"},
            {"expected_revision": "invalid"},
        ):
            with self.assertRaises(ValidationError):
                self._api().resolve_conversation(**self._arguments(channel, **values))
        self.assertEqual(channel.contact_center_state, "open")
        self.assertFalse(self._notes(channel))

    def test_outsider_cannot_read_catalog_or_resolve(self):
        channel, _event = self._conversation()
        arguments = self._arguments(channel)
        with self.assertRaises(AccessError):
            self._api(self.outsider).resolution_reason_catalog(channel.id)
        with self.assertRaises(AccessError):
            self._api(self.outsider).resolve_conversation(**arguments)
        self.assertEqual(channel.contact_center_state, "open")

    def test_reason_catalog_and_management_respect_role_and_company(self):
        channel, _event = self._conversation()
        catalog = self._api().resolution_reason_catalog(channel.id)
        self.assertIn(self.reason.id, [item["id"] for item in catalog["items"]])
        self.assertFalse(catalog["can_manage"])
        self.assertTrue(
            self._api(self.supervisor).resolution_reason_catalog(channel.id)[
                "can_manage"
            ]
        )
        with self.assertRaises(AccessError):
            self.env["contact.center.resolution.reason"].with_user(self.agent).create(
                {"name": "Not allowed"}
            )
        with self.assertRaises(AccessError):
            self.reason.with_user(self.agent).write({"name": "Not allowed"})
        with self.assertRaises(AccessError):
            self.reason.with_user(self.agent).unlink()
        reason = (
            self.env["contact.center.resolution.reason"]
            .with_user(self.supervisor)
            .create({"name": "  Temporary reason  "})
        )
        self.assertEqual(reason.name, "Temporary reason")
        reason.write({"name": "Renamed reason", "active": False})
        self.assertNotIn(
            reason.id,
            [
                item["id"]
                for item in self._api().resolution_reason_catalog(channel.id)["items"]
            ],
        )
        reason.unlink()
        self.assertFalse(reason.exists())
        company = self.env["res.company"].create({"name": "Reason outside company"})
        other = self.env["contact.center.resolution.reason"].create(
            {"name": "Company scoped", "company_id": company.id}
        )
        self.assertNotIn(
            other.id,
            [
                item["id"]
                for item in self._api().resolution_reason_catalog(channel.id)["items"]
            ],
        )
        with self.assertRaises(AccessError):
            other.with_user(self.supervisor).write({"name": "Forbidden"})

    def test_resolution_note_failure_rolls_back_state_and_receipt(self):
        channel, _event = self._conversation()
        arguments = self._arguments(channel)
        api = self._api()
        with self.assertRaises(ValidationError), self.cr.savepoint(), mock.patch.object(
            type(api),
            "_persist_internal_note",
            side_effect=ValidationError("Injected note failure"),
        ):
            api.resolve_conversation(**arguments)
        channel.invalidate_recordset()
        self.assertEqual(channel.contact_center_state, "open")
        self.assertFalse(self._notes(channel))
        self.assertFalse(
            self.env["contact.center.resolution.request"].search_count(
                [("channel_id", "=", channel.id)]
            )
        )

    def test_new_customer_message_reopens_with_one_note_and_replay_does_not(self):
        channel, first = self._conversation()
        self._api().resolve_conversation(**self._arguments(channel))
        next_event = self._event(first.conversation_ref)
        message, _channel = self._process(next_event)
        self.assertEqual(channel.contact_center_state, "open")
        self.assertEqual(len(self._reopen_notes(channel)), 1)
        self.assertEqual(channel.contact_center_last_message_id, message)
        note = self._reopen_notes(channel).message_id
        self.assertEqual(note.subtype_id, self.env.ref("mail.mt_note"))
        self.assertFalse(note.partner_ids)
        self.assertFalse(
            self.env["contact.center.message.binding"].search_count(
                [("message_id", "=", note.id)]
            )
        )
        self._process(next_event)
        self.assertEqual(len(self._reopen_notes(channel)), 1)
        self._api().resolve_conversation(**self._arguments(channel))
        self._process(next_event)
        self.assertEqual(channel.contact_center_state, "resolved")
        self.assertEqual(len(self._reopen_notes(channel)), 1)

    def test_open_or_archived_conversation_does_not_get_an_automatic_reopen_note(self):
        channel, event = self._conversation()
        self._process(self._event(event.conversation_ref))
        self.assertFalse(self._reopen_notes(channel))
        self._api().update_conversation(channel.id, {"state": "archived"})
        self._process(self._event(event.conversation_ref))
        self.assertEqual(channel.contact_center_state, "archived")
        self.assertFalse(self._reopen_notes(channel))

    def test_control_event_does_not_reopen_or_create_a_reopen_note(self):
        channel, event = self._conversation()
        self._api().resolve_conversation(**self._arguments(channel))
        values = event.to_dict()
        values.update(
            {
                "event_type": "conversation.call.updated",
                "event_id": "call-%s" % uuid.uuid4(),
                "message": None,
                "extensions": {
                    "call": {
                        "state": "accepted",
                        "direction": "inbound",
                        "ref": hashlib.sha256(
                            event.conversation_ref.encode()
                        ).hexdigest(),
                    }
                },
            }
        )
        self.application._process_event(self.connection, EventDTO.from_dict(values))
        self.assertEqual(channel.contact_center_state, "resolved")
        self.assertFalse(self._reopen_notes(channel))

    def test_outbound_echo_and_read_receipt_do_not_reopen(self):
        channel, event = self._conversation()
        self.account.write({"technical_author_id": self.agent.partner_id.id})
        self._api().resolve_conversation(**self._arguments(channel))
        echo = self._event(event.conversation_ref).to_dict()
        echo.update(
            {
                "direction": "outbound",
                "is_from_me": True,
                "origin": "external_device",
                "actor": {"addresses": []},
            }
        )
        self.application._process_event(self.connection, EventDTO.from_dict(echo))
        self.assertEqual(channel.contact_center_state, "resolved")
        self.assertFalse(self._reopen_notes(channel))
        receipt = event.to_dict()
        receipt.update(
            {
                "event_id": "receipt-%s" % uuid.uuid4(),
                "event_type": "delivery.updated",
                "message": None,
                "direction": "outbound",
                "actor": {"addresses": []},
                "delivery": {
                    "state": "read",
                    "external_message_ids": [echo["message"]["external_message_id"]],
                },
            }
        )
        self.application._process_event(self.connection, EventDTO.from_dict(receipt))
        self.assertEqual(channel.contact_center_state, "resolved")
        self.assertFalse(self._reopen_notes(channel))

    def test_inbound_note_failure_rolls_back_reopening_and_new_message(self):
        channel, event = self._conversation()
        self._api().resolve_conversation(**self._arguments(channel))
        next_event = self._event(event.conversation_ref)
        with self.assertRaises(ValidationError), self.cr.savepoint(), mock.patch.object(
            type(self._api()),
            "_persist_internal_note",
            side_effect=ValidationError("Injected note failure"),
        ):
            self._process(next_event)
        channel.invalidate_recordset()
        self.assertEqual(channel.contact_center_state, "resolved")
        self.assertFalse(self._reopen_notes(channel))
        self.assertFalse(
            self.env["contact.center.message.binding"].search_count(
                [("external_message_id", "=", next_event.message.external_message_id)]
            )
        )

    def test_resolution_receipt_and_note_are_immutable(self):
        channel, _event = self._conversation()
        self._api().resolve_conversation(**self._arguments(channel))
        receipt = self.env["contact.center.resolution.request"].search(
            [("channel_id", "=", channel.id)]
        )
        self.assertEqual(len(receipt), 1)
        with self.assertRaises(AccessError):
            receipt.write({"reason_id": False})
        with self.assertRaises(AccessError):
            receipt.unlink()
        with self.assertRaises(AccessError):
            receipt.note_request_id.message_id.write({"body": "Changed audit"})

    def test_legacy_internal_state_update_remains_compatible(self):
        channel, _event = self._conversation()
        self._api().update_conversation(channel.id, {"state": "resolved"})
        self.assertEqual(channel.contact_center_state, "resolved")
        self.assertEqual(len(self._notes(channel)), 1)

    def test_reason_management_opens_separately_for_supervisors(self):
        action = self.env.ref(
            "contact_center_base.action_contact_center_resolution_reasons"
        )
        menu = self.env.ref(
            "contact_center_base.menu_contact_center_resolution_reasons"
        )
        self.assertEqual(action.target, "new")
        self.assertEqual(
            menu.parent_id, self.env.ref("contact_center_base.menu_contact_center_root")
        )
        self.assertIn(
            self.env.ref("contact_center_base.group_contact_center_supervisor"),
            menu.groups_id,
        )


@tagged("-at_install", "post_install")
class TestContactCenterSourceTranslations(TransactionCase):
    def test_portuguese_labels_and_validation_survive_english_source_messages(self):
        self.env["res.lang"]._activate_lang("pt_BR")
        self.env["ir.module.module"].search(
            [("name", "=", "contact_center_base")]
        )._update_translations(["pt_BR"], overwrite=True)
        reasons = self.env["contact.center.resolution.reason"]
        self.assertEqual(
            reasons.with_context(lang="en_US").fields_get(["name"])["name"]["string"],
            "Reason",
        )
        self.assertEqual(
            reasons.with_context(lang="pt_BR").fields_get(["name"])["name"]["string"],
            "Motivo",
        )
        for language, expected in (
            ("en_US", "Enter a reason of up to 120 characters."),
            ("pt_BR", "Informe um motivo de até 120 caracteres."),
        ):
            with self.assertRaisesRegex(ValidationError, expected), self.cr.savepoint():
                reasons.with_context(lang=language).create({"name": "x" * 121})
        action = self.env.ref(
            "contact_center_base.action_contact_center_resolution_reasons"
        )
        self.assertEqual(action.with_context(lang="pt_BR").name, "Motivos de resolução")
