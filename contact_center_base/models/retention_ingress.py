"""Content-free expiry receipts and admission barriers for group retention."""

import datetime

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.tokens import CONTACT_CENTER_DELETION_TOKEN

# These fragments form one retention admission service across native models.
# pylint: disable=consider-merging-classes-inherited
_RETENTION_INDEX_TOKEN = object()
_INDEX_CONTEXT = "contact_center_retention_index_token"
_INDEX_FIELDS = frozenset(
    (
        "retention_route_indexed",
        "retention_route_ref",
        "retention_occurred_at",
        "retention_event_kind",
        "retention_message_id",
        "retention_external_ids_json",
        "retention_target_id",
        "retention_multiple_targets",
        "retention_reply_id",
        "retention_multiple_quotes",
        "retention_reply_ids_json",
    )
)


class ContactCenterRetentionReceipt(models.Model):
    _name = "contact.center.retention.receipt"
    _description = "Expired Contact Center Message Receipt"

    account_id = fields.Many2one(
        "contact.center.account", required=True, index=True, ondelete="restrict"
    )
    company_id = fields.Many2one(
        related="account_id.company_id", store=True, index=True
    )
    conversation_ref = fields.Char(required=True, index=True)
    external_message_id = fields.Char(required=True, index=True)

    _sql_constraints = [
        (
            "account_group_message_unique",
            "unique(account_id, conversation_ref, external_message_id)",
            "This expired group message already has a receipt.",
        )
    ]

    @api.model_create_multi
    def create(self, vals_list):
        self._retention_check_service()
        return super().create(vals_list)

    def write(self, values):
        self._retention_check_service()
        return super().write(values)

    def unlink(self):
        self._retention_check_service()
        return super().unlink()

    def _retention_check_service(self):
        if (
            self.env.context.get("contact_center_deletion_token")
            is not CONTACT_CENTER_DELETION_TOKEN
        ):
            raise AccessError(_("Expiry receipts are managed by message retention."))


class ContactCenterRetentionIngress(models.AbstractModel):
    _inherit = "contact.center.retention"

    def _record_expired_messages(self, binding, message_bindings):
        """Keep only protocol identifiers, scoped across connection replacements."""
        binding.ensure_one()
        if any(item.channel_binding_id != binding for item in message_bindings):
            raise ValidationError(_("Expired messages must belong to one group."))
        ids = set(filter(None, message_bindings.mapped("external_message_id")))
        return self._record_expired_ids(binding, ids)

    def _record_expired_ids(self, binding, external_ids):
        model = self.env["contact.center.retention.receipt"].sudo()
        ids = set(filter(None, external_ids))
        existing = model.search(
            [
                ("account_id", "=", binding.account_id.id),
                ("conversation_ref", "=", binding.conversation_ref),
                ("external_message_id", "in", sorted(ids)),
            ]
        )
        missing = ids - set(existing.mapped("external_message_id"))
        if missing:
            model.with_context(
                contact_center_deletion_token=CONTACT_CENTER_DELETION_TOKEN
            ).create(
                [
                    {
                        "account_id": binding.account_id.id,
                        "conversation_ref": binding.conversation_ref,
                        "external_message_id": external_id,
                    }
                    for external_id in sorted(missing)
                ]
            )
        return True

    def _retention_route(self, connection, envelope):
        if connection.adapter_key != "wuzapi":
            return None
        reader = getattr(connection.get_adapter(), "retention_route", None)
        return reader(connection, envelope) if reader else None

    def _retention_expired_ids(self, account, reference, external_ids):
        if not external_ids:
            return set()
        return set(
            self.env["contact.center.retention.receipt"]
            .sudo()
            .search(
                [
                    ("account_id", "=", account.id),
                    ("conversation_ref", "=", reference),
                    ("external_message_id", "in", list(external_ids)),
                ]
            )
            .mapped("external_message_id")
        )

    def _retention_route_is_expired(self, connection, route):
        if not route or route.get("conversation_type") != "group":
            return False
        account = connection.account_id.sudo()
        self.env["contact.center.application"]._lock_inbound_account_scope(account)
        ids = set(route.get("external_ids") or [])
        expired = self._retention_expired_ids(account, route["conversation_ref"], ids)
        # Mixed receipts still carry evidence for surviving messages. They contain
        # no message body and the normal receipt handler skips unknown targets.
        if ids and ids <= expired:
            return True
        occurred_at = route.get("occurred_at")
        if not occurred_at or route.get("kind") not in ("message", "control"):
            return False
        binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("account_id", "=", account.id),
                    ("conversation_type", "=", "group"),
                    ("conversation_ref", "=", route["conversation_ref"]),
                    ("retention_expired_before", "!=", False),
                ]
            )
        )
        return any(occurred_at < item.retention_expired_before for item in binding)

    def _retention_envelope_is_expired(self, connection, envelope):
        return self._retention_route_is_expired(
            connection, self._retention_route(connection, envelope)
        )

    def _retention_sanitize_envelope(self, connection, envelope, route=None):
        route = route or self._retention_route(connection, envelope)
        if not route or not route.get("reply_ids"):
            return envelope
        expired = self._retention_expired_ids(
            connection.account_id, route["conversation_ref"], route["reply_ids"]
        )
        if not expired:
            return envelope
        return connection.get_adapter().retention_remove_quotes(envelope, expired)


