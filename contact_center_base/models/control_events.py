import hashlib
import re

from odoo import _, api, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import html_escape

_CALL_STATES = frozenset({"offered", "accepted", "terminated"})
_CALL_DIRECTIONS = frozenset({"inbound", "outbound", "unknown"})
_CONTROL_CONTENT_TYPES = frozenset(
    {
        "call.offer",
        "call.accept",
        "call.terminate",
        "identity.security.changed",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Control-event projection is a cohesive application/UI service lane. Odoo composes
# these `_inherit` fragments with the other service files at registry load time.
# pylint: disable=consider-merging-classes-inherited


class ContactCenterApplicationControlEvents(models.AbstractModel):
    _inherit = "contact.center.application"

    @api.model
    def _validate_direct_control_event(self, event, extension_name):
        if event.conversation.conversation_type != "direct":
            raise ValidationError(_("Control events require a direct conversation."))
        if (
            event.direction != "inbound"
            or event.is_from_me
            or event.origin != "provider"
        ):
            raise ValidationError(
                _("Control events must be inbound provider observations.")
            )
        if (
            event.message
            or event.reply_to
            or event.delivery
            or event.attribution
            or event.mutation
        ):
            raise ValidationError(
                _("Control events cannot contain message or mutation payloads.")
            )
        invalid_extensions = tuple(
            key
            for key in event.extensions
            if key != extension_name
            and (not isinstance(key, str) or not key.startswith("provider."))
        )
        if invalid_extensions:
            raise ValidationError(
                _("Control events contain unsupported extension fields.")
            )
        extension = event.extensions.get(extension_name)
        if not isinstance(extension, dict):
            raise ValidationError(_("The control event extension is invalid."))
        return extension

    @api.model
    def _validate_control_actor_matches_conversation(self, event):
        actor_keys = {
            (address.namespace, address.value_normalized)
            for address in event.actor.addresses
        }
        conversation_keys = {
            (address.namespace, address.value_normalized)
            for address in event.conversation.addresses
        }
        if not actor_keys.intersection(conversation_keys):
            raise ValidationError(
                _("The control event actor does not belong to the conversation.")
            )

    @api.model
    def _validate_call_event(self, event):
        call = self._validate_direct_control_event(event, "call")
        if set(call) != {"ref", "state", "direction"}:
            raise ValidationError(_("The call event shape is invalid."))
        if not isinstance(call.get("ref"), str) or not _SHA256_RE.fullmatch(
            call["ref"]
        ):
            raise ValidationError(_("The call reference must be a SHA-256 digest."))
        if call.get("state") not in _CALL_STATES:
            raise ValidationError(_("The call state is invalid."))
        if call.get("direction") not in _CALL_DIRECTIONS:
            raise ValidationError(_("The call direction is invalid."))
        if not event.actor.addresses:
            raise ValidationError(_("Call events require a remote actor address."))
        if not any(
            address.role in ("primary", "routing")
            for address in event.conversation.addresses
        ):
            raise ValidationError(
                _("Call events require a primary or routing conversation address.")
            )
        self._validate_control_actor_matches_conversation(event)
        return call

    @api.model
    def _validate_identity_security_event(self, event):
        evidence = self._validate_direct_control_event(event, "identity_security")
        if set(evidence) != {"change_kind", "implicit"}:
            raise ValidationError(_("The identity security event shape is invalid."))
        if evidence.get("change_kind") != "primary_device" or not isinstance(
            evidence.get("implicit"), bool
        ):
            raise ValidationError(_("The identity security evidence is invalid."))
        if not event.actor.addresses:
            raise ValidationError(
                _("Identity security events require a remote actor address.")
            )
        if not any(
            address.role in ("primary", "routing")
            for address in event.conversation.addresses
        ):
            raise ValidationError(
                _(
                    "Identity security events require a primary or routing "
                    "conversation address."
                )
            )
        self._validate_control_actor_matches_conversation(event)
        return evidence

    @api.model
    def _call_card_body(self, state, direction):
        if state == "accepted":
            return _("Chamada atendida.")
        if state == "terminated":
            return _("Chamada encerrada.")
        return {
            "inbound": _("Chamada recebida."),
            "outbound": _("Chamada realizada."),
            "unknown": _("Chamada de direção desconhecida."),
        }[direction]

    @api.model
    def _ensure_control_guest_member(self, binding):
        identity = binding.identity_id
        guest = identity.mail_guest_id.sudo()
        members = binding.channel_id.sudo().channel_member_ids
        if guest not in members.guest_id:
            binding.channel_id._contact_center_reconcile_members(
                partner_ids=members.partner_id.ids,
                guest_ids=members.guest_id.ids + guest.ids,
            )
        return guest

    @api.model
    def _post_control_card(
        self,
        connection,
        event,
        binding,
        *,
        external_message_id,
        content_type,
        body,
        enrich_addresses=False,
        inbox_event=None,
    ):
        if (
            binding.account_id != connection.account_id
            or binding.conversation_type != "direct"
            or not binding.identity_id
        ):
            raise ValidationError(
                _("The control event does not belong to this direct conversation.")
            )
        self._lock_inbound_projection_binding(binding, connection.account_id)
        if enrich_addresses:
            self._enrich_channel_aliases(binding, event.conversation.addresses)
        binding_model = self.env["contact.center.message.binding"].sudo()
        existing = binding_model.search(
            [
                ("channel_binding_id", "=", binding.id),
                ("external_message_id", "=", external_message_id),
            ],
            limit=1,
        )
        if existing:
            if (
                existing.provider_connection_id != connection
                or existing.content_type != content_type
            ):
                raise ValidationError(
                    _("The existing control event belongs to another projection.")
                )
            if inbox_event and not existing.source_inbox_event_id:
                existing.write({"source_inbox_event_id": inbox_event.id})
                self._notify_ui(
                    binding.channel_id,
                    "message_updated",
                    {"message_id": existing.message_id.id},
                )
            return existing.message_id
        guest = self._ensure_control_guest_member(binding)
        public_user = self.env.ref("base.public_user")
        message = (
            binding.channel_id.with_user(public_user)
            .sudo()
            .with_context(guest=guest)
            ._contact_center_post(
                origin="inbound",
                body=html_escape(body),
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
                date=self._event_datetime(event),
                partner_ids=[],
            )
        )
        binding_model.create(
            {
                "message_id": message.id,
                "channel_binding_id": binding.id,
                "provider_connection_id": connection.id,
                "source_inbox_event_id": inbox_event.id if inbox_event else False,
                "direction": "inbound",
                "origin": "provider",
                "content_type": content_type,
                "external_message_id": external_message_id,
                "delivery_state": "delivered",
            }
        )
        self._publish_message_created(binding.channel_id, message, direction="inbound")
        return message

    @api.model
    def _process_call_control_event(self, connection, event, inbox_event=None):
        call = self._validate_call_event(event)
        identity = self._resolve_identity(
            connection.account_id,
            event.actor,
            inbox_event=inbox_event,
            observed_name_at=self._event_datetime(event),
        )
        binding = self._resolve_channel(connection.account_id, identity, event)
        state = call["state"]
        return self._post_control_card(
            connection,
            event,
            binding,
            external_message_id="control:call:%s:%s" % (call["ref"], state),
            content_type={
                "offered": "call.offer",
                "accepted": "call.accept",
                "terminated": "call.terminate",
            }[state],
            body=self._call_card_body(state, call["direction"]),
            enrich_addresses=True,
            inbox_event=inbox_event,
        )

    @api.model
    def _process_identity_security_control_event(
        self, connection, event, inbox_event=None
    ):
        evidence = self._validate_identity_security_event(event)
        binding = self._find_channel_binding(
            connection.account_id,
            event.conversation.addresses,
            conversation_ref=event.conversation_ref,
        )
        if not binding:
            return self.env["mail.channel"]
        if binding.conversation_type != "direct" or not binding.identity_id:
            raise ValidationError(
                _("Identity security events require an existing direct conversation.")
            )
        digest = hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()
        return self._post_control_card(
            connection,
            event,
            binding,
            external_message_id="control:identity-security:%s" % digest,
            content_type="identity.security.changed",
            inbox_event=inbox_event,
            body=(
                _(
                    "O provedor sinalizou uma possível alteração automática da "
                    "identidade de segurança do dispositivo principal."
                )
                if evidence["implicit"]
                else _(
                    "A identidade de segurança do dispositivo principal foi "
                    "alterada."
                )
            ),
        )

    def _process_event(self, connection, event, inbox_event=None):
        if event.event_type not in (
            "conversation.call.updated",
            "identity.security.changed",
        ):
            return super()._process_event(connection, event, inbox_event=inbox_event)
        self._validate_event_scope(connection, event)
        if inbox_event:
            inbox_event = inbox_event.sudo()
            inbox_event.ensure_one()
            if (
                inbox_event.provider_connection_id != connection
                or inbox_event.account_id != connection.account_id
                or inbox_event.company_id != connection.company_id
                or inbox_event.normalized_dto_json != event.to_dict()
            ):
                raise ValidationError(
                    _("The control source must be this exact normalized inbox event.")
                )
        if event.event_type == "conversation.call.updated":
            return self._process_call_control_event(
                connection, event, inbox_event=inbox_event
            )
        return self._process_identity_security_control_event(
            connection, event, inbox_event=inbox_event
        )

    def _outbound_reply_binding(self, binding, connection, reply_to_message_id):
        if reply_to_message_id:
            target = (
                self.env["contact.center.message.binding"]
                .sudo()
                .search(
                    [
                        ("message_id", "=", reply_to_message_id),
                        ("channel_binding_id", "=", binding.id),
                    ],
                    limit=1,
                )
            )
            if target.content_type in _CONTROL_CONTENT_TYPES:
                raise UserError(_("Control event cards cannot be replied to."))
        return super()._outbound_reply_binding(binding, connection, reply_to_message_id)

    def _validate_outbound_mutation(
        self, target, mutation_type, emoji, operation, new_text
    ):
        if target.content_type in _CONTROL_CONTENT_TYPES:
            raise UserError(_("Control event cards cannot be changed."))
        return super()._validate_outbound_mutation(
            target, mutation_type, emoji, operation, new_text
        )


class ContactCenterUiApiControlEvents(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def _message_reply_allowed(self, binding, connection, capabilities, is_deleted):
        if binding and binding.content_type in _CONTROL_CONTENT_TYPES:
            return False
        return super()._message_reply_allowed(
            binding, connection, capabilities, is_deleted
        )

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
        result = super()._serialize_message(
            message,
            binding=binding,
            outbox=outbox,
            group_delivery_summary=group_delivery_summary,
            parent_binding_by_message=parent_binding_by_message,
            group_mutation_allowed_by_binding=group_mutation_allowed_by_binding,
        )
        if binding and binding.content_type in _CONTROL_CONTENT_TYPES:
            result["actions"] = {
                "reply": False,
                "react": False,
                "edit": False,
                "delete": False,
                "resend": False,
            }
        return result


class ContactCenterMessageMutationControlEvents(models.Model):
    _inherit = "contact.center.message.mutation"

    @api.constrains("target_message_binding_id")
    def _check_control_event_target(self):
        if any(
            mutation.target_message_binding_id.content_type in _CONTROL_CONTENT_TYPES
            for mutation in self
        ):
            raise ValidationError(_("Control event cards are immutable."))
