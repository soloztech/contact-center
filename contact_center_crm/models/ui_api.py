from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_base.services.dto import SCHEMA_VERSION

CUSTOMER_RECORD_MODELS = {
    "opportunities": "crm.lead",
    "quotations": "sale.order",
    "orders": "sale.order",
    "invoices": "account.move",
}


class ContactCenterUiApi(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def bootstrap(self):
        result = super().bootstrap()
        result["capabilities"]["view_crm"] = any(
            tab["available"] for tab in self._customer_record_tabs()
        )
        return result

    def _customer_record_tabs(self):
        available = {
            name: name in self.env.registry
            and self.env[name].check_access_rights("read", raise_exception=False)
            for name in set(CUSTOMER_RECORD_MODELS.values())
        }
        return [
            {"id": tab, "available": bool(available[name])}
            for tab, name in CUSTOMER_RECORD_MODELS.items()
        ]

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

    def _customer_record_selection(self, record, field_name):
        key = record[field_name]
        labels = dict(record._fields[field_name]._description_selection(record.env))
        return {"key": key, "label": labels.get(key, key) or ""}

    def _serialize_customer_record(self, record, tab, channel_id=None):
        if tab == "opportunities":
            currency = record.company_currency or record.env.company.currency_id
            amount = record.expected_revenue
            date = record.create_date
            if not record.active:
                state = (
                    {"key": "lost", "label": _("Lost")}
                    if record.probability == 0
                    else {"key": "archived", "label": _("Archived")}
                )
            elif record.stage_id.is_won:
                state = {"key": "won", "label": _("Won")}
            else:
                state = {"key": "open", "label": _("Open")}
        else:
            currency = record.currency_id
            amount = record.amount_total
            date = record.invoice_date if tab == "invoices" else record.date_order
            state = self._customer_record_selection(record, "state")
            if tab == "invoices" and record.move_type == "out_refund":
                amount = -amount
        result = {
            "id": record.id,
            "name": record.display_name
            if tab == "invoices" and record.name in (False, "/")
            else record.name,
            "model": CUSTOMER_RECORD_MODELS[tab],
            "state": state,
            "amount": amount,
            "currency": {
                "id": currency.id,
                "symbol": currency.symbol,
                "position": currency.position,
                "decimal_places": currency.decimal_places,
            },
            "date": fields.Date.to_string(date) if date else False,
        }
        if tab == "opportunities":
            result.update(
                {
                    "type": record.type,
                    "type_label": self._customer_record_selection(record, "type")[
                        "label"
                    ],
                    "active": record.active,
                    "stage": {
                        "id": record.stage_id.id,
                        "name": record.stage_id.name,
                        "is_won": record.stage_id.is_won,
                    }
                    if record.stage_id
                    else False,
                    "team": {"id": record.team_id.id, "name": record.team_id.name}
                    if record.team_id
                    else False,
                    "user": {"id": record.user_id.id, "name": record.user_id.name}
                    if record.user_id
                    else False,
                }
            )
        elif tab == "invoices":
            result.update(
                {
                    "type": record.move_type,
                    "type_label": self._customer_record_selection(record, "move_type")[
                        "label"
                    ],
                    "payment_state": self._customer_record_selection(
                        record, "payment_state"
                    ),
                }
            )
        elif tab in ("quotations", "orders"):
            result["document_tools"] = {
                "pdf_url": "/contact_center/customer/%s/sale/%s/pdf"
                % (channel_id, record.id),
                "attachments": True,
            }
        return result

    @api.model
    def get_customer_records(
        self, channel_id, tab="opportunities", query="", offset=0, limit=20
    ):
        channel, _member = self._authorized_channel(channel_id)
        if not isinstance(tab, str) or tab not in CUSTOMER_RECORD_MODELS:
            raise ValidationError(_("Invalid customer records tab."))
        if not isinstance(query, str):
            raise ValidationError(_("Invalid customer records search."))
        query = query.strip()[:128]
        limit = self._bounded_int(
            limit, default=20, minimum=1, maximum=50, label=_("limit")
        )
        offset = self._bounded_int(
            offset, default=0, minimum=0, maximum=10000, label=_("offset")
        )
        partner, company = self._crm_customer(channel)
        tabs = self._customer_record_tabs()
        available = next(item["available"] for item in tabs if item["id"] == tab)
        result = {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "tab": tab,
            "status": "ready" if available else "unavailable",
            "partner": {"id": partner.id, "name": partner.name} if partner else False,
            "commercial_partner": {"id": company.id, "name": company.name}
            if company and company != partner
            else False,
            "tabs": tabs,
            "items": [],
            "has_more": False,
            "can_create_quotation": self._customer_can_create_quotation(
                channel, partner
            ),
        }
        if not available or not partner:
            return result
        # The conversation authorizes access and identifies the customer only.
        # Native document ACL/rules apply, including when another company is
        # active in the caller's browser. No conversation-link ledger is queried.
        documents = (
            self.env[CUSTOMER_RECORD_MODELS[tab]]
            .with_company(channel.contact_center_company_id)
            .with_context(
                allowed_company_ids=[channel.contact_center_company_id.id],
                active_test=False,
            )
        )
        domain = [("partner_id.commercial_partner_id", "=", company.id)]
        if tab == "opportunities":
            domain += self._crm_scope_domain(channel)
        else:
            domain += [("company_id", "=", channel.contact_center_company_id.id)]
            if tab == "invoices":
                domain += [
                    ("move_type", "in", ["out_invoice", "out_refund"]),
                    ("state", "!=", "cancel"),
                ]
            else:
                states = ["draft", "sent"] if tab == "quotations" else ["sale", "done"]
                domain += [("state", "in", states)]
        if query:
            domain += [("name", "ilike", query)]
        records = documents.search(
            domain, order="id desc", offset=offset, limit=limit + 1
        )
        result.update(
            {
                "items": [
                    self._serialize_customer_record(record, tab, channel.id)
                    for record in records[:limit]
                ],
                "has_more": len(records) > limit,
            }
        )
        return result

    def _customer_can_create_quotation(self, channel, partner):
        return bool(
            partner
            and "sale.order" in self.env.registry
            and self.env["sale.order"]
            .with_company(channel.contact_center_company_id)
            .check_access_rights("create", raise_exception=False)
        )

    def _customer_sale_document(self, channel_id, order_id):
        channel, _member = self._authorized_channel(channel_id)
        order_id = self._positive_id(order_id, _("Sales document ID"))
        partner, commercial_partner = self._crm_customer(channel)
        if not partner or "sale.order" not in self.env.registry:
            raise AccessError(_("The sales document is not available."))
        orders = (
            self.env["sale.order"]
            .with_company(channel.contact_center_company_id)
            .with_context(allowed_company_ids=channel.contact_center_company_id.ids)
        )
        orders.check_access_rights("read")
        order = orders.search(
            [
                ("id", "=", order_id),
                ("partner_id.commercial_partner_id", "=", commercial_partner.id),
                ("company_id", "=", channel.contact_center_company_id.id),
                ("state", "in", ["draft", "sent", "sale", "done"]),
            ],
            limit=1,
        )
        if not order:
            raise AccessError(_("The sales document is not available."))
        return channel, order

    def _customer_sale_attachment_domain(self, order):
        return [
            ("res_model", "=", "sale.order"),
            ("res_id", "=", order.id),
            ("res_field", "=", False),
            ("type", "=", "binary"),
            ("company_id", "in", [False, order.company_id.id]),
        ]

    def _customer_sale_attachment(self, order, attachment_id):
        attachment_id = self._positive_id(attachment_id, _("Attachment ID"))
        attachments = order.env["ir.attachment"]
        attachments.check_access_rights("read")
        attachment = attachments.search(
            self._customer_sale_attachment_domain(order) + [("id", "=", attachment_id)],
            limit=1,
        )
        if not attachment:
            raise AccessError(_("The sales attachment is not available."))
        attachment.check("read")
        return attachment

    @api.model
    def get_customer_sale_attachments(self, channel_id, order_id, offset=0, limit=20):
        channel, order = self._customer_sale_document(channel_id, order_id)
        limit = self._bounded_int(
            limit, default=20, minimum=1, maximum=50, label=_("limit")
        )
        offset = self._bounded_int(
            offset, default=0, minimum=0, maximum=10000, label=_("offset")
        )
        attachments = order.env["ir.attachment"].search(
            self._customer_sale_attachment_domain(order),
            order="id desc",
            offset=offset,
            limit=limit + 1,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "order_id": order.id,
            "items": [
                {
                    "id": attachment.id,
                    "name": attachment.name,
                    "mimetype": attachment.mimetype or "application/octet-stream",
                    "size_bytes": attachment.file_size,
                    "url": "/contact_center/customer/%s/sale/%s/attachment/%s"
                    % (channel.id, order.id, attachment.id),
                }
                for attachment in attachments[:limit]
            ],
            "has_more": len(attachments) > limit,
        }

    @api.model
    def get_customer_quotation_action(self, channel_id):
        channel, _member = self._authorized_channel(channel_id)
        partner, _commercial_partner = self._crm_customer(channel)
        if not self._customer_can_create_quotation(channel, partner):
            raise AccessError(_("You cannot create a quotation for this customer."))
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "action": {
                "type": "ir.actions.act_window",
                "name": _("New Quotation"),
                "res_model": "sale.order",
                "views": [(False, "form")],
                "target": "new",
                "context": {
                    "default_partner_id": partner.id,
                    "default_company_id": channel.contact_center_company_id.id,
                    "allowed_company_ids": channel.contact_center_company_id.ids,
                },
            },
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
