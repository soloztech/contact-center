import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

_ONBOARDING_INTERNAL_CONTEXT = "contact_center_onboarding_internal"
_ONBOARDING_INTERNAL_TOKEN = object()
_ONBOARDING_ACTIVATION_CONTEXT = "contact_center_onboarding_activation"
_ONBOARDING_ACTIVATION_TOKEN = object()
_ONBOARDING_EVENT_RELEASE_BATCH_SIZE = 100
_ONBOARDING_EVENT_RELEASE_MAX_BATCH_SIZE = 500
_ONBOARDING_RECOVERY_CONNECTION_BATCH_SIZE = 100
_ONBOARDING_RECOVERY_CONNECTION_MAX_BATCH_SIZE = 1000
_ONBOARDING_RECOVERY_CURSOR_PARAMETER = (
    "contact_center.onboarding_release_recovery_cursor"
)


class ContactCenterProviderConnection(models.Model):
    _inherit = "contact.center.provider.connection"

    onboarding_ref = fields.Char(
        string="Onboarding Reference",
        readonly=True,
        copy=False,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Opaque idempotency reference of the guided setup that created this "
            "provider connection."
        ),
    )
    onboarding_activated_at = fields.Datetime(
        string="Guided Setup Activated At",
        readonly=True,
        copy=False,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "First explicit activation of this guided provider connection. It "
            "prevents a later standby connection from being mistaken for a new setup."
        ),
    )
    onboarding_ingress_revision = fields.Integer(
        required=True,
        readonly=True,
        copy=False,
        default=0,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Monotonic fence advanced whenever a provider callback is durably "
            "buffered before explicit guided activation."
        ),
    )

    _sql_constraints = [
        (
            "onboarding_ref_unique",
            "unique(onboarding_ref)",
            "The Contact Center onboarding reference must be unique.",
        ),
        (
            "onboarding_ingress_revision_nonnegative",
            "check(onboarding_ingress_revision >= 0)",
            "The Contact Center onboarding ingress revision cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if any(
            {"onboarding_activated_at", "onboarding_ingress_revision"}.intersection(
                values
            )
            for values in vals_list
        ) and (
            self.env.context.get(_ONBOARDING_INTERNAL_CONTEXT)
            is not _ONBOARDING_INTERNAL_TOKEN
        ):
            raise AccessError(_("Guided setup bookkeeping is managed internally."))
        for values in vals_list:
            if not values.get("onboarding_ref"):
                continue
            if (
                values.get("role") not in ("migration", "standby")
                or values.get("inbound_active")
                or values.get("outbound_active")
            ):
                raise ValidationError(
                    _(
                        "A guided provider connection must be staged before it can "
                        "be activated."
                    )
                )
        return super().create(vals_list)

    def write(self, values):
        if "onboarding_ref" in values and any(
            values["onboarding_ref"] != connection.onboarding_ref for connection in self
        ):
            raise AccessError(_("The guided setup reference is write-once."))
        if {"onboarding_activated_at", "onboarding_ingress_revision"}.intersection(
            values
        ) and (
            self.env.context.get(_ONBOARDING_INTERNAL_CONTEXT)
            is not _ONBOARDING_INTERNAL_TOKEN
        ):
            raise AccessError(_("Guided setup bookkeeping is managed internally."))
        if {"active", "role", "inbound_active", "outbound_active"}.intersection(
            values
        ) and self.env.context.get(
            _ONBOARDING_ACTIVATION_CONTEXT
        ) is not _ONBOARDING_ACTIVATION_TOKEN:
            for connection in self.filtered("onboarding_ref"):
                currently_live = bool(
                    connection.active
                    and connection.role == "primary"
                    and connection.inbound_active
                )
                requested_live = bool(
                    values.get("outbound_active") is True
                    or (
                        values.get("active", connection.active)
                        and values.get("role", connection.role) == "primary"
                        and values.get("inbound_active", connection.inbound_active)
                    )
                )
                if requested_live and not currently_live:
                    raise ValidationError(
                        _(
                            "Activate guided provider connections through the "
                            "controlled primary switch."
                        )
                    )
        return super().write(values)

    def _contact_center_activation_blockers(self, direction="inbound", now=None):
        """Return cached, secret-free reasons that make a cutover unsafe.

        Provider addons extend this method. It must remain pure: no provider I/O,
        no writes and no job scheduling are allowed from an activation preflight.
        """

        self.ensure_one()
        if direction not in ("inbound", "outbound"):
            raise ValidationError(_("The activation direction is invalid."))
        blockers = []
        if not self.account_id._contact_center_access_is_ready():
            blockers.append(_("inbox owner or access team has no valid attendant"))
        return blockers

    def _contact_center_assert_activation_ready(self, direction="inbound"):
        self.ensure_one()
        blockers = tuple(
            reason
            for reason in self._contact_center_activation_blockers(direction)
            if reason
        )
        if blockers:
            raise ValidationError(
                _(
                    "This provider connection is not ready: %(reason)s",
                    reason="; ".join(blockers),
                )
            )
        return True

    def action_enable_outbound(self):
        self.ensure_one()
        self._contact_center_assert_activation_ready("outbound")
        if not self.active or self.role != "primary" or not self.inbound_active:
            raise ValidationError(
                _("Activate this connection as the inbox primary before sending.")
            )
        self.write({"outbound_active": True})
        return True

    def action_disable_outbound(self):
        self.ensure_one()
        self.write({"outbound_active": False})
        return True

    def _contact_center_record_onboarding_ingress(self, expected_onboarding_ref):
        """Advance the callback fence while the ingress topology lock is held.

        The provider controller calls this in the same transaction that inserts
        a new blocked callback. A concurrent activation using PostgreSQL
        REPEATABLE READ must then either observe the callback or retry after a
        serialization conflict; it cannot activate from an older snapshot.
        """

        self.ensure_one()
        if not expected_onboarding_ref:
            raise ValidationError(_("The guided setup reference is required."))
        self.env.cr.execute(
            """
            UPDATE contact_center_provider_connection
               SET onboarding_ingress_revision = onboarding_ingress_revision + 1
             WHERE id = %s
               AND onboarding_ref = %s
               AND onboarding_activated_at IS NULL
         RETURNING onboarding_ingress_revision
            """,
            [self.id, expected_onboarding_ref],
        )
        row = self.env.cr.fetchone()
        self.invalidate_recordset(["onboarding_ingress_revision"])
        return int(row[0]) if row else False

    def _contact_center_release_onboarding_inbox_events(
        self,
        expected_onboarding_ref,
        after_id=0,
        limit=_ONBOARDING_EVENT_RELEASE_BATCH_SIZE,
    ):
        """Release callbacks buffered between pairing and explicit activation."""

        self.ensure_one()
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= _ONBOARDING_EVENT_RELEASE_MAX_BATCH_SIZE
        ):
            raise ValidationError(_("The onboarding release batch size is invalid."))
        if not self._contact_center_inbound_is_available():
            raise ValidationError(_("Activate inbound traffic before replaying setup."))
        self.env.cr.execute(
            """
            SELECT id
              FROM contact_center_inbox_event
             WHERE provider_connection_id = %s
               AND state = 'blocked'
               AND id > %s
               AND metadata_json ->> 'blocked_reason' = 'onboarding_not_activated'
               AND metadata_json ->> 'onboarding_ref' = %s
             ORDER BY id
             LIMIT %s
             FOR UPDATE
            """,
            [self.id, after_id, expected_onboarding_ref, limit],
        )
        events = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .browse([row[0] for row in self.env.cr.fetchall()])
        )
        if not events:
            return 0, 0
        events._contact_center_requeue_onboarding_activation(expected_onboarding_ref)
        return len(events), max(events.ids)

    def _contact_center_schedule_onboarding_inbox_release(self):
        """Mark cutover and enqueue a post-commit release in a fresh snapshot."""

        self.ensure_one()
        if not self.onboarding_ref or not self._contact_center_inbound_is_available():
            raise ValidationError(_("Activate guided inbound traffic before release."))
        if not self.onboarding_activated_at:
            self.with_context(
                **{_ONBOARDING_INTERNAL_CONTEXT: _ONBOARDING_INTERNAL_TOKEN}
            ).write({"onboarding_activated_at": fields.Datetime.now()})
        self._contact_center_enqueue_onboarding_release(self.onboarding_ref, 0)
        return True

    def _contact_center_enqueue_onboarding_release(
        self, expected_onboarding_ref, after_id
    ):
        self.ensure_one()
        self.with_delay(
            identity_key="contact_center:onboarding_release:%s:%s:%s"
            % (self.id, expected_onboarding_ref, after_id),
            max_retries=5,
            priority=8,
            description="Contact Center onboarding inbox release %s" % self.id,
        )._job_release_onboarding_inbox_events(expected_onboarding_ref, after_id)
        return True

    def _job_release_onboarding_inbox_events(self, expected_onboarding_ref, after_id=0):
        """Replay the pairing window only after activation committed."""

        self.ensure_one()
        if not self.env.context.get("job_uuid"):
            raise ValidationError(_("Onboarding release must run through queue_job."))
        connection = self.sudo().exists()
        if not connection:
            return False
        connection._contact_center_lock_topology(connection.account_id.ids)
        connection.invalidate_recordset(
            [
                "active",
                "role",
                "inbound_active",
                "onboarding_ref",
                "onboarding_activated_at",
            ]
        )
        if (
            connection.onboarding_ref != expected_onboarding_ref
            or not connection.onboarding_activated_at
            or not connection._contact_center_inbound_is_available()
        ):
            return False
        (
            released_count,
            last_id,
        ) = connection._contact_center_release_onboarding_inbox_events(
            expected_onboarding_ref,
            after_id=after_id,
            limit=_ONBOARDING_EVENT_RELEASE_BATCH_SIZE,
        )
        if last_id:
            connection.env.cr.execute(
                """
                SELECT 1
                  FROM contact_center_inbox_event
                 WHERE provider_connection_id = %s
                   AND id > %s
                   AND state = 'blocked'
                   AND metadata_json ->> 'blocked_reason' =
                       'onboarding_not_activated'
                   AND metadata_json ->> 'onboarding_ref' = %s
                 LIMIT 1
                """,
                [connection.id, last_id, expected_onboarding_ref],
            )
            if connection.env.cr.fetchone():
                connection._contact_center_enqueue_onboarding_release(
                    expected_onboarding_ref, last_id
                )
        return released_count

    @api.model
    def _contact_center_onboarding_recovery_batch(self, limit):
        """Return one serialized round-robin page of eligible connections.

        The advisory lock and cursor update share the cron transaction.  A rollback
        therefore repeats the same page (at-least-once), while successful runs move
        beyond low IDs even if those connections remain eligible until their release
        jobs execute.
        """

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= _ONBOARDING_RECOVERY_CONNECTION_MAX_BATCH_SIZE
        ):
            raise ValidationError(_("The onboarding recovery batch size is invalid."))
        self.flush_model(
            [
                "active",
                "role",
                "inbound_active",
                "onboarding_ref",
                "onboarding_activated_at",
            ]
        )
        self.env["contact.center.inbox.event"].sudo().flush_model(
            ["provider_connection_id", "state", "metadata_json"]
        )
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            [
                "contact.center.scheduler:provider_connection",
                "onboarding_release_recovery",
            ],
        )
        parameters = self.env["ir.config_parameter"].sudo()
        raw_cursor = parameters.get_param(
            _ONBOARDING_RECOVERY_CURSOR_PARAMETER,
            "0",
        )
        try:
            cursor_id = max(0, int(raw_cursor or 0))
        except (TypeError, ValueError):
            cursor_id = 0

        def eligible_ids(operator, boundary, batch_limit):
            if operator not in (">", "<="):
                raise ValidationError(_("The onboarding recovery cursor is invalid."))
            self.env.cr.execute(
                """
                SELECT connection.id
                  FROM contact_center_provider_connection AS connection
                 WHERE connection.id %s %%s
                   AND connection.active IS TRUE
                   AND connection.role = 'primary'
                   AND connection.inbound_active IS TRUE
                   AND connection.onboarding_activated_at IS NOT NULL
                   AND EXISTS (
                        SELECT 1
                          FROM contact_center_inbox_event AS event
                         WHERE event.provider_connection_id = connection.id
                           AND event.state = 'blocked'
                           AND event.metadata_json ->> 'blocked_reason' =
                               'onboarding_not_activated'
                           AND event.metadata_json ->> 'onboarding_ref' =
                               connection.onboarding_ref
                   )
                 ORDER BY connection.id
                 LIMIT %%s
                """
                % operator,
                [boundary, batch_limit],
            )
            return [row[0] for row in self.env.cr.fetchall()]

        selected_ids = eligible_ids(">", cursor_id, limit)
        remaining = limit - len(selected_ids)
        if remaining:
            selected_ids.extend(eligible_ids("<=", cursor_id, remaining))
        connections = self.sudo().browse(selected_ids)
        if connections:
            parameters.set_param(
                _ONBOARDING_RECOVERY_CURSOR_PARAMETER,
                str(connections[-1].id),
            )
        return connections

    @api.model
    def _cron_recover_onboarding_inbox_releases(
        self, limit=_ONBOARDING_RECOVERY_CONNECTION_BATCH_SIZE
    ):
        """Recover missing release jobs without starving higher connection IDs."""

        connections = self._contact_center_onboarding_recovery_batch(limit)
        for connection in connections:
            connection._contact_center_enqueue_onboarding_release(
                connection.onboarding_ref, 0
            )
        return len(connections)

    def action_use_as_primary(self):
        """Make guided activation bookkeeping an invariant of every cutover path."""

        self.ensure_one()
        activation_self = self.with_context(
            **{_ONBOARDING_ACTIVATION_CONTEXT: _ONBOARDING_ACTIVATION_TOKEN}
        )
        result = super(
            ContactCenterProviderConnection, activation_self
        ).action_use_as_primary()
        if self.onboarding_ref:
            self._contact_center_schedule_onboarding_inbox_release()
        return result

    def _contact_center_onboarding_resume_values(self):
        self.ensure_one()
        return {}

    def _contact_center_onboarding_resume_step(self):
        self.ensure_one()
        if self.role == "primary" and self.inbound_active:
            return "done"
        return "connect"

    def action_resume_onboarding(self):
        self.ensure_one()
        if not (
            self.env.is_superuser()
            or self.env.user.has_group("contact_center_base.group_contact_center_admin")
        ):
            raise AccessError(
                _("Only Contact Center administrators can resume guided setup.")
            )
        if not self.onboarding_ref:
            raise ValidationError(
                _("This connection was not created by the guided setup.")
            )
        if self.company_id not in self.env.companies:
            raise AccessError(_("This connection belongs to another company."))
        wizard_model = self.env["contact.center.account.setup.wizard"]
        wizard = wizard_model.search([("setup_ref", "=", self.onboarding_ref)], limit=1)
        values = {
            "setup_ref": self.onboarding_ref,
            "step": self._contact_center_onboarding_resume_step(),
            "user_id": self.env.user.id,
            "company_id": self.company_id.id,
            "provider_key": self.adapter_key,
            "account_id": self.account_id.id,
            "connection_id": self.id,
            "inbox_name": self.account_id.name,
            "owner_user_id": self.account_id.owner_user_id.id,
            "default_team_id": self.account_id.default_team_id.id,
            "auto_assignment_user_id": self.account_id.auto_assignment_user_id.id,
            "enable_outbound": self.outbound_active,
            **self._contact_center_onboarding_resume_values(),
        }
        if wizard:
            wizard._contact_center_internal().write(values)
        else:
            wizard = wizard_model.with_context(
                **{_ONBOARDING_INTERNAL_CONTEXT: _ONBOARDING_INTERNAL_TOKEN}
            ).create(values)
        return wizard._contact_center_open_action()


