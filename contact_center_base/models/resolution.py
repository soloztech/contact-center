"""Reusable resolution reasons and transaction-bound conversation audit notes."""

import hashlib
import json
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.dto import SCHEMA_VERSION
from ..services.tokens import CONTACT_CENTER_PRODUCTIVITY_TOKEN

# The reason, receipt and service extensions form one bounded resolution lane.
# pylint: disable=consider-merging-classes-inherited

_RESOLUTION_TRANSITION_TOKEN = object()


class ContactCenterResolutionReason(models.Model):
    _name = "contact.center.resolution.reason"
    _description = "Motivo de resolução da conversa"
    _order = "sequence, name, id"
    _check_company_auto = True

    name = fields.Char(string="Motivo", required=True)
    active = fields.Boolean(string="Ativo", default=True)
    sequence = fields.Integer(string="Ordem", default=10)
    company_id = fields.Many2one(
        "res.company",
        string="Empresa",
        required=True,
        index=True,
        default=lambda self: self.env.company,
        ondelete="restrict",
    )

    _sql_constraints = [
        (
            "name_company_unique",
            "unique(name, company_id)",
            "O motivo já está cadastrado nesta empresa.",
        ),
    ]

    @api.constrains("name")
    def _check_name(self):
        for reason in self:
            if (
                not isinstance(reason.name, str)
                or not reason.name.strip()
                or len(reason.name) > 120
            ):
                raise ValidationError(_("Informe um motivo de até 120 caracteres."))

    @api.model_create_multi
    def create(self, values_list):
        prepared = []
        for values in values_list:
            values = dict(values)
            if isinstance(values.get("name"), str):
                values["name"] = values["name"].strip()
            prepared.append(values)
        return super().create(prepared)

    def write(self, values):
        values = dict(values)
        if "company_id" in values and any(
            reason.company_id.id != values["company_id"] for reason in self
        ):
            raise ValidationError(
                _("Um motivo de resolução não pode mudar de empresa.")
            )
        if isinstance(values.get("name"), str):
            values["name"] = values["name"].strip()
        return super().write(values)


