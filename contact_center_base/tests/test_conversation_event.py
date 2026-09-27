import threading
import uuid
from contextlib import contextmanager
from unittest import mock

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.queue_job.exception import RetryableJobError

from ..services.dto import EventDTO
from ..services.tokens import (
    CONTACT_CENTER_DELETION_TOKEN,
    CONTACT_CENTER_MEMBERSHIP_TOKEN,
    CONTACT_CENTER_POST_TOKEN,
)
from .attendance_common import DAY, AttendanceCase, at


def _event_values(event):
    return {
        "sequence": event.sequence,
        "event_type": event.event_type,
        "responsible": event.responsible_id,
        "previous_responsible": event.previous_responsible_id,
        "state": event.state,
        "previous_state": event.previous_state,
        "actor": event.actor_id,
        "source": event.source,
    }


class TestConversationEvent(AttendanceCase):
    """Criterion 1: each writer records exactly the dimensions it changed."""

    def assertEvent(self, event, **expected):
        values = _event_values(event)
        for name, value in expected.items():
            self.assertEqual(values[name], value, "%s of %s" % (name, event))

    def new_conversation(self, sent_at=None, **message):
        self.tick(9, 0, 0)
        binding = self.inbound(self.remote(), sent_at or at(8, 59, 58), **message)
        return self.channel_of(binding), binding

    # Creation ------------------------------------------------------------------

    def test_inbound_creation_records_created_without_responsible(self):
        channel, binding = self.new_conversation()

        events = self.events(channel)
        self.assertEqual(len(events), 1)
        self.assertEvent(
            events,
            sequence=1,
            event_type="created",
            responsible=self.env["res.users"],
            previous_responsible=self.env["res.users"],
            state="open",
            actor=self.env["res.users"],
            source="creation",
        )
        self.assertEqual(events.account_id, self.account)
        self.assertEqual(events.company_id, self.env.company)
        self.assertEqual(events.occurred_at, at(9))
        self.assertEqual(self.counter(channel), 1)
        self.assertEqual(binding.lifecycle_seq, 1)
        # L09-15: the message whose processing created the conversation carries
        # the creation as causal evidence.
        self.assertEqual(binding.caused_assignment_event_id, events)

    def test_inbound_creation_with_default_assignee_needs_no_assignment(self):
        self.account.auto_assignment_user_id = self.agent_b
        channel, binding = self.new_conversation()

        events = self.events(channel)
        self.assertEqual(events.mapped("event_type"), ["created"])
        self.assertEqual(events.responsible_id, self.agent_b)
        self.assertEqual(binding.lifecycle_seq, 1)
        self.assertEqual(binding.caused_assignment_event_id, events)

    def test_outbound_first_creation_records_the_agent(self):
        self.tick(9)
        result = self.api(self.agent_a).start_conversation(
            self.account.id, "(11) 99876-5432"
        )
        channel = self.env["mail.channel"].browse(result["channel_id"])

        events = self.events(channel)
        self.assertEqual(len(events), 1)
        self.assertEvent(
            events,
            sequence=1,
            event_type="created",
            state="open",
            actor=self.agent_a,
            source="creation",
        )
        self.assertEqual(events.account_id, self.account)
        self.tick(9, 5)
        sent = self.send(channel)
        self.assertEqual(sent.lifecycle_seq, 1)

    def test_inbound_processing_never_records_its_technical_user(self):
        """Whatever user runs the job, inbound processing has no human actor."""

        self.account.auto_assignment_user_id = self.agent_b
        self.tick(9)
        event = self._event(self.remote(), at(8, 59))
        message = (
            self.application.with_user(self.admin)
            .with_context(contact_center_skip_enqueue=True)
            ._process_event(self.connection, event)
        )
        channel = self.channel_of(self._binding_of(message))
        self.assertEqual(self.events(channel).mapped("event_type"), ["created"])
        self.assertFalse(self.events(channel).actor_id)
        self.api(self.supervisor).update_conversation(
            channel.id, {"responsible_id": False}
        )
        self.tick(9, 1)
        self.application.with_user(self.admin).with_context(
            contact_center_skip_enqueue=True
        )._process_event(
            self.connection, self._event(self._remote_of(channel), at(9, 1))
        )
        assigned = self.events(channel)[-1]
        self.assertEqual(
            (assigned.event_type, assigned.source, assigned.actor_id),
            ("assigned", "automatic_inbound", self.env["res.users"]),
        )

    def test_phone_first_creation_is_automatic(self):
        remote = self.remote()
        self.tick(9)
        # A known identity routes the reply but has no conversation yet.
        self.inbound(remote, at(8, 58))
        channel = self.env["mail.channel"].search(
            [("contact_center_binding_ids.conversation_ref", "=", remote)]
        )
        self.assertEqual(self.events(channel).mapped("source"), ["creation"])
        phone = self.phone(channel, at(9, 1))
        self.assertEqual(phone.origin, "external_device")
        self.assertEqual(phone.lifecycle_seq, 1)
        self.assertFalse(phone.caused_assignment_event_id)

    def test_group_creation_records_created(self):
        account = self._account("Grupos", group_inbound_enabled=True)
        connection = self._connection(account)
        connection.write(
            {
                "last_state_observed_at": fields.Datetime.now(),
                "last_state_source": "health_job",
                "health_detail": "healthy",
            }
        )
        group_ref = "1203630%s@g.us" % (uuid.uuid4().int % 10**9)
        sender = "700000%s@lid" % (uuid.uuid4().int % 10**9)
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "attendance-group-%s" % uuid.uuid4(),
                "event_type": "message.created",
                "occurred_at": "%sT09:00:00Z" % DAY,
                "account_ref": account.external_ref,
                "connection_ref": connection.external_ref,
                "conversation_ref": group_ref,
                "platform": "whatsapp",
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {
                    "display_name": "Participante",
                    "addresses": [
                        {
                            "namespace": "whatsapp.lid",
                            "value": sender,
                            "value_normalized": sender,
                            "role": "sender",
                            "source_field": "Info.Sender",
                            "confidence": "protocol",
                        }
                    ],
                },
                "conversation": {
                    "conversation_type": "group",
                    "addresses": [
                        {
                            "namespace": "whatsapp.group",
                            "value": group_ref,
                            "value_normalized": group_ref,
                            "role": "group",
                            "source_field": "Info.Chat",
                            "confidence": "protocol",
                        }
                    ],
                },
                "extensions": {"conversation_name": "Grupo de clientes"},
                "message": {
                    "external_message_id": "attendance-group-message-%s" % uuid.uuid4(),
                    "content_type": "text",
                    "text": "Mensagem no grupo",
                },
            }
        )
        self.tick(9)
        message = self.application.with_context(
            contact_center_skip_enqueue=True
        )._process_event(connection, event)
        binding = self._binding_of(message)
        channel = self.channel_of(binding)

        self.assertEqual(binding.channel_binding_id.conversation_type, "group")
        self.assertEqual(self.events(channel).mapped("event_type"), ["created"])
        self.assertEqual(self.events(channel).account_id, account)
        self.assertEqual(binding.lifecycle_seq, 1)

    # Assignment ----------------------------------------------------------------

    def test_auto_assignment_is_evidenced_only_on_its_own_message(self):
        channel, first = self.new_conversation()
        self.account.auto_assignment_user_id = self.agent_b
        self.tick(9, 5)
        second = self.inbound(self._remote_of(channel), at(9, 4, 59))
        self.tick(9, 6)
        third = self.inbound(self._remote_of(channel), at(9, 5, 59))

        events = self.events(channel)
        self.assertEqual(events.mapped("event_type"), ["created", "assigned"])
        assigned = events[1]
        self.assertEvent(
            assigned,
            sequence=2,
            responsible=self.agent_b,
            previous_responsible=self.env["res.users"],
            state="open",
            previous_state="open",
            actor=self.env["res.users"],
            source="automatic_inbound",
        )
        self.assertEqual(first.caused_assignment_event_id, events[0])
        self.assertEqual(second.lifecycle_seq, 2)
        self.assertEqual(second.caused_assignment_event_id, assigned)
        self.assertEqual(third.lifecycle_seq, 2)
        self.assertFalse(third.caused_assignment_event_id)

    def test_only_the_creating_message_carries_the_creation(self):
        """An agent-started conversation gives no evidence to a later message."""

        self.account.auto_assignment_user_id = self.agent_b
        self.tick(10)
        result = self.api(self.agent_a).start_conversation(
            self.account.id, "(11) 99876-5432"
        )
        channel = self.env["mail.channel"].browse(result["channel_id"])
        self.assertEqual(self.events(channel).responsible_id, self.agent_b)
        self.tick(11)
        late = self.inbound(self._remote_of(channel), at(9))
        self.assertEqual(late.lifecycle_seq, 1)
        self.assertFalse(late.caused_assignment_event_id)

        created_by_message = self.inbound(self.remote(), at(10, 59))
        self.assertEqual(
            created_by_message.caused_assignment_event_id,
            self.events(self.channel_of(created_by_message)),
        )

    def _remote_of(self, channel):
        return self._channel_binding(channel).conversation_ref

    def test_claim_transfer_and_removal(self):
        channel, _binding = self.new_conversation()
        self.tick(9, 1)
        self.claim(channel, self.agent_a)
        self.tick(9, 2)
        self.assign(channel, self.agent_b)
        self.tick(9, 3)
        self.assign(channel, False)

        events = self.events(channel)[1:]
        self.assertEqual(
            [_event_values(event) for event in events],
            [
                {
                    "sequence": 2,
                    "event_type": "assigned",
                    "responsible": self.agent_a,
                    "previous_responsible": self.env["res.users"],
                    "state": "open",
                    "previous_state": "open",
                    "actor": self.agent_a,
                    "source": "manual",
                },
                {
                    "sequence": 3,
                    "event_type": "assigned",
                    "responsible": self.agent_b,
                    "previous_responsible": self.agent_a,
                    "state": "open",
                    "previous_state": "open",
                    "actor": self.supervisor,
                    "source": "manual",
                },
                {
                    "sequence": 4,
                    "event_type": "unassigned",
                    "responsible": self.env["res.users"],
                    "previous_responsible": self.agent_b,
                    "state": "open",
                    "previous_state": "open",
                    "actor": self.supervisor,
                    "source": "manual",
                },
            ],
        )
        self.assertEqual(events.mapped("occurred_at"), [at(9, 1), at(9, 2), at(9, 3)])
        self.assertEqual(self.counter(channel), 4)

    def test_access_removal_unassigns_with_its_own_source(self):
        channel, _binding = self.new_conversation()
        self.tick(9, 1)
        self.claim(channel, self.agent_a)
        self.tick(9, 2)
        self.account.with_user(self.admin).write(
            {"access_user_ids": [(3, self.agent_a.id)]}
        )

        removal = self.events(channel)[-1]
        self.assertEvent(
            removal,
            sequence=3,
            event_type="unassigned",
            responsible=self.env["res.users"],
            previous_responsible=self.agent_a,
            actor=self.admin,
            source="access_change",
        )

    # State ---------------------------------------------------------------------

    def test_resolve_reopen_archive_unarchive(self):
        channel, _binding = self.new_conversation()
        self.tick(9, 1)
        self.claim(channel, self.agent_a)
        self.tick(9, 2)
        self.set_state(channel, "resolved")
        self.tick(9, 3)
        self.set_state(channel, "open")
        self.tick(9, 4)
        self.set_state(channel, "archived")
        self.tick(9, 5)
        self.set_state(channel, "open")
        self.tick(9, 6)
        self.set_state(channel, "resolved")
        self.tick(9, 7)
        reopening = self.inbound(self._remote_of(channel), at(9, 6, 30))

        events = self.events(channel)[2:]
        self.assertEqual(
            [
                (
                    event.sequence,
                    event.event_type,
                    event.previous_state,
                    event.state,
                    event.responsible_id,
                    event.actor_id,
                    event.source,
                )
                for event in events
            ],
            [
                (
                    3,
                    "resolved",
                    "open",
                    "resolved",
                    self.agent_a,
                    self.agent_a,
                    "manual",
                ),
                (
                    4,
                    "reopened",
                    "resolved",
                    "open",
                    self.agent_a,
                    self.agent_a,
                    "manual",
                ),
                (
                    5,
                    "archived",
                    "open",
                    "archived",
                    self.agent_a,
                    self.agent_a,
                    "manual",
                ),
                (
                    6,
                    "unarchived",
                    "archived",
                    "open",
                    self.agent_a,
                    self.agent_a,
                    "manual",
                ),
                (
                    7,
                    "resolved",
                    "open",
                    "resolved",
                    self.agent_a,
                    self.agent_a,
                    "manual",
                ),
                (
                    8,
                    "reopened",
                    "resolved",
                    "open",
                    self.agent_a,
                    self.env["res.users"],
                    "automatic_inbound",
                ),
            ],
        )
        self.assertEqual(reopening.lifecycle_seq, 8)

    def test_claim_and_resolve_records_assignment_then_state(self):
        channel, _binding = self.new_conversation()
        catalog = self.api(self.agent_a).resolution_reason_catalog(channel.id)
        self.tick(9, 10)
        self.api(self.agent_a).resolve_conversation(
            channel.id,
            self.reason.id,
            "Cliente atendido",
            str(uuid.uuid4()),
            catalog["revision"],
        )

        events = self.events(channel)[1:]
        self.assertEqual(
            [
                (e.sequence, e.event_type, e.responsible_id, e.state, e.actor_id)
                for e in events
            ],
            [
                (2, "assigned", self.agent_a, "open", self.agent_a),
                (3, "resolved", self.agent_a, "resolved", self.agent_a),
            ],
        )

    def test_one_write_changing_both_dimensions_records_two_events(self):
        channel, _binding = self.new_conversation()
        self.tick(9, 1)
        self.claim(channel, self.agent_a)
        self.tick(9, 2)
        self.api(self.supervisor).update_conversation(
            channel.id, {"responsible_id": self.agent_b.id, "state": "resolved"}
        )

        events = self.events(channel)[2:]
        self.assertEqual(
            [
                (
                    e.sequence,
                    e.event_type,
                    e.previous_responsible_id,
                    e.responsible_id,
                    e.previous_state,
                    e.state,
                    e.occurred_at,
                )
                for e in events
            ],
            [
                (3, "assigned", self.agent_a, self.agent_b, "open", "open", at(9, 2)),
                (
                    4,
                    "resolved",
                    self.agent_b,
                    self.agent_b,
                    "open",
                    "resolved",
                    at(9, 2),
                ),
            ],
        )
        self.assertEqual(self.counter(channel), 4)

    def test_writes_without_change_record_nothing(self):
        channel, _binding = self.new_conversation()
        self.tick(9, 1)
        self.claim(channel, self.agent_a)
        before = self.events(channel)
        self.claim(channel, self.agent_a)
        self.set_state(channel, "open")
        self.api(self.supervisor).update_conversation(
            channel.id, {"responsible_id": self.agent_a.id, "tag_ids": []}
        )
        channel.sudo().with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write(
            {
                "contact_center_state": "open",
                "contact_center_responsible_id": self.agent_a.id,
            }
        )
        self.assertEqual(self.events(channel), before)
        self.assertEqual(self.counter(channel), 2)

    def test_clock_stepping_back_never_backdates_an_event(self):
        channel, _binding = self.new_conversation()
        self.tick(8, 30)
        self.claim(channel, self.agent_a)
        self.assertEqual(self.events(channel)[-1].occurred_at, at(9))

    # Criterion 2: immutable and scoped -----------------------------------------

    def test_events_are_immutable_and_recorded_only_by_the_service(self):
        channel, _binding = self.new_conversation()
        event = self.events(channel)
        with self.assertRaises(AccessError):
            event.write({"event_type": "resolved"})
        with self.assertRaises(AccessError):
            event.unlink()
        with self.assertRaises(AccessError):
            self.event_model.sudo().create(
                {
                    "company_id": self.env.company.id,
                    "channel_id": channel.id,
                    "sequence": 99,
                    "event_type": "resolved",
                    "source": "manual",
                    "occurred_at": at(9),
                }
            )
        with self.assertRaises(AccessError):
            channel.sudo().with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
            ).write({"contact_center_lifecycle_seq": 7})
        binding = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("channel_binding_id.channel_id", "=", channel.id)])
        )
        with self.assertRaises(AccessError):
            binding.write({"lifecycle_seq": 5})
        with self.assertRaises(AccessError):
            binding.write({"caused_assignment_event_id": event.id})
        # A string sent through RPC context cannot choose the recorded source.
        self.tick(9, 1)
        self.api(self.agent_a).with_context(
            contact_center_lifecycle_source="automatic_inbound"
        ).claim_conversation(channel.id)
        self.assertEqual(
            (self.events(channel)[-1].source, self.events(channel)[-1].actor_id),
            ("manual", self.agent_a),
        )

    def test_event_reading_follows_company_and_membership(self):
        channel, _binding = self.new_conversation()
        event = self.events(channel)
        with self.assertRaises(AccessError):
            event.with_user(self.agent_a).read(["event_type"])
        self.assertEqual(
            self.event_model.with_user(self.supervisor).search(
                [("channel_id", "=", channel.id)]
            ),
            event,
        )
        self.assertFalse(
            self.event_model.with_user(self.outsider).search(
                [("channel_id", "=", channel.id)]
            )
        )
        self.assertEqual(
            self.event_model.with_user(self.admin).search(
                [("channel_id", "=", channel.id)]
            ),
            event,
        )
        other_company = self.env["res.company"].create({"name": "Outra empresa"})
        foreign_admin = self._user("Foreign", "group_contact_center_admin")
        foreign_admin.write(
            {
                "company_ids": [(6, 0, other_company.ids)],
                "company_id": other_company.id,
            }
        )
        self.assertFalse(
            self.event_model.with_user(foreign_admin)
            .with_context(allowed_company_ids=other_company.ids)
            .search([("channel_id", "=", channel.id)])
        )

    # Criterion 1b: every binding creator copies the counter under the lock ----

    def test_every_message_creator_copies_the_counter_under_the_channel_lock(self):
        binding_class = type(self.env["contact.center.channel.binding"])
        message_class = type(self.env["contact.center.message.binding"])
        original_lock = binding_class._contact_center_lock_channel_then_binding
        original_position = message_class._contact_center_lifecycle_position
        locked = set()
        observed = []

        def lock(record):
            result = original_lock(record)
            locked.add(record.channel_id.id)
            return result

        def position(model, channel_binding_id):
            channel_id = (
                model.env["contact.center.channel.binding"]
                .sudo()
                .browse(channel_binding_id)
                .channel_id.id
            )
            observed.append(channel_id in locked)
            return original_position(model, channel_binding_id)

        channel, _binding = self.new_conversation()
        remote = self._remote_of(channel)
        self.tick(9, 1)
        self.claim(channel, self.agent_a)
        with mock.patch.object(
            binding_class, "_contact_center_lock_channel_then_binding", lock
        ), mock.patch.object(
            message_class, "_contact_center_lifecycle_position", position
        ):
            creators = {}
            locked.clear()
            creators["inbound"] = self.inbound(remote, at(9, 2))
            locked.clear()
            creators["agent"] = self.send(channel)
            locked.clear()
            creators["automation"] = self.automation(channel)
            locked.clear()
            creators["phone"] = self.phone(channel, at(9, 3))
            locked.clear()
            self.tick(9, 4)
            call = self.application.with_context(
                contact_center_skip_enqueue=True
            )._process_event(
                self.connection,
                EventDTO.from_dict(
                    dict(
                        self._event(remote, at(9, 4)).to_dict(),
                        event_type="conversation.call.updated",
                        message=None,
                        extensions={
                            "call": {
                                "ref": uuid.uuid4().hex * 2,
                                "state": "offered",
                                "direction": "inbound",
                            }
                        },
                    )
                ),
            )
            creators["control"] = self._binding_of(call)
            outbox = (
                self.env["contact.center.outbox.command"]
                .sudo()
                .search([("message_binding_id", "=", creators["agent"].id)])
            )
            outbox._finish_failure("dead", ValidationError("Rejected by provider"))
            locked.clear()
            retried = (
                self.api(self.agent_a)
                .with_context(contact_center_skip_enqueue=True)
                .resend_message(
                    channel.id, creators["agent"].message_id.id, str(uuid.uuid4())
                )
            )
            creators["resend"] = self._binding_of(
                self.env["mail.message"].browse(retried["message_id"])
            )
            group_binding = self._group_message_binding()
            creators["group"] = group_binding

        self.assertEqual(observed, [True] * len(creators))
        self.assertEqual(creators["control"].content_type, "call.offer")
        for name in ("inbound", "agent", "automation", "phone", "control", "resend"):
            self.assertEqual(creators[name].lifecycle_seq, 2, name)
        self.assertEqual(creators["group"].lifecycle_seq, 1)

    def _group_message_binding(self):
        account = self._account("Grupos", group_inbound_enabled=True)
        connection = self._connection(account)
        group_ref = "1203631%s@g.us" % (uuid.uuid4().int % 10**9)
        sender = "700001%s@lid" % (uuid.uuid4().int % 10**9)
        address = {
            "namespace": "whatsapp.group",
            "value": group_ref,
            "value_normalized": group_ref,
            "role": "group",
            "source_field": "Info.Chat",
            "confidence": "protocol",
        }
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "attendance-group-%s" % uuid.uuid4(),
                "event_type": "message.created",
                "occurred_at": "%sT09:05:00Z" % DAY,
                "account_ref": account.external_ref,
                "connection_ref": connection.external_ref,
                "conversation_ref": group_ref,
                "platform": "whatsapp",
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {
                    "display_name": "Participante",
                    "addresses": [
                        {
                            "namespace": "whatsapp.lid",
                            "value": sender,
                            "value_normalized": sender,
                            "role": "sender",
                            "source_field": "Info.Sender",
                            "confidence": "protocol",
                        }
                    ],
                },
                "conversation": {"conversation_type": "group", "addresses": [address]},
                "extensions": {"conversation_name": "Grupo"},
                "message": {
                    "external_message_id": "attendance-group-message-%s" % uuid.uuid4(),
                    "content_type": "text",
                    "text": "Mensagem no grupo",
                },
            }
        )
        message = self.application.with_context(
            contact_center_skip_enqueue=True
        )._process_event(connection, event)
        return self._binding_of(message)

    def test_counter_is_read_after_the_transitions_of_this_transaction(self):
        channel, binding = self.new_conversation()
        message = channel.sudo()._contact_center_post(
            origin="inbound",
            body="Registrada logo após a transição",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        self.tick(9, 1)
        channel.sudo().with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write(
            {
                "contact_center_state": "resolved",
                "contact_center_responsible_id": self.agent_a.id,
            }
        )
        created = (
            self.env["contact.center.message.binding"]
            .sudo()
            .create(
                {
                    "message_id": message.id,
                    "channel_binding_id": binding.channel_binding_id.id,
                    "direction": "inbound",
                    "origin": "provider",
                }
            )
        )
        self.assertEqual(created.lifecycle_seq, 3)

    def test_reopening_message_carries_the_reopening_sequence(self):
        channel, _binding = self.new_conversation()
        self.tick(9, 1)
        self.claim(channel, self.agent_a)
        self.tick(9, 2)
        self.set_state(channel, "resolved")
        self.tick(9, 3)
        message = self.inbound(self._remote_of(channel), at(9, 1, 30))

        reopened = self.events(channel)[-1]
        self.assertEqual(reopened.event_type, "reopened")
        self.assertEqual(message.lifecycle_seq, reopened.sequence)
        self.assertEqual(self.counter(channel), reopened.sequence)

    def test_failed_reopening_note_rolls_back_message_event_and_counter(self):
        channel, _binding = self.new_conversation()
        self.tick(9, 1)
        self.claim(channel, self.agent_a)
        self.tick(9, 2)
        self.set_state(channel, "resolved")
        events_before = self.events(channel)
        counter_before = self.counter(channel)
        messages_before = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search_count([("channel_binding_id.channel_id", "=", channel.id)])
        )
        ui_api = type(self.env["contact.center.ui.api"])
        self.tick(9, 3)
        with mock.patch.object(
            ui_api,
            "_persist_internal_note",
            side_effect=ValidationError("note storage unavailable"),
        ), self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                self.inbound(self._remote_of(channel), at(9, 2, 30))

        self.assertEqual(self.events(channel), events_before)
        self.assertEqual(self.counter(channel), counter_before)
        channel.invalidate_recordset(["contact_center_state"])
        self.assertEqual(channel.contact_center_state, "resolved")
        self.assertEqual(
            self.env["contact.center.message.binding"]
            .sudo()
            .search_count([("channel_binding_id.channel_id", "=", channel.id)]),
            messages_before,
        )

    def test_causal_evidence_must_be_the_assignment_of_the_message(self):
        channel, binding = self.new_conversation()
        created = self.events(channel)
        message = channel.sudo()._contact_center_post(
            origin="inbound",
            body="Evidência forjada",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        values = {
            "message_id": message.id,
            "channel_binding_id": binding.channel_binding_id.id,
            "direction": "inbound",
            "origin": "provider",
            "caused_assignment_event_id": created.id,
        }
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["contact.center.message.binding"].sudo().create(values)

        # Neither can the creation by an agent be claimed by a first message.
        self.tick(10)
        started = self.env["mail.channel"].browse(
            self.api(self.agent_a).start_conversation(
                self.account.id, "(11) 99876-1111"
            )["channel_id"]
        )
        started_binding = self._channel_binding(started)
        message = started.sudo()._contact_center_post(
            origin="inbound",
            body="Primeira mensagem",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["contact.center.message.binding"].sudo().create(
                {
                    "message_id": message.id,
                    "channel_binding_id": started_binding.id,
                    "direction": "inbound",
                    "origin": "provider",
                    "caused_assignment_event_id": self.events(started).id,
                }
            )
        # Nor another conversation's creation.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["contact.center.message.binding"].sudo().create(
                {
                    "message_id": message.id,
                    "channel_binding_id": started_binding.id,
                    "direction": "inbound",
                    "origin": "provider",
                    "caused_assignment_event_id": created.id,
                }
            )

    def test_phone_evidence_only_on_the_first_message(self):
        """The phone-first creation is evidence only for its own first binding."""

        self.tick(9)
        opening = self.phone_first(self.remote(), at(8, 59))
        channel = self.channel_of(opening)
        created = self.events(channel)
        self.assertEqual(opening.caused_assignment_event_id, created)
        self.tick(9, 1)
        later = self.phone(channel, at(9, 1))
        self.assertFalse(later.caused_assignment_event_id)
        message = channel.sudo()._contact_center_post(
            origin="external_device",
            body="Evidência forjada",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            partner_ids=[],
        )
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["contact.center.message.binding"].sudo().create(
                {
                    "message_id": message.id,
                    "channel_binding_id": opening.channel_binding_id.id,
                    "direction": "outbound",
                    "origin": "external_device",
                    "caused_assignment_event_id": created.id,
                }
            )

    def test_ledger_searches_handle_empty_values(self):
        """L09-LEDGER-02: is set / is not set and empty users in lists."""

        channel, _binding = self.new_conversation()
        self.tick(9, 1)
        self.claim(channel, self.agent_a)
        self.tick(9, 2)
        self.assign(channel, False)
        events = self.events(channel)
        created, assigned, unassigned = events
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT responsible_ref FROM contact_center_conversation_event "
            "WHERE id = %s",
            [unassigned.id],
        )
        self.assertEqual(self.env.cr.fetchone()[0], 0)  # the ORM stores 0
        model = self.event_model.with_user(self.supervisor)
        scope = [("channel_id", "=", channel.id)]
        for domain, expected in (
            ([("platform", "!=", False)], events),
            ([("platform", "=", False)], self.event_model),
            ([("platform", "in", ["whatsapp", False])], events),
            ([("platform", "not in", ["telegram", False])], events),
            ([("conversation_type", "!=", False)], events),
            ([("conversation_type", "=", False)], self.event_model),
            ([("conversation_type", "in", ["group", False])], self.event_model),
            ([("conversation_type", "not in", ["group"])], events),
            ([("responsible_id", "=", False)], created | unassigned),
            ([("responsible_id", "!=", False)], assigned),
            ([("responsible_id", "in", [False, self.agent_a.id])], events),
            ([("responsible_id", "in", [False])], created | unassigned),
            ([("responsible_id", "not in", [False, self.agent_b.id])], assigned),
            ([("responsible_id", "not in", [self.agent_a.id])], created | unassigned),
            ([("previous_responsible_id", "in", [self.agent_a.id])], unassigned),
        ):
            self.assertEqual(model.search(scope + domain), expected, domain)

    def test_ledger_searches_keep_operator_semantics(self):
        """L09-I2-02: negation, case, wildcards and empty lists as stored fields."""

        channel, _binding = self.new_conversation()
        self.tick(9, 1)
        self.claim(channel, self.agent_a)
        bindingless = (
            self.env["mail.channel"]
            .sudo()
            .with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
            )
            .create(
                {
                    "name": "Conversa sem vínculo",
                    "channel_type": "contact_center",
                    "contact_center_company_id": self.env.company.id,
                    "contact_center_state": "open",
                }
            )
        )
        # A conversation from before the ledger: its baseline has NULL users.
        self.env.flush_all()
        self.env.cr.execute(
            "DELETE FROM contact_center_conversation_event WHERE channel_id = %s",
            [bindingless.id],
        )
        self.env.cr.execute(
            "UPDATE mail_channel SET contact_center_lifecycle_seq = 0 WHERE id = %s",
            [bindingless.id],
        )
        self.env.invalidate_all()
        self.event_model._contact_center_record_migration_baselines()
        events = self.events(channel)
        created, assigned = events
        loose = self.events(bindingless)
        self.assertEqual(loose.mapped("event_type"), ["baseline"])
        self.env.cr.execute(
            "SELECT responsible_ref FROM contact_center_conversation_event "
            "WHERE id = %s",
            [loose.id],
        )
        self.assertIsNone(self.env.cr.fetchone()[0])
        empty = self.event_model
        model = self.event_model.sudo()
        scope = [("channel_id", "in", (channel | bindingless).ids)]
        inbox = self.account.name
        for domain, expected in (
            ([("platform", "ilike", "WHATS")], events),
            ([("platform", "like", "WHATS")], empty),
            ([("platform", "like", "whats")], events),
            ([("platform", "not ilike", "whatsapp")], loose),
            ([("platform", "not like", "WHATS")], events | loose),
            ([("platform", "=like", "whats%")], events),
            ([("platform", "=like", "whats")], empty),
            ([("platform", "=ilike", "WHATSAPP")], events),
            ([("platform", "!=", "whatsapp")], loose),
            ([("platform", "in", [])], empty),
            ([("platform", "not in", [])], events | loose),
            ([("conversation_type", "not ilike", "dir")], loose),
            ([("conversation_type", "in", ["direct"])], events),
            ([("account_id", "ilike", inbox)], events),
            ([("account_id", "not ilike", inbox)], loose),
            ([("account_id", "=", inbox)], events),
            ([("account_id", "=", False)], loose),
            ([("account_id", "in", [])], empty),
            ([("account_id", "not in", [])], events | loose),
            ([("account_id", "not in", self.account.ids)], loose),
            ([("responsible_id", "ilike", self.agent_a.name)], assigned),
            ([("responsible_id", "not ilike", self.agent_a.name)], created | loose),
            ([("responsible_id", "=", self.agent_a.name)], assigned),
            ([("responsible_id", "=", False)], created | loose),
        ):
            self.assertEqual(model.search(scope + domain), expected, domain)
        # Anything else is refused instead of being given another meaning.
        for domain in (
            [("platform", ">", "a")],
            [("platform", "in", [1])],
            [("account_id", "child_of", self.account.id)],
            [("responsible_id", "child_of", self.agent_a.id)],
            [("responsible_id", "in", [1.5])],
        ):
            with self.assertRaises(UserError, msg=str(domain)):
                model.search(scope + domain)

    # Criterion 4b: migration baseline ------------------------------------------

    def _as_legacy(self, channel):
        """Simulate a conversation that existed before the history was published."""

        self.env.flush_all()
        self.env.cr.execute(
            "DELETE FROM contact_center_conversation_event WHERE channel_id = %s",
            [channel.id],
        )
        self.env.cr.execute(
            "UPDATE mail_channel SET contact_center_lifecycle_seq = 0 WHERE id = %s",
            [channel.id],
        )
        self.env.cr.execute(
            "UPDATE contact_center_message_binding SET lifecycle_seq = NULL "
            "WHERE channel_binding_id IN (SELECT id FROM contact_center_channel_binding "
            "WHERE channel_id = %s)",
            [channel.id],
        )
        self.env.invalidate_all()

    def test_migration_records_one_baseline_and_is_idempotent(self):
        legacy, _binding = self.new_conversation()
        self.tick(9, 1)
        self.claim(legacy, self.agent_a)
        self.tick(9, 2)
        self.set_state(legacy, "resolved")
        self._as_legacy(legacy)
        self.tick(10)
        recent = self.channel_of(self.inbound(self.remote(), at(9, 59)))

        self.tick(11)
        migrated = self.event_model._contact_center_record_migration_baselines()
        self.assertIn(legacy.id, migrated)
        self.assertNotIn(recent.id, migrated)
        baseline = self.events(legacy)
        self.assertEqual(len(baseline), 1)
        self.assertEvent(
            baseline,
            sequence=1,
            event_type="baseline",
            responsible=self.agent_a,
            state="resolved",
            actor=self.env["res.users"],
            source="migration",
        )
        self.assertEqual(baseline.occurred_at, at(11))
        self.assertEqual(baseline.account_id, self.account)
        self.assertEqual(self.counter(legacy), 1)
        self.assertEqual(self.events(recent).mapped("event_type"), ["created"])

        self.tick(11, 30)
        self.assertEqual(
            self.event_model._contact_center_record_migration_baselines(), []
        )
        self.assertEqual(self.events(legacy), baseline)
        self.assertEqual(self.counter(legacy), 1)
        self.assertEqual(self.counter(recent), 1)

        sent = self.send(legacy)
        self.assertEqual(sent.lifecycle_seq, 1)
        self.tick(11, 40)
        self.set_state(legacy, "open")
        self.assertEqual(self.events(legacy)[-1].sequence, 2)
        self.assertEqual(self.events(legacy)[-1].event_type, "reopened")

    def test_migration_never_lowers_a_counter(self):
        legacy, _binding = self.new_conversation()
        self._as_legacy(legacy)
        self.env.cr.execute(
            "UPDATE mail_channel SET contact_center_lifecycle_seq = 3 WHERE id = %s",
            [legacy.id],
        )
        self.env.invalidate_all()
        self.tick(11)
        self.event_model._contact_center_record_migration_baselines()
        self.assertEqual(self.events(legacy).mapped("sequence"), [1])
        self.assertEqual(self.counter(legacy), 3)
        self.tick(11, 1)
        self.claim(legacy, self.agent_a)
        self.assertEqual(self.events(legacy).mapped("sequence"), [1, 4])

    def test_migration_script_runs_the_baseline(self):
        from odoo.modules.migration import load_script

        module = load_script(
            "contact_center_base/migrations/16.0.1.11.0/post-migrate.py",
            "post_migrate_l09",
        )
        legacy, _binding = self.new_conversation()
        self._as_legacy(legacy)
        module.migrate(self.env.cr, "16.0.1.10.0")
        self.assertEqual(self.events(legacy).mapped("event_type"), ["baseline"])
        module.migrate(self.env.cr, None)
        self.assertEqual(len(self.events(legacy)), 1)


