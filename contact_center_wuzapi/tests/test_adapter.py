import base64
import copy
import dataclasses
import datetime
import hashlib
import hmac
import json
import traceback
from unittest import mock

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError

from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderPausedError,
    ProviderRateLimitError,
    TransientAdapterError,
    UnsupportedEventError,
    adapter_registry,
)
from odoo.addons.contact_center_base.services.dto import (
    AddressDTO,
    CommandDTO,
    ConversationDTO,
    MediaDTO,
    MessageDTO,
)
from odoo.addons.contact_center_base.services.media import (
    enabled_media_kinds,
    validate_media_metadata,
    validate_provider_media_capability,
)

from ..controllers.webhook import sanitize_webhook_envelope
from ..services.adapter import (
    WUZAPI_COMMIT,
    WUZAPI_VERSION,
    WuzapiAdapter,
    _address,
    _limited_json_object,
    _safe_attribution_url,
)
from ..services.structured_content import OUTBOUND_STRUCTURED_CONTENT
from .common import WuzapiCase

REQUEST_PATCH = "odoo.addons.contact_center_wuzapi.services.adapter.requests.request"
COMMAND_UUID = "12345678-1234-5678-1234-567812345678"
CLIENT_MESSAGE_ID = "12345678123456781234567812345678"
OPUS_HEAD = (
    b"OpusHead\x01\x01\x00\x00" + (48000).to_bytes(4, "little") + b"\x00\x00\x00"
)


def _ogg_page(packet, *, granule=0, header_type=2, serial=1, sequence=0):
    return (
        b"OggS\x00"
        + bytes((header_type,))
        + granule.to_bytes(8, "little")
        + serial.to_bytes(4, "little")
        + sequence.to_bytes(4, "little")
        + (b"\x00" * 4)
        + b"\x01"
        + bytes((len(packet),))
        + packet
    )


OGG_OPUS_CONTENT = _ogg_page(OPUS_HEAD) + _ogg_page(
    b"\xf8\xff\xfe",
    granule=3 * 48000,
    header_type=4,
    sequence=1,
)


def _mp4_box(box_type, payload=b""):
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