class ContactCenterResolutionRequest(models.Model):
    _name = "contact.center.resolution.request"
    _description = "Recibo de resolução da conversa"
    _order = "id desc"

    channel_id = fields.Many2one(
        "mail.channel", required=True, readonly=True, index=True, ondelete="cascade"
    )
    company_id = fields.Many2one(
        related="channel_id.contact_center_company_id", store=True, index=True
    )
    reason_id = fields.Many2one(
        "contact.center.resolution.reason",
        required=True,
        readonly=True,
        ondelete="restrict",
    )
    requested_by_id = fields.Many2one(
        "res.users", required=True, readonly=True, ondelete="restrict"
    )
    ui_request_id = fields.Char(required=True, readonly=True, index=True)
    payload_sha256 = fields.Char(required=True, readonly=True)
    note_request_id = fields.Many2one(
        "contact.center.internal.note.request",
        required=True,
        readonly=True,
        ondelete="cascade",
    )

    _sql_constraints = [
        (
            "channel_request_unique",
            "unique(channel_id, ui_request_id)",
            "Esta solicitação de resolução já foi processada.",
        ),
    ]

    @api.model_create_multi
    def create(self, values_list):
        if (
            self.env.context.get("contact_center_productivity_service_token")
            is not CONTACT_CENTER_PRODUCTIVITY_TOKEN
        ):
            raise AccessError(
                _("Os recibos de resolução são criados pelo serviço de atendimento.")
            )
        return super().create(values_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("O histórico de resolução é imutável."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("O histórico de resolução é imutável."))


class ContactCenterApplicationResolution(models.AbstractModel):
    _inherit = "contact.center.application"

    def _apply_inbound_conversation_lifecycle(self, binding):
        reopened = super()._apply_inbound_conversation_lifecycle(binding)
        if reopened:
            # The caller holds the projection lock and has already ruled out
            # duplicate, control and self-side events. The note rolls back with
            # the new inbound message, and never advances customer activity.
            self.env["contact.center.ui.api"]._persist_internal_note(
                binding.channel_id,
                _("Conversa reaberta por nova mensagem do cliente."),
                "inbound-reopen:%s" % uuid.uuid4(),
            )
        return reopened


class ContactCenterUiApiResolution(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def _resolution_revision(self, channel):
        channel.ensure_one()
        values = (
            channel.id,
            channel.contact_center_state,
            str(channel.write_date or ""),
            channel.contact_center_last_message_id.id or False,
        )
        return hashlib.sha256(json.dumps(values).encode("utf-8")).hexdigest()

    @api.model
    def resolution_reason_catalog(self, channel_id):
        channel, _member = self._authorized_channel(channel_id)
        reasons = self.env["contact.center.resolution.reason"].search(
            [
                ("company_id", "=", channel.contact_center_company_id.id),
                ("active", "=", True),
            ]
        )
        return {
            "items": [{"id": reason.id, "name": reason.name} for reason in reasons],
            "can_manage": self.env.user.has_group(
                "contact_center_base.group_contact_center_supervisor"
            )
            and self.env["contact.center.resolution.reason"].check_access_rights(
                "create", raise_exception=False
            ),
            "revision": self._resolution_revision(channel),
        }

    @api.model
    def resolve_conversation(
        self, channel_id, reason_id, justification, client_request_id, expected_revision
    ):
        channel, _member = self._authorized_channel(channel_id)
        reason_id = self._positive_id(reason_id, _("motivo de resolução"))
        if (
            not isinstance(justification, str)
            or not justification.strip()
            or len(justification.strip()) > 500
        ):
            raise ValidationError(
                _("Informe uma justificativa breve, de até 500 caracteres.")
            )
        justification = justification.strip()
        try:
            request_id = str(uuid.UUID(str(client_request_id)))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValidationError(
                _("A solicitação de resolução precisa de um UUID válido.")
            ) from error
        if (
            not isinstance(expected_revision, str)
            or len(expected_revision) != 64
            or any(char not in "0123456789abcdef" for char in expected_revision)
        ):
            raise ValidationError(
                _("Reabra o formulário para atualizar o estado da conversa.")
            )
        payload_sha256 = hashlib.sha256(
            json.dumps([reason_id, justification, expected_revision]).encode("utf-8")
        ).hexdigest()
        # The existing fence owns the channel row and forces a transaction retry
        # instead of reusing a stale REPEATABLE READ snapshot after a waiter.
        self._productivity_fence_channel(channel)
        channel, _member = self._authorized_channel(channel.id)
        channel.invalidate_recordset(
            ["contact_center_state", "write_date", "contact_center_last_message_id"]
        )
        receipts = self.env["contact.center.resolution.request"].sudo()
        receipt = receipts.search(
            [("channel_id", "=", channel.id), ("ui_request_id", "=", request_id)],
            limit=1,
        )
        if receipt:
            if (
                receipt.requested_by_id != self.env.user
                or receipt.payload_sha256 != payload_sha256
            ):
                raise ValidationError(
                    _("Esta solicitação já foi usada com outros dados de resolução.")
                )
            self._internal_note_message(channel, receipt.note_request_id)
            return {
                "schema_version": SCHEMA_VERSION,
                "item": self._serialize_conversation(channel),
                "replayed": True,
            }
        if (
            channel.contact_center_state != "open"
            or self._resolution_revision(channel) != expected_revision
        ):
            raise ValidationError(
                _(
                    "A conversa foi atualizada. Cancele e reabra o formulário "
                    "antes de resolver."
                )
            )
        reason = self.env["contact.center.resolution.reason"].search(
            [
                ("id", "=", reason_id),
                ("active", "=", True),
                ("company_id", "=", channel.contact_center_company_id.id),
            ],
            limit=1,
        )
        if not reason:
            raise ValidationError(_("Selecione um motivo ativo desta empresa."))
        reason.check_access_rights("read")
        reason.check_access_rule("read")
        body = _(
            "%(actor)s resolveu a conversa.\nMotivo: %(reason)s\n"
            "Justificativa: %(justification)s",
            actor=self.env.user.display_name,
            reason=reason.name,
            justification=justification,
        )
        result = self.with_context(
            contact_center_resolution_transition_token=_RESOLUTION_TRANSITION_TOKEN
        ).update_conversation(channel.id, {"state": "resolved"})
        note_key = "resolution:%s" % request_id
        self._persist_internal_note(channel, body, note_key)
        note_request = (
            self.env["contact.center.internal.note.request"]
            .sudo()
            .search(
                [("channel_id", "=", channel.id), ("ui_request_id", "=", note_key)],
                limit=1,
            )
        )
        receipts.with_context(
            contact_center_productivity_service_token=CONTACT_CENTER_PRODUCTIVITY_TOKEN
        ).create(
            {
                "channel_id": channel.id,
                "reason_id": reason.id,
                "requested_by_id": self.env.uid,
                "ui_request_id": request_id,
                "payload_sha256": payload_sha256,
                "note_request_id": note_request.id,
            }
        )
        result["replayed"] = False
        return result

    def _post_conversation_transition_note(
        self, channel, *, previous_state, previous_responsible, claimed=False
    ):
        if (
            self.env.context.get("contact_center_resolution_transition_token")
            is _RESOLUTION_TRANSITION_TOKEN
            and previous_state == "open"
            and channel.contact_center_state == "resolved"
            and previous_responsible == channel.contact_center_responsible_id
            and not claimed
        ):
            return self.env["mail.message"]
        return super()._post_conversation_transition_note(
            channel,
            previous_state=previous_state,
            previous_responsible=previous_responsible,
            claimed=claimed,
        )
