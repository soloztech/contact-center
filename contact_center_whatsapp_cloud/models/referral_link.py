"""Private permalink of a click-to-WhatsApp ad, kept only until dispatch (R17).

The shared ledger rejects every URL key, and the thumbnail locator accepts only
CDN images. The public ``source_url`` permalink is therefore held here, keyed by
connection and message, for longer than Meta's 7-day redelivery window. The
consumer resolves it once into the private inbox envelope and deletes the row.
Like ``contact.center.ad.preview.locator`` this model has no ACL: it is reached
only through ``sudo()`` with the module's context token.
"""

import hashlib
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from odoo.addons.contact_center_base.services.ad_origin_preview import (
    clean_text,
    public_source_url,
)

from ..services.contracts import REFERRAL_LINK_RETENTION
from ..services.tokens import (
    CONTACT_CENTER_WHATSAPP_CLOUD_CONTEXT_KEY,
    CONTACT_CENTER_WHATSAPP_CLOUD_INTERNAL_TOKEN,
)

_AUTOVACUUM_LIMIT = 1000


def _internal(records):
    return bool(
        records.env.su
        and records.env.context.get(CONTACT_CENTER_WHATSAPP_CLOUD_CONTEXT_KEY)
        is CONTACT_CENTER_WHATSAPP_CLOUD_INTERNAL_TOKEN
    )


def _owned(records):
    return records.sudo().with_context(
        **{
            CONTACT_CENTER_WHATSAPP_CLOUD_CONTEXT_KEY: (
                CONTACT_CENTER_WHATSAPP_CLOUD_INTERNAL_TOKEN
            )
        }
    )


class ContactCenterWhatsAppCloudReferralLink(models.Model):
    _name = "contact.center.whatsapp.cloud.referral.link"
    _description = "WhatsApp Cloud Ad Referral Link"
    _order = "id"

    reference = fields.Char(
        required=True, default=lambda self: str(uuid.uuid4()), index=True, copy=False
    )
    connection_id = fields.Many2one(
        "contact.center.provider.connection",
        required=True,
        index=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        related="connection_id.company_id", store=True, index=True
    )
    source_key = fields.Char(required=True, index=True)
    url_hash = fields.Char(required=True, index=True)
    source_url = fields.Char(required=True, copy=False)
    expires_at = fields.Datetime(
        required=True,
        default=lambda self: fields.Datetime.now() + REFERRAL_LINK_RETENTION,
        index=True,
    )

    _sql_constraints = [
        (
            "reference_unique",
            "unique(reference)",
            "The referral link reference must be unique.",
        ),
        (
            "source_url_unique",
            "unique(connection_id, source_key, url_hash)",
            "This referral link already exists.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_("WhatsApp referral links are managed internally."))
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("WhatsApp referral links are immutable."))

    def unlink(self):
        if not _internal(self):
            raise AccessError(_("WhatsApp referral links are managed internally."))
        return super().unlink()

    @api.model
    def _register_source_link(self, connection, source_key, url):
        """Store one validated public permalink and return its opaque reference."""

        connection.ensure_one()
        url = public_source_url(url)
        if not url or not source_key or clean_text(source_key, 512) != source_key:
            return ""
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ["cc:wac-referral-link:%s:%s:%s" % (connection.id, source_key, digest)],
        )
        existing = self.sudo().search(
            [
                ("connection_id", "=", connection.id),
                ("source_key", "=", source_key),
                ("url_hash", "=", digest),
            ],
            limit=1,
        )
        if existing:
            return existing.reference
        return (
            _owned(self)
            .create(
                {
                    "connection_id": connection.id,
                    "source_key": source_key,
                    "url_hash": digest,
                    "source_url": url,
                }
            )
            .reference
        )

    @api.model
    def _route_domain(self, connection):
        """Rows of the dispatching connection's stable route (CC-WAC-17).

        A link is registered by the connection that received the message; a
        replacement of that connection in the same inbox, business account and
        number may dispatch it. Another inbox's or number's rows never match.
        """

        return [("connection_id", "in", connection._wac_stable_route_connections().ids)]

    @api.model
    def _resolve_source_link(self, reference, connection, source_key):
        """Return the validated permalink, or ``""`` when absent or expired."""

        if not isinstance(reference, str) or not reference or not source_key:
            return ""
        record = self.sudo().search(
            [
                ("reference", "=", reference),
                ("source_key", "=", source_key),
            ]
            + self._route_domain(connection),
            limit=1,
        )
        if not record or record.expires_at <= fields.Datetime.now():
            return ""
        return public_source_url(record.source_url)

    @api.model
    def _discard_source_link(self, reference, connection, source_key):
        if not isinstance(reference, str) or not reference:
            return True
        _owned(self).search(
            [
                ("reference", "=", reference),
                ("source_key", "=", source_key),
            ]
            + self._route_domain(connection)
        ).unlink()
        return True

    @api.model
    def _purge_source_keys(self, connections, source_keys):
        """Privacy hook: delete the links of an erased conversation."""

        source_keys = sorted({key for key in source_keys if key})
        if not connections or not source_keys:
            return True
        _owned(self).search(
            [
                ("connection_id", "in", connections.ids),
                ("source_key", "in", source_keys),
            ]
        ).unlink()
        return True

    @api.autovacuum
    def _gc_expired_links(self):
        _owned(self).search(
            [("expires_at", "<=", fields.Datetime.now())], limit=_AUTOVACUUM_LIMIT
        ).unlink()
        return True
