"""Verify connected legacy rows that have no conclusive observation."""


def migrate(cr, version):
    cr.execute(
        "UPDATE contact_center_provider_connection SET next_health_check_at = NOW() "
        "WHERE active AND state = 'connected' AND NOT health_check_pending "
        "AND (last_state_observed_at IS NULL OR last_state_source IS NULL "
        "OR last_state_source NOT IN ('health_job', 'provider_event'))"
    )
