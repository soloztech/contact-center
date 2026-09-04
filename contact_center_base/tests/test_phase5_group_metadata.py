import base64
import datetime
import uuid
from unittest.mock import patch

from psycopg2 import OperationalError

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.adapter import (
    AdapterResult,
    ProviderAdapter,
    ProviderRateLimitError,
    UnsupportedEventError,
    adapter_registry,
)
from ..services.dto import AvatarResult, DTOValidationError, EventDTO, GroupMetadataDTO
from ..services.tokens import CONTACT_CENTER_MEMBERSHIP_TOKEN, CONTACT_CENTER_POST_TOKEN


@adapter_registry.register("test.group.metadata")
class GroupMetadataTestAdapter(ProviderAdapter):
    display_name = "Group Metadata Test"
    snapshot = None
    avatar = AvatarResult(state="unavailable")
    before_metadata_return = None
    metadata_error = None

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return EventDTO.from_dict(envelope)

    def execute_command(self, connection, command):
        return AdapterResult.success(external_message_id="unexpected")

    def prepare_request_snapshot(self, connection, command):
        return {
            "provider": self.key,
            "method": "POST",
            "endpoint": "/unexpected",
            "payload": {"command_id": command.command_id},
        }

    def get_capabilities(self, connection):
        return {"send_message": False, "media": {}}

    def get_health(self, connection):
        return {"state": "connected"}

    def fetch_group_metadata(self, connection, conversation_ref):
        if self.__class__.before_metadata_return:
            self.__class__.before_metadata_return(connection, conversation_ref)
        if self.__class__.metadata_error:
            raise self.__class__.metadata_error
        return self.__class__.snapshot

    def fetch_group_avatar(self, connection, conversation_ref):
        return self.__class__.avatar


