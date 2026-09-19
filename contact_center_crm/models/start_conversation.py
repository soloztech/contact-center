from odoo import _, api, fields, models
from odoo.exceptions import AccessError


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
            self.account_id, phone=self.phone
        )
        return {
            "type": "ir.actions.client",
            "name": _("Contact Center"),
            "tag": "contact_center_ui.inbox",
            "params": {"channel_id": channel.id},
            "target": "current",
        }
