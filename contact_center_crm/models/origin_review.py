# Cooperative ORM policy and projection extensions have separate responsibilities.
# pylint: disable=consider-merging-classes-inherited
"""Decisions for one captured origin and one business generation."""

import datetime
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .auto_origin import lead_is_open

EVIDENCE_KEY = re.compile(r"^[0-9a-f]{64}$")


class Decision(models.Model):
    _inherit = "contact.center.crm.review.decision"

    origin_reason = fields.Selection(
        [
            ("outside_window", "Fora do período"),
            ("after_closed", "Após encerramento"),
            ("business", "Revisão comercial"),
        ],
        readonly=True,
    )


class ConversationLink(models.Model):
    _inherit = "contact.center.crm.conversation.link"

    def _crm_origin_pending_reason(self, occurred_at):
        self.ensure_one()
        if self.automatic_lineage:
            if (
                self.origin_first_closed_at
                and occurred_at >= self.origin_first_closed_at
            ):
                return "after_closed"
            if not self._scope_contains(occurred_at):
                return "outside_window"
        return False

    def _crm_origin_evidence_scope(self, occurred_at, evidence_key):
        self.ensure_one()
        if self.state != "active" or self.scope_state != "confirmed":
            return "pending"
        reason = self._crm_origin_pending_reason(occurred_at)
        decision = (
            self.env["contact.center.crm.review.decision"]
            .sudo()
            .search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("kind", "=", "origin"),
                    ("link_ref", "=", self.id),
                    ("lead_ref", "=", self.lead_id.id),
                    ("evidence_key", "=", evidence_key),
                    ("origin_reason", "=", reason or "business"),
                ],
                order="id desc",
                limit=1,
            )
        )
        if decision:
            if decision.decision == "exclude":
                return "ineligible"
            if decision.decision == "review":
                return "pending"
            if decision.decision == "include" and self._scope_contains(occurred_at):
                return "eligible"
        if reason:
            return "pending"
        return "eligible" if self._scope_contains(occurred_at) else "ineligible"


class Lead(models.Model):
    _inherit = "crm.lead"

    def _journey_origin_credit(self, channel, link, evidence_key):
        """Optional provider adapter validates a captured, authorized key."""
        return False

    def _journey_origin_review_context(self, channel_id, evidence_key):
        self._journey_check()
        self.check_access_rights("write")
        self.check_access_rule("write")
        if not isinstance(evidence_key, str) or not EVIDENCE_KEY.fullmatch(
            evidence_key
        ):
            raise ValidationError(_("Invalid captured origin reference."))
        channel = self._journey_channel(channel_id)
        self.env["contact.center.ui.api"]._crm_channel(channel.id, mutate=True)
        link = self._journey_links().filtered(lambda row: row.channel_id == channel)
        if len(link) != 1 or link.scope_state != "confirmed":
            raise ValidationError(
                _("Confirm the business period before reviewing an origin.")
            )
        occurred_at = self._journey_origin_credit(channel, link, evidence_key)
        if not occurred_at:
            raise AccessError(
                _("This captured origin is unavailable for the business.")
            )
        return channel, link, occurred_at

    def action_contact_center_origin_review(self, channel_id, evidence_key):
        channel, link, occurred_at = self._journey_origin_review_context(
            channel_id, evidence_key
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": "contact.center.crm.origin.review",
            "name": _("Revisar origem"),
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "new",
            "context": {
                "default_lead_id": self.id,
                "default_channel_id": channel.id,
                "default_evidence_key": evidence_key,
                "default_scope_start": min(link.scope_start, occurred_at),
                "default_scope_end": occurred_at + datetime.timedelta(seconds=1)
                if link.scope_end and occurred_at >= link.scope_end
                else link.scope_end,
            },
        }


class OriginReview(models.TransientModel):
    _name = "contact.center.crm.origin.review"
    _description = "Revisar origem deste negócio"

    lead_id = fields.Many2one("crm.lead", required=True, readonly=True)
    channel_id = fields.Many2one("mail.channel", required=True, readonly=True)
    evidence_key = fields.Char(required=True, readonly=True)
    decision = fields.Selection(
        [
            ("include", "Incluir neste negócio"),
            ("exclude", "Excluir deste negócio"),
            ("review", "Voltar para revisão"),
        ],
        default="exclude",
        required=True,
    )
    scope_start = fields.Datetime(string="Início do período", required=True)
    scope_end = fields.Datetime(string="Fim exclusivo")

    @api.model
    def _validate_references(self, values):
        lead_id = self.env["contact.center.ui.api"]._positive_id(
            values.get("lead_id"), _("business ID")
        )
        self.env["crm.lead"].browse(lead_id)._journey_origin_review_context(
            values.get("channel_id"), values.get("evidence_key")
        )

    def onchange(self, values, field_name, field_onchange):
        self.check_access_rights("read")
        self._validate_references(
            dict(self.default_get(["lead_id", "channel_id", "evidence_key"]), **values)
        )
        return super().onchange(values, field_name, field_onchange)

    @api.model_create_multi
    def create(self, values_list):
        defaults = self.default_get(["lead_id", "channel_id", "evidence_key"])
        for values in values_list:
            self._validate_references(dict(defaults, **values))
        return super().create(values_list)

    def write(self, values):
        if {"lead_id", "channel_id", "evidence_key"}.intersection(values):
            raise AccessError(_("The review target cannot be changed."))
        return super().write(values)

    def action_confirm(self):
        self.ensure_one()
        lead = self.lead_id
        channel, link, occurred_at = lead._journey_origin_review_context(
            self.channel_id.id, self.evidence_key
        )
        lead._contact_center_lock_conversation_graph(
            channel_ids=channel.ids, touch_leads=True, touch_channels=True
        )
        channel, link, occurred_at = lead._journey_origin_review_context(
            channel.id, self.evidence_key
        )
        reason = link._crm_origin_pending_reason(occurred_at) or "business"
        if self.decision == "include":
            if reason == "after_closed" and not lead_is_open(lead):
                raise ValidationError(
                    _(
                        "Reopen the business explicitly before accepting a return interaction."
                    )
                )
            start, end = self.scope_start, self.scope_end
            if occurred_at < start or (end and occurred_at >= end):
                raise ValidationError(
                    _("The confirmed period must contain this origin.")
                )
            link = (
                self.env["contact.center.crm.conversation.link"]
                .browse(link.id)
                ._confirm_scope(start, end)
            )
        binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("channel_id", "=", channel.id),
                    ("company_id", "=", link.company_id.id),
                ],
                order="id desc",
                limit=1,
            )
        )
        decision = self.env["contact.center.crm.review.decision"]._record(
            binding,
            kind="origin",
            decision=self.decision,
            revision=link.origin_policy_revision or 1,
            lead=lead,
            link=link,
            evidence_key=self.evidence_key,
            origin_reason=link._crm_origin_pending_reason(occurred_at) or "business",
        )
        link._crm_origin_decision_changed(decision)
        return {"type": "ir.actions.act_window_close"}

    def action_split_period(self):
        self.ensure_one()
        return self.lead_id.action_contact_center_scope(self.channel_id.id)


class DecisionHooks(models.Model):
    _inherit = "contact.center.crm.conversation.link"

    def _crm_origin_decision_changed(self, decision):
        return None
