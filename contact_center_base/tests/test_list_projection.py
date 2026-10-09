import json
import logging
from unittest import mock

from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from .conversation_list_common import ConversationListFixture

_logger = logging.getLogger(__name__)

_ROW_KEYS = {
    "projection",
    "channel_id",
    "conversation_type",
    "name",
    "state",
    "ignored",
    "unread_count",
    "first_unread_message_id",
    "preference",
    "last_activity_at",
    "account",
    "platform",
    "provider",
    "responsible",
    "tags",
    "last_message",
    "identity",
    "group",
    "capabilities",
}


class TestConversationListProjection(ConversationListFixture, SavepointCase):
    def _assert_shared_parity(self, compact, full, path="row"):
        """Check every shared field recursively, including linked/group names."""

        if isinstance(compact, dict):
            self.assertIsInstance(full, dict, path)
            for key, value in compact.items():
                if key == "projection":
                    continue
                self.assertIn(key, full, "%s.%s" % (path, key))
                self._assert_shared_parity(value, full[key], "%s.%s" % (path, key))
        elif isinstance(compact, list):
            self.assertEqual(len(compact), len(full), path)
            for index, value in enumerate(compact):
                self._assert_shared_parity(value, full[index], "%s[%s]" % (path, index))
        else:
            self.assertEqual(compact, full, path)

    def test_compact_contract_and_recursive_full_parity(self):
        direct = self._conversation(1, linked=True)
        grouped = self._conversation(2, group=True)
        self._api().set_conversation_preference(direct[0].id, {"pinned": True})
        full = self._api().list_conversations(filters=self._filters())
        compact = self._api().list_conversations(
            filters=self._filters(), projection="list_v1"
        )
        self.assertNotIn("projection", full)
        self.assertEqual(compact["projection"], "list_v1")
        self.assertEqual(len(compact["items"]), 2)
        for key in ("schema_version", "has_more", "next_cursor", "total"):
            self.assertEqual(compact[key], full[key])
        for row, full_row in zip(compact["items"], full["items"], strict=True):
            self.assertEqual(set(row), _ROW_KEYS)
            self._assert_shared_parity(row, full_row)
            self.assertEqual(
                set(row["capabilities"]), {"delete_conversation", "ignore_conversation"}
            )
            self.assertTrue(row["capabilities"]["delete_conversation"])
            self.assertTrue(row["capabilities"]["ignore_conversation"])
            self.assertNotIn("can_send", row)
            self.assertNotIn("retention", row)
            self.assertNotIn("provider_connection", row)
            self.assertNotIn("access_teams", row)
            if row["identity"]:
                self.assertEqual(set(row["identity"]), {"id", "name", "avatar_url"})
                self.assertEqual(row["name"], direct[2].partner_id.name)
                self.assertNotEqual(row["name"], direct[2].name)
            if row["group"]:
                self.assertEqual(row["name"], "Group Profile Name 2")
                self.assertNotEqual(row["name"], grouped[0].name)
                self.assertEqual(len(row["group"]), 7)

    def test_compact_does_not_build_full_detail_and_batches_menu_policy(self):
        self._conversation(1, linked=True)
        self._conversation(2, group=True)
        self._conversation(3)
        api = self._api()
        account_type = type(api.env["contact.center.account"])
        original = account_type._contact_center_user_can_manage_conversation
        calls = []

        def management(account, operation):
            calls.append((account.id, operation))
            return original(account, operation)

        with (
            mock.patch.object(
                type(api),
                "_serialize_conversation",
                side_effect=AssertionError("full DTO"),
            ),
            mock.patch.object(
                type(api),
                "_batch_partner_company_projection",
                side_effect=AssertionError("company detail"),
            ),
            mock.patch.object(
                type(api),
                "_serialize_identity",
                side_effect=AssertionError("identity detail"),
            ),
            mock.patch.object(
                type(api),
                "_batch_group_own_protocol_participants",
                side_effect=AssertionError("group sender detail"),
            ),
            mock.patch.object(
                account_type, "_contact_center_user_can_manage_conversation", management
            ),
        ):
            page = api.list_conversations(filters=self._filters(), projection="list_v1")
        self.assertEqual(len(page["items"]), 3)
        self.assertEqual(
            calls, [(self.account.id, "delete"), (self.account.id, "ignore")]
        )

    def test_default_helpers_and_bulk_read_keep_full_contract(self):
        channel, _binding, _identity, message = self._conversation(1, linked=True)
        api = self._api()
        full = api.list_conversations(filters=self._filters())["items"][0]
        prefetched = api._conversation_list_prefetch(channel.with_user(self.agent))
        self.assertEqual(
            api._serialize_conversation_list_items(
                channel.with_user(self.agent), prefetched
            ),
            [full],
        )
        result = api.mark_conversations_read(
            [
                {
                    "channel_id": channel.id,
                    "message_id": message.id,
                    "max_message_id": message.id,
                }
            ]
        )
        self.assertEqual(len(result["items"]), 1)
        row = result["items"][0]
        self.assertNotIn("projection", row)
        self.assertIn("can_send", row)
        self.assertIn("retention", row)
        self.assertIn("partner", row["identity"])
        self.assertIn("aliases", row["identity"])
        self.assertIn("send_message", row["capabilities"])

    def test_projection_is_exact_opt_in(self):
        self._conversation(1)
        for value in (False, "full", "list_v2", [], {}):
            with self.subTest(projection=value), self.assertRaises(ValidationError):
                self._api().list_conversations(projection=value)
        self.assertNotIn("projection", self._api().list_conversations(projection=None))

    def test_compact_and_full_cursor_pages_have_identical_order_and_meta(self):
        conversations = [self._conversation(index) for index in range(4)]
        api = self._api()
        api.set_conversation_preference(conversations[0][0].id, {"pinned": True})
        cursor = None
        ordered_ids = []
        for _iteration in range(4):
            full = api.list_conversations(
                limit=1, filters=self._filters(), cursor=cursor
            )
            compact = api.list_conversations(
                limit=1, filters=self._filters(), cursor=cursor, projection="list_v1"
            )
            self.assertEqual(len(compact["items"]), 1)
            self._assert_shared_parity(compact["items"][0], full["items"][0])
            for key in ("has_more", "next_cursor", "total"):
                self.assertEqual(compact[key], full[key])
            ordered_ids.append(compact["items"][0]["channel_id"])
            if cursor:
                self.assertIs(compact["total"], False)
            cursor = compact["next_cursor"]
        self.assertEqual(
            ordered_ids, [conversations[index][0].id for index in (0, 3, 2, 1)]
        )
        self.assertFalse(cursor)

    def test_synthetic_batch_50_benchmark_and_parity(self):
        for index in range(50):
            self._conversation(index, group=index % 5 == 0, linked=index % 5 == 1)
        api = self._api()
        counts = {}
        pages = {}
        for label, projection in (("full", None), ("compact", "list_v1")):
            api.env.invalidate_all()
            before = api.env.cr.sql_log_count
            pages[label] = api.list_conversations(
                limit=50, filters=self._filters(), projection=projection
            )
            counts[label] = api.env.cr.sql_log_count - before
        for compact, full in zip(
            pages["compact"]["items"], pages["full"]["items"], strict=True
        ):
            self._assert_shared_parity(compact, full)
        sizes = {
            label: len(json.dumps(page, sort_keys=True).encode())
            for label, page in pages.items()
        }
        self.assertEqual(len(pages["compact"]["items"]), 50)
        self.assertLess(sizes["compact"], sizes["full"])
        _logger.info(
            "E2_SYNTHETIC_LIST_BENCHMARK rows=50 full_bytes=%s compact_bytes=%s "
            "full_sql=%s compact_sql=%s",
            sizes["full"],
            sizes["compact"],
            counts["full"],
            counts["compact"],
        )
