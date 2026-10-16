from odoo import SUPERUSER_ID, api, fields


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for name in (
        "ir_cron_contact_center_group_metadata",
        "ir_cron_contact_center_identity_avatar",
    ):
        cron = env.ref("contact_center_base." + name, raise_if_not_found=False)
        if cron:
            cron.write({"active": False})

    # Legacy config invalidation used a due timestamp. Keep valid caches, errors,
    # partial snapshots not yet due and accepted requests; invalidate only the
    # old due marker. This never creates a job or rewrites a roster.
    env["contact.center.group.profile"].flush_model()
    env["queue.job"].flush_model()
    cr.execute(
        """
        UPDATE contact_center_group_profile AS profile
           SET next_sync_at = NULL
          FROM contact_center_provider_connection AS connection
         WHERE connection.id = profile.provider_connection_id
           AND profile.metadata_state = 'stale'
           AND profile.last_synced_at IS NOT NULL
           AND profile.applied_revision = profile.sync_revision
           AND COALESCE(profile.last_error_class, '') = ''
           AND profile.queue_job_uuid IS NULL
           AND profile.next_sync_at <= %s
           AND (profile.own_protocol_participant_json IS NULL
                OR profile.own_protocol_participant_json = '{}'::jsonb
                OR profile.own_protocol_participant_health_revision
                    != connection.health_configuration_revision)
           AND NOT EXISTS (
                SELECT 1 FROM queue_job AS job
                 WHERE job.state IN ('pending', 'enqueued', 'started', 'wait_dependencies')
                   AND job.identity_key = 'contact_center:group_metadata:'
                       || profile.id::text || ':' || profile.sync_revision::text)
        """,
        [fields.Datetime.now()],
    )
    env["contact.center.group.profile"].invalidate_model(["next_sync_at"])
