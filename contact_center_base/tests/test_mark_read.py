import uuid
from unittest import mock

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.adapter import AdapterResult, ProviderAdapter, adapter_registry
from ..services.dto import EventDTO


@adapter_registry.register("test.mark.read")
class MarkReadTestAdapter(ProviderAdapter):
    display_name = "Mark Read Test"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return EventDTO.from_dict(envelope)

    def execute_command(self, connection, command):
        return AdapterResult.success(provider_response={"accepted": True})

    def prepare_request_snapshot(self, connection, command):
        return {
            "provider": self.key,
            "method": "POST",
            "endpoint": "/chat/markread",
            "payload": {
                "phone": command.target_address.value_normalized,
                "ids": command.options["external_message_ids"],
            },
        }

    def get_capabilities(self, connection):
        return {"send_message": True, "mark_read": True}

    def get_health(self, connection):
        return {"state": "connected"}


class TestMarkRead(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Read Receipt Agent",
                    "login": "cc-read-%s" % uuid.uuid4(),
                    "email": "cc-read@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, [agent_group.id])],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Read Receipt Team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Read Receipt Account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "read-account-%s" % uuid.uuid4(),
                "default_team_id": cls.team.id,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Read Receipt Connection",
                "account_id": cls.account.id,
                "adapter_key": "test.mark.read",
                "external_ref": "read-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "test-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": True,
                "capabilities_json": {"send_message": True, "mark_read": True},
                "last_state_observed_at": fields.Datetime.now(),
                "last_state_source": "health_job",
                "health_detail": "healthy",
            }
        )

    def _conversation(self, conversation_type="direct"):
        guest = self.env["mail.guest"].sudo().create({"name": "Read Guest"})
        identity = (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": "Read Guest",
                    "company_id": self.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )
        channel_identity = identity if conversation_type == "direct" else None
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            identity=channel_identity,
            conversation_type=conversation_type,
            name="Read Group" if conversation_type == "group" else None,
            team=self.team,
            partner_ids=self.agent.partner_id.ids,
            guest_ids=guest.ids,
        )
        binding = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .create(
                {
                    "channel_id": channel.id,
                    "account_id": self.account.id,
                    "identity_id": (
                        identity.id if conversation_type == "direct" else False
                    ),
                    "conversation_type": conversation_type,
                    "conversation_ref": "read-conversation-%s" % uuid.uuid4(),
                }
            )
        )
        role = "group" if conversation_type == "group" else "primary"
        suffix = "@g.us" if conversation_type == "group" else "@s.whatsapp.net"
        address = "%s%s" % (uuid.uuid4().int, suffix)
        self.env["contact.center.channel.alias"].sudo().create(
            {
                "channel_binding_id": binding.id,
                "account_id": self.account.id,
                "namespace": (
                    "whatsapp.group" if conversation_type == "group" else "whatsapp.pn"
                ),
                "value_raw": address,
                "value_normalized": address,
                "role": role,
            }
        )
        return channel, binding, guest

    def _inbound_message(self, channel, binding, guest, external_id=None):
        message = channel._contact_center_post(
            origin="inbound",
            body="Inbound read target",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_guest_id=guest.id,
            partner_ids=[],
        )
        target = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": binding.id,
                    "provider_connection_id": self.connection.id,
                    "direction": "inbound",
                    "origin": "provider",
                    "content_type": "text",
                    "external_message_id": external_id or "wamid-%s" % uuid.uuid4(),
                    "delivery_state": "delivered",
                }
            )
        )
        return message, target

    def _mark_seen(self, channel, message):
        return (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            .with_context(contact_center_skip_enqueue=True)
            .mark_seen(channel.id, message.id)
        )

    def test_disabled_policy_updates_local_pointer_without_outbox(self):
        channel, binding, guest = self._conversation()
        message, _target = self._inbound_message(channel, binding, guest)
        self.account.invalidate_recordset(["access_topology_revision"])
        access_revision = self.account.access_topology_revision

        result = self._mark_seen(channel, message)

        self.account.invalidate_recordset(["access_topology_revision"])
        self.assertEqual(result["message_id"], message.id)
        self.assertEqual(self.account.access_topology_revision, access_revision)
        self.assertFalse(
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)])
        )

    def test_enabled_policy_creates_one_idempotent_direct_command(self):
        self.account.mark_read_enabled = True
        channel, binding, guest = self._conversation()
        message, target = self._inbound_message(channel, binding, guest)
        self.account.invalidate_recordset(["access_topology_revision"])
        access_revision = self.account.access_topology_revision

        self._mark_seen(channel, message)
        self._mark_seen(channel, message)

        self.account.invalidate_recordset(["access_topology_revision"])
        outboxes = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)])
        )
        self.assertEqual(self.account.access_topology_revision, access_revision)
        self.assertEqual(len(outboxes), 1)
        self.assertEqual(outboxes.command_type, "mark_read")
        self.assertEqual(outboxes.target_message_binding_id, target)
        self.assertFalse(outboxes.message_binding_id)
        self.assertEqual(
            outboxes.command_json["options"]["external_message_ids"],
            [target.external_message_id],
        )

    def test_seen_pointer_batches_every_covered_inbound_message(self):
        self.account.mark_read_enabled = True
        channel, binding, guest = self._conversation()
        rows = [
            self._inbound_message(
                channel,
                binding,
                guest,
                external_id="batch-read-%s" % index,
            )
            for index in range(1, 4)
        ]

        self._mark_seen(channel, rows[-1][0])

        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)])
        )
        self.assertEqual(len(outbox), 1)
        self.assertEqual(
            outbox.command_json["options"]["external_message_ids"],
            ["batch-read-1", "batch-read-2", "batch-read-3"],
        )
        self.assertEqual(outbox.target_message_binding_id, rows[-1][1])

    def test_later_pointer_uses_persisted_read_watermark(self):
        self.account.mark_read_enabled = True
        channel, binding, guest = self._conversation()
        first = self._inbound_message(
            channel, binding, guest, external_id="watermark-read-1"
        )
        second = self._inbound_message(
            channel, binding, guest, external_id="watermark-read-2"
        )

        self._mark_seen(channel, first[0])
        self._mark_seen(channel, second[0])

        outboxes = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)], order="id")
        )
        self.assertEqual(len(outboxes), 2)
        self.assertEqual(
            [
                command.command_json["options"]["external_message_ids"]
                for command in outboxes
            ],
            [["watermark-read-1"], ["watermark-read-2"]],
        )

    def test_group_seen_never_creates_direct_read_command(self):
        self.account.mark_read_enabled = True
        channel, binding, guest = self._conversation(conversation_type="group")
        message, _target = self._inbound_message(channel, binding, guest)

        self._mark_seen(channel, message)

        self.assertFalse(
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)])
        )

    def test_dispatch_scope_rejects_a_tampered_external_id(self):
        self.account.mark_read_enabled = True
        channel, binding, guest = self._conversation()
        message, _target = self._inbound_message(channel, binding, guest)
        self._mark_seen(channel, message)
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)], limit=1)
        )
        command_json = dict(outbox.command_json)
        command_json["options"] = {"external_message_ids": ["tampered"]}
        outbox.command_json = command_json

        with self.assertRaises(ValidationError):
            outbox._process_one()

    def test_valid_command_dispatches_without_message_projection(self):
        self.account.mark_read_enabled = True
        channel, binding, guest = self._conversation()
        message, _target = self._inbound_message(channel, binding, guest)
        self._mark_seen(channel, message)
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)], limit=1)
        )

        outbox._process_one()

        self.assertEqual(outbox.state, "done")
        self.assertFalse(outbox.message_binding_id)
        self.assertEqual(outbox.provider_response_json, {"accepted": True})

    def test_pending_read_is_cancelled_after_primary_switch(self):
        self.account.mark_read_enabled = True
        channel, binding, guest = self._conversation()
        message, _target = self._inbound_message(channel, binding, guest)
        self._mark_seen(channel, message)
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)], limit=1)
        )
        replacement = self.env["contact.center.provider.connection"].create(
            {
                "name": "Replacement Read Connection",
                "account_id": self.account.id,
                "adapter_key": "test.mark.read",
                "external_ref": "replacement-read-%s" % uuid.uuid4(),
                "provider_schema_version": "test-v1",
                "state": "connected",
                "active": True,
                "role": "standby",
                "inbound_active": False,
                "outbound_active": False,
                "capabilities_json": {"send_message": True, "mark_read": True},
            }
        )
        replacement.action_use_as_primary()

        self.assertEqual(outbox.state, "cancelled")
        self.assertEqual(outbox.last_error_class, "SupersededReadReceipt")

    def test_policy_cancelled_read_is_revived_without_duplicate(self):
        self.account.mark_read_enabled = True
        channel, binding, guest = self._conversation()
        message, target = self._inbound_message(
            channel, binding, guest, external_id="revived-read-target"
        )
        ui = self.env["contact.center.ui.api"].with_user(self.agent)
        with trap_jobs() as first_trap:
            ui.mark_seen(channel.id, message.id)
            first_trap.assert_jobs_count(1)
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)], limit=1)
        )
        original_id = outbox.id

        self.account.mark_read_enabled = False
        outbox.invalidate_recordset()
        self.assertEqual(outbox.state, "cancelled")
        outbox.sudo().write(
            {
                "attempts": 4,
                "dispatch_job_uuid": "cancelled-dispatch",
                "dispatch_started_at": fields.Datetime.now(),
                "provider_request_json": {"stale": "request"},
                "provider_response_json": {"stale": "response"},
            }
        )
        self.account.mark_read_enabled = True

        with trap_jobs() as revived_trap:
            ui.mark_seen(channel.id, message.id)
            revived_trap.assert_jobs_count(1)

        outboxes = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)])
        )
        self.assertEqual(len(outboxes), 1)
        self.assertEqual(outboxes.id, original_id)
        self.assertEqual(outboxes.state, "pending")
        self.assertEqual(outboxes.target_message_binding_id, target)
        self.assertEqual(outboxes.attempts, 0)
        self.assertEqual(outboxes.queue_job_uuid, revived_trap.enqueued_jobs[0].uuid)
        self.assertFalse(outboxes.dispatch_job_uuid)
        self.assertFalse(outboxes.dispatch_started_at)
        self.assertFalse(outboxes.processed_at)
        self.assertFalse(outboxes.provider_request_json)
        self.assertFalse(outboxes.provider_response_json)
        self.assertFalse(outboxes.last_error_class)
        self.assertFalse(outboxes.last_error_message)

    def test_dead_read_does_not_advance_watermark_and_is_safely_revived(self):
        self.account.mark_read_enabled = True
        channel, binding, guest = self._conversation()
        message, target = self._inbound_message(
            channel, binding, guest, external_id="dead-read-target"
        )
        ui = self.env["contact.center.ui.api"].with_user(self.agent)
        with trap_jobs() as first_trap:
            ui.mark_seen(channel.id, message.id)
            first_trap.assert_jobs_count(1)
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)], limit=1)
        )
        original_id = outbox.id
        original_job_uuid = outbox.queue_job_uuid
        outbox.sudo().write(
            {
                "state": "dead",
                "attempts": 8,
                "processed_at": fields.Datetime.now(),
                "provider_request_json": {"stale": "request"},
                "provider_response_json": {"accepted": False},
                "last_error_class": "AdapterError",
                "last_error_message": "definite provider rejection",
            }
        )

        application = self.env["contact.center.application"]
        self.assertEqual(
            application._mark_read_watermark_message_id(binding, self.connection), 0
        )
        with trap_jobs() as revived_trap:
            ui.mark_seen(channel.id, message.id)
            revived_trap.assert_jobs_count(1)

        outboxes = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)])
        )
        self.assertEqual(len(outboxes), 1)
        self.assertEqual(outboxes.id, original_id)
        self.assertEqual(outboxes.state, "pending")
        self.assertEqual(outboxes.target_message_binding_id, target)
        self.assertEqual(outboxes.attempts, 0)
        self.assertNotEqual(outboxes.queue_job_uuid, original_job_uuid)
        self.assertEqual(outboxes.queue_job_uuid, revived_trap.enqueued_jobs[0].uuid)
        self.assertFalse(outboxes.processed_at)
        self.assertFalse(outboxes.provider_request_json)
        self.assertFalse(outboxes.provider_response_json)
        self.assertFalse(outboxes.last_error_class)
        self.assertFalse(outboxes.last_error_message)

    def test_terminal_gap_caps_batch_and_reuses_its_exact_snapshot(self):
        self.account.mark_read_enabled = True
        channel, binding, guest = self._conversation()
        rows = [
            self._inbound_message(
                channel,
                binding,
                guest,
                external_id="terminal-gap-read-%s" % index,
            )
            for index in range(1, 201)
        ]

        self._mark_seen(channel, rows[49][0])
        self._mark_seen(channel, rows[99][0])
        self._mark_seen(channel, rows[199][0])
        outboxes = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)], order="id")
        )
        self.assertEqual(len(outboxes), 3)
        first_batch, terminal_batch, later_batch = outboxes
        terminal_original_id = terminal_batch.id
        terminal_original_key = terminal_batch.outbox_idempotency_key
        terminal_original_payload = terminal_batch.command_json
        first_batch.write({"state": "done"})
        terminal_batch.write(
            {
                "state": "dead",
                "attempts": 8,
                "processed_at": fields.Datetime.now(),
                "last_error_class": "AdapterError",
                "last_error_message": "definite provider rejection",
            }
        )
        later_batch.write({"state": "done"})

        application = self.env["contact.center.application"]
        self.assertEqual(
            application._mark_read_progress_message_ids(binding, self.connection),
            (rows[49][0].id, rows[99][0].id, terminal_original_id),
        )

        self._mark_seen(channel, rows[199][0])

        outboxes = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)], order="id")
        )
        self.assertEqual(len(outboxes), 3)
        revived = outboxes.filtered(lambda command: command.id == terminal_original_id)
        self.assertEqual(revived.state, "pending")
        self.assertEqual(revived.outbox_idempotency_key, terminal_original_key)
        self.assertEqual(revived.command_json, terminal_original_payload)
        self.assertEqual(revived.target_message_binding_id, rows[99][1])
        self.assertEqual(later_batch.state, "done")
        self.assertEqual(
            application._mark_read_watermark_message_id(binding, self.connection),
            rows[199][0].id,
        )

    def test_changed_terminal_snapshot_does_not_create_a_partial_batch(self):
        self.account.mark_read_enabled = True
        channel, binding, guest = self._conversation()
        rows = [
            self._inbound_message(
                channel,
                binding,
                guest,
                external_id="changed-terminal-read-%s" % index,
            )
            for index in range(1, 6)
        ]
        self._mark_seen(channel, rows[0][0])
        self._mark_seen(channel, rows[3][0])
        self._mark_seen(channel, rows[4][0])
        outboxes = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)], order="id")
        )
        self.assertEqual(len(outboxes), 3)
        first_batch, terminal_batch, later_batch = outboxes
        first_batch.write({"state": "done"})
        terminal_batch.write(
            {
                "state": "dead",
                "processed_at": fields.Datetime.now(),
                "last_error_class": "AdapterError",
                "last_error_message": "definite provider rejection",
            }
        )
        later_batch.write({"state": "done"})
        rows[2][1].write({"message_state": "deleted"})

        self._mark_seen(channel, rows[4][0])

        outboxes = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("channel_binding_id", "=", binding.id)], order="id")
        )
        self.assertEqual(len(outboxes), 3)
        self.assertEqual(terminal_batch.state, "dead")
        self.assertEqual(
            self.env["contact.center.application"]._mark_read_watermark_message_id(
                binding, self.connection
            ),
            rows[0][0].id,
        )

    def test_seen_locks_conversation_before_native_member_pointer(self):
        self.account.mark_read_enabled = True
        channel, binding, guest = self._conversation()
        message, _target = self._inbound_message(channel, binding, guest)
        member = channel.sudo().channel_member_ids.filtered(
            lambda item: item.partner_id == self.agent.partner_id
        )
        self.assertEqual(len(member), 1)
        observations = []
        binding_model_class = type(self.env["contact.center.channel.binding"])
        original_lock = binding_model_class._contact_center_lock_channel_then_binding

        def observed_lock(records):
            member.invalidate_recordset(["seen_message_id"])
            observations.append(member.seen_message_id.id)
            return original_lock(records)

        with mock.patch.object(
            binding_model_class,
            "_contact_center_lock_channel_then_binding",
            new=observed_lock,
        ):
            self._mark_seen(channel, message)

        self.assertGreaterEqual(len(observations), 2)
        self.assertFalse(observations[0])
        self.assertEqual(observations[1], message.id)

    def test_connection_is_selected_after_conversation_and_topology_locks(self):
        self.account.mark_read_enabled = True
        channel, binding, guest = self._conversation()
        message, _target = self._inbound_message(channel, binding, guest)
        order = []
        binding_model_class = type(self.env["contact.center.channel.binding"])
        connection_model_class = type(self.env["contact.center.provider.connection"])
        application_model_class = type(self.env["contact.center.application"])
        original_binding_lock = (
            binding_model_class._contact_center_lock_channel_then_binding
        )
        original_topology_lock = (
            connection_model_class._contact_center_lock_operational_admission
        )
        original_connection_selection = application_model_class._mark_read_connection

        def observed_binding_lock(records):
            order.append("conversation")
            return original_binding_lock(records)

        def observed_topology_lock(records, account_ids):
            order.append("topology")
            return original_topology_lock(records, account_ids)

        def observed_connection_selection(records, locked_binding):
            order.append("connection")
            self.assertIn("topology", order)
            return original_connection_selection(records, locked_binding)

        with mock.patch.object(
            binding_model_class,
            "_contact_center_lock_channel_then_binding",
            new=observed_binding_lock,
        ), mock.patch.object(
            connection_model_class,
            "_contact_center_lock_operational_admission",
            new=observed_topology_lock,
        ), mock.patch.object(
            application_model_class,
            "_mark_read_connection",
            new=observed_connection_selection,
        ):
            (
                self.env["contact.center.application"]
                .with_context(contact_center_skip_enqueue=True)
                ._queue_direct_mark_read(channel, message)
            )

        self.assertEqual(order, ["topology", "conversation", "connection"])
