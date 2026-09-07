from odoo import models


class ContactCenterCrmConversationLink(models.Model):
    _inherit = "contact.center.crm.conversation.link"

    def _before_tombstone(self, reason):
        """Stop case projections when their conversation association is removed."""

        # A native merge moves the case projections to its survivor before
        # collapsing duplicate conversation links.  That collapse is not an
        # operator request to disconnect the survivor from its cases.
        if reason != "merged":
            CaseLink = self.env["contact.center.crm.case.link"].sudo()
            for link in self:
                CaseLink.search(
                    [
                        ("channel_id", "=", link.channel_id.id),
                        ("lead_id", "=", link.lead_id.id),
                        ("state", "=", "active"),
                    ]
                )._contact_center_tombstone(
                    "lead_deleted" if reason == "lead_deleted" else "manual",
                    actor_user_id=self.env.uid,
                )
        return super()._before_tombstone(reason)
