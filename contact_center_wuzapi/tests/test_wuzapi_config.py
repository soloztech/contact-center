import datetime
import json
import secrets
import uuid
from unittest import mock

import requests
from lxml import etree

from odoo import fields
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderRateLimitError,
    adapter_registry,
)
from odoo.addons.contact_center_base.services.job import provider_paused_retry_seconds
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.adapter import WUZAPI_DEFAULT_WEBHOOK_EVENTS, WUZAPI_WEBHOOK_EVENT_TYPES
from .common import WuzapiCase

REQUEST_PATCH = "odoo.addons.contact_center_wuzapi.services.adapter.requests.request"


class WebhookResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size=65536):
        raw = json.dumps(self.payload, separators=(",", ":")).encode("utf-8")
        for offset in range(0, len(raw), chunk_size):
            yield raw[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class TestWuzapiConfig(WuzapiCase):
    def test_configuration_is_pinned_and_url_is_normalized(self):
        self.assertEqual(self.connection.wuzapi_base_url, "https://wuzapi.invalid")
        self.assertEqual(self.connection.wuzapi_provider_version, "v1.0.8")
        self.assertEqual(self.connection.wuzapi_provider_commit, "9487eca")
        self.assertGreaterEqual(len(self.connection.wuzapi_webhook_key), 32)
        self.assertEqual(
            set(self.connection.wuzapi_webhook_event_ids.mapped("technical_name")),
            set(WUZAPI_DEFAULT_WEBHOOK_EVENTS),
        )
        self.assertTrue(self.connection.capabilities_json["sender_signature"])
        self.assertTrue(
            self.connection.capabilities_json["conversation_types"]["group"][
                "sender_signature"
            ]
        )

    def test_topology_lock_precedes_provider_local_configuration_lock(self):
        call_order = []

        def topology_lock(*_args, **_kwargs):
            call_order.append("topology")

        def configuration_lock(*_args, **_kwargs):
            call_order.append("configuration")

        model_class = type(self.connection)
        with mock.patch.object(
            model_class,
            "_wuzapi_lock_topology_for_write",
            side_effect=topology_lock,
        ), mock.patch.object(
            model_class,
            "_wuzapi_lock_configuration_candidates",
            side_effect=configuration_lock,
        ):
            self.connection.write(
                {
                    "outbound_active": False,
                    "wuzapi_webhook_event_ids": [
                        (6, 0, self.connection.wuzapi_webhook_event_ids.ids)
                    ],
                }
            )

        self.assertEqual(call_order, ["topology", "configuration"])

    def test_pinned_webhook_catalog_is_complete(self):
        catalog = self.env["contact.center.wuzapi.webhook.event"].search([])
        self.assertEqual(
            set(catalog.mapped("technical_name")),
            set(WUZAPI_WEBHOOK_EVENT_TYPES),
        )
        self.assertEqual(
            set(catalog.filtered("default_enabled").mapped("technical_name")),
            set(WUZAPI_DEFAULT_WEBHOOK_EVENTS),
        )
        control_events = catalog.filtered(
            lambda event: event.technical_name
            in {"CallAccept", "CallOffer", "CallTerminate", "IdentityChange"}
        )
        self.assertEqual(len(control_events), 4)
        self.assertTrue(all(control_events.mapped("adapter_supported")))
        self.assertTrue(all(control_events.mapped("default_enabled")))

    @mock.patch(REQUEST_PATCH)
    def test_hmac_rotation_is_fenced_and_does_not_put_secret_in_job(self, request):
        response = WebhookResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {"Details": "HMAC configuration saved successfully"},
            },
        )
        request.return_value = response
        previous_secret = self.connection.wuzapi_hmac_secret

        with trap_jobs() as trap:
            action = self.connection.action_wuzapi_rotate_hmac()
            request.assert_not_called()
            trap.assert_jobs_count(1)
            queued_job = trap.enqueued_jobs[0]

        pending_secret = self.connection.wuzapi_hmac_pending_secret
        self.assertTrue(pending_secret)
        self.assertNotEqual(pending_secret, previous_secret)
        self.assertEqual(self.connection.wuzapi_hmac_rotation_state, "pending")
        self.assertEqual(
            queued_job.args,
            (self.connection.wuzapi_hmac_rotation_revision,),
        )
        self.assertEqual(
            queued_job.identity_key,
            "contact_center:wuzapi_hmac:%s:%s"
            % (self.connection.id, self.connection.wuzapi_hmac_rotation_revision),
        )
        serialized_job = repr(
            (
                queued_job.args,
                queued_job.kwargs,
                queued_job.description,
                queued_job.identity_key,
            )
        )
        self.assertNotIn(pending_secret, serialized_job)
        self.assertEqual(action["params"]["type"], "info")

        with trap_jobs() as cleanup_trap:
            self.assertTrue(queued_job.perform())
            cleanup_trap.assert_jobs_count(1)
            cleanup_job = cleanup_trap.enqueued_jobs[0]
        request.assert_called_once()
        call_args, call_kwargs = request.call_args
        self.assertEqual(
            call_args,
            ("POST", "https://wuzapi.invalid/session/hmac/config"),
        )
        self.assertEqual(call_kwargs["json"], {"hmac_key": pending_secret})
        self.assertFalse(call_kwargs["allow_redirects"])
        self.assertTrue(call_kwargs["stream"])
        self.assertTrue(response.closed)

        self.connection.invalidate_recordset()
        self.assertEqual(self.connection.wuzapi_hmac_secret, pending_secret)
        self.assertFalse(self.connection.wuzapi_hmac_pending_secret)
        self.assertEqual(self.connection.wuzapi_hmac_rotation_state, "stable")
        self.assertFalse(self.connection.wuzapi_hmac_rotation_job_uuid)
        self.assertTrue(self.connection.wuzapi_hmac_rotated_at)
        self.assertEqual(
            self.connection.wuzapi_hmac_previous_secret,
            previous_secret,
        )
        self.assertGreater(
            self.connection.wuzapi_hmac_previous_valid_until,
            self.connection.wuzapi_hmac_rotated_at,
        )
        with self.assertRaises(ValidationError):
            self.connection.action_wuzapi_rotate_hmac()
        self.assertEqual(
            cleanup_job.args,
            (self.connection.wuzapi_hmac_rotation_revision,),
        )
        self.assertNotIn(
            previous_secret,
            repr(
                (
                    cleanup_job.args,
                    cleanup_job.description,
                    cleanup_job.identity_key,
                )
            ),
        )

        self.connection._wuzapi_hmac_internal().write(
            {
                "wuzapi_hmac_previous_valid_until": (
                    fields.Datetime.now() - datetime.timedelta(seconds=1)
                )
            }
        )
        self.assertTrue(cleanup_job.perform())
        self.connection.invalidate_recordset()
        self.assertFalse(self.connection.wuzapi_hmac_previous_secret)
        self.assertFalse(self.connection.wuzapi_hmac_previous_valid_until)

    @mock.patch(REQUEST_PATCH)
    def test_hmac_accepts_exact_bare_ack_from_pinned_handler(self, request):
        response = WebhookResponse(
            200,
            {"Details": "HMAC configuration saved successfully"},
        )
        request.return_value = response

        self.assertTrue(
            self.connection.get_adapter().set_hmac_configuration(
                self.connection,
                "replacement-hmac-secret-at-least-32-characters",
            )
        )
        self.assertTrue(response.closed)

    @mock.patch(REQUEST_PATCH)
    def test_hmac_rejects_bare_ack_with_unexpected_fields(self, request):
        request.return_value = WebhookResponse(
            200,
            {
                "Details": "HMAC configuration saved successfully",
                "unexpected": True,
            },
        )

        with self.assertRaises(AdapterError):
            self.connection.get_adapter().set_hmac_configuration(
                self.connection,
                "replacement-hmac-secret-at-least-32-characters",
            )

    @mock.patch(REQUEST_PATCH)
    def test_ambiguous_hmac_http_success_never_promotes_local_key(self, request):
        response = WebhookResponse(200, {})
        request.return_value = response
        previous_secret = self.connection.wuzapi_hmac_secret
        with trap_jobs() as trap:
            self.connection.action_wuzapi_rotate_hmac()
            queued_job = trap.enqueued_jobs[0]
        pending_secret = self.connection.wuzapi_hmac_pending_secret

        self.assertFalse(queued_job.perform())
        self.connection.invalidate_recordset()
        self.assertEqual(self.connection.wuzapi_hmac_secret, previous_secret)
        self.assertEqual(self.connection.wuzapi_hmac_pending_secret, pending_secret)
        self.assertEqual(self.connection.wuzapi_hmac_rotation_state, "error")
        self.assertNotIn(
            pending_secret,
            self.connection.wuzapi_hmac_rotation_last_error,
        )

    @mock.patch(REQUEST_PATCH)
    def test_hmac_failure_keeps_current_key_and_a_retriable_private_stage(
        self, request
    ):
        response = WebhookResponse(401, {"success": False})
        request.return_value = response
        previous_secret = self.connection.wuzapi_hmac_secret
        with trap_jobs() as trap:
            self.connection.action_wuzapi_rotate_hmac()
            queued_job = trap.enqueued_jobs[0]
        pending_secret = self.connection.wuzapi_hmac_pending_secret

        self.assertFalse(queued_job.perform())
        self.connection.invalidate_recordset()
        self.assertEqual(self.connection.wuzapi_hmac_secret, previous_secret)
        self.assertEqual(self.connection.wuzapi_hmac_pending_secret, pending_secret)
        self.assertEqual(self.connection.wuzapi_hmac_rotation_state, "error")
        self.assertFalse(self.connection.wuzapi_hmac_rotation_job_uuid)
        self.assertNotIn(
            pending_secret,
            self.connection.wuzapi_hmac_rotation_last_error,
        )
        self.assertNotIn(
            previous_secret,
            self.connection.wuzapi_hmac_rotation_last_error,
        )

    @mock.patch(REQUEST_PATCH)
    def test_superseded_hmac_job_never_calls_provider_or_promotes(self, request):
        with trap_jobs() as trap:
            self.connection.action_wuzapi_rotate_hmac()
            queued_job = trap.enqueued_jobs[0]
        original_secret = self.connection.wuzapi_hmac_secret
        replacement_stage = secrets.token_urlsafe(48)
        self.connection._wuzapi_hmac_internal().write(
            {
                "wuzapi_hmac_pending_secret": replacement_stage,
                "wuzapi_hmac_rotation_revision": (
                    self.connection.wuzapi_hmac_rotation_revision + 1
                ),
                "wuzapi_hmac_rotation_job_uuid": False,
                "wuzapi_hmac_rotation_state": "error",
            }
        )

        self.assertFalse(queued_job.perform())
        request.assert_not_called()
        self.connection.invalidate_recordset()
        self.assertEqual(self.connection.wuzapi_hmac_secret, original_secret)
        self.assertEqual(
            self.connection.wuzapi_hmac_pending_secret,
            replacement_stage,
        )

    def test_established_hmac_secret_cannot_be_edited_directly(self):
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.connection.write(
                {"wuzapi_hmac_secret": "manual-replacement-secret-at-least-32-chars"}
            )

    @mock.patch(REQUEST_PATCH)
    def test_apply_webhook_replaces_and_verifies_remote_selection(self, request):
        desired = sorted(WUZAPI_DEFAULT_WEBHOOK_EVENTS)
        webhook_url = self.connection.wuzapi_webhook_url
        request.side_effect = [
            WebhookResponse(
                200,
                {
                    "success": True,
                    "data": {
                        "webhook": webhook_url,
                        "events": desired,
                        "active": True,
                    },
                },
            ),
            WebhookResponse(
                200,
                {
                    "success": True,
                    "data": {"webhook": webhook_url, "subscribe": desired},
                },
            ),
        ]

        with trap_jobs() as trap:
            action = self.connection.action_wuzapi_apply_webhook()
            request.assert_not_called()
            trap.assert_jobs_count(1)
            queued_job = trap.enqueued_jobs[0]

        self.assertEqual(action["params"]["type"], "info")
        self.assertEqual(self.connection.wuzapi_webhook_sync_state, "pending")
        self.assertEqual(self.connection.wuzapi_webhook_job_uuid, queued_job.uuid)
        self.assertEqual(queued_job.channel, "root.contact_center.wuzapi_webhook")
        self.assertEqual(queued_job.priority, 40)
        self.assertEqual(
            queued_job.args,
            (self.connection.wuzapi_webhook_sync_revision, "apply"),
        )
        self.assertEqual(queued_job.recordset.ids, self.connection.ids)
        self.assertEqual(
            queued_job.identity_key,
            "contact_center:wuzapi_webhook:%s:%s"
            % (self.connection.id, self.connection.wuzapi_webhook_sync_revision),
        )

        self.assertTrue(
            self.connection.with_context(
                job_uuid=queued_job.uuid
            )._job_wuzapi_sync_webhook(
                self.connection.wuzapi_webhook_sync_revision, "apply"
            )
        )
        self.assertEqual(self.connection.wuzapi_webhook_sync_state, "in_sync")
        self.assertTrue(self.connection.wuzapi_webhook_url_matches)
        self.assertEqual(self.connection.wuzapi_webhook_observed_events_json, desired)
        self.assertFalse(self.connection.wuzapi_webhook_job_uuid)
        put_call, get_call = request.call_args_list
        self.assertEqual(put_call.args, ("PUT", "https://wuzapi.invalid/webhook"))
        self.assertEqual(
            put_call.kwargs["json"],
            {"webhook": webhook_url, "events": desired, "active": True},
        )
        self.assertEqual(get_call.args, ("GET", "https://wuzapi.invalid/webhook"))
        self.assertIsNone(get_call.kwargs["json"])
        self.assertFalse(
            any(call.kwargs.get("allow_redirects") for call in (put_call, get_call))
        )

    @mock.patch(REQUEST_PATCH)
    def test_refresh_webhook_reports_drift_without_overwriting_desired(self, request):
        desired_before = self.connection.wuzapi_webhook_event_ids
        request.return_value = WebhookResponse(
            200,
            {
                "success": True,
                "data": {
                    "webhook": self.connection.wuzapi_webhook_url,
                    "subscribe": ["Message"],
                },
            },
        )

        with trap_jobs() as trap:
            action = self.connection.action_wuzapi_refresh_webhook()
            request.assert_not_called()
            trap.assert_jobs_count(1)
            queued_job = trap.enqueued_jobs[0]

        self.assertEqual(action["params"]["type"], "info")
        self.assertFalse(
            self.connection.with_context(
                job_uuid=queued_job.uuid
            )._job_wuzapi_sync_webhook(
                self.connection.wuzapi_webhook_sync_revision, "refresh"
            )
        )
        self.assertEqual(self.connection.wuzapi_webhook_sync_state, "drift")
        self.assertEqual(self.connection.wuzapi_webhook_event_ids, desired_before)
        self.assertEqual(
            self.connection.wuzapi_webhook_observed_events_json, ["Message"]
        )

    @mock.patch(REQUEST_PATCH)
    def test_apply_webhook_reports_provider_discard_as_verified_drift(self, request):
        webhook_url = self.connection.wuzapi_webhook_url
        requested = sorted(WUZAPI_DEFAULT_WEBHOOK_EVENTS)
        request.side_effect = [
            WebhookResponse(
                200,
                {
                    "success": True,
                    "data": {
                        "webhook": webhook_url,
                        "events": requested[:-1],
                        "active": True,
                    },
                },
            ),
            WebhookResponse(
                200,
                {
                    "success": True,
                    "data": {
                        "webhook": webhook_url,
                        "subscribe": requested[:-1],
                    },
                },
            ),
        ]

        with trap_jobs() as trap:
            action = self.connection.action_wuzapi_apply_webhook()
            request.assert_not_called()
            queued_job = trap.enqueued_jobs[0]

        self.assertEqual(action["params"]["type"], "info")
        self.assertFalse(
            self.connection.with_context(
                job_uuid=queued_job.uuid
            )._job_wuzapi_sync_webhook(
                self.connection.wuzapi_webhook_sync_revision, "apply"
            )
        )
        self.assertEqual(self.connection.wuzapi_webhook_sync_state, "drift")
        self.assertEqual(
            self.connection.wuzapi_webhook_observed_events_json, requested[:-1]
        )

    @mock.patch(REQUEST_PATCH)
    def test_webhook_stream_failure_uses_queue_retry_pattern(self, request):
        response = mock.Mock(status_code=200, headers={})
        response.iter_content.side_effect = requests.ConnectionError("stream reset")
        request.return_value = response

        with trap_jobs() as trap:
            action = self.connection.action_wuzapi_refresh_webhook()
            request.assert_not_called()
            queued_job = trap.enqueued_jobs[0]

        self.assertEqual(action["params"]["type"], "info")
        with self.assertRaises(RetryableJobError) as raised:
            self.connection.with_context(
                job_uuid=queued_job.uuid
            )._job_wuzapi_sync_webhook(
                self.connection.wuzapi_webhook_sync_revision, "refresh"
            )
        self.assertIsNone(raised.exception.seconds)
        self.assertEqual(self.connection.wuzapi_webhook_sync_state, "pending")
        self.assertEqual(self.connection.wuzapi_webhook_job_uuid, queued_job.uuid)
        response.close.assert_called_once_with()

    @mock.patch(REQUEST_PATCH)
    def test_webhook_rate_limit_retries_without_consuming_ceiling(self, request):
        response = WebhookResponse(
            429,
            {"success": False},
            headers={"Retry-After": "37"},
        )
        request.return_value = response

        with trap_jobs() as trap:
            self.connection.action_wuzapi_refresh_webhook()
            queued_job = trap.enqueued_jobs[0]
        queued_job.retry = 4

        with mock.patch.object(
            type(self.connection),
            "_wuzapi_webhook_job_attempt",
            autospec=True,
        ) as job_attempt, self.assertRaises(RetryableJobError) as raised:
            queued_job.perform()

        self.assertTrue(response.closed)
        self.assertIsInstance(raised.exception.__cause__, ProviderRateLimitError)
        self.assertEqual(raised.exception.__cause__.retry_after_seconds, 37)
        self.assertTrue(raised.exception.ignore_retry)
        self.assertEqual(
            raised.exception.seconds,
            provider_paused_retry_seconds(
                raised.exception.__cause__,
                ("wuzapi_webhook", self.connection.id),
            ),
        )
        self.assertEqual(queued_job.retry, 4)
        job_attempt.assert_not_called()
        self.connection.invalidate_recordset()
        self.assertEqual(self.connection.wuzapi_webhook_sync_state, "pending")
        self.assertEqual(self.connection.wuzapi_webhook_job_uuid, queued_job.uuid)

    @mock.patch(REQUEST_PATCH)
    def test_webhook_server_error_still_respects_retry_ceiling(self, request):
        response = WebhookResponse(503, {"success": False})
        request.return_value = response

        with trap_jobs() as trap:
            self.connection.action_wuzapi_refresh_webhook()
            queued_job = trap.enqueued_jobs[0]

        with mock.patch.object(
            type(self.connection),
            "_wuzapi_webhook_job_attempt",
            autospec=True,
            return_value=9,
        ) as job_attempt:
            self.assertFalse(queued_job.perform())

        self.assertTrue(response.closed)
        job_attempt.assert_called_once()
        attempted_connection, attempted_job_uuid = job_attempt.call_args.args
        self.assertEqual(attempted_connection.ids, self.connection.ids)
        self.assertEqual(attempted_job_uuid, queued_job.uuid)
        self.connection.invalidate_recordset()
        self.assertEqual(self.connection.wuzapi_webhook_sync_state, "error")
        self.assertFalse(self.connection.wuzapi_webhook_job_uuid)
        self.assertTrue(self.connection.wuzapi_webhook_last_error)

    @mock.patch(REQUEST_PATCH)
    def test_permanent_webhook_failure_is_persisted_by_the_job(self, request):
        response = WebhookResponse(401, {"success": False})
        request.return_value = response
        with trap_jobs() as trap:
            self.connection.action_wuzapi_refresh_webhook()
            request.assert_not_called()
            queued_job = trap.enqueued_jobs[0]

        self.assertFalse(
            self.connection.with_context(
                job_uuid=queued_job.uuid
            )._job_wuzapi_sync_webhook(
                self.connection.wuzapi_webhook_sync_revision, "refresh"
            )
        )

        self.assertTrue(response.closed)
        self.assertEqual(self.connection.wuzapi_webhook_sync_state, "error")
        self.assertFalse(self.connection.wuzapi_webhook_job_uuid)
        self.assertTrue(self.connection.wuzapi_webhook_last_error)
        self.assertNotIn(
            self.connection.wuzapi_api_token,
            self.connection.wuzapi_webhook_last_error,
        )

    @mock.patch(REQUEST_PATCH)
    def test_inflight_result_cannot_overwrite_a_newer_selection(self, request):
        original_events = sorted(WUZAPI_DEFAULT_WEBHOOK_EVENTS)
        webhook_url = self.connection.wuzapi_webhook_url
        message_event = self.env.ref("contact_center_wuzapi.webhook_event_message")

        def change_selection_during_provider_call(method, _url, **_kwargs):
            if method == "PUT":
                self.connection.wuzapi_webhook_event_ids = message_event
                return WebhookResponse(
                    200,
                    {
                        "success": True,
                        "data": {
                            "webhook": webhook_url,
                            "events": original_events,
                            "active": True,
                        },
                    },
                )
            return WebhookResponse(
                200,
                {
                    "success": True,
                    "data": {
                        "webhook": webhook_url,
                        "subscribe": original_events,
                    },
                },
            )

        request.side_effect = change_selection_during_provider_call
        with trap_jobs() as trap:
            self.connection.action_wuzapi_apply_webhook()
            queued_job = trap.enqueued_jobs[0]
            old_revision = self.connection.wuzapi_webhook_sync_revision

        self.assertFalse(
            self.connection.with_context(
                job_uuid=queued_job.uuid
            )._job_wuzapi_sync_webhook(old_revision, "apply")
        )

        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.connection.wuzapi_webhook_sync_revision, old_revision + 1)
        self.assertEqual(self.connection.wuzapi_webhook_event_ids, message_event)
        self.assertEqual(self.connection.wuzapi_webhook_sync_state, "pending")
        self.assertFalse(self.connection.wuzapi_webhook_job_uuid)
        self.assertFalse(self.connection.wuzapi_webhook_observed_events_json)

    @mock.patch(REQUEST_PATCH)
    def test_superseded_webhook_job_does_not_call_the_provider(self, request):
        message_event = self.env.ref("contact_center_wuzapi.webhook_event_message")
        with trap_jobs() as trap:
            self.connection.action_wuzapi_apply_webhook()
            queued_job = trap.enqueued_jobs[0]
            old_revision = self.connection.wuzapi_webhook_sync_revision

        self.connection.wuzapi_webhook_event_ids = message_event

        self.assertFalse(
            self.connection.with_context(
                job_uuid=queued_job.uuid
            )._job_wuzapi_sync_webhook(old_revision, "apply")
        )
        request.assert_not_called()
        self.assertEqual(self.connection.wuzapi_webhook_sync_state, "pending")
        self.assertFalse(self.connection.wuzapi_webhook_job_uuid)

    def test_webhook_job_uses_the_declared_retry_pattern(self):
        function = self.env.ref(
            "contact_center_wuzapi.queue_job_function_contact_center_wuzapi_webhook"
        )
        self.assertEqual(
            function.retry_pattern,
            {
                "1": 10,
                "2": 30,
                "3": 60,
                "4": 120,
                "5": 300,
                "6": 600,
                "7": 1200,
                "8": 1800,
            },
        )
        self.assertEqual(
            function.channel_id.complete_name,
            "root.contact_center.wuzapi_webhook",
        )

    def test_hmac_job_uses_the_declared_retry_pattern(self):
        function = self.env.ref(
            "contact_center_wuzapi."
            "queue_job_function_contact_center_wuzapi_hmac_rotation"
        )
        self.assertEqual(
            function.retry_pattern,
            {
                "1": 10,
                "2": 30,
                "3": 60,
                "4": 120,
                "5": 300,
                "6": 600,
                "7": 1200,
                "8": 1800,
            },
        )
        self.assertEqual(
            function.channel_id.complete_name,
            "root.contact_center.wuzapi_webhook",
        )

    def test_all_webhook_subscription_must_be_selected_alone(self):
        all_event = self.env.ref("contact_center_wuzapi.webhook_event_all")
        message_event = self.env.ref("contact_center_wuzapi.webhook_event_message")
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.connection.wuzapi_webhook_event_ids = all_event | message_event

    def test_active_outbound_freezes_dispatch_identity_configuration(self):
        self.env.cr.execute(
            "UPDATE contact_center_provider_connection "
            "SET role = 'primary', inbound_active = TRUE, "
            "outbound_active = TRUE WHERE id = %s",
            [self.connection.id],
        )
        self.connection.invalidate_recordset(
            ["role", "inbound_active", "outbound_active"]
        )

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.connection.write({"wuzapi_api_token": "rotated-token"})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.account.write({"own_external_identity": "+5511999999999"})

    def test_safe_token_rotation_latches_and_schedules_identity_verification(self):
        self.connection.write({"health_retry_not_before": "2099-01-01 00:00:00"})
        with trap_jobs() as trap:
            self.connection.write({"wuzapi_api_token": "rotated-token"})
            trap.assert_jobs_count(1)

        self.connection.invalidate_recordset(
            [
                "state",
                "health_detail",
                "identity_mismatch_latched",
                "health_retry_not_before",
                "next_health_check_at",
            ]
        )
        self.assertEqual(self.connection.state, "degraded")
        self.assertEqual(self.connection.health_detail, "identity_unverified")
        self.assertTrue(self.connection.identity_mismatch_latched)
        self.assertFalse(self.connection.health_retry_not_before)
        self.assertGreater(self.connection.next_health_check_at, fields.Datetime.now())

    def test_wuzapi_connection_requires_its_configuration(self):
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["contact.center.provider.connection"].create(
                {
                    "name": "Incomplete WuzAPI connection",
                    "account_id": self.account.id,
                    "adapter_key": "wuzapi",
                    "active": True,
                    "role": "standby",
                    "inbound_active": False,
                    "outbound_active": False,
                }
            )

    def test_wuzapi_connection_requires_whatsapp_account(self):
        telegram_account = self.env["contact.center.account"].create(
            {
                "name": "Telegram test account",
                "company_id": self.env.company.id,
                "platform": "telegram",
            }
        )

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["contact.center.provider.connection"].create(
                {
                    "name": "WuzAPI on wrong platform",
                    "account_id": telegram_account.id,
                    "adapter_key": "wuzapi",
                    "active": True,
                    "role": "standby",
                    "inbound_active": False,
                    "outbound_active": False,
                    "wuzapi_base_url": "https://wuzapi.invalid",
                    "wuzapi_api_token": "token",
                    "wuzapi_hmac_secret": "secret",
                }
            )

    def test_configuration_lives_on_provider_connection(self):
        self.assertIn(
            "wuzapi_base_url",
            self.env["contact.center.provider.connection"]._fields,
        )
        self.assertNotIn("contact.center.wuzapi.config", self.env.registry.models)

    def test_configuration_rejects_unvalidated_provider_revision(self):
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.update_config(
                wuzapi_provider_version="main",
                wuzapi_provider_commit="unvalidated",
            )

    def test_url_rejects_embedded_credentials_and_query(self):
        for invalid_url in (
            "ftp://wuzapi.invalid",
            "https://user:password@wuzapi.invalid",
            "https://@wuzapi.invalid",
            "https://wuzapi.invalid?token=secret",
            "https://wuzapi.invalid:abc",
            "https://wuzapi.invalid:0",
            "https://wuzapi.invalid\\redirect",
            "https://bad host.invalid",
            "https://wuzapi.invalid/\nredirect",
        ):
            with self.subTest(url=invalid_url), self.assertRaises(
                ValidationError
            ), self.env.cr.savepoint():
                self.update_config(wuzapi_base_url=invalid_url)

    def test_url_normalizer_accepts_only_the_service_origin_root(self):
        normalizer = self.env[
            "contact.center.provider.connection"
        ]._normalize_wuzapi_base_url
        for root_url in (
            "https://wuzapi.invalid",
            "https://wuzapi.invalid/",
            "HTTPS://WUZAPI.INVALID:443/",
        ):
            with self.subTest(url=root_url):
                self.assertEqual(normalizer(root_url), "https://wuzapi.invalid")

        for path_url in (
            "https://wuzapi.invalid/.",
            "https://wuzapi.invalid/foo/..",
            "https://wuzapi.invalid/api",
            "https://wuzapi.invalid/api/",
            "https://wuzapi.invalid/%2e",
        ):
            with self.subTest(url=path_url), self.assertRaises(ValidationError):
                normalizer(path_url)

    def test_webhook_routing_key_must_be_url_safe(self):
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.update_config(wuzapi_webhook_key="not a valid webhook key")

    def test_capabilities_never_contain_credentials(self):
        adapter = adapter_registry.get("wuzapi")(self.env)
        serialized = repr(adapter.get_capabilities(self.connection))

        self.assertNotIn(self.connection.wuzapi_api_token, serialized)
        self.assertNotIn(self.connection.wuzapi_hmac_secret, serialized)

    def test_agent_can_read_endpoint_but_cannot_read_secrets(self):
        agent = self.env["res.users"].create(
            {
                "name": "WuzAPI Agent",
                "login": "wuzapi-agent-%s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "company_ids": [(6, 0, self.env.company.ids)],
                "groups_id": [
                    (
                        6,
                        0,
                        [
                            self.env.ref(
                                "contact_center_base.group_contact_center_agent"
                            ).id
                        ],
                    )
                ],
            }
        )
        team = self.env["contact.center.team"].create(
            {
                "name": "WuzAPI Agent Scope %s" % uuid.uuid4(),
                "company_id": self.env.company.id,
                "agent_ids": [(6, 0, agent.ids)],
            }
        )
        self.account.write({"access_team_ids": [(6, 0, team.ids)]})

        endpoint = self.connection.with_user(agent).read(["wuzapi_base_url"])
        self.assertEqual(endpoint[0]["wuzapi_base_url"], "https://wuzapi.invalid")
        with self.assertRaises(AccessError):
            self.connection.with_user(agent).read(
                [
                    "wuzapi_api_token",
                    "wuzapi_hmac_secret",
                    "wuzapi_hmac_pending_secret",
                    "wuzapi_hmac_previous_secret",
                    "wuzapi_webhook_key",
                ]
            )
        with self.assertRaises(AccessError):
            self.connection.with_user(agent).read(["wuzapi_webhook_event_ids"])

    def test_connection_company_rule_also_isolates_wuzapi_configuration(self):
        other_company = self.env["res.company"].create({"name": "Other WuzAPI Co"})
        other_admin = self.env["res.users"].create(
            {
                "name": "Other WuzAPI Admin",
                "login": "wuzapi-admin-%s" % uuid.uuid4(),
                "company_id": other_company.id,
                "company_ids": [(6, 0, other_company.ids)],
                "groups_id": [
                    (
                        6,
                        0,
                        [
                            self.env.ref(
                                "contact_center_base.group_contact_center_admin"
                            ).id
                        ],
                    )
                ],
            }
        )

        visible = (
            self.env["contact.center.provider.connection"]
            .with_user(other_admin)
            .search_count([("id", "=", self.connection.id)])
        )
        self.assertEqual(visible, 0)

    def test_provider_selector_and_form_are_extended_by_addon(self):
        self.assertIn(("wuzapi", "WuzAPI"), adapter_registry.choices())

        view = self.env.ref(
            "contact_center_wuzapi.view_contact_center_connection_form_wuzapi"
        )
        self.assertEqual(
            view.inherit_id,
            self.env.ref("contact_center_base.view_contact_center_connection_form"),
        )
        arch = etree.fromstring(view.arch_db.encode())
        pages = arch.xpath("//page[@name='wuzapi_configuration']")
        self.assertEqual(len(pages), 1)
        self.assertIn("adapter_key", pages[0].get("attrs"))
        self.assertIn("wuzapi", pages[0].get("attrs"))
        self.assertEqual(
            len(pages[0].xpath(".//field[@name='wuzapi_webhook_event_ids']")), 1
        )
        self.assertEqual(
            len(pages[0].xpath(".//button[@name='action_wuzapi_apply_webhook']")),
            1,
        )
        self.assertEqual(
            len(pages[0].xpath(".//button[@name='action_wuzapi_refresh_webhook']")),
            1,
        )
        secret_field = pages[0].xpath(".//field[@name='wuzapi_hmac_secret']")
        self.assertEqual(len(secret_field), 1)
        self.assertIn("readonly", secret_field[0].get("attrs"))
        self.assertEqual(
            len(pages[0].xpath(".//button[@name='action_wuzapi_rotate_hmac']")),
            1,
        )

    def test_legacy_wuzapi_menu_is_absent_or_inactive(self):
        legacy_menu = self.env.ref(
            "contact_center_wuzapi.menu_contact_center_wuzapi_config",
            raise_if_not_found=False,
        )
        self.assertTrue(not legacy_menu or not legacy_menu.active)
