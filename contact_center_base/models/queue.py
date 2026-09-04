import datetime
import logging
import math

from psycopg2.errors import DeadlockDetected, SerializationFailure

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import (
    AdapterError,
    AmbiguousTimeoutError,
    ProviderPausedError,
    ProviderRateLimitError,
    TransientAdapterError,
    UnsupportedEventError,
    conversation_capabilities,
    validate_provider_request_snapshot,
)
from ..services.dto import (
    AdapterResult,
    AddressDTO,
    CommandDTO,
    DTOValidationError,
    EventDTO,
)
from ..services.job import (
    ACTIVE_QUEUE_JOB_STATES,
    PROVIDER_PAUSED_RETRY_MAX_SECONDS,
    QUEUE_ATTEMPT_CEILING,
    canonical_queue_job,
    provider_paused_retry_seconds,
    queue_job_owns_record,
)
from ..services.media import (
    validate_provider_media_capability,
    validate_provider_media_caption_capability,
)
from .application import GroupRosterRefreshRequired, IdentityConflictError

_logger = logging.getLogger(__name__)

_ACTIVE_QUEUE_JOB_STATES = ACTIVE_QUEUE_JOB_STATES
_QUEUE_RECOVERY_GRACE_SECONDS = 300
_QUEUE_RECOVERY_LIMIT_PER_MODEL = 100
_QUEUE_RECOVERY_MAX_BATCH = 500
_PROVIDER_SUCCESS_RECONCILIATION_MAX_ATTEMPTS = 3
_PROVIDER_SUCCESS_RECONCILIATION_ERROR_CLASSES = frozenset(
    {"DeadlockDetected", "SerializationFailure"}
)
_OUTBOUND_MIN_INTERVAL_MAX_SECONDS = 300
_QUEUE_RECOVERY_LANES = {
    "inbox": {
        "model": "contact.center.inbox.event",
        "table": "contact_center_inbox_event",
        "states": ("pending", "retry"),
        "enqueue_method": "_enqueue",
        # A privileged group mutation waiting for a post-event roster is owned by
        # the group metadata lane. Generic recovery may pick it up only after the
        # requested complete snapshot has actually covered its local receipt.
        "extra_where": (
            "AND (ledger.waiting_group_profile_id IS NULL OR EXISTS ("
            "SELECT 1 FROM contact_center_group_profile AS waiting_profile "
            "WHERE waiting_profile.id = ledger.waiting_group_profile_id "
            "AND waiting_profile.metadata_state = 'ready' "
            "AND waiting_profile.roster_complete IS TRUE "
            "AND waiting_profile.sync_requested_at "
            ">= ledger.waiting_group_roster_after "
            "AND waiting_profile.last_synced_at "
            ">= ledger.waiting_group_roster_after "
            "AND waiting_profile.applied_revision = "
            "waiting_profile.sync_revision))"
        ),
    },
    "outbox": {
        "model": "contact.center.outbox.command",
        "table": "contact_center_outbox_command",
        "states": ("pending", "retry"),
        "enqueue_method": "_enqueue",
        "extra_where": (
            "AND ledger.dispatch_job_uuid IS NULL "
            "AND ledger.dispatch_started_at IS NULL"
        ),
    },
    "media": {
        "model": "contact.center.media.binding",
        "table": "contact_center_media_binding",
        "states": ("pending",),
        "enqueue_method": "_enqueue_download",
        "extra_where": "",
    },
}


def _queue_identity_key(lane, record_id):
    return "contact_center:%s:%s" % (lane, record_id)


def _related_queue_job(record, lane, states):
    """Find the job that owns a ledger lane by its canonical identity only."""

    identity_key = _queue_identity_key(lane, record.id)
    return canonical_queue_job(
        record,
        identity_key,
        states,
    )


def _adopt_queue_job(record, job):
    if job and record.queue_job_uuid != job.uuid:
        record.sudo().write({"queue_job_uuid": job.uuid})
    return job


def _reuse_queue_job(record, lane):
    """Reuse an active job or revive its failed predecessor before creating one."""

    active_job = _related_queue_job(record, lane, _ACTIVE_QUEUE_JOB_STATES)
    if active_job:
        return active_job, False
    failed_job = _related_queue_job(record, lane, ("failed",))
    if not failed_job:
        return failed_job, False
    failed_job.requeue()
    # Keep recovery deterministic across queue_job patch releases.
    failed_job.write({"eta": False, "retry": 0})
    return _adopt_queue_job(record, failed_job), True


def _retry_delay(error, default=None):
    hinted = getattr(error, "retry_after_seconds", 0)
    if isinstance(hinted, int) and not isinstance(hinted, bool) and hinted > 0:
        return min(PROVIDER_PAUSED_RETRY_MAX_SECONDS, hinted)
    return default


def _ceil_datetime_to_second(value):
    """Round upward so second-precision ORM persistence never shortens a delay."""

    rounded = value.replace(microsecond=0)
    if value.microsecond:
        rounded += datetime.timedelta(seconds=1)
    return rounded


def _outbound_throttle_retry_seconds(deadline, now):
    return min(
        PROVIDER_PAUSED_RETRY_MAX_SECONDS,
        max(1, math.ceil((deadline - now).total_seconds())),
    )


class OutboundThrottleError(ProviderPausedError):
    """Local pre-dispatch deadline that must retry exactly, without fleet jitter."""


class RetrySourceInvalidatedError(ValidationError):
    """A manual retry lost its fail-safe source before provider dispatch."""


class PostDispatchPersistenceError(Exception):
    """Provider returned success, but local finalization could not be committed."""

    def __init__(self, original_error, result=None):
        self.original_error = original_error
        self.result = result
        super().__init__(
            "provider dispatch succeeded but local persistence failed: %s"
            % original_error.__class__.__name__
        )

    def provider_evidence(self):
        result = self.result
        return {
            "dispatch_outcome": "provider_returned_success",
            "external_message_id": str(
                getattr(result, "external_message_id", "") or ""
            ),
            "provider_response": dict(getattr(result, "provider_response", {}) or {}),
            "local_error_class": self.original_error.__class__.__name__,
        }


class AmbiguousAdapterResultError(AmbiguousTimeoutError):
    """Typed uncertain provider result whose sanitized evidence must survive."""

    def __init__(self, result):
        self.result = result
        super().__init__(result.error_message or result.error_code)

    def provider_evidence(self):
        return {
            "dispatch_outcome": "provider_returned_uncertain",
            "error_code": self.result.error_code,
            "provider_response": dict(self.result.provider_response or {}),
        }


class GroupRosterMetadataFailedError(ValidationError):
    """Permanent roster dependency failure recorded on an inbox event."""


