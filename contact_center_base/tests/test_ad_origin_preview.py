"""Private presentation authorization, queue ownership and content lifecycle."""

import hashlib
import json
import uuid
from datetime import timedelta
from unittest import mock

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.job import Job

from ..models.ad_origin_preview import _owned
from ..services.ad_origin_preview import PreviewError
from ..services.adapter import ProviderAdapter, adapter_registry
from ..services.dto import AttributionDTO, EventDTO
from ..services.tokens import CONTACT_CENTER_DELETION_TOKEN
from . import test_attribution as attribution_fixtures


@adapter_registry.register("test.ad.preview")
class AdPreviewTransportFixture(ProviderAdapter):
    display_name = "Ad preview synthetic transport"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return EventDTO.from_dict(envelope)

    def execute_command(self, connection, command):
        raise AssertionError("Ad preview tests must not send messages")

    def prepare_request_snapshot(self, connection, command):
        raise AssertionError("Ad preview tests must not prepare outbound requests")

    def get_capabilities(self, connection):
        return {}

    def get_health(self, connection):
        return {"state": "connected"}


@tagged("post_install", "-at_install", "contact_center_ad_origin")
class TestContactCenterAdOriginPreview(SavepointCase):
    _attribution = attribution_fixtures.TestContactCenterAttribution._attribution
    _event = attribution_fixtures.TestContactCenterAttribution._event
    _inbox = attribution_fixtures.TestContactCenterAttribution._inbox
    _run_inbox_job = attribution_fixtures.TestContactCenterAttribution._run_inbox_job
    _process_job = attribution_fixtures.TestContactCenterAttribution._process_job

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = cls._user("agent", "group_contact_center_agent")
        cls.admin = cls._user("admin", "group_contact_center_admin")
        cls.supervisor = cls._user("supervisor", "group_contact_center_supervisor")
        cls.outsider = cls._user("outsider", "group_contact_center_agent")
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Ad preview synthetic inbox",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "ad-preview-account-" + str(uuid.uuid4()),
                "access_user_ids": [(6, 0, (cls.agent | cls.admin).ids)],
                "attribution_ui_enabled": True,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Ad preview transport",
                "account_id": cls.account.id,
                "adapter_key": "test.ad.preview",
                "external_ref": "ad-preview-connection-" + str(uuid.uuid4()),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": False,
            }
        )
        cls.previews = cls.env["contact.center.attribution.preview"]
        cls.locators = cls.env["contact.center.ad.preview.locator"]

    @classmethod
    def _user(cls, name, group):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Ad preview " + name,
                    "login": "ad-preview-" + str(uuid.uuid4()),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [
                        (6, 0, cls.env.ref("contact_center_base." + group).ids)
                    ],
                }
            )
        )

    def setUp(self):
        super().setUp()
        fetch_patch = mock.patch(
            "odoo.addons.contact_center_base.models.ad_origin_preview.fetch_thumbnail",
            return_value=(b"synthetic-private-jpeg", 320, 180),
        )
        self.fetch = fetch_patch.start()
        self.addCleanup(fetch_patch.stop)
        marketing_patch = mock.patch.object(
            type(self.previews), "_marketing_preview_values", return_value={}
        )
        self.marketing = marketing_patch.start()
        self.addCleanup(marketing_patch.stop)
        for target in (
            "requests.sessions.Session.request",
            "urllib3.HTTPSConnectionPool.urlopen",
        ):
            network = mock.patch(
                target, side_effect=AssertionError("Real HTTP is forbidden in tests")
            )
            network.start()
            self.addCleanup(network.stop)

    def _preview(self, *, creative=None, image=False, message_id=None):
        key = message_id or "ad-preview-message-" + str(uuid.uuid4())
        creative = dict(
            creative or {"title": "Provider title", "body": "Provider body"}
        )
        if image:
            creative["thumbnail_ref"] = self.locators._register(
                self.connection,
                key,
                "https://scontent.fbcdn.net/fixture.jpg?signature=private-fixture",
            )
        inbox = self._process_job(
            self._event(
                external_message_id=key,
                attribution=self._attribution(creative=creative),
            )
        )
        self.assertEqual(inbox.state, "done")
        point = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)], limit=1)
        )
        preview = self.previews.sudo().search([("touchpoint_id", "=", point.id)])
        self.assertEqual(len(preview), 1)
        self.assertTrue(preview._eligible())
        return preview

    def _job(self, preview):
        return (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", preview.queue_job_uuid)], limit=1)
        )

    def _perform(self, preview):
        return preview.with_context(
            job_uuid=preview.queue_job_uuid
        )._job_prepare_preview()

    def _descriptor(self, preview, user=None):
        return preview.with_user(user or self.agent).sudo()._descriptor()

    def _redact(self, preview):
        mutation = self.env["contact.center.message.mutation"].create(
            {
                "target_message_binding_id": preview.touchpoint_id.message_binding_id.id,
                "provider_connection_id": self.connection.id,
                "mutation_type": "delete",
                "direction": "inbound",
                "deletion_display_mode": "redact",
                "external_event_id": str(uuid.uuid4()),
            }
        )
        mutation._apply_delete()

    def test_capture_and_queue_do_not_fetch_remote_content(self):
        preview = self._preview(image=True)
        self.assertEqual(preview.state, "pending")
        self.assertTrue(self._job(preview))
        self.fetch.assert_not_called()
        self.marketing.assert_not_called()
        self.assertFalse(self.account.ad_preview_enrichment_enabled)

    def test_private_locator_is_exact_scope_and_deduplicated(self):
        key = "locator-" + str(uuid.uuid4())
        url = "https://scontent.fbcdn.net/fixture.jpg?signature=synthetic"
        reference = self.locators._register(self.connection, key, url)
        self.assertEqual(reference, self.locators._register(self.connection, key, url))
        locator = self.locators._resolve(reference, self.connection, key)
        self.assertEqual(locator.download_url, url)
        with self.assertRaises(PreviewError):
            self.locators._resolve(reference, self.connection, "different-message")
        other = self.connection.copy(
            {
                "external_ref": str(uuid.uuid4()),
                "role": "standby",
                "inbound_active": False,
            }
        )
        with self.assertRaises(PreviewError):
            self.locators._resolve(reference, other, key)
        locator._consume()
        self.assertFalse(locator.download_url)
        with self.assertRaises(PreviewError):
            self.locators._resolve(reference, self.connection, key)

    def test_internal_models_reject_manual_changes_and_locator_reads(self):
        preview = self._preview(image=True)
        locator = self.locators.sudo().search(
            [("reference", "=", preview.thumbnail_ref)]
        )
        for user in (self.agent, self.supervisor, self.admin):
            with self.subTest(user=user.name), self.assertRaises(AccessError):
                locator.with_user(user).read(["download_url"])
        for record, values in (
            (preview, {"title": "tampered"}),
            (locator, {"download_url": False}),
        ):
            with self.assertRaises(AccessError):
                record.sudo().write(values)
            with self.assertRaises(AccessError):
                record.sudo().unlink()
        with self.assertRaises(AccessError):
            self.previews.sudo().create({"touchpoint_id": preview.touchpoint_id.id})
        with self.assertRaises(AccessError):
            _owned(preview).write({"public_ref": str(uuid.uuid4())})

    def test_authorized_viewer_receives_only_private_thumbnail_route(self):
        preview = self._preview(image=True)
        self.assertTrue(self._perform(preview))
        descriptor = self._descriptor(preview)
        self.assertEqual(descriptor["state"], "ready")
        self.assertEqual(descriptor["title"], "Provider title")
        self.assertEqual(
            descriptor["thumbnail_url"],
            "/contact_center/attribution/%s/thumbnail" % preview.public_ref,
        )
        self.assertNotIn("signature", json.dumps(descriptor))
        self.assertNotIn("fbcdn", json.dumps(descriptor))
        attachment = preview.thumbnail_attachment_id
        self.assertFalse(attachment.public)
        self.assertEqual(attachment.res_model, preview._name)
        self.assertEqual(attachment.res_id, preview.id)
        self.assertEqual((preview.width, preview.height), (320, 180))
        locator = self.locators.sudo().search(
            [("reference", "=", preview.thumbnail_ref)]
        )
        self.assertTrue(locator.consumed)
        self.assertFalse(locator.download_url)

    def test_outsider_and_other_company_cannot_read_preview(self):
        preview = self._preview()
        with self.assertRaises(AccessError):
            self._descriptor(preview, self.outsider)
        company = self.env["res.company"].create({"name": "Ad preview other company"})
        with self.assertRaises(AccessError):
            preview.with_user(self.agent).sudo().with_context(
                allowed_company_ids=company.ids
            )._descriptor()

    def test_policy_off_hides_preview_from_agent_but_admin_can_read(self):
        preview = self._preview()
        self.account.write({"attribution_ui_enabled": False})
        with self.assertRaises(AccessError):
            self._descriptor(preview)
        self.assertEqual(
            self._descriptor(preview, self.admin)["title"], "Provider title"
        )

    def test_only_admin_can_enable_enrichment_or_backfill(self):
        for user in (self.agent, self.supervisor):
            with self.subTest(user=user.name), self.assertRaises(AccessError):
                self.account.with_user(user).write(
                    {"ad_preview_enrichment_enabled": True}
                )
            with self.subTest(user=user.name), self.assertRaises(AccessError):
                self.account.with_user(user).action_backfill_ad_previews()
            with self.subTest(user=user.name), self.assertRaises(AccessError):
                self.env["contact.center.account"].with_user(user).with_context(
                    default_ad_preview_enrichment_enabled=True
                ).create({"name": "Must not create", "platform": "whatsapp"})
        self.account.with_user(self.admin).write(
            {"ad_preview_enrichment_enabled": True}
        )
        self.assertTrue(self.account.ad_preview_enrichment_enabled)

    def test_presentation_changes_do_not_change_acquisition_fingerprint(self):
        first = self._attribution(creative={"title": "First", "body": "Original"})
        later = self._attribution(
            creative={
                "title": "Different",
                "body": "Changed",
                "thumbnail_ref": str(uuid.uuid4()),
                "public_url": "https://www.facebook.com/123/posts/456",
            }
        )
        model = self.env["contact.center.attribution.touchpoint"]
        self.assertEqual(
            model._attribution_fingerprint(AttributionDTO.from_dict(first)),
            model._attribution_fingerprint(AttributionDTO.from_dict(later)),
        )

    def test_capture_dedupe_preserves_original_copy_and_ledger(self):
        preview = self._preview()
        point = preview.touchpoint_id
        fingerprint = point.attribution_fingerprint
        original = point.inbox_event_id.raw_envelope_json
        repeated = self.previews._capture(
            point, {"title": "Replacement", "body": "Replacement"}, merge=True
        )
        self.assertEqual(repeated, preview)
        self.assertEqual(preview.title, "Provider title")
        self.assertEqual(preview.body, "Provider body")
        self.assertEqual(point.attribution_fingerprint, fingerprint)
        self.assertEqual(point.inbox_event_id.raw_envelope_json, original)

    def test_queue_is_idempotent_and_requires_persisted_job_owner(self):
        preview = self._preview(image=True)
        first = self._job(preview)
        preview._queue_work()
        self.assertEqual(self._job(preview), first)
        self.assertFalse(preview._job_prepare_preview())
        self.assertFalse(
            preview.with_context(job_uuid=str(uuid.uuid4()))._job_prepare_preview()
        )
        self.fetch.assert_not_called()

    def test_pending_without_active_job_is_unavailable_to_ui(self):
        preview = self._preview(image=True)
        self._job(preview).button_cancelled()
        self.assertEqual(self._descriptor(preview)["state"], "unavailable")
        self.assertEqual(preview.state, "pending")

    def test_enrichment_fills_only_missing_fields_and_never_rewrites_evidence(self):
        self.account.write({"ad_preview_enrichment_enabled": True})
        preview = self._preview(creative={"title": "Snapshot title"})
        original = preview.touchpoint_id.inbox_event_id.raw_envelope_json
        self.marketing.return_value = {
            "title": "Catalog replacement",
            "body": "Actual ad copy",
            "public_url": "https://www.instagram.com/p/fixture/",
            "thumbnail_url": "https://scontent.fbcdn.net/creative.jpg?signature=private",
            "media_type": "image",
        }
        self.assertTrue(self._perform(preview))
        self.assertEqual(preview.title, "Snapshot title")
        self.assertEqual(preview.body, "Actual ad copy")
        self.assertEqual(preview.presentation_source, "marketing_catalog")
        self.assertTrue(preview.enrichment_attempted)
        self.assertTrue(preview.thumbnail_attachment_id)
        self.assertEqual(
            preview.touchpoint_id.inbox_event_id.raw_envelope_json, original
        )
        preview._queue_work()
        self.marketing.assert_called_once()

    def test_enrichment_disabled_before_job_does_not_call_marketing(self):
        self.account.write({"ad_preview_enrichment_enabled": True})
        preview = self._preview(creative={"title": "Snapshot"})
        self.account.write({"ad_preview_enrichment_enabled": False})
        self.assertTrue(self._perform(preview))
        self.marketing.assert_not_called()
        self.assertEqual(preview.title, "Snapshot")

    def test_redaction_before_job_consumes_locator_and_blocks_download(self):
        preview = self._preview(image=True)
        reference = preview.thumbnail_ref
        self._redact(preview)
        self.assertFalse(self._perform(preview))
        self.fetch.assert_not_called()
        self.assertTrue(preview.expired)
        self.assertFalse(preview.title)
        self.assertFalse(preview.body)
        self.assertFalse(preview.thumbnail_attachment_id)
        self.assertTrue(
            self.locators.sudo().search([("reference", "=", reference)]).consumed
        )

    def test_redaction_during_download_discards_derivative(self):
        preview = self._preview(image=True)

        def redact_then_return(_url):
            self._redact(preview)
            return b"late-private-jpeg", 100, 50

        self.fetch.side_effect = redact_then_return
        self.assertFalse(self._perform(preview))
        self.assertTrue(preview.expired)
        self.assertFalse(preview.thumbnail_attachment_id)
        self.assertFalse(preview.title)
        self.assertFalse(
            self.env["ir.attachment"]
            .sudo()
            .search([("res_model", "=", preview._name), ("res_id", "=", preview.id)])
        )

    def test_redaction_during_enrichment_discards_copy(self):
        self.account.write({"ad_preview_enrichment_enabled": True})
        preview = self._preview(creative={"title": "Snapshot"})

        def redact_then_return():
            self._redact(preview)
            return {"body": "Must not reappear"}

        self.marketing.side_effect = redact_then_return
        self.assertFalse(self._perform(preview))
        self.assertTrue(preview.expired)
        self.assertFalse(preview.title)
        self.assertFalse(preview.body)

    def test_expiry_removes_attachment_and_prevents_capture_resurrection(self):
        preview = self._preview(image=True)
        self._perform(preview)
        attachment = preview.thumbnail_attachment_id
        preview._expire()
        self.assertFalse(attachment.exists())
        self.assertFalse(preview.thumbnail_attachment_id)
        self.assertEqual(preview.error_code, "expired")
        self.assertEqual(
            self.previews._capture(
                preview.touchpoint_id, {"title": "Late"}, merge=True
            ),
            preview,
        )
        self.assertFalse(preview.title)
        with self.assertRaises(AccessError):
            self._descriptor(preview)

    def test_ttl_cleanup_expires_previews_and_orphan_locators(self):
        preview = self._preview(image=True)
        self._perform(preview)
        past = fields.Datetime.now() - timedelta(days=1)
        _owned(preview).write({"expires_at": past})
        orphan = _owned(self.locators).create(
            {
                "connection_id": self.connection.id,
                "source_key": "orphan-" + str(uuid.uuid4()),
                "url_hash": hashlib.sha256(b"orphan").hexdigest(),
                "download_url": "https://scontent.fbcdn.net/orphan.jpg",
                "expires_at": past,
            }
        )
        self.previews._cron_expire()
        self.assertTrue(preview.expired)
        self.assertFalse(preview.thumbnail_attachment_id)
        self.assertTrue(orphan.consumed)
        self.assertFalse(orphan.download_url)

    def test_retention_collects_current_and_historical_jobs(self):
        preview = self._preview(image=True)
        first = self._job(preview)
        stored = Job.load(self.env, first.uuid)
        stored.set_done()
        stored.store()
        preview._queue_work()
        second = self._job(preview)
        self.assertNotEqual(first, second)
        jobs = self.env["contact.center.retention"]._jobs(
            [], preview.touchpoint_id.message_binding_id.message_id
        )
        self.assertTrue(first in jobs)
        self.assertTrue(second in jobs)
        preview._expire()
        self.assertEqual(second.state, "cancelled")
        self.assertEqual(first.state, "done")

    def test_backfill_is_bounded_skips_complete_and_preserves_expired(self):
        full = self._preview(
            creative={
                "title": "Complete",
                "body": "Complete body",
                "public_url": "https://www.instagram.com/p/complete/",
            },
            image=True,
        )
        expired = self._preview()
        expired._expire()
        legacy = [self._preview() for _item in range(3)]
        points = [preview.touchpoint_id for preview in legacy]
        for preview in legacy:
            _owned(preview).unlink()
        self.account.write({"ad_preview_enrichment_enabled": True})
        self.assertEqual(self.previews._backfill_account(self.account, limit=2), 2)
        rebuilt = self.previews.sudo().search(
            [("touchpoint_id", "in", [p.id for p in points])]
        )
        self.assertEqual(len(rebuilt), 2)
        self.assertTrue(all(rebuilt.mapped("queue_job_uuid")))
        self.assertFalse(expired.title)
        self.assertTrue(expired.expired)
        self.assertEqual(full.title, "Complete")
        self.fetch.assert_not_called()
        self.marketing.assert_not_called()

    def test_retention_preparation_expires_derived_content_and_preserves_evidence(self):
        preview = self._preview(image=True)
        self._perform(preview)
        point = preview.touchpoint_id
        fingerprint = point.attribution_fingerprint
        binding = point.message_binding_id
        result = self.env[
            "contact.center.retention"
        ]._retention_prepare_external_references(
            point.channel_binding_id,
            binding.message_id,
            binding,
            point.inbox_event_id,
        )
        self.assertIsNot(result, False)
        self.assertTrue(preview.expired)
        self.assertFalse(preview.thumbnail_attachment_id)
        self.assertEqual(point.attribution_fingerprint, fingerprint)
        self.assertTrue(point.identifier_ids)

    def test_conversation_deletion_expires_preview_before_detaching_projection(self):
        preview = self._preview(image=True)
        self._perform(preview)
        point = preview.touchpoint_id
        binding = point.channel_binding_id
        self.env["contact.center.attribution.touchpoint"].with_context(
            contact_center_deletion_token=CONTACT_CENTER_DELETION_TOKEN
        )._contact_center_prepare_conversation_deletion(
            binding.channel_id,
            binding,
            point.message_binding_id.message_id,
            point.inbox_event_id,
        )
        self.assertTrue(preview.expired)
        self.assertFalse(preview.thumbnail_attachment_id)
        self.assertFalse(point.channel_binding_id)
        self.assertFalse(point.message_binding_id)
        self.assertTrue(point.identifier_ids)

    def test_retryable_download_has_bounded_retries_and_sanitized_failure(self):
        preview = self._preview(image=True)
        self.fetch.side_effect = PreviewError("download_failed", retryable=True)
        with self.assertRaises(RetryableJobError):
            self._perform(preview)
        self.assertEqual(preview.state, "pending")
        stored = Job.load(self.env, self._job(preview).uuid)
        stored.retry = 3
        stored.store()
        self.assertTrue(self._perform(preview))
        self.assertEqual(preview.state, "unavailable")
        self.assertEqual(preview.error_code, "download_failed")
        self.assertFalse(preview.thumbnail_attachment_id)

    def test_untrusted_enrichment_exception_is_not_exposed(self):
        self.account.write({"ad_preview_enrichment_enabled": True})
        preview = self._preview(creative={"title": "Snapshot"})
        self.marketing.side_effect = RuntimeError(
            "https://private.invalid/?token=secret"
        )
        self.assertTrue(self._perform(preview))
        self.assertEqual(preview.error_code, "unavailable")
        self.assertNotIn("secret", json.dumps(self._descriptor(preview)))

    def test_message_projection_includes_preview_only_for_authorized_viewer(self):
        preview = self._preview()
        binding = preview.touchpoint_id.message_binding_id
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        result = api._serialize_message(
            binding.message_id.sudo(), binding=binding.sudo()
        )
        self.assertEqual(result["ad_origin_previews"][0]["title"], "Provider title")
        self.account.write({"attribution_ui_enabled": False})
        result = api._serialize_message(
            binding.message_id.sudo(), binding=binding.sudo()
        )
        self.assertEqual(result["ad_origin_previews"], [])
