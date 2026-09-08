import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_base.models.account import _relational_command_ids

from ..services.tokens import CONTACT_CENTER_CASE_TRANSITION_TOKEN

CRM_CATALOG_SYNC_TOKEN = object()
CRM_CASE_STAGE_SYNC_TOKEN = object()
CRM_ROSTER_SYNC_TOKEN = object()
CRM_BINDING_GRAPH_LOCK_TOKEN = object()
CRM_ROSTER_AGGREGATE_LOCK_TOKEN = object()
CRM_ROLE_GRANT_SERVICE_TOKEN = object()
CRM_CATALOG_AUTHORITY_TOKEN = object()
CRM_PHYSICAL_UNLINK_TOKEN = object()

_IN_GROUP_FIELD = re.compile(r"^in_group_(\d+)$")
_SEL_GROUPS_FIELD = re.compile(r"^sel_groups_(\d+(?:_\d+)*)$")


def binding_graph_is_locked(env):
    return (
        env.context.get("contact_center_crm_binding_graph_lock")
        is CRM_BINDING_GRAPH_LOCK_TOKEN
    )


def roster_aggregate_is_locked(env):
    return (
        env.context.get("contact_center_crm_roster_aggregate_lock")
        is CRM_ROSTER_AGGREGATE_LOCK_TOKEN
    )


def physical_unlink_is_allowed(env):
    """Allow physical lifecycle cleanup only to a trusted superuser path.

    RPC callers can forge arbitrary context values, including
    ``module_uninstall``.  They cannot forge ``env.su`` or the process-local
    object token.  Odoo's real module-uninstall path runs as superuser and
    supplies ``module_uninstall``; internal maintenance code may instead use
    the private token.
    """

    if not env.su:
        return False
    return bool(
        env.context.get("module_uninstall")
        or env.context.get("contact_center_crm_physical_unlink")
        is CRM_PHYSICAL_UNLINK_TOKEN
    )


def _positive_ids(values):
    return sorted(
        {
            value
            for value in values
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        }
    )


