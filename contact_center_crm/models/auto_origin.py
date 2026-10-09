# Cooperative ORM policy and projection extensions have separate responsibilities.
# pylint: disable=consider-merging-classes-inherited
"""New-cohort business periods; immutable evidence stays in its source models."""

import datetime

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .intake_policy import ADMIN, INTAKE_TOKEN, require_intake_admin

ORIGIN_TOKEN = object()
POLICY = {"crm_origin_auto_enabled"}
AUDIT = {"crm_origin_revision", "crm_origin_enabled_at", "crm_origin_binding_watermark"}
FIRST = {
    "crm_origin_first_source_id",
    "crm_origin_first_at",
    "crm_origin_first_revision",
    "crm_origin_recovered",
}
REVIEW_REASONS = [
    ("anchor_unavailable", "Primeira entrada não comprovada"),
    ("policy_rearmed", "Política alterada após a primeira entrada"),
    ("recovery", "Entrada recuperada: confirmar período"),
    ("before_cutoff", "Origem anterior à ativação"),
    ("outside_window", "Origem capturada fora do período"),
    ("scope_refused", "Período recusado: revisar acesso ou sobreposição"),
    ("scope_overlap", "Período sobreposto a outro negócio"),
    ("scope_access", "Acesso ao período recusado"),
    ("after_closed", "Nova interação após encerramento"),
]


def lead_is_open(lead):
    return bool(lead.active and not lead.stage_id.is_won and lead.probability < 100)


class Account(models.Model):
    _inherit = "contact.center.account"

    crm_origin_auto_enabled = fields.Boolean(
        string="Origem automática em novos leads",
        groups=ADMIN,
        copy=False,
        help="Inicia o período comercial de leads novos, com primeira entrada comprovada.",
    )
    crm_origin_revision = fields.Integer(
        default=0, readonly=True, copy=False, groups=ADMIN
    )
    crm_origin_enabled_at = fields.Datetime(readonly=True, copy=False, groups=ADMIN)
    crm_origin_binding_watermark = fields.Integer(
        readonly=True, copy=False, groups=ADMIN
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any(AUDIT.intersection(v) for v in vals_list) or any(
            "default_" + key in self.env.context for key in AUDIT
        ):
            raise AccessError(_("Commercial origin audit is managed internally."))
        if any(POLICY.intersection(v) for v in vals_list) or any(
            "default_" + key in self.env.context for key in POLICY
        ):
            require_intake_admin(self.env)
        rows = super().create(vals_list)
        rows.filtered(
            lambda row: row.sudo().crm_origin_auto_enabled
        )._crm_origin_revise(require_enabled=True)
        return rows

    def write(self, values):
        if AUDIT.intersection(values):
            raise AccessError(_("Commercial origin audit is managed internally."))
        relevant = POLICY | {"active", "company_id", "platform", "crm_intake_enabled"}
        if not relevant.intersection(values):
            return super().write(values)
        if POLICY.intersection(values):
            require_intake_admin(self.env)
        # Same company admission fence as both automatic creators, before account locks.
        for company in self.sudo().mapped("company_id").sorted("id"):
            self.env["contact.center.crm.intake.gate"]._acquire_company_ui(company)
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id=ANY(%s) ORDER BY id FOR UPDATE",
            [self.ids],
        )
        self.invalidate_recordset()
        changed = self.sudo().filtered(
            lambda row: any(
                (row[key].id if key == "company_id" else row[key]) != value
                for key, value in values.items()
                if key in relevant
            )
        )
        if values.get("crm_origin_auto_enabled"):
            for row in self.sudo():
                if not (
                    values.get("active", row.active)
                    and values.get("crm_intake_enabled", row.crm_intake_enabled)
                ):
                    raise ValidationError(
                        _(
                            "Automatic origin requires an active inbox with CRM intake enabled."
                        )
                    )
        result = super().write(values)
        if changed:
            changed._crm_origin_revise()
        return result

    def _crm_origin_revise(self, require_enabled=False):
        for row in self.sudo():
            enabled = row.crm_origin_auto_enabled
            if enabled and not (row.active and row.crm_intake_enabled):
                if require_enabled:
                    raise ValidationError(
                        _(
                            "Automatic origin requires an active inbox with CRM intake enabled."
                        )
                    )
                enabled = False
            if enabled:
                row._crm_intake_check_policy()
            newest = (
                self.env["contact.center.channel.binding"]
                .sudo()
                .with_context(active_test=False)
                .search([("account_id", "=", row.id)], order="id desc", limit=1)
            )
            values = {
                "crm_origin_auto_enabled": enabled,
                "crm_origin_revision": row.crm_origin_revision + 1,
            }
            if enabled:
                values.update(
                    crm_origin_enabled_at=fields.Datetime.now()
                    + datetime.timedelta(seconds=1),
                    crm_origin_binding_watermark=newest.id,
                )
            # Bypass only this policy extension, never generic RPC/token fields.
            super(Account, row).write(values)
        return True


