import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_base.services.tokens import (
    CONTACT_CENTER_DELETION_TOKEN,
)

from ..services.tokens import CONTACT_CENTER_CASE_TRANSITION_TOKEN

_CASE_SERVICE_TOKEN = object()
_CASE_TOPOLOGY_FENCE_TOKEN = object()
_PIPELINE_ARCHIVE_SERVICE_TOKEN = object()
_PIPELINE_STAGE_SERVICE_TOKEN = object()
_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_DEFAULT_PIPELINE_CODE = "default-service"
_DEFAULT_STAGES = (
    {
        "code": "new",
        "name": "New",
        "sequence": 10,
        "is_initial": True,
        "fold": False,
        "is_closed": False,
    },
    {
        "code": "in-progress",
        "name": "In Progress",
        "sequence": 20,
        "is_initial": False,
        "fold": False,
        "is_closed": False,
    },
    {
        "code": "waiting",
        "name": "Waiting",
        "sequence": 30,
        "is_initial": False,
        "fold": False,
        "is_closed": False,
    },
    {
        "code": "done",
        "name": "Done",
        "sequence": 40,
        "is_initial": False,
        "fold": True,
        "is_closed": True,
    },
)

_CASE_SERVER_OWNED_CREATE_FIELDS = {
    "active",
    "case_ref",
    "closed_at",
    "company_id",
    "is_default",
    "opened_at",
    "stage_changed_at",
    "stage_revision",
    "team_id",
}


def _normalized_code(value):
    return str(value or "").strip().lower()


def _normalized_uuid(value, label):
    if value in (None, False, ""):
        return str(uuid.uuid4())
    if not isinstance(value, str):
        raise ValidationError(_("Invalid %s.", label))
    try:
        return str(uuid.UUID(value.strip()))
    except (AttributeError, ValueError) as error:
        raise ValidationError(_("Invalid %s.", label)) from error


def _positive_ids(values):
    return sorted({int(value) for value in values or [] if value})


def _relational_command_ids(commands):
    """Return existing IDs named by x2many commands without applying them."""

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


class ContactCenterPipeline(models.Model):
    _name = "contact.center.pipeline"
    _description = "Contact Center Pipeline"
    _order = "name, id"
    _check_company_auto = True

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True, copy=False)
    active = fields.Boolean(default=True)
    topology_revision = fields.Integer(
        required=True,
        default=0,
        readonly=True,
        copy=False,
        help="Internal concurrency fence for the pipeline and case topology.",
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company,
        ondelete="restrict",
    )
    stage_ids = fields.One2many(
        "contact.center.pipeline.stage",
        "pipeline_id",
        string="Stages",
    )
    team_ids = fields.Many2many(
        "contact.center.team",
        "contact_center_team_pipeline_rel",
        "pipeline_id",
        "team_id",
        string="Service Teams",
        readonly=True,
    )
    account_ids = fields.One2many(
        "contact.center.account",
        "default_pipeline_id",
        string="Inboxes",
        readonly=True,
    )
    case_ids = fields.One2many(
        "contact.center.case",
        "pipeline_id",
        string="Atendimentos",
        readonly=True,
    )

    _sql_constraints = [
        (
            "code_company_unique",
            "unique(company_id, code)",
            "Pipeline codes must be unique per company.",
        ),
        (
            "default_pipeline_active",
            "check(code != 'default-service' OR active IS TRUE)",
            "The default service pipeline must remain active.",
        ),
        (
            "topology_revision_nonnegative",
            "check(topology_revision >= 0)",
            "The pipeline topology revision cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if any("topology_revision" in values for values in vals_list):
            raise AccessError(_("The pipeline topology revision is internal."))
        normalized = []
        for values in vals_list:
            values = dict(values)
            values["code"] = _normalized_code(values.get("code"))
            if (
                values["code"] == _DEFAULT_PIPELINE_CODE
                and values.get("active", True) is False
            ):
                raise ValidationError(
                    _("The default service pipeline must remain active.")
                )
            normalized.append(values)
        return super().create(normalized)

    def write(self, values):
        values = dict(values)
        if "topology_revision" in values:
            raise AccessError(_("The pipeline topology revision is internal."))
        archive_change = "active" in values and any(
            bool(values["active"]) != pipeline.active for pipeline in self
        )
        if archive_change and (
            self.env.context.get("contact_center_pipeline_archive_service_token")
            is not _PIPELINE_ARCHIVE_SERVICE_TOKEN
        ):
            raise AccessError(
                _("Archive or restore a pipeline with its explicit service action.")
            )
        if "code" in values:
            values["code"] = _normalized_code(values["code"])
        immutable = {"company_id", "code"} & set(values)
        for pipeline in self:
            for field_name in immutable:
                incoming = values[field_name]
                current = pipeline[field_name]
                if pipeline._fields[field_name].relational:
                    current = current.id
                if incoming != current:
                    raise ValidationError(
                        _(
                            "The stable pipeline field '%s' cannot be changed.",
                            field_name,
                        )
                    )
        if archive_change:
            self.env["contact.center.account"]._contact_center_lock_access_topology(
                pipeline_ids=self.ids
            )
            self.invalidate_recordset(["active", "team_ids", "account_ids", "case_ids"])
        if "active" in values and not bool(values["active"]):
            if any(pipeline.code == _DEFAULT_PIPELINE_CODE for pipeline in self):
                raise ValidationError(
                    _("The default service pipeline cannot be archived.")
                )
            referenced = self.filtered(
                lambda pipeline: pipeline.team_ids
                or pipeline.account_ids
                or pipeline.case_ids.filtered(lambda case: case.active)
            )
            if referenced:
                raise ValidationError(
                    _(
                        "A pipeline assigned to an inbox, team or active case "
                        "cannot be archived."
                    )
                )
        return super().write(values)

    def action_archive(self):
        self.check_access_rights("write")
        self.check_access_rule("write")
        active = self.filtered("active")
        if active:
            active.with_context(
                contact_center_pipeline_archive_service_token=(
                    _PIPELINE_ARCHIVE_SERVICE_TOKEN
                )
            ).write({"active": False})
        return True

    def action_unarchive(self):
        self.check_access_rights("write")
        self.check_access_rule("write")
        inactive = self.filtered(lambda pipeline: not pipeline.active)
        if inactive:
            inactive.with_context(
                contact_center_pipeline_archive_service_token=(
                    _PIPELINE_ARCHIVE_SERVICE_TOKEN
                )
            ).write({"active": True})
        return True

    @api.constrains("code")
    def _check_code(self):
        for pipeline in self:
            if not _CODE_PATTERN.fullmatch(pipeline.code or ""):
                raise ValidationError(
                    _(
                        "Pipeline codes must start with a lowercase letter and use "
                        "only lowercase letters, digits, dots, dashes or underscores."
                    )
                )

    def _contact_center_initial_stage(self):
        self.ensure_one()
        stage = self.stage_ids.filtered(lambda item: item.active and item.is_initial)[
            :1
        ]
        if not stage:
            raise ValidationError(
                _("Pipeline %s has no active initial stage.", self.display_name)
            )
        return stage

    def _contact_center_set_initial_stage(self, stage):
        """Atomically replace the pipeline initial stage inside the service layer."""

        self.ensure_one()
        stage.ensure_one()
        self.env["contact.center.account"]._contact_center_lock_access_topology(
            pipeline_ids=self.ids
        )
        stages = (
            self.env["contact.center.pipeline.stage"]
            .sudo()
            .with_context(active_test=False)
            .search([("pipeline_id", "=", self.id)], order="id")
        )
        stages.invalidate_recordset(["active", "is_closed", "is_initial"])
        target = stages.filtered(lambda item: item.id == stage.id)
        if not target:
            raise ValidationError(
                _("The initial stage must belong to the pipeline being updated.")
            )
        if not target.active or target.is_closed:
            raise ValidationError(
                _("The initial stage must be active and cannot be closed.")
            )
        current = stages.filtered("is_initial")
        if current == target:
            return target
        service_context = {
            "contact_center_pipeline_stage_service_token": (
                _PIPELINE_STAGE_SERVICE_TOKEN
            )
        }
        current.with_context(**service_context).write({"is_initial": False})
        # Odoo defers stored-field updates. Flush the removal before setting the
        # replacement so PostgreSQL's partial unique index never observes two
        # initial stages, even inside this otherwise atomic transaction.
        current.flush_recordset(["is_initial"])
        target.with_context(**service_context).write({"is_initial": True})
        target.flush_recordset(["is_initial"])
        stages.invalidate_recordset(["is_initial"])
        self.invalidate_recordset(["stage_ids"])
        return self._contact_center_initial_stage()

    @api.model
    def _contact_center_target_companies(self):
        companies = (
            self.env["contact.center.team"].sudo().search([]).mapped("company_id")
        )
        companies |= (
            self.env["contact.center.account"].sudo().search([]).mapped("company_id")
        )
        companies |= (
            self.env["mail.channel"]
            .sudo()
            .with_context(active_test=False)
            .search([("channel_type", "=", "contact_center")])
            .mapped("contact_center_company_id")
        )
        return companies

    @api.model
    def _contact_center_provision_default_pipeline(self, company):
        """Return one complete default pipeline under a per-company row lock."""

        company.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM res_company WHERE id = %s FOR UPDATE", [company.id]
        )
        pipeline = (
            self.sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("company_id", "=", company.id),
                    ("code", "=", _DEFAULT_PIPELINE_CODE),
                ],
                limit=1,
            )
        )
        if not pipeline:
            pipeline = self.sudo().create(
                {
                    "name": _("Service"),
                    "code": _DEFAULT_PIPELINE_CODE,
                    "company_id": company.id,
                }
            )
        elif not pipeline.active:
            pipeline.action_unarchive()

        stage_model = self.env["contact.center.pipeline.stage"].sudo()
        stages_by_code = {
            stage.code: stage
            for stage in stage_model.with_context(active_test=False).search(
                [("pipeline_id", "=", pipeline.id)]
            )
        }
        for stage_values in _DEFAULT_STAGES:
            if stage_values["code"] in stages_by_code:
                continue
            values = dict(stage_values, pipeline_id=pipeline.id)
            stages_by_code[stage_values["code"]] = stage_model.create(values)

        pipeline.invalidate_recordset(["stage_ids", "active"])
        initial = pipeline.stage_ids.filtered("is_initial")
        if not initial:
            if not stages_by_code["new"].active:
                stages_by_code["new"].write({"active": True})
            pipeline._contact_center_set_initial_stage(stages_by_code["new"])
        pipeline.invalidate_recordset(["stage_ids", "active"])
        pipeline._contact_center_initial_stage()
        return pipeline

    @api.model
    def _contact_center_provision_defaults(
        self,
        companies=None,
        *,
        assign_accounts=True,
        assign_teams=True,
        backfill_channels=True,
    ):
        """Provision defaults and optionally backfill teams and conversations."""

        if companies is None:
            companies = self._contact_center_target_companies()
        companies = companies.sudo().exists()
        provisioned = self.browse()
        account_model = self.env["contact.center.account"].sudo()
        team_model = self.env["contact.center.team"].sudo()
        for company in companies.sorted("id"):
            pipeline = self._contact_center_provision_default_pipeline(company)
            provisioned |= pipeline
            if assign_teams:
                teams = team_model.with_context(active_test=False).search(
                    [("company_id", "=", company.id)]
                )
                for team in teams:
                    values = {}
                    if pipeline not in team.pipeline_ids:
                        values["pipeline_ids"] = [(4, pipeline.id)]
                    if not team.default_pipeline_id:
                        values["default_pipeline_id"] = pipeline.id
                    if values:
                        team.write(values)
            if assign_accounts:
                accounts = account_model.with_context(active_test=False).search(
                    [
                        ("company_id", "=", company.id),
                        ("default_pipeline_id", "=", False),
                    ]
                )
                for account in accounts:
                    account_pipeline = account._contact_center_unique_team_pipeline()
                    if (
                        not account_pipeline
                        or not account_pipeline.active
                        or account_pipeline.company_id != company
                    ):
                        account_pipeline = pipeline
                    account.write({"default_pipeline_id": account_pipeline.id})
        if backfill_channels:
            self.env["mail.channel"]._contact_center_backfill_default_cases(
                companies=companies
            )
        return provisioned

    def unlink(self):
        if any(pipeline.code == _DEFAULT_PIPELINE_CODE for pipeline in self):
            raise ValidationError(_("The default service pipeline cannot be deleted."))
        self.env["contact.center.account"]._contact_center_lock_access_topology(
            pipeline_ids=self.ids
        )
        self.invalidate_recordset(["team_ids", "account_ids", "stage_ids", "case_ids"])
        if any(
            pipeline.team_ids
            or pipeline.account_ids
            or pipeline.stage_ids
            or pipeline.case_ids
            for pipeline in self
        ):
            raise ValidationError(
                _("A pipeline with inboxes, teams, stages or cases cannot be deleted.")
            )
        return super().unlink()