@tagged("-at_install", "post_install")
class TestConversationEventConcurrency(TransactionCase):
    """Criterion 1c: a resolution and an inbound message race on real cursors."""

    WORKER_TIMEOUT_SECONDS = 20

    def _setup(self, token, claim=True):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            agent = (
                env["res.users"]
                .with_context(no_reset_password=True)
                .create(
                    {
                        "name": "Lifecycle Race %s" % token,
                        "login": "cc-lifecycle-race-%s" % token,
                        "company_id": env.company.id,
                        "company_ids": [(6, 0, env.company.ids)],
                        "groups_id": [
                            (
                                6,
                                0,
                                env.ref(
                                    "contact_center_base.group_contact_center_agent"
                                ).ids,
                            )
                        ],
                    }
                )
            )
            account = env["contact.center.account"].create(
                {
                    "name": "Lifecycle Race %s" % token,
                    "company_id": env.company.id,
                    "platform": "telegram",
                    "external_ref": "lifecycle-race-%s" % token,
                    "access_user_ids": [(6, 0, agent.ids)],
                }
            )
            connection = env["contact.center.provider.connection"].create(
                {
                    "name": "Lifecycle Race %s" % token,
                    "account_id": account.id,
                    "adapter_key": "test.fake",
                    "external_ref": "lifecycle-race-connection-%s" % token,
                    "provider_schema_version": "fixture-v1",
                    "state": "connected",
                    "active": True,
                    "role": "primary",
                    "inbound_active": True,
                    "outbound_active": False,
                }
            )
            remote = "race-remote-%s" % token
            inbox_model = (
                env["contact.center.inbox.event"]
                .sudo()
                .with_context(contact_center_skip_enqueue=True)
            )
            first = inbox_model.create(self._inbox_values(connection, remote, "first"))
            first.write({"queue_job_uuid": str(uuid.uuid4())})
            first.with_context(job_uuid=first.queue_job_uuid)._job_process()
            channel = (
                env["contact.center.channel.binding"]
                .search([("account_id", "=", account.id)])
                .channel_id
            )
            if claim:
                env["contact.center.ui.api"].with_user(agent).claim_conversation(
                    channel.id
                )
            reason = env["contact.center.resolution.reason"].create(
                {"name": "Lifecycle Race %s" % token, "company_id": env.company.id}
            )
            second = inbox_model.create(
                self._inbox_values(connection, remote, "second")
            )
            second.write({"queue_job_uuid": str(uuid.uuid4())})
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "agent_id": agent.id,
                "account_id": account.id,
                "connection_id": connection.id,
                "channel_id": channel.id,
                "inbox_id": second.id,
                "reason_id": reason.id,
                "token": token,
            }

    def _inbox_values(self, connection, remote, key):
        address = {
            "namespace": "telegram.user",
            "value": remote,
            "value_normalized": remote,
            "role": "primary",
        }
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "race-%s-%s" % (remote, key),
                "event_type": "message.created",
                "occurred_at": "2026-09-28T10:00:00Z",
                "account_ref": connection.account_id.external_ref,
                "connection_ref": connection.external_ref,
                "conversation_ref": remote,
                "platform": "telegram",
                "direction": "inbound",
                "is_from_me": False,
                "origin": "provider",
                "actor": {"display_name": "Race", "addresses": [address]},
                "conversation": {"conversation_type": "direct", "addresses": [address]},
                "message": {
                    "external_message_id": "race-message-%s-%s" % (remote, key),
                    "content_type": "text",
                    "text": "Race %s" % key,
                },
            }
        )
        return {
            "provider_connection_id": connection.id,
            "inbox_dedupe_key": event.event_id,
            "provider_schema_version": "fixture-v1",
            "raw_envelope_json": event.to_dict(),
        }

    def _cleanup(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            account = env["contact.center.account"].browse(fixture["account_id"])
            inboxes = (
                env["contact.center.inbox.event"]
                .sudo()
                .search([("account_id", "=", account.id)])
            )
            env["queue.job"].sudo().search(
                [
                    (
                        "uuid",
                        "in",
                        [uuid for uuid in inboxes.mapped("queue_job_uuid") if uuid],
                    )
                ]
            ).unlink()
            bindings = (
                env["contact.center.channel.binding"]
                .sudo()
                .search([("account_id", "=", account.id)])
            )
            channels = bindings.channel_id
            env["contact.center.message.binding"].sudo().search(
                [("account_id", "=", account.id)]
            ).unlink()
            inboxes.unlink()
            env["contact.center.internal.note.request"].sudo().search(
                [("channel_id", "in", channels.ids)]
            ).with_context(
                contact_center_deletion_token=CONTACT_CENTER_DELETION_TOKEN
            ).unlink()
            env["mail.message"].sudo().search(
                [("model", "=", "mail.channel"), ("res_id", "in", channels.ids)]
            ).with_context(contact_center_post_token=CONTACT_CENTER_POST_TOKEN).unlink()
            identities = bindings.identity_id
            guests = identities.mail_guest_id
            bindings.unlink()
            channels.with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN,
                contact_center_post_token=CONTACT_CENTER_POST_TOKEN,
            ).unlink()
            identities.alias_ids.unlink()
            identities.unlink()
            guests.with_context(
                contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
            ).unlink()
            env["contact.center.provider.connection"].browse(
                fixture["connection_id"]
            ).unlink()
            account.unlink()
            env["contact.center.resolution.reason"].browse(
                fixture["reason_id"]
            ).unlink()
            user = env["res.users"].browse(fixture["agent_id"])
            partner = user.partner_id
            user.unlink()
            partner.unlink()
            cr.commit()  # pylint: disable=invalid-commit

    def _resolve(self, cr, fixture):
        env = api.Environment(cr, fixture["agent_id"], {})
        env["contact.center.ui.api"].update_conversation(
            fixture["channel_id"], {"state": "resolved"}
        )
        env.flush_all()

    def _inbound(self, cr, fixture):
        env = api.Environment(cr, SUPERUSER_ID, {})
        inbox = env["contact.center.inbox.event"].sudo().browse(fixture["inbox_id"])
        inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
        env.flush_all()

    def _retry(self, operation, fixture):
        with self.registry.cursor() as cr:
            operation(cr, fixture)
            cr.commit()  # pylint: disable=invalid-commit

    @contextmanager
    def _fixture(self, **kwargs):
        fixture = self._setup(uuid.uuid4().hex, **kwargs)
        try:
            yield fixture
        finally:
            self._cleanup(fixture)

    def _race(self, fixture, first, second):
        """Take both snapshots, commit ``first``, then run ``second``."""

        snapshots = threading.Barrier(2, timeout=self.WORKER_TIMEOUT_SECONDS)
        first_done = threading.Event()
        outcome = {}

        def run(name, operation, is_first):
            try:
                with self.registry.cursor() as cr:
                    cr.execute("SET LOCAL lock_timeout = '5s'")
                    cr.execute("SELECT 1")  # the REPEATABLE READ snapshot
                    snapshots.wait()
                    if not is_first and not first_done.wait(
                        self.WORKER_TIMEOUT_SECONDS
                    ):
                        raise RuntimeError("the first transaction did not finish")
                    operation(cr, fixture)
                    cr.commit()  # pylint: disable=invalid-commit
                outcome[name] = "done"
            except RetryableJobError as error:
                outcome[name] = type(error.__cause__).__name__
            except SerializationFailure as error:
                outcome[name] = type(error).__name__
            except Exception as error:  # surface in the main thread
                outcome[name] = error
            finally:
                if is_first:
                    first_done.set()

        threads = [
            threading.Thread(target=run, args=("first", first, True), daemon=True),
            threading.Thread(target=run, args=("second", second, False), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(self.WORKER_TIMEOUT_SECONDS)
        self.assertFalse([thread.name for thread in threads if thread.is_alive()])
        for value in outcome.values():
            if isinstance(value, Exception):
                raise value
        return outcome

    def _state(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            events = env["contact.center.conversation.event"].search(
                [("channel_id", "=", fixture["channel_id"])], order="sequence"
            )
            message = env["contact.center.message.binding"].search(
                [
                    ("channel_binding_id.channel_id", "=", fixture["channel_id"]),
                    ("external_message_id", "like", "%-second"),
                ]
            )
            placement = env["contact.center.attendance.message"].search(
                [("message_binding_id", "=", message.id)]
            )
            return {
                "events": [(e.sequence, e.event_type) for e in events],
                "message_seq": message.lifecycle_seq,
                "cycle": placement.cycle_sequence,
                "counter": env["mail.channel"]
                .browse(fixture["channel_id"])
                .contact_center_lifecycle_seq,
            }

    def test_resolution_committed_first_makes_the_message_reopen(self):
        with self._fixture() as fixture:
            outcome = self._race(fixture, self._resolve, self._inbound)
            self.assertEqual(
                outcome, {"first": "done", "second": "SerializationFailure"}
            )
            self._retry(self._inbound, fixture)
            state = self._state(fixture)
            self.assertEqual(
                state["events"],
                [(1, "created"), (2, "assigned"), (3, "resolved"), (4, "reopened")],
            )
            self.assertEqual(state["message_seq"], 4)
            self.assertEqual(state["cycle"], 4)
            self.assertEqual(state["counter"], 4)

    def test_message_committed_first_stays_in_the_resolved_cycle(self):
        with self._fixture() as fixture:
            outcome = self._race(fixture, self._inbound, self._resolve)
            self.assertEqual(
                outcome, {"first": "done", "second": "SerializationFailure"}
            )
            self._retry(self._resolve, fixture)
            state = self._state(fixture)
            self.assertEqual(
                state["events"], [(1, "created"), (2, "assigned"), (3, "resolved")]
            )
            self.assertEqual(state["message_seq"], 2)
            self.assertEqual(state["cycle"], 1)
            self.assertEqual(state["counter"], 3)

    def test_manual_transitions_never_wait_for_an_inbound_inbox_lock(self):
        """L09-EV-01: the ledger takes no lock on the inbox row.

        Inbound processing holds its inbox FOR UPDATE during the whole job and
        only then locks the conversation. A claim and a resolution lock the
        conversation first; their events must not need the inbox row, or they
        would queue behind (and deadlock with) every inbound job of the inbox.
        """

        with self._fixture(claim=False) as fixture:
            blocker = self.registry.cursor()
            try:
                blocker.execute(
                    "SELECT id FROM contact_center_account WHERE id = %s FOR UPDATE",
                    [fixture["account_id"]],
                )
                with self.registry.cursor() as cr:
                    cr.execute("SET LOCAL lock_timeout = '2s'")
                    api_env = api.Environment(cr, fixture["agent_id"], {})
                    ui_api = api_env["contact.center.ui.api"]
                    ui_api.claim_conversation(fixture["channel_id"])
                    revision = ui_api.resolution_reason_catalog(fixture["channel_id"])[
                        "revision"
                    ]
                    ui_api.resolve_conversation(
                        fixture["channel_id"],
                        fixture["reason_id"],
                        "Atendido",
                        str(uuid.uuid4()),
                        revision,
                    )
                    api_env.flush_all()
                    cr.commit()  # pylint: disable=invalid-commit
            finally:
                blocker.rollback()
                blocker.close()
            self.assertEqual(
                self._state(fixture)["events"],
                [(1, "created"), (2, "assigned"), (3, "resolved")],
            )

    def test_unlocked_writer_racing_a_transition_gets_a_retryable_failure(self):
        """L09-EV-02: an access-change style write never hits a duplicate position."""

        with self._fixture() as fixture:
            with self.registry.cursor() as cr:
                cr.execute("SELECT 1")  # snapshot before the concurrent commit
                self._retry(self._resolve, fixture)
                env = api.Environment(cr, SUPERUSER_ID, {})
                channel = env["mail.channel"].browse(fixture["channel_id"])
                with self.assertRaises(SerializationFailure):
                    channel.sudo().with_context(
                        contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
                    ).write({"contact_center_responsible_id": False})
                cr.rollback()

            def unassign(cr, fixture):
                api.Environment(cr, SUPERUSER_ID, {})["mail.channel"].browse(
                    fixture["channel_id"]
                ).sudo().with_context(
                    contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
                ).write(
                    {"contact_center_responsible_id": False}
                )

            self._retry(unassign, fixture)
            self.assertEqual(
                self._state(fixture)["events"],
                [(1, "created"), (2, "assigned"), (3, "resolved"), (4, "unassigned")],
            )