class Binding(models.Model):
    _inherit = "contact.center.channel.binding"

    crm_origin_first_source_id = fields.Integer(readonly=True, copy=False, groups=ADMIN)
    crm_origin_first_at = fields.Datetime(readonly=True, copy=False, groups=ADMIN)
    crm_origin_first_revision = fields.Integer(readonly=True, copy=False, groups=ADMIN)
    crm_origin_recovered = fields.Boolean(readonly=True, copy=False, groups=ADMIN)

    @api.model_create_multi
    def create(self, vals_list):
        self._crm_origin_first_guard(vals_list)
        return super().create(vals_list)

    def write(self, values):
        self._crm_origin_first_guard([values])
        return super().write(values)

    def _crm_origin_first_guard(self, vals_list):
        if self.env.context.get("crm_intake_service") is INTAKE_TOKEN:
            return
        if any(FIRST.intersection(values) for values in vals_list) or any(
            "default_" + key in self.env.context for key in FIRST
        ):
            raise AccessError(_("First inbound evidence is managed internally."))

    def _crm_origin_note_first(self, source):
        self.ensure_one()
        row = self.sudo()
        account = row.account_id
        if not (
            account.crm_origin_auto_enabled
            and not row.crm_origin_first_source_id
            and account.crm_origin_enabled_at
            and row.id > account.crm_origin_binding_watermark
            and row.create_date >= account.crm_origin_enabled_at
            and row.conversation_type == "direct"
            and source.channel_binding_id == row
            and source.direction == "inbound"
            and source.origin == "provider"
        ):
            return
        row.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM contact_center_channel_binding WHERE id=%s FOR UPDATE NOWAIT",
            [row.id],
        )
        row.invalidate_recordset()
        if not row.crm_origin_first_source_id:
            row.with_context(crm_intake_service=INTAKE_TOKEN).write(
                {
                    "crm_origin_first_source_id": source.id,
                    "crm_origin_first_at": source.message_id.date,
                    "crm_origin_first_revision": account.crm_origin_revision,
                }
            )

    def _crm_origin_anchor(self):
        row = self.sudo()
        account = row.account_id
        if row.crm_origin_recovered:
            return False, "recovery"
        if not row.crm_origin_first_source_id:
            return False, "anchor_unavailable"
        if row.crm_origin_first_revision != account.crm_origin_revision:
            return False, "policy_rearmed"
        first = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("channel_binding_id", "=", row.id),
                    ("direction", "=", "inbound"),
                    ("origin", "=", "provider"),
                ],
                order="id",
                limit=1,
            )
        )
        if not (
            first
            and first.id == row.crm_origin_first_source_id
            and first.message_id.date == row.crm_origin_first_at
        ):
            return False, "anchor_unavailable"
        times = [row.crm_origin_first_at]
        if "contact.center.attribution.touchpoint" in self.env.registry:
            point = (
                self.env["contact.center.attribution.touchpoint"]
                .sudo()
                .search(
                    [
                        ("channel_binding_id", "=", row.id),
                    ],
                    order="occurred_at,id",
                    limit=1,
                )
            )
            if point.occurred_at:
                times.append(point.occurred_at)
        anchor = min(times)
        if not (
            row.id > account.crm_origin_binding_watermark
            and row.create_date >= account.crm_origin_enabled_at
            and anchor >= account.crm_origin_enabled_at
            and anchor >= account.crm_intake_enabled_at
        ):
            return False, "before_cutoff"
        return anchor, False

    def _crm_origin_after_intake(self, actor, source, lead, link, created):
        """Refusing a period never rolls back the newly admitted business."""
        if not created or not self.sudo().account_id.crm_origin_auto_enabled:
            return link
        reason = False
        try:
            with self.env.cr.savepoint():
                anchor, reason = self._crm_origin_anchor()
                if reason:
                    raise ValidationError(_("The first commercial entry needs review."))
                if not (
                    link.state == "active"
                    and link.writer == "intake"
                    and link.origin == "created"
                    and link.scope_state == "context"
                    and link.lead_id == lead
                    and link.channel_id == self.channel_id
                    and lead.contact_center_intake_created
                    and lead_is_open(lead)
                    and source.message_id.date >= self.account_id.crm_origin_enabled_at
                ):
                    raise ValidationError(
                        _("The business changed before its period started.")
                    )
                successor = link.with_context(
                    crm_origin_service=ORIGIN_TOKEN,
                    crm_origin_anchor_source=self.sudo().crm_origin_first_source_id,
                    crm_origin_policy_revision=self.sudo().account_id.crm_origin_revision,
                )._confirm_scope(anchor)
                self.env.flush_all()
                return successor
        except (AccessError, ValidationError) as error:
            if isinstance(error, AccessError):
                reason = "scope_access"
            elif getattr(error, "crm_scope_reason", None) == "scope_overlap":
                reason = "scope_overlap"
            # Odoo clears caches on savepoint rollback; reload the surviving generation.
            current = (
                self.env["contact.center.crm.conversation.link"]
                .sudo()
                .search(
                    [
                        ("channel_id", "=", self.channel_id.id),
                        ("lead_id", "=", lead.id),
                        ("state", "=", "active"),
                    ],
                    limit=1,
                )
            )
            if current:
                current._service().write(
                    {"origin_review_reason": reason or "scope_refused"}
                )
            return current

    def _crm_intake_projection(self):
        result = super()._crm_intake_projection()
        if result and self.sudo().crm_intake_state == "created":
            link = (
                self.env["contact.center.crm.conversation.link"]
                .sudo()
                .search(
                    [
                        ("channel_id", "=", self.channel_id.id),
                        ("state", "=", "active"),
                        ("lead_id", "=", self.sudo().crm_intake_lead_snapshot),
                    ],
                    limit=1,
                )
            )
            if link.scope_decision_mode == "automatic_intake":
                result["label"] = _(
                    "Lead criado com período comercial automático. A origem será "
                    "preenchida quando comprovada."
                )
            elif link.origin_review_reason:
                result["label"] = dict(REVIEW_REASONS)[link.origin_review_reason]
        return result


