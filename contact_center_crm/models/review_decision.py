# Cooperative ORM policy and projection extensions have separate responsibilities.
# pylint: disable=consider-merging-classes-inherited
"""Private immutable decisions; native CRM and inbox rights remain authoritative."""

import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

REVIEW_TOKEN = object()


class ReviewDecision(models.Model):
    _name = "contact.center.crm.review.decision"
    _description = "Private CRM Business Review Decision"
    _order = "id desc"

    public_ref = fields.Char(
        required=True, default=lambda self: str(uuid.uuid4()), size=36, readonly=True
    )
    company_id = fields.Many2one(
        "res.company", required=True, ondelete="restrict", readonly=True, index=True
    )
    kind = fields.Selection(
        [
            ("origin", "Origem"),
            ("identity_confirmation", "Identidade desta associação"),
            ("intake_resolution", "Resolução da entrada"),
        ],
        required=True,
        readonly=True,
    )
    decision = fields.Selection(
        [
            ("include", "Incluir"),
            ("exclude", "Excluir"),
            ("review", "Revisar"),
            ("link", "Vincular contexto"),
            ("dismiss", "Encerrar sem negócio"),
        ],
        required=True,
        readonly=True,
    )
    binding_id = fields.Many2one(
        "contact.center.channel.binding", ondelete="set null", readonly=True, index=True
    )
    binding_ref = fields.Integer(readonly=True)
    identity_ref = fields.Integer(readonly=True)
    revision = fields.Integer(required=True, readonly=True)
    link_ref = fields.Integer(readonly=True, index=True)
    lead_ref = fields.Integer(readonly=True, index=True)
    evidence_key = fields.Char(size=128, readonly=True, index=True)
    comparison_mode = fields.Selection(
        [
            ("exact_phone", "Telefone completo"),
            ("br_pair", "Par BR confirmado nesta associação"),
            ("business", "Decisão de negócio"),
        ],
        readonly=True,
    )
    actor_ref = fields.Integer(required=True, readonly=True)
    decided_at = fields.Datetime(
        required=True, default=fields.Datetime.now, readonly=True
    )

    _sql_constraints = [
        (
            "public_ref_unique",
            "UNIQUE(public_ref)",
            "The review reference must be unique.",
        )
    ]

    @api.model_create_multi
    def create(self, values_list):
        if self.env.context.get("crm_review_service") is not REVIEW_TOKEN:
            raise AccessError(_("CRM review decisions are recorded internally."))
        return super().create(values_list)

    # Immutable decisions deliberately have no parent mutation path.
    # pylint: disable=method-required-super
    def write(self, values):
        raise AccessError(_("CRM review decisions are immutable."))

    def unlink(self):
        raise AccessError(_("CRM review decisions are immutable."))

    @api.model
    def _record(
        self,
        binding,
        *,
        kind,
        decision,
        revision,
        lead=False,
        link=False,
        evidence_key=False,
        comparison_mode="business",
        origin_reason=False
    ):
        return (
            self.sudo()
            .with_context(crm_review_service=REVIEW_TOKEN)
            .create(
                {
                    "public_ref": str(uuid.uuid4()),
                    "decided_at": fields.Datetime.now(),
                    "company_id": (link.company_id if link else binding.company_id).id,
                    "binding_id": binding.id,
                    "binding_ref": binding.id,
                    "identity_ref": binding.identity_id.id,
                    "kind": kind,
                    "decision": decision,
                    "revision": revision,
                    "lead_ref": lead.id if lead else 0,
                    "link_ref": link.id if link else 0,
                    "evidence_key": evidence_key,
                    "comparison_mode": comparison_mode,
                    "actor_ref": self.env.uid,
                    "origin_reason": origin_reason,
                }
            )
        )


