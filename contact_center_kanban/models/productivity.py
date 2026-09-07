from odoo import _, api, models
from odoo.exceptions import ValidationError


class MailActivityKanban(models.Model):
    _inherit = "mail.activity"

    @api.model
    def _contact_center_reject_case_activity(self, values):
        if values.get("res_model") == "contact.center.case" or values.get(
            "res_model_id"
        ) == self.env["ir.model"]._get_id("contact.center.case"):
            raise ValidationError(
                _("Create the follow-up in the Contact Center conversation.")
            )

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get(["res_model", "res_model_id"])
        for values in vals_list:
            target = dict(defaults, **values)
            if "res_model" not in values:
                target.pop("res_model", None)
            self._contact_center_reject_case_activity(target)
        return super().create(vals_list)

    def write(self, values):
        for activity in self:
            target = {"res_model_id": activity.res_model_id.id}
            self._contact_center_reject_case_activity(dict(target, **values))
        return super().write(values)


class MailChannelKanbanProductivity(models.Model):
    _inherit = "mail.channel"

    def _contact_center_reconcile_members(
        self, partner_ids=None, guest_ids=None, allow_empty=False
    ):
        """Revoke stale internal followers on optional cases."""

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
            if not cases:
                continue
            # Only internal-user followers are access projections.  A business
            # contact deliberately following a case is not removed here.
            internal_followers = cases.message_follower_ids.filtered(
                lambda follower: bool(
                    follower.partner_id.with_context(
                        active_test=False
                    ).user_ids.filtered(lambda user: not user.share)
                )
            )
            internal_followers.filtered(
                lambda follower: follower.partner_id.id not in allowed_partner_ids
            ).sudo().unlink()
        return result
