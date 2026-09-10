import socket
import uuid
from unittest.mock import Mock, patch

import requests

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..services.link_preview import (
    PublicPreviewSession,
    preview_urls,
    public_url_target,
)
from ..services.tokens import CONTACT_CENTER_POST_TOKEN

SERVICE = "odoo.addons.contact_center_base.services.link_preview"
MODEL = "odoo.addons.contact_center_base.models.link_preview"


class TestContactCenterLinkPreview(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.users = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                [
                    {
                        "name": "Preview agent %s" % index,
                        "login": "preview-%s" % uuid.uuid4(),
                        "company_id": cls.env.company.id,
                        "company_ids": [fields.Command.set(cls.env.company.ids)],
                        "groups_id": [fields.Command.set(group.ids)],
                    }
                    for index in range(2)
                ]
            )
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Preview inbox",
                "platform": "whatsapp",
                "company_id": cls.env.company.id,
                "access_user_ids": [fields.Command.set(cls.users[:1].ids)],
            }
        )
        guest = cls.env["mail.guest"].create({"name": "Preview guest"})
        identity = cls.env["contact.center.identity"].create(
            {
                "name": guest.name,
                "mail_guest_id": guest.id,
                "company_id": cls.env.company.id,
            }
        )
        cls.channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=cls.account,
            identity=identity,
            guest_ids=guest.ids,
        )
        cls.binding = cls.env["contact.center.channel.binding"].create(
            {
                "channel_id": cls.channel.id,
                "account_id": cls.account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": "preview-%s" % uuid.uuid4(),
            }
        )
        cls.message = cls.channel._contact_center_post(
            origin="inbound",
            body="See https://example.com/product",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
        )
        cls.card = cls.env["contact.center.message.binding"].create(
            {
                "message_id": cls.message.id,
                "channel_binding_id": cls.binding.id,
                "direction": "inbound",
                "origin": "provider",
                "content_type": "text",
                "external_message_id": "preview-%s" % uuid.uuid4(),
            }
        )

    def test_native_metadata_is_idempotent_and_does_not_send_messages(self):
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.encoding = "utf-8"
        response._content = (
            b'<html><meta property="og:title" content="Product">'
            b'<meta property="og:description" content="Details"></html>'
        )
        session = Mock()
        session.head.return_value = response
        session.get.return_value = response
        outbox_count = self.env["contact.center.outbox.command"].search_count([])
        body_hash = self.message._contact_center_preview_hash()
        with patch(MODEL + ".PublicPreviewSession", return_value=session):
            self.assertTrue(self.message._job_contact_center_link_previews(body_hash))
            self.assertFalse(self.message._job_contact_center_link_previews(body_hash))
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(len(self.message.link_preview_ids), 1)
        self.assertEqual(self.message.link_preview_ids.og_title, "Product")
        self.assertEqual(
            self.env["contact.center.outbox.command"].search_count([]), outbox_count
        )
        with patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Serialization performed HTTP"),
        ):
            values = (
                self.env["contact.center.ui.api"]
                .with_user(self.users[0])
                ._serialize_message(self.message, self.card)
            )
        self.assertEqual(values["link_previews"][0]["og_title"], "Product")
        self.assertTrue(values["link_preview_checked"])

    def test_changed_deleted_and_disabled_messages_do_not_fetch(self):
        body_hash = self.message._contact_center_preview_hash()
        self.message.with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).write({"body": "Changed"})
        with patch(MODEL + ".PublicPreviewSession") as transport:
            self.assertFalse(self.message._job_contact_center_link_previews(body_hash))
            self.env["ir.config_parameter"].sudo().set_param(
                "mail.link_preview_throttle", "0"
            )
            self.assertFalse(self.message._contact_center_request_link_previews())
            self.env["ir.config_parameter"].sudo().set_param(
                "mail.link_preview_throttle", "99"
            )
            self.card.write({"message_state": "deleted"})
            self.assertFalse(
                self.message._job_contact_center_link_previews(
                    self.message._contact_center_preview_hash()
                )
            )
            transport.assert_not_called()

    def test_request_checks_channel_scope_before_queueing(self):
        api = self.env["contact.center.ui.api"]
        with self.assertRaises(AccessError):
            api.with_user(self.users[1]).request_link_previews(
                self.channel.id, self.message.id
            )
        unrelated = self.env["mail.message"].create(
            {"body": "Not in this conversation"}
        )
        with self.assertRaises(ValidationError):
            api.with_user(self.users[0]).request_link_previews(
                self.channel.id, unrelated.id
            )
        with patch.object(type(self.message), "with_delay") as delay:
            result = api.with_user(self.users[0]).request_link_previews(
                self.channel.id, self.message.id
            )
            self.assertTrue(result["requested"])
            delay.assert_called_once()

    def test_unavailable_preview_is_checked_once_without_failure(self):
        with patch(MODEL + ".PublicPreviewSession") as factory:
            factory.return_value.head.side_effect = requests.Timeout()
            self.assertTrue(
                self.message._job_contact_center_link_previews(
                    self.message._contact_center_preview_hash()
                )
            )
        self.assertFalse(self.message.link_preview_ids)
        self.assertFalse(self.message._contact_center_request_link_previews())

    def test_public_transport_rejects_local_and_mixed_dns_answers(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "224.0.0.1"):
            with self.subTest(address=address), patch(
                SERVICE + ".socket.getaddrinfo",
                return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
                ],
            ):
                with self.assertRaises(requests.RequestException):
                    public_url_target("https://example.com")
        for url in (
            "file:///etc/passwd",
            "http://user:secret@example.com",
            "http://example.com:8069",
        ):
            with self.assertRaises(requests.RequestException):
                public_url_target(url)
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
            for ip in ("93.184.216.34", "192.168.1.1")
        ]
        with patch(SERVICE + ".socket.getaddrinfo", return_value=addresses):
            with self.assertRaises(requests.RequestException):
                public_url_target("https://example.com")

    def test_public_transport_pins_dns_and_rejects_private_redirect(self):
        response = Mock(status=302, headers={"Location": "http://127.0.0.1/private"})
        with patch(
            SERVICE + ".socket.getaddrinfo",
            side_effect=[
                [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
                [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))],
            ],
        ), patch(SERVICE + ".urllib3.HTTPSConnectionPool") as pool:
            pool.return_value.urlopen.return_value = response
            with self.assertRaises(requests.RequestException):
                PublicPreviewSession().head("https://example.com")
            self.assertEqual(pool.call_args.args[0], "93.184.216.34")
            self.assertEqual(pool.call_args.kwargs["assert_hostname"], "example.com")
            self.assertEqual(pool.call_args.kwargs["server_hostname"], "example.com")
        self.assertEqual(
            preview_urls("https://example.com https://example.com"),
            ["https://example.com"],
        )

    def test_public_transport_refuses_compression_before_reading_body(self):
        response = Mock(status=200, headers={"Content-Encoding": "gzip"})
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]
        with patch(SERVICE + ".socket.getaddrinfo", return_value=addresses), patch(
            SERVICE + ".urllib3.HTTPSConnectionPool"
        ) as pool:
            pool.return_value.urlopen.return_value = response
            with self.assertRaises(requests.RequestException):
                PublicPreviewSession().get("https://example.com")
            response.stream.assert_not_called()