class ContactCenterCrmCatalogAuthority(models.Model):
    """Private MVCC fence for global and per-sales-team CRM catalogs."""

    _name = "contact.center.crm.catalog.authority"
    _description = "Contact Center CRM Catalog Authority"
    _order = "authority_key, id"

    authority_key = fields.Char(required=True, readonly=True, index=True)
    crm_team_id = fields.Many2one(
        "crm.team", readonly=True, index=True, ondelete="cascade"
    )
    authority_revision = fields.Integer(required=True, default=0, readonly=True)

    _sql_constraints = [
        (
            "authority_key_unique",
            "unique(authority_key)",
            "Authority key must be unique.",
        ),
        (
            "authority_revision_nonnegative",
            "check(authority_revision >= 0)",
            "Authority revision cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("contact_center_crm_catalog_authority")
            is not CRM_CATALOG_AUTHORITY_TOKEN
        ):
            raise AccessError(_("CRM catalog authorities are managed internally."))
        return super().create(vals_list)

    def write(self, values):
        if (
            self.env.context.get("contact_center_crm_catalog_authority")
            is not CRM_CATALOG_AUTHORITY_TOKEN
        ):
            raise AccessError(_("CRM catalog authorities are managed internally."))
        return super().write(values)

    def unlink(self):
        if not self.env.context.get("module_uninstall"):
            raise AccessError(_("CRM catalog authorities are lifecycle records."))
        return super().unlink()


def lock_catalog_authorities(env, crm_team_ids=()):
    """Touch global then local catalog fences, including the first-row case."""

    team_ids = _positive_ids(crm_team_ids)
    Authority = env["contact.center.crm.catalog.authority"].sudo()
    Authority.flush_model(["authority_key", "crm_team_id", "authority_revision"])
    targets = [("global", None)] + [("team:%s" % value, value) for value in team_ids]
    for key, team_id in targets:
        env.cr.execute(
            "INSERT INTO contact_center_crm_catalog_authority "
            "(authority_key, crm_team_id, authority_revision, create_uid, write_uid, "
            "create_date, write_date) VALUES (%s, %s, 1, %s, %s, NOW(), NOW()) "
            "ON CONFLICT (authority_key) DO UPDATE SET "
            "authority_revision = "
            "contact_center_crm_catalog_authority.authority_revision + 1, "
            "write_uid = EXCLUDED.write_uid, write_date = NOW() RETURNING id",
            [key, team_id, env.uid, env.uid],
        )
        if not env.cr.fetchone():
            raise ValidationError(_("The CRM catalog authority disappeared."))
    return True


def lock_binding_graph(
    env,
    *,
    pipeline_ids=(),
    team_ids=(),
    pipeline_binding_ids=(),
    team_binding_ids=(),
    crm_team_ids=(),
    touch=False,
):
    """Serialize the CRM mapping authority before lead/case graph locks.

    Binding rows are the authority for admitting a first case/lead link.  Every
    caller uses Catalog Authority -> CRM Team -> Pipeline Binding -> Team
    Binding.  When ``touch`` is true, CRM-team and binding revision writes make
    stale REPEATABLE READ waiters fail and retry rather than validating a
    pre-lock snapshot; inventory acceptance uses a lock-only pass for those
    rows and publishes its roster revision after revalidating the candidate.
    """

    pipeline_ids = _positive_ids(pipeline_ids)
    team_ids = _positive_ids(team_ids)
    pipeline_binding_ids = _positive_ids(pipeline_binding_ids)
    team_binding_ids = _positive_ids(team_binding_ids)
    PipelineBinding = (
        env["contact.center.crm.pipeline.binding"]
        .sudo()
        .with_context(active_test=False)
    )
    TeamBinding = (
        env["contact.center.crm.team.binding"].sudo().with_context(active_test=False)
    )
    # Preliminary reads only discover which catalog authorities must be fenced.
    preliminary_pipeline = (
        PipelineBinding.search(
            [
                "|",
                ("pipeline_id", "in", pipeline_ids or [0]),
                ("crm_team_id", "in", _positive_ids(crm_team_ids) or [0]),
            ]
        )
        | PipelineBinding.browse(pipeline_binding_ids).exists()
    )
    preliminary_team = (
        TeamBinding.search(
            [
                "|",
                ("contact_center_team_id", "in", team_ids or [0]),
                ("crm_team_id", "in", _positive_ids(crm_team_ids) or [0]),
            ]
        )
        | TeamBinding.browse(team_binding_ids).exists()
    )
    locked_crm_team_ids = _positive_ids(
        list(crm_team_ids)
        + preliminary_pipeline.mapped("crm_team_id").ids
        + preliminary_team.mapped("crm_team_id").ids
    )
    lock_catalog_authorities(env, locked_crm_team_ids)
    if locked_crm_team_ids:
        env["crm.team"].sudo().with_context(active_test=False).browse(
            locked_crm_team_ids
        )._contact_center_lock_roster_authorities(touch=touch)

    PipelineBinding.flush_model(
        ["pipeline_id", "active", "authority_revision", "crm_team_id"]
    )
    TeamBinding.flush_model(
        [
            "contact_center_team_id",
            "active",
            "authority_revision",
            "crm_team_id",
        ]
    )

    pipeline_domain = []
    if pipeline_ids:
        pipeline_domain.append(("pipeline_id", "in", pipeline_ids))
    if pipeline_binding_ids:
        pipeline_domain.append(("id", "in", pipeline_binding_ids))
    if locked_crm_team_ids:
        pipeline_domain.append(("crm_team_id", "in", locked_crm_team_ids))
    pipeline_bindings = PipelineBinding
    if pipeline_domain:
        domain = [pipeline_domain[0]]
        for item in pipeline_domain[1:]:
            domain = ["|"] + domain + [item]
        pipeline_bindings = PipelineBinding.search(domain, order="id")
    if pipeline_bindings:
        env.cr.execute(
            "SELECT id FROM contact_center_crm_pipeline_binding "
            "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [pipeline_bindings.ids],
        )

    team_domain = []
    if team_ids:
        team_domain.append(("contact_center_team_id", "in", team_ids))
    if team_binding_ids:
        team_domain.append(("id", "in", team_binding_ids))
    if locked_crm_team_ids:
        team_domain.append(("crm_team_id", "in", locked_crm_team_ids))
    team_bindings = TeamBinding
    if team_domain:
        domain = [team_domain[0]]
        for item in team_domain[1:]:
            domain = ["|"] + domain + [item]
        team_bindings = TeamBinding.search(domain, order="id")
    if team_bindings:
        env.cr.execute(
            "SELECT id FROM contact_center_crm_team_binding "
            "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [team_bindings.ids],
        )

    if touch and pipeline_bindings:
        env.cr.execute(
            "UPDATE contact_center_crm_pipeline_binding "
            "SET authority_revision = authority_revision + 1 "
            "WHERE id = ANY(%s)",
            [pipeline_bindings.ids],
        )
    if touch and team_bindings:
        env.cr.execute(
            "UPDATE contact_center_crm_team_binding "
            "SET authority_revision = authority_revision + 1 "
            "WHERE id = ANY(%s)",
            [team_bindings.ids],
        )
    pipeline_bindings.invalidate_recordset(
        ["active", "authority_revision", "crm_team_id", "pipeline_id"]
    )
    team_bindings.invalidate_recordset(
        [
            "active",
            "authority_revision",
            "crm_team_id",
            "contact_center_team_id",
        ]
    )
    return pipeline_bindings, team_bindings


def lock_roster_aggregate(
    env,
    *,
    team_binding_ids=(),
    contact_center_team_ids=(),
    crm_team_ids=(),
    extra_user_ids=(),
    touch=True,
):
    """Fence one roster aggregate before grants or user groups are written.

    The complete order is CRM binding graph -> core account -> core team ->
    core user -> role-grant rows.  Both manual ``res.users`` group edits and
    CRM roster projection enter through this helper, so neither path can hold
    grants while waiting for the core topology (or hold a team while waiting
    for a user already owned by its peer transaction).
    """

    binding_ids = _positive_ids(team_binding_ids)
    cc_team_ids = _positive_ids(contact_center_team_ids)
    crm_ids = _positive_ids(crm_team_ids)
    user_ids = set(_positive_ids(extra_user_ids))
    TeamBinding = (
        env["contact.center.crm.team.binding"].sudo().with_context(active_test=False)
    )
    Grant = env["contact.center.crm.role.grant"].sudo()

    # Discovery is deliberately read-only.  The authoritative rows are then
    # fenced and invalidated by lock_binding_graph before any state is changed.
    preliminary_grants = Grant.search(
        [
            "|",
            ("binding_id", "in", binding_ids or [0]),
            ("user_id", "in", sorted(user_ids) or [0]),
        ]
    )
    preliminary_bindings = (
        TeamBinding.browse(binding_ids).exists()
        | TeamBinding.search(
            [
                "|",
                ("contact_center_team_id", "in", cc_team_ids or [0]),
                ("crm_team_id", "in", crm_ids or [0]),
            ]
        )
        | preliminary_grants.mapped("binding_id")
    )
    binding_ids = _positive_ids(preliminary_bindings.ids)
    cc_team_ids = _positive_ids(
        cc_team_ids + preliminary_bindings.mapped("contact_center_team_id").ids
    )
    crm_ids = _positive_ids(crm_ids + preliminary_bindings.mapped("crm_team_id").ids)

    if not binding_graph_is_locked(env):
        lock_binding_graph(
            env,
            team_ids=cc_team_ids,
            team_binding_ids=binding_ids,
            crm_team_ids=crm_ids,
            touch=touch,
        )

    bindings = TeamBinding.browse(binding_ids).exists()
    cc_team_ids = _positive_ids(
        cc_team_ids + bindings.mapped("contact_center_team_id").ids
    )
    crm_ids = _positive_ids(crm_ids + bindings.mapped("crm_team_id").ids)

    crm_teams = (
        env["crm.team"].sudo().with_context(active_test=False).browse(crm_ids).exists()
    )
    user_ids.update(crm_teams.mapped("user_id").ids)
    if crm_ids:
        user_ids.update(
            env["crm.team.member"]
            .sudo()
            .with_context(active_test=False)
            .search([("crm_team_id", "in", crm_ids), ("active", "=", True)])
            .mapped("user_id")
            .ids
        )
    user_ids.update(
        Grant.search([("binding_id", "in", binding_ids or [0])]).mapped("user_id").ids
    )

    cc_teams = (
        env["contact.center.team"]
        .sudo()
        .with_context(active_test=False)
        .browse(cc_team_ids)
        .exists()
    )
    user_ids.update(
        (cc_teams.mapped("agent_ids") | cc_teams.mapped("supervisor_ids")).ids
    )

    users = env["res.users"].sudo().browse(_positive_ids(user_ids)).exists()
    # A manual authorization edit can affect native CC teams that have no CRM
    # binding. Include the exact topology the base res.users.write will lock.
    native_accounts = env["contact.center.account"].sudo().browse()
    for user in users:
        native_teams, user_accounts = user._contact_center_access_topology_records()
        cc_teams |= native_teams.sudo().with_context(active_test=False)
        native_accounts |= user_accounts.sudo().with_context(active_test=False)
    accounts = (
        env["contact.center.account"]
        .sudo()
        .with_context(active_test=False)
        .search([("access_team_ids", "in", cc_teams.ids or [0])])
        | native_accounts
    )
    env["contact.center.account"]._contact_center_lock_access_topology(
        account_ids=accounts.ids,
        team_ids=cc_teams.ids,
        user_ids=users.ids,
    )

    grant_domain = []
    if binding_ids:
        grant_domain.append(("binding_id", "in", binding_ids))
    if users:
        grant_domain.append(("user_id", "in", users.ids))
    grants = Grant
    if grant_domain:
        domain = [grant_domain[0]]
        for item in grant_domain[1:]:
            domain = ["|"] + domain + [item]
        grants = Grant.search(domain, order="id")
    if grants:
        Grant.flush_model(["binding_id", "user_id", "state", "managed"])
        env.cr.execute(
            "SELECT id FROM contact_center_crm_role_grant "
            "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [grants.ids],
        )
        grants.invalidate_recordset(["state", "managed"])

    bindings = bindings.with_context(
        contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN,
        contact_center_crm_roster_aggregate_lock=CRM_ROSTER_AGGREGATE_LOCK_TOKEN,
    )
    users = users.with_context(
        contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN,
        contact_center_crm_roster_aggregate_lock=CRM_ROSTER_AGGREGATE_LOCK_TOKEN,
    )
    grants = grants.with_context(
        contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN,
        contact_center_crm_roster_aggregate_lock=CRM_ROSTER_AGGREGATE_LOCK_TOKEN,
    )
    return bindings, users, grants


def _crm_team_matches_company(crm_team, company):
    return not crm_team.company_id or crm_team.company_id == company


class ContactCenterCrmRoleGrant(models.Model):
    _name = "contact.center.crm.role.grant"
    _description = "Contact Center CRM-managed Role Grant"
    _order = "binding_id, user_id, group_id, id"
    _check_company_auto = True

    binding_id = fields.Many2one(
        "contact.center.crm.team.binding",
        required=True,
        index=True,
        ondelete="cascade",
        check_company=True,
    )
    user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="restrict"
    )
    group_id = fields.Many2one(
        "res.groups", required=True, index=True, ondelete="restrict"
    )
    company_id = fields.Many2one(
        related="binding_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    state = fields.Selection(
        [("active", "Active"), ("released", "Released")],
        required=True,
        readonly=True,
        default="active",
        index=True,
    )
    managed = fields.Boolean(
        required=True,
        readonly=True,
        default=False,
        help="The role was introduced by this bridge and may be safely revoked.",
    )
    granted_at = fields.Datetime(
        required=True, readonly=True, default=fields.Datetime.now
    )
    released_at = fields.Datetime(readonly=True, copy=False)

    _sql_constraints = [
        (
            "binding_user_group_unique",
            "unique(binding_id, user_id, group_id)",
            "This CRM-managed role grant already exists.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("contact_center_crm_role_grant_service")
            is not CRM_ROLE_GRANT_SERVICE_TOKEN
        ):
            raise AccessError(_("CRM role grants are managed internally."))
        return super().create(vals_list)

    def write(self, values):
        if (
            self.env.context.get("contact_center_crm_role_grant_service")
            is not CRM_ROLE_GRANT_SERVICE_TOKEN
        ):
            raise AccessError(_("CRM role grants are managed internally."))
        allowed = {"state", "managed", "granted_at", "released_at"}
        if set(values) - allowed:
            raise AccessError(_("CRM role grant identity is immutable."))
        return super().write(values)

    def unlink(self):  # pylint: disable=method-required-super
        if self.env.context.get("module_uninstall"):
            return super().unlink()
        raise AccessError(_("CRM role grant evidence is immutable."))


class ResUsers(models.Model):
    _inherit = "res.users"

    @api.model
    def _contact_center_explicit_group_ids(self, values):
        """Return groups the caller explicitly selected through any Odoo UI.

        ``res.users`` exposes both the real ``groups_id`` relation and dynamic
        ``in_group_*``/``sel_groups_*`` fields.  Treating only the relation as
        an ownership signal means a role selected in the standard user form
        can later be revoked by the CRM roster bridge.  Normalize all three
        representations before the native inverse methods consume them.
        """

        group_ids = set()
        for command in values.get("groups_id") or ():
            if not isinstance(command, (list, tuple)) or not command:
                continue
            if command[0] == 4 and len(command) > 1:
                group_ids.add(command[1])
            elif command[0] == 6 and len(command) > 2:
                group_ids.update(command[2])

        for field_name, value in values.items():
            in_group = _IN_GROUP_FIELD.match(field_name)
            if in_group and value:
                group_ids.add(int(in_group.group(1)))
                continue
            selection = _SEL_GROUPS_FIELD.match(field_name)
            if not selection or isinstance(value, bool):
                continue
            try:
                selected_id = int(value)
            except (TypeError, ValueError):
                continue
            if selected_id in {int(item) for item in selection.group(1).split("_")}:
                group_ids.add(selected_id)

        explicit_groups = self.env["res.groups"].sudo().browse(_positive_ids(group_ids))
        # A manual selection owns the implied capabilities too.  This matters
        # for Contact Center Supervisor, which implies the Agent capability.
        effective_groups = explicit_groups | explicit_groups.mapped("trans_implied_ids")
        return set(effective_groups.ids)

    def write(self, values):
        manual_group_edit = any(
            field_name == "groups_id"
            or _IN_GROUP_FIELD.match(field_name)
            or _SEL_GROUPS_FIELD.match(field_name)
            for field_name in values
        ) and (
            self.env.context.get("contact_center_crm_role_grant_service")
            is not CRM_ROLE_GRANT_SERVICE_TOKEN
        )
        if manual_group_edit and not roster_aggregate_is_locked(self.env):
            # The aggregate helper deliberately uses sudo while discovering and
            # fencing every affected topology row.  Reject an unauthorized
            # caller before entering that privileged/locking section.  Keep the
            # check scoped to group edits so Odoo's native self-service writes
            # (language, timezone, preferences, ...) retain their semantics.
            self.check_access_rights("write")
            self.check_access_rule("write")
            _bindings, _users, _grants = lock_roster_aggregate(
                self.env,
                extra_user_ids=self.ids,
                touch=True,
            )
            return self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN,
                contact_center_crm_roster_aggregate_lock=(
                    CRM_ROSTER_AGGREGATE_LOCK_TOKEN
                ),
            ).write(values)
        if manual_group_edit:
            explicitly_kept = self._contact_center_explicit_group_ids(values)
            if explicitly_kept:
                grants = (
                    self.env["contact.center.crm.role.grant"]
                    .sudo()
                    .search(
                        [
                            ("user_id", "in", self.ids),
                            ("group_id", "in", list(explicitly_kept)),
                            ("managed", "=", True),
                        ]
                    )
                )
                if grants:
                    grants.with_context(
                        contact_center_crm_role_grant_service=(
                            CRM_ROLE_GRANT_SERVICE_TOKEN
                        )
                    ).write({"managed": False})
        return super().write(values)


