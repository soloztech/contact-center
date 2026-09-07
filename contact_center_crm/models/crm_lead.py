from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .conversation_link import (
    CRM_CONVERSATION_GRAPH_LOCK_TOKEN,
    conversation_graph_is_locked,
    lock_conversation_graph,
)


class CrmLead(models.Model):
    _inherit = "crm.lead"

    contact_center_conversation_count = fields.Integer(
        compute="_compute_contact_center_conversation_count"
    )

    def _contact_center_lock_conversation_graph(
        self, *, channel_ids=(), touch_leads=False, touch_channels=False
    ):
        lock_conversation_graph(
            self.env,
            lead_ids=self.ids,
            channel_ids=channel_ids,
            touch_leads=touch_leads,
            touch_channels=touch_channels,
        )
        return self.with_context(
            contact_center_crm_conversation_graph_lock=CRM_CONVERSATION_GRAPH_LOCK_TOKEN
        )

    def _conversation_links(self):
        return (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search([("lead_id", "in", self.ids), ("state", "=", "active")])
        )

    def _visible_contact_center_channels(self):
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_agent"
        ):
            return self.env["mail.channel"]
        return self.env["mail.channel"].search(
            [
                ("id", "in", self._conversation_links().mapped("channel_id").ids),
                ("contact_center_company_id", "in", self.env.companies.ids),
            ]
        )

    @api.depends_context("uid", "allowed_company_ids")
    def _compute_contact_center_conversation_count(self):
        for lead in self:
            lead.contact_center_conversation_count = len(
                lead._visible_contact_center_channels()
            )

    def action_view_contact_center_conversations(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        channels = self._visible_contact_center_channels()
        return {
            "type": "ir.actions.act_window",
            "name": _("Conversations"),
            "res_model": "mail.channel",
            "view_mode": "tree,form",
            "domain": [("id", "in", channels.ids)],
            "context": {"create": False},
        }

    def write(self, values):
        leads = self
        company_inputs = {"company_id", "user_id", "team_id", "partner_id"}
        check_company = bool(self and company_inputs.intersection(values))
        links = self.env["contact.center.crm.conversation.link"]
        if check_company:
            self.check_access_rights("write")
            self.check_access_rule("write")
            if not conversation_graph_is_locked(self.env):
                leads = self._contact_center_lock_conversation_graph(touch_leads=True)
                leads.check_access_rights("write")
                leads.check_access_rule("write")
            links = leads._conversation_links()
            company_id = values.get("company_id")
            if company_id and any(link.company_id.id != company_id for link in links):
                raise ValidationError(
                    _("A linked CRM record cannot move to another company.")
                )
        result = super(CrmLead, leads).write(values)
        # Native CRM can recompute company_id from the salesperson, sales team
        # or customer without an explicit company_id in the write payload.
        # The private ledger is read only after the caller's CRM write checks;
        # sudo here also permits a legitimate reassignment to another seller.
        if check_company and any(
            link.lead_id.company_id and link.lead_id.company_id != link.company_id
            for link in links
        ):
            raise ValidationError(
                _("A linked CRM record cannot move to another company.")
            )
        return result

    def unlink(self):
        self.check_access_rights("unlink")
        self.check_access_rule("unlink")
        leads = self._contact_center_lock_conversation_graph(
            touch_leads=True, touch_channels=True
        )
        leads.check_access_rule("unlink")
        leads._conversation_links()._tombstone("lead_deleted")
        return super(CrmLead, leads).unlink()

    def _merge_opportunity(
        self, user_id=False, team_id=False, auto_unlink=True, max_length=5
    ):
        leads = self._contact_center_lock_conversation_graph(
            touch_leads=True, touch_channels=True
        )
        return super(CrmLead, leads)._merge_opportunity(
            user_id=user_id,
            team_id=team_id,
            auto_unlink=auto_unlink,
            max_length=max_length,
        )

    def _merge_dependences(self, opportunities):
        self.ensure_one()
        combined = (self | opportunities)._contact_center_lock_conversation_graph(
            touch_leads=True, touch_channels=True
        )
        target = combined.filtered(lambda lead: lead.id == self.id)
        (combined - target)._conversation_links()._transfer_to_lead(target)
        return super(CrmLead, target)._merge_dependences(opportunities)
