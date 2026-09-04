import hashlib
import logging
import uuid

from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import html_escape

from ..services.adapter import AdapterError, conversation_capabilities
from ..services.dto import (
    AddressDTO,
    CommandDTO,
    ConversationDTO,
    DTOValidationError,
    MediaDTO,
    MessageDTO,
)
from ..services.media import (
    canonical_recorded_audio_duration_seconds,
    validate_provider_media_capability,
    validate_provider_media_caption_capability,
    validate_provider_recorded_audio_capability,
)
from ..services.tokens import CONTACT_CENTER_POST_TOKEN

_logger = logging.getLogger("%s.application" % __name__.rsplit(".", 1)[0])
_MAX_OUTBOUND_TEXT_CHARS = 65536


class ContactCenterApplicationOutbound(models.AbstractModel):
    _inherit = "contact.center.application"

    def _normalize_outbound_message_content(self, body, media_refs):
        if not isinstance(body, str):
            raise UserError(_("A message body must be text."))
        clean_body = body.strip()
        if len(clean_body) > _MAX_OUTBOUND_TEXT_CHARS:
            raise UserError(_("The message body exceeds the 65536 character limit."))
        if media_refs in (None, False):
            media_refs = []
        if not isinstance(media_refs, (list, tuple)):
            raise ValidationError(_("Media references must be an array."))
        if len(media_refs) > 1:
            raise UserError(_("Only one media file can be sent per message."))
        normalized_media_refs = []
        for media_ref in media_refs:
            try:
                normalized_media_refs.append(str(uuid.UUID(str(media_ref))))
            except (AttributeError, TypeError, ValueError) as error:
                raise ValidationError(
                    _("A media reference is not a valid UUID.")
                ) from error
        if not clean_body and not normalized_media_refs:
            raise UserError(_("Write a message or attach a media file."))
        return clean_body, normalized_media_refs

    def _outbound_signature(self, account, clean_body):
        """Snapshot a provider-neutral sender signature for one text command."""

        if not clean_body or not account.outbound_signature_enabled:
            return {}
        signature_name = "".join(
            " " if ord(character) < 32 or ord(character) == 127 else character
            for character in (self.env.user.name or "")
        )
        signature_name = " ".join(signature_name.split())[:120] or _("Agent")
        return {"display_name": signature_name}

    def _check_outbound_signature_capability(
        self, connection, conversation_type, text, sender_signature
    ):
        if not sender_signature:
            return True
        capabilities = conversation_capabilities(
            connection.capabilities_json or {}, conversation_type
        )
        if capabilities.get("sender_signature") is not True:
            raise UserError(_("The active provider cannot format agent signatures."))
        try:
            connection.get_adapter().validate_outbound_signature(
                connection, text, sender_signature
            )
        except AdapterError as error:
            _logger.info(
                "Contact Center provider rejected an outbound signature "
                "(connection=%s, error_class=%s)",
                connection.id,
                error.__class__.__name__,
            )
            raise UserError(
                _("The active provider rejected the agent signature.")
            ) from error
        return True

    def _active_channel_binding(self, channel):
        return self.env["contact.center.channel.binding"].search(
            [
                ("channel_id", "=", channel.id),
                ("merged_into_id", "=", False),
                ("active", "=", True),
            ],
            limit=1,
        )

    def _canonical_client_request_id(self, client_request_id):
        try:
            return str(uuid.UUID(str(client_request_id or uuid.uuid4())))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValidationError(
                _("The client request ID must be a canonical UUID.")
            ) from error

    def _matching_send_outbox(
        self,
        binding,
        client_request_id,
        clean_body,
        normalized_media_refs,
        reply_to_message_id,
    ):
        existing_outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search(
                [
                    ("channel_binding_id", "=", binding.id),
                    ("ui_request_id", "=", client_request_id),
                ],
                limit=1,
            )
        )
        if existing_outbox:
            persisted_message = (existing_outbox.command_json or {}).get(
                "message"
            ) or {}
            persisted_media_refs = [
                ((item.get("remote_locator") or {}).get("upload_ref"))
                for item in (persisted_message.get("media") or [])
            ]
            reply_binding = existing_outbox.message_binding_id.reply_to_binding_id
            persisted_reply_message_id = (
                reply_binding.message_id.id if reply_binding else False
            )
            if (
                persisted_message.get("text") != clean_body
                or persisted_media_refs != normalized_media_refs
                or (reply_to_message_id or False)
                != (persisted_reply_message_id or False)
            ):
                raise ValidationError(
                    _("The client request ID was already used for another message.")
                )
        return existing_outbox

    def _matching_retry_outbox(self, binding, source_outbox, client_request_id):
        """Return one prior retry admission or reject a reused request UUID."""

        outbox_model = self.env["contact.center.outbox.command"].sudo()
        existing_request = outbox_model.search(
            [
                ("channel_binding_id", "=", binding.id),
                ("ui_request_id", "=", client_request_id),
            ],
            limit=1,
        )
        if existing_request:
            if existing_request.retry_of_id != source_outbox:
                raise ValidationError(
                    _("The client request ID was already used for another message.")
                )
            return existing_request
        return outbox_model.search(
            [("retry_of_id", "=", source_outbox.id)], order="id", limit=1
        )

    def _outbound_connection(self, binding, capability, capability_error):
        if binding.conversation_type == "group":
            if not binding.account_id.group_outbound_enabled:
                raise UserError(
                    _("Group sending is not enabled for this Contact Center inbox.")
                )
            profile = (
                self.env["contact.center.group.profile"]
                .sudo()
                .search([("channel_binding_id", "=", binding.id)], limit=1)
            )
            connection = profile.provider_connection_id
            if connection and (
                not connection.active
                or connection.role != "primary"
                or not connection.outbound_active
            ):
                connection = self.env["contact.center.provider.connection"]
        else:
            connection = self.env["contact.center.provider.connection"].search(
                [
                    ("account_id", "=", binding.account_id.id),
                    ("role", "=", "primary"),
                    ("outbound_active", "=", True),
                    ("active", "=", True),
                ],
                limit=1,
            )
        if not connection:
            raise UserError(_("No outbound provider connection is active."))
        capabilities = conversation_capabilities(
            connection.capabilities_json or {}, binding.conversation_type
        )
        if not capabilities.get(capability):
            raise UserError(capability_error)
        return connection

    def _lock_active_outbound_scope(self, channel, binding):
        """Linearize one UI command with conversation and provider cutovers.

        Group and avatar projections already establish the global parent-first
        order ``account -> connections -> channel -> binding``. Keep the UI lane
        in the same order so a provider cutover cannot form an ABBA cycle.
        """

        expected_account = binding.account_id
        expected_account.flush_recordset(
            ["active", "group_outbound_enabled", "outbound_signature_enabled"]
        )
        connection_model = self.env["contact.center.provider.connection"].sudo()
        connections = connection_model._contact_center_lock_operational_admission(
            expected_account.ids
        )
        expected_account.invalidate_recordset(
            ["active", "group_outbound_enabled", "outbound_signature_enabled"]
        )
        connections.invalidate_recordset(
            [
                "active",
                "role",
                "inbound_active",
                "outbound_active",
                "capabilities_json",
            ]
        )
        if not expected_account.active:
            raise ValidationError(_("The Contact Center inbox is no longer active."))
        if not binding._contact_center_lock_channel_then_binding():
            raise ValidationError(_("The conversation no longer exists."))
        channel.invalidate_recordset(["active", "channel_type"])
        binding.invalidate_recordset(
            ["active", "merged_into_id", "channel_id", "account_id"]
        )
        if (
            not binding.active
            or binding.merged_into_id
            or binding.channel_id != channel
            or binding.account_id != expected_account
            or not channel.active
            or channel.channel_type != "contact_center"
        ):
            raise ValidationError(_("The conversation is no longer active."))
        if binding.conversation_type == "group":
            profiles = (
                self.env["contact.center.group.profile"]
                .sudo()
                .search([("channel_binding_id", "=", binding.id)], order="id")
            )
            if profiles:
                self.env.cr.execute(
                    "SELECT id FROM contact_center_group_profile "
                    "WHERE id = ANY(%s) ORDER BY id FOR SHARE",
                    [profiles.ids],
                )
                profiles.invalidate_recordset(
                    ["provider_connection_id", "own_protocol_participant_json"]
                )
        return True

    def _validated_outbound_uploads(
        self, binding, connection, normalized_media_refs, clean_body=""
    ):
        uploads = self.env["contact.center.media.upload"].sudo()
        if normalized_media_refs:
            uploads = (
                self.env["contact.center.media.upload"]
                .sudo()
                .search([("reference", "in", normalized_media_refs)])
            )
            upload_by_reference = {upload.reference: upload for upload in uploads}
            if set(upload_by_reference) != set(normalized_media_refs):
                raise ValidationError(_("A media upload was not found."))
            uploads = (
                self.env["contact.center.media.upload"]
                .sudo()
                .browse(
                    [upload_by_reference[value].id for value in normalized_media_refs]
                )
            )
            for upload in uploads:
                if upload.uploaded_by_user_id != self.env.user:
                    raise AccessError(_("The media upload belongs to another user."))
                if upload.channel_binding_id != binding:
                    raise AccessError(
                        _("The media upload belongs to another conversation.")
                    )
                validate_provider_media_capability(
                    conversation_capabilities(
                        connection.capabilities_json or {}, binding.conversation_type
                    ),
                    upload.kind,
                    upload.mime_type,
                    upload.size_bytes,
                )
                validate_provider_media_caption_capability(
                    conversation_capabilities(
                        connection.capabilities_json or {}, binding.conversation_type
                    ),
                    upload.kind,
                    bool(clean_body),
                )
                validate_provider_recorded_audio_capability(
                    conversation_capabilities(
                        connection.capabilities_json or {}, binding.conversation_type
                    ),
                    upload.mime_type,
                    bool(upload.is_voice_note),
                    upload.duration_seconds or 0,
                )
        return uploads

    def _outbound_reply_binding(self, binding, connection, reply_to_message_id):
        reply_binding = self.env["contact.center.message.binding"]
        reply_reference = {}
        if reply_to_message_id:
            reply_binding = self.env["contact.center.message.binding"].search(
                [
                    ("message_id", "=", reply_to_message_id),
                    ("channel_binding_id", "=", binding.id),
                ],
                limit=1,
            )
            if not reply_binding:
                raise ValidationError(
                    _("The replied message does not belong to this conversation.")
                )
            self._check_outbound_target_provider_affinity(
                reply_binding, connection, _("reply to")
            )
            if binding.conversation_type == "group":
                if (
                    reply_binding.message_state == "deleted"
                    or not reply_binding.external_message_id
                ):
                    raise ValidationError(
                        _(
                            "The replied message is no longer available at the "
                            "provider."
                        )
                    )
                capabilities = conversation_capabilities(
                    connection.capabilities_json or {}, "group"
                )
                if capabilities.get("reply") is not True:
                    raise UserError(
                        _("The active provider connection cannot reply in groups.")
                    )
                if (
                    capabilities.get("reply_requires_participant") is True
                    and not reply_binding.protocol_participant_json
                ):
                    raise ValidationError(
                        _(
                            "The group message cannot be replied to because its "
                            "protocol participant was not observed."
                        )
                    )
                try:
                    reply_reference = connection.get_adapter().prepare_reply_reference(
                        connection,
                        conversation_type="group",
                        external_message_id=reply_binding.external_message_id,
                        protocol_snapshot=reply_binding.protocol_snapshot_json or {},
                        protocol_participant=(
                            reply_binding.protocol_participant_json or {}
                        ),
                    )
                except AdapterError as error:
                    _logger.info(
                        "Contact Center provider rejected a group reply reference "
                        "(connection=%s, error_class=%s)",
                        connection.id,
                        error.__class__.__name__,
                    )
                    raise ValidationError(
                        _(
                            "The provider cannot build a safe reply reference for "
                            "this message."
                        )
                    ) from error
            else:
                # Keep the established direct-message behavior: an operator may
                # create a local threaded reply while the target is still awaiting
                # provider correlation.  Group replies cannot use this fallback.
                reply_reference = {
                    "external_message_id": reply_binding.external_message_id or "",
                    "protocol_snapshot": reply_binding.protocol_snapshot_json or {},
                }
        return reply_binding, reply_reference

    def _check_outbound_target_provider_affinity(self, target, connection, operation):
        """Keep provider-owned correlation IDs on their originating transport."""

        if target.provider_connection_id != connection:
            raise ValidationError(
                _(
                    "This message belongs to a previous provider connection and "
                    "cannot be used to %(operation)s after a provider migration.",
                    operation=operation,
                )
            )
        return True

    def _create_outbound_message_projection(
        self,
        channel,
        binding,
        connection,
        clean_body,
        reply_binding,
        reply_to_message_id,
        uploads,
    ):
        message = channel._contact_center_post(
            origin="outbound",
            body=html_escape(clean_body),
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
            parent_id=reply_to_message_id or False,
        )
        command_id = str(uuid.uuid4())
        client_message_id = (
            connection.get_adapter().derive_client_message_id(command_id) or ""
        )
        message_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": binding.id,
                    "provider_connection_id": connection.id,
                    "direction": "outbound",
                    "origin": "agent",
                    "content_type": uploads.kind if len(uploads) == 1 else "text",
                    "client_message_id": client_message_id,
                    "reply_to_binding_id": reply_binding.id if reply_binding else False,
                    "protocol_snapshot_json": {},
                    "delivery_state": "queued",
                }
            )
        )
        outbound_media = []
        outbound_attachments = []
        for upload in uploads:
            attachment = upload._consume(message_binding, user=self.env.user)
            outbound_attachments.append(attachment)
            outbound_media.append(upload._as_outbound_media_dto(attachment))
        if outbound_media:
            media_bindings = self._create_media_bindings(
                message_binding, outbound_media, state="ready"
            )
            for media_binding, attachment in zip(
                media_bindings.sorted("sequence"), outbound_attachments
            ):
                media_binding.write({"attachment_id": attachment.id})
        return (
            message,
            message_binding,
            outbound_media,
            command_id,
            client_message_id,
        )

    def _validated_retry_source(self, source_outbox, binding):
        """Return the immutable content snapshot of one safely failed send."""

        source_binding = source_outbox.message_binding_id
        try:
            source_command = CommandDTO.from_dict(source_outbox.command_json or {})
        except DTOValidationError as error:
            raise ValidationError(
                _("The failed message no longer has a safe retry snapshot.")
            ) from error
        message = source_command.message
        if (
            source_outbox.state != "dead"
            or source_outbox.command_type != "send_message"
            or source_outbox.resolution
            or not source_binding
            or source_binding.channel_binding_id != binding
            or source_binding.provider_connection_id
            != source_outbox.provider_connection_id
            or source_binding.direction != "outbound"
            or source_binding.origin != "agent"
            or source_binding.message_state == "deleted"
            or source_binding.delivery_state != "failed"
            or source_binding.external_message_id
            or not message
            or source_command.command_type != "send_message"
            or source_command.account_ref != source_outbox.account_id.external_ref
            or source_command.connection_ref
            != source_outbox.provider_connection_id.external_ref
            or source_command.conversation_ref != binding.conversation_ref
            or source_command.client_message_id
            != (source_binding.client_message_id or "")
            or message.client_message_id != (source_binding.client_message_id or "")
            or message.content_type != source_binding.content_type
        ):
            raise ValidationError(
                _("Only an unequivocally failed outbound message can be resent.")
            )
        clean_body = message.text.strip()
        if message.text != clean_body or len(clean_body) > _MAX_OUTBOUND_TEXT_CHARS:
            raise ValidationError(
                _("The failed message no longer has a safe retry snapshot.")
            )
        source_media = source_binding.media_ids.sorted("sequence")
        if len(source_media) > 1 or len(source_media) != len(message.media):
            raise ValidationError(
                _("The failed message media cannot be retried safely.")
            )
        if not clean_body and not source_media:
            raise ValidationError(
                _("The failed message no longer has content to resend.")
            )
        reply_binding = source_binding.reply_to_binding_id
        expected_reply_external_id = (
            reply_binding.external_message_id or "" if reply_binding else ""
        )
        if (
            (reply_binding and reply_binding.message_state == "deleted")
            or message.reply_to_external_id != expected_reply_external_id
            or bool(source_command.reply_to) != bool(reply_binding)
            or (
                source_command.reply_to
                and (
                    (source_command.reply_to.get("external_message_id") or "")
                    != expected_reply_external_id
                    or (source_command.reply_to.get("protocol_snapshot") or {})
                    != message.protocol_snapshot
                )
            )
        ):
            raise ValidationError(
                _("The failed message reply reference cannot be retried safely.")
            )
        return source_command, clean_body, source_media, reply_binding

    def _validated_retry_media(
        self, source_media, source_command, connection, clean_body
    ):
        """Validate and load one attachment without reusing its consumed upload."""

        if not source_media:
            return []
        source_media.ensure_one()
        media = source_media
        command_media = source_command.message.media[0]
        attachment = media.attachment_id.sudo().exists()
        locator = command_media.remote_locator or {}
        locator_attachment_id = locator.get("attachment_id")
        if isinstance(locator_attachment_id, str) and locator_attachment_id.isdigit():
            locator_attachment_id = int(locator_attachment_id)
        metadata_matches = bool(
            media.state == "ready"
            and attachment
            and len(attachment) == 1
            and attachment.type == "binary"
            and attachment in media.message_binding_id.message_id.attachment_ids
            and locator_attachment_id == attachment.id
            and (media.remote_locator_json or {}) == locator
            and command_media.kind == media.kind
            and command_media.mime_type == (media.mime_type or "")
            and command_media.file_name == (media.file_name or "")
            and command_media.size_bytes == media.size_bytes
            and command_media.sha256 == (media.sha256 or "")
            and command_media.is_voice_note == bool(media.is_voice_note)
            and command_media.duration_seconds == (media.duration_seconds or 0)
            and command_media.width == (media.width or 0)
            and command_media.height == (media.height or 0)
            and set(locator).issubset({"attachment_id", "upload_ref"})
        )
        if not metadata_matches:
            raise ValidationError(
                _("The failed message media cannot be retried safely.")
            )
        content = attachment.raw or b""
        if isinstance(content, memoryview):
            content = content.tobytes()
        elif isinstance(content, bytearray):
            content = bytes(content)
        if (
            not isinstance(content, bytes)
            or len(content) != media.size_bytes
            or not media.sha256
            or hashlib.sha256(content).hexdigest() != media.sha256.lower()
        ):
            raise ValidationError(
                _("The failed message attachment is no longer intact.")
            )
        upload_reference = locator.get("upload_ref")
        if upload_reference:
            consumed_upload = (
                self.env["contact.center.media.upload"]
                .sudo()
                .search([("reference", "=", upload_reference)], limit=1)
            )
            if consumed_upload and (
                consumed_upload.state != "consumed"
                or consumed_upload.consumed_message_binding_id
                != media.message_binding_id
                or consumed_upload.attachment_id != attachment
            ):
                raise ValidationError(
                    _("The failed message upload cannot be retried safely.")
                )
        capabilities = conversation_capabilities(
            connection.capabilities_json or {},
            media.message_binding_id.channel_binding_id.conversation_type,
        )
        validate_provider_media_capability(
            capabilities, media.kind, media.mime_type, media.size_bytes
        )
        validate_provider_media_caption_capability(
            capabilities, media.kind, bool(clean_body)
        )
        canonical_duration = media.duration_seconds or 0
        if media.is_voice_note or media.duration_seconds:
            canonical_duration = canonical_recorded_audio_duration_seconds(
                content, media.mime_type
            )
            if canonical_duration != media.duration_seconds:
                raise ValidationError(
                    _("The failed audio duration no longer matches its content.")
                )
            try:
                connection.get_adapter().validate_recorded_audio_upload(
                    connection,
                    content=content,
                    mimetype=media.mime_type,
                    is_voice_note=bool(media.is_voice_note),
                    duration_seconds=media.duration_seconds or 0,
                )
            except AdapterError as error:
                raise ValidationError(
                    _("The active provider rejected the failed audio attachment.")
                ) from error
        validate_provider_recorded_audio_capability(
            capabilities,
            media.mime_type,
            bool(media.is_voice_note),
            canonical_duration,
        )
        return [{"media": media, "content": content}]

    def _create_retry_message_projection(
        self,
        channel,
        binding,
        connection,
        clean_body,
        reply_binding,
        retry_media,
    ):
        """Create an independent message/binding/attachment projection for a retry."""

        message = channel._contact_center_post(
            origin="outbound",
            body=html_escape(clean_body),
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
            parent_id=reply_binding.message_id.id if reply_binding else False,
        )
        command_id = str(uuid.uuid4())
        client_message_id = (
            connection.get_adapter().derive_client_message_id(command_id) or ""
        )
        content_type = retry_media[0]["media"].kind if retry_media else "text"
        message_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": binding.id,
                    "provider_connection_id": connection.id,
                    "direction": "outbound",
                    "origin": "agent",
                    "content_type": content_type,
                    "client_message_id": client_message_id,
                    "reply_to_binding_id": reply_binding.id if reply_binding else False,
                    "protocol_snapshot_json": {},
                    "delivery_state": "queued",
                }
            )
        )
        outbound_media = []
        for item in retry_media:
            source_media = item["media"]
            attachment = (
                self.env["ir.attachment"]
                .sudo()
                .with_context(image_no_postprocess=True)
                .create(
                    {
                        "name": (source_media.file_name or source_media.kind)[:255],
                        "type": "binary",
                        "raw": item["content"],
                        "mimetype": source_media.mime_type,
                        "res_model": "mail.message",
                        "res_id": message.id,
                    }
                )
            )
            message.sudo().with_context(
                contact_center_post_token=CONTACT_CENTER_POST_TOKEN
            ).write({"attachment_ids": [(4, attachment.id)]})
            outbound_media.append(
                MediaDTO(
                    kind=source_media.kind,
                    remote_locator={"attachment_id": attachment.id},
                    mime_type=source_media.mime_type or "",
                    file_name=source_media.file_name or "",
                    size_bytes=source_media.size_bytes or 0,
                    sha256=source_media.sha256 or "",
                    is_voice_note=bool(source_media.is_voice_note),
                    duration_seconds=source_media.duration_seconds or 0,
                    width=source_media.width or 0,
                    height=source_media.height or 0,
                )
            )
            new_media = self._create_media_bindings(
                message_binding, outbound_media[-1:], state="ready"
            )
            new_media.write({"attachment_id": attachment.id})
        return (
            message,
            message_binding,
            outbound_media,
            command_id,
            client_message_id,
        )

    def _outbound_route(self, binding, missing_address_message):
        address_dtos = tuple(
            AddressDTO(
                namespace=alias.namespace,
                value=alias.value_raw,
                value_normalized=alias.value_normalized,
                role=alias.role,
                source_field=alias.source_field or "",
                confidence=alias.confidence,
            )
            for alias in binding.alias_ids
        )
        target_roles = (
            ("group",)
            if binding.conversation_type == "group"
            else ("primary", "routing")
        )
        target_address = next(
            (address for address in address_dtos if address.role in target_roles),
            None,
        )
        if not target_address:
            raise ValidationError(missing_address_message)
        return address_dtos, target_address

    def _create_send_outbox(
        self,
        binding,
        connection,
        message_binding,
        reply_binding,
        reply_reference,
        clean_body,
        sender_signature,
        outbound_media,
        address_dtos,
        target_address,
        command_id,
        client_message_id,
        client_request_id,
        own_protocol_participant=None,
        retry_of=None,
    ):
        command_dto = CommandDTO(
            command_id=command_id,
            command_type="send_message",
            account_ref=binding.account_id.external_ref,
            connection_ref=connection.external_ref,
            conversation_ref=binding.conversation_ref,
            conversation=ConversationDTO(
                addresses=address_dtos,
                conversation_type=binding.conversation_type,
            ),
            target_address=target_address,
            own_protocol_participant=own_protocol_participant,
            message=MessageDTO(
                content_type=message_binding.content_type,
                text=clean_body,
                client_message_id=client_message_id,
                reply_to_external_id=(
                    (reply_reference.get("external_message_id") or "")
                    if reply_reference
                    else ""
                ),
                protocol_snapshot=(reply_reference.get("protocol_snapshot") or {}),
                media=tuple(outbound_media),
            ),
            reply_to=reply_reference,
            options={"sender_signature": sender_signature} if sender_signature else {},
            client_message_id=client_message_id,
        )
        outbox_values = {
            "account_id": binding.account_id.id,
            "provider_connection_id": connection.id,
            "channel_binding_id": binding.id,
            "message_binding_id": message_binding.id,
            "ui_request_id": client_request_id,
            "outbox_idempotency_key": command_id,
            "command_type": "send_message",
            "command_json": command_dto.to_dict(),
        }
        if retry_of:
            retry_of.ensure_one()
            outbox_values.update(
                {
                    "retry_of_id": retry_of.id,
                    "retry_requested_at": fields.Datetime.now(),
                    "retry_requested_by_id": self.env.user.id,
                }
            )
        outbox = self.env["contact.center.outbox.command"].sudo().create(outbox_values)
        self.env["contact.center.delivery.event"].sudo().create(
            {
                "message_binding_id": message_binding.id,
                "state": "queued",
                "occurred_at": fields.Datetime.now(),
            }
        )
        return outbox

    def _send_message(
        self,
        channel,
        body,
        reply_to_message_id=None,
        client_request_id=None,
        media_refs=None,
    ):
        self._check_agent()
        channel.ensure_one()
        channel.check_access_rights("read")
        channel.check_access_rule("read")
        channel._contact_center_member_for_current_user()
        clean_body, normalized_media_refs = self._normalize_outbound_message_content(
            body, media_refs
        )
        binding = self._active_channel_binding(channel)
        if not binding:
            raise ValidationError(_("The conversation has no active binding."))
        binding._contact_center_ensure_outbound_supported(
            operation="send_message",
            has_text=bool(clean_body),
            has_media=bool(normalized_media_refs),
            has_reply=bool(reply_to_message_id),
        )
        client_request_id = self._canonical_client_request_id(client_request_id)
        self._lock_active_outbound_scope(channel, binding)
        # The initial check gives the agent a fast error. Repeat it under the
        # canonical locks because archival/merge may have completed while this
        # request was waiting for the conversation.
        binding._contact_center_ensure_outbound_supported(
            operation="send_message",
            has_text=bool(clean_body),
            has_media=bool(normalized_media_refs),
            has_reply=bool(reply_to_message_id),
        )
        existing_outbox = self._matching_send_outbox(
            binding,
            client_request_id,
            clean_body,
            normalized_media_refs,
            reply_to_message_id,
        )
        if existing_outbox:
            return existing_outbox.message_binding_id.message_id, existing_outbox
        sender_signature = self._outbound_signature(binding.account_id, clean_body)
        connection = self._outbound_connection(
            binding,
            "send_message",
            _("The active provider connection cannot send messages yet."),
        )
        capabilities = conversation_capabilities(
            connection.capabilities_json or {}, binding.conversation_type
        )
        self._check_outbound_signature_capability(
            connection, binding.conversation_type, clean_body, sender_signature
        )
        own_protocol_participant = self._group_own_protocol_participant(
            binding,
            connection,
            required=bool(
                binding.conversation_type == "group"
                and capabilities.get("reply_requires_participant") is True
            ),
        )
        uploads = self._validated_outbound_uploads(
            binding, connection, normalized_media_refs, clean_body=clean_body
        )
        reply_binding, reply_reference = self._outbound_reply_binding(
            binding, connection, reply_to_message_id
        )
        connection._contact_center_record_outbound_admission()
        (
            message,
            message_binding,
            outbound_media,
            command_id,
            client_message_id,
        ) = self._create_outbound_message_projection(
            channel,
            binding,
            connection,
            clean_body,
            reply_binding,
            reply_to_message_id,
            uploads,
        )
        address_dtos, target_address = self._outbound_route(
            binding, _("The conversation has no outbound routing address.")
        )
        outbox = self._create_send_outbox(
            binding,
            connection,
            message_binding,
            reply_binding,
            reply_reference,
            clean_body,
            sender_signature,
            outbound_media,
            address_dtos,
            target_address,
            command_id,
            client_message_id,
            client_request_id,
            own_protocol_participant=own_protocol_participant,
        )
        self._publish_message_created(channel, message, direction="outbound")
        return message, outbox

    def _resend_message(self, channel, message_id, client_request_id):
        """Create one independently auditable retry of a safely failed send."""

        self._check_agent()
        channel.ensure_one()
        channel.check_access_rights("read")
        channel.check_access_rule("read")
        channel._contact_center_member_for_current_user()
        binding = self._active_channel_binding(channel)
        if not binding:
            raise ValidationError(_("The conversation has no active binding."))
        binding.account_id._contact_center_check_user_scope()
        source_binding = self.env["contact.center.message.binding"].search(
            [
                ("message_id", "=", message_id),
                ("channel_binding_id", "=", binding.id),
            ],
            limit=1,
        )
        if not source_binding:
            raise ValidationError(
                _("The failed message does not belong to this conversation.")
            )
        source_outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search(
                [
                    ("message_binding_id", "=", source_binding.id),
                    ("command_type", "=", "send_message"),
                ],
                order="id desc",
                limit=1,
            )
        )
        if not source_outbox:
            raise ValidationError(_("The failed message has no outbound command."))
        client_request_id = self._canonical_client_request_id(client_request_id)
        self._lock_active_outbound_scope(channel, binding)
        binding.account_id._contact_center_check_user_scope()
        binding._contact_center_ensure_outbound_supported(
            operation="send_message",
            has_text=bool((source_outbox.command_json or {}).get("message")),
            has_media=bool(source_binding.media_ids),
            has_reply=bool(source_binding.reply_to_binding_id),
        )
        existing_outbox = self._matching_retry_outbox(
            binding, source_outbox, client_request_id
        )
        if existing_outbox:
            return (
                existing_outbox.message_binding_id.message_id,
                existing_outbox,
                source_outbox,
            )

        # Updating only the monotonic audit barrier makes a concurrent request
        # retry its database transaction and observe the child created by the
        # winner. The terminal state itself remains untouched.
        source_outbox._contact_center_admit_safe_retry()
        source_binding.invalidate_recordset(
            [
                "delivery_state",
                "external_message_id",
                "message_state",
                "reply_to_binding_id",
                "media_ids",
            ]
        )
        (
            source_command,
            clean_body,
            source_media,
            source_reply,
        ) = self._validated_retry_source(source_outbox, binding)
        connection = self._outbound_connection(
            binding,
            "send_message",
            _("The active provider connection cannot resend messages."),
        )
        connection.invalidate_recordset(
            [
                "active",
                "role",
                "outbound_active",
                "state",
                "identity_mismatch_latched",
                "last_state_observed_at",
                "last_health_at",
                "capabilities_json",
            ]
        )
        connection.account_id.invalidate_recordset(["active"])
        if not connection._contact_center_outbound_is_available():
            raise UserError(
                _("The active provider connection must be healthy before resending.")
            )
        capabilities = conversation_capabilities(
            connection.capabilities_json or {}, binding.conversation_type
        )
        sender_signature = self._outbound_signature(binding.account_id, clean_body)
        self._check_outbound_signature_capability(
            connection, binding.conversation_type, clean_body, sender_signature
        )
        own_protocol_participant = self._group_own_protocol_participant(
            binding,
            connection,
            required=bool(
                binding.conversation_type == "group"
                and capabilities.get("reply_requires_participant") is True
            ),
        )
        reply_binding, reply_reference = self._outbound_reply_binding(
            binding,
            connection,
            source_reply.message_id.id if source_reply else None,
        )
        retry_media = self._validated_retry_media(
            source_media, source_command, connection, clean_body
        )
        address_dtos, target_address = self._outbound_route(
            binding, _("The conversation has no outbound routing address.")
        )
        connection._contact_center_record_outbound_admission()
        (
            message,
            message_binding,
            outbound_media,
            command_id,
            client_message_id,
        ) = self._create_retry_message_projection(
            channel,
            binding,
            connection,
            clean_body,
            reply_binding,
            retry_media,
        )
        outbox = self._create_send_outbox(
            binding,
            connection,
            message_binding,
            reply_binding,
            reply_reference,
            clean_body,
            sender_signature,
            outbound_media,
            address_dtos,
            target_address,
            command_id,
            client_message_id,
            client_request_id,
            own_protocol_participant=own_protocol_participant,
            retry_of=source_outbox,
        )
        source_outbox.invalidate_recordset(["retry_child_ids"])
        self._notify_ui(
            channel,
            "message_updated",
            {"message_id": source_binding.message_id.id},
        )
        self._publish_message_created(channel, message, direction="outbound")
        return message, outbox, source_outbox

    def _outbound_mutation_target(self, binding, message_id):
        target = self.env["contact.center.message.binding"].search(
            [
                ("message_id", "=", message_id),
                ("channel_binding_id", "=", binding.id),
            ],
            limit=1,
        )
        if not target:
            raise ValidationError(_("The target message is outside this conversation."))
        if not target.external_message_id:
            raise UserError(_("Wait until the provider confirms the target message."))
        return target

    def _normalize_outbound_mutation_request(
        self, mutation_type, emoji, operation, new_text
    ):
        """Validate request-owned values without consulting mutable target state."""

        command_type_by_mutation = {
            "react": "react",
            "edit": "edit_message",
            "delete": "delete_message",
        }
        capability_by_mutation = {
            "react": "react",
            "edit": "edit_message",
            "delete": "delete_message",
        }
        if mutation_type not in command_type_by_mutation:
            raise ValidationError(_("Unsupported message action."))
        if mutation_type == "react":
            if operation not in ("add", "remove"):
                raise ValidationError(_("Unsupported reaction operation."))
            if operation == "add" and (
                not isinstance(emoji, str)
                or not emoji
                or len(emoji) > 32
                or any(ord(character) < 32 for character in emoji)
            ):
                raise ValidationError(_("Choose a valid reaction."))
        if mutation_type == "edit":
            if not isinstance(new_text, str) or not new_text.strip():
                raise ValidationError(_("The edited message cannot be empty."))
            new_text = new_text.strip()
            if len(new_text) > 65536:
                raise ValidationError(_("The edited message is too long."))
        return (
            command_type_by_mutation[mutation_type],
            capability_by_mutation[mutation_type],
            new_text,
        )

    def _check_outbound_mutation_target_access(self, target, mutation_type):
        """Enforce stable target ownership before returning an idempotent replay."""

        if mutation_type in ("edit", "delete") and (
            target.direction != "outbound"
            or target.message_id.author_id != self.env.user.partner_id
        ):
            raise AccessError(_("You can only change messages sent by you."))
        return True

    def _validate_outbound_mutation(
        self, target, mutation_type, emoji, operation, new_text
    ):
        values = self._normalize_outbound_mutation_request(
            mutation_type, emoji, operation, new_text
        )
        self._check_outbound_mutation_target_access(target, mutation_type)
        if target.message_state == "deleted":
            raise UserError(_("The message was already deleted."))
        return values

    def _outbound_mutation_options(
        self, target, mutation_type, emoji, operation, new_text, sender_signature
    ):
        options = {
            "target_external_message_id": target.external_message_id,
            "target_from_me": target.direction == "outbound",
        }
        if target.channel_binding_id.conversation_type == "direct":
            source = (target.protocol_snapshot_json or {}).get("source") or {}
            participant = source.get("sender_normalized") or source.get("sender") or ""
            if participant:
                options["target_participant"] = participant
        if mutation_type == "react":
            options.update({"emoji": emoji, "operation": operation})
        elif mutation_type == "edit":
            options["new_text"] = new_text
            if sender_signature:
                options["sender_signature"] = sender_signature
        return options

    def _group_outbound_mutation_participants(self, binding, connection, target):
        if binding.conversation_type != "group":
            return None, None
        own_participant = self._group_own_protocol_participant(
            binding, connection, required=True
        )
        try:
            target_participant = AddressDTO.from_dict(
                target.protocol_participant_json or {}
            )
        except DTOValidationError as error:
            raise UserError(
                _("Wait until the provider correlates the target group " "participant.")
            ) from error
        profile = self._group_profile_for_connection(binding, connection)
        own_roster_participant = profile._resolve_protocol_participant(
            (own_participant,), active_only=True
        )
        target_roster_participant = profile._resolve_protocol_participant(
            (target_participant,), active_only=True
        )
        if not own_roster_participant or not target_roster_participant:
            raise UserError(
                _("This group action is waiting for the current participant " "roster.")
            )
        if (
            target.direction == "outbound"
            and target_roster_participant != own_roster_participant
        ):
            raise ValidationError(
                _("The outbound target is not owned by the current group sender.")
            )
        return own_participant, target_participant

    def _matching_mutation_outbox(
        self, binding, target, client_request_id, command_type, options
    ):
        existing = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search(
                [
                    ("channel_binding_id", "=", binding.id),
                    ("ui_request_id", "=", client_request_id),
                ],
                limit=1,
            )
        )
        if existing and (
            existing.target_message_binding_id != target
            or existing.command_type != command_type
            or {
                key: value
                for key, value in (
                    (existing.command_json or {}).get("options") or {}
                ).items()
                if key != "sender_signature"
            }
            != {
                key: value
                for key, value in (options or {}).items()
                if key != "sender_signature"
            }
        ):
            raise ValidationError(
                _("The client request ID was already used for another action.")
            )
        return existing

    def _create_mutation_outbox(
        self,
        binding,
        target,
        connection,
        client_request_id,
        mutation_type,
        emoji,
        operation,
        new_text,
        command_type,
        options,
        address_dtos,
        target_address,
        own_protocol_participant=None,
        target_protocol_participant=None,
    ):
        actor_partner = self.env.user.partner_id
        if mutation_type == "react":
            actor_partner = binding.account_id.technical_author_id
            if not actor_partner:
                raise UserError(
                    _("Configure the account technical author before reacting.")
                )
        mutation = (
            self.env["contact.center.message.mutation"]
            .sudo()
            .create(
                {
                    "target_message_binding_id": target.id,
                    "provider_connection_id": connection.id,
                    "client_request_id": client_request_id,
                    "mutation_type": mutation_type,
                    "direction": "outbound",
                    "actor_user_id": self.env.user.id,
                    "actor_partner_id": actor_partner.id,
                    "reaction_emoji": emoji if mutation_type == "react" else False,
                    "reaction_operation": operation,
                    "new_text": new_text if mutation_type == "edit" else False,
                    "occurred_at": fields.Datetime.now(),
                    "details_json": options,
                }
            )
        )
        command_id = str(uuid.uuid4())
        command_dto = CommandDTO(
            command_id=command_id,
            command_type=command_type,
            account_ref=binding.account_id.external_ref,
            connection_ref=connection.external_ref,
            conversation_ref=binding.conversation_ref,
            conversation=ConversationDTO(
                addresses=address_dtos,
                conversation_type=binding.conversation_type,
            ),
            target_address=target_address,
            own_protocol_participant=own_protocol_participant,
            target_protocol_participant=target_protocol_participant,
            message=(
                MessageDTO(content_type="text", text=new_text)
                if mutation_type == "edit"
                else None
            ),
            options=options,
        )
        return (
            self.env["contact.center.outbox.command"]
            .sudo()
            .create(
                {
                    "account_id": binding.account_id.id,
                    "provider_connection_id": connection.id,
                    "channel_binding_id": binding.id,
                    "target_message_binding_id": target.id,
                    "mutation_id": mutation.id,
                    "ui_request_id": client_request_id,
                    "outbox_idempotency_key": command_id,
                    "command_type": command_type,
                    "command_json": command_dto.to_dict(),
                }
            )
        )

    def _send_message_mutation(
        self,
        channel,
        message_id,
        mutation_type,
        *,
        client_request_id=None,
        emoji="",
        operation="add",
        new_text="",
    ):
        self._check_agent()
        channel.ensure_one()
        channel.check_access_rights("read")
        channel.check_access_rule("read")
        channel._contact_center_member_for_current_user()
        binding = self._active_channel_binding(channel)
        if not binding:
            raise ValidationError(_("The conversation has no active binding."))
        binding._contact_center_ensure_outbound_supported(operation=mutation_type)
        target = self._outbound_mutation_target(binding, message_id)
        command_type, capability, new_text = self._normalize_outbound_mutation_request(
            mutation_type, emoji, operation, new_text
        )
        self._check_outbound_mutation_target_access(target, mutation_type)
        client_request_id = self._canonical_client_request_id(client_request_id)
        self._lock_active_outbound_scope(channel, binding)
        binding._contact_center_ensure_outbound_supported(operation=mutation_type)
        target = self._outbound_mutation_target(binding, message_id)
        command_type, capability, new_text = self._normalize_outbound_mutation_request(
            mutation_type, emoji, operation, new_text
        )
        self._check_outbound_mutation_target_access(target, mutation_type)
        options = self._outbound_mutation_options(
            target,
            mutation_type,
            emoji,
            operation,
            new_text,
            {},
        )
        existing = self._matching_mutation_outbox(
            binding, target, client_request_id, command_type, options
        )
        if existing:
            return target.message_id, existing
        # Mutable state is checked only after the exact replay lane above. A
        # successful delete makes the target terminal, but retrying the same UI
        # UUID must still return its original durable command.
        self._validate_outbound_mutation(
            target, mutation_type, emoji, operation, new_text
        )
        sender_signature = (
            self._outbound_signature(binding.account_id, new_text)
            if mutation_type == "edit"
            else {}
        )
        options = self._outbound_mutation_options(
            target,
            mutation_type,
            emoji,
            operation,
            new_text,
            sender_signature,
        )
        connection = self._outbound_connection(
            binding,
            capability,
            _("The provider does not support this message action."),
        )
        self._check_outbound_target_provider_affinity(
            target, connection, _("apply this action")
        )
        self._check_outbound_signature_capability(
            connection, binding.conversation_type, new_text, sender_signature
        )
        (
            own_protocol_participant,
            target_protocol_participant,
        ) = self._group_outbound_mutation_participants(binding, connection, target)
        address_dtos, target_address = self._outbound_route(
            binding, _("The conversation has no outbound address.")
        )
        connection._contact_center_record_outbound_admission()
        outbox = self._create_mutation_outbox(
            binding,
            target,
            connection,
            client_request_id,
            mutation_type,
            emoji,
            operation,
            new_text,
            command_type,
            options,
            address_dtos,
            target_address,
            own_protocol_participant=own_protocol_participant,
            target_protocol_participant=target_protocol_participant,
        )
        return target.message_id, outbox
