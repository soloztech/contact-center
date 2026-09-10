from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from ..services.tokens import CONTACT_CENTER_PRODUCTIVITY_TOKEN


class MailShortcodeContactCenter(models.Model):
    _inherit = "mail.shortcode"

    contact_center_binding_ids = fields.One2many(
        "contact.center.quick.reply.binding",
        "shortcode_id",
        string="Contact Center Scopes",
        readonly=True,
        context={"active_test": False},
    )

    @api.model_create_multi
    def create(self, values_list):
        if any("contact_center_binding_ids" in values for values in values_list):
            raise AccessError(
                _("Manage Contact Center scopes from the quick reply form.")
            )
        return super().create(values_list)

    def _contact_center_check_content_management(self):
        """A native RPC must not bypass the audience/ownership controls."""
        if self.env.su:
            return
        user = self.env.user
        bindings = (
            self.env["contact.center.quick.reply.binding"]
            .sudo()
            .with_context(active_test=False)
            .search([("shortcode_id", "in", self.ids)])
        )
        bindings.with_env(self.env)._check_management()
        if user.has_group(
            "contact_center_base.group_contact_center_agent"
        ) and not user.has_group("contact_center_base.group_contact_center_admin"):
            unbound = self - bindings.shortcode_id
            if any(shortcode.create_uid != user for shortcode in unbound):
                raise AccessError(
                    _("You can only modify your own native quick replies.")
                )

    def write(self, values):
        if "contact_center_binding_ids" in values:
            raise AccessError(
                _("Manage Contact Center scopes from the quick reply form.")
            )
        self._contact_center_check_content_management()
        return super().write(values)

    def unlink(self):
        # The binding already checked the personal owner before deleting its
        # orphaned content. Native create_uid may belong to an administrator who
        # originally prepared the reply for that owner. Keep normal unlink ACLs.
        personal_cleanup = self.env.context.get(
            "contact_center_personal_reply_deletion_token"
        ) is CONTACT_CENTER_PRODUCTIVITY_TOKEN and not self.env[
            "contact.center.quick.reply.binding"
        ].sudo().with_context(
            active_test=False
        ).search_count(
            [("shortcode_id", "in", self.ids)]
        )
        if not personal_cleanup:
            self._contact_center_check_content_management()
        return super().unlink()
