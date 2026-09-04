import datetime
import logging
import re
import time
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.adapter import (
    AdapterError,
    ProviderPausedError,
    TransientAdapterError,
    adapter_registry,
)
from ..services.tokens import CONTACT_CENTER_MEMBERSHIP_TOKEN

_logger = logging.getLogger(__name__)

_ACTIVE_QUEUE_JOB_STATES = (
    "pending",
    "enqueued",
    "started",
    "wait_dependencies",
)
_CANONICAL_HEALTH_STATES = {
    "connected",
    "degraded",
    "disconnected",
    "authentication_required",
}
_HEALTH_SAFETY_RANK = {
    "connected": 0,
    "degraded": 1,
    "disconnected": 2,
    "authentication_required": 3,
}
_HEALTH_DETAIL_SELECTION = [
    ("healthy", "Healthy"),
    ("metadata_limited", "Metadata Limited"),
    ("unavailable", "Unavailable"),
    ("unreachable", "Unreachable"),
    ("authentication_required", "Authentication Required"),
    ("rate_limited", "Rate Limited"),
    ("provider_paused", "Provider Paused"),
    ("provider_error", "Provider Error"),
    ("identity_mismatch", "Identity Mismatch"),
    ("identity_unverified", "Identity Unverified"),
    ("invalid_response", "Invalid Response"),
    ("internal_error", "Internal Error"),
    ("unknown", "Unknown"),
]
_HEALTH_DETAIL_ALIASES = {
    "connected": "healthy",
    "ok": "healthy",
    "available": "healthy",
    "healthy": "healthy",
    "metadata_limited": "metadata_limited",
    "unavailable": "unavailable",
    "disconnected": "unavailable",
    "unreachable": "unreachable",
    "timeout": "unreachable",
    "network_error": "unreachable",
    "authentication_required": "authentication_required",
    "not_authenticated": "authentication_required",
    "not_logged_in": "authentication_required",
    "logged_out": "authentication_required",
    "unauthorized": "authentication_required",
    "rate_limited": "rate_limited",
    "paused": "provider_paused",
    "provider_paused": "provider_paused",
    "provider_error": "provider_error",
    "identity_mismatch": "identity_mismatch",
    "identity_unverified": "identity_unverified",
    "invalid_response": "invalid_response",
    "internal_error": "internal_error",
    "unknown": "unknown",
}
_SAFE_ERROR_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_OUTBOUND_DISPATCH_INTERNAL_TOKEN = object()
_CONNECTION_ROLE_SWITCH_INTERNAL_TOKEN = object()
_OUTBOX_RECOVERY_BATCH_SIZE = 25
_OUTBOX_RECOVERY_MAX_BATCH_SIZE = 100
_OUTBOX_RECOVERY_PRIORITY = 20
_OUTBOX_RECOVERY_MAX_RETRIES = 5


def _relational_command_ids(commands):
    """Return every existing record ID touched by x2many command values."""

    record_ids = set()
    for command in commands or []:
        if not isinstance(command, (tuple, list)) or not command:
            continue
        operation = command[0]
        if operation in (1, 2, 3, 4) and len(command) > 1 and command[1]:
            record_ids.add(int(command[1]))
        elif operation == 6 and len(command) > 2:
            values = command[2].ids if hasattr(command[2], "ids") else command[2]
            record_ids.update(int(value) for value in values or [] if value)
    return record_ids


def _odoo_datetime(value=None):
    """Return one UTC-naive datetime accepted by Odoo fields."""

    value = value or fields.Datetime.now()
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, datetime.date):
        parsed = datetime.datetime.combine(value, datetime.time.min)
    elif isinstance(value, str):
        try:
            parsed = fields.Datetime.to_datetime(value)
        except ValueError:
            # DTO timestamps are ISO-8601, while Odoo's parser only accepts its
            # server format. Normalize both without trusting a provider zone.
            iso_value = value.strip()
            if iso_value.endswith(("Z", "z")):
                iso_value = iso_value[:-1] + "+00:00"
            parsed = datetime.datetime.fromisoformat(iso_value)
    else:
        raise TypeError("unsupported Contact Center datetime value")
    if parsed.tzinfo:
        parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    # Odoo 16 Datetime fields deliberately use whole-second precision.
    return parsed.replace(microsecond=0)


class ContactCenterTeam(models.Model):
    _name = "contact.center.team"
    _description = "Contact Center Team"
    _order = "name"
    _check_company_auto = True

    name = fields.Char(required=True, translate=True)
    active = fields.Boolean(default=True)
    access_topology_revision = fields.Integer(
        required=True,
        default=0,
        readonly=True,
        copy=False,
        help=(
            "Internal concurrency fence shared by inbox, roster and provider-route "
            "configuration."
        ),
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company,
        ondelete="restrict",
    )
    agent_ids = fields.Many2many(
        "res.users",
        "contact_center_team_user_rel",
        "team_id",
        "user_id",
        string="Agents",
        domain="[('active', '=', True), ('share', '=', False)]",
        context={"active_test": False},
        check_company=True,
    )
    supervisor_ids = fields.Many2many(
        "res.users",
        "contact_center_team_supervisor_rel",
        "team_id",
        "user_id",
        string="Supervisors",
        domain="[('active', '=', True), ('share', '=', False)]",
        context={"active_test": False},
        check_company=True,
    )
    account_ids = fields.One2many(
        "contact.center.account",
        "default_team_id",
        string="Shared Inboxes",
        readonly=True,
    )

    _sql_constraints = [
        (
            "name_company_unique",
            "unique(name, company_id)",
            "Team names must be unique per company.",
        ),
        (
            "access_topology_revision_nonnegative",
            "check(access_topology_revision >= 0)",
            "The access topology revision cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if any("access_topology_revision" in values for values in vals_list):
            raise AccessError(
                _("The Contact Center access topology revision is internal.")
            )
        user_ids = set()
        for values in vals_list:
            user_ids.update(_relational_command_ids(values.get("agent_ids")))
            user_ids.update(_relational_command_ids(values.get("supervisor_ids")))
        self.env["contact.center.account"]._contact_center_lock_access_topology(
            user_ids=user_ids
        )
        return super().create(vals_list)

    @api.model
    def _contact_center_scope_domain(self, user=None, companies=None):
        """Return the operational inbox scope for one internal user.

        Roles grant operations. Team roster membership grants shared inboxes,
        while an inbox owner can also read the roster of that inbox's team.
        """

        user = user or self.env.user
        if companies is None:
            companies = self.env.companies
        return [
            ("company_id", "in", companies.ids),
            "|",
            "|",
            ("agent_ids", "=", user.id),
            ("supervisor_ids", "=", user.id),
            ("account_ids.owner_user_id", "=", user.id),
        ]

    def _contact_center_has_user(self, user=None):
        self.ensure_one()
        user = user or self.env.user
        return user in (self.agent_ids | self.supervisor_ids)

    def _contact_center_roster_error(self, user, required_group, role_label):
        """Explain a fail-closed roster violation without silently changing grants."""

        self.ensure_one()
        reasons = []
        if not user.active:
            reasons.append(_("the user is archived"))
        if user.share:
            reasons.append(_("the user is external/portal"))
        if required_group not in user.groups_id:
            reasons.append(_("the required Contact Center role is missing"))
        if self.company_id not in user.company_ids:
            reasons.append(
                _("the user does not have access to company %(company)s")
                % {"company": self.company_id.display_name}
            )
        return _(
            "%(user)s cannot remain as %(role)s in Contact Center team "
            "%(team)s because %(reasons)s. Remove the user from this team's "
            "roster before changing their access or archiving them."
        ) % {
            "user": user.display_name,
            "role": role_label,
            "team": self.display_name,
            "reasons": ", ".join(reasons) or _("the authorization is invalid"),
        }

    @api.constrains("agent_ids", "supervisor_ids")
    def _check_contact_center_groups(self):
        agent_group = self.env.ref(
            "contact_center_base.group_contact_center_agent", raise_if_not_found=False
        )
        supervisor_group = self.env.ref(
            "contact_center_base.group_contact_center_supervisor",
            raise_if_not_found=False,
        )
        if not agent_group or not supervisor_group:
            return
        for team in self:
            invalid_agents = team.agent_ids.filtered(
                lambda user, team=team: not user.active
                or user.share
                or agent_group not in user.groups_id
                or team.company_id not in user.company_ids
            )
            invalid_supervisors = team.supervisor_ids.filtered(
                lambda user, team=team: not user.active
                or user.share
                or supervisor_group not in user.groups_id
                or team.company_id not in user.company_ids
            )
            if invalid_agents:
                user = invalid_agents.sorted("id")[0]
                raise ValidationError(
                    team._contact_center_roster_error(user, agent_group, _("agent"))
                )
            if invalid_supervisors:
                user = invalid_supervisors.sorted("id")[0]
                raise ValidationError(
                    team._contact_center_roster_error(
                        user, supervisor_group, _("supervisor")
                    )
                )
            self.env["mail.channel"]._contact_center_validate_agent_partners(
                (team.agent_ids | team.supervisor_ids).partner_id.ids,
                company=team.company_id,
            )

    def write(self, values):
        if "access_topology_revision" in values:
            raise AccessError(
                _("The Contact Center access topology revision is internal.")
            )
        if "company_id" in values and any(
            values["company_id"] != team.company_id.id for team in self
        ):
            raise ValidationError(_("A Contact Center team cannot change company."))
        roster_fields = {"agent_ids", "supervisor_ids"} & set(values)
        affected_accounts = self.env["contact.center.account"]
        affected_user_ids = set((self.agent_ids | self.supervisor_ids).ids)
        previous_partner_ids_by_team = {}
        if roster_fields:
            for field_name in roster_fields:
                affected_user_ids.update(
                    _relational_command_ids(values.get(field_name))
                )
            previous_partner_ids_by_team = {
                team.id: (team.agent_ids | team.supervisor_ids).partner_id.ids
                for team in self
            }
            affected_accounts = (
                self.env["contact.center.account"]
                .sudo()
                .with_context(active_test=False)
                .search([("default_team_id", "in", self.ids)])
            )
        if roster_fields or {"active", "company_id"} & set(values):
            self.env["contact.center.account"]._contact_center_lock_access_topology(
                account_ids=affected_accounts.ids,
                team_ids=self.ids,
                user_ids=affected_user_ids,
            )
        if "active" in values and not bool(values["active"]):
            self._contact_center_check_not_referenced("archived")
        result = super().write(values)
        if roster_fields:
            affected_accounts._contact_center_reconcile_auto_assignment()
            affected_accounts._contact_center_validate_live_route_access()
            self._contact_center_reconcile_channels()
            application = self.env["contact.center.application"]
            for connection in affected_accounts.mapped("connection_ids"):
                application._notify_connection_health(
                    connection,
                    invalidate=True,
                    additional_partner_ids=previous_partner_ids_by_team.get(
                        connection.account_id.default_team_id.id, []
                    ),
                )
        return result

    def _contact_center_reconcile_channels(self):
        for team in self:
            accounts = (
                self.env["contact.center.account"]
                .sudo()
                .with_context(active_test=False)
                .search([("default_team_id", "=", team.id)])
            )
            accounts._contact_center_reconcile_channels()
        return True

    def _contact_center_check_not_referenced(self, operation):
        assigned = (
            self.env["mail.channel"]
            .sudo()
            .with_context(active_test=False)
            .search_count(
                [
                    ("channel_type", "=", "contact_center"),
                    ("contact_center_team_id", "in", self.ids),
                ]
            )
        )
        default_for_account = (
            self.env["contact.center.account"]
            .sudo()
            .with_context(active_test=False)
            .search_count([("default_team_id", "in", self.ids)])
        )
        if assigned or default_for_account:
            raise ValidationError(
                _(
                    "A team assigned to conversations or accounts cannot be %s.",
                    operation,
                )
            )
        return True

    def unlink(self):
        self._contact_center_check_not_referenced("deleted")
        return super().unlink()


class ContactCenterTag(models.Model):
    _name = "contact.center.tag"
    _description = "Contact Center Tag"
    _order = "name"
    _check_company_auto = True

    name = fields.Char(required=True, translate=True)
    color = fields.Integer(default=0)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company,
        ondelete="restrict",
    )

    _sql_constraints = [
        (
            "name_company_unique",
            "unique(name, company_id)",
            "Tag names must be unique per company.",
        ),
    ]

    def write(self, values):
        if "company_id" in values and any(
            values["company_id"] != tag.company_id.id for tag in self
        ):
            raise ValidationError(_("A Contact Center tag cannot change company."))
        return super().write(values)


class ResUsers(models.Model):
    _inherit = "res.users"

    contact_center_access_topology_revision = fields.Integer(
        required=True,
        default=0,
        readonly=True,
        copy=False,
        help="Internal Contact Center access-topology concurrency fence.",
    )

    _sql_constraints = [
        (
            "contact_center_access_topology_revision_nonnegative",
            "check(contact_center_access_topology_revision >= 0)",
            "The Contact Center access topology revision cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if any(
            "contact_center_access_topology_revision" in values for values in vals_list
        ):
            raise AccessError(
                _("The Contact Center access topology revision is internal.")
            )
        users = super().create(vals_list)
        users._contact_center_validate_existing_grants()
        return users

    def write(self, values):
        if "contact_center_access_topology_revision" in values:
            raise AccessError(
                _("The Contact Center access topology revision is internal.")
            )
        value_names = set(values)
        previous_partner_ids = self.partner_id.ids if "partner_id" in values else []
        has_reified_group_field = any(
            field_name.startswith(("in_group_", "sel_groups_"))
            for field_name in value_names
        )
        authorization_fields = {
            "active",
            "share",
            "groups_id",
            "company_ids",
            "partner_id",
        }
        authorization_changed = bool(
            (authorization_fields & value_names) or has_reified_group_field
        )
        if authorization_changed:
            teams, accounts = self._contact_center_access_topology_records(
                extra_partner_ids=previous_partner_ids
            )
            self.env["contact.center.account"]._contact_center_lock_access_topology(
                account_ids=accounts.ids,
                team_ids=teams.ids,
                user_ids=self.ids,
            )
        result = super().write(values)
        if authorization_changed:
            self._contact_center_validate_existing_grants(
                extra_partner_ids=previous_partner_ids
            )
        return result

    def _contact_center_access_topology_records(self, extra_partner_ids=None):
        partner_ids = set(self.partner_id.ids) | set(extra_partner_ids or [])
        teams = (
            self.env["contact.center.team"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    "|",
                    "|",
                    "|",
                    ("agent_ids", "in", self.ids),
                    ("supervisor_ids", "in", self.ids),
                    ("agent_ids.partner_id", "in", list(partner_ids)),
                    ("supervisor_ids.partner_id", "in", list(partner_ids)),
                ]
            )
        )
        accounts = (
            self.env["contact.center.account"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    "|",
                    ("owner_user_id", "in", self.ids),
                    ("default_team_id", "in", teams.ids),
                ]
            )
        )
        return teams, accounts

    def _contact_center_validate_existing_grants(self, extra_partner_ids=None):
        partner_ids = set(self.partner_id.ids) | set(extra_partner_ids or [])
        teams = (
            self.env["contact.center.team"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    "|",
                    "|",
                    "|",
                    ("agent_ids", "in", self.ids),
                    ("supervisor_ids", "in", self.ids),
                    ("agent_ids.partner_id", "in", list(partner_ids)),
                    ("supervisor_ids.partner_id", "in", list(partner_ids)),
                ]
            )
        )
        teams._check_contact_center_groups()
        owned_accounts = (
            self.env["contact.center.account"]
            .sudo()
            .with_context(active_test=False)
            .search([("owner_user_id", "in", self.ids)])
        )
        owned_accounts._check_contact_center_access_configuration()
        channels = (
            self.env["mail.channel"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("channel_type", "=", "contact_center"),
                    ("channel_member_ids.partner_id", "in", list(partner_ids)),
                ]
            )
        )
        for channel in channels:
            channel_partner_ids = partner_ids & set(
                channel.channel_member_ids.partner_id.ids
            )
            channel._contact_center_validate_agent_partners(
                list(channel_partner_ids),
                company=channel.contact_center_company_id,
            )
        return True

    def unlink(self):
        teams_to_lock, accounts_to_lock = self._contact_center_access_topology_records()
        self.env["contact.center.account"]._contact_center_lock_access_topology(
            account_ids=accounts_to_lock.ids,
            team_ids=teams_to_lock.ids,
            user_ids=self.ids,
        )
        owned_accounts = (
            self.env["contact.center.account"]
            .sudo()
            .with_context(active_test=False)
            .search_count([("owner_user_id", "in", self.ids)])
        )
        teams = (
            self.env["contact.center.team"]
            .sudo()
            .with_context(active_test=False)
            .search_count(
                [
                    "|",
                    ("agent_ids", "in", self.ids),
                    ("supervisor_ids", "in", self.ids),
                ]
            )
        )
        channel_memberships = (
            self.env["mail.channel.member"]
            .sudo()
            .search_count(
                [
                    ("channel_id.channel_type", "=", "contact_center"),
                    ("partner_id", "in", self.partner_id.ids),
                ]
            )
        )
        if owned_accounts or teams or channel_memberships:
            raise AccessError(
                _(
                    "Users who own Contact Center inboxes, belong to service teams, "
                    "or hold conversation memberships cannot be deleted before "
                    "their grants are reconciled."
                )
            )
        return super().unlink()


