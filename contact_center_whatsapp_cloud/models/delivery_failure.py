"""Local diagnostics the core delivery model cannot represent (R11, R18).

``failed`` is not a core delivery state and WABA ``errors[]`` belong to no
conversation. Both are recorded here: read-only for Contact Center
administrators within their companies, created only by this module's service.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from ..services.tokens import (
    CONTACT_CENTER_WHATSAPP_CLOUD_CONTEXT_KEY,
    CONTACT_CENTER_WHATSAPP_CLOUD_INTERNAL_TOKEN,
)


def _internal(records):
    return bool(
        records.env.su
        and records.env.context.get(CONTACT_CENTER_WHATSAPP_CLOUD_CONTEXT_KEY)
        is CONTACT_CENTER_WHATSAPP_CLOUD_INTERNAL_TOKEN
    )


class ContactCenterWhatsAppCloudDeliveryFailure(models.Model):
    _name = "contact.center.whatsapp.cloud.delivery.failure"
    _description = "WhatsApp Cloud Delivery Failure"
    _order = "occurred_at desc, id desc"
    _rec_name = "occurrence_sha256"

    kind = fields.Selection(
        [
            ("status_failed", "Failed Delivery Status"),
            ("webhook_error", "Business Account Webhook Error"),
        ],
        required=True,
        readonly=True,
        index=True,
    )
    connection_id = fields.Many2one(
        "contact.center.provider.connection",
        required=True,
        readonly=True,
        index=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        related="connection_id.company_id", store=True, readonly=True, index=True
    )
    account_id = fields.Many2one(
        related="connection_id.account_id", store=True, readonly=True, index=True
    )
    code = fields.Integer(readonly=True)
    title = fields.Char(readonly=True, size=256)
    occurred_at = fields.Datetime(readonly=True, required=True, index=True)
    wamid = fields.Char(string="WhatsApp Message ID", readonly=True, index=True)
    message_binding_id = fields.Many2one(
        "contact.center.message.binding",
        readonly=True,
        index=True,
        ondelete="set null",
    )
    meta_delivery_ref = fields.Char(readonly=True, size=36)
    occurrence_sha256 = fields.Char(required=True, readonly=True, size=64, index=True)

    _sql_constraints = [
        (
            "occ_unique",
            "unique(connection_id, occurrence_sha256)",
            "This WhatsApp failure occurrence is already recorded.",
        ),
        (
            "kind_shape",
            "check((kind = 'status_failed' AND wamid IS NOT NULL) OR "
            "(kind = 'webhook_error' AND wamid IS NULL "
            "AND message_binding_id IS NULL))",
            "The WhatsApp failure record shape is invalid.",
        ),
        (
            "occ_digest",
            "check(char_length(occurrence_sha256) = 64)",
            "The WhatsApp failure occurrence must be a SHA-256 digest.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_("WhatsApp failures are recorded by the service only."))
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("WhatsApp failure records are read-only."))

    def unlink(self):
        if not _internal(self):
            raise AccessError(_("WhatsApp failure records are read-only."))
        return super().unlink()

    @api.model
    def _record_occurrence(self, values, *, stable_route=False):
        """Idempotently record one failure occurrence and return it.

        A ``failed`` status names one message of a stable route: Meta may
        repeat it after the receiving connection was replaced, so with
        ``stable_route`` the occurrence is looked up, and its lock taken, on
        the whole route (inbox, business account and number) instead of the
        connection alone (CC-WAC-20). A webhook error keeps its own identity,
        already bound to its connection and delivery.
        """

        values = dict(values)
        connection_id = values["connection_id"]
        occurrence = values["occurrence_sha256"]
        connection = (
            self.env["contact.center.provider.connection"].sudo().browse(connection_id)
        )
        if stable_route:
            scope = connection._wac_stable_route_connections()
            lock_key = "cc:wac-failure:route:%s:%s:%s:%s" % (
                connection.account_id.id,
                connection.wa_business_account_id,
                connection.wa_phone_number_id,
                occurrence,
            )
        else:
            scope = connection
            lock_key = "cc:wac-failure:%s:%s" % (connection_id, occurrence)
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
        )
        owned = self.sudo().with_context(
            **{
                CONTACT_CENTER_WHATSAPP_CLOUD_CONTEXT_KEY: (
                    CONTACT_CENTER_WHATSAPP_CLOUD_INTERNAL_TOKEN
                )
            }
        )
        existing = owned.search(
            [
                ("connection_id", "in", scope.ids),
                ("occurrence_sha256", "=", occurrence),
            ],
            order="id",
            limit=1,
        )
        if existing:
            return existing
        return owned.create(values)
