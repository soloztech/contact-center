import copy
import datetime
import json
from unittest import mock

from odoo.exceptions import AccessError

from odoo.addons.contact_center_base.models import retention_dependencies
from odoo.addons.contact_center_base.models.retention import _service_context

from ..services.adapter import WuzapiAdapter
from .common import WuzapiCase


class TestWuzapiRetentionIngress(WuzapiCase):
    def setUp(self):
        super().setUp()
        self.envelope = self.load_fixture("message_group_text_lid.json")
        self.adapter = WuzapiAdapter(self.env)
        self.service = self.env["contact.center.retention"]
        self.event = self.adapter.normalize_event(self.connection, self.envelope)
        self.binding = self.env["contact.center.application"]._resolve_group_channel(
            self.account, self.event
        )

    def _receipt(self, external_id=None):
        external_id = external_id or self.event.message.external_message_id
        self.account._lock_conversation_policy()
        self.service._record_expired_ids(self.binding, [external_id])
        return (
            self.env["contact.center.retention.receipt"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    ("conversation_ref", "=", self.binding.conversation_ref),
                    ("external_message_id", "=", external_id),
                ]
            )
        )

    def _inbox(self, envelope=None, key="retention-test"):
        return (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": key,
                    "provider_schema_version": "v1.0.8",
                    "raw_envelope_json": envelope or self.envelope,
                }
            )
        )

    def test_route_is_body_independent_and_preserves_original_utc_time(self):
        route = self.adapter.retention_route(self.connection, self.envelope)
        self.assertEqual(route["kind"], "message")
        self.assertEqual(route["occurred_at"], datetime.datetime(2026, 8, 24, 12, 1, 2))
        self.assertEqual(
            route["external_ids"], [self.event.message.external_message_id]
        )
        unsupported = copy.deepcopy(self.envelope)
        unsupported["event"]["Message"] = {
            "futureUnsupportedMessage": {"private": "payload"}
        }
        unsupported_route = self.adapter.retention_route(self.connection, unsupported)
        self.assertEqual(
            {
                key: value
                for key, value in unsupported_route.items()
                if key != "reply_ids"
            },
            {key: value for key, value in route.items() if key != "reply_ids"},
        )
        self.assertFalse(unsupported_route["reply_ids"])
        self.assertNotIn("payload", str(route))

    def test_index_receipt_and_cutoff_are_not_reset_when_policy_is_disabled(self):
        self.binding.with_context(**_service_context()).write(
            {
                "retention_expired_before": datetime.datetime(2026, 8, 25),
                "retention_preserve": True,
            }
        )
        self.assertFalse(self.account.retention_enabled)
        self.assertTrue(
            self.service._retention_envelope_is_expired(self.connection, self.envelope)
        )
        newer = copy.deepcopy(self.envelope)
        newer["event"]["Info"]["Timestamp"] = "2026-08-25T00:00:00Z"
        self.assertFalse(
            self.service._retention_envelope_is_expired(self.connection, newer)
        )
        self._receipt()
        self.assertTrue(
            self.service._retention_envelope_is_expired(self.connection, newer)
        )

    def test_expired_event_create_and_direct_projection_never_restore_content(self):
        self._receipt()
        inbox = self._inbox()
        self.assertEqual(inbox.raw_envelope_json, {"content_erased": True})
        self.assertFalse(inbox.normalized_dto_json)
        self.assertEqual(inbox.state, "blocked")
        self.assertEqual(inbox.retention_route_ref, self.binding.conversation_ref)
        self.assertEqual(
            inbox.retention_message_id, self.event.message.external_message_id
        )
        self.assertFalse(inbox._process_one())
        app = self.env["contact.center.application"]
        self.assertFalse(app._process_event(self.connection, self.event))
        self.assertFalse(
            self.env["contact.center.message.binding"].search(
                [("channel_binding_id", "=", self.binding.id)]
            )
        )

    def test_queued_message_is_rechecked_before_normalization(self):
        inbox = self._inbox()
        self._receipt()
        with mock.patch.object(type(inbox), "_normalize_one") as normalize:
            self.assertFalse(inbox._process_one())
        normalize.assert_not_called()
        self.assertEqual(inbox.raw_envelope_json, {"content_erased": True})

    def test_receipts_survive_connection_replacement_but_are_scoped_to_group(self):
        self._receipt()
        historical = self.env["contact.center.provider.connection"].create(
            {
                "name": "Replacement retention connection",
                "account_id": self.account.id,
                "adapter_key": "wuzapi",
                "role": "historical",
                "active": False,
                "inbound_active": False,
                "outbound_active": False,
                "wuzapi_base_url": "https://wuzapi.invalid/",
                "wuzapi_api_token": "test",
                "wuzapi_hmac_secret": "unit-test-hmac-secret-at-least-32-chars",
            }
        )
        self.assertTrue(
            self.service._retention_envelope_is_expired(historical, self.envelope)
        )
        other = copy.deepcopy(self.envelope)
        other["event"]["Info"]["Chat"] = "120363000000099@g.us"
        self.assertFalse(
            self.service._retention_envelope_is_expired(self.connection, other)
        )

    def test_recent_mutation_of_expired_target_is_not_persisted(self):
        self._receipt()
        mutation = copy.deepcopy(self.envelope)
        mutation["event"]["Info"]["ID"] = "NEW-REACTION-ID"
        mutation["event"]["Info"]["Timestamp"] = "2026-09-12T13:00:00Z"
        mutation["event"]["Message"] = {
            "reactionMessage": {
                "key": {"ID": self.event.message.external_message_id},
                "text": "👍",
            }
        }
        self.assertEqual(
            self.adapter.retention_route(self.connection, mutation)["kind"], "mutation"
        )
        self.assertTrue(
            self.service._retention_envelope_is_expired(self.connection, mutation)
        )
        self.assertEqual(
            self._inbox(mutation).raw_envelope_json, {"content_erased": True}
        )

    def test_mixed_receipt_keeps_evidence_for_surviving_messages(self):
        self._receipt()
        receipt = {
            "type": "ReadReceipt",
            "state": "read",
            "event": {
                "Chat": self.binding.conversation_ref,
                "IsFromMe": False,
                "Sender": "5511900000001@s.whatsapp.net",
                "IsGroup": True,
                "Timestamp": "2026-09-12T13:00:00Z",
                "MessageIDs": [
                    self.event.message.external_message_id,
                    "SURVIVING-MESSAGE",
                ],
            },
        }
        self.assertFalse(
            self.service._retention_envelope_is_expired(self.connection, receipt)
        )
        receipt["event"]["MessageIDs"].pop()
        self.assertTrue(
            self.service._retention_envelope_is_expired(self.connection, receipt)
        )

    def test_quote_of_expired_message_is_removed_without_changing_reply_text(self):
        quoted = copy.deepcopy(self.envelope)
        context = quoted["event"]["Message"]["extendedTextMessage"]["contextInfo"]
        target = context["stanzaID"]
        context["quotedMessage"] = {"conversation": "expired confidential quote"}
        self._receipt(target)
        original = json.dumps(quoted, sort_keys=True)
        inbox = self._inbox(quoted)
        self.assertNotIn(
            "expired confidential quote", json.dumps(inbox.raw_envelope_json)
        )
        self.assertEqual(
            inbox.raw_envelope_json["event"]["Message"]["extendedTextMessage"]["text"],
            quoted["event"]["Message"]["extendedTextMessage"]["text"],
        )
        self.assertEqual(json.dumps(quoted, sort_keys=True), original)
        self.assertEqual(inbox.retention_reply_id, target)

    def test_agents_cannot_forge_indexes_or_expiry_receipts(self):
        inbox = self._inbox()
        with self.assertRaises(AccessError):
            inbox.with_user(self.agent).write({"retention_route_ref": "another-group"})
        with self.assertRaises(AccessError):
            self.env["contact.center.retention.receipt"].sudo().create(
                {
                    "account_id": self.account.id,
                    "conversation_ref": self.binding.conversation_ref,
                    "external_message_id": "forged",
                }
            )

    def test_existing_quoted_payload_is_sanitized_by_partial_retention(self):
        quoted = copy.deepcopy(self.envelope)
        context = quoted["event"]["Message"]["extendedTextMessage"]["contextInfo"]
        target = context["stanzaID"]
        context["quotedMessage"] = {"conversation": "expired copied content"}
        surviving = self._inbox(quoted, key="surviving-reply")
        target_envelope = copy.deepcopy(self.envelope)
        target_envelope["event"]["Info"]["ID"] = target
        expired = self._inbox(target_envelope, key="expired-target")
        self.service.sudo()._retention_remove_copied_quotes(
            self.binding, self.env["contact.center.message.binding"], expired
        )
        self.assertNotIn(
            "expired copied content", json.dumps(surviving.raw_envelope_json)
        )
        self.assertIn("Mensagem sanitizada", json.dumps(surviving.raw_envelope_json))

    def test_quote_staging_advances_and_does_not_scan_unrelated_multi_quotes(self):
        quoted = copy.deepcopy(self.envelope)
        context = quoted["event"]["Message"]["extendedTextMessage"]["contextInfo"]
        target = context["stanzaID"]
        context["quotedMessage"] = {"conversation": "expired copied content"}
        events = self.env["contact.center.inbox.event"]
        for index in range(3):
            item = copy.deepcopy(quoted)
            item["event"]["Info"]["ID"] = "STAGING-REPLY-%s" % index
            events |= self._inbox(item, key="staging-reply-%s" % index)
        unrelated = copy.deepcopy(quoted)
        unrelated_context = unrelated["event"]["Message"]["extendedTextMessage"][
            "contextInfo"
        ]
        unrelated_context["stanzaID"] = "UNRELATED-ONE"
        unrelated_context["extra"] = {
            "stanzaId": "UNRELATED-TWO",
            "quotedMessage": {"conversation": "retained unrelated quote"},
        }
        unrelated_event = self._inbox(unrelated, key="unrelated-multi-quote")
        original_unrelated = copy.deepcopy(unrelated_event.raw_envelope_json)
        target_envelope = copy.deepcopy(self.envelope)
        target_envelope["event"]["Info"]["ID"] = target
        expired = self._inbox(target_envelope, key="staging-expired-target")
        projections = self.env["contact.center.message.binding"]
        with mock.patch.object(retention_dependencies, "_QUOTE_BATCH_SIZE", 2):
            self.assertFalse(
                self.service.sudo()._retention_remove_copied_quotes(
                    self.binding, projections, expired
                )
            )
            self.assertFalse(events[:2].filtered("retention_reply_id"))
            self.assertEqual(events[2].retention_reply_id, target)
            self.assertTrue(
                self.service.sudo()._retention_remove_copied_quotes(
                    self.binding, projections, expired
                )
            )
            self.assertFalse(
                self.service._retention_quote_events(self.binding, {target}, expired)
            )
        self.assertFalse(self.binding.retention_expired_before)
        self.assertTrue(expired.raw_envelope_json.get("event"))
        for event in events:
            self.assertNotIn(
                "expired copied content", json.dumps(event.raw_envelope_json)
            )
            self.assertIn("Mensagem sanitizada", json.dumps(event.raw_envelope_json))
        self.assertEqual(unrelated_event.raw_envelope_json, original_unrelated)

    def test_quote_progress_stops_before_receipts_or_business_detachment(self):
        with mock.patch.object(
            type(self.service), "_retention_remove_copied_quotes", return_value=False
        ), mock.patch.object(
            type(self.service), "_retention_prepare_business_attachments"
        ) as prepare, mock.patch.object(
            type(self.service), "_record_expired_ids"
        ) as record:
            self.assertFalse(
                self.service._retention_prepare_dependencies(
                    self.binding,
                    self.env["mail.message"],
                    self.env["contact.center.message.binding"],
                    self.env["contact.center.inbox.event"],
                )
            )
        prepare.assert_not_called()
        record.assert_not_called()

    def test_business_file_discovered_only_by_ownership_is_preserved(self):
        service = self.service.sudo().with_context(**_service_context())
        message = service.env["mail.message"].create(
            {
                "model": "mail.channel",
                "res_id": self.binding.channel_id.id,
                "message_type": "comment",
                "body": "Expired message",
            }
        )
        attachment = service.env["ir.attachment"].create(
            {
                "name": "shared-business-file.txt",
                "raw": b"shared business content",
                "res_model": "mail.message",
                "res_id": message.id,
            }
        )
        template = service.env["mail.template"].create(
            {
                "name": "Business file consumer",
                "model_id": self.env.ref("base.model_res_partner").id,
                "attachment_ids": [(6, 0, attachment.ids)],
            }
        )
        self.assertNotIn(attachment, message.attachment_ids)
        projections = service.env["contact.center.message.binding"]
        service._retention_prepare_business_attachments(message, projections)
        self.assertEqual(attachment.res_model, "mail.template")
        self.assertEqual(attachment.res_id, template.id)
        exclusive = service._prepare_attachments(
            message,
            service.env["contact.center.media.upload"],
            service.env["contact.center.media.binding"],
        )
        self.assertNotIn(attachment, exclusive)
        message.unlink()
        self.assertTrue(attachment.exists())
        self.assertIn(attachment, template.attachment_ids)
