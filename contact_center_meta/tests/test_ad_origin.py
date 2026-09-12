"""Synthetic optional CTM fields from RestFB PostbackReferral.AdsContextData.

The fixture is not a WhatsApp Cloud payload and contains no customer material:
https://restfb.com/javadoc/src-html/com/restfb/types/webhook/messaging/PostbackReferral.AdsContextData.html
"""

import copy
import json
from unittest import mock

from odoo.tests import tagged

from odoo.addons.meta_webhook_base.services.tokens import META_WEBHOOK_INTERNAL_TOKEN
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.messaging import (
    ad_origin_referral_candidate,
    atomic_dedupe_key,
    atomic_events,
    routing_values,
    sanitize_webhook_envelope,
)
from .common import MetaCase


@tagged("post_install", "-at_install", "contact_center_ad_origin")
class TestMetaAdOrigin(MetaCase):
    def _fixture(self):
        return self.load_fixture("messenger_ad_origin.json")

    def _normalized_delivery(self, raw):
        with mock.patch("requests.sessions.Session.request") as request:
            delivery = self.create_delivery(raw)
        request.assert_not_called()
        item = delivery.item_ids.ensure_one()
        event = self.connection.get_adapter().normalize_event(
            self.connection, item.payload_json
        )
        return delivery, item, event

    def test_ad_copy_and_private_image_cross_shared_ingress_without_changing_message(
        self,
    ):
        delivery, item, event = self._normalized_delivery(self._fixture())
        self.assertEqual(event.message.text, "Hello Can I get more info")
        creative = event.attribution[0].creative
        self.assertEqual(creative["title"], "Solicite um orçamento gratuito!")
        self.assertNotIn("body", creative)
        self.assertNotIn("public_url", creative)
        locator = (
            self.env["contact.center.ad.preview.locator"]
            .sudo()
            .search([("reference", "=", creative["thumbnail_ref"])])
        )
        self.assertEqual(locator.connection_id, self.connection)
        self.assertEqual(locator.source_key, event.message.external_message_id)
        self.assertIn("synthetic-private", locator.download_url)
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        dispatch = delivery.dispatch_ids.ensure_one()
        result = self.env["meta.webhook.dispatcher"]._dispatch_consumer(dispatch)
        inbox_id = int(result["result_ref"].rsplit(":", 1)[1])
        inbox = self.env["contact.center.inbox.event"].browse(inbox_id)
        for value in (
            delivery.sanitized_envelope_json,
            item.payload_json,
            inbox.raw_envelope_json,
            event.to_dict(),
        ):
            self.assertNotIn("synthetic-private", json.dumps(value))
        self.assertEqual(inbox.provider_connection_id, self.connection)

    def test_standalone_referral_uses_event_source_and_preview_does_not_change_dedupe(
        self,
    ):
        raw = self._fixture()
        messaging = raw["entry"][0]["messaging"][0]
        messaging["referral"] = messaging.pop("message")["referral"]
        sanitized = next(atomic_events(sanitize_webhook_envelope(raw)))
        route = routing_values(sanitized)
        original_key = atomic_dedupe_key(sanitized, route)
        changed = copy.deepcopy(sanitized)
        changed["messaging"]["referral"]["contact_center_ad_origin"] = {
            "title": "Updated presentation",
            "thumbnail_ref": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        }
        self.assertEqual(atomic_dedupe_key(changed, route), original_key)
        _delivery, item, event = self._normalized_delivery(raw)
        self.assertIsNone(event.message)
        self.assertEqual(event.event_type, "attribution.observed")
        self.assertEqual(atomic_dedupe_key(item.payload_json, route), original_key)
        locator = (
            self.env["contact.center.ad.preview.locator"]
            .sudo()
            .search(
                [("reference", "=", event.attribution[0].creative["thumbnail_ref"])]
            )
        )
        self.assertEqual(locator.source_key, event.event_id)
        self.assertTrue(event.event_id.startswith("Referral:"))

    def test_unsubscribed_referral_does_not_create_preview_locator(self):
        self.subscriptions.filtered(
            lambda item: item.object_type == "page" and item.field_name == "messages"
        ).write({"active": False})
        delivery = self.create_delivery(self._fixture())
        self.assertEqual(delivery.item_ids.ensure_one().kind, "unknown")
        self.assertFalse(
            self.env["contact.center.ad.preview.locator"]
            .sudo()
            .search_count([("connection_id", "=", self.connection.id)])
        )

    def test_quoted_echo_and_untrusted_marker_do_not_supply_an_ad_preview(self):
        raw = self._fixture()
        messaging = raw["entry"][0]["messaging"][0]
        message = messaging["message"]
        referral = message.pop("referral")
        message["reply_to"] = {"mid": "m_quoted", "referral": referral}
        self.assertIsNone(ad_origin_referral_candidate(messaging))
        message["referral"] = referral
        message["is_echo"] = True
        self.assertIsNone(ad_origin_referral_candidate(messaging))
        message.pop("is_echo")
        referral.pop("ads_context_data")
        referral["contact_center_ad_origin"] = {
            "title": "Forged preview",
            "thumbnail_ref": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        }
        sanitized = next(atomic_events(sanitize_webhook_envelope(raw)))
        self.assertNotIn(
            "contact_center_ad_origin", sanitized["messaging"]["message"]["referral"]
        )

    def test_malformed_optional_photo_keeps_bounded_title_and_customer_message(self):
        raw = self._fixture()
        context = raw["entry"][0]["messaging"][0]["message"]["referral"][
            "ads_context_data"
        ]
        context.update(
            {"ad_title": "A" * 1000, "photo_url": "http://127.0.0.1/private"}
        )
        _delivery, _item, event = self._normalized_delivery(raw)
        self.assertEqual(event.message.text, "Hello Can I get more info")
        self.assertEqual(event.attribution[0].creative["title"], "A" * 256)
        self.assertNotIn("thumbnail_ref", event.attribution[0].creative)
