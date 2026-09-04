from odoo import models


class ContactCenterAccountSetupWizard(models.TransientModel):
    _inherit = "contact.center.account.setup.wizard"

    def _contact_center_inbox_action(self):
        self.ensure_one()
        return self.env.ref("contact_center_ui.action_contact_center_inbox").read()[0]
