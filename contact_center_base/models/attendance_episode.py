"""Attendance projections over the conversation lifecycle ledger.

``contact.center.attendance.message`` places every direct-conversation message
recorded after the lifecycle history existed (``lifecycle_seq`` filled) in a
service cycle; ``contact.center.attendance.episode`` derives the waiting episodes
of each cycle. Both are read-only SQL views built from the same statement so the
report, the list and the pivot always agree.

Rules (plan L09 §2 item 2):

* A cycle starts at ``created``, ``baseline`` (when open), ``reopened`` or
  ``unarchived`` and ends at the next state event (``resolved``/``archived``).
  A message belongs to the segment of the last state event whose sequence is not
  greater than its ``lifecycle_seq``: the order of commits, not of clocks.
* A reply written on the phone (``external_device``) is only recorded when its
  webhook arrives. It stays in the segment of its sequence when that segment is
  open and its provider time is not earlier than the event that opened it
  (L09-14/L09-16/L09-17); otherwise it is placed by provider time in the one cycle
  whose ``[temporal start, end)`` interval contains it. A cycle opened by inbound
  processing starts, in time, at the earlier of its event and the provider time of
  the message that opened it (the one that created the conversation, or the first
  of an automatic reopening), because the phone sees that message before its
  webhook is processed. A time equal to a cycle's closing second, inside more than
  one interval, between cycles or before the history is ambiguous: the reply is not
  placed and is counted as coverage.
* Effective times are whole seconds: provider time for customer and phone
  messages, first positive delivery evidence for agent and automation messages
  (no evidence, no response and no outbound volume). Within a cycle,
  ``(effective time, binding id)`` is a total order; binding ids are serialized
  per conversation by the channel lock.
* An episode starts at the first customer message (control cards excluded) after
  the previous human response and ends at the next human response. Automation never
  answers. The end of a cycle closes a pending episode as ``closed_unanswered``.
* The responsible at the start is the one in force at the provider time of the
  opening message: the event that message's own processing recorded (automatic
  assignment, or creation of the conversation), else the last assignment event not
  later than that time, else unknown (L09-15).
"""

import datetime

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.tools import drop_view_if_exists

from ..services.dto import SCHEMA_VERSION
from .control_events import _CONTROL_CONTENT_TYPES
from .conversation_event import ASSIGNMENT_EVENT_TYPES, STATE_EVENT_TYPES

# The message and episode projections share one SQL statement and one report.
# pylint: disable=consider-merging-classes-inherited


def _sql_list(values):
    # Only module constants (fixed identifiers without quotes) reach this helper.
    return ", ".join("'%s'" % value for value in sorted(values))


_POSITIVE_DELIVERY_STATES = ("sent", "delivered", "read")

