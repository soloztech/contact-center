"""Synthetic externalAdReply snapshots; no customer data or provider downloads."""

import copy
import json
from unittest import mock

from odoo.tests import tagged

from ..controllers.webhook import _capture_ad_origin_preview, sanitize_webhook_envelope
from ..services.adapter import WuzapiAdapter, ad_origin_preview_candidate
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
