"""Mark the conversations of the current list as read, agent side only (L08).

The action has two steps. The preparation chooses explicit targets from the
list the agent sees; the execution never advances a read pointer over a
message that arrived after the preparation, and sends no read receipt to the
customer.
"""

from odoo import _, api, models
from odoo.exceptions import ValidationError
from odoo.osv import expression

from ..services.timeline import message_chronology_key
from ..services.tokens import CONTACT_CENTER_BULK_READ_TOKEN
from .ui_api import SCHEMA_VERSION

BULK_READ_LIMIT = 200


class ContactCenterUiApiBulkRead(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def prepare_mark_conversations_read(
        self, filters=None, exclude_channel_ids=None, limit=BULK_READ_LIMIT
    ):
        """Choose the unread conversations of the list, in list order.

        Nothing is written. Each target names the newest unread message of the
        conversation and the newest message ID the preparation saw.
        """

        self._application()._check_agent()
        limit = self._bounded_int(
            limit,
            default=BULK_READ_LIMIT,
            minimum=1,
            maximum=BULK_READ_LIMIT,
            label=_("limit"),
        )
        if filters is not None and not isinstance(filters, dict):
            raise ValidationError(_("The conversation filters must be an object."))
        excluded = self._bulk_read_channel_ids(exclude_channel_ids)
        domain = self._conversation_list_domain(dict(filters or {}, unread_only=True))
        if excluded:
            domain = expression.AND([domain, [("id", "not in", excluded)]])
        channels = self._bulk_read_ordered_channels(domain, limit + 1)
        remaining = len(channels) > limit
        channels = channels[:limit]
        newest_unread, newest_id = self._bulk_read_snapshot(channels)
        targets = [
            {
                "channel_id": channel.id,
                "message_id": newest_unread[channel.id],
                "max_message_id": newest_id[channel.id],
            }
            for channel in channels
            if channel.id in newest_unread
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "targets": targets,
            "count": len(targets),
            "remaining": remaining,
        }

    @api.model
    def mark_conversations_read(self, targets):
        """Advance the agent's read pointers to the prepared targets.

        The limit of each conversation recedes before the first message that
        arrived after the preparation with a date up to the target, and the
        conversation is skipped when that does not advance the pointer. The
        targets are explicit, so a retry after a concurrency conflict marks the
        same set. No ``mark_read`` command is created.
        """

        self._application()._check_agent()
        parsed = self._bulk_read_targets(targets)
        # Authorize every target before locking anything: a request never locks
        # a conversation its user cannot read.
        for channel_id, _message_id, _max_message_id in parsed:
            self._authorized_channel(channel_id)
        bindings = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search([("channel_id", "in", [target[0] for target in parsed])])
        )
        self._bulk_read_lock_topology([target[0] for target in parsed], bindings)
        account_by_channel = {
            binding.channel_id.id: binding.account_id.id for binding in bindings
        }
        # One canonical order across concurrent bulk actions: account, then id.
        parsed.sort(
            key=lambda target: (account_by_channel.get(target[0], 0), target[0])
        )
        marked = self.env["mail.channel"]
        # Every processed conversation returns its authoritative projection,
        # skipped ones included (already read elsewhere, or held back by an
        # arrival): the client cannot refresh rows beyond its window (L08-I01).
        processed = self.env["mail.channel"]
        bulk = self.with_context(
            contact_center_bulk_read_token=CONTACT_CENTER_BULK_READ_TOKEN
        )
        for channel_id, message_id, max_message_id in parsed:
            channel, member = self._authorized_channel(channel_id)
            target = self.env["mail.message"].browse(message_id).exists()
            if (
                not target
                or target.model != "mail.channel"
                or target.res_id != channel.id
            ):
                raise ValidationError(
                    _("The message does not belong to this conversation.")
                )
            # The same conversation lock as ingestion and the ordinary read. A
            # message committed after this snapshot versioned the channel row,
            # so the lock fails and the request is retried with a new snapshot.
            self._lock_seen_conversation(channel)
            processed |= channel
            limit = self._bulk_read_limit(channel, target, max_message_id)
            if not limit:
                continue
            member.invalidate_recordset(["seen_message_id"])
            if member.seen_message_id and message_chronology_key(
                limit
            ) <= message_chronology_key(member.seen_message_id):
                continue
            bulk._mark_member_pointer(channel.id, message_id=limit.id, seen=True)
            marked |= channel
        items = []
        if processed:
            prefetched = self._conversation_list_prefetch(processed)
            items = self._serialize_conversation_list_items(processed, prefetched)
        return {
            "schema_version": SCHEMA_VERSION,
            "marked": len(marked),
            "items": items,
        }

    # -- helpers ------------------------------------------------------------------

    @api.model
    def _bulk_read_lock_topology(self, channel_ids, bindings):
        """Lock every inbox, connection and conversation of the batch up front.

        The canonical order is kept (accounts, their connections, conversations,
        bindings). Taking them before the first conversation is processed means
        routine traffic in a later inbox of the batch can no longer version a
        row between the snapshot and its lock and abort the whole action
        (L08-CONC-01); the write fence still makes any message committed on a
        target fail the lock, and the retry sees it.
        """

        self.env[
            "contact.center.provider.connection"
        ].sudo()._contact_center_lock_operational_admission(bindings.account_id.ids)
        self.env.cr.execute(
            "SELECT id FROM mail_channel WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [sorted(channel_ids)],
        )
        if bindings:
            self.env.cr.execute(
                "SELECT id FROM contact_center_channel_binding "
                "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                [sorted(bindings.ids)],
            )

    @api.model
    def _bulk_read_channel_ids(self, values):
        if values in (None, False):
            return []
        if not isinstance(values, list) or len(values) > BULK_READ_LIMIT:
            raise ValidationError(_("Invalid conversation IDs."))
        return sorted(
            {self._positive_id(value, _("conversation ID")) for value in values}
        )

    @api.model
    def _bulk_read_targets(self, targets):
        if (
            not isinstance(targets, list)
            or not targets
            or len(targets) > BULK_READ_LIMIT
        ):
            raise ValidationError(
                _("Select between 1 and %s conversations.", BULK_READ_LIMIT)
            )
        parsed = []
        seen = set()
        for target in targets:
            if not isinstance(target, dict) or set(target) != {
                "channel_id",
                "message_id",
                "max_message_id",
            }:
                raise ValidationError(_("Invalid read target."))
            channel_id = self._positive_id(target["channel_id"], _("conversation ID"))
            message_id = self._positive_id(target["message_id"], _("message ID"))
            max_message_id = self._positive_id(
                target["max_message_id"], _("message ID")
            )
            if channel_id in seen:
                raise ValidationError(_("Each conversation may be targeted once."))
            if max_message_id < message_id:
                raise ValidationError(_("Invalid read target."))
            seen.add(channel_id)
            parsed.append((channel_id, message_id, max_message_id))
        return parsed

    @api.model
    def _bulk_read_ordered_channels(self, domain, limit):
        """The conversations of ``domain`` in list order: pinned, then activity."""

        pinned, _preferences = self._ordered_pinned_conversations(domain)
        pinned = pinned[:limit]
        all_pinned_ids = (
            self.env["contact.center.conversation.preference"]
            .search([("user_id", "=", self.env.user.id), ("pinned_at", "!=", False)])
            .channel_id.ids
        )
        remaining = limit - len(pinned)
        activity = self.env["mail.channel"]
        if remaining > 0:
            activity = self.env["mail.channel"].search(
                expression.AND([domain, [("id", "not in", all_pinned_ids or [0])]]),
                order="contact_center_last_message_at desc, id desc",
                limit=remaining,
            )
        return pinned | activity

    @api.model
    def _bulk_read_snapshot(self, channels):
        """Newest unread operational message and newest message ID per channel."""

        if not channels:
            return {}, {}
        self._flush_first_unread_dependencies()
        self.env.cr.execute(
            """
            SELECT DISTINCT ON (message.res_id) message.res_id, message.id
              FROM mail_message AS message
             WHERE message.model = 'mail.channel'
               AND message.res_id = ANY(%s)
               AND message.message_type NOT IN ('notification', 'user_notification')
               AND NOT EXISTS (
                    SELECT 1 FROM contact_center_internal_note_request AS note_request
                     WHERE note_request.message_id = message.id)
          ORDER BY message.res_id,
                   COALESCE(message.date, '9999-12-31 23:59:59'::timestamp) DESC,
                   message.id DESC
            """,
            [channels.ids],
        )
        newest_unread = dict(self.env.cr.fetchall())
        self.env.cr.execute(
            """
            SELECT res_id, MAX(id)
              FROM mail_message
             WHERE model = 'mail.channel' AND res_id = ANY(%s)
          GROUP BY res_id
            """,
            [channels.ids],
        )
        return newest_unread, dict(self.env.cr.fetchall())

    @api.model
    def _bulk_read_limit(self, channel, target, max_message_id):
        """The newest message the pointer may reach without passing an arrival.

        An operational message created after the preparation (ID above its
        snapshot) and dated up to the target would be covered by the target:
        the limit recedes to the newest message dated before the first one.
        """

        self._flush_first_unread_dependencies()
        self.env.cr.execute(
            """
            SELECT message.id
              FROM mail_message AS message
             WHERE message.model = 'mail.channel'
               AND message.res_id = %s
               AND message.id > %s
               AND message.message_type NOT IN ('notification', 'user_notification')
               AND NOT EXISTS (
                    SELECT 1 FROM contact_center_internal_note_request AS note_request
                     WHERE note_request.message_id = message.id)
               AND (COALESCE(message.date, '9999-12-31 23:59:59'::timestamp),
                    message.id)
                   <= (COALESCE(%s, '9999-12-31 23:59:59'::timestamp), %s)
          ORDER BY COALESCE(message.date, '9999-12-31 23:59:59'::timestamp),
                   message.id
             LIMIT 1
            """,
            [channel.id, max_message_id, target.date or None, target.id],
        )
        row = self.env.cr.fetchone()
        if not row:
            return target
        arrival = self.env["mail.message"].browse(row[0])
        self.env.cr.execute(
            """
            SELECT message.id
              FROM mail_message AS message
             WHERE message.model = 'mail.channel'
               AND message.res_id = %s
               AND message.id <= %s
               AND (COALESCE(message.date, '9999-12-31 23:59:59'::timestamp),
                    message.id)
                   < (COALESCE(%s, '9999-12-31 23:59:59'::timestamp), %s)
          ORDER BY COALESCE(message.date, '9999-12-31 23:59:59'::timestamp) DESC,
                   message.id DESC
             LIMIT 1
            """,
            [channel.id, max_message_id, arrival.date or None, arrival.id],
        )
        row = self.env.cr.fetchone()
        return self.env["mail.message"].browse(row[0]) if row else False
