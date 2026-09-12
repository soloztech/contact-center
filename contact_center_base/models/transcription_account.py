from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

_CONFIG_FIELDS = {"transcription_mode", "transcription_provider_id"}
_ADMIN = "contact_center_base.group_contact_center_admin"


class ContactCenterAccount(models.Model):
    _inherit = "contact.center.account"

    transcription_mode = fields.Selection(
        [("disabled", "Disabled"), ("manual", "Manual"), ("automatic", "Automatic")],
        default="disabled",
        required=True,
        groups=_ADMIN,
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
        if any(_CONFIG_FIELDS.intersection(values) for values in vals_list):
            self._check_transcription_admin()
        return super().create(vals_list)

    def write(self, values):
        if _CONFIG_FIELDS.intersection(values):
            self._check_transcription_admin()
        return super().write(values)

    def _check_transcription_admin(self):
        if not self.env.su and not self.env.user.has_group(_ADMIN):
            raise AccessError(_("Only administrators can configure transcription."))

    @api.constrains("transcription_mode", "transcription_provider_id", "company_id")
    def _check_transcription_configuration(self):
        for account in self.sudo():
            provider = account.transcription_provider_id
            if account.transcription_mode != "disabled" and not provider:
                raise ValidationError(
                    _("Select a speech provider before enabling transcription.")
                )
            if provider and provider.company_id != account.company_id:
                raise ValidationError(
                    _("The speech provider must belong to the inbox company.")
                )