class ContactCenterAccountSetupWizard(models.TransientModel):
    _name = "contact.center.account.setup.wizard"
    _description = "Contact Center Guided Inbox Setup"
    _rec_name = "inbox_name"

    def _default_setup_ref(self):
        return str(uuid.uuid4())

    step = fields.Selection(
        [
            ("inbox", "Inbox"),
            ("provider", "Provider"),
            ("connect", "Connect"),
            ("ready", "Review"),
            ("done", "Ready"),
        ],
        required=True,
        readonly=True,
        default="inbox",
    )
    setup_ref = fields.Char(
        required=True,
        readonly=True,
        copy=False,
        default=_default_setup_ref,
    )
    user_id = fields.Many2one(
        "res.users",
        required=True,
        readonly=True,
        default=lambda self: self.env.user,
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )
    inbox_name = fields.Char(required=True)
    owner_user_id = fields.Many2one(
        "res.users",
        string="Inbox Owner",
        default=lambda self: self.env.user,
        domain=(
            "[('active', '=', True), ('share', '=', False), "
            "('company_ids', 'in', company_id)]"
        ),
        help="Use an owner without a team for an exclusive personal inbox.",
    )
    default_team_id = fields.Many2one(
        "contact.center.team",
        string="Access Team",
        check_company=True,
        domain="[('company_id', '=', company_id), ('active', '=', True)]",
    )
    auto_assignment_eligible_user_ids = fields.Many2many(
        "res.users",
        string="Eligible Automatic Assignees",
        compute="_compute_auto_assignment_eligible_user_ids",
        compute_sudo=True,
    )
    auto_assignment_user_id = fields.Many2one(
        "res.users",
        string="Automatic Assignee",
        domain="[('id', 'in', auto_assignment_eligible_user_ids)]",
        help=(
            "Leave empty to keep automatic assignment disabled. When set, new "
            "inbound conversations without a responsible agent are assigned to "
            "this user, who must belong to the inbox owner/access-team scope."
        ),
    )
    provider_key = fields.Selection(
        selection="_contact_center_onboarding_provider_choices",
        string="Provider",
        required=True,
    )
    provider_available = fields.Boolean(
        default="_default_provider_available",
        readonly=True,
    )
    outbound_signature_enabled = fields.Boolean(string="Sign Agent Messages")
    mark_read_enabled = fields.Boolean(string="Mark Messages as Read")
    show_deleted_message_content = fields.Boolean(
        help=(
            "Keep deleted content visible to agents with a deletion marker and "
            "strikethrough. When disabled, remove the operational content and show "
            "only a tombstone."
        ),
    )
    group_inbound_enabled = fields.Boolean(string="Receive Group Messages")
    group_outbound_enabled = fields.Boolean(string="Send Group Messages")
    attribution_ui_enabled = fields.Boolean(string="Show Attribution to Agents")
    enable_outbound = fields.Boolean(
        string="Enable Sending after Activation",
        default=True,
        help=(
            "Sending is enabled only after the provider-specific outbound "
            "preflight also passes."
        ),
    )
    account_id = fields.Many2one(
        "contact.center.account",
        readonly=True,
        copy=False,
        ondelete="set null",
    )
    connection_id = fields.Many2one(
        "contact.center.provider.connection",
        readonly=True,
        copy=False,
        ondelete="set null",
    )

    _sql_constraints = [
        (
            "setup_ref_unique",
            "unique(setup_ref)",
            "The guided setup reference must be unique.",
        ),
    ]

    @api.model
    def _contact_center_onboarding_provider_choices(self):
        """Providers opt in by extending this selection from their addon."""

        return []

    @api.model
    def _default_provider_available(self):
        return bool(self._contact_center_onboarding_provider_choices())

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
        account_model = self.env["contact.center.account"]
        for wizard in self:
            wizard.auto_assignment_eligible_user_ids = (
                account_model._contact_center_users_for_access_scope(
                    owner=wizard.owner_user_id,
                    team=wizard.default_team_id,
                )
            )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for original in vals_list:
            values = dict(original)
            values["provider_available"] = self._default_provider_available()
            if (
                self.env.context.get(_ONBOARDING_INTERNAL_CONTEXT)
                is not _ONBOARDING_INTERNAL_TOKEN
            ):
                values.pop("account_id", None)
                values.pop("connection_id", None)
                values["setup_ref"] = self._default_setup_ref()
                values["step"] = "inbox"
                values["user_id"] = self.env.user.id
            prepared.append(values)
        return super().create(prepared)

    def write(self, values):
        protected = {
            "account_id",
            "connection_id",
            "provider_available",
            "setup_ref",
            "step",
            "user_id",
        }
        if protected.intersection(values) and (
            self.env.context.get(_ONBOARDING_INTERNAL_CONTEXT)
            is not _ONBOARDING_INTERNAL_TOKEN
        ):
            raise AccessError(_("Guided setup state is managed internally."))
        return super().write(values)

    def _contact_center_internal(self):
        return self.with_context(
            **{_ONBOARDING_INTERNAL_CONTEXT: _ONBOARDING_INTERNAL_TOKEN}
        )

    def _contact_center_require_admin(self):
        self.ensure_one()
        if not (
            self.env.is_superuser()
            or self.env.user.has_group("contact_center_base.group_contact_center_admin")
        ):
            raise AccessError(
                _("Only Contact Center administrators can configure inboxes.")
            )
        if self.user_id != self.env.user:
            raise AccessError(_("This guided setup belongs to another user."))
        if self.company_id not in self.env.companies:
            raise AccessError(_("The selected company is outside your access scope."))
        self.check_access_rights("write")
        self.check_access_rule("write")
        return True

    def _contact_center_lock(self):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM contact_center_account_setup_wizard "
            "WHERE id = %s FOR UPDATE",
            [self.id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(_("The guided setup no longer exists."))
        self.invalidate_recordset(["account_id", "connection_id", "step"])

    def _contact_center_open_action(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Add Inbox"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def _contact_center_onboarding_platform(self):
        self.ensure_one()
        raise ValidationError(
            _("The selected provider does not support guided setup yet.")
        )

    def _contact_center_onboarding_validate_provider(self):
        self.ensure_one()
        self._contact_center_onboarding_platform()
        return True

    def _contact_center_onboarding_connection_values(self):
        self.ensure_one()
        raise ValidationError(
            _("The selected provider does not support guided setup yet.")
        )

    def _contact_center_onboarding_start(self):
        self.ensure_one()
        raise ValidationError(
            _("The selected provider does not support guided setup yet.")
        )

    def _contact_center_onboarding_refresh(self):
        self.ensure_one()
        return self.step

    def _contact_center_onboarding_revalidate(self):
        self.ensure_one()
        return self._contact_center_onboarding_refresh()

    def _contact_center_onboarding_restart(self):
        self.ensure_one()
        return self._contact_center_onboarding_start()

    def _contact_center_onboarding_prepare_repair(self):
        self.ensure_one()
        raise ValidationError(
            _("The selected provider does not support guided repair yet.")
        )

    def _contact_center_onboarding_update_staged_connection(self, connection):
        self.ensure_one()
        connection.ensure_one()
        return True

    def _contact_center_validate_inbox(self):
        self.ensure_one()
        if not (self.inbox_name or "").strip():
            raise ValidationError(_("Enter a name for the inbox."))
        owner = self.owner_user_id
        team = self.default_team_id
        if team and (not team.active or team.company_id != self.company_id):
            raise ValidationError(_("Select an active team from the same company."))
        if owner:
            agent_group = self.env.ref("contact_center_base.group_contact_center_agent")
            if (
                not owner.active
                or owner.share
                or agent_group not in owner.groups_id
                or self.company_id not in owner.company_ids
            ):
                raise ValidationError(
                    _("Select an active internal Contact Center agent as owner.")
                )
        if not owner and not (team and (team.agent_ids or team.supervisor_ids)):
            raise ValidationError(
                _("Select an inbox owner or an access team with attendants.")
            )
        if self.auto_assignment_user_id and self.auto_assignment_user_id not in (
            self.env["contact.center.account"]._contact_center_users_for_access_scope(
                owner=owner,
                team=team,
            )
        ):
            raise ValidationError(
                _(
                    "Select an automatic assignee from the inbox owner or "
                    "access-team scope."
                )
            )
        return True

    def action_next_provider(self):
        self._contact_center_require_admin()
        self._contact_center_validate_inbox()
        if not self.provider_key:
            raise ValidationError(_("Select a provider."))
        self._contact_center_internal().write({"step": "provider"})
        return self._contact_center_open_action()

    def action_back_inbox(self):
        self._contact_center_require_admin()
        if self.connection_id:
            raise ValidationError(
                _("The staged connection already exists and cannot be rewritten.")
            )
        self._contact_center_internal().write({"step": "inbox"})
        return self._contact_center_open_action()

    def action_prepare(self):
        self._contact_center_require_admin()
        self._contact_center_lock()
        self._contact_center_validate_inbox()
        self._contact_center_onboarding_validate_provider()
        connection = self.connection_id.exists()
        if not connection:
            connection = (
                self.env["contact.center.provider.connection"]
                .with_context(active_test=False)
                .search([("onboarding_ref", "=", self.setup_ref)], limit=1)
            )
        account = connection.account_id if connection else self.account_id.exists()
        if not account:
            account = self.env["contact.center.account"].create(
                {
                    "name": self.inbox_name.strip(),
                    "active": True,
                    "company_id": self.company_id.id,
                    "platform": self._contact_center_onboarding_platform(),
                    "owner_user_id": self.owner_user_id.id,
                    "default_team_id": self.default_team_id.id,
                    "auto_assignment_user_id": self.auto_assignment_user_id.id,
                    "outbound_signature_enabled": self.outbound_signature_enabled,
                    "mark_read_enabled": self.mark_read_enabled,
                    "show_deleted_message_content": (self.show_deleted_message_content),
                    "group_inbound_enabled": self.group_inbound_enabled,
                    "group_outbound_enabled": self.group_outbound_enabled,
                    "attribution_ui_enabled": self.attribution_ui_enabled,
                }
            )
        if not connection:
            provider_values = self._contact_center_onboarding_connection_values()
            connection = self.env["contact.center.provider.connection"].create(
                dict(
                    provider_values,
                    name=self.inbox_name.strip(),
                    account_id=account.id,
                    adapter_key=self.provider_key,
                    onboarding_ref=self.setup_ref,
                    active=True,
                    role="migration",
                    inbound_active=False,
                    outbound_active=False,
                )
            )
        else:
            self._contact_center_onboarding_update_staged_connection(connection)
        self._contact_center_internal().write(
            {
                "account_id": account.id,
                "connection_id": connection.id,
                "step": "connect",
            }
        )
        return self._contact_center_open_action()

    def action_start_provider(self):
        self._contact_center_require_admin()
        if not self.connection_id:
            raise ValidationError(_("Create the staged connection first."))
        self._contact_center_onboarding_start()
        return self._contact_center_open_action()

    def action_refresh_provider(self):
        self._contact_center_require_admin()
        if not self.connection_id:
            raise ValidationError(_("Create the staged connection first."))
        self._contact_center_onboarding_refresh()
        return self._contact_center_open_action()

    def action_revalidate_provider(self):
        self._contact_center_require_admin()
        if not self.connection_id:
            raise ValidationError(_("The staged provider connection no longer exists."))
        self._contact_center_internal().write({"step": "connect"})
        self._contact_center_onboarding_revalidate()
        return self._contact_center_open_action()

    def action_restart_provider(self):
        self._contact_center_require_admin()
        if not self.connection_id:
            raise ValidationError(_("Create the staged connection first."))
        self._contact_center_onboarding_restart()
        return self._contact_center_open_action()

    def action_repair_provider(self):
        self._contact_center_require_admin()
        self._contact_center_lock()
        connection = self.connection_id.exists()
        if (
            not connection
            or connection.onboarding_ref != self.setup_ref
            or connection.onboarding_activated_at
        ):
            raise ValidationError(
                _("The staged provider connection is not eligible for guided repair.")
            )
        self._contact_center_onboarding_prepare_repair()
        self._contact_center_internal().write({"step": "provider"})
        return self._contact_center_open_action()

    def action_activate(self):
        self._contact_center_require_admin()
        connection = self.connection_id.exists()
        if not connection:
            raise ValidationError(_("The staged provider connection no longer exists."))
        connection.action_use_as_primary()
        if self.enable_outbound:
            connection.action_enable_outbound()
        self._contact_center_internal().write({"step": "done"})
        return self._contact_center_open_action()

    def action_open_inbox(self):
        self._contact_center_require_admin()
        return self._contact_center_inbox_action()

    def _contact_center_inbox_action(self):
        """Return the best installed inbox action without reversing dependencies.

        Interface addons can override this hook.  The base module deliberately
        falls back to its own account form and never resolves an XML ID owned by
        an optional UI layer.
        """

        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Inbox"),
            "res_model": "contact.center.account",
            "res_id": self.account_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_add_another(self):
        self._contact_center_require_admin()
        return {
            "type": "ir.actions.act_window",
            "name": _("Add Inbox"),
            "res_model": self._name,
            "view_mode": "form",
            "target": "current",
        }

    def action_cancel(self):
        self._contact_center_require_admin()
        action = self.env.ref("contact_center_base.action_contact_center_accounts")
        return action.read()[0]
