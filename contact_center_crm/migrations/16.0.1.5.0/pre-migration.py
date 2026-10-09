"""Existing confirmed periods were exclusively human decisions."""


def migrate(cr, version):
    cr.execute(
        "ALTER TABLE contact_center_crm_conversation_link "
        "ADD COLUMN IF NOT EXISTS scope_decision_mode varchar"
    )
    cr.execute(
        "UPDATE contact_center_crm_conversation_link "
        "SET scope_decision_mode=CASE WHEN scope_state='confirmed' "
        "THEN 'human' ELSE 'none' END WHERE scope_decision_mode IS NULL"
    )
