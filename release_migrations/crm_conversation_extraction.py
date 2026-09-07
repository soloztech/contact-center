"""One-time ownership handoff before loading the independent CRM addon.

Run through --pre-upgrade-scripts while all workers are stopped. Only Odoo
technical metadata changes; operational rows and IDs stay in place. The
companion post-migration performs canonical links/Marketing reconciliation
through ORM after the complete new registry has loaded.
"""

import logging

_logger = logging.getLogger(__name__)
SOURCE_MODULE = "contact_center_crm"
TARGET_MODULE = "contact_center_kanban"
EXTRACTION_STATE_KEY = "contact_center_crm.conversation_extraction_state"
MOVED_XMLIDS = {
    "access_cc_crm_case_link_admin": "ir.model.access",
    "access_cc_crm_case_link_salesman": "ir.model.access",
    "access_cc_crm_link_wizard_salesman": "ir.model.access",
    "access_cc_crm_mapping_inventory_admin": "ir.model.access",
    "access_cc_crm_mapping_inventory_line_admin": "ir.model.access",
    "access_cc_crm_pipeline_binding_admin": "ir.model.access",
    "access_cc_crm_pipeline_binding_agent": "ir.model.access",
    "access_cc_crm_role_grant_admin": "ir.model.access",
    "access_cc_crm_stage_binding_admin": "ir.model.access",
    "access_cc_crm_stage_binding_agent": "ir.model.access",
    "access_cc_crm_team_binding_admin": "ir.model.access",
    "access_cc_crm_team_binding_agent": "ir.model.access",
    "action_cc_crm_case_links": "ir.actions.act_window",
    "action_cc_crm_mapping_inventory": "ir.actions.act_window",
    "action_cc_crm_pipeline_bindings": "ir.actions.act_window",
    "action_cc_crm_team_bindings": "ir.actions.act_window",
    "menu_cc_crm_case_links": "ir.ui.menu",
    "menu_cc_crm_integration": "ir.ui.menu",
    "menu_cc_crm_mapping_inventory": "ir.ui.menu",
    "menu_cc_crm_pipeline_bindings": "ir.ui.menu",
    "menu_cc_crm_team_bindings": "ir.ui.menu",
    "rule_cc_crm_case_link_admin": "ir.rule",
    "rule_cc_crm_case_link_all_leads": "ir.rule",
    "rule_cc_crm_case_link_own": "ir.rule",
    "rule_cc_crm_mapping_inventory_admin": "ir.rule",
    "rule_cc_crm_mapping_inventory_line_admin": "ir.rule",
    "rule_cc_crm_pipeline_binding_company": "ir.rule",
    "rule_cc_crm_role_grant_admin": "ir.rule",
    "rule_cc_crm_stage_binding_company": "ir.rule",
    "rule_cc_crm_team_binding_company": "ir.rule",
    "view_cc_crm_case_link_form": "ir.ui.view",
    "view_cc_crm_case_link_tree": "ir.ui.view",
    "view_cc_crm_link_wizard_form": "ir.ui.view",
    "view_cc_crm_mapping_inventory_form": "ir.ui.view",
    "view_cc_crm_pipeline_binding_form": "ir.ui.view",
    "view_cc_crm_pipeline_binding_tree": "ir.ui.view",
    "view_cc_crm_team_binding_form": "ir.ui.view",
    "view_cc_crm_team_binding_tree": "ir.ui.view",
    "view_contact_center_case_form_crm": "ir.ui.view",
    "view_contact_center_case_tree_crm": "ir.ui.view",
    "view_contact_center_pipeline_form_crm": "ir.ui.view",
    "view_contact_center_pipeline_tree_crm": "ir.ui.view",
    "view_contact_center_team_form_crm": "ir.ui.view",
    "view_crm_lead_form_contact_center": "ir.ui.view",
    "view_crm_stage_search_contact_center": "ir.ui.view",
    "view_crm_team_form_contact_center": "ir.ui.view",
    "view_mail_channel_form_contact_center_crm": "ir.ui.view",
}

MOVED_MODELS = (
    "contact.center.crm.case.link",
    "contact.center.crm.catalog.authority",
    "contact.center.crm.link.wizard",
    "contact.center.crm.mapping.inventory",
    "contact.center.crm.mapping.inventory.line",
    "contact.center.crm.pipeline.binding",
    "contact.center.crm.role.grant",
    "contact.center.crm.stage.binding",
    "contact.center.crm.team.binding",
)

MOVED_EXTENSION_FIELDS = {
    "contact.center.case": (
        "crm_link_ids",
        "crm_lead_id",
        "crm_lead_count",
        "crm_link_exists",
    ),
    "contact.center.pipeline": ("crm_pipeline_binding_ids", "crm_team_id"),
    "contact.center.pipeline.stage": ("crm_stage_binding_ids",),
    "contact.center.team": ("crm_team_binding_ids", "crm_team_id"),
    "crm.lead": ("contact_center_case_link_ids", "contact_center_case_count"),
    "crm.stage": ("active", "contact_center_stage_binding_ids"),
    "crm.team": (
        "contact_center_roster_revision",
        "contact_center_team_binding_ids",
        "contact_center_pipeline_binding_ids",
        "contact_center_team_binding_count",
        "contact_center_pipeline_binding_count",
    ),
}