class ContactCenterCrmTeamBinding(models.Model):
    _name = "contact.center.crm.team.binding"
    _description = "Contact Center CRM Team Binding"
    _rec_name = "contact_center_team_id"
    _order = "company_id, contact_center_team_id, id"
    _check_company_auto = True

    active = fields.Boolean(default=True)
    authority_revision = fields.Integer(
        required=True,
        default=1,
        readonly=True,
        copy=False,
        help="Internal MVCC fence for CRM/Contact Center topology changes.",
    )
    contact_center_team_id = fields.Many2one(
        "contact.center.team",
        string="Contact Center Team",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    crm_team_id = fields.Many2one(
        "crm.team",
        string="CRM Sales Team",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
    )
    company_id = fields.Many2one(
        related="contact_center_team_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    role_grant_ids = fields.One2many(
        "contact.center.crm.role.grant",
        "binding_id",
        readonly=True,
    )

    _sql_constraints = [
        (
            "contact_center_team_unique",
            "unique(contact_center_team_id)",
            "A Contact Center team can be bound to only one CRM team.",
        ),
        (
            "crm_team_unique",
            "unique(crm_team_id)",
            "A CRM sales team can be bound to only one Contact Center team.",
        ),
        (
            "authority_revision_positive",
            "check(authority_revision > 0)",
            "The CRM binding revision must be positive.",
        ),
    ]

    @api.model
    def _check_crm_team_available(self, crm_team_ids, excluding=None):
        """Enforce one CRM team to one Contact Center team under a row lock."""

        crm_team_ids = [team_id for team_id in crm_team_ids if team_id]
        if len(crm_team_ids) != len(set(crm_team_ids)):
            raise ValidationError(
                _("A CRM sales team can be bound to only one Contact Center team.")
            )
        domain = [("crm_team_id", "in", crm_team_ids)]
        if excluding:
            domain.append(("id", "not in", excluding.ids))
        if crm_team_ids and self.sudo().with_context(active_test=False).search_count(
            domain
        ):
            raise ValidationError(
                _("A CRM sales team can be bound to only one Contact Center team.")
            )
        return True

    @api.model_create_multi
    def create(self, vals_list):
        if any("authority_revision" in values for values in vals_list):
            raise AccessError(_("The CRM binding revision is managed internally."))
        crm_team_ids = [values.get("crm_team_id") for values in vals_list]
        contact_center_team_ids = [
            values.get("contact_center_team_id") for values in vals_list
        ]
        if not binding_graph_is_locked(self.env):
            lock_binding_graph(
                self.env,
                team_ids=contact_center_team_ids,
                crm_team_ids=crm_team_ids,
                touch=True,
            )
            bindings = self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            ).create(vals_list)
            # A process-local capability is valid only inside the recursive
            # operation that acquired the graph lock.  Never return it to the
            # caller: reusing that recordset later in the same transaction must
            # acquire a fresh fence for the new operation.
            return self.browse(bindings.ids)
        _bindings, _users, _grants = lock_roster_aggregate(
            self.env,
            contact_center_team_ids=contact_center_team_ids,
            crm_team_ids=crm_team_ids,
            touch=False,
        )
        self = self.with_context(
            contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN,
            contact_center_crm_roster_aggregate_lock=CRM_ROSTER_AGGREGATE_LOCK_TOKEN,
        )
        self._check_crm_team_available(crm_team_ids)
        crm_teams = (
            self.env["crm.team"]
            .sudo()
            .with_context(active_test=False)
            .browse(_positive_ids(crm_team_ids))
        )
        teams = (
            self.env["contact.center.team"]
            .sudo()
            .with_context(active_test=False)
            .browse(_positive_ids(contact_center_team_ids))
        )
        if any(values.get("active", True) for values in vals_list) and (
            any(not team.active for team in teams)
            or any(not team.active for team in crm_teams)
        ):
            raise ValidationError(_("Both bound teams must be active."))
        bindings = super().create(vals_list)
        active_bindings = bindings.with_context(
            contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
        ).filtered("active")
        active_bindings._validate_linked_case_team_contract()
        active_bindings._sync_crm_roster()
        return bindings

    @api.constrains("contact_center_team_id", "crm_team_id")
    def _check_binding_company(self):
        for binding in self:
            if not _crm_team_matches_company(binding.crm_team_id, binding.company_id):
                raise ValidationError(
                    _("The Contact Center and CRM teams belong to different companies.")
                )

    def write(self, values):
        if "authority_revision" in values:
            raise AccessError(_("The CRM binding revision is managed internally."))
        if not binding_graph_is_locked(self.env):
            lock_binding_graph(
                self.env,
                team_binding_ids=self.ids,
                crm_team_ids=self.mapped("crm_team_id").ids
                + ([values["crm_team_id"]] if values.get("crm_team_id") else []),
                touch=True,
            )
            return self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            ).write(values)
        target_crm_team_ids = self.mapped("crm_team_id").ids + (
            [values["crm_team_id"]] if values.get("crm_team_id") else []
        )
        _locked_bindings, _users, _grants = lock_roster_aggregate(
            self.env,
            team_binding_ids=self.ids,
            contact_center_team_ids=self.mapped("contact_center_team_id").ids,
            crm_team_ids=target_crm_team_ids,
            touch=False,
        )
        self = self.with_context(
            contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN,
            contact_center_crm_roster_aggregate_lock=CRM_ROSTER_AGGREGATE_LOCK_TOKEN,
        )
        if "contact_center_team_id" in values and any(
            values["contact_center_team_id"] != binding.contact_center_team_id.id
            for binding in self
        ):
            raise ValidationError(
                _("The Contact Center side of a CRM team binding is immutable.")
            )
        crm_team_changed = "crm_team_id" in values and any(
            values["crm_team_id"] != binding.crm_team_id.id for binding in self
        )
        if crm_team_changed:
            if len(self) != 1:
                raise ValidationError(
                    _(
                        "Change CRM team bindings one at a time so their roster "
                        "authority remains unambiguous."
                    )
                )
            self._check_crm_team_available([values["crm_team_id"]], excluding=self)
        if values.get("active") is True or (
            crm_team_changed and any(self.mapped("active"))
        ):
            self.invalidate_recordset(["active", "contact_center_team_id"])
            for binding in self:
                target_crm_team = (
                    self.env["crm.team"]
                    .sudo()
                    .with_context(active_test=False)
                    .browse(values.get("crm_team_id") or binding.crm_team_id.id)
                )
                if (
                    not target_crm_team.active
                    or not binding.contact_center_team_id.active
                ):
                    raise ValidationError(_("Both bound teams must be active."))
        # Roster authority and case/lead integration are separate contracts.
        # Archiving a roster binding deliberately freezes the last projected
        # Contact Center roster, while changing its CRM authority must not make
        # an existing case/lead association contradict an active binding.
        if crm_team_changed:
            linked = (
                self.env["contact.center.crm.case.link"]
                .sudo()
                .search_count(
                    [
                        (
                            "contact_center_team_id",
                            "in",
                            self.contact_center_team_id.ids,
                        ),
                        ("state", "=", "active"),
                        ("lead_id", "!=", False),
                    ]
                )
            )
            if linked:
                raise ValidationError(
                    _(
                        "A team binding used by linked cases cannot change its "
                        "CRM authority."
                    )
                )
        result = super().write(values)
        if "crm_team_id" in values or values.get("active") is True:
            active_bindings = self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            ).filtered("active")
            active_bindings._validate_linked_case_team_contract()
            active_bindings._sync_crm_roster()
        if values.get("active") is False:
            self._release_role_grants(remove_groups=False)
        return result

    def unlink(self):
        if not physical_unlink_is_allowed(self.env):
            raise AccessError(
                _("CRM team bindings are lifecycle records; archive them instead.")
            )
        return super().unlink()

    def action_open_crm_team(self):
        self.ensure_one()
        self.crm_team_id.check_access_rights("read")
        self.crm_team_id.check_access_rule("read")
        return {
            "type": "ir.actions.act_window",
            "name": self.crm_team_id.display_name,
            "res_model": "crm.team",
            "res_id": self.crm_team_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def _ensure_roster_sync_access(self):
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_admin"
        ):
            raise AccessError(
                _("Only Contact Center administrators can synchronize CRM rosters.")
            )

    def _validate_linked_case_team_contract(self):
        """Reject an active roster authority that contradicts linked CRM teams."""

        Link = self.env["contact.center.crm.case.link"].sudo()
        for binding in self.filtered("active"):
            mismatch = Link.search(
                [
                    ("contact_center_team_id", "=", binding.contact_center_team_id.id),
                    ("state", "=", "active"),
                    ("lead_id", "!=", False),
                    ("lead_team_id", "!=", binding.crm_team_id.id),
                ],
                limit=1,
            )
            if mismatch:
                raise ValidationError(
                    _(
                        "This Contact Center team already has a case linked to "
                        "another CRM sales team. Keep it manual or bind it to "
                        "that same CRM team."
                    )
                )
        return True

    def action_sync_roster(self):
        self._ensure_roster_sync_access()
        # This is an RPC-facing command.  Validate the original recordset
        # before the roster service elevates for its internal projection.
        self.check_access_rights("write")
        self.check_access_rule("write")
        if self.filtered(lambda binding: not binding.active):
            raise ValidationError(
                _("Activate the CRM team binding before synchronizing its roster.")
            )
        self._sync_crm_roster()
        return True

    def _desired_crm_roster(self):
        self.ensure_one()
        crm_team = self.crm_team_id.sudo().with_context(active_test=False)
        leader = crm_team.user_id
        members = (
            self.env["crm.team.member"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("crm_team_id", "=", crm_team.id),
                    ("active", "=", True),
                ]
            )
            .mapped("user_id")
        )
        return members - leader, leader

    def _validate_crm_roster(self, agents, supervisors):
        self.ensure_one()
        crm_team = self.crm_team_id.sudo().with_context(active_test=False)
        contact_center_team = self.contact_center_team_id.sudo().with_context(
            active_test=False
        )
        if not crm_team.active or not contact_center_team.active:
            raise ValidationError(
                _(
                    "Both the CRM and Contact Center teams must be active while "
                    "their roster binding is active."
                )
            )
        if not _crm_team_matches_company(crm_team, contact_center_team.company_id):
            raise ValidationError(
                _("The Contact Center and CRM teams belong to different companies.")
            )
        for user in (agents | supervisors).sorted("id"):
            if not user.active or user.share:
                raise ValidationError(
                    _(
                        "CRM team member %(user)s must be an active internal user "
                        "before receiving Contact Center access.",
                        user=user.display_name,
                    )
                )
            if contact_center_team.company_id not in user.company_ids:
                raise ValidationError(
                    _(
                        "CRM team member %(user)s has no access to company "
                        "%(company)s. Grant company access or remove the member "
                        "before synchronizing the Contact Center roster.",
                        user=user.display_name,
                        company=contact_center_team.company_id.display_name,
                    )
                )
        if not agents and not supervisors:
            ownerless_accounts = (
                self.env["contact.center.account"]
                .sudo()
                .with_context(active_test=False)
                .search(
                    [
                        ("active", "=", True),
                        ("access_team_ids", "=", contact_center_team.id),
                        ("access_user_ids", "=", False),
                    ]
                )
            )
            orphaned_accounts = ownerless_accounts.filtered(
                lambda account: not account._contact_center_users_for_access_scope(
                    users=account.access_user_ids,
                    teams=account.access_team_ids - contact_center_team,
                )
            )
            if orphaned_accounts:
                raise ValidationError(
                    _(
                        "The CRM roster cannot become empty while its Contact "
                        "Center team is the last source of access to an active inbox."
                    )
                )
        return True

    def _release_role_grants(self, *, remove_groups):
        if self and not roster_aggregate_is_locked(self.env):
            bindings, _users, _grants = lock_roster_aggregate(
                self.env,
                team_binding_ids=self.ids,
                touch=True,
            )
            return bindings._release_role_grants(remove_groups=remove_groups)
        Grant = (
            self.env["contact.center.crm.role.grant"]
            .sudo()
            .with_context(
                contact_center_crm_role_grant_service=CRM_ROLE_GRANT_SERVICE_TOKEN
            )
        )
        active_grants = Grant.search(
            [("binding_id", "in", self.ids), ("state", "=", "active")]
        )
        if not active_grants:
            return True
        candidates = active_grants.filtered("managed")
        active_grants.write(
            {
                "state": "released",
                "released_at": fields.Datetime.now(),
                **({} if remove_groups else {"managed": False}),
            }
        )
        if remove_groups:
            self._remove_unowned_roles(candidates)
        return True

    @api.model
    def _remove_unowned_roles(self, candidates):
        return self._reconcile_role_grants_for_users(
            candidates.mapped("user_id").ids, removal_candidates=candidates
        )

    @api.model
    def _reconcile_role_grants_for_users(self, user_ids, removal_candidates=None):
        """Reconcile every CRM claim together, including implied Odoo groups."""

        if user_ids and not roster_aggregate_is_locked(self.env):
            binding_ids = (
                (removal_candidates or self.env["contact.center.crm.role.grant"])
                .mapped("binding_id")
                .ids
            )
            bindings, _users, grants = lock_roster_aggregate(
                self.env,
                team_binding_ids=binding_ids,
                extra_user_ids=user_ids,
                touch=True,
            )
            del bindings, grants
            candidates = (
                removal_candidates or self.env["contact.center.crm.role.grant"].browse()
            ).with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN,
                contact_center_crm_roster_aggregate_lock=(
                    CRM_ROSTER_AGGREGATE_LOCK_TOKEN
                ),
            )
            return self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN,
                contact_center_crm_roster_aggregate_lock=(
                    CRM_ROSTER_AGGREGATE_LOCK_TOKEN
                ),
            )._reconcile_role_grants_for_users(
                user_ids,
                removal_candidates=candidates,
            )

        users = self.env["res.users"].sudo().browse(_positive_ids(user_ids)).exists()
        if not users:
            return True
        agent_group = self.env.ref("contact_center_base.group_contact_center_agent")
        supervisor_group = self.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        groups = agent_group | supervisor_group
        Grant = (
            self.env["contact.center.crm.role.grant"]
            .sudo()
            .with_context(
                contact_center_crm_role_grant_service=CRM_ROLE_GRANT_SERVICE_TOKEN
            )
        )
        grants = Grant.search(
            [("user_id", "in", users.ids), ("group_id", "in", groups.ids)]
        )
        candidate_pairs = {
            (item.user_id.id, item.group_id.id)
            for item in (removal_candidates or Grant.browse())
            if item.managed
        }
        for user_id, group_id in sorted(candidate_pairs):
            if grants.filtered(
                lambda item: item.user_id.id == user_id
                and item.group_id.id == group_id
                and item.state == "active"
            ):
                continue
            user = users.filtered(lambda item: item.id == user_id)
            group = groups.filtered(lambda item: item.id == group_id)
            user.invalidate_recordset(["groups_id"])
            if user and group in user.groups_id:
                user.with_context(
                    contact_center_crm_role_grant_service=(CRM_ROLE_GRANT_SERVICE_TOKEN)
                ).write({"groups_id": [(3, group.id)]})

        # Group implication may remove another capability. Restore every live
        # claim only after all candidate-driven removals have completed.
        for user in users.sorted("id"):
            for group in groups.sorted("id"):
                claims = grants.filtered(
                    lambda item: item.user_id == user and item.group_id == group
                )
                active_claim = claims.filtered(lambda item: item.state == "active")
                user.invalidate_recordset(["groups_id"])
                if active_claim and group not in user.groups_id:
                    active_claim.write({"managed": True})
                    user.with_context(
                        contact_center_crm_role_grant_service=(
                            CRM_ROLE_GRANT_SERVICE_TOKEN
                        )
                    ).write({"groups_id": [(4, group.id)]})
        users.invalidate_recordset(["groups_id"])
        return True

    def _sync_contact_center_roles(self, agents, supervisors):
        self.ensure_one()
        if not roster_aggregate_is_locked(self.env):
            bindings, _users, _grants = lock_roster_aggregate(
                self.env,
                team_binding_ids=self.ids,
                extra_user_ids=(agents | supervisors).ids,
                touch=True,
            )
            return bindings._sync_contact_center_roles(agents, supervisors)
        agent_group = self.env.ref("contact_center_base.group_contact_center_agent")
        supervisor_group = self.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        desired = {(user.id, agent_group.id) for user in agents} | {
            (user.id, supervisor_group.id) for user in supervisors
        }
        Grant = (
            self.env["contact.center.crm.role.grant"]
            .sudo()
            .with_context(
                contact_center_crm_role_grant_service=CRM_ROLE_GRANT_SERVICE_TOKEN
            )
        )
        grants = Grant.search([("binding_id", "=", self.id)])
        by_pair = {(grant.user_id.id, grant.group_id.id): grant for grant in grants}
        now = fields.Datetime.now()
        for user_id, group_id in sorted(desired):
            grant = by_pair.get((user_id, group_id))
            user = self.env["res.users"].sudo().browse(user_id)
            group = self.env["res.groups"].sudo().browse(group_id)
            if grant:
                if grant.state != "active":
                    grant.write(
                        {
                            "state": "active",
                            "granted_at": now,
                            "released_at": False,
                        }
                    )
                continue
            bridge_already_owns = Grant.search_count(
                [
                    ("user_id", "=", user_id),
                    ("group_id", "=", group_id),
                    ("state", "=", "active"),
                    ("managed", "=", True),
                ]
            )
            by_pair[(user_id, group_id)] = Grant.create(
                {
                    "binding_id": self.id,
                    "user_id": user_id,
                    "group_id": group_id,
                    "managed": bool(bridge_already_owns or group not in user.groups_id),
                }
            )

        stale = grants.filtered(
            lambda grant: grant.state == "active"
            and (grant.user_id.id, grant.group_id.id) not in desired
        )
        stale_managed = stale.filtered("managed")
        if stale:
            stale.write({"state": "released", "released_at": now})

        # Grant every capability required by the destination roster before the
        # roster itself changes. Stale managed capabilities are deliberately
        # retained until the caller has removed their users from the source
        # roster; native Contact Center invariants reject the inverse order.
        for user_id, group_id in sorted(desired):
            grant = by_pair[(user_id, group_id)]
            user = self.env["res.users"].sudo().browse(user_id)
            group = self.env["res.groups"].sudo().browse(group_id)
            user.invalidate_recordset(["groups_id"])
            if group in user.groups_id:
                continue
            grant.write({"managed": True})
            user.with_context(
                contact_center_crm_role_grant_service=(CRM_ROLE_GRANT_SERVICE_TOKEN)
            ).write({"groups_id": [(4, group_id)]})
        affected_user_ids = (
            set(grants.mapped("user_id").ids) | set(agents.ids) | set(supervisors.ids)
        )
        self._reconcile_role_grants_for_users(affected_user_ids)
        return stale_managed

    def _sync_crm_roster(self):
        """Project the authoritative CRM roster into native Contact Center ACLs."""

        if self and not binding_graph_is_locked(self.env):
            lock_binding_graph(
                self.env,
                team_binding_ids=self.ids,
                crm_team_ids=self.mapped("crm_team_id").ids,
                touch=True,
            )
            return self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            )._sync_crm_roster()

        if self and not roster_aggregate_is_locked(self.env):
            bindings, _users, _grants = lock_roster_aggregate(
                self.env,
                team_binding_ids=self.ids,
                crm_team_ids=self.mapped("crm_team_id").ids,
                touch=False,
            )
            return bindings._sync_crm_roster()

        for binding in (
            self.sudo()
            .filtered("active")
            .sorted(lambda item: (item.crm_team_id.id, item.id))
        ):
            # Recheck the one-to-one authority before applying its roster.
            binding._check_crm_team_available(
                binding.crm_team_id.ids, excluding=binding
            )
            agents, supervisors = binding._desired_crm_roster()
            binding._validate_crm_roster(agents, supervisors)
            stale_managed_roles = binding._sync_contact_center_roles(
                agents, supervisors
            )
            team = binding.contact_center_team_id.sudo()
            if not (
                set(team.agent_ids.ids) == set(agents.ids)
                and set(team.supervisor_ids.ids) == set(supervisors.ids)
            ):
                team.with_context(
                    contact_center_crm_roster_sync=CRM_ROSTER_SYNC_TOKEN,
                    contact_center_crm_binding_graph_lock=(
                        CRM_BINDING_GRAPH_LOCK_TOKEN
                    ),
                ).write(
                    {
                        "agent_ids": [(6, 0, agents.ids)],
                        "supervisor_ids": [(6, 0, supervisors.ids)],
                    }
                )
            # Revoke bridge-owned capabilities only after the atomic roster
            # replacement no longer references users released above. Active
            # claims held by another binding are preserved by the reconciler.
            binding._remove_unowned_roles(stale_managed_roles)
        return True


