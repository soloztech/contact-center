import base64

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.base.models.avatar_mixin import get_hsl_from_seed

from ..services.adapter import TransientAdapterError
from ..services.tokens import (
    CONTACT_CENTER_MEMBERSHIP_TOKEN as _CONTACT_CENTER_MEMBERSHIP_TOKEN,
    CONTACT_CENTER_POST_TOKEN as _CONTACT_CENTER_POST_TOKEN,
)

_CONTACT_CENTER_AVATAR = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
<circle cx="64" cy="64" r="64" fill="#455A64"/>
<path d="M31 36h66v43H59L43 94V79H31z" fill="#fff"/>
<circle cx="48" cy="58" r="5" fill="#455A64"/>
<circle cx="64" cy="58" r="5" fill="#455A64"/>
<circle cx="80" cy="58" r="5" fill="#455A64"/>
</svg>"""


def _has_contact_center_channel(env, channel_ids):
    return bool(
        env["mail.channel"]
        .sudo()
        .browse(list(channel_ids))
        .exists()
        .filtered(lambda channel: channel.channel_type == "contact_center")
    )


def _is_flat_relation_value(value):
    return (
        hasattr(value, "_name")
        and hasattr(value, "ids")
        or isinstance(value, tuple)
        or (
            isinstance(value, list)
            and value
            and not isinstance(value[0], (dict, list, tuple))
        )
    )


def _flat_relation_ids(value):
    values = value.ids if hasattr(value, "ids") else value
    if not all(isinstance(channel_id, int) for channel_id in values):
        return None
    return set(values)


def _strict_record_id(value):
    if value in (None, False, ""):
        return False
    if type(value) is int:  # noqa: E721 - bool must not pass as an integer ID
        return value if value > 0 else None
    if (
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and not value.startswith("0")
    ):
        return int(value)
    return None


def _relation_command_effect(command):
    """Return (unsafe, referenced ids) for one Odoo x2many command."""

    if isinstance(command, dict):
        return command.get("channel_type") == "contact_center", set()
    if not isinstance(command, (list, tuple)) or not command:
        return True, set()
    operation = command[0]
    if operation == 0:
        if len(command) <= 2 or not isinstance(command[2], dict):
            return True, set()
        return command[2].get("channel_type") == "contact_center", set()
    if operation in (1, 2, 3, 4):
        if len(command) <= 1 or not isinstance(command[1], int):
            return True, set()
        return False, {command[1]}
    if operation == 5:
        return False, set()
    if operation == 6:
        if len(command) <= 2 or not isinstance(command[2], (list, tuple)):
            return True, set()
        channel_ids = _flat_relation_ids(command[2])
        return channel_ids is None, channel_ids or set()
    return True, set()


def _membership_commands_touch_contact_center(env, records, commands):
    """Detect relation commands that would add/remove a contact center channel."""

    current_by_record = {
        record.id: set(
            record.sudo()
            .channel_ids.filtered(
                lambda channel: channel.channel_type == "contact_center"
            )
            .ids
        )
        for record in records
    }
    # Once a partner/guest participates in Contact Center, every generic inverse
    # relation write is rejected.  The application service is the only supported
    # membership writer, so even a nominal no-op must not become a version-specific
    # ORM bypass.
    if any(current_by_record.values()):
        return True
    if commands is False or commands is None:
        return False
    # Odoo 16 interprets a naked tuple, or a flat list of ids, as Command.set.
    if _is_flat_relation_value(commands):
        requested_ids = _flat_relation_ids(commands)
        if requested_ids is None:
            return True
        return _has_contact_center_channel(env, requested_ids)
    if not isinstance(commands, list):
        return True

    referenced_ids = set()
    for command in commands:
        unsafe, command_ids = _relation_command_effect(command)
        if unsafe:
            return True
        referenced_ids.update(command_ids)
    return _has_contact_center_channel(env, referenced_ids)


def _reset_contact_center_channel_type(records):
    """Preserve native channels when the selection value is removed on uninstall."""

    if not records:
        return
    records.flush_recordset(["channel_type"])
    records.env.cr.execute(
        "UPDATE mail_channel SET channel_type = 'group' WHERE id = ANY(%s)",
        [records.ids],
    )
    records.invalidate_recordset(["channel_type"], flush=False)


class MailChannel(models.Model):
    _inherit = "mail.channel"

    channel_type = fields.Selection(
        selection_add=[("contact_center", "Contact Center")],
        ondelete={"contact_center": _reset_contact_center_channel_type},
    )
    is_contact_center = fields.Boolean(
        compute="_compute_is_contact_center", store=False
    )
    contact_center_company_id = fields.Many2one(
        "res.company", index=True, ondelete="restrict"
    )
    contact_center_access_team_ids = fields.Many2many(
        "contact.center.team",
        "contact_center_channel_access_team_rel",
        "channel_id",
        "team_id",
        context={"active_test": False},
        check_company=True,
        domain="[('company_id', '=', contact_center_company_id)]",
    )
    contact_center_access_user_ids = fields.Many2many(
        "res.users",
        "contact_center_channel_access_user_rel",
        "channel_id",
        "user_id",
        string="Contact Center Authorized Users",
        check_company=True,
        context={"active_test": False},
        domain="[('share', '=', False)]",
        help="Access projection of the logical inbox users.",
    )
    contact_center_responsible_id = fields.Many2one(
        "res.users",
        index=True,
        ondelete="set null",
        check_company=True,
        domain="[('share', '=', False)]",
    )
    contact_center_state = fields.Selection(
        [("open", "Open"), ("resolved", "Resolved"), ("archived", "Archived")],
        default=False,
        index=True,
    )
    contact_center_last_message_id = fields.Many2one(
        "mail.message",
        string="Last Contact Center Message",
        index=True,
        copy=False,
        readonly=True,
        ondelete="set null",
    )
    contact_center_last_message_at = fields.Datetime(
        string="Last Contact Center Message At",
        index=True,
        copy=False,
        readonly=True,
    )
    contact_center_tag_ids = fields.Many2many(
        "contact.center.tag",
        "contact_center_channel_tag_rel",
        "channel_id",
        "tag_id",
        string="Contact Center Tags",
    )
    contact_center_binding_ids = fields.One2many(
        "contact.center.channel.binding", "channel_id", string="Contact Center Binding"
    )

    @api.depends("channel_type")
    def _compute_is_contact_center(self):
        for channel in self:
            channel.is_contact_center = channel.channel_type == "contact_center"

    @api.model_create_multi
    def create(self, vals_list):
        default_channel_type = self.default_get(["channel_type"]).get("channel_type")
        creates_contact_center = any(
            (
                values["channel_type"]
                if "channel_type" in values
                else default_channel_type
            )
            == "contact_center"
            for values in vals_list
        )
        if (
            creates_contact_center
            and self.env.context.get("contact_center_membership_token")
            is not _CONTACT_CENTER_MEMBERSHIP_TOKEN
        ):
            raise AccessError(
                _("Contact center channels must be created by the application service.")
            )
        return super().create(vals_list)

    def write(self, values):
        if (
            "active" in values
            and self.env.context.get("contact_center_membership_token")
            is not _CONTACT_CENTER_MEMBERSHIP_TOKEN
            and any(channel.channel_type == "contact_center" for channel in self)
        ):
            raise AccessError(
                _(
                    "Archive or reopen conversations from the Contact Center inbox. "
                    "Native channel archival would disable incoming messages."
                )
            )
        protected_fields = {
            "contact_center_company_id",
            "contact_center_access_team_ids",
            "contact_center_access_user_ids",
            "contact_center_responsible_id",
            "contact_center_state",
            "contact_center_tag_ids",
            "contact_center_last_message_id",
            "contact_center_last_message_at",
        }
        if (
            protected_fields & set(values)
            and self.env.context.get("contact_center_membership_token")
            is not _CONTACT_CENTER_MEMBERSHIP_TOKEN
            and any(channel.channel_type == "contact_center" for channel in self)
        ):
            raise AccessError(
                _(
                    "Contact center assignment and operational fields are managed "
                    "by the application service."
                )
            )
        if (
            "channel_type" in values
            and self.env.context.get("contact_center_membership_token")
            is not _CONTACT_CENTER_MEMBERSHIP_TOKEN
            and any(
                channel.channel_type != values["channel_type"]
                and (
                    channel.channel_type == "contact_center"
                    or values["channel_type"] == "contact_center"
                )
                for channel in self
            )
        ):
            raise AccessError(
                _("The contact center channel type is managed by the application.")
            )
        return super().write(values)

    def unlink(self):
        if self.env.context.get(
            "contact_center_membership_token"
        ) is not _CONTACT_CENTER_MEMBERSHIP_TOKEN and any(
            channel.channel_type == "contact_center" for channel in self
        ):
            raise AccessError(
                _(
                    "Contact center conversations cannot be deleted in this phase; "
                    "archive them instead."
                )
            )
        if self.ids:
            self.env.cr.execute(
                "SELECT id FROM mail_channel WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                [sorted(self.ids)],
            )
        avatars = self.sudo().mapped(
            "contact_center_binding_ids.group_profile_ids.avatar_attachment_id"
        )
        result = super().unlink()
        avatars.exists().sudo().unlink()
        return result

    def add_members(
        self,
        partner_ids=None,
        guest_ids=None,
        invite_to_rtc_call=False,
        open_chat_window=False,
        post_joined_message=True,
    ):
        if (
            any(channel.channel_type == "contact_center" for channel in self)
            and self.env.context.get("contact_center_membership_token")
            is not _CONTACT_CENTER_MEMBERSHIP_TOKEN
        ):
            raise AccessError(
                _("Contact center membership is managed by the application service.")
            )
        return super().add_members(
            partner_ids=partner_ids,
            guest_ids=guest_ids,
            invite_to_rtc_call=invite_to_rtc_call,
            open_chat_window=open_chat_window,
            post_joined_message=post_joined_message,
        )

    def _can_invite(self, partner_id):
        if (
            any(channel.channel_type == "contact_center" for channel in self)
            and self.env.context.get("contact_center_membership_token")
            is not _CONTACT_CENTER_MEMBERSHIP_TOKEN
        ):
            return False
        return super()._can_invite(partner_id)

    def _action_unfollow(self, partner):
        if (
            any(channel.channel_type == "contact_center" for channel in self)
            and self.env.context.get("contact_center_membership_token")
            is not _CONTACT_CENTER_MEMBERSHIP_TOKEN
        ):
            raise AccessError(
                _("Contact center membership is managed by the application service.")
            )
        return super()._action_unfollow(partner)

    def _generate_avatar(self):
        self.ensure_one()
        if self.channel_type != "contact_center":
            return super()._generate_avatar()
        avatar = _CONTACT_CENTER_AVATAR.replace(
            'fill="#455A64"', 'fill="%s"' % get_hsl_from_seed(self.uuid), 1
        )
        return base64.b64encode(avatar.encode())

    @api.constrains(
        "channel_type",
        "contact_center_company_id",
        "contact_center_access_team_ids",
        "contact_center_access_user_ids",
        "contact_center_responsible_id",
        "contact_center_state",
        "contact_center_tag_ids",
        "contact_center_last_message_id",
        "contact_center_last_message_at",
    )
    def _check_contact_center_configuration(self):
        for channel in self:
            if (
                channel.channel_type == "contact_center"
                and not channel.contact_center_company_id
            ):
                raise ValidationError(_("Contact center channels require a company."))
            if (
                channel.channel_type == "contact_center"
                and not channel.contact_center_state
            ):
                raise ValidationError(
                    _("Contact center channels require a service state.")
                )
            if channel.channel_type != "contact_center" and (
                channel.contact_center_company_id
                or channel.contact_center_access_team_ids
                or channel.contact_center_access_user_ids
                or channel.contact_center_responsible_id
                or channel.contact_center_state
                or channel.contact_center_tag_ids
                or channel.contact_center_last_message_id
                or channel.contact_center_last_message_at
            ):
                raise ValidationError(
                    _(
                        "Contact center fields can only be used on contact center channels."
                    )
                )
            if any(
                team.company_id != channel.contact_center_company_id
                for team in channel.contact_center_access_team_ids
            ):
                raise ValidationError(_("The channel team belongs to another company."))
            if (
                channel.contact_center_responsible_id
                and channel.contact_center_company_id
                not in channel.contact_center_responsible_id.company_ids
            ):
                raise ValidationError(
                    _("The responsible agent cannot access the channel company.")
                )
            if any(
                not user.active
                or user.share
                or channel.contact_center_company_id not in user.company_ids
                for user in channel.contact_center_access_user_ids
            ):
                raise ValidationError(
                    _("An authorized user cannot access the channel company.")
                )
            allowed_users = self.env[
                "contact.center.account"
            ]._contact_center_users_for_access_scope(
                users=channel.contact_center_access_user_ids,
                teams=channel.contact_center_access_team_ids,
            )
            if (
                channel.contact_center_responsible_id
                and channel.contact_center_responsible_id not in allowed_users
            ):
                raise ValidationError(
                    _("The responsible agent is outside the inbox access scope.")
                )
            if any(
                tag.company_id != channel.contact_center_company_id
                for tag in channel.contact_center_tag_ids
            ):
                raise ValidationError(
                    _("Channel tags must belong to the channel company.")
                )
            if channel.contact_center_last_message_id and (
                channel.contact_center_last_message_id.model != "mail.channel"
                or channel.contact_center_last_message_id.res_id != channel.id
            ):
                raise ValidationError(
                    _("The last message must belong to the contact center channel.")
                )

    @api.model
    def _contact_center_create_channel(
        self,
        *,
        account,
        identity=None,
        conversation_type="direct",
        name=None,
        teams=None,
        responsible=None,
        partner_ids=None,
        guest_ids=None,
    ):
        """Create a channel and reconcile the exact authorized membership."""

        account.ensure_one()
        if conversation_type not in ("direct", "group", "other"):
            raise ValidationError(_("Unsupported conversation type."))
        identity = identity or self.env["contact.center.identity"]
        if conversation_type == "direct" and not identity:
            raise ValidationError(_("Direct conversations require a remote identity."))
        if identity:
            identity.ensure_one()
            if identity.company_id != account.company_id:
                raise ValidationError(_("The identity belongs to another company."))
        if teams is not None and set(teams.ids) != set(account.access_team_ids.ids):
            raise ValidationError(
                _("A conversation must use every access team configured on its inbox.")
            )
        teams = account.access_team_ids
        users = account.access_user_ids
        responsible = (
            responsible or account.auto_assignment_user_id or self.env["res.users"]
        )
        requested_partner_ids = set(partner_ids or [])
        partner_ids = set()
        guest_ids = set(guest_ids or [])
        if identity:
            guest_ids.add(identity.mail_guest_id.id)
        guests = self.env["mail.guest"].browse(list(guest_ids)).exists()
        if len(guests) != len(guest_ids):
            raise ValidationError(_("Every requested guest must exist."))
        if teams:
            self.env.cr.execute(
                "SELECT id FROM contact_center_team WHERE id = ANY(%s) ORDER BY id FOR SHARE",
                [sorted(teams.ids)],
            )
            teams.invalidate_recordset(["agent_ids", "supervisor_ids", "active"])
            if any(team.company_id != account.company_id for team in teams):
                raise ValidationError(_("An access team belongs to another company."))
            if any(not team.active for team in teams):
                raise ValidationError(_("Every access team must be active."))
        authorized_users = account._contact_center_effective_users()
        partner_ids.update(authorized_users.partner_id.ids)
        if requested_partner_ids - partner_ids:
            raise AccessError(
                _("Conversation members must belong to the inbox access scope.")
            )
        if responsible:
            responsible.ensure_one()
            if responsible not in authorized_users:
                raise ValidationError(
                    _("The responsible agent is outside the inbox access scope.")
                )
        if not partner_ids:
            raise ValidationError(
                _(
                    "A Contact Center conversation requires at least one authorized "
                    "agent member."
                )
            )
        self._contact_center_validate_agent_partners(
            partner_ids, company=account.company_id
        )

        fallback_name = name or (identity and identity.name) or account.name
        member_commands = [
            (0, 0, {"partner_id": partner_id}) for partner_id in sorted(partner_ids)
        ] + [
            (0, 0, {"partner_id": False, "guest_id": guest_id})
            for guest_id in sorted(guest_ids)
        ]
        channel = (
            self.sudo()
            .with_context(
                contact_center_membership_token=_CONTACT_CENTER_MEMBERSHIP_TOKEN
            )
            .create(
                {
                    "name": fallback_name,
                    "channel_type": "contact_center",
                    "contact_center_company_id": account.company_id.id,
                    "contact_center_access_team_ids": [(6, 0, teams.ids)],
                    "contact_center_access_user_ids": [(6, 0, users.ids)],
                    "contact_center_responsible_id": (
                        responsible.id if responsible else False
                    ),
                    "contact_center_state": "open",
                    "contact_center_last_message_at": fields.Datetime.now(),
                    "channel_member_ids": member_commands,
                }
            )
        )

        allowed_partners = self.env["res.partner"].browse(partner_ids)
        allowed_guests = self.env["mail.guest"].browse(guest_ids)
        implicit_members = channel.sudo().channel_member_ids.filtered(
            lambda member: (
                (member.partner_id and member.partner_id not in allowed_partners)
                or (member.guest_id and member.guest_id not in allowed_guests)
            )
        )
        implicit_members.with_context(
            contact_center_membership_token=_CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).unlink()
        channel.invalidate_recordset(["channel_member_ids"])
        return channel.with_context(contact_center_membership_token=None)

    def _contact_center_reconcile_members(
        self, partner_ids=None, guest_ids=None, allow_empty=False
    ):
        """Synchronize membership after assignment or transfer."""

        partner_ids = set(partner_ids or [])
        guest_ids = set(guest_ids or [])
        if not partner_ids and not allow_empty:
            raise ValidationError(
                _(
                    "A Contact Center conversation requires at least one authorized "
                    "agent member."
                )
            )
        member_model = (
            self.env["mail.channel.member"]
            .sudo()
            .with_context(
                mail_create_bypass_create_check=self.env[
                    "mail.channel.member"
                ]._bypass_create_check,
                contact_center_membership_token=_CONTACT_CENTER_MEMBERSHIP_TOKEN,
            )
        )
        for channel in self:
            if channel.channel_type != "contact_center":
                raise ValidationError(
                    _("Membership reconciliation requires a contact center channel.")
                )
            self.env.cr.execute(
                "SELECT id FROM mail_channel WHERE id = %s FOR UPDATE", [channel.id]
            )
            self._contact_center_validate_agent_partners(
                partner_ids, company=channel.contact_center_company_id
            )
            existing = channel.sudo().channel_member_ids
            existing_partner_ids = set(existing.partner_id.ids)
            existing_guest_ids = set(existing.guest_id.ids)
            create_values = [
                {"channel_id": channel.id, "partner_id": partner_id}
                for partner_id in partner_ids - existing_partner_ids
            ] + [
                {"channel_id": channel.id, "guest_id": guest_id}
                for guest_id in guest_ids - existing_guest_ids
            ]
            if create_values:
                member_model.create(create_values)
            existing.filtered(
                lambda member: (
                    (member.partner_id and member.partner_id.id not in partner_ids)
                    or (member.guest_id and member.guest_id.id not in guest_ids)
                )
            ).with_context(
                contact_center_membership_token=_CONTACT_CENTER_MEMBERSHIP_TOKEN
            ).unlink()
        return True

    @api.model
    def _contact_center_validate_agent_partners(self, partner_ids, company=None):
        partners = self.env["res.partner"].browse(list(partner_ids)).exists()
        if len(partners) != len(set(partner_ids)):
            raise ValidationError(_("Every requested agent partner must exist."))
        agent_group = self.env.ref("contact_center_base.group_contact_center_agent")
        invalid_partners = self.env["res.partner"]
        for partner in partners:
            active_users = partner.user_ids.filtered("active")
            if len(active_users) != 1 or any(
                user.share
                or agent_group not in user.groups_id
                or (company and company not in user.company_ids)
                for user in active_users
            ):
                invalid_partners |= partner
        if invalid_partners:
            raise AccessError(
                _(
                    "Only Contact Center agents can be channel members: %s",
                    ", ".join(invalid_partners.mapped("display_name")),
                )
            )
        return partners

    @api.returns("mail.message", lambda value: value.id)
    def message_post(
        self,
        *,
        message_type="notification",
        subtype_xmlid=None,
        subtype_id=False,
        **kwargs,
    ):
        self.ensure_one()
        if self.channel_type == "contact_center":
            comment_subtype_id = self.env.ref("mail.mt_comment").id
            note_subtype_id = self.env.ref("mail.mt_note").id
            is_external_comment = message_type == "comment" and (
                subtype_xmlid == "mail.mt_comment" or subtype_id == comment_subtype_id
            )
            is_explicit_core_post = (
                self.env.context.get("contact_center_post_token")
                is _CONTACT_CENTER_POST_TOKEN
            )
            is_internal_note = (
                message_type == "comment"
                and subtype_xmlid in (None, "mail.mt_note")
                and subtype_id in (False, note_subtype_id)
            )
            has_explicit_recipients = any(
                kwargs.get(field_name)
                for field_name in ("partner_ids", "email_to", "email_cc")
            )
            has_explicit_author = any(
                field_name in kwargs
                for field_name in ("author_id", "author_guest_id", "email_from")
            )
            if is_explicit_core_post and (
                not is_external_comment or has_explicit_recipients
            ):
                raise UserError(
                    _(
                        "Contact Center application posts must be external comments "
                        "without native recipients."
                    )
                )
            if not is_explicit_core_post and (
                not is_internal_note or has_explicit_recipients or has_explicit_author
            ):
                raise UserError(
                    _(
                        "Only recipient-free internal notes may use native "
                        "message_post on a Contact Center conversation."
                    )
                )
        target = self
        if self.channel_type == "contact_center":
            target = self.with_context(
                contact_center_membership_token=_CONTACT_CENTER_MEMBERSHIP_TOKEN,
                contact_center_post_token=_CONTACT_CENTER_POST_TOKEN,
            )
        message = super(MailChannel, target).message_post(
            message_type=message_type,
            subtype_xmlid=subtype_xmlid,
            subtype_id=subtype_id,
            **kwargs,
        )
        return message.with_context(
            contact_center_membership_token=None,
            contact_center_post_token=None,
        )

    def _contact_center_post(self, origin, **message_values):
        self.ensure_one()
        if self.channel_type != "contact_center":
            raise ValidationError(
                _("This operation requires a contact center channel.")
            )
        if origin not in ("inbound", "outbound", "external_device"):
            raise ValidationError(_("Invalid contact center message origin."))
        return self.with_context(
            contact_center_post_token=_CONTACT_CENTER_POST_TOKEN,
            contact_center_post_origin=origin,
        ).message_post(**message_values)

    def _channel_message_notifications(self, message, message_format=False):
        native_channels = self.filtered(
            lambda channel: channel.channel_type != "contact_center"
        )
        if not native_channels:
            return []
        return super(MailChannel, native_channels)._channel_message_notifications(
            message, message_format=message_format
        )

    def _channel_fetch_message(self, last_id=False, limit=20):
        self.ensure_one()
        if self.channel_type == "contact_center":
            return []
        return super()._channel_fetch_message(last_id=last_id, limit=limit)

    def _contact_center_reject_native_discuss_mutation(self):
        if any(channel.channel_type == "contact_center" for channel in self):
            raise AccessError(
                _(
                    "Contact Center conversations are managed by the dedicated "
                    "inbox, not by Discuss."
                )
            )

    def channel_fold(self, state=None):
        self._contact_center_reject_native_discuss_mutation()
        return super().channel_fold(state=state)

    def channel_pin(self, pinned=False):
        self._contact_center_reject_native_discuss_mutation()
        return super().channel_pin(pinned=pinned)

    def channel_set_custom_name(self, name):
        self._contact_center_reject_native_discuss_mutation()
        return super().channel_set_custom_name(name)

    def channel_rename(self, name):
        self._contact_center_reject_native_discuss_mutation()
        return super().channel_rename(name)

    def channel_change_description(self, description):
        self._contact_center_reject_native_discuss_mutation()
        return super().channel_change_description(description)

    def _contact_center_member_for_current_user(self):
        self.ensure_one()
        if self.channel_type != "contact_center":
            raise AccessError(_("This is not a contact center channel."))
        member = self.env["mail.channel.member"].search(
            [
                ("channel_id", "=", self.id),
                ("partner_id", "=", self.env.user.partner_id.id),
            ],
            limit=1,
        )
        if not member:
            raise AccessError(_("You are not a member of this conversation."))
        return member


class MailChannelMember(models.Model):
    _inherit = "mail.channel.member"

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("contact_center_membership_token")
            is not _CONTACT_CENTER_MEMBERSHIP_TOKEN
        ):
            default_channel_id = self.default_get(["channel_id"]).get("channel_id")
            channel_ids = {
                values.get("channel_id", default_channel_id)
                for values in vals_list
                if values.get("channel_id", default_channel_id)
            }
            normalized_channel_ids = set()
            for channel_id in channel_ids:
                normalized_channel_id = _strict_record_id(channel_id)
                if normalized_channel_id is None:
                    raise AccessError(
                        _("Channel membership requires a canonical integer channel ID.")
                    )
                if normalized_channel_id:
                    normalized_channel_ids.add(normalized_channel_id)
            channels = (
                self.env["mail.channel"]
                .sudo()
                .browse(list(normalized_channel_ids))
                .exists()
            )
            if any(channel.channel_type == "contact_center" for channel in channels):
                raise AccessError(
                    _(
                        "Contact center membership is managed by the application service."
                    )
                )
        return super().create(vals_list)

    def unlink(self):
        if self.env.context.get(
            "contact_center_membership_token"
        ) is not _CONTACT_CENTER_MEMBERSHIP_TOKEN and any(
            channel.channel_type == "contact_center"
            for channel in self.sudo().channel_id
        ):
            raise AccessError(
                _("Contact center membership is managed by the application service.")
            )
        return super().unlink()

    def write(self, values):
        membership_fields = {"channel_id", "partner_id", "guest_id"}
        pointer_fields = {
            "fetched_message_id",
            "seen_message_id",
            "last_seen_dt",
        }
        destination_channel = self.env["mail.channel"]
        if "channel_id" in values:
            destination_channel_id = _strict_record_id(values["channel_id"])
            if destination_channel_id is None:
                raise AccessError(
                    _("Channel membership requires a canonical integer channel ID.")
                )
            if destination_channel_id:
                destination_channel = (
                    self.env["mail.channel"]
                    .sudo()
                    .browse(destination_channel_id)
                    .exists()
                )
        touches_contact_center_membership = bool(membership_fields & set(values)) and (
            any(
                channel.channel_type == "contact_center"
                for channel in self.sudo().channel_id
            )
            or destination_channel.channel_type == "contact_center"
        )
        if (
            (pointer_fields & set(values) or touches_contact_center_membership)
            and self.env.context.get("contact_center_membership_token")
            is not _CONTACT_CENTER_MEMBERSHIP_TOKEN
            and (
                touches_contact_center_membership
                or any(
                    channel.channel_type == "contact_center"
                    for channel in self.sudo().channel_id
                )
            )
        ):
            raise AccessError(
                _(
                    "Contact center membership and read pointers are managed by "
                    "the local application API."
                )
            )
        return super().write(values)


class ResPartner(models.Model):
    _inherit = "res.partner"

    @api.model_create_multi
    def create(self, vals_list):
        default_commands = self.default_get(["channel_ids"]).get("channel_ids")
        if self.env.context.get(
            "contact_center_membership_token"
        ) is not _CONTACT_CENTER_MEMBERSHIP_TOKEN and any(
            _membership_commands_touch_contact_center(
                self.env,
                self.browse(),
                values.get("channel_ids", default_commands),
            )
            for values in vals_list
            if "channel_ids" in values or default_commands is not None
        ):
            raise AccessError(
                _("Contact center membership is managed by the application service.")
            )
        return super().create(vals_list)

    def write(self, values):
        if (
            "channel_ids" in values
            and self.env.context.get("contact_center_membership_token")
            is not _CONTACT_CENTER_MEMBERSHIP_TOKEN
            and _membership_commands_touch_contact_center(
                self.env, self, values["channel_ids"]
            )
        ):
            raise AccessError(
                _("Contact center membership is managed by the application service.")
            )
        return super().write(values)


class BasePartnerMergeAutomaticWizard(models.TransientModel):
    _inherit = "base.partner.merge.automatic.wizard"

    def _contact_center_merge_scope(self, partner_ids, dst_partner):
        partner_model = self.env["res.partner"].sudo().with_context(active_test=False)
        normalized_ids = partner_ids.ids if hasattr(partner_ids, "ids") else partner_ids
        partners = partner_model.browse(normalized_ids).exists()
        if len(partners) < 2:
            return partners, partner_model
        destination = dst_partner.sudo().exists() if dst_partner else partner_model
        if not destination or destination not in partners:
            destination = self._get_ordered_partner(partners.ids)[-1].sudo()
        return partners, destination

    @api.model
    def _contact_center_merge_partner_projection(self, partners, destination):
        """Mirror the structural subset selected by core ``_update_values``."""

        ordered = list(partners - destination) + [destination]
        projection = {}
        for field_name in ("active", "company_id", "is_company", "type"):
            selected = False
            for partner in ordered:
                value = partner[field_name]
                if value:
                    selected = value.id if hasattr(value, "id") else value
            projection[field_name] = selected
        projection["active"] = bool(projection["active"])
        projection["is_company"] = bool(projection["is_company"])
        return projection

    def _contact_center_partner_merge_identities(self, partners):
        return (
            self.env["contact.center.identity"]
            .sudo()
            .with_context(active_test=False)
            .search([("partner_id", "in", partners.ids)], order="id")
        )

    def _contact_center_validate_partner_merge(self, partners, destination, identities):
        projection = self._contact_center_merge_partner_projection(
            partners, destination
        )
        users = (
            self.env["res.users"]
            .sudo()
            .with_context(active_test=False)
            .search([("partner_id", "in", partners.ids)])
        )
        if len(users) > 1:
            raise ValidationError(
                _(
                    "Contacts belonging to different Odoo users cannot be merged; "
                    "resolve the user accounts first."
                )
            )
        if identities and users.filtered(lambda user: user.active and not user.share):
            raise ValidationError(
                _(
                    "The merged contact would become an internal user contact and "
                    "cannot remain linked to a Contact Center identity."
                )
            )
        for identity in identities:
            identity._contact_center_validate_partner_link(
                destination,
                identity.partner_link_kind,
                values=projection,
            )
        return identities

    def _contact_center_reconcile_merged_memberships(self, channel_ids):
        channels = (
            self.env["mail.channel"]
            .sudo()
            .with_context(active_test=False)
            .browse(channel_ids)
            .exists()
        )
        for channel in channels.sorted("id"):
            bindings = channel.contact_center_binding_ids.filtered(
                lambda binding: binding.active and not binding.merged_into_id
            )
            account = bindings[:1].account_id
            if account and account.active:
                users = account._contact_center_effective_users()
            else:
                users = channel.contact_center_access_user_ids
                if channel.contact_center_access_team_ids:
                    users |= (
                        channel.contact_center_access_team_ids.agent_ids
                        | channel.contact_center_access_team_ids.supervisor_ids
                    )
                users = users.filtered(
                    lambda user: user.active
                    and not user.share
                    and channel.contact_center_company_id in user.company_ids
                )
            channel._contact_center_reconcile_members(
                partner_ids=users.partner_id.ids,
                guest_ids=channel.channel_member_ids.guest_id.ids,
            )
        return True

    def _merge(self, partner_ids, dst_partner=None, extra_checks=True):
        partners, destination = self._contact_center_merge_scope(
            partner_ids, dst_partner
        )
        if len(partners) < 2:
            return super()._merge(
                partner_ids,
                dst_partner=dst_partner,
                extra_checks=extra_checks,
            )
        channels = (
            self.env["mail.channel"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("channel_type", "=", "contact_center"),
                    ("channel_member_ids.partner_id", "in", partners.ids),
                ]
            )
        )
        # Reject an obviously incompatible merge before taking write locks.  The
        # same contract is evaluated again below while holding the canonical
        # partner -> identity locks; the first pass is only an early diagnostic,
        # never the concurrency authority.
        identities = self._contact_center_partner_merge_identities(partners)
        self._contact_center_validate_partner_merge(partners, destination, identities)
        locked_identities = partners._contact_center_lock_linked_identities(touch=True)
        partners.invalidate_recordset(
            ["active", "company_id", "company_type", "is_company", "type", "user_ids"]
        )
        destination_id = destination.id
        destination = partners.filtered(lambda partner: partner.id == destination_id)
        destination.ensure_one()
        self._contact_center_validate_partner_merge(
            partners, destination, locked_identities
        )
        result = super()._merge(
            partners.ids,
            dst_partner=destination,
            extra_checks=extra_checks,
        )
        destination = (
            self.env["res.partner"]
            .sudo()
            .with_context(active_test=False)
            .browse(destination_id)
            .exists()
        )
        if not destination:
            raise ValidationError(_("The Contact Center merge destination was lost."))
        identities = (
            self.env["contact.center.identity"]
            .sudo()
            .with_context(active_test=False)
            .search([("partner_id", "=", destination.id)])
        )
        destination._contact_center_validate_linked_identities(identities)
        self._contact_center_reconcile_merged_memberships(channels.ids)
        return result


class MailGuest(models.Model):
    _inherit = "mail.guest"

    @api.model_create_multi
    def create(self, vals_list):
        default_commands = self.default_get(["channel_ids"]).get("channel_ids")
        if self.env.context.get(
            "contact_center_membership_token"
        ) is not _CONTACT_CENTER_MEMBERSHIP_TOKEN and any(
            _membership_commands_touch_contact_center(
                self.env,
                self.browse(),
                values.get("channel_ids", default_commands),
            )
            for values in vals_list
            if "channel_ids" in values or default_commands is not None
        ):
            raise AccessError(
                _("Contact center membership is managed by the application service.")
            )
        return super().create(vals_list)

    def write(self, values):
        if (
            "channel_ids" in values
            and self.env.context.get("contact_center_membership_token")
            is not _CONTACT_CENTER_MEMBERSHIP_TOKEN
            and _membership_commands_touch_contact_center(
                self.env, self, values["channel_ids"]
            )
        ):
            raise AccessError(
                _("Contact center membership is managed by the application service.")
            )
        return super().write(values)


class ContactCenterChannelBinding(models.Model):
    _name = "contact.center.channel.binding"
    _description = "Contact Center Channel Binding"
    _order = "channel_id"

    channel_id = fields.Many2one(
        "mail.channel", required=True, index=True, ondelete="cascade"
    )
    account_id = fields.Many2one(
        "contact.center.account", required=True, index=True, ondelete="restrict"
    )
    company_id = fields.Many2one(
        related="account_id.company_id", store=True, readonly=True, index=True
    )
    identity_id = fields.Many2one(
        "contact.center.identity", index=True, ondelete="restrict"
    )
    conversation_type = fields.Selection(
        [("direct", "Direct"), ("group", "Group"), ("other", "Other")],
        required=True,
        default="direct",
        index=True,
    )
    conversation_ref = fields.Char(required=True, index=True)
    alias_ids = fields.One2many(
        "contact.center.channel.alias",
        "channel_binding_id",
        string="External Addresses",
    )
    merged_into_id = fields.Many2one(
        "contact.center.channel.binding", index=True, copy=False, ondelete="restrict"
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            "channel_unique",
            "unique(channel_id)",
            "A channel can have only one binding.",
        ),
        (
            "merged_binding_not_self",
            "check(merged_into_id IS NULL OR merged_into_id != id)",
            "A channel binding cannot redirect to itself.",
        ),
        (
            "direct_identity_required",
            "check(conversation_type != 'direct' OR identity_id IS NOT NULL)",
            "A direct conversation requires a remote identity.",
        ),
        (
            "group_identity_forbidden",
            "check(conversation_type != 'group' OR identity_id IS NULL)",
            "A group conversation cannot use one participant as its identity.",
        ),
    ]

    def init(self):
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS contact_center_channel_direct_unique
            ON contact_center_channel_binding (account_id, identity_id, conversation_type)
            WHERE conversation_type = 'direct' AND identity_id IS NOT NULL
                AND merged_into_id IS NULL AND active IS TRUE
            """
        )
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS contact_center_channel_group_unique
            ON contact_center_channel_binding (
                account_id, conversation_ref, conversation_type
            )
            WHERE conversation_type = 'group' AND identity_id IS NULL
                AND merged_into_id IS NULL AND active IS TRUE
            """
        )

    @api.constrains(
        "channel_id", "account_id", "identity_id", "conversation_type", "merged_into_id"
    )
    def _check_domain_consistency(self):
        for binding in self:
            if binding.channel_id.channel_type != "contact_center":
                raise ValidationError(_("Bindings require a contact center channel."))
            if (
                binding.channel_id.contact_center_company_id
                != binding.account_id.company_id
            ):
                raise ValidationError(
                    _("The channel and account belong to different companies.")
                )
            if binding.conversation_type == "direct" and not binding.identity_id:
                raise ValidationError(
                    _("Direct conversations require a remote identity.")
                )
            if binding.conversation_type == "group" and binding.identity_id:
                raise ValidationError(
                    _(
                        "Group conversations identify each sender separately and "
                        "cannot have one remote identity."
                    )
                )
            if (
                binding.identity_id
                and binding.identity_id.company_id != binding.account_id.company_id
            ):
                raise ValidationError(
                    _("The identity and account belong to different companies.")
                )
            if (
                binding.merged_into_id
                and binding.merged_into_id.account_id != binding.account_id
            ):
                raise ValidationError(
                    _("Merged bindings must belong to the same account.")
                )

    def _contact_center_ensure_outbound_supported(
        self,
        operation=None,
        has_text=False,
        has_media=False,
        has_reply=False,
        has_structured=False,
    ):
        """Allow only the explicitly opened outbound shape for each channel type."""

        self.ensure_one()
        if self.conversation_type == "direct":
            return True
        if self.conversation_type == "group":
            if operation in ("react", "edit", "delete"):
                return True
            if (
                operation == "upload_media"
                and has_media
                and not has_text
                and not has_reply
            ):
                return True
            if operation == "send_message" and (
                has_text or has_media or has_structured
            ):
                return True
        raise UserError(
            _("This group operation requires an explicit provider capability.")
        )

    def _contact_center_lock_channel_then_binding(self):
        """Acquire the canonical conversation projection locks.

        Any transaction that needs both rows must keep this parent-first order:
        ``mail.channel`` then ``contact.center.channel.binding``.  Group and avatar
        projections use the same order, which prevents an agent send and an inbound
        projection from forming an ABBA deadlock.
        """

        self.ensure_one()
        channel_id = self.channel_id.id
        binding_id = self.id
        if not channel_id or not binding_id:
            return False
        self.env.cr.execute(
            "SELECT id FROM mail_channel WHERE id = %s FOR UPDATE", [channel_id]
        )
        if not self.env.cr.fetchone():
            return False
        self.env.cr.execute(
            "SELECT id FROM contact_center_channel_binding WHERE id = %s FOR UPDATE",
            [binding_id],
        )
        if not self.env.cr.fetchone():
            return False
        self.invalidate_recordset()
        return True

    def _contact_center_lock_identity_channel_binding(self):
        """Lock a direct identity before its canonical conversation rows."""

        self.ensure_one()
        self.invalidate_recordset(["identity_id", "channel_id"])
        identity_id = self.identity_id.id
        if identity_id:
            self.env.cr.execute(
                "SELECT id FROM contact_center_identity WHERE id = %s FOR UPDATE",
                [identity_id],
            )
            if not self.env.cr.fetchone():
                return False
        if not self._contact_center_lock_channel_then_binding():
            return False
        self.invalidate_recordset(["identity_id"])
        if self.identity_id.id != identity_id:
            raise TransientAdapterError(
                "conversation identity changed while acquiring projection locks"
            )
        return True


class ContactCenterChannelAlias(models.Model):
    _name = "contact.center.channel.alias"
    _description = "Contact Center Channel Alias"
    _order = "account_id, namespace, value_normalized"

    channel_binding_id = fields.Many2one(
        "contact.center.channel.binding", required=True, index=True, ondelete="cascade"
    )
    account_id = fields.Many2one(
        "contact.center.account", required=True, index=True, ondelete="restrict"
    )
    company_id = fields.Many2one(
        related="account_id.company_id", store=True, readonly=True, index=True
    )
    namespace = fields.Char(required=True, index=True)
    value_raw = fields.Char(required=True)
    value_normalized = fields.Char(required=True, index=True)
    role = fields.Selection(
        [
            ("primary", "Primary"),
            ("alternate", "Alternate"),
            ("group", "Group"),
            ("routing", "Routing"),
        ],
        required=True,
        default="primary",
    )
    source_field = fields.Char()
    confidence = fields.Selection(
        [
            ("observed", "Observed"),
            ("protocol", "Protocol-validated"),
            ("manual", "Manual"),
        ],
        required=True,
        default="observed",
    )
    first_seen_at = fields.Datetime(required=True, default=fields.Datetime.now)
    last_seen_at = fields.Datetime(required=True, default=fields.Datetime.now)

    _sql_constraints = [
        (
            "account_namespace_value_unique",
            "unique(account_id, namespace, value_normalized)",
            "This conversation address already exists for the account.",
        ),
    ]

    @api.constrains("channel_binding_id", "account_id")
    def _check_account(self):
        for alias in self:
            if alias.channel_binding_id.account_id != alias.account_id:
                raise ValidationError(
                    _("Channel aliases must use the binding account.")
                )
