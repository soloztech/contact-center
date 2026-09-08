import datetime
import uuid
from unittest import mock

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from ..models.application import GroupRosterRefreshRequired
from ..services.tokens import CONTACT_CENTER_MEMBERSHIP_TOKEN


class TestQueueRecovery(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        admin_group = cls.env.ref("contact_center_base.group_contact_center_admin")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Queue Recovery Agent",
                    "login": "cc-queue-agent-%s" % uuid.uuid4(),
                    "email": "cc-queue-agent@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, agent_group.ids)],
                }
            )
        )
        cls.admin = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Queue Recovery Administrator",
                    "login": "cc-queue-admin-%s" % uuid.uuid4(),
                    "email": "cc-queue-admin@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, admin_group.ids)],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Queue Recovery Team %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Queue Recovery Account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "queue-account-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, cls.team.ids)],
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Queue Recovery Connection",
                "account_id": cls.account.id,
                "adapter_key": "test.fake",
                "external_ref": "queue-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "queue-fixture-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {"send_message": True},
            }
        )
        guest = cls.env["mail.guest"].sudo().create({"name": "Queue Guest"})
        identity = (
            cls.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": "Queue Guest",
                    "company_id": cls.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        cls.channel = cls.env["mail.channel"]._contact_center_create_channel(
            account=cls.account,
            identity=identity,
            teams=cls.team,
            partner_ids=cls.agent.partner_id.ids,
            guest_ids=guest.ids,
        )
        cls.channel_binding = (
            cls.env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": cls.channel.id,
                    "account_id": cls.account.id,
                    "identity_id": identity.id,
                    "conversation_type": "direct",
                    "conversation_ref": "queue-conversation-%s" % uuid.uuid4(),
                }
            )
        )

    def test_recovery_indexes_are_owned_by_their_existing_queue_tables(self):
        self.env.cr.execute(
            """
            SELECT tablename, indexname
              FROM pg_indexes
             WHERE schemaname = current_schema()
               AND indexname IN (
                    'cc_onboarding_release_eligible_idx',
                    'cc_outbox_recovery_eligible_idx'
               )
             ORDER BY indexname
            """
        )
        self.assertEqual(
            self.env.cr.fetchall(),
            [
                (
                    "contact_center_inbox_event",
                    "cc_onboarding_release_eligible_idx",
                ),
                (
                    "contact_center_outbox_command",
                    "cc_outbox_recovery_eligible_idx",
                ),
            ],
        )

    def _inbox(self, state="pending"):
        return (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "queue-inbox-%s" % uuid.uuid4(),
                    "provider_schema_version": "queue-fixture-v1",
                    "raw_envelope_json": {"fixture": True},
                    "state": state,
                }
            )
        )

    def _outbox(self, state="pending"):
        return (
            self.env["contact.center.outbox.command"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "account_id": self.account.id,
                    "provider_connection_id": self.connection.id,
                    "channel_binding_id": self.channel_binding.id,
                    "outbox_idempotency_key": "queue-outbox-%s" % uuid.uuid4(),
                    "command_type": "queue_fixture",
                    "command_json": {"fixture": True},
                    "state": state,
                }
            )
        )

    def _media(self, state="pending"):
        message = self.channel.sudo()._contact_center_post(
            origin="inbound",
            body="Queue recovery media",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        message_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": self.channel_binding.id,
                    "provider_connection_id": self.connection.id,
                    "direction": "inbound",
                    "origin": "provider",
                    "content_type": "image",
                    "external_message_id": "queue-message-%s" % uuid.uuid4(),
                    "delivery_state": "delivered",
                }
            )
        )
        return (
            self.env["contact.center.media.binding"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "message_binding_id": message_binding.id,
                    "kind": "image",
                    "external_media_id": "queue-media-%s" % uuid.uuid4(),
                    "remote_locator_json": {"fixture": True},
                    "mime_type": "image/png",
                    "size_bytes": 1,
                    "state": state,
                }
            )
        )

    def _group_scope(self, suffix=None, *, isolated=False):
        suffix = suffix or uuid.uuid4().hex
        account = self.account
        connection = self.connection
        if isolated:
            account = self.env["contact.center.account"].create(
                {
                    "name": "Queue Group Account %s" % suffix,
                    "company_id": self.env.company.id,
                    "platform": "whatsapp",
                    "external_ref": "queue-group-account-%s" % suffix,
                    "access_team_ids": [(6, 0, self.team.ids)],
                }
            )
            connection = self.env["contact.center.provider.connection"].create(
                {
                    "name": "Queue Group Connection %s" % suffix,
                    "account_id": account.id,
                    "adapter_key": "test.fake",
                    "external_ref": "queue-group-connection-%s" % suffix,
                    "provider_schema_version": "queue-fixture-v1",
                    "state": "connected",
                    "active": True,
                    "role": "primary",
                    "inbound_active": True,
                    "outbound_active": False,
                    "capabilities_json": {"send_message": False},
                }
            )
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=account,
            conversation_type="group",
            name="Queue Group %s" % suffix,
            teams=self.team,
            partner_ids=self.agent.partner_id.ids,
        )
        binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": channel.id,
                    "account_id": account.id,
                    "conversation_type": "group",
                    "conversation_ref": "120363%s@g.us" % suffix,
                }
            )
        )
        profile = (
            self.env["contact.center.group.profile"]
            .sudo()
            .create(
                {
                    "channel_binding_id": binding.id,
                    "provider_connection_id": connection.id,
                    "name": "Queue Group %s" % suffix,
                }
            )
        )
        return account, connection, channel, binding, profile

    def _group_waiter(self, profile, connection=None):
        connection = connection or profile.provider_connection_id
        waiter = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": connection.id,
                    "inbox_dedupe_key": "queue-group-waiter-%s" % uuid.uuid4(),
                    "provider_schema_version": "queue-fixture-v1",
                    "raw_envelope_json": {"fixture": "group-waiter"},
                }
            )
        )
        required_after = fields.Datetime.now() - datetime.timedelta(seconds=1)
        waiter.write(
            {
                "waiting_group_profile_id": profile.id,
                "waiting_group_roster_after": required_after,
                "last_error_class": "GroupRosterRefreshRequired",
                "last_error_message": "waiting for group roster",
                "group_roster_wait_count": 1,
                "first_group_roster_wait_at": fields.Datetime.now(),
            }
        )
        return waiter

    @staticmethod
    def _make_group_profile_ready(profile):
        observed_at = fields.Datetime.now()
        profile.sudo().write(
            {
                "metadata_state": "ready",
                "roster_complete": True,
                "sync_requested_at": observed_at,
                "last_synced_at": observed_at,
                "applied_revision": profile.sync_revision,
                "queue_job_uuid": False,
            }
        )

    def _active_jobs(self, lane, record):
        return (
            self.env["queue.job"]
            .sudo()
            .search(
                [
                    ("identity_key", "=", "contact_center:%s:%s" % (lane, record.id)),
                    (
                        "state",
                        "in",
                        ("pending", "enqueued", "started", "wait_dependencies"),
                    ),
                ]
            )
        )

    def _age_for_recovery(self, *records):
        """Move only test fixtures beyond the production grace window."""

        model_tables = {
            "contact.center.inbox.event": "contact_center_inbox_event",
            "contact.center.outbox.command": "contact_center_outbox_command",
            "contact.center.media.binding": "contact_center_media_binding",
        }
        # ``sudo()`` recordsets used by enqueue/write can retain a pending
        # log-access value in another environment cache. Flush every environment
        # before the deliberate SQL time travel, then invalidate without another
        # flush so a cached current ``write_date`` cannot overwrite the fixture.
        self.env.flush_all()
        stale_at = fields.Datetime.now() - datetime.timedelta(minutes=10)
        for record in records:
            table = model_tables[record._name]
            self.env.cr.execute(
                "UPDATE %s SET write_date = %%s WHERE id = %%s" % table,
                [stale_at, record.id],
            )
        self.env.invalidate_all(flush=False)

    def test_cron_obeys_grace_and_is_idempotent_across_all_lanes(self):
        inbox = self._inbox()
        outbox = self._outbox()
        media = self._media()
        recovery = self.env["contact.center.inbox.event"].sudo()

        untouched = recovery._cron_recover_orphaned_queue_jobs(
            limit_per_model=10, grace_seconds=300
        )

        self.assertEqual(untouched, {"inbox": 0, "outbox": 0, "media": 0})
        self.assertFalse(inbox.queue_job_uuid)
        self.assertFalse(outbox.queue_job_uuid)
        self.assertFalse(media.queue_job_uuid)
        self._age_for_recovery(inbox, outbox, media)

        recovered = recovery._cron_recover_orphaned_queue_jobs(
            limit_per_model=10, grace_seconds=0
        )

        self.assertEqual(recovered, {"inbox": 1, "outbox": 1, "media": 1})
        records = (("inbox", inbox), ("outbox", outbox), ("media", media))
        first_uuids = {}
        for lane, record in records:
            record.invalidate_recordset(["queue_job_uuid"])
            self.assertTrue(record.queue_job_uuid)
            self.assertEqual(len(self._active_jobs(lane, record)), 1)
            first_uuids[lane] = record.queue_job_uuid

        repeated = recovery._cron_recover_orphaned_queue_jobs(
            limit_per_model=10, grace_seconds=0
        )

        self.assertEqual(repeated, {"inbox": 0, "outbox": 0, "media": 0})
        for lane, record in records:
            record.invalidate_recordset(["queue_job_uuid"])
            self.assertEqual(record.queue_job_uuid, first_uuids[lane])
            self.assertEqual(len(self._active_jobs(lane, record)), 1)

    def test_group_waiter_recovery_requires_the_complete_covering_roster(self):
        _account, _connection, _channel, _binding, profile = self._group_scope()
        waiter = self._group_waiter(profile)
        self._age_for_recovery(waiter)
        recovery = self.env["contact.center.inbox.event"].sudo()

        before_ready = recovery._cron_recover_orphaned_queue_jobs(
            limit_per_model=10, grace_seconds=0
        )

        waiter.invalidate_recordset(["queue_job_uuid", "state"])
        self.assertEqual(before_ready["inbox"], 0)
        self.assertEqual(waiter.state, "pending")
        self.assertFalse(waiter.queue_job_uuid)

        self._make_group_profile_ready(profile)
        after_ready = recovery._cron_recover_orphaned_queue_jobs(
            limit_per_model=10, grace_seconds=0
        )

        waiter.invalidate_recordset(["queue_job_uuid", "state"])
        self.assertEqual(after_ready["inbox"], 1)
        self.assertEqual(waiter.state, "pending")
        self.assertTrue(waiter.queue_job_uuid)

    def test_group_waiter_release_is_bounded_and_isolated_by_profile(self):
        _account, _connection, _channel, _binding, first_profile = self._group_scope(
            "release-first"
        )
        _account, _connection, _channel, _binding, second_profile = self._group_scope(
            "release-second"
        )
        first_waiters = self._group_waiter(first_profile) | self._group_waiter(
            first_profile
        )
        first_waiters[0].write({"attempts": 3})
        first_waiters[1].write({"attempts": 7})
        second_waiter = self._group_waiter(second_profile)
        self._make_group_profile_ready(first_profile)
        self._make_group_profile_ready(second_profile)

        self.assertEqual(first_profile._release_roster_waiters(limit=1), 1)

        first_waiters.invalidate_recordset(
            ["queue_job_uuid", "waiting_group_profile_id", "attempts"]
        )
        second_waiter.invalidate_recordset(
            ["queue_job_uuid", "waiting_group_profile_id"]
        )
        self.assertEqual(len(first_waiters.filtered("queue_job_uuid")), 1)
        self.assertEqual(len(first_waiters.filtered("waiting_group_profile_id")), 1)
        self.assertFalse(second_waiter.queue_job_uuid)
        self.assertEqual(second_waiter.waiting_group_profile_id, second_profile)

        self.assertEqual(first_profile._release_roster_waiters(limit=10), 1)
        first_waiters.invalidate_recordset(
            ["queue_job_uuid", "waiting_group_profile_id", "attempts"]
        )
        self.assertTrue(all(first_waiters.mapped("queue_job_uuid")))
        self.assertFalse(first_waiters.mapped("waiting_group_profile_id"))
        self.assertEqual(first_waiters.mapped("attempts"), [3, 7])
        self.assertFalse(second_waiter.queue_job_uuid)

    def test_group_roster_defer_release_defer_preserves_lifetime_counters(self):
        _account, connection, _channel, _binding, profile = self._group_scope(
            "defer-release-defer"
        )
        waiter = self._inbox()
        required_after = fields.Datetime.now() - datetime.timedelta(seconds=2)
        first_error = GroupRosterRefreshRequired(
            profile.id,
            connection.id,
            required_after,
        )
        profile.write(
            {
                "metadata_state": "unavailable",
                "last_error_class": "UnsupportedEventError",
                "last_error_message": "obsolete provider observation",
            }
        )

        with mock.patch.object(type(profile), "_enqueue_sync", return_value=True):
            self.assertFalse(waiter._defer_for_group_roster(first_error, 1))

        waiter.invalidate_recordset(
            [
                "attempts",
                "group_roster_wait_count",
                "first_group_roster_wait_at",
                "waiting_group_profile_id",
            ]
        )
        profile.invalidate_recordset(["metadata_state", "last_error_class"])
        first_wait_at = waiter.first_group_roster_wait_at
        self.assertEqual(waiter.attempts, 1)
        self.assertEqual(waiter.group_roster_wait_count, 1)
        self.assertTrue(first_wait_at)
        self.assertEqual(waiter.waiting_group_profile_id, profile)
        self.assertIn(profile.metadata_state, ("pending", "stale"))
        self.assertFalse(profile.last_error_class)
        self.assertEqual(
            self.env["contact.center.inbox.event"]
            .sudo()
            ._finalize_invalid_group_roster_waiters(limit=10),
            0,
        )

        self._make_group_profile_ready(profile)
        with mock.patch.object(type(waiter), "_enqueue", return_value=True):
            self.assertEqual(profile._release_roster_waiters(), 1)
        waiter.invalidate_recordset(
            [
                "attempts",
                "group_roster_wait_count",
                "first_group_roster_wait_at",
                "waiting_group_profile_id",
            ]
        )
        self.assertEqual(waiter.attempts, 1)
        self.assertEqual(waiter.group_roster_wait_count, 1)
        self.assertEqual(waiter.first_group_roster_wait_at, first_wait_at)
        self.assertFalse(waiter.waiting_group_profile_id)

        profile.write({"metadata_state": "stale", "queue_job_uuid": False})
        second_required_after = fields.Datetime.now()
        second_error = GroupRosterRefreshRequired(
            profile.id,
            connection.id,
            second_required_after,
        )
        with mock.patch.object(type(profile), "_enqueue_sync", return_value=True):
            self.assertFalse(waiter._defer_for_group_roster(second_error, 2))

        waiter.invalidate_recordset(
            [
                "attempts",
                "group_roster_wait_count",
                "first_group_roster_wait_at",
                "waiting_group_profile_id",
            ]
        )
        self.assertEqual(waiter.attempts, 2)
        self.assertEqual(waiter.group_roster_wait_count, 2)
        self.assertEqual(waiter.first_group_roster_wait_at, first_wait_at)
        self.assertEqual(waiter.waiting_group_profile_id, profile)

    def test_failed_group_profile_dead_letters_waiter_not_unsupported(self):
        _account, _connection, _channel, _binding, profile = self._group_scope(
            "failed-profile"
        )
        waiter = self._group_waiter(profile)
        waiter.write({"attempts": 4})
        profile.write(
            {
                "metadata_state": "failed",
                "last_error_class": "DTOValidationError",
                "last_error_message": "invalid provider-neutral group result",
            }
        )

        recovered = (
            self.env["contact.center.inbox.event"]
            .sudo()
            ._cron_recover_orphaned_queue_jobs(
                limit_per_model=100,
                grace_seconds=300,
            )
        )

        waiter.invalidate_recordset(
            [
                "state",
                "attempts",
                "waiting_group_profile_id",
                "waiting_group_roster_after",
                "last_error_class",
            ]
        )
        self.assertEqual(recovered, {"inbox": 0, "outbox": 0, "media": 0})
        self.assertEqual(waiter.state, "dead")
        self.assertEqual(waiter.attempts, 4)
        self.assertFalse(waiter.waiting_group_profile_id)
        self.assertFalse(waiter.waiting_group_roster_after)
        self.assertEqual(
            waiter.last_error_class,
            "GroupRosterMetadataFailedError",
        )
        terminal_evidence = waiter.metadata_json["group_roster_terminal_failure"]
        self.assertEqual(terminal_evidence["profile_id"], profile.id)
        self.assertEqual(
            terminal_evidence["profile_error_class"],
            "DTOValidationError",
        )
        self.assertEqual(
            terminal_evidence["profile_sync_revision"],
            profile.sync_revision,
        )
        self.assertEqual(terminal_evidence["profile_attempts"], profile.attempts)
        self.assertTrue(terminal_evidence["recorded_at"])

        with mock.patch.object(type(profile), "_enqueue_sync", return_value=True):
            self.assertTrue(profile.with_user(self.admin).action_retry_metadata_sync())
        profile.invalidate_recordset(["metadata_state", "last_error_class"])
        waiter.invalidate_recordset(["metadata_json"])
        self.assertIn(profile.metadata_state, ("pending", "stale"))
        self.assertFalse(profile.last_error_class)
        self.assertEqual(
            waiter.metadata_json["group_roster_terminal_failure"],
            terminal_evidence,
        )

    def test_structurally_invalid_group_waiters_are_terminal_but_paused_is_not(self):
        scopes = {
            name: self._group_scope(name, isolated=True)
            for name in (
                "binding-archived",
                "channel-archived",
                "account-archived",
                "connection-archived",
                "binding-merged",
                "connection-rotated",
                "profile-removed",
                "provider-paused",
                "provider-disconnected",
            )
        }
        waiters = {
            name: self._group_waiter(scope[4], connection=scope[1])
            for name, scope in scopes.items()
        }
        scopes["binding-archived"][3].write({"active": False})
        scopes["channel-archived"][2].with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write({"active": False})
        scopes["account-archived"][0].write({"active": False})
        scopes["connection-archived"][1].write({"active": False})
        merge_scope = scopes["binding-merged"]
        target_channel = self.env["mail.channel"]._contact_center_create_channel(
            account=merge_scope[0],
            conversation_type="group",
            name="Queue Merge Target",
            teams=self.team,
            partner_ids=self.agent.partner_id.ids,
        )
        merge_target = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": target_channel.id,
                    "account_id": merge_scope[0].id,
                    "conversation_type": "group",
                    "conversation_ref": "120363-merge-target@g.us",
                }
            )
        )
        merge_scope[3].write({"merged_into_id": merge_target.id})
        rotation_scope = scopes["connection-rotated"]
        rotated_connection = self.env["contact.center.provider.connection"].create(
            {
                "name": "Queue Rotated Connection",
                "account_id": rotation_scope[0].id,
                "adapter_key": "test.fake",
                "external_ref": "queue-rotated-%s" % uuid.uuid4(),
                "provider_schema_version": "queue-fixture-v1",
                "state": "connected",
                "active": True,
                "role": "standby",
                "inbound_active": False,
                "outbound_active": False,
                "capabilities_json": {"send_message": False},
            }
        )
        rotation_scope[4].write({"provider_connection_id": rotated_connection.id})
        scopes["profile-removed"][4].unlink()
        scopes["provider-paused"][1].write({"state": "paused"})
        scopes["provider-disconnected"][1].write({"state": "disconnected"})

        finalized = (
            self.env["contact.center.inbox.event"]
            .sudo()
            ._finalize_invalid_group_roster_waiters(limit=100)
        )

        self.assertEqual(finalized, 7)
        for name, waiter in waiters.items():
            waiter.invalidate_recordset(
                [
                    "state",
                    "waiting_group_profile_id",
                    "waiting_group_roster_after",
                    "last_error_class",
                ]
            )
            if name in ("provider-paused", "provider-disconnected"):
                self.assertEqual(waiter.state, "pending")
                self.assertTrue(waiter.waiting_group_profile_id)
                continue
            self.assertEqual(waiter.state, "unsupported")
            self.assertFalse(waiter.waiting_group_profile_id)
            self.assertFalse(waiter.waiting_group_roster_after)
            self.assertEqual(waiter.last_error_class, "UnsupportedEventError")

    def test_cron_never_recovers_ambiguous_processing_states(self):
        inbox = self._inbox(state="processing")
        outbox = self._outbox(state="processing")
        media = self._media(state="downloading")
        pending_at_boundary = self._outbox(state="pending")
        retry_at_boundary = self._outbox(state="retry")
        pending_at_boundary.write(
            {
                "dispatch_job_uuid": str(uuid.uuid4()),
                "dispatch_started_at": fields.Datetime.now(),
            }
        )
        retry_at_boundary.write(
            {
                "dispatch_job_uuid": str(uuid.uuid4()),
                "dispatch_started_at": fields.Datetime.now(),
            }
        )

        recovered = (
            self.env["contact.center.inbox.event"]
            .sudo()
            ._cron_recover_orphaned_queue_jobs(limit_per_model=10, grace_seconds=0)
        )

        self.assertEqual(recovered, {"inbox": 0, "outbox": 0, "media": 0})
        self.assertFalse(inbox.queue_job_uuid)
        self.assertFalse(outbox.queue_job_uuid)
        self.assertFalse(media.queue_job_uuid)
        self.assertFalse(pending_at_boundary.queue_job_uuid)
        self.assertFalse(retry_at_boundary.queue_job_uuid)
        with self.assertRaises(ValidationError):
            pending_at_boundary.with_user(self.admin).action_requeue()
        with self.assertRaises(ValidationError):
            retry_at_boundary.with_user(self.admin).action_requeue()

    def test_cron_limits_each_lane_batch(self):
        inboxes = self._inbox() | self._inbox()
        self._age_for_recovery(*inboxes)

        recovered = (
            self.env["contact.center.inbox.event"]
            .sudo()
            ._cron_recover_orphaned_queue_jobs(limit_per_model=1, grace_seconds=0)
        )

        self.assertEqual(recovered["inbox"], 1)
        inboxes.invalidate_recordset(["queue_job_uuid"])
        self.assertEqual(len(inboxes.filtered("queue_job_uuid")), 1)

    def test_new_ledger_enqueue_establishes_canonical_lane_identity(self):
        inbox = self._inbox()
        outbox = self._outbox()

        inbox._enqueue()
        outbox._enqueue()

        self.assertTrue(inbox.queue_job_uuid)
        self.assertTrue(outbox.queue_job_uuid)
        self.assertEqual(len(self._active_jobs("inbox", inbox)), 1)
        self.assertEqual(len(self._active_jobs("outbox", outbox)), 1)

    def test_cron_and_manual_requeue_adopt_active_identity_without_duplicate(self):
        inbox = self._inbox()
        outbox = self._outbox()
        inbox._enqueue()
        outbox._enqueue()
        inbox.invalidate_recordset(["queue_job_uuid"])
        outbox.invalidate_recordset(["queue_job_uuid"])
        inbox_uuid = inbox.queue_job_uuid
        outbox_uuid = outbox.queue_job_uuid
        self.assertTrue(inbox_uuid)
        self.assertTrue(outbox_uuid)
        self.assertEqual(len(self._active_jobs("inbox", inbox)), 1)
        self.assertEqual(len(self._active_jobs("outbox", outbox)), 1)
        inbox.sudo().write({"queue_job_uuid": False})
        outbox.sudo().write({"queue_job_uuid": False})
        self._age_for_recovery(inbox, outbox)
        inbox.invalidate_recordset(["queue_job_uuid"])
        outbox.invalidate_recordset(["queue_job_uuid"])
        self.assertFalse(inbox.queue_job_uuid)
        self.assertFalse(outbox.queue_job_uuid)

        recovery = self.env["contact.center.inbox.event"].sudo()
        recovered = recovery._cron_recover_orphaned_queue_jobs(
            limit_per_model=10, grace_seconds=0
        )

        self.assertEqual(recovered["inbox"], 1)
        self.assertEqual(recovered["outbox"], 1)
        inbox.invalidate_recordset(["queue_job_uuid"])
        outbox.invalidate_recordset(["queue_job_uuid"])
        self.assertEqual(inbox.queue_job_uuid, inbox_uuid)
        self.assertEqual(outbox.queue_job_uuid, outbox_uuid)

        inbox.with_user(self.admin).action_requeue()
        outbox.with_user(self.admin).action_requeue()

        inbox.invalidate_recordset(["queue_job_uuid", "has_active_queue_job"])
        outbox.invalidate_recordset(["queue_job_uuid", "has_active_queue_job"])
        self.assertEqual(inbox.queue_job_uuid, inbox_uuid)
        self.assertEqual(outbox.queue_job_uuid, outbox_uuid)
        self.assertTrue(inbox.has_active_queue_job)
        self.assertTrue(outbox.has_active_queue_job)
        self.assertEqual(len(self._active_jobs("inbox", inbox)), 1)
        self.assertEqual(len(self._active_jobs("outbox", outbox)), 1)

    def test_active_lane_detection_repairs_missing_uuid_from_canonical_identity(self):
        inbox = self._inbox()
        outbox = self._outbox()
        media = self._media()
        inbox._enqueue()
        outbox._enqueue()
        media._enqueue_download()
        expected = {
            "inbox": inbox.queue_job_uuid,
            "outbox": outbox.queue_job_uuid,
            "media": media.queue_job_uuid,
        }
        inbox.sudo().write({"queue_job_uuid": False})
        outbox.sudo().write({"queue_job_uuid": False})
        media.sudo().write({"queue_job_uuid": False})

        self.assertTrue(inbox._has_active_queue_job())
        self.assertTrue(outbox._has_active_queue_job())
        self.assertTrue(media._has_active_queue_job())
        inbox.invalidate_recordset(["queue_job_uuid"])
        outbox.invalidate_recordset(["queue_job_uuid"])
        media.invalidate_recordset(["queue_job_uuid"])
        self.assertEqual(inbox.queue_job_uuid, expected["inbox"])
        self.assertEqual(outbox.queue_job_uuid, expected["outbox"])
        self.assertEqual(media.queue_job_uuid, expected["media"])

    def test_recovery_never_reuses_pointer_without_canonical_lane_identity(self):
        records = {
            "inbox": self._inbox(),
            "outbox": self._outbox(),
            "media": self._media(),
        }
        records["inbox"]._enqueue()
        records["outbox"]._enqueue()
        records["media"]._enqueue_download()
        unrelated_uuids = {}
        for lane, record in records.items():
            record.invalidate_recordset(["queue_job_uuid"])
            unrelated_uuids[lane] = record.queue_job_uuid
            job = (
                self.env["queue.job"]
                .sudo()
                .search([("uuid", "=", record.queue_job_uuid)], limit=1)
            )
            job.write({"identity_key": "unrelated:%s:%s" % (lane, record.id)})
        self._age_for_recovery(*records.values())

        recovered = (
            self.env["contact.center.inbox.event"]
            .sudo()
            ._cron_recover_orphaned_queue_jobs(
                limit_per_model=10,
                grace_seconds=0,
            )
        )

        self.assertEqual(recovered, {"inbox": 1, "outbox": 1, "media": 1})
        for lane, record in records.items():
            record.invalidate_recordset(["queue_job_uuid"])
            self.assertNotEqual(record.queue_job_uuid, unrelated_uuids[lane])
            self.assertEqual(len(self._active_jobs(lane, record)), 1)

    def test_cron_revives_failed_media_job_instead_of_duplicating_it(self):
        media = self._media()
        media._enqueue_download()
        failed_uuid = media.queue_job_uuid
        failed_job = (
            self.env["queue.job"].sudo().search([("uuid", "=", failed_uuid)], limit=1)
        )
        failed_job.write(
            {
                "state": "failed",
                "retry": 7,
                "eta": fields.Datetime.now(),
            }
        )
        self._age_for_recovery(media)

        recovered = (
            self.env["contact.center.inbox.event"]
            .sudo()
            ._cron_recover_orphaned_queue_jobs(limit_per_model=10, grace_seconds=0)
        )

        media.invalidate_recordset(["queue_job_uuid"])
        failed_job.invalidate_recordset(["state", "retry", "eta"])
        self.assertEqual(recovered["media"], 1)
        self.assertEqual(media.queue_job_uuid, failed_uuid)
        self.assertEqual(failed_job.state, "pending")
        self.assertEqual(failed_job.retry, 0)
        self.assertFalse(failed_job.eta)
        self.assertEqual(
            self.env["queue.job"]
            .sudo()
            .search_count(
                [
                    (
                        "identity_key",
                        "=",
                        "contact_center:media:%s" % media.id,
                    )
                ]
            ),
            1,
        )