class ContactCenterCrmPipelineBinding(models.Model):
    _name = "contact.center.crm.pipeline.binding"
    _description = "Contact Center CRM Pipeline Binding"
    _rec_name = "pipeline_id"
    _order = "company_id, pipeline_id, id"
    _check_company_auto = True

    active = fields.Boolean(default=True)
    authority_revision = fields.Integer(
        required=True,
        default=1,
        readonly=True,
        copy=False,
        help="Internal MVCC fence for CRM/Contact Center topology changes.",
    )
    pipeline_id = fields.Many2one(
        "contact.center.pipeline",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    crm_team_id = fields.Many2one(
        "crm.team",
        string="CRM Sales Team",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
    )
    company_id = fields.Many2one(
        related="pipeline_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    stage_binding_ids = fields.One2many(
        "contact.center.crm.stage.binding",
        "pipeline_binding_id",
        string="CRM Stages",
    )

    _sql_constraints = [
        (
            "pipeline_unique",
            "unique(pipeline_id)",
            "A Contact Center pipeline can be bound to only one CRM team.",
        ),
        (
            "authority_revision_positive",
            "check(authority_revision > 0)",
            "The CRM binding revision must be positive.",
        ),
    ]

    @api.constrains("pipeline_id", "crm_team_id")
    def _check_binding_company(self):
        for binding in self:
            if not _crm_team_matches_company(binding.crm_team_id, binding.company_id):
                raise ValidationError(
                    _(
                        "The Contact Center pipeline and CRM team use different "
                        "companies."
                    )
                )

    @api.model_create_multi
    def create(self, vals_list):
        if any("authority_revision" in values for values in vals_list):
            raise AccessError(_("The CRM binding revision is managed internally."))
        pipeline_ids = [values.get("pipeline_id") for values in vals_list]
        crm_team_ids = [values.get("crm_team_id") for values in vals_list]
        if not binding_graph_is_locked(self.env):
            lock_binding_graph(
                self.env,
                pipeline_ids=pipeline_ids,
                crm_team_ids=crm_team_ids,
                touch=True,
            )
            bindings = self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            ).create(vals_list)
            # Do not leak the private lock capability through the recordset
            # returned by create(); subsequent commands are separate authority
            # decisions even when they run in the same database transaction.
            return self.browse(bindings.ids)
        self.env["contact.center.account"]._contact_center_lock_access_topology(
            pipeline_ids=_positive_ids(pipeline_ids)
        )
        pipelines = (
            self.env["contact.center.pipeline"]
            .sudo()
            .with_context(active_test=False)
            .browse(_positive_ids(pipeline_ids))
        )
        crm_teams = (
            self.env["crm.team"]
            .sudo()
            .with_context(active_test=False)
            .browse(_positive_ids(crm_team_ids))
        )
        if any(values.get("active", True) for values in vals_list) and (
            any(not pipeline.active for pipeline in pipelines)
            or any(not team.active for team in crm_teams)
        ):
            raise ValidationError(_("The pipeline and CRM team must be active."))
        bindings = super().create(vals_list)
        bindings.with_context(
            contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
        ).filtered("active")._sync_crm_stage_catalog()
        return bindings

    def write(self, values):
        if "authority_revision" in values:
            raise AccessError(_("The CRM binding revision is managed internally."))
        if not binding_graph_is_locked(self.env):
            lock_binding_graph(
                self.env,
                pipeline_binding_ids=self.ids,
                crm_team_ids=self.mapped("crm_team_id").ids
                + ([values["crm_team_id"]] if values.get("crm_team_id") else []),
                touch=True,
            )
            return self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            ).write(values)
        self.env["contact.center.account"]._contact_center_lock_access_topology(
            pipeline_ids=self.mapped("pipeline_id").ids
        )
        if "pipeline_id" in values and any(
            values["pipeline_id"] != binding.pipeline_id.id for binding in self
        ):
            raise ValidationError(
                _("The Contact Center side of a CRM pipeline binding is immutable.")
            )
        crm_team_changed = "crm_team_id" in values and any(
            values["crm_team_id"] != binding.crm_team_id.id for binding in self
        )
        if values.get("active") is True or (
            crm_team_changed and any(self.mapped("active"))
        ):
            self.invalidate_recordset(["active", "pipeline_id"])
            for binding in self:
                target_crm_team = (
                    self.env["crm.team"]
                    .sudo()
                    .with_context(active_test=False)
                    .browse(values.get("crm_team_id") or binding.crm_team_id.id)
                )
                if not target_crm_team.active or not binding.pipeline_id.active:
                    raise ValidationError(
                        _("The pipeline and CRM team must be active.")
                    )
        deactivated = values.get("active") is False
        if (crm_team_changed or deactivated) and self.env[
            "contact.center.crm.case.link"
        ].sudo().search_count(
            [
                ("case_id.pipeline_id", "in", self.pipeline_id.ids),
                ("state", "=", "active"),
                ("lead_id", "!=", False),
            ]
        ):
            raise ValidationError(
                _(
                    "A pipeline binding used by linked cases cannot be changed "
                    "or archived."
                )
            )
        result = super().write(values)
        if crm_team_changed or values.get("active") is True:
            self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            )._sync_crm_stage_catalog()
        return result

    def unlink(self):
        if not physical_unlink_is_allowed(self.env):
            raise AccessError(
                _("CRM pipeline bindings are lifecycle records; archive them instead.")
            )
        return super().unlink()

    def _effective_crm_stages(self):
        self.ensure_one()
        return self.env["crm.stage"].search(
            ["|", ("team_id", "=", False), ("team_id", "=", self.crm_team_id.id)],
            order="sequence, name, id",
        )

    def _ensure_stage_catalog_access(self):
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_admin"
        ):
            raise AccessError(
                _("Only Contact Center administrators can synchronize CRM stages.")
            )

    def action_sync_stage_catalog(self):
        self._ensure_stage_catalog_access()
        # Keep company record rules authoritative at the public command
        # boundary; `_sync_crm_stage_catalog` necessarily uses sudo internally.
        self.check_access_rights("write")
        self.check_access_rule("write")
        self._sync_crm_stage_catalog()
        return True

    def _sync_crm_stage_catalog(self):
        """Mirror the effective CRM catalog without changing any CRM membership."""

        StageBinding = self.env["contact.center.crm.stage.binding"].sudo()
        CoreCase = self.env["contact.center.case"].sudo()
        CoreStage = self.env["contact.center.pipeline.stage"].sudo()
        pipeline_bindings = (
            self.sudo()
            .filtered("active")
            .sorted(key=lambda item: (item.pipeline_id.id, item.id))
        )
        if pipeline_bindings and not binding_graph_is_locked(self.env):
            lock_binding_graph(
                self.env,
                pipeline_binding_ids=pipeline_bindings.ids,
                crm_team_ids=pipeline_bindings.mapped("crm_team_id").ids,
                touch=True,
            )
        sync_context = {
            "contact_center_crm_binding_graph_lock": CRM_BINDING_GRAPH_LOCK_TOKEN
        }
        pipeline_bindings = pipeline_bindings.with_context(**sync_context)
        StageBinding = StageBinding.with_context(**sync_context)
        CoreCase = CoreCase.with_context(**sync_context)
        CoreStage = CoreStage.with_context(**sync_context)
        pipeline_ids = pipeline_bindings.mapped("pipeline_id").ids
        if pipeline_ids:
            self.env.cr.execute(
                """
                SELECT id
                  FROM contact_center_pipeline
                 WHERE id = ANY(%s)
              ORDER BY id
                   FOR UPDATE
                """,
                [pipeline_ids],
            )
        for pipeline_binding in pipeline_bindings:
            pipeline_binding.invalidate_recordset(["stage_binding_ids"])
            effective_stages = pipeline_binding._effective_crm_stages()
            if not effective_stages:
                raise ValidationError(
                    _("The selected CRM team has no effective pipeline stages.")
                )
            all_stage_bindings = pipeline_binding.with_context(
                active_test=False
            ).stage_binding_ids
            existing_by_crm = {
                item.crm_stage_id.id: item for item in all_stage_bindings
            }
            desired_initial = effective_stages.filtered(lambda stage: not stage.is_won)[
                :1
            ]
            if not desired_initial:
                raise ValidationError(
                    _("The selected CRM team needs at least one stage that is not won.")
                )

            mapped_core_stages = all_stage_bindings.mapped("stage_id")
            all_core_stages = CoreStage.with_context(active_test=False).search(
                [("pipeline_id", "=", pipeline_binding.pipeline_id.id)], order="id"
            )
            unbound_stages = all_core_stages - mapped_core_stages
            local_initial_stage_ids = set(unbound_stages.filtered("is_initial").ids)
            local_cases = CoreCase.with_context(active_test=False).search(
                [
                    ("stage_id", "in", unbound_stages.ids),
                    ("active", "=", True),
                ]
            )
            non_transitionable = local_cases.filtered(
                lambda case: case.stage_revision != 0
                or case.stage_id.id not in local_initial_stage_ids
            )
            if non_transitionable:
                raise ValidationError(
                    _(
                        "Pipeline %(pipeline)s still has cases with history in "
                        "local stages. Move or close those cases before binding "
                        "the pipeline to CRM.",
                        pipeline=pipeline_binding.pipeline_id.display_name,
                    )
                )

            seen_crm_stage_ids = set()
            core_stage_by_crm_id = {}
            for crm_stage in effective_stages:
                seen_crm_stage_ids.add(crm_stage.id)
                stage_binding = existing_by_crm.get(crm_stage.id)
                stage_values = {
                    "name": crm_stage.name,
                    "sequence": crm_stage.sequence,
                    "fold": crm_stage.fold,
                    "is_closed": crm_stage.is_won,
                    "active": True,
                }
                if stage_binding:
                    core_stage_by_crm_id[crm_stage.id] = stage_binding.stage_id
                    stage_binding.stage_id.with_context(
                        contact_center_crm_catalog_sync=CRM_CATALOG_SYNC_TOKEN
                    ).write(stage_values)
                    if not stage_binding.active:
                        stage_binding.active = True
                    continue
                core_stage = (
                    self.env["contact.center.pipeline.stage"]
                    .sudo()
                    .with_context(
                        contact_center_crm_catalog_sync=CRM_CATALOG_SYNC_TOKEN
                    )
                    .create(
                        {
                            "pipeline_id": pipeline_binding.pipeline_id.id,
                            "code": "crm_%s" % crm_stage.id,
                            **stage_values,
                        }
                    )
                )
                StageBinding.create(
                    {
                        "pipeline_binding_id": pipeline_binding.id,
                        "stage_id": core_stage.id,
                        "crm_stage_id": crm_stage.id,
                    }
                )
                core_stage_by_crm_id[crm_stage.id] = core_stage

            desired_initial_stage = core_stage_by_crm_id[desired_initial.id]
            pipeline_binding.pipeline_id.with_context(
                contact_center_crm_catalog_sync=CRM_CATALOG_SYNC_TOKEN
            )._contact_center_set_initial_stage(desired_initial_stage)
            for case in local_cases.sorted("id"):
                case.with_context(
                    contact_center_crm_stage_sync=CRM_CASE_STAGE_SYNC_TOKEN,
                    contact_center_case_transition_token=(
                        CONTACT_CENTER_CASE_TRANSITION_TOKEN
                    ),
                ).action_transition(
                    desired_initial_stage.id,
                    expected_revision=0,
                    source="integration",
                )
            unbound_stages.with_context(
                contact_center_crm_catalog_sync=CRM_CATALOG_SYNC_TOKEN
            ).write({"active": False, "is_initial": False})

            stale_bindings = all_stage_bindings.filtered(
                lambda item: item.crm_stage_id.id not in seen_crm_stage_ids
            )
            for stage_binding in stale_bindings:
                if CoreCase.search_count(
                    [("stage_id", "=", stage_binding.stage_id.id)]
                ):
                    raise ValidationError(
                        _(
                            "CRM stage %(stage)s is no longer available to team "
                            "%(team)s but is still used by a Contact Center case.",
                            stage=stage_binding.crm_stage_id.display_name,
                            team=pipeline_binding.crm_team_id.display_name,
                        )
                    )
                stage_binding.stage_id.with_context(
                    contact_center_crm_catalog_sync=CRM_CATALOG_SYNC_TOKEN
                ).write({"active": False, "is_initial": False})
                stage_binding.active = False
            pipeline_binding.pipeline_id._contact_center_initial_stage()
        return True

    def action_open_crm_team(self):
        self.ensure_one()
        self.crm_team_id.check_access_rights("read")
        self.crm_team_id.check_access_rule("read")
        return {
            "type": "ir.actions.act_window",
            "name": self.crm_team_id.display_name,
            "res_model": "crm.team",
            "res_id": self.crm_team_id.id,
            "view_mode": "form",
            "target": "current",
        }