# Common table expressions shared by both views. Every time is truncated to the
# second because providers do not report fractions. The expressions are inlined
# (NOT MATERIALIZED) and every window, DISTINCT ON and GROUP BY keeps the channel
# among its keys, so a filter on the conversation (for example the candidate
# conversations of a report period) reaches the base tables through their indexes
# instead of computing the whole history.
_PLACEMENT_CTES = """
    segment AS NOT MATERIALIZED (
        SELECT event.channel_id,
               event.id AS event_id,
               event.sequence AS seq_from,
               LEAD(event.sequence) OVER lifecycle AS seq_to,
               event.state,
               event.event_type,
               event.source,
               date_trunc('second', event.occurred_at) AS at_from,
               date_trunc('second', LEAD(event.occurred_at) OVER lifecycle) AS at_to
          FROM contact_center_conversation_event AS event
         WHERE event.event_type IN ({state_types})
        WINDOW lifecycle AS (PARTITION BY event.channel_id ORDER BY event.sequence)
    ),
    base AS NOT MATERIALIZED (
        SELECT binding.id,
               channel_binding.channel_id,
               channel_binding.account_id,
               channel_binding.company_id,
               account.platform,
               binding.direction,
               CASE
                   WHEN binding.direction = 'inbound'
                        AND binding.origin = 'provider'
                        AND binding.content_type IN ({control_types})
                       THEN 'control'
                   WHEN binding.direction = 'inbound' AND binding.origin = 'provider'
                       THEN 'customer'
                   WHEN binding.origin = 'agent' THEN 'agent'
                   WHEN binding.origin = 'external_device' THEN 'phone'
                   WHEN binding.origin = 'automation' THEN 'automation'
                   ELSE 'other'
               END AS kind,
               binding.lifecycle_seq,
               binding.caused_assignment_event_id,
               mail.create_uid AS author_user_id,
               date_trunc('second', mail.date) AS message_at,
               CASE WHEN binding.origin IN ('agent', 'automation') THEN (
                   SELECT date_trunc('second', min(delivery.occurred_at))
                     FROM contact_center_delivery_event AS delivery
                    WHERE delivery.message_binding_id = binding.id
                      AND delivery.state IN ({positive_states})
               ) END AS delivered_at
          FROM contact_center_message_binding AS binding
          JOIN contact_center_channel_binding AS channel_binding
            ON channel_binding.id = binding.channel_binding_id
          JOIN contact_center_account AS account
            ON account.id = channel_binding.account_id
          JOIN mail_message AS mail ON mail.id = binding.message_id
         WHERE binding.lifecycle_seq IS NOT NULL
           AND channel_binding.conversation_type = 'direct'
    ),
    by_sequence AS NOT MATERIALIZED (
        SELECT base.*,
               segment.seq_from AS segment_seq,
               segment.state AS segment_state,
               segment.at_from AS segment_opened_at,
               segment.event_type AS segment_opened_by,
               segment.at_to AS segment_closed_at
          FROM base
     LEFT JOIN segment
            ON segment.channel_id = base.channel_id
           AND segment.seq_from <= base.lifecycle_seq
           AND (segment.seq_to IS NULL OR base.lifecycle_seq < segment.seq_to)
    ),
    -- Open cycles for phone placement, with two boundaries: the original opening
    -- (its event) and the temporal start. A cycle opened by inbound processing
    -- starts, in time, at the earlier of its event and the provider time of the
    -- message that opened it (the one that created the conversation, by its
    -- causal evidence, or the first customer message of an automatic
    -- reopening): the phone saw that message before its webhook was processed.
    -- Index probes per open cycle keep this out of the per-message pipeline.
    cycle AS NOT MATERIALIZED (
        SELECT segment.channel_id,
               segment.seq_from,
               segment.at_to,
               CASE
                   WHEN segment.event_type = 'created' THEN LEAST(
                       segment.at_from,
                       (
                           SELECT date_trunc('second', min(mail.date))
                             FROM contact_center_message_binding AS opening
                             JOIN mail_message AS mail ON mail.id = opening.message_id
                            WHERE opening.caused_assignment_event_id
                                = segment.event_id
                       )
                   )
                   WHEN segment.event_type = 'reopened'
                        AND segment.source = 'automatic_inbound' THEN LEAST(
                       segment.at_from,
                       (
                           SELECT date_trunc('second', mail.date)
                             FROM contact_center_channel_binding AS reopened
                             JOIN contact_center_message_binding AS opening
                               ON opening.channel_binding_id = reopened.id
                             JOIN mail_message AS mail ON mail.id = opening.message_id
                            WHERE reopened.channel_id = segment.channel_id
                              AND opening.direction = 'inbound'
                              AND opening.origin = 'provider'
                              AND opening.content_type NOT IN ({control_types})
                              AND opening.lifecycle_seq >= segment.seq_from
                              AND (segment.seq_to IS NULL
                                   OR opening.lifecycle_seq < segment.seq_to)
                         ORDER BY opening.id
                            LIMIT 1
                       )
                   )
                   ELSE segment.at_from
               END AS at_from
          FROM segment
         WHERE segment.state = 'open'
    ),
    message AS NOT MATERIALIZED (
        SELECT by_sequence.id,
               by_sequence.channel_id,
               by_sequence.account_id,
               by_sequence.company_id,
               by_sequence.platform,
               by_sequence.direction,
               by_sequence.kind,
               by_sequence.lifecycle_seq,
               by_sequence.caused_assignment_event_id,
               by_sequence.author_user_id,
               by_sequence.message_at,
               by_sequence.segment_opened_by,
               by_sequence.segment_closed_at,
               located.cycle_sequence,
               CASE
                   WHEN by_sequence.kind IN ('agent', 'automation')
                       THEN by_sequence.delivered_at
                   ELSE by_sequence.message_at
               END AS effective_at,
               CASE
                   WHEN located.cycle_sequence IS NOT NULL THEN 'cycle'
                   WHEN by_sequence.kind = 'phone' THEN 'unplaced'
                   WHEN by_sequence.segment_seq IS NULL THEN 'uncovered'
                   WHEN by_sequence.segment_state = 'archived' THEN 'archived'
                   ELSE 'closed'
               END AS placement
          FROM by_sequence
         CROSS JOIN LATERAL (
            SELECT CASE
                       -- L09-14/L09-16/L09-17: a reply recorded while its own
                       -- cycle was open, at or after that cycle's original
                       -- opening, stays there. Any other reply is placed by
                       -- provider time in the one cycle whose [temporal start,
                       -- end) contains it; none, more than one or a closing
                       -- second is ambiguous. The sub-select runs only for
                       -- the other phone replies, over their own conversation.
                       WHEN by_sequence.kind = 'phone'
                            AND by_sequence.segment_state = 'open'
                            AND by_sequence.message_at
                                >= by_sequence.segment_opened_at
                           THEN by_sequence.segment_seq
                       WHEN by_sequence.kind = 'phone' THEN (
                           SELECT CASE
                                      WHEN bool_or(
                                          open_cycle.at_to = by_sequence.message_at
                                      ) THEN NULL
                                      WHEN count(*) FILTER (
                                          WHERE open_cycle.at_from
                                                  <= by_sequence.message_at
                                            AND (open_cycle.at_to IS NULL
                                                 OR by_sequence.message_at
                                                     < open_cycle.at_to)
                                      ) = 1 THEN max(open_cycle.seq_from) FILTER (
                                          WHERE open_cycle.at_from
                                                  <= by_sequence.message_at
                                            AND (open_cycle.at_to IS NULL
                                                 OR by_sequence.message_at
                                                     < open_cycle.at_to)
                                      )
                                  END
                             FROM cycle AS open_cycle
                            WHERE open_cycle.channel_id = by_sequence.channel_id
                       )
                       WHEN by_sequence.segment_state = 'open'
                           THEN by_sequence.segment_seq
                   END AS cycle_sequence
         ) AS located
    )
""".format(
    state_types=_sql_list(STATE_EVENT_TYPES),
    positive_states=_sql_list(_POSITIVE_DELIVERY_STATES),
    control_types=_sql_list(_CONTROL_CONTENT_TYPES),
)

