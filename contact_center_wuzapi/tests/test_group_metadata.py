import copy
import hashlib
import json
from unittest import mock

import requests

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderPausedError,
    ProviderRateLimitError,
    TransientAdapterError,
    UnsupportedEventError,
)
from odoo.addons.contact_center_base.services.dto import (
    AddressDTO,
    AvatarResult,
    IdentityProfileResult,
)

from ..services.adapter import WuzapiAdapter
from .common import WuzapiCase

REQUEST_PATCH = "odoo.addons.contact_center_wuzapi.services.group.requests.request"
DNS_PATCH = "odoo.addons.contact_center_wuzapi.services.group.socket.getaddrinfo"
GROUP_REF = "120363000000901@g.us"
LEGACY_GROUP_REF = "15550101001-1700000000@g.us"


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


class FakeBinaryResponse(FakeResponse):
    def __init__(self, status_code, content=b"", headers=None, interruption=None):
        super().__init__(status_code, payload=None, headers=headers)
        self.content = content
        self.interruption = interruption

    def iter_content(self, chunk_size=65536):
        if self.interruption:
            raise self.interruption
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]


class TestWuzapiGroupMetadata(WuzapiCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.adapter = WuzapiAdapter(cls.env)

    def test_group_lifecycle_webhooks_become_provider_neutral_pull_hints(self):
        group_info = self.adapter.normalize_event(
            self.connection, self.load_fixture("group_info_webhook.json")
        )
        self.assertEqual(group_info.event_type, "group.metadata.changed")
        self.assertEqual(group_info.conversation_ref, GROUP_REF)
        self.assertEqual(group_info.conversation.conversation_type, "group")
        self.assertEqual(
            group_info.extensions["group_metadata_hint"],
            {
                "kind": "metadata",
                "display_name": "Grupo de validação",
                "provider_revision": "1787572800002",
            },
        )
        self.assertNotIn("Participants", repr(group_info.extensions))
        self.assertFalse(group_info.actor.addresses)

        joined = self.adapter.normalize_event(
            self.connection, self.load_fixture("joined_group_webhook.json")
        )
        replayed_joined = self.adapter.normalize_event(
            self.connection, self.load_fixture("joined_group_webhook.json")
        )
        self.assertEqual(joined.conversation_ref, GROUP_REF)
        self.assertEqual(joined.event_id, replayed_joined.event_id)
        self.assertEqual(
            joined.extensions["provider.wuzapi"]["occurred_at_source"],
            "odoo_received",
        )
        self.assertEqual(
            joined.extensions["group_metadata_hint"],
            {
                "kind": "joined",
                "display_name": "Grupo de validação",
                "provider_revision": "1787572800002",
                "avatar_changed": True,
            },
        )
        self.assertNotIn("Participants", repr(joined.extensions))

    def test_picture_hint_routes_group_and_direct_profiles(self):
        picture = self.adapter.normalize_event(
            self.connection, self.load_fixture("picture_group_webhook.json")
        )
        self.assertEqual(picture.event_type, "group.metadata.changed")
        self.assertEqual(picture.conversation_ref, GROUP_REF)
        self.assertEqual(
            picture.extensions["group_metadata_hint"],
            {
                "kind": "picture",
                "provider_revision": "1787573400001",
                "avatar_changed": True,
            },
        )

        direct_payload = self.load_fixture("picture_direct_webhook.json")
        direct = self.adapter.normalize_event(self.connection, direct_payload)
        replayed = self.adapter.normalize_event(self.connection, direct_payload)
        expected_address = {
            "namespace": "whatsapp.pn",
            "value": "15550101002@s.whatsapp.net",
            "value_normalized": "15550101002@s.whatsapp.net",
            "role": "primary",
            "source_field": "event.Picture.JID",
            "confidence": "protocol",
            "resolution_scope": "account",
        }

        self.assertEqual(direct.event_id, replayed.event_id)
        self.assertEqual(direct.event_type, "identity.avatar.changed")
        self.assertEqual(direct.conversation_ref, "15550101002@s.whatsapp.net")
        self.assertEqual(direct.conversation.conversation_type, "direct")
        self.assertEqual(
            [address.to_dict() for address in direct.actor.addresses],
            [expected_address],
        )
        self.assertEqual(
            [address.to_dict() for address in direct.conversation.addresses],
            [expected_address],
        )
        self.assertIsNone(direct.message)
        self.assertEqual(
            direct.extensions["identity_avatar_hint"],
            {
                "kind": "picture",
                "provider_revision": "1787573400001",
                "avatar_changed": True,
            },
        )
        self.assertNotIn("Author", repr(direct.extensions))

    def test_direct_picture_supports_lid_removal_and_rejects_invalid_subject(self):
        removed_payload = self.load_fixture("picture_direct_webhook.json")
        removed_payload["event"].update(
            {
                "JID": "700000000000002@lid",
                "Author": "15550101001@s.whatsapp.net",
                "Remove": True,
                "PictureID": "",
            }
        )
        removed = self.adapter.normalize_event(self.connection, removed_payload)

        self.assertEqual(removed.actor.addresses[0].namespace, "whatsapp.lid")
        self.assertEqual(
            removed.actor.addresses[0].value_normalized,
            "700000000000002@lid",
        )
        self.assertEqual(
            removed.extensions["identity_avatar_hint"],
            {
                "kind": "picture",
                "avatar_changed": True,
                "avatar_removed": True,
            },
        )
        # Author is who made the change, not another alias of the profile JID.
        self.assertEqual(len(removed.actor.addresses), 1)

        invalid = self.load_fixture("picture_direct_webhook.json")
        invalid["event"]["JID"] = "status@broadcast"
        with self.assertRaisesRegex(AdapterError, "identity avatar JID"):
            self.adapter.normalize_event(self.connection, invalid)

    def test_group_hint_omits_unsafe_name_and_rejects_malformed_source(self):
        unsafe = self.load_fixture("group_info_webhook.json")
        unsafe["event"]["Name"]["Name"] = "nome\nforjado"
        event = self.adapter.normalize_event(self.connection, unsafe)
        self.assertNotIn("display_name", event.extensions["group_metadata_hint"])

        invalid_jid = self.load_fixture("group_info_webhook.json")
        invalid_jid["event"]["JID"] = "not-a-group@s.whatsapp.net"
        with self.assertRaisesRegex(AdapterError, "group JID"):
            self.adapter.normalize_event(self.connection, invalid_jid)

        invalid_time = self.load_fixture("picture_group_webhook.json")
        invalid_time["event"]["Timestamp"] = "not-a-timestamp"
        with self.assertRaisesRegex(AdapterError, "timestamp"):
            self.adapter.normalize_event(self.connection, invalid_time)

    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_metadata_maps_pn_lid_roles_and_own_role(self, request):
        self.account.own_external_identity = "15550101001@s.whatsapp.net"
        response = FakeResponse(200, self.load_fixture("group_info_response.json"))
        request.return_value = response

        metadata = self.adapter.fetch_group_metadata(self.connection, GROUP_REF)

        self.assertTrue(response.closed)
        self.assertEqual(metadata.conversation_ref, GROUP_REF)
        self.assertEqual(metadata.display_name, "Grupo de validação")
        self.assertEqual(metadata.participant_count, 3)
        self.assertTrue(metadata.is_complete)
        self.assertEqual(metadata.own_role, "superadmin")
        self.assertEqual(
            metadata.own_protocol_participant.to_dict(),
            {
                "namespace": "whatsapp.lid",
                "value": "700000000000001@lid",
                "value_normalized": "700000000000001@lid",
                "role": "sender",
                "source_field": "data.Participants.JID",
                "confidence": "protocol",
                "resolution_scope": "account",
            },
        )
        self.assertEqual(metadata.provider_revision, "1787572800002")
        self.assertEqual(
            [participant.participant_ref for participant in metadata.participants],
            [
                "700000000000001@lid",
                "700000000000002@lid",
                "700000000000003@lid",
            ],
        )
        self.assertEqual(
            [participant.role for participant in metadata.participants],
            ["superadmin", "admin", "member"],
        )
        self.assertEqual(
            {
                (address.namespace, address.value_normalized)
                for address in metadata.participants[1].addresses
            },
            {
                ("whatsapp.pn", "15550101002@s.whatsapp.net"),
                ("whatsapp.lid", "700000000000002@lid"),
            },
        )
        args, kwargs = request.call_args
        self.assertEqual(args, ("GET", "https://wuzapi.invalid/group/info"))
        self.assertEqual(kwargs["params"], {"groupJID": GROUP_REF})
        self.assertNotIn("json", kwargs)
        self.assertNotIn("data", kwargs)
        self.assertEqual(kwargs["timeout"], (5, 30))
        self.assertIs(kwargs["stream"], True)
        self.assertEqual(kwargs["headers"]["Token"], "not-a-real-api-token")

    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_metadata_uses_own_primary_pn_in_pn_mode(self, request):
        # Matching may start from the LID alias, while AddressingMode still
        # determines that the canonical send participant is the primary PN JID.
        self.account.own_external_identity = "700000000000001@lid"
        payload = self.load_fixture("group_info_response.json")
        payload["data"]["AddressingMode"] = "pn"
        payload["data"]["Participants"][0]["JID"] = "15550101001@s.whatsapp.net"
        request.return_value = FakeResponse(200, payload)

        metadata = self.adapter.fetch_group_metadata(self.connection, GROUP_REF)

        self.assertEqual(metadata.own_protocol_participant.namespace, "whatsapp.pn")
        self.assertEqual(
            metadata.own_protocol_participant.value_normalized,
            "15550101001@s.whatsapp.net",
        )

    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_metadata_fails_closed_for_invalid_own_addressing(
        self, request
    ):
        self.account.own_external_identity = "15550101001@s.whatsapp.net"
        invalid_mode = self.load_fixture("group_info_response.json")
        invalid_mode["data"]["AddressingMode"] = "unexpected"
        request.return_value = FakeResponse(200, invalid_mode)
        with self.assertRaisesRegex(AdapterError, "AddressingMode"):
            self.adapter.fetch_group_metadata(self.connection, GROUP_REF)

        contradictory = self.load_fixture("group_info_response.json")
        contradictory["data"]["AddressingMode"] = "pn"
        request.return_value = FakeResponse(200, contradictory)
        with self.assertRaisesRegex(AdapterError, "contradicts"):
            self.adapter.fetch_group_metadata(self.connection, GROUP_REF)

        own_missing = self.load_fixture("group_info_response.json")
        self.account.own_external_identity = "155509999999@s.whatsapp.net"
        request.return_value = FakeResponse(200, own_missing)
        metadata = self.adapter.fetch_group_metadata(self.connection, GROUP_REF)
        self.assertEqual(metadata.own_role, "unknown")
        self.assertIsNone(metadata.own_protocol_participant)

    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_metadata_accepts_legacy_numeric_group_jid(self, request):
        payload = self.load_fixture("group_info_response.json")
        payload["data"]["JID"] = LEGACY_GROUP_REF
        request.return_value = FakeResponse(200, payload)

        metadata = self.adapter.fetch_group_metadata(self.connection, LEGACY_GROUP_REF)

        self.assertEqual(metadata.conversation_ref, LEGACY_GROUP_REF)
        _args, kwargs = request.call_args
        self.assertEqual(kwargs["params"], {"groupJID": LEGACY_GROUP_REF})

    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_metadata_is_stable_when_roster_order_changes(self, request):
        original = self.load_fixture("group_info_response.json")
        reordered = copy.deepcopy(original)
        reordered["data"]["Participants"].reverse()
        request.side_effect = [
            FakeResponse(200, original),
            FakeResponse(200, reordered),
        ]

        first = self.adapter.fetch_group_metadata(self.connection, GROUP_REF)
        second = self.adapter.fetch_group_metadata(self.connection, GROUP_REF)

        self.assertEqual(first.participants, second.participants)
        self.assertEqual(first.provider_revision, second.provider_revision)

    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_metadata_marks_truncated_roster_incomplete(self, request):
        payload = self.load_fixture("group_info_response.json")
        payload["data"]["ParticipantCount"] = 5
        missing_count = copy.deepcopy(payload)
        missing_count["data"].pop("ParticipantCount")
        request.side_effect = [
            FakeResponse(200, payload),
            FakeResponse(200, missing_count),
        ]

        metadata = self.adapter.fetch_group_metadata(self.connection, GROUP_REF)

        self.assertEqual(metadata.participant_count, 5)
        self.assertEqual(len(metadata.participants), 3)
        self.assertFalse(metadata.is_complete)
        without_total = self.adapter.fetch_group_metadata(self.connection, GROUP_REF)
        self.assertEqual(without_total.participant_count, 3)
        self.assertFalse(without_total.is_complete)

    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_metadata_rejects_mismatch_and_malformed_roster(self, request):
        mismatch = self.load_fixture("group_info_response.json")
        mismatch["data"]["JID"] = "120363000000902@g.us"
        request.return_value = FakeResponse(200, mismatch)
        with self.assertRaisesRegex(AdapterError, "does not match"):
            self.adapter.fetch_group_metadata(self.connection, GROUP_REF)

        duplicate = self.load_fixture("group_info_response.json")
        duplicate["data"]["Participants"][2][
            "PhoneNumber"
        ] = "15550101002@s.whatsapp.net"
        request.return_value = FakeResponse(200, duplicate)
        with self.assertRaisesRegex(AdapterError, "roster is invalid"):
            self.adapter.fetch_group_metadata(self.connection, GROUP_REF)

        malformed = self.load_fixture("group_info_response.json")
        malformed["data"]["Participants"] = {"unexpected": True}
        request.return_value = FakeResponse(200, malformed)
        with self.assertRaisesRegex(AdapterError, "participants are invalid"):
            self.adapter.fetch_group_metadata(self.connection, GROUP_REF)

    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_metadata_classifies_transport_and_http_failures(self, request):
        request.side_effect = requests.Timeout("read timeout")
        with self.assertRaises(TransientAdapterError):
            self.adapter.fetch_group_metadata(self.connection, GROUP_REF)

        for status, expected_error in (
            (401, ProviderPausedError),
            (429, ProviderRateLimitError),
            (503, TransientAdapterError),
            (400, AdapterError),
        ):
            with self.subTest(status=status):
                request.side_effect = None
                request.return_value = FakeResponse(
                    status,
                    {"code": status, "success": False, "error": "sanitized"},
                    headers={"Retry-After": "17"},
                )
                with self.assertRaises(expected_error) as raised:
                    self.adapter.fetch_group_metadata(self.connection, GROUP_REF)
                if status == 429:
                    self.assertEqual(raised.exception.retry_after_seconds, 17)

        request.return_value = FakeResponse(
            500,
            {
                "code": 500,
                "success": False,
                "error": "Failed to get group info: you're not participating in that group",
            },
        )
        with self.assertRaisesRegex(AdapterError, "no longer available") as raised:
            self.adapter.fetch_group_metadata(self.connection, GROUP_REF)
        self.assertIsInstance(raised.exception, UnsupportedEventError)
        self.assertNotIsInstance(raised.exception, TransientAdapterError)

        request.return_value = FakeResponse(
            200,
            {
                "code": 500,
                "success": False,
                "error": "Failed to get group info: that group does not exist",
            },
        )
        with self.assertRaises(UnsupportedEventError):
            self.adapter.fetch_group_metadata(self.connection, GROUP_REF)

        request.reset_mock()
        with self.assertRaisesRegex(AdapterError, "group JID"):
            self.adapter.fetch_group_metadata(
                self.connection, "15550101001@s.whatsapp.net"
            )
        request.assert_not_called()

    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_metadata_closes_an_oversized_response(self, request):
        response = FakeResponse(
            200,
            self.load_fixture("group_info_response.json"),
            headers={"Content-Length": str(8 * 1024 * 1024 + 1)},
        )
        request.return_value = response

        with self.assertRaisesRegex(AdapterError, "size limit"):
            self.adapter.fetch_group_metadata(self.connection, GROUP_REF)

        self.assertTrue(response.closed)

    @mock.patch(DNS_PATCH)
    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_avatar_returns_bounded_sniffed_content(self, request, dns):
        content = b"\xff\xd8\xff\xe0" + b"sanitized-jpeg-fixture"
        lookup = FakeResponse(200, self.load_fixture("group_avatar_response.json"))
        image = FakeBinaryResponse(
            200,
            content,
            headers={
                "Content-Type": "image/jpeg",
                "Content-Length": str(len(content)),
            },
        )
        request.side_effect = [lookup, image]
        dns.return_value = [
            (2, 1, 6, "", ("8.8.8.8", 443)),
        ]

        avatar = self.adapter.fetch_group_avatar(self.connection, GROUP_REF)

        self.assertEqual(avatar.state, "ready")
        self.assertEqual(avatar.provider_revision, "1787573400001")
        self.assertEqual(avatar.content, content)
        self.assertEqual(avatar.mime_type, "image/jpeg")
        self.assertEqual(avatar.file_name, "group-avatar.jpg")
        self.assertEqual(avatar.size_bytes, len(content))
        self.assertEqual(avatar.sha256, hashlib.sha256(content).hexdigest())
        self.assertTrue(lookup.closed)
        self.assertTrue(image.closed)

        post_args, post_kwargs = request.call_args_list[0]
        self.assertEqual(post_args, ("POST", "https://wuzapi.invalid/user/avatar"))
        self.assertEqual(post_kwargs["json"], {"Phone": GROUP_REF, "Preview": True})
        self.assertEqual(post_kwargs["headers"]["Token"], "not-a-real-api-token")
        self.assertIs(post_kwargs["stream"], True)
        get_args, get_kwargs = request.call_args_list[1]
        self.assertEqual(get_args[0], "GET")
        self.assertEqual(
            get_args[1],
            "https://pps.whatsapp.net/v/group-avatar-test.jpg?ccb=11-4",
        )
        self.assertNotIn("Token", get_kwargs["headers"])
        self.assertIs(get_kwargs["allow_redirects"], False)
        self.assertIs(get_kwargs["stream"], True)

    @mock.patch(DNS_PATCH)
    @mock.patch(REQUEST_PATCH)
    def test_fetch_identity_avatar_reuses_the_bounded_private_download(
        self, request, dns
    ):
        content = b"\xff\xd8\xff\xe0" + b"direct-profile-jpeg-fixture"
        lookup = FakeResponse(200, self.load_fixture("group_avatar_response.json"))
        image = FakeBinaryResponse(
            200,
            content,
            headers={
                "Content-Type": "image/jpeg",
                "Content-Length": str(len(content)),
            },
        )
        request.side_effect = [lookup, image]
        dns.return_value = [(2, 1, 6, "", ("8.8.8.8", 443))]
        address = AddressDTO(
            namespace="whatsapp.pn",
            value="15550101002@s.whatsapp.net",
            value_normalized="15550101002@s.whatsapp.net",
            role="primary",
            source_field="identity.alias",
            confidence="protocol",
        )

        avatar = self.adapter.fetch_identity_avatar(self.connection, address)

        self.assertIsInstance(avatar, AvatarResult)
        self.assertEqual(avatar.state, "ready")
        self.assertEqual(avatar.provider_revision, "1787573400001")
        self.assertEqual(avatar.content, content)
        self.assertEqual(avatar.mime_type, "image/jpeg")
        self.assertEqual(avatar.file_name, "identity-avatar.jpg")
        self.assertEqual(avatar.size_bytes, len(content))
        self.assertEqual(avatar.sha256, hashlib.sha256(content).hexdigest())
        self.assertTrue(lookup.closed)
        self.assertTrue(image.closed)

        post_args, post_kwargs = request.call_args_list[0]
        self.assertEqual(post_args, ("POST", "https://wuzapi.invalid/user/avatar"))
        self.assertEqual(
            post_kwargs["json"],
            {"Phone": "15550101002@s.whatsapp.net", "Preview": True},
        )
        self.assertEqual(post_kwargs["headers"]["Token"], "not-a-real-api-token")
        get_args, get_kwargs = request.call_args_list[1]
        self.assertEqual(get_args[0], "GET")
        self.assertNotIn("Token", get_kwargs["headers"])
        self.assertIs(get_kwargs["allow_redirects"], False)
        self.assertIs(get_kwargs["stream"], True)

    @mock.patch(REQUEST_PATCH)
    def test_fetch_identity_avatar_accepts_direct_namespaces_and_rejects_forgery(
        self, request
    ):
        addresses = (
            AddressDTO(
                namespace="whatsapp.pn",
                value="15550101002@s.whatsapp.net",
                value_normalized="15550101002@s.whatsapp.net",
            ),
            AddressDTO(
                namespace="whatsapp.lid",
                value="700000000000002@lid",
                value_normalized="700000000000002@lid",
            ),
            AddressDTO(
                namespace="whatsapp.jid",
                value="15550101002@c.us",
                value_normalized="15550101002@c.us",
            ),
            AddressDTO(
                namespace="phone",
                value="+55 (19) 5010-1002",
                value_normalized="+551950101002",
            ),
        )
        for address in addresses:
            response = FakeResponse(
                500,
                {"code": 500, "success": False, "error": "no avatar found"},
            )
            request.return_value = response
            avatar = self.adapter.fetch_identity_avatar(self.connection, address)
            self.assertEqual(avatar.state, "absent")
            self.assertEqual(
                request.call_args.kwargs["json"]["Phone"],
                address.value_normalized,
            )
            self.assertTrue(response.closed)

        invalid_addresses = (
            "15550101002@s.whatsapp.net",
            AddressDTO(
                namespace="whatsapp.group",
                value=GROUP_REF,
                value_normalized=GROUP_REF,
                role="group",
            ),
            AddressDTO(
                namespace="whatsapp.pn",
                value="15550101002@s.whatsapp.net",
                value_normalized="15550101003@s.whatsapp.net",
            ),
            AddressDTO(
                namespace="whatsapp.lid",
                value="15550101002@s.whatsapp.net",
                value_normalized="15550101002@s.whatsapp.net",
            ),
            AddressDTO(
                namespace="phone",
                value="+55 (19) 5010-1002",
                value_normalized="+551950101003",
            ),
            AddressDTO(
                namespace="whatsapp.jid",
                value="status@broadcast",
                value_normalized="status@broadcast",
            ),
        )
        request.reset_mock()
        for address in invalid_addresses:
            with self.assertRaisesRegex(AdapterError, "identity avatar"):
                self.adapter.fetch_identity_avatar(self.connection, address)
        self.assertFalse(request.called)

    @mock.patch(REQUEST_PATCH)
    def test_fetch_identity_profile_combines_targeted_verified_name_and_avatar(
        self, request
    ):
        check = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {
                    "Users": [
                        {
                            "Query": "15550101002",
                            "IsInWhatsapp": True,
                            "JID": "700000000000002@lid",
                            "VerifiedName": "Empresa verificada",
                        }
                    ]
                },
            },
        )
        avatar = FakeResponse(
            500,
            {"code": 500, "success": False, "error": "no avatar found"},
        )
        request.side_effect = [check, avatar]
        address = AddressDTO(
            namespace="whatsapp.pn",
            value="15550101002@s.whatsapp.net",
            value_normalized="15550101002@s.whatsapp.net",
        )

        profile = self.adapter.fetch_identity_profile(self.connection, address)

        self.assertIsInstance(profile, IdentityProfileResult)
        self.assertEqual(profile.display_name, "Empresa verificada")
        self.assertEqual(profile.avatar.state, "absent")
        self.assertTrue(check.closed)
        self.assertTrue(avatar.closed)
        check_args, check_kwargs = request.call_args_list[0]
        self.assertEqual(check_args, ("POST", "https://wuzapi.invalid/user/check"))
        self.assertEqual(check_kwargs["json"], {"Phone": ["15550101002"]})
        self.assertEqual(check_kwargs["headers"]["Token"], "not-a-real-api-token")
        self.assertIs(check_kwargs["stream"], True)
        avatar_args, avatar_kwargs = request.call_args_list[1]
        self.assertEqual(avatar_args, ("POST", "https://wuzapi.invalid/user/avatar"))
        self.assertEqual(
            avatar_kwargs["json"],
            {"Phone": "15550101002@s.whatsapp.net", "Preview": True},
        )

    @mock.patch(REQUEST_PATCH)
    def test_fetch_identity_profile_does_not_send_opaque_lid_to_phone_check(
        self, request
    ):
        request.return_value = FakeResponse(
            500,
            {"code": 500, "success": False, "error": "no avatar found"},
        )
        address = AddressDTO(
            namespace="whatsapp.lid",
            value="700000000000002@lid",
            value_normalized="700000000000002@lid",
        )

        profile = self.adapter.fetch_identity_profile(self.connection, address)

        self.assertEqual(profile.display_name, "")
        self.assertEqual(profile.avatar.state, "absent")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(
            request.call_args.args,
            ("POST", "https://wuzapi.invalid/user/avatar"),
        )

    @mock.patch(REQUEST_PATCH)
    def test_fetch_identity_profile_rejects_mismatch_and_ignores_unsafe_name(
        self, request
    ):
        address = AddressDTO(
            namespace="whatsapp.pn",
            value="15550101002@s.whatsapp.net",
            value_normalized="15550101002@s.whatsapp.net",
        )
        mismatch = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {
                    "Users": [
                        {
                            "Query": "15550101003",
                            "IsInWhatsapp": True,
                            "JID": "15550101003@s.whatsapp.net",
                            "VerifiedName": "Outro",
                        }
                    ]
                },
            },
        )
        request.return_value = mismatch
        with self.assertRaisesRegex(AdapterError, "query does not match"):
            self.adapter.fetch_identity_profile(self.connection, address)
        self.assertTrue(mismatch.closed)

        mismatched_jid = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {
                    "Users": [
                        {
                            "Query": "15550101002",
                            "IsInWhatsapp": True,
                            "JID": "15550101003@s.whatsapp.net",
                            "VerifiedName": "Outro",
                        }
                    ]
                },
            },
        )
        request.reset_mock()
        request.side_effect = None
        request.return_value = mismatched_jid
        with self.assertRaisesRegex(AdapterError, "JID does not match"):
            self.adapter.fetch_identity_profile(self.connection, address)
        self.assertTrue(mismatched_jid.closed)

        unsafe = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {
                    "Users": [
                        {
                            "Query": "15550101002",
                            "IsInWhatsapp": True,
                            "JID": "15550101002@s.whatsapp.net",
                            "VerifiedName": "Nome\nforjado",
                        }
                    ]
                },
            },
        )
        avatar = FakeResponse(
            500,
            {"code": 500, "success": False, "error": "no avatar found"},
        )
        request.reset_mock()
        request.side_effect = [unsafe, avatar]
        profile = self.adapter.fetch_identity_profile(self.connection, address)
        self.assertEqual(profile.display_name, "")
        self.assertEqual(profile.avatar.state, "absent")
        self.assertTrue(unsafe.closed)
        self.assertTrue(avatar.closed)

        oversized = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {
                    "Users": [
                        {
                            "Query": "15550101002",
                            "IsInWhatsapp": True,
                            "JID": "15550101002@s.whatsapp.net",
                            "VerifiedName": "N" * 256,
                        }
                    ]
                },
            },
        )
        avatar = FakeResponse(
            500,
            {"code": 500, "success": False, "error": "no avatar found"},
        )
        request.reset_mock()
        request.side_effect = [oversized, avatar]
        profile = self.adapter.fetch_identity_profile(self.connection, address)
        self.assertEqual(profile.display_name, "")
        self.assertEqual(profile.avatar.state, "absent")
        self.assertTrue(oversized.closed)
        self.assertTrue(avatar.closed)

        maximum = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {
                    "Users": [
                        {
                            "Query": "15550101002",
                            "IsInWhatsapp": True,
                            "JID": "15550101002@s.whatsapp.net",
                            "VerifiedName": "N" * 255,
                        }
                    ]
                },
            },
        )
        avatar = FakeResponse(
            500,
            {"code": 500, "success": False, "error": "no avatar found"},
        )
        request.reset_mock()
        request.side_effect = [maximum, avatar]
        profile = self.adapter.fetch_identity_profile(self.connection, address)
        self.assertEqual(profile.display_name, "N" * 255)
        self.assertEqual(profile.avatar.state, "absent")
        self.assertTrue(maximum.closed)
        self.assertTrue(avatar.closed)

    @mock.patch(REQUEST_PATCH)
    def test_fetch_identity_profile_preserves_retry_after_from_check(self, request):
        response = FakeResponse(
            429,
            {"code": 429, "success": False, "error": "rate limited"},
            headers={"Retry-After": "37"},
        )
        request.return_value = response
        address = AddressDTO(
            namespace="whatsapp.pn",
            value="15550101002@s.whatsapp.net",
            value_normalized="15550101002@s.whatsapp.net",
        )

        with self.assertRaises(ProviderRateLimitError) as raised:
            self.adapter.fetch_identity_profile(self.connection, address)

        self.assertEqual(raised.exception.retry_after_seconds, 37)
        self.assertTrue(response.closed)

    @mock.patch(DNS_PATCH)
    @mock.patch(REQUEST_PATCH)
    def test_fetch_identity_avatar_keeps_group_ssrf_and_mime_guards(self, request, dns):
        address = AddressDTO(
            namespace="whatsapp.pn",
            value="15550101002@s.whatsapp.net",
            value_normalized="15550101002@s.whatsapp.net",
        )
        lookup_payload = self.load_fixture("group_avatar_response.json")
        request.return_value = FakeResponse(200, lookup_payload)
        dns.return_value = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with self.assertRaisesRegex(AdapterError, "not public"):
            self.adapter.fetch_identity_avatar(self.connection, address)
        self.assertEqual(request.call_count, 1)

        request.reset_mock()
        request.side_effect = [
            FakeResponse(200, lookup_payload),
            FakeBinaryResponse(
                200,
                b"\x89PNG\r\n\x1a\n" + b"direct-profile-png-fixture",
                headers={"Content-Type": "image/jpeg"},
            ),
        ]
        dns.return_value = [(2, 1, 6, "", ("8.8.8.8", 443))]
        with self.assertRaisesRegex(AdapterError, "MIME type does not match"):
            self.adapter.fetch_identity_avatar(self.connection, address)

    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_avatar_distinguishes_absent_and_unavailable(self, request):
        request.return_value = FakeResponse(
            500,
            {"code": 500, "success": False, "error": "no avatar found"},
        )
        absent = self.adapter.fetch_group_avatar(self.connection, GROUP_REF)
        self.assertEqual(absent.state, "absent")

        request.return_value = FakeResponse(
            200,
            {
                "code": 200,
                "success": True,
                "data": {"url": "", "id": "known-but-not-readable"},
            },
        )
        unavailable = self.adapter.fetch_group_avatar(self.connection, GROUP_REF)
        self.assertEqual(unavailable.state, "unavailable")
        self.assertEqual(unavailable.provider_revision, "known-but-not-readable")

        request.return_value = FakeResponse(
            500,
            {
                "code": 500,
                "success": False,
                "error": "failed to get avatar: user has hidden their profile picture",
            },
        )
        hidden = self.adapter.fetch_group_avatar(self.connection, GROUP_REF)
        self.assertEqual(hidden.state, "unavailable")

        request.return_value = FakeResponse(
            200,
            {
                "code": 500,
                "success": False,
                "error": "failed to get avatar: no avatar found",
            },
        )
        embedded_absent = self.adapter.fetch_group_avatar(self.connection, GROUP_REF)
        self.assertEqual(embedded_absent.state, "absent")

    @mock.patch(DNS_PATCH)
    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_avatar_blocks_non_public_resolution(self, request, dns):
        request.return_value = FakeResponse(
            200, self.load_fixture("group_avatar_response.json")
        )
        dns.return_value = [(2, 1, 6, "", ("127.0.0.1", 443))]

        with self.assertRaisesRegex(AdapterError, "not public"):
            self.adapter.fetch_group_avatar(self.connection, GROUP_REF)

        self.assertEqual(request.call_count, 1)

    @mock.patch(DNS_PATCH)
    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_avatar_classifies_download_failures(self, request, dns):
        lookup_payload = self.load_fixture("group_avatar_response.json")
        dns.return_value = [(2, 1, 6, "", ("8.8.8.8", 443))]

        request.side_effect = [
            FakeResponse(200, lookup_payload),
            requests.Timeout("avatar timeout"),
        ]
        with self.assertRaises(TransientAdapterError):
            self.adapter.fetch_group_avatar(self.connection, GROUP_REF)

        request.side_effect = [
            FakeResponse(200, lookup_payload),
            FakeBinaryResponse(503),
        ]
        with self.assertRaises(TransientAdapterError):
            self.adapter.fetch_group_avatar(self.connection, GROUP_REF)

        request.side_effect = [
            FakeResponse(200, lookup_payload),
            FakeBinaryResponse(404),
        ]
        unavailable = self.adapter.fetch_group_avatar(self.connection, GROUP_REF)
        self.assertEqual(unavailable.state, "unavailable")

        interrupted = FakeBinaryResponse(
            200,
            headers={"Content-Type": "image/jpeg"},
            interruption=requests.ConnectionError("stream interrupted"),
        )
        request.side_effect = [
            FakeResponse(200, lookup_payload),
            interrupted,
        ]
        with self.assertRaises(TransientAdapterError):
            self.adapter.fetch_group_avatar(self.connection, GROUP_REF)
        self.assertTrue(interrupted.closed)

    @mock.patch(DNS_PATCH)
    @mock.patch(REQUEST_PATCH)
    def test_fetch_group_avatar_rejects_mime_spoof_and_oversize(self, request, dns):
        lookup_payload = self.load_fixture("group_avatar_response.json")
        dns.return_value = [(2, 1, 6, "", ("8.8.8.8", 443))]
        request.side_effect = [
            FakeResponse(200, lookup_payload),
            FakeBinaryResponse(
                200,
                b"\x89PNG\r\n\x1a\n" + b"sanitized-png-fixture",
                headers={"Content-Type": "image/jpeg"},
            ),
        ]
        with self.assertRaisesRegex(AdapterError, "MIME type does not match"):
            self.adapter.fetch_group_avatar(self.connection, GROUP_REF)

        oversized = FakeBinaryResponse(
            200,
            b"",
            headers={"Content-Length": str(2 * 1024 * 1024 + 1)},
        )
        request.side_effect = [
            FakeResponse(200, lookup_payload),
            oversized,
        ]
        with self.assertRaisesRegex(AdapterError, "size limit"):
            self.adapter.fetch_group_avatar(self.connection, GROUP_REF)
        self.assertTrue(oversized.closed)