class ContactCenterCrmStageBinding(models.Model):
    _name = "contact.center.crm.stage.binding"
    _description = "Contact Center CRM Stage Binding"
    _rec_name = "stage_id"
    _order = "pipeline_binding_id, stage_id, id"
    _check_company_auto = True

    active = fields.Boolean(default=True)
    pipeline_binding_id = fields.Many2one(
        "contact.center.crm.pipeline.binding",
        required=True,
        index=True,
        ondelete="cascade",
        check_company=True,
    )
    stage_id = fields.Many2one(
        "contact.center.pipeline.stage",
        string="Contact Center Stage",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    crm_stage_id = fields.Many2one(
        "crm.stage",
        required=True,
        index=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one(
        related="pipeline_binding_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )

    _sql_constraints = [
        (
            "stage_unique",
            "unique(stage_id)",
            "A Contact Center stage can be bound to only one CRM stage.",
        ),
        (
            "crm_stage_pipeline_unique",
            "unique(pipeline_binding_id, crm_stage_id)",
            "A CRM stage can appear only once in a Contact Center pipeline.",
        ),
    ]

    @api.constrains("pipeline_binding_id", "stage_id", "crm_stage_id")
    def _check_binding_consistency(self):
        for binding in self:
            pipeline_binding = binding.pipeline_binding_id
            if binding.stage_id.pipeline_id != pipeline_binding.pipeline_id:
                raise ValidationError(
                    _("The Contact Center stage belongs to another pipeline.")
                )
            if (
                binding.crm_stage_id.team_id
                and binding.crm_stage_id.team_id != pipeline_binding.crm_team_id
            ):
                raise ValidationError(_("The CRM stage belongs to another sales team."))

    def write(self, values):
        if not binding_graph_is_locked(self.env):
            lock_binding_graph(
                self.env,
                pipeline_binding_ids=self.mapped("pipeline_binding_id").ids,
                touch=True,
            )
        immutable = {"pipeline_binding_id", "stage_id", "crm_stage_id"}
        if immutable & set(values):
            raise ValidationError(_("CRM stage mappings are managed by catalog sync."))
        if values.get("active") is False and self.env[
            "contact.center.case"
        ].sudo().search_count([("stage_id", "in", self.stage_id.ids)]):
            raise ValidationError(
                _("A CRM stage mapping used by cases cannot be archived.")
            )
        return super().write(values)

    def unlink(self):
        if not binding_graph_is_locked(self.env):
            lock_binding_graph(
                self.env,
                pipeline_binding_ids=self.mapped("pipeline_binding_id").ids,
                touch=True,
            )
        if (
            self.env["contact.center.case"]
            .sudo()
            .search_count([("stage_id", "in", self.stage_id.ids)])
        ):
            raise ValidationError(
                _("A CRM stage mapping used by cases cannot be deleted.")
            )
        return super().unlink()

    def action_open_crm_stage(self):
        self.ensure_one()
        self.crm_stage_id.check_access_rights("read")
        self.crm_stage_id.check_access_rule("read")
        return {
            "type": "ir.actions.act_window",
            "name": self.crm_stage_id.display_name,
            "res_model": "crm.stage",
            "res_id": self.crm_stage_id.id,
            "view_mode": "form",
            "target": "current",
        }


class ContactCenterPipelineStage(models.Model):
    _inherit = "contact.center.pipeline.stage"

    crm_stage_binding_ids = fields.One2many(
        "contact.center.crm.stage.binding",
        "stage_id",
        string="CRM Stage Binding",
    )

    @api.model_create_multi
    def create(self, vals_list):
        pipeline_ids = _positive_ids(values.get("pipeline_id") for values in vals_list)
        if (
            self.env.context.get("contact_center_crm_catalog_sync")
            is not CRM_CATALOG_SYNC_TOKEN
        ):
            lock_binding_graph(self.env, pipeline_ids=pipeline_ids, touch=True)
            if pipeline_ids and self.env[
                "contact.center.crm.pipeline.binding"
            ].sudo().search_count(
                [("pipeline_id", "in", pipeline_ids), ("active", "=", True)]
            ):
                raise ValidationError(
                    _("CRM-managed stages must be created from the CRM pipeline.")
                )
        return super().create(vals_list)

    def write(self, values):
        if (
            self.env.context.get("contact_center_crm_catalog_sync")
            is not CRM_CATALOG_SYNC_TOKEN
        ):
            lock_binding_graph(
                self.env, pipeline_ids=self.mapped("pipeline_id").ids, touch=True
            )
        protected = {"name", "sequence", "fold", "is_closed", "is_initial", "active"}
        if (
            protected & set(values)
            and self.env.context.get("contact_center_crm_catalog_sync")
            is not CRM_CATALOG_SYNC_TOKEN
            and self.env["contact.center.crm.pipeline.binding"]
            .sudo()
            .search_count(
                [("pipeline_id", "in", self.pipeline_id.ids), ("active", "=", True)]
            )
        ):
            raise ValidationError(
                _("CRM-managed stages must be configured from the CRM pipeline.")
            )
        return super().write(values)

    def unlink(self):
        lock_binding_graph(
            self.env, pipeline_ids=self.mapped("pipeline_id").ids, touch=True
        )
        if (
            self.env["contact.center.crm.pipeline.binding"]
            .sudo()
            .search_count(
                [("pipeline_id", "in", self.pipeline_id.ids), ("active", "=", True)]
            )
        ):
            raise ValidationError(
                _("CRM-managed stages must be deleted from the CRM pipeline.")
            )
        return super().unlink()

    def _contact_center_validate_used_pipeline_readiness(self):
        if (
            self.env.context.get("contact_center_crm_catalog_sync")
            is CRM_CATALOG_SYNC_TOKEN
        ):
            return True
        return super()._contact_center_validate_used_pipeline_readiness()


class ContactCenterTeam(models.Model):
    _inherit = "contact.center.team"

    crm_team_binding_ids = fields.One2many(
        "contact.center.crm.team.binding",
        "contact_center_team_id",
        string="CRM Team Binding",
    )
    crm_team_id = fields.Many2one(
        "crm.team",
        compute="_compute_crm_team_projection",
        string="CRM Sales Team",
    )

    @api.depends(
        "crm_team_binding_ids",
        "crm_team_binding_ids.crm_team_id",
        "crm_team_binding_ids.active",
    )
    def _compute_crm_team_projection(self):
        for team in self:
            team.crm_team_id = team.crm_team_binding_ids.filtered("active")[
                :1
            ].crm_team_id

    def _active_crm_roster_bindings(self):
        return (
            self.env["contact.center.crm.team.binding"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("contact_center_team_id", "in", self.ids),
                    ("active", "=", True),
                ]
            )
        )

    def write(self, values):
        if not binding_graph_is_locked(self.env):
            lock_binding_graph(self.env, team_ids=self.ids, touch=True)
            return self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            ).write(values)
        protected_roster = {"agent_ids", "supervisor_ids"} & set(values)
        if (
            protected_roster
            and self.env.context.get("contact_center_crm_roster_sync")
            is not CRM_ROSTER_SYNC_TOKEN
            and self._active_crm_roster_bindings()
        ):
            raise ValidationError(
                _(
                    "Agents and supervisors of a CRM-bound Contact Center team "
                    "must be managed from the CRM sales team."
                )
            )
        if values.get("active") is False and self._active_crm_roster_bindings():
            raise ValidationError(
                _("Archive the active CRM team binding before this team.")
            )
        return super().write(values)

    def unlink(self):
        if not binding_graph_is_locked(self.env):
            lock_binding_graph(self.env, team_ids=self.ids, touch=True)
            return self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            ).unlink()
        if self._active_crm_roster_bindings():
            raise ValidationError(
                _("Remove the active CRM team binding before deleting this team.")
            )
        return super().unlink()


