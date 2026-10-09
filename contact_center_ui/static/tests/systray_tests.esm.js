/** @odoo-module **/

/* global QUnit */

import {
    CONTACT_CENTER_NOTIFICATION_TYPE,
    SUPPORTED_SCHEMA_VERSION,
} from "@contact_center_ui/js/contact_center_model.esm";
import {
    ContactCenterStore,
    createInboxDocumentContext,
} from "@contact_center_ui/js/contact_center_store.esm";
import {
    SYSTRAY_SUMMARY_DEBOUNCE,
    SystraySummary,
} from "@contact_center_ui/js/contact_center_systray.esm";

const USER_ID = 7;

function fakeTimer() {
    const pending = new Map();
    let next = 1;
    let time = 0;
    return {
        pending,
        now: () => time,
        delays: [],
        setTimeout(callback, delay) {
            const id = next++;
            pending.set(id, {callback, delay});
            this.delays.push(delay);
            return id;
        },
        clearTimeout(id) {
            pending.delete(id);
        },
        // Run the callbacks scheduled with this delay, or all of them.
        flush(delay = undefined) {
            const due = [...pending.entries()].filter(
                ([, item]) => delay === undefined || item.delay === delay
            );
            due.forEach(([id]) => pending.delete(id));
            if (due.length) {
                time += Math.max(...due.map(([, item]) => item.delay));
            }
            due.forEach(([, item]) => item.callback());
        },
    };
}

function notification(eventType, values = {}) {
    return {
        type: CONTACT_CENTER_NOTIFICATION_TYPE,
        payload: {
            schema_version: SUPPORTED_SCHEMA_VERSION,
            event_type: eventType,
            channel_id: 10,
            ...values,
        },
    };
}

function summaryFixture(responses = []) {
    const timer = fakeTimer();
    const calls = [];
    const changes = [];
    const summary = new SystraySummary({
        call: () => {
            calls.push(true);
            const response = responses.length
                ? responses.shift()
                : {enabled: true, mine_unread: 1, all_unread: 2};
            return response instanceof Error
                ? Promise.reject(response)
                : Promise.resolve(response);
        },
        userId: USER_ID,
        timer,
        now: timer.now,
        onChange: (value) => changes.push(value),
    });
    return {summary, timer, calls, changes};
}

async function settle() {
    for (let attempt = 0; attempt < 10; attempt += 1) {
        await Promise.resolve();
    }
}

