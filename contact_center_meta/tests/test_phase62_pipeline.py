import copy
import uuid

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.messaging import atomic_events, sanitize_webhook_envelope
from .common import MetaCase


class TestMetaPhase62Pipeline(MetaCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.technical_author = cls.env["res.partner"].create(
            {"name": "Meta External Device"}
        )
        cls.account.technical_author_id = cls.technical_author

    def _events(self, fixture_name="messenger_phase62.json"):
        envelope = sanitize_webhook_envelope(self.load_fixture(fixture_name))
        return list(atomic_events(envelope))

    def _inbox(self, atomic, suffix=None):
        message = (atomic.get("messaging") or {}).get("message") or {}
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "meta-phase62:%s:%s"
                    % (message.get("mid") or "event", suffix or uuid.uuid4()),
                    "provider_schema_version": self.connection.provider_schema_version,
                    "raw_envelope_json": atomic,
                }
            )
        )
        # Suppress only the automatic job created by ``inbox.event.create``.
        # Manual processing below must be allowed to enqueue its own downstream
        # avatar/media work, just as the real inbox job does.
        return inbox.with_context(contact_center_skip_enqueue=False)

    def _process(self, atomic, suffix=None):
        inbox = self._inbox(atomic, suffix=suffix)
        result = self._run_inbox_job(inbox)
        inbox.invalidate_recordset()
        self.assertTrue(result)
        self.assertEqual(inbox.state, "done")
        message = (atomic.get("messaging") or {}).get("message") or {}
        if message.get("mid"):
            self.assertEqual(
                inbox.normalized_dto_json["message"]["external_message_id"],
                message["mid"],
            )
        else:
            self.assertEqual(
                inbox.normalized_dto_json["event_type"],
                "attribution.observed",
            )
            self.assertIsNone(inbox.normalized_dto_json["message"])
        return inbox

    def _run_inbox_job(self, inbox, job_uuid=None):
        job_uuid = job_uuid or inbox.queue_job_uuid or str(uuid.uuid4())
        if not inbox.queue_job_uuid:
            inbox.sudo().write({"queue_job_uuid": job_uuid})
        return inbox.with_context(job_uuid=job_uuid)._job_process()

    def _message_binding(self, external_message_id):
        return (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("provider_connection_id", "=", self.connection.id),
                    ("external_message_id", "=", external_message_id),
                ],
                limit=1,
            )
        )

    def test_inbound_pipeline_creates_guest_channel_message_and_attribution(self):
        inbound = self._events()[0]
        before = {
            model: self.env[model].sudo().search_count([])
            for model in (
                "mail.guest",
                "contact.center.identity",
                "contact.center.channel.binding",
                "contact.center.message.binding",
            )
        }

        with trap_jobs() as trap:
            inbox = self._process(inbound)
            contact_center_jobs = [
                job
                for job in trap.enqueued_jobs
                if job.channel.startswith("root.contact_center.")
            ]
            self.assertEqual(len(contact_center_jobs), 1)
            self.assertEqual(
                contact_center_jobs[0].channel,
                "root.contact_center.identity_avatar",
            )

        message_binding = self._message_binding("m_phase62_messenger_inbound")
        self.assertTrue(message_binding)
        channel_binding = message_binding.channel_binding_id
        identity = channel_binding.identity_id
        message = message_binding.message_id

        self.assertEqual(channel_binding.account_id, self.account)
        self.assertEqual(channel_binding.conversation_type, "direct")
        self.assertEqual(channel_binding.conversation_ref, "900000000000020")
        self.assertEqual(identity.mail_guest_id, message.author_guest_id)
        self.assertFalse(message.author_id)
        self.assertEqual(message_binding.direction, "inbound")
        self.assertEqual(message_binding.origin, "provider")
        self.assertEqual(message_binding.delivery_state, "delivered")
        self.assertEqual(
            {
                (alias.namespace, alias.value_normalized, alias.account_id.id)
                for alias in identity.alias_ids
            },
            {("meta.messenger.psid", "900000000000020", self.account.id)},
        )
        for model, count in before.items():
            self.assertEqual(self.env[model].sudo().search_count([]), count + 1, model)

        touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)], limit=1)
        )
        self.assertTrue(touchpoint)
        self.assertEqual(touchpoint.touchpoint_type, "paid_ad_click")
        self.assertEqual(touchpoint.network, "meta")
        self.assertEqual(touchpoint.source_platform, "messenger")
        self.assertEqual(touchpoint.message_binding_id, message_binding)
        self.assertEqual(touchpoint.channel_binding_id, channel_binding)
        self.assertEqual(touchpoint.identity_id, identity)
        self.assertTrue(
            self.connection.get_adapter().supports_identity_profile(self.connection)
        )
        self.assertEqual(channel_binding.direct_avatar_state, "pending")
        self.assertTrue(channel_binding.direct_avatar_queue_job_uuid)

    def test_invalid_referral_never_discards_the_customer_message(self):
        envelope = self.load_fixture("messenger_phase62.json")
        message = envelope["entry"][0]["messaging"][0]["message"]
        message["mid"] = "m_phase62_invalid_referral"
        message["referral"] = {
            "source": "   ",
            "ref": "campaign\tcontrol",
            "ad_id": "   ",
        }
        sanitized = sanitize_webhook_envelope(envelope)
        atomic = list(atomic_events(sanitized))[0]

        inbox = self._process(atomic, suffix="invalid-referral")

        binding = self._message_binding("m_phase62_invalid_referral")
        self.assertTrue(binding)
        self.assertIn("Synthetic Messenger inbound", str(binding.message_id.body))
        self.assertFalse(
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)])
        )

    def test_standalone_referral_creates_ledger_without_chat_projection(self):
        standalone = self._events("messenger_referral.json")[0]
        before = {
            model: self.env[model].sudo().search_count([])
            for model in (
                "mail.guest",
                "mail.channel",
                "mail.message",
                "contact.center.identity",
                "contact.center.channel.binding",
                "contact.center.message.binding",
            )
        }

        inbox = self._process(standalone, suffix="standalone-referral")

        for model, count in before.items():
            self.assertEqual(self.env[model].sudo().search_count([]), count, model)
        touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)], limit=1)
        )
        self.assertTrue(touchpoint)
        self.assertEqual(touchpoint.source_key_kind, "event")
        self.assertEqual(touchpoint.touchpoint_type, "paid_ad_click")
        self.assertEqual(touchpoint.source_platform, "messenger")
        self.assertFalse(touchpoint.message_binding_id)
        self.assertFalse(touchpoint.channel_binding_id)
        self.assertFalse(touchpoint.identity_id)

    def test_pending_standalone_referral_links_when_first_message_arrives(self):
        standalone = self._events("messenger_referral.json")[0]
        referral_inbox = self._process(standalone, suffix="pending-referral")
        touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", referral_inbox.id)], limit=1)
        )
        self.assertFalse(touchpoint.channel_binding_id)

        message = copy.deepcopy(self._events("messenger_phase62.json")[0])
        message["messaging"]["sender"]["id"] = "900000000000040"
        message["messaging"]["message"]["mid"] = "m_after_standalone_referral"
        message["messaging"]["message"].pop("referral")
        message_inbox = self._process(message, suffix="message-after-referral")
        message_binding = self._message_binding("m_after_standalone_referral")

        touchpoint.invalidate_recordset()
        self.assertEqual(
            touchpoint.channel_binding_id, message_binding.channel_binding_id
        )
        self.assertEqual(
            touchpoint.identity_id, message_binding.channel_binding_id.identity_id
        )
        self.assertFalse(touchpoint.message_binding_id)
        self.assertNotEqual(touchpoint.inbox_event_id, message_inbox)

    def test_instagram_standalone_referral_uses_its_own_account(self):
        account = self._create_account(
            "instagram",
            self.INSTAGRAM_ID,
            team=self.team,
        )
        connection = self._create_connection(
            account,
            self.instagram_asset,
        )
        standalone = self._events("instagram_referral.json")[0]
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": connection.id,
                    "inbox_dedupe_key": "meta-instagram-referral:%s" % uuid.uuid4(),
                    "provider_schema_version": connection.provider_schema_version,
                    "raw_envelope_json": standalone,
                }
            )
        )

        self.assertTrue(self._run_inbox_job(inbox))
        inbox.invalidate_recordset()
        self.assertEqual(inbox.state, "done")
        touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)], limit=1)
        )
        self.assertEqual(touchpoint.account_id, account)
        self.assertEqual(touchpoint.provider_connection_id, connection)
        self.assertEqual(touchpoint.source_platform, "instagram")
        self.assertEqual(touchpoint.source_type, "shortlink")
        self.assertFalse(touchpoint.message_binding_id)

    def test_repeated_inbound_and_external_echo_reuse_the_same_conversation(self):
        inbound, reply, echo = self._events()
        self._process(inbound)
        first = self._message_binding("m_phase62_messenger_inbound")
        identity = first.channel_binding_id.identity_id
        channel_binding = first.channel_binding_id
        guest = identity.mail_guest_id
        counts = {
            model: self.env[model].sudo().search_count([])
            for model in ("mail.guest", "contact.center.identity", "mail.channel")
        }

        self._process(reply)
        second = self._message_binding("m_phase62_messenger_reply")
        self.assertEqual(second.channel_binding_id, channel_binding)
        self.assertEqual(second.message_id.author_guest_id, guest)
        self.assertEqual(second.reply_to_binding_id, first)
        self.assertEqual(second.message_id.parent_id, first.message_id)

        outbox_count = self.env["contact.center.outbox.command"].sudo().search_count([])
        self._process(echo)
        echoed = self._message_binding("m_phase62_messenger_echo")
        self.assertEqual(echoed.channel_binding_id, channel_binding)
        self.assertEqual(echoed.direction, "outbound")
        self.assertEqual(echoed.origin, "external_device")
        self.assertEqual(echoed.delivery_state, "sent")
        self.assertEqual(echoed.message_id.author_id, self.technical_author)
        self.assertFalse(echoed.message_id.author_guest_id)
        self.assertEqual(
            self.env["contact.center.outbox.command"].sudo().search_count([]),
            outbox_count,
        )
        for model, count in counts.items():
            self.assertEqual(self.env[model].sudo().search_count([]), count, model)

    def test_reply_before_target_retries_then_projects_after_target_arrives(self):
        target, reply, _echo = self._events()
        reply_inbox = self._inbox(reply, suffix="out-of-order")

        with self.assertRaises(RetryableJobError) as raised:
            self._run_inbox_job(reply_inbox)
        self.assertFalse(raised.exception.ignore_retry)
        self.assertIsNone(raised.exception.seconds)
        self.assertFalse(self._message_binding("m_phase62_messenger_reply"))

        self._process(target, suffix="target-after-reply")
        target_binding = self._message_binding("m_phase62_messenger_inbound")
        self.assertTrue(target_binding)

        self.assertTrue(self._run_inbox_job(reply_inbox))
        reply_inbox.invalidate_recordset()
        self.assertEqual(reply_inbox.state, "done")
        reply_binding = self._message_binding("m_phase62_messenger_reply")
        self.assertEqual(reply_binding.reply_to_binding_id, target_binding)
        self.assertEqual(reply_binding.message_id.parent_id, target_binding.message_id)

    def test_same_opaque_remote_id_on_two_pages_remains_two_identities(self):
        first_atomic = self._events()[0]
        first_event = self.connection.get_adapter().normalize_event(
            self.connection, first_atomic
        )
        first_message = self.env["contact.center.application"]._process_event(
            self.connection, first_event
        )

        second_page_id = "100000000000077"
        page = self._create_page(self.endpoint, second_page_id)
        asset = page.asset_ids.ensure_one()
        account = self._create_account("messenger", second_page_id, team=self.team)
        connection = self._create_connection(account, asset)
        second_atomic = copy.deepcopy(first_atomic)
        second_atomic["entry"]["id"] = second_page_id
        second_atomic["messaging"]["recipient"]["id"] = second_page_id
        second_atomic["messaging"]["message"]["mid"] = "m_phase62_second_page"
        second_atomic["messaging"]["message"].pop("referral")
        second_event = connection.get_adapter().normalize_event(
            connection, second_atomic
        )
        second_message = self.env["contact.center.application"]._process_event(
            connection, second_event
        )

        first_binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", first_message.id)], limit=1
        )
        second_binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", second_message.id)], limit=1
        )
        self.assertNotEqual(
            first_binding.channel_binding_id, second_binding.channel_binding_id
        )
        self.assertNotEqual(
            first_binding.channel_binding_id.identity_id,
            second_binding.channel_binding_id.identity_id,
        )
        self.assertNotEqual(
            first_binding.channel_binding_id.identity_id.mail_guest_id,
            second_binding.channel_binding_id.identity_id.mail_guest_id,
        )
        self.assertEqual(
            first_event.conversation.addresses[0].value_normalized,
            second_event.conversation.addresses[0].value_normalized,
        )

    def test_unknown_echo_without_technical_author_is_retained_as_unsupported(self):
        echo = self._events()[2]
        self.account.technical_author_id = False
        before = {
            model: self.env[model].sudo().search_count([])
            for model in (
                "mail.guest",
                "mail.channel",
                "mail.message",
                "contact.center.identity",
                "contact.center.channel.binding",
                "contact.center.message.binding",
            )
        }
        inbox = self._inbox(echo, suffix="missing-technical-author")

        with trap_jobs() as trap:
            self.assertTrue(self._run_inbox_job(inbox))
            trap.assert_jobs_count(0)

        inbox.invalidate_recordset()
        self.assertEqual(inbox.state, "unsupported")
        self.assertEqual(inbox.last_error_class, "UnsupportedEventError")
        for model, count in before.items():
            self.assertEqual(self.env[model].sudo().search_count([]), count, model)

    def test_quarantined_empty_attachments_are_unsupported_not_dead(self):
        envelope = self.load_fixture("messenger_phase62.json")
        message = envelope["entry"][0]["messaging"][0]["message"]
        message.pop("text")
        message.pop("referral")
        message["attachments"] = ["invalid-attachment-shape"]
        atomic = list(atomic_events(sanitize_webhook_envelope(envelope)))[0]
        sanitized_message = atomic["messaging"]["message"]
        self.assertEqual(sanitized_message["attachments"], [])
        self.assertEqual(
            sanitized_message["sanitization_rejections"],
            [{"slot": "attachment:0", "reason": "invalid_attachment"}],
        )
        inbox = self._inbox(atomic, suffix="quarantined-empty-attachments")

        with trap_jobs() as trap:
            self.assertTrue(self._run_inbox_job(inbox))
            trap.assert_jobs_count(0)

        inbox.invalidate_recordset()
        self.assertEqual(inbox.state, "unsupported")
        self.assertEqual(inbox.attempts, 1)
        self.assertEqual(inbox.last_error_class, "UnsupportedEventError")
        self.assertFalse(self._message_binding(message["mid"]))

    def test_unrelated_rejection_does_not_quarantine_empty_attachments(self):
        envelope = self.load_fixture("messenger_phase62.json")
        message = envelope["entry"][0]["messaging"][0]["message"]
        message.pop("text")
        message.pop("referral")
        message["attachments"] = []
        message["quick_reply"] = "invalid-quick-reply-shape"
        atomic = list(atomic_events(sanitize_webhook_envelope(envelope)))[0]
        sanitized_message = atomic["messaging"]["message"]
        self.assertEqual(sanitized_message["attachments"], [])
        self.assertEqual(
            sanitized_message["sanitization_rejections"],
            [{"slot": "quick_reply", "reason": "invalid_quick_reply"}],
        )
        inbox = self._inbox(atomic, suffix="unrelated-empty-attachments-rejection")

        with trap_jobs() as trap:
            self.assertTrue(self._run_inbox_job(inbox))
            trap.assert_jobs_count(0)

        inbox.invalidate_recordset()
        self.assertEqual(inbox.state, "dead")
        self.assertEqual(inbox.attempts, 1)
        self.assertEqual(inbox.last_error_class, "AdapterError")
        self.assertFalse(self._message_binding(message["mid"]))

    def test_referral_and_text_survive_unsupported_media(self):
        media_with_referral = self._events("messenger_batch.json")[0]
        before = {
            model: self.env[model].sudo().search_count([])
            for model in (
                "mail.guest",
                "mail.channel",
                "mail.message",
                "contact.center.identity",
                "contact.center.channel.binding",
                "contact.center.message.binding",
            )
        }
        inbox = self._inbox(media_with_referral, suffix="attribution-only")

        with trap_jobs() as trap:
            self.assertTrue(self._run_inbox_job(inbox))
            contact_center_jobs = [
                job
                for job in trap.enqueued_jobs
                if job.channel.startswith("root.contact_center.")
            ]
            self.assertEqual(len(contact_center_jobs), 1)
            self.assertEqual(
                contact_center_jobs[0].channel,
                "root.contact_center.identity_avatar",
            )

        inbox.invalidate_recordset()
        self.assertEqual(inbox.state, "done")
        self.assertFalse(inbox.last_error_class)
        self.assertEqual(
            inbox.normalized_dto_json["message"]["content_type"],
            "text",
        )
        self.assertEqual(
            inbox.normalized_dto_json["message"]["text"],
            "Synthetic Messenger message",
        )
        message_binding = self._message_binding("m_synthetic_active")
        self.assertTrue(message_binding)
        self.assertIn(
            "Synthetic Messenger message", str(message_binding.message_id.body)
        )
        for model, count in before.items():
            self.assertEqual(self.env[model].sudo().search_count([]), count + 1, model)

        touchpoint = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)], limit=1)
        )
        self.assertTrue(touchpoint)
        self.assertEqual(touchpoint.touchpoint_type, "paid_ad_click")
        self.assertEqual(touchpoint.network, "meta")
        self.assertEqual(touchpoint.source_platform, "messenger")
        self.assertEqual(touchpoint.message_binding_id, message_binding)
        self.assertEqual(
            touchpoint.channel_binding_id, message_binding.channel_binding_id
        )
        self.assertEqual(
            touchpoint.identity_id, message_binding.channel_binding_id.identity_id
        )
