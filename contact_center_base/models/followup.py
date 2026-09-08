import hashlib
import json
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.osv import expression
from odoo.tools import html2plaintext
from odoo.tools.mail import plaintext2html

from ..services.dto import SCHEMA_VERSION
from .productivity import _PRODUCTIVITY_SERVICE_TOKEN, _canonical_uuid, _plain_text

_PRODUCTIVITY_NOTIFICATION_SUPPRESSION_TOKEN = object()
_ACTIVITY_SUBSCRIPTION_TOKEN = object()
_ACTIVITY_COMPLETION_TOKEN = object()
_ACTIVITY_TARGET_FIELDS = frozenset(("res_id", "res_model", "res_model_id"))
_FOLLOWUP_NOTE_NAMESPACE = uuid.UUID("47821bf4-ce42-44ee-86a9-626d63836b36")


def _payload_sha256(values):
    canonical = json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MailChannelFollowup(models.Model):
    _name = "mail.channel"
    _inherit = ["mail.channel", "mail.activity.mixin"]

    def _message_subscribe(self, partner_ids=None, subtype_ids=None, customer_ids=None):
        if (
            self.env.context.get("contact_center_activity_subscription_token")
            is _ACTIVITY_SUBSCRIPTION_TOKEN
        ):
            conversations = self.filtered(
                lambda channel: channel.channel_type == "contact_center"
            )
            for channel in conversations:
                if customer_ids or set(partner_ids or []) - set(
                    channel.channel_member_ids.partner_id.ids
                ):
                    raise AccessError(
                        _("Follow-up subscriptions cannot add conversation members.")
                    )
            other = self - conversations
            return (
                super(MailChannelFollowup, other)._message_subscribe(
                    partner_ids=partner_ids,
                    subtype_ids=subtype_ids,
                    customer_ids=customer_ids,
                )
                if other
                else True
            )
        return super()._message_subscribe(
            partner_ids=partner_ids, subtype_ids=subtype_ids, customer_ids=customer_ids
        )

    def message_post_with_view(self, views_or_xmlid, **kwargs):
        if (
            self.env.context.get("contact_center_activity_completion_token")
            is _ACTIVITY_COMPLETION_TOKEN
            and self.channel_type == "contact_center"
        ):
            self.ensure_one()
            activity = (kwargs.get("values") or {}).get("activity")
            if (
                views_or_xmlid != "mail.message_activity_done"
                or not activity
                or activity.res_model != "mail.channel"
                or activity.res_id != self.id
                or activity.id
                not in self.env.context.get(
                    "contact_center_activity_completion_ids", ()
                )
            ):
                raise AccessError(
                    _("Invalid Contact Center follow-up completion message.")
                )
            feedback = html2plaintext(
                (kwargs.get("values") or {}).get("feedback") or ""
            ).strip()
            body = _(
                "Follow-up completed: %s",
                activity.summary or activity.activity_type_id.name,
            )
            if feedback:
                body += "\n" + feedback
            result = self.env["contact.center.ui.api"].post_internal_note(
                self.id,
                body,
                str(uuid.uuid5(_FOLLOWUP_NOTE_NAMESPACE, str(activity.id))),
            )
            return self.env["mail.message"].browse(result["message"]["message_id"])
        return super().message_post_with_view(views_or_xmlid, **kwargs)

    def _contact_center_reconcile_members(
        self, partner_ids=None, guest_ids=None, allow_empty=False
    ):
        result = super()._contact_center_reconcile_members(
            partner_ids=partner_ids,
            guest_ids=guest_ids,
            allow_empty=allow_empty,
        )
        users = (
            self.env["res.users"]
            .sudo()
            .search(
                [
                    ("partner_id", "in", list(partner_ids or [])),
                    ("active", "=", True),
                    ("share", "=", False),
                ],
                order="id",
            )
        )
        for channel in self:
            allowed = users.filtered(
                lambda user: channel.contact_center_company_id in user.company_ids
            )
            activities = (
                self.env["mail.activity"]
                .sudo()
                .search(
                    [
                        ("res_model", "=", "mail.channel"),
                        ("res_id", "=", channel.id),
                    ]
                )
            )
            reassigned = activities.filtered(
                lambda activity: activity.user_id not in allowed
            )
            if reassigned and not allowed:
                raise ValidationError(
                    _(
                        "Complete or reassign the open follow-ups before removing "
                        "the last attendant from this inbox."
                    )
                )
            preferred = channel.contact_center_responsible_id.filtered(
                lambda user: user in allowed
            )
            if reassigned:
                reassigned.write(
                    {"user_id": (preferred or allowed.sorted("id")[:1]).id}
                )
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

    def action_notify(self):
        channel_ids = self._contact_center_productivity_channel_ids()
        conversations = self.filtered(
            lambda activity: activity.res_model == "mail.channel"
            and activity.res_id in channel_ids
        )
        # Assignment stays an internal activity signal, without creating a
        # conversation message or sending email to the assignee.
        if conversations:
            self.env["bus.bus"]._sendmany(
                [
                    (
                        user.partner_id,
                        "mail.activity/updated",
                        {"activity_created": True},
                    )
                    for user in conversations.user_id
                ]
            )
        remaining = self - conversations
        return (
            super(MailActivityProductivity, remaining).action_notify()
            if remaining
            else True
        )

    @api.model
    def _contact_center_target_from_values(self, values, activity=None):
        """Return the prospective CC channel id, or ``False`` for another model."""

        model_id = values.get(
            "res_model_id", activity.res_model_id.id if activity else False
        )
        model = self.env["ir.model"]
        if type(model_id) is int and model_id > 0:  # noqa: E721
            # Resolve only the technical model name. Record authorization follows
            # under the caller after locking the effective conversation scope.
            model = model.sudo().browse(model_id).exists()
        model_name = model.model if model else False
        if "res_model" in values:
            explicit_model_name = values.get("res_model") or False
            if explicit_model_name != model_name:
                raise ValidationError(
                    _("The activity model identifier and model name do not match.")
                )
        channel_id = values.get("res_id") if "res_id" in values else False
        if activity and "res_id" not in values:
            channel_id = activity.res_id
        if model_name != "mail.channel":
            return False
        if type(channel_id) is not int or channel_id <= 0:  # noqa: E721
            raise ValidationError(
                _("A Contact Center activity requires a valid channel.")
            )
        channel = self.env["mail.channel"].sudo().browse(channel_id).exists()
        return channel_id if channel.channel_type == "contact_center" else False

    @api.model
    def _contact_center_activity_user_from_values(self, values, activity=None):
        if "user_id" in values:
            return values.get("user_id")
        return activity.user_id.id if activity else self.env.user.id

    @api.model
    def _contact_center_lock_activity_scope(self, channel_ids, user_ids=None):
        channel_ids = sorted({int(value) for value in channel_ids if value})
        channels = self.env["mail.channel"].sudo().browse(channel_ids).exists()
        if set(channels.ids) != set(channel_ids) or any(
            channel.channel_type != "contact_center" for channel in channels
        ):
            raise ValidationError(_("The Contact Center conversation does not exist."))
        bindings = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search([("channel_id", "in", channel_ids)])
        )
        if set(bindings.channel_id.ids) != set(channel_ids):
            raise ValidationError(_("The conversation has no active binding."))
        self.env["contact.center.account"]._contact_center_lock_access_topology(
            account_ids=bindings.account_id.ids,
            team_ids=channels.contact_center_access_team_ids.ids,
            user_ids=sorted(set(user_ids or []) | {self.env.user.id}),
            channel_ids=channel_ids,
        )
        channels.invalidate_recordset(
            ["channel_type", "channel_member_ids", "contact_center_company_id"]
        )
        for channel in channels:
            if not self.env.su:
                self.env["contact.center.ui.api"]._authorized_channel(channel.id)
        return channels

    @api.model
    def _contact_center_validate_assignees(self, channels, assignments):
        agent_group = self.env.ref("contact_center_base.group_contact_center_agent")
        by_id = {channel.id: channel for channel in channels}
        bindings = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search([("channel_id", "in", channels.ids)])
        )
        account_by_channel = {
            binding.channel_id.id: binding.account_id for binding in bindings
        }
        for channel_id, user_id in assignments:
            channel = by_id[channel_id]
            user = self.env["res.users"].sudo().browse(user_id).exists()
            account = account_by_channel.get(channel_id)
            if (
                not user
                or not user.active
                or user.share
                or agent_group not in user.groups_id
                or channel.contact_center_company_id not in user.company_ids
                or not account
                or user not in account._contact_center_effective_users()
                or user.partner_id not in channel.channel_member_ids.partner_id
            ):
                raise ValidationError(
                    _("The activity assignee is outside this inbox access scope.")
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
    def _contact_center_snapshot_values(self, activity, channel):
        return {
            "id": activity.id,
            "channel_id": channel.id,
            "channel_name": channel.display_name,
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
            channel = self.env["mail.channel"].sudo().browse(activity.res_id).exists()
            if (
                activity.res_model != "mail.channel"
                or not channel
                or receipt.channel_id != channel
                or receipt.activity_id != activity
            ):
                raise ValidationError(
                    _("The follow-up receipt failed its integrity check.")
                )
            receipt._service_write(
                {
                    "activity_snapshot_json": self._contact_center_snapshot_values(
                        activity, channel
                    )
                }
            )
        return True

    def _contact_center_productivity_channel_ids(self):
        channel_ids = {
            activity.res_id
            for activity in self
            if activity.res_model == "mail.channel" and activity.res_id
        }
        if not channel_ids:
            return set()
        channels = (
            self.env["mail.channel"]
            .sudo()
            .with_context(active_test=False)
            .browse(sorted(channel_ids))
            .exists()
        )
        return set(
            channels.filtered(
                lambda channel: channel.channel_type == "contact_center"
            ).ids
        )

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
        defaults = self.default_get(["res_model_id", "res_model", "res_id", "user_id"])
        effective_values = []
        channel_ids = []
        assignments = []
        for supplied in vals_list:
            values = dict(defaults, **supplied)
            if "res_model" not in supplied:
                values.pop("res_model", None)
            channel_id = self._contact_center_target_from_values(values)
            # res_model is a read-only projection of the validated foreign key.
            values.pop("res_model", None)
            effective_values.append(values)
            if not channel_id:
                continue
            channel_ids.append(channel_id)
            assignments.append(
                (channel_id, self._contact_center_activity_user_from_values(values))
            )
        if channel_ids:
            channels = self._contact_center_lock_activity_scope(
                channel_ids, user_ids=[user_id for _channel_id, user_id in assignments]
            )
            if not self._contact_center_trusted_activity_service():
                self._contact_center_validate_assignees(channels, assignments)
        target = self.with_context(
            contact_center_activity_subscription_token=_ACTIVITY_SUBSCRIPTION_TOKEN
        )
        activities = super(MailActivityProductivity, target).create(effective_values)
        activities = activities.with_context(
            contact_center_activity_subscription_token=None
        )
        activities._contact_center_notify_productivity(
            activities._contact_center_productivity_channel_ids()
        )
        return activities

    def write(self, values):
        channel_ids = self._contact_center_productivity_channel_ids()
        current_channel_by_activity = {
            activity.id: (self._contact_center_target_from_values({}, activity))
            for activity in self
        }
        target_channel_by_activity = {
            activity.id: self._contact_center_target_from_values(values, activity)
            for activity in self
        }
        channel_ids = set(current_channel_by_activity.values()) | set(
            target_channel_by_activity.values()
        )
        channel_ids.discard(False)
        assignments = []
        for activity in self:
            target_channel_id = target_channel_by_activity[activity.id]
            if target_channel_id:
                assignments.append(
                    (
                        target_channel_id,
                        self._contact_center_activity_user_from_values(
                            values, activity
                        ),
                    )
                )
        receipts = self.env["contact.center.followup.request"]
        if channel_ids:
            channels = self._contact_center_lock_activity_scope(
                channel_ids, user_ids=[user_id for _channel_id, user_id in assignments]
            )
            if not self._contact_center_trusted_activity_service():
                receipts = self._contact_center_lock_activities_and_receipts(self.ids)
                if _ACTIVITY_TARGET_FIELDS & set(values):
                    protected_ids = set(receipts.mapped("activity_record_id"))
                    for activity in self:
                        if (
                            activity.id in protected_ids
                            and current_channel_by_activity[activity.id]
                            != target_channel_by_activity[activity.id]
                        ):
                            raise AccessError(
                                _(
                                    "A receipted Contact Center follow-up cannot be "
                                    "retargeted."
                                )
                            )
                self._contact_center_validate_assignees(channels, assignments)
        target = self.with_context(
            contact_center_activity_subscription_token=_ACTIVITY_SUBSCRIPTION_TOKEN
        )
        result = super(MailActivityProductivity, target).write(values)
        if receipts:
            self._contact_center_sync_active_receipts(receipts)
        channel_ids.update(self._contact_center_productivity_channel_ids())
        self._contact_center_notify_productivity(channel_ids)
        return result

    def unlink(self):
        channel_ids = sorted(self._contact_center_productivity_channel_ids())
        receipts = self.env["contact.center.followup.request"]
        if channel_ids:
            user_ids = [activity.user_id.id for activity in self if activity.user_id]
            self._contact_center_lock_activity_scope(channel_ids, user_ids=user_ids)
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

    def _action_done(self, feedback=False, attachment_ids=None):
        channel_ids = sorted(self._contact_center_productivity_channel_ids())
        if not channel_ids:
            return super()._action_done(
                feedback=feedback, attachment_ids=attachment_ids
            )
        cc_activities = self.filtered(
            lambda activity: activity.res_model == "mail.channel"
            and activity.res_id in channel_ids
        )
        self._contact_center_lock_activity_scope(
            channel_ids, user_ids=cc_activities.user_id.ids
        )
        self._contact_center_lock_activities_and_receipts(cc_activities.ids)
        cc_activities.check_access_rights("write")
        cc_activities.check_access_rule("write")
        if (
            not self.env.su
            and not self.env.user.has_group(
                "contact_center_base.group_contact_center_supervisor"
            )
            and any(activity.user_id != self.env.user for activity in cc_activities)
        ):
            raise AccessError(_("Only the assignee or a supervisor can complete it."))
        if attachment_ids or self.env["ir.attachment"].sudo().search_count(
            [
                ("res_model", "=", "mail.activity"),
                ("res_id", "in", cc_activities.ids),
            ]
        ):
            raise ValidationError(
                _(
                    "Complete Contact Center follow-ups without attachments. "
                    "Keep the files in the conversation draft instead."
                )
            )
        target = self.with_context(
            contact_center_activity_completion_token=_ACTIVITY_COMPLETION_TOKEN,
            contact_center_activity_completion_ids=tuple(cc_activities.ids),
        )
        return super(MailActivityProductivity, target)._action_done(
            feedback=feedback, attachment_ids=attachment_ids
        )


class ContactCenterUiApiFollowup(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def bootstrap(self):
        result = super().bootstrap()
        result["capabilities"]["followups"] = True
        return result

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
    def _activity_types_for_conversations(self):
        return self.env["mail.activity.type"].search(
            [
                ("active", "=", True),
                "|",
                ("res_model", "=", False),
                ("res_model", "=", "mail.channel"),
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
                "channel_id": request.channel_id.id,
                "date_deadline": fields.Date.to_string(deadline),
                "note": note,
                "summary": summary,
                "user_id": user_id,
            }
        )

    @api.model
    def _serialize_activity_productivity(self, activity, channel=None, completed=False):
        channel = channel or self.env["mail.channel"].browse(activity.res_id)
        result = self.env["mail.activity"]._contact_center_snapshot_values(
            activity, channel
        )
        result.update(
            state="completed" if completed else activity.state,
            completed=bool(completed),
            can_complete=bool(
                not completed
                and (
                    activity.user_id == self.env.user
                    or self.env.user.has_group(
                        "contact_center_base.group_contact_center_supervisor"
                    )
                )
            ),
        )
        return result

    @api.model
    def _serialize_followup_request_activity(self, request):
        activity = request.activity_id.exists()
        if request.state == "active" and activity:
            if (
                activity.res_model != "mail.channel"
                or activity.res_id != request.channel_id.id
            ):
                raise ValidationError(
                    _("The follow-up receipt failed its integrity check.")
                )
            return self._serialize_activity_productivity(
                activity, channel=request.channel_id
            )
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
        activities = self.env["mail.activity"].search(
            [("res_model", "=", "mail.channel"), ("res_id", "=", channel.id)],
            order="date_deadline, id",
        )
        result.update(
            activities=[
                self._serialize_activity_productivity(activity, channel=channel)
                for activity in activities
            ],
            activity_types=[
                {
                    "id": activity_type.id,
                    "name": activity_type.display_name,
                    "summary": activity_type.summary or "",
                    "icon": activity_type.icon or "",
                }
                for activity_type in self._activity_types_for_conversations()
            ],
            assignable_users=[
                {"id": user.id, "name": user.display_name}
                for user in self._productivity_assignable_users(channel).sorted(
                    key=lambda user: (user.name, user.id)
                )
            ],
        )
        return result

    @api.model
    def schedule_followup(self, channel_id, values):
        channel, _member = self._authorized_channel(channel_id)
        if not isinstance(values, dict):
            raise ValidationError(_("Follow-up values must be an object."))
        if set(values) - {
            "client_request_id",
            "activity_type_id",
            "date_deadline",
            "summary",
            "note",
            "user_id",
        }:
            raise ValidationError(_("Unsupported follow-up fields."))
        request_id = _canonical_uuid(
            values.get("client_request_id"), _("client request ID")
        )
        request_model = self.env["contact.center.followup.request"].sudo()
        preliminary_receipt = request_model.search(
            [("channel_id", "=", channel.id), ("ui_request_id", "=", request_id)],
            limit=1,
        )
        candidate_user_id = (
            preliminary_receipt.activity_id.user_id.id
            if preliminary_receipt.activity_id
            else self._positive_id(
                values.get("user_id") or self.env.user.id, _("assigned user ID")
            )
        )
        # Fence access authorities before the conversation, also for native activity hooks.
        self.env["mail.activity"]._contact_center_lock_activity_scope(
            channel.ids, user_ids=[candidate_user_id]
        )
        self._productivity_fence_channel(channel)
        channel, _member = self._authorized_channel(channel.id)
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
            activity_type = self._activity_types_for_conversations().filtered(
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
                "channel_id": channel.id,
                "date_deadline": fields.Date.to_string(deadline),
                "note": note,
                "summary": summary,
                "user_id": user.id,
            }
        )
        activity = channel.with_context(
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
        snapshot = self._serialize_activity_productivity(activity, channel=channel)
        request_model.with_context(
            contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
        ).create(
            {
                "channel_id": channel.id,
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
            preliminary_channel = preliminary_receipt.channel_id
        elif preliminary_activity and preliminary_activity.res_model == "mail.channel":
            preliminary_channel = (
                self.env["mail.channel"].browse(preliminary_activity.res_id).exists()
            )
        else:
            preliminary_channel = self.env["mail.channel"]
        if preliminary_channel != channel:
            raise ValidationError(_("The follow-up activity does not exist."))
        self.env["mail.activity"]._contact_center_lock_activity_scope(
            channel.ids,
            user_ids=preliminary_activity.user_id.ids if preliminary_activity else [],
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
        if activity.res_model != "mail.channel":
            raise ValidationError(_("The follow-up activity does not exist."))
        if activity.res_id != channel.id:
            raise ValidationError(_("The follow-up belongs to another conversation."))
        if activity.user_id != self.env.user and not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(_("Only the assignee or a supervisor can complete it."))
        snapshot = self._serialize_activity_productivity(
            activity, channel=channel, completed=True
        )
        if request_receipt:
            if (
                request_receipt.channel_id != channel
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
                    "activity_id": activity.id,
                    "activity_record_id": activity.id,
                    "requested_by_id": activity.create_uid.id,
                    "activity_snapshot_json": self._serialize_activity_productivity(
                        activity, channel=channel
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
        if activity_timing not in ("all", "due", "overdue", "today", "planned"):
            raise ValidationError(_("Unsupported follow-up timing filter."))
        today = fields.Date.context_today(self)
        activity_domain = [
            ("res_model", "=", "mail.channel"),
            ("user_id", "=", self.env.user.id),
        ]
        if activity_timing != "all":
            operator = {"due": "<=", "overdue": "<", "today": "=", "planned": ">"}[
                activity_timing
            ]
            activity_domain.append(("date_deadline", operator, today))
        activities = self.env["mail.activity"].search(activity_domain)
        return expression.AND([domain, [("id", "in", activities.mapped("res_id"))]])
