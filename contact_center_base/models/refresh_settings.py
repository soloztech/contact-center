from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# Shared refresh controls form one responsibility across the native settings models.
# pylint: disable=consider-merging-classes-inherited


class ResCompany(models.Model):
    _inherit = "res.company"

    contact_center_contact_cache_days = fields.Integer(
        string="Contact profile and photo validity (days)", default=7, required=True
    )
    contact_center_group_cache_days = fields.Integer(
        string="Group information and photo validity (days)", default=7, required=True
    )
    contact_center_health_interval_minutes = fields.Integer(
        string="Healthy connection check interval (minutes)", default=60, required=True
    )
    contact_center_profile_failure_hours = fields.Integer(
        string="Profile update failure wait (hours)", default=24, required=True
    )
    contact_center_roster_retry_minutes = fields.Integer(
        string="Minimum group participant check interval (minutes)",
        default=15,
        required=True,
    )

    @api.constrains(
        "contact_center_contact_cache_days",
        "contact_center_group_cache_days",
        "contact_center_health_interval_minutes",
        "contact_center_profile_failure_hours",
        "contact_center_roster_retry_minutes",
    )
    def _check_contact_center_refresh_intervals(self):
        bounds = (
            ("contact_center_contact_cache_days", 365),
            ("contact_center_group_cache_days", 365),
            ("contact_center_health_interval_minutes", 60),
            ("contact_center_profile_failure_hours", 168),
            ("contact_center_roster_retry_minutes", 1440),
        )
        for company in self:
            for field_name, maximum in bounds:
                if not 1 <= company[field_name] <= maximum:
                    raise ValidationError(
                        _("%(field)s must be between 1 and %(maximum)s.")
                        % {
                            "field": company._fields[field_name]._description_string(
                                self.env
                            ),
                            "maximum": maximum,
                        }
                    )


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    contact_center_contact_cache_days = fields.Integer(
        related="company_id.contact_center_contact_cache_days", readonly=False
    )
    contact_center_group_cache_days = fields.Integer(
        related="company_id.contact_center_group_cache_days", readonly=False
    )
    contact_center_health_interval_minutes = fields.Integer(
        related="company_id.contact_center_health_interval_minutes", readonly=False
    )
    contact_center_profile_failure_hours = fields.Integer(
        related="company_id.contact_center_profile_failure_hours", readonly=False
    )
    contact_center_roster_retry_minutes = fields.Integer(
        related="company_id.contact_center_roster_retry_minutes", readonly=False
    )
