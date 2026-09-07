import hashlib
import json
import re
import unicodedata

from psycopg2 import IntegrityError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .binding import CRM_BINDING_GRAPH_LOCK_TOKEN, lock_binding_graph


def _normalized_label(value):
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_value.lower())


def _snapshot_hash(payload):
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _ensure_mapping_authority(env):
    """Enforce the intersection of both administrative trust domains."""

    if env.su:
        return True
    has_contact_center_authority = env.user.has_group(
        "contact_center_base.group_contact_center_admin"
    )
    has_crm_authority = env.user.has_group("sales_team.group_sale_manager")
    if not (has_contact_center_authority and has_crm_authority):
        raise AccessError(
            _(
                "CRM mapping candidates require both Contact Center "
                "Administrator and CRM Sales Manager authority."
            )
        )
    return True


class ContactCenterCrmMappingInventory(models.TransientModel):
    _name = "contact.center.crm.mapping.inventory"
    _description = "Contact Center CRM Mapping Candidate Inventory"

    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        domain="[('id', 'in', allowed_company_ids)]",
    )
    allowed_company_ids = fields.Many2many(
        "res.company",
        compute="_compute_allowed_company_ids",
    )
    include_inactive = fields.Boolean(
        help="Include archived Contact Center sources and matching CRM teams.",
    )
    generated_at = fields.Datetime(readonly=True)
    line_ids = fields.One2many(
        "contact.center.crm.mapping.inventory.line",
        "inventory_id",
        string="Candidate Evidence",
        readonly=True,
    )
    source_count = fields.Integer(readonly=True)
    candidate_count = fields.Integer(readonly=True)
    ambiguous_source_count = fields.Integer(readonly=True)
    blocked_candidate_count = fields.Integer(readonly=True)

    @api.depends_context("allowed_company_ids")
    def _compute_allowed_company_ids(self):
        for inventory in self:
            inventory.allowed_company_ids = self.env.companies

    def _ensure_mapping_admin(self):
        _ensure_mapping_authority(self.env)
        if any(inventory.company_id not in self.env.companies for inventory in self):
            raise AccessError(_("The selected company is outside your active scope."))
        return True

    @api.model_create_multi
    def create(self, vals_list):
        _ensure_mapping_authority(self.env)
        return super().create(vals_list)

    @api.model
    def search(self, args, offset=0, limit=None, order=None, count=False):
        _ensure_mapping_authority(self.env)
        return super().search(
            args,
            offset=offset,
            limit=limit,
            order=order,
            count=count,
        )

    def read(self, fields=None, load="_classic_read"):
        _ensure_mapping_authority(self.env)
        return super().read(fields=fields, load=load)

    @api.model
    def read_group(
        self,
        domain,
        fields,
        groupby,
        offset=0,
        limit=None,
        orderby=False,
        lazy=True,
    ):
        _ensure_mapping_authority(self.env)
        return super().read_group(
            domain,
            fields,
            groupby,
            offset=offset,
            limit=limit,
            orderby=orderby,
            lazy=lazy,
        )

    def write(self, values):
        _ensure_mapping_authority(self.env)
        return super().write(values)

    def unlink(self):
        _ensure_mapping_authority(self.env)
        return super().unlink()

    @api.model
    def _crm_roster(self, crm_team, snapshot_context=None):
        leader = crm_team.user_id
        if snapshot_context is not None:
            members = snapshot_context["crm_roster_by_team"].get(
                crm_team.id, snapshot_context["user_model"]
            )
            return members - leader, leader
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

    @api.model
    def _binding_snapshot(
        self,
        model_name,
        source_field,
        source,
        crm_team,
        snapshot_context=None,
    ):
        # Team targets are one-to-one, so a target-side binding blocks the pair.
        # Pipeline targets are intentionally shareable and only source bindings
        # belong in that candidate's fingerprint.
        target_bindings_are_relevant = model_name == "contact.center.crm.team.binding"
        if snapshot_context is None:
            domain = [(source_field, "=", source.id)]
            if target_bindings_are_relevant:
                domain = [
                    "|",
                    (source_field, "=", source.id),
                    ("crm_team_id", "=", crm_team.id),
                ]
            bindings = (
                self.env[model_name]
                .sudo()
                .with_context(active_test=False)
                .search(domain, order="id")
            )
        else:
            index = snapshot_context["binding_indices"][model_name]
            binding_ids = list(index["by_source"].get(source.id, ()))
            if target_bindings_are_relevant:
                binding_ids.extend(index["by_target"].get(crm_team.id, ()))
            bindings = index["records"].browse(sorted(set(binding_ids)))
        same = bindings.filtered(
            lambda binding: binding[source_field] == source
            and binding.crm_team_id == crm_team
        )[:1]
        source_other = bindings.filtered(
            lambda binding: binding[source_field] == source
            and binding.crm_team_id != crm_team
        )[:1]
        target_other = bindings.filtered(
            lambda binding: binding.crm_team_id == crm_team
            and binding[source_field] != source
        )[:1]
        return bindings, same, source_other, target_other

    @api.model
    def _binding_index(self, bindings, source_field):
        by_source = {}
        by_target = {}
        for binding in bindings:
            by_source.setdefault(binding[source_field].id, []).append(binding.id)
            by_target.setdefault(binding.crm_team_id.id, []).append(binding.id)
        return {
            "records": bindings,
            "by_source": by_source,
            "by_target": by_target,
        }

    def _candidate_snapshot_context(self, sources, crm_teams):
        """Load every candidate dependency once for one inventory evaluation."""

        self.ensure_one()
        team_sources = sources.get("team", self.env["contact.center.team"])
        pipeline_sources = sources.get("pipeline", self.env["contact.center.pipeline"])
        candidate_team_ids = crm_teams.ids
        relevant_cc_team_ids = sorted(
            set(team_sources.ids) | set(pipeline_sources.mapped("team_ids").ids)
        )
        relevant_pipeline_ids = sorted(
            set(pipeline_sources.ids) | set(team_sources.mapped("pipeline_ids").ids)
        )

        TeamBinding = (
            self.env["contact.center.crm.team.binding"]
            .sudo()
            .with_context(active_test=False)
        )
        target_team_ids = candidate_team_ids if team_sources else []
        if relevant_cc_team_ids or target_team_ids:
            team_bindings = TeamBinding.search(
                [
                    "|",
                    ("contact_center_team_id", "in", relevant_cc_team_ids or [0]),
                    ("crm_team_id", "in", target_team_ids or [0]),
                ],
                order="id",
            )
        else:
            team_bindings = TeamBinding

        PipelineBinding = (
            self.env["contact.center.crm.pipeline.binding"]
            .sudo()
            .with_context(active_test=False)
        )
        pipeline_bindings = (
            PipelineBinding.search(
                [("pipeline_id", "in", relevant_pipeline_ids)], order="id"
            )
            if relevant_pipeline_ids
            else PipelineBinding
        )

        User = self.env["res.users"].sudo().with_context(active_test=False)
        crm_roster_ids_by_team = {}
        crm_member_rows = (
            self.env["crm.team.member"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("crm_team_id", "in", candidate_team_ids),
                    ("active", "=", True),
                ],
                order="id",
            )
            if candidate_team_ids
            else self.env["crm.team.member"].sudo()
        )
        for member in crm_member_rows:
            crm_roster_ids_by_team.setdefault(member.crm_team_id.id, []).append(
                member.user_id.id
            )
        crm_roster_by_team = {
            crm_team_id: User.browse(user_ids)
            for crm_team_id, user_ids in crm_roster_ids_by_team.items()
        }

        CrmStage = self.env["crm.stage"].sudo().with_context(active_test=False)
        all_crm_stages = (
            CrmStage.search(
                [
                    "|",
                    ("team_id", "=", False),
                    ("team_id", "in", candidate_team_ids),
                ],
                order="id",
            )
            if candidate_team_ids
            else CrmStage
        )
        global_crm_stage_ids = all_crm_stages.filtered(
            lambda stage: not stage.team_id
        ).ids
        crm_stage_ids_by_team = {}
        for stage in all_crm_stages.filtered("team_id"):
            crm_stage_ids_by_team.setdefault(stage.team_id.id, []).append(stage.id)
        crm_stages_by_team = {
            crm_team.id: CrmStage.browse(
                sorted(
                    set(global_crm_stage_ids)
                    | set(crm_stage_ids_by_team.get(crm_team.id, ()))
                )
            )
            for crm_team in crm_teams
        }

        LocalStage = (
            self.env["contact.center.pipeline.stage"]
            .sudo()
            .with_context(active_test=False)
        )
        all_local_stages = (
            LocalStage.search([("pipeline_id", "in", pipeline_sources.ids)], order="id")
            if pipeline_sources
            else LocalStage
        )
        local_stage_ids_by_pipeline = {}
        for stage in all_local_stages:
            local_stage_ids_by_pipeline.setdefault(stage.pipeline_id.id, []).append(
                stage.id
            )

        StageBinding = (
            self.env["contact.center.crm.stage.binding"]
            .sudo()
            .with_context(active_test=False)
        )
        stage_bindings = (
            StageBinding.search(
                [("pipeline_binding_id", "in", pipeline_bindings.ids)], order="id"
            )
            if pipeline_bindings
            else StageBinding
        )
        mapped_stage_ids_by_binding = {}
        for stage_binding in stage_bindings:
            mapped_stage_ids_by_binding.setdefault(
                stage_binding.pipeline_binding_id.id, []
            ).append(stage_binding.stage_id.id)

        case_count_by_stage = {}
        revised_case_count_by_stage = {}
        cases = (
            self.env["contact.center.case"]
            .sudo()
            .with_context(active_test=False)
            .search([("stage_id", "in", all_local_stages.ids)])
            if all_local_stages
            else self.env["contact.center.case"].sudo()
        )
        for case in cases:
            stage_id = case.stage_id.id
            case_count_by_stage[stage_id] = case_count_by_stage.get(stage_id, 0) + 1
            if case.stage_revision != 0:
                revised_case_count_by_stage[stage_id] = (
                    revised_case_count_by_stage.get(stage_id, 0) + 1
                )

        shared_inbox_team_ids = (
            set(
                self.env["contact.center.account"]
                .sudo()
                .search(
                    [
                        ("active", "=", True),
                        ("default_team_id", "in", team_sources.ids),
                        ("owner_user_id", "=", False),
                    ]
                )
                .mapped("default_team_id")
                .ids
            )
            if team_sources
            else set()
        )

        active_team_binding_targets_by_source = {}
        for binding in team_bindings.filtered("active"):
            active_team_binding_targets_by_source.setdefault(
                binding.contact_center_team_id.id, set()
            ).add(binding.crm_team_id.id)
        active_pipeline_binding_targets_by_source = {}
        for binding in pipeline_bindings.filtered("active"):
            active_pipeline_binding_targets_by_source.setdefault(
                binding.pipeline_id.id, set()
            ).add(binding.crm_team_id.id)

        return {
            "binding_indices": {
                "contact.center.crm.team.binding": self._binding_index(
                    team_bindings, "contact_center_team_id"
                ),
                "contact.center.crm.pipeline.binding": self._binding_index(
                    pipeline_bindings, "pipeline_id"
                ),
            },
            "user_model": User,
            "crm_roster_by_team": crm_roster_by_team,
            "crm_stage_model": CrmStage,
            "crm_stages_by_team": crm_stages_by_team,
            "local_stage_model": LocalStage,
            "local_stage_ids_by_pipeline": local_stage_ids_by_pipeline,
            "mapped_stage_ids_by_binding": mapped_stage_ids_by_binding,
            "case_count_by_stage": case_count_by_stage,
            "revised_case_count_by_stage": revised_case_count_by_stage,
            "shared_inbox_team_ids": shared_inbox_team_ids,
            "active_team_binding_targets_by_source": (
                active_team_binding_targets_by_source
            ),
            "active_pipeline_binding_targets_by_source": (
                active_pipeline_binding_targets_by_source
            ),
        }

    @api.model
    def _team_evidence(
        self,
        *,
        same,
        overlap,
        cc_users,
        name_match,
        exact_roster,
        leader_match,
        pipeline_match,
    ):
        evidence = []
        if name_match:
            evidence.append(_("Normalized names match (advisory signal only)."))
        if overlap:
            evidence.append(
                _(
                    "%(overlap)s of %(contact_center)s Contact Center roster "
                    "users also occur in the CRM roster.",
                    overlap=len(overlap),
                    contact_center=len(cc_users),
                )
            )
        if exact_roster:
            evidence.append(_("The current non-empty rosters contain the same users."))
        if leader_match:
            evidence.append(
                _("The CRM leader is currently a Contact Center supervisor.")
            )
        if pipeline_match:
            evidence.append(
                _("A pipeline used by this team is already bound to this CRM team.")
            )
        if same:
            evidence.append(
                _(
                    "An %(state)s binding already links this exact pair.",
                    state=_("active") if same.active else _("archived"),
                )
            )
        return evidence

    @api.model
    def _team_blockers(
        self,
        *,
        team,
        crm_team,
        crm_agents,
        crm_leader,
        source_other,
        target_other,
        has_shared_inbox=None,
    ):
        blockers = []
        if not team.active:
            blockers.append(_("The Contact Center team is archived."))
        if not crm_team.active:
            blockers.append(_("The CRM team is archived."))
        if crm_team.company_id and crm_team.company_id != team.company_id:
            blockers.append(_("The records belong to different companies."))
        if source_other:
            blockers.append(
                _(
                    "The Contact Center team already has a binding " "to %(team)s.",
                    team=source_other.crm_team_id.display_name,
                )
            )
        if target_other:
            if target_other.company_id != team.company_id:
                blockers.append(
                    _(
                        "The CRM team already has a Contact Center "
                        "binding outside the active company scope."
                    )
                )
            else:
                blockers.append(
                    _(
                        "The CRM team already has a binding to %(team)s.",
                        team=target_other.contact_center_team_id.display_name,
                    )
                )
        desired_users = crm_agents | crm_leader
        invalid_users = desired_users.filtered(
            lambda user: not user.active
            or user.share
            or team.company_id not in user.company_ids
        )
        if invalid_users:
            blockers.append(
                _(
                    "%(count)s CRM roster users are not eligible internal users "
                    "of the Contact Center company.",
                    count=len(invalid_users),
                )
            )
        if has_shared_inbox is None:
            has_shared_inbox = bool(
                self.env["contact.center.account"]
                .sudo()
                .search_count(
                    [
                        ("active", "=", True),
                        ("default_team_id", "=", team.id),
                        ("owner_user_id", "=", False),
                    ]
                )
            )
        if not desired_users and has_shared_inbox:
            blockers.append(
                _("The CRM roster is empty while this team owns a shared inbox.")
            )
        return blockers

    @api.model
    def _team_candidate_payload(self, team, crm_team, snapshot_context=None):
        bindings, same, source_other, target_other = self._binding_snapshot(
            "contact.center.crm.team.binding",
            "contact_center_team_id",
            team,
            crm_team,
            snapshot_context=snapshot_context,
        )
        crm_agents, crm_leader = self._crm_roster(
            crm_team, snapshot_context=snapshot_context
        )
        cc_users = team.agent_ids | team.supervisor_ids
        crm_users = crm_agents | crm_leader
        overlap = cc_users & crm_users
        exact_roster = bool(cc_users) and set(cc_users.ids) == set(crm_users.ids)
        leader_match = bool(crm_leader and crm_leader in team.supervisor_ids)
        name_match = bool(
            _normalized_label(team.name)
            and _normalized_label(team.name) == _normalized_label(crm_team.name)
        )
        if snapshot_context is None:
            pipeline_match = bool(
                self.env["contact.center.crm.pipeline.binding"]
                .sudo()
                .search_count(
                    [
                        ("pipeline_id", "in", team.pipeline_ids.ids),
                        ("crm_team_id", "=", crm_team.id),
                        ("active", "=", True),
                    ]
                )
            )
        else:
            targets_by_pipeline = snapshot_context[
                "active_pipeline_binding_targets_by_source"
            ]
            pipeline_match = any(
                crm_team.id in targets_by_pipeline.get(pipeline_id, ())
                for pipeline_id in team.pipeline_ids.ids
            )
        anchor = bool(same or name_match or overlap or pipeline_match)

        evidence = self._team_evidence(
            same=same,
            overlap=overlap,
            cc_users=cc_users,
            name_match=name_match,
            exact_roster=exact_roster,
            leader_match=leader_match,
            pipeline_match=pipeline_match,
        )
        blockers = self._team_blockers(
            team=team,
            crm_team=crm_team,
            crm_agents=crm_agents,
            crm_leader=crm_leader,
            source_other=source_other,
            target_other=target_other,
            has_shared_inbox=(
                team.id in snapshot_context["shared_inbox_team_ids"]
                if snapshot_context is not None
                else None
            ),
        )

        score = min(
            100,
            (25 if name_match else 0)
            + (45 if exact_roster else min(len(overlap) * 10, 30))
            + (15 if leader_match else 0)
            + (30 if pipeline_match else 0),
        )
        snapshot = {
            "kind": "team",
            "source": {
                "id": team.id,
                "active": team.active,
                "company_id": team.company_id.id,
                "name": team.name,
                "revision": team.access_topology_revision,
                "agent_ids": sorted(team.agent_ids.ids),
                "supervisor_ids": sorted(team.supervisor_ids.ids),
                "pipeline_ids": sorted(team.pipeline_ids.ids),
            },
            "target": {
                "id": crm_team.id,
                "active": crm_team.active,
                "company_id": crm_team.company_id.id,
                "name": crm_team.name,
                "revision": crm_team.contact_center_roster_revision,
                "leader_id": crm_leader.id,
                "member_ids": sorted(crm_agents.ids),
            },
            "bindings": [(item.id, item.active) for item in bindings],
            "anchor": anchor,
            "blockers": blockers,
            "score": score,
        }
        if same and same.active:
            state = "bound"
        elif blockers:
            state = "blocked"
        elif same:
            state = "reactivable"
        else:
            state = "candidate"
        return {
            "anchor": anchor,
            "state": state,
            "score": score,
            "evidence": "\n".join("- %s" % item for item in evidence)
            or _("No explanatory signal was found."),
            "blockers": "\n".join("- %s" % item for item in blockers),
            "snapshot_hash": _snapshot_hash(snapshot),
            "same_binding": same,
        }

    @api.model
    def _pipeline_candidate_dependencies(
        self, pipeline, crm_team, same, snapshot_context=None
    ):
        if snapshot_context is None:
            team_binding_ids = (
                self.env["contact.center.crm.team.binding"]
                .sudo()
                .search(
                    [
                        ("contact_center_team_id", "in", pipeline.team_ids.ids),
                        ("active", "=", True),
                    ]
                )
                .mapped("crm_team_id")
                .ids
            )
            crm_stages = (
                self.env["crm.stage"]
                .sudo()
                .search(
                    ["|", ("team_id", "=", False), ("team_id", "=", crm_team.id)],
                    order="sequence, name, id",
                )
            )
            all_crm_stages = (
                self.env["crm.stage"]
                .sudo()
                .with_context(active_test=False)
                .search(
                    ["|", ("team_id", "=", False), ("team_id", "=", crm_team.id)],
                    order="id",
                )
            )
            all_local_stages = (
                self.env["contact.center.pipeline.stage"]
                .sudo()
                .with_context(active_test=False)
                .search([("pipeline_id", "=", pipeline.id)], order="id")
            )
            mapped_stages = (
                same.stage_binding_ids.mapped("stage_id")
                if same
                else self.env["contact.center.pipeline.stage"]
            )
        else:
            targets_by_team = snapshot_context["active_team_binding_targets_by_source"]
            team_binding_ids = {
                target_id
                for team_id in pipeline.team_ids.ids
                for target_id in targets_by_team.get(team_id, ())
            }
            all_crm_stages = snapshot_context["crm_stages_by_team"].get(
                crm_team.id, snapshot_context["crm_stage_model"]
            )
            crm_stages = all_crm_stages.filtered("active")
            all_local_stages = snapshot_context["local_stage_model"].browse(
                snapshot_context["local_stage_ids_by_pipeline"].get(pipeline.id, ())
            )
            mapped_stages = (
                snapshot_context["local_stage_model"].browse(
                    snapshot_context["mapped_stage_ids_by_binding"].get(same.id, ())
                )
                if same
                else snapshot_context["local_stage_model"]
            )

        unbound_stages = all_local_stages - mapped_stages
        local_initial_ids = set(unbound_stages.filtered("is_initial").ids)
        if snapshot_context is None:
            non_transitionable_count = len(
                self.env["contact.center.case"]
                .sudo()
                .with_context(active_test=False)
                .search([("stage_id", "in", unbound_stages.ids)])
                .filtered(
                    lambda case: case.stage_revision != 0
                    or case.stage_id.id not in local_initial_ids
                )
            )
        else:
            case_count_by_stage = snapshot_context["case_count_by_stage"]
            revised_case_count_by_stage = snapshot_context[
                "revised_case_count_by_stage"
            ]
            non_transitionable_count = sum(
                (
                    revised_case_count_by_stage.get(stage.id, 0)
                    if stage.id in local_initial_ids
                    else case_count_by_stage.get(stage.id, 0)
                )
                for stage in unbound_stages
            )
        return {
            "team_binding_ids": team_binding_ids,
            "crm_stages": crm_stages,
            "all_crm_stages": all_crm_stages,
            "all_local_stages": all_local_stages,
            "non_transitionable_count": non_transitionable_count,
        }

    @api.model
    def _pipeline_candidate_payload(self, pipeline, crm_team, snapshot_context=None):
        bindings, same, source_other, __target_other = self._binding_snapshot(
            "contact.center.crm.pipeline.binding",
            "pipeline_id",
            pipeline,
            crm_team,
            snapshot_context=snapshot_context,
        )
        dependencies = self._pipeline_candidate_dependencies(
            pipeline,
            crm_team,
            same,
            snapshot_context=snapshot_context,
        )
        team_match = crm_team.id in dependencies["team_binding_ids"]
        name_match = bool(
            _normalized_label(pipeline.name)
            and _normalized_label(pipeline.name) == _normalized_label(crm_team.name)
        )
        anchor = bool(same or team_match or name_match)
        crm_stages = dependencies["crm_stages"]
        all_crm_stages = dependencies["all_crm_stages"]
        all_local_stages = dependencies["all_local_stages"]
        local_stage_names = {
            _normalized_label(stage.name)
            for stage in all_local_stages.filtered("active")
            if _normalized_label(stage.name)
        }
        crm_stage_names = {
            _normalized_label(stage.name)
            for stage in crm_stages
            if _normalized_label(stage.name)
        }
        stage_overlap = local_stage_names & crm_stage_names

        evidence = []
        if name_match:
            evidence.append(_("Normalized names match (advisory signal only)."))
        if team_match:
            evidence.append(
                _(
                    "A Contact Center team using this pipeline is bound to this CRM team."
                )
            )
        if stage_overlap:
            evidence.append(
                _(
                    "%(count)s normalized stage names overlap; stage names are "
                    "supporting evidence only.",
                    count=len(stage_overlap),
                )
            )
        if same:
            evidence.append(
                _(
                    "An %(state)s binding already links this exact pair.",
                    state=_("active") if same.active else _("archived"),
                )
            )

        blockers = []
        if not pipeline.active:
            blockers.append(_("The Contact Center pipeline is archived."))
        if not crm_team.active:
            blockers.append(_("The CRM team is archived."))
        if crm_team.company_id and crm_team.company_id != pipeline.company_id:
            blockers.append(_("The records belong to different companies."))
        if source_other:
            blockers.append(
                _(
                    "The Contact Center pipeline already has a binding " "to %(team)s.",
                    team=source_other.crm_team_id.display_name,
                )
            )
        if not crm_stages:
            blockers.append(_("The CRM team has no effective stages."))
        elif not crm_stages.filtered(lambda stage: not stage.is_won):
            blockers.append(_("The CRM team has no effective non-won stage."))

        non_transitionable_count = dependencies["non_transitionable_count"]
        if non_transitionable_count:
            blockers.append(
                _(
                    "%(count)s cases have local stage history that cannot be "
                    "transitioned automatically.",
                    count=non_transitionable_count,
                )
            )

        score = min(
            100,
            (35 if name_match else 0)
            + (55 if team_match else 0)
            + min(len(stage_overlap) * 5, 20),
        )
        snapshot = {
            "kind": "pipeline",
            "source": {
                "id": pipeline.id,
                "active": pipeline.active,
                "company_id": pipeline.company_id.id,
                "name": pipeline.name,
                "write_date": fields.Datetime.to_string(pipeline.write_date),
                "team_ids": sorted(pipeline.team_ids.ids),
                "stages": [
                    (
                        stage.id,
                        stage.active,
                        stage.name,
                        stage.sequence,
                        stage.is_initial,
                        stage.is_closed,
                        fields.Datetime.to_string(stage.write_date),
                    )
                    for stage in all_local_stages
                ],
            },
            "target": {
                "id": crm_team.id,
                "active": crm_team.active,
                "company_id": crm_team.company_id.id,
                "name": crm_team.name,
                "write_date": fields.Datetime.to_string(crm_team.write_date),
                "stages": [
                    (
                        stage.id,
                        stage.active,
                        stage.name,
                        stage.sequence,
                        stage.fold,
                        stage.is_won,
                        fields.Datetime.to_string(stage.write_date),
                    )
                    for stage in all_crm_stages
                ],
            },
            "bindings": [(item.id, item.active) for item in bindings],
            "anchor": anchor,
            "blockers": blockers,
            "score": score,
        }
        if same and same.active:
            state = "bound"
        elif blockers:
            state = "blocked"
        elif same:
            state = "reactivable"
        else:
            state = "candidate"
        return {
            "anchor": anchor,
            "state": state,
            "score": score,
            "evidence": "\n".join("- %s" % item for item in evidence)
            or _("No explanatory signal was found."),
            "blockers": "\n".join("- %s" % item for item in blockers),
            "snapshot_hash": _snapshot_hash(snapshot),
            "same_binding": same,
        }

    def _candidate_crm_teams(self):
        self.ensure_one()
        domain = [
            "|",
            ("company_id", "=", False),
            ("company_id", "=", self.company_id.id),
        ]
        if not self.include_inactive:
            domain.append(("active", "=", True))
        return (
            self.env["crm.team"]
            .sudo()
            .with_context(active_test=False)
            .search(domain, order="name, id")
        )

    def _source_records(self, model_name):
        self.ensure_one()
        domain = [("company_id", "=", self.company_id.id)]
        if not self.include_inactive:
            domain.append(("active", "=", True))
        return (
            self.env[model_name]
            .sudo()
            .with_context(active_test=False)
            .search(domain, order="name, id")
        )

    def _line_values(
        self,
        kind,
        source,
        crm_team,
        payload,
        candidate_count,
        candidate_set_hash,
    ):
        return {
            "mapping_kind": kind,
            "company_id": source.company_id.id,
            "contact_center_team_id": source.id if kind == "team" else False,
            "pipeline_id": source.id if kind == "pipeline" else False,
            "crm_team_id": crm_team.id if crm_team else False,
            "candidate_state": payload["state"],
            "candidate_count": candidate_count,
            "score": payload.get("score", 0),
            "evidence": payload.get("evidence"),
            "blockers": payload.get("blockers"),
            "snapshot_hash": payload.get("snapshot_hash"),
            "candidate_set_hash": candidate_set_hash,
        }

    def _candidate_set_snapshot(
        self, kind, source, crm_teams=None, snapshot_context=None
    ):
        """Return anchored candidates and a stable digest of the complete set."""

        self.ensure_one()
        crm_teams = crm_teams if crm_teams is not None else self._candidate_crm_teams()
        if snapshot_context is None:
            snapshot_context = self._candidate_snapshot_context(
                {
                    "team": (
                        source if kind == "team" else self.env["contact.center.team"]
                    ),
                    "pipeline": (
                        source
                        if kind == "pipeline"
                        else self.env["contact.center.pipeline"]
                    ),
                },
                crm_teams,
            )
        payload_method = (
            self._team_candidate_payload
            if kind == "team"
            else self._pipeline_candidate_payload
        )
        candidates = []
        for crm_team in crm_teams:
            payload = payload_method(
                source, crm_team, snapshot_context=snapshot_context
            )
            if payload["anchor"]:
                candidates.append((crm_team, payload))
        candidate_set_hash = _snapshot_hash(
            {
                "kind": kind,
                "source_id": source.id,
                "candidates": [
                    (crm_team.id, payload["state"], payload["snapshot_hash"])
                    for crm_team, payload in candidates
                ],
            }
        )
        return candidates, candidate_set_hash

    def _inventory_values_for_source(
        self, kind, source, crm_teams, snapshot_context=None
    ):
        candidates, candidate_set_hash = self._candidate_set_snapshot(
            kind, source, crm_teams, snapshot_context=snapshot_context
        )
        if not candidates:
            payload = {
                "state": "no_candidate",
                "score": 0,
                "evidence": _(
                    "No CRM team has an explanatory name, roster, or existing "
                    "binding signal. Configure a binding manually if the business "
                    "mapping is nevertheless known."
                ),
                "blockers": False,
                "snapshot_hash": False,
            }
            return [
                self._line_values(kind, source, False, payload, 0, candidate_set_hash)
            ]

        actionable = [
            payload
            for __crm_team, payload in candidates
            if payload["state"] in {"candidate", "reactivable"}
        ]
        candidate_count = len(actionable)
        result = []
        for crm_team, payload in candidates:
            payload = dict(payload)
            if payload["state"] == "candidate" and candidate_count > 1:
                payload["state"] = "ambiguous"
            result.append(
                self._line_values(
                    kind,
                    source,
                    crm_team,
                    payload,
                    candidate_count,
                    candidate_set_hash,
                )
            )
        return result

    def action_refresh(self):
        self.ensure_one()
        self._ensure_mapping_admin()
        crm_teams = self._candidate_crm_teams()
        values = []
        sources = {
            "team": self._source_records("contact.center.team"),
            "pipeline": self._source_records("contact.center.pipeline"),
        }
        snapshot_context = self._candidate_snapshot_context(sources, crm_teams)
        for kind, records in sources.items():
            for source in records:
                values.extend(
                    self._inventory_values_for_source(
                        kind,
                        source,
                        crm_teams,
                        snapshot_context=snapshot_context,
                    )
                )
        self.line_ids.unlink()
        lines = self.env["contact.center.crm.mapping.inventory.line"].create(
            [{"inventory_id": self.id, **item} for item in values]
        )
        ambiguous_sources = {
            (line.mapping_kind, line.contact_center_team_id.id or line.pipeline_id.id)
            for line in lines.filtered(lambda line: line.candidate_state == "ambiguous")
        }
        self.write(
            {
                "generated_at": fields.Datetime.now(),
                "source_count": sum(len(records) for records in sources.values()),
                "candidate_count": len(
                    lines.filtered(
                        lambda line: line.candidate_state
                        in {"candidate", "ambiguous", "reactivable"}
                    )
                ),
                "ambiguous_source_count": len(ambiguous_sources),
                "blocked_candidate_count": len(
                    lines.filtered(lambda line: line.candidate_state == "blocked")
                ),
            }
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("CRM Mapping Candidates"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }


class ContactCenterCrmMappingInventoryLine(models.TransientModel):
    _name = "contact.center.crm.mapping.inventory.line"
    _description = "Contact Center CRM Mapping Candidate Evidence"
    _order = "mapping_kind, contact_center_team_id, pipeline_id, score desc, id"

    inventory_id = fields.Many2one(
        "contact.center.crm.mapping.inventory",
        required=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    mapping_kind = fields.Selection(
        [("team", "Team Roster"), ("pipeline", "Pipeline Catalog")],
        required=True,
        readonly=True,
    )
    contact_center_team_id = fields.Many2one("contact.center.team", readonly=True)
    pipeline_id = fields.Many2one("contact.center.pipeline", readonly=True)
    source_name = fields.Char(compute="_compute_source_name")
    crm_team_id = fields.Many2one("crm.team", readonly=True)
    candidate_state = fields.Selection(
        [
            ("no_candidate", "No Candidate"),
            ("candidate", "Candidate"),
            ("ambiguous", "Ambiguous"),
            ("reactivable", "Archived Binding"),
            ("bound", "Already Bound"),
            ("blocked", "Invalid / Blocked"),
        ],
        required=True,
        readonly=True,
    )
    candidate_count = fields.Integer(readonly=True)
    score = fields.Integer(readonly=True)
    evidence = fields.Text(readonly=True)
    blockers = fields.Text(readonly=True)
    snapshot_hash = fields.Char(readonly=True)
    candidate_set_hash = fields.Char(readonly=True)
    snapshot_validity = fields.Selection(
        [
            ("not_applicable", "Not Applicable"),
            ("current", "Current"),
            ("stale", "Refresh Required"),
            ("invalid", "No Longer Valid"),
        ],
        compute="_compute_snapshot_validity",
    )
    can_accept = fields.Boolean(compute="_compute_snapshot_validity")

    @api.model_create_multi
    def create(self, vals_list):
        _ensure_mapping_authority(self.env)
        return super().create(vals_list)

    @api.model
    def search(self, args, offset=0, limit=None, order=None, count=False):
        _ensure_mapping_authority(self.env)
        return super().search(
            args,
            offset=offset,
            limit=limit,
            order=order,
            count=count,
        )

    def read(self, fields=None, load="_classic_read"):
        _ensure_mapping_authority(self.env)
        return super().read(fields=fields, load=load)

    @api.model
    def read_group(
        self,
        domain,
        fields,
        groupby,
        offset=0,
        limit=None,
        orderby=False,
        lazy=True,
    ):
        _ensure_mapping_authority(self.env)
        return super().read_group(
            domain,
            fields,
            groupby,
            offset=offset,
            limit=limit,
            orderby=orderby,
            lazy=lazy,
        )

    def write(self, values):
        _ensure_mapping_authority(self.env)
        return super().write(values)

    def unlink(self):
        _ensure_mapping_authority(self.env)
        return super().unlink()

    @api.depends("mapping_kind", "contact_center_team_id", "pipeline_id")
    def _compute_source_name(self):
        for line in self:
            source = (
                line.contact_center_team_id
                if line.mapping_kind == "team"
                else line.pipeline_id
            )
            line.source_name = source.display_name

    def _current_payload(self):
        self.ensure_one()
        source = (
            self.contact_center_team_id
            if self.mapping_kind == "team"
            else self.pipeline_id
        )
        if not source.exists() or not self.crm_team_id.exists():
            return False
        inventory = self.inventory_id
        return (
            inventory._team_candidate_payload(source, self.crm_team_id)
            if self.mapping_kind == "team"
            else inventory._pipeline_candidate_payload(source, self.crm_team_id)
        )

    def _current_candidate_set_hash(self):
        self.ensure_one()
        source = (
            self.contact_center_team_id
            if self.mapping_kind == "team"
            else self.pipeline_id
        )
        if not source.exists():
            return False
        __candidates, candidate_set_hash = self.inventory_id._candidate_set_snapshot(
            self.mapping_kind, source
        )
        return candidate_set_hash

    @api.depends(
        "candidate_state",
        "snapshot_hash",
        "candidate_set_hash",
        "contact_center_team_id",
        "pipeline_id",
        "crm_team_id",
    )
    def _compute_snapshot_validity(self):
        for line in self:
            line.can_accept = False
            line.snapshot_validity = "not_applicable"
        relevant_lines = self.filtered(
            lambda line: line.crm_team_id and line.snapshot_hash
        )
        for inventory in relevant_lines.mapped("inventory_id"):
            inventory_lines = relevant_lines.filtered(
                lambda line: line.inventory_id == inventory
            )
            sources = {
                "team": inventory_lines.mapped("contact_center_team_id").exists(),
                "pipeline": inventory_lines.mapped("pipeline_id").exists(),
            }
            crm_teams = inventory._candidate_crm_teams()
            snapshot_context = inventory._candidate_snapshot_context(sources, crm_teams)
            current_by_source = {}
            for kind, records in sources.items():
                for source in records:
                    candidates, candidate_set_hash = inventory._candidate_set_snapshot(
                        kind,
                        source,
                        crm_teams,
                        snapshot_context=snapshot_context,
                    )
                    current_by_source[(kind, source.id)] = (
                        {crm_team.id: payload for crm_team, payload in candidates},
                        candidate_set_hash,
                    )
            for line in inventory_lines:
                source = (
                    line.contact_center_team_id
                    if line.mapping_kind == "team"
                    else line.pipeline_id
                )
                current = current_by_source.get((line.mapping_kind, source.id))
                payload = current[0].get(line.crm_team_id.id) if current else False
                if not payload or payload["state"] == "blocked":
                    line.snapshot_validity = "invalid"
                    continue
                if payload["snapshot_hash"] != line.snapshot_hash:
                    line.snapshot_validity = "stale"
                    continue
                if current[1] != line.candidate_set_hash:
                    line.snapshot_validity = "stale"
                    continue
                line.snapshot_validity = "current"
                line.can_accept = line.candidate_state in {
                    "candidate",
                    "ambiguous",
                    "reactivable",
                }

    def _lock_source(self):
        self.ensure_one()
        if self.mapping_kind == "team":
            table = "contact_center_team"
            source_id = self.contact_center_team_id.id
        else:
            table = "contact_center_pipeline"
            source_id = self.pipeline_id.id
        self.env.cr.execute(
            "SELECT id FROM %s WHERE id = %%s FOR UPDATE" % table,
            [source_id],
        )

    def _lock_authorities(self):
        """Fence catalog/binding authorities before the Contact Center source."""

        self.ensure_one()
        source_team_ids = (
            self.contact_center_team_id.ids
            if self.mapping_kind == "team"
            else self.pipeline_id.team_ids.ids
        )
        source_pipeline_ids = (
            self.contact_center_team_id.pipeline_ids.ids
            if self.mapping_kind == "team"
            else self.pipeline_id.ids
        )
        lock_binding_graph(
            self.env,
            team_ids=source_team_ids,
            pipeline_ids=source_pipeline_ids,
            crm_team_ids=self.crm_team_id.ids,
            # Revalidation compares the captured roster revision.  Acquire the
            # row lock without changing that revision; the acceptance barrier
            # publishes exactly one revision after validation.
            touch=False,
        )
        self._lock_source()
        return self.with_context(
            contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
        )

    def _existing_binding(self):
        self.ensure_one()
        if self.mapping_kind == "team":
            model_name = "contact.center.crm.team.binding"
            source_field = "contact_center_team_id"
            source = self.contact_center_team_id
        else:
            model_name = "contact.center.crm.pipeline.binding"
            source_field = "pipeline_id"
            source = self.pipeline_id
        return (
            self.env[model_name]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    (source_field, "=", source.id),
                    ("crm_team_id", "=", self.crm_team_id.id),
                ],
                limit=1,
            )
        )

    def _binding_action(self, binding):
        self.ensure_one()
        if self.mapping_kind == "team":
            view = self.env.ref("contact_center_kanban.view_cc_crm_team_binding_form")
        else:
            view = self.env.ref(
                "contact_center_kanban.view_cc_crm_pipeline_binding_form"
            )
        return {
            "type": "ir.actions.act_window",
            "name": binding.display_name,
            "res_model": binding._name,
            "res_id": binding.id,
            "views": [(view.id, "form")],
            "view_mode": "form",
            "target": "current",
        }

    def _create_binding(self):
        self.ensure_one()
        if self.mapping_kind == "team":
            return self.env["contact.center.crm.team.binding"].create(
                {
                    "contact_center_team_id": self.contact_center_team_id.id,
                    "crm_team_id": self.crm_team_id.id,
                }
            )
        return self.env["contact.center.crm.pipeline.binding"].create(
            {
                "pipeline_id": self.pipeline_id.id,
                "crm_team_id": self.crm_team_id.id,
            }
        )

    def _create_binding_idempotently(self):
        """Turn a concurrent unique-key race into an idempotent result."""

        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                return self._create_binding()
        except IntegrityError as error:
            binding = self._existing_binding()
            if binding and binding.active:
                return binding
            raise ValidationError(
                _(
                    "The binding changed concurrently. Refresh the inventory "
                    "before accepting it."
                )
            ) from error

    def _revalidate_candidate(self):
        """Validate both the selected pair and its complete candidate set."""

        self.ensure_one()
        payload = self._current_payload()
        if not payload or not payload["anchor"] or payload["state"] == "blocked":
            raise ValidationError(
                _("This candidate is no longer valid. Refresh the inventory.")
            )
        if payload["snapshot_hash"] != self.snapshot_hash:
            raise ValidationError(
                _("This candidate changed after the scan. Refresh before accepting it.")
            )
        if self._current_candidate_set_hash() != self.candidate_set_hash:
            raise ValidationError(
                _(
                    "The candidate set changed after the scan. Refresh before "
                    "accepting it."
                )
            )
        return payload

    def _write_acceptance_barrier(self):
        """Fence concurrent accepts after scan validation.

        The CRM row is already locked first. Updating its roster revision makes
        a waiter with an older REPEATABLE READ snapshot fail serialization, so
        Odoo retries the complete request instead of relying on stale visibility.
        """

        self.ensure_one()
        crm_team = self.crm_team_id
        crm_team.flush_model(["contact_center_roster_revision"])
        self.env.cr.execute(
            "UPDATE crm_team SET contact_center_roster_revision = "
            "contact_center_roster_revision + 1 WHERE id = %s",
            [crm_team.id],
        )
        crm_team.invalidate_recordset(["contact_center_roster_revision"])
        return True

    def action_accept_candidate(self):
        self.ensure_one()
        self.inventory_id._ensure_mapping_admin()
        if self.company_id != self.inventory_id.company_id:
            raise AccessError(_("The candidate is outside the inventory company."))
        line = self._lock_authorities()
        existing = line._existing_binding()
        if existing and existing.active:
            return line._binding_action(existing)

        line._revalidate_candidate()
        line._write_acceptance_barrier()

        if existing:
            existing.with_user(line.env.user).with_context(
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN
            ).write({"active": True})
            binding = existing
        else:
            binding = line._create_binding_idempotently()
        return line._binding_action(binding)
