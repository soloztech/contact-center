import datetime
import uuid

from odoo import fields
from odoo.exceptions import AccessError, ValidationError

from ..services.dto import EventDTO
from .attendance_common import DAY, AttendanceCase, at


class TestAttendanceEpisode(AttendanceCase):
    """Criteria 3 to 4: waiting episodes, their cycles and their responsible."""

    def open_conversation(self, opened_at=None, sent_at=None, **message):
        """A new conversation whose first customer message starts an episode."""

        opened_at = opened_at or at(10)
        self.clock[0] = opened_at
        binding = self.inbound(self.remote(), sent_at or opened_at, **message)
        return self.channel_of(binding), binding

    def remote_of(self, channel):
        return self._channel_binding(channel).conversation_ref

    def customer(self, channel, sent_at, processed_at=None, **message):
        if processed_at:
            self.clock[0] = processed_at
        return self.inbound(self.remote_of(channel), sent_at, **message)

    def summary(self, channel):
        """Episodes as (start, outcome, SQL wait, order); ``None`` = not measured."""

        episodes = self.episodes(channel)
        self.env.cr.execute(
            "SELECT id, wait_seconds FROM contact_center_attendance_episode "
            "WHERE id = ANY(%s)",
            [episodes.ids],
        )
        waits = dict(self.env.cr.fetchall())
        return [
            (
                episode.start_at,
                episode.outcome,
                waits[episode.id],
                episode.response_order,
            )
            for episode in episodes
        ]

    # Criterion 3 -----------------------------------------------------------------

    def test_consecutive_messages_wait_for_the_first_positive_delivery(self):
        channel, first = self.open_conversation(at(10))
        self.customer(channel, at(10, 0, 30), processed_at=at(10, 0, 31))
        self.clock[0] = at(10, 1)
        failed = self.deliver(self.send(channel), at(10, 1, 5), state="failed")
        queued = self.send(channel)
        self.assertEqual(queued.delivery_state, "queued")
        self.assertEqual(self.summary(channel), [(at(10), "pending", None, "first")])

        answer = self.deliver(self.send(channel, self.agent_b), at(10, 15))
        episode = self.episodes(channel)
        self.assertEqual(len(episode), 1)
        self.assertEqual(episode.start_message_binding_id, first)
        self.assertEqual(episode.response_message_binding_id, answer)
        self.assertEqual(episode.outcome, "answered")
        self.assertEqual(episode.wait_seconds, 15 * 60)
        self.assertEqual(episode.end_at, at(10, 15))
        self.assertTrue(episode.is_first)
        self.assertEqual(episode.response_origin, "agent")
        self.assertEqual(episode.respondent_id, self.agent_b)
        self.assertEqual(episode.cycle_opened_by, "created")
        self.assertEqual(episode.account_id, self.account)
        self.assertEqual(episode.platform, "whatsapp")
        # Later evidence of the same answer does not move the measurement.
        self.deliver(answer, at(10, 20), state="read")
        self.deliver(failed, at(10, 30), state="sent")
        self.assertEqual(self.episodes(channel).wait_seconds, 15 * 60)

    def test_following_message_after_an_answer_is_a_subsequent_episode(self):
        channel, _first = self.open_conversation(at(10))
        self.deliver(self.send(channel), at(10, 2))
        self.customer(channel, at(10, 5), processed_at=at(10, 5))
        self.customer(channel, at(10, 6), processed_at=at(10, 6))
        self.deliver(self.send(channel), at(10, 9))

        self.assertEqual(
            self.summary(channel),
            [
                (at(10), "answered", 120, "first"),
                (at(10, 5), "answered", 240, "subsequent"),
            ],
        )
        self.assertFalse(self.episodes(channel)[1].is_first)

    def test_automation_does_not_answer_but_a_phone_reply_does(self):
        channel, _first = self.open_conversation(at(10))
        self.deliver(self.automation(channel), at(10, 1))
        self.assertEqual(self.summary(channel), [(at(10), "pending", None, "first")])

        self.clock[0] = at(10, 4)
        reply = self.phone(channel, at(10, 3))
        episode = self.episodes(channel)
        self.assertEqual(episode.outcome, "answered")
        self.assertEqual(episode.wait_seconds, 180)
        self.assertEqual(episode.response_origin, "external_device")
        self.assertEqual(episode.response_message_binding_id, reply)
        self.assertFalse(episode.respondent_id)

    def test_control_cards_groups_and_old_messages_open_no_episode(self):
        channel, _first = self.open_conversation(at(10))
        self.deliver(self.send(channel), at(10, 1))
        self.clock[0] = at(10, 2)
        call = EventDTO.from_dict(
            dict(
                self._event(self.remote_of(channel), at(10, 2)).to_dict(),
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
        )
        self.application._process_event(self.connection, call)
        self.assertEqual(len(self.episodes(channel)), 1)
        kinds = self.placements(channel).mapped("kind")
        self.assertEqual(kinds, ["customer", "agent", "control"])

        # A message recorded before the history existed has no position.
        old_channel, old_message = self.open_conversation(at(10, 30))
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE contact_center_message_binding SET lifecycle_seq = NULL "
            "WHERE id = %s",
            [old_message.id],
        )
        self.assertFalse(self.episodes(old_channel))
        self.assertFalse(self.placements(old_channel))

        group_account = self._account("Grupos", group_inbound_enabled=True)
        group_connection = self._connection(group_account)
        group_ref = "1203632%s@g.us" % (uuid.uuid4().int % 10**9)
        sender = "700002%s@lid" % (uuid.uuid4().int % 10**9)
        event = EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": "fixture-v1",
                "event_id": "attendance-group-%s" % uuid.uuid4(),
                "event_type": "message.created",
                "occurred_at": "%sT10:00:00Z" % DAY,
                "account_ref": group_account.external_ref,
                "connection_ref": group_connection.external_ref,
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
                            "confidence": "protocol",
                        }
                    ],
                },
                "extensions": {"conversation_name": "Grupo"},
                "message": {
                    "external_message_id": "attendance-group-%s" % uuid.uuid4(),
                    "content_type": "text",
                    "text": "Mensagem no grupo",
                },
            }
        )
        group_message = self.application.with_context(
            contact_center_skip_enqueue=True
        )._process_event(group_connection, event)
        group_channel = self.channel_of(self._binding_of(group_message))
        self.assertTrue(self._binding_of(group_message).lifecycle_seq)
        self.assertFalse(self.episodes(group_channel))

    # Criterion 3b ----------------------------------------------------------------

    def test_resolution_or_archive_closes_a_pending_episode(self):
        for state in ("resolved", "archived"):
            channel, _first = self.open_conversation(at(10))
            self.claim(channel)
            self.clock[0] = at(10, 5)
            self.set_state(channel, state)
            episode = self.episodes(channel)
            self.assertEqual(episode.outcome, "closed_unanswered", state)
            self.assertFalse(episode.wait_seconds)
            self.assertEqual(episode.end_at, at(10, 5))
            self.assertEqual(episode.cycle_closed_at, at(10, 5))

    def test_late_trigger_replay_and_equal_times_start_a_new_first_episode(self):
        channel, _first = self.open_conversation(at(10))
        self.claim(channel)
        self.clock[0] = at(10, 4)
        self.set_state(channel, "resolved")
        key = uuid.uuid4().hex
        trigger = self.customer(channel, at(10, 4), processed_at=at(10, 4), key=key)
        replay = self.customer(channel, at(10, 4), processed_at=at(10, 6), key=key)
        self.assertEqual(replay, trigger)

        reopened = self.events(channel)[-1]
        self.assertEqual(reopened.event_type, "reopened")
        episodes = self.episodes(channel)
        self.assertEqual(
            [(e.outcome, e.cycle_sequence, e.response_order) for e in episodes],
            [
                ("closed_unanswered", 1, "first"),
                ("pending", reopened.sequence, "first"),
            ],
        )
        self.assertEqual(episodes[1].cycle_opened_by, "reopened")
        self.assertTrue(episodes[1].is_first)
        self.assertEqual(episodes[1].start_message_binding_id, trigger)

    def test_archived_inbound_opens_nothing_and_unarchive_starts_a_cycle(self):
        channel, _first = self.open_conversation(at(10))
        self.deliver(self.send(channel), at(10, 1))
        self.clock[0] = at(10, 5)
        self.set_state(channel, "archived")
        archived = self.customer(channel, at(10, 6), processed_at=at(10, 6))
        self.assertEqual(
            self.placements(channel)
            .filtered(lambda item: item.message_binding_id == archived)
            .placement,
            "archived",
        )
        self.assertEqual(len(self.episodes(channel)), 1)

        self.clock[0] = at(10, 7)
        self.set_state(channel, "open")
        self.customer(channel, at(10, 8), processed_at=at(10, 8))
        episodes = self.episodes(channel)
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[1].cycle_opened_by, "unarchived")
        self.assertEqual(episodes[1].response_order, "first")
        self.assertEqual(episodes[1].start_at, at(10, 8))

    # Criterion 3c ----------------------------------------------------------------

    def test_trigger_written_before_the_resolution_belongs_to_the_new_cycle(self):
        channel, _first = self.open_conversation(at(9))
        self.deliver(self.send(channel), at(9, 1))
        self.clock[0] = at(10, 4)
        self.set_state(channel, "resolved")
        self.customer(channel, at(10, 3), processed_at=at(10, 5))
        self.deliver(self.send(channel), at(10, 6))

        episodes = self.episodes(channel)
        self.assertEqual(
            self.summary(channel),
            [(at(9), "answered", 60, "first"), (at(10, 3), "answered", 180, "first")],
        )
        self.assertEqual(episodes[1].cycle_opened_by, "reopened")
        self.assertNotEqual(episodes[0].cycle_sequence, episodes[1].cycle_sequence)

    # Criterion 3d ----------------------------------------------------------------

    def test_same_second_measures_zero_only_for_a_later_recorded_answer(self):
        phone_channel, _first = self.open_conversation(at(10))
        self.phone(phone_channel, at(10))
        self.assertEqual(
            self.summary(phone_channel), [(at(10), "answered", 0, "first")]
        )

        agent_channel, _first = self.open_conversation(at(10))
        self.deliver(self.send(agent_channel), at(10))
        self.assertEqual(
            self.summary(agent_channel), [(at(10), "answered", 0, "first")]
        )

        early_channel, _first = self.open_conversation(at(9))
        self.phone(early_channel, at(9, 1))
        typed_before = self.send(early_channel)
        self.customer(early_channel, at(10), processed_at=at(10))
        self.deliver(typed_before, at(10))
        self.assertEqual(
            self.summary(early_channel),
            [(at(9), "answered", 60, "first"), (at(10), "pending", None, "subsequent")],
        )

    # Criterion 3e ----------------------------------------------------------------

    def test_answer_recorded_before_resolution_and_delivered_after_counts(self):
        channel, _first = self.open_conversation(at(10))
        self.claim(channel)
        self.clock[0] = at(10, 1)
        answer = self.send(channel)
        self.clock[0] = at(10, 2)
        self.set_state(channel, "resolved")
        self.assertEqual(self.episodes(channel).outcome, "closed_unanswered")

        self.deliver(answer, at(10, 3))
        episode = self.episodes(channel)
        self.assertEqual(episode.outcome, "answered")
        self.assertEqual(episode.wait_seconds, 180)

    # Criterion 3f ----------------------------------------------------------------

    def test_late_phone_webhook_answers_the_cycle_it_was_written_in(self):
        channel, _first = self.open_conversation(at(10))
        self.claim(channel)
        self.clock[0] = at(10, 2)
        self.set_state(channel, "resolved")
        self.customer(channel, at(10, 3), processed_at=at(10, 3))
        self.clock[0] = at(10, 4)
        reply = self.phone(channel, at(10, 1))
        self.clock[0] = at(10, 5)
        between = self.phone(channel, at(10, 2, 30))

        episodes = self.episodes(channel)
        self.assertEqual(
            [item[:3] for item in self.summary(channel)],
            [(at(10), "answered", 60), (at(10, 3), "pending", None)],
        )
        self.assertEqual(episodes[0].response_message_binding_id, reply)
        self.assertEqual(episodes[0].response_origin, "external_device")
        placements = {
            item.message_binding_id: item for item in self.placements(channel)
        }
        self.assertEqual(placements[reply].cycle_sequence, episodes[0].cycle_sequence)
        self.assertEqual(placements[between].placement, "unplaced")
        self.assertFalse(placements[between].cycle_sequence)

    def test_phone_reply_during_the_creation_webhook_lag_answers(self):
        """L09-SQL-01: the created cycle starts at the creating message."""

        channel, _first = self.open_conversation(at(10, 0, 5), at(10))
        self.clock[0] = at(10, 1)
        reply = self.phone(channel, at(10, 0, 3))
        self.assertEqual(self.summary(channel), [(at(10), "answered", 3, "first")])
        self.assertEqual(self.episodes(channel).response_message_binding_id, reply)

    def test_phone_reply_during_an_automatic_reopening_lag_answers(self):
        channel, _first = self.open_conversation(at(9))
        self.claim(channel)
        self.phone(channel, at(9, 1))
        self.clock[0] = at(9, 30)
        self.set_state(channel, "resolved")
        self.customer(channel, at(10), processed_at=at(10, 0, 5))
        self.clock[0] = at(10, 1)
        reply = self.phone(channel, at(10, 0, 3))
        episodes = self.episodes(channel)
        self.assertEqual(
            [item[:3] for item in self.summary(channel)],
            [(at(9), "answered", 60), (at(10), "answered", 3)],
        )
        self.assertEqual(episodes[1].cycle_opened_by, "reopened")
        self.assertEqual(episodes[1].response_message_binding_id, reply)

    def test_phone_first_conversation_places_its_opening_message(self):
        """L09-SQL-04: the phone message that created the conversation is placed."""

        self.clock[0] = at(10, 0, 3)
        opening = self.phone_first(self.remote(), at(10))
        channel = self.channel_of(opening)
        created = self.events(channel)
        self.assertEqual(created.event_type, "created")
        self.assertEqual(opening.caused_assignment_event_id, created)
        placement = self.placements(channel)
        self.assertEqual(placement.placement, "cycle")
        self.assertEqual(placement.cycle_sequence, created.sequence)
        self.assertFalse(self.episodes(channel))

    def test_customer_webhook_after_a_phone_first_reply_is_answered(self):
        """Out-of-order webhooks: the phone reply that created it still answers."""

        remote = self.remote()
        self.clock[0] = at(10, 0, 6)
        reply = self.phone_first(remote, at(10, 0, 5))
        channel = self.channel_of(reply)
        first = self.customer(channel, at(10), processed_at=at(10, 0, 10))
        self.assertFalse(first.caused_assignment_event_id)
        episode = self.episodes(channel)
        self.assertEqual(self.summary(channel), [(at(10), "answered", 5, "first")])
        self.assertEqual(episode.response_message_binding_id, reply)
        self.assertEqual(episode.response_origin, "external_device")

    def test_manual_reopening_keeps_its_event_time(self):
        """Only inbound processing anticipates the start of its cycle."""

        channel, _first = self.open_conversation(at(9))
        self.claim(channel)
        self.phone(channel, at(9, 1))
        self.clock[0] = at(10)
        self.set_state(channel, "resolved")
        self.clock[0] = at(10, 5)
        self.set_state(channel, "open")
        self.customer(channel, at(10, 3), processed_at=at(10, 6))
        self.clock[0] = at(10, 7)
        reply = self.phone(channel, at(10, 4))
        placement = self.placements(channel).filtered(
            lambda item: item.message_binding_id == reply
        )
        self.assertEqual(placement.placement, "unplaced")
        self.assertEqual(
            [item[:2] for item in self.summary(channel)],
            [(at(9), "answered"), (at(10, 3), "pending")],
        )

    def test_reply_recorded_in_its_cycle_wins_over_a_later_anticipated_start(self):
        """L09-16 (1): a reply kept by sequence is never re-examined by time."""

        channel, _first = self.open_conversation(at(10))
        self.claim(channel)
        self.clock[0] = at(10, 1)
        reply = self.phone(channel, at(10, 1))
        self.clock[0] = at(10, 2)
        self.set_state(channel, "resolved")
        self.customer(channel, at(10, 0, 30), processed_at=at(10, 5))

        episodes = self.episodes(channel)
        self.assertEqual(
            [item[:3] for item in self.summary(channel)],
            [(at(10), "answered", 60), (at(10, 0, 30), "pending", None)],
        )
        self.assertEqual(episodes[0].response_message_binding_id, reply)

    def test_reply_arriving_after_the_reopening_but_written_before_is_unplaced(self):
        """L09-17: the sequence of a late webhook proves nothing before B opened."""

        channel, _first = self.open_conversation(at(10))
        self.claim(channel)
        self.clock[0] = at(10, 2)
        self.set_state(channel, "resolved")
        self.customer(channel, at(10, 0, 30), processed_at=at(10, 5))
        self.clock[0] = at(10, 6)
        late = self.phone(channel, at(10, 1))
        reopened = self.events(channel).filtered(
            lambda event: event.event_type == "reopened"
        )
        self.assertEqual(late.lifecycle_seq, reopened.sequence)
        placement = self.placements(channel).filtered(
            lambda item: item.message_binding_id == late
        )
        self.assertEqual(placement.placement, "unplaced")
        self.assertEqual(
            [item[:2] for item in self.summary(channel)],
            [(at(10), "closed_unanswered"), (at(10, 0, 30), "pending")],
        )

    def test_late_reply_inside_two_anticipated_cycles_is_unplaced(self):
        """L09-16 (2): placed by time into an overlap, the reply is ambiguous."""

        channel, _first = self.open_conversation(at(10))
        self.claim(channel)
        self.clock[0] = at(10, 2)
        self.set_state(channel, "resolved")
        self.clock[0] = at(10, 3)
        late = self.phone(channel, at(10, 1))
        self.customer(channel, at(10, 0, 30), processed_at=at(10, 5))

        placement = self.placements(channel).filtered(
            lambda item: item.message_binding_id == late
        )
        self.assertEqual(placement.placement, "unplaced")
        self.assertEqual(
            [item[:2] for item in self.summary(channel)],
            [(at(10), "closed_unanswered"), (at(10, 0, 30), "pending")],
        )

    # Criterion 3g ----------------------------------------------------------------

    def test_same_second_closure_keeps_the_answer_and_rejects_a_late_reply(self):
        channel, _first = self.open_conversation(at(10))
        self.claim(channel)
        self.phone(channel, at(10))
        self.set_state(channel, "resolved")
        self.assertEqual(self.summary(channel), [(at(10), "answered", 0, "first")])

        self.clock[0] = at(10, 1)
        late = self.phone(channel, at(10))
        placement = self.placements(channel).filtered(
            lambda item: item.message_binding_id == late
        )
        self.assertEqual(placement.placement, "unplaced")
        self.assertEqual(self.summary(channel), [(at(10), "answered", 0, "first")])

    # Criterion 4 -----------------------------------------------------------------

    def test_a_late_reply_at_a_closing_second_is_ambiguous(self):
        channel, _first = self.open_conversation(at(10))
        self.claim(channel)
        self.clock[0] = at(10, 2)
        self.set_state(channel, "resolved")
        # Reopened within the same second as the resolution, then closed again.
        self.customer(channel, at(10, 1, 59), processed_at=at(10, 2))
        self.clock[0] = at(10, 3)
        self.set_state(channel, "resolved")
        self.clock[0] = at(10, 4)
        late = self.phone(channel, at(10, 2))
        inside = self.phone(channel, at(10, 2, 30))

        placements = {
            item.message_binding_id: item for item in self.placements(channel)
        }
        self.assertEqual(placements[late].placement, "unplaced")
        reopened = self.events(channel).filtered(
            lambda event: event.event_type == "reopened"
        )
        self.assertEqual(placements[inside].cycle_sequence, reopened.sequence)

    def answered_conversation(self):
        """A conversation whose opening episode is already answered at 09:01."""

        channel, _first = self.open_conversation(at(9))
        self.clock[0] = at(9, 1)
        self.phone(channel, at(9, 1))
        return channel

    def test_responsible_is_taken_at_the_customer_time(self):
        channel = self.answered_conversation()
        self.clock[0] = at(9, 30)
        self.claim(channel, self.agent_a)
        self.clock[0] = at(10, 5)
        self.assign(channel, self.agent_b)
        self.customer(channel, at(10, 1), processed_at=at(10, 10))

        episode = self.episodes(channel)[-1]
        self.assertEqual(episode.start_at, at(10, 1))
        self.assertTrue(episode.responsible_known)
        self.assertEqual(episode.responsible_id, self.agent_a)
        self.assertEqual(episode.responsible_status, "assigned")

    def test_assignment_in_the_customer_second_is_in_force(self):
        """``occurred_at <=`` the provider time: the same second counts."""

        channel = self.answered_conversation()
        self.clock[0] = at(10)
        self.claim(channel, self.agent_a)
        self.customer(channel, at(10), processed_at=at(10, 0, 20))
        episode = self.episodes(channel)[-1]
        self.assertEqual(episode.start_at, at(10))
        self.assertEqual(episode.responsible_id, self.agent_a)

    def test_automatic_assignment_caused_by_the_opening_message(self):
        channel = self.answered_conversation()
        self.account.auto_assignment_user_id = self.agent_b
        opening = self.customer(channel, at(10, 4, 58), processed_at=at(10, 5))

        self.assertTrue(opening.caused_assignment_event_id)
        episode = self.episodes(channel)[-1]
        self.assertEqual(episode.start_message_binding_id, opening)
        self.assertEqual(episode.responsible_id, self.agent_b)
        self.assertEqual(episode.responsible_status, "assigned")

    def test_a_late_message_of_the_same_sequence_does_not_inherit_the_cause(self):
        channel = self.answered_conversation()
        self.clock[0] = at(9, 30)
        self.claim(channel, self.agent_a)
        self.clock[0] = at(10)
        self.assign(channel, False)
        self.account.auto_assignment_user_id = self.agent_b
        first = self.customer(channel, at(10, 4, 58), processed_at=at(10, 5))
        late = self.customer(channel, at(9, 59), processed_at=at(10, 6))

        self.assertTrue(first.caused_assignment_event_id)
        self.assertFalse(late.caused_assignment_event_id)
        self.assertEqual(late.lifecycle_seq, first.lifecycle_seq)
        episode = self.episodes(channel)[-1]
        self.assertEqual(episode.start_message_binding_id, late)
        self.assertEqual(episode.responsible_id, self.agent_a)

    def test_empty_creation_is_a_known_absence_of_responsible(self):
        channel, _first = self.open_conversation(at(10), at(9, 59, 58))
        episode = self.episodes(channel)
        self.assertEqual(episode.start_at, at(9, 59, 58))
        self.assertTrue(episode.responsible_known)
        self.assertFalse(episode.responsible_id)
        self.assertEqual(episode.responsible_status, "unassigned")

    def test_creation_by_the_message_gives_the_created_responsible(self):
        self.account.auto_assignment_user_id = self.agent_b
        channel, first = self.open_conversation(at(10), at(9, 59, 58))
        self.assertEqual(first.caused_assignment_event_id, self.events(channel))
        episode = self.episodes(channel)
        self.assertEqual(episode.responsible_id, self.agent_b)
        self.assertEqual(episode.responsible_status, "assigned")

    def test_webhook_older_than_an_agent_started_conversation_is_unknown(self):
        """L09-15: the agent's creation never answers for an earlier message."""

        self.account.auto_assignment_user_id = self.agent_b
        self.clock[0] = at(10)
        channel = self.env["mail.channel"].browse(
            self.api(self.agent_a).start_conversation(
                self.account.id, "(11) 99876-2222"
            )["channel_id"]
        )
        self.customer(channel, at(9), processed_at=at(11))
        episode = self.episodes(channel)
        self.assertEqual(episode.start_at, at(9))
        self.assertFalse(episode.responsible_known)
        self.assertEqual(episode.responsible_status, "unknown")
        self.assertFalse(episode.responsible_id)

    def test_message_before_the_baseline_has_an_unknown_responsible(self):
        channel = self.answered_conversation()
        self.clock[0] = at(9, 30)
        self.claim(channel, self.agent_a)
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
            "WHERE channel_binding_id IN (SELECT id FROM "
            "contact_center_channel_binding WHERE channel_id = %s)",
            [channel.id],
        )
        self.env.invalidate_all()
        self.clock[0] = at(11)
        self.event_model._contact_center_record_migration_baselines()

        self.customer(channel, at(10, 30), processed_at=at(11, 5))
        self.clock[0] = at(11, 6)
        self.phone(channel, at(11, 6))
        self.customer(channel, at(11, 10), processed_at=at(11, 10))

        episodes = self.episodes(channel)
        self.assertEqual(
            [
                (e.start_at, e.response_order, e.responsible_status, e.is_first)
                for e in episodes
            ],
            [
                (at(10, 30), "before_history", "unknown", False),
                (at(11, 10), "subsequent", "assigned", False),
            ],
        )
        self.assertFalse(episodes[0].responsible_known)
        self.assertEqual(episodes[1].responsible_id, self.agent_a)
        self.assertEqual(episodes[0].cycle_opened_by, "baseline")
        # The ORM shows False; the view keeps SQL NULL for "before the history".
        self.env.cr.execute(
            "SELECT is_first IS NULL FROM contact_center_attendance_episode "
            "WHERE id = %s",
            [episodes[0].id],
        )
        self.assertTrue(self.env.cr.fetchone()[0])

    def test_transfer_during_an_episode_keeps_the_opening_responsible(self):
        channel = self.answered_conversation()
        self.clock[0] = at(9, 30)
        self.claim(channel, self.agent_a)
        self.customer(channel, at(10), processed_at=at(10))
        self.clock[0] = at(10, 3)
        self.assign(channel, self.agent_b)
        self.deliver(self.send(channel, self.agent_b), at(10, 5))

        episode = self.episodes(channel)[-1]
        self.assertEqual(episode.responsible_id, self.agent_a)
        self.assertEqual(episode.respondent_id, self.agent_b)
        self.assertEqual(episode.wait_seconds, 300)


