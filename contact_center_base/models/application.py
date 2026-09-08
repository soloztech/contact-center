import datetime
import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import html_escape

from ..services.adapter import TransientAdapterError, UnsupportedEventError
from ..services.dto import (
    SCHEMA_VERSION,
    ActorDTO,
    AddressDTO,
    CommandDTO,
    DTOValidationError,
    EventDTO,
    MediaDTO,
)
from ..services.timeline import message_chronology_key
from ..services.tokens import CONTACT_CENTER_MEMBERSHIP_TOKEN

_logger = logging.getLogger(__name__)
_COMPANY_PORTABLE_IDENTITY_NAMESPACES = frozenset({"whatsapp.pn"})
_ALIAS_LAST_SEEN_WRITE_INTERVAL = datetime.timedelta(minutes=5)
_REPLY_TARGET_RETRY_DELAYS = (5, 10)


class IdentityConflictError(Exception):
    """Stop inbox processing until an identity conflict is resolved explicitly."""

    def __init__(self, message, conflict_values=None):
        super().__init__(message)
        self.conflict_values = conflict_values or {}

    def __str__(self):
        message = super().__str__()
        evidence = self.conflict_values.get("address_evidence_json")
        if not evidence:
            return message
        return "%s; evidence=%s" % (
            message,
            json.dumps(evidence, sort_keys=True, default=str),
        )


class GroupRosterRefreshRequired(TransientAdapterError):
    """Defer one privileged group mutation until a post-event roster exists.

    The identifiers are local database keys only.  Inbox processing catches this
    outside its projection savepoint so that the metadata job request can be
    committed before the current queue job finishes successfully.
    """

    def __init__(self, group_profile_id, provider_connection_id, required_after):
        super().__init__(
            "group administrator authorization is waiting for a current roster"
        )
        self.group_profile_id = int(group_profile_id)
        self.provider_connection_id = int(provider_connection_id)
        self.required_after = required_after