class ContactCenterPipelineStage(models.Model):
    _name = "contact.center.pipeline.stage"
    _description = "Contact Center Pipeline Stage"
    _order = "sequence, name, id"
    _check_company_auto = True

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True, copy=False)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10, required=True)
    pipeline_id = fields.Many2one(
        "contact.center.pipeline",
        required=True,
        index=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one(
        related="pipeline_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    is_initial = fields.Boolean(default=False, index=True, readonly=True)
    fold = fields.Boolean(default=False)
    is_closed = fields.Boolean(default=False, index=True)
    case_ids = fields.One2many(
        "contact.center.case", "stage_id", string="Atendimentos", readonly=True
    )

    _sql_constraints = [
        (
            "code_pipeline_unique",
            "unique(pipeline_id, code)",
            "Stage codes must be unique per pipeline.",
        ),
    ]

    def init(self):
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                contact_center_pipeline_stage_initial_unique
            ON contact_center_pipeline_stage (pipeline_id)
            WHERE is_initial IS TRUE
            """
        )

    @api.model_create_multi
    def create(self, vals_list):
        pipeline_ids = {
            int(values["pipeline_id"])
            for values in vals_list
            if values.get("pipeline_id")
        }
        self.env["contact.center.account"]._contact_center_lock_access_topology(
            pipeline_ids=pipeline_ids
        )
        normalized = []
        for values in vals_list:
            values = dict(values)
            values["code"] = _normalized_code(values.get("code"))
            normalized.append(values)
        return super().create(normalized)

    def write(self, values):
        values = dict(values)
        service_write = (
            self.env.context.get("contact_center_pipeline_stage_service_token")
            is _PIPELINE_STAGE_SERVICE_TOKEN
        )
        if "is_initial" in values and not service_write:
            changed = self.filtered(
                lambda stage: bool(values["is_initial"]) != stage.is_initial
            )
            if changed:
                raise AccessError(
                    _("Use Set as Initial to replace the pipeline initial stage.")
                )
        topology_fields = {"active", "is_closed", "is_initial", "pipeline_id"}
        if topology_fields & set(values) and not service_write:
            self.env["contact.center.account"]._contact_center_lock_access_topology(
                pipeline_ids=self.mapped("pipeline_id").ids
            )
            self.invalidate_recordset(
                ["active", "is_closed", "is_initial", "pipeline_id", "case_ids"]
            )
        if "code" in values:
            values["code"] = _normalized_code(values["code"])
        immutable = {"pipeline_id", "code"} & set(values)
        for stage in self:
            for field_name in immutable:
                incoming = values[field_name]
                current = stage[field_name]
                if stage._fields[field_name].relational:
                    current = current.id
                if incoming != current:
                    raise ValidationError(
                        _("The stable stage field '%s' cannot be changed.", field_name)
                    )
        if "is_closed" in values:
            changed = self.filtered(
                lambda stage: bool(values["is_closed"]) != stage.is_closed
                and bool(
                    self.env["contact.center.case"]
                    .sudo()
                    .with_context(active_test=False)
                    .search_count([("stage_id", "=", stage.id)])
                )
            )
            if changed:
                raise ValidationError(
                    _("A stage used by cases cannot change its closed semantics.")
                )
        if "active" in values and not bool(values["active"]):
            used = self.filtered(
                lambda stage: bool(
                    self.env["contact.center.case"]
                    .sudo()
                    .search_count([("stage_id", "=", stage.id)])
                )
            )
            if used:
                raise ValidationError(
                    _("A stage with active cases cannot be archived.")
                )
        result = super().write(values)
        if (
            self.env.context.get("contact_center_pipeline_stage_service_token")
            is not _PIPELINE_STAGE_SERVICE_TOKEN
        ):
            self._contact_center_validate_used_pipeline_readiness()
        return result

    def action_set_initial(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        self.pipeline_id.check_access_rights("write")
        self.pipeline_id.check_access_rule("write")
        self.pipeline_id._contact_center_set_initial_stage(self)
        return True

    @api.constrains("code", "active", "is_initial", "is_closed")
    def _check_stage_configuration(self):
        for stage in self:
            if not _CODE_PATTERN.fullmatch(stage.code or ""):
                raise ValidationError(
                    _(
                        "Stage codes must start with a lowercase letter and use only "
                        "lowercase letters, digits, dots, dashes or underscores."
                    )
                )
            if stage.is_initial and not stage.active:
                raise ValidationError(_("The initial stage must be active."))
            if stage.is_initial and stage.is_closed:
                raise ValidationError(_("The initial stage cannot be closed."))

    def _contact_center_validate_used_pipeline_readiness(self):
        pipelines = self.mapped("pipeline_id")
        used = pipelines.filtered(
            lambda pipeline: bool(
                self.env["contact.center.team"]
                .sudo()
                .search_count([("default_pipeline_id", "=", pipeline.id)])
                or self.env["contact.center.account"]
                .sudo()
                .search_count([("default_pipeline_id", "=", pipeline.id)])
                or self.env["contact.center.case"]
                .sudo()
                .with_context(active_test=False)
                .search_count([("pipeline_id", "=", pipeline.id)])
            )
        )
        for pipeline in used:
            pipeline._contact_center_initial_stage()
        return True

    def unlink(self):
        pipelines = self.mapped("pipeline_id")
        self.env["contact.center.account"]._contact_center_lock_access_topology(
            pipeline_ids=pipelines.ids
        )
        self.invalidate_recordset(["case_ids", "pipeline_id", "is_initial"])
        referenced = self.filtered(
            lambda stage: stage.case_ids
            or self.env["contact.center.case.transition"]
            .sudo()
            .search_count(
                [
                    "|",
                    ("from_stage_id", "=", stage.id),
                    ("to_stage_id", "=", stage.id),
                ]
            )
        )
        if referenced:
            raise ValidationError(_("A stage with case history cannot be deleted."))
        result = super().unlink()
        for pipeline in pipelines.exists():
            if pipeline.team_ids or pipeline.account_ids or pipeline.case_ids:
                pipeline._contact_center_initial_stage()
        return result


# Keep pipeline definitions separate from the CRM projection's orchestration.
class ContactCenterTeamPipeline(models.Model):
    # pylint: disable-next=consider-merging-classes-inherited
    _inherit = "contact.center.team"

    pipeline_ids = fields.Many2many(
        "contact.center.pipeline",
        "contact_center_team_pipeline_rel",
        "team_id",
        "pipeline_id",
        string="Pipelines",
        check_company=True,
    )
    default_pipeline_id = fields.Many2one(
        "contact.center.pipeline",
        string="Default Pipeline",
        check_company=True,
        ondelete="restrict",
        domain=(
            "[('id', 'in', pipeline_ids), ('company_id', '=', company_id), "
            "('active', '=', True)]"
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        pipeline_model = self.env["contact.center.pipeline"]
        for source in vals_list:
            values = dict(source)
            company = self.env["res.company"].browse(
                values.get("company_id") or self.env.company.id
            )
            if not values.get("default_pipeline_id"):
                pipeline = pipeline_model._contact_center_provision_default_pipeline(
                    company
                )
                values["default_pipeline_id"] = pipeline.id
                if "pipeline_ids" not in values:
                    values["pipeline_ids"] = [(6, 0, pipeline.ids)]
                else:
                    values["pipeline_ids"] = list(values["pipeline_ids"] or []) + [
                        (4, pipeline.id)
                    ]
            elif "pipeline_ids" not in values:
                values["pipeline_ids"] = [(6, 0, [values["default_pipeline_id"]])]
            prepared.append(values)
        pipeline_ids = set()
        user_ids = set()
        for values in prepared:
            if values.get("default_pipeline_id"):
                pipeline_ids.add(int(values["default_pipeline_id"]))
            pipeline_ids.update(_relational_command_ids(values.get("pipeline_ids")))
            user_ids.update(_relational_command_ids(values.get("agent_ids")))
            user_ids.update(_relational_command_ids(values.get("supervisor_ids")))
        self.env["contact.center.account"]._contact_center_lock_access_topology(
            user_ids=user_ids,
            pipeline_ids=pipeline_ids,
        )
        return super().create(prepared)

    def write(self, values):
        values = dict(values)
        if values.get("default_pipeline_id") and "pipeline_ids" not in values:
            values["pipeline_ids"] = [(4, values["default_pipeline_id"])]
        pipeline_change = bool({"pipeline_ids", "default_pipeline_id"} & set(values))
        if pipeline_change:
            accounts = (
                self.env["contact.center.account"]
                .sudo()
                .with_context(active_test=False)
                .search([("access_team_ids", "in", self.ids)])
            )
            pipeline_ids = set(self.mapped("pipeline_ids").ids)
            pipeline_ids.update(self.mapped("default_pipeline_id").ids)
            if values.get("default_pipeline_id"):
                pipeline_ids.add(int(values["default_pipeline_id"]))
            pipeline_ids.update(_relational_command_ids(values.get("pipeline_ids")))
            self.env["contact.center.account"]._contact_center_lock_access_topology(
                account_ids=accounts.ids,
                team_ids=self.ids,
                user_ids=(self.mapped("agent_ids") | self.mapped("supervisor_ids")).ids,
                pipeline_ids=pipeline_ids,
            )
            self.invalidate_recordset(["pipeline_ids", "default_pipeline_id"])
        return super().write(values)

    @api.constrains("company_id", "pipeline_ids", "default_pipeline_id")
    def _check_pipeline_configuration(self):
        account_model = (
            self.env["contact.center.account"].sudo().with_context(active_test=False)
        )
        case_model = (
            self.env["contact.center.case"].sudo().with_context(active_test=False)
        )
        for team in self:
            if any(
                pipeline.company_id != team.company_id for pipeline in team.pipeline_ids
            ):
                raise ValidationError(
                    _("Every team pipeline must belong to the team company.")
                )
            if team.default_pipeline_id:
                if team.default_pipeline_id not in team.pipeline_ids:
                    raise ValidationError(
                        _("The default pipeline must be enabled for the team.")
                    )
                if not team.default_pipeline_id.active:
                    raise ValidationError(_("The default pipeline must be active."))
                team.default_pipeline_id._contact_center_initial_stage()
            invalid_account = account_model.search(
                [
                    ("access_team_ids", "=", team.id),
                    ("default_pipeline_id", "not in", team.pipeline_ids.ids or [0]),
                ],
            ).filtered(
                lambda account: account.default_pipeline_id
                and account.default_pipeline_id
                not in account.access_team_ids.mapped("pipeline_ids")
            )[
                :1
            ]
            if invalid_account:
                raise ValidationError(
                    _(
                        "A pipeline used as the default by inbox %(inbox)s cannot "
                        "be removed from team %(team)s.",
                        inbox=invalid_account.display_name,
                        team=team.display_name,
                    )
                )
            invalid_case = case_model.search(
                [
                    ("team_id", "=", team.id),
                    ("pipeline_id", "not in", team.pipeline_ids.ids or [0]),
                ],
                limit=1,
            )
            if invalid_case:
                raise ValidationError(
                    _(
                        "A pipeline used by an existing case cannot be removed from its team."
                    )
                )


class ContactCenterAccountPipeline(models.Model):
    # pylint: disable-next=consider-merging-classes-inherited
    _inherit = "contact.center.account"

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
        """Extend the base lock graph with pipeline and case authorities.

        The order is inbox, team, user, pipeline, conversation and case. Provider
        connectors may append their own authority rows after this method returns.
        """

        pipeline_ids = _positive_ids(pipeline_ids)
        case_ids = _positive_ids(case_ids)
        if not pipeline_ids and not case_ids:
            return super()._contact_center_lock_access_topology(
                account_ids=account_ids,
                team_ids=team_ids,
                user_ids=user_ids,
                channel_ids=channel_ids,
            )

        accounts = self._contact_center_lock_access_authority_rows(
            account_ids=account_ids,
            team_ids=team_ids,
            user_ids=user_ids,
        )
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

        self._contact_center_lock_conversation_rows(channel_ids=channel_ids)
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

    default_pipeline_id = fields.Many2one(
        "contact.center.pipeline",
        string="Default Pipeline",
        check_company=True,
        ondelete="restrict",
        domain="[('company_id', '=', company_id), ('active', '=', True)]",
    )

    def _contact_center_unique_team_pipeline(self):
        """Infer a default only when the access teams agree on one pipeline."""

        self.ensure_one()
        pipelines = self.access_team_ids.mapped("default_pipeline_id")
        return pipelines if len(pipelines) == 1 else pipelines.browse()

    @api.model_create_multi
    def create(self, vals_list):
        pipeline_model = self.env["contact.center.pipeline"]
        prepared = []
        for source in self._contact_center_prepare_access_defaults(vals_list):
            values = dict(source)
            if not values.get("default_pipeline_id"):
                company = self.env["res.company"].browse(
                    values.get("company_id") or self.env.company.id
                )
                _users, teams = self._contact_center_access_scope_values(values)
                pipelines = teams.mapped("default_pipeline_id")
                pipeline = pipelines if len(pipelines) == 1 else pipeline_model.browse()
                if (
                    not pipeline
                    or not pipeline.active
                    or pipeline.company_id != company
                ):
                    pipeline = (
                        pipeline_model._contact_center_provision_default_pipeline(
                            company
                        )
                    )
                values["default_pipeline_id"] = pipeline.id
            prepared.append(values)
        team_ids = set()
        user_ids = set()
        for values in prepared:
            users, teams = self._contact_center_access_scope_values(values)
            team_ids.update(teams.ids)
            user_ids.update(users.ids)
        user_ids.update(
            {
                int(values[field_name])
                for values in prepared
                for field_name in ("auto_assignment_user_id",)
                if values.get(field_name)
            }
        )
        pipeline_ids = {
            int(values["default_pipeline_id"])
            for values in prepared
            if values.get("default_pipeline_id")
        }
        self._contact_center_lock_access_topology(
            team_ids=team_ids,
            user_ids=user_ids,
            pipeline_ids=pipeline_ids,
        )
        return super().create(prepared)

    def write(self, values):
        if "default_pipeline_id" in values and any(
            (values["default_pipeline_id"] or False)
            != (account.default_pipeline_id.id or False)
            for account in self
        ):
            pipeline_ids = set(self.mapped("default_pipeline_id").ids)
            if values.get("default_pipeline_id"):
                pipeline_ids.add(int(values["default_pipeline_id"]))
            self._contact_center_lock_access_topology(
                account_ids=self.ids,
                team_ids=self.mapped("access_team_ids").ids,
                user_ids=(
                    self.mapped("access_user_ids")
                    | self.mapped("auto_assignment_user_id")
                ).ids,
                pipeline_ids=pipeline_ids,
            )
            self.invalidate_recordset(["default_pipeline_id"])
        return super().write(values)

    @api.constrains("company_id", "access_team_ids", "default_pipeline_id")
    def _check_default_pipeline_configuration(self):
        for account in self:
            pipeline = account.default_pipeline_id
            if not pipeline:
                continue
            if pipeline.company_id != account.company_id:
                raise ValidationError(
                    _("The inbox default pipeline belongs to another company.")
                )
            if not pipeline.active:
                raise ValidationError(_("The inbox default pipeline must be active."))
            pipeline._contact_center_initial_stage()
            if (
                account.access_team_ids
                and pipeline not in account.access_team_ids.mapped("pipeline_ids")
            ):
                raise ValidationError(
                    _("The inbox default pipeline must be enabled for an access team.")
                )


# Keep the optional case projection isolated from the canonical channel model;
# future CRM/Helpdesk bridges extend this boundary without coupling channel.py.
# pylint: disable=consider-merging-classes-inherited
class MailChannelCase(models.Model):
    _inherit = "mail.channel"

    contact_center_case_ids = fields.One2many(
        "contact.center.case", "channel_id", string="Atendimentos"
    )
    contact_center_case_count = fields.Integer(
        compute="_compute_contact_center_case_count",
        string="Quantidade de atendimentos",
    )

    def _contact_center_default_case_team(self):
        """A case can infer its operational team only from an unambiguous scope."""

        self.ensure_one()
        teams = self.contact_center_access_team_ids
        return teams if len(teams) == 1 else teams.browse()

    @api.depends("contact_center_case_ids")
    def _compute_contact_center_case_count(self):
        counts = self.env["contact.center.case"]._read_group(
            [("channel_id", "in", self.ids)],
            ["channel_id"],
            ["channel_id"],
        )
        by_channel = {
            item["channel_id"][0]: item["channel_id_count"] for item in counts
        }
        for channel in self:
            channel.contact_center_case_count = by_channel.get(channel.id, 0)

    def _contact_center_reconcile_members(
        self, partner_ids=None, guest_ids=None, allow_empty=False
    ):
        """Keep case scope aligned after native membership is reconciled."""

        result = super()._contact_center_reconcile_members(
            partner_ids=partner_ids,
            guest_ids=guest_ids,
            allow_empty=allow_empty,
        )
        allowed_partner_ids = set(partner_ids or [])
        for channel in self:
            cases = (
                self.env["contact.center.case"]
                .sudo()
                .with_context(active_test=False)
                .search([("channel_id", "=", channel.id)])
            )
            for case in cases:
                team = case.team_id
                if not team or team not in channel.contact_center_access_team_ids:
                    team = channel._contact_center_default_case_team()
                missing_pipelines = (
                    case.pipeline_id - team.pipeline_ids
                    if team
                    else self.env["contact.center.pipeline"]
                )
                if missing_pipelines:
                    raise ValidationError(
                        _(
                            "Enable pipelines %(pipelines)s for team %(team)s before "
                            "assigning this inbox to it."
                        )
                        % {
                            "pipelines": ", ".join(
                                missing_pipelines.mapped("display_name")
                            ),
                            "team": team.display_name,
                        }
                    )
                values = {}
                if case.team_id != team:
                    values["team_id"] = team.id if team else False
                responsible = case.responsible_user_id
                if responsible and responsible.partner_id.id not in allowed_partner_ids:
                    values["responsible_user_id"] = False
                if values:
                    case.with_context(
                        contact_center_case_service_token=_CASE_SERVICE_TOKEN
                    ).write(values)
        return result

    @api.model
    def _contact_center_create_channel(self, **kwargs):
        channel = super()._contact_center_create_channel(**kwargs)
        channel._contact_center_ensure_default_case(account=kwargs.get("account"))
        return channel

    def _contact_center_case_account(self):
        self.ensure_one()
        binding = self.contact_center_binding_ids.filtered(
            lambda item: item.active and not item.merged_into_id
        )[:1]
        return binding.account_id

    def _contact_center_ensure_default_case(self, team=None, account=None):
        self.ensure_one()
        if self.channel_type != "contact_center":
            return self.env["contact.center.case"]
        team = team or self._contact_center_default_case_team()
        account = account or self._contact_center_case_account()
        company = self.contact_center_company_id
        if not company:
            return self.env["contact.center.case"]
        pipeline = (
            account.default_pipeline_id
            if account
            else self.env["contact.center.pipeline"]
        )
        if not pipeline and team:
            pipeline = team.default_pipeline_id
        if not pipeline:
            # Default provisioning serializes on res.company.  Resolve it before
            # locking the channel so this path follows the same company ->
            # channel order as the bulk provisioner/backfill flow.
            pipeline = self.env[
                "contact.center.pipeline"
            ]._contact_center_provision_default_pipeline(company)
        responsible = self.contact_center_responsible_id
        self.env["contact.center.case"]._contact_center_lock_case_topology_ids(
            account_ids=account.ids,
            team_ids=team.ids,
            user_ids=responsible.ids,
            pipeline_ids=pipeline.ids,
            channel_ids=self.ids,
        )
        self.invalidate_recordset(
            [
                "contact_center_company_id",
                "contact_center_access_user_ids",
                "contact_center_responsible_id",
                "contact_center_access_team_ids",
            ]
        )
        existing = (
            self.env["contact.center.case"]
            .sudo()
            .with_context(active_test=False)
            .search([("channel_id", "=", self.id), ("is_default", "=", True)], limit=1)
        )
        if existing:
            return existing
        if team and pipeline not in team.pipeline_ids:
            raise ValidationError(
                _(
                    "Enable pipeline %(pipeline)s for team %(team)s before "
                    "creating a case in this inbox."
                )
                % {
                    "pipeline": pipeline.display_name,
                    "team": team.display_name,
                }
            )
        stage = pipeline._contact_center_initial_stage()
        return (
            self.env["contact.center.case"]
            .sudo()
            .with_context(
                contact_center_case_account_id=account.id if account else False,
                contact_center_case_service_token=_CASE_SERVICE_TOKEN,
            )
            .create(
                {
                    "name": self.name or _("Service"),
                    "channel_id": self.id,
                    "company_id": company.id,
                    "team_id": team.id if team else False,
                    "pipeline_id": pipeline.id,
                    "stage_id": stage.id,
                    "responsible_user_id": responsible.id,
                    "is_default": True,
                }
            )
        )

    @api.model
    def _contact_center_backfill_default_cases(self, companies=None):
        domain = [("channel_type", "=", "contact_center")]
        if companies:
            domain.append(("contact_center_company_id", "in", companies.ids))
        channels = (
            self.sudo().with_context(active_test=False).search(domain, order="id")
        )
        created = self.env["contact.center.case"]
        for channel in channels:
            before = (
                self.env["contact.center.case"]
                .sudo()
                .with_context(active_test=False)
                .search_count(
                    [("channel_id", "=", channel.id), ("is_default", "=", True)]
                )
            )
            case = channel._contact_center_ensure_default_case()
            if case and not before:
                created |= case
        return created

    def action_contact_center_cases(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id(
            "contact_center_kanban.action_contact_center_cases"
        )
        action["domain"] = [("channel_id", "=", self.id)]
        action["context"] = {
            "default_channel_id": self.id,
            "default_company_id": self.contact_center_company_id.id,
            "default_team_id": self._contact_center_default_case_team().id,
            "search_default_group_stage": 1,
        }
        return action


class ContactCenterCase(models.Model):
    _name = "contact.center.case"
    _inherit = ["mail.thread"]
    _description = "Contact Center Case"
    _order = "priority desc, stage_changed_at desc, id desc"
    _check_company_auto = True

    def _default_case_ref(self):
        return str(uuid.uuid4())

    name = fields.Char(required=True)
    case_ref = fields.Char(
        required=True, default=_default_case_ref, copy=False, readonly=True, index=True
    )
    active = fields.Boolean(default=True)
    is_default = fields.Boolean(default=False, copy=False, index=True)
    company_id = fields.Many2one(
        "res.company", required=True, index=True, ondelete="restrict"
    )
    channel_id = fields.Many2one(
        "mail.channel", required=True, index=True, ondelete="cascade"
    )
    team_id = fields.Many2one(
        "contact.center.team",
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    pipeline_id = fields.Many2one(
        "contact.center.pipeline",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    stage_id = fields.Many2one(
        "contact.center.pipeline.stage",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
        domain="[('pipeline_id', '=', pipeline_id)]",
    )
    responsible_user_id = fields.Many2one(
        "res.users",
        string="Responsible",
        index=True,
        ondelete="set null",
        check_company=True,
        domain="[('share', '=', False)]",
    )
    priority = fields.Selection(
        [("0", "Low"), ("1", "Normal"), ("2", "High"), ("3", "Urgent")],
        default="1",
        required=True,
        index=True,
    )
    description = fields.Text()
    opened_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    stage_changed_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True, copy=False
    )
    closed_at = fields.Datetime(index=True, copy=False)
    stage_revision = fields.Integer(required=True, default=0, copy=False, readonly=True)
    transition_ids = fields.One2many(
        "contact.center.case.transition",
        "case_id",
        string="Stage History",
        readonly=True,
    )

    _sql_constraints = [
        ("case_ref_unique", "unique(case_ref)", "Case references must be unique."),
        (
            "default_case_active",
            "check(NOT is_default OR active IS TRUE)",
            "The canonical conversation case must remain active.",
        ),
        (
            "stage_revision_nonnegative",
            "check(stage_revision >= 0)",
            "The case stage revision cannot be negative.",
        ),
    ]

    def init(self):
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS contact_center_case_default_unique
            ON contact_center_case (channel_id)
            WHERE is_default IS TRUE
            """
        )

    @api.model
    def _contact_center_lock_case_topology_ids(
        self,
        *,
        account_ids=(),
        team_ids=(),
        user_ids=(),
        pipeline_ids=(),
        channel_ids=(),
        case_ids=(),
    ):
        """Lock exactly one affected case graph in the canonical core order."""

        self.env["contact.center.account"]._contact_center_lock_access_topology(
            account_ids=account_ids,
            team_ids=team_ids,
            user_ids=user_ids,
            pipeline_ids=pipeline_ids,
            channel_ids=channel_ids,
            case_ids=case_ids,
        )
        return True

    def _contact_center_lock_case_topology(
        self, *, include_cases=True, extra_user_ids=()
    ):
        """Fence this aggregate, optionally leaving case rows for a bridge.

        Optional bridges with their own authority (for example CRM leads) call this
        with ``include_cases=False``, lock their authority, then every affected case,
        and finally call ``_contact_center_mark_case_topology_fenced`` on the recordset
        passed to ``super``.  That preserves ``core authorities -> bridge authority ->
        case`` without importing the optional addon here.
        """

        if (
            include_cases
            and self.env.context.get("contact_center_case_topology_fence_token")
            is _CASE_TOPOLOGY_FENCE_TOKEN
        ):
            return self
        cases = self.sudo().with_context(active_test=False).exists()
        accounts = self.env["contact.center.account"]
        for channel in cases.mapped("channel_id"):
            accounts |= channel._contact_center_case_account()
        teams = cases.mapped("team_id") | cases.mapped(
            "channel_id.contact_center_access_team_ids"
        )
        users = cases.mapped("responsible_user_id") | self.env["res.users"].browse(
            list(extra_user_ids or [])
        )
        pipelines = cases.mapped("pipeline_id") | cases.mapped("stage_id.pipeline_id")
        channels = cases.mapped("channel_id")
        self._contact_center_lock_case_topology_ids(
            account_ids=accounts.ids,
            team_ids=teams.ids,
            user_ids=users.ids,
            pipeline_ids=pipelines.ids,
            channel_ids=channels.ids,
            case_ids=cases.ids if include_cases else (),
        )
        cases.invalidate_recordset(
            [
                "active",
                "channel_id",
                "company_id",
                "is_default",
                "pipeline_id",
                "responsible_user_id",
                "stage_id",
                "stage_revision",
                "team_id",
            ]
        )
        if include_cases:
            return self.with_context(
                contact_center_case_topology_fence_token=_CASE_TOPOLOGY_FENCE_TOKEN
            )
        return self

    def _contact_center_mark_case_topology_fenced(self):
        """Mark cases already locked last by an optional bridge's graph helper."""

        return self.with_context(
            contact_center_case_topology_fence_token=_CASE_TOPOLOGY_FENCE_TOKEN
        )

    @api.onchange("channel_id")
    def _onchange_channel_id(self):
        for case in self:
            channel = case.channel_id
            if not channel or channel.channel_type != "contact_center":
                continue
            case.company_id = channel.contact_center_company_id
            case.team_id = channel._contact_center_default_case_team()
            case.responsible_user_id = channel.contact_center_responsible_id
            account = channel._contact_center_case_account()
            pipeline = account.default_pipeline_id if account else False
            if not pipeline and case.team_id:
                pipeline = case.team_id.default_pipeline_id
            if pipeline:
                case.pipeline_id = pipeline
                case.stage_id = pipeline._contact_center_initial_stage()

    @api.onchange("team_id")
    def _onchange_team_id(self):
        for case in self:
            if case.team_id and not case.pipeline_id:
                case.pipeline_id = case.team_id.default_pipeline_id
            if case.pipeline_id and (
                not case.stage_id or case.stage_id.pipeline_id != case.pipeline_id
            ):
                case.stage_id = case.pipeline_id._contact_center_initial_stage()

    @api.onchange("pipeline_id")
    def _onchange_pipeline_id(self):
        for case in self:
            if case.pipeline_id and (
                not case.stage_id or case.stage_id.pipeline_id != case.pipeline_id
            ):
                case.stage_id = case.pipeline_id._contact_center_initial_stage()

    @api.model
    def _contact_center_internal_case_create(self, vals_list):
        internal = (
            self.env.context.get("contact_center_case_service_token")
            is _CASE_SERVICE_TOKEN
        )
        if internal:
            return True
        protected = sorted(
            set().union(*(set(values) for values in vals_list))
            & _CASE_SERVER_OWNED_CREATE_FIELDS
        )
        if protected:
            raise AccessError(
                _(
                    "Case metadata is assigned by the application service: %s",
                    ", ".join(protected),
                )
            )
        return False

    @api.model_create_multi
    def create(self, vals_list):
        internal = self._contact_center_internal_case_create(vals_list)
        prepared = []
        topology = {
            "account_ids": set(),
            "team_ids": set(),
            "user_ids": set(),
            "pipeline_ids": set(),
            "channel_ids": set(),
        }
        for source in vals_list:
            values = dict(source)
            if not internal:
                # RPC context defaults must not bypass the same service-owned
                # metadata boundary as explicit create values.
                values.update(active=True, is_default=False)
            channel = self.env["mail.channel"].browse(values.get("channel_id")).exists()
            if not channel:
                raise ValidationError(_("A case requires an existing conversation."))
            if (
                channel.channel_type != "contact_center"
                or not channel.contact_center_company_id
            ):
                raise ValidationError(
                    _("Cases require a company-scoped Contact Center conversation.")
                )
            team_value = (
                values["team_id"]
                if "team_id" in values
                else channel._contact_center_default_case_team().id
            )
            team = self.env["contact.center.team"].browse(team_value).exists()
            account = channel._contact_center_case_account()
            if (
                not account
                and internal
                and self.env.context.get("contact_center_case_account_id")
            ):
                account = (
                    self.env["contact.center.account"]
                    .browse(self.env.context["contact_center_case_account_id"])
                    .exists()
                )
            pipeline_id = values.get("pipeline_id")
            if not pipeline_id and account:
                pipeline_id = account.default_pipeline_id.id
            if not pipeline_id and team:
                pipeline_id = team.default_pipeline_id.id
            pipeline = self.env["contact.center.pipeline"].browse(pipeline_id).exists()
            if not pipeline:
                pipeline = self.env[
                    "contact.center.pipeline"
                ]._contact_center_provision_default_pipeline(
                    channel.contact_center_company_id
                )
            stage = (
                self.env["contact.center.pipeline.stage"]
                .browse(values.get("stage_id"))
                .exists()
            )
            if not stage and pipeline:
                stage = pipeline._contact_center_initial_stage()
            now = fields.Datetime.now()
            values.update(
                {
                    "case_ref": _normalized_uuid(
                        values.get("case_ref"), _("case reference")
                    ),
                    "company_id": values.get("company_id")
                    or channel.contact_center_company_id.id,
                    "team_id": team.id if team else False,
                    "pipeline_id": pipeline.id,
                    "stage_id": stage.id,
                    "responsible_user_id": (
                        values["responsible_user_id"]
                        if "responsible_user_id" in values
                        else (channel.contact_center_responsible_id).id
                    ),
                    "opened_at": values.get("opened_at") or now,
                    "stage_changed_at": values.get("stage_changed_at") or now,
                    "closed_at": (
                        (values.get("closed_at") or now)
                        if stage and stage.is_closed
                        else False
                    ),
                    "stage_revision": 0,
                }
            )
            prepared.append(values)
            if account:
                topology["account_ids"].add(account.id)
            if team:
                topology["team_ids"].add(team.id)
            if pipeline:
                topology["pipeline_ids"].add(pipeline.id)
            if channel:
                topology["channel_ids"].add(channel.id)
            if values.get("responsible_user_id"):
                topology["user_ids"].add(int(values["responsible_user_id"]))
        self._contact_center_lock_case_topology_ids(**topology)
        cases = super().create(prepared)
        transition_model = self.env["contact.center.case.transition"].sudo()
        for case in cases:
            transition = transition_model.with_context(
                contact_center_case_service_token=_CASE_SERVICE_TOKEN
            ).create(
                {
                    "case_id": case.id,
                    "from_stage_id": False,
                    "to_stage_id": case.stage_id.id,
                    "from_revision": 0,
                    "to_revision": 0,
                    "request_uuid": case.case_ref,
                    "source": "creation",
                    "changed_by_id": self.env.user.id,
                    "occurred_at": case.opened_at,
                    "is_noop": False,
                }
            )
            case._contact_center_after_transition(transition)
        return cases

    def write(self, values):
        protected = {
            "active",
            "case_ref",
            "company_id",
            "channel_id",
            "team_id",
            "pipeline_id",
            "opened_at",
            "stage_revision",
            "stage_changed_at",
            "closed_at",
            "is_default",
        }
        internal = (
            self.env.context.get("contact_center_case_service_token")
            is _CASE_SERVICE_TOKEN
        )
        if not internal and protected & set(values):
            raise AccessError(
                _("Case structure is managed by the application service.")
            )
        cases = self
        topology_fields = {
            "active",
            "pipeline_id",
            "responsible_user_id",
            "stage_id",
            "team_id",
        }
        if topology_fields & set(values) and (
            self.env.context.get("contact_center_case_topology_fence_token")
            is not _CASE_TOPOLOGY_FENCE_TOKEN
        ):
            user_ids = set(self.mapped("responsible_user_id").ids)
            if values.get("responsible_user_id"):
                user_ids.add(int(values["responsible_user_id"]))
            cases = self._contact_center_lock_case_topology(extra_user_ids=user_ids)
        if not internal and "stage_id" in values:
            if len(values) != 1:
                raise ValidationError(
                    _("Move a case stage separately from other case changes.")
                )
            target_stage_id = values["stage_id"]
            for case in cases:
                case.action_transition(
                    target_stage_id,
                    expected_revision=case.stage_revision,
                    request_uuid=str(uuid.uuid4()),
                )
            return True
        return super(ContactCenterCase, cases).write(values)

    @api.constrains(
        "company_id",
        "channel_id",
        "team_id",
        "pipeline_id",
        "stage_id",
        "responsible_user_id",
        "opened_at",
        "stage_changed_at",
        "closed_at",
        "active",
    )
    def _check_case_configuration(self):
        for case in self:
            case._contact_center_validate_case_scope()
            case._contact_center_validate_case_responsible()
            case._contact_center_validate_case_dates()

    def _contact_center_validate_case_scope(self):
        self.ensure_one()
        if self.channel_id.channel_type != "contact_center":
            raise ValidationError(_("Cases require a Contact Center conversation."))
        if self.channel_id.contact_center_company_id != self.company_id:
            raise ValidationError(_("The case and conversation companies differ."))
        if (
            self.team_id
            and self.team_id not in self.channel_id.contact_center_access_team_ids
        ):
            raise ValidationError(
                _("The case team must be one of the conversation access teams.")
            )
        if self.team_id and self.team_id.company_id != self.company_id:
            raise ValidationError(_("The case team belongs to another company."))
        if self.pipeline_id.company_id != self.company_id:
            raise ValidationError(_("The case pipeline belongs to another company."))
        if self.active and not self.pipeline_id.active:
            raise ValidationError(_("An active case requires an active pipeline."))
        if self.team_id and self.pipeline_id not in self.team_id.pipeline_ids:
            raise ValidationError(_("The case pipeline is not enabled for its team."))
        if self.stage_id.pipeline_id != self.pipeline_id:
            raise ValidationError(_("The case stage belongs to another pipeline."))
        if self.active and not self.stage_id.active:
            raise ValidationError(_("An active case requires an active stage."))

    def _contact_center_case_access_account(self):
        self.ensure_one()
        account = self.channel_id._contact_center_case_account()
        account_id = self.env.context.get("contact_center_case_account_id")
        if not account and account_id:
            account = self.env["contact.center.account"].browse(account_id).exists()
        return account

    def _contact_center_validate_case_responsible(self):
        self.ensure_one()
        responsible = self.responsible_user_id
        if not responsible:
            return
        if (
            not responsible.active
            or responsible.share
            or self.company_id not in responsible.company_ids
        ):
            raise ValidationError(
                _("The responsible user is not available for this case.")
            )
        account = self._contact_center_case_access_account()
        allowed_users = self.env["res.users"]
        if self.channel_id.contact_center_access_user_ids:
            allowed_users |= self.channel_id.contact_center_access_user_ids
        if account:
            allowed_users |= account._contact_center_effective_users()
        if self.team_id:
            allowed_users |= self.team_id.agent_ids | self.team_id.supervisor_ids
        if responsible not in allowed_users:
            raise ValidationError(
                _("The responsible user is not available for this case.")
            )

    def _contact_center_validate_case_dates(self):
        self.ensure_one()
        opened_at = fields.Datetime.to_datetime(self.opened_at)
        changed_at = fields.Datetime.to_datetime(self.stage_changed_at)
        closed_at = fields.Datetime.to_datetime(self.closed_at)
        if changed_at and opened_at and changed_at < opened_at:
            raise ValidationError(
                _("The stage change date cannot precede the case opening date.")
            )
        if self.stage_id.is_closed and not closed_at:
            raise ValidationError(_("A case in a closed stage requires a close date."))
        if not self.stage_id.is_closed and closed_at:
            raise ValidationError(_("An open case cannot have a close date."))
        if closed_at and opened_at and closed_at < opened_at:
            raise ValidationError(
                _("The case close date cannot precede its opening date.")
            )

    def _contact_center_check_operational_access(self):
        self.ensure_one()
        self.check_access_rights("write")
        self.check_access_rule("write")
        if not self.env.su and not self.env.user.has_group("base.group_system"):
            self.channel_id._contact_center_member_for_current_user()
        return True

    def _contact_center_archive_blockers(self):
        """Return extension-owned archive blockers for this case.

        Optional bridges append human-readable reasons and must coordinate their
        child-row lifecycle with the case topology fence.  The base intentionally
        knows nothing about CRM, Helpdesk, or future projections.
        """

        self.ensure_one()
        return []

    def _contact_center_native_archive_blockers(self):
        self.ensure_one()
        blockers = []
        if self.is_default:
            blockers.append(_("the canonical conversation case cannot be archived"))
        return blockers

    def action_archive(self):
        self.ensure_one()
        case = self._contact_center_lock_case_topology(extra_user_ids=self.env.user.ids)
        case._contact_center_check_operational_access()
        if not case.active:
            return True
        blockers = (
            case._contact_center_native_archive_blockers()
            + case._contact_center_archive_blockers()
        )
        if blockers:
            raise ValidationError(
                _("This case cannot be archived: %s", "; ".join(blockers))
            )
        case.with_context(contact_center_case_service_token=_CASE_SERVICE_TOKEN).write(
            {"active": False}
        )
        return True

    def action_unarchive(self):
        self.ensure_one()
        case = self._contact_center_lock_case_topology(extra_user_ids=self.env.user.ids)
        case._contact_center_check_operational_access()
        if case.active:
            return True
        case.with_context(contact_center_case_service_token=_CASE_SERVICE_TOKEN).write(
            {"active": True}
        )
        return True

    def toggle_active(self):
        for case in self:
            if case.active:
                case.action_archive()
            else:
                case.action_unarchive()
        return True

    def _contact_center_after_transition(self, transition):
        """Neutral same-transaction hook for optional CRM/Helpdesk bridges."""

        self.ensure_one()
        transition.ensure_one()
        return True

    def action_transition(
        self,
        target_stage_id,
        expected_revision=None,
        request_uuid=None,
        source="manual",
    ):
        self.ensure_one()
        case = self._contact_center_lock_case_topology(extra_user_ids=self.env.user.ids)
        case._contact_center_check_operational_access()
        if not case.active:
            raise ValidationError(_("An archived case cannot change stage."))
        if type(target_stage_id) is not int or target_stage_id <= 0:  # noqa: E721
            raise ValidationError(_("Invalid target stage."))
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0  # noqa: E721
        ):
            raise ValidationError(_("Invalid expected stage revision."))
        if source not in ("manual", "automation", "integration", "migration"):
            raise ValidationError(_("Invalid case transition source."))
        if (
            source != "manual"
            and self.env.context.get("contact_center_case_transition_token")
            is not CONTACT_CENTER_CASE_TRANSITION_TOKEN
        ):
            raise AccessError(
                _(
                    "Only an internal Contact Center service can record a "
                    "non-manual transition source."
                )
            )
        request_uuid = _normalized_uuid(request_uuid, _("transition request UUID"))

        case.invalidate_recordset(
            ["stage_id", "stage_revision", "stage_changed_at", "closed_at"]
        )
        transition_model = self.env["contact.center.case.transition"].sudo()
        existing = transition_model.search(
            [("case_id", "=", case.id), ("request_uuid", "=", request_uuid)],
            limit=1,
        )
        if existing:
            if existing.to_stage_id.id != target_stage_id or existing.source != source:
                raise ValidationError(
                    _("This transition request UUID was used with another payload.")
                )
            return existing._contact_center_result(idempotent=True)
        if expected_revision is not None and expected_revision != case.stage_revision:
            raise ValidationError(
                _(
                    "The case stage changed from revision %(expected)s to "
                    "%(current)s. Refresh before moving it again."
                )
                % {"expected": expected_revision, "current": case.stage_revision}
            )
        target = (
            self.env["contact.center.pipeline.stage"].browse(target_stage_id).exists()
        )
        if not target or target.pipeline_id != case.pipeline_id:
            raise ValidationError(_("The target stage is outside the case pipeline."))
        if not target.active and target != case.stage_id:
            raise ValidationError(_("An archived stage cannot receive cases."))

        previous = case.stage_id
        from_revision = case.stage_revision
        is_noop = previous == target
        to_revision = from_revision if is_noop else from_revision + 1
        occurred_at = fields.Datetime.now()
        if not is_noop:
            values = {
                "stage_id": target.id,
                "stage_revision": to_revision,
                "stage_changed_at": occurred_at,
                "closed_at": (
                    (case.closed_at or occurred_at) if target.is_closed else False
                ),
            }
            case.with_context(
                contact_center_case_service_token=_CASE_SERVICE_TOKEN
            ).write(values)
        transition = transition_model.with_context(
            contact_center_case_service_token=_CASE_SERVICE_TOKEN
        ).create(
            {
                "case_id": case.id,
                "from_stage_id": previous.id,
                "to_stage_id": target.id,
                "from_revision": from_revision,
                "to_revision": to_revision,
                "request_uuid": request_uuid,
                "source": source,
                "changed_by_id": self.env.user.id,
                "occurred_at": occurred_at,
                "is_noop": is_noop,
            }
        )
        case._contact_center_after_transition(transition)
        return transition._contact_center_result(idempotent=False)

    def unlink(self):
        if (
            self.env.context.get("contact_center_deletion_token")
            is CONTACT_CENTER_DELETION_TOKEN
        ):
            return super().unlink()
        raise AccessError(_("Cases cannot be deleted; archive them instead."))


