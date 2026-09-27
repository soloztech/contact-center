"""Append-only lifecycle ledger of Contact Center conversations.

Responsible and service state of a conversation live on ``mail.channel`` and had
no history. Every writer already goes through ``mail.channel.create``/``write``
with the application token, so those two methods are the single recording point:
they compare the values before and after each write, per channel, and append one
event per changed dimension (assignment first, then state).

Each event advances ``contact_center_lifecycle_seq`` in the same channel write. A
transition therefore always updates the channel row: two transitions of one
conversation cannot commit out of order, because under REPEATABLE READ the second
one fails with a serialization error and is retried. Message bindings copy the
counter when they are created, which places every message after or before each
transition exactly as the transactions committed (see
``contact.center.attendance.episode``).
"""

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.osv import expression

from ..services.lifecycle import (
    LIFECYCLE_CREATIONS_CONTEXT_KEY,
    LIFECYCLE_EVENT_TOKEN,
    LIFECYCLE_SOURCE_AUTOMATIC_INBOUND,
    LIFECYCLE_SOURCE_CONTEXT_KEY,
    LifecycleCreations,
    lifecycle_source,
)

# Lifecycle extensions of the channel, the message binding and the application
# service belong to this one ledger.
# pylint: disable=consider-merging-classes-inherited

_LIKE_OPERATORS = {"like": "LIKE", "ilike": "ILIKE", "=like": "LIKE", "=ilike": "ILIKE"}


def _split_ids_and_names(values):
    """Split search values into record ids and names; refuse anything else."""

    ids, names = [], []
    for item in values:
        if isinstance(item, models.BaseModel):
            ids += item.ids
        elif type(item) is int:  # noqa: E721 - a bool is not an id
            ids.append(item)
        elif isinstance(item, str):
            names.append(item)
        else:
            raise UserError(_("Unsupported search value: %s", repr(item)))
    return ids, names


CONVERSATION_STATES = [
    ("open", "Open"),
    ("resolved", "Resolved"),
    ("archived", "Archived"),
]
EVENT_TYPES = [
    ("baseline", "Baseline at publication"),
    ("created", "Created"),
    ("assigned", "Assigned"),
    ("unassigned", "Unassigned"),
    ("resolved", "Resolved"),
    ("reopened", "Reopened"),
    ("archived", "Archived"),
    ("unarchived", "Unarchived"),
]
EVENT_SOURCES = [
    ("manual", "Manual"),
    ("automatic_inbound", "Automatic (inbound message)"),
    ("access_change", "Inbox access change"),
    ("creation", "Conversation creation"),
    ("migration", "Migration"),
]
# Events that state who is responsible, and events that state the service state.
ASSIGNMENT_EVENT_TYPES = ("baseline", "created", "assigned", "unassigned")
STATE_EVENT_TYPES = (
    "baseline",
    "created",
    "resolved",
    "reopened",
    "archived",
    "unarchived",
)
_TRACKED_FIELDS = ("contact_center_responsible_id", "contact_center_state")


def _state_event_type(previous_state, state):
    if state == "resolved":
        return "resolved"
    if state == "archived":
        return "archived"
    return "unarchived" if previous_state == "archived" else "reopened"