# Responsible in force at ``{time}`` for rows of ``{alias}``: the event the
# row's own processing recorded (automatic assignment or creation of the
# conversation), else the last assignment event not later than the time, else
# unknown. Users are plain ids in the ledger (see the event model).
_RESPONSIBLE_JOINS = """
     LEFT JOIN contact_center_conversation_event AS caused
            ON caused.id = {alias}.caused_assignment_event_id
     LEFT JOIN LATERAL (
            SELECT event.id, NULLIF(event.responsible_ref, 0) AS responsible_ref
              FROM contact_center_conversation_event AS event
             WHERE event.channel_id = {alias}.channel_id
               AND event.event_type IN ({assignment_types})
               AND date_trunc('second', event.occurred_at) <= {time}
          ORDER BY event.sequence DESC
             LIMIT 1
          ) AS at_time ON TRUE
    CROSS JOIN LATERAL (
            SELECT CASE
                       WHEN caused.id IS NOT NULL THEN NULLIF(caused.responsible_ref, 0)
                       ELSE at_time.responsible_ref
                   END AS ref
          ) AS responsible
     LEFT JOIN res_users AS responsible_user
            ON responsible_user.id = responsible.ref
"""
# The historic user id is kept (``responsible_ref``) even after the user was
# deleted; the Many2one only carries existing users, so no reader ever resolves
# a missing record (L09-18).
_RESPONSIBLE_COLUMNS = """
               (caused.id IS NOT NULL OR at_time.id IS NOT NULL) AS responsible_known,
               responsible.ref AS responsible_ref,
               responsible_user.id AS responsible_id,
               CASE
                   WHEN caused.id IS NULL AND at_time.id IS NULL THEN 'unknown'
                   WHEN responsible.ref IS NULL THEN 'unassigned'
                   WHEN responsible_user.id IS NULL THEN 'removed'
                   ELSE 'assigned'
               END AS responsible_status,
               -- Native grouping key (L09-I1-01): one value per historic user,
               -- existing or deleted, plus "no responsible" and "unknown".
               CASE
                   WHEN caused.id IS NULL AND at_time.id IS NULL THEN 'unknown'
                   WHEN responsible.ref IS NULL THEN 'unassigned'
                   ELSE responsible.ref::text
               END AS responsible_group
"""

ATTENDANCE_MESSAGE_SQL = """
    WITH {ctes}
    SELECT message.id,
           message.id AS message_binding_id,
           message.channel_id,
           message.account_id,
           message.company_id,
           message.platform,
           message.direction,
           message.kind,
           message.message_at,
           message.effective_at,
           message.cycle_sequence,
           message.placement,
           {responsible_columns}
      FROM message
      {responsible_joins}
""".format(
    ctes=_PLACEMENT_CTES,
    responsible_columns=_RESPONSIBLE_COLUMNS,
    responsible_joins=_RESPONSIBLE_JOINS.format(
        alias="message",
        time="message.message_at",
        assignment_types=_sql_list(ASSIGNMENT_EVENT_TYPES),
    ),
)

# ``next_response`` is the first human response at or after each row in the total
# order ``(effective time, binding id)``: a running minimum over the cycle read
# backwards, so one ordered pass answers every episode without a self-join. The
# array is compared lexicographically; its first two items are unique.
ATTENDANCE_EPISODE_SQL = """
    WITH {ctes},
    item AS NOT MATERIALIZED (
        SELECT message.id,
               message.channel_id,
               message.account_id,
               message.company_id,
               message.platform,
               message.cycle_sequence,
               message.effective_at,
               message.caused_assignment_event_id,
               message.segment_opened_by,
               message.segment_closed_at,
               message.kind IN ('agent', 'phone') AS is_response,
               CASE WHEN message.kind IN ('agent', 'phone') THEN ARRAY[
                   EXTRACT(EPOCH FROM message.effective_at)::bigint,
                   message.id,
                   CASE message.kind WHEN 'agent' THEN 1 ELSE 2 END,
                   COALESCE(message.author_user_id, 0)
               ] END AS response_key
          FROM message
         WHERE message.placement = 'cycle'
           AND message.effective_at IS NOT NULL
           AND message.kind IN ('customer', 'agent', 'phone')
    ),
    ranked AS NOT MATERIALIZED (
        SELECT item.*,
               count(*) FILTER (WHERE item.is_response) OVER forward
                   AS responses_before,
               min(item.response_key) OVER backward AS next_response
          FROM item
        WINDOW forward AS (
                   PARTITION BY item.channel_id, item.cycle_sequence
                   ORDER BY item.effective_at, item.id
                   ROWS UNBOUNDED PRECEDING
               ),
               backward AS (
                   PARTITION BY item.channel_id, item.cycle_sequence
                   ORDER BY item.effective_at DESC, item.id DESC
                   ROWS UNBOUNDED PRECEDING
               )
    ),
    opening AS NOT MATERIALIZED (
        SELECT DISTINCT ON (ranked.channel_id, ranked.cycle_sequence,
                            ranked.responses_before)
               ranked.*
          FROM ranked
         WHERE NOT ranked.is_response
      ORDER BY ranked.channel_id, ranked.cycle_sequence, ranked.responses_before,
               ranked.effective_at, ranked.id
    ),
    episode AS NOT MATERIALIZED (
        SELECT opening.id,
               opening.channel_id,
               opening.account_id,
               opening.company_id,
               opening.platform,
               opening.cycle_sequence,
               opening.effective_at AS start_at,
               opening.caused_assignment_event_id,
               -- A customer message is placed in the segment of its sequence, so
               -- that segment is the cycle of the episode it opens.
               opening.segment_opened_by AS cycle_opened_by,
               opening.segment_closed_at AS cycle_closed_at,
               opening.next_response[2]::integer AS response_id,
               opening.next_response[1] AS response_epoch,
               opening.next_response[3] AS response_kind,
               NULLIF(opening.next_response[4], 0)::integer AS response_author_id,
               row_number() OVER (
                   PARTITION BY opening.channel_id, opening.cycle_sequence
                   ORDER BY opening.effective_at, opening.id
               ) AS ordinal
          FROM opening
    )
    SELECT episode.id,
           episode.id AS start_message_binding_id,
           episode.channel_id,
           episode.account_id,
           episode.company_id,
           episode.platform,
           episode.cycle_sequence,
           episode.cycle_opened_by,
           episode.cycle_closed_at,
           episode.start_at,
           CASE
               WHEN episode.response_id IS NOT NULL
                   THEN to_timestamp(episode.response_epoch) AT TIME ZONE 'UTC'
               ELSE episode.cycle_closed_at
           END AS end_at,
           (episode.response_epoch
               - EXTRACT(EPOCH FROM episode.start_at)::bigint)::integer
               AS wait_seconds,
           CASE
               WHEN episode.ordinal > 1 THEN FALSE
               WHEN episode.cycle_opened_by = 'baseline' THEN NULL
               ELSE TRUE
           END AS is_first,
           CASE
               WHEN episode.ordinal > 1 THEN 'subsequent'
               WHEN episode.cycle_opened_by = 'baseline' THEN 'before_history'
               ELSE 'first'
           END AS response_order,
           CASE
               WHEN episode.response_id IS NOT NULL THEN 'answered'
               WHEN episode.cycle_closed_at IS NOT NULL THEN 'closed_unanswered'
               ELSE 'pending'
           END AS outcome,
           CASE episode.response_kind
               WHEN 1 THEN 'agent'
               WHEN 2 THEN 'external_device'
           END AS response_origin,
           episode.response_id AS response_message_binding_id,
           CASE
               WHEN episode.response_kind = 1 THEN respondent_user.id
           END AS respondent_id,
           {responsible_columns}
      FROM episode
 LEFT JOIN res_users AS respondent_user
        ON respondent_user.id = episode.response_author_id
      {responsible_joins}
""".format(
    ctes=_PLACEMENT_CTES,
    responsible_columns=_RESPONSIBLE_COLUMNS,
    responsible_joins=_RESPONSIBLE_JOINS.format(
        alias="episode",
        time="episode.start_at",
        assignment_types=_sql_list(ASSIGNMENT_EVENT_TYPES),
    ),
)