class TestContactCenterGroupMetadata(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        admin_group = cls.env.ref("contact_center_base.group_contact_center_admin")
        cls.agent = cls._create_user("Metadata Agent", agent_group)
        cls.outsider = cls._create_user("Metadata Outsider", agent_group)
        cls.admin = cls._create_user("Metadata Admin", admin_group)
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Metadata Team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Metadata Account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "metadata-account-%s" % uuid.uuid4(),
                "default_team_id": cls.team.id,
                "group_inbound_enabled": True,
            }
        )
        now = fields.Datetime.now()
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Metadata Provider",
                "account_id": cls.account.id,
                "adapter_key": "test.group.metadata",
                "external_ref": "metadata-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "fixture-v1",
                "state": "connected",
                "last_state_observed_at": now,
                "last_health_at": now,
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": False,
                "capabilities_json": {"send_message": False, "media": {}},
            }
        )
        cls.group_ref = "120363000000001@g.us"

    def setUp(self):
        super().setUp()
        now = fields.Datetime.now()
        self.connection.sudo().write(
            {
                "state": "connected",
                "last_state_observed_at": now,
                "last_health_at": now,
            }
        )
        GroupMetadataTestAdapter.snapshot = None
        GroupMetadataTestAdapter.avatar = AvatarResult(state="unavailable")
        GroupMetadataTestAdapter.before_metadata_return = None
        GroupMetadataTestAdapter.metadata_error = None
        self.observation_index = 0

    @classmethod
    def _create_user(cls, name, group):
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": name,
                    "login": "cc-metadata-%s" % uuid.uuid4(),
                    "email": "cc-metadata-%s@example.invalid" % uuid.uuid4(),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )

    def _event(self, *, group_ref=None, event_type="message.created", message=True):
        group_ref = group_ref or self.group_ref
        values = {
            "provider_schema_version": "fixture-v1",
            "event_id": "metadata-event-%s" % uuid.uuid4(),
            "event_type": event_type,
            "occurred_at": "2026-08-24T13:00:00Z",
            "account_ref": self.account.external_ref,
            "connection_ref": self.connection.external_ref,
            "conversation_ref": group_ref,
            "platform": "whatsapp",
            "direction": "inbound",
            "is_from_me": False,
            "origin": "provider",
            "actor": {
                "display_name": "Observed Sender",
                "addresses": (
                    [
                        {
                            "namespace": "whatsapp.lid",
                            "value": "70000000000001@lid",
                            "value_normalized": "70000000000001@lid",
                            "role": "sender",
                            "confidence": "protocol",
                        },
                        {
                            "namespace": "whatsapp.pn",
                            "value": "15550000001@s.whatsapp.net",
                            "value_normalized": "15550000001@s.whatsapp.net",
                            "role": "alternate",
                            "confidence": "protocol",
                        },
                    ]
                    if message
                    else []
                ),
            },
            "conversation": {
                "conversation_type": "group",
                "addresses": [
                    {
                        "namespace": "whatsapp.group",
                        "value": group_ref,
                        "value_normalized": group_ref,
                        "role": "group",
                        "confidence": "protocol",
                    }
                ],
            },
            "extensions": {
                "group_metadata_hint": {"kind": "metadata"},
            },
        }
        if message:
            values["message"] = {
                "external_message_id": "metadata-message-%s" % uuid.uuid4(),
                "content_type": "text",
                "text": "Creates the canonical group conversation",
            }
        return EventDTO.from_dict(values)

    def _create_group(self):
        self.env["contact.center.application"].with_context(
            contact_center_skip_enqueue=True
        )._process_event(self.connection, self._event())
        binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    ("conversation_ref", "=", self.group_ref),
                    ("conversation_type", "=", "group"),
                ],
                limit=1,
            )
        )
        profile = (
            self.env["contact.center.group.profile"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)], limit=1)
        )
        self.assertTrue(profile)
        return binding, profile

    def _snapshot(
        self,
        participants,
        *,
        complete=True,
        participant_count=None,
        revision="roster-v1",
        name="Operations Group",
        own_role="admin",
        own_protocol_participant=None,
    ):
        self.observation_index += 1
        participant_values = []
        for values in participants:
            lid, pn, role = values[:3]
            participant_ref = values[3] if len(values) > 3 else lid
            addresses = [
                {
                    "namespace": "whatsapp.lid",
                    "value": lid,
                    "value_normalized": lid,
                    "role": "sender",
                    "confidence": "protocol",
                }
            ]
            if pn:
                addresses.append(
                    {
                        "namespace": "whatsapp.pn",
                        "value": pn,
                        "value_normalized": pn,
                        "role": "alternate",
                        "confidence": "protocol",
                    }
                )
            participant_values.append(
                {
                    "participant_ref": participant_ref,
                    "display_name": "",
                    "role": role,
                    "addresses": addresses,
                }
            )
        if participant_count is None:
            participant_count = len(participant_values)
        observed_at = datetime.datetime(
            2026,
            8,
            24,
            13,
            self.observation_index,
            tzinfo=datetime.timezone.utc,
        )
        values = {
            "conversation_ref": self.group_ref,
            "observed_at": observed_at,
            "display_name": name,
            "participants": participant_values,
            "participant_count": participant_count,
            "own_role": own_role,
            "provider_revision": revision,
            "is_complete": complete,
        }
        if own_protocol_participant:
            values["own_protocol_participant"] = own_protocol_participant
        return GroupMetadataDTO.from_dict(values)

    def _sync(self, profile, snapshot, avatar=None, request_new_revision=False):
        if request_new_revision:
            profile.with_context(contact_center_skip_enqueue=True)._request_sync(
                self.connection, force=True
            )
            profile.invalidate_recordset()
        job_uuid = str(uuid.uuid4())
        profile.sudo().write({"queue_job_uuid": job_uuid})
        profile.invalidate_recordset()
        GroupMetadataTestAdapter.snapshot = snapshot
        GroupMetadataTestAdapter.avatar = avatar or AvatarResult(state="unavailable")
        result = profile.with_context(job_uuid=job_uuid)._job_sync_metadata(
            profile.sync_revision
        )
        profile.invalidate_recordset()
        return result

    def test_first_message_creates_one_profile_and_roster_creates_no_personas(self):
        binding, profile = self._create_group()
        guest_count = self.env["mail.guest"].sudo().search_count([])
        partner_count = self.env["res.partner"].sudo().search_count([])
        member_ids = binding.channel_id.sudo().channel_member_ids.ids
        snapshot = self._snapshot(
            [
                (
                    "70000000000001@lid",
                    "15550000001@s.whatsapp.net",
                    "admin",
                ),
                (
                    "70000000000002@lid",
                    "15550000002@s.whatsapp.net",
                    "member",
                ),
                ("70000000000003@lid", "", "superadmin"),
            ]
        )

        self.assertTrue(self._sync(profile, snapshot))

        self.assertEqual(profile.metadata_state, "ready")
        self.assertEqual(profile.participant_count, 3)
        self.assertEqual(profile.admin_count, 2)
        self.assertEqual(len(profile.participant_ids), 3)
        self.assertEqual(len(profile.participant_ids.alias_ids), 5)
        self.assertEqual(self.env["mail.guest"].sudo().search_count([]), guest_count)
        self.assertEqual(self.env["res.partner"].sudo().search_count([]), partner_count)
        self.assertEqual(binding.channel_id.sudo().channel_member_ids.ids, member_ids)

    def test_own_participant_is_revision_bound_and_partial_absence_clears_it(self):
        _binding, profile = self._create_group()
        lid = "70000000000001@lid"
        candidate = {
            "namespace": "whatsapp.lid",
            "value": lid,
            "value_normalized": lid,
            "role": "sender",
            "source_field": "data.Participants.JID",
            "confidence": "protocol",
            "resolution_scope": "account",
        }
        complete = self._snapshot(
            [(lid, "15550000001@s.whatsapp.net", "admin")],
            own_protocol_participant=candidate,
        )

        self.assertTrue(self._sync(profile, complete))
        profile.invalidate_recordset(
            [
                "own_protocol_participant_json",
                "own_protocol_participant_health_revision",
            ]
        )
        self.assertEqual(profile.own_protocol_participant_json, candidate)
        self.assertEqual(
            profile.own_protocol_participant_health_revision,
            self.connection.health_configuration_revision,
        )

        partial = self._snapshot(
            [(lid, "15550000001@s.whatsapp.net", "admin")],
            complete=False,
            revision="partial-without-own",
        )
        self.assertTrue(self._sync(profile, partial, request_new_revision=True))
        profile.invalidate_recordset(
            [
                "own_protocol_participant_json",
                "own_protocol_participant_health_revision",
            ]
        )
        self.assertFalse(profile.own_protocol_participant_json)
        self.assertEqual(profile.own_protocol_participant_health_revision, 0)

    def test_health_configuration_change_invalidates_group_sender_evidence(self):
        _binding, profile = self._create_group()
        lid = "70000000000001@lid"
        snapshot = self._snapshot(
            [(lid, "15550000001@s.whatsapp.net", "admin")],
            own_protocol_participant={
                "namespace": "whatsapp.lid",
                "value": lid,
                "value_normalized": lid,
                "role": "sender",
                "source_field": "data.Participants.JID",
                "confidence": "protocol",
            },
        )
        self.assertTrue(self._sync(profile, snapshot))

        self.connection.sudo().write(
            {
                "health_configuration_revision": (
                    self.connection.health_configuration_revision + 1
                )
            }
        )

        profile.invalidate_recordset(
            [
                "metadata_state",
                "next_sync_at",
                "own_protocol_participant_json",
                "own_protocol_participant_health_revision",
            ]
        )
        self.assertEqual(profile.metadata_state, "stale")
        self.assertTrue(profile.next_sync_at)
        self.assertFalse(profile.own_protocol_participant_json)
        self.assertEqual(profile.own_protocol_participant_health_revision, 0)

    def test_ordinary_messages_coalesce_while_the_initial_job_is_active(self):
        _binding, profile = self._create_group()
        previous_revision = profile.sync_revision
        previous_requested_at = profile.sync_requested_at

        with patch.object(
            type(profile), "_has_active_queue_job", autospec=True, return_value=True
        ):
            profile._request_sync(self.connection)

        profile.invalidate_recordset()
        self.assertEqual(profile.sync_revision, previous_revision)
        self.assertEqual(profile.sync_requested_at, previous_requested_at)

    def test_reordered_snapshot_is_idempotent_and_keeps_the_same_hash(self):
        _binding, profile = self._create_group()
        participants = [
            ("70000000000001@lid", "15550000001@s.whatsapp.net", "admin"),
            ("70000000000002@lid", "15550000002@s.whatsapp.net", "member"),
        ]
        self._sync(profile, self._snapshot(participants))
        participant_ids = profile.participant_ids.ids
        alias_ids = profile.participant_ids.alias_ids.ids
        snapshot_hash = profile.snapshot_hash

        self._sync(
            profile,
            self._snapshot(list(reversed(participants))),
            request_new_revision=True,
        )

        self.assertEqual(profile.snapshot_hash, snapshot_hash)
        self.assertEqual(profile.participant_ids.ids, participant_ids)
        self.assertEqual(profile.participant_ids.alias_ids.ids, alias_ids)

    def test_partial_snapshot_never_removes_and_complete_snapshot_marks_left(self):
        _binding, profile = self._create_group()
        first = ("70000000000001@lid", "15550000001@s.whatsapp.net", "admin")
        second = ("70000000000002@lid", "15550000002@s.whatsapp.net", "member")
        self._sync(profile, self._snapshot([first, second]))

        self._sync(
            profile,
            self._snapshot(
                [first], complete=False, participant_count=2, revision="partial-v2"
            ),
            request_new_revision=True,
        )
        self.assertEqual(len(profile.participant_ids.filtered("active")), 2)
        self.assertEqual(profile.metadata_state, "stale")
        self.assertFalse(profile.roster_complete)
        partial_payload = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._serialize_conversation(profile.channel_id.with_user(self.agent))
        )
        self.assertIs(partial_payload["group"]["participant_count"], False)
        self.assertIs(partial_payload["group"]["admin_count"], False)

        self._sync(
            profile,
            self._snapshot([first], revision="complete-v3"),
            request_new_revision=True,
        )
        self.assertEqual(len(profile.participant_ids.filtered("active")), 1)
        self.assertEqual(profile.metadata_state, "ready")
        self.assertTrue(profile.roster_complete)
        removed = profile.with_context(active_test=False).participant_ids.filtered(
            lambda item: not item.active
        )
        self.assertEqual(len(removed), 1)
        self.assertTrue(removed.removed_at)

    def test_lid_then_lid_and_pn_enriches_one_roster_entry(self):
        _binding, profile = self._create_group()
        lid = "70000000000001@lid"
        pn = "15550000001@s.whatsapp.net"
        self._sync(profile, self._snapshot([(lid, "", "member")]))

        self._sync(
            profile,
            self._snapshot([(lid, pn, "admin")], revision="roster-v2"),
            request_new_revision=True,
        )

        self.assertEqual(len(profile.participant_ids), 1)
        self.assertEqual(profile.participant_ids.role, "admin")
        self.assertEqual(
            set(profile.participant_ids.alias_ids.mapped("value_normalized")),
            {lid, pn},
        )

    def test_removed_participant_can_return_with_a_new_observed_address(self):
        _binding, profile = self._create_group()
        original_lid = "70000000000001@lid"
        original_pn = "15550000001@s.whatsapp.net"
        self._sync(profile, self._snapshot([(original_lid, original_pn, "member")]))
        participant = profile.participant_ids

        self._sync(
            profile,
            self._snapshot([], revision="roster-v2"),
            request_new_revision=True,
        )
        participant.invalidate_recordset()
        self.assertFalse(participant.active)

        new_lid = "70000000000999@lid"
        new_pn = "15550000999@s.whatsapp.net"
        self._sync(
            profile,
            self._snapshot(
                [(new_lid, new_pn, "admin", original_lid)],
                revision="roster-v3",
            ),
            request_new_revision=True,
        )

        all_participants = profile.with_context(active_test=False).participant_ids
        self.assertEqual(all_participants.ids, participant.ids)
        self.assertTrue(participant.active)
        self.assertEqual(participant.role, "admin")
        self.assertEqual(
            set(participant.alias_ids.mapped("value_normalized")),
            {original_lid, original_pn, new_lid, new_pn},
        )

    def test_alias_conflict_fails_atomically_without_merging_people(self):
        _binding, profile = self._create_group()
        first_lid = "70000000000001@lid"
        first_pn = "15550000001@s.whatsapp.net"
        second_lid = "70000000000002@lid"
        second_pn = "15550000002@s.whatsapp.net"
        self._sync(
            profile,
            self._snapshot(
                [
                    (first_lid, first_pn, "member"),
                    (second_lid, second_pn, "member"),
                ]
            ),
        )
        participant_ids = profile.participant_ids.ids
        alias_ids = profile.participant_ids.alias_ids.ids
        conflicting = GroupMetadataDTO.from_dict(
            {
                "conversation_ref": self.group_ref,
                "observed_at": "2026-08-24T13:59:00Z",
                "display_name": "Operations Group",
                "participant_count": 1,
                "own_role": "unknown",
                "provider_revision": "conflicting-v2",
                "participants": [
                    {
                        "participant_ref": first_lid,
                        "role": "member",
                        "addresses": [
                            {
                                "namespace": "whatsapp.lid",
                                "value": first_lid,
                                "value_normalized": first_lid,
                                "role": "sender",
                            },
                            {
                                "namespace": "whatsapp.pn",
                                "value": second_pn,
                                "value_normalized": second_pn,
                                "role": "alternate",
                            },
                        ],
                    }
                ],
            }
        )

        self.assertFalse(self._sync(profile, conflicting, request_new_revision=True))
        self.assertEqual(profile.metadata_state, "failed")
        self.assertEqual(profile.participant_ids.ids, participant_ids)
        self.assertEqual(profile.participant_ids.alias_ids.ids, alias_ids)

    def test_two_snapshot_items_cannot_resolve_to_one_historical_participant(self):
        _binding, profile = self._create_group()
        lid = "70000000000001@lid"
        pn = "15550000001@s.whatsapp.net"
        self._sync(profile, self._snapshot([(lid, pn, "member")]))
        participant = profile.participant_ids
        participant_values = participant.read(["name", "role", "last_seen_at"])[0]
        snapshot_hash = profile.snapshot_hash
        applied_revision = profile.applied_revision
        alias_owners = {
            alias.id: alias.participant_id.id for alias in participant.alias_ids
        }
        split_identity = GroupMetadataDTO.from_dict(
            {
                "conversation_ref": self.group_ref,
                "observed_at": "2026-08-24T14:00:00Z",
                "display_name": "Operations Group",
                "participant_count": 2,
                "own_role": "unknown",
                "provider_revision": "split-v2",
                "participants": [
                    {
                        "participant_ref": lid,
                        "display_name": "Must roll back",
                        "role": "admin",
                        "addresses": [
                            {
                                "namespace": "whatsapp.lid",
                                "value": lid,
                                "value_normalized": lid,
                                "role": "sender",
                            }
                        ],
                    },
                    {
                        "participant_ref": pn,
                        "role": "member",
                        "addresses": [
                            {
                                "namespace": "whatsapp.pn",
                                "value": pn,
                                "value_normalized": pn,
                                "role": "alternate",
                            }
                        ],
                    },
                ],
            }
        )

        self.assertFalse(self._sync(profile, split_identity, request_new_revision=True))
        self.assertEqual(profile.metadata_state, "failed")
        participant.invalidate_recordset()
        self.assertEqual(
            participant.read(["name", "role", "last_seen_at"])[0],
            participant_values,
        )
        self.assertEqual(profile.snapshot_hash, snapshot_hash)
        self.assertEqual(profile.applied_revision, applied_revision)
        self.assertEqual(
            {alias.id: alias.participant_id.id for alias in participant.alias_ids},
            alias_owners,
        )

    def test_metadata_hint_only_invalidates_an_existing_group(self):
        binding, profile = self._create_group()
        self._sync(
            profile,
            self._snapshot(
                [
                    (
                        "70000000000001@lid",
                        "15550000001@s.whatsapp.net",
                        "admin",
                    )
                ]
            ),
        )
        previous_revision = profile.sync_revision
        hint = self._event(event_type="group.metadata.changed", message=False)

        result = (
            self.env["contact.center.application"]
            .with_context(contact_center_skip_enqueue=True)
            ._process_event(self.connection, hint)
        )

        self.assertEqual(result, profile)
        self.assertEqual(profile.sync_revision, previous_revision + 1)
        self.assertEqual(profile.metadata_state, "stale")
        unknown_ref = "120363000000099@g.us"
        unknown = self._event(
            group_ref=unknown_ref,
            event_type="group.metadata.changed",
            message=False,
        )
        self.env["contact.center.application"].with_context(
            contact_center_skip_enqueue=True
        )._process_event(self.connection, unknown)
        self.assertFalse(
            self.env["contact.center.channel.binding"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    ("conversation_ref", "=", unknown_ref),
                ]
            )
        )
        self.assertEqual(binding.group_profile_ids, profile)

    def test_metadata_hint_locks_channel_binding_before_enriching_aliases(self):
        binding, _profile = self._create_group()
        hint = self._event(event_type="group.metadata.changed", message=False)
        application_type = type(self.env["contact.center.application"])
        original_lock = application_type._lock_inbound_projection_binding
        original_enrich = application_type._enrich_channel_aliases
        observed_order = []

        def record_lock(application, candidate, account):
            observed_order.append(("lock", candidate.id))
            return original_lock(application, candidate, account)

        def record_enrich(application, candidate, addresses):
            observed_order.append(("enrich", candidate.id))
            return original_enrich(application, candidate, addresses)

        with patch.object(
            application_type,
            "_lock_inbound_projection_binding",
            autospec=True,
            side_effect=record_lock,
        ), patch.object(
            application_type,
            "_enrich_channel_aliases",
            autospec=True,
            side_effect=record_enrich,
        ):
            self.env["contact.center.application"].with_context(
                contact_center_skip_enqueue=True
            )._process_event(self.connection, hint)

        self.assertEqual(
            observed_order,
            [("lock", binding.id), ("enrich", binding.id)],
        )

    def test_ui_exposes_only_group_aggregates_and_acl_hides_the_roster(self):
        binding, profile = self._create_group()
        snapshot = self._snapshot(
            [
                ("70000000000001@lid", "15550000001@s.whatsapp.net", "admin"),
                ("70000000000002@lid", "15550000002@s.whatsapp.net", "member"),
            ]
        )
        self._sync(profile, snapshot)
        payload = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._serialize_conversation(binding.channel_id.with_user(self.agent))
        )

        self.assertEqual(
            set(payload["group"]),
            {
                "display_name",
                "avatar_url",
                "participant_count",
                "admin_count",
                "own_role",
                "metadata_state",
                "last_synced_at",
            },
        )
        self.assertEqual(payload["group"]["participant_count"], 2)
        self.assertNotIn("participants", payload["group"])
        self.assertTrue(
            self.env["contact.center.group.profile"]
            .with_user(self.agent)
            .search([("id", "=", profile.id)])
        )
        readable_profile = profile.with_user(self.agent).read()[0]
        self.assertNotIn("participant_ids", readable_profile)
        self.assertFalse(
            self.env["contact.center.group.profile"]
            .with_user(self.outsider)
            .search([("id", "=", profile.id)])
        )
        with self.assertRaises(AccessError):
            self.env["contact.center.group.participant"].with_user(self.agent).search(
                []
            )
        self.assertEqual(
            self.env["contact.center.group.participant"]
            .with_user(self.admin)
            .search_count([("group_profile_id", "=", profile.id)]),
            2,
        )

        replacement = self.connection.copy(
            {
                "name": "UI Exact Metadata Provider",
                "external_ref": "metadata-ui-exact-%s" % uuid.uuid4(),
            }
        )
        profile.sudo().write({"provider_connection_id": replacement.id})
        exact_payload = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._serialize_conversation(binding.channel_id.with_user(self.agent))
        )
        self.assertEqual(exact_payload["provider_connection"]["id"], replacement.id)

    def test_runtime_contract_is_admin_only_and_secret_free(self):
        with self.assertRaises(AccessError):
            self.env["contact.center.group.profile"].with_user(
                self.agent
            ).get_group_metadata_runtime_contract()

        contract = (
            self.env["contact.center.group.profile"]
            .with_user(self.admin)
            .get_group_metadata_runtime_contract()
        )
        self.assertEqual(contract["priority"], 50)
        self.assertEqual(contract["ttl_seconds"], 6 * 60 * 60)
        self.assertEqual(contract["partial_retry_seconds"], 15 * 60)
        self.assertEqual(contract["avatar_max_bytes"], 2 * 1024 * 1024)
        self.assertEqual(
            set(contract["ui_group_keys"]),
            {
                "display_name",
                "avatar_url",
                "participant_count",
                "admin_count",
                "own_role",
                "metadata_state",
                "last_synced_at",
            },
        )
        self.assertEqual(
            contract["cron"],
            {
                "id": self.env.ref(
                    "contact_center_base.ir_cron_contact_center_group_metadata"
                ).id,
                "model": "contact.center.group.profile",
                "state": "code",
                "code": "model._cron_schedule_group_metadata()",
                "active": True,
                "interval_number": 15,
                "interval_type": "minutes",
                "numbercall": -1,
                "doall": False,
            },
        )
        self.assertEqual(
            contract["job_function"]["model"], "contact.center.group.profile"
        )
        self.assertEqual(contract["job_function"]["method"], "_job_sync_metadata")
        self.assertEqual(
            contract["roster_acl"],
            {
                "contact.center.group.participant": {
                    "record_count": 1,
                    "admin_only": True,
                    "read_only": True,
                },
                "contact.center.group.participant.alias": {
                    "record_count": 1,
                    "admin_only": True,
                    "read_only": True,
                },
            },
        )
        self.assertEqual(
            contract["profile_acl"],
            {
                "record_count": 2,
                "agent_read_only": True,
                "admin_read_only": True,
            },
        )
        self.assertEqual(
            contract["record_rules"],
            {
                "contact.center.group.profile": {
                    "record_count": 2,
                    "agent_membership_scoped": True,
                    "admin_company_scoped": True,
                },
                "contact.center.group.participant": {
                    "record_count": 1,
                    "admin_company_scoped": True,
                },
                "contact.center.group.participant.alias": {
                    "record_count": 1,
                    "admin_company_scoped": True,
                },
            },
        )
        self.assertNotIn("participants", repr(contract))
        self.assertNotIn("provider_connection", repr(contract))

    def test_admin_can_retry_failed_group_metadata_without_duplicate_job(self):
        _binding, profile = self._create_group()
        previous_revision = profile.sync_revision
        profile.sudo().write(
            {
                "metadata_state": "failed",
                "attempts": 12,
                "queue_job_uuid": False,
                "last_error_class": "AdapterError",
                "last_error_message": "sanitized failure",
            }
        )

        with self.assertRaises(AccessError):
            profile.with_user(self.agent).action_retry_metadata_sync()

        with trap_jobs() as trap:
            self.assertTrue(
                profile.with_user(self.admin)
                .with_context(contact_center_skip_enqueue=True)
                .action_retry_metadata_sync()
            )
            trap.assert_jobs_count(1)
            queued_job = trap.enqueued_jobs[0]
            self.assertEqual(queued_job.channel, "root.contact_center.group_metadata")
            self.assertEqual(queued_job.priority, 50)
            self.assertEqual(
                queued_job.identity_key,
                "contact_center:group_metadata:%s:%s"
                % (profile.id, previous_revision + 1),
            )

        profile.invalidate_recordset()
        self.assertEqual(profile.metadata_state, "pending")
        self.assertEqual(profile.sync_revision, previous_revision + 1)
        self.assertEqual(profile.queue_job_uuid, queued_job.uuid)
        self.assertEqual(profile.attempts, 0)
        self.assertFalse(profile.last_error_class)
        self.assertFalse(profile.last_error_message)

        profile.sudo().write({"metadata_state": "stale", "queue_job_uuid": False})
        profile._enqueue_sync(profile.sync_revision)
        profile.invalidate_recordset()
        active_job_uuid = profile.queue_job_uuid
        profile.sudo().write({"queue_job_uuid": False})
        with trap_jobs() as active_trap:
            self.assertTrue(profile.with_user(self.admin).action_retry_metadata_sync())
            active_trap.assert_jobs_count(0)
        profile.invalidate_recordset()
        self.assertEqual(profile.sync_revision, previous_revision + 1)
        self.assertEqual(profile.queue_job_uuid, active_job_uuid)

    def test_group_metadata_worker_requires_exact_persisted_owner(self):
        _binding, profile = self._create_group()
        current_uuid = str(uuid.uuid4())
        stale_uuid = str(uuid.uuid4())

        with patch.object(
            GroupMetadataTestAdapter, "fetch_group_metadata", autospec=True
        ) as fetch_metadata:
            self.assertFalse(
                profile.with_context(job_uuid=current_uuid)._job_sync_metadata(
                    profile.sync_revision
                )
            )
            profile.sudo().write({"queue_job_uuid": stale_uuid})
            self.assertFalse(
                profile.with_context(job_uuid=current_uuid)._job_sync_metadata(
                    profile.sync_revision
                )
            )

        fetch_metadata.assert_not_called()
        self.assertFalse(profile.last_synced_at)

    def test_stale_group_job_never_replaces_the_current_revision_job(self):
        _binding, profile = self._create_group()
        old_revision = profile.sync_revision
        old_uuid = str(uuid.uuid4())
        profile.sudo().write({"queue_job_uuid": old_uuid})
        profile._request_sync(self.connection, force=True)
        profile.invalidate_recordset(["sync_revision", "queue_job_uuid"])
        successor_uuid = profile.queue_job_uuid
        self.assertEqual(profile.sync_revision, old_revision + 1)
        self.assertNotEqual(successor_uuid, old_uuid)

        with patch.object(
            GroupMetadataTestAdapter, "fetch_group_metadata", autospec=True
        ) as fetch_metadata:
            self.assertFalse(
                profile.with_context(job_uuid=old_uuid)._job_sync_metadata(old_revision)
            )

        profile.invalidate_recordset(["queue_job_uuid"])
        fetch_metadata.assert_not_called()
        self.assertEqual(profile.queue_job_uuid, successor_uuid)

    def test_ready_and_absent_avatar_update_private_attachment_and_ui_url(self):
        binding, profile = self._create_group()
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0l"
            "EQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        ready = AvatarResult(
            state="ready",
            provider_revision="picture-v1",
            content=png,
            mime_type="image/png",
            file_name="group.png",
        )
        self._sync(
            profile,
            self._snapshot([]),
            avatar=ready,
        )
        attachment = profile.avatar_attachment_id
        self.assertTrue(attachment)
        self.assertEqual(attachment.res_model, profile._name)
        payload = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._serialize_conversation(binding.channel_id.with_user(self.agent))
        )
        self.assertTrue(payload["group"]["avatar_url"].startswith("/contact_center/"))
        self.assertNotIn("wuzapi", payload["group"]["avatar_url"])

        self._sync(
            profile,
            self._snapshot([], revision="roster-same-avatar"),
            avatar=ready,
            request_new_revision=True,
        )
        self.assertEqual(profile.avatar_attachment_id, attachment)

        self._sync(
            profile,
            self._snapshot([], revision="roster-avatar-unavailable"),
            avatar=AvatarResult(state="unavailable"),
            request_new_revision=True,
        )
        self.assertEqual(profile.avatar_attachment_id, attachment)

        changed = AvatarResult(
            state="ready",
            provider_revision="picture-v2",
            content=png + b"changed",
            mime_type="image/png",
            file_name="group-v2.png",
        )
        self._sync(
            profile,
            self._snapshot([], revision="roster-new-avatar"),
            avatar=changed,
            request_new_revision=True,
        )
        replacement = profile.avatar_attachment_id
        self.assertTrue(replacement)
        self.assertNotEqual(replacement, attachment)
        self.assertFalse(attachment.exists())

        self._sync(
            profile,
            self._snapshot([], revision="roster-avatar-absent"),
            avatar=AvatarResult(state="absent", provider_revision="picture-v3"),
            request_new_revision=True,
        )
        self.assertFalse(profile.avatar_attachment_id)
        self.assertFalse(replacement.exists())

    def test_result_is_discarded_when_a_new_revision_arrives_during_provider_io(self):
        _binding, profile = self._create_group()
        snapshot = self._snapshot(
            [("70000000000001@lid", "15550000001@s.whatsapp.net", "admin")]
        )
        old_revision = profile.sync_revision

        def invalidate_during_fetch(_connection, _conversation_ref):
            profile.with_context(contact_center_skip_enqueue=True)._request_sync(
                self.connection, force=True
            )

        GroupMetadataTestAdapter.snapshot = snapshot
        GroupMetadataTestAdapter.before_metadata_return = invalidate_during_fetch
        job_uuid = str(uuid.uuid4())
        profile.sudo().write({"queue_job_uuid": job_uuid})
        with patch.object(
            type(profile), "_enqueue_sync", autospec=True, return_value=True
        ) as enqueue:
            result = profile.with_context(job_uuid=job_uuid)._job_sync_metadata(
                old_revision
            )

        profile.invalidate_recordset()
        self.assertFalse(result)
        self.assertEqual(profile.sync_revision, old_revision + 1)
        self.assertNotEqual(profile.metadata_state, "ready")
        self.assertFalse(profile.last_synced_at)
        self.assertFalse(profile.participant_ids)
        enqueue.assert_called_once()

    def test_connection_change_during_provider_io_discards_the_old_result(self):
        _binding, profile = self._create_group()
        replacement = self.connection.copy(
            {
                "name": "Replacement Metadata Provider",
                "external_ref": "metadata-replacement-%s" % uuid.uuid4(),
            }
        )
        snapshot = self._snapshot(
            [("70000000000001@lid", "15550000001@s.whatsapp.net", "admin")]
        )
        old_revision = profile.sync_revision

        def switch_connection(_connection, _conversation_ref):
            profile.with_context(contact_center_skip_enqueue=True)._request_sync(
                replacement, force=True
            )

        GroupMetadataTestAdapter.snapshot = snapshot
        GroupMetadataTestAdapter.before_metadata_return = switch_connection
        job_uuid = str(uuid.uuid4())
        profile.sudo().write({"queue_job_uuid": job_uuid})
        with patch.object(
            type(profile), "_enqueue_sync", autospec=True, return_value=True
        ):
            result = profile.with_context(job_uuid=job_uuid)._job_sync_metadata(
                old_revision
            )

        profile.invalidate_recordset()
        self.assertFalse(result)
        self.assertEqual(profile.provider_connection_id, replacement)
        self.assertEqual(profile.sync_revision, old_revision + 1)
        self.assertFalse(profile.last_synced_at)
        self.assertFalse(profile.participant_ids)

    def test_profile_deleted_during_provider_io_finishes_as_a_noop(self):
        binding, profile = self._create_group()
        snapshot = self._snapshot(
            [("70000000000001@lid", "15550000001@s.whatsapp.net", "admin")]
        )

        def delete_scope(_connection, _conversation_ref):
            binding.sudo().unlink()

        GroupMetadataTestAdapter.snapshot = snapshot
        GroupMetadataTestAdapter.before_metadata_return = delete_scope
        job_uuid = str(uuid.uuid4())
        profile.sudo().write({"queue_job_uuid": job_uuid})

        result = profile.with_context(job_uuid=job_uuid)._job_sync_metadata(
            profile.sync_revision
        )

        self.assertFalse(result)
        self.assertFalse(profile.exists())

    def test_health_configuration_change_during_io_retries_without_projection(self):
        _binding, profile = self._create_group()
        snapshot = self._snapshot(
            [("70000000000001@lid", "15550000001@s.whatsapp.net", "admin")]
        )

        def rotate_configuration(_connection, _conversation_ref):
            self.connection.sudo().write(
                {
                    "health_configuration_revision": (
                        self.connection.health_configuration_revision + 1
                    )
                }
            )

        GroupMetadataTestAdapter.snapshot = snapshot
        GroupMetadataTestAdapter.before_metadata_return = rotate_configuration
        job_uuid = str(uuid.uuid4())
        profile.sudo().write({"queue_job_uuid": job_uuid})

        with self.assertRaises(RetryableJobError):
            profile.with_context(job_uuid=job_uuid)._job_sync_metadata(
                profile.sync_revision
            )

        profile.invalidate_recordset()
        self.assertFalse(profile.last_synced_at)
        self.assertFalse(profile.participant_ids)

    def test_database_operational_error_escapes_for_queue_job_retry(self):
        _binding, profile = self._create_group()
        job_uuid = str(uuid.uuid4())
        profile.sudo().write({"queue_job_uuid": job_uuid})
        database_error = OperationalError("synthetic serialization failure")

        with patch.object(
            type(profile),
            "_run_metadata_sync",
            autospec=True,
            side_effect=database_error,
        ), patch.object(
            type(profile), "_retry_unexpected", autospec=True
        ) as unexpected:
            with self.assertRaises(OperationalError) as raised:
                profile.with_context(job_uuid=job_uuid)._job_sync_metadata(
                    profile.sync_revision
                )

        self.assertIs(raised.exception, database_error)
        unexpected.assert_not_called()

    def test_rate_limited_group_read_does_not_consume_the_retry_ceiling(self):
        _binding, profile = self._create_group()
        waiter = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "metadata-paused-waiter-%s" % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": {"fixture": "paused-waiter"},
                }
            )
        )
        waiter.write(
            {
                "waiting_group_profile_id": profile.id,
                "waiting_group_roster_after": fields.Datetime.now(),
                "group_roster_wait_count": 1,
                "first_group_roster_wait_at": fields.Datetime.now(),
            }
        )
        rate_limit = ProviderRateLimitError("provider rate limited group metadata")
        rate_limit.retry_after_seconds = 37
        GroupMetadataTestAdapter.metadata_error = rate_limit
        job_uuid = str(uuid.uuid4())
        profile.sudo().write(
            {
                "attempts": 11,
                "queue_job_uuid": job_uuid,
            }
        )

        with self.assertRaises(RetryableJobError) as raised:
            profile.with_context(job_uuid=job_uuid)._job_sync_metadata(
                profile.sync_revision
            )

        profile.invalidate_recordset(["attempts", "metadata_state", "queue_job_uuid"])
        self.assertTrue(raised.exception.ignore_retry)
        self.assertGreaterEqual(raised.exception.seconds, 37)
        self.assertIs(raised.exception.__cause__, rate_limit)
        self.assertEqual(profile.attempts, 11)
        self.assertNotEqual(profile.metadata_state, "failed")
        self.assertEqual(profile.queue_job_uuid, job_uuid)
        waiter.invalidate_recordset(
            [
                "state",
                "waiting_group_profile_id",
                "waiting_group_roster_after",
                "group_roster_wait_count",
            ]
        )
        self.assertEqual(waiter.state, "pending")
        self.assertEqual(waiter.waiting_group_profile_id, profile)
        self.assertTrue(waiter.waiting_group_roster_after)
        self.assertEqual(waiter.group_roster_wait_count, 1)

    def test_waiter_release_failure_keeps_the_valid_snapshot_ready(self):
        _binding, profile = self._create_group()
        snapshot = self._snapshot(
            [("70000000000001@lid", "15550000001@s.whatsapp.net", "admin")]
        )
        waiter = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "metadata-release-failure-waiter-%s"
                    % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": {"fixture": "release-failure-waiter"},
                }
            )
        )
        waiter.write(
            {
                "waiting_group_profile_id": profile.id,
                "waiting_group_roster_after": fields.Datetime.now(),
                "group_roster_wait_count": 1,
                "first_group_roster_wait_at": fields.Datetime.now(),
            }
        )
        profile_type = type(profile)

        def fail_release_with_database_error(profile_recordset, limit=100):
            del limit
            profile_recordset.env["contact.center.inbox.event"].sudo().search(
                [("waiting_group_profile_id", "=", profile_recordset.id)]
            ).write({"attempts": 9})
            profile_recordset.env.cr.execute("SELECT 1 / 0")

        with patch.object(
            profile_type,
            "_release_roster_waiters",
            fail_release_with_database_error,
        ), patch(
            "odoo.addons.contact_center_base.models.group._logger.exception"
        ) as release_log:
            self.assertTrue(self._sync(profile, snapshot))

        self.env.cr.execute("SELECT 1")
        self.assertEqual(self.env.cr.fetchone(), (1,))

        profile.invalidate_recordset(
            [
                "metadata_state",
                "roster_complete",
                "applied_revision",
                "sync_revision",
                "last_error_class",
                "next_sync_at",
            ]
        )
        self.assertEqual(profile.metadata_state, "ready")
        self.assertTrue(profile.roster_complete)
        self.assertEqual(profile.applied_revision, profile.sync_revision)
        self.assertFalse(profile.last_error_class)
        self.assertTrue(profile.next_sync_at)
        waiter.invalidate_recordset(
            ["state", "attempts", "waiting_group_profile_id", "queue_job_uuid"]
        )
        self.assertEqual(waiter.state, "pending")
        self.assertEqual(waiter.attempts, 0)
        self.assertEqual(waiter.waiting_group_profile_id, profile)
        self.assertFalse(waiter.queue_job_uuid)
        release_log.assert_called_once()

    def test_cron_backfills_an_existing_group_without_creating_personas(self):
        binding, profile = self._create_group()
        guest_count = self.env["mail.guest"].sudo().search_count([])
        partner_count = self.env["res.partner"].sudo().search_count([])
        member_ids = binding.channel_id.sudo().channel_member_ids.ids
        profile.sudo().unlink()

        with trap_jobs() as trap:
            self.env["contact.center.group.profile"]._cron_schedule_group_metadata(
                limit=100
            )
            trap.assert_jobs_count(1)

            backfilled = (
                self.env["contact.center.group.profile"]
                .sudo()
                .search([("channel_binding_id", "=", binding.id)])
            )
            queued_job = trap.enqueued_jobs[0]
            self.assertEqual(queued_job.channel, "root.contact_center.group_metadata")
            self.assertEqual(queued_job.priority, 50)
            self.assertEqual(
                queued_job.identity_key,
                "contact_center:group_metadata:%s:%s"
                % (backfilled.id, backfilled.sync_revision),
            )
            self.assertEqual(backfilled.queue_job_uuid, queued_job.uuid)
        self.assertEqual(len(backfilled), 1)
        self.assertEqual(backfilled.provider_connection_id, self.connection)
        self.assertEqual(backfilled.metadata_state, "pending")
        self.assertEqual(backfilled.sync_revision, 1)
        self.assertEqual(self.env["mail.guest"].sudo().search_count([]), guest_count)
        self.assertEqual(self.env["res.partner"].sudo().search_count([]), partner_count)
        self.assertEqual(binding.channel_id.sudo().channel_member_ids.ids, member_ids)

        # ``trap_jobs`` exposes a queue_job ``Job`` value object, not an Odoo
        # recordset.  Some queue_job revisions persist the trapped row and some do
        # not, so mark it terminal through the canonical model only when present.
        persisted_job = queued_job.db_record()
        if persisted_job:
            persisted_job.write({"state": "done"})
        backfilled.sudo().write(
            {
                "sync_requested_at": fields.Datetime.now()
                - datetime.timedelta(minutes=10),
                # Simulate a persisted reference whose runnable queue.job was lost.
                # The original job is explicitly terminal so canonical recovery is
                # not allowed to adopt it as the current revision owner.
                "queue_job_uuid": str(uuid.uuid4()),
            }
        )
        with trap_jobs() as recovery_trap:
            self.env["contact.center.group.profile"]._cron_schedule_group_metadata(
                limit=100
            )
            backfilled.invalidate_recordset(
                ["sync_revision", "queue_job_uuid", "sync_requested_at"]
            )
            self.assertEqual(backfilled.sync_revision, 2)
            recovery_trap.assert_jobs_count(1)
            recovered_job = recovery_trap.enqueued_jobs[0]
            self.assertEqual(recovered_job.priority, 50)
            self.assertEqual(backfilled.queue_job_uuid, recovered_job.uuid)
            self.assertEqual(
                recovered_job.identity_key,
                "contact_center:group_metadata:%s:2" % backfilled.id,
            )

    def test_cron_does_not_supersede_an_active_due_metadata_job(self):
        _binding, profile = self._create_group()
        profile.sudo().write(
            {
                "metadata_state": "stale",
                "next_sync_at": fields.Datetime.now() - datetime.timedelta(minutes=1),
                "queue_job_uuid": False,
            }
        )
        profile._enqueue_sync(profile.sync_revision)
        profile.invalidate_recordset()
        active_job = (
            self.env["queue.job"]
            .sudo()
            .search([("uuid", "=", profile.queue_job_uuid)], limit=1)
        )
        self.assertIn(active_job.state, ("pending", "enqueued", "started"))
        previous_revision = profile.sync_revision
        # Recovery must use the revision identity, adopt this live job and never
        # create a successor merely because the denormalized pointer was lost.
        profile.sudo().write({"queue_job_uuid": False})

        with trap_jobs() as trap:
            self.env["contact.center.group.profile"]._cron_schedule_group_metadata(
                limit=100
            )
            trap.assert_jobs_count(0)

        profile.invalidate_recordset()
        self.assertEqual(profile.sync_revision, previous_revision)
        self.assertEqual(profile.queue_job_uuid, active_job.uuid)

    def test_group_no_longer_available_uses_terminal_unavailable_state(self):
        _binding, profile = self._create_group()
        waiter = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "metadata-unavailable-waiter-%s" % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": {"fixture": "unavailable-waiter"},
                }
            )
        )
        waiter.write(
            {
                "waiting_group_profile_id": profile.id,
                "waiting_group_roster_after": fields.Datetime.now(),
                "group_roster_wait_count": 1,
                "first_group_roster_wait_at": fields.Datetime.now(),
            }
        )
        GroupMetadataTestAdapter.metadata_error = UnsupportedEventError(
            "group is no longer available"
        )

        self.assertFalse(
            self._sync(
                profile,
                self._snapshot([]),
            )
        )

        self.assertEqual(profile.metadata_state, "unavailable")
        self.assertTrue(profile.next_sync_at)
        self.assertEqual(profile.last_error_class, "UnsupportedEventError")
        waiter.invalidate_recordset(
            [
                "state",
                "waiting_group_profile_id",
                "waiting_group_roster_after",
                "last_error_class",
            ]
        )
        self.assertEqual(waiter.state, "unsupported")
        self.assertFalse(waiter.waiting_group_profile_id)
        self.assertFalse(waiter.waiting_group_roster_after)
        self.assertEqual(waiter.last_error_class, "UnsupportedEventError")

    def test_dto_contract_error_is_permanent_and_does_not_retry(self):
        _binding, profile = self._create_group()
        waiter = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "metadata-failed-waiter-%s" % uuid.uuid4(),
                    "provider_schema_version": "fixture-v1",
                    "raw_envelope_json": {"fixture": "failed-waiter"},
                }
            )
        )
        waiter.write(
            {
                "waiting_group_profile_id": profile.id,
                "waiting_group_roster_after": fields.Datetime.now(),
                "attempts": 3,
                "group_roster_wait_count": 1,
                "first_group_roster_wait_at": fields.Datetime.now(),
            }
        )
        GroupMetadataTestAdapter.metadata_error = DTOValidationError(
            "invalid provider-neutral group result"
        )

        self.assertFalse(self._sync(profile, self._snapshot([])))

        self.assertEqual(profile.metadata_state, "failed")
        self.assertEqual(profile.last_error_class, "DTOValidationError")
        waiter.invalidate_recordset(
            [
                "state",
                "attempts",
                "waiting_group_profile_id",
                "waiting_group_roster_after",
                "last_error_class",
            ]
        )
        self.assertEqual(waiter.state, "dead")
        self.assertEqual(waiter.attempts, 3)
        self.assertFalse(waiter.waiting_group_profile_id)
        self.assertFalse(waiter.waiting_group_roster_after)
        self.assertEqual(
            waiter.last_error_class,
            "GroupRosterMetadataFailedError",
        )
        terminal_evidence = waiter.metadata_json["group_roster_terminal_failure"]
        self.assertEqual(terminal_evidence["profile_id"], profile.id)
        self.assertEqual(
            terminal_evidence["profile_error_class"],
            "DTOValidationError",
        )
        self.assertEqual(
            terminal_evidence["profile_sync_revision"],
            profile.sync_revision,
        )

    def test_binding_unlink_removes_its_private_avatar_attachment(self):
        binding, profile = self._create_group()
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0l"
            "EQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        self._sync(
            profile,
            self._snapshot([]),
            avatar=AvatarResult(
                state="ready",
                content=png,
                mime_type="image/png",
                file_name="group.png",
            ),
        )
        attachment = profile.avatar_attachment_id

        binding.sudo().unlink()

        self.assertFalse(attachment.exists())

    def test_profile_unlink_removes_its_private_avatar_attachment(self):
        _binding, profile = self._create_group()
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0l"
            "EQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        self._sync(
            profile,
            self._snapshot([]),
            avatar=AvatarResult(
                state="ready",
                content=png,
                mime_type="image/png",
                file_name="group.png",
            ),
        )
        attachment = profile.avatar_attachment_id

        profile.sudo().unlink()

        self.assertFalse(attachment.exists())

    def test_channel_unlink_removes_its_private_avatar_attachment(self):
        binding, profile = self._create_group()
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0l"
            "EQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        self._sync(
            profile,
            self._snapshot([]),
            avatar=AvatarResult(
                state="ready",
                content=png,
                mime_type="image/png",
                file_name="group.png",
            ),
        )
        attachment = profile.avatar_attachment_id

        binding.channel_id.sudo().with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN,
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN,
        ).unlink()

        self.assertFalse(attachment.exists())