class ResCompany(models.Model):
    _inherit = "res.company"

    def write(self, values):
        previous_users = (
            self.user_ids if "user_ids" in values else self.env["res.users"]
        )
        affected_users = previous_users
        if "user_ids" in values:
            affected_users |= (
                self.env["res.users"]
                .sudo()
                .browse(list(_relational_command_ids(values.get("user_ids"))))
            )
            teams, accounts = affected_users._contact_center_access_topology_records()
            self.env["contact.center.account"]._contact_center_lock_access_topology(
                account_ids=accounts.ids,
                team_ids=teams.ids,
                user_ids=affected_users.ids,
            )
        result = super().write(values)
        if "user_ids" in values:
            (previous_users | self.user_ids)._contact_center_validate_existing_grants()
        return result


class ResGroups(models.Model):
    _inherit = "res.groups"

    def write(self, values):
        if "users" in values:
            protected_groups = self.env.ref(
                "contact_center_base.group_contact_center_agent"
            ) | self.env.ref("contact_center_base.group_contact_center_supervisor")
            effective_groups = self | self.mapped("trans_implied_ids")
            if effective_groups & protected_groups:
                raise AccessError(
                    _(
                        "Manage Contact Center role membership from the user record "
                        "so active conversation grants can be validated."
                    )
                )
        return super().write(values)


