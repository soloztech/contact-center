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
    _description = "Conversation Resolution Reason"
    _order = "sequence, name, id"
    _check_company_auto = True

    name = fields.Char(string="Reason", required=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        index=True,
        default=lambda self: self.env.company,
        ondelete="restrict",
    )

    _sql_constraints = [
        (
            "name_company_unique",
            "unique(name, company_id)",
            "This reason already exists in this company.",
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
                raise ValidationError(_("Enter a reason of up to 120 characters."))

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
            raise ValidationError(_("A resolution reason cannot change company."))
        if isinstance(values.get("name"), str):
            values["name"] = values["name"].strip()
        return super().write(values)


class ContactCenterResolutionRequest(models.Model):
    _name = "contact.center.resolution.request"
    _description = "Conversation Resolution Receipt"
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
            "This resolution request has already been processed.",
        ),
    ]

    @api.model_create_multi
    def create(self, values_list):
        if (
            self.env.context.get("contact_center_productivity_service_token")
            is not CONTACT_CENTER_PRODUCTIVITY_TOKEN
        ):
            raise AccessError(
                _("Resolution receipts are created by the conversation service.")
            )
        return super().create(values_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Resolution history is immutable."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Resolution history is immutable."))


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
                _("Conversation reopened by a new customer message."),
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
        reason_id = self._positive_id(reason_id, _("resolution reason"))
        if (
            not isinstance(justification, str)
            or not justification.strip()
            or len(justification.strip()) > 500
        ):
            raise ValidationError(
                _("Enter a brief justification of up to 500 characters.")
            )
        justification = justification.strip()
        try:
            request_id = str(uuid.UUID(str(client_request_id)))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValidationError(
                _("The resolution request requires a valid UUID.")
            ) from error
        if (
            not isinstance(expected_revision, str)
            or len(expected_revision) != 64
            or any(char not in "0123456789abcdef" for char in expected_revision)
        ):
            raise ValidationError(
                _("Reopen the form to refresh the conversation state.")
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
                    _(
                        "This request has already been used with different resolution data."
                    )
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
                    (
                        "The conversation changed. Cancel and reopen the form before "
                        "resolving it."
                    )
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
            raise ValidationError(_("Select an active reason from this company."))
        reason.check_access_rights("read")
        reason.check_access_rule("read")
        body = _(
            (
                "%(actor)s resolved the conversation.\nReason: %(reason)s\n"
                "Justification: %(justification)s"
            ),
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