def _mp4_with_handlers(*handlers, duration_seconds=3):
    tracks = b"".join(
        _mp4_box(
            b"trak",
            _mp4_box(b"tkhd", (b"\x00" * 12) + track_id.to_bytes(4, "big"))
            + _mp4_box(
                b"mdia",
                (
                    _mp4_box(
                        b"mdhd",
                        b"\x00\x00\x00\x00"
                        + (b"\x00" * 8)
                        + (48000).to_bytes(4, "big")
                        + (duration_seconds * 48000).to_bytes(4, "big")
                        + (b"\x00" * 4),
                    )
                    + _mp4_box(b"hdlr", (b"\x00" * 8) + handler + (b"\x00" * 4))
                ),
            ),
        )
        for track_id, handler in enumerate(handlers, start=1)
    )
    return (
        _mp4_box(b"ftyp", b"isom\x00\x00\x00\x00")
        + _mp4_box(b"moov", tracks)
        + _mp4_box(b"mdat", b"\x00")
    )


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def iter_content(self, chunk_size=65536):
        raw = json.dumps(self._payload, separators=(",", ":")).encode("utf-8")
        for offset in range(0, len(raw), chunk_size):
            yield raw[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class TestWuzapiAdapter(WuzapiCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.adapter = WuzapiAdapter(cls.env)

    def _record_own_call_identity(self, pn, lid):
        self.account.own_external_identity = pn
        self.connection._apply_health_result(
            {
                "state": "connected",
                "identity_matches": True,
                "wuzapi_own_identity": {"jid": pn, "lid": lid},
            },
            records_health_probe=True,
            expected_configuration_revision=self.connection.health_configuration_revision,
        )

    def test_call_accepted_on_own_device_routes_to_creator_without_provider_io(self):
        self._record_own_call_identity(
            "5511888888888:12@s.whatsapp.net", "200000000000002:12@lid"
        )
        envelope = self.load_fixture("call_accept.json")
        envelope["event"].update(
            From="200000000000002@lid",
            CallCreator="100000000000001@lid",
            CallCreatorAlt="5511999999999@s.whatsapp.net",
        )
        with mock.patch(REQUEST_PATCH, side_effect=AssertionError("Ingress did I/O")):
            event = self.adapter.normalize_event(self.connection, envelope)
            route = self.adapter.conversation_route(self.connection, envelope)
        self.assertEqual(event.conversation_ref, "100000000000001@lid")
        self.assertEqual(route["conversation_ref"], event.conversation_ref)
        self.assertEqual(event.extensions["call"]["direction"], "inbound")
        self.assertEqual(event.actor.addresses[0].source_field, "event.CallCreator")
        self.assertEqual(len(event.conversation.addresses), 2)
        self.assertNotIn("200000000000002", json.dumps(event.to_dict()))

        offer = copy.deepcopy(envelope)
        offer["type"] = "CallOffer"
        offer["event"]["From"] = offer["event"]["CallCreator"]
        application = self.env["contact.center.application"]
        first = application._process_event(
            self.connection, self.adapter.normalize_event(self.connection, offer)
        )
        accepted = application._process_event(self.connection, event)
        replayed = application._process_event(self.connection, event)
        self.assertEqual(first.res_id, accepted.res_id)
        self.assertEqual(accepted, replayed)
        self.assertFalse(
            self.env["contact.center.identity.alias"].search_count(
                [
                    ("account_id", "=", self.account.id),
                    ("value_normalized", "=", "200000000000002@lid"),
                ]
            )
        )

    def test_outbound_call_uses_remote_from_and_excludes_own_creator_lid(self):
        self._record_own_call_identity(
            "5511888888888@s.whatsapp.net", "200000000000002@lid"
        )
        envelope = self.load_fixture("call_accept.json")
        envelope["event"].update(
            From="100000000000001@lid",
            CallCreator="200000000000002@lid",
            CallCreatorAlt="",
        )
        event = self.adapter.normalize_event(self.connection, envelope)
        self.assertEqual(event.conversation_ref, "100000000000001@lid")
        self.assertEqual(event.extensions["call"]["direction"], "outbound")
        self.assertEqual(len(event.conversation.addresses), 1)
        self.assertNotIn("200000000000002", json.dumps(event.to_dict()))

    def test_call_routing_requires_current_session_proof_not_cross_inbox_aliases(self):
        self.account.own_external_identity = "5511888888888@s.whatsapp.net"
        envelope = self.load_fixture("call_accept.json")
        with self.assertRaises(TransientAdapterError):
            self.adapter.normalize_event(self.connection, envelope)
        self.assertIsNone(self.adapter.conversation_route(self.connection, envelope))
        self._record_own_call_identity(
            "5511888888888@s.whatsapp.net", "200000000000002@lid"
        )
        self.connection.wuzapi_api_token = "changed-session-token"
        with self.assertRaises(TransientAdapterError):
            self.adapter.normalize_event(self.connection, envelope)

    def test_call_creator_pair_cannot_merge_remote_and_own_identities(self):
        self._record_own_call_identity(
            "5511888888888@s.whatsapp.net", "200000000000002@lid"
        )
        envelope = self.load_fixture("call_accept.json")
        envelope["event"].update(
            From="200000000000002@lid",
            CallCreator="100000000000001@lid",
            CallCreatorAlt="5511888888888@s.whatsapp.net",
        )
        with self.assertRaises(AdapterError):
            self.adapter.normalize_event(self.connection, envelope)

    def test_session_identity_snapshot_is_internal_and_rejects_other_phone(self):
        self.account.own_external_identity = "5511888888888@s.whatsapp.net"
        with self.assertRaises(AccessError):
            self.connection.write({"wuzapi_own_identity_json": {"lid": "1@lid"}})
        with self.assertRaises(AdapterError):
            self.connection._apply_health_result(
                {
                    "state": "connected",
                    "identity_matches": True,
                    "wuzapi_own_identity": {
                        "jid": "5511777777777@s.whatsapp.net",
                        "lid": "200000000000002@lid",
                    },
                },
                records_health_probe=True,
            )

    def test_stale_health_probe_cannot_overwrite_session_identity(self):
        self._record_own_call_identity(
            "5511888888888@s.whatsapp.net", "200000000000002@lid"
        )
        before = dict(self.connection.wuzapi_own_identity_json)
        self.connection._apply_health_result(
            {
                "state": "connected",
                "identity_matches": True,
                "wuzapi_own_identity": {
                    "jid": "5511888888888@s.whatsapp.net",
                    "lid": "300000000000003@lid",
                },
            },
            expected_configuration_revision=self.connection.health_configuration_revision
            - 1,
            records_health_probe=True,
        )
        self.assertEqual(self.connection.wuzapi_own_identity_json, before)

    def _command(
        self,
        reply_to=None,
        protocol_snapshot=None,
        command_type="send_message",
        media=(),
        options=None,
        text="Resposta do Odoo",
        conversation_type="direct",
        target_value=None,
        target_role=None,
        target_namespace=None,
        own_protocol_participant=None,
        target_protocol_participant=None,
        structured_content=None,
    ):
        if target_value is None:
            target_value = (
                "120363000000001@g.us"
                if conversation_type == "group"
                else "5511999999999@s.whatsapp.net"
            )
        target = AddressDTO(
            namespace=target_namespace
            or ("whatsapp.group" if conversation_type == "group" else "whatsapp.pn"),
            value=target_value,
            value_normalized=target_value,
            role=target_role
            or ("group" if conversation_type == "group" else "primary"),
            source_field="test",
            confidence="protocol",
        )
        reply_to = reply_to or {}
        return CommandDTO(
            command_id=COMMAND_UUID,
            command_type=command_type,
            account_ref=self.account.external_ref,
            connection_ref=self.connection.external_ref,
            conversation_ref=target_value,
            conversation=ConversationDTO(
                addresses=(target,), conversation_type=conversation_type
            ),
            target_address=target,
            own_protocol_participant=own_protocol_participant,
            target_protocol_participant=target_protocol_participant,
            message=(
                MessageDTO(
                    content_type=(
                        structured_content["type"]
                        if structured_content
                        else media[0].kind
                        if media
                        else "text"
                    ),
                    text=text,
                    client_message_id=CLIENT_MESSAGE_ID,
                    reply_to_external_id=reply_to.get("external_message_id", ""),
                    protocol_snapshot=protocol_snapshot or {},
                    media=media,
                    structured_content=structured_content or {},
                )
                if command_type in ("send_message", "edit_message")
                else None
            ),
            reply_to=reply_to,
            options=options or {},
            client_message_id=CLIENT_MESSAGE_ID,
        )

    def _outbound_media(
        self,
        kind,
        content,
        mime_type,
        file_name,
        is_voice_note=False,
        duration_seconds=0,
    ):
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .create(
                {
                    "name": file_name,
                    "type": "binary",
                    "datas": base64.b64encode(content),
                    "mimetype": mime_type,
                }
            )
        )
        return MediaDTO(
            kind=kind,
            external_media_id="outbound-attachment",
            remote_locator={"attachment_id": attachment.id},
            mime_type=mime_type,
            file_name=file_name,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            is_voice_note=is_voice_note,
            duration_seconds=duration_seconds,
            width=0,
            height=0,
        )

    def test_adapter_is_registered_and_capabilities_are_enabled(self):
        self.assertIn("wuzapi", adapter_registry.keys())
        self.assertIs(adapter_registry.get("wuzapi"), WuzapiAdapter)
        self.assertEqual(adapter_registry.owner("wuzapi"), "contact_center_wuzapi")
        self.assertIsInstance(self.connection.get_adapter(), WuzapiAdapter)

        capabilities = self.adapter.get_capabilities(self.connection)
        self.assertTrue(capabilities["send_message"])
        self.assertTrue(capabilities["sender_signature"])
        self.assertTrue(capabilities["mark_read"])
        self.assertTrue(capabilities["react"])
        self.assertTrue(capabilities["edit_message"])
        self.assertTrue(capabilities["delete_message"])
        self.assertEqual(
            set(capabilities["media"]),
            {
                "image",
                "audio",
                "video",
                "document",
            },
        )
        self.assertEqual(
            capabilities["media"]["image"],
            {
                "enabled": True,
                "max_bytes": 16 * 1024 * 1024,
                "mimetypes": ["image/jpeg", "image/png"],
            },
        )
        self.assertEqual(
            capabilities["media"]["video"],
            {
                "enabled": True,
                "max_bytes": 50 * 1024 * 1024,
                "mimetypes": ["video/3gpp", "video/mp4"],
            },
        )
        self.assertNotIn("mimetypes", capabilities["media"]["audio"])
        self.assertEqual(
            capabilities["media"]["audio"],
            {
                "enabled": True,
                "max_bytes": 16 * 1024 * 1024,
                "caption": False,
                "recording_mimetypes": [
                    "audio/ogg;codecs=opus",
                    "audio/mp4",
                ],
                "voice_note_mimetypes": ["audio/ogg;codecs=opus"],
                "max_duration_seconds": 900,
            },
        )
        self.assertNotIn("mimetypes", capabilities["media"]["document"])
        self.assertEqual(
            capabilities["conversation_types"]["group"],
            {
                "send_message": True,
                "sender_signature": True,
                "media": capabilities["media"],
                "outbound_structured_content": capabilities[
                    "outbound_structured_content"
                ],
                "reply": True,
                "reply_requires_participant": True,
                "react": True,
                "edit_message": True,
                "delete_message": True,
                "delivery_receipts": True,
            },
        )
        extension = capabilities["extensions"]["provider.wuzapi"]
        self.assertTrue(extension["transport_enabled"])
        self.assertNotIn("implementation_phase", extension)
        self.assertEqual(extension["inbound_media_mode"], "manual_download")
        self.assertEqual(extension["baseline_version"], WUZAPI_VERSION)
        self.assertEqual(extension["baseline_commit"], WUZAPI_COMMIT)

    def test_sender_signature_is_rendered_only_at_the_wuzapi_boundary(self):
        signature = {"sender_signature": {"display_name": "Lucas * Zotelli"}}
        send_snapshot = self.adapter.prepare_request_snapshot(
            self.connection,
            self._command(options=signature, text="Olá"),
        )
        self.assertEqual(send_snapshot["payload"]["Body"], "*Lucas Zotelli:*\nOlá")
        self.assertTrue(
            self.adapter.validate_outbound_signature(
                self.connection,
                "Olá",
                signature["sender_signature"],
            )
        )
        with self.assertRaisesRegex(AdapterError, "exceeds the text limit"):
            self.adapter.validate_outbound_signature(
                self.connection,
                "x" * 65536,
                signature["sender_signature"],
            )

        edit_snapshot = self.adapter.prepare_request_snapshot(
            self.connection,
            self._command(
                command_type="edit_message",
                text="Corrigido",
                options={
                    "target_external_message_id": "TARGET-SIGNED-EDIT",
                    "new_text": "Corrigido",
                    **signature,
                },
            ),
        )
        self.assertEqual(
            edit_snapshot["payload"]["Body"],
            "*Lucas Zotelli:*\nCorrigido",
        )
        edit_command = self._command(
            command_type="edit_message",
            text="Corrigido",
            options={
                "target_external_message_id": "TARGET-SIGNED-ECHO",
                "new_text": "Corrigido",
                **signature,
            },
        )
        self.assertTrue(
            self.adapter.matches_outbound_mutation_echo(
                self.connection,
                edit_command,
                {
                    "type": "edit",
                    "new_text": "*Lucas Zotelli:*\nCorrigido",
                },
            )
        )
        self.assertFalse(
            self.adapter.matches_outbound_mutation_echo(
                self.connection,
                edit_command,
                {"type": "edit", "new_text": "Corrigido"},
            ),
            "the signed wire echo is compared at the provider boundary",
        )

        with self.assertRaisesRegex(AdapterError, "sender signature"):
            self.adapter.prepare_request_snapshot(
                self.connection,
                self._command(
                    options={"sender_signature": {"display_name": ""}},
                    text="Inválido",
                ),
            )

    def test_structured_media_capabilities_are_enforced_by_base_helper(self):
        capabilities = self.adapter.get_capabilities(self.connection)

        self.assertEqual(
            enabled_media_kinds(capabilities),
            ("image", "audio", "video", "document"),
        )
        self.assertEqual(
            validate_provider_media_capability(
                capabilities, "image", "image/jpeg", 1024
            ),
            "image/jpeg",
        )
        self.assertEqual(
            validate_provider_media_capability(
                capabilities, "audio", "audio/wav", 1024
            ),
            "audio/wav",
        )
        with self.assertRaises(UserError):
            validate_provider_media_capability(
                capabilities, "image", "image/webp", 1024
            )
        with self.assertRaises(UserError):
            validate_provider_media_capability(
                capabilities, "video", "video/webm", 1024
            )
        with self.assertRaises(ValidationError):
            validate_provider_media_capability(
                capabilities,
                "image",
                "image/jpeg",
                16 * 1024 * 1024 + 1,
            )

    def test_hmac_authentication_uses_exact_raw_bytes(self):
        body = b'{"type":"Message", "event":{}}\n'
        signature = hmac.new(
            self.connection.wuzapi_hmac_secret.encode(), body, hashlib.sha256
        ).hexdigest()

        self.assertTrue(
            self.adapter.authenticate_webhook(
                self.connection,
                {"X-HMAC-SIGNATURE": signature.upper()},
                body,
            )
        )
        self.assertFalse(
            self.adapter.authenticate_webhook(
                self.connection,
                {"x-hmac-signature": signature},
                body.rstrip(),
            )
        )
        self.assertFalse(self.adapter.authenticate_webhook(self.connection, {}, body))
        self.assertFalse(
            self.adapter.authenticate_webhook(
                self.connection,
                {"x-hmac-signature": "not-hex"},
                body,
            )
        )

    def test_hmac_authentication_accepts_private_pending_rotation_key(self):
        body = b'{"type":"Message","event":{"rotation":true}}'
        pending_secret = "pending-wuzapi-webhook-secret-at-least-32-chars"
        signature = hmac.new(pending_secret.encode(), body, hashlib.sha256).hexdigest()

        self.assertTrue(
            self.adapter.authenticate_webhook_secrets(
                (self.connection.wuzapi_hmac_secret, pending_secret),
                {"x-hmac-signature": signature},
                body,
            )
        )
        self.assertFalse(
            self.adapter.authenticate_webhook(
                self.connection,
                {"x-hmac-signature": signature},
                body,
            )
        )

    def test_call_control_events_share_an_opaque_ref_and_safe_remote_identity(self):
        expected_ref = hashlib.sha256(
            ("%s\0synthetic-secret-call-id-1" % self.connection.external_ref).encode(
                "utf-8"
            )
        ).hexdigest()
        expected_states = (
            ("call_offer.json", "offered"),
            ("call_accept.json", "accepted"),
            ("call_terminate.json", "terminated"),
        )
        event_ids = set()
        for fixture_name, expected_state in expected_states:
            with self.subTest(fixture_name=fixture_name):
                event = self.adapter.normalize_event(
                    self.connection, self.load_fixture(fixture_name)
                )

                self.assertEqual(event.event_type, "conversation.call.updated")
                self.assertEqual(event.direction, "inbound")
                self.assertFalse(event.is_from_me)
                self.assertEqual(event.origin, "provider")
                self.assertIsNone(event.message)
                self.assertEqual(event.conversation.conversation_type, "direct")
                self.assertEqual(
                    event.conversation_ref,
                    "5511999999999@s.whatsapp.net",
                )
                self.assertEqual(
                    event.extensions["call"],
                    {
                        "ref": expected_ref,
                        "state": expected_state,
                        "direction": "inbound",
                    },
                )
                self.assertEqual(
                    {
                        (address.namespace, address.role, address.value_normalized)
                        for address in event.conversation.addresses
                    },
                    {
                        (
                            "whatsapp.pn",
                            "primary",
                            "5511999999999@s.whatsapp.net",
                        ),
                        (
                            "whatsapp.lid",
                            "alternate",
                            "100000000000001@lid",
                        ),
                    },
                )
                self.assertEqual(
                    {
                        (address.namespace, address.role, address.value_normalized)
                        for address in event.actor.addresses
                    },
                    {
                        (
                            "whatsapp.pn",
                            "sender",
                            "5511999999999@s.whatsapp.net",
                        ),
                        (
                            "whatsapp.lid",
                            "alternate",
                            "100000000000001@lid",
                        ),
                    },
                )
                serialized = json.dumps(event.to_dict(), sort_keys=True)
                for forbidden in (
                    "synthetic-secret-call-id-1",
                    "SENTINEL-CALL",
                    "synthetic-version",
                    "synthetic-private-reason",
                    "RemotePlatform",
                    "RemoteVersion",
                    "Reason",
                    "Data",
                ):
                    self.assertNotIn(forbidden, serialized)
                event_ids.add(event.event_id)

        self.assertEqual(len(event_ids), 3)

    def test_call_direction_never_projects_the_accounts_own_identity_as_guest(self):
        self._record_own_call_identity(
            "5511888888888@s.whatsapp.net", "200000000000002@lid"
        )
        envelope = self.load_fixture("call_offer.json")
        envelope["event"].update(
            {
                "CallCreator": "5511888888888@s.whatsapp.net",
                "CallCreatorAlt": "200000000000002@lid",
            }
        )

        event = self.adapter.normalize_event(self.connection, envelope)

        self.assertEqual(event.extensions["call"]["direction"], "outbound")
        self.assertEqual(len(event.conversation.addresses), 1)
        self.assertEqual(len(event.actor.addresses), 1)
        serialized = json.dumps(event.to_dict(), sort_keys=True)
        self.assertNotIn("5511888888888", serialized)
        self.assertNotIn("200000000000002", serialized)

        unknown = copy.deepcopy(envelope)
        unknown["event"].update(
            {
                "CallCreator": "300000000000003@lid",
                "CallCreatorAlt": "5511777777777@s.whatsapp.net",
            }
        )
        normalized_unknown = self.adapter.normalize_event(self.connection, unknown)
        self.assertEqual(normalized_unknown.extensions["call"]["direction"], "unknown")
        self.assertEqual(len(normalized_unknown.conversation.addresses), 1)

    def test_call_control_events_reject_group_or_own_remote_identity(self):
        group = self.load_fixture("call_offer.json")
        group["event"]["GroupJID"] = "120363000000000000@g.us"
        with self.assertRaises(UnsupportedEventError):
            self.adapter.normalize_event(self.connection, group)

        self._record_own_call_identity(
            "5511999999999@s.whatsapp.net", "100000000000001@lid"
        )
        own_remote = self.load_fixture("call_offer.json")
        with self.assertRaises(UnsupportedEventError):
            self.adapter.normalize_event(self.connection, own_remote)

    def test_identity_change_is_a_direct_provider_neutral_security_event(self):
        envelope = self.load_fixture("identity_change.json")

        event = self.adapter.normalize_event(self.connection, envelope)
        equivalent = self.adapter.normalize_event(
            self.connection, copy.deepcopy(envelope)
        )

        self.assertEqual(event.event_id, equivalent.event_id)
        self.assertNotIn("5511999999999", event.event_id)
        self.assertEqual(event.event_type, "identity.security.changed")
        self.assertEqual(event.direction, "inbound")
        self.assertFalse(event.is_from_me)
        self.assertEqual(event.origin, "provider")
        self.assertIsNone(event.message)
        self.assertEqual(event.conversation_ref, "5511999999999@s.whatsapp.net")
        self.assertEqual(event.conversation.conversation_type, "direct")
        self.assertEqual(
            event.extensions["identity_security"],
            {"change_kind": "primary_device", "implicit": True},
        )
        self.assertEqual(
            [address.role for address in event.conversation.addresses], ["primary"]
        )
        self.assertEqual(
            [address.role for address in event.actor.addresses], ["sender"]
        )

        group = copy.deepcopy(envelope)
        group["event"]["JID"] = "120363000000000000@g.us"
        with self.assertRaises(UnsupportedEventError):
            self.adapter.normalize_event(self.connection, group)

        invalid = copy.deepcopy(envelope)
        invalid["event"].pop("Implicit")
        with self.assertRaises(AdapterError):
            self.adapter.normalize_event(self.connection, invalid)

    def test_normalize_inbound_text_preserves_pn_lid_and_reply(self):
        envelope = self.load_fixture("message_text_lid.json")
        envelope["base64"] = "media-must-not-enter-the-dto"

        event = self.adapter.normalize_event(self.connection, envelope)

        self.assertEqual(event.event_type, "message.created")
        self.assertEqual(event.direction, "inbound")
        self.assertFalse(event.is_from_me)
        self.assertEqual(event.origin, "provider")
        self.assertEqual(event.account_ref, self.account.external_ref)
        self.assertEqual(event.connection_ref, self.connection.external_ref)
        self.assertEqual(event.provider_schema_version, "v1.0.8")
        self.assertEqual(
            event.occurred_at,
            datetime.datetime(2026, 8, 21, 18, 4, 5, tzinfo=datetime.timezone.utc),
        )
        self.assertEqual(event.actor.display_name, "Pessoa de teste")
        actor_addresses = {
            (address.namespace, address.value, address.value_normalized)
            for address in event.actor.addresses
        }
        self.assertIn(
            (
                "whatsapp.lid",
                "100000000000001:7@lid",
                "100000000000001@lid",
            ),
            actor_addresses,
        )
        self.assertIn(
            (
                "whatsapp.pn",
                "5511999999999@s.whatsapp.net",
                "5511999999999@s.whatsapp.net",
            ),
            actor_addresses,
        )
        conversation_addresses = {
            (address.namespace, address.value_normalized)
            for address in event.conversation.addresses
        }
        self.assertEqual(
            conversation_addresses,
            {
                ("whatsapp.lid", "100000000000001@lid"),
                ("whatsapp.pn", "5511999999999@s.whatsapp.net"),
            },
        )
        self.assertEqual(
            {
                address.namespace: address.resolution_scope
                for address in event.actor.addresses
            },
            {
                "whatsapp.lid": "account",
                "whatsapp.pn": "company",
            },
        )
        self.assertEqual(
            {
                address.namespace: address.resolution_scope
                for address in event.conversation.addresses
            },
            {
                "whatsapp.lid": "account",
                "whatsapp.pn": "company",
            },
        )
        self.assertEqual(event.conversation_ref, "100000000000001@lid")
        self.assertEqual(event.message.text, "Mensagem de teste 👍")
        self.assertEqual(
            event.message.external_message_id,
            "3EB0A1B2C3D4E5F60718293A4B5C6D7E",
        )
        self.assertEqual(
            event.message.reply_to_external_id,
            "3EB00000000000000000000000000000",
        )
        self.assertEqual(
            event.reply_to["participant_normalized"],
            "5511999999999@s.whatsapp.net",
        )
        snapshot = event.message.protocol_snapshot
        self.assertEqual(snapshot["source"]["sender"], "100000000000001:7@lid")
        self.assertEqual(snapshot["source"]["sender_normalized"], "100000000000001@lid")
        self.assertNotIn("media-must-not-enter-the-dto", repr(event.to_dict()))

    def test_message_direction_is_required_and_never_defaults_to_inbound(self):
        for invalid_direction in (None, "", "unknown", 2, {}, []):
            with self.subTest(invalid_direction=invalid_direction):
                envelope = self.load_fixture("message_text_lid.json")
                envelope["event"]["Info"]["IsFromMe"] = invalid_direction
                with self.assertRaisesRegex(AdapterError, "IsFromMe"):
                    self.adapter.normalize_event(self.connection, envelope)

        missing = self.load_fixture("message_text_lid.json")
        missing["event"]["Info"].pop("IsFromMe")
        with self.assertRaisesRegex(AdapterError, "IsFromMe"):
            self.adapter.normalize_event(self.connection, missing)

        textual = self.load_fixture("message_text_lid.json")
        textual["event"]["Info"]["IsFromMe"] = "true"
        event = self.adapter.normalize_event(self.connection, textual)
        self.assertTrue(event.is_from_me)
        self.assertEqual(event.direction, "outbound")

    def test_normalize_meta_ctwa_attribution_is_provider_neutral_and_bounded(self):
        envelope = self.load_fixture("message_text_lid.json")
        context = envelope["event"]["Message"]["extendedTextMessage"]["contextInfo"]
        context.update(
            {
                "externalAdReply": {
                    "sourceType": "ad",
                    "sourceApp": "Instagram",
                    "sourceID": "synthetic-meta-source-17",
                    "ctwaClid": "synthetic-ctwa-click-17",
                    "sourceURL": "https://www.instagram.com/p/synthetic/",
                    "mediaType": "video",
                    "showAdAttribution": True,
                    "ctwaPayload": "opaque-ctwa-payload",
                },
                "conversionSource": "FB_Ads",
                "conversionDelaySeconds": 3,
                "entryPointConversionSource": "ctwa_ad",
                "entryPointConversionApp": "Instagram",
                "entryPointConversionDelaySeconds": 15,
                "conversionData": "opaque-conversion-data",
                "ctwaSignals": "opaque-ctwa-signals",
                "alwaysShowAdAttribution": False,
                "utm": {
                    "source": "instagram",
                    "campaign": "synthetic-campaign",
                },
            }
        )

        event = self.adapter.normalize_event(self.connection, envelope)

        self.assertEqual(len(event.attribution), 1)
        attribution = event.attribution[0]
        self.assertEqual(attribution.touchpoint_type, "paid_ad_click")
        self.assertEqual(attribution.evidence_level, "provider_asserted")
        self.assertEqual(attribution.network, "meta")
        self.assertEqual(attribution.source_platform, "instagram")
        self.assertEqual(attribution.source_type, "ad")
        self.assertEqual(
            attribution.source_url,
            "https://www.instagram.com/p/synthetic/",
        )
        self.assertEqual(
            {
                (identifier.namespace, identifier.role, identifier.value)
                for identifier in attribution.external_identifiers
            },
            {
                ("meta.source_id", "ad_source", "synthetic-meta-source-17"),
                ("meta.ctwa_clid", "click", "synthetic-ctwa-click-17"),
            },
        )
        self.assertEqual(
            attribution.entry_point,
            {
                "source": "ctwa_ad",
                "app": "instagram",
                "conversion_source": "fb_ads",
                "delay_seconds": 15,
                "conversion_delay_seconds": 3,
            },
        )
        self.assertEqual(attribution.creative, {"media_type": "video"})
        self.assertEqual(
            attribution.flags,
            {
                "show_ad_attribution": True,
                "always_show_ad_attribution": False,
            },
        )
        self.assertEqual(
            attribution.utm,
            {
                "source": "instagram",
                "campaign": "synthetic-campaign",
            },
        )
        fingerprints = attribution.provider_extensions["provider.wuzapi"][
            "opaque_fingerprints"
        ]
        self.assertEqual(
            fingerprints,
            {
                "conversion_data": hashlib.sha256(
                    b"opaque-conversion-data"
                ).hexdigest(),
                "ctwa_payload": hashlib.sha256(b"opaque-ctwa-payload").hexdigest(),
                "ctwa_signals": hashlib.sha256(b"opaque-ctwa-signals").hexdigest(),
            },
        )
        self.assertNotIn("opaque-conversion-data", repr(event.to_dict()))
        self.assertNotIn("opaque-ctwa-payload", repr(event.to_dict()))
        self.assertNotIn("opaque-ctwa-signals", repr(event.to_dict()))

        source_only = copy.deepcopy(envelope)
        source_only_context = source_only["event"]["Message"]["extendedTextMessage"][
            "contextInfo"
        ]
        source_only_external = source_only_context["externalAdReply"]
        source_only_external.pop("ctwaClid")
        source_only_context.pop("entryPointConversionSource")
        source_only_context.pop("conversionSource")
        source_only_event = self.adapter.normalize_event(self.connection, source_only)
        self.assertEqual(
            source_only_event.attribution[0].touchpoint_type,
            "paid_ad_click",
        )
        self.assertEqual(
            [
                identifier.namespace
                for identifier in source_only_event.attribution[0].external_identifiers
            ],
            ["meta.source_id"],
        )

    def test_phone_jid_must_be_numeric_before_it_becomes_company_portable(self):
        numeric = _address("5511999999999@s.whatsapp.net", "primary", "event.Info.Chat")
        opaque = _address("not-a-phone@s.whatsapp.net", "primary", "event.Info.Chat")

        self.assertEqual(
            (numeric.namespace, numeric.resolution_scope), ("whatsapp.pn", "company")
        )
        self.assertEqual(
            (opaque.namespace, opaque.resolution_scope), ("whatsapp.jid", "account")
        )
        self.assertIsNone(_address("5511999999999", "primary", "event.Info.Chat"))
        self.assertIsNone(
            _address(
                "%s@lid" % ("1" * 252),
                "primary",
                "event.Info.Chat",
            )
        )

    def test_complementary_message_contexts_form_one_attribution_touchpoint(self):
        envelope = self.load_fixture("message_text_lid.json")
        extended = envelope["event"]["Message"]["extendedTextMessage"]
        extended["contextInfo"].update(
            {
                "externalAdReply": {
                    "sourceType": "ad",
                    "sourceID": "preferred-source-id",
                },
                "utm": {"source": "preferred-source"},
            }
        )
        extended["messageContextInfo"] = {
            "externalAdReply": {
                "sourceID": "conflicting-secondary-source-id",
                "sourceApp": "Facebook",
                "ctwaClid": "secondary-click-id",
                "mediaType": "image",
            },
            "entryPointConversionSource": "ctwa_ad",
            "utm": {
                "source": "conflicting-secondary-source",
                "medium": "paid_social",
                "campaign": "secondary-campaign",
            },
        }

        event = self.adapter.normalize_event(self.connection, envelope)

        self.assertEqual(len(event.attribution), 1)
        attribution = event.attribution[0]
        self.assertEqual(attribution.touchpoint_type, "paid_ad_click")
        self.assertEqual(attribution.source_platform, "facebook")
        self.assertEqual(attribution.creative, {"media_type": "image"})
        self.assertEqual(
            attribution.utm,
            {
                "source": "preferred-source",
                "medium": "paid_social",
                "campaign": "secondary-campaign",
            },
        )
        self.assertEqual(
            {
                identifier.namespace: identifier.value
                for identifier in attribution.external_identifiers
            },
            {
                "meta.source_id": "preferred-source-id",
                "meta.ctwa_clid": "secondary-click-id",
            },
        )

    def test_utm_only_is_preserved_without_claiming_paid_media(self):
        envelope = self.load_fixture("message_text_lid.json")
        context = envelope["event"]["Message"]["extendedTextMessage"]["contextInfo"]
        context["utm"] = {
            "source": "newsletter",
            "medium": "link",
            "campaign": "synthetic-organic-entry",
        }

        event = self.adapter.normalize_event(self.connection, envelope)

        self.assertEqual(len(event.attribution), 1)
        attribution = event.attribution[0]
        self.assertEqual(attribution.touchpoint_type, "unknown")
        self.assertEqual(attribution.evidence_level, "observed")
        self.assertEqual(attribution.utm, context["utm"])

    def test_attribution_delay_larger_than_database_integer_is_ignored(self):
        envelope = self.load_fixture("message_text_lid.json")
        context = envelope["event"]["Message"]["extendedTextMessage"]["contextInfo"]
        context.update(
            {
                "entryPointConversionSource": "ctwa_ad",
                "entryPointConversionDelaySeconds": str(2**63),
            }
        )

        event = self.adapter.normalize_event(self.connection, envelope)

        self.assertEqual(event.attribution[0].touchpoint_type, "paid_ad_click")
        self.assertNotIn("delay_seconds", event.attribution[0].entry_point)

    def test_attribution_survives_unsupported_content_without_exposing_opaque_data(
        self,
    ):
        envelope = self.load_fixture("message_text_lid.json")
        envelope["event"]["Message"] = {
            "productMessage": {
                "product": {"productImageCount": 1},
                "contextInfo": {
                    "conversionSource": "FB_Ads",
                    "conversionData": "opaque-product-conversion-data",
                },
            }
        }

        event = self.adapter.normalize_event(self.connection, envelope)

        self.assertEqual(event.event_type, "message.created")
        self.assertEqual(event.message.content_type, "unsupported")
        self.assertEqual(event.message.text, "[Conteúdo do WhatsApp não suportado]")
        self.assertFalse(event.message.media)
        self.assertEqual(len(event.attribution), 1)
        attribution = event.attribution[0]
        self.assertEqual(attribution.touchpoint_type, "paid_ad_signal")
        self.assertEqual(attribution.evidence_level, "provider_hint")
        self.assertEqual(
            attribution.entry_point,
            {"conversion_source": "fb_ads"},
        )
        self.assertEqual(
            attribution.provider_extensions["provider.wuzapi"]["opaque_fingerprints"][
                "conversion_data"
            ],
            hashlib.sha256(b"opaque-product-conversion-data").hexdigest(),
        )
        self.assertNotIn("opaque-product-conversion-data", repr(event.to_dict()))

    def test_non_paid_entry_points_are_never_promoted_to_paid_media(self):
        for entry_point in (
            "click_to_chat_link",
            "global_search_new_chat",
            "phone_number_hyperlink",
            "status",
        ):
            with self.subTest(entry_point=entry_point):
                envelope = self.load_fixture("message_text_lid.json")
                context = envelope["event"]["Message"]["extendedTextMessage"][
                    "contextInfo"
                ]
                context.update(
                    {
                        "entryPointConversionSource": entry_point,
                        "entryPointConversionApp": "WhatsApp",
                        "entryPointConversionDelaySeconds": 0,
                    }
                )

                event = self.adapter.normalize_event(self.connection, envelope)

                self.assertEqual(len(event.attribution), 1)
                attribution = event.attribution[0]
                self.assertEqual(attribution.touchpoint_type, "entry_point")
                self.assertEqual(
                    attribution.evidence_level,
                    "provider_asserted_non_paid",
                )
                self.assertEqual(
                    attribution.entry_point,
                    {
                        "source": entry_point,
                        "app": "whatsapp",
                        "delay_seconds": 0,
                    },
                )

    def test_empty_outbound_or_quoted_ad_context_creates_no_touchpoint(self):
        outbound = self.load_fixture("message_text_lid.json")
        outbound["event"]["Info"]["IsFromMe"] = True
        outbound["event"]["Message"] = {
            "extendedTextMessage": {
                "contextInfo": {
                    "externalAdReply": {},
                }
            }
        }
        with self.assertRaises(UnsupportedEventError):
            self.adapter.normalize_event(self.connection, outbound)

        meaningful_outbound = copy.deepcopy(outbound)
        meaningful_outbound["event"]["Message"]["extendedTextMessage"]["contextInfo"][
            "externalAdReply"
        ] = {
            "sourceType": "ad",
            "sourceID": "must-not-be-attributed-from-own-message",
        }
        with self.assertRaises(UnsupportedEventError):
            self.adapter.normalize_event(self.connection, meaningful_outbound)
        supported_own_message = copy.deepcopy(meaningful_outbound)
        supported_own_message["event"]["Message"]["extendedTextMessage"][
            "text"
        ] = "Mensagem própria"
        own_event = self.adapter.normalize_event(self.connection, supported_own_message)
        self.assertFalse(own_event.attribution)

        quoted = self.load_fixture("message_text_lid.json")
        context = quoted["event"]["Message"]["extendedTextMessage"]["contextInfo"]
        context["quotedMessage"] = {
            "extendedTextMessage": {
                "text": "quoted",
                "contextInfo": {
                    "externalAdReply": {
                        "sourceType": "ad",
                        "sourceID": "quoted-message-source-id",
                    }
                },
            }
        }
        event = self.adapter.normalize_event(self.connection, quoted)
        self.assertFalse(event.attribution)

    def test_normalize_from_me_echo_correlates_provider_and_client_id(self):
        envelope = self.load_fixture("message_text_lid.json")
        info = envelope["event"]["Info"]
        info.update(
            {
                "Chat": "5511999999999@s.whatsapp.net",
                "Sender": "5511888888888@s.whatsapp.net",
                "SenderAlt": "100000000000002@lid",
                "RecipientAlt": "100000000000001@lid",
                "IsFromMe": True,
            }
        )
        envelope["event"]["Message"] = {"conversation": "Eco de saída"}

        event = self.adapter.normalize_event(self.connection, envelope)

        self.assertTrue(event.is_from_me)
        self.assertEqual(event.direction, "outbound")
        self.assertEqual(event.origin, "external_device")
        self.assertEqual(
            event.message.client_message_id, event.message.external_message_id
        )
        self.assertIn(
            ("whatsapp.lid", "100000000000001@lid"),
            {
                (address.namespace, address.value_normalized)
                for address in event.conversation.addresses
            },
        )

    def test_normalize_all_supported_media_and_reply_metadata(self):
        cases = (
            (
                "message_image.json",
                "image",
                "Foto recebida",
                "image/jpeg",
                13,
                "8810c61dc998e2ef39791e76b6377bcb78d6b99809e08aeff51ba98216529ece",
            ),
            (
                "message_audio.json",
                "audio",
                "",
                "audio/ogg; codecs=opus",
                13,
                "c8c89a7283fe10583f08619ed19d685da4a31d2d378e15d2487e62453f022305",
            ),
            (
                "message_video.json",
                "video",
                "Vídeo recebido",
                "video/mp4",
                13,
                "cf5664a86426afa42799efd6c0e8b2e9df0ef34c9024dc89e132032872edf938",
            ),
            (
                "message_document.json",
                "document",
                "Documento recebido",
                "application/pdf",
                16,
                "c9c3e86d586428a1c98966e171b8c88c8bf3c907a3d188c638a546b6d7fee7be",
            ),
        )
        for fixture, kind, text, mime_type, size, sha256 in cases:
            with self.subTest(kind=kind):
                event = self.adapter.normalize_event(
                    self.connection, self.load_fixture(fixture)
                )
                self.assertEqual(event.event_type, "message.created")
                self.assertEqual(event.message.content_type, kind)
                self.assertEqual(event.message.text, text)
                self.assertEqual(len(event.message.media), 1)
                media = event.message.media[0]
                self.assertIsInstance(media, MediaDTO)
                self.assertEqual(media.kind, kind)
                self.assertEqual(media.mime_type, mime_type)
                self.assertEqual(media.size_bytes, size)
                self.assertEqual(media.sha256, sha256)
                self.assertTrue(media.remote_locator["url"].startswith("https://"))
                self.assertIn("media_key", media.remote_locator)
                self.assertNotIn("base64", repr(event.to_dict()).lower())

        dwg_envelope = self.load_fixture("message_document.json")
        document = dwg_envelope["event"]["Message"]["documentMessage"]
        document.update({"mimetype": "image/vnd.dwg", "fileName": "projeto.dwg"})
        dwg_media = self.adapter.normalize_event(
            self.connection, dwg_envelope
        ).message.media[0]
        self.assertEqual(dwg_media.kind, "document")
        self.assertEqual(dwg_media.mime_type, "image/vnd.dwg")
        self.assertEqual(
            validate_media_metadata(
                dwg_media.kind, dwg_media.mime_type, dwg_media.size_bytes
            ),
            "image/vnd.dwg",
        )
        image = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_image.json")
        )
        self.assertEqual(
            image.reply_to["external_message_id"],
            "3EB00000000000000000000000000000",
        )
        audio = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_audio.json")
        ).message.media[0]
        self.assertTrue(audio.is_voice_note)
        self.assertEqual(audio.duration_seconds, 7)

    def test_forwarding_uses_only_explicit_active_context_evidence(self):
        forwarded_image = self.load_fixture("message_image.json")
        image_context = forwarded_image["event"]["Message"]["imageMessage"][
            "contextInfo"
        ]
        image_context.update({"isForwarded": True, "forwardingScore": 1})
        image = self.adapter.normalize_event(self.connection, forwarded_image).message
        self.assertTrue(image.is_forwarded)
        self.assertEqual(image.forwarding_score, 1)

        flag_without_score = self.load_fixture("message_text_lid.json")
        text_context = flag_without_score["event"]["Message"]["extendedTextMessage"][
            "contextInfo"
        ]
        text_context["Is_Forwarded"] = True
        text = self.adapter.normalize_event(self.connection, flag_without_score).message
        self.assertTrue(text.is_forwarded)
        self.assertIsNone(text.forwarding_score)

        score_only = self.load_fixture("message_image.json")
        score_only["event"]["Message"]["imageMessage"]["contextInfo"].update(
            {"forwardingScore": 127}
        )
        score_message = self.adapter.normalize_event(
            self.connection, score_only
        ).message
        self.assertFalse(score_message.is_forwarded)
        self.assertEqual(score_message.forwarding_score, 127)

        quoted_only = self.load_fixture("message_text_lid.json")
        quoted_context = quoted_only["event"]["Message"]["extendedTextMessage"][
            "contextInfo"
        ]
        quoted_context["quotedMessage"] = {
            "extendedTextMessage": {
                "text": "Forwarded quoted content",
                "contextInfo": {"isForwarded": True, "forwardingScore": 2},
            }
        }
        quoted = self.adapter.normalize_event(self.connection, quoted_only).message
        self.assertFalse(quoted.is_forwarded)
        self.assertIsNone(quoted.forwarding_score)

        associated = self.load_fixture("message_associated_child_video.json")
        child_context = associated["event"]["Message"]["associatedChildMessage"][
            "message"
        ]["videoMessage"]["contextInfo"]
        child_context.update({"isForwarded": True, "forwardingScore": 2})
        child = self.adapter.normalize_event(self.connection, associated).message
        self.assertTrue(child.is_forwarded)
        self.assertEqual(child.forwarding_score, 2)

        for invalid_score in (True, "1", -1, 128):
            with self.subTest(invalid_score=invalid_score):
                malformed = self.load_fixture("message_image.json")
                malformed["event"]["Message"]["imageMessage"]["contextInfo"].update(
                    {"isForwarded": True, "forwardingScore": invalid_score}
                )
                normalized = self.adapter.normalize_event(
                    self.connection, malformed
                ).message
                self.assertTrue(normalized.is_forwarded)
                self.assertIsNone(normalized.forwarding_score)

    @mock.patch(REQUEST_PATCH)
    def test_structured_outbound_uses_pinned_handlers_and_stable_ids(self, request):
        cases = (
            (
                {
                    "type": "buttons",
                    "title": "Atendimento",
                    "footer": "Equipe",
                    "buttons": [
                        {"type": "reply", "id": "sales", "title": "Vendas"},
                        {
                            "type": "url",
                            "url": "https://example.com/catalog",
                            "title": "Catálogo",
                        },
                        {"type": "phone", "phone": "+5511999999999", "title": "Ligar"},
                    ],
                },
                "/chat/send/buttons",
                {
                    "Body": "Escolha uma opção",
                    "Title": "Atendimento",
                    "Footer": "Equipe",
                    "Buttons": [
                        {"type": "reply", "id": "sales", "title": "Vendas"},
                        {
                            "type": "cta_url",
                            "url": "https://example.com/catalog",
                            "title": "Catálogo",
                        },
                        {
                            "type": "cta_call",
                            "phone_number": "+5511999999999",
                            "title": "Ligar",
                        },
                    ],
                },
            ),
            (
                {
                    "type": "list",
                    "title": "Horários",
                    "footer": "Equipe",
                    "button_text": "Ver horários",
                    "sections": [
                        {
                            "title": "Manhã",
                            "rows": [
                                {
                                    "id": "slot-9",
                                    "title": "09:00",
                                    "description": "Horário local",
                                }
                            ],
                        },
                    ],
                },
                "/chat/send/list",
                {
                    "Desc": "Escolha uma opção",
                    "TopText": "Horários",
                    "FooterText": "Equipe",
                    "ButtonText": "Ver horários",
                    "Sections": [
                        {
                            "title": "Manhã",
                            "rows": [
                                {
                                    "RowId": "slot-9",
                                    "title": "09:00",
                                    "desc": "Horário local",
                                }
                            ],
                        },
                    ],
                },
            ),
            (
                {
                    "type": "contacts",
                    "contacts": [
                        {
                            "name": "Pessoa; Exemplo",
                            "phones": ["+5511999999999"],
                            "emails": ["person@example.com"],
                        }
                    ],
                },
                "/chat/send/contact",
                {
                    "Name": "Pessoa; Exemplo",
                    "Vcard": (
                        "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Pessoa\\; Exemplo\r\n"
                        "TEL;TYPE=CELL:+5511999999999\r\n"
                        "EMAIL;TYPE=INTERNET:person@example.com\r\nEND:VCARD\r\n"
                    ),
                },
            ),
            (
                {
                    "type": "location",
                    "latitude": -22.7253,
                    "longitude": -47.6492,
                    "name": "Entrada principal",
                },
                "/chat/send/location",
                {
                    "Latitude": -22.7253,
                    "Longitude": -47.6492,
                    "Name": "Entrada principal",
                },
            ),
        )
        for content, endpoint, expected in cases:
            for conversation_type in ("direct", "group"):
                with self.subTest(kind=content["type"], conversation=conversation_type):
                    request.reset_mock()
                    request.return_value = FakeResponse(
                        200, {"success": True, "data": {"Id": CLIENT_MESSAGE_ID}}
                    )
                    command = self._command(
                        structured_content=content,
                        text="Escolha uma opção"
                        if content["type"] in ("buttons", "list")
                        else "",
                        conversation_type=conversation_type,
                    )

                    result = self.adapter.execute_command(self.connection, command)

                    self.assertEqual(result.status, "success")
                    self.assertEqual(result.external_message_id, CLIENT_MESSAGE_ID)
                    self.assertEqual(
                        request.call_args.args[1],
                        self.connection.wuzapi_base_url + endpoint,
                    )
                    self.assertEqual(
                        request.call_args.kwargs["json"],
                        dict(
                            expected,
                            Phone=command.target_address.value_normalized,
                            Id=CLIENT_MESSAGE_ID,
                        ),
                    )
                    self.assertFalse(request.call_args.kwargs["allow_redirects"])

    @mock.patch(REQUEST_PATCH)
    def test_structured_outbound_rejects_provider_limitations_before_dispatch(
        self, request
    ):
        contact = {"name": "Pessoa", "phones": ["+5511999999999"], "emails": []}
        cases = (
            (
                {"type": "contacts", "contacts": [contact, contact]},
                "contact count limit",
            ),
            ({"type": "location", "latitude": 0, "longitude": 20}, "longitude zero"),
            ({"type": "location", "latitude": 20, "longitude": 0}, "longitude zero"),
            (
                {"type": "location", "latitude": 20, "longitude": 30, "live": True},
                "live locations",
            ),
            (
                {"type": "selection", "id": "button-a", "title": "Opção A"},
                "not sendable",
            ),
        )
        for content, reason in cases:
            with self.subTest(kind=content["type"]):
                with self.assertRaisesRegex(AdapterError, reason):
                    self.adapter.validate_outbound_structured_content(
                        self.connection, content, ""
                    )
                with self.assertRaisesRegex(AdapterError, reason):
                    self.adapter._build_send_request(
                        self._command(structured_content=content, text="")
                    )
        request.assert_not_called()

    @mock.patch(REQUEST_PATCH)
    def test_neutral_cards_above_wuz_limits_are_rejected_before_http(self, request):
        capabilities = self.adapter.get_capabilities(self.connection)
        self.assertEqual(
            capabilities["outbound_structured_content"], OUTBOUND_STRUCTURED_CONTENT
        )
        self.assertNotIn("structured_content", capabilities)
        button = {"type": "reply", "id": "yes", "title": "Yes"}
        cases = [
            (
                {
                    "type": "buttons",
                    "buttons": [{"type": "phone", "phone": "1" * 31, "title": "Call"}],
                },
                "Choose",
            ),
            (
                {
                    "type": "buttons",
                    "buttons": [dict(button, id=str(i)) for i in range(4)],
                },
                "Choose",
            ),
            ({"type": "buttons", "buttons": [dict(button, title="A" * 21)]}, "Choose"),
            ({"type": "buttons", "buttons": [dict(button, id="A" * 201)]}, "Choose"),
            ({"type": "buttons", "title": "A" * 61, "buttons": [button]}, "Choose"),
            ({"type": "buttons", "buttons": [button]}, "A" * 1025),
            (
                {
                    "type": "list",
                    "button_text": "Choose",
                    "sections": [
                        {
                            "title": "Options",
                            "rows": [{"id": str(i), "title": "Row"} for i in range(11)],
                        }
                    ],
                },
                "Choose",
            ),
            (
                {
                    "type": "list",
                    "button_text": "Choose",
                    "sections": [
                        {"title": "Options", "rows": [{"id": "one", "title": "A" * 25}]}
                    ],
                },
                "Choose",
            ),
            (
                {"type": "contacts", "contacts": [{"name": "Contact"}]},
                "Caption must not disappear",
            ),
            ({"type": "contacts", "contacts": [{"name": "A" * 121}]}, ""),
            (
                {"type": "location", "latitude": 1, "longitude": 2},
                "Caption must not disappear",
            ),
        ]
        for content, body in cases:
            with self.subTest(kind=content["type"], body_length=len(body)):
                MessageDTO(structured_content=content)
                command = self._command(structured_content=content, text=body)
                with self.assertRaises(AdapterError):
                    self.adapter.prepare_request_snapshot(self.connection, command)
                with self.assertRaises(AdapterError):
                    self.adapter.execute_command(self.connection, command)
        request.assert_not_called()

    def test_normalize_native_and_legacy_choices_preserves_only_neutral_fields(self):
        cases = (
            (
                {
                    "buttonsMessage": {
                        "Header": {"Text": "Atendimento"},
                        "contentText": "Escolha",
                        "footerText": "Equipe",
                        "buttons": [
                            {
                                "buttonID": "sales",
                                "buttonText": {"displayText": "Vendas"},
                                "type": 1,
                            }
                        ],
                    }
                },
                {
                    "type": "buttons",
                    "title": "Atendimento",
                    "footer": "Equipe",
                    "buttons": [{"type": "reply", "id": "sales", "title": "Vendas"}],
                },
                "Escolha",
            ),
            (
                {
                    "interactiveMessage": {
                        "InteractiveMessage": {
                            "NativeFlowMessage": {
                                "buttons": [
                                    {
                                        "name": "quick_reply",
                                        "buttonParamsJSON": json.dumps(
                                            {
                                                "id": "support",
                                                "display_text": "Suporte",
                                                "untrusted": "opaque-secret",
                                            }
                                        ),
                                    }
                                ]
                            }
                        },
                        "body": {"text": "Como ajudar?"},
                        "header": {"title": "Atendimento"},
                        "footer": {"text": "Equipe"},
                    }
                },
                {
                    "type": "buttons",
                    "title": "Atendimento",
                    "footer": "Equipe",
                    "buttons": [{"type": "reply", "id": "support", "title": "Suporte"}],
                },
                "Como ajudar?",
            ),
            (
                {
                    "listMessage": {
                        "title": "Horários",
                        "description": "Escolha",
                        "buttonText": "Ver opções",
                        "footerText": "Equipe",
                        "sections": [
                            {
                                "title": "Manhã",
                                "rows": [
                                    {
                                        "rowID": "slot-9",
                                        "title": "09:00",
                                        "description": "Horário local",
                                    }
                                ],
                            }
                        ],
                    }
                },
                {
                    "type": "list",
                    "title": "Horários",
                    "footer": "Equipe",
                    "button_text": "Ver opções",
                    "sections": [
                        {
                            "title": "Manhã",
                            "rows": [
                                {
                                    "id": "slot-9",
                                    "title": "09:00",
                                    "description": "Horário local",
                                }
                            ],
                        }
                    ],
                },
                "Escolha",
            ),
            (
                {
                    "interactiveResponseMessage": {
                        "InteractiveResponseMessage": {
                            "NativeFlowResponseMessage": {
                                "name": "quick_reply",
                                "paramsJSON": json.dumps(
                                    {
                                        "id": "sales",
                                        "display_text": "Vendas",
                                        "token": "opaque-secret",
                                    }
                                ),
                            }
                        },
                        "body": {"text": "Seleção"},
                    }
                },
                {"type": "selection", "id": "sales", "title": "Vendas"},
                "Vendas",
            ),
        )
        for payload, expected, body in cases:
            with self.subTest(kind=expected["type"]):
                envelope = self.load_fixture("message_text_lid.json")
                envelope["event"]["Message"] = payload
                event = self.adapter.normalize_event(
                    self.connection, sanitize_webhook_envelope(envelope)
                )
                self.assertEqual(event.message.structured_content, expected)
                self.assertEqual(event.message.content_type, expected["type"])
                self.assertEqual(event.message.text, body)
                self.assertNotIn("opaque-secret", repr(event.to_dict()))
                self.assertNotIn("paramsJSON", repr(event.to_dict()))

    def test_native_selection_malformed_or_unknown_payload_keeps_human_body(self):
        cases = (
            ("quick_reply", "{invalid-json"),
            ("quick_reply", "[]"),
            ("quick_reply", json.dumps({"id": "x" * 201, "display_text": "Escolhi"})),
            ("quick_reply", "x" * 16385),
            ("flow", json.dumps({"id": "flow-token", "display_text": "Escolhi"})),
        )
        for name, params in cases:
            with self.subTest(name=name, size=len(params)):
                envelope = self.load_fixture("message_text_lid.json")
                envelope["event"]["Message"] = {
                    "interactiveResponseMessage": {
                        "InteractiveResponseMessage": {
                            "NativeFlowResponseMessage": {
                                "name": name,
                                "paramsJSON": params,
                            }
                        },
                        "body": {"text": "Escolha recebida"},
                    }
                }
                event = self.adapter.normalize_event(
                    self.connection, sanitize_webhook_envelope(envelope)
                )
                self.assertEqual(event.message.text, "Escolha recebida")
                self.assertFalse(event.message.structured_content)

    def test_vcard_contacts_unfold_decode_and_project_bounded_fields(self):
        envelope = self.load_fixture("message_text_lid.json")
        envelope["event"]["Message"] = {
            "contactMessage": {
                "vcard": (
                    "BEGIN:VCARD\r\nVERSION:3.0\r\n"
                    "FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Jos=C3=A9=\r\n Silva\r\n"
                    "item1.TEL;TYPE=CELL:tel:+55 (11) 99999-9999\r\n"
                    "EMAIL;TYPE=INTERNET:jose@exam\r\n ple.com\r\n"
                    "EMAIL:invalid@example.com?subject=Injected\r\n"
                    "NOTE:opaque-secret\r\nURL:javascript:alert(1)\r\nEND:VCARD\r\n"
                )
            }
        }
        event = self.adapter.normalize_event(
            self.connection, sanitize_webhook_envelope(envelope)
        )
        self.assertEqual(event.message.content_type, "contacts")
        self.assertEqual(
            event.message.structured_content,
            {
                "type": "contacts",
                "contacts": [
                    {
                        "name": "José Silva",
                        "phones": ["+5511999999999"],
                        "emails": ["jose@example.com"],
                    }
                ],
            },
        )
        self.assertNotIn("opaque-secret", repr(event.to_dict()))
        self.assertNotIn("BEGIN:VCARD", repr(event.to_dict()))
        self.assertNotIn("javascript", repr(event.to_dict()))

    def test_vcard_card_and_field_limits_preserve_contact_name(self):
        card = (
            "BEGIN:VCARD\nFN:Pessoa\\, Exemplo\n"
            + "".join(
                "TEL:+55119999999%02d\nEMAIL:p%s@example.com\n" % (index, index)
                for index in range(8)
            )
            + "END:VCARD"
        )
        envelope = self.load_fixture("message_text_lid.json")
        envelope["event"]["Message"] = {
            "contactsArrayMessage": {"contacts": [{"vcard": card}] * 12}
        }
        event = self.adapter.normalize_event(
            self.connection, sanitize_webhook_envelope(envelope)
        )
        contacts = event.message.structured_content["contacts"]
        self.assertEqual(len(contacts), 10)
        self.assertEqual(contacts[0]["name"], "Pessoa, Exemplo")
        self.assertEqual(len(contacts[0]["phones"]), 5)
        self.assertEqual(len(contacts[0]["emails"]), 5)
        envelope["event"]["Message"] = {
            "contactMessage": {"displayName": "Nome preservado", "vcard": "x" * 32769}
        }
        event = self.adapter.normalize_event(self.connection, envelope)
        self.assertEqual(
            event.message.structured_content["contacts"],
            [{"name": "Nome preservado", "phones": [], "emails": []}],
        )

    def test_structured_location_preserves_zero_and_marks_live_snapshot(self):
        for kind in ("locationMessage", "liveLocationMessage"):
            with self.subTest(kind=kind):
                envelope = self.load_fixture("message_text_lid.json")
                envelope["event"]["Message"] = {
                    kind: {
                        "degreesLatitude": 0,
                        "degreesLongitude": 0,
                        "name": "Ponto",
                        "caption": "A caminho",
                        "address": "Endereço",
                    }
                }
                event = self.adapter.normalize_event(
                    self.connection, sanitize_webhook_envelope(envelope)
                )
                self.assertEqual(
                    event.message.structured_content,
                    {
                        "type": "location",
                        "latitude": 0.0,
                        "longitude": 0.0,
                        "name": "A caminho"
                        if kind == "liveLocationMessage"
                        else "Ponto",
                        "address": "Endereço",
                        "live": kind == "liveLocationMessage",
                    },
                )

    def test_circular_video_sanitizer_preserves_only_its_download_key(self):
        envelope = self.load_fixture("message_video.json")
        payload = envelope["event"]["Message"].pop("videoMessage")
        envelope["event"]["Message"]["ptvMessage"] = payload
        payload["contextInfo"] = {"mediaKey": "quoted-key-must-be-dropped"}
        envelope["shadow"] = {"ptvMessage": {"mediaKey": "shadow-key-must-be-dropped"}}
        sanitized = sanitize_webhook_envelope(envelope)
        event = self.adapter.normalize_event(self.connection, sanitized)
        self.assertEqual(event.message.content_type, "video")
        self.assertEqual(event.message.media[0].kind, "video")
        self.assertEqual(
            event.message.media[0].remote_locator["media_key"], payload["mediaKey"]
        )
        request = self.adapter._prepare_download_request(event.message.media[0])
        self.assertEqual(request["endpoint"], "/chat/downloadvideo")
        self.assertNotIn("quoted-key-must-be-dropped", repr(sanitized))
        self.assertNotIn("shadow-key-must-be-dropped", repr(sanitized))

    def test_invalid_large_location_number_keeps_human_label_without_card(self):
        envelope = self.load_fixture("message_text_lid.json")
        envelope["event"]["Message"] = {
            "locationMessage": {
                "degreesLatitude": 10**400,
                "degreesLongitude": 20,
                "name": "Ponto recebido",
            }
        }
        event = self.adapter.normalize_event(self.connection, envelope)
        self.assertEqual(event.message.text, "Localização: Ponto recebido")
        self.assertFalse(event.message.structured_content)

    def test_normalize_provider_neutral_human_content_variants(self):
        cases = (
            (
                {
                    "stickerMessage": {
                        "URL": "https://mmg.whatsapp.net/synthetic-sticker.enc",
                        "mimetype": "image/webp",
                        "fileLength": 13,
                        "fileSHA256": ("iBDGHcmY4u85eR52tjd7y3jWuZgekIr/UbuYIWUp/s4="),
                        "mediaKey": "synthetic-secret-that-must-not-be-projected",
                        "contextInfo": {
                            "stanzaID": "STICKER-REPLY-TARGET",
                            "participant": "100000000000001@lid",
                        },
                    }
                },
                "sticker",
                "[Figurinha]",
                "STICKER-REPLY-TARGET",
            ),
            (
                {
                    "locationMessage": {
                        "degreesLatitude": -22.7253,
                        "degreesLongitude": -47.6492,
                        "name": "Local de teste",
                        "address": "Endereço sintético",
                        "comment": "Entrada lateral",
                    }
                },
                "location",
                (
                    "Localização: Local de teste\nEndereço sintético\n"
                    "Entrada lateral\n-22.725300, -47.649200"
                ),
                "",
            ),
            (
                {
                    "liveLocationMessage": {
                        "degreesLatitude": -22.7253,
                        "degreesLongitude": -47.6492,
                        "caption": "Estou chegando",
                    }
                },
                "location",
                "Localização em tempo real: Estou chegando\n-22.725300, -47.649200",
                "",
            ),
            (
                {
                    "contactMessage": {
                        "displayName": "Contato sintético",
                        "vcard": (
                            "BEGIN:VCARD\nFN:Contato sintético\n"
                            "TEL:+5500000000000\nEND:VCARD"
                        ),
                    }
                },
                "contacts",
                "Contato: Contato sintético",
                "",
            ),
            (
                {
                    "contactsArrayMessage": {
                        "displayName": "Equipe sintética",
                        "contacts": [
                            {
                                "displayName": "Pessoa A",
                                "vcard": "BEGIN:VCARD\nTEL:+5500000000001\nEND:VCARD",
                            },
                            {
                                "displayName": "Pessoa B",
                                "vcard": "BEGIN:VCARD\nTEL:+5500000000002\nEND:VCARD",
                            },
                        ],
                    }
                },
                "contacts",
                "Contatos: Equipe sintética\n• Pessoa A\n• Pessoa B",
                "",
            ),
            (
                {
                    "templateMessage": {
                        "hydratedTemplate": {
                            "hydratedContentText": "Confirme o atendimento"
                        },
                        "templateID": "provider-template-id-must-not-be-projected",
                    }
                },
                "template",
                "Confirme o atendimento",
                "",
            ),
            (
                {
                    "templateMessage": {
                        "Format": {
                            "FourRowTemplate": {
                                "contentText": "Conteúdo da codificação oneof"
                            }
                        }
                    }
                },
                "template",
                "Conteúdo da codificação oneof",
                "",
            ),
            (
                {
                    "pollCreationMessage": {
                        "name": "Qual horário?",
                        "options": [
                            {"optionName": "Manhã"},
                            {"optionName": "Tarde"},
                        ],
                        "selectableOptionsCount": 1,
                    }
                },
                "poll",
                "Enquete: Qual horário?\n• Manhã\n• Tarde",
                "",
            ),
            (
                {
                    "buttonsResponseMessage": {
                        "selectedButtonID": "provider-row-id",
                        "Response": {"SelectedDisplayText": "Quero atendimento"},
                    }
                },
                "selection",
                "Quero atendimento",
                "",
            ),
            (
                {
                    "listResponseMessage": {
                        "title": "Opção escolhida",
                        "singleSelectReply": {"selectedRowID": "provider-row-id"},
                    }
                },
                "selection",
                "Opção escolhida",
                "",
            ),
            (
                {
                    "interactiveMessage": {
                        "body": {"text": "Escolha uma alternativa"},
                        "footer": {"text": "Rodapé sintético"},
                    }
                },
                "interactive",
                "Escolha uma alternativa\nRodapé sintético",
                "",
            ),
        )
        for provider_message, content_type, text, reply_target in cases:
            with self.subTest(content_type=content_type):
                envelope = self.load_fixture("message_text_lid.json")
                envelope["event"]["Message"] = provider_message

                event = self.adapter.normalize_event(self.connection, envelope)

                self.assertEqual(event.event_type, "message.created")
                self.assertEqual(event.message.content_type, content_type)
                self.assertEqual(event.message.text, text)
                if content_type == "sticker":
                    self.assertEqual(len(event.message.media), 1)
                    sticker = event.message.media[0]
                    self.assertEqual(sticker.kind, "image")
                    self.assertEqual(sticker.mime_type, "image/webp")
                    self.assertEqual(
                        sticker.remote_locator["provider_media_kind"], "sticker"
                    )
                    self.assertEqual(
                        self.adapter._prepare_download_request(sticker)["endpoint"],
                        "/chat/downloadsticker",
                    )
                else:
                    self.assertFalse(event.message.media)
                self.assertEqual(event.message.reply_to_external_id, reply_target)
                serialized = repr(event.to_dict())
                self.assertNotIn("BEGIN:VCARD", serialized)
                self.assertNotIn("provider-row-id", event.message.text)
                self.assertNotIn("provider-template-id", serialized)

    def test_button_reply_go_oneof_survives_webhook_sanitization(self):
        cases = (
            (
                "buttonsResponseMessage",
                {
                    "selectedButtonID": "provider-button-id-must-not-project",
                    "Response": {"SelectedDisplayText": " Quero\n atendimento\x00 "},
                },
            ),
            (
                "templateButtonReplyMessage",
                {
                    "selectedID": "provider-button-id-must-not-project",
                    "selectedDisplayText": " Quero\n atendimento\x00 ",
                },
            ),
        )
        for kind, content in cases:
            with self.subTest(kind=kind):
                envelope = self.load_fixture("message_text_lid.json")
                content["contextInfo"] = {"stanzaID": "BUTTON-REPLY-TARGET"}
                envelope["event"]["Message"] = {kind: content}

                event = self.adapter.normalize_event(
                    self.connection, sanitize_webhook_envelope(envelope)
                )

                self.assertEqual(event.message.text, "Quero atendimento")
                self.assertEqual(event.message.content_type, "selection")
                self.assertEqual(
                    event.message.reply_to_external_id, "BUTTON-REPLY-TARGET"
                )
                self.assertEqual(
                    event.message.structured_content["id"],
                    "provider-button-id-must-not-project",
                )
                self.assertNotIn("provider-button-id", event.message.text)

    def test_button_reply_go_oneof_label_is_bounded_and_never_uses_provider_id(self):
        cases = (
            ({"SelectedDisplayText": "x" * 5000}, "x" * 4096),
            (
                {"SelectedDisplayText": {"opaque": "not-human-text"}},
                "[Resposta interativa]",
            ),
            (None, "[Resposta interativa]"),
        )
        for response, expected in cases:
            with self.subTest(response_type=type(response).__name__):
                envelope = self.load_fixture("message_text_lid.json")
                envelope["event"]["Message"] = {
                    "buttonsResponseMessage": {
                        "selectedButtonID": "provider-button-id-must-not-project",
                        "Response": response,
                    }
                }

                event = self.adapter.normalize_event(
                    self.connection, sanitize_webhook_envelope(envelope)
                )

                self.assertEqual(event.message.text, expected)
                self.assertNotIn("provider-button-id", event.message.text)

    @mock.patch(REQUEST_PATCH)
    def test_sanitized_sticker_and_associated_child_media_download(self, request):
        cases = (
            (
                "message_sticker.json",
                b"fixture-sticker",
                "sticker",
                "image",
                "/chat/downloadsticker",
            ),
            (
                "message_animated_sticker.json",
                b"fixture-animated-sticker",
                "sticker",
                "image",
                "/chat/downloadsticker",
            ),
            (
                "message_associated_child_video.json",
                b"fixture-video",
                "video",
                "video",
                "/chat/downloadvideo",
            ),
            (
                "message_ephemeral_view_once_image.json",
                b"fixture-image",
                "image",
                "image",
                "/chat/downloadimage",
            ),
        )
        for fixture, content, content_type, kind, endpoint in cases:
            with self.subTest(fixture=fixture):
                envelope = sanitize_webhook_envelope(self.load_fixture(fixture))
                event = self.adapter.normalize_event(self.connection, envelope)

                self.assertEqual(event.message.content_type, content_type)
                media = event.message.media[0]
                self.assertEqual(media.kind, kind)
                self.assertTrue(media.remote_locator["media_key"])
                request.return_value = FakeResponse(
                    200,
                    {
                        "code": 200,
                        "success": True,
                        "data": {
                            "Mimetype": media.mime_type,
                            "Data": "data:%s;base64,%s"
                            % (
                                media.mime_type.replace(" ", ""),
                                base64.b64encode(content).decode("ascii"),
                            ),
                        },
                    },
                )

                result = self.adapter.download_media(self.connection, media)

                self.assertEqual(result.content, content)
                self.assertEqual(
                    request.call_args.args[1], "https://wuzapi.invalid%s" % endpoint
                )
                self.assertEqual(
                    request.call_args.kwargs["json"]["MediaKey"],
                    media.remote_locator["media_key"],
                )
                request.reset_mock()

        associated = self.adapter.normalize_event(
            self.connection,
            sanitize_webhook_envelope(
                self.load_fixture("message_associated_child_video.json")
            ),
        )
        self.assertEqual(associated.message.reply_to_external_id, "")
        self.assertFalse(associated.reply_to)
        self.assertEqual(
            associated.message.protocol_snapshot["association"],
            {
                "kind": "associated_child",
                "type": "media_album",
                "parent_external_message_id": "ALBUMPARENT000000000000000000001",
                "message_index": 2,
            },
        )

    def test_inbound_sticker_contract_rejects_non_webp_and_invalid_animation(self):
        non_webp = self.load_fixture("message_animated_sticker.json")
        non_webp["event"]["Message"]["stickerMessage"]["mimetype"] = "video/mp4"
        with self.assertRaisesRegex(AdapterError, "must use image/webp"):
            self.adapter.normalize_event(self.connection, non_webp)

        invalid_animation = self.load_fixture("message_sticker.json")
        invalid_animation["event"]["Message"]["stickerMessage"][
            "isAnimated"
        ] = "provider-private-value"
        with self.assertRaisesRegex(AdapterError, "animation metadata is invalid"):
            self.adapter.normalize_event(self.connection, invalid_animation)

        sticker = self.adapter.normalize_event(
            self.connection,
            sanitize_webhook_envelope(self.load_fixture("message_sticker.json")),
        ).message.media[0]
        for invalid_media in (
            dataclasses.replace(sticker, mime_type="image/png"),
            dataclasses.replace(sticker, kind="video", mime_type="video/mp4"),
        ):
            with self.subTest(
                kind=invalid_media.kind, mime_type=invalid_media.mime_type
            ):
                with self.assertRaisesRegex(
                    AdapterError, "sticker media metadata is invalid"
                ):
                    self.adapter._prepare_download_request(invalid_media)

    def test_associated_child_correlation_is_bounded_and_not_a_reply(self):
        envelope = self.load_fixture("message_associated_child_video.json")
        association = envelope["event"]["Message"]["messageContextInfo"][
            "messageAssociation"
        ]
        association.update(
            {
                "associationType": "provider-private-type-must-not-project",
                "parentMessageKey": {"ID": "X" * 257},
                "messageIndex": 10001,
            }
        )

        event = self.adapter.normalize_event(
            self.connection, sanitize_webhook_envelope(envelope)
        )

        self.assertEqual(
            event.message.protocol_snapshot["association"],
            {"kind": "associated_child"},
        )
        self.assertEqual(event.message.reply_to_external_id, "")
        self.assertFalse(event.reply_to)
        serialized = repr(event.to_dict())
        self.assertNotIn("provider-private-type", serialized)
        self.assertNotIn("X" * 257, serialized)

    def test_message_wrapper_depth_and_associated_child_count_are_bounded(self):
        def wrapped_envelope(depth, wrapper="ephemeralMessage"):
            envelope = self.load_fixture("message_image.json")
            message = envelope["event"]["Message"]
            for _index in range(depth):
                message = {wrapper: {"message": message}}
            envelope["event"]["Message"] = message
            return sanitize_webhook_envelope(envelope)

        accepted = self.adapter.normalize_event(self.connection, wrapped_envelope(8))
        self.assertEqual(accepted.message.content_type, "image")
        self.assertTrue(accepted.message.media[0].remote_locator["media_key"])

        with self.assertRaisesRegex(UnsupportedEventError, "nesting is invalid"):
            self.adapter.normalize_event(self.connection, wrapped_envelope(9))
        with self.assertRaisesRegex(
            UnsupportedEventError, "nested associated child messages are invalid"
        ):
            self.adapter.normalize_event(
                self.connection,
                wrapped_envelope(2, wrapper="associatedChildMessage"),
            )

    def test_human_unknown_content_gets_explicit_fallback_but_protocol_only_does_not(
        self,
    ):
        unknown = self.load_fixture("message_text_lid.json")
        unknown["event"]["Message"] = {
            "productMessage": {"product": {"productImageCount": 1}}
        }

        event = self.adapter.normalize_event(self.connection, unknown)

        self.assertEqual(event.message.content_type, "unsupported")
        self.assertEqual(event.message.text, "[Conteúdo do WhatsApp não suportado]")
        self.assertNotIn("productImageCount", repr(event.to_dict()))

        protocol_only_messages = (
            {"protocolMessage": {"type": 4, "ephemeralExpiration": 86400}},
            {
                "senderKeyDistributionMessage": {
                    "axolotlSenderKeyDistributionMessage": "opaque"
                }
            },
            {"messageContextInfo": {"messageSecret": "opaque"}},
            {"secretEncryptedMessage": {"encPayload": "opaque"}},
            {"albumMessage": {"expectedImageCount": 2, "expectedVideoCount": 1}},
            {
                "pollUpdateMessage": {
                    "pollCreationMessageKey": {"ID": "POLL-TARGET"},
                    "vote": {"encPayload": "opaque"},
                }
            },
        )
        for provider_message in protocol_only_messages:
            with self.subTest(provider_message=tuple(provider_message)):
                envelope = self.load_fixture("message_text_lid.json")
                envelope["event"]["Message"] = provider_message
                with self.assertRaises(UnsupportedEventError):
                    self.adapter.normalize_event(self.connection, envelope)

    def test_normalize_reaction_add_remove_edit_and_delete(self):
        reaction = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_reaction_add.json")
        )
        self.assertEqual(reaction.event_type, "message.reaction")
        self.assertIsNone(reaction.message)
        self.assertEqual(reaction.mutation["type"], "react")
        self.assertEqual(reaction.mutation["operation"], "add")
        self.assertEqual(reaction.mutation["emoji"], "👍")
        self.assertTrue(reaction.mutation["target_from_me"])

        removed = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_reaction_remove.json")
        )
        self.assertEqual(removed.mutation["operation"], "remove")
        self.assertEqual(removed.mutation["emoji"], "")

        edited = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_edit.json")
        )
        self.assertEqual(edited.event_type, "message.updated")
        self.assertEqual(edited.message.text, "Texto corrigido")
        self.assertEqual(
            edited.mutation["target_external_message_id"],
            "TARGET-MESSAGE-EDIT-0001",
        )
        self.assertNotEqual(edited.event_id, "Message:TARGET-MESSAGE-EDIT-0001")
        edited_again = copy.deepcopy(self.load_fixture("message_edit.json"))
        edited_again["event"]["Info"]["Timestamp"] = "2026-08-21T18:12:02Z"
        edited_again["event"]["Message"]["extendedTextMessage"][
            "text"
        ] = "Outra correção"
        second_edit = self.adapter.normalize_event(self.connection, edited_again)
        self.assertNotEqual(edited.event_id, second_edit.event_id)

        deleted = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_delete.json")
        )
        self.assertEqual(deleted.event_type, "message.deleted")
        self.assertIsNone(deleted.message)
        self.assertEqual(deleted.mutation["type"], "delete")
        self.assertEqual(
            deleted.mutation["target_external_message_id"],
            "TARGET-MESSAGE-DELETE-0001",
        )

    def test_protocol_type_14_edit_uses_nested_text_and_target_key(self):
        envelope = self.load_fixture("message_text_lid.json")
        envelope["event"].pop("IsEdit", None)
        envelope["event"]["Info"].update(
            {
                "ID": "EDIT-WRAPPER-EVENT-ID",
                "Edit": "",
            }
        )
        envelope["event"]["Message"] = {
            "protocolMessage": {
                "type": 14,
                "key": {
                    "ID": "TARGET-MESSAGE-EDIT-REAL-0001",
                    "fromMe": False,
                },
                "editedMessage": {
                    "extendedTextMessage": {
                        "text": "Texto corrigido pelo payload real",
                        "contextInfo": {
                            "stanzaID": "EDIT-REPLY-TARGET",
                            "participant": "100000000000001@lid",
                        },
                    }
                },
                "timestampMS": 1787335921000,
            }
        }

        event = self.adapter.normalize_event(self.connection, envelope)

        self.assertEqual(event.event_type, "message.updated")
        self.assertEqual(
            event.mutation["target_external_message_id"],
            "TARGET-MESSAGE-EDIT-REAL-0001",
        )
        self.assertFalse(event.mutation["target_from_me"])
        self.assertEqual(
            event.mutation["new_text"], "Texto corrigido pelo payload real"
        )
        self.assertEqual(event.message.text, "Texto corrigido pelo payload real")
        self.assertEqual(event.message.reply_to_external_id, "EDIT-REPLY-TARGET")
        self.assertNotEqual(event.event_id, "Message:EDIT-WRAPPER-EVENT-ID")

    def test_protocol_type_14_edit_fails_closed_for_media_or_missing_target(self):
        for edited_message, error_pattern in (
            (
                {
                    "imageMessage": {
                        "URL": "https://mmg.whatsapp.net/synthetic-edit.enc",
                        "mimetype": "image/jpeg",
                        "fileLength": 10,
                        "fileSHA256": "c3ludGhldGlj",
                        "mediaKey": "c3ludGhldGlj",
                    }
                },
                "media edits are not implemented",
            ),
            ({"conversation": "Texto sem alvo"}, "target message ID is missing"),
        ):
            with self.subTest(error_pattern=error_pattern):
                envelope = self.load_fixture("message_text_lid.json")
                protocol = {
                    "type": "MESSAGE_EDIT",
                    "editedMessage": edited_message,
                }
                if "sem alvo" not in repr(edited_message):
                    protocol["key"] = {"ID": "TARGET-MEDIA-EDIT"}
                envelope["event"]["Message"] = {"protocolMessage": protocol}
                envelope["event"].pop("IsEdit", None)
                envelope["event"]["Info"]["Edit"] = ""
                with self.assertRaisesRegex(UnsupportedEventError, error_pattern):
                    self.adapter.normalize_event(self.connection, envelope)

    def test_normalize_read_receipt_has_stable_delivery_identity(self):
        envelope = self.load_fixture("read_receipt.json")

        event = self.adapter.normalize_event(self.connection, envelope)
        reversed_envelope = copy.deepcopy(envelope)
        reversed_envelope["event"]["MessageIDs"].reverse()
        equivalent = self.adapter.normalize_event(self.connection, reversed_envelope)

        self.assertEqual(event.event_type, "delivery.updated")
        self.assertEqual(event.direction, "outbound")
        self.assertEqual(event.delivery["state"], "delivered")
        self.assertEqual(
            event.delivery["external_message_ids"],
            [
                "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            ],
        )
        self.assertEqual(event.delivery["external_event_id"], event.event_id)
        self.assertEqual(event.event_id, equivalent.event_id)
        self.assertTrue(event.event_id.startswith("ReadReceipt:"))

        duplicate_envelope = copy.deepcopy(envelope)
        duplicate_envelope["event"]["MessageIDs"] = [
            "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        ]
        duplicate = self.adapter.normalize_event(self.connection, duplicate_envelope)
        self.assertEqual(duplicate.event_id, event.event_id)
        self.assertEqual(
            duplicate.delivery["external_message_ids"],
            [
                "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            ],
        )

        read_envelope = copy.deepcopy(envelope)
        read_envelope["state"] = "Read"
        read_event = self.adapter.normalize_event(self.connection, read_envelope)
        self.assertEqual(read_event.delivery["state"], "read")

        own_read_envelope = copy.deepcopy(envelope)
        own_read_envelope["event"]["IsFromMe"] = True
        own_read_event = self.adapter.normalize_event(
            self.connection, own_read_envelope
        )
        self.assertTrue(own_read_event.is_from_me)
        self.assertEqual(own_read_event.direction, "inbound")
        self.assertNotEqual(own_read_event.event_id, event.event_id)

        read_self_envelope = copy.deepcopy(envelope)
        read_self_envelope["state"] = "ReadSelf"
        with self.assertRaises(UnsupportedEventError):
            self.adapter.normalize_event(self.connection, read_self_envelope)

    def test_read_receipt_empty_message_id_array_is_terminally_unsupported(self):
        envelope = self.load_fixture("read_receipt.json")

        for message_ids in ([], ["", "  "]):
            with self.subTest(message_ids=message_ids):
                candidate = copy.deepcopy(envelope)
                candidate["event"]["MessageIDs"] = message_ids
                with self.assertRaisesRegex(
                    UnsupportedEventError, "no non-empty message IDs"
                ):
                    self.adapter.normalize_event(self.connection, candidate)

        for message_ids in (None, "MESSAGE-ID", {"id": "MESSAGE-ID"}):
            with self.subTest(message_ids=message_ids):
                candidate = copy.deepcopy(envelope)
                candidate["event"]["MessageIDs"] = message_ids
                with self.assertRaisesRegex(AdapterError, "no MessageIDs array"):
                    self.adapter.normalize_event(self.connection, candidate)
        missing = copy.deepcopy(envelope)
        missing["event"].pop("MessageIDs")
        with self.assertRaisesRegex(AdapterError, "no MessageIDs array"):
            self.adapter.normalize_event(self.connection, missing)

    def test_receipt_direction_count_and_identifiers_fail_closed(self):
        envelope = self.load_fixture("read_receipt.json")
        for invalid_direction in (None, "", "unknown", 2, {}, []):
            with self.subTest(invalid_direction=invalid_direction):
                candidate = copy.deepcopy(envelope)
                candidate["event"]["IsFromMe"] = invalid_direction
                with self.assertRaisesRegex(AdapterError, "IsFromMe"):
                    self.adapter.normalize_event(self.connection, candidate)

        for message_ids, error_pattern in (
            ([None], "message ID is missing"),
            ([17], "message ID is missing"),
            (["A B"], "message ID is invalid"),
            (["A" * 257], "message ID is invalid"),
            (["A"] * 101, "too many message IDs"),
        ):
            with self.subTest(message_ids=message_ids):
                candidate = copy.deepcopy(envelope)
                candidate["event"]["MessageIDs"] = message_ids
                with self.assertRaisesRegex(AdapterError, error_pattern):
                    self.adapter.normalize_event(self.connection, candidate)

    def test_message_reply_and_mutation_identifiers_are_bounded(self):
        oversized = "A" * 257

        message = self.load_fixture("message_text_lid.json")
        message["event"]["Info"]["ID"] = oversized
        with self.assertRaisesRegex(AdapterError, "message ID is invalid"):
            self.adapter.normalize_event(self.connection, message)

        reply = self.load_fixture("message_text_lid.json")
        reply["event"]["Message"]["extendedTextMessage"]["contextInfo"][
            "stanzaID"
        ] = oversized
        with self.assertRaisesRegex(AdapterError, "reply target message ID is invalid"):
            self.adapter.normalize_event(self.connection, reply)

        for fixture, key_path in (
            ("message_reaction_add.json", ("reactionMessage", "key")),
            ("message_delete.json", ("protocolMessage", "key")),
        ):
            with self.subTest(fixture=fixture):
                mutation = self.load_fixture(fixture)
                provider_message = mutation["event"]["Message"]
                provider_message[key_path[0]][key_path[1]]["ID"] = oversized
                with self.assertRaisesRegex(AdapterError, "message ID is invalid"):
                    self.adapter.normalize_event(self.connection, mutation)

    def test_connection_lifecycle_events_are_provider_neutral_and_stable(self):
        cases = (
            ("Connected", {}, "connected"),
            ("KeepAliveRestored", {}, "connected"),
            ("Disconnected", {}, "disconnected"),
            ("ConnectFailure", "ConnectFailure", "disconnected"),
            ("StreamReplaced", {}, "disconnected"),
            (
                "KeepAliveTimeout",
                {"ErrorCount": 2, "LastSuccess": "2026-08-23T12:00:00Z"},
                "degraded",
            ),
            ("StreamError", {"Code": "test", "Raw": None}, "degraded"),
            (
                "LoggedOut",
                {"OnConnect": False, "Reason": 401},
                "authentication_required",
            ),
            ("QRTimeout", "timeout", "authentication_required"),
            ("ClientOutdated", {}, "error"),
            ("TemporaryBan", {"Code": 101, "Expire": 60000000000}, "paused"),
        )
        before = datetime.datetime.now(datetime.timezone.utc)
        for provider_type, raw_event, expected_state in cases:
            with self.subTest(provider_type=provider_type):
                envelope = {
                    "type": provider_type,
                    "event": raw_event,
                    "instanceName": "WuzAPI test instance",
                    "userID": "wuzapi-test-user",
                }
                event = self.adapter.normalize_event(self.connection, envelope)
                equivalent = self.adapter.normalize_event(
                    self.connection,
                    dict(reversed(tuple(envelope.items()))),
                )

                self.assertEqual(event.event_type, "connection.updated")
                self.assertEqual(event.extensions["state"], expected_state)
                self.assertEqual(
                    event.extensions.get("health_confirmation_required", False),
                    expected_state == "connected",
                )
                self.assertEqual(event.event_id, equivalent.event_id)
                self.assertTrue(
                    event.event_id.startswith("Lifecycle:%s:" % provider_type)
                )
                self.assertEqual(event.conversation_ref, self.connection.external_ref)
                self.assertEqual(event.conversation.conversation_type, "other")
                self.assertFalse(event.conversation.addresses)
                self.assertEqual(event.direction, "inbound")
                self.assertFalse(event.is_from_me)
                provider = event.extensions["provider.wuzapi"]
                self.assertEqual(provider["event_type"], provider_type)
                self.assertEqual(provider["occurred_at_source"], "odoo_received")
                self.assertGreaterEqual(event.occurred_at, before)

    def test_normalize_inbound_group_keeps_chat_and_actor_aliases_separate(self):
        event = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_group_text_lid.json")
        )

        self.assertEqual(event.event_type, "message.created")
        self.assertEqual(event.direction, "inbound")
        self.assertFalse(event.is_from_me)
        self.assertEqual(event.origin, "provider")
        self.assertEqual(event.conversation_ref, "120363000000001@g.us")
        self.assertEqual(event.conversation.conversation_type, "group")
        self.assertEqual(len(event.conversation.addresses), 1)
        group_address = event.conversation.addresses[0]
        self.assertEqual(group_address.namespace, "whatsapp.group")
        self.assertEqual(group_address.value_normalized, event.conversation_ref)
        self.assertEqual(group_address.role, "group")
        self.assertEqual(group_address.source_field, "event.Info.Chat")

        actor_addresses = {
            (address.namespace, address.value_normalized, address.role)
            for address in event.actor.addresses
        }
        self.assertEqual(
            actor_addresses,
            {
                ("whatsapp.lid", "200000000000001@lid", "sender"),
                (
                    "whatsapp.pn",
                    "5511900000001@s.whatsapp.net",
                    "alternate",
                ),
            },
        )
        self.assertNotIn(
            event.conversation_ref,
            {address.value_normalized for address in event.actor.addresses},
        )
        self.assertEqual(event.actor.display_name, "Participante de teste")
        self.assertEqual(event.message.text, "Mensagem sanitizada do grupo")
        self.assertEqual(
            event.message.reply_to_external_id,
            "GROUPTARGET000000000000000000001",
        )

    def test_normalize_inbound_group_media_supports_pn_and_lid_actor(self):
        event = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_group_image_pn.json")
        )

        self.assertEqual(event.conversation.conversation_type, "group")
        self.assertEqual(
            {
                (address.namespace, address.value_normalized)
                for address in event.actor.addresses
            },
            {
                ("whatsapp.pn", "5511900000002@s.whatsapp.net"),
                ("whatsapp.lid", "200000000000002@lid"),
            },
        )
        self.assertEqual(event.message.content_type, "image")
        self.assertEqual(event.message.text, "Imagem sanitizada do grupo")
        self.assertEqual(len(event.message.media), 1)
        media = event.message.media[0]
        self.assertEqual(media.kind, "image")
        self.assertEqual(media.mime_type, "image/jpeg")
        self.assertEqual(media.size_bytes, 2048)
        self.assertEqual(media.width, 800)
        self.assertEqual(media.height, 600)

    def test_group_from_me_first_image_pair_ignores_protocol_only_callback(self):
        protocol_only = self.load_fixture(
            "message_group_from_me_first_image_lid_protocol_only.json"
        )
        human_message = self.load_fixture("message_group_from_me_first_image_lid.json")
        protocol_info = protocol_only["event"]["Info"]
        human_info = human_message["event"]["Info"]

        self.assertEqual(protocol_info["ID"], human_info["ID"])
        self.assertTrue(protocol_info["IsFromMe"])
        self.assertEqual(protocol_info["AddressingMode"], "lid")
        self.assertNotEqual(
            json.dumps(protocol_only, sort_keys=True),
            json.dumps(human_message, sort_keys=True),
        )

        sanitized_protocol = sanitize_webhook_envelope(protocol_only)
        self.assertNotIn(
            "senderKeyDistributionMessage",
            sanitized_protocol["event"]["Message"],
        )
        with self.assertRaisesRegex(
            UnsupportedEventError, "protocol-only message has no human content"
        ):
            self.adapter.normalize_event(self.connection, sanitized_protocol)

        event = self.adapter.normalize_event(
            self.connection, sanitize_webhook_envelope(human_message)
        )

        self.assertEqual(event.event_id, "Message:%s" % human_info["ID"])
        self.assertEqual(event.event_type, "message.created")
        self.assertEqual(event.direction, "outbound")
        self.assertTrue(event.is_from_me)
        self.assertEqual(event.origin, "external_device")
        self.assertEqual(event.conversation_ref, "120363888888888@g.us")
        self.assertEqual(event.conversation.conversation_type, "group")
        self.assertEqual(
            {
                (address.namespace, address.value_normalized, address.role)
                for address in event.actor.addresses
            },
            {
                ("whatsapp.lid", "700000000000888@lid", "sender"),
                (
                    "whatsapp.pn",
                    "999000000000888@s.whatsapp.net",
                    "alternate",
                ),
            },
        )
        self.assertEqual(event.message.content_type, "image")
        self.assertEqual(event.message.client_message_id, human_info["ID"])
        self.assertEqual(event.message.text, "Primeira imagem de teste do grupo")
        self.assertEqual(
            event.message.protocol_snapshot["source"]["addressing_mode"], "lid"
        )
        media = event.message.media[0]
        self.assertEqual(media.kind, "image")
        self.assertEqual(media.mime_type, "image/jpeg")
        self.assertEqual(media.size_bytes, 4096)
        self.assertEqual(media.width, 1024)
        self.assertEqual(media.height, 768)
        self.assertEqual(
            media.remote_locator["direct_path"],
            "/dummy-first-group-image.enc",
        )
        self.assertTrue(media.remote_locator["media_key"])

    def test_group_classification_uses_chat_jid_not_is_group_flag(self):
        group = self.load_fixture("message_group_text_lid.json")
        group["event"]["Info"]["IsGroup"] = False
        event = self.adapter.normalize_event(self.connection, group)
        self.assertEqual(event.conversation.conversation_type, "group")

        direct = self.load_fixture("message_text_lid.json")
        direct["event"]["Info"]["IsGroup"] = True
        event = self.adapter.normalize_event(self.connection, direct)
        self.assertEqual(event.conversation.conversation_type, "direct")

        with self.assertRaises(UnsupportedEventError):
            self.adapter.normalize_event(
                self.connection, self.load_fixture("message_broadcast.json")
            )

        for unsupported_chat in (
            "1234567890@newsletter",
            "opaque@unknown.whatsapp",
        ):
            with self.subTest(chat=unsupported_chat):
                envelope = self.load_fixture("message_text_lid.json")
                envelope["event"]["Info"]["Chat"] = unsupported_chat
                with self.assertRaisesRegex(
                    UnsupportedEventError, "JID class is not implemented"
                ):
                    self.adapter.normalize_event(self.connection, envelope)

    def test_group_rejects_missing_invalid_or_own_participant(self):
        missing = self.load_fixture("message_group_text_lid.json")
        missing["event"]["Info"]["Sender"] = ""
        with self.assertRaisesRegex(AdapterError, "no participant sender"):
            self.adapter.normalize_event(self.connection, missing)

        group_sender = self.load_fixture("message_group_text_lid.json")
        info = group_sender["event"]["Info"]
        info["Sender"] = info["Chat"]
        with self.assertRaisesRegex(AdapterError, "cannot be the group"):
            self.adapter.normalize_event(self.connection, group_sender)

        for invalid_sender in ("abc@lid", "+5511900000001@s.whatsapp.net"):
            invalid = self.load_fixture("message_group_text_lid.json")
            invalid["event"]["Info"]["Sender"] = invalid_sender
            with self.subTest(sender=invalid_sender), self.assertRaisesRegex(
                AdapterError, "participant JID is invalid"
            ):
                self.adapter.normalize_event(self.connection, invalid)

        outbound = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_group_from_me_text.json")
        )
        self.assertEqual(outbound.conversation.conversation_type, "group")
        self.assertEqual(outbound.direction, "outbound")
        self.assertTrue(outbound.is_from_me)
        self.assertEqual(outbound.origin, "external_device")
        self.assertEqual(outbound.message.content_type, "text")
        self.assertEqual(outbound.message.client_message_id, CLIENT_MESSAGE_ID)

    def test_group_protocol_only_stays_unsupported_while_mutations_are_normalized(self):
        with self.assertRaises(UnsupportedEventError):
            self.adapter.normalize_event(
                self.connection,
                self.load_fixture("message_group_protocol_only.json"),
            )

        reaction = self.load_fixture("message_reaction_add.json")
        info = reaction["event"]["Info"]
        info.update(
            {
                "Chat": "120363000000001@g.us",
                "Sender": "200000000000001@lid",
                "SenderAlt": "5511900000001@s.whatsapp.net",
                "IsGroup": True,
            }
        )
        normalized_reaction = self.adapter.normalize_event(self.connection, reaction)
        self.assertEqual(normalized_reaction.conversation.conversation_type, "group")
        self.assertEqual(normalized_reaction.mutation["type"], "react")
        # Preserve the provider-side observation. The correlated target binding,
        # not this ambiguous group hint, determines the canonical account lane.
        self.assertTrue(normalized_reaction.mutation["target_from_me"])
        self.assertNotIn("target_participant", normalized_reaction.mutation)
        target_participant = AddressDTO.from_dict(
            normalized_reaction.mutation["target_protocol_participant"]
        )
        self.assertEqual(target_participant.namespace, "whatsapp.pn")
        self.assertEqual(
            target_participant.value_normalized,
            "5511888888888@s.whatsapp.net",
        )

        edit = self.load_fixture("message_edit.json")
        edit["event"]["Info"].update(
            {
                "Chat": "120363000000001@g.us",
                "Sender": "200000000000001@lid",
                "SenderAlt": "5511900000001@s.whatsapp.net",
                "IsGroup": True,
            }
        )
        normalized_edit = self.adapter.normalize_event(self.connection, edit)
        self.assertEqual(normalized_edit.mutation["type"], "edit")
        self.assertFalse(normalized_edit.mutation["target_from_me"])
        self.assertEqual(
            normalized_edit.mutation["target_protocol_participant"]["value_normalized"],
            "200000000000001@lid",
        )
        self.assertEqual(
            normalized_edit.mutation["target_protocol_participant"]["source_field"],
            "event.Info.Sender",
        )

        delete = self.load_fixture("message_delete.json")
        delete["event"]["Info"].update(
            {
                "Chat": "120363000000001@g.us",
                "Sender": "200000000000001@lid",
                "SenderAlt": "5511900000001@s.whatsapp.net",
                "IsGroup": True,
            }
        )
        normalized_delete = self.adapter.normalize_event(self.connection, delete)
        self.assertEqual(normalized_delete.mutation["type"], "delete")
        self.assertTrue(normalized_delete.mutation["target_from_me"])

        delete_without_participant = copy.deepcopy(delete)
        delete_key = delete_without_participant["event"]["Message"]["protocolMessage"][
            "key"
        ]
        delete_key["fromMe"] = False
        delete_key["participant"] = ""
        normalized_delete_without_participant = self.adapter.normalize_event(
            self.connection, delete_without_participant
        )
        self.assertFalse(
            normalized_delete_without_participant.mutation["target_from_me"]
        )
        self.assertNotIn(
            "target_protocol_participant",
            normalized_delete_without_participant.mutation,
        )

        missing_target_evidence = copy.deepcopy(reaction)
        key = missing_target_evidence["event"]["Message"]["reactionMessage"]["key"]
        key.pop("fromMe")
        normalized_without_direction = self.adapter.normalize_event(
            self.connection, missing_target_evidence
        )
        self.assertNotIn("target_from_me", normalized_without_direction.mutation)
        self.assertIn(
            "target_protocol_participant", normalized_without_direction.mutation
        )

        account_target_without_participant = copy.deepcopy(reaction)
        key = account_target_without_participant["event"]["Message"]["reactionMessage"][
            "key"
        ]
        key["fromMe"] = False
        key["participant"] = ""
        normalized_account_target = self.adapter.normalize_event(
            self.connection, account_target_without_participant
        )
        self.assertFalse(normalized_account_target.mutation["target_from_me"])
        self.assertNotIn(
            "target_protocol_participant", normalized_account_target.mutation
        )

        remote_target_without_participant = copy.deepcopy(reaction)
        key = remote_target_without_participant["event"]["Message"]["reactionMessage"][
            "key"
        ]
        key["fromMe"] = True
        key["participant"] = ""
        normalized_remote_target = self.adapter.normalize_event(
            self.connection, remote_target_without_participant
        )
        self.assertTrue(normalized_remote_target.mutation["target_from_me"])
        self.assertNotIn(
            "target_protocol_participant", normalized_remote_target.mutation
        )

        own_actor = copy.deepcopy(reaction)
        own_actor["event"]["Info"]["IsFromMe"] = True
        own_actor_key = own_actor["event"]["Message"]["reactionMessage"]["key"]
        own_actor_key["fromMe"] = True
        normalized_own_actor = self.adapter.normalize_event(self.connection, own_actor)
        self.assertTrue(normalized_own_actor.mutation["target_from_me"])

        own_actor_key["fromMe"] = False
        normalized_own_actor_remote_target = self.adapter.normalize_event(
            self.connection, own_actor
        )
        self.assertFalse(normalized_own_actor_remote_target.mutation["target_from_me"])

    def test_group_receipt_uses_participant_actor_and_order_independent_identity(self):
        receipt = self.load_fixture("read_receipt.json")
        receipt["event"].update(
            {
                "Chat": "120363000000001@g.us",
                "Sender": "200000000000001@lid",
                "SenderAlt": "5511900000001@s.whatsapp.net",
                "IsGroup": True,
            }
        )
        event = self.adapter.normalize_event(self.connection, receipt)
        reordered = copy.deepcopy(receipt)
        reordered["event"]["MessageIDs"].reverse()
        reordered["event"]["Sender"], reordered["event"]["SenderAlt"] = (
            reordered["event"]["SenderAlt"],
            reordered["event"]["Sender"],
        )
        equivalent = self.adapter.normalize_event(self.connection, reordered)

        self.assertEqual(event.event_id, equivalent.event_id)
        self.assertEqual(event.conversation.conversation_type, "group")
        self.assertEqual(event.conversation_ref, "120363000000001@g.us")
        self.assertEqual(event.actor.addresses[0].namespace, "whatsapp.lid")
        self.assertEqual(event.delivery["state"], "delivered")
        self.assertNotIn("participant", event.delivery)

        with self.assertRaises(UnsupportedEventError):
            self.adapter.normalize_event(
                self.connection,
                {"type": "ReadSelf", "event": copy.deepcopy(receipt["event"])},
            )

    def test_unsupported_event_is_rejected(self):
        for event_type in (
            "Blocklist",
            "BlocklistChange",
            "CallOfferNotice",
            "CallRelayLatency",
            "MediaRetry",
            "Presence",
            "UndecryptableMessage",
        ):
            with self.subTest(event_type=event_type), self.assertRaises(
                UnsupportedEventError
            ):
                self.adapter.normalize_event(
                    self.connection, {"type": event_type, "event": {}}
                )

    @mock.patch(REQUEST_PATCH)
    def test_mark_read_direct_builds_bounded_request_and_accepts_ack(self, request):
        message_ids = [
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        ]
        command = self._command(
            command_type="mark_read",
            options={"external_message_ids": message_ids},
        )
        request.return_value = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {"Details": "Message(s) marked as read"},
            },
        )

        snapshot = self.adapter.prepare_request_snapshot(self.connection, command)

        self.assertEqual(
            snapshot,
            {
                "provider": "wuzapi",
                "provider_version": WUZAPI_VERSION,
                "method": "POST",
                "endpoint": "/chat/markread",
                "payload": {
                    "ChatPhone": "5511999999999@s.whatsapp.net",
                    "Id": message_ids,
                },
            },
        )
        result = self.adapter.execute_command(self.connection, command)
        self.assertEqual(result.status, "success")
        self.assertFalse(result.external_message_id)
        self.assertEqual(
            result.provider_response["data"]["details"],
            "Message(s) marked as read",
        )
        self.assertEqual(
            request.call_args.kwargs["json"],
            {"ChatPhone": "5511999999999@s.whatsapp.net", "Id": message_ids},
        )

    def test_mark_read_is_direct_only_and_fails_closed_on_malformed_contract(self):
        valid = self._command(
            command_type="mark_read",
            options={"external_message_ids": ["VALID-MESSAGE-ID"]},
        )
        invalid_commands = (
            self._command(
                command_type="mark_read",
                conversation_type="group",
                options={"external_message_ids": ["VALID-MESSAGE-ID"]},
            ),
            self._command(command_type="mark_read", options={}),
            self._command(
                command_type="mark_read",
                options={"external_message_ids": "VALID-MESSAGE-ID"},
            ),
            self._command(
                command_type="mark_read", options={"external_message_ids": []}
            ),
            self._command(
                command_type="mark_read",
                options={"external_message_ids": ["DUPLICATE", "DUPLICATE"]},
            ),
            self._command(
                command_type="mark_read",
                options={
                    "external_message_ids": ["VALID-MESSAGE-ID"],
                    "sender": "must-not-cross-provider-boundary",
                },
            ),
            self._command(
                command_type="mark_read",
                options={"external_message_ids": ["contains whitespace"]},
            ),
            self._command(
                command_type="mark_read",
                options={"external_message_ids": ["X" * 513]},
            ),
            self._command(
                command_type="mark_read",
                options={
                    "external_message_ids": [
                        "MESSAGE-%03d" % index for index in range(101)
                    ]
                },
            ),
            dataclasses.replace(
                valid,
                target_address=AddressDTO(
                    namespace="whatsapp.jid",
                    value="status@broadcast",
                    value_normalized="status@broadcast",
                    role="primary",
                    source_field="test",
                    confidence="protocol",
                ),
            ),
            dataclasses.replace(
                valid,
                message=MessageDTO(content_type="text", text="unexpected"),
            ),
        )
        for command in invalid_commands:
            with self.subTest(command=command), self.assertRaises(AdapterError):
                self.adapter.prepare_request_snapshot(self.connection, command)

        foreign_target = AddressDTO(
            namespace="whatsapp.pn",
            value="5511888888888@s.whatsapp.net",
            value_normalized="5511888888888@s.whatsapp.net",
            role="primary",
            source_field="test",
            confidence="protocol",
        )
        with self.assertRaises(AdapterError):
            self.adapter.prepare_request_snapshot(
                self.connection,
                dataclasses.replace(valid, target_address=foreign_target),
            )

        routing_target = dataclasses.replace(valid.target_address, role="routing")
        routing_command = dataclasses.replace(
            valid,
            target_address=routing_target,
            conversation=ConversationDTO(
                addresses=(routing_target,), conversation_type="direct"
            ),
        )
        self.assertEqual(
            self.adapter.prepare_request_snapshot(self.connection, routing_command)[
                "payload"
            ]["ChatPhone"],
            routing_target.value_normalized,
        )

        lid_target = AddressDTO(
            namespace="whatsapp.lid",
            value="100000000000001@lid",
            value_normalized="100000000000001@lid",
            role="primary",
            source_field="test",
            confidence="protocol",
        )
        lid_command = dataclasses.replace(
            valid,
            conversation_ref=lid_target.value_normalized,
            target_address=lid_target,
            conversation=ConversationDTO(
                addresses=(lid_target,), conversation_type="direct"
            ),
        )
        self.assertEqual(
            self.adapter.prepare_request_snapshot(self.connection, lid_command)[
                "payload"
            ]["ChatPhone"],
            "100000000000001@lid",
        )

    @mock.patch(REQUEST_PATCH)
    def test_mark_read_unrecognized_success_ack_is_uncertain(self, request):
        request.return_value = FakeResponse(
            200,
            {"code": 200, "success": True, "data": {}},
        )
        command = self._command(
            command_type="mark_read",
            options={"external_message_ids": ["VALID-MESSAGE-ID"]},
        )

        result = self.adapter.execute_command(self.connection, command)

        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.error_code, "invalid_success_response")

    def test_client_id_is_uppercase_uuid_without_hyphens(self):
        self.assertEqual(
            self.adapter.derive_client_message_id(COMMAND_UUID),
            CLIENT_MESSAGE_ID,
        )
        with self.assertRaises(AdapterError):
            self.adapter.derive_client_message_id("not-a-uuid")

    @mock.patch(REQUEST_PATCH)
    def test_send_text_uses_pinned_endpoint_token_id_and_safe_reply(self, request):
        inbound = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_text_lid.json")
        )
        request.return_value = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {
                    "Details": "Sent",
                    "Timestamp": 1787345200,
                    "Id": CLIENT_MESSAGE_ID,
                },
            },
        )
        command = self._command(
            reply_to={
                "external_message_id": inbound.message.external_message_id,
                "protocol_snapshot": inbound.message.protocol_snapshot,
            },
            protocol_snapshot=inbound.message.protocol_snapshot,
        )

        snapshot = self.adapter.prepare_request_snapshot(self.connection, command)
        self.assertEqual(
            snapshot,
            {
                "provider": "wuzapi",
                "provider_version": WUZAPI_VERSION,
                "method": "POST",
                "endpoint": "/chat/send/text",
                "payload": {
                    "Phone": "5511999999999@s.whatsapp.net",
                    "Body": "Resposta do Odoo",
                    "Id": CLIENT_MESSAGE_ID,
                    "ContextInfo": {
                        "StanzaID": inbound.message.external_message_id,
                        "Participant": "100000000000001@lid",
                    },
                },
            },
        )
        self.assertNotIn(self.connection.wuzapi_api_token, repr(snapshot))
        self.assertNotIn(self.connection.wuzapi_hmac_secret, repr(snapshot))

        result = self.adapter.execute_command(self.connection, command)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.external_message_id, CLIENT_MESSAGE_ID)
        request.assert_called_once()
        args, kwargs = request.call_args
        self.assertEqual(args, ("POST", "https://wuzapi.invalid/chat/send/text"))
        self.assertEqual(kwargs["headers"]["Token"], self.connection.wuzapi_api_token)

        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(
            kwargs["json"],
            {
                "Phone": "5511999999999@s.whatsapp.net",
                "Body": "Resposta do Odoo",
                "Id": CLIENT_MESSAGE_ID,
                "ContextInfo": {
                    "StanzaID": inbound.message.external_message_id,
                    "Participant": "100000000000001@lid",
                },
            },
        )
        serialized_result = json.dumps(result.to_dict(), sort_keys=True)
        self.assertNotIn(self.connection.wuzapi_api_token, serialized_result)
        self.assertNotIn(self.connection.wuzapi_hmac_secret, serialized_result)

    @mock.patch(REQUEST_PATCH)
    def test_send_group_text_uses_the_complete_group_jid(self, request):
        request.return_value = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {"Id": CLIENT_MESSAGE_ID},
            },
        )
        command = self._command(conversation_type="group")

        snapshot = self.adapter.prepare_request_snapshot(self.connection, command)
        self.assertEqual(snapshot["endpoint"], "/chat/send/text")
        self.assertEqual(
            snapshot["payload"],
            {
                "Phone": "120363000000001@g.us",
                "Body": "Resposta do Odoo",
                "Id": CLIENT_MESSAGE_ID,
            },
        )

        result = self.adapter.execute_command(self.connection, command)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.external_message_id, CLIENT_MESSAGE_ID)
        self.assertEqual(
            request.call_args.kwargs["json"]["Phone"],
            "120363000000001@g.us",
        )

    def test_group_send_rejects_a_direct_or_noncanonical_target(self):
        with self.assertRaisesRegex(AdapterError, "group target"):
            self.adapter.prepare_request_snapshot(
                self.connection,
                self._command(
                    conversation_type="group",
                    target_role="primary",
                ),
            )
        with self.assertRaisesRegex(AdapterError, "group JID"):
            self.adapter.prepare_request_snapshot(
                self.connection,
                self._command(
                    conversation_type="group",
                    target_value="5511999999999@s.whatsapp.net",
                ),
            )

    @mock.patch(REQUEST_PATCH)
    def test_group_media_reply_uses_group_jid_and_exact_participant(self, request):
        request.return_value = FakeResponse(
            200,
            {"code": 200, "success": True, "data": {"Id": CLIENT_MESSAGE_ID}},
        )
        participant = AddressDTO(
            namespace="whatsapp.lid",
            value="100000000000001:7@lid",
            value_normalized="100000000000001@lid",
            role="sender",
            source_field="event.Info.Sender",
            confidence="protocol",
        ).to_dict()
        media = self._outbound_media("image", b"group-image", "image/jpeg", "group.jpg")
        command = self._command(
            conversation_type="group",
            media=(media,),
            text="Legenda do grupo",
            reply_to={
                "external_message_id": "GROUP-TARGET",
                "protocol_participant": participant,
            },
        )

        snapshot = self.adapter.prepare_request_snapshot(self.connection, command)
        self.assertEqual(snapshot["endpoint"], "/chat/send/image")
        self.assertEqual(snapshot["payload"]["Phone"], "120363000000001@g.us")
        self.assertEqual(snapshot["payload"]["Caption"], "Legenda do grupo")
        self.assertEqual(
            snapshot["payload"]["ContextInfo"],
            {
                "StanzaID": "GROUP-TARGET",
                "Participant": "100000000000001@lid",
            },
        )
        self.assertNotIn("base64", json.dumps(snapshot).lower())

        result = self.adapter.execute_command(self.connection, command)
        self.assertEqual(result.status, "success")
        self.assertEqual(
            request.call_args.kwargs["json"]["ContextInfo"],
            snapshot["payload"]["ContextInfo"],
        )

    @mock.patch(REQUEST_PATCH)
    def test_group_reply_matrix_supports_pn_lid_text_and_each_media(self, request):
        request.return_value = FakeResponse(
            200,
            {"code": 200, "success": True, "data": {"Id": CLIENT_MESSAGE_ID}},
        )
        media_cases = (
            ("text", None, "/chat/send/text"),
            (
                "image",
                (b"matrix-image", "image/jpeg", "matrix.jpg", False),
                "/chat/send/image",
            ),
            (
                "audio",
                (OGG_OPUS_CONTENT, "audio/ogg; codecs=opus", "matrix.ogg", True),
                "/chat/send/audio",
            ),
            (
                "video",
                (b"matrix-video", "video/mp4", "matrix.mp4", False),
                "/chat/send/video",
            ),
            (
                "document",
                (b"matrix-document", "application/pdf", "matrix.pdf", False),
                "/chat/send/document",
            ),
        )
        participants = (
            ("whatsapp.lid", "100000000000001@lid"),
            ("whatsapp.pn", "15550101001@s.whatsapp.net"),
        )
        for namespace, participant_value in participants:
            participant = AddressDTO(
                namespace=namespace,
                value=participant_value,
                value_normalized=participant_value,
                role="sender",
                source_field="event.Info.Sender",
                confidence="protocol",
            ).to_dict()
            for kind, media_values, endpoint in media_cases:
                with self.subTest(namespace=namespace, kind=kind):
                    media = (
                        (
                            self._outbound_media(
                                kind,
                                media_values[0],
                                media_values[1],
                                media_values[2],
                                is_voice_note=media_values[3],
                            ),
                        )
                        if media_values
                        else ()
                    )
                    command = self._command(
                        conversation_type="group",
                        media=media,
                        text="" if kind == "audio" else "Matrix reply",
                        reply_to={
                            "external_message_id": "GROUP-MATRIX-TARGET",
                            "protocol_participant": participant,
                        },
                    )

                    snapshot = self.adapter.prepare_request_snapshot(
                        self.connection, command
                    )
                    self.assertEqual(snapshot["endpoint"], endpoint)
                    self.assertEqual(
                        snapshot["payload"]["ContextInfo"],
                        {
                            "StanzaID": "GROUP-MATRIX-TARGET",
                            "Participant": participant_value,
                        },
                    )
                    result = self.adapter.execute_command(self.connection, command)
                    self.assertEqual(result.status, "success")
                    self.assertEqual(
                        request.call_args.kwargs["json"]["ContextInfo"],
                        snapshot["payload"]["ContextInfo"],
                    )

    def test_group_reply_rejects_missing_or_non_personal_participant(self):
        cases = (
            {},
            AddressDTO(
                namespace="whatsapp.group",
                value="120363000000001@g.us",
                value_normalized="120363000000001@g.us",
                role="sender",
                source_field="test",
                confidence="protocol",
            ).to_dict(),
            AddressDTO(
                namespace="whatsapp.jid",
                value="status@broadcast",
                value_normalized="status@broadcast",
                role="sender",
                source_field="test",
                confidence="protocol",
            ).to_dict(),
            AddressDTO(
                namespace="whatsapp.lid",
                value="abc@lid",
                value_normalized="abc@lid",
                role="sender",
                source_field="test",
                confidence="protocol",
            ).to_dict(),
        )
        for participant in cases:
            with self.subTest(participant=participant), self.assertRaises(AdapterError):
                self.adapter.prepare_reply_reference(
                    self.connection,
                    conversation_type="group",
                    external_message_id="GROUP-TARGET",
                    protocol_snapshot={
                        "reply_to": {"participant": "must-not-be-used@lid"}
                    },
                    protocol_participant=participant,
                )

        valid_participant = AddressDTO(
            namespace="whatsapp.lid",
            value="100000000000001@lid",
            value_normalized="100000000000001@lid",
            role="sender",
            source_field="test",
            confidence="protocol",
        ).to_dict()
        with self.assertRaisesRegex(AdapterError, "target message ID"):
            self.adapter.prepare_reply_reference(
                self.connection,
                conversation_type="group",
                external_message_id="",
                protocol_snapshot={},
                protocol_participant=valid_participant,
            )

    @mock.patch(REQUEST_PATCH)
    def test_send_response_with_another_id_is_uncertain(self, request):
        request.return_value = FakeResponse(
            200,
            {"code": 200, "success": True, "data": {"Id": "ANOTHER-ID"}},
        )

        result = self.adapter.execute_command(self.connection, self._command())

        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.error_code, "correlation_mismatch")
        self.assertEqual(result.provider_response["data"]["id"], "ANOTHER-ID")

    @mock.patch(REQUEST_PATCH)
    def test_reply_context_is_omitted_when_participant_is_not_observed(self, request):
        request.return_value = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {"Details": "Sent", "Id": CLIENT_MESSAGE_ID},
            },
        )
        command = self._command(reply_to={"external_message_id": "ORIGINAL-MESSAGE"})

        self.adapter.execute_command(self.connection, command)

        self.assertNotIn("ContextInfo", request.call_args.kwargs["json"])

    @mock.patch(REQUEST_PATCH)
    def test_send_all_supported_media_uses_attachment_data_and_provider_endpoint(
        self, request
    ):
        request.return_value = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {"Details": "Sent", "Id": CLIENT_MESSAGE_ID},
            },
        )
        cases = (
            ("image", b"out-image", "image/jpeg", "foto.jpg", "Image"),
            (
                "audio",
                OGG_OPUS_CONTENT,
                "audio/ogg; codecs=opus",
                "audio.ogg",
                "Audio",
            ),
            ("video", b"out-video", "video/mp4", "video.mp4", "Video"),
            (
                "document",
                b"out-document",
                "application/pdf",
                "arquivo.pdf",
                "Document",
            ),
        )
        for kind, content, mime_type, file_name, payload_field in cases:
            with self.subTest(kind=kind):
                request.reset_mock()
                media = self._outbound_media(
                    kind,
                    content,
                    mime_type,
                    file_name,
                    is_voice_note=kind == "audio",
                    duration_seconds=3 if kind == "audio" else 0,
                )
                command = self._command(
                    media=(media,), text="" if kind == "audio" else "Legenda"
                )
                snapshot = self.adapter.prepare_request_snapshot(
                    self.connection, command
                )
                safe_media = snapshot["payload"][payload_field]
                self.assertEqual(snapshot["method"], "POST")
                self.assertEqual(snapshot["endpoint"], "/chat/send/%s" % kind)
                self.assertTrue(safe_media["content_omitted"])
                self.assertEqual(safe_media["kind"], kind)
                self.assertEqual(safe_media["mime_type"], mime_type)
                self.assertEqual(safe_media["file_name"], file_name)
                self.assertEqual(safe_media["size_bytes"], len(content))
                self.assertEqual(
                    safe_media["sha256"], hashlib.sha256(content).hexdigest()
                )
                serialized_snapshot = json.dumps(snapshot, sort_keys=True)
                self.assertNotIn("base64", serialized_snapshot.lower())
                self.assertNotIn("attachment_id", serialized_snapshot)
                self.assertNotIn(self.connection.wuzapi_api_token, serialized_snapshot)
                self.assertNotIn(
                    self.connection.wuzapi_hmac_secret, serialized_snapshot
                )
                result = self.adapter.execute_command(
                    self.connection,
                    command,
                )
                self.assertEqual(result.status, "success")
                args, kwargs = request.call_args
                self.assertEqual(
                    args,
                    ("POST", "https://wuzapi.invalid/chat/send/%s" % kind),
                )
                payload = kwargs["json"]
                self.assertTrue(payload[payload_field].startswith("data:"))
                self.assertNotIn("attachment_id", repr(payload))
                self.assertEqual(payload["Id"], CLIENT_MESSAGE_ID)
                if kind == "document":
                    self.assertEqual(payload["FileName"], file_name)
                if kind == "audio":
                    self.assertTrue(payload["ptt"])
                    self.assertEqual(payload["Seconds"], 3)
                    self.assertNotIn("Caption", payload)
                else:
                    self.assertEqual(payload["Caption"], "Legenda")

    def test_audio_caption_is_rejected_before_provider_dispatch(self):
        media = self._outbound_media(
            "audio",
            b"out-audio-caption",
            "audio/ogg; codecs=opus",
            "audio.ogg",
            is_voice_note=True,
        )
        command = self._command(media=(media,), text="Legenda que seria perdida")

        with self.assertRaisesRegex(AdapterError, "do not support captions"):
            self.adapter.prepare_request_snapshot(self.connection, command)

    def test_voice_note_requires_ogg_opus_but_regular_mp4_audio_is_supported(self):
        for content, mimetype, file_name in (
            (b"webm-opus-is-not-ogg", "audio/webm", "recording.webm"),
            (
                b"OggS\x00\x02" + (b"\x00" * 20) + b"\x01\x08\x01vorbis",
                "audio/ogg",
                "recording.ogg",
            ),
        ):
            with self.subTest(mimetype=mimetype):
                voice_note = self._outbound_media(
                    "audio",
                    content,
                    mimetype,
                    file_name,
                    is_voice_note=True,
                    duration_seconds=3,
                )
                with self.assertRaisesRegex(AdapterError, "require an OGG Opus"):
                    self.adapter.prepare_request_snapshot(
                        self.connection,
                        self._command(media=(voice_note,), text=""),
                    )

        regular_audio = self._outbound_media(
            "audio",
            _mp4_with_handlers(b"soun", duration_seconds=4),
            "audio/mp4",
            "recording.m4a",
            is_voice_note=False,
            duration_seconds=4,
        )
        snapshot = self.adapter.prepare_request_snapshot(
            self.connection, self._command(media=(regular_audio,), text="")
        )
        self.assertFalse(snapshot["payload"]["ptt"])
        self.assertEqual(snapshot["payload"]["Seconds"], 4)
        self.assertEqual(snapshot["payload"]["mimetype"], "audio/mp4")

    def test_recorded_audio_upload_validates_provider_specific_containers(self):
        self.assertTrue(
            self.adapter.validate_recorded_audio_upload(
                self.connection,
                content=OGG_OPUS_CONTENT,
                mimetype="audio/ogg",
                is_voice_note=True,
                duration_seconds=3,
            )
        )
        self.assertTrue(
            self.adapter.validate_recorded_audio_upload(
                self.connection,
                content=_mp4_with_handlers(b"soun"),
                mimetype="audio/mp4",
                is_voice_note=False,
                duration_seconds=3,
            )
        )
        for content, mimetype, is_voice_note in (
            (_mp4_with_handlers(b"vide"), "audio/mp4", False),
            (_mp4_with_handlers(b"soun", b"vide"), "audio/mp4", False),
            (b"OggS-not-opus", "audio/ogg", True),
            (b"OggS-not-opus", "audio/ogg", False),
        ):
            with self.subTest(mimetype=mimetype), self.assertRaises(AdapterError):
                self.adapter.validate_recorded_audio_upload(
                    self.connection,
                    content=content,
                    mimetype=mimetype,
                    is_voice_note=is_voice_note,
                    duration_seconds=3,
                )

        tampered_recording = self._outbound_media(
            "audio",
            _mp4_with_handlers(b"vide"),
            "audio/mp4",
            "recording.m4a",
            is_voice_note=False,
            duration_seconds=3,
        )
        with self.assertRaisesRegex(AdapterError, "must not contain a video track"):
            self.adapter.prepare_request_snapshot(
                self.connection,
                self._command(media=(tampered_recording,), text=""),
            )

    def test_outbound_media_reads_raw_without_decoding_datas(self):
        content = b"raw-outbound-document"
        media = self._outbound_media(
            "document", content, "application/pdf", "arquivo.pdf"
        )
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .browse(media.remote_locator["attachment_id"])
        )
        attachment.invalidate_recordset(["raw", "datas"])

        with mock.patch(
            "odoo.addons.contact_center_wuzapi.services.adapter.base64.b64decode",
            side_effect=AssertionError(
                "outbound media must not decode attachment.datas"
            ),
        ):
            result, mime_type, file_name = self.adapter._outbound_media_content(media)

        self.assertEqual(result, content)
        self.assertEqual(mime_type, "application/pdf")
        self.assertEqual(file_name, "arquivo.pdf")

    @mock.patch(REQUEST_PATCH)
    def test_outbound_media_is_base64_encoded_once_at_dispatch(self, request):
        request.return_value = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {"Details": "Sent", "Id": CLIENT_MESSAGE_ID},
            },
        )
        content = b"encode-only-at-provider-boundary"
        media = self._outbound_media(
            "document", content, "application/pdf", "arquivo.pdf"
        )
        command = self._command(media=(media,))
        original_b64encode = base64.b64encode

        with mock.patch(
            "odoo.addons.contact_center_wuzapi.services.adapter.base64.b64encode",
            wraps=original_b64encode,
        ) as encode:
            self.adapter.prepare_request_snapshot(self.connection, command)
            result = self.adapter.execute_command(self.connection, command)

        self.assertEqual(result.status, "success")
        self.assertEqual(encode.call_count, 1)

    def test_outbound_media_raw_supports_database_backed_attachment(self):
        content = b"database-backed-document"
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .create(
                {
                    "name": "banco.pdf",
                    "type": "binary",
                    "db_datas": content,
                    "file_size": len(content),
                    "checksum": hashlib.sha1(content).hexdigest(),
                    "mimetype": "application/pdf",
                }
            )
        )
        media = MediaDTO(
            kind="document",
            external_media_id="database-attachment",
            remote_locator={"attachment_id": attachment.id},
            mime_type="application/pdf",
            file_name="banco.pdf",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

        self.assertFalse(attachment.store_fname)
        self.assertEqual(
            self.adapter._outbound_media_content(media),
            (content, "application/pdf", "banco.pdf"),
        )

    def test_outbound_media_rejects_size_metadata_before_reading_raw(self):
        content = b"size-checked-before-read"
        media = self._outbound_media(
            "document", content, "application/pdf", "arquivo.pdf"
        )
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .browse(media.remote_locator["attachment_id"])
        )
        attachment.invalidate_recordset(["raw", "datas"])
        mismatched = dataclasses.replace(media, size_bytes=len(content) + 1)

        with mock.patch.object(
            type(attachment),
            "_compute_raw",
            side_effect=AssertionError("raw content must not be read"),
        ):
            with self.assertRaisesRegex(AdapterError, "size does not match metadata"):
                self.adapter._outbound_media_content(mismatched)

    @mock.patch(REQUEST_PATCH)
    def test_send_reaction_edit_and_delete_contracts(self, request):
        target = "TARGET-MESSAGE-0001"
        request.return_value = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {"Details": "Sent", "Id": target},
            },
        )
        cases = (
            (
                "react",
                {
                    "target_external_message_id": target,
                    "emoji": "❤️",
                    "operation": "add",
                    "target_from_me": True,
                },
                "/chat/react",
                {
                    "Phone": "5511999999999@s.whatsapp.net",
                    "Body": "❤️",
                    "Id": "me:" + target,
                },
            ),
            (
                "react",
                {
                    "target_external_message_id": target,
                    "operation": "remove",
                    "target_from_me": False,
                    "target_participant": "5511999999999:4@s.whatsapp.net",
                },
                "/chat/react",
                {
                    "Phone": "5511999999999@s.whatsapp.net",
                    "Body": "remove",
                    "Id": target,
                    "Participant": "5511999999999@s.whatsapp.net",
                },
            ),
            (
                "edit_message",
                {"target_external_message_id": target, "new_text": "Corrigido"},
                "/chat/send/edit",
                {
                    "Phone": "5511999999999@s.whatsapp.net",
                    "Body": "Corrigido",
                    "Id": target,
                },
            ),
            (
                "delete_message",
                {"target_external_message_id": target},
                "/chat/delete",
                {"Phone": "5511999999999@s.whatsapp.net", "Id": target},
            ),
        )
        for command_type, options, endpoint, expected_payload in cases:
            with self.subTest(command_type=command_type, options=options):
                request.reset_mock()
                command = self._command(
                    command_type=command_type,
                    options=options,
                    text=options.get("new_text", ""),
                )
                snapshot = self.adapter.prepare_request_snapshot(
                    self.connection, command
                )
                self.assertEqual(snapshot["method"], "POST")
                self.assertEqual(snapshot["endpoint"], endpoint)
                self.assertEqual(snapshot["payload"], expected_payload)
                self.assertNotIn(self.connection.wuzapi_api_token, repr(snapshot))
                self.assertNotIn(self.connection.wuzapi_hmac_secret, repr(snapshot))
                result = self.adapter.execute_command(
                    self.connection,
                    command,
                )
                self.assertEqual(result.status, "success")
                self.assertEqual(
                    request.call_args.args,
                    ("POST", "https://wuzapi.invalid%s" % endpoint),
                )
                self.assertEqual(request.call_args.kwargs["json"], expected_payload)

    @mock.patch(REQUEST_PATCH)
    def test_send_group_mutations_use_structured_participants(self, request):
        target = "TARGET-GROUP-MESSAGE-0001"
        own_participant = AddressDTO(
            namespace="whatsapp.lid",
            value="200000000000009@lid",
            value_normalized="200000000000009@lid",
            role="sender",
            source_field="group_profile.own_protocol_participant",
            confidence="protocol",
        )
        remote_participant = AddressDTO(
            namespace="whatsapp.pn",
            value="5511900000001:4@s.whatsapp.net",
            value_normalized="5511900000001@s.whatsapp.net",
            role="sender",
            source_field="message.protocol_participant",
            confidence="protocol",
        )
        request.return_value = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {"Details": "Sent", "Id": target},
            },
        )
        cases = (
            (
                "react",
                {
                    "target_external_message_id": target,
                    "emoji": "❤️",
                    "operation": "add",
                    "target_from_me": False,
                },
                remote_participant,
                "/chat/react",
                {
                    "Phone": "120363000000001@g.us",
                    "Body": "❤️",
                    "Id": target,
                    "Participant": "5511900000001@s.whatsapp.net",
                },
            ),
            (
                "react",
                {
                    "target_external_message_id": target,
                    "operation": "remove",
                    "target_from_me": True,
                },
                own_participant,
                "/chat/react",
                {
                    "Phone": "120363000000001@g.us",
                    "Body": "remove",
                    "Id": "me:" + target,
                },
            ),
            (
                "edit_message",
                {
                    "target_external_message_id": target,
                    "target_from_me": True,
                    "new_text": "Corrigido no grupo",
                },
                own_participant,
                "/chat/send/edit",
                {
                    "Phone": "120363000000001@g.us",
                    "Body": "Corrigido no grupo",
                    "Id": target,
                },
            ),
            (
                "delete_message",
                {
                    "target_external_message_id": target,
                    "target_from_me": True,
                },
                own_participant,
                "/chat/delete",
                {"Phone": "120363000000001@g.us", "Id": target},
            ),
        )
        for command_type, options, target_participant, endpoint, payload in cases:
            with self.subTest(command_type=command_type, options=options):
                request.reset_mock()
                command = self._command(
                    command_type=command_type,
                    options=options,
                    text=options.get("new_text", ""),
                    conversation_type="group",
                    own_protocol_participant=own_participant,
                    target_protocol_participant=target_participant,
                )

                snapshot = self.adapter.prepare_request_snapshot(
                    self.connection, command
                )
                result = self.adapter.execute_command(self.connection, command)

                self.assertEqual(snapshot["endpoint"], endpoint)
                self.assertEqual(snapshot["payload"], payload)
                self.assertEqual(result.status, "success")
                self.assertEqual(
                    request.call_args.args,
                    ("POST", "https://wuzapi.invalid%s" % endpoint),
                )
                self.assertEqual(request.call_args.kwargs["json"], payload)

    def test_group_mutation_dispatch_fails_closed_on_scope_or_participant(self):
        own_participant = AddressDTO(
            namespace="whatsapp.lid",
            value="200000000000009@lid",
            value_normalized="200000000000009@lid",
            role="sender",
            source_field="group_profile.own_protocol_participant",
            confidence="protocol",
        )
        remote_participant = AddressDTO(
            namespace="whatsapp.pn",
            value="5511900000001@s.whatsapp.net",
            value_normalized="5511900000001@s.whatsapp.net",
            role="sender",
            source_field="message.protocol_participant",
            confidence="protocol",
        )
        target = "TARGET-GROUP-MESSAGE-0001"
        for command_type in ("edit_message", "delete_message"):
            with self.subTest(command_type=command_type):
                command = self._command(
                    command_type=command_type,
                    options={
                        "target_external_message_id": target,
                        "target_from_me": False,
                        "new_text": "Remote edit",
                    },
                    text="Remote edit",
                    conversation_type="group",
                    own_protocol_participant=own_participant,
                    target_protocol_participant=remote_participant,
                )
                with self.assertRaisesRegex(AdapterError, "only .* own messages"):
                    self.adapter.prepare_request_snapshot(self.connection, command)

        malformed_participant = dataclasses.replace(
            remote_participant,
            namespace="whatsapp.jid",
        )
        command = self._command(
            command_type="react",
            options={
                "target_external_message_id": target,
                "target_from_me": False,
                "emoji": "👍",
                "operation": "add",
            },
            conversation_type="group",
            own_protocol_participant=own_participant,
            target_protocol_participant=malformed_participant,
        )
        with self.assertRaisesRegex(
            AdapterError, "target_protocol_participant is invalid"
        ):
            self.adapter.prepare_request_snapshot(self.connection, command)

    @mock.patch(REQUEST_PATCH)
    def test_download_all_supported_media_validates_size_hash_and_caps(self, request):
        cases = (
            ("message_image.json", b"fixture-image", "/chat/downloadimage"),
            ("message_audio.json", b"fixture-audio", "/chat/downloadaudio"),
            ("message_video.json", b"fixture-video", "/chat/downloadvideo"),
            (
                "message_document.json",
                b"fixture-document",
                "/chat/downloaddocument",
            ),
        )
        for fixture, content, endpoint in cases:
            with self.subTest(fixture=fixture):
                media = self.adapter.normalize_event(
                    self.connection, self.load_fixture(fixture)
                ).message.media[0]
                request.return_value = FakeResponse(
                    200,
                    {
                        "code": 200,
                        "success": True,
                        "data": {
                            "Mimetype": media.mime_type,
                            "Data": "data:%s;base64,%s"
                            % (
                                media.mime_type.replace(" ", ""),
                                base64.b64encode(content).decode("ascii"),
                            ),
                        },
                    },
                )
                result = self.adapter.download_media(self.connection, media)
                self.assertEqual(result.content, content)
                self.assertEqual(result.size_bytes, len(content))
                self.assertEqual(result.sha256, hashlib.sha256(content).hexdigest())
                self.assertTrue(request.return_value.closed)
                self.assertEqual(
                    request.call_args.args,
                    ("POST", "https://wuzapi.invalid%s" % endpoint),
                )
                self.assertTrue(request.call_args.kwargs["stream"])
                self.assertNotIn("base64", repr(request.call_args.kwargs["json"]))

        media = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_image.json")
        ).message.media[0]
        request.return_value = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {
                    "Mimetype": "image/jpeg",
                    "Data": "data:image/jpeg;base64,%s"
                    % base64.b64encode(b"tampered-data").decode("ascii"),
                },
            },
        )
        with self.assertRaises(AdapterError):
            self.adapter.download_media(
                self.connection,
                dataclasses.replace(media, size_bytes=len(b"tampered-data")),
            )
        request.reset_mock()
        with self.assertRaises(AdapterError):
            self.adapter.download_media(
                self.connection,
                dataclasses.replace(
                    media,
                    size_bytes=17 * 1024 * 1024,
                    remote_locator=dict(
                        media.remote_locator, file_length=17 * 1024 * 1024
                    ),
                ),
            )
        request.assert_not_called()

    @mock.patch(REQUEST_PATCH)
    def test_download_image_document_falls_back_only_for_invalid_media_hmac(
        self, request
    ):
        media = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_document.json")
        ).message.media[0]
        content = b"fixture-document"
        invalid_hmac_payload = {
            "code": 500,
            "error": (
                "failed to download document failed to download media from last "
                "host: invalid media hmac"
            ),
            "success": False,
        }

        for document_mime, response_mime in (
            ("image/jpeg; name=photo.jpg", "image/jpeg"),
            ("image/png", "image/png"),
        ):
            with self.subTest(document_mime=document_mime):
                image_document = dataclasses.replace(media, mime_type=document_mime)
                canonical_response = FakeResponse(500, invalid_hmac_payload)
                fallback_response = FakeResponse(
                    200,
                    {
                        "code": 200,
                        "success": True,
                        "data": {
                            "Mimetype": response_mime,
                            "Data": "data:%s;base64,%s"
                            % (
                                response_mime,
                                base64.b64encode(content).decode("ascii"),
                            ),
                        },
                    },
                )
                request.side_effect = [canonical_response, fallback_response]

                result = self.adapter.download_media(self.connection, image_document)

                self.assertEqual(result.content, content)
                self.assertEqual(result.mime_type, response_mime)
                self.assertEqual(
                    [call.args[1] for call in request.call_args_list],
                    [
                        "https://wuzapi.invalid/chat/downloaddocument",
                        "https://wuzapi.invalid/chat/downloadimage",
                    ],
                )
                self.assertTrue(canonical_response.closed)
                self.assertTrue(fallback_response.closed)
                request.reset_mock(side_effect=True)

    @mock.patch(REQUEST_PATCH)
    def test_download_image_document_hmac_fallback_is_strict(self, request):
        media = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_document.json")
        ).message.media[0]
        image_document = dataclasses.replace(media, mime_type="image/jpeg")
        invalid_hmac_payload = {
            "code": 500,
            "error": "download failed: invalid media hmac",
            "success": False,
        }

        cases = (
            (media, invalid_hmac_payload, {}),
            (
                image_document,
                {
                    "code": 500,
                    "error": "download failed: media expired",
                    "success": False,
                },
                {},
            ),
            (
                image_document,
                invalid_hmac_payload,
                {"Content-Length": str(16 * 1024 + 1)},
            ),
        )
        for candidate, payload, headers in cases:
            with self.subTest(mime_type=candidate.mime_type, payload=payload):
                response = FakeResponse(500, payload, headers=headers)
                request.return_value = response

                with self.assertRaises(TransientAdapterError):
                    self.adapter.download_media(self.connection, candidate)

                self.assertEqual(request.call_count, 1)
                self.assertTrue(response.closed)
                request.reset_mock(return_value=True)

        for response_mime, error_pattern in (
            ("application/pdf", "image MIME type is not supported"),
            ("image/png", "fallback MIME does not match"),
        ):
            with self.subTest(response_mime=response_mime):
                canonical_response = FakeResponse(500, invalid_hmac_payload)
                fallback_response = FakeResponse(
                    200,
                    {
                        "code": 200,
                        "success": True,
                        "data": {
                            "Mimetype": response_mime,
                            "Data": "data:%s;base64,%s"
                            % (
                                response_mime,
                                base64.b64encode(b"fixture-document").decode("ascii"),
                            ),
                        },
                    },
                )
                request.side_effect = [canonical_response, fallback_response]

                with self.assertRaisesRegex(AdapterError, error_pattern):
                    self.adapter.download_media(self.connection, image_document)

                self.assertEqual(request.call_count, 2)
                self.assertTrue(canonical_response.closed)
                self.assertTrue(fallback_response.closed)
                request.reset_mock(side_effect=True)

        canonical_response = FakeResponse(500, invalid_hmac_payload)
        tampered_content = b"tampered-content"
        fallback_response = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {
                    "Mimetype": "image/jpeg",
                    "Data": "data:image/jpeg;base64,%s"
                    % base64.b64encode(tampered_content).decode("ascii"),
                },
            },
        )
        request.side_effect = [canonical_response, fallback_response]

        with self.assertRaisesRegex(AdapterError, "hash does not match"):
            self.adapter.download_media(self.connection, image_document)

        self.assertEqual(request.call_count, 2)
        self.assertTrue(canonical_response.closed)
        self.assertTrue(fallback_response.closed)

    @mock.patch(REQUEST_PATCH)
    def test_download_rejects_mime_conflicts_and_classifies_http_errors(self, request):
        media = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_image.json")
        ).message.media[0]
        content = b"fixture-image"
        request.return_value = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {
                    "Mimetype": "image/jpeg",
                    "Data": "data:image/png;base64,%s"
                    % base64.b64encode(content).decode("ascii"),
                },
            },
        )
        with self.assertRaises(AdapterError):
            self.adapter.download_media(self.connection, media)

        for status_code, error_class in (
            (401, ProviderPausedError),
            (408, TransientAdapterError),
            (425, TransientAdapterError),
            (429, ProviderRateLimitError),
            (503, TransientAdapterError),
            (400, AdapterError),
        ):
            with self.subTest(status_code=status_code):
                response = FakeResponse(status_code, {})
                request.return_value = response
                with self.assertRaises(error_class):
                    self.adapter.download_media(self.connection, media)
                self.assertTrue(response.closed)

    @mock.patch(REQUEST_PATCH)
    def test_download_rejects_incomplete_locators_and_malformed_payloads(self, request):
        media = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_image.json")
        ).message.media[0]
        with self.assertRaises(AdapterError):
            self.adapter.download_media(
                self.connection, dataclasses.replace(media, remote_locator={})
            )
        request.assert_not_called()

        malformed_payloads = (
            {"code": 200, "success": True, "data": {}},
            {
                "code": 200,
                "success": True,
                "data": {
                    "Mimetype": "image/jpeg",
                    "Data": "data:image/jpeg;base64,not-valid-base64!",
                },
            },
            {
                "code": 200,
                "success": True,
                "data": {
                    "Mimetype": "image/jpeg",
                    "Data": "data:image/jpeg;base64,",
                },
            },
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                request.return_value = FakeResponse(200, payload)
                with self.assertRaises(AdapterError):
                    self.adapter.download_media(self.connection, media)
                self.assertTrue(request.return_value.closed)

        embedded_rate_limit = FakeResponse(
            200,
            {"code": 429, "success": False, "data": {}},
            headers={"Retry-After": "23"},
        )
        request.return_value = embedded_rate_limit
        with self.assertRaises(ProviderRateLimitError) as raised:
            self.adapter.download_media(self.connection, media)
        self.assertEqual(raised.exception.retry_after_seconds, 23)
        self.assertTrue(embedded_rate_limit.closed)

    @mock.patch(REQUEST_PATCH)
    def test_download_classifies_request_and_stream_interruptions(self, request):
        media = self.adapter.normalize_event(
            self.connection, self.load_fixture("message_image.json")
        ).message.media[0]
        secret = "https://mmg.whatsapp.net/media?token=private-synthetic-token"
        request.side_effect = requests.Timeout(secret)
        with self.assertRaises(TransientAdapterError) as caught:
            self.adapter.download_media(self.connection, media)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(secret, "".join(traceback.format_exception(caught.exception)))

        interrupted = FakeResponse(200, {})
        interrupted.iter_content = mock.Mock(
            side_effect=requests.ConnectionError(secret)
        )
        request.side_effect = None
        request.return_value = interrupted
        with self.assertRaises(TransientAdapterError) as caught:
            self.adapter.download_media(self.connection, media)
        self.assertTrue(interrupted.closed)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(secret, "".join(traceback.format_exception(caught.exception)))

    def test_provider_json_readers_reject_deep_or_private_invalid_payloads(self):
        readers = (
            lambda response: _limited_json_object(response, 64 * 1024, "WuzAPI"),
            lambda response: self.adapter._limited_download_json(response, 64 * 1024),
        )
        for reader in readers:
            for raw in (
                b"[" * 2000 + b"0" + b"]" * 2000,
                b'{"private-synthetic-token":"\xff"}',
            ):
                response = FakeResponse(200)
                response.iter_content = mock.Mock(return_value=iter([raw]))
                with self.subTest(reader=reader, raw_size=len(raw)), self.assertRaises(
                    AdapterError
                ) as caught:
                    reader(response)
                self.assertTrue(response.closed)
                self.assertIsNone(caught.exception.__cause__)
                self.assertNotIn(
                    "private-synthetic-token",
                    "".join(traceback.format_exception(caught.exception)),
                )

    def test_malformed_optional_attribution_url_does_not_drop_the_message(self):
        self.assertEqual(_safe_attribution_url("https://[invalid"), "")

    def test_http_failures_are_classified_without_real_network(self):
        cases = (
            (400, {}, "permanent", 0),
            (401, {}, "paused", 0),
            (403, {}, "paused", 0),
            (408, {}, "uncertain", 0),
            (425, {}, "uncertain", 0),
            (429, {}, "transient", 60),
            (429, {"Retry-After": "17"}, "transient", 17),
            (429, {"Retry-After": "7200"}, "transient", 3600),
            (503, {}, "uncertain", 0),
        )
        for status_code, headers, expected_status, retry_after in cases:
            with self.subTest(status_code=status_code), mock.patch(
                REQUEST_PATCH,
                return_value=FakeResponse(status_code, {}, headers=headers),
            ):
                result = self.adapter.execute_command(self.connection, self._command())
                self.assertEqual(result.status, expected_status)
                self.assertEqual(result.retry_after_seconds, retry_after)
                if status_code == 429:
                    self.assertEqual(result.error_code, "http_429")

        for exception in (
            requests.Timeout("read timeout"),
            requests.ConnectionError("no response"),
        ):
            with self.subTest(exception=exception.__class__.__name__), mock.patch(
                REQUEST_PATCH, side_effect=exception
            ):
                result = self.adapter.execute_command(self.connection, self._command())
                self.assertEqual(result.status, "uncertain")

    def test_outbound_dispatch_is_paced_per_connection(self):
        self.assertEqual(
            self.adapter.outbound_min_interval_seconds(self.connection),
            1,
        )

    def test_pre_dispatch_connection_failures_are_retryable(self):
        new_connection = NewConnectionError(None, "connection refused")
        refused = requests.ConnectionError(
            MaxRetryError(None, "/chat/send/text", reason=new_connection)
        )
        for exception in (
            requests.ConnectTimeout("connect timeout"),
            refused,
        ):
            with self.subTest(exception=exception.__class__.__name__), mock.patch(
                REQUEST_PATCH, side_effect=exception
            ):
                result = self.adapter.execute_command(self.connection, self._command())
                self.assertEqual(result.status, "transient")
                self.assertEqual(result.error_code, "transport_not_connected")

    def test_post_connect_transport_failures_remain_uncertain(self):
        for exception in (
            requests.ReadTimeout("read timeout"),
            requests.ConnectionError("connection reset after request bytes"),
        ):
            with self.subTest(exception=exception.__class__.__name__), mock.patch(
                REQUEST_PATCH, side_effect=exception
            ):
                result = self.adapter.execute_command(self.connection, self._command())
                self.assertEqual(result.status, "uncertain")
                self.assertEqual(result.error_code, "transport_no_response")

    def test_success_without_correlatable_id_is_uncertain(self):
        with mock.patch(
            REQUEST_PATCH,
            return_value=FakeResponse(
                200,
                {"code": 200, "success": True, "data": {"Details": "Sent"}},
            ),
        ):
            result = self.adapter.execute_command(self.connection, self._command())
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.error_code, "missing_provider_message_id")

    @mock.patch(REQUEST_PATCH)
    def test_command_response_is_bounded_streamed_and_closed(self, request):
        oversized = FakeResponse(
            200,
            {
                "success": True,
                "data": {"Id": "provider-id", "padding": "x" * (64 * 1024)},
            },
        )
        request.return_value = oversized

        result = self.adapter.execute_command(self.connection, self._command())

        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.error_code, "invalid_success_response")
        self.assertTrue(oversized.closed)
        self.assertTrue(request.call_args.kwargs["stream"])

        rejected = FakeResponse(400, {})
        request.return_value = rejected
        result = self.adapter.execute_command(self.connection, self._command())
        self.assertEqual(result.status, "permanent")
        self.assertTrue(rejected.closed)

    @mock.patch(REQUEST_PATCH)
    def test_health_uses_session_status_and_never_returns_secrets(self, request):
        self.account.own_external_identity = "5511888888888@s.whatsapp.net"
        request.return_value = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {
                    "connected": True,
                    "loggedIn": True,
                    "jid": "5511888888888@s.whatsapp.net",
                    "token": self.connection.wuzapi_api_token,
                    "hmac_key": self.connection.wuzapi_hmac_secret,
                },
            },
        )

        health = self.adapter.get_health(self.connection)

        self.assertTrue(health["available"])
        self.assertEqual(health["state"], "connected")
        self.assertTrue(health["connected"])
        self.assertTrue(health["logged_in"])
        self.assertEqual(health["baseline_version"], "v1.0.8")
        self.assertEqual(health["baseline_commit"], "9487eca")
        self.assertNotIn("jid", health)
        self.assertNotIn(self.connection.wuzapi_api_token, repr(health))
        self.assertNotIn(self.connection.wuzapi_hmac_secret, repr(health))
        args, kwargs = request.call_args_list[0]
        self.assertEqual(args, ("GET", "https://wuzapi.invalid/session/status"))
        self.assertEqual(kwargs["headers"]["Token"], self.connection.wuzapi_api_token)
        self.assertEqual(kwargs["timeout"], (3, 10))
        self.assertTrue(kwargs["stream"])
        self.assertTrue(request.return_value.closed)

    @mock.patch(REQUEST_PATCH)
    def test_health_distinguishes_session_states_and_degraded_schema(self, request):
        self.account.own_external_identity = "5511888888888@s.whatsapp.net"
        cases = (
            (True, True, "connected", "ready"),
            (False, True, "disconnected", "session_disconnected"),
            (True, False, "authentication_required", "session_not_authenticated"),
            (False, False, "authentication_required", "session_not_authenticated"),
        )
        for connected, logged_in, expected_state, expected_reason in cases:
            with self.subTest(connected=connected, logged_in=logged_in):
                request.return_value = FakeResponse(
                    200,
                    {
                        "code": 200,
                        "success": True,
                        "data": {
                            "connected": connected,
                            "loggedIn": logged_in,
                            "jid": "5511888888888@s.whatsapp.net",
                        },
                    },
                )
                health = self.adapter.get_health(self.connection)
                self.assertTrue(health["available"])
                self.assertEqual(health["state"], expected_state)
                self.assertEqual(health["reason"], expected_reason)
                self.assertEqual(health["connected"], connected)
                self.assertEqual(health["logged_in"], logged_in)

        for payload, expected_reason in (
            (
                {"code": 200, "success": True, "data": {"connected": True}},
                "invalid_response",
            ),
            ({"code": 200, "success": False, "data": {}}, "invalid_response"),
        ):
            with self.subTest(expected_reason=expected_reason):
                request.return_value = FakeResponse(200, payload)
                health = self.adapter.get_health(self.connection)
                self.assertTrue(health["available"])
                self.assertEqual(health["state"], "degraded")
                self.assertEqual(health["reason"], expected_reason)

    @mock.patch(REQUEST_PATCH)
    def test_health_failures_are_short_safe_and_provider_neutral(self, request):
        sensitive_payload = {
            "token": self.connection.wuzapi_api_token,
            "url": self.connection.wuzapi_base_url,
        }
        cases = (
            (requests.Timeout("health read timeout"), None, "error", "unreachable"),
            (None, FakeResponse(401, sensitive_payload), "error", "unauthorized"),
            (None, FakeResponse(503, sensitive_payload), "error", "provider_error"),
            (
                None,
                FakeResponse(429, sensitive_payload, headers={"Retry-After": "19"}),
                "paused",
                "rate_limited",
            ),
        )
        for exception, response, expected_state, expected_reason in cases:
            with self.subTest(expected_state=expected_state, reason=expected_reason):
                request.reset_mock(side_effect=True, return_value=True)
                request.side_effect = exception
                request.return_value = response

                health = self.adapter.get_health(self.connection)

                self.assertFalse(health["available"])
                self.assertEqual(health["state"], expected_state)
                self.assertEqual(health["reason"], expected_reason)
                self.assertNotIn(self.connection.wuzapi_api_token, repr(health))
                self.assertNotIn(self.connection.wuzapi_base_url, repr(health))
                self.assertEqual(request.call_args.kwargs["timeout"], (3, 10))
                if expected_reason == "rate_limited":
                    self.assertEqual(health["retry_after_seconds"], 19)
                if response is not None:
                    self.assertTrue(response.closed)

    @mock.patch(REQUEST_PATCH)
    def test_health_response_is_bounded_streamed_and_closed(self, request):
        oversized = FakeResponse(
            200,
            {
                "success": True,
                "data": {"connected": True, "loggedIn": True},
            },
            headers={"Content-Length": str(64 * 1024 + 1)},
        )
        request.return_value = oversized

        health = self.adapter.get_health(self.connection)

        self.assertTrue(health["available"])
        self.assertEqual(health["state"], "degraded")
        self.assertEqual(health["reason"], "invalid_response")
        self.assertTrue(oversized.closed)
        self.assertTrue(request.call_args.kwargs["stream"])

        interrupted = FakeResponse(200, {})
        interrupted.iter_content = mock.Mock(
            side_effect=requests.ConnectionError("stream interrupted")
        )
        request.return_value = interrupted

        health = self.adapter.get_health(self.connection)

        self.assertFalse(health["available"])
        self.assertEqual(health["state"], "error")
        self.assertEqual(health["reason"], "unreachable")
        self.assertTrue(interrupted.closed)

    @mock.patch(REQUEST_PATCH)
    def test_health_validates_configured_identity_without_returning_jids(self, request):
        def health_for(jid_marker):
            data = {"connected": True, "loggedIn": True}
            if jid_marker is not None:
                data["jid"] = jid_marker
            request.return_value = FakeResponse(
                200,
                {"code": 200, "success": True, "data": data},
            )
            return self.adapter.get_health(self.connection)

        configured = "5511888888888:42@s.whatsapp.net"
        observed_match = "5511888888888:7@s.whatsapp.net"
        self.account.own_external_identity = configured
        health = health_for(observed_match)
        self.assertEqual(health["state"], "connected")
        self.assertEqual(health["reason"], "ready")
        self.assertIs(health["identity_matches"], True)
        self.assertNotIn("jid", health)
        self.assertNotIn(configured, repr(health))
        self.assertNotIn(observed_match, repr(health))

        for configured_variant in ("+5511888888888", "5511888888888@c.us"):
            with self.subTest(configured_variant=configured_variant):
                self.account.own_external_identity = configured_variant
                health = health_for(observed_match)
                self.assertEqual(health["state"], "connected")
                self.assertIs(health["identity_matches"], True)
                self.assertNotIn(configured_variant, repr(health))

        observed_mismatch = "5511777777777@s.whatsapp.net"
        self.account.own_external_identity = configured
        health = health_for(observed_mismatch)
        self.assertEqual(health["state"], "degraded")
        self.assertEqual(health["reason"], "identity_mismatch")
        self.assertIs(health["identity_matches"], False)
        self.assertNotIn(observed_mismatch, repr(health))

        health = health_for(None)
        self.assertEqual(health["state"], "degraded")
        self.assertEqual(health["reason"], "identity_unverified")
        self.assertIs(health["identity_matches"], False)
        self.assertNotIn("jid", health)

        self.account.own_external_identity = False
        for observed in ("5511666666666@s.whatsapp.net", None):
            with self.subTest(unconfigured_observed=observed):
                health = health_for(observed)
                self.assertEqual(health["state"], "degraded")
                self.assertEqual(health["reason"], "identity_unverified")
                self.assertIs(health["identity_matches"], False)
                self.assertNotIn("jid", health)
                if observed:
                    self.assertNotIn(observed, repr(health))

    @mock.patch(REQUEST_PATCH)
    def test_health_reloads_every_revision_fenced_input_before_io(self, request):
        self.account.own_external_identity = "5511888888888@s.whatsapp.net"
        self.assertEqual(self.connection.wuzapi_base_url, "https://wuzapi.invalid")
        self.assertEqual(self.connection.wuzapi_api_token, "not-a-real-api-token")
        self.assertEqual(
            self.account.own_external_identity,
            "5511888888888@s.whatsapp.net",
        )
        # Direct SQL below simulates values committed by another transaction.
        # Flush the deliberately prefetched ORM baseline first so the adapter's
        # default flush-before-invalidate cannot overwrite the simulated change.
        self.connection.flush_recordset(["wuzapi_base_url", "wuzapi_api_token"])
        self.account.flush_recordset(["own_external_identity"])
        self.env.cr.execute(
            "UPDATE contact_center_provider_connection "
            "SET wuzapi_base_url = %s, wuzapi_api_token = %s WHERE id = %s",
            [
                "https://rotated.wuzapi.invalid",
                "rotated-api-token",
                self.connection.id,
            ],
        )
        self.env.cr.execute(
            "UPDATE contact_center_account SET own_external_identity = %s "
            "WHERE id = %s",
            ["5511777777777@s.whatsapp.net", self.account.id],
        )
        request.return_value = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {
                    "connected": True,
                    "loggedIn": True,
                    "jid": "5511777777777:4@s.whatsapp.net",
                },
            },
        )

        health = self.adapter.get_health(self.connection)

        self.assertEqual(health["state"], "connected")
        self.assertIs(health["identity_matches"], True)
        args, kwargs = request.call_args
        self.assertEqual(args, ("GET", "https://rotated.wuzapi.invalid/session/status"))
        self.assertEqual(kwargs["headers"]["Token"], "rotated-api-token")