class ContactCenterRetentionInbox(models.Model):
    _inherit = "contact.center.inbox.event"

    retention_route_indexed = fields.Boolean(
        default=False, index=True, readonly=True, copy=False
    )
    retention_route_ref = fields.Char(index=True, readonly=True, copy=False)
    retention_occurred_at = fields.Datetime(index=True, readonly=True, copy=False)
    retention_event_kind = fields.Char(index=True, readonly=True, copy=False)
    retention_message_id = fields.Char(index=True, readonly=True, copy=False)
    retention_external_ids_json = fields.Json(readonly=True, copy=False)
    retention_target_id = fields.Char(index=True, readonly=True, copy=False)
    retention_multiple_targets = fields.Boolean(index=True, readonly=True, copy=False)
    retention_reply_id = fields.Char(index=True, readonly=True, copy=False)
    retention_multiple_quotes = fields.Boolean(index=True, readonly=True, copy=False)
    retention_reply_ids_json = fields.Json(readonly=True, copy=False)

    @api.model
    def _retention_index_values(self, connection, envelope, fallback=None):
        route = self.env["contact.center.retention"]._retention_route(
            connection, envelope
        )
        values = {name: False for name in _INDEX_FIELDS}
        values["retention_route_indexed"] = True
        if route:
            reply_ids = route.get("reply_ids") or []
            external_ids = route.get("external_ids") or []
            values.update(
                retention_route_ref=route["conversation_ref"],
                retention_occurred_at=route.get("occurred_at")
                or fallback
                or fields.Datetime.now(),
                retention_event_kind=route["kind"],
                retention_message_id=route.get("message_id") or False,
                retention_external_ids_json=external_ids,
                retention_target_id=external_ids[0] if external_ids else False,
                retention_multiple_targets=len(external_ids) > 1,
                retention_reply_id=reply_ids[0] if reply_ids else False,
                retention_multiple_quotes=len(reply_ids) > 1,
                retention_reply_ids_json=reply_ids,
            )
        return values

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        service = self.env["contact.center.retention"]
        for values in vals_list:
            if _INDEX_FIELDS.intersection(values):
                raise AccessError(
                    _("Retention routing is derived from provider events.")
                )
            values = dict(values)
            connection = (
                self.env["contact.center.provider.connection"]
                .sudo()
                .browse(values.get("provider_connection_id"))
            )
            envelope = values.get("raw_envelope_json") or {}
            route = service._retention_route(connection, envelope)
            values.update(self._retention_index_values(connection, envelope))
            if service._retention_route_is_expired(connection, route):
                values.update(
                    raw_envelope_json={"content_erased": True},
                    normalized_dto_json=False,
                    metadata_json={
                        "content_erased": True,
                        "reason": "retention_expired",
                    },
                    state="blocked",
                    last_error_class="ConversationContentErased",
                    last_error_message=False,
                )
            elif route:
                values["raw_envelope_json"] = service._retention_sanitize_envelope(
                    connection, envelope, route=route
                )
            prepared.append(values)
        return super().create(prepared)

    def write(self, values):
        if (
            _INDEX_FIELDS.intersection(values)
            and self.env.context.get(_INDEX_CONTEXT) is not _RETENTION_INDEX_TOKEN
        ):
            raise AccessError(_("Retention routing is managed internally."))
        return super().write(values)

    @api.model
    def _retention_index_routes(self, account, limit=500):
        """Bounded legacy indexing; never repeatedly scan already classified JSON."""
        batch = self.sudo().search(
            [
                ("account_id", "=", account.id),
                ("retention_route_indexed", "=", False),
            ],
            order="id",
            limit=max(1, min(int(limit), 500)),
        )
        for event in batch:
            values = self._retention_index_values(
                event.provider_connection_id,
                event.raw_envelope_json,
                fallback=event.create_date,
            )
            event.with_context(**{_INDEX_CONTEXT: _RETENTION_INDEX_TOKEN}).write(values)
        return len(batch)

    def _conversation_content_is_blocked(self):
        if super()._conversation_content_is_blocked():
            return True
        service = self.env["contact.center.retention"]
        if service._retention_envelope_is_expired(
            self.provider_connection_id, self.raw_envelope_json
        ):
            self.with_context(
                contact_center_deletion_token=CONTACT_CENTER_DELETION_TOKEN
            )._erase_conversation_content("retention_expired")
            return True
        return False

    def _process_one(self):
        if self._conversation_content_is_blocked():
            return False
        return super()._process_one()

    def _process_normalized(self, event_dto, attribution_touchpoints=None):
        if self._conversation_content_is_blocked():
            return False
        return super()._process_normalized(
            event_dto, attribution_touchpoints=attribution_touchpoints
        )