class ConversationLink(models.Model):
    _inherit = "contact.center.crm.conversation.link"

    scope_decision_mode = fields.Selection(
        [
            ("none", "Sem decisão"),
            ("human", "Humana"),
            ("automatic_intake", "Entrada automática"),
        ],
        default="none",
        required=True,
        readonly=True,
        copy=False,
    )
    automatic_lineage = fields.Boolean(readonly=True, copy=False, index=True)
    origin_first_closed_at = fields.Datetime(readonly=True, copy=False)
    origin_anchor_source_id = fields.Integer(readonly=True, copy=False)
    origin_policy_revision = fields.Integer(readonly=True, copy=False)
    origin_review_reason = fields.Selection(REVIEW_REASONS, readonly=True, copy=False)

    _sql_constraints = [
        (
            "decision_mode",
            "CHECK(((scope_state='confirmed' AND scope_decision_mode IN "
            "('human','automatic_intake')) OR (scope_state!='confirmed' AND "
            "scope_decision_mode='none')) AND (scope_decision_mode!='automatic_intake'"
            " OR (COALESCE(writer,'')='intake' AND COALESCE(origin,'')='created' AND "
            "COALESCE(automatic_lineage,FALSE) AND "
            "COALESCE(origin_anchor_source_id,0)>0 AND COALESCE(origin_policy_revision,0)>0)))",
            "Invalid business period decision provenance.",
        )
    ]

    @api.model_create_multi
    def create(self, vals_list):
        values = []
        for incoming in vals_list:
            value = dict(incoming)
            automatic = value.get("scope_decision_mode") == "automatic_intake"
            if (
                automatic
                and self.env.context.get("crm_origin_service") is not ORIGIN_TOKEN
            ):
                raise AccessError(_("Automatic periods are decided only by admission."))
            value.setdefault(
                "scope_decision_mode",
                "human" if value.get("scope_state") == "confirmed" else "none",
            )
            values.append(value)
        return super().create(values)

    def _successor_values(self, lead, scope_state, **values):
        result = super()._successor_values(lead, scope_state, **values)
        automatic = self.env.context.get("crm_origin_service") is ORIGIN_TOKEN
        result.update(
            scope_decision_mode=("automatic_intake" if automatic else "human")
            if scope_state == "confirmed"
            else "none",
            automatic_lineage=automatic
            or bool(self.automatic_lineage and self.lead_id == lead),
            origin_first_closed_at=self.origin_first_closed_at
            if self.lead_id == lead
            else False,
            origin_anchor_source_id=self.env.context.get("crm_origin_anchor_source")
            if automatic
            else self.origin_anchor_source_id
            if self.lead_id == lead
            else False,
            origin_policy_revision=self.env.context.get("crm_origin_policy_revision")
            if automatic
            else self.origin_policy_revision
            if self.lead_id == lead
            else 0,
        )
        return result


