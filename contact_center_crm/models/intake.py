"""Post-commit CRM admission; inbox projection must never depend on CRM success."""

import logging

from psycopg2 import OperationalError
from psycopg2.errors import DeadlockDetected, LockNotAvailable, SerializationFailure

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.osv import expression

from odoo.addons.queue_job.exception import RetryableJobError

from .intake_policy import (
    GATE_NAMESPACE,
    INTAKE_TOKEN,
    automation_guards_available,
    require_intake_admin,
)

_logger = logging.getLogger(__name__)
HUMAN_CONTENT_TYPES = frozenset(
    {
        "text",
        "image",
        "audio",
        "video",
        "document",
        "sticker",
        "location",
        "contacts",
        "selection",
        "live_location",
        "contact",
        "template",
        "poll",
        "interactive",
        "interactive_response",
        "buttons",
        "list",
        "media",
    }
)
EXCLUDED_CONTENT_TYPES = frozenset(
    {
        "unsupported",
        "reaction",
        "call.offer",
        "call.accept",
        "call.terminate",
        "identity.security.changed",
        "whatsapp.system",
    }
)
TERMINAL_STATES = {"created", "reused", "review"}
INTAKE_STATES = [
    ("pending", "Na fila"),
    ("created", "Lead criado"),
    ("reused", "Negócio reutilizado"),
    ("review", "Revisão necessária"),
    ("policy_changed", "Regra alterada"),
]
REASONS = [
    "ambiguous",
    "company_review",
    "inaccessible",
    "identity_unavailable",
    "source_unavailable",
    "policy_invalid",
    "automation_guard_missing",
    "manual_unlink",
    "validation",
]
RECEIPT_FIELDS = {
    "crm_intake_state",
    "crm_intake_reason",
    "crm_intake_revision",
    "crm_intake_source_id",
    "crm_intake_source_at",
    "crm_intake_admitted_at",
    "crm_intake_decided_at",
    "crm_intake_lead_snapshot",
}


def is_human_inbound(message):
    return bool(
        message.direction == "inbound"
        and message.origin == "provider"
        and message.content_type in HUMAN_CONTENT_TYPES
        and not (message.external_message_id or "").startswith("control:")
    )


class CrmLeadIntake(models.Model):
    _inherit = "crm.lead"

    contact_center_intake_created = fields.Boolean(
        readonly=True, copy=False, index=True
    )

    @api.model_create_multi
    def create(self, vals_list):
        self._crm_intake_provenance_guard(vals_list)
        return super().create(vals_list)

    def write(self, values):
        self._crm_intake_provenance_guard([values])
        return super().write(values)

    def _merge_opportunity(
        self, user_id=False, team_id=False, auto_unlink=True, max_length=5
    ):
        if any(self.sudo().mapped("contact_center_intake_created")):
            # A native merge keeps intake provenance on the survivor before any
            # merge write can trigger automation. Ordinary merges are unchanged.
            self.filtered(
                lambda lead: not lead.contact_center_intake_created
            ).with_context(crm_intake_service=INTAKE_TOKEN).write(
                {"contact_center_intake_created": True}
            )
        return super()._merge_opportunity(
            user_id=user_id,
            team_id=team_id,
            auto_unlink=auto_unlink,
            max_length=max_length,
        )

    def _crm_intake_provenance_guard(self, vals_list):
        if self.env.context.get("crm_intake_service") is not INTAKE_TOKEN and (
            any("contact_center_intake_created" in v for v in vals_list)
            or "default_contact_center_intake_created" in self.env.context
        ):
            raise AccessError(_("Automatic intake provenance is managed internally."))


