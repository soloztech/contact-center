# Contact relationships and their setting form one feature across native models.
# pylint: disable=consider-merging-classes-inherited

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ResPartner(models.Model):
    _inherit = "res.partner"

    contact_center_secondary_company_ids = fields.Many2many(
        "res.partner",
        "contact_center_partner_company_rel",
        "person_id",
        "company_partner_id",
        string="Secondary companies",
        copy=False,
        domain="[('is_company', '=', True), ('type', '=', 'contact'), "
        "('company_id', 'in', [False, company_id]), ('id', '!=', parent_id)]",
        help="Additional companies associated with this person. The primary company "
        "remains Odoo's commercial entity; these relationships do not alter documents.",
    )

    @api.constrains(
        "contact_center_secondary_company_ids",
        "parent_id",
        "is_company",
        "type",
        "company_id",
    )
    def _check_contact_center_secondary_companies(self):
        for person in self:
            companies = person.sudo().contact_center_secondary_company_ids
            if not companies:
                continue
            if person.is_company or person.type != "contact":
                raise ValidationError(
                    _("Only individual contacts may have secondary companies.")
                )
            if any(
                not company.is_company
                or company.type != "contact"
                or company == person.parent_id
                or (company.company_id and company.company_id != person.company_id)
                for company in companies
            ):
                raise ValidationError(
                    _(
                        "Select companies in the contact's Odoo company, "
                        "different from its primary company."
                    )
                )

    def write(self, values):
        result = super().write(values)
        if {"is_company", "company_type", "type", "company_id"} & set(values):
            # Validate the inverse side as well: a company already referenced by
            # people cannot silently become a person or cross a tenant boundary.
            self.sudo().search(
                [("contact_center_secondary_company_ids", "in", self.ids)]
            )._check_contact_center_secondary_companies()
        return result


class ResCompany(models.Model):
    _inherit = "res.company"

    contact_center_secondary_companies_enabled = fields.Boolean(
        string="Secondary company relationships",
        default=True,
        help="Allow agents to add secondary companies to contacts. Disabling "
        "prevents new relationships and preserves existing ones.",
    )


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    contact_center_secondary_companies_enabled = fields.Boolean(
        related="company_id.contact_center_secondary_companies_enabled",
        readonly=False,
    )
