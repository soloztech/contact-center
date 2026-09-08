import datetime
import hashlib
import hmac
import io
import json
import os
import uuid
from unittest import mock

from psycopg2 import errorcodes
from psycopg2.errors import SerializationFailure

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase
from odoo.tools import mute_logger

from ..controllers import webhook as webhook_controller
from ..controllers.webhook import (
    MAX_WEBHOOK_BODY_BYTES,
    _read_bounded_body,
    sanitize_webhook_envelope,
)


@tagged("post_install", "-at_install")
class TestWuzapiWebhook(HttpCase):
    HMAC_SECRET = "wuzapi-webhook-test-secret-with-32-chars"

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
                    "name": "WuzAPI Webhook Agent",
                    "login": "wuzapi-webhook-agent-%s" % uuid.uuid4(),
                    "email": "wuzapi-webhook-agent@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, [agent_group.id, admin_group.id])],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "WuzAPI Webhook Team %s" % uuid.uuid4(),
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "WuzAPI webhook account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "access_team_ids": [(6, 0, cls.team.ids)],
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "WuzAPI webhook connection",
                "account_id": cls.account.id,
                "adapter_key": "wuzapi",
                "wuzapi_base_url": "https://wuzapi.invalid",
                "wuzapi_api_token": "not-a-real-api-token",
                "wuzapi_hmac_secret": cls.HMAC_SECRET,
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
            }
        )
        cls.inactive_account = cls.env["contact.center.account"].create(
            {
                "name": "Archived WuzAPI webhook account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "access_team_ids": [(6, 0, cls.team.ids)],
                "active": False,
            }
        )
        cls.inactive_connection = (
            cls.env["contact.center.provider.connection"]
            .with_context(active_test=False)
            .create(
                {
                    "name": "Archived WuzAPI webhook connection",
                    "account_id": cls.inactive_account.id,
                    "adapter_key": "wuzapi",
                    "wuzapi_base_url": "https://wuzapi.invalid",
                    "wuzapi_api_token": "archived-account-api-token",
                    "wuzapi_hmac_secret": cls.HMAC_SECRET,
                    "active": False,
                    "role": "historical",
                    "inbound_active": False,
                    "outbound_active": False,
                }
            )
        )
        cls.webhook_path = "/contact-center/webhook/wuzapi/%s" % (
            cls.connection.wuzapi_webhook_key
        )

    def _signature(self, body):
        return hmac.new(
            self.HMAC_SECRET.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()

    def _post(self, body, signature=None):
        if signature is None:
            signature = self._signature(body)
        headers = {
            "Content-Type": "application/json",
            "X-Hmac-Signature": signature,
        }
        if not body:
            return self.opener.post(
                self.base_url() + self.webhook_path,
                data=body,
                headers=headers,
                timeout=12,
            )
        return self.url_open(self.webhook_path, data=body, headers=headers)

    def _connection_inboxes(self):
        self.env.invalidate_all()
        return (
            self.env["contact.center.inbox.event"]
            .sudo()
            .search([("provider_connection_id", "=", self.connection.id)])
        )

    @classmethod
    def _load_fixture(cls, filename):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", filename)
        with open(fixture_path, encoding="utf-8") as fixture_file:
            return json.load(fixture_file)

    def _inbox_job_count(self):
        self.env.invalidate_all()
        return (
            self.env["queue.job"]
            .sudo()
            .search_count([("model_name", "=", "contact.center.inbox.event")])
        )

    def test_bounded_body_reader_supports_odoo16_werkzeug(self):
        class FakeRequest:
            content_length = None
            environ = {"wsgi.input": io.BytesIO(b"123456")}

        request = FakeRequest()
        self.assertEqual(_read_bounded_body(request, 4), b"12345")
        self.assertEqual(_read_bounded_body(request, 4), b"12345")

        class CachedRequest:
            _cached_data = b"abcdef"

        self.assertEqual(_read_bounded_body(CachedRequest(), 4), b"abcde")

    def test_serialization_retry_preserves_signed_body_and_deduplication(self):
        class ConcurrentUpdate(SerializationFailure):
            pgcode = errorcodes.SERIALIZATION_FAILURE

        body = json.dumps(
            {"type": "Message", "event": {"fixture": "transaction-retry"}},
            separators=(",", ":"),
        ).encode()
        original_lock = webhook_controller._locked_ingress_response
        attempts = []
        connection_id = self.connection.id

        def fail_once(connection, adapter, headers, signed_body):
            attempts.append(signed_body)
            if len(attempts) == 1:
                raise ConcurrentUpdate("synthetic ingress admission conflict")
            return original_lock(connection, adapter, headers, signed_body)

        with mock.patch.object(
            webhook_controller, "_locked_ingress_response", new=fail_once
        ):
            response = self._post(body)

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(attempts, [body, body])
        self.assertFalse(response.json()["duplicate"])
        replay = self._post(body)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["duplicate"])
        self.assertEqual(response.json()["event_id"], replay.json()["event_id"])
        with self.registry.cursor() as cr:
            cr.execute(
                """
                SELECT count(*)
                  FROM contact_center_inbox_event
                 WHERE provider_connection_id = %s
                """,
                [connection_id],
            )
            self.assertEqual(cr.fetchone()[0], 1)

    def test_unique_collision_retries_http_and_acknowledges_existing_inbox(self):
        body = json.dumps(
            {"type": "Message", "event": {"fixture": "concurrent-duplicate"}}
        ).encode()
        first = self._post(body)
        self.assertEqual(first.status_code, 202, first.text)
        inbox_class = type(self.env["contact.center.inbox.event"])
        original_search = inbox_class.search
        dedupe_domain = [
            ("provider_connection_id", "=", self.connection.id),
            (
                "inbox_dedupe_key",
                "=",
                "wuzapi:json:sha256:%s" % hashlib.sha256(body).hexdigest(),
            ),
        ]
        hidden_lookups = []

        def stale_search(recordset, domain, *args, **kwargs):
            if domain == dedupe_domain and body_reader.call_count == 1:
                # Keep the winner invisible for the entire first transaction;
                # the following real INSERT must hit PostgreSQL's constraint.
                hidden_lookups.append(True)
                return recordset.browse()
            return original_search(recordset, domain, *args, **kwargs)

        with mock.patch.object(
            webhook_controller,
            "_read_bounded_body",
            wraps=webhook_controller._read_bounded_body,
        ) as body_reader, mock.patch.object(
            inbox_class, "search", new=stale_search
        ), mute_logger(
            "odoo.sql_db"
        ):
            response = self._post(body)

        self.assertEqual(hidden_lookups, [True])
        self.assertEqual(body_reader.call_count, 2)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["duplicate"])
        self.assertEqual(response.json()["event_id"], first.json()["event_id"])
        self.assertEqual(len(self._connection_inboxes()), 1)

    def test_signed_webhook_is_accepted_and_replay_is_idempotent(self):
        body = json.dumps(
            {
                "type": "Message",
                "instanceName": "Lucas",
                "userID": "lucas-test",
                "event": {"fixture": True},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        self.account.invalidate_recordset(["access_topology_revision"])
        topology_revision = self.account.access_topology_revision

        response = self._post(body)

        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertTrue(payload["accepted"])
        self.assertFalse(payload["duplicate"])
        self.assertFalse(payload["blocked"])
        inboxes = self._connection_inboxes()
        self.assertEqual(len(inboxes), 1)
        inbox = inboxes.ensure_one()
        self.assertEqual(payload["event_id"], inbox.id)
        self.assertTrue(inbox.queue_job_uuid)
        job_domain = [("identity_key", "=", "contact_center:inbox:%s" % inbox.id)]
        jobs = self.env["queue.job"].sudo().search(job_domain)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs.uuid, inbox.queue_job_uuid)
        self.account.invalidate_recordset(["access_topology_revision"])
        self.assertEqual(
            self.account.access_topology_revision,
            topology_revision,
            "ordinary webhooks must not rewrite the access-topology fence",
        )

        replay = self._post(body)

        self.assertEqual(replay.status_code, 200, replay.text)
        replay_payload = replay.json()
        self.assertTrue(replay_payload["accepted"])
        self.assertTrue(replay_payload["duplicate"])
        self.assertFalse(replay_payload["blocked"])
        self.assertEqual(replay_payload["event_id"], inbox.id)
        inboxes = self._connection_inboxes()
        self.assertEqual(len(inboxes), 1)
        self.assertEqual(inboxes.queue_job_uuid, inbox.queue_job_uuid)
        self.assertEqual(
            self.env["queue.job"].sudo().search_count(job_domain),
            1,
        )
        self.account.invalidate_recordset(["access_topology_revision"])
        self.assertEqual(self.account.access_topology_revision, topology_revision)

    def test_protocol_only_then_first_group_image_bootstraps_once(self):
        self.account.write(
            {
                "technical_author_id": self.agent.partner_id.id,
                "group_inbound_enabled": True,
            }
        )
        protocol_envelope = self._load_fixture(
            "message_group_from_me_first_image_lid_protocol_only.json"
        )
        message_envelope = self._load_fixture(
            "message_group_from_me_first_image_lid.json"
        )
        protocol_body = json.dumps(
            protocol_envelope, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        message_body = json.dumps(
            message_envelope, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        message_id = message_envelope["event"]["Info"]["ID"]
        group_jid = message_envelope["event"]["Info"]["Chat"]

        self.assertEqual(protocol_envelope["event"]["Info"]["ID"], message_id)
        self.assertNotEqual(protocol_body, message_body)
        self.assertFalse(
            self.env["contact.center.channel.binding"]
            .sudo()
            .search_count(
                [
                    ("account_id", "=", self.account.id),
                    ("conversation_ref", "=", group_jid),
                ]
            )
        )

        protocol_response = self._post(protocol_body)

        self.assertEqual(protocol_response.status_code, 202, protocol_response.text)
        protocol_inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .browse(protocol_response.json()["event_id"])
        )
        self.assertNotIn(
            "senderKeyDistributionMessage",
            protocol_inbox.raw_envelope_json["event"]["Message"],
        )
        self.assertTrue(
            protocol_inbox.with_context(
                job_uuid=protocol_inbox.queue_job_uuid
            )._job_process()
        )
        protocol_inbox.invalidate_recordset(
            ["state", "last_error_class", "last_error_message"]
        )
        self.assertEqual(protocol_inbox.state, "unsupported")
        self.assertEqual(protocol_inbox.last_error_class, "UnsupportedEventError")
        self.assertIn("protocol-only", protocol_inbox.last_error_message)
        self.assertFalse(
            self.env["contact.center.channel.binding"]
            .sudo()
            .search_count(
                [
                    ("account_id", "=", self.account.id),
                    ("conversation_ref", "=", group_jid),
                ]
            )
        )

        message_response = self._post(message_body)

        self.assertEqual(message_response.status_code, 202, message_response.text)
        self.assertNotEqual(message_response.json()["event_id"], protocol_inbox.id)
        message_inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .browse(message_response.json()["event_id"])
        )
        self.assertEqual(
            message_inbox.raw_envelope_json["event"]["Message"]["imageMessage"][
                "mediaKey"
            ],
            "aWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWk=",
        )
        self.assertTrue(
            message_inbox.with_context(
                job_uuid=message_inbox.queue_job_uuid
            )._job_process()
        )
        message_inbox.invalidate_recordset(["state", "normalized_dto_json"])
        self.assertEqual(message_inbox.state, "done")
        self.assertEqual(
            message_inbox.normalized_dto_json["message"]["protocol_snapshot"]["source"][
                "addressing_mode"
            ],
            "lid",
        )

        channel_binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    ("conversation_ref", "=", group_jid),
                ]
            )
            .ensure_one()
        )
        self.assertEqual(channel_binding.conversation_type, "group")
        self.assertFalse(channel_binding.identity_id)
        self.assertEqual(len(channel_binding.group_profile_ids), 1)
        message_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("provider_connection_id", "=", self.connection.id),
                    ("external_message_id", "=", message_id),
                ]
            )
            .ensure_one()
        )
        self.assertEqual(message_binding.channel_binding_id, channel_binding)
        self.assertEqual(message_binding.source_inbox_event_id, message_inbox)
        self.assertEqual(message_binding.direction, "outbound")
        self.assertEqual(message_binding.origin, "external_device")
        self.assertEqual(message_binding.content_type, "image")
        self.assertEqual(message_binding.delivery_state, "sent")
        self.assertEqual(
            message_binding.protocol_participant_json["value_normalized"],
            "700000000000888@lid",
        )
        media = message_binding.media_ids.ensure_one()
        self.assertEqual(media.kind, "image")
        self.assertEqual(media.mime_type, "image/jpeg")
        self.assertEqual(media.size_bytes, 4096)
        self.assertEqual(
            media.remote_locator_json["direct_path"],
            "/dummy-first-group-image.enc",
        )
        self.assertTrue(media.remote_locator_json["media_key"])
        self.assertTrue(media.queue_job_uuid)
        self.assertFalse(
            self.env["contact.center.outbox.command"]
            .sudo()
            .search_count([("message_binding_id", "=", message_binding.id)])
        )

        protocol_replay = self._post(protocol_body)
        message_replay = self._post(message_body)

        self.assertEqual(protocol_replay.status_code, 200, protocol_replay.text)
        self.assertEqual(protocol_replay.json()["event_id"], protocol_inbox.id)
        self.assertTrue(protocol_replay.json()["duplicate"])
        self.assertEqual(message_replay.status_code, 200, message_replay.text)
        self.assertEqual(message_replay.json()["event_id"], message_inbox.id)
        self.assertTrue(message_replay.json()["duplicate"])
        self.assertEqual(len(self._connection_inboxes()), 2)
        self.assertFalse(message_inbox._job_process())
        self.assertEqual(
            self.env["contact.center.message.binding"]
            .sudo()
            .search_count(
                [
                    ("provider_connection_id", "=", self.connection.id),
                    ("external_message_id", "=", message_id),
                ]
            ),
            1,
        )

    def test_first_group_image_then_protocol_only_companion_is_harmless(self):
        self.account.write(
            {
                "technical_author_id": self.agent.partner_id.id,
                "group_inbound_enabled": True,
            }
        )
        message_envelope = self._load_fixture(
            "message_group_from_me_first_image_lid.json"
        )
        protocol_envelope = self._load_fixture(
            "message_group_from_me_first_image_lid_protocol_only.json"
        )
        message_body = json.dumps(
            message_envelope, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        protocol_body = json.dumps(
            protocol_envelope, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        message_id = message_envelope["event"]["Info"]["ID"]

        message_response = self._post(message_body)
        self.assertEqual(message_response.status_code, 202, message_response.text)
        message_inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .browse(message_response.json()["event_id"])
        )
        self.assertTrue(
            message_inbox.with_context(
                job_uuid=message_inbox.queue_job_uuid
            )._job_process()
        )
        message_inbox.invalidate_recordset(["state"])
        self.assertEqual(message_inbox.state, "done")
        protocol_response = self._post(protocol_body)
        self.assertEqual(protocol_response.status_code, 202, protocol_response.text)
        protocol_inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .browse(protocol_response.json()["event_id"])
        )
        self.assertTrue(
            protocol_inbox.with_context(
                job_uuid=protocol_inbox.queue_job_uuid
            )._job_process()
        )
        protocol_inbox.invalidate_recordset(["state"])
        self.assertEqual(protocol_inbox.state, "unsupported")

        self.assertTrue(self._post(message_body).json()["duplicate"])
        self.assertTrue(self._post(protocol_body).json()["duplicate"])
        self.assertEqual(len(self._connection_inboxes()), 2)
        message_bindings = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("provider_connection_id", "=", self.connection.id),
                    ("external_message_id", "=", message_id),
                ]
            )
        )
        self.assertEqual(len(message_bindings), 1)
        self.assertEqual(len(message_bindings.media_ids), 1)
        self.assertEqual(len(message_bindings.channel_binding_id.group_profile_ids), 1)

    def test_webhook_accepts_owner_only_inbox(self):
        self.account.write(
            {
                "access_user_ids": [(6, 0, self.agent.ids)],
                "access_team_ids": [(6, 0, [])],
            }
        )
        body = json.dumps(
            {
                "type": "Message",
                "instanceName": "Owner-only instance",
                "userID": "owner-only-user",
                "event": {"fixture": True},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        response = self._post(body)

        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()["accepted"])
        self.assertFalse(response.json()["blocked"])
        inbox = self._connection_inboxes().ensure_one()
        self.assertEqual(inbox.state, "pending")
        self.assertTrue(inbox.queue_job_uuid)

    def test_webhook_without_access_scope_is_persisted_blocked_and_replayable(self):
        # The ORM invariant no longer permits an active primary route to lose
        # its last attendant.  Reach the unscoped state through the supported
        # standby transition, then simulate a corrupt primary row so the
        # public ingress boundary keeps its defense-in-depth coverage.
        self.connection.action_set_standby()
        self.account.access_team_ids = [(5, 0, 0)]
        self.connection.flush_recordset(["role", "inbound_active", "outbound_active"])
        self.env.cr.execute(
            """
            UPDATE contact_center_provider_connection
               SET role = 'primary',
                   inbound_active = TRUE,
                   outbound_active = FALSE
             WHERE id = %s
            """,
            [self.connection.id],
        )
        self.connection.invalidate_recordset(
            ["role", "inbound_active", "outbound_active"]
        )
        body = json.dumps(
            {
                "type": "Message",
                "instanceName": "Unassigned instance",
                "userID": "unassigned-user",
                "event": {"fixture": True},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        initial_job_count = self._inbox_job_count()

        first = self._post(body)
        replay = self._post(body)

        self.assertEqual(first.status_code, 202, first.text)
        self.assertTrue(first.json()["accepted"])
        self.assertTrue(first.json()["blocked"])
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["duplicate"])
        self.assertTrue(replay.json()["blocked"])
        inbox = self._connection_inboxes().ensure_one()
        self.assertEqual(inbox.state, "blocked")
        self.assertEqual(inbox.last_error_class, "AccountNotReady")
        self.assertEqual(inbox.metadata_json["blocked_reason"], "account_not_ready")
        self.assertFalse(inbox.queue_job_uuid)
        self.assertEqual(self._inbox_job_count(), initial_job_count)

        self.account.access_team_ids = self.team
        self.assertTrue(inbox.with_user(self.agent).action_requeue())
        inbox.invalidate_recordset(["state", "queue_job_uuid"])
        self.assertEqual(inbox.state, "pending")
        self.assertTrue(inbox.queue_job_uuid)

    def test_signed_webhook_for_archived_account_is_acknowledged_without_work(self):
        body = json.dumps(
            {
                "type": "Connected",
                "instanceName": "Archived instance",
                "userID": "archived-user",
                "event": {},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        path = "/contact-center/webhook/wuzapi/%s" % (
            self.inactive_connection.wuzapi_webhook_key
        )

        response = self.url_open(
            path,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hmac-Signature": self._signature(body),
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"accepted": True, "ignored": "inactive"})
        self.env.invalidate_all()
        self.assertFalse(
            self.env["contact.center.inbox.event"]
            .sudo()
            .search_count(
                [("provider_connection_id", "=", self.inactive_connection.id)]
            )
        )

    def test_signed_webhook_from_standby_is_acknowledged_without_work(self):
        standby = self.env["contact.center.provider.connection"].create(
            {
                "name": "WuzAPI standby webhook connection",
                "account_id": self.account.id,
                "adapter_key": "wuzapi",
                "wuzapi_base_url": "https://wuzapi.invalid",
                "wuzapi_api_token": "standby-api-token",
                "wuzapi_hmac_secret": self.HMAC_SECRET,
                "active": True,
                "role": "standby",
                "inbound_active": False,
                "outbound_active": False,
            }
        )
        body = json.dumps(
            {
                "type": "Message",
                "instanceName": "Standby instance",
                "userID": "standby-user",
                "event": {"fixture": True},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        response = self.url_open(
            "/contact-center/webhook/wuzapi/%s" % standby.wuzapi_webhook_key,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hmac-Signature": self._signature(body),
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {"accepted": True, "ignored": "inactive_transport"},
        )
        self.env.invalidate_all()
        self.assertFalse(
            self.env["contact.center.inbox.event"]
            .sudo()
            .search_count([("provider_connection_id", "=", standby.id)])
        )

    def test_first_paired_inbox_buffers_callbacks_until_activation(self):
        account = self.env["contact.center.account"].create(
            {
                "name": "Guided WuzAPI webhook account",
                "company_id": self.env.company.id,
                "platform": "whatsapp",
                "access_team_ids": [(6, 0, self.team.ids)],
            }
        )
        staged = self.env["contact.center.provider.connection"].create(
            {
                "name": "Guided WuzAPI staged connection",
                "account_id": account.id,
                "adapter_key": "wuzapi",
                "active": True,
                "role": "migration",
                "inbound_active": False,
                "outbound_active": False,
                "onboarding_ref": str(uuid.uuid4()),
                # Remote webhook/HMAC may already be live even if a later setup
                # observation failed. First-cutover buffering must not depend on
                # this transient state.
                "wuzapi_onboarding_state": "error",
                "wuzapi_base_url": "https://wuzapi.invalid",
                "wuzapi_api_token": "guided-staged-token",
                "wuzapi_hmac_secret": self.HMAC_SECRET,
            }
        )
        body = json.dumps(
            {
                "type": "Message",
                "instanceName": "Guided instance",
                "userID": "guided-user",
                "event": {"fixture": True},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        response = self.url_open(
            "/contact-center/webhook/wuzapi/%s" % staged.wuzapi_webhook_key,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hmac-Signature": self._signature(body),
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()["blocked"])
        event = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .browse(response.json()["event_id"])
        )
        self.assertEqual(event.state, "blocked")
        self.assertFalse(event.queue_job_uuid)
        self.assertEqual(
            event.metadata_json["blocked_reason"],
            "onboarding_not_activated",
        )
        self.assertEqual(event.metadata_json["onboarding_ref"], staged.onboarding_ref)
        staged.invalidate_recordset(["onboarding_ingress_revision"])
        self.assertEqual(staged.onboarding_ingress_revision, 1)
        with self.assertRaises(ValidationError):
            event.with_user(self.agent).action_requeue()

    def test_lifecycle_retries_dedupe_but_new_state_cycle_is_persisted(self):
        def body(event_type):
            return json.dumps(
                {
                    "type": event_type,
                    "instanceName": "WuzAPI test instance",
                    "userID": "wuzapi-test-user",
                    "event": {},
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")

        connected_body = body("Connected")
        first_connected = self._post(connected_body)
        connected_retry = self._post(connected_body)
        disconnected_body = body("Disconnected")
        disconnected = self._post(disconnected_body)
        disconnected_retry = self._post(disconnected_body)
        second_connected = self._post(connected_body)
        second_connected_retry = self._post(connected_body)

        self.assertEqual(first_connected.status_code, 202, first_connected.text)
        self.assertEqual(connected_retry.status_code, 200, connected_retry.text)
        self.assertTrue(connected_retry.json()["duplicate"])
        self.assertEqual(
            connected_retry.json()["event_id"], first_connected.json()["event_id"]
        )
        self.assertEqual(disconnected.status_code, 202, disconnected.text)
        self.assertEqual(disconnected_retry.status_code, 200, disconnected_retry.text)
        self.assertEqual(
            disconnected_retry.json()["event_id"], disconnected.json()["event_id"]
        )
        self.assertEqual(second_connected.status_code, 202, second_connected.text)
        self.assertNotEqual(
            second_connected.json()["event_id"], first_connected.json()["event_id"]
        )
        self.assertEqual(
            second_connected_retry.json()["event_id"],
            second_connected.json()["event_id"],
        )

        inboxes = self._connection_inboxes()
        self.assertEqual(len(inboxes), 3)
        self.assertEqual(
            [inbox.metadata_json["event_type"] for inbox in inboxes],
            ["Connected", "Disconnected", "Connected"],
        )
        self.assertTrue(
            all(
                inbox.inbox_dedupe_key.startswith("wuzapi:lifecycle:")
                for inbox in inboxes
            )
        )

    def test_health_change_opens_new_completed_lifecycle_cycle(self):
        body = json.dumps(
            {
                "type": "Connected",
                "instanceName": "WuzAPI test instance",
                "userID": "wuzapi-test-user",
                "event": {},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        first = self._post(body)
        self.assertEqual(first.status_code, 202, first.text)
        inbox = self._connection_inboxes().ensure_one()

        for active_state in ("pending", "processing", "retry"):
            inbox.state = active_state
            retry = self._post(body)
            self.assertEqual(retry.status_code, 200, retry.text)
            self.assertEqual(retry.json()["event_id"], inbox.id)

        inbox._process_one()
        inbox.invalidate_recordset(["state", "processed_at"])
        self.assertEqual(inbox.state, "done")
        health_observed_at = inbox.create_date + datetime.timedelta(seconds=2)
        self.connection._apply_health_result(
            {"state": "disconnected", "detail": "unavailable"},
            source="health_job",
            observed_at=health_observed_at,
        )
        self.connection.invalidate_recordset(
            [
                "state",
                "last_state_source",
                "last_state_observed_at",
                "last_health_state_inbox_event_id",
            ]
        )
        self.assertEqual(self.connection.state, "disconnected")
        self.assertEqual(self.connection.last_state_source, "health_job")
        self.assertEqual(self.connection.last_health_state_inbox_event_id, inbox.id)

        second = self._post(body)
        second_retry = self._post(body)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertNotEqual(second.json()["event_id"], inbox.id)
        self.assertEqual(second_retry.status_code, 200, second_retry.text)
        self.assertEqual(second_retry.json()["event_id"], second.json()["event_id"])
        second_inbox = self.env["contact.center.inbox.event"].browse(
            second.json()["event_id"]
        )
        self.assertIn(":health:", second_inbox.inbox_dedupe_key)

    def test_same_second_health_change_opens_new_lifecycle_cycle(self):
        body = json.dumps(
            {
                "type": "Connected",
                "instanceName": "WuzAPI test instance",
                "userID": "wuzapi-test-user",
                "event": {},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        first = self._post(body)
        self.assertEqual(first.status_code, 202, first.text)
        inbox = self._connection_inboxes().ensure_one()
        inbox._process_one()
        inbox.invalidate_recordset(["state", "processed_at"])
        self.assertEqual(inbox.state, "done")

        self.connection._apply_health_result(
            {"state": "disconnected", "detail": "unavailable"},
            source="health_job",
            observed_at=inbox.create_date,
        )
        self.connection.invalidate_recordset(
            [
                "state",
                "last_state_source",
                "last_state_observed_at",
                "last_health_state_inbox_event_id",
            ]
        )
        self.assertEqual(self.connection.state, "disconnected")
        self.assertEqual(self.connection.last_state_source, "health_job")
        self.assertEqual(self.connection.last_health_state_inbox_event_id, inbox.id)

        second = self._post(body)
        retry = self._post(body)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertNotEqual(second.json()["event_id"], inbox.id)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["event_id"], second.json()["event_id"])

    def test_health_before_lifecycle_processing_does_not_open_retry_cycle(self):
        body = json.dumps(
            {
                "type": "Connected",
                "instanceName": "WuzAPI test instance",
                "userID": "wuzapi-test-user",
                "event": {},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        first = self._post(body)
        self.assertEqual(first.status_code, 202, first.text)
        inbox = self._connection_inboxes().ensure_one()

        # Health is accepted after persistence but before this lifecycle inbox
        # crosses the application boundary, all within one Odoo second.
        self.connection._apply_health_result(
            {"state": "disconnected", "detail": "unavailable"},
            source="health_job",
            observed_at=inbox.create_date,
        )
        self.connection.invalidate_recordset(["last_health_state_inbox_event_id"])
        self.assertLess(self.connection.last_health_state_inbox_event_id, inbox.id)

        inbox._process_one()
        inbox.invalidate_recordset(["state", "processed_at"])
        self.assertEqual(inbox.state, "done")
        self.connection.invalidate_recordset(
            ["state", "last_state_source", "last_state_inbox_event_id"]
        )
        self.assertEqual(self.connection.state, "disconnected")
        self.assertEqual(self.connection.last_state_source, "health_job")
        self.assertEqual(self.connection.last_state_inbox_event_id, inbox.id)

        retry = self._post(body)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["event_id"], inbox.id)
        self.assertEqual(len(self._connection_inboxes()), 1)

    def test_invalid_signature_does_not_persist(self):
        body = b'{"type":"Message","event":{"fixture":true}}'
        initial_job_count = self._inbox_job_count()

        response = self._post(body, signature="0" * 64)

        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json(), {"error": "invalid_signature"})
        self.assertFalse(self._connection_inboxes())
        self.assertEqual(self._inbox_job_count(), initial_job_count)

    def test_secret_rotated_after_first_auth_rejects_old_signature(self):
        body = json.dumps(
            {
                "type": "Message",
                "instanceName": "Rotating secret",
                "userID": "rotating-secret-user",
                "event": {"fixture": True},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        rotated_secret = "rotated-wuzapi-webhook-secret-with-32-chars"
        model_class = type(self.connection)
        original_lock = model_class._contact_center_lock_ingress_admission
        rotated = []

        def rotate_then_lock(recordset):
            if not rotated:
                target = (
                    recordset.sudo()
                    .with_context(active_test=False)
                    .search([("id", "=", self.connection.id)])
                )
                target._wuzapi_hmac_internal().write(
                    {"wuzapi_hmac_secret": rotated_secret}
                )
                rotated.append(True)
            return original_lock(recordset)

        try:
            with mock.patch.object(
                model_class,
                "_contact_center_lock_ingress_admission",
                new=rotate_then_lock,
            ):
                response = self._post(body)

            self.assertEqual(response.status_code, 401, response.text)
            self.assertEqual(response.json(), {"error": "invalid_signature"})
            self.assertTrue(rotated)
            self.assertFalse(self._connection_inboxes())
        finally:
            self.env.invalidate_all()
            self.connection._wuzapi_hmac_internal().write(
                {"wuzapi_hmac_secret": self.HMAC_SECRET}
            )

    def test_coordinated_rotation_drains_old_callback_waiting_for_lock(self):
        body = json.dumps(
            {
                "type": "Message",
                "instanceName": "Draining old signature",
                "userID": "draining-old-signature-user",
                "event": {"fixture": True},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        rotated_secret = "rotated-draining-wuzapi-secret-at-least-32-chars"
        model_class = type(self.connection)
        original_lock = model_class._contact_center_lock_ingress_admission
        rotated = []

        def promote_then_lock(recordset):
            if not rotated:
                target = (
                    recordset.sudo()
                    .with_context(active_test=False)
                    .search([("id", "=", self.connection.id)])
                )
                target._wuzapi_hmac_internal().write(
                    {
                        "wuzapi_hmac_secret": rotated_secret,
                        "wuzapi_hmac_previous_secret": self.HMAC_SECRET,
                        "wuzapi_hmac_previous_valid_until": (
                            fields.Datetime.now() + datetime.timedelta(minutes=5)
                        ),
                    }
                )
                rotated.append(True)
            return original_lock(recordset)

        try:
            with mock.patch.object(
                model_class,
                "_contact_center_lock_ingress_admission",
                new=promote_then_lock,
            ):
                response = self._post(body)

            self.assertEqual(response.status_code, 202, response.text)
            self.assertTrue(rotated)
            self.assertEqual(len(self._connection_inboxes()), 1)
        finally:
            self.env.invalidate_all()
            self.connection._wuzapi_hmac_internal().write(
                {
                    "wuzapi_hmac_secret": self.HMAC_SECRET,
                    "wuzapi_hmac_previous_secret": False,
                    "wuzapi_hmac_previous_valid_until": False,
                }
            )

    def test_oversized_body_does_not_persist(self):
        body = b"x" * (MAX_WEBHOOK_BODY_BYTES + 1)
        initial_job_count = self._inbox_job_count()

        response = self._post(body)

        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json(), {"error": "payload_too_large"})
        self.assertFalse(self._connection_inboxes())
        self.assertEqual(self._inbox_job_count(), initial_job_count)

    def test_invalid_json_does_not_persist(self):
        body = b'{"type":"Message","event":'
        initial_job_count = self._inbox_job_count()

        response = self._post(body)

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json(), {"error": "invalid_json"})
        self.assertFalse(self._connection_inboxes())
        self.assertEqual(self._inbox_job_count(), initial_job_count)

    def test_nonstandard_and_parser_recursive_json_do_not_persist(self):
        initial_job_count = self._inbox_job_count()

        nonstandard = self._post(b'{"type":"Message","event":{"value":NaN}}')
        self.assertEqual(nonstandard.status_code, 400, nonstandard.text)
        self.assertEqual(nonstandard.json(), {"error": "invalid_json"})

        recursive_body = (
            b'{"type":"Message","event":'
            + (b'{"nested":' * 12000)
            + b"null"
            + (b"}" * 12000)
            + b"}"
        )
        recursive = self._post(recursive_body)
        self.assertEqual(recursive.status_code, 400, recursive.text)
        self.assertEqual(recursive.json(), {"error": "invalid_json"})

        self.assertFalse(self._connection_inboxes())
        self.assertEqual(self._inbox_job_count(), initial_job_count)

    def test_empty_non_object_deep_and_wrong_content_type_are_rejected(self):
        initial_job_count = self._inbox_job_count()

        empty = self._post(b"")
        self.assertEqual(empty.status_code, 400, empty.text)
        self.assertEqual(empty.json(), {"error": "empty_payload"})

        non_object_body = b"[]"
        non_object = self._post(non_object_body)
        self.assertEqual(non_object.status_code, 400, non_object.text)
        self.assertEqual(non_object.json(), {"error": "invalid_envelope"})

        nested = "value"
        for _index in range(34):
            nested = {"nested": nested}
        deep_body = json.dumps(nested).encode("utf-8")
        too_deep = self._post(deep_body)
        self.assertEqual(too_deep.status_code, 400, too_deep.text)
        self.assertEqual(too_deep.json(), {"error": "invalid_envelope"})

        wrong_type_body = b"{}"
        wrong_type = self.url_open(
            self.webhook_path,
            data=wrong_type_body,
            headers={
                "Content-Type": "text/plain",
                "X-Hmac-Signature": self._signature(wrong_type_body),
            },
        )
        self.assertEqual(wrong_type.status_code, 415, wrong_type.text)
        self.assertEqual(wrong_type.json(), {"error": "unsupported_media_type"})

        missing = self.url_open(
            "/contact-center/webhook/wuzapi/not-a-real-webhook-key",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(missing.json(), {"error": "not_found"})

        self.assertFalse(self._connection_inboxes())
        self.assertEqual(self._inbox_job_count(), initial_job_count)

    def test_sanitizer_keeps_only_the_media_key_required_for_download(self):
        envelope = {
            "type": "Message",
            "base64": "inline-binary",
            "event": {
                "Info": {"ID": "MEDIA-1"},
                "Message": {
                    "imageMessage": {
                        "mediaKey": "required-download-key",
                        "JPEGThumbnail": "thumbnail",
                        "streamingSidecar": "stream-crypto-material",
                        "scansSidecar": "scan-crypto-material",
                        "inlineData": "data:text/plain,inline-content",
                        "nested": {"mediaKey": "redundant-key"},
                    }
                },
                "RawMessage": {"mediaKey": "raw-secret"},
                "SourceWebMsg": {"thumbnailSHA256": "raw-thumbnail"},
            },
        }

        sanitized = sanitize_webhook_envelope(envelope)

        serialized = json.dumps(sanitized, sort_keys=True)
        self.assertNotIn("inline-binary", serialized)
        self.assertNotIn("thumbnail", serialized.lower())
        self.assertNotIn("raw-secret", serialized)
        self.assertNotIn("redundant-key", serialized)
        self.assertNotIn("sidecar", serialized.lower())
        self.assertNotIn("inline-content", serialized)
        self.assertEqual(
            sanitized["event"]["Message"]["imageMessage"]["mediaKey"],
            "required-download-key",
        )

    def test_sanitizer_keeps_only_the_media_key_selected_by_the_adapter(self):
        multiple_media = {
            "type": "Message",
            "event": {
                "Message": {
                    "imageMessage": {
                        "mediaKey": "selected-image-key",
                        "media_key": "ignored-duplicate-key",
                    },
                    "audioMessage": {"mediaKey": "ignored-audio-key"},
                    "stickerMessage": {"mediaKey": "ignored-sticker-key"},
                }
            },
        }
        multiple_wrappers = {
            "type": "Message",
            "event": {
                "Message": {
                    # Adapter wrapper priority, rather than JSON insertion
                    # order, selects associatedChildMessage first.
                    "ephemeralMessage": {
                        "message": {"imageMessage": {"mediaKey": "ignored-wrapper-key"}}
                    },
                    "associatedChildMessage": {
                        "message": {
                            "videoMessage": {"mediaKey": "selected-wrapper-key"}
                        }
                    },
                }
            },
        }
        malformed_list_wrapper = {
            "type": "Message",
            "event": {
                "Message": {
                    "ephemeralMessage": [
                        {"message": {"imageMessage": {"mediaKey": "ignored-list-key"}}}
                    ],
                    "audioMessage": {"mediaKey": "selected-top-level-key"},
                }
            },
        }

        sanitized_media = sanitize_webhook_envelope(multiple_media)
        sanitized_wrappers = sanitize_webhook_envelope(multiple_wrappers)
        sanitized_list = sanitize_webhook_envelope(malformed_list_wrapper)

        image = sanitized_media["event"]["Message"]["imageMessage"]
        self.assertEqual(image["mediaKey"], "selected-image-key")
        self.assertNotIn("media_key", image)
        self.assertNotIn("ignored-audio-key", json.dumps(sanitized_media))
        self.assertNotIn("ignored-sticker-key", json.dumps(sanitized_media))

        wrappers = sanitized_wrappers["event"]["Message"]
        selected_video = wrappers["associatedChildMessage"]["message"]["videoMessage"]
        self.assertEqual(selected_video["mediaKey"], "selected-wrapper-key")
        self.assertNotIn("ignored-wrapper-key", json.dumps(sanitized_wrappers))

        list_message = sanitized_list["event"]["Message"]
        self.assertEqual(
            list_message["audioMessage"]["mediaKey"], "selected-top-level-key"
        )
        self.assertNotIn("ignored-list-key", json.dumps(sanitized_list))

    def test_sanitizer_drops_media_keys_when_text_wins_content_selection(self):
        envelopes = (
            {
                "type": "Message",
                "event": {
                    "Message": {
                        "conversation": "primary text",
                        "imageMessage": {"mediaKey": "ignored-conversation-key"},
                    }
                },
            },
            {
                "type": "Message",
                "event": {
                    "Message": {
                        "extendedTextMessage": {"text": "primary extended text"},
                        "videoMessage": {"mediaKey": "ignored-extended-key"},
                    }
                },
            },
        )

        for envelope in envelopes:
            with self.subTest(message=envelope["event"]["Message"]):
                sanitized = sanitize_webhook_envelope(envelope)
                self.assertNotIn("mediakey", json.dumps(sanitized).lower())

    def test_sanitizer_drops_unhandled_protocol_crypto_subtrees(self):
        protocol_only = {
            "senderKeyDistributionMessage": {
                "axolotlSenderKeyDistributionMessage": "sender-key-secret"
            },
            "fastRatchetKeySenderKeyDistributionMessage": {
                "key": "fast-ratchet-secret"
            },
            "messageDistributionMessage": {"key": "distribution-secret"},
            "appStateSyncKeyShare": {
                "keys": [{"keyData": {"keyData": "app-state-secret"}}]
            },
            "secretEncryptedMessage": {
                "encPayload": "encrypted-payload-secret",
                "encIV": "encrypted-iv-secret",
            },
            "encCommentMessage": {"payload": "comment-secret"},
            "encReactionMessage": {"payload": "reaction-secret"},
            "placeholderMessage": {"payload": "placeholder-secret"},
            "stickerSyncRMRMessage": {"payload": "sticker-sync-secret"},
        }
        envelope = {
            "type": "Message",
            "event": {
                "Message": {
                    "conversation": "safe text",
                    **protocol_only,
                    "messageContextInfo": {
                        "messageAssociation": {"associationType": 1},
                        "axolotlSenderKeyDistributionMessage": "loose-axolotl-secret",
                    },
                    # Keep mutation metadata that the adapter does normalize,
                    # while removing an unsupported key-share child.
                    "protocolMessage": {
                        "type": 0,
                        "key": {"ID": "TARGET-MESSAGE-ID"},
                        "appStateSyncKeyShare": {
                            "keys": [{"keyData": "nested-app-state-secret"}]
                        },
                    },
                }
            },
        }

        sanitized = sanitize_webhook_envelope(envelope)

        message = sanitized["event"]["Message"]
        for field_name in protocol_only:
            self.assertNotIn(field_name, message)
        self.assertEqual(message["conversation"], "safe text")
        self.assertEqual(
            message["messageContextInfo"]["messageAssociation"],
            {"associationType": 1},
        )
        self.assertNotIn(
            "axolotlSenderKeyDistributionMessage", message["messageContextInfo"]
        )
        self.assertEqual(message["protocolMessage"]["key"], {"ID": "TARGET-MESSAGE-ID"})
        self.assertNotIn("appStateSyncKeyShare", message["protocolMessage"])
        serialized = json.dumps(sanitized, sort_keys=True)
        for sentinel in (
            "sender-key-secret",
            "fast-ratchet-secret",
            "distribution-secret",
            "app-state-secret",
            "encrypted-payload-secret",
            "comment-secret",
            "reaction-secret",
            "placeholder-secret",
            "sticker-sync-secret",
            "loose-axolotl-secret",
            "nested-app-state-secret",
        ):
            self.assertNotIn(sentinel, serialized)

    def test_sanitizer_allows_exact_sticker_and_associated_child_media_paths(self):
        sticker = self._load_fixture("message_sticker.json")
        associated = self._load_fixture("message_associated_child_video.json")
        wrapped = self._load_fixture("message_ephemeral_view_once_image.json")
        associated["shadow"] = {
            "event": {"message": {"videoMessage": {"mediaKey": "lookalike-path-key"}}}
        }

        sanitized_sticker = sanitize_webhook_envelope(sticker)
        sanitized_associated = sanitize_webhook_envelope(associated)

        sticker_payload = sanitized_sticker["event"]["Message"]["stickerMessage"]
        self.assertEqual(
            sticker_payload["mediaKey"],
            "c3NzcnNzcnNzcnNzcnNzcnNzcnNzcnNzcnNzcnNzcnM=",
        )
        self.assertNotIn("mediaKey", sticker_payload["contextInfo"])
        self.assertNotIn("messageSecret", sticker_payload["contextInfo"])

        message = sanitized_associated["event"]["Message"]
        child = message["associatedChildMessage"]
        video = child["message"]["videoMessage"]
        self.assertEqual(
            video["mediaKey"],
            "dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnY=",
        )
        self.assertNotIn("mediaKey", child)
        self.assertNotIn("mediaKey", video["contextInfo"])
        self.assertNotIn("messageSecret", message["messageContextInfo"])
        self.assertNotIn("paddingBytes", message["messageContextInfo"])
        self.assertNotIn(
            "mediaKey",
            sanitized_associated["shadow"]["event"]["message"]["videoMessage"],
        )
        serialized = json.dumps(sanitized_associated, sort_keys=True)
        self.assertNotIn("thumbnail", serialized.lower())
        self.assertNotIn("sidecar", serialized.lower())

        sanitized_wrapped = sanitize_webhook_envelope(wrapped)
        ephemeral = sanitized_wrapped["event"]["Message"]["ephemeralMessage"]
        view_once = ephemeral["message"]["viewOnceMessageV2"]
        image = view_once["message"]["imageMessage"]
        self.assertNotIn("mediaKey", ephemeral)
        self.assertNotIn("messageSecret", view_once)
        self.assertTrue(image["mediaKey"])
        self.assertNotIn("thumbnail", json.dumps(sanitized_wrapped).lower())

    def test_associated_child_webhook_projects_downloadable_media(self):
        envelope = self._load_fixture("message_associated_child_video.json")
        body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )

        response = self._post(body)

        self.assertEqual(response.status_code, 202, response.text)
        inbox = self._connection_inboxes().ensure_one()
        persisted_video = inbox.raw_envelope_json["event"]["Message"][
            "associatedChildMessage"
        ]["message"]["videoMessage"]
        self.assertIn("mediaKey", persisted_video)
        inbox._process_one()
        inbox.invalidate_recordset(["state", "normalized_dto_json"])
        self.assertEqual(inbox.state, "done")
        self.assertEqual(
            inbox.normalized_dto_json["message"]["protocol_snapshot"]["association"],
            {
                "kind": "associated_child",
                "type": "media_album",
                "parent_external_message_id": "ALBUMPARENT000000000000000000001",
                "message_index": 2,
            },
        )
        binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("provider_connection_id", "=", self.connection.id),
                    (
                        "external_message_id",
                        "=",
                        "ASSOCIATEDVIDEO00000000000000001",
                    ),
                ],
                limit=1,
            )
        )
        self.assertTrue(binding)
        media = binding.media_ids.ensure_one()
        self.assertEqual(media.kind, "video")
        self.assertTrue(media.remote_locator_json["media_key"])
        self.assertTrue(media.queue_job_uuid)

    def test_sanitizer_wrapper_grammar_has_an_eight_wrapper_limit(self):
        def envelope_with_depth(depth):
            message = {"imageMessage": {"mediaKey": "bounded-wrapper-key"}}
            for _index in range(depth):
                message = {"ephemeralMessage": {"message": message}}
            return {"type": "Message", "event": {"Message": message}}

        eight_wrappers = sanitize_webhook_envelope(envelope_with_depth(8))
        nine_wrappers = sanitize_webhook_envelope(envelope_with_depth(9))

        self.assertIn("bounded-wrapper-key", json.dumps(eight_wrappers))
        self.assertNotIn("bounded-wrapper-key", json.dumps(nine_wrappers))

    def test_call_sanitizer_drops_the_complete_binary_data_subtree(self):
        for event_type in ("CallOffer", "CallAccept", "CallTerminate"):
            with self.subTest(event_type=event_type):
                envelope = {
                    "type": event_type,
                    "event": {
                        "From": "5511999999999@s.whatsapp.net",
                        "CallID": "synthetic-secret-call-id",
                        "Data": {
                            "Payload": "U0VOVElORUwtQ0FMTC1EQVRBLUJBU0U2NA==",
                            "Nested": ["SENTINEL-CALL-DATA-PLAINTEXT"],
                        },
                    },
                }

                sanitized = sanitize_webhook_envelope(envelope)

                self.assertNotIn("Data", sanitized["event"])
                serialized = json.dumps(sanitized, sort_keys=True)
                self.assertNotIn("SENTINEL-CALL-DATA", serialized)
                self.assertEqual(
                    sanitized["event"]["CallID"], "synthetic-secret-call-id"
                )

    def test_call_webhook_uses_raw_body_for_auth_and_dedupe_but_persists_no_data(self):
        envelope = {
            "type": "CallOffer",
            "instanceName": "Lucas",
            "userID": "lucas-test",
            "event": {
                "From": "5511999999999@s.whatsapp.net",
                "Timestamp": "2026-08-29T12:00:01Z",
                "CallCreator": "5511999999999@s.whatsapp.net",
                "CallCreatorAlt": "100000000000001@lid",
                "CallID": "synthetic-secret-call-id",
                "GroupJID": "",
                "Data": {
                    "Payload": "U0VOVElORUwtQ0FMTC1EQVRBLUJBU0U2NA==",
                    "Nested": ["SENTINEL-CALL-DATA-PLAINTEXT"],
                },
            },
        }
        body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )

        response = self._post(body)

        self.assertEqual(response.status_code, 202, response.text)
        inbox = self._connection_inboxes().ensure_one()
        self.assertNotIn("Data", inbox.raw_envelope_json["event"])
        self.assertNotIn(
            "SENTINEL-CALL-DATA",
            json.dumps(inbox.raw_envelope_json, sort_keys=True),
        )
        self.assertEqual(
            inbox.metadata_json["content_sha256"], hashlib.sha256(body).hexdigest()
        )
        self.assertTrue(inbox.metadata_json["raw_envelope_sanitized"])

        replay = self._post(body)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["duplicate"])
        self.assertEqual(len(self._connection_inboxes()), 1)

    def test_webhook_persists_sanitized_envelope_but_deduplicates_raw_body(self):
        envelope = {
            "type": "Message",
            "instanceName": "Lucas",
            "userID": "lucas-test",
            "base64": "inline-binary",
            "event": {
                "Info": {"ID": "MEDIA-1"},
                "Message": {
                    "imageMessage": {
                        "mediaKey": "required-download-key",
                        "JPEGThumbnail": "thumbnail",
                    }
                },
                "RawMessage": {"mediaKey": "raw-secret"},
            },
        }
        body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )

        response = self._post(body)

        self.assertEqual(response.status_code, 202, response.text)
        inbox = self._connection_inboxes().ensure_one()
        persisted = inbox.raw_envelope_json
        serialized = json.dumps(persisted, sort_keys=True)
        self.assertNotIn("inline-binary", serialized)
        self.assertNotIn("thumbnail", serialized.lower())
        self.assertNotIn("raw-secret", serialized)
        self.assertEqual(
            persisted["event"]["Message"]["imageMessage"]["mediaKey"],
            "required-download-key",
        )
        self.assertTrue(inbox.metadata_json["raw_envelope_sanitized"])
        replay = self._post(body)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["duplicate"])
