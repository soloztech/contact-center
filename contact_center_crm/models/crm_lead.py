from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.contact_center_kanban.services.tokens import (
    CONTACT_CENTER_CASE_TRANSITION_TOKEN,
)

from .binding import CRM_BINDING_GRAPH_LOCK_TOKEN, CRM_CASE_STAGE_SYNC_TOKEN
from .stage_sync import CRM_STAGE_SYNC_GRAPH_LOCK_TOKEN, stage_sync_graph_is_locked


class CrmLead(models.Model):
    _inherit = "crm.lead"

    contact_center_case_link_ids = fields.One2many(
        "contact.center.crm.case.link",
        "lead_id",
        string="Contact Center Cases",
    )
    contact_center_case_count = fields.Integer(
        compute="_compute_contact_center_case_count"
    )

    def _contact_center_lock_link_graph(self, *, touch_leads=False, touch_cases=False):
        """Acquire optional bridge locks in the canonical global order."""

        links = (
            self.env["contact.center.crm.case.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "in", self.ids),
                    ("state", "=", "active"),
                ]
            )
        )
        links.mapped("case_id")._contact_center_lock_crm_graph(
            lead_ids=self.ids,
            touch_leads=touch_leads,
            touch_cases=touch_cases,
        )
        return self.with_context(
            contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN,
            contact_center_crm_stage_sync_graph_lock=(CRM_STAGE_SYNC_GRAPH_LOCK_TOKEN),
        )

    @api.depends("contact_center_case_link_ids")
    def _compute_contact_center_case_count(self):
        counts = self.env["contact.center.crm.case.link"].read_group(
            [
                ("lead_id", "in", self.ids),
                ("state", "=", "active"),
            ],
            ["lead_id"],
            ["lead_id"],
        )
        count_by_lead = {item["lead_id"][0]: item["lead_id_count"] for item in counts}
        for lead in self:
            lead.contact_center_case_count = count_by_lead.get(lead.id, 0)

    def _contact_center_validate_linked_values(self, values):
        relevant = {"company_id", "team_id", "stage_id"} & set(values)
        if not relevant:
            return True
        links = (
            self.env["contact.center.crm.case.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "in", self.ids),
                    ("state", "=", "active"),
                ]
            )
        )
        if not links:
            return True
        links_by_lead = {}
        for link in links:
            links_by_lead.setdefault(link.lead_id.id, []).append(link)
        proposed_stages = (
            self.env["crm.stage"].browse(values["stage_id"])
            if "stage_id" in values and values["stage_id"]
            else self.mapped("stage_id")
        )
        contract_maps = links._contact_center_contract_maps(crm_stages=proposed_stages)
        for lead in self:
            lead_links = links_by_lead.get(lead.id)
            if not lead_links:
                continue
            company = (
                self.env["res.company"].browse(values["company_id"])
                if "company_id" in values
                else lead.company_id
            )
            team = (
                self.env["crm.team"].browse(values["team_id"])
                if "team_id" in values
                else lead.team_id
            )
            stage = (
                self.env["crm.stage"].browse(values["stage_id"])
                if "stage_id" in values
                else lead.stage_id
            )
            if not company or not team or not stage:
                raise ValidationError(
                    _(
                        "A CRM lead linked to Contact Center requires company, "
                        "sales team and stage."
                    )
                )
            for link in lead_links:
                case = link.case_id
                if company != case.company_id:
                    raise ValidationError(
                        _("A linked CRM lead cannot move to another company.")
                    )
                pipeline_binding = contract_maps["pipeline_by_source"].get(
                    case.pipeline_id.id
                )
                if not pipeline_binding or team != pipeline_binding.crm_team_id:
                    raise ValidationError(
                        _("A linked CRM lead cannot move to an unmapped sales team.")
                    )
                if stage.team_id and stage.team_id != team:
                    raise ValidationError(
                        _("The CRM stage belongs to another sales team.")
                    )
                if not contract_maps["stage_by_pair"].get(
                    (pipeline_binding.id, stage.id)
                ):
                    raise ValidationError(
                        _("The CRM stage is not mapped in every linked case pipeline.")
                    )
        return True

    def _merge_opportunity(
        self,
        user_id=False,
        team_id=False,
        auto_unlink=True,
        max_length=5,
    ):
        """Serialize native CRM merge against every linked service case."""

        leads = self
        if self.ids and not stage_sync_graph_is_locked(self.env):
            leads = self._contact_center_lock_link_graph(
                touch_leads=True,
                touch_cases=True,
            )
            leads.invalidate_recordset(["company_id", "stage_id", "team_id"])
        return super(CrmLead, leads)._merge_opportunity(
            user_id=user_id,
            team_id=team_id,
            auto_unlink=auto_unlink,
            max_length=max_length,
        )

    def _merge_dependences(self, opportunities):
        """Transfer case bridges before core removes the source CRM leads."""

        self.ensure_one()
        if not stage_sync_graph_is_locked(self.env):
            combined = self | opportunities
            combined = combined._contact_center_lock_link_graph(
                touch_leads=True,
                touch_cases=True,
            )
            self = combined.filtered(lambda lead: lead.id == self.id)
            opportunities = combined - self
        source_links = (
            self.env["contact.center.crm.case.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "in", opportunities.ids),
                    ("state", "=", "active"),
                ],
                order="case_id, id",
            )
        )
        if source_links:
            self.invalidate_recordset(["company_id", "stage_id", "team_id"])
            opportunities.invalidate_recordset(["company_id", "stage_id", "team_id"])
            source_links.invalidate_recordset(["case_id", "lead_id", "state"])
            source_links._contact_center_transfer_to_lead(self)
        return super()._merge_dependences(opportunities)

    def unlink(self):
        """Keep an audit tombstone without preventing native CRM deletion."""

        # Native ``super().unlink()`` checks this only after the bridge side
        # effects below.  Preserve the original caller and reject before any
        # sudo graph discovery or tombstone mutation crosses that boundary.
        self.check_access_rights("unlink")
        self.check_access_rule("unlink")
        actor_user_id = self.env.uid
        if self.ids and not stage_sync_graph_is_locked(self.env):
            self = self._contact_center_lock_link_graph(
                touch_leads=True,
                touch_cases=True,
            )
            self.check_access_rights("unlink")
            self.check_access_rule("unlink")
        links = (
            self.env["contact.center.crm.case.link"]
            .sudo()
            .search(
                [
                    ("lead_id", "in", self.ids),
                    ("state", "=", "active"),
                ]
            )
        )
        if links:
            links._contact_center_tombstone_deleted_lead(actor_user_id=actor_user_id)
        return super().unlink()

    def write(self, values):
        relevant = {"company_id", "team_id", "stage_id"} & set(values)
        leads = self
        if relevant and self:
            self.check_access_rights("write")
            self.check_access_rule("write")
            if not stage_sync_graph_is_locked(self.env):
                leads = self._contact_center_lock_link_graph(
                    touch_cases=True,
                )
        leads._contact_center_validate_linked_values(values)
        result = super(CrmLead, leads).write(values)
        links = leads.sudo().contact_center_case_link_ids if relevant else None
        contract_maps = None
        if relevant:
            contract_maps = links._contact_center_contract_maps()
            links._validate_link_contract(contract_maps=contract_maps)
        if "stage_id" in values:
            leads._contact_center_sync_case_stages(
                links=links, contract_maps=contract_maps
            )
        return result

    def _contact_center_sync_case_stages(self, *, links=None, contract_maps=None):
        origin_case_id = (
            self.env.context.get("contact_center_crm_origin_case_id")
            if self.env.context.get("contact_center_crm_stage_sync")
            is CRM_CASE_STAGE_SYNC_TOKEN
            else None
        )
        links = links if links is not None else self.sudo().contact_center_case_link_ids
        contract_maps = contract_maps or links._contact_center_contract_maps()
        links_by_lead = {}
        for link in links:
            links_by_lead.setdefault(link.lead_id.id, []).append(link)
        for lead in self.sorted("id"):
            for link in sorted(
                links_by_lead.get(lead.id, ()),
                key=lambda item: (item.case_id.id, item.id),
            ):
                case = link.case_id
                if not case.active or case.id == origin_case_id:
                    continue
                stage_binding = link._stage_binding_for_crm_stage(
                    lead.stage_id, contract_maps=contract_maps
                )
                if case.stage_id == stage_binding.stage_id:
                    continue
                case.sudo().with_context(
                    contact_center_crm_stage_sync=CRM_CASE_STAGE_SYNC_TOKEN,
                    contact_center_case_transition_token=(
                        CONTACT_CENTER_CASE_TRANSITION_TOKEN
                    ),
                ).action_transition(
                    stage_binding.stage_id.id,
                    source="integration",
                )
        return True

    def action_view_contact_center_cases(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")
        cases = self.contact_center_case_link_ids.mapped("case_id")
        action = self.env["ir.actions.actions"]._for_xml_id(
            "contact_center_kanban.action_contact_center_cases"
        )
        action.update(
            {
                "name": _("Contact Center Cases"),
                "domain": [("id", "in", cases.ids)],
                "context": {"create": False},
            }
        )
        if len(cases) == 1:
            action.update(
                {
                    "res_id": cases.id,
                    "view_mode": "form",
                    "views": [(False, "form")],
                }
            )
        return action
