import datetime
import uuid
from types import SimpleNamespace
from unittest import mock

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..models import account as account_model
from ..services.adapter import ProviderAdapter, adapter_registry
from ..services.dto import (
    AdapterResult,
    AddressDTO,
    CommandDTO,
    ConversationDTO,
    EventDTO,
)


@adapter_registry.register("test.connection.health")
class ConnectionHealthAdapter(ProviderAdapter):
    display_name = "Test Connection Health"
    health_result = {"state": "connected", "detail": "healthy"}
    health_error = None
    health_calls = []
    execute_calls = []
    capability_calls = []

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return EventDTO.from_dict(envelope)

    def execute_command(self, connection, command):
        self.execute_calls.append(connection.id)
        return AdapterResult.success(provider_response={"accepted": True})

    def prepare_request_snapshot(self, connection, command):
        return {
            "provider": self.key,
            "method": "POST",
            "endpoint": "/health-test",
            "payload": {"command_id": command.command_id},
        }

    def get_capabilities(self, connection):
        self.capability_calls.append(connection.id)
        return {"send_message": True, "mark_read": True}

    def get_health(self, connection):
        self.health_calls.append(connection.id)
        if self.health_error:
            raise self.health_error
        return dict(self.health_result)


class TestConnectionHealth(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        supervisor_group = cls.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        admin_group = cls.env.ref("contact_center_base.group_contact_center_admin")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Connection Health Agent",
                    "login": "cc-health-agent-%s" % uuid.uuid4(),
                    "email": "cc-health-agent@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, agent_group.ids)],
                }
            )
        )
        cls.supervisor = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Connection Health Supervisor",
                    "login": "cc-health-supervisor-%s" % uuid.uuid4(),
                    "email": "cc-health-supervisor@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, supervisor_group.ids)],
                }
            )
        )
        cls.admin = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Connection Health Administrator",
                    "login": "cc-health-admin-%s" % uuid.uuid4(),
                    "email": "cc-health-admin@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, admin_group.ids)],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Connection Health Team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
                "supervisor_ids": [(6, 0, cls.supervisor.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Connection Health Account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "health-account-%s" % uuid.uuid4(),
                "default_team_id": cls.team.id,
                "mark_read_enabled": True,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Connection Health Primary",
                "account_id": cls.account.id,
                "adapter_key": "test.connection.health",
                "external_ref": "health-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "health-fixture-v1",
                "state": "disconnected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {"send_message": True, "mark_read": True},
            }
        )

    def setUp(self):
        super().setUp()
        ConnectionHealthAdapter.health_result = {
            "state": "connected",
            "detail": "healthy",
        }
        ConnectionHealthAdapter.health_error = None
        ConnectionHealthAdapter.health_calls = []
        ConnectionHealthAdapter.execute_calls = []
        ConnectionHealthAdapter.capability_calls = []

    def test_capability_refresh_authorizes_before_provider_io(self):
        with self.assertRaises(AccessError):
            self.connection.with_user(self.agent).refresh_capabilities()
        self.assertEqual(ConnectionHealthAdapter.capability_calls, [])

        self.connection.with_user(self.supervisor).refresh_capabilities()
        self.assertEqual(
            ConnectionHealthAdapter.capability_calls,
            [self.connection.id],
        )

    def _channel_binding(self):
        token = str(uuid.uuid4())
        guest = self.env["mail.guest"].sudo().create({"name": "Health Remote"})
        identity = (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": "Health Remote",
                    "company_id": self.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            identity=identity,
            team=self.team,
            partner_ids=(self.agent | self.supervisor).partner_id.ids,
            guest_ids=guest.ids,
        )
        binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": channel.id,
                    "account_id": self.account.id,
                    "identity_id": identity.id,
                    "conversation_type": "direct",
                    "conversation_ref": "health-conversation-%s" % token,
                }
            )
        )
        address = "%s@s.whatsapp.net" % uuid.uuid4().int
        self.env["contact.center.channel.alias"].sudo().create(
            {
                "channel_binding_id": binding.id,
                "account_id": self.account.id,
                "namespace": "whatsapp.pn",
                "value_raw": address,
                "value_normalized": address,
                "role": "primary",
            }
        )
        return binding

    def _outbox(self, binding, state="pending", enqueue=False, **extra_values):
        command_id = "health-command-%s" % uuid.uuid4()
        external_message_id = "health-message-%s" % uuid.uuid4()
        message = binding.channel_id._contact_center_post(
            origin="inbound",
            body="Health command target",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_guest_id=binding.identity_id.mail_guest_id.id,
            partner_ids=[],
        )
        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": binding.id,
                    "provider_connection_id": self.connection.id,
                    "direction": "inbound",
                    "origin": "provider",
                    "content_type": "text",
                    "external_message_id": external_message_id,
                    "delivery_state": "delivered",
                }
            )
        )
        alias = binding.alias_ids.ensure_one()
        address = AddressDTO(
            namespace=alias.namespace,
            value=alias.value_raw,
            value_normalized=alias.value_normalized,
            role=alias.role,
            source_field=alias.source_field or "health_fixture",
            confidence=alias.confidence,
        )
        command = CommandDTO(
            command_id=command_id,
            command_type="mark_read",
            account_ref=self.account.external_ref,
            connection_ref=self.connection.external_ref,
            conversation_ref=binding.conversation_ref,
            conversation=ConversationDTO(
                conversation_type=binding.conversation_type,
                addresses=(address,),
            ),
            target_address=address,
            options={"external_message_ids": [external_message_id]},
        )
        values = {
            "account_id": self.account.id,
            "provider_connection_id": self.connection.id,
            "channel_binding_id": binding.id,
            "target_message_binding_id": target.id,
            "outbox_idempotency_key": command_id,
            "command_type": "mark_read",
            "command_json": command.to_dict(),
            "state": state,
        }
        values.update(extra_values)
        return (
            self.env["contact.center.outbox.command"]
            .sudo()
            .with_context(contact_center_skip_enqueue=not enqueue)
            .create(values)
        )

    def _run_outbox_job(self, outbox, job_uuid=None):
        job_uuid = job_uuid or outbox.queue_job_uuid or str(uuid.uuid4())
        if not outbox.queue_job_uuid:
            outbox.sudo().write({"queue_job_uuid": job_uuid})

        def flush_without_commit(record):
            record.env.flush_all()
            return True

        with mock.patch.object(
            type(outbox),
            "_commit_job_transaction",
            autospec=True,
            side_effect=flush_without_commit,
        ):
            return outbox.with_context(job_uuid=job_uuid)._job_process()

    def test_cron_only_enqueues_one_unique_jittered_health_job(self):
        connection_model = self.env["contact.center.provider.connection"]
        now = fields.Datetime.now()
        with trap_jobs() as trap:
            self.assertEqual(connection_model._cron_schedule_health_checks(), 1)
            trap.assert_jobs_count(1)
            trap.assert_enqueued_job(
                self.connection.sudo()
                .with_company(self.connection.company_id)
                ._job_check_health,
                properties={
                    "identity_key": "contact_center:health:%s" % self.connection.id,
                    "max_retries": 5,
                    "priority": 30,
                    "description": "Contact Center connection health %s"
                    % self.connection.id,
                },
            )
            queued_job = trap.enqueued_jobs[0]
            self.assertEqual(queued_job.channel, "root.contact_center.health")
            self.assertGreaterEqual(queued_job.eta, now)
            self.assertLessEqual(
                queued_job.eta,
                now + datetime.timedelta(seconds=30),
            )
            self.assertEqual(connection_model._cron_schedule_health_checks(), 0)
            trap.assert_jobs_count(1)

        self.connection.invalidate_recordset(
            ["health_check_pending", "health_job_uuid", "next_health_check_at"]
        )
        self.assertTrue(self.connection.health_check_pending)
        self.assertEqual(self.connection.health_job_uuid, queued_job.uuid)
        self.assertTrue(self.connection.next_health_check_at)
        self.assertGreaterEqual(
            self.connection.next_health_check_at,
            queued_job.eta + datetime.timedelta(seconds=59),
        )
        self.assertEqual(ConnectionHealthAdapter.health_calls, [])

    def test_manual_refresh_is_supervisor_only_and_never_calls_provider_inline(self):
        other_agent = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Other Health Agent",
                    "login": "cc-other-health-%s" % uuid.uuid4(),
                    "email": "cc-other-health@example.invalid",
                    "company_id": self.env.company.id,
                    "company_ids": [(6, 0, self.env.company.ids)],
                    "groups_id": [
                        (
                            6,
                            0,
                            self.env.ref(
                                "contact_center_base.group_contact_center_agent"
                            ).ids,
                        )
                    ],
                }
            )
        )
        other_team = self.env["contact.center.team"].create(
            {
                "name": "Other Health Team",
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, other_agent.ids)],
            }
        )
        other_account = self.env["contact.center.account"].create(
            {
                "name": "Other Health Account",
                "company_id": self.env.company.id,
                "platform": "whatsapp",
                "external_ref": "other-health-account-%s" % uuid.uuid4(),
                "default_team_id": other_team.id,
            }
        )
        other_connection = self.env["contact.center.provider.connection"].create(
            {
                "name": "Other Health Connection",
                "account_id": other_account.id,
                "adapter_key": "test.connection.health",
                "external_ref": "other-health-connection-%s" % uuid.uuid4(),
                "state": "disconnected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": False,
            }
        )

        agent_api = self.env["contact.center.ui.api"].with_user(self.agent)
        bootstrap = agent_api.bootstrap()
        self.assertEqual(bootstrap["connection_health"]["summary"]["total"], 1)
        self.assertFalse(bootstrap["capabilities"]["check_connection_health"])
        self.assertFalse(bootstrap["connection_health"]["summary"]["can_check"])
        with self.assertRaises(AccessError):
            agent_api.check_connection_health()

        supervisor_api = self.env["contact.center.ui.api"].with_user(self.supervisor)
        with trap_jobs() as trap:
            snapshot = supervisor_api.check_connection_health()
            trap.assert_jobs_count(1)
            self.assertEqual(trap.enqueued_jobs[0].priority, 30)
        self.assertEqual(snapshot["summary"]["total"], 1)
        self.assertEqual(snapshot["summary"]["checking"], 1)
        self.assertEqual(snapshot["summary"]["unknown"], 1)
        self.assertTrue(snapshot["summary"]["can_check"])
        self.assertEqual(snapshot["items"][0]["id"], self.connection.id)
        self.assertTrue(snapshot["items"][0]["can_check"])
        self.assertEqual(
            set(snapshot["items"][0]),
            {
                "id",
                "account_id",
                "account_name",
                "connection_name",
                "platform",
                "provider",
                "display_address",
                "state",
                "checking",
                "last_check_at",
                "state_changed_at",
                "detail",
                "can_check",
                "connection_role",
                "accepts_inbound",
                "unsupported_count",
            },
        )
        self.assertEqual(ConnectionHealthAdapter.health_calls, [])
        with self.assertRaises(ValidationError):
            supervisor_api.check_connection_health(other_connection.id)

    def test_form_health_refresh_remains_low_priority(self):
        with trap_jobs() as trap:
            action = self.connection.with_user(self.supervisor).action_check_health()
            trap.assert_jobs_count(1)
            self.assertEqual(trap.enqueued_jobs[0].priority, 30)
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(ConnectionHealthAdapter.health_calls, [])

    def test_runtime_contract_is_admin_only_and_secret_free(self):
        with self.assertRaises(AccessError):
            self.env["contact.center.provider.connection"].with_user(
                self.agent
            ).get_health_runtime_contract()
        contract = (
            self.env["contact.center.provider.connection"]
            .with_user(self.admin)
            .get_health_runtime_contract()
        )
        self.assertTrue(contract["cron"]["active"])
        self.assertEqual(contract["cron"]["interval_number"], 1)
        self.assertEqual(contract["cron"]["interval_type"], "minutes")
        self.assertEqual(contract["job_function"]["method"], "_job_check_health")
        self.assertEqual(
            contract["job_function"]["channel"], "root.contact_center.health"
        )
        self.assertEqual(contract["priority_floor"], 30)
        self.assertEqual(contract["jitter_window_seconds"], 30)
        self.assertNotIn("token", repr(contract).lower())

    def test_display_address_uses_only_bounded_own_account_identity(self):
        item = self.connection._contact_center_health_item()
        self.assertFalse(item["display_address"])

        remote_identifier = "remote-customer-5588999999999"
        self.account.own_external_identity = "551199887766:42@s.whatsapp.net"
        item = self.connection._contact_center_health_item()
        self.assertEqual(item["display_address"], "+551199887766")
        self.assertNotIn(remote_identifier, repr(item))

        self.account.own_external_identity = (
            "Fleet <script>alert('x')</script> " + "x" * 120
        )
        item = self.connection._contact_center_health_item()
        self.assertLessEqual(len(item["display_address"]), 80)
        for unsafe_character in "<>&\"'\n\r":
            self.assertNotIn(unsafe_character, item["display_address"])

        telegram_account = self.env["contact.center.account"].create(
            {
                "name": "Numeric Telegram Account",
                "company_id": self.env.company.id,
                "platform": "telegram",
                "external_ref": "telegram-health-account-%s" % uuid.uuid4(),
                "own_external_identity": "123456789",
            }
        )
        self.assertEqual(
            telegram_account._contact_center_display_address(),
            "123456789",
        )

    def test_unknown_state_always_has_unknown_detail(self):
        self.connection.write(
            {
                "state": "connected",
                "health_detail": "healthy",
                "last_state_observed_at": "2026-08-23 10:00:00",
            }
        )
        item = self.connection._contact_center_health_item(now="2026-08-23 10:10:00")
        self.assertEqual(item["state"], "unknown")
        self.assertEqual(item["detail"], "unknown")

    def test_health_ui_and_dispatch_share_the_exact_freshness_boundary(self):
        now = fields.Datetime.now()
        cutoff = now - datetime.timedelta(
            seconds=self.connection._health_stale_after_seconds
        )
        self.connection.write(
            {
                "state": "connected",
                "health_detail": "healthy",
                "last_state_observed_at": cutoff,
            }
        )
        self.assertEqual(
            self.connection._contact_center_health_status(now=now), "connected"
        )
        self.assertTrue(self.connection._contact_center_outbound_is_available(now=now))

        self.connection.last_state_observed_at = cutoff - datetime.timedelta(seconds=1)
        self.assertEqual(
            self.connection._contact_center_health_status(now=now), "unknown"
        )
        self.assertFalse(self.connection._contact_center_outbound_is_available(now=now))

    def test_fresh_health_recovers_outbox_when_persisted_state_was_stale_connected(
        self,
    ):
        binding = self._channel_binding()
        command = self._outbox(binding)
        now = fields.Datetime.now()
        self.connection.write(
            {
                "state": "connected",
                "health_detail": "healthy",
                "last_state_observed_at": now
                - datetime.timedelta(
                    seconds=self.connection._health_stale_after_seconds + 1
                ),
            }
        )

        with self.assertRaises(RetryableJobError):
            self._run_outbox_job(command)
        command.invalidate_recordset(
            ["state", "attempts", "provider_request_json", "dispatch_started_at"]
        )
        self.assertEqual(command.state, "pending")
        self.assertEqual(command.attempts, 0)
        self.assertFalse(command.provider_request_json)
        self.assertFalse(command.dispatch_started_at)
        self.assertEqual(ConnectionHealthAdapter.execute_calls, [])

        with trap_jobs() as trap:
            self.connection._apply_health_result(
                {"state": "connected", "detail": "healthy"},
                source="health_job",
                observed_at=fields.Datetime.now(),
            )
            trap.assert_jobs_count(1)
            recovery_job = trap.enqueued_jobs[0]

        with trap_jobs() as recovery_trap:
            self.assertEqual(recovery_job.perform(), 1)
            recovery_trap.assert_jobs_count(1)

        command.invalidate_recordset(["queue_job_uuid"])
        self.connection.invalidate_recordset(["last_recovered_command_count"])
        self.assertTrue(command.queue_job_uuid)
        self.assertEqual(self.connection.last_recovered_command_count, 1)

    def test_identity_mismatch_is_a_safe_degraded_detail(self):
        normalized = self.connection._normalize_health_result(
            {
                "state": "degraded",
                "detail": "identity_mismatch",
                "identity_matches": "true",
            }
        )
        self.assertEqual(
            normalized,
            {"state": "degraded", "detail": "identity_mismatch"},
        )
        matched = self.connection._normalize_health_result(
            {
                "state": "connected",
                "detail": "healthy",
                "identity_matches": True,
            }
        )
        self.assertIs(matched["identity_matches"], True)
        metadata_limited = self.connection._normalize_health_result(
            {
                "state": "connected",
                "detail": "metadata_limited",
                "identity_matches": True,
            }
        )
        self.assertEqual(metadata_limited["state"], "connected")
        self.assertEqual(metadata_limited["detail"], "metadata_limited")
        self.assertIs(metadata_limited["identity_matches"], True)

    def test_identity_safety_latch_ignores_lifecycle_until_verified_health(self):
        connection_type = type(self.connection)
        observed_at = fields.Datetime.now()
        with mock.patch.object(
            connection_type,
            "_enqueue_outbox_recovery",
            autospec=True,
        ) as enqueue_recovery:
            self.connection._apply_health_result(
                {
                    "state": "connected",
                    "detail": "healthy",
                    "identity_matches": False,
                },
                source="health_job",
                observed_at=observed_at,
            )
            self.connection._apply_provider_state_event(
                "disconnected",
                observed_at=observed_at + datetime.timedelta(seconds=1),
            )
            self.connection._apply_provider_state_event(
                "connected",
                observed_at=observed_at + datetime.timedelta(seconds=2),
            )
            self.connection._apply_health_result(
                {"state": "connected", "detail": "healthy"},
                source="health_job",
                observed_at=observed_at + datetime.timedelta(seconds=3),
            )

            self.connection.invalidate_recordset(
                ["state", "health_detail", "identity_mismatch_latched"]
            )
            self.assertEqual(self.connection.state, "degraded")
            self.assertEqual(self.connection.health_detail, "identity_unverified")
            self.assertTrue(self.connection.identity_mismatch_latched)
            self.assertEqual(
                self.connection._contact_center_health_item(
                    now=observed_at + datetime.timedelta(seconds=3)
                )["state"],
                "degraded",
            )
            enqueue_recovery.assert_not_called()

            self.connection._apply_health_result(
                {
                    "state": "connected",
                    "detail": "healthy",
                    "identity_matches": True,
                },
                source="health_job",
                observed_at=observed_at + datetime.timedelta(seconds=4),
            )

        self.connection.invalidate_recordset(
            ["state", "health_detail", "identity_mismatch_latched"]
        )
        self.assertEqual(self.connection.state, "connected")
        self.assertEqual(self.connection.health_detail, "healthy")
        self.assertFalse(self.connection.identity_mismatch_latched)
        enqueue_recovery.assert_not_called()

    def test_identity_mismatch_latch_is_a_final_dispatch_guard(self):
        binding = self._channel_binding()
        command = self._outbox(binding)
        self.connection.write({"state": "connected", "identity_mismatch_latched": True})

        with self.assertRaises(RetryableJobError):
            self._run_outbox_job(command)

        command.invalidate_recordset(
            ["state", "attempts", "provider_request_json", "dispatch_started_at"]
        )
        self.assertEqual(command.state, "pending")
        self.assertEqual(command.attempts, 0)
        self.assertFalse(command.provider_request_json)
        self.assertFalse(command.dispatch_started_at)
        self.assertEqual(ConnectionHealthAdapter.execute_calls, [])

    def test_archived_account_is_a_final_dispatch_guard(self):
        binding = self._channel_binding()
        command = self._outbox(binding)
        self.connection.write(
            {
                "state": "connected",
                "health_detail": "healthy",
                "last_state_observed_at": fields.Datetime.now(),
            }
        )
        self.account.active = False

        self.assertFalse(self._run_outbox_job(command))

        command.invalidate_recordset(
            ["state", "attempts", "provider_request_json", "dispatch_started_at"]
        )
        self.assertEqual(command.state, "cancelled")
        self.assertEqual(command.attempts, 0)
        self.assertFalse(command.provider_request_json)
        self.assertFalse(command.dispatch_started_at)
        self.assertEqual(ConnectionHealthAdapter.execute_calls, [])

    def test_stale_identity_latch_remains_visible_until_positive_verification(self):
        self.connection.write(
            {
                "state": "connected",
                "health_detail": "identity_mismatch",
                "identity_mismatch_latched": True,
                "last_state_observed_at": "2026-08-23 10:00:00",
            }
        )
        item = self.connection._contact_center_health_item(now="2026-08-23 12:00:00")
        self.assertEqual(item["state"], "degraded")
        self.assertEqual(item["detail"], "identity_mismatch")

    def test_latched_identity_still_exposes_fresh_authentication_required(self):
        observed_at = fields.Datetime.now()
        self.connection.write(
            {
                "state": "degraded",
                "health_detail": "identity_mismatch",
                "identity_mismatch_latched": True,
                "last_state_observed_at": observed_at,
            }
        )
        self.connection._apply_health_result(
            {"state": "authentication_required", "detail": "logged_out"},
            source="health_job",
            observed_at=observed_at + datetime.timedelta(seconds=1),
        )

        item = self.connection._contact_center_health_item(
            now=observed_at + datetime.timedelta(seconds=1)
        )
        self.assertEqual(item["state"], "authentication_required")
        self.assertEqual(item["detail"], "authentication_required")
        self.assertTrue(self.connection.identity_mismatch_latched)
        self.assertFalse(
            self.connection._contact_center_outbound_is_available(
                now=observed_at + datetime.timedelta(seconds=1)
            )
        )

    def test_health_from_an_old_configuration_revision_cannot_reopen_outbound(self):
        job_uuid = str(uuid.uuid4())
        observed_at = fields.Datetime.now()
        self.connection.write(
            {
                "state": "degraded",
                "health_detail": "identity_unverified",
                "identity_mismatch_latched": True,
                "health_configuration_revision": 2,
                "health_check_pending": True,
                "health_job_uuid": job_uuid,
            }
        )

        applied = self.connection._apply_normalized_health(
            {
                "state": "connected",
                "detail": "healthy",
                "identity_matches": True,
            },
            source="health_job",
            observed_at=observed_at,
            expected_job_uuid=job_uuid,
            expected_configuration_revision=1,
            completes_check=True,
        )

        self.assertFalse(applied)
        self.connection.invalidate_recordset(
            [
                "state",
                "identity_mismatch_latched",
                "health_check_pending",
                "health_job_uuid",
                "next_health_check_at",
            ]
        )
        self.assertEqual(self.connection.state, "degraded")
        self.assertTrue(self.connection.identity_mismatch_latched)
        self.assertFalse(self.connection.health_check_pending)
        self.assertFalse(self.connection.health_job_uuid)
        self.assertEqual(self.connection.next_health_check_at, observed_at)

    def test_rate_limit_defers_next_probe_with_a_bounded_retry_after(self):
        normalized = self.connection._normalize_health_result(
            {
                "state": "paused",
                "reason": "rate_limited",
                "retry_after_seconds": "7200",
            }
        )
        self.assertEqual(normalized["retry_after_seconds"], 3600)
        observed_at = fields.Datetime.now()
        self.connection._apply_normalized_health(
            normalized,
            source="health_job",
            observed_at=observed_at,
            completes_check=True,
        )
        self.connection.invalidate_recordset(
            ["next_health_check_at", "health_retry_not_before"]
        )
        self.assertEqual(
            self.connection.next_health_check_at,
            observed_at + datetime.timedelta(hours=1),
        )
        self.assertEqual(
            self.connection.health_retry_not_before,
            observed_at + datetime.timedelta(hours=1),
        )
        with trap_jobs() as trap:
            self.assertFalse(self.connection._enqueue_health_check())
            trap.assert_jobs_count(0)

    def test_snapshot_scales_to_twenty_connections_and_summarizes_locally(self):
        observed_at = fields.Datetime.now()
        states = (
            ["connected"] * 5
            + ["degraded"] * 5
            + ["disconnected"] * 5
            + ["authentication_required"] * 5
        )
        connections = self.connection
        for index in range(1, 20):
            connections |= self.env["contact.center.provider.connection"].create(
                {
                    "name": "Health Number %02d" % (index + 1),
                    "account_id": self.account.id,
                    "adapter_key": "test.connection.health",
                    "external_ref": "health-number-%s" % uuid.uuid4(),
                    "state": states[index],
                    "active": True,
                    "role": "standby",
                    "inbound_active": False,
                    "outbound_active": False,
                    "health_detail": (
                        "healthy" if states[index] == "connected" else "unavailable"
                    ),
                    "last_state_observed_at": observed_at,
                }
            )
        for connection, state in zip(connections.sorted("id"), states):
            connection.write(
                {
                    "state": state,
                    "health_detail": (
                        "healthy" if state == "connected" else "unavailable"
                    ),
                    "last_state_observed_at": observed_at,
                }
            )
        first = connections.sorted("id")[0]
        first.write(
            {
                "health_check_pending": True,
                "next_health_check_at": observed_at + datetime.timedelta(minutes=1),
            }
        )

        snapshot = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._connection_health_snapshot()
        )
        self.assertEqual(snapshot["summary"]["total"], 20)
        self.assertEqual(snapshot["summary"]["checking"], 1)
        self.assertEqual(snapshot["summary"]["connected"], 5)
        self.assertEqual(snapshot["summary"]["degraded"], 5)
        self.assertEqual(snapshot["summary"]["disconnected"], 5)
        self.assertEqual(snapshot["summary"]["authentication_required"], 5)
        self.assertEqual(snapshot["summary"]["unknown"], 0)
        self.assertEqual(
            sum(
                snapshot["summary"][state]
                for state in (
                    "connected",
                    "degraded",
                    "disconnected",
                    "authentication_required",
                    "unknown",
                )
            ),
            snapshot["summary"]["total"],
        )
        # ``checking`` is transverse and therefore intentionally not part of
        # the mutually-exclusive state sum above.
        self.assertEqual(len(snapshot["items"]), 20)

    def test_reconnect_resumes_only_pre_boundary_pending_and_retry(self):
        binding = self._channel_binding()
        pending = self._outbox(binding, "pending")
        retry = self._outbox(binding, "retry", attempts=3)
        processing = self._outbox(
            binding,
            "processing",
            dispatch_job_uuid=str(uuid.uuid4()),
            dispatch_started_at=fields.Datetime.now(),
        )
        uncertain = self._outbox(binding, "uncertain")
        dead = self._outbox(binding, "dead")
        done = self._outbox(binding, "done")
        cancelled = self._outbox(binding, "cancelled")

        with trap_jobs() as trap:
            self.connection._apply_provider_state_event(
                "connected", observed_at=fields.Datetime.now()
            )
            trap.assert_jobs_count(1)
            recovery_job = trap.enqueued_jobs[0]

        with trap_jobs() as recovery_trap:
            self.assertEqual(recovery_job.perform(), 2)
            recovery_trap.assert_jobs_count(2)

        for resumed in pending | retry:
            resumed.invalidate_recordset(["queue_job_uuid", "state", "attempts"])
            self.assertTrue(resumed.queue_job_uuid)
            self.assertIn(resumed.state, ("pending", "retry"))
        self.assertEqual(retry.attempts, 3)
        for protected in processing | uncertain | dead | done | cancelled:
            protected.invalidate_recordset(["queue_job_uuid", "state"])
            self.assertFalse(protected.queue_job_uuid)
        self.connection.invalidate_recordset(
            [
                "state",
                "last_recovered_command_count",
                "total_recovered_command_count",
            ]
        )
        self.assertEqual(self.connection.state, "connected")
        self.assertEqual(self.connection.last_recovered_command_count, 2)
        self.assertEqual(self.connection.total_recovered_command_count, 2)

    def test_reconnect_advances_existing_pending_job_without_duplicate(self):
        binding = self._channel_binding()
        command = self._outbox(binding, enqueue=True)
        job_model = self.env["queue.job"].sudo()
        job = job_model.search([("uuid", "=", command.queue_job_uuid)], limit=1)
        self.assertTrue(job)
        job.write(
            {
                "eta": fields.Datetime.now() + datetime.timedelta(hours=1),
                "retry": 4,
            }
        )
        with trap_jobs() as trap:
            self.connection._apply_provider_state_event(
                "connected", observed_at=fields.Datetime.now()
            )
            trap.assert_jobs_count(1)
            recovery_job = trap.enqueued_jobs[0]
        with trap_jobs() as recovery_trap:
            self.assertEqual(recovery_job.perform(), 1)
            recovery_trap.assert_jobs_count(0)

        job.invalidate_recordset(["state", "eta", "retry"])
        command.invalidate_recordset(["queue_job_uuid"])
        self.assertEqual(job_model.search_count([("uuid", "=", job.uuid)]), 1)
        self.assertEqual(job.state, "pending")
        self.assertFalse(job.eta)
        self.assertEqual(job.retry, 0)
        self.assertEqual(command.queue_job_uuid, job.uuid)
        self.assertEqual(self.connection.last_recovered_command_count, 1)

    def test_reconnect_recovery_is_bounded_and_chains_a_fixed_snapshot(self):
        binding = self._channel_binding()
        commands = self.env["contact.center.outbox.command"]
        for _index in range(5):
            commands |= self._outbox(binding)

        with mock.patch.object(account_model, "_OUTBOX_RECOVERY_BATCH_SIZE", 2):
            with trap_jobs() as initial_trap:
                self.connection._apply_provider_state_event(
                    "connected", observed_at=fields.Datetime.now()
                )
                initial_trap.assert_jobs_count(1)
                recovery_job = initial_trap.enqueued_jobs[0]
            late_command = self._outbox(binding)
            self.assertEqual(
                recovery_job.channel,
                "root.contact_center.outbox",
            )
            self.assertEqual(recovery_job.priority, 20)

            recovered_ids = []
            for expected_batch_size in (2, 2, 1):
                with trap_jobs() as batch_trap:
                    self.assertEqual(recovery_job.perform(), expected_batch_size)
                command_jobs = [
                    job
                    for job in batch_trap.enqueued_jobs
                    if job.identity_key.startswith("contact_center:outbox:")
                ]
                continuation_jobs = [
                    job
                    for job in batch_trap.enqueued_jobs
                    if job.identity_key.startswith("contact_center:outbox_recovery:")
                ]
                self.assertEqual(len(command_jobs), expected_batch_size)
                recovered_ids.extend(job.recordset.id for job in command_jobs)
                if expected_batch_size == 1:
                    self.assertFalse(continuation_jobs)
                else:
                    self.assertEqual(len(continuation_jobs), 1)
                    recovery_job = continuation_jobs[0]

        self.assertEqual(recovered_ids, commands.sorted("id").ids)
        self.connection.invalidate_recordset(
            ["last_recovered_command_count", "total_recovered_command_count"]
        )
        self.assertEqual(self.connection.last_recovered_command_count, 5)
        self.assertEqual(self.connection.total_recovered_command_count, 5)
        late_command.invalidate_recordset(["queue_job_uuid"])
        self.assertFalse(late_command.queue_job_uuid)

    def test_old_recovery_continuation_cannot_cross_a_new_generation(self):
        binding = self._channel_binding()
        command = self._outbox(binding)
        first_observed_at = fields.Datetime.now()
        with trap_jobs() as first_trap:
            self.connection._apply_provider_state_event(
                "connected", observed_at=first_observed_at
            )
            first_recovery = first_trap.enqueued_jobs[0]

        self.connection._apply_provider_state_event(
            "disconnected",
            observed_at=first_observed_at + datetime.timedelta(seconds=1),
        )
        with trap_jobs() as second_trap:
            self.connection._apply_provider_state_event(
                "connected",
                observed_at=first_observed_at + datetime.timedelta(seconds=2),
            )
            second_recovery = second_trap.enqueued_jobs[0]

        with trap_jobs() as stale_trap:
            self.assertFalse(first_recovery.perform())
            stale_trap.assert_jobs_count(0)
        command.invalidate_recordset(["queue_job_uuid"])
        self.assertFalse(command.queue_job_uuid)

        with trap_jobs() as current_trap:
            self.assertEqual(second_recovery.perform(), 1)
            current_trap.assert_jobs_count(1)
        command.invalidate_recordset(["queue_job_uuid"])
        self.assertTrue(command.queue_job_uuid)

    def test_connected_standby_neither_recovers_nor_dispatches(self):
        binding = self._channel_binding()
        command = self._outbox(binding)
        self.connection.outbound_active = False
        with trap_jobs() as trap:
            self.connection._apply_provider_state_event(
                "connected", observed_at=fields.Datetime.now()
            )
            trap.assert_jobs_count(0)
        command.invalidate_recordset(["queue_job_uuid", "attempts", "state"])
        self.assertFalse(command.queue_job_uuid)
        self.assertEqual(self.connection.last_recovered_command_count, 0)

        self.assertFalse(self._run_outbox_job(command))
        command.invalidate_recordset(["state", "attempts", "provider_request_json"])
        self.assertEqual(command.state, "cancelled")
        self.assertEqual(command.attempts, 0)
        self.assertFalse(command.provider_request_json)
        self.assertEqual(ConnectionHealthAdapter.execute_calls, [])

    def test_connected_hint_requires_health_confirmation_before_recovery(self):
        binding = self._channel_binding()
        command = self._outbox(binding)
        received_at = fields.Datetime.now()
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "health-fixture-v1",
                "event_id": "health-confirmation-%s" % uuid.uuid4(),
                "event_type": "connection.updated",
                "occurred_at": received_at.isoformat() + "Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": self.connection.external_ref,
                "platform": self.account.platform,
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {"addresses": []},
                "conversation": {
                    "conversation_type": "other",
                    "addresses": [],
                },
                "extensions": {
                    "state": "connected",
                    "health_confirmation_required": True,
                },
            }
        )

        with trap_jobs() as trap:
            self.env["contact.center.application"]._process_event(
                self.connection,
                event,
                inbox_event=SimpleNamespace(id=301, create_date=received_at),
            )
            trap.assert_jobs_count(1)
            self.assertEqual(trap.enqueued_jobs[0].priority, 30)

        self.connection.invalidate_recordset(
            ["state", "health_detail", "health_check_pending"]
        )
        command.invalidate_recordset(["queue_job_uuid"])
        self.assertEqual(self.connection.state, "degraded")
        self.assertEqual(self.connection.health_detail, "identity_unverified")
        self.assertTrue(self.connection.health_check_pending)
        self.assertFalse(command.queue_job_uuid)

    def test_health_job_updates_metrics_and_emits_checking_then_result(self):
        application_type = type(self.env["contact.center.application"])
        notifications = []

        def capture(_application, connection, invalidate=False):
            self.assertFalse(invalidate)
            notifications.append(connection._contact_center_health_item())
            return True

        with mock.patch.object(
            application_type,
            "_notify_connection_health",
            autospec=True,
            side_effect=capture,
        ), trap_jobs() as trap:
            self.connection._enqueue_health_check()
            job_uuid = trap.enqueued_jobs[0].uuid
            self.assertTrue(
                self.connection.with_context(job_uuid=job_uuid)._job_check_health()
            )

        self.connection.invalidate_recordset(
            [
                "state",
                "health_detail",
                "health_check_pending",
                "health_job_uuid",
                "last_health_at",
                "last_connected_at",
                "last_health_latency_ms",
            ]
        )
        self.assertEqual(ConnectionHealthAdapter.health_calls, [self.connection.id])
        self.assertEqual(self.connection.state, "connected")
        self.assertEqual(self.connection.health_detail, "healthy")
        self.assertFalse(self.connection.health_check_pending)
        self.assertFalse(self.connection.health_job_uuid)
        self.assertTrue(self.connection.last_health_at)
        self.assertTrue(self.connection.last_connected_at)
        self.assertGreaterEqual(self.connection.last_health_latency_ms, 0)
        self.assertEqual(
            [(item["state"], item["checking"]) for item in notifications],
            [("unknown", True), ("connected", False)],
        )

    def test_health_snapshot_cannot_revert_lifecycle_received_during_probe(self):
        probe_started_at = datetime.datetime(2026, 8, 30, 12, 0, 0)
        lifecycle_received_at = probe_started_at + datetime.timedelta(seconds=1)

        def interleaved_health(_adapter, connection):
            connection._apply_provider_state_event(
                "disconnected",
                observed_at=lifecycle_received_at,
                detail="session_disconnected",
                observation_sequence=701,
            )
            return {"state": "connected", "detail": "healthy"}

        with trap_jobs() as trap:
            self.connection._enqueue_health_check()
            job_uuid = trap.enqueued_jobs[0].uuid

        with mock.patch.object(
            ConnectionHealthAdapter,
            "get_health",
            autospec=True,
            side_effect=interleaved_health,
        ), mock.patch(
            "odoo.addons.contact_center_base.models.account.fields.Datetime.now",
            return_value=probe_started_at,
        ):
            self.assertTrue(
                self.connection.with_context(job_uuid=job_uuid)._job_check_health()
            )

        self.connection.invalidate_recordset(
            [
                "state",
                "health_detail",
                "last_health_at",
                "last_state_observed_at",
                "last_state_source",
            ]
        )
        self.assertEqual(self.connection.state, "disconnected")
        self.assertEqual(self.connection.health_detail, "unavailable")
        self.assertEqual(self.connection.last_health_at, probe_started_at)
        self.assertEqual(
            self.connection.last_state_observed_at,
            lifecycle_received_at,
        )
        self.assertEqual(self.connection.last_state_source, "provider_event")

    def test_health_failure_is_sanitized_and_disconnected_dispatch_stays_pending(self):
        secret = "TOP-SECRET-health-provider-token"
        ConnectionHealthAdapter.health_error = RuntimeError(secret)
        with trap_jobs() as trap:
            self.connection._enqueue_health_check()
            job_uuid = trap.enqueued_jobs[0].uuid
            self.assertTrue(
                self.connection.with_context(job_uuid=job_uuid)._job_check_health()
            )
        self.connection.invalidate_recordset(
            [
                "state",
                "health_detail",
                "last_health_error_class",
                "consecutive_unhealthy_checks",
            ]
        )
        self.assertEqual(self.connection.state, "degraded")
        self.assertEqual(self.connection.health_detail, "internal_error")
        self.assertEqual(self.connection.last_health_error_class, "RuntimeError")
        self.assertEqual(self.connection.consecutive_unhealthy_checks, 1)
        item = self.connection._contact_center_health_item()
        self.assertNotIn(secret, repr(item))
        self.assertNotIn("error", item)

        binding = self._channel_binding()
        command = self._outbox(binding)
        with self.assertRaises(RetryableJobError):
            self._run_outbox_job(command)
        command.invalidate_recordset(
            ["state", "attempts", "provider_request_json", "dispatch_started_at"]
        )
        self.assertEqual(command.state, "pending")
        self.assertEqual(command.attempts, 0)
        self.assertFalse(command.provider_request_json)
        self.assertFalse(command.dispatch_started_at)
        self.assertEqual(ConnectionHealthAdapter.execute_calls, [])

    def test_provider_state_events_are_monotonic(self):
        self.connection._apply_provider_state_event(
            "disconnected", observed_at="2026-08-23T12:02:00+00:00"
        )
        self.connection._apply_provider_state_event(
            "connected", observed_at="2026-08-23T12:01:00+00:00"
        )
        self.connection.invalidate_recordset(["state", "last_state_observed_at"])
        self.assertEqual(self.connection.state, "disconnected")
        self.assertEqual(
            self.connection.last_state_observed_at,
            fields.Datetime.to_datetime("2026-08-23 12:02:00"),
        )
        self.connection._apply_provider_state_event(
            "connected", observed_at="2026-08-23T12:03:00+00:00"
        )
        self.connection.invalidate_recordset(["state", "last_state_observed_at"])
        self.assertEqual(self.connection.state, "connected")
        self.assertEqual(
            self.connection.last_state_observed_at,
            fields.Datetime.to_datetime("2026-08-23 12:03:00"),
        )

    def test_old_inbox_receipt_time_cannot_override_newer_health(self):
        self.connection._apply_health_result(
            {"state": "connected", "detail": "healthy"},
            source="health_job",
            observed_at="2026-08-23T12:05:00+00:00",
        )
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "health-fixture-v1",
                "event_id": "health-event-%s" % uuid.uuid4(),
                "event_type": "connection.updated",
                # Deliberately newer than the health result: durable inbox receipt,
                # not this provider timestamp, controls ordering.
                "occurred_at": "2026-08-23T12:06:00Z",
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": "connection-health",
                "platform": self.account.platform,
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {"addresses": []},
                "conversation": {
                    "conversation_type": "other",
                    "addresses": [],
                },
                "extensions": {
                    "state": "disconnected",
                    "detail": "unavailable",
                },
            }
        )
        old_inbox = SimpleNamespace(
            id=10, create_date=fields.Datetime.to_datetime("2026-08-23 12:04:00")
        )
        self.env["contact.center.application"]._process_event(
            self.connection,
            event,
            inbox_event=old_inbox,
        )
        self.connection.invalidate_recordset(["state", "last_state_observed_at"])
        self.assertEqual(self.connection.state, "connected")
        self.assertEqual(
            self.connection.last_state_observed_at,
            fields.Datetime.to_datetime("2026-08-23 12:05:00"),
        )

    def test_provider_events_same_second_use_durable_inbox_sequence(self):
        def connection_event(state):
            return EventDTO.from_dict(
                {
                    "schema_version": 1,
                    "provider_schema_version": "health-fixture-v1",
                    "event_id": "health-event-%s" % uuid.uuid4(),
                    "event_type": "connection.updated",
                    "occurred_at": "2026-08-23T12:00:00Z",
                    "account_ref": self.account.external_ref,
                    "connection_ref": self.connection.external_ref,
                    "conversation_ref": "connection-health",
                    "platform": self.account.platform,
                    "direction": "inbound",
                    "is_from_me": False,
                    "origin": "provider",
                    "actor": {"addresses": []},
                    "conversation": {
                        "conversation_type": "other",
                        "addresses": [],
                    },
                    "extensions": {"state": state, "detail": state},
                }
            )

        received_at = fields.Datetime.to_datetime("2026-08-23 12:10:00")
        application = self.env["contact.center.application"]
        # Deliberately process the newer receipt first and the older receipt last.
        application._process_event(
            self.connection,
            connection_event("connected"),
            inbox_event=SimpleNamespace(id=102, create_date=received_at),
        )
        application._process_event(
            self.connection,
            connection_event("disconnected"),
            inbox_event=SimpleNamespace(id=101, create_date=received_at),
        )
        self.connection.invalidate_recordset(
            ["state", "last_state_source", "last_state_inbox_event_id"]
        )
        self.assertEqual(self.connection.state, "connected")
        self.assertEqual(self.connection.last_state_source, "provider_event")
        self.assertEqual(self.connection.last_state_inbox_event_id, 102)

    def test_cross_source_same_second_keeps_less_permissive_state(self):
        observed_at = "2026-08-23T12:20:00+00:00"
        self.connection._apply_provider_state_event(
            "disconnected",
            observed_at=observed_at,
            observation_sequence=201,
        )
        self.connection._apply_health_result(
            {"state": "connected", "detail": "healthy"},
            source="health_job",
            observed_at=observed_at,
        )
        self.connection.invalidate_recordset(["state", "last_state_source"])
        self.assertEqual(self.connection.state, "disconnected")
        self.assertEqual(self.connection.last_state_source, "provider_event")

        self.connection._apply_health_result(
            {"state": "connected", "detail": "healthy"},
            source="health_job",
            observed_at="2026-08-23T12:20:01+00:00",
        )
        self.connection._apply_provider_state_event(
            "authentication_required",
            observed_at="2026-08-23T12:20:01+00:00",
            observation_sequence=202,
        )
        self.connection.invalidate_recordset(["state", "last_state_source"])
        self.assertEqual(self.connection.state, "authentication_required")
        self.assertEqual(self.connection.last_state_source, "provider_event")

    def test_health_bus_payload_is_safe_and_limited_to_roster_and_admins(self):
        sent = []

        def capture(_bus, notifications):
            sent.extend(notifications)
            return True

        bus_type = type(self.env["bus.bus"])
        with mock.patch.object(
            bus_type,
            "_sendmany",
            autospec=True,
            side_effect=capture,
        ):
            self.assertTrue(
                self.env["contact.center.application"]._notify_connection_health(
                    self.connection
                )
            )

        company_admins = self.env["res.users"].search(
            [
                ("active", "=", True),
                ("share", "=", False),
                (
                    "groups_id",
                    "=",
                    self.env.ref("contact_center_base.group_contact_center_admin").id,
                ),
                ("company_ids", "in", self.env.company.ids),
            ]
        )
        self.assertIn(self.admin, company_admins)
        expected_partners = (
            self.agent.partner_id
            | self.supervisor.partner_id
            | company_admins.partner_id
        )
        self.assertEqual(
            {notification[0].id for notification in sent},
            set(expected_partners.ids),
        )
        for _partner, topic, payload in sent:
            self.assertEqual(topic, "contact_center/event")
            self.assertEqual(payload["event_type"], "connection_health_updated")
            self.assertEqual(payload["connection_id"], self.connection.id)
            self.assertEqual(payload["item"]["id"], self.connection.id)
            self.assertNotIn("can_check", payload["item"])
            self.assertNotIn("last_health_error_class", payload["item"])
            self.assertNotIn("health_job_uuid", payload["item"])

    def test_removed_agent_receives_fleet_scope_invalidation(self):
        sent = []

        def capture(_bus, notifications):
            sent.extend(notifications)
            return True

        with mock.patch.object(
            type(self.env["bus.bus"]),
            "_sendmany",
            autospec=True,
            side_effect=capture,
        ):
            self.team.write({"agent_ids": [(3, self.agent.id)]})

        former_agent_payloads = [
            payload
            for partner, topic, payload in sent
            if partner == self.agent.partner_id and topic == "contact_center/event"
        ]
        self.assertTrue(former_agent_payloads)
        for payload in former_agent_payloads:
            self.assertEqual(payload["event_type"], "connection_health_updated")
            self.assertEqual(payload["connection_id"], self.connection.id)
            self.assertNotIn("item", payload)
