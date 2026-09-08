from odoo import models


class ContactCenterUiApi(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    def _prepare_conversation_deletion_dependencies(
        self, channel, bindings, messages, inbox_events
    ):
        result = super()._prepare_conversation_deletion_dependencies(
            channel, bindings, messages, inbox_events
        )
        cases = (
            self.env["contact.center.case"]
            .sudo()
            .with_context(active_test=False)
            .search([("channel_id", "=", channel.id)])
        )
        # Remove only the optional pipeline projections of the deleted chat.
        # Native mail.thread.unlink cleans their chatter and followers as well.
        self.env["contact.center.crm.case.link"].sudo().search(
            [("case_id", "in", cases.ids)]
        ).unlink()
        cases.unlink()
        return result