class Lead(models.Model):
    _inherit = "crm.lead"

    def _crm_origin_lineage(self):
        return (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search(
                [
                    ("lead_record_id_snapshot", "in", self.ids),
                    ("automatic_lineage", "=", True),
                ]
            )
        )

    def _crm_origin_stamp_closed(self, at):
        closed = self.filtered(lambda lead: not lead_is_open(lead))
        rows = closed._crm_origin_lineage().filtered(
            lambda row: not row.origin_first_closed_at
        )
        if rows:
            rows._service().write({"origin_first_closed_at": at})

    def write(self, values):
        scoped = self.browse()
        if {"active", "stage_id", "probability"}.intersection(values):
            scoped = (
                self.browse(
                    self._crm_origin_lineage().mapped("lead_record_id_snapshot")
                )
                & self
            )
            if scoped:
                scoped.check_access_rights("write")
                scoped.check_access_rule("write")
                scoped._contact_center_lock_conversation_graph(touch_leads=True)
        result = super().write(values)
        if scoped:
            scoped._crm_origin_stamp_closed(fields.Datetime.now())
        return result


class Stage(models.Model):
    _inherit = "crm.stage"

    def write(self, values):
        leads = self.env["crm.lead"]
        if values.get("is_won") is True:
            self.env.flush_all()
            self.env.cr.execute(
                "SELECT DISTINCT lead.id FROM crm_lead lead "
                "JOIN contact_center_crm_conversation_link link "
                "ON link.lead_record_id_snapshot=lead.id "
                "WHERE lead.stage_id=ANY(%s) AND link.automatic_lineage "
                "AND link.origin_first_closed_at IS NULL",
                [self.ids],
            )
            leads = (
                leads.sudo()
                .with_context(active_test=False)
                .browse([row[0] for row in self.env.cr.fetchall()])
            )
            if leads:
                leads._contact_center_lock_conversation_graph(touch_leads=True)
        result = super().write(values)
        if leads:
            leads._crm_origin_stamp_closed(fields.Datetime.now())
        return result
