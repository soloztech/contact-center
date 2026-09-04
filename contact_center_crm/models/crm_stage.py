from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .binding import (
    CRM_BINDING_GRAPH_LOCK_TOKEN,
    binding_graph_is_locked,
    lock_binding_graph,
    lock_catalog_authorities,
)


class CrmStage(models.Model):
    _inherit = "crm.stage"

    active = fields.Boolean(default=True, index=True)
    contact_center_stage_binding_ids = fields.One2many(
        "contact.center.crm.stage.binding",
        "crm_stage_id",
        string="Contact Center Stage Bindings",
    )

    def _contact_center_affected_pipeline_bindings(self):
        binding_model = self.env["contact.center.crm.pipeline.binding"].sudo()
        if not self:
            return binding_model.browse()
        domain = [("active", "=", True)]
        if all(stage.team_id for stage in self):
            domain.append(("crm_team_id", "in", self.mapped("team_id").ids))
        return binding_model.search(domain)

    @api.model_create_multi
    def create(self, vals_list):
        team_ids = [
            values.get("team_id") for values in vals_list if values.get("team_id")
        ]
        lock_catalog_authorities(self.env, team_ids)
        stages = super().create(vals_list)
        stages._contact_center_affected_pipeline_bindings()._sync_crm_stage_catalog()
        return stages

    def write(self, values):
        relevant = {
            "active",
            "name",
            "sequence",
            "fold",
            "is_won",
            "team_id",
        } & set(values)
        if not relevant:
            return super().write(values)

        previous = self.env["contact.center.crm.pipeline.binding"]
        previous = (
            self.sudo()
            .with_context(active_test=False)
            .mapped("contact_center_stage_binding_ids.pipeline_binding_id")
        )
        team_ids = set(self.mapped("team_id").ids)
        if values.get("team_id"):
            team_ids.add(int(values["team_id"]))
        lock_binding_graph(
            self.env,
            pipeline_binding_ids=previous.ids,
            crm_team_ids=team_ids,
            touch=True,
        )
        with self.env.cr.savepoint():
            result = super().write(values)
            affected = previous | self._contact_center_affected_pipeline_bindings()
            affected.with_context(active_test=True)._sync_crm_stage_catalog()
        return result

    def unlink(self):
        self.check_access_rights("unlink")
        self.check_access_rule("unlink")
        with self.env.cr.savepoint():
            mappings = (
                self.env["contact.center.crm.stage.binding"]
                .sudo()
                .with_context(active_test=False)
                .search([("crm_stage_id", "in", self.ids)])
            )
            affected = mappings.mapped("pipeline_binding_id").filtered("active")
            lock_binding_graph(
                self.env,
                pipeline_binding_ids=affected.ids,
                crm_team_ids=self.mapped("team_id").ids,
                touch=True,
            )
            # Current cases make the mapping unlink fail. Historical-only core stages
            # stay archived after catalog convergence, preserving transition ledgers.
            mappings.unlink()
            result = super().unlink()
            affected.exists()._sync_crm_stage_catalog()
        return result


