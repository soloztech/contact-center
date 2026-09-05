from psycopg2.errors import UniqueViolation

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_kanban.services.tokens import (
    CONTACT_CENTER_CASE_TRANSITION_TOKEN,
)

from .binding import (
    CRM_BINDING_GRAPH_LOCK_TOKEN,
    CRM_CASE_STAGE_SYNC_TOKEN,
    binding_graph_is_locked,
    lock_binding_graph,
    physical_unlink_is_allowed,
)
from .stage_sync import (
    CRM_STAGE_SYNC_GRAPH_LOCK_TOKEN,
    lock_stage_sync_graph,
    stage_sync_graph_is_locked,
)

CRM_CASE_LINK_SERVICE_TOKEN = object()
CRM_CASE_LINK_LIFECYCLE_TOKEN = object()


class ContactCenterCrmCaseLink(models.Model):
    _name = "contact.center.crm.case.link"
    _description = "Contact Center CRM Case Link"
    _rec_name = "lead_name_snapshot"
    _order = "linked_at desc, id desc"
    _check_company_auto = True

    case_id = fields.Many2one(
        "contact.center.case",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    lead_id = fields.Many2one(
        "crm.lead",
        index=True,
        ondelete="set null",
        check_company=True,
    )
    state = fields.Selection(
        [("active", "Active"), ("unlinked", "Unlinked")],
        required=True,
        readonly=True,
        default="active",
        index=True,
    )
    origin = fields.Selection(
        [("created", "Created from Contact Center"), ("linked", "Linked Existing")],
        required=True,
        readonly=True,
    )
    linked_by_id = fields.Many2one(
        "res.users",
        required=True,
        readonly=True,
        ondelete="restrict",
        default=lambda self: self.env.user,
    )
    linked_at = fields.Datetime(
        required=True,
        readonly=True,
        default=fields.Datetime.now,
    )
    unlinked_at = fields.Datetime(readonly=True, copy=False, index=True)
    unlinked_by_id = fields.Many2one(
        "res.users",
        readonly=True,
        copy=False,
        ondelete="restrict",
    )
    unlinked_reason = fields.Selection(
        [
            ("lead_deleted", "CRM Lead Deleted"),
            ("manual", "Manually Unlinked"),
        ],
        readonly=True,
        copy=False,
    )
    lead_name_snapshot = fields.Char(
        string="Latest CRM Lead Name",
        readonly=True,
        copy=False,
        help="Last known CRM lead name, refreshed on merge or unlink.",
    )
    lead_record_id_snapshot = fields.Integer(
        string="Latest CRM Lead Record ID",
        readonly=True,
        copy=False,
        index=True,
        help="Last known CRM lead ID, refreshed when a native merge moves the link.",
    )
    lead_type_snapshot = fields.Selection(
        [("lead", "Lead"), ("opportunity", "Opportunity")],
        string="Latest CRM Lead Type",
        readonly=True,
        copy=False,
    )
    lead_team_name_snapshot = fields.Char(
        string="Latest CRM Sales Team",
        readonly=True,
        copy=False,
    )
    original_lead_name_snapshot = fields.Char(
        string="Original CRM Lead Name",
        readonly=True,
        copy=False,
        help="CRM lead name captured once when this case link was created.",
    )
    original_lead_record_id_snapshot = fields.Integer(
        string="Original CRM Lead Record ID",
        readonly=True,
        copy=False,
        index=True,
        help="Immutable ID of the CRM lead first attached to this case.",
    )
    original_lead_type_snapshot = fields.Selection(
        [("lead", "Lead"), ("opportunity", "Opportunity")],
        string="Original CRM Lead Type",
        readonly=True,
        copy=False,
    )
    original_lead_team_name_snapshot = fields.Char(
        string="Original CRM Sales Team",
        readonly=True,
        copy=False,
    )
    company_id = fields.Many2one(
        related="case_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    channel_id = fields.Many2one(
        related="case_id.channel_id",
        store=True,
        readonly=True,
        index=True,
    )
    contact_center_team_id = fields.Many2one(
        related="case_id.team_id",
        store=True,
        readonly=True,
        index=True,
    )
    pipeline_id = fields.Many2one(
        related="case_id.pipeline_id",
        store=True,
        readonly=True,
        index=True,
    )
    lead_company_id = fields.Many2one(
        related="lead_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    lead_team_id = fields.Many2one(
        related="lead_id.team_id",
        store=True,
        readonly=True,
        index=True,
    )
    lead_user_id = fields.Many2one(
        related="lead_id.user_id",
        store=True,
        readonly=True,
        index=True,
    )

    _sql_constraints = [
        (
            "lifecycle_consistent",
            "check((lead_id IS NOT NULL AND state = 'active') OR "
            "(lead_id IS NULL AND state = 'unlinked'))",
            "An active CRM case link must reference a live CRM lead.",
        ),
        (
            "lead_record_id_snapshot_consistent",
            "check((lead_record_id_snapshot IS NOT NULL AND "
            "lead_record_id_snapshot > 0) OR "
            "(lead_id IS NULL AND state = 'unlinked'))",
            "A live CRM link must retain a positive CRM lead audit reference.",
        ),
    ]

    def init(self):
        """Keep one live bridge per case while retaining unlink tombstones."""
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "contact_center_crm_case_link_live_case_unique "
            "ON contact_center_crm_case_link (case_id) "
            "WHERE lead_id IS NOT NULL"
        )

    @api.constrains("lead_id", "state")
    def _check_lifecycle_consistency(self):
        for link in self:
            if (link.state == "active") != bool(link.lead_id):
                raise ValidationError(
                    _("An active CRM case link must reference a live CRM lead.")
                )

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("contact_center_crm_link_service")
            is not CRM_CASE_LINK_SERVICE_TOKEN
        ):
            raise AccessError(
                _("Create CRM case links from the explicit case link action.")
            )
        lead_ids = [values.get("lead_id") for values in vals_list]
        case_ids = [values.get("case_id") for values in vals_list]
        cases = (
            self.env["contact.center.case"]
            .sudo()
            .with_context(active_test=False)
            .browse(case_ids)
            .exists()
        )
        if len(cases) != len(set(case_ids)):
            raise ValidationError(
                _("The selected Contact Center case no longer exists.")
            )
        if not binding_graph_is_locked(self.env):
            lock_binding_graph(
                self.env,
                pipeline_ids=cases.mapped("pipeline_id").ids,
                team_ids=cases.mapped("team_id").ids,
                touch=True,
            )
        if not stage_sync_graph_is_locked(self.env):
            cases._contact_center_lock_crm_graph(
                lead_ids=lead_ids,
                touch_leads=True,
                touch_cases=True,
                bindings_already_locked=True,
            )
        cases.invalidate_recordset(["active", "pipeline_id", "team_id"])
        if any(not case.active for case in cases):
            raise ValidationError(_("An archived case cannot be linked to CRM."))
        existing = self.sudo().search(
            [("case_id", "in", case_ids), ("lead_id", "!=", False)], limit=1
        )
        if existing:
            raise ValidationError(_("This case already has a CRM lead."))
        if any(type(lead_id) is not int or lead_id <= 0 for lead_id in lead_ids):
            raise ValidationError(_("The selected CRM lead no longer exists."))
        normalized_lead_ids = set(lead_ids)
        leads = self.env["crm.lead"].browse(sorted(normalized_lead_ids)).exists()
        if len(leads) != len(normalized_lead_ids):
            raise ValidationError(_("The selected CRM lead no longer exists."))
        lead_by_id = {lead.id: lead for lead in leads}
        normalized = []
        for values in vals_list:
            item = dict(values)
            lead = lead_by_id[item["lead_id"]]
            item.update(
                {
                    "state": "active",
                    "lead_record_id_snapshot": lead.id,
                    "lead_name_snapshot": lead.display_name,
                    "lead_type_snapshot": lead.type,
                    "lead_team_name_snapshot": lead.team_id.display_name or False,
                    "original_lead_record_id_snapshot": lead.id,
                    "original_lead_name_snapshot": lead.display_name,
                    "original_lead_type_snapshot": lead.type,
                    "original_lead_team_name_snapshot": (
                        lead.team_id.display_name or False
                    ),
                    "unlinked_at": False,
                    "unlinked_by_id": False,
                    "unlinked_reason": False,
                }
            )
            item["linked_by_id"] = self.env.user.id
            item["linked_at"] = fields.Datetime.now()
            normalized.append(item)
        try:
            with self.env.cr.savepoint():
                links = super().create(normalized)
                links.flush_recordset(["case_id"])
        except UniqueViolation as error:
            if error.diag.constraint_name in (
                "contact_center_crm_case_link_case_unique",
                "contact_center_crm_case_link_live_case_unique",
            ):
                raise ValidationError(_("This case already has a CRM lead.")) from error
            raise
        links._validate_link_contract(check_access=True)
        return links

    def write(self, values):
        original_snapshot_fields = {
            "original_lead_record_id_snapshot",
            "original_lead_name_snapshot",
            "original_lead_type_snapshot",
            "original_lead_team_name_snapshot",
        }
        if original_snapshot_fields & set(values):
            raise ValidationError(_("The original CRM lead snapshot is immutable."))
        immutable = {
            "case_id",
            "lead_id",
            "origin",
            "linked_by_id",
            "linked_at",
            "lead_record_id_snapshot",
        }
        lifecycle_write = (
            self.env.context.get("contact_center_crm_link_lifecycle")
            is CRM_CASE_LINK_LIFECYCLE_TOKEN
        )
        if immutable & set(values) and not lifecycle_write:
            raise ValidationError(
                _("CRM case links are immutable; unlink and create an explicit link.")
            )
        lifecycle_fields = {
            "state",
            "unlinked_at",
            "unlinked_by_id",
            "unlinked_reason",
            "lead_name_snapshot",
            "lead_record_id_snapshot",
            "lead_type_snapshot",
            "lead_team_name_snapshot",
        }
        if lifecycle_fields & set(values) and not lifecycle_write:
            raise ValidationError(_("CRM case-link lifecycle is managed internally."))
        return super().write(values)

    def _contact_center_transfer_to_lead(self, lead):
        """Atomically move live case bridges to the native merge survivor."""

        lead = lead.exists()
        lead.ensure_one()
        links = self.filtered(lambda link: link.lead_id and link.state == "active")
        if not links:
            return True
        links.with_context(
            contact_center_crm_link_lifecycle=CRM_CASE_LINK_LIFECYCLE_TOKEN
        ).write(
            {
                "lead_id": lead.id,
                "lead_record_id_snapshot": lead.id,
                "lead_name_snapshot": lead.display_name,
                "lead_type_snapshot": lead.type,
                "lead_team_name_snapshot": lead.team_id.display_name or False,
            }
        )
        links._validate_link_contract()
        return True

    def _contact_center_tombstone(
        self,
        reason,
        *,
        check_access=False,
        actor_user_id=None,
    ):
        """Close live bridges while retaining immutable business evidence."""

        if reason not in ("lead_deleted", "manual"):
            raise ValidationError(_("The CRM unlink reason is invalid."))
        actor_user_id = actor_user_id or self.env.uid
        if type(actor_user_id) is not int or actor_user_id <= 0:  # noqa: E721
            raise ValidationError(_("The CRM unlink actor is invalid."))

        links = self.filtered(lambda link: link.lead_id and link.state == "active")
        if check_access:
            for link in links:
                link.case_id._check_crm_action_access(
                    lead=link.lead_id,
                    operation="write",
                )
        if links and not stage_sync_graph_is_locked(self.env):
            links.mapped("case_id")._contact_center_lock_crm_graph(
                lead_ids=links.mapped("lead_id").ids,
                touch_leads=True,
                touch_cases=True,
            )
        if links:
            links.invalidate_recordset(["case_id", "lead_id", "state"])
            links = links.filtered(lambda link: link.lead_id and link.state == "active")
        if check_access:
            # Authorization is deliberately repeated after the graph fence.  A
            # concurrent roster or CRM-rule change must win before any sudo
            # lifecycle write is admitted.
            for link in links:
                link.case_id._check_crm_action_access(
                    lead=link.lead_id,
                    operation="write",
                )
        if links:
            links._contact_center_before_tombstone(reason)
        for link in links:
            lead = link.lead_id
            link.sudo().with_context(
                contact_center_crm_link_lifecycle=CRM_CASE_LINK_LIFECYCLE_TOKEN
            ).write(
                {
                    "lead_id": False,
                    "lead_record_id_snapshot": lead.id,
                    "state": "unlinked",
                    "unlinked_at": fields.Datetime.now(),
                    "unlinked_by_id": actor_user_id,
                    "unlinked_reason": reason,
                    "lead_name_snapshot": lead.display_name,
                    "lead_type_snapshot": lead.type,
                    "lead_team_name_snapshot": lead.team_id.display_name or False,
                }
            )
        return True

    def _contact_center_before_tombstone(self, reason):
        """Run extension effects after fencing while live identity is available."""

        del reason
        return True

    def _contact_center_tombstone_deleted_lead(self, *, actor_user_id=None):
        """Close a live bridge before its CRM record is deleted."""

        return self._contact_center_tombstone(
            "lead_deleted",
            actor_user_id=actor_user_id,
        )

    def action_unlink(self):
        if any(link.state != "active" or not link.lead_id for link in self):
            raise ValidationError(_("This CRM case link is no longer active."))
        self._contact_center_tombstone("manual", check_access=True)
        return True

    def unlink(self):
        if physical_unlink_is_allowed(self.env):
            return super().unlink()
        raise AccessError(
            _("CRM case-link evidence is immutable; use the explicit unlink action.")
        )

    def _contact_center_contract_maps(self, crm_stages=None):
        """Load the active CRM mapping graph once for this link recordset."""

        links = self.filtered(lambda link: link.state == "active" and link.lead_id)
        PipelineBinding = self.env["contact.center.crm.pipeline.binding"]
        pipeline_bindings = (
            PipelineBinding.search(
                [
                    ("pipeline_id", "in", links.mapped("case_id.pipeline_id").ids),
                    ("active", "=", True),
                ]
            )
            if links
            else PipelineBinding
        )
        pipeline_by_source = {
            binding.pipeline_id.id: binding for binding in pipeline_bindings
        }

        TeamBinding = self.env["contact.center.crm.team.binding"]
        team_bindings = (
            TeamBinding.search(
                [
                    (
                        "contact_center_team_id",
                        "in",
                        links.mapped("case_id.team_id").ids,
                    ),
                    ("active", "=", True),
                ]
            )
            if links.mapped("case_id.team_id")
            else TeamBinding
        )
        team_by_source = {
            binding.contact_center_team_id.id: binding for binding in team_bindings
        }

        crm_stages = (
            crm_stages if crm_stages is not None else links.mapped("lead_id.stage_id")
        )
        StageBinding = self.env["contact.center.crm.stage.binding"]
        stage_bindings = (
            StageBinding.search(
                [
                    ("pipeline_binding_id", "in", pipeline_bindings.ids),
                    ("crm_stage_id", "in", crm_stages.ids),
                    ("active", "=", True),
                ]
            )
            if pipeline_bindings and crm_stages
            else StageBinding
        )
        stage_by_pair = {
            (binding.pipeline_binding_id.id, binding.crm_stage_id.id): binding
            for binding in stage_bindings
        }
        return {
            "pipeline_by_source": pipeline_by_source,
            "team_by_source": team_by_source,
            "stage_by_pair": stage_by_pair,
        }

    def _validate_link_contract(self, *, check_access=False, contract_maps=None):
        links = self.filtered(lambda link: link.state == "active" and link.lead_id)
        contract_maps = contract_maps or links._contact_center_contract_maps()
        for link in links:
            case = link.case_id
            lead = link.lead_id
            if not case.active:
                raise ValidationError(_("An archived case cannot be linked to CRM."))
            if check_access:
                case.check_access_rights("read")
                case.check_access_rule("read")
                lead.check_access_rights("read")
                lead.check_access_rule("read")
            if lead.company_id != case.company_id:
                raise ValidationError(
                    _("The CRM lead and Contact Center case must use the same company.")
                )
            pipeline_binding = contract_maps["pipeline_by_source"].get(
                case.pipeline_id.id
            )
            if not pipeline_binding:
                raise ValidationError(
                    _("Bind this Contact Center pipeline to CRM before linking a lead.")
                )
            if lead.team_id != pipeline_binding.crm_team_id:
                raise ValidationError(_("The CRM lead belongs to another sales team."))
            if case.team_id:
                team_binding = contract_maps["team_by_source"].get(case.team_id.id)
                if team_binding and team_binding.crm_team_id != lead.team_id:
                    raise ValidationError(
                        _("The case team is bound to another CRM sales team.")
                    )
            stage_binding = contract_maps["stage_by_pair"].get(
                (pipeline_binding.id, lead.stage_id.id)
            )
            if not stage_binding:
                raise ValidationError(
                    _("The CRM lead stage is not available in the bound pipeline.")
                )
        return True

    def _stage_binding_for_crm_stage(self, crm_stage, contract_maps=None):
        self.ensure_one()
        contract_maps = contract_maps or self._contact_center_contract_maps(
            crm_stages=crm_stage
        )
        pipeline_binding = contract_maps["pipeline_by_source"].get(
            self.case_id.pipeline_id.id
        )
        stage_binding = contract_maps["stage_by_pair"].get(
            (pipeline_binding.id if pipeline_binding else False, crm_stage.id)
        )
        if not stage_binding:
            raise ValidationError(
                _("The CRM stage has no active mapping in this case pipeline.")
            )
        return stage_binding

    def action_open_lead(self):
        self.ensure_one()
        if self.state != "active" or not self.lead_id:
            raise ValidationError(_("This CRM case link is no longer active."))
        self.lead_id.check_access_rights("read")
        self.lead_id.check_access_rule("read")
        return {
            "type": "ir.actions.act_window",
            "name": self.lead_id.display_name,
            "res_model": "crm.lead",
            "res_id": self.lead_id.id,
            "view_mode": "form",
            "target": "current",
        }


