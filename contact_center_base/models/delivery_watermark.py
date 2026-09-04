import datetime
import hashlib
import struct

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


def _utc_naive(value, field_label):
    """Return a second-precision naive UTC datetime for an Odoo field."""

    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            value = fields.Datetime.to_datetime(value)
    elif not isinstance(value, datetime.datetime):
        value = fields.Datetime.to_datetime(value)
    if not value:
        raise ValidationError(_("%s is required.", field_label))
    if value.tzinfo:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


class ContactCenterDeliveryWatermark(models.Model):
    """Durable cumulative delivery cursor for one direct provider conversation.

    Providers can publish a cumulative receipt before the corresponding message
    echo is committed locally.  Keeping the high-watermark separately makes that
    evidence replayable when the outbound binding appears later.
    """

    _name = "contact.center.delivery.watermark"
    _description = "Contact Center Direct Delivery Watermark"
    _order = "watermark_at desc, id desc"
    _check_company_auto = True

    provider_connection_id = fields.Many2one(
        "contact.center.provider.connection",
        required=True,
        index=True,
        ondelete="cascade",
        check_company=True,
    )
    channel_binding_id = fields.Many2one(
        "contact.center.channel.binding",
        required=True,
        index=True,
        ondelete="cascade",
        check_company=True,
    )
    account_id = fields.Many2one(
        related="channel_binding_id.account_id",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="channel_binding_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    state = fields.Selection(
        [("delivered", "Delivered"), ("read", "Read")],
        required=True,
        index=True,
    )
    watermark_at = fields.Datetime(required=True, index=True)
    occurred_at = fields.Datetime(required=True, index=True)
    external_event_id = fields.Char(required=True, index=True, copy=False)
    details_json = fields.Json(
        default=dict,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )

    _sql_constraints = [
        (
            "connection_channel_state_unique",
            "unique(provider_connection_id, channel_binding_id, state)",
            "A direct conversation can have only one high-watermark per state.",
        ),
    ]

    @api.constrains(
        "provider_connection_id",
        "channel_binding_id",
        "state",
        "watermark_at",
        "occurred_at",
        "external_event_id",
    )
    def _check_scope(self):
        for cursor in self:
            binding = cursor.channel_binding_id
            connection = cursor.provider_connection_id
            if (
                binding.conversation_type != "direct"
                or not binding.identity_id
                or binding.account_id != connection.account_id
            ):
                raise ValidationError(
                    _(
                        "Delivery watermarks require a direct conversation and "
                        "provider connection from the same account."
                    )
                )
            if not cursor.external_event_id.strip():
                raise ValidationError(
                    _("A delivery watermark external event ID is required.")
                )

    @api.model
    def _scope_lock_key(self, connection, channel_binding):
        """Return one stable signed int64 key for a transaction advisory lock."""

        connection.ensure_one()
        channel_binding.ensure_one()
        material = "contact-center:delivery-watermark:%s:%s" % (
            connection.id,
            channel_binding.id,
        )
        return struct.unpack(">q", hashlib.sha256(material.encode()).digest()[:8])[0]

    @api.model
    def _lock_scope(self, connection, channel_binding):
        """Serialize receipt persistence and late outbound correlation."""

        connection.ensure_one()
        channel_binding.ensure_one()
        if connection.account_id != channel_binding.account_id:
            raise ValidationError(
                _("The delivery watermark scope belongs to another account.")
            )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            [self._scope_lock_key(connection, channel_binding)],
        )
        return True

    @api.model
    def _record(
        self,
        *,
        connection,
        channel_binding,
        state,
        watermark_at,
        occurred_at,
        external_event_id,
        details=None,
    ):
        """Monotonically converge one cumulative receipt and return its cursor."""

        connection.ensure_one()
        channel_binding.ensure_one()
        if state not in ("delivered", "read"):
            raise ValidationError(_("Unsupported delivery watermark state: %s", state))
        if not isinstance(external_event_id, str) or not external_event_id.strip():
            raise ValidationError(
                _("A delivery watermark external event ID is required.")
            )
        watermark_at = _utc_naive(watermark_at, _("Delivery watermark"))
        occurred_at = _utc_naive(occurred_at, _("Receipt occurrence time"))
        self._lock_scope(connection, channel_binding)
        cursor = self.sudo().search(
            [
                ("provider_connection_id", "=", connection.id),
                ("channel_binding_id", "=", channel_binding.id),
                ("state", "=", state),
            ],
            limit=1,
        )
        if cursor:
            self.env.cr.execute(
                "SELECT id FROM contact_center_delivery_watermark "
                "WHERE id = %s FOR UPDATE",
                [cursor.id],
            )
            cursor.invalidate_recordset(
                ["watermark_at", "occurred_at", "external_event_id", "details_json"]
            )
            if cursor.watermark_at >= watermark_at:
                return cursor
            cursor.write(
                {
                    "watermark_at": watermark_at,
                    "occurred_at": occurred_at,
                    "external_event_id": external_event_id.strip(),
                    "details_json": details or {},
                }
            )
            return cursor
        return self.sudo().create(
            {
                "provider_connection_id": connection.id,
                "channel_binding_id": channel_binding.id,
                "state": state,
                "watermark_at": watermark_at,
                "occurred_at": occurred_at,
                "external_event_id": external_event_id.strip(),
                "details_json": details or {},
            }
        )

    @api.model
    def _apply_to_binding(self, message_binding):
        """Replay every applicable cumulative cursor onto one outbound message."""

        message_binding.ensure_one()
        binding = message_binding.sudo()
        connection = binding.provider_connection_id.sudo()
        channel_binding = binding.channel_binding_id.sudo()
        if (
            not connection
            or binding.direction != "outbound"
            or channel_binding.conversation_type != "direct"
            or not binding.external_message_id
        ):
            return self.env["contact.center.delivery.watermark"]
        self._lock_scope(connection, channel_binding)
        message_at = fields.Datetime.to_datetime(binding.message_id.date)
        cursors = self.sudo().search(
            [
                ("provider_connection_id", "=", connection.id),
                ("channel_binding_id", "=", channel_binding.id),
                ("watermark_at", ">=", message_at),
            ],
            order="watermark_at, state, id",
        )
        for cursor in cursors:
            binding.with_context(
                contact_center_skip_watermark_replay=True
            )._contact_center_apply_delivery(
                cursor.state,
                occurred_at=cursor.occurred_at,
                external_event_id=cursor.external_event_id,
                details=cursor.details_json or {},
            )
        return cursors