class ContactCenterCaseTransition(models.Model):
    _name = "contact.center.case.transition"
    _description = "Contact Center Case Stage Transition"
    _order = "occurred_at desc, id desc"
    _check_company_auto = True

    case_id = fields.Many2one(
        "contact.center.case", required=True, index=True, ondelete="cascade"
    )
    company_id = fields.Many2one(
        related="case_id.company_id", store=True, readonly=True, index=True
    )
    channel_id = fields.Many2one(
        related="case_id.channel_id", store=True, readonly=True, index=True
    )
    pipeline_id = fields.Many2one(
        related="case_id.pipeline_id", store=True, readonly=True, index=True
    )
    from_stage_id = fields.Many2one(
        "contact.center.pipeline.stage", index=True, ondelete="restrict"
    )
    to_stage_id = fields.Many2one(
        "contact.center.pipeline.stage", required=True, index=True, ondelete="restrict"
    )
    from_revision = fields.Integer(required=True, readonly=True)
    to_revision = fields.Integer(required=True, readonly=True)
    request_uuid = fields.Char(required=True, index=True, readonly=True, copy=False)
    source = fields.Selection(
        [
            ("creation", "Creation"),
            ("manual", "Manual"),
            ("automation", "Automation"),
            ("integration", "Integration"),
            ("migration", "Migration"),
        ],
        required=True,
        readonly=True,
    )
    changed_by_id = fields.Many2one(
        "res.users", required=True, readonly=True, ondelete="restrict"
    )
    occurred_at = fields.Datetime(required=True, readonly=True, index=True)
    is_noop = fields.Boolean(required=True, default=False, readonly=True)

    _sql_constraints = [
        (
            "request_case_unique",
            "unique(case_id, request_uuid)",
            "A transition request can be applied only once per case.",
        ),
        (
            "revision_order",
            "check(from_revision >= 0 AND to_revision >= from_revision)",
            "Case transition revisions are invalid.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get("contact_center_case_service_token")
            is not _CASE_SERVICE_TOKEN
        ):
            raise AccessError(
                _("Case transition history is managed by the application service.")
            )
        return super().create(vals_list)

    @api.constrains(
        "case_id",
        "from_stage_id",
        "to_stage_id",
        "from_revision",
        "to_revision",
        "source",
        "is_noop",
    )
    def _check_transition_consistency(self):
        for transition in self:
            pipeline = transition.case_id.pipeline_id
            if transition.to_stage_id.pipeline_id != pipeline:
                raise ValidationError(
                    _("The transition target belongs to another pipeline.")
                )
            if (
                transition.from_stage_id
                and transition.from_stage_id.pipeline_id != pipeline
            ):
                raise ValidationError(
                    _("The transition origin belongs to another pipeline.")
                )
            if transition.source == "creation":
                if (
                    transition.from_stage_id
                    or transition.from_revision != 0
                    or transition.to_revision != 0
                    or transition.is_noop
                ):
                    raise ValidationError(_("The case creation history is invalid."))
            elif transition.is_noop:
                if (
                    transition.from_stage_id != transition.to_stage_id
                    or transition.from_revision != transition.to_revision
                ):
                    raise ValidationError(_("The no-op transition history is invalid."))
            elif (
                not transition.from_stage_id
                or transition.from_stage_id == transition.to_stage_id
                or transition.to_revision != transition.from_revision + 1
            ):
                raise ValidationError(_("The stage transition history is invalid."))

    def write(self, values):  # pylint: disable=method-required-super
        raise AccessError(_("Case transition history is immutable."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("Case transition history is immutable."))

    def _contact_center_result(self, *, idempotent):
        self.ensure_one()
        return {
            "case_id": self.case_id.id,
            "stage_id": self.to_stage_id.id,
            "stage_revision": self.to_revision,
            "transition_id": self.id,
            "idempotent": idempotent,
            "noop": self.is_noop,
        }