class ContactCenterConversationEvent(models.Model):
    _name = "contact.center.conversation.event"
    _description = "Conversation Lifecycle Event"
    _order = "channel_id, sequence"
    _rec_name = "event_type"
    # No create/write user columns: they would be foreign keys to res.users.
    # ``occurred_at`` and ``actor_ref`` record when and by whom.
    _log_access = False

    # The channel is the only foreign key of the ledger. A transition always holds
    # the channel row already; a foreign key to the inbox, the company or a user
    # would take FOR KEY SHARE on rows that inbound processing and access changes
    # lock FOR UPDATE *before* the channel, inverting the canonical lock order
    # (account/users -> channel) and deadlocking a manual transition with them.
    # The inbox and company are therefore derived from the channel, and users are
    # kept as plain ids with read-only display fields.
    channel_id = fields.Many2one(
        "mail.channel",
        string="Conversation",
        required=True,
        readonly=True,
        index=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        related="channel_id.contact_center_company_id", string="Company"
    )
    account_id = fields.Many2one(
        "contact.center.account",
        string="Inbox",
        compute="_compute_account_id",
        search="_search_account_id",
        help="Inbox of the conversation (its unique channel binding).",
    )
    conversation_type = fields.Selection(
        [("direct", "Direct"), ("group", "Group"), ("other", "Other")],
        compute="_compute_account_id",
        search="_search_conversation_type",
    )
    platform = fields.Char(
        string="Channel",
        compute="_compute_account_id",
        search="_search_platform",
    )
    sequence = fields.Integer(
        required=True,
        readonly=True,
        help="Position of the event in its conversation, serialized by the channel.",
    )
    event_type = fields.Selection(
        EVENT_TYPES, string="Event", required=True, readonly=True, index=True
    )
    responsible_ref = fields.Integer(
        string="Responsible User ID",
        readonly=True,
        index=True,
        help="Responsible agent after the event.",
    )
    previous_responsible_ref = fields.Integer(
        string="Previous Responsible User ID",
        readonly=True,
        index=True,
        help="Responsible agent before the event. A filled value on an "
        "assignment is a transfer.",
    )
    actor_ref = fields.Integer(
        string="Actor User ID",
        readonly=True,
        help="User of the transaction; empty for automatic processing.",
    )
    responsible_id = fields.Many2one(
        "res.users",
        string="Responsible",
        compute="_compute_users",
        search="_search_responsible_id",
        help="Responsible agent after the event.",
    )
    previous_responsible_id = fields.Many2one(
        "res.users",
        string="Previous Responsible",
        compute="_compute_users",
        search="_search_previous_responsible_id",
        help="Responsible agent before the event. A filled value on an "
        "assignment is a transfer.",
    )
    actor_id = fields.Many2one(
        "res.users",
        string="Actor",
        compute="_compute_users",
        search="_search_actor_id",
        help="User of the transaction; empty for automatic processing.",
    )
    responsible_label = fields.Char(string="Responsible", compute="_compute_users")
    previous_responsible_label = fields.Char(
        string="Previous Responsible", compute="_compute_users"
    )
    actor_label = fields.Char(string="Actor", compute="_compute_users")
    state = fields.Selection(
        CONVERSATION_STATES, readonly=True, help="Service state after the event."
    )
    previous_state = fields.Selection(
        CONVERSATION_STATES, readonly=True, help="Service state before the event."
    )
    source = fields.Selection(EVENT_SOURCES, required=True, readonly=True, index=True)
    occurred_at = fields.Datetime(
        required=True,
        readonly=True,
        index=True,
        help="Processing time of the transition. Events are never backdated.",
    )

    _sql_constraints = [
        (
            "channel_sequence_unique",
            "unique(channel_id, sequence)",
            "A conversation event position can be used only once.",
        ),
        (
            "sequence_positive",
            "check(sequence > 0)",
            "A conversation event position must be positive.",
        ),
    ]

    @api.depends("channel_id")
    def _compute_account_id(self):
        bindings = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .with_context(active_test=False)
            .search([("channel_id", "in", self.channel_id.ids)])
        )
        binding_by_channel = {binding.channel_id.id: binding for binding in bindings}
        for event in self:
            binding = binding_by_channel.get(event.channel_id.id)
            event.account_id = binding.account_id if binding else False
            event.conversation_type = binding.conversation_type if binding else False
            event.platform = binding.account_id.platform if binding else False

    @api.model
    def _channel_subselect(self, condition, params, positive=True):
        """Scope by the channel binding without the viewer's channel rules."""

        return [
            (
                "channel_id",
                "inselect" if positive else "not inselect",
                (
                    "SELECT binding.channel_id"
                    "  FROM contact_center_channel_binding AS binding"
                    "  JOIN contact_center_account AS account"
                    "    ON account.id = binding.account_id"
                    " WHERE " + condition,
                    params,
                ),
            )
        ]

    def _search_binding_value(self, column, operator, value, resolve=None):
        """Search a binding/inbox column with the semantics of a stored field.

        False/None means "no such value" (L09-LEDGER-02). A negative operator is
        the exact complement of its positive one, so a conversation without a
        binding matches it as an empty value would; an empty ``in`` list matches
        nothing and an empty ``not in`` list everything; like operators keep
        their case sensitivity and wildcards; any other operator is refused
        rather than given another meaning (L09-I2-02). ``resolve(operator,
        value)`` turns names into ids for a relational column. ``column`` is a
        fixed SQL identifier chosen by the caller, never input.
        """

        if operator in expression.NEGATIVE_TERM_OPERATORS:
            positive = self._search_binding_value(
                column, expression.TERM_OPERATORS_NEGATION[operator], value, resolve
            )
            if positive == expression.FALSE_DOMAIN:
                return list(expression.TRUE_DOMAIN)
            return ["!"] + positive
        if operator in ("=", "in"):
            values = list(value) if isinstance(value, (list, tuple)) else [value]
            empty = any(item is False or item is None for item in values)
            values = [item for item in values if item is not False and item is not None]
            if values and resolve:
                values = resolve("in", values)
            elif any(not isinstance(item, str) for item in values):
                raise UserError(_("Unsupported search value: %s", repr(value)))
            domains = []
            if values:
                domains.append(
                    self._channel_subselect(column + " = ANY(%s)", [list(values)])
                )
            if empty:
                # No binding with a value: the complement of "has one".
                domains.append(
                    self._channel_subselect(column + " IS NOT NULL", [], False)
                )
            return expression.OR(domains) if domains else list(expression.FALSE_DOMAIN)
        if operator in _LIKE_OPERATORS and isinstance(value, str):
            if resolve:
                return self._channel_subselect(
                    column + " = ANY(%s)", [list(resolve(operator, value))]
                )
            pattern = value if operator.startswith("=") else "%%%s%%" % value
            return self._channel_subselect(
                "%s %s %%s" % (column, _LIKE_OPERATORS[operator]), [pattern]
            )
        raise UserError(_("Unsupported search operator: %s", operator))

    def _search_conversation_type(self, operator, value):
        return self._search_binding_value("binding.conversation_type", operator, value)

    def _search_platform(self, operator, value):
        return self._search_binding_value("account.platform", operator, value)

    def _search_account_id(self, operator, value):
        # A sub-select keeps the conversation record rules of the viewer out of
        # the ledger search; the ledger's own rules still apply.
        return self._search_binding_value(
            "binding.account_id", operator, value, self._resolve_account_ids
        )

    @api.model
    def _resolve_account_ids(self, operator, value):
        accounts = self.env["contact.center.account"].with_context(active_test=False)
        if operator != "in":
            return accounts.search([("name", operator, value)]).ids
        ids, names = _split_ids_and_names(value)
        if names:
            ids += accounts.search([("name", "in", names)]).ids
        return ids

    @api.depends("responsible_ref", "previous_responsible_ref", "actor_ref")
    def _compute_users(self):
        users = self.env["res.users"].sudo().with_context(active_test=False)
        existing = set(
            users.browse(
                {
                    ref
                    for event in self
                    for ref in (
                        event.responsible_ref,
                        event.previous_responsible_ref,
                        event.actor_ref,
                    )
                    if ref
                }
            )
            .exists()
            .ids
        )

        def user(ref):
            return users.browse(ref if ref in existing else [])

        def label(ref):
            # A deleted user keeps its id in the ledger and reads as removed.
            if not ref:
                return False
            if ref in existing:
                return user(ref).display_name
            return _("Removed user (#%s)", ref)

        for event in self:
            event.responsible_id = user(event.responsible_ref)
            event.previous_responsible_id = user(event.previous_responsible_ref)
            event.actor_id = user(event.actor_ref)
            event.responsible_label = label(event.responsible_ref)
            event.previous_responsible_label = label(event.previous_responsible_ref)
            event.actor_label = label(event.actor_ref)

    def _search_user_ref(self, column, operator, value):
        # The ORM stores an empty Integer as 0; the migration writes NULL. Both
        # mean "no user", also inside in/not in lists (L09-LEDGER-02). A name
        # search keeps its negation: "not like" also matches events without a
        # user, as on a stored Many2one (L09-I2-02).
        empty_domain = ["|", (column, "=", False), (column, "=", 0)]
        users = self.env["res.users"].with_context(active_test=False)
        if operator in ("=", "!=", "in", "not in"):
            values = value if isinstance(value, (list, tuple)) else [value]
            empty = any(item is False or item is None for item in values)
            ids, names = _split_ids_and_names(
                [item for item in values if item is not False and item is not None]
            )
            if names:
                ids += users.search([("name", "in", names)]).ids
            if operator in ("=", "in"):
                domains = [[(column, "in", ids)]] if ids else []
                if empty:
                    domains.append(empty_domain)
                return expression.OR(domains) if domains else [(0, "=", 1)]
            domain = [(column, ">", 0)] if empty else []
            if ids:
                domain.append((column, "not in", ids))
            return domain or [(1, "=", 1)]
        negative = operator in ("not like", "not ilike")
        if (operator in _LIKE_OPERATORS or negative) and isinstance(value, str):
            positive = (
                expression.TERM_OPERATORS_NEGATION[operator] if negative else operator
            )
            matches = users._search([("name", positive, value)])
            if negative:
                return expression.OR([[(column, "not in", matches)], empty_domain])
            return [(column, "in", matches)]
        raise UserError(_("Unsupported search operator: %s", operator))

    def _search_responsible_id(self, operator, value):
        return self._search_user_ref("responsible_ref", operator, value)

    def _search_previous_responsible_id(self, operator, value):
        return self._search_user_ref("previous_responsible_ref", operator, value)

    def _search_actor_id(self, operator, value):
        return self._search_user_ref("actor_ref", operator, value)

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("contact_center_lifecycle_event_token")
            is not LIFECYCLE_EVENT_TOKEN
        ):
            raise AccessError(
                _("Conversation history is recorded by the conversation service.")
            )
        return super().create(vals_list)

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Conversation history is immutable."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Conversation history is immutable."))

    @api.model
    def _contact_center_lifecycle_now(self):
        """Processing clock of lifecycle events (a seam for deterministic tests)."""

        return fields.Datetime.now()

    @api.model
    def _contact_center_lifecycle_actor(self):
        """Return the human user of this transaction, if any.

        Inbound processing runs as a technical user and is flagged by its source
        object; the superuser and share/public users are never recorded as actors.
        """

        source = lifecycle_source(self.env.context)
        if source and source.automatic:
            return False
        uid = self.env.uid
        if not uid or uid == SUPERUSER_ID:
            return False
        user = self.env["res.users"].sudo().browse(uid)
        if not user.exists() or user.share:
            return False
        return uid

    @api.model
    def _contact_center_append(self, values_list):
        return (
            self.sudo()
            .with_context(contact_center_lifecycle_event_token=LIFECYCLE_EVENT_TOKEN)
            .create(values_list)
        )

    @api.model
    def _contact_center_occurred_at(self, channels):
        """Return a per-channel processing time that never goes backwards.

        A wall clock may step back (NTP, virtual machines). Clamping to the last
        event of the same conversation keeps every lifecycle interval well formed.
        """

        now = self._contact_center_lifecycle_now()
        result = {channel.id: now for channel in channels}
        if not channels:
            return result
        self.flush_model(["channel_id", "occurred_at"])
        self.env.cr.execute(
            """
            SELECT channel_id, max(occurred_at)
              FROM contact_center_conversation_event
             WHERE channel_id = ANY(%s)
          GROUP BY channel_id
            """,
            [channels.ids],
        )
        for channel_id, last_at in self.env.cr.fetchall():
            if last_at and last_at > result[channel_id]:
                result[channel_id] = last_at
        return result

    @api.model
    def _contact_center_record_migration_baselines(self):
        """Record the present of every conversation that has no history yet.

        One ``baseline`` (sequence 1, source ``migration``) holds the state and the
        responsible at the time of the upgrade; it is a present fact, not a
        reconstruction of the past. The counter becomes 1 in the same statement, so
        the next message copies 1 and the next transition receives 2. Conversations
        that already have events are ignored and a counter is never lowered, which
        makes a second run a no-op.
        """

        self.env.flush_all()
        now = self._contact_center_lifecycle_now()
        self.env.cr.execute(
            """
            WITH inserted AS (
                INSERT INTO contact_center_conversation_event (
                    channel_id, sequence, event_type, responsible_ref, state,
                    source, occurred_at
                )
                SELECT channel.id, 1, 'baseline',
                       channel.contact_center_responsible_id,
                       channel.contact_center_state, 'migration', %(now)s
                  FROM mail_channel AS channel
                 WHERE channel.channel_type = 'contact_center'
                   AND channel.contact_center_company_id IS NOT NULL
                   AND NOT EXISTS (
                        SELECT 1
                          FROM contact_center_conversation_event AS event
                         WHERE event.channel_id = channel.id
                   )
             RETURNING channel_id
            )
            UPDATE mail_channel AS channel
               SET contact_center_lifecycle_seq =
                   GREATEST(COALESCE(channel.contact_center_lifecycle_seq, 0), 1)
              FROM inserted
             WHERE channel.id = inserted.channel_id
         RETURNING channel.id
            """,
            {"now": now},
        )
        channel_ids = [row[0] for row in self.env.cr.fetchall()]
        self.env["mail.channel"].browse(channel_ids).invalidate_recordset(
            ["contact_center_lifecycle_seq"]
        )
        self.invalidate_model()
        return channel_ids


