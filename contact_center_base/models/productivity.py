import datetime
import hashlib
import json
import math
import uuid

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.osv import expression
from odoo.tools import html2plaintext
from odoo.tools.mail import plaintext2html

from odoo.addons.queue_job.exception import RetryableJobError

from ..services.dto import SCHEMA_VERSION

# Productivity projections are a bounded concern shared by three Odoo core
# models.  Keeping them together avoids growing the message/channel aggregate
# modules and makes the service token and invariants auditable as one unit.
# pylint: disable=consider-merging-classes-inherited

_PRODUCTIVITY_SERVICE_TOKEN = object()
_PRODUCTIVITY_NOTIFICATION_SUPPRESSION_TOKEN = object()
_ACTIVE_QUEUE_JOB_STATES = (
    "pending",
    "enqueued",
    "started",
    "wait_dependencies",
)
_MAX_NOTE_CHARS = 65536
_MAX_SCHEDULED_BODY_CHARS = 65536
_MIN_SCHEDULE_DELAY_SECONDS = 60
_MAX_SCHEDULE_DELAY_DAYS = 365
_RECOVERY_GRACE_SECONDS = 300
_RECOVERY_LIMIT = 100
_SCHEDULED_HISTORY_LIMIT = 50
_QUICK_REPLY_SCOPE_PRIORITY = {
    "company": 0,
    "team": 1,
    "account": 2,
    "channel": 3,
}

_ACTIVITY_TARGET_FIELDS = frozenset(("res_id", "res_model", "res_model_id"))

_INTERNAL_NOTE_PROTECTED_MESSAGE_FIELDS = frozenset(
    (
        "attachment_ids",
        "author_guest_id",
        "author_id",
        "body",
        "email_from",
        "message_type",
        "model",
        "parent_id",
        "res_id",
        "subtype_id",
    )
)


def _canonical_uuid(value, label):
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValidationError(_("The %s must be a UUID.", label)) from error


def _plain_text(value, label, maximum, required=True):
    if not isinstance(value, str):
        raise ValidationError(_("The %s must be text.", label))
    clean = value.strip()
    if required and not clean:
        raise UserError(_("The %s cannot be empty.", label))
    if len(clean) > maximum:
        raise UserError(
            _("The %(label)s exceeds the %(limit)s character limit.")
            % {"label": label, "limit": maximum}
        )
    return clean


def _payload_sha256(values):
    canonical = json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MailChannelProductivity(models.Model):
    _inherit = "mail.channel"

    contact_center_productivity_revision = fields.Integer(
        default=0,
        required=True,
        copy=False,
        readonly=True,
        help="Monotonic concurrency fence for Contact Center productivity intents.",
    )

    def write(self, values):
        if (
            "contact_center_productivity_revision" in values
            and self.env.context.get("contact_center_productivity_service_token")
            is not _PRODUCTIVITY_SERVICE_TOKEN
        ):
            raise AccessError(_("The productivity revision is managed internally."))
        return super().write(values)

    def _contact_center_reconcile_members(
        self, partner_ids=None, guest_ids=None, allow_empty=False
    ):
        """Reassign open case work and revoke stale internal followers."""

        result = super()._contact_center_reconcile_members(
            partner_ids=partner_ids,
            guest_ids=guest_ids,
            allow_empty=allow_empty,
        )
        allowed_partner_ids = set(partner_ids or [])
        user_model = self.env["res.users"].sudo().with_context(active_test=False)
        for channel in self:
            cases = (
                self.env["contact.center.case"]
                .sudo()
                .with_context(active_test=False)
                .search([("channel_id", "=", channel.id)])
            )
            if not cases:
                continue
            allowed_users = user_model.search(
                [
                    ("partner_id", "in", sorted(allowed_partner_ids)),
                    ("active", "=", True),
                    ("share", "=", False),
                    ("company_ids", "in", channel.contact_center_company_id.id),
                ],
                order="id",
            )
            activities = (
                self.env["mail.activity"]
                .sudo()
                .search(
                    [
                        ("res_model", "=", "contact.center.case"),
                        ("res_id", "in", cases.ids),
                    ]
                )
            )
            unauthorized_activities = activities.filtered(
                lambda activity: activity.user_id not in allowed_users
            )
            if unauthorized_activities and not allowed_users:
                raise ValidationError(
                    _(
                        "Complete or reassign the open follow-ups before removing "
                        "the last attendant from this inbox."
                    )
                )
            for activity in unauthorized_activities:
                case = cases.filtered(lambda item: item.id == activity.res_id)[:1]
                preferred = (
                    case.responsible_user_id
                    | channel.contact_center_responsible_id
                    | channel.contact_center_owner_user_id
                ).filtered(lambda user: user in allowed_users)[:1]
                replacement = preferred or allowed_users[:1]
                activity.write({"user_id": replacement.id})

            # Only internal-user followers are access projections.  A business
            # contact deliberately following a case is not removed here.
            internal_followers = cases.message_follower_ids.filtered(
                lambda follower: bool(
                    follower.partner_id.with_context(
                        active_test=False
                    ).user_ids.filtered(lambda user: not user.share)
                )
            )
            internal_followers.filtered(
                lambda follower: follower.partner_id.id not in allowed_partner_ids
            ).sudo().unlink()
        return result


class MailChannelMemberProductivity(models.Model):
    _inherit = "mail.channel.member"

    def _compute_message_unread(self):
        """Exclude ledger-backed internal notes from operational unread counts."""

        super()._compute_message_unread()
        members = self.browse(self.ids).filtered(
            lambda member: member.channel_id.channel_type == "contact_center"
        )
        if not members:
            return
        self.env["contact.center.internal.note.request"].flush_model(["message_id"])
        members.flush_recordset(["channel_id", "seen_message_id"])
        self.env.cr.execute(
            """
            SELECT member.id, COUNT(message.id)
              FROM mail_channel_member AS member
              JOIN mail_message AS message
                ON message.model = 'mail.channel'
               AND message.res_id = member.channel_id
              JOIN contact_center_internal_note_request AS note_request
                ON note_request.message_id = message.id
             WHERE member.id = ANY(%s)
               AND (
                    member.seen_message_id IS NULL
                    OR message.id > member.seen_message_id
               )
          GROUP BY member.id
            """,
            [members.ids],
        )
        internal_note_count = dict(self.env.cr.fetchall())
        for member in members:
            member.message_unread_counter = max(
                0,
                (member.message_unread_counter or 0)
                - internal_note_count.get(member.id, 0),
            )


class MailMessageProductivity(models.Model):
    _inherit = "mail.message"

    def _contact_center_internal_note_requests(self):
        if not self.ids:
            return self.env["contact.center.internal.note.request"]
        return (
            self.env["contact.center.internal.note.request"]
            .sudo()
            .search([("message_id", "in", self.ids)])
        )

    def write(self, values):
        if (
            _INTERNAL_NOTE_PROTECTED_MESSAGE_FIELDS & set(values)
            and self.env.context.get("contact_center_productivity_service_token")
            is not _PRODUCTIVITY_SERVICE_TOKEN
            and self._contact_center_internal_note_requests()
        ):
            raise AccessError(_("Contact Center internal note messages are immutable."))
        return super().write(values)

    def unlink(self):
        if (
            self.env.context.get("contact_center_productivity_service_token")
            is not _PRODUCTIVITY_SERVICE_TOKEN
            and self._contact_center_internal_note_requests()
        ):
            raise AccessError(_("Contact Center internal note messages are immutable."))
        return super().unlink()


