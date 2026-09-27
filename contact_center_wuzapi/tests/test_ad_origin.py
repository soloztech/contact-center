"""Synthetic externalAdReply snapshots; no customer data or provider downloads."""

import copy
import json
import uuid
from unittest import mock

from odoo.tests import tagged

from odoo.addons.contact_center_base.services.dto import (
    AttributionDTO,
    ExternalIdentifierDTO,
)

from ..controllers.webhook import _capture_ad_origin_preview, sanitize_webhook_envelope
from ..services.adapter import (
    WUZAPI_VERSION,
    WuzapiAdapter,
    ad_origin_preview_candidate,
)
from .common import WuzapiCase


@tagged("post_install", "-at_install", "contact_center_ad_origin")
class TestWuzapiAdOrigin(WuzapiCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.adapter = WuzapiAdapter(cls.env)

    def _fixture(self):
        return self.load_fixture("message_ad_origin.json")

    def test_ingress_separates_private_thumbnail_and_keeps_original_message(self):
        raw = self._fixture()
        sanitized = sanitize_webhook_envelope(raw)
        with mock.patch("requests.sessions.Session.request") as request:
            _capture_ad_origin_preview(self.connection, raw, sanitized)
        request.assert_not_called()
        event = self.adapter.normalize_event(self.connection, sanitized)
        self.assertEqual(event.message.text, "Hello Can I get more info")
        self.assertEqual(len(event.attribution), 1)
        creative = event.attribution[0].creative
        self.assertEqual(creative["title"], "Solicite um orçamento gratuito!")
        self.assertIn("Descrição sintética", creative["body"])
        self.assertEqual(
            creative["public_url"], "https://www.instagram.com/p/SyntheticAd/"
        )
        locator = (
            self.env["contact.center.ad.preview.locator"]
            .sudo()
            .search([("reference", "=", creative["thumbnail_ref"])])
        )
        self.assertEqual(locator.connection_id, self.connection)
        self.assertEqual(locator.source_key, event.message.external_message_id)
        self.assertIn("synthetic-private", locator.download_url)
        for value in (sanitized, event.to_dict()):
            self.assertNotIn("synthetic-private", json.dumps(value))
            self.assertNotIn("synthetic-inline-bytes", json.dumps(value))

    def test_quoted_outbound_and_conflicting_contexts_do_not_supply_creative(self):
        raw = self._fixture()
        quoted = copy.deepcopy(raw)
        message = quoted["event"]["Message"]
        message["extendedTextMessage"] = {
            "text": "A normal reply",
            "contextInfo": {"quotedMessage": copy.deepcopy(raw["event"]["Message"])},
        }
        self.assertIsNone(ad_origin_preview_candidate(quoted))
        own = copy.deepcopy(raw)
        own["event"]["Info"]["IsFromMe"] = True
        self.assertIsNone(ad_origin_preview_candidate(own))
        external = raw["event"]["Message"]["extendedTextMessage"]["contextInfo"][
            "externalAdReply"
        ]
        secondary = copy.deepcopy(external)
        for field in ("title", "body", "sourceURL", "thumbnailURL", "originalImageURL"):
            external.pop(field)
        secondary["sourceID"] = "conflicting-other-ad"
        raw["event"]["Message"]["messageContextInfo"] = {"externalAdReply": secondary}
        self.assertIsNone(ad_origin_preview_candidate(raw))

    def test_forged_preview_is_removed_and_cross_message_reference_is_ignored(self):
        raw = self._fixture()
        raw["contact_center_ad_origin"] = {
            "source_key": raw["event"]["Info"]["ID"],
            "creative": {
                "title": "Forged copy",
                "thumbnail_ref": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            },
        }
        sanitized = sanitize_webhook_envelope(raw)
        self.assertNotIn("contact_center_ad_origin", sanitized)
        sanitized["contact_center_ad_origin"] = {
            "source_key": "different-message",
            "creative": {"title": "Forged copy"},
        }
        event = self.adapter.normalize_event(self.connection, sanitized)
        self.assertNotIn("title", event.attribution[0].creative)

    def test_history_recovers_copy_without_locators_and_checks_message_identity(self):
        raw = sanitize_webhook_envelope(self._fixture())
        source_key = raw["event"]["Info"]["ID"]
        with mock.patch("requests.sessions.Session.request") as request:
            preview = self.adapter.ad_origin_preview_from_history(raw, source_key)
        request.assert_not_called()
        self.assertEqual(preview["title"], "Solicite um orçamento gratuito!")
        self.assertNotIn("thumbnail_ref", preview)
        self.assertEqual(self.adapter.ad_origin_preview_from_history(raw, "other"), {})
        self.assertFalse(
            self.env["contact.center.ad.preview.locator"].sudo().search_count([])
        )

    def _process_inbox(self, raw):
        sanitized = sanitize_webhook_envelope(raw)
        _capture_ad_origin_preview(self.connection, raw, sanitized)
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "wuzapi:ad-origin-test:%s" % uuid.uuid4(),
                    "provider_schema_version": WUZAPI_VERSION,
                    "raw_envelope_json": sanitized,
                }
            )
        )
        inbox.write({"queue_job_uuid": str(uuid.uuid4())})
        inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
        inbox.invalidate_recordset(["state"])
        self.assertEqual(inbox.state, "done")
        return inbox

    def test_lone_conversion_marker_projects_messages_without_ad_origin(self):
        inboxes = self.env["contact.center.inbox.event"]
        raws = []
        for index in range(3):
            raw = self.load_fixture("message_text_lid.json")
            raw["event"]["Info"]["ID"] = "3EB0FBADS%023d" % index
            # A fresh message, not a reply: only the marker is left in context.
            raw["event"]["Message"]["extendedTextMessage"]["contextInfo"] = {
                "conversionSource": "FB_Ads",
                "conversionData": "opaque-marker",
            }
            raws.append(raw)
            inboxes |= self._process_inbox(copy.deepcopy(raw))
        # Replaying one delivery is idempotent and still creates no ad origin.
        inboxes |= self._process_inbox(copy.deepcopy(raws[0]))
        message_ids = [raw["event"]["Info"]["ID"] for raw in raws]
        touchpoints = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "in", inboxes.ids)])
        )
        self.assertFalse(touchpoints)
        self.assertFalse(
            self.env["contact.center.attribution.preview"]
            .sudo()
            .search_count([("account_id", "=", self.account.id)])
        )
        bindings = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("external_message_id", "in", message_ids),
                    ("provider_connection_id", "=", self.connection.id),
                ]
            )
        )
        self.assertEqual(sorted(bindings.mapped("external_message_id")), message_ids)

    def test_real_click_keeps_one_idempotent_origin_with_marker(self):
        raw = self._fixture()
        external = raw["event"]["Message"]["extendedTextMessage"]["contextInfo"][
            "externalAdReply"
        ]
        external["sourceID"] = "120212345678900017"
        raw["event"]["Message"]["extendedTextMessage"]["contextInfo"][
            "conversionSource"
        ] = "FB_Ads"
        first = self._process_inbox(copy.deepcopy(raw))
        replay = self._process_inbox(copy.deepcopy(raw))
        touchpoints = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "in", (first | replay).ids)])
        )
        self.assertEqual(len(touchpoints), 1)
        self.assertEqual(touchpoints.touchpoint_type, "paid_ad_click")
        self.assertEqual(touchpoints.conversion_source, "fb_ads")
        self.assertTrue(touchpoints._has_ad_identity())

    def test_backfill_recovers_copy_of_touchpoint_recorded_from_legacy_marker(self):
        raw = self.load_fixture("message_text_lid.json")
        raw["event"]["Info"]["ID"] = "3EB0LEGACYFBADS%017d" % 1
        raw["event"]["Message"]["extendedTextMessage"]["contextInfo"] = {
            "conversionSource": "FB_Ads",
            "externalAdReply": {
                "sourceID": "120212345678900017",
                "title": "Legacy copy",
                "body": "Legacy body",
            },
        }
        # A new delivery of this shape no longer creates any ad origin.
        sanitized = sanitize_webhook_envelope(copy.deepcopy(raw))
        self.assertEqual(
            self.adapter.normalize_event(self.connection, sanitized).attribution, ()
        )
        # Simulate a touchpoint recorded before this release and before previews.
        legacy = (
            AttributionDTO(
                touchpoint_type="paid_ad_signal",
                evidence_level="provider_hint",
                network="meta",
                external_identifiers=(
                    ExternalIdentifierDTO(
                        namespace="meta.source_id",
                        role="ad_source",
                        value="120212345678900017",
                        source_field="contextInfo.externalAdReply.sourceID",
                    ),
                ),
                entry_point={"conversion_source": "fb_ads"},
            ),
        )
        previews = self.env["contact.center.attribution.preview"]
        with mock.patch(
            "odoo.addons.contact_center_wuzapi.services.adapter._attribution_values",
            return_value=legacy,
        ), mock.patch.object(type(previews), "_capture", return_value=previews):
            inbox = self._process_inbox(copy.deepcopy(raw))
        point = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("inbox_event_id", "=", inbox.id)])
        )
        self.assertEqual(point.touchpoint_type, "paid_ad_signal")
        self.assertFalse(previews.sudo().search([("touchpoint_id", "=", point.id)]))

        previews.sudo()._backfill_account(self.account)

        preview = previews.sudo().search([("touchpoint_id", "=", point.id)])
        self.assertEqual((preview.title, preview.body), ("Legacy copy", "Legacy body"))
        self.assertTrue(preview._is_presentable())
