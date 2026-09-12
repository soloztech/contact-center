# Keep independent feature extensions in their own source files.
# pylint: disable=consider-merging-classes-inherited
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

_CONFIG_FIELDS = {
    "transcription_mode",
    "transcription_group_mode",
    "transcription_provider_id",
}
_ADMIN = "contact_center_base.group_contact_center_admin"
_MODES = [("disabled", "Disabled"), ("manual", "Manual"), ("automatic", "Automatic")]


class ContactCenterAccount(models.Model):
    _inherit = "contact.center.account"

    transcription_mode = fields.Selection(
        _MODES,
        string="Direct Conversation Transcription",
        default="disabled",
        required=True,
        groups=_ADMIN,
        help="Transcription mode for direct conversations in this inbox. "
        "Existing transcription settings continue to apply to direct conversations.",
    )
    transcription_group_mode = fields.Selection(
        _MODES,
        string="Group Conversation Transcription",
        default="disabled",
        required=True,
        groups=_ADMIN,
        help="Independent transcription mode for group conversations in this inbox. "
        "Disabled by default, including after an upgrade.",
    )
    transcription_provider_id = fields.Many2one(
        "contact.center.transcription.provider",
        string="Speech Provider",
        check_company=True,
        ondelete="restrict",
        groups=_ADMIN,
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any(_CONFIG_FIELDS.intersection(values) for values in vals_list) or any(
            "default_" + name in self.env.context for name in _CONFIG_FIELDS
        ):
            self._check_transcription_admin()
        return super().create(vals_list)

    def write(self, values):
        if _CONFIG_FIELDS.intersection(values):
            self._check_transcription_admin()
        return super().write(values)

    def _check_transcription_admin(self):
        if not self.env.su and not self.env.user.has_group(_ADMIN):
            raise AccessError(_("Only administrators can configure transcription."))

    def _transcription_mode_for_conversation(self, conversation_type):
        """Keep the legacy field for direct chats; new scopes are opt-in."""
        self.ensure_one()
        field_name = {
            "direct": "transcription_mode",
            "group": "transcription_group_mode",
        }.get(conversation_type)
        return self[field_name] if field_name else "disabled"

    @api.constrains(
        "transcription_mode",
        "transcription_group_mode",
        "transcription_provider_id",
        "company_id",
    )
    def _check_transcription_configuration(self):
        for account in self.sudo():
            provider = account.transcription_provider_id
            if (
                account.transcription_mode != "disabled"
                or account.transcription_group_mode != "disabled"
            ) and not provider:
                raise ValidationError(
                    _("Select a speech provider before enabling transcription.")
                )
            if provider and provider.company_id != account.company_id:
                raise ValidationError(
                    _("The speech provider must belong to the inbox company.")
                )
