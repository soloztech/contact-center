from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env.ref("contact_center_base.ir_cron_contact_center_payload_expiry").write(
        {"interval_number": 1, "interval_type": "days"}
    )
