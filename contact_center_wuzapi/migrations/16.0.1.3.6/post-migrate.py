from odoo import SUPERUSER_ID, api
from odoo.exceptions import ValidationError


def migrate(cr, version):
    """Remove only retired catalog metadata; never rewrite connections."""
    env = api.Environment(cr, SUPERUSER_ID, {"active_test": False})
    events = env["contact.center.wuzapi.webhook.event"].search(
        [("technical_name", "=", "IdentityChange")]
    )
    if env["contact.center.provider.connection"].search_count(
        [("wuzapi_webhook_event_ids", "in", events.ids)]
    ):
        raise ValidationError("Retired identity subscriptions still exist.")
    env["ir.model.data"].search(
        [
            ("module", "=", "contact_center_wuzapi"),
            ("name", "=", "webhook_event_identity_change"),
            ("model", "=", "contact.center.wuzapi.webhook.event"),
        ]
    ).unlink()
    events.unlink()
