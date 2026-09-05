from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError


class ContactCenterConversationPreference(models.Model):
    """Sparse, personal presentation preferences for one conversation.

    Conversation lifecycle and routing remain properties of ``mail.channel``.
    Pinning and muting are deliberately user-scoped: one attendant changing their
    workspace must never reorder or silence the same conversation for colleagues.
    """

    _name = "contact.center.conversation.preference"
    _description = "Contact Center Conversation Preference"
    _order = "pinned_at desc, channel_id desc"
    _rec_name = "channel_id"

    channel_id = fields.Many2one(
        "mail.channel",
        string="Conversation",
        required=True,
        index=True,
        ondelete="cascade",
    )
    user_id = fields.Many2one(
        "res.users",
        string="User",
        required=True,
        index=True,
        default=lambda self: self.env.user,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        related="channel_id.contact_center_company_id",
        store=True,
        readonly=True,
        index=True,
    )
    pinned_at = fields.Datetime(
        index=True,
        copy=False,
        help="When set, this conversation is pinned for this user only.",
    )
    muted = fields.Boolean(
        default=False,
        index=True,
        help=(
            "Suppress personal browser attention for this user. Messages remain "
            "persisted and realtime invalidation events are still delivered."
        ),
    )

    _sql_constraints = [
        (
            "channel_user_unique",
            "unique(channel_id, user_id)",
            "A user can have only one preference row per conversation.",
        )
    ]

    @api.model
    def _validate_personal_scope(self, channel, user):
        channel.ensure_one()
        user.ensure_one()
        if user != self.env.user and not self.env.su:
            raise AccessError(_("Conversation preferences are personal."))
        if channel.channel_type != "contact_center":
            raise ValidationError(
                _("Preferences can only be set on Contact Center conversations.")
            )
        if channel.contact_center_company_id not in user.company_ids:
            raise AccessError(
                _("The conversation company is not available to this user.")
            )
        member = channel.sudo().channel_member_ids.filtered(
            lambda item: item.partner_id == user.partner_id
        )[:1]
        if not member:
            raise AccessError(_("You are not a member of this conversation."))
        if not self.env.su:
            channel.check_access_rights("read")
            channel.check_access_rule("read")
        return member

    @api.model_create_multi
    def create(self, vals_list):
        records_values = []
        for values in vals_list:
            values = dict(values)
            requested_user_id = values.get("user_id") or self.env.user.id
            if type(requested_user_id) is not int:  # noqa: E721 - reject bool
                raise ValidationError(_("The preference user is invalid."))
            user = self.env["res.users"].browse(requested_user_id).exists()
            channel_id = values.get("channel_id")
            if type(channel_id) is not int:  # noqa: E721 - reject bool
                raise ValidationError(_("The preference conversation is invalid."))
            channel = self.env["mail.channel"].browse(channel_id).exists()
            if not user or not channel:
                raise ValidationError(_("The preference scope no longer exists."))
            self._validate_personal_scope(channel, user)
            values["user_id"] = user.id
            records_values.append(values)
        return super().create(records_values)

    def write(self, values):
        if set(values) & {"channel_id", "user_id", "company_id"}:
            raise AccessError(_("The preference owner and conversation are immutable."))
        if set(values) - {"pinned_at", "muted"}:
            raise ValidationError(_("Unsupported conversation preference field."))
        for preference in self:
            self._validate_personal_scope(preference.channel_id, preference.user_id)
        return super().write(values)

    def unlink(self):
        for preference in self:
            self._validate_personal_scope(preference.channel_id, preference.user_id)
        return super().unlink()

    @api.constrains("channel_id", "user_id")
    def _check_personal_scope(self):
        for preference in self:
            preference._validate_personal_scope(
                preference.channel_id, preference.user_id
            )
