from odoo import api, models
from odoo.osv import expression


class ResUsers(models.Model):
    _inherit = "res.users"

    @api.model
    def systray_get_activities(self):
        groups = super().systray_get_activities()
        channel_group = next(
            (group for group in groups if group["model"] == "mail.channel"), None
        )
        if not channel_group:
            return groups
        activities = self.env["mail.activity"].search(
            [("user_id", "=", self.env.uid), ("res_model", "=", "mail.channel")]
        )
        channels = self.env["mail.channel"].search(
            [("id", "in", activities.mapped("res_id"))]
        )
        native_ids = set(
            channels.filtered(
                lambda channel: channel.channel_type != "contact_center"
            ).ids
        )
        contact_ids = set(
            self.env["mail.channel"]
            .search(
                expression.AND(
                    [
                        [("id", "in", channels.ids)],
                        self.env["contact.center.ui.api"]._conversation_list_domain({}),
                    ]
                )
            )
            .ids
        )
        native_activities = activities.filtered(
            lambda activity: activity.res_id in native_ids
        )
        contact_activities = activities.filtered(
            lambda activity: activity.res_id in contact_ids
        )
        groups = [group for group in groups if group is not channel_group]
        if native_activities:
            groups.append(
                dict(
                    channel_group,
                    domain=expression.AND(
                        [
                            channel_group.get("domain") or [],
                            [("channel_type", "!=", "contact_center")],
                        ]
                    ),
                    **self._contact_center_activity_counts(native_activities),
                )
            )
        if contact_activities:
            model = self.env["ir.model"]._get("contact.center.followup.request")
            groups.append(
                {
                    "id": model.id,
                    "model": model.model,
                    "name": "Contact Center",
                    "type": "activity",
                    "contact_center": True,
                    "icon": "/contact_center_base/static/description/icon.png",
                    "actions": [{"icon": "fa-clock-o", "name": "Contact Center"}],
                    **self._contact_center_activity_counts(contact_activities),
                }
            )
        return groups

    @api.model
    def _contact_center_activity_counts(self, activities):
        counts = {
            "total_count": 0,
            "today_count": 0,
            "overdue_count": 0,
            "planned_count": 0,
        }
        for activity in activities:
            counts[f"{activity.state}_count"] += 1
            if activity.state in ("today", "overdue"):
                counts["total_count"] += 1
        return counts