QUnit.module("contact_center_ui > systray", () => {
    QUnit.test("the counts come from the server summary", async (assert) => {
        const {summary, calls, changes} = summaryFixture([
            {enabled: true, mine_unread: 3, all_unread: 7},
        ]);
        summary.start();
        await settle();
        assert.strictEqual(calls.length, 1);
        assert.deepEqual(changes, [{enabled: true, mine: 3, all: 7}]);
        summary.destroy();
    });

    QUnit.test("a user without the agent role shows nothing", async (assert) => {
        const {summary, changes} = summaryFixture([
            {schema_version: 1, enabled: false},
        ]);
        summary.start();
        await settle();
        assert.deepEqual(changes, [{enabled: false, mine: 0, all: 0}]);
        summary.destroy();
    });

    QUnit.test(
        "inbound and outbound messages, muting and own reads reload the summary",
        async (assert) => {
            const {summary, timer, calls} = summaryFixture();
            for (const detail of [
                [notification("message_created", {direction: "inbound"})],
                [notification("message_created", {direction: "outbound"})],
                [notification("conversation_preference_updated")],
                [notification("conversation_updated")],
                [notification("conversation_deleted")],
                [notification("member_seen", {user_id: USER_ID, message_id: 5})],
                // An internal note or a completed follow-up reads for its author.
                [notification("message_updated", {reason: "internal_note_created"})],
                [notification("productivity_updated")],
                [notification("message_deleted")],
            ]) {
                assert.ok(
                    summary.handleNotifications(detail),
                    detail[0].payload.event_type
                );
                const due = [...timer.pending.values()].find(
                    (item) => item.delay !== 30000
                );
                timer.flush(due ? due.delay : undefined);
                await settle();
            }
            assert.strictEqual(calls.length, 9);
            assert.strictEqual(timer.delays[0], SYSTRAY_SUMMARY_DEBOUNCE);
            summary.destroy();
        }
    );

    QUnit.test("another user's read and unrelated events reload nothing", (assert) => {
        const {summary, timer} = summaryFixture();
        assert.notOk(
            summary.handleNotifications([
                notification("member_seen", {user_id: USER_ID + 1, message_id: 5}),
            ])
        );
        assert.notOk(summary.handleNotifications([notification("media_updated")]));
        assert.notOk(summary.handleNotifications([{type: "other", payload: {}}]));
        assert.strictEqual(timer.pending.size, 0);
        summary.destroy();
    });

    QUnit.test("a burst of events is grouped into one request", async (assert) => {
        const {summary, timer, calls} = summaryFixture();
        summary.handleNotifications([notification("message_created")]);
        summary.handleNotifications([notification("message_created")]);
        summary.handleNotifications([notification("conversation_updated")]);
        assert.strictEqual(timer.pending.size, 1);
        timer.flush(SYSTRAY_SUMMARY_DEBOUNCE);
        await settle();
        assert.strictEqual(calls.length, 1);
        summary.destroy();
    });

    QUnit.test(
        "unknown and channel-free conversation invalidations recount conservatively",
        async (assert) => {
            const {summary, timer, calls} = summaryFixture();
            for (const values of [
                {event_type: "future_event"},
                {channel_id: false},
                {
                    channel_id: "10",
                    update_scope_version: 1,
                    update_scope: "identity_name",
                    changed_fields: ["identity_name"],
                },
            ]) {
                assert.ok(
                    summary.handleNotifications([
                        notification("conversation_updated", values),
                    ])
                );
                timer.flush();
                await settle();
            }
            assert.strictEqual(calls.length, 3);
            assert.notOk(
                summary.handleNotifications([
                    notification("member_seen", {channel_id: false, user_id: USER_ID}),
                ])
            );
            assert.notOk(
                summary.handleNotifications([
                    notification("delivery_updated", {channel_id: false}),
                ])
            );
            summary.destroy();
        }
    );

    QUnit.test("a failed request keeps the last counts", async (assert) => {
        const {summary, timer, changes} = summaryFixture([
            {enabled: true, mine_unread: 2, all_unread: 4},
            new Error("offline"),
        ]);
        summary.start();
        await settle();
        assert.ok(summary.schedule());
        timer.flush(2000);
        await settle();
        assert.deepEqual(changes, [{enabled: true, mine: 2, all: 4}]);
        summary.destroy();
        assert.strictEqual(timer.pending.size, 0, "nothing survives the component");
    });

    QUnit.test(
        "sustained events never starve the counts: one request at a time",
        async (assert) => {
            const timer = fakeTimer();
            const pending = [];
            const changes = [];
            const summary = new SystraySummary({
                call: () => new Promise((resolve) => pending.push(resolve)),
                userId: USER_ID,
                timer,
                now: timer.now,
                onChange: (value) => changes.push(value),
            });
            summary.load();
            // Events keep arriving while the first answer is slow.
            for (let index = 0; index < 3; index += 1) {
                summary.handleNotifications([notification("message_created")]);
                timer.flush(SYSTRAY_SUMMARY_DEBOUNCE);
                await settle();
            }
            assert.strictEqual(pending.length, 1, "no overlapping request");
            pending[0]({enabled: true, mine_unread: 1, all_unread: 1});
            await settle();
            assert.deepEqual(changes, [{enabled: true, mine: 1, all: 1}]);
            assert.strictEqual(
                pending.length,
                1,
                "follow-up waits for its scheduled deadline"
            );
            timer.flush(2000);
            await settle();
            assert.strictEqual(pending.length, 2, "one coalesced follow-up request");
            pending[1]({enabled: true, mine_unread: 2, all_unread: 5});
            await settle();
            assert.deepEqual(changes[1], {enabled: true, mine: 2, all: 5});
            assert.strictEqual(pending.length, 2);
            // Destroying cancels what is pending: a late answer changes nothing.
            summary.load();
            timer.flush(2000);
            await settle();
            summary.handleNotifications([notification("message_created")]);
            summary.destroy();
            pending[2]({enabled: true, mine_unread: 9, all_unread: 9});
            await settle();
            assert.strictEqual(changes.length, 2);
            assert.strictEqual(pending.length, 3);
            assert.strictEqual(timer.pending.size, 0);
        }
    );

    QUnit.test(
        "a preset opens the counted list, selects nothing and keeps the saved filters",
        async (assert) => {
            const context = Object.assign(createInboxDocumentContext(), {
                userId: USER_ID,
                channelId: 10,
            });
            const store = new ContactCenterStore({
                orm: {},
                busService: new EventTarget(),
                notification: {add: () => undefined},
                inboxPreferenceStorage: {
                    getItem: () => null,
                    setItem: () => undefined,
                    removeItem: () => undefined,
                },
                inboxContext: context,
                inboxPreferenceTimer: fakeTimer(),
                operationDatabase: "systray-test",
                initialActionParams: {preset: "mine_unread"},
            });
            const lists = [];
            store.call = async (method, _args, kwargs = {}) => {
                if (method === "bootstrap") {
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        user: {id: USER_ID},
                        accounts: [],
                        agents: [],
                        tags: [],
                        capabilities: {},
                    };
                }
                if (method === "list_conversations") {
                    lists.push(kwargs.filters);
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        items: [],
                        has_more: false,
                        next_cursor: false,
                        total: 0,
                    };
                }
                throw new Error(`Unexpected call ${method}`);
            };
            await store.loadBootstrap();
            assert.deepEqual(lists[0], {
                responsibility: "mine",
                unread_only: true,
                exclude_muted: true,
            });
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.notOk(store.persistFilters, "the preset filters are temporary");
            store.destroy();
        }
    );
    QUnit.test(
        "all four strict metadata scopes skip recount; legacy, unknown and mixed scopes refresh",
        async (assert) => {
            const {summary, timer, calls} = summaryFixture();
            const metadataScopes = [
                "identity_avatar",
                "group_metadata",
                "identity_name",
                "identity_aliases",
            ];
            for (const scope of metadataScopes) {
                assert.notOk(
                    summary.handleNotifications([
                        notification("conversation_updated", {
                            update_scope_version: 1,
                            update_scope: scope,
                            changed_fields: [scope],
                        }),
                    ])
                );
            }
            await settle();
            assert.strictEqual(calls.length, 0);
            for (const values of [
                {},
                {changed_fields: ["identity_name"]},
                {
                    update_scope_version: 2,
                    update_scope: "identity_avatar",
                    changed_fields: ["identity_avatar"],
                },
                {
                    update_scope_version: 1,
                    update_scope: "identity_avatar",
                    changed_fields: ["identity_avatar", "identity_aliases"],
                },
                {
                    update_scope_version: 1,
                    update_scope: "identity_name",
                    changed_fields: ["identity_aliases"],
                },
                {
                    update_scope_version: 1,
                    update_scope: "membership",
                    changed_fields: ["membership"],
                },
            ]) {
                assert.ok(
                    summary.handleNotifications([
                        notification("conversation_updated", values),
                    ])
                );
                timer.flush();
                await settle();
            }
            assert.strictEqual(calls.length, 6);
            for (const scope of metadataScopes) {
                const before = calls.length;
                assert.ok(
                    summary.handleNotifications([
                        notification("conversation_updated", {
                            update_scope_version: 1,
                            update_scope: scope,
                            changed_fields: [scope],
                        }),
                        notification("message_created"),
                    ])
                );
                timer.flush();
                await settle();
                assert.strictEqual(calls.length, before + 1);
            }
            summary.destroy();
        }
    );
});
