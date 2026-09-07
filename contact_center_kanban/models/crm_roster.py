from odoo import api, models

from .binding import CRM_BINDING_GRAPH_LOCK_TOKEN, lock_binding_graph


class CrmTeamMember(models.Model):
    _inherit = "crm.team.member"

    @api.model
    def _contact_center_lock_team_ids(self, team_ids):
        """Fence catalogs/bindings before touching their CRM roster rows."""

        team_ids = sorted({team_id for team_id in team_ids if team_id})
        if team_ids:
            lock_binding_graph(
                self.env,
                crm_team_ids=team_ids,
                touch=True,
            )
        teams = (
            self.env["crm.team"]
            .sudo()
            .with_context(
                active_test=False,
                contact_center_crm_binding_graph_lock=(CRM_BINDING_GRAPH_LOCK_TOKEN),
            )
            .browse(team_ids)
        )
        return teams

    @api.model
    def _contact_center_active_team_ids_for_users(self, user_ids):
        if not user_ids:
            return []
        return (
            self.sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("user_id", "in", list(set(user_ids))),
                    ("active", "=", True),
                ]
            )
            .mapped("crm_team_id")
            .ids
        )

    @api.model_create_multi
    def create(self, vals_list):
        user_ids = [values.get("user_id") for values in vals_list]
        team_ids = [values.get("crm_team_id") for values in vals_list]
        team_ids += self._contact_center_active_team_ids_for_users(user_ids)
        affected_teams = self._contact_center_lock_team_ids(team_ids)
        memberships = super().create(vals_list)
        affected_teams |= memberships.crm_team_id.sudo().with_context(
            active_test=False,
            contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN,
        )
        affected_teams._contact_center_sync_bound_rosters()
        return memberships

    def write(self, values):
        roster_fields = {"crm_team_id", "user_id", "active"} & set(values)
        affected_teams = self.env["crm.team"]
        if roster_fields:
            user_ids = self.user_id.ids
            if values.get("user_id"):
                user_ids.append(values["user_id"])
            team_ids = self.crm_team_id.ids
            if values.get("crm_team_id"):
                team_ids.append(values["crm_team_id"])
            if values.get("active") is True or "user_id" in values:
                team_ids += self._contact_center_active_team_ids_for_users(user_ids)
            affected_teams = self._contact_center_lock_team_ids(team_ids)
        result = super().write(values)
        if roster_fields:
            affected_teams |= self.crm_team_id.sudo().with_context(
                active_test=False,
                contact_center_crm_binding_graph_lock=CRM_BINDING_GRAPH_LOCK_TOKEN,
            )
            affected_teams._contact_center_sync_bound_rosters()
        return result

    def unlink(self):
        affected_teams = self._contact_center_lock_team_ids(self.crm_team_id.ids)
        result = super().unlink()
        affected_teams._contact_center_sync_bound_rosters()
        return result
