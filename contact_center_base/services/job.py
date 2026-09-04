"""Shared queue policy helpers for Contact Center jobs."""

import hashlib

QUEUE_ATTEMPT_CEILING = 12
PROVIDER_PAUSED_RETRY_SECONDS = 60
PROVIDER_PAUSED_RETRY_MAX_SECONDS = 3600
PROVIDER_PAUSED_JITTER_PERCENT = 30

ACTIVE_QUEUE_JOB_STATES = (
    "pending",
    "enqueued",
    "started",
    "wait_dependencies",
)


def canonical_queue_job(
    record,
    identity_key,
    states,
    uuid_field="queue_job_uuid",
    *,
    adopt=True,
):
    """Return the newest job for an exact lane identity and optionally adopt it.

    The identity key is the durable owner of a logical lane.  A UUID stored on
    the business record is only a denormalized pointer and must never make an
    unrelated or pre-contract job look active.
    """

    record.ensure_one()
    owner = record.sudo()
    job = (
        owner.env["queue.job"]
        .sudo()
        .search(
            [("identity_key", "=", identity_key), ("state", "in", states)],
            order="id desc",
            limit=1,
        )
    )
    owner.invalidate_recordset([uuid_field])
    if adopt and job and getattr(owner, uuid_field) != job.uuid:
        owner.write({uuid_field: job.uuid})
    return job


def queue_job_owns_record(record, uuid_field="queue_job_uuid"):
    """Require an exact persisted UUID before a worker may have side effects."""

    record.ensure_one()
    owner = record.sudo()
    job_uuid = owner.env.context.get("job_uuid")
    owner.invalidate_recordset([uuid_field])
    persisted_uuid = getattr(owner, uuid_field)
    return bool(job_uuid and persisted_uuid and job_uuid == persisted_uuid)


def provider_paused_retry_seconds(error, seed):
    """Return a stable, bounded retry delay with positive dephasing jitter.

    Paused jobs deliberately do not consume the normal failure ceiling.  A
    deterministic positive jitter dephases jobs belonging to many inboxes while
    keeping retries reproducible in logs/tests. Explicit ``Retry-After`` values
    are first clamped to the one-hour operational policy ceiling, so this helper
    intentionally treats that ceiling as stronger than an excessive provider hint.
    """

    hinted = getattr(error, "retry_after_seconds", 0)
    base = hinted if isinstance(hinted, int) and not isinstance(hinted, bool) else 0
    base = min(
        PROVIDER_PAUSED_RETRY_MAX_SECONDS,
        max(1, base or PROVIDER_PAUSED_RETRY_SECONDS),
    )
    jitter_window = max(1, (base * PROVIDER_PAUSED_JITTER_PERCENT) // 100)
    digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
    # Keep an actual positive offset whenever the operational ceiling leaves
    # headroom. A zero offset would keep a subset of a large paused fleet aligned.
    jitter = 1 + (int.from_bytes(digest[:4], "big") % jitter_window)
    return min(PROVIDER_PAUSED_RETRY_MAX_SECONDS, base + jitter)
