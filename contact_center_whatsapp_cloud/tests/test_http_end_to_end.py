import json

from odoo.tests.common import HttpCase, tagged

from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.contracts import WHATSAPP_CLOUD_CONSUMER_KEY
from .common import WhatsAppCloudFixtureMixin


@tagged("post_install", "-at_install")
class TestWhatsAppCloudHttpEndToEnd(WhatsAppCloudFixtureMixin, HttpCase):
    """A signed POST on the shared route reaches the existing Contact Center UI."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_whatsapp_cloud()

    @classmethod
    def tearDownClass(cls):
        cls._restore_environment()
        super().tearDownClass()

    def _post(self, body):
        return self.opener.post(
            "%s/meta/webhook/%s" % (self.base_url(), self.endpoint.routing_key),
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": self.signature(body),
            },
            timeout=30,
        )

    def _run_pipeline(self, delivery):
        delivery = delivery.sudo()
        with trap_jobs():
            self.assertTrue(
                delivery.with_context(job_uuid=delivery.queue_job_uuid)._job_fanout()
            )
        dispatches = delivery.dispatch_ids.filtered(
            lambda dispatch: dispatch.consumer_key == WHATSAPP_CLOUD_CONSUMER_KEY
        )
        inboxes = self.env["contact.center.inbox.event"].sudo()
        for dispatch in dispatches:
            with trap_jobs():
                self.assertTrue(
                    dispatch.with_context(
                        job_uuid=dispatch.queue_job_uuid
                    )._job_process()
                )
            dispatch.invalidate_recordset()
            inboxes |= inboxes.browse(int(dispatch.result_ref.rsplit(":", 1)[1]))
        for inbox in inboxes:
            with trap_jobs():
                inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
            inbox.invalidate_recordset()
        return inboxes

    def test_signed_post_shows_the_conversation_and_is_idempotent(self):
        message = self.text_message(body="Olá pela Cloud API, ponta a ponta!")
        envelope = self.envelope(self.value(messages=[message]))
        body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()
        first = self._post(body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["duplicate"])
        self.env.invalidate_all()
        delivery = (
            self.env["meta.webhook.delivery"]
            .sudo()
            .search([("public_ref", "=", first.json()["delivery_ref"])])
        )
        self.assertEqual(delivery.item_ids.kind, "messaging")
        self.assertNotIn("ponta a ponta", json.dumps(delivery.sanitized_envelope_json))
        inboxes = self._run_pipeline(delivery)
        self.assertEqual(inboxes.mapped("state"), ["done"])

        api = self.env["contact.center.ui.api"].with_user(self.agent)
        conversations = api.list_conversations()
        binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("external_message_id", "=", message["id"])])
        )
        channel = binding.channel_binding_id.channel_id
        self.assertIn(
            channel.id, [item["channel_id"] for item in conversations["items"]]
        )
        timeline = api.get_timeline(channel.id)
        bodies = json.dumps(timeline, ensure_ascii=False)
        self.assertIn("Olá pela Cloud API, ponta a ponta!", bodies)

        again = self._post(body)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertTrue(again.json()["duplicate"])
        self.env.invalidate_all()
        self.assertEqual(
            self.env["meta.webhook.delivery"]
            .sudo()
            .search_count([("endpoint_id", "=", self.endpoint.id)]),
            1,
        )
        self.assertEqual(
            self.env["contact.center.inbox.event"]
            .sudo()
            .search_count([("provider_connection_id", "=", self.connection.id)]),
            1,
        )
        self.assertEqual(
            self.env["contact.center.message.binding"]
            .sudo()
            .search_count([("external_message_id", "=", message["id"])]),
            1,
        )

    def test_unsigned_post_is_rejected_without_evidence(self):
        body = json.dumps(
            self.envelope(self.value(messages=[self.text_message()]))
        ).encode()
        response = self.opener.post(
            "%s/meta/webhook/%s" % (self.base_url(), self.endpoint.routing_key),
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=%s" % ("0" * 64),
            },
            timeout=30,
        )
        self.assertEqual(response.status_code, 401)
        self.env.invalidate_all()
        self.assertFalse(
            self.env["meta.webhook.delivery"]
            .sudo()
            .search_count([("endpoint_id", "=", self.endpoint.id)])
        )
