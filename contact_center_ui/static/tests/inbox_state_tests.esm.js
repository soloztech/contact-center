/** @odoo-module **/

/* global QUnit */

import {
    ContactCenterStore,
    consumeInboxActionParams,
    createInboxDocumentContext,
    inboxStateStorageKey,
    sanitizeInboxPreferences,
} from "@contact_center_ui/js/contact_center_store.esm";
import {ContactCenterApp} from "@contact_center_ui/js/contact_center_app.esm";
import {SUPPORTED_SCHEMA_VERSION} from "@contact_center_ui/js/contact_center_model.esm";

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
    return {
        pending,
        setTimeout(callback) {
            const id = next++;
            pending.set(id, callback);
            return id;
        },
        clearTimeout(id) {
            pending.delete(id);
        },
        flush() {
            const callbacks = [...pending.values()];
            pending.clear();
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
        operationDatabase: DATABASE,
        initialActionParams: params,
    });
    const calls = [];
    const server = {items};
    store.call = async (method, args = [], kwargs = {}) => {
        calls.push({method, args, kwargs});
        if (method === "bootstrap") {
            return bootstrapPayload(userId);
        }
        if (method === "list_conversations") {
            const accountId = kwargs.filters && kwargs.filters.account_id;
            const rows = server.items.filter(
                (item) => !accountId || item.account.id === accountId
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
