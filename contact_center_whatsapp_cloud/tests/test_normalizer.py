import base64
import copy
import json
import os

from odoo.addons.contact_center_base.services.adapter import UnsupportedEventError

from ..services.normalizer import UNSUPPORTED_TEXT
from .common import WhatsAppCloudCase


def _fixture(name):
    path = os.path.join(os.path.dirname(__file__), "fixtures", name)
    with open(path, encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


class TestWhatsAppCloudNormalizer(WhatsAppCloudCase):
    _probe = 0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.catalog = _fixture("inbound_messages.json")

    def _message(self, name, **values):
        message = self.text_message(**values)
        message.pop("text")
        message.update(copy.deepcopy(self.catalog[name]))
        return message

    def _payload(self, message, *, contacts=None):
        envelope = self.envelope(self.value(messages=[message], contacts=contacts))
        # A distinct body per probe: the real pipeline may receive it again.
        TestWhatsAppCloudNormalizer._probe += 1
        envelope["entry"][0]["time"] = 1_700_000_000 + self._probe
        delivery = self.create_delivery(envelope)
        item = delivery.item_ids.ensure_one()
        self.assertEqual(item.kind, "messaging")
        return item.payload_json

    def _event(self, message, **kwargs):
        payload = self._payload(message, **kwargs)
        return self.connection.get_adapter().normalize_event(self.connection, payload)

    # -- 4: every message type ----------------------------------------------------

    def test_text_message_identity_profile_and_timestamps(self):
        message = self._message("text", timestamp=1_800_000_000)
        event = self._event(message)
        self.assertEqual(event.event_type, "message.created")
        self.assertEqual(event.event_id, "Message:%s" % message["id"])
        self.assertEqual(event.message.content_type, "text")
        self.assertEqual(event.message.text, "Bom dia! Vocês entregam em Campinas?")
        self.assertEqual(event.message.external_message_id, message["id"])
        self.assertEqual(int(event.occurred_at.timestamp()), 1_800_000_000)
        self.assertEqual(event.direction, "inbound")
        self.assertFalse(event.is_from_me)
        self.assertEqual(event.actor.display_name, "Cliente Sintético")
        self.assertEqual(event.conversation_ref, "%s@s.whatsapp.net" % self.CUSTOMER)
        namespaces = [
            (address.namespace, address.role)
            for address in event.conversation.addresses
        ]
        self.assertEqual(
            namespaces, [("whatsapp.pn", "primary"), ("whatsapp.bsuid", "alternate")]
        )
        self.assertEqual(
            event.message.protocol_snapshot["phone_number_id"], self.PHONE_ID
        )

    def test_media_messages_carry_provider_id_mime_and_sha256(self):
        expectations = {
            "image": ("image", "image", "image/jpeg", "Foto do defeito"),
            "audio": ("audio", "audio", "audio/ogg", ""),
            "video": ("video", "video", "video/mp4", ""),
            "document": ("document", "document", "application/pdf", "Pedido de compra"),
            "sticker": ("image", "sticker", "image/webp", "[Figurinha]"),
        }
        for name, (kind, content_type, mime_type, text) in expectations.items():
            with self.subTest(name=name):
                message = self._message(name)
                provider = self.catalog[name][name]
                event = self._event(message)
                media = event.message.media[0]
                self.assertEqual(len(event.message.media), 1)
                self.assertEqual(media.kind, kind)
                self.assertEqual(event.message.content_type, content_type)
                self.assertEqual(media.external_media_id, provider["id"])
                self.assertEqual(media.remote_locator, {"media_id": provider["id"]})
                self.assertEqual(media.mime_type, mime_type)
                raw_digest = provider["sha256"]
                expected_digest = (
                    raw_digest
                    if len(raw_digest) == 64
                    else base64.b64decode(raw_digest).hex()
                )
                self.assertEqual(media.sha256, expected_digest)
                self.assertEqual(event.message.text, text)
                if name == "audio":
                    self.assertTrue(media.is_voice_note)
                if name == "document":
                    self.assertEqual(media.file_name, "pedido-2026.pdf")

    def test_location_contacts_and_replies_use_structured_content(self):
        location = self._event(self._message("location"))
        self.assertEqual(
            location.message.structured_content,
            {
                "type": "location",
                "latitude": -23.5505,
                "longitude": -46.6333,
                "live": False,
                "name": "Fábrica Soloz",
                "address": "Av. Paulista, 1000",
            },
        )
        self.assertIn("Fábrica Soloz", location.message.text)
        location_payload = self._payload(self._message("location"))
        self.assertNotIn("maps.example.invalid", json.dumps(location_payload))

        contacts_payload = self._payload(self._message("contacts"))
        self.assertNotIn("example.invalid/profile", json.dumps(contacts_payload))
        contacts = self.connection.get_adapter().normalize_event(
            self.connection, contacts_payload
        )
        self.assertEqual(
            contacts.message.structured_content,
            {
                "type": "contacts",
                "contacts": [
                    {
                        "name": "Maria Compras",
                        "phones": ["+5511977776666"],
                        "emails": ["maria@example.invalid"],
                    }
                ],
            },
        )
        for name, expected in (
            ("button_reply", ("quote-yes", "Quero orçamento")),
            ("list_reply", ("product-42", "Painel solar 550 W")),
            ("template_button", ("CONFIRMAR_VISITA", "Confirmar visita")),
        ):
            with self.subTest(name=name):
                event = self._event(self._message(name))
                self.assertEqual(
                    event.message.structured_content,
                    {"type": "selection", "id": expected[0], "title": expected[1]},
                )
                self.assertEqual(event.message.text, expected[1])
                self.assertEqual(event.message.content_type, "selection")

    def test_system_notice_is_a_control_event_without_provider_body(self):
        message = self._message("system")
        payload = self._payload(message)
        self.assertEqual(payload["message"]["system"], {"type": "user_changed_number"})
        self.assertNotIn("96666-5555", json.dumps(payload))
        event = self.connection.get_adapter().normalize_event(self.connection, payload)
        self.assertEqual(event.event_type, "whatsapp_cloud.system")
        self.assertIsNone(event.message)
        self.assertEqual(
            event.extensions["whatsapp_system"]["system_type"], "user_changed_number"
        )

    def test_system_notice_projects_a_control_card_in_the_existing_conversation(self):
        first = self.text_message(body="Oi")
        self.deliver(self.envelope(self.value(messages=[first])))
        channel_binding = self.binding_for(first["id"]).channel_binding_id
        notice = self._message("system")
        _delivery, inbox = self.deliver(self.envelope(self.value(messages=[notice])))
        self.assertEqual(inbox.state, "done")
        card = self.env["contact.center.message.binding"].search(
            [
                ("channel_binding_id", "=", channel_binding.id),
                ("content_type", "=", "whatsapp.system"),
            ]
        )
        self.assertEqual(len(card), 1)
        self.assertIn("alterou o número", str(card.message_id.body))
        self.assertNotIn("96666", str(card.message_id.body))
        serialized = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._serialize_message(card.message_id, card)
        )
        self.assertFalse(serialized["actions"]["reply"])
        self.assertFalse(serialized["actions"]["react"])

    def test_unsupported_type_is_an_unsupported_message_with_its_code(self):
        message = self._message("unsupported")
        payload = self._payload(message)
        self.assertEqual(
            payload["message"]["errors"],
            [{"code": 131051, "title": "Message type unknown"}],
        )
        self.assertNotIn("currently not supported", json.dumps(payload))
        event = self.connection.get_adapter().normalize_event(self.connection, payload)
        self.assertEqual(event.message.content_type, "unsupported")
        self.assertEqual(event.message.text, UNSUPPORTED_TEXT)
        extension = event.extensions["provider.whatsapp_cloud"]
        self.assertEqual(extension["provider_error_codes"], [131051])
        _delivery, inbox = self.deliver(self.envelope(self.value(messages=[message])))
        self.assertEqual(inbox.state, "done")
        self.assertIn(
            "não suportado", str(self.binding_for(message["id"]).message_id.body)
        )

    def test_reply_context_and_forwarded_flag(self):
        target = self.text_message(body="Mensagem original")
        self.deliver(self.envelope(self.value(messages=[target])))
        reply = self.text_message(
            body="Respondendo",
            context={"from": self.CUSTOMER, "id": target["id"], "forwarded": True},
        )
        event = self._event(reply)
        self.assertEqual(event.message.reply_to_external_id, target["id"])
        self.assertEqual(event.reply_to, {"external_message_id": target["id"]})
        self.assertTrue(event.message.is_forwarded)
        self.deliver(self.envelope(self.value(messages=[reply])))
        binding = self.binding_for(reply["id"])
        self.assertEqual(binding.reply_to_binding_id, self.binding_for(target["id"]))

    def test_media_and_structured_types_project_through_the_inbox(self):
        for name in ("image", "document", "location", "contacts", "list_reply"):
            with self.subTest(name=name):
                message = self._message(name)
                _delivery, inbox = self.deliver(
                    self.envelope(self.value(messages=[message]))
                )
                self.assertEqual(inbox.state, "done", inbox.last_error_message)
                binding = self.binding_for(message["id"])
                self.assertEqual(len(binding), 1)
                if name in ("image", "document"):
                    media = binding.media_ids.ensure_one()
                    self.assertEqual(media.state, "pending")
                    self.assertEqual(
                        media.remote_locator_json,
                        {"media_id": self.catalog[name][name]["id"]},
                    )
                else:
                    self.assertEqual(
                        binding.structured_content_json["type"],
                        {"location": "location", "contacts": "contacts"}.get(
                            name, "selection"
                        ),
                    )

    def test_failed_status_is_never_a_normalized_delivery_state(self):
        payload = self._status_payload("failed")
        with self.assertRaises(UnsupportedEventError):
            self.connection.get_adapter().normalize_event(self.connection, payload)

    def _status_payload(self, status):
        delivery = self.create_delivery(
            self.envelope(self.value(statuses=[self.status(self.wamid(), status)]))
        )
        return delivery.item_ids.ensure_one().payload_json

    # -- 4b: reactions ------------------------------------------------------------

    def _reaction(self, target, emoji, **values):
        return self.typed_message(
            "reaction", {"message_id": target, "emoji": emoji}, **values
        )

    def _mutations(self, target_binding):
        return (
            self.env["contact.center.message.mutation"]
            .sudo()
            .search([("target_message_binding_id", "=", target_binding.id)])
        )

    def test_reaction_add_and_remove_follow_the_mutation_contract(self):
        target = self.text_message(body="Pode reagir")
        self.deliver(self.envelope(self.value(messages=[target])))
        target_binding = self.binding_for(target["id"])
        add = self._reaction(target["id"], "👍")
        event = self._event(add)
        self.assertEqual(event.event_type, "message.reaction")
        self.assertEqual(
            event.mutation,
            {
                "type": "react",
                "target_external_message_id": target["id"],
                "operation": "add",
                "emoji": "👍",
            },
        )
        _delivery, inbox = self.deliver(self.envelope(self.value(messages=[add])))
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        added = self._mutations(target_binding)
        self.assertEqual(len(added), 1)
        self.assertEqual(added.reaction_operation, "add")
        self.assertEqual(added.reaction_emoji, "👍")

        remove = self._reaction(target["id"], "")
        removal = self._event(remove)
        self.assertEqual(removal.mutation["operation"], "remove")
        self.assertEqual(removal.mutation["emoji"], "")
        _delivery, inbox = self.deliver(self.envelope(self.value(messages=[remove])))
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        self.assertEqual(
            sorted(self._mutations(target_binding).mapped("reaction_operation")),
            ["add", "remove"],
        )

    def test_duplicate_reaction_is_applied_once(self):
        target = self.text_message(body="Uma reação só")
        self.deliver(self.envelope(self.value(messages=[target])))
        reaction = self._reaction(target["id"], "❤️")
        _delivery, first = self.deliver(self.envelope(self.value(messages=[reaction])))
        envelope = self.envelope(self.value(messages=[reaction]))
        envelope["entry"][0]["time"] = 1_900_000_000
        _delivery, second = self.deliver(envelope)
        self.assertEqual(first, second)
        self.assertEqual(len(self._mutations(self.binding_for(target["id"]))), 1)

    def test_reaction_before_its_target_is_retried_then_applied(self):
        target = self.text_message(body="Chegou depois")
        reaction = self._reaction(target["id"], "🙏")
        delivery = self.create_delivery(self.envelope(self.value(messages=[reaction])))
        inbox = self.inbox_from(self.dispatch_all(delivery)[0])
        inbox.sudo().write({"queue_job_uuid": "11111111-1111-4111-8111-111111111111"})
        from odoo.addons.queue_job.exception import RetryableJobError

        with self.assertRaises(RetryableJobError):
            inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
        self.deliver(self.envelope(self.value(messages=[target])))
        self.process_inbox(inbox)
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        self.assertEqual(len(self._mutations(self.binding_for(target["id"]))), 1)
