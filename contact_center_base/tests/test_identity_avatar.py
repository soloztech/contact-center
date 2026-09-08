import base64
import uuid
from unittest.mock import patch

from psycopg2 import OperationalError

from odoo import fields
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.adapter import (
    AdapterResult,
    ProviderAdapter,
    TransientAdapterError,
    adapter_registry,
)
from ..services.dto import (
    AvatarResult,
    DTOValidationError,
    EventDTO,
    IdentityProfileResult,
)


@adapter_registry.register("test.identity.avatar")
class IdentityAvatarTestAdapter(ProviderAdapter):
    display_name = "Identity Avatar Test"
    avatar = AvatarResult(state="unavailable")
    observed_address = None
    provider_read_override = None
    observed_read_purpose = None

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

    def fetch_identity_profile(self, connection, address):
        self.__class__.observed_address = address
        return IdentityProfileResult(display_name="", avatar=self.__class__.avatar)

    def supports_identity_profile(self, connection):
        return True

    def is_provider_read_ready(self, connection, purpose):
        self.__class__.observed_read_purpose = purpose
        if self.__class__.provider_read_override is not None:
            return self.__class__.provider_read_override
        return super().is_provider_read_ready(connection, purpose)


class TestContactCenterIdentityAvatar(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Identity Avatar Agent",
                    "login": "cc-avatar-%s" % uuid.uuid4(),
                    "email": "cc-avatar-%s@example.invalid" % uuid.uuid4(),
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, agent_group.ids)],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Identity Avatar Team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Identity Avatar Account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "avatar-account-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, cls.team.ids)],
            }
        )
        now = fields.Datetime.now()
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Identity Avatar Provider",
                "account_id": cls.account.id,
                "adapter_key": "test.identity.avatar",
                "external_ref": "avatar-connection-%s" % uuid.uuid4(),
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
        cls.remote_jid = "15550001111@s.whatsapp.net"

    def setUp(self):
        super().setUp()
        IdentityAvatarTestAdapter.avatar = AvatarResult(state="unavailable")
        IdentityAvatarTestAdapter.observed_address = None
        IdentityAvatarTestAdapter.provider_read_override = None
        IdentityAvatarTestAdapter.observed_read_purpose = None

    def _event(self, *, jid=None, event_type="message.created", message=True):
        jid = jid or self.remote_jid
        values = {
            "provider_schema_version": "fixture-v1",
            "event_id": "identity-avatar-event-%s" % uuid.uuid4(),
            "event_type": event_type,
            "occurred_at": "2026-08-24T18:00:00Z",
            "account_ref": self.account.external_ref,
            "connection_ref": self.connection.external_ref,
            "conversation_ref": jid,
            "platform": "whatsapp",
            "direction": "inbound",
            "is_from_me": False,
            "origin": "provider",
            "actor": {
                "display_name": "Remote Person",
                "addresses": [
                    {
                        "namespace": "whatsapp.pn",
                        "value": jid,
                        "value_normalized": jid,
                        "role": "primary",
                        "source_field": "fixture.JID",
                        "confidence": "protocol",
                    }
                ],
            },
            "conversation": {
                "conversation_type": "direct",
                "addresses": [
                    {
                        "namespace": "whatsapp.pn",
                        "value": jid,
                        "value_normalized": jid,
                        "role": "primary",
                        "source_field": "fixture.JID",
                        "confidence": "protocol",
                    }
                ],
            },
            "extensions": {"identity_avatar_hint": {"kind": "picture"}},
        }
        if message:
            values["message"] = {
                "external_message_id": "identity-avatar-message-%s" % uuid.uuid4(),
                "content_type": "text",
                "text": "Creates the direct conversation",
            }
        return EventDTO.from_dict(values)

    def _create_direct_binding(self):
        self.env["contact.center.application"].with_context(
            contact_center_skip_enqueue=True
        )._process_event(self.connection, self._event())
        binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    ("conversation_ref", "=", self.remote_jid),
                    ("conversation_type", "=", "direct"),
                ],
                limit=1,
            )
        )
        self.assertTrue(binding)
        return binding

    def _sync(self, binding, avatar):
        IdentityAvatarTestAdapter.avatar = avatar
        result = self._run_job(binding)
        binding.invalidate_recordset()
        return result

    def _run_job(self, binding, expected_revision=None, job_uuid=None):
        expected_revision = (
            binding.direct_avatar_sync_revision
            if expected_revision is None
            else expected_revision
        )
        job_uuid = job_uuid or str(uuid.uuid4())
        binding.sudo().write({"direct_avatar_queue_job_uuid": job_uuid})
        return binding.with_context(job_uuid=job_uuid)._job_sync_identity_avatar(
            expected_revision
        )

    def test_first_direct_message_schedules_avatar_without_provider_io(self):
        with trap_jobs() as trap:
            self.env["contact.center.application"]._process_event(
                self.connection, self._event()
            )
            trap.assert_jobs_count(1)
            job = trap.enqueued_jobs[0]
            binding = self.env["contact.center.channel.binding"].search(
                [("conversation_ref", "=", self.remote_jid)], limit=1
            )
            self.assertEqual(job.channel, "root.contact_center.identity_avatar")
            self.assertEqual(job.priority, 60)
            self.assertEqual(
                job.identity_key,
                "contact_center:identity_avatar:%s:1" % binding.id,
            )
        self.assertIsNone(IdentityAvatarTestAdapter.observed_address)

    def test_identity_avatar_worker_requires_exact_persisted_owner(self):
        binding = self._create_direct_binding()
        current_uuid = str(uuid.uuid4())
        stale_uuid = str(uuid.uuid4())

        with patch.object(
            IdentityAvatarTestAdapter, "fetch_identity_profile", autospec=True
        ) as fetch_profile:
            self.assertFalse(
                binding.with_context(job_uuid=current_uuid)._job_sync_identity_avatar(
                    binding.direct_avatar_sync_revision
                )
            )
            binding.sudo().write({"direct_avatar_queue_job_uuid": stale_uuid})
            self.assertFalse(
                binding.with_context(job_uuid=current_uuid)._job_sync_identity_avatar(
                    binding.direct_avatar_sync_revision
                )
            )

        fetch_profile.assert_not_called()
        self.assertFalse(binding.direct_avatar_last_synced_at)

    def test_identity_avatar_active_detection_adopts_canonical_job(self):
        binding = self._create_direct_binding()
        binding._enqueue_identity_avatar_sync(binding.direct_avatar_sync_revision)
        binding.invalidate_recordset(["direct_avatar_queue_job_uuid"])
        expected_uuid = binding.direct_avatar_queue_job_uuid
        binding.sudo().write({"direct_avatar_queue_job_uuid": False})

        self.assertTrue(binding._direct_avatar_has_active_job())
        binding.invalidate_recordset(["direct_avatar_queue_job_uuid"])
        self.assertEqual(binding.direct_avatar_queue_job_uuid, expected_uuid)

    def test_avatar_refresh_adopts_live_revision_job_after_pointer_loss(self):
        binding = self._create_direct_binding()
        binding._enqueue_identity_avatar_sync(binding.direct_avatar_sync_revision)
        binding.invalidate_recordset(
            ["direct_avatar_sync_revision", "direct_avatar_queue_job_uuid"]
        )
        expected_revision = binding.direct_avatar_sync_revision
        expected_uuid = binding.direct_avatar_queue_job_uuid
        binding.sudo().write({"direct_avatar_queue_job_uuid": False})

        with trap_jobs() as trap:
            binding._request_identity_avatar_sync(self.connection)
            trap.assert_jobs_count(0)

        binding.invalidate_recordset(
            ["direct_avatar_sync_revision", "direct_avatar_queue_job_uuid"]
        )
        self.assertEqual(binding.direct_avatar_sync_revision, expected_revision)
        self.assertEqual(binding.direct_avatar_queue_job_uuid, expected_uuid)

    def test_stale_avatar_job_never_replaces_the_current_revision_job(self):
        binding = self._create_direct_binding()
        old_revision = binding.direct_avatar_sync_revision
        old_uuid = str(uuid.uuid4())
        binding.sudo().write({"direct_avatar_queue_job_uuid": old_uuid})
        binding._request_identity_avatar_sync(self.connection, force=True)
        binding.invalidate_recordset(
            ["direct_avatar_sync_revision", "direct_avatar_queue_job_uuid"]
        )
        successor_uuid = binding.direct_avatar_queue_job_uuid
        self.assertEqual(binding.direct_avatar_sync_revision, old_revision + 1)
        self.assertNotEqual(successor_uuid, old_uuid)

        with patch.object(
            IdentityAvatarTestAdapter, "fetch_identity_profile", autospec=True
        ) as fetch_profile:
            self.assertFalse(
                binding.with_context(job_uuid=old_uuid)._job_sync_identity_avatar(
                    old_revision
                )
            )

        binding.invalidate_recordset(["direct_avatar_queue_job_uuid"])
        fetch_profile.assert_not_called()
        self.assertEqual(binding.direct_avatar_queue_job_uuid, successor_uuid)

    def test_ready_and_absent_avatar_use_private_local_projection(self):
        binding = self._create_direct_binding()
        jpeg = b"\xff\xd8\xff\xe0" + b"identity-avatar-jpeg-fixture"
        ready = AvatarResult(
            state="ready",
            provider_revision="avatar-v1",
            content=jpeg,
            mime_type="image/jpeg",
            file_name="profile.jpg",
        )

        self.assertTrue(self._sync(binding, ready))

        self.assertEqual(binding.direct_avatar_state, "ready")
        self.assertEqual(binding.direct_avatar_attachment_id.sudo().raw, jpeg)
        self.assertFalse(binding.direct_avatar_attachment_id.public)
        self.assertEqual(
            IdentityAvatarTestAdapter.observed_address.value_normalized,
            self.remote_jid,
        )
        payload = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._serialize_conversation(binding.channel_id.with_user(self.agent))
        )
        self.assertTrue(
            payload["identity"]["avatar_url"].startswith(
                "/contact_center/conversation/%s/avatar?v=" % binding.channel_id.id
            )
        )
        self.assertNotIn("wuzapi", payload["identity"]["avatar_url"])

        old_attachment = binding.direct_avatar_attachment_id
        binding.with_context(
            contact_center_skip_enqueue=True
        )._request_identity_avatar_sync(self.connection, force=True)
        self.assertTrue(
            self._sync(
                binding,
                AvatarResult(state="absent", provider_revision="avatar-v2"),
            )
        )
        self.assertEqual(binding.direct_avatar_state, "absent")
        self.assertFalse(binding.direct_avatar_attachment_id)
        self.assertFalse(old_attachment.exists())

    def test_picture_hint_never_creates_unknown_identity_or_conversation(self):
        self._create_direct_binding()
        identity_count = self.env["contact.center.identity"].sudo().search_count([])
        binding_count = (
            self.env["contact.center.channel.binding"].sudo().search_count([])
        )
        guest_count = self.env["mail.guest"].sudo().search_count([])
        unknown = "15550002222@s.whatsapp.net"

        result = (
            self.env["contact.center.application"]
            .with_context(contact_center_skip_enqueue=True)
            ._process_event(
                self.connection,
                self._event(
                    jid=unknown,
                    event_type="identity.avatar.changed",
                    message=False,
                ),
            )
        )

        self.assertEqual(result, self.connection)
        self.assertEqual(
            self.env["contact.center.identity"].sudo().search_count([]),
            identity_count,
        )
        self.assertEqual(
            self.env["contact.center.channel.binding"].sudo().search_count([]),
            binding_count,
        )
        self.assertEqual(self.env["mail.guest"].sudo().search_count([]), guest_count)

    def test_existing_picture_hint_only_invalidates_the_matching_binding(self):
        binding = self._create_direct_binding()
        revision = binding.direct_avatar_sync_revision
        message_count = self.env["mail.message"].sudo().search_count([])

        result = (
            self.env["contact.center.application"]
            .with_context(contact_center_skip_enqueue=True)
            ._process_event(
                self.connection,
                self._event(
                    event_type="identity.avatar.changed",
                    message=False,
                ),
            )
        )

        binding.invalidate_recordset()
        self.assertEqual(result, binding)
        self.assertEqual(binding.direct_avatar_sync_revision, revision + 1)
        self.assertEqual(
            self.env["mail.message"].sudo().search_count([]), message_count
        )

    def test_picture_hint_locks_channel_binding_before_enriching_aliases(self):
        binding = self._create_direct_binding()
        hint = self._event(event_type="identity.avatar.changed", message=False)
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

        binding_type = type(binding)
        with patch.object(
            binding_type,
            "_contact_center_lock_identity_channel_binding",
            autospec=True,
            side_effect=TransientAdapterError("identity changed"),
        ), self.assertRaises(TransientAdapterError):
            self.env["contact.center.application"]._process_event(
                self.connection,
                hint,
            )

    def test_avatar_result_is_binary_safe_for_direct_and_group_profiles(self):
        from ..services.dto import AvatarResult

        content = base64.b64decode("/9j/4GlkZW50aXR5LWF2YXRhcg==")
        avatar = AvatarResult(state="ready", content=content, mime_type="image/jpeg")
        self.assertIsInstance(avatar, AvatarResult)
        self.assertNotIn("content", avatar.to_dict())

    def test_identity_profile_result_is_bounded_typed_and_binary_safe(self):
        content = base64.b64decode("/9j/4GlkZW50aXR5LWF2YXRhcg==")
        avatar = AvatarResult(
            state="ready",
            content=content,
            mime_type="image/jpeg",
        )
        profile = IdentityProfileResult(display_name="Remote Person", avatar=avatar)

        self.assertEqual(profile.display_name, "Remote Person")
        self.assertEqual(profile.avatar, avatar)
        self.assertNotIn("content", profile.to_dict()["avatar"])
        for display_name in (None, "x" * 256, "Remote\nPerson"):
            with self.subTest(display_name=display_name), self.assertRaises(
                DTOValidationError
            ):
                IdentityProfileResult(display_name=display_name, avatar=avatar)
        with self.assertRaises(DTOValidationError):
            IdentityProfileResult(display_name="Remote Person", avatar={})

    def test_adapter_uses_unified_identity_profile_contract(self):
        binding = self._create_direct_binding()
        adapter = self.connection.get_adapter()
        avatar = AvatarResult(state="absent", provider_revision="profile-v1")
        IdentityAvatarTestAdapter.avatar = avatar

        profile = adapter.fetch_identity_profile(
            self.connection,
            binding._direct_avatar_address(),
        )

        self.assertEqual(profile, IdentityProfileResult(display_name="", avatar=avatar))
        self.assertEqual(
            IdentityAvatarTestAdapter.observed_address.value_normalized,
            self.remote_jid,
        )
        self.assertTrue(adapter.supports_identity_profile(self.connection))

        original_alias = binding.identity_id.alias_ids.filtered(
            lambda alias: alias.value_normalized == self.remote_jid
        )
        original_alias.write(
            {
                "first_seen_at": "2026-08-20 10:00:00",
                "last_seen_at": "2026-08-20 10:00:00",
            }
        )
        latest_jid = "15550003333@s.whatsapp.net"
        self.env["contact.center.identity.alias"].sudo().create(
            {
                "identity_id": binding.identity_id.id,
                "account_id": self.account.id,
                "namespace": "whatsapp.pn",
                "value_raw": "15550002222@s.whatsapp.net",
                "value_normalized": "15550002222@s.whatsapp.net",
                "role": "primary",
                "confidence": "protocol",
                "first_seen_at": "2026-08-21 10:00:00",
                "last_seen_at": "2026-08-21 10:00:00",
            }
        )
        self.env["contact.center.identity.alias"].sudo().create(
            {
                "identity_id": binding.identity_id.id,
                "account_id": self.account.id,
                "namespace": "whatsapp.pn",
                "value_raw": latest_jid,
                "value_normalized": latest_jid,
                "role": "alternate",
                "confidence": "protocol",
                "first_seen_at": "2026-08-22 10:00:00",
                "last_seen_at": "2026-08-22 10:00:00",
            }
        )

        adapter.fetch_identity_profile(
            self.connection,
            binding._direct_avatar_address(),
        )

        self.assertEqual(
            IdentityAvatarTestAdapter.observed_address.value_normalized,
            latest_jid,
        )

    def test_profile_job_projects_name_and_avatar_in_one_provider_read(self):
        binding = self._create_direct_binding()
        identity = binding.identity_id
        profile = IdentityProfileResult(
            display_name="Fetched Profile Name",
            avatar=AvatarResult(state="absent", provider_revision="profile-v1"),
        )

        with patch.object(
            IdentityAvatarTestAdapter,
            "fetch_identity_profile",
            autospec=True,
            return_value=profile,
        ) as fetch_profile:
            self.assertTrue(self._run_job(binding))

        identity.invalidate_recordset()
        binding.invalidate_recordset()
        self.assertEqual(fetch_profile.call_count, 1)
        self.assertEqual(identity.observed_name, "Fetched Profile Name")
        self.assertEqual(identity.name, "Fetched Profile Name")
        self.assertEqual(identity.mail_guest_id.name, "Fetched Profile Name")
        self.assertEqual(binding.direct_avatar_state, "absent")

    def test_database_operational_error_escapes_for_queue_job_retry(self):
        binding = self._create_direct_binding()
        database_error = OperationalError("synthetic serialization failure")

        with patch.object(
            type(binding),
            "_run_identity_avatar_sync",
            autospec=True,
            side_effect=database_error,
        ), patch.object(
            type(binding), "_handle_identity_avatar_sync_error", autospec=True
        ) as handler:
            with self.assertRaises(OperationalError) as raised:
                self._run_job(binding)

        self.assertIs(raised.exception, database_error)
        handler.assert_not_called()

    def test_profile_job_records_but_does_not_project_over_manual_or_partner_name(self):
        manual_binding = self._create_direct_binding()
        manual_identity = manual_binding.identity_id
        manual_identity.sudo().write({"name": "Manual Choice"})
        manual_profile = IdentityProfileResult(
            display_name="Provider Manual Replacement",
            avatar=AvatarResult(state="unavailable"),
        )
        with patch.object(
            IdentityAvatarTestAdapter,
            "fetch_identity_profile",
            autospec=True,
            return_value=manual_profile,
        ):
            self.assertTrue(self._run_job(manual_binding))
        manual_identity.invalidate_recordset()
        self.assertEqual(manual_identity.name_source, "manual")
        self.assertEqual(manual_identity.name, "Manual Choice")
        self.assertEqual(manual_identity.observed_name, "Provider Manual Replacement")

        partner_jid = "15550003333@s.whatsapp.net"
        self.env["contact.center.application"].with_context(
            contact_center_skip_enqueue=True
        )._process_event(self.connection, self._event(jid=partner_jid))
        partner_binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    ("conversation_ref", "=", partner_jid),
                ],
                limit=1,
            )
        )
        partner_identity = partner_binding.identity_id
        partner = self.env["res.partner"].create({"name": "Canonical Partner"})
        partner_identity.with_user(self.agent).action_link_partner(partner.id)
        partner_name = partner_identity.name
        partner_profile = IdentityProfileResult(
            display_name="Provider Partner Replacement",
            avatar=AvatarResult(state="unavailable"),
        )
        with patch.object(
            IdentityAvatarTestAdapter,
            "fetch_identity_profile",
            autospec=True,
            return_value=partner_profile,
        ):
            self.assertTrue(self._run_job(partner_binding))
        partner_identity.invalidate_recordset()
        partner.invalidate_recordset()
        self.assertEqual(partner_identity.partner_id, partner)
        self.assertEqual(partner.name, "Canonical Partner")
        self.assertEqual(partner_identity.name, partner_name)
        self.assertEqual(
            partner_identity.observed_name,
            "Provider Partner Replacement",
        )

    def test_provider_read_readiness_default_and_degraded_override(self):
        binding = self._create_direct_binding()
        adapter = self.connection.get_adapter()
        self.assertTrue(binding._direct_avatar_provider_available(self.connection))
        self.assertEqual(
            IdentityAvatarTestAdapter.observed_read_purpose,
            "identity_profile",
        )

        self.connection.sudo().write({"state": "degraded"})
        self.assertFalse(binding._direct_avatar_provider_available(self.connection))
        self.assertFalse(
            ProviderAdapter.is_provider_read_ready(
                adapter,
                self.connection,
                "identity_profile",
            )
        )

        IdentityAvatarTestAdapter.provider_read_override = True
        profile = IdentityProfileResult(
            display_name="Degraded Read Name",
            avatar=AvatarResult(state="unavailable"),
        )
        with patch.object(
            IdentityAvatarTestAdapter,
            "fetch_identity_profile",
            autospec=True,
            return_value=profile,
        ):
            self.assertTrue(self._run_job(binding))
        binding.identity_id.invalidate_recordset()
        self.assertEqual(binding.identity_id.name, "Degraded Read Name")