class ContactCenterRetentionApplication(models.AbstractModel):
    _inherit = "contact.center.application"

    def _process_event(self, connection, event, inbox_event=None):
        self._validate_event_scope(connection, event)
        if (
            connection.adapter_key == "wuzapi"
            and event.conversation.conversation_type == "group"
        ):
            ids = []
            kind = "control"
            if event.mutation:
                kind = "mutation"
                ids = [event.mutation.get("target_external_message_id")]
            elif event.delivery:
                kind = "receipt"
                ids = event.delivery.get("external_message_ids") or []
            elif event.message:
                kind = "message"
                ids = [event.message.external_message_id]
            route = {
                "conversation_type": "group",
                "conversation_ref": event.conversation_ref,
                "kind": kind,
                "external_ids": list(filter(None, ids)),
                "occurred_at": event.occurred_at.astimezone(
                    datetime.timezone.utc
                ).replace(tzinfo=None),
            }
            if self.env["contact.center.retention"]._retention_route_is_expired(
                connection, route
            ):
                if inbox_event:
                    inbox_event.with_context(
                        contact_center_deletion_token=CONTACT_CENTER_DELETION_TOKEN
                    )._erase_conversation_content("retention_expired")
                return self.env["mail.message"]
        return super()._process_event(connection, event, inbox_event=inbox_event)


class ContactCenterRetentionUi(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    def _serialize_conversation(self, channel, *args, **kwargs):
        result = super()._serialize_conversation(channel, *args, **kwargs)
        binding = channel.sudo().contact_center_binding_ids.filtered(
            lambda item: item.active and not item.merged_into_id
        )[:1]
        if binding:
            result["retention"] = self.env["contact.center.retention"]._policy(binding)
        return result
