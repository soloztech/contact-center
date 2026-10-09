"""Document-bound journey; existence projections never serialize restricted rows."""
# Preserve the separate optional journey extension.
# pylint: disable=consider-merging-classes-inherited
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, ValidationError


class CrmLead(models.Model):
    _inherit = "crm.lead"

    def _journey_check(self):
        self.ensure_one()
        self.env["contact.center.application"]._check_agent()
        self.check_access_rights("read")
        self.check_access_rule("read")
        if not self.exists() or (
            self.company_id and self.company_id not in self.env.companies
        ):
            raise AccessError(_("The business is not available in this session."))

    def _journey_links(self):
        self._journey_check()
        return self._conversation_links().filtered(
            lambda row: row.company_id in self.env.companies
        )

    def _journey_context_domain(self):
        self._journey_check()
        if not self.company_id or not self.partner_id:
            return None
        try:
            customer = self.partner_id.commercial_partner_id
        except (AccessError, MissingError):
            return None
        company = self.company_id.id
        return [
            ("active", "=", True),
            ("merged_into_id", "=", False),
            ("conversation_type", "=", "direct"),
            ("company_id", "=", company),
            ("identity_id.company_id", "=", company),
            ("channel_id.contact_center_company_id", "=", company),
            ("channel_id.channel_type", "=", "contact_center"),
            ("identity_id.partner_id", "!=", False),
            ("identity_id.partner_id.company_id", "in", [False, company]),
            ("channel_id", "not in", self._journey_links().mapped("channel_id").ids),
            "|",
            ("identity_id.partner_id.commercial_partner_id", "=", customer.id),
            (
                "identity_id.partner_id.contact_center_secondary_company_ids",
                "in",
                customer.ids,
            ),
        ]

    def _journey_channel(self, channel_id):
        self._journey_check()
        api_model = self.env["contact.center.ui.api"]
        channel_id = api_model._positive_id(channel_id, _("conversation ID"))
        linked = self._journey_links().filtered(
            lambda row: row.channel_id.id == channel_id
        )
        related = bool(linked)
        if not related:
            domain = self._journey_context_domain()
            related = domain is not None and self.env[
                "contact.center.channel.binding"
            ].sudo().search_count(domain + [("channel_id", "=", channel_id)])
        if related:
            try:
                return api_model._authorized_channel(channel_id)[0]
            # Match the uniform rejection used by the native conversation picker.
            # pylint: disable-next=except-pass
            except (AccessError, MissingError, ValidationError):
                pass
        raise AccessError(
            _("The conversation is not available for this business.")
        ) from None

    def action_contact_center_journey(self):
        self._journey_check()
        return {
            "type": "ir.actions.client",
            "tag": "contact_center_crm.journey",
            "params": {"lead_id": self.id},
        }

    def _journey_origins(self, channel, link):
        """Optional Marketing extension, never required to install CRM."""
        return {"status": "unavailable", "items": []}

    def get_contact_center_origin_page(self, channel_id, offset=0):
        self._journey_check()
        if type(offset) is not int or offset < 0:
            raise ValidationError(_("Invalid origin page."))
        channel = self._journey_channel(channel_id)
        link = self._journey_links().filtered(lambda row: row.channel_id == channel)
        if len(link) != 1:
            raise AccessError(_("The business association is unavailable."))
        return self.with_context(crm_journey_origin_offset=offset)._journey_origins(
            channel, link
        )

    def get_contact_center_journey(self, area="linked", offset=0, limit=20):
        self._journey_check()
        if (
            not isinstance(area, str)
            or area not in {"linked", "context"}
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or limit < 1
        ):
            raise ValidationError(_("Invalid journey page."))
        limit = min(limit, 100)
        links = self._journey_links()
        if area == "linked":
            total, rows = len(links), links.sorted("id")[offset : offset + limit]
        else:
            domain = self._journey_context_domain()
            if domain is None:
                return {
                    "status": "unavailable",
                    "reason": "company_and_customer_required",
                    "items": [],
                    "offset": offset,
                    "limit": limit,
                    "total": 0,
                    "has_more": False,
                }
            bindings = (
                self.env["contact.center.channel.binding"]
                .sudo()
                .with_context(active_test=False)
            )
            total = bindings.search_count(domain)
            rows = bindings.search(domain, offset=offset, limit=limit, order="id")
        items = []
        api_model = self.env["contact.center.ui.api"]
        for row in rows:
            try:
                channel, _member = api_model._authorized_channel(row.channel_id.id)
            except (AccessError, MissingError, ValidationError):
                items.append({"can_open": False, "restricted": True})
                continue
            binding = api_model._binding_for_channel(channel)
            link = (
                row
                if area == "linked"
                else self.env["contact.center.crm.conversation.link"]
            )
            responsible = channel.contact_center_responsible_id
            items.append(
                {
                    "can_open": True,
                    "channel_id": channel.id,
                    "inbox_name": binding.account_id.name
                    if binding
                    else _("Conversation"),
                    "state": channel.contact_center_state,
                    "responsible": responsible.display_name if responsible else False,
                    "scope": link.scope_state if link else "customer_context",
                    "scope_decision_mode": link.scope_decision_mode if link else "none",
                    "writer": link.writer if link else False,
                    "scope_start": fields.Datetime.to_string(link.scope_start)
                    if link and link.scope_start
                    else False,
                    "scope_end": fields.Datetime.to_string(link.scope_end)
                    if link and link.scope_end
                    else False,
                    "origins": self._journey_origins(channel, link),
                }
            )
        return {
            "status": "ready",
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(items) < total,
        }

    def open_contact_center_journey_conversation(self, channel_id):
        channel = self._journey_channel(channel_id)
        return self.env["contact.center.ui.api"].get_conversation(channel.id)

    def get_contact_center_journey_history(self, channel_id, offset=0, limit=20):
        channel = self._journey_channel(channel_id)
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise ValidationError(_("Invalid history page."))
        limit = min(limit, 100)
        events = self.env["contact.center.conversation.event"]
        domain = [("channel_id", "=", channel.id)]
        empty = {"status": "restricted", "items": [], "has_more": False}
        if not events.check_access_rights("read", raise_exception=False):
            return empty
        total = events.search_count(domain)
        # Record rules may filter silently. Do not call that missing coverage.
        if total != events.sudo().search_count(domain):
            return empty
        rows = events.search(domain, offset=offset, limit=limit, order="sequence")
        first = events.search(domain, limit=1, order="sequence")
        return {
            "status": "ready",
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + len(rows) < total,
            "coverage_from": fields.Datetime.to_string(first.occurred_at)
            if first
            else False,
            "coverage_kind": first.event_type if first else "none",
            "items": [
                {
                    "sequence": r.sequence,
                    "event": r.event_type,
                    "at": fields.Datetime.to_string(r.occurred_at),
                    "responsible": r.responsible_label,
                    "actor": r.actor_label,
                    "state": r.state,
                }
                for r in rows
            ],
        }

    def action_contact_center_scope(self, channel_id):
        channel = self._journey_channel(channel_id)
        link = self._journey_links().filtered(lambda row: row.channel_id == channel)
        if not link:
            raise ValidationError(
                _(
                    "Link this conversation to the business before confirming its period."
                )
            )
        return {
            "type": "ir.actions.act_window",
            "res_model": "contact.center.crm.scope",
            "name": _("Confirm business period"),
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "new",
            "context": {
                "default_lead_id": self.id,
                "default_channel_id": channel.id,
                "default_scope_start": link.scope_start,
                "default_scope_end": link.scope_end,
            },
        }


