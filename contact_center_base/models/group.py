import base64
import datetime
import hashlib
import json
import logging

from psycopg2 import IntegrityError, OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.tools.mimetypes import guess_mimetype

from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import (
    AdapterError,
    ProviderPausedError,
    TransientAdapterError,
    UnsupportedEventError,
)
from ..services.dto import (
    AddressDTO,
    AvatarResult,
    DTOValidationError,
    GroupMetadataDTO,
)
from ..services.job import (
    ACTIVE_QUEUE_JOB_STATES,
    QUEUE_ATTEMPT_CEILING,
    canonical_queue_job,
    provider_paused_retry_seconds,
    queue_job_owns_record,
)

_logger = logging.getLogger(__name__)

_ACTIVE_QUEUE_JOB_STATES = ACTIVE_QUEUE_JOB_STATES
_GROUP_METADATA_TTL = datetime.timedelta(hours=6)
_GROUP_PARTIAL_RETRY = datetime.timedelta(minutes=15)
_GROUP_AVATAR_MAX_BYTES = 2 * 1024 * 1024
_GROUP_AVATAR_MIMETYPES = frozenset(("image/jpeg", "image/png", "image/webp"))
_GROUP_METADATA_JOB_PRIORITY = 50
_GROUP_UI_KEYS = frozenset(
    (
        "display_name",
        "avatar_url",
        "participant_count",
        "admin_count",
        "own_role",
        "metadata_state",
        "last_synced_at",
    )
)


def _odoo_datetime(value):
    value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


class ContactCenterChannelBinding(models.Model):
    _inherit = "contact.center.channel.binding"

    group_profile_ids = fields.One2many(
        "contact.center.group.profile",
        "channel_binding_id",
        string="Group Profile",
    )

    def unlink(self):
        channel_ids = sorted(self.sudo().mapped("channel_id").ids)
        if channel_ids:
            self.env.cr.execute(
                "SELECT id FROM mail_channel WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                [channel_ids],
            )
        avatars = self.sudo().mapped("group_profile_ids.avatar_attachment_id")
        result = super().unlink()
        avatars.exists().sudo().unlink()
        return result


