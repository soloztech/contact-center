from odoo import SUPERUSER_ID, api


def post_init_hook(cr, registry):
    """Provision the neutral service pipeline after a fresh installation."""

    env = api.Environment(cr, SUPERUSER_ID, {})
    env["contact.center.pipeline"]._contact_center_provision_defaults()
