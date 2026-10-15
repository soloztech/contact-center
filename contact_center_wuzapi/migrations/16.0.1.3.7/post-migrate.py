from odoo import SUPERUSER_ID, api

# Migrations are loaded by file path, outside the addon's Python package.
# pylint: disable-next=odoo-addons-relative-import
from odoo.addons.contact_center_wuzapi.models.provider_connection import (
    _WUZAPI_OWN_IDENTITY_TOKEN,
)


def migrate(cr, version):
    """Reacquire old proofs once before switching to reuse without an age limit."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["contact.center.provider.connection"].with_context(
        active_test=False, wuzapi_own_identity_token=_WUZAPI_OWN_IDENTITY_TOKEN
    ).search([("adapter_key", "=", "wuzapi")]).write(
        {"wuzapi_own_identity_json": False}
    )