class ContactCenterApplication(models.AbstractModel):
    _name = "contact.center.application"
    _description = "Contact Center Application Service"

    def _check_agent(self):
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_agent"
        ):
            raise AccessError(_("You are not a Contact Center agent."))

    @api.model
    def _alias_observation_updates(
        self, alias, address, observed_at, *, promote_resolution_scope=False
    ):
        """Return material alias changes and a bounded ``last_seen_at`` touch.

        Repeated webhooks commonly carry the same identity and conversation
        addresses.  Writing those alias rows for every message turns them into hot
        rows without adding useful precision.  Material observations still persist
        immediately; otherwise the timestamp advances at most once per interval.
        """

        updates = {}
        if alias.value_raw != address.value:
            updates["value_raw"] = address.value
        if (
            promote_resolution_scope
            and address.resolution_scope == "company"
            and address.confidence == "protocol"
        ):
            if alias.resolution_scope != "company":
                updates["resolution_scope"] = "company"
            # Scope and confidence form one portability invariant. Persisting a
            # company-scoped alias as merely "observed" makes it invisible to the
            # company-wide resolver and can create a second guest in another inbox.
            if alias.confidence == "observed":
                updates["confidence"] = "protocol"
                if address.source_field:
                    updates["source_field"] = address.source_field
        last_seen_at = fields.Datetime.to_datetime(alias.last_seen_at)
        if (
            updates
            or not last_seen_at
            or last_seen_at <= observed_at - _ALIAS_LAST_SEEN_WRITE_INTERVAL
        ):
            updates["last_seen_at"] = observed_at
        return updates

    @api.model
    def _company_portable_identity_namespaces(self):
        """Return semantic address namespaces safe to resolve across inboxes.

        Provider modules may extend this contract for a genuinely portable
        identifier. Opaque account identifiers such as LID, PSID and IGSID must
        remain account-scoped.
        """

        return _COMPANY_PORTABLE_IDENTITY_NAMESPACES

    def _publish_message_created(self, channel, message, direction=None):
        """Advance the canonical inbox cursor and publish one stable UI event."""

        channel.ensure_one()
        message.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM mail_channel WHERE id = %s FOR UPDATE", [channel.id]
        )
        channel.invalidate_recordset(
            ["contact_center_last_message_id", "contact_center_last_message_at"]
        )
        current_message = channel.contact_center_last_message_id
        advances_cursor = not current_message or message_chronology_key(
            message
        ) > message_chronology_key(current_message)
        if advances_cursor:
            channel.sudo().with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
            ).write(
                {
                    "contact_center_last_message_id": message.id,
                    "contact_center_last_message_at": message.date
                    or fields.Datetime.now(),
                }
            )
        payload = {"message_id": message.id}
        if direction in ("inbound", "outbound"):
            payload["direction"] = direction
        self._notify_ui(channel, "message_created", payload)

    def _lock_inbound_projection_binding(self, binding, account):
        """Serialize a new inbound projection in the parent-first lock order."""

        binding.ensure_one()
        account.ensure_one()
        if not binding._contact_center_lock_identity_channel_binding():
            raise ValidationError(_("The conversation no longer exists."))
        channel = binding.channel_id
        channel.invalidate_recordset(["active", "channel_type"])
        if (
            not binding.active
            or binding.merged_into_id
            or binding.account_id != account
            or not channel.active
            or channel.channel_type != "contact_center"
        ):
            raise ValidationError(_("The conversation is no longer active."))
        return binding

    def _lock_inbound_account_scope(self, account):
        """Serialize the first identity/conversation projection for one inbox.

        Access writers take the account lock before touching conversation rows. The
        aggregate lock here follows that same order, prevents duplicate first-seen
        identity/channel projections from racing on their unique keys, and keeps an
        automatic assignee from being revoked between eligibility validation and the
        channel write.
        """

        account.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id = %s FOR UPDATE",
            [account.id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("The Contact Center inbox no longer exists."))
        account.invalidate_recordset(
            [
                "active",
                "owner_user_id",
                "default_team_id",
                "auto_assignment_user_id",
            ]
        )
        return account

    def _advance_inbound_projection_revision(self, account):
        """Fence a first-seen projection against an older PostgreSQL snapshot.

        A row lock alone does not refresh an Odoo REPEATABLE READ snapshot. Update
        this dedicated revision only before creating canonical identity/routing
        data, so a concurrent waiter restarts instead of acting on stale absence
        while established conversations keep their normal throughput.
        """

        account.ensure_one()
        account.flush_model(["inbound_projection_revision"])
        self.env.cr.execute(
            """
            UPDATE contact_center_account
               SET inbound_projection_revision = inbound_projection_revision + 1
             WHERE id = %s
         RETURNING id
            """,
            [account.id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("The Contact Center inbox no longer exists."))
        account.invalidate_recordset(["inbound_projection_revision"])
        return account

    def _apply_inbound_conversation_lifecycle(self, binding):
        """Apply lifecycle invariants after one genuinely new inbound message.

        The caller owns the canonical binding/channel projection lock and invokes
        this only after provider-message deduplication. Consequently, a webhook
        replay, receipt, mutation, or self-side message cannot reopen a conversation. A
        resolved conversation always reopens; an archived conversation deliberately
        remains archived, mirroring the persistent archive semantics of WhatsApp.
        """

        binding.ensure_one()
        channel = binding.channel_id
        channel.invalidate_recordset(["contact_center_state"])
        if channel.contact_center_state != "resolved":
            return False
        channel.sudo().with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write({"contact_center_state": "open"})
        self._notify_ui(
            channel,
            "conversation_updated",
            {"changed_fields": ["state"]},
        )
        return True

    def _auto_assign_inbound_conversation(self, binding):
        """Assign one genuinely new inbound message without stealing ownership.

        The caller already owns the channel/binding projection lock and has already
        ruled out a duplicate provider message. A concurrent manual claim therefore
        wins if it committed first, while this conditional write can never overwrite
        an existing responsible agent.
        """

        binding.ensure_one()
        channel = binding.channel_id
        channel.invalidate_recordset(["contact_center_responsible_id"])
        if channel.contact_center_responsible_id:
            return False
        account = binding.account_id
        assignee = account.auto_assignment_user_id
        if not assignee or assignee not in account._contact_center_effective_users():
            return False
        channel.sudo().with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write({"contact_center_responsible_id": assignee.id})
        self._notify_ui(
            channel,
            "conversation_updated",
            {"changed_fields": ["responsible_id"]},
        )
        return True

    def _validate_event_scope(self, connection, event):
        connection.ensure_one()
        if not isinstance(event, EventDTO):
            raise ValidationError(_("The inbound payload must be an EventDTO."))
        if event.account_ref != connection.account_id.external_ref:
            raise ValidationError(
                _("The event account reference does not match the connection.")
            )
        if event.connection_ref != connection.external_ref:
            raise ValidationError(_("The event connection reference does not match."))
        if event.platform != connection.account_id.platform:
            raise ValidationError(_("The event platform does not match the account."))

    def _process_event(self, connection, event, inbox_event=None):
        self._validate_event_scope(connection, event)
        if event.event_type == "connection.updated":
            state = event.extensions.get("state")
            if not state:
                raise ValidationError(
                    _("Connection events require a provider-neutral state.")
                )
            health_confirmation_required = bool(
                state == "connected"
                and event.extensions.get("health_confirmation_required") is True
            )
            connection.sudo()._apply_provider_state_event(
                "degraded" if health_confirmation_required else state,
                observed_at=(
                    inbox_event.create_date
                    if inbox_event and inbox_event.create_date
                    else event.occurred_at
                ),
                detail=(
                    "identity_unverified"
                    if health_confirmation_required
                    else event.extensions.get("detail")
                    or event.extensions.get("reason")
                ),
                observation_sequence=(
                    inbox_event.id if inbox_event and inbox_event.id else 0
                ),
            )
            if health_confirmation_required:
                # This schedules a low-priority, unique OCA job and performs no
                # provider I/O inside inbound processing.
                connection.sudo()._enqueue_health_check(priority=30)
            return connection
        if event.conversation.conversation_type == "group":
            return self._process_group_message(
                connection, event, inbox_event=inbox_event
            )
        if event.conversation.conversation_type != "direct":
            raise UnsupportedEventError(
                "only direct and inbound group conversations are supported"
            )
        if event.event_type in (
            "attribution.observed",
            "identity.avatar.changed",
            "delivery.updated",
        ):
            return self._process_direct_control_event(connection, event)
        if event.mutation:
            return self._apply_inbound_mutation(
                connection, event, inbox_event=inbox_event
            )
        if event.event_type != "message.created":
            raise UnsupportedEventError(
                "event type is persisted but not implemented yet: %s" % event.event_type
            )
        if not event.message or (
            not event.message.text
            and not event.message.media
            and not event.message.structured_content
        ):
            raise UnsupportedEventError(
                "message events require text or a supported media descriptor"
            )
        if not event.message.external_message_id:
            raise ValidationError(_("Message events require a provider message ID."))
        if event.is_from_me:
            if event.direction != "outbound":
                raise ValidationError(
                    _("Messages marked from_me must have outbound direction.")
                )
            return self._reconcile_from_me(connection, event, inbox_event=inbox_event)
        if event.direction != "inbound":
            raise UnsupportedEventError(
                "non-from_me outbound provider messages are unsupported"
            )
        if not any(
            address.role in ("primary", "routing")
            for address in event.conversation.addresses
        ):
            raise ValidationError(
                _(
                    "Inbound direct conversations require a primary or routing "
                    "conversation address."
                )
            )
        return self._post_inbound(connection, event, inbox_event=inbox_event)

    def _process_direct_control_event(self, connection, event):
        if event.event_type == "attribution.observed":
            return self._process_attribution_observation(connection, event)
        if event.event_type == "identity.avatar.changed":
            return self._process_identity_avatar_hint(connection, event)
        return self._apply_delivery_event(connection, event)

    def _process_attribution_observation(self, connection, event):
        """Accept attribution telemetry without manufacturing a chat message."""

        if (
            not event.attribution
            or event.message
            or event.reply_to
            or event.delivery
            or event.mutation
        ):
            raise ValidationError(
                _("Attribution observations require only attribution evidence.")
            )
        if (
            event.direction != "inbound"
            or event.is_from_me
            or event.origin != "provider"
        ):
            raise ValidationError(
                _("Attribution observations must be inbound provider events.")
            )
        if not any(
            address.role in ("primary", "routing")
            for address in event.conversation.addresses
        ):
            raise ValidationError(
                _(
                    "Attribution observations require a primary or routing "
                    "conversation address."
                )
            )
        binding = self._find_channel_binding(
            connection.account_id,
            event.conversation.addresses,
            conversation_ref=event.conversation_ref,
        )
        if not binding:
            return self.env["mail.channel"]
        if binding.conversation_type != "direct":
            raise ValidationError(
                _("An attribution observation resolved to a non-direct conversation.")
            )
        self._lock_inbound_projection_binding(binding, connection.account_id)
        self._enrich_channel_aliases(binding, event.conversation.addresses)
        return binding.channel_id

    def _process_group_message(self, connection, event, inbox_event=None):
        """Project inbound group messages and correlate sends from this account."""

        if event.event_type == "group.metadata.changed":
            return self._process_group_metadata_hint(connection, event)
        self._group_conversation_addresses(event)
        if event.event_type == "delivery.updated":
            return self._apply_group_delivery_event(
                connection, event, inbox_event=inbox_event
            )
        if event.mutation:
            return self._apply_inbound_mutation(
                connection, event, inbox_event=inbox_event
            )
        if event.event_type != "message.created":
            raise UnsupportedEventError(
                "group event type is persisted but not implemented: %s"
                % event.event_type
            )
        if event.is_from_me:
            if event.direction != "outbound":
                raise ValidationError(
                    _("Group messages marked from_me must have outbound direction.")
                )
            if not event.message or (
                not event.message.text
                and not event.message.media
                and not event.message.structured_content
            ):
                raise UnsupportedEventError(
                    "from_me group messages require text or media content"
                )
            if not event.message.external_message_id:
                raise ValidationError(
                    _("Group messages require a provider message ID.")
                )
            return self._reconcile_from_me(connection, event, inbox_event=inbox_event)
        if not connection.account_id._contact_center_accepts_group_inbound():
            raise UnsupportedEventError(
                "inbound group messages are disabled for this account"
            )
        if event.direction != "inbound" or event.origin != "provider":
            raise UnsupportedEventError(
                "only inbound provider group messages are supported"
            )
        if not event.message or (
            not event.message.text
            and not event.message.media
            and not event.message.structured_content
        ):
            raise UnsupportedEventError(
                "group metadata without human message content is unsupported"
            )
        if not event.message.external_message_id:
            raise ValidationError(_("Group messages require a provider message ID."))
        if not event.actor.addresses:
            raise ValidationError(
                _("Inbound group messages require an identified participant.")
            )
        return self._post_inbound(connection, event, inbox_event=inbox_event)

    @api.model
    def _group_protocol_participant(self, event):
        """Return the one provider-observed sender address for a group message."""

        if event.conversation.conversation_type != "group":
            return None
        participants = tuple(
            address
            for address in event.actor.addresses
            if address.role == "sender" and address.confidence == "protocol"
        )
        if len(participants) != 1:
            raise ValidationError(
                _("Group messages require exactly one protocol sender address.")
            )
        participant = participants[0]
        if participant.role in ("group", "routing"):
            raise ValidationError(
                _("A group protocol participant cannot be a routing address.")
            )
        return participant

    @api.model
    def _group_profile_for_connection(self, binding, connection):
        binding.ensure_one()
        connection.ensure_one()
        profile = binding.group_profile_ids.sudo()[:1]
        if not profile or profile.provider_connection_id != connection:
            raise ValidationError(
                _("The group conversation is not tied to this provider " "connection.")
            )
        return profile

    @api.model
    def _group_roster_participant(
        self,
        binding,
        connection,
        addresses,
        *,
        active_only=False,
        enrich=False,
    ):
        profile = self._group_profile_for_connection(binding, connection)
        participant = profile._resolve_protocol_participant(
            addresses, active_only=active_only, enrich=enrich
        )
        if not participant:
            raise TransientAdapterError(
                "group participant aliases are waiting for roster correlation"
            )
        return profile, participant

    @api.model
    def _group_roster_authority_status(self, profile, event, inbox_event=None):
        """Return whether the complete roster was observed after this event."""

        now = fields.Datetime.to_datetime(fields.Datetime.now()).replace(microsecond=0)
        required_after = (
            fields.Datetime.to_datetime(inbox_event.create_date)
            if inbox_event and inbox_event.create_date
            else self._event_datetime(event)
        ).replace(microsecond=0)
        # Provider timestamps can be ahead of the Odoo host. Local inbox receipt
        # is used in production; clamping keeps direct service calls and fixtures
        # from waiting forever on clock skew.
        required_after = min(required_after, now)
        sync_requested_at = fields.Datetime.to_datetime(profile.sync_requested_at)
        last_synced_at = fields.Datetime.to_datetime(profile.last_synced_at)
        authoritative = bool(
            profile.metadata_state == "ready"
            and profile.roster_complete
            and sync_requested_at
            and sync_requested_at >= required_after
            and last_synced_at
            and last_synced_at >= required_after
            and profile.applied_revision == profile.sync_revision
        )
        return authoritative, required_after

    @api.model
    def _group_mutation_roster_context(
        self, binding, connection, event, target, mutation, inbox_event=None
    ):
        """Resolve participants and the roster authorization horizon."""

        profile = self._group_profile_for_connection(binding, connection)
        roster_is_authoritative = True
        required_after = None
        if mutation.get("type") == "delete":
            (
                roster_is_authoritative,
                required_after,
            ) = self._group_roster_authority_status(
                profile, event, inbox_event=inbox_event
            )

        def refresh_or_original(error):
            if mutation.get("type") == "delete" and not roster_is_authoritative:
                return GroupRosterRefreshRequired(
                    profile.id, connection.id, required_after
                )
            return error

        try:
            _profile, actor_participant = self._group_roster_participant(
                binding,
                connection,
                event.actor.addresses,
                active_only=False,
                enrich=True,
            )
        except TransientAdapterError as error:
            # A participant that joined after the cached roster cannot be resolved
            # yet. Convert that ordinary retry into the durable refresh dependency.
            raise refresh_or_original(error) from error
        try:
            target_participant = self._group_mutation_target_participant(
                binding, connection, target, mutation
            )
        except TransientAdapterError as error:
            raise refresh_or_original(error) from error
        return (
            profile,
            actor_participant,
            target_participant,
            roster_is_authoritative,
            required_after,
        )

    @api.model
    def _validate_group_delete_actor(
        self,
        profile,
        connection,
        actor_participant,
        actor_is_author,
        roster_is_authoritative,
        required_after,
    ):
        if actor_is_author:
            return True
        if not roster_is_authoritative:
            raise GroupRosterRefreshRequired(profile.id, connection.id, required_after)
        if not actor_participant.active or actor_participant.role not in (
            "admin",
            "superadmin",
        ):
            raise ValidationError(
                _(
                    "Group deletes must be performed by the message author or "
                    "an active group administrator."
                )
            )
        return True

    @api.model
    def _validate_group_inbound_mutation(
        self, connection, event, target, inbox_event=None
    ):
        """Fail closed when a group mutation cannot prove actor and target lanes."""

        binding = target.channel_binding_id
        if binding.conversation_type != "group":
            raise ValidationError(
                _("The group mutation target is not a group message.")
            )
        self._group_protocol_participant(event)
        mutation = event.mutation or {}
        (
            profile,
            actor_participant,
            target_participant,
            roster_is_authoritative,
            required_after,
        ) = self._group_mutation_roster_context(
            binding,
            connection,
            event,
            target,
            mutation,
            inbox_event=inbox_event,
        )
        # The correlated binding is the account-scoped canonical lane.  Some
        # providers expose nested mutation keys from the remote actor's
        # perspective, so their normalized boolean is retained as evidence but
        # can never override the target ledger.
        target_from_me = target.direction == "outbound"

        own_address = None
        own_participant = self.env["contact.center.group.participant"]
        if event.is_from_me or target_from_me:
            own_address = self._group_own_protocol_participant(
                binding, connection, required=False
            )
            if not own_address:
                # Missing evidence is expected while a new session/configuration
                # revision is waiting for its first roster.  Corrupt or ambiguous
                # persisted evidence still raises ValidationError from the resolver
                # and must remain a permanent inbox failure.
                raise TransientAdapterError(
                    "group mutation is waiting for current session roster correlation"
                )
            own_participant = profile._resolve_protocol_participant(
                (own_address,), active_only=True
            )
            if not own_participant:
                raise ValidationError(
                    _("The current group sender is absent from the active roster.")
                )
        if event.is_from_me and actor_participant != own_participant:
            raise ValidationError(
                _("The self-side group mutation actor is not the current sender.")
            )
        if target_from_me and target_participant != own_participant:
            raise ValidationError(
                _("The mutation target is not owned by the current group sender.")
            )
        mutation_type = mutation.get("type")
        actor_is_author = (
            event.is_from_me == target_from_me
            and actor_participant == target_participant
        )
        if mutation_type == "edit" and not actor_is_author:
            raise ValidationError(
                _("Group edits must be performed by the message author.")
            )
        if mutation_type == "delete":
            # Administrator deletes are the only inbound mutation whose
            # authorization depends on mutable roster roles.  A six-hour cache is
            # suitable for display, but it cannot be authoritative for a delete
            # received after that snapshot: a participant may have been promoted
            # or demoted in the meantime.  Require one complete provider read at
            # or after local receipt before accepting or rejecting the command.
            self._validate_group_delete_actor(
                profile,
                connection,
                actor_participant,
                actor_is_author,
                roster_is_authoritative,
                required_after,
            )
        return True

    @api.model
    def _group_mutation_target_participant(self, binding, connection, target, mutation):
        try:
            persisted_target = AddressDTO.from_dict(
                target.protocol_participant_json or {}
            )
        except DTOValidationError as error:
            raise ValidationError(
                _("The group mutation target has no valid protocol participant.")
            ) from error
        _profile, target_participant = self._group_roster_participant(
            binding,
            connection,
            (persisted_target,),
            active_only=False,
        )
        observed_values = mutation.get("target_protocol_participant") or {}
        if not observed_values:
            return target_participant
        try:
            observed = AddressDTO.from_dict(observed_values)
        except DTOValidationError as error:
            raise ValidationError(
                _("The observed mutation target participant is invalid.")
            ) from error
        _profile, observed_participant = self._group_roster_participant(
            binding,
            connection,
            (observed,),
            active_only=False,
            enrich=True,
        )
        if observed_participant != target_participant:
            raise ValidationError(
                _(
                    "The observed mutation target belongs to another group "
                    "participant."
                )
            )
        return target_participant

    @api.model
    def _event_reply_binding(self, channel_binding, event, inbox_event=None):
        """Give reordered quotes a short grace without dropping human content.

        An original from before capture began may never arrive. Keep that external
        reference in the normalized event/provider snapshot, without inventing a
        local parent or making the new message depend on historical availability.
        """

        reply_external_id = event.message.reply_to_external_id if event.message else ""
        if not reply_external_id:
            return self.env["contact.center.message.binding"]
        reply_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("channel_binding_id", "=", channel_binding.id),
                    ("external_message_id", "=", reply_external_id),
                ],
                limit=1,
            )
        )
        attempt = inbox_event.attempts if inbox_event else 0
        if not reply_binding and 1 <= attempt <= len(_REPLY_TARGET_RETRY_DELAYS):
            error = TransientAdapterError("message reply arrived before its target")
            error.retry_after_seconds = _REPLY_TARGET_RETRY_DELAYS[attempt - 1]
            raise error
        return reply_binding

    def _process_group_metadata_hint(self, connection, event):
        """Invalidate an existing group profile without trusting webhook deltas."""

        if (
            event.direction != "inbound"
            or event.origin != "provider"
            or event.is_from_me
            or event.message
            or event.mutation
        ):
            raise UnsupportedEventError(
                "group metadata hints must be inbound provider invalidations"
            )
        group_addresses = self._group_conversation_addresses(event)
        binding = self._find_channel_binding(
            connection.account_id,
            group_addresses,
            conversation_ref=event.conversation_ref,
        )
        # Metadata webhooks must not populate the inbox with empty conversations.
        # The first human message remains the creation boundary.
        if not binding:
            return connection
        if binding.conversation_type != "group" or binding.identity_id:
            raise ValidationError(
                _("A group metadata hint resolved to a non-group conversation.")
            )
        self._lock_inbound_projection_binding(binding, connection.account_id)
        self._enrich_channel_aliases(binding, group_addresses)
        profile = self.env["contact.center.group.profile"]._get_or_create(
            binding, connection
        )
        hint = event.extensions.get("group_metadata_hint") or {}
        if not isinstance(hint, dict):
            raise ValidationError(_("The group metadata hint is invalid."))
        # The webhook is only an invalidation hint. The provider pull remains the
        # authoritative source for group name, avatar and roster.
        profile._request_sync(connection, force=True)
        return profile

    def _process_identity_avatar_hint(self, connection, event):
        """Invalidate an existing direct avatar without creating inbox entities."""

        if (
            event.direction != "inbound"
            or event.origin != "provider"
            or event.is_from_me
            or event.message
            or event.mutation
        ):
            raise UnsupportedEventError(
                "identity avatar hints must be inbound provider invalidations"
            )
        addresses = tuple(
            dict.fromkeys((*event.conversation.addresses, *event.actor.addresses))
        )
        if not addresses or any(address.role == "group" for address in addresses):
            raise ValidationError(_("The identity avatar hint has invalid addresses."))
        hint = event.extensions.get("identity_avatar_hint") or {}
        if not isinstance(hint, dict):
            raise ValidationError(_("The identity avatar hint is invalid."))
        binding = self._find_channel_binding(
            connection.account_id,
            event.conversation.addresses or event.actor.addresses,
            conversation_ref=event.conversation_ref,
        )
        # Profile events never create a guest or an empty conversation. A regular
        # human message remains the only direct-conversation creation boundary.
        if not binding:
            return connection
        if binding.conversation_type != "direct" or not binding.identity_id:
            raise ValidationError(
                _("An identity avatar hint resolved to a non-direct conversation.")
            )
        self._lock_inbound_projection_binding(binding, connection.account_id)
        self._enrich_channel_aliases(binding, event.conversation.addresses)
        self._enrich_identity_aliases(
            connection.account_id, binding.identity_id, event.actor.addresses
        )
        binding._request_identity_avatar_sync(connection, force=True)
        return binding

    @api.model
    def _group_conversation_addresses(self, event):
        if any(address.role != "group" for address in event.conversation.addresses):
            raise ValidationError(
                _(
                    "Group conversation addresses must contain only group routing "
                    "identifiers."
                )
            )
        addresses = tuple(
            address
            for address in event.conversation.addresses
            if address.role == "group"
        )
        if not addresses:
            raise ValidationError(
                _("Inbound group messages require a group conversation address.")
            )
        group_values = {address.value_normalized for address in addresses}
        if any(
            address.role in ("group", "routing") for address in event.actor.addresses
        ):
            raise ValidationError(
                _(
                    "A group participant must use a person or device identifier, "
                    "not a group routing identifier."
                )
            )
        if any(
            address.value_normalized in group_values
            for address in event.actor.addresses
        ):
            raise ValidationError(
                _("A group participant cannot use the group conversation address.")
            )
        return addresses

    @api.model
    def _event_datetime(self, event):
        return event.occurred_at.astimezone(datetime.timezone.utc).replace(
            tzinfo=None, microsecond=0
        )

    def _find_message_binding(self, connection, message, channel_binding=None):
        keys = tuple(
            dict.fromkeys(
                value
                for value in (
                    message.external_message_id,
                    message.client_message_id,
                )
                if value
            )
        )
        if not keys:
            return self.env["contact.center.message.binding"]
        domain = [
            ("provider_connection_id", "=", connection.id),
            ("direction", "=", "outbound"),
            "|",
            ("external_message_id", "in", list(keys)),
            ("client_message_id", "in", list(keys)),
        ]
        if channel_binding:
            domain.insert(1, ("channel_binding_id", "=", channel_binding.id))
        bindings = self.env["contact.center.message.binding"].sudo().search(domain)
        if len(bindings) > 1:
            raise ValidationError(
                _("Provider message identifiers resolve to multiple local messages.")
            )
        return bindings

    def _find_channel_binding(self, account, addresses, conversation_ref=None):
        exact_keys = {
            (address.namespace, address.value_normalized) for address in addresses
        }
        binding_model = self.env["contact.center.channel.binding"].sudo()
        bindings = binding_model
        if exact_keys:
            aliases = (
                self.env["contact.center.channel.alias"]
                .sudo()
                .search(
                    [
                        ("account_id", "=", account.id),
                        ("namespace", "in", list({key[0] for key in exact_keys})),
                        (
                            "value_normalized",
                            "in",
                            list({key[1] for key in exact_keys}),
                        ),
                        ("channel_binding_id.active", "=", True),
                        ("channel_binding_id.merged_into_id", "=", False),
                    ]
                )
            )
            aliases = aliases.filtered(
                lambda alias: (alias.namespace, alias.value_normalized) in exact_keys
            )
            bindings |= aliases.mapped("channel_binding_id")
        if conversation_ref:
            bindings |= binding_model.search(
                [
                    ("account_id", "=", account.id),
                    ("conversation_ref", "=", conversation_ref),
                    ("active", "=", True),
                    ("merged_into_id", "=", False),
                ]
            )
        if len(bindings) > 1:
            raise IdentityConflictError(
                "conversation addresses resolve to multiple channels",
                conflict_values={
                    "account_id": account.id,
                    "identity_ids": [(6, 0, bindings.identity_id.ids)],
                    "address_evidence_json": [
                        address.to_dict() for address in addresses
                    ],
                },
            )
        return bindings

    def _bootstrap_from_me_group_channel(self, connection, event):
        """Create the group projection allowed for one external-device echo."""

        # Correlate before creating anything. A provider echo whose message ID
        # already belongs to a local outbound in another conversation must never
        # manufacture an alias/channel for the observed group address.
        globally_correlated = self._find_message_binding(connection, event.message)
        if globally_correlated:
            raise UnsupportedEventError(
                "a group from_me message requires an existing conversation"
            )
        if event.origin != "external_device":
            raise UnsupportedEventError(
                "only external-device group messages may create a conversation"
            )
        if not connection.account_id._contact_center_accepts_group_inbound():
            raise UnsupportedEventError(
                "group conversation bootstrap is disabled for this account"
            )
        group_addresses = self._group_conversation_addresses(event)
        if (
            len(group_addresses) != 1
            or group_addresses[0].confidence != "protocol"
            or group_addresses[0].value_normalized != event.conversation_ref
        ):
            raise ValidationError(
                _(
                    "A new group conversation requires one exact protocol "
                    "routing address."
                )
            )
        self._group_protocol_participant(event)
        if not connection.account_id.technical_author_id:
            raise UnsupportedEventError(
                "external-device messages require an account technical author"
            )
        return self._resolve_group_channel(connection.account_id, event)

    def _resolve_from_me_channel_binding(self, connection, event):
        """Resolve an echo conversation and bootstrap an eligible group once."""

        channel_binding = self._find_channel_binding(
            connection.account_id,
            event.conversation.addresses,
            conversation_ref=event.conversation_ref,
        )
        if channel_binding and (
            channel_binding.conversation_type != event.conversation.conversation_type
        ):
            raise ValidationError(
                _("The provider echo resolved to another conversation type.")
            )
        bootstrapped_group = False
        if event.conversation.conversation_type == "group" and not channel_binding:
            channel_binding = self._bootstrap_from_me_group_channel(connection, event)
            bootstrapped_group = True
        if event.conversation.conversation_type == "direct" and channel_binding:
            # A conversation initiated on the provider device must not remain a
            # numeric guest until the remote person eventually replies.
            channel_binding._request_identity_avatar_sync(connection)
        return channel_binding, bootstrapped_group

    @api.model
    def _lock_reconcilable_send_outboxes(self, message_bindings):
        """Lock sends that may be completed by exact positive provider evidence.

        A provider receipt or ``from_me`` echo can arrive while the dispatch worker
        is between its durable ``processing`` boundary and local finalization.  Both
        flows must lock the outbox before the message binding, matching the
        post-provider finalization order.  If the worker still owns the boundary,
        retry the inbox event instead of racing its result.

        ``pending`` and ``retry`` are included defensively: exact authenticated
        provider evidence makes a future dispatch unnecessary, so completing the
        command under this lock prevents a duplicate send.
        """

        message_bindings = message_bindings.sudo().exists()
        if not message_bindings:
            return self.env["contact.center.outbox.command"]
        outbox_model = self.env["contact.center.outbox.command"].sudo()
        outbox_model.flush_model(["message_binding_id", "command_type", "state"])
        self.env.cr.execute(
            """
            SELECT id
              FROM contact_center_outbox_command
             WHERE message_binding_id = ANY(%s)
               AND command_type = 'send_message'
               AND state IN ('pending', 'retry', 'processing', 'uncertain')
             ORDER BY id
             FOR UPDATE
            """,
            [message_bindings.ids],
        )
        outboxes = outbox_model.browse([row[0] for row in self.env.cr.fetchall()])
        outboxes.invalidate_recordset(
            [
                "state",
                "provider_response_json",
                "last_error_class",
                "last_error_message",
            ]
        )
        if outboxes.filtered(lambda command: command.state == "processing"):
            raise TransientAdapterError(
                "positive send evidence is waiting for dispatch finalization"
            )
        return outboxes.filtered(
            lambda command: command.state in ("pending", "retry", "uncertain")
        )

    @api.model
    def _complete_send_outboxes_from_positive_evidence(
        self, outboxes, event, evidence_source
    ):
        """Complete already locked sends without ever calling the provider."""

        if evidence_source not in (
            "delivery_receipt",
            "from_me_echo",
            "group_delivery_receipt",
        ):
            raise ValidationError(_("Unsupported outbound reconciliation evidence."))
        reconciled = self.env["contact.center.outbox.command"]
        reconciled_at = fields.Datetime.now()
        for outbox in outboxes.sudo().sorted("id"):
            if outbox.state not in ("pending", "retry", "uncertain"):
                continue
            evidence = dict(outbox.provider_response_json or {})
            evidence.update(
                {
                    "reconciled_by": evidence_source,
                    "provider_event_id": event.event_id,
                }
            )
            outbox.write(
                {
                    "state": "done",
                    "processed_at": reconciled_at,
                    "provider_response_json": evidence,
                    "last_error_class": False,
                    "last_error_message": False,
                }
            )
            reconciled |= outbox
        return reconciled

    def _reconcile_existing_from_me_message(self, connection, event, message_binding):
        """Merge one provider echo into its already projected outbound message."""

        reconcilable_outboxes = self._lock_reconcilable_send_outboxes(message_binding)

        forwarding_changed = message_binding._contact_center_merge_forwarding(
            event.message.is_forwarded,
            event.message.forwarding_score,
        )
        participant_changed = False
        previous_external_message_id = message_binding.external_message_id or ""
        observed_snapshot = dict(event.message.protocol_snapshot or {})
        if event.conversation.conversation_type == "group":
            persisted_reply = message_binding.reply_to_binding_id
            persisted_reply_external_id = persisted_reply.external_message_id or ""
            observed_reply_external_id = event.message.reply_to_external_id or ""
            if not persisted_reply and message_binding.origin == "external_device":
                # An imported human message may quote uncaptured history. Its
                # repeated observation must agree with that durable reference;
                # locally composed replies still require their real bound target.
                persisted_reference = (
                    message_binding.protocol_snapshot_json or {}
                ).get("reply_to")
                if isinstance(persisted_reference, dict):
                    persisted_reply_external_id = (
                        persisted_reference.get("external_message_id") or ""
                    )
                    if persisted_reply_external_id and not observed_reply_external_id:
                        # Echoes can omit quote context. Absence must not erase the
                        # reference needed to recognize a later complete replay.
                        observed_snapshot["reply_to"] = dict(persisted_reference)
            if observed_reply_external_id and (
                persisted_reply_external_id != observed_reply_external_id
            ):
                raise ValidationError(
                    _(
                        "The provider echo reply target conflicts with the "
                        "persisted outbound reply."
                    )
                )
            participant_changed = (
                message_binding._contact_center_set_protocol_participant(
                    self._group_echo_protocol_participant(
                        connection, message_binding, event
                    )
                )
            )
        self._enrich_channel_aliases(
            message_binding.channel_binding_id, event.conversation.addresses
        )
        snapshot = dict(message_binding.protocol_snapshot_json or {})
        snapshot.update(observed_snapshot)
        if snapshot != (message_binding.protocol_snapshot_json or {}):
            message_binding.sudo().write({"protocol_snapshot_json": snapshot})
        message_binding._contact_center_apply_delivery(
            "sent",
            occurred_at=self._event_datetime(event),
            external_event_id=event.event_id,
            external_message_id=event.message.external_message_id,
            details={"source": "from_me_echo"},
        )
        reconciled_outboxes = self._complete_send_outboxes_from_positive_evidence(
            reconcilable_outboxes,
            event,
            "from_me_echo",
        )
        self._notify_ui(
            message_binding.channel_binding_id.channel_id,
            "delivery_updated",
            {
                "message_id": message_binding.message_id.id,
                "state": message_binding.delivery_state,
                "dispatch_state": "done" if reconciled_outboxes else False,
            },
        )
        if forwarding_changed or (
            event.conversation.conversation_type == "group"
            and (
                participant_changed
                or previous_external_message_id
                != (message_binding.external_message_id or "")
            )
        ):
            self._notify_ui(
                message_binding.channel_binding_id.channel_id,
                "message_updated",
                {"message_id": message_binding.message_id.id},
            )
        return message_binding.message_id

    def _reconcile_from_me(self, connection, event, inbox_event=None):
        self._lock_inbound_account_scope(connection.account_id)
        channel_binding, bootstrapped_group = self._resolve_from_me_channel_binding(
            connection, event
        )
        message_binding = self._find_message_binding(
            connection, event.message, channel_binding=channel_binding
        )
        if message_binding:
            return self._reconcile_existing_from_me_message(
                connection, event, message_binding
            )

        technical_author = connection.account_id.technical_author_id
        if not technical_author:
            raise UnsupportedEventError(
                "external-device messages require an account technical author"
            )
        if not channel_binding:
            # For a direct ``from_me`` message, the actor is our own WhatsApp
            # account.  The remote person is identified by the conversation
            # addresses, so never project the actor PushName onto that identity.
            remote_actor = ActorDTO(addresses=event.conversation.addresses)
            identity = self._resolve_identity(
                connection.account_id,
                remote_actor,
                inbox_event=inbox_event,
            )
            channel_binding = self._resolve_channel(
                connection.account_id, identity, event
            )
            channel_binding._request_identity_avatar_sync(connection)
        self._lock_inbound_projection_binding(
            channel_binding,
            connection.account_id,
        )
        self._enrich_channel_aliases(channel_binding, event.conversation.addresses)
        if bootstrapped_group:
            profile = self.env["contact.center.group.profile"]._get_or_create(
                channel_binding, connection
            )
            # Metadata is enrichment, not a prerequisite for persisting the
            # authenticated human message.  This schedules provider I/O on the
            # dedicated queue and keeps media processing independent.
            profile._request_sync(connection)
        existing = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("channel_binding_id", "=", channel_binding.id),
                    (
                        "external_message_id",
                        "=",
                        event.message.external_message_id,
                    ),
                ],
                limit=1,
            )
        )
        if existing:
            if existing._contact_center_merge_forwarding(
                event.message.is_forwarded,
                event.message.forwarding_score,
            ):
                self._notify_ui(
                    existing.channel_binding_id.channel_id,
                    "message_updated",
                    {"message_id": existing.message_id.id},
                )
            return existing.message_id
        reply_binding = self._event_reply_binding(
            channel_binding, event, inbox_event=inbox_event
        )
        message = channel_binding.channel_id.sudo()._contact_center_post(
            origin="external_device",
            body=html_escape(event.message.text or ""),
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            date=self._event_datetime(event),
            author_id=technical_author.id,
            partner_ids=[],
            parent_id=reply_binding.message_id.id if reply_binding else False,
        )
        message_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": channel_binding.id,
                    "provider_connection_id": connection.id,
                    "source_inbox_event_id": (inbox_event.id if inbox_event else False),
                    "direction": "outbound",
                    "origin": "external_device",
                    "content_type": event.message.content_type,
                    "external_message_id": event.message.external_message_id,
                    "client_message_id": event.message.client_message_id,
                    "is_forwarded": event.message.is_forwarded,
                    "forwarding_score": event.message.forwarding_score or 0,
                    "forwarding_score_observed": (
                        event.message.forwarding_score is not None
                    ),
                    "protocol_snapshot_json": event.message.protocol_snapshot,
                    "structured_content_json": event.message.structured_content,
                    "protocol_participant_json": {},
                    "reply_to_binding_id": reply_binding.id if reply_binding else False,
                    "delivery_state": "queued",
                }
            )
        )
        if event.conversation.conversation_type == "group":
            message_binding._contact_center_set_protocol_participant(
                self._group_echo_protocol_participant(
                    connection, message_binding, event
                )
            )
        message_binding._contact_center_apply_delivery(
            "sent",
            occurred_at=self._event_datetime(event),
            external_event_id=event.event_id,
            details={
                "source": "external_device",
                **(
                    {"group_conversation_bootstrap": True} if bootstrapped_group else {}
                ),
            },
        )
        if event.message.media:
            self._create_media_bindings(message_binding, event.message.media)
        self._publish_message_created(
            channel_binding.channel_id, message, direction="outbound"
        )
        return message

    def _apply_delivery_event(self, connection, event):
        if event.direction != "outbound" or event.is_from_me:
            raise UnsupportedEventError(
                "self-side direct read evidence is not outbound delivery"
            )
        state = (event.delivery or {}).get("state")
        if state not in ("sent", "delivered", "read"):
            raise UnsupportedEventError(
                "unsupported provider delivery state: %s" % (state or "missing")
            )
        raw_ids = (event.delivery or {}).get("external_message_ids") or []
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        external_message_ids = tuple(
            dict.fromkeys(
                value for value in raw_ids if isinstance(value, str) and value
            )
        )
        has_watermark = (event.delivery or {}).get("watermark") is not None
        if not external_message_ids:
            return self._apply_delivery_watermark_event(
                connection,
                event,
                state,
            )
        conversation_binding = self._find_channel_binding(
            connection.account_id,
            event.conversation.addresses,
            conversation_ref=event.conversation_ref,
        )
        binding_domain = [
            ("provider_connection_id", "=", connection.id),
            ("direction", "=", "outbound"),
            "|",
            ("external_message_id", "in", list(external_message_ids)),
            ("client_message_id", "in", list(external_message_ids)),
        ]
        if conversation_binding:
            binding_domain.insert(
                1, ("channel_binding_id", "=", conversation_binding.id)
            )
        bindings = (
            self.env["contact.center.message.binding"].sudo().search(binding_domain)
        )
        if not conversation_binding and bindings:
            candidate_channels = bindings.mapped("channel_binding_id")
            if len(candidate_channels) > 1:
                raise ValidationError(
                    _(
                        "A cumulative provider receipt resolves to multiple "
                        "conversations."
                    )
                )
            conversation_binding = candidate_channels
        binding_by_external_id = {}
        for external_message_id in external_message_ids:
            candidates = bindings.filtered(
                lambda binding, value=external_message_id: value
                in (binding.external_message_id, binding.client_message_id)
            )
            if len(candidates) > 1:
                raise ValidationError(
                    _(
                        "A provider receipt resolves to multiple local messages: %s",
                        external_message_id,
                    )
                )
            if candidates:
                binding_by_external_id[external_message_id] = candidates
        missing_ids = [
            value
            for value in external_message_ids
            if value not in binding_by_external_id
        ]
        if missing_ids and not has_watermark:
            error = TransientAdapterError(
                "delivery receipt arrived before message correlation: %s"
                % ", ".join(missing_ids[:5])
            )
            raise error

        # Lock every correlated send before any message binding.  A receipt may
        # have been emitted while the dispatch worker was finalizing the same
        # rows; retry that receipt if the durable provider boundary is still owned.
        reconcilable_outboxes = self._lock_reconcilable_send_outboxes(bindings)

        external_event_id = (event.delivery or {}).get(
            "external_event_id"
        ) or event.event_id
        # Applying delivery takes a row lock per binding.  Provider payload order is
        # not stable, so use one canonical order across concurrent receipt batches.
        result = self.env["contact.center.message.binding"]
        for external_message_id, binding in sorted(
            binding_by_external_id.items(),
            key=lambda item: (item[1].id, item[0]),
        ):
            self._enrich_channel_aliases(
                binding.channel_binding_id, event.conversation.addresses
            )
            binding._contact_center_apply_delivery(
                state,
                occurred_at=self._event_datetime(event),
                external_event_id=external_event_id,
                external_message_id=(
                    external_message_id if not binding.external_message_id else None
                ),
                details=event.delivery,
            )
            result |= binding
        reconciled_outboxes = self._complete_send_outboxes_from_positive_evidence(
            reconcilable_outboxes,
            event,
            "delivery_receipt",
        )
        reconciled_binding_ids = set(
            reconciled_outboxes.mapped("message_binding_id").ids
        )
        for binding in result:
            self._notify_ui(
                binding.channel_binding_id.channel_id,
                "delivery_updated",
                {
                    "message_id": binding.message_id.id,
                    "state": binding.delivery_state,
                    "dispatch_state": (
                        "done" if binding.id in reconciled_binding_ids else False
                    ),
                },
            )
        if has_watermark:
            result |= self._apply_delivery_watermark_event(
                connection,
                event,
                state,
                conversation_binding=conversation_binding,
            )
        return result

    def _delivery_watermark_datetime(self, event):
        """Return a provider receipt watermark as a naive UTC datetime."""

        watermark = (event.delivery or {}).get("watermark")
        if watermark is None:
            raise ValidationError(
                _(
                    "Delivery events require provider message IDs or an epoch "
                    "millisecond watermark."
                )
            )
        if isinstance(watermark, bool) or not isinstance(watermark, int):
            raise ValidationError(
                _("A delivery watermark must be an epoch millisecond integer.")
            )
        try:
            watermark_at = datetime.datetime.fromtimestamp(
                watermark / 1000,
                tz=datetime.timezone.utc,
            )
        except (OverflowError, OSError, ValueError) as error:
            raise ValidationError(
                _("The delivery watermark is outside the supported date range.")
            ) from error
        return watermark_at.replace(tzinfo=None)

    def _apply_delivery_watermark_event(
        self, connection, event, state, conversation_binding=None
    ):
        """Persist and apply cumulative direct-message evidence within its inbox."""

        watermark_at = self._delivery_watermark_datetime(event)
        conversation_binding = conversation_binding or self._find_channel_binding(
            connection.account_id,
            event.conversation.addresses,
            conversation_ref=event.conversation_ref,
        )
        if not conversation_binding:
            raise TransientAdapterError(
                "watermark receipt arrived before conversation correlation"
            )
        if conversation_binding.conversation_type != "direct":
            raise ValidationError(
                _("A direct receipt resolved to a non-direct conversation.")
            )

        external_event_id = (event.delivery or {}).get(
            "external_event_id"
        ) or event.event_id
        watermark_model = self.env["contact.center.delivery.watermark"].sudo()
        watermark_model._record(
            connection=connection,
            channel_binding=conversation_binding,
            state=state,
            watermark_at=watermark_at,
            occurred_at=self._event_datetime(event),
            external_event_id=external_event_id,
            details=event.delivery,
        )

        bindings = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("provider_connection_id", "=", connection.id),
                    ("channel_binding_id", "=", conversation_binding.id),
                    ("direction", "=", "outbound"),
                    ("external_message_id", "!=", False),
                    ("external_message_id", "!=", ""),
                    ("message_id.date", "<=", watermark_at),
                ],
                order="id",
            )
        )
        for binding in bindings:
            self._enrich_channel_aliases(
                binding.channel_binding_id, event.conversation.addresses
            )
            binding.with_context(
                contact_center_skip_watermark_replay=True
            )._contact_center_apply_delivery(
                state,
                occurred_at=self._event_datetime(event),
                external_event_id=external_event_id,
                details=event.delivery,
            )
            self._notify_ui(
                binding.channel_binding_id.channel_id,
                "delivery_updated",
                {"message_id": binding.message_id.id, "state": binding.delivery_state},
            )
        return bindings

    def _group_delivery_request(self, event):
        """Validate and normalize the participant receipt wire contract."""

        state = (event.delivery or {}).get("state")
        if state not in ("delivered", "read"):
            raise UnsupportedEventError(
                "unsupported group delivery state: %s" % (state or "missing")
            )
        if event.direction != "outbound" or event.is_from_me:
            raise UnsupportedEventError(
                "self-side group read evidence is not a participant receipt"
            )
        actor_address = self._group_protocol_participant(event)
        raw_ids = (event.delivery or {}).get("external_message_ids") or []
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        elif not isinstance(raw_ids, (list, tuple)):
            raise ValidationError(
                _("Group delivery provider message IDs must be a string or a list.")
            )
        external_message_ids = tuple(
            dict.fromkeys(
                value.strip()
                for value in raw_ids
                if isinstance(value, str) and value.strip()
            )
        )
        if not external_message_ids:
            raise ValidationError(
                _("Group delivery events require provider message IDs.")
            )
        return state, actor_address, external_message_ids

    def _group_delivery_binding(self, connection, event):
        """Resolve the already-projected group conversation for one receipt."""

        binding = self._find_channel_binding(
            connection.account_id,
            event.conversation.addresses,
            conversation_ref=event.conversation_ref,
        )
        if not binding:
            raise TransientAdapterError(
                "group receipt arrived before conversation correlation"
            )
        if binding.conversation_type != "group":
            raise ValidationError(
                _("A group receipt resolved to a non-group conversation.")
            )
        binding.ensure_one()
        return binding

    def _lock_group_delivery_scope(self, connection, binding):
        """Lock and revalidate the complete parent scope of a group receipt."""

        connection.ensure_one()
        binding.ensure_one()
        # Keep the same parent-first order as outbound finalization:
        # account -> provider connection -> channel -> channel binding -> group
        # profile -> outbox -> message binding.
        # A successful group send finalizer takes the connection FOR UPDATE before
        # writing its outbox.  Taking the connection only inside _record_receipt,
        # after the outbox, would create an ABBA deadlock under a fast receipt.
        expected_account_id = connection.account_id.id
        expected_connection_id = connection.id
        expected_channel_id = binding.channel_id.id
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id = %s FOR KEY SHARE",
            [expected_account_id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("The group receipt account no longer exists."))
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection "
            "WHERE id = %s AND account_id = %s FOR SHARE",
            [expected_connection_id, expected_account_id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(
                _("The group receipt provider connection is no longer valid.")
            )
        connection.invalidate_recordset(["account_id"])
        if connection.account_id.id != expected_account_id:
            raise ValidationError(
                _("The group receipt provider connection changed account.")
            )
        # Alias creation stores a FK to the group profile. Acquire the parent
        # channel first so a first-seen participant receipt cannot invert the
        # channel -> group-profile order used by metadata synchronization.
        self.env.cr.execute(
            "SELECT id FROM mail_channel WHERE id = %s FOR SHARE",
            [expected_channel_id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("The group receipt conversation no longer exists."))
        self.env.cr.execute(
            "SELECT id FROM contact_center_channel_binding " "WHERE id = %s FOR SHARE",
            [binding.id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("The group receipt conversation no longer exists."))
        binding.invalidate_recordset(
            [
                "active",
                "merged_into_id",
                "conversation_type",
                "identity_id",
                "account_id",
                "channel_id",
            ]
        )
        if (
            not binding.active
            or binding.merged_into_id
            or binding.conversation_type != "group"
            or binding.identity_id
            or binding.account_id.id != expected_account_id
            or binding.channel_id.id != expected_channel_id
        ):
            raise ValidationError(
                _("The group receipt conversation is no longer active.")
            )
        profile = self._group_profile_for_connection(binding, connection)
        self.env.cr.execute(
            "SELECT id FROM contact_center_group_profile "
            "WHERE id = %s AND channel_binding_id = %s "
            "AND provider_connection_id = %s FOR SHARE",
            [profile.id, binding.id, expected_connection_id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("The group receipt profile is no longer valid."))
        profile.invalidate_recordset(["channel_binding_id", "provider_connection_id"])
        if (
            profile.channel_binding_id != binding
            or profile.provider_connection_id != connection
        ):
            raise ValidationError(_("The group receipt profile is no longer valid."))
        return profile

    def _group_delivery_participant(self, profile, event):
        """Resolve and safely enrich the roster participant under its parent lock."""

        participant = profile._resolve_protocol_participant(
            event.actor.addresses,
            active_only=False,
            enrich=True,
        )
        if not participant:
            raise TransientAdapterError(
                "group participant aliases are waiting for roster correlation"
            )
        return participant

    def _group_delivery_candidates(
        self, binding, connection, external_message_ids, inbox_event
    ):
        """Correlate the requested IDs to one outbound message each."""

        candidates = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("channel_binding_id", "=", binding.id),
                    ("provider_connection_id", "=", connection.id),
                    "|",
                    ("external_message_id", "in", list(external_message_ids)),
                    ("client_message_id", "in", list(external_message_ids)),
                ]
            )
        )
        by_external_id = {}
        for external_message_id in external_message_ids:
            matches = candidates.filtered(
                lambda candidate, value=external_message_id: value
                in (candidate.external_message_id, candidate.client_message_id)
            )
            if len(matches) > 1:
                raise ValidationError(
                    _(
                        "A group receipt resolves to multiple messages: %s",
                        external_message_id,
                    )
                )
            if matches:
                by_external_id[external_message_id] = matches
        missing_ids = [
            value for value in external_message_ids if value not in by_external_id
        ]
        self._handle_group_receipt_correlation_gaps(
            missing_ids,
            external_message_ids,
            inbox_event,
        )
        invalid = [
            value
            for value, candidate in by_external_id.items()
            if candidate.direction != "outbound"
        ]
        if invalid:
            raise UnsupportedEventError(
                "group participant receipts can target only outbound messages"
            )
        return candidates, by_external_id

    def _record_group_delivery_receipts(
        self,
        *,
        by_external_id,
        profile,
        participant,
        state,
        event,
        inbox_event,
        actor_address,
    ):
        """Record a sorted batch while preserving aggregate send semantics."""

        receipt_model = self.env["contact.center.group.delivery.event"].sudo()
        occurred_at = self._event_datetime(event)
        external_event_id = (event.delivery or {}).get(
            "external_event_id"
        ) or event.event_id
        result = self.env["contact.center.message.binding"]
        # Both the dispatch proof and participant ledger lock the message binding.
        # Sorting prevents inverse lock acquisition across overlapping batches.
        for external_message_id, message_binding in sorted(
            by_external_id.items(),
            key=lambda item: (item[1].id, item[0]),
        ):
            if message_binding.delivery_state in ("queued", "failed"):
                message_binding._contact_center_apply_delivery(
                    "sent",
                    occurred_at=occurred_at,
                    external_event_id="%s:group-dispatch-proof" % external_event_id,
                    external_message_id=(
                        external_message_id
                        if not message_binding.external_message_id
                        else None
                    ),
                    details={"source": "group_participant_receipt"},
                )
            receipt_model._record_receipt(
                message_binding=message_binding,
                group_profile=profile,
                participant=participant,
                state=state,
                occurred_at=occurred_at,
                external_event_id=external_event_id,
                inbox_event=inbox_event,
                protocol_address=actor_address,
            )
            result |= message_binding
        return result

    def _notify_group_delivery_receipts(
        self, binding, message_bindings, reconciled_outboxes
    ):
        """Publish one refresh per affected message after durable reconciliation."""

        reconciled_binding_ids = set(
            reconciled_outboxes.mapped("message_binding_id").ids
        )
        for message_binding in message_bindings:
            self._notify_ui(
                binding.channel_id,
                "delivery_updated",
                {
                    "message_id": message_binding.message_id.id,
                    "dispatch_state": (
                        "done"
                        if message_binding.id in reconciled_binding_ids
                        else False
                    ),
                    "refresh": True,
                },
            )

    def _apply_group_delivery_event(self, connection, event, inbox_event=None):
        """Persist participant receipts without promoting the whole group state."""

        state, actor_address, external_message_ids = self._group_delivery_request(event)
        binding = self._group_delivery_binding(connection, event)
        profile = self._lock_group_delivery_scope(connection, binding)
        participant = self._group_delivery_participant(profile, event)
        candidates, by_external_id = self._group_delivery_candidates(
            binding,
            connection,
            external_message_ids,
            inbox_event,
        )

        # Participant receipts are also exact proof that the provider accepted
        # the group send. Fence the outbox before any target message row, just as
        # for direct receipts, so a concurrent finalizer cannot leave it ambiguous.
        reconcilable_outboxes = self._lock_reconcilable_send_outboxes(candidates)
        result = self._record_group_delivery_receipts(
            by_external_id=by_external_id,
            profile=profile,
            participant=participant,
            state=state,
            event=event,
            inbox_event=inbox_event,
            actor_address=actor_address,
        )
        reconciled_outboxes = self._complete_send_outboxes_from_positive_evidence(
            reconcilable_outboxes,
            event,
            "group_delivery_receipt",
        )
        self._notify_group_delivery_receipts(binding, result, reconciled_outboxes)
        return result

    def _handle_group_receipt_correlation_gaps(
        self,
        missing_ids,
        external_message_ids,
        inbox_event,
    ):
        """Reject wholly orphaned receipts and annotate partially matched ones."""

        if missing_ids and len(missing_ids) == len(external_message_ids):
            raise TransientAdapterError(
                "group receipt arrived before message correlation: %s"
                % ", ".join(missing_ids[:5])
            )
        if missing_ids and inbox_event:
            metadata = dict(inbox_event.metadata_json or {})
            metadata.update(
                {
                    "group_receipt_partial": True,
                    "group_receipt_uncorrelated_count": len(missing_ids),
                }
            )
            inbox_event.sudo().write({"metadata_json": metadata})

    def _matching_outbound_mutation_echo(self, connection, target, event):
        """Reuse the clean local edit matched by an exact self-side wire echo."""

        event_mutation = event.mutation or {}
        if not event.is_from_me or event_mutation.get("type") != "edit":
            return self.env["contact.center.message.mutation"]
        candidates = (
            self.env["contact.center.message.mutation"]
            .sudo()
            .search(
                [
                    ("target_message_binding_id", "=", target.id),
                    ("provider_connection_id", "=", connection.id),
                    ("mutation_type", "=", "edit"),
                    ("direction", "=", "outbound"),
                    ("external_event_id", "=", False),
                ],
                order="occurred_at desc, id desc",
                limit=50,
            )
        )
        if not candidates:
            return candidates
        outboxes = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search(
                [
                    ("mutation_id", "in", candidates.ids),
                    ("command_type", "=", "edit_message"),
                    ("state", "in", ("processing", "done", "uncertain")),
                ],
                order="id desc",
            )
        )
        outbox_by_mutation = {}
        for outbox in outboxes:
            outbox_by_mutation.setdefault(outbox.mutation_id.id, outbox)
        adapter = connection.get_adapter()
        event_time = self._event_datetime(event)
        for candidate in candidates:
            outbox = outbox_by_mutation.get(candidate.id)
            if not outbox:
                continue
            dispatch_time = (
                outbox.dispatch_started_at or outbox.processed_at or outbox.create_date
            )
            if (
                dispatch_time
                and abs((event_time - dispatch_time).total_seconds()) > 1800
            ):
                continue
            try:
                command = CommandDTO.from_dict(outbox.command_json or {})
            except DTOValidationError:
                continue
            if adapter.matches_outbound_mutation_echo(
                connection, command, event_mutation
            ):
                return self._confirm_outbound_mutation_echo(candidate, outbox, event)
        return self.env["contact.center.message.mutation"]

    def _confirm_outbound_mutation_echo(self, mutation, outbox, event):
        """Persist exact provider proof without copying provider-formatted text."""

        self.env.cr.execute(
            "SELECT id FROM contact_center_outbox_command WHERE id = %s FOR UPDATE",
            [outbox.id],
        )
        outbox.invalidate_recordset(
            [
                "state",
                "provider_response_json",
                "last_error_class",
                "last_error_message",
            ]
        )
        if outbox.state == "processing":
            # The provider echo is positive evidence, but the dispatch worker still
            # owns the outcome. Retry the inbox after it durably reaches done or
            # uncertain so an ambiguous worker result cannot overwrite this proof.
            raise TransientAdapterError(
                "outbound edit echo is waiting for dispatch finalization"
            )
        self.env.cr.execute(
            "SELECT id FROM contact_center_message_binding WHERE id = %s FOR UPDATE",
            [mutation.target_message_binding_id.id],
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_message_mutation WHERE id = %s FOR UPDATE",
            [mutation.id],
        )
        mutation.invalidate_recordset(["external_event_id", "state", "details_json"])
        if not mutation.external_event_id:
            mutation.write({"external_event_id": event.event_id})
        mutation._apply_projection()

        if outbox.state == "uncertain":
            evidence = dict(outbox.provider_response_json or {})
            evidence.update(
                {
                    "reconciled_by": "from_me_mutation_echo",
                    "provider_event_id": event.event_id,
                }
            )
            outbox.write(
                {
                    "state": "done",
                    "processed_at": fields.Datetime.now(),
                    "provider_response_json": evidence,
                    "last_error_class": False,
                    "last_error_message": False,
                }
            )
            outbox._notify_delivery_ui()
        return mutation

    def _apply_inbound_mutation(self, connection, event, inbox_event=None):
        mutation = event.mutation or {}
        expected_direction = "outbound" if event.is_from_me else "inbound"
        if event.direction != expected_direction:
            raise ValidationError(
                _("The mutation direction does not match its from_me flag.")
            )
        target_external_id = mutation.get("target_external_message_id")
        conversation_binding = self._find_channel_binding(
            connection.account_id,
            event.conversation.addresses,
            conversation_ref=event.conversation_ref,
        )
        target_domain = [
            ("provider_connection_id", "=", connection.id),
            "|",
            ("external_message_id", "=", target_external_id),
            ("client_message_id", "=", target_external_id),
        ]
        if conversation_binding:
            target_domain.insert(
                1, ("channel_binding_id", "=", conversation_binding.id)
            )
        targets = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(target_domain, limit=2)
        )
        if not targets:
            error = TransientAdapterError(
                "message mutation arrived before its target: %s" % target_external_id
            )
            raise error
        if len(targets) > 1:
            raise ValidationError(
                _(
                    "A provider mutation resolves to multiple local messages: %s",
                    target_external_id,
                )
            )
        target = targets
        if conversation_binding and conversation_binding != target.channel_binding_id:
            raise ValidationError(
                _("The mutation target belongs to another conversation.")
            )
        target_conversation_type = target.channel_binding_id.conversation_type
        if event.conversation.conversation_type != target_conversation_type:
            raise ValidationError(
                _("The mutation conversation type does not match its target.")
            )
        if target_conversation_type == "group":
            if not conversation_binding:
                raise TransientAdapterError(
                    "group mutation arrived before conversation correlation"
                )
            self._validate_group_inbound_mutation(
                connection, event, target, inbox_event=inbox_event
            )
        self._enrich_channel_aliases(
            target.channel_binding_id, event.conversation.addresses
        )
        mutation_model = self.env["contact.center.message.mutation"].sudo()
        existing = mutation_model.search(
            [
                ("provider_connection_id", "=", connection.id),
                ("external_event_id", "=", event.event_id),
            ],
            limit=1,
        )
        if existing:
            existing._apply_projection()
            return target.message_id

        if self._matching_outbound_mutation_echo(connection, target, event):
            return target.message_id

        actor_partner = self.env["res.partner"]
        actor_guest = self.env["mail.guest"]
        if event.is_from_me:
            actor_partner = connection.account_id.technical_author_id
            if mutation["type"] == "react" and not actor_partner:
                raise UnsupportedEventError(
                    "self mutations require a persisted technical author"
                )
        else:
            identity = self._resolve_identity(
                connection.account_id,
                event.actor,
                inbox_event=inbox_event,
                observed_name_at=self._event_datetime(event),
            )
            actor_guest = identity.mail_guest_id

        values = {
            "target_message_binding_id": target.id,
            "provider_connection_id": connection.id,
            "external_event_id": event.event_id,
            "mutation_type": mutation["type"],
            "direction": event.direction,
            "actor_partner_id": actor_partner.id if actor_partner else False,
            "actor_guest_id": actor_guest.id if actor_guest else False,
            "reaction_emoji": (
                mutation.get("emoji") or False if mutation["type"] == "react" else False
            ),
            "reaction_operation": (
                mutation.get("operation") or "add"
                if mutation["type"] == "react"
                else False
            ),
            "new_text": (
                mutation.get("new_text") or False
                if mutation["type"] == "edit"
                else False
            ),
            "occurred_at": self._event_datetime(event),
            "details_json": {
                key: value
                for key, value in mutation.items()
                if key not in ("new_text",)
            },
        }
        record = mutation_model.create(values)
        record._apply_projection()
        return target.message_id

    def _resolve_identity(
        self, account, actor, inbox_event=None, observed_name_at=None
    ):
        addresses = tuple(actor.addresses)
        if not addresses:
            raise ValidationError(
                _("Inbound remote actors require at least one address.")
            )
        exact_keys = {
            (address.namespace, address.value_normalized) for address in addresses
        }
        portable_namespaces = self._company_portable_identity_namespaces()
        invalid_portable = sorted(
            {
                address.namespace
                for address in addresses
                if address.resolution_scope == "company"
                and (
                    address.confidence != "protocol"
                    or address.namespace not in portable_namespaces
                )
            }
        )
        if invalid_portable:
            raise ValidationError(
                _(
                    "These address namespaces cannot resolve an identity across "
                    "inboxes: %s",
                    ", ".join(invalid_portable),
                )
            )
        portable_keys = {
            (address.namespace, address.value_normalized)
            for address in addresses
            if address.resolution_scope == "company"
            and address.namespace in portable_namespaces
        }
        # A portable protocol identifier is a company-wide person key. Serialize
        # concurrent first observations from different inboxes before searching,
        # otherwise both transactions could create a guest for the same person.
        for namespace, value_normalized in sorted(portable_keys):
            lock_key = "contact_center:identity:%s:%s:%s" % (
                account.company_id.id,
                namespace,
                value_normalized,
            )
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [lock_key]
            )

        alias_model = self.env["contact.center.identity.alias"].sudo()
        aliases = alias_model.search(
            [
                ("account_id", "=", account.id),
                ("namespace", "in", sorted({key[0] for key in exact_keys})),
                ("value_normalized", "in", sorted({key[1] for key in exact_keys})),
            ]
        ).filtered(
            lambda alias: (alias.namespace, alias.value_normalized) in exact_keys
        )
        if portable_keys:
            portable_aliases = alias_model.search(
                [
                    ("company_id", "=", account.company_id.id),
                    ("confidence", "in", ("protocol", "manual")),
                    ("namespace", "in", sorted({key[0] for key in portable_keys})),
                    (
                        "value_normalized",
                        "in",
                        sorted({key[1] for key in portable_keys}),
                    ),
                ]
            ).filtered(
                lambda alias: (alias.namespace, alias.value_normalized) in portable_keys
            )
            aliases |= portable_aliases
        identities = aliases.mapped("identity_id")
        if identities:
            # Identity consolidation also rewires aliases, bindings and active guest
            # membership.  Lock every candidate before deciding so an LID-only event
            # cannot recreate a retired direct binding while a PN bridge is merging it.
            self.env.cr.execute(
                "SELECT id FROM contact_center_identity WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                [tuple(identities.ids)],
            )
            identities.invalidate_recordset(["state", "merged_into_id"])
            canonical_identities = self.env["contact.center.identity"]
            for identity in identities:
                visited = set()
                while (
                    identity.state == "merged"
                    and identity.merged_into_id
                    and identity.id not in visited
                ):
                    visited.add(identity.id)
                    identity = identity.merged_into_id
                canonical_identities |= identity
            identities = canonical_identities
        if len(identities) > 1 and portable_keys:
            survivor, blocker = (
                self.env["contact.center.identity"]
                .sudo()
                ._contact_center_merge_portable_component(identities)
            )
            if not blocker:
                identities = survivor
        if len(identities) > 1:
            raise IdentityConflictError(
                "observed addresses resolve to different identities",
                conflict_values={
                    "account_id": account.id,
                    "identity_ids": [(6, 0, identities.ids)],
                    "address_evidence_json": [
                        address.to_dict() for address in addresses
                    ],
                    "inbox_event_id": inbox_event.id if inbox_event else False,
                },
            )

        if identities:
            identity = identities[0]
        else:
            self._advance_inbound_projection_revision(account)
            identity_model = self.env["contact.center.identity"]
            observed_name = identity_model._contact_center_clean_observed_name(
                actor.display_name
            )
            address_values = {
                value.strip().casefold()
                for address in addresses
                for value in (address.value, address.value_normalized)
                if isinstance(value, str) and value.strip()
            }
            if observed_name.casefold() in address_values:
                observed_name = ""
            fallback_name = (
                observed_name
                or identity_model._contact_center_fallback_name_from_addresses(
                    addresses
                )
            )
            guest = self.env["mail.guest"].sudo().create({"name": fallback_name})
            identity = identity_model._contact_center_create_managed(
                {
                    "name": fallback_name,
                    "company_id": account.company_id.id,
                    "mail_guest_id": guest.id,
                }
            )
        self._enrich_identity_aliases(account, identity, addresses)
        if observed_name_at is not None or inbox_event:
            identity._contact_center_observe_name(
                actor.display_name,
                observed_name_at,
                inbox_event,
            )
        identity._contact_center_sync_fallback_name_from_addresses(addresses)
        return identity

    def _enrich_identity_aliases(self, account, identity, addresses):
        alias_model = self.env["contact.center.identity.alias"].sudo()
        observed_at = fields.Datetime.to_datetime(fields.Datetime.now())
        for address in addresses:
            alias = alias_model.search(
                [
                    ("account_id", "=", account.id),
                    ("namespace", "=", address.namespace),
                    ("value_normalized", "=", address.value_normalized),
                ],
                limit=1,
            )
            if alias:
                if alias.identity_id != identity:
                    raise IdentityConflictError(
                        "an observed alias belongs to another identity",
                        conflict_values={
                            "account_id": account.id,
                            "identity_ids": [
                                (6, 0, (alias.identity_id | identity).ids)
                            ],
                            "address_evidence_json": [address.to_dict()],
                        },
                    )
                updates = self._alias_observation_updates(
                    alias,
                    address,
                    observed_at,
                    promote_resolution_scope=True,
                )
                if updates:
                    alias.write(updates)
                continue
            self._advance_inbound_projection_revision(account)
            role = (
                address.role
                if address.role in dict(alias_model._fields["role"].selection)
                else "primary"
            )
            confidence = (
                address.confidence
                if address.confidence
                in dict(alias_model._fields["confidence"].selection)
                else "observed"
            )
            alias_model.create(
                {
                    "identity_id": identity.id,
                    "account_id": account.id,
                    "namespace": address.namespace,
                    "value_raw": address.value,
                    "value_normalized": address.value_normalized,
                    "role": role,
                    "source_field": address.source_field,
                    "confidence": confidence,
                    "resolution_scope": address.resolution_scope,
                }
            )

    def _resolve_channel(self, account, identity, event):
        binding_model = self.env["contact.center.channel.binding"].sudo()
        binding = binding_model.search(
            [
                ("account_id", "=", account.id),
                ("identity_id", "=", identity.id),
                ("conversation_type", "=", "direct"),
                ("merged_into_id", "=", False),
                ("active", "=", True),
            ],
            limit=1,
        )
        if binding:
            return binding

        self._advance_inbound_projection_revision(account)
        team = account.default_team_id
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=account,
            identity=identity,
            conversation_type="direct",
            team=team,
            guest_ids=[identity.mail_guest_id.id],
        )
        binding = binding_model.create(
            {
                "channel_id": channel.id,
                "account_id": account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": event.conversation_ref,
            }
        )
        return binding

    def _resolve_group_channel(self, account, event):
        group_addresses = self._group_conversation_addresses(event)
        try:
            binding = self._find_channel_binding(
                account,
                group_addresses,
                conversation_ref=event.conversation_ref,
            )
        except IdentityConflictError as error:
            raise ValidationError(
                _("Group routing resolves to multiple conversations.")
            ) from error
        if binding:
            if binding.conversation_type != "group" or binding.identity_id:
                raise ValidationError(
                    _("A group address resolves to a non-group conversation.")
                )
            return binding

        self._advance_inbound_projection_revision(account)
        group_name = event.extensions.get("conversation_name") or event.extensions.get(
            "group_name"
        )
        if not isinstance(group_name, str) or not group_name.strip():
            group_name = event.conversation_ref
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=account,
            conversation_type="group",
            name=group_name.strip()[:255],
            team=account.default_team_id,
        )
        binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": channel.id,
                    "account_id": account.id,
                    "identity_id": False,
                    "conversation_type": "group",
                    "conversation_ref": event.conversation_ref,
                }
            )
        )
        return binding

    def _enrich_channel_aliases(self, binding, addresses):
        alias_model = self.env["contact.center.channel.alias"].sudo()
        observed_at = fields.Datetime.to_datetime(fields.Datetime.now())
        for address in addresses:
            alias = alias_model.search(
                [
                    ("account_id", "=", binding.account_id.id),
                    ("namespace", "=", address.namespace),
                    ("value_normalized", "=", address.value_normalized),
                ],
                limit=1,
            )
            if alias:
                if alias.channel_binding_id != binding:
                    identities = (
                        alias.channel_binding_id.identity_id | binding.identity_id
                    )
                    evidence = address.to_dict()
                    evidence.update(
                        {
                            "conflict_type": "channel_alias",
                            "existing_channel_binding_id": alias.channel_binding_id.id,
                            "candidate_channel_binding_id": binding.id,
                        }
                    )
                    raise IdentityConflictError(
                        "a conversation address belongs to another channel",
                        conflict_values=(
                            {
                                "account_id": binding.account_id.id,
                                "identity_ids": [(6, 0, identities.ids)],
                                "address_evidence_json": [evidence],
                            }
                            if identities
                            else {}
                        ),
                    )
                updates = self._alias_observation_updates(alias, address, observed_at)
                if updates:
                    alias.write(updates)
                continue
            self._advance_inbound_projection_revision(binding.account_id)
            role = (
                address.role
                if address.role in dict(alias_model._fields["role"].selection)
                else "routing"
            )
            confidence = (
                address.confidence
                if address.confidence
                in dict(alias_model._fields["confidence"].selection)
                else "observed"
            )
            alias_model.create(
                {
                    "channel_binding_id": binding.id,
                    "account_id": binding.account_id.id,
                    "namespace": address.namespace,
                    "value_raw": address.value,
                    "value_normalized": address.value_normalized,
                    "role": role,
                    "source_field": address.source_field,
                    "confidence": confidence,
                }
            )

    def _create_media_bindings(self, message_binding, media_values, state="pending"):
        media_model = self.env["contact.center.media.binding"].sudo()
        records = media_model
        for sequence, media in enumerate(media_values, start=1):
            if not isinstance(media, MediaDTO):
                media = MediaDTO.from_dict(media)
            records |= media_model.create(
                {
                    "message_binding_id": message_binding.id,
                    "sequence": sequence,
                    "kind": media.kind,
                    "external_media_id": media.external_media_id or False,
                    "remote_locator_json": media.remote_locator,
                    "mime_type": media.mime_type or False,
                    "file_name": media.file_name or False,
                    "size_bytes": media.size_bytes,
                    "sha256": media.sha256 or False,
                    "is_voice_note": media.is_voice_note,
                    "duration_seconds": media.duration_seconds,
                    "width": media.width,
                    "height": media.height,
                    "state": state,
                }
            )
        return records

    def _post_inbound(self, connection, event, inbox_event=None):
        account = connection.account_id
        self._lock_inbound_account_scope(account)
        identity = self._resolve_identity(
            account,
            event.actor,
            inbox_event=inbox_event,
            observed_name_at=self._event_datetime(event),
        )
        binding = (
            self._resolve_group_channel(account, event)
            if event.conversation.conversation_type == "group"
            else self._resolve_channel(account, identity, event)
        )
        self._lock_inbound_projection_binding(binding, account)
        self._enrich_channel_aliases(binding, event.conversation.addresses)
        if event.conversation.conversation_type == "group":
            profile = self.env["contact.center.group.profile"]._get_or_create(
                binding, connection
            )
            profile._request_sync(connection)
        else:
            # The first direct message schedules a provider pull; no remote I/O is
            # performed in webhook processing and later messages only refresh on TTL.
            binding._request_identity_avatar_sync(connection)
        external_message_id = event.message.external_message_id
        existing = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("channel_binding_id", "=", binding.id),
                    ("external_message_id", "=", external_message_id),
                ],
                limit=1,
            )
        )
        if existing:
            if existing._contact_center_merge_forwarding(
                event.message.is_forwarded,
                event.message.forwarding_score,
            ):
                self._notify_ui(
                    existing.channel_binding_id.channel_id,
                    "message_updated",
                    {"message_id": existing.message_id.id},
                )
            return existing.message_id

        self._apply_inbound_conversation_lifecycle(binding)
        self._auto_assign_inbound_conversation(binding)

        current_guest_members = binding.channel_id.sudo().channel_member_ids.guest_id
        if identity.mail_guest_id not in current_guest_members:
            partner_ids = binding.channel_id.sudo().channel_member_ids.partner_id.ids
            guest_ids = current_guest_members.ids + [identity.mail_guest_id.id]
            binding.channel_id._contact_center_reconcile_members(
                partner_ids=partner_ids, guest_ids=guest_ids
            )
        public_user = self.env.ref("base.public_user")
        occurred_at = self._event_datetime(event)
        reply_binding = self._event_reply_binding(
            binding, event, inbox_event=inbox_event
        )
        guest = identity.mail_guest_id.sudo()
        participant = (
            self._group_protocol_participant(event)
            if event.conversation.conversation_type == "group"
            else None
        )
        message = (
            binding.channel_id.with_user(public_user)
            .sudo()
            .with_context(guest=guest)
            ._contact_center_post(
                origin="inbound",
                body=html_escape(event.message.text or ""),
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
                date=occurred_at,
                partner_ids=[],
                parent_id=reply_binding.message_id.id if reply_binding else False,
            )
        )
        message_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": binding.id,
                    "provider_connection_id": connection.id,
                    "source_inbox_event_id": (inbox_event.id if inbox_event else False),
                    "direction": "inbound",
                    "origin": "provider",
                    "content_type": event.message.content_type,
                    "external_message_id": external_message_id,
                    "client_message_id": event.message.client_message_id,
                    "is_forwarded": event.message.is_forwarded,
                    "forwarding_score": event.message.forwarding_score or 0,
                    "forwarding_score_observed": (
                        event.message.forwarding_score is not None
                    ),
                    "protocol_snapshot_json": event.message.protocol_snapshot,
                    "structured_content_json": event.message.structured_content,
                    "protocol_participant_json": (
                        participant.to_dict() if participant else {}
                    ),
                    "reply_to_binding_id": reply_binding.id if reply_binding else False,
                    "delivery_state": "delivered",
                }
            )
        )
        if event.message.media:
            self._create_media_bindings(message_binding, event.message.media)
        self._publish_message_created(binding.channel_id, message, direction="inbound")
        return message

    @api.model
    def _group_own_protocol_participant(self, binding, connection, *, required=False):
        """Resolve the sender identity observed by this exact group session.

        The value is deliberately kept out of the UI DTO.  It may cross the
        provider boundary only after being revalidated against the current roster
        and provider-configuration revision.
        """

        binding.ensure_one()
        connection.ensure_one()
        if binding.conversation_type != "group":
            return None
        profile = (
            self.env["contact.center.group.profile"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)], limit=1)
        )
        unavailable = bool(
            not profile
            or profile.provider_connection_id != connection
            or not profile.own_protocol_participant_json
            or profile.own_protocol_participant_health_revision
            != connection.health_configuration_revision
        )
        if unavailable:
            if required:
                raise UserError(
                    _(
                        "Group sending is waiting for the provider to confirm this "
                        "session's identity in the group roster."
                    )
                )
            return None
        try:
            participant = AddressDTO.from_dict(profile.own_protocol_participant_json)
        except DTOValidationError as error:
            raise ValidationError(
                _("The group sender identity stored from the provider is invalid.")
            ) from error
        if participant.role != "sender" or participant.confidence != "protocol":
            raise ValidationError(
                _("The group sender identity stored from the provider is invalid.")
            )
        aliases = (
            self.env["contact.center.group.participant.alias"]
            .sudo()
            .search(
                [
                    ("group_profile_id", "=", profile.id),
                    ("participant_id.active", "=", True),
                    ("namespace", "=", participant.namespace),
                    ("value_normalized", "=", participant.value_normalized),
                    ("confidence", "=", "protocol"),
                ]
            )
        )
        if len(aliases) != 1:
            raise ValidationError(
                _(
                    "The group sender identity is no longer uniquely present in "
                    "the current provider roster."
                )
            )
        return participant

    @api.model
    def _group_echo_protocol_participant(self, connection, message_binding, event):
        """Keep AddressingMode identity stable across PN/LID echo aliases."""

        observed = self._group_protocol_participant(event)
        binding = message_binding.channel_binding_id
        current_values = message_binding.protocol_participant_json or {}
        current = None
        if current_values:
            try:
                current = AddressDTO.from_dict(current_values)
            except DTOValidationError as error:
                raise ValidationError(
                    _("The persisted group message participant is invalid.")
                ) from error
            if (
                current.namespace,
                current.value_normalized,
            ) == (observed.namespace, observed.value_normalized):
                return current

            # Historical message evidence remains immutable across a later
            # connection/configuration rotation. PN and LID are equivalent here
            # only when the retained roster proves they belong to the same entry.
            profile = binding.group_profile_ids.sudo().filtered(
                lambda item: item.provider_connection_id == connection
            )[:1]
            aliases = (
                self.env["contact.center.group.participant.alias"]
                .sudo()
                .search(
                    [
                        ("group_profile_id", "=", profile.id),
                        ("participant_id.active", "=", True),
                        (
                            "namespace",
                            "in",
                            [current.namespace, observed.namespace],
                        ),
                        (
                            "value_normalized",
                            "in",
                            [current.value_normalized, observed.value_normalized],
                        ),
                    ]
                )
                if profile
                else self.env["contact.center.group.participant.alias"]
            )
            by_key = {
                (alias.namespace, alias.value_normalized): alias.participant_id
                for alias in aliases
            }
            if by_key.get((current.namespace, current.value_normalized)) == by_key.get(
                (observed.namespace, observed.value_normalized)
            ) and by_key.get((current.namespace, current.value_normalized)):
                return current
            raise ValidationError(
                _(
                    "The provider echo participant conflicts with the persisted "
                    "group message participant."
                )
            )

        canonical = self._group_own_protocol_participant(
            binding, connection, required=False
        )
        if canonical is None:
            return observed
        profile = binding.group_profile_ids.sudo()[:1]
        canonical_alias = (
            self.env["contact.center.group.participant.alias"]
            .sudo()
            .search(
                [
                    ("group_profile_id", "=", profile.id),
                    ("participant_id.active", "=", True),
                    ("namespace", "=", canonical.namespace),
                    ("value_normalized", "=", canonical.value_normalized),
                ],
                limit=1,
            )
        )
        own_keys = {
            (alias.namespace, alias.value_normalized)
            for alias in canonical_alias.participant_id.sudo().alias_ids
        }
        observed_key = (observed.namespace, observed.value_normalized)
        if observed_key not in own_keys:
            raise ValidationError(
                _(
                    "The provider echo participant does not belong to the current "
                    "session's group roster entry."
                )
            )
        return canonical

    def _notify_ui(self, channel, event_type, payload, partner_ids=None):
        channel.ensure_one()
        common_data = {
            "schema_version": SCHEMA_VERSION,
            "event_type": event_type,
            "channel_id": channel.id,
            **payload,
        }
        partners = (
            self.env["res.partner"].sudo().browse(list(set(partner_ids))).exists()
            if partner_ids is not None
            else channel.sudo().channel_member_ids.filtered("partner_id").partner_id
        )
        muted_partner_ids = set()
        if (
            event_type == "message_created"
            and payload.get("direction") == "inbound"
            and partners
        ):
            muted_preferences = (
                self.env["contact.center.conversation.preference"]
                .sudo()
                .search(
                    [
                        ("channel_id", "=", channel.id),
                        ("muted", "=", True),
                        ("user_id.partner_id", "in", partners.ids),
                    ]
                )
            )
            muted_partner_ids = set(muted_preferences.user_id.partner_id.ids)
        notifications = []
        for partner in partners:
            data = common_data
            if (
                event_type == "message_created"
                and payload.get("direction") == "inbound"
            ):
                data = {
                    **common_data,
                    "personal_attention": partner.id not in muted_partner_ids,
                }
            notifications.append((partner, "contact_center/event", data))
        if notifications:
            try:
                with self.env.cr.savepoint():
                    self.env["bus.bus"].sudo()._sendmany(notifications)
            except Exception:
                # Realtime is an invalidation hint.  A transient bus failure must
                # never roll back a persisted inbound message or provider dispatch.
                _logger.exception(
                    "Contact Center UI notification failed for channel %s",
                    channel.id,
                )

    def _notify_connection_health(
        self, connection, invalidate=False, additional_partner_ids=None
    ):
        """Publish a safe health upsert or a scope-refresh invalidation."""

        connection.ensure_one()
        account = connection.sudo().account_id
        team = account.default_team_id
        users = self.env["res.users"].sudo()
        if account.owner_user_id:
            users |= account.owner_user_id
        if team:
            users |= (team.agent_ids | team.supervisor_ids).filtered(
                lambda user: user.active and not user.share
            )
        admin_group = self.env.ref(
            "contact_center_base.group_contact_center_admin",
            raise_if_not_found=False,
        )
        if admin_group:
            users |= (
                self.env["res.users"]
                .sudo()
                .search(
                    [
                        ("active", "=", True),
                        ("share", "=", False),
                        ("groups_id", "=", admin_group.id),
                        ("company_ids", "in", connection.company_id.ids),
                    ]
                )
            )
        partners = users.partner_id
        if invalidate and additional_partner_ids:
            partners |= (
                self.env["res.partner"].sudo().browse(list(set(additional_partner_ids)))
            )
        partners = partners.exists()
        if not partners:
            return False
        data = {
            "schema_version": SCHEMA_VERSION,
            "event_type": "connection_health_updated",
            "connection_id": connection.id,
        }
        if not invalidate:
            data["item"] = connection.sudo()._contact_center_health_item()
        try:
            with self.env.cr.savepoint():
                self.env["bus.bus"].sudo()._sendmany(
                    [(partner, "contact_center/event", data) for partner in partners]
                )
        except Exception:
            # Realtime is an invalidation hint and never controls health state.
            _logger.exception(
                "Contact Center health notification failed for connection %s",
                connection.id,
            )
        return True
