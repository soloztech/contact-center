# Cooperative ORM policy and projection extensions have separate responsibilities.
# pylint: disable=consider-merging-classes-inherited
"""Private comparison receipts, independent of the lifetime of a CRM row."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from .intake_policy import INTAKE_TOKEN

COMPARISON_FIELDS = {
    "crm_comparison_exact",
    "crm_comparison_variant",
    "crm_comparison_version",
    "crm_comparison_source_at",
    "crm_comparison_policy_revision",
    "crm_comparison_erased_at",
}


class Binding(models.Model):
    _inherit = "contact.center.channel.binding"

    crm_comparison_exact = fields.Char(
        size=64,
        index=True,
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    crm_comparison_variant = fields.Char(
        size=64,
        index=True,
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    crm_comparison_version = fields.Integer(
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    crm_comparison_source_at = fields.Datetime(readonly=True, copy=False, index=True)
    crm_comparison_policy_revision = fields.Integer(
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    crm_comparison_erased_at = fields.Datetime(
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )

    _sql_constraints = [
        (
            "crm_comparison_receipt",
            "CHECK ((crm_comparison_exact IS NULL AND "
            "crm_comparison_variant IS NULL) OR (crm_comparison_exact IS NOT NULL AND "
            "length(crm_comparison_exact)=64 "
            "AND (crm_comparison_variant IS NULL OR length(crm_comparison_variant)=64)"
            " AND COALESCE(crm_comparison_version,0)=1 AND crm_comparison_source_at IS"
            " NOT NULL "
            "AND COALESCE(crm_comparison_policy_revision,0)>0 AND "
            "crm_comparison_erased_at IS NULL))",
            "The CRM comparison receipt is incomplete.",
        ),
        (
            "crm_intake_resolution_target",
            "CHECK (crm_intake_state IS DISTINCT FROM 'resolved' OR "
            "(crm_intake_reason IS NOT NULL AND ((crm_intake_reason='human_dismissed' "
            "AND COALESCE(crm_intake_lead_snapshot,0)=0) OR "
            "(crm_intake_reason='human_resolved' AND "
            "COALESCE(crm_intake_lead_snapshot,0)>0))))",
            "The CRM review resolution needs a valid target or dismissal.",
        ),
    ]

    def _crm_comparison_guard(self, values_list):
        if self.env.context.get("crm_intake_service") is not INTAKE_TOKEN and (
            any(COMPARISON_FIELDS.intersection(values) for values in values_list)
            or any(
                "default_" + field in self.env.context for field in COMPARISON_FIELDS
            )
        ):
            raise AccessError(_("CRM comparison receipts are managed internally."))

    @api.model_create_multi
    def create(self, values_list):
        self._crm_comparison_guard(values_list)
        return super().create(values_list)

    def write(self, values):
        self._crm_comparison_guard([values])
        return super().write(values)

    def _crm_erase_comparison(self):
        """Called only by an authenticated privacy adapter, never a repair job."""
        return self._crm_intake_write(
            {
                "crm_comparison_exact": False,
                "crm_comparison_variant": False,
                "crm_comparison_erased_at": fields.Datetime.now(),
            }
        )
