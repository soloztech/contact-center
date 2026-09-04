from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.dto import MAX_FORWARDING_SCORE, AddressDTO, DTOValidationError
from ..services.tokens import CONTACT_CENTER_POST_TOKEN


def _record_id(value):
    if value in (None, False, ""):
        return False
    if type(value) is int:  # noqa: E721 - bool must not pass as an integer ID
        return value if value > 0 else None
    if (
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and not value.startswith("0")
    ):
        return int(value)
    return None


class MailMessage(models.Model):
    _inherit = "mail.message"

    @api.model
    def _contact_center_target(self, values):
        if values.get("model") != "mail.channel" or not values.get("res_id"):
            return False
        channel_id = _record_id(values["res_id"])
        if channel_id is None:
            raise AccessError(
                _("A mail.channel message requires a canonical integer record ID.")
            )
        if not channel_id:
            return False
        channel = self.env["mail.channel"].sudo().browse(channel_id).exists()
        return bool(channel and channel.channel_type == "contact_center")

    @api.model
    def _contact_center_external_comment(self, values):
        if not self._contact_center_target(values):
            return False
        if values.get("message_type") != "comment":
            return False
        if _record_id(values.get("subtype_id")) != self.env.ref("mail.mt_comment").id:
            return False
        return True

    @api.model
    def _contact_center_internal_note(self, values):
        return bool(
            self._contact_center_target(values)
            and values.get("message_type") == "comment"
            and _record_id(values.get("subtype_id")) == self.env.ref("mail.mt_note").id
        )

    def _contact_center_external_messages(self):
        comment_subtype = self.env.ref("mail.mt_comment")
        candidate_messages = self.filtered(
            lambda message: message.model == "mail.channel"
            and message.res_id
            and message.message_type == "comment"
            and message.subtype_id == comment_subtype
        )
        if not candidate_messages:
            return candidate_messages
        channel_ids = list(set(candidate_messages.mapped("res_id")))
        contact_center_ids = set(
            self.env["mail.channel"]
            .sudo()
            .browse(channel_ids)
            .exists()
            .filtered(lambda channel: channel.channel_type == "contact_center")
            .ids
        )
        return candidate_messages.filtered(
            lambda message: message.res_id in contact_center_ids
        )

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("contact_center_post_token")
            is not CONTACT_CENTER_POST_TOKEN
        ):
            defaults = self.default_get(
                ["model", "res_id", "message_type", "subtype_id"]
            )
            for values in vals_list:
                effective = {
                    field_name: values.get(field_name, defaults.get(field_name))
                    for field_name in (
                        "model",
                        "res_id",
                        "message_type",
                        "subtype_id",
                    )
                }
                if self._contact_center_target(effective):
                    raise AccessError(
                        _(
                            "Contact Center messages must be created through the "
                            "channel application service."
                        )
                    )
        return super().create(vals_list)

    def write(self, values):
        if (
            set(values) - {"starred_partner_ids"}
            and self.env.context.get("contact_center_post_token")
            is not CONTACT_CENTER_POST_TOKEN
        ):
            channel_messages = self.filtered(
                lambda message: message.model == "mail.channel"
            )
            bound_message_ids = set(
                self.env["contact.center.message.binding"]
                .sudo()
                .search([("message_id", "in", channel_messages.ids)])
                .message_id.ids
            )
            external_message_ids = set(
                channel_messages._contact_center_external_messages().ids
            )
            for message in self:
                current_target = {
                    "model": message.model,
                    "res_id": message.res_id,
                }
                effective = {
                    "model": values.get("model", message.model),
                    "res_id": values.get("res_id", message.res_id),
                    "message_type": values.get("message_type", message.message_type),
                    "subtype_id": values.get("subtype_id", message.subtype_id.id),
                }
                if (
                    message.id in bound_message_ids
                    or message.id in external_message_ids
                    or self._contact_center_external_comment(effective)
                    or (
                        self._contact_center_target(effective)
                        and not self._contact_center_internal_note(effective)
                    )
                    or (
                        {"model", "res_id"} & set(values)
                        and (
                            self._contact_center_target(current_target)
                            or self._contact_center_target(effective)
                        )
                    )
                    or (
                        {"author_id", "author_guest_id", "email_from", "date"}
                        & set(values)
                        and self._contact_center_target(effective)
                    )
                ):
                    raise AccessError(
                        _(
                            "External Contact Center messages are immutable in "
                            "this phase."
                        )
                    )
        return super().write(values)

    def unlink(self):
        if (
            self.env.context.get("contact_center_post_token")
            is not CONTACT_CENTER_POST_TOKEN
        ):
            channel_messages = self.filtered(
                lambda message: message.model == "mail.channel"
            )
            bound_message_ids = set(
                self.env["contact.center.message.binding"]
                .sudo()
                .search([("message_id", "in", channel_messages.ids)])
                .message_id.ids
            )
            if (
                bound_message_ids
                or channel_messages._contact_center_external_messages()
            ):
                raise AccessError(
                    _(
                        "External Contact Center messages cannot be deleted in "
                        "this phase."
                    )
                )
        return super().unlink()

    def _contact_center_reaction_guard(self):
        if (
            self.env.context.get("contact_center_post_token")
            is CONTACT_CENTER_POST_TOKEN
        ):
            return
        channel_messages = self.filtered(
            lambda message: message.model == "mail.channel"
        )
        bound_ids = set(
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "in", channel_messages.ids)])
            .message_id.ids
        )
        if bound_ids or channel_messages._contact_center_external_messages():
            raise AccessError(
                _("Use the Contact Center inbox to react to provider messages.")
            )

    def _message_add_reaction(self, content):
        self._contact_center_reaction_guard()
        return super()._message_add_reaction(content)

    def _message_remove_reaction(self, content):
        self._contact_center_reaction_guard()
        return super()._message_remove_reaction(content)


