import uuid

from psycopg2 import errorcodes
from psycopg2.errors import SerializationFailure, UniqueViolation

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.osv import expression
from odoo.tools import html2plaintext

from ..services.adapter import AdapterError, conversation_capabilities
from ..services.dto import SCHEMA_VERSION, AddressDTO, DTOValidationError
from ..services.media import (
    enabled_media_kinds,
    validate_provider_media_capability,
    validate_provider_media_caption_capability,
    validate_provider_recorded_audio_capability,
)
from ..services.structured_content import outbound_structured_capabilities
from ..services.timeline import chronology_domain, message_chronology_key
from ..services.tokens import CONTACT_CENTER_MEMBERSHIP_TOKEN


class _ConversationPreferenceSerializationFailure(SerializationFailure):
    # Python-created psycopg2 exceptions need an explicit SQLSTATE for Odoo's
    # transaction retry to restart the request with a fresh database snapshot.
    pgcode = errorcodes.SERIALIZATION_FAILURE


class ContactCenterUiApi(models.AbstractModel):
    _name = "contact.center.ui.api"
    _description = "Contact Center UI API v1"

    def _application(self):
        return self.env["contact.center.application"]

    @api.model
    def _positive_id(self, value, label):
        if type(value) is int and value > 0:  # noqa: E721 - reject bool explicitly
            return value
        if (
            isinstance(value, str)
            and value.isascii()
            and value.isdigit()
            and not value.startswith("0")
        ):
            return int(value)
        raise ValidationError(_("Invalid %s.", label))

    @api.model
    def _bounded_int(self, value, *, default, minimum, maximum, label):
        if value in (None, False, ""):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError) as error:
            raise ValidationError(_("Invalid %s.", label)) from error
        if parsed < minimum:
            return minimum
        return min(parsed, maximum)

    @api.model
    def _conversation_responsibility_domain(self, filters):
        responsibility = filters.get("responsibility", "all")
        if not isinstance(responsibility, str) or responsibility not in (
            "all",
            "mine",
            "unassigned",
        ):
            raise ValidationError(_("Unsupported responsibility filter."))
        if responsibility == "mine":
            return [("contact_center_responsible_id", "=", self.env.user.id)]
        if responsibility == "unassigned":
            return [("contact_center_responsible_id", "=", False)]
        return []

    @api.model
    def _authorized_channel(self, channel_id):
        self._application()._check_agent()
        channel = (
            self.env["mail.channel"]
            .browse(self._positive_id(channel_id, _("conversation ID")))
            .exists()
        )
        if not channel or channel.channel_type != "contact_center":
            raise ValidationError(_("The conversation does not exist."))
        if channel.contact_center_company_id not in self.env.companies:
            raise AccessError(
                _("The conversation company is not active for the current request.")
            )
        channel.check_access_rights("read")
        channel.check_access_rule("read")
        member = channel._contact_center_member_for_current_user()
        return channel, member

    @api.model
    def _binding_for_channel(self, channel):
        return self.env["contact.center.channel.binding"].search(
            [
                ("channel_id", "=", channel.id),
                ("merged_into_id", "=", False),
                ("active", "=", True),
            ],
            limit=1,
        )

    @api.model
    def _provider_connection(self, binding):
        if not binding:
            return self.env["contact.center.provider.connection"]
        if binding.conversation_type == "group":
            profile = binding.group_profile_ids[:1]
            connection = (
                profile.provider_connection_id
                if profile
                else self.env["contact.center.provider.connection"]
            )
            return connection.filtered(
                lambda item: item.active and item.role == "primary"
            )
        return binding.account_id.connection_ids.filtered(
            lambda connection: connection.active and connection.role == "primary"
        )[:1]

    @api.model
    def _visible_connections(self):
        self._application()._check_agent()
        return self.env["contact.center.provider.connection"].search(
            [
                ("active", "=", True),
                ("account_id.active", "=", True),
                ("company_id", "in", self.env.companies.ids),
            ],
            order="account_id, name, id",
        )

    @api.model
    def _connection_health_snapshot(self, connections=None):
        connections = (
            connections if connections is not None else self._visible_connections()
        )
        now = fields.Datetime.now()
        can_check = self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        )
        items = [
            connection._contact_center_health_item(now=now, can_check=can_check)
            for connection in connections
        ]
        summary = {
            "total": len(items),
            "connected": 0,
            "degraded": 0,
            "disconnected": 0,
            "authentication_required": 0,
            "checking": 0,
            "unknown": 0,
        }
        for item in items:
            status = item["state"]
            summary[
                status if status != "checking" and status in summary else "unknown"
            ] += 1
            if item["checking"]:
                summary["checking"] += 1
        last_check_dates = [
            connection.last_health_at
            for connection in connections
            if connection.last_health_at
        ]
        summary["last_check_at"] = fields.Datetime.to_string(
            max(last_check_dates) if last_check_dates else False
        )
        summary["can_check"] = can_check
        return {
            "schema_version": SCHEMA_VERSION,
            "checked_at": fields.Datetime.to_string(now),
            "summary": summary,
            "items": items,
        }

    @api.model
    def _body_text(self, body):
        return html2plaintext(str(body or "")).strip()

    @api.model
    def _body_preview(self, body, limit=160):
        text = " ".join(self._body_text(body).split())
        return text if len(text) <= limit else "%s…" % text[: limit - 1].rstrip()

    @api.model
    def _conversation_preference(self, channel):
        return self.env["contact.center.conversation.preference"].search(
            [
                ("channel_id", "=", channel.id),
                ("user_id", "=", self.env.user.id),
            ],
            limit=1,
        )

    @api.model
    def _serialize_conversation_preference(self, preference):
        return {
            "pinned": bool(preference and preference.pinned_at),
            "pinned_at": (
                fields.Datetime.to_string(preference.pinned_at)
                if preference and preference.pinned_at
                else False
            ),
            "muted": bool(preference and preference.muted),
        }

    @api.model
    def _first_unread_message_id(self, channel, member):
        if not member:
            return False
        self._flush_first_unread_dependencies()
        self.env.cr.execute(
            """
                SELECT message.id
                  FROM mail_message AS message
             LEFT JOIN mail_message AS seen ON seen.id = %s
                 WHERE message.model = 'mail.channel'
                   AND message.res_id = %s
                   AND (seen.id IS NULL OR
                       (COALESCE(message.date, '9999-12-31 23:59:59'::timestamp), message.id)
                       > (COALESCE(seen.date, '9999-12-31 23:59:59'::timestamp), seen.id))
                   AND message.message_type NOT IN (
                       'notification', 'user_notification'
                   )
                   AND NOT EXISTS (
                       SELECT 1
                         FROM contact_center_internal_note_request AS note_request
                        WHERE note_request.message_id = message.id
                   )
              ORDER BY message.date, message.id
                 LIMIT 1
            """,
            [member.seen_message_id.id or 0, channel.id],
        )
        row = self.env.cr.fetchone()
        return row[0] if row else False

    @api.model
    def _flush_first_unread_dependencies(self):
        """Make the unread anchor agree with the operational unread counter."""

        self.env["mail.message"].flush_model(
            ["model", "res_id", "message_type", "date"]
        )
        self.env["contact.center.internal.note.request"].flush_model(["message_id"])

    @api.model
    def _serialize_author(self, message):
        if message.author_guest_id:
            return {
                "type": "guest",
                "id": message.author_guest_id.id,
                "name": message.author_guest_id.name or _("Guest"),
                "is_current_user": False,
            }
        if message.author_id:
            return {
                "type": "partner",
                "id": message.author_id.id,
                "name": message.author_id.display_name,
                "is_current_user": message.author_id == self.env.user.partner_id,
            }
        return {
            "type": "system",
            "id": False,
            "name": _("System"),
            "is_current_user": False,
        }

    @api.model
    def _connection_allows_outbound_ui_actions(self, connection):
        """Expose actions only on the current routed transport.

        Health freshness is revalidated at the durable dispatch boundary. Keeping
        it out of this presentation decision avoids making buttons flicker between
        polling runs while still preventing actions on standby/history providers.
        """

        return bool(
            connection
            and connection.active
            and connection.account_id.active
            and connection.role == "primary"
            and connection.outbound_active
        )

    @api.model
    def _message_reply_allowed(self, binding, connection, capabilities, is_deleted):
        if (
            not binding
            or not binding.external_message_id
            or is_deleted
            or not connection
            or binding.provider_connection_id != connection
            or not self._connection_allows_outbound_ui_actions(connection)
        ):
            return False
        channel_binding = binding.channel_binding_id
        if channel_binding.conversation_type == "direct":
            return True
        if channel_binding.conversation_type != "group":
            return False
        if (
            not channel_binding.account_id.group_outbound_enabled
            or capabilities.get("reply") is not True
            or (
                capabilities.get("reply_requires_participant") is True
                and not binding.protocol_participant_json
            )
        ):
            return False
        try:
            connection.get_adapter().prepare_reply_reference(
                connection,
                conversation_type="group",
                external_message_id=binding.external_message_id,
                protocol_snapshot=binding.protocol_snapshot_json or {},
                protocol_participant=binding.protocol_participant_json or {},
            )
        except AdapterError:
            return False
        return True

    @api.model
    def _message_resend_availability(
        self, binding, outbox, connection, capabilities, is_deleted
    ):
        """Return a fail-closed retry decision plus one allowlisted reason code."""

        if not binding or not outbox or binding.direction != "outbound":
            return False, False
        dispatch_reason = self._outbox_dispatch_reason(outbox)
        if outbox.state != "dead":
            return False, dispatch_reason
        snapshot, unavailable_reason = self._retry_source_snapshot(
            binding, outbox, is_deleted
        )
        if snapshot is None:
            return False, unavailable_reason
        route_reason = self._retry_route_unavailable_reason(
            binding, connection, capabilities, snapshot
        )
        return (False, route_reason) if route_reason else (True, dispatch_reason)

    @api.model
    def _outbox_dispatch_reason(self, outbox):
        waiting_reason = {
            "ProviderPausedError": "waiting_connection",
            "OutboundThrottleError": "waiting_provider_limit",
            "ProviderRateLimitError": "waiting_provider_limit",
        }.get(outbox.last_error_class)
        if outbox.state == "pending":
            return waiting_reason or "waiting_queue"
        if outbox.state == "retry":
            return waiting_reason or "automatic_retry"
        return {
            "processing": "sending",
            "uncertain": "uncertain",
            "dead": "permanent_failure",
            "cancelled": "cancelled",
        }.get(outbox.state, False)

    @api.model
    def _retry_source_snapshot(self, binding, outbox, is_deleted):
        if binding.delivery_state != "failed" or binding.external_message_id:
            # Positive provider evidence is stronger than a prior local failure.
            return None, False
        if outbox.retry_child_ids:
            return None, "retry_created"
        if is_deleted or binding.message_state == "deleted":
            return None, "retry_content_unavailable"
        try:
            snapshot = self._application()._validated_retry_source(
                outbox.sudo(), binding.channel_binding_id
            )
        except (UserError, ValidationError):
            return None, "retry_content_unavailable"
        return snapshot, False

    @api.model
    def _retry_route_unavailable_reason(
        self, binding, connection, capabilities, snapshot
    ):
        _source_command, clean_body, source_media, source_reply = snapshot
        if (
            not connection
            or not self._connection_allows_outbound_ui_actions(connection)
            or capabilities.get("send_message") is not True
        ):
            return "retry_capability_unavailable"
        if not connection._contact_center_outbound_is_available():
            return "retry_connection_unavailable"
        try:
            self._application()._check_outbound_structured_capability(
                connection,
                binding.channel_binding_id.conversation_type,
                _source_command.message.structured_content,
                clean_body,
            )
        except (UserError, ValidationError):
            return "retry_capability_unavailable"
        if (
            clean_body
            and not _source_command.message.structured_content
            and binding.account_id.outbound_signature_enabled
            and capabilities.get("sender_signature") is not True
        ):
            return "retry_capability_unavailable"
        if source_reply:
            try:
                self._application()._outbound_reply_binding(
                    binding.channel_binding_id,
                    connection,
                    source_reply.message_id.id,
                )
            except (UserError, ValidationError):
                return "retry_reply_unavailable"
        if source_media and not self._retry_media_projection_available(
            binding, source_media, capabilities, clean_body
        ):
            return "retry_media_unavailable"
        return False

    @api.model
    def _retry_media_projection_available(
        self, binding, source_media, capabilities, clean_body
    ):
        media = source_media[:1]
        attachment = media.attachment_id.sudo().exists()
        if (
            media.state != "ready"
            or not attachment
            or len(attachment) != 1
            or attachment.type != "binary"
            or attachment not in binding.message_id.attachment_ids
        ):
            return False
        try:
            validate_provider_media_capability(
                capabilities, media.kind, media.mime_type, media.size_bytes
            )
            validate_provider_media_caption_capability(
                capabilities, media.kind, bool(clean_body)
            )
            validate_provider_recorded_audio_capability(
                capabilities,
                media.mime_type,
                bool(media.is_voice_note),
                media.duration_seconds or 0,
            )
        except (UserError, ValidationError):
            return False
        return True

    @api.model
    def _serialize_message(
        self,
        message,
        binding=None,
        outbox=None,
        group_delivery_summary=None,
        parent_binding_by_message=None,
        group_mutation_allowed_by_binding=None,
    ):
        binding = binding or self.env["contact.center.message.binding"]
        outbox = outbox or self.env["contact.center.outbox.command"]
        parent = message.parent_id
        reply_to = False
        if (
            parent
            and parent.model == "mail.channel"
            and parent.res_id == message.res_id
        ):
            parent_binding = (
                self.env["contact.center.message.binding"].search(
                    [("message_id", "=", parent.id)], limit=1
                )
                if parent_binding_by_message is None
                else parent_binding_by_message.get(
                    parent.id, self.env["contact.center.message.binding"]
                )
            )
            parent_media = (
                parent_binding.media_ids.sorted("sequence")[:1]
                if parent_binding
                else self.env["contact.center.media.binding"]
            )
            parent_is_deleted = bool(
                parent_binding and parent_binding.message_state == "deleted"
            )
            reply_to = {
                "message_id": parent.id,
                "body_text": (
                    ""
                    if parent_is_deleted
                    else self._body_preview(parent.body, limit=120)
                ),
                "author": self._serialize_author(parent),
                "media": (
                    []
                    if parent_is_deleted
                    else [
                        {
                            "kind": media.kind,
                            "is_voice_note": bool(media.is_voice_note),
                        }
                        for media in parent_media
                    ]
                ),
                "is_deleted": parent_is_deleted,
            }
        media_items = binding.media_ids.sorted("sequence") if binding else []
        technical_author = (
            binding.account_id.technical_author_id
            if binding
            else self.env["res.partner"]
        )
        reactions_by_emoji = {}
        for reaction in message.sudo().reaction_ids:
            values = reactions_by_emoji.setdefault(
                reaction.content,
                {"emoji": reaction.content, "count": 0, "reacted_by_me": False},
            )
            values["count"] += 1
            if reaction.partner_id in (self.env.user.partner_id, technical_author):
                values["reacted_by_me"] = True
        connection = (
            self._provider_connection(binding.channel_binding_id)
            if binding
            else self.env["contact.center.provider.connection"]
        )
        conversation_type = (
            binding.channel_binding_id.conversation_type if binding else "other"
        )
        capabilities = (
            conversation_capabilities(
                connection.capabilities_json or {}, conversation_type
            )
            if connection
            else {}
        )
        is_deleted = bool(binding and binding.message_state == "deleted")
        deleted_content_visible = bool(
            is_deleted and binding.deleted_display_mode == "strike"
        )
        content_visible = not is_deleted or deleted_content_visible
        body_source = binding.deleted_body if deleted_content_visible else message.body
        allows_mutations = bool(
            binding
            and conversation_type == "direct"
            and connection
            and binding.provider_connection_id == connection
            and self._connection_allows_outbound_ui_actions(connection)
        )
        if binding and conversation_type == "group" and connection:
            if group_mutation_allowed_by_binding is not None:
                allows_mutations = bool(
                    group_mutation_allowed_by_binding.get(binding.id)
                )
            else:
                try:
                    (
                        own_participant,
                        _target_participant,
                    ) = self._application()._group_outbound_mutation_participants(
                        binding.channel_binding_id, connection, binding
                    )
                except (UserError, ValidationError):
                    own_participant = None
                allows_mutations = bool(
                    binding.channel_binding_id.account_id.group_outbound_enabled
                    and connection.active
                    and connection.outbound_active
                    and binding.provider_connection_id == connection
                    and own_participant
                )
        allows_reply = self._message_reply_allowed(
            binding, connection, capabilities, is_deleted
        )
        is_own_outbound = bool(
            binding
            and binding.direction == "outbound"
            and message.author_id == self.env.user.partner_id
        )
        group_delivery = False
        if binding and conversation_type == "group" and binding.direction == "outbound":
            if group_delivery_summary is None:
                summaries = (
                    self.env["contact.center.group.delivery.event"]
                    .sudo()
                    ._summary_by_binding(binding)
                )
                group_delivery_summary = summaries.get(binding.id)
            group_delivery = group_delivery_summary or {
                "delivered_count": 0,
                "read_count": 0,
            }
        can_resend, dispatch_reason = self._message_resend_availability(
            binding, outbox, connection, capabilities, is_deleted
        )
        source_webhook_values = {}
        if binding and self.env.user.has_group("base.group_system"):
            source_webhook_values["source_inbox_event_id"] = (
                binding.sudo().source_inbox_event_id.id or False
            )
        return {
            "message_id": message.id,
            "body_text": self._body_text(body_source) if content_visible else "",
            "date": fields.Datetime.to_string(message.date),
            "date_utc": fields.Datetime.to_string(message.date),
            "author": self._serialize_author(message),
            "direction": binding.direction if binding else "internal",
            "origin": binding.origin if binding else "internal",
            "content_type": binding.content_type if binding else "note",
            "structured_content": (binding.structured_content_json or {})
            if binding and content_visible
            else {},
            "platform": binding.account_id.platform if binding else "internal",
            "provider": (
                binding.provider_connection_id.adapter_key
                if binding and binding.provider_connection_id
                else "internal"
            ),
            "delivery_state": binding.delivery_state if binding else False,
            "group_delivery": group_delivery,
            "dispatch_state": outbox.state if outbox else False,
            "dispatch_reason": dispatch_reason,
            "retry_of_message_id": (
                outbox.retry_of_id.message_binding_id.message_id.id
                if outbox and outbox.retry_of_id
                else False
            ),
            "is_forwarded": bool(binding and binding.is_forwarded and content_visible),
            **source_webhook_values,
            "reply_to": reply_to if content_visible else False,
            "attachment_count": (len(message.attachment_ids) if content_visible else 0),
            "media": [
                {
                    "id": media.id,
                    "kind": media.kind,
                    "state": media.state,
                    "name": media.file_name or "",
                    "mimetype": media.mime_type or "",
                    "size_bytes": media.size_bytes or 0,
                    "content_url": (
                        "/contact_center/media/%s/content" % media.id
                        if media.state == "ready" and media.attachment_id
                        else ""
                    ),
                    "download_url": (
                        "/contact_center/media/%s/content?download=1" % media.id
                        if media.state == "ready" and media.attachment_id
                        else ""
                    ),
                    "is_voice_note": bool(media.is_voice_note),
                    "duration_seconds": media.duration_seconds or 0,
                    "width": media.width or 0,
                    "height": media.height or 0,
                }
                for media in media_items
                if content_visible
            ],
            "is_deleted": is_deleted,
            "deleted_content_visible": deleted_content_visible,
            "edited_at": (
                fields.Datetime.to_string(binding.edited_at)
                if binding and binding.edited_at
                else False
            ),
            "reactions": (
                sorted(reactions_by_emoji.values(), key=lambda item: item["emoji"])
                if content_visible
                else []
            ),
            "actions": {
                "reply": bool(allows_reply),
                "react": bool(
                    allows_mutations
                    and binding.external_message_id
                    and capabilities.get("react")
                    and not is_deleted
                ),
                "edit": bool(
                    allows_mutations
                    and is_own_outbound
                    and binding.external_message_id
                    and capabilities.get("edit_message")
                    and not is_deleted
                    and self._application()._outbound_text_edit_supported(binding)
                ),
                "delete": bool(
                    allows_mutations
                    and is_own_outbound
                    and binding.external_message_id
                    and capabilities.get("delete_message")
                    and not is_deleted
                ),
                "resend": bool(can_resend),
            },
        }

    @api.model
    def _serialize_message_preview(self, message, binding=None, outbox=None):
        """Return the bounded subset consumed by the conversation list.

        Timeline-only policy (reply/mutation checks, reactions, quoted messages and
        group delivery aggregation) is deliberately omitted. Besides reducing the
        payload, this keeps a list page from issuing participant-roster queries for
        every group's latest message.
        """

        binding = binding or self.env["contact.center.message.binding"]
        outbox = outbox or self.env["contact.center.outbox.command"]
        is_deleted = bool(binding and binding.message_state == "deleted")
        media_items = (
            binding.media_ids.sorted("sequence")[:1]
            if binding and not is_deleted
            else self.env["contact.center.media.binding"]
        )
        return {
            "message_id": message.id,
            "body_text": "" if is_deleted else self._body_text(message.body),
            "author": self._serialize_author(message),
            "direction": binding.direction if binding else "internal",
            "content_type": binding.content_type if binding else "note",
            "delivery_state": binding.delivery_state if binding else False,
            "dispatch_state": outbox.state if outbox else False,
            "media": [
                {
                    "kind": media.kind,
                    "is_voice_note": bool(media.is_voice_note),
                }
                for media in media_items
            ],
            "is_deleted": is_deleted,
        }

    @api.model
    def _suggested_phone(self, identity, binding=None):
        preferred_namespaces = (
            "whatsapp.pn",
            "whatsapp.jid",
            "phone",
        )
        visible_aliases = identity.alias_ids.filtered(
            lambda alias: binding and alias.account_id == binding.account_id
        )
        aliases = sorted(
            visible_aliases,
            key=lambda alias: (
                (
                    preferred_namespaces.index(alias.namespace)
                    if alias.namespace in preferred_namespaces
                    else len(preferred_namespaces)
                ),
                alias.id,
            ),
        )
        for alias in aliases:
            if alias.namespace not in preferred_namespaces:
                continue
            value = (alias.value_normalized or alias.value_raw or "").split("@", 1)[0]
            digits = "".join(character for character in value if character.isdigit())
            if len(digits) >= 8:
                return "+%s" % digits
        return ""

    @api.model
    def _serialize_partner_company(self, company):
        if not company:
            return False
        return {
            "id": company.id,
            "name": company.name or _("Unnamed company"),
            "email": company.email or "",
            "phone": company.phone or company.mobile or "",
            "vat": company.vat or "",
        }

    @api.model
    def _visible_partner_company(self, identity, partner):
        company_id = partner.sudo().parent_id.id
        if not company_id:
            return self.env["res.partner"]
        # Resolve the parent again without sudo so an inaccessible or
        # cross-company legacy relation is hidden instead of breaking the whole
        # identity projection or leaking partner data.
        return (
            self.env["res.partner"]
            .with_context(active_test=False)
            .search(
                [
                    ("id", "=", company_id),
                    ("is_company", "=", True),
                    ("type", "=", "contact"),
                    ("company_id", "in", [False, identity.company_id.id]),
                ],
                limit=1,
            )
        )

    @api.model
    def _partner_has_internal_user(self, partner):
        return bool(
            partner
            and self.env["res.users"]
            .sudo()
            .search(
                [("partner_id", "=", partner.id), ("share", "=", False)],
                limit=1,
            )
        )

    @api.model
    def _partner_company_linking_allowed(self, identity, partner):
        return bool(
            partner
            and identity.partner_link_kind == "person"
            and partner.active
            and not partner.is_company
            and partner.type == "contact"
            and partner.company_id == identity.company_id
            and not partner.parent_id
            and not self._partner_has_internal_user(partner)
        )

    @api.model
    def _batch_partner_company_projection(self, identities):
        empty_company = self.env["res.partner"]
        partners = identities.mapped("partner_id")
        internal_user_partner_ids = set(
            self.env["res.users"]
            .sudo()
            .search(
                [
                    ("partner_id", "in", partners.ids),
                    ("share", "=", False),
                ]
            )
            .mapped("partner_id")
            .ids
        )
        parent_id_by_identity = {
            identity.id: identity.partner_id.sudo().parent_id.id
            for identity in identities
            if identity.partner_id
        }
        parent_ids = list(set(parent_id_by_identity.values()) - {False})
        company_ids = identities.mapped("company_id").ids
        visible_companies = (
            self.env["res.partner"]
            .with_context(active_test=False)
            .search(
                [
                    ("id", "in", parent_ids),
                    ("is_company", "=", True),
                    ("type", "=", "contact"),
                    ("company_id", "in", [False] + company_ids),
                ]
            )
            if parent_ids
            else empty_company
        )
        visible_by_id = {company.id: company for company in visible_companies}
        company_by_identity = {}
        linking_allowed_by_identity = {}
        for identity in identities:
            partner = identity.partner_id
            company = visible_by_id.get(parent_id_by_identity.get(identity.id))
            if (
                company
                and company.company_id
                and company.company_id != identity.company_id
            ):
                company = empty_company
            company_by_identity[identity.id] = company or empty_company
            linking_allowed_by_identity[identity.id] = bool(
                partner
                and identity.partner_link_kind == "person"
                and partner.active
                and not partner.is_company
                and partner.type == "contact"
                and partner.company_id == identity.company_id
                and not parent_id_by_identity.get(identity.id)
                and partner.id not in internal_user_partner_ids
            )
        return company_by_identity, linking_allowed_by_identity

    @api.model
    def _serialize_identity(
        self,
        identity,
        binding=None,
        partner_company=None,
        company_linking_allowed=None,
    ):
        if not identity:
            return False
        partner = identity.partner_id
        link_kind = identity.partner_link_kind or ("person" if partner else False)
        if partner_company is None:
            partner_company = (
                self._visible_partner_company(identity, partner) if partner else False
            )
        if company_linking_allowed is None:
            company_linking_allowed = self._partner_company_linking_allowed(
                identity, partner
            )
        # The caller has already authorized the channel/member scope. Technical
        # avatar fields are admin-only at ORM level, so read only this projection
        # with sudo and expose no attachment ID or provider detail.
        avatar_binding = binding.sudo() if binding else binding
        return {
            "id": identity.id,
            "name": (partner.name or identity.name) if partner else identity.name,
            "persona_kind": "contact" if partner else "guest",
            "link_kind": link_kind,
            "link_invariant_valid": bool(
                not partner
                or (
                    link_kind == "person"
                    and partner.active
                    and not partner.is_company
                    and partner.type == "contact"
                    and (
                        not partner.company_id
                        or partner.company_id == identity.company_id
                    )
                )
                or (
                    link_kind == "central_company"
                    and partner.active
                    and partner.is_company
                    and partner.type == "contact"
                    and (
                        not partner.company_id
                        or partner.company_id == identity.company_id
                    )
                )
            ),
            "guest": {
                "id": identity.mail_guest_id.id,
                "name": identity.mail_guest_id.name,
            },
            "partner": (
                {
                    "id": partner.id,
                    "name": partner.name or identity.name,
                    "email": partner.email or "",
                    "phone": partner.phone or partner.mobile or "",
                    "vat": partner.vat or "",
                    "is_company": bool(partner.is_company),
                    "company_linking_allowed": bool(company_linking_allowed),
                    "company": self._serialize_partner_company(partner_company),
                }
                if partner
                else False
            ),
            "suggested_phone": self._suggested_phone(identity, binding=binding),
            "avatar_url": (
                "/contact_center/conversation/%s/avatar?v=%s"
                % (
                    avatar_binding.channel_id.id,
                    (avatar_binding.direct_avatar_sha256 or "")[:12],
                )
                if avatar_binding
                and avatar_binding.conversation_type == "direct"
                and avatar_binding.identity_id == identity
                and avatar_binding.direct_avatar_attachment_id
                else False
            ),
            "aliases": [
                {
                    "id": alias.id,
                    "namespace": alias.namespace,
                    "value": alias.value_raw,
                    "normalized": alias.value_normalized,
                    "role": alias.role,
                    "confidence": alias.confidence,
                }
                for alias in identity.alias_ids.filtered(
                    lambda item: binding and item.account_id == binding.account_id
                ).sorted(key=lambda item: (item.namespace, item.id))
            ],
        }

    @api.model
    def _latest_message(self, channel):
        message = channel.contact_center_last_message_id
        if message and message.model == "mail.channel" and message.res_id == channel.id:
            return message
        return self.env["mail.message"]

    @api.model
    def _batch_group_own_protocol_participants(self, profiles):
        """Resolve the safe group sender projection with one alias query."""

        profiles = profiles.sudo().exists()
        candidate_by_profile = {}
        for profile in profiles:
            connection = profile.provider_connection_id
            if (
                not connection
                or not profile.own_protocol_participant_json
                or profile.own_protocol_participant_health_revision
                != connection.health_configuration_revision
            ):
                continue
            try:
                participant = AddressDTO.from_dict(
                    profile.own_protocol_participant_json
                )
            except DTOValidationError:
                continue
            if participant.role != "sender" or participant.confidence != "protocol":
                continue
            candidate_by_profile[profile.id] = participant
        if not candidate_by_profile:
            return {}
        candidates = tuple(candidate_by_profile.values())
        aliases = (
            self.env["contact.center.group.participant.alias"]
            .sudo()
            .search(
                [
                    ("group_profile_id", "in", list(candidate_by_profile)),
                    ("participant_id.active", "=", True),
                    (
                        "namespace",
                        "in",
                        sorted({item.namespace for item in candidates}),
                    ),
                    (
                        "value_normalized",
                        "in",
                        sorted({item.value_normalized for item in candidates}),
                    ),
                    ("confidence", "=", "protocol"),
                ]
            )
        )
        aliases_by_profile = {}
        for alias in aliases:
            participant = candidate_by_profile.get(alias.group_profile_id.id)
            if participant and (
                alias.namespace,
                alias.value_normalized,
            ) == (participant.namespace, participant.value_normalized):
                aliases_by_profile.setdefault(alias.group_profile_id.id, []).append(
                    alias
                )
        return {
            profile.channel_binding_id.id: candidate_by_profile[profile.id]
            for profile in profiles
            if profile.id in candidate_by_profile
            and len(aliases_by_profile.get(profile.id, ())) == 1
        }

    @api.model
    def _group_mutation_own_participant(self, profile):
        """Return one revision-current, protocol-proven own participant."""

        connection = profile.provider_connection_id
        if (
            not connection
            or not profile.own_protocol_participant_json
            or profile.own_protocol_participant_health_revision
            != connection.health_configuration_revision
        ):
            return None
        try:
            own = AddressDTO.from_dict(profile.own_protocol_participant_json)
        except DTOValidationError:
            return None
        if own.role != "sender" or own.confidence != "protocol":
            return None
        return own

    @api.model
    def _group_mutation_permission_target(self, binding):
        """Return a protocol-proven message participant suitable for mutations."""

        try:
            target = AddressDTO.from_dict(binding.protocol_participant_json or {})
        except DTOValidationError:
            return None
        if target.role in ("group", "routing") or target.confidence != "protocol":
            return None
        return target

    @api.model
    def _batch_group_mutation_roster(self, profiles, keys_by_profile):
        """Resolve all own/target keys with the single batched roster query."""

        all_keys = {
            key for profile_keys in keys_by_profile.values() for key in profile_keys
        }
        if not all_keys:
            return {}, {}, set()
        aliases = (
            self.env["contact.center.group.participant.alias"]
            .sudo()
            .search(
                [
                    ("group_profile_id", "in", profiles.ids),
                    ("participant_id.active", "=", True),
                    ("namespace", "in", sorted({key[0] for key in all_keys})),
                    (
                        "value_normalized",
                        "in",
                        sorted({key[1] for key in all_keys}),
                    ),
                ]
            )
        )
        participant_by_key = {}
        confidence_by_key = {}
        ambiguous_keys = set()
        for alias in aliases:
            key = (
                alias.group_profile_id.id,
                alias.namespace,
                alias.value_normalized,
            )
            existing = participant_by_key.get(key)
            if existing and existing != alias.participant_id:
                ambiguous_keys.add(key)
            else:
                participant_by_key[key] = alias.participant_id
                confidence_by_key[key] = alias.confidence
        return participant_by_key, confidence_by_key, ambiguous_keys

    @api.model
    def _group_mutation_is_allowed(
        self,
        binding,
        profile,
        own,
        target,
        participant_by_key,
        confidence_by_key,
        ambiguous_keys,
    ):
        """Apply the fail-closed mutation policy to one prefetched binding."""

        if not profile or not target or not own:
            return False
        connection = profile.provider_connection_id
        if not connection:
            return False
        own_key = (profile.id, own.namespace, own.value_normalized)
        target_key = (profile.id, target.namespace, target.value_normalized)
        own_roster = participant_by_key.get(own_key)
        target_roster = participant_by_key.get(target_key)
        return not (
            own_key in ambiguous_keys
            or target_key in ambiguous_keys
            or not own_roster
            or not target_roster
            or confidence_by_key.get(own_key) != "protocol"
            or not binding.channel_binding_id.account_id.group_outbound_enabled
            or not connection.active
            or not connection.outbound_active
            or binding.provider_connection_id != connection
            or (binding.direction == "outbound" and target_roster != own_roster)
        )

    @api.model
    def _batch_group_mutation_permissions(self, bindings):
        """Evaluate timeline mutation policy without per-message roster searches."""

        result = {binding.id: False for binding in bindings}
        group_bindings = bindings.filtered(
            lambda item: item.channel_binding_id.conversation_type == "group"
        )
        if not group_bindings:
            return result
        channel_bindings = group_bindings.mapped("channel_binding_id")
        profiles = (
            self.env["contact.center.group.profile"]
            .sudo()
            .search([("channel_binding_id", "in", channel_bindings.ids)])
        )
        profile_by_binding = {
            profile.channel_binding_id.id: profile for profile in profiles
        }
        own_by_profile = {}
        target_by_binding = {}
        keys_by_profile = {}
        for profile in profiles:
            own = self._group_mutation_own_participant(profile)
            if not own:
                continue
            own_by_profile[profile.id] = own
            keys_by_profile.setdefault(profile.id, set()).add(
                (own.namespace, own.value_normalized)
            )
        for binding in group_bindings:
            profile = profile_by_binding.get(binding.channel_binding_id.id)
            if not profile or profile.id not in own_by_profile:
                continue
            target = self._group_mutation_permission_target(binding)
            if not target:
                continue
            target_by_binding[binding.id] = target
            keys_by_profile.setdefault(profile.id, set()).add(
                (target.namespace, target.value_normalized)
            )
        (
            participant_by_key,
            confidence_by_key,
            ambiguous_keys,
        ) = self._batch_group_mutation_roster(profiles, keys_by_profile)
        for binding in group_bindings:
            profile = profile_by_binding.get(binding.channel_binding_id.id)
            target = target_by_binding.get(binding.id)
            own = own_by_profile.get(profile.id) if profile else None
            result[binding.id] = self._group_mutation_is_allowed(
                binding,
                profile,
                own,
                target,
                participant_by_key,
                confidence_by_key,
                ambiguous_keys,
            )
        return result

    @api.model
    def _serialize_conversation(
        self, channel, member=None, binding=None, prefetched=None
    ):
        prefetched = prefetched or {}
        member = member or channel._contact_center_member_for_current_user()
        binding = binding or self._binding_for_channel(channel)
        identity = (
            binding.identity_id if binding else self.env["contact.center.identity"]
        )
        account = binding.account_id if binding else self.env["contact.center.account"]
        conversation_type = binding.conversation_type if binding else "other"
        group_profile = (
            prefetched.get("group_profile", self.env["contact.center.group.profile"])
            if "group_profile" in prefetched
            else (
                self.env["contact.center.group.profile"]
                .sudo()
                .search([("channel_binding_id", "=", binding.id)], limit=1)
                if binding and conversation_type == "group"
                else self.env["contact.center.group.profile"]
            )
        )
        connection = (
            group_profile.provider_connection_id
            if conversation_type == "group" and group_profile
            else self._provider_connection(binding)
        )
        last_message = (
            prefetched["last_message"]
            if "last_message" in prefetched
            else self._latest_message(channel)
        )
        last_binding = (
            prefetched.get("last_binding", self.env["contact.center.message.binding"])
            if "last_binding" in prefetched
            else (
                self.env["contact.center.message.binding"].search(
                    [("message_id", "=", last_message.id)], limit=1
                )
                if last_message
                else self.env["contact.center.message.binding"]
            )
        )
        last_outbox = (
            prefetched.get("last_outbox", self.env["contact.center.outbox.command"])
            if "last_outbox" in prefetched
            else (
                self.env["contact.center.outbox.command"]
                .sudo()
                .search(
                    [("message_binding_id", "=", last_binding.id)],
                    order="id desc",
                    limit=1,
                )
                if last_binding
                else self.env["contact.center.outbox.command"]
            )
        )
        group_display_name = (
            (group_profile.name or channel.name or _("Conversation"))
            if conversation_type == "group"
            else ""
        )
        provider_capabilities = (
            conversation_capabilities(
                connection.capabilities_json or {}, conversation_type
            )
            if connection
            else {}
        )
        outbound_policy_enabled = bool(
            conversation_type == "direct"
            or (
                conversation_type == "group"
                and account
                and account.group_outbound_enabled
            )
        )
        can_send = bool(
            outbound_policy_enabled
            and connection
            and connection.active
            and connection.outbound_active
            and provider_capabilities.get("send_message")
        )
        own_protocol_participant = None
        if (
            can_send
            and conversation_type == "group"
            and provider_capabilities.get("reply_requires_participant") is True
        ):
            if "own_protocol_participant" in prefetched:
                own_protocol_participant = prefetched["own_protocol_participant"]
            else:
                try:
                    own_protocol_participant = (
                        self._application()._group_own_protocol_participant(
                            binding, connection, required=False
                        )
                    )
                except ValidationError:
                    own_protocol_participant = None
            can_send = bool(own_protocol_participant)
        if conversation_type == "direct":
            effective_capabilities = dict(provider_capabilities)
            effective_capabilities["send_message"] = can_send
            effective_capabilities.pop("structured_content", None)
            effective_capabilities["outbound_structured_content"] = (
                outbound_structured_capabilities(provider_capabilities)
                if can_send
                else {}
            )
        elif conversation_type == "group":
            if own_protocol_participant is None and can_send:
                if "own_protocol_participant" in prefetched:
                    own_protocol_participant = prefetched["own_protocol_participant"]
                else:
                    try:
                        own_protocol_participant = (
                            self._application()._group_own_protocol_participant(
                                binding, connection, required=False
                            )
                        )
                    except ValidationError:
                        own_protocol_participant = None
            mutations_ready = bool(can_send and own_protocol_participant)
            effective_capabilities = {
                "send_message": can_send,
                "sender_signature": bool(
                    can_send and provider_capabilities.get("sender_signature") is True
                ),
                "media": (provider_capabilities.get("media", {}) if can_send else {}),
                "outbound_structured_content": outbound_structured_capabilities(
                    provider_capabilities
                )
                if can_send
                else {},
                "reply": bool(can_send and provider_capabilities.get("reply") is True),
                "react": bool(
                    mutations_ready and provider_capabilities.get("react") is True
                ),
                "edit_message": bool(
                    mutations_ready
                    and provider_capabilities.get("edit_message") is True
                ),
                "delete_message": bool(
                    mutations_ready
                    and provider_capabilities.get("delete_message") is True
                ),
                "delivery_receipts": bool(
                    provider_capabilities.get("delivery_receipts") is True
                ),
            }
        else:
            effective_capabilities = {
                "send_message": False,
                "media": {},
                "reply": False,
                "react": False,
                "edit_message": False,
                "delete_message": False,
            }
        # This policy belongs to the logical inbox, never to provider-advertised
        # capabilities. Always overwrite a same-named adapter value.
        effective_capabilities["view_attribution"] = bool(
            account and account.sudo().attribution_ui_enabled
        )
        identity_payload = (
            self._serialize_identity(
                identity,
                binding=binding,
                partner_company=prefetched["partner_company"],
                company_linking_allowed=prefetched["company_linking_allowed"],
            )
            if "partner_company" in prefetched
            else self._serialize_identity(identity, binding=binding)
        )
        preference = (
            prefetched.get(
                "preference", self.env["contact.center.conversation.preference"]
            )
            if "preference" in prefetched
            else self._conversation_preference(channel)
        )
        first_unread_message_id = (
            prefetched.get("first_unread_message_id", False)
            if "first_unread_message_id" in prefetched
            else self._first_unread_message_id(channel, member)
        )
        return {
            "channel_id": channel.id,
            "conversation_type": conversation_type,
            "name": (
                group_display_name
                if conversation_type == "group"
                else (
                    identity_payload["name"]
                    if identity_payload
                    else channel.name or _("Conversation")
                )
            ),
            "state": channel.contact_center_state,
            "unread_count": member.message_unread_counter or 0,
            "first_unread_message_id": first_unread_message_id,
            "preference": self._serialize_conversation_preference(preference),
            "last_activity_at": fields.Datetime.to_string(
                channel.contact_center_last_message_at
                or (last_message.date if last_message else channel.create_date)
            ),
            "account": (
                {
                    "id": account.id,
                    "name": account.name,
                    "platform": account.platform,
                    "signature_enabled": account.outbound_signature_enabled,
                    "show_deleted_message_content": bool(
                        account.show_deleted_message_content
                    ),
                }
                if account
                else False
            ),
            "platform": account.platform if account else "",
            "provider": connection.adapter_key if connection else "",
            "provider_connection": (
                {
                    "id": connection.id,
                    "name": connection.name,
                    "key": connection.adapter_key,
                    "state": connection.state,
                }
                if connection
                else False
            ),
            "can_send": can_send,
            "capabilities": effective_capabilities,
            "identity": identity_payload,
            "group": (
                {
                    "display_name": group_display_name,
                    "avatar_url": (
                        "/contact_center/group/%s/avatar?v=%s"
                        % (channel.id, (group_profile.avatar_sha256 or "")[:12])
                        if group_profile and group_profile.avatar_attachment_id
                        else False
                    ),
                    "participant_count": (
                        group_profile.participant_count
                        if group_profile
                        and group_profile.last_synced_at
                        and group_profile.roster_complete
                        else False
                    ),
                    "admin_count": (
                        group_profile.admin_count
                        if group_profile
                        and group_profile.last_synced_at
                        and group_profile.roster_complete
                        else False
                    ),
                    "own_role": (
                        group_profile.own_role if group_profile else "unknown"
                    ),
                    "metadata_state": (
                        group_profile.metadata_state if group_profile else "unavailable"
                    ),
                    "last_synced_at": (
                        fields.Datetime.to_string(group_profile.last_synced_at)
                        if group_profile and group_profile.last_synced_at
                        else False
                    ),
                }
                if conversation_type == "group"
                else False
            ),
            "team": (
                {
                    "id": channel.contact_center_team_id.id,
                    "name": channel.contact_center_team_id.name,
                }
                if channel.contact_center_team_id
                else False
            ),
            "owner": (
                {
                    "id": channel.contact_center_owner_user_id.id,
                    "name": channel.contact_center_owner_user_id.display_name,
                }
                if channel.contact_center_owner_user_id
                else False
            ),
            "responsible": (
                {
                    "id": channel.contact_center_responsible_id.id,
                    "name": channel.contact_center_responsible_id.display_name,
                }
                if channel.contact_center_responsible_id
                else False
            ),
            "tags": [
                {"id": tag.id, "name": tag.name, "color": tag.color}
                for tag in channel.contact_center_tag_ids.sorted(
                    key=lambda item: (item.name, item.id)
                )
            ],
            "last_message": (
                (
                    self._serialize_message_preview(
                        last_message, last_binding, last_outbox
                    )
                    if prefetched.get("compact_last_message")
                    else self._serialize_message(
                        last_message,
                        last_binding,
                        last_outbox,
                        prefetched.get("group_delivery_summary"),
                        prefetched.get("parent_binding_by_message"),
                    )
                )
                if last_message
                else False
            ),
        }

    @api.model
    def bootstrap(self):
        self._application()._check_agent()
        account_model = self.env["contact.center.account"]
        accounts = account_model.search(
            account_model._contact_center_scope_domain() + [("active", "=", True)]
        )
        connections = self._visible_connections()
        team_model = self.env["contact.center.team"]
        teams = team_model.search(
            team_model._contact_center_scope_domain() + [("active", "=", True)]
        )
        agents = (
            teams.agent_ids
            | teams.supervisor_ids
            | accounts._contact_center_effective_users()
        ).filtered(lambda user: user.active and not user.share)
        tags = self.env["contact.center.tag"].search(
            [("company_id", "in", self.env.companies.ids)]
        )
        is_supervisor = self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        )
        active_capabilities = [
            connection.capabilities_json or {}
            for account in accounts
            for connection in account.connection_ids
            if connection.active and connection.outbound_active
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "user": {"id": self.env.user.id, "name": self.env.user.display_name},
            "capabilities": {
                "send_message": any(
                    connection.active
                    and connection.outbound_active
                    and (connection.capabilities_json or {}).get("send_message")
                    for account in accounts
                    for connection in account.connection_ids
                ),
                "media": sorted(
                    {
                        kind
                        for capabilities in active_capabilities
                        for kind in enabled_media_kinds(capabilities)
                    }
                ),
                "react": any(
                    capabilities.get("react") for capabilities in active_capabilities
                ),
                "edit_message": any(
                    capabilities.get("edit_message")
                    for capabilities in active_capabilities
                ),
                "delete_message": any(
                    capabilities.get("delete_message")
                    for capabilities in active_capabilities
                ),
                "manage_assignment": is_supervisor,
                "check_connection_health": is_supervisor,
                "manage_tags": True,
                "link_contact": True,
                # This capability follows create_and_link_partner's dedicated,
                # channel-scoped policy, not the broad res.partner create ACL.
                "create_contact": True,
                "link_company": is_supervisor,
                # Company creation follows the same narrow, channel-scoped
                # policy as contact creation, but only supervisors may mutate
                # the commercial parent and trigger Odoo's field sync.
                "create_company": is_supervisor,
                # A direct company link is an explicit exception for a shared,
                # centralized number.  Keep it separate from the person-first
                # promotion contract and supervisor-only.
                "link_central_company": is_supervisor,
                "create_central_company": is_supervisor,
                "rename_guest": True,
                "view_source_webhook": self.env.user.has_group("base.group_system"),
            },
            "accounts": [
                {
                    "id": account.id,
                    "name": account.name,
                    "platform": account.platform,
                    "signature_enabled": account.outbound_signature_enabled,
                    "show_deleted_message_content": bool(
                        account.show_deleted_message_content
                    ),
                    "capabilities": next(
                        (
                            connection.capabilities_json or {}
                            for connection in account.connection_ids
                            if connection.active and connection.outbound_active
                        ),
                        {},
                    ),
                }
                for account in accounts
            ],
            "connection_health": self._connection_health_snapshot(connections),
            "teams": [
                {
                    "id": team.id,
                    "name": team.name,
                    "agent_ids": (team.agent_ids | team.supervisor_ids).ids,
                }
                for team in teams
            ],
            "agents": [
                {"id": user.id, "name": user.display_name}
                for user in agents.sorted(key=lambda item: (item.name, item.id))
            ],
            "tags": [
                {"id": tag.id, "name": tag.name, "color": tag.color} for tag in tags
            ],
            "states": {
                "conversation": [
                    {"key": "open", "label": _("Aberta")},
                    {"key": "resolved", "label": _("Resolvida")},
                    {"key": "archived", "label": _("Arquivada")},
                ]
            },
        }

    @api.model
    def check_connection_health(self, connection_id=False):
        """Schedule visible health probes and return their current safe snapshot."""

        self._application()._check_agent()
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(
                _("Only Contact Center supervisors can request a health refresh.")
            )
        visible = self._visible_connections()
        targets = visible
        if connection_id:
            parsed_id = self._positive_id(connection_id, _("provider connection ID"))
            targets = visible.filtered(lambda connection: connection.id == parsed_id)
            if not targets:
                raise ValidationError(
                    _("The provider connection is not available for this request.")
                )
        targets.sudo()._enqueue_health_check(priority=30)
        visible.invalidate_recordset(
            [
                "health_check_pending",
                "health_job_uuid",
                "next_health_check_at",
            ]
        )
        return self._connection_health_snapshot(visible)

    @api.model
    def _conversation_list_domain(self, filters):
        """Validate list filters and return the member-scoped search domain."""

        if filters in (None, False):
            filters = {}
        if not isinstance(filters, dict):
            raise ValidationError(_("Conversation filters must be an object."))
        domain = [
            ("channel_type", "=", "contact_center"),
            ("channel_member_ids.partner_id", "=", self.env.user.partner_id.id),
            ("contact_center_company_id", "in", self.env.companies.ids),
        ]
        state = filters.get("state")
        if state:
            if state not in ("open", "resolved", "archived"):
                raise ValidationError(_("Unsupported conversation state."))
            domain.append(("contact_center_state", "=", state))
        account_id = filters.get("account_id")
        if account_id:
            domain.append(
                (
                    "contact_center_binding_ids.account_id",
                    "=",
                    self._positive_id(account_id, _("account ID")),
                )
            )
        domain.extend(self._conversation_responsibility_domain(filters))
        query = filters.get("query") or ""
        if not isinstance(query, str):
            raise ValidationError(_("The conversation search must be text."))
        query = query.strip()[:100]
        if not query:
            return domain
        searchable_fields = (
            "name",
            "contact_center_binding_ids.conversation_ref",
            "contact_center_binding_ids.identity_id.name",
            "contact_center_binding_ids.identity_id.partner_id.name",
            "contact_center_binding_ids.identity_id.partner_id.email",
            "contact_center_binding_ids.identity_id.partner_id.phone",
            "contact_center_binding_ids.identity_id.partner_id.mobile",
            "contact_center_binding_ids.alias_ids.value_raw",
            "contact_center_binding_ids.alias_ids.value_normalized",
            "contact_center_binding_ids.group_profile_ids.name",
        )
        return expression.AND(
            [
                domain,
                expression.OR(
                    [[(field_name, "ilike", query)] for field_name in searchable_fields]
                ),
            ]
        )

    @api.model
    def _conversation_list_cursor_domain(self, cursor):
        """Validate one stable activity cursor and return its seek predicate."""

        if not isinstance(cursor, dict):
            raise ValidationError(_("The conversation cursor is invalid."))
        cursor_id = self._positive_id(cursor.get("channel_id"), _("cursor channel ID"))
        try:
            cursor_date = fields.Datetime.to_datetime(cursor.get("last_activity_at"))
        except (TypeError, ValueError) as error:
            raise ValidationError(
                _("The conversation cursor date is invalid.")
            ) from error
        if not cursor_date:
            raise ValidationError(_("The conversation cursor date is required."))
        return [
            "|",
            ("contact_center_last_message_at", "<", cursor_date),
            "&",
            ("contact_center_last_message_at", "=", cursor_date),
            ("id", "<", cursor_id),
        ]

    @api.model
    def _conversation_list_cursor(self, cursor):
        """Parse the canonical segment-aware conversation cursor."""

        if not isinstance(cursor, dict):
            raise ValidationError(_("The conversation cursor is invalid."))
        segment = cursor.get("segment")
        if segment not in ("pinned", "activity"):
            raise ValidationError(_("The conversation cursor segment is invalid."))
        channel_id = self._positive_id(cursor.get("channel_id"), _("cursor channel ID"))
        if segment == "pinned":
            try:
                pinned_at = fields.Datetime.to_datetime(cursor.get("pinned_at"))
            except (TypeError, ValueError) as error:
                raise ValidationError(
                    _("The pinned conversation cursor date is invalid.")
                ) from error
            if not pinned_at:
                raise ValidationError(
                    _("The pinned conversation cursor date is required.")
                )
            return {
                "segment": segment,
                "channel_id": channel_id,
                "pinned_at": pinned_at,
            }
        # Reuse the established activity validation and normalize its date once.
        self._conversation_list_cursor_domain(cursor)
        return {
            "segment": segment,
            "channel_id": channel_id,
            "last_activity_at": fields.Datetime.to_datetime(
                cursor.get("last_activity_at")
            ),
        }

    @api.model
    def _ordered_pinned_conversations(self, domain, cursor=False):
        preferences = self.env["contact.center.conversation.preference"].search(
            [
                ("user_id", "=", self.env.user.id),
                ("pinned_at", "!=", False),
            ],
            order="pinned_at desc, channel_id desc",
        )
        if not preferences:
            return self.env["mail.channel"], {}
        channels = self.env["mail.channel"].search(
            expression.AND([domain, [("id", "in", preferences.channel_id.ids)]])
        )
        channel_by_id = {channel.id: channel for channel in channels}
        preference_by_channel = {
            preference.channel_id.id: preference
            for preference in preferences
            if preference.channel_id.id in channel_by_id
        }
        ordered = self.env["mail.channel"]
        for preference in preferences:
            channel = channel_by_id.get(preference.channel_id.id)
            if not channel:
                continue
            if cursor and cursor["segment"] == "pinned":
                key = (preference.pinned_at, channel.id)
                if key >= (cursor["pinned_at"], cursor["channel_id"]):
                    continue
            ordered |= channel
        return ordered, preference_by_channel

    @api.model
    def _conversation_list_prefetch(self, channels):
        """Load every list projection dependency in bounded, shared queries."""

        bindings = self.env["contact.center.channel.binding"].search(
            [
                ("channel_id", "in", channels.ids),
                ("merged_into_id", "=", False),
                ("active", "=", True),
            ]
        )
        binding_by_channel = {binding.channel_id.id: binding for binding in bindings}
        (
            partner_company_by_identity,
            company_linking_allowed_by_identity,
        ) = self._batch_partner_company_projection(bindings.mapped("identity_id"))
        members = self.env["mail.channel.member"].search(
            [
                ("channel_id", "in", channels.ids),
                ("partner_id", "=", self.env.user.partner_id.id),
            ]
        )
        member_by_channel = {member.channel_id.id: member for member in members}
        preferences = self.env["contact.center.conversation.preference"].search(
            [
                ("channel_id", "in", channels.ids),
                ("user_id", "=", self.env.user.id),
            ]
        )
        preference_by_channel = {
            preference.channel_id.id: preference for preference in preferences
        }
        first_unread_by_channel = {}
        if members:
            self._flush_first_unread_dependencies()
            seen_by_channel = {
                member.channel_id.id: member.seen_message_id.id or 0
                for member in members
            }
            self.env.cr.execute(
                """
                    SELECT DISTINCT ON (scoped.channel_id)
                           scoped.channel_id, message.id
                      FROM mail_message AS message
                      JOIN unnest(%s::int[], %s::int[])
                        AS scoped(channel_id, seen_message_id)
                        ON message.res_id = scoped.channel_id
                 LEFT JOIN mail_message AS seen ON seen.id = scoped.seen_message_id
                     WHERE message.model = 'mail.channel'
                       AND (seen.id IS NULL OR
                           (COALESCE(message.date, '9999-12-31 23:59:59'::timestamp),
                            message.id)
                           > (COALESCE(seen.date, '9999-12-31 23:59:59'::timestamp), seen.id))
                       AND message.message_type NOT IN (
                           'notification', 'user_notification'
                       )
                       AND NOT EXISTS (
                           SELECT 1
                             FROM contact_center_internal_note_request AS note_request
                            WHERE note_request.message_id = message.id
                       )
                  ORDER BY scoped.channel_id, message.date, message.id
                """,
                [
                    list(seen_by_channel),
                    list(seen_by_channel.values()),
                ],
            )
            first_unread_by_channel = dict(self.env.cr.fetchall())

        last_message_by_channel = {}
        for channel in channels:
            last_message_by_channel[channel.id] = self._latest_message(channel)
        last_messages = self.env["mail.message"].browse(
            [message.id for message in last_message_by_channel.values() if message]
        )
        last_bindings = self.env["contact.center.message.binding"].search(
            [("message_id", "in", last_messages.ids)]
        )
        last_binding_by_message = {item.message_id.id: item for item in last_bindings}
        last_outboxes = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("message_binding_id", "in", last_bindings.ids)], order="id desc")
        )
        last_outbox_by_binding = {}
        for outbox in last_outboxes:
            last_outbox_by_binding.setdefault(outbox.message_binding_id.id, outbox)
        group_bindings = bindings.filtered(
            lambda item: item.conversation_type == "group"
        )
        group_profiles = (
            self.env["contact.center.group.profile"]
            .sudo()
            .search([("channel_binding_id", "in", group_bindings.ids)], order="id")
        )
        group_profile_by_binding = {}
        for profile in group_profiles:
            group_profile_by_binding.setdefault(profile.channel_binding_id.id, profile)
        return {
            "binding_by_channel": binding_by_channel,
            "member_by_channel": member_by_channel,
            "preference_by_channel": preference_by_channel,
            "first_unread_by_channel": first_unread_by_channel,
            "last_message_by_channel": last_message_by_channel,
            "last_binding_by_message": last_binding_by_message,
            "last_outbox_by_binding": last_outbox_by_binding,
            "group_profile_by_binding": group_profile_by_binding,
            "partner_company_by_identity": partner_company_by_identity,
            "company_linking_allowed_by_identity": (
                company_linking_allowed_by_identity
            ),
            "own_protocol_by_binding": self._batch_group_own_protocol_participants(
                group_profiles
            ),
        }

    @api.model
    def _serialize_conversation_list_items(self, channels, prefetched):
        """Serialize one authorized page using only prefetched dependencies."""

        empty_binding = self.env["contact.center.message.binding"]
        empty_outbox = self.env["contact.center.outbox.command"]
        empty_profile = self.env["contact.center.group.profile"]
        empty_partner = self.env["res.partner"]
        items = []
        for channel in channels:
            member = prefetched["member_by_channel"].get(channel.id)
            if not member:
                # The domain and record rules should make this impossible, but
                # preserve fail-closed behavior if membership changes in flight.
                raise AccessError(_("You are not a member of this conversation."))
            binding = prefetched["binding_by_channel"].get(channel.id)
            last_message = prefetched["last_message_by_channel"][channel.id]
            last_binding = (
                prefetched["last_binding_by_message"].get(last_message.id)
                if last_message
                else empty_binding
            )
            items.append(
                self._serialize_conversation(
                    channel,
                    member=member,
                    binding=binding,
                    prefetched={
                        "last_message": last_message,
                        "last_binding": last_binding,
                        "last_outbox": (
                            prefetched["last_outbox_by_binding"].get(last_binding.id)
                            if last_binding
                            else empty_outbox
                        ),
                        "group_profile": (
                            prefetched["group_profile_by_binding"].get(
                                binding.id, empty_profile
                            )
                            if binding
                            else empty_profile
                        ),
                        "own_protocol_participant": (
                            prefetched["own_protocol_by_binding"].get(binding.id)
                            if binding
                            else None
                        ),
                        "compact_last_message": True,
                        "preference": prefetched["preference_by_channel"].get(
                            channel.id,
                            self.env["contact.center.conversation.preference"],
                        ),
                        "first_unread_message_id": prefetched[
                            "first_unread_by_channel"
                        ].get(channel.id, False),
                        "partner_company": (
                            prefetched["partner_company_by_identity"].get(
                                binding.identity_id.id, empty_partner
                            )
                            if binding and binding.identity_id
                            else empty_partner
                        ),
                        "company_linking_allowed": (
                            prefetched["company_linking_allowed_by_identity"].get(
                                binding.identity_id.id, False
                            )
                            if binding and binding.identity_id
                            else False
                        ),
                    },
                )
            )
        return items

    @api.model
    def list_conversations(self, limit=50, offset=0, filters=None, cursor=None):
        self._application()._check_agent()
        limit = self._bounded_int(
            limit, default=50, minimum=1, maximum=100, label=_("limit")
        )
        offset = self._bounded_int(
            offset, default=0, minimum=0, maximum=100000, label=_("offset")
        )
        domain = self._conversation_list_domain(filters)
        parsed_cursor = self._conversation_list_cursor(cursor) if cursor else False
        (
            pinned_channels,
            pinned_preference_by_channel,
        ) = self._ordered_pinned_conversations(domain, parsed_cursor)
        all_pinned_ids = (
            self.env["contact.center.conversation.preference"]
            .search(
                [
                    ("user_id", "=", self.env.user.id),
                    ("pinned_at", "!=", False),
                ]
            )
            .channel_id.ids
        )
        pinned_offset = 0
        activity_offset = 0
        if not parsed_cursor:
            pinned_offset = min(offset, len(pinned_channels))
            activity_offset = max(offset - len(pinned_channels), 0)
        elif parsed_cursor["segment"] == "activity":
            pinned_channels = self.env["mail.channel"]
            activity_offset = 0
        pinned_page = pinned_channels[pinned_offset : pinned_offset + limit + 1]
        remaining = max(limit + 1 - len(pinned_page), 0)
        activity_domain = expression.AND(
            [domain, [("id", "not in", all_pinned_ids or [0])]]
        )
        if parsed_cursor and parsed_cursor["segment"] == "activity":
            activity_domain = expression.AND(
                [activity_domain, self._conversation_list_cursor_domain(cursor)]
            )
        activity_page = self.env["mail.channel"].search(
            activity_domain,
            order="contact_center_last_message_at desc, id desc",
            limit=remaining,
            offset=activity_offset,
        )
        channels = pinned_page | activity_page
        has_more = len(channels) > limit
        channels = channels[:limit]
        prefetched = self._conversation_list_prefetch(channels)
        prefetched["preference_by_channel"].update(pinned_preference_by_channel)
        items = self._serialize_conversation_list_items(channels, prefetched)
        next_cursor = False
        if has_more and items:
            last_item = items[-1]
            if last_item["preference"]["pinned"]:
                next_cursor = {
                    "segment": "pinned",
                    "pinned_at": last_item["preference"]["pinned_at"],
                    "channel_id": last_item["channel_id"],
                }
            else:
                next_cursor = {
                    "segment": "activity",
                    "last_activity_at": last_item["last_activity_at"],
                    "channel_id": last_item["channel_id"],
                }
        return {
            "schema_version": SCHEMA_VERSION,
            "items": items,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "total": (
                self.env["mail.channel"].search_count(domain) if not cursor else False
            ),
        }

    @api.model
    def get_conversation(self, channel_id):
        channel, member = self._authorized_channel(channel_id)
        return {
            "schema_version": SCHEMA_VERSION,
            "item": self._serialize_conversation(channel, member=member),
        }

    @api.model
    def get_attribution(self, channel_id, cursor=None, limit=3):
        """Return only the bounded, account-opted-in operator projection."""

        channel, _member = self._authorized_channel(channel_id)
        binding = self._binding_for_channel(channel)
        if not binding:
            return {
                "schema_version": SCHEMA_VERSION,
                "channel_id": channel.id,
                "enabled": False,
                "items": [],
                "has_more": False,
                "next_cursor": False,
            }
        limit = self._bounded_int(
            limit, default=3, minimum=1, maximum=20, label=_("attribution limit")
        )
        if cursor not in (None, False, ""):
            if not isinstance(cursor, str) or len(cursor) != 36:
                raise ValidationError(_("The attribution cursor is invalid."))
            try:
                canonical_cursor = str(uuid.UUID(cursor))
            except (AttributeError, ValueError) as error:
                raise ValidationError(
                    _("The attribution cursor is invalid.")
                ) from error
            if canonical_cursor != cursor.lower():
                raise ValidationError(_("The attribution cursor is invalid."))
            cursor = canonical_cursor
        projection = self.env[
            "contact.center.attribution.touchpoint"
        ]._safe_projection_for_binding(
            binding.sudo(), limit=limit, before_public_ref=cursor or None
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            **projection,
        }

    @api.model
    def get_timeline(
        self,
        channel_id,
        before_message_id=None,
        limit=50,
        after_message_id=None,
        anchor_message_id=None,
        after_chronological_message_id=None,
        known_received_message_id=None,
    ):
        """Page history by date/ID, while realtime deltas retain an ingestion cursor."""

        channel, member = self._authorized_channel(channel_id)
        limit = self._bounded_int(
            limit, default=50, minimum=1, maximum=100, label=_("limit")
        )
        has_before_cursor = before_message_id not in (None, False, "")
        has_after_cursor = after_message_id not in (None, False, "")
        has_anchor = anchor_message_id not in (None, False, "")
        has_chronological_cursor = after_chronological_message_id not in (
            None,
            False,
            "",
        )
        if (
            sum(
                (
                    has_before_cursor,
                    has_after_cursor,
                    has_anchor,
                    has_chronological_cursor,
                )
            )
            > 1
        ):
            raise ValidationError(
                _("Timeline cursors and the message anchor are mutually exclusive.")
            )
        before_cursor = (
            self._positive_id(before_message_id, _("message cursor"))
            if has_before_cursor
            else False
        )
        after_cursor = (
            self._positive_id(after_message_id, _("message cursor"))
            if has_after_cursor
            else False
        )
        anchor_cursor = (
            self._positive_id(anchor_message_id, _("message anchor"))
            if has_anchor
            else False
        )
        chronological_cursor = (
            self._positive_id(after_chronological_message_id, _("message cursor"))
            if has_chronological_cursor
            else False
        )
        known_received_cursor = (
            self._positive_id(known_received_message_id, _("received message cursor"))
            if known_received_message_id not in (None, False, "")
            else False
        )
        forward_mode = bool(after_cursor or anchor_cursor or chronological_cursor)
        domain = [
            ("model", "=", "mail.channel"),
            ("res_id", "=", channel.id),
            ("message_type", "not in", ("notification", "user_notification")),
        ]
        base_domain = list(domain)
        history_cursor = before_cursor or anchor_cursor or chronological_cursor
        cursor_message = self.env["mail.message"]
        if history_cursor:
            cursor_message = self.env["mail.message"].search(
                domain + [("id", "=", history_cursor)], limit=1
            )
            if not cursor_message:
                raise ValidationError(
                    _("The message cursor does not belong to this conversation.")
                )
            operator = "<" if before_cursor else (">=" if anchor_cursor else ">")
            domain = expression.AND(
                [domain, chronology_domain(cursor_message, operator)]
            )
        elif after_cursor:
            domain.append(("id", ">", after_cursor))
        messages = self.env["mail.message"].search(
            domain,
            order=(
                "id asc"
                if after_cursor
                else ("date asc, id asc" if forward_mode else "date desc, id desc")
            ),
            limit=limit + 1,
        )
        page_has_more = len(messages) > limit
        messages = messages[:limit]
        has_older_than_anchor = bool(
            anchor_cursor
            and self.env["mail.message"].search(
                expression.AND([base_domain, chronology_domain(cursor_message, "<")]),
                limit=1,
            )
        )
        bindings = self.env["contact.center.message.binding"].search(
            [("message_id", "in", messages.ids)]
        )
        binding_by_message = {binding.message_id.id: binding for binding in bindings}
        outboxes = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("message_binding_id", "in", bindings.ids)], order="id desc")
        )
        outbox_by_binding = {}
        for outbox in outboxes:
            outbox_by_binding.setdefault(outbox.message_binding_id.id, outbox)
        group_delivery_by_binding = (
            self.env["contact.center.group.delivery.event"]
            .sudo()
            ._summary_by_binding(
                bindings.filtered(
                    lambda item: item.channel_binding_id.conversation_type == "group"
                    and item.direction == "outbound"
                )
            )
        )
        parent_messages = messages.mapped("parent_id").filtered(
            lambda item: item.model == "mail.channel" and item.res_id == channel.id
        )
        parent_bindings = self.env["contact.center.message.binding"].search(
            [("message_id", "in", parent_messages.ids)]
        )
        parent_binding_by_message = {
            item.message_id.id: item for item in parent_bindings
        }
        group_mutation_allowed_by_binding = self._batch_group_mutation_permissions(
            bindings
        )
        items = []
        ordered_messages = messages.sorted(message_chronology_key)
        for message in ordered_messages:
            binding = binding_by_message.get(message.id)
            items.append(
                self._serialize_message(
                    message,
                    binding,
                    outbox_by_binding.get(binding.id) if binding else None,
                    group_delivery_by_binding.get(binding.id, {}) if binding else None,
                    parent_binding_by_message,
                    group_mutation_allowed_by_binding,
                )
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "items": items,
            "anchor_message_id": anchor_cursor or False,
            "first_unread_message_id": self._first_unread_message_id(channel, member),
            "has_more": (
                has_older_than_anchor
                if anchor_cursor
                else (page_has_more if not forward_mode else False)
            ),
            "next_before_message_id": (
                anchor_cursor
                if anchor_cursor and has_older_than_anchor
                else (
                    messages[-1].id
                    if not forward_mode and page_has_more and messages
                    else False
                )
            ),
            "has_more_forward": page_has_more if forward_mode else False,
            "next_after_message_id": (
                max(messages.ids) if after_cursor and messages else False
            ),
            "next_after_chronological_message_id": (
                ordered_messages[-1].id
                if (anchor_cursor or chronological_cursor) and messages
                else False
            ),
            # This cursor is independent from the chronological page. An older
            # imported/provider-delayed message can have the highest local ID.
            "latest_received_message_id": self.env["mail.message"]
            .search(base_domain, order="id desc", limit=1)
            .id
            or False,
            "has_unloaded_received": bool(
                known_received_cursor
                and self.env["mail.message"].search(
                    base_domain
                    + [
                        ("id", ">", known_received_cursor),
                        ("id", "not in", messages.ids),
                    ],
                    limit=1,
                )
            ),
        }

    @api.model
    def set_conversation_preference(self, channel_id, patch):
        """Update the current user's sparse pin/mute preference atomically."""

        channel, member = self._authorized_channel(channel_id)
        if not isinstance(patch, dict) or not patch:
            raise ValidationError(_("The conversation preference must be an object."))
        unknown = set(patch) - {"pinned", "muted"}
        if unknown:
            raise ValidationError(
                _(
                    "Unsupported conversation preferences: %s",
                    ", ".join(sorted(unknown)),
                )
            )
        if any(type(value) is not bool for value in patch.values()):  # noqa: E721
            raise ValidationError(_("Conversation preferences must be booleans."))

        # The member row is the canonical user/conversation scope and serializes
        # simultaneous toggles without introducing an unrelated global lock.
        self.env.cr.execute(
            "SELECT id FROM mail_channel_member WHERE id = %s FOR UPDATE", [member.id]
        )
        preference_model = self.env["contact.center.conversation.preference"]
        preference = preference_model.search(
            [
                ("channel_id", "=", channel.id),
                ("user_id", "=", self.env.user.id),
            ],
            limit=1,
        )
        values = {}
        if "pinned" in patch:
            values["pinned_at"] = (
                preference.pinned_at
                if patch["pinned"] and preference and preference.pinned_at
                else (fields.Datetime.now() if patch["pinned"] else False)
            )
        if "muted" in patch:
            values["muted"] = patch["muted"]
        if preference:
            preference.write(values)
        elif values.get("pinned_at") or values.get("muted"):
            try:
                with self.env.cr.savepoint():
                    preference = preference_model.create(
                        {
                            "channel_id": channel.id,
                            "user_id": self.env.user.id,
                            **values,
                        }
                    )
            except UniqueViolation as error:
                if error.diag.constraint_name != (
                    "contact_center_conversation_preference_channel_user_unique"
                ):
                    raise
                # The member lock serializes changes but does not update that
                # row. At REPEATABLE READ a first preference committed while
                # this request waited is still invisible to its old snapshot.
                # Restart the whole request so the patch merges into that row.
                raise _ConversationPreferenceSerializationFailure(
                    "Concurrent conversation preference requires a fresh snapshot"
                ) from error
        if preference and not preference.pinned_at and not preference.muted:
            preference.unlink()
            preference = preference_model

        serialized_preference = self._serialize_conversation_preference(preference)
        self._application()._notify_ui(
            channel,
            "conversation_preference_updated",
            {"preference": serialized_preference},
            partner_ids=[self.env.user.partner_id.id],
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "item": self._serialize_conversation(channel, member=member),
        }

    @api.model
    def mark_fetched(self, channel_id, message_id=None):
        return self._mark_member_pointer(channel_id, message_id, seen=False)

    @api.model
    def mark_seen(self, channel_id, message_id=None):
        return self._mark_member_pointer(channel_id, message_id, seen=True)

    def _mark_member_pointer(self, channel_id, message_id=None, seen=False):
        channel, member = self._authorized_channel(channel_id)
        message_domain = [
            ("model", "=", "mail.channel"),
            ("res_id", "=", channel.id),
        ]
        if message_id:
            message = (
                self.env["mail.message"]
                .browse(self._positive_id(message_id, _("message ID")))
                .exists()
            )
            if (
                not message
                or message.model != "mail.channel"
                or message.res_id != channel.id
            ):
                raise ValidationError(
                    _("The message does not belong to this conversation.")
                )
        else:
            message = self.env["mail.message"].search(
                message_domain, order="date desc, id desc", limit=1
            )
        if not message:
            return {"channel_id": channel.id, "message_id": False}
        self.env.cr.execute(
            "SELECT id FROM mail_channel_member WHERE id = %s FOR UPDATE", (member.id,)
        )
        member.invalidate_recordset(
            ["fetched_message_id", "seen_message_id", "last_seen_dt"]
        )
        values = {}
        if not member.fetched_message_id or message_chronology_key(
            member.fetched_message_id
        ) < message_chronology_key(message):
            values["fetched_message_id"] = message.id
        if seen and (
            not member.seen_message_id
            or message_chronology_key(member.seen_message_id)
            < message_chronology_key(message)
        ):
            values.update(
                {
                    "seen_message_id": message.id,
                    "last_seen_dt": fields.Datetime.now(),
                }
            )
        if values:
            member.sudo().with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
            ).write(values)
            self._application()._notify_ui(
                channel,
                "member_seen" if seen else "member_fetched",
                {"message_id": message.id, "user_id": self.env.user.id},
            )
        return {"channel_id": channel.id, "message_id": message.id}

    def _conversation_assignment_fields(self, patch):
        if not isinstance(patch, dict):
            raise ValidationError(_("The conversation update must be an object."))
        if "team_id" in patch:
            raise ValidationError(
                _(
                    "The conversation team is defined by its inbox and cannot "
                    "be changed here."
                )
            )
        allowed = {"state", "responsible_id", "tag_ids"}
        unknown = set(patch) - allowed
        if unknown:
            raise ValidationError(
                _("Unsupported conversation fields: %s", ", ".join(sorted(unknown)))
            )
        return {"responsible_id"} & set(patch)

    def _conversation_update_responsible(self, channel, responsible_id):
        responsible = (
            self.env["res.users"]
            .browse(self._positive_id(responsible_id, _("responsible ID")))
            .exists()
            if responsible_id
            else self.env["res.users"]
        )
        if responsible and (
            not responsible.active
            or responsible.share
            or channel.contact_center_company_id not in responsible.company_ids
            or not responsible.has_group(
                "contact_center_base.group_contact_center_agent"
            )
        ):
            raise ValidationError(_("The selected responsible is not available."))
        return responsible

    def _conversation_update_tags(self, channel, tag_ids):
        if not isinstance(tag_ids, list) or any(
            type(tag_id) is not int or tag_id <= 0 for tag_id in tag_ids
        ):
            raise ValidationError(_("Tag IDs must be positive integers."))
        unique_tag_ids = set(tag_ids)
        tags = self.env["contact.center.tag"].browse(list(unique_tag_ids)).exists()
        if len(tags) != len(unique_tag_ids) or any(
            tag.company_id != channel.contact_center_company_id for tag in tags
        ):
            raise ValidationError(_("One or more tags are not available."))
        tags.check_access_rights("read")
        tags.check_access_rule("read")
        return tags

    def _conversation_update_values(self, channel, patch, access_users):
        values = {}
        if "state" in patch:
            if patch["state"] not in ("open", "resolved", "archived"):
                raise ValidationError(_("Unsupported conversation state."))
            values["contact_center_state"] = patch["state"]

        responsible = channel.contact_center_responsible_id
        if "responsible_id" in patch:
            responsible = self._conversation_update_responsible(
                channel, patch["responsible_id"]
            )
            values["contact_center_responsible_id"] = responsible.id or False

        if responsible and responsible not in access_users:
            raise ValidationError(
                _("The responsible agent is outside the inbox access scope.")
            )

        if "tag_ids" in patch:
            tags = self._conversation_update_tags(channel, patch["tag_ids"])
            values["contact_center_tag_ids"] = [(6, 0, tags.ids)]
        return values

    def _persist_conversation_update(
        self, channel, patch, assignment_fields, values, access_users
    ):
        old_partner_ids = channel.sudo().channel_member_ids.partner_id.ids
        channel.sudo().with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write(values)
        if assignment_fields:
            channel._contact_center_reconcile_members(
                partner_ids=access_users.partner_id.ids,
                guest_ids=channel.sudo().channel_member_ids.guest_id.ids,
            )
        new_partner_ids = channel.sudo().channel_member_ids.partner_id.ids
        self._application()._notify_ui(
            channel,
            "conversation_updated",
            {"changed_fields": sorted(patch)},
            partner_ids=list(set(old_partner_ids) | set(new_partner_ids)),
        )
        current_member = channel.sudo().channel_member_ids.filtered(
            lambda item: item.partner_id == self.env.user.partner_id
        )[:1]
        return {
            "schema_version": SCHEMA_VERSION,
            "removed_from_conversation": not bool(current_member),
            "item": (
                self._serialize_conversation(channel, member=current_member)
                if current_member
                else False
            ),
        }

    @api.model
    def update_conversation(self, channel_id, patch):
        channel, _member = self._authorized_channel(channel_id)
        assignment_fields = self._conversation_assignment_fields(patch)
        if not patch:
            return {
                "schema_version": SCHEMA_VERSION,
                "item": self._serialize_conversation(channel),
            }
        if assignment_fields and not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(_("Only supervisors can assign conversations."))
        binding = self._binding_for_channel(channel)
        if not binding:
            raise ValidationError(_("The conversation has no active binding."))
        account = binding.account_id
        access_users = account._contact_center_effective_users()
        if assignment_fields:
            if not access_users:
                raise ValidationError(_("The conversation inbox has no attendants."))
            if not self.env.user.has_group(
                "contact_center_base.group_contact_center_admin"
            ):
                account._contact_center_check_user_scope()
        self.env.cr.execute(
            "SELECT id FROM mail_channel WHERE id = %s FOR UPDATE", [channel.id]
        )
        channel.invalidate_recordset(
            [
                "contact_center_state",
                "contact_center_team_id",
                "contact_center_responsible_id",
                "contact_center_tag_ids",
                "channel_member_ids",
            ]
        )
        values = self._conversation_update_values(channel, patch, access_users)
        return self._persist_conversation_update(
            channel, patch, assignment_fields, values, access_users
        )

    @api.model
    def claim_conversation(self, channel_id):
        channel, _member = self._authorized_channel(channel_id)
        self.env.cr.execute(
            "SELECT id FROM mail_channel WHERE id = %s FOR UPDATE", [channel.id]
        )
        channel.invalidate_recordset(["contact_center_responsible_id"])
        if (
            channel.contact_center_responsible_id
            and channel.contact_center_responsible_id != self.env.user
        ):
            raise AccessError(
                _("Only supervisors can transfer an assigned conversation.")
            )
        binding = self._binding_for_channel(channel)
        if not binding or self.env.user not in (
            binding.account_id._contact_center_effective_users()
        ):
            raise AccessError(_("You do not belong to this inbox access scope."))
        channel.sudo().with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write({"contact_center_responsible_id": self.env.user.id})
        self._application()._notify_ui(
            channel,
            "conversation_updated",
            {"changed_fields": ["responsible_id"]},
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "item": self._serialize_conversation(channel),
        }

    @api.model
    def search_partners(self, channel_id, query, limit=12):
        channel, _member = self._authorized_channel(channel_id)
        if not isinstance(query, str) or len(query.strip()) < 2:
            return {"schema_version": SCHEMA_VERSION, "items": []}
        query = query.strip()[:100]
        limit = self._bounded_int(
            limit, default=12, minimum=1, maximum=30, label=_("limit")
        )
        partners = self.env["res.partner"].search(
            [
                ("active", "=", True),
                ("is_company", "=", False),
                ("type", "=", "contact"),
                ("company_id", "in", [False, channel.contact_center_company_id.id]),
                ("partner_share", "=", True),
                "|",
                "|",
                "|",
                ("name", "ilike", query),
                ("email", "ilike", query),
                ("phone", "ilike", query),
                ("mobile", "ilike", query),
            ],
            order="name, id",
            limit=limit,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "items": [
                {
                    "id": partner.id,
                    "name": partner.display_name or _("Unnamed contact"),
                    "email": partner.email or "",
                    "phone": partner.phone or partner.mobile or "",
                }
                for partner in partners
            ],
        }

    @api.model
    def _identity_for_channel(self, channel):
        binding = self._binding_for_channel(channel)
        if not binding or not binding.identity_id:
            raise ValidationError(_("The conversation has no remote identity."))
        return binding.identity_id

    @api.model
    def _lock_identity(self, identity):
        self.env.cr.execute(
            "SELECT id FROM contact_center_identity WHERE id = %s FOR UPDATE",
            [identity.id],
        )
        identity.invalidate_recordset(["partner_id", "partner_link_kind"])
        return identity

    @api.model
    def _check_central_company_access(self):
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(
                _(
                    "Only Contact Center supervisors can link a centralized number "
                    "directly to a company."
                )
            )
        return True

    @api.model
    def _company_creation_values(self, channel, values):
        if not isinstance(values, dict):
            raise ValidationError(_("Company values must be an object."))
        allowed = {"name", "vat", "email", "phone", "mobile"}
        if set(values) - allowed:
            raise ValidationError(_("Unsupported company field."))
        name = values.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(_("A company name is required."))
        company_values = {
            "name": name.strip()[:256],
            "company_id": channel.contact_center_company_id.id,
            "is_company": True,
            "type": "contact",
        }
        for field_name in ("vat", "email", "phone", "mobile"):
            value = values.get(field_name)
            if value is None or value is False or value == "":
                continue
            if not isinstance(value, str):
                raise ValidationError(_("Company fields must be text."))
            value = value.strip()
            if value:
                company_values[field_name] = value[:256]
        return company_values

    @api.model
    def _linked_person_for_company(self, channel, expected_partner_id, *, lock=False):
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(
                _("Only Contact Center supervisors can manage contact companies.")
            )
        expected_partner_id = self._positive_id(expected_partner_id, _("contact ID"))
        if lock:
            # A linked partner's structural-write guard takes partner ->
            # identity. Keep the same order while changing its parent relation.
            self.env.cr.execute(
                "SELECT id FROM res_partner WHERE id = %s FOR UPDATE",
                [expected_partner_id],
            )
        identity = self._identity_for_channel(channel)
        if lock:
            self._lock_identity(identity)
        person = identity.partner_id
        if not person:
            raise ValidationError(_("Link a contact before linking a company."))
        if person.id != expected_partner_id:
            raise ValidationError(
                _("The linked contact changed. Reopen the company editor.")
            )
        if identity.partner_link_kind != "person":
            raise ValidationError(
                _("Only a personal Contact Center link can receive a company.")
            )
        if lock:
            person.invalidate_recordset(
                [
                    "active",
                    "is_company",
                    "type",
                    "company_id",
                    "parent_id",
                    "user_ids",
                ]
            )
        if (
            not person.active
            or person.is_company
            or person.type != "contact"
            or person.company_id != channel.contact_center_company_id
            or self._partner_has_internal_user(person)
        ):
            raise ValidationError(
                _("Manage this contact's company from the Contacts application.")
            )
        return identity, person

    @api.model
    def _partner_company_domain(self, channel):
        return [
            ("active", "=", True),
            ("is_company", "=", True),
            ("type", "=", "contact"),
            ("company_id", "in", [False, channel.contact_center_company_id.id]),
            ("partner_share", "=", True),
        ]

    @api.model
    def _search_partner_company_items(self, channel, query, limit):
        companies = self.env["res.partner"].search(
            self._partner_company_domain(channel)
            + [
                "|",
                "|",
                "|",
                "|",
                ("name", "ilike", query),
                ("email", "ilike", query),
                ("phone", "ilike", query),
                ("mobile", "ilike", query),
                ("vat", "ilike", query),
            ],
            order="name, id",
            limit=limit,
        )
        return [self._serialize_partner_company(company) for company in companies]

    @api.model
    def _partner_company_for_channel(self, channel, company_partner_id, *, lock=False):
        # Keep custom Contacts record rules as part of the boundary. The sudo
        # below is limited to the final write after this record was readable and
        # matched the explicit operational-company domain.
        company = self.env["res.partner"].search(
            self._partner_company_domain(channel)
            + [("id", "=", self._positive_id(company_partner_id, _("company ID")))],
            limit=1,
        )
        if not company:
            raise ValidationError(_("The selected company is not available."))
        company.check_access_rights("read")
        company.check_access_rule("read")
        if lock:
            self.env.cr.execute(
                "SELECT id FROM res_partner WHERE id = %s FOR UPDATE", [company.id]
            )
            company.invalidate_recordset(
                ["active", "is_company", "type", "company_id", "user_ids"]
            )
            if (
                not company.active
                or not company.is_company
                or company.type != "contact"
                or self._partner_has_internal_user(company)
                or (
                    company.company_id
                    and company.company_id != channel.contact_center_company_id
                )
            ):
                raise ValidationError(_("The selected company is not available."))
        return company

    @api.model
    def _notify_identity_channels(self, identity):
        """Invalidate every active inbox that projects one shared identity."""

        channels = (
            identity.channel_binding_ids.sudo()
            .filtered(
                lambda binding: binding.active
                and not binding.merged_into_id
                and binding.conversation_type == "direct"
            )
            .mapped("channel_id")
        )
        for channel in channels:
            self._application()._notify_ui(
                channel,
                "identity_updated",
                {"identity_id": identity.id},
            )
        return True

    @api.model
    def _notify_partner_identities(self, partner):
        identities = (
            self.env["contact.center.identity"]
            .sudo()
            .search([("partner_id", "=", partner.id), ("state", "=", "active")])
        )
        for identity in identities:
            self._notify_identity_channels(identity)
        return True

    @api.model
    def link_partner(self, channel_id, partner_id):
        channel, _member = self._authorized_channel(channel_id)
        identity = self._identity_for_channel(channel)
        partner_id = self._positive_id(partner_id, _("contact ID"))
        # ``action_link_partner`` owns the canonical partner -> identity lock
        # order and all under-lock revalidation. Pre-locking the identity here
        # would invert that protocol against a concurrent Contacts edit.
        if identity.action_link_partner(partner_id):
            self._notify_identity_channels(identity)
        return {
            "schema_version": SCHEMA_VERSION,
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def create_and_link_partner(self, channel_id, values):
        channel, _member = self._authorized_channel(channel_id)
        if not isinstance(values, dict):
            raise ValidationError(_("Contact values must be an object."))
        allowed = {"name", "email", "phone", "mobile"}
        if set(values) - allowed:
            raise ValidationError(_("Unsupported contact field."))
        name = values.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(_("A contact name is required."))
        identity = self._identity_for_channel(channel)
        self._lock_identity(identity)
        if identity.partner_id:
            partner = identity.partner_id
            if identity.partner_link_kind != "person" or partner.is_company:
                raise ValidationError(_("Select the centralized company workflow."))
            return {
                "schema_version": SCHEMA_VERSION,
                "created": False,
                "partner": {
                    "id": partner.id,
                    "name": partner.name or identity.name,
                    "email": partner.email or "",
                    "phone": partner.phone or partner.mobile or "",
                },
                "identity": self._serialize_identity(
                    identity, binding=self._binding_for_channel(channel)
                ),
            }
        partner_values = {
            "name": name.strip()[:256],
            "company_id": channel.contact_center_company_id.id,
            "company_type": "person",
            "type": "contact",
        }
        for field_name in ("email", "phone", "mobile"):
            value = values.get(field_name)
            if value:
                if not isinstance(value, str):
                    raise ValidationError(_("Contact fields must be text."))
                partner_values[field_name] = value.strip()[:256]
        # Promotion is a constrained Contact Center operation.  Do not require
        # the broad ``base.group_partner_manager`` permission merely to create
        # this one person: the authorized channel fixes the company and the
        # allow-list above fixes every accepted field.
        partner = self.env["res.partner"].sudo().create(partner_values)
        identity.action_link_partner(partner.id)
        self._notify_identity_channels(identity)
        return {
            "schema_version": SCHEMA_VERSION,
            "created": True,
            "partner": {
                "id": partner.id,
                "name": partner.name or identity.name,
                "email": partner.email or "",
                "phone": partner.phone or partner.mobile or "",
            },
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def search_central_companies(self, channel_id, query, limit=12):
        """Search the explicit exception for a shared company-owned number."""

        channel, _member = self._authorized_channel(channel_id)
        self._check_central_company_access()
        identity = self._identity_for_channel(channel)
        if identity.partner_id or not isinstance(query, str) or len(query.strip()) < 2:
            return {"schema_version": SCHEMA_VERSION, "items": []}
        query = query.strip()[:100]
        limit = self._bounded_int(
            limit, default=12, minimum=1, maximum=30, label=_("limit")
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "items": self._search_partner_company_items(channel, query, limit),
        }

    @api.model
    def link_central_company(self, channel_id, company_partner_id):
        """Link a guest identity directly to a centralized company number."""

        channel, _member = self._authorized_channel(channel_id)
        self._check_central_company_access()
        company_partner_id = self._positive_id(company_partner_id, _("company ID"))
        # Resolve and authorize the company while acquiring the first canonical
        # lock; the identity action performs the final under-lock revalidation.
        company = self._partner_company_for_channel(
            channel, company_partner_id, lock=True
        )
        identity = self._identity_for_channel(channel)
        if identity.action_link_central_company(company.id):
            self._notify_identity_channels(identity)
        return {
            "schema_version": SCHEMA_VERSION,
            "company": self._serialize_partner_company(company),
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def create_and_link_central_company(self, channel_id, values):
        """Create and link the explicit centralized-company exception once."""

        channel, _member = self._authorized_channel(channel_id)
        self._check_central_company_access()
        company_values = self._company_creation_values(channel, values)
        identity = self._identity_for_channel(channel)
        self._lock_identity(identity)
        if identity.partner_id:
            if (
                identity.partner_link_kind != "central_company"
                or not identity.partner_id.is_company
            ):
                raise ValidationError(
                    _("The linked contact changed. Reopen the company editor.")
                )
            company = identity.partner_id
            created = False
        else:
            company = (
                self.env["res.partner"]
                .sudo()
                .with_context(no_vat_validation=False)
                .create(company_values)
            )
            identity.action_link_central_company(company.id)
            self._notify_identity_channels(identity)
            created = True
        return {
            "schema_version": SCHEMA_VERSION,
            "created": created,
            "company": self._serialize_partner_company(company),
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def search_partner_companies(
        self, channel_id, expected_partner_id, query, limit=12
    ):
        channel, _member = self._authorized_channel(channel_id)
        _identity, person = self._linked_person_for_company(
            channel, expected_partner_id
        )
        if person.parent_id or not isinstance(query, str) or len(query.strip()) < 2:
            return {"schema_version": SCHEMA_VERSION, "items": []}
        query = query.strip()[:100]
        limit = self._bounded_int(
            limit, default=12, minimum=1, maximum=30, label=_("limit")
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "items": self._search_partner_company_items(channel, query, limit),
        }

    @api.model
    def link_partner_company(self, channel_id, expected_partner_id, company_partner_id):
        channel, _member = self._authorized_channel(channel_id)
        company_partner_id = self._positive_id(company_partner_id, _("company ID"))
        # Authorize the person before resolving the supplied company, then lock
        # company -> identity -> person. Core partner field synchronization uses
        # the same parent-before-child order, avoiding a C/P lock inversion.
        self._linked_person_for_company(channel, expected_partner_id)
        company = self._partner_company_for_channel(
            channel, company_partner_id, lock=True
        )
        identity, person = self._linked_person_for_company(
            channel, expected_partner_id, lock=True
        )
        if person.parent_id and person.parent_id.id != company_partner_id:
            raise ValidationError(_("This contact already belongs to another company."))
        if not person.parent_id:
            # This is the standard Odoo person -> commercial entity relation.
            # The narrow sudo is intentional: agents have partner read access,
            # while the channel, person and target company were checked above.
            person.sudo().write({"parent_id": company.id})
            person.invalidate_recordset(["parent_id", "display_name"])
            self._notify_partner_identities(person)
        return {
            "schema_version": SCHEMA_VERSION,
            "company": self._serialize_partner_company(company),
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def create_and_link_partner_company(self, channel_id, expected_partner_id, values):
        channel, _member = self._authorized_channel(channel_id)
        company_values = self._company_creation_values(channel, values)

        identity, person = self._linked_person_for_company(
            channel, expected_partner_id, lock=True
        )
        if person.parent_id:
            raise ValidationError(_("This contact already belongs to a company."))
        company = (
            self.env["res.partner"]
            .sudo()
            .with_context(no_vat_validation=False)
            .create(company_values)
        )
        person.sudo().write({"parent_id": company.id})
        person.invalidate_recordset(["parent_id", "display_name"])
        self._notify_partner_identities(person)
        return {
            "schema_version": SCHEMA_VERSION,
            "company": self._serialize_partner_company(company),
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def unlink_partner(self, channel_id, expected_partner_id=False):
        channel, _member = self._authorized_channel(channel_id)
        if expected_partner_id in (False, None, ""):
            raise ValidationError(_("The expected linked contact ID is required."))
        expected_partner_id = self._positive_id(expected_partner_id, _("contact ID"))
        identity = self._identity_for_channel(channel)
        if identity.action_unlink_partner(expected_partner_id):
            self._notify_identity_channels(identity)
        return {
            "schema_version": SCHEMA_VERSION,
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def rename_guest(self, channel_id, name):
        channel, _member = self._authorized_channel(channel_id)
        identity = self._identity_for_channel(channel)
        identity.action_rename_guest(name)
        return {
            "schema_version": SCHEMA_VERSION,
            "identity": self._serialize_identity(
                identity, binding=self._binding_for_channel(channel)
            ),
        }

    @api.model
    def send_message(
        self,
        channel_id,
        body,
        reply_to_message_id=None,
        client_request_id=None,
        media_refs=None,
        structured_content=None,
    ):
        channel, _member = self._authorized_channel(channel_id)
        reply_to_message_id = (
            self._positive_id(reply_to_message_id, _("reply message ID"))
            if reply_to_message_id
            else None
        )
        message, outbox = self._application()._send_message(
            channel,
            body,
            reply_to_message_id=reply_to_message_id,
            client_request_id=client_request_id,
            media_refs=media_refs,
            structured_content=structured_content,
        )
        binding = outbox.message_binding_id
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "message_id": message.id,
            "outbox_command_id": outbox.id,
            "state": outbox.state,
            "client_request_id": outbox.ui_request_id,
            "message": self._serialize_message(message, binding, outbox),
        }

    @api.model
    def resend_message(self, channel_id, message_id, client_request_id):
        channel, _member = self._authorized_channel(channel_id)
        source_message_id = self._positive_id(message_id, _("message ID"))
        message, outbox, source_outbox = self._application()._resend_message(
            channel,
            source_message_id,
            client_request_id,
        )
        binding = outbox.message_binding_id
        source_binding = source_outbox.message_binding_id
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "message_id": message.id,
            "outbox_command_id": outbox.id,
            "state": outbox.state,
            "client_request_id": outbox.ui_request_id,
            "retry_of_message_id": source_binding.message_id.id,
            "source_message": self._serialize_message(
                source_binding.message_id,
                source_binding,
                source_outbox,
            ),
            "message": self._serialize_message(message, binding, outbox),
        }

    @api.model
    def react_message(
        self, channel_id, message_id, emoji, operation, client_request_id
    ):
        channel, _member = self._authorized_channel(channel_id)
        message, outbox = self._application()._send_message_mutation(
            channel,
            self._positive_id(message_id, _("message ID")),
            "react",
            emoji=emoji,
            operation=operation,
            client_request_id=client_request_id,
        )
        binding = outbox.target_message_binding_id
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "message": self._serialize_message(message, binding, outbox),
            "outbox_command_id": outbox.id,
            "state": outbox.state,
            "client_request_id": outbox.ui_request_id,
        }

    @api.model
    def edit_message(self, channel_id, message_id, new_body, client_request_id):
        channel, _member = self._authorized_channel(channel_id)
        message, outbox = self._application()._send_message_mutation(
            channel,
            self._positive_id(message_id, _("message ID")),
            "edit",
            new_text=new_body,
            client_request_id=client_request_id,
        )
        binding = outbox.target_message_binding_id
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "message": self._serialize_message(message, binding, outbox),
            "outbox_command_id": outbox.id,
            "state": outbox.state,
            "client_request_id": outbox.ui_request_id,
        }

    @api.model
    def delete_message(self, channel_id, message_id, client_request_id):
        channel, _member = self._authorized_channel(channel_id)
        message, outbox = self._application()._send_message_mutation(
            channel,
            self._positive_id(message_id, _("message ID")),
            "delete",
            client_request_id=client_request_id,
        )
        binding = outbox.target_message_binding_id
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "message": self._serialize_message(message, binding, outbox),
            "outbox_command_id": outbox.id,
            "state": outbox.state,
            "client_request_id": outbox.ui_request_id,
        }