class CrmTeam(models.Model):
    _inherit = "crm.team"

    contact_center_roster_revision = fields.Integer(
        required=True,
        default=0,
        readonly=True,
        copy=False,
        help="Internal concurrency fence for Contact Center roster projection.",
    )

    _sql_constraints = [
        (
            "contact_center_roster_revision_nonnegative",
            "check(contact_center_roster_revision >= 0)",
            "The Contact Center roster revision cannot be negative.",
        ),
    ]

    contact_center_team_binding_ids = fields.One2many(
        "contact.center.crm.team.binding",
        "crm_team_id",
        string="Contact Center Teams",
    )
    contact_center_pipeline_binding_ids = fields.One2many(
        "contact.center.crm.pipeline.binding",
        "crm_team_id",
        string="Contact Center Pipelines",
    )
    contact_center_team_binding_count = fields.Integer(
        compute="_compute_contact_center_binding_counts"
    )
    contact_center_pipeline_binding_count = fields.Integer(
        compute="_compute_contact_center_binding_counts"
    )

    @api.depends(
        "contact_center_team_binding_ids",
        "contact_center_pipeline_binding_ids",
    )
    def _compute_contact_center_binding_counts(self):
        for team in self:
            team.contact_center_team_binding_count = len(
                team.contact_center_team_binding_ids
            )
            team.contact_center_pipeline_binding_count = len(
                team.contact_center_pipeline_binding_ids
            )

    def _contact_center_lock_roster_authorities(self, *, touch=True):
        """Lock CRM roster authorities and optionally publish a new revision."""

        team_ids = sorted(set(self.ids))
        if team_ids:
            self.flush_model(["contact_center_roster_revision"])
            self.env.cr.execute(
                "SELECT id FROM crm_team WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                [team_ids],
            )
            if touch:
                self.env.cr.execute(
                    "UPDATE crm_team SET contact_center_roster_revision = "
                    "contact_center_roster_revision + 1 WHERE id = ANY(%s)",
                    [team_ids],
                )
                self.invalidate_recordset(["contact_center_roster_revision"])
        return True

    def _contact_center_sync_bound_rosters(self):
        if not self:
            return True
        bindings = (
            self.env["contact.center.crm.team.binding"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("crm_team_id", "in", self.ids),
                    ("active", "=", True),
                ],
                order="crm_team_id, id",
            )
        )
        bindings._sync_crm_roster()
        return True

    @api.model_create_multi
    def create(self, vals_list):
        if any("contact_center_roster_revision" in values for values in vals_list):
            raise AccessError(_("The Contact Center roster revision is internal."))
        return super().create(vals_list)

    def write(self, values):
        if "contact_center_roster_revision" in values:
            raise AccessError(_("The Contact Center roster revision is internal."))
        roster_fields = {"user_id", "member_ids", "active", "company_id"} & set(values)
        if roster_fields and not binding_graph_is_locked(self.env):
            lock_binding_graph(self.env, crm_team_ids=self.ids, touch=True)
            return self.with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            ).write(values)
        if values.get("active") is False:
            bound = self.filtered(
                lambda team: team.sudo().contact_center_team_binding_ids.filtered(
                    "active"
                )
                or team.sudo().contact_center_pipeline_binding_ids.filtered("active")
            )
            if bound:
                raise ValidationError(
                    _(
                        "A CRM team with active Contact Center bindings cannot be "
                        "archived."
                    )
                )
        if "company_id" in values:
            company = self.env["res.company"].browse(values["company_id"])
            for team in self:
                team_bindings = (
                    team.sudo()
                    .with_context(active_test=False)
                    .contact_center_team_binding_ids
                )
                pipeline_bindings = (
                    team.sudo()
                    .with_context(active_test=False)
                    .contact_center_pipeline_binding_ids
                )
                if company and (
                    any(binding.company_id != company for binding in team_bindings)
                    or any(
                        binding.company_id != company for binding in pipeline_bindings
                    )
                ):
                    raise ValidationError(
                        _(
                            "The CRM team company cannot differ from its "
                            "Contact Center bindings."
                        )
                    )
        result = super().write(values)
        if roster_fields:
            self._contact_center_sync_bound_rosters()
        return result

    def action_view_contact_center_bindings(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Contact Center Bindings"),
            "res_model": "contact.center.crm.pipeline.binding",
            "view_mode": "tree,form",
            "domain": [("crm_team_id", "=", self.id)],
            "context": {"default_crm_team_id": self.id},
        }

    def action_view_contact_center_team_bindings(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Contact Center Teams"),
            "res_model": "contact.center.crm.team.binding",
            "view_mode": "tree,form",
            "domain": [("crm_team_id", "=", self.id)],
            "context": {"default_crm_team_id": self.id},
        }