class ContactCenterCase(models.Model):
    _inherit = "contact.center.case"

    crm_link_ids = fields.One2many(
        "contact.center.crm.case.link",
        "case_id",
        string="CRM Link",
        groups="sales_team.group_sale_salesman",
    )
    crm_lead_id = fields.Many2one(
        "crm.lead",
        compute="_compute_crm_projection",
        string="CRM Lead",
        groups="sales_team.group_sale_salesman",
    )
    crm_lead_count = fields.Integer(
        compute="_compute_crm_projection",
        groups="sales_team.group_sale_salesman",
    )
    crm_link_exists = fields.Boolean(
        compute="_compute_crm_projection",
        groups="sales_team.group_sale_salesman",
    )

    @api.depends(
        "crm_link_ids",
        "crm_link_ids.lead_id",
        "crm_link_ids.lead_user_id",
    )
    def _compute_crm_projection(self):
        links = (
            self.env["contact.center.crm.case.link"]
            .sudo()
            .search(
                [
                    ("case_id", "in", self.ids),
                    ("state", "=", "active"),
                    ("lead_id", "!=", False),
                ],
                order="case_id, id",
            )
        )
        link_by_case = {}
        for link in links:
            link_by_case.setdefault(link.case_id.id, link)
        accessible_leads = (
            self.env["crm.lead"]
            .with_context(active_test=False)
            .search([("id", "in", links.mapped("lead_id").ids)])
        )
        accessible_by_id = {lead.id: lead for lead in accessible_leads}
        for case in self:
            link = link_by_case.get(case.id)
            accessible_lead = accessible_by_id.get(link.lead_id.id) if link else False
            case.crm_link_exists = bool(link)
            case.crm_lead_id = accessible_lead
            case.crm_lead_count = 1 if accessible_lead else 0

    def _contact_center_archive_blockers(self):
        blockers = super()._contact_center_archive_blockers()
        if (
            self.env["contact.center.crm.case.link"]
            .sudo()
            .search_count(
                [
                    ("case_id", "=", self.id),
                    ("state", "=", "active"),
                    ("lead_id", "!=", False),
                ]
            )
        ):
            blockers.append(_("unlink the active CRM lead"))
        return blockers

    def write(self, values):
        """Keep linked CRM contracts valid after an internal scope change."""

        validate_links = bool({"team_id", "pipeline_id"} & set(values))
        cases = self
        if validate_links and self and not stage_sync_graph_is_locked(self.env):
            # The canonical CRM graph helpers inspect hidden sibling records
            # under sudo while fencing the aggregate.  Preserve the caller's
            # native authorization before crossing that trust boundary.
            self.check_access_rights("write")
            self.check_access_rule("write")
            links = (
                self.env["contact.center.crm.case.link"]
                .sudo()
                .search([("case_id", "in", self.ids)])
            )
            target_pipeline_ids = self.mapped("pipeline_id").ids
            target_team_ids = self.mapped("team_id").ids
            if values.get("pipeline_id"):
                target_pipeline_ids.append(values["pipeline_id"])
            if values.get("team_id"):
                target_team_ids.append(values["team_id"])
            lock_binding_graph(
                self.env,
                pipeline_ids=target_pipeline_ids,
                team_ids=target_team_ids,
                touch=True,
            )
            cases = self._contact_center_lock_crm_graph(
                lead_ids=links.mapped("lead_id").ids,
                touch_cases=True,
                bindings_already_locked=True,
            )
        result = super(ContactCenterCase, cases).write(values)
        if validate_links:
            links = (
                self.env["contact.center.crm.case.link"]
                .sudo()
                .search([("case_id", "in", self.ids)])
            )
            links._validate_link_contract()
        return result

    def _crm_pipeline_binding(self):
        self.ensure_one()
        return self.env["contact.center.crm.pipeline.binding"].search(
            [("pipeline_id", "=", self.pipeline_id.id), ("active", "=", True)],
            limit=1,
        )

    def _crm_team_binding(self):
        self.ensure_one()
        if not self.team_id:
            return self.env["contact.center.crm.team.binding"]
        return self.env["contact.center.crm.team.binding"].search(
            [
                ("contact_center_team_id", "=", self.team_id.id),
                ("active", "=", True),
            ],
            limit=1,
        )

    def _crm_stage_binding(self, stage=None):
        self.ensure_one()
        stage = stage or self.stage_id
        pipeline_binding = self._crm_pipeline_binding()
        return self.env["contact.center.crm.stage.binding"].search(
            [
                ("pipeline_binding_id", "=", pipeline_binding.id),
                ("stage_id", "=", stage.id),
                ("active", "=", True),
            ],
            limit=1,
        )

    def _crm_link_sudo(self):
        self.ensure_one()
        return (
            self.env["contact.center.crm.case.link"]
            .sudo()
            .search(
                [
                    ("case_id", "=", self.id),
                    ("state", "=", "active"),
                    ("lead_id", "!=", False),
                ],
                order="id",
                limit=1,
            )
        )

    def _contact_center_lock_crm_graph(
        self,
        *,
        lead_ids=(),
        touch_leads=False,
        touch_cases=False,
        bindings_already_locked=False,
    ):
        """Lock bindings, core topology, leads, then cases in one order."""

        cases = self.sudo().with_context(active_test=False).exists()
        if not bindings_already_locked:
            lock_binding_graph(
                self.env,
                pipeline_ids=cases.mapped("pipeline_id").ids,
                team_ids=cases.mapped("team_id").ids,
                touch=True,
            )
        cases._contact_center_lock_case_topology(include_cases=False)
        lock_stage_sync_graph(
            self.env,
            lead_ids=lead_ids,
            case_ids=cases.ids,
            touch_leads=touch_leads,
            touch_cases=touch_cases,
        )
        # Keep the original caller environment.  Locks and hidden graph
        # discovery run under sudo above, but authorization after the wait must
        # never silently become superuser authorization.
        return (
            self.browse(cases.ids)
            ._contact_center_mark_case_topology_fenced()
            .with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN,
                contact_center_crm_stage_sync_graph_lock=CRM_STAGE_SYNC_GRAPH_LOCK_TOKEN,
            )
        )

    def _with_locked_crm_link_graph(self, lead_ids=(), *, touch_leads=False):
        """Lock binding authorities before the lead/case graph."""

        self.ensure_one()
        return self._contact_center_lock_crm_graph(
            lead_ids=lead_ids,
            touch_leads=touch_leads,
            touch_cases=True,
        )

    def _crm_existing_partner(self):
        """Project an already-linked direct identity; never create a contact."""

        self.ensure_one()
        binding = self.channel_id.sudo().contact_center_binding_ids.filtered(
            lambda item: item.active
            and not item.merged_into_id
            and item.conversation_type == "direct"
            and item.identity_id
        )[:1]
        return binding.identity_id.partner_id if binding else self.env["res.partner"]

    def _check_crm_action_access(self, lead=None, operation="read"):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        if not self.env.user.has_group("sales_team.group_sale_salesman"):
            raise AccessError(_("CRM access is required for this operation."))
        if lead:
            lead.check_access_rights(operation)
            lead.check_access_rule(operation)
        return True

    def _crm_link_prerequisites(self):
        self.ensure_one()
        if not self.active:
            raise ValidationError(_("An archived case cannot be linked to CRM."))
        pipeline_binding = self._crm_pipeline_binding()
        if not pipeline_binding:
            raise ValidationError(_("This case pipeline is not bound to CRM."))
        team_binding = self._crm_team_binding()
        if team_binding and team_binding.crm_team_id != pipeline_binding.crm_team_id:
            raise ValidationError(
                _("This case team is not bound to the pipeline CRM team.")
            )
        stage_binding = self._crm_stage_binding()
        if not stage_binding:
            raise ValidationError(
                _("This case stage is not mapped to an effective CRM stage.")
            )
        return pipeline_binding, team_binding, stage_binding

    def action_create_crm_lead(self, lead_type="lead"):
        self.ensure_one()
        self._check_crm_action_access()
        case = self._with_locked_crm_link_graph()
        case.invalidate_recordset(
            ["active", "channel_id", "company_id", "pipeline_id", "stage_id", "team_id"]
        )
        case._check_crm_action_access()
        if case._crm_link_sudo():
            raise ValidationError(_("This case already has a CRM lead."))
        if lead_type not in ("lead", "opportunity"):
            raise ValidationError(_("Unsupported CRM lead type."))
        pipeline_binding, _team_binding, stage_binding = case._crm_link_prerequisites()
        partner = case._crm_existing_partner()
        lead = case.env["crm.lead"].create(
            {
                "name": case.name,
                "type": lead_type,
                "user_id": False,
                "team_id": pipeline_binding.crm_team_id.id,
                "company_id": case.company_id.id,
                "stage_id": stage_binding.crm_stage_id.id,
                "partner_id": partner.id or False,
            }
        )
        link = (
            case.env["contact.center.crm.case.link"]
            .with_context(contact_center_crm_link_service=CRM_CASE_LINK_SERVICE_TOKEN)
            .create({"case_id": case.id, "lead_id": lead.id, "origin": "created"})
        )
        return link.action_open_lead()

    def action_create_crm_opportunity(self):
        return self.action_create_crm_lead(lead_type="opportunity")

    def action_link_crm_lead(self, lead_id):
        self.ensure_one()
        if type(lead_id) is not int or lead_id <= 0:  # noqa: E721
            raise ValidationError(_("The CRM lead ID must be a positive integer."))
        lead = self.env["crm.lead"].browse(lead_id).exists()
        if not lead:
            raise ValidationError(_("The selected CRM lead does not exist."))
        self._check_crm_action_access(lead=lead, operation="write")
        case = self._with_locked_crm_link_graph(lead.ids, touch_leads=True)
        lead.invalidate_recordset(["company_id", "stage_id", "team_id"])
        lead = case.env["crm.lead"].browse(lead.id).exists()
        if not lead:
            raise ValidationError(_("The selected CRM lead no longer exists."))
        case._check_crm_action_access(lead=lead, operation="write")
        if case._crm_link_sudo():
            raise ValidationError(_("This case already has a CRM lead."))
        pipeline_binding, team_binding, _stage_binding = case._crm_link_prerequisites()
        if lead.company_id != case.company_id:
            raise ValidationError(
                _("The CRM lead and case must belong to the same company.")
            )
        if lead.team_id != pipeline_binding.crm_team_id:
            raise ValidationError(_("The CRM lead belongs to another sales team."))
        if team_binding and team_binding.crm_team_id != lead.team_id:
            raise ValidationError(_("The case team is bound to another CRM team."))
        crm_stage_binding = case.env["contact.center.crm.stage.binding"].search(
            [
                ("pipeline_binding_id", "=", pipeline_binding.id),
                ("crm_stage_id", "=", lead.stage_id.id),
                ("active", "=", True),
            ],
            limit=1,
        )
        if not crm_stage_binding:
            raise ValidationError(
                _("The selected lead stage is not mapped to this pipeline.")
            )
        link = (
            case.env["contact.center.crm.case.link"]
            .with_context(contact_center_crm_link_service=CRM_CASE_LINK_SERVICE_TOKEN)
            .create({"case_id": case.id, "lead_id": lead.id, "origin": "linked"})
        )
        if case.stage_id != crm_stage_binding.stage_id:
            case.sudo().with_context(
                contact_center_crm_stage_sync=CRM_CASE_STAGE_SYNC_TOKEN,
                contact_center_case_transition_token=(
                    CONTACT_CENTER_CASE_TRANSITION_TOKEN
                ),
            ).action_transition(
                crm_stage_binding.stage_id.id,
                source="integration",
            )
        return link.action_open_lead()

    def action_open_link_crm_lead_wizard(self):
        self.ensure_one()
        self._check_crm_action_access()
        if self._crm_link_sudo():
            raise ValidationError(_("This case already has a CRM lead."))
        pipeline_binding, _team_binding, _stage_binding = self._crm_link_prerequisites()
        return {
            "type": "ir.actions.act_window",
            "name": _("Link CRM Lead"),
            "res_model": "contact.center.crm.link.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_case_id": self.id,
                "default_crm_team_id": pipeline_binding.crm_team_id.id,
                "default_company_id": self.company_id.id,
            },
        }

    def action_open_crm_lead(self):
        self.ensure_one()
        link = self._crm_link_sudo()
        if not link:
            raise ValidationError(_("This case has no CRM lead."))
        visible_link = self.env["contact.center.crm.case.link"].browse(link.id)
        visible_link.check_access_rights("read")
        visible_link.check_access_rule("read")
        return visible_link.action_open_lead()

    def action_unlink_crm_lead(self):
        self.ensure_one()
        link = self._crm_link_sudo()
        if not link:
            raise ValidationError(_("This case has no CRM lead."))
        visible_link = self.env["contact.center.crm.case.link"].browse(link.id)
        visible_link.check_access_rights("read")
        visible_link.check_access_rule("read")
        visible_link.action_unlink()
        return True

    def action_transition(
        self,
        target_stage_id,
        expected_revision=None,
        request_uuid=None,
        source="manual",
    ):
        self.ensure_one()
        self._contact_center_check_operational_access()
        case = self
        if not stage_sync_graph_is_locked(self.env):
            links = self.sudo().crm_link_ids.filtered(
                lambda item: item.state == "active" and item.lead_id
            )
            case = self._contact_center_lock_crm_graph(
                lead_ids=links.mapped("lead_id").ids,
                touch_cases=True,
            )
        else:
            case = self._contact_center_mark_case_topology_fenced()
        return super(ContactCenterCase, case).action_transition(
            target_stage_id,
            expected_revision=expected_revision,
            request_uuid=request_uuid,
            source=source,
        )

    def _contact_center_after_transition(self, transition):
        self.ensure_one()
        result = super()._contact_center_after_transition(transition)
        if (
            self.env.context.get("contact_center_crm_stage_sync")
            is CRM_CASE_STAGE_SYNC_TOKEN
        ):
            return result
        link = self.sudo().crm_link_ids.filtered(
            lambda item: item.state == "active" and item.lead_id
        )[:1]
        if not link:
            return result
        stage_binding = self._crm_stage_binding(self.stage_id)
        if not stage_binding:
            raise ValidationError(_("The target stage is not managed by CRM."))
        lead = self.env["crm.lead"].browse(link.lead_id.id)
        self._check_crm_action_access(lead=lead, operation="write")
        link._validate_link_contract()
        lead.with_context(
            contact_center_crm_stage_sync=CRM_CASE_STAGE_SYNC_TOKEN,
            contact_center_crm_origin_case_id=self.id,
        ).write({"stage_id": stage_binding.crm_stage_id.id})
        return result


class ContactCenterCrmLinkWizard(models.TransientModel):
    _name = "contact.center.crm.link.wizard"
    _description = "Link a Contact Center Case to a CRM Lead"

    case_id = fields.Many2one(
        "contact.center.case",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        related="case_id.company_id",
        readonly=True,
    )
    crm_team_id = fields.Many2one(
        "crm.team",
        compute="_compute_crm_team",
        readonly=True,
    )
    lead_id = fields.Many2one(
        "crm.lead",
        required=True,
        ondelete="cascade",
        domain="[('company_id', '=', company_id), ('team_id', '=', crm_team_id)]",
    )

    @api.depends("case_id", "case_id.pipeline_id")
    def _compute_crm_team(self):
        for wizard in self:
            binding = wizard.case_id._crm_pipeline_binding()
            wizard.crm_team_id = binding.crm_team_id

    def action_link(self):
        self.ensure_one()
        return self.case_id.action_link_crm_lead(self.lead_id.id)