class ContactCenterGroupProfile(models.Model):
    _name = "contact.center.group.profile"
    _description = "Contact Center Group Profile"
    _rec_name = "name"
    _order = "channel_id"
    _check_company_auto = True

    channel_binding_id = fields.Many2one(
        "contact.center.channel.binding",
        required=True,
        index=True,
        ondelete="cascade",
    )
    channel_id = fields.Many2one(
        related="channel_binding_id.channel_id",
        store=True,
        readonly=True,
        index=True,
    )
    account_id = fields.Many2one(
        related="channel_binding_id.account_id",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="channel_binding_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    provider_connection_id = fields.Many2one(
        "contact.center.provider.connection",
        required=True,
        index=True,
        ondelete="restrict",
        check_company=True,
    )
    name = fields.Char()
    metadata_state = fields.Selection(
        [
            ("unavailable", "Unavailable"),
            ("pending", "Pending"),
            ("ready", "Ready"),
            ("stale", "Stale"),
            ("failed", "Failed"),
        ],
        required=True,
        default="unavailable",
        index=True,
        copy=False,
    )
    roster_complete = fields.Boolean(default=False, copy=False)
    participant_count = fields.Integer(default=0, required=True, copy=False)
    admin_count = fields.Integer(default=0, required=True, copy=False)
    own_role = fields.Selection(
        [
            ("unknown", "Unknown"),
            ("member", "Member"),
            ("admin", "Administrator"),
            ("superadmin", "Super Administrator"),
        ],
        required=True,
        default="unknown",
        copy=False,
    )
    own_protocol_participant_json = fields.Json(
        default=dict,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Canonical provider-observed sender address used only after a successful "
            "group send to make that own message replyable."
        ),
    )
    own_protocol_participant_health_revision = fields.Integer(
        default=0,
        required=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Provider-configuration revision that observed the own protocol "
            "participant. A mismatch blocks group dispatch until metadata is "
            "synchronized again."
        ),
    )
    participant_ids = fields.One2many(
        "contact.center.group.participant",
        "group_profile_id",
        string="Participants",
        groups="contact_center_base.group_contact_center_admin",
    )
    avatar_attachment_id = fields.Many2one(
        "ir.attachment",
        copy=False,
        ondelete="set null",
        groups="contact_center_base.group_contact_center_admin",
    )
    avatar_sha256 = fields.Char(
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    avatar_provider_revision = fields.Char(
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    provider_revision = fields.Char(
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    snapshot_hash = fields.Char(
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    last_synced_at = fields.Datetime(copy=False, index=True)
    next_sync_at = fields.Datetime(copy=False, index=True)
    sync_requested_at = fields.Datetime(copy=False)
    sync_revision = fields.Integer(default=0, required=True, copy=False)
    applied_revision = fields.Integer(default=0, required=True, copy=False)
    attempts = fields.Integer(default=0, required=True, copy=False)
    queue_job_uuid = fields.Char(
        index=True,
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    last_error_class = fields.Char(
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    last_error_message = fields.Text(
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )

    _sql_constraints = [
        (
            "channel_binding_unique",
            "unique(channel_binding_id)",
            "A group conversation can have only one group profile.",
        ),
        (
            "counts_nonnegative",
            "check(participant_count >= 0 AND admin_count >= 0 "
            "AND admin_count <= participant_count)",
            "Group participant counters are invalid.",
        ),
        (
            "revisions_nonnegative",
            "check(sync_revision >= 0 AND applied_revision >= 0 " "AND attempts >= 0)",
            "Group synchronization counters cannot be negative.",
        ),
        (
            "own_participant_revision_nonnegative",
            "check(own_protocol_participant_health_revision >= 0)",
            "The group sender observation revision cannot be negative.",
        ),
    ]

    def _resolve_protocol_participant(
        self, addresses, *, active_only=False, enrich=False
    ):
        """Resolve provider-observed PN/LID-style aliases inside this roster.

        The core deliberately treats namespaces as provider-neutral.  Two
        addresses are equivalent only when the persisted roster maps both to
        the same participant.  A receipt or mutation may enrich an already
        resolved participant with another protocol alias, but it never creates
        a roster participant by itself.
        """

        self.ensure_one()
        normalized = []
        for address in addresses or ():
            try:
                address = (
                    address
                    if isinstance(address, AddressDTO)
                    else AddressDTO.from_dict(address)
                )
            except DTOValidationError as error:
                raise ValidationError(
                    _("The observed group participant address is invalid.")
                ) from error
            if address.role in ("group", "routing") or address.confidence != "protocol":
                raise ValidationError(
                    _("Group participant aliases must be protocol observations.")
                )
            normalized.append(address)
        if not normalized:
            return self.env["contact.center.group.participant"]

        keys = {(address.namespace, address.value_normalized) for address in normalized}
        aliases = (
            self.env["contact.center.group.participant.alias"]
            .sudo()
            .search(
                [
                    ("group_profile_id", "=", self.id),
                    ("namespace", "in", sorted({key[0] for key in keys})),
                    ("value_normalized", "in", sorted({key[1] for key in keys})),
                ]
            )
            .filtered(lambda alias: (alias.namespace, alias.value_normalized) in keys)
        )
        participants = aliases.with_context(active_test=False).mapped("participant_id")
        if active_only:
            participants = participants.filtered("active")
        if len(participants) > 1:
            raise ValidationError(
                _("Observed group aliases resolve to different participants.")
            )
        participant = participants[:1]
        if not participant:
            return participant

        if enrich:
            known_keys = {
                (alias.namespace, alias.value_normalized) for alias in aliases
            }
            alias_model = self.env["contact.center.group.participant.alias"].sudo()
            for address in normalized:
                key = (address.namespace, address.value_normalized)
                if key in known_keys:
                    continue
                try:
                    with self.env.cr.savepoint():
                        alias_model.create(
                            {
                                "participant_id": participant.id,
                                "namespace": address.namespace,
                                "value_raw": address.value,
                                "value_normalized": address.value_normalized,
                                "role": address.role,
                                "source_field": address.source_field or False,
                                "confidence": address.confidence,
                                "last_seen_at": fields.Datetime.now(),
                            }
                        )
                except IntegrityError as error:
                    existing = alias_model.search(
                        [
                            ("group_profile_id", "=", self.id),
                            ("namespace", "=", address.namespace),
                            ("value_normalized", "=", address.value_normalized),
                        ],
                        limit=1,
                    )
                    if not existing:
                        # Under PostgreSQL REPEATABLE READ, the transaction that
                        # lost a concurrent unique-key race cannot see the winner
                        # in its already-fixed snapshot.  A fresh queue attempt
                        # can resolve it safely; absence here is not evidence of
                        # an ownership conflict.
                        raise TransientAdapterError(
                            "concurrent group alias observation must be retried"
                        ) from error
                    if existing.participant_id != participant:
                        raise ValidationError(
                            _(
                                "The newly observed group alias belongs to another "
                                "participant."
                            )
                        ) from error
        return participant

    @api.model
    def get_group_metadata_runtime_contract(self):
        """Return the bounded, secret-free group runtime contract to admins."""

        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_admin"
        ):
            raise AccessError(
                _(
                    "Only Contact Center administrators can inspect the group "
                    "metadata contract."
                )
            )
        cron = self.env.ref(
            "contact_center_base.ir_cron_contact_center_group_metadata"
        ).sudo()
        job_function = self.env.ref(
            "contact_center_base.queue_job_function_contact_center_group_metadata"
        ).sudo()
        admin_group = self.env.ref(
            "contact_center_base.group_contact_center_admin"
        ).sudo()
        agent_group = self.env.ref(
            "contact_center_base.group_contact_center_agent"
        ).sudo()

        def _is_read_only_access(access, group):
            return (
                access.group_id == group
                and access.perm_read
                and not access.perm_write
                and not access.perm_create
                and not access.perm_unlink
            )

        def _normalized_domain(rule):
            return "".join((rule.domain_force or "").split())

        admin_company_domain = "[('company_id','in',company_ids)]"
        agent_profile_domain = (
            "[('company_id','in',company_ids),"
            "('channel_id.channel_member_ids.partner_id','=',user.partner_id.id)]"
        )

        def _has_rule(rules, group, normalized_domain):
            return any(
                set(rule.groups.ids) == {group.id}
                and _normalized_domain(rule) == normalized_domain
                for rule in rules
            )

        roster_acl = {}
        for model_name in (
            "contact.center.group.participant",
            "contact.center.group.participant.alias",
        ):
            accesses = (
                self.env["ir.model.access"]
                .sudo()
                .search([("model_id.model", "=", model_name)])
            )
            roster_acl[model_name] = {
                "record_count": len(accesses),
                "admin_only": bool(accesses)
                and all(access.group_id == admin_group for access in accesses),
                "read_only": bool(accesses)
                and all(
                    _is_read_only_access(access, admin_group) for access in accesses
                ),
            }
        profile_accesses = (
            self.env["ir.model.access"]
            .sudo()
            .search([("model_id.model", "=", "contact.center.group.profile")])
        )
        profile_rules = (
            self.env["ir.rule"]
            .sudo()
            .search([("model_id.model", "=", "contact.center.group.profile")])
        )
        record_rules = {}
        for model_name in (
            "contact.center.group.participant",
            "contact.center.group.participant.alias",
        ):
            rules = (
                self.env["ir.rule"].sudo().search([("model_id.model", "=", model_name)])
            )
            record_rules[model_name] = {
                "record_count": len(rules),
                "admin_company_scoped": _has_rule(
                    rules, admin_group, admin_company_domain
                ),
            }
        record_rules["contact.center.group.profile"] = {
            "record_count": len(profile_rules),
            "agent_membership_scoped": _has_rule(
                profile_rules, agent_group, agent_profile_domain
            ),
            "admin_company_scoped": _has_rule(
                profile_rules, admin_group, admin_company_domain
            ),
        }
        return {
            "cron": {
                "id": cron.id,
                "model": cron.model_id.model,
                "state": cron.state,
                "code": cron.code,
                "active": cron.active,
                "interval_number": cron.interval_number,
                "interval_type": cron.interval_type,
                "numbercall": cron.numbercall,
                "doall": cron.doall,
            },
            "job_function": {
                "id": job_function.id,
                "model": job_function.model_id.model,
                "method": job_function.method,
                "channel": job_function.channel,
                "retry_pattern": dict(job_function.retry_pattern or {}),
                "allow_commit": job_function.allow_commit,
            },
            "priority": _GROUP_METADATA_JOB_PRIORITY,
            "ttl_seconds": int(_GROUP_METADATA_TTL.total_seconds()),
            "partial_retry_seconds": int(_GROUP_PARTIAL_RETRY.total_seconds()),
            "avatar_max_bytes": _GROUP_AVATAR_MAX_BYTES,
            "ui_group_keys": sorted(_GROUP_UI_KEYS),
            "roster_acl": roster_acl,
            "profile_acl": {
                "record_count": len(profile_accesses),
                "agent_read_only": any(
                    _is_read_only_access(access, agent_group)
                    for access in profile_accesses
                ),
                "admin_read_only": any(
                    _is_read_only_access(access, admin_group)
                    for access in profile_accesses
                ),
            },
            "record_rules": record_rules,
        }

    def action_retry_metadata_sync(self):
        """Retry terminal or stale group metadata pulls as an administrator."""

        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_admin"
        ):
            raise AccessError(
                _(
                    "Only Contact Center administrators can retry group metadata "
                    "synchronization."
                )
            )
        self.check_access_rights("read")
        self.check_access_rule("read")
        return self.with_context(contact_center_skip_enqueue=False)._request_sync(
            force=True,
            eligible_states=("failed", "unavailable", "stale"),
            skip_if_active=True,
        )

    @api.constrains("channel_binding_id", "provider_connection_id")
    def _check_group_scope(self):
        for profile in self:
            binding = profile.channel_binding_id
            if binding.conversation_type != "group" or binding.identity_id:
                raise ValidationError(
                    _("Group profiles require a group conversation binding.")
                )
            if profile.provider_connection_id.account_id != binding.account_id:
                raise ValidationError(
                    _("The group profile connection belongs to another account.")
                )

    @api.constrains("own_protocol_participant_json")
    def _check_own_protocol_participant(self):
        for profile in self:
            values = profile.own_protocol_participant_json or {}
            if not values:
                continue
            try:
                participant = AddressDTO.from_dict(values)
            except DTOValidationError as error:
                raise ValidationError(
                    _("The group own protocol participant is invalid.")
                ) from error
            if participant.role != "sender" or participant.confidence != "protocol":
                raise ValidationError(
                    _("The group own protocol participant must be a protocol sender.")
                )

    @api.model
    def _get_or_create(self, binding, connection):
        binding.ensure_one()
        connection.ensure_one()
        if binding.conversation_type != "group" or binding.identity_id:
            raise ValidationError(_("Only group bindings can have a group profile."))
        if connection.account_id != binding.account_id:
            raise ValidationError(
                _("The observed group connection belongs to another account.")
            )
        # The profile can be created concurrently by the first inbound message,
        # a metadata hint or the upgrade backfill. Acquire the canonical channel
        # before touching the profile so creators follow channel -> profile order.
        # The unique constraint remains the final guard because an older
        # REPEATABLE READ snapshot is not refreshed by taking this lock.
        self.env.cr.execute(
            "SELECT id FROM mail_channel WHERE id = %s FOR UPDATE",
            [binding.channel_id.id],
        )
        profile = self.sudo().search([("channel_binding_id", "=", binding.id)], limit=1)
        if profile:
            return profile
        values = {
            "channel_binding_id": binding.id,
            "provider_connection_id": connection.id,
            "name": (binding.channel_id.name or binding.conversation_ref)[:255],
        }
        try:
            with self.env.cr.savepoint():
                return self.sudo().create(values)
        except IntegrityError:
            profile = self.sudo().search(
                [("channel_binding_id", "=", binding.id)], limit=1
            )
            if not profile:
                raise
            return profile

    def unlink(self):
        channel_ids = sorted(self.sudo().mapped("channel_id").ids)
        if channel_ids:
            self.env.cr.execute(
                "SELECT id FROM mail_channel WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                [channel_ids],
            )
        avatars = self.sudo().mapped("avatar_attachment_id")
        result = super().unlink()
        avatars.exists().sudo().unlink()
        return result

    def _has_active_queue_job(self, *, adopt=True):
        self.ensure_one()
        return bool(
            canonical_queue_job(
                self,
                "contact_center:group_metadata:%s:%s" % (self.id, self.sync_revision),
                _ACTIVE_QUEUE_JOB_STATES,
                adopt=adopt,
            )
        )

    def _request_sync(
        self,
        connection=None,
        force=False,
        eligible_states=None,
        skip_if_active=False,
    ):
        for candidate in self.sudo():
            profile = candidate.exists()
            if not profile:
                continue
            channel_id = profile.channel_id.id
            # Global lock order for every path that may later touch both records:
            # mail.channel -> group profile. Inbound processing follows the same
            # order when it subsequently reconciles channel membership.
            self.env.cr.execute(
                "SELECT id FROM mail_channel WHERE id = %s FOR UPDATE",
                [channel_id],
            )
            if not self.env.cr.fetchone():
                continue
            self.env.cr.execute(
                "SELECT id FROM contact_center_group_profile WHERE id = %s FOR UPDATE",
                [profile.id],
            )
            if not self.env.cr.fetchone():
                continue
            profile.invalidate_recordset(
                [
                    "provider_connection_id",
                    "metadata_state",
                    "last_synced_at",
                    "next_sync_at",
                    "sync_revision",
                    "queue_job_uuid",
                ]
            )
            if eligible_states and profile.metadata_state not in eligible_states:
                continue
            if skip_if_active and profile._has_active_queue_job():
                continue
            # Resolve the implicit connection only after taking the row lock. This
            # prevents a cron request from restoring a connection that a concurrent
            # inbound event has just replaced.
            target_connection = connection or profile.provider_connection_id
            target_connection.ensure_one()
            if target_connection.account_id != profile.account_id:
                raise ValidationError(
                    _("The group synchronization connection is out of scope.")
                )
            now = fields.Datetime.now()
            due = not profile.next_sync_at or profile.next_sync_at <= now
            connection_changed = profile.provider_connection_id != target_connection
            if (
                not force
                and not connection_changed
                and profile.metadata_state in ("pending", "stale")
                and profile._has_active_queue_job()
            ):
                # Ordinary messages do not imply a roster change. Let the current
                # initial/TTL pull finish instead of continuously superseding it.
                continue
            if not (
                force
                or due
                or connection_changed
                or profile.metadata_state in ("unavailable", "failed")
            ):
                continue
            values = {
                "provider_connection_id": target_connection.id,
                "metadata_state": ("stale" if profile.last_synced_at else "pending"),
                "sync_requested_at": now,
                "sync_revision": profile.sync_revision + 1,
                "attempts": 0,
                "last_error_class": False,
                "last_error_message": False,
            }
            if connection_changed:
                values.update(
                    {
                        "own_protocol_participant_json": {},
                        "own_protocol_participant_health_revision": 0,
                    }
                )
            profile.write(values)
            profile.invalidate_recordset(
                ["sync_revision", "metadata_state", "queue_job_uuid"]
            )
            if (
                not self.env.context.get("contact_center_skip_enqueue")
                and not profile._has_active_queue_job()
            ):
                profile._enqueue_sync(profile.sync_revision)
            profile._notify_updated()
        return True

    def _enqueue_sync(self, revision=None, eta=None):
        for profile in self.sudo():
            target_revision = (
                revision if revision is not None else profile.sync_revision
            )
            profile.invalidate_recordset(["sync_revision"])
            if target_revision != profile.sync_revision:
                continue
            identity_key = "contact_center:group_metadata:%s:%s" % (
                profile.id,
                target_revision,
            )
            if canonical_queue_job(
                profile,
                identity_key,
                _ACTIVE_QUEUE_JOB_STATES,
            ):
                continue
            delayed = (
                profile.with_company(profile.company_id)
                .with_delay(
                    identity_key=identity_key,
                    max_retries=0,
                    priority=_GROUP_METADATA_JOB_PRIORITY,
                    description="Contact Center group metadata %s" % profile.id,
                    eta=eta,
                )
                ._job_sync_metadata(target_revision)
            )
            profile.write({"queue_job_uuid": delayed.uuid})
        return True

    def _job_attempt(self):
        self.ensure_one()
        job_uuid = self.env.context.get("job_uuid")
        if job_uuid:
            job = (
                self.env["queue.job"].sudo().search([("uuid", "=", job_uuid)], limit=1)
            )
            if job:
                return self.attempts + job.retry + 1
        return self.attempts + 1

    def _superseded(self, expected_revision, job_uuid, connection_id=None):
        self = self.exists()
        if not self:
            return True
        self.ensure_one()
        self.invalidate_recordset(
            ["sync_revision", "queue_job_uuid", "provider_connection_id"]
        )
        return bool(
            self.sync_revision != expected_revision
            or not self.queue_job_uuid
            or self.queue_job_uuid != job_uuid
            or (
                connection_id is not None
                and self.provider_connection_id.id != connection_id
            )
        )

    def _schedule_latest_after_superseded(self, job_uuid):
        self = self.exists()
        if not self:
            return False
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM mail_channel WHERE id = %s FOR UPDATE",
            [self.channel_id.id],
        )
        if not self.env.cr.fetchone():
            return False
        self.env.cr.execute(
            "SELECT id FROM contact_center_group_profile WHERE id = %s FOR UPDATE",
            [self.id],
        )
        if not self.env.cr.fetchone():
            return False
        self.invalidate_recordset(["sync_revision", "queue_job_uuid"])
        if job_uuid and self.queue_job_uuid == job_uuid:
            self.write({"queue_job_uuid": False})
            self._enqueue_sync(self.sync_revision)
        return False

    def _provider_read_is_available(self, connection):
        connection.ensure_one()
        return bool(
            connection.active
            and connection.account_id.active
            and connection.state == "connected"
            and not connection.identity_mismatch_latched
            and connection._contact_center_observation_is_fresh()
        )

    def _provider_read_preflight(self, connection):
        self.ensure_one()
        connection.invalidate_recordset()
        binding = self.channel_binding_id
        binding.invalidate_recordset(
            ["active", "merged_into_id", "conversation_type", "identity_id"]
        )
        self.channel_id.invalidate_recordset(["active", "channel_type"])
        connection.account_id.invalidate_recordset(["active"])
        if (
            not binding.active
            or binding.merged_into_id
            or binding.conversation_type != "group"
            or binding.identity_id
            or not self.channel_id.active
            or self.channel_id.channel_type != "contact_center"
            or not connection.active
            or not connection.account_id.active
        ):
            raise UnsupportedEventError("group metadata scope is no longer active")
        if not self._provider_read_is_available(connection):
            raise ProviderPausedError(
                "provider connection is not ready for group metadata"
            )
        return connection.health_configuration_revision, binding.conversation_ref

    def _validate_projection_scope(
        self, connection, expected_health_revision, expected_conversation_ref
    ):
        self.ensure_one()
        connection.invalidate_recordset()
        binding = self.channel_binding_id
        binding.invalidate_recordset()
        self.channel_id.invalidate_recordset(["active", "channel_type"])
        connection.account_id.invalidate_recordset(["active"])
        if (
            not binding.active
            or binding.merged_into_id
            or binding.conversation_type != "group"
            or binding.identity_id
            or binding.conversation_ref != expected_conversation_ref
            or not self.channel_id.active
            or self.channel_id.channel_type != "contact_center"
            or not connection.active
            or not connection.account_id.active
        ):
            raise UnsupportedEventError(
                "group metadata scope changed while reading the provider"
            )
        if (
            connection.health_configuration_revision != expected_health_revision
            or not self._provider_read_is_available(connection)
        ):
            raise TransientAdapterError(
                "provider connection changed while reading group metadata"
            )
        return True

    def _job_sync_metadata(self, expected_revision):
        self = self.exists()
        if not self:
            return False
        self.ensure_one()
        job_uuid = self.env.context.get("job_uuid") or ""
        if not queue_job_owns_record(self):
            return False
        if not isinstance(expected_revision, int) or isinstance(
            expected_revision, bool
        ):
            raise ValidationError(_("The group synchronization revision is invalid."))
        if self._superseded(expected_revision, job_uuid):
            return self._schedule_latest_after_superseded(job_uuid)
        connection = self.provider_connection_id
        connection_id = connection.id
        attempt = self._job_attempt()
        try:
            result = self._run_metadata_sync(
                connection, expected_revision, job_uuid, connection_id, attempt
            )
            if result is False:
                return False
        except OperationalError:
            # Let queue_job see the original PostgreSQL error and SQLSTATE. In
            # particular, serialization failures (40001) are retried by its
            # transaction boundary. Inspecting this record after PostgreSQL has
            # aborted the cursor would only mask the recoverable root cause as
            # InFailedSqlTransaction and strand the job in `failed`.
            raise
        except ProviderPausedError as error:
            return self._retry_paused(error, expected_revision, job_uuid, connection_id)
        except TransientAdapterError as error:
            return self._retry_transient(
                error, attempt, expected_revision, job_uuid, connection_id
            )
        except UnsupportedEventError as error:
            if self._superseded(expected_revision, job_uuid, connection_id):
                return self._schedule_latest_after_superseded(job_uuid)
            self._finish_unavailable(error, attempt)
            return False
        except (AdapterError, DTOValidationError, ValidationError) as error:
            if self._superseded(expected_revision, job_uuid, connection_id):
                return self._schedule_latest_after_superseded(job_uuid)
            self._finish_failure(error, attempt)
            return False
        except Exception as error:
            return self._retry_unexpected(
                error, attempt, expected_revision, job_uuid, connection_id
            )
        self._notify_updated()
        return True

    def _run_metadata_sync(
        self, connection, expected_revision, job_uuid, connection_id, attempt
    ):
        expected_health_revision, conversation_ref = self._provider_read_preflight(
            connection
        )
        scope_ids = (
            self.account_id.id,
            self.channel_id.id,
            self.channel_binding_id.id,
        )
        adapter = connection.get_adapter()
        snapshot = adapter.fetch_group_metadata(connection, conversation_ref)
        if not isinstance(snapshot, GroupMetadataDTO):
            raise AdapterError("fetch_group_metadata must return GroupMetadataDTO")
        if snapshot.conversation_ref != conversation_ref:
            raise AdapterError("group metadata belongs to another conversation")
        try:
            avatar = adapter.fetch_group_avatar(connection, conversation_ref)
        except UnsupportedEventError:
            avatar = AvatarResult(state="unavailable")
        if not isinstance(avatar, AvatarResult):
            raise AdapterError("fetch_group_avatar must return AvatarResult")
        if not self._lock_for_projection(connection, *scope_ids):
            return False
        if self._superseded(expected_revision, job_uuid, connection_id=connection_id):
            return self._schedule_latest_after_superseded(job_uuid)
        self._validate_projection_scope(
            connection, expected_health_revision, conversation_ref
        )
        with self.env.cr.savepoint():
            self._apply_snapshot(
                snapshot,
                avatar,
                expected_revision,
                expected_health_revision,
                attempt,
            )
        # Privileged inbound deletes wait for a roster observed after local
        # receipt.  The inbox job that discovered the dependency returned
        # normally (RetryableJobError would roll its transaction back), so a
        # successful complete snapshot can now release those durable waiters.
        with self.env.cr.savepoint() as release_savepoint:
            try:
                self._release_roster_waiters()
            except Exception:
                # Roll back only the release body.  Savepoint construction and its
                # automatic flush stay outside this catch: swallowing an entry-flush
                # database error would leave the outer cursor aborted.
                release_savepoint.rollback()
                # Releasing waiters is an optimization. The generic inbox recovery
                # cron uses the same readiness predicate and must remain the durable
                # fallback; a queue-side failure cannot invalidate a valid snapshot.
                _logger.exception(
                    "Could not release Contact Center group-roster waiters for "
                    "profile %s",
                    self.id,
                )
        return True

    def _retry_paused(self, error, expected_revision, job_uuid, connection_id):
        if self._superseded(expected_revision, job_uuid, connection_id):
            return self._schedule_latest_after_superseded(job_uuid)
        raise RetryableJobError(
            str(error),
            seconds=provider_paused_retry_seconds(
                error,
                ("group", self.id),
            ),
            ignore_retry=True,
        ) from error

    def _retry_transient(
        self, error, attempt, expected_revision, job_uuid, connection_id
    ):
        if self._superseded(expected_revision, job_uuid, connection_id):
            return self._schedule_latest_after_superseded(job_uuid)
        if attempt >= QUEUE_ATTEMPT_CEILING:
            self._finish_failure(error, attempt)
            return False
        raise RetryableJobError(
            str(error),
            seconds=getattr(error, "retry_after_seconds", 0) or None,
        ) from error

    def _retry_unexpected(
        self, error, attempt, expected_revision, job_uuid, connection_id
    ):
        if self._superseded(expected_revision, job_uuid, connection_id):
            return self._schedule_latest_after_superseded(job_uuid)
        if attempt >= QUEUE_ATTEMPT_CEILING:
            self._finish_failure(error, attempt)
            return False
        raise RetryableJobError(str(error), seconds=None) from error

    def _lock_for_projection(self, connection, account_id, channel_id, binding_id):
        self.ensure_one()
        connection.ensure_one()
        # Parent-first ordering makes concurrent account/channel/binding deletion
        # converge without crossing the child profile lock held by this job.
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id = %s FOR SHARE",
            [account_id],
        )
        if not self.env.cr.fetchone():
            return False
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection WHERE id = %s FOR SHARE",
            [connection.id],
        )
        if not self.env.cr.fetchone():
            return False
        self.env.cr.execute(
            "SELECT id FROM mail_channel WHERE id = %s FOR UPDATE",
            [channel_id],
        )
        if not self.env.cr.fetchone():
            return False
        self.env.cr.execute(
            "SELECT id FROM contact_center_channel_binding WHERE id = %s FOR SHARE",
            [binding_id],
        )
        if not self.env.cr.fetchone():
            return False
        self.env.cr.execute(
            "SELECT id FROM contact_center_group_profile WHERE id = %s FOR UPDATE",
            [self.id],
        )
        if not self.env.cr.fetchone():
            return False
        self.invalidate_recordset()
        return True

    @api.model
    def _snapshot_hash(self, snapshot):
        values = {
            "conversation_ref": snapshot.conversation_ref,
            "display_name": snapshot.display_name,
            "participant_count": snapshot.participant_count,
            "own_role": snapshot.own_role,
            "own_protocol_participant": (
                snapshot.own_protocol_participant.to_dict()
                if snapshot.own_protocol_participant
                else None
            ),
            "provider_revision": snapshot.provider_revision,
            "is_complete": snapshot.is_complete,
            "participants": sorted(
                (
                    {
                        "participant_ref": participant.participant_ref,
                        "display_name": participant.display_name,
                        "role": participant.role,
                        "addresses": sorted(
                            (
                                {
                                    "namespace": address.namespace,
                                    "value_raw": address.value,
                                    "value": address.value_normalized,
                                    "role": address.role,
                                    "source_field": address.source_field,
                                    "confidence": address.confidence,
                                }
                                for address in participant.addresses
                            ),
                            key=lambda item: (
                                item["namespace"],
                                item["value"],
                                item["role"],
                            ),
                        ),
                    }
                    for participant in snapshot.participants
                ),
                key=lambda item: item["participant_ref"],
            ),
        }
        payload = json.dumps(
            values,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _apply_snapshot(
        self,
        snapshot,
        avatar,
        expected_revision,
        expected_health_revision,
        attempt,
    ):
        self.ensure_one()
        observed_at = _odoo_datetime(snapshot.observed_at)
        if self.last_synced_at and observed_at < self.last_synced_at:
            raise AdapterError("group metadata observation moved backwards")
        snapshot_hash = self._snapshot_hash(snapshot)
        if snapshot_hash != self.snapshot_hash:
            self._reconcile_participants(snapshot)
        admin_count = sum(
            participant.role in ("admin", "superadmin")
            for participant in snapshot.participants
        )
        participant_count = (
            snapshot.participant_count
            if snapshot.is_complete
            else max(self.participant_count, snapshot.participant_count)
        )
        if not snapshot.is_complete:
            admin_count = max(self.admin_count, admin_count)
        group_name = (snapshot.display_name or self.name or "").strip()[:255]
        values = {
            "name": group_name or False,
            "metadata_state": "ready" if snapshot.is_complete else "stale",
            "roster_complete": bool(snapshot.is_complete),
            "participant_count": participant_count,
            "admin_count": min(admin_count, participant_count),
            "own_role": (
                snapshot.own_role
                if snapshot.own_role != "unknown" or self.own_role == "unknown"
                else self.own_role
            ),
            "own_protocol_participant_json": (
                snapshot.own_protocol_participant.to_dict()
                if snapshot.own_protocol_participant
                else {}
            ),
            "own_protocol_participant_health_revision": (
                expected_health_revision if snapshot.own_protocol_participant else 0
            ),
            "provider_revision": snapshot.provider_revision or False,
            "snapshot_hash": snapshot_hash,
            "last_synced_at": observed_at,
            "next_sync_at": observed_at
            + (_GROUP_METADATA_TTL if snapshot.is_complete else _GROUP_PARTIAL_RETRY),
            "applied_revision": expected_revision,
            "attempts": attempt,
            "queue_job_uuid": False,
            "last_error_class": False,
            "last_error_message": False,
        }
        values.update(self._avatar_values(avatar))
        previous_attachment = self.avatar_attachment_id
        self.write(values)
        if group_name and self.channel_id.name != group_name:
            self.channel_id.sudo().write({"name": group_name})
        if previous_attachment and previous_attachment != self.avatar_attachment_id:
            previous_attachment.sudo().unlink()

    def _release_roster_waiters(self, limit=100):
        """Re-enqueue inbox mutations covered by this committed roster revision.

        The generic orphan-recovery cron uses the same readiness predicate as a
        fallback, so the bounded batch cannot strand an unusually large burst.
        """

        self.ensure_one()
        limit = max(0, min(int(limit), 500))
        if (
            not limit
            or self.metadata_state != "ready"
            or not self.roster_complete
            or not self.sync_requested_at
            or not self.last_synced_at
            or self.applied_revision != self.sync_revision
        ):
            return 0
        covered_through = min(
            fields.Datetime.to_datetime(self.sync_requested_at),
            fields.Datetime.to_datetime(self.last_synced_at),
        )
        inbox_model = self.env["contact.center.inbox.event"].sudo()
        inbox_model.flush_model(
            [
                "state",
                "queue_job_uuid",
                "waiting_group_profile_id",
                "waiting_group_roster_after",
            ]
        )
        self.env.cr.execute(
            """
            SELECT id
              FROM contact_center_inbox_event
             WHERE state = 'pending'
               AND waiting_group_profile_id = %s
               AND waiting_group_roster_after <= %s
          ORDER BY id
             FOR UPDATE SKIP LOCKED
             LIMIT %s
            """,
            [self.id, covered_through, limit],
        )
        waiters = inbox_model.browse([row[0] for row in self.env.cr.fetchall()])
        if not waiters:
            return 0
        waiters.invalidate_recordset(
            [
                "state",
                "queue_job_uuid",
                "waiting_group_profile_id",
                "waiting_group_roster_after",
            ]
        )
        waiters.write(
            {
                "queue_job_uuid": False,
                "waiting_group_profile_id": False,
                "waiting_group_roster_after": False,
                "last_error_class": False,
                "last_error_message": False,
            }
        )
        waiters._enqueue()
        return len(waiters)

    def _reconcile_participants(self, snapshot):
        self.ensure_one()
        participant_model = self.env["contact.center.group.participant"].sudo()
        alias_model = self.env["contact.center.group.participant.alias"].sudo()
        participants = participant_model.with_context(active_test=False).search(
            [("group_profile_id", "=", self.id)], order="id"
        )
        aliases = alias_model.search([("group_profile_id", "=", self.id)], order="id")
        by_address = {
            (alias.namespace, alias.value_normalized): alias.participant_id
            for alias in aliases
        }
        by_ref = {
            participant.participant_ref: participant for participant in participants
        }
        seen_ids = set()
        now = fields.Datetime.now()
        for item in snapshot.participants:
            keys = {
                (address.namespace, address.value_normalized)
                for address in item.addresses
            }
            candidates = participant_model
            for key in keys:
                candidates |= by_address.get(key, participant_model)
            reference_match = by_ref.get(item.participant_ref, participant_model)
            if reference_match:
                candidates |= reference_match
            if len(candidates) > 1:
                raise ValidationError(
                    _("Group participant aliases resolve to multiple roster entries.")
                )
            participant = candidates[:1]
            if participant and participant.id in seen_ids:
                raise ValidationError(
                    _("Two group roster items resolve to the same participant.")
                )
            values = {
                "participant_ref": item.participant_ref,
                "name": (item.display_name or "").strip()[:255] or False,
                "role": item.role,
                "active": True,
                "last_seen_at": now,
                "removed_at": False,
            }
            if participant:
                participant.write(values)
            else:
                participant = participant_model.create(
                    {"group_profile_id": self.id, **values}
                )
                participants |= participant
            by_ref[item.participant_ref] = participant
            seen_ids.add(participant.id)
            for address in item.addresses:
                key = (address.namespace, address.value_normalized)
                existing = by_address.get(key)
                if existing and existing != participant:
                    raise ValidationError(
                        _("A group participant address belongs to another entry.")
                    )
                alias = aliases.filtered(
                    lambda candidate, key=key: (
                        candidate.namespace,
                        candidate.value_normalized,
                    )
                    == key
                )[:1]
                alias_values = {
                    "participant_id": participant.id,
                    "namespace": address.namespace,
                    "value_raw": address.value,
                    "value_normalized": address.value_normalized,
                    "role": address.role,
                    "source_field": address.source_field or False,
                    "confidence": address.confidence,
                    "last_seen_at": now,
                }
                if alias:
                    alias.write(alias_values)
                else:
                    alias = alias_model.create(alias_values)
                    aliases |= alias
                by_address[key] = participant
        if snapshot.is_complete:
            removed = participants.filtered(
                lambda participant: participant.active
                and participant.id not in seen_ids
            )
            removed.write({"active": False, "removed_at": now, "last_seen_at": now})

    def _avatar_values(self, avatar):
        self.ensure_one()
        if avatar.state == "unavailable":
            return {}
        if avatar.state == "absent":
            return {
                "avatar_attachment_id": False,
                "avatar_sha256": False,
                "avatar_provider_revision": avatar.provider_revision or False,
            }
        if avatar.size_bytes > _GROUP_AVATAR_MAX_BYTES:
            raise AdapterError("group avatar exceeds the size limit")
        declared_mimetype = avatar.mime_type.split(";", 1)[0].strip().lower()
        detected_mimetype = guess_mimetype(avatar.content, default=declared_mimetype)
        detected_mimetype = detected_mimetype.split(";", 1)[0].strip().lower()
        if (
            declared_mimetype not in _GROUP_AVATAR_MIMETYPES
            or detected_mimetype != declared_mimetype
        ):
            raise AdapterError("group avatar MIME type is not supported")
        if self.avatar_sha256 == avatar.sha256 and self.avatar_attachment_id:
            return {
                "avatar_provider_revision": avatar.provider_revision or False,
            }
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .with_context(image_no_postprocess=True)
            .create(
                {
                    "name": (avatar.file_name or "group-avatar")[:255],
                    "type": "binary",
                    "datas": base64.b64encode(avatar.content),
                    "mimetype": declared_mimetype,
                    "res_model": self._name,
                    "res_id": self.id,
                }
            )
        )
        return {
            "avatar_attachment_id": attachment.id,
            "avatar_sha256": avatar.sha256,
            "avatar_provider_revision": avatar.provider_revision or False,
        }

    def _finish_failure(self, error, attempt):
        self.ensure_one()
        self.write(
            {
                "metadata_state": "failed",
                "attempts": attempt,
                "queue_job_uuid": False,
                "next_sync_at": fields.Datetime.now() + datetime.timedelta(hours=24),
                "last_error_class": error.__class__.__name__[:128],
                "last_error_message": str(error)[:4000],
            }
        )
        self._finalize_roster_waiters_best_effort(profile_failed=True)
        self._notify_updated()

    def _finish_unavailable(self, error, attempt):
        self.ensure_one()
        self.write(
            {
                "metadata_state": ("stale" if self.last_synced_at else "unavailable"),
                "attempts": attempt,
                "queue_job_uuid": False,
                "next_sync_at": fields.Datetime.now() + datetime.timedelta(hours=24),
                "last_error_class": error.__class__.__name__[:128],
                "last_error_message": str(error)[:4000],
            }
        )
        self._finalize_roster_waiters_best_effort(profile_unavailable=True)
        self._notify_updated()

    def _finalize_roster_waiters_best_effort(
        self, *, profile_unavailable=False, profile_failed=False
    ):
        """Isolate optional waiter finalization from the profile transition."""

        self.ensure_one()
        with self.env.cr.savepoint() as finalization_savepoint:
            try:
                inbox_model = self.env["contact.center.inbox.event"].sudo()
                if profile_failed:
                    inbox_model._finalize_failed_group_roster_waiters_locked(
                        limit=500,
                        profile_ids=self.ids,
                        profile_transition_locked=True,
                    )
                else:
                    inbox_model._finalize_invalid_group_roster_waiters(
                        limit=500,
                        profile_ids=self.ids,
                        profile_unavailable=profile_unavailable,
                    )
            except Exception:
                # Keep failures from the optional body isolated, but never catch a
                # failure while opening or closing the savepoint itself.
                finalization_savepoint.rollback()
                _logger.exception(
                    "Could not finalize group-roster waiters for profile %s",
                    self.id,
                )
        return True

    def _notify_updated(self):
        for profile in self.exists():
            self.env["contact.center.application"]._notify_ui(
                profile.channel_id,
                "conversation_updated",
                {"channel_id": profile.channel_id.id},
            )
        return True

    @api.model
    def _cron_schedule_group_metadata(self, limit=100):
        now = fields.Datetime.now()
        binding_model = self.env["contact.center.channel.binding"]
        message_binding_model = self.env["contact.center.message.binding"]
        binding_model.flush_model(
            [
                "conversation_type",
                "identity_id",
                "active",
                "merged_into_id",
                "channel_id",
                "account_id",
            ]
        )
        self.env["mail.channel"].flush_model(["active"])
        self.env["contact.center.account"].flush_model(["active"])
        message_binding_model.flush_model(
            ["channel_binding_id", "provider_connection_id"]
        )
        self.flush_model(
            [
                "channel_binding_id",
                "next_sync_at",
                "metadata_state",
                "queue_job_uuid",
                "sync_requested_at",
            ]
        )
        self.env["queue.job"].flush_model(["uuid", "identity_key", "state"])
        self.env.cr.execute(
            """
            SELECT binding.id
              FROM contact_center_channel_binding AS binding
              JOIN mail_channel AS channel ON channel.id = binding.channel_id
              JOIN contact_center_account AS account
                ON account.id = binding.account_id
         LEFT JOIN contact_center_group_profile AS profile
                ON profile.channel_binding_id = binding.id
             WHERE binding.conversation_type = 'group'
               AND binding.identity_id IS NULL
               AND binding.active IS TRUE
               AND binding.merged_into_id IS NULL
               AND profile.id IS NULL
               AND channel.active IS TRUE
               AND account.active IS TRUE
               AND EXISTS (
                    SELECT 1
                      FROM contact_center_message_binding AS message
                     WHERE message.channel_binding_id = binding.id
                       AND message.provider_connection_id IS NOT NULL
               )
          ORDER BY binding.id
             FOR UPDATE OF channel SKIP LOCKED
             LIMIT %s
            """,
            [limit],
        )
        missing_bindings = binding_model.sudo().browse(
            [row[0] for row in self.env.cr.fetchall()]
        )
        message_binding_model = message_binding_model.sudo()
        for binding in missing_bindings:
            last_message = message_binding_model.search(
                [
                    ("channel_binding_id", "=", binding.id),
                    ("provider_connection_id", "!=", False),
                ],
                order="id desc",
                limit=1,
            )
            if not last_message.provider_connection_id:
                continue
            profile = self._get_or_create(binding, last_message.provider_connection_id)
            profile._request_sync(last_message.provider_connection_id, force=True)
        remaining = max(0, limit - len(missing_bindings))
        if remaining:
            # The loop above may have created or updated profiles in this same
            # transaction. Flush only the columns read by the SQL recovery query.
            self.flush_model(
                [
                    "channel_binding_id",
                    "next_sync_at",
                    "metadata_state",
                    "queue_job_uuid",
                    "sync_requested_at",
                ]
            )
            self.env["queue.job"].flush_model(["uuid", "identity_key", "state"])
            self.env.cr.execute(
                """
                SELECT profile.id
                  FROM contact_center_group_profile AS profile
                  JOIN contact_center_channel_binding AS binding
                    ON binding.id = profile.channel_binding_id
                  JOIN mail_channel AS channel ON channel.id = binding.channel_id
                  JOIN contact_center_account AS account
                    ON account.id = binding.account_id
                 WHERE (
                        (
                            profile.next_sync_at IS NOT NULL
                            AND profile.next_sync_at <= %s
                            AND profile.metadata_state
                                IN ('ready', 'stale', 'failed', 'unavailable')
                        )
                        OR (
                            (
                                profile.metadata_state = 'pending'
                                OR (
                                    profile.metadata_state = 'stale'
                                    AND profile.queue_job_uuid IS NOT NULL
                                )
                            )
                            AND (
                                profile.sync_requested_at IS NULL
                                OR profile.sync_requested_at <= %s
                            )
                        )
                   )
                   AND NOT EXISTS (
                        SELECT 1
                          FROM queue_job AS job
                         WHERE job.uuid = profile.queue_job_uuid
                           AND job.identity_key =
                               'contact_center:group_metadata:'
                               || profile.id::text || ':'
                               || profile.sync_revision::text
                           AND job.state IN (
                               'pending', 'enqueued', 'started',
                               'wait_dependencies'
                           )
                   )
                   AND binding.conversation_type = 'group'
                   AND binding.identity_id IS NULL
                   AND binding.active IS TRUE
                   AND binding.merged_into_id IS NULL
                   AND channel.active IS TRUE
                   AND account.active IS TRUE
              ORDER BY COALESCE(
                           profile.next_sync_at,
                           profile.sync_requested_at,
                           profile.create_date
                       ), profile.id
                 FOR UPDATE OF channel SKIP LOCKED
                 LIMIT %s
                """,
                [now, now - datetime.timedelta(minutes=5), remaining],
            )
            profiles = (
                self.sudo().browse([row[0] for row in self.env.cr.fetchall()]).exists()
            )
            # The SQL intentionally selects missing/stale UUID pointers.  Re-check
            # the canonical revision identity under the profile lock before forcing
            # a successor: a live job may only need its denormalized pointer adopted.
            profiles._request_sync(force=True, skip_if_active=True)
        return True


class ContactCenterGroupParticipant(models.Model):
    _name = "contact.center.group.participant"
    _description = "Contact Center Group Participant"
    _rec_name = "name"
    _order = "group_profile_id, id"
    _check_company_auto = True

    group_profile_id = fields.Many2one(
        "contact.center.group.profile",
        required=True,
        index=True,
        ondelete="cascade",
    )
    account_id = fields.Many2one(
        related="group_profile_id.account_id",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="group_profile_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    participant_ref = fields.Char(
        required=True,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    name = fields.Char()
    role = fields.Selection(
        [
            ("member", "Member"),
            ("admin", "Administrator"),
            ("superadmin", "Super Administrator"),
        ],
        required=True,
        default="member",
        index=True,
    )
    active = fields.Boolean(default=True, index=True)
    alias_ids = fields.One2many(
        "contact.center.group.participant.alias",
        "participant_id",
        string="Addresses",
    )
    first_seen_at = fields.Datetime(required=True, default=fields.Datetime.now)
    last_seen_at = fields.Datetime(required=True, default=fields.Datetime.now)
    removed_at = fields.Datetime(copy=False)

    _sql_constraints = [
        (
            "profile_reference_unique",
            "unique(group_profile_id, participant_ref)",
            "A group participant reference must be unique within its group.",
        ),
    ]


class ContactCenterGroupParticipantAlias(models.Model):
    _name = "contact.center.group.participant.alias"
    _description = "Contact Center Group Participant Address"
    _order = "group_profile_id, namespace, value_normalized"
    _check_company_auto = True

    participant_id = fields.Many2one(
        "contact.center.group.participant",
        required=True,
        index=True,
        ondelete="cascade",
    )
    group_profile_id = fields.Many2one(
        related="participant_id.group_profile_id",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="participant_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    namespace = fields.Char(
        required=True,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    value_raw = fields.Char(
        required=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    value_normalized = fields.Char(
        required=True,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    role = fields.Selection(
        [
            ("primary", "Primary"),
            ("alternate", "Alternate"),
            ("sender", "Sender"),
            ("recipient", "Recipient"),
            ("device", "Device"),
        ],
        required=True,
        default="primary",
    )
    source_field = fields.Char()
    confidence = fields.Selection(
        [
            ("observed", "Observed"),
            ("protocol", "Protocol-validated"),
            ("manual", "Manual"),
        ],
        required=True,
        default="observed",
    )
    first_seen_at = fields.Datetime(required=True, default=fields.Datetime.now)
    last_seen_at = fields.Datetime(required=True, default=fields.Datetime.now)

    _sql_constraints = [
        (
            "profile_address_unique",
            "unique(group_profile_id, namespace, value_normalized)",
            "A group participant address must be unique within its group.",
        ),
    ]

    @api.constrains("participant_id", "group_profile_id")
    def _check_profile_consistency(self):
        for alias in self:
            if alias.group_profile_id != alias.participant_id.group_profile_id:
                raise ValidationError(
                    _("The participant address belongs to another group profile.")
                )
