import copy
import datetime

from odoo.tests.common import TransactionCase

from ..services.adapter import (
    AdapterError,
    AdapterRegistry,
    ProviderAdapter,
    UnsupportedEventError,
    conversation_capabilities,
    validate_provider_request_snapshot,
)
from ..services.dto import (
    AdapterResult,
    AddressDTO,
    AttributionDTO,
    AvatarResult,
    CommandDTO,
    DTOValidationError,
    EventDTO,
    GroupMetadataDTO,
    MediaDTO,
    MessageDTO,
)


class DummyAdapter(ProviderAdapter):
    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return envelope

    def execute_command(self, connection, command):
        return AdapterResult.success()

    def prepare_request_snapshot(self, connection, command):
        return {
            "provider": "dummy",
            "method": "POST",
            "endpoint": "/messages",
            "payload": {"command_id": command.command_id},
        }

    def get_capabilities(self, connection):
        return {}

    def get_health(self, connection):
        return {"state": "ok"}


class TestDtoAdapter(TransactionCase):
    def test_identity_profile_contract_has_no_avatar_compatibility_fallback(self):
        adapter = DummyAdapter(self.env)
        address = AddressDTO(
            namespace="test.id",
            value="person-1",
            value_normalized="person-1",
        )

        self.assertFalse(adapter.supports_identity_profile(None))
        with self.assertRaises(UnsupportedEventError):
            adapter.fetch_identity_profile(None, address)

    def test_conversation_capabilities_require_an_explicit_group_scope(self):
        capabilities = {
            "send_message": True,
            "media": {"image": {"enabled": True}},
            "conversation_types": {
                "group": {
                    "send_message": True,
                    "media": {},
                }
            },
        }

        self.assertEqual(
            conversation_capabilities(capabilities, "direct"),
            {"send_message": True, "media": {"image": {"enabled": True}}},
        )
        self.assertEqual(
            conversation_capabilities(capabilities, "group"),
            {"send_message": True, "media": {}},
        )
        self.assertFalse(
            conversation_capabilities(
                {"send_message": True, "group": {"send_message": True}},
                "group",
            )
        )
        self.assertFalse(conversation_capabilities(capabilities, "other"))

    def test_event_roundtrip_preserves_actor_and_conversation_addresses(self):
        values = {
            "schema_version": 1,
            "provider_schema_version": "fixture-v1",
            "event_id": "evt-1",
            "event_type": "message.created",
            "occurred_at": "2026-08-21T10:00:00-03:00",
            "account_ref": "account-1",
            "connection_ref": "connection-1",
            "conversation_ref": "conversation-1",
            "platform": "whatsapp",
            "direction": "inbound",
            "is_from_me": False,
            "origin": "provider",
            "actor": {
                "addresses": [
                    {
                        "namespace": "whatsapp.lid",
                        "value": "123@lid",
                        "normalized": "123@lid",
                    }
                ]
            },
            "conversation": {
                "conversation_type": "direct",
                "addresses": [
                    {
                        "namespace": "whatsapp.pn",
                        "value": "5511999999999@s.whatsapp.net",
                        "value_normalized": "5511999999999@s.whatsapp.net",
                    }
                ],
            },
            "message": {
                "external_message_id": "msg-1",
                "content_type": "text",
                "text": "Hello",
                "is_forwarded": True,
                "forwarding_score": 1,
            },
            "reply_to": {"external_message_id": "msg-0"},
        }
        event = EventDTO.from_dict(values)
        self.assertEqual(event.occurred_at.tzinfo, datetime.timezone.utc)
        self.assertEqual(event.actor.addresses[0].namespace, "whatsapp.lid")
        self.assertEqual(event.conversation.addresses[0].namespace, "whatsapp.pn")
        self.assertEqual(event.reply_to["external_message_id"], "msg-0")
        self.assertTrue(event.message.is_forwarded)
        self.assertEqual(event.message.forwarding_score, 1)
        self.assertEqual(EventDTO.from_dict(event.to_dict()), event)

    def test_message_forwarding_evidence_is_typed_and_optional(self):
        default_message = MessageDTO()
        self.assertFalse(default_message.is_forwarded)
        self.assertIsNone(default_message.forwarding_score)
        self.assertEqual(
            MessageDTO.from_dict(default_message.to_dict()), default_message
        )

        forwarded = MessageDTO(is_forwarded=True, forwarding_score=127)
        self.assertEqual(MessageDTO.from_dict(forwarded.to_dict()), forwarded)
        # The score is independent evidence and must not manufacture the flag.
        score_only = MessageDTO(forwarding_score=1)
        self.assertFalse(score_only.is_forwarded)

        for values in (
            {"is_forwarded": 1},
            {"is_forwarded": "true"},
            {"forwarding_score": True},
            {"forwarding_score": -1},
            {"forwarding_score": 2_147_483_648},
        ):
            with self.subTest(values=values), self.assertRaises(DTOValidationError):
                MessageDTO(**values)

    def test_core_dto_bounds_identifiers_text_collections_and_json(self):
        with self.assertRaisesRegex(DTOValidationError, "address.value.*too long"):
            AddressDTO(
                namespace="provider.address",
                value="x" * 2049,
                value_normalized="x",
            )
        with self.assertRaisesRegex(DTOValidationError, "control characters"):
            AddressDTO(
                namespace="provider.address",
                value="valid",
                value_normalized="invalid\nidentifier",
            )
        with self.assertRaisesRegex(DTOValidationError, "message.text.*too long"):
            MessageDTO(text="x" * 131_073)
        with self.assertRaisesRegex(DTOValidationError, "protocol_snapshot.*too large"):
            MessageDTO(protocol_snapshot={"opaque": "x" * 65_536})

        valid = {
            "provider_schema_version": "v1",
            "event_id": "evt-1",
            "event_type": "message.created",
            "occurred_at": "2026-08-21T13:00:00Z",
            "account_ref": "account",
            "connection_ref": "connection",
            "conversation_ref": "conversation",
            "platform": "whatsapp",
            "direction": "inbound",
            "is_from_me": False,
            "origin": "provider",
            "actor": {},
            "conversation": {},
        }
        with self.assertRaisesRegex(DTOValidationError, "too many addresses"):
            EventDTO.from_dict(
                dict(
                    valid,
                    actor={
                        "addresses": [
                            {
                                "namespace": "provider.address",
                                "value": "value-%s" % index,
                                "value_normalized": "value-%s" % index,
                            }
                            for index in range(65)
                        ]
                    },
                )
            )
        with self.assertRaisesRegex(DTOValidationError, "extensions.*nested"):
            nested = {}
            cursor = nested
            for _index in range(10):
                cursor["nested"] = {}
                cursor = cursor["nested"]
            EventDTO.from_dict(dict(valid, extensions=nested))

    def test_core_dto_rejects_non_finite_json_numbers(self):
        attribution_values = {
            "touchpoint_type": "unknown",
            "evidence_level": "provider_hint",
            "network": "provider",
        }
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(container="message", value=value):
                with self.assertRaisesRegex(DTOValidationError, "non-finite"):
                    MessageDTO(protocol_snapshot={"value": value})
            with self.subTest(container="media", value=value):
                with self.assertRaisesRegex(DTOValidationError, "non-finite"):
                    MediaDTO(kind="image", remote_locator={"value": value})
            with self.subTest(container="attribution", value=value):
                with self.assertRaisesRegex(DTOValidationError, "non-finite"):
                    AttributionDTO(
                        **dict(
                            attribution_values,
                            provider_extensions={"provider.value": value},
                        )
                    )
            with self.subTest(container="adapter_result", value=value):
                with self.assertRaisesRegex(DTOValidationError, "non-finite"):
                    AdapterResult.success(provider_response={"value": value})

    def test_dto_rejects_naive_timestamp_and_unsupported_version(self):
        common = {
            "provider_schema_version": "v1",
            "event_id": "evt-1",
            "event_type": "connection.updated",
            "occurred_at": "2026-08-21T10:00:00",
            "account_ref": "account",
            "connection_ref": "connection",
            "conversation_ref": "connection",
            "platform": "whatsapp",
            "direction": "inbound",
            "is_from_me": False,
            "origin": "provider",
            "actor": {},
            "conversation": {},
        }
        with self.assertRaises(DTOValidationError):
            EventDTO.from_dict(common)
        common.update({"occurred_at": "2026-08-21T13:00:00Z", "schema_version": 2})
        with self.assertRaises(DTOValidationError):
            EventDTO.from_dict(common)

        valid = dict(common, schema_version=1)
        for field_name, invalid_value in (
            ("is_from_me", "false"),
            ("extensions", []),
            ("reply_to", []),
            ("actor", []),
            ("conversation", []),
        ):
            with self.subTest(field_name=field_name), self.assertRaises(
                DTOValidationError
            ):
                EventDTO.from_dict(dict(valid, **{field_name: invalid_value}))

        event = EventDTO.from_dict(valid)
        with self.assertRaises(DTOValidationError):
            EventDTO(**dict(event.__dict__, actor={}))

    def test_command_requires_stable_id(self):
        with self.assertRaises(DTOValidationError):
            CommandDTO.from_dict(
                {
                    "command_id": "",
                    "command_type": "send_message",
                    "account_ref": "account",
                    "connection_ref": "connection",
                    "conversation_ref": "conversation",
                    "conversation": {},
                }
            )
        with self.assertRaises(DTOValidationError):
            CommandDTO.from_dict(
                {
                    "command_id": "command-1",
                    "command_type": "send_message",
                    "account_ref": "account",
                    "connection_ref": "connection",
                    "conversation_ref": "conversation",
                    "conversation": {},
                    "options": [],
                }
            )

    def test_group_metadata_dto_requires_one_consistent_complete_roster(self):
        values = {
            "conversation_ref": "120363000000001@g.us",
            "observed_at": "2026-08-24T13:00:00Z",
            "display_name": "Operations",
            "participant_count": 1,
            "own_role": "admin",
            "own_protocol_participant": {
                "namespace": "whatsapp.lid",
                "value": "70000000000001@lid",
                "value_normalized": "70000000000001@lid",
                "role": "sender",
                "source_field": "data.Participants.JID",
                "confidence": "protocol",
            },
            "provider_revision": "opaque-v1",
            "participants": [
                {
                    "participant_ref": "70000000000001@lid",
                    "role": "admin",
                    "addresses": [
                        {
                            "namespace": "whatsapp.lid",
                            "value": "70000000000001@lid",
                            "value_normalized": "70000000000001@lid",
                            "role": "sender",
                        },
                        {
                            "namespace": "whatsapp.pn",
                            "value": "15550000001@s.whatsapp.net",
                            "value_normalized": "15550000001@s.whatsapp.net",
                            "role": "alternate",
                        },
                    ],
                }
            ],
        }
        snapshot = GroupMetadataDTO.from_dict(values)
        self.assertEqual(GroupMetadataDTO.from_dict(snapshot.to_dict()), snapshot)

        with self.assertRaisesRegex(DTOValidationError, "complete.*count"):
            GroupMetadataDTO.from_dict(dict(values, participant_count=2))
        duplicate = dict(values)
        duplicate["participant_count"] = 2
        duplicate["participants"] = values["participants"] * 2
        with self.assertRaisesRegex(DTOValidationError, "references.*unique"):
            GroupMetadataDTO.from_dict(duplicate)

        outside_roster = copy.deepcopy(values)
        outside_roster["own_protocol_participant"]["value"] = "79999999999999@lid"
        outside_roster["own_protocol_participant"][
            "value_normalized"
        ] = "79999999999999@lid"
        with self.assertRaisesRegex(DTOValidationError, "belong to the roster"):
            GroupMetadataDTO.from_dict(outside_roster)

        invalid_role = copy.deepcopy(values)
        invalid_role["own_protocol_participant"]["role"] = "primary"
        with self.assertRaisesRegex(DTOValidationError, "protocol sender"):
            GroupMetadataDTO.from_dict(invalid_role)

        group_command = {
            "command_id": "group-command-1",
            "command_type": "send_message",
            "account_ref": "account",
            "connection_ref": "connection",
            "conversation_ref": values["conversation_ref"],
            "conversation": {
                "conversation_type": "group",
                "addresses": [],
            },
            "own_protocol_participant": values["own_protocol_participant"],
        }
        command = CommandDTO.from_dict(group_command)
        self.assertEqual(CommandDTO.from_dict(command.to_dict()), command)
        direct_command = copy.deepcopy(group_command)
        direct_command["conversation"]["conversation_type"] = "direct"
        with self.assertRaisesRegex(DTOValidationError, "only valid for group"):
            CommandDTO.from_dict(direct_command)
        mutation_command = copy.deepcopy(group_command)
        mutation_command["command_type"] = "react"
        mutation_command["target_protocol_participant"] = copy.deepcopy(
            values["own_protocol_participant"]
        )
        mutation_command["options"] = {
            "target_external_message_id": "group-target-1",
            "target_from_me": True,
            "emoji": "👍",
            "operation": "add",
        }
        mutation = CommandDTO.from_dict(mutation_command)
        self.assertEqual(CommandDTO.from_dict(mutation.to_dict()), mutation)

        missing_target = copy.deepcopy(mutation_command)
        missing_target.pop("target_protocol_participant")
        with self.assertRaisesRegex(
            DTOValidationError, "require target_protocol_participant"
        ):
            CommandDTO.from_dict(missing_target)

        invalid_target_from_me = copy.deepcopy(mutation_command)
        invalid_target_from_me["options"]["target_from_me"] = "true"
        with self.assertRaisesRegex(DTOValidationError, "must be a boolean"):
            CommandDTO.from_dict(invalid_target_from_me)

    def test_group_mutation_event_accepts_optional_structured_protocol_target(self):
        participant = {
            "namespace": "whatsapp.lid",
            "value": "70000000000001@lid",
            "value_normalized": "70000000000001@lid",
            "role": "sender",
            "source_field": "event.Message.key.participant",
            "confidence": "protocol",
        }
        values = {
            "schema_version": 1,
            "provider_schema_version": "fixture-v1",
            "event_id": "group-reaction-1",
            "event_type": "message.reaction",
            "occurred_at": "2026-08-24T13:00:00Z",
            "account_ref": "account",
            "connection_ref": "connection",
            "conversation_ref": "120363000000001@g.us",
            "platform": "whatsapp",
            "direction": "inbound",
            "is_from_me": False,
            "origin": "provider",
            "actor": {"addresses": [participant]},
            "conversation": {
                "conversation_type": "group",
                "addresses": [
                    {
                        "namespace": "whatsapp.group",
                        "value": "120363000000001@g.us",
                        "value_normalized": "120363000000001@g.us",
                        "role": "group",
                    }
                ],
            },
            "mutation": {
                "type": "react",
                "target_external_message_id": "group-target-1",
                "target_from_me": False,
                "target_protocol_participant": participant,
                "emoji": "👍",
                "operation": "add",
            },
        }

        event = EventDTO.from_dict(values)
        self.assertEqual(EventDTO.from_dict(event.to_dict()), event)

        own_target = copy.deepcopy(values)
        own_target["mutation"]["target_from_me"] = True
        own_target["mutation"].pop("target_protocol_participant")
        own_event = EventDTO.from_dict(own_target)
        self.assertNotIn("target_protocol_participant", own_event.mutation)

        remote_without_observed_target = copy.deepcopy(values)
        remote_without_observed_target["mutation"].pop("target_protocol_participant")
        remote_event = EventDTO.from_dict(remote_without_observed_target)
        self.assertFalse(remote_event.mutation["target_from_me"])
        self.assertNotIn("target_protocol_participant", remote_event.mutation)

        without_target_direction = copy.deepcopy(values)
        without_target_direction["mutation"].pop("target_from_me")
        directionless_event = EventDTO.from_dict(without_target_direction)
        self.assertNotIn("target_from_me", directionless_event.mutation)

        invalid_target_from_me = copy.deepcopy(values)
        invalid_target_from_me["mutation"]["target_from_me"] = "false"
        with self.assertRaises(DTOValidationError):
            EventDTO.from_dict(invalid_target_from_me)

        invalid_participant = copy.deepcopy(values)
        invalid_participant["mutation"]["target_protocol_participant"][
            "role"
        ] = "alternate"
        with self.assertRaisesRegex(DTOValidationError, "protocol sender"):
            EventDTO.from_dict(invalid_participant)

        direct = copy.deepcopy(values)
        direct["conversation"]["conversation_type"] = "direct"
        with self.assertRaisesRegex(DTOValidationError, "only valid for groups"):
            EventDTO.from_dict(direct)

    def test_edit_provider_revision_is_optional_bounded_and_roundtrips(self):
        values = {
            "schema_version": 1,
            "provider_schema_version": "fixture-v1",
            "event_id": "edit-with-provider-revision",
            "event_type": "message.edited",
            "occurred_at": "2026-08-26T03:00:00Z",
            "account_ref": "account",
            "connection_ref": "connection",
            "conversation_ref": "conversation",
            "platform": "messenger",
            "direction": "inbound",
            "is_from_me": False,
            "origin": "provider",
            "actor": {},
            "conversation": {
                "conversation_type": "direct",
                "addresses": [],
            },
            "mutation": {
                "type": "edit",
                "target_external_message_id": "mid.1",
                "new_text": "Corrected text",
                "provider_revision": 0,
            },
        }

        event = EventDTO.from_dict(values)
        self.assertEqual(event.mutation["provider_revision"], 0)
        self.assertEqual(EventDTO.from_dict(event.to_dict()), event)

        maximum = copy.deepcopy(values)
        maximum["mutation"]["provider_revision"] = 2_147_483_647
        self.assertEqual(
            EventDTO.from_dict(maximum).mutation["provider_revision"],
            2_147_483_647,
        )

        for invalid_revision in (True, False, -1, 2_147_483_648, "2", 2.0, None):
            invalid = copy.deepcopy(values)
            invalid["mutation"]["provider_revision"] = invalid_revision
            with self.subTest(revision=invalid_revision), self.assertRaisesRegex(
                DTOValidationError, "bounded non-negative integer"
            ):
                EventDTO.from_dict(invalid)

        reaction = copy.deepcopy(values)
        reaction["mutation"] = {
            "type": "react",
            "target_external_message_id": "mid.1",
            "emoji": "\U0001f44d",
            "operation": "add",
            "provider_revision": 1,
        }
        with self.assertRaisesRegex(DTOValidationError, "only valid for edits"):
            EventDTO.from_dict(reaction)

    def test_group_avatar_result_never_serializes_binary_content(self):
        avatar = AvatarResult(
            state="ready",
            provider_revision="picture-1",
            content=b"not-a-real-image",
            mime_type="image/png",
            file_name="avatar.png",
        )
        self.assertEqual(avatar.size_bytes, len(avatar.content))
        self.assertNotIn("content", avatar.to_dict())
        self.assertEqual(AvatarResult(state="absent").state, "absent")
        with self.assertRaises(DTOValidationError):
            AvatarResult(state="unavailable", content=b"unexpected")

    def test_registry_decorator_and_duplicate_protection(self):
        registry = AdapterRegistry()
        registered = registry.register("dummy", module="dummy_module")(DummyAdapter)
        self.assertIs(registered, DummyAdapter)
        self.assertIs(registry.get("dummy"), DummyAdapter)
        self.assertEqual(registry.keys(), ("dummy",))
        self.assertEqual(registry.owner("dummy"), "dummy_module")

        class OtherDummy(DummyAdapter):
            pass

        with self.assertRaises(ValueError):
            registry.register("dummy", OtherDummy)

    def test_provider_request_snapshot_rejects_secrets_and_binary_content(self):
        safe = {
            "provider": "dummy",
            "method": "POST",
            "endpoint": "/messages",
            "payload": {"Body": "Hello", "size_bytes": 5},
        }
        self.assertEqual(validate_provider_request_snapshot(safe), safe)

        unsafe_snapshots = (
            dict(safe, headers={"Token": "secret"}),
            dict(safe, payload={"api_token": "secret"}),
            dict(safe, payload={"Image": b"binary"}),
            dict(safe, payload={"Image": "data:image/png;base64,AAAA"}),
            dict(safe, payload={"binary_content": "AAAA"}),
        )
        for snapshot in unsafe_snapshots:
            with self.subTest(snapshot=snapshot), self.assertRaises(AdapterError):
                validate_provider_request_snapshot(snapshot)