class TestAttendanceReport(AttendanceCase):
    """Criterion 5: server percentiles per group, coverage and "sem dados"."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.quiet_account = cls._account("Sem dados")
        cls.quiet_connection = cls._connection(cls.quiet_account)
        cls.other_account = cls._account("Outra caixa")
        cls.other_connection = cls._connection(cls.other_account)

    def setUp(self):
        super().setUp()
        (self.quiet_connection | self.other_connection).write(
            {
                "last_state_observed_at": self.connection.last_state_observed_at,
                "last_state_source": "health_job",
                "health_detail": "healthy",
            }
        )

    def conversation_answered_after(self, seconds, *, start=None, responsible=None):
        start = start or at(10)
        self.clock[0] = start
        channel = self.channel_of(self.inbound(self.remote(), start))
        if responsible:
            self.claim(channel, responsible)
        self.deliver(
            self.send(channel, responsible or self.agent_a),
            start + datetime.timedelta(seconds=seconds),
        )
        return channel

    def report(self, user=None, **filters):
        # Agent posts are dated by the wall clock; cover it and the fixture day.
        today = fields.Date.today()
        values = {
            "date_from": str(min(today, DAY) - datetime.timedelta(days=1)),
            "date_to": str(max(today, DAY) + datetime.timedelta(days=1)),
            "account_ids": (self.account | self.quiet_account).ids,
        }
        values.update(filters)
        self.env.flush_all()
        return (
            self.env["contact.center.attendance.report"]
            .with_user(user or self.supervisor)
            .get_report(values)
        )

    def test_medians_and_p90_are_server_percentiles_per_group_and_total(self):
        channels = [
            self.conversation_answered_after(seconds)
            for seconds in (60, 120, 180, 240, 600)
        ]
        # Two subsequent episodes of 30 s and 90 s.
        for channel, seconds in zip(channels[:2], (30, 90)):
            self.clock[0] = at(11)
            self.inbound(self._channel_binding(channel).conversation_ref, at(11))
            self.deliver(
                self.send(channel), at(11) + datetime.timedelta(seconds=seconds)
            )
        self.clock[0] = at(11, 30)
        other = self.channel_of(
            self.inbound(self.remote(), at(11, 30), connection=self.other_connection)
        )
        self.deliver(self.send(other), at(11, 30) + datetime.timedelta(seconds=1000))
        # A pending episode in the quiet inbox and a transfer/reopening.
        self.clock[0] = at(12)
        pending = self.channel_of(
            self.inbound(self.remote(), at(12), connection=self.quiet_connection)
        )
        self.claim(channels[0], self.agent_a)
        self.clock[0] = at(12, 1)
        self.assign(channels[0], self.agent_b)
        self.set_state(channels[1], "resolved")
        self.clock[0] = at(12, 2)
        self.inbound(self._channel_binding(channels[1]).conversation_ref, at(12, 2))

        result = self.report(
            account_ids=(self.account | self.quiet_account | self.other_account).ids
        )
        groups = {row["key"]: row for row in result["groups"]}
        main = groups[self.account.id]
        self.assertEqual(main["episodes"], 8)
        self.assertEqual(main["answered"], 7)
        self.assertEqual(main["pending"], 1)
        self.assertEqual(main["first_response"]["measured"], 5)
        self.assertEqual(main["first_response"]["median"], 180)
        self.assertAlmostEqual(main["first_response"]["p90"], 456)
        self.assertEqual(main["subsequent_response"]["median"], 60)
        self.assertAlmostEqual(main["subsequent_response"]["p90"], 84)
        self.assertEqual(main["transfers"], 1)
        self.assertEqual(main["reopenings"], 1)
        self.assertEqual(main["inbound"], 8)
        self.assertEqual(main["outbound"], 7)
        self.assertEqual(main["responsible_known_ratio"], 1)

        quiet = groups[self.quiet_account.id]
        self.assertEqual(quiet["episodes"], 1)
        self.assertEqual(quiet["pending"], 1)
        self.assertIsNone(quiet["first_response"]["median"])
        self.assertIsNone(quiet["first_response"]["p90"])
        self.assertIsNone(quiet["subsequent_response"]["median"])

        other = groups[self.other_account.id]
        self.assertEqual(other["first_response"]["median"], 1000)
        # The total is a percentile of all episodes (210 s), not a mean of the
        # group medians (590 s).
        self.assertEqual(result["total"]["episodes"], 10)
        self.assertEqual(result["total"]["first_response"]["median"], 210)
        self.assertEqual(result["time_basis"], "elapsed")
        self.assertTrue(pending)

    def test_groups_without_episodes_show_no_measurement(self):
        self.conversation_answered_after(60)
        result = self.report()
        quiet = next(
            row for row in result["groups"] if row["key"] == self.quiet_account.id
        )
        self.assertEqual(quiet["episodes"], 0)
        self.assertIsNone(quiet["responsible_known_ratio"])
        self.assertIsNone(quiet["first_response"]["median"])
        self.assertEqual(quiet["first_response"]["measured"], 0)
        self.assertIn(
            self.quiet_account.id,
            [item["id"] for item in result["options"]["accounts"]],
        )

    def test_responsible_grouping_and_known_coverage(self):
        known = self.conversation_answered_after(60, responsible=self.agent_a)
        # The first episode takes the creation its message caused (no
        # responsible, even if claimed within the same second, L09-15); the
        # next one takes the claim in force at the customer time.
        self.clock[0] = at(10, 30)
        self.inbound(self._channel_binding(known).conversation_ref, at(10, 30))
        self.conversation_answered_after(120)
        legacy = self.conversation_answered_after(30, start=at(9))
        self.env.flush_all()
        self.env.cr.execute(
            "DELETE FROM contact_center_conversation_event WHERE channel_id = %s",
            [legacy.id],
        )
        self.env.cr.execute(
            "UPDATE mail_channel SET contact_center_lifecycle_seq = 0 WHERE id = %s",
            [legacy.id],
        )
        self.env.cr.execute(
            "UPDATE contact_center_message_binding SET lifecycle_seq = NULL "
            "WHERE channel_binding_id IN (SELECT id FROM "
            "contact_center_channel_binding WHERE channel_id = %s)",
            [legacy.id],
        )
        self.env.invalidate_all()
        self.clock[0] = at(13)
        self.event_model._contact_center_record_migration_baselines()
        self.inbound(self._channel_binding(legacy).conversation_ref, at(12, 30))

        result = self.report(group_by="responsible")
        groups = {row["key"]: row for row in result["groups"]}
        self.assertEqual(groups[self.agent_a.id]["episodes"], 1)
        self.assertEqual(groups[0]["episodes"], 2)
        self.assertEqual(groups[-1]["episodes"], 1)
        self.assertEqual(groups[-1]["before_history"], 1)
        self.assertEqual(groups[-1]["label"], "Unknown responsible")
        self.assertEqual(result["total"]["responsible_known"], 3)
        self.assertAlmostEqual(result["total"]["responsible_known_ratio"], 3 / 4)
        # The publication (baseline) falls inside the period: partial coverage.
        self.assertEqual(result["history_since"], "%s 13:00:00" % DAY)
        self.assertTrue(result["partial_history"])
        self.assertGreater(at(13), _period_start(result))
        self.assertTrue(known)

    def test_report_coverage_counts_unplaced_replies_and_archived_messages(self):
        self.clock[0] = at(10)
        channel = self.channel_of(self.inbound(self.remote(), at(10)))
        self.claim(channel)
        self.clock[0] = at(10, 2)
        self.set_state(channel, "resolved")
        self.clock[0] = at(10, 5)
        self.phone(channel, at(10, 3))
        self.clock[0] = at(10, 6)
        self.set_state(channel, "open")
        self.clock[0] = at(10, 7)
        self.set_state(channel, "archived")
        self.inbound(self._channel_binding(channel).conversation_ref, at(10, 8))

        total = self.report()["total"]
        self.assertEqual(total["unplaced_replies"], 1)
        self.assertEqual(total["archived_inbound"], 1)
        self.assertEqual(total["closed_unanswered"], 1)

    def scoped_activity(self):
        """One conversation with every counted kind of fact in the period."""

        channel = self.conversation_answered_after(60, responsible=self.agent_a)
        remote = self._channel_binding(channel).conversation_ref
        self.clock[0] = at(10, 5)
        self.assign(channel, self.agent_b)
        self.set_state(channel, "resolved", user=self.agent_b)
        self.clock[0] = at(10, 6)
        self.phone(channel, at(10, 5, 30))
        self.inbound(remote, at(10, 7))
        self.clock[0] = at(10, 8)
        self.set_state(channel, "archived", user=self.agent_b)
        self.inbound(remote, at(10, 9))
        return channel

    def test_report_is_for_supervisors_within_their_scope(self):
        self.scoped_activity()
        with self.assertRaises(AccessError):
            self.report(user=self.agent_a)
        visible = self.report(user=self.admin)["total"]
        self.assertEqual(
            {
                name: visible[name]
                for name in (
                    "episodes",
                    "inbound",
                    "outbound",
                    "transfers",
                    "reopenings",
                    "unplaced_replies",
                    "archived_inbound",
                )
            },
            {
                "episodes": 2,
                "inbound": 3,
                "outbound": 2,
                "transfers": 1,
                "reopenings": 1,
                "unplaced_replies": 1,
                "archived_inbound": 1,
            },
        )
        other_company = self.env["res.company"].create({"name": "Outra empresa"})
        foreign_admin = self._user("Foreign", "group_contact_center_admin")
        foreign_admin.write(
            {
                "company_ids": [(6, 0, other_company.ids)],
                "company_id": other_company.id,
            }
        )
        for viewer, context in (
            (self.outsider, {}),
            (foreign_admin, {"allowed_company_ids": other_company.ids}),
        ):
            env_user = viewer.with_context(**context)
            total = (
                self.env["contact.center.attendance.report"]
                .with_user(env_user)
                .with_context(**context)
                .get_report(
                    {
                        "date_from": str(DAY - datetime.timedelta(days=1)),
                        "date_to": str(
                            fields.Date.today() + datetime.timedelta(days=1)
                        ),
                        "account_ids": self.account.ids,
                    }
                )["total"]
            )
            for name in (
                "episodes",
                "inbound",
                "outbound",
                "transfers",
                "reopenings",
                "unplaced_replies",
                "archived_inbound",
                "closed_unanswered",
            ):
                self.assertEqual(total[name], 0, "%s for %s" % (name, viewer.name))
            for model_name in (
                "contact.center.attendance.episode",
                "contact.center.attendance.message",
                "contact.center.conversation.event",
            ):
                model = self.env[model_name].with_user(viewer).with_context(**context)
                self.assertFalse(model.search([]), model_name)
                self.assertFalse(
                    model.read_group([], ["channel_id"], ["channel_id"]), model_name
                )
        platform = self.report(group_by="platform")
        self.assertEqual([row["key"] for row in platform["groups"]], ["whatsapp"])
        self.assertEqual(platform["groups"][0]["episodes"], 2)
        none = self.report(group_by="none")
        self.assertEqual(none["groups"], [])
        self.assertEqual(none["total"]["first_response"]["median"], 60)

    def test_transfers_belong_to_the_previous_and_reopenings_to_the_current(self):
        channel = self.conversation_answered_after(60, responsible=self.agent_a)
        self.clock[0] = at(10, 5)
        self.assign(channel, self.agent_b)
        self.set_state(channel, "resolved", user=self.agent_b)
        self.clock[0] = at(10, 6)
        self.inbound(self._channel_binding(channel).conversation_ref, at(10, 6))

        groups = {
            row["key"]: row for row in self.report(group_by="responsible")["groups"]
        }
        self.assertEqual(groups[self.agent_a.id]["transfers"], 1)
        self.assertEqual(groups[self.agent_a.id]["reopenings"], 0)
        self.assertEqual(groups[self.agent_b.id]["transfers"], 0)
        self.assertEqual(groups[self.agent_b.id]["reopenings"], 1)

    def test_a_deleted_former_automatic_responsible_keeps_its_key(self):
        """L09-18: the report and the lists never resolve a deleted user."""

        former = self._user("Former", "group_contact_center_agent")
        self.account.write(
            {
                "access_user_ids": [(4, former.id)],
                "auto_assignment_user_id": former.id,
            }
        )
        # An episode, a transfer away from the former owner, and a reopening
        # of a conversation it still owns; it never acts itself.
        transferred = self.conversation_answered_after(60)
        self.clock[0] = at(10, 5)
        self.assign(transferred, self.agent_b)
        kept = self.conversation_answered_after(120, start=at(11))
        self.clock[0] = at(11, 10)
        self.api(self.supervisor).update_conversation(kept.id, {"state": "resolved"})
        self.clock[0] = at(11, 20)
        self.inbound(self._channel_binding(kept).conversation_ref, at(11, 20))
        # Access removal unassigns it (after the messages), then it is deleted.
        self.clock[0] = at(11, 30)
        self.account.write(
            {"auto_assignment_user_id": False, "access_user_ids": [(3, former.id)]}
        )
        former_id = former.id
        former.unlink()
        self.assertFalse(self.env["res.users"].browse(former_id).exists())

        groups = {
            row["key"]: row for row in self.report(group_by="responsible")["groups"]
        }
        removed = groups[former_id]
        self.assertEqual(removed["label"], "Removed user (#%s)" % former_id)
        self.assertEqual(removed["episodes"], 3)
        self.assertEqual(removed["transfers"], 1)
        self.assertEqual(removed["reopenings"], 1)
        self.assertEqual(removed["responsible_known"], 3)
        self.assertEqual(sum(row["transfers"] for row in groups.values()), 1)
        self.assertNotIn(-1, groups)
        filtered = self.report(responsible_ids=[former_id])["total"]
        self.assertEqual((filtered["episodes"], filtered["transfers"]), (3, 1))

        # The lists and the pivot read without resolving the missing user.
        for model_name in (
            "contact.center.attendance.episode",
            "contact.center.attendance.message",
        ):
            model = self.env[model_name].with_user(self.supervisor)
            rows = model.search([("channel_id", "in", (transferred | kept).ids)]).read(
                ["responsible_id", "responsible_ref", "responsible_label"]
            )
            removed_rows = [row for row in rows if row["responsible_ref"] == former_id]
            self.assertTrue(removed_rows, model_name)
            for row in removed_rows:
                self.assertFalse(row["responsible_id"])
                self.assertEqual(
                    row["responsible_label"], "Removed user (#%s)" % former_id
                )
            model.read_group(
                [("channel_id", "in", (transferred | kept).ids)],
                ["responsible_id", "responsible_status"],
                ["responsible_id", "responsible_status"],
                lazy=False,
            )
        statuses = set(
            self.episodes(transferred | kept)
            .filtered(lambda item: item.responsible_ref == former_id)
            .mapped("responsible_status")
        )
        self.assertEqual(statuses, {"removed"})

        # The ledger display fields keep the id and read as removed.
        events = self.event_model.with_user(self.supervisor).search(
            [("channel_id", "in", (transferred | kept).ids)]
        )
        values = events.read(
            [
                "responsible_ref",
                "responsible_id",
                "responsible_label",
                "previous_responsible_ref",
                "previous_responsible_id",
                "previous_responsible_label",
                "actor_label",
            ]
        )
        transfer = next(
            row
            for row in values
            if row["previous_responsible_ref"] == former_id
            and row["responsible_ref"] == self.agent_b.id
        )
        self.assertFalse(transfer["previous_responsible_id"])
        self.assertEqual(
            transfer["previous_responsible_label"], "Removed user (#%s)" % former_id
        )
        self.assertEqual(transfer["responsible_label"], self.agent_b.display_name)
        created = next(
            row
            for row in values
            if row["responsible_ref"] == former_id and not row["actor_label"]
        )
        self.assertFalse(created["responsible_id"])
        self.assertEqual(created["responsible_label"], "Removed user (#%s)" % former_id)

    def test_list_and_pivot_group_by_the_historic_responsibility(self):
        """L09-I1-01: deleted users, no responsible and unknown stay apart."""

        first = self._user("First", "group_contact_center_agent")
        second = self._user("Second", "group_contact_center_agent")
        self.account.write({"access_user_ids": [(4, first.id), (4, second.id)]})
        channels = {}
        for key, assignee, start in (
            ("first", first, at(10)),
            ("second", second, at(10, 10)),
            ("existing", self.agent_a, at(10, 20)),
            ("unassigned", False, at(10, 30)),
            ("unknown", False, at(9)),
        ):
            self.account.auto_assignment_user_id = assignee
            channels[key] = self.conversation_answered_after(60, start=start)
        legacy = channels["unknown"]
        self.env.flush_all()
        self.env.cr.execute(
            "DELETE FROM contact_center_conversation_event WHERE channel_id = %s",
            [legacy.id],
        )
        self.env.cr.execute(
            "UPDATE mail_channel SET contact_center_lifecycle_seq = 0 WHERE id = %s",
            [legacy.id],
        )
        self.env.cr.execute(
            "UPDATE contact_center_message_binding SET lifecycle_seq = NULL "
            "WHERE channel_binding_id IN (SELECT id FROM "
            "contact_center_channel_binding WHERE channel_id = %s)",
            [legacy.id],
        )
        self.env.invalidate_all()
        self.clock[0] = at(13)
        self.event_model._contact_center_record_migration_baselines()
        self.inbound(self._channel_binding(legacy).conversation_ref, at(12, 30))
        self.clock[0] = at(13, 30)
        self.account.write(
            {
                "auto_assignment_user_id": False,
                "access_user_ids": [(3, first.id), (3, second.id)],
            }
        )
        first_id, second_id = first.id, second.id
        (first | second).unlink()

        domain = [("channel_id", "in", [channel.id for channel in channels.values()])]
        model = self.env["contact.center.attendance.episode"].with_user(self.supervisor)
        self.env.flush_all()
        model.invalidate_model()
        groups = {
            row["responsible_group"]: row["responsible_group_count"]
            for row in model.read_group(
                domain, ["responsible_group"], ["responsible_group"]
            )
        }
        self.assertEqual(
            groups,
            {
                str(first_id): 1,
                str(second_id): 1,
                str(self.agent_a.id): 1,
                "unassigned": 1,
                "unknown": 1,
            },
        )
        labels = dict(
            model.fields_get(["responsible_group"])["responsible_group"]["selection"]
        )
        self.assertEqual(labels[str(first_id)], "Removed user (#%s)" % first_id)
        self.assertEqual(labels[str(second_id)], "Removed user (#%s)" % second_id)
        self.assertEqual(labels[str(self.agent_a.id)], self.agent_a.display_name)
        self.assertEqual(labels["unassigned"], "No responsible")
        self.assertEqual(labels["unknown"], "Unknown responsible")
        # The pivot groups the same way, crossed with another dimension.
        pivot = model.read_group(
            domain,
            ["responsible_group"],
            ["account_id", "responsible_group"],
            lazy=False,
        )
        self.assertEqual(
            sorted(row["responsible_group"] for row in pivot),
            sorted(groups),
        )
        search_arch = self.env.ref(
            "contact_center_base.view_contact_center_attendance_episode_search"
        ).arch
        self.assertIn("'group_by': 'responsible_group'", search_arch)
        rows = model.search(domain).read(["responsible_group", "responsible_label"])
        self.assertIn(
            "Removed user (#%s)" % second_id,
            [row["responsible_label"] for row in rows],
        )
        report = {
            row["key"]: row["label"]
            for row in self.report(group_by="responsible")["groups"]
        }
        self.assertEqual(report[first_id], "Removed user (#%s)" % first_id)
        self.assertEqual(report[second_id], "Removed user (#%s)" % second_id)

    def former_responsible(self, name, hour=10):
        """The automatic responsible of one answered conversation, then out of it.

        It never acts itself, so it can be deleted afterwards.
        """

        former = self._user(name, "group_contact_center_agent")
        self.account.write(
            {"access_user_ids": [(4, former.id)], "auto_assignment_user_id": former.id}
        )
        channel = self.conversation_answered_after(60, start=at(hour))
        self.clock[0] = at(hour, 30)
        self.account.write(
            {"auto_assignment_user_id": False, "access_user_ids": [(3, former.id)]}
        )
        return former, channel

    def foreign_admin(self):
        other_company = self.env["res.company"].create({"name": "Outra empresa"})
        admin = self._user("Foreign", "group_contact_center_admin")
        admin.write(
            {"company_ids": [(6, 0, other_company.ids)], "company_id": other_company.id}
        )
        return admin, {"allowed_company_ids": other_company.ids}

    def test_responsible_choices_follow_the_viewer_scope(self):
        """L09-I2-01: grouping metadata never names a responsible out of scope."""

        former, _channel = self.former_responsible("Former")
        former_id = former.id
        former.unlink()
        key, removed = str(former_id), "Removed user (#%s)" % former_id
        static = [("unassigned", "No responsible"), ("unknown", "Unknown responsible")]
        foreign_admin, foreign_context = self.foreign_admin()
        viewers = (
            (self.supervisor, {}, True),
            (self.admin, {}, True),
            (self.outsider, {}, False),
            (foreign_admin, foreign_context, False),
        )
        self.env.flush_all()
        for model_name in (
            "contact.center.attendance.episode",
            "contact.center.attendance.message",
        ):
            for viewer, context, in_scope in viewers:
                model = self.env[model_name].with_user(viewer).with_context(**context)
                selection = model.fields_get(["responsible_group"])[
                    "responsible_group"
                ]["selection"]
                if in_scope:
                    self.assertEqual(dict(selection)[key], removed, viewer.name)
                else:
                    self.assertEqual(selection, static, viewer.name)
            # An agent cannot even describe the key, nor compute its choices.
            agent_model = self.env[model_name].with_user(self.agent_a)
            self.assertNotIn("responsible_group", agent_model.fields_get())
            self.assertEqual(agent_model._selection_responsible_group(), static)
        report = self.env["contact.center.attendance.report"]
        for viewer, context, in_scope in viewers:
            options = (
                report.with_user(viewer)
                .with_context(**context)
                .get_report({"date_from": str(DAY), "date_to": str(DAY)})["options"]
            )
            self.assertEqual(
                {"id": former_id, "name": removed} in options["responsibles"],
                in_scope,
                viewer.name,
            )

    def test_report_options_keep_former_responsibles_and_archived_inboxes(self):
        """L09-I2-03: whoever still owns history in the report stays selectable."""

        deleted, _channel = self.former_responsible("Deleted", 9)
        archived, _channel = self.former_responsible("Archived", 10)
        demoted, _channel = self.former_responsible("Demoted", 11)
        deleted_id = deleted.id
        deleted.unlink()
        archived.active = False
        demoted.groups_id = [(6, 0, self.env.ref("base.group_user").ids)]
        self.clock[0] = at(13)
        self.inbound(self.remote(), at(13), connection=self.other_connection)
        (self.other_account | self.quiet_account).write({"active": False})

        options = self.report(user=self.admin)["options"]
        responsibles = {item["id"]: item["name"] for item in options["responsibles"]}
        self.assertEqual(responsibles[deleted_id], "Removed user (#%s)" % deleted_id)
        self.assertEqual(responsibles[archived.id], archived.display_name)
        self.assertEqual(responsibles[demoted.id], demoted.display_name)
        self.assertEqual(responsibles[self.agent_b.id], self.agent_b.display_name)
        accounts = [(item["id"], item["name"]) for item in options["accounts"]]
        self.assertIn((self.account.id, self.account.display_name), accounts)
        # Archived inboxes come last, marked as such.
        self.assertEqual(
            set(accounts[-2:]),
            {
                (account.id, "%s (archived)" % account.display_name)
                for account in self.other_account | self.quiet_account
            },
        )
        # Each historic choice filters the report as it is offered.
        for user_id in (deleted_id, archived.id, demoted.id):
            total = self.report(user=self.admin, responsible_ids=[user_id])["total"]
            self.assertEqual(total["episodes"], 1, responsibles[user_id])
        archived_inbox = self.report(
            user=self.admin, account_ids=self.other_account.ids
        )
        self.assertEqual(
            [row["key"] for row in archived_inbox["groups"]], self.other_account.ids
        )
        self.assertEqual(archived_inbox["total"]["episodes"], 1)
        # A selected archived inbox without data still reads "sem dados".
        quiet = self.report(user=self.admin, account_ids=self.quiet_account.ids)
        self.assertEqual(
            [row["key"] for row in quiet["groups"]], self.quiet_account.ids
        )
        self.assertEqual(quiet["groups"][0]["episodes"], 0)

    def test_inbox_created_after_the_publication_has_complete_history(self):
        """L09-R5: only conversations that predate the publication lack history."""

        legacy = self.conversation_answered_after(60, start=at(9))
        self.env.flush_all()
        self.env.cr.execute(
            "DELETE FROM contact_center_conversation_event WHERE channel_id = %s",
            [legacy.id],
        )
        self.env.cr.execute(
            "UPDATE mail_channel SET contact_center_lifecycle_seq = 0 WHERE id = %s",
            [legacy.id],
        )
        self.env.invalidate_all()
        self.clock[0] = at(11)
        self.event_model._contact_center_record_migration_baselines()
        self.clock[0] = at(12)
        self.inbound(self.remote(), at(12), connection=self.other_connection)

        new_inbox = self.report(account_ids=self.other_account.ids)
        self.assertEqual(new_inbox["history_since"], "%s 11:00:00" % DAY)
        self.assertFalse(new_inbox["partial_history"])
        self.assertEqual(new_inbox["total"]["episodes"], 1)
        published = self.report(account_ids=self.account.ids)
        self.assertTrue(published["partial_history"])
        after_publication = self.report(
            account_ids=self.account.ids,
            date_from=str(fields.Date.today()),
            date_to=str(fields.Date.today()),
        )
        self.assertFalse(after_publication["partial_history"])

    def test_before_history_episodes_stay_out_of_the_response_medians(self):
        """L09-T3: an answered baseline-cycle episode is counted, not measured."""

        self.conversation_answered_after(60)
        legacy = self.conversation_answered_after(30, start=at(9))
        self.env.flush_all()
        self.env.cr.execute(
            "DELETE FROM contact_center_conversation_event WHERE channel_id = %s",
            [legacy.id],
        )
        self.env.cr.execute(
            "UPDATE mail_channel SET contact_center_lifecycle_seq = 0 WHERE id = %s",
            [legacy.id],
        )
        self.env.cr.execute(
            "UPDATE contact_center_message_binding SET lifecycle_seq = NULL "
            "WHERE channel_binding_id IN (SELECT id FROM "
            "contact_center_channel_binding WHERE channel_id = %s)",
            [legacy.id],
        )
        self.env.invalidate_all()
        self.clock[0] = at(13)
        self.event_model._contact_center_record_migration_baselines()
        self.clock[0] = at(13, 10)
        self.inbound(self._channel_binding(legacy).conversation_ref, at(13, 10))
        self.clock[0] = at(13, 20)
        self.phone(legacy, at(13, 20))

        total = self.report(account_ids=self.account.ids)["total"]
        self.assertEqual(total["episodes"], 2)
        self.assertEqual(total["answered"], 2)
        self.assertEqual(total["before_history"], 1)
        self.assertEqual(total["first_response"]["measured"], 1)
        self.assertEqual(total["first_response"]["median"], 60)
        self.assertEqual(total["first_response"]["p90"], 60)
        self.assertEqual(total["subsequent_response"]["measured"], 0)
        self.assertIsNone(total["subsequent_response"]["median"])

    def test_group_transfers_and_reopenings_are_not_counted(self):
        """L09-R1: every column of the report counts direct conversations only."""

        account = self._account("Grupos", group_inbound_enabled=True)
        connection = self._connection(account)
        group_ref = "1203633%s@g.us" % (uuid.uuid4().int % 10**9)

        def group_message(sent_at):
            sender = "700003%s@lid" % (uuid.uuid4().int % 10**9)
            event = EventDTO.from_dict(
                {
                    "schema_version": 1,
                    "provider_schema_version": "fixture-v1",
                    "event_id": "attendance-group-%s" % uuid.uuid4(),
                    "event_type": "message.created",
                    "occurred_at": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
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
                                "confidence": "protocol",
                            }
                        ],
                    },
                    "extensions": {"conversation_name": "Grupo"},
                    "message": {
                        "external_message_id": "attendance-group-%s" % uuid.uuid4(),
                        "content_type": "text",
                        "text": "Mensagem no grupo",
                    },
                }
            )
            return self._binding_of(
                self.application.with_context(
                    contact_center_skip_enqueue=True
                )._process_event(connection, event)
            )

        self.clock[0] = at(10)
        channel = self.channel_of(group_message(at(10)))
        self.claim(channel, self.agent_a)
        self.clock[0] = at(10, 1)
        self.set_state(channel, "resolved")
        self.clock[0] = at(10, 2)
        group_message(at(10, 2))
        self.clock[0] = at(10, 3)
        self.assign(channel, self.agent_b)
        event_types = self.events(channel).mapped("event_type")
        self.assertIn("reopened", event_types)
        self.assertEqual(self.events(channel)[-1].previous_responsible_id, self.agent_a)

        total = self.report(account_ids=account.ids)["total"]
        self.assertEqual(
            (total["transfers"], total["reopenings"], total["episodes"]), (0, 0, 0)
        )
        self.assertEqual(self.report(account_ids=account.ids)["history_since"], False)

    def test_outbound_counts_delivered_sends_and_phone_replies(self):
        """L09-R4: undelivered attempts and failed resends are not "saídas"."""

        self.clock[0] = at(10)
        channel = self.channel_of(self.inbound(self.remote(), at(10)))
        self.deliver(self.send(channel), at(10, 1))
        failed = self.send(channel)
        outbox = (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search([("message_binding_id", "=", failed.id)])
        )
        outbox._finish_failure("dead", ValidationError("Rejected by provider"))
        retried = (
            self.api(self.agent_a)
            .with_context(contact_center_skip_enqueue=True)
            .resend_message(channel.id, failed.message_id.id, str(uuid.uuid4()))
        )
        resend = self._binding_of(
            self.env["mail.message"].browse(retried["message_id"])
        )
        self.send(channel)  # queued, never sent
        self.deliver(self.automation(channel), at(10, 2))
        self.automation(channel)  # queued automation
        self.phone(channel, at(10, 3))
        self.assertEqual(self.report()["total"]["outbound"], 3)
        self.deliver(resend, at(10, 4))
        self.assertEqual(self.report()["total"]["outbound"], 4)


def _period_start(result):
    return fields.Datetime.to_datetime(result["period"]["start"])