class ContactCenterInboxEvent(models.Model):
    _name = "contact.center.inbox.event"
    _description = "Contact Center Inbox Event"
    _order = "create_date, id"

    provider_connection_id = fields.Many2one(
        "contact.center.provider.connection",
        required=True,
        index=True,
        ondelete="restrict",
    )
    account_id = fields.Many2one(
        related="provider_connection_id.account_id",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="provider_connection_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    inbox_dedupe_key = fields.Char(required=True, index=True)
    provider_schema_version = fields.Char(required=True)
    raw_envelope_json = fields.Json(
        required=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    metadata_json = fields.Json(default=dict)
    normalized_dto_json = fields.Json(
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("retry", "Retry"),
            ("done", "Done"),
            ("blocked", "Blocked"),
            ("unsupported", "Unsupported"),
            ("dead", "Dead"),
        ],
        required=True,
        default="pending",
        index=True,
        copy=False,
    )
    attempts = fields.Integer(default=0, required=True, copy=False)
    queue_job_uuid = fields.Char(readonly=True, copy=False, index=True)
    waiting_group_profile_id = fields.Many2one(
        "contact.center.group.profile",
        readonly=True,
        copy=False,
        index=True,
        ondelete="set null",
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Complete group roster whose refresh must cover this event before "
            "privileged mutation processing can resume."
        ),
    )
    waiting_group_roster_after = fields.Datetime(
        readonly=True,
        copy=False,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    group_roster_wait_count = fields.Integer(
        default=0,
        required=True,
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Lifetime number of processing attempts that required newer group-roster "
            "evidence, including the terminal ceiling attempt."
        ),
    )
    first_group_roster_wait_at = fields.Datetime(
        readonly=True,
        copy=False,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
        help="First time this event was durably deferred for group-roster evidence.",
    )
    has_active_queue_job = fields.Boolean(
        compute="_compute_has_active_queue_job",
    )
    processed_at = fields.Datetime(copy=False)
    last_error_class = fields.Char(copy=False)
    last_error_message = fields.Text(copy=False)

    _sql_constraints = [
        (
            "connection_dedupe_unique",
            "unique(provider_connection_id, inbox_dedupe_key)",
            "This provider event was already received.",
        ),
        (
            "attempts_nonnegative",
            "check(attempts >= 0)",
            "Attempts cannot be negative.",
        ),
        (
            "group_roster_wait_count_nonnegative",
            "check(group_roster_wait_count >= 0)",
            "Group roster wait count cannot be negative.",
        ),
    ]

    def init(self):
        """Install the recovery index only after the inbox table exists."""

        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS cc_onboarding_release_eligible_idx
                ON contact_center_inbox_event (provider_connection_id, id)
             WHERE state = 'blocked'
               AND metadata_json ->> 'blocked_reason' =
                   'onboarding_not_activated'
            """
        )

    @api.model_create_multi
    def create(self, vals_list):
        events = super().create(vals_list)
        if not self.env.context.get("contact_center_skip_enqueue"):
            events._enqueue()
        return events

    def _enqueue(self, eta=None):
        for event in self.filtered(
            lambda item: item.state in ("pending", "processing", "retry")
            and item._group_roster_wait_is_satisfied()
        ):
            existing_job, _recovered = _reuse_queue_job(event, "inbox")
            if existing_job:
                continue
            if event.queue_job_uuid:
                event.sudo().write({"queue_job_uuid": False})
            delayed = (
                event.sudo()
                .with_company(event.company_id)
                .with_delay(
                    identity_key="contact_center:inbox:%s" % event.id,
                    max_retries=0,
                    description="Contact Center inbox event %s" % event.id,
                    eta=eta,
                )
                ._job_process()
            )
            event.sudo().write({"queue_job_uuid": delayed.uuid})
        return True

    def _group_roster_wait_is_satisfied(self):
        """Return whether a deferred group authorization can be retried now."""

        self.ensure_one()
        # Recovery selects eligibility with SQL and may run in an environment that
        # previously cached the waiter/profile before a concurrent snapshot commit.
        # Re-read both sides before the ORM enqueue guard repeats that predicate.
        self.invalidate_recordset(
            ["waiting_group_profile_id", "waiting_group_roster_after"]
        )
        profile = self.waiting_group_profile_id
        if not profile:
            return True
        profile.invalidate_recordset(
            [
                "metadata_state",
                "roster_complete",
                "sync_requested_at",
                "last_synced_at",
                "applied_revision",
                "sync_revision",
            ]
        )
        required_after = fields.Datetime.to_datetime(self.waiting_group_roster_after)
        sync_requested_at = fields.Datetime.to_datetime(profile.sync_requested_at)
        last_synced_at = fields.Datetime.to_datetime(profile.last_synced_at)
        return bool(
            required_after
            and profile.metadata_state == "ready"
            and profile.roster_complete
            and sync_requested_at
            and sync_requested_at >= required_after
            and last_synced_at
            and last_synced_at >= required_after
            and profile.applied_revision == profile.sync_revision
        )

    @api.constrains(
        "provider_connection_id",
        "waiting_group_profile_id",
        "waiting_group_roster_after",
    )
    def _check_waiting_group_roster_scope(self):
        for event in self:
            profile = event.waiting_group_profile_id
            if not profile:
                if event.waiting_group_roster_after:
                    raise ValidationError(
                        _("A roster wait timestamp requires its group profile.")
                    )
                continue
            if not event.waiting_group_roster_after:
                raise ValidationError(
                    _("A deferred group mutation requires its roster timestamp.")
                )
            if (
                profile.provider_connection_id != event.provider_connection_id
                or profile.account_id != event.account_id
            ):
                raise ValidationError(
                    _("The deferred group roster belongs to another inbox scope.")
                )

    def _has_active_queue_job(self):
        self.ensure_one()
        return bool(_related_queue_job(self, "inbox", _ACTIVE_QUEUE_JOB_STATES))

    @api.depends("queue_job_uuid")
    def _compute_has_active_queue_job(self):
        for event in self:
            event.has_active_queue_job = event._has_active_queue_job()

    def action_requeue(self):
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_admin"
        ):
            raise AccessError(_("Only Contact Center administrators can requeue."))
        eligible = self.filtered(
            lambda event: event.state
            in ("pending", "retry", "blocked", "unsupported", "dead")
        )
        to_enqueue = self.browse()
        for event in eligible:
            self.env.cr.execute(
                "SELECT id FROM contact_center_inbox_event WHERE id = %s FOR UPDATE",
                [event.id],
            )
            event.invalidate_recordset(
                ["state", "attempts", "queue_job_uuid", "metadata_json"]
            )
            active_job = _related_queue_job(event, "inbox", _ACTIVE_QUEUE_JOB_STATES)
            if active_job:
                _adopt_queue_job(event, active_job)
                if event.state in ("blocked", "unsupported", "dead"):
                    raise ValidationError(
                        _("Wait for the active Inbox job before replaying this event.")
                    )
                continue
            if event.state == "blocked" and self.env[
                "contact.center.identity.conflict"
            ].sudo().search_count(
                [("inbox_event_id", "=", event.id), ("state", "=", "open")]
            ):
                raise ValidationError(
                    _("Resolve the identity conflict before replaying this event.")
                )
            metadata = dict(event.metadata_json or {})
            if (
                event.state == "blocked"
                and metadata.get("blocked_reason") == "onboarding_not_activated"
                and not event.provider_connection_id.onboarding_activated_at
            ):
                raise ValidationError(
                    _("Activate the guided inbox before replaying its callbacks.")
                )
            metadata.update(
                {
                    "manual_requeue_count": int(
                        metadata.get("manual_requeue_count") or 0
                    )
                    + 1,
                    "manual_requeue_at": fields.Datetime.to_string(
                        fields.Datetime.now()
                    ),
                    "manual_requeue_user_id": self.env.user.id,
                }
            )
            values = {
                "queue_job_uuid": False,
                "metadata_json": metadata,
                "waiting_group_profile_id": False,
                "waiting_group_roster_after": False,
            }
            if event.state in ("blocked", "unsupported", "dead"):
                values.update(
                    {
                        "state": "pending",
                        "attempts": 0,
                        "processed_at": False,
                        "last_error_class": False,
                        "last_error_message": False,
                        "normalized_dto_json": False,
                    }
                )
            event.sudo().write(values)
            existing_job, _recovered = _reuse_queue_job(event, "inbox")
            if existing_job:
                continue
            to_enqueue |= event
        return to_enqueue._enqueue()

    def _contact_center_requeue_onboarding_activation(self, expected_onboarding_ref):
        """Release one bounded, already-locked setup batch through Inbox fences."""

        if not isinstance(expected_onboarding_ref, str) or not expected_onboarding_ref:
            raise ValidationError(_("The onboarding release reference is invalid."))
        to_enqueue = self.browse()
        released_at = fields.Datetime.to_string(fields.Datetime.now())
        for event in self.sudo().sorted("id"):
            self.env.cr.execute(
                "SELECT id FROM contact_center_inbox_event WHERE id = %s FOR UPDATE",
                [event.id],
            )
            event.invalidate_recordset(
                ["state", "attempts", "queue_job_uuid", "metadata_json"]
            )
            metadata = dict(event.metadata_json or {})
            connection = event.provider_connection_id
            connection.invalidate_recordset(
                [
                    "active",
                    "role",
                    "inbound_active",
                    "onboarding_ref",
                    "onboarding_activated_at",
                ]
            )
            connection.account_id.invalidate_recordset(["active"])
            if (
                event.state != "blocked"
                or metadata.get("blocked_reason") != "onboarding_not_activated"
                or metadata.get("onboarding_ref") != expected_onboarding_ref
                or connection.onboarding_ref != expected_onboarding_ref
                or not connection.onboarding_activated_at
                or not connection._contact_center_inbound_is_available()
            ):
                raise ValidationError(
                    _("The buffered callback is not eligible for activation replay.")
                )
            if _related_queue_job(event, "inbox", _ACTIVE_QUEUE_JOB_STATES):
                raise ValidationError(
                    _("Wait for the active Inbox job before releasing this callback.")
                )
            if (
                self.env["contact.center.identity.conflict"]
                .sudo()
                .search_count(
                    [("inbox_event_id", "=", event.id), ("state", "=", "open")]
                )
            ):
                raise ValidationError(
                    _("Resolve the identity conflict before releasing this callback.")
                )
            metadata.pop("blocked_reason", None)
            metadata.update(
                {
                    "onboarding_release_source": "activation",
                    "onboarding_replayed_at": released_at,
                    "onboarding_replay_count": int(
                        metadata.get("onboarding_replay_count") or 0
                    )
                    + 1,
                }
            )
            event.write(
                {
                    "state": "pending",
                    "attempts": 0,
                    "queue_job_uuid": False,
                    "processed_at": False,
                    "last_error_class": False,
                    "last_error_message": False,
                    "normalized_dto_json": False,
                    "waiting_group_profile_id": False,
                    "waiting_group_roster_after": False,
                    "metadata_json": metadata,
                }
            )
            to_enqueue |= event
        return to_enqueue._enqueue()

    @api.model
    def _lock_orphaned_queue_record_ids(self, lane, cutoff, limit):
        """Lock one bounded set of committed ledgers with no runnable job."""

        specification = _QUEUE_RECOVERY_LANES.get(lane)
        if not specification:
            raise ValidationError(_("Unknown Contact Center queue recovery lane."))
        ledger_model = self.env[specification["model"]].sudo()
        flush_fields = ["state", "queue_job_uuid", "write_date"]
        if lane == "inbox":
            # ``extra_where`` joins the group profile directly. Flush its readiness
            # horizon as well, otherwise a snapshot committed in the current worker
            # can remain invisible to this SQL recovery pass until another cron run.
            self.env["contact.center.group.profile"].sudo().flush_model(
                [
                    "metadata_state",
                    "roster_complete",
                    "sync_requested_at",
                    "last_synced_at",
                    "applied_revision",
                    "sync_revision",
                ]
            )
        if lane == "outbox":
            flush_fields.extend(["dispatch_job_uuid", "dispatch_started_at"])
        ledger_model.flush_model(flush_fields)
        self.env["queue.job"].sudo().flush_model(["uuid", "identity_key", "state"])
        # Table names and the optional predicate come only from the closed map
        # above; all runtime values remain SQL parameters.
        self.env.cr.execute(
            """
            SELECT ledger.id
              FROM %s AS ledger
             WHERE ledger.state IN %%s
               AND COALESCE(ledger.write_date, ledger.create_date) <= %%s
               %s
               AND NOT EXISTS (
                    SELECT 1
                      FROM queue_job AS job
                     WHERE job.state IN %%s
                       AND job.uuid = ledger.queue_job_uuid
                       AND job.identity_key = %%s || ledger.id::text
               )
          ORDER BY COALESCE(ledger.write_date, ledger.create_date), ledger.id
             FOR UPDATE OF ledger SKIP LOCKED
             LIMIT %%s
            """
            % (specification["table"], specification["extra_where"]),
            [
                specification["states"],
                cutoff,
                _ACTIVE_QUEUE_JOB_STATES,
                _queue_identity_key(lane, ""),
                limit,
            ],
        )
        return [row[0] for row in self.env.cr.fetchall()]

    @api.model
    def _lock_invalid_group_roster_waiter_ids(
        self,
        limit,
        profile_ids=None,
        profile_unavailable=False,
        profile_failed=False,
    ):
        """Lock a bounded batch whose group dependency cannot currently recover.

        Provider health is deliberately absent from this predicate. A connected
        session may be temporarily paused or disconnected and its waiter must stay
        durable. Archiving or rotating one of the stable scope records is terminal;
        a permanent metadata-contract failure is selected only by the dedicated
        ``profile_failed`` lane so it can be classified as ``dead`` rather than
        ``unsupported``.
        """

        limit = max(0, min(int(limit), _QUEUE_RECOVERY_MAX_BATCH))
        if not limit:
            return []
        self.flush_model(
            [
                "state",
                "provider_connection_id",
                "account_id",
                "waiting_group_profile_id",
                "waiting_group_roster_after",
            ]
        )
        self.env["contact.center.group.profile"].sudo().flush_model(
            [
                "channel_binding_id",
                "provider_connection_id",
                "account_id",
                "metadata_state",
                "last_error_class",
            ]
        )
        self.env["contact.center.channel.binding"].sudo().flush_model(
            [
                "channel_id",
                "account_id",
                "identity_id",
                "conversation_type",
                "merged_into_id",
                "active",
            ]
        )
        self.env["mail.channel"].sudo().flush_model(["active", "channel_type"])
        self.env["contact.center.provider.connection"].sudo().flush_model(
            ["account_id", "active"]
        )
        self.env["contact.center.account"].sudo().flush_model(["active"])
        profile_clause = ""
        parameters = []
        normalized_profile_ids = sorted(
            {
                int(profile_id)
                for profile_id in (profile_ids or [])
                if isinstance(profile_id, int)
                and not isinstance(profile_id, bool)
                and profile_id > 0
            }
        )
        if profile_ids is not None:
            if not normalized_profile_ids:
                return []
            profile_clause = "AND ledger.waiting_group_profile_id = ANY(%s)"
            parameters.append(normalized_profile_ids)
        if profile_unavailable and profile_ids is None:
            raise ValidationError(
                _("Unavailable group waiters require an explicit profile scope.")
            )
        if profile_unavailable and profile_failed:
            raise ValidationError(
                _("A group waiter cannot be unavailable and failed simultaneously.")
            )
        if profile_unavailable:
            invalid_scope_clause = "TRUE"
        elif profile_failed:
            invalid_scope_clause = """
                profile.metadata_state = 'failed'
                AND profile.provider_connection_id = ledger.provider_connection_id
                AND profile.account_id = ledger.account_id
                AND binding.account_id = ledger.account_id
                AND connection.account_id = ledger.account_id
                AND binding.conversation_type = 'group'
                AND binding.identity_id IS NULL
                AND binding.active IS TRUE
                AND binding.merged_into_id IS NULL
                AND channel.active IS TRUE
                AND channel.channel_type = 'contact_center'
                AND connection.active IS TRUE
                AND account.active IS TRUE
            """
        else:
            invalid_scope_clause = """
                ledger.waiting_group_profile_id IS NULL
                OR ledger.waiting_group_roster_after IS NULL
                OR profile.id IS NULL
                OR binding.id IS NULL
                OR channel.id IS NULL
                OR connection.id IS NULL
                OR account.id IS NULL
                OR profile.last_error_class = 'UnsupportedEventError'
                OR profile.provider_connection_id
                    IS DISTINCT FROM ledger.provider_connection_id
                OR profile.account_id IS DISTINCT FROM ledger.account_id
                OR binding.account_id IS DISTINCT FROM ledger.account_id
                OR connection.account_id IS DISTINCT FROM ledger.account_id
                OR binding.conversation_type IS DISTINCT FROM 'group'
                OR binding.identity_id IS NOT NULL
                OR binding.active IS NOT TRUE
                OR binding.merged_into_id IS NOT NULL
                OR channel.active IS NOT TRUE
                OR channel.channel_type IS DISTINCT FROM 'contact_center'
                OR connection.active IS NOT TRUE
                OR account.active IS NOT TRUE
            """
        parameters.append(limit)
        self.env.cr.execute(
            """
            SELECT ledger.id
              FROM contact_center_inbox_event AS ledger
         LEFT JOIN contact_center_group_profile AS profile
                ON profile.id = ledger.waiting_group_profile_id
         LEFT JOIN contact_center_channel_binding AS binding
                ON binding.id = profile.channel_binding_id
         LEFT JOIN mail_channel AS channel ON channel.id = binding.channel_id
         LEFT JOIN contact_center_provider_connection AS connection
                ON connection.id = ledger.provider_connection_id
         LEFT JOIN contact_center_account AS account
                ON account.id = ledger.account_id
             WHERE ledger.state = 'pending'
               AND (
                    ledger.waiting_group_profile_id IS NOT NULL
                    OR ledger.waiting_group_roster_after IS NOT NULL
               )
               {profile_clause}
               AND ({invalid_scope_clause})
          ORDER BY ledger.id
             FOR UPDATE OF ledger SKIP LOCKED
             LIMIT %s
            """.format(
                profile_clause=profile_clause,
                invalid_scope_clause=invalid_scope_clause,
            ),
            parameters,
        )
        return [row[0] for row in self.env.cr.fetchall()]

    @api.model
    def _finalize_invalid_group_roster_waiters(
        self, limit=100, profile_ids=None, profile_unavailable=False
    ):
        waiter_ids = self._lock_invalid_group_roster_waiter_ids(
            limit,
            profile_ids=profile_ids,
            profile_unavailable=profile_unavailable,
        )
        waiters = self.sudo().browse(waiter_ids).exists()
        error_message = (
            "group metadata is unavailable for deferred authorization"
            if profile_unavailable
            else "deferred group roster scope is no longer active"
        )
        error = UnsupportedEventError(error_message)
        for waiter in waiters:
            waiter._finish_failure("unsupported", error, attempts=waiter.attempts)
        if waiters:
            _logger.info(
                "Finalized %s Contact Center group-roster waiters with inactive scope",
                len(waiters),
            )
        return len(waiters)

    @api.model
    def _finalize_failed_group_roster_waiters(self, limit=100, profile_ids=None):
        """Dead-letter waiters whose roster pull failed permanently.

        ``failed`` is produced only after a non-retryable DTO/adapter/validation
        error or after the metadata retry ceiling.  The profile itself remains
        administratively retryable, while each dependent inbox event becomes an
        explicit dead letter that can be requeued after the root cause is fixed.
        """

        return self._finalize_failed_group_roster_waiters_locked(
            limit=limit,
            profile_ids=profile_ids,
            profile_transition_locked=False,
        )

    @api.model
    def _lock_failed_group_roster_profile_ids(self, limit, profile_ids=None):
        """Lock failed profiles in the canonical channel -> profile order."""

        limit = max(0, min(int(limit), _QUEUE_RECOVERY_MAX_BATCH))
        if not limit:
            return []
        normalized_profile_ids = sorted(
            {
                int(profile_id)
                for profile_id in (profile_ids or [])
                if isinstance(profile_id, int)
                and not isinstance(profile_id, bool)
                and profile_id > 0
            }
        )
        profile_clause = ""
        parameters = []
        if profile_ids is not None:
            if not normalized_profile_ids:
                return []
            profile_clause = "AND profile.id = ANY(%s)"
            parameters.append(normalized_profile_ids)
        self.env["contact.center.group.profile"].sudo().flush_model(
            [
                "channel_binding_id",
                "provider_connection_id",
                "account_id",
                "metadata_state",
            ]
        )
        self.env["contact.center.channel.binding"].sudo().flush_model(
            [
                "channel_id",
                "account_id",
                "identity_id",
                "conversation_type",
                "merged_into_id",
                "active",
            ]
        )
        self.env["mail.channel"].sudo().flush_model(["active", "channel_type"])
        self.env["contact.center.provider.connection"].sudo().flush_model(
            ["account_id", "active"]
        )
        self.env["contact.center.account"].sudo().flush_model(["active"])
        self.flush_model(
            [
                "state",
                "provider_connection_id",
                "account_id",
                "waiting_group_profile_id",
                "waiting_group_roster_after",
            ]
        )
        parameters.append(limit)
        self.env.cr.execute(
            """
            SELECT profile.id
              FROM contact_center_group_profile AS profile
              JOIN contact_center_channel_binding AS binding
                ON binding.id = profile.channel_binding_id
              JOIN mail_channel AS channel ON channel.id = binding.channel_id
              JOIN contact_center_provider_connection AS connection
                ON connection.id = profile.provider_connection_id
              JOIN contact_center_account AS account ON account.id = profile.account_id
             WHERE profile.metadata_state = 'failed'
               AND binding.account_id = profile.account_id
               AND connection.account_id = profile.account_id
               AND binding.conversation_type = 'group'
               AND binding.identity_id IS NULL
               AND binding.active IS TRUE
               AND binding.merged_into_id IS NULL
               AND channel.active IS TRUE
               AND channel.channel_type = 'contact_center'
               AND connection.active IS TRUE
               AND account.active IS TRUE
               AND EXISTS (
                    SELECT 1
                      FROM contact_center_inbox_event AS ledger
                     WHERE ledger.state = 'pending'
                       AND ledger.waiting_group_profile_id = profile.id
                       AND ledger.waiting_group_roster_after IS NOT NULL
                       AND ledger.provider_connection_id =
                           profile.provider_connection_id
                       AND ledger.account_id = profile.account_id
               )
               {profile_clause}
          ORDER BY channel.id, profile.id
             FOR UPDATE OF channel SKIP LOCKED
             LIMIT %s
            """.format(
                profile_clause=profile_clause
            ),
            parameters,
        )
        candidate_ids = [row[0] for row in self.env.cr.fetchall()]
        if not candidate_ids:
            return []
        # A concurrent recovery takes channel -> profile in this same order. If it
        # changed the profile after our REPEATABLE READ snapshot, PostgreSQL raises
        # a serialization error here and the cron retries instead of dead-lettering
        # a waiter from stale evidence.
        self.env.cr.execute(
            """
            SELECT id
              FROM contact_center_group_profile
             WHERE id = ANY(%s)
               AND metadata_state = 'failed'
          ORDER BY id
             FOR UPDATE
            """,
            [candidate_ids],
        )
        return [row[0] for row in self.env.cr.fetchall()]

    @api.model
    def _finalize_failed_group_roster_waiters_locked(
        self,
        limit=100,
        profile_ids=None,
        profile_transition_locked=False,
    ):
        """Finalize failed-profile waiters after serializing profile recovery."""

        limit = max(0, min(int(limit), _QUEUE_RECOVERY_MAX_BATCH))
        if not limit:
            return 0
        if not profile_transition_locked:
            locked_profile_ids = self._lock_failed_group_roster_profile_ids(
                limit,
                profile_ids=profile_ids,
            )
            finalized = 0
            for profile_id in locked_profile_ids:
                finalized += self._finalize_failed_group_roster_waiters_locked(
                    limit=limit - finalized,
                    profile_ids=[profile_id],
                    profile_transition_locked=True,
                )
                if finalized >= limit:
                    break
            return finalized

        waiter_ids = self._lock_invalid_group_roster_waiter_ids(
            limit, profile_ids=profile_ids, profile_failed=True
        )
        waiters = self.sudo().browse(waiter_ids).exists()
        waiters.mapped("waiting_group_profile_id").invalidate_recordset(
            ["last_error_class", "sync_revision", "attempts"]
        )
        error = GroupRosterMetadataFailedError(
            "group roster metadata failed before deferred authorization completed"
        )
        recorded_at = fields.Datetime.to_string(fields.Datetime.now())
        for waiter in waiters:
            profile = waiter.waiting_group_profile_id
            metadata = dict(waiter.metadata_json or {})
            metadata["group_roster_terminal_failure"] = {
                "profile_id": profile.id,
                "profile_error_class": profile.last_error_class or "UnknownError",
                "profile_sync_revision": profile.sync_revision,
                "profile_attempts": profile.attempts,
                "recorded_at": recorded_at,
            }
            waiter.sudo().write({"metadata_json": metadata})
            waiter._finish_failure("dead", error, attempts=waiter.attempts)
        if waiters:
            _logger.info(
                "Dead-lettered %s Contact Center group-roster waiters after a "
                "permanent metadata failure",
                len(waiters),
            )
        return len(waiters)

    @api.model
    def _cron_recover_orphaned_queue_jobs(
        self,
        limit_per_model=_QUEUE_RECOVERY_LIMIT_PER_MODEL,
        grace_seconds=_QUEUE_RECOVERY_GRACE_SECONDS,
    ):
        """Coordinate recovery lanes; Inbox is only the cron's technical host."""

        limit_per_model = max(0, min(int(limit_per_model), _QUEUE_RECOVERY_MAX_BATCH))
        grace_seconds = max(0, int(grace_seconds))
        recovered = {"inbox": 0, "outbox": 0, "media": 0}
        if not limit_per_model:
            return recovered
        self._finalize_failed_group_roster_waiters(limit=limit_per_model)
        self._finalize_invalid_group_roster_waiters(limit=limit_per_model)
        cutoff = fields.Datetime.now() - datetime.timedelta(seconds=grace_seconds)
        for lane, specification in _QUEUE_RECOVERY_LANES.items():
            record_ids = self._lock_orphaned_queue_record_ids(
                lane, cutoff, limit_per_model
            )
            records = (
                self.env[specification["model"]].sudo().browse(record_ids).exists()
            )
            for record in records:
                record.invalidate_recordset(["state", "queue_job_uuid"])
                previous_uuid = record.queue_job_uuid
                existing_job, revived = _reuse_queue_job(record, lane)
                if existing_job:
                    recovered[lane] += int(
                        revived or previous_uuid != existing_job.uuid
                    )
                    continue
                if record.queue_job_uuid:
                    record.write({"queue_job_uuid": False})
                getattr(record, specification["enqueue_method"])()
                record.invalidate_recordset(["queue_job_uuid"])
                recovered[lane] += int(bool(record.queue_job_uuid))
        if any(recovered.values()):
            _logger.info("Recovered orphaned Contact Center jobs: %s", recovered)
        return recovered

    def _job_process(self):
        self.ensure_one()
        job_uuid = self.env.context.get("job_uuid")
        if not queue_job_owns_record(self):
            return False
        if self.state in ("done", "blocked", "unsupported", "dead"):
            return False

        attempt = self._job_attempt_number(job_uuid)
        try:
            # This must precede every inbox write. ``account_id`` is a stored
            # related FK on this ledger; writing the ledger first can leave two
            # workers holding referential row locks that both try to promote to
            # ``FOR UPDATE`` during projection.  Starting account -> inbox avoids
            # that promotion deadlock and matches the topology lock order.
            self.env["contact.center.application"]._lock_inbound_account_scope(
                self.account_id.sudo()
            )
            self.write({"state": "processing", "attempts": attempt})
            with self.env.cr.savepoint():
                event_dto = self._normalize_one()
            if event_dto.message is None:
                # A standalone attribution observation has no other projection.  Its
                # ledger write is therefore the primary business operation and must
                # fail visibly/recoverably instead of being downgraded to optional
                # telemetry.  Attribution attached to a message remains best-effort:
                # malformed campaign evidence must not discard customer content.
                with self.env.cr.savepoint():
                    attribution_touchpoints = self._capture_attribution(event_dto)
            else:
                attribution_touchpoints = self._capture_attribution_best_effort(
                    event_dto
                )
            with self.env.cr.savepoint():
                self._process_normalized(
                    event_dto, attribution_touchpoints=attribution_touchpoints
                )
        except IdentityConflictError as error:
            self._record_identity_conflict(error)
            self._finish_failure("blocked", error, attempts=attempt)
        except GroupRosterRefreshRequired as error:
            return self._defer_for_group_roster(error, attempt)
        except UnsupportedEventError as error:
            self._finish_failure("unsupported", error, attempts=attempt)
        except ProviderPausedError as error:
            raise RetryableJobError(
                str(error),
                seconds=provider_paused_retry_seconds(error, ("inbox", self.id)),
                ignore_retry=True,
            ) from error
        except (TransientAdapterError, AmbiguousTimeoutError) as error:
            if attempt >= QUEUE_ATTEMPT_CEILING:
                self._finish_failure("dead", error, attempts=attempt)
                return False
            raise RetryableJobError(str(error), seconds=_retry_delay(error)) from error
        except (AdapterError, DTOValidationError, ValidationError) as error:
            self._finish_failure("dead", error, attempts=attempt)
        except Exception as error:  # queue job isolation boundary
            _logger.exception(
                "Unexpected contact center inbox error for event %s", self.id
            )
            if attempt >= QUEUE_ATTEMPT_CEILING:
                self._finish_failure("dead", error, attempts=attempt)
                return False
            raise RetryableJobError(str(error), seconds=_retry_delay(error)) from error
        return True

    def _defer_for_group_roster(self, error, attempt):
        """Commit a roster dependency instead of losing it to retry rollback.

        OCA ``queue_job`` deliberately rolls the business cursor back when a job
        raises ``RetryableJobError``.  The projection savepoint has already been
        rolled back when this method runs, so scheduling the group refresh here
        and returning normally makes both the dependency and the waiting inbox
        state durable.
        """

        self.ensure_one()
        profile = (
            self.env["contact.center.group.profile"]
            .sudo()
            .browse(error.group_profile_id)
            .exists()
        )
        connection = self.provider_connection_id.sudo()
        if (
            not profile
            or error.provider_connection_id != connection.id
            or profile.provider_connection_id != connection
            or profile.account_id != self.account_id
        ):
            scope_error = ValidationError(
                _("The requested group roster refresh is outside the inbox scope.")
            )
            self._finish_failure("dead", scope_error, attempts=attempt)
            return False

        now = fields.Datetime.now()
        wait_count = self.group_roster_wait_count + 1
        wait_observability = {
            "group_roster_wait_count": wait_count,
            "first_group_roster_wait_at": self.first_group_roster_wait_at or now,
        }
        if attempt >= QUEUE_ATTEMPT_CEILING:
            self.sudo().write(wait_observability)
            ceiling_error = ValidationError(
                _(
                    "Group roster authorization remained unresolved after %s "
                    "processing attempts.",
                    attempt,
                )
            )
            self._finish_failure("dead", ceiling_error, attempts=attempt)
            return False

        profile.invalidate_recordset(["sync_requested_at", "queue_job_uuid"])
        sync_requested_at = fields.Datetime.to_datetime(profile.sync_requested_at)
        # Reuse only a refresh requested after this event. An older active job may
        # return a perfectly valid snapshot that still predates a promotion or
        # demotion; superseding its revision makes it schedule one successor.
        refresh_already_covers_event = bool(
            sync_requested_at
            and sync_requested_at >= error.required_after
            and profile._has_active_queue_job(adopt=False)
        )
        profile.with_context(contact_center_skip_enqueue=False)._request_sync(
            connection,
            force=True,
            skip_if_active=refresh_already_covers_event,
        )
        metadata = dict(self.metadata_json or {})
        metadata.update(
            {
                "group_roster_deferred_at": fields.Datetime.to_string(now),
                "group_roster_wait_count": wait_count,
                "first_group_roster_wait_at": fields.Datetime.to_string(
                    wait_observability["first_group_roster_wait_at"]
                ),
                "group_roster_profile_id": profile.id,
                "group_roster_required_after": fields.Datetime.to_string(
                    error.required_after
                ),
            }
        )
        self.sudo().write(
            {
                "state": "pending",
                "attempts": attempt,
                "queue_job_uuid": False,
                "processed_at": False,
                "last_error_class": error.__class__.__name__,
                "last_error_message": str(error)[:4000],
                "metadata_json": metadata,
                "waiting_group_profile_id": profile.id,
                "waiting_group_roster_after": error.required_after,
                **wait_observability,
            }
        )
        return False

    def _job_attempt_number(self, job_uuid):
        self.ensure_one()
        if job_uuid:
            job = (
                self.env["queue.job"].sudo().search([("uuid", "=", job_uuid)], limit=1)
            )
            if job:
                # Inbox retries roll back the ledger transaction, while queue_job
                # persists its own retry counter.  The ledger value is therefore
                # the baseline from a previous/legacy job and job.retry is the
                # number of failed executions of the current job.
                return self.attempts + job.retry + 1
        return self.attempts + 1

    def _record_identity_conflict(self, error):
        self.ensure_one()
        if not error.conflict_values:
            return False
        conflict_values = dict(error.conflict_values)
        conflict_values.setdefault("inbox_event_id", self.id)
        identity_commands = conflict_values.get("identity_ids") or []
        identity_ids = identity_commands[0][2] if identity_commands else []
        existing_identity_ids = []
        if identity_ids:
            self.env.cr.execute(
                "SELECT id FROM contact_center_identity WHERE id = ANY(%s)",
                [list(identity_ids)],
            )
            existing_identity_ids = [row[0] for row in self.env.cr.fetchall()]
        if existing_identity_ids:
            conflict_values["identity_ids"] = [(6, 0, existing_identity_ids)]
            self.env["contact.center.identity.conflict"].sudo().create(conflict_values)
        return True

    def _process_one(self):
        self.ensure_one()
        event_dto = self._normalize_one()
        return self._process_normalized(event_dto)

    def _normalize_one(self):
        self.ensure_one()
        adapter = self.provider_connection_id.get_adapter()
        event_dto = adapter.normalize_event(
            self.provider_connection_id, self.raw_envelope_json
        )
        if isinstance(event_dto, dict):
            event_dto = EventDTO.from_dict(event_dto)
        if not isinstance(event_dto, EventDTO):
            raise AdapterError("normalize_event must return EventDTO")
        self.normalized_dto_json = event_dto.to_dict()
        return event_dto

    def _capture_attribution(self, event_dto):
        self.ensure_one()
        application = self.env["contact.center.application"]
        application._validate_event_scope(self.provider_connection_id, event_dto)
        return self.env["contact.center.attribution.touchpoint"]._capture_event(
            self.provider_connection_id,
            event_dto,
            self,
        )

    def _capture_attribution_best_effort(self, event_dto):
        """Capture optional acquisition evidence without dropping the message."""

        self.ensure_one()
        if event_dto.message is None:
            return self._capture_attribution(event_dto)
        try:
            # Keep attribution evidence outside the projection savepoint. An
            # unsupported message can still commit a valid acquisition touchpoint.
            with self.env.cr.savepoint():
                touchpoints = self._capture_attribution(event_dto)
            metadata = dict(self.metadata_json or {})
            if metadata.pop("attribution_capture_failure", None):
                self.sudo().write({"metadata_json": metadata})
            return touchpoints
        except (DTOValidationError, ValidationError) as error:
            # Attribution is telemetry. A bounded domain/DTO mismatch must not turn
            # an otherwise valid customer message into a terminal inbox event.
            _logger.warning(
                "Ignored invalid attribution for inbox event %s (%s)",
                self.id,
                error.__class__.__name__,
            )
            metadata = dict(self.metadata_json or {})
            metadata["attribution_capture_failure"] = {
                "error_class": error.__class__.__name__,
                "recorded_at": fields.Datetime.to_string(fields.Datetime.now()),
            }
            self.sudo().write({"metadata_json": metadata})
            return self.env["contact.center.attribution.touchpoint"]

    def _process_normalized(self, event_dto, attribution_touchpoints=None):
        self.ensure_one()
        if not isinstance(event_dto, EventDTO):
            raise DTOValidationError("normalized inbox event must be an EventDTO")
        if attribution_touchpoints is None:
            attribution_touchpoints = self._capture_attribution(event_dto)
        projection = self.env["contact.center.application"]._process_event(
            self.provider_connection_id, event_dto, inbox_event=self
        )
        attribution_touchpoints._link_projection(
            self.provider_connection_id,
            event_dto,
            projection=projection,
        )
        if event_dto.event_type == "attribution.observed":
            attribution_touchpoints.filtered(
                lambda touchpoint: not touchpoint.channel_binding_id
            )._enqueue_projection_reconciliation()
        self.write(
            {
                "state": "done",
                "processed_at": fields.Datetime.now(),
                "last_error_class": False,
                "last_error_message": False,
                "waiting_group_profile_id": False,
                "waiting_group_roster_after": False,
            }
        )
        return True

    def _finish_failure(self, state, error, attempts=None):
        self.ensure_one()
        self.write(
            {
                "state": state,
                "attempts": self.attempts if attempts is None else attempts,
                "processed_at": fields.Datetime.now(),
                "last_error_class": error.__class__.__name__,
                "last_error_message": str(error)[:4000],
                "waiting_group_profile_id": False,
                "waiting_group_roster_after": False,
            }
        )


