from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.contact_center_base.services.phone import normalize_start_phone

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
    contact_center_phone_normalized = fields.Char(
        compute="_compute_contact_center_phones", store=True, index=True
    )
    contact_center_mobile_normalized = fields.Char(
        compute="_compute_contact_center_phones", store=True, index=True
    )

    @api.depends("phone", "mobile", "country_id", "company_id.country_id")
    def _compute_contact_center_phones(self):
        for lead in self:
            country = lead.country_id or lead.company_id.country_id
            for source in ("phone", "mobile"):
                try:
                    value = normalize_start_phone(lead[source], country.code or "BR")
                except ValueError:
                    value = False
                lead["contact_center_%s_normalized" % source] = value

    def _contact_center_start_and_link(self, account, phone=None, *, writer):
        """Open/reuse an authorized conversation; never send or create a partner.

        Shared entry point for the manual wizard and optional automation addons.
        It returns a mail.channel record in the caller's environment.
        """
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        api_model = self.env["contact.center.ui.api"]
        account.ensure_one()
        account = api_model._start_account(account.id)
        if self.company_id and self.company_id != account.company_id:
            raise ValidationError(_("Choose an inbox from the lead's company."))
        candidates = {
            value
            for value in (
                self.contact_center_mobile_normalized,
                self.contact_center_phone_normalized,
            )
            if value
        }
        if phone is None:
            normalized = (
                self.contact_center_mobile_normalized
                or self.contact_center_phone_normalized
            )
        else:
            normalized = api_model._start_normalized_phone(account, phone)[
                "normalized_phone"
            ]
        if not normalized or normalized not in candidates:
            raise ValidationError(
                _(
                    "Choose a valid phone from this lead. Update the lead first if needed."
                )
            )
        # Roll back conversation admission as well if final CRM authorization or
        # linking fails. Provider address lookup itself does not send a message.
        with self.env.cr.savepoint():
            result = api_model.start_conversation(account.id, "+" + normalized)
            channel = api_model._crm_channel(result["channel_id"], mutate=True)
            self.env["contact.center.crm.conversation.link"]._link(
                channel, self, writer=writer
            )
            # _link locks and reauthorizes the lead after provider I/O. Recheck
            # the selected address under that lock before committing the graph.
            self.invalidate_recordset(
                ["contact_center_mobile_normalized", "contact_center_phone_normalized"]
            )
            if normalized not in (
                self.contact_center_mobile_normalized,
                self.contact_center_phone_normalized,
            ):
                raise ValidationError(
                    _("The lead's phone changed. Open the conversation again.")
                )
        return channel

    def action_contact_center_converse(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        self.env["contact.center.ui.api"]._application()._check_agent()
        phone = (
            self.contact_center_mobile_normalized
            or self.contact_center_phone_normalized
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("Conversar"),
            "res_model": "contact.center.crm.start",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_lead_id": self.id,
                "default_phone": "+" + phone if phone else False,
            },
        }

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
