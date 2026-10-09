/** @odoo-module **/
/* global QUnit */
import {CONTACT_CENTER_NOTIFICATION_TYPE} from "@contact_center_ui/js/contact_center_model.esm";
import {conversationListRow} from "@contact_center_ui/js/contact_center_store.esm";
import {e2Deferred, e2Full, e2Metadata, e2Settle, e2Store} from "./e2_test_helpers.esm";

function delta(items, ids = [10], extra = {}) {
    return {
        schema_version: 1,
        refresh_required: false,
        window_ids: ids,
        items: items.map(conversationListRow),
        total: ids.length,
        has_more: false,
        next_cursor: false,
        ...extra,
    };
}
QUnit.module("contact_center_ui > E2 authorized metadata delta", () => {
    QUnit.test(
        "missing channel invalidation clears queued metadata and takes full selected path",
        async (assert) => {
            const {store, calls, timer} = e2Store();
            await store.selectConversation(10);
            calls.length = 0;
            store.synchronizeNotification(e2Metadata());
            store.onNotification({
                detail: [
                    {type: CONTACT_CENTER_NOTIFICATION_TYPE, payload: e2Metadata(0)},
                ],
            });
            assert.strictEqual(store.syncDeltaChannels.size, 0);
            await timer.advance(120);
            assert.deepEqual(
                calls.map((call) => call.method),
                ["list_conversations", "get_conversation", "get_timeline"]
            );
            store.destroy();
        }
    );
    QUnit.test(
        "100 metadata events coalesce, selected detail refreshes once, no timeline; outside-window total applies",
        async (assert) => {
            const {store, server, calls, timer} = e2Store();
            await store.selectConversation(10);
            calls.length = 0;
            server.respond = (method) =>
                method === "reconcile_conversations"
                    ? Promise.resolve(
                          delta([e2Full(10, {name: "Renamed"})], [10], {total: 3})
                      )
                    : undefined;
            for (let i = 0; i < 100; i++) {
                store.synchronizeNotification(e2Metadata());
            }
            await timer.advance(120);
            assert.deepEqual(
                calls.map((call) => call.method),
                ["reconcile_conversations", "get_conversation"]
            );
            assert.strictEqual(store.state.conversationTotal, 3);
            assert.ok(store.selectedConversation.identity.partner);
            assert.ok(store.selectedDetailReady);
            calls.length = 0;
            server.respond = (method) =>
                method === "reconcile_conversations"
                    ? Promise.resolve(delta([], [10], {total: 4}))
                    : undefined;
            store.synchronizeNotification(e2Metadata(999));
            await timer.advance(2000);
            assert.deepEqual(
                calls.map((call) => call.method),
                ["reconcile_conversations"]
            );
            assert.strictEqual(store.state.conversationTotal, 4);
            store.destroy();
        }
    );
    QUnit.test(
        "refresh-required and malformed delta fallback once to full list+selected without timeline",
        async (assert) => {
            const bad = [
                {schema_version: 1, refresh_required: true, items: []},
                delta([e2Full()], [20]),
                delta([e2Full()], [10], {has_more: "false"}),
                delta([e2Full()], [10], {
                    items: [{...conversationListRow(e2Full()), can_send: true}],
                }),
            ];
            for (const payload of bad) {
                const {store, calls, server, timer} = e2Store();
                await store.selectConversation(10);
                calls.length = 0;
                server.respond = (method) =>
                    method === "reconcile_conversations"
                        ? Promise.resolve(payload)
                        : undefined;
                store.synchronizeNotification(e2Metadata());
                await timer.advance(120);
                assert.deepEqual(
                    calls.map((call) => call.method),
                    [
                        "reconcile_conversations",
                        "list_conversations",
                        "get_conversation",
                    ]
                );
                assert.ok(store.selectedDetailReady);
                store.destroy();
            }
        }
    );
    QUnit.test(
        "search/unread/empty/pagination/tail/filters/preserved/bulk uncertainty skip delta",
        async (assert) => {
            const states = [
                (store) => {
                    store.state.filters.query = "search";
                },
                (store) => {
                    store.state.filters.unreadOnly = true;
                },
                (store) => {
                    store.state.conversations = [];
                },
                (store) => {
                    store.listLoadsInFlight = 1;
                },
                (store) => {
                    store.listTailStale = true;
                },
                (store) => {
                    store.listWindowFilterRevision = -1;
                },
                (store) => {
                    store.preservedConversationChannelId = 10;
                },
                (store) => {
                    store.bulkReadUncertain = true;
                },
                (store) => {
                    store.state.bulkReadPending = true;
                },
                (store) => {
                    store.state.conversations = Array.from({length: 101}, (_, index) =>
                        conversationListRow(e2Full(index + 1))
                    );
                },
            ];
            for (const change of states) {
                const {store, calls, timer} = e2Store();
                await store.selectConversation(10);
                calls.length = 0;
                change(store);
                store.synchronizeNotification(e2Metadata());
                await timer.advance(120);
                assert.notOk(
                    calls.some((call) => call.method === "reconcile_conversations")
                );
                assert.strictEqual(
                    calls.filter((call) => call.method === "list_conversations").length,
                    1
                );
                assert.notOk(
                    calls.some((call) => call.method === "get_timeline"),
                    "metadata fallback never reloads timeline"
                );
                store.destroy();
            }
        }
    );
    QUnit.test(
        "new event/filter/request/context makes pending delta stale before patch",
        async (assert) => {
            for (const change of [
                (store) => {
                    store.synchronizeNotification(e2Metadata());
                },
                (store) => {
                    store.bumpFilterRevision();
                },
                (store) => {
                    store.listRequest += 1;
                },
                (store, user) => {
                    user.context = {...user.context, allowed_company_ids: [2]};
                },
            ]) {
                const pending = e2Deferred();
                const {store, calls, server, user} = e2Store();
                server.respond = (method) =>
                    method === "reconcile_conversations" ? pending.promise : undefined;
                store.synchronizeNotification(e2Metadata());
                const running = store.runSynchronization();
                await e2Settle();
                change(store, user);
                pending.resolve(delta([e2Full(10, {name: "Stale"})]));
                await running;
                assert.notOk(
                    store.state.conversations.some((row) => row.name === "Stale")
                );
                assert.strictEqual(
                    calls.filter((call) => call.method === "reconcile_conversations")
                        .length,
                    1
                );
                store.destroy();
            }
        }
    );
    QUnit.test(
        "class dispatch: avatar lightweight; strict metadata delta; message/identity/reaction/media/mixed/unknown full",
        async (assert) => {
            for (const event of [
                "message_created",
                "message_updated",
                "message_deleted",
                "identity_updated",
                "reaction_updated",
                "media_updated",
                "unknown_new_event",
            ]) {
                const {store, calls, timer} = e2Store();
                await store.selectConversation(10);
                calls.length = 0;
                store.synchronizeNotification({
                    schema_version: 1,
                    event_type: event,
                    channel_id: 10,
                });
                await timer.advance(120);
                assert.deepEqual(
                    calls.map((call) => call.method),
                    ["list_conversations", "get_conversation", "get_timeline"],
                    event
                );
                store.destroy();
            }
            const {store, calls, timer, server} = e2Store();
            await store.selectConversation(10);
            calls.length = 0;
            server.respond = (method) =>
                method === "get_conversation_avatars"
                    ? Promise.resolve({
                          schema_version: 1,
                          items: [
                              {channel_id: 10, identity_id: 110, avatar_url: false},
                          ],
                      })
                    : undefined;
            store.synchronizeNotification(e2Metadata(10, "identity_avatar"));
            await timer.advance(120);
            assert.deepEqual(
                calls.map((call) => call.method),
                ["get_conversation_avatars"]
            );
            calls.length = 0;
            const mixed = e2Metadata();
            mixed.changed_fields.push("identity_aliases");
            store.synchronizeNotification(mixed);
            await timer.advance(2000);
            assert.deepEqual(
                calls.map((call) => call.method),
                ["list_conversations", "get_conversation", "get_timeline"]
            );
            calls.length = 0;
            store.synchronizeNotification({
                schema_version: 1,
                event_type: "member_seen",
                channel_id: 10,
                user_id: 999,
            });
            await timer.advance(2000);
            assert.strictEqual(calls.length, 0);
            store.destroy();
        }
    );
    QUnit.test(
        "full wins over queued delta and later full request prevents successful cycle",
        async (assert) => {
            const {store, calls, timer, server} = e2Store();
            await store.selectConversation(10);
            calls.length = 0;
            store.synchronizeNotification(e2Metadata());
            store.synchronizeNotification({
                schema_version: 1,
                event_type: "message_created",
                channel_id: 10,
            });
            assert.strictEqual(store.syncDeltaChannels.size, 0);
            await timer.advance(120);
            assert.deepEqual(
                calls.map((call) => call.method),
                ["list_conversations", "get_conversation", "get_timeline"]
            );
            const pending = e2Deferred();
            server.respond = (method) =>
                method === "list_conversations" ? pending.promise : undefined;
            store.scheduleSynchronization(false);
            const cycle = store.runSynchronization();
            await e2Settle();
            store.scheduleSynchronization(false, true);
            pending.resolve({
                schema_version: 1,
                projection: "list_v1",
                items: [conversationListRow(e2Full())],
                total: 1,
                has_more: false,
                next_cursor: false,
            });
            assert.notOk(
                await cycle,
                "cannot report completed cycle with requested full refresh outstanding"
            );
            store.destroy();
        }
    );
});