class ContactCenterInternalNoteRequest(models.Model):
    _name = "contact.center.internal.note.request"
    _description = "Contact Center Internal Note Request"
    _order = "id desc"

    channel_id = fields.Many2one(
        "mail.channel", required=True, index=True, ondelete="cascade"
    )
    company_id = fields.Many2one(
        related="channel_id.contact_center_company_id",
        store=True,
        readonly=True,
        index=True,
    )
    message_id = fields.Many2one(
        "mail.message", required=True, index=True, ondelete="restrict"
    )
    requested_by_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="restrict"
    )
    ui_request_id = fields.Char(required=True, index=True, readonly=True, copy=False)
    body_sha256 = fields.Char(required=True, readonly=True, copy=False)
    message_body_sha256 = fields.Char(required=True, readonly=True, copy=False)

    _sql_constraints = [
        (
            "channel_request_unique",
            "unique(channel_id, ui_request_id)",
            "This internal note request was already processed.",
        ),
        (
            "message_unique",
            "unique(message_id)",
            "An internal message can belong to only one note request.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("contact_center_productivity_service_token")
            is not _PRODUCTIVITY_SERVICE_TOKEN
        ):
            raise AccessError(
                _("Internal note requests are created by the UI service.")
            )
        return super().create(vals_list)

    def write(self, _values):  # pylint: disable=method-required-super
        raise AccessError(_("Internal note request history is immutable."))

    def unlink(self):
        if (
            self.env.context.get("contact_center_productivity_service_token")
            is not _PRODUCTIVITY_SERVICE_TOKEN
        ):
            raise AccessError(_("Internal note request history is immutable."))
        return super().unlink()


class ContactCenterFollowupRequest(models.Model):
    _name = "contact.center.followup.request"
    _description = "Contact Center Follow-up Request"
    _order = "id desc"

    channel_id = fields.Many2one(
        "mail.channel", required=True, index=True, ondelete="cascade"
    )
    company_id = fields.Many2one(
        related="channel_id.contact_center_company_id",
        store=True,
        readonly=True,
        index=True,
    )
    case_id = fields.Many2one(
        "contact.center.case", required=True, index=True, ondelete="cascade"
    )
    activity_id = fields.Many2one(
        "mail.activity", index=True, readonly=True, copy=False, ondelete="set null"
    )
    activity_record_id = fields.Integer(required=True, index=True, readonly=True)
    requested_by_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="restrict"
    )
    ui_request_id = fields.Char(index=True, readonly=True, copy=False)
    schedule_payload_sha256 = fields.Char(readonly=True, copy=False)
    activity_snapshot_json = fields.Json(required=True, default=dict, copy=False)
    state = fields.Selection(
        [
            ("active", "Active"),
            ("completed", "Completed"),
            ("closed", "Closed Outside Contact Center"),
        ],
        required=True,
        default="active",
        index=True,
        readonly=True,
        copy=False,
    )
    completion_request_id = fields.Char(index=True, readonly=True, copy=False)
    completion_payload_sha256 = fields.Char(readonly=True, copy=False)
    completion_snapshot_json = fields.Json(default=dict, copy=False)
    completed_by_id = fields.Many2one(
        "res.users", index=True, readonly=True, copy=False, ondelete="set null"
    )
    completed_at = fields.Datetime(index=True, readonly=True, copy=False)
    closed_at = fields.Datetime(index=True, readonly=True, copy=False)

    _sql_constraints = [
        (
            "activity_record_unique",
            "unique(activity_record_id)",
            "A follow-up activity can have only one Contact Center receipt.",
        ),
        (
            "channel_schedule_request_unique",
            "unique(channel_id, ui_request_id)",
            "This follow-up request was already processed.",
        ),
        (
            "channel_completion_request_unique",
            "unique(channel_id, completion_request_id)",
            "This follow-up completion request was already processed.",
        ),
        (
            "activity_record_positive",
            "check(activity_record_id > 0)",
            "A follow-up activity receipt requires a valid activity ID.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("contact_center_productivity_service_token")
            is not _PRODUCTIVITY_SERVICE_TOKEN
        ):
            raise AccessError(_("Follow-up receipts are created by the UI service."))
        return super().create(vals_list)

    def write(self, values):
        if (
            self.env.context.get("contact_center_productivity_service_token")
            is not _PRODUCTIVITY_SERVICE_TOKEN
        ):
            raise AccessError(_("Follow-up receipt history is managed internally."))
        return super().write(values)

    def unlink(self):
        if (
            self.env.context.get("contact_center_productivity_service_token")
            is not _PRODUCTIVITY_SERVICE_TOKEN
        ):
            raise AccessError(_("Follow-up receipt history is immutable."))
        return super().unlink()

    def _service_write(self, values):
        return (
            self.sudo()
            .with_context(
                contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
            )
            .write(values)
        )


class ContactCenterQuickReplyBinding(models.Model):
    """Scope native ``mail.shortcode`` content to one operational audience."""

    _name = "contact.center.quick.reply.binding"
    _description = "Contact Center Quick Reply Binding"
    _rec_name = "shortcode_id"
    _order = "sequence, id"
    _check_company_auto = True

    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    shortcode_id = fields.Many2one(
        "mail.shortcode",
        string="Native Quick Reply",
        required=True,
        index=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company,
        ondelete="restrict",
    )
    scope = fields.Selection(
        [
            ("company", "Company"),
            ("team", "Team"),
            ("account", "Inbox"),
            ("channel", "Conversation"),
        ],
        required=True,
        default="company",
        index=True,
    )
    team_id = fields.Many2one(
        "contact.center.team",
        string="Team",
        index=True,
        check_company=True,
        ondelete="cascade",
        domain="[('company_id', '=', company_id)]",
    )
    account_id = fields.Many2one(
        "contact.center.account",
        string="Inbox",
        index=True,
        check_company=True,
        ondelete="cascade",
        domain="[('company_id', '=', company_id)]",
    )
    channel_id = fields.Many2one(
        "mail.channel",
        string="Conversation",
        index=True,
        ondelete="cascade",
        domain=(
            "[('channel_type', '=', 'contact_center'), "
            "('contact_center_company_id', '=', company_id)]"
        ),
    )
    scope_key = fields.Char(
        compute="_compute_scope_key",
        precompute=True,
        store=True,
        required=True,
        index=True,
        readonly=True,
        copy=False,
    )

    _sql_constraints = [
        (
            "scope_key_unique",
            "unique(scope_key)",
            "This native quick reply is already bound to the selected scope.",
        ),
        (
            "scope_target_exact",
            "CHECK((scope = 'company' AND team_id IS NULL AND account_id IS NULL "
            "AND channel_id IS NULL) OR (scope = 'team' AND team_id IS NOT NULL "
            "AND account_id IS NULL AND channel_id IS NULL) OR (scope = 'account' "
            "AND team_id IS NULL AND account_id IS NOT NULL AND channel_id IS NULL) "
            "OR (scope = 'channel' AND team_id IS NULL AND account_id IS NULL "
            "AND channel_id IS NOT NULL))",
            "Select exactly the target required by the quick reply scope.",
        ),
    ]

    @api.depends(
        "shortcode_id", "company_id", "scope", "team_id", "account_id", "channel_id"
    )
    def _compute_scope_key(self):
        for binding in self:
            target = {
                "company": binding.company_id.id,
                "team": binding.team_id.id,
                "account": binding.account_id.id,
                "channel": binding.channel_id.id,
            }.get(binding.scope, 0)
            binding.scope_key = "%s:%s:%s:%s" % (
                binding.company_id.id or 0,
                binding.scope or "invalid",
                target or 0,
                binding.shortcode_id.id or 0,
            )

    @api.onchange("scope")
    def _onchange_scope(self):
        for binding in self:
            if binding.scope != "team":
                binding.team_id = False
            if binding.scope != "account":
                binding.account_id = False
            if binding.scope != "channel":
                binding.channel_id = False

    @api.constrains(
        "shortcode_id", "company_id", "scope", "team_id", "account_id", "channel_id"
    )
    def _check_scope(self):
        for binding in self:
            selected = {
                "team": binding.team_id,
                "account": binding.account_id,
                "channel": binding.channel_id,
            }
            expected = selected.get(binding.scope)
            populated = [name for name, record in selected.items() if record]
            if binding.scope == "company":
                if populated:
                    raise ValidationError(
                        _("A company quick reply cannot target a narrower scope.")
                    )
            elif not expected or populated != [binding.scope]:
                raise ValidationError(
                    _("Select exactly the target required by the quick reply scope.")
                )
            if binding.team_id and binding.team_id.company_id != binding.company_id:
                raise ValidationError(
                    _("The quick reply team belongs to another company.")
                )
            if (
                binding.account_id
                and binding.account_id.company_id != binding.company_id
            ):
                raise ValidationError(
                    _("The quick reply inbox belongs to another company.")
                )
            if binding.channel_id and (
                binding.channel_id.channel_type != "contact_center"
                or binding.channel_id.contact_center_company_id != binding.company_id
            ):
                raise ValidationError(
                    _("The quick reply conversation belongs to another scope.")
                )


class MailActivityProductivity(models.Model):
    _inherit = "mail.activity"

    @api.model
    def _contact_center_target_from_values(self, values, activity=None):
        """Return the prospective CC case id, or ``False`` for another model."""

        model_name = activity.res_model if activity else False
        model_name_from_id = False
        if "res_model_id" in values:
            model_id = values.get("res_model_id")
            model = self.env["ir.model"]
            if type(model_id) is int and model_id > 0:  # noqa: E721
                # ``res_model_id`` is a technical foreign key.  Ordinary Contact
                # Center agents intentionally have no read ACL on ``ir.model``;
                # resolving only its model name under sudo does not elevate access
                # to the target record, which is fenced and checked below.
                model = model.sudo().browse(model_id).exists()
            model_name_from_id = model.model if model else False
            model_name = model_name_from_id
        if "res_model" in values:
            explicit_model_name = values.get("res_model") or False
            if "res_model_id" in values and explicit_model_name != model_name_from_id:
                raise ValidationError(
                    _("The activity model identifier and model name do not match.")
                )
            if "res_model_id" not in values:
                model_name = explicit_model_name
        case_id = values.get("res_id") if "res_id" in values else False
        if activity and "res_id" not in values:
            case_id = activity.res_id
        if model_name != "contact.center.case":
            return False
        if type(case_id) is not int or case_id <= 0:  # noqa: E721
            raise ValidationError(_("A Contact Center activity requires a valid case."))
        return case_id

    @api.model
    def _contact_center_activity_user_from_values(self, values, activity=None):
        if "user_id" in values:
            return values.get("user_id")
        return activity.user_id.id if activity else self.env.user.id

    @api.model
    def _contact_center_lock_activity_scope(
        self, case_ids, user_ids=None, require_active=True
    ):
        """Fence topology, conversations and cases in their canonical order."""

        case_ids = sorted({int(value) for value in case_ids if value})
        if not case_ids:
            return self.env["contact.center.case"]
        cases = (
            self.env["contact.center.case"]
            .sudo()
            .with_context(active_test=False)
            .browse(case_ids)
            .exists()
        )
        if set(cases.ids) != set(case_ids):
            raise ValidationError(_("The Contact Center activity case does not exist."))
        locked_user_ids = set(user_ids or []) | {self.env.user.id}
        cases = cases._contact_center_lock_case_topology(
            extra_user_ids=sorted(locked_user_ids)
        )
        for case in cases:
            if require_active and not case.active:
                raise ValidationError(
                    _("An archived case cannot receive or retain a follow-up.")
                )
            case._contact_center_validate_case_scope()
            if not self.env.su:
                case.with_user(self.env.user)._contact_center_check_operational_access()
        return cases

    @api.model
    def _contact_center_validate_assignees(self, cases, assignments):
        agent_group = self.env.ref("contact_center_base.group_contact_center_agent")
        case_by_id = {case.id: case for case in cases}
        for case_id, user_id in assignments:
            case = case_by_id.get(case_id)
            user = self.env["res.users"].sudo().browse(user_id).exists()
            account = case.channel_id._contact_center_case_account()
            if (
                not user
                or not user.active
                or user.share
                or agent_group not in user.groups_id
                or case.company_id not in user.company_ids
                or not account
                or user not in account._contact_center_effective_users()
            ):
                raise ValidationError(
                    _("The activity assignee is outside this inbox access scope.")
                )

    @api.model
    def _contact_center_presubscribe_duplicate_assignees(self, cases, assignments):
        """Neutralize an Odoo 16 bulk-activity follower duplication defect.

        Native ``mail.activity.create`` batches subscriptions by model and user,
        but it keeps duplicate ``res_id`` values.  Two activities assigned to the
        same user on the same record therefore try to insert the same follower
        twice in one SQL statement.  Pre-subscribing only repeated pairs preserves
        native semantics while making legitimate bulk creation deterministic.  The
        caller already holds every case lock in canonical order, so concurrent and
        multi-case batches remain serialized without introducing another lock order.
        """

        counts = {}
        for case_id, user_id in assignments:
            if not case_id or not user_id:
                continue
            key = (case_id, user_id)
            counts[key] = counts.get(key, 0) + 1
        duplicates = {key for key, count in counts.items() if count > 1}
        if not duplicates:
            return
        case_by_id = {case.id: case for case in cases}
        users = (
            self.env["res.users"]
            .sudo()
            .browse(sorted({user_id for _case_id, user_id in duplicates}))
            .exists()
        )
        partner_by_user_id = {
            user.id: user.partner_id.id for user in users if user.partner_id
        }
        partners_by_case_id = {}
        for case_id, user_id in duplicates:
            partner_id = partner_by_user_id.get(user_id)
            if partner_id:
                partners_by_case_id.setdefault(case_id, set()).add(partner_id)
        for case_id in sorted(partners_by_case_id):
            partner_ids = partners_by_case_id[case_id]
            case_by_id[case_id].with_user(self.env.user).message_subscribe(
                partner_ids=sorted(partner_ids)
            )

    @api.model
    def _contact_center_lock_activities_and_receipts(self, activity_ids):
        activity_ids = sorted({int(value) for value in activity_ids if value})
        if not activity_ids:
            return self.env["contact.center.followup.request"]
        self.env.cr.execute(
            "SELECT id FROM mail_activity WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [activity_ids],
        )
        self.env["contact.center.followup.request"].sudo().flush_model(
            ["activity_record_id"]
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_followup_request "
            "WHERE activity_record_id = ANY(%s) ORDER BY id FOR UPDATE",
            [activity_ids],
        )
        receipt_ids = [row[0] for row in self.env.cr.fetchall()]
        self.invalidate_recordset()
        return self.env["contact.center.followup.request"].sudo().browse(receipt_ids)

    @api.model
    def _contact_center_trusted_activity_service(self):
        return (
            self.env.context.get("contact_center_productivity_notification_suppression")
            is _PRODUCTIVITY_NOTIFICATION_SUPPRESSION_TOKEN
        )

    @api.model
    def _contact_center_snapshot_values(self, activity, case):
        return {
            "id": activity.id,
            "case_id": case.id,
            "case_name": case.display_name,
            "activity_type": {
                "id": activity.activity_type_id.id,
                "name": activity.activity_type_id.display_name,
                "icon": activity.icon or "",
            },
            "summary": activity.summary or activity.activity_type_id.display_name,
            "note": html2plaintext(activity.note or "").strip(),
            "date_deadline": fields.Date.to_string(activity.date_deadline),
            "state": activity.state,
            "assigned_to": {
                "id": activity.user_id.id,
                "name": activity.user_id.display_name,
            },
            "can_complete": False,
            "completed": False,
        }

    def _contact_center_sync_active_receipts(self, receipts):
        for receipt in receipts.filtered(lambda item: item.state == "active"):
            activity = self.filtered(
                lambda item: item.id == receipt.activity_record_id
            )[:1]
            if not activity or not activity.exists():
                continue
            case = (
                self.env["contact.center.case"].sudo().browse(activity.res_id).exists()
            )
            if (
                activity.res_model != "contact.center.case"
                or not case
                or receipt.case_id != case
                or receipt.channel_id != case.channel_id
                or receipt.activity_id != activity
            ):
                raise ValidationError(
                    _("The follow-up receipt failed its integrity check.")
                )
            receipt._service_write(
                {
                    "activity_snapshot_json": self._contact_center_snapshot_values(
                        activity, case
                    )
                }
            )
        return True

    def _contact_center_productivity_channel_ids(self):
        case_ids = {
            activity.res_id
            for activity in self
            if activity.res_model == "contact.center.case" and activity.res_id
        }
        if not case_ids:
            return set()
        cases = (
            self.env["contact.center.case"]
            .sudo()
            .with_context(active_test=False)
            .browse(sorted(case_ids))
            .exists()
        )
        return set(cases.mapped("channel_id").ids)

    @api.model
    def _contact_center_notify_productivity(self, channel_ids):
        if (
            self.env.context.get("contact_center_productivity_notification_suppression")
            is _PRODUCTIVITY_NOTIFICATION_SUPPRESSION_TOKEN
        ):
            return True
        channels = (
            self.env["mail.channel"].sudo().browse(sorted(set(channel_ids))).exists()
        )
        application = self.env["contact.center.application"]
        for channel in channels:
            application._notify_ui(
                channel, "productivity_updated", {"reason": "mail_activity_changed"}
            )
        return True

    @api.model_create_multi
    def create(self, vals_list):
        case_ids = []
        assignments = []
        for values in vals_list:
            case_id = self._contact_center_target_from_values(values)
            if not case_id:
                continue
            case_ids.append(case_id)
            assignments.append(
                (case_id, self._contact_center_activity_user_from_values(values))
            )
        if case_ids:
            cases = self._contact_center_lock_activity_scope(
                case_ids, user_ids=[user_id for _case_id, user_id in assignments]
            )
            if not self._contact_center_trusted_activity_service():
                self._contact_center_validate_assignees(cases, assignments)
            self._contact_center_presubscribe_duplicate_assignees(cases, assignments)
        activities = super().create(vals_list)
        activities._contact_center_notify_productivity(
            activities._contact_center_productivity_channel_ids()
        )
        return activities

    def write(self, values):
        channel_ids = self._contact_center_productivity_channel_ids()
        current_case_by_activity = {
            activity.id: (
                activity.res_id
                if activity.res_model == "contact.center.case"
                else False
            )
            for activity in self
        }
        target_case_by_activity = {
            activity.id: self._contact_center_target_from_values(values, activity)
            for activity in self
        }
        case_ids = set(current_case_by_activity.values()) | set(
            target_case_by_activity.values()
        )
        case_ids.discard(False)
        assignments = []
        for activity in self:
            target_case_id = target_case_by_activity[activity.id]
            if target_case_id:
                assignments.append(
                    (
                        target_case_id,
                        self._contact_center_activity_user_from_values(
                            values, activity
                        ),
                    )
                )
        receipts = self.env["contact.center.followup.request"]
        if case_ids:
            cases = self._contact_center_lock_activity_scope(
                case_ids, user_ids=[user_id for _case_id, user_id in assignments]
            )
            if not self._contact_center_trusted_activity_service():
                receipts = self._contact_center_lock_activities_and_receipts(self.ids)
                if _ACTIVITY_TARGET_FIELDS & set(values):
                    protected_ids = set(receipts.mapped("activity_record_id"))
                    for activity in self:
                        if (
                            activity.id in protected_ids
                            and current_case_by_activity[activity.id]
                            != target_case_by_activity[activity.id]
                        ):
                            raise AccessError(
                                _(
                                    "A receipted Contact Center follow-up cannot be "
                                    "retargeted."
                                )
                            )
                self._contact_center_validate_assignees(cases, assignments)
        result = super().write(values)
        if receipts:
            self._contact_center_sync_active_receipts(receipts)
        channel_ids.update(self._contact_center_productivity_channel_ids())
        self._contact_center_notify_productivity(channel_ids)
        return result

    def unlink(self):
        channel_ids = self._contact_center_productivity_channel_ids()
        case_ids = [
            activity.res_id
            for activity in self
            if activity.res_model == "contact.center.case" and activity.res_id
        ]
        receipts = self.env["contact.center.followup.request"]
        if case_ids:
            user_ids = [activity.user_id.id for activity in self if activity.user_id]
            self._contact_center_lock_activity_scope(case_ids, user_ids=user_ids)
            if not self._contact_center_trusted_activity_service():
                receipts = self._contact_center_lock_activities_and_receipts(self.ids)
                self._contact_center_sync_active_receipts(receipts)
                active_receipts = receipts.filtered(lambda item: item.state == "active")
                if active_receipts:
                    active_receipts._service_write(
                        {
                            "state": "closed",
                            "closed_at": fields.Datetime.now(),
                        }
                    )
        result = super().unlink()
        self._contact_center_notify_productivity(channel_ids)
        return result


class ContactCenterScheduledMessage(models.Model):
    _name = "contact.center.scheduled.message"
    _description = "Contact Center Scheduled Message"
    _order = "scheduled_at desc, id desc"
    _check_company_auto = True

    channel_id = fields.Many2one(
        "mail.channel", required=True, index=True, ondelete="cascade"
    )
    company_id = fields.Many2one(
        related="channel_id.contact_center_company_id",
        store=True,
        readonly=True,
        index=True,
    )
    requested_by_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="restrict"
    )
    ui_request_id = fields.Char(required=True, index=True, readonly=True, copy=False)
    outbound_request_id = fields.Char(
        required=True,
        index=True,
        readonly=True,
        copy=False,
        default=lambda self: str(uuid.uuid4()),
        help=(
            "Server-owned idempotency key used only when the scheduled intent is "
            "released into the outbound message ledger."
        ),
    )
    body = fields.Text(required=True, readonly=True)
    body_sha256 = fields.Char(required=True, readonly=True, copy=False)
    scheduled_at = fields.Datetime(required=True, index=True, readonly=True)
    state = fields.Selection(
        [
            ("scheduled", "Scheduled"),
            ("processing", "Processing"),
            ("released", "Released"),
            ("cancelled", "Cancelled"),
            ("failed", "Failed"),
        ],
        required=True,
        default="scheduled",
        index=True,
        readonly=True,
        copy=False,
    )
    queue_job_uuid = fields.Char(index=True, readonly=True, copy=False)
    message_id = fields.Many2one(
        "mail.message", index=True, readonly=True, copy=False, ondelete="set null"
    )
    outbox_command_id = fields.Many2one(
        "contact.center.outbox.command",
        index=True,
        readonly=True,
        copy=False,
        ondelete="set null",
    )
    released_at = fields.Datetime(index=True, readonly=True, copy=False)
    cancelled_at = fields.Datetime(index=True, readonly=True, copy=False)
    cancelled_by_id = fields.Many2one(
        "res.users", index=True, readonly=True, copy=False, ondelete="set null"
    )
    failed_at = fields.Datetime(index=True, readonly=True, copy=False)
    last_error_class = fields.Char(readonly=True, copy=False)
    last_error_message = fields.Text(readonly=True, copy=False)

    _sql_constraints = [
        (
            "channel_request_unique",
            "unique(channel_id, ui_request_id)",
            "This scheduled message request already exists.",
        ),
        (
            "outbound_request_unique",
            "unique(outbound_request_id)",
            "The scheduled outbound request identifier must be unique.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("contact_center_productivity_service_token")
            is not _PRODUCTIVITY_SERVICE_TOKEN
        ):
            raise AccessError(_("Scheduled messages are created by the UI service."))
        if any("outbound_request_id" in values for values in vals_list):
            raise AccessError(
                _("The scheduled outbound request identifier is managed internally.")
            )
        normalized = []
        for values in vals_list:
            item = dict(values)
            item["outbound_request_id"] = str(uuid.uuid4())
            normalized.append(item)
        return super().create(normalized)

    def write(self, values):
        if (
            self.env.context.get("contact_center_productivity_service_token")
            is not _PRODUCTIVITY_SERVICE_TOKEN
        ):
            raise AccessError(_("Scheduled messages are managed by the UI service."))
        immutable = {
            "body",
            "body_sha256",
            "channel_id",
            "company_id",
            "outbound_request_id",
            "requested_by_id",
            "scheduled_at",
            "ui_request_id",
        }
        if immutable & set(values):
            raise AccessError(_("Scheduled message intent fields are immutable."))
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Scheduled message history is immutable."))

    def _service_write(self, values):
        return (
            self.sudo()
            .with_context(
                contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
            )
            .write(values)
        )

    def _identity_key(self):
        self.ensure_one()
        return "contact_center:scheduled:%s" % self.id

    def _active_queue_job(self):
        self.ensure_one()
        job_model = self.env["queue.job"].sudo()
        expected_identity = self._identity_key()
        job = job_model.search(
            [
                ("state", "in", _ACTIVE_QUEUE_JOB_STATES),
                ("identity_key", "=", expected_identity),
            ],
            order="id desc",
            limit=1,
        )
        return job

    def _failed_queue_job(self):
        self.ensure_one()
        job_model = self.env["queue.job"].sudo()
        expected_identity = self._identity_key()
        job = job_model.search(
            [("state", "=", "failed"), ("identity_key", "=", expected_identity)],
            order="id desc",
            limit=1,
        )
        return job

    def _enqueue(self):
        now = fields.Datetime.now()
        for scheduled in self.sudo().filtered(lambda item: item.state == "scheduled"):
            active_job = scheduled._active_queue_job()
            if active_job:
                if scheduled.queue_job_uuid != active_job.uuid:
                    scheduled._service_write({"queue_job_uuid": active_job.uuid})
                continue
            eta = max(
                fields.Datetime.to_datetime(scheduled.scheduled_at),
                fields.Datetime.to_datetime(now),
            )
            delayed = (
                scheduled.with_company(scheduled.company_id)
                .with_delay(
                    identity_key=scheduled._identity_key(),
                    max_retries=5,
                    description="Contact Center scheduled message %s" % scheduled.id,
                    eta=eta,
                )
                ._job_release()
            )
            scheduled._service_write({"queue_job_uuid": delayed.uuid})
        return True

    def _notify_productivity(self):
        for scheduled in self:
            self.env["contact.center.application"]._notify_ui(
                scheduled.channel_id,
                "productivity_updated",
                {"scheduled_message_id": scheduled.id},
            )
        return True

    def _lock_for_transition(self):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM contact_center_scheduled_message WHERE id = %s FOR UPDATE",
            [self.id],
        )
        if not self.env.cr.fetchone():
            return False
        self.invalidate_recordset()
        return True

    def _job_release(self):
        self.ensure_one()
        if not self.env.context.get("job_uuid"):
            raise ValidationError(_("A scheduled message must run through queue_job."))
        if not self._lock_for_transition():
            return False
        job_uuid = self.env.context.get("job_uuid")
        if not self.queue_job_uuid or self.queue_job_uuid != job_uuid:
            return False
        if self.state != "scheduled":
            return False
        persisted_hash = hashlib.sha256(
            str(self.body or "").encode("utf-8")
        ).hexdigest()
        if persisted_hash != self.body_sha256:
            self._service_write(
                {
                    "state": "failed",
                    "failed_at": fields.Datetime.now(),
                    "last_error_class": "ScheduledBodyIntegrityError",
                    "last_error_message": (
                        "The scheduled message body no longer matches its immutable digest."
                    ),
                }
            )
            self._notify_productivity()
            return False
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        scheduled_at = fields.Datetime.to_datetime(self.scheduled_at)
        if scheduled_at > now:
            raise RetryableJobError(
                _("The scheduled delivery time has not arrived yet."),
                seconds=max(1, math.ceil((scheduled_at - now).total_seconds())),
            )

        self._service_write({"state": "processing"})
        requester = self.requested_by_id.sudo().exists()
        try:
            if not requester or not requester.active or requester.share:
                raise AccessError(
                    _("The user who scheduled this message is unavailable.")
                )
            with self.env.cr.savepoint():
                result = (
                    self.env["contact.center.ui.api"]
                    .with_user(requester)
                    .send_message(
                        self.channel_id.id,
                        self.body,
                        client_request_id=self.outbound_request_id,
                        media_refs=[],
                    )
                )
        except (AccessError, UserError, ValidationError) as error:
            self._service_write(
                {
                    "state": "failed",
                    "failed_at": fields.Datetime.now(),
                    "last_error_class": error.__class__.__name__,
                    "last_error_message": str(error)[:4000],
                }
            )
            self._notify_productivity()
            return False

        self._service_write(
            {
                "state": "released",
                "message_id": result["message_id"],
                "outbox_command_id": result["outbox_command_id"],
                "released_at": fields.Datetime.now(),
                "last_error_class": False,
                "last_error_message": False,
            }
        )
        self._notify_productivity()
        return True

    @api.model
    def _cron_recover_scheduled_messages(
        self, limit=_RECOVERY_LIMIT, grace_seconds=_RECOVERY_GRACE_SECONDS
    ):
        limit = max(0, min(int(limit), 500))
        grace_seconds = max(0, int(grace_seconds))
        if not limit:
            return 0
        cutoff = fields.Datetime.now() - datetime.timedelta(seconds=grace_seconds)
        self.flush_model(["state", "queue_job_uuid", "write_date", "scheduled_at"])
        self.env["queue.job"].sudo().flush_model(
            ["uuid", "identity_key", "state", "eta"]
        )
        self.env.cr.execute(
            """
            SELECT scheduled.id
              FROM contact_center_scheduled_message AS scheduled
             WHERE scheduled.state = 'scheduled'
               AND COALESCE(scheduled.write_date, scheduled.create_date) <= %s
               AND NOT EXISTS (
                    SELECT 1
                      FROM queue_job AS job
                     WHERE job.state IN %s
                       AND job.identity_key = 'contact_center:scheduled:'
                           || scheduled.id::text
               )
          ORDER BY scheduled.scheduled_at, scheduled.id
             FOR UPDATE OF scheduled SKIP LOCKED
             LIMIT %s
            """,
            [cutoff, _ACTIVE_QUEUE_JOB_STATES, limit],
        )
        scheduled_messages = self.sudo().browse(
            [row[0] for row in self.env.cr.fetchall()]
        )
        recovered = 0
        for scheduled in scheduled_messages:
            scheduled.invalidate_recordset(["state", "queue_job_uuid", "scheduled_at"])
            failed_job = scheduled._failed_queue_job()
            if failed_job:
                # queue_job has already exhausted the function retry policy.  A
                # recovery cron must not turn a deterministic programming or data
                # error into an infinite dispatch loop.
                scheduled._service_write(
                    {
                        "state": "failed",
                        "failed_at": fields.Datetime.now(),
                        "last_error_class": "ScheduledQueueJobFailed",
                        "last_error_message": (
                            "The scheduled release job exhausted its retry policy."
                        ),
                    }
                )
                scheduled._notify_productivity()
                recovered += 1
                continue
            if scheduled.queue_job_uuid:
                scheduled._service_write({"queue_job_uuid": False})
            scheduled._enqueue()
            scheduled.invalidate_recordset(["queue_job_uuid"])
            recovered += int(bool(scheduled.queue_job_uuid))
        return recovered