# Module constants only: the statements contain no runtime value.
_CREATE_MESSAGE_VIEW = "CREATE VIEW contact_center_attendance_message AS (%s)" % (
    ATTENDANCE_MESSAGE_SQL
)
_CREATE_EPISODE_VIEW = "CREATE VIEW contact_center_attendance_episode AS (%s)" % (
    ATTENDANCE_EPISODE_SQL
)

RESPONSIBLE_STATUS = [
    ("assigned", "Known responsible"),
    ("removed", "Removed user"),
    ("unassigned", "No responsible"),
    ("unknown", "Unknown responsible"),
]


def removed_user_label(user_id):
    """Label of a deleted user: its historic id keeps deleted users apart."""

    return _("Removed user (#%s)", user_id)


def user_labels(env, user_ids):
    """Return ``{id: label}`` without ever reading a missing user (L09-18).

    Existing users the viewer may read show their name, existing users outside
    the viewer's scope a neutral "User #id", deleted users "Removed user (#id)".
    """

    user_ids = {int(user_id) for user_id in user_ids if user_id}
    users = env["res.users"].with_context(active_test=False)
    existing = set(users.sudo().browse(user_ids).exists().ids)
    visible = users.search([("id", "in", list(existing))])
    labels = {user.id: user.display_name for user in visible}
    for user_id in user_ids - existing:
        labels[user_id] = removed_user_label(user_id)
    for user_id in existing - set(labels):
        labels[user_id] = _("User #%s", user_id)
    return labels


def _responsible_labels(records):
    """Name of the responsible, "Removed user (#id)" for a deleted one."""

    labels = user_labels(records.env, records.mapped("responsible_ref"))
    for record in records:
        record.responsible_label = labels.get(record.responsible_ref) or False


def historic_responsible_refs(env):
    """Historic responsible ids of the direct conversations the viewer may read.

    Read from the ledger under its access rights and record rules, which are
    those of the episodes and messages (L09-I2-01): an agent, a supervisor
    outside the conversations or an administrator of another company never
    learns who was responsible for them, not even as a removed user.
    """

    events = env["contact.center.conversation.event"]
    if not events.check_access_rights("read", raise_exception=False):
        return []
    env.flush_all()
    query = events._where_calc(
        [("responsible_ref", ">", 0), ("conversation_type", "=", "direct")]
    )
    events._apply_ir_rules(query, "read")
    from_clause, where_clause, params = query.get_sql()
    # pylint: disable-next=sql-injection
    env.cr.execute(
        'SELECT DISTINCT "contact_center_conversation_event".responsible_ref'
        " FROM %s WHERE %s" % (from_clause, where_clause or "TRUE"),
        params,
    )
    return [row[0] for row in env.cr.fetchall()]


def _responsible_groups(model):
    """Selection of the historic responsibility keys the viewer may read.

    ``fields_get`` runs this for anyone who may describe the model, whatever
    its read access, so the choices come only from the viewer's own ledger.
    """

    labels = user_labels(model.env, historic_responsible_refs(model.env))
    return sorted(
        ((str(user_id), label) for user_id, label in labels.items()),
        key=lambda item: (item[1].lower(), item[0]),
    ) + [("unassigned", _("No responsible")), ("unknown", _("Unknown responsible"))]


