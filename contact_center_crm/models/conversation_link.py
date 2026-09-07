"""Conversation/CRM association, independent of pipelines and service cases."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

CRM_CONVERSATION_GRAPH_LOCK_TOKEN = object()
CRM_CONVERSATION_LINK_TOKEN = object()


def conversation_graph_is_locked(env):
    return (
        env.context.get("contact_center_crm_conversation_graph_lock")
        is CRM_CONVERSATION_GRAPH_LOCK_TOKEN
    )


def lock_conversation_graph(
    env, *, lead_ids=(), channel_ids=(), touch_leads=False, touch_channels=False
):
    """Serialize topology changes in lead → conversation order.

    Parent row versions also fence waiters using Odoo's REPEATABLE READ snapshot.
    Addons with earlier locks extend crm.lead's orchestration method, rather than
    taking these locks directly.
    """
    lead_ids = sorted(set(lead_ids))
    channels = set(channel_ids)
    if lead_ids:
        env["crm.lead"].flush_model(["company_id", "team_id", "stage_id", "write_date"])
        env.cr.execute(
            "SELECT id FROM crm_lead WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [lead_ids],
        )
        lead_ids = [row[0] for row in env.cr.fetchall()]
        if touch_leads:
            env.cr.execute(
                "UPDATE crm_lead SET write_date = write_date WHERE id = ANY(%s)",
                [lead_ids],
            )
        env["contact.center.crm.conversation.link"].sudo().flush_model(
            ["channel_id", "lead_id", "state"]
        )
        links = (
            env["contact.center.crm.conversation.link"]
            .sudo()
            .search([("lead_id", "in", lead_ids), ("state", "=", "active")])
        )
        channels.update(links.mapped("channel_id").ids)
    channel_ids = sorted(channels)
    if channel_ids:
        env["mail.channel"].flush_model(["contact_center_company_id", "write_date"])
        env.cr.execute(
            "SELECT id FROM mail_channel WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [channel_ids],
        )
        channel_ids = [row[0] for row in env.cr.fetchall()]
        if touch_channels:
            env.cr.execute(
                "UPDATE mail_channel SET write_date = write_date WHERE id = ANY(%s)",
                [channel_ids],
            )
    leads = env["crm.lead"].browse(lead_ids)
    channels = env["mail.channel"].browse(channel_ids)
    leads.invalidate_recordset(["company_id", "team_id", "stage_id", "write_date"])
    channels.invalidate_recordset(["contact_center_company_id", "write_date"])
    return leads, channels


class ContactCenterCrmConversationLink(models.Model):
    _name = "contact.center.crm.conversation.link"
    _description = "Contact Center Conversation CRM Link"
    _order = "id"
    _check_company_auto = True

    channel_id = fields.Many2one(
        "mail.channel", required=True, index=True, ondelete="cascade", readonly=True
    )
    company_id = fields.Many2one(
        related="channel_id.contact_center_company_id", store=True, index=True
    )
    lead_id = fields.Many2one(
        "crm.lead", index=True, ondelete="set null", readonly=True, check_company=True
    )
    state = fields.Selection(
        [("active", "Active"), ("unlinked", "Unlinked")],
        default="active",
        required=True,
        readonly=True,
        index=True,
    )
    origin = fields.Selection(
        [("created", "Created from Contact Center"), ("linked", "Linked Existing")],
        required=True,
        readonly=True,
        default="linked",
    )
    linked_at = fields.Datetime(
        default=fields.Datetime.now, required=True, readonly=True
    )
    linked_by_id = fields.Many2one(
        "res.users", default=lambda self: self.env.user, required=True, readonly=True
    )
    unlinked_at = fields.Datetime(readonly=True)
    unlinked_by_id = fields.Many2one("res.users", readonly=True)
    unlinked_reason = fields.Char(readonly=True)
    lead_record_id_snapshot = fields.Integer(readonly=True)

    _sql_constraints = [
        (
            "lifecycle",
            "CHECK((state = 'active' AND lead_id IS NOT NULL) OR "
            "(state = 'unlinked' AND lead_id IS NULL))",
            "Invalid CRM link lifecycle.",
        ),
    ]

    def init(self):
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS contact_center_crm_conversation_live_unique "
            "ON contact_center_crm_conversation_link (channel_id, lead_id) "
            "WHERE state = 'active'"
        )

    @api.model_create_multi
    def create(self, vals_list):
        self._check_service_token()
        return super().create(vals_list)

    def write(self, values):
        self._check_service_token()
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        # The ledger is append-only; even internal callers must retire a link.
        raise AccessError(_("Use the conversation CRM panel to remove a link."))

    def _check_service_token(self):
        if (
            self.env.context.get("contact_center_crm_link_service")
            is not CRM_CONVERSATION_LINK_TOKEN
        ):
            raise AccessError(_("Manage CRM links from the conversation."))

    def _service(self):
        return self.sudo().with_context(
            contact_center_crm_link_service=CRM_CONVERSATION_LINK_TOKEN
        )

    @api.model
    def _link(self, channel, lead, origin="linked"):
        channel.ensure_one()
        lead.ensure_one()
        channel.check_access_rights("read")
        channel.check_access_rule("read")
        lead.check_access_rights("read")
        lead.check_access_rule("read")
        locked = lead._contact_center_lock_conversation_graph(
            channel_ids=channel.ids, touch_leads=True, touch_channels=True
        )
        channel.invalidate_recordset(["contact_center_company_id", "channel_type"])
        lead.invalidate_recordset(["company_id", "user_id", "team_id", "partner_id"])
        channel.check_access_rule("read")
        lead.check_access_rule("read")
        company = channel.contact_center_company_id
        if (
            channel.channel_type != "contact_center"
            or not company
            or company not in self.env.companies
            or (lead.company_id and lead.company_id != company)
        ):
            raise ValidationError(
                _("The CRM record and conversation must belong to the same company.")
            )
        links = self.with_context(**locked.env.context)._service()
        existing = links.search(
            [
                ("channel_id", "=", channel.id),
                ("lead_id", "=", lead.id),
                ("state", "=", "active"),
            ],
            limit=1,
        )
        if existing:
            return existing
        result = links.create(
            {
                "channel_id": channel.id,
                "lead_id": lead.id,
                "origin": origin,
                "lead_record_id_snapshot": lead.id,
                "linked_by_id": self.env.uid,
            }
        )
        lead.invalidate_recordset(["contact_center_conversation_count"])
        return result

    def _lock_crm_graph(self):
        leads = (
            self.sudo()
            .mapped("lead_id")
            ._contact_center_lock_conversation_graph(
                channel_ids=self.sudo().mapped("channel_id").ids,
                touch_leads=True,
                touch_channels=True,
            )
        )
        self.invalidate_recordset()
        return self.with_context(**leads.env.context)

    def _before_tombstone(self, reason):
        """Extension point, called with live links and the whole graph locked."""
        return True

    def _tombstone(self, reason="manual"):
        links = self._lock_crm_graph().filtered(lambda link: link.state == "active")
        if not links:
            return True
        leads = links.mapped("lead_id")
        links._before_tombstone(reason)
        links._service().write(
            {
                "state": "unlinked",
                "lead_id": False,
                "unlinked_at": fields.Datetime.now(),
                "unlinked_by_id": self.env.uid,
                "unlinked_reason": reason,
            }
        )
        leads.invalidate_recordset(["contact_center_conversation_count"])
        return True

    def _transfer_to_lead(self, lead):
        """Preserve associations during native CRM merge; collapse duplicates."""
        leads = self.mapped("lead_id") | lead
        for link in self.sorted("id"):
            if lead.company_id and lead.company_id != link.company_id:
                raise ValidationError(
                    _("Linked conversations cannot move to another company.")
                )
            duplicate = self.sudo().search(
                [
                    ("channel_id", "=", link.channel_id.id),
                    ("lead_id", "=", lead.id),
                    ("state", "=", "active"),
                    ("id", "!=", link.id),
                ],
                limit=1,
            )
            if duplicate:
                link._tombstone("merged")
            else:
                link._service().write(
                    {"lead_id": lead.id, "lead_record_id_snapshot": lead.id}
                )
        leads.invalidate_recordset(["contact_center_conversation_count"])
