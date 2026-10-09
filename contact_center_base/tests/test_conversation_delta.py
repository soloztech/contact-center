from unittest import mock

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import SavepointCase

from ..services.tokens import CONTACT_CENTER_MEMBERSHIP_TOKEN
from .conversation_list_common import ConversationListFixture


class TestConversationDelta(ConversationListFixture, SavepointCase):
    def _page(self, limit=2, filters=None):
        return self._api().list_conversations(
            limit=limit,
            filters=filters or self._filters(),
            projection="list_v1",
        )

    def _ids(self, page):
        return [item["channel_id"] for item in page["items"]]

    def _assert_fallback(self, delta):
        self.assertEqual(
            delta, {"schema_version": 1, "refresh_required": True, "items": []}
        )

    def test_stable_window_delta_has_native_row_and_envelope_parity(self):
        conversations = [self._conversation(index) for index in range(4)]
        api = self._api()
        page = self._page()
        affected = page["items"][0]["channel_id"]
        api.rename_guest(affected, "Delta Authorized Name")
        delta = api.reconcile_conversations(
            [affected, affected, conversations[0][0].id],
            self._ids(page),
            filters=self._filters(),
        )
        full = api.list_conversations(limit=2, filters=self._filters())
        native = self._page()
        self.assertFalse(delta["refresh_required"])
        self.assertEqual(delta["window_ids"], self._ids(native))
        self.assertEqual(delta["items"], [native["items"][0]])
        for key in ("total", "has_more", "next_cursor", "schema_version"):
            self.assertEqual(delta[key], full[key])

    def test_window_mismatch_has_no_dto_prefetch_or_invisible_id(self):
        conversations = [self._conversation(index) for index in range(3)]
        page = self._page()
        api = self._api()
        with mock.patch.object(
            type(api),
            "_conversation_list_prefetch",
            side_effect=AssertionError("DTO prefetch on fallback"),
        ):
            self._assert_fallback(
                api.reconcile_conversations(
                    [conversations[0][0].id],
                    list(reversed(self._ids(page))),
                    filters=self._filters(),
                )
            )
            self._assert_fallback(
                api.reconcile_conversations(
                    [999999999], [999999999], filters=self._filters()
                )
            )

    def test_matching_pinned_window_uses_native_pinned_cursor(self):
        conversations = [self._conversation(index) for index in range(3)]
        api = self._api()
        api.set_conversation_preference(conversations[0][0].id, {"pinned": True})
        page = self._page(limit=1)
        self.assertEqual(self._ids(page), [conversations[0][0].id])
        self.assertEqual(page["next_cursor"]["segment"], "pinned")
        api.rename_guest(conversations[0][0].id, "Pinned Updated Identity")
        delta = api.reconcile_conversations(
            self._ids(page), self._ids(page), filters=self._filters()
        )
        native = self._page(limit=1)
        self.assertFalse(delta["refresh_required"])
        self.assertEqual(delta["items"], native["items"])
        for key in ("total", "has_more", "next_cursor"):
            self.assertEqual(delta[key], native[key])

    def test_access_filter_state_pin_order_and_new_item_fall_back(self):
        conversations = [self._conversation(index) for index in range(3)]
        api = self._api()
        page = self._page()
        outside = conversations[0][0]
        api.set_conversation_preference(outside.id, {"pinned": True})
        self._assert_fallback(
            api.reconcile_conversations(
                [outside.id], self._ids(page), filters=self._filters()
            )
        )
        api.set_conversation_preference(outside.id, {"pinned": False})
        self._assert_fallback(
            api.reconcile_conversations(
                [outside.id],
                self._ids(page),
                filters=self._filters(states=["resolved"]),
            )
        )
        newest = conversations[-1][0]
        newest.sudo().with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write({"contact_center_state": "resolved"})
        self._assert_fallback(
            api.reconcile_conversations(
                [newest.id], self._ids(page), filters=self._filters(states=["open"])
            )
        )
        self._conversation(4)
        self._assert_fallback(
            api.reconcile_conversations(
                [outside.id], self._ids(page), filters=self._filters()
            )
        )
        page = self._page()
        denied = self.env["mail.channel"].browse(self._ids(page)[0])
        denied._contact_center_reconcile_members(
            partner_ids=[],
            guest_ids=denied.channel_member_ids.guest_id.ids,
            allow_empty=True,
        )
        self._assert_fallback(
            api.reconcile_conversations(
                [denied.id], self._ids(page), filters=self._filters()
            )
        )

    def test_active_company_reauthorizes_window(self):
        self._conversation(1)
        page = self._page()
        other = self.env["res.company"].create(
            {"name": "Synthetic Other Delta Company"}
        )
        self.agent.write({"company_ids": [fields.Command.link(other.id)]})
        api = self._api().with_context(allowed_company_ids=other.ids)
        self._assert_fallback(
            api.reconcile_conversations(
                self._ids(page), self._ids(page), filters=self._filters()
            )
        )

    def test_outside_window_rename_updates_total_without_disclosing_row(self):
        marker = "delta-total-%s" % self.account.id
        conversations = [
            self._conversation(index, name="%s %s" % (marker, index))
            for index in range(4)
        ]
        filters = self._filters(query=marker)
        page = self._page(filters=filters)
        outside = conversations[0][0]
        self._api().rename_guest(outside.id, "No longer matches")
        delta = self._api().reconcile_conversations(
            [outside.id], self._ids(page), filters=filters
        )
        native = self._page(filters=filters)
        self.assertFalse(delta["refresh_required"])
        self.assertEqual(delta["items"], [])
        self.assertEqual(delta["window_ids"], self._ids(page))
        self.assertEqual(page["total"], 4)
        self.assertEqual(delta["total"], 3)
        for key in ("total", "has_more", "next_cursor"):
            self.assertEqual(delta[key], native[key])

    def test_outside_window_has_more_change_uses_authoritative_envelope(self):
        conversations = [self._conversation(index) for index in range(3)]
        page = self._page(filters=self._filters(states=["open"]))
        conversations[0][0].sudo().with_context(
            contact_center_membership_token=CONTACT_CENTER_MEMBERSHIP_TOKEN
        ).write({"contact_center_state": "resolved"})
        delta = self._api().reconcile_conversations(
            [conversations[0][0].id],
            self._ids(page),
            filters=self._filters(states=["open"]),
        )
        self.assertTrue(page["has_more"])
        self.assertFalse(delta["refresh_required"])
        self.assertFalse(delta["has_more"])
        self.assertFalse(delta["next_cursor"])
        self.assertEqual(delta["total"], 2)
        self.assertEqual(delta["items"], [])

    def test_window_helper_never_serializes_or_computes_row_unread(self):
        for index in range(3):
            self._conversation(index)
        api = self._api()
        with (
            mock.patch.object(
                type(api),
                "_conversation_list_prefetch",
                side_effect=AssertionError("prefetch"),
            ),
            mock.patch.object(
                type(api),
                "_serialize_conversation",
                side_effect=AssertionError("detail"),
            ),
            mock.patch.object(
                type(api.env["mail.channel.member"]),
                "_compute_message_unread",
                side_effect=AssertionError("row unread"),
            ),
        ):
            window = api._conversation_list_window(2, 0, self._filters(), None)
        self.assertEqual(len(window["channels"]), 2)
        self.assertEqual(window["total"], 3)
        self.assertTrue(window["has_more"])

    def test_delta_unread_sql_and_prefetch_are_only_affected_intersection(self):
        conversations = [self._conversation(index) for index in range(4)]
        page = self._page()
        affected = self._ids(page)[0]
        api = self._api()
        api.env.invalidate_all()
        original_prefetch = type(api)._conversation_list_prefetch
        original_execute = type(api.env.cr).execute
        queries = []

        def prefetch(instance, channels, **kwargs):
            self.assertEqual(channels.ids, [affected])
            return original_prefetch(instance, channels, **kwargs)

        def execute(cursor, query, params=None, *args, **kwargs):
            if isinstance(query, str) and (
                "SELECT DISTINCT ON (scoped.channel_id)" in query
                or "member.id, COUNT(message.id)" in query
            ):
                queries.append((query, params))
            return original_execute(cursor, query, params, *args, **kwargs)

        with (
            mock.patch.object(type(api), "_conversation_list_prefetch", prefetch),
            mock.patch.object(type(api.env.cr), "execute", execute),
        ):
            result = api.reconcile_conversations(
                [affected, conversations[0][0].id],
                self._ids(page),
                filters=self._filters(),
            )
        self.assertFalse(result["refresh_required"])
        self.assertEqual(len(result["items"]), 1)
        first_unread_queries = [
            params for query, params in queries if "scoped.channel_id" in query
        ]
        self.assertEqual(len(first_unread_queries), 1)
        self.assertEqual(first_unread_queries[0][0], [affected])
        members = api.env["mail.channel.member"].search(
            [
                ("channel_id", "=", affected),
                ("partner_id", "=", self.agent.partner_id.id),
            ]
        )
        for query, params in queries:
            if "member.id, COUNT(message.id)" in query:
                self.assertLessEqual(set(params[0]), set(members.ids))

    def test_ids_filters_and_agent_access_are_validated(self):
        channel = self._conversation(1)[0]
        api = self._api()
        invalid_ids = (
            [],
            [False],
            [True],
            ["1"],
            [0],
            [-1],
            [1.0],
            list(range(1, 102)),
        )
        for values in invalid_ids:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                api.reconcile_conversations(values, [channel.id])
            with self.subTest(window=values), self.assertRaises(ValidationError):
                api.reconcile_conversations([channel.id], values)
        for filters in (
            [],
            "all",
            {"state": "unknown"},
            {"tag_ids": [False]},
            {"responsibility": "other"},
        ):
            with self.subTest(filters=filters), self.assertRaises(ValidationError):
                api.reconcile_conversations([channel.id], [channel.id], filters=filters)
        portal = self.env.ref("base.public_user")
        with self.assertRaises(AccessError):
            api.with_user(portal).reconcile_conversations([channel.id], [channel.id])
        result = api.reconcile_conversations(
            [channel.id],
            [channel.id, channel.id],
            filters=self._filters(
                domain=[("id", "=", 999999999)],
                offset=999,
                sql="ignore",
                clientproof=True,
            ),
        )
        self.assertFalse(result["refresh_required"])
        self.assertEqual(result["window_ids"], [channel.id])
