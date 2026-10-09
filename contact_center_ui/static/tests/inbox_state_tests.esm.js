/** @odoo-module **/

/* global QUnit */

import {
    ContactCenterStore,
    consumeInboxActionParams,
    createInboxDocumentContext,
    inboxStateStorageKey,
    sanitizeInboxPreferences,
} from "@contact_center_ui/js/contact_center_store.esm";
import {
    SUPPORTED_SCHEMA_VERSION,
    conversationPreference,
} from "@contact_center_ui/js/contact_center_model.esm";
import {ContactCenterApp} from "@contact_center_ui/js/contact_center_app.esm";

const DATABASE = "inbox-state-test";
const USER_ID = 7;

function fakeStorage(initial = {}) {
    const values = new Map(Object.entries(initial));
    return {
        values,
        getItem: (key) => (values.has(key) ? values.get(key) : null),
        setItem: (key, value) => values.set(key, String(value)),
        removeItem: (key) => values.delete(key),
    };
}

function fakeTimer() {
    const pending = new Map();
    let next = 1;
    let time = 0;
    const delays = new Map();
    return {
        pending,
        now: () => time,
        setTimeout(callback, delay = 0) {
            const id = next++;
            pending.set(id, callback);
            delays.set(id, delay);
            return id;
        },
        clearTimeout(id) {
            pending.delete(id);
            delays.delete(id);
        },
        flush() {
            const callbacks = [...pending.values()];
            time += Math.max(0, ...delays.values());
            pending.clear();
            delays.clear();
            callbacks.forEach((callback) => callback());
        },
    };
}

function bootstrapPayload(userId = USER_ID) {
    return {
        schema_version: SUPPORTED_SCHEMA_VERSION,
        user: {id: userId},
        accounts: [
            {id: 1, name: "Comercial"},
            {id: 2, name: "Atendimento"},
        ],
        agents: [
            {id: 7, name: "Lucas"},
            {id: 8, name: "Ana"},
        ],
        tags: [{id: 5, name: "Retorno"}],
        capabilities: {followups: true},
    };
}

function conversation(channelId, values = {}) {
    return {
        channel_id: channelId,
        conversation_type: "direct",
        name: `Conversa ${channelId}`,
        state: "open",
        responsible: false,
        tags: [],
        unread_count: 0,
        preference: {pinned: false, pinned_at: false, muted: false, revision: 0},
        account: {id: 1, name: "Comercial"},
        ...values,
    };
}

function saved(filters = {}, layout = {}, version = 1) {
    return JSON.stringify({version, filters, layout});
}

/**
 * A store with an in-memory server: list, conversation and seen calls are
 * recorded; the timeline itself is out of scope for these tests.
 *
 * @param {Object} options storage, document context, timer and server rows
 * @returns {Object} the store with its recorded calls and fakes
 */
function inboxStore({
    storage = fakeStorage(),
    context = createInboxDocumentContext(),
    timer = fakeTimer(),
    params = false,
    userId = USER_ID,
    items = [conversation(10), conversation(20)],
    getConversation = false,
    realtimeTimer = undefined,
} = {}) {
    const notifications = [];
    const store = new ContactCenterStore({
        orm: {},
        busService: new EventTarget(),
        notification: {add: (message) => notifications.push(message)},
        inboxPreferenceStorage: storage,
        inboxContext: context,
        inboxPreferenceTimer: timer,
        realtimeTimer,
        refreshNow: realtimeTimer && realtimeTimer.now,
        operationDatabase: DATABASE,
        initialActionParams: params,
    });
    const calls = [];
    const server = {items};
    store.call = async (method, args = [], kwargs = {}) => {
        calls.push({method, args, kwargs});
        if (method === "get_connection_health") {
            return {
                schema_version: SUPPORTED_SCHEMA_VERSION,
                items: [],
                summary: {total: 0},
            };
        }
        if (method === "bootstrap") {
            return bootstrapPayload(userId);
        }
        if (method === "list_conversations") {
            const accountId = kwargs.filters && kwargs.filters.account_id;
            const excludeMuted = kwargs.filters && kwargs.filters.exclude_muted;
            const rows = server.items.filter(
                (item) =>
                    (!accountId || item.account.id === accountId) &&
                    !(excludeMuted && item.preference && item.preference.muted)
            );
            const offset = (kwargs.cursor && kwargs.cursor.offset) || 0;
            const limit = kwargs.limit || rows.length;
            const page = rows.slice(offset, offset + limit);
            const hasMore = offset + limit < rows.length;
            return {
                schema_version: SUPPORTED_SCHEMA_VERSION,
                items: page,
                has_more: hasMore,
                next_cursor: hasMore
                    ? {
                          segment: "activity",
                          channel_id: page[page.length - 1].channel_id,
                          last_activity_at:
                              page[page.length - 1].last_activity_at || "",
                          offset: offset + limit,
                      }
                    : false,
                total: rows.length,
            };
        }
        if (method === "get_conversation") {
            if (getConversation) {
                return getConversation(args[0]);
            }
            return {
                schema_version: SUPPORTED_SCHEMA_VERSION,
                item: server.items.find((item) => item.channel_id === args[0]),
            };
        }
        if (method === "mark_seen") {
            return {channel_id: args[0], message_id: args[1]};
        }
        throw new Error(`Unexpected call ${method}`);
    };
    const timelines = [];
    store.loadTimeline = async (options) => {
        timelines.push({channelId: store.state.selectedChannelId, options});
        return true;
    };
    return {store, calls, server, storage, context, timer, notifications, timelines};
}

async function settleUntil(condition, attempts = 20) {
    for (let attempt = 0; attempt < attempts; attempt += 1) {
        if (condition()) {
            return true;
        }
        await Promise.resolve();
    }
    return Boolean(condition());
}

function methods(calls, method) {
    return calls.filter((call) => call.method === method);
}