class ContactCenterChannelBinding(models.Model):
    _inherit = "contact.center.channel.binding"

    # Deliberately no message FK: history retention must not erase admission.
    crm_intake_state = fields.Selection(INTAKE_STATES, readonly=True, copy=False)
    crm_intake_reason = fields.Selection(
        [(v, v) for v in REASONS],
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    crm_intake_revision = fields.Integer(readonly=True, copy=False)
    crm_intake_source_id = fields.Integer(
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    crm_intake_source_at = fields.Datetime(readonly=True, copy=False)
    crm_intake_admitted_at = fields.Datetime(readonly=True, copy=False)
    crm_intake_decided_at = fields.Datetime(readonly=True, copy=False)
    crm_intake_lead_snapshot = fields.Integer(
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )

    @api.model_create_multi
    def create(self, vals_list):
        self._crm_intake_receipt_guard(vals_list)
        return super().create(vals_list)

    def write(self, values):
        self._crm_intake_receipt_guard([values])
        return super().write(values)

    def _crm_intake_receipt_guard(self, vals_list):
        if self.env.context.get("crm_intake_service") is not INTAKE_TOKEN and (
            any(RECEIPT_FIELDS.intersection(v) for v in vals_list)
            or any("default_" + key in self.env.context for key in RECEIPT_FIELDS)
        ):
            raise AccessError(_("CRM intake receipts are managed internally."))

    def _crm_intake_write(self, values):
        return self.sudo().with_context(crm_intake_service=INTAKE_TOKEN).write(values)

    def _crm_intake_eligible(self, source):
        self.ensure_one()
        binding = self.sudo()
        account = binding.account_id
        cutoff = account.crm_intake_enabled_at
        if not (
            account.active
            and account.platform == "whatsapp"
            and account.crm_intake_enabled
            and cutoff
            and binding.active
            and not binding.merged_into_id
            and binding.conversation_type == "direct"
            and source.channel_binding_id == binding
            and is_human_inbound(source)
        ):
            return False
        if source.message_id.date < cutoff or (
            source.source_inbox_event_id
            and source.source_inbox_event_id.create_date < cutoff
        ):
            return False
        if binding.crm_intake_admitted_at:
            return binding.crm_intake_state not in TERMINAL_STATES
        if (
            binding.id <= account.crm_intake_binding_watermark
            or binding.create_date < cutoff
        ):
            return False
        first = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("channel_binding_id", "=", binding.id),
                    ("direction", "=", "inbound"),
                    ("origin", "=", "provider"),
                    ("content_type", "in", sorted(HUMAN_CONTENT_TYPES)),
                    "|",
                    ("external_message_id", "=", False),
                    "!",
                    ("external_message_id", "=like", "control:%"),
                ],
                order="id",
                limit=1,
            )
        )
        return bool(
            first
            and first.message_id.date >= cutoff
            and (
                not first.source_inbox_event_id
                or first.source_inbox_event_id.create_date >= cutoff
            )
        )

    def _crm_intake_admit(self, source):
        self.ensure_one()
        if self.crm_intake_state in TERMINAL_STATES or not self._crm_intake_eligible(
            source
        ):
            return False
        revision = self.sudo().account_id.crm_intake_revision
        if self.crm_intake_state != "pending" or self.crm_intake_revision != revision:
            self._crm_intake_write(
                {
                    "crm_intake_state": "pending",
                    "crm_intake_reason": False,
                    "crm_intake_revision": revision,
                    "crm_intake_source_id": source.id,
                    "crm_intake_source_at": source.message_id.date,
                    "crm_intake_admitted_at": self.crm_intake_admitted_at
                    or fields.Datetime.now(),
                }
            )
        self.with_delay(
            identity_key="contact_center:crm_intake:%s:%s" % (self.id, revision),
            max_retries=12,
            description="CRM intake conversation %s" % self.id,
        )._job_crm_intake(revision)
        return True

    def action_recover_crm_intake(self):
        """Explicit bounded recovery of selected, eligible conversations only."""
        require_intake_admin(self.env)
        if len(self) > 100:
            raise ValidationError(_("Select at most 100 conversations."))
        for binding in self.sudo().exists():
            if binding.company_id not in self.env.companies:
                raise AccessError(_("The inbox company is not available."))
            if binding.crm_intake_state in TERMINAL_STATES:
                continue
            source = (
                self.env["contact.center.message.binding"]
                .sudo()
                .search(
                    [
                        ("channel_binding_id", "=", binding.id),
                        ("direction", "=", "inbound"),
                        ("origin", "=", "provider"),
                        ("content_type", "in", sorted(HUMAN_CONTENT_TYPES)),
                        "|",
                        ("external_message_id", "=", False),
                        "!",
                        ("external_message_id", "=like", "control:%"),
                    ],
                    order="id desc",
                    limit=1,
                )
            )
            if source:
                binding._crm_intake_admit(source)
        return True

    def _crm_intake_projection(self):
        self.ensure_one()
        state = self.sudo().crm_intake_state
        if state == "pending" and (
            not self.sudo().account_id.crm_intake_enabled
            or self.crm_intake_revision != self.sudo().account_id.crm_intake_revision
        ):
            state = "policy_changed"
        labels = {
            "pending": _("Entrada automática no CRM aguardando processamento."),
            "created": _(
                "Lead criado pela entrada automática. "
                "Confirme o período comercial na Jornada quando apropriado."
            ),
            "reused": _(
                "Negócio existente reutilizado. "
                "O período comercial depende de confirmação na Jornada."
            ),
            "review": _(
                "Entrada automática precisa de revisão. "
                "Use a associação manual para escolher o negócio."
            ),
            "policy_changed": _(
                "A regra de entrada mudou. "
                "Uma nova mensagem elegível poderá retomar o processamento."
            ),
        }
        return {"state": state, "label": labels[state]} if state in labels else False

    def _job_crm_intake(self, revision):
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute("SHOW lock_timeout")
                previous_timeout = self.env.cr.fetchone()[0]
                self.env.cr.execute("SET LOCAL lock_timeout = '250ms'")
                result = self._crm_intake_run(revision)
                # Early decisions also defer receipt writes. Flush while every
                # wait is still bounded, before savepoint exit can flush again.
                self.env.flush_all()
                self.env.cr.execute(
                    "SELECT set_config('lock_timeout', %s, true)", [previous_timeout]
                )
                return result
        except (DeadlockDetected, LockNotAvailable, SerializationFailure) as error:
            raise RetryableJobError(
                "CRM intake contention", seconds=2, ignore_retry=True
            ) from error

    def _crm_intake_run(self, revision):
        binding = self.sudo().exists()
        if (
            not binding
            or binding.crm_intake_state != "pending"
            or binding.crm_intake_revision != revision
        ):
            return
        account = binding.account_id
        gate = (
            self.env["contact.center.crm.intake.gate"]
            .sudo()
            .search([("company_id", "=", account.company_id.id)], limit=1)
        )
        if not gate:
            return binding._crm_intake_review("policy_invalid")
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)",
            [GATE_NAMESPACE, account.company_id.id],
        )
        if not self.env.cr.fetchone()[0]:
            raise RetryableJobError(
                "CRM intake company busy", seconds=2, ignore_retry=True
            )
        gate.flush_recordset()
        self.env.cr.execute(
            "UPDATE contact_center_crm_intake_gate SET write_date = write_date WHERE id = %s",
            [gate.id],
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id = %s FOR SHARE NOWAIT",
            [account.id],
        )
        account.invalidate_recordset()
        if not account.crm_intake_enabled or account.crm_intake_revision != revision:
            return binding._crm_intake_write({"crm_intake_state": "policy_changed"})
        try:
            account._crm_intake_check_policy()
        except ValidationError:
            return binding._crm_intake_review(
                "automation_guard_missing"
                if not automation_guards_available(self.env)
                else "policy_invalid"
            )
        source = (
            self.env["contact.center.message.binding"]
            .sudo()
            .browse(binding.crm_intake_source_id)
            .exists()
        )
        if not source:
            return binding._crm_intake_review("source_unavailable")
        if not binding._crm_intake_eligible(source):
            return binding._crm_intake_review("policy_invalid")
        executor = account.crm_intake_user_id
        actor = api.Environment(
            self.env.cr, executor.id, {"allowed_company_ids": account.company_id.ids}
        )
        try:
            with self.env.cr.savepoint():
                return binding._crm_intake_decide(actor, source)
        except (AccessError, ValidationError):
            return binding._crm_intake_review("validation")

    def _crm_intake_review(self, reason):
        return self._crm_intake_write(
            {
                "crm_intake_state": "review",
                "crm_intake_reason": reason,
                "crm_intake_decided_at": fields.Datetime.now(),
            }
        )

    def _crm_intake_candidates(self, actor):
        self.ensure_one()
        api_model = actor["contact.center.ui.api"]
        channel = api_model._crm_channel(self.channel_id.id, mutate=True)
        partner = actor["res.partner"].browse(self.identity_id.partner_id.id)
        if partner:
            partner.check_access_rights("read")
            partner.check_access_rule("read")
            if partner.company_id and partner.company_id != self.company_id:
                return actor["crm.lead"], partner, "identity_unavailable"
        numbers = api_model._crm_phone_numbers(channel)
        domains = [api_model._crm_phone_domain(channel)]
        if partner:
            domains.append([("partner_id", "=", partner.id)])
        open_domain = [
            ("active", "=", True),
            "|",
            ("stage_id", "=", False),
            ("stage_id.is_won", "=", False),
            "|",
            ("probability", "=", False),
            ("probability", "<", 100),
        ]
        linked = (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search(
                [("channel_id", "=", channel.id), ("state", "=", "active")]
                + [
                    ("lead_id." + term[0], term[1], term[2])
                    if isinstance(term, tuple)
                    else term
                    for term in open_domain
                ],
                limit=2,
            )
            .mapped("lead_id")
        )
        if linked:
            candidates = linked
        else:
            candidates = (
                actor["crm.lead"]
                .sudo()
                .search(
                    expression.AND(
                        [
                            open_domain,
                            expression.OR(domains),
                            [("company_id", "in", [False, self.company_id.id])],
                        ]
                    ),
                    limit=2,
                )
            )
        if len(candidates) > 1:
            return candidates, partner, "ambiguous"
        if candidates and candidates.company_id != self.company_id:
            return candidates, partner, "company_review"
        if candidates:
            lead = actor["crm.lead"].browse(candidates.id)
            try:
                lead.check_access_rights("read")
                lead.check_access_rule("read")
            except AccessError:
                return candidates, partner, "inaccessible"
        if not candidates and not partner and len(numbers) != 1:
            return candidates, partner, "identity_unavailable"
        return candidates, partner, False

    def _crm_intake_decide(self, actor, source):
        candidates, partner, reason = self._crm_intake_candidates(actor)
        if reason:
            return self._crm_intake_review(reason)
        # Use the established optional-pipeline/Marketing graph orchestration.
        graph = (
            actor["crm.lead"]
            .browse(candidates.ids)
            ._contact_center_lock_conversation_graph(
                channel_ids=self.channel_id.ids, touch_leads=True, touch_channels=True
            )
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_identity WHERE id = %s FOR UPDATE NOWAIT",
            [self.identity_id.id],
        )
        self.invalidate_recordset()
        self.identity_id.invalidate_recordset()
        candidates, partner, reason = self._crm_intake_candidates(actor)
        if reason:
            return self._crm_intake_review(reason)
        retired = (
            self.env["contact.center.crm.conversation.link"]
            .sudo()
            .search_count(
                [
                    ("channel_id", "=", self.channel_id.id),
                    ("state", "=", "unlinked"),
                    ("unlinked_reason", "=", "manual"),
                ]
            )
        )
        if retired:
            return self._crm_intake_review("manual_unlink")
        lead = graph.browse(candidates.ids)
        created = not lead
        if created:
            if not automation_guards_available(actor):
                return self._crm_intake_review("automation_guard_missing")
            account = self.account_id
            responsible = self.channel_id.contact_center_responsible_id
            if not account._crm_intake_valid_user(responsible):
                responsible = account.crm_intake_user_id
            numbers = actor["contact.center.ui.api"]._crm_phone_numbers(
                actor["mail.channel"].browse(self.channel_id.id)
            )
            values = {
                "name": ("WhatsApp: " + (self.identity_id.name or _("Novo contato")))[
                    :128
                ],
                "company_id": self.company_id.id,
                "type": "lead",
                "user_id": responsible.id,
                "team_id": account.crm_intake_team_id.id or False,
                "partner_id": partner.id or False,
                "contact_center_intake_created": True,
            }
            if len(numbers) == 1:
                values["mobile" if partner else "phone"] = "+" + next(iter(numbers))
            if "cc_automation_paused" in lead._fields:
                values["cc_automation_paused"] = True
            if "iap_enrich_done" in lead._fields:
                values["iap_enrich_done"] = True
            lead = lead.with_context(
                crm_intake_service=INTAKE_TOKEN,
                mail_create_nosubscribe=True,
                mail_auto_subscribe_no_notify=True,
                mail_notrack=True,
                mail_create_nolog=True,
            ).create(values)
            lead._message_log(
                body=_(
                    "Lead criado pela entrada automática da caixa comercial. "
                    "A conversa permanece no Contact Center."
                )
            )
        actor["contact.center.crm.conversation.link"].with_context(
            **graph.env.context
        )._link(
            actor["mail.channel"].browse(self.channel_id.id),
            lead,
            origin="created" if created else "linked",
            writer="intake",
        )
        return self._crm_intake_write(
            {
                "crm_intake_state": "created" if created else "reused",
                "crm_intake_reason": False,
                "crm_intake_decided_at": fields.Datetime.now(),
                "crm_intake_lead_snapshot": lead.id,
            }
        )


class ContactCenterMessageBinding(models.Model):
    _inherit = "contact.center.message.binding"

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        for message in messages:
            if message.direction != "inbound" or message.origin != "provider":
                continue
            binding = message.sudo().channel_binding_id
            if not binding.account_id.crm_intake_enabled:
                continue
            # Host-ingress flush errors belong to the caller. Only optional
            # CRM admission/queue work is isolated by the fail-open savepoint.
            self.env.flush_all()
            try:
                with self.env.cr.savepoint():
                    message.sudo().channel_binding_id._crm_intake_admit(message.sudo())
            except OperationalError:
                raise
            except Exception as error:  # Keep the already persisted message.
                _logger.warning(
                    "CRM intake admission deferred for channel binding %s "
                    "(message binding %s; %s)",
                    binding.id,
                    message.id,
                    type(error).__name__,
                )
        return messages
