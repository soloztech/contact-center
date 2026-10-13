from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    view = env.ref(
        "contact_center_base.view_contact_center_account_ad_origin",
        raise_if_not_found=False,
    )
    if view and "action_backfill_ad_previews" in view.arch_db:
        # The module model is not registered in pre-migration. Disable the old
        # extension until its replacement XML loads with the model available.
        view.write({"active": False})
