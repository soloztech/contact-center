"""Existing-session repair, pairing fences and explicit inbox-origin review."""
import datetime
import uuid
from unittest import mock

from odoo import fields
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.queue_job.tests.common import trap_jobs

from ..controllers import webhook
from ..models.provider_connection import _WUZAPI_OWN_IDENTITY_TOKEN
from .common import WuzapiCase

CLIENT = "odoo.addons.contact_center_wuzapi.models.reconnect.WuzapiOnboardingClient"


class TestWuzapiReconnect(WuzapiCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Reconnect Administrator",
                    "login": "cc-reconnect-" + uuid.uuid4().hex,
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [
                        (
                            6,
                            0,
                            cls.env.ref(
                                "contact_center_base.group_contact_center_admin"
                            ).ids,
                        )
                    ],
                }
            )
        )

    def setUp(self):
        super().setUp()
        self.account.own_external_identity = "5511888888888@s.whatsapp.net"
        self.wizards = self.env["contact.center.wuzapi.reconnect.wizard"].with_user(
            self.admin
        )
        self.clock = fields.Datetime.now() + datetime.timedelta(seconds=2)

    def _request(self):
        return self.wizards.create({"connection_id": self.connection.id})

    def _phase(self, phase="qr", epoch=1):
        self.connection.with_context(
            wuzapi_own_identity_token=_WUZAPI_OWN_IDENTITY_TOKEN
        ).write(
            {
                "wuzapi_pairing_revision": epoch,
                "wuzapi_pairing_state": phase,
                "identity_mismatch_latched": phase in ("qr", "expired", "mismatch"),
            }
        )

    def _proof(self, *, matches=True, offset=0):
        self.connection._apply_health_result(
            {
                "state": "connected" if matches else "degraded",
                "reason": "ready" if matches else "identity_mismatch",
                "identity_matches": matches,
            },
            observed_at=self.clock + datetime.timedelta(seconds=offset),
            records_health_probe=True,
        )

    def _held(self, epoch=1, **metadata):
        return self.env["contact.center.inbox.event"].create(
            {
                "provider_connection_id": self.connection.id,
                "inbox_dedupe_key": "held-%s-%s"
                % (epoch, self.env["contact.center.inbox.event"].search_count([])),
                "provider_schema_version": "v1.0.8",
                "state": "blocked",
                "last_error_class": "PairingUnverified",
                "raw_envelope_json": {"type": "Message"},
                "metadata_json": dict(
                    blocked_reason="pairing_unverified",
                    hold_epoch=epoch,
                    hold_configuration_revision=self.connection.health_configuration_revision,
                    **metadata
                ),
            }
        )

    def _job(
        self,
        request,
        health,
        observation=None,
        after_health=None,
        after_observation=None,
    ):
        with trap_jobs() as trap:
            request.action_start()
        token = self.connection._contact_center_health_observation_token()
        snapshots = (
            health,
            observation or {"events": ["Message", "Connected"]},
            token,
            self.clock,
        )
        with mock.patch.object(
            type(request),
            "_health_snapshot",
            side_effect=[
                snapshots,
                (
                    after_health or health,
                    snapshots[1] if after_observation is None else after_observation,
                    token,
                    self.clock,
                ),
            ],
        ), mock.patch(CLIENT) as client:
            request.with_context(job_uuid=trap.enqueued_jobs[0].uuid)._job_reconnect(
                request.request_revision
            )
        return client

    def test_only_admin_can_create_and_connection_cannot_be_changed(self):
        with self.assertRaises(AccessError):
            self.wizards.with_user(self.agent).create(
                {"connection_id": self.connection.id}
            )
        request = self._request()
        with self.assertRaises(AccessError):
            request.write({"state": "ready"})
        with self.assertRaises(AccessError):
            request.with_user(self.agent).action_start()
        with self.assertRaises(AccessError):
            self.connection.write({"wuzapi_pairing_state": "verified"})

    def test_paired_restore_preserves_lid_group_metadata_and_configuration(self):
        self.connection._apply_health_result(
            {
                "state": "connected",
                "identity_matches": True,
                "wuzapi_own_identity": {
                    "jid": self.account.own_external_identity,
                    "lid": "200000000000002@lid",
                },
            },
            records_health_probe=True,
            observed_at=self.clock - datetime.timedelta(seconds=1),
        )
        pair = dict(self.connection.wuzapi_own_identity_json)
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            conversation_type="group",
            name="Reconnect Group",
            partner_ids=self.agent.partner_id.ids,
        )
        binding = self.env["contact.center.channel.binding"].create(
            {
                "channel_id": channel.id,
                "account_id": self.account.id,
                "conversation_type": "group",
                "conversation_ref": "120363000000901@g.us",
            }
        )
        revision = self.connection.health_configuration_revision
        profile = self.env["contact.center.group.profile"].create(
            {
                "channel_binding_id": binding.id,
                "provider_connection_id": self.connection.id,
                "metadata_state": "stale",
                "next_sync_at": self.clock + datetime.timedelta(hours=2),
                "own_protocol_participant_json": {
                    "namespace": "whatsapp.pn",
                    "value": self.account.own_external_identity,
                    "value_normalized": self.account.own_external_identity,
                    "role": "sender",
                    "confidence": "protocol",
                },
                "own_protocol_participant_health_revision": revision,
            }
        )
        names = [
            "metadata_state",
            "next_sync_at",
            "own_protocol_participant_json",
            "own_protocol_participant_health_revision",
        ]
        group_before = profile.read(names)
        request = self._request()
        state = self.connection.wuzapi_onboarding_state
        self._phase("none", epoch=4)
        client = self._job(
            request,
            {"state": "disconnected", "reason": "session_inactive", "connected": False},
            after_health={
                "state": "connected",
                "connected": True,
                "identity_matches": True,
            },
        )
        self.assertEqual(request.state, "ready")
        client.return_value.connect.assert_called_once_with(())
        client.return_value.qr_png.assert_not_called()
        self.assertEqual(self.connection.wuzapi_pairing_revision, 4)
        self.assertEqual(self.connection.health_configuration_revision, revision)
        self.assertEqual(self.connection.wuzapi_onboarding_state, state)
        self.assertEqual(self.connection.wuzapi_own_identity_json, pair)
        profile.invalidate_recordset()
        self.assertEqual(profile.read(names), group_before)

    def test_qr_is_only_exposed_after_marker_and_pairing_health_are_persisted(self):
        request = self._request()
        health = {
            "state": "disconnected",
            "reason": "session_inactive",
            "connected": True,
            "logged_in": False,
        }
        self._job(request, health)
        self.assertEqual(request.state, "qr")
        self.assertEqual(self.connection.wuzapi_pairing_state, "qr")
        self.assertEqual(
            request.pairing_revision, self.connection.wuzapi_pairing_revision
        )
        self.assertTrue(self.connection.identity_mismatch_latched)
        self.assertEqual(self.connection.health_detail, "pairing_required")
        self.assertTrue(request.qr_url)
        self.assertEqual(self.connection._contact_center_fix_kind(), "reconnect")

    def test_api_outage_and_bad_credentials_never_request_qr(self):
        for health in (
            {"state": "degraded", "reason": "unreachable", "conclusive": False},
            {"state": "authentication_required", "reason": "unauthorized"},
        ):
            request = self._request()
            client = self._job(request, health)
            client.return_value.connect.assert_not_called()
            client.return_value.qr_png.assert_not_called()
            self.assertFalse(request.qr_url)
            self.assertEqual(self.connection.wuzapi_pairing_state, "none")

    def test_wrong_number_requires_unlink_and_never_exposes_qr(self):
        request = self._request()
        client = self._job(
            request,
            {
                "state": "degraded",
                "reason": "identity_mismatch",
                "connected": True,
                "identity_matches": False,
            },
        )
        self.assertEqual(request.state, "error")
        self.assertTrue(
            any(word in request.message.lower() for word in ("unlink", "desvincule"))
        )
        client.return_value.qr_png.assert_not_called()
        self.assertTrue(self.connection.identity_mismatch_latched)

    def test_old_observation_cannot_open_new_pairing_epoch(self):
        token = self.connection._contact_center_health_observation_token()
        self._phase("qr")
        self.assertFalse(
            self.connection._apply_health_result(
                {"state": "connected", "identity_matches": True},
                expected_observation_token=token,
                observed_at=self.clock,
                records_health_probe=True,
            )
        )
        self.assertTrue(self.connection.identity_mismatch_latched)
        self.assertEqual(self.connection.wuzapi_pairing_state, "qr")

    def test_mismatch_then_match_rejects_held_epoch_but_allows_future_input(self):
        self._phase()
        event = self._held()
        self._proof(matches=False)
        self.assertEqual(self.connection.wuzapi_pairing_state, "mismatch")
        self._proof(offset=1)
        self.assertEqual(self.connection.wuzapi_pairing_state, "rejected")
        self.assertFalse(self.connection._wuzapi_pairing_pending())
        self.assertFalse(self.connection.identity_mismatch_latched)
        with self.assertRaises(ValidationError):
            event.with_user(self.admin)._contact_center_confirm_pairing_origin()
        self.assertEqual(event.state, "blocked")

    def test_only_current_verified_epoch_can_be_confirmed_explicitly(self):
        self._phase()
        event = self._held()
        self._proof()
        with self.assertRaises(ValidationError):
            event.with_user(self.admin).action_requeue()
        action = event.with_user(self.admin).action_prepare_pairing_release()
        request = self.wizards.browse(action["res_id"])
        with self.assertRaises(ValidationError):
            request.action_confirm_origins()
        request.confirm_origin = True
        with trap_jobs() as trap:
            request.action_confirm_origins()
            trap.assert_jobs_count(1)
        self.assertEqual(event.state, "pending")
        self.assertEqual(
            event.metadata_json["pairing_origin_confirmed_by"], self.admin.id
        )

    def test_old_or_mixed_epochs_are_rejected_as_a_whole(self):
        self._phase(epoch=2)
        old = self._held(epoch=1)
        current = self._held(epoch=2)
        self._proof()
        with self.assertRaises(ValidationError), trap_jobs() as trap:
            (old | current).with_user(
                self.admin
            )._contact_center_confirm_pairing_origin()
            trap.assert_jobs_count(0)
        self.assertEqual((old | current).mapped("state"), ["blocked", "blocked"])
        self.assertEqual(old.pairing_hold_status, "obsolete")

    def test_negative_receipt_pending_prevents_release_after_positive_probe(self):
        self._phase()
        held = self._held()
        self.env["contact.center.inbox.event"].create(
            {
                "provider_connection_id": self.connection.id,
                "inbox_dedupe_key": "negative-receipt",
                "provider_schema_version": "v1.0.8",
                "raw_envelope_json": {"type": "LoggedOut"},
                "metadata_json": {
                    "event_type": "LoggedOut",
                    "pairing_epoch_at_receipt": 1,
                    "pairing_phase_at_receipt": "qr",
                },
            }
        )
        self._proof()
        self.assertEqual(self.connection.wuzapi_pairing_state, "verified")
        self.assertEqual(held.pairing_hold_status, "rejected")
        with self.assertRaises(ValidationError):
            held.with_user(self.admin)._contact_center_confirm_pairing_origin()

    def test_lifecycle_receipt_only_affects_its_original_epoch(self):
        self._phase()
        values = webhook._inbox_ledger_values(
            self.connection,
            {"type": "LoggedOut"},
            {"type": "LoggedOut"},
            b"{}",
            "digest",
            "logout-receipt",
            False,
        )
        event = self.env["contact.center.inbox.event"].create(values)
        self._proof()
        self.connection._apply_provider_state_event(
            "disconnected",
            detail="session_inactive",
            observed_at=self.clock - datetime.timedelta(seconds=1),
            observation_sequence=event.id,
        )
        self.assertEqual(self.connection.wuzapi_pairing_state, "rejected")
        self._phase(epoch=2)
        self.connection._apply_provider_state_event(
            "disconnected",
            detail="session_inactive",
            observed_at=self.clock + datetime.timedelta(seconds=1),
            observation_sequence=event.id,
        )
        self.assertEqual(self.connection.wuzapi_pairing_state, "qr")

    def test_dedupe_does_not_hide_logout_in_a_new_or_previously_unmarked_epoch(self):
        inbox = self.env["contact.center.inbox.event"]
        envelope = {"type": "LoggedOut"}
        original = webhook._inbox_dedupe_key(inbox, self.connection, envelope, "digest")
        values = webhook._inbox_ledger_values(
            self.connection, envelope, envelope, b"{}", "digest", original, False
        )
        event = inbox.create(values)
        self._phase()
        first = webhook._inbox_dedupe_key(inbox, self.connection, envelope, "digest")
        self.assertNotEqual(first, original)
        event.metadata_json = {
            "event_type": "LoggedOut",
            "pairing_epoch_at_receipt": 1,
            "pairing_phase_at_receipt": "qr",
        }
        self.assertEqual(
            webhook._inbox_dedupe_key(inbox, self.connection, envelope, "digest"),
            original,
        )
        self._phase(epoch=2)
        self.assertNotEqual(
            webhook._inbox_dedupe_key(inbox, self.connection, envelope, "digest"),
            original,
        )

    def test_session_ends_without_timeout_webhook_and_does_not_reopen_old_qr(self):
        self._phase()
        self.connection._apply_health_result(
            {"state": "disconnected", "reason": "session_inactive"},
            records_health_probe=True,
            observed_at=self.clock,
        )
        self.assertEqual(self.connection.wuzapi_pairing_state, "expired")
        self.assertTrue(self.connection._wuzapi_pairing_pending())
        self._proof(offset=1)
        self.assertEqual(self.connection.wuzapi_pairing_state, "verified")

    def test_revoked_or_stale_reconnect_request_cannot_call_provider(self):
        request = self._request()
        with trap_jobs() as trap:
            request.action_start()
        old_job = trap.enqueued_jobs[0]
        revision = request.request_revision
        self.connection.write({"wuzapi_onboarding_job_uuid": "superseding-operation"})
        with mock.patch(CLIENT) as client:
            self.assertFalse(
                request.with_context(job_uuid=old_job.uuid)._job_reconnect(revision)
            )
            client.assert_not_called()
        self.connection.write({"wuzapi_onboarding_job_uuid": old_job.uuid})
        self.admin.groups_id -= self.env.ref(
            "contact_center_base.group_contact_center_admin"
        )
        with mock.patch(CLIENT) as client:
            self.assertFalse(
                request.with_context(job_uuid=old_job.uuid)._job_reconnect(revision)
            )
        client.assert_not_called()
        self.assertEqual(request.sudo().state, "error")
        self.assertFalse(request.sudo().retry_at)

    def test_qr_route_fences_owner_company_epoch_and_configuration_before_io(self):
        request = self._request()
        for revision in (0, 1):
            with self.subTest(revision=revision), mock.patch(
                CLIENT
            ) as client, self.assertRaises(ValidationError):
                request._qr_png(revision)
            client.assert_not_called()
        with mock.patch(CLIENT) as client, self.assertRaises(AccessError):
            request.with_user(self.agent)._qr_png(1)
        client.assert_not_called()

    def test_wrong_scan_unlink_and_new_qr_never_release_the_old_epoch(self):
        self._phase("mismatch", epoch=1)
        held = self._held()
        wrong = self._request()
        client = self._job(
            wrong,
            {
                "state": "degraded",
                "reason": "identity_mismatch",
                "identity_matches": False,
                "connected": True,
            },
        )
        self.assertEqual(wrong.state, "error")
        client.return_value.qr_png.assert_not_called()
        self.connection._apply_health_result(
            {"state": "disconnected", "reason": "session_inactive"},
            records_health_probe=True,
            observed_at=self.clock + datetime.timedelta(seconds=1),
        )
        correct = self._request()
        with mock.patch.object(
            fields.Datetime,
            "now",
            return_value=self.clock + datetime.timedelta(seconds=5),
        ):
            client = self._job(
                correct,
                {
                    "state": "disconnected",
                    "reason": "session_inactive",
                    "connected": False,
                },
                after_health={
                    "state": "disconnected",
                    "reason": "session_inactive",
                    "connected": True,
                    "logged_in": False,
                },
            )
        client.return_value.connect.assert_called_once_with(())
        self.assertEqual(self.connection.wuzapi_pairing_revision, 2)
        self.assertEqual(self.connection.wuzapi_pairing_state, "qr")
        self._proof(offset=6)
        self.assertEqual(self.connection.wuzapi_pairing_state, "verified")
        self.assertEqual(held.pairing_hold_status, "obsolete")
        with self.assertRaises(ValidationError):
            held.with_user(self.admin)._contact_center_confirm_pairing_origin()

    def test_confirmed_pairing_event_can_recover_a_later_processing_failure(self):
        self._phase()
        event = self._held()
        self._proof()
        with trap_jobs() as trap:
            event.with_user(self.admin)._contact_center_confirm_pairing_origin()
        for job in trap.enqueued_jobs:
            if job.db_record():
                job.db_record().write({"state": "done"})
        self.assertNotIn("blocked_reason", event.metadata_json)
        self.assertEqual(event.metadata_json["hold_epoch"], 1)
        self.assertEqual(
            event.metadata_json["pairing_origin_confirmed_by"], self.admin.id
        )
        self._phase(epoch=2)
        event.write({"state": "dead", "last_error_class": "AdapterError"})
        event.invalidate_recordset(["pairing_hold_status", "pairing_hold_epoch"])
        self.assertFalse(event.pairing_hold_status)
        request = self._request()
        self.assertEqual(request.quarantined_count, 0)
        with trap_jobs() as trap:
            event.with_user(self.admin).action_requeue()
            trap.assert_jobs_count(1)
        self.assertEqual(event.state, "pending")

    def test_logged_in_unverified_identity_never_requests_a_qr(self):
        request = self._request()
        client = self._job(
            request,
            {
                "state": "degraded",
                "reason": "identity_unverified",
                "connected": True,
                "logged_in": True,
                "identity_matches": False,
            },
        )
        client.return_value.connect.assert_not_called()
        client.return_value.qr_png.assert_not_called()
        self.assertEqual(request.state, "waiting")
        self.assertFalse(self.connection.wuzapi_onboarding_job_uuid)

    def test_subscription_drift_fails_closed_with_a_specific_diagnostic(self):
        request = self._request()
        with trap_jobs() as trap:
            request.action_start()
        token = self.connection._contact_center_health_observation_token()
        with mock.patch.object(
            type(request),
            "_health_snapshot",
            side_effect=[
                (
                    {"state": "disconnected", "connected": False, "logged_in": False},
                    {"events": "Message,Connected"},
                    token,
                    self.clock,
                ),
                (
                    {
                        "state": "connected",
                        "connected": True,
                        "logged_in": True,
                        "identity_matches": True,
                    },
                    {"events": "Connected"},
                    token,
                    self.clock,
                ),
            ],
        ), mock.patch(CLIENT) as client:
            self.assertFalse(
                request.with_context(
                    job_uuid=trap.enqueued_jobs[0].uuid
                )._job_reconnect(request.request_revision)
            )
        client.return_value.qr_png.assert_not_called()
        self.assertEqual(request.state, "error")
        self.assertEqual(self.connection.wuzapi_webhook_sync_state, "drift")
        self.assertFalse(self.connection._contact_center_outbound_is_available())
        self.assertFalse(request.pending_observation_json)

    def test_unfinished_subscription_comparison_is_rechecked_without_another_connect(
        self,
    ):
        for failed in (
            {"state": "degraded", "reason": "unreachable", "conclusive": False},
            {"state": "authentication_required", "reason": "unauthorized"},
        ):
            request = self._request()
            with trap_jobs() as trap:
                request.action_start()
            token = self.connection._contact_center_health_observation_token()
            baseline = {"events": "Message,Connected", "proxy_present": False}
            with mock.patch.object(
                type(request),
                "_health_snapshot",
                side_effect=[
                    (
                        {
                            "state": "disconnected",
                            "connected": False,
                            "logged_in": False,
                        },
                        baseline,
                        token,
                        self.clock,
                    ),
                    (failed, {}, token, self.clock),
                ],
            ), mock.patch(CLIENT):
                request.with_context(
                    job_uuid=trap.enqueued_jobs[0].uuid
                )._job_reconnect(request.request_revision)
            self.assertEqual(request.state, "waiting")
            self.assertEqual(request.pending_observation_json, baseline)
            self.assertNotEqual(self.connection.wuzapi_webhook_sync_state, "drift")
            with trap_jobs() as check_jobs:
                request.action_check()
                check_jobs.assert_jobs_count(1, only=request._job_reconnect)
            good = {
                "state": "connected",
                "connected": True,
                "logged_in": True,
                "identity_matches": True,
            }
            token = self.connection._contact_center_health_observation_token()
            with mock.patch.object(
                type(request),
                "_health_snapshot",
                return_value=(
                    good,
                    baseline,
                    token,
                    self.clock + datetime.timedelta(seconds=1),
                ),
            ), mock.patch(CLIENT) as client:
                self.assertTrue(
                    request.with_context(
                        job_uuid=check_jobs.enqueued_jobs[0].uuid
                    )._job_reconnect(request.request_revision)
                )
            client.return_value.connect.assert_not_called()
            client.return_value.qr_png.assert_not_called()
            self.assertFalse(request.pending_observation_json)
            self.assertEqual(request.state, "ready")

    def test_pending_subscription_baseline_cannot_cross_configuration_change(self):
        request = self._request()
        self._job(
            request,
            {"state": "disconnected", "connected": False, "logged_in": False},
            after_observation={},
            after_health={
                "state": "degraded",
                "reason": "unreachable",
                "conclusive": False,
            },
        )
        self.assertTrue(request.pending_observation_json)
        self.connection.health_configuration_revision += 1
        with trap_jobs() as trap, mock.patch(CLIENT) as client:
            request.action_check()
            trap.assert_jobs_count(0)
        client.assert_not_called()
        self.assertEqual(request.state, "error")
        self.assertFalse(request.pending_observation_json)

    def test_banner_reopens_the_same_owners_unfinished_comparison(self):
        request = self._request()
        self._job(
            request,
            {"state": "disconnected", "connected": False, "logged_in": False},
            after_observation={},
            after_health={
                "state": "degraded",
                "reason": "unreachable",
                "conclusive": False,
            },
        )
        self.assertTrue(request.pending_observation_json)
        self._request()  # An unused newer modal must not hide the pending check.
        before = self.wizards.search_count([("connection_id", "=", self.connection.id)])
        action = self.connection.with_user(self.admin)._contact_center_fix_action()
        self.assertEqual(action["res_model"], request._name)
        self.assertEqual(action["res_id"], request.id)
        self.assertTrue(self.wizards.browse(action["res_id"]).pending_observation_json)
        self.assertEqual(
            self.wizards.search_count([("connection_id", "=", self.connection.id)]),
            before,
        )