class ContactCenterAccount(models.Model):
    _name = "contact.center.account"
    _description = "Contact Center Account"
    _order = "name"
    _check_company_auto = True

    def _default_external_ref(self):
        return str(uuid.uuid4())

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    access_topology_revision = fields.Integer(
        required=True,
        default=0,
        readonly=True,
        copy=False,
        help=(
            "Internal concurrency fence shared by inbox, roster and provider-route "
            "configuration."
        ),
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company,
        ondelete="restrict",
    )
    platform = fields.Char(
        required=True,
        index=True,
        help="Platform-neutral key such as whatsapp, telegram or instagram.",
    )
    external_ref = fields.Char(
        required=True,
        index=True,
        copy=False,
        default=_default_external_ref,
        help="Stable local reference exposed in DTOs.",
    )
    own_external_identity = fields.Char(
        help="The account address on the platform. This is not a remote-person identity."
    )
    technical_author_id = fields.Many2one(
        "res.partner",
        check_company=True,
        domain="[('company_id', 'in', [False, company_id])]",
        help="Optional author for messages sent from another device.",
    )
    owner_user_id = fields.Many2one(
        "res.users",
        string="Inbox Owner",
        index=True,
        ondelete="restrict",
        domain=(
            "[('active', '=', True), ('share', '=', False), "
            "('company_ids', 'in', company_id)]"
        ),
        help=(
            "Optional exclusive owner of this inbox. The owner and every member "
            "of the access team form one union of authorized attendants."
        ),
    )
    default_team_id = fields.Many2one(
        "contact.center.team",
        string="Access Team",
        check_company=True,
        domain="[('company_id', '=', company_id)]",
        help=(
            "Optional shared-access team. Leave it empty for an owner-only inbox. "
            "When owner and team are both set, both scopes can attend."
        ),
    )
    auto_assignment_eligible_user_ids = fields.Many2many(
        "res.users",
        string="Eligible Automatic Assignees",
        compute="_compute_auto_assignment_eligible_user_ids",
        compute_sudo=True,
        help="Active internal users in the effective owner and access-team scope.",
    )
    auto_assignment_user_id = fields.Many2one(
        "res.users",
        string="Automatic Assignee",
        index=True,
        ondelete="set null",
        domain="[('id', 'in', auto_assignment_eligible_user_ids)]",
        help=(
            "Leave empty to keep automatic assignment disabled and let an agent "
            "use Assume. When set, each new inbound conversation or inbound "
            "message assigns an unassigned conversation to this user. The user "
            "must remain in the inbox owner/access-team scope."
        ),
    )
    group_outbound_enabled = fields.Boolean(
        string="Enable Group Sending",
        default=False,
        help=(
            "Allow supported provider connections for this inbox to send new "
            "group messages. Provider capabilities and conversation access are "
            "still enforced."
        ),
    )
    outbound_signature_enabled = fields.Boolean(
        string="Sign Agent Messages",
        default=False,
        help=(
            "Prefix outbound text with the current Odoo agent name on its own "
            "line. Media without a caption is never given an artificial caption."
        ),
    )
    mark_read_enabled = fields.Boolean(
        string="Mark Messages as Read on Provider",
        default=False,
        help=(
            "When an agent views a conversation, allow the provider addon to "
            "enqueue a read receipt. The provider capability and its own policy "
            "are still enforced. Disabled by default."
        ),
    )
    reopen_resolved_on_inbound = fields.Boolean(
        string="Reopen Resolved Conversations on New Messages",
        default=False,
        help=(
            "Automatically move a resolved conversation back to Open when a new "
            "inbound message is received. Duplicate provider events, delivery "
            "receipts, and message mutations never reopen a conversation."
        ),
    )
    show_deleted_message_content = fields.Boolean(
        default=False,
        help=(
            "Keep the content visible in the Contact Center after a delete, marked "
            "as deleted and struck through. When disabled, the operational body, "
            "reactions, and attachments are removed and agents see only a deletion "
            "tombstone. Technical ledger records remain available for correlation, "
            "idempotency, and audit in both modes."
        ),
    )
    group_inbound_enabled = fields.Boolean(
        string="Enable Group Receiving",
        default=False,
        help=(
            "Project new messages received from group participants and allow a "
            "human message sent from this account on an external device to "
            "initialize an unknown group conversation. Provider echoes for "
            "existing conversations, delivery receipts and group metadata remain "
            "processable so outbound correlation stays consistent."
        ),
    )
    attribution_ui_enabled = fields.Boolean(
        string="Show Attribution to Agents",
        default=False,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Show the bounded attribution projection in the operator interface. "
            "Technical identifiers, source URLs and provider evidence remain "
            "restricted to administrators."
        ),
    )
    connection_ids = fields.One2many(
        "contact.center.provider.connection",
        "account_id",
        string="Provider Connections",
    )

    _sql_constraints = [
        (
            "external_ref_unique",
            "unique(external_ref)",
            "The account reference must be unique.",
        ),
        (
            "access_topology_revision_nonnegative",
            "check(access_topology_revision >= 0)",
            "The access topology revision cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if any("access_topology_revision" in values for values in vals_list):
            raise AccessError(
                _("The Contact Center access topology revision is internal.")
            )
        team_ids = {
            int(values["default_team_id"])
            for values in vals_list
            if values.get("default_team_id")
        }
        user_ids = {
            int(values["owner_user_id"])
            for values in vals_list
            if values.get("owner_user_id")
        }
        user_ids.update(
            int(values["auto_assignment_user_id"])
            for values in vals_list
            if values.get("auto_assignment_user_id")
        )
        self._contact_center_lock_access_topology(
            team_ids=team_ids,
            user_ids=user_ids,
        )
        for values in vals_list:
            assignee = self.env["res.users"].browse(
                values.get("auto_assignment_user_id")
            )
            owner = self.env["res.users"].browse(values.get("owner_user_id"))
            team = self.env["contact.center.team"].browse(values.get("default_team_id"))
            self._contact_center_validate_auto_assignment_scope(
                assignee=assignee,
                owner=owner,
                team=team,
            )
        return super().create(vals_list)

    def _contact_center_display_address(self):
        """Return a bounded display-only form of this account's own identity."""

        self.ensure_one()
        raw = str(self.own_external_identity or "").strip()
        if not raw:
            return False

        # WhatsApp phone JIDs can contain a device suffix before the domain.
        # Never infer a telephone number from an opaque LID or another provider.
        local, separator, domain = raw.rpartition("@")
        phone_domain = separator and domain.lower() in ("s.whatsapp.net", "c.us")
        plain_phone = not separator and (self.platform or "").lower() == "whatsapp"
        if phone_domain or plain_phone:
            candidate = (local if separator else raw).split(":", 1)[0]
            compact = re.sub(r"[\s().-]", "", candidate)
            digits = compact[1:] if compact.startswith("+") else compact
            if digits.isdigit() and 7 <= len(digits) <= 20:
                return "+" + digits

        # This value remains data rendered by the UI, but strip controls and
        # HTML metacharacters as defence in depth and cap fleet-list payloads.
        safe = "".join(
            character
            for character in raw
            if character.isprintable() and character not in "<>&\"'"
        )
        safe = " ".join(safe.split()).strip()
        return safe[:80] or False

    def _contact_center_accepts_group_inbound(self):
        """Return whether new human group conversations/messages may be projected."""

        self.ensure_one()
        return bool(self.active and self.group_inbound_enabled)

    @api.model
    def _contact_center_scope_domain(self, user=None, companies=None):
        """Return inboxes directly owned by ``user`` or shared with their team."""

        user = user or self.env.user
        if companies is None:
            companies = self.env.companies
        return [
            ("company_id", "in", companies.ids),
            "|",
            "|",
            ("owner_user_id", "=", user.id),
            ("default_team_id.agent_ids", "=", user.id),
            ("default_team_id.supervisor_ids", "=", user.id),
        ]

    @api.model
    def _contact_center_lock_access_topology(
        self,
        account_ids=None,
        team_ids=None,
        user_ids=None,
        pipeline_ids=None,
        channel_ids=None,
        case_ids=None,
    ):
        """Fence affected access/case-topology rows in one deterministic order.

        Odoo runs at PostgreSQL REPEATABLE READ.  Row locks alone do not refresh an
        already established snapshot, so each locked row also receives a monotonic
        write.  A concurrent roster, inbox-assignment or provider-topology change then
        raises a serialization failure and is retried with a fresh snapshot instead of
        allowing write skew.

        The canonical core order is account -> team -> user -> pipeline -> channel ->
        case -> provider connection.  Only IDs in the affected aggregate are locked;
        there is deliberately no company-wide or table-wide lock.  The provider
        topology helper appends connection locks only after calling this method.

        Pipeline rows carry a dedicated revision.  Channels and cases already have
        stable authority rows, so a no-value MVCC update is enough to make a waiter
        with an old REPEATABLE READ snapshot serialize and retry.  ``FOR UPDATE``
        alone would not provide that guarantee when the conflicting transaction only
        inserted or removed a related row.
        """

        account_ids = sorted({int(value) for value in account_ids or [] if value})
        team_ids = sorted({int(value) for value in team_ids or [] if value})
        user_ids = sorted({int(value) for value in user_ids or [] if value})
        pipeline_ids = sorted({int(value) for value in pipeline_ids or [] if value})
        channel_ids = sorted({int(value) for value in channel_ids or [] if value})
        case_ids = sorted({int(value) for value in case_ids or [] if value})
        accounts = self.sudo().with_context(active_test=False).browse(account_ids)
        teams = (
            self.env["contact.center.team"]
            .sudo()
            .with_context(active_test=False)
            .browse(team_ids)
        )
        users = (
            self.env["res.users"]
            .sudo()
            .with_context(active_test=False)
            .browse(user_ids)
        )
        if account_ids:
            self.flush_model(["access_topology_revision"])
            self.env.cr.execute(
                "SELECT id FROM contact_center_account WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                [account_ids],
            )
            self.env.cr.execute(
                "UPDATE contact_center_account "
                "SET access_topology_revision = access_topology_revision + 1 "
                "WHERE id = ANY(%s)",
                [account_ids],
            )
            accounts.invalidate_recordset(["access_topology_revision"])
        if team_ids:
            teams.flush_model(["access_topology_revision"])
            self.env.cr.execute(
                "SELECT id FROM contact_center_team WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                [team_ids],
            )
            self.env.cr.execute(
                "UPDATE contact_center_team "
                "SET access_topology_revision = access_topology_revision + 1 "
                "WHERE id = ANY(%s)",
                [team_ids],
            )
            teams.invalidate_recordset(["access_topology_revision"])
        if user_ids:
            users.flush_model(["contact_center_access_topology_revision"])
            self.env.cr.execute(
                "SELECT id FROM res_users WHERE id = ANY(%s) " "ORDER BY id FOR UPDATE",
                [user_ids],
            )
            self.env.cr.execute(
                "UPDATE res_users SET contact_center_access_topology_revision = "
                "contact_center_access_topology_revision + 1 WHERE id = ANY(%s)",
                [user_ids],
            )
            users.invalidate_recordset(["contact_center_access_topology_revision"])
        if pipeline_ids:
            pipelines = (
                self.env["contact.center.pipeline"]
                .sudo()
                .with_context(active_test=False)
                .browse(pipeline_ids)
            )
            pipelines.flush_model(["topology_revision"])
            self.env.cr.execute(
                "SELECT id FROM contact_center_pipeline WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                [pipeline_ids],
            )
            self.env.cr.execute(
                "UPDATE contact_center_pipeline "
                "SET topology_revision = topology_revision + 1 "
                "WHERE id = ANY(%s)",
                [pipeline_ids],
            )
            pipelines.invalidate_recordset(["topology_revision"])
        if channel_ids:
            channels = (
                self.env["mail.channel"]
                .sudo()
                .with_context(active_test=False)
                .browse(channel_ids)
            )
            channels.flush_model(["write_date"])
            self.env.cr.execute(
                "SELECT id FROM mail_channel WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                [channel_ids],
            )
            self.env.cr.execute(
                "UPDATE mail_channel SET write_date = write_date WHERE id = ANY(%s)",
                [channel_ids],
            )
            channels.invalidate_recordset(["write_date"])
        if case_ids:
            cases = (
                self.env["contact.center.case"]
                .sudo()
                .with_context(active_test=False)
                .browse(case_ids)
            )
            cases.flush_model(["stage_revision"])
            self.env.cr.execute(
                "SELECT id FROM contact_center_case WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                [case_ids],
            )
            self.env.cr.execute(
                "UPDATE contact_center_case SET stage_revision = stage_revision "
                "WHERE id = ANY(%s)",
                [case_ids],
            )
            cases.invalidate_recordset(["stage_revision"])
        return accounts

    def _contact_center_validate_live_route_access(self):
        """Reject transitions from a configured live route to no attendants.

        A brand-new unassigned inbox/connection remains a valid fail-closed draft.
        Once a live primary route has an access scope, however, owner/team mutations
        cannot silently orphan it; the provider must first be demoted/archived or a
        replacement attendant must be assigned.
        """

        candidates = (
            self.sudo()
            .with_context(active_test=False)
            .filtered(
                lambda account: account.active
                and not account._contact_center_access_is_ready()
            )
        )
        if not candidates:
            return True
        live_route = (
            self.env["contact.center.provider.connection"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("account_id", "in", candidates.ids),
                    ("active", "=", True),
                    ("role", "=", "primary"),
                    "|",
                    ("inbound_active", "=", True),
                    ("outbound_active", "=", True),
                ],
                limit=1,
            )
        )
        if live_route:
            raise ValidationError(
                _(
                    "An active Contact Center provider route cannot lose its last "
                    "attendant. Assign an inbox owner or team attendant first, or "
                    "demote/archive the provider route."
                )
            )
        return True

    @api.model
    def _contact_center_users_for_access_scope(self, owner=None, team=None):
        """Return valid internal users in one owner/team access-scope union."""

        users = self.env["res.users"]
        if owner:
            users |= owner
        if team and team.active:
            users |= team.agent_ids | team.supervisor_ids
        return users.filtered(lambda user: user.active and not user.share)

    def _contact_center_effective_users(self):
        """Return the owner/team union used by ACL projection and assignment."""

        users = self.env["res.users"]
        for account in self:
            users |= self._contact_center_users_for_access_scope(
                owner=account.owner_user_id,
                team=account.default_team_id,
            )
        return users

    @api.depends(
        "owner_user_id",
        "owner_user_id.active",
        "owner_user_id.share",
        "default_team_id",
        "default_team_id.active",
        "default_team_id.agent_ids",
        "default_team_id.agent_ids.active",
        "default_team_id.agent_ids.share",
        "default_team_id.supervisor_ids",
        "default_team_id.supervisor_ids.active",
        "default_team_id.supervisor_ids.share",
    )
    def _compute_auto_assignment_eligible_user_ids(self):
        for account in self:
            account.auto_assignment_eligible_user_ids = (
                account._contact_center_effective_users()
            )

    def _contact_center_validate_auto_assignment(self):
        """Reject an explicitly configured assignee outside the effective scope."""

        for account in self:
            self._contact_center_validate_auto_assignment_scope(
                assignee=account.auto_assignment_user_id,
                owner=account.owner_user_id,
                team=account.default_team_id,
            )
        return True

    @api.model
    def _contact_center_validate_auto_assignment_scope(
        self, *, assignee=None, owner=None, team=None
    ):
        """Validate one prospective policy before it reaches persistent state."""

        assignee = assignee or self.env["res.users"]
        if assignee and assignee not in self._contact_center_users_for_access_scope(
            owner=owner,
            team=team,
        ):
            raise ValidationError(
                _(
                    "The automatic assignee must be an active internal user "
                    "in the inbox owner or access-team scope."
                )
            )
        return True

    def _contact_center_reconcile_auto_assignment(self):
        """Disable a stale policy after an owner/team/roster revocation.

        Access revocation is authoritative. It must not be rejected merely because
        the revoked user was selected for automatic assignment; clearing the optional
        policy in the same transaction preserves the scope invariant instead.
        """

        invalid = self.filtered(
            lambda account: account.auto_assignment_user_id
            and account.auto_assignment_user_id
            not in account._contact_center_effective_users()
        )
        if invalid:
            invalid.write({"auto_assignment_user_id": False})
        return True

    def _contact_center_validate_auto_assignment_write(self, values, changed):
        """Validate a prospective explicit selection after topology locking."""

        if not changed:
            return True
        assignee = self.env["res.users"].browse(values.get("auto_assignment_user_id"))
        for account in self:
            owner = (
                self.env["res.users"].browse(values.get("owner_user_id"))
                if "owner_user_id" in values
                else account.owner_user_id
            )
            team = (
                self.env["contact.center.team"].browse(values.get("default_team_id"))
                if "default_team_id" in values
                else account.default_team_id
            )
            self._contact_center_validate_auto_assignment_scope(
                assignee=assignee,
                owner=owner,
                team=team,
            )
        return True

    def _contact_center_access_is_ready(self):
        """Whether a live provider route has at least one valid attendant."""

        self.ensure_one()
        return bool(self.active and self._contact_center_effective_users())

    def _contact_center_check_user_scope(self, user=None):
        user = user or self.env.user
        inaccessible = self.filtered(
            lambda account: user not in account._contact_center_effective_users()
        )
        if inaccessible:
            raise AccessError(_("You are not assigned to this Contact Center inbox."))
        return True

    @api.constrains("owner_user_id", "default_team_id", "company_id")
    def _check_contact_center_access_configuration(self):
        agent_group = self.env.ref(
            "contact_center_base.group_contact_center_agent", raise_if_not_found=False
        )
        for account in self:
            if account.default_team_id and not account.default_team_id.active:
                raise ValidationError(
                    _("The Contact Center access team must be active.")
                )
            owner = account.owner_user_id
            if not owner:
                continue
            reasons = []
            if not owner.active:
                reasons.append(_("the user is archived"))
            if owner.share:
                reasons.append(_("the user is external/portal"))
            if agent_group and agent_group not in owner.groups_id:
                reasons.append(_("the Contact Center agent role is missing"))
            if account.company_id not in owner.company_ids:
                reasons.append(_("the user cannot access the inbox company"))
            if reasons:
                raise ValidationError(
                    _(
                        "%(user)s cannot own Contact Center inbox %(inbox)s because "
                        "%(reasons)s."
                    )
                    % {
                        "user": owner.display_name,
                        "inbox": account.display_name,
                        "reasons": ", ".join(reasons),
                    }
                )

    def _contact_center_reconcile_channels(self):
        """Project an inbox access change to exact native channel membership."""

        application = self.env["contact.center.application"]
        binding_model = self.env["contact.center.channel.binding"].sudo()
        for account in self.sudo().with_context(active_test=False):
            bindings = binding_model.with_context(active_test=False).search(
                [("account_id", "=", account.id)]
            )
            users = (
                account._contact_center_effective_users()
                if account.active
                else self.env["res.users"]
            )
            for channel in bindings.mapped("channel_id"):
                old_partner_ids = channel.sudo().channel_member_ids.partner_id.ids
                responsible = channel.contact_center_responsible_id
                values = {
                    "contact_center_owner_user_id": account.owner_user_id.id or False,
                    "contact_center_team_id": account.default_team_id.id or False,
                }
                if responsible and responsible not in users:
                    values["contact_center_responsible_id"] = False
                channel.sudo().with_context(
                    contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
                ).write(values)
                channel._contact_center_reconcile_members(
                    partner_ids=users.partner_id.ids,
                    guest_ids=channel.sudo().channel_member_ids.guest_id.ids,
                    allow_empty=True,
                )
                application._notify_ui(
                    channel,
                    "conversation_updated",
                    {"changed_fields": ["access_scope", "responsible_id"]},
                    partner_ids=list(
                        set(old_partner_ids)
                        | set(channel.sudo().channel_member_ids.partner_id.ids)
                    ),
                )
        return True

    def _contact_center_projection_changed_fields(self, values):
        """Return operator-projection policies changed by this write."""

        changed_fields = []
        if "attribution_ui_enabled" in values and any(
            bool(values["attribution_ui_enabled"]) != account.attribution_ui_enabled
            for account in self
        ):
            changed_fields.append("attribution_visibility")
        if "show_deleted_message_content" in values and any(
            bool(values["show_deleted_message_content"])
            != account.show_deleted_message_content
            for account in self
        ):
            changed_fields.append("deleted_message_policy")
        return changed_fields

    def write(self, values):
        if "access_topology_revision" in values:
            raise AccessError(
                _("The Contact Center access topology revision is internal.")
            )
        immutable_fields = {"company_id", "external_ref", "platform"} & set(values)
        for account in self:
            for field_name in immutable_fields:
                current_value = account[field_name]
                if account._fields[field_name].relational:
                    current_value = current_value.id
                if values[field_name] != current_value:
                    raise ValidationError(
                        _(
                            "The stable account field '%s' cannot be changed.",
                            field_name,
                        )
                    )
        active_changed = "active" in values and any(
            bool(values["active"]) != account.active for account in self
        )
        team_changed = "default_team_id" in values and any(
            (values["default_team_id"] or False)
            != (account.default_team_id.id or False)
            for account in self
        )
        owner_changed = "owner_user_id" in values and any(
            (values["owner_user_id"] or False) != (account.owner_user_id.id or False)
            for account in self
        )
        auto_assignment_changed = "auto_assignment_user_id" in values and any(
            (values["auto_assignment_user_id"] or False)
            != (account.auto_assignment_user_id.id or False)
            for account in self
        )
        projection_changed_fields = self._contact_center_projection_changed_fields(
            values
        )
        scope_changed = active_changed or team_changed or owner_changed
        assignment_topology_changed = scope_changed or auto_assignment_changed
        previous_partner_ids_by_account = (
            {
                account.id: account._contact_center_effective_users().partner_id.ids
                for account in self
            }
            if scope_changed
            else {}
        )
        if assignment_topology_changed and self.ids:
            scope_team_ids = set(self.mapped("default_team_id").ids)
            scope_user_ids = set(
                (
                    self.mapped("owner_user_id")
                    | self.mapped("auto_assignment_user_id")
                ).ids
            )
            if values.get("default_team_id"):
                scope_team_ids.add(int(values["default_team_id"]))
            if values.get("owner_user_id"):
                scope_user_ids.add(int(values["owner_user_id"]))
            if values.get("auto_assignment_user_id"):
                scope_user_ids.add(int(values["auto_assignment_user_id"]))
            self._contact_center_lock_access_topology(
                account_ids=self.ids,
                team_ids=scope_team_ids,
                user_ids=scope_user_ids,
            )
        self._contact_center_validate_auto_assignment_write(
            values, auto_assignment_changed
        )
        affected_connections = (
            self.sudo().with_context(active_test=False).mapped("connection_ids")
            if scope_changed
            else self.env["contact.center.provider.connection"]
        )
        result = super().write(values)
        if scope_changed and not auto_assignment_changed:
            self._contact_center_reconcile_auto_assignment()
        if scope_changed:
            self._contact_center_validate_live_route_access()
            self._contact_center_reconcile_channels()
            application = self.env["contact.center.application"]
            for connection in affected_connections:
                application._notify_connection_health(
                    connection,
                    invalidate=True,
                    additional_partner_ids=previous_partner_ids_by_account.get(
                        connection.account_id.id, []
                    ),
                )
        if projection_changed_fields:
            application = self.env["contact.center.application"]
            bindings = (
                self.env["contact.center.channel.binding"]
                .sudo()
                .search(
                    [
                        ("account_id", "in", self.ids),
                        ("active", "=", True),
                        ("merged_into_id", "=", False),
                    ]
                )
            )
            for channel in bindings.mapped("channel_id"):
                application._notify_ui(
                    channel,
                    "conversation_updated",
                    {"changed_fields": projection_changed_fields},
                )
        return result


class ContactCenterProviderConnection(models.Model):
    _name = "contact.center.provider.connection"
    _description = "Contact Center Provider Connection"
    _order = "account_id, name"

    def _default_external_ref(self):
        return str(uuid.uuid4())

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    account_id = fields.Many2one(
        "contact.center.account", required=True, index=True, ondelete="restrict"
    )
    company_id = fields.Many2one(
        related="account_id.company_id", store=True, readonly=True, index=True
    )
    adapter_key = fields.Selection(
        selection="_selection_adapter_key",
        string="Provider",
        required=True,
        index=True,
    )
    external_ref = fields.Char(
        required=True, copy=False, index=True, default=_default_external_ref
    )
    provider_schema_version = fields.Char(default="unknown", required=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("connecting", "Connecting"),
            ("connected", "Connected"),
            ("degraded", "Degraded"),
            ("paused", "Paused"),
            ("disconnected", "Disconnected"),
            ("authentication_required", "Authentication Required"),
            ("error", "Error"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    role = fields.Selection(
        [
            ("primary", "Primary"),
            ("standby", "Standby"),
            ("migration", "Migration Candidate"),
            ("historical", "Historical"),
        ],
        string="Connection Role",
        required=True,
        default="standby",
        index=True,
        copy=False,
        help=(
            "Primary is the only connection allowed to receive webhooks or send "
            "commands for this inbox. Standby remains configured and monitored. "
            "Migration Candidate is staged for a controlled switch. Historical "
            "is archived and retained only for references and audit history."
        ),
    )
    inbound_active = fields.Boolean(
        string="Receive Webhooks",
        default=False,
        index=True,
        copy=False,
        help=(
            "Authorize this connection as the inbox ingress. Only the active "
            "primary connection can receive webhooks. Provider controllers must "
            "check the core inbound availability boundary before persistence."
        ),
    )
    outbound_active = fields.Boolean(
        default=False,
        index=True,
        copy=False,
        help=(
            "Authorize outbound dispatch through this connection. Only the active "
            "primary connection may dispatch commands for an account."
        ),
    )
    outbound_admission_revision = fields.Integer(
        default=0,
        required=True,
        readonly=True,
        copy=False,
        help=(
            "Monotonic write barrier updated whenever a new durable outbound "
            "command is admitted. It makes provider cutovers retry safely under "
            "PostgreSQL REPEATABLE READ."
        ),
    )
    outbound_dispatch_not_before = fields.Datetime(
        readonly=True,
        copy=False,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Earliest instant at which another outbound command may cross the "
            "provider boundary. Core maintains it from provider pacing and "
            "bounded Retry-After responses."
        ),
    )
    outbound_dispatch_not_before_reason = fields.Selection(
        [
            ("pacing", "Provider Pacing"),
            ("provider_retry_after", "Provider Retry-After"),
        ],
        string="Outbound Dispatch Delay Reason",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    capabilities_json = fields.Json(default=dict, copy=False)
    last_health_at = fields.Datetime(readonly=True, copy=False, index=True)
    last_state_observed_at = fields.Datetime(readonly=True, copy=False, index=True)
    last_state_change_at = fields.Datetime(readonly=True, copy=False)
    last_state_source = fields.Selection(
        [
            ("health_job", "Health Job"),
            ("provider_event", "Provider Event"),
        ],
        readonly=True,
        copy=False,
    )
    last_state_inbox_event_id = fields.Integer(
        readonly=True,
        copy=False,
        default=0,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Durable provider-event ordering cursor for the current observation "
            "second. It is technical state and is never exposed by the Contact "
            "Center UI API."
        ),
    )
    last_health_state_inbox_event_id = fields.Integer(
        readonly=True,
        copy=False,
        default=0,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Provider-event cursor captured when the latest health state was "
            "accepted. Adapters use it as a durable ordering fence; it is never "
            "exposed by the Contact Center UI API."
        ),
    )
    last_connected_at = fields.Datetime(readonly=True, copy=False)
    last_disconnected_at = fields.Datetime(readonly=True, copy=False)
    health_detail = fields.Selection(
        _HEALTH_DETAIL_SELECTION,
        readonly=True,
        copy=False,
        default="unknown",
    )
    identity_mismatch_latched = fields.Boolean(
        string="Identity Safety Lock",
        readonly=True,
        copy=False,
        default=False,
        index=True,
        help=(
            "Blocks outbound dispatch after a provider health check observes a "
            "different session identity. Provider lifecycle events cannot clear "
            "this lock; only a later health check with an explicit identity match "
            "can do so."
        ),
    )
    last_health_latency_ms = fields.Integer(readonly=True, copy=False, default=0)
    consecutive_unhealthy_checks = fields.Integer(readonly=True, copy=False, default=0)
    last_health_error_at = fields.Datetime(readonly=True, copy=False)
    last_health_error_class = fields.Char(readonly=True, copy=False)
    health_check_pending = fields.Boolean(
        readonly=True,
        copy=False,
        default=False,
        index=True,
    )
    health_job_uuid = fields.Char(
        readonly=True,
        copy=False,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    health_configuration_revision = fields.Integer(
        readonly=True,
        copy=False,
        default=0,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Monotonic provider-configuration revision used to discard a health "
            "response produced by credentials or identity that changed in flight."
        ),
    )
    next_health_check_at = fields.Datetime(readonly=True, copy=False, index=True)
    health_retry_not_before = fields.Datetime(
        string="Provider Health Cooldown Until",
        readonly=True,
        copy=False,
        index=True,
        help=(
            "Earliest time at which another provider health request is allowed "
            "after a rate-limit response. Cron and manual checks both respect it."
        ),
    )
    last_recovery_at = fields.Datetime(readonly=True, copy=False)
    last_recovered_command_count = fields.Integer(readonly=True, copy=False, default=0)
    total_recovered_command_count = fields.Integer(readonly=True, copy=False, default=0)
    outbox_recovery_revision = fields.Integer(
        readonly=True,
        copy=False,
        default=0,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Monotonic generation fence for bounded outbox recovery after a "
            "connection becomes available."
        ),
    )
    last_success_at = fields.Datetime(copy=False)
    inbox_open_count = fields.Integer(
        compute="_compute_queue_health", string="Inbox Open"
    )
    inbox_unsupported_count = fields.Integer(
        compute="_compute_queue_health", string="Unsupported Inbox Events"
    )
    inbox_unsupported_recent_count = fields.Integer(
        compute="_compute_queue_health",
        string="Unsupported Inbox Events (24h)",
    )
    outbox_open_count = fields.Integer(
        compute="_compute_queue_health", string="Outbox Open"
    )
    outbox_uncertain_count = fields.Integer(
        compute="_compute_queue_health", string="Outbox Uncertain"
    )
    oldest_pending_at = fields.Datetime(
        compute="_compute_queue_health", string="Oldest Pending"
    )

    _sql_constraints = [
        (
            "external_ref_unique",
            "unique(external_ref)",
            "The provider connection reference must be unique.",
        ),
        (
            "role_state_check",
            "check(role IN ('primary', 'standby', 'migration', 'historical') "
            "AND ((role = 'historical' AND active IS FALSE "
            "AND inbound_active IS FALSE AND outbound_active IS FALSE) "
            "OR (role <> 'historical' AND active IS TRUE "
            "AND ((role = 'primary' AND inbound_active IS TRUE) "
            "OR (role <> 'primary' AND inbound_active IS FALSE "
            "AND outbound_active IS FALSE)))))",
            "The provider role and traffic state are inconsistent.",
        ),
        (
            "health_metrics_nonnegative",
            "check(last_health_latency_ms >= 0 "
            "and consecutive_unhealthy_checks >= 0 "
            "and last_state_inbox_event_id >= 0 "
            "and last_health_state_inbox_event_id >= 0 "
            "and health_configuration_revision >= 0 "
            "and last_recovered_command_count >= 0 "
            "and total_recovered_command_count >= 0 "
            "and outbox_recovery_revision >= 0)",
            "Connection health metrics cannot be negative.",
        ),
    ]

    _health_interval_seconds = 60
    _health_jitter_window_seconds = 30
    _health_stale_after_seconds = 180

    @api.model
    def _contact_center_scope_domain(self, user=None, companies=None):
        """Mirror the operational scope of the connection's logical account."""

        user = user or self.env.user
        if companies is None:
            companies = self.env.companies
        return [
            ("company_id", "in", companies.ids),
            "|",
            "|",
            ("account_id.owner_user_id", "=", user.id),
            ("account_id.default_team_id.agent_ids", "=", user.id),
            ("account_id.default_team_id.supervisor_ids", "=", user.id),
        ]

    @api.model
    def _selection_adapter_key(self):
        """Let each installed provider addon populate the provider selector."""

        return list(adapter_registry.choices())

    @api.model
    def get_health_runtime_contract(self):
        """Return a secret-free M4 runtime contract to Contact Center admins."""

        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_admin"
        ):
            raise AccessError(
                _("Only Contact Center administrators can inspect this contract.")
            )
        cron = self.env.ref(
            "contact_center_base.ir_cron_contact_center_connection_health"
        ).sudo()
        job_function = self.env.ref(
            "contact_center_base.queue_job_function_contact_center_connection_health"
        ).sudo()
        return {
            "cron": {
                "id": cron.id,
                "active": cron.active,
                "interval_number": cron.interval_number,
                "interval_type": cron.interval_type,
                "numbercall": cron.numbercall,
                "doall": cron.doall,
            },
            "job_function": {
                "id": job_function.id,
                "method": job_function.method,
                "channel": job_function.channel,
                "retry_pattern": dict(job_function.retry_pattern or {}),
                "allow_commit": job_function.allow_commit,
            },
            "priority_floor": 30,
            "interval_seconds": self._health_interval_seconds,
            "jitter_window_seconds": self._health_jitter_window_seconds,
        }

    def _compute_queue_health(self):
        unsupported_cutoff = fields.Datetime.now() - datetime.timedelta(hours=24)
        defaults = {
            "inbox_open_count": 0,
            "inbox_unsupported_count": 0,
            "inbox_unsupported_recent_count": 0,
            "outbox_open_count": 0,
            "outbox_uncertain_count": 0,
            "oldest_pending_at": False,
        }
        for connection in self:
            for field_name, value in defaults.items():
                connection[field_name] = value
        connection_ids = [
            connection.id
            for connection in self
            if isinstance(connection.id, int) and not isinstance(connection.id, bool)
        ]
        if not connection_ids:
            return

        # The fleet panel refreshes all visible transports together (roughly twenty
        # in the intended deployment). Aggregate the two ledgers once so health
        # polling never degenerates into five queries per connection.
        self.env.cr.execute(
            """
            WITH inbox_stats AS (
                SELECT provider_connection_id,
                       COUNT(*) FILTER (
                           WHERE state IN ('pending', 'processing', 'retry')
                       ) AS open_count,
                       COUNT(*) FILTER (
                           WHERE state = 'unsupported'
                       ) AS unsupported_count,
                       COUNT(*) FILTER (
                           WHERE state = 'unsupported' AND create_date >= %s
                       ) AS unsupported_recent_count,
                       MIN(create_date) FILTER (
                           WHERE state IN ('pending', 'processing', 'retry')
                       ) AS oldest_open_at
                  FROM contact_center_inbox_event
                 WHERE provider_connection_id = ANY(%s)
                   AND state IN ('pending', 'processing', 'retry', 'unsupported')
              GROUP BY provider_connection_id
            ),
            outbox_stats AS (
                SELECT provider_connection_id,
                       COUNT(*) FILTER (
                           WHERE state IN ('pending', 'processing', 'retry')
                       ) AS open_count,
                       COUNT(*) FILTER (
                           WHERE state = 'uncertain'
                       ) AS uncertain_count,
                       MIN(create_date) FILTER (
                           WHERE state IN ('pending', 'processing', 'retry')
                       ) AS oldest_open_at
                  FROM contact_center_outbox_command
                 WHERE provider_connection_id = ANY(%s)
                   AND state IN ('pending', 'processing', 'retry', 'uncertain')
              GROUP BY provider_connection_id
            )
            SELECT connection.id,
                   COALESCE(inbox.open_count, 0),
                   COALESCE(inbox.unsupported_count, 0),
                   COALESCE(inbox.unsupported_recent_count, 0),
                   COALESCE(outbox.open_count, 0),
                   COALESCE(outbox.uncertain_count, 0),
                   LEAST(inbox.oldest_open_at, outbox.oldest_open_at),
                   inbox.oldest_open_at,
                   outbox.oldest_open_at
              FROM contact_center_provider_connection AS connection
         LEFT JOIN inbox_stats AS inbox
                ON inbox.provider_connection_id = connection.id
         LEFT JOIN outbox_stats AS outbox
                ON outbox.provider_connection_id = connection.id
             WHERE connection.id = ANY(%s)
            """,
            [
                unsupported_cutoff,
                connection_ids,
                connection_ids,
                connection_ids,
            ],
        )
        stats_by_id = {
            row[0]: {
                "inbox_open_count": row[1],
                "inbox_unsupported_count": row[2],
                "inbox_unsupported_recent_count": row[3],
                "outbox_open_count": row[4],
                "outbox_uncertain_count": row[5],
                # PostgreSQL LEAST ignores NULL in supported Odoo versions, but
                # retain the individual values as an explicit compatibility fence.
                "oldest_pending_at": row[6] or row[7] or row[8] or False,
            }
            for row in self.env.cr.fetchall()
        }
        for connection in self:
            for field_name, value in stats_by_id.get(connection.id, defaults).items():
                connection[field_name] = value

    def _contact_center_health_status(self, now=None):
        """Return last-known canonical state or the derived ``unknown`` state."""

        self.ensure_one()
        observation_is_fresh = self._contact_center_observation_is_fresh(now=now)
        if observation_is_fresh and self.state in (
            "authentication_required",
            "disconnected",
        ):
            return self.state
        if self.identity_mismatch_latched:
            return "degraded"
        if not observation_is_fresh:
            return "unknown"
        return self.state if self.state in _CANONICAL_HEALTH_STATES else "unknown"

    def _contact_center_observation_is_fresh(self, now=None):
        """Use the same exact freshness boundary for UI and dispatch."""

        self.ensure_one()
        now = _odoo_datetime(now)
        stale_deadline = now - datetime.timedelta(
            seconds=self._health_stale_after_seconds
        )
        reference_at = self.last_state_observed_at or self.last_health_at
        return bool(reference_at and reference_at >= stale_deadline)

    def _contact_center_inbound_is_available(self):
        """Return the fail-closed core ingress decision for provider controllers."""

        self.ensure_one()
        return bool(
            self.active
            and self.account_id.active
            and self.account_id._contact_center_access_is_ready()
            and self.role == "primary"
            and self.inbound_active
        )

    def _contact_center_outbound_is_available(self, now=None):
        """Return whether this connection may cross the provider boundary."""

        self.ensure_one()
        return bool(
            self.active
            and self.account_id.active
            and self.account_id._contact_center_access_is_ready()
            and self.role == "primary"
            and self.outbound_active
            and self.state == "connected"
            and not self.identity_mismatch_latched
            and self._contact_center_observation_is_fresh(now=now)
        )

    def _contact_center_record_outbound_admission(self):
        """Write the locked transport row before one new durable command.

        A row lock alone does not refresh a transaction snapshot under PostgreSQL
        REPEATABLE READ. Updating this monotonic revision makes a primary switch
        that started before this command fail with a serialization error and retry
        against the now-visible outbox row.
        """

        self.ensure_one()
        self.flush_recordset(
            [
                "active",
                "role",
                "outbound_active",
                "outbound_admission_revision",
            ]
        )
        self.env.cr.execute(
            """
            UPDATE contact_center_provider_connection
               SET outbound_admission_revision = outbound_admission_revision + 1
             WHERE id = %s
               AND active IS TRUE
               AND role = 'primary'
               AND outbound_active IS TRUE
         RETURNING outbound_admission_revision
            """,
            [self.id],
        )
        row = self.env.cr.fetchone()
        if not row:
            raise ValidationError(
                _("The outbound provider changed before the command was persisted.")
            )
        self.invalidate_recordset(["outbound_admission_revision"])
        return int(row[0])

    def _contact_center_set_outbound_dispatch_deadline(self, deadline, reason=False):
        """Persist technical pacing state through one non-forgeable boundary."""

        self.ensure_one()
        if deadline and reason not in ("pacing", "provider_retry_after"):
            raise ValidationError(_("An outbound dispatch deadline requires a reason."))
        if not deadline:
            reason = False
        return (
            self.sudo()
            .with_company(self.company_id)
            .with_context(
                contact_center_outbound_dispatch_token=(
                    _OUTBOUND_DISPATCH_INTERNAL_TOKEN
                )
            )
            .write(
                {
                    "outbound_dispatch_not_before": deadline or False,
                    "outbound_dispatch_not_before_reason": reason,
                }
            )
        )

    @api.model
    def _contact_center_prepare_role_create_values(self, values):
        """Prepare an explicit role contract without inferring a live route."""

        normalized = dict(values)
        if not normalized.get("role") and (
            normalized.get("inbound_active") or normalized.get("outbound_active")
        ):
            raise ValidationError(
                _(
                    "A provider connection without an explicit role is standby "
                    "and cannot enable traffic. Declare a complete primary role "
                    "or use the controlled primary switch action."
                )
            )
        normalized["role"] = normalized.get("role") or "standby"
        if normalized["role"] == "primary":
            required_fields = {
                "active",
                "role",
                "inbound_active",
                "outbound_active",
            }
            missing_fields = sorted(required_fields - set(normalized))
            if missing_fields:
                raise ValidationError(
                    _(
                        "Creating a primary provider connection requires explicit "
                        "active, role, inbound and outbound values. Missing: %s",
                        ", ".join(missing_fields),
                    )
                )
        return normalized

    @api.model
    def _contact_center_lock_topology(self, account_ids):
        """Serialize access/role changes before locking provider connections."""

        account_ids = sorted(
            {int(account_id) for account_id in account_ids if account_id}
        )
        if not account_ids:
            return
        self.env["contact.center.account"]._contact_center_lock_access_topology(
            account_ids=account_ids
        )
        self.flush_model(
            ["account_id", "active", "role", "inbound_active", "outbound_active"]
        )
        self.env.cr.execute(
            """
            SELECT id
              FROM contact_center_provider_connection
             WHERE account_id = ANY(%s)
             ORDER BY account_id, id
             FOR UPDATE
            """,
            [account_ids],
        )

    @api.model
    def _contact_center_lock_operational_admission(self, account_ids):
        """Fence a routine operation without rewriting the account aggregate.

        Sending, updating the native seen pointer and admitting an optional read
        receipt are readers of the current provider topology.  They still need the
        canonical ``account -> connections`` lock order, but incrementing the
        access-topology revision for every such read turns the account into a hot
        MVCC row.  A concurrent outbox finalization can then fail while PostgreSQL
        checks the delivery-event foreign key against that freshly rewritten row.

        ``FOR SHARE`` keeps the account snapshot stable against configuration
        writes.  Provider connections remain exclusively locked because a send
        upgrades the selected connection through its outbound-admission revision;
        serializing here avoids a shared-lock upgrade deadlock between two sends.
        Neither lock creates a new tuple version.
        """

        account_ids = sorted(
            {int(account_id) for account_id in account_ids if account_id}
        )
        if not account_ids:
            return self.browse()
        self.flush_model(
            [
                "account_id",
                "active",
                "role",
                "inbound_active",
                "outbound_active",
                "capabilities_json",
            ]
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id = ANY(%s) "
            "ORDER BY id FOR SHARE",
            [account_ids],
        )
        locked_account_ids = [row[0] for row in self.env.cr.fetchall()]
        if locked_account_ids != account_ids:
            return self.browse()
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection "
            "WHERE account_id = ANY(%s) ORDER BY account_id, id FOR UPDATE",
            [account_ids],
        )
        connections = (
            self.sudo()
            .with_context(active_test=False)
            .browse([row[0] for row in self.env.cr.fetchall()])
        )
        connections.invalidate_recordset(
            [
                "account_id",
                "active",
                "role",
                "inbound_active",
                "outbound_active",
                "capabilities_json",
            ]
        )
        self.env["contact.center.account"].sudo().browse(
            account_ids
        ).invalidate_recordset(
            [
                "active",
                "owner_user_id",
                "default_team_id",
                "group_outbound_enabled",
                "outbound_signature_enabled",
                "mark_read_enabled",
                "access_topology_revision",
            ]
        )
        return connections

    def _contact_center_lock_ingress_admission(self):
        """Fence webhook admission without mutating a hot account row.

        Normal callbacks are concurrent readers of the active route. ``FOR KEY
        SHARE`` keeps those readers compatible with each other, foreign-key checks,
        and ordinary non-key health writes. Topology and secret writers explicitly
        take ``FOR UPDATE`` first, so they still serialize with this admission read.
        The lock order remains account then connection, matching those writers.

        This deliberately differs from ``_contact_center_lock_topology``: that
        writer increments a revision to detect write skew under REPEATABLE READ.
        Calling it for every webhook needlessly rewrites the account tuple and can
        make unrelated delivery-event inserts fail serialization during bursts.
        """

        connections = self.sudo().with_context(active_test=False).exists()
        if not connections:
            return connections
        expected_account_by_connection = {
            connection.id: connection.account_id.id for connection in connections
        }
        account_ids = sorted(set(expected_account_by_connection.values()))
        connection_ids = sorted(expected_account_by_connection)
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id = ANY(%s) "
            "ORDER BY id FOR KEY SHARE",
            [account_ids],
        )
        locked_account_ids = {row[0] for row in self.env.cr.fetchall()}
        self.env.cr.execute(
            "SELECT id, account_id FROM contact_center_provider_connection "
            "WHERE id = ANY(%s) AND account_id = ANY(%s) "
            "ORDER BY account_id, id FOR KEY SHARE",
            [connection_ids, account_ids],
        )
        locked_account_by_connection = dict(self.env.cr.fetchall())
        if (
            locked_account_ids != set(account_ids)
            or locked_account_by_connection != expected_account_by_connection
        ):
            return self.browse()
        connections.invalidate_recordset()
        connections.mapped("account_id").invalidate_recordset()
        return connections

    @api.model
    def _contact_center_validate_role_values(self, values):
        """Validate one complete create-state before it reaches SQL constraints."""

        role = values.get("role")
        active = bool(values.get("active", True))
        inbound_active = bool(values.get("inbound_active"))
        outbound_active = bool(values.get("outbound_active"))
        if role not in ("primary", "standby", "migration", "historical"):
            raise ValidationError(_("A valid provider connection role is required."))
        if role == "historical" and (active or inbound_active or outbound_active):
            raise ValidationError(
                _("A historical provider connection must be archived and inactive.")
            )
        if role != "historical" and not active:
            raise ValidationError(
                _("An archived provider connection must use the historical role.")
            )
        if (inbound_active or outbound_active) and role != "primary":
            raise ValidationError(
                _(
                    "Only the active primary provider connection can receive "
                    "webhooks or dispatch commands."
                )
            )
        if role == "primary" and not inbound_active:
            raise ValidationError(
                _("The active primary provider connection must receive webhooks.")
            )

    @api.model_create_multi
    def create(self, vals_list):
        dispatch_fields = {
            "outbound_dispatch_not_before",
            "outbound_dispatch_not_before_reason",
            "outbound_admission_revision",
        }
        if any(dispatch_fields.intersection(values) for values in vals_list) and (
            self.env.context.get("contact_center_outbound_dispatch_token")
            is not _OUTBOUND_DISPATCH_INTERNAL_TOKEN
        ):
            raise AccessError(
                _(
                    "Outbound dispatch pacing state can only be changed by the "
                    "queue boundary."
                )
            )
        prepared = []
        for values in vals_list:
            normalized = self._contact_center_prepare_role_create_values(values)
            self._contact_center_validate_role_values(normalized)
            prepared.append(normalized)

        account_ids = [values.get("account_id") for values in prepared]
        self._contact_center_lock_topology(account_ids)
        existing_primaries = (
            self.sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("account_id", "in", account_ids),
                    ("role", "=", "primary"),
                    ("active", "=", True),
                ]
            )
        )
        existing_primaries.invalidate_recordset(
            ["active", "role", "inbound_active", "outbound_active"]
        )
        for account_id in sorted({item for item in account_ids if item}):
            account = (
                self.env["contact.center.account"]
                .sudo()
                .with_context(active_test=False)
                .browse(account_id)
                .exists()
            )
            indexes = [
                index
                for index, values in enumerate(prepared)
                if values.get("account_id") == account_id
            ]
            new_primary_indexes = [
                index
                for index in indexes
                if prepared[index]["role"] == "primary"
                and prepared[index].get("active", True)
            ]
            if len(new_primary_indexes) > 1:
                raise ValidationError(
                    _("Only one active primary connection is allowed per account.")
                )
            if new_primary_indexes and (
                not account or not account._contact_center_access_is_ready()
            ):
                raise ValidationError(
                    _(
                        "Assign an inbox owner or team attendant before activating "
                        "the primary provider route."
                    )
                )
            current = existing_primaries.filtered(
                lambda connection, account_id=account_id: (
                    connection.account_id.id == account_id
                )
            )
            if new_primary_indexes and current:
                raise ValidationError(
                    _(
                        "This account already has an active primary connection. "
                        "Use the controlled primary switch action."
                    )
                )

        for values in prepared:
            self._contact_center_validate_role_values(values)
        return super().create(prepared)

    def action_use_as_primary(self):
        """Atomically move ingress ownership without enabling outbound implicitly."""

        self.ensure_one()
        self._contact_center_lock_topology(self.account_id.ids)
        self.invalidate_recordset(
            ["active", "role", "inbound_active", "outbound_active"]
        )
        self._contact_center_assert_activation_ready("inbound")
        connections = (
            self.sudo()
            .with_context(active_test=False)
            .search([("account_id", "=", self.account_id.id)])
        )
        others = (connections - self).filtered(
            lambda connection: connection.active
            and (
                connection.role == "primary"
                or connection.inbound_active
                or connection.outbound_active
            )
        )
        others._contact_center_assert_no_active_outbox()
        internal_context = {
            "contact_center_connection_role_switch_token": (
                _CONNECTION_ROLE_SWITCH_INTERNAL_TOKEN
            )
        }
        if others:
            others.with_context(**internal_context).write(
                {
                    "role": "standby",
                    "inbound_active": False,
                    "outbound_active": False,
                    "active": True,
                }
            )
            # Odoo may defer stored-field writes until a later model flush.  The
            # partial unique indexes are the final split-brain fence, so make the
            # demotion durable in this transaction before the replacement can be
            # promoted.  Otherwise a later model-wide flush is free to write the
            # new (usually higher-ID) primary first and trip the index even though
            # the application followed the correct logical order.
            others.flush_recordset(
                ["active", "role", "inbound_active", "outbound_active"]
            )
        self.with_context(**internal_context).write(
            {
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": bool(self.outbound_active),
            }
        )
        self.flush_recordset(["active", "role", "inbound_active", "outbound_active"])
        return True

    def _contact_center_assert_no_active_outbox(self):
        """Prevent a transport cutover from orphaning durable user commands."""

        if not self:
            return True
        blocking_commands = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search_count(
                [
                    ("provider_connection_id", "in", self.ids),
                    ("command_type", "!=", "mark_read"),
                    ("state", "in", ("pending", "processing", "retry", "uncertain")),
                ]
            )
        )
        if blocking_commands:
            raise ValidationError(
                _(
                    "Wait for or resolve the %s active outbound command(s) on the "
                    "current primary connection before changing its traffic role.",
                    blocking_commands,
                )
            )
        return True

    def action_set_standby(self):
        """Keep the connection configured and monitored without provider traffic."""

        for connection in self:
            connection._contact_center_set_nonprimary_role("standby")
        return True

    def action_set_migration(self):
        """Stage the connection for a future controlled primary switch."""

        for connection in self:
            connection._contact_center_set_nonprimary_role("migration")
        return True

    def action_set_historical(self):
        """Archive the transport while preserving all durable references."""

        for connection in self:
            connection._contact_center_lock_topology(connection.account_id.ids)
            connection.with_context(
                contact_center_connection_role_switch_token=(
                    _CONNECTION_ROLE_SWITCH_INTERNAL_TOKEN
                )
            ).write(
                {
                    "active": False,
                    "role": "historical",
                    "inbound_active": False,
                    "outbound_active": False,
                }
            )
        return True

    def _contact_center_set_nonprimary_role(self, role):
        self.ensure_one()
        if role not in ("standby", "migration"):
            raise ValidationError(_("Invalid non-primary connection role."))
        self._contact_center_lock_topology(self.account_id.ids)
        self.with_context(
            contact_center_connection_role_switch_token=(
                _CONNECTION_ROLE_SWITCH_INTERNAL_TOKEN
            )
        ).write(
            {
                "active": True,
                "role": role,
                "inbound_active": False,
                "outbound_active": False,
            }
        )
        return True

    def _contact_center_health_is_checking(self, now=None):
        """Return a fresh transverse probe flag without hiding last-known state."""

        self.ensure_one()
        now = _odoo_datetime(now)
        stale_deadline = now - datetime.timedelta(
            seconds=self._health_stale_after_seconds
        )
        return bool(
            self.health_check_pending
            and self.next_health_check_at
            and self.next_health_check_at >= stale_deadline
        )

    def _contact_center_health_item(self, now=None, can_check=None):
        """Serialize the stable, flat and secret-free public health item."""

        self.ensure_one()
        now = _odoo_datetime(now)
        status = self._contact_center_health_status(now=now)
        item = {
            "id": self.id,
            "account_id": self.account_id.id,
            "account_name": self.account_id.name,
            "connection_name": self.name,
            "connection_role": self.role,
            "accepts_inbound": self._contact_center_inbound_is_available(),
            # The operator badge is deliberately a moving window. The lifetime
            # total remains available only on the administrative form.
            "unsupported_count": self.inbox_unsupported_recent_count,
            "platform": self.account_id.platform,
            "provider": self.adapter_key,
            "display_address": self.account_id._contact_center_display_address(),
            "state": status,
            "checking": self._contact_center_health_is_checking(now=now),
            "last_check_at": fields.Datetime.to_string(self.last_health_at),
            "state_changed_at": fields.Datetime.to_string(self.last_state_change_at),
            "detail": (
                "unknown"
                if status == "unknown"
                else (
                    (
                        self.health_detail
                        if self.health_detail
                        in ("identity_mismatch", "identity_unverified")
                        else "identity_unverified"
                    )
                    if self.identity_mismatch_latched and status == "degraded"
                    else self.health_detail or "unknown"
                )
            ),
        }
        if can_check is not None:
            item["can_check"] = bool(can_check)
        return item

    @api.model
    def _normalize_health_result(self, health):
        if not isinstance(health, dict):
            raise ValidationError(_("Provider health must be a JSON object."))
        raw_state = health.get("state")
        if not isinstance(raw_state, str) or not raw_state.strip():
            raise ValidationError(_("Provider health must contain a state."))
        raw_state = raw_state.strip().lower().replace("-", "_").replace(" ", "_")
        reason = health.get("detail") or health.get("reason") or ""
        reason = (
            reason.strip().lower().replace("-", "_").replace(" ", "_")
            if isinstance(reason, str)
            else ""
        )

        if raw_state in (
            "logged_out",
            "not_logged_in",
            "not_authenticated",
            "authentication_required",
        ):
            state = "authentication_required"
        elif raw_state == "error":
            if reason in (
                "unauthorized",
                "logged_out",
                "not_logged_in",
                "not_authenticated",
                "authentication_required",
            ):
                state = "authentication_required"
            elif reason in ("unreachable", "timeout", "network_error"):
                state = "disconnected"
            else:
                state = "degraded"
        elif raw_state in ("paused", "connecting"):
            state = "degraded"
        elif raw_state in _CANONICAL_HEALTH_STATES:
            state = raw_state
        else:
            raise ValidationError(_("Provider health contains an unsupported state."))

        detail = _HEALTH_DETAIL_ALIASES.get(reason)
        if state == "connected":
            detail = "metadata_limited" if detail == "metadata_limited" else "healthy"
        elif state == "authentication_required":
            detail = "authentication_required"
        elif state == "disconnected":
            detail = detail or "unavailable"
        else:
            detail = detail or "provider_error"
        normalized = {"state": state, "detail": detail}
        # This is an internal fail-closed proof emitted by a provider adapter.
        # Only the literal boolean True is accepted and it is never exposed in
        # the public health DTO or bus payload.
        if isinstance(health.get("identity_matches"), bool):
            normalized["identity_matches"] = health["identity_matches"]
        raw_retry_after = health.get("retry_after_seconds")
        retry_after_text = str(raw_retry_after or "")
        if (
            not isinstance(raw_retry_after, bool)
            and len(retry_after_text) <= 10
            and retry_after_text.isascii()
            and retry_after_text.isdigit()
        ):
            retry_after = min(3600, max(0, int(retry_after_text)))
            if retry_after:
                normalized["retry_after_seconds"] = retry_after
        return normalized

    @api.model
    def _safe_health_error_class(self, error):
        class_name = error.__class__.__name__
        return class_name if _SAFE_ERROR_CLASS.fullmatch(class_name) else "Exception"

    @api.model
    def _health_failure_result(self, error):
        if isinstance(error, ProviderPausedError):
            result = {"state": "degraded", "detail": "provider_paused"}
        elif isinstance(error, TransientAdapterError):
            result = {"state": "disconnected", "detail": "unreachable"}
        elif isinstance(error, (AdapterError, ValidationError)):
            result = {"state": "degraded", "detail": "invalid_response"}
        else:
            result = {"state": "degraded", "detail": "internal_error"}
        result["error_class"] = self._safe_health_error_class(error)
        return result

    def _health_job_record(self):
        self.ensure_one()
        job_model = self.env["queue.job"].sudo()
        job = job_model
        if self.health_job_uuid:
            job = job_model.search(
                [
                    ("uuid", "=", self.health_job_uuid),
                    ("state", "in", _ACTIVE_QUEUE_JOB_STATES),
                ],
                limit=1,
            )
        if not job:
            job = job_model.search(
                [
                    (
                        "identity_key",
                        "=",
                        "contact_center:health:%s" % self.id,
                    ),
                    ("state", "in", _ACTIVE_QUEUE_JOB_STATES),
                ],
                limit=1,
            )
        return job

    def _has_active_health_job(self):
        self.ensure_one()
        return bool(self._health_job_record())

    def _enqueue_health_check(self, eta=None, priority=30):
        """Enqueue one unique health job per active connection; never call provider."""

        scheduled_ids = []
        now = _odoo_datetime()
        requested_for = max(now, _odoo_datetime(eta)) if eta else now
        # OCA uses a lower number as a higher priority. Health probes must never
        # jump ahead of inbox/outbox work (default 10), especially on root:1.
        priority = max(30, min(100, int(priority)))
        for original in self:
            connection = original.sudo().with_company(original.company_id)
            self.env.cr.execute(
                "SELECT id FROM contact_center_provider_connection "
                "WHERE id = %s FOR UPDATE",
                [connection.id],
            )
            connection.invalidate_recordset(
                [
                    "active",
                    "account_id",
                    "health_check_pending",
                    "health_job_uuid",
                    "next_health_check_at",
                    "health_retry_not_before",
                ]
            )
            connection.account_id.invalidate_recordset(["active"])
            if not connection.active or not connection.account_id.active:
                continue
            active_job = connection._health_job_record()
            if active_job:
                values = {}
                if not connection.health_check_pending:
                    values["health_check_pending"] = True
                if connection.health_job_uuid != active_job.uuid:
                    values["health_job_uuid"] = active_job.uuid
                if (
                    not connection.next_health_check_at
                    or connection.next_health_check_at < now
                ):
                    values["next_health_check_at"] = now + datetime.timedelta(
                        seconds=self._health_interval_seconds
                    )
                if values:
                    connection.write(values)
                    connection._notify_health_updated()
                continue
            cooldown_until = connection.health_retry_not_before
            if cooldown_until and cooldown_until > now:
                values = {}
                if connection.health_check_pending:
                    values["health_check_pending"] = False
                if connection.health_job_uuid:
                    values["health_job_uuid"] = False
                if (
                    not connection.next_health_check_at
                    or connection.next_health_check_at < cooldown_until
                ):
                    values["next_health_check_at"] = cooldown_until
                if values:
                    connection.write(values)
                    connection._notify_health_updated()
                continue
            scheduled_for = requested_for
            delayed = connection.with_delay(
                identity_key="contact_center:health:%s" % connection.id,
                max_retries=5,
                priority=priority,
                description="Contact Center connection health %s" % connection.id,
                eta=eta,
            )._job_check_health()
            connection.write(
                {
                    "health_check_pending": True,
                    "health_job_uuid": delayed.uuid,
                    "next_health_check_at": scheduled_for
                    + datetime.timedelta(seconds=self._health_interval_seconds),
                }
            )
            scheduled_ids.append(connection.id)
            connection._notify_health_updated()
        return self.browse(scheduled_ids)

    @api.model
    def _cron_schedule_health_checks(self):
        """Select due connections and only enqueue OCA jobs with bounded jitter."""

        now = _odoo_datetime()
        due = (
            self.sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("active", "=", True),
                    ("account_id.active", "=", True),
                    "|",
                    ("next_health_check_at", "=", False),
                    ("next_health_check_at", "<=", now),
                ],
                order="next_health_check_at, id",
                limit=200,
            )
        )
        scheduled_count = 0
        for connection in due:
            self.env.cr.execute(
                "SELECT id FROM contact_center_provider_connection "
                "WHERE id = %s FOR UPDATE SKIP LOCKED",
                [connection.id],
            )
            if not self.env.cr.fetchone():
                continue
            connection.invalidate_recordset(
                [
                    "active",
                    "health_check_pending",
                    "health_job_uuid",
                    "next_health_check_at",
                ]
            )
            if not connection.active or connection._has_active_health_job():
                continue
            jitter_seconds = (connection.id * 17) % self._health_jitter_window_seconds
            eta = now + datetime.timedelta(seconds=jitter_seconds)
            if connection._enqueue_health_check(eta=eta, priority=30):
                scheduled_count += 1
        return scheduled_count

    def _job_check_health(self):
        """Perform one read-only provider health probe inside ``queue_job``."""

        self.ensure_one()
        job_uuid = self.env.context.get("job_uuid")
        if not job_uuid:
            raise ValidationError(
                _("Connection health checks must run through queue_job.")
            )
        self.invalidate_recordset(
            [
                "active",
                "account_id",
                "health_job_uuid",
                "health_configuration_revision",
            ]
        )
        if not self.health_job_uuid or self.health_job_uuid != job_uuid:
            return False
        self.account_id.invalidate_recordset(["active"])
        if not self.active or not self.account_id.active:
            return self._complete_inactive_health_check(job_uuid)

        configuration_revision = self.health_configuration_revision
        started = time.monotonic()
        # Date the provider snapshot at the start of its read. A lifecycle
        # event accepted while the network request is in flight must remain
        # newer than the returned snapshot and cannot be reopened by it.
        status_observed_at = _odoo_datetime()
        error_class = False
        try:
            health = self.get_adapter().get_health(self)
            normalized = self._normalize_health_result(health)
        except Exception as error:  # provider isolation boundary
            normalized = self._health_failure_result(error)
            error_class = normalized.pop("error_class")
            _logger.warning(
                "Contact Center health probe failed for connection %s (%s)",
                self.id,
                error_class,
            )
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        return self._apply_normalized_health(
            normalized,
            source="health_job",
            observed_at=status_observed_at,
            latency_ms=latency_ms,
            error_class=error_class,
            expected_job_uuid=job_uuid,
            expected_configuration_revision=configuration_revision,
            completes_check=True,
        )

    def _complete_inactive_health_check(self, expected_job_uuid):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection "
            "WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset(["active", "health_check_pending", "health_job_uuid"])
        if self.health_job_uuid != expected_job_uuid:
            return False
        self.sudo().write({"health_check_pending": False, "health_job_uuid": False})
        self._notify_health_updated()
        return False

    def _apply_health_result(
        self,
        health,
        *,
        source="health_job",
        observed_at=None,
        latency_ms=0,
        error_class=False,
        expected_job_uuid=None,
        expected_configuration_revision=None,
        completes_check=False,
        records_health_probe=False,
        observation_sequence=0,
    ):
        normalized = self._normalize_health_result(health)
        return self._apply_normalized_health(
            normalized,
            source=source,
            observed_at=observed_at,
            latency_ms=latency_ms,
            error_class=error_class,
            expected_job_uuid=expected_job_uuid,
            expected_configuration_revision=expected_configuration_revision,
            completes_check=completes_check,
            records_health_probe=records_health_probe,
            observation_sequence=observation_sequence,
        )

    def _health_identity_guard(self, normalized, source):
        """Return the health state after applying the fail-closed identity latch."""

        self.ensure_one()
        new_state = normalized["state"]
        new_detail = normalized["detail"]
        mismatch_observed = bool(
            source == "health_job"
            and (
                new_detail in ("identity_mismatch", "identity_unverified")
                or normalized.get("identity_matches") is False
            )
        )
        explicit_identity_match = bool(
            source == "health_job"
            and new_state == "connected"
            and normalized.get("identity_matches") is True
        )
        identity_lock_for_guard = bool(
            self.identity_mismatch_latched or mismatch_observed
        )
        if (
            identity_lock_for_guard
            and new_state == "connected"
            and not explicit_identity_match
        ):
            # A provider lifecycle event only says that *a* session connected.
            # It cannot prove that it is the configured WhatsApp identity.
            new_state = "degraded"
            new_detail = (
                self.health_detail
                if self.health_detail in ("identity_mismatch", "identity_unverified")
                else "identity_unverified"
            )
        return (
            new_state,
            new_detail,
            mismatch_observed,
            explicit_identity_match,
        )

    def _health_identity_cursor_values(
        self,
        *,
        source,
        observed_at,
        observation_sequence,
        mismatch_observed,
        explicit_identity_match,
        state_is_fresh,
    ):
        """Build identity-latch and provider-event cursor updates."""

        self.ensure_one()
        values = {}
        if mismatch_observed and not self.identity_mismatch_latched:
            # A mismatch is fail-closed even if its timestamp loses an ordering
            # tie to a more restrictive provider observation.
            values["identity_mismatch_latched"] = True
        elif (
            explicit_identity_match
            and state_is_fresh
            and self.identity_mismatch_latched
        ):
            values["identity_mismatch_latched"] = False
        provider_cursor_advances = bool(
            source == "provider_event"
            and observation_sequence
            and observed_at == self.last_state_observed_at
            and observation_sequence > self.last_state_inbox_event_id
        )
        if provider_cursor_advances:
            # Remember receipt order even when a same-second cross-source tie is
            # resolved in favour of the safer state. This prevents a still older
            # provider event from being considered afterwards.
            values["last_state_inbox_event_id"] = observation_sequence
        return values

    def _health_probe_values(
        self,
        normalized,
        *,
        observed_at,
        latency_ms,
        error_class,
        new_state,
    ):
        """Build metrics and retry cooldown for an accepted provider probe."""

        self.ensure_one()
        values = {
            "last_health_at": observed_at,
            "last_health_latency_ms": latency_ms,
            "consecutive_unhealthy_checks": (
                0 if new_state == "connected" else self.consecutive_unhealthy_checks + 1
            ),
            "last_health_error_at": observed_at if error_class else False,
            "last_health_error_class": error_class or False,
        }
        retry_after_seconds = normalized.get("retry_after_seconds", 0)
        if retry_after_seconds:
            provider_deadline = observed_at + datetime.timedelta(
                seconds=retry_after_seconds
            )
            if (
                not self.next_health_check_at
                or self.next_health_check_at < provider_deadline
            ):
                values["next_health_check_at"] = provider_deadline
            values["health_retry_not_before"] = provider_deadline
        elif (
            self.health_retry_not_before and self.health_retry_not_before <= observed_at
        ):
            values["health_retry_not_before"] = False
        return values

    def _health_state_transition_values(
        self,
        *,
        old_state,
        new_state,
        new_detail,
        observed_at,
        source,
        observation_sequence,
        state_is_fresh,
    ):
        """Build canonical state and transition timestamps for a fresh observation."""

        self.ensure_one()
        if not state_is_fresh:
            return {}
        values = {
            "state": new_state,
            "health_detail": new_detail,
            "last_state_observed_at": observed_at,
            "last_state_source": source,
            "last_state_inbox_event_id": (
                observation_sequence
                if source == "provider_event"
                else self.last_state_inbox_event_id
            ),
        }
        if source == "health_job":
            # Snapshot the provider-event cursor rather than comparing Datetime
            # values. Odoo stores state observations at whole-second precision
            # while inbox create_date may retain microseconds. This also proves
            # whether health was accepted before or after a lifecycle event and
            # prevents health-before-event retry loops.
            values["last_health_state_inbox_event_id"] = self.last_state_inbox_event_id
        if old_state != new_state:
            values["last_state_change_at"] = observed_at
            if new_state == "connected":
                values["last_connected_at"] = observed_at
            elif old_state == "connected" or not self.last_disconnected_at:
                values["last_disconnected_at"] = observed_at
        return values

    def _recover_outbox_after_health(
        self, *, was_available, availability_now, observed_at
    ):
        """Schedule bounded recovery after a transition to outbound availability.

        Health owns the connection row while applying a provider observation.  It
        must not also lock and enqueue an unbounded outbox backlog: that extends a
        hot health transaction and can create a queue burst.  Capture the immutable
        high-water mark while admission is fenced, then let ``queue_job`` process
        small pages after this transaction commits.
        """

        self.ensure_one()
        is_available = self._contact_center_outbound_is_available(now=availability_now)
        became_available = bool(not was_available and is_available)
        if not became_available:
            return False
        recovery_cutoff_id = self._outbox_recovery_cutoff_id()
        recovery_revision = self.outbox_recovery_revision + 1
        self.write(
            {
                "last_recovery_at": observed_at,
                "last_recovered_command_count": 0,
                "outbox_recovery_revision": recovery_revision,
            }
        )
        if recovery_cutoff_id:
            self._enqueue_outbox_recovery(
                recovery_revision,
                after_id=0,
                cutoff_id=recovery_cutoff_id,
            )
        return True

    def _outbox_recovery_cutoff_id(self):
        """Return the last eligible command admitted before this recovery wave."""

        self.ensure_one()
        self.env["contact.center.outbox.command"].sudo().flush_model(
            [
                "provider_connection_id",
                "state",
                "dispatch_job_uuid",
                "dispatch_started_at",
            ]
        )
        self.env.cr.execute(
            """
            SELECT COALESCE(MAX(id), 0)
              FROM contact_center_outbox_command
             WHERE provider_connection_id = %s
               AND state IN ('pending', 'retry')
               AND dispatch_job_uuid IS NULL
               AND dispatch_started_at IS NULL
            """,
            [self.id],
        )
        return int(self.env.cr.fetchone()[0] or 0)

    def _enqueue_outbox_recovery(self, recovery_revision, *, after_id, cutoff_id):
        """Enqueue one uniquely identified continuation on the Outbox channel."""

        self.ensure_one()
        arguments = (recovery_revision, after_id, cutoff_id)
        if any(
            not isinstance(value, int) or isinstance(value, bool) for value in arguments
        ) or not (recovery_revision > 0 and 0 <= after_id < cutoff_id):
            raise ValidationError(_("The outbox recovery cursor is invalid."))
        delayed = (
            self.sudo()
            .with_company(self.company_id)
            .with_delay(
                identity_key="contact_center:outbox_recovery:%s:%s:%s"
                % (self.id, recovery_revision, after_id),
                max_retries=_OUTBOX_RECOVERY_MAX_RETRIES,
                priority=_OUTBOX_RECOVERY_PRIORITY,
                channel="root.contact_center.outbox",
                description="Contact Center outbox recovery %s" % self.id,
            )
            ._job_recover_outbox_commands(
                recovery_revision,
                after_id,
                cutoff_id,
            )
        )
        return delayed.uuid

    def _lock_outbox_recovery_generation(self, recovery_revision):
        """Lock one recovery generation in canonical account -> connection order."""

        self.ensure_one()
        expected_account_id = self.account_id.id
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id = %s FOR SHARE",
            [expected_account_id],
        )
        if not self.env.cr.fetchone():
            return False
        self.env.cr.execute(
            "SELECT account_id FROM contact_center_provider_connection "
            "WHERE id = %s FOR UPDATE",
            [self.id],
        )
        row = self.env.cr.fetchone()
        self.invalidate_recordset(
            [
                "account_id",
                "active",
                "role",
                "outbound_active",
                "state",
                "last_state_observed_at",
                "last_health_at",
                "identity_mismatch_latched",
                "outbox_recovery_revision",
                "last_recovered_command_count",
                "total_recovered_command_count",
            ]
        )
        self.account_id.invalidate_recordset(
            ["active", "owner_user_id", "default_team_id"]
        )
        return bool(
            row
            and row[0] == expected_account_id
            and self.outbox_recovery_revision == recovery_revision
        )

    def _locked_outbox_recovery_batch(self, *, after_id, cutoff_id, limit):
        """Lock one ordered and bounded page from a fixed recovery snapshot."""

        self.ensure_one()
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= _OUTBOX_RECOVERY_MAX_BATCH_SIZE
        ):
            raise ValidationError(_("The outbox recovery batch size is invalid."))
        self.env.cr.execute(
            """
            SELECT id
              FROM contact_center_outbox_command
             WHERE provider_connection_id = %s
               AND id > %s
               AND id <= %s
               AND state IN ('pending', 'retry')
               AND dispatch_job_uuid IS NULL
               AND dispatch_started_at IS NULL
             ORDER BY id
             LIMIT %s
             FOR UPDATE
            """,
            [self.id, after_id, cutoff_id, limit],
        )
        return (
            self.env["contact.center.outbox.command"]
            .sudo()
            .browse([row[0] for row in self.env.cr.fetchall()])
        )

    def _resume_locked_outbox_batch(self, commands):
        """Resume one locked page with a constant number of queue lookups."""

        commands = commands.sudo().sorted("id")
        if not commands:
            return 0
        identity_by_command = {
            command.id: "contact_center:outbox:%s" % command.id for command in commands
        }
        identity_keys = list(identity_by_command.values())
        job_model = self.env["queue.job"].sudo()
        active_jobs = job_model.search(
            [
                ("identity_key", "in", identity_keys),
                ("state", "in", _ACTIVE_QUEUE_JOB_STATES),
            ],
            order="id desc",
        )
        failed_jobs = job_model.search(
            [("identity_key", "in", identity_keys), ("state", "=", "failed")],
            order="id desc",
        )
        active_by_identity = {}
        failed_by_identity = {}
        for job in active_jobs:
            active_by_identity.setdefault(job.identity_key, job)
        for job in failed_jobs:
            failed_by_identity.setdefault(job.identity_key, job)

        resumed_count = 0
        for command in commands:
            identity_key = identity_by_command[command.id]
            active_job = active_by_identity.get(identity_key)
            if active_job:
                if command.queue_job_uuid != active_job.uuid:
                    command.write({"queue_job_uuid": active_job.uuid})
                if active_job.state == "pending":
                    active_job.requeue()
                    active_job.write({"eta": False, "retry": 0})
                    resumed_count += 1
                continue
            failed_job = failed_by_identity.get(identity_key)
            if failed_job:
                failed_job.requeue()
                failed_job.write({"eta": False, "retry": 0})
                if command.queue_job_uuid != failed_job.uuid:
                    command.write({"queue_job_uuid": failed_job.uuid})
                resumed_count += 1
                continue
            delayed = (
                command.with_company(command.company_id)
                .with_delay(
                    identity_key=identity_key,
                    max_retries=0,
                    description="Contact Center outbox command %s" % command.id,
                )
                ._job_process()
            )
            command.write({"queue_job_uuid": delayed.uuid})
            resumed_count += 1
        return resumed_count

    def _has_outbox_recovery_after(self, *, after_id, cutoff_id):
        self.ensure_one()
        self.env.cr.execute(
            """
            SELECT 1
              FROM contact_center_outbox_command
             WHERE provider_connection_id = %s
               AND id > %s
               AND id <= %s
               AND state IN ('pending', 'retry')
               AND dispatch_job_uuid IS NULL
               AND dispatch_started_at IS NULL
             LIMIT 1
            """,
            [self.id, after_id, cutoff_id],
        )
        return bool(self.env.cr.fetchone())

    def _job_recover_outbox_commands(self, recovery_revision, after_id=0, cutoff_id=0):
        """Resume one page and enqueue at most one lower-priority continuation."""

        self.ensure_one()
        if not self.env.context.get("job_uuid"):
            raise ValidationError(_("Outbox recovery must run through queue_job."))
        arguments = (recovery_revision, after_id, cutoff_id)
        if any(
            not isinstance(value, int) or isinstance(value, bool) for value in arguments
        ) or not (recovery_revision > 0 and 0 <= after_id < cutoff_id):
            raise ValidationError(_("The outbox recovery cursor is invalid."))
        connection = self.sudo().exists()
        if not connection or not connection._lock_outbox_recovery_generation(
            recovery_revision
        ):
            return False
        if not connection._contact_center_outbound_is_available():
            return False

        commands = connection._locked_outbox_recovery_batch(
            after_id=after_id,
            cutoff_id=cutoff_id,
            limit=_OUTBOX_RECOVERY_BATCH_SIZE,
        )
        resumed_count = connection._resume_locked_outbox_batch(commands)
        if resumed_count:
            connection.write(
                {
                    "last_recovered_command_count": (
                        connection.last_recovered_command_count + resumed_count
                    ),
                    "total_recovered_command_count": (
                        connection.total_recovered_command_count + resumed_count
                    ),
                }
            )
        last_id = commands[-1].id if commands else cutoff_id
        if commands and connection._has_outbox_recovery_after(
            after_id=last_id,
            cutoff_id=cutoff_id,
        ):
            connection._enqueue_outbox_recovery(
                recovery_revision,
                after_id=last_id,
                cutoff_id=cutoff_id,
            )
        return resumed_count

    def _apply_normalized_health(
        self,
        normalized,
        *,
        source,
        observed_at=None,
        latency_ms=0,
        error_class=False,
        expected_job_uuid=None,
        expected_configuration_revision=None,
        completes_check=False,
        records_health_probe=False,
        observation_sequence=0,
    ):
        self.ensure_one()
        if source not in ("health_job", "provider_event"):
            raise ValidationError(_("Unsupported connection health source."))
        observed_at = _odoo_datetime(observed_at)
        observation_sequence = max(0, int(observation_sequence or 0))
        latency_ms = max(0, int(latency_ms or 0))
        connection = self.sudo().with_company(self.company_id)
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection "
            "WHERE id = %s FOR UPDATE",
            [connection.id],
        )
        connection.invalidate_recordset(
            [
                "active",
                "account_id",
                "role",
                "inbound_active",
                "outbound_active",
                "state",
                "last_state_observed_at",
                "last_state_source",
                "last_state_inbox_event_id",
                "last_health_state_inbox_event_id",
                "last_health_at",
                "health_check_pending",
                "health_job_uuid",
                "health_configuration_revision",
                "next_health_check_at",
                "health_retry_not_before",
                "consecutive_unhealthy_checks",
                "total_recovered_command_count",
                "outbox_recovery_revision",
                "identity_mismatch_latched",
            ]
        )
        connection.account_id.invalidate_recordset(["active"])
        if expected_job_uuid and connection.health_job_uuid != expected_job_uuid:
            return False
        owns_health_check = bool(
            completes_check
            and (
                not connection.health_job_uuid
                or connection.health_job_uuid == expected_job_uuid
            )
        )
        if (
            expected_configuration_revision is not None
            and connection.health_configuration_revision
            != expected_configuration_revision
        ):
            if owns_health_check:
                connection.write(
                    {
                        "health_check_pending": False,
                        "health_job_uuid": False,
                        "next_health_check_at": observed_at,
                    }
                )
                connection._notify_health_updated()
            return False
        if not connection.active or not connection.account_id.active:
            if owns_health_check:
                connection.write(
                    {"health_check_pending": False, "health_job_uuid": False}
                )
            connection._notify_health_updated()
            return False

        availability_now = _odoo_datetime()
        was_available = connection._contact_center_outbound_is_available(
            now=availability_now
        )
        old_state = connection.state
        (
            new_state,
            new_detail,
            mismatch_observed,
            explicit_identity_match,
        ) = connection._health_identity_guard(normalized, source)
        state_is_fresh = connection._state_observation_is_fresh(
            observed_at,
            source,
            new_state,
            observation_sequence=observation_sequence,
        )
        values = connection._health_identity_cursor_values(
            source=source,
            observed_at=observed_at,
            observation_sequence=observation_sequence,
            mismatch_observed=mismatch_observed,
            explicit_identity_match=explicit_identity_match,
            state_is_fresh=state_is_fresh,
        )
        if owns_health_check:
            # Only the queue job that passed ``expected_job_uuid`` above owns
            # these fields. Provider-specific setup probes may record a health
            # proof, but must never complete a newer canonical health job.
            values.update(
                {
                    "health_check_pending": False,
                    "health_job_uuid": False,
                }
            )
        probe_is_fresh = bool(
            not connection.last_health_at
            or observed_at >= _odoo_datetime(connection.last_health_at)
        )
        if (completes_check or records_health_probe) and probe_is_fresh:
            values.update(
                connection._health_probe_values(
                    normalized,
                    observed_at=observed_at,
                    latency_ms=latency_ms,
                    error_class=error_class,
                    new_state=new_state,
                )
            )
        values.update(
            connection._health_state_transition_values(
                old_state=old_state,
                new_state=new_state,
                new_detail=new_detail,
                observed_at=observed_at,
                source=source,
                observation_sequence=observation_sequence,
                state_is_fresh=state_is_fresh,
            )
        )
        if values:
            connection.write(values)

        connection._recover_outbox_after_health(
            was_available=was_available,
            availability_now=availability_now,
            observed_at=observed_at,
        )
        if state_is_fresh or completes_check or records_health_probe:
            connection._notify_health_updated()
        return bool(values)

    def _state_observation_is_fresh(
        self, observed_at, source, new_state, observation_sequence=0
    ):
        """Order state observations without trusting provider timestamps.

        Provider events received in the same database second are ordered by the
        durable inbox primary key. There is no comparable sequence for a health
        probe, so a cross-source tie keeps the less permissive state. That may
        delay recovery until the next probe, but never opens outbound dispatch
        while the durable ordering is ambiguous.
        """

        self.ensure_one()
        existing_at = self.last_state_observed_at
        if not existing_at or observed_at > existing_at:
            return True
        if observed_at < existing_at:
            return False
        if (
            source == "provider_event"
            and observation_sequence
            and observation_sequence <= self.last_state_inbox_event_id
        ):
            return False
        if source == "provider_event" and self.last_state_source == "provider_event":
            return bool(
                observation_sequence
                and observation_sequence > self.last_state_inbox_event_id
            )
        old_rank = _HEALTH_SAFETY_RANK.get(self.state, 2)
        new_rank = _HEALTH_SAFETY_RANK.get(new_state, 2)
        return new_rank > old_rank

    def _apply_provider_state_event(
        self,
        state,
        observed_at=None,
        detail=None,
        observation_sequence=0,
    ):
        """Apply one already-normalized provider session event without provider I/O."""

        self.ensure_one()
        return self._apply_health_result(
            {"state": state, "detail": detail or state},
            source="provider_event",
            observed_at=observed_at,
            completes_check=False,
            observation_sequence=observation_sequence,
        )

    def _notify_health_updated(self):
        for connection in self:
            self.env["contact.center.application"]._notify_connection_health(
                connection,
                invalidate=not connection.active or not connection.account_id.active,
            )
        return True

    def init(self):
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                contact_center_provider_connection_one_outbound
            ON contact_center_provider_connection (account_id)
            WHERE outbound_active IS TRUE AND active IS TRUE
            """
        )
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                contact_center_provider_connection_one_active_primary
            ON contact_center_provider_connection (account_id)
            WHERE role = 'primary' AND active IS TRUE
            """
        )
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                contact_center_provider_connection_one_inbound
            ON contact_center_provider_connection (account_id)
            WHERE inbound_active IS TRUE AND active IS TRUE
            """
        )

    @api.constrains("adapter_key")
    def _check_adapter_key(self):
        for record in self:
            if not record.adapter_key or any(
                character.isspace() for character in record.adapter_key
            ):
                raise ValidationError(
                    _("Adapter keys cannot be empty or contain whitespace.")
                )

    @api.constrains("active", "role", "inbound_active", "outbound_active")
    def _check_connection_role_state(self):
        for connection in self:
            self._contact_center_validate_role_values(
                {
                    "active": connection.active,
                    "role": connection.role,
                    "inbound_active": connection.inbound_active,
                    "outbound_active": connection.outbound_active,
                }
            )

    def _contact_center_prepare_role_write_values(self, values):
        """Normalize archive toggles while requiring controlled live-route changes."""

        values = dict(values)
        switch_is_internal = (
            self.env.context.get("contact_center_connection_role_switch_token")
            is _CONNECTION_ROLE_SWITCH_INTERNAL_TOKEN
        )
        controlled_switch_targets = self.filtered(
            lambda connection: connection.role != "primary"
            and (
                values.get("role", connection.role) == "primary"
                or values.get("outbound_active") is True
            )
        )
        if controlled_switch_targets and not switch_is_internal:
            raise ValidationError(
                _(
                    "Use the controlled primary switch action before enabling "
                    "inbound or outbound traffic on a standby or migration "
                    "provider connection."
                )
            )
        historical_targets = (
            self.filtered(lambda connection: connection.role == "historical")
            if values.get("active") is True and "role" not in values
            else self.browse()
        )
        if historical_targets:
            if historical_targets != self:
                raise ValidationError(
                    _(
                        "Restore historical provider connections separately from "
                        "active connections."
                    )
                )
            if values.get("inbound_active") or values.get("outbound_active"):
                raise ValidationError(
                    _(
                        "Restore the historical provider as standby before "
                        "promoting or enabling traffic."
                    )
                )
            # Odoo's standard unarchive action writes only active=True. Restore
            # configuration in a fail-closed standby role; ingress/outbound are
            # enabled only through the explicit primary controls.
            values.update(
                {
                    "role": "standby",
                    "inbound_active": False,
                    "outbound_active": False,
                }
            )
        if values.get("active") is False or values.get("role") == "historical":
            values.update(
                {
                    "active": False,
                    "role": "historical",
                    "inbound_active": False,
                    "outbound_active": False,
                }
            )
        elif values.get("role") in ("standby", "migration"):
            values.update({"inbound_active": False, "outbound_active": False})
        return values

    def write(self, values):
        values = self._contact_center_prepare_role_write_values(values)

        dispatch_fields = {
            "outbound_dispatch_not_before",
            "outbound_dispatch_not_before_reason",
            "outbound_admission_revision",
        }
        if (
            dispatch_fields.intersection(values)
            and self.env.context.get("contact_center_outbound_dispatch_token")
            is not _OUTBOUND_DISPATCH_INTERNAL_TOKEN
        ):
            raise AccessError(
                _(
                    "Outbound dispatch pacing state can only be changed by the "
                    "queue boundary."
                )
            )
        immutable_fields = {"account_id", "adapter_key", "external_ref"} & set(values)
        for connection in self:
            for field_name in immutable_fields:
                current_value = connection[field_name]
                if connection._fields[field_name].relational:
                    current_value = current_value.id
                if values[field_name] != current_value:
                    raise ValidationError(
                        _(
                            "The provider connection field '%s' cannot be changed.",
                            field_name,
                        )
                    )
        topology_fields = {"active", "role", "inbound_active", "outbound_active"}
        topology_changed = bool(topology_fields.intersection(values))
        if topology_changed and (
            self.env.context.get("contact_center_connection_role_switch_token")
            is not _CONNECTION_ROLE_SWITCH_INTERNAL_TOKEN
        ):
            self._contact_center_lock_topology(self.mapped("account_id").ids)
        if topology_changed:
            self.invalidate_recordset(
                ["active", "role", "inbound_active", "outbound_active"]
            )
            if (
                self.env.context.get("contact_center_connection_role_switch_token")
                is not _CONNECTION_ROLE_SWITCH_INTERNAL_TOKEN
            ):
                inbound_enabling = self.filtered(
                    lambda connection: (
                        bool(values.get("active", connection.active))
                        and values.get("role", connection.role) == "primary"
                        and bool(
                            values.get("inbound_active", connection.inbound_active)
                        )
                        and not (
                            connection.active
                            and connection.role == "primary"
                            and connection.inbound_active
                        )
                    )
                )
                for connection in inbound_enabling:
                    connection._contact_center_assert_activation_ready("inbound")
            traffic_demotions = self.filtered(
                lambda connection: connection.active
                and connection.role == "primary"
                and (
                    not bool(values.get("active", connection.active))
                    or values.get("role", connection.role) != "primary"
                    or not bool(values.get("inbound_active", connection.inbound_active))
                    or (
                        connection.outbound_active
                        and not bool(values.get("outbound_active", True))
                    )
                )
            )
            traffic_demotions._contact_center_assert_no_active_outbox()
        if values.get("outbound_active") is True:
            outbound_enabling = self.filtered(
                lambda connection: not connection.outbound_active
            )
            for connection in outbound_enabling:
                connection._contact_center_assert_activation_ready("outbound")
        active_changed = "active" in values and any(
            bool(values["active"]) != connection.active for connection in self
        )
        configuration_revision_changed = (
            "health_configuration_revision" in values
            and self.filtered(
                lambda connection: values["health_configuration_revision"]
                != connection.health_configuration_revision
            )
        )
        result = super().write(values)
        if configuration_revision_changed:
            profiles = (
                self.env["contact.center.group.profile"]
                .sudo()
                .search(
                    [
                        (
                            "provider_connection_id",
                            "in",
                            configuration_revision_changed.ids,
                        )
                    ]
                )
            )
            if profiles:
                profiles.write(
                    {
                        "own_protocol_participant_json": {},
                        "own_protocol_participant_health_revision": 0,
                        "metadata_state": "stale",
                        "next_sync_at": fields.Datetime.now(),
                    }
                )
                profiles._notify_updated()
        if topology_changed:
            application = self.env["contact.center.application"]
            for connection in self:
                application._notify_connection_health(
                    connection,
                    invalidate=(
                        active_changed
                        or not connection.active
                        or not connection.account_id.active
                    ),
                )
        return result

    def get_adapter(self):
        self.ensure_one()
        try:
            adapter_class = adapter_registry.get(self.adapter_key)
        except KeyError as error:
            raise ValidationError(
                _("No provider adapter is registered for '%s'.", self.adapter_key)
            ) from error
        owner_module = adapter_registry.owner(self.adapter_key)
        if owner_module:
            module = (
                self.env["ir.module.module"]
                .sudo()
                .search([("name", "=", owner_module)], limit=1)
            )
            if not module or module.state not in (
                "installed",
                "to install",
                "to upgrade",
            ):
                raise ValidationError(
                    _(
                        "Provider adapter '%(adapter)s' belongs to inactive module "
                        "'%(module)s'.",
                        adapter=self.adapter_key,
                        module=owner_module,
                    )
                )
        return adapter_class(self.env)

    def refresh_capabilities(self):
        if not self.env.su and not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(
                _(
                    "Only Contact Center supervisors can refresh provider "
                    "capabilities."
                )
            )
        self.check_access_rights("read")
        self.check_access_rule("read")
        for connection in self:
            capabilities = connection.get_adapter().get_capabilities(connection)
            if not isinstance(capabilities, dict):
                raise ValidationError(_("Provider capabilities must be a JSON object."))
            connection.sudo().write({"capabilities_json": capabilities})
        return True

    def action_refresh_capabilities(self):
        self.refresh_capabilities()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Contact Center"),
                "message": _("Provider capabilities refreshed."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_check_health(self):
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(
                _("Only Contact Center supervisors can request a health refresh.")
            )
        self.check_access_rights("read")
        self.check_access_rule("read")
        self.sudo()._enqueue_health_check(priority=30)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Contact Center"),
                "message": _("Provider health check scheduled."),
                "type": "info",
                "sticky": False,
            },
        }
