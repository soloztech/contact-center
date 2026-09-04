import copy
import json

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    UnsupportedEventError,
)

from ..services.messaging import atomic_events, sanitize_webhook_envelope
from .common import MetaCase


class TestMetaPhase63Normalizer(MetaCase):
    REMOTE_MESSENGER_ID = "900000000000063"
    REMOTE_INSTAGRAM_ID = "800000000000063"
    TIMESTAMP = 1787684400123

    def _instagram_connection(self):
        cached = getattr(self, "_phase63_instagram_connection", None)
        if cached:
            return cached
        account = self._create_account(
            "instagram",
            self.INSTAGRAM_ID,
            team=self.team,
        )
        connection = self._create_connection(
            account,
            self.instagram_asset,
        )
        self._phase63_instagram_connection = connection
        return connection

    def _raw_envelope(self, carrier, *, platform="messenger", from_me=False):
        if platform == "messenger":
            object_type = "page"
            asset_id = self.ACTIVE_PAGE_ID
            remote_id = self.REMOTE_MESSENGER_ID
        else:
            object_type = "instagram"
            asset_id = self.INSTAGRAM_ID
            remote_id = self.REMOTE_INSTAGRAM_ID
        sender_id, recipient_id = (
            (asset_id, remote_id) if from_me else (remote_id, asset_id)
        )
        return {
            "object": object_type,
            "entry": [
                {
                    "id": asset_id,
                    "time": self.TIMESTAMP,
                    "messaging": [
                        {
                            "sender": {"id": sender_id},
                            "recipient": {"id": recipient_id},
                            "timestamp": self.TIMESTAMP,
                            **carrier,
                        }
                    ],
                }
            ],
        }

    def _atomic(self, envelope, locator_references=None):
        sanitized = sanitize_webhook_envelope(
            envelope,
            private_locator_references=locator_references,
        )
        return list(atomic_events(sanitized))[0]

    def _normalize(
        self,
        carrier,
        *,
        platform="messenger",
        from_me=False,
        locator_references=None,
    ):
        connection = (
            self.connection if platform == "messenger" else self._instagram_connection()
        )
        envelope = self._raw_envelope(
            carrier,
            platform=platform,
            from_me=from_me,
        )
        return connection.get_adapter().normalize_event(
            connection,
            self._atomic(envelope, locator_references),
        )

    def test_media_uses_only_private_locators_and_preserves_attribution(self):
        locator_refs = {
            (0, "attachment:%s" % index): ("%x" % (index + 1)) * 64
            for index in range(4)
        }
        signed_urls = [
            "https://lookaside.example.invalid/media/%s?access_token=secret-%s"
            % (index, index)
            for index in range(4)
        ]
        attachments = [
            {
                "type": kind,
                "name": "../../guide.pdf" if kind == "file" else "%s.bin" % kind,
                "payload": {"url": signed_urls[index]},
            }
            for index, kind in enumerate(("image", "audio", "video", "file"))
        ]
        event = self._normalize(
            {
                "message": {
                    "mid": "m_phase63_media_bundle",
                    "text": "Synthetic media bundle",
                    "attachments": attachments,
                    "referral": {
                        "source": "ADS",
                        "type": "OPEN_THREAD",
                        "ad_id": "phase63-ad",
                    },
                }
            },
            locator_references=locator_refs,
        )

        self.assertEqual(event.event_type, "message.created")
        self.assertEqual(event.message.content_type, "media")
        self.assertEqual(event.message.text, "Synthetic media bundle")
        self.assertEqual(
            [media.kind for media in event.message.media],
            ["image", "audio", "video", "document"],
        )
        self.assertEqual(event.message.media[-1].file_name, "guide.pdf")
        for index, media in enumerate(event.message.media):
            self.assertEqual(
                media.external_media_id,
                "m_phase63_media_bundle:%s" % index,
            )
            self.assertEqual(
                media.remote_locator,
                {"private_locator_ref": locator_refs[(0, "attachment:%s" % index)]},
            )
        self.assertEqual(event.attribution[0].touchpoint_type, "paid_ad_click")
        serialized = json.dumps(event.to_dict(), sort_keys=True)
        for url in signed_urls:
            self.assertNotIn(url, serialized)
        self.assertNotIn("access_token", serialized)
        self.assertNotIn("secret-", serialized)

    def test_single_media_kind_becomes_message_content_type(self):
        locator_ref = "a" * 64
        event = self._normalize(
            {
                "message": {
                    "mid": "m_phase63_image",
                    "attachments": [
                        {
                            "type": "image",
                            "payload": {
                                "url": "https://lookaside.example.invalid/image"
                            },
                        }
                    ],
                }
            },
            locator_references={(0, "attachment:0"): locator_ref},
        )

        self.assertEqual(event.message.content_type, "image")
        self.assertFalse(event.message.text)
        self.assertEqual(len(event.message.media), 1)
        self.assertEqual(event.message.media[0].kind, "image")

    def test_quick_reply_is_visible_and_keeps_only_bounded_interaction_payload(self):
        event = self._normalize(
            {
                "message": {
                    "mid": "m_phase63_quick_reply",
                    "text": "Quero falar com vendas",
                    "quick_reply": {"payload": "QUEUE_SALES"},
                }
            }
        )

        self.assertEqual(event.message.content_type, "text")
        self.assertEqual(event.message.text, "Quero falar com vendas")
        self.assertEqual(
            event.message.protocol_snapshot["interaction"],
            {"type": "quick_reply", "payload": "QUEUE_SALES"},
        )

    def test_instagram_story_reply_keeps_private_reference_without_false_parent(self):
        locator_ref = "b" * 64
        event = self._normalize(
            {
                "message": {
                    "mid": "m_phase63_story_reply",
                    "text": "Gostei deste story",
                    "reply_to": {
                        "story": {
                            "id": "story_phase63",
                            "url": (
                                "https://lookaside.example.invalid/story"
                                "?token=private"
                            ),
                        }
                    },
                }
            },
            platform="instagram",
            locator_references={(0, "story"): locator_ref},
        )

        self.assertFalse(event.reply_to)
        self.assertFalse(event.message.reply_to_external_id)
        self.assertEqual(
            event.message.protocol_snapshot["story_reply"],
            {"id": "story_phase63", "private_locator_ref": locator_ref},
        )
        serialized = json.dumps(event.to_dict(), sort_keys=True)
        self.assertNotIn("lookaside", serialized)
        self.assertNotIn("token=private", serialized)

    def test_postback_is_provider_neutral_interactive_message(self):
        event = self._normalize(
            {
                "postback": {
                    "mid": "m_phase63_postback",
                    "title": "Consultar pedido",
                    "payload": "ORDER_STATUS:synthetic",
                    "referral": {
                        "source": "ADS",
                        "type": "OPEN_THREAD",
                        "ad_id": "phase63-postback-ad",
                    },
                }
            }
        )

        self.assertEqual(event.event_type, "message.created")
        self.assertEqual(event.message.content_type, "interactive")
        self.assertEqual(event.message.text, "Consultar pedido")
        self.assertEqual(
            event.message.protocol_snapshot["interaction"],
            {
                "type": "postback",
                "title": "Consultar pedido",
                "payload": "ORDER_STATUS:synthetic",
            },
        )
        self.assertEqual(event.attribution[0].touchpoint_type, "paid_ad_click")

    def test_remote_and_self_reactions_keep_actor_side_and_operation(self):
        remote = self._normalize(
            {
                "reaction": {
                    "mid": "m_phase63_reaction_target",
                    "action": "react",
                    "reaction": "love",
                    "emoji": "❤️",
                }
            }
        )
        own = self._normalize(
            {
                "reaction": {
                    "mid": "m_phase63_reaction_target",
                    "action": "unreact",
                    "reaction": "love",
                    "emoji": "",
                }
            },
            from_me=True,
        )

        self.assertEqual(remote.event_type, "message.reaction")
        self.assertEqual(remote.direction, "inbound")
        self.assertFalse(remote.is_from_me)
        self.assertEqual(remote.mutation["operation"], "add")
        self.assertEqual(remote.mutation["emoji"], "❤️")
        self.assertEqual(remote.actor.addresses[0].namespace, "meta.messenger.psid")
        self.assertEqual(own.direction, "outbound")
        self.assertTrue(own.is_from_me)
        self.assertEqual(own.origin, "external_device")
        self.assertEqual(own.mutation["operation"], "remove")
        self.assertEqual(own.actor.addresses[0].namespace, "meta.messenger.page")

    def test_delete_and_edit_are_platform_specific_and_monotonic(self):
        instagram_delete = self._normalize(
            {"message": {"mid": "m_phase63_delete", "is_deleted": True}},
            platform="instagram",
        )
        messenger_edit = self._normalize(
            {
                "message_edit": {
                    "mid": "m_phase63_edit_target",
                    "text": "Texto editado",
                    "num_edit": 2,
                }
            }
        )
        instagram_edit = self._normalize(
            {
                "message_edit": {
                    "mid": "m_phase63_instagram_edit",
                    "text": "Texto editado no Instagram",
                    "num_edit": 3,
                }
            },
            platform="instagram",
        )

        self.assertEqual(instagram_delete.event_type, "message.deleted")
        self.assertEqual(
            instagram_delete.mutation,
            {
                "type": "delete",
                "target_external_message_id": "m_phase63_delete",
                "operation": "delete",
            },
        )
        self.assertEqual(messenger_edit.event_type, "message.updated")
        self.assertEqual(messenger_edit.mutation["type"], "edit")
        self.assertEqual(messenger_edit.mutation["new_text"], "Texto editado")
        self.assertEqual(messenger_edit.mutation["provider_revision"], 2)
        self.assertEqual(instagram_edit.event_type, "message.updated")
        self.assertEqual(instagram_edit.mutation["provider_revision"], 3)

        with self.assertRaises(UnsupportedEventError):
            self._normalize(
                {"message": {"mid": "m_phase63_messenger_delete", "is_deleted": True}}
            )

    def test_delivery_and_read_differences_fail_closed_by_platform(self):
        messenger_delivery = self._normalize(
            {
                "delivery": {
                    "mids": ["m_phase63_sent", "m_phase63_sent"],
                    "watermark": self.TIMESTAMP,
                }
            }
        )
        messenger_read = self._normalize({"read": {"watermark": self.TIMESTAMP}})
        watermark_only_delivery = self._normalize(
            {"delivery": {"watermark": self.TIMESTAMP}}
        )
        instagram_read = self._normalize(
            {"read": {"mid": "m_phase63_ig_sent"}},
            platform="instagram",
        )

        self.assertEqual(messenger_delivery.event_type, "delivery.updated")
        self.assertEqual(messenger_delivery.direction, "outbound")
        self.assertFalse(messenger_delivery.is_from_me)
        self.assertEqual(
            messenger_delivery.delivery["external_message_ids"],
            ["m_phase63_sent"],
        )
        self.assertEqual(messenger_delivery.delivery["state"], "delivered")
        self.assertEqual(watermark_only_delivery.delivery["external_message_ids"], [])
        self.assertEqual(watermark_only_delivery.delivery["watermark"], self.TIMESTAMP)
        self.assertEqual(messenger_read.delivery["state"], "read")
        self.assertEqual(messenger_read.delivery["external_message_ids"], [])
        self.assertEqual(messenger_read.delivery["watermark"], self.TIMESTAMP)
        self.assertEqual(
            instagram_read.delivery["external_message_ids"],
            ["m_phase63_ig_sent"],
        )

        with self.assertRaises(UnsupportedEventError):
            self._normalize(
                {"delivery": {"mids": ["m_phase63_ig_sent"]}},
                platform="instagram",
            )
        with self.assertRaises(UnsupportedEventError):
            self._normalize({"read": {"mid": "m_phase63_wrong_read"}})
        with self.assertRaises(AdapterError):
            self._normalize(
                {"read": {"watermark": self.TIMESTAMP}},
                platform="instagram",
            )
        with self.assertRaises(AdapterError):
            self._normalize({"delivery": {"mids": []}})

    def test_sticker_transition_and_shared_posts_are_normalized_once(self):
        sticker = self._normalize(
            {
                "message": {
                    "mid": "m_phase63_literal_sticker",
                    "attachments": [
                        {
                            "type": "sticker",
                            "payload": {
                                "url": "https://lookaside.example.invalid/sticker"
                            },
                        }
                    ],
                }
            },
            locator_references={(0, "attachment:0"): "a" * 64},
        )
        duplicate_transition = self._normalize(
            {
                "message": {
                    "mid": "m_phase63_sticker_transition",
                    "attachments": [
                        {
                            "type": "image",
                            "payload": {
                                "url": "https://lookaside.example.invalid/image",
                                "sticker_id": "phase63-sticker",
                            },
                        },
                        {
                            "type": "sticker",
                            "payload": {
                                "url": "https://lookaside.example.invalid/sticker",
                                "sticker_id": "phase63-sticker",
                            },
                        },
                    ],
                }
            },
            locator_references={
                (0, "attachment:0"): "b" * 64,
                (0, "attachment:1"): "c" * 64,
            },
        )
        shared_post = self._normalize(
            {
                "message": {
                    "mid": "m_phase63_shared_post_transition",
                    "attachments": [
                        {
                            "type": "share",
                            "payload": {
                                "url": (
                                    "https://lookaside.example.invalid/share"
                                    "?token=secret"
                                )
                            },
                        },
                        {
                            "type": "ig_post",
                            "payload": {
                                "url": (
                                    "https://lookaside.example.invalid/post"
                                    "?token=secret"
                                )
                            },
                        },
                    ],
                }
            },
            platform="instagram",
            locator_references={
                (0, "attachment:0"): "d" * 64,
                (0, "attachment:1"): "e" * 64,
            },
        )
        messenger_share = self._normalize(
            {
                "message": {
                    "mid": "m_phase63_messenger_share",
                    "attachments": [{"type": "share", "payload": {}}],
                }
            },
        )

        self.assertEqual(sticker.message.content_type, "image")
        self.assertEqual([item.kind for item in sticker.message.media], ["image"])
        self.assertEqual(len(duplicate_transition.message.media), 1)
        self.assertEqual(shared_post.message.content_type, "interactive")
        self.assertEqual(shared_post.message.text, "Instagram post shared")
        self.assertEqual(messenger_share.message.text, "Facebook post shared")
        self.assertFalse(shared_post.message.media)
        self.assertEqual(
            shared_post.message.protocol_snapshot["interaction"],
            {"type": "shared_content", "content_kind": "social_post"},
        )
        serialized = json.dumps(shared_post.to_dict(), sort_keys=True)
        self.assertNotIn("lookaside.example.invalid", serialized)
        self.assertNotIn("token=secret", serialized)

        for provider_kind, content_kind, visible_text in (
            ("story_mention", "story_mention", "Instagram story mention"),
            ("ig_reel", "reel", "Instagram reel shared"),
            ("reel", "reel", "Instagram reel shared"),
        ):
            with self.subTest(provider_kind=provider_kind):
                social_event = self._normalize(
                    {
                        "message": {
                            "mid": "m_phase63_%s" % provider_kind,
                            "attachments": [
                                {
                                    "type": provider_kind,
                                    "payload": {
                                        "url": "https://lookaside.example.invalid/%s"
                                        % provider_kind
                                    },
                                }
                            ],
                        }
                    },
                    platform="instagram",
                    locator_references={(0, "attachment:0"): "f" * 64},
                )
                self.assertEqual(social_event.message.content_type, "interactive")
                self.assertEqual(social_event.message.text, visible_text)
                self.assertEqual(
                    social_event.message.protocol_snapshot["interaction"][
                        "content_kind"
                    ],
                    content_kind,
                )

    def test_compound_and_missing_private_media_contracts_fail_closed(self):
        compound = self._raw_envelope(
            {
                "message": {"mid": "m_phase63_compound", "text": "Compound"},
                "reaction": {
                    "mid": "m_phase63_target",
                    "action": "react",
                    "emoji": "👍",
                },
            }
        )
        missing_locator = self._raw_envelope(
            {
                "message": {
                    "mid": "m_phase63_missing_locator",
                    "attachments": [
                        {
                            "type": "image",
                            "payload": {
                                "url": "https://lookaside.example.invalid/missing"
                            },
                        }
                    ],
                }
            }
        )

        for envelope in (compound, missing_locator):
            with self.subTest(envelope=envelope), self.assertRaises(
                UnsupportedEventError
            ):
                atomic = self._atomic(envelope)
                self.connection.get_adapter().normalize_event(self.connection, atomic)

    def test_attribution_survives_media_without_downloadable_locator(self):
        envelope = self._raw_envelope(
            {
                "message": {
                    "mid": "m_phase63_attribution_only_media",
                    "attachments": [
                        {
                            "type": "image",
                            "payload": {"sticker_id": "synthetic-sticker"},
                        }
                    ],
                    "referral": {
                        "source": "ADS",
                        "type": "OPEN_THREAD",
                        "ad_id": "phase63-attribution-only",
                    },
                }
            }
        )

        event = self.connection.get_adapter().normalize_event(
            self.connection,
            self._atomic(envelope),
        )

        self.assertEqual(event.message.content_type, "unsupported")
        self.assertFalse(event.message.media)
        self.assertEqual(event.attribution[0].touchpoint_type, "paid_ad_click")
        self.assertEqual(
            event.extensions["provider.meta"]["unsupported_content"],
            "media",
        )

    def test_malformed_message_edit_revision_is_rejected(self):
        envelope = self._raw_envelope(
            {
                "message_edit": {
                    "mid": "m_phase63_bad_edit",
                    "text": "Bad revision",
                    "num_edit": 0,
                }
            }
        )
        atomic = self._atomic(envelope)

        with self.assertRaises(AdapterError):
            self.connection.get_adapter().normalize_event(self.connection, atomic)

        invalid_shape = copy.deepcopy(atomic)
        invalid_shape["messaging"]["message_edit"]["num_edit"] = "2"
        with self.assertRaises(AdapterError):
            self.connection.get_adapter().normalize_event(
                self.connection,
                invalid_shape,
            )