class ContactCenterUiApiProductivity(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def bootstrap(self):
        result = super().bootstrap()
        result["capabilities"].update(
            {
                "quick_replies": True,
                "internal_notes": True,
                "followups": True,
                "scheduled_messages": True,
            }
        )
        return result

    @api.model
    def _productivity_fence_channel(self, channel):
        channel.ensure_one()
        self.env.cr.execute(
            """
            UPDATE mail_channel
               SET contact_center_productivity_revision =
                   contact_center_productivity_revision + 1
             WHERE id = %s
         RETURNING contact_center_productivity_revision
            """,
            [channel.id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("The conversation no longer exists."))
        channel.invalidate_recordset(["contact_center_productivity_revision"])
        return True

    @api.model
    def _internal_note_message(self, channel, request):
        message = request.message_id.exists()
        note_subtype = self.env.ref("mail.mt_note")
        if (
            not message
            or message.model != "mail.channel"
            or message.res_id != channel.id
            or message.message_type != "comment"
            or message.subtype_id != note_subtype
        ):
            raise ValidationError(
                _("The stored internal note failed its integrity check.")
            )
        persisted_body = str(message.body or "")
        persisted_hash = hashlib.sha256(persisted_body.encode("utf-8")).hexdigest()
        if persisted_hash != request.message_body_sha256:
            raise ValidationError(_("The stored internal note body was modified."))
        return message

    @api.model
    def search_quick_replies(self, channel_id, query="", limit=20):
        channel, _member = self._authorized_channel(channel_id)
        if not isinstance(query, str):
            raise ValidationError(_("The quick reply search must be text."))
        query = query.strip()[:100]
        limit = self._bounded_int(
            limit, default=20, minimum=1, maximum=50, label=_("quick reply limit")
        )
        channel_binding = self._binding_for_channel(channel)
        if not channel_binding:
            raise ValidationError(_("The conversation has no active binding."))
        query_domain = []
        if query:
            query_domain = expression.OR(
                [
                    [("shortcode_id.source", "ilike", query)],
                    [("shortcode_id.description", "ilike", query)],
                    [("shortcode_id.substitution", "ilike", query)],
                ]
            )
        target_by_scope = {
            "company": ("company_id", channel.contact_center_company_id.id),
            "team": ("team_id", channel.contact_center_team_id.id),
            "account": ("account_id", channel_binding.account_id.id),
            "channel": ("channel_id", channel.id),
        }
        bindings = self.env["contact.center.quick.reply.binding"]
        for scope, (target_field, target_id) in target_by_scope.items():
            if not target_id:
                continue
            bindings |= self.env["contact.center.quick.reply.binding"].search(
                expression.AND(
                    [
                        [
                            ("active", "=", True),
                            ("scope", "=", scope),
                            (target_field, "=", target_id),
                        ],
                        query_domain,
                    ]
                ),
                order="sequence, id",
                limit=limit,
            )
        # Collapse a shortcode bound at multiple levels.  The most specific
        # applicable binding wins deterministically.
        selected_by_shortcode = {}
        for binding in bindings:
            previous = selected_by_shortcode.get(binding.shortcode_id.id)
            if (
                not previous
                or _QUICK_REPLY_SCOPE_PRIORITY[binding.scope]
                > _QUICK_REPLY_SCOPE_PRIORITY[previous.scope]
            ):
                selected_by_shortcode[binding.shortcode_id.id] = binding
        selected = sorted(
            selected_by_shortcode.values(),
            key=lambda binding: (
                binding.sequence,
                binding.shortcode_id.source or "",
                binding.id,
            ),
        )[:limit]
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "items": [
                {
                    "id": binding.shortcode_id.id,
                    "binding_id": binding.id,
                    "scope": binding.scope,
                    "shortcut": binding.shortcode_id.sudo().source or "",
                    "body": binding.shortcode_id.sudo().substitution or "",
                    "description": binding.shortcode_id.sudo().description or "",
                }
                for binding in selected
            ],
        }

    @api.model
    def post_internal_note(self, channel_id, body, client_request_id):
        parsed_channel_id = self._positive_id(channel_id, _("conversation ID"))
        request_id = _canonical_uuid(client_request_id, _("client request ID"))
        clean_body = _plain_text(body, _("internal note"), _MAX_NOTE_CHARS)
        body_sha256 = hashlib.sha256(clean_body.encode("utf-8")).hexdigest()
        channel, _member = self._authorized_channel(parsed_channel_id)
        self._productivity_fence_channel(channel)
        channel, _member = self._authorized_channel(channel.id)
        ledger_model = self.env["contact.center.internal.note.request"].sudo()
        existing = ledger_model.search(
            [("channel_id", "=", channel.id), ("ui_request_id", "=", request_id)],
            limit=1,
        )
        if existing:
            if existing.body_sha256 != body_sha256:
                raise ValidationError(
                    _("The client request ID belongs to another internal note.")
                )
            message = self._internal_note_message(channel, existing)
            return {
                "schema_version": SCHEMA_VERSION,
                "channel_id": channel.id,
                "client_request_id": request_id,
                "message": self._serialize_message(message),
            }
        message = channel.message_post(
            body=plaintext2html(clean_body),
            message_type="comment",
            subtype_xmlid="mail.mt_note",
            partner_ids=[],
        )
        message_body_sha256 = hashlib.sha256(
            str(message.body or "").encode("utf-8")
        ).hexdigest()
        request = ledger_model.with_context(
            contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
        ).create(
            {
                "channel_id": channel.id,
                "message_id": message.id,
                "requested_by_id": self.env.user.id,
                "ui_request_id": request_id,
                "body_sha256": body_sha256,
                "message_body_sha256": message_body_sha256,
            }
        )
        self._internal_note_message(channel, request)
        channel.sudo().channel_member_ids.invalidate_recordset(
            ["message_unread_counter"]
        )
        # Internal notes are timeline-only information.  Reuse an existing
        # timeline invalidation event without publishing a canonical
        # ``message_created`` event, which would advance inbox ordering/SLA and
        # trigger customer-message attention handling.
        self._application()._notify_ui(
            channel,
            "message_updated",
            {"message_id": message.id, "reason": "internal_note_created"},
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "client_request_id": request_id,
            "message": self._serialize_message(message),
        }

    @api.model
    def _case_for_productivity(self, channel, case_id=False, write=False):
        if case_id:
            case = (
                self.env["contact.center.case"]
                .browse(self._positive_id(case_id, _("case ID")))
                .exists()
            )
        else:
            default_case = channel._contact_center_ensure_default_case()
            case = self.env["contact.center.case"].browse(default_case.id).exists()
        if not case or case.channel_id != channel or not case.active:
            raise ValidationError(_("The service case is not available."))
        case.check_access_rights("write" if write else "read")
        case.check_access_rule("write" if write else "read")
        return case

    @api.model
    def _productivity_cases(self, channel):
        return self.env["contact.center.case"].search(
            [("channel_id", "=", channel.id), ("active", "=", True)],
            order="is_default desc, id",
        )

    @api.model
    def _productivity_assignable_users(self, channel):
        binding = self._binding_for_channel(channel)
        users = (
            binding.account_id._contact_center_effective_users()
            if binding
            else self.env["res.users"]
        )
        return users.filtered(
            lambda user: user.active
            and not user.share
            and channel.contact_center_company_id in user.company_ids
        )

    @api.model
    def _activity_types_for_cases(self):
        return self.env["mail.activity.type"].search(
            [
                ("active", "=", True),
                "|",
                ("res_model", "=", False),
                ("res_model", "=", "contact.center.case"),
            ],
            order="sequence, name, id",
        )

    @api.model
    def _followup_schedule_payload_sha256_for_replay(self, request, values):
        """Canonicalize a replay from immutable receipt data, not live policy."""

        request.ensure_one()
        snapshot = dict(request.activity_snapshot_json or {})
        activity_type_snapshot = snapshot.get("activity_type") or {}
        assigned_to_snapshot = snapshot.get("assigned_to") or {}
        if not isinstance(activity_type_snapshot, dict) or not isinstance(
            assigned_to_snapshot, dict
        ):
            raise ValidationError(
                _("The follow-up receipt failed its integrity check.")
            )

        case_id = (
            self._positive_id(values.get("case_id"), _("case ID"))
            if values.get("case_id")
            else request.case_id.id
        )
        activity_type_id = self._positive_id(
            values.get("activity_type_id") or activity_type_snapshot.get("id"),
            _("activity type ID"),
        )
        deadline_value = values.get("date_deadline")
        try:
            deadline = fields.Date.to_date(deadline_value)
        except (TypeError, ValueError) as error:
            raise ValidationError(_("The follow-up deadline is invalid.")) from error
        if not deadline:
            raise ValidationError(_("The follow-up deadline is required."))

        summary_value = values.get("summary")
        summary = _plain_text(
            summary_value or snapshot.get("summary") or "",
            _("follow-up summary"),
            200,
        )
        note = _plain_text(
            values.get("note") or "", _("follow-up note"), 4000, required=False
        )
        user_id = self._positive_id(
            values.get("user_id") or assigned_to_snapshot.get("id"),
            _("assigned user ID"),
        )
        return _payload_sha256(
            {
                "activity_type_id": activity_type_id,
                "case_id": case_id,
                "date_deadline": fields.Date.to_string(deadline),
                "note": note,
                "summary": summary,
                "user_id": user_id,
            }
        )

    @api.model
    def _serialize_case_productivity(self, case):
        return {
            "id": case.id,
            "name": case.display_name,
            "is_default": bool(case.is_default),
            "pipeline": {"id": case.pipeline_id.id, "name": case.pipeline_id.name},
            "stage": {"id": case.stage_id.id, "name": case.stage_id.name},
            "responsible_user": (
                {
                    "id": case.responsible_user_id.id,
                    "name": case.responsible_user_id.display_name,
                }
                if case.responsible_user_id
                else False
            ),
        }

    @api.model
    def _serialize_activity_productivity(self, activity, case=None, completed=False):
        case = case or self.env["contact.center.case"].browse(activity.res_id)
        can_complete = bool(
            not completed
            and (
                activity.user_id == self.env.user
                or self.env.user.has_group(
                    "contact_center_base.group_contact_center_supervisor"
                )
            )
        )
        return {
            "id": activity.id,
            "case_id": case.id,
            "case_name": case.display_name,
            "activity_type": {
                "id": activity.activity_type_id.id,
                "name": activity.activity_type_id.display_name,
                "icon": activity.icon or "",
            },
            "summary": activity.summary or activity.activity_type_id.display_name,
            "note": html2plaintext(activity.note or "").strip(),
            "date_deadline": fields.Date.to_string(activity.date_deadline),
            "state": "completed" if completed else activity.state,
            "assigned_to": {
                "id": activity.user_id.id,
                "name": activity.user_id.display_name,
            },
            "can_complete": can_complete,
            "completed": bool(completed),
        }

    @api.model
    def _serialize_followup_request_activity(self, request):
        activity = request.activity_id.exists()
        if request.state == "active" and activity:
            if (
                activity.res_model != "contact.center.case"
                or activity.res_id != request.case_id.id
            ):
                raise ValidationError(
                    _("The follow-up receipt failed its integrity check.")
                )
            return self._serialize_activity_productivity(activity, case=request.case_id)
        snapshot = dict(
            request.completion_snapshot_json
            if request.state == "completed"
            else request.activity_snapshot_json or {}
        )
        if not snapshot:
            raise ValidationError(_("The follow-up receipt has no stable snapshot."))
        snapshot.update(
            {
                "can_complete": False,
                "completed": request.state == "completed",
                "state": (
                    "completed" if request.state == "completed" else "unavailable"
                ),
            }
        )
        return snapshot

    @api.model
    def _followup_replay_envelope(
        self, channel, request, payload_sha256=None, client_request_id=False
    ):
        if payload_sha256 and request.completion_payload_sha256 != payload_sha256:
            raise ValidationError(
                _("The follow-up was already completed with different feedback.")
            )
        activity = self._serialize_followup_request_activity(request)
        assigned_to = activity.get("assigned_to") or {}
        if (
            request.state == "completed"
            and assigned_to.get("id") != self.env.user.id
            and not self.env.user.has_group(
                "contact_center_base.group_contact_center_supervisor"
            )
        ):
            raise AccessError(
                _("Only the assignee or a supervisor can inspect this completion.")
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "client_request_id": (
                request.completion_request_id or client_request_id or False
            ),
            "activity": activity,
        }

    @api.model
    def _serialize_scheduled_productivity(self, scheduled):
        can_cancel = bool(
            scheduled.state == "scheduled"
            and (
                scheduled.requested_by_id == self.env.user
                or self.env.user.has_group(
                    "contact_center_base.group_contact_center_supervisor"
                )
            )
        )
        return {
            "id": scheduled.id,
            "channel_id": scheduled.channel_id.id,
            "body": scheduled.body,
            "scheduled_at": fields.Datetime.to_string(scheduled.scheduled_at),
            "state": scheduled.state,
            "requested_by": {
                "id": scheduled.requested_by_id.id,
                "name": scheduled.requested_by_id.display_name,
            },
            "message_id": scheduled.message_id.id or False,
            "outbox_command_id": scheduled.outbox_command_id.id or False,
            "released_at": fields.Datetime.to_string(scheduled.released_at),
            "cancelled_at": fields.Datetime.to_string(scheduled.cancelled_at),
            "failed_at": fields.Datetime.to_string(scheduled.failed_at),
            "error": (
                {
                    "class": scheduled.last_error_class,
                    "message": scheduled.last_error_message,
                }
                if scheduled.last_error_class
                else False
            ),
            "can_cancel": can_cancel,
        }

    @api.model
    def get_productivity(self, channel_id):
        channel, _member = self._authorized_channel(channel_id)
        cases = self._productivity_cases(channel)
        activities = self.env["mail.activity"].search(
            [("res_model", "=", "contact.center.case"), ("res_id", "in", cases.ids)],
            order="date_deadline, id",
        )
        case_by_id = {case.id: case for case in cases}
        active_scheduled_messages = self.env["contact.center.scheduled.message"].search(
            [
                ("channel_id", "=", channel.id),
                ("state", "in", ("scheduled", "processing")),
            ],
            order="scheduled_at, id",
        )
        scheduled_history = self.env["contact.center.scheduled.message"].search(
            [
                ("channel_id", "=", channel.id),
                ("state", "not in", ("scheduled", "processing")),
            ],
            order="scheduled_at desc, id desc",
            limit=_SCHEDULED_HISTORY_LIMIT,
        )
        scheduled_messages = active_scheduled_messages | scheduled_history
        activity_types = self._activity_types_for_cases()
        assignable_users = self._productivity_assignable_users(channel)
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "cases": [self._serialize_case_productivity(case) for case in cases],
            "activities": [
                self._serialize_activity_productivity(
                    activity, case=case_by_id.get(activity.res_id)
                )
                for activity in activities
                if case_by_id.get(activity.res_id)
            ],
            "scheduled_messages": [
                self._serialize_scheduled_productivity(scheduled)
                for scheduled in scheduled_messages
            ],
            "activity_types": [
                {
                    "id": activity_type.id,
                    "name": activity_type.display_name,
                    "summary": activity_type.summary or "",
                    "icon": activity_type.icon or "",
                }
                for activity_type in activity_types
            ],
            "assignable_users": [
                {"id": user.id, "name": user.display_name}
                for user in assignable_users.sorted(
                    key=lambda item: (item.name, item.id)
                )
            ],
        }

    @api.model
    def schedule_followup(self, channel_id, values):
        channel, _member = self._authorized_channel(channel_id)
        if not isinstance(values, dict):
            raise ValidationError(_("Follow-up values must be an object."))
        request_id = _canonical_uuid(
            values.get("client_request_id"), _("client request ID")
        )
        request_model = self.env["contact.center.followup.request"].sudo()
        preliminary_receipt = request_model.search(
            [("channel_id", "=", channel.id), ("ui_request_id", "=", request_id)],
            limit=1,
        )
        case = (
            preliminary_receipt.case_id
            if preliminary_receipt
            else self._case_for_productivity(
                channel, case_id=values.get("case_id"), write=True
            )
        )
        candidate_user_id = (
            preliminary_receipt.activity_id.user_id.id
            if preliminary_receipt.activity_id
            else self._positive_id(
                values.get("user_id") or self.env.user.id, _("assigned user ID")
            )
        )
        # The activity ORM hooks acquire the complete case topology.  Acquire it
        # before the channel productivity fence as well, so this endpoint never
        # introduces a channel -> account inversion against roster/topology
        # reconciliation.
        self.env["mail.activity"]._contact_center_lock_activity_scope(
            case.ids, user_ids=[candidate_user_id]
        )
        self._productivity_fence_channel(channel)
        channel, _member = self._authorized_channel(channel.id)
        case = self._case_for_productivity(channel, case_id=case.id, write=True)
        existing = request_model.search(
            [("channel_id", "=", channel.id), ("ui_request_id", "=", request_id)],
            limit=1,
        )
        if existing:
            schedule_payload_sha256 = self._followup_schedule_payload_sha256_for_replay(
                existing, values
            )
            if existing.schedule_payload_sha256 != schedule_payload_sha256:
                raise ValidationError(
                    _("The client request ID belongs to another follow-up.")
                )
            if (
                existing.requested_by_id != self.env.user
                and not self.env.user.has_group(
                    "contact_center_base.group_contact_center_supervisor"
                )
            ):
                raise AccessError(_("This follow-up request belongs to another user."))
            return {
                "schema_version": SCHEMA_VERSION,
                "channel_id": channel.id,
                "client_request_id": request_id,
                "activity": self._serialize_followup_request_activity(existing),
            }

        activity_type_id = values.get("activity_type_id")
        if activity_type_id:
            activity_type = self._activity_types_for_cases().filtered(
                lambda item: item.id
                == self._positive_id(activity_type_id, _("activity type ID"))
            )[:1]
        else:
            activity_type = self.env.ref("mail.mail_activity_data_todo")
        if not activity_type:
            raise ValidationError(_("The activity type is not available."))
        deadline_value = values.get("date_deadline")
        try:
            deadline = fields.Date.to_date(deadline_value)
        except (TypeError, ValueError) as error:
            raise ValidationError(_("The follow-up deadline is invalid.")) from error
        if not deadline:
            raise ValidationError(_("The follow-up deadline is required."))
        if deadline < fields.Date.context_today(self):
            raise ValidationError(_("The follow-up deadline cannot be in the past."))
        summary = _plain_text(
            values.get("summary") or activity_type.summary or activity_type.name,
            _("follow-up summary"),
            200,
        )
        note = _plain_text(
            values.get("note") or "", _("follow-up note"), 4000, required=False
        )
        assignable_users = self._productivity_assignable_users(channel)
        user_id = values.get("user_id") or self.env.user.id
        user = assignable_users.filtered(
            lambda item: item.id == self._positive_id(user_id, _("assigned user ID"))
        )[:1]
        if not user:
            raise ValidationError(_("The assigned user is outside this inbox scope."))
        schedule_payload_sha256 = _payload_sha256(
            {
                "activity_type_id": activity_type.id,
                "case_id": case.id,
                "date_deadline": fields.Date.to_string(deadline),
                "note": note,
                "summary": summary,
                "user_id": user.id,
            }
        )
        activity = case.with_context(
            contact_center_productivity_notification_suppression=(
                _PRODUCTIVITY_NOTIFICATION_SUPPRESSION_TOKEN
            )
        ).activity_schedule(
            activity_type_id=activity_type.id,
            date_deadline=deadline,
            summary=summary,
            note=plaintext2html(note) if note else False,
            user_id=user.id,
        )
        snapshot = self._serialize_activity_productivity(activity, case=case)
        request_model.with_context(
            contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
        ).create(
            {
                "channel_id": channel.id,
                "case_id": case.id,
                "activity_id": activity.id,
                "activity_record_id": activity.id,
                "requested_by_id": self.env.user.id,
                "ui_request_id": request_id,
                "schedule_payload_sha256": schedule_payload_sha256,
                "activity_snapshot_json": snapshot,
            }
        )
        self._application()._notify_ui(
            channel,
            "productivity_updated",
            {"activity_id": activity.id},
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "client_request_id": request_id,
            "activity": snapshot,
        }

    @api.model
    def complete_followup(
        self, channel_id, activity_id, feedback="", client_request_id=None
    ):
        channel, _member = self._authorized_channel(channel_id)
        parsed_activity_id = self._positive_id(activity_id, _("activity ID"))
        completion_request_id = _canonical_uuid(
            client_request_id, _("client request ID")
        )
        feedback = _plain_text(
            feedback or "", _("follow-up feedback"), 4000, required=False
        )
        completion_payload_sha256 = _payload_sha256(
            {"activity_id": parsed_activity_id, "feedback": feedback}
        )
        request_model = self.env["contact.center.followup.request"].sudo()
        preliminary_receipt = request_model.search(
            [
                ("channel_id", "=", channel.id),
                ("activity_record_id", "=", parsed_activity_id),
            ],
            limit=1,
        )
        preliminary_activity = (
            self.env["mail.activity"].browse(parsed_activity_id).exists()
        )
        if preliminary_receipt:
            preliminary_case = preliminary_receipt.case_id
        elif (
            preliminary_activity
            and preliminary_activity.res_model == "contact.center.case"
        ):
            preliminary_case = (
                self.env["contact.center.case"]
                .with_context(active_test=False)
                .browse(preliminary_activity.res_id)
                .exists()
            )
        else:
            preliminary_case = self.env["contact.center.case"]
        if not preliminary_case or preliminary_case.channel_id != channel:
            raise ValidationError(_("The follow-up activity does not exist."))
        preliminary_user_ids = (
            preliminary_activity.user_id.ids if preliminary_activity else []
        )
        self.env["mail.activity"]._contact_center_lock_activity_scope(
            preliminary_case.ids,
            user_ids=preliminary_user_ids,
            require_active=preliminary_receipt.state != "completed",
        )
        self._productivity_fence_channel(channel)
        channel, _member = self._authorized_channel(channel.id)
        # Complete the canonical order with activity -> receipt.  This helper
        # also covers the replay path after ``action_feedback`` removed the
        # activity row.
        self.env["mail.activity"]._contact_center_lock_activities_and_receipts(
            [parsed_activity_id]
        )
        request_collision = request_model.search(
            [
                ("channel_id", "=", channel.id),
                ("completion_request_id", "=", completion_request_id),
            ],
            limit=1,
        )
        if (
            request_collision
            and request_collision.activity_record_id != parsed_activity_id
        ):
            raise ValidationError(
                _("The completion request ID belongs to another follow-up.")
            )
        request_receipt = request_model.search(
            [
                ("channel_id", "=", channel.id),
                ("activity_record_id", "=", parsed_activity_id),
            ],
            limit=1,
        )
        if request_receipt.state == "completed":
            return self._followup_replay_envelope(
                channel,
                request_receipt,
                payload_sha256=completion_payload_sha256,
                client_request_id=completion_request_id,
            )

        activity = self.env["mail.activity"].browse(parsed_activity_id).exists()
        if not activity:
            request_receipt.invalidate_recordset()
            request_receipt = request_model.search(
                [
                    ("channel_id", "=", channel.id),
                    ("activity_record_id", "=", parsed_activity_id),
                ],
                limit=1,
            )
            if request_receipt.state == "completed":
                return self._followup_replay_envelope(
                    channel,
                    request_receipt,
                    payload_sha256=completion_payload_sha256,
                    client_request_id=completion_request_id,
                )
            raise ValidationError(
                _("The follow-up is no longer active and was not completed here.")
            )

        # Re-evaluate every authorization fact after obtaining the activity row
        # lock.  This is the cutover that prevents two feedback messages.
        channel, _member = self._authorized_channel(channel.id)
        activity.check_access_rights("write")
        activity.check_access_rule("write")
        if activity.res_model != "contact.center.case":
            raise ValidationError(_("The follow-up activity does not exist."))
        case = self._case_for_productivity(channel, case_id=activity.res_id, write=True)
        if activity.user_id != self.env.user and not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(_("Only the assignee or a supervisor can complete it."))
        snapshot = self._serialize_activity_productivity(
            activity, case=case, completed=True
        )
        if request_receipt:
            if (
                request_receipt.case_id != case
                or request_receipt.activity_id != activity
                or request_receipt.state != "active"
            ):
                raise ValidationError(
                    _("The follow-up receipt failed its integrity check.")
                )
        else:
            request_receipt = request_model.with_context(
                contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
            ).create(
                {
                    "channel_id": channel.id,
                    "case_id": case.id,
                    "activity_id": activity.id,
                    "activity_record_id": activity.id,
                    "requested_by_id": activity.create_uid.id,
                    "activity_snapshot_json": self._serialize_activity_productivity(
                        activity, case=case
                    ),
                }
            )
        request_receipt._service_write(
            {
                "state": "completed",
                "completion_request_id": completion_request_id,
                "completion_payload_sha256": completion_payload_sha256,
                "completion_snapshot_json": snapshot,
                "completed_by_id": self.env.user.id,
                "completed_at": fields.Datetime.now(),
            }
        )
        activity.with_context(
            contact_center_productivity_notification_suppression=(
                _PRODUCTIVITY_NOTIFICATION_SUPPRESSION_TOKEN
            )
        ).action_feedback(feedback=plaintext2html(feedback) if feedback else False)
        self._application()._notify_ui(
            channel,
            "productivity_updated",
            {"activity_id": snapshot["id"], "completed": True},
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "client_request_id": completion_request_id,
            "activity": snapshot,
        }

    @api.model
    def _normalize_scheduled_datetime_from_user(self, value):
        if isinstance(value, datetime.datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            raw = value.strip()
            try:
                parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                try:
                    parsed = fields.Datetime.to_datetime(raw.replace("T", " "))
                except (TypeError, ValueError) as error:
                    raise ValidationError(
                        _("The scheduled delivery time is invalid.")
                    ) from error
        else:
            raise ValidationError(_("The scheduled delivery time is required."))
        try:
            if parsed.tzinfo is None:
                user_timezone = pytz.timezone(self.env.user.tz or "UTC")
                parsed = user_timezone.localize(parsed, is_dst=None)
            parsed = parsed.astimezone(pytz.UTC).replace(tzinfo=None, microsecond=0)
        except (pytz.AmbiguousTimeError, pytz.NonExistentTimeError) as error:
            raise ValidationError(
                _("The scheduled delivery time is ambiguous in your timezone.")
            ) from error
        return parsed

    @api.model
    def _validate_scheduled_datetime_window(self, parsed):
        now = fields.Datetime.to_datetime(fields.Datetime.now()).replace(microsecond=0)
        minimum = now + datetime.timedelta(seconds=_MIN_SCHEDULE_DELAY_SECONDS)
        maximum = now + datetime.timedelta(days=_MAX_SCHEDULE_DELAY_DAYS)
        if parsed < minimum:
            raise ValidationError(
                _("Schedule the message at least one minute in the future.")
            )
        if parsed > maximum:
            raise ValidationError(
                _("Messages can be scheduled at most one year ahead.")
            )
        return parsed

    @api.model
    def schedule_message(self, channel_id, body, scheduled_at, client_request_id):
        parsed_channel_id = self._positive_id(channel_id, _("conversation ID"))
        request_id = _canonical_uuid(client_request_id, _("client request ID"))
        clean_body = _plain_text(
            body, _("scheduled message"), _MAX_SCHEDULED_BODY_CHARS
        )
        body_sha256 = hashlib.sha256(clean_body.encode("utf-8")).hexdigest()
        normalized_at = self._normalize_scheduled_datetime_from_user(scheduled_at)
        channel, _member = self._authorized_channel(parsed_channel_id)
        self._productivity_fence_channel(channel)
        channel, _member = self._authorized_channel(channel.id)
        scheduled_model = self.env["contact.center.scheduled.message"].sudo()
        existing = scheduled_model.search(
            [("channel_id", "=", channel.id), ("ui_request_id", "=", request_id)],
            limit=1,
        )
        if existing:
            if (
                existing.body_sha256 != body_sha256
                or fields.Datetime.to_datetime(existing.scheduled_at) != normalized_at
            ):
                raise ValidationError(
                    _("The client request ID belongs to another scheduled message.")
                )
            return {
                "schema_version": SCHEMA_VERSION,
                "channel_id": channel.id,
                "client_request_id": request_id,
                "scheduled_message": self._serialize_scheduled_productivity(existing),
            }
        self._validate_scheduled_datetime_window(normalized_at)
        binding = self._binding_for_channel(channel)
        if not binding:
            raise ValidationError(_("The conversation has no active binding."))
        binding._contact_center_ensure_outbound_supported(
            operation="send_message", has_text=True
        )
        scheduled = scheduled_model.with_context(
            contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
        ).create(
            {
                "channel_id": channel.id,
                "requested_by_id": self.env.user.id,
                "ui_request_id": request_id,
                "body": clean_body,
                "body_sha256": body_sha256,
                "scheduled_at": normalized_at,
            }
        )
        scheduled._enqueue()
        scheduled.invalidate_recordset(["queue_job_uuid"])
        scheduled._notify_productivity()
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "client_request_id": request_id,
            "scheduled_message": self._serialize_scheduled_productivity(scheduled),
        }

    @api.model
    def cancel_scheduled_message(self, channel_id, scheduled_message_id):
        parsed_channel_id = self._positive_id(channel_id, _("conversation ID"))
        scheduled = (
            self.env["contact.center.scheduled.message"]
            .sudo()
            .browse(self._positive_id(scheduled_message_id, _("scheduled message ID")))
            .exists()
        )
        if not scheduled or not scheduled._lock_for_transition():
            raise ValidationError(_("The scheduled message does not exist."))
        scheduled.invalidate_recordset()
        channel, _member = self._authorized_channel(parsed_channel_id)
        if scheduled.channel_id != channel:
            raise ValidationError(_("The scheduled message does not exist."))
        if scheduled.requested_by_id != self.env.user and not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(_("Only the scheduler or a supervisor can cancel it."))
        # Release and cancellation both acquire scheduled -> channel.  Keep the
        # authorization fence inside that order so neither path can form ABBA.
        self._productivity_fence_channel(channel)
        channel, _member = self._authorized_channel(channel.id)
        scheduled.invalidate_recordset()
        if scheduled.channel_id != channel:
            raise ValidationError(_("The scheduled message does not exist."))
        if scheduled.requested_by_id != self.env.user and not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(_("Only the scheduler or a supervisor can cancel it."))
        if scheduled.state == "scheduled":
            scheduled._service_write(
                {
                    "state": "cancelled",
                    "cancelled_at": fields.Datetime.now(),
                    "cancelled_by_id": self.env.user.id,
                }
            )
            scheduled._notify_productivity()
        elif scheduled.state != "cancelled":
            raise ValidationError(
                _("Only a pending scheduled message can be cancelled.")
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "scheduled_message": self._serialize_scheduled_productivity(scheduled),
        }

    @api.model
    def _conversation_list_domain(self, filters):
        domain = super()._conversation_list_domain(filters)
        filters = filters or {}
        unread_only = filters.get("unread_only", False)
        if unread_only not in (False, True, None):
            raise ValidationError(_("The unread filter must be boolean."))
        if unread_only:
            # ``message_unread_counter`` is computed by mail and is not a
            # searchable stored column in every supported Odoo release.  Scope
            # the recordset to the current partner first, then evaluate the
            # counter in Python.  This also avoids the classic one2many-domain
            # false positive where the partner condition matches one member
            # and the unread condition matches another.
            current_members = self.env["mail.channel.member"].search(
                [
                    ("partner_id", "=", self.env.user.partner_id.id),
                    ("channel_id.channel_type", "=", "contact_center"),
                    (
                        "channel_id.contact_center_company_id",
                        "in",
                        self.env.companies.ids,
                    ),
                ]
            )
            unread_members = current_members.filtered(
                lambda member: member.message_unread_counter > 0
            )
            domain = expression.AND(
                [domain, [("id", "in", unread_members.channel_id.ids)]]
            )
        conversation_type = filters.get("conversation_type")
        if conversation_type:
            if conversation_type not in ("direct", "group"):
                raise ValidationError(_("Unsupported conversation type filter."))
            bindings = self.env["contact.center.channel.binding"].search(
                [
                    ("active", "=", True),
                    ("merged_into_id", "=", False),
                    ("conversation_type", "=", conversation_type),
                ]
            )
            domain = expression.AND([domain, [("id", "in", bindings.channel_id.ids)]])
        tag_id = filters.get("tag_id")
        if tag_id:
            tag = (
                self.env["contact.center.tag"]
                .browse(self._positive_id(tag_id, _("tag ID")))
                .exists()
            )
            if not tag or tag.company_id not in self.env.companies:
                raise ValidationError(_("The tag is not available."))
            domain = expression.AND(
                [domain, [("contact_center_tag_ids", "in", tag.ids)]]
            )
        activity_timing = filters.get("activity_timing")
        if activity_timing:
            if activity_timing not in ("overdue", "today", "planned"):
                raise ValidationError(_("Unsupported follow-up timing filter."))
            today = fields.Date.context_today(self)
            operator = {"overdue": "<", "today": "=", "planned": ">"}[activity_timing]
            activities = self.env["mail.activity"].search(
                [
                    ("res_model", "=", "contact.center.case"),
                    ("user_id", "=", self.env.user.id),
                    ("date_deadline", operator, today),
                ]
            )
            cases = self.env["contact.center.case"].search(
                [("id", "in", activities.mapped("res_id"))]
            )
            domain = expression.AND([domain, [("id", "in", cases.channel_id.ids)]])
        return domain
