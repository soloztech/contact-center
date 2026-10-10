import datetime
import uuid

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase
from odoo.tools import mute_logger

from . import test_conversation_privacy as privacy

_PARAMETER = "contact_center.payload_retention_days"
_EXPIRED = {"content_erased": True}
_EXPIRY_LOGGER = "odoo.addons.contact_center_base.models.payload_expiry"


class TestPayloadExpiry(SavepointCase):
    # Borrow the privacy fixtures and adapter: its normalizer raises if erased
    # content is ever replayed, and its route reader scopes conversation deletion.
    _user = privacy.TestConversationPrivacy.__dict__["_user"]
    _account = privacy.TestConversationPrivacy.__dict__["_account"]
    _conversation = privacy.TestConversationPrivacy.__dict__["_conversation"]
    _message = privacy.TestConversationPrivacy.__dict__["_message"]
    _api = privacy.TestConversationPrivacy.__dict__["_api"]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = cls._user("Agent", "contact_center_base.group_contact_center_agent")
        cls.admin = cls._user("Admin", "contact_center_base.group_contact_center_admin")
        cls.members = cls.agent | cls.admin
        cls.account = cls._account()
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Expiry provider",
                "account_id": cls.account.id,
                "adapter_key": "test.conversation.privacy",
                "external_ref": str(uuid.uuid4()),
                "state": "connected",
                "role": "primary",
                "active": True,
                "inbound_active": True,
                "outbound_active": False,
            }
        )
        cls.inbox = cls.env["contact.center.inbox.event"].with_context(
            contact_center_skip_enqueue=True
        )
        cls.parameters = cls.env["ir.config_parameter"].sudo()

    def _event(self, state="done", age_days=40, route=None, dedupe_key=None):
        return self.inbox.create(
            {
                "provider_connection_id": self.connection.id,
                "inbox_dedupe_key": dedupe_key or str(uuid.uuid4()),
                "provider_schema_version": "expiry-v1",
                "raw_envelope_json": {"route": route, "text": "Private payload"},
                "normalized_dto_json": {"text": "Private copy"},
                "metadata_json": {"event_type": "Message"},
                "state": state,
                "processed_at": fields.Datetime.now()
                - datetime.timedelta(days=age_days),
            }
        )

    def _assert_payload_kept(self, events):
        for event in events:
            self.assertEqual(event.raw_envelope_json["text"], "Private payload")
            self.assertEqual(event.normalized_dto_json, {"text": "Private copy"})
            self.assertEqual(event.metadata_json, {"event_type": "Message"})

    def test_disabled_or_too_short_window_keeps_every_payload(self):
        event = self._event()
        for value in (False, "0", "6", "not-a-number", "800000", "1000000000"):
            self.parameters.set_param(_PARAMETER, value)
            with mute_logger(_EXPIRY_LOGGER):
                self.assertEqual(self.inbox._cron_expire_payloads(), 0)
        self._assert_payload_kept(event)

    def test_expires_only_terminal_events_past_the_window(self):
        self.parameters.set_param(_PARAMETER, "30")
        expirable = self._event("done") | self._event("unsupported")
        kept = self._event("done", age_days=10)
        for state in ("pending", "processing", "retry", "blocked", "dead"):
            kept |= self._event(state)
        receipts = {
            event.id: (event.state, event.processed_at, event.inbox_dedupe_key)
            for event in expirable
        }
        self.assertEqual(self.inbox._cron_expire_payloads(), 2)
        for event in expirable:
            self.assertEqual(event.raw_envelope_json, _EXPIRED)
            self.assertFalse(event.normalized_dto_json)
            self.assertEqual(
                event.metadata_json,
                {
                    "event_type": "Message",
                    "content_erased": True,
                    "reason": "payload_expired",
                },
            )
            self.assertEqual(
                (event.state, event.processed_at, event.inbox_dedupe_key),
                receipts[event.id],
            )
        self._assert_payload_kept(kept)
        self.assertEqual(self.inbox._cron_expire_payloads(), 0)

    def test_batch_limit_takes_the_oldest_first(self):
        self.parameters.set_param(_PARAMETER, "30")
        newest, middle, oldest = (self._event(age_days=age) for age in (40, 50, 60))
        self.assertEqual(self.inbox._cron_expire_payloads(limit=2), 2)
        self._assert_payload_kept(newest)
        self.assertEqual(middle.raw_envelope_json, _EXPIRED)
        self.assertEqual(oldest.raw_envelope_json, _EXPIRED)
        self.assertEqual(self.inbox._cron_expire_payloads(limit=2), 1)

    def test_expired_receipt_still_dedupes_and_is_never_replayed(self):
        self.parameters.set_param(_PARAMETER, "30")
        done, unsupported = self._event("done"), self._event("unsupported")
        self.assertEqual(self.inbox._cron_expire_payloads(), 2)
        with self.assertRaises(IntegrityError), mute_logger(
            "odoo.sql_db"
        ), self.env.cr.savepoint():
            self._event(dedupe_key=done.inbox_dedupe_key)
        with self.assertRaises(ValidationError):
            unsupported.with_user(self.admin).action_requeue()
        self.assertTrue(done.with_user(self.admin).action_requeue())
        self.assertEqual((done.state, done.queue_job_uuid), ("done", False))
        self.assertEqual(done.raw_envelope_json, _EXPIRED)
        self.assertEqual(unsupported.state, "unsupported")

    def test_conversation_deletion_still_works_after_expiry(self):
        self.parameters.set_param(_PARAMETER, "30")
        channel, binding = self._conversation()
        route = {
            "conversation_type": binding.conversation_type,
            "conversation_ref": binding.conversation_ref,
            "addresses": [["whatsapp.pn", binding.conversation_ref]],
        }
        projected = self._event("done", route=route)
        unprojected = self._event("unsupported", route=route)
        message, _projection = self._message(channel, binding, projected)
        self.assertEqual(self.inbox._cron_expire_payloads(), 2)
        self._api().delete_conversation(channel.id)
        self.assertFalse(channel.exists())
        self.assertFalse(message.exists())
        self.assertEqual(projected.state, "blocked")
        self.assertEqual(
            projected.metadata_json,
            {"content_erased": True, "reason": "conversation_deleted"},
        )
        self.assertEqual(unprojected.state, "unsupported")
        self.assertEqual(unprojected.metadata_json["reason"], "payload_expired")

    def test_partial_index_backs_the_expiry_pass(self):
        self.env.cr.execute(
            "SELECT indexdef FROM pg_indexes"
            " WHERE indexname = 'cc_inbox_payload_expirable_idx'"
        )
        (definition,) = self.env.cr.fetchone()
        self.assertIn("processed_at", definition)
        self.assertIn("content_erased", definition)