class Binding(models.Model):
    _inherit = "contact.center.channel.binding"

    def _crm_intake_review_candidates(self, actor):
        candidates, _partner, _reason = self._crm_intake_native_candidates(
            actor, review_only=True
        )
        return candidates

    def _crm_intake_review_match(self, lead, actor):
        return "business"

    def _crm_intake_resolve_receipt(
        self, lead=False, *, kind="intake_resolution", comparison_mode="business"
    ):
        self.ensure_one()
        binding = self.sudo()
        if binding.crm_intake_state != "review":
            raise ValidationError(_("This intake review is no longer pending."))
        decision = self.env["contact.center.crm.review.decision"]._record(
            binding,
            kind=kind,
            decision="link" if lead else "dismiss",
            revision=binding.crm_intake_revision,
            lead=lead,
            comparison_mode=comparison_mode,
        )
        binding._crm_intake_write(
            {
                "crm_intake_state": "resolved",
                "crm_intake_reason": "human_resolved" if lead else "human_dismissed",
                "crm_intake_decided_at": decision.decided_at,
                "crm_intake_lead_snapshot": lead.id if lead else False,
            }
        )
        return decision

    def _crm_intake_projection(self):
        result = super()._crm_intake_projection()
        if self.sudo().crm_intake_state == "resolved":
            return {
                "state": "resolved",
                "label": _(
                    "Entrada revisada. O vínculo e o vendedor podem ser alterados no CRM."
                ),
            }
        if not result or result["state"] != "review":
            return result
        actor = api.Environment(
            self.env.cr, self.env.uid, dict(self.env.context), su=False
        )
        try:
            actor["contact.center.ui.api"]._crm_channel(self.channel_id.id, mutate=True)
            if not actor["crm.lead"].check_access_rights(
                "write", raise_exception=False
            ):
                return result
            # A display probe must not acquire the admission fence or write keys.
            candidates = self._crm_intake_review_candidates(actor)
            items = []
            has_more = False
            candidates = candidates.sorted(
                lambda row: (
                    not (
                        row.active and not row.stage_id.is_won and row.probability < 100
                    ),
                    row.id,
                )
            )
            for candidate in candidates:
                native = (
                    actor["crm.lead"]
                    .with_context(active_test=False)
                    .browse(candidate.id)
                )
                try:
                    native.check_access_rights("read")
                    native.check_access_rule("read")
                    native.check_access_rule("write")
                except AccessError:
                    continue
                if native.company_id == self.company_id:
                    if len(items) == 3:
                        has_more = True
                        break
                    items.append(
                        {
                            "id": native.id,
                            "name": native.name,
                            "closed": not native.active
                            or native.stage_id.is_won
                            or native.probability >= 100,
                        }
                    )
            result.update(
                review_revision=self.sudo().crm_intake_revision,
                review_candidates=items,
                review_has_more=has_more,
                can_dismiss=True,
            )
        except AccessError:
            return result
        return result


class UiApi(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def resolve_crm_intake_review(
        self, channel_id, revision, lead_id=False, confirm_same_business=False
    ):
        channel = self._crm_channel(channel_id, mutate=True)
        binding = self._binding_for_channel(channel)
        if type(revision) is not int or type(confirm_same_business) is not bool:
            raise ValidationError(_("Invalid intake review request."))
        self.env["crm.lead"].check_access_rights("write")
        self.env["contact.center.crm.intake.gate"]._acquire_company_ui(
            binding.company_id
        )
        binding.invalidate_recordset()
        if (
            binding.crm_intake_state != "review"
            or binding.crm_intake_revision != revision
        ):
            raise ValidationError(_("The review changed. Reload the conversation."))
        candidates = binding._crm_intake_review_candidates(self.env)
        lead = self.env["crm.lead"].with_context(active_test=False).browse()
        if lead_id:
            lead_id = self._positive_id(lead_id, _("business ID"))
            lead = lead.browse(lead_id).exists()
            if (
                not lead
                or lead.id not in candidates.ids
                or lead.company_id != binding.company_id
                or not confirm_same_business
            ):
                raise ValidationError(
                    _(
                        "Confirm the same person and business from the current review "
                        "candidates."
                    )
                )
            lead.check_access_rights("read")
            lead.check_access_rule("read")
            lead.check_access_rule("write")
        graph = lead._contact_center_lock_conversation_graph(
            channel_ids=channel.ids, touch_leads=True, touch_channels=True
        )
        self._crm_channel(channel.id, mutate=True)
        self._lock_identity(binding.identity_id)
        binding.invalidate_recordset()
        current = binding._crm_intake_review_candidates(self.env)
        if binding.crm_intake_state != "review" or (
            lead and lead.id not in current.ids
        ):
            raise ValidationError(_("The review changed. Reload the conversation."))
        if lead:
            lead.check_access_rights("read")
            lead.check_access_rule("read")
            lead.check_access_rule("write")
            self.env["contact.center.crm.conversation.link"].with_context(
                **graph.env.context
            )._link(channel, lead, writer="manual", origin="linked")
        mode = binding._crm_intake_review_match(lead, self.env) if lead else "business"
        binding._crm_intake_resolve_receipt(
            lead,
            kind="identity_confirmation" if mode == "br_pair" else "intake_resolution",
            comparison_mode=mode,
        )
        return {"schema_version": 1, "channel_id": channel.id, "resolved": True}

    @api.model
    def link_crm_opportunity(self, channel_id, opportunity_id):
        channel = self._crm_channel(channel_id, mutate=True)
        binding = self._binding_for_channel(channel)
        if binding.sudo().crm_intake_state == "review":
            # Exact-domain association still belongs to the existing endpoint.
            self.env["crm.lead"].check_access_rights("write")
            opportunity_id = self._positive_id(opportunity_id, _("business ID"))
            lead = self.env["crm.lead"].browse(opportunity_id)
            lead.check_access_rule("write")
            self.env["contact.center.crm.intake.gate"]._acquire_company_ui(
                binding.company_id
            )
        result = super().link_crm_opportunity(channel_id, opportunity_id)
        binding.invalidate_recordset()
        if binding.sudo().crm_intake_state == "review":
            binding._crm_intake_resolve_receipt(
                self.env["crm.lead"].browse(opportunity_id)
            )
        return result
