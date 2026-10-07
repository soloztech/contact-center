from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError


class ContactCenterCrmStart(models.TransientModel):
    _name = "contact.center.crm.start"
    _description = "Start a CRM conversation"

    lead_id = fields.Many2one("crm.lead", required=True, readonly=True)
    available_account_ids = fields.Many2many(
        "contact.center.account", compute="_compute_available_accounts"
    )
    account_id = fields.Many2one(
        "contact.center.account", string="Caixa de WhatsApp", required=True
    )
    phone = fields.Char(string="Telefone do lead", required=True)

    @api.model
    def _check_references(self, lead_id, account_id):
        api_model = self.env["contact.center.ui.api"]
        lead_id = api_model._positive_id(lead_id, _("business ID"))
        lead = self.env["crm.lead"].browse(lead_id)
        try:
            lead._journey_check()
            if account_id:
                account = api_model._start_account(account_id)
                if lead.company_id and lead.company_id != account.company_id:
                    raise AccessError(
                        _("The business and inbox belong to different companies.")
                    )
        except (AccessError, MissingError):
            raise AccessError(_("The business or inbox is not available.")) from None

    def onchange(self, values, field_name, field_onchange):
        self.check_access_rights("read")
        self.check_access_rule("read")
        refs = dict(self.default_get(["lead_id", "account_id"]), **values)
        self._check_references(refs.get("lead_id"), refs.get("account_id"))
        return super().onchange(values, field_name, field_onchange)

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get(["lead_id", "account_id"])
        prepared = [dict(defaults, **values) for values in vals_list]
        for values in prepared:
            self._check_references(values.get("lead_id"), values.get("account_id"))
        return super().create(prepared)

    def write(self, values):
        self.check_access_rights("write")
        self.check_access_rule("write")
        if "lead_id" in values:
            raise AccessError(_("The assistant's business cannot be changed."))
        if "account_id" in values:
            for wizard in self:
                self._check_references(wizard.lead_id.id, values["account_id"])
        return super().write(values)

    @api.depends("lead_id")
    @api.depends_context("uid", "allowed_company_ids")
    def _compute_available_accounts(self):
        api_model = self.env["contact.center.ui.api"]
        for wizard in self:
            accounts = self.env["contact.center.account"]
            if wizard.lead_id:
                wizard.lead_id.check_access_rights("read")
                wizard.lead_id.check_access_rule("read")
                company_ids = wizard.lead_id.company_id.ids or self.env.companies.ids
                for account in accounts.search(
                    [("company_id", "in", company_ids), ("platform", "=", "whatsapp")]
                ):
                    try:
                        api_model._start_account(account.id)
                    except AccessError:
                        continue
                    if api_model._start_connection(account, required=False):
                        accounts |= account
            wizard.available_account_ids = accounts

    @api.onchange("lead_id")
    def _onchange_lead_id(self):
        if len(self.available_account_ids) == 1:
            self.account_id = self.available_account_ids

    def action_start(self):
        self.ensure_one()
        channel = self.lead_id._contact_center_start_and_link(
            self.account_id, phone=self.phone, writer="manual"
        )
        return {
            "type": "ir.actions.client",
            "name": _("Contact Center"),
            "tag": "contact_center_ui.inbox",
            "params": {"channel_id": channel.id},
            "target": "current",
        }
