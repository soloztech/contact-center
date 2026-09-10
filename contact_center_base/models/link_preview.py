"""Native link preview records shared with the Contact Center timeline."""

import hashlib
import logging
from urllib.parse import urljoin

import requests
from lxml.etree import LxmlError

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import html2plaintext

from ..services.dto import SCHEMA_VERSION
from ..services.link_preview import (
    PublicPreviewSession,
    preview_urls,
    public_url_target,
)
from ..services.tokens import CONTACT_CENTER_POST_TOKEN

_logger = logging.getLogger(__name__)


class MailMessage(models.Model):
    _inherit = "mail.message"

    contact_center_link_preview_body_hash = fields.Char(readonly=True, copy=False)

    def _contact_center_preview_hash(self):
        self.ensure_one()
        return hashlib.sha256((self.body or "").encode()).hexdigest()

    def _contact_center_preview_binding(self):
        self.ensure_one()
        return (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("message_id", "=", self.id),
                    ("message_state", "!=", "deleted"),
                    ("channel_binding_id.active", "=", True),
                    ("channel_binding_id.channel_id.active", "=", True),
                ],
                limit=1,
            )
        )

    def _contact_center_request_link_previews(self):
        self.ensure_one()
        if (
            not self._contact_center_preview_binding()
            or not self.env["mail.link.preview"]._is_link_preview_enabled()
            or self.contact_center_link_preview_body_hash
            == self._contact_center_preview_hash()
            or not preview_urls(html2plaintext(self.body or ""))
        ):
            return False
        body_hash = self._contact_center_preview_hash()
        self.sudo().with_delay(
            identity_key="contact_center.link_preview:%s:%s" % (self.id, body_hash),
            priority=50,
            description="Contact Center link preview %s" % self.id,
        )._job_contact_center_link_previews(body_hash)
        return True

    def _job_contact_center_link_previews(self, body_hash):
        self = self.exists()
        if not self:
            return False
        self.ensure_one()
        preview_model = self.env["mail.link.preview"].sudo()
        if (
            not self._contact_center_preview_binding()
            or not preview_model._is_link_preview_enabled()
            or self._contact_center_preview_hash() != body_hash
            or self.contact_center_link_preview_body_hash == body_hash
        ):
            return False
        values = []
        for url in preview_urls(html2plaintext(self.body or "")):
            try:
                if preview_model._is_domain_throttled(url):
                    continue
                session = PublicPreviewSession()
                value = preview_model._get_link_preview_from_url(url, session)
                if value:
                    if value.get("og_image"):
                        value["og_image"] = urljoin(
                            session.last_url or url, value["og_image"]
                        )
                        try:
                            public_url_target(value["og_image"])
                        except requests.RequestException:
                            value["og_image"] = False
                    values.append(dict(value, message_id=self.id))
            except (requests.RequestException, LxmlError, ValueError):
                _logger.debug(
                    "No link preview available for Contact Center message %s", self.id
                )
        # No lock is held during HTTP. Concurrent edits/deletion/duplicate jobs
        # are rechecked before committing native preview rows.
        self.env.cr.execute(
            "SELECT id FROM mail_message WHERE id = %s FOR UPDATE", [self.id]
        )
        if not self.env.cr.fetchone():
            return False
        self.invalidate_recordset(["body", "contact_center_link_preview_body_hash"])
        if (
            not self._contact_center_preview_binding()
            or self._contact_center_preview_hash() != body_hash
            or self.contact_center_link_preview_body_hash == body_hash
        ):
            return False
        existing = preview_model.search([("message_id", "=", self.id)])
        # Preview rows are derived metadata, never message content.
        existing.unlink()
        if values:
            preview_model.create(values)
        self.with_context(contact_center_post_token=CONTACT_CENTER_POST_TOKEN).write(
            {"contact_center_link_preview_body_hash": body_hash}
        )
        channel = self.env["mail.channel"].browse(self.res_id)
        self.env["contact.center.application"]._notify_ui(
            channel, "message_updated", {"message_id": self.id}
        )
        return True


class ContactCenterApplication(models.AbstractModel):
    _inherit = "contact.center.application"

    def _publish_message_created(self, channel, message, direction=None):
        result = super()._publish_message_created(channel, message, direction=direction)
        message._contact_center_request_link_previews()
        return result


class ContactCenterMessageMutation(models.Model):
    _inherit = "contact.center.message.mutation"

    def _purge_redacted_operational_content(self, target):
        result = super()._purge_redacted_operational_content(target)
        # Derived native metadata must follow the same deletion policy as the
        # message body, including readers outside the custom timeline.
        message = target.message_id.sudo()
        message.link_preview_ids.unlink()
        if message.contact_center_link_preview_body_hash:
            message.with_context(
                contact_center_post_token=CONTACT_CENTER_POST_TOKEN
            ).write({"contact_center_link_preview_body_hash": False})
        return result


class ContactCenterUiApi(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    def _serialize_message(self, message, *args, **kwargs):
        result = super()._serialize_message(message, *args, **kwargs)
        result["channel_id"] = message.res_id
        result["link_preview_checked"] = (
            message.contact_center_link_preview_body_hash
            == message._contact_center_preview_hash()
        )
        result["link_previews"] = []
        if not result["is_deleted"]:
            previews = message.sudo().link_preview_ids.sorted("id")[:3]
            current_urls = preview_urls(html2plaintext(message.body or ""))
            result["link_previews"] = previews.filtered(
                lambda preview: preview.source_url in current_urls
            )._link_preview_format()
        return result

    @api.model
    def request_link_previews(self, channel_id, message_id):
        channel, _member = self._authorized_channel(channel_id)
        message = self.env["mail.message"].search(
            [
                ("id", "=", self._positive_id(message_id, _("message ID"))),
                ("model", "=", "mail.channel"),
                ("res_id", "=", channel.id),
            ],
            limit=1,
        )
        if not message:
            raise ValidationError(
                _("The message does not belong to this conversation.")
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "requested": message._contact_center_request_link_previews(),
        }
