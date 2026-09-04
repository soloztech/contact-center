import datetime
import uuid
from unittest import mock

from psycopg2 import IntegrityError

from odoo.exceptions import ValidationError
from odoo.tests.common import Form, SavepointCase

from odoo.addons.contact_center_base.services.adapter import AdapterError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.onboarding import WuzapiOnboardingClient

ADAPTER_PATH = "odoo.addons.contact_center_wuzapi.services.adapter.WuzapiAdapter"
CLIENT_PATH = (
    "odoo.addons.contact_center_wuzapi.models.onboarding.WuzapiOnboardingClient"
)
EXISTING_INSTANCE_TOKEN = "existing-instance-token-at-least-32-chars"


class TestWuzapiOnboarding(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = cls.env.ref("base.user_admin")
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Guided Setup Agent",
                    "login": "wuzapi-guided-agent-%s" % uuid.uuid4(),
                    "email": "wuzapi-guided-agent@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, [agent_group.id])],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Guided setup team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.server = cls.env["contact.center.wuzapi.server"].create(
            {
                "name": "WuzAPI test server",
                "company_id": cls.env.company.id,
                "base_url": "https://wuzapi.invalid/",
                "admin_token": "unit-test-admin-token",
                "state": "available",
            }
        )

    def _new_wizard(self, mode="existing"):
        wizard_model = self.env["contact.center.account.setup.wizard"].with_user(
            self.admin
        )
        with Form(wizard_model) as form:
            form.inbox_name = "WhatsApp Guided"
            form.default_team_id = self.team
            form.provider_key = "wuzapi"
            wizard = form.save()
        wizard.action_next_provider()
        values = {
            "wuzapi_server_id": self.server.id,
            "wuzapi_instance_mode": mode,
            "wuzapi_instance_name": "Guided Instance",
        }
        if mode == "existing":
            values["wuzapi_existing_token"] = EXISTING_INSTANCE_TOKEN
        wizard.write(values)
        return wizard

    def test_external_create_cannot_forge_wizard_state_or_setup_reference(self):
        wizard = self.env["contact.center.account.setup.wizard"].create(
            {
                "step": "done",
                "setup_ref": "attacker",
                "inbox_name": "Forged Guided State",
                "company_id": self.env.company.id,
                "default_team_id": self.team.id,
                "provider_key": "wuzapi",
            }
        )

        self.assertEqual(wizard.step, "inbox")
        self.assertTrue(wizard.provider_available)
        self.assertNotEqual(wizard.setup_ref, "attacker")
        self.assertEqual(str(uuid.UUID(wizard.setup_ref)), wizard.setup_ref)

    def test_provider_step_can_be_saved_empty_to_allow_back_navigation(self):
        wizard_model = self.env["contact.center.account.setup.wizard"].with_user(
            self.admin
        )
        with Form(wizard_model) as form:
            form.inbox_name = "Guided Back Navigation"
            form.default_team_id = self.team
            form.provider_key = "wuzapi"
            wizard = form.save()
        wizard.action_next_provider()

        with Form(wizard) as form:
            form.save()
        wizard.action_back_inbox()

        self.assertEqual(wizard.step, "inbox")

    def test_guided_setup_defaults_owner_and_allows_owner_only_inbox(self):
        wizard_model = self.env["contact.center.account.setup.wizard"].with_user(
            self.admin
        )
        with Form(wizard_model) as form:
            form.inbox_name = "Exclusive WhatsApp"
            form.provider_key = "wuzapi"
            wizard = form.save()

        self.assertEqual(wizard.owner_user_id, self.admin)
        self.assertFalse(wizard.default_team_id)
        wizard.action_next_provider()
        wizard.write(
            {
                "wuzapi_server_id": self.server.id,
                "wuzapi_instance_mode": "existing",
                "wuzapi_instance_name": "Exclusive Guided Instance",
                "wuzapi_existing_token": EXISTING_INSTANCE_TOKEN,
            }
        )
        wizard.action_prepare()

        self.assertEqual(wizard.account_id.owner_user_id, self.admin)
        self.assertFalse(wizard.account_id.default_team_id)
        self.assertTrue(wizard.account_id._contact_center_access_is_ready())

    def test_guided_setup_propagates_optional_auto_assignment(self):
        wizard = self._new_wizard()
        wizard.write({"auto_assignment_user_id": self.agent.id})

        wizard.action_prepare()

        self.assertEqual(wizard.account_id.auto_assignment_user_id, self.agent)

    def test_guided_setup_rejects_inbox_without_owner_or_team(self):
        wizard_model = self.env["contact.center.account.setup.wizard"].with_user(
            self.admin
        )
        with Form(wizard_model) as form:
            form.inbox_name = "Unscoped WhatsApp"
            form.owner_user_id = self.env["res.users"]
            form.provider_key = "wuzapi"
            wizard = form.save()

        with self.assertRaisesRegex(ValidationError, "owner or an access team"):
            wizard.action_next_provider()

    def test_form_lists_wuzapi_and_stages_fail_closed_idempotently(self):
        wizard = self._new_wizard()
        self.assertEqual(wizard.display_name, "WhatsApp Guided")
        wizard.action_prepare()

        connection = wizard.connection_id
        self.assertEqual(wizard.step, "connect")
        self.assertEqual(connection.role, "migration")
        self.assertFalse(connection.inbound_active)
        self.assertFalse(connection.outbound_active)
        self.assertEqual(connection.account_id.platform, "whatsapp")
        self.assertEqual(connection.account_id.owner_user_id, self.admin)
        self.assertEqual(connection.account_id.default_team_id, self.team)
        self.assertEqual(connection.wuzapi_server_id, self.server)
        self.assertEqual(connection.wuzapi_api_token, EXISTING_INSTANCE_TOKEN)
        self.assertFalse(wizard.wuzapi_existing_token)
        self.assertTrue(connection.onboarding_ref)

        account_id = connection.account_id.id
        connection_id = connection.id
        wizard.action_prepare()
        self.assertEqual(wizard.account_id.id, account_id)
        self.assertEqual(wizard.connection_id.id, connection_id)

    def test_managed_setup_generates_distinct_credentials(self):
        first = self._new_wizard(mode="managed")
        second = self._new_wizard(mode="managed")
        first.action_prepare()
        second.write({"wuzapi_instance_name": "Guided Instance 2"})
        second.action_prepare()

        self.assertNotEqual(
            first.connection_id.wuzapi_api_token,
            second.connection_id.wuzapi_api_token,
        )
        self.assertNotEqual(
            first.connection_id.wuzapi_hmac_secret,
            second.connection_id.wuzapi_hmac_secret,
        )
        self.assertNotEqual(
            first.connection_id.wuzapi_instance_fingerprint,
            second.connection_id.wuzapi_instance_fingerprint,
        )

    def test_activation_is_blocked_until_cached_provider_proofs_pass(self):
        wizard = self._new_wizard()
        wizard.action_prepare()

        with self.assertRaisesRegex(ValidationError, "not ready"):
            wizard.connection_id.action_use_as_primary()

    def test_setup_rejects_header_unsafe_credentials(self):
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["contact.center.wuzapi.server"].create(
                {
                    "name": "Unsafe token server",
                    "company_id": self.env.company.id,
                    "base_url": "https://unsafe-token.invalid",
                    "admin_token": "unsafe-token-é",
                }
            )

        wizard = self._new_wizard()
        wizard.wuzapi_existing_token = "token-é"
        with self.assertRaises(ValidationError):
            wizard.action_prepare()

    def test_existing_instance_token_requires_at_least_32_characters(self):
        weak = self._new_wizard()
        weak.wuzapi_existing_token = "x" * 31
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            weak.action_prepare()

        exact_boundary = self._new_wizard()
        exact_boundary.wuzapi_existing_token = "x" * 32
        exact_boundary.action_prepare()

        self.assertEqual(exact_boundary.connection_id.wuzapi_api_token, "x" * 32)
        self.assertFalse(exact_boundary.wuzapi_existing_token)

    def test_failed_existing_setup_repair_reuses_records_and_locks_server_and_mode(
        self,
    ):
        wizard = self._new_wizard()
        wizard.action_prepare()
        connection = wizard.connection_id
        original_account = connection.account_id
        original_setup_ref = connection.onboarding_ref
        original_server = connection.wuzapi_server_id
        original_mode = wizard.wuzapi_instance_mode
        replacement_server = self.env["contact.center.wuzapi.server"].create(
            {
                "name": "Replacement WuzAPI test server",
                "company_id": self.env.company.id,
                "base_url": "https://replacement-wuzapi.invalid",
                "admin_token": "replacement-admin-token",
                "state": "available",
            }
        )
        connection.write(
            {
                "wuzapi_onboarding_state": "error",
                "wuzapi_onboarding_job_uuid": False,
            }
        )

        wizard.action_repair_provider()
        self.assertEqual(wizard.step, "provider")

        wizard.wuzapi_instance_mode = "managed"
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            wizard.action_prepare()
        wizard.wuzapi_instance_mode = original_mode

        wizard.wuzapi_server_id = replacement_server
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            wizard.action_prepare()
        wizard.wuzapi_server_id = original_server

        replacement_token = "replacement-instance-token-at-least-32-chars"
        wizard.write(
            {
                "wuzapi_instance_name": "Repaired Guided Instance",
                "wuzapi_existing_token": replacement_token,
            }
        )
        wizard.action_prepare()

        connection.invalidate_recordset(
            [
                "account_id",
                "onboarding_ref",
                "wuzapi_server_id",
                "wuzapi_managed_instance",
                "wuzapi_instance_name",
                "wuzapi_api_token",
            ]
        )
        self.assertEqual(wizard.connection_id, connection)
        self.assertEqual(wizard.account_id, original_account)
        self.assertEqual(connection.account_id, original_account)
        self.assertEqual(connection.onboarding_ref, original_setup_ref)
        self.assertEqual(connection.wuzapi_server_id, original_server)
        self.assertFalse(connection.wuzapi_managed_instance)
        self.assertEqual(connection.wuzapi_instance_name, "Repaired Guided Instance")
        self.assertEqual(connection.wuzapi_api_token, replacement_token)
        self.assertFalse(wizard.wuzapi_existing_token)
        self.assertEqual(wizard.step, "connect")

    def test_status_rejects_malformed_session_identity(self):
        client = WuzapiOnboardingClient(
            "https://wuzapi.invalid", api_token="safe-token"
        )
        payload = {
            "success": True,
            "data": {
                "connected": True,
                "loggedIn": True,
                "jid": "not-a-whatsapp-jid",
                "hmac_configured": True,
            },
        }
        with mock.patch.object(
            client, "_request", return_value=payload
        ), self.assertRaises(AdapterError):
            client.status()

    def test_status_rejects_oversized_session_identity(self):
        client = WuzapiOnboardingClient(
            "https://wuzapi.invalid", api_token="safe-token"
        )
        payload = {
            "success": True,
            "data": {
                "connected": True,
                "loggedIn": True,
                "jid": "%s@lid" % ("1" * 256),
                "hmac_configured": True,
            },
        }
        with mock.patch.object(
            client, "_request", return_value=payload
        ), self.assertRaises(AdapterError):
            client.status()

    def test_direct_guided_promotion_must_use_controlled_action(self):
        wizard = self._new_wizard()
        wizard.action_prepare()

        with self.assertRaisesRegex(ValidationError, "controlled primary switch"):
            wizard.connection_id.write({"role": "primary", "inbound_active": True})

        with self.assertRaisesRegex(ValidationError, "controlled primary switch"):
            wizard.connection_id.write({"outbound_active": True})

    def test_orphaned_setup_state_is_requeued_on_refresh(self):
        wizard = self._new_wizard()
        wizard.action_prepare()
        connection = wizard.connection_id
        connection.write(
            {
                "wuzapi_onboarding_state": "queued",
                "wuzapi_onboarding_job_uuid": "missing-job",
            }
        )

        with trap_jobs() as trap:
            wizard.action_refresh_provider()

        trap.assert_jobs_count(1)
        self.assertEqual(trap.enqueued_jobs[0].args[1], "prepare")
        self.assertNotEqual(connection.wuzapi_onboarding_job_uuid, "missing-job")

    def test_missing_pointer_adopts_the_active_canonical_setup_job(self):
        wizard = self._new_wizard()
        wizard.action_prepare()
        wizard.action_start_provider()
        connection = wizard.connection_id
        queued_job = connection._wuzapi_active_onboarding_job()
        self.assertTrue(queued_job)
        revision = connection.wuzapi_onboarding_revision
        connection.sudo().write({"wuzapi_onboarding_job_uuid": False})

        with trap_jobs() as trap:
            self.assertFalse(connection._enqueue_wuzapi_onboarding("prepare"))
            trap.assert_jobs_count(0)
        connection.invalidate_recordset(
            ["wuzapi_onboarding_job_uuid", "wuzapi_onboarding_revision"]
        )
        self.assertEqual(connection.wuzapi_onboarding_job_uuid, queued_job.uuid)
        self.assertEqual(connection.wuzapi_onboarding_revision, revision)

    def test_awaiting_scan_can_request_a_fresh_pairing_cycle(self):
        wizard = self._new_wizard()
        wizard.action_prepare()
        connection = wizard.connection_id
        connection.write(
            {
                "wuzapi_onboarding_state": "awaiting_scan",
                "wuzapi_onboarding_job_uuid": False,
            }
        )

        with trap_jobs() as trap:
            wizard.action_restart_provider()

        trap.assert_jobs_count(1)
        self.assertEqual(trap.enqueued_jobs[0].args[1], "prepare")

    @mock.patch("%s.set_webhook_configuration" % ADAPTER_PATH)
    @mock.patch("%s.set_hmac_configuration" % ADAPTER_PATH)
    @mock.patch(CLIENT_PATH)
    def test_revalidation_repairs_hmac_and_webhook_drift(
        self,
        client_class,
        set_hmac,
        set_webhook,
    ):
        wizard = self._new_wizard()
        wizard.action_prepare()
        connection = wizard.connection_id
        desired = connection._wuzapi_desired_webhook_events()
        observed = {
            "webhook_url": connection.wuzapi_webhook_url,
            "events": desired,
        }
        set_webhook.return_value = observed
        client_class.return_value.status.return_value = {
            "connected": True,
            "logged_in": True,
            "jid": "5511999999999@s.whatsapp.net",
            "hmac_configured": True,
        }

        result = connection._wuzapi_check_remote()
        self.assertEqual(
            result[:3],
            (None, observed, client_class.return_value.status.return_value),
        )
        self.assertTrue(result[3])
        set_hmac.assert_called_once_with(connection, connection.wuzapi_hmac_secret)
        set_webhook.assert_called_once_with(
            connection,
            connection.wuzapi_webhook_url,
            desired,
        )

    @mock.patch("%s.set_webhook_configuration" % ADAPTER_PATH)
    @mock.patch("%s.set_hmac_configuration" % ADAPTER_PATH)
    @mock.patch(CLIENT_PATH)
    def test_stale_health_revision_fences_provider_observation(
        self,
        client_class,
        set_hmac,
        set_webhook,
    ):
        wizard = self._new_wizard()
        wizard.action_prepare()
        connection = wizard.connection_id
        desired = connection._wuzapi_desired_webhook_events()
        set_webhook.return_value = {
            "webhook_url": connection.wuzapi_webhook_url,
            "events": desired,
        }
        client_class.return_value.status.return_value = {
            "connected": True,
            "logged_in": True,
            "jid": "5511999999999@s.whatsapp.net",
            "hmac_configured": True,
        }

        with trap_jobs() as trap:
            wizard.action_start_provider()
            job = trap.enqueued_jobs[0]
        connection.write(
            {
                "health_configuration_revision": (
                    connection.health_configuration_revision + 1
                )
            }
        )

        self.assertFalse(job.perform())
        set_hmac.assert_not_called()
        set_webhook.assert_not_called()
        self.assertFalse(connection.account_id.own_external_identity)

    @mock.patch("%s.set_webhook_configuration" % ADAPTER_PATH)
    @mock.patch("%s.set_hmac_configuration" % ADAPTER_PATH)
    @mock.patch(CLIENT_PATH)
    def test_provider_job_reaches_ready_without_promoting(
        self,
        client_class,
        set_hmac,
        set_webhook,
    ):
        wizard = self._new_wizard(mode="managed")
        wizard.action_prepare()
        connection = wizard.connection_id
        desired = connection._wuzapi_desired_webhook_events()
        observed = {
            "webhook_url": connection.wuzapi_webhook_url,
            "events": desired,
        }
        set_hmac.return_value = True
        set_webhook.return_value = observed
        client = client_class.return_value
        client.create_or_find_user.return_value = {
            "id": "remote-user-id",
            "name": "Guided Instance",
        }
        client.connect.return_value = {"already_connected": False}
        client.status.return_value = {
            "connected": True,
            "logged_in": True,
            "jid": "5511999999999@s.whatsapp.net",
            "hmac_configured": True,
        }

        with trap_jobs() as trap:
            wizard.action_start_provider()
            trap.assert_jobs_count(1)
            job = trap.enqueued_jobs[0]

        self.assertEqual(
            job.args,
            (
                connection.wuzapi_onboarding_revision,
                "prepare",
                connection.health_configuration_revision,
                connection.wuzapi_webhook_sync_revision,
                connection.wuzapi_hmac_rotation_revision,
            ),
        )
        self.assertTrue(job.perform())
        connection.invalidate_recordset()

        self.assertEqual(connection.wuzapi_onboarding_state, "ready")
        self.assertEqual(connection.wuzapi_remote_user_id, "remote-user-id")
        self.assertEqual(connection.state, "connected")
        self.assertEqual(connection.health_detail, "healthy")

        self.assertTrue(connection.wuzapi_hmac_verified_at)
        self.assertEqual(connection.wuzapi_webhook_sync_state, "in_sync")
        self.assertEqual(
            connection.account_id.own_external_identity,
            "5511999999999@s.whatsapp.net",
        )
        self.assertEqual(connection.role, "migration")
        self.assertFalse(connection.inbound_active)
        self.assertFalse(connection.outbound_active)

        buffered = (
            self.env["contact.center.inbox.event"]
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": connection.id,
                    "inbox_dedupe_key": "wuzapi:onboarding-buffered-fixture",
                    "provider_schema_version": "v1.0.8",
                    "raw_envelope_json": {"type": "Message", "event": {}},
                    "metadata_json": {
                        "blocked_reason": "onboarding_not_activated",
                        "onboarding_ref": connection.onboarding_ref,
                    },
                    "state": "blocked",
                    "last_error_class": "OnboardingNotActivated",
                }
            )
        )
        restrictive = (
            self.env["contact.center.inbox.event"]
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": connection.id,
                    "inbox_dedupe_key": (
                        "wuzapi:onboarding-buffered-disconnected-fixture"
                    ),
                    "provider_schema_version": "v1.0.8",
                    "raw_envelope_json": {
                        "type": "Disconnected",
                        "instanceName": "Guided Instance",
                        "userID": "guided-user",
                        "event": {},
                    },
                    "metadata_json": {
                        "blocked_reason": "onboarding_not_activated",
                        "onboarding_ref": connection.onboarding_ref,
                        "event_type": "Disconnected",
                    },
                    "state": "blocked",
                    "last_error_class": "OnboardingNotActivated",
                }
            )
        )
        # PostgreSQL's ``now()`` is the transaction-start timestamp.  This
        # SavepointCase creates the provider proof and callback in one long
        # transaction, unlike production where each webhook starts a later
        # transaction.  Move the fixture's audit timestamp past the proof so
        # the test faithfully models that real ordering.
        restrictive_created_at = connection.last_health_at + datetime.timedelta(
            seconds=1
        )
        self.env.cr.execute(
            "UPDATE contact_center_inbox_event SET create_date = %s WHERE id = %s",
            [restrictive_created_at, restrictive.id],
        )
        restrictive.invalidate_recordset(["create_date"])

        wizard.action_refresh_provider()
        self.assertEqual(wizard.step, "ready")
        with self.assertRaisesRegex(
            ValidationError, "newer provider disconnection"
        ), self.env.cr.savepoint():
            wizard.action_activate()

        # A new status proof obtained after the buffered lifecycle event is the
        # only safe way to make activation eligible again.
        revalidated_at = restrictive.create_date + datetime.timedelta(seconds=1)
        connection._apply_health_result(
            {
                "state": "connected",
                "reason": "ready",
                "identity_matches": True,
            },
            source="health_job",
            observed_at=revalidated_at,
            completes_check=True,
        )
        with trap_jobs() as release_trap:
            wizard.action_activate()
            release_trap.assert_jobs_count(1)
            release_job = release_trap.enqueued_jobs[0]
        self.assertTrue(release_job.perform())
        connection.invalidate_recordset()
        self.assertEqual(wizard.step, "done")
        self.assertEqual(connection.role, "primary")
        self.assertTrue(connection.inbound_active)
        self.assertTrue(connection.outbound_active)
        self.assertTrue(connection.last_health_at)
        buffered.invalidate_recordset(["state", "queue_job_uuid", "metadata_json"])
        self.assertEqual(buffered.state, "pending")
        self.assertTrue(buffered.queue_job_uuid)
        self.assertNotIn("blocked_reason", buffered.metadata_json)
        self.assertTrue(buffered.metadata_json["onboarding_replayed_at"])
        restrictive.invalidate_recordset(["state", "queue_job_uuid"])
        self.assertEqual(restrictive.state, "pending")
        self.assertTrue(
            restrictive.with_context(job_uuid=restrictive.queue_job_uuid)._job_process()
        )
        restrictive.invalidate_recordset(["state"])
        connection.invalidate_recordset(["state", "health_detail"])
        self.assertEqual(restrictive.state, "done")
        self.assertEqual(connection.state, "connected")
        self.assertEqual(connection.health_detail, "healthy")

    def test_onboarding_proof_preserves_concurrent_health_job_and_newer_restriction(
        self,
    ):
        wizard = self._new_wizard(mode="managed")
        wizard.action_prepare()
        connection = wizard.connection_id

        with trap_jobs() as onboarding_trap:
            wizard.action_start_provider()
            onboarding_trap.assert_jobs_count(1)
            onboarding_job = onboarding_trap.enqueued_jobs[0]
        with trap_jobs() as health_trap:
            connection._enqueue_health_check()
            health_trap.assert_jobs_count(1)
            health_job = health_trap.enqueued_jobs[0]

        (
            revision,
            _operation,
            configuration_revision,
            webhook_revision,
            hmac_revision,
        ) = onboarding_job.args
        proof_at = datetime.datetime(2026, 8, 30, 12, 0, 0)
        desired_events = connection._wuzapi_desired_webhook_events()

        self.assertTrue(
            connection._wuzapi_apply_onboarding_observation(
                revision,
                onboarding_job.uuid,
                configuration_revision,
                webhook_revision,
                hmac_revision,
                None,
                {
                    "webhook_url": connection.wuzapi_webhook_url,
                    "events": desired_events,
                },
                {
                    "connected": True,
                    "logged_in": True,
                    "jid": "5511999999999@s.whatsapp.net",
                    "hmac_configured": True,
                },
                proof_at,
            )
        )
        connection.invalidate_recordset(
            [
                "state",
                "health_detail",
                "last_health_at",
                "health_check_pending",
                "health_job_uuid",
            ]
        )
        self.assertEqual(connection.state, "connected")
        self.assertEqual(connection.health_detail, "healthy")
        self.assertEqual(connection.last_health_at, proof_at)
        self.assertTrue(connection.health_check_pending)
        self.assertEqual(connection.health_job_uuid, health_job.uuid)

        restrictive_at = proof_at + datetime.timedelta(seconds=1)
        self.assertTrue(
            connection._apply_provider_state_event(
                "authentication_required",
                observed_at=restrictive_at,
                observation_sequence=901,
            )
        )
        connection.invalidate_recordset(
            [
                "state",
                "health_detail",
                "last_state_observed_at",
                "health_check_pending",
                "health_job_uuid",
            ]
        )
        self.assertEqual(connection.state, "authentication_required")
        self.assertEqual(connection.health_detail, "authentication_required")
        self.assertEqual(connection.last_state_observed_at, restrictive_at)
        self.assertTrue(connection.health_check_pending)
        self.assertEqual(connection.health_job_uuid, health_job.uuid)
        self.assertEqual(health_job.state, "pending")

        canonical_probe_at = restrictive_at + datetime.timedelta(seconds=1)
        self.assertTrue(
            connection._apply_health_result(
                {
                    "state": "authentication_required",
                    "detail": "session_not_authenticated",
                },
                source="health_job",
                observed_at=canonical_probe_at,
                expected_job_uuid=health_job.uuid,
                expected_configuration_revision=(
                    connection.health_configuration_revision
                ),
                completes_check=True,
            )
        )
        connection.invalidate_recordset(
            [
                "state",
                "health_detail",
                "last_health_at",
                "health_check_pending",
                "health_job_uuid",
            ]
        )
        self.assertEqual(connection.state, "authentication_required")
        self.assertEqual(connection.health_detail, "authentication_required")
        self.assertEqual(connection.last_health_at, canonical_probe_at)
        self.assertFalse(connection.health_check_pending)
        self.assertFalse(connection.health_job_uuid)

    def test_same_active_instance_cannot_back_two_connections(self):
        first = self._new_wizard()
        first.action_prepare()
        first.connection_id.flush_recordset(["wuzapi_instance_fingerprint"])
        second_account = self.env["contact.center.account"].create(
            {
                "name": "Duplicate instance account",
                "company_id": self.env.company.id,
                "platform": "whatsapp",
                "default_team_id": self.team.id,
            }
        )
        duplicate = self.env["contact.center.provider.connection"].create(
            {
                "name": "Duplicate instance",
                "account_id": second_account.id,
                "adapter_key": "wuzapi",
                "active": True,
                "role": "migration",
                "inbound_active": False,
                "outbound_active": False,
                "wuzapi_base_url": first.connection_id.wuzapi_base_url,
                "wuzapi_api_token": "distinct-unit-test-token",
                "wuzapi_hmac_secret": "another-unit-test-secret-at-least-32-chars",
            }
        )
        duplicate.flush_recordset(["wuzapi_instance_fingerprint"])
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            # Exercise the database invariant without leaving a failed stored
            # compute pending in the shared Odoo test environment.
            self.env.cr.execute(
                "UPDATE contact_center_provider_connection "
                "SET wuzapi_instance_fingerprint = %s WHERE id = %s",
                [first.connection_id.wuzapi_instance_fingerprint, duplicate.id],
            )