class ContactCenterOutboxCommand(models.Model):
    _name = "contact.center.outbox.command"
    _description = "Contact Center Outbox Command"
    _order = "create_date, id"

    account_id = fields.Many2one(
        "contact.center.account", required=True, index=True, ondelete="restrict"
    )
    company_id = fields.Many2one(
        related="account_id.company_id", store=True, readonly=True, index=True
    )
    provider_connection_id = fields.Many2one(
        "contact.center.provider.connection",
        required=True,
        index=True,
        ondelete="restrict",
    )
    channel_binding_id = fields.Many2one(
        "contact.center.channel.binding", required=True, index=True, ondelete="cascade"
    )
    message_binding_id = fields.Many2one(
        "contact.center.message.binding", index=True, ondelete="cascade"
    )
    target_message_binding_id = fields.Many2one(
        "contact.center.message.binding", index=True, ondelete="cascade"
    )
    mutation_id = fields.Many2one(
        "contact.center.message.mutation", index=True, ondelete="cascade"
    )
    ui_request_id = fields.Char(index=True, copy=False, readonly=True)
    retry_of_id = fields.Many2one(
        "contact.center.outbox.command",
        string="Retry Of",
        readonly=True,
        copy=False,
        index=True,
        ondelete="restrict",
        help=(
            "Terminal send command that authorized this new, independently "
            "dispatched attempt. The source command is never reopened."
        ),
    )
    retry_child_ids = fields.One2many(
        "contact.center.outbox.command",
        "retry_of_id",
        string="Retry Attempts",
        readonly=True,
    )
    retry_requested_at = fields.Datetime(readonly=True, copy=False, index=True)
    retry_requested_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False, ondelete="restrict", index=True
    )
    retry_admission_revision = fields.Integer(
        default=0,
        required=True,
        readonly=True,
        copy=False,
        help=(
            "Monotonic write barrier used to converge concurrent retry requests "
            "under PostgreSQL REPEATABLE READ."
        ),
    )
    outbox_idempotency_key = fields.Char(required=True, index=True, copy=False)
    command_type = fields.Char(required=True, index=True)
    command_json = fields.Json(required=True, copy=False)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("retry", "Retry"),
            ("done", "Done"),
            ("uncertain", "Uncertain"),
            ("dead", "Dead"),
            ("cancelled", "Cancelled"),
        ],
        required=True,
        default="pending",
        index=True,
        copy=False,
    )
    attempts = fields.Integer(default=0, required=True, copy=False)
    queue_job_uuid = fields.Char(readonly=True, copy=False, index=True)
    has_active_queue_job = fields.Boolean(
        compute="_compute_has_active_queue_job",
    )
    dispatch_job_uuid = fields.Char(readonly=True, copy=False, index=True)
    dispatch_started_at = fields.Datetime(readonly=True, copy=False, index=True)
    processed_at = fields.Datetime(copy=False)
    provider_request_json = fields.Json(
        string="Provider Request (Sanitized)", readonly=True, copy=False
    )
    provider_response_json = fields.Json(copy=False)
    last_error_class = fields.Char(copy=False)
    last_error_message = fields.Text(copy=False)
    resolution = fields.Selection(
        [
            ("external_delivery_confirmed", "External Delivery Confirmed"),
            ("external_application_confirmed", "External Application Confirmed"),
            ("closed_without_send", "Closed Without Sending"),
        ],
        readonly=True,
        copy=False,
        index=True,
    )
    resolution_reason = fields.Text(readonly=True, copy=False)
    resolved_at = fields.Datetime(readonly=True, copy=False, index=True)
    resolved_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False, ondelete="restrict", index=True
    )

    _sql_constraints = [
        (
            "account_idempotency_unique",
            "unique(account_id, outbox_idempotency_key)",
            "This outbound command already exists for the account.",
        ),
        (
            "attempts_nonnegative",
            "check(attempts >= 0)",
            "Attempts cannot be negative.",
        ),
        (
            "channel_ui_request_unique",
            "unique(channel_binding_id, ui_request_id)",
            "This UI send request already exists for the conversation.",
        ),
        (
            "retry_source_unique",
            "unique(retry_of_id)",
            "A terminal outbound command can create only one direct retry attempt.",
        ),
        (
            "retry_admission_revision_nonnegative",
            "check(retry_admission_revision >= 0)",
            "The retry admission revision cannot be negative.",
        ),
    ]

    def init(self):
        """Install the recovery index only after the outbox table exists."""

        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS cc_outbox_recovery_eligible_idx
                ON contact_center_outbox_command (provider_connection_id, id)
             WHERE state IN ('pending', 'retry')
               AND dispatch_job_uuid IS NULL
               AND dispatch_started_at IS NULL
            """
        )

    @api.constrains(
        "retry_of_id",
        "retry_requested_at",
        "retry_requested_by_id",
        "command_type",
        "account_id",
        "channel_binding_id",
        "message_binding_id",
    )
    def _check_retry_lineage(self):
        for command in self:
            source = command.retry_of_id
            retry_audit = (
                command.retry_requested_at,
                command.retry_requested_by_id,
            )
            if not source:
                if any(retry_audit):
                    raise ValidationError(
                        _("An outbound retry audit record requires its source command.")
                    )
                continue
            if not all(retry_audit):
                raise ValidationError(
                    _("An outbound retry requires a complete audit record.")
                )
            source_binding = source.message_binding_id
            if (
                source == command
                or source.id >= command.id
                or source.command_type != "send_message"
                or command.command_type != "send_message"
                or not source_binding
                or source.state != "dead"
                or source.resolution
                or source_binding.direction != "outbound"
                or source_binding.origin != "agent"
                or source_binding.delivery_state != "failed"
                or source_binding.external_message_id
                or source.account_id != command.account_id
                or source.channel_binding_id != command.channel_binding_id
                or source_binding == command.message_binding_id
                or command.message_binding_id.direction != "outbound"
                or command.message_binding_id.origin != "agent"
            ):
                raise ValidationError(
                    _(
                        "An outbound retry must follow one unequivocally failed "
                        "send in the same conversation."
                    )
                )

    @api.constrains(
        "resolution",
        "resolution_reason",
        "resolved_at",
        "resolved_by_id",
        "state",
        "command_type",
        "message_binding_id",
        "mutation_id",
    )
    def _check_resolution_audit(self):
        for command in self:
            audit_values = (
                command.resolution_reason,
                command.resolved_at,
                command.resolved_by_id,
            )
            if not command.resolution:
                if any(audit_values):
                    raise ValidationError(
                        _("An outbox resolution audit record is incomplete.")
                    )
                continue
            if not all(audit_values) or not command.resolution_reason.strip():
                raise ValidationError(
                    _("A resolved outbox command requires a complete audit record.")
                )
            expected_state = command._validate_uncertain_resolution(command.resolution)
            if command.state != expected_state:
                raise ValidationError(
                    _("The outbox state conflicts with its recorded resolution.")
                )

    @api.constrains(
        "provider_connection_id",
        "account_id",
        "channel_binding_id",
        "message_binding_id",
        "target_message_binding_id",
        "mutation_id",
        "command_type",
    )
    def _check_accounts(self):
        for command in self:
            if command.provider_connection_id.account_id != command.account_id:
                raise ValidationError(
                    _("The provider connection belongs to another account.")
                )
            if command.channel_binding_id.account_id != command.account_id:
                raise ValidationError(_("The conversation belongs to another account."))
            binding = command.channel_binding_id
            if binding.conversation_type not in ("direct", "group"):
                raise ValidationError(
                    _("This conversation type cannot create outbound commands.")
                )
            if binding.conversation_type == "group":
                command._check_group_command_shape()
            if command.message_binding_id and (
                command.message_binding_id.account_id != command.account_id
                or command.message_binding_id.channel_binding_id
                != command.channel_binding_id
                or command.message_binding_id.provider_connection_id
                != command.provider_connection_id
            ):
                raise ValidationError(
                    _(
                        "The message binding must belong to the command conversation "
                        "and provider connection."
                    )
                )
            if command.target_message_binding_id and (
                command.target_message_binding_id.account_id != command.account_id
                or command.target_message_binding_id.channel_binding_id
                != command.channel_binding_id
                or command.target_message_binding_id.provider_connection_id
                != command.provider_connection_id
            ):
                raise ValidationError(
                    _(
                        "The target message must belong to the command conversation "
                        "and provider connection."
                    )
                )
            if command.command_type == "send_message":
                if not command.message_binding_id or command.target_message_binding_id:
                    raise ValidationError(
                        _("A send command requires only its created message binding.")
                    )
                reply_target = command.message_binding_id.reply_to_binding_id
                if reply_target and (
                    reply_target.channel_binding_id != command.channel_binding_id
                    or reply_target.provider_connection_id
                    != command.provider_connection_id
                ):
                    raise ValidationError(
                        _(
                            "A reply target must belong to the command provider "
                            "connection."
                        )
                    )
            elif command.command_type in ("react", "edit_message", "delete_message"):
                if command.message_binding_id or not command.target_message_binding_id:
                    raise ValidationError(
                        _("A mutation command requires only a target message binding.")
                    )
                if (
                    not command.mutation_id
                    or command.mutation_id.target_message_binding_id
                    != command.target_message_binding_id
                ):
                    raise ValidationError(
                        _("A mutation command requires its matching mutation ledger.")
                    )

    def _check_group_command_shape(self):
        self.ensure_one()
        binding = self.channel_binding_id
        profile = binding.group_profile_ids[:1]
        if not profile or self.provider_connection_id != profile.provider_connection_id:
            raise ValidationError(
                _(
                    "Group outbound commands require the observing provider "
                    "connection."
                )
            )
        if self.command_type == "send_message":
            message_binding = self.message_binding_id
            media = (
                message_binding.media_ids
                if message_binding
                else self.env["contact.center.media.binding"]
            )
            reply_target = (
                message_binding.reply_to_binding_id
                if message_binding
                else self.env["contact.center.message.binding"]
            )
            media_shape_valid = bool(
                len(media) <= 1
                and (
                    (
                        not media
                        and message_binding
                        and message_binding.content_type == "text"
                    )
                    or (
                        len(media) == 1
                        and message_binding.content_type == media.kind
                        and media.state == "ready"
                        and media.attachment_id
                    )
                )
            )
            reply_shape_valid = bool(
                not reply_target
                or (
                    reply_target.channel_binding_id == binding
                    and reply_target.provider_connection_id
                    == self.provider_connection_id
                    and reply_target.external_message_id
                    and reply_target.message_state != "deleted"
                )
            )
            if (
                not message_binding
                or message_binding.provider_connection_id != self.provider_connection_id
                or message_binding.direction != "outbound"
                or message_binding.origin != "agent"
                or not message_binding.client_message_id
                or not media_shape_valid
                or not reply_shape_valid
                or self.target_message_binding_id
                or self.mutation_id
            ):
                raise ValidationError(
                    _(
                        "Group sends require one canonical message on the "
                        "observing provider connection."
                    )
                )
            return
        if self.command_type not in ("react", "edit_message", "delete_message"):
            raise ValidationError(_("This group outbound command type is unsupported."))
        target = self.target_message_binding_id
        if (
            self.message_binding_id
            or not target
            or target.provider_connection_id != self.provider_connection_id
            or not target.external_message_id
            or not target.protocol_participant_json
            or not self.mutation_id
            or (
                self.command_type in ("edit_message", "delete_message")
                and (target.direction != "outbound" or target.origin != "agent")
            )
        ):
            raise ValidationError(
                _(
                    "Group mutations require one canonical target on the observing "
                    "provider connection."
                )
            )

    @api.model_create_multi
    def create(self, vals_list):
        commands = super().create(vals_list)
        if not self.env.context.get("contact_center_skip_enqueue"):
            commands._enqueue()
        return commands

    def _enqueue(self, eta=None):
        for command in self.filtered(lambda item: item.state in ("pending", "retry")):
            existing_job, _recovered = _reuse_queue_job(command, "outbox")
            if existing_job:
                continue
            if command.queue_job_uuid:
                command.sudo().write({"queue_job_uuid": False})
            delayed = (
                command.sudo()
                .with_company(command.company_id)
                .with_delay(
                    identity_key="contact_center:outbox:%s" % command.id,
                    max_retries=0,
                    description="Contact Center outbox command %s" % command.id,
                    eta=eta,
                )
                ._job_process()
            )
            command.sudo().write({"queue_job_uuid": delayed.uuid})
        return True

    def _has_active_queue_job(self):
        self.ensure_one()
        return bool(_related_queue_job(self, "outbox", _ACTIVE_QUEUE_JOB_STATES))

    @api.depends("queue_job_uuid")
    def _compute_has_active_queue_job(self):
        for command in self:
            command.has_active_queue_job = command._has_active_queue_job()

    def write(self, values):
        protected_fields = {
            "resolution",
            "resolution_reason",
            "resolved_at",
            "resolved_by_id",
        }
        immutable_retry_fields = {
            "retry_of_id",
            "retry_requested_at",
            "retry_requested_by_id",
            "retry_admission_revision",
        }
        if immutable_retry_fields.intersection(values):
            raise AccessError(_("Outbox retry audit fields are immutable."))
        if protected_fields.intersection(values) and not self.env.context.get(
            "contact_center_uncertain_resolution"
        ):
            raise AccessError(
                _(
                    "Outbox resolution audit fields can only be written through "
                    "the administrative resolution workflow."
                )
            )
        if protected_fields.intersection(values):
            for command in self:
                if command.resolution:
                    changed = any(
                        values.get(field_name) != command[field_name]
                        for field_name in protected_fields.intersection(values)
                    )
                    if changed:
                        raise ValidationError(
                            _("An outbox resolution audit record is immutable.")
                        )
        return super().write(values)

    def _contact_center_admit_safe_retry(self):
        """Create a write barrier without reopening the terminal source command."""

        self.ensure_one()
        message_binding_id = self.message_binding_id.id
        if not message_binding_id:
            raise ValidationError(
                _("Only a permanently failed send can create a retry attempt.")
            )
        # Positive provider evidence locks this same projection before advancing
        # delivery. Holding it through admission makes the failed/no-correlation
        # decision one database snapshot, not two racy ORM reads.
        self.env.cr.execute(
            "SELECT id FROM contact_center_message_binding WHERE id = %s FOR UPDATE",
            [message_binding_id],
        )
        if not self.env.cr.fetchone():
            raise ValidationError(
                _("Only a permanently failed send can create a retry attempt.")
            )
        self.message_binding_id.invalidate_recordset(
            ["delivery_state", "external_message_id"]
        )
        if (
            self.message_binding_id.delivery_state != "failed"
            or self.message_binding_id.external_message_id
        ):
            raise ValidationError(
                _("Only a permanently failed send can create a retry attempt.")
            )
        self.env.cr.execute(
            """
            UPDATE contact_center_outbox_command
               SET retry_admission_revision = retry_admission_revision + 1
             WHERE id = %s
               AND state = 'dead'
               AND command_type = 'send_message'
         RETURNING retry_admission_revision
            """,
            [self.id],
        )
        row = self.env.cr.fetchone()
        self.invalidate_recordset(
            [
                "state",
                "command_type",
                "retry_admission_revision",
                "message_binding_id",
            ]
        )
        if not row:
            raise ValidationError(
                _("Only a permanently failed send can create a retry attempt.")
            )
        return int(row[0])

    def _check_uncertain_resolution_access(self):
        self.ensure_one()
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_admin"
        ):
            raise AccessError(
                _(
                    "Only Contact Center administrators can resolve uncertain "
                    "outbox commands."
                )
            )
        self.check_access_rights("read")
        self.check_access_rule("read")
        if self.company_id not in self.env.companies:
            raise AccessError(
                _("The uncertain command belongs to an unavailable company.")
            )
        return True

    def _validate_uncertain_resolution(self, resolution):
        self.ensure_one()
        if resolution == "external_delivery_confirmed":
            if self.command_type != "send_message" or not self.message_binding_id:
                raise ValidationError(
                    _(
                        "Only an uncertain send command can be confirmed as "
                        "externally delivered."
                    )
                )
            return "done"
        if resolution == "external_application_confirmed":
            if (
                self.command_type
                not in (
                    "react",
                    "edit_message",
                    "delete_message",
                )
                or not self.mutation_id
            ):
                raise ValidationError(
                    _(
                        "Only an uncertain mutation command can be confirmed as "
                        "externally applied."
                    )
                )
            return "done"
        if resolution == "closed_without_send":
            return "cancelled"
        raise ValidationError(_("Unsupported uncertain command resolution."))

    def _open_uncertain_resolution_wizard(self, resolution):
        self.ensure_one()
        self._check_uncertain_resolution_access()
        self._validate_uncertain_resolution(resolution)
        if self.state != "uncertain":
            raise ValidationError(
                _("Only an uncertain outbox command can be resolved manually.")
            )
        return {
            "type": "ir.actions.act_window",
            "name": _("Resolve Uncertain Outbox Command"),
            "res_model": "contact.center.outbox.resolution.wizard",
            "view_mode": "form",
            "view_id": self.env.ref(
                "contact_center_base.view_contact_center_outbox_resolution_wizard_form"
            ).id,
            "target": "new",
            "context": {
                "default_outbox_command_id": self.id,
                "default_requested_resolution": resolution,
            },
        }

    def action_confirm_external_delivery(self):
        return self._open_uncertain_resolution_wizard("external_delivery_confirmed")

    def action_confirm_external_application(self):
        return self._open_uncertain_resolution_wizard("external_application_confirmed")

    def action_close_without_send(self):
        return self._open_uncertain_resolution_wizard("closed_without_send")

    def _lock_for_uncertain_resolution(self, *, connection_write=False):
        """Lock in the same account -> connection -> outbox order as dispatch."""

        self.ensure_one()
        expected_account_id = self.account_id.id
        expected_connection_id = self.provider_connection_id.id
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id = %s FOR SHARE",
            [expected_account_id],
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection "
            "WHERE id = %s FOR " + ("UPDATE" if connection_write else "SHARE"),
            [expected_connection_id],
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_outbox_command WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()
        if (
            self.account_id.id != expected_account_id
            or self.provider_connection_id.id != expected_connection_id
        ):
            raise ValidationError(
                _("The scope of an outbox command cannot change during resolution.")
            )
        return True

    def _provider_success_reconciliation_evidence(self):
        """Return one strictly proven provider-success ID, or no evidence.

        Only a database concurrency failure raised while persisting an already
        validated successful adapter result is locally recoverable. Transport
        ambiguity, provider errors, missing correlation and invariant failures
        deliberately remain ``uncertain`` for external evidence or administrator
        review.
        """

        self.ensure_one()
        evidence = self.provider_response_json
        external_message_id = (
            evidence.get("external_message_id") if isinstance(evidence, dict) else None
        )
        if (
            self.state != "uncertain"
            or self.command_type != "send_message"
            or self.last_error_class != "PostDispatchPersistenceError"
            or self.resolution
            or not isinstance(evidence, dict)
            or evidence.get("dispatch_outcome") != "provider_returned_success"
            or evidence.get("local_error_class")
            not in _PROVIDER_SUCCESS_RECONCILIATION_ERROR_CLASSES
            or not isinstance(evidence.get("provider_response"), dict)
            or not isinstance(external_message_id, str)
            or not external_message_id.strip()
            or external_message_id != external_message_id.strip()
            or not self.dispatch_job_uuid
            or self.queue_job_uuid != self.dispatch_job_uuid
            or not self.dispatch_started_at
            or self.attempts <= 0
            or not isinstance(self.provider_request_json, dict)
            or not self.provider_request_json
        ):
            return False
        try:
            # Reuse the adapter result boundary for length and control-character
            # validation without asking the provider or provider addon anything.
            AdapterResult.success(external_message_id=external_message_id)
        except DTOValidationError:
            return False
        binding = self.message_binding_id.sudo().exists()
        if (
            not binding
            or len(binding) != 1
            or self.target_message_binding_id
            or self.mutation_id
            or binding.account_id != self.account_id
            or binding.channel_binding_id != self.channel_binding_id
            or binding.provider_connection_id != self.provider_connection_id
            or binding.direction != "outbound"
            or binding.origin != "agent"
        ):
            return False
        try:
            command_dto = CommandDTO.from_dict(self.command_json or {})
        except DTOValidationError:
            return False
        message_dto = command_dto.message
        expected_scope = {
            "command_id": self.outbox_idempotency_key,
            "command_type": self.command_type,
            "account_ref": self.account_id.external_ref,
            "connection_ref": self.provider_connection_id.external_ref,
            "conversation_ref": self.channel_binding_id.conversation_ref,
        }
        if (
            any(
                getattr(command_dto, field_name) != expected_value
                for field_name, expected_value in expected_scope.items()
            )
            or command_dto.conversation.conversation_type
            != self.channel_binding_id.conversation_type
            or not command_dto.target_address
            or not message_dto
            or command_dto.client_message_id != (binding.client_message_id or "")
            or message_dto.client_message_id != (binding.client_message_id or "")
            or message_dto.content_type != binding.content_type
        ):
            return False
        return external_message_id, command_dto, evidence["local_error_class"]

    def _contact_center_reconcile_provider_success(self):
        """Project strict persisted success evidence without provider I/O.

        The caller owns the transaction boundary.  This method is idempotent and
        returns whether it changed an uncertain command to ``done``.
        """

        self.ensure_one()
        self._lock_for_uncertain_resolution(connection_write=True)
        proven_success = self._provider_success_reconciliation_evidence()
        if not proven_success:
            return False
        external_message_id, command_dto, local_error_class = proven_success
        binding = self.message_binding_id.sudo()
        binding_model = self.env["contact.center.message.binding"].sudo()
        binding_model.flush_model(["channel_binding_id", "external_message_id"])
        conflict = binding_model.search(
            [
                ("id", "!=", binding.id),
                ("channel_binding_id", "=", binding.channel_binding_id.id),
                ("external_message_id", "=", external_message_id),
            ],
            limit=1,
        )
        if conflict:
            return False
        binding.invalidate_recordset(["delivery_state", "external_message_id"])
        if (
            binding.external_message_id
            and binding.external_message_id != external_message_id
        ):
            return False

        reconciled_at = fields.Datetime.now()
        provider_succeeded_at = self.processed_at or reconciled_at
        group_participant = self._group_finalization_participant(
            self.provider_connection_id, command_dto
        )
        evidence = dict(self.provider_response_json)
        evidence.update(
            {
                "reconciled_by": "positive_provider_response",
                "reconciled_at": fields.Datetime.to_string(reconciled_at),
                "reconciled_previous_error_class": self.last_error_class,
            }
        )
        binding._contact_center_apply_delivery(
            "sent",
            occurred_at=provider_succeeded_at,
            external_event_id="provider-success-outbox:%s" % self.id,
            external_message_id=external_message_id,
            details={
                "source": "positive_provider_response",
                "outbox_command_id": self.id,
                "local_error_class": local_error_class,
            },
        )
        participant_changed = False
        if group_participant:
            participant_changed = binding._contact_center_set_protocol_participant(
                group_participant
            )
        connection = self.provider_connection_id.sudo()
        connection.invalidate_recordset(["last_success_at"])
        if (
            not connection.last_success_at
            or provider_succeeded_at > connection.last_success_at
        ):
            connection.last_success_at = provider_succeeded_at
        self.write(
            {
                "state": "done",
                "processed_at": reconciled_at,
                "provider_response_json": evidence,
                "last_error_class": False,
                "last_error_message": False,
            }
        )
        self.env.flush_all()
        self._notify_delivery_ui(
            refresh_message=bool(
                participant_changed
                or command_dto.conversation.conversation_type == "group"
            )
        )
        return True

    def _restart_provider_success_reconciliation_transaction(self):
        """Discard a failed snapshot before a local-only retry."""

        self.ensure_one()
        self.env.cr.rollback()  # pylint: disable=invalid-commit
        self.env.invalidate_all(flush=False)
        return True

    def _attempt_provider_success_reconciliation_after_commit(self):
        """Retry only the local projection in genuinely fresh transactions."""

        self.ensure_one()
        for attempt in range(1, _PROVIDER_SUCCESS_RECONCILIATION_MAX_ATTEMPTS + 1):
            try:
                with self.env.cr.savepoint():
                    reconciled = bool(
                        self.sudo()._contact_center_reconcile_provider_success()
                    )
                # Serialization can be reported only when PostgreSQL commits.
                self._commit_job_transaction()
                return reconciled
            except (SerializationFailure, DeadlockDetected) as error:
                # End the obsolete/aborted REPEATABLE READ transaction. The next
                # loop iteration starts a new snapshot and still performs no
                # provider or adapter I/O.
                self._restart_provider_success_reconciliation_transaction()
                if attempt < _PROVIDER_SUCCESS_RECONCILIATION_MAX_ATTEMPTS:
                    _logger.warning(
                        "Concurrent provider-success reconciliation failed for "
                        "outbox command %s on attempt %s/%s (%s); retrying local "
                        "projection only",
                        self.id,
                        attempt,
                        _PROVIDER_SUCCESS_RECONCILIATION_MAX_ATTEMPTS,
                        error.__class__.__name__,
                    )
                    continue
                _logger.exception(
                    "Provider-success reconciliation exhausted %s concurrency "
                    "attempts for outbox command %s; keeping it uncertain",
                    _PROVIDER_SUCCESS_RECONCILIATION_MAX_ATTEMPTS,
                    self.id,
                )
                return False
            except Exception:  # keep durable uncertainty on invariant failures
                self._restart_provider_success_reconciliation_transaction()
                _logger.exception(
                    "Could not reconcile proven Contact Center provider success "
                    "without redispatch (command=%s)",
                    self.id,
                )
                return False
        return False

    def action_reconcile_provider_success(self):
        """Let an administrator reconcile old strictly eligible commands safely."""

        self.ensure_one()
        self._check_uncertain_resolution_access()
        with self.env.cr.savepoint():
            return bool(self.sudo()._contact_center_reconcile_provider_success())

    def _resolve_uncertain(self, resolution, reason):
        """Resolve provider ambiguity once, without ever scheduling dispatch."""

        self.ensure_one()
        self._check_uncertain_resolution_access()
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError(_("Resolution evidence or a reason is required."))
        if len(reason) > 4000:
            raise ValidationError(
                _("The resolution reason cannot exceed 4,000 characters.")
            )
        self._lock_for_uncertain_resolution()
        target_state = self._validate_uncertain_resolution(resolution)

        if self.resolution:
            if self.resolution == resolution and self.state == target_state:
                return True
            raise ValidationError(_("This uncertain command was already resolved."))
        if self.state != "uncertain":
            raise ValidationError(
                _("Only an uncertain outbox command can be resolved manually.")
            )

        # The command access and company boundary have already been checked above.
        # Bindings and mutations are technical projections whose operational team
        # rules may legitimately exclude the administrator resolving the command.
        technical_command = self.sudo()
        message_binding = technical_command.message_binding_id
        mutation = technical_command.mutation_id
        resolved_by_id = self.env.user.id
        resolved_at = fields.Datetime.now()
        audit_details = {
            "source": "administrative_uncertain_resolution",
            "outbox_command_id": self.id,
            "resolution": resolution,
            "resolved_by_id": resolved_by_id,
            "reason": reason,
        }
        if resolution == "external_delivery_confirmed":
            message_binding._contact_center_apply_delivery(
                "delivered",
                occurred_at=resolved_at,
                external_event_id="admin-outbox-resolution:%s" % self.id,
                details=audit_details,
            )
        elif resolution == "external_application_confirmed":
            mutation._apply_projection()
            mutation_details = dict(mutation.details_json or {})
            mutation_details["dispatch"] = {
                "state": "done",
                "resolution": resolution,
                "resolved_at": fields.Datetime.to_string(resolved_at),
                "resolved_by_id": resolved_by_id,
                "reason": reason,
            }
            mutation.write({"details_json": mutation_details})
        elif mutation:
            mutation.invalidate_recordset(["state", "details_json"])
            if mutation.state == "applied":
                raise ValidationError(
                    _(
                        "This mutation is already applied and cannot be closed as "
                        "not sent."
                    )
                )
            mutation_details = dict(mutation.details_json or {})
            mutation_details["dispatch"] = {
                "state": "cancelled",
                "resolution": resolution,
                "resolved_at": fields.Datetime.to_string(resolved_at),
                "resolved_by_id": resolved_by_id,
                "reason": reason,
            }
            mutation.write({"state": "failed", "details_json": mutation_details})
        elif message_binding:
            message_binding.invalidate_recordset(["delivery_state"])
            if message_binding.delivery_state in (
                "sent",
                "delivered",
                "read",
            ):
                raise ValidationError(
                    _(
                        "This message already has positive delivery evidence and "
                        "cannot be closed as not sent."
                    )
                )

        technical_command.with_context(contact_center_uncertain_resolution=True).write(
            {
                "state": target_state,
                "resolution": resolution,
                "resolution_reason": reason,
                "resolved_at": resolved_at,
                "resolved_by_id": resolved_by_id,
            }
        )
        technical_command._notify_delivery_ui()
        return True

    def action_requeue(self):
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_admin"
        ):
            raise AccessError(_("Only Contact Center administrators can requeue."))
        to_enqueue = self.browse()
        for command in self.filtered(lambda item: item.state in ("pending", "retry")):
            self.env.cr.execute(
                "SELECT id FROM contact_center_outbox_command "
                "WHERE id = %s FOR UPDATE",
                [command.id],
            )
            command.invalidate_recordset(
                [
                    "state",
                    "queue_job_uuid",
                    "dispatch_job_uuid",
                    "dispatch_started_at",
                ]
            )
            if command.state not in ("pending", "retry"):
                continue
            if command.dispatch_job_uuid or command.dispatch_started_at:
                raise ValidationError(
                    _("A command at the provider dispatch boundary cannot be requeued.")
                )
            existing_job, _recovered = _reuse_queue_job(command, "outbox")
            if existing_job:
                continue
            command.sudo().write({"queue_job_uuid": False})
            to_enqueue |= command
        return to_enqueue._enqueue()

    def _job_process(self):
        """Dispatch once, with a durable boundary before provider I/O."""

        self.ensure_one()
        job_uuid = self.env.context.get("job_uuid")
        if not queue_job_owns_record(self):
            return False

        # The dispatch boundary locks account -> connection -> retry source -> outbox.
        # The exclusive connection lock serializes the short pre-I/O pacing
        # reservation across workers.  It is released by the durable boundary
        # before provider I/O begins.
        expected_account_id = self.account_id.id
        expected_connection_id = self.provider_connection_id.id
        expected_retry_source_id = self.retry_of_id.id
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id = %s FOR SHARE",
            [expected_account_id],
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection "
            "WHERE id = %s FOR UPDATE",
            [expected_connection_id],
        )
        if expected_retry_source_id:
            self.env.cr.execute(
                "SELECT id FROM contact_center_outbox_command "
                "WHERE id = %s FOR UPDATE",
                [expected_retry_source_id],
            )
        self.env.cr.execute(
            "SELECT id FROM contact_center_outbox_command WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()
        if (
            self.account_id.id != expected_account_id
            or self.provider_connection_id.id != expected_connection_id
            or self.retry_of_id.id != expected_retry_source_id
        ):
            raise ValidationError(
                _("The scope and retry source of a command cannot change.")
            )
        if not queue_job_owns_record(self):
            return False
        if self.state in ("done", "uncertain", "dead", "cancelled"):
            return False
        if self.state == "processing":
            self._finish_failure(
                "uncertain",
                RuntimeError(
                    "queue job observed a command after the durable "
                    "provider-dispatch boundary"
                ),
            )
            self._commit_job_transaction()
            return False
        if self.state not in ("pending", "retry"):
            return False

        prepared_dispatch = self._prepare_dispatch_for_job()
        if not prepared_dispatch:
            return False
        connection, command_dto, adapter, request_snapshot = prepared_dispatch
        self._start_dispatch(job_uuid, request_snapshot)
        return self._dispatch_after_boundary(connection, command_dto, adapter)

    def _prepare_dispatch_for_job(self):
        self.ensure_one()
        try:
            self._lock_and_validate_retry_source_for_dispatch()
            self._raise_if_outbound_dispatch_delayed()
            prepared_dispatch = self._prepare_dispatch()
            if prepared_dispatch:
                connection, _command_dto, adapter, _snapshot = prepared_dispatch
                self._reserve_outbound_dispatch(connection, adapter)
            return prepared_dispatch
        except RetrySourceInvalidatedError as error:
            # This new attempt has never crossed the provider boundary. Cancel it
            # instead of turning it into another resendable failure: stronger,
            # late evidence on the original attempt makes any send unsafe.
            self._finish_failure("cancelled", error)
            self._commit_job_transaction()
            return False
        except OutboundThrottleError as error:
            self._persist_safe_retry(error, state="pending")
            self._commit_job_transaction()
            raise RetryableJobError(
                str(error),
                seconds=error.retry_after_seconds,
                ignore_retry=True,
            ) from error
        except ProviderPausedError as error:
            self._persist_safe_retry(error, state="pending")
            self._commit_job_transaction()
            raise RetryableJobError(
                str(error),
                seconds=provider_paused_retry_seconds(error, ("outbox", self.id)),
                ignore_retry=True,
            ) from error
        except (AdapterError, DTOValidationError, ValidationError) as error:
            self._finish_failure("dead", error)
            self._commit_job_transaction()
            return False
        except Exception as error:
            _logger.exception(
                "Unexpected pre-dispatch error for Contact Center command %s", self.id
            )
            attempts = self.attempts + 1
            if attempts >= QUEUE_ATTEMPT_CEILING:
                self._finish_failure("dead", error, attempts=attempts)
                self._commit_job_transaction()
                return False
            self._persist_safe_retry(error, state="retry", attempts=attempts)
            self._commit_job_transaction()
            raise RetryableJobError(str(error), seconds=_retry_delay(error)) from error

    def _database_clock(self):
        """Return one exact UTC-naive timestamp shared by all workers."""

        self.env.cr.execute("SELECT clock_timestamp() AT TIME ZONE 'UTC'")
        return self.env.cr.fetchone()[0]

    def _raise_if_outbound_dispatch_delayed(self):
        """Stop before snapshot/boundary when another dispatch owns the interval."""

        self.ensure_one()
        connection = self.provider_connection_id
        connection.invalidate_recordset(["outbound_dispatch_not_before"])
        deadline = connection.outbound_dispatch_not_before
        now = self._database_clock()
        if deadline and deadline > now:
            error = OutboundThrottleError(
                "provider connection outbound dispatch is throttled"
            )
            error.retry_after_seconds = _outbound_throttle_retry_seconds(
                deadline,
                now,
            )
            raise error
        return True

    def _reserve_outbound_dispatch(self, connection, adapter):
        """Reserve one immediate dispatch in the same transaction as the boundary."""

        self.ensure_one()
        interval = adapter.outbound_min_interval_seconds(connection)
        if (
            not isinstance(interval, int)
            or isinstance(interval, bool)
            or not 0 <= interval <= _OUTBOUND_MIN_INTERVAL_MAX_SECONDS
        ):
            raise AdapterError(
                "adapter outbound_min_interval_seconds must be an integer from 0 to 300"
            )
        now = self._database_clock()
        connection.invalidate_recordset(["outbound_dispatch_not_before"])
        deadline = connection.outbound_dispatch_not_before
        if deadline and deadline > now:
            error = OutboundThrottleError(
                "provider connection outbound dispatch is throttled"
            )
            error.retry_after_seconds = _outbound_throttle_retry_seconds(
                deadline,
                now,
            )
            raise error
        if interval:
            connection._contact_center_set_outbound_dispatch_deadline(
                _ceil_datetime_to_second(now + datetime.timedelta(seconds=interval)),
                "pacing",
            )
        elif deadline:
            connection._contact_center_set_outbound_dispatch_deadline(False)
        return True

    def _start_dispatch(self, job_uuid, request_snapshot):
        self.ensure_one()
        self.write(
            {
                "state": "processing",
                "attempts": self.attempts + 1,
                "dispatch_job_uuid": job_uuid,
                "dispatch_started_at": fields.Datetime.now(),
                "provider_request_json": request_snapshot,
                "last_error_class": False,
                "last_error_message": False,
            }
        )
        self._commit_job_transaction()
        return True

    def _dispatch_after_boundary(self, connection, command_dto, adapter):
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                result = self._execute_adapter(adapter, connection, command_dto)
                self._raise_for_adapter_result(result)
                self._finalize_dispatch_success(connection, result, command_dto)
        except ProviderPausedError as error:
            self._extend_outbound_dispatch_deadline(error)
            self._persist_safe_retry(
                error,
                state="pending",
                attempts=max(0, self.attempts - 1),
            )
            self._commit_job_transaction()
            raise RetryableJobError(
                str(error),
                seconds=provider_paused_retry_seconds(error, ("outbox", self.id)),
                ignore_retry=True,
            ) from error
        except TransientAdapterError as error:
            self._extend_outbound_dispatch_deadline(error)
            if self.attempts >= QUEUE_ATTEMPT_CEILING:
                self._finish_failure("dead", error)
                self._commit_job_transaction()
                return False
            self._persist_safe_retry(error, state="retry")
            self._commit_job_transaction()
            raise RetryableJobError(str(error), seconds=_retry_delay(error)) from error
        except PostDispatchPersistenceError as error:
            self._finish_failure(
                "uncertain",
                error,
                provider_evidence=error.provider_evidence(),
            )
            # Persist the ambiguity before attempting any repair.  The provider
            # boundary has already been crossed, so every recovery step must run
            # in a fresh transaction and must never invoke the adapter again.
            self._commit_job_transaction()
            if (
                error.original_error.__class__.__name__
                in _PROVIDER_SUCCESS_RECONCILIATION_ERROR_CLASSES
            ):
                self._attempt_provider_success_reconciliation_after_commit()
            return True
        except AmbiguousTimeoutError as error:
            evidence = getattr(error, "provider_evidence", None)
            self._finish_failure(
                "uncertain",
                error,
                provider_evidence=evidence() if callable(evidence) else None,
            )
        except AdapterError as error:
            self._finish_failure("dead", error)
        except (DTOValidationError, ValidationError) as error:
            self._finish_failure("dead", error)
        except Exception:  # anything after the boundary is ambiguous
            _logger.exception(
                "Unexpected contact center outbox error for command %s", self.id
            )
            self._finish_failure(
                "uncertain",
                AmbiguousTimeoutError(
                    "unexpected failure after provider dispatch began"
                ),
            )
        self._commit_job_transaction()
        return True

    def _lock_outbound_retry_after_scope(self):
        """Reacquire account -> connection -> outbox after provider I/O."""

        self.ensure_one()
        expected_account_id = self.account_id.id
        expected_connection_id = self.provider_connection_id.id
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id = %s FOR SHARE",
            [expected_account_id],
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection "
            "WHERE id = %s FOR UPDATE",
            [expected_connection_id],
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_outbox_command WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset(
            [
                "account_id",
                "provider_connection_id",
                "state",
                "attempts",
            ]
        )
        if (
            self.account_id.id != expected_account_id
            or self.provider_connection_id.id != expected_connection_id
        ):
            raise ValidationError(
                _("The scope of an outbound command changed after provider dispatch.")
            )
        return self.provider_connection_id

    def _extend_outbound_dispatch_deadline(self, error):
        """Share a bounded positive Retry-After without shortening a newer pause."""

        self.ensure_one()
        seconds = _retry_delay(error)
        if not seconds:
            return False
        connection = self._lock_outbound_retry_after_scope()
        connection.invalidate_recordset(["outbound_dispatch_not_before"])
        now = self._database_clock()
        candidate = _ceil_datetime_to_second(now + datetime.timedelta(seconds=seconds))
        current = connection.outbound_dispatch_not_before
        if not current or candidate > current:
            connection._contact_center_set_outbound_dispatch_deadline(
                candidate,
                "provider_retry_after",
            )
            return True
        return False

    def _prepare_dispatch(self):
        self.ensure_one()
        self._lock_and_validate_retry_source_for_dispatch()
        connection = self.provider_connection_id
        # Serialize account archival and connection health/event transitions
        # with the final state check. Shared locks are released by the durable
        # boundary before provider I/O begins.
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id = %s FOR SHARE",
            [connection.account_id.id],
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection "
            "WHERE id = %s FOR SHARE",
            [connection.id],
        )
        connection.invalidate_recordset(
            [
                "active",
                "role",
                "outbound_active",
                "state",
                "account_id",
                "identity_mismatch_latched",
                "last_state_observed_at",
                "last_health_at",
                "capabilities_json",
            ]
        )
        connection.account_id.invalidate_recordset(["active", "group_outbound_enabled"])
        if not connection._contact_center_outbound_is_available():
            raise ProviderPausedError(
                "provider connection is not active for outbound dispatch"
            )
        command_dto = CommandDTO.from_dict(self.command_json)
        self._validate_command_scope(command_dto)
        adapter = connection.get_adapter()
        request_snapshot = validate_provider_request_snapshot(
            adapter.prepare_request_snapshot(connection, command_dto)
        )
        return connection, command_dto, adapter, request_snapshot

    def _lock_and_validate_retry_source_for_dispatch(self):
        """Fence a manual retry against stronger evidence on its source send."""

        self.ensure_one()
        source = self.retry_of_id.sudo()
        if not source:
            return True
        source_binding = source.message_binding_id.sudo()
        if not source_binding:
            raise RetrySourceInvalidatedError(
                _("The original failed send is no longer safe to retry.")
            )
        expected_reply_id = source_binding.reply_to_binding_id.id
        binding_ids = sorted({source_binding.id, expected_reply_id} - {False})
        self.env.cr.execute(
            "SELECT id FROM contact_center_outbox_command WHERE id = %s FOR UPDATE",
            [source.id],
        )
        source_exists = bool(self.env.cr.fetchone())
        self.env.cr.execute(
            "SELECT id FROM contact_center_message_binding "
            "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [binding_ids],
        )
        locked_binding_ids = {row[0] for row in self.env.cr.fetchall()}
        source.invalidate_recordset(
            ["state", "command_type", "resolution", "message_binding_id"]
        )
        source_binding.invalidate_recordset(
            [
                "delivery_state",
                "external_message_id",
                "message_state",
                "reply_to_binding_id",
            ]
        )
        reply_binding = source_binding.reply_to_binding_id.sudo()
        if reply_binding:
            reply_binding.invalidate_recordset(["message_state"])
        if (
            not source_exists
            or locked_binding_ids != set(binding_ids)
            or self.command_type != "send_message"
            or source == self
            or self.retry_of_id != source
            or source.command_type != "send_message"
            or source.state != "dead"
            or source.resolution
            or source.message_binding_id != source_binding
            or source_binding.delivery_state != "failed"
            or source_binding.external_message_id
            or source_binding.message_state == "deleted"
            or source_binding.reply_to_binding_id.id != expected_reply_id
            or (reply_binding and reply_binding.message_state == "deleted")
        ):
            raise RetrySourceInvalidatedError(
                _("The original failed send is no longer safe to retry.")
            )
        return True

    def _persist_safe_retry(self, error, state="retry", attempts=None):
        self.ensure_one()
        self.write(
            {
                "state": state,
                "attempts": self.attempts if attempts is None else attempts,
                "dispatch_job_uuid": False,
                "dispatch_started_at": False,
                "last_error_class": error.__class__.__name__,
                "last_error_message": str(error)[:4000],
            }
        )
        if self.command_type == "send_message" and self.message_binding_id:
            self._notify_delivery_ui(refresh_message=True)
        return True

    def _commit_job_transaction(self):
        self.ensure_one()
        if not self.env.context.get("job_uuid"):
            raise ValidationError(_("A queue job UUID is required at commit boundary."))
        self.env.cr.commit()  # pylint: disable=invalid-commit
        return True

    def _process_one(self):
        """Synchronous provider operation retained for focused unit tests."""

        self.ensure_one()
        connection, command_dto, adapter, request_snapshot = self._prepare_dispatch()
        self.provider_request_json = request_snapshot
        result = self._execute_adapter(adapter, connection, command_dto)
        self._raise_for_adapter_result(result)
        self._finalize_dispatch_success(connection, result, command_dto)

    def _send_command_scope_mismatches(self, command_dto):
        if command_dto.command_type != "send_message":
            return []
        mismatches = []
        message = command_dto.message
        ledger_client_message_id = self.message_binding_id.client_message_id or ""
        if not command_dto.target_address:
            mismatches.append("target_address")
        if (
            not message
            or command_dto.client_message_id != ledger_client_message_id
            or message.client_message_id != ledger_client_message_id
        ):
            mismatches.append("client_message_id")
        if (
            not self.message_binding_id
            or self.message_binding_id.provider_connection_id
            != self.provider_connection_id
        ):
            mismatches.append("message_binding.provider_connection_id")
        reply_target = (
            self.message_binding_id.reply_to_binding_id
            if self.message_binding_id
            else self.env["contact.center.message.binding"]
        )
        if reply_target and (
            reply_target.channel_binding_id != self.channel_binding_id
            or reply_target.provider_connection_id != self.provider_connection_id
        ):
            mismatches.append("message_binding.reply_to_provider_connection_id")
        if self.channel_binding_id.conversation_type == "direct" and message:
            mismatches.extend(
                self._direct_reply_scope_mismatches(
                    self.message_binding_id,
                    message,
                    command_dto,
                )
            )
        return mismatches

    def _direct_reply_scope_mismatches(
        self,
        message_binding,
        message,
        command_dto,
    ):
        """Anchor a direct reply DTO to its immutable local ledger relation."""

        target = message_binding.reply_to_binding_id
        if not target:
            mismatches = []
            if command_dto.reply_to or message.reply_to_external_id:
                mismatches.append("message.reply_to")
            if message.protocol_snapshot:
                mismatches.append("message.protocol_snapshot")
            return mismatches

        expected = {
            "external_message_id": target.external_message_id or "",
            "protocol_snapshot": target.protocol_snapshot_json or {},
        }
        mismatches = []
        if target.message_state == "deleted":
            mismatches.append("reply_to.deleted")
        if command_dto.reply_to != expected:
            mismatches.append("reply_to")
        if message.reply_to_external_id != expected["external_message_id"]:
            mismatches.append("message.reply_to_external_id")
        if message.protocol_snapshot != expected["protocol_snapshot"]:
            mismatches.append("message.protocol_snapshot")
        return mismatches

    def _group_command_scope_mismatches(self, binding, command_dto):
        if binding.conversation_type != "group":
            return []
        (
            mismatches,
            profile,
            group_capabilities,
            current_own_participant,
        ) = self._group_common_scope_mismatches(binding, command_dto)
        if command_dto.command_type == "send_message":
            mismatches.extend(
                self._group_send_scope_mismatches(
                    binding, command_dto, group_capabilities
                )
            )
        elif command_dto.command_type in (
            "react",
            "edit_message",
            "delete_message",
        ):
            mismatches.extend(
                self._group_mutation_scope_mismatches(
                    binding,
                    command_dto,
                    profile,
                    group_capabilities,
                    current_own_participant,
                )
            )
        else:
            mismatches.append("command_type")
        return mismatches

    def _group_common_scope_mismatches(self, binding, command_dto):
        mismatches = []
        profile = binding.group_profile_ids[:1]
        group_capabilities = conversation_capabilities(
            self.provider_connection_id.capabilities_json or {}, "group"
        )
        if not self.account_id.group_outbound_enabled:
            mismatches.append("account.group_outbound_enabled")
        if not profile or profile.provider_connection_id != self.provider_connection_id:
            mismatches.append("provider_connection_id")
        if not command_dto.conversation.addresses or any(
            address.role != "group" for address in command_dto.conversation.addresses
        ):
            mismatches.append("conversation.addresses.group")
        if not command_dto.target_address or command_dto.target_address.role != "group":
            mismatches.append("target_address.group")
        if command_dto.extensions:
            mismatches.append("command.extensions")
        is_mutation = command_dto.command_type in (
            "react",
            "edit_message",
            "delete_message",
        )
        own_required = bool(
            is_mutation or group_capabilities.get("reply_requires_participant") is True
        )
        try:
            current_own_participant = self.env[
                "contact.center.application"
            ]._group_own_protocol_participant(
                binding,
                self.provider_connection_id,
                required=own_required,
            )
        except (UserError, ValidationError):
            current_own_participant = None
            mismatches.append("group_profile.own_protocol_participant")
        command_own_participant = command_dto.own_protocol_participant
        if bool(current_own_participant) != bool(command_own_participant):
            mismatches.append("own_protocol_participant")
        elif current_own_participant and command_own_participant:
            current_key = (
                current_own_participant.namespace,
                current_own_participant.value_normalized,
                current_own_participant.role,
                current_own_participant.confidence,
            )
            command_key = (
                command_own_participant.namespace,
                command_own_participant.value_normalized,
                command_own_participant.role,
                command_own_participant.confidence,
            )
            if current_key != command_key:
                mismatches.append("own_protocol_participant")
        return mismatches, profile, group_capabilities, current_own_participant

    def _group_send_scope_mismatches(self, binding, command_dto, group_capabilities):
        mismatches = []
        message = command_dto.message
        message_binding = self.message_binding_id
        if not group_capabilities.get("send_message"):
            mismatches.append("capabilities.conversation_types.group.send_message")
        if not message or not message_binding.client_message_id:
            mismatches.append("message")
        else:
            mismatches.extend(
                self._group_media_scope_mismatches(
                    message_binding, message, group_capabilities
                )
            )
            mismatches.extend(
                self._group_reply_scope_mismatches(
                    binding,
                    message_binding,
                    message,
                    command_dto,
                    group_capabilities,
                )
            )
        mismatches.extend(
            self._group_sender_signature_scope_mismatches(
                command_dto, group_capabilities
            )
        )
        return mismatches

    def _group_sender_signature_scope_mismatches(self, command_dto, group_capabilities):
        """Allow only the bounded, semantic signature option on group sends."""

        options = command_dto.options or {}
        if not options:
            return []
        if set(options) != {"sender_signature"}:
            return ["command.options"]
        signature = options.get("sender_signature")
        if not isinstance(signature, dict) or set(signature) != {"display_name"}:
            return ["options.sender_signature"]
        display_name = signature.get("display_name")
        if (
            not isinstance(display_name, str)
            or not display_name
            or len(display_name) > 120
            or display_name != " ".join(display_name.split())
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in display_name
            )
        ):
            return ["options.sender_signature.display_name"]
        mismatches = []
        if group_capabilities.get("sender_signature") is not True:
            mismatches.append("capabilities.conversation_types.group.sender_signature")
        return mismatches

    def _group_mutation_scope_mismatches(
        self,
        binding,
        command_dto,
        profile,
        group_capabilities,
        current_own_participant,
    ):
        mismatches = []
        capability = {
            "react": "react",
            "edit_message": "edit_message",
            "delete_message": "delete_message",
        }[command_dto.command_type]
        if group_capabilities.get(capability) is not True:
            mismatches.append("capabilities.conversation_types.group.%s" % capability)
        target = self.target_message_binding_id
        if (
            not target
            or target.channel_binding_id != binding
            or target.provider_connection_id != self.provider_connection_id
            or not target.external_message_id
            or not target.protocol_participant_json
        ):
            mismatches.append("target_message_binding")
            return mismatches
        try:
            persisted_target = AddressDTO.from_dict(
                target.protocol_participant_json or {}
            )
        except DTOValidationError:
            persisted_target = None
            mismatches.append("target_message_binding.protocol_participant")
        command_target = command_dto.target_protocol_participant
        if bool(persisted_target) != bool(command_target):
            mismatches.append("target_protocol_participant")
        elif persisted_target and command_target:
            persisted_key = (
                persisted_target.namespace,
                persisted_target.value_normalized,
                persisted_target.role,
                persisted_target.confidence,
            )
            command_key = (
                command_target.namespace,
                command_target.value_normalized,
                command_target.role,
                command_target.confidence,
            )
            if persisted_key != command_key:
                mismatches.append("target_protocol_participant")
        target_from_me = (command_dto.options or {}).get("target_from_me")
        if not isinstance(target_from_me, bool) or target_from_me != (
            target.direction == "outbound"
        ):
            mismatches.append("options.target_from_me")
        if command_dto.command_type in ("edit_message", "delete_message") and (
            target.direction != "outbound" or target.origin != "agent"
        ):
            mismatches.append("target_message_binding.ownership")
        if not self.mutation_id or self.mutation_id.details_json != command_dto.options:
            mismatches.append("mutation.options")
        if command_dto.command_type == "edit_message":
            if (
                not command_dto.message
                or command_dto.message.content_type != "text"
                or command_dto.message.text
                != (command_dto.options or {}).get("new_text")
            ):
                mismatches.append("message.edit")
        elif command_dto.message:
            mismatches.append("message")
        mismatches.extend(
            self._group_mutation_roster_mismatches(
                profile, current_own_participant, persisted_target, target
            )
        )
        return mismatches

    def _group_mutation_roster_mismatches(
        self, profile, current_own_participant, persisted_target, target
    ):
        if not profile or not current_own_participant or not persisted_target:
            return []
        try:
            own_roster = profile._resolve_protocol_participant(
                (current_own_participant,), active_only=True
            )
            target_roster = profile._resolve_protocol_participant(
                (persisted_target,), active_only=True
            )
        except ValidationError:
            own_roster = self.env["contact.center.group.participant"]
            target_roster = self.env["contact.center.group.participant"]
        mismatches = []
        if not own_roster:
            mismatches.append("group_profile.own_protocol_participant")
        if not target_roster:
            mismatches.append("target_protocol_participant.roster")
        if target.direction == "outbound" and own_roster != target_roster:
            mismatches.append("target_protocol_participant.ownership")
        return mismatches

    def _group_media_scope_mismatches(
        self, message_binding, message, group_capabilities
    ):
        mismatches = []
        ledger_media = message_binding.media_ids.sorted("sequence")
        command_media = message.media
        if len(ledger_media) > 1 or len(command_media) > 1:
            return ["message.media.count"]
        if len(ledger_media) != len(command_media):
            mismatches.append("message.media.ledger")
            return mismatches
        if not command_media:
            if (
                message.content_type != "text"
                or message_binding.content_type != "text"
                or not message.text
            ):
                mismatches.append("message.content")
            return mismatches

        media_binding = ledger_media[0]
        media = command_media[0]
        if (
            message.content_type != media.kind
            or message_binding.content_type != media.kind
        ):
            mismatches.append("message.content_type")
        if media_binding._as_dto().to_dict() != media.to_dict():
            mismatches.append("message.media.dto")
        attachment = media_binding.attachment_id.exists()
        if (
            media_binding.state != "ready"
            or not attachment
            or attachment.type != "binary"
            or attachment.res_model != "mail.message"
            or attachment.res_id != message_binding.message_id.id
        ):
            mismatches.append("message.media.attachment")
        try:
            validate_provider_media_capability(
                group_capabilities,
                media.kind,
                media.mime_type,
                media.size_bytes,
            )
            validate_provider_media_caption_capability(
                group_capabilities,
                media.kind,
                bool(message.text),
            )
        except (UserError, ValidationError):
            mismatches.append("capabilities.conversation_types.group.media")
        return mismatches

    def _group_reply_scope_mismatches(
        self,
        binding,
        message_binding,
        message,
        command_dto,
        group_capabilities,
    ):
        mismatches = []
        target = message_binding.reply_to_binding_id
        if not target:
            if message.reply_to_external_id or command_dto.reply_to:
                mismatches.append("message.reply_to")
            if message.protocol_snapshot:
                mismatches.append("message.protocol_snapshot")
            return mismatches

        if group_capabilities.get("reply") is not True:
            mismatches.append("capabilities.conversation_types.group.reply")
        if (
            target.channel_binding_id != binding
            or target.provider_connection_id != self.provider_connection_id
            or not target.external_message_id
            or target.message_state == "deleted"
        ):
            mismatches.append("message.reply_to_binding")
            return mismatches
        if (
            group_capabilities.get("reply_requires_participant") is True
            and not target.protocol_participant_json
        ):
            mismatches.append("message.reply_to_participant")
            return mismatches
        try:
            expected = (
                self.provider_connection_id.get_adapter().prepare_reply_reference(
                    self.provider_connection_id,
                    conversation_type="group",
                    external_message_id=target.external_message_id,
                    protocol_snapshot=target.protocol_snapshot_json or {},
                    protocol_participant=target.protocol_participant_json or {},
                )
            )
        except AdapterError:
            mismatches.append("message.reply_to_reference")
            return mismatches
        if command_dto.reply_to != expected:
            mismatches.append("reply_to")
        if message.reply_to_external_id != expected.get("external_message_id", ""):
            mismatches.append("message.reply_to_external_id")
        if message.protocol_snapshot != expected.get("protocol_snapshot", {}):
            mismatches.append("message.protocol_snapshot")
        return mismatches

    def _validate_command_scope(self, command_dto):
        self.ensure_one()
        binding = self.channel_binding_id
        expected = {
            "command_id": self.outbox_idempotency_key,
            "command_type": self.command_type,
            "account_ref": self.account_id.external_ref,
            "connection_ref": self.provider_connection_id.external_ref,
            "conversation_ref": binding.conversation_ref,
        }
        mismatches = [
            field_name
            for field_name, expected_value in expected.items()
            if getattr(command_dto, field_name) != expected_value
        ]
        if command_dto.conversation.conversation_type != binding.conversation_type:
            mismatches.append("conversation.conversation_type")
        allowed_addresses = {
            (alias.namespace, alias.value_raw, alias.value_normalized, alias.role)
            for alias in binding.alias_ids
        }
        supplied_addresses = {
            (
                address.namespace,
                address.value,
                address.value_normalized,
                address.role,
            )
            for address in command_dto.conversation.addresses
        }
        if not supplied_addresses.issubset(allowed_addresses):
            mismatches.append("conversation.addresses")
        if (
            command_dto.target_address
            and (
                command_dto.target_address.namespace,
                command_dto.target_address.value,
                command_dto.target_address.value_normalized,
                command_dto.target_address.role,
            )
            not in allowed_addresses
        ):
            mismatches.append("target_address")
        mismatches.extend(self._send_command_scope_mismatches(command_dto))
        mismatches.extend(self._group_command_scope_mismatches(binding, command_dto))
        if command_dto.command_type in ("react", "edit_message", "delete_message"):
            target_external_id = (command_dto.options or {}).get(
                "target_external_message_id"
            )
            if (
                not self.target_message_binding_id
                or self.target_message_binding_id.provider_connection_id
                != self.provider_connection_id
                or not target_external_id
                or target_external_id
                != self.target_message_binding_id.external_message_id
            ):
                mismatches.append("options.target_external_message_id")
        if mismatches:
            raise ValidationError(
                _(
                    "The persisted provider command does not match its outbox scope: %s",
                    ", ".join(sorted(set(mismatches))),
                )
            )
        return True

    def _execute_adapter(self, adapter, connection, command_dto):
        try:
            result = adapter.execute_command(connection, command_dto)
        except AdapterError:
            raise
        except Exception as error:
            raise AmbiguousTimeoutError(
                "adapter failed after provider dispatch began"
            ) from error
        try:
            if isinstance(result, dict):
                result = AdapterResult(**result)
            if not isinstance(result, AdapterResult):
                raise TypeError("execute_command must return AdapterResult")
        except Exception as error:
            raise PostDispatchPersistenceError(error) from error
        return result

    @api.model
    def _raise_for_adapter_result(self, result):
        if result.status == "transient":
            error_class = (
                ProviderRateLimitError
                if result.error_code in ("http_429", "rate_limited")
                else TransientAdapterError
            )
            error = error_class(result.error_message or result.error_code)
            error.retry_after_seconds = result.retry_after_seconds
            raise error
        if result.status == "uncertain":
            raise AmbiguousAdapterResultError(result)
        if result.status == "paused":
            error = ProviderPausedError(result.error_message or result.error_code)
            error.retry_after_seconds = result.retry_after_seconds
            raise error
        if result.status == "permanent":
            raise AdapterError(result.error_message or result.error_code)
        return True

    def _group_finalization_participant(self, connection, command_dto):
        """Return a still-current sender proof without invalidating send success."""

        candidate = command_dto.own_protocol_participant
        if command_dto.conversation.conversation_type != "group" or not candidate:
            return None
        profile = self.channel_binding_id.group_profile_ids.sudo()[:1]
        if not profile:
            return None
        # Group sync takes connection -> channel -> binding -> profile locks.
        # Start with the connection here as well, then hold the profile stable
        # until the successful local projection is flushed.
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection "
            "WHERE id = %s FOR UPDATE",
            [connection.id],
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_group_profile WHERE id = %s FOR SHARE",
            [profile.id],
        )
        profile.invalidate_recordset(
            [
                "provider_connection_id",
                "own_protocol_participant_json",
                "own_protocol_participant_health_revision",
            ]
        )
        connection.invalidate_recordset(["health_configuration_revision"])
        try:
            current = self.env[
                "contact.center.application"
            ]._group_own_protocol_participant(
                self.channel_binding_id, connection, required=False
            )
        except (UserError, ValidationError):
            return None
        if not current:
            return None
        current_key = (
            current.namespace,
            current.value_normalized,
            current.role,
            current.confidence,
        )
        candidate_key = (
            candidate.namespace,
            candidate.value_normalized,
            candidate.role,
            candidate.confidence,
        )
        return candidate if current_key == candidate_key else None

    def _finalize_dispatch_success(self, connection, result, command_dto):
        participant_changed = False
        refresh_message = False
        try:
            if connection.id != self.provider_connection_id.id:
                raise ValidationError(
                    _("The provider connection changed after dispatch began.")
                )
            # The durable provider boundary released the admission locks. Restore
            # the canonical account -> connection -> outbox order before group or
            # delivery projection can take lower-level locks. This prevents a
            # topology writer (account -> connection) from meeting finalization
            # in the inverse connection -> account-FK order.
            self._lock_for_uncertain_resolution(connection_write=True)
            connection = self.provider_connection_id.sudo()
            group_participant = self._group_finalization_participant(
                connection, command_dto
            )
            values = {
                "state": "done",
                "processed_at": fields.Datetime.now(),
                "provider_response_json": result.provider_response,
                "last_error_class": False,
                "last_error_message": False,
            }
            self.write(values)
            if self.command_type == "send_message" and self.message_binding_id:
                message_binding = self.message_binding_id
                message_binding._contact_center_apply_delivery(
                    "sent",
                    occurred_at=fields.Datetime.now(),
                    external_message_id=result.external_message_id or None,
                    details=result.provider_response,
                )
                if command_dto.conversation.conversation_type == "group":
                    refresh_message = bool(result.external_message_id)
                    if group_participant:
                        participant_changed = (
                            message_binding._contact_center_set_protocol_participant(
                                group_participant
                            )
                        )
            elif self.mutation_id:
                self.mutation_id._apply_projection()
            connection.last_success_at = fields.Datetime.now()
            # Odoo may defer these writes until the surrounding savepoint exits.
            # Flush while still inside this try so a local constraint failure after
            # provider success is classified as uncertain, never redispatched.
            self.env.flush_all()
        except Exception as error:
            raise PostDispatchPersistenceError(error, result=result) from error
        self._notify_delivery_ui(
            refresh_message=bool(refresh_message or participant_changed)
        )
        return True

    def _notify_delivery_ui(self, refresh_message=False):
        self.ensure_one()
        binding = self.message_binding_id or self.target_message_binding_id
        if not binding:
            return False
        if self.command_type != "send_message":
            self.env["contact.center.application"]._notify_ui(
                binding.channel_binding_id.channel_id,
                "message_updated",
                {"message_id": binding.message_id.id, "dispatch_state": self.state},
            )
            return True
        self.env["contact.center.application"]._notify_ui(
            binding.channel_binding_id.channel_id,
            "delivery_updated",
            {
                "message_id": binding.message_id.id,
                "state": binding.delivery_state,
                "dispatch_state": self.state,
            },
        )
        if refresh_message:
            self.env["contact.center.application"]._notify_ui(
                binding.channel_binding_id.channel_id,
                "message_updated",
                {"message_id": binding.message_id.id},
            )
        return True

    def _finish_failure(self, state, error, attempts=None, provider_evidence=None):
        self.ensure_one()
        values = {
            "state": state,
            "attempts": self.attempts if attempts is None else attempts,
            "processed_at": fields.Datetime.now(),
            "last_error_class": error.__class__.__name__,
            "last_error_message": str(error)[:4000],
        }
        if provider_evidence is not None:
            values["provider_response_json"] = provider_evidence
        self.write(values)
        if (
            state == "dead"
            and self.command_type == "send_message"
            and self.message_binding_id
            and self.message_binding_id.delivery_state == "queued"
        ):
            self.message_binding_id._contact_center_apply_delivery(
                "failed",
                occurred_at=fields.Datetime.now(),
                details={
                    "outbox_state": state,
                    "error_class": error.__class__.__name__,
                    "error_message": str(error)[:4000],
                },
            )
        if state in ("dead", "uncertain") and self.mutation_id:
            mutation_details = dict(self.mutation_id.details_json or {})
            dispatch_details = {
                "state": state,
                "error_class": error.__class__.__name__,
                "error_message": str(error)[:4000],
            }
            if provider_evidence is not None:
                dispatch_details["provider_evidence"] = provider_evidence
            mutation_details["dispatch"] = dispatch_details
            mutation_values = {"details_json": mutation_details}
            if state == "dead":
                mutation_values["state"] = "failed"
            self.mutation_id.write(mutation_values)
        if state in ("dead", "uncertain", "cancelled") and (
            self.message_binding_id or self.target_message_binding_id
        ):
            self._notify_delivery_ui(refresh_message=True)


class ContactCenterOutboxResolutionWizard(models.TransientModel):
    _name = "contact.center.outbox.resolution.wizard"
    _description = "Resolve Uncertain Contact Center Outbox Command"

    outbox_command_id = fields.Many2one(
        "contact.center.outbox.command",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        related="outbox_command_id.company_id",
        store=True,
        readonly=True,
    )
    requested_resolution = fields.Selection(
        [
            ("external_delivery_confirmed", "External Delivery Confirmed"),
            ("external_application_confirmed", "External Application Confirmed"),
            ("closed_without_send", "Closed Without Sending"),
        ],
        string="Resolution",
        required=True,
        readonly=True,
    )
    reason = fields.Text(
        required=True,
        help=(
            "Describe the external evidence used to confirm the result, or why "
            "the command is being closed without sending."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        wizards = super().create(vals_list)
        for wizard in wizards:
            wizard.outbox_command_id._check_uncertain_resolution_access()
            wizard.outbox_command_id._validate_uncertain_resolution(
                wizard.requested_resolution
            )
        return wizards

    def action_confirm(self):
        self.ensure_one()
        self.outbox_command_id._resolve_uncertain(
            self.requested_resolution, self.reason
        )
        return {"type": "ir.actions.act_window_close"}
