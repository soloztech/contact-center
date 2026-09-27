"""Record the lifecycle baseline of every conversation that existed before."""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if version:
        api.Environment(cr, SUPERUSER_ID, {})[
            "contact.center.conversation.event"
        ]._contact_center_record_migration_baselines()
