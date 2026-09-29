from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, ValidationError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    contact_center_conversation_count = fields.Integer(
        string="Atendimentos",
        compute="_compute_contact_center_conversation_count",
        compute_sudo=False,
    )

    def _contact_center_check_order(self):
        self.ensure_one()
        self.env["contact.center.application"]._check_agent()
        self.check_access_rights("read")
        self.check_access_rule("read")
        if not self.exists():
            raise MissingError(_("O pedido não está mais disponível."))
        if self.company_id not in self.env.companies:
            raise AccessError(_("A empresa do pedido não está ativa nesta sessão."))

    def _contact_center_binding_domain(self):
        """Existence-only projection, derived from an authorized sales document.

        This deliberately crosses inbox rules, but never document/company scope.
        Its sudo records must not be used to serialize conversations or messages.
        """
        self._contact_center_check_order()
        customer = self.partner_id.commercial_partner_id
        company_id = self.company_id.id
        return [
            ("active", "=", True),
            ("merged_into_id", "=", False),
            ("conversation_type", "=", "direct"),
            ("company_id", "=", company_id),
            ("identity_id.company_id", "=", company_id),
            ("channel_id.contact_center_company_id", "=", company_id),
            ("channel_id.channel_type", "=", "contact_center"),
            ("identity_id.partner_id", "!=", False),
            ("identity_id.partner_id.company_id", "in", [False, company_id]),
            "|",
            ("identity_id.partner_id.commercial_partner_id", "=", customer.id),
            (
                "identity_id.partner_id.contact_center_secondary_company_ids",
                "in",
                customer.ids,
            ),
        ]

    def _contact_center_bindings(self):
        return (
            self.env["contact.center.channel.binding"]
            .sudo()
            .with_context(active_test=False)
        )

    @api.depends("partner_id", "partner_id.commercial_partner_id", "company_id")
    @api.depends_context("uid", "allowed_company_ids")
    def _compute_contact_center_conversation_count(self):
        agent = self.env.user.has_group(
            "contact_center_base.group_contact_center_agent"
        )
        for order in self:
            order.contact_center_conversation_count = (
                order._contact_center_bindings().search_count(
                    order._contact_center_binding_domain()
                )
                if agent and isinstance(order.id, int) and order.partner_id
                else 0
            )

    def action_contact_center_conversations(self):
        self._contact_center_check_order()
        return {
            "type": "ir.actions.client",
            "tag": "contact_center_sale.conversations",
            "params": {"order_id": self.id},
        }

    def get_contact_center_conversations(self, offset=0, limit=50):
        domain = self._contact_center_binding_domain()
        if type(offset) is not int or offset < 0:  # noqa: E721
            raise ValidationError(_("Página inválida."))
        if type(limit) is not int or limit < 1:  # noqa: E721
            raise ValidationError(_("Tamanho de página inválido."))
        limit = min(limit, 100)
        bindings = self._contact_center_bindings()
        total = bindings.search_count(domain)
        items = []
        api_model = self.env["contact.center.ui.api"]
        for binding in bindings.search(domain, offset=offset, limit=limit, order="id"):
            channel_id = binding.channel_id.id
            try:
                api_model._authorized_channel(channel_id)
                can_open = True
            except (AccessError, MissingError, ValidationError):
                can_open = False
            # Explicit checks are essential: Odoo shares its field cache across
            # sudo/user environments. Never fall back to a provider/phone name.
            partner = self.env["res.partner"].browse(binding.identity_id.partner_id.id)
            try:
                partner.check_access_rights("read")
                partner.check_access_rule("read")
                contact_name = partner.name or _("Contato")
            except (AccessError, MissingError):
                contact_name = _("Contato")
            items.append(
                {
                    "channel_id": channel_id if can_open else False,
                    "contact_name": contact_name,
                    "inbox_name": binding.account_id.name,
                    "can_open": can_open,
                }
            )
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(items) < total,
        }

    def open_contact_center_conversation(self, channel_id):
        domain = self._contact_center_binding_domain()
        api_model = self.env["contact.center.ui.api"]
        channel_id = api_model._positive_id(channel_id, _("conversation ID"))
        if self._contact_center_bindings().search_count(
            domain + [("channel_id", "=", channel_id)]
        ):
            try:
                # No sudo content access, even for administrators: the existing
                # API rechecks company, rules and inbox membership at open time.
                return api_model.get_conversation(channel_id)
            # pylint: disable-next=except-pass
            except (AccessError, MissingError, ValidationError):
                # Deliberately discard the cause before the uniform rejection.
                pass
        # One rejection site also keeps the RPC traceback from distinguishing a
        # hidden customer channel from an unrelated/nonexistent channel ID.
        raise AccessError(
            _("Não foi possível acessar a conversa para este pedido.")
        ) from None