class MailChannel(models.Model):
    _inherit = "mail.channel"

    contact_center_lifecycle_seq = fields.Integer(
        string="Lifecycle Sequence",
        default=0,
        readonly=True,
        copy=False,
        help="Last lifecycle event position of the conversation. Every transition "
        "advances it in the same channel write; new messages copy it.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any("contact_center_lifecycle_seq" in values for values in vals_list):
            raise AccessError(
                _("The conversation lifecycle is managed by the application service.")
            )
        default_channel_type = self.default_get(["channel_type"]).get("channel_type")
        prepared = []
        for values in vals_list:
            channel_type = values.get("channel_type", default_channel_type)
            if channel_type == "contact_center":
                # The creation event is the first position of the conversation.
                values = dict(values, contact_center_lifecycle_seq=1)
            prepared.append(values)
        channels = super().create(prepared)
        contact_center = channels.sudo().filtered(
            lambda channel: channel.channel_type == "contact_center"
        )
        if contact_center:
            contact_center._contact_center_record_creation()
        return channels

    def write(self, values):
        if "contact_center_lifecycle_seq" in values:
            raise AccessError(
                _("The conversation lifecycle is managed by the application service.")
            )
        if not self or not any(name in values for name in _TRACKED_FIELDS):
            return super().write(values)
        self._contact_center_lock_lifecycle_rows()
        plans = self._contact_center_lifecycle_plans(values)
        if not plans:
            return super().write(values)
        planned = self.browse(list(plans))
        unchanged = self - planned
        if unchanged:
            super(MailChannel, unchanged).write(values)
        for channel in planned:
            super(MailChannel, channel).write(
                dict(values, contact_center_lifecycle_seq=plans[channel.id][-1][0])
            )
        self._contact_center_record_transitions(planned, plans)
        return True

    def _contact_center_lock_lifecycle_rows(self):
        """Own the conversation rows before planning their next positions.

        Most writers already hold the lock (then this is a no-op). One that does
        not, such as an inbox access change, would otherwise plan from its
        snapshot and insert an event whose position a concurrent transition has
        just committed: a non-retried unique violation. Taking the row lock here,
        in id order and before any event insert, turns that race into the usual
        retryable serialization failure.
        """

        channel_ids = sorted(
            channel.id
            for channel in self.sudo()
            if channel.channel_type == "contact_center"
        )
        if channel_ids:
            self.env.cr.execute(
                "SELECT id FROM mail_channel WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                [channel_ids],
            )

    def _contact_center_lifecycle_plans(self, values):
        """Return ``{channel_id: [(sequence, event values), ...]}`` for this write.

        Old values come from the transaction cache/snapshot; the new ones are the
        ORM conversion of ``values``. A dimension whose value does not change
        produces no event, so a no-op write records nothing.
        """

        responsible_field = self._fields["contact_center_responsible_id"]
        state_field = self._fields["contact_center_state"]
        plans = {}
        for channel in self.sudo():
            if channel.channel_type != "contact_center":
                continue
            old_responsible = channel.contact_center_responsible_id.id or False
            old_state = channel.contact_center_state or False
            new_responsible = old_responsible
            new_state = old_state
            if "contact_center_responsible_id" in values:
                new_responsible = (
                    responsible_field.convert_to_cache(
                        values["contact_center_responsible_id"], channel
                    )
                    or False
                )
            if "contact_center_state" in values:
                new_state = (
                    state_field.convert_to_cache(
                        values["contact_center_state"], channel
                    )
                    or False
                )
            sequence = channel.contact_center_lifecycle_seq or 0
            events = []
            if new_responsible != old_responsible:
                sequence += 1
                events.append(
                    (
                        sequence,
                        {
                            "event_type": (
                                "assigned" if new_responsible else "unassigned"
                            ),
                            "responsible_ref": new_responsible,
                            "previous_responsible_ref": old_responsible,
                            "state": old_state,
                            "previous_state": old_state,
                        },
                    )
                )
            if new_state != old_state and new_state:
                sequence += 1
                events.append(
                    (
                        sequence,
                        {
                            "event_type": _state_event_type(old_state, new_state),
                            "responsible_ref": new_responsible,
                            "previous_responsible_ref": new_responsible,
                            "state": new_state,
                            "previous_state": old_state,
                        },
                    )
                )
            if events:
                plans[channel.id] = events
        return plans

    def _contact_center_record_transitions(self, channels, plans):
        event_model = self.env["contact.center.conversation.event"]
        source = lifecycle_source(self.env.context)
        actor_ref = event_model._contact_center_lifecycle_actor()
        occurred_at = event_model._contact_center_occurred_at(channels)
        values_list = []
        for channel in channels.sudo():
            for sequence, values in plans[channel.id]:
                values_list.append(
                    dict(
                        values,
                        channel_id=channel.id,
                        sequence=sequence,
                        actor_ref=actor_ref,
                        source=source.value if source else "manual",
                        occurred_at=occurred_at[channel.id],
                    )
                )
        return event_model._contact_center_append(values_list)

    def _contact_center_record_creation(self):
        """Append ``created`` for every new conversation, even without owner.

        An empty responsible on this event means "known to have no responsible",
        which the report distinguishes from a conversation without history. The
        event joins the creation collector of the processing that created it, if
        any, so only that processing can offer it as causal evidence.
        """

        event_model = self.env["contact.center.conversation.event"]
        actor_ref = event_model._contact_center_lifecycle_actor()
        now = event_model._contact_center_lifecycle_now()
        events = event_model._contact_center_append(
            [
                {
                    "channel_id": channel.id,
                    "sequence": channel.contact_center_lifecycle_seq,
                    "event_type": "created",
                    "responsible_ref": channel.contact_center_responsible_id.id,
                    "state": channel.contact_center_state,
                    "actor_ref": actor_ref,
                    "source": "creation",
                    "occurred_at": now,
                }
                for channel in self
            ]
        )
        creations = self.env.context.get(LIFECYCLE_CREATIONS_CONTEXT_KEY)
        if isinstance(creations, LifecycleCreations):
            for event in events:
                creations.add(event)
        return events

    def _contact_center_lifecycle_event_at(self, sequence):
        self.ensure_one()
        return (
            self.env["contact.center.conversation.event"]
            .sudo()
            .search(
                [("channel_id", "=", self.id), ("sequence", "=", sequence)], limit=1
            )
        )


class ContactCenterMessageBinding(models.Model):
    _inherit = "contact.center.message.binding"

    lifecycle_seq = fields.Integer(
        string="Lifecycle Sequence",
        readonly=True,
        copy=False,
        help="Conversation lifecycle position when the message was recorded. "
        "Empty for messages recorded before the lifecycle history existed.",
    )
    caused_assignment_event_id = fields.Many2one(
        "contact.center.conversation.event",
        string="Caused Assignment",
        readonly=True,
        copy=False,
        index="btree_not_null",
        ondelete="set null",
        groups="contact_center_base.group_contact_center_supervisor",
        help="Automatic assignment, or automatic creation of the conversation, "
        "performed while processing this very message.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any("lifecycle_seq" in values for values in vals_list):
            raise AccessError(
                _("The conversation lifecycle is managed by the application service.")
            )
        prepared = [
            dict(
                values,
                lifecycle_seq=self._contact_center_lifecycle_position(
                    values.get("channel_binding_id")
                ),
            )
            for values in vals_list
        ]
        bindings = super().create(prepared)
        bindings.sudo().filtered(
            "caused_assignment_event_id"
        )._contact_center_check_caused_assignment()
        return bindings

    def write(self, values):
        if {"lifecycle_seq", "caused_assignment_event_id"} & set(values):
            raise AccessError(_("The message lifecycle position is immutable."))
        return super().write(values)

    @api.model
    def _contact_center_lifecycle_position(self, channel_binding_id):
        """Copy the conversation counter after this transaction's transitions.

        Every message-binding creator already holds the canonical channel ->
        binding lock (``_contact_center_lock_channel_then_binding``) before it
        creates the binding:

        * customer messages, direct and group (``_post_inbound``) and control cards
          (``_post_control_card``) through ``_lock_inbound_projection_binding``;
        * phone replies (``_reconcile_from_me``) through the same lock;
        * agent and automation sends (``_send_message``) and resends
          (``_resend_message``) through ``_lock_active_outbound_scope``.

        The ``FOR UPDATE`` below is therefore a lock no-op that cannot invert the
        lock order; with the flush it guarantees that the value read is the row
        version this transaction owns, including its own pending transitions.
        """

        if type(channel_binding_id) is not int:  # noqa: E721 - reject bool IDs
            return False
        channel = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .browse(channel_binding_id)
            .exists()
            .channel_id
        )
        if not channel:
            return False
        channel.flush_recordset(["contact_center_lifecycle_seq"])
        self.env.cr.execute(
            "SELECT contact_center_lifecycle_seq FROM mail_channel "
            "WHERE id = %s FOR UPDATE",
            [channel.id],
        )
        row = self.env.cr.fetchone()
        return row[0] or 0 if row else False

    def _contact_center_check_caused_assignment(self):
        """Accept only the event the processing of this very message recorded.

        That is an automatic assignment at the customer message's own position,
        or the automatic creation of the conversation when this is its first
        message: a customer message, or a reply from the business phone.
        """

        for binding in self:
            event = binding.caused_assignment_event_id
            customer = binding.direction == "inbound" and binding.origin == "provider"
            valid = (
                event.channel_id == binding.channel_binding_id.channel_id
                and event.sequence == binding.lifecycle_seq
            )
            if valid and event.event_type == "assigned":
                valid = customer and event.source == "automatic_inbound"
            elif valid and event.event_type == "created":
                valid = (
                    (customer or binding.origin == "external_device")
                    and event.source == "creation"
                    and not event.actor_ref
                    and not self.search_count(
                        [
                            ("channel_binding_id", "=", binding.channel_binding_id.id),
                            ("id", "<", binding.id),
                        ]
                    )
                )
            else:
                valid = False
            if not valid:
                raise ValidationError(
                    _("The assignment evidence does not belong to this message.")
                )


class ContactCenterApplicationLifecycle(models.AbstractModel):
    _inherit = "contact.center.application"

    def _process_event(self, connection, event, inbox_event=None):
        # Inbound processing is automatic: conversations it creates, reopens or
        # assigns have no human actor, whatever technical user runs the job.
        return super(
            ContactCenterApplicationLifecycle,
            self.with_context(
                **{LIFECYCLE_SOURCE_CONTEXT_KEY: LIFECYCLE_SOURCE_AUTOMATIC_INBOUND}
            ),
        )._process_event(connection, event, inbox_event=inbox_event)
