import copy
import datetime
import hashlib
import json
import os
from unittest import mock

from odoo import fields

from odoo.addons.contact_center_base.services.ad_origin_preview import (
    presentable_source_url,
)
from odoo.addons.queue_job.tests.common import trap_jobs

from .common import WhatsAppCloudCase

PERMALINK = "https://www.facebook.com/SolozIndustrial/posts/pfbid02xYz"
CDN_IMAGE = "https://scontent.whatsapp.net/v/t45/ad-image.jpg?stp=dst-jpg&oh=signed"


def _referral_fixture():
    path = os.path.join(os.path.dirname(__file__), "fixtures", "referral_message.json")
    with open(path, encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


class TestWhatsAppCloudAttribution(WhatsAppCloudCase):
    def _ad_message(self, **referral_changes):
        message = self.text_message()
        fixture = _referral_fixture()
        message["text"] = fixture["text"]
        referral = copy.deepcopy(fixture["referral"])
        for key, value in referral_changes.items():
            if value is None:
                referral.pop(key, None)
            else:
                referral[key] = value
        message["referral"] = referral
        return message

    def _links(self):
        return (
            self.env["contact.center.whatsapp.cloud.referral.link"]
            .sudo()
            .search([("connection_id", "=", self.connection.id)])
        )

    def _locators(self):
        return (
            self.env["contact.center.ad.preview.locator"]
            .sudo()
            .search([("connection_id", "=", self.connection.id)])
        )

    def _touchpoints(self, wamid):
        return (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search(
                [
                    ("provider_connection_id", "=", self.connection.id),
                    ("source_external_key", "=", wamid),
                ]
            )
        )

    def _normalize_item(self, message):
        delivery = self.create_delivery(self.envelope(self.value(messages=[message])))
        payload = delivery.item_ids.ensure_one().payload_json
        return self.connection.get_adapter().normalize_event(self.connection, payload)

    def test_ad_identifiers_create_a_paid_click_like_wuzapi(self):
        event = self._normalize_item(self._ad_message())
        attribution = event.attribution[0]
        self.assertEqual(len(event.attribution), 1)
        self.assertEqual(attribution.touchpoint_type, "paid_ad_click")
        self.assertEqual(attribution.evidence_level, "provider_asserted")
        self.assertEqual(attribution.network, "meta")
        self.assertEqual(
            {
                (identifier.namespace, identifier.role, identifier.value)
                for identifier in attribution.external_identifiers
            },
            {
                ("meta.source_id", "ad_source", "120210000000000042"),
                (
                    "meta.ctwa_clid",
                    "click",
                    _referral_fixture()["referral"]["ctwa_clid"],
                ),
            },
        )
        self.assertEqual(attribution.creative["title"], "Painéis solares com desconto")
        # The shared item never carries the permalink: the DTO from the ledger
        # has no link until dispatch resolves it into the private envelope.
        self.assertEqual(attribution.source_url, "")
        for kwargs in ({"source_id": None}, {"ctwa_clid": None}):
            with self.subTest(**{key: "absent" for key in kwargs}):
                partial = self._normalize_item(self._ad_message(**kwargs))
                self.assertEqual(
                    partial.attribution[0].touchpoint_type, "paid_ad_click"
                )

    def test_empty_or_marker_only_referral_creates_no_touchpoint(self):
        cases = (
            {},
            {"source_type": "ad"},
            {"source_type": "ad", "source_id": "not-a-number"},
            {"source_type": "post", "source_id": "120210000000000042"},
        )
        for referral in cases:
            with self.subTest(referral=referral):
                message = self.text_message()
                message["referral"] = referral
                _delivery, inbox = self.deliver(
                    self.envelope(self.value(messages=[message]))
                )
                self.assertEqual(inbox.state, "done")
                self.assertFalse(inbox.normalized_dto_json["attribution"])
                self.assertFalse(self._touchpoints(message["id"]))
        self.assertFalse(self._links())

    def test_unsafe_permalinks_are_dropped_and_attribution_stays_without_link(self):
        unsafe = (
            "https://user:secret@www.facebook.com/SolozIndustrial/posts/1",
            "https://www.facebook.com/SolozIndustrial/posts/1?access_token=secret",
            "https://www.facebook.com/SolozIndustrial/posts/1?utm_source=ad",
            "https://evil.example.invalid/SolozIndustrial/posts/1",
            "http://www.facebook.com/SolozIndustrial/posts/1",
        )
        for url in unsafe:
            with self.subTest(url=url):
                message = self._ad_message(source_url=url)
                delivery, inbox = self.deliver(
                    self.envelope(self.value(messages=[message]))
                )
                self.assertFalse(self._links())
                referral = delivery.item_ids.payload_json["message"]["referral"]
                self.assertNotIn("source_link_ref", referral)
                self.assertNotIn(
                    "source_url", inbox.raw_envelope_json["message"]["referral"]
                )
                touchpoint = self._touchpoints(message["id"]).ensure_one()
                self.assertEqual(touchpoint.touchpoint_type, "paid_ad_click")
                self.assertFalse(touchpoint.source_url)

    def test_permalink_crosses_ingress_dispatch_worker_and_preview(self):
        message = self._ad_message()
        wamid = message["id"]
        delivery = self.create_delivery(self.envelope(self.value(messages=[message])))
        item = delivery.item_ids.ensure_one()

        # Ingress: private link row, the item only keeps references.
        link = self._links().ensure_one()
        self.assertEqual(link.source_key, wamid)
        self.assertEqual(link.source_url, PERMALINK)
        referral = item.payload_json["message"]["referral"]
        self.assertEqual(referral["source_link_ref"], link.reference)
        serialized_item = json.dumps(item.payload_json)
        self.assertNotIn("facebook.com", serialized_item)
        self.assertNotIn("whatsapp.net", serialized_item)
        self.assertNotIn(PERMALINK, json.dumps(delivery.sanitized_envelope_json))
        # The CDN thumbnail follows the core locator; the permalink never does.
        locator = self._locators().ensure_one()
        self.assertEqual(locator.download_url, CDN_IMAGE)
        self.assertEqual(locator.source_key, wamid)
        self.assertEqual(referral["thumbnail_ref"], locator.reference)
        permalink_hash = hashlib.sha256(PERMALINK.encode()).hexdigest()
        self.assertFalse(
            self._locators().filtered(lambda row: row.url_hash == permalink_hash)
        )

        # Dispatch: URL only in the private inbox envelope; link row consumed.
        inbox = self.inbox_from(self.dispatch_all(delivery)[0])
        self.assertEqual(
            inbox.raw_envelope_json["message"]["referral"]["source_url"], PERMALINK
        )
        self.assertNotIn(
            "source_link_ref", inbox.raw_envelope_json["message"]["referral"]
        )
        self.assertNotIn(PERMALINK, json.dumps(item.payload_json))
        self.assertFalse(link.exists())

        # Worker after the link row is gone, then a reprocess of the same event.
        self.process_inbox(inbox)
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        touchpoint = self._touchpoints(wamid).ensure_one()
        self.assertEqual(touchpoint.source_url, PERMALINK)
        inbox.sudo().write({"state": "pending"})
        self.process_inbox(inbox)
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        self.assertEqual(self._touchpoints(wamid), touchpoint)
        self.assertEqual(touchpoint.source_url, PERMALINK)
        self.assertEqual(
            inbox.normalized_dto_json["attribution"][0]["source_url"], PERMALINK
        )

        # Preview: public URL from the permalink, only the CDN image is fetched.
        preview = touchpoint.sudo().ad_preview_ids.ensure_one()
        self.assertEqual(preview.source_public_url, PERMALINK)
        self.assertEqual(preview.thumbnail_ref, locator.reference)
        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return b"thumbnail-jpeg", 64, 36

        with mock.patch(
            "odoo.addons.contact_center_base.models.ad_origin_preview.fetch_thumbnail",
            side_effect=fake_fetch,
        ):
            job_preview = preview.sudo()
            if not job_preview.queue_job_uuid:
                job_preview._queue_work()
            job_preview.invalidate_recordset()
            job_preview.with_context(
                job_uuid=job_preview.queue_job_uuid
            )._job_prepare_preview()
        self.assertEqual(fetched, [CDN_IMAGE])
        self.assertNotIn(PERMALINK, fetched)
        # The UI descriptor exposes exactly this browser-stable public link.
        self.assertEqual(presentable_source_url(preview.source_public_url), PERMALINK)
        self.assertTrue(preview.sudo().thumbnail_attachment_id)

    def test_expired_reference_at_dispatch_keeps_attribution_without_link(self):
        message = self._ad_message()
        delivery = self.create_delivery(self.envelope(self.value(messages=[message])))
        link = self._links().ensure_one()
        self.env.cr.execute(
            "UPDATE contact_center_whatsapp_cloud_referral_link "
            "SET expires_at = %s WHERE id = %s",
            [fields.Datetime.now() - datetime.timedelta(minutes=1), link.id],
        )
        link.invalidate_recordset()
        results = self.dispatch_all(delivery)
        self.assertTrue(results[0]["handled"])
        inbox = self.inbox_from(results[0])
        self.assertNotIn("source_url", inbox.raw_envelope_json["message"]["referral"])
        self.assertFalse(link.exists())
        self.process_inbox(inbox)
        self.assertEqual(inbox.state, "done")
        touchpoint = self._touchpoints(message["id"]).ensure_one()
        self.assertEqual(touchpoint.touchpoint_type, "paid_ad_click")
        self.assertFalse(touchpoint.source_url)

    def test_expired_links_are_removed_by_autovacuum(self):
        link_model = self.env["contact.center.whatsapp.cloud.referral.link"]
        reference = link_model._register_source_link(
            self.connection, self.wamid(), PERMALINK
        )
        link = link_model.sudo().search([("reference", "=", reference)])
        self.assertEqual(
            link.expires_at.date(),
            (fields.Datetime.now() + datetime.timedelta(days=8)).date(),
        )
        link_model._gc_expired_links()
        self.assertTrue(link.exists())
        self.env.cr.execute(
            "UPDATE contact_center_whatsapp_cloud_referral_link "
            "SET expires_at = %s WHERE id = %s",
            [fields.Datetime.now() - datetime.timedelta(seconds=1), link.id],
        )
        link.invalidate_recordset()
        link_model._gc_expired_links()
        self.assertFalse(link.exists())

    def test_referral_links_are_private_service_records(self):
        from odoo.exceptions import AccessError

        link_model = self.env["contact.center.whatsapp.cloud.referral.link"]
        with self.assertRaises(AccessError):
            link_model.with_user(self.admin).search([])
        with self.assertRaises(AccessError):
            link_model.sudo().create(
                {
                    "connection_id": self.connection.id,
                    "source_key": "wamid.x",
                    "url_hash": "0" * 64,
                    "source_url": PERMALINK,
                }
            )
        with trap_jobs():
            reference = link_model._register_source_link(
                self.connection, "wamid.private", PERMALINK
            )
        link = link_model.sudo().search([("reference", "=", reference)])
        with self.assertRaises(AccessError):
            link.write({"source_url": "https://www.facebook.com/other"})
        with self.assertRaises(AccessError):
            link.unlink()

    # -- CC-WAC-17: a replacement dispatches what the replaced route received ---

    def _run_preview(self, preview):
        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return b"thumbnail-jpeg", 64, 36

        with mock.patch(
            "odoo.addons.contact_center_base.models.ad_origin_preview.fetch_thumbnail",
            side_effect=fake_fetch,
        ):
            job_preview = preview.sudo()
            if not job_preview.queue_job_uuid:
                job_preview._queue_work()
            job_preview.invalidate_recordset()
            job_preview.with_context(
                job_uuid=job_preview.queue_job_uuid
            )._job_prepare_preview()
        return fetched

    def test_replacement_between_ingestion_and_dispatch_keeps_the_ad_links(self):
        message = self._ad_message()
        wamid = message["id"]
        delivery = self.create_delivery(self.envelope(self.value(messages=[message])))
        link = self._links().ensure_one()
        origin_locator = self._locators().ensure_one()
        # The receiving connection is replaced in the same inbox before dispatch.
        self.connection.write({"outbound_active": False})
        self.connection.write({"active": False})
        replacement = self._create_connection(self.account, self.waba_asset)
        replacement.action_whatsapp_cloud_configure_webhook()

        inbox = self.inbox_from(self.dispatch_all(delivery)[0])
        self.assertEqual(inbox.provider_connection_id, replacement)
        referral = inbox.raw_envelope_json["message"]["referral"]
        self.assertEqual(referral["source_url"], PERMALINK)
        self.assertFalse(link.exists(), "the original link row is consumed")
        rebound = (
            self.env["contact.center.ad.preview.locator"]
            .sudo()
            .search([("connection_id", "=", replacement.id)])
            .ensure_one()
        )
        self.assertEqual(rebound.source_key, wamid)
        self.assertEqual(rebound.download_url, CDN_IMAGE)
        self.assertEqual(referral["thumbnail_ref"], rebound.reference)
        origin_locator.invalidate_recordset()
        self.assertTrue(origin_locator.consumed)
        self.assertFalse(origin_locator.download_url)

        # Worker, then a reprocess of the same durable event.
        touchpoints = self.env["contact.center.attribution.touchpoint"].sudo()
        for _attempt in range(2):
            self.process_inbox(inbox)
            self.assertEqual(inbox.state, "done", inbox.last_error_message)
            touchpoint = touchpoints.search(
                [("source_external_key", "=", wamid)]
            ).ensure_one()
            self.assertEqual(touchpoint.provider_connection_id, replacement)
            self.assertEqual(touchpoint.source_url, PERMALINK)
            inbox.sudo().write({"state": "pending"})

        # The preview resolves the image through the dispatching connection.
        preview = touchpoint.ad_preview_ids.ensure_one()
        self.assertEqual(preview.source_public_url, PERMALINK)
        self.assertEqual(preview.thumbnail_ref, rebound.reference)
        self.assertEqual(self._run_preview(preview), [CDN_IMAGE])
        self.assertTrue(preview.sudo().thumbnail_attachment_id)

    def test_ad_links_of_another_inbox_are_never_resolved(self):
        other_account = self._create_account(team=self.team)
        other = self._create_connection(
            other_account, self.waba_asset, self.OTHER_PHONE_ID
        )
        wamid = self.wamid("foreign-ad")
        link_ref = self.env[
            "contact.center.whatsapp.cloud.referral.link"
        ]._register_source_link(other, wamid, PERMALINK)
        locator_ref = self.env[
            "contact.center.ad.preview.locator"
        ]._register_thumbnail_locator(other, wamid, CDN_IMAGE)
        payload = {
            "message": {
                "id": wamid,
                "referral": {"source_link_ref": link_ref, "thumbnail_ref": locator_ref},
            }
        }
        dispatcher = self.env["meta.webhook.dispatcher"]
        reference, envelope = dispatcher._wac_private_envelope(self.connection, payload)
        self.assertNotIn("source_url", envelope["message"]["referral"])
        self.assertFalse(dispatcher._wac_rebind_thumbnail(self.connection, envelope))
        self.assertNotIn("thumbnail_ref", envelope["message"]["referral"])
        dispatcher._wac_discard_link(self.connection, payload, reference)
        # The other inbox keeps its own rows untouched.
        self.assertTrue(
            self.env["contact.center.whatsapp.cloud.referral.link"]
            .sudo()
            .search([("reference", "=", link_ref)])
        )
        locator = (
            self.env["contact.center.ad.preview.locator"]
            .sudo()
            .search([("reference", "=", locator_ref)])
        )
        self.assertEqual(locator.download_url, CDN_IMAGE)
        self.assertFalse(
            self.env["contact.center.ad.preview.locator"]
            .sudo()
            .search([("connection_id", "=", self.connection.id)])
        )
