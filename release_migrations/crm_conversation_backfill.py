"""Complete the one-time CRM extraction with the new, complete Odoo registry.

Invoke ``migrate(env)`` in an offline Odoo shell after the module upgrade and
before restarting workers. The caller commits once after all invariants pass;
this file never commits, sends messages, or changes CRM/pipeline assignments.
"""

from odoo import fields

EXTRACTION_STATE_KEY = "contact_center_crm.conversation_extraction_state"

_LEGACY_JOB_METHODS = (
    "_job_marketing_contact_center_crm_case_link",
    "_job_marketing_contact_center_crm_backfill",
    "_job_marketing_contact_center_crm_attribution_link",
)


def _retire_legacy_jobs(env):
    if "queue.job" not in env.registry:
        return 0
    jobs = (
        env["queue.job"]
        .sudo()
        .search(
            [
                ("model_name", "=", "res.company"),
                ("method_name", "in", _LEGACY_JOB_METHODS),
                ("state", "not in", ["done", "cancelled"]),
            ]
        )
    )
    if jobs.filtered(lambda job: job.state == "started" or job.graph_uuid):
        raise RuntimeError("Stop/drain Marketing convergence jobs before CRM migration")
    # The old method no longer exists, so Job.load/button_cancelled cannot load
    # it. Retire only these standalone derived-projection jobs via ORM. Their
    # complete replacement is reconciled synchronously below, in this transaction.
    jobs.with_context(tracking_disable=True).write(
        {
            "state": "cancelled",
            "date_cancelled": fields.Datetime.now(),
            "result": "Replaced by conversation CRM extraction reconciliation",
        }
    )
    return len(jobs)


def _backfill_links(env):
    if "contact.center.crm.case.link" not in env.registry:
        return 0
    links = env["contact.center.crm.conversation.link"].sudo()
    cases = env["contact.center.crm.case.link"].sudo().with_context(active_test=False)
    cursor = 0
    created = 0
    while True:
        page = cases.search(
            [("id", ">", cursor), ("state", "=", "active"), ("lead_id", "!=", False)],
            order="id",
            limit=200,
        )
        if not page:
            break
        for old in page:
            if old.company_id != old.channel_id.contact_center_company_id or (
                old.lead_id.company_id and old.lead_id.company_id != old.company_id
            ):
                raise RuntimeError("Existing CRM case link has an inconsistent company")
            scoped = links.with_company(old.company_id).with_context(
                allowed_company_ids=[old.company_id.id]
            )
            existing = scoped.search(
                [
                    ("channel_id", "=", old.channel_id.id),
                    ("lead_id", "=", old.lead_id.id),
                    ("state", "=", "active"),
                ],
                limit=1,
            )
            canonical = scoped._link(old.channel_id, old.lead_id, origin=old.origin)
            if not existing:
                canonical._service().write(
                    {
                        "linked_at": old.linked_at,
                        "linked_by_id": old.linked_by_id.id,
                    }
                )
                created += 1
        cursor = page[-1].id
    return created


def _replace_marketing_authority(env):
    if "marketing.contact.center.crm.service" not in env.registry:
        return {"revoked_assertions": 0, "reconciled_pairs": 0}
    assertions = env["marketing.attribution.crm.link"].sudo()
    revoked = 0
    while True:
        page = assertions.search(
            [
                ("authority_key", "=", "contact_center.case"),
                ("revocation_ids", "=", False),
            ],
            order="id",
            limit=200,
        )
        if not page:
            break
        for assertion in page:
            service = (
                env["marketing.crm.service"]
                .sudo()
                .with_company(assertion.company_id)
                .with_context(allowed_company_ids=[assertion.company_id.id])
            )
            service._revoke_attribution_assertions(
                assertion,
                "contact_center.case",
                assertion.authority_ref,
                "crm-conversation-extraction:%s" % assertion.id,
                reason="replaced_by_conversation_crm_authority",
            )
            revoked += 1
    cursor = 0
    reconciled = 0
    canonical = env["contact.center.crm.conversation.link"].sudo()
    while True:
        links = canonical.search(
            [
                ("id", ">", cursor),
                ("state", "=", "active"),
                ("lead_id", "!=", False),
            ],
            order="id",
            limit=200,
        )
        if not links:
            break
        for link in links:
            service = (
                env["marketing.contact.center.crm.service"]
                .sudo()
                .with_company(link.company_id)
                .with_context(allowed_company_ids=[link.company_id.id])
            )
            after = 0
            while True:
                page = service._attribution_link_page(
                    link.channel_id, after_id=after, limit=200
                )
                if not page:
                    break
                reconciled += len(
                    service._reconcile_channel(
                        link.channel_id,
                        conversation_links=link,
                        attribution_links=page,
                    )
                )
                after = page[-1].id
        cursor = links[-1].id
    return {"revoked_assertions": revoked, "reconciled_pairs": reconciled}


def migrate(env):
    """Backfill links and replace derived authority without changing source ledgers."""
    if "contact.center.crm.conversation.link" not in env.registry:
        raise RuntimeError("The independent CRM registry must load before backfill")
    with env.cr.savepoint():
        retired_jobs = _retire_legacy_jobs(env)
        created_links = _backfill_links(env)
        marketing = _replace_marketing_authority(env)
        env.flush_all()
        # Completion is committed together with the rebuilt graph. Any failure
        # or caller rollback leaves the pre-upgrade pending marker intact.
        env["ir.config_parameter"].sudo().set_param(EXTRACTION_STATE_KEY, "done")
    return {
        "created_conversation_links": created_links,
        "retired_legacy_jobs": retired_jobs,
        **marketing,
    }
