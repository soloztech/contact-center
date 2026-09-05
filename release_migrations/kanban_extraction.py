"""Prepare the one-time Kanban extraction before Odoo loads Base data.

This script is passed explicitly through ``--pre-upgrade-scripts`` by the
atomic release runner.  It is intentionally not a runtime compatibility
layer: it hands the ordinary XML records from the former Base namespace to the
optional Kanban addon before Base is loaded, and retires the old ``noupdate``
rules.  The work shares the module-upgrade transaction and is rolled back when
the upgrade fails.
"""

import logging

_logger = logging.getLogger(__name__)

_ORDINARY_XMLIDS_BY_MODEL = {
    "ir.ui.view": (
        "view_contact_center_pipeline_tree",
        "view_contact_center_pipeline_form",
        "view_contact_center_case_search",
        "view_contact_center_case_tree",
        "view_contact_center_case_kanban",
        "view_contact_center_case_form",
        "view_contact_center_team_form_pipeline",
        "view_contact_center_account_form_pipeline",
        "view_mail_channel_form_contact_center_cases",
        "view_contact_center_followup_request_tree",
        "view_contact_center_followup_request_form",
    ),
    "ir.actions.act_window": (
        "action_contact_center_pipelines",
        "action_contact_center_cases",
        "action_contact_center_followup_requests",
    ),
    "ir.ui.menu": (
        "menu_contact_center_cases",
        "menu_contact_center_pipelines",
        "menu_contact_center_followup_requests",
    ),
    "ir.model.access": (
        "access_contact_center_pipeline_agent",
        "access_contact_center_pipeline_stage_agent",
        "access_contact_center_case_agent",
        "access_contact_center_case_transition_agent",
        "access_contact_center_pipeline_admin",
        "access_contact_center_pipeline_stage_admin",
        "access_contact_center_case_admin",
        "access_contact_center_case_transition_admin",
        "access_contact_center_followup_request_admin",
    ),
}

_LEGACY_RULE_XMLIDS = (
    "rule_contact_center_pipeline_company",
    "rule_contact_center_pipeline_stage_company",
    "rule_contact_center_case_membership",
    "rule_contact_center_case_admin_company",
    "rule_contact_center_case_transition_membership",
    "rule_contact_center_case_transition_admin_company",
    "rule_contact_center_followup_request_admin_company",
)


def _sql_names(names):
    if not names or len(names) != len(set(names)):
        raise RuntimeError("Kanban cutover XML-ID inventory is invalid")
    return tuple(names)


def _assert_xmlid_models(cr, names, expected_model):
    cr.execute(
        """
        SELECT name, model
          FROM ir_model_data
         WHERE module = 'contact_center_base'
           AND name IN %s
         ORDER BY name
        """,
        (_sql_names(names),),
    )
    rows = cr.fetchall()
    mismatches = [(name, model) for name, model in rows if model != expected_model]
    if mismatches:
        raise RuntimeError("Kanban cutover XML-ID model mismatch: %s" % (mismatches,))
    return rows


def migrate(cr, installed_version):
    """Transfer fixed technical records without touching operational data."""

    ordinary_names = tuple(
        name for names in _ORDINARY_XMLIDS_BY_MODEL.values() for name in names
    )
    if len(ordinary_names) != 26 or len(ordinary_names) != len(set(ordinary_names)):
        raise RuntimeError("Kanban ordinary XML-ID inventory must contain 26 names")
    cr.execute(
        """
        SELECT module, name, model, res_id, noupdate
          FROM ir_model_data
         WHERE module IN ('contact_center_base', 'contact_center_kanban')
           AND name IN %s
         ORDER BY name, module
        """,
        (_sql_names(ordinary_names),),
    )
    ordinary_rows = cr.fetchall()
    expected_models = {
        name: model
        for model, names in _ORDINARY_XMLIDS_BY_MODEL.items()
        for name in names
    }
    invalid_rows = [
        row for row in ordinary_rows if row[2] != expected_models[row[1]] or row[4]
    ]
    if invalid_rows:
        raise RuntimeError(
            "Kanban ordinary XML-ID metadata mismatch: %s" % (invalid_rows,)
        )
    row_keys = [(module, name) for module, name, *_rest in ordinary_rows]
    if len(row_keys) != len(set(row_keys)):
        raise RuntimeError("Kanban ordinary XML-ID rows are duplicated")
    source_names = {row[1] for row in ordinary_rows if row[0] == "contact_center_base"}
    target_names = {
        row[1] for row in ordinary_rows if row[0] == "contact_center_kanban"
    }
    if source_names & target_names:
        raise RuntimeError("Kanban cutover has conflicting source and target XML-IDs")
    if source_names and source_names != set(ordinary_names):
        raise RuntimeError("Kanban source XML-ID inventory is incomplete")
    if target_names and target_names != set(ordinary_names):
        raise RuntimeError("Kanban target XML-ID inventory is incomplete")
    if not source_names and not target_names and ordinary_rows:
        raise RuntimeError("Kanban ordinary XML-ID inventory is inconsistent")

    rule_rows = _assert_xmlid_models(cr, _LEGACY_RULE_XMLIDS, "ir.rule")

    cr.execute(
        """
        UPDATE ir_model_data
           SET module = 'contact_center_kanban'
         WHERE module = 'contact_center_base'
           AND name IN %s
        RETURNING id
        """,
        (_sql_names(ordinary_names),),
    )
    transferred_xmlid_ids = [row[0] for row in cr.fetchall()]
    if source_names and len(transferred_xmlid_ids) != len(ordinary_names):
        raise RuntimeError("Kanban XML-ID handoff did not transfer all records")
    if target_names and transferred_xmlid_ids:
        raise RuntimeError("Idempotent Kanban XML-ID handoff unexpectedly wrote rows")

    cr.execute(
        """
        DELETE FROM ir_rule AS rule
         USING ir_model_data AS data
         WHERE data.module = 'contact_center_base'
           AND data.name IN %s
           AND data.model = 'ir.rule'
           AND rule.id = data.res_id
        RETURNING rule.id
        """,
        (_sql_names(_LEGACY_RULE_XMLIDS),),
    )
    deleted_rule_ids = [row[0] for row in cr.fetchall()]
    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE module = 'contact_center_base'
           AND name IN %s
           AND model = 'ir.rule'
        """,
        (_sql_names(_LEGACY_RULE_XMLIDS),),
    )

    _logger.info(
        "Kanban extraction pre-upgrade completed: installed_version=%s, "
        "ordinary_xmlids=%s, transferred_xmlids=%s, legacy_rules=%s, "
        "deleted_rules=%s",
        installed_version,
        len(ordinary_rows),
        len(transferred_xmlid_ids),
        len(rule_rows),
        len(deleted_rule_ids),
    )
