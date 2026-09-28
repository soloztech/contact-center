from unittest import mock

from .common import GRAPH_PATCH, WhatsAppCloudCase
from .test_normalizer import _fixture
from .test_status import WhatsAppCloudOutboundMixin


class TestWhatsAppCloudAttendance(WhatsAppCloudOutboundMixin, WhatsAppCloudCase):
    """System notices are control cards for the attendance report."""

    def _placements(self, channel):
        self.env.flush_all()
        model = self.env["contact.center.attendance.message"].sudo()
        model.invalidate_model()
        return model.search([("channel_id", "=", channel.id)], order="id")

    def _episodes(self, channel):
        self.env.flush_all()
        model = self.env["contact.center.attendance.episode"].sudo()
        model.invalidate_model()
        return model.search([("channel_id", "=", channel.id)], order="start_at, id")

    def _answered_conversation(self, start):
        inbound = self.text_message(body="Pergunta do cliente", timestamp=start)
        self.deliver(self.envelope(self.value(messages=[inbound])))
        customer = self.binding_for(inbound["id"])
        channel = customer.channel_binding_id.channel_id
        outbox = self._send(channel)
        wamid = self.wamid("answer")
        with mock.patch(GRAPH_PATCH, return_value=self._accepted(wamid, self.CUSTOMER)):
            outbox._process_one()
        self.deliver(
            self.envelope(
                self.value(
                    statuses=[self.status(wamid, "delivered", timestamp=start + 10)]
                )
            )
        )
        return channel, customer

    def test_system_notice_opens_no_episode_and_is_not_customer_input(self):
        start = self.now() - 600
        channel, customer = self._answered_conversation(start)
        episode = self._episodes(channel).ensure_one()
        self.assertEqual(episode.outcome, "answered")
        self.assertEqual(episode.wait_seconds, 10)

        notice = self.text_message(timestamp=start + 300)
        notice.pop("text")
        notice.update(_fixture("inbound_messages.json")["system"])
        self.deliver(self.envelope(self.value(messages=[notice])))
        card = self.env["contact.center.message.binding"].search(
            [
                ("channel_binding_id", "=", customer.channel_binding_id.id),
                ("content_type", "=", "whatsapp.system"),
            ]
        )
        self.assertEqual(len(card), 1)
        self.assertEqual(
            self._placements(channel).mapped("kind"), ["customer", "agent", "control"]
        )
        self.assertEqual(self._episodes(channel), episode)

        # The next wait starts at the customer's own message, not at the notice.
        later = self.text_message(body="Mudei de número", timestamp=start + 400)
        self.deliver(self.envelope(self.value(messages=[later])))
        episodes = self._episodes(channel)
        self.assertEqual(len(episodes), 2)
        self.assertEqual(
            episodes[1].start_message_binding_id, self.binding_for(later["id"])
        )
        self.assertEqual(episodes[1].outcome, "pending")
