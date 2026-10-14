from odoo.exceptions import ValidationError


def migrate(cr, version):
    """Refuse legacy content/configuration before the first changed module."""
    cr.execute(
        "SELECT 1 FROM contact_center_message_binding WHERE content_type = %s LIMIT 1",
        ["identity.security.changed"],
    )
    if cr.fetchone():
        raise ValidationError(
            "Retired identity notices must be selectively removed before upgrading "
            "Contact Center. Preserve their business references during cleanup."
        )
    cr.execute(
        "SELECT to_regclass(%s), to_regclass(%s)",
        [
            "contact_center_wuzapi_webhook_event",
            "contact_center_wuzapi_connection_event_rel",
        ],
    )
    if not all(cr.fetchone()):
        return
    cr.execute(
        """SELECT 1
             FROM contact_center_wuzapi_connection_event_rel AS selection
             JOIN contact_center_wuzapi_webhook_event AS event
               ON event.id = selection.event_id
            WHERE event.technical_name = %s LIMIT 1""",
        ["IdentityChange"],
    )
    if cr.fetchone():
        raise ValidationError(
            "Retired identity subscriptions must be removed and verified through "
            "the existing webhook configuration before upgrading Contact Center."
        )
