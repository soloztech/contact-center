from unittest import mock

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    TransientAdapterError,
)
from odoo.addons.contact_center_base.services.dto import MediaDTO
from odoo.addons.meta_webhook_base.services.tokens import META_WEBHOOK_INTERNAL_TOKEN
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.media import _request_media
from .common import MetaCase

REQUEST_PATCH = "odoo.addons.contact_center_meta.services.media.requests.request"


class FakeMediaResponse:
    def __init__(self, status_code=200, *, content=b"image", headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._content = content
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class TestMetaPrivateMedia(MetaCase):
    def test_malformed_media_url_is_classified_before_network(self):
        with mock.patch(REQUEST_PATCH) as request_mock, self.assertRaises(AdapterError):
            _request_media("https://[invalid")
        request_mock.assert_not_called()

    def test_temporary_http_errors_keep_profile_download_retryable(self):
        from ..services.media import download_profile_avatar

        for status in (408, 425, 429, 503):
            response = FakeMediaResponse(status_code=status)
            with self.subTest(status=status), mock.patch(
                REQUEST_PATCH, return_value=response
            ), self.assertRaises(TransientAdapterError):
                download_profile_avatar("https://lookaside.fbsbx.com/avatar")
            self.assertTrue(response.closed)

    def _media_envelope(self, url):
        return {
            "object": "page",
            "entry": [
                {
                    "id": self.ACTIVE_PAGE_ID,
                    "time": 1787605000000,
                    "messaging": [
                        {
                            "sender": {"id": "900000000000050"},
                            "recipient": {"id": self.ACTIVE_PAGE_ID},
                            "timestamp": 1787605000000,
                            "message": {
                                "mid": "m_phase63_private_media",
                                "attachments": [
                                    {"type": "image", "payload": {"url": url}}
                                ],
                            },
                        }
                    ],
                }
            ],
        }

    def _routed_locator(self, url="https://lookaside.fbsbx.com/media?token=secret"):
        delivery = self.create_delivery(self._media_envelope(url))
        internal = delivery.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        with trap_jobs():
            internal._fanout_once()
        dispatch = delivery.dispatch_ids.ensure_one()
        result = self.env["meta.webhook.dispatcher"]._dispatch_consumer(dispatch)
        self.assertTrue(result["handled"])
        locator = (
            self.env["contact.center.meta.media.locator"]
            .sudo()
            .search([("meta_delivery_id", "=", delivery.id)])
        )
        return delivery, locator.ensure_one()

    @staticmethod
    def _dto(locator):
        return MediaDTO(
            kind="image",
            external_media_id="m_phase63_private_media:0",
            remote_locator={"private_locator_ref": locator.reference},
            mime_type="image/png",
            file_name="phase63.png",
        )

    def test_download_uses_private_vault_and_terminal_success_clears_url(self):
        _delivery, locator = self._routed_locator()
        content = b"\x89PNG\r\n\x1a\nphase63"
        response = FakeMediaResponse(
            content=content,
            headers={
                "Content-Type": "image/png",
                "Content-Length": str(len(content)),
            },
        )

        with mock.patch(REQUEST_PATCH, return_value=response) as request_mock:
            result = self.connection.get_adapter().download_media(
                self.connection, self._dto(locator)
            )

        self.assertEqual(result.content, content)
        self.assertEqual(result.mime_type, "image/png")
        self.assertEqual(result.file_name, "phase63.png")
        self.assertTrue(response.closed)
        request_mock.assert_called_once()
        self.assertNotIn("Authorization", request_mock.call_args.kwargs["headers"])

        self.connection.get_adapter().finalize_media_download(
            self.connection,
            self._dto(locator),
            succeeded=True,
        )
        locator.invalidate_recordset(["state", "download_url", "consumed_at"])
        self.assertEqual(locator.state, "consumed")
        self.assertFalse(locator.download_url)
        self.assertTrue(locator.consumed_at)

        # A repeated terminal callback is the expected idempotent not-found case.
        self.connection.get_adapter().finalize_media_download(
            self.connection,
            self._dto(locator),
            succeeded=True,
        )

    def test_finalize_does_not_hide_unexpected_model_failures(self):
        _delivery, locator = self._routed_locator()
        model_class = type(self.env["contact.center.meta.media.locator"])

        with mock.patch.object(
            model_class,
            "_resolve_for_download",
            side_effect=RuntimeError("synthetic database failure"),
        ), self.assertRaisesRegex(RuntimeError, "synthetic database failure"):
            self.connection.get_adapter().finalize_media_download(
                self.connection,
                self._dto(locator),
                succeeded=True,
            )

    def test_non_meta_host_is_rejected_before_network(self):
        _delivery, locator = self._routed_locator(
            "https://cdn.example.invalid/private?token=secret"
        )

        with mock.patch(REQUEST_PATCH) as request_mock, self.assertRaises(AdapterError):
            self.connection.get_adapter().download_media(
                self.connection, self._dto(locator)
            )

        request_mock.assert_not_called()
        self.assertEqual(locator.state, "active")
        self.assertTrue(locator.download_url)

    def test_transient_response_keeps_locator_for_queue_retry(self):
        _delivery, locator = self._routed_locator()
        response = FakeMediaResponse(
            status_code=503,
            headers={"Retry-After": "17"},
        )

        with mock.patch(REQUEST_PATCH, return_value=response), self.assertRaises(
            TransientAdapterError
        ) as caught:
            self.connection.get_adapter().download_media(
                self.connection, self._dto(locator)
            )

        self.assertEqual(caught.exception.retry_after_seconds, 17)
        self.assertTrue(response.closed)
        self.assertEqual(locator.state, "active")
        self.assertTrue(locator.download_url)

    def test_locator_cannot_cross_provider_connections(self):
        _delivery, locator = self._routed_locator()
        other_page_id = self._numeric_id()
        other_page = self._create_page(self.endpoint, other_page_id)
        other_asset = other_page.asset_ids.ensure_one()
        other_account = self._create_account(
            "messenger",
            other_page_id,
            team=self.team,
        )
        other_connection = self._create_connection(
            other_account,
            other_asset,
        )

        with mock.patch(REQUEST_PATCH) as request_mock, self.assertRaises(AdapterError):
            other_connection.get_adapter().download_media(
                other_connection, self._dto(locator)
            )

        request_mock.assert_not_called()
