"""Canonical locking helpers for the CRM/Contact Center stage projection."""

CRM_STAGE_SYNC_GRAPH_LOCK_TOKEN = object()


def stage_sync_graph_is_locked(env):
    return (
        env.context.get("contact_center_crm_stage_sync_graph_lock")
        is CRM_STAGE_SYNC_GRAPH_LOCK_TOKEN
    )


def _positive_ids(values):
    return sorted(
        {
            value
            for value in values
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        }
    )


def lock_stage_sync_graph(
    env,
    *,
    lead_ids=(),
    case_ids=(),
    touch_leads=False,
    touch_cases=False,
):
    """Lock a stage-sync graph in the only supported order: leads, then cases.

    A lead can project its stage into several Contact Center cases.  Locking a
    single case before its lead lets two case transitions form a cycle.  The
    lead row is therefore the serialization authority and every caller locks
    all linked cases in ascending order before reading mutable stage facts.
    """

    locked_lead_ids = _positive_ids(lead_ids)
    locked_case_ids = set(_positive_ids(case_ids))
    if locked_lead_ids:
        env["crm.lead"].flush_model(["company_id", "stage_id", "team_id", "write_date"])
        env.cr.execute(
            "SELECT id FROM crm_lead WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [locked_lead_ids],
        )
        locked_lead_ids = [row[0] for row in env.cr.fetchall()]
        if touch_leads and locked_lead_ids:
            # A topology mutation (link, unlink or merge) can otherwise wait on
            # this lock and continue with a REPEATABLE READ snapshot that does
            # not contain a link committed by the prior holder.  Create a new
            # MVCC version so the stale waiter serializes and retries instead of
            # leaving an active bridge pointing at a deleted/merged lead.
            env.cr.execute(
                "UPDATE crm_lead SET write_date = write_date WHERE id = ANY(%s)",
                [locked_lead_ids],
            )
        env["contact.center.crm.case.link"].sudo().flush_model(["case_id", "lead_id"])
        env.cr.execute(
            """
            SELECT DISTINCT case_id
              FROM contact_center_crm_case_link
             WHERE lead_id = ANY(%s)
          ORDER BY case_id
            """,
            [locked_lead_ids],
        )
        locked_case_ids.update(row[0] for row in env.cr.fetchall())

    ordered_case_ids = sorted(locked_case_ids)
    if ordered_case_ids:
        env["contact.center.case"].flush_model(
            ["company_id", "pipeline_id", "stage_id", "stage_revision", "team_id"]
        )
        env.cr.execute(
            """
            SELECT id
              FROM contact_center_case
             WHERE id = ANY(%s)
          ORDER BY id
               FOR UPDATE
            """,
            [ordered_case_ids],
        )
        ordered_case_ids = [row[0] for row in env.cr.fetchall()]
        if touch_cases and ordered_case_ids:
            # Linking/unlinking changes a child ledger row, which by itself does
            # not create a new MVCC version of the case authority.  Touch the
            # parent so an archive waiter that started from an older
            # REPEATABLE READ snapshot must serialize/retry instead of missing
            # the newly committed link lifecycle.
            env.cr.execute(
                "UPDATE contact_center_case SET write_date = write_date "
                "WHERE id = ANY(%s)",
                [ordered_case_ids],
            )

    leads = env["crm.lead"].browse(locked_lead_ids)
    cases = env["contact.center.case"].browse(ordered_case_ids)
    leads.invalidate_recordset(["company_id", "stage_id", "team_id", "write_date"])
    cases.invalidate_recordset(
        [
            "company_id",
            "pipeline_id",
            "stage_id",
            "stage_revision",
            "team_id",
            "write_date",
        ]
    )
    return leads, cases