class ContactCenterAttendanceMessage(models.Model):
    _name = "contact.center.attendance.message"
    _description = "Attendance Message Placement"
    _auto = False
    _order = "message_at desc, id desc"

    message_binding_id = fields.Many2one(
        "contact.center.message.binding", string="Message", readonly=True
    )
    channel_id = fields.Many2one("mail.channel", string="Conversation", readonly=True)
    account_id = fields.Many2one(
        "contact.center.account", string="Inbox", readonly=True
    )
    company_id = fields.Many2one("res.company", readonly=True)
    platform = fields.Char(string="Channel", readonly=True)
    direction = fields.Selection(
        [("inbound", "Inbound"), ("outbound", "Outbound")], readonly=True
    )
    kind = fields.Selection(
        [
            ("customer", "Customer"),
            ("agent", "Agent"),
            ("phone", "Phone reply"),
            ("automation", "Automation"),
            ("control", "Control card"),
            ("other", "Other"),
        ],
        readonly=True,
    )
    message_at = fields.Datetime(string="Message Time", readonly=True)
    effective_at = fields.Datetime(string="Effective Time", readonly=True)
    cycle_sequence = fields.Integer(string="Cycle", readonly=True, group_operator=None)
    placement = fields.Selection(
        [
            ("cycle", "In a service cycle"),
            ("archived", "Archived conversation"),
            ("closed", "Resolved conversation"),
            ("unplaced", "Phone reply not placed"),
            ("uncovered", "Before the history"),
        ],
        readonly=True,
    )
    responsible_known = fields.Boolean(readonly=True)
    responsible_ref = fields.Integer(
        string="Responsible User ID", readonly=True, group_operator=None
    )
    responsible_id = fields.Many2one(
        "res.users", string="Responsible at the Time", readonly=True
    )
    responsible_label = fields.Char(
        string="Responsible", compute="_compute_responsible_label"
    )
    responsible_status = fields.Selection(RESPONSIBLE_STATUS, readonly=True)
    responsible_group = fields.Selection(
        "_selection_responsible_group",
        readonly=True,
        groups="contact_center_base.group_contact_center_supervisor",
        help="Historic responsibility key for grouping: each user (existing or "
        "deleted), no responsible or unknown.",
    )

    @api.depends("responsible_id", "responsible_ref")
    def _compute_responsible_label(self):
        _responsible_labels(self)

    @api.model
    def _selection_responsible_group(self):
        return _responsible_groups(self)

    def init(self):
        drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(_CREATE_MESSAGE_VIEW)


class ContactCenterAttendanceEpisode(models.Model):
    _name = "contact.center.attendance.episode"
    _description = "Attendance Waiting Episode"
    _auto = False
    _order = "start_at desc, id desc"
    _rec_name = "start_at"

    start_message_binding_id = fields.Many2one(
        "contact.center.message.binding", string="Opening Message", readonly=True
    )
    channel_id = fields.Many2one("mail.channel", string="Conversation", readonly=True)
    account_id = fields.Many2one(
        "contact.center.account", string="Inbox", readonly=True
    )
    company_id = fields.Many2one("res.company", readonly=True)
    platform = fields.Char(string="Channel", readonly=True)
    cycle_sequence = fields.Integer(string="Cycle", readonly=True, group_operator=None)
    cycle_opened_by = fields.Selection(
        [
            ("created", "Created"),
            ("baseline", "Before the history"),
            ("reopened", "Reopened"),
            ("unarchived", "Unarchived"),
        ],
        readonly=True,
    )
    cycle_closed_at = fields.Datetime(readonly=True)
    start_at = fields.Datetime(string="Start", readonly=True)
    end_at = fields.Datetime(string="End", readonly=True)
    # Elapsed time; 0 is a valid measurement and empty means "not measured".
    # Never summed or averaged in views: the report uses medians/percentiles.
    wait_seconds = fields.Integer(
        string="Wait (seconds)", readonly=True, group_operator=None
    )
    is_first = fields.Boolean(string="First Response", readonly=True)
    response_order = fields.Selection(
        [
            ("first", "First response"),
            ("subsequent", "Subsequent response"),
            ("before_history", "Cycle before the history"),
        ],
        readonly=True,
    )
    outcome = fields.Selection(
        [
            ("answered", "Answered"),
            ("pending", "Pending"),
            ("closed_unanswered", "Closed without response"),
        ],
        readonly=True,
    )
    response_origin = fields.Selection(
        [("agent", "Agent"), ("external_device", "Phone")], readonly=True
    )
    response_message_binding_id = fields.Many2one(
        "contact.center.message.binding", string="Response", readonly=True
    )
    respondent_id = fields.Many2one("res.users", readonly=True)
    responsible_known = fields.Boolean(readonly=True)
    responsible_ref = fields.Integer(
        string="Responsible User ID", readonly=True, group_operator=None
    )
    responsible_id = fields.Many2one(
        "res.users", string="Responsible at Start", readonly=True
    )
    responsible_label = fields.Char(
        string="Responsible", compute="_compute_responsible_label"
    )
    responsible_status = fields.Selection(RESPONSIBLE_STATUS, readonly=True)
    responsible_group = fields.Selection(
        "_selection_responsible_group",
        readonly=True,
        groups="contact_center_base.group_contact_center_supervisor",
        help="Historic responsibility key for grouping: each user (existing or "
        "deleted), no responsible or unknown.",
    )

    @api.depends("responsible_id", "responsible_ref")
    def _compute_responsible_label(self):
        _responsible_labels(self)

    @api.model
    def _selection_responsible_group(self):
        return _responsible_groups(self)

    def init(self):
        drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(_CREATE_EPISODE_VIEW)


