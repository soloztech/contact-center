import hashlib
import json
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.osv import expression
from odoo.tools import html2plaintext
from odoo.tools.mail import plaintext2html

from odoo.addons.contact_center_base.services.dto import SCHEMA_VERSION
from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_PRODUCTIVITY_TOKEN,
)

_PRODUCTIVITY_SERVICE_TOKEN = CONTACT_CENTER_PRODUCTIVITY_TOKEN
_PRODUCTIVITY_NOTIFICATION_SUPPRESSION_TOKEN = object()
_ACTIVITY_TARGET_FIELDS = frozenset(("res_id", "res_model", "res_model_id"))


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


class MailChannelKanbanProductivity(models.Model):
    _inherit = "mail.channel"

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


class ContactCenterUiApiKanbanProductivity(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def bootstrap(self):
        result = super().bootstrap()
        result["capabilities"]["followups"] = True
        return result

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
    def get_productivity(self, channel_id):
        result = super().get_productivity(channel_id)
        channel, _member = self._authorized_channel(channel_id)
        cases = self._productivity_cases(channel)
        activities = self.env["mail.activity"].search(
            [("res_model", "=", "contact.center.case"), ("res_id", "in", cases.ids)],
            order="date_deadline, id",
        )
        case_by_id = {case.id: case for case in cases}
        activity_types = self._activity_types_for_cases()
        assignable_users = self._productivity_assignable_users(channel)
        result.update(
            {
                "cases": [self._serialize_case_productivity(case) for case in cases],
                "activities": [
                    self._serialize_activity_productivity(
                        activity, case=case_by_id.get(activity.res_id)
                    )
                    for activity in activities
                    if case_by_id.get(activity.res_id)
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
        )
        return result

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
    def _contact_center_apply_activity_timing_filter(self, domain, activity_timing):
        if not activity_timing:
            return domain
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
        return expression.AND([domain, [("id", "in", cases.channel_id.ids)]])
