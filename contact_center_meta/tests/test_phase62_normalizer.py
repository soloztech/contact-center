import copy

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    UnsupportedEventError,
)

from ..services.messaging import atomic_events, sanitize_webhook_envelope
from .common import MetaCase


class TestMetaPhase62Normalizer(MetaCase):
    def _events(self, fixture_name):
        envelope = sanitize_webhook_envelope(self.load_fixture(fixture_name))
        return list(atomic_events(envelope))

    def _instagram_connection(self):
        account = self._create_account(
            "instagram",
            self.INSTAGRAM_ID,
            team=self.team,
        )
        return self._create_connection(
            account,
            self.instagram_asset,
        )

    def test_messenger_inbound_text_and_referral_use_provider_neutral_contracts(self):
        atomic = self._events("messenger_phase62.json")[0]

        event = self.connection.get_adapter().normalize_event(self.connection, atomic)

        self.assertEqual(event.provider_schema_version, "meta.messaging.v2")
        self.assertEqual(event.event_id, "Message:m_phase62_messenger_inbound")
        self.assertEqual(event.event_type, "message.created")
        self.assertAlmostEqual(event.occurred_at.timestamp(), 1787603000.1, places=3)
        self.assertEqual(event.account_ref, self.account.external_ref)
        self.assertEqual(event.connection_ref, self.connection.external_ref)
        self.assertEqual(event.conversation_ref, "900000000000020")
        self.assertEqual(event.platform, "messenger")
        self.assertEqual(event.direction, "inbound")
        self.assertFalse(event.is_from_me)
        self.assertEqual(event.origin, "provider")
        self.assertEqual(event.conversation.conversation_type, "direct")

        actor_address = event.actor.addresses[0]
        conversation_address = event.conversation.addresses[0]
        for address in (actor_address, conversation_address):
            self.assertEqual(address.namespace, "meta.messenger.psid")
            self.assertEqual(address.value, "900000000000020")
            self.assertEqual(address.value_normalized, "900000000000020")
            self.assertEqual(address.confidence, "protocol")
            self.assertEqual(address.resolution_scope, "account")
        self.assertEqual(actor_address.role, "sender")
        self.assertEqual(conversation_address.role, "primary")

        self.assertEqual(
            event.message.external_message_id,
            "m_phase62_messenger_inbound",
        )
        self.assertEqual(event.message.content_type, "text")
        self.assertEqual(event.message.text, "Synthetic Messenger inbound")
        self.assertFalse(event.message.reply_to_external_id)
        self.assertFalse(event.reply_to)
        self.assertEqual(
            event.extensions["provider.meta"],
            {
                "graph_api_version": "v26.0",
                "object": "page",
                "transport_mode": "messenger_page",
                "message_kind": "message",
            },
        )

        self.assertEqual(len(event.attribution), 1)
        attribution = event.attribution[0]
        self.assertEqual(attribution.touchpoint_type, "paid_ad_click")
        self.assertEqual(attribution.evidence_level, "provider_asserted")
        self.assertEqual(attribution.network, "meta")
        self.assertEqual(attribution.source_platform, "messenger")
        self.assertEqual(attribution.source_type, "ad")
        self.assertEqual(attribution.entry_point, {"source": "open_thread"})
        self.assertEqual(
            {
                (identifier.namespace, identifier.role, identifier.value)
                for identifier in attribution.external_identifiers
            },
            {
                ("meta.ad_id", "ad_source", "phase62-meta-ad"),
                ("meta.ref", "entry_reference", "phase62-campaign-ref"),
            },
        )

    def test_malformed_optional_referral_degrades_to_message_without_attribution(self):
        atomic = copy.deepcopy(self._events("messenger_phase62.json")[0])
        atomic["messaging"]["message"]["referral"] = {
            "source": "ADS",
            "ref": "campaign\tcontrol",
        }

        event = self.connection.get_adapter().normalize_event(self.connection, atomic)

        self.assertEqual(event.message.text, "Synthetic Messenger inbound")
        self.assertFalse(event.attribution)

    def test_messenger_standalone_referral_is_attribution_observation(self):
        atomic = self._events("messenger_referral.json")[0]

        event = self.connection.get_adapter().normalize_event(self.connection, atomic)

        self.assertEqual(event.event_type, "attribution.observed")
        self.assertTrue(event.event_id.startswith("Referral:"))
        self.assertAlmostEqual(event.occurred_at.timestamp(), 1787680800.1, places=3)
        self.assertEqual(event.conversation_ref, "900000000000040")
        self.assertEqual(event.direction, "inbound")
        self.assertFalse(event.is_from_me)
        self.assertEqual(event.origin, "provider")
        self.assertFalse(event.message)
        self.assertEqual(event.actor.addresses[0].role, "sender")
        self.assertEqual(event.actor.addresses[0].namespace, "meta.messenger.psid")
        self.assertEqual(event.conversation.addresses[0].role, "primary")
        self.assertEqual(event.extensions["provider.meta"]["event_kind"], "referral")
        attribution = event.attribution[0]
        self.assertEqual(attribution.touchpoint_type, "paid_ad_click")
        self.assertEqual(attribution.evidence_level, "provider_asserted")
        self.assertEqual(attribution.source_type, "ad")
        self.assertEqual(
            {
                (item.namespace, item.value, item.source_field)
                for item in attribution.external_identifiers
            },
            {
                (
                    "meta.ad_id",
                    "phase62-messenger-ad",
                    "messaging.referral.ad_id",
                ),
                (
                    "meta.ref",
                    "phase62-messenger-standalone",
                    "messaging.referral.ref",
                ),
            },
        )

    def test_instagram_standalone_referral_canonicalizes_shortlinks(self):
        connection = self._instagram_connection()
        atomic = self._events("instagram_referral.json")[0]

        event = connection.get_adapter().normalize_event(connection, atomic)

        self.assertEqual(event.event_type, "attribution.observed")
        self.assertEqual(event.platform, "instagram")
        self.assertEqual(event.conversation_ref, "800000000000040")
        self.assertFalse(event.message)
        self.assertEqual(event.actor.addresses[0].namespace, "meta.instagram.igsid")
        attribution = event.attribution[0]
        self.assertEqual(attribution.touchpoint_type, "entry_point")
        self.assertEqual(attribution.evidence_level, "provider_hint")
        self.assertEqual(attribution.source_type, "shortlink")
        self.assertEqual(attribution.entry_point, {"source": "open_thread"})
        identifier = attribution.external_identifiers[0]
        self.assertEqual(identifier.namespace, "meta.ref")
        self.assertEqual(identifier.source_field, "messaging.referral.ref")

    def test_standalone_referral_fails_closed_on_invalid_or_compound_shape(self):
        valid = self._events("messenger_referral.json")[0]
        cases = []

        invalid_route = copy.deepcopy(valid)
        invalid_route["messaging"]["recipient"]["id"] = "777777777777777"
        cases.append((invalid_route, AdapterError))

        empty = copy.deepcopy(valid)
        empty["messaging"]["referral"] = {}
        cases.append((empty, UnsupportedEventError))

        compound = copy.deepcopy(valid)
        compound["messaging"]["message"] = {
            "mid": "m_compound_referral",
            "text": "Compound",
        }
        cases.append((compound, UnsupportedEventError))

        for atomic, expected_error in cases:
            with self.subTest(atomic=atomic), self.assertRaises(expected_error):
                self.connection.get_adapter().normalize_event(self.connection, atomic)

    def test_messenger_reply_and_echo_preserve_correlation_and_remote_person(self):
        _inbound, reply_atomic, echo_atomic = self._events("messenger_phase62.json")
        adapter = self.connection.get_adapter()

        reply = adapter.normalize_event(self.connection, reply_atomic)
        echo = adapter.normalize_event(self.connection, echo_atomic)

        self.assertEqual(
            reply.message.reply_to_external_id,
            "m_phase62_messenger_inbound",
        )
        self.assertEqual(
            reply.reply_to,
            {"external_message_id": "m_phase62_messenger_inbound"},
        )
        self.assertEqual(
            reply.message.protocol_snapshot["reply_to"],
            {"external_message_id": "m_phase62_messenger_inbound"},
        )
        self.assertEqual(echo.conversation_ref, "900000000000020")
        self.assertEqual(echo.direction, "outbound")
        self.assertTrue(echo.is_from_me)
        self.assertEqual(echo.origin, "external_device")
        self.assertEqual(echo.message.external_message_id, "m_phase62_messenger_echo")
        self.assertEqual(echo.message.protocol_snapshot["is_echo"], True)
        self.assertEqual(echo.actor.addresses[0].namespace, "meta.messenger.page")
        self.assertEqual(echo.actor.addresses[0].value, self.ACTIVE_PAGE_ID)
        self.assertEqual(
            echo.conversation.addresses[0].namespace,
            "meta.messenger.psid",
        )
        self.assertEqual(echo.conversation.addresses[0].value, "900000000000020")
        self.assertEqual(
            echo.extensions["provider.meta"]["message_kind"],
            "echo",
        )

    def test_instagram_inbound_and_echo_use_igsid_and_account_namespaces(self):
        connection = self._instagram_connection()
        inbound_atomic, echo_atomic = self._events("instagram_phase62.json")
        adapter = connection.get_adapter()

        inbound = adapter.normalize_event(connection, inbound_atomic)
        echo = adapter.normalize_event(connection, echo_atomic)

        self.assertEqual(inbound.platform, "instagram")
        self.assertEqual(inbound.conversation_ref, "800000000000020")
        self.assertEqual(inbound.direction, "inbound")
        self.assertFalse(inbound.is_from_me)
        self.assertEqual(inbound.actor.addresses[0].namespace, "meta.instagram.igsid")
        self.assertEqual(
            inbound.conversation.addresses[0].namespace,
            "meta.instagram.igsid",
        )
        self.assertEqual(echo.direction, "outbound")
        self.assertTrue(echo.is_from_me)
        self.assertEqual(echo.origin, "external_device")
        self.assertEqual(echo.actor.addresses[0].namespace, "meta.instagram.account")
        self.assertEqual(echo.actor.addresses[0].value, self.INSTAGRAM_ID)
        self.assertEqual(echo.conversation.addresses[0].value, "800000000000020")

    def test_endpoint_direction_contradictions_fail_closed(self):
        inbound_atomic, _reply_atomic, echo_atomic = self._events(
            "messenger_phase62.json"
        )
        contradictions = []

        inbound_marked_echo = copy.deepcopy(inbound_atomic)
        inbound_marked_echo["messaging"]["message"]["is_echo"] = True
        contradictions.append(inbound_marked_echo)

        echo_without_flag = copy.deepcopy(echo_atomic)
        echo_without_flag["messaging"]["message"].pop("is_echo")
        contradictions.append(echo_without_flag)

        self_endpoint = copy.deepcopy(inbound_atomic)
        self_endpoint["messaging"]["sender"]["id"] = self.ACTIVE_PAGE_ID
        contradictions.append(self_endpoint)

        for atomic in contradictions:
            with self.subTest(atomic=atomic), self.assertRaisesRegex(
                AdapterError,
                "contradict",
            ):
                self.connection.get_adapter().normalize_event(self.connection, atomic)

    def test_text_survives_unsupported_provider_content(self):
        base = self._events("messenger_phase62.json")[0]
        base["messaging"]["message"].pop("referral")
        cases = {}

        media = copy.deepcopy(base)
        media["messaging"]["message"]["attachments"] = [
            {"type": "image", "payload": {"sticker_id": "phase62-sticker"}}
        ]
        cases["media"] = media

        for provider_kind in ("fallback", "location"):
            unsupported_attachment = copy.deepcopy(base)
            unsupported_attachment["messaging"]["message"]["attachments"] = [
                {"type": provider_kind, "payload": {}}
            ]
            cases[provider_kind] = unsupported_attachment

        deleted = copy.deepcopy(base)
        deleted["messaging"]["message"]["is_deleted"] = True
        cases["delete"] = deleted

        self_message = copy.deepcopy(base)
        self_message["messaging"]["message"]["is_self"] = True
        cases["is_self"] = self_message

        story = copy.deepcopy(base)
        story["messaging"]["message"]["reply_to"] = {"story": {"id": "phase62-story"}}
        cases["story"] = story

        marked_unsupported = copy.deepcopy(base)
        marked_unsupported["messaging"]["message"]["is_unsupported"] = True
        cases["provider_unsupported"] = marked_unsupported

        for name in (
            "media",
            "fallback",
            "location",
            "story",
            "provider_unsupported",
        ):
            with self.subTest(name=name):
                event = self.connection.get_adapter().normalize_event(
                    self.connection, cases[name]
                )
                self.assertEqual(event.message.content_type, "text")
                self.assertEqual(event.message.text, "Synthetic Messenger inbound")
                self.assertFalse(event.message.media)
                self.assertIn(
                    event.extensions["provider.meta"]["unsupported_content"],
                    ("content", "media", "provider_unsupported", "self_message"),
                )
                self.assertNotIn(
                    "private_locator_ref",
                    str(event.to_dict()),
                )

        for name in ("delete", "is_self"):
            with self.subTest(name=name), self.assertRaises(UnsupportedEventError):
                self.connection.get_adapter().normalize_event(
                    self.connection, cases[name]
                )

        without_text = copy.deepcopy(media)
        without_text["messaging"]["message"].pop("text")
        with self.assertRaises(UnsupportedEventError):
            self.connection.get_adapter().normalize_event(self.connection, without_text)

    def test_invalid_message_lifecycle_flags_quarantine_only_the_item(self):
        for flag in ("is_echo", "is_self", "is_deleted", "is_unsupported"):
            envelope = self.load_fixture("messenger_phase62.json")
            envelope["entry"][0]["messaging"][0]["message"][flag] = "true"
            sanitized = sanitize_webhook_envelope(envelope)
            atomic = list(atomic_events(sanitized))[0]
            rejection = atomic["messaging"]["message"]["sanitization_rejections"]
            self.assertEqual(
                rejection,
                [{"slot": "message", "reason": "invalid_%s" % flag}],
            )
            with self.subTest(flag=flag), self.assertRaises(UnsupportedEventError):
                self.connection.get_adapter().normalize_event(self.connection, atomic)

    def test_invalid_optional_media_name_does_not_discard_valid_media(self):
        atomic = copy.deepcopy(self._events("messenger_phase62.json")[0])
        message = atomic["messaging"]["message"]
        message["attachments"] = [
            {
                "type": "image",
                "payload": {"private_locator_ref": "a" * 64},
            }
        ]
        message["sanitization_rejections"] = [
            {"slot": "attachment:0", "reason": "invalid_attachment_name"}
        ]

        event = self.connection.get_adapter().normalize_event(self.connection, atomic)

        self.assertEqual(event.message.content_type, "image")
        self.assertEqual(event.message.text, "Synthetic Messenger inbound")
        self.assertEqual(len(event.message.media), 1)
        self.assertEqual(
            event.message.media[0].remote_locator,
            {"private_locator_ref": "a" * 64},
        )

    def test_media_with_referral_preserves_text_and_attribution_evidence(self):
        atomic = self._events("messenger_batch.json")[0]

        event = self.connection.get_adapter().normalize_event(self.connection, atomic)

        self.assertEqual(event.message.external_message_id, "m_synthetic_active")
        self.assertEqual(event.message.content_type, "text")
        self.assertEqual(event.message.text, "Synthetic Messenger message")
        self.assertFalse(event.message.media)
        self.assertEqual(len(event.attribution), 1)
        self.assertEqual(event.attribution[0].touchpoint_type, "paid_ad_click")
        self.assertEqual(event.attribution[0].network, "meta")
        self.assertEqual(
            event.extensions["provider.meta"]["unsupported_content"],
            "media",
        )
