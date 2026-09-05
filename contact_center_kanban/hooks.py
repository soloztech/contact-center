from odoo import SUPERUSER_ID, api


def post_init_hook(cr, registry):
    """Provision service pipelines only when the optional addon is installed."""

    env = api.Environment(cr, SUPERUSER_ID, {})
    env["contact.center.pipeline"]._contact_center_provision_defaults()
