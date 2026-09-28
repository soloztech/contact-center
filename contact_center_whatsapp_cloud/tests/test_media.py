import hashlib
import uuid
from unittest import mock

from odoo.exceptions import UserError

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    TransientAdapterError,
)
from odoo.addons.contact_center_base.services.dto import CommandDTO, MediaDTO
from odoo.addons.contact_center_base.services.media import (
    validate_provider_media_capability,
)
from odoo.addons.queue_job.tests.common import trap_jobs

from .common import GRAPH_PATCH, FakeResponse, WhatsAppCloudCase
from .test_outbound import WhatsAppCloudMediaMixin
from .test_status import WhatsAppCloudOutboundMixin

MiB = 1024 * 1024
MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1&hash=x"


class TestWhatsAppCloudMedia(
    WhatsAppCloudMediaMixin, WhatsAppCloudOutboundMixin, WhatsAppCloudCase
):
    MEDIA_ID = "900000000000777"

    def _media(self, content, *, kind="image", mime_type="image/jpeg", sha256=None):
        return MediaDTO(
            kind=kind,
            external_media_id=self.MEDIA_ID,
            remote_locator={"media_id": self.MEDIA_ID},
            mime_type=mime_type,
            sha256=hashlib.sha256(content).hexdigest() if sha256 is None else sha256,
        )

    def _resolution(self, content, *, url=MEDIA_URL, mime_type="image/jpeg"):
        return FakeResponse(
            {
                "messaging_product": "whatsapp",
                "url": url,
                "mime_type": mime_type,
                "sha256": hashlib.sha256(content).hexdigest(),
                "file_size": len(content),
                "id": self.MEDIA_ID,
            }
        )

    @staticmethod
    def _binary(content, mime_type="image/jpeg", status=200):
        return FakeResponse(
            status=status, content=content, headers={"Content-Type": mime_type}
        )

    def _download(self, media, responses):
        with mock.patch(GRAPH_PATCH, side_effect=responses) as request:
            try:
                result = self.connection.get_adapter().download_media(
                    self.connection, media
                )
            finally:
                calls = list(request.call_args_list)
        return result, calls

    # -- 9: resolution, Bearer download, hosts, checksum, expiry ------------------

    def test_media_id_is_resolved_and_downloaded_with_bearer(self):
        content = b"\xff\xd8\xff synthetic whatsapp image"
        result, calls = self._download(
            self._media(content), [self._resolution(content), self._binary(content)]
        )
        self.assertEqual(result.content, content)
        self.assertEqual(result.mime_type, "image/jpeg")
        resolve, download = calls
        self.assertEqual(
            (resolve.args[0], resolve.args[1]),
            ("GET", "https://graph.facebook.com/v26.0/%s" % self.MEDIA_ID),
        )
        self.assertEqual(download.args, ("GET", MEDIA_URL))
        self.assertEqual(
            download.kwargs["headers"]["Authorization"], "Bearer %s" % self.WABA_TOKEN
        )
        self.assertFalse(download.kwargs["allow_redirects"])

    def test_inbound_media_job_stores_the_verified_attachment(self):
        content = b"\xff\xd8\xff inbound image via core job"
        message = self.typed_message(
            "image",
            {
                "id": self.MEDIA_ID,
                "mime_type": "image/jpeg",
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        )
        self.deliver(self.envelope(self.value(messages=[message])))
        media = self.binding_for(message["id"]).media_ids.ensure_one().sudo()
        media.write({"queue_job_uuid": str(uuid.uuid4())})
        with mock.patch(
            GRAPH_PATCH, side_effect=[self._resolution(content), self._binary(content)]
        ), trap_jobs():
            media.with_context(job_uuid=media.queue_job_uuid)._job_download()
        self.assertEqual(media.state, "ready")
        self.assertEqual(media.attachment_id.raw, content)

    def test_host_outside_the_allow_list_is_refused_before_download(self):
        content = b"image"
        with self.assertRaisesRegex(AdapterError, "host is not allowed"):
            self._download(
                self._media(content),
                [self._resolution(content, url="https://evil.example.invalid/x")],
            )

    def test_divergent_sha256_is_refused(self):
        content = b"real bytes"
        with self.assertRaisesRegex(AdapterError, "checksum"):
            self._download(
                self._media(content, sha256="0" * 64),
                [self._resolution(content), self._binary(content)],
            )

    def test_expired_url_is_resolved_again_by_media_id(self):
        content = b"\xff\xd8\xff second try"
        result, calls = self._download(
            self._media(content),
            [
                self._resolution(content),
                self._binary(b"", status=404),
                self._resolution(content),
                self._binary(content),
            ],
        )
        self.assertEqual(result.content, content)
        self.assertEqual(len(calls), 4)

    def test_missing_media_after_retention_is_permanent(self):
        gone = FakeResponse(
            {"error": {"code": 100, "message": "Unsupported get request."}},
            status=404,
        )
        with self.assertRaises(AdapterError) as raised:
            self._download(self._media(b"x"), [gone])
        self.assertNotIsInstance(raised.exception, TransientAdapterError)
        with self.assertRaises(TransientAdapterError):
            self._download(self._media(b"x"), [FakeResponse({}, status=503)])

    # -- 9: limits at the boundary through the real client ------------------------

    def test_receive_document_up_to_the_core_limit(self):
        content = b"%PDF" + b"0" * (50 * MiB - 4)
        media = self._media(content, kind="document", mime_type="application/pdf")
        result, _calls = self._download(
            media,
            [
                self._resolution(content, mime_type="application/pdf"),
                self._binary(content, "application/pdf"),
            ],
        )
        self.assertEqual(result.size_bytes, 50 * MiB)
        oversized = content + b"0"
        with self.assertRaisesRegex(AdapterError, "size limit"):
            self._download(
                self._media(oversized, kind="document", mime_type="application/pdf"),
                [
                    self._resolution(oversized, mime_type="application/pdf"),
                    self._binary(oversized, "application/pdf"),
                ],
            )

    def _document_outbox(self, size):
        channel, inbound = self._conversation()
        content = b"%PDF" + b"1" * (size - 4)
        upload = self._upload(
            inbound.channel_binding_id,
            content,
            "application/pdf",
            "document",
            "catalogo.pdf",
        )
        with trap_jobs():
            result = (
                self.env["contact.center.ui.api"]
                .with_user(self.agent)
                .send_message(channel.id, "", media_refs=[upload.reference])
            )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("message_binding_id.message_id", "=", result["message_id"])])
        )
        return outbox, content

    def test_send_document_at_25_mib_is_uploaded_by_the_shared_client(self):
        outbox, content = self._document_outbox(25 * MiB)
        _connection, command, adapter, _snapshot = outbox._prepare_dispatch()
        with mock.patch(
            GRAPH_PATCH,
            side_effect=[
                FakeResponse({"id": "123456789012345"}),
                self._accepted(self.wamid("doc")),
            ],
        ) as request:
            result = adapter.execute_command(self.connection, command)
        self.assertEqual(result.status, "success")
        _name, uploaded, _mime = request.call_args_list[0].kwargs["files"]["file"]
        self.assertEqual(len(uploaded), 25 * MiB)
        self.assertEqual(uploaded, content)

    def test_send_document_over_25_mib_is_refused_before_network(self):
        capabilities = self.connection.capabilities_json
        with self.assertRaises(UserError):
            validate_provider_media_capability(
                capabilities, "document", "application/pdf", 25 * MiB + 1
            )
        outbox, _content = self._document_outbox(1024)
        command = CommandDTO.from_dict(outbox.command_json)
        oversized = dict(command.message.media[0].to_dict(), size_bytes=25 * MiB + 1)
        forged = CommandDTO.from_dict(
            dict(
                command.to_dict(),
                message=dict(command.message.to_dict(), media=[oversized]),
            )
        )
        with mock.patch(GRAPH_PATCH) as request:
            with self.assertRaisesRegex(AdapterError, "size limit"):
                self.connection.get_adapter().execute_command(self.connection, forged)
        request.assert_not_called()

    def test_send_image_limit_is_five_megabytes(self):
        capabilities = self.connection.capabilities_json
        self.assertEqual(capabilities["media"]["image"]["max_bytes"], 5_000_000)
        self.assertEqual(
            validate_provider_media_capability(
                capabilities, "image", "image/jpeg", 5_000_000
            ),
            "image/jpeg",
        )
        with self.assertRaises(UserError):
            validate_provider_media_capability(
                capabilities, "image", "image/jpeg", 5_000_001
            )
        self.assertEqual(capabilities["media"]["document"]["max_bytes"], 25 * MiB)
        self.assertEqual(capabilities["media"]["video"]["max_bytes"], 16_000_000)
        self.assertEqual(capabilities["media"]["audio"]["max_bytes"], 16_000_000)
        extension = capabilities["extensions"]["provider.whatsapp_cloud"]
        self.assertEqual(extension["window_hours"], 24)
        self.assertEqual(
            extension["sticker_max_bytes"], {"static": 100_000, "animated": 500_000}
        )
        self.assertEqual(extension["inbound_media_max_bytes"]["document"], 50 * MiB)
        self.assertFalse(capabilities["media"]["audio"]["caption"])
        self.assertFalse(capabilities["edit_message"])
        self.assertFalse(capabilities["delete_message"])
        self.assertTrue(capabilities["mark_read"])
        self.assertTrue(capabilities["react"])
