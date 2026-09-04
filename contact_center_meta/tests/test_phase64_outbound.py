import datetime
import hashlib
import hmac
import json
import uuid
from unittest import mock

from odoo import fields

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    AmbiguousTimeoutError,
    ProviderRateLimitError,
)
from odoo.addons.contact_center_base.services.dto import (
    AddressDTO,
    CommandDTO,
    ConversationDTO,
    MediaDTO,
    MessageDTO,
)
from odoo.addons.contact_center_base.services.tokens import CONTACT_CENTER_POST_TOKEN
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.messaging import atomic_events, sanitize_webhook_envelope
from .common import MetaCase


class TestMetaPhase64Outbound(MetaCase):
    REMOTE_MESSENGER_ID = "900000000000064"
    REMOTE_INSTAGRAM_ID = "800000000000064"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection._apply_health_result(
            {
                "state": "connected",
                "reason": "healthy",
                "identity_matches": True,
            },
            source="health_job",
        )
        cls.connection.write({"outbound_active": True})
        cls.technical_author = cls.env["res.partner"].create(
            {"name": "Meta Phase 6.4 Technical Author"}
        )
        cls.account.technical_author_id = cls.technical_author

    def _instagram_connection(self):
        account = self._create_account(
            "instagram",
            self.INSTAGRAM_ID,
            team=self.team,
        )
        account.technical_author_id = self.technical_author
        connection = self._create_connection(account, self.instagram_asset)
        connection._apply_health_result(
            {
                "state": "connected",
                "reason": "healthy",
                "identity_matches": True,
            },
            source="health_job",
        )
        connection.write({"outbound_active": True})
        return connection

    def _seed_inbound(self, connection=None, remote_id=None):
        connection = connection or self.connection
        remote_id = remote_id or (
            self.REMOTE_INSTAGRAM_ID
            if connection.account_id.platform == "instagram"
            else self.REMOTE_MESSENGER_ID
        )
        now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
        object_type = (
            "instagram" if connection.account_id.platform == "instagram" else "page"
        )
        envelope = {
            "object": object_type,
            "entry": [
                {
                    "id": connection.meta_target_asset_id,
                    "messaging": [
                        {
                            "sender": {"id": remote_id},
                            "recipient": {"id": connection.meta_target_asset_id},
                            "timestamp": now_ms,
                            "message": {
                                "mid": "m_phase64_inbound_%s" % uuid.uuid4().hex,
                                "text": "Synthetic response-window evidence",
                            },
                        }
                    ],
                }
            ],
        }
        atomic = list(atomic_events(sanitize_webhook_envelope(envelope)))[0]
        event = connection.get_adapter().normalize_event(connection, atomic)
        with trap_jobs():
            message = self.env["contact.center.application"]._process_event(
                connection,
                event,
            )
        binding = self.env["contact.center.message.binding"].search(
            [("message_id", "=", message.id)],
            limit=1,
        )
        return binding, remote_id

    def _command(self, connection, remote_id, *, text="Resposta humana"):
        namespace = (
            "meta.instagram.igsid"
            if connection.account_id.platform == "instagram"
            else "meta.messenger.psid"
        )
        address = AddressDTO(
            namespace=namespace,
            value=remote_id,
            value_normalized=remote_id,
            role="primary",
            source_field="messaging.sender.id",
            confidence="protocol",
            resolution_scope="account",
        )
        client_id = str(uuid.uuid4())
        return CommandDTO(
            command_id=str(uuid.uuid4()),
            command_type="send_message",
            account_ref=connection.account_id.external_ref,
            connection_ref=connection.external_ref,
            conversation_ref=remote_id,
            conversation=ConversationDTO(
                addresses=(address,),
                conversation_type="direct",
            ),
            target_address=address,
            message=MessageDTO(
                content_type="text",
                text=text,
                client_message_id=client_id,
            ),
            client_message_id=client_id,
        )

    def _reply_command(
        self,
        connection,
        remote_id,
        *,
        reply_message_id="m_phase64_reply_target",
        text="Resposta encadeada",
        protocol_snapshot=None,
    ):
        command = self._command(connection, remote_id, text=text)
        protocol_snapshot = protocol_snapshot or {}
        return CommandDTO.from_dict(
            {
                **command.to_dict(),
                "message": {
                    **command.message.to_dict(),
                    "reply_to_external_id": reply_message_id,
                    "protocol_snapshot": protocol_snapshot,
                },
                "reply_to": {
                    "external_message_id": reply_message_id,
                    "protocol_snapshot": protocol_snapshot,
                },
            }
        )

    @staticmethod
    def _graph_response(payload, *, status=200, headers=None):
        response = mock.Mock()
        response.status_code = status
        response.headers = dict(headers or {})
        response.iter_content.return_value = [
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ]
        return response

    def test_messenger_snapshot_is_exact_bounded_and_contains_no_credentials(self):
        _binding, remote_id = self._seed_inbound()
        command = self._command(self.connection, remote_id)

        snapshot = self.connection.get_adapter().prepare_request_snapshot(
            self.connection,
            command,
        )

        self.assertEqual(snapshot["method"], "POST")
        self.assertEqual(
            snapshot["endpoint"],
            "/%s/messages" % self.ACTIVE_PAGE_ID,
        )
        self.assertEqual(
            snapshot["payload"],
            {
                "recipient": {"id": remote_id},
                "message": {"text": "Resposta humana"},
                "messaging_type": "RESPONSE",
            },
        )
        self.assertEqual(
            snapshot["policy"]["response_window"],
            "standard_24h",
        )
        serialized = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn(self.PAGE_TOKEN, serialized)
        self.assertNotIn(self.APP_SECRET, serialized)
        self.assertNotIn("appsecret_proof", serialized)

    def test_instagram_page_linked_uses_page_path_and_strict_byte_limit(self):
        connection = self._instagram_connection()
        _binding, remote_id = self._seed_inbound(connection)
        command = self._command(connection, remote_id, text="Olá do Instagram")

        snapshot = connection.get_adapter().prepare_request_snapshot(
            connection,
            command,
        )

        self.assertEqual(snapshot["endpoint"], "/%s/messages" % self.ACTIVE_PAGE_ID)
        self.assertNotEqual(
            snapshot["endpoint"],
            "/%s/messages" % self.INSTAGRAM_ID,
        )
        self.assertNotIn("messaging_type", snapshot["payload"])
        oversized = self._command(connection, remote_id, text="á" * 501)
        with self.assertRaisesRegex(AdapterError, "byte limit"):
            connection.get_adapter().prepare_request_snapshot(connection, oversized)

    def test_standard_window_fails_closed_when_missing_expired_or_future(self):
        binding, remote_id = self._seed_inbound()
        command = self._command(self.connection, remote_id)
        adapter = self.connection.get_adapter()
        binding.direction = "outbound"
        with self.assertRaisesRegex(AdapterError, "requires an inbound"):
            adapter.prepare_request_snapshot(self.connection, command)

        binding.direction = "inbound"
        binding.message_id.with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).write(
            {"date": fields.Datetime.now() - datetime.timedelta(hours=24, seconds=1)}
        )
        with self.assertRaisesRegex(AdapterError, "window is closed"):
            adapter.prepare_request_snapshot(self.connection, command)

        binding.message_id.with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).write({"date": fields.Datetime.now() + datetime.timedelta(minutes=6)})
        with self.assertRaisesRegex(AdapterError, "future timestamp"):
            adapter.prepare_request_snapshot(self.connection, command)

    def test_reply_contract_is_exact_for_messenger_and_instagram(self):
        messenger_binding, messenger_remote_id = self._seed_inbound()
        messenger = self._reply_command(
            self.connection,
            messenger_remote_id,
            reply_message_id=messenger_binding.external_message_id,
        )
        messenger_snapshot = self.connection.get_adapter().prepare_request_snapshot(
            self.connection,
            messenger,
        )
        self.assertEqual(
            messenger_snapshot["payload"],
            {
                "recipient": {"id": messenger_remote_id},
                "message": {"text": "Resposta encadeada"},
                "messaging_type": "RESPONSE",
                "reply_to": {"mid": messenger_binding.external_message_id},
            },
        )

        instagram_connection = self._instagram_connection()
        instagram_binding, instagram_remote_id = self._seed_inbound(
            instagram_connection
        )
        instagram = self._reply_command(
            instagram_connection,
            instagram_remote_id,
            reply_message_id=instagram_binding.external_message_id,
        )
        instagram_snapshot = (
            instagram_connection.get_adapter().prepare_request_snapshot(
                instagram_connection,
                instagram,
            )
        )
        self.assertEqual(
            instagram_snapshot["endpoint"],
            "/%s/messages" % self.ACTIVE_PAGE_ID,
        )
        self.assertEqual(
            instagram_snapshot["payload"],
            {
                "recipient": {"id": instagram_remote_id},
                "message": {"text": "Resposta encadeada"},
                "reply_to": {"mid": instagram_binding.external_message_id},
            },
        )

    def test_reply_requires_exact_binding_evidence_and_connection_affinity(self):
        binding, remote_id = self._seed_inbound()
        valid = self._reply_command(
            self.connection,
            remote_id,
            reply_message_id=binding.external_message_id,
            protocol_snapshot={"provider_safe": "evidence"},
        )
        adapter = self.connection.get_adapter()

        mismatched_id = CommandDTO.from_dict(
            {
                **valid.to_dict(),
                "message": {
                    **valid.message.to_dict(),
                    "reply_to_external_id": "m_other",
                },
            }
        )
        with self.assertRaisesRegex(AdapterError, "reply evidence is inconsistent"):
            adapter.prepare_request_snapshot(self.connection, mismatched_id)

        mismatched_snapshot = CommandDTO.from_dict(
            {
                **valid.to_dict(),
                "message": {
                    **valid.message.to_dict(),
                    "protocol_snapshot": {"provider_safe": "other"},
                },
            }
        )
        with self.assertRaisesRegex(AdapterError, "reply evidence is inconsistent"):
            adapter.prepare_request_snapshot(self.connection, mismatched_snapshot)

        unsupported_reference = CommandDTO.from_dict(
            {
                **valid.to_dict(),
                "reply_to": {**valid.reply_to, "provider_hint": "not-allowed"},
            }
        )
        with self.assertRaisesRegex(AdapterError, "unsupported fields"):
            adapter.prepare_request_snapshot(
                self.connection,
                unsupported_reference,
            )

        wrong_connection = CommandDTO.from_dict(
            {**valid.to_dict(), "connection_ref": str(uuid.uuid4())}
        )
        with self.assertRaisesRegex(AdapterError, "direct text messages"):
            adapter.prepare_request_snapshot(self.connection, wrong_connection)

    def test_phase64_rejects_media_mutations_and_wrong_target_namespace(self):
        _binding, remote_id = self._seed_inbound()
        valid = self._command(self.connection, remote_id)
        adapter = self.connection.get_adapter()

        media_message = MessageDTO(
            content_type="image",
            media=(
                MediaDTO(
                    kind="image",
                    remote_locator={"attachment_id": 999999},
                ),
            ),
        )
        media_command = CommandDTO.from_dict(
            {**valid.to_dict(), "message": media_message.to_dict()}
        )
        with self.assertRaisesRegex(AdapterError, "direct text messages"):
            adapter.prepare_request_snapshot(self.connection, media_command)

        wrong_target = {
            **valid.target_address.to_dict(),
            "namespace": "meta.instagram.igsid",
        }
        invalid_route = CommandDTO.from_dict(
            {
                **valid.to_dict(),
                "target_address": wrong_target,
                "conversation": {
                    **valid.conversation.to_dict(),
                    "addresses": [wrong_target],
                },
            }
        )
        with self.assertRaisesRegex(AdapterError, "target is invalid"):
            adapter.prepare_request_snapshot(self.connection, invalid_route)

    @mock.patch("odoo.addons.contact_center_meta.services.outbound.graph_request")
    def test_success_requires_exact_graph_correlation(self, graph_request):
        _binding, remote_id = self._seed_inbound()
        command = self._command(self.connection, remote_id)
        adapter = self.connection.get_adapter()
        graph_request.return_value = {
            "recipient_id": remote_id,
            "message_id": "m_phase64_provider_success",
        }

        result = adapter.execute_command(self.connection, command)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.external_message_id, "m_phase64_provider_success")
        args, kwargs = graph_request.call_args
        self.assertEqual(args[0].external_app_id, self.app.external_app_id)
        self.assertEqual(args[2:4], ("POST", "%s/messages" % self.ACTIVE_PAGE_ID))
        self.assertTrue(kwargs["mutating"])
        self.assertEqual(kwargs["json_data"]["recipient"], {"id": remote_id})

        graph_request.return_value = {
            "recipient_id": "999999999999999",
            "message_id": "m_wrong_recipient",
        }
        uncertain = adapter.execute_command(self.connection, command)
        self.assertEqual(uncertain.status, "uncertain")
        self.assertEqual(uncertain.error_code, "invalid_success_correlation")

    @mock.patch("odoo.addons.contact_center_meta.services.outbound.graph_request")
    def test_instagram_reply_execution_uses_page_route_and_exact_correlation(
        self,
        graph_request,
    ):
        connection = self._instagram_connection()
        binding, remote_id = self._seed_inbound(connection)
        command = self._reply_command(
            connection,
            remote_id,
            reply_message_id=binding.external_message_id,
        )
        graph_request.return_value = {
            "recipient_id": remote_id,
            "message_id": "m_phase64_instagram_reply",
        }

        result = connection.get_adapter().execute_command(connection, command)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.external_message_id, "m_phase64_instagram_reply")
        args, kwargs = graph_request.call_args
        self.assertEqual(args[2:4], ("POST", "%s/messages" % self.ACTIVE_PAGE_ID))
        self.assertEqual(
            kwargs["json_data"]["reply_to"],
            {"mid": binding.external_message_id},
        )

    @mock.patch("odoo.addons.meta_api_base.services.graph.requests.request")
    def test_graph_send_uses_json_bearer_and_appsecret_proof_in_memory(self, request):
        _binding, remote_id = self._seed_inbound()
        command = self._command(self.connection, remote_id)
        response = self._graph_response(
            {
                "recipient_id": remote_id,
                "message_id": "m_phase64_graph_success",
            }
        )
        request.return_value = response

        result = self.connection.get_adapter().execute_command(
            self.connection,
            command,
        )

        self.assertEqual(result.status, "success")
        _args, kwargs = request.call_args
        self.assertIsNone(kwargs["data"])
        self.assertEqual(kwargs["json"]["recipient"], {"id": remote_id})
        self.assertEqual(
            kwargs["json"]["appsecret_proof"],
            hmac.new(
                self.APP_SECRET.encode("utf-8"),
                self.PAGE_TOKEN.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
        )
        self.assertNotIn(self.APP_SECRET, json.dumps(kwargs["json"]))
        self.assertNotIn(self.PAGE_TOKEN, json.dumps(kwargs["json"]))
        self.assertEqual(
            kwargs["headers"]["Authorization"],
            "Bearer %s" % self.PAGE_TOKEN,
        )
        response.close.assert_called_once()

    @mock.patch("odoo.addons.meta_api_base.services.graph.requests.request")
    def test_graph_rate_limit_is_retryable_but_server_error_is_uncertain(
        self,
        request,
    ):
        _binding, remote_id = self._seed_inbound()
        command = self._command(self.connection, remote_id)
        adapter = self.connection.get_adapter()
        request.return_value = self._graph_response(
            {"error": {"code": 613, "message": "rate limited"}},
            status=400,
            headers={"Retry-After": "17"},
        )

        with self.assertRaises(ProviderRateLimitError) as rate_limited:
            adapter.execute_command(self.connection, command)
        self.assertEqual(rate_limited.exception.retry_after_seconds, 17)

        request.return_value = self._graph_response(
            {"error": {"code": 2, "message": "temporary"}},
            status=503,
        )
        with self.assertRaises(AmbiguousTimeoutError):
            adapter.execute_command(self.connection, command)

    @mock.patch("odoo.addons.contact_center_meta.services.outbound.graph_request")
    def test_ambiguous_transport_failure_propagates_without_redispatch(self, request):
        _binding, remote_id = self._seed_inbound()
        command = self._command(self.connection, remote_id)
        request.side_effect = AmbiguousTimeoutError("Meta send result is ambiguous")

        with self.assertRaises(AmbiguousTimeoutError):
            self.connection.get_adapter().execute_command(self.connection, command)
        request.assert_called_once()

    @mock.patch("odoo.addons.contact_center_meta.services.outbound.graph_request")
    def test_provider_message_id_makes_later_echo_idempotent(self, graph_request):
        inbound_binding, remote_id = self._seed_inbound()
        command = self._command(self.connection, remote_id)
        graph_request.return_value = {
            "recipient_id": remote_id,
            "message_id": "m_phase64_echo_correlation",
        }
        result = self.connection.get_adapter().execute_command(
            self.connection,
            command,
        )
        channel = inbound_binding.channel_binding_id.channel_id
        outbound_message = channel.sudo()._contact_center_post(
            origin="outbound",
            body="Resposta humana",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            date=fields.Datetime.now(),
            author_id=self.technical_author.id,
            partner_ids=[],
        )
        outbound_binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": outbound_message.id,
                    "channel_binding_id": inbound_binding.channel_binding_id.id,
                    "provider_connection_id": self.connection.id,
                    "direction": "outbound",
                    "origin": "agent",
                    "content_type": "text",
                    "external_message_id": result.external_message_id,
                    "client_message_id": command.client_message_id,
                    "delivery_state": "sent",
                }
            )
        )
        before_count = self.env["contact.center.message.binding"].search_count(
            [
                (
                    "channel_binding_id",
                    "=",
                    inbound_binding.channel_binding_id.id,
                )
            ]
        )
        now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
        echo = {
            "object": "page",
            "entry": [
                {
                    "id": self.ACTIVE_PAGE_ID,
                    "messaging": [
                        {
                            "sender": {"id": self.ACTIVE_PAGE_ID},
                            "recipient": {"id": remote_id},
                            "timestamp": now_ms,
                            "message": {
                                "mid": result.external_message_id,
                                "is_echo": True,
                                "text": "Resposta humana",
                            },
                        }
                    ],
                }
            ],
        }
        atomic = list(atomic_events(sanitize_webhook_envelope(echo)))[0]
        event = self.connection.get_adapter().normalize_event(
            self.connection,
            atomic,
        )

        correlated = self.env["contact.center.application"]._process_event(
            self.connection,
            event,
        )

        self.assertEqual(correlated, outbound_message)
        self.assertEqual(
            self.env["contact.center.message.binding"].search_count(
                [
                    (
                        "channel_binding_id",
                        "=",
                        inbound_binding.channel_binding_id.id,
                    )
                ]
            ),
            before_count,
        )
        outbound_binding.invalidate_recordset(["delivery_state"])
        self.assertEqual(outbound_binding.delivery_state, "sent")