class BusinessScope(models.TransientModel):
    _name = "contact.center.crm.scope"
    _description = "Explicit conversation business period"

    lead_id = fields.Many2one("crm.lead", required=True, readonly=True)
    channel_id = fields.Many2one("mail.channel", required=True, readonly=True)
    scope_start = fields.Datetime(string="Início do negócio", required=True)
    scope_end = fields.Datetime(string="Fim (exclusivo)")

    @api.model
    def _check_references(self, lead_id, channel_id):
        api_model = self.env["contact.center.ui.api"]
        lead_id = api_model._positive_id(lead_id, _("business ID"))
        try:
            self.env["crm.lead"].browse(lead_id).action_contact_center_scope(channel_id)
        except (AccessError, MissingError):
            raise AccessError(
                _("The business or conversation is not available.")
            ) from None

    def onchange(self, values, field_name, field_onchange):
        # Native onchange serializes unsaved many2one names with sudo. Validate
        # even when an RPC caller omits every field's onchange trigger.
        self.check_access_rights("read")
        self.check_access_rule("read")
        refs = dict(self.default_get(["lead_id", "channel_id"]), **values)
        self._check_references(refs.get("lead_id"), refs.get("channel_id"))
        return super().onchange(values, field_name, field_onchange)

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get(["lead_id", "channel_id"])
        prepared = [dict(defaults, **values) for values in vals_list]
        for values in prepared:
            self._check_references(values.get("lead_id"), values.get("channel_id"))
        return super().create(prepared)

    def write(self, values):
        if {"lead_id", "channel_id"}.intersection(values):
            raise AccessError(
                _("The assistant's business and conversation cannot be changed.")
            )
        return super().write(values)

    def action_confirm(self):
        self.ensure_one()
        channel = self.lead_id._journey_channel(self.channel_id.id)
        row = self.lead_id._journey_links().filtered(lambda r: r.channel_id == channel)
        if not row:
            raise ValidationError(_("The association is no longer active."))
        self.env["contact.center.crm.conversation.link"].browse(row.id)._confirm_scope(
            self.scope_start, self.scope_end
        )
        self.unlink()
        return {"type": "ir.actions.act_window_close"}