class ContactCenterPipeline(models.Model):
    _inherit = "contact.center.pipeline"

    crm_pipeline_binding_ids = fields.One2many(
        "contact.center.crm.pipeline.binding",
        "pipeline_id",
        string="CRM Pipeline Binding",
    )
    crm_team_id = fields.Many2one(
        "crm.team",
        compute="_compute_crm_pipeline_projection",
        string="CRM Sales Team",
    )

    @api.depends(
        "crm_pipeline_binding_ids",
        "crm_pipeline_binding_ids.crm_team_id",
        "crm_pipeline_binding_ids.active",
    )
    def _compute_crm_pipeline_projection(self):
        for pipeline in self:
            binding = pipeline.crm_pipeline_binding_ids.filtered("active")[:1]
            pipeline.crm_team_id = binding.crm_team_id

    def write(self, values):
        if self.env.context.get(
            "contact_center_crm_catalog_sync"
        ) is not CRM_CATALOG_SYNC_TOKEN and not binding_graph_is_locked(self.env):
            lock_binding_graph(self.env, pipeline_ids=self.ids, touch=True)
            return self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            ).write(values)
        if values.get("active") is False and any(
            self.mapped("crm_pipeline_binding_ids").filtered("active")
        ):
            raise ValidationError(
                _("Archive or remove the active CRM binding before this pipeline.")
            )
        return super().write(values)

    def unlink(self):
        if not binding_graph_is_locked(self.env):
            lock_binding_graph(self.env, pipeline_ids=self.ids, touch=True)
            return self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            ).unlink()
        return super().unlink()


