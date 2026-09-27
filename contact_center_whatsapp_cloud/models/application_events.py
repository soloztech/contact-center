"""Application hooks for WhatsApp Cloud statuses and system notices.

Adapters never mutate domain records; the correlation of an uncertain send and
the projection of a provider notice therefore live here, on the core paths.
"""

import collections
import hashlib

from odoo import _, api, models
from odoo.exceptions import UserError, ValidationError

from ..services.contracts import (
    WHATSAPP_CLOUD_ADAPTER_KEY,
    WHATSAPP_SYSTEM_CONTENT_TYPE,
    WHATSAPP_SYSTEM_EVENT_TYPE,
    WHATSAPP_SYSTEM_EXTENSION,
)
from ..services.normalizer import status_correlation_id
from ..services.shared_webhook import occurrence_digest

# Application, UI and mutation guards are one cohesive provider lane.
# pylint: disable=consider-merging-classes-inherited

# The only event attribute the core send reconciliation records: a ``failed``
# status has no EventDTO, but it has the same stable status identity.
_StatusEvidence = collections.namedtuple("_StatusEvidence", ["event_id"])


class ContactCenterWhatsAppCloudApplication(models.AbstractModel):
    _inherit = "contact.center.application"

    def _apply_delivery_event(self, connection, event):
        if connection.adapter_key == WHATSAPP_CLOUD_ADAPTER_KEY:
            if self._wac_reaction_receipt(connection, event):
                return self.env["contact.center.message.binding"]
            binding = self._wac_check_status_recipient(connection, event)
            self._wac_record_callback_correlation(connection, event)
            if binding and binding.provider_connection_id != connection:
                # A receipt for a message a replaced connection of this stable
                # route sent: the core applies it on the binding's own
                # connection, with its usual rules (CC-WAC-15).
                connection = connection.browse(binding.provider_connection_id.id)
        return super()._apply_delivery_event(connection, event)

    def _apply_inbound_mutation(self, connection, event, inbox_event=None):
        if connection.adapter_key == WHATSAPP_CLOUD_ADAPTER_KEY and event.mutation:
            target = self._wac_mutation_target(connection, event)
            if target:
                if not self._wac_status_recipient_matches(
                    target,
                    {
                        (address.namespace, address.value_normalized)
                        for address in event.conversation.addresses
                    },
                ):
                    raise ValidationError(
                        _(
                            "A WhatsApp reaction comes from outside its message's "
                            "conversation."
                        )
                    )
                if target.provider_connection_id != connection:
                    # A reaction to a message a replaced connection of this
                    # stable route holds: a mutation must share its target's
                    # connection, so the core applies it there (CC-WAC-19).
                    connection = connection.browse(target.provider_connection_id.id)
        return super()._apply_inbound_mutation(
            connection, event, inbox_event=inbox_event
        )

    @api.model
    def _wac_mutation_target(self, connection, event):
        """Return the one message a Cloud reaction targets on its stable route.

        A reaction reaches the live connection, while its target may stay on a
        replaced connection of the same inbox, business account and number.
        Another route's messages are never searched. No or several targets:
        the core keeps its own retry or ambiguity error.
        """

        empty = self.env["contact.center.message.binding"]
        target_id = (event.mutation or {}).get("target_external_message_id")
        if not isinstance(target_id, str) or not target_id:
            return empty
        targets = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    (
                        "provider_connection_id",
                        "in",
                        connection._wac_stable_route_connections().ids,
                    ),
                    "|",
                    ("external_message_id", "=", target_id),
                    ("client_message_id", "=", target_id),
                ],
                limit=2,
            )
        )
        return targets if len(targets) == 1 else empty

    @api.model
    def _wac_status_binding(self, connection, correlation_id, *, by_callback):
        """Return the outbound bindings (at most two) one status names.

        A message stays on the connection that sent it, while its later
        statuses arrive on the live connection of the same stable route (inbox,
        business account and phone number): an archived, replaced connection
        of that route is searched too, never another route (CC-WAC-15).
        """

        if not isinstance(correlation_id, str) or not correlation_id:
            return self.env["contact.center.message.binding"]
        return (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    (
                        "provider_connection_id",
                        "in",
                        connection._wac_stable_route_connections().ids,
                    ),
                    ("direction", "=", "outbound"),
                    (
                        "client_message_id" if by_callback else "external_message_id",
                        "=",
                        correlation_id,
                    ),
                ],
                limit=2,
            )
        )

    @api.model
    def _wac_status_recipient_matches(self, binding, recipient_keys):
        """Whether a status recipient (or reaction sender) is the message's contact.

        A callback or ``wamid`` names one message; its recipient must be one of
        the addresses that conversation established, or the target the command
        persisted. Otherwise the core would adopt that conversation for an
        unrelated recipient and add the recipient's addresses to it, even when
        the recipient has no local conversation at all (CC-WAC-04).
        """

        binding = binding.sudo()
        binding.ensure_one()
        established = {
            (alias.namespace, alias.value_normalized)
            for alias in binding.channel_binding_id.alias_ids
        }
        commands = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("message_binding_id", "=", binding.id)])
        )
        for command in commands:
            target = (command.command_json or {}).get("target_address")
            if isinstance(target, dict) and target.get("value_normalized"):
                established.add((target.get("namespace"), target["value_normalized"]))
        return bool(established.intersection(recipient_keys))

    def _wac_check_status_recipient(self, connection, event):
        """Refuse a status whose recipient is not its message's conversation.

        Returns the one binding the status names, or an empty recordset.
        """

        empty = self.env["contact.center.message.binding"]
        extension = (event.extensions or {}).get("provider.whatsapp_cloud") or {}
        correlation = extension.get("correlation")
        correlation_ids = (event.delivery or {}).get("external_message_ids") or []
        if correlation not in ("client_message_id", "provider"):
            return empty
        if len(correlation_ids) != 1:
            return empty
        binding = self._wac_status_binding(
            connection,
            correlation_ids[0],
            by_callback=correlation == "client_message_id",
        )
        # No or several bindings: the core raises its own correlation error.
        if len(binding) == 1 and not self._wac_status_recipient_matches(
            binding,
            {
                (address.namespace, address.value_normalized)
                for address in event.conversation.addresses
            },
        ):
            raise ValidationError(
                _("A WhatsApp status names a recipient outside its conversation.")
            )
        return binding if len(binding) == 1 else empty

    def _wac_reaction_receipt(self, connection, event):
        """Whether the status concerns a reaction this route sent.

        The dispatch already acknowledges these statuses; this covers a status
        that reached the inbox before the reaction's send was recorded.
        """

        extension = (event.extensions or {}).get("provider.whatsapp_cloud") or {}
        if extension.get("correlation") != "provider":
            return False
        return bool(
            self.env["contact.center.outbox.command"]._wac_sent_reaction(
                connection, extension.get("provider_message_id")
            )
        )

    def _wac_record_callback_correlation(self, connection, event):
        """Record the provider ID of an uncertain send from its own status (R08).

        The status echoes ``biz_opaque_callback_data`` (our client message ID),
        so it correlates by ``client_message_id``. Before the core projects the
        state, the ``wamid`` is written with the same core call that records it
        after a successful send; the core then never stores the callback as the
        provider message ID.
        """

        extension = (event.extensions or {}).get("provider.whatsapp_cloud") or {}
        if extension.get("correlation") != "client_message_id":
            return False
        wamid = extension.get("provider_message_id")
        callback_ids = (event.delivery or {}).get("external_message_ids") or []
        if not isinstance(wamid, str) or not wamid or len(callback_ids) != 1:
            return False
        # The recipient was checked against this binding's conversation first.
        binding = self._wac_status_binding(
            connection, callback_ids[0], by_callback=True
        )
        if len(binding) != 1:
            # The core raises its own retryable correlation error.
            return False
        if binding.external_message_id:
            if binding.external_message_id != wamid:
                raise ValidationError(
                    _("A WhatsApp status contradicts the recorded message ID.")
                )
            return False
        self._wac_bind_provider_message_id(
            binding, wamid, occurred_at=self._event_datetime(event)
        )
        return True

    @api.model
    def _wac_bind_provider_message_id(
        self, binding, wamid, *, occurred_at, details=None
    ):
        """Record the ``wamid`` of an uncertain send under the send lock.

        Shared by every status that echoes our callback, ``failed`` included.
        The send is locked before the binding, as dispatch finalization does: a
        status racing that finalization is retried instead of duplicated. The
        locked, still reconcilable sends are returned to the caller.
        """

        outboxes = self._lock_reconcilable_send_outboxes(binding)
        binding._contact_center_apply_delivery(
            "sent",
            occurred_at=occurred_at,
            external_message_id=wamid,
            details=dict(details or {}, correlated_by="whatsapp_cloud_status_callback"),
        )
        return outboxes

    @api.model
    def _wac_correlate_failed_status(self, connection, payload, *, occurred_at):
        """Correlate a ``failed`` status like any other status (CC-WAC-05).

        ``failed`` proves that Meta accepted the send under this ``wamid``; only
        its delivery failed. Through the same locked correlation, the ``wamid``
        is recorded and an uncertain send is completed as provider-accepted
        (``sent``), exactly once. ``failed`` itself is no core delivery state
        yet (phase 2): the caller keeps it as a local diagnostic. The recipient
        must belong to the message's conversation (CC-WAC-04). Returns the
        correlated binding, or an empty recordset.
        """

        empty = self.env["contact.center.message.binding"]
        status = payload.get("status") or {}
        wamid = status.get("id")
        if not isinstance(wamid, str) or not wamid:
            return empty
        correlation_id, by_callback = status_correlation_id(status)
        binding = self._wac_status_binding(
            connection, correlation_id, by_callback=by_callback
        )
        route = self.env["contact.center.conversation.ignore"]._route(
            connection, payload
        )
        if (
            len(binding) != 1
            or not route
            or not self._wac_status_recipient_matches(
                binding, {tuple(value) for value in route.get("addresses", ())}
            )
            or binding.external_message_id not in (False, wamid)
        ):
            # Ambiguous, foreign or contradictory: the diagnostic stays unlinked.
            return empty
        bound = not binding.external_message_id
        if bound:
            outboxes = self._wac_bind_provider_message_id(
                binding,
                wamid,
                occurred_at=occurred_at,
                details={"status": "failed"},
            )
        else:
            outboxes = self._lock_reconcilable_send_outboxes(binding)
        reconciled = self._complete_send_outboxes_from_positive_evidence(
            outboxes,
            _StatusEvidence("MessageStatus:%s" % occurrence_digest(payload)),
            "delivery_receipt",
        )
        if bound or reconciled:
            self._notify_ui(
                binding.channel_binding_id.channel_id,
                "delivery_updated",
                {
                    "message_id": binding.message_id.id,
                    "state": binding.delivery_state,
                    "dispatch_state": "done" if reconciled else False,
                },
            )
        return binding

    def _process_event(self, connection, event, inbox_event=None):
        if event.event_type != WHATSAPP_SYSTEM_EVENT_TYPE:
            return super()._process_event(connection, event, inbox_event=inbox_event)
        self._validate_event_scope(connection, event)
        if connection.adapter_key != WHATSAPP_CLOUD_ADAPTER_KEY:
            raise ValidationError(_("WhatsApp system notices need a Cloud connection."))
        evidence = self._validate_direct_control_event(event, WHATSAPP_SYSTEM_EXTENSION)
        if set(evidence) != {"system_type", "message_id"}:
            raise ValidationError(_("The WhatsApp system notice shape is invalid."))
        if not any(
            address.role in ("primary", "routing")
            for address in event.conversation.addresses
        ):
            raise ValidationError(
                _("WhatsApp system notices require a primary conversation address.")
            )
        self._validate_control_actor_matches_conversation(event)
        binding = self._find_channel_binding(
            connection.account_id,
            event.conversation.addresses,
            conversation_ref=event.conversation_ref,
        )
        if not binding:
            # A notice creates no conversation by itself.
            return self.env["mail.channel"]
        if binding.conversation_type != "direct" or not binding.identity_id:
            raise ValidationError(
                _("WhatsApp system notices require a direct conversation.")
            )
        digest = hashlib.sha256(evidence["message_id"].encode("utf-8")).hexdigest()
        return self._post_control_card(
            connection,
            event,
            binding,
            external_message_id="control:whatsapp-system:%s" % digest,
            content_type=WHATSAPP_SYSTEM_CONTENT_TYPE,
            body=self._wac_system_body(evidence["system_type"]),
            inbox_event=inbox_event,
        )

    @api.model
    def _wac_system_body(self, system_type):
        if system_type in ("user_changed_number", "customer_changed_number"):
            return _("O contato alterou o número do WhatsApp.")
        if system_type == "user_changed_user_id":
            return _("O identificador do contato no WhatsApp foi alterado.")
        if system_type == "customer_identity_changed":
            return _("A identidade de segurança do contato no WhatsApp foi alterada.")
        return _("O WhatsApp enviou um aviso de sistema sobre este contato.")

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
            if target.content_type == WHATSAPP_SYSTEM_CONTENT_TYPE:
                raise UserError(_("Control event cards cannot be replied to."))
        return super()._outbound_reply_binding(binding, connection, reply_to_message_id)

    def _validate_outbound_mutation(
        self, target, mutation_type, emoji, operation, new_text
    ):
        if target.content_type == WHATSAPP_SYSTEM_CONTENT_TYPE:
            raise UserError(_("Control event cards cannot be changed."))
        return super()._validate_outbound_mutation(
            target, mutation_type, emoji, operation, new_text
        )


class ContactCenterWhatsAppCloudUiApi(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def _message_reply_allowed(self, binding, connection, capabilities, is_deleted):
        if binding and binding.content_type == WHATSAPP_SYSTEM_CONTENT_TYPE:
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
        if binding and binding.content_type == WHATSAPP_SYSTEM_CONTENT_TYPE:
            result["actions"] = {
                "reply": False,
                "react": False,
                "edit": False,
                "delete": False,
                "resend": False,
            }
        return result


class ContactCenterWhatsAppCloudMessageMutation(models.Model):
    _inherit = "contact.center.message.mutation"

    @api.constrains("target_message_binding_id")
    def _check_whatsapp_system_target(self):
        if any(
            mutation.target_message_binding_id.content_type
            == WHATSAPP_SYSTEM_CONTENT_TYPE
            for mutation in self
        ):
            raise ValidationError(_("Control event cards are immutable."))
