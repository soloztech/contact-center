"""Partial retention preserves business consumers and strips copied quotes."""

import copy

from odoo import _, models
from odoo.exceptions import ValidationError

from ..services.tokens import CONTACT_CENTER_ATTRIBUTION_TOKEN
from .retention_ingress import _INDEX_CONTEXT, _RETENTION_INDEX_TOKEN

_QUOTE_BATCH_SIZE = 500


class ContactCenterRetentionDependencies(models.AbstractModel):
    _inherit = "contact.center.retention"

    def _retention_prepare_dependencies(
        self, binding, messages, message_bindings, inbox_events
    ):
        result = super()._retention_prepare_dependencies(
            binding, messages, message_bindings, inbox_events
        )
        if result is False:
            return False
        self._retention_guard_optional_consumers(messages, message_bindings)
        if not self._retention_remove_copied_quotes(
            binding, message_bindings, inbox_events
        ):
            return False
        touchpoints = (
            self.env["contact.center.attribution.touchpoint"]
            .sudo()
            .search([("message_binding_id", "in", message_bindings.ids)])
        )
        # The immutable acquisition evidence and its channel link survive.
        touchpoints.with_context(
            contact_center_attribution_token=CONTACT_CENTER_ATTRIBUTION_TOKEN,
            marketing_contact_center_skip_enqueue=True,
        ).write({"message_binding_id": False})
        self._retention_prepare_business_attachments(messages, message_bindings)
        # Unprojected messages still need ID receipts for edits/reactions with a
        # new timestamp, and for a later provider-connection replacement.
        self._record_expired_ids(
            binding,
            inbox_events.filtered(
                lambda item: item.retention_event_kind == "message"
            ).mapped("retention_message_id"),
        )
        return result

    def _retention_guard_optional_consumers(self, messages, message_bindings):
        """An unimplemented bridge defers expiry instead of cascading business data."""
        native_consumers = {
            "mail.message",
            "mail.notification",
            "mail.message.reaction",
            "mail.tracking.value",
            "mail.link.preview",
            "mail.channel.member",
            "mail.channel",
            "contact.center.message.binding",
            "contact.center.message.mutation",
            "contact.center.media.binding",
            "contact.center.media.upload",
            "contact.center.outbox.command",
            "contact.center.delivery.event",
            "contact.center.group.delivery.event",
            "contact.center.scheduled.message",
            "contact.center.internal.note.request",
            "contact.center.attribution.touchpoint",
        }
        targets = {
            "mail.message": messages.ids,
            "contact.center.message.binding": message_bindings.ids,
        }
        for name, model_class in self.env.registry.models.items():
            if name in native_consumers or not model_class._auto:
                continue
            for field_name, field in model_class._fields.items():
                ids = targets.get(getattr(field, "comodel_name", None))
                if (
                    not ids
                    or not field.store
                    or field.type not in ("many2one", "many2many")
                ):
                    continue
                if (
                    self.env[name]
                    .sudo()
                    .with_context(active_test=False)
                    .search_count([(field_name, "in", ids)])
                ):
                    raise ValidationError(
                        _(
                            "O histórico ainda é usado por %(model)s e será preservado.",
                            model=name,
                        )
                    )

    def _retention_prepare_business_attachments(self, messages, message_bindings):
        uploads = (
            self.env["contact.center.media.upload"]
            .sudo()
            .search([("consumed_message_binding_id", "in", message_bindings.ids)])
        )
        attachments = (
            messages.attachment_ids
            | message_bindings.media_ids.attachment_id
            | uploads.attachment_id
        )
        # The eraser also discovers files by ownership, including files detached
        # from message.attachment_ids. Protect exactly that same candidate set.
        attachments |= (
            self.env["ir.attachment"]
            .sudo()
            .search(
                [
                    "|",
                    "&",
                    ("res_model", "=", "mail.message"),
                    ("res_id", "in", messages.ids),
                    "&",
                    ("res_model", "=", "contact.center.media.upload"),
                    ("res_id", "in", uploads.ids),
                ]
            )
        )
        owned = attachments.filtered(
            lambda item: (
                item.res_model == "mail.message" and item.res_id in messages.ids
            )
            or (
                item.res_model == "contact.center.media.upload"
                and item.res_id in uploads.ids
            )
        )
        native = {
            "mail.message",
            "contact.center.media.binding",
            "contact.center.media.upload",
            "ir.attachment",
        }
        for name, model_class in self.env.registry.models.items():
            if not owned:
                break
            if name in native or not model_class._auto:
                continue
            for field_name, field in model_class._fields.items():
                if (
                    getattr(field, "comodel_name", None) != "ir.attachment"
                    or not field.store
                    or field.type not in ("many2one", "many2many")
                ):
                    continue
                for attachment in owned:
                    owner = (
                        self.env[name]
                        .sudo()
                        .with_context(active_test=False)
                        .search([(field_name, "in", attachment.ids)], limit=1)
                    )
                    if owner:
                        attachment.write({"res_model": name, "res_id": owner.id})
                        owned -= attachment

    def _retention_quote_events(self, binding, external_ids, erased_events):
        inbox = self.env["contact.center.inbox.event"].sudo()
        inbox.flush_model(
            [
                "account_id",
                "retention_route_ref",
                "retention_reply_id",
                "retention_multiple_quotes",
                "retention_reply_ids_json",
            ]
        )
        # JSONB membership handles every quoted ID, not just the first. An OR
        # against all multi-quote rows would revisit unrelated events forever.
        self.env.cr.execute(
            """
            SELECT id FROM contact_center_inbox_event
             WHERE account_id = %s AND retention_route_ref = %s
               AND id != ALL(%s)
               AND (retention_reply_id = ANY(%s)
                    OR (retention_multiple_quotes
                        AND retention_reply_ids_json ?| %s))
             ORDER BY id LIMIT %s
            """,
            [
                binding.account_id.id,
                binding.conversation_ref,
                erased_events.ids,
                sorted(external_ids),
                sorted(external_ids),
                _QUOTE_BATCH_SIZE + 1,
            ],
        )
        return inbox.browse([row[0] for row in self.env.cr.fetchall()])

    def _retention_clean_quoted_event(self, event, external_ids):
        adapter = event.provider_connection_id.get_adapter()
        cleaner = getattr(adapter, "retention_remove_quotes", None)
        if not cleaner:
            raise ValidationError(
                _("O provedor não permite sanear as respostas citadas.")
            )
        envelope = cleaner(event.raw_envelope_json, external_ids)
        # Reindex the cleaned envelope so another cron invocation advances to
        # the next bounded set instead of selecting these rows again.
        values = event._retention_index_values(
            event.provider_connection_id, envelope, fallback=event.create_date
        )
        if envelope != event.raw_envelope_json:
            values["raw_envelope_json"] = envelope
        normalized = self._retention_clean_snapshot(
            event.normalized_dto_json, external_ids
        )
        if normalized != event.normalized_dto_json:
            values["normalized_dto_json"] = normalized
        event.with_context(**{_INDEX_CONTEXT: _RETENTION_INDEX_TOKEN}).write(values)

    def _retention_remove_copied_quotes(self, binding, message_bindings, erased_events):
        external_ids = set(filter(None, message_bindings.mapped("external_message_id")))
        external_ids.update(
            filter(
                None,
                erased_events.filtered(
                    lambda item: item.retention_event_kind == "message"
                ).mapped("retention_message_id"),
            )
        )
        if not external_ids:
            return True
        events = self._retention_quote_events(binding, external_ids, erased_events)
        for event in events[:_QUOTE_BATCH_SIZE]:
            self._retention_clean_quoted_event(event, external_ids)
        if len(events) > _QUOTE_BATCH_SIZE:
            # The original messages remain intact until all copied content has
            # been sanitized. The engine commits this progress without expiring
            # IDs, detaching business records, or advancing the watermark.
            return False
        surviving = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("reply_to_binding_id", "in", message_bindings.ids),
                    ("id", "not in", message_bindings.ids),
                ]
            )
        )
        # Some historical projections carry normalized snapshots even when the
        # provider envelope omitted quotedMessage. Sanitize those before unlink.
        for event in surviving.source_inbox_event_id - erased_events - events:
            self._retention_clean_quoted_event(event, external_ids)
        for projection in surviving:
            cleaned = self._retention_clean_snapshot(
                projection.protocol_snapshot_json, external_ids
            )
            if cleaned != projection.protocol_snapshot_json:
                projection.write({"protocol_snapshot_json": cleaned})
        commands = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("message_binding_id", "in", surviving.ids)])
        )
        for command in commands:
            if command.state not in ("done", "dead", "cancelled"):
                raise ValidationError(
                    _("Uma resposta em andamento ainda depende do histórico.")
                )
            values = {}
            for field in (
                "command_json",
                "provider_request_json",
                "provider_response_json",
            ):
                clean = self._retention_clean_snapshot(command[field], external_ids)
                if clean != command[field]:
                    values[field] = clean
            if values:
                command.write(values)
        return True

    def _retention_clean_snapshot(self, value, external_ids):
        """Strip reference subdocuments, never the surviving author's own text."""
        if not isinstance(value, (dict, list)):
            return value
        value = copy.deepcopy(value)

        def clean(item, depth=0):
            if depth > 32:
                return item
            if isinstance(item, list):
                return [clean(child, depth + 1) for child in item]
            if not isinstance(item, dict):
                return item
            for key in list(item):
                normalized_key = key.lower().replace("_", "")
                child = item[key]
                if normalized_key == "replyto" and isinstance(child, dict):
                    target = child.get("external_message_id") or child.get(
                        "external_id"
                    )
                    if isinstance(target, str) and target in external_ids:
                        item[key] = {
                            name: data
                            for name, data in child.items()
                            if name
                            not in (
                                "protocol_snapshot",
                                "text",
                                "body",
                                "quoted_message",
                                "media",
                                "structured_content",
                            )
                        }
                reference = next(
                    (
                        data
                        for name, data in item.items()
                        if name.lower().replace("_", "") == "stanzaid"
                    ),
                    None,
                )
                if (
                    normalized_key == "quotedmessage"
                    and isinstance(reference, str)
                    and reference in external_ids
                ):
                    item.pop(key)
                else:
                    item[key] = clean(item[key], depth + 1)
            return item

        return clean(value)