QUnit.module("contact_center_ui > inbox state", () => {
    QUnit.test(
        "the saved state is namespaced by database and user and versioned",
        (assert) => {
            assert.strictEqual(
                inboxStateStorageKey("db one", 7),
                "contact_center_ui.inbox_state.v1.db%20one.7"
            );
            assert.notOk(inboxStateStorageKey("", 7));
            assert.notOk(inboxStateStorageKey("db", 0));
            assert.notOk(inboxStateStorageKey("db", "7x"));
        }
    );

    QUnit.test(
        "leaving and returning restores filters, layout, search and the open conversation",
        async (assert) => {
            const storage = fakeStorage();
            const context = createInboxDocumentContext();
            const items = [
                // Listed under the Unread filter: it has unread messages.
                conversation(10, {tags: [{id: 5, name: "Retorno"}], unread_count: 2}),
                conversation(20),
            ];
            const first = inboxStore({storage, context, items});
            await first.store.loadBootstrap();
            assert.strictEqual(first.store.state.selectedChannelId, false);
            await first.store.setFilter("accountId", 1);
            await first.store.setFilter("unreadOnly", true);
            await first.store.setFilter("tagIds", [5]);
            await first.store.setConversationStateFilters(["open", "resolved"]);
            first.store.state.filters.query = "Ana";
            first.store.rememberInboxLayout({
                listView: "flat",
                collapsedInboxes: {"inbox:1": true},
                sidePanel: "crm",
            });
            first.store.toggleDetails();
            const detailsOpen = first.store.state.detailsOpen;
            await first.store.selectConversation(10);
            first.store.rememberListScroll(180);
            first.store.destroy();

            const second = inboxStore({storage, context, items});
            await second.store.loadBootstrap();
            const filters = second.store.state.filters;
            assert.strictEqual(filters.accountId, 1);
            assert.strictEqual(filters.unreadOnly, true);
            assert.deepEqual(filters.tagIds, [5]);
            assert.deepEqual(filters.states, ["open", "resolved"]);
            assert.strictEqual(filters.query, "Ana", "search returns in this document");
            assert.deepEqual(second.store.inboxLayout, {
                listView: "flat",
                collapsedInboxes: {"inbox:1": true},
                sidePanel: "crm",
                detailsOpen,
            });
            assert.strictEqual(
                second.store.state.detailsOpen,
                detailsOpen && window.innerWidth >= 1200
            );
            assert.deepEqual(
                methods(second.calls, "list_conversations")[0].kwargs.filters,
                {
                    states: ["open", "resolved"],
                    account_id: 1,
                    query: "Ana",
                    unread_only: true,
                    tag_ids: [5],
                }
            );
            assert.deepEqual(
                methods(second.calls, "get_conversation").map((call) => call.args),
                [[10]],
                "the server authorizes the remembered conversation again"
            );
            assert.strictEqual(second.store.state.selectedChannelId, 10);
            assert.strictEqual(second.store.state.seenPausedChannelId, 10);
            assert.strictEqual(second.store.consumePendingListScroll(), 180);
            assert.strictEqual(second.store.consumePendingListScroll(), 0);
            second.store.destroy();
        }
    );

    QUnit.test(
        "a new document restores preferences but neither search nor selection",
        async (assert) => {
            const storage = fakeStorage();
            const first = inboxStore({storage});
            await first.store.loadBootstrap();
            await first.store.setFilter("accountId", 2);
            first.store.state.filters.query = "Ana";
            await first.store.selectConversation(20);
            first.store.destroy();

            // A reload, a new tab or a duplicated tab starts a new web client:
            // the document service is recreated even if sessionStorage is copied.
            const second = inboxStore({storage});
            await second.store.loadBootstrap();
            assert.strictEqual(second.store.state.filters.accountId, 2);
            assert.strictEqual(second.store.state.filters.query, "");
            assert.strictEqual(second.store.state.selectedChannelId, false);
            assert.deepEqual(methods(second.calls, "get_conversation"), []);
            second.store.destroy();
        }
    );

    QUnit.test(
        "another user's document memory is ignored and cleared",
        async (assert) => {
            const context = Object.assign(createInboxDocumentContext(), {
                userId: 8,
                channelId: 10,
                query: "Ana",
                listScrollTop: 50,
            });
            const {store, calls} = inboxStore({context});
            await store.loadBootstrap();
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.strictEqual(store.state.filters.query, "");
            assert.deepEqual(methods(calls, "get_conversation"), []);
            assert.deepEqual(
                {...context, owner: false},
                {...createInboxDocumentContext()},
                "nothing of another user survives"
            );
            assert.strictEqual(context.owner, store.documentOwner);
            store.destroy();
        }
    );

    QUnit.test(
        "invalid, stale or foreign saved preferences are ignored without errors",
        async (assert) => {
            const key = inboxStateStorageKey(DATABASE, USER_ID);
            const defaults = inboxStore().store.state.filters;
            for (const [label, raw] of [
                ["corrupt JSON", "{not json"],
                ["other version", saved({accountId: 1}, {listView: "flat"}, 2)],
                ["array", "[]"],
                ["null", "null"],
            ]) {
                const {store} = inboxStore({storage: fakeStorage({[key]: raw})});
                await store.loadBootstrap();
                assert.deepEqual(store.state.filters, defaults, label);
                assert.strictEqual(store.inboxLayout.listView, "grouped", label);
                store.destroy();
            }
            const {store} = inboxStore({
                storage: fakeStorage({
                    [key]: saved(
                        {
                            accountId: 99,
                            responsibleId: 99,
                            responsibility: "everyone",
                            tagIds: [5, 404, "x"],
                            states: ["open", "deleted"],
                            conversationType: "broadcast",
                            unreadOnly: "yes",
                            activityTiming: "someday",
                        },
                        {
                            listView: "cards",
                            sidePanel: "billing",
                            detailsOpen: "yes",
                            collapsedInboxes: {
                                "inbox:1": true,
                                "inbox:404": true,
                                "inbox:unknown": true,
                                "inbox:2": "yes",
                            },
                        }
                    ),
                }),
            });
            await store.loadBootstrap();
            assert.deepEqual(store.state.filters, {
                ...defaults,
                tagIds: [5],
            });
            assert.deepEqual(store.inboxLayout, {
                listView: "grouped",
                collapsedInboxes: {"inbox:1": true, "inbox:unknown": true},
                sidePanel: "contact",
                detailsOpen: undefined,
            });
            store.destroy();
            const other = inboxStore({
                storage: fakeStorage({
                    [inboxStateStorageKey(DATABASE, 8)]: saved({accountId: 1}),
                    [inboxStateStorageKey("other-db", USER_ID)]: saved({accountId: 2}),
                }),
            });
            await other.store.loadBootstrap();
            assert.strictEqual(
                other.store.state.filters.accountId,
                false,
                "another user's or database's preferences are never read"
            );
            other.store.destroy();
            assert.deepEqual(sanitizeInboxPreferences(undefined, {}), {
                filters: {},
                layout: {},
            });
        }
    );

    QUnit.test(
        "storage failures fall back to defaults and never break the inbox",
        async (assert) => {
            const broken = {
                getItem() {
                    throw new Error("denied");
                },
                setItem() {
                    throw new Error("quota");
                },
            };
            const {store} = inboxStore({storage: broken});
            await store.loadBootstrap();
            assert.strictEqual(store.state.phase, "ready");
            await store.setFilter("accountId", 1);
            assert.notOk(store.saveInboxPreferences());
            store.destroy();
            assert.ok(true, "destroying with a failing storage does not throw");
        }
    );

    QUnit.test(
        "a remembered conversation without access opens nothing and is forgotten silently",
        async (assert) => {
            for (const name of [
                "odoo.exceptions.AccessError",
                "odoo.exceptions.MissingError",
                "odoo.exceptions.ValidationError",
            ]) {
                const context = Object.assign(createInboxDocumentContext(), {
                    userId: USER_ID,
                    channelId: 10,
                    query: "",
                });
                const fixture = inboxStore({
                    context,
                    getConversation: () => {
                        const error = new Error("denied");
                        error.data = {name};
                        throw error;
                    },
                });
                await fixture.store.loadBootstrap();
                assert.strictEqual(fixture.store.state.selectedChannelId, false, name);
                assert.strictEqual(context.channelId, false, name);
                assert.deepEqual(fixture.notifications, [], `${name}: no notification`);
                assert.deepEqual(fixture.timelines, [], name);
                fixture.store.destroy();
            }
        }
    );

    QUnit.test(
        "a remembered conversation outside the restored filters is not opened",
        async (assert) => {
            const context = Object.assign(createInboxDocumentContext(), {
                userId: USER_ID,
                channelId: 10,
            });
            const fixture = inboxStore({
                context,
                getConversation: () => ({
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    item: conversation(10, {state: "resolved"}),
                }),
            });
            await fixture.store.loadBootstrap();
            assert.deepEqual(fixture.store.state.filters.states, ["open"]);
            assert.strictEqual(fixture.store.state.selectedChannelId, false);
            assert.strictEqual(context.channelId, false);
            assert.deepEqual(fixture.notifications, []);
            fixture.store.destroy();
        }
    );

    QUnit.test(
        "without a remembered conversation nothing is selected, and filters never pick the first",
        async (assert) => {
            const fixture = inboxStore();
            const {store, server} = fixture;
            await store.loadBootstrap();
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.deepEqual(fixture.timelines, []);
            await store.selectConversation(20);
            server.items = [conversation(10), conversation(20), conversation(30)];
            await store.setFilter("unreadOnly", true);
            assert.strictEqual(
                store.state.selectedChannelId,
                20,
                "a visible selection stays"
            );
            server.items = [conversation(30)];
            await store.setFilter("accountId", 2);
            assert.strictEqual(
                store.state.selectedChannelId,
                false,
                "a selection that left the list is cleared, not replaced"
            );
            await store.clearConversationFilters();
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.deepEqual(
                fixture.timelines.map((item) => item.channelId),
                [20],
                "only the agent's own choice loaded a timeline"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "realtime and periodic refreshes clear a selection that left the list without opening another",
        async (assert) => {
            const fixture = inboxStore({
                items: [
                    conversation(10, {responsible: {id: USER_ID, name: "Lucas"}}),
                    conversation(20, {responsible: {id: USER_ID, name: "Lucas"}}),
                ],
            });
            const {store, server, calls} = fixture;
            await store.loadBootstrap();
            await store.setFilter("responsibility", "mine");
            await store.selectConversation(10);
            // Reassigned to someone else: the periodic refresh no longer lists it.
            server.items = [
                conversation(20, {responsible: {id: USER_ID, name: "Lucas"}}),
            ];
            await store.refreshLoadedConversations({silent: true});
            assert.strictEqual(store.state.selectedChannelId, false, "reassignment");

            await store.setFilter("responsibility", "all");
            await store.setFilter("tagIds", [5]);
            server.items = [conversation(20, {tags: [{id: 5, name: "Retorno"}]})];
            await store.refreshLoadedConversations({silent: true});
            await store.selectConversation(20);
            // The tag is removed on the server and the bus refresh arrives.
            server.items = [];
            await store.refreshLoadedConversations({silent: true});
            assert.strictEqual(store.state.selectedChannelId, false, "tag removal");

            await store.setFilter("tagIds", []);
            server.items = [conversation(10), conversation(20)];
            await store.refreshLoadedConversations({silent: true});
            await store.selectConversation(10);
            const resolved = conversation(10, {state: "resolved"});
            store.call = async (method, args, kwargs) => {
                calls.push({method, args, kwargs});
                if (method === "get_conversation") {
                    return {schema_version: SUPPORTED_SCHEMA_VERSION, item: resolved};
                }
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items: [conversation(20)],
                    has_more: false,
                    next_cursor: false,
                    total: 1,
                };
            };
            await store.refreshSelectedConversation({silent: true});
            assert.strictEqual(store.state.selectedChannelId, false, "state change");
            assert.deepEqual(
                fixture.timelines.map((item) => item.channelId),
                [10, 20, 10],
                "no conversation was opened by a refresh"
            );
            assert.deepEqual(methods(calls, "mark_seen"), []);
            store.destroy();
        }
    );

    QUnit.test(
        "directed navigation uses temporary neutral filters and keeps the saved ones",
        async (assert) => {
            const key = inboxStateStorageKey(DATABASE, USER_ID);
            const savedFilters = {
                responsibility: "mine",
                tagIds: [5],
                accountId: 1,
                states: ["open"],
            };
            const storage = fakeStorage({[key]: saved(savedFilters)});
            const context = Object.assign(createInboxDocumentContext(), {
                userId: USER_ID,
                channelId: 20,
                query: "Ana",
            });
            const directed = inboxStore({
                storage,
                context,
                params: {channel_id: 42},
                items: [conversation(42, {account: {id: 2, name: "Atendimento"}})],
            });
            await directed.store.loadBootstrap();
            assert.deepEqual(
                methods(directed.calls, "list_conversations")[0].kwargs.filters,
                {},
                "saved filters cannot hide the directed conversation"
            );
            assert.deepEqual(
                methods(directed.calls, "get_conversation").map((call) => call.args),
                [[42]],
                "action parameters win over the remembered conversation"
            );
            assert.strictEqual(directed.store.state.selectedChannelId, 42);
            assert.strictEqual(
                directed.store.state.seenPausedChannelId,
                false,
                "a directed opening is the agent's own choice"
            );
            directed.store.rememberInboxLayout({listView: "flat"});
            directed.store.destroy();
            const stored = JSON.parse(storage.getItem(key));
            assert.deepEqual(stored.filters, savedFilters);
            assert.strictEqual(stored.layout.listView, "flat");

            const timing = inboxStore({
                storage,
                params: {activity_timing: "due"},
            });
            await timing.store.loadBootstrap();
            assert.deepEqual(
                methods(timing.calls, "list_conversations")[0].kwargs.filters,
                {activity_timing: "due"}
            );
            assert.strictEqual(timing.store.state.selectedChannelId, false);
            await timing.store.setFilter("accountId", 2);
            timing.store.destroy();
            assert.deepEqual(
                JSON.parse(storage.getItem(key)).filters,
                {
                    states: [],
                    accountId: 2,
                    responsibility: "all",
                    responsibleId: false,
                    unreadOnly: false,
                    excludeMuted: false,
                    conversationType: false,
                    tagIds: [],
                    activityTiming: "due",
                },
                "a filter change by the agent saves the filters on screen"
            );
        }
    );

    QUnit.test(
        "changes are saved after a short delay and flushed on exit",
        async (assert) => {
            const key = inboxStateStorageKey(DATABASE, USER_ID);
            const {store, storage, timer} = inboxStore();
            store.rememberInboxLayout({listView: "flat"});
            assert.strictEqual(
                timer.pending.size,
                0,
                "nothing is written before loading"
            );
            await store.loadBootstrap();
            await store.setFilter("unreadOnly", true);
            assert.strictEqual(timer.pending.size, 1);
            assert.strictEqual(storage.getItem(key), null);
            timer.flush();
            assert.strictEqual(
                JSON.parse(storage.getItem(key)).filters.unreadOnly,
                true
            );
            store.toggleDetails();
            const expected = store.state.detailsOpen;
            store.onPageHide();
            assert.strictEqual(
                JSON.parse(storage.getItem(key)).layout.detailsOpen,
                expected
            );
            store.state.filters.query = "private text";
            store.destroy();
            assert.notOk(
                storage.getItem(key).includes("private text"),
                "search text never reaches localStorage"
            );
        }
    );

    QUnit.test(
        "a restored conversation is not marked seen until the agent interacts",
        async (assert) => {
            const context = Object.assign(createInboxDocumentContext(), {
                userId: USER_ID,
                channelId: 10,
            });
            const fixture = inboxStore({context});
            const {store, calls} = fixture;
            await store.loadBootstrap();
            assert.strictEqual(store.state.seenPausedChannelId, 10);
            assert.notOk(await store.markSeen(100));
            assert.deepEqual(methods(calls, "mark_seen"), []);
            const app = Object.create(ContactCenterApp.prototype);
            app.store = store;
            app.onConversationInteraction();
            assert.strictEqual(store.state.seenPausedChannelId, false);
            assert.ok(await store.markSeen(100));
            assert.deepEqual(
                methods(calls, "mark_seen").map((call) => call.args),
                [[10, 100]]
            );
            store.state.seenPausedChannelId = 10;
            await store.selectConversation(20);
            assert.strictEqual(
                store.state.seenPausedChannelId,
                false,
                "the agent's own selection reads as before"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "returning after a directed opening in another inbox keeps it closed under the saved filters",
        async (assert) => {
            const key = inboxStateStorageKey(DATABASE, USER_ID);
            const storage = fakeStorage({[key]: saved({accountId: 1})});
            const context = createInboxDocumentContext();
            const items = [
                conversation(10),
                conversation(42, {account: {id: 2, name: "Atendimento"}}),
            ];
            const directed = inboxStore({
                storage,
                context,
                items,
                params: {channel_id: 42},
            });
            await directed.store.loadBootstrap();
            assert.strictEqual(directed.store.state.selectedChannelId, 42);
            directed.store.destroy();
            assert.strictEqual(context.channelId, 42);

            const back = inboxStore({storage, context, items});
            await back.store.loadBootstrap();
            assert.strictEqual(back.store.state.filters.accountId, 1);
            assert.strictEqual(
                back.store.state.selectedChannelId,
                false,
                "a conversation outside the saved inbox filter is not reopened"
            );
            assert.deepEqual(methods(back.calls, "get_conversation"), []);
            assert.strictEqual(context.channelId, false);
            assert.deepEqual(back.notifications, []);
            back.store.destroy();
        }
    );

    QUnit.test(
        "the remembered window is reloaded page by page before position and selection",
        async (assert) => {
            const items = Array.from({length: 120}, (_value, index) =>
                conversation(index + 1)
            );
            const context = Object.assign(createInboxDocumentContext(), {
                userId: USER_ID,
                channelId: 110,
                listScrollTop: 4200,
                loadedCount: 120,
            });
            const fixture = inboxStore({context, items});
            const {store, calls} = fixture;
            await store.loadBootstrap();
            assert.strictEqual(methods(calls, "list_conversations").length, 3);
            assert.strictEqual(store.state.conversations.length, 120);
            assert.strictEqual(
                store.state.selectedChannelId,
                110,
                "a conversation beyond the first page but inside the window returns"
            );
            assert.strictEqual(store.state.listScrollRestoreRequest, 1);
            assert.strictEqual(store.consumePendingListScroll(), 4200);
            store.destroy();
            assert.strictEqual(context.loadedCount, 120);

            const capped = Object.assign(createInboxDocumentContext(), {
                userId: USER_ID,
                loadedCount: 5000,
            });
            const many = inboxStore({
                context: capped,
                items: Array.from({length: 260}, (_value, index) =>
                    conversation(index + 1)
                ),
            });
            await many.store.loadBootstrap();
            assert.strictEqual(
                many.store.state.conversations.length,
                200,
                "the window never grows beyond the realtime refresh bound"
            );
            many.store.destroy();
        }
    );

    QUnit.test(
        "a silent refresh keeps the open conversation only for paging or volatile filters",
        async (assert) => {
            async function scenario(filters, refreshed, current) {
                const fixture = inboxStore({
                    items: [conversation(10), conversation(20)],
                });
                const {store, server} = fixture;
                await store.loadBootstrap();
                await store.selectConversation(10);
                Object.assign(store.state.filters, filters);
                server.items = refreshed;
                await store.refreshLoadedConversations({silent: true});
                const afterList = store.state.selectedChannelId;
                if (afterList && current) {
                    server.items = [current];
                    await store.refreshSelectedConversation({silent: true});
                }
                const result = {afterList, final: store.state.selectedChannelId};
                store.destroy();
                return result;
            }
            const paging = Array.from({length: 60}, (_value, index) =>
                conversation(100 + index)
            );
            assert.deepEqual(
                await scenario({}, paging, conversation(10)),
                {afterList: 10, final: 10},
                "absent from a window with more pages: kept"
            );
            assert.deepEqual(
                await scenario({accountId: 1}, [conversation(20)], false),
                {afterList: false, final: false},
                "excluded with only structural filters: cleared"
            );
            assert.deepEqual(
                await scenario(
                    {unreadOnly: true},
                    [conversation(20, {unread_count: 1})],
                    conversation(10, {unread_count: 0})
                ),
                {afterList: 10, final: 10},
                "read in another tab under unread only: kept"
            );
            assert.deepEqual(
                await scenario(
                    {activityTiming: "due"},
                    [conversation(20)],
                    conversation(10)
                ),
                {afterList: 10, final: 10},
                "its due activity completed: kept"
            );
            assert.deepEqual(
                await scenario(
                    {unreadOnly: true},
                    [conversation(20, {unread_count: 1})],
                    conversation(10, {state: "resolved"})
                ),
                {afterList: 10, final: false},
                "a volatile filter never hides a structural exit"
            );
        }
    );

    QUnit.test(
        "every silent refresh revalidates a preserved selection on the server",
        async (assert) => {
            const mine = {responsible: {id: USER_ID, name: "Lucas"}, unread_count: 1};
            const fixture = inboxStore({
                items: [conversation(10, mine), conversation(20, mine)],
            });
            const {store, server, calls} = fixture;
            await store.loadBootstrap();
            await store.setFilter("responsibility", "mine");
            await store.setFilter("unreadOnly", true);
            await store.selectConversation(10);
            // Reassigned elsewhere while the bus is down: the next list refresh
            // comes from pinning another conversation, not from the bus.
            server.items = [
                conversation(10, {responsible: {id: 8, name: "Ana"}}),
                conversation(20, mine),
            ];
            store.call = async (method, args = [], kwargs = {}) => {
                calls.push({method, args, kwargs});
                if (method === "set_conversation_preference") {
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        item: conversation(20, {...mine, preference: {pinned: true}}),
                    };
                }
                if (method === "get_conversation") {
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        item: server.items.find((item) => item.channel_id === args[0]),
                    };
                }
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items: [conversation(20, mine)],
                    has_more: false,
                    next_cursor: false,
                    total: 1,
                };
            };
            assert.ok(await store.setConversationPreference({pinned: true}, 20));
            assert.strictEqual(
                store.state.selectedChannelId,
                false,
                "the current item is re-read and its reassignment clears the selection"
            );
            assert.ok(
                methods(calls, "get_conversation").some((call) => call.args[0] === 10)
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a search typed while restoring cancels the restoration before the debounce",
        async (assert) => {
            const context = Object.assign(createInboxDocumentContext(), {
                userId: USER_ID,
                channelId: 10,
                listScrollTop: 90,
            });
            let release = null;
            const fixture = inboxStore({
                context,
                getConversation: (channelId) =>
                    new Promise((resolve) => {
                        release = () =>
                            resolve({
                                schema_version: SUPPORTED_SCHEMA_VERSION,
                                item: conversation(channelId),
                            });
                    }),
            });
            const {store} = fixture;
            const booting = store.loadBootstrap();
            await settleUntil(() => release);
            assert.ok(release, "the restoration waits for the server");
            store.setFilter("query", "someone else");
            release();
            await booting;
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.strictEqual(
                store.consumePendingListScroll(),
                0,
                "a filter edit discards a position not yet applied"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "an inbox change while restoring never applies the old position",
        async (assert) => {
            const context = Object.assign(createInboxDocumentContext(), {
                userId: USER_ID,
                channelId: 10,
                listScrollTop: 90,
            });
            let release = null;
            const fixture = inboxStore({
                context,
                getConversation: (channelId) =>
                    new Promise((resolve) => {
                        release = () =>
                            resolve({
                                schema_version: SUPPORTED_SCHEMA_VERSION,
                                item: conversation(channelId),
                            });
                    }),
            });
            const {store} = fixture;
            const booting = store.loadBootstrap();
            await settleUntil(() => release);
            // The replacement list completes before the old response returns.
            const requests = store.state.listScrollRestoreRequest;
            await store.setFilter("accountId", 2);
            release();
            await booting;
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.strictEqual(
                store.state.listScrollRestoreRequest,
                requests,
                "no position is applied after the inbox change"
            );
            assert.strictEqual(store.consumePendingListScroll(), 0);
            assert.strictEqual(
                context.channelId,
                10,
                "a cancelled restoration decides nothing about the memory"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a search typed while the first page loads cancels the restoration",
        async (assert) => {
            const context = Object.assign(createInboxDocumentContext(), {
                userId: USER_ID,
                channelId: 10,
                listScrollTop: 90,
            });
            const {store, calls} = inboxStore({context});
            const original = store.call;
            let releaseList = null;
            let delayed = false;
            store.call = (method, args, kwargs) => {
                if (method === "list_conversations" && !delayed) {
                    delayed = true;
                    return new Promise((resolve) => {
                        releaseList = () => resolve(original(method, args, kwargs));
                    });
                }
                return original(method, args, kwargs);
            };
            const booting = store.loadBootstrap();
            assert.ok(await settleUntil(() => releaseList));
            // Still inside the search debounce: no new list request yet.
            store.setFilter("query", "someone else");
            releaseList();
            await booting;
            assert.deepEqual(methods(calls, "get_conversation"), []);
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.strictEqual(store.state.listScrollRestoreRequest, 0);
            store.destroy();
        }
    );

    QUnit.test(
        "a selection kept from the cached tail beyond 200 rows is revalidated",
        async (assert) => {
            const pad = (value) => String(value).padStart(2, "0");
            const rows = Array.from({length: 250}, (_value, index) =>
                conversation(index + 1, {
                    last_activity_at: `2026-09-27 ${pad(
                        23 - Math.floor(index / 60)
                    )}:${pad(59 - (index % 60))}:00`,
                })
            );
            const fixture = inboxStore({items: rows});
            const {store, server, calls} = fixture;
            await store.loadBootstrap();
            store.state.conversations = rows.map((row) => ({...row}));
            store.state.conversationsHaveMore = false;
            store.state.nextConversationCursor = false;
            await store.selectConversation(225);
            // Resolved elsewhere; the preference refresh reads only 200 rows and
            // keeps the cached tail, where the stale selected row sits.
            server.items = rows.map((row) =>
                row.channel_id === 225 ? {...row, state: "resolved"} : row
            );
            const original = store.call;
            store.call = async (method, args, kwargs) => {
                if (method === "set_conversation_preference") {
                    calls.push({method, args, kwargs});
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        item: {...rows[0], preference: {pinned: false, muted: true}},
                    };
                }
                return original(method, args, kwargs);
            };
            assert.ok(await store.setConversationPreference({muted: true}, 1));
            assert.ok(
                methods(calls, "get_conversation").some((call) => call.args[0] === 225),
                "the tail row is read again on the server"
            );
            assert.strictEqual(store.state.selectedChannelId, false);
            store.destroy();
        }
    );

    QUnit.test(
        "muting a row beyond 200 while hiding muted conversations drops it",
        async (assert) => {
            const pad = (value) => String(value).padStart(2, "0");
            const rows = Array.from({length: 250}, (_value, index) =>
                conversation(index + 1, {
                    last_activity_at: `2026-09-27 ${pad(
                        23 - Math.floor(index / 60)
                    )}:${pad(59 - (index % 60))}:00`,
                })
            );
            const {store} = inboxStore({items: rows});
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            store.state.conversations = rows.map((row) => ({...row}));
            store.state.conversationsHaveMore = false;
            const original = store.call;
            store.call = async (method, args, kwargs) => {
                if (method === "set_conversation_preference") {
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        item: {...rows[209], preference: {pinned: false, muted: true}},
                    };
                }
                return original(method, args, kwargs);
            };
            assert.ok(await store.setConversationPreference({muted: true}, 210));
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210),
                "the retained tail does not keep the muted row"
            );
            store.destroy();
        }
    );

    function rememberedContext(extra = {}) {
        return Object.assign(createInboxDocumentContext(), {
            userId: USER_ID,
            channelId: 10,
            listScrollTop: 90,
            loadedCount: 2,
            ...extra,
        });
    }

    function delayFirst(store, method) {
        const original = store.call;
        const gate = {release: null};
        let delayed = false;
        store.call = (name, args, kwargs) => {
            if (name === method && !delayed) {
                delayed = true;
                return new Promise((resolve, reject) => {
                    gate.release = () =>
                        original(name, args, kwargs).then(resolve, reject);
                    gate.fail = (error) => reject(error);
                });
            }
            return original(name, args, kwargs);
        };
        return gate;
    }

    QUnit.test(
        "a mute from another tab drops the row beyond 200 despite an older refresh",
        async (assert) => {
            const pad = (value) => String(value).padStart(2, "0");
            const rows = Array.from({length: 250}, (_value, index) =>
                conversation(index + 1, {
                    last_activity_at: `2026-09-27 ${pad(
                        23 - Math.floor(index / 60)
                    )}:${pad(59 - (index % 60))}:00`,
                })
            );
            const realtime = fakeTimer();
            const {store} = inboxStore({items: rows, realtimeTimer: realtime});
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            store.state.conversations = rows.map((row) => ({...row}));
            store.state.conversationsHaveMore = false;
            // A refresh started before the mute is still at the server.
            const gate = delayFirst(store, "list_conversations");
            const refreshing = store.refreshLoadedConversations({silent: true});
            assert.ok(await settleUntil(() => gate.release));
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "conversation_preference_updated",
                channel_id: 210,
                preference: {pinned: false, muted: true},
            });
            gate.release();
            await refreshing;
            // Selection cancellation now releases its owner immediately. The
            // uncancellable fake transport can still settle its preference.
            await settleUntil(() => false, 50);
            realtime.flush();
            await settleUntil(() => false, 50);
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210),
                "neither the older refresh nor the retained tail brings it back"
            );
            store.destroy();
        }
    );

    function mutedTailFixture(count) {
        const pad = (value) => String(value).padStart(2, "0");
        const rows = Array.from({length: count}, (_value, index) =>
            conversation(index + 1, {
                last_activity_at: `2026-09-27 ${pad(23 - Math.floor(index / 60))}:${pad(
                    59 - (index % 60)
                )}:00`,
            })
        );
        const realtime = fakeTimer();
        return {rows, realtime, ...inboxStore({items: rows, realtimeTimer: realtime})};
    }

    QUnit.test(
        "a muted open conversation beyond 200 leaves once another one is chosen",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(250);
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            store.state.conversations = rows.map((row) => ({...row}));
            store.state.conversationsHaveMore = false;
            await store.selectConversation(210);
            const original = store.call;
            store.call = async (method, args, kwargs) => {
                if (method === "set_conversation_preference") {
                    // The server now reports the conversation as muted.
                    const muted = {
                        ...rows[209],
                        preference: {pinned: false, muted: true},
                    };
                    server.items = server.items.map((item) =>
                        item.channel_id === 210 ? muted : item
                    );
                    return {schema_version: SUPPORTED_SCHEMA_VERSION, item: muted};
                }
                return original(method, args, kwargs);
            };
            assert.ok(await store.setConversationPreference({muted: true}, 210));
            assert.ok(
                store.state.conversations.some((item) => item.channel_id === 210),
                "kept while it is open"
            );
            await store.selectConversation(20);
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210)
            );
            await store.refreshLoadedConversations({silent: true});
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210),
                "the retained tail never brings it back"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a mute event invalidates a page in flight even before the row is loaded",
        async (assert) => {
            const {store} = mutedTailFixture(250);
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            while (store.state.conversations.length < 200) {
                await store.loadMoreConversations();
            }
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210)
            );
            const gate = delayFirst(store, "list_conversations");
            const paging = store.loadMoreConversations();
            assert.ok(await settleUntil(() => gate.release));
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "conversation_preference_updated",
                channel_id: 210,
                preference: {pinned: false, muted: true, revision: 1},
            });
            // The stale page arrives before the synchronization starts.
            gate.release();
            await paging;
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210),
                "the page fetched before the mute cannot list it"
            );
            assert.strictEqual(store.state.listPhase, "ready");
            store.destroy();
        }
    );

    QUnit.test(
        "an external mute of the open conversation applies once another is chosen",
        async (assert) => {
            const {rows, store, realtime} = mutedTailFixture(250);
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            store.state.conversations = rows.map((row) => ({...row}));
            store.state.conversationsHaveMore = false;
            await store.selectConversation(210);
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "conversation_preference_updated",
                channel_id: 210,
                preference: {pinned: false, muted: true},
            });
            assert.ok(
                store.state.conversations.some((item) => item.channel_id === 210),
                "the open conversation stays while open"
            );
            // Another conversation is chosen before the synchronization runs.
            await store.selectConversation(20);
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210)
            );
            realtime.flush();
            await settleUntil(() => false, 50);
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210)
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a refresh captured before a selection change follows the new selection",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(250);
            const muted = {...rows[209], preference: {pinned: false, muted: true}};
            server.items = rows.map((row) => (row.channel_id === 210 ? muted : row));
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            store.state.conversations = server.items.map((row) => ({...row}));
            store.state.conversationsHaveMore = false;
            await store.selectConversation(210);
            const gate = delayFirst(store, "list_conversations");
            const refreshing = store.refreshLoadedConversations({silent: true});
            assert.ok(await settleUntil(() => gate.release));
            await store.selectConversation(20);
            gate.release();
            await refreshing;
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210),
                "the captured tail is filtered with the current selection"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a newer mute learned from the detail discards the tail of a pending refresh",
        async (assert) => {
            const {rows, store, server, realtime} = mutedTailFixture(250);
            const unmuted = {pinned: false, muted: false, revision: 1};
            server.items = rows.map((row) =>
                row.channel_id === 210 ? {...row, preference: unmuted} : row
            );
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            store.state.conversations = server.items.map((row) => ({...row}));
            store.state.conversationsHaveMore = false;
            await store.selectConversation(210);
            // A refresh captures the tail beyond 200 while 210 is still unmuted.
            const gate = delayFirst(store, "list_conversations");
            const refreshing = store.refreshLoadedConversations({silent: true});
            assert.ok(await settleUntil(() => gate.release));
            // Muted elsewhere; this tab missed the event and learns it from the
            // detail of the open conversation.
            server.items = server.items.map((item) =>
                item.channel_id === 210
                    ? {...item, preference: {pinned: false, muted: true, revision: 2}}
                    : item
            );
            await store.refreshSelectedConversation({silent: true});
            assert.strictEqual(
                conversationPreference(store.loadedConversation(210)).revision,
                2
            );
            await store.selectConversation(20);
            gate.release();
            await refreshing;
            // Selection cancellation now releases its owner immediately. The
            // uncancellable fake transport can still settle its preference.
            await settleUntil(() => false, 50);
            realtime.flush();
            await settleUntil(() => false, 50);
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210),
                "the tail captured before the mute does not bring it back"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a late unmute answer for a closed conversation restarts the paged list",
        async (assert) => {
            const {rows, store, server, realtime} = mutedTailFixture(300);
            server.items = rows.map((row) =>
                row.channel_id === 210
                    ? {...row, preference: {pinned: false, muted: true, revision: 1}}
                    : row
            );
            await store.loadBootstrap();
            await store.loadConversations({reset: true});
            while (store.state.conversations.length < 250) {
                await store.loadMoreConversations();
            }
            await store.selectConversation(210);
            // "Sem silenciadas" with the muted conversation still open.
            store.state.filters.excludeMuted = true;
            assert.ok(store.state.conversationsHaveMore, "a seek cursor is saved");
            const original = store.call;
            let answer = null;
            store.call = (method, args, kwargs) => {
                if (method === "set_conversation_preference") {
                    return new Promise((resolve) => {
                        answer = () => {
                            const unmuted = {
                                ...rows[209],
                                preference: {pinned: false, muted: false, revision: 2},
                            };
                            server.items = server.items.map((item) =>
                                item.channel_id === 210 ? unmuted : item
                            );
                            resolve({
                                schema_version: SUPPORTED_SCHEMA_VERSION,
                                item: unmuted,
                            });
                        };
                    });
                }
                return original(method, args, kwargs);
            };
            const unmuting = store.setConversationPreference({muted: false}, 210);
            assert.ok(await settleUntil(() => answer));
            await store.selectConversation(20);
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210)
            );
            // The bus event is missed: only the late answer tells the unmute.
            answer();
            await unmuting;
            realtime.flush();
            await settleUntil(() => false, 50);
            while (
                !store.state.conversations.some((item) => item.channel_id === 210) &&
                store.state.conversationsHaveMore
            ) {
                await store.loadMoreConversations();
            }
            assert.ok(
                store.state.conversations.some((item) => item.channel_id === 210),
                "no retained tail or cursor skips the unmuted conversation"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a newer muted detail stops the restoration of an older unmuted row",
        async (assert) => {
            const realtime = fakeTimer();
            const {store, server} = inboxStore({
                context: rememberedContext(),
                realtimeTimer: realtime,
                items: [
                    conversation(10, {preference: {muted: false, revision: 1}}),
                    conversation(20),
                ],
                // Muted in another tab; this tab missed the event.
                getConversation: (channelId) => ({
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    item: {
                        ...server.items.find((item) => item.channel_id === channelId),
                        preference: {muted: true, revision: 2},
                    },
                }),
            });
            store.state.filters.excludeMuted = true;
            await store.loadBootstrap();
            assert.notOk(store.state.selectedChannelId, "not reopened");
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 10),
                "the muted row leaves the list"
            );
            store.destroy();
        }
    );

    function unmutedBeyondBoundary(server, rows) {
        server.items = server.items.map((item) =>
            item.channel_id === 210
                ? {
                      ...rows[209],
                      last_message: {message_id: 9210},
                      preference: {pinned: false, muted: false, revision: 2},
                  }
                : item
        );
    }

    async function openMutedBeyondBoundary(store, server, rows) {
        server.items = rows.map((row) =>
            row.channel_id === 210
                ? {
                      ...row,
                      unread_count: 1,
                      last_message: {message_id: 9210},
                      preference: {pinned: false, muted: true, revision: 1},
                  }
                : row
        );
        await store.loadBootstrap();
        await store.loadConversations({reset: true});
        while (store.state.conversations.length < 250) {
            await store.loadMoreConversations();
        }
        await store.selectConversation(210);
        store.state.filters.excludeMuted = true;
    }

    async function pageUntil(store, channelId) {
        while (
            !store.state.conversations.some((item) => item.channel_id === channelId) &&
            store.state.conversationsHaveMore
        ) {
            await store.loadMoreConversations();
        }
        return store.state.conversations.some((item) => item.channel_id === channelId);
    }

    QUnit.test(
        "a newer unmute in a late read answer restarts the paged list",
        async (assert) => {
            const {rows, store, server, realtime} = mutedTailFixture(300);
            await openMutedBeyondBoundary(store, server, rows);
            const gate = delayFirst(store, "get_conversation");
            const reading = store.markConversationRead(210);
            assert.ok(await settleUntil(() => gate.release));
            unmutedBeyondBoundary(server, rows);
            await store.selectConversation(20);
            gate.release();
            await reading;
            realtime.flush();
            await settleUntil(() => false, 50);
            assert.ok(await pageUntil(store, 210), "no retained cursor skips it");
            store.destroy();
        }
    );

    QUnit.test(
        "a newer unmute in a late detail answer restarts the paged list",
        async (assert) => {
            const {rows, store, server, realtime} = mutedTailFixture(300);
            await openMutedBeyondBoundary(store, server, rows);
            const gate = delayFirst(store, "get_conversation");
            const refreshing = store.refreshSelectedConversation({silent: true});
            assert.ok(await settleUntil(() => gate.release));
            unmutedBeyondBoundary(server, rows);
            await store.selectConversation(20);
            gate.release();
            await refreshing;
            // Selection cancellation now releases its owner immediately. The
            // uncancellable fake transport can still settle its preference.
            await settleUntil(() => false, 50);
            realtime.flush();
            await settleUntil(() => false, 50);
            assert.ok(await pageUntil(store, 210), "no retained cursor skips it");
            store.destroy();
        }
    );

    function preferenceEvent(channelId, preference) {
        return {
            schema_version: SUPPORTED_SCHEMA_VERSION,
            event_type: "conversation_preference_updated",
            channel_id: channelId,
            preference,
        };
    }

    QUnit.test(
        "paging after an unmute keeps the open conversation and finds the unmuted",
        async (assert) => {
            const {rows, store, server, realtime} = mutedTailFixture(250);
            server.items = rows.map((row) =>
                row.channel_id === 230
                    ? {...row, preference: {pinned: false, muted: true, revision: 1}}
                    : row
            );
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            await store.loadConversations({reset: true});
            while (store.state.conversations.length < 120) {
                await store.loadMoreConversations();
            }
            await store.selectConversation(90);
            // Unmuted in another tab: the list synchronizes.
            server.items = rows.map((row) => ({...row}));
            store.synchronizeNotification(
                preferenceEvent(230, {pinned: false, muted: false, revision: 2})
            );
            // The agent scrolls before the synchronization runs.
            await store.loadMoreConversations();
            assert.strictEqual(store.state.selectedChannelId, 90, "still open");
            assert.ok(store.state.conversations.some((item) => item.channel_id === 90));
            realtime.flush();
            await settleUntil(() => false, 50);
            assert.ok(await pageUntil(store, 230), "the unmuted conversation is found");
            assert.strictEqual(store.state.selectedChannelId, 90);
            store.destroy();
        }
    );

    QUnit.test(
        "a refresh under Sem silenciadas keeps up to 200 rows and a fresh cursor",
        async (assert) => {
            const {store, realtime} = mutedTailFixture(300);
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            await store.loadConversations({reset: true});
            while (store.state.conversations.length < 250) {
                await store.loadMoreConversations();
            }
            store.synchronizeNotification(
                preferenceEvent(140, {pinned: true, muted: false, revision: 1})
            );
            realtime.flush();
            await settleUntil(() => false, 50);
            assert.strictEqual(
                store.state.conversations.length,
                200,
                "no tail beyond 200"
            );
            assert.ok(store.state.conversationsHaveMore);
            await store.loadMoreConversations();
            assert.strictEqual(
                store.state.conversations.length,
                250,
                "paging continues right after the refreshed window"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a pin event under Sem silenciadas keeps the loaded window",
        async (assert) => {
            const {store, realtime} = mutedTailFixture(250);
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            await store.loadConversations({reset: true});
            while (store.state.conversations.length < 150) {
                await store.loadMoreConversations();
            }
            store.synchronizeNotification(
                preferenceEvent(140, {pinned: true, muted: false, revision: 1})
            );
            realtime.flush();
            await settleUntil(() => false, 50);
            assert.ok(store.state.conversations.length >= 150, "no rows are lost");
            store.destroy();
        }
    );

    QUnit.test(
        "a mute during the first page hides the row from the page in flight",
        async (assert) => {
            const {store, calls} = inboxStore({
                items: [conversation(10), conversation(20), conversation(30)],
                realtimeTimer: fakeTimer(),
            });
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            const before = methods(calls, "list_conversations").length;
            const gate = delayFirst(store, "list_conversations");
            const loading = store.loadConversations({reset: true});
            assert.ok(await settleUntil(() => gate.release));
            store.synchronizeNotification(
                preferenceEvent(30, {pinned: false, muted: true, revision: 1})
            );
            assert.strictEqual(store.state.listPhase, "loading", "never an empty list");
            gate.release();
            await loading;
            assert.strictEqual(store.state.listPhase, "ready");
            assert.deepEqual(
                store.state.conversations.map((item) => item.channel_id),
                [10, 20],
                "the newer revision wins over the page answered before it"
            );
            assert.strictEqual(methods(calls, "list_conversations").length - before, 1);
            store.destroy();
        }
    );

    QUnit.test(
        "a late muted answer for a closed conversation keeps the window",
        async (assert) => {
            const {rows, store, server, realtime} = mutedTailFixture(300);
            await openMutedBeyondBoundary(store, server, rows);
            const gate = delayFirst(store, "get_conversation");
            const refreshing = store.refreshSelectedConversation({silent: true});
            assert.ok(await settleUntil(() => gate.release));
            await store.selectConversation(20);
            // It only confirms the mute the list already applied.
            gate.release();
            await refreshing;
            // Selection cancellation now releases its owner immediately. The
            // uncancellable fake transport can still settle its preference.
            await settleUntil(() => false, 50);
            realtime.flush();
            await settleUntil(() => false, 50);
            assert.ok(store.state.conversations.length > 200, "the tail is kept");
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210)
            );
            store.destroy();
        }
    );

    QUnit.test("a cancelled restoration still applies a newer mute", async (assert) => {
        const {store, server} = inboxStore({
            context: rememberedContext(),
            realtimeTimer: fakeTimer(),
            items: [
                conversation(10, {preference: {muted: false, revision: 1}}),
                conversation(20),
            ],
            getConversation: (channelId) => ({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                item: {
                    ...server.items.find((item) => item.channel_id === channelId),
                    preference: {muted: true, revision: 2},
                },
            }),
        });
        store.state.filters.excludeMuted = true;
        const gate = delayFirst(store, "get_conversation");
        const booting = store.loadBootstrap();
        assert.ok(await settleUntil(() => gate.release));
        // The agent chooses another conversation before the answer.
        await store.selectConversation(20);
        gate.release();
        await booting;
        assert.strictEqual(store.state.selectedChannelId, 20);
        assert.notOk(
            store.state.conversations.some((item) => item.channel_id === 10),
            "the newer mute still applies"
        );
        store.destroy();
    });

    QUnit.test(
        "a silent reload replacing a visible first page shows its failure",
        async (assert) => {
            const {store} = inboxStore({realtimeTimer: fakeTimer()});
            await store.loadBootstrap();
            // A search starts a visible first page.
            const gate = delayFirst(store, "list_conversations");
            const visible = store.loadConversations({reset: true});
            assert.ok(await settleUntil(() => gate.release));
            const original = store.call;
            store.call = (method, args, kwargs) =>
                method === "list_conversations"
                    ? Promise.reject(new Error("temporary network failure"))
                    : original(method, args, kwargs);
            // The reload after starting a conversation replaces it and fails.
            assert.notOk(await store.loadConversations({reset: true, silent: true}));
            gate.release();
            await visible;
            assert.strictEqual(
                store.state.listPhase,
                "error",
                "never an endless skeleton"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a synchronization during the first page never leaves the list loading",
        async (assert) => {
            const realtime = fakeTimer();
            const {store} = inboxStore({realtimeTimer: realtime});
            await store.loadBootstrap();
            const gate = delayFirst(store, "list_conversations");
            const loading = store.loadConversations({reset: true});
            assert.ok(await settleUntil(() => gate.release));
            // From now on every list request fails.
            const original = store.call;
            store.call = (method, args, kwargs) =>
                method === "list_conversations"
                    ? Promise.reject(new Error("temporary network failure"))
                    : original(method, args, kwargs);
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "message_created",
                channel_id: 10,
            });
            // The synchronization replaces the visible page and owns its failure.
            realtime.flush();
            assert.ok(await settleUntil(() => store.state.listPhase === "error", 50));
            gate.release();
            await loading;
            assert.strictEqual(
                store.state.listPhase,
                "error",
                "the failure is shown with its retry, never an endless skeleton"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "an action answer during the first page keeps it visible and synchronizes",
        async (assert) => {
            const realtime = fakeTimer();
            const {store, calls} = inboxStore({realtimeTimer: realtime});
            await store.loadBootstrap();
            const gate = delayFirst(store, "list_conversations");
            const loading = store.loadConversations({reset: true});
            assert.ok(await settleUntil(() => gate.release));
            store.cancelListAnswers();
            assert.strictEqual(store.state.listPhase, "loading", "not shown as ready");
            gate.release();
            assert.ok(await loading, "the visible page is applied");
            assert.strictEqual(store.state.listPhase, "ready");
            const before = methods(calls, "list_conversations").length;
            realtime.flush();
            assert.ok(
                await settleUntil(
                    () => methods(calls, "list_conversations").length > before,
                    50
                ),
                "the list synchronizes after the action"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a failed refresh page still proves the revisions it carried",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(150);
            server.items = rows.map((row) =>
                row.channel_id === 10
                    ? {...row, preference: {pinned: false, muted: false, revision: 1}}
                    : row
            );
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            await store.loadConversations({reset: true});
            while (store.state.conversations.length < 150) {
                await store.loadMoreConversations();
            }
            await store.selectConversation(10);
            // Unmuted again elsewhere at revision 3; the first refresh page
            // carries it, the second page fails.
            server.items = server.items.map((item) =>
                item.channel_id === 10
                    ? {...item, preference: {pinned: false, muted: false, revision: 3}}
                    : item
            );
            const original = store.call;
            let listCalls = 0;
            store.call = (method, args, kwargs) => {
                if (method === "list_conversations" && ++listCalls === 2) {
                    return Promise.reject(new Error("temporary network failure"));
                }
                return original(method, args, kwargs);
            };
            assert.notOk(await store.refreshLoadedConversations({silent: true}));
            // An older detail (revision 2, muted) arrives afterwards.
            store.call = async (method, args, kwargs) =>
                method === "get_conversation"
                    ? {
                          schema_version: SUPPORTED_SCHEMA_VERSION,
                          item: {
                              ...server.items.find((item) => item.channel_id === 10),
                              preference: {pinned: false, muted: true, revision: 2},
                          },
                      }
                    : original(method, args, kwargs);
            await store.refreshSelectedConversation({silent: true});
            assert.deepEqual(
                [
                    conversationPreference(store.loadedConversation(10)).muted,
                    conversationPreference(store.loadedConversation(10)).revision,
                ],
                [false, 3],
                "revision 3 from the failed refresh still wins"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a newer unmute in a failed refresh keeps the open row once closed",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(150);
            server.items = rows.map((row) =>
                row.channel_id === 10
                    ? {...row, preference: {pinned: false, muted: true, revision: 1}}
                    : row
            );
            await store.loadBootstrap();
            await store.loadConversations({reset: true});
            while (store.state.conversations.length < 150) {
                await store.loadMoreConversations();
            }
            await store.selectConversation(10);
            store.state.filters.excludeMuted = true;
            // Unmuted elsewhere (event missed); refresh page 1 carries it, page 2 fails.
            server.items = server.items.map((item) =>
                item.channel_id === 10
                    ? {...item, preference: {pinned: false, muted: false, revision: 2}}
                    : item
            );
            const original = store.call;
            let listCalls = 0;
            store.call = (method, args, kwargs) => {
                if (method === "list_conversations" && ++listCalls === 2) {
                    return Promise.reject(new Error("temporary network failure"));
                }
                return original(method, args, kwargs);
            };
            assert.notOk(await store.refreshLoadedConversations({silent: true}));
            store.call = original;
            assert.notOk(conversationPreference(store.loadedConversation(10)).muted);
            await store.selectConversation(20);
            assert.ok(
                store.state.conversations.some((item) => item.channel_id === 10),
                "closed but unmuted, it stays listed"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a superseded page still proves the revisions it carried",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(120);
            await store.loadBootstrap();
            await store.loadConversations({reset: true});
            server.items = rows.map((row) =>
                row.channel_id === 60
                    ? {...row, preference: {pinned: false, muted: true, revision: 4}}
                    : row
            );
            const gate = delayFirst(store, "list_conversations");
            const paging = store.loadMoreConversations();
            assert.ok(await settleUntil(() => gate.release));
            store.cancelListAnswers();
            gate.release();
            assert.notOk(await paging, "the page is superseded");
            assert.strictEqual(
                conversationPreference({preference: store.knownPreference(60)})
                    .revision,
                4
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a late generic action answer with a newer unmute synchronizes the list",
        async (assert) => {
            const {rows, store, server, realtime} = mutedTailFixture(300);
            await openMutedBeyondBoundary(store, server, rows);
            const original = store.call;
            let answer = null;
            store.call = (method, args, kwargs) => {
                if (method === "update_conversation") {
                    return new Promise((resolve) => {
                        answer = () =>
                            resolve({
                                schema_version: SUPPORTED_SCHEMA_VERSION,
                                item: server.items.find(
                                    (item) => item.channel_id === 210
                                ),
                            });
                    });
                }
                return original(method, args, kwargs);
            };
            const updating = store.updateConversation({tag_ids: []}, 210);
            assert.ok(await settleUntil(() => answer));
            // Unmuted elsewhere at revision 2; the bus event is missed.
            unmutedBeyondBoundary(server, rows);
            await store.selectConversation(20);
            answer();
            await updating;
            realtime.flush();
            await settleUntil(() => false, 50);
            assert.ok(await pageUntil(store, 210), "no stale cursor skips it");
            store.destroy();
        }
    );

    QUnit.test(
        "a sync during a filter change never keeps the previous filter tail",
        async (assert) => {
            const {rows, store, server, realtime} = mutedTailFixture(400);
            // One in five conversations is muted.
            server.items = rows.map((row, index) =>
                index % 5 === 4
                    ? {...row, preference: {pinned: false, muted: true, revision: 1}}
                    : row
            );
            await store.loadBootstrap();
            await store.setFilter("excludeMuted", true);
            while (store.state.conversations.length < 250) {
                await store.loadMoreConversations();
            }
            const gate = delayFirst(store, "list_conversations");
            const changing = store.setFilter("excludeMuted", false);
            assert.ok(await settleUntil(() => gate.release));
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "message_created",
                channel_id: 1,
            });
            realtime.flush();
            await settleUntil(() => false, 50);
            gate.release();
            await changing;
            while (store.state.conversationsHaveMore) {
                await store.loadMoreConversations();
            }
            assert.strictEqual(
                new Set(store.state.conversations.map((item) => item.channel_id)).size,
                400,
                "every conversation, muted ones included, is reached"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "the open row kept at the end takes its place when a page brings it",
        async (assert) => {
            const {store, realtime} = mutedTailFixture(300);
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            await store.loadConversations({reset: true});
            while (store.state.conversations.length < 250) {
                await store.loadMoreConversations();
            }
            await store.selectConversation(230);
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "message_created",
                channel_id: 1,
            });
            realtime.flush();
            await settleUntil(() => false, 50);
            assert.strictEqual(store.preservedConversationChannelId, 230);
            await store.loadMoreConversations();
            const ids = store.state.conversations.map((item) => item.channel_id);
            assert.strictEqual(ids.indexOf(230), ids.indexOf(229) + 1, "in order");
            store.destroy();
        }
    );

    QUnit.test(
        "a purge during a visible first page keeps the open conversation",
        async (assert) => {
            const {store} = mutedTailFixture(250);
            await store.loadBootstrap();
            await store.loadConversations({reset: true});
            while (store.state.conversations.length < 150) {
                await store.loadMoreConversations();
            }
            await store.selectConversation(120);
            const gate = delayFirst(store, "list_conversations");
            const loading = store.loadConversations({reset: true});
            assert.ok(await settleUntil(() => gate.release));
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "conversation_updated",
                channel_id: 5,
                retention_purged: true,
            });
            assert.ok(await settleUntil(() => store.state.listPhase === "ready", 50));
            gate.release();
            await loading;
            assert.strictEqual(store.state.selectedChannelId, 120, "still open");
            store.destroy();
        }
    );

    QUnit.test(
        "a purge during the first page never restores an erased preview",
        async (assert) => {
            const realtime = fakeTimer();
            const {store, server} = inboxStore({
                realtimeTimer: realtime,
                items: [
                    conversation(10),
                    conversation(20, {last_message: {message_id: 5, body: "apagado"}}),
                ],
            });
            await store.loadBootstrap();
            // The page is computed now, before the purge, and delivered later
            // as a fresh copy, like any RPC answer.
            const original = store.call;
            let computed = null;
            let deliver = null;
            store.call = (method, args, kwargs) => {
                if (method === "list_conversations" && !deliver) {
                    const answer = original(method, args, kwargs).then(
                        (payload) => (computed = JSON.parse(JSON.stringify(payload)))
                    );
                    return new Promise((resolve) => {
                        deliver = () => answer.then(resolve);
                    });
                }
                return original(method, args, kwargs);
            };
            const loading = store.loadConversations({reset: true});
            assert.ok(await settleUntil(() => deliver && computed));
            server.items = [conversation(10), conversation(20)];
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "conversation_updated",
                channel_id: 20,
                retention_purged: true,
            });
            deliver();
            await loading;
            assert.ok(await settleUntil(() => store.state.listPhase === "ready", 50));
            assert.notOk(
                store.loadedConversation(20).last_message,
                "the page answered before the purge is not applied"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a retention notice during the first page never leaves the list loading",
        async (assert) => {
            const realtime = fakeTimer();
            const {store} = inboxStore({realtimeTimer: realtime});
            await store.loadBootstrap();
            const gate = delayFirst(store, "list_conversations");
            const loading = store.loadConversations({reset: true});
            assert.ok(await settleUntil(() => gate.release));
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "conversation_updated",
                channel_id: 20,
                retention_purged: true,
            });
            realtime.flush();
            gate.release();
            await loading;
            assert.ok(await settleUntil(() => store.state.listPhase === "ready", 50));
            assert.ok(store.state.conversations.length > 0);
            store.destroy();
        }
    );

    // -- L08: "Marcar todas como lidas" -----------------------------------------

    function bulkReadCalls(
        store,
        server,
        {delayPrepare = false, delayMark = false} = {}
    ) {
        const original = store.call;
        const bulk = {prepare: [], mark: [], releasePrepare: null, releaseMark: null};
        store.call = (method, args, kwargs) => {
            if (method === "prepare_mark_conversations_read") {
                bulk.prepare.push(kwargs);
                const exclude = new Set(kwargs.exclude_channel_ids);
                const answer = () => ({
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    targets: server.items
                        .filter(
                            (item) =>
                                item.unread_count > 0 && !exclude.has(item.channel_id)
                        )
                        .map((item) => ({
                            channel_id: item.channel_id,
                            message_id: item.channel_id * 10,
                            max_message_id: item.channel_id * 10,
                        })),
                    remaining: false,
                });
                if (delayPrepare && bulk.prepare.length === 1) {
                    return new Promise((resolve) => {
                        bulk.releasePrepare = () => resolve(answer());
                    });
                }
                return Promise.resolve(answer());
            }
            if (method === "mark_conversations_read") {
                bulk.mark.push(args[0]);
                const ids = new Set(args[0].map((target) => target.channel_id));
                const answer = () => {
                    server.items = server.items.map((item) =>
                        ids.has(item.channel_id) ? {...item, unread_count: 0} : item
                    );
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        marked: ids.size,
                        items: server.items.filter((item) => ids.has(item.channel_id)),
                    };
                };
                if (delayMark) {
                    return new Promise((resolve) => {
                        bulk.releaseMark = () => resolve(answer());
                    });
                }
                return Promise.resolve(answer());
            }
            if (method === "list_conversations" && kwargs.filters.unread_only) {
                const all = server.items;
                server.items = all.filter((item) => item.unread_count > 0);
                return original(method, args, kwargs).finally(() => {
                    server.items = all;
                });
            }
            return original(method, args, kwargs);
        };
        return bulk;
    }

    QUnit.test(
        "mark all read prepares the current list without the open conversation",
        async (assert) => {
            const {store, server} = inboxStore({
                items: [
                    conversation(10, {unread_count: 1}),
                    conversation(20, {unread_count: 2}),
                    conversation(30, {unread_count: 1}),
                ],
            });
            const bulk = bulkReadCalls(store, server);
            await store.loadBootstrap();
            await store.setFilter("unreadOnly", true);
            await store.selectConversation(10);
            const prepared = await store.prepareMarkAllRead();
            assert.deepEqual(bulk.prepare[0], {
                filters: store.conversationFilters(),
                exclude_channel_ids: [10],
            });
            assert.strictEqual(prepared.count, 2);
            const result = await store.markAllRead(prepared);
            assert.deepEqual(result, {marked: 2, remaining: false});
            assert.deepEqual(bulk.mark[0], prepared.targets);
            assert.deepEqual(
                store.state.conversations.map((item) => item.channel_id),
                [10],
                "read ones leave the Unread list; the open one stays"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a preparation answered after the selection changed is made again",
        async (assert) => {
            const {store, server} = inboxStore({
                items: [
                    conversation(10, {unread_count: 1}),
                    conversation(20, {unread_count: 1}),
                    conversation(30, {unread_count: 1}),
                ],
            });
            const bulk = bulkReadCalls(store, server, {delayPrepare: true});
            await store.loadBootstrap();
            await store.selectConversation(10);
            const preparing = store.prepareMarkAllRead();
            assert.ok(await settleUntil(() => bulk.releasePrepare));
            await store.selectConversation(20);
            bulk.releasePrepare();
            const prepared = await preparing;
            assert.deepEqual(
                bulk.prepare.map((kwargs) => kwargs.exclude_channel_ids),
                [[10], [20]]
            );
            assert.deepEqual(
                prepared.targets.map((target) => target.channel_id),
                [10, 30],
                "the new open conversation stays out"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "the list stays busy while marking and the open one stays out",
        async (assert) => {
            const {store, server} = inboxStore({
                items: [
                    conversation(10, {unread_count: 1}),
                    conversation(20, {unread_count: 1}),
                    conversation(30, {unread_count: 1}),
                ],
            });
            const bulk = bulkReadCalls(store, server, {delayMark: true});
            await store.loadBootstrap();
            const prepared = await store.prepareMarkAllRead();
            await store.selectConversation(20);
            const marking = store.markAllRead(prepared);
            assert.ok(await settleUntil(() => bulk.releaseMark));
            assert.ok(store.state.bulkReadPending);
            assert.notOk(await store.selectConversation(30), "no switch meanwhile");
            assert.strictEqual(store.state.selectedChannelId, 20);
            bulk.releaseMark();
            await marking;
            assert.notOk(store.state.bulkReadPending);
            assert.deepEqual(
                bulk.mark[0].map((target) => target.channel_id),
                [10, 30],
                "the conversation opened at the confirmation is left out"
            );
            store.destroy();
        }
    );

    async function loadedTail(store, server, rows, changes) {
        // 250 loaded rows: position 240 is beyond the 200-row refresh window.
        server.items = rows.map((row) => ({
            ...row,
            ...(changes[row.channel_id] || {}),
        }));
        const bulk = bulkReadCalls(store, server);
        await store.loadBootstrap();
        await store.loadConversations({reset: true});
        while (store.state.conversations.length < 250) {
            await store.loadMoreConversations();
        }
        return bulk;
    }

    async function pagedConversation(store, channelId) {
        while (
            !store.loadedConversation(channelId) &&
            store.state.conversationsHaveMore
        ) {
            await store.loadMoreConversations();
        }
        return store.loadedConversation(channelId);
    }

    function deferredCall(store, method) {
        // The answer is computed at the call and delivered later, as a copy.
        const via = store.call;
        const deferred = {deliver: null, fail: null};
        store.call = (name, args, kwargs) => {
            if (name === method && !deferred.deliver) {
                const answer = via(name, args, kwargs).then((payload) =>
                    JSON.parse(JSON.stringify(payload))
                );
                return new Promise((resolve, reject) => {
                    deferred.deliver = () => answer.then(resolve);
                    deferred.fail = (error) => reject(error);
                });
            }
            return via(name, args, kwargs);
        };
        return deferred;
    }

    function notify(store, event_type, channel_id, values = {}) {
        store.synchronizeNotification({
            schema_version: SUPPORTED_SCHEMA_VERSION,
            event_type,
            channel_id,
            ...values,
        });
    }

    QUnit.test(
        "after the action the list reloads without the rows beyond 200",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(250);
            const bulk = await loadedTail(store, server, rows, {
                240: {unread_count: 3},
            });
            const prepared = await store.prepareMarkAllRead();
            assert.deepEqual(
                prepared.targets.map((target) => target.channel_id),
                [240]
            );
            await store.markAllRead(prepared);
            assert.strictEqual(bulk.mark.length, 1);
            assert.strictEqual(
                store.state.conversations.length,
                200,
                "the rows beyond the refresh window are dropped"
            );
            assert.notOk(store.loadedConversation(240));
            assert.ok(store.state.conversationsHaveMore);
            assert.strictEqual(store.state.conversationTotal, 250);
            assert.strictEqual(
                (await pagedConversation(store, 240)).unread_count,
                0,
                "paging brings the server state"
            );
            while (store.state.conversations.length < 250) {
                await store.loadMoreConversations();
            }
            await store.refreshLoadedConversations({silent: true});
            assert.strictEqual(
                store.state.conversations.length,
                250,
                "once a reload was applied, refreshes keep the tail again"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a purge during a delayed answer is not undone, in or beyond the window",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(250);
            const erased = {
                unread_count: 2,
                last_message: {message_id: 7, body: "apagado"},
            };
            await loadedTail(store, server, rows, {20: erased, 240: erased});
            const prepared = await store.prepareMarkAllRead();
            const bulk = deferredCall(store, "mark_conversations_read");
            const marking = store.markAllRead(prepared);
            assert.ok(await settleUntil(() => bulk.deliver));
            server.items = server.items.map((item) =>
                [20, 240].includes(item.channel_id)
                    ? {...item, last_message: false}
                    : item
            );
            notify(store, "conversation_updated", 20, {retention_purged: true});
            notify(store, "conversation_updated", 240, {retention_purged: true});
            const reload = deferredCall(store, "list_conversations");
            bulk.deliver();
            assert.ok(await settleUntil(() => reload.deliver, 50));
            assert.notOk(
                store.loadedConversation(20).last_message ||
                    store.loadedConversation(240).last_message,
                "the answer computed before the purge is not applied"
            );
            reload.deliver();
            await marking;
            assert.notOk(store.loadedConversation(20).last_message);
            assert.strictEqual(store.loadedConversation(20).unread_count, 0);
            assert.notOk(
                store.loadedConversation(240),
                "beyond the window it is dropped"
            );
            const paged = await pagedConversation(store, 240);
            assert.notOk(paged.last_message, "paging brings the erased preview");
            assert.strictEqual(paged.unread_count, 0);
            store.destroy();
        }
    );

    QUnit.test(
        "a message arriving before a delayed answer keeps its row unread",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(250);
            await loadedTail(store, server, rows, {
                20: {unread_count: 1},
                240: {unread_count: 1},
            });
            const prepared = await store.prepareMarkAllRead();
            const bulk = deferredCall(store, "mark_conversations_read");
            const marking = store.markAllRead(prepared);
            assert.ok(await settleUntil(() => bulk.deliver));
            // New messages: the database counts them unread.
            server.items = server.items.map((item) =>
                [20, 240].includes(item.channel_id) ? {...item, unread_count: 1} : item
            );
            notify(store, "message_created", 20);
            notify(store, "message_created", 240);
            const reload = deferredCall(store, "list_conversations");
            bulk.deliver();
            assert.ok(await settleUntil(() => reload.deliver, 50));
            assert.deepEqual(
                [20, 240].map((id) => store.loadedConversation(id).unread_count),
                [1, 1],
                "the stale zero is not applied"
            );
            reload.deliver();
            await marking;
            assert.strictEqual(store.loadedConversation(20).unread_count, 1);
            assert.notOk(store.loadedConversation(240));
            assert.strictEqual((await pagedConversation(store, 240)).unread_count, 1);
            store.destroy();
        }
    );

    QUnit.test(
        "a read in another tab during a delayed answer is not undone",
        async (assert) => {
            const {store, server} = inboxStore({
                realtimeTimer: fakeTimer(),
                items: [
                    conversation(10, {unread_count: 1}),
                    conversation(20, {unread_count: 2}),
                    conversation(30, {unread_count: 1}),
                ],
            });
            bulkReadCalls(store, server);
            await store.loadBootstrap();
            const prepared = await store.prepareMarkAllRead();
            const via = store.call;
            let deliver = null;
            store.call = (method, args, kwargs) => {
                if (method !== "mark_conversations_read") {
                    return via(method, args, kwargs);
                }
                // A message of 20 arrived after the preparation (its event came
                // before the confirmation): the execution conservatively keeps it.
                server.items = server.items.map((item) => {
                    if (item.channel_id === 20) {
                        return {...item, unread_count: 1};
                    }
                    return [10, 30].includes(item.channel_id)
                        ? {...item, unread_count: 0}
                        : item;
                });
                const answer = JSON.parse(
                    JSON.stringify({
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        marked: 3,
                        items: server.items,
                    })
                );
                return new Promise((resolve) => {
                    deliver = () => resolve(answer);
                });
            };
            const marking = store.markAllRead(prepared);
            assert.ok(await settleUntil(() => deliver));
            // Another tab of this user reads 20; the action's own echo for 30,
            // and a read by someone else of 10.
            server.items = server.items.map((item) =>
                item.channel_id === 20 ? {...item, unread_count: 0} : item
            );
            notify(store, "member_seen", 20, {message_id: 201, user_id: USER_ID});
            notify(store, "member_seen", 30, {message_id: 300, user_id: USER_ID});
            notify(store, "member_seen", 10, {message_id: 100, user_id: USER_ID + 1});
            const reload = deferredCall(store, "list_conversations");
            deliver();
            assert.ok(await settleUntil(() => reload.deliver, 50));
            assert.deepEqual(
                [10, 20, 30].map((id) => store.loadedConversation(id).unread_count),
                [0, 2, 0],
                "the count read elsewhere is not overwritten; nothing left unread is"
            );
            reload.deliver();
            await marking;
            assert.strictEqual(store.loadedConversation(20).unread_count, 0);
            store.destroy();
        }
    );

    QUnit.test(
        "a refresh in flight with a tail captured before the action cannot restore it",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(250);
            await loadedTail(store, server, rows, {240: {unread_count: 2}});
            const prepared = await store.prepareMarkAllRead();
            const older = deferredCall(store, "list_conversations");
            const refreshing = store.refreshLoadedConversations({silent: true});
            assert.ok(await settleUntil(() => older.deliver));
            await store.markAllRead(prepared);
            older.deliver();
            await refreshing;
            assert.strictEqual(store.state.conversations.length, 200);
            assert.notOk(store.loadedConversation(240));
            assert.strictEqual((await pagedConversation(store, 240)).unread_count, 0);
            store.destroy();
        }
    );

    QUnit.test(
        "a lost answer to an applied action still reloads without the tail",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(250);
            await loadedTail(store, server, rows, {240: {unread_count: 2}});
            const prepared = await store.prepareMarkAllRead();
            const via = store.call;
            store.call = (method, args, kwargs) => {
                if (method === "mark_conversations_read") {
                    // Applied and committed on the server; the answer is lost.
                    return via(method, args, kwargs).then(() => {
                        throw new Error("connection lost");
                    });
                }
                return via(method, args, kwargs);
            };
            await assert.rejects(store.markAllRead(prepared), /connection lost/);
            assert.notOk(store.state.bulkReadPending);
            assert.strictEqual(store.state.conversations.length, 200);
            assert.notOk(store.loadedConversation(240));
            assert.strictEqual((await pagedConversation(store, 240)).unread_count, 0);
            store.destroy();
        }
    );

    async function uncertainBulkRead(store, server, rows) {
        // The transport fails while the server is still working on the action.
        await loadedTail(store, server, rows, {240: {unread_count: 2}});
        const prepared = await store.prepareMarkAllRead();
        const via = store.call;
        store.call = (method, args, kwargs) =>
            method === "mark_conversations_read"
                ? Promise.reject(new Error("connection lost"))
                : via(method, args, kwargs);
        return prepared;
    }

    async function loadTo250(store) {
        while (store.state.conversations.length < 250) {
            await store.loadMoreConversations();
        }
    }

    QUnit.test(
        "a late commit after a lost answer is found by the next periodic refresh",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(250);
            const prepared = await uncertainBulkRead(store, server, rows);
            await assert.rejects(store.markAllRead(prepared), /connection lost/);
            assert.strictEqual(store.state.conversations.length, 200);
            await loadTo250(store);
            assert.strictEqual(
                store.loadedConversation(240).unread_count,
                2,
                "nothing committed yet"
            );
            // The server commits now, and its notifications are lost.
            server.items = server.items.map((item) =>
                item.channel_id === 240 ? {...item, unread_count: 0} : item
            );
            await store.refreshLoadedConversations({silent: true});
            assert.strictEqual(
                store.state.conversations.length,
                200,
                "the periodic refresh drops the tail"
            );
            assert.strictEqual((await pagedConversation(store, 240)).unread_count, 0);
            store.destroy();
        }
    );

    QUnit.test(
        "a read notification only brings the synchronization forward",
        async (assert) => {
            const {rows, store, server, realtime} = mutedTailFixture(250);
            const prepared = await uncertainBulkRead(store, server, rows);
            await assert.rejects(store.markAllRead(prepared), /connection lost/);
            await loadTo250(store);
            server.items = server.items.map((item) =>
                item.channel_id === 240 ? {...item, unread_count: 0} : item
            );
            notify(store, "member_seen", 240, {message_id: 2400, user_id: USER_ID});
            realtime.flush();
            assert.ok(
                await settleUntil(() => store.state.conversations.length === 200, 50),
                "synchronized at once"
            );
            assert.strictEqual((await pagedConversation(store, 240)).unread_count, 0);
            // An ordinary read never settles the unknown outcome.
            await loadTo250(store);
            notify(store, "member_seen", 30, {message_id: 300, user_id: USER_ID});
            realtime.flush();
            assert.ok(
                await settleUntil(() => store.state.conversations.length === 200, 50)
            );
            await loadTo250(store);
            await store.refreshLoadedConversations({silent: true});
            assert.strictEqual(
                store.state.conversations.length,
                200,
                "every later refresh still drops the tail"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "an unknown outcome reports whether the immediate reload applied",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(250);
            const prepared = await uncertainBulkRead(store, server, rows);
            const via = store.call;
            store.call = (method, args, kwargs) =>
                method === "list_conversations"
                    ? Promise.reject(new Error("offline"))
                    : via(method, args, kwargs);
            const error = await store.markAllRead(prepared).catch((failure) => failure);
            assert.strictEqual(error.message, "connection lost");
            assert.ok(error.bulkReadUncertain);
            assert.strictEqual(error.listReloaded, false, "nothing was reloaded");
            assert.strictEqual(store.state.conversations.length, 250);
            store.call = via;
            await store.refreshLoadedConversations({silent: true});
            assert.strictEqual(
                store.state.conversations.length,
                200,
                "the next refresh drops the tail"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a read by someone else does not synchronize an uncertain list",
        async (assert) => {
            const {rows, store, server, realtime} = mutedTailFixture(250);
            const prepared = await uncertainBulkRead(store, server, rows);
            await assert.rejects(store.markAllRead(prepared), /connection lost/);
            await loadTo250(store);
            notify(store, "member_seen", 240, {
                message_id: 2400,
                user_id: USER_ID + 1,
            });
            realtime.flush();
            await settleUntil(() => false, 20);
            assert.strictEqual(store.state.conversations.length, 250);
            store.destroy();
        }
    );

    QUnit.test(
        "a first page loaded after a failed reload clears the pending tail drop",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(250);
            await loadedTail(store, server, rows, {240: {unread_count: 2}});
            const prepared = await store.prepareMarkAllRead();
            const reload = deferredCall(store, "list_conversations");
            const marking = store.markAllRead(prepared);
            assert.ok(await settleUntil(() => reload.fail));
            reload.fail(new Error("offline"));
            await marking;
            await store.loadConversations({reset: true});
            while (store.state.conversations.length < 250) {
                await store.loadMoreConversations();
            }
            await store.refreshLoadedConversations({silent: true});
            assert.strictEqual(
                store.state.conversations.length,
                250,
                "rows loaded after the action are kept"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a failed reload leaves dropping the tail to the next synchronization",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(250);
            await loadedTail(store, server, rows, {240: {unread_count: 2}});
            const prepared = await store.prepareMarkAllRead();
            const reload = deferredCall(store, "list_conversations");
            const marking = store.markAllRead(prepared);
            assert.ok(await settleUntil(() => reload.fail));
            reload.fail(new Error("offline"));
            await marking;
            assert.strictEqual(
                store.state.conversations.length,
                250,
                "nothing reloaded"
            );
            // Changed on the server meanwhile, with no event for this tab.
            server.items = server.items.map((item) =>
                item.channel_id === 240 ? {...item, unread_count: 1} : item
            );
            await store.refreshLoadedConversations({silent: true});
            assert.strictEqual(
                store.state.conversations.length,
                200,
                "the next refresh still drops the tail"
            );
            assert.strictEqual((await pagedConversation(store, 240)).unread_count, 1);
            store.destroy();
        }
    );

    QUnit.test(
        "a stale answer for the open conversation cannot keep it after an external mute",
        async (assert) => {
            const {rows, store, server, realtime} = mutedTailFixture(250);
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            store.state.conversations = rows.map((row) => ({...row}));
            store.state.conversationsHaveMore = false;
            await store.selectConversation(210);
            // A revalidation of the open conversation started before the mute.
            const gate = delayFirst(store, "get_conversation");
            const revalidating = store.refreshSelectedConversation({silent: true});
            assert.ok(await settleUntil(() => gate.release));
            server.items = server.items.map((item) =>
                item.channel_id === 210
                    ? {...item, preference: {pinned: false, muted: true}}
                    : item
            );
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "conversation_preference_updated",
                channel_id: 210,
                preference: {pinned: false, muted: true},
            });
            // The stale answer (fetched before the mute) arrives afterwards.
            server.items = rows.map((row) => ({...row}));
            gate.release();
            await revalidating;
            server.items = server.items.map((item) =>
                item.channel_id === 210
                    ? {...item, preference: {pinned: false, muted: true}}
                    : item
            );
            await store.selectConversation(20);
            realtime.flush();
            await settleUntil(() => false, 50);
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210),
                "closed and muted, it follows the server list"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a conversation unmuted beyond the refresh boundary appears when paging",
        async (assert) => {
            const {rows, store, server, realtime} = mutedTailFixture(300);
            server.items = rows.map((row) =>
                row.channel_id === 210
                    ? {...row, preference: {pinned: false, muted: true}}
                    : row
            );
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            await store.loadConversations({reset: true});
            while (store.state.conversations.length < 250) {
                await store.loadMoreConversations();
            }
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 210)
            );
            // Unmuted in another tab: it becomes eligible between rows 200 and 250.
            server.items = rows.map((row) => ({...row}));
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "conversation_preference_updated",
                channel_id: 210,
                preference: {pinned: false, muted: false},
            });
            realtime.flush();
            await settleUntil(() => false, 50);
            while (
                !store.state.conversations.some((item) => item.channel_id === 210) &&
                store.state.conversationsHaveMore
            ) {
                await store.loadMoreConversations();
            }
            assert.ok(
                store.state.conversations.some((item) => item.channel_id === 210),
                "no saved cursor skips it"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a row kept only because it was open leaves when another one is chosen",
        async (assert) => {
            const {store, server} = inboxStore({
                items: [
                    conversation(10, {unread_count: 2}),
                    conversation(20, {unread_count: 1}),
                ],
            });
            const original = store.call;
            store.call = (method, args, kwargs) => {
                if (method === "list_conversations" && kwargs.filters.unread_only) {
                    const all = server.items;
                    server.items = all.filter((item) => item.unread_count > 0);
                    return original(method, args, kwargs).finally(() => {
                        server.items = all;
                    });
                }
                return original(method, args, kwargs);
            };
            await store.loadBootstrap();
            await store.setFilter("unreadOnly", true);
            await store.selectConversation(10);
            // Read meanwhile: the Unread list no longer has it, the open one stays.
            server.items = server.items.map((item) =>
                item.channel_id === 10 ? {...item, unread_count: 0} : item
            );
            await store.refreshLoadedConversations({silent: true});
            assert.strictEqual(store.preservedConversationChannelId, 10);
            assert.ok(store.state.conversations.some((item) => item.channel_id === 10));
            await store.selectConversation(20);
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 10),
                "no longer open, it follows the Unread list at once"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "an unmuted conversation is found after a failed and a later refresh",
        async (assert) => {
            const {rows, store, server} = mutedTailFixture(300);
            server.items = rows.map((row) =>
                row.channel_id === 210
                    ? {...row, preference: {pinned: false, muted: true}}
                    : row
            );
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            await store.loadConversations({reset: true});
            while (store.state.conversations.length < 250) {
                await store.loadMoreConversations();
            }
            server.items = rows.map((row) => ({...row}));
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "conversation_preference_updated",
                channel_id: 210,
                preference: {pinned: false, muted: false},
            });
            // The restart fails; then another notification refreshes the list.
            const original = store.call;
            let failures = 1;
            store.call = (method, args, kwargs) => {
                if (method === "list_conversations" && failures > 0) {
                    failures -= 1;
                    return Promise.reject(new Error("temporary network failure"));
                }
                return original(method, args, kwargs);
            };
            assert.notOk(await store.refreshLoadedConversations({silent: true}));
            assert.ok(await store.refreshLoadedConversations({silent: true}));
            while (
                !store.state.conversations.some((item) => item.channel_id === 210) &&
                store.state.conversationsHaveMore
            ) {
                await store.loadMoreConversations();
            }
            assert.ok(
                store.state.conversations.some((item) => item.channel_id === 210)
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a late action answer never brings back a muted conversation no longer open",
        async (assert) => {
            const muted = {pinned: false, muted: true};
            const {store, server} = inboxStore({
                items: [conversation(10, {preference: muted}), conversation(20)],
            });
            await store.loadBootstrap();
            store.state.filters.states = [];
            store.state.filters.excludeMuted = true;
            await store.selectConversation(10);
            const original = store.call;
            let answer = null;
            store.call = (method, args, kwargs) => {
                if (method === "update_conversation") {
                    return new Promise((resolve) => {
                        answer = () =>
                            resolve({
                                schema_version: SUPPORTED_SCHEMA_VERSION,
                                item: {...server.items[0], state: "archived"},
                            });
                    });
                }
                return original(method, args, kwargs);
            };
            const archiving = store.updateConversation({state: "archived"}, 10);
            assert.ok(await settleUntil(() => answer));
            await store.selectConversation(20);
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 10)
            );
            answer();
            await archiving;
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 10),
                "the acknowledgement does not reinsert it"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "an older action answer cannot undo a newer mute of a closed conversation",
        async (assert) => {
            const realtime = fakeTimer();
            const {store, server} = inboxStore({
                items: [conversation(10), conversation(20)],
                realtimeTimer: realtime,
            });
            await store.loadBootstrap();
            store.state.filters.states = [];
            store.state.filters.excludeMuted = true;
            await store.selectConversation(10);
            const original = store.call;
            let answer = null;
            store.call = (method, args, kwargs) => {
                if (method === "update_conversation") {
                    // Serialized before the mute: it still says unmuted.
                    const stale = {...server.items[0], preference: {muted: false}};
                    return new Promise((resolve) => {
                        answer = () =>
                            resolve({
                                schema_version: SUPPORTED_SCHEMA_VERSION,
                                item: stale,
                            });
                    });
                }
                return original(method, args, kwargs);
            };
            const archiving = store.updateConversation({state: "archived"}, 10);
            assert.ok(await settleUntil(() => answer));
            server.items = server.items.map((item) =>
                item.channel_id === 10
                    ? {...item, preference: {pinned: false, muted: true}}
                    : item
            );
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "conversation_preference_updated",
                channel_id: 10,
                preference: {pinned: false, muted: true},
            });
            await store.selectConversation(20);
            realtime.flush();
            await settleUntil(() => false, 50);
            answer();
            await archiving;
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 10),
                "the newer mute wins over the older answer"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a list page requested after a known preference supersedes it",
        async (assert) => {
            const {store, server} = inboxStore();
            await store.loadBootstrap();
            const original = store.call;
            store.call = async (method, args, kwargs) => {
                if (method === "set_conversation_preference") {
                    const muted = {...server.items[0], preference: {muted: true}};
                    server.items = server.items.map((item) =>
                        item.channel_id === 10 ? muted : item
                    );
                    return {schema_version: SUPPORTED_SCHEMA_VERSION, item: muted};
                }
                return original(method, args, kwargs);
            };
            assert.ok(await store.setConversationPreference({muted: true}, 10));
            assert.ok(conversationPreference(store.loadedConversation(10)).muted);
            // Unmuted elsewhere while this tab missed the event.
            server.items = server.items.map((item) =>
                item.channel_id === 10
                    ? {...item, preference: {pinned: false, muted: false}}
                    : item
            );
            await store.refreshLoadedConversations({silent: true});
            assert.notOk(
                conversationPreference(store.loadedConversation(10)).muted,
                "the fresher server page wins"
            );
            store.destroy();
        }
    );

    function preferenceOf(store, channelId) {
        return conversationPreference(store.loadedConversation(channelId));
    }

    QUnit.test(
        "the server revision orders preference snapshots of any kind",
        async (assert) => {
            const {store, server} = inboxStore({
                items: [
                    conversation(10, {preference: {muted: true, revision: 3}}),
                    conversation(20),
                ],
            });
            await store.loadBootstrap();
            await store.selectConversation(10);
            // An older detail answer (revision 2) arrives late.
            server.items = server.items.map((item) =>
                item.channel_id === 10
                    ? {...item, preference: {muted: false, revision: 2}}
                    : item
            );
            await store.refreshSelectedConversation({silent: true});
            assert.deepEqual(
                [preferenceOf(store, 10).muted, preferenceOf(store, 10).revision],
                [true, 3],
                "an older snapshot never undoes a newer preference"
            );
            // An older bus event changes nothing either.
            store.synchronizeNotification({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: "conversation_preference_updated",
                channel_id: 10,
                preference: {muted: false, revision: 1},
            });
            assert.ok(preferenceOf(store, 10).muted);
            // A missed unmute (revision 4) is fixed by any newer snapshot.
            server.items = server.items.map((item) =>
                item.channel_id === 10
                    ? {...item, preference: {muted: false, revision: 4}}
                    : item
            );
            await store.refreshSelectedConversation({silent: true});
            assert.deepEqual(
                [preferenceOf(store, 10).muted, preferenceOf(store, 10).revision],
                [false, 4]
            );
            store.destroy();
        }
    );

    QUnit.test(
        "a late mute answer after a newer unmute keeps the row listed",
        async (assert) => {
            const {store, server} = inboxStore({
                items: [
                    conversation(10, {preference: {muted: false, revision: 4}}),
                    conversation(20),
                ],
            });
            await store.loadBootstrap();
            store.state.filters.excludeMuted = true;
            await store.selectConversation(20);
            const original = store.call;
            store.call = async (method, args, kwargs) => {
                if (method === "set_conversation_preference") {
                    // Serialized before the unmute that already reached the list.
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        item: {
                            ...server.items[0],
                            preference: {muted: true, revision: 3},
                        },
                    };
                }
                return original(method, args, kwargs);
            };
            assert.ok(await store.setConversationPreference({muted: true}, 10));
            assert.ok(
                store.state.conversations.some((item) => item.channel_id === 10),
                "the newer unmuted preference decides, not the patch sent"
            );
            assert.notOk(preferenceOf(store, 10).muted);
            store.destroy();
        }
    );

    QUnit.test(
        "an older muted answer cannot cancel the restoration of an unmuted row",
        async (assert) => {
            const context = rememberedContext();
            const {store, server} = inboxStore({
                context,
                items: [
                    conversation(10, {preference: {muted: false, revision: 4}}),
                    conversation(20),
                ],
                getConversation: (channelId) => ({
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    item: {
                        ...server.items.find((item) => item.channel_id === channelId),
                        preference: {muted: true, revision: 3},
                    },
                }),
            });
            store.state.filters.excludeMuted = true;
            await store.loadBootstrap();
            assert.strictEqual(store.state.selectedChannelId, 10);
            assert.strictEqual(context.channelId, 10);
            store.destroy();
        }
    );

    QUnit.test(
        "a realtime refresh during the first page does not cancel the restoration",
        async (assert) => {
            const {store} = inboxStore({context: rememberedContext()});
            const gate = delayFirst(store, "list_conversations");
            const booting = store.loadBootstrap();
            assert.ok(await settleUntil(() => gate.release));
            // The bus asks for a silent refresh while the first page is pending.
            const refreshing = store.refreshLoadedConversations({silent: true});
            gate.release();
            await refreshing;
            await booting;
            assert.strictEqual(store.state.selectedChannelId, 10);
            assert.strictEqual(store.state.listScrollRestoreRequest, 1);
            assert.strictEqual(store.consumePendingListScroll(), 90);
            store.destroy();
        }
    );

    QUnit.test(
        "a realtime refresh while the server authorizes still restores the conversation",
        async (assert) => {
            const {store} = inboxStore({context: rememberedContext()});
            const gate = delayFirst(store, "get_conversation");
            const booting = store.loadBootstrap();
            assert.ok(await settleUntil(() => gate.release));
            await store.refreshLoadedConversations({silent: true});
            gate.release();
            await booting;
            assert.strictEqual(store.state.selectedChannelId, 10);
            assert.strictEqual(store.state.seenPausedChannelId, 10);
            store.destroy();
        }
    );

    QUnit.test(
        "leaving during the restoration, or after a transient failure, keeps the memory",
        async (assert) => {
            const context = rememberedContext();
            const {store} = inboxStore({context});
            const gate = delayFirst(store, "get_conversation");
            const booting = store.loadBootstrap();
            assert.ok(await settleUntil(() => gate.release));
            store.destroy();
            assert.deepEqual(
                {channelId: context.channelId, listScrollTop: context.listScrollTop},
                {channelId: 10, listScrollTop: 90},
                "the agent changed nothing"
            );
            gate.release();
            await booting;

            const failing = inboxStore({context});
            const blocked = delayFirst(failing.store, "get_conversation");
            const again = failing.store.loadBootstrap();
            assert.ok(await settleUntil(() => blocked.fail));
            blocked.fail(new Error("temporary network failure"));
            await again;
            assert.strictEqual(failing.store.state.selectedChannelId, false);
            failing.store.destroy();
            assert.strictEqual(
                context.channelId,
                10,
                "a transient failure forgets nothing"
            );
        }
    );

    QUnit.test(
        "leaving while the first page loads keeps the memory",
        async (assert) => {
            const context = rememberedContext({query: "Conversa"});
            const {store} = inboxStore({context});
            const gate = delayFirst(store, "list_conversations");
            const booting = store.loadBootstrap();
            assert.ok(await settleUntil(() => gate.release));
            store.destroy();
            const remembered = () => ({
                channelId: context.channelId,
                listScrollTop: context.listScrollTop,
                loadedCount: context.loadedCount,
                query: context.query,
            });
            const expected = {
                channelId: 10,
                listScrollTop: 90,
                loadedCount: 2,
                query: "Conversa",
            };
            assert.deepEqual(remembered(), expected, "the agent chose nothing yet");
            gate.release();
            await booting;
            assert.deepEqual(remembered(), expected);
        }
    );

    QUnit.test(
        "after a transient failure, the agent's own choice is remembered on exit",
        async (assert) => {
            const context = rememberedContext();
            const {store} = inboxStore({context});
            const blocked = delayFirst(store, "get_conversation");
            const booting = store.loadBootstrap();
            assert.ok(await settleUntil(() => blocked.fail));
            blocked.fail(new Error("temporary network failure"));
            await booting;
            assert.strictEqual(store.state.selectedChannelId, false);
            await store.selectConversation(20);
            store.destroy();
            assert.strictEqual(
                context.channelId,
                20,
                "a selection supersedes the failed restoration"
            );

            const searched = rememberedContext();
            const second = inboxStore({context: searched});
            const failing = delayFirst(second.store, "get_conversation");
            const again = second.store.loadBootstrap();
            assert.ok(await settleUntil(() => failing.fail));
            failing.fail(new Error("temporary network failure"));
            await again;
            second.store.setFilter("query", "Ana");
            second.store.destroy();
            assert.deepEqual(
                {channelId: searched.channelId, query: searched.query},
                {channelId: false, query: "Ana"},
                "a filter edit supersedes it too"
            );
        }
    );

    QUnit.test(
        "choosing another conversation while the restoration is pending wins on exit",
        async (assert) => {
            const context = rememberedContext();
            const {store} = inboxStore({context});
            const gate = delayFirst(store, "get_conversation");
            const booting = store.loadBootstrap();
            assert.ok(await settleUntil(() => gate.release));
            await store.selectConversation(20);
            store.destroy();
            assert.strictEqual(context.channelId, 20);
            gate.release();
            await booting;
            assert.strictEqual(context.channelId, 20);
        }
    );

    QUnit.test(
        "a failed refresh that superseded the first page decides nothing",
        async (assert) => {
            const context = rememberedContext();
            const {store, calls} = inboxStore({context});
            const original = store.call;
            let releaseFirst = null;
            let listCalls = 0;
            store.call = (method, args, kwargs) => {
                if (method === "list_conversations") {
                    listCalls += 1;
                    if (listCalls === 1) {
                        return new Promise((resolve) => {
                            releaseFirst = () =>
                                resolve(original(method, args, kwargs));
                        });
                    }
                    if (listCalls === 2) {
                        return Promise.reject(new Error("temporary network failure"));
                    }
                }
                return original(method, args, kwargs);
            };
            const booting = store.loadBootstrap();
            assert.ok(await settleUntil(() => releaseFirst));
            // The bus refresh supersedes the first page and then fails; the
            // first page's own answer arrives obsolete.
            await store.refreshLoadedConversations({silent: true});
            releaseFirst();
            await booting;
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.deepEqual(methods(calls, "get_conversation"), []);
            assert.strictEqual(store.documentRestore.outcome, "failed");
            store.destroy();
            assert.strictEqual(
                context.channelId,
                10,
                "no server window decided that the conversation left the filters"
            );
        }
    );

    QUnit.test(
        "pages overtaken by realtime refreshes still let the full window restore",
        async (assert) => {
            const items = Array.from({length: 260}, (_value, index) =>
                conversation(index + 1)
            );
            const context = rememberedContext({channelId: 190, loadedCount: 200});
            const {store} = inboxStore({context, items});
            const original = store.call;
            const gates = [];
            let pages = 0;
            store.call = (method, args, kwargs) => {
                if (method === "list_conversations" && kwargs.cursor) {
                    pages += 1;
                    if (pages <= 2) {
                        return new Promise((resolve, reject) => {
                            gates.push(() =>
                                original(method, args, kwargs).then(resolve, reject)
                            );
                        });
                    }
                }
                return original(method, args, kwargs);
            };
            const booting = store.loadBootstrap();
            for (let index = 0; index < 2; index += 1) {
                assert.ok(await settleUntil(() => gates.length > index, 200));
                // A bus refresh overtakes the pending page, which adds nothing.
                await store.refreshLoadedConversations({silent: true});
                gates[index]();
            }
            await booting;
            assert.strictEqual(store.state.conversations.length, 200);
            assert.strictEqual(
                store.state.selectedChannelId,
                190,
                "the last page completed the window: it is checked, not discarded"
            );
            assert.strictEqual(store.state.listScrollRestoreRequest, 1);
            store.destroy();
        }
    );

    QUnit.test(
        "a deletion during the first page waits for its refresh instead of giving up",
        async (assert) => {
            const realtime = fakeTimer();
            const context = rememberedContext();
            const {store} = inboxStore({context, realtimeTimer: realtime});
            const gate = delayFirst(store, "list_conversations");
            const booting = store.loadBootstrap();
            assert.ok(await settleUntil(() => gate.release));
            // What the bus handler does for another agent's deletion.
            store.forgetDeletedConversation(99);
            store.scheduleSynchronization(false, false);
            gate.release();
            assert.ok(
                await settleUntil(() => store.syncWaiters.length > 0, 200),
                "the restoration waits for the scheduled refresh"
            );
            realtime.flush();
            await booting;
            assert.strictEqual(store.state.selectedChannelId, 10);
            assert.strictEqual(store.state.listScrollRestoreRequest, 1);
            store.destroy();
        }
    );

    QUnit.test(
        "a window fetched before bootstrap never certifies the saved filters",
        async (assert) => {
            const realtime = fakeTimer();
            const storage = fakeStorage({
                [inboxStateStorageKey(DATABASE, USER_ID)]: saved({accountId: 2}),
            });
            const items = [
                ...Array.from({length: 55}, (_value, index) =>
                    conversation(101 + index)
                ),
                conversation(20, {account: {id: 2, name: "Atendimento"}}),
            ];
            const context = rememberedContext({channelId: 20, loadedCount: 1});
            const {store} = inboxStore({
                context,
                storage,
                items,
                realtimeTimer: realtime,
            });
            // A realtime refresh ran with the default filters before bootstrap:
            // its first 50 rows do not include conversation 20.
            await store.refreshLoadedConversations({silent: true});
            const gate = delayFirst(store, "list_conversations");
            const booting = store.loadBootstrap();
            assert.ok(await settleUntil(() => gate.release, 200));
            store.forgetDeletedConversation(99);
            store.scheduleSynchronization(false, false);
            gate.release();
            assert.ok(await settleUntil(() => store.syncWaiters.length > 0, 200));
            realtime.flush();
            await booting;
            assert.strictEqual(store.state.filters.accountId, 2);
            assert.strictEqual(
                store.state.selectedChannelId,
                20,
                "only a window of the saved filters decides"
            );
            store.destroy();
            assert.strictEqual(context.channelId, 20);
        }
    );

    QUnit.test(
        "a leaving inbox hands over before the next boots and never overwrites it",
        async (assert) => {
            const context = rememberedContext();
            const storage = fakeStorage();
            const first = inboxStore({context, storage});
            await first.store.loadBootstrap();
            await first.store.selectConversation(20);
            first.store.setFilter("accountId", 1);
            assert.ok(first.store.inboxPreferencesDirty, "a preference is pending");
            // The action service asks the leaving controller first; the next
            // inbox then boots before the previous one is destroyed.
            assert.strictEqual(first.store.handOffDocument(), true);
            const second = inboxStore({context, storage});
            await second.store.loadBootstrap();
            assert.strictEqual(second.store.state.selectedChannelId, 20);
            assert.strictEqual(second.store.state.filters.accountId, 1);
            await second.store.selectConversation(10);
            second.store.handOffDocument();
            first.store.setFilter("accountId", 2);
            first.store.destroy();
            // Nothing the replaced inbox scheduled may still fire.
            first.timer.flush();
            assert.strictEqual(
                context.channelId,
                10,
                "the replaced inbox no longer writes the document memory"
            );
            assert.strictEqual(
                JSON.parse(storage.getItem(inboxStateStorageKey(DATABASE, USER_ID)))
                    .filters.accountId,
                1,
                "nor its late preferences"
            );
            second.store.destroy();
        }
    );

    function accessDenied() {
        return Object.assign(new Error("denied"), {
            data: {name: "odoo.exceptions.AccessError"},
        });
    }

    QUnit.test(
        "a timeline refused right after authorization clears the restoration silently",
        async (assert) => {
            const context = rememberedContext();
            const {store, notifications} = inboxStore({context});
            delete store.loadTimeline;
            const original = store.call;
            store.call = (method, args, kwargs) =>
                method === "get_timeline"
                    ? Promise.reject(accessDenied())
                    : original(method, args, kwargs);
            await store.loadBootstrap();
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 10)
            );
            assert.deepEqual(notifications, [], "nothing reveals the conversation");
            assert.strictEqual(store.documentRestore.outcome, "done");
            store.destroy();
            assert.strictEqual(context.channelId, false, "the memory forgets it");
        }
    );

    QUnit.test(
        "a transient timeline failure while restoring keeps the conversation",
        async (assert) => {
            const context = rememberedContext();
            const {store, notifications} = inboxStore({context});
            delete store.loadTimeline;
            const original = store.call;
            store.call = (method, args, kwargs) =>
                method === "get_timeline"
                    ? Promise.reject(new Error("temporary network failure"))
                    : original(method, args, kwargs);
            await store.loadBootstrap();
            assert.strictEqual(store.state.selectedChannelId, 10);
            assert.strictEqual(store.state.timelinePhase, "error");
            assert.strictEqual(notifications.length, 1, "the failure is shown");
            store.destroy();
            assert.strictEqual(context.channelId, 10, "and forgets nothing");
        }
    );

    QUnit.test(
        "a refusal of a refresh that replaced the restored timeline still clears it",
        async (assert) => {
            const context = rememberedContext();
            const {store, notifications} = inboxStore({context});
            delete store.loadTimeline;
            const original = store.call;
            let release = null;
            let timelines = 0;
            store.call = (method, args, kwargs) => {
                if (method === "get_timeline") {
                    timelines += 1;
                    if (timelines === 1) {
                        return new Promise((_resolve, reject) => {
                            release = () => reject(accessDenied());
                        });
                    }
                    return Promise.reject(accessDenied());
                }
                return original(method, args, kwargs);
            };
            const booting = store.loadBootstrap();
            assert.ok(await settleUntil(() => release, 200));
            // A realtime synchronization replaces the pending first page.
            await store.refreshLatestTimeline();
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.strictEqual(context.channelId, false);
            release();
            await booting;
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 10)
            );
            assert.deepEqual(notifications, []);
            store.destroy();
            assert.strictEqual(context.channelId, false);
        }
    );

    QUnit.test(
        "a background refresh refused during the restoration clears it silently",
        async (assert) => {
            const context = rememberedContext();
            let authorizations = 0;
            const {store, notifications, server} = inboxStore({
                context,
                getConversation: (channelId) => {
                    authorizations += 1;
                    if (authorizations > 1) {
                        return Promise.reject(accessDenied());
                    }
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        item: server.items.find(
                            (item) => item.channel_id === channelId
                        ),
                    };
                },
            });
            delete store.loadTimeline;
            const original = store.call;
            let release = null;
            store.call = (method, args, kwargs) =>
                method === "get_timeline"
                    ? new Promise((resolve) => {
                          release = () =>
                              resolve({
                                  schema_version: SUPPORTED_SCHEMA_VERSION,
                                  channel_id: 10,
                                  items: [],
                              });
                      })
                    : original(method, args, kwargs);
            const booting = store.loadBootstrap();
            assert.ok(await settleUntil(() => release, 200));
            // The background revalidation of the selection is refused.
            await store.refreshSelectedConversation({silent: true});
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.deepEqual(notifications, [], "nothing reveals the conversation");
            // Leaving before the first timeline settles keeps nothing either.
            store.destroy();
            assert.strictEqual(context.channelId, false);
            release();
            await booting;
            assert.strictEqual(context.channelId, false);
        }
    );

    QUnit.test(
        "a conversation read elsewhere since the unread list is not restored",
        async (assert) => {
            const storage = fakeStorage({
                [inboxStateStorageKey(DATABASE, USER_ID)]: saved({unreadOnly: true}),
            });
            const context = rememberedContext();
            const {store, calls, timelines, server} = inboxStore({
                context,
                storage,
                items: [conversation(10, {unread_count: 2}), conversation(20)],
                getConversation: (channelId) => ({
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    item: {
                        ...server.items.find((item) => item.channel_id === channelId),
                        unread_count: 0,
                    },
                }),
            });
            await store.loadBootstrap();
            assert.ok(store.state.filters.unreadOnly);
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.deepEqual(timelines, []);
            assert.deepEqual(methods(calls, "mark_seen"), []);
            assert.strictEqual(context.channelId, false, "forgotten silently");
            store.destroy();
        }
    );

    QUnit.test(
        "a late timeline refusal never clears a newer choice of the agent",
        async (assert) => {
            const context = rememberedContext();
            const {store} = inboxStore({context});
            const stubbed = store.loadTimeline;
            delete store.loadTimeline;
            const real = store.loadTimeline;
            store.loadTimeline = function (options) {
                return this.state.selectedChannelId === 10
                    ? real.call(this, options)
                    : stubbed(options);
            };
            const original = store.call;
            let refuse = null;
            store.call = (method, args, kwargs) => {
                if (method === "get_timeline") {
                    return new Promise((_resolve, reject) => {
                        refuse = () => reject(accessDenied());
                    });
                }
                return original(method, args, kwargs);
            };
            const booting = store.loadBootstrap();
            assert.ok(await settleUntil(() => refuse, 200));
            await store.selectConversation(20);
            refuse();
            await booting;
            assert.strictEqual(store.state.selectedChannelId, 20);
            assert.ok(store.state.conversations.some((item) => item.channel_id === 10));
            store.destroy();
            assert.strictEqual(context.channelId, 20);
        }
    );

    QUnit.test(
        "an idle tab never overwrites preferences saved later by another tab",
        async (assert) => {
            const key = inboxStateStorageKey(DATABASE, USER_ID);
            const storage = fakeStorage({[key]: saved({accountId: 1})});
            const idle = inboxStore({storage});
            const busy = inboxStore({storage});
            await idle.store.loadBootstrap();
            await busy.store.loadBootstrap();
            await busy.store.setFilter("accountId", 2);
            busy.timer.flush();
            idle.store.onPageHide();
            idle.store.destroy();
            assert.strictEqual(JSON.parse(storage.getItem(key)).filters.accountId, 2);
            busy.store.destroy();
        }
    );

    QUnit.test("a narrow window keeps the saved details preference", async (assert) => {
        const key = inboxStateStorageKey(DATABASE, USER_ID);
        const storage = fakeStorage({[key]: saved({}, {detailsOpen: true})});
        const descriptor = Object.getOwnPropertyDescriptor(window, "innerWidth");
        Object.defineProperty(window, "innerWidth", {configurable: true, value: 800});
        try {
            const {store, timer} = inboxStore({storage});
            await store.loadBootstrap();
            assert.notOk(store.state.detailsOpen, "the pane stays closed here");
            store.rememberInboxLayout({listView: "flat"});
            timer.flush();
            assert.strictEqual(
                JSON.parse(storage.getItem(key)).layout.detailsOpen,
                true,
                "the explicit choice survives"
            );
            store.destroy();
        } finally {
            if (descriptor) {
                Object.defineProperty(window, "innerWidth", descriptor);
            } else {
                delete window.innerWidth;
            }
        }
    });

    QUnit.test(
        "a visit that does not restore the position leaves no stale position behind",
        async (assert) => {
            const context = rememberedContext({listScrollTop: 2400});
            const directed = inboxStore({
                context,
                params: {channel_id: 20},
            });
            await directed.store.loadBootstrap();
            directed.store.destroy();
            assert.strictEqual(context.listScrollTop, 0);
            assert.strictEqual(context.channelId, 20);
        }
    );

    QUnit.test(
        "a refused conversation switch keeps the restored conversation paused",
        async (assert) => {
            const {store, calls} = inboxStore({context: rememberedContext()});
            await store.loadBootstrap();
            assert.strictEqual(store.state.seenPausedChannelId, 10);
            store.registerConversationSelectionGuard(() => false);
            assert.notOk(await store.selectConversation(20));
            assert.strictEqual(store.state.selectedChannelId, 10);
            assert.strictEqual(store.state.seenPausedChannelId, 10);
            assert.notOk(await store.markSeen(100));
            assert.deepEqual(methods(calls, "mark_seen"), []);
            store.destroy();
        }
    );

    QUnit.test(
        "only focus inside the composer resumes reading; other focus may be programmatic",
        async (assert) => {
            const {store} = inboxStore({context: rememberedContext()});
            await store.loadBootstrap();
            const app = Object.create(ContactCenterApp.prototype);
            app.store = store;
            const header = document.createElement("button");
            const composer = document.createElement("footer");
            composer.className = "cc-composer";
            const input = document.createElement("textarea");
            composer.append(input);
            app.onConversationInteraction({type: "focusin", target: header});
            assert.strictEqual(store.state.seenPausedChannelId, 10);
            app.onConversationInteraction({type: "focusin", target: input});
            assert.strictEqual(store.state.seenPausedChannelId, false);
            store.destroy();
        }
    );

    QUnit.test(
        "action parameters apply once: a breadcrumb return follows the memory",
        (assert) => {
            const context = createInboxDocumentContext();
            const action = {params: {channel_id: 42}};
            assert.deepEqual(consumeInboxActionParams(context, action), {
                channel_id: 42,
            });
            assert.strictEqual(consumeInboxActionParams(context, action), false);
            assert.deepEqual(
                consumeInboxActionParams(context, {params: {channel_id: 42}}),
                {channel_id: 42},
                "a new opening from the menu or a link applies its parameters"
            );
            assert.strictEqual(consumeInboxActionParams(context, {}), false);
        }
    );
});
