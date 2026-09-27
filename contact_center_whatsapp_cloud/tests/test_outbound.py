import datetime
import hashlib
import json
import uuid
from unittest import mock

import requests

from odoo import fields

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    AmbiguousTimeoutError,
    ProviderPausedError,
    ProviderRateLimitError,
    TransientAdapterError,
)
from odoo.addons.contact_center_base.services.dto import CommandDTO
from odoo.addons.contact_center_base.services.tokens import CONTACT_CENTER_POST_TOKEN
from odoo.addons.meta_api_base.services.errors import MetaApiError
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.api import send_error
from .common import GRAPH_PATCH, FakeResponse, WhatsAppCloudCase
from .test_status import WhatsAppCloudOutboundMixin

GRAPH_BASE = "https://graph.facebook.com/v26.0"


class WhatsAppCloudMediaMixin:
    def _upload(self, channel_binding, content, mime_type, kind, name):
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .with_context(image_no_postprocess=True)
            .create(
                {
                    "name": name,
                    "type": "binary",
                    "raw": content,
                    "mimetype": mime_type,
                    "res_model": "contact.center.media.upload",
                    "res_id": 0,
                }
            )
        )
        upload = (
            self.env["contact.center.media.upload"]
            .sudo()
            .create(
                {
                    "reference": str(uuid.uuid4()),
                    "channel_binding_id": channel_binding.id,
                    "uploaded_by_user_id": self.agent.id,
                    "attachment_id": attachment.id,
                    "kind": kind,
                    "mime_type": mime_type,
                    "file_name": name,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        )
        attachment.res_id = upload.id
        return upload


class TestWhatsAppCloudOutbound(
    WhatsAppCloudMediaMixin, WhatsAppCloudOutboundMixin, WhatsAppCloudCase
):
    def _age(self, binding, *, now=None, **delta):
        binding.message_id.with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).write({"date": (now or fields.Datetime.now()) - datetime.timedelta(**delta)})

    def _requests(self, request):
        return [
            (call.args[0], call.args[1], call.kwargs) for call in request.call_args_list
        ]

    # -- 8: snapshot and documented requests --------------------------------------

    def test_snapshot_is_exact_and_contains_no_secret(self):
        channel, _inbound = self._conversation()
        outbox = self._send(channel, "Olá! Posso ajudar.")
        _connection, command, _adapter, snapshot = outbox._prepare_dispatch()
        self.assertEqual(snapshot["method"], "POST")
        self.assertEqual(snapshot["endpoint"], "/v26.0/%s/messages" % self.PHONE_ID)
        self.assertEqual(
            snapshot["payload"],
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": self.CUSTOMER,
                "type": "text",
                "text": {"body": "Olá! Posso ajudar."},
                "biz_opaque_callback_data": "wac:%s" % command.command_id,
            },
        )
        self.assertEqual(snapshot["policy"]["response_window"], "standard_24h")
        serialized = json.dumps(snapshot)
        for secret in (self.WABA_TOKEN, self.APP_SECRET, "appsecret_proof"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(
            outbox.message_binding_id.client_message_id, "wac:%s" % command.command_id
        )

    def test_text_send_uses_bearer_and_records_the_provider_id(self):
        channel, _inbound = self._conversation()
        outbox = self._send(channel, "Texto enviado")
        wamid = self.wamid("text")
        with mock.patch(
            GRAPH_PATCH, return_value=self._accepted(wamid, self.CUSTOMER)
        ) as request:
            outbox._process_one()
        method, url, kwargs = self._requests(request)[0]
        self.assertEqual(
            (method, url), ("POST", "%s/%s/messages" % (GRAPH_BASE, self.PHONE_ID))
        )
        self.assertEqual(
            kwargs["headers"]["Authorization"], "Bearer %s" % self.WABA_TOKEN
        )
        self.assertEqual(kwargs["json"]["text"], {"body": "Texto enviado"})
        self.assertIn("appsecret_proof", kwargs["json"])
        self.assertNotIn(self.WABA_TOKEN, json.dumps(kwargs["json"]))
        self.assertEqual(outbox.state, "done")
        self.assertEqual(outbox.message_binding_id.external_message_id, wamid)
        self.assertEqual(outbox.provider_response_json["message_id"], wamid)

    def test_reply_references_the_customer_message(self):
        channel, inbound = self._conversation()
        outbox = self._send(
            channel, "Respondendo", reply_to_message_id=inbound.message_id.id
        )
        _connection, _command, _adapter, snapshot = outbox._prepare_dispatch()
        self.assertEqual(
            snapshot["payload"]["context"], {"message_id": inbound.external_message_id}
        )

    def test_media_is_uploaded_first_then_sent_by_id_with_caption(self):
        channel, inbound = self._conversation()
        content = b"\xff\xd8\xff synthetic jpeg bytes"
        upload = self._upload(
            inbound.channel_binding_id, content, "image/jpeg", "image", "foto.jpg"
        )
        with trap_jobs():
            result = (
                self.env["contact.center.ui.api"]
                .with_user(self.agent)
                .send_message(channel.id, "Segue a foto", media_refs=[upload.reference])
            )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("message_binding_id.message_id", "=", result["message_id"])])
        )
        _connection, command, adapter, snapshot = outbox._prepare_dispatch()
        self.assertEqual(
            snapshot["upload"]["endpoint"], "/v26.0/%s/media" % self.PHONE_ID
        )
        self.assertNotIn("synthetic jpeg", json.dumps(snapshot))
        wamid = self.wamid("media")
        with mock.patch(
            GRAPH_PATCH,
            side_effect=[
                FakeResponse({"id": "123456789012345"}),
                self._accepted(wamid, self.CUSTOMER),
            ],
        ) as request:
            result = adapter.execute_command(self.connection, command)
        self.assertEqual(result.status, "success")
        (upload_method, upload_url, upload_kwargs), (
            _m,
            send_url,
            send_kwargs,
        ) = self._requests(request)
        self.assertEqual(
            (upload_method, upload_url),
            ("POST", "%s/%s/media" % (GRAPH_BASE, self.PHONE_ID)),
        )
        filename, uploaded, mime_type = upload_kwargs["files"]["file"]
        self.assertEqual(uploaded, content)
        self.assertEqual(mime_type, "image/jpeg")
        self.assertTrue(filename.isascii())
        self.assertEqual(upload_kwargs["data"]["messaging_product"], "whatsapp")
        self.assertEqual(send_url, "%s/%s/messages" % (GRAPH_BASE, self.PHONE_ID))
        self.assertEqual(send_kwargs["json"]["type"], "image")
        self.assertEqual(
            send_kwargs["json"]["image"],
            {"id": "123456789012345", "caption": "Segue a foto"},
        )

    def test_reaction_add_and_remove_payloads(self):
        channel, inbound = self._conversation()
        api = self.env["contact.center.ui.api"].with_user(self.agent)
        with trap_jobs():
            api.react_message(
                channel.id, inbound.message_id.id, "👍", "add", str(uuid.uuid4())
            )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("command_type", "=", "react")], order="id desc", limit=1)
        )
        _connection, command, adapter, snapshot = outbox._prepare_dispatch()
        self.assertEqual(
            snapshot["payload"],
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": self.CUSTOMER,
                "type": "reaction",
                "reaction": {"message_id": inbound.external_message_id, "emoji": "👍"},
            },
        )
        with mock.patch(
            GRAPH_PATCH, return_value=self._accepted(self.wamid("react"))
        ) as request:
            self.assertEqual(
                adapter.execute_command(self.connection, command).status, "success"
            )
        self.assertEqual(request.call_args.kwargs["json"]["type"], "reaction")
        removal = type(command).from_dict(
            dict(
                command.to_dict(),
                options=dict(command.options, operation="remove", emoji=""),
            )
        )
        removal_snapshot = adapter.prepare_request_snapshot(self.connection, removal)
        self.assertEqual(removal_snapshot["payload"]["reaction"]["emoji"], "")

    # -- 8: 24-hour window --------------------------------------------------------

    def test_outside_the_window_is_permanent_without_network(self):
        channel, inbound = self._conversation()
        outbox = self._send(channel, "Tarde demais")
        checked_at = fields.Datetime.now()
        self._age(inbound, now=checked_at, hours=24, seconds=1)
        # The window is one second past its limit at ``checked_at``; pin that
        # clock, as a backwards host clock step (WSL) would move it back inside.
        with mock.patch(GRAPH_PATCH) as request, mock.patch.object(
            fields.Datetime, "now", return_value=checked_at
        ):
            with self.assertRaisesRegex(AdapterError, "customer service window"):
                outbox._prepare_dispatch()
            command = CommandDTO.from_dict(outbox.command_json)
            with self.assertRaisesRegex(AdapterError, "customer service window"):
                self.connection.get_adapter().execute_command(self.connection, command)
        request.assert_not_called()

    def test_window_follows_the_stable_route_including_replaced_connections(self):
        channel, _inbound = self._conversation()
        self.connection.write({"outbound_active": False})
        self.connection.write({"active": False})
        replacement = self._make_connected(
            self._create_connection(self.account, self.waba_asset)
        )
        outbox = self._send(channel, "Pela conexão substituta")
        self.assertEqual(outbox.provider_connection_id, replacement)
        _connection, _command, _adapter, snapshot = outbox._prepare_dispatch()
        self.assertTrue(snapshot["policy"]["last_inbound_at"])
        # Another phone number of the same inbox never inherits the window.
        outbox.sudo().write({"state": "cancelled"})
        replacement.write({"outbound_active": False})
        replacement.write({"active": False})
        other_number = self._make_connected(
            self._create_connection(self.account, self.waba_asset, self.OTHER_PHONE_ID)
        )
        other = self._send(channel, "Por outro número")
        self.assertEqual(other.provider_connection_id, other_number)
        with self.assertRaisesRegex(AdapterError, "customer service window"):
            other._prepare_dispatch()

    # -- 8: classification ---------------------------------------------------------

    def test_accepted_upload_then_502_is_uncertain_and_never_sent_twice(self):
        channel, inbound = self._conversation()
        content = b"%PDF-1.4 synthetic"
        upload = self._upload(
            inbound.channel_binding_id,
            content,
            "application/pdf",
            "document",
            "proposta.pdf",
        )
        with trap_jobs():
            result = (
                self.env["contact.center.ui.api"]
                .with_user(self.agent)
                .send_message(channel.id, "", media_refs=[upload.reference])
            )
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("message_binding_id.message_id", "=", result["message_id"])])
        )
        with mock.patch(
            GRAPH_PATCH,
            side_effect=[
                FakeResponse({"id": "123456789012345"}),
                FakeResponse({}, status=502, content=b"<html>bad gateway</html>"),
            ],
        ) as request:
            self._run_job(outbox)
            self.assertEqual(outbox.state, "uncertain")
            self.assertFalse(self._run_job(outbox))
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[1].kwargs["json"]["document"]["filename"],
            "proposta.pdf",
        )

    def test_response_without_message_id_is_uncertain(self):
        channel, _inbound = self._conversation()
        outbox = self._send(channel, "Sem id")
        with mock.patch(
            GRAPH_PATCH,
            return_value=FakeResponse(
                {"messaging_product": "whatsapp", "messages": []}
            ),
        ) as request:
            self._run_job(outbox)
            self.assertEqual(outbox.state, "uncertain")
            self._run_job(outbox)
        self.assertEqual(request.call_count, 1)

    def test_pair_rate_limit_waits_and_resends_only_the_refused_message(self):
        channel, _inbound = self._conversation()
        first = self._send(channel, "Primeira")
        second = self._send(channel, "Segunda")
        accepted = self.wamid("first")
        refused = FakeResponse(
            {"error": {"code": 131056, "message": "(#131056) Pair rate limit hit"}},
            status=400,
        )
        with mock.patch(
            GRAPH_PATCH, side_effect=[self._accepted(accepted), refused]
        ) as request:
            self._run_job(first)
            self.assertEqual(first.state, "done")
            outcome = self._run_job(second)
        self.assertIsInstance(outcome, RetryableJobError)
        self.assertEqual(second.state, "pending")
        self.assertEqual(second.last_error_class, "ProviderRateLimitError")
        self.assertEqual(second.attempts, 0, "a proven refusal refunds the attempt")
        deadline = self.connection.sudo().outbound_dispatch_not_before
        self.assertTrue(deadline)
        # Still inside the pair interval: nothing crosses the provider boundary.
        with mock.patch(GRAPH_PATCH) as request_during_wait:
            self._run_job(second)
        request_during_wait.assert_not_called()
        resent = self.wamid("second")
        with mock.patch.object(
            type(second),
            "_database_clock",
            autospec=True,
            return_value=deadline + datetime.timedelta(seconds=1),
        ), mock.patch(GRAPH_PATCH, return_value=self._accepted(resent)) as retry:
            self._run_job(second)
        self.assertEqual(second.state, "done")
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(retry.call_args.kwargs["json"]["text"], {"body": "Segunda"})
        self.assertEqual(first.message_binding_id.external_message_id, accepted)
        self.assertEqual(second.message_binding_id.external_message_id, resent)
        self.assertEqual(request.call_count, 2)

    def test_each_provider_code_family_maps_to_its_classification(self):
        channel, _inbound = self._conversation()
        outbox = self._send(channel, "Classificação")
        _connection, command, adapter, _snapshot = outbox._prepare_dispatch()
        cases = [
            (400, 131047, AdapterError),
            (400, 131026, AdapterError),
            (400, 131048, AdapterError),
            (400, 131049, AdapterError),
            (400, 131050, AdapterError),
            (400, 131051, AdapterError),
            (400, 131052, AdapterError),
            (400, 131053, AdapterError),
            (400, 131009, AdapterError),
            (400, 132000, AdapterError),
            (400, 132012, AdapterError),
            (400, 131062, AdapterError),
            (400, 130429, ProviderRateLimitError),
            (400, 80007, ProviderRateLimitError),
            (400, 4, ProviderRateLimitError),
            (400, 131056, ProviderRateLimitError),
            (429, 130429, ProviderRateLimitError),
            (401, 190, ProviderPausedError),
            (400, 0, ProviderPausedError),
            (403, 368, ProviderPausedError),
            (400, 131031, ProviderPausedError),
            (400, 131000, TransientAdapterError),
            (500, 131000, AmbiguousTimeoutError),
            (503, 0, AmbiguousTimeoutError),
            (429, 0, AmbiguousTimeoutError),
            (408, 0, AmbiguousTimeoutError),
        ]
        for status, code, error_class in cases:
            payload = {"error": {"code": code, "message": "synthetic"}}
            with self.subTest(status=status, code=code), mock.patch(
                GRAPH_PATCH, return_value=FakeResponse(payload, status=status)
            ):
                with self.assertRaises(error_class) as raised:
                    adapter.execute_command(self.connection, command)
                self.assertIs(type(raised.exception), error_class)
        for failure in (requests.Timeout("t"), requests.ConnectionError("reset")):
            with self.subTest(failure=type(failure).__name__), mock.patch(
                GRAPH_PATCH, side_effect=failure
            ):
                with self.assertRaises(AmbiguousTimeoutError):
                    adapter.execute_command(self.connection, command)

    # -- 8b: read receipts --------------------------------------------------------

    def _mark_read_outbox(self, *, count=3, age=None):
        self.account.mark_read_enabled = True
        bindings = self.env["contact.center.message.binding"]
        channel = None
        for index in range(count):
            message = self.text_message(body="Mensagem %s" % index)
            self.deliver(self.envelope(self.value(messages=[message])))
            binding = self.binding_for(message["id"])
            bindings |= binding
            channel = binding.channel_binding_id.channel_id
        if age:
            for offset, binding in enumerate(bindings):
                binding.message_id.with_context(
                    contact_center_post_token=CONTACT_CENTER_POST_TOKEN
                ).write(
                    {
                        "date": fields.Datetime.now()
                        - age
                        + datetime.timedelta(seconds=offset)
                    }
                )
        with trap_jobs():
            commands = self.env["contact.center.application"]._queue_direct_mark_read(
                channel, bindings[-1].message_id
            )
        return commands.ensure_one(), bindings

    def test_mark_read_after_24h_sends_the_newest_id_for_the_whole_batch(self):
        outbox, bindings = self._mark_read_outbox(age=datetime.timedelta(days=2))
        _connection, command, adapter, snapshot = outbox._prepare_dispatch()
        self.assertEqual(
            sorted(command.options["external_message_ids"]),
            sorted(bindings.mapped("external_message_id")),
        )
        newest = bindings[-1].external_message_id
        self.assertEqual(
            snapshot["payload"],
            {"messaging_product": "whatsapp", "status": "read", "message_id": newest},
        )
        with mock.patch(
            GRAPH_PATCH, return_value=FakeResponse({"success": True})
        ) as request:
            result = adapter.execute_command(self.connection, command)
        self.assertEqual(result.status, "success")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["json"]["message_id"], newest)
        self.assertEqual(request.call_args.kwargs["json"]["status"], "read")
        self.assertEqual(
            sorted(result.provider_response["covered_message_ids"]),
            sorted(bindings.mapped("external_message_id")),
        )
        with mock.patch(GRAPH_PATCH, return_value=FakeResponse({"success": True})):
            outbox._process_one()
        self.assertEqual(outbox.state, "done")

    def test_mark_read_older_than_30_days_is_permanent_without_network(self):
        outbox, _bindings = self._mark_read_outbox(
            count=1, age=datetime.timedelta(days=30, minutes=5)
        )
        command = CommandDTO.from_dict(outbox.command_json)
        with mock.patch(GRAPH_PATCH) as request:
            with self.assertRaisesRegex(AdapterError, "30 days"):
                self.connection.get_adapter().prepare_request_snapshot(
                    self.connection, command
                )
            with self.assertRaisesRegex(AdapterError, "30 days"):
                self.connection.get_adapter().execute_command(self.connection, command)
        request.assert_not_called()

    def test_mark_read_server_errors_are_transient(self):
        outbox, _bindings = self._mark_read_outbox(count=1)
        _connection, command, adapter, _snapshot = outbox._prepare_dispatch()
        for response in (
            FakeResponse({}, status=503),
            FakeResponse({"success": False}),
        ):
            with self.subTest(status=response.status_code), mock.patch(
                GRAPH_PATCH, return_value=response
            ):
                with self.assertRaises(TransientAdapterError):
                    adapter.execute_command(self.connection, command)

    def test_send_classification_uses_the_http_status_of_any_graph_error(self):
        for status, code in ((502, 131000), (500, 0), (425, 0), (408, 131026)):
            with self.subTest(status=status, code=code):
                error = send_error(
                    MetaApiError("synthetic", http_status=status, provider_code=code)
                )
                self.assertIs(type(error), AmbiguousTimeoutError)
        refused = send_error(
            MetaApiError("synthetic", http_status=400, provider_code=131047)
        )
        self.assertIs(type(refused), AdapterError)