_GROUP_BY = ("account", "platform", "responsible", "none")
_UNKNOWN_KEY = -1
_UNASSIGNED_KEY = 0
_EPISODE_AGGREGATES = """
    count(*) AS episodes,
    count(*) FILTER (WHERE {t}.outcome = 'answered') AS answered,
    count(*) FILTER (WHERE {t}.outcome = 'pending') AS pending,
    count(*) FILTER (WHERE {t}.outcome = 'closed_unanswered') AS closed_unanswered,
    count(*) FILTER (WHERE {t}.responsible_known) AS responsible_known,
    count(*) FILTER (WHERE {t}.is_first IS NULL) AS before_history,
    count(*) FILTER (WHERE {first}) AS first_measured,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY {t}.wait_seconds)
        FILTER (WHERE {first}) AS first_median,
    percentile_cont(0.9) WITHIN GROUP (ORDER BY {t}.wait_seconds)
        FILTER (WHERE {first}) AS first_p90,
    count(*) FILTER (WHERE {next}) AS subsequent_measured,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY {t}.wait_seconds)
        FILTER (WHERE {next}) AS subsequent_median,
    percentile_cont(0.9) WITHIN GROUP (ORDER BY {t}.wait_seconds)
        FILTER (WHERE {next}) AS subsequent_p90
""".format(
    t='"contact_center_attendance_episode"',
    first=(
        "\"contact_center_attendance_episode\".outcome = 'answered' "
        'AND "contact_center_attendance_episode".is_first IS TRUE'
    ),
    next=(
        "\"contact_center_attendance_episode\".outcome = 'answered' "
        'AND "contact_center_attendance_episode".is_first IS FALSE'
    ),
)
_MESSAGE_AGGREGATES = """
    count(*) FILTER (WHERE {t}.kind = 'customer') AS inbound,
    count(*) FILTER (
        WHERE {t}.kind = 'phone'
           OR ({t}.kind IN ('agent', 'automation') AND {t}.effective_at IS NOT NULL)
    ) AS outbound,
    count(*) FILTER (WHERE {t}.kind = 'phone' AND {t}.placement = 'unplaced')
        AS unplaced_replies,
    count(*) FILTER (WHERE {t}.kind = 'customer' AND {t}.placement = 'archived')
        AS archived_inbound
""".format(
    t='"contact_center_attendance_message"'
)
_EVENT_AGGREGATES = "count(*) AS events"


