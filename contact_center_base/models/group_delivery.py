import datetime

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..services.dto import AddressDTO, DTOValidationError


def _utc_naive(value, field_label):
    """Return one second-precision UTC value accepted by Odoo Datetime fields."""

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


class ContactCenterGroupDeliveryEvent(models.Model):
    """One monotonic delivery milestone for one technical group participant."""

    _name = "contact.center.group.delivery.event"
    _description = "Contact Center Group Participant Delivery Event"
    _order = "occurred_at desc, id desc"
    _check_company_auto = True

    message_binding_id = fields.Many2one(
        "contact.center.message.binding",
        required=True,
        index=True,
        ondelete="cascade",
        check_company=True,
    )
    participant_id = fields.Many2one(
        "contact.center.group.participant",
        required=True,
        index=True,
        ondelete="cascade",
        check_company=True,
    )
    group_profile_id = fields.Many2one(
        related="participant_id.group_profile_id",
        store=True,
        readonly=True,
        index=True,
    )
    provider_connection_id = fields.Many2one(
        related="message_binding_id.provider_connection_id",
        store=True,
        readonly=True,
        index=True,
    )
    account_id = fields.Many2one(
        related="message_binding_id.account_id",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="message_binding_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    state = fields.Selection(
        [("delivered", "Delivered"), ("read", "Read")],
        required=True,
        index=True,
    )
    occurred_at = fields.Datetime(required=True, index=True)
    last_observed_at = fields.Datetime(required=True, index=True)
    external_event_id = fields.Char(required=True, index=True, copy=False)
    inbox_event_id = fields.Many2one(
        "contact.center.inbox.event",
        index=True,
        copy=False,
        ondelete="set null",
        check_company=True,
    )
    protocol_address_json = fields.Json(
        default=dict,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Exact provider-observed participant address used to resolve this "
            "delivery milestone. It is technical evidence and is never exposed "
            "in UiDTO."
        ),
    )

    _sql_constraints = [
        (
            "binding_participant_state_unique",
            "unique(message_binding_id, participant_id, state)",
            "A participant can have only one delivery milestone of each type.",
        ),
    ]

    @api.model
    def _validated_address(self, values):
        try:
            address = (
                values
                if isinstance(values, AddressDTO)
                else AddressDTO.from_dict(values or {})
            )
        except DTOValidationError as error:
            raise ValidationError(
                _("The group receipt protocol address is invalid.")
            ) from error
        if address.role != "sender" or address.confidence != "protocol":
            raise ValidationError(
                _("A group receipt requires a protocol-observed sender address.")
            )
        return address

    def _validate_scope_and_address(self):
        for event in self.sudo():
            binding = event.message_binding_id
            participant = event.participant_id
            profile = participant.group_profile_id
            if (
                binding.direction != "outbound"
                or binding.channel_binding_id.conversation_type != "group"
                or binding.channel_binding_id.identity_id
            ):
                raise ValidationError(
                    _("Participant receipts require an outbound group message.")
                )
            if profile.channel_binding_id != binding.channel_binding_id:
                raise ValidationError(
                    _("The receipt participant belongs to another conversation.")
                )
            if (
                not binding.provider_connection_id
                or profile.provider_connection_id != binding.provider_connection_id
            ):
                raise ValidationError(
                    _("The receipt participant belongs to another provider connection.")
                )
            if event.inbox_event_id and (
                event.inbox_event_id.provider_connection_id
                != binding.provider_connection_id
            ):
                raise ValidationError(
                    _("The receipt Inbox event belongs to another connection.")
                )
            address_values = event.protocol_address_json or {}
            if not address_values:
                continue
            address = self._validated_address(address_values)
            address_key = (address.namespace, address.value_normalized)
            if (
                self.env["contact.center.channel.alias"]
                .sudo()
                .search_count(
                    [
                        ("channel_binding_id", "=", binding.channel_binding_id.id),
                        ("namespace", "=", address_key[0]),
                        ("value_normalized", "=", address_key[1]),
                        ("role", "in", ("group", "routing")),
                    ]
                )
            ):
                raise ValidationError(
                    _("A group receipt sender cannot be a conversation address.")
                )
            if (
                not self.env["contact.center.group.participant.alias"]
                .sudo()
                .search_count(
                    [
                        ("participant_id", "=", participant.id),
                        ("namespace", "=", address_key[0]),
                        ("value_normalized", "=", address_key[1]),
                    ]
                )
            ):
                raise ValidationError(
                    _("The receipt address does not belong to the participant.")
                )

    @api.constrains(
        "message_binding_id",
        "participant_id",
        "inbox_event_id",
        "protocol_address_json",
    )
    def _check_scope_and_address(self):
        self._validate_scope_and_address()

    @api.model
    def _record_receipt(
        self,
        *,
        message_binding,
        group_profile,
        participant,
        state,
        occurred_at,
        external_event_id,
        inbox_event=None,
        protocol_address=None,
    ):
        """Converge one participant milestone without changing aggregate delivery."""

        message_binding.ensure_one()
        group_profile.ensure_one()
        participant.ensure_one()
        inbox_event = inbox_event or self.env["contact.center.inbox.event"]
        if inbox_event:
            inbox_event.ensure_one()
        if state not in ("delivered", "read"):
            raise ValidationError(_("Unsupported group receipt state: %s", state))
        if not isinstance(external_event_id, str) or not external_event_id.strip():
            raise ValidationError(_("A group receipt external event ID is required."))
        occurred_at = _utc_naive(occurred_at, _("Receipt occurrence time"))
        observed_at = _utc_naive(
            self.env.context.get("contact_center_group_receipt_observed_at")
            or (inbox_event.create_date if inbox_event else None)
            or fields.Datetime.now(),
            _("Receipt observation time"),
        )
        address = (
            self._validated_address(protocol_address) if protocol_address else None
        )

        profile = group_profile.sudo()
        if participant.group_profile_id != profile:
            raise ValidationError(
                _("The receipt participant belongs to another group profile.")
            )
        connection = message_binding.provider_connection_id.sudo()
        channel = message_binding.channel_binding_id.channel_id.sudo()
        if connection:
            self.env.cr.execute(
                "SELECT id FROM contact_center_provider_connection "
                "WHERE id = %s FOR SHARE",
                [connection.id],
            )
            if not self.env.cr.fetchone():
                raise ValidationError(
                    _("The group receipt provider connection no longer exists.")
                )
        self.env.cr.execute(
            "SELECT id FROM mail_channel WHERE id = %s FOR SHARE", [channel.id]
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("The group receipt conversation no longer exists."))
        self.env.cr.execute(
            "SELECT id FROM contact_center_group_profile WHERE id = %s FOR SHARE",
            [profile.id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("The group receipt profile no longer exists."))
        self.env.cr.execute(
            "SELECT id FROM contact_center_group_participant "
            "WHERE id = %s FOR SHARE",
            [participant.id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("The group receipt participant no longer exists."))
        # This row lock serializes every participant milestone for the message and
        # therefore also the first-create path protected by the SQL natural key.
        self.env.cr.execute(
            "SELECT id FROM contact_center_message_binding " "WHERE id = %s FOR UPDATE",
            [message_binding.id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("The group receipt message no longer exists."))
        message_binding.invalidate_recordset()
        participant.invalidate_recordset()
        profile.invalidate_recordset()

        values = {
            "message_binding_id": message_binding.id,
            "participant_id": participant.id,
            "state": state,
            "occurred_at": occurred_at,
            "last_observed_at": observed_at,
            "external_event_id": external_event_id.strip(),
            "inbox_event_id": inbox_event.id if inbox_event else False,
            "protocol_address_json": address.to_dict() if address else {},
        }
        existing = self.sudo().search(
            [
                ("message_binding_id", "=", message_binding.id),
                ("participant_id", "=", participant.id),
                ("state", "=", state),
            ],
            limit=1,
        )
        if not existing:
            return self.sudo().create(values)

        updates = {}
        if occurred_at < existing.occurred_at:
            updates["occurred_at"] = occurred_at
        if observed_at >= existing.last_observed_at:
            updates.update(
                {
                    "last_observed_at": observed_at,
                    "external_event_id": external_event_id.strip(),
                    "inbox_event_id": inbox_event.id if inbox_event else False,
                    "protocol_address_json": address.to_dict() if address else {},
                }
            )
        if updates:
            existing.write(updates)
        return existing

    @api.model
    def _summary_by_binding(self, bindings):
        """Return participant delivery counts for a set of message bindings."""

        if bindings._name != "contact.center.message.binding":
            raise ValidationError(
                _("Group delivery summaries require message bindings.")
            )
        binding_ids = bindings.exists().ids
        summary = {
            binding_id: {"delivered_count": 0, "read_count": 0}
            for binding_id in binding_ids
        }
        if not binding_ids:
            return summary
        # The aggregation bypasses the ORM cache. Flush pending ledger writes so
        # a timeline serialized in the same transaction cannot miss them.
        self.flush_model(["message_binding_id", "participant_id", "state"])
        self.env.cr.execute(
            """
            SELECT message_binding_id,
                   COUNT(DISTINCT participant_id) FILTER (
                       WHERE state IN ('delivered', 'read')
                   ) AS delivered_count,
                   COUNT(DISTINCT participant_id) FILTER (
                       WHERE state = 'read'
                   ) AS read_count
              FROM contact_center_group_delivery_event
             WHERE message_binding_id = ANY(%s)
          GROUP BY message_binding_id
            """,
            [binding_ids],
        )
        for binding_id, delivered_count, read_count in self.env.cr.fetchall():
            summary[binding_id] = {
                "delivered_count": delivered_count,
                "read_count": read_count,
            }
        return summary