def _metadata_ids(cr):
    """Select only the former bridge, never the new CRM conversation core."""
    cr.execute("SELECT id FROM ir_model WHERE model IN %s", [MOVED_MODELS])
    model_ids = [row[0] for row in cr.fetchall()]
    cr.execute("SELECT id FROM ir_model_fields WHERE model IN %s", [MOVED_MODELS])
    field_ids = {row[0] for row in cr.fetchall()}
    for model, names in MOVED_EXTENSION_FIELDS.items():
        cr.execute(
            "SELECT id FROM ir_model_fields WHERE model = %s AND name IN %s",
            [model, names],
        )
        field_ids.update(row[0] for row in cr.fetchall())
    cr.execute(
        "SELECT id FROM ir_model_fields_selection WHERE field_id = ANY(%s)",
        [sorted(field_ids)],
    )
    selections = [row[0] for row in cr.fetchall()]
    cr.execute(
        """
        SELECT c.id FROM ir_model_constraint c JOIN ir_model m ON m.id = c.model
        WHERE c.model = ANY(%s) OR
              (m.model = 'crm.team' AND c.name =
               'crm_team_contact_center_roster_revision_nonnegative')
        """,
        [model_ids],
    )
    return {
        "ir.model": model_ids,
        "ir.model.fields": sorted(field_ids),
        "ir.model.fields.selection": selections,
        "ir.model.constraint": [row[0] for row in cr.fetchall()],
    }


def migrate(cr, installed_version):
    """Fail on partial/conflicting ownership; move the whole old bridge once."""
    cr.execute(
        """SELECT module, name, model, res_id FROM ir_model_data
           WHERE module IN %s AND name IN %s ORDER BY name, module""",
        [(SOURCE_MODULE, TARGET_MODULE), tuple(MOVED_XMLIDS)],
    )
    rows = cr.fetchall()
    source = {row[1] for row in rows if row[0] == SOURCE_MODULE}
    target = {row[1] for row in rows if row[0] == TARGET_MODULE}
    if any(model != MOVED_XMLIDS[name] for _, name, model, _ in rows):
        raise RuntimeError("CRM extraction XML-ID model mismatch")
    if source & target:
        raise RuntimeError("CRM extraction source and target XML-IDs conflict")
    if source and source != set(MOVED_XMLIDS):
        raise RuntimeError("CRM extraction source XML-ID inventory is incomplete")
    if target and target != set(MOVED_XMLIDS):
        raise RuntimeError("CRM extraction target XML-ID inventory is incomplete")
    if not source:
        return
    metadata = _metadata_ids(cr)
    if len(metadata["ir.model"]) != len(MOVED_MODELS):
        raise RuntimeError("CRM extraction model inventory is incomplete")
    cr.execute("SELECT id FROM ir_module_module WHERE name = %s", [TARGET_MODULE])
    target_module = cr.fetchone()
    if not target_module:
        raise RuntimeError("CRM extraction requires the optional Kanban addon")
    cr.execute("SELECT id FROM ir_module_module WHERE name = %s", [SOURCE_MODULE])
    source_module_id = cr.fetchone()[0]
    # Model reflection may create matching target metadata during a previous
    # partially planned attempt. Fail closed instead of guessing which identity wins.
    for model, ids in metadata.items():
        cr.execute(
            """SELECT s.name FROM ir_model_data s JOIN ir_model_data t
               ON t.module = %s AND t.name = s.name
               WHERE s.module = %s AND s.model = %s AND s.res_id = ANY(%s)""",
            [TARGET_MODULE, SOURCE_MODULE, model, ids],
        )
        if cr.fetchall():
            raise RuntimeError("CRM extraction generated XML-IDs conflict")
        cr.execute(
            """UPDATE ir_model_data SET module = %s
               WHERE module = %s AND model = %s AND res_id = ANY(%s)""",
            [TARGET_MODULE, SOURCE_MODULE, model, ids],
        )
    cr.execute(
        """UPDATE ir_model_data SET module = %s
           WHERE module = %s AND name IN %s""",
        [TARGET_MODULE, SOURCE_MODULE, tuple(MOVED_XMLIDS)],
    )
    cr.execute(
        """UPDATE ir_model_constraint SET module = %s
           WHERE module = %s AND id = ANY(%s)""",
        [target_module[0], source_module_id, metadata["ir.model.constraint"]],
    )
    cr.execute(
        """UPDATE ir_model_relation SET module = %s
           WHERE module = %s AND model = ANY(%s)""",
        [target_module[0], source_module_id, metadata["ir.model"]],
    )
    # The installed Marketing consumer changes hook/model and queue function.
    # It must reload in the same registry as the independent CRM core.
    cr.execute(
        """UPDATE ir_module_module SET state = 'to upgrade'
           WHERE name = 'marketing_center_contact_center_crm' AND state = 'installed'"""
    )
    # Persist the unfinished second phase in the same transaction as ownership.
    # Zero old XML-IDs alone cannot distinguish completion from interruption.
    cr.execute(
        """INSERT INTO ir_config_parameter
               (key, value, create_uid, write_uid, create_date, write_date)
           VALUES (%s, 'pending', 1, 1,
                   NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
           ON CONFLICT (key) DO UPDATE SET value = 'pending', write_uid = 1,
               write_date = NOW() AT TIME ZONE 'UTC'""",
        [EXTRACTION_STATE_KEY],
    )
    _logger.info(
        "CRM extraction transferred %s declared XML-IDs and %s models",
        len(source),
        len(metadata["ir.model"]),
    )