class IrAttachment(models.Model):
    _inherit = "ir.attachment"

    def _contact_center_external_attachments(self):
        if not self:
            return self
        messages = (
            self.env["mail.message"].sudo().search([("attachment_ids", "in", self.ids)])
        )
        bound_messages = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("message_id", "in", messages.ids)])
            .message_id
        )
        protected_ids = set(
            (
                bound_messages | messages._contact_center_external_messages()
            ).attachment_ids.ids
        )
        return self.filtered(lambda attachment: attachment.id in protected_ids)

    def write(self, values):
        if (
            self.env.context.get("contact_center_post_token")
            is not CONTACT_CENTER_POST_TOKEN
            and self._contact_center_external_attachments()
        ):
            raise AccessError(
                _("Attachments of external Contact Center messages are immutable.")
            )
        return super().write(values)

    def unlink(self):
        if (
            self.env.context.get("contact_center_post_token")
            is not CONTACT_CENTER_POST_TOKEN
            and self._contact_center_external_attachments()
        ):
            raise AccessError(
                _("Attachments of external Contact Center messages cannot be deleted.")
            )
        return super().unlink()


class ContactCenterMessageBinding(models.Model):
    _name = "contact.center.message.binding"
    _description = "Contact Center Message Binding"
    _order = "message_id desc"

    message_id = fields.Many2one(
        "mail.message", required=True, index=True, ondelete="cascade"
    )
    channel_binding_id = fields.Many2one(
        "contact.center.channel.binding", required=True, index=True, ondelete="cascade"
    )
    account_id = fields.Many2one(
        related="channel_binding_id.account_id", store=True, readonly=True, index=True
    )
    company_id = fields.Many2one(
        related="channel_binding_id.company_id", store=True, readonly=True, index=True
    )
    provider_connection_id = fields.Many2one(
        "contact.center.provider.connection", index=True, ondelete="restrict"
    )
    source_inbox_event_id = fields.Many2one(
        "contact.center.inbox.event",
        string="Source Inbox Event",
        readonly=True,
        copy=False,
        index=True,
        ondelete="restrict",
        groups="base.group_system",
        help=("Immutable inbound ledger event that created this message projection."),
    )
    direction = fields.Selection(
        [("inbound", "Inbound"), ("outbound", "Outbound")],
        required=True,
        index=True,
    )
    origin = fields.Selection(
        [
            ("provider", "Provider"),
            ("agent", "Agent"),
            ("automation", "Automation"),
            ("external_device", "External Device"),
        ],
        required=True,
        index=True,
    )
    content_type = fields.Char(required=True, default="text", index=True)
    external_message_id = fields.Char(index=True, copy=False)
    client_message_id = fields.Char(index=True, copy=False)
    is_forwarded = fields.Boolean(
        default=False,
        required=True,
        copy=False,
        readonly=True,
        help=(
            "Provider-asserted evidence that the sender forwarded this message. "
            "It is independent from replies and quoted content."
        ),
    )
    forwarding_score = fields.Integer(
        default=0,
        required=True,
        copy=False,
        readonly=True,
        help=(
            "Provider forwarding score when explicitly observed. The score does "
            "not by itself classify a message as forwarded."
        ),
    )
    forwarding_score_observed = fields.Boolean(
        default=False,
        required=True,
        copy=False,
        readonly=True,
        help="Whether the provider explicitly supplied a forwarding score.",
    )
    protocol_snapshot_json = fields.Json(default=dict, copy=False)
    protocol_participant_json = fields.Json(
        default=dict,
        copy=False,
        help=(
            "Canonical protocol address of this message's group participant. It is "
            "kept in the technical ledger and is never exposed through the UiDTO."
        ),
    )
    reply_to_binding_id = fields.Many2one(
        "contact.center.message.binding", index=True, ondelete="set null"
    )
    media_ids = fields.One2many(
        "contact.center.media.binding", "message_binding_id", string="Media"
    )
    mutation_ids = fields.One2many(
        "contact.center.message.mutation",
        "target_message_binding_id",
        string="Mutations",
    )
    message_state = fields.Selection(
        [("active", "Active"), ("edited", "Edited"), ("deleted", "Deleted")],
        default="active",
        required=True,
        index=True,
        copy=False,
    )
    edited_at = fields.Datetime(copy=False)
    deleted_at = fields.Datetime(copy=False)
    deleted_display_mode = fields.Selection(
        [
            ("redact", "Redact"),
            ("strike", "Strike Through"),
        ],
        required=True,
        default="redact",
        readonly=True,
        copy=False,
        help=(
            "Immutable display-policy snapshot captured when the message is deleted. "
            "Redact hides its content from the operator projection; strike preserves "
            "a bounded body snapshot for a struck-through projection."
        ),
    )
    deleted_body = fields.Html(
        readonly=True,
        copy=False,
        help=(
            "Sanitized message body captured immediately before a strike-mode "
            "deletion. The canonical mail.message body remains a deletion tombstone."
        ),
    )
    # Kept only as mutation audit data, but retain the normal Odoo sanitizer so
    # a future projection cannot accidentally turn historical provider content
    # into executable HTML.
    original_body = fields.Html(copy=False)
    mutation_projection_revision = fields.Integer(
        default=0,
        required=True,
        copy=False,
        readonly=True,
        help=(
            "Monotonic row revision used to serialize message mutation projections "
            "across independent workers."
        ),
    )
    delivery_state = fields.Selection(
        [
            ("queued", "Queued"),
            ("sent", "Sent"),
            ("delivered", "Delivered"),
            ("read", "Read"),
            ("failed", "Failed"),
        ],
        default="queued",
        index=True,
    )

    _sql_constraints = [
        (
            "message_unique",
            "unique(message_id)",
            "A message can have only one binding.",
        ),
        (
            "external_message_unique",
            "unique(channel_binding_id, external_message_id)",
            "The provider message ID already exists in this conversation.",
        ),
        (
            "mutation_projection_revision_nonnegative",
            "check(mutation_projection_revision >= 0)",
            "The mutation projection revision cannot be negative.",
        ),
        (
            "forwarding_score_range",
            "check(forwarding_score >= 0 AND forwarding_score <= 2147483647)",
            "The forwarding score is outside the supported range.",
        ),
        (
            "forwarding_score_observation",
            "check(forwarding_score_observed OR forwarding_score = 0)",
            "A forwarding score requires explicit provider evidence.",
        ),
    ]

    def init(self):
        """Keep provider correlation keys unique without constraining empty values."""

        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                contact_center_message_binding_client_message_unique
            ON contact_center_message_binding
                (provider_connection_id, client_message_id)
            WHERE provider_connection_id IS NOT NULL
              AND client_message_id IS NOT NULL
              AND client_message_id <> ''
            """
        )

    def write(self, values):
        if "source_inbox_event_id" in values:
            requested_id = _record_id(values.get("source_inbox_event_id"))
            if requested_id is None:
                raise ValidationError(_("The source inbox event ID is invalid."))
            for binding in self.sudo().sorted("id"):
                self.env.cr.execute(
                    "SELECT id FROM contact_center_message_binding "
                    "WHERE id = %s FOR UPDATE",
                    [binding.id],
                )
                binding.invalidate_recordset(["source_inbox_event_id"])
                current_id = binding.source_inbox_event_id.id
                if current_id and requested_id != current_id:
                    raise ValidationError(_("The source inbox event is immutable."))
        return super().write(values)

    @api.constrains(
        "message_id",
        "channel_binding_id",
        "provider_connection_id",
        "source_inbox_event_id",
        "origin",
        "reply_to_binding_id",
        "protocol_participant_json",
    )
    def _check_binding(self):
        for binding in self:
            message = binding.message_id
            if (
                message.model != "mail.channel"
                or message.res_id != binding.channel_binding_id.channel_id.id
            ):
                raise ValidationError(
                    _("The message does not belong to the bound channel.")
                )
            if (
                binding.provider_connection_id
                and binding.provider_connection_id.account_id != binding.account_id
            ):
                raise ValidationError(
                    _("The provider connection belongs to another account.")
                )
            source_event = binding.sudo().source_inbox_event_id
            if source_event and (
                binding.origin not in ("provider", "external_device")
                or not binding.provider_connection_id
                or source_event.provider_connection_id != binding.provider_connection_id
                or source_event.account_id != binding.account_id
                or source_event.company_id != binding.company_id
            ):
                raise ValidationError(
                    _(
                        "The source inbox event must belong to the same provider, "
                        "account, and company as its provider-created message."
                    )
                )
            reply_target = binding.reply_to_binding_id
            if reply_target and (
                reply_target == binding
                or reply_target.account_id != binding.account_id
                or reply_target.channel_binding_id != binding.channel_binding_id
            ):
                raise ValidationError(
                    _("A replied message must belong to the same conversation.")
                )
            participant = binding.protocol_participant_json or {}
            if participant:
                try:
                    address = AddressDTO.from_dict(participant)
                except DTOValidationError as error:
                    raise ValidationError(
                        _("The protocol participant address is invalid.")
                    ) from error
                if address.role != "sender" or address.confidence != "protocol":
                    raise ValidationError(
                        _(
                            "A protocol participant must be a sender address observed "
                            "from the provider."
                        )
                    )

    def _contact_center_set_protocol_participant(self, participant):
        """Persist one immutable group participant identity with a row lock.

        Raw JID spelling and source fields are evidence, not identity.  Preserve
        the first canonical observation when a later echo carries the same
        namespace and normalized value through another provider field.
        """

        if isinstance(participant, AddressDTO):
            participant = participant.to_dict()
        try:
            address = AddressDTO.from_dict(participant)
        except DTOValidationError as error:
            raise ValidationError(
                _("The protocol participant address is invalid.")
            ) from error
        if address.role != "sender" or address.confidence != "protocol":
            raise ValidationError(
                _(
                    "A protocol participant must be a sender address observed from "
                    "the provider."
                )
            )
        canonical = address.to_dict()
        changed = False
        for binding in self.sudo():
            self.env.cr.execute(
                "SELECT id FROM contact_center_message_binding "
                "WHERE id = %s FOR UPDATE",
                [binding.id],
            )
            binding.invalidate_recordset(["protocol_participant_json"])
            current = binding.protocol_participant_json or {}
            if current:
                try:
                    current_address = AddressDTO.from_dict(current)
                except DTOValidationError as error:
                    raise ValidationError(
                        _("The persisted protocol participant address is invalid.")
                    ) from error
                if (
                    current_address.namespace,
                    current_address.value_normalized,
                    current_address.role,
                    current_address.confidence,
                ) != (
                    address.namespace,
                    address.value_normalized,
                    address.role,
                    address.confidence,
                ):
                    raise ValidationError(
                        _(
                            "The observed protocol participant conflicts with the "
                            "persisted message participant."
                        )
                    )
            if not current:
                binding.write({"protocol_participant_json": canonical})
                changed = True
        return changed

    def _contact_center_merge_forwarding(self, is_forwarded, forwarding_score=None):
        """Merge provider forwarding evidence monotonically under a row lock.

        A duplicate/replayed webhook may enrich an old projection, but it must
        never erase stronger evidence already observed. The score is retained
        separately because it is not reliable proof of forwarding on its own.
        """

        if not isinstance(is_forwarded, bool):
            raise ValidationError(_("The forwarded flag must be a boolean."))
        if forwarding_score is not None and (
            not isinstance(forwarding_score, int)
            or isinstance(forwarding_score, bool)
            or not 0 <= forwarding_score <= MAX_FORWARDING_SCORE
        ):
            raise ValidationError(_("The forwarding score is invalid."))
        changed = False
        for binding in self.sudo().sorted("id"):
            self.env.cr.execute(
                "SELECT id FROM contact_center_message_binding "
                "WHERE id = %s FOR UPDATE",
                [binding.id],
            )
            binding.invalidate_recordset(
                ["is_forwarded", "forwarding_score", "forwarding_score_observed"]
            )
            values = {}
            if is_forwarded and not binding.is_forwarded:
                values["is_forwarded"] = True
            if forwarding_score is not None:
                if not binding.forwarding_score_observed:
                    values["forwarding_score_observed"] = True
                if forwarding_score > binding.forwarding_score:
                    values["forwarding_score"] = forwarding_score
            if values:
                binding.write(values)
                changed = True
        return changed

    def _contact_center_apply_delivery(
        self,
        state,
        *,
        occurred_at=None,
        external_event_id=None,
        external_message_id=None,
        details=None,
    ):
        """Apply delivery evidence without ever regressing the canonical state."""

        allowed_states = {"queued", "sent", "delivered", "read", "failed"}
        if state not in allowed_states:
            raise ValidationError(_("Unsupported delivery state: %s", state))
        occurred_at = occurred_at or fields.Datetime.now()
        details = details or {}
        ranks = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}
        delivery_model = self.env["contact.center.delivery.event"].sudo()
        watermark_model = self.env["contact.center.delivery.watermark"].sudo()
        replay_watermarks = not self.env.context.get(
            "contact_center_skip_watermark_replay"
        )

        # A cumulative receipt can race the provider response or a from-me echo.
        # Acquire one provider/conversation advisory lock before any message row so
        # either transaction observes and applies the other's committed evidence.
        if replay_watermarks:
            scopes = {
                (binding.provider_connection_id.id, binding.channel_binding_id.id): (
                    binding.provider_connection_id.sudo(),
                    binding.channel_binding_id.sudo(),
                )
                for binding in self.sudo()
                if binding.provider_connection_id
                and binding.direction == "outbound"
                and binding.channel_binding_id.conversation_type == "direct"
            }
            for connection, channel_binding in (scopes[key] for key in sorted(scopes)):
                watermark_model._lock_scope(connection, channel_binding)

        for binding in self.sudo():
            self.env.cr.execute(
                "SELECT id FROM contact_center_message_binding "
                "WHERE id = %s FOR UPDATE",
                [binding.id],
            )
            binding.invalidate_recordset(["delivery_state", "external_message_id"])
            values = {}
            if external_message_id:
                if (
                    binding.external_message_id
                    and binding.external_message_id != external_message_id
                ):
                    raise ValidationError(
                        _(
                            "Provider message correlation conflicts with the "
                            "persisted external message ID."
                        )
                    )
                if not binding.external_message_id:
                    values["external_message_id"] = external_message_id

            current_state = binding.delivery_state or "queued"
            if state == "failed":
                should_advance = current_state in (False, "queued")
            elif current_state == "failed":
                # Later provider evidence is stronger than a local dispatch failure.
                should_advance = True
            else:
                should_advance = ranks[state] > ranks.get(current_state, -1)
            if should_advance:
                values["delivery_state"] = state
            if values:
                binding.write(values)

            event_domain = []
            already_recorded = False
            if external_event_id:
                event_domain = [
                    ("message_binding_id", "=", binding.id),
                    ("external_event_id", "=", external_event_id),
                ]
                already_recorded = bool(delivery_model.search(event_domain, limit=1))
            if (should_advance or external_event_id) and not already_recorded:
                delivery_model.create(
                    {
                        "message_binding_id": binding.id,
                        "state": state,
                        "occurred_at": occurred_at,
                        "external_event_id": external_event_id or False,
                        "details_json": details,
                    }
                )
            if replay_watermarks:
                watermark_model._apply_to_binding(binding)
        return True


class ContactCenterDeliveryEvent(models.Model):
    _name = "contact.center.delivery.event"
    _description = "Contact Center Delivery Event"
    _order = "occurred_at desc, id desc"

    message_binding_id = fields.Many2one(
        "contact.center.message.binding", required=True, index=True, ondelete="cascade"
    )
    account_id = fields.Many2one(
        related="message_binding_id.account_id", store=True, readonly=True, index=True
    )
    company_id = fields.Many2one(
        related="message_binding_id.company_id", store=True, readonly=True, index=True
    )
    state = fields.Selection(
        [
            ("queued", "Queued"),
            ("sent", "Sent"),
            ("delivered", "Delivered"),
            ("read", "Read"),
            ("failed", "Failed"),
        ],
        required=True,
        index=True,
    )
    occurred_at = fields.Datetime(required=True, index=True)
    external_event_id = fields.Char(index=True)
    details_json = fields.Json(default=dict)

    def init(self):
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                contact_center_delivery_event_external_unique
            ON contact_center_delivery_event
                (message_binding_id, external_event_id)
            WHERE external_event_id IS NOT NULL
              AND external_event_id <> ''
            """
        )
