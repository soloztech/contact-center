"""Preserve proven human periods, including upgrades predating period fields."""


def migrate(cr, version):
    cr.execute(
        "ALTER TABLE contact_center_crm_conversation_link "
        "ADD COLUMN IF NOT EXISTS scope_decision_mode varchar"
    )
    # Odoo runs all pre-migrations before the new registry creates columns.
    # Earlier installed releases have no commercial-period state to preserve.
    cr.execute(
        "SELECT EXISTS(SELECT 1 FROM pg_attribute "
        "WHERE attrelid='contact_center_crm_conversation_link'::regclass "
        "AND attname='scope_state' AND NOT attisdropped)"
    )
    has_periods = cr.fetchone()[0]
    if has_periods:
        cr.execute(
            "UPDATE contact_center_crm_conversation_link "
            "SET scope_decision_mode=CASE WHEN scope_state='confirmed' "
            "THEN 'human' ELSE 'none' END WHERE scope_decision_mode IS NULL"
        )
    else:
        cr.execute(
            "UPDATE contact_center_crm_conversation_link "
            "SET scope_decision_mode='none' WHERE scope_decision_mode IS NULL"
        )
