from odoo.tests.common import BaseCase

from odoo.addons.meta_webhook_base.services.sanitizer import MAX_WEBHOOK_ENTRIES

from ..services.messaging import (
    atomic_dedupe_key,
    private_media_locators,
    sanitize_webhook_envelope,
)


class TestMetaMessagingContract(BaseCase):
    @staticmethod
    def _envelope(entry_count):
        return {
            "object": "page",
            "entry": [
                {
                    "id": str(index + 1),
                    "messaging": [],
                }
                for index in range(entry_count)
            ],
        }

    def test_shared_entry_limit_is_accepted_at_the_boundary(self):
        envelope = self._envelope(MAX_WEBHOOK_ENTRIES)

        sanitized = sanitize_webhook_envelope(envelope)
        locators = private_media_locators(envelope, "a" * 64)

        self.assertEqual(len(sanitized["entry"]), MAX_WEBHOOK_ENTRIES)
        self.assertEqual(locators, ())

    def test_shared_entry_limit_is_enforced_by_both_messaging_paths(self):
        envelope = self._envelope(MAX_WEBHOOK_ENTRIES + 1)

        for operation in (
            sanitize_webhook_envelope,
            lambda value: private_media_locators(value, "a" * 64),
        ):
            with self.subTest(operation=operation.__name__), self.assertRaises(
                ValueError
            ):
                operation(envelope)

    def test_delivery_semantic_dedupe_is_order_and_duplicate_independent(self):
        route = {
            "object": "page",
            "platform": "messenger",
            "asset_id": "100000000000001",
        }

        def atomic(message_ids, watermark=1787605000000):
            return {
                "object": "page",
                "entry": {"id": route["asset_id"]},
                "messaging": {
                    "sender": {"id": "900000000000050"},
                    "recipient": {"id": route["asset_id"]},
                    "timestamp": 1787605000000,
                    "delivery": {"mids": message_ids, "watermark": watermark},
                },
            }

        baseline = atomic_dedupe_key(atomic(["mid-a", "mid-b"]), route)
        equivalent = atomic_dedupe_key(atomic(["mid-b", "mid-a", "mid-a"]), route)
        different = atomic_dedupe_key(
            atomic(["mid-a", "mid-b"], watermark=1787605000001), route
        )

        self.assertEqual(baseline, equivalent)
        self.assertNotEqual(baseline, different)