class ContactCenterAccount(models.Model):
    _inherit = "contact.center.account"

    def _contact_center_crm_binding_scope(self, values=None):
        values = values or {}
        team_ids = set(self.mapped("access_team_ids").ids)
        pipeline_ids = set(self.mapped("default_pipeline_id").ids)
        if values.get("access_team_ids"):
            team_ids.update(_relational_command_ids(values["access_team_ids"]))
        if values.get("default_pipeline_id"):
            pipeline_ids.add(int(values["default_pipeline_id"]))
        return team_ids, pipeline_ids

    def write(self, values):
        relevant = {"active", "access_team_ids", "default_pipeline_id"} & set(values)
        if relevant and not binding_graph_is_locked(self.env):
            team_ids, pipeline_ids = self._contact_center_crm_binding_scope(values)
            lock_binding_graph(
                self.env,
                team_ids=team_ids,
                pipeline_ids=pipeline_ids,
                touch=True,
            )
            return self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            ).write(values)
        return super().write(values)

    def unlink(self):
        if not binding_graph_is_locked(self.env):
            team_ids, pipeline_ids = self._contact_center_crm_binding_scope()
            lock_binding_graph(
                self.env,
                team_ids=team_ids,
                pipeline_ids=pipeline_ids,
                touch=True,
            )
            return self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            ).unlink()
        return super().unlink()
