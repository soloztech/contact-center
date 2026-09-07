from odoo import _, api, models
from odoo.exceptions import AccessError, ValidationError
from odoo.osv import expression

from odoo.addons.contact_center_base.services.dto import SCHEMA_VERSION


class ContactCenterUiApi(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def bootstrap(self):
        result = super().bootstrap()
        result["capabilities"]["view_crm"] = self.env["crm.lead"].check_access_rights(
            "read", raise_exception=False
        )
        return result

    def _crm_channel(self, channel_id, *, mutate=False):
        channel, _member = self._authorized_channel(channel_id)
        self.env["crm.lead"].check_access_rights("read")
        if mutate:
            channel.check_access_rights("write")
            channel.check_access_rule("write")
        return channel

    def _crm_customer(self, channel):
        binding = self._binding_for_channel(channel)
        partner = binding.identity_id.partner_id if binding else self.env["res.partner"]
        if not partner:
            return partner, partner
        try:
            partner.check_access_rights("read")
            partner.check_access_rule("read")
            if (
                partner.company_id
                and partner.company_id != channel.contact_center_company_id
            ):
                return self.env["res.partner"], self.env["res.partner"]
            company = partner.commercial_partner_id
            company.check_access_rule("read")
            if (
                company.company_id
                and company.company_id != channel.contact_center_company_id
            ):
                company = partner
        except AccessError:
            return self.env["res.partner"], self.env["res.partner"]
        return partner, company

    def _crm_customer_domain(self, channel):
        partner, company = self._crm_customer(channel)
        if not partner:
            return [("id", "=", 0)]
        return [("partner_id.commercial_partner_id", "=", company.id)]

    def _crm_scope_domain(self, channel):
        return [("company_id", "in", [False, channel.contact_center_company_id.id])]

    def _crm_links(self, channel):
        # This private ledger has no agent ACL. Its projection always intersects
        # native crm.lead rules after checking the conversation's own access.
        return (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search([("channel_id", "=", channel.id), ("state", "=", "active")])
        )

    def _serialize_crm_opportunity(self, lead, linked_ids):
        currency = lead.company_currency or self.env.company.currency_id
        return {
            "id": lead.id,
            "name": lead.name,
            "type": lead.type,
            "active": lead.active,
            "linked": lead.id in linked_ids,
            "stage": {
                "id": lead.stage_id.id,
                "name": lead.stage_id.name,
                "is_won": lead.stage_id.is_won,
            }
            if lead.stage_id
            else False,
            "team": {"id": lead.team_id.id, "name": lead.team_id.name}
            if lead.team_id
            else False,
            "user": {"id": lead.user_id.id, "name": lead.user_id.name}
            if lead.user_id
            else False,
            "expected_revenue": lead.expected_revenue,
            "currency": {
                "id": currency.id,
                "symbol": currency.symbol,
                "position": currency.position,
            },
        }

    @api.model
    def get_crm_opportunities(self, channel_id, query="", offset=0, limit=20):
        channel = self._crm_channel(channel_id)
        if not isinstance(query, str):
            raise ValidationError(_("Invalid CRM search."))
        query = query.strip()[:128]
        limit = self._bounded_int(
            limit, default=20, minimum=1, maximum=50, label=_("limit")
        )
        offset = self._bounded_int(
            offset, default=0, minimum=0, maximum=10000, label=_("offset")
        )
        leads = self.env["crm.lead"].with_context(active_test=False)
        scope = self._crm_scope_domain(channel)
        linked = leads.search(
            scope + [("id", "in", self._crm_links(channel).mapped("lead_id").ids)]
        )
        linked_ids = set(linked.ids)
        search_domain = [("name", "ilike", query)] if query else []
        first_domain = scope + search_domain + [("id", "in", linked.ids)]
        linked_count = leads.search_count(first_domain)
        items = leads.search(
            first_domain, order="id desc", offset=offset, limit=limit + 1
        )
        if len(items) <= limit:
            related_domain = expression.AND(
                [
                    scope
                    + search_domain
                    + [("active", "=", True), ("id", "not in", linked.ids)],
                    self._crm_customer_domain(channel),
                ]
            )
            items |= leads.search(
                related_domain,
                order="id desc",
                offset=max(0, offset - linked_count),
                limit=limit + 1 - len(items),
            )
        partner, company = self._crm_customer(channel)
        can_change = channel.check_access_rights("write", raise_exception=False)
        if can_change:
            try:
                channel.check_access_rule("write")
            except AccessError:
                can_change = False
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "partner": {"id": partner.id, "name": partner.name} if partner else False,
            "commercial_partner": {"id": company.id, "name": company.name}
            if company and company != partner
            else False,
            "capabilities": {
                "view": True,
                "link": bool(can_change and partner),
                "unlink": can_change,
            },
            "linked_opportunity_ids": linked.ids,
            "items": [
                self._serialize_crm_opportunity(lead, linked_ids)
                for lead in items[:limit]
            ],
            "has_more": len(items) > limit,
        }

    def _crm_opportunity(self, channel, opportunity_id):
        lead_id = self._positive_id(opportunity_id, _("CRM record ID"))
        lead = (
            self.env["crm.lead"]
            .with_context(active_test=False)
            .search(self._crm_scope_domain(channel) + [("id", "=", lead_id)], limit=1)
        )
        if not lead:
            raise AccessError(
                _("The CRM record is unavailable or you do not have access.")
            )
        return lead

    @api.model
    def link_crm_opportunity(self, channel_id, opportunity_id):
        channel = self._crm_channel(channel_id, mutate=True)
        lead = self._crm_opportunity(channel, opportunity_id)
        lead._contact_center_lock_conversation_graph(
            channel_ids=channel.ids, touch_leads=True, touch_channels=True
        )
        self._crm_channel(channel.id, mutate=True)
        lead.check_access_rule("read")
        binding = self._binding_for_channel(channel)
        if binding.identity_id:
            self._lock_identity(binding.identity_id)
        existing = self._crm_links(channel).filtered(
            lambda link: link.lead_id.id == lead.id
        )
        if not existing and not self.env["crm.lead"].search_count(
            self._crm_scope_domain(channel)
            + self._crm_customer_domain(channel)
            + [("id", "=", lead.id), ("active", "=", True)]
        ):
            raise ValidationError(
                _("Choose an opportunity belonging to this customer.")
            )
        self.env["contact.center.crm.conversation.link"]._link(channel, lead)
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "opportunity_id": lead.id,
            "linked": True,
        }

    @api.model
    def unlink_crm_opportunity(self, channel_id, opportunity_id):
        channel = self._crm_channel(channel_id, mutate=True)
        lead = self._crm_opportunity(channel, opportunity_id)
        locked = lead._contact_center_lock_conversation_graph(
            channel_ids=channel.ids, touch_leads=True, touch_channels=True
        )
        self._crm_channel(channel.id, mutate=True)
        lead.check_access_rule("read")
        self._crm_links(channel).filtered(
            lambda link: link.lead_id.id == lead.id
        ).with_context(**locked.env.context)._tombstone()
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "opportunity_id": lead.id,
            "linked": False,
        }
