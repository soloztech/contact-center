import base64
import hashlib
import hmac
import json
import time
import uuid

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from ..services.tokens import CONTACT_CENTER_POST_TOKEN

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "/x8AAusB9Y9Z4e0AAAAASUVORK5CYII="
)


@tagged("post_install", "-at_install")
class TestContactCenterHttpEndpoints(HttpCase):
    PASSWORD = "contact-center-http-test"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Contact Center HTTP Agent",
                    "login": "cc-http-agent-%s" % uuid.uuid4(),
                    "email": "cc-http-agent@example.invalid",
                    "password": cls.PASSWORD,
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, [agent_group.id])],
                }
            )
        )
        cls.outsider = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Contact Center HTTP Outsider",
                    "login": "cc-http-outsider-%s" % uuid.uuid4(),
                    "email": "cc-http-outsider@example.invalid",
                    "password": cls.PASSWORD,
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, [agent_group.id])],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Contact Center HTTP Team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Contact Center HTTP Account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "cc-http-account-%s" % uuid.uuid4(),
                "default_team_id": cls.team.id,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Contact Center HTTP Connection",
                "account_id": cls.account.id,
                "adapter_key": "test.fake",
                "external_ref": "cc-http-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "http-test-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {
                    "send_message": True,
                    "media": {
                        "image": {
                            "enabled": True,
                            "max_bytes": len(_PNG),
                            "mimetypes": ["image/png"],
                        }
                    },
                },
            }
        )
        guest = cls.env["mail.guest"].sudo().create({"name": "HTTP Remote Guest"})
        identity = (
            cls.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": "HTTP Remote Guest",
                    "company_id": cls.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        cls.channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=cls.account,
            identity=identity,
            team=cls.team,
            partner_ids=cls.agent.partner_id.ids,
            guest_ids=guest.ids,
        )
        cls.binding = (
            cls.env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": cls.channel.id,
                    "account_id": cls.account.id,
                    "identity_id": identity.id,
                    "conversation_type": "direct",
                    "conversation_ref": "cc-http-conversation-%s" % uuid.uuid4(),
                }
            )
        )
        avatar_sha256 = hashlib.sha256(_PNG).hexdigest()
        cls.identity_avatar = (
            cls.env["ir.attachment"]
            .sudo()
            .with_context(image_no_postprocess=True)
            .create(
                {
                    "name": "identity-avatar.png",
                    "type": "binary",
                    "raw": _PNG,
                    "mimetype": "image/png",
                    "res_model": "contact.center.channel.binding",
                    "res_id": cls.binding.id,
                }
            )
        )
        cls.binding.sudo().write(
            {
                "direct_avatar_state": "ready",
                "direct_avatar_attachment_id": cls.identity_avatar.id,
                "direct_avatar_sha256": avatar_sha256,
            }
        )
        cls.identity_avatar_url = (
            "/contact_center/conversation/%s/avatar" % cls.channel.id
        )

        cls.group_channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=cls.account,
            conversation_type="group",
            name="HTTP Group",
            team=cls.team,
            partner_ids=cls.agent.partner_id.ids,
        )
        cls.group_binding = (
            cls.env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": cls.group_channel.id,
                    "account_id": cls.account.id,
                    "conversation_type": "group",
                    "conversation_ref": "cc-http-group-%s" % uuid.uuid4(),
                }
            )
        )
        cls.group_avatar = (
            cls.env["ir.attachment"]
            .sudo()
            .with_context(image_no_postprocess=True)
            .create(
                {
                    "name": "group-avatar.png",
                    "type": "binary",
                    "raw": _PNG,
                    "mimetype": "image/png",
                    "res_model": "contact.center.group.profile",
                    "res_id": 0,
                }
            )
        )
        cls.group_profile = (
            cls.env["contact.center.group.profile"]
            .sudo()
            .create(
                {
                    "channel_binding_id": cls.group_binding.id,
                    "provider_connection_id": cls.connection.id,
                    "name": "HTTP Group",
                    "metadata_state": "ready",
                    "avatar_attachment_id": cls.group_avatar.id,
                    "avatar_sha256": avatar_sha256,
                }
            )
        )
        cls.group_avatar.write({"res_id": cls.group_profile.id})
        cls.group_avatar_url = "/contact_center/group/%s/avatar" % cls.group_channel.id
        message = cls.channel.sudo()._contact_center_post(
            origin="inbound",
            body="HTTP media fixture",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        message_binding = (
            cls.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": cls.binding.id,
                    "provider_connection_id": cls.connection.id,
                    "direction": "inbound",
                    "origin": "provider",
                    "content_type": "image",
                    "external_message_id": "cc-http-message-%s" % uuid.uuid4(),
                    "delivery_state": "delivered",
                }
            )
        )
        attachment = (
            cls.env["ir.attachment"]
            .sudo()
            .with_context(image_no_postprocess=True)
            .create(
                {
                    "name": "pixel.png",
                    "type": "binary",
                    "raw": _PNG,
                    "mimetype": "image/png",
                    "res_model": "mail.message",
                    "res_id": message.id,
                }
            )
        )
        message.sudo().with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).write({"attachment_ids": [(4, attachment.id)]})
        cls.media = (
            cls.env["contact.center.media.binding"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "message_binding_id": message_binding.id,
                    "kind": "image",
                    "mime_type": "image/png",
                    "file_name": "pixel.png",
                    "size_bytes": len(_PNG),
                    "sha256": hashlib.sha256(_PNG).hexdigest(),
                    "state": "ready",
                    "attachment_id": attachment.id,
                }
            )
        )
        cls.media_url = "/contact_center/media/%s/content" % cls.media.id

    def setUp(self):
        super().setUp()
        # This suite shares the transactional test cursor with its HTTP requests.
        # Reject unrelated lab probes that do not carry HttpCase's test cookie;
        # otherwise a concurrent /web/login request can interleave SQL operations
        # on the same cursor and corrupt the following test setup.
        self.http_request_strict_check = True
        self.authenticate(self.agent.login, self.PASSWORD)

    def _csrf_token(self):
        secret = self.env["ir.config_parameter"].sudo().get_param("database.secret")
        self.assertTrue(secret)
        max_ts = int(time.time() + 3600)
        message = "%s%s" % (self.session.sid, max_ts)
        digest = hmac.new(
            secret.encode("ascii"), message.encode("utf-8"), hashlib.sha1
        ).hexdigest()
        return "%so%s" % (digest, max_ts)

    def _json_route(self, route, params):
        response = self.url_open(
            route,
            data=json.dumps(
                {"jsonrpc": "2.0", "method": "call", "params": params, "id": 1}
            ),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotIn("error", payload)
        return payload.get("result")

    def _upload_image(self, client_upload_id):
        return self.url_open(
            "/contact_center/media/upload",
            data={
                "channel_id": str(self.channel.id),
                "client_upload_id": client_upload_id,
                "csrf_token": self._csrf_token(),
            },
            files={"file": ("pixel.png", _PNG, "image/png")},
        )

    def test_native_discuss_and_chat_routes_reject_contact_center_channel(self):
        invitation = self.url_open("/chat/%s/%s" % (self.channel.id, self.channel.uuid))
        self.assertEqual(invitation.status_code, 404)

        public_discuss = self.url_open("/discuss/channel/%s" % self.channel.id)
        self.assertEqual(public_discuss.status_code, 404)

        message_count = (
            self.env["mail.message"]
            .sudo()
            .search_count(
                [("model", "=", "mail.channel"), ("res_id", "=", self.channel.id)]
            )
        )
        self.assertFalse(
            self._json_route(
                "/mail/chat_post",
                {"uuid": self.channel.uuid, "message_content": "must be blocked"},
            )
        )
        self.assertEqual(
            self._json_route(
                "/mail/chat_history", {"uuid": self.channel.uuid, "limit": 20}
            ),
            [],
        )
        self.assertEqual(
            self.env["mail.message"]
            .sudo()
            .search_count(
                [("model", "=", "mail.channel"), ("res_id", "=", self.channel.id)]
            ),
            message_count,
        )

    def test_identity_avatar_etag_revalidation_and_membership_isolation(self):
        response = self.url_open(self.identity_avatar_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, _PNG)
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        etag = response.headers.get("ETag")
        self.assertEqual(etag, '"%s"' % hashlib.sha256(_PNG).hexdigest())

        cached = self.url_open(
            self.identity_avatar_url, headers={"If-None-Match": etag}
        )
        self.assertEqual(cached.status_code, 304)
        self.assertFalse(cached.content)
        self.assertEqual(cached.headers.get("ETag"), etag)

        self.authenticate(self.outsider.login, self.PASSWORD)
        isolated = self.url_open(self.identity_avatar_url)
        self.assertEqual(isolated.status_code, 404)

    def test_group_avatar_etag_revalidation_and_membership_isolation(self):
        response = self.url_open(self.group_avatar_url)
        self.assertEqual(response.status_code, 200)
        etag = response.headers.get("ETag")
        self.assertTrue(etag)
        cached = self.url_open(self.group_avatar_url, headers={"If-None-Match": etag})
        self.assertEqual(cached.status_code, 304)
        self.assertFalse(cached.content)

        self.authenticate(self.outsider.login, self.PASSWORD)
        isolated = self.url_open(self.group_avatar_url)
        self.assertEqual(isolated.status_code, 404)

    def test_media_upload_is_multipart_idempotent_and_rejects_invalid_uuid(self):
        reference = str(uuid.uuid4())
        upload_model = self.env["contact.center.media.upload"].sudo()
        before_count = upload_model.search_count([])

        first = self._upload_image(reference)
        self.assertEqual(first.status_code, 200)
        first_payload = first.json()
        self.assertEqual(first_payload["schema_version"], 1)
        self.assertEqual(first_payload["media_ref"], reference)
        self.assertEqual(
            first_payload["media"],
            {
                "kind": "image",
                "name": "pixel.png",
                "mimetype": "image/png",
                "size_bytes": len(_PNG),
                "is_voice_note": False,
                "duration_seconds": 0,
            },
        )
        upload = upload_model.search([("reference", "=", reference)], limit=1)
        self.assertTrue(upload)
        upload_id = upload.id
        attachment_id = upload.attachment_id.id
        self.assertEqual(upload_model.search_count([]), before_count + 1)

        replay = self._upload_image(reference)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first_payload)
        replayed_upload = upload_model.search([("reference", "=", reference)])
        self.assertEqual(replayed_upload.ids, [upload_id])
        self.assertEqual(replayed_upload.attachment_id.id, attachment_id)
        self.assertEqual(upload_model.search_count([]), before_count + 1)

        invalid = self._upload_image("not-a-uuid")
        self.assertEqual(invalid.status_code, 400)
        invalid_payload = invalid.json()
        self.assertEqual(set(invalid_payload), {"error"})
        self.assertTrue(invalid_payload["error"])
        self.assertEqual(upload_model.search_count([]), before_count + 1)

    def test_media_stream_supports_full_range_suffix_and_open_ranges(self):
        response = self.url_open(self.media_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, _PNG)
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertIn("no-cache", response.headers["Cache-Control"])
        self.assertIn("max-age=0", response.headers["Cache-Control"])
        self.assertIn("must-revalidate", response.headers["Cache-Control"])
        self.assertIn("sandbox", response.headers["Content-Security-Policy"])

        prefix = self.url_open(self.media_url, headers={"Range": "bytes=0-4"})
        self.assertEqual(prefix.status_code, 206)
        self.assertEqual(prefix.content, _PNG[:5])
        self.assertEqual(prefix.headers["Content-Range"], "bytes 0-4/%s" % len(_PNG))

        suffix = self.url_open(self.media_url, headers={"Range": "bytes=-5"})
        self.assertEqual(suffix.status_code, 206)
        self.assertEqual(suffix.content, _PNG[-5:])

        opened = self.url_open(self.media_url, headers={"Range": "bytes=5-"})
        self.assertEqual(opened.status_code, 206)
        self.assertEqual(opened.content, _PNG[5:])

    def test_media_stream_revalidates_etag_and_rejects_invalid_range(self):
        response = self.url_open(self.media_url)
        etag = response.headers.get("ETag")
        self.assertTrue(etag)
        cached = self.url_open(self.media_url, headers={"If-None-Match": etag})
        self.assertEqual(cached.status_code, 304)
        self.assertFalse(cached.content)

        invalid = self.url_open(
            self.media_url,
            headers={"Range": "bytes=%s-" % len(_PNG)},
        )
        self.assertEqual(invalid.status_code, 416)
        self.assertEqual(invalid.headers["Content-Range"], "bytes */%s" % len(_PNG))

    def test_media_download_disposition_and_membership_isolation(self):
        download = self.url_open("%s?download=1" % self.media_url)
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["Content-Disposition"])

        self.authenticate(self.outsider.login, self.PASSWORD)
        forbidden = self.url_open(self.media_url)
        self.assertEqual(forbidden.status_code, 404)

    def test_redacted_deleted_media_is_not_served(self):
        self.assertFalse(self.account.show_deleted_message_content)
        attachment = self.media.attachment_id
        standard_content_url = "/web/content/%s" % attachment.id
        self.assertEqual(self.url_open(standard_content_url).status_code, 200)
        deletion = (
            self.env["contact.center.message.mutation"]
            .sudo()
            .create(
                {
                    "target_message_binding_id": self.media.message_binding_id.id,
                    "provider_connection_id": self.connection.id,
                    "external_event_id": "http-redact-delete-%s" % uuid.uuid4(),
                    "mutation_type": "delete",
                    "direction": "inbound",
                    "actor_partner_id": self.agent.partner_id.id,
                }
            )
        )
        self.assertEqual(deletion.deletion_display_mode, "redact")
        deletion._apply_projection()

        response = self.url_open(self.media_url)
        standard_response = self.url_open(standard_content_url)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(standard_response.status_code, 404)
        self.assertFalse(attachment.exists())

    def test_struck_deleted_media_remains_available_to_authorized_member(self):
        self.account.show_deleted_message_content = True
        deletion = (
            self.env["contact.center.message.mutation"]
            .sudo()
            .create(
                {
                    "target_message_binding_id": self.media.message_binding_id.id,
                    "provider_connection_id": self.connection.id,
                    "external_event_id": "http-strike-delete-%s" % uuid.uuid4(),
                    "mutation_type": "delete",
                    "direction": "inbound",
                    "actor_partner_id": self.agent.partner_id.id,
                }
            )
        )
        self.assertEqual(deletion.deletion_display_mode, "strike")
        deletion._apply_projection()

        response = self.url_open(self.media_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, _PNG)