class ContactCenterAttendanceReport(models.AbstractModel):
    _name = "contact.center.attendance.report"
    _description = "Contact Center Attendance Report"

    @api.model
    def _check_report_access(self):
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(_("Only supervisors can open the attendance report."))

    @api.model
    def _normalize_filters(self, filters):
        filters = filters if isinstance(filters, dict) else {}

        def ids(name):
            value = filters.get(name) or []
            if not isinstance(value, list) or any(
                type(item) is not int or item <= 0  # noqa: E721 - reject bool IDs
                for item in value
            ):
                raise ValidationError(_("Invalid report filter: %s", name))
            return sorted(set(value))

        def day(name, default):
            value = filters.get(name) or default
            try:
                return fields.Date.to_date(value)
            except (TypeError, ValueError) as error:
                raise ValidationError(_("Invalid report date: %s", name)) from error

        date_to = day("date_to", fields.Date.context_today(self))
        date_from = day("date_from", date_to - datetime.timedelta(days=29))
        if not date_from or not date_to:
            raise ValidationError(_("Choose the report period."))
        if date_from > date_to or (date_to - date_from).days > 366:
            raise ValidationError(_("Choose a period of up to one year."))
        platforms = filters.get("platforms") or []
        if not isinstance(platforms, list) or any(
            not isinstance(item, str) or not item or len(item) > 64
            for item in platforms
        ):
            raise ValidationError(_("Invalid report filter: %s", "platforms"))
        group_by = filters.get("group_by") or "account"
        if group_by not in _GROUP_BY:
            raise ValidationError(_("Invalid report grouping."))
        return {
            "date_from": date_from,
            "date_to": date_to,
            "account_ids": ids("account_ids"),
            "responsible_ids": ids("responsible_ids"),
            "platforms": sorted(set(platforms)),
            "group_by": group_by,
        }

    @api.model
    def _period_bounds(self, date_from, date_to):
        """Return UTC ``[start, end)`` covering whole local days of the viewer."""

        timezone = pytz.timezone(self.env.user.tz or "UTC")
        start = timezone.localize(datetime.datetime.combine(date_from, datetime.time()))
        end = timezone.localize(
            datetime.datetime.combine(
                date_to + datetime.timedelta(days=1), datetime.time()
            )
        )
        return (
            start.astimezone(pytz.utc).replace(tzinfo=None),
            end.astimezone(pytz.utc).replace(tzinfo=None),
        )

    @api.model
    def _dimension_domain(self, filters, platform_field="platform"):
        domain = []
        if filters["account_ids"]:
            domain.append(("account_id", "in", filters["account_ids"]))
        if filters["platforms"]:
            domain.append((platform_field, "in", filters["platforms"]))
        return domain

    @api.model
    def _group_expression(self, table, group_by, responsible_column=None):
        column = '"%s".%%s' % table
        if table == "contact_center_conversation_event":
            # The ledger derives its inbox from the unique channel binding.
            if group_by == "account":
                return "event_binding.account_id"
            if group_by == "platform":
                return "event_account.platform"
        if group_by == "account":
            return column % "account_id"
        if group_by == "platform":
            return column % "platform"
        if responsible_column:
            # A ledger event always knows who held the conversation.
            return "COALESCE(NULLIF(%s, 0), %d)" % (
                column % responsible_column,
                _UNASSIGNED_KEY,
            )
        return "CASE WHEN %s THEN COALESCE(%s, %d) ELSE %d END" % (
            column % "responsible_known",
            column % "responsible_ref",
            _UNASSIGNED_KEY,
            _UNKNOWN_KEY,
        )

    @api.model
    def _grouped_rows(
        self, model_name, domain, group_by, aggregates, responsible_column=None
    ):
        """Aggregate ``domain`` under the viewer's record rules.

        Grouping sets compute the total over the raw rows, so a total median or
        percentile is never derived from the per-group values.
        """

        # Archived bindings still own their conversation's history.
        model = self.env[model_name].with_context(active_test=False)
        model.check_access_rights("read")
        query = model._where_calc(domain)
        model._apply_ir_rules(query, "read")
        from_clause, where_clause, params = query.get_sql()
        where_clause = where_clause or "TRUE"
        if model._table == "contact_center_conversation_event":
            from_clause += (
                " LEFT JOIN contact_center_channel_binding AS event_binding"
                ' ON event_binding.channel_id = "contact_center_conversation_event"'
                ".channel_id"
                " LEFT JOIN contact_center_account AS event_account"
                " ON event_account.id = event_binding.account_id"
            )
        if group_by == "none":
            # pylint: disable-next=sql-injection
            self.env.cr.execute(
                "SELECT %s FROM %s WHERE %s" % (aggregates, from_clause, where_clause),
                params,
            )
            return {}, self.env.cr.dictfetchone()
        group = self._group_expression(model._table, group_by, responsible_column)
        # pylint: disable-next=sql-injection
        self.env.cr.execute(
            """
            SELECT GROUPING(%(group)s) = 1 AS is_total,
                   %(group)s AS group_key,
                   %(aggregates)s
              FROM %(from)s
             WHERE %(where)s
          GROUP BY GROUPING SETS ((%(group)s), ())
            """
            % {
                "group": group,
                "aggregates": aggregates,
                "from": from_clause,
                "where": where_clause,
            },
            params,
        )
        rows = {}
        total = {}
        for row in self.env.cr.dictfetchall():
            if row.pop("is_total"):
                total = row
            else:
                rows[row["group_key"]] = row
        return rows, total

    @api.model
    def _candidate_channel_ids(self, bounds):
        """Return every conversation that can hold a message dated in the period.

        ``contact_center_last_message_at`` (indexed) is the newest message date of
        a conversation, so a conversation whose value is older than the period
        start has no message in it. Filtering the views by these conversations
        is exact for episodes and messages of the period and lets the channel
        condition reach the base tables instead of computing all history.
        """

        self.env["mail.channel"].flush_model(
            ["channel_type", "contact_center_last_message_at"]
        )
        self.env.cr.execute(
            """
            SELECT id
              FROM mail_channel
             WHERE channel_type = 'contact_center'
               AND (contact_center_last_message_at >= %s
                    OR contact_center_last_message_at IS NULL)
            """,
            [bounds[0]],
        )
        return [row[0] for row in self.env.cr.fetchall()]

    @api.model
    def _episode_metrics(self, bounds, filters):
        domain = [("start_at", ">=", bounds[0]), ("start_at", "<", bounds[1])]
        domain.append(("channel_id", "in", filters["candidate_channel_ids"]))
        domain += self._dimension_domain(filters)
        if filters["responsible_ids"]:
            domain.append(("responsible_ref", "in", filters["responsible_ids"]))
        return self._grouped_rows(
            "contact.center.attendance.episode",
            domain,
            filters["group_by"],
            _EPISODE_AGGREGATES,
        )

    @api.model
    def _message_metrics(self, bounds, filters):
        domain = [("message_at", ">=", bounds[0]), ("message_at", "<", bounds[1])]
        domain.append(("channel_id", "in", filters["candidate_channel_ids"]))
        domain += self._dimension_domain(filters)
        if filters["responsible_ids"]:
            domain.append(("responsible_ref", "in", filters["responsible_ids"]))
        return self._grouped_rows(
            "contact.center.attendance.message",
            domain,
            filters["group_by"],
            _MESSAGE_AGGREGATES,
        )

    @api.model
    def _ledger_domain(self, filters):
        """Direct conversations of the selected inboxes/channels, as the views.

        The event fields search through a sub-select on the channel binding: a
        domain path through ``channel_id`` would apply the viewer's conversation
        rules (administrators are not members of every channel), while only the
        ledger's own rules must decide.
        """

        domain = [("conversation_type", "=", "direct")]
        if filters["account_ids"]:
            domain.append(("account_id", "in", filters["account_ids"]))
        if filters["platforms"]:
            domain.append(("platform", "in", filters["platforms"]))
        return domain

    @api.model
    def _event_metrics(self, bounds, filters, event_type):
        """Count transfers or reopenings from the lifecycle ledger.

        A transfer belongs to the responsible who held the conversation when it
        happened; a reopening to the responsible of the reopened conversation.
        """

        domain = [
            ("occurred_at", ">=", bounds[0]),
            ("occurred_at", "<", bounds[1]),
            ("event_type", "=", event_type),
        ] + self._ledger_domain(filters)
        responsible_column = "responsible_ref"
        if event_type == "assigned":
            # An empty user reference is stored as 0 (or NULL by the migration).
            domain.append(("previous_responsible_ref", ">", 0))
            responsible_column = "previous_responsible_ref"
        if filters["responsible_ids"]:
            domain.append((responsible_column, "in", filters["responsible_ids"]))
        return self._grouped_rows(
            "contact.center.conversation.event",
            domain,
            filters["group_by"],
            _EVENT_AGGREGATES,
            responsible_column=responsible_column,
        )

    @api.model
    def _publication_time(self):
        """When the history was published: the first ``baseline`` event.

        ``None`` on an installation that never had conversations before the
        ledger existed, whose history is complete (L09-R5).
        """

        self.env["contact.center.conversation.event"].flush_model(
            ["event_type", "occurred_at"]
        )
        self.env.cr.execute(
            "SELECT min(occurred_at) FROM contact_center_conversation_event "
            "WHERE event_type = 'baseline'"
        )
        return self.env.cr.fetchone()[0]

    @api.model
    def _partial_history(self, bounds, filters):
        """Whether conversations in scope have no history for part of the period.

        Only conversations that existed at the publication lack history, up to
        their ``baseline``; a later inbox or conversation is complete from its
        creation, whatever the period.
        """

        return bool(
            self.env["contact.center.conversation.event"]
            .with_context(active_test=False)
            .search_count(
                self._ledger_domain(filters)
                + [("event_type", "=", "baseline"), ("occurred_at", ">", bounds[0])],
                limit=1,
            )
        )

    @api.model
    def _group_label(self, group_by, key):
        if group_by == "none":
            return _("All conversations")
        if key is None:
            return _("Not identified")
        if group_by == "account":
            account = (
                self.env["contact.center.account"]
                .sudo()
                .with_context(active_test=False)
                .browse(key)
                .exists()
            )
            return account.display_name if account else _("Not identified")
        if group_by == "platform":
            return key
        if key == _UNKNOWN_KEY:
            return _("Unknown responsible")
        if key == _UNASSIGNED_KEY:
            return _("No responsible")
        # A deleted user keeps its historic key and counts (L09-18).
        return user_labels(self.env, [key])[key]

    @api.model
    def _report_row(self, key, label, episode, message, transfers, reopenings):
        episode = episode or {}
        message = message or {}
        episodes = episode.get("episodes") or 0

        def metric(prefix):
            measured = episode.get("%s_measured" % prefix) or 0
            return {
                "measured": measured,
                "median": episode.get("%s_median" % prefix) if measured else None,
                "p90": episode.get("%s_p90" % prefix) if measured else None,
            }

        known = episode.get("responsible_known") or 0
        return {
            "key": key,
            "label": label,
            "episodes": episodes,
            "answered": episode.get("answered") or 0,
            "pending": episode.get("pending") or 0,
            "closed_unanswered": episode.get("closed_unanswered") or 0,
            "before_history": episode.get("before_history") or 0,
            "responsible_known": known,
            "responsible_known_ratio": known / episodes if episodes else None,
            "first_response": metric("first"),
            "subsequent_response": metric("subsequent"),
            "transfers": (transfers or {}).get("events") or 0,
            "reopenings": (reopenings or {}).get("events") or 0,
            "inbound": message.get("inbound") or 0,
            "outbound": message.get("outbound") or 0,
            "unplaced_replies": message.get("unplaced_replies") or 0,
            "archived_inbound": message.get("archived_inbound") or 0,
        }

    @api.model
    def _report_options(self):
        """Filter choices: current ones, and whoever still owns readable history.

        An archived inbox, or a former responsible that was archived, deleted
        or lost its role, keeps its history in the report and stays selectable;
        both come from the viewer's own records (L09-I2-03).
        """

        accounts = (
            self.env["contact.center.account"]
            .with_context(active_test=False)
            .search([], order="active desc, name, id")
        )
        agent_group = self.env.ref("contact_center_base.group_contact_center_agent")
        users = self.env["res.users"].search(
            [("groups_id", "in", agent_group.ids), ("share", "=", False)]
        )
        labels = user_labels(
            self.env, set(users.ids) | set(historic_responsible_refs(self.env))
        )
        return {
            "accounts": [
                {
                    "id": account.id,
                    "name": account.display_name
                    if account.active
                    else _("%s (archived)", account.display_name),
                }
                for account in accounts
            ],
            "platforms": sorted(set(accounts.mapped("platform"))),
            "responsibles": [
                {"id": user_id, "name": labels[user_id]}
                for user_id in sorted(
                    labels, key=lambda user_id: (labels[user_id].lower(), user_id)
                )
            ],
        }

    @api.model
    def get_report(self, filters=None):
        """Return attendance metrics per group, each with its coverage.

        Medians and 90th percentiles are ``percentile_cont`` over the episodes of
        each group, and over all filtered episodes for the total; an absent
        measurement is ``None`` ("sem dados"), never zero. Waiting times are
        elapsed time because no business calendar is configured.
        """

        self._check_report_access()
        filters = self._normalize_filters(filters)
        bounds = self._period_bounds(filters["date_from"], filters["date_to"])
        self.env.flush_all()
        filters["candidate_channel_ids"] = self._candidate_channel_ids(bounds)
        episodes, episode_total = self._episode_metrics(bounds, filters)
        messages, message_total = self._message_metrics(bounds, filters)
        transfers, transfer_total = self._event_metrics(bounds, filters, "assigned")
        reopenings, reopening_total = self._event_metrics(bounds, filters, "reopened")
        group_by = filters["group_by"]
        keys = set(episodes) | set(messages) | set(transfers) | set(reopenings)
        if group_by in ("account", "platform"):
            # A selected (or visible) inbox without data still shows "sem dados".
            domain = self._dimension_domain(filters, platform_field="platform")
            domain = [
                ("id" if name == "account_id" else name, operator, value)
                for name, operator, value in domain
            ]
            # A selected archived inbox is shown as well (L09-I2-03).
            accounts = (
                self.env["contact.center.account"]
                .with_context(active_test=not filters["account_ids"])
                .search(domain)
            )
            keys |= set(
                accounts.ids if group_by == "account" else accounts.mapped("platform")
            )
        groups = [
            self._report_row(
                key,
                self._group_label(group_by, key),
                episodes.get(key),
                messages.get(key),
                transfers.get(key),
                reopenings.get(key),
            )
            for key in keys
        ]
        groups.sort(key=lambda row: (row["key"] is None, str(row["label"]).lower()))
        history_since = self._publication_time()
        return {
            "schema_version": SCHEMA_VERSION,
            "filters": {
                "date_from": fields.Date.to_string(filters["date_from"]),
                "date_to": fields.Date.to_string(filters["date_to"]),
                "account_ids": filters["account_ids"],
                "responsible_ids": filters["responsible_ids"],
                "platforms": filters["platforms"],
                "group_by": group_by,
            },
            "period": {
                "start": fields.Datetime.to_string(bounds[0]),
                "end": fields.Datetime.to_string(bounds[1]),
            },
            "time_basis": "elapsed",
            "history_since": (
                fields.Datetime.to_string(history_since) if history_since else False
            ),
            "partial_history": self._partial_history(bounds, filters),
            "groups": groups,
            "total": self._report_row(
                None,
                _("Total"),
                episode_total,
                message_total,
                transfer_total,
                reopening_total,
            ),
            "options": self._report_options(),
        }
